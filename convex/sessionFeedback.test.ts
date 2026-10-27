/// <reference types="vite/client" />
/**
 * ANH-146: the rider's feedback after a session, on the server.
 *
 * - M1: one row per session; the last write in the window wins, from the
 *   rider (site) or the operator (machine), in either order;
 * - M2: the window closes 24 h after the session's end, on the server's
 *   clock, for the site and the machine alike;
 * - M3: a bench session, a session not over, a session of the retired
 *   recording mode take no feedback;
 * - M4: the bounds of a feedback, through the site's mutation;
 * - M5: the `feedback` event of `POST /api/machine/training/events` (contract
 *   1.7) goes to `session_feedback` and never to `training_events`, alone;
 * - M6: the free text and the scores never reach a refusal, an audit entry,
 *   a notification, `training_events` or a log;
 * - M8: the export, by organisation, identifiers only.
 *
 * Who may read and write, role by role, is the authorization matrix
 * (`authorization.matrix.ts`, entries `sessionFeedback.*`).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import type { Doc, Id } from "./_generated/dataModel";
import { EDIT_WINDOW_MS } from "./lib/feedback";
import { machineHeaders } from "./machineAuth.fixtures";
import { EXPORT_PAGE_MAX } from "./sessionFeedback";
import { addSession, as, modules, NOW, seedWorld, type World } from "./test.setup";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

/** The free text of every feedback here: it must never leave `session_feedback`. */
const SECRET = "synthetic-private-feedback-sentinel";
/** Scores no other value of these tests takes. */
const FEEDBACK = {
  nauseaScore: 7,
  dizzinessScore: 9,
  comfort: "bad",
  freeText: SECRET,
} as const;

type SessionFields = {
  machineId?: Id<"machines">;
  userId?: Id<"users">;
  status?: Doc<"sessions">["status"];
  kind?: "auto" | "manual" | null;
  notes?: string;
  endedAt?: number | null;
};

/** A session of `patient` on `machine`, completed, auto, ended at `NOW`, unless told otherwise. */
async function session(w: World, fields: SessionFields = {}) {
  const sessionId = await addSession(w, {
    machineId: fields.machineId ?? w.machine,
    userId: fields.userId ?? w.patient,
    status: fields.status ?? "completed",
    kind: fields.kind === null ? undefined : (fields.kind ?? "auto"),
  });
  await w.t.run((ctx) =>
    ctx.db.patch(sessionId, {
      notes: fields.notes,
      endedAt: fields.endedAt === null ? undefined : (fields.endedAt ?? NOW),
    }),
  );
  return sessionId;
}

const submit = (
  w: World,
  sessionId: Id<"sessions">,
  feedback: Record<string, unknown> = FEEDBACK,
  actor: "patient" | "orgBPatient" = "patient",
) =>
  as(w.t, actor).mutation(api.sessionFeedback.submitSessionFeedback, {
    sessionId,
    ...(feedback as typeof FEEDBACK),
  });

/** What a refused call threw, whole, as text: the code and nothing of the values. */
async function refusalText(call: Promise<unknown>): Promise<string> {
  const error = await call.then(
    () => {
      throw new Error("Expected a refusal");
    },
    (thrown: unknown) => thrown as Error & { data?: unknown },
  );
  return JSON.stringify({ message: error.message, data: error.data });
}

async function machineKey(w: World, machineId: Id<"machines"> = w.machine) {
  const issued = await as(w.t, "admin").mutation(
    api.machineKeys.createMachineKey,
    { machineId },
  );
  return issued.apiKey;
}

const feedbackEvent = (detail: unknown = FEEDBACK, seq = 0) => ({
  seq,
  t: NOW,
  kind: "feedback",
  detail: typeof detail === "string" ? detail : JSON.stringify(detail),
  actor: "operator-1",
});

const phaseEvent = (seq: number) => ({
  seq,
  t: NOW - 1000,
  kind: "phase",
  detail: "hold",
  actor: "system",
});

function postEvents(w: World, key: string, body: unknown) {
  return w.t.fetch("/api/machine/training/events", {
    method: "POST",
    headers: { ...machineHeaders(key), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

const rowsOf = (w: World) =>
  w.t.run((ctx) => ctx.db.query("session_feedback").collect());

const eventsOf = (w: World, sessionId: Id<"sessions">) =>
  w.t.run((ctx) =>
    ctx.db
      .query("training_events")
      .withIndex("by_session_and_seq", (q) => q.eq("sessionId", sessionId))
      .collect(),
  );

describe("ANH-146 M1 one feedback per session, the last write wins", () => {
  it("keeps one row, the operator's, when the rider wrote first", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const sessionId = await session(w);
    await submit(w, sessionId);
    vi.setSystemTime(NOW + 1000);
    const response = await postEvents(w, key, {
      sessionId,
      events: [feedbackEvent({ nauseaScore: 2, dizzinessScore: 1, comfort: "good" })],
    });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ stored: 1, duplicates: 0, rejected: 0 });

    const rows = await rowsOf(w);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      organizationId: w.orgA,
      sessionId,
      nauseaScore: 2,
      dizzinessScore: 1,
      comfort: "good",
      submittedBy: "operator",
      submittedAt: NOW + 1000,
    });
    // Replaced whole: the rider's text is gone with the rider's feedback.
    expect(rows[0]).not.toHaveProperty("freeText");
  });

  it("keeps one row, the rider's, when the operator wrote first", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const sessionId = await session(w);
    expect((await postEvents(w, key, { sessionId, events: [feedbackEvent()] })).status).toBe(200);
    vi.setSystemTime(NOW + 2000);
    await submit(w, sessionId, { nauseaScore: 0, dizzinessScore: 0, comfort: "good" });

    const rows = await rowsOf(w);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      sessionId,
      nauseaScore: 0,
      dizzinessScore: 0,
      comfort: "good",
      submittedBy: "rider",
      submittedAt: NOW + 2000,
    });
    expect(rows[0]).not.toHaveProperty("freeText");
  });

  it("keeps one row when the same side writes twice, and one per session", async () => {
    const w = await seedWorld(modules);
    const first = await session(w);
    const second = await session(w);
    await submit(w, first);
    await submit(w, first, { ...FEEDBACK, nauseaScore: 1 });
    await submit(w, second);
    const rows = await rowsOf(w);
    expect(rows.map((row) => [row.sessionId, row.nauseaScore]).sort()).toEqual(
      [
        [first, 1],
        [second, 7],
      ].sort(),
    );
  });
});

describe("ANH-146 M2 the window: 24 h after the end, on the server's clock", () => {
  it("accepts the rider's feedback 1 ms before it closes, refuses it 1 ms after", async () => {
    const w = await seedWorld(modules);
    const sessionId = await session(w, { endedAt: NOW });
    vi.setSystemTime(NOW + EDIT_WINDOW_MS - 1);
    await submit(w, sessionId);
    expect(await rowsOf(w)).toHaveLength(1);

    vi.setSystemTime(NOW + EDIT_WINDOW_MS + 1);
    await expect(submit(w, sessionId, { ...FEEDBACK, nauseaScore: 1 })).rejects.toThrow(
      "feedback_window_closed",
    );
    expect((await rowsOf(w))[0].nauseaScore).toBe(7);
  });

  it("accepts the machine's feedback 1 ms before it closes, refuses it 1 ms after", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const sessionId = await session(w, { endedAt: NOW });
    vi.setSystemTime(NOW + EDIT_WINDOW_MS - 1);
    expect((await postEvents(w, key, { sessionId, events: [feedbackEvent()] })).status).toBe(200);

    vi.setSystemTime(NOW + EDIT_WINDOW_MS + 1);
    const refused = await postEvents(w, key, {
      sessionId,
      events: [feedbackEvent({ ...FEEDBACK, nauseaScore: 1 })],
    });
    expect(refused.status).toBe(400);
    expect(await refused.json()).toEqual({
      error: "feedback_window_closed",
      message: "The feedback window of the session is closed",
    });
    expect((await rowsOf(w))[0].nauseaScore).toBe(7);
  });

  it("tells the reader when the window closes, and lets only the rider edit", async () => {
    const w = await seedWorld(modules);
    const sessionId = await session(w, { endedAt: NOW - 5000 });
    const read = (actor: "patient" | "manager") =>
      as(w.t, actor).query(api.sessionFeedback.getSessionFeedback, { sessionId });
    expect(await read("patient")).toEqual({
      eligible: true,
      feedback: null,
      endedAt: NOW - 5000,
      editableUntil: NOW - 5000 + EDIT_WINDOW_MS,
      canEdit: true,
    });
    await submit(w, sessionId);
    expect(await read("manager")).toEqual({
      eligible: true,
      feedback: {
        nauseaScore: 7,
        dizzinessScore: 9,
        comfort: "bad",
        freeText: SECRET,
        submittedBy: "rider",
        submittedAt: NOW,
      },
      endedAt: NOW - 5000,
      editableUntil: NOW - 5000 + EDIT_WINDOW_MS,
      canEdit: false,
    });
  });
});

describe("ANH-146 M3 only a finished session with someone on board takes feedback", () => {
  const refusedSessions: [string, SessionFields, string][] = [
    ["a bench session (manual, not occupied)", { kind: "manual", notes: "Occupancy: empty" }, "feedback_not_eligible"],
    ["a manual session with no occupancy noted", { kind: "manual" }, "feedback_not_eligible"],
    ["a session of the retired recording mode", { kind: null, notes: "Occupancy: occupied" }, "feedback_not_eligible"],
    ["an active session", { status: "active", endedAt: null }, "feedback_session_not_ended"],
    ["a pending session", { status: "pending", endedAt: null }, "feedback_session_not_ended"],
    ["an interrupted session", { status: "interrupted", endedAt: null }, "feedback_session_not_ended"],
    ["a completed session without an end", { endedAt: null }, "feedback_session_not_ended"],
  ];

  it.each(refusedSessions)("refuses, from the site, %s", async (_name, fields, code) => {
    const w = await seedWorld(modules);
    const sessionId = await session(w, fields);
    await expect(submit(w, sessionId)).rejects.toThrow(code);
    expect(await rowsOf(w)).toEqual([]);
  });

  it.each(refusedSessions)("refuses, from the machine, %s", async (_name, fields, code) => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const sessionId = await session(w, fields);
    const response = await postEvents(w, key, { sessionId, events: [feedbackEvent()] });
    expect(response.status).toBe(400);
    expect(((await response.json()) as { error: string }).error).toBe(code);
    expect(await rowsOf(w)).toEqual([]);
    expect(await eventsOf(w, sessionId)).toEqual([]);
  });

  it("accepts a manual session the console noted occupied, and a failed one", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const occupied = await session(w, {
      kind: "manual",
      notes: "Operator: x; Occupancy: occupied",
    });
    const failed = await session(w, { status: "failed" });
    await submit(w, occupied);
    expect((await postEvents(w, key, { sessionId: failed, events: [feedbackEvent()] })).status).toBe(200);
    expect((await rowsOf(w)).map((row) => row.sessionId).sort()).toEqual(
      [occupied, failed].sort(),
    );
  });

  it("tells a reader a bench session takes no feedback, and a running one not yet", async () => {
    const w = await seedWorld(modules);
    const bench = await session(w, { kind: "manual" });
    const running = await session(w, { status: "active", endedAt: null });
    const read = (sessionId: Id<"sessions">) =>
      as(w.t, "patient").query(api.sessionFeedback.getSessionFeedback, { sessionId });
    expect(await read(bench)).toEqual({
      eligible: false,
      feedback: null,
      endedAt: NOW,
      editableUntil: null,
      canEdit: false,
    });
    expect(await read(running)).toEqual({
      eligible: true,
      feedback: null,
      endedAt: null,
      editableUntil: null,
      canEdit: false,
    });
  });

  it("answers null for a session that does not exist any more", async () => {
    const w = await seedWorld(modules);
    const sessionId = await session(w);
    await w.t.run((ctx) => ctx.db.delete(sessionId));
    expect(
      await as(w.t, "patient").query(api.sessionFeedback.getSessionFeedback, { sessionId }),
    ).toBeNull();
    await expect(submit(w, sessionId)).rejects.toThrow("session_not_found");
  });
});

describe("ANH-146 M4 the bounds of a feedback, from the site", () => {
  it.each([
    ["nauseaScore", -1],
    ["nauseaScore", 11],
    ["nauseaScore", 2.5],
    ["dizzinessScore", -1],
    ["dizzinessScore", 11],
    ["dizzinessScore", 2.5],
    ["comfort", "great"],
    ["freeText", "x".repeat(501)],
  ])("refuses %s = %s, without naming it", async (field, value) => {
    const w = await seedWorld(modules);
    const sessionId = await session(w);
    const text = await refusalText(submit(w, sessionId, { ...FEEDBACK, [field]: value }));
    expect(text).toContain("feedback_invalid");
    expect(text).not.toContain(String(value));
    expect(text).not.toContain(SECRET);
    expect(await rowsOf(w)).toEqual([]);
  });

  it("accepts 0 and 10, and 500 characters of text", async () => {
    const w = await seedWorld(modules);
    const low = await session(w);
    const high = await session(w);
    const text = "y".repeat(500);
    await submit(w, low, { nauseaScore: 0, dizzinessScore: 0, comfort: "good" });
    await submit(w, high, { nauseaScore: 10, dizzinessScore: 10, comfort: "acceptable", freeText: text });
    const rows = await rowsOf(w);
    expect(rows.find((row) => row.sessionId === low)).toMatchObject({ nauseaScore: 0, dizzinessScore: 0 });
    expect(rows.find((row) => row.sessionId === high)).toMatchObject({
      nauseaScore: 10,
      dizzinessScore: 10,
      freeText: text,
    });
  });

  it.each(["", "   ", "\n"])("stores an empty or blank text as no text: %j", async (freeText) => {
    const w = await seedWorld(modules);
    const sessionId = await session(w);
    await submit(w, sessionId, { ...FEEDBACK, freeText });
    expect((await rowsOf(w))[0]).not.toHaveProperty("freeText");
  });
});

describe("ANH-146 M5 the feedback event of the events route (contract 1.7)", () => {
  it("stores a feedback as the operator's, and no event of the session", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const sessionId = await session(w);
    const response = await postEvents(w, key, { sessionId, events: [feedbackEvent()] });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ stored: 1, duplicates: 0, rejected: 0 });
    expect(await rowsOf(w)).toMatchObject([
      { sessionId, nauseaScore: 7, dizzinessScore: 9, comfort: "bad", freeText: SECRET, submittedBy: "operator" },
    ]);
    expect(await eventsOf(w, sessionId)).toEqual([]);
    expect(
      await w.t.run((ctx) => ctx.db.query("training_events").collect()),
    ).toEqual([]);
  });

  it.each([
    ["mixed with an ordinary event", [phaseEvent(1), feedbackEvent()]],
    ["after an ordinary event", [feedbackEvent(FEEDBACK, 1), phaseEvent(2)]],
    ["sent twice in one request", [feedbackEvent(), feedbackEvent(FEEDBACK, 1)]],
    ["whose detail is not JSON", [feedbackEvent(`nausea=7 ${SECRET}`)]],
    ["whose detail has another key", [feedbackEvent({ ...FEEDBACK, rider: SECRET })]],
    ["whose detail is off the scale", [feedbackEvent({ ...FEEDBACK, nauseaScore: 77 })]],
  ])("refuses a feedback %s, storing nothing", async (_name, events) => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const sessionId = await session(w);
    const response = await postEvents(w, key, { sessionId, events });
    expect(response.status).toBe(400);
    const body = (await response.json()) as { error: string; message: string };
    expect(body.error).toBe("invalid_request");
    expect(body.message).toMatch(/^(A feedback event travels alone|Malformed feedback)$/);
    expect(await rowsOf(w)).toEqual([]);
    expect(await eventsOf(w, sessionId)).toEqual([]);
  });

  it("refuses a feedback on a session of another machine, or on no session", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const elsewhere = await session(w, { machineId: w.otherMachine, userId: w.otherPatient });
    for (const sessionId of [elsewhere, "not-a-session-id"]) {
      const response = await postEvents(w, key, { sessionId, events: [feedbackEvent()] });
      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({
        error: "session_not_found",
        message: "Session not found",
      });
    }
    expect(await rowsOf(w)).toEqual([]);
  });

  it("checks the seq, t and actor of a feedback as any event's", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const sessionId = await session(w);
    for (const broken of [{ seq: -1 }, { t: 1.5 }, { actor: "an operator name" }]) {
      const response = await postEvents(w, key, {
        sessionId,
        events: [{ ...feedbackEvent(), ...broken }],
      });
      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({ error: "invalid_request", message: "Malformed event" });
    }
    expect(await rowsOf(w)).toEqual([]);
  });

  it("still stores the ordinary events as before", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const sessionId = await session(w, { status: "active", endedAt: null });
    await w.t.run((ctx) => ctx.db.patch(sessionId, { startedAt: NOW - 10_000 }));
    const response = await postEvents(w, key, { sessionId, events: [phaseEvent(1), phaseEvent(2)] });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ stored: 2, duplicates: 0, rejected: 0 });
    expect((await eventsOf(w, sessionId)).map((event) => event.seq)).toEqual([1, 2]);
    expect(await rowsOf(w)).toEqual([]);
  });

  it("refuses, called directly, a feedback off the scale", async () => {
    const w = await seedWorld(modules);
    const sessionId = await session(w);
    await expect(
      w.t.mutation(internal.sessionFeedback.storeMachineFeedback, {
        machineId: w.machine,
        sessionId,
        feedback: { ...FEEDBACK, nauseaScore: 11 },
      }),
    ).rejects.toThrow("Malformed feedback");
    expect(await rowsOf(w)).toEqual([]);
  });
});

describe("ANH-146 M6 a feedback leaves session_feedback only", () => {
  it("is in no refusal, audit entry, notification, event or log", async () => {
    const logged: unknown[] = [];
    for (const method of ["log", "info", "warn", "error", "debug"] as const)
      vi.spyOn(console, method).mockImplementation((...args: unknown[]) => {
        logged.push(args);
      });
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const sessionId = await session(w);
    const late = await session(w, { endedAt: NOW - EDIT_WINDOW_MS - 1 });
    const bench = await session(w, { kind: "manual" });

    await submit(w, sessionId);
    const answers: string[] = [];
    for (const call of [
      submit(w, late),
      submit(w, bench),
      submit(w, sessionId, { ...FEEDBACK, freeText: `${SECRET}${"z".repeat(500)}` }),
      submit(w, sessionId, FEEDBACK, "orgBPatient"),
    ])
      answers.push(await refusalText(call));
    for (const body of [
      { sessionId, events: [feedbackEvent()] },
      { sessionId: late, events: [feedbackEvent()] },
      { sessionId: bench, events: [feedbackEvent()] },
      { sessionId, events: [feedbackEvent(`{"freeText": "${SECRET}"`)] },
      { sessionId, events: [phaseEvent(1), feedbackEvent()] },
    ])
      answers.push(await (await postEvents(w, key, body)).text());

    const stored = await w.t.run(async (ctx) => ({
      audit: await ctx.db.query("audit_log").collect(),
      notifications: await ctx.db.query("notifications").collect(),
      events: await ctx.db.query("training_events").collect(),
    }));
    expect(stored.events).toEqual([]);
    const feedbackAudit = stored.audit.filter(
      (row) => row.action === "session.feedback_submitted",
    );
    expect(feedbackAudit).toHaveLength(2);
    for (const row of feedbackAudit) expect(row.details).toBeUndefined();
    for (const text of [
      ...answers,
      JSON.stringify(stored),
      JSON.stringify(logged),
    ]) {
      expect(text).not.toContain(SECRET);
      expect(text).not.toMatch(/"(nauseaScore|dizzinessScore|comfort|freeText)"/);
    }
    // The answers carry no number at all: no score is quoted back.
    for (const answer of answers.slice(0, 4)) expect(answer).not.toMatch(/\d/);
  });
});

describe("ANH-146 M8 the export, identifiers only, by organisation", () => {
  /** A summary of `sessionId` with its leg-tip peaks. */
  async function summarize(w: World, sessionId: Id<"sessions">, organizationId: Id<"organizations">) {
    await w.t.run((ctx) =>
      ctx.db.insert("session_summaries_v2", {
        organizationId,
        sessionId,
        source: "record",
        computedAt: NOW,
        figures: {
          durationS: 1200,
          samples: 6000,
          heartRate: null,
          zone: null,
          maxOutputRpm: 25,
          maxMotorRpm: 1250,
          reference: { maxG: 1.1, maxGRate: 0.01, radiusM: null },
          legTip: { maxG: 2.25, maxGRate: 0.125, radiusM: 1.6, measured: true },
          verdicts: [],
          verdictsOmitted: 0,
          endReason: "programme_complete",
          endRule: null,
          warnings: [],
        },
      }),
    );
  }

  /** Feedback in centre A on a summarised session and on one without summary, and in centre B. */
  async function exportWorld() {
    const w = await seedWorld(modules);
    const summarised = await session(w);
    await summarize(w, summarised, w.orgA);
    await submit(w, summarised);
    vi.setSystemTime(NOW + 1000);
    const bare = await session(w);
    await submit(w, bare, { nauseaScore: 1, dizzinessScore: 2, comfort: "good", freeText: SECRET });
    vi.setSystemTime(NOW + 2000);
    const foreign = await session(w, { machineId: w.orgBMachine, userId: w.orgBPatient });
    await submit(w, foreign, FEEDBACK, "orgBPatient");
    return { w, summarised, bare, foreign };
  }

  const exportAs = (
    w: World,
    actor: "admin" | "orgAdmin" | "orgBAdmin" | "manager" | "patient",
    organizationId?: Id<"organizations">,
    numItems = 50,
  ) =>
    as(w.t, actor).query(api.sessionFeedback.exportFeedback, {
      paginationOpts: { cursor: null, numItems },
      ...(organizationId === undefined ? {} : { organizationId }),
    });

  it("gives the Anheart admin every organisation, newest first, crossed with the summary", async () => {
    const { w, summarised, bare, foreign } = await exportWorld();
    const result = await exportAs(w, "admin");
    expect(result.isDone).toBe(true);
    expect(result.page.map((row) => row.sessionId)).toEqual([foreign, bare, summarised]);
    expect(result.page[2]).toEqual({
      sessionId: summarised,
      organizationId: w.orgA,
      machineId: w.machine,
      userId: w.patient,
      startedAt: NOW,
      durationS: 1200,
      legTipMaxG: 2.25,
      legTipMaxGRate: 0.125,
      nauseaScore: 7,
      dizzinessScore: 9,
      comfort: "bad",
      submittedBy: "rider",
      submittedAt: NOW,
    });
    expect(result.page[1]).toMatchObject({
      durationS: null,
      legTipMaxG: null,
      legTipMaxGRate: null,
      nauseaScore: 1,
    });
  });

  it("filters the Anheart admin's export on the organisation asked for", async () => {
    const { w, foreign } = await exportWorld();
    expect((await exportAs(w, "admin", w.orgB)).page.map((row) => row.sessionId)).toEqual([foreign]);
  });

  it("gives an organisation admin their own organisation only", async () => {
    const { w, summarised, bare, foreign } = await exportWorld();
    expect((await exportAs(w, "orgAdmin")).page.map((row) => row.sessionId)).toEqual([bare, summarised]);
    expect((await exportAs(w, "orgAdmin", w.orgA)).page).toHaveLength(2);
    expect(await exportAs(w, "orgAdmin", w.orgB)).toEqual({ page: [], isDone: true, continueCursor: "" });
    expect((await exportAs(w, "orgBAdmin")).page.map((row) => row.sessionId)).toEqual([foreign]);
  });

  it("refuses a gestionnaire and a rider", async () => {
    const { w } = await exportWorld();
    await expect(exportAs(w, "manager")).rejects.toThrow("not_authorized");
    await expect(exportAs(w, "patient")).rejects.toThrow("not_authorized");
  });

  it("carries no name, e-mail address or free text", async () => {
    const { w } = await exportWorld();
    const text = JSON.stringify(await exportAs(w, "admin"));
    const people = await w.t.run((ctx) => ctx.db.query("users").collect());
    for (const secret of [
      SECRET,
      "freeText",
      ...people.flatMap((person) => [person.firstName, person.lastName, person.email]),
    ])
      expect(text).not.toContain(secret);
  });

  it(`serves ${EXPORT_PAGE_MAX} rows a page at most`, async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      for (let index = 0; index <= EXPORT_PAGE_MAX; index++) {
        const sessionId = await ctx.db.insert("sessions", {
          organizationId: w.orgA,
          machineId: w.machine,
          userId: w.patient,
          status: "completed",
          startedAt: NOW,
          endedAt: NOW,
          channels: ["ECG"],
          kind: "auto",
        });
        await ctx.db.insert("session_feedback", {
          organizationId: w.orgA,
          sessionId,
          nauseaScore: 1,
          dizzinessScore: 1,
          comfort: "good",
          submittedBy: "rider",
          submittedAt: NOW + index,
        });
      }
    });
    const first = await exportAs(w, "admin", undefined, 10_000);
    expect(first.page).toHaveLength(EXPORT_PAGE_MAX);
    expect(first.isDone).toBe(false);
    const rest = await as(w.t, "admin").query(api.sessionFeedback.exportFeedback, {
      paginationOpts: { cursor: first.continueCursor, numItems: 10_000 },
    });
    expect(rest.page).toHaveLength(1);
  });

  it("exports a session with no rider chosen, or of no organisation, with null", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    // Started at the machine, occupied, with no rider picked from the roster.
    const anonymous = await session(w, { kind: "manual", notes: "Occupancy: occupied" });
    await w.t.run((ctx) => ctx.db.patch(anonymous, { userId: undefined }));
    expect((await postEvents(w, key, { sessionId: anonymous, events: [feedbackEvent()] })).status).toBe(200);
    // A row written before organisations: only the Anheart admin reads it.
    const legacy = await session(w);
    await w.t.run(async (ctx) => {
      await ctx.db.patch(legacy, { organizationId: undefined });
      await ctx.db.insert("session_feedback", {
        sessionId: legacy,
        nauseaScore: 1,
        dizzinessScore: 1,
        comfort: "good",
        submittedBy: "rider",
        submittedAt: NOW - 1,
      });
    });
    expect(
      await as(w.t, "admin").query(api.sessionFeedback.getSessionFeedback, { sessionId: anonymous }),
    ).toMatchObject({ eligible: true, canEdit: false });
    expect((await exportAs(w, "admin")).page).toMatchObject([
      { sessionId: anonymous, organizationId: w.orgA, userId: null, submittedBy: "operator" },
      { sessionId: legacy, organizationId: null, userId: w.patient },
    ]);
    expect((await exportAs(w, "orgAdmin")).page.map((row) => row.sessionId)).toEqual([anonymous]);
  });

  it("leaves out a feedback whose session is gone", async () => {
    const { w, summarised, bare } = await exportWorld();
    await w.t.run((ctx) => ctx.db.delete(bare));
    expect((await exportAs(w, "orgAdmin")).page.map((row) => row.sessionId)).toEqual([summarised]);
  });
});

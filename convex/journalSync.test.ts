/// <reference types="vite/client" />
/**
 * ANH-129: what Convex does with a session a machine reads back from its
 * local record and sends, possibly late, possibly twice.
 *
 * - EX-3: a telemetry point is one `(sessionId, t)`, an event one
 *   `(sessionId, seq)`. Sent again, it is stored once, and the machine is
 *   told so in a 200 it can acknowledge;
 * - EX-1 (server half): the route `POST /api/machine/training/events`, its
 *   sizes and what it refuses;
 * - EX-6: no "too old" rule on the training routes. A point or an event is
 *   stored when it is dated inside its session, on the machine's own clock,
 *   and counted `rejected` otherwise, without refusing the batch;
 * - the two clocks: the machine dates its points, the server dates the
 *   session. A machine whose clock is wrong (a Raspberry Pi has no real-time
 *   clock) loses no point for it, and nothing it sends is shown at its date.
 *
 * Everything runs in memory with `convex-test`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { TELEMETRY_FRESH_MS } from "../lib/training";
import { machineHeaders } from "./machineAuth.fixtures";
import {
  EARLIEST_MACHINE_DATE_MS,
  SESSION_WINDOW_MARGIN_MS,
} from "./training";
import {
  configureAnheartOrganization,
  modules,
  NOW,
  seedLegacyWorld,
  seedMachineWorld,
} from "./test.setup";

beforeEach(() => {
  // The server's clock only: every date the server writes is read on it.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  configureAnheartOrganization(null);
});

const world = () => seedMachineWorld(modules);
type MachineWorld = Awaited<ReturnType<typeof world>>;

const TELEMETRY = "/api/machine/training/telemetry";
const EVENTS = "/api/machine/training/events";
const LOCAL = "/api/machine/training/local";
const START = "/api/machine/training/start";
const END = "/api/machine/training/end";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
/** A machine that started without a network: its clock says the 1st of January 1970. */
const NEVER_SET = 10 * MINUTE;

type Outcome = { stored: number; duplicates: number; rejected: number };

/** A request of a machine of the world, with its key and the contract it speaks. */
function send(
  w: MachineWorld,
  path: string,
  body: unknown,
  key: string = w.machineKey,
) {
  return w.t.fetch(path, {
    method: "POST",
    headers: { ...machineHeaders(key), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** The same, for a request that must be accepted: its answer. */
async function accepted<T>(
  w: MachineWorld,
  path: string,
  body: unknown,
): Promise<T> {
  const response = await send(w, path, body);
  expect(response.status).toBe(200);
  return (await response.json()) as T;
}

/** One 1 Hz point, `second` seconds into a session the machine dated `start`. */
const point = (start: number, second: number, bpm = 140) => ({
  t: start + second * 1000,
  elapsedS: second,
  phase: "hold",
  bpm,
  motorRpm: 1000,
  outputRpm: 20,
  setpointMotorRpm: 1000,
  gLoad: 1.1,
  safetyAction: "none",
});

const points = (start: number, from: number, count: number) =>
  Array.from({ length: count }, (_, index) => point(start, from + index));

/** One event of the record, of rank `seq`, `second` seconds into the session. */
const event = (start: number, seq: number, second = seq) => ({
  seq,
  t: start + second * 1000,
  kind: "phase",
  detail: `phase ${seq}`,
  actor: "system",
});

const events = (start: number, from: number, count: number) =>
  Array.from({ length: count }, (_, index) => event(start, from + index));

/** A session started at the machine, as the machine registers it. */
async function localSession(
  w: MachineWorld,
  fields: { startedAt: number; sessionAgeMs?: number; localRef?: string },
): Promise<Id<"sessions">> {
  const { sessionId } = await accepted<{ sessionId: Id<"sessions"> }>(
    w,
    LOCAL,
    {
      localRef: "record-1",
      kind: "manual",
      operatorName: "op-0123456789abcdef",
      ...fields,
    },
  );
  return sessionId;
}

/** A launch from the dashboard, waiting for the machine since `launchedAt`. */
async function launched(
  w: MachineWorld,
  machineId: Id<"machines"> = w.machine,
  launchedAt: number = NOW,
): Promise<Id<"sessions">> {
  return await w.t.run((ctx) =>
    ctx.db.insert("sessions", {
      organizationId: w.organizationId,
      machineId,
      userId: w.patient,
      status: "pending" as const,
      startedAt: launchedAt,
      channels: ["ECG"],
      kind: "auto" as const,
      origin: "remote" as const,
      profileId: "p1",
      subjectHrMax: 180,
      subjectAge: 36,
    }),
  );
}

const telemetryOf = (w: MachineWorld, sessionId: Id<"sessions">) =>
  w.t.run((ctx) =>
    ctx.db
      .query("training_telemetry")
      .withIndex("by_session_and_t", (q) => q.eq("sessionId", sessionId))
      .collect(),
  );

const eventsOf = (w: MachineWorld, sessionId: Id<"sessions">) =>
  w.t.run((ctx) =>
    ctx.db
      .query("training_events")
      .withIndex("by_session_and_seq", (q) => q.eq("sessionId", sessionId))
      .collect(),
  );

const sessionOf = (w: MachineWorld, sessionId: Id<"sessions">) =>
  w.t.run((ctx) => ctx.db.get(sessionId));

/** What the dashboard is told of the session and of its curve. */
async function shown(w: MachineWorld, sessionId: Id<"sessions">) {
  const session = await w.admin.query(api.training.getTrainingSession, {
    sessionId,
  });
  const curve = await w.admin.query(api.training.getSessionTelemetry, {
    sessionId,
  });
  return { session, curve };
}

// ---------------------------------------------------------------------------
// EX-3: telemetry, one row per (sessionId, t)
// ---------------------------------------------------------------------------

describe("ANH-129 EX-3 a telemetry point is stored once, however many times it is sent", () => {
  it("stores one row per point when the same batch is sent twice, and says so", async () => {
    const w = await world();
    const sessionId = await localSession(w, { startedAt: NOW });
    const batch = { sessionId, points: points(NOW, 0, 300) };

    const first = await accepted<Outcome>(w, TELEMETRY, batch);
    const second = await accepted<Outcome>(w, TELEMETRY, batch);

    expect(first).toEqual({ stored: 300, duplicates: 0, rejected: 0 });
    expect(second).toEqual({ stored: 0, duplicates: 300, rejected: 0 });
    const rows = await telemetryOf(w, sessionId);
    expect(rows.map((row) => row.t)).toEqual(
      points(NOW, 0, 300).map((sent) => sent.t),
    );
  });

  it("stores only what is new of a batch that overlaps one already stored", async () => {
    const w = await world();
    const sessionId = await localSession(w, { startedAt: NOW });
    await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: points(NOW, 0, 10),
    });

    const overlapping = await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: points(NOW, 5, 10),
    });

    expect(overlapping).toEqual({ stored: 5, duplicates: 5, rejected: 0 });
    expect((await telemetryOf(w, sessionId)).map((row) => row.elapsedS)).toEqual(
      [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14],
    );
  });

  it("stores once a point that is twice in one batch", async () => {
    const w = await world();
    const sessionId = await localSession(w, { startedAt: NOW });

    const outcome = await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: [point(NOW, 3), point(NOW, 3), point(NOW, 4)],
    });

    expect(outcome).toEqual({ stored: 2, duplicates: 1, rejected: 0 });
    expect(await telemetryOf(w, sessionId)).toHaveLength(2);
  });

  it("keeps the point first stored, with the date the server first received it", async () => {
    const w = await world();
    const sessionId = await localSession(w, { startedAt: NOW });
    vi.setSystemTime(NOW + 5000);
    await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: [point(NOW, 4, 140)],
    });
    const [stored] = await telemetryOf(w, sessionId);

    // The answer was lost: the machine sends the point again a minute later.
    vi.setSystemTime(NOW + 65_000);
    const again = await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: [point(NOW, 4, 199)],
    });

    expect(again).toEqual({ stored: 0, duplicates: 1, rejected: 0 });
    expect(await telemetryOf(w, sessionId)).toEqual([stored]);
    expect(stored.bpm).toBe(140);
    expect(Math.floor(stored._creationTime)).toBe(NOW + 5000);
  });

  it("leaves alone a point stored twice before the rule existed", async () => {
    const w = await world();
    const sessionId = await localSession(w, { startedAt: NOW });
    await w.t.run(async (ctx) => {
      for (let copy = 0; copy < 2; copy++) {
        await ctx.db.insert("training_telemetry", {
          organizationId: w.organizationId,
          sessionId,
          machineId: w.machine,
          ...point(NOW, 7),
        });
      }
    });

    const outcome = await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: [point(NOW, 7), point(NOW, 8)],
    });

    expect(outcome).toEqual({ stored: 1, duplicates: 1, rejected: 0 });
    expect(await telemetryOf(w, sessionId)).toHaveLength(3);
  });

  it("tells a point of one session from the point of another session at the same date", async () => {
    const w = await world();
    const one = await localSession(w, { startedAt: NOW, localRef: "one" });
    const other = await localSession(w, { startedAt: NOW, localRef: "other" });

    await accepted<Outcome>(w, TELEMETRY, {
      sessionId: one,
      points: [point(NOW, 1)],
    });
    const outcome = await accepted<Outcome>(w, TELEMETRY, {
      sessionId: other,
      points: [point(NOW, 1)],
    });

    expect(outcome).toEqual({ stored: 1, duplicates: 0, rejected: 0 });
    expect(await telemetryOf(w, one)).toHaveLength(1);
    expect(await telemetryOf(w, other)).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// EX-1, EX-3: the events route, one row per (sessionId, seq)
// ---------------------------------------------------------------------------

describe("ANH-129 EX-1 POST /api/machine/training/events stores the events of the record", () => {
  it("stores each event with its rank, its date, its kind, its text and its actor, in the session's organisation", async () => {
    const w = await world();
    const sessionId = await localSession(w, { startedAt: NOW });

    const outcome = await accepted<Outcome>(w, EVENTS, {
      sessionId,
      events: [
        {
          seq: 0,
          t: NOW,
          kind: "operator_action",
          detail: "manual session started",
          actor: "op-0123456789abcdef",
        },
        {
          seq: 1,
          t: NOW + 12_400,
          kind: "verdict",
          detail: "hr_above_hard_max (RAMP_DOWN): 171 bpm",
          actor: "system",
        },
        {
          seq: 2,
          t: NOW + 13_000,
          kind: "remote_command",
          detail: "end_requested: arret demande depuis le tableau de bord",
          actor: "remote",
        },
      ],
    });

    expect(outcome).toEqual({ stored: 3, duplicates: 0, rejected: 0 });
    expect(await eventsOf(w, sessionId)).toMatchObject([
      {
        organizationId: w.organizationId,
        machineId: w.machine,
        sessionId,
        seq: 0,
        t: NOW,
        kind: "operator_action",
        detail: "manual session started",
        actor: "op-0123456789abcdef",
      },
      {
        seq: 1,
        t: NOW + 12_400,
        kind: "verdict",
        detail: "hr_above_hard_max (RAMP_DOWN): 171 bpm",
        actor: "system",
      },
      { seq: 2, kind: "remote_command", actor: "remote" },
    ]);
  });

  it("accepts 200 events of 2000 characters in one request", async () => {
    const w = await world();
    const sessionId = await localSession(w, { startedAt: NOW });
    const batch = events(NOW, 0, 200).map((sent) => ({
      ...sent,
      detail: "x".repeat(2000),
    }));

    const outcome = await accepted<Outcome>(w, EVENTS, {
      sessionId,
      events: batch,
    });

    expect(outcome).toEqual({ stored: 200, duplicates: 0, rejected: 0 });
  });

  it("accepts a request that carries no event", async () => {
    const w = await world();
    const sessionId = await localSession(w, { startedAt: NOW });

    expect(
      await accepted<Outcome>(w, EVENTS, { sessionId, events: [] }),
    ).toEqual({ stored: 0, duplicates: 0, rejected: 0 });
  });

  it.each([
    ["no body that is an object", [1, 2]],
    ["no session", { events: [] }],
    ["an empty session identifier", { sessionId: "", events: [] }],
    ["no events", { sessionId: "x" }],
    ["events that are not a list", { sessionId: "x", events: "all" }],
    [
      "more than 200 events",
      { sessionId: "x", events: events(NOW, 0, 201) },
    ],
  ])("refuses a request with %s", async (_name, body) => {
    const w = await world();

    const response = await send(w, EVENTS, body);

    expect(response.status).toBe(400);
    expect(((await response.json()) as { error: string }).error).toBe(
      "invalid_request",
    );
  });

  it.each([
    ["null in place of an event", null],
    ["text in place of an event", "phase"],
    ["no rank", { ...event(NOW, 0), seq: undefined }],
    ["a rank that is text", { ...event(NOW, 0), seq: "0" }],
    ["a rank that is not whole", { ...event(NOW, 0), seq: 1.5 }],
    ["a negative rank", { ...event(NOW, 0), seq: -1 }],
    ["a rank that is not finite", { ...event(NOW, 0), seq: "Infinity" }],
    ["no date", { ...event(NOW, 0), t: undefined }],
    ["a date that is text", { ...event(NOW, 0), t: "now" }],
    ["no kind", { ...event(NOW, 0), kind: undefined }],
    ["a kind that is not of the vocabulary's form", { ...event(NOW, 0), kind: "Phase!" }],
    ["a kind of 65 characters", { ...event(NOW, 0), kind: "k".repeat(65) }],
    ["no text", { ...event(NOW, 0), detail: undefined }],
    ["a text of 2001 characters", { ...event(NOW, 0), detail: "x".repeat(2001) }],
    ["no actor", { ...event(NOW, 0), actor: undefined }],
    ["an actor that is a name", { ...event(NOW, 0), actor: "Dr Attending" }],
    ["an empty actor", { ...event(NOW, 0), actor: "" }],
  ])(
    "refuses a batch holding %s, and stores none of it",
    async (_name, intruder) => {
      const w = await world();
      const sessionId = await localSession(w, { startedAt: NOW });

      const response = await send(w, EVENTS, {
        sessionId,
        events: [event(NOW, 0), intruder, event(NOW, 2)],
      });

      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({
        error: "invalid_request",
        message: "Malformed event",
      });
      expect(await eventsOf(w, sessionId)).toEqual([]);
    },
  );

  it("refuses the events of a session of another machine as those of an unknown session, and stores none", async () => {
    const w = await world();
    const foreign = await launched(w, w.otherMachine);

    const response = await send(w, EVENTS, {
      sessionId: foreign,
      events: events(NOW, 0, 3),
    });

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({
      error: "session_not_found",
      message: "Session not found",
    });
    expect(await eventsOf(w, foreign)).toEqual([]);
    // The machine the session belongs to is the one that may write to it.
    const own = await send(
      w,
      EVENTS,
      { sessionId: foreign, events: events(NOW, 0, 3) },
      w.otherKey,
    );
    expect(own.status).toBe(200);
    expect(await eventsOf(w, foreign)).toHaveLength(3);
  });
});

describe("ANH-129 EX-3 an event is stored once, however many times it is sent", () => {
  it("stores one row per event when the same batch is sent twice, and says so", async () => {
    const w = await world();
    const sessionId = await localSession(w, { startedAt: NOW });
    const batch = { sessionId, events: events(NOW, 0, 40) };

    const first = await accepted<Outcome>(w, EVENTS, batch);
    const second = await accepted<Outcome>(w, EVENTS, batch);

    expect(first).toEqual({ stored: 40, duplicates: 0, rejected: 0 });
    expect(second).toEqual({ stored: 0, duplicates: 40, rejected: 0 });
    expect((await eventsOf(w, sessionId)).map((row) => row.seq)).toEqual(
      Array.from({ length: 40 }, (_, seq) => seq),
    );
  });

  it("keeps the event first stored when its rank is sent again with other content", async () => {
    const w = await world();
    const sessionId = await localSession(w, { startedAt: NOW });
    await accepted<Outcome>(w, EVENTS, { sessionId, events: events(NOW, 0, 3) });
    const before = await eventsOf(w, sessionId);

    const again = await accepted<Outcome>(w, EVENTS, {
      sessionId,
      events: [
        { ...event(NOW, 2), detail: "rewritten" },
        event(NOW, 3),
        event(NOW, 3),
      ],
    });

    expect(again).toEqual({ stored: 1, duplicates: 2, rejected: 0 });
    const after = await eventsOf(w, sessionId);
    expect(after.slice(0, 3)).toEqual(before);
    expect(after.map((row) => row.detail)).toEqual([
      "phase 0",
      "phase 1",
      "phase 2",
      "phase 3",
    ]);
  });

  it("tells an event of one session from the event of same rank of another session", async () => {
    const w = await world();
    const one = await localSession(w, { startedAt: NOW, localRef: "one" });
    const other = await localSession(w, { startedAt: NOW, localRef: "other" });
    await accepted<Outcome>(w, EVENTS, {
      sessionId: one,
      events: events(NOW, 0, 2),
    });

    const outcome = await accepted<Outcome>(w, EVENTS, {
      sessionId: other,
      events: events(NOW, 0, 2),
    });

    expect(outcome).toEqual({ stored: 2, duplicates: 0, rejected: 0 });
  });
});

// ---------------------------------------------------------------------------
// EX-6: dated inside the session, on the machine's own clock
// ---------------------------------------------------------------------------

describe("ANH-129 EX-6 a point or an event is stored when it is dated inside its session", () => {
  it("has no 'too old' rule: the record of a session of last week is stored whole", async () => {
    const w = await world();
    const lastWeek = NOW - 7 * 24 * HOUR;
    const sessionId = await localSession(w, { startedAt: lastWeek });

    const telemetry = await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: points(lastWeek, 0, 300),
    });
    const told = await accepted<Outcome>(w, EVENTS, {
      sessionId,
      events: events(lastWeek, 0, 5),
    });

    expect(telemetry).toEqual({ stored: 300, duplicates: 0, rejected: 0 });
    expect(told).toEqual({ stored: 5, duplicates: 0, rejected: 0 });
  });

  it("stores a point up to one minute before the start the machine dated, and counts an earlier one without refusing the batch", async () => {
    const w = await world();
    const sessionId = await localSession(w, { startedAt: NOW });
    const at = (t: number) => ({ ...point(NOW, 0), t });

    const outcome = await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: [
        at(NOW - SESSION_WINDOW_MARGIN_MS - 1),
        at(NOW - SESSION_WINDOW_MARGIN_MS),
        at(NOW),
        at(NOW + 12 * HOUR),
      ],
    });

    expect(outcome).toEqual({ stored: 3, duplicates: 0, rejected: 1 });
    expect((await telemetryOf(w, sessionId)).map((row) => row.t)).toEqual([
      NOW - SESSION_WINDOW_MARGIN_MS,
      NOW,
      NOW + 12 * HOUR,
    ]);
  });

  it("stores a point up to one minute after the end the machine dated, and counts a later one", async () => {
    const w = await world();
    const sessionId = await localSession(w, { startedAt: NOW });
    const endedAt = NOW + 10 * MINUTE;
    await accepted(w, END, { sessionId, failed: false, reason: "operator_stop", endedAt });
    const at = (t: number) => ({ ...point(NOW, 0), t });

    const outcome = await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: [
        at(endedAt),
        at(endedAt + SESSION_WINDOW_MARGIN_MS),
        at(endedAt + SESSION_WINDOW_MARGIN_MS + 1),
      ],
    });

    expect(outcome).toEqual({ stored: 2, duplicates: 0, rejected: 1 });
    expect((await telemetryOf(w, sessionId)).map((row) => row.t)).toEqual([
      endedAt,
      endedAt + SESSION_WINDOW_MARGIN_MS,
    ]);
  });

  it("applies the same bounds to events", async () => {
    const w = await world();
    const sessionId = await localSession(w, { startedAt: NOW });
    const endedAt = NOW + 10 * MINUTE;
    await accepted(w, END, { sessionId, failed: false, reason: "operator_stop", endedAt });
    const at = (seq: number, t: number) => ({ ...event(NOW, seq), t });

    const outcome = await accepted<Outcome>(w, EVENTS, {
      sessionId,
      events: [
        at(0, NOW - SESSION_WINDOW_MARGIN_MS - 1),
        at(1, NOW - SESSION_WINDOW_MARGIN_MS),
        at(2, endedAt + SESSION_WINDOW_MARGIN_MS),
        at(3, endedAt + SESSION_WINDOW_MARGIN_MS + 1),
      ],
    });

    expect(outcome).toEqual({ stored: 2, duplicates: 0, rejected: 2 });
    expect((await eventsOf(w, sessionId)).map((row) => row.seq)).toEqual([1, 2]);
  });

  it("counts a point outside the session again, each time it is sent: it is never stored", async () => {
    const w = await world();
    const sessionId = await localSession(w, { startedAt: NOW });
    const outside = { ...point(NOW, 0), t: NOW - HOUR };

    for (let attempt = 0; attempt < 2; attempt++) {
      expect(
        await accepted<Outcome>(w, TELEMETRY, { sessionId, points: [outside] }),
      ).toEqual({ stored: 0, duplicates: 0, rejected: 1 });
    }
    expect(await telemetryOf(w, sessionId)).toEqual([]);
  });

  it("bounds a launch from the dashboard by the start its machine dated", async () => {
    const w = await world();
    const sessionId = await launched(w);
    await accepted(w, START, { sessionId, startedAt: NOW - 2000 });

    const outcome = await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: [
        { ...point(NOW, 0), t: NOW - 2000 - SESSION_WINDOW_MARGIN_MS - 1 },
        point(NOW - 2000, 0),
      ],
    });

    expect(outcome).toEqual({ stored: 1, duplicates: 0, rejected: 1 });
  });

  it("bounds a session registered by its machine before this rule by the start it already carries", async () => {
    // Such a row has no `machineStartedAt`: its `startedAt` is the machine's.
    const w = await world();
    const sessionId = await w.t.run((ctx) =>
      ctx.db.insert("sessions", {
        organizationId: w.organizationId,
        machineId: w.machine,
        status: "active" as const,
        startedAt: NOW - HOUR,
        channels: ["ECG"],
        kind: "manual" as const,
        origin: "local" as const,
        localRef: "registered-before",
      }),
    );

    const outcome = await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: [
        { ...point(NOW, 0), t: NOW - HOUR - SESSION_WINDOW_MARGIN_MS - 1 },
        point(NOW - HOUR, 0),
      ],
    });

    expect(outcome).toEqual({ stored: 1, duplicates: 0, rejected: 1 });
    const { session, curve } = await shown(w, sessionId);
    expect(session?.lastMeasuredAt).toBe(NOW - HOUR);
    expect(curve.map((served) => served.t)).toEqual([NOW - HOUR]);
  });

  it("sets no lower bound on a launch whose machine dated no start, and keeps the upper one", async () => {
    // A console older than this contract confirms a launch without a date.
    const w = await world();
    const sessionId = await launched(w);
    await accepted(w, START, { sessionId });
    const stored = await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: [{ ...point(NOW, 0), t: NOW - 10 * MINUTE }],
    });
    expect(stored).toEqual({ stored: 1, duplicates: 0, rejected: 0 });

    const endedAt = NOW + 5 * MINUTE;
    await accepted(w, END, { sessionId, failed: false, reason: "operator_stop", endedAt });
    const after = await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: [
        { ...point(NOW, 0), t: endedAt + SESSION_WINDOW_MARGIN_MS },
        { ...point(NOW, 0), t: endedAt + SESSION_WINDOW_MARGIN_MS + 1 },
      ],
    });
    expect(after).toEqual({ stored: 1, duplicates: 0, rejected: 1 });
  });
});

// ---------------------------------------------------------------------------
// Two clocks
// ---------------------------------------------------------------------------

describe("ANH-129 a machine whose clock is wrong loses no point, and nothing is shown at its date", () => {
  it("stores every point of a session whose machine was dated 1970, and shows the session at the server's date", async () => {
    const w = await world();
    // The session started five minutes ago; the machine's clock was never set.
    const sessionId = await localSession(w, {
      startedAt: NEVER_SET,
      sessionAgeMs: 5 * MINUTE,
    });

    const outcome = await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: points(NEVER_SET, 0, 300),
    });
    const told = await accepted<Outcome>(w, EVENTS, {
      sessionId,
      events: events(NEVER_SET, 0, 4),
    });

    expect(outcome).toEqual({ stored: 300, duplicates: 0, rejected: 0 });
    expect(told).toEqual({ stored: 4, duplicates: 0, rejected: 0 });
    // The table keeps the machine's own dates: they are what a point is known by.
    expect(await sessionOf(w, sessionId)).toMatchObject({
      startedAt: NOW - 5 * MINUTE,
      machineStartedAt: NEVER_SET,
    });
    expect((await telemetryOf(w, sessionId))[299].t).toBe(NEVER_SET + 299_000);
    // The dashboard is told the server's.
    const { session, curve } = await shown(w, sessionId);
    expect(session).toMatchObject({
      startedAt: NOW - 5 * MINUTE,
      lastSignalAt: NOW,
      lastMeasuredAt: NOW - 5 * MINUTE + 299_000,
      serverNow: NOW,
    });
    expect(curve[0].t).toBe(NOW - 5 * MINUTE);
    expect(curve[299].t).toBe(NOW - 5 * MINUTE + 299_000);
    expect(curve.every((served) => served.t > EARLIEST_MACHINE_DATE_MS)).toBe(
      true,
    );
  });

  it("dates the end of that session on the server's clock too", async () => {
    const w = await world();
    const sessionId = await localSession(w, {
      startedAt: NEVER_SET,
      sessionAgeMs: 5 * MINUTE,
    });

    await accepted(w, END, {
      sessionId,
      failed: false,
      reason: "operator_stop",
      endedAt: NEVER_SET + 4 * MINUTE,
    });

    expect(await sessionOf(w, sessionId)).toMatchObject({
      status: "completed",
      startedAt: NOW - 5 * MINUTE,
      endedAt: NOW - MINUTE,
    });
  });

  it("reads the measurements of a machine dated 1970 that sends live as measured now", async () => {
    const w = await world();
    const sessionId = await localSession(w, {
      startedAt: NEVER_SET,
      sessionAgeMs: 0,
    });

    // Five seconds later the machine sends the five points it has measured.
    vi.setSystemTime(NOW + 5000);
    await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: points(NEVER_SET, 1, 5),
    });

    const { session } = await shown(w, sessionId);
    expect(session).toMatchObject({
      lastSignalAt: NOW + 5000,
      lastMeasuredAt: NOW + 5000,
      serverNow: NOW + 5000,
    });
  });

  it("serves the points measured after `sinceT`, read on the server's clock", async () => {
    const w = await world();
    const sessionId = await localSession(w, {
      startedAt: NEVER_SET,
      sessionAgeMs: MINUTE,
    });
    await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: points(NEVER_SET, 0, 10),
    });

    const later = await w.admin.query(api.training.getSessionTelemetry, {
      sessionId,
      sinceT: NOW - MINUTE + 6000,
    });

    expect(later.map((served) => served.elapsedS)).toEqual([7, 8, 9]);
    expect(later[0].t).toBe(NOW - MINUTE + 7000);
  });

  it("dates at its registration a session whose machine wrote a date no session can have, when it says no age", async () => {
    const w = await world();

    const sessionId = await localSession(w, {
      startedAt: EARLIEST_MACHINE_DATE_MS - 1,
    });

    expect(await sessionOf(w, sessionId)).toMatchObject({
      startedAt: NOW,
      machineStartedAt: EARLIEST_MACHINE_DATE_MS - 1,
    });
  });

  it("keeps the date the machine wrote when it says no age and the date is believable", async () => {
    // A console older than this contract, or one that restarted since the
    // session: neither can say how long ago the session started.
    const w = await world();

    const sessionId = await localSession(w, { startedAt: NOW - HOUR });

    expect(await sessionOf(w, sessionId)).toMatchObject({
      startedAt: NOW - HOUR,
      machineStartedAt: NOW - HOUR,
    });
    await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: points(NOW - HOUR, 0, 3),
    });
    const { session, curve } = await shown(w, sessionId);
    expect(session?.lastMeasuredAt).toBe(NOW - HOUR + 2000);
    expect(curve.map((served) => served.t)).toEqual([
      NOW - HOUR,
      NOW - HOUR + 1000,
      NOW - HOUR + 2000,
    ]);
  });

  it.each([
    ["negative", -1],
    ["older than any session can be", NOW - EARLIEST_MACHINE_DATE_MS + 1],
  ])(
    "does not believe an age that is %s: the date the machine wrote stands",
    async (_name, sessionAgeMs) => {
      const w = await world();

      const sessionId = await localSession(w, {
        startedAt: NOW - HOUR,
        sessionAgeMs,
      });

      expect((await sessionOf(w, sessionId))?.startedAt).toBe(NOW - HOUR);
    },
  );

  it("dates a session once: registered again, it keeps its first date", async () => {
    const w = await world();
    const first = await localSession(w, {
      startedAt: NEVER_SET,
      sessionAgeMs: MINUTE,
    });

    vi.setSystemTime(NOW + HOUR);
    const again = await localSession(w, {
      startedAt: NEVER_SET,
      sessionAgeMs: 61 * MINUTE,
    });

    expect(again).toBe(first);
    expect((await sessionOf(w, first))?.startedAt).toBe(NOW - MINUTE);
  });
});

describe("ANH-129 the start of a launch from the dashboard, as its machine reports it", () => {
  it("dates the start as long ago as the machine says, and keeps the date the machine wrote", async () => {
    // The machine armed the launch, then lost its link for three minutes.
    const w = await world();
    const sessionId = await launched(w, w.machine, NOW - 4 * MINUTE);

    await accepted(w, START, {
      sessionId,
      startedAt: NEVER_SET,
      sessionAgeMs: 3 * MINUTE,
    });

    expect(await sessionOf(w, sessionId)).toMatchObject({
      status: "active",
      startedAt: NOW - 3 * MINUTE,
      machineStartedAt: NEVER_SET,
    });
    await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: points(NEVER_SET, 0, 180),
    });
    const { session } = await shown(w, sessionId);
    expect(session?.lastMeasuredAt).toBe(NOW - 1000);
  });

  it("never dates the start before the launch itself", async () => {
    const w = await world();
    const sessionId = await launched(w, w.machine, NOW - MINUTE);

    await accepted(w, START, {
      sessionId,
      startedAt: NOW - 5 * MINUTE,
      sessionAgeMs: 5 * MINUTE,
    });

    expect((await sessionOf(w, sessionId))?.startedAt).toBe(NOW);
  });

  it.each([
    ["text", "five minutes", "an hour ago"],
    ["null", null, null],
  ])(
    "reads neither a date nor an age that is %s: the start is the reception",
    async (_name, sessionAgeMs, startedAt) => {
      const w = await world();
      const sessionId = await launched(w, w.machine, NOW - MINUTE);

      await accepted(w, START, { sessionId, startedAt, sessionAgeMs });

      const session = await sessionOf(w, sessionId);
      expect(session).toMatchObject({ status: "active", startedAt: NOW });
      expect(session?.machineStartedAt).toBeUndefined();
    },
  );

  it("refuses a start whose body is not an object", async () => {
    const w = await world();

    const response = await send(w, START, ["session"]);

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({
      error: "invalid_request",
      message: "Missing sessionId",
    });
  });
});

describe("ANH-129 the end of a session, on the server's clock", () => {
  it("places the end the machine dated on the server's clock, and never before the start", async () => {
    const w = await world();
    const sessionId = await localSession(w, {
      startedAt: NEVER_SET,
      sessionAgeMs: 10 * MINUTE,
    });
    const before = await localSession(w, {
      startedAt: NEVER_SET,
      sessionAgeMs: 10 * MINUTE,
      localRef: "ends-before-it-starts",
    });

    await accepted(w, END, {
      sessionId,
      failed: true,
      reason: "interrupted",
      endedAt: NEVER_SET + 5 * MINUTE,
    });
    await accepted(w, END, {
      sessionId: before,
      failed: true,
      reason: "interrupted",
      endedAt: NEVER_SET - HOUR,
    });

    expect(await sessionOf(w, sessionId)).toMatchObject({
      status: "failed",
      endedAt: NOW - 5 * MINUTE,
      endReason: "interrupted",
    });
    expect((await sessionOf(w, before))?.endedAt).toBe(NOW - 10 * MINUTE);
  });

  it("dates at its reception an end the machine did not date", async () => {
    const w = await world();
    const sessionId = await localSession(w, {
      startedAt: NEVER_SET,
      sessionAgeMs: 10 * MINUTE,
    });

    vi.setSystemTime(NOW + 2000);
    await accepted(w, END, { sessionId, failed: false, reason: "operator_stop" });

    expect((await sessionOf(w, sessionId))?.endedAt).toBe(NOW + 2000);
  });
});

// ---------------------------------------------------------------------------
// Sent late is never live
// ---------------------------------------------------------------------------

describe("ANH-129 what is sent late keeps both of its dates", () => {
  /** The newest point of the session: how long ago it was received, and measured. */
  async function ages(w: MachineWorld, sessionId: Id<"sessions">) {
    const { session } = await shown(w, sessionId);
    if (!session || session.lastSignalAt === null) return null;
    return {
      receivedAgoMs: session.serverNow - session.lastSignalAt,
      measuredAgoMs:
        session.lastMeasuredAt === null
          ? null
          : session.serverNow - session.lastMeasuredAt,
    };
  }

  it("serves the record of a session sent after a restart as received now and measured when it was", async () => {
    // A ten-minute session, at the machine. The link is lost at 3 min, the
    // console is killed at 5 min; the link returns at 8 min, and the console
    // sends the two minutes it had recorded and not sent.
    const w = await world();
    const sessionId = await localSession(w, { startedAt: NOW, sessionAgeMs: 0 });
    vi.setSystemTime(NOW + 3 * MINUTE);
    await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: points(NOW, 0, 180),
    });

    vi.setSystemTime(NOW + 8 * MINUTE);
    const late = await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: points(NOW, 180, 120),
    });

    expect(late).toEqual({ stored: 120, duplicates: 0, rejected: 0 });
    const told = await ages(w, sessionId);
    expect(told).toEqual({
      receivedAgoMs: 0,
      measuredAgoMs: 3 * MINUTE + 1000,
    });
    // Received within the threshold, measured far outside it: not of now.
    expect(told?.measuredAgoMs).toBeGreaterThan(TELEMETRY_FRESH_MS);
    const rows = await telemetryOf(w, sessionId);
    expect(rows).toHaveLength(300);
    expect(Math.floor(rows[0]._creationTime)).toBe(NOW + 3 * MINUTE);
    expect(Math.floor(rows[299]._creationTime)).toBe(NOW + 8 * MINUTE);
  });

  it("does not make a session look heard again by sending a batch twice", async () => {
    const w = await world();
    const sessionId = await localSession(w, { startedAt: NOW, sessionAgeMs: 0 });
    vi.setSystemTime(NOW + 5000);
    const batch = { sessionId, points: points(NOW, 1, 5) };
    await accepted<Outcome>(w, TELEMETRY, batch);

    vi.setSystemTime(NOW + 45_000);
    await accepted<Outcome>(w, TELEMETRY, batch);

    expect(await ages(w, sessionId)).toEqual({
      receivedAgoMs: 40_000,
      measuredAgoMs: 40_000,
    });
  });

  it("reads the measurements of a machine whose clock is 10 min ahead as measured when they were, once it says how long ago it started", async () => {
    const w = await world();
    const ahead = NOW + 10 * MINUTE;
    const sessionId = await localSession(w, { startedAt: ahead, sessionAgeMs: 0 });

    vi.setSystemTime(NOW + 5000);
    await accepted<Outcome>(w, TELEMETRY, {
      sessionId,
      points: points(ahead, 1, 5),
    });

    expect(await ages(w, sessionId)).toEqual({
      receivedAgoMs: 0,
      measuredAgoMs: 0,
    });
  });
});

// ---------------------------------------------------------------------------
// The organisation of what a machine writes
// ---------------------------------------------------------------------------

describe("ANH-129 the events of a machine follow its organisation", () => {
  it("attaches to the machine's organisation the events stored before the organisations existed", async () => {
    const w = await seedLegacyWorld(modules);
    await w.t.mutation(internal.training.storeEvents, {
      machineId: w.machine,
      sessionId: w.session,
      events: events(NOW, 0, 2),
    });
    const before = await w.t.run((ctx) =>
      ctx.db.query("training_events").collect(),
    );
    expect(before.map((row) => row.organizationId)).toEqual([
      undefined,
      undefined,
    ]);

    const { organizationId } = await w.t.mutation(
      internal.migrations.multiOrganization.attachExistingRowsToAnheart,
      {},
    );

    const after = await w.t.run((ctx) =>
      ctx.db.query("training_events").collect(),
    );
    expect(after.map((row) => row.organizationId)).toEqual([
      organizationId,
      organizationId,
    ]);
  });
});

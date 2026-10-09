/// <reference types="vite/client" />
/**
 * ANH-129, the acceptance test, on the Convex side.
 *
 * `contracts/fixtures/journal-sync-trace.json` is every request the Raspberry
 * Pi console sent while its own acceptance test ran
 * (`raspberry-pi/tests/test_cloud_journal_e2e.py`): a session started at the
 * machine, the network lost at 3 min (the last batch received, its answer
 * lost), the console killed at 5 min, restarted, the network back at 8 min
 * together with the right time, on a machine that had been dating everything
 * from 1970. The Pi's test fails when that file is no longer what the console
 * sends.
 *
 * Here those requests are replayed, in order and at the same moments, against
 * the real functions. Convex must answer each as the trace recorded, and end
 * up holding every 1 Hz point and every event exactly once, and the end of
 * the session with its reason.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import traceText from "../contracts/fixtures/journal-sync-trace.json?raw";
import { api } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { TELEMETRY_FRESH_MS } from "../lib/training";
import { machineHeaders } from "./machineAuth.fixtures";
import { EARLIEST_SESSION_START_MS } from "./training";
import {
  configureAnheartOrganization,
  modules,
  NOW,
  seedMachineWorld,
} from "./test.setup";

type Exchange = {
  at: number;
  path: string;
  body: Record<string, unknown>;
  status: number;
  answer: Record<string, unknown>;
};

const trace = JSON.parse(traceText) as {
  scenario: { cut_s: number; kill_s: number; back_s: number };
  expected: {
    points: number;
    events: number;
    duplicates: number;
    end: { status: string; reason: string };
    machine_started_at: number;
    started_at: number;
    ended_at: number;
  };
  exchanges: Exchange[];
};

const LOCAL = "/api/machine/training/local";
const TELEMETRY = "/api/machine/training/telemetry";
const EVENTS = "/api/machine/training/events";
const END = "/api/machine/training/end";

/** The trace's first request, replayed at `NOW`: every other keeps its distance to it. */
const firstAt = trace.exchanges[0].at;
const onServerClock = (at: number) => NOW + (at - firstAt);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  configureAnheartOrganization(null);
});

/** Replay the whole trace on a fresh machine. What Convex answered, and what it holds. */
async function replayed() {
  const w = await seedMachineWorld(modules);
  let sessionId: Id<"sessions"> | null = null;
  const answers: Array<{
    exchange: Exchange;
    status: number;
    answer: Record<string, unknown>;
  }> = [];
  for (const exchange of trace.exchanges) {
    vi.setSystemTime(onServerClock(exchange.at));
    // The trace names the session by the word its recorder gave it.
    const body = JSON.stringify(exchange.body).replaceAll(
      '"SESSION-1"',
      JSON.stringify(sessionId ?? "SESSION-1"),
    );
    const response = await w.t.fetch(exchange.path, {
      method: "POST",
      headers: {
        ...machineHeaders(w.machineKey),
        "Content-Type": "application/json",
      },
      body,
    });
    const answer = (await response.json()) as Record<string, unknown>;
    if (exchange.path === LOCAL) {
      sessionId = answer.sessionId as Id<"sessions">;
    }
    answers.push({ exchange, status: response.status, answer });
  }
  if (sessionId === null) throw new TypeError("The trace declares no session");
  const declared = sessionId;
  const stored = await w.t.run(async (ctx) => ({
    session: await ctx.db.get(declared),
    sessions: await ctx.db.query("sessions").collect(),
    telemetry: await ctx.db
      .query("training_telemetry")
      .withIndex("by_session_and_t", (q) => q.eq("sessionId", declared))
      .collect(),
    events: await ctx.db
      .query("training_events")
      .withIndex("by_session_and_seq", (q) => q.eq("sessionId", declared))
      .collect(),
  }));
  return { w, sessionId: declared, answers, ...stored };
}

describe("ANH-129 the trace is the acceptance scenario", () => {
  it("is a session cut at 3 min, killed at 5 min, sent again from 8 min", () => {
    expect(trace.scenario).toEqual({ cut_s: 180, kill_s: 300, back_s: 480 });
    const paths = trace.exchanges.map((exchange) => exchange.path);
    expect(paths[0]).toBe(LOCAL);
    expect(paths.filter((path) => path === LOCAL)).toHaveLength(1);
    expect(paths.filter((path) => path === END)).toHaveLength(1);
    // Nothing reaches the server between the cut and the return of the network.
    const seconds = trace.exchanges.map((exchange) =>
      Math.round((exchange.at - trace.expected.started_at) / 1000),
    );
    const silence = seconds.filter(
      (second) => second > trace.scenario.cut_s + 5 && second < trace.scenario.back_s,
    );
    expect(silence).toEqual([]);
    expect(seconds.some((second) => second >= trace.scenario.back_s)).toBe(true);
    // The machine dated everything from 1970, and said the age of its session.
    expect(trace.expected.machine_started_at).toBeLessThan(
      EARLIEST_SESSION_START_MS,
    );
    expect(trace.exchanges[0].body.sessionAgeMs).toEqual(expect.any(Number));
    // Five minutes of session, and a batch sent twice.
    expect(trace.expected.points).toBeGreaterThanOrEqual(trace.scenario.kill_s);
    expect(trace.expected.duplicates).toBeGreaterThan(0);
  });
});

describe("ANH-129 acceptance: Convex, replaying what the console sent", () => {
  it("answers every request as the console's test expected", async () => {
    const { answers } = await replayed();

    for (const { exchange, status, answer } of answers) {
      expect(status, exchange.path).toBe(exchange.status);
      if (exchange.path === LOCAL) {
        expect(answer.sessionId).toEqual(expect.any(String));
      } else {
        // {stored, duplicates, rejected} for a batch, {success: true} for the end.
        expect(answer, `${exchange.path} at ${exchange.at}`).toEqual(
          exchange.answer,
        );
      }
    }
    const duplicates = answers
      .filter(({ exchange }) => exchange.path === TELEMETRY)
      .reduce((sum, { answer }) => sum + (answer.duplicates as number), 0);
    expect(duplicates).toBe(trace.expected.duplicates);
    const rejected = answers
      .filter(({ exchange }) => [TELEMETRY, EVENTS].includes(exchange.path))
      .reduce((sum, { answer }) => sum + (answer.rejected as number), 0);
    expect(rejected).toBe(0);
  });

  it("holds every 1 Hz point once", async () => {
    const { telemetry } = await replayed();

    expect(telemetry).toHaveLength(trace.expected.points);
    expect(telemetry.map((row) => Math.floor(row.elapsedS))).toEqual(
      Array.from({ length: trace.expected.points }, (_, second) => second),
    );
    expect(new Set(telemetry.map((row) => row.t)).size).toBe(telemetry.length);
  });

  it("holds every event once, in the order of the record", async () => {
    const { events } = await replayed();

    expect(events.map((row) => row.seq)).toEqual(
      Array.from({ length: trace.expected.events }, (_, seq) => seq),
    );
    const sent = trace.exchanges
      .filter((exchange) => exchange.path === EVENTS)
      .flatMap((exchange) => exchange.body.events as Array<{ seq: number; kind: string }>);
    expect(events.map((row) => row.kind)).toEqual(
      events.map((row) => sent.find((event) => event.seq === row.seq)?.kind),
    );
  });

  it("ends the session with the reason of the record, and declares it once", async () => {
    const { session, sessions } = await replayed();

    expect(sessions).toHaveLength(1);
    expect(session).toMatchObject({
      status: trace.expected.end.status,
      endReason: trace.expected.end.reason,
      origin: "local",
    });
    expect(session?.endReason).toBe("interrupted");
  });

  it("dates the session on its own clock, and serves nothing in 1970", async () => {
    const { w, sessionId, session } = await replayed();

    expect(session).toMatchObject({
      machineStartedAt: trace.expected.machine_started_at,
      startedAt: onServerClock(trace.expected.started_at),
      endedAt: onServerClock(trace.expected.ended_at),
    });
    const curve = await w.admin.query(api.training.getSessionTelemetry, {
      sessionId,
    });
    expect(curve).toHaveLength(trace.expected.points);
    expect(curve[0].t).toBe(onServerClock(trace.expected.started_at));
    expect(
      curve.every(
        (point) =>
          point.t >= onServerClock(trace.expected.started_at) &&
          point.t <= onServerClock(trace.expected.ended_at),
      ),
    ).toBe(true);
  });

  it("keeps both dates of what arrived after the restart: received then, measured before", async () => {
    const { session, telemetry } = await replayed();
    const back = onServerClock(
      trace.expected.started_at + trace.scenario.back_s * 1000,
    );
    const shift = (session?.startedAt ?? 0) - (session?.machineStartedAt ?? 0);
    const late = telemetry.filter((row) => row._creationTime >= back - 1000);

    expect(late.length).toBeGreaterThanOrEqual(
      trace.scenario.kill_s - trace.scenario.cut_s - 10,
    );
    for (const row of late) {
      // Measured before the console was killed, received three minutes later:
      // far outside what the dashboard shows as current.
      expect(row._creationTime - (row.t + shift)).toBeGreaterThan(
        TELEMETRY_FRESH_MS,
      );
    }
  });
});

import { httpRouter } from "convex/server";
import { httpAction } from "./_generated/server";
import { internal } from "./_generated/api";
import {
  authenticateMachineRequest,
  validateMachineAuth,
} from "./lib/machineHttpAuth";
import type { Id } from "./_generated/dataModel";
import type { Infer } from "convex/values";
import { liveStateValidator } from "./schema";
import { machineHealthSchema } from "./lib/machineHealth";
import * as records from "./recordHttp";
import { receive as receiveClerkWebhook } from "./clerkWebhook";
import {
  CONTRACT_VERSION,
  machineErrorResponse,
  refusalOf,
  softwareVersionOf,
  type MachineErrorCode,
} from "./lib/contract";
import {
  boundedText,
  limitMachine,
  MAX_EVENTS_BYTES,
  MAX_HEARTBEAT_BYTES,
  MAX_TELEMETRY_BYTES,
  payloadTooLarge,
  type MachineLimit,
} from "./lib/rateLimits";
import { isMedicalVersion } from "./lib/medicalParameters";

const http = httpRouter();
http.route({
  path: "/clerk/webhook",
  method: "POST",
  handler: receiveClerkWebhook,
});
http.route({
  path: "/api/machine/records/upload-url",
  method: "POST",
  handler: records.uploadUrl,
});
http.route({
  path: "/api/machine/records/commit",
  method: "POST",
  handler: records.commit,
});
http.route({
  path: "/api/machine/records/status",
  method: "GET",
  handler: records.status,
});
http.route({
  path: "/api/records/download",
  method: "GET",
  handler: records.download,
});
http.route({
  path: "/api/records/download",
  method: "OPTIONS",
  handler: records.options,
});

/**
 * POST /api/machine/heartbeat
 * The local panel sends this with what the machine is doing, every 10 seconds,
 * with its software version and the contract it speaks (stored on the machine
 * for the dashboard to show). One every 5 s per machine at most, 16 KiB of
 * body at most (`contracts/machine-api.json`).
 */
http.route({
  path: "/api/machine/heartbeat",
  method: "POST",
  handler: httpAction(async (ctx, req) => {
    // Validate API key
    const authResult = await validateMachineAuth(ctx, req);
    if ("error" in authResult) {
      return authResult.error;
    }
    const { machine } = authResult;
    const limited = await limitMachine(ctx, "machineHeartbeat", machine._id);
    if (limited) return limited;

    // Parse optional body
    let body: {
      batteryLevel?: number;
      wifiStrength?: number;
      activeSessionId?: string;
      live?: Infer<typeof liveStateValidator>;
      programsEnabled?: boolean;
      software_version?: unknown;
      medical_parameters_version?: unknown;
    } = {};

    // An empty body is a heartbeat without a field. Anything else is refused
    // before anything is recorded, unless it is a JSON object whose stored
    // fields have their type: text that is not JSON, JSON that is not an
    // object, a battery level that is not a number.
    const text = await boundedText(req, MAX_HEARTBEAT_BYTES);
    if (text === null) {
      return payloadTooLarge("A heartbeat is 16384 bytes at most");
    }
    const sent = optionalJsonOf(text);
    if (sent === null || !validHeartbeatFields(sent)) {
      return refuse(
        400,
        "invalid_request",
        "Expected an empty body or {batteryLevel?: number, wifiStrength?: number, activeSessionId?: string, ...}",
      );
    }
    body = sent as typeof body;

    const rawHealth = sent.health;
    const parsedHealth =
      rawHealth === undefined
        ? undefined
        : machineHealthSchema.safeParse(rawHealth);
    if (parsedHealth !== undefined && !parsedHealth.success) {
      return refuse(
        400,
        "invalid_request",
        "Invalid machine health measurements",
      );
    }

    // Record the heartbeat
    await ctx.runMutation(internal.machines.recordHeartbeat, {
      machineId: machine._id as Parameters<
        typeof ctx.runMutation<typeof internal.machines.recordHeartbeat>
      >[1]["machineId"],
      batteryLevel: body.batteryLevel,
      wifiStrength: body.wifiStrength,
      activeSessionId: body.activeSessionId,
      softwareVersion: softwareVersionOf(body.software_version),
      medicalParametersVersion: medicalParametersVersionOf(
        body.medical_parameters_version,
      ),
      // The header's value, which the gate just checked, not the body's copy.
      contractVersion: authResult.contract,
      health: parsedHealth?.data,
    });

    // The local panel reports what the machine is doing with each heartbeat.
    if (body.live && validLive(body.live)) {
      await ctx.runMutation(internal.training.updateLive, {
        machineId: machine._id as Id<"machines">,
        live: { ...body.live, updatedAt: Date.now() },
        programsEnabled:
          typeof body.programsEnabled === "boolean"
            ? body.programsEnabled
            : undefined,
      });
    }

    return new Response(
      JSON.stringify({
        success: true,
        serverTime: Date.now(),
        server_contract_version: CONTRACT_VERSION,
      }),
      {
        status: 200,
        headers: { "Content-Type": "application/json" },
      },
    );
  }),
});

// ===========================================================================
// Training: the local panel's side of the sync.
//
// The Pi is the authority on the machine. These routes let it (a) publish its
// presets and live state, (b) pick up AUTO sessions launched from the
// dashboard, (c) register sessions started at the machine (manual, or auto
// from the panel), (d) stream telemetry, and (e) learn that a stop was asked
// for. Manual sessions never travel from here to the Pi.
// ===========================================================================

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** A refusal: `{error: <stable code>, message: <words>}` (contracts/machine-api.json). */
function refuse(
  status: number,
  code: MachineErrorCode,
  message: string,
): Response {
  return machineErrorResponse(status, code, message);
}

/** True for what JSON calls an object: not null, not an array, not a scalar. */
function isJsonObject(parsed: unknown): parsed is Record<string, unknown> {
  return (
    typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
  );
}

async function readJson(req: Request): Promise<Record<string, unknown> | null> {
  try {
    const parsed: unknown = await req.json();
    return isJsonObject(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/** The object a body of JSON carries, or null (text that is not JSON, JSON that is not an object). */
function jsonObjectOf(text: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(text);
    return isJsonObject(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * The body of a route that may be sent without one: `{}` for an empty body,
 * the object it carries, or null for anything else (text that is not JSON,
 * JSON that is not an object).
 */
function optionalJsonOf(text: string): Record<string, unknown> | null {
  return text === "" ? {} : jsonObjectOf(text);
}

/**
 * A batch route's body: its object, or the 413 of a body over `max` bytes,
 * answered before the JSON is read. A body that is not an object is null.
 */
async function readBoundedJson(
  req: Request,
  max: number,
): Promise<Record<string, unknown> | null | Response> {
  const text = await boundedText(req, max);
  if (text === null) {
    return payloadTooLarge(`This route takes ${max} bytes of body at most`);
  }
  return jsonObjectOf(text);
}

function isNum(x: unknown): x is number {
  return typeof x === "number" && Number.isFinite(x);
}
function isStr(x: unknown): x is string {
  return typeof x === "string";
}

function medicalParametersVersionOf(
  value: unknown,
): string | null | undefined {
  if (value === null) return null;
  if (!isMedicalVersion(value)) return undefined;
  return value;
}

/** The fields of a heartbeat that are stored as sent: each absent, or of its type. */
function validHeartbeatFields(sent: Record<string, unknown>): boolean {
  return (
    (sent.batteryLevel === undefined || isNum(sent.batteryLevel)) &&
    (sent.wifiStrength === undefined || isNum(sent.wifiStrength)) &&
    (sent.activeSessionId === undefined || isStr(sent.activeSessionId))
  );
}

function validLive(live: unknown): live is Infer<typeof liveStateValidator> {
  if (typeof live !== "object" || live === null) return false;
  const l = live as Record<string, unknown>;
  return (
    isStr(l.runMode) &&
    isStr(l.phase) &&
    (l.bpm === undefined || isNum(l.bpm)) &&
    isNum(l.motorRpm) &&
    isNum(l.outputRpm) &&
    isNum(l.setpointMotorRpm) &&
    isNum(l.gLoad) &&
    isStr(l.safetyAction) &&
    (l.driveState === undefined || isStr(l.driveState)) &&
    (l.sessionId === undefined || isStr(l.sessionId))
  );
}

type Machine = { _id: string };

type MachineHandler = (
  ctx: Parameters<Parameters<typeof httpAction>[0]>[0],
  req: Request,
  machineId: Id<"machines">,
) => Promise<Response>;

/**
 * Pass the gate (by default the key, then the contract), then the machine's
 * rate limit of the route when it has one, then run `handle`; any thrown
 * error becomes a 400 carrying its stable code (`request_failed` when it has
 * none). Only the stop request's route passes another gate, and it has no
 * limit: a stop is never held back.
 */
function machineRoute(
  handle: MachineHandler,
  gate: typeof authenticateMachineRequest = validateMachineAuth,
  limit?: MachineLimit,
) {
  return httpAction(async (ctx, req) => {
    const auth = await gate(ctx, req);
    if ("error" in auth) return auth.error;
    const machine: Machine = auth.machine;
    if (limit !== undefined) {
      const limited = await limitMachine(ctx, limit, machine._id);
      if (limited) return limited;
    }
    try {
      return await handle(ctx, req, machine._id as Id<"machines">);
    } catch (error: unknown) {
      const { code, message } = refusalOf(error);
      return refuse(400, code, message);
    }
  });
}

/** A machine route under one of the machine limits (ANH-166), before its body is read. */
function limitedRoute(limit: MachineLimit, handle: MachineHandler) {
  return machineRoute(handle, validateMachineAuth, limit);
}

function sessionIdOf(
  ctx: Parameters<Parameters<typeof httpAction>[0]>[0],
  raw: unknown,
): Id<"sessions"> | null {
  return isStr(raw) && raw.length > 0 ? (raw as Id<"sessions">) : null;
}

/**
 * GET /api/machine/training/poll
 * -> {session: PendingTraining | null, server_contract_version}
 * The console arms nothing from an answer whose major is not its own.
 */
http.route({
  path: "/api/machine/training/poll",
  method: "GET",
  handler: limitedRoute("machinePoll", async (ctx, _req, machineId) => {
    const session = await ctx.runQuery(
      internal.training.getPendingTrainingSession,
      { machineId },
    );
    return json(200, { session, server_contract_version: CONTRACT_VERSION });
  }),
});

/**
 * GET /api/machine/roster -> {riders: [{userId, name, hrMax, birthYear,
 * fit, fitnessValidUntil, consent, fitnessQuestionnaireVersion}]}
 * The riders the local console offers (`training.getRoster`), asked at its
 * start and once a minute at rest.
 * Contract 1.6: fitness status and its validated questionnaire version only,
 * never answers or review notes (`contracts/machine-api.json`, `roster`).
 * Never rate limited: read only; an invalid key, which cannot be limited,
 * already costs as much.
 */
http.route({
  path: "/api/machine/roster",
  method: "GET",
  handler: machineRoute(async (ctx, _req, machineId) => {
    const riders = await ctx.runQuery(internal.training.getRoster, {
      machineId,
    });
    return json(200, { riders });
  }),
});

/**
 * POST /api/machine/profiles {storeRev, programsEnabled, profiles[]}
 * One every 10 s per machine, five in reserve (ANH-228).
 */
http.route({
  path: "/api/machine/profiles",
  method: "POST",
  handler: limitedRoute("machineProfiles", async (ctx, req, machineId) => {
    const body = await readJson(req);
    if (!body || !isNum(body.storeRev) || !Array.isArray(body.profiles)) {
      return refuse(
        400,
        "invalid_request",
        "Expected {storeRev, programsEnabled, profiles}",
      );
    }
    const keys = [
      "totalDurationS",
      "zoneLowBpm",
      "zoneHighBpm",
      "hardMaxBpm",
      "criticalBpm",
      "subjectHrMax",
      "minRunRpm",
      "maxRpm",
    ] as const;
    const profiles = [];
    for (const raw of body.profiles as unknown[]) {
      if (typeof raw !== "object" || raw === null) {
        return refuse(400, "invalid_request", "Profile must be an object");
      }
      const p = raw as Record<string, unknown>;
      if (
        !isStr(p.profileId) ||
        !isStr(p.name) ||
        !keys.every((k) => isNum(p[k]))
      ) {
        return refuse(
          400,
          "invalid_request",
          `Malformed profile ${String(p.profileId)}`,
        );
      }
      profiles.push({
        profileId: p.profileId,
        name: p.name,
        totalDurationS: p.totalDurationS as number,
        zoneLowBpm: p.zoneLowBpm as number,
        zoneHighBpm: p.zoneHighBpm as number,
        hardMaxBpm: p.hardMaxBpm as number,
        criticalBpm: p.criticalBpm as number,
        subjectHrMax: p.subjectHrMax as number,
        minRunRpm: p.minRunRpm as number,
        maxRpm: p.maxRpm as number,
      });
    }
    const result = await ctx.runMutation(internal.training.syncProfiles, {
      machineId,
      storeRev: body.storeRev,
      programsEnabled: body.programsEnabled === true,
      profiles,
    });
    return json(200, result);
  }),
});

/**
 * POST /api/machine/training/start {sessionId, startedAt?, sessionAgeMs?}:
 * the Pi armed a remote launch. `startedAt` is the start as the machine dated
 * it (unix ms, its clock), `sessionAgeMs` how long ago that was on its
 * monotonic clock. They are read together or not at all, and a value that is
 * not a number is not read: without both, the start is dated at its
 * reception, as for a machine that sends neither.
 *
 * 409 `{error: <arming refusal>, message}` when the launch is checked again
 * and refused (ANH-144, contract 1.5, `arming_refusals`): the session is
 * failed with that code and the machine must not run it. Never rate limited:
 * only writes for a pending launch, already bounded by launchAutoSession;
 * a 429 would stop an armed console from before 1.5.
 */
http.route({
  path: "/api/machine/training/start",
  method: "POST",
  handler: machineRoute(async (ctx, req, machineId) => {
    const body = await readJson(req);
    const sessionId = sessionIdOf(ctx, body?.sessionId);
    if (!body || !sessionId)
      return refuse(400, "invalid_request", "Missing sessionId");
    const started = await ctx.runMutation(
      internal.training.markTrainingStarted,
      {
        machineId,
        sessionId,
        machineStartedAt: isNum(body.startedAt) ? body.startedAt : undefined,
        sessionAgeMs: isNum(body.sessionAgeMs) ? body.sessionAgeMs : undefined,
      },
    );
    return started.armed
      ? json(200, { success: true })
      : refuse(409, started.code, started.message);
  }),
});

/**
 * POST /api/machine/training/local {...}: register a session started at the
 * machine. `startedAt` is the start as the machine dated it; `sessionAgeMs`,
 * optional, how long ago that was on its monotonic clock. Without it the
 * session is dated as the machine wrote it.
 * One per minute per machine, twenty in reserve (ANH-228).
 */
http.route({
  path: "/api/machine/training/local",
  method: "POST",
  handler: limitedRoute("machineLocalSession", async (ctx, req, machineId) => {
    const b = await readJson(req);
    if (
      !b ||
      !isStr(b.localRef) ||
      (b.kind !== "auto" && b.kind !== "manual") ||
      !isNum(b.startedAt) ||
      !isStr(b.operatorName)
    ) {
      return refuse(
        400,
        "invalid_request",
        "Expected {localRef, kind, startedAt, operatorName}",
      );
    }
    const optStr = (x: unknown) => (isStr(x) ? x : undefined);
    const optNum = (x: unknown) => (isNum(x) ? x : undefined);
    const sessionId = await ctx.runMutation(
      internal.training.registerLocalSession,
      {
        machineId,
        localRef: b.localRef,
        kind: b.kind,
        startedAt: b.startedAt,
        sessionAgeMs: optNum(b.sessionAgeMs),
        operatorName: b.operatorName,
        userId: optStr(b.userId),
        subjectLabel: optStr(b.subjectLabel),
        profileId: optStr(b.profileId),
        profileName: optStr(b.profileName),
        zoneLowBpm: optNum(b.zoneLowBpm),
        zoneHighBpm: optNum(b.zoneHighBpm),
        totalDurationS: optNum(b.totalDurationS),
        subjectHrMax: optNum(b.subjectHrMax),
        occupancy: optStr(b.occupancy),
      },
    );
    return json(200, { sessionId });
  }),
});

/**
 * POST /api/machine/training/end {sessionId, failed, reason, endedAt?}
 * Never rate limited: the end of a session must always reach the server.
 */
http.route({
  path: "/api/machine/training/end",
  method: "POST",
  handler: machineRoute(async (ctx, req, machineId) => {
    const b = await readJson(req);
    const sessionId = sessionIdOf(ctx, b?.sessionId);
    if (!b || !sessionId || !isStr(b.reason)) {
      return refuse(
        400,
        "invalid_request",
        "Expected {sessionId, failed, reason}",
      );
    }
    await ctx.runMutation(internal.training.endTrainingSession, {
      machineId,
      sessionId,
      failed: b.failed === true,
      reason: b.reason,
      endedAt: isNum(b.endedAt) ? b.endedAt : undefined,
    });
    return json(200, { success: true });
  }),
});

/**
 * GET /api/machine/training/status?sessionId=
 * -> {status, active, stopRequested, server_contract_version}
 *
 * The one machine route that answers whatever contract the machine announces
 * (the key is still required): it carries the stop request, and "stop" means
 * the same thing under every contract. A console of another major ends its
 * session on `stopRequested: true` or `active: false` (either can only cause
 * an ordinary stop) and trusts nothing else in this answer. The route only
 * reads. Never rate limited: a stop must always reach the machine.
 */
http.route({
  path: "/api/machine/training/status",
  method: "GET",
  handler: machineRoute(async (ctx, req, machineId) => {
    const sessionId = sessionIdOf(
      ctx,
      new URL(req.url).searchParams.get("sessionId"),
    );
    if (!sessionId) return refuse(400, "invalid_request", "Missing sessionId");
    const status = await ctx.runQuery(internal.training.getTrainingStatus, {
      machineId,
      sessionId,
    });
    return status
      ? json(200, { ...status, server_contract_version: CONTRACT_VERSION })
      : refuse(404, "session_not_found", "Session not found");
  }, authenticateMachineRequest),
});

/** Points accepted in one request. */
const MAX_POINTS_PER_BATCH = 600;

/**
 * POST /api/machine/training/telemetry {sessionId, points[]}
 * -> {stored, duplicates, rejected}
 *
 * A point is one `(sessionId, t)`: sent again, it is counted in `duplicates`
 * and stored once. A 200 acknowledges every point of the batch, stored or
 * not: the machine has nothing to send again. Two batches every 5 s per
 * machine at most; 300 KiB of body and 600 points at most, both refused (413)
 * before a point is read.
 */
http.route({
  path: "/api/machine/training/telemetry",
  method: "POST",
  handler: limitedRoute("machineTelemetry", async (ctx, req, machineId) => {
    const b = await readBoundedJson(req, MAX_TELEMETRY_BYTES);
    if (b instanceof Response) return b;
    const sessionId = sessionIdOf(ctx, b?.sessionId);
    if (!b || !sessionId || !Array.isArray(b.points)) {
      return refuse(400, "invalid_request", "Expected {sessionId, points}");
    }
    if (b.points.length > MAX_POINTS_PER_BATCH) {
      return payloadTooLarge("At most 600 points per batch");
    }
    const points = [];
    for (const raw of b.points as unknown[]) {
      const p = (typeof raw === "object" && raw !== null ? raw : {}) as Record<
        string,
        unknown
      >;
      if (
        !isNum(p.t) ||
        !isNum(p.elapsedS) ||
        !isStr(p.phase) ||
        (p.bpm !== undefined && !isNum(p.bpm)) ||
        !isNum(p.motorRpm) ||
        !isNum(p.outputRpm) ||
        !isNum(p.setpointMotorRpm) ||
        !isNum(p.gLoad) ||
        !isStr(p.safetyAction)
      ) {
        return refuse(400, "invalid_request", "Malformed telemetry point");
      }
      points.push({
        t: p.t,
        elapsedS: p.elapsedS,
        phase: p.phase,
        bpm: p.bpm as number | undefined,
        motorRpm: p.motorRpm,
        outputRpm: p.outputRpm,
        setpointMotorRpm: p.setpointMotorRpm,
        gLoad: p.gLoad,
        safetyAction: p.safetyAction,
      });
    }
    const result = await ctx.runMutation(internal.training.storeTelemetry, {
      machineId,
      sessionId,
      points,
    });
    return json(200, result);
  }),
});

/** Events accepted in one request. */
const MAX_EVENTS_PER_BATCH = 200;
/**
 * The highest rank of an event: an event is one `(sessionId, seq)`, so this
 * is also the most events one session can hold.
 */
const MAX_EVENT_SEQ = 999_999;
/** Characters of free text accepted in one event. */
const MAX_EVENT_DETAIL = 2000;
/** A kind of the record's vocabulary: lower case, digits and underscores. */
const EVENT_KIND = /^[a-z][a-z0-9_]{0,63}$/;
/** `system`, `remote` or an opaque operator identifier: never a name. */
const EVENT_ACTOR = /^[A-Za-z0-9_-]{1,64}$/;

/** One well-formed event of a request, or null. */
function eventOf(raw: unknown) {
  const e = (typeof raw === "object" && raw !== null ? raw : {}) as Record<
    string,
    unknown
  >;
  const { seq, t, kind, detail, actor } = e;
  if (
    !isNum(seq) ||
    !Number.isInteger(seq) ||
    seq < 0 ||
    seq > MAX_EVENT_SEQ ||
    !isNum(t) ||
    !Number.isSafeInteger(t) ||
    !isStr(kind) ||
    !EVENT_KIND.test(kind) ||
    !isStr(detail) ||
    detail.length > MAX_EVENT_DETAIL ||
    !isStr(actor) ||
    !EVENT_ACTOR.test(actor)
  ) {
    return null;
  }
  return { seq, t, kind, detail, actor };
}

/**
 * POST /api/machine/training/events {sessionId, events[]}
 * -> {stored, duplicates, rejected}
 *
 * The events of the session's local record, each `{seq, t, kind, detail,
 * actor}`: `seq` is its rank in the record (999 999 at most), `t` a whole
 * number of ms, and an event is one `(sessionId, seq)`, stored once however
 * many times it is sent. Sizes are bounded here: 2.5 MiB of body and 200
 * events per request, both refused (413) before an event is read; 2000
 * characters of text per event. Ten requests a second per machine at most.
 */
http.route({
  path: "/api/machine/training/events",
  method: "POST",
  handler: limitedRoute("machineEvents", async (ctx, req, machineId) => {
    const b = await readBoundedJson(req, MAX_EVENTS_BYTES);
    if (b instanceof Response) return b;
    const sessionId = sessionIdOf(ctx, b?.sessionId);
    if (!b || !sessionId || !Array.isArray(b.events)) {
      return refuse(400, "invalid_request", "Expected {sessionId, events}");
    }
    if (b.events.length > MAX_EVENTS_PER_BATCH) {
      return payloadTooLarge("At most 200 events per batch");
    }
    const events = [];
    for (const raw of b.events as unknown[]) {
      const event = eventOf(raw);
      if (event === null) {
        return refuse(400, "invalid_request", "Malformed event");
      }
      events.push(event);
    }
    const result = await ctx.runMutation(internal.training.storeEvents, {
      machineId,
      sessionId,
      events,
    });
    return json(200, result);
  }),
});

export default http;

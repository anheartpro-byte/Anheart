import { httpRouter } from "convex/server";
import { httpAction } from "./_generated/server";
import { internal } from "./_generated/api";
import { hashApiKey } from "./lib/crypto";
import type { Id } from "./_generated/dataModel";
import type { Infer } from "convex/values";
import { liveStateValidator } from "./schema";

const http = httpRouter();

/**
 * Helper to validate machine API key from request
 */
async function validateMachineAuth(
  ctx: Parameters<Parameters<typeof httpAction>[0]>[0],
  req: Request,
): Promise<
  | {
      machine: {
        _id: string;
        name: string;
        status: string;
        config: {
          sampleRate: number;
          channels: string[];
          batchInterval: number;
        };
      };
    }
  | { error: Response }
> {
  const authHeader = req.headers.get("Authorization");

  if (!authHeader || !authHeader.startsWith("Bearer ")) {
    return {
      error: new Response(
        JSON.stringify({ error: "Missing Authorization header" }),
        {
          status: 401,
          headers: { "Content-Type": "application/json" },
        },
      ),
    };
  }

  const apiKey = authHeader.substring(7); // Remove "Bearer "
  const apiKeyHash = hashApiKey(apiKey);

  const machine = await ctx.runQuery(internal.machines.getMachineByApiKey, {
    apiKeyHash,
  });

  if (!machine) {
    return {
      error: new Response(JSON.stringify({ error: "Invalid API key" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    };
  }

  return {
    machine: machine as {
      _id: string;
      name: string;
      status: string;
      config: { sampleRate: number; channels: string[]; batchInterval: number };
    },
  };
}

/**
 * POST /api/machine/heartbeat
 * Raspberry Pi sends this every 30 seconds
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

    // Parse optional body
    let body: {
      batteryLevel?: number;
      wifiStrength?: number;
      activeSessionId?: string;
      live?: Infer<typeof liveStateValidator>;
      programsEnabled?: boolean;
    } = {};

    try {
      const text = await req.text();
      if (text) {
        body = JSON.parse(text);
      }
    } catch {
      // Empty or invalid body is OK - use defaults
    }

    // Record the heartbeat
    await ctx.runMutation(internal.machines.recordHeartbeat, {
      machineId: machine._id as Parameters<
        typeof ctx.runMutation<typeof internal.machines.recordHeartbeat>
      >[1]["machineId"],
      batteryLevel: body.batteryLevel,
      wifiStrength: body.wifiStrength,
      activeSessionId: body.activeSessionId,
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
      }),
      {
        status: 200,
        headers: { "Content-Type": "application/json" },
      },
    );
  }),
});

/**
 * GET /api/machine/session/poll
 * RPi polls for pending sessions
 */
http.route({
  path: "/api/machine/session/poll",
  method: "GET",
  handler: httpAction(async (ctx, req) => {
    // Validate API key
    const authResult = await validateMachineAuth(ctx, req);
    if ("error" in authResult) {
      return authResult.error;
    }
    const { machine } = authResult;

    // Check for pending session
    const pendingSession = await ctx.runQuery(
      internal.sessions.getPendingSessionForMachine,
      {
        machineId: machine._id as Parameters<
          typeof ctx.runQuery<
            typeof internal.sessions.getPendingSessionForMachine
          >
        >[1]["machineId"],
      },
    );

    if (!pendingSession) {
      return new Response(JSON.stringify({ session: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response(
      JSON.stringify({
        session: {
          id: pendingSession._id,
          channels: pendingSession.channels,
          config: pendingSession.config,
        },
      }),
      {
        status: 200,
        headers: { "Content-Type": "application/json" },
      },
    );
  }),
});

/**
 * POST /api/machine/session/start
 * RPi notifies it has started a session
 */
http.route({
  path: "/api/machine/session/start",
  method: "POST",
  handler: httpAction(async (ctx, req) => {
    // Validate API key
    const authResult = await validateMachineAuth(ctx, req);
    if ("error" in authResult) {
      return authResult.error;
    }

    // Parse body
    let body: { sessionId?: string };
    try {
      body = await req.json();
    } catch {
      return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (!body.sessionId) {
      return new Response(JSON.stringify({ error: "Missing sessionId" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    try {
      await ctx.runMutation(internal.sessions.startSession, {
        sessionId: body.sessionId as Parameters<
          typeof ctx.runMutation<typeof internal.sessions.startSession>
        >[1]["sessionId"],
      });

      return new Response(JSON.stringify({ success: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : "Unknown error";
      return new Response(JSON.stringify({ error: message }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }
  }),
});

/**
 * POST /api/machine/session/end
 * RPi notifies session has ended (or failed)
 */
http.route({
  path: "/api/machine/session/end",
  method: "POST",
  handler: httpAction(async (ctx, req) => {
    // Validate API key
    const authResult = await validateMachineAuth(ctx, req);
    if ("error" in authResult) {
      return authResult.error;
    }

    // Parse body
    let body: { sessionId?: string; reason?: string; failed?: boolean };
    try {
      body = await req.json();
    } catch {
      return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (!body.sessionId) {
      return new Response(JSON.stringify({ error: "Missing sessionId" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    try {
      if (body.failed) {
        // Session failed
        await ctx.runMutation(internal.sessions.failSession, {
          sessionId: body.sessionId as Parameters<
            typeof ctx.runMutation<typeof internal.sessions.failSession>
          >[1]["sessionId"],
          reason: body.reason ?? "Unknown error",
        });
      } else {
        // Session completed normally
        await ctx.runMutation(internal.sessions.endSessionInternal, {
          sessionId: body.sessionId as Parameters<
            typeof ctx.runMutation<typeof internal.sessions.endSessionInternal>
          >[1]["sessionId"],
        });
      }

      return new Response(JSON.stringify({ success: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : "Unknown error";
      return new Response(JSON.stringify({ error: message }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }
  }),
});

/**
 * GET /api/machine/session/status
 * RPi checks if session is still active (to detect remote session end)
 */
http.route({
  path: "/api/machine/session/status",
  method: "GET",
  handler: httpAction(async (ctx, req) => {
    // Validate API key
    const authResult = await validateMachineAuth(ctx, req);
    if ("error" in authResult) {
      return authResult.error;
    }

    // Get session ID from query parameter
    const url = new URL(req.url);
    const sessionId = url.searchParams.get("sessionId");

    if (!sessionId) {
      return new Response(JSON.stringify({ error: "Missing sessionId" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    const sessionStatus = await ctx.runQuery(
      internal.sessions.getSessionStatus,
      {
        sessionId: sessionId as Parameters<
          typeof ctx.runQuery<typeof internal.sessions.getSessionStatus>
        >[1]["sessionId"],
      },
    );

    if (!sessionStatus) {
      return new Response(JSON.stringify({ error: "Session not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response(
      JSON.stringify({
        status: sessionStatus.status,
        endedAt: sessionStatus.endedAt,
        active: sessionStatus.status === "active",
      }),
      {
        status: 200,
        headers: { "Content-Type": "application/json" },
      },
    );
  }),
});

/**
 * POST /api/machine/data
 * Receive ECG data batch from Raspberry Pi
 */
http.route({
  path: "/api/machine/data",
  method: "POST",
  handler: httpAction(async (ctx, req) => {
    // Validate API key
    const authResult = await validateMachineAuth(ctx, req);
    if ("error" in authResult) {
      return authResult.error;
    }
    const { machine } = authResult;

    // Parse body
    let body: {
      sessionId?: string;
      timestamp?: number;
      sampleRate?: number;
      samples?: Array<{ channel: string; values: number[]; unit?: string }>;
      metrics?: Record<
        string,
        {
          heartRate?: number;
          hrv?: number;
          respRate?: number;
          scrCount?: number;
          activations?: number;
          pulse?: number;
          quality?: string;
        }
      >;
      batchId?: string;
    };

    try {
      body = await req.json();
    } catch {
      return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    // Validate required fields
    if (!body.sessionId) {
      return new Response(JSON.stringify({ error: "Missing sessionId" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (!body.timestamp) {
      return new Response(JSON.stringify({ error: "Missing timestamp" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (!body.samples || !Array.isArray(body.samples)) {
      return new Response(
        JSON.stringify({ error: "Missing or invalid samples array" }),
        {
          status: 400,
          headers: { "Content-Type": "application/json" },
        },
      );
    }

    // Validate timestamp
    const now = Date.now();
    const maxFuture = now + 60000; // 1 minute in future
    const maxPast = now - 300000; // 5 minutes in past

    if (body.timestamp > maxFuture) {
      return new Response(
        JSON.stringify({ error: "Timestamp too far in future" }),
        {
          status: 400,
          headers: { "Content-Type": "application/json" },
        },
      );
    }

    if (body.timestamp < maxPast) {
      return new Response(
        JSON.stringify({
          error: "Timestamp too old. Data must be less than 5 minutes old.",
        }),
        {
          status: 400,
          headers: { "Content-Type": "application/json" },
        },
      );
    }

    // Store the data
    try {
      await ctx.runMutation(internal.ecgData.storeEcgBatch, {
        machineId: machine._id as Parameters<
          typeof ctx.runMutation<typeof internal.ecgData.storeEcgBatch>
        >[1]["machineId"],
        sessionId: body.sessionId as Parameters<
          typeof ctx.runMutation<typeof internal.ecgData.storeEcgBatch>
        >[1]["sessionId"],
        timestamp: body.timestamp,
        sampleRate: body.sampleRate,
        samples: body.samples,
        metrics: body.metrics,
      });

      return new Response(
        JSON.stringify({
          success: true,
          batchId: body.batchId,
          serverTime: now,
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      );
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : "Unknown error";
      return new Response(JSON.stringify({ error: message }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }
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

async function readJson(req: Request): Promise<Record<string, unknown> | null> {
  try {
    const parsed: unknown = await req.json();
    return typeof parsed === "object" &&
      parsed !== null &&
      !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function isNum(x: unknown): x is number {
  return typeof x === "number" && Number.isFinite(x);
}
function isStr(x: unknown): x is string {
  return typeof x === "string";
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

/** Authenticate, then run `handle`; any thrown error becomes a 400 with its message. */
function machineRoute(
  handle: (
    ctx: Parameters<Parameters<typeof httpAction>[0]>[0],
    req: Request,
    machineId: Id<"machines">,
  ) => Promise<Response>,
) {
  return httpAction(async (ctx, req) => {
    const auth = await validateMachineAuth(ctx, req);
    if ("error" in auth) return auth.error;
    const machine: Machine = auth.machine;
    try {
      return await handle(ctx, req, machine._id as Id<"machines">);
    } catch (error: unknown) {
      return json(400, {
        error: error instanceof Error ? error.message : "Unknown error",
      });
    }
  });
}

function sessionIdOf(
  ctx: Parameters<Parameters<typeof httpAction>[0]>[0],
  raw: unknown,
): Id<"sessions"> | null {
  return isStr(raw) && raw.length > 0 ? (raw as Id<"sessions">) : null;
}

/** GET /api/machine/training/poll -> {session: PendingTraining | null} */
http.route({
  path: "/api/machine/training/poll",
  method: "GET",
  handler: machineRoute(async (ctx, _req, machineId) => {
    const session = await ctx.runQuery(
      internal.training.getPendingTrainingSession,
      { machineId },
    );
    return json(200, { session });
  }),
});

/** GET /api/machine/roster -> {riders: [{userId, name, hrMax}]} */
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

/** POST /api/machine/profiles {storeRev, programsEnabled, profiles[]} */
http.route({
  path: "/api/machine/profiles",
  method: "POST",
  handler: machineRoute(async (ctx, req, machineId) => {
    const body = await readJson(req);
    if (!body || !isNum(body.storeRev) || !Array.isArray(body.profiles)) {
      return json(400, {
        error: "Expected {storeRev, programsEnabled, profiles}",
      });
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
        return json(400, { error: "Profile must be an object" });
      }
      const p = raw as Record<string, unknown>;
      if (
        !isStr(p.profileId) ||
        !isStr(p.name) ||
        !keys.every((k) => isNum(p[k]))
      ) {
        return json(400, { error: `Malformed profile ${String(p.profileId)}` });
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

/** POST /api/machine/training/start {sessionId}: the Pi armed a remote launch. */
http.route({
  path: "/api/machine/training/start",
  method: "POST",
  handler: machineRoute(async (ctx, req, machineId) => {
    const body = await readJson(req);
    const sessionId = sessionIdOf(ctx, body?.sessionId);
    if (!sessionId) return json(400, { error: "Missing sessionId" });
    await ctx.runMutation(internal.training.markTrainingStarted, {
      machineId,
      sessionId,
    });
    return json(200, { success: true });
  }),
});

/** POST /api/machine/training/local {...}: register a session started at the machine. */
http.route({
  path: "/api/machine/training/local",
  method: "POST",
  handler: machineRoute(async (ctx, req, machineId) => {
    const b = await readJson(req);
    if (
      !b ||
      !isStr(b.localRef) ||
      (b.kind !== "auto" && b.kind !== "manual") ||
      !isNum(b.startedAt) ||
      !isStr(b.operatorName)
    ) {
      return json(400, {
        error: "Expected {localRef, kind, startedAt, operatorName}",
      });
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

/** POST /api/machine/training/end {sessionId, failed, reason, endedAt?} */
http.route({
  path: "/api/machine/training/end",
  method: "POST",
  handler: machineRoute(async (ctx, req, machineId) => {
    const b = await readJson(req);
    const sessionId = sessionIdOf(ctx, b?.sessionId);
    if (!b || !sessionId || !isStr(b.reason)) {
      return json(400, { error: "Expected {sessionId, failed, reason}" });
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

/** GET /api/machine/training/status?sessionId= -> {status, active, stopRequested} */
http.route({
  path: "/api/machine/training/status",
  method: "GET",
  handler: machineRoute(async (ctx, req, machineId) => {
    const sessionId = sessionIdOf(
      ctx,
      new URL(req.url).searchParams.get("sessionId"),
    );
    if (!sessionId) return json(400, { error: "Missing sessionId" });
    const status = await ctx.runQuery(internal.training.getTrainingStatus, {
      machineId,
      sessionId,
    });
    return status
      ? json(200, status)
      : json(404, { error: "Session not found" });
  }),
});

/** POST /api/machine/training/telemetry {sessionId, points[]} */
http.route({
  path: "/api/machine/training/telemetry",
  method: "POST",
  handler: machineRoute(async (ctx, req, machineId) => {
    const b = await readJson(req);
    const sessionId = sessionIdOf(ctx, b?.sessionId);
    if (!b || !sessionId || !Array.isArray(b.points)) {
      return json(400, { error: "Expected {sessionId, points}" });
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
        return json(400, { error: "Malformed telemetry point" });
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
    if (points.length > 600)
      return json(400, { error: "At most 600 points per batch" });
    const result = await ctx.runMutation(internal.training.storeTelemetry, {
      machineId,
      sessionId,
      points,
    });
    return json(200, result);
  }),
});

export default http;

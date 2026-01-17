import { httpRouter } from "convex/server";
import { httpAction } from "./_generated/server";
import { internal } from "./_generated/api";
import { hashApiKey } from "./lib/crypto";

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
      samples?: Array<{ channel: string; values: number[] }>;
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
        samples: body.samples,
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

export default http;

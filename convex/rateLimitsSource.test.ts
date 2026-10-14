/// <reference types="vite/client" />
/**
 * ANH-166: `convex/lib/rateLimits.ts` READS the machine limits and the body
 * sizes from the shared file `contracts/machine-api.json`, the one the Pi's
 * tests pin its cadences to; it keeps no copy of them.
 *
 * The shared file is replaced here by another: if the module held its own
 * values, it would still configure the real ones and these tests would fail.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

const ROUTES = [
  "POST /api/machine/heartbeat",
  "GET /api/machine/training/poll",
  "POST /api/machine/training/telemetry",
  "POST /api/machine/training/events",
  "POST /api/machine/records/upload-url",
] as const;

function substitute(
  routes: readonly string[],
  sizes: Record<string, number> = {
    "POST /api/machine/heartbeat": 111,
    "POST /api/machine/training/telemetry": 222,
    "POST /api/machine/training/events": 333,
  },
) {
  vi.resetModules();
  vi.doMock("../contracts/machine-api.json", () => ({
    default: {
      contract_version: "1.4",
      header: "X-Anheart-Contract",
      error_codes: { rate_limited: {}, payload_too_large: {} },
      rate_limits: {
        routes: Object.fromEntries(
          routes.map((route, index) => [
            route,
            { rate: index + 2, period_ms: 7_000, capacity: index + 3 },
          ]),
        ),
      },
      max_body_bytes: { routes: sizes },
    },
  }));
}

afterEach(() => {
  vi.doUnmock("../contracts/machine-api.json");
  vi.resetModules();
});

describe("ANH-166 the limits are read from contracts/machine-api.json", () => {
  it("configures each machine limit and each body size from that file", async () => {
    substitute(ROUTES);
    const limits = await import("./lib/rateLimits");

    expect(limits.rateLimiter.limits?.machineHeartbeat).toEqual({
      kind: "token bucket",
      rate: 2,
      period: 7_000,
      capacity: 3,
    });
    expect(limits.rateLimiter.limits?.machineUploadUrl).toEqual({
      kind: "token bucket",
      rate: 6,
      period: 7_000,
      capacity: 7,
    });
    expect([
      limits.MAX_HEARTBEAT_BYTES,
      limits.MAX_TELEMETRY_BYTES,
      limits.MAX_EVENTS_BYTES,
    ]).toEqual([111, 222, 333]);
  });

  it("refuses to load without a limit for a route it limits", async () => {
    substitute(ROUTES.filter((route) => !route.startsWith("GET")));

    await expect(import("./lib/rateLimits")).rejects.toThrow(
      "No rate limit for GET /api/machine/training/poll",
    );
  });

  it("refuses to load without a size for a route it bounds", async () => {
    substitute(ROUTES, { "POST /api/machine/heartbeat": 111 });

    await expect(import("./lib/rateLimits")).rejects.toThrow(
      "No body size for POST /api/machine/training/telemetry",
    );
  });
});

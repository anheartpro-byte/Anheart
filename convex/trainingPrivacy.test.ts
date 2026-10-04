/// <reference types="vite/client" />
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./_generated/api";
import {
  now,
  trainingFixture as seedTraining,
} from "./trainingPrivacy.fixtures";

const modules = import.meta.glob([
  "./**/*.ts",
  "!./**/*.test.ts",
  "!./**/*.fixtures.ts",
]);
const trainingFixture = () => seedTraining(modules);

type Fixture = Awaited<ReturnType<typeof trainingFixture>>;
type Reader = ReturnType<Fixture["t"]["withIdentity"]>;

const views = [
  {
    name: "getMachineLive",
    read: (t: Reader, f: Fixture) =>
      t.query(api.training.getMachineLive, { machineId: f.machineId }),
  },
  {
    name: "listLaunchableMachines",
    read: async (t: Reader, f: Fixture) =>
      (await t.query(api.training.listLaunchableMachines, {})).find(
        (m) => m._id === f.machineId,
      ) ?? null,
  },
] as const;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(now);
});
afterEach(() => {
  vi.useRealTimers();
});

describe.each(views)("$name privacy", ({ read }) => {
  it("hides another rider's telemetry while preserving launch availability", async () => {
    // Given
    const f = await trainingFixture();
    // When
    const result = await read(f.t.withIdentity({ subject: "launcher" }), f);
    // Then
    expect(result).toMatchObject({
      status: "in_session",
      programsEnabled: true,
      live: null,
    });
  });

  it.each(["rider", "manager", "admin"])(
    "preserves live telemetry for %s",
    async (subject) => {
      // Given
      const f = await trainingFixture();
      // When
      const result = await read(f.t.withIdentity({ subject }), f);
      // Then
      expect(result?.live).toEqual(f.live);
    },
  );

  it.each(["outsider", "otherManager"])(
    "denies machine access to %s",
    async (subject) => {
      // Given
      const f = await trainingFixture();
      // When
      const result = await read(f.t.withIdentity({ subject }), f);
      // Then
      expect(result).toBeNull();
    },
  );

  it("requires authentication", async () => {
    // Given
    const f = await trainingFixture();
    // When / Then
    await expect(read(f.t, f)).rejects.toThrow();
  });

  it.each([undefined, "invalid-session-id"])(
    "hides telemetry with unresolved session %s",
    async (sessionId) => {
      // Given
      const f = await trainingFixture();
      await f.t.run((ctx) =>
        ctx.db.patch(f.machineId, { live: { ...f.live, sessionId } }),
      );
      // When
      const result = await read(f.t.withIdentity({ subject: "rider" }), f);
      // Then
      expect(result?.live).toBeNull();
    },
  );

  it("hides telemetry after the referenced session is deleted", async () => {
    // Given
    const f = await trainingFixture();
    await f.t.run((ctx) => ctx.db.delete(f.sessionId));
    // When
    const result = await read(f.t.withIdentity({ subject: "rider" }), f);
    // Then
    expect(result?.live).toBeNull();
  });

  it("hides telemetry when the rider's session belongs to another machine", async () => {
    // Given
    const f = await trainingFixture();
    await f.t.run((ctx) =>
      ctx.db.patch(f.sessionId, { machineId: f.otherMachineId }),
    );
    // When
    const result = await read(f.t.withIdentity({ subject: "rider" }), f);
    // Then
    expect(result?.live).toBeNull();
  });

  it("keeps idle machines selectable without exposing unowned sensor data", async () => {
    // Given
    const f = await trainingFixture();
    await f.t.run((ctx) =>
      ctx.db.patch(f.machineId, {
        status: "online",
        live: { ...f.live, runMode: "repos", sessionId: undefined },
      }),
    );
    // When
    const result = await read(f.t.withIdentity({ subject: "launcher" }), f);
    // Then
    expect(result).toMatchObject({
      status: "online",
      programsEnabled: true,
      live: null,
    });
  });

  it("preserves manager access when a local session has no cloud identifier", async () => {
    // Given
    const f = await trainingFixture();
    const live = { ...f.live, sessionId: undefined };
    await f.t.run((ctx) => ctx.db.patch(f.machineId, { live }));
    // When
    const result = await read(f.t.withIdentity({ subject: "manager" }), f);
    // Then
    expect(result?.live).toEqual(live);
  });

  it("returns no live data when the machine has never reported it", async () => {
    // Given
    const f = await trainingFixture();
    await f.t.run((ctx) => ctx.db.patch(f.machineId, { live: undefined }));
    // When
    const result = await read(f.t.withIdentity({ subject: "manager" }), f);
    // Then
    expect(result?.live).toBeNull();
  });

  it("hides telemetry when the session identifier belongs to another table", async () => {
    // Given
    const f = await trainingFixture();
    await f.t.run((ctx) =>
      ctx.db.patch(f.machineId, { live: { ...f.live, sessionId: f.rider } }),
    );
    // When
    const result = await read(f.t.withIdentity({ subject: "rider" }), f);
    // Then
    expect(result?.live).toBeNull();
  });
});

describe("stale machine telemetry", () => {
  it.each(["launcher", "rider", "manager"])(
    "keeps stale launch-list telemetry unavailable to %s",
    async (subject) => {
      // Given
      const f = await trainingFixture();
      await f.t.run((ctx) =>
        ctx.db.patch(f.machineId, {
          live: { ...f.live, updatedAt: now - 90_001 },
        }),
      );
      // When
      const result = await f.t
        .withIdentity({ subject })
        .query(api.training.listLaunchableMachines, {});
      // Then
      expect(result).toMatchObject([{ status: "in_session", live: null }]);
    },
  );

  it.each(["rider", "manager"])(
    "marks authorized stale live telemetry for %s",
    async (subject) => {
      // Given
      const f = await trainingFixture();
      const live = { ...f.live, updatedAt: now - 90_001 };
      await f.t.run((ctx) => ctx.db.patch(f.machineId, { live }));
      // When
      const result = await f.t
        .withIdentity({ subject })
        .query(api.training.getMachineLive, { machineId: f.machineId });
      // Then
      expect(result).toMatchObject({ live, stale: true });
    },
  );

  it("hides stale telemetry from another rider", async () => {
    // Given
    const f = await trainingFixture();
    await f.t.run((ctx) =>
      ctx.db.patch(f.machineId, {
        live: { ...f.live, updatedAt: now - 90_001 },
      }),
    );
    // When
    const result = await f.t
      .withIdentity({ subject: "launcher" })
      .query(api.training.getMachineLive, { machineId: f.machineId });
    // Then
    expect(result).toMatchObject({ live: null, stale: true });
  });
});

describe("getSessionTelemetry boundary", () => {
  it.each(["launcher", "outsider", "otherManager"])(
    "denies telemetry to %s",
    async (subject) => {
      // Given
      const f = await trainingFixture();
      // When
      const rows = await f.t
        .withIdentity({ subject })
        .query(api.training.getSessionTelemetry, { sessionId: f.sessionId });
      // Then
      expect(rows).toEqual([]);
    },
  );

  it.each(["rider", "manager", "admin"])(
    "retains telemetry for %s",
    async (subject) => {
      // Given
      const f = await trainingFixture();
      // When
      const rows = await f.t
        .withIdentity({ subject })
        .query(api.training.getSessionTelemetry, { sessionId: f.sessionId });
      // Then
      expect(rows).toMatchObject([{ bpm: 137, t: now }]);
    },
  );
});

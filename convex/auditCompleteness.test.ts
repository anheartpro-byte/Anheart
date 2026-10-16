/// <reference types="vite/client" />
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./_generated/api";
import type { Doc } from "./_generated/dataModel";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";
import {
  addSession,
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  seedLaunchWorld,
  seedMachineWorld,
  type World,
} from "./test.setup";

const rows = (w: Pick<World, "t">) =>
  w.t.run((ctx) => ctx.db.query("audit_log").collect());

async function unchanged(w: Pick<World, "t">, call: () => Promise<unknown>) {
  const before = await rows(w);
  await call();
  expect(await rows(w)).toEqual(before);
}

type MachineWorld = Awaited<ReturnType<typeof seedMachineWorld>>;
function machinePost(w: MachineWorld, path: string, body: unknown) {
  return w.t.fetch(`/api/machine/training/${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${w.machineKey}`,
      "Content-Type": "application/json",
      [CONTRACT_HEADER]: CONTRACT_VERSION,
    },
    body: JSON.stringify(body),
  });
}

function expectEntry(
  actual: Doc<"audit_log"> | undefined,
  expected: Pick<
    Doc<"audit_log">,
    "action" | "source" | "actorRole" | "targetType" | "targetId"
  > &
    Pick<Partial<Doc<"audit_log">>, "actorId" | "organizationId" | "details">,
) {
  expect(actual).toMatchObject(expected);
  expect(actual?.actorId).toBe(expected.actorId);
  expect(actual?.organizationId).toBe(expected.organizationId);
  expect(actual?.requestId.length).toBeGreaterThan(0);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
  configureAnheartOrganization(null);
});

describe("ANH-167 T02 training safety and absent effects", () => {
  it("keeps grant/revoke replays unwritten and records only the changed launch right", async () => {
    const w = await seedLaunchWorld(modules);
    const args = { machineId: w.machine, userId: w.patient };
    const manager = as(w.t, "manager");
    await unchanged(w, () =>
      manager.mutation(api.training.grantLaunchRight, args),
    );
    await manager.mutation(api.training.revokeLaunchRight, args);
    expectEntry((await rows(w)).slice(-1)[0], {
      action: "launch_right.revoked",
      source: "site",
      actorRole: "gestionnaire",
      actorId: w.manager,
      organizationId: w.orgA,
      targetType: "user",
      targetId: w.patient,
      details: { machineId: w.machine },
    });
    await unchanged(w, () =>
      manager.mutation(api.training.revokeLaunchRight, args),
    );
    await manager.mutation(api.training.grantLaunchRight, args);
    expect((await rows(w)).slice(-1)[0]?.action).toBe("launch_right.granted");
  });

  it("always stops active sessions and cancels pending sessions with one row per effect", async () => {
    const w = await seedLaunchWorld(modules);
    for (const status of ["active", "pending", "completed"] as const) {
      const sessionId = await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status,
        kind: "auto",
      });
      const call = () =>
        as(w.t, "patient").mutation(api.training.requestStop, { sessionId });
      if (status === "completed") {
        await unchanged(w, call);
        continue;
      }
      const before = (await rows(w)).length;
      await call();
      const session = await w.t.run((ctx) => ctx.db.get(sessionId));
      if (status === "active") expect(session?.stopRequestedAt).toBe(NOW);
      else expect(session?.status).toBe("failed");
      expect((await rows(w)).length).toBe(before + 1);
      expectEntry((await rows(w)).slice(-1)[0], {
        action:
          status === "active" ? "session.stop_requested" : "session.cancelled",
        source: "site",
        actorRole: "user",
        actorId: w.patient,
        organizationId: w.orgA,
        targetType: "session",
        targetId: sessionId,
      });
      await unchanged(w, call);
    }
  });

  it("omits physiology values and refuses a patient without an audit row", async () => {
    const w = await seedLaunchWorld(modules);
    const args = { userId: w.patient, hrMax: 187, birthYear: 1990 };
    await unchanged(w, () =>
      expect(
        as(w.t, "patient").mutation(api.training.setUserPhysiology, args),
      ).rejects.toThrow("not_authorized"),
    );
    await as(w.t, "manager").mutation(api.training.setUserPhysiology, args);
    const row = (await rows(w)).slice(-1)[0];
    expect(row?.details?.fields).toEqual(["hrMax", "birthYear"]);
    expect(JSON.stringify(row)).not.toContain("187");
    expect(JSON.stringify(row)).not.toContain("1990");
  });

  it("commits arming refusal as failed, keeps HTTP 409, and writes no started row on refusal or replay", async () => {
    const w = await seedMachineWorld(modules);
    const sessionId = await w.admin.mutation(api.training.launchAutoSession, {
      machineId: w.machine,
      userId: w.patient,
      profileId: w.profileId,
    });
    await w.t.run((ctx) => ctx.db.patch(w.patient, { hrMax: 187 }));
    const call = async () => {
      const response = await machinePost(w, "start", { sessionId });
      expect(response.status).toBe(409);
      expect(await response.json()).toMatchObject({
        error: "hr_max_changed_before_start",
      });
      expect((await w.t.run((ctx) => ctx.db.get(sessionId)))?.status).toBe(
        "failed",
      );
    };
    await unchanged(w, call);
    await unchanged(w, call);
  });

  it("records local and end effects once through HTTP and omits the end reason", async () => {
    const w = await seedMachineWorld(modules);
    const body = {
      localRef: "synthetic-audit-local",
      kind: "manual",
      startedAt: NOW,
      operatorName: "Synthetic operator",
    };
    const first = await machinePost(w, "local", body);
    expect(first.status).toBe(200);
    const { sessionId } = (await first.json()) as { sessionId: string };
    expectEntry((await rows(w)).slice(-1)[0], {
      action: "session.local_registered",
      source: "machine",
      actorRole: "machine",
      actorId: w.machine,
      organizationId: w.organizationId,
      targetType: "session",
      targetId: sessionId,
    });
    await unchanged(w, async () =>
      expect((await machinePost(w, "local", body)).status).toBe(200),
    );
    const end = {
      sessionId,
      failed: true,
      reason: "synthetic-private-end-code-and-reason",
    };
    expect((await machinePost(w, "end", end)).status).toBe(200);
    const row = (await rows(w)).slice(-1)[0];
    expectEntry(row, {
      action: "session.ended",
      source: "machine",
      actorRole: "machine",
      actorId: w.machine,
      organizationId: w.organizationId,
      targetType: "session",
      targetId: sessionId,
      details: { outcome: "failed" },
    });
    expect(JSON.stringify(row)).not.toContain(end.reason);
    await unchanged(w, async () =>
      expect((await machinePost(w, "end", end)).status).toBe(200),
    );
  });
});

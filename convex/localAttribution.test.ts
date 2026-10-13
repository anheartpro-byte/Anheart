/// <reference types="vite/client" />
import { afterEach, describe, expect, it } from "vitest";
import { api, internal } from "./_generated/api";
import { machineHeaders } from "./machineAuth.fixtures";
import {
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  seedMachineWorld,
  seedWorld,
} from "./test.setup";

afterEach(() => configureAnheartOrganization(null));

describe("local session attribution", () => {
  it.each([
    "authorized",
    "no-right",
    "inactive",
    "foreign",
    "malformed",
    "empty",
    "deleted",
    "absent",
  ] as const)("retains the session and resolves the %s rider", async (kind) => {
    const w = await seedWorld(modules);
    if (kind === "deleted") await w.t.run((ctx) => ctx.db.delete(w.patient));
    if (kind === "inactive") {
      await w.t.run(async (ctx) => {
        const rows = await ctx.db.query("memberships").collect();
        for (const row of rows) {
          if (row.userId === w.patient)
            await ctx.db.patch(row._id, { active: false });
        }
      });
    }
    const userId =
      kind === "absent"
        ? undefined
        : kind === "empty"
          ? ""
          : kind === "malformed"
            ? "invalid"
            : kind === "foreign"
              ? w.orgBPatient
              : kind === "no-right"
                ? w.otherPatient
                : w.patient;
    const id = await w.t.mutation(internal.training.registerLocalSession, {
      machineId: w.machine,
      localRef: `attribution-${kind}`,
      kind: "manual",
      startedAt: NOW,
      operatorName: "Operator",
      userId,
      subjectLabel: "Local label",
      occupancy: "occupied",
    });
    const row = await w.t.run((ctx) => ctx.db.get(id));
    expect(row).toMatchObject({
      organizationId: w.orgA,
      status: "active",
      origin: "local",
    });
    if (kind === "authorized") {
      expect(row?.userId).toBe(w.patient);
      expect(row?.notes).toBe("Occupancy: occupied");
    } else if (kind === "absent") {
      expect(row?.userId).toBeUndefined();
      expect(row?.notes).toBe("Occupancy: occupied");
    } else {
      expect(row?.userId).toBeUndefined();
      expect(row?.subjectLabel).toBeUndefined();
      expect(row?.notes).toBe(
        "Occupancy: occupied; Passenger attribution unavailable at registration",
      );
    }
    if (kind === "no-right") {
      expect(
        await as(w.t, "otherPatient").query(api.sessions.listSessions, {}),
      ).toEqual([]);
    }
  });

  it("keeps registration retries stable across a change of permission", async () => {
    const w = await seedWorld(modules);
    const args = {
      machineId: w.machine,
      localRef: "stable-registration",
      kind: "manual" as const,
      startedAt: NOW,
      operatorName: "Operator",
      userId: w.patient,
    };
    const first = await w.t.mutation(
      internal.training.registerLocalSession,
      args,
    );
    await w.t.run(async (ctx) => {
      const permission = await ctx.db
        .query("machine_user_permissions")
        .withIndex("by_machine_and_user", (q) =>
          q.eq("machineId", w.machine).eq("userId", w.patient),
        )
        .unique();
      if (!permission) throw new Error("Missing fixture permission");
      await ctx.db.delete(permission._id);
    });
    expect(
      await w.t.mutation(internal.training.registerLocalSession, args),
    ).toBe(first);
    expect((await w.t.run((ctx) => ctx.db.get(first)))?.userId).toBe(w.patient);
    const late = await w.t.mutation(internal.training.registerLocalSession, {
      ...args,
      localRef: "late-registration",
    });
    expect(await w.t.run((ctx) => ctx.db.get(late))).toMatchObject({
      notes: "Passenger attribution unavailable at registration",
    });
    expect((await w.t.run((ctx) => ctx.db.get(late)))?.userId).toBeUndefined();
    await w.t.run((ctx) =>
      ctx.db.insert("machine_user_permissions", {
        organizationId: w.orgA,
        machineId: w.machine,
        userId: w.patient,
        grantedBy: w.manager,
        createdAt: NOW,
      }),
    );
    expect(
      await w.t.mutation(internal.training.registerLocalSession, {
        ...args,
        localRef: "late-registration",
      }),
    ).toBe(late);
    expect((await w.t.run((ctx) => ctx.db.get(late)))?.userId).toBeUndefined();
  });

  it.each([true, false])(
    "serves local registration with roster membership %s",
    async (permitted) => {
      const w = await seedMachineWorld(modules);
      if (!permitted) {
        await w.t.run(async (ctx) => {
          const permissions = await ctx.db
            .query("machine_user_permissions")
            .collect();
          for (const permission of permissions)
            await ctx.db.delete(permission._id);
        });
      }
      const response = await w.t.fetch("/api/machine/training/local", {
        method: "POST",
        headers: machineHeaders(w.machineKey),
        body: JSON.stringify({
          localRef: "route-registration",
          kind: "manual",
          startedAt: NOW,
          operatorName: "Operator",
          userId: w.patient,
          subjectLabel: "Local label",
        }),
      });
      expect(response.status).toBe(200);
      const rows = await w.t.run((ctx) => ctx.db.query("sessions").collect());
      expect(rows).toHaveLength(1);
      expect(rows[0]?.userId).toBe(permitted ? w.patient : undefined);
      expect(rows[0]?.subjectLabel).toBe(permitted ? "Local label" : undefined);
      expect(rows[0]?.notes).toBe(
        permitted
          ? undefined
          : "Passenger attribution unavailable at registration",
      );
    },
  );
});

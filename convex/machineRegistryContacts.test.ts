import { describe, expect, it } from "vitest";
import { api } from "./_generated/api";
import { as, modules, seedWorld } from "./test.setup";

describe("machine registry contact candidates", () => {
  it("offers only active members, with their organisation role and no email, and accepts every candidate", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      const inactive = await ctx.db
        .query("memberships")
        .withIndex("by_user_and_organization", (q) =>
          q.eq("userId", w.otherManager).eq("organizationId", w.orgA),
        )
        .unique();
      if (inactive === null) throw new Error("Missing test membership");
      await ctx.db.patch(inactive._id, { active: false });
      await ctx.db.patch(w.otherPatient, { clerkDeletedAt: Date.now() });
      await ctx.db.delete(w.stranger);
      await ctx.db.insert("memberships", {
        userId: w.orgBManager,
        organizationId: w.orgA,
        role: "admin",
        active: true,
      });
    });
    const rows = await as(w.t, "admin").query(
      api.machineRegistry.listContactCandidates,
      { machineId: w.machine },
    );
    expect(rows).toEqual([
      {
        userId: w.manager,
        firstName: "manager",
        lastName: "Synthetic",
        role: "gestionnaire",
      },
      {
        userId: w.orgAdmin,
        firstName: "orgAdmin",
        lastName: "Synthetic",
        role: "admin",
      },
      {
        userId: w.orgBManager,
        firstName: "orgBManager",
        lastName: "Synthetic",
        role: "admin",
      },
      {
        userId: w.patient,
        firstName: "patient",
        lastName: "Synthetic",
        role: "user",
      },
    ]);
    await as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
      machineId: w.machine,
      contacts: rows.map((row) => row.userId),
    });
    expect(
      (
        await as(w.t, "admin").query(api.machineRegistry.getRegistry, {
          machineId: w.machine,
        })
      )?.contacts,
    ).toEqual(
      rows.map(({ userId, firstName, lastName }) => ({
        userId,
        firstName,
        lastName,
      })),
    );
  });

  it("scopes foreign machines to their own organisation and sorts by last name, first name then id", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      await ctx.db.patch(w.orgBManager, { firstName: "TEST", lastName: "A" });
      await ctx.db.patch(w.orgBPatient, { firstName: "TEST", lastName: "A" });
      await ctx.db.patch(w.orgBAdmin, { firstName: "TEST", lastName: "B" });
    });
    const rows = await as(w.t, "admin").query(
      api.machineRegistry.listContactCandidates,
      { machineId: w.orgBMachine },
    );
    expect(rows.map((row) => row.userId)).toEqual([
      ...[w.orgBManager, w.orgBPatient].sort((a, b) => a.localeCompare(b)),
      w.orgBAdmin,
    ]);
    expect(rows.every((row) => row.firstName === "TEST")).toBe(true);
  });

  it("refuses missing or deleted machines and offers nobody for a machine without an organisation", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      await ctx.db.delete(w.otherMachine);
      await ctx.db.patch(w.orgBMachine, { isDeleted: true });
      await ctx.db.patch(w.machine, { organizationId: undefined });
    });
    for (const machineId of [w.otherMachine, w.orgBMachine]) {
      await expect(
        as(w.t, "admin").query(api.machineRegistry.listContactCandidates, {
          machineId,
        }),
      ).rejects.toThrow("machine_not_found");
    }
    expect(
      await as(w.t, "admin").query(api.machineRegistry.listContactCandidates, {
        machineId: w.machine,
      }),
    ).toEqual([]);
  });
});

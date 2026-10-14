/// <reference types="vite/client" />
/**
 * ANH-84: what `GET /api/machine/roster` tells the local console of each
 * rider. The console vets a programmed session with these values the way
 * `launchAutoSession` does on the site, and cannot change them: an absent or
 * unusable value is served as null, never replaced by another.
 */
import { describe, expect, it } from "vitest";
import { internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";
import { modules, NOW, seedMachineWorld } from "./test.setup";

type World = Awaited<ReturnType<typeof seedMachineWorld>>;
type Rider = {
  userId: string;
  name: string;
  hrMax: number | null;
  birthYear: number | null;
};

/** A rider of the machine's organisation holding the launch right, with `physiology`. */
async function rider(
  w: World,
  clerkId: string,
  physiology: { hrMax?: number; birthYear?: number },
): Promise<Id<"users">> {
  return await w.t.run(async (ctx) => {
    const id = await ctx.db.insert("users", {
      clerkId,
      role: "user" as const,
      organizationId: w.organizationId,
      firstName: "Synthetic",
      lastName: clerkId,
      email: `${clerkId}@example.invalid`,
      language: "en" as const,
      createdAt: NOW,
      ...physiology,
    });
    await ctx.db.insert("memberships", {
      userId: id,
      organizationId: w.organizationId,
      role: "user",
      active: true,
    });
    await ctx.db.insert("machine_user_permissions", {
      organizationId: w.organizationId,
      machineId: w.machine,
      userId: id,
      grantedBy: w.adminId,
      createdAt: NOW,
    });
    return id;
  });
}

async function served(w: World): Promise<Rider[]> {
  const response = await w.t.fetch("/api/machine/roster", {
    method: "GET",
    headers: {
      Authorization: `Bearer ${w.machineKey}`,
      [CONTRACT_HEADER]: CONTRACT_VERSION,
    },
  });
  expect(response.status).toBe(200);
  return ((await response.json()) as { riders: Rider[] }).riders;
}

describe("ANH-84 roster: the values a console vets a programme with", () => {
  it("serves the max heart rate and the birth year set on the site", async () => {
    const w = await seedMachineWorld(modules);
    const riders = await served(w);
    expect(riders).toEqual([
      {
        userId: w.patient,
        name: "Synthetic Patient",
        hrMax: 180,
        birthYear: 1990,
      },
    ]);
  });

  it("serves null for a rider whose manager has set neither", async () => {
    const w = await seedMachineWorld(modules);
    const bare = await rider(w, "roster-bare", {});
    const riders = await served(w);
    expect(riders.find((r) => r.userId === bare)).toEqual({
      userId: bare,
      name: "Synthetic roster-bare",
      hrMax: null,
      birthYear: null,
    });
  });

  it("estimates the max heart rate from the birth year, as a launch from the site would", async () => {
    const w = await seedMachineWorld(modules);
    const year = new Date().getUTCFullYear();
    const estimated = await rider(w, "roster-estimated", {
      birthYear: year - 40,
    });
    const roster = await w.t.query(internal.training.getRoster, {
      machineId: w.machine,
    });
    expect(roster.find((r) => r.userId === estimated)).toMatchObject({
      hrMax: Math.round(208 - 0.7 * 40),
      birthYear: year - 40,
    });
  });

  it("serves null for a value on record that is not a whole number", async () => {
    const w = await seedMachineWorld(modules);
    const fractional = await rider(w, "roster-fractional", {
      hrMax: 180.5,
      birthYear: 1990.5,
    });
    const roster = await w.t.query(internal.training.getRoster, {
      machineId: w.machine,
    });
    expect(roster.find((r) => r.userId === fractional)).toMatchObject({
      hrMax: null,
      birthYear: null,
    });
  });

  it("no longer lists a rider whose launch right was revoked", async () => {
    const w = await seedMachineWorld(modules);
    await w.t.run(async (ctx) => {
      const rows = await ctx.db
        .query("machine_user_permissions")
        .withIndex("by_machine", (q) => q.eq("machineId", w.machine))
        .collect();
      for (const row of rows) await ctx.db.delete(row._id);
    });
    expect(await served(w)).toEqual([]);
  });
});

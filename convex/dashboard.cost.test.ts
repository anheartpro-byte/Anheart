/// <reference types="vite/client" />
/**
 * ANH-226: what one call of `dashboard.counters` reads, on worlds of realistic
 * size (docs/convex.md, `dashboard.ts`, paragraph "Coût").
 *
 * Measured in memory: `convex-test` counts the documents, the index ranges and
 * the bytes a call reads (`ctx.meta.getTransactionMetrics()`), against the
 * limits Convex sets on one call. No deployment is touched. The query keeps
 * every row its index ranges return (no `.filter()`), so what the ranges return
 * is what a deployment reads too.
 *
 * - "today": Anheart and 3 centres of 2 machines and 40 accounts, 2 open
 *   sessions;
 * - "target": Anheart and 20 centres of 5 machines and 150 accounts, 20 open
 *   sessions, and rows no figure counts: ended sessions, deleted machines,
 *   memberships no longer active, heartbeats.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FunctionReturnType } from "convex/server";
import { api } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import type { MutationCtx } from "./_generated/server";
import { getCurrentUserOrThrow } from "./lib/auth";
import {
  ANHEART_CLERK_ORG,
  ORG_A_CLERK_ORG,
  ORG_B_CLERK_ORG,
  as,
  backendOf,
  configureAnheartOrganization,
  modules,
  NOW,
  type Actor,
} from "./test.setup";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
});

/** Accounts and machines of one centre. */
type Centre = {
  machines: number;
  admins: number;
  gestionnaires: number;
  patients: number;
};

/** A world: its centres, all alike, and its open sessions, one per centre in turn. */
type Shape = Centre & { centres: number; openSessions: number };

/** Rows that no figure counts. */
type Leftovers = {
  endedSessions: number;
  deletedMachines: number;
  inactiveMemberships: number;
  heartbeats: number;
};

const TODAY: Shape = {
  centres: 3,
  machines: 2,
  admins: 2,
  gestionnaires: 3,
  patients: 35,
  openSessions: 2,
};

const TARGET: Shape = {
  centres: 20,
  machines: 5,
  admins: 2,
  gestionnaires: 8,
  patients: 140,
  openSessions: 20,
};

const TARGET_LEFTOVERS: Leftovers = {
  endedSessions: 500,
  deletedMachines: 10,
  inactiveMemberships: 200,
  heartbeats: 1000,
};

const NO_LEFTOVERS: Leftovers = {
  endedSessions: 0,
  deletedMachines: 0,
  inactiveMemberships: 0,
  heartbeats: 0,
};

const HOUR = 3_600_000;

/** What `seed` returns for one centre. */
type Seeded = {
  organizationId: Id<"organizations">;
  machines: Id<"machines">[];
  gestionnaires: Id<"users">[];
  patients: Id<"users">[];
};

/** The first account of each role takes the name of an actor of `as`. */
type Names = Partial<Record<"admin" | "gestionnaire" | "user", Actor>>;

async function addAccount(
  ctx: MutationCtx,
  organizationId: Id<"organizations">,
  clerkId: string,
  role: "admin" | "gestionnaire" | "user",
  active = true,
): Promise<Id<"users">> {
  const userId = await ctx.db.insert("users", {
    clerkId,
    role,
    organizationId,
    firstName: `Prénom ${clerkId}`,
    lastName: "Synthétique",
    email: `${clerkId}@example.invalid`,
    language: "fr",
    createdAt: NOW - 30 * 24 * HOUR,
    ...(role === "user" ? { hrMax: 175, birthYear: 1968 } : {}),
  });
  await ctx.db.insert("memberships", {
    clerkMembershipId: `orgmem_${clerkId}`,
    clerkUpdatedAt: NOW - 24 * HOUR,
    clerkEventAt: NOW - 24 * HOUR,
    userId,
    organizationId,
    role,
    active,
  });
  return userId;
}

/** A machine as its heartbeats leave it: versions, live state and health. */
async function addMachine(
  ctx: MutationCtx,
  organizationId: Id<"organizations">,
  name: string,
  index: number,
  deleted?: { by: Id<"users"> },
): Promise<Id<"machines">> {
  const statuses = ["online", "in_session", "offline"] as const;
  return await ctx.db.insert("machines", {
    organizationId,
    name,
    location: `${name}, salle ${index + 1}`,
    status: deleted ? "offline" : statuses[index % statuses.length],
    lastHeartbeat: NOW - index * 10_000,
    createdAt: NOW - 90 * 24 * HOUR,
    programsEnabled: true,
    softwareVersion: "pi-0.4.2",
    contractVersion: "1.4.0",
    medicalParametersVersion: "0.1.0",
    lastVersionSeenAt: NOW,
    live: {
      runMode: "repos",
      phase: "idle",
      bpm: 72,
      motorRpm: 0,
      outputRpm: 0,
      setpointMotorRpm: 0,
      gLoad: 0,
      safetyAction: "none",
      driveState: "ready",
      updatedAt: NOW,
    },
    health: {
      cpu_temp_c: 51.2,
      cpu_load_1m: 0.42,
      mem_used_pct: 38.5,
      disk_free_mb: 21_480,
      uptime_s: 86_400,
      clock_offset_s: 0.01,
      link_rtt_ms: 84,
      bitalino_battery_pct: 76,
      drive_thermal_state: 31,
      records_pending: 0,
      incidents_open_high: 0,
      bitalino_link: "up",
      drive_link: "up",
      record_degraded: false,
      restart_loop: false,
      preflight_last: "ok",
      counters: {
        rotation_hours: 412.5,
        sessions_total: 380,
        sessions_failed: 4,
        drive_faults_by_code: { OHF: 1 },
      },
    },
    healthUpdatedAt: NOW,
    ...(deleted
      ? { isDeleted: true, deletedAt: NOW - HOUR, deletedBy: deleted.by }
      : {}),
  });
}

/**
 * The accounts and machines of a centre, with its gestionnaires' links: the
 * gestionnaire `i` manages machine `i` (modulo) and the patients `p` with
 * `p % gestionnaires === i`. `tag` keeps the Clerk identifiers unique.
 */
async function seed(
  ctx: MutationCtx,
  organizationId: Id<"organizations">,
  centre: Centre,
  tag: string,
  createdBy: Id<"users">,
  names: Names = {},
): Promise<Seeded> {
  const accounts = async (
    role: "admin" | "gestionnaire" | "user",
    count: number,
  ) => {
    const ids: Id<"users">[] = [];
    for (let k = 0; k < count; k++) {
      const name = k === 0 ? names[role] : undefined;
      ids.push(await addAccount(ctx, organizationId, name ?? `${tag}-${role}-${k}`, role));
    }
    return ids;
  };
  await accounts("admin", centre.admins);
  const gestionnaires = await accounts("gestionnaire", centre.gestionnaires);
  const patients = await accounts("user", centre.patients);
  const machines: Id<"machines">[] = [];
  for (let j = 0; j < centre.machines; j++) {
    machines.push(await addMachine(ctx, organizationId, `Machine ${tag}-${j}`, j));
  }
  for (const [i, gestionnaireId] of gestionnaires.entries()) {
    await ctx.db.insert("machine_gestionnaires", {
      organizationId,
      machineId: machines[i % machines.length],
      gestionnaireId,
      isOwner: i < machines.length,
      createdAt: NOW,
      createdBy,
    });
  }
  for (const [p, userId] of patients.entries()) {
    await ctx.db.insert("user_gestionnaires", {
      organizationId,
      userId,
      gestionnaireId: gestionnaires[p % gestionnaires.length],
      createdAt: NOW,
      createdBy,
    });
  }
  return { organizationId, machines, gestionnaires, patients };
}

async function addSession(
  ctx: MutationCtx,
  centre: Seeded,
  k: number,
  status: "pending" | "active" | "completed" | "failed",
): Promise<void> {
  const startedAt = NOW - (k + 1) * HOUR;
  const ended = status === "completed" || status === "failed";
  await ctx.db.insert("sessions", {
    organizationId: centre.organizationId,
    machineId: centre.machines[k % centre.machines.length],
    userId: centre.patients[k % centre.patients.length],
    startedById: centre.gestionnaires[k % centre.gestionnaires.length],
    status,
    startedAt,
    channels: ["ECG"],
    kind: "auto",
    origin: "remote",
    profileId: "p1",
    profileName: "Programme p1",
    zoneLowBpm: 130,
    zoneHighBpm: 150,
    totalDurationS: 1200,
    subjectHrMax: 175,
    subjectAge: 58,
    operatorName: "Gestionnaire synthétique",
    ...(ended ? { endedAt: startedAt + 20 * 60_000 } : {}),
  });
}

/** Open sessions `from` to `from + count - 1`: centre `k % n`, active then pending. */
async function addOpenSessions(
  ctx: MutationCtx,
  centres: Seeded[],
  count: number,
  from = 0,
): Promise<void> {
  for (let k = from; k < from + count; k++) {
    const centre = centres[k % centres.length];
    const turn = Math.floor(k / centres.length);
    await addSession(ctx, centre, turn, k % 2 === 0 ? "active" : "pending");
  }
}

async function addLeftovers(
  ctx: MutationCtx,
  centres: Seeded[],
  admin: Id<"users">,
  leftovers: Leftovers,
  tag: string,
): Promise<void> {
  const centreOf = (i: number) => centres[i % centres.length];
  for (let i = 0; i < leftovers.endedSessions; i++) {
    await addSession(ctx, centreOf(i), i, i % 10 === 0 ? "failed" : "completed");
  }
  for (let i = 0; i < leftovers.deletedMachines; i++) {
    const { organizationId } = centreOf(i);
    await addMachine(ctx, organizationId, `Retirée ${tag}-${i}`, i, { by: admin });
  }
  for (let i = 0; i < leftovers.inactiveMemberships; i++) {
    const { organizationId } = centreOf(i);
    await addAccount(ctx, organizationId, `${tag}-former-${i}`, "user", false);
  }
  const machines = centres.flatMap((centre) => centre.machines);
  for (let i = 0; i < leftovers.heartbeats; i++) {
    await ctx.db.insert("machine_heartbeats", {
      machineId: machines[i % machines.length],
      timestamp: NOW - Math.floor(i / machines.length) * 10_000,
      batteryLevel: 76,
      wifiStrength: -58,
    });
  }
}

/** Centres 1 and 2 are those of the actors of `as` ("orgAdmin", "manager", "patient"). */
function clerkOrgOf(c: number): string {
  if (c === 0) return ORG_A_CLERK_ORG;
  if (c === 1) return ORG_B_CLERK_ORG;
  return `org_synthetic_centre_${c + 1}`;
}

/** A world of this shape, built in one transaction. */
async function build(shape: Shape, leftovers: Leftovers) {
  configureAnheartOrganization(ANHEART_CLERK_ORG);
  const t = backendOf(modules);
  const ids = await t.run(async (ctx) => {
    const addOrganization = (clerkOrgId: string, name: string) =>
      ctx.db.insert("organizations", {
        clerkOrgId,
        name,
        slug: name.toLowerCase().replace(/ /g, "-"),
        createdAt: NOW - 365 * 24 * HOUR,
        settings: { requirePrescription: false, language: "fr" },
      });
    const anheart = await addOrganization(ANHEART_CLERK_ORG, "Anheart");
    const admin = await addAccount(ctx, anheart, "admin", "admin");
    const centres: Seeded[] = [];
    for (let c = 0; c < shape.centres; c++) {
      const organizationId = await addOrganization(clerkOrgOf(c), `Centre ${c + 1}`);
      const names: Names =
        c === 0 ? { admin: "orgAdmin", gestionnaire: "manager", user: "patient" } : {};
      centres.push(await seed(ctx, organizationId, shape, `c${c + 1}`, admin, names));
    }
    await addOpenSessions(ctx, centres, shape.openSessions);
    await addLeftovers(ctx, centres, admin, leftovers, "w");
    return { admin, centres };
  });
  return { t, ...ids };
}

type Counted = FunctionReturnType<typeof api.dashboard.counters>;

/** What one call read. */
type Read = { documents: number; ranges: number; bytes: number };

/**
 * What `read` reads, run in one transaction as `actor`, and the limits of
 * Convex on one call, as the transaction states them (`used + remaining`).
 */
async function measure<T>(
  t: ReturnType<typeof backendOf>,
  actor: Actor,
  read: (ctx: MutationCtx) => Promise<T>,
): Promise<{ result: T; used: Read; limit: Read }> {
  return await as(t, actor).run(async (ctx) => {
    const before = await ctx.meta.getTransactionMetrics();
    const result = await read(ctx);
    const after = await ctx.meta.getTransactionMetrics();
    const used = (key: "documentsRead" | "databaseQueries" | "bytesRead") =>
      after[key].used - before[key].used;
    const limit = (key: "documentsRead" | "databaseQueries" | "bytesRead") =>
      after[key].used + after[key].remaining;
    return {
      result,
      used: {
        documents: used("documentsRead"),
        ranges: used("databaseQueries"),
        bytes: used("bytesRead"),
      },
      limit: {
        documents: limit("documentsRead"),
        ranges: limit("databaseQueries"),
        bytes: limit("bytesRead"),
      },
    };
  });
}

/** One call of `dashboard.counters`, the registered function itself. */
function counters(t: ReturnType<typeof backendOf>, actor: Actor) {
  return measure(t, actor, (ctx): Promise<Counted> =>
    ctx.runQuery(api.dashboard.counters, {}),
  );
}

/** What resolving the caller alone reads (`getCurrentUserOrThrow`). */
async function callerPart(t: ReturnType<typeof backendOf>, actor: Actor) {
  const { used } = await measure(t, actor, async (ctx) => {
    await getCurrentUserOrThrow(ctx);
    return null;
  });
  return used;
}

/** The rows the Anheart admin's figures count, from the shape of the world. */
function countedRows(shape: Shape) {
  const accounts = shape.admins + shape.gestionnaires + shape.patients;
  return {
    // The Anheart admin's own membership, then every centre's.
    memberships: 1 + shape.centres * accounts,
    machines: shape.centres * shape.machines,
    sessions: shape.openSessions,
  };
}

const ROLES = ["admin", "orgAdmin", "manager", "patient"] as const;

describe("dashboard.counters: the cost of a call, world « today »", () => {
  it("measures what the site's own call gets, for every role", async () => {
    const w = await build(TODAY, NO_LEFTOVERS);

    for (const actor of ROLES) {
      const measured = await counters(w.t, actor);
      const served = await as(w.t, actor).query(api.dashboard.counters, {});

      expect(measured.result).toEqual(served);
    }
  });

  it("costs the Anheart admin the caller, the active memberships, the machines not deleted and the open sessions", async () => {
    const w = await build(TODAY, NO_LEFTOVERS);
    const rows = countedRows(TODAY);

    const caller = await callerPart(w.t, "admin");
    const { used, result } = await counters(w.t, "admin");

    expect(result.members?.total).toBe(rows.memberships);
    expect(result.machines?.total).toBe(rows.machines);
    expect(used.documents).toBe(
      caller.documents + rows.memberships + rows.machines + rows.sessions,
    );
    // Two ranges of machines, two of sessions, one of memberships.
    expect(used.ranges).toBe(caller.ranges + 5);
  });

  it("reads none of the rows no figure counts: ended sessions, deleted machines, inactive memberships, heartbeats", async () => {
    const w = await build(TODAY, NO_LEFTOVERS);
    const before = await counters(w.t, "admin");

    await w.t.run((ctx) => addLeftovers(ctx, w.centres, w.admin, TARGET_LEFTOVERS, "x"));
    const after = await counters(w.t, "admin");

    expect(after.used).toEqual(before.used);
    expect(after.result).toEqual(before.result);
  });

  it.each(["orgAdmin", "manager", "patient"] as const)(
    "costs %s of centre 1 nothing more when centre 2 grows",
    async (actor) => {
      const w = await build(TODAY, NO_LEFTOVERS);
      const before = await counters(w.t, actor);

      await w.t.run(async (ctx) => {
        const grown = await seed(ctx, w.centres[1].organizationId, TARGET, "c2b", w.admin);
        await addOpenSessions(ctx, [grown], 10);
        await addLeftovers(ctx, [grown], w.admin, TARGET_LEFTOVERS, "y");
      });
      const after = await counters(w.t, actor);
      const anheart = await counters(w.t, "admin");

      expect(after.used).toEqual(before.used);
      expect(after.result).toEqual(before.result);
      // Centre 2 did grow: the Anheart admin counts its new accounts.
      expect(anheart.result.members?.total).toBe(
        countedRows(TODAY).memberships + TARGET.admins + TARGET.gestionnaires + TARGET.patients,
      );
    },
  );
});

describe("dashboard.counters: the cost of a call, world « target »", () => {
  it("costs the Anheart admin exactly what it counts, under 10 % of the limits of a call", async () => {
    const w = await build(TARGET, TARGET_LEFTOVERS);
    const rows = countedRows(TARGET);

    const caller = await callerPart(w.t, "admin");
    const { used, limit, result } = await counters(w.t, "admin");

    expect(result.members?.total).toBe(rows.memberships);
    expect(result.machines?.total).toBe(rows.machines);
    expect(result.sessions.active + result.sessions.pending).toBe(rows.sessions);
    expect(used.documents).toBe(
      caller.documents + rows.memberships + rows.machines + rows.sessions,
    );
    expect(used.ranges).toBe(caller.ranges + 5);
    expect(used.documents / limit.documents).toBeLessThan(0.1);
    expect(used.bytes / limit.bytes).toBeLessThan(0.1);
    expect(used.ranges / limit.ranges).toBeLessThan(0.1);

    // A centre's own roles read a fraction of it.
    for (const actor of ["orgAdmin", "manager", "patient"] as const) {
      const centre = await counters(w.t, actor);
      expect(centre.used.documents).toBeLessThan(used.documents / 10);
      expect(centre.used.bytes).toBeLessThan(used.bytes / 10);
    }
  });
});

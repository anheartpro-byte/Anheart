/// <reference types="vite/client" />
/**
 * Shared setup for the Convex authorization tests (ANH-132).
 *
 * Everything runs in memory with `convex-test`; no deployment is ever touched.
 * The file name has two dots (`test.setup.ts`) so the Convex bundler skips it as
 * a deployable module, the same convention the `*.fixtures.ts` files rely on.
 *
 * Roles that exist in the code today: "admin", "gestionnaire", "user", plus the
 * anonymous (unauthenticated) caller. Multi-organisation (ANH-114) is not built
 * yet, so there is no organisation dimension in the schema. The factories and
 * the actor list are shaped so an organisation can be added later as one more
 * dimension (a second `otherOrg*` set of actors and a column in the matrix)
 * without rewriting the tests.
 */
import { convexTest } from "convex-test";
import schema from "./schema";
import { api } from "./_generated/api";
import type { Id } from "./_generated/dataModel";

/** Fixed clock so every expectation is deterministic (2027-01-15 UTC). */
export const NOW = 1_800_000_000_000;

/** The glob convex-test needs: every Convex module, no test/fixture/setup/matrix. */
export const modules = import.meta.glob([
  "./**/*.ts",
  "!./**/*.test.ts",
  "!./**/*.fixtures.ts",
  "!./**/*.setup.ts",
  "!./**/*.matrix.ts",
]);

/**
 * Named actors encode role AND relationship, which is exactly the ownership /
 * assignment dimension the matrix needs ("their own resource" vs "someone
 * else's"). A future organisation dimension adds more named actors here.
 */
export type Actor =
  | "anonymous"
  | "admin"
  | "manager" // gestionnaire of `machine` and of `patient`
  | "otherManager" // gestionnaire of `otherMachine` and of `otherPatient`
  | "patient" // user managed by `manager`, launch right on `machine`
  | "otherPatient" // user managed by `otherManager`, launch right on `otherMachine`
  | "stranger"; // user with no relationship to anything

export type Role = "admin" | "gestionnaire" | "user";

export const ROLE_OF: Record<Actor, Role | null> = {
  anonymous: null,
  admin: "admin",
  manager: "gestionnaire",
  otherManager: "gestionnaire",
  patient: "user",
  otherPatient: "user",
  stranger: "user",
};

export const USER_ACTORS = [
  "admin",
  "manager",
  "otherManager",
  "patient",
  "otherPatient",
  "stranger",
] as const satisfies readonly Actor[];

export type World = Awaited<ReturnType<typeof seedWorld>>;

/** A `t` scoped to the actor's Clerk identity, or anonymous for the raw `t`. */
export function as(t: World["t"], actor: Actor) {
  return actor === "anonymous" ? t : t.withIdentity({ subject: actor });
}

const MACHINE_CONFIG = {
  sampleRate: 1000,
  channels: ["ECG"],
  batchInterval: 1000,
} as const;

/**
 * One organisation's worth of accounts, machines and relationships. Two
 * managers with disjoint patients and machines let every ownership cell be
 * expressed as `manager` vs `otherManager` and `patient` vs `otherPatient`.
 *
 * Invariant kept on purpose: `machine` has NO pending session, so it is a clean
 * target for `launchAutoSession`. Functions that need a pending/active session
 * create their own in their `setup` hook.
 */
export async function seedWorld(
  mods: Record<string, () => Promise<unknown>>,
) {
  const t = convexTest(schema, mods);
  const ids = await t.run(async (ctx) => {
    const mkUser = (name: Actor, role: Role) =>
      ctx.db.insert("users", {
        clerkId: name,
        role,
        firstName: name,
        lastName: "Synthetic",
        email: `${name}@example.invalid`,
        language: "en" as const,
        createdAt: NOW,
      });

    const admin = await mkUser("admin", "admin");
    const manager = await mkUser("manager", "gestionnaire");
    const otherManager = await mkUser("otherManager", "gestionnaire");
    const patient = await mkUser("patient", "user");
    const otherPatient = await mkUser("otherPatient", "user");
    const stranger = await mkUser("stranger", "user");

    // Physiology so an auto launch for `patient` passes the zone/age checks.
    await ctx.db.patch(patient, { hrMax: 180, birthYear: 1990 });
    await ctx.db.patch(otherPatient, { hrMax: 180, birthYear: 1990 });

    const mkMachine = () =>
      ctx.db.insert("machines", {
        name: "Synthetic machine",
        apiKey: "synthetic-hash",
        status: "online" as const,
        lastHeartbeat: NOW,
        config: { ...MACHINE_CONFIG, channels: ["ECG"] },
        createdAt: NOW,
        programsEnabled: true,
      });
    const machine = await mkMachine();
    const otherMachine = await mkMachine();

    await ctx.db.insert("machine_gestionnaires", {
      machineId: machine,
      gestionnaireId: manager,
      isOwner: true,
      createdAt: NOW,
      createdBy: admin,
    });
    await ctx.db.insert("machine_gestionnaires", {
      machineId: otherMachine,
      gestionnaireId: otherManager,
      isOwner: true,
      createdAt: NOW,
      createdBy: admin,
    });

    await ctx.db.insert("user_gestionnaires", {
      userId: patient,
      gestionnaireId: manager,
      createdAt: NOW,
      createdBy: admin,
    });
    await ctx.db.insert("user_gestionnaires", {
      userId: otherPatient,
      gestionnaireId: otherManager,
      createdAt: NOW,
      createdBy: admin,
    });

    await ctx.db.insert("machine_user_permissions", {
      machineId: machine,
      userId: patient,
      grantedBy: manager,
      createdAt: NOW,
    });
    await ctx.db.insert("machine_user_permissions", {
      machineId: otherMachine,
      userId: otherPatient,
      grantedBy: otherManager,
      createdAt: NOW,
    });

    const mkProfile = (machineId: Id<"machines">, profileId: string) =>
      ctx.db.insert("machine_profiles", {
        machineId,
        profileId,
        name: `Programme ${profileId}`,
        totalDurationS: 600,
        zoneLowBpm: 130,
        zoneHighBpm: 150,
        hardMaxBpm: 170,
        criticalBpm: 185,
        subjectHrMax: 190,
        minRunRpm: 20,
        maxRpm: 1500,
        storeRev: 1,
        updatedAt: NOW,
      });
    await mkProfile(machine, "p1");
    await mkProfile(otherMachine, "p1");

    return {
      admin,
      manager,
      otherManager,
      patient,
      otherPatient,
      stranger,
      machine,
      otherMachine,
      profileId: "p1",
    };
  });
  return { t, ...ids };
}

/**
 * A world for the machine HTTP routes: two machines each with a REAL credential
 * issued by `createMachine`, both online with programmes enabled, a patient
 * holding the launch right on the first machine, and a synced profile. Used by
 * `httpRoutes.test.ts`.
 */
export async function seedMachineWorld(
  mods: Record<string, () => Promise<unknown>>,
) {
  const t = convexTest(schema, mods);
  const adminId = await t.run((ctx) =>
    ctx.db.insert("users", {
      clerkId: "http-admin",
      role: "admin" as const,
      firstName: "Synthetic",
      lastName: "Admin",
      email: "http-admin@example.invalid",
      language: "en" as const,
      createdAt: NOW,
    }),
  );
  const admin = t.withIdentity({ subject: "http-admin" });
  const first = await admin.mutation(api.machines.createMachine, {
    name: "Synthetic machine one",
  });
  const second = await admin.mutation(api.machines.createMachine, {
    name: "Synthetic machine two",
  });
  const patient = await t.run(async (ctx) => {
    const id = await ctx.db.insert("users", {
      clerkId: "http-patient",
      role: "user" as const,
      firstName: "Synthetic",
      lastName: "Patient",
      email: "http-patient@example.invalid",
      language: "en" as const,
      createdAt: NOW,
      hrMax: 180,
      birthYear: 1990,
    });
    for (const machineId of [first.machineId, second.machineId]) {
      await ctx.db.patch(machineId, { status: "online", programsEnabled: true });
      await ctx.db.insert("machine_profiles", {
        machineId,
        profileId: "p1",
        name: "Programme p1",
        totalDurationS: 600,
        zoneLowBpm: 130,
        zoneHighBpm: 150,
        hardMaxBpm: 170,
        criticalBpm: 185,
        subjectHrMax: 190,
        minRunRpm: 20,
        maxRpm: 1500,
        storeRev: 1,
        updatedAt: NOW,
      });
    }
    await ctx.db.insert("machine_user_permissions", {
      machineId: first.machineId,
      userId: id,
      grantedBy: adminId,
      createdAt: NOW,
    });
    return id;
  });
  return {
    t,
    admin,
    adminId,
    patient,
    machine: first.machineId,
    machineKey: first.apiKey,
    otherMachine: second.machineId,
    otherKey: second.apiKey,
    profileId: "p1",
  };
}

/** Insert a session straight into the store (bypassing policy) for read/stop tests. */
export async function addSession(
  w: World,
  fields: {
    machineId: Id<"machines">;
    userId?: Id<"users">;
    status: "pending" | "active" | "completed" | "failed";
    kind?: "recording" | "auto" | "manual";
    startedById?: Id<"users">;
    stopRequestedAt?: number;
  },
): Promise<Id<"sessions">> {
  return await w.t.run((ctx) =>
    ctx.db.insert("sessions", {
      machineId: fields.machineId,
      userId: fields.userId,
      startedById: fields.startedById,
      status: fields.status,
      startedAt: NOW,
      channels: ["ECG"],
      sampleRate: 1000,
      kind: fields.kind,
      stopRequestedAt: fields.stopRequestedAt,
    }),
  );
}

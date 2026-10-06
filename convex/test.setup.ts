/// <reference types="vite/client" />
/**
 * Shared setup for the Convex authorization tests (ANH-132, ANH-114).
 *
 * Everything runs in memory with `convex-test`; no deployment is ever touched.
 * The file name has two dots (`test.setup.ts`) so the Convex bundler skips it as
 * a deployable module, the same convention the `*.fixtures.ts` files rely on.
 *
 * The world holds three organisations: Anheart (whose admin is the super-admin)
 * and two client centres, A and B. Every actor carries the organisation claims
 * (`org_id`, `org_role`) a Clerk token would carry for them, so the matrix runs
 * the way a configured deployment does. Centre A holds the actors of ANH-132;
 * centre B exists so that every function can be called from another
 * organisation, by direct identifier.
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

/** Clerk organisation identifiers of the synthetic world. */
export const ANHEART_CLERK_ORG = "org_synthetic_anheart";
export const ORG_A_CLERK_ORG = "org_synthetic_centre_a";
export const ORG_B_CLERK_ORG = "org_synthetic_centre_b";

/**
 * Named actors encode organisation, role AND relationship, which is exactly
 * the ownership / assignment dimension the matrix needs ("their own resource"
 * vs "someone else's", "their organisation" vs "another one").
 */
export type Actor =
  | "anonymous"
  | "admin" // admin of the Anheart organisation: every organisation
  // --- centre A
  | "orgAdmin" // admin of centre A
  | "manager" // gestionnaire of `machine` and of `patient`
  | "otherManager" // gestionnaire of `otherMachine` and of `otherPatient`
  | "patient" // user managed by `manager`, launch right on `machine`
  | "otherPatient" // user managed by `otherManager`, launch right on `otherMachine`
  | "stranger" // user with no relationship to anything
  // --- centre B
  | "orgBAdmin" // admin of centre B
  | "orgBManager" // gestionnaire of `orgBMachine` and of `orgBPatient`
  | "orgBPatient"; // user managed by `orgBManager`, launch right on `orgBMachine`

/** Role the actor acts with (`convex/lib/auth.ts`). */
export type Role = "admin" | "org_admin" | "gestionnaire" | "user";

type UserActor = Exclude<Actor, "anonymous">;

const ROLE_OF_USER: Record<UserActor, Role> = {
  admin: "admin",
  orgAdmin: "org_admin",
  manager: "gestionnaire",
  otherManager: "gestionnaire",
  patient: "user",
  otherPatient: "user",
  stranger: "user",
  orgBAdmin: "org_admin",
  orgBManager: "gestionnaire",
  orgBPatient: "user",
};

export const ROLE_OF: Record<Actor, Role | null> = {
  anonymous: null,
  ...ROLE_OF_USER,
};

/** The organisation each actor belongs to, by Clerk identifier. */
const CLERK_ORG_OF: Record<UserActor, string> = {
  admin: ANHEART_CLERK_ORG,
  orgAdmin: ORG_A_CLERK_ORG,
  manager: ORG_A_CLERK_ORG,
  otherManager: ORG_A_CLERK_ORG,
  patient: ORG_A_CLERK_ORG,
  otherPatient: ORG_A_CLERK_ORG,
  stranger: ORG_A_CLERK_ORG,
  orgBAdmin: ORG_B_CLERK_ORG,
  orgBManager: ORG_B_CLERK_ORG,
  orgBPatient: ORG_B_CLERK_ORG,
};

/** The role the actor holds in that organisation, as stored and as Clerk names it. */
const ORGANIZATION_ROLE_OF = {
  admin: "admin",
  org_admin: "admin",
  gestionnaire: "gestionnaire",
  user: "user",
} as const satisfies Record<Role, "admin" | "gestionnaire" | "user">;

const CLERK_ROLE_OF: Record<Role, string> = {
  admin: "org:admin",
  org_admin: "org:admin",
  gestionnaire: "org:gestionnaire",
  user: "org:patient",
};

export const USER_ACTORS = [
  "admin",
  "orgAdmin",
  "manager",
  "otherManager",
  "patient",
  "otherPatient",
  "stranger",
  "orgBAdmin",
  "orgBManager",
  "orgBPatient",
] as const satisfies readonly Actor[];

/** Actors of centre B: every one of them is foreign to centre A's resources. */
export const ORG_B_ACTORS = [
  "orgBAdmin",
  "orgBManager",
  "orgBPatient",
] as const satisfies readonly Actor[];

export type World = Awaited<ReturnType<typeof seedWorld>>;

/** Extra JWT claims a cell adds to its actor's identity (e.g. `email`). */
export type Claims = Parameters<World["t"]["withIdentity"]>[0];

/**
 * The claims that say who the caller is and in which organisation they act: a
 * cell never sets them itself.
 */
const IDENTITY_CLAIMS = [
  "subject",
  "issuer",
  "tokenIdentifier",
  "org_id",
  "org_role",
] as const;

/** The organisation claims a Clerk token carries for this actor. */
export function organizationClaims(actor: UserActor) {
  return {
    org_id: CLERK_ORG_OF[actor],
    org_role: CLERK_ROLE_OF[ROLE_OF_USER[actor]],
  };
}

/**
 * Declare whether Clerk Organizations is configured for the deployment under
 * test: the Clerk identifier of the Anheart organisation, or null for a
 * deployment that has not been configured yet.
 */
export function configureAnheartOrganization(clerkOrgId: string | null) {
  if (clerkOrgId === null) delete process.env.ANHEART_ORG_ID;
  else process.env.ANHEART_ORG_ID = clerkOrgId;
}

/**
 * A `t` scoped to the actor's Clerk identity, or anonymous for the raw `t`.
 *
 * `claims` adds JWT claims to that identity. The subject and the organisation
 * are always the actor's: claims cannot replace them, and the anonymous actor
 * carries no claim at all.
 */
export function as(t: World["t"], actor: Actor, claims?: Claims | null) {
  if (actor === "anonymous") {
    if (claims) throw new Error("The anonymous actor carries no claims");
    return t;
  }
  if (claims && IDENTITY_CLAIMS.some((claim) => claim in claims)) {
    throw new Error("Claims cannot replace the actor's identity");
  }
  return t.withIdentity({
    ...claims,
    ...organizationClaims(actor),
    subject: actor,
  });
}

const ORGANIZATION_SETTINGS = {
  requirePrescription: false,
  language: "fr",
} as const;

/**
 * Three organisations' worth of accounts, machines and relationships.
 *
 * Centre A: two managers with disjoint patients and machines let every
 * ownership cell be expressed as `manager` vs `otherManager` and `patient` vs
 * `otherPatient`. Centre B mirrors it with one manager, one patient and one
 * machine. Anheart holds the super-admin only.
 *
 * Invariant kept on purpose: `machine` has NO pending session, so it is a clean
 * target for `launchAutoSession`. Functions that need a pending/active session
 * create their own in their `setup` hook.
 */
export async function seedWorld(mods: Record<string, () => Promise<unknown>>) {
  configureAnheartOrganization(ANHEART_CLERK_ORG);
  const t = convexTest(schema, mods);
  const ids = await t.run(async (ctx) => {
    const mkOrganization = (clerkOrgId: string, name: string, slug: string) =>
      ctx.db.insert("organizations", {
        clerkOrgId,
        name,
        slug,
        createdAt: NOW,
        settings: { ...ORGANIZATION_SETTINGS },
      });
    const anheartOrg = await mkOrganization(
      ANHEART_CLERK_ORG,
      "Anheart",
      "anheart",
    );
    const orgA = await mkOrganization(ORG_A_CLERK_ORG, "Centre A", "centre-a");
    const orgB = await mkOrganization(ORG_B_CLERK_ORG, "Centre B", "centre-b");
    const organizationOf: Record<string, Id<"organizations">> = {
      [ANHEART_CLERK_ORG]: anheartOrg,
      [ORG_A_CLERK_ORG]: orgA,
      [ORG_B_CLERK_ORG]: orgB,
    };

    const mkUser = async (name: UserActor) => {
      const organizationId = organizationOf[CLERK_ORG_OF[name]];
      const role = ORGANIZATION_ROLE_OF[ROLE_OF_USER[name]];
      const userId = await ctx.db.insert("users", {
        clerkId: name,
        role,
        organizationId,
        firstName: name,
        lastName: "Synthetic",
        email: `${name}@example.invalid`,
        language: "en" as const,
        createdAt: NOW,
      });
      await ctx.db.insert("memberships", {
        userId,
        organizationId,
        role,
        active: true,
      });
      return userId;
    };

    const admin = await mkUser("admin");
    const orgAdmin = await mkUser("orgAdmin");
    const manager = await mkUser("manager");
    const otherManager = await mkUser("otherManager");
    const patient = await mkUser("patient");
    const otherPatient = await mkUser("otherPatient");
    const stranger = await mkUser("stranger");
    const orgBAdmin = await mkUser("orgBAdmin");
    const orgBManager = await mkUser("orgBManager");
    const orgBPatient = await mkUser("orgBPatient");

    // Physiology so an auto launch for a patient passes the zone/age checks.
    for (const rider of [patient, otherPatient, orgBPatient]) {
      await ctx.db.patch(rider, { hrMax: 180, birthYear: 1990 });
    }

    const mkMachine = (organizationId: Id<"organizations">) =>
      ctx.db.insert("machines", {
        organizationId,
        name: "Synthetic machine",
        apiKey: "synthetic-hash",
        status: "online" as const,
        lastHeartbeat: NOW,
        createdAt: NOW,
        programsEnabled: true,
      });
    const machine = await mkMachine(orgA);
    const otherMachine = await mkMachine(orgA);
    const orgBMachine = await mkMachine(orgB);

    const mkProfile = (
      organizationId: Id<"organizations">,
      machineId: Id<"machines">,
      profileId: string,
    ) =>
      ctx.db.insert("machine_profiles", {
        organizationId,
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

    // One manager, one patient, one machine: linked, with the launch right.
    for (const [organizationId, machineId, gestionnaireId, userId] of [
      [orgA, machine, manager, patient],
      [orgA, otherMachine, otherManager, otherPatient],
      [orgB, orgBMachine, orgBManager, orgBPatient],
    ] as const) {
      await ctx.db.insert("machine_gestionnaires", {
        organizationId,
        machineId,
        gestionnaireId,
        isOwner: true,
        createdAt: NOW,
        createdBy: admin,
      });
      await ctx.db.insert("user_gestionnaires", {
        organizationId,
        userId,
        gestionnaireId,
        createdAt: NOW,
        createdBy: admin,
      });
      await ctx.db.insert("machine_user_permissions", {
        organizationId,
        machineId,
        userId,
        grantedBy: gestionnaireId,
        createdAt: NOW,
      });
      await mkProfile(organizationId, machineId, "p1");
    }

    return {
      anheartOrg,
      orgA,
      orgB,
      admin,
      orgAdmin,
      manager,
      otherManager,
      patient,
      otherPatient,
      stranger,
      orgBAdmin,
      orgBManager,
      orgBPatient,
      machine,
      otherMachine,
      orgBMachine,
      profileId: "p1",
    };
  });
  return { t, ...ids };
}

/**
 * A world for the machine HTTP routes: two machines each with a REAL credential
 * issued by `createMachine`, both online with programmes enabled, a patient
 * holding the launch right on the first machine, and a synced profile. Used by
 * `httpRoutes.test.ts`. Everything lives in the Anheart organisation.
 */
export async function seedMachineWorld(
  mods: Record<string, () => Promise<unknown>>,
) {
  configureAnheartOrganization(ANHEART_CLERK_ORG);
  const t = convexTest(schema, mods);
  const { adminId, organizationId } = await t.run(async (ctx) => {
    const organizationId = await ctx.db.insert("organizations", {
      clerkOrgId: ANHEART_CLERK_ORG,
      name: "Anheart",
      slug: "anheart",
      createdAt: NOW,
      settings: { ...ORGANIZATION_SETTINGS },
    });
    const adminId = await ctx.db.insert("users", {
      clerkId: "http-admin",
      role: "admin" as const,
      organizationId,
      firstName: "Synthetic",
      lastName: "Admin",
      email: "http-admin@example.invalid",
      language: "en" as const,
      createdAt: NOW,
    });
    await ctx.db.insert("memberships", {
      userId: adminId,
      organizationId,
      role: "admin",
      active: true,
    });
    return { adminId, organizationId };
  });
  const admin = t.withIdentity({
    subject: "http-admin",
    org_id: ANHEART_CLERK_ORG,
    org_role: "org:admin",
  });
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
      organizationId,
      firstName: "Synthetic",
      lastName: "Patient",
      email: "http-patient@example.invalid",
      language: "en" as const,
      createdAt: NOW,
      hrMax: 180,
      birthYear: 1990,
    });
    await ctx.db.insert("memberships", {
      userId: id,
      organizationId,
      role: "user",
      active: true,
    });
    for (const machineId of [first.machineId, second.machineId]) {
      await ctx.db.patch(machineId, {
        status: "online",
        programsEnabled: true,
      });
      await ctx.db.insert("machine_profiles", {
        organizationId,
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
      organizationId,
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
    organizationId,
    patient,
    machine: first.machineId,
    machineKey: first.apiKey,
    otherMachine: second.machineId,
    otherKey: second.apiKey,
    profileId: "p1",
  };
}

/**
 * A deployment as it was before organisations existed (ANH-114): one admin, one
 * gestionnaire with a patient and a machine, a session with telemetry. No row
 * carries an organisation, no token carries an organisation claim, and Clerk
 * Organizations is not configured (`ANHEART_ORG_ID` unset). The migration has
 * NOT run: tests run it themselves.
 */
export async function seedLegacyWorld(
  mods: Record<string, () => Promise<unknown>>,
) {
  configureAnheartOrganization(null);
  const t = convexTest(schema, mods);
  const ids = await t.run(async (ctx) => {
    const mkUser = (clerkId: string, role: "admin" | "gestionnaire" | "user") =>
      ctx.db.insert("users", {
        clerkId,
        role,
        firstName: clerkId,
        lastName: "Legacy",
        email: `${clerkId}@example.invalid`,
        language: "fr" as const,
        createdAt: NOW,
      });
    const admin = await mkUser("legacy-admin", "admin");
    const manager = await mkUser("legacy-manager", "gestionnaire");
    const patient = await mkUser("legacy-patient", "user");
    await ctx.db.patch(patient, { hrMax: 180, birthYear: 1990 });
    const machine = await ctx.db.insert("machines", {
      name: "Legacy machine",
      apiKey: "synthetic-hash",
      status: "online" as const,
      lastHeartbeat: NOW,
      createdAt: NOW,
      programsEnabled: true,
    });
    const machineLink = await ctx.db.insert("machine_gestionnaires", {
      machineId: machine,
      gestionnaireId: manager,
      isOwner: true,
      createdAt: NOW,
      createdBy: admin,
    });
    const patientLink = await ctx.db.insert("user_gestionnaires", {
      userId: patient,
      gestionnaireId: manager,
      createdAt: NOW,
      createdBy: admin,
    });
    const launchRight = await ctx.db.insert("machine_user_permissions", {
      machineId: machine,
      userId: patient,
      grantedBy: manager,
      createdAt: NOW,
    });
    const profile = await ctx.db.insert("machine_profiles", {
      machineId: machine,
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
    const session = await ctx.db.insert("sessions", {
      machineId: machine,
      userId: patient,
      status: "completed" as const,
      startedAt: NOW,
      endedAt: NOW + 600_000,
      channels: ["ECG"],
      kind: "auto" as const,
    });
    const telemetry = [];
    for (let second = 0; second < 5; second++) {
      telemetry.push(
        await ctx.db.insert("training_telemetry", {
          sessionId: session,
          machineId: machine,
          t: NOW + second * 1000,
          elapsedS: second,
          phase: "hold",
          bpm: 140,
          motorRpm: 1000,
          outputRpm: 20,
          setpointMotorRpm: 1000,
          gLoad: 1.1,
          safetyAction: "none",
        }),
      );
    }
    return {
      admin,
      manager,
      patient,
      machine,
      machineLink,
      patientLink,
      launchRight,
      profile,
      session,
      telemetry,
    };
  });
  return { t, ...ids };
}

export type LegacyWorld = Awaited<ReturnType<typeof seedLegacyWorld>>;

/**
 * Insert a session straight into the store (bypassing policy) for read/stop
 * tests. Like every session, it belongs to its machine's organisation.
 * Without `kind`, the row is a session of the retired ECG recording mode: that
 * mode never wrote the field.
 */
export async function addSession(
  w: Pick<World, "t">,
  fields: {
    machineId: Id<"machines">;
    userId?: Id<"users">;
    status: "pending" | "active" | "completed" | "failed";
    kind?: "auto" | "manual";
    startedById?: Id<"users">;
    stopRequestedAt?: number;
  },
): Promise<Id<"sessions">> {
  return await w.t.run(async (ctx) => {
    const machine = await ctx.db.get(fields.machineId);
    return await ctx.db.insert("sessions", {
      organizationId: machine?.organizationId,
      machineId: fields.machineId,
      userId: fields.userId,
      startedById: fields.startedById,
      status: fields.status,
      startedAt: NOW,
      channels: ["ECG"],
      sampleRate: 1000,
      kind: fields.kind,
      stopRequestedAt: fields.stopRequestedAt,
    });
  });
}

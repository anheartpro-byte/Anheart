import type { Doc, Id } from "./_generated/dataModel";
import {
  PROGRAMME_MEDICAL_VERSION,
  PROGRAMME_SCHEMA_VERSION,
  type ProgrammeProfile,
} from "./lib/programmes";
import { registryUpload } from "./machineRegistry.fixtures";
import { ANHEART_CLERK_ORG, NOW, type World } from "./test.setup";

/** The programme of ANH-137's acceptance criterion: jog 145-155, 30 min. */
export const JOG: ProgrammeProfile = {
  profileId: "jog_145_155_30_min",
  name: "jog 145-155, 30 min",
  totalDurationS: 1800,
  baselineS: 180,
  warmupMaxS: 300,
  holdMinS: 300,
  cooldownS: 240,
  recoveryS: 300,
  zoneLowBpm: 145,
  zoneHighBpm: 155,
  hardMaxBpm: 165,
  criticalBpm: 175,
  subjectHrMax: 175,
  minRunRpm: 55,
  maxRpm: 276,
  warmupRpmCeilingFraction: 0.6,
  channels: ["ECG"],
  allowAboveNameplate: false,
};

export const SIGNOFF = {
  by: "Dr Synthetic",
  at: Date.UTC(2026, 9, 1),
  note: "synthetic sign-off",
};

/** A second admin of the Anheart organisation, clerk id `secondAdmin`. */
export async function addSecondAdmin(w: World): Promise<Id<"users">> {
  return await w.t.run(async (ctx) => {
    const userId = await ctx.db.insert("users", {
      clerkId: "secondAdmin",
      role: "admin",
      organizationId: w.anheartOrg,
      firstName: "secondAdmin",
      lastName: "Synthetic",
      email: "secondAdmin@example.invalid",
      language: "en",
      createdAt: NOW,
    });
    await ctx.db.insert("memberships", {
      userId,
      organizationId: w.anheartOrg,
      role: "admin",
      active: true,
    });
    return userId;
  });
}

export function asSecondAdmin(w: World) {
  return w.t.withIdentity({
    subject: "secondAdmin",
    org_id: ANHEART_CLERK_ORG,
    org_role: "org:admin",
  });
}

/**
 * A catalogue programme in `status`, written by `author`. A draft carries a
 * simulation report, so that another admin can validate it at once.
 */
export async function insertProgramme(
  w: World,
  status: Doc<"programmes">["status"],
  author: Id<"users">,
  profile: Partial<ProgrammeProfile> = {},
): Promise<Id<"programmes">> {
  const simulationReport = await registryUpload(w);
  const validated = status !== "draft";
  return await w.t.run((ctx) =>
    ctx.db.insert("programmes", {
      ...JOG,
      ...profile,
      organizationId: null,
      status,
      schemaVersion: PROGRAMME_SCHEMA_VERSION,
      revision: status === "published" || status === "retired" ? 1 : 0,
      medicalParametersVersion: PROGRAMME_MEDICAL_VERSION,
      medicalSignoff: validated ? SIGNOFF : null,
      simulationReport,
      validatedBy: validated ? w.admin : null,
      createdBy: author,
      updatedBy: author,
      createdAt: NOW,
      updatedAt: NOW,
    }),
  );
}

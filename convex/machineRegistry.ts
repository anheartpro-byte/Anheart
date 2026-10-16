/**
 * ANH-147: authorised machine registry reads and administrator evidence writes.
 * Storage URLs and session-derived alerts stay within the reader's access.
 */
import { v, type Infer } from "convex/values";
import { audit, userActor } from "./lib/audit";
import type { Doc, Id } from "./_generated/dataModel";
import {
  mutation,
  query,
  type MutationCtx,
  type QueryCtx,
} from "./_generated/server";
import {
  getCurrentUserOrThrow,
  organizationRoleOf,
  requireRole,
  type CurrentUser,
} from "./lib/auth";
import { refusal } from "./lib/errors";
import { limitUser } from "./lib/rateLimits";
import {
  configurationLockVerdict,
  downgradeRefusal,
  effectiveValidationLevel,
  levelAt,
  MEDICAL_KEY_IS_TEST,
  personOnBoard,
  radiiProposal,
  reviewRefusal,
  validationRank,
  versionStatus,
} from "./lib/machineRegistry";
import {
  isReleaseVersion,
  releaseAllowedOnMachine,
} from "./lib/releaseValidation";
import {
  driveParameterSheetValidator,
  machineRegistryIdentityFields,
  machineRegistryRadiiFields,
  machineReviewLevelValidator,
  machineValidationLevelValidator,
  machineValidationValidator,
} from "./schema";
import { ARMING_REFUSAL_CODES } from "./training";

const configurationKeyValidator = v.union(
  v.literal("PROGRAMS_ENABLED"),
  v.literal("OCCUPANCY_OCCUPIED_ENABLED"),
);
const lockVerdictValidator = v.object({
  allowed: v.boolean(),
  requiredLevel: machineValidationLevelValidator,
  reachedLevel: machineValidationLevelValidator,
  message: v.union(v.string(), v.null()),
});
const versionStatusValidator = v.union(
  v.literal("unknown"),
  v.literal("not_validated"),
  v.literal("up_to_date"),
  v.literal("outdated"),
);
const alertCodeValidator = v.union(
  v.literal("programs_enabled_without_auto"),
  v.literal("person_on_board_without_occupied"),
);
const authorValidator = v.object({
  userId: v.id("users"),
  firstName: v.optional(v.string()),
  lastName: v.optional(v.string()),
});
const sheetWithUrlValidator = driveParameterSheetValidator
  .omit("attachedBy")
  .extend({
    attachedBy: authorValidator,
    url: v.union(v.string(), v.null()),
  });
const emptyValidation: Infer<typeof machineValidationValidator> = {
  level: "none",
  reviews: [],
  downgrades: [],
};

async function registryOf(ctx: QueryCtx, machineId: Id<"machines">) {
  return await ctx.db
    .query("machine_registry")
    .withIndex("by_machine", (q) => q.eq("machineId", machineId))
    .unique();
}

function canRead(user: CurrentUser, machine: Doc<"machines">): boolean {
  return (
    user.role === "admin" ||
    (user.role === "org_admin" &&
      user.organizationId === machine.organizationId &&
      !machine.isDeleted)
  );
}

async function readableMachine(ctx: QueryCtx, machineId: Id<"machines">) {
  const user = await getCurrentUserOrThrow(ctx);
  const machine = await ctx.db.get(machineId);
  return machine !== null && canRead(user, machine) ? { user, machine } : null;
}

async function contactOf(
  ctx: QueryCtx,
  userId: Id<"users">,
  organizationId: Id<"organizations"> | undefined,
) {
  const contact = await ctx.db.get(userId);
  if (contact === null || contact.clerkDeletedAt !== undefined) return null;
  const role = await organizationRoleOf(ctx, userId, organizationId);
  return role === null
    ? null
    : {
        userId,
        firstName: contact.firstName,
        lastName: contact.lastName,
        role,
      };
}

/** Lists active contacts in the machine's current organisation for Anheart administrators. */
export const listContactCandidates = query({
  args: { machineId: v.id("machines") },
  returns: v.array(
    v.object({
      userId: v.id("users"),
      firstName: v.string(),
      lastName: v.string(),
      role: v.union(
        v.literal("admin"),
        v.literal("gestionnaire"),
        v.literal("user"),
      ),
    }),
  ),
  handler: async (ctx, args) => {
    await requireRole(ctx, ["admin"]);
    const machine = await ctx.db.get(args.machineId);
    if (machine === null || machine.isDeleted)
      throw refusal("machine_not_found");
    const organizationId = machine.organizationId;
    if (organizationId === undefined) return [];
    const memberships = await ctx.db
      .query("memberships")
      .withIndex("by_organization", (q) =>
        q.eq("organizationId", organizationId),
      )
      .collect();
    const contacts = [];
    for (const userId of new Set(
      memberships
        .filter((member) => member.active)
        .map((member) => member.userId),
    )) {
      const contact = await contactOf(ctx, userId, machine.organizationId);
      if (contact !== null) contacts.push(contact);
    }
    return contacts.sort(
      (a, b) =>
        a.lastName.localeCompare(b.lastName) ||
        a.firstName.localeCompare(b.firstName) ||
        a.userId.localeCompare(b.userId),
    );
  },
});

async function editableMachine(ctx: MutationCtx, machineId: Id<"machines">) {
  await limitUser(ctx);
  const admin = await requireRole(ctx, ["admin"]);
  const machine = await ctx.db.get(machineId);
  if (machine === null || machine.isDeleted) throw refusal("machine_not_found");
  return { admin, machine, registry: await registryOf(ctx, machineId) };
}

async function saveRegistry(
  ctx: MutationCtx,
  state: Awaited<ReturnType<typeof editableMachine>>,
  fields: Partial<Omit<Doc<"machine_registry">, "_id" | "_creationTime">>,
) {
  const updated = {
    organizationId: state.machine.organizationId,
    updatedAt: Date.now(),
    updatedBy: state.admin._id,
  };
  if (state.registry === null) {
    await ctx.db.insert("machine_registry", {
      machineId: state.machine._id,
      contacts: [],
      driveParameterSheetHistory: [],
      validation: emptyValidation,
      ...fields,
      ...updated,
    });
  } else {
    await ctx.db.patch(state.registry._id, { ...fields, ...updated });
  }
  return null;
}

function text(value: string, max = 200) {
  const trimmed = value.trim();
  if (trimmed.length === 0 || trimmed.length > max)
    throw refusal("invalid_registry_field");
  return trimmed;
}

function timestamp(value: number) {
  if (
    !Number.isSafeInteger(value) ||
    value < Date.UTC(2024, 0, 1) ||
    value > Date.now() + 86_400_000
  )
    throw refusal("invalid_registry_field");
  return value;
}

async function piRelease(ctx: QueryCtx, version: string) {
  return await ctx.db
    .query("software_releases")
    .withIndex("by_component_and_version", (q) =>
      q.eq("component", "pi").eq("version", version),
    )
    .unique();
}

async function requirePiVersion(ctx: QueryCtx, version: string) {
  const normalized = version.trim();
  if (
    !isReleaseVersion("pi", normalized) ||
    (await piRelease(ctx, normalized)) === null
  )
    throw refusal("unknown_software_version");
  return normalized;
}

/** Accepts fresh PDF/JSON uploads only once across current and historical registry evidence. */
async function registryFile(
  ctx: MutationCtx,
  storageId: Id<"_storage">,
): Promise<"application/pdf" | "application/json"> {
  const file = await ctx.db.system.get(storageId);
  if (
    file === null ||
    (file.contentType !== "application/pdf" &&
      file.contentType !== "application/json") ||
    file.size > 10 * 1024 * 1024 ||
    Date.now() - file._creationTime >= 3_600_000
  )
    throw refusal("invalid_registry_file");
  const registries = await ctx.db.query("machine_registry").collect();
  if (
    registries.some(
      (registry) =>
        registry.driveParameterSheet?.storageId === storageId ||
        registry.driveParameterSheetHistory.some(
          (sheet) => sheet.storageId === storageId,
        ) ||
        registry.validation.reviews.some(
          (review) => review.reportStorageId === storageId,
        ),
    )
  )
    throw refusal("invalid_registry_file");
  return file.contentType;
}

async function statusOf(
  ctx: QueryCtx,
  machine: Doc<"machines">,
  registry: Doc<"machine_registry"> | null,
) {
  const reported = machine.softwareVersion;
  const expected = registry?.expectedSoftwareVersion;
  const [reportedRelease, expectedRelease] = await Promise.all([
    reported === undefined ? null : piRelease(ctx, reported),
    expected === undefined ? null : piRelease(ctx, expected),
  ]);
  const machineLevel = effectiveValidationLevel(
    registry?.validation,
    Date.now(),
  );
  return {
    versionStatus: versionStatus({
      reported,
      expected,
      reportedReleaseLevel: reportedRelease?.validationLevel,
      machineLevel,
    }),
    expectedVersionAllowed:
      expected === undefined
        ? null
        : releaseAllowedOnMachine(
            expectedRelease?.validationLevel,
            machineLevel,
          ),
  };
}

/** Counts readable sessions conservatively, excluding only known pre-start failures. */
async function alertsOf(
  ctx: QueryCtx,
  { machine, user }: { machine: Doc<"machines">; user: CurrentUser },
  registry: Doc<"machine_registry"> | null,
) {
  const validation = registry?.validation ?? emptyValidation;
  const level = effectiveValidationLevel(validation, Date.now());
  const coherent = validation.level === levelAt(validation, Date.now());
  const alerts: Array<{
    code: Infer<typeof alertCodeValidator>;
    count: number;
    mostRecentAt: number | null;
  }> = [];
  if (
    machine.programsEnabled === true &&
    validationRank(level) < validationRank("auto")
  )
    alerts.push({
      code: "programs_enabled_without_auto",
      count: 1,
      mostRecentAt: null,
    });
  const query = ctx.db
    .query("sessions")
    .withIndex("by_machine_and_started", (q) => q.eq("machineId", machine._id))
    .order("desc");
  // Apply read access before the limit so foreign sessions cannot hide readable ones.
  const readable =
    user.role === "admin"
      ? query
      : query.filter((q) =>
          q.and(
            q.neq(q.field("organizationId"), undefined),
            q.eq(q.field("organizationId"), user.organizationId),
          ),
        );
  const sessions = await readable.take(50);
  const unvalidated = sessions.filter((session) => {
    if (session.status === "pending") return false;
    // Exclude only known pre-start endings; every other ending counts (fail closed).
    if (
      session.status === "failed" &&
      (ARMING_REFUSAL_CODES.some((code) => code === session.endCode) ||
        (session.endCode === undefined &&
          (session.endReason?.startsWith("Cancelled before start by") ||
            session.endReason?.startsWith("refusee par la machine : "))))
    )
      return false;
    return (
      personOnBoard(session) &&
      validationRank(
        coherent ? levelAt(validation, session.startedAt) : "none",
      ) < validationRank("occupied")
    );
  });
  if (unvalidated.length > 0)
    alerts.push({
      code: "person_on_board_without_occupied",
      count: unvalidated.length,
      mostRecentAt: unvalidated[0].startedAt,
    });
  return alerts;
}

/** Returns the authorised record, resolved authors, effective decisions and readable alerts. */
export const getRegistry = query({
  args: { machineId: v.id("machines") },
  returns: v.union(
    v.null(),
    v.object({
      machineId: v.id("machines"),
      organizationId: v.optional(v.id("organizations")),
      ...machineRegistryIdentityFields,
      ...machineRegistryRadiiFields,
      contacts: v.array(
        v.object({
          userId: v.id("users"),
          firstName: v.string(),
          lastName: v.string(),
        }),
      ),
      driveParameterSheet: v.union(sheetWithUrlValidator, v.null()),
      driveParameterSheetHistory: v.array(sheetWithUrlValidator),
      validation: machineValidationValidator
        .omit("reviews", "downgrades")
        .extend({
          reviews: v.array(
            machineValidationValidator.fields.reviews.element
              .omit("by")
              .extend({
                by: authorValidator,
                reportUrl: v.union(v.string(), v.null()),
              }),
          ),
          downgrades: v.array(
            machineValidationValidator.fields.downgrades.element
              .omit("by")
              .extend({ by: authorValidator }),
          ),
        }),
      effectiveLevel: machineValidationLevelValidator,
      updatedAt: v.union(v.number(), v.null()),
      updatedBy: v.union(authorValidator, v.null()),
      reportedSoftwareVersion: v.union(v.string(), v.null()),
      versionStatus: versionStatusValidator,
      expectedVersionAllowed: v.union(v.boolean(), v.null()),
      configurationLocks: v.object({
        PROGRAMS_ENABLED: lockVerdictValidator,
        OCCUPANCY_OCCUPIED_ENABLED: lockVerdictValidator,
      }),
      configurationStatus: v.literal("unavailable"),
      configurationProposal: v.object({
        ARM_RADIUS_M: v.union(v.number(), v.null()),
        LEG_TIP_RADIUS_M: v.union(v.number(), v.null()),
      }),
      alerts: v.array(
        v.object({
          code: alertCodeValidator,
          count: v.number(),
          mostRecentAt: v.union(v.number(), v.null()),
        }),
      ),
      medicalKeyIsTest: v.boolean(),
      canEdit: v.boolean(),
    }),
  ),
  handler: async (ctx, args) => {
    const access = await readableMachine(ctx, args.machineId);
    if (access === null) return null;
    const { machine, user } = access;
    const registry = await registryOf(ctx, machine._id);
    const validation = registry?.validation ?? emptyValidation;
    const effectiveLevel = effectiveValidationLevel(validation, Date.now());
    const author = async (userId: Id<"users">) => {
      const account = await ctx.db.get(userId);
      return account === null || account.clerkDeletedAt !== undefined
        ? { userId }
        : { userId, firstName: account.firstName, lastName: account.lastName };
    };
    const withUrl = async (
      sheet: Infer<typeof driveParameterSheetValidator>,
    ) => ({
      ...sheet,
      attachedBy: await author(sheet.attachedBy),
      url: await ctx.storage.getUrl(sheet.storageId),
    });
    const contacts = [];
    for (const userId of registry?.contacts ?? []) {
      const contact = await contactOf(ctx, userId, machine.organizationId);
      if (contact !== null)
        contacts.push({
          userId,
          firstName: contact.firstName,
          lastName: contact.lastName,
        });
    }
    return {
      machineId: machine._id,
      organizationId: machine.organizationId,
      serialPi: registry?.serialPi,
      serialDrive: registry?.serialDrive,
      serialMotor: registry?.serialMotor,
      serialGearbox: registry?.serialGearbox,
      bitalinoAddress: registry?.bitalinoAddress,
      commissionedAt: registry?.commissionedAt,
      site: registry?.site,
      expectedSoftwareVersion: registry?.expectedSoftwareVersion,
      armRadiusM: registry?.armRadiusM,
      legTipRadiusM: registry?.legTipRadiusM,
      radiiMeasuredAt: registry?.radiiMeasuredAt,
      radiiMeasuredBy: registry?.radiiMeasuredBy,
      contacts,
      driveParameterSheet:
        registry?.driveParameterSheet === undefined
          ? null
          : await withUrl(registry.driveParameterSheet),
      driveParameterSheetHistory: await Promise.all(
        (registry?.driveParameterSheetHistory ?? []).map(withUrl),
      ),
      validation: {
        ...validation,
        reviews: await Promise.all(
          validation.reviews.map(async (review) => ({
            ...review,
            by: await author(review.by),
            reportUrl: await ctx.storage.getUrl(review.reportStorageId),
          })),
        ),
        downgrades: await Promise.all(
          validation.downgrades.map(async (entry) => ({
            ...entry,
            by: await author(entry.by),
          })),
        ),
      },
      effectiveLevel,
      updatedAt: registry?.updatedAt ?? null,
      updatedBy: registry === null ? null : await author(registry.updatedBy),
      reportedSoftwareVersion: machine.softwareVersion ?? null,
      ...(await statusOf(ctx, machine, registry)),
      configurationLocks: {
        PROGRAMS_ENABLED: configurationLockVerdict(
          effectiveLevel,
          "PROGRAMS_ENABLED",
          true,
        ),
        OCCUPANCY_OCCUPIED_ENABLED: configurationLockVerdict(
          effectiveLevel,
          "OCCUPANCY_OCCUPIED_ENABLED",
          true,
        ),
      },
      configurationStatus: "unavailable" as const,
      configurationProposal: radiiProposal(registry),
      alerts: await alertsOf(ctx, access, registry),
      medicalKeyIsTest: MEDICAL_KEY_IS_TEST,
      canEdit: user.role === "admin" && machine.isDeleted !== true,
    };
  },
});

/** Lists effective level and version badges without reading session alerts. */
export const listRegistryBadges = query({
  args: {},
  returns: v.array(
    v.object({
      machineId: v.id("machines"),
      level: machineValidationLevelValidator,
      versionStatus: versionStatusValidator,
      configurationStatus: v.literal("unavailable"),
    }),
  ),
  handler: async (ctx) => {
    const user = await getCurrentUserOrThrow(ctx);
    if (user.role !== "admin" && user.role !== "org_admin") return [];
    const machines =
      user.role === "admin"
        ? await ctx.db.query("machines").collect()
        : await ctx.db
            .query("machines")
            .withIndex("by_organization", (q) =>
              q.eq("organizationId", user.organizationId),
            )
            .collect();
    return await Promise.all(
      machines
        .filter((machine) => !machine.isDeleted)
        .map(async (machine) => {
          const registry = await registryOf(ctx, machine._id);
          return {
            machineId: machine._id,
            level: effectiveValidationLevel(registry?.validation, Date.now()),
            versionStatus: (await statusOf(ctx, machine, registry))
              .versionStatus,
            configurationStatus: "unavailable" as const,
          };
        }),
    );
  },
});

/** Returns the effective configuration verdict for a reader allowed to see the machine. */
export const checkConfigurationLock = query({
  args: {
    machineId: v.id("machines"),
    key: configurationKeyValidator,
    value: v.boolean(),
  },
  returns: v.union(v.null(), lockVerdictValidator),
  handler: async (ctx, args) => {
    if ((await readableMachine(ctx, args.machineId)) === null) return null;
    const registry = await registryOf(ctx, args.machineId);
    return configurationLockVerdict(
      effectiveValidationLevel(registry?.validation, Date.now()),
      args.key,
      args.value,
    );
  },
});

/** Issues a registry upload URL to an Anheart administrator. */
export const generateUploadUrl = mutation({
  args: {},
  returns: v.string(),
  handler: async (ctx) => {
    await limitUser(ctx);
    await requireRole(ctx, ["admin"]);
    return await ctx.storage.generateUploadUrl();
  },
});

const nullableText = v.optional(v.union(v.string(), v.null()));
/** Updates supplied identity fields, preserving absent fields and clearing explicit nulls. */
export const updateIdentity = mutation({
  args: {
    machineId: v.id("machines"),
    serialPi: nullableText,
    serialDrive: nullableText,
    serialMotor: nullableText,
    serialGearbox: nullableText,
    bitalinoAddress: nullableText,
    site: nullableText,
    expectedSoftwareVersion: nullableText,
    commissionedAt: v.optional(v.union(v.number(), v.null())),
    contacts: v.optional(v.union(v.array(v.id("users")), v.null())),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const state = await editableMachine(ctx, args.machineId);
    const fields: Partial<Doc<"machine_registry">> = {};
    for (const key of [
      "serialPi",
      "serialDrive",
      "serialMotor",
      "serialGearbox",
      "site",
    ] as const) {
      const value = args[key];
      if (value !== undefined)
        fields[key] =
          value === null ? undefined : text(value, key === "site" ? 500 : 200);
    }
    if (args.bitalinoAddress !== undefined) {
      const value =
        args.bitalinoAddress === null
          ? undefined
          : text(args.bitalinoAddress).toUpperCase();
      if (value !== undefined && !/^[0-9A-F]{2}(:[0-9A-F]{2}){5}$/.test(value))
        throw refusal("invalid_registry_field");
      fields.bitalinoAddress = value;
    }
    if (args.commissionedAt !== undefined)
      fields.commissionedAt =
        args.commissionedAt === null
          ? undefined
          : timestamp(args.commissionedAt);
    if (args.expectedSoftwareVersion !== undefined)
      fields.expectedSoftwareVersion =
        args.expectedSoftwareVersion === null
          ? undefined
          : await requirePiVersion(ctx, text(args.expectedSoftwareVersion));
    if (args.contacts !== undefined) {
      const contacts = args.contacts ?? [];
      if (contacts.length > 20 || new Set(contacts).size !== contacts.length)
        throw refusal("invalid_registry_field");
      for (const userId of contacts) {
        if (
          (await contactOf(ctx, userId, state.machine.organizationId)) === null
        )
          throw refusal("contact_not_in_organization");
      }
      fields.contacts = contacts;
    }
    const changedFields = (
      Object.keys(fields) as (keyof typeof fields)[]
    ).filter((key) => {
      const previous =
        state.registry?.[key] ?? (key === "contacts" ? [] : undefined);
      return JSON.stringify(previous) !== JSON.stringify(fields[key]);
    });
    await saveRegistry(ctx, state, fields);
    if (changedFields.length > 0)
      await audit(ctx, {
        actor: userActor(state.admin),
        source: "site",
        organizationId: state.machine.organizationId,
        action: "registry.identity_updated",
        targetId: args.machineId,
        details: { fields: changedFields },
      });
    return null;
  },
});

/** Records the measured radii together with their date and measurer. */
export const recordRadii = mutation({
  args: {
    machineId: v.id("machines"),
    armRadiusM: v.number(),
    legTipRadiusM: v.number(),
    measuredAt: v.number(),
    measuredBy: v.string(),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const state = await editableMachine(ctx, args.machineId);
    // Ten metres catches unit mistakes; it is a plausibility bound, not a safety limit.
    if (
      [args.armRadiusM, args.legTipRadiusM].some(
        (radius) => !Number.isFinite(radius) || radius <= 0 || radius > 10,
      )
    )
      throw refusal("invalid_registry_field");
    await saveRegistry(ctx, state, {
      armRadiusM: args.armRadiusM,
      legTipRadiusM: args.legTipRadiusM,
      radiiMeasuredAt: timestamp(args.measuredAt),
      radiiMeasuredBy: text(args.measuredBy),
    });
    await audit(ctx, {
      actor: userActor(state.admin),
      source: "site",
      organizationId: state.machine.organizationId,
      action: "registry.radii_recorded",
      targetId: args.machineId,
      details: {
        fields: [
          "armRadiusM",
          "legTipRadiusM",
          "radiiMeasuredAt",
          "radiiMeasuredBy",
        ],
      },
    });
    return null;
  },
});

/** Attaches a checked upload while retaining every replaced drive sheet. */
export const attachDriveParameterSheet = mutation({
  args: {
    machineId: v.id("machines"),
    storageId: v.id("_storage"),
    version: v.string(),
    signedBy: v.string(),
    at: v.number(),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const state = await editableMachine(ctx, args.machineId);
    const sheet = {
      storageId: args.storageId,
      contentType: await registryFile(ctx, args.storageId),
      version: text(args.version, 50),
      signedBy: text(args.signedBy),
      at: timestamp(args.at),
      attachedBy: state.admin._id,
      attachedAt: Date.now(),
    };
    const history = state.registry?.driveParameterSheetHistory ?? [];
    if (state.registry?.driveParameterSheet !== undefined)
      history.push(state.registry.driveParameterSheet);
    await saveRegistry(ctx, state, {
      driveParameterSheet: sheet,
      driveParameterSheetHistory: history,
    });
    await audit(ctx, {
      actor: userActor(state.admin),
      source: "site",
      organizationId: state.machine.organizationId,
      action: "registry.drive_sheet_attached",
      targetId: args.machineId,
    });
    return null;
  },
});

/** Raises the effective level by one review with an attached report and known versions. */
export const addValidationReview = mutation({
  args: {
    machineId: v.id("machines"),
    level: machineReviewLevelValidator,
    reportStorageId: v.id("_storage"),
    softwareVersion: v.string(),
    medicalParametersVersion: v.union(v.string(), v.null()),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const state = await editableMachine(ctx, args.machineId);
    const validation = state.registry?.validation ?? emptyValidation;
    const rejected = reviewRefusal(
      effectiveValidationLevel(validation, Date.now()),
      args.level,
      MEDICAL_KEY_IS_TEST,
      args.medicalParametersVersion,
    );
    if (rejected !== null) throw refusal(rejected);
    await registryFile(ctx, args.reportStorageId);
    const softwareVersion = await requirePiVersion(ctx, args.softwareVersion);
    const medicalVersion = args.medicalParametersVersion;
    if (
      medicalVersion !== null &&
      (await ctx.db
        .query("medical_parameters_versions")
        .withIndex("by_version", (q) => q.eq("version", medicalVersion))
        .unique()) === null
    )
      throw refusal("unknown_medical_parameters_version");
    const nextValidation = {
      ...validation,
      level: args.level,
      reviews: [
        ...validation.reviews,
        {
          level: args.level,
          by: state.admin._id,
          at: Date.now(),
          reportStorageId: args.reportStorageId,
          softwareVersion,
          medicalParametersVersion: args.medicalParametersVersion,
        },
      ],
    };
    if (levelAt(nextValidation, Date.now()) !== args.level)
      throw refusal("validation_level_not_next");
    await saveRegistry(ctx, state, { validation: nextValidation });
    await audit(ctx, {
      actor: userActor(state.admin),
      source: "site",
      organizationId: state.machine.organizationId,
      action: "registry.review_added",
      targetId: args.machineId,
      details: {
        level: args.level,
        previousLevel: effectiveValidationLevel(validation, Date.now()),
        softwareVersion,
        medicalParametersVersion: medicalVersion ?? undefined,
      },
    });
    return null;
  },
});

/** Lowers the effective level with an administrator's recorded reason. */
export const lowerValidationLevel = mutation({
  args: {
    machineId: v.id("machines"),
    level: machineValidationLevelValidator,
    reason: v.string(),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const state = await editableMachine(ctx, args.machineId);
    const validation = state.registry?.validation ?? emptyValidation;
    const effectiveLevel = effectiveValidationLevel(validation, Date.now());
    const rejected = downgradeRefusal(effectiveLevel, args.level, args.reason);
    if (rejected !== null) throw refusal(rejected);
    await saveRegistry(ctx, state, {
      validation: {
        ...validation,
        level: args.level,
        downgrades: [
          ...validation.downgrades,
          {
            from: effectiveLevel,
            to: args.level,
            by: state.admin._id,
            at: Date.now(),
            reason: args.reason.trim(),
          },
        ],
      },
    });
    await audit(ctx, {
      actor: userActor(state.admin),
      source: "site",
      organizationId: state.machine.organizationId,
      action: "registry.level_lowered",
      targetId: args.machineId,
      details: { level: args.level, previousLevel: effectiveLevel },
    });
    return null;
  },
});

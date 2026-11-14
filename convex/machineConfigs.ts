/**
 * ANH-141: a machine's configuration, proposed and approved on the site, then
 * fetched signed and applied at rest by the console, which revalidates it.
 *
 * Who does what:
 * - an Anheart admin proposes any key; an organisation admin only the
 *   machine's `name` and `location`, on a machine of their organisation;
 * - a revision that changes no safety key from the revision applied is
 *   approved once, its author included (an organisation admin approves only
 *   what they may propose);
 * - a revision that changes a safety key (`SAFETY_KEYS`) from the revision
 *   applied, or the first revision of a machine, is approved by an Anheart
 *   admin other than its author, who must be one too: two named accounts;
 * - the two locks are refused above the machine's validation level
 *   (`requireConfigurationAllowed`, ANH-147), at proposal and at approval;
 * - the machine says what it applied, or refused (`recordApplied`).
 *
 * Every step is audited with the keys it changes (`fields`), never the values,
 * which the revision keeps unchanged.
 */
import { ConvexError, v } from "convex/values";
import type { Doc, Id } from "./_generated/dataModel";
import {
  internalMutation,
  internalQuery,
  mutation,
  query,
  type MutationCtx,
  type QueryCtx,
} from "./_generated/server";
import { audit, machineActor, userActor } from "./lib/audit";
import { getCurrentUserOrThrow, requireRole, type CurrentUser } from "./lib/auth";
import { refusal } from "./lib/errors";
import {
  canonicalConfig,
  changedKeys,
  configHash,
  configurationStatus,
  configValuesValidator,
  contractDefaults,
  machineConfigFields,
  machineConfigPayload,
  machineConfigProblems,
  ORGANIZATION_KEYS,
  SAFETY_KEYS,
  type ConfigValues,
} from "./lib/machineConfig";
import {
  effectiveValidationLevel,
  radiiProposal,
  requireConfigurationAllowed,
} from "./lib/machineRegistry";
import { limitUser } from "./lib/rateLimits";
import { machineValidationLevelValidator } from "./schema";

type Revision = Doc<"machine_configs">;

async function revisionsOf(ctx: QueryCtx, machineId: Id<"machines">): Promise<Revision[]> {
  return await ctx.db
    .query("machine_configs")
    .withIndex("by_machine_and_revision", (q) => q.eq("machineId", machineId))
    .collect();
}

/** The latest revision of `status` among `rows` (ordered by revision), or null. */
function latest(rows: readonly Revision[], ...status: Revision["status"][]): Revision | null {
  return [...rows].reverse().find((row) => status.includes(row.status)) ?? null;
}

/** What the machine is sent: the latest revision approved or applied. */
function inForce(rows: readonly Revision[]) {
  const row = latest(rows, "approved", "applied");
  return row === null ? null : { ...row, status: row.status as "approved" | "applied" };
}

async function levelOf(ctx: QueryCtx, machineId: Id<"machines">) {
  const registry = await ctx.db
    .query("machine_registry")
    .withIndex("by_machine", (q) => q.eq("machineId", machineId))
    .unique();
  return { registry, level: effectiveValidationLevel(registry?.validation, Date.now()) };
}

/** The Anheart admin reaches every machine; an organisation admin their organisation's, not deleted. */
function reaches(user: CurrentUser, machine: Doc<"machines">): boolean {
  return (
    user.role === "admin" ||
    (user.role === "org_admin" &&
      machine.organizationId === user.organizationId &&
      machine.isDeleted !== true)
  );
}

/** The machine a write aims at, refused as unknown when deleted or out of reach. */
async function writableMachine(ctx: MutationCtx, user: CurrentUser, machineId: Id<"machines">) {
  const machine = await ctx.db.get(machineId);
  if (machine === null || machine.isDeleted === true || !reaches(user, machine))
    throw refusal("machine_not_found");
  return machine;
}

function invalid(problems: ReturnType<typeof machineConfigProblems>) {
  return new ConvexError({
    code: "invalid_machine_config",
    message: problems.map((problem) => `${problem.key}: ${problem.detail}`).join("; "),
    problems,
  });
}

/** Who may approve `row`, and whether a safety key changes from the revision applied. */
function approvalOf(user: CurrentUser, row: Revision, rows: readonly Revision[]) {
  const changed = changedKeys(latest(rows, "applied")?.values ?? null, row.values);
  const safety = changed.some((key) => SAFETY_KEYS.includes(key));
  const fromInForce = changedKeys(inForce(rows)?.values ?? null, row.values);
  let blocked: "machine_config_keys_not_allowed" | "machine_config_self_approval" | "machine_config_safety_needs_admins" | null = null;
  if (user.role !== "admin" && (safety || fromInForce.some((key) => !ORGANIZATION_KEYS.includes(key))))
    blocked = "machine_config_keys_not_allowed";
  else if (safety && row.proposedBy === user._id) blocked = "machine_config_self_approval";
  else if (safety && row.proposedByRole !== "admin") blocked = "machine_config_safety_needs_admins";
  return { changed, safety, blocked };
}

const authorValidator = v.object({
  userId: v.id("users"),
  firstName: v.optional(v.string()),
  lastName: v.optional(v.string()),
});

const revisionValidator = v.object({
  ...machineConfigFields,
  _id: v.id("machine_configs"),
  _creationTime: v.number(),
  proposedBy: authorValidator,
  approvedBy: v.optional(v.array(authorValidator)),
  rejectedBy: v.optional(authorValidator),
  /** The keys that differ from the revision applied now. */
  changedFromApplied: v.array(v.string()),
  safetyChange: v.boolean(),
  /** Null when the caller may approve this draft; else the refusal approving would meet. */
  approveBlocked: v.union(
    v.null(),
    v.literal("machine_config_wrong_status"),
    v.literal("machine_config_keys_not_allowed"),
    v.literal("machine_config_self_approval"),
    v.literal("machine_config_safety_needs_admins"),
  ),
});

/** The revisions a machine page shows, at most this many, the latest first. */
const SHOWN_REVISIONS = 20;

/**
 * The machine's configuration for its page: the revisions, the one applied,
 * the one in force, what the heartbeat reports and whether it matches, the
 * defaults of a first proposal (the contract's, the registry's radii, the
 * machine's name and place), the validation level and what the caller may
 * write. Anheart admin: every machine. Organisation admin: their
 * organisation's machines, not deleted. Anyone else: null.
 */
export const getMachineConfig = query({
  args: { machineId: v.id("machines") },
  returns: v.union(
    v.null(),
    v.object({
      machineId: v.id("machines"),
      revisions: v.array(revisionValidator),
      appliedRevision: v.union(v.number(), v.null()),
      inForceRevision: v.union(v.number(), v.null()),
      reportedHash: v.union(v.string(), v.null()),
      hashMatches: v.union(v.boolean(), v.null()),
      configurationStatus: v.union(
        v.literal("unavailable"),
        v.literal("pending"),
        v.literal("applied"),
        v.literal("mismatch"),
      ),
      defaults: configValuesValidator,
      validationLevel: machineValidationLevelValidator,
      canPropose: v.union(v.literal("all"), v.literal("name_location"), v.literal("none")),
    }),
  ),
  handler: async (ctx, args) => {
    const user = await getCurrentUserOrThrow(ctx);
    const machine = await ctx.db.get(args.machineId);
    if (machine === null || !reaches(user, machine)) return null;
    const rows = await revisionsOf(ctx, machine._id);
    const { registry, level } = await levelOf(ctx, machine._id);
    const applied = latest(rows, "applied");
    const target = inForce(rows);
    const author = async (userId: Id<"users">) => {
      const account = await ctx.db.get(userId);
      return account === null || account.clerkDeletedAt !== undefined
        ? { userId }
        : { userId, firstName: account.firstName, lastName: account.lastName };
    };
    const shown = rows.slice(-SHOWN_REVISIONS).reverse();
    const revisions = await Promise.all(
      shown.map(async (row) => {
        const approval = approvalOf(user, row, rows);
        return {
          ...row,
          proposedBy: await author(row.proposedBy),
          approvedBy:
            row.approvedBy === undefined ? undefined : await Promise.all(row.approvedBy.map(author)),
          rejectedBy: row.rejectedBy === undefined ? undefined : await author(row.rejectedBy),
          changedFromApplied: approval.changed,
          safetyChange: approval.safety,
          approveBlocked: row.status === "draft" ? approval.blocked : ("machine_config_wrong_status" as const),
        };
      }),
    );
    const reported = machine.configHash ?? null;
    const defaults: ConfigValues = {
      ...contractDefaults(),
      ...radiiProposal(registry),
      name: machine.name,
      location: machine.location ?? null,
    };
    return {
      machineId: machine._id,
      revisions,
      appliedRevision: applied?.revision ?? null,
      inForceRevision: target?.revision ?? null,
      reportedHash: reported,
      hashMatches: target === null ? null : reported === target.hash,
      configurationStatus: configurationStatus(target, reported),
      defaults,
      validationLevel: level,
      canPropose:
        machine.isDeleted === true
          ? ("none" as const)
          : user.role === "admin"
            ? ("all" as const)
            : ("name_location" as const),
    };
  },
});

/**
 * Propose a revision. Every problem the console would raise is refused at
 * once (`invalid_machine_config`, with `problems`); a revision equal to the
 * one in force is refused (`machine_config_unchanged`). A draft waiting is
 * replaced: it is rejected, "remplacée par la révision N".
 */
export const proposeConfig = mutation({
  args: { machineId: v.id("machines"), values: configValuesValidator },
  returns: v.object({ configId: v.id("machine_configs"), revision: v.number() }),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const user = await requireRole(ctx, ["admin", "org_admin"]);
    const machine = await writableMachine(ctx, user, args.machineId);
    const rows = await revisionsOf(ctx, machine._id);
    const { level } = await levelOf(ctx, machine._id);
    const problems = machineConfigProblems(args.values, level);
    if (problems.length > 0) throw invalid(problems);
    const values: ConfigValues = args.values;
    const canonical = canonicalConfig(values);
    const hash = await configHash(canonical);
    const target = inForce(rows);
    if (target?.hash === hash) throw refusal("machine_config_unchanged");
    if (
      user.role !== "admin" &&
      changedKeys(target?.values ?? null, values).some((key) => !ORGANIZATION_KEYS.includes(key))
    )
      throw refusal("machine_config_keys_not_allowed");
    const now = Date.now();
    const revision = (rows[rows.length - 1]?.revision ?? 0) + 1;
    const origin = { source: "site" as const, actor: userActor(user) };
    for (const draft of rows.filter((row) => row.status === "draft")) {
      await ctx.db.patch(draft._id, {
        status: "rejected",
        rejectedAt: now,
        rejectedBy: user._id,
        rejectReason: `remplacée par la révision ${revision}`,
      });
      await audit(ctx, {
        ...origin,
        organizationId: machine.organizationId,
        action: "machine_config.rejected",
        targetId: draft._id,
        details: { machineId: machine._id, version: String(draft.revision) },
      });
    }
    const changed = changedKeys(latest(rows, "applied")?.values ?? null, values);
    const configId = await ctx.db.insert("machine_configs", {
      organizationId: machine.organizationId,
      machineId: machine._id,
      revision,
      values,
      canonical,
      hash,
      status: "draft",
      changedKeys: changed,
      proposedBy: user._id,
      proposedByRole: user.role === "admin" ? "admin" : "org_admin",
      proposedAt: now,
    });
    await audit(ctx, {
      ...origin,
      organizationId: machine.organizationId,
      action: "machine_config.proposed",
      targetId: configId,
      details: { machineId: machine._id, version: String(revision), fields: changed },
    });
    return { configId, revision };
  },
});

async function writableRevision(ctx: MutationCtx, configId: Id<"machine_configs">) {
  await limitUser(ctx);
  const user = await requireRole(ctx, ["admin", "org_admin"]);
  const row = await ctx.db.get(configId);
  const machine = row === null ? null : await ctx.db.get(row.machineId);
  if (row === null || machine === null || machine.isDeleted === true || !reaches(user, machine))
    throw refusal("machine_config_not_found");
  return { user, row, machine };
}

/**
 * Approve a draft (see the rules at the top). The values are judged again at
 * the machine's level now; once approved, the revision is what the machine
 * is sent, an earlier one approved and not applied is rejected, and the
 * machine's name and place follow the values.
 */
export const approveConfig = mutation({
  args: { configId: v.id("machine_configs") },
  returns: v.null(),
  handler: async (ctx, args) => {
    const { user, row, machine } = await writableRevision(ctx, args.configId);
    if (row.status !== "draft") throw refusal("machine_config_wrong_status");
    const rows = await revisionsOf(ctx, machine._id);
    const approval = approvalOf(user, row, rows);
    if (approval.blocked !== null) throw refusal(approval.blocked);
    const { level } = await levelOf(ctx, machine._id);
    const problems = machineConfigProblems(row.values, level);
    if (problems.length > 0) throw invalid(problems);
    // EX-5, the registry's own guard (ANH-147): never true above the validated level.
    await requireConfigurationAllowed(ctx, machine._id, "PROGRAMS_ENABLED", row.values.PROGRAMS_ENABLED === true);
    await requireConfigurationAllowed(
      ctx,
      machine._id,
      "OCCUPANCY_OCCUPIED_ENABLED",
      row.values.OCCUPANCY_OCCUPIED_ENABLED === true,
    );
    const now = Date.now();
    const origin = { source: "site" as const, actor: userActor(user) };
    for (const earlier of rows.filter((other) => other.status === "approved")) {
      await ctx.db.patch(earlier._id, {
        status: "rejected",
        rejectedAt: now,
        rejectReason: `remplacée par la révision ${row.revision} avant son application`,
      });
      await audit(ctx, {
        ...origin,
        organizationId: machine.organizationId,
        action: "machine_config.rejected",
        targetId: earlier._id,
        details: { machineId: machine._id, version: String(earlier.revision) },
      });
    }
    await ctx.db.patch(row._id, {
      status: "approved",
      approvedBy: approval.safety ? [row.proposedBy, user._id] : [user._id],
      approvedAt: now,
    });
    await ctx.db.patch(machine._id, {
      name: row.values.name as string,
      location: (row.values.location as string | null) ?? undefined,
    });
    await audit(ctx, {
      ...origin,
      organizationId: machine.organizationId,
      action: "machine_config.approved",
      targetId: row._id,
      details: { machineId: machine._id, version: String(row.revision), fields: approval.changed },
    });
    return null;
  },
});

/**
 * Reject a draft, or a revision approved and not yet applied, with a reason
 * of 3 to 500 characters. The Anheart admin: any. An organisation admin:
 * their own drafts.
 */
export const rejectConfig = mutation({
  args: { configId: v.id("machine_configs"), reason: v.string() },
  returns: v.null(),
  handler: async (ctx, args) => {
    const { user, row, machine } = await writableRevision(ctx, args.configId);
    if (row.status !== "draft" && row.status !== "approved")
      throw refusal("machine_config_wrong_status");
    if (user.role !== "admin" && (row.status !== "draft" || row.proposedBy !== user._id))
      throw refusal("not_authorized");
    const reason = args.reason.trim();
    if (reason.length < 3 || reason.length > 500) throw refusal("machine_config_reason_required");
    await ctx.db.patch(row._id, {
      status: "rejected",
      rejectedAt: Date.now(),
      rejectedBy: user._id,
      rejectReason: reason,
    });
    await audit(ctx, {
      source: "site",
      actor: userActor(user),
      organizationId: machine.organizationId,
      action: "machine_config.rejected",
      targetId: row._id,
      details: { machineId: machine._id, version: String(row.revision) },
    });
    return null;
  },
});

/**
 * The text `GET /api/machine/config` signs (contract 1.15): the revision in
 * force, its canonical values and hash, and the machine's validation level
 * now, dated `issuedAt`.
 */
export const signedConfig = internalQuery({
  args: { machineId: v.id("machines"), issuedAt: v.number() },
  returns: v.string(),
  handler: async (ctx, args) => {
    const target = inForce(await revisionsOf(ctx, args.machineId));
    const { level } = await levelOf(ctx, args.machineId);
    return machineConfigPayload({
      machineId: args.machineId,
      issuedAt: args.issuedAt,
      validationLevel: level,
      revision:
        target === null
          ? null
          : { revision: target.revision, hash: target.hash, values: target.canonical },
    });
  },
});

/**
 * What the machine says of a revision (`POST /api/machine/config/applied`):
 * applied (`refusal` absent), or refused with its reason. `unknown`: no such
 * revision of this machine, or another hash; `not_approved`: a draft. A
 * revision said applied again, or refused once it is no longer approved,
 * changes nothing.
 */
export const recordApplied = internalMutation({
  args: {
    machineId: v.id("machines"),
    revision: v.number(),
    hash: v.string(),
    refusal: v.optional(v.string()),
  },
  returns: v.union(
    v.literal("applied"),
    v.literal("refused"),
    v.literal("unchanged"),
    v.literal("unknown"),
    v.literal("not_approved"),
  ),
  handler: async (ctx, args) => {
    const row = await ctx.db
      .query("machine_configs")
      .withIndex("by_machine_and_revision", (q) =>
        q.eq("machineId", args.machineId).eq("revision", args.revision),
      )
      .unique();
    if (row === null || row.hash !== args.hash) return "unknown";
    if (row.status === "draft") return "not_approved";
    const origin = {
      source: "machine" as const,
      actor: machineActor(args.machineId),
      organizationId: row.organizationId,
      targetId: row._id,
      details: { machineId: args.machineId, version: String(row.revision) },
    };
    const now = Date.now();
    if (args.refusal !== undefined) {
      if (row.status !== "approved") return "unchanged";
      await ctx.db.patch(row._id, {
        status: "rejected",
        rejectedAt: now,
        rejectReason: `refusée par la machine : ${args.refusal}`,
      });
      await audit(ctx, { ...origin, action: "machine_config.refused" });
      return "refused";
    }
    if (row.status === "applied") return "unchanged";
    await ctx.db.patch(row._id, { status: "applied", appliedAt: now, appliedHash: args.hash });
    await audit(ctx, { ...origin, action: "machine_config.applied" });
    return "applied";
  },
});

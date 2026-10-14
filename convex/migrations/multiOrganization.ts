/**
 * Multi-organisation migration (ANH-114).
 *
 * `attachExistingRowsToAnheart` creates the default organisation "Anheart" and
 * attaches to it every row written before organisations existed. It is an
 * internal mutation, run by hand once per deployment after this code is
 * deployed, never by a user of the site:
 *
 *     npx convex run migrations/multiOrganization:attachExistingRowsToAnheart '{}'
 *
 * It is idempotent and resumable: it only ever touches rows that have no
 * organisation yet, works in bounded batches and reschedules itself until
 * nothing is left, so running it twice, or again after an interruption, gives
 * the same result. A row that already has an organisation is never moved.
 *
 * `repairDuplicatedAnheartOrganization` is the way out of the state where the
 * migration refuses (`ANHEART_ORGANIZATION_DUPLICATED`), also internal and run
 * by hand (ANH-225), in two steps: a dry run that writes nothing and says
 * which organisation it would absorb, then the run that applies it, which
 * names that organisation as the dry run reported it:
 *
 *     npx convex run migrations/multiOrganization:repairDuplicatedAnheartOrganization '{}'
 *     npx convex run migrations/multiOrganization:repairDuplicatedAnheartOrganization '{"apply": true, "expectedName": "..."}'
 */
import { ConvexError, v } from "convex/values";
import { internalMutation, type MutationCtx } from "../_generated/server";
import { internal } from "../_generated/api";
import type { Doc, Id } from "../_generated/dataModel";
import {
  DEFAULT_ORGANIZATION_SLUG,
  anheartClerkOrganizationId,
  findMembership,
  findUnlinkedDefaultOrganization,
} from "../lib/auth";

/** Rows attached per run when the operator does not choose. */
const DEFAULT_BATCH_SIZE = 500;
const MAX_BATCH_SIZE = 1000;

/** Tables whose rows follow their machine's organisation. */
const MACHINE_TABLES = [
  "sessions",
  "training_telemetry",
  "training_events",
  "machine_profiles",
  "machine_user_permissions",
  "machine_gestionnaires",
] as const;

/**
 * Why the migration stops without writing anything: the Clerk organisation
 * named by `ANHEART_ORG_ID` already has its own row (the webhook received it
 * before the variable was set), while the default organisation, which holds
 * the rows written before organisations existed, is still unlinked. Taking
 * either would leave the other's rows out of the Anheart organisation.
 */
export const ANHEART_ORGANIZATION_DUPLICATED =
  "Two Anheart organisations: the Clerk organisation named by ANHEART_ORG_ID " +
  "already has its own row while the default organisation is not linked to " +
  "it. Nothing was changed. Set ANHEART_ORG_ID before the Clerk webhook " +
  "receives that organisation (docs/convex.md, Activer le multi-organisation). " +
  "To repair, first describe it without writing anything: npx convex run " +
  "migrations/multiOrganization:repairDuplicatedAnheartOrganization '{}' " +
  "(docs/convex.md, Sortir de deux organisations Anheart).";

/** The repair cannot tell which row is the Anheart organisation. */
export const ANHEART_ORG_ID_REQUIRED =
  "ANHEART_ORG_ID is not set on this deployment, so no row is known as the " +
  "Clerk Anheart organisation. Nothing was changed.";

/** ANHEART_ORG_ID names an organisation Clerk has deleted. */
export const ANHEART_ORGANIZATION_DELETED_IN_CLERK =
  "The Clerk organisation named by ANHEART_ORG_ID was deleted in Clerk. " +
  "Nothing was changed: check ANHEART_ORG_ID.";

/**
 * The applying run does not name the organisation the dry run reported. The
 * name is what makes a wrong `ANHEART_ORG_ID` visible: a client organisation
 * the variable names by mistake would be absorbed, and every row written
 * before organisations existed would follow that client's Clerk organisation.
 */
export function repairNotConfirmed(actualName: string): string {
  return (
    `The repair would absorb the organisation named "${actualName}" and was ` +
    `not confirmed with that name. Nothing was changed. Check in Clerk that ` +
    `it is the Anheart organisation, then pass {"apply": true, ` +
    `"expectedName": <the name the dry run reported>}; if it is not, ` +
    `ANHEART_ORG_ID is wrong: correct it and do not apply.`
  );
}

/**
 * The row the webhook created for the Anheart organisation already holds data
 * of its own: merging it would need a decision row by row, so the repair
 * leaves it to the operator.
 */
export function duplicateHoldsData(table: string): string {
  return (
    `The row the Clerk webhook created for the Anheart organisation holds ` +
    `data (table ${table}). Nothing was changed: it cannot be merged ` +
    `automatically (docs/convex.md, Sortir de deux organisations Anheart).`
  );
}

/**
 * Tables whose rows belong to an organisation and would be lost with it. The
 * repair refuses while any of them points to the duplicate. Three more tables
 * carry an organisation (`machine_health_history`, `session_record_files`,
 * `session_record_uploads`) but have no index on it: they are written with the
 * organisation of their machine or of their session, and a machine or a
 * session of the duplicate already stops the repair. Memberships and accounts
 * are not in this list: they are what the webhook writes, and the repair moves
 * them.
 */
const ORGANIZATION_DATA_TABLES = [
  "machines",
  ...MACHINE_TABLES,
  "user_gestionnaires",
] as const;

/**
 * The default organisation, created on first run. When `ANHEART_ORG_ID` is
 * set, the organisation is the mirror of that Clerk organisation: an existing
 * unlinked default organisation is linked to it rather than duplicated. When
 * both the mirror and the unlinked default organisation exist, it refuses
 * (`ANHEART_ORGANIZATION_DUPLICATED`).
 */
async function ensureAnheartOrganization(
  ctx: MutationCtx,
): Promise<Id<"organizations">> {
  const clerkOrgId = anheartClerkOrganizationId();
  if (clerkOrgId !== null) {
    const linked = await ctx.db
      .query("organizations")
      .withIndex("by_clerk_org_id", (q) => q.eq("clerkOrgId", clerkOrgId))
      .unique();
    if (linked) {
      if ((await findUnlinkedDefaultOrganization(ctx)) !== null) {
        throw new ConvexError(ANHEART_ORGANIZATION_DUPLICATED);
      }
      return linked._id;
    }
  }
  const unlinked = await findUnlinkedDefaultOrganization(ctx);
  if (unlinked) {
    if (clerkOrgId !== null) await ctx.db.patch(unlinked._id, { clerkOrgId });
    return unlinked._id;
  }
  return await ctx.db.insert("organizations", {
    clerkOrgId: clerkOrgId ?? undefined,
    name: "Anheart",
    slug: DEFAULT_ORGANIZATION_SLUG,
    createdAt: Date.now(),
    settings: { requirePrescription: false, language: "fr" },
  });
}

export const attachExistingRowsToAnheart = internalMutation({
  args: { batchSize: v.optional(v.number()) },
  returns: v.object({
    organizationId: v.id("organizations"),
    attached: v.number(),
    done: v.boolean(),
  }),
  handler: async (ctx, args) => {
    const batchSize = Math.min(
      Math.max(Math.floor(args.batchSize ?? DEFAULT_BATCH_SIZE), 1),
      MAX_BATCH_SIZE,
    );
    const anheart = await ensureAnheartOrganization(ctx);
    let attached = 0;
    /** How many rows this run may still attach. */
    const room = () => batchSize - attached;

    // Machines first: what a machine wrote follows the machine.
    const machines =
      room() > 0
        ? await ctx.db
            .query("machines")
            .withIndex("by_organization", (q) =>
              q.eq("organizationId", undefined),
            )
            .take(room())
        : [];
    for (const machine of machines) {
      await ctx.db.patch(machine._id, { organizationId: anheart });
      attached++;
    }

    // Accounts: main organisation, and the membership that mirrors their role.
    const users =
      room() > 0
        ? await ctx.db
            .query("users")
            .withIndex("by_organization", (q) =>
              q.eq("organizationId", undefined),
            )
            .take(room())
        : [];
    for (const user of users) {
      await ctx.db.patch(user._id, { organizationId: anheart });
      if (!(await findMembership(ctx, user._id, anheart))) {
        await ctx.db.insert("memberships", {
          userId: user._id,
          organizationId: anheart,
          role: user.role,
          active: true,
        });
      }
      attached++;
    }

    const organizationOfMachine = new Map<
      Id<"machines">,
      Id<"organizations">
    >();
    const machineOrganization = async (machineId: Id<"machines">) => {
      const known = organizationOfMachine.get(machineId);
      if (known) return known;
      const machine = await ctx.db.get(machineId);
      const organizationId = machine?.organizationId ?? anheart;
      organizationOfMachine.set(machineId, organizationId);
      return organizationId;
    };
    for (const table of MACHINE_TABLES) {
      if (room() <= 0) break;
      const rows = await ctx.db
        .query(table)
        .withIndex("by_organization", (q) => q.eq("organizationId", undefined))
        .take(room());
      for (const row of rows) {
        await ctx.db.patch(row._id, {
          organizationId: await machineOrganization(row.machineId),
        });
        attached++;
      }
    }

    // A patient-gestionnaire link lives in the patient's main organisation.
    const links =
      room() > 0
        ? await ctx.db
            .query("user_gestionnaires")
            .withIndex("by_organization", (q) =>
              q.eq("organizationId", undefined),
            )
            .take(room())
        : [];
    for (const link of links) {
      const patient = await ctx.db.get(link.userId);
      await ctx.db.patch(link._id, {
        organizationId: patient?.organizationId ?? anheart,
      });
      attached++;
    }

    // A full batch may have left rows behind: continue in a new transaction.
    const done = attached < batchSize;
    if (!done) {
      await ctx.scheduler.runAfter(
        0,
        internal.migrations.multiOrganization.attachExistingRowsToAnheart,
        { batchSize },
      );
    }
    return { organizationId: anheart, attached, done };
  },
});

/**
 * What the repair would do, read without writing anything; null when there is
 * nothing to repair. It throws the refusals: no `ANHEART_ORG_ID`, an
 * organisation Clerk deleted, or a row that holds data of its own.
 */
async function planRepair(ctx: MutationCtx) {
  const clerkOrgId = anheartClerkOrganizationId();
  if (clerkOrgId === null) throw new ConvexError(ANHEART_ORG_ID_REQUIRED);
  const duplicate = await ctx.db
    .query("organizations")
    .withIndex("by_clerk_org_id", (q) => q.eq("clerkOrgId", clerkOrgId))
    .unique();
  const anheart = await findUnlinkedDefaultOrganization(ctx);
  if (duplicate === null || anheart === null) return null;
  if (duplicate.deletedAt !== undefined) {
    throw new ConvexError(ANHEART_ORGANIZATION_DELETED_IN_CLERK);
  }
  for (const table of ORGANIZATION_DATA_TABLES) {
    const row = await ctx.db
      .query(table)
      .withIndex("by_organization", (q) =>
        q.eq("organizationId", duplicate._id),
      )
      .first();
    if (row !== null) throw new ConvexError(duplicateHoldsData(table));
  }
  const invitation = await ctx.db
    .query("patient_invitations")
    .withIndex("by_organization_and_email", (q) =>
      q.eq("organizationId", duplicate._id),
    )
    .first();
  if (invitation !== null) {
    throw new ConvexError(duplicateHoldsData("patient_invitations"));
  }

  const memberships = await ctx.db
    .query("memberships")
    .withIndex("by_organization", (q) => q.eq("organizationId", duplicate._id))
    .collect();
  // A member the default organisation already has keeps that membership.
  const merges: Array<{
    membership: Doc<"memberships">;
    held: Doc<"memberships">;
  }> = [];
  const moves: Array<Doc<"memberships">> = [];
  for (const membership of memberships) {
    const held = await findMembership(ctx, membership.userId, anheart._id);
    if (held === null) moves.push(membership);
    else merges.push({ membership, held });
  }
  const users = await ctx.db
    .query("users")
    .withIndex("by_organization", (q) => q.eq("organizationId", duplicate._id))
    .collect();
  return { clerkOrgId, duplicate, anheart, memberships, merges, moves, users };
}

const repairCounts = {
  movedMemberships: v.number(),
  mergedMemberships: v.number(),
  movedUsers: v.number(),
};

/**
 * Way out of `ANHEART_ORGANIZATION_DUPLICATED` (ANH-225): the Clerk webhook
 * received the Anheart organisation before `ANHEART_ORG_ID` was set and gave it
 * its own row, while the default organisation, which holds every row written
 * before organisations existed, is still unlinked.
 *
 * Only when that row holds nothing but what the webhook writes (memberships,
 * accounts): its memberships go to the default organisation, an account whose
 * main organisation it was now has the default one, the row is deleted and the
 * default organisation is linked to the Clerk organisation, with the name, the
 * slug and the dates the webhook had received for it. That is the state the
 * webhook would have left had the default organisation been linked first.
 *
 * A member already in the default organisation (the migration gave every
 * existing account a membership) keeps that one membership, which takes the
 * role, the state and the Clerk identifiers the webhook received: from
 * `ANHEART_ORG_ID` on, Clerk is the authority on roles.
 *
 * Two steps. Without `apply`, a dry run: nothing is written, and the answer
 * names the organisation that would be absorbed (name, slug, Clerk
 * identifier, members, admins) and the default organisation it would link.
 * Nothing in the database tells the Anheart organisation from a client one
 * that `ANHEART_ORG_ID` names by mistake: the operator checks that name in
 * Clerk. With `apply: true`, it writes only if `expectedName` is exactly that
 * name, and refuses otherwise.
 *
 * It refuses, and changes nothing, when that row holds any other data, when
 * Clerk deleted that organisation, or when `ANHEART_ORG_ID` is unset. Run again
 * once repaired, it finds nothing to do.
 */
export const repairDuplicatedAnheartOrganization = internalMutation({
  args: {
    apply: v.optional(v.boolean()),
    expectedName: v.optional(v.string()),
  },
  returns: v.union(
    v.object({ status: v.literal("nothing_to_repair") }),
    v.object({
      status: v.literal("dry_run"),
      absorbs: v.object({
        organizationId: v.id("organizations"),
        name: v.string(),
        slug: v.string(),
        clerkOrgId: v.string(),
        memberships: v.number(),
        admins: v.number(),
      }),
      links: v.object({
        organizationId: v.id("organizations"),
        name: v.string(),
        slug: v.string(),
      }),
      ...repairCounts,
    }),
    v.object({
      status: v.literal("repaired"),
      organizationId: v.id("organizations"),
      ...repairCounts,
    }),
  ),
  handler: async (ctx, args) => {
    const plan = await planRepair(ctx);
    if (plan === null) return { status: "nothing_to_repair" as const };
    const { clerkOrgId, duplicate, anheart } = plan;
    const counts = {
      movedMemberships: plan.moves.length,
      mergedMemberships: plan.merges.length,
      movedUsers: plan.users.length,
    };
    if (args.apply !== true) {
      return {
        status: "dry_run" as const,
        absorbs: {
          organizationId: duplicate._id,
          name: duplicate.name,
          slug: duplicate.slug,
          clerkOrgId,
          memberships: plan.memberships.length,
          admins: plan.memberships.filter((m) => m.active && m.role === "admin")
            .length,
        },
        links: {
          organizationId: anheart._id,
          name: anheart.name,
          slug: anheart.slug,
        },
        ...counts,
      };
    }
    if (args.expectedName !== duplicate.name) {
      throw new ConvexError(repairNotConfirmed(duplicate.name));
    }

    for (const membership of plan.moves) {
      await ctx.db.patch(membership._id, { organizationId: anheart._id });
    }
    for (const { membership, held } of plan.merges) {
      await ctx.db.patch(held._id, {
        role: membership.role,
        active: membership.active,
        clerkMembershipId: membership.clerkMembershipId,
        clerkUpdatedAt: membership.clerkUpdatedAt,
        clerkEventAt: membership.clerkEventAt,
      });
      await ctx.db.delete(membership._id);
    }
    for (const user of plan.users) {
      await ctx.db.patch(user._id, { organizationId: anheart._id });
    }
    await ctx.db.delete(duplicate._id);
    await ctx.db.patch(anheart._id, {
      clerkOrgId,
      name: duplicate.name,
      slug: duplicate.slug,
      clerkUpdatedAt: duplicate.clerkUpdatedAt,
      clerkEventAt: duplicate.clerkEventAt,
    });
    return {
      status: "repaired" as const,
      organizationId: anheart._id,
      ...counts,
    };
  },
});

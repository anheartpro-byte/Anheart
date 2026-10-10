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
 */
import { v } from "convex/values";
import { internalMutation, type MutationCtx } from "../_generated/server";
import { internal } from "../_generated/api";
import type { Id } from "../_generated/dataModel";
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
 * The default organisation, created on first run. When `ANHEART_ORG_ID` is
 * set, the organisation is the mirror of that Clerk organisation: an existing
 * unlinked default organisation is linked to it rather than duplicated.
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
    if (linked) return linked._id;
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

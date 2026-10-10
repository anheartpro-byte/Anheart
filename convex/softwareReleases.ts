/**
 * ANH-134: the register of released software versions.
 *
 * One row per component version (`pi-0.1.0`, `cloud-0.1.0`, `web-0.1.0`). A Pi
 * row carries how far that version has been validated; `lib/releaseValidation`
 * holds the rule the machine registry and the remote update will apply with it.
 * Only an admin reads or writes this table.
 *
 * ANH-195: the row says which validation level is in force, and
 * `software_release_levels` keeps every level the version has held, with who
 * decided it, when and why. Raising or lowering a level adds a row there; it
 * never erases the decision before it.
 *
 * The register is Anheart-wide, not per organisation (ANH-114): a version is
 * the same for every client, so the tables carry no `organizationId`, and
 * "admin" here is the admin of the Anheart organisation. The admin of a client
 * organisation (`org_admin`) neither reads nor writes it.
 */
import { ConvexError, v } from "convex/values";
import type { Doc } from "./_generated/dataModel";
import {
  internalQuery,
  mutation,
  query,
  type QueryCtx,
} from "./_generated/server";
import { requireRole } from "./lib/auth";
import { isReleaseVersion } from "./lib/releaseValidation";
import { softwareComponentValidator, validationLevelValidator } from "./schema";
import { CLOUD_VERSION } from "./cloudVersion";

const MAX_NOTES_LENGTH = 2000;

/** One validation level a version has held: who decided it, when and why. */
const levelDecisionValidator = v.object({
  validationLevel: validationLevelValidator,
  reason: v.optional(v.string()),
  decidedBy: v.id("users"),
  decidedAt: v.number(),
});

/** The decisions stored for a version, oldest first. */
async function storedLevels(ctx: QueryCtx, release: Doc<"software_releases">) {
  return await ctx.db
    .query("software_release_levels")
    .withIndex("by_release", (q) => q.eq("releaseId", release._id))
    .collect();
}

/**
 * What a row recorded before the history existed says of its own level: the
 * first decision of its history, in the words of the row.
 */
function firstLevelOf(release: Doc<"software_releases">) {
  if (release.validationLevel === undefined) return [];
  return [
    {
      validationLevel: release.validationLevel,
      reason: release.notes,
      decidedBy: release.recordedBy,
      decidedAt: release.updatedAt,
    },
  ];
}

/**
 * Record a released version, or correct the record of one already known
 * (same component and version). Raising or lowering the validation level of a
 * known version must say why in `notes` (the review that decided it), and
 * adds that decision to the history of the version.
 */
export const recordRelease = mutation({
  args: {
    component: softwareComponentValidator,
    version: v.string(), // the git tag, e.g. "pi-0.1.0"
    validationLevel: v.optional(validationLevelValidator), // Pi only
    releasedAt: v.number(), // unix ms
    notes: v.optional(v.string()),
  },
  returns: v.id("software_releases"),
  handler: async (ctx, args) => {
    const admin = await requireRole(ctx, ["admin"]);

    if (!isReleaseVersion(args.component, args.version)) {
      throw new ConvexError(
        `Invalid version: expected ${args.component}-X.Y.Z`,
      );
    }
    if (args.component === "pi" && args.validationLevel === undefined) {
      throw new ConvexError("A Pi version needs a validation level");
    }
    if (args.component !== "pi" && args.validationLevel !== undefined) {
      throw new ConvexError("Only a Pi version carries a validation level");
    }
    if (!Number.isFinite(args.releasedAt) || args.releasedAt <= 0) {
      throw new ConvexError("Invalid release date");
    }
    const notes = args.notes?.trim() || undefined;
    if (notes !== undefined && notes.length > MAX_NOTES_LENGTH) {
      throw new ConvexError(
        `Notes are limited to ${MAX_NOTES_LENGTH} characters`,
      );
    }

    const now = Date.now();
    const level = args.validationLevel;
    const fields = {
      component: args.component,
      version: args.version,
      validationLevel: level,
      releasedAt: args.releasedAt,
      notes,
      recordedBy: admin._id,
      updatedAt: now,
    };
    const decision = { reason: notes, decidedBy: admin._id, decidedAt: now };

    const existing = await ctx.db
      .query("software_releases")
      .withIndex("by_component_and_version", (q) =>
        q.eq("component", args.component).eq("version", args.version),
      )
      .unique();
    if (existing === null) {
      const releaseId = await ctx.db.insert("software_releases", fields);
      if (level !== undefined) {
        await ctx.db.insert("software_release_levels", {
          releaseId,
          validationLevel: level,
          ...decision,
        });
      }
      return releaseId;
    }

    // A Pi row recorded before the history existed first gets its own level
    // written down. What the row says of it (notes, author, date) is about to
    // be replaced, by a level change or by a plain correction. A refusal
    // below undoes this write with the rest of the mutation.
    if (
      existing.component === "pi" &&
      (await storedLevels(ctx, existing)).length === 0
    ) {
      for (const first of firstLevelOf(existing)) {
        await ctx.db.insert("software_release_levels", {
          releaseId: existing._id,
          ...first,
        });
      }
    }
    if (existing.validationLevel !== level) {
      if (notes === undefined) {
        throw new ConvexError(
          "Changing the validation level of a recorded version needs notes",
        );
      }
      if (level !== undefined) {
        await ctx.db.insert("software_release_levels", {
          releaseId: existing._id,
          validationLevel: level,
          ...decision,
        });
      }
    }
    // The row is the record in force: the last write wins there. What it
    // said of an earlier level stays in `software_release_levels`.
    await ctx.db.replace(existing._id, fields);
    return existing._id;
  },
});

/**
 * Recorded versions, most recent first, optionally for one component. Each
 * carries `levelHistory`: every validation level it has held, oldest first
 * (empty for a version that carries no level).
 */
export const listReleases = query({
  args: { component: v.optional(softwareComponentValidator) },
  returns: v.array(
    v.object({
      _id: v.id("software_releases"),
      _creationTime: v.number(),
      component: softwareComponentValidator,
      version: v.string(),
      validationLevel: v.optional(validationLevelValidator),
      releasedAt: v.number(),
      notes: v.optional(v.string()),
      recordedBy: v.id("users"),
      updatedAt: v.number(),
      levelHistory: v.array(levelDecisionValidator),
    }),
  ),
  handler: async (ctx, args) => {
    await requireRole(ctx, ["admin"]);
    const component = args.component;
    const rows =
      component === undefined
        ? await ctx.db.query("software_releases").collect()
        : await ctx.db
            .query("software_releases")
            .withIndex("by_component_and_version", (q) =>
              q.eq("component", component),
            )
            .collect();
    const listed = [];
    for (const row of rows.sort((a, b) => b.releasedAt - a.releasedAt)) {
      const stored = await storedLevels(ctx, row);
      const levelHistory =
        stored.length === 0
          ? firstLevelOf(row)
          : stored.map(({ validationLevel, reason, decidedBy, decidedAt }) => ({
              validationLevel,
              reason,
              decidedBy,
              decidedAt,
            }));
      listed.push({ ...row, levelHistory });
    }
    return listed;
  },
});

/**
 * The version of the backend that is deployed. Internal: read it with
 * `npx convex run softwareReleases:deployedCloudVersion` (deploy key required).
 */
export const deployedCloudVersion = internalQuery({
  args: {},
  returns: v.string(),
  handler: async () => CLOUD_VERSION,
});

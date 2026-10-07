/**
 * ANH-134: the register of released software versions.
 *
 * One row per component version (`pi-0.1.0`, `cloud-0.1.0`, `web-0.1.0`). A Pi
 * row carries how far that version has been validated; `lib/releaseValidation`
 * holds the rule the machine registry and the remote update will apply with it.
 * Only an admin reads or writes this table.
 */
import { ConvexError, v } from "convex/values";
import { internalQuery, mutation, query } from "./_generated/server";
import { requireRole } from "./lib/auth";
import { isReleaseVersion } from "./lib/releaseValidation";
import { softwareComponentValidator, validationLevelValidator } from "./schema";
import { CLOUD_VERSION } from "./cloudVersion";

const MAX_NOTES_LENGTH = 2000;

/**
 * Record a released version, or correct the record of one already known
 * (same component and version). Raising or lowering the validation level of a
 * known version must say why in `notes` (the review that decided it).
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

    const fields = {
      component: args.component,
      version: args.version,
      validationLevel: args.validationLevel,
      releasedAt: args.releasedAt,
      notes,
      recordedBy: admin._id,
      updatedAt: Date.now(),
    };

    const existing = await ctx.db
      .query("software_releases")
      .withIndex("by_component_and_version", (q) =>
        q.eq("component", args.component).eq("version", args.version),
      )
      .unique();
    if (existing === null) {
      return await ctx.db.insert("software_releases", fields);
    }
    if (
      existing.validationLevel !== args.validationLevel &&
      notes === undefined
    ) {
      throw new ConvexError(
        "Changing the validation level of a recorded version needs notes",
      );
    }
    await ctx.db.replace(existing._id, fields);
    return existing._id;
  },
});

/** Recorded versions, most recent first, optionally for one component. */
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
    return rows.sort((a, b) => b.releasedAt - a.releasedAt);
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

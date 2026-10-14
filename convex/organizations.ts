import { query } from "./_generated/server";
import { v } from "convex/values";
import { getCurrentUserOrThrow } from "./lib/auth";

/**
 * The organisations a machine can be created in, for the Anheart admin (the
 * only role that creates machines): every organisation the server knows and
 * that Clerk has not deleted, by name. Anyone else gets an empty list: an
 * organisation admin works in their own organisation only.
 */
export const listOrganizations = query({
  args: {},
  returns: v.array(
    v.object({
      _id: v.id("organizations"),
      name: v.string(),
      slug: v.string(),
    }),
  ),
  handler: async (ctx) => {
    const currentUser = await getCurrentUserOrThrow(ctx);
    if (currentUser.role !== "admin") return [];
    const organizations = await ctx.db.query("organizations").collect();
    return organizations
      .filter((organization) => organization.deletedAt === undefined)
      .map(({ _id, name, slug }) => ({ _id, name, slug }))
      .sort((a, b) => a.name.localeCompare(b.name));
  },
});

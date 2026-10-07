import { v } from "convex/values";
import type { QueryCtx } from "../_generated/server";
import { apiKeySelector, verifyApiKey } from "./crypto";

export const authenticatedMachine = v.union(
  v.object({
    _id: v.id("machines"),
    name: v.string(),
    status: v.string(),
  }),
  v.null(),
);

export async function authenticateMachine(ctx: QueryCtx, apiKey: string) {
  const selector = apiKeySelector(apiKey);
  if (!selector) return null;
  const machine = await ctx.db
    .query("machines")
    .withIndex("by_apiKeySelector", (q) => q.eq("apiKeySelector", selector))
    .unique();
  if (!machine || machine.isDeleted || machine.authenticationEnabled === false)
    return null;
  if (!(await verifyApiKey(apiKey, machine.apiKey))) return null;
  return {
    _id: machine._id,
    name: machine.name,
    status: machine.status,
  };
}

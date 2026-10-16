/**
 * Data migration of ANH-165: the single key of each machine
 * (`machines.apiKey`, `machines.apiKeySelector`) moves into the table
 * `machine_api_keys`, where keys are rotated, revoked and expire.
 *
 * One internal action, run by hand once per deployment after this code is
 * deployed (never from the application, never by a schedule):
 *
 *     npx convex run migrations/machineApiKeys:moveMachineKeysToTable
 *
 * Nothing is interrupted, before or after it: until it has run, a key still
 * in `machines` opens the machine routes as before (convex/lib/machineAuth.ts);
 * after it, the same key opens them as a row of the table. Idempotent: a
 * second run moves and discards nothing, and reports any conflicts again.
 * The action calls one internal mutation per page, each in its own transaction,
 * until every machine is checked. Optional `pageSize` is an integer from 1
 * to 1000 (100 by default). No scheduling: after an interruption, rerun the
 * same command; pages already committed need no key moved again.
 *
 * A value that is not a verifier with its selector (the reversible encodings
 * refused since ANH-121) opens nothing: it is cleared, not moved, and counted
 * in `legacyDiscarded`. Such a machine needs a new key
 * (`machineKeys.createMachineKey`).
 * A selector already in the table counts as moved only for the same machine.
 * Otherwise `selectorConflicts` counts it and nothing is cleared or inserted:
 * the table alone decides which machine that selector can open.
 */
import { paginationOptsValidator } from "convex/server";
import { ConvexError, v, type Infer } from "convex/values";
import { internal } from "../_generated/api";
import { internalAction, internalMutation } from "../_generated/server";
import { moveLegacyKey } from "../lib/machineKeys";

const counts = v.object({
  machinesChecked: v.number(),
  keysMoved: v.number(),
  legacyDiscarded: v.number(),
  selectorConflicts: v.number(),
});
const pageResult = counts.extend({
  isDone: v.boolean(),
  continueCursor: v.string(),
});

/** One committed page; its cursor follows creation order, unaffected by clearing keys. */
export const moveMachineKeysPage = internalMutation({
  args: { paginationOpts: paginationOptsValidator },
  returns: pageResult,
  handler: async (ctx, { paginationOpts }) => {
    const now = Date.now();
    const { page, isDone, continueCursor } = await ctx.db
      .query("machines")
      .paginate(paginationOpts);
    let keysMoved = 0;
    let legacyDiscarded = 0;
    let selectorConflicts = 0;
    for (const machine of page) {
      const outcome = await moveLegacyKey(ctx, machine, now);
      if (outcome === "moved") keysMoved += 1;
      if (outcome === "discarded") legacyDiscarded += 1;
      if (outcome === "conflict") selectorConflicts += 1;
    }
    return {
      machinesChecked: page.length,
      keysMoved,
      legacyDiscarded,
      selectorConflicts,
      isDone,
      continueCursor,
    };
  },
});

export const moveMachineKeysToTable = internalAction({
  args: { pageSize: v.optional(v.number()) },
  returns: counts,
  handler: async (ctx, args): Promise<Infer<typeof counts>> => {
    const pageSize = args.pageSize ?? 100;
    if (!Number.isInteger(pageSize) || pageSize < 1 || pageSize > 1000) {
      throw new ConvexError(
        "Machine key migration pageSize must be an integer from 1 to 1000.",
      );
    }
    const total = {
      machinesChecked: 0,
      keysMoved: 0,
      legacyDiscarded: 0,
      selectorConflicts: 0,
    };
    let cursor: string | null = null;
    for (;;) {
      const page: Infer<typeof pageResult> = await ctx.runMutation(
        internal.migrations.machineApiKeys.moveMachineKeysPage,
        { paginationOpts: { cursor, numItems: pageSize } },
      );
      total.machinesChecked += page.machinesChecked;
      total.keysMoved += page.keysMoved;
      total.legacyDiscarded += page.legacyDiscarded;
      total.selectorConflicts += page.selectorConflicts;
      if (page.isDone) return total;
      cursor = page.continueCursor;
    }
  },
});

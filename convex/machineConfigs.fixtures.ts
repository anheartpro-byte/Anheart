import type { Id } from "./_generated/dataModel";
import { canonicalConfig, configHash, type ConfigValues } from "./lib/machineConfig";
import type { World } from "./test.setup";
import cases from "../contracts/machine-config-cases.json";

/** A configuration the console accepts: the shared cases' base (ANH-141). */
export const MACHINE_CONFIG_BASE = cases.base as ConfigValues;

/**
 * A revision of `machineId`'s configuration as the mutations leave it: of
 * `status`, by `author`, `values` the shared base unless given.
 */
export async function insertConfigRevision(
  w: World,
  {
    status,
    author,
    revision,
    values = MACHINE_CONFIG_BASE,
    machineId = w.machine,
    role = "admin",
  }: {
    status: "draft" | "approved" | "applied";
    author: Id<"users">;
    revision: number;
    values?: ConfigValues;
    machineId?: Id<"machines">;
    role?: "admin" | "org_admin";
  },
) {
  const canonical = canonicalConfig(values);
  const hash = await configHash(canonical);
  const at = Date.now();
  return await w.t.run(async (ctx) =>
    ctx.db.insert("machine_configs", {
      organizationId: (await ctx.db.get(machineId))?.organizationId,
      machineId,
      revision,
      values,
      canonical,
      hash,
      status,
      changedKeys: [],
      proposedBy: author,
      proposedByRole: role,
      proposedAt: at,
      ...(status === "draft" ? {} : { approvedBy: [author], approvedAt: at }),
      ...(status === "applied" ? { appliedAt: at, appliedHash: hash } : {}),
    }),
  );
}

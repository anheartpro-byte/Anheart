import type { ActionCtx } from "../_generated/server";
import { internal } from "../_generated/api";
import type { Infer } from "convex/values";
import type { authenticatedMachine } from "./machineAuth";

export async function validateMachineAuth(
  ctx: ActionCtx,
  req: Request,
): Promise<
  | { readonly machine: Exclude<Infer<typeof authenticatedMachine>, null> }
  | { readonly error: Response }
> {
  const authHeader = req.headers.get("Authorization");
  if (!authHeader?.startsWith("Bearer ")) {
    return {
      error: Response.json(
        { error: "Missing Authorization header" },
        { status: 401 },
      ),
    };
  }
  const machine = await ctx.runQuery(internal.machines.getMachineByApiKey, {
    apiKey: authHeader.substring(7),
  });
  if (!machine) {
    return {
      error: Response.json({ error: "Invalid API key" }, { status: 401 }),
    };
  }
  return { machine };
}

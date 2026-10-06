import type { ActionCtx } from "../_generated/server";
import { internal } from "../_generated/api";
import type { Infer } from "convex/values";
import type { authenticatedMachine } from "./machineAuth";
import {
  contractUnsupported,
  machineErrorResponse,
  servedContract,
} from "./contract";

/**
 * The one gate every machine route passes: the machine's key first, then the
 * contract it announces (`X-Anheart-Contract`). A request that fails either is
 * answered here and never reaches the route's own code. `contract` is the
 * version the machine announced, once its major is known to be served.
 */
export async function validateMachineAuth(
  ctx: ActionCtx,
  req: Request,
): Promise<
  | {
      readonly machine: Exclude<Infer<typeof authenticatedMachine>, null>;
      readonly contract: string;
    }
  | { readonly error: Response }
> {
  const authHeader = req.headers.get("Authorization");
  if (!authHeader?.startsWith("Bearer ")) {
    return {
      error: machineErrorResponse(
        401,
        "unauthorized",
        "Missing Authorization header",
      ),
    };
  }
  const machine = await ctx.runQuery(internal.machines.getMachineByApiKey, {
    apiKey: authHeader.substring(7),
  });
  if (!machine) {
    return {
      error: machineErrorResponse(401, "unauthorized", "Invalid API key"),
    };
  }
  const contract = servedContract(req);
  if (contract === null) return { error: contractUnsupported() };
  return { machine, contract };
}

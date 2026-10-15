import type { ActionCtx } from "../_generated/server";
import { internal } from "../_generated/api";
import type { Infer } from "convex/values";
import type { authenticatedMachine } from "./machineAuth";
import {
  contractUnsupported,
  machineErrorResponse,
  servedContract,
} from "./contract";

type Machine = Omit<
  Exclude<Infer<typeof authenticatedMachine>, null>,
  "keyUseDue"
>;

/**
 * The machine's key, and nothing else. On its own it is the gate of the one
 * route that must answer whatever contract the machine announces (the stop
 * request, see `convex/http.ts`); every other route goes through
 * `validateMachineAuth`.
 *
 * Authenticating only reads. When the key's last recorded use is a minute old
 * or more, this use is written next (`machineKeys.recordKeyUse`): a machine
 * that sends a request every few seconds writes it once a minute, not with
 * every request.
 */
export async function authenticateMachineRequest(
  ctx: ActionCtx,
  req: Request,
): Promise<{ readonly machine: Machine } | { readonly error: Response }> {
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
  const found = await ctx.runQuery(internal.machines.getMachineByApiKey, {
    apiKey: authHeader.substring(7),
  });
  if (!found) {
    return {
      error: machineErrorResponse(401, "unauthorized", "Invalid API key"),
    };
  }
  const { keyUseDue, ...machine } = found;
  if (keyUseDue !== undefined) {
    await ctx.runMutation(internal.machineKeys.recordKeyUse, {
      keyId: keyUseDue,
    });
  }
  return { machine };
}

/**
 * The gate of a machine route: the machine's key first, then the contract it
 * announces (`X-Anheart-Contract`). A request that fails either is answered
 * here and never reaches the route's own code. `contract` is the version the
 * machine announced, once its major is known to be served.
 */
export async function validateMachineAuth(
  ctx: ActionCtx,
  req: Request,
): Promise<
  | { readonly machine: Machine; readonly contract: string }
  | { readonly error: Response }
> {
  const authenticated = await authenticateMachineRequest(ctx, req);
  if ("error" in authenticated) return authenticated;
  const contract = servedContract(req);
  if (contract === null) return { error: contractUnsupported() };
  return { machine: authenticated.machine, contract };
}

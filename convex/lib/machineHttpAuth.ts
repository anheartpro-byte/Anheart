import type { ActionCtx } from "../_generated/server";
import { internal } from "../_generated/api";
import type { Infer } from "convex/values";
import type { authenticatedMachine } from "./machineAuth";
import { keyIsValid, keyUseIsDue } from "./machineKeys";
import {
  contractUnsupported,
  machineErrorResponse,
  servedContract,
} from "./contract";

type Machine = Omit<Exclude<Infer<typeof authenticatedMachine>, null>, "key">;

/**
 * The machine's key, and nothing else. On its own it is the gate of the one
 * route that must answer whatever contract the machine announces (the stop
 * request, see `convex/http.ts`); every other route goes through
 * `validateMachineAuth`.
 *
 * The action checks expiry and the last use on its own clock: cached query
 * results must never reopen an expired key or defer recording a due use.
 * A due use is attempted before the contract and limiter, even for a later
 * 426, 429, 413 or 400: the key was presented and verified. Its last use tells
 * whether a replaced or stolen key still serves; even a console of another
 * major uses its key to ask for a stop. This does not spend a rate limit token.
 * A failed accounting write is logged without its exception (which may carry
 * secrets) and ignored, including for stop and end; the next request retries
 * the still-due use. The mutation also checks the minute in its transaction.
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
  const now = Date.now();
  if (!found || (found.key !== undefined && !keyIsValid(found.key, now))) {
    return {
      error: machineErrorResponse(401, "unauthorized", "Invalid API key"),
    };
  }
  const { key, ...machine } = found;
  if (key !== undefined && keyUseIsDue(key, now)) {
    try {
      await ctx.runMutation(internal.machineKeys.recordKeyUse, {
        keyId: key.id,
      });
    } catch {
      console.warn(`Failed to record machine key use (${key.id})`);
    }
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

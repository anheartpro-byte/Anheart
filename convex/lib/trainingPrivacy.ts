import type { Doc } from "../_generated/dataModel";
import type { QueryCtx } from "../_generated/server";
import { canAccessMachine } from "./auth";

export async function authorizedMachineLive(
  ctx: QueryCtx,
  user: Doc<"users">,
  machine: Doc<"machines">,
): Promise<NonNullable<Doc<"machines">["live"]> | null> {
  const live = machine.live;
  if (!live) return null;
  if (await canAccessMachine(ctx, machine._id)) return live;
  if (!live.sessionId) return null;
  const sessionId = ctx.db.normalizeId("sessions", live.sessionId);
  if (!sessionId) return null;
  const session = await ctx.db.get(sessionId);
  return session?.machineId === machine._id && session.userId === user._id
    ? live
    : null;
}

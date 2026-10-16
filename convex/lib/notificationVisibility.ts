/**
 * Visibility and bounded reads of alerts (ANH-237), shared by the page, the
 * bell and read mutations. A machine never changes organisation: alerts and
 * gestionnaire links therefore keep its organisation without reading its
 * live document. Logical deletion preserves access, as it does for sessions.
 */
import type { Doc, Id } from "../_generated/dataModel";
import type { QueryCtx } from "../_generated/server";
import type { CurrentUser } from "./auth";

export const LIST_LIMIT = 50;
export const UNREAD_COUNT_LIMIT = 100;
/** Maximum notification rows read and marked in one mark-all transaction. */
export const MARK_ALL_READ_LIMIT = 500;

export async function notificationVisibility(
  ctx: QueryCtx,
  caller: CurrentUser,
) {
  let machineIds: Set<Id<"machines">> | null;
  switch (caller.role) {
    case "admin":
    case "org_admin":
      machineIds = null;
      break;
    case "gestionnaire": {
      const links = await ctx.db
        .query("machine_gestionnaires")
        .withIndex("by_gestionnaire", (q) => q.eq("gestionnaireId", caller._id))
        .collect();
      machineIds = new Set(
        links
          .filter((link) => link.organizationId === caller.organizationId)
          .map((link) => link.machineId),
      );
      break;
    }
    case "user":
      machineIds = new Set();
      break;
  }
  return {
    machineIds,
    allows: (row: Doc<"notifications">) =>
      row.userId === caller._id &&
      row.organizationId === caller.organizationId &&
      (machineIds === null || machineIds.has(row.machineId)),
  };
}

export async function visibleUnread(
  ctx: QueryCtx,
  caller: CurrentUser,
  limit: number,
): Promise<Doc<"notifications">[]> {
  const { machineIds } = await notificationVisibility(ctx, caller);
  if (machineIds === null) {
    return await ctx.db
      .query("notifications")
      .withIndex("by_user_and_organization_and_read", (q) =>
        q
          .eq("userId", caller._id)
          .eq("organizationId", caller.organizationId)
          .eq("read", false),
      )
      .take(limit);
  }
  const rows: Doc<"notifications">[] = [];
  for (const machineId of machineIds) {
    const unread = await ctx.db
      .query("notifications")
      .withIndex("by_user_and_organization_and_machine_and_read", (q) =>
        q
          .eq("userId", caller._id)
          .eq("organizationId", caller.organizationId)
          .eq("machineId", machineId)
          .eq("read", false),
      )
      .take(limit - rows.length);
    rows.push(...unread);
    if (rows.length === limit) break;
  }
  return rows;
}

export async function recentVisible(
  ctx: QueryCtx,
  caller: CurrentUser,
): Promise<Doc<"notifications">[]> {
  const { machineIds } = await notificationVisibility(ctx, caller);
  if (machineIds === null) {
    return await ctx.db
      .query("notifications")
      .withIndex("by_user_and_organization_and_updated", (q) =>
        q.eq("userId", caller._id).eq("organizationId", caller.organizationId),
      )
      .order("desc")
      .take(LIST_LIMIT);
  }
  const perMachine = await Promise.all(
    [...machineIds].map((machineId) =>
      ctx.db
        .query("notifications")
        .withIndex("by_user_and_organization_and_machine_and_updated", (q) =>
          q
            .eq("userId", caller._id)
            .eq("organizationId", caller.organizationId)
            .eq("machineId", machineId),
        )
        .order("desc")
        .take(LIST_LIMIT),
    ),
  );
  return perMachine
    .flat()
    .sort(
      (a, b) => b.updatedAt - a.updatedAt || b._creationTime - a._creationTime,
    )
    .slice(0, LIST_LIMIT);
}

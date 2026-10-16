/**
 * Visibility and bounded reads of alerts (ANH-237), shared by the page, the
 * bell and read mutations. A machine never changes organisation: alerts and
 * gestionnaire links therefore keep its organisation without reading its
 * live document. Logical deletion preserves access, as it does for sessions.
 */
import type { Doc, Id } from "../_generated/dataModel";
import type { MutationCtx, QueryCtx } from "../_generated/server";
import { internal } from "../_generated/api";
import {
  effectiveRole,
  getCurrentUser,
  organizationRoleOf,
  requireRole,
  STAFF_ROLES,
  type CurrentUser,
} from "./auth";
import { limitUser } from "./rateLimits";

export const LIST_LIMIT = 50;
export const UNREAD_COUNT_LIMIT = 100;
/** Maximum notification rows read and marked in one mark-all transaction. */
export const MARK_ALL_READ_LIMIT = 500;

export function staffOrNull(caller: CurrentUser): CurrentUser | null {
  return STAFF_ROLES.includes(caller.role) ? caller : null;
}

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

export async function unreadCountHandler(
  ctx: QueryCtx,
): Promise<number | null> {
  const current = await getCurrentUser(ctx);
  if (current === null || current.session === null) return null;
  const caller = staffOrNull({
    ...current.user,
    role: current.session.role,
    organizationId: current.session.organization._id,
  });
  if (caller === null) return null;
  return (await visibleUnread(ctx, caller, UNREAD_COUNT_LIMIT)).length;
}

async function markVisibleReadBatch(
  ctx: MutationCtx,
  caller: CurrentUser,
): Promise<{ marked: number }> {
  const unread = await visibleUnread(ctx, caller, MARK_ALL_READ_LIMIT);
  for (const row of unread) await ctx.db.patch(row._id, { read: true });
  if (unread.length === MARK_ALL_READ_LIMIT) {
    await ctx.scheduler.runAfter(
      0,
      internal.notifications.continueMarkAllRead,
      {
        userId: caller._id,
        organizationId: caller.organizationId,
      },
    );
  }
  return { marked: unread.length };
}

export async function markAllReadHandler(
  ctx: MutationCtx,
): Promise<{ marked: number }> {
  await limitUser(ctx);
  const caller = await requireRole(ctx, STAFF_ROLES);
  return await markVisibleReadBatch(ctx, caller);
}

export async function continueMarkAllReadHandler(
  ctx: MutationCtx,
  {
    userId,
    organizationId,
  }: {
    readonly userId: Id<"users">;
    readonly organizationId: Id<"organizations">;
  },
): Promise<{ marked: number }> {
  const user = await ctx.db.get(userId);
  const organization = await ctx.db.get(organizationId);
  const role = await organizationRoleOf(ctx, userId, organizationId);
  if (
    !user ||
    user.clerkDeletedAt !== undefined ||
    !organization ||
    organization.deletedAt !== undefined ||
    role === null
  ) {
    return { marked: 0 };
  }
  const caller = staffOrNull({
    ...user,
    organizationId,
    role: effectiveRole(organization, role),
  });
  return caller === null
    ? { marked: 0 }
    : await markVisibleReadBatch(ctx, caller);
}

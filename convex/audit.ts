import {
  paginationOptsValidator,
  paginationResultValidator,
  type Expression,
  type IndexRange,
  type IndexRangeBuilder,
} from "convex/server";
import { v } from "convex/values";
import type { Doc, Id } from "./_generated/dataModel";
import { query, type QueryCtx } from "./_generated/server";
import { ORGANIZATION_ADMIN_ROLES, requireRole } from "./lib/auth";
import {
  auditActionValidator,
  auditActorRoleValidator,
  auditTargetTypeValidator,
} from "./lib/audit";
import { chooseAuditIndex, type AuditIndexFilters } from "./lib/auditIndex";
import schema from "./schema";

const rowValidator = schema
  .doc("audit_log")
  .omit("_creationTime")
  .extend({
    organizationName: v.union(v.string(), v.null()),
    actorLabel: v.union(v.string(), v.null()),
    targetLabel: v.union(v.string(), v.null()),
  });

type Period = { from?: number; to?: number };

function rangeOf(ctx: QueryCtx, filters: AuditIndexFilters & Period) {
  const {
    organizationId,
    actorId,
    actorRole,
    action,
    targetType,
    targetId,
    from,
    to,
  } = filters;
  const rows = ctx.db.query("audit_log");
  const period = (
    q: IndexRange &
      Pick<IndexRangeBuilder<Doc<"audit_log">, ["at"]>, "gte" | "lt">,
  ) => {
    if (from !== undefined) {
      const lower = q.gte("at", from);
      return to === undefined ? lower : lower.lt("at", to);
    }
    return to === undefined ? q : q.lt("at", to);
  };
  switch (chooseAuditIndex(filters)) {
    case "by_organization_and_target_and_at":
      return rows.withIndex("by_organization_and_target_and_at", (q) =>
        period(
          q
            .eq("organizationId", organizationId)
            .eq("targetType", targetType!)
            .eq("targetId", targetId!),
        ),
      );
    case "by_target_and_at":
      return rows.withIndex("by_target_and_at", (q) =>
        period(q.eq("targetType", targetType!).eq("targetId", targetId!)),
      );
    case "by_organization_and_actor_and_at":
      return rows.withIndex("by_organization_and_actor_and_at", (q) =>
        period(q.eq("organizationId", organizationId).eq("actorId", actorId)),
      );
    case "by_actor_and_at":
      return rows.withIndex("by_actor_and_at", (q) =>
        period(q.eq("actorId", actorId)),
      );
    case "by_organization_and_actor_role_and_at":
      return rows.withIndex("by_organization_and_actor_role_and_at", (q) =>
        period(
          q.eq("organizationId", organizationId).eq("actorRole", actorRole!),
        ),
      );
    case "by_actor_role_and_at":
      return rows.withIndex("by_actor_role_and_at", (q) =>
        period(q.eq("actorRole", actorRole!)),
      );
    case "by_organization_and_action_and_at":
      return rows.withIndex("by_organization_and_action_and_at", (q) =>
        period(q.eq("organizationId", organizationId).eq("action", action!)),
      );
    case "by_action_and_at":
      return rows.withIndex("by_action_and_at", (q) =>
        period(q.eq("action", action!)),
      );
    case "by_organization_and_target_type_and_at":
      return rows.withIndex("by_organization_and_target_type_and_at", (q) =>
        period(
          q.eq("organizationId", organizationId).eq("targetType", targetType!),
        ),
      );
    case "by_target_type_and_at":
      return rows.withIndex("by_target_type_and_at", (q) =>
        period(q.eq("targetType", targetType!)),
      );
    case "by_organization_and_at":
      return rows.withIndex("by_organization_and_at", (q) =>
        period(q.eq("organizationId", organizationId)),
      );
    case "by_at":
      return rows.withIndex("by_at", period);
  }
}

async function labelOf(ctx: QueryCtx, id: Id<"users"> | Id<"machines"> | null) {
  if (id === null) return null;
  const record = await ctx.db.get(id);
  if (record === null) return null;
  return "firstName" in record
    ? `${record.firstName} ${record.lastName}`
    : record.name;
}

export const list = query({
  args: {
    paginationOpts: paginationOptsValidator,
    organizationId: v.optional(v.id("organizations")),
    actorId: v.optional(v.union(v.id("users"), v.id("machines"))),
    actorRole: v.optional(auditActorRoleValidator),
    action: v.optional(auditActionValidator),
    targetType: v.optional(auditTargetTypeValidator),
    targetId: v.optional(v.string()),
    from: v.optional(v.number()),
    to: v.optional(v.number()),
  },
  returns: paginationResultValidator(rowValidator),
  handler: async (ctx, { paginationOpts, ...filters }) => {
    const caller = await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);
    if (
      (caller.role !== "admin" &&
        filters.organizationId !== undefined &&
        filters.organizationId !== caller.organizationId) ||
      (filters.targetId !== undefined && filters.targetType === undefined) ||
      (filters.from !== undefined &&
        filters.to !== undefined &&
        filters.from >= filters.to)
    )
      return { page: [], isDone: true, continueCursor: "" };
    const organizationId =
      caller.role === "admin" ? filters.organizationId : caller.organizationId;
    const page = await rangeOf(ctx, { ...filters, organizationId })
      .order("desc")
      .filter((q) => {
        const predicates: Expression<boolean>[] = [];
        if (filters.actorId !== undefined)
          predicates.push(q.eq(q.field("actorId"), filters.actorId));
        if (filters.actorRole !== undefined)
          predicates.push(q.eq(q.field("actorRole"), filters.actorRole));
        if (filters.action !== undefined)
          predicates.push(q.eq(q.field("action"), filters.action));
        if (filters.targetType !== undefined)
          predicates.push(q.eq(q.field("targetType"), filters.targetType));
        if (filters.targetId !== undefined)
          predicates.push(q.eq(q.field("targetId"), filters.targetId));
        return predicates.length === 0 ? true : q.and(...predicates);
      })
      .paginate(paginationOpts);
    return {
      ...page,
      page: await Promise.all(
        page.page.map(async (row) => {
          const organization = row.organizationId
            ? await ctx.db.get(row.organizationId)
            : null;
          const targetId =
            row.targetType === "user" || row.targetType === "machine"
              ? ctx.db.normalizeId(
                  row.targetType === "user" ? "users" : "machines",
                  row.targetId,
                )
              : null;
          return {
            _id: row._id,
            at: row.at,
            organizationId: row.organizationId,
            actorId: row.actorId,
            actorRole: row.actorRole,
            action: row.action,
            targetType: row.targetType,
            targetId: row.targetId,
            details: row.details,
            requestId: row.requestId,
            source: row.source,
            organizationName: organization?.name ?? null,
            actorLabel: await labelOf(ctx, row.actorId ?? null),
            targetLabel: await labelOf(ctx, targetId),
          };
        }),
      ),
    };
  },
});

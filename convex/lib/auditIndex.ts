import type { Id } from "../_generated/dataModel";
import type { AuditAction, AuditTargetType } from "./audit";

export type AuditIndexFilters = {
  organizationId?: Id<"organizations">;
  actorId?: Id<"users"> | Id<"machines">;
  action?: AuditAction;
  targetType?: AuditTargetType;
  targetId?: string;
};

export function chooseAuditIndex(filters: AuditIndexFilters) {
  const scoped = filters.organizationId !== undefined;
  if (filters.targetType !== undefined && filters.targetId !== undefined) {
    return scoped ? "by_organization_and_target_and_at" : "by_target_and_at";
  }
  if (filters.actorId !== undefined) {
    return scoped ? "by_organization_and_actor_and_at" : "by_actor_and_at";
  }
  if (filters.action !== undefined) {
    return scoped ? "by_organization_and_action_and_at" : "by_action_and_at";
  }
  if (filters.targetType !== undefined) {
    return scoped
      ? "by_organization_and_target_type_and_at"
      : "by_target_type_and_at";
  }
  return scoped ? "by_organization_and_at" : "by_at";
}

import { useQuery } from "convex/react";
import { useTranslations } from "next-intl";
import { api } from "@/convex/_generated/api";
import {
  AUDIT_ACTIONS,
  AUDIT_TARGET_TYPES,
  type AuditAction,
  type AuditTargetType,
} from "@/convex/lib/audit";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export type AuditFilterState = {
  actor: string;
  action: AuditAction | "";
  targetType: AuditTargetType | "";
  targetId: string;
  targetLabel: string;
  organizationId: string;
  from: string;
  to: string;
};

export const EMPTY_AUDIT_FILTERS: AuditFilterState = {
  actor: "",
  action: "",
  targetType: "",
  targetId: "",
  targetLabel: "",
  organizationId: "",
  from: "",
  to: "",
};

function Choice({
  id,
  label,
  value,
  onChange,
  options,
  all,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: Array<{ value: string; label: string }>;
  all: string;
}) {
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      <Select
        value={value || "all"}
        onValueChange={(value) => onChange(value === "all" ? "" : value)}
      >
        <SelectTrigger id={id} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">{all}</SelectItem>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

export function AuditFilters({
  value,
  onChange,
  admin,
}: {
  value: AuditFilterState;
  onChange: (value: AuditFilterState) => void;
  admin: boolean;
}) {
  const t = useTranslations("audit");
  const people = useQuery(api.users.listUsers, {});
  const machines = useQuery(api.machines.listMachines, {});
  const organizations = useQuery(
    api.organizations.listOrganizations,
    admin ? {} : "skip",
  );
  const patch = (next: Partial<AuditFilterState>) =>
    onChange({ ...value, ...next });
  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Choice
          id="audit-actor"
          label={t("filters.actor")}
          value={value.actor}
          all={t("all")}
          onChange={(actor) => patch({ actor })}
          options={[
            { value: "system", label: t("roles.system") },
            { value: "machine", label: t("filters.allMachines") },
            ...(people ?? []).map((person) => ({
              value: person._id,
              label: `${person.firstName} ${person.lastName}`,
            })),
            ...(machines ?? []).map((machine) => ({
              value: machine._id,
              label: machine.name,
            })),
          ]}
        />
        <Choice
          id="audit-action"
          label={t("filters.action")}
          value={value.action}
          all={t("all")}
          onChange={(action) => patch({ action: action as AuditAction | "" })}
          options={AUDIT_ACTIONS.map((action) => ({
            value: action,
            label: t(`actions.${action}`),
          }))}
        />
        <Choice
          id="audit-target"
          label={t("filters.targetType")}
          value={value.targetType}
          all={t("all")}
          onChange={(targetType) =>
            patch({
              targetType: targetType as AuditTargetType | "",
              targetId: "",
              targetLabel: "",
            })
          }
          options={AUDIT_TARGET_TYPES.map((type) => ({
            value: type,
            label: t(`targets.${type}`),
          }))}
        />
        <div className="space-y-2">
          <Label htmlFor="audit-from">{t("filters.from")}</Label>
          <Input
            id="audit-from"
            type="date"
            value={value.from}
            onChange={(event) => patch({ from: event.target.value })}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="audit-to">{t("filters.to")}</Label>
          <Input
            id="audit-to"
            type="date"
            value={value.to}
            onChange={(event) => patch({ to: event.target.value })}
          />
        </div>
        {admin && (
          <Choice
            id="audit-organization"
            label={t("filters.organization")}
            value={value.organizationId}
            all={t("all")}
            onChange={(organizationId) => patch({ organizationId })}
            options={(organizations ?? []).map((organization) => ({
              value: organization._id,
              label: organization.name,
            }))}
          />
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {value.targetId && (
          <Badge variant="secondary">
            {t("filters.target", {
              target: value.targetLabel || value.targetId.slice(0, 12),
            })}
            <Button
              variant="ghost"
              size="sm"
              aria-label={t("filters.removeTarget")}
              onClick={() => patch({ targetId: "", targetLabel: "" })}
            >
              x
            </Button>
          </Badge>
        )}
        <Button variant="outline" onClick={() => onChange(EMPTY_AUDIT_FILTERS)}>
          {t("filters.reset")}
        </Button>
      </div>
    </div>
  );
}

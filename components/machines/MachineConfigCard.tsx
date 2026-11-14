"use client";

import { useId, useState } from "react";
import { useQuery } from "convex/react";
import type { FunctionReturnType } from "convex/server";
import { useLocale, useTranslations } from "next-intl";
import { format } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import {
  canonicalConfig,
  changedKeys,
  CONFIG_KEY_NAMES,
  CONFIG_KEYS,
  machineConfigProblems,
  ORGANIZATION_KEYS,
  type ConfigKeySpec,
  type ConfigProblem,
  type ConfigValue,
  type ConfigValues,
} from "@/convex/lib/machineConfig";
import { useMutationWithFeedback } from "@/hooks/use-mutation-with-feedback";
import { isAnheartAdmin, isOrganizationAdmin } from "@/lib/organizationAccess";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";

export type MachineConfigView = NonNullable<
  FunctionReturnType<typeof api.machineConfigs.getMachineConfig>
>;
type Revision = MachineConfigView["revisions"][number];

/** A value as the console would read it, from what a field holds. */
function valueOf(spec: ConfigKeySpec, raw: string | boolean | string[]): ConfigValue {
  if (typeof raw === "boolean" || Array.isArray(raw)) return raw;
  if (raw === "") return null;
  return spec.type === "integer" || spec.type === "number" ? Number(raw) : raw;
}

/** What a field holds for a value. */
function fieldOf(spec: ConfigKeySpec, value: ConfigValue): string | boolean | string[] {
  if (spec.type === "boolean") return value === true;
  if (spec.type === "enum_list") return Array.isArray(value) ? value : [];
  return value === null || Array.isArray(value) ? "" : String(value);
}

const shown = (value: ConfigValue | undefined) =>
  value === undefined || value === null
    ? "-"
    : Array.isArray(value)
      ? value.join(", ")
      : String(value);

function ConfigField({
  name,
  value,
  onChange,
  disabled,
  problems,
}: {
  name: string;
  value: string | boolean | string[];
  onChange: (value: string | boolean | string[]) => void;
  disabled: boolean;
  problems: ConfigProblem[];
}) {
  const t = useTranslations("machineConfig");
  const id = useId();
  const spec = CONFIG_KEYS[name];
  const errorId = `${id}-error`;
  const invalid = problems.length > 0;
  const label = (
    <Label htmlFor={id}>
      {t(`keys.${name}`)} <code className="text-xs">{name}</code>
      {spec.family === "safety" && (
        <Badge variant="destructive" className="ml-2">
          {t("safety")}
        </Badge>
      )}
    </Label>
  );
  const described = invalid ? { "aria-invalid": true, "aria-describedby": errorId } : {};
  let control;
  if (spec.type === "boolean" || spec.type === "enum_list") {
    const names = spec.type === "boolean" ? [name] : (spec.values ?? []);
    const held = Array.isArray(value) ? value : value === true ? [name] : [];
    control = (
      <div id={id} role="group" aria-label={name} className="flex flex-wrap gap-3" {...described}>
        {names.map((option) => (
          <label key={option} className="flex items-center gap-1 text-sm">
            <input
              type="checkbox"
              disabled={disabled}
              checked={held.includes(option)}
              onChange={(event) => {
                if (spec.type === "boolean") onChange(event.target.checked);
                else
                  onChange(
                    (spec.values ?? []).filter((each) =>
                      each === option ? event.target.checked : held.includes(each),
                    ),
                  );
              }}
            />
            {spec.type === "boolean" ? t("enabled") : option}
          </label>
        ))}
      </div>
    );
  } else if (spec.type === "enum") {
    control = (
      <Select
        value={typeof value === "string" ? value : ""}
        disabled={disabled}
        onValueChange={(choice) => onChange(choice)}
      >
        <SelectTrigger id={id} {...described}>
          <SelectValue placeholder={t("choose")} />
        </SelectTrigger>
        <SelectContent>
          {(spec.values ?? []).map((option) => (
            <SelectItem key={option} value={option}>
              {option}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  } else {
    control = (
      <Input
        id={id}
        disabled={disabled}
        inputMode={spec.type === "integer" || spec.type === "number" ? "decimal" : undefined}
        value={typeof value === "string" ? value : ""}
        onChange={(event) => onChange(event.target.value)}
        {...described}
      />
    );
  }
  return (
    <div className="space-y-1">
      {label}
      {control}
      {invalid && (
        <p id={errorId} className="text-sm text-destructive">
          {problems.map((problem) => problem.detail).join(" ; ")}
        </p>
      )}
    </div>
  );
}

function ConfigForm({ view, machineId }: { view: MachineConfigView; machineId: Id<"machines"> }) {
  const t = useTranslations("machineConfig");
  const propose = useMutationWithFeedback(api.machineConfigs.proposeConfig);
  const byNumber = (revision: number | null) =>
    view.revisions.find((row) => row.revision === revision)?.values ?? null;
  const applied = byNumber(view.appliedRevision);
  const inForce = byNumber(view.inForceRevision);
  const start: ConfigValues = inForce ?? view.defaults;
  const [fields, setFields] = useState(() =>
    Object.fromEntries(CONFIG_KEY_NAMES.map((key) => [key, fieldOf(CONFIG_KEYS[key], start[key])])),
  );
  const [busy, setBusy] = useState(false);
  const values: ConfigValues = Object.fromEntries(
    CONFIG_KEY_NAMES.map((key) => [key, valueOf(CONFIG_KEYS[key], fields[key])]),
  );
  const problems = machineConfigProblems(values, view.validationLevel);
  const diff = changedKeys(applied, values);
  const unchanged = inForce !== null && canonicalConfig(inForce) === canonicalConfig(values);
  const editable = (key: string) =>
    view.canPropose === "all" || ORGANIZATION_KEYS.includes(key);
  const general = problems.filter((problem) => !CONFIG_KEY_NAMES.includes(problem.key));
  return (
    <form
      aria-label={t("form")}
      className="space-y-4"
      onSubmit={async (event) => {
        event.preventDefault();
        if (busy || problems.length > 0 || unchanged) return;
        setBusy(true);
        try {
          await propose(
            { machineId, values },
            { success: (result) => t("proposed", { revision: result.revision }) },
          );
        } finally {
          setBusy(false);
        }
      }}
    >
      <p className="text-sm text-muted-foreground">
        {t("level", { level: view.validationLevel })}
      </p>
      {problems.length > 0 && (
        <Alert variant="destructive">
          <AlertDescription>
            <p className="font-medium">{t("problems", { count: problems.length })}</p>
            <ul aria-label={t("problemList")} className="list-disc pl-5">
              {problems.map((problem) => (
                <li key={`${problem.key}:${problem.detail}`}>
                  <code>{problem.key}</code> : {problem.detail}
                </li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}
      <fieldset disabled={busy} className="grid gap-4 md:grid-cols-2">
        {CONFIG_KEY_NAMES.map((key) => (
          <ConfigField
            key={key}
            name={key}
            value={fields[key]}
            disabled={!editable(key)}
            problems={problems.filter((problem) => problem.key === key)}
            onChange={(value) => setFields((held) => ({ ...held, [key]: value }))}
          />
        ))}
      </fieldset>
      {general.length > 0 && (
        <p className="text-sm text-destructive">
          {general.map((problem) => `${problem.key} : ${problem.detail}`).join(" ; ")}
        </p>
      )}
      <div className="rounded-md border p-3 text-sm">
        <p className="font-medium">
          {view.appliedRevision === null
            ? t("diffNone")
            : t("diffApplied", { revision: view.appliedRevision })}
        </p>
        <ul aria-label={t("diff")}>
          {diff.map((key) => (
            <li key={key}>
              <code>{key}</code> : {shown(applied?.[key])} → {shown(values[key])}
              {CONFIG_KEYS[key].family === "safety" && (
                <Badge variant="destructive" className="ml-2">
                  {t("safety")}
                </Badge>
              )}
            </li>
          ))}
        </ul>
      </div>
      <Button type="submit" disabled={busy || problems.length > 0 || unchanged}>
        {t("propose")}
      </Button>
      {unchanged && <p className="text-sm text-muted-foreground">{t("unchanged")}</p>}
    </form>
  );
}

function RevisionRow({ revision, canReject }: { revision: Revision; canReject: boolean }) {
  const t = useTranslations("machineConfig");
  const locale = useLocale();
  const reasonId = useId();
  const [reason, setReason] = useState("");
  const approve = useMutationWithFeedback(api.machineConfigs.approveConfig);
  const reject = useMutationWithFeedback(api.machineConfigs.rejectConfig);
  const date = (at: number | undefined) =>
    at === undefined ? "-" : format(at, "Pp", { locale: locale === "fr" ? fr : enUS });
  const name = (author: { firstName?: string; lastName?: string; userId: string }) =>
    author.firstName === undefined ? author.userId : `${author.firstName} ${author.lastName ?? ""}`.trim();
  const open = revision.status === "draft" || revision.status === "approved";
  return (
    <li aria-label={t("revision", { revision: revision.revision })} className="space-y-2 border-t pt-3">
      <p className="font-medium">
        {t("revision", { revision: revision.revision })} : {t(`statuses.${revision.status}`)}
        {revision.safetyChange && (
          <Badge variant="destructive" className="ml-2">
            {t("safetyChange")}
          </Badge>
        )}
      </p>
      <p className="text-sm">
        {t("proposedBy", { name: name(revision.proposedBy), at: date(revision.proposedAt) })}
        {revision.approvedBy !== undefined &&
          ` ; ${t("approvedBy", { names: revision.approvedBy.map(name).join(", "), at: date(revision.approvedAt) })}`}
        {revision.appliedAt !== undefined && ` ; ${t("appliedAt", { at: date(revision.appliedAt) })}`}
      </p>
      <p className="text-sm text-muted-foreground">
        {t("changed", { keys: revision.changedFromApplied.join(", ") || "-" })}
      </p>
      {revision.rejectReason !== undefined && (
        <p className="text-sm">{t("rejectReason", { reason: revision.rejectReason })}</p>
      )}
      {revision.status === "draft" && (
        <div className="space-y-1">
          <Button
            size="sm"
            disabled={revision.approveBlocked !== null}
            onClick={() =>
              approve(
                { configId: revision._id },
                { success: t("approved", { revision: revision.revision }) },
              )
            }
          >
            {t("approve")}
          </Button>
          {revision.approveBlocked !== null && (
            <p className="text-sm text-muted-foreground">
              {t(`blocked.${revision.approveBlocked}`)}
            </p>
          )}
        </div>
      )}
      {open && canReject && (
        <div className="space-y-1">
          <Label htmlFor={reasonId}>{t("reason")}</Label>
          <Textarea
            id={reasonId}
            value={reason}
            maxLength={500}
            onChange={(event) => setReason(event.target.value)}
          />
          <Button
            size="sm"
            variant="outline"
            disabled={reason.trim().length < 3}
            onClick={() =>
              reject(
                { configId: revision._id, reason },
                { success: t("rejected", { revision: revision.revision }) },
              )
            }
          >
            {t("reject")}
          </Button>
        </div>
      )}
    </li>
  );
}

/**
 * The machine's configuration (ANH-141), for the Anheart admin and the
 * organisation admin: its status, a form generated from
 * `contracts/machine-config.json` that shows every problem the console would
 * raise at once and the diff with the revision applied, and the revisions to
 * approve or reject. The server decides who may do what; the buttons follow.
 */
export function MachineConfigCard({
  machineId,
  role,
  userId,
}: {
  machineId: Id<"machines">;
  role: string | undefined;
  userId: Id<"users"> | undefined;
}) {
  const t = useTranslations("machineConfig");
  const visible = isOrganizationAdmin(role);
  const view = useQuery(api.machineConfigs.getMachineConfig, visible ? { machineId } : "skip");
  if (!visible || view === null) return null;
  if (view === undefined) return <Skeleton aria-label={t("loading")} className="h-40 w-full" />;
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2>{t("title")}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <section aria-label={t("title")} className="space-y-4">
          <div className="flex flex-wrap gap-2">
            <Badge variant="outline">{t(`status.${view.configurationStatus}`)}</Badge>
          </div>
          {view.hashMatches === false && (
            <Alert variant="destructive">
              <AlertDescription>{t("mismatch")}</AlertDescription>
            </Alert>
          )}
          {view.canPropose !== "none" && <ConfigForm view={view} machineId={machineId} />}
          <ul aria-label={t("revisions")} className="space-y-3">
            {view.revisions.map((revision) => (
              <RevisionRow
                key={revision._id}
                revision={revision}
                canReject={
                  isAnheartAdmin(role) ||
                  (revision.status === "draft" && revision.proposedBy.userId === userId)
                }
              />
            ))}
          </ul>
        </section>
      </CardContent>
    </Card>
  );
}

"use client";

import type { ReactNode } from "react";
import { useQuery } from "convex/react";
import type { FunctionReturnType } from "convex/server";
import { useLocale, useTranslations } from "next-intl";
import { format } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { isAnheartAdmin, isOrganizationAdmin } from "@/lib/organizationAccess";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  IdentityForm,
  RadiiForm,
  DriveSheetForm,
  ValidationForms,
} from "./MachineRegistryForms";

export type MachineRegistry = NonNullable<
  FunctionReturnType<typeof api.machineRegistry.getRegistry>
>;

function Value({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="break-words">{children}</dd>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h3>{title}</h3>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">{children}</CardContent>
    </Card>
  );
}

export function MachineRegistryCard({
  machineId,
  role,
  readOnly = false,
}: {
  machineId: Id<"machines">;
  role: string | undefined;
  readOnly?: boolean;
}) {
  const t = useTranslations("machineRegistry");
  const locale = useLocale();
  const visible = isOrganizationAdmin(role);
  const registry = useQuery(
    api.machineRegistry.getRegistry,
    visible ? { machineId } : "skip",
  );
  if (!visible || registry === null) return null;
  if (registry === undefined)
    return <Skeleton className="h-32 w-full" aria-label={t("loading")} />;

  const canEdit = isAnheartAdmin(role) && registry.canEdit && !readOnly;
  const when = (at: number) =>
    format(at, "PP", { locale: locale === "fr" ? fr : enUS });
  const missing = t("missing");
  const date = (at: number | undefined) =>
    at === undefined ? missing : when(at);
  const authorName = (author: NonNullable<MachineRegistry["updatedBy"]>) =>
    [author.firstName, author.lastName].filter(Boolean).join(" ") ||
    author.userId;
  const attachment = (url: string | null, label: string) =>
    url === null ? (
      <span className="text-muted-foreground">{t("fileUnavailable")}</span>
    ) : (
      <a className="text-primary underline" href={url} download>
        {label}
      </a>
    );

  return (
    <section className="space-y-4" aria-label={t("title")}>
      <div>
        <h2 className="text-xl font-semibold">{t("title")}</h2>
        <p className="text-sm text-muted-foreground">
          {t(canEdit ? "editable" : "readOnly")}
        </p>
      </div>
      {registry.updatedBy !== null && registry.updatedAt !== null && (
        <p className="text-sm text-muted-foreground">
          {t("lastUpdated", {
            by: authorName(registry.updatedBy),
            at: when(registry.updatedAt),
          })}
        </p>
      )}
      {registry.alerts.map((alert) => (
        <Alert variant="destructive" key={alert.code}>
          <AlertDescription>
            {t(`alerts.${alert.code}`, { count: alert.count })}
            {alert.mostRecentAt !== null && (
              <span className="block">
                {t("mostRecent", { at: when(alert.mostRecentAt) })}
              </span>
            )}
          </AlertDescription>
        </Alert>
      ))}
      <div className="grid gap-6 xl:grid-cols-2">
        <Section title={t("identity")}>
          <dl className="grid gap-3 sm:grid-cols-2">
            {(
              [
                "serialPi",
                "serialDrive",
                "serialMotor",
                "serialGearbox",
                "bitalinoAddress",
                "site",
              ] as const
            ).map((field) => (
              <Value key={field} label={t(field)}>
                {registry[field] ?? missing}
              </Value>
            ))}
            <Value label={t("commissionedAt")}>
              {registry.commissionedAt === undefined
                ? missing
                : new Intl.DateTimeFormat(locale, {
                    dateStyle: "medium",
                    timeZone: "UTC",
                  }).format(registry.commissionedAt)}
            </Value>
            <Value label={t("contacts")}>
              {registry.contacts.length === 0
                ? missing
                : registry.contacts.map((contact) => (
                    <p key={contact.userId}>
                      {contact.firstName} {contact.lastName}
                    </p>
                  ))}
            </Value>
          </dl>
          {canEdit && (
            <IdentityForm
              key={JSON.stringify([
                registry.machineId,
                registry.serialPi,
                registry.serialDrive,
                registry.serialMotor,
                registry.serialGearbox,
                registry.bitalinoAddress,
                registry.site,
                registry.commissionedAt,
                registry.expectedSoftwareVersion,
                registry.contacts.map((contact) => contact.userId),
              ])}
              registry={registry}
            />
          )}
        </Section>
        <Section title={t("measurements")}>
          <dl className="grid gap-3 sm:grid-cols-2">
            <Value label={t("armRadiusM")}>
              {registry.armRadiusM === undefined
                ? missing
                : t("metres", { value: registry.armRadiusM })}
            </Value>
            <Value label={t("legTipRadiusM")}>
              {registry.legTipRadiusM === undefined
                ? missing
                : t("metres", { value: registry.legTipRadiusM })}
            </Value>
            <Value label={t("measuredAt")}>
              {date(registry.radiiMeasuredAt)}
            </Value>
            <Value label={t("measuredBy")}>
              {registry.radiiMeasuredBy ?? missing}
            </Value>
          </dl>
          <div className="rounded-md border p-3 text-sm space-y-1">
            <p className="font-medium">{t("proposal")}</p>
            {Object.entries(registry.configurationProposal).map(
              ([key, value]) => (
                <p key={key}>
                  <code>{key}</code> :{" "}
                  {value === null ? missing : t("metres", { value })}
                </p>
              ),
            )}
            <p className="text-muted-foreground">{t("proposalPending")}</p>
          </div>
          {canEdit && (
            <RadiiForm
              key={JSON.stringify([
                registry.machineId,
                registry.armRadiusM,
                registry.legTipRadiusM,
                registry.radiiMeasuredAt,
                registry.radiiMeasuredBy,
              ])}
              registry={registry}
            />
          )}
        </Section>
        <Section title={t("drive")}>
          <p className="text-sm text-muted-foreground">{t("fileHelp")}</p>
          {registry.driveParameterSheet === null ? (
            <p>{missing}</p>
          ) : (
            <div>
              <p className="font-medium">{t("currentSheet")}</p>
              {attachment(
                registry.driveParameterSheet.url,
                registry.driveParameterSheet.version,
              )}
              <p className="text-sm">
                {t("signed", {
                  by: registry.driveParameterSheet.signedBy,
                  at: when(registry.driveParameterSheet.at),
                })}
              </p>
              <p className="text-sm">
                {t("attachedAudit", {
                  by: authorName(registry.driveParameterSheet.attachedBy),
                  at: when(registry.driveParameterSheet.attachedAt),
                })}
              </p>
            </div>
          )}
          {registry.driveParameterSheetHistory.length > 0 && (
            <div>
              <h4 className="font-medium">{t("sheetHistory")}</h4>
              <ul className="space-y-2">
                {registry.driveParameterSheetHistory.map((sheet) => (
                  <li key={sheet.storageId}>
                    {attachment(sheet.url, sheet.version)}
                    <p className="text-sm">
                      {t("signed", { by: sheet.signedBy, at: when(sheet.at) })}
                    </p>
                    <p className="text-sm">
                      {t("attachedAudit", {
                        by: authorName(sheet.attachedBy),
                        at: when(sheet.attachedAt),
                      })}
                    </p>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {canEdit && <DriveSheetForm registry={registry} />}
        </Section>
        <Section title={t("validation")}>
          <Badge>{t(`levels.${registry.effectiveLevel}`)}</Badge>
          {registry.validation.level !== registry.effectiveLevel && (
            <Alert variant="destructive">
              <AlertDescription>
                {t("levelMismatch", {
                  stored: t(`levels.${registry.validation.level}`),
                  effective: t(`levels.${registry.effectiveLevel}`),
                })}
              </AlertDescription>
            </Alert>
          )}
          <p className="text-sm text-muted-foreground">{t("reviewRule")}</p>
          {registry.medicalKeyIsTest && (
            <Alert>
              <AlertDescription>{t("medicalKeyIsTest")}</AlertDescription>
            </Alert>
          )}
          <h4 className="font-medium">{t("reviews")}</h4>
          {registry.validation.reviews.length === 0 ? (
            <p>{t("noReviews")}</p>
          ) : (
            <ul className="space-y-3">
              {registry.validation.reviews.map((review, index) => (
                <li
                  className="rounded-md border p-3 text-sm space-y-1"
                  key={`${review.at}-${index}`}
                >
                  <Badge variant="outline">{t(`levels.${review.level}`)}</Badge>
                  <p>
                    {t("audit", {
                      by: authorName(review.by),
                      at: when(review.at),
                    })}
                  </p>
                  <p>
                    {t("softwareVersion")} : {review.softwareVersion}
                  </p>
                  <p>
                    {t("medicalVersion")} :{" "}
                    {review.medicalParametersVersion ?? t("noMedicalVersion")}
                  </p>
                  {attachment(review.reportUrl, t("downloadReport"))}
                </li>
              ))}
            </ul>
          )}
          <h4 className="font-medium">{t("downgrades")}</h4>
          {registry.validation.downgrades.length === 0 ? (
            <p>{t("noDowngrades")}</p>
          ) : (
            <ul className="space-y-3">
              {registry.validation.downgrades.map((entry, index) => (
                <li
                  className="rounded-md border p-3 text-sm"
                  key={`${entry.at}-${index}`}
                >
                  <p>
                    {t("lowered", {
                      from: t(`levels.${entry.from}`),
                      to: t(`levels.${entry.to}`),
                    })}
                  </p>
                  <p>
                    {t("audit", {
                      by: authorName(entry.by),
                      at: when(entry.at),
                    })}
                  </p>
                  <p>{entry.reason}</p>
                </li>
              ))}
            </ul>
          )}
          {canEdit && (
            <ValidationForms
              key={`${registry.machineId}:${registry.effectiveLevel}`}
              registry={registry}
            />
          )}
        </Section>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>
            <h3>{t("softwareAndLocks")}</h3>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <dl className="grid gap-3 sm:grid-cols-3">
            <Value label={t("reportedVersion")}>
              {registry.reportedSoftwareVersion ?? missing}
            </Value>
            <Value label={t("expectedSoftwareVersion")}>
              {registry.expectedSoftwareVersion ?? missing}
            </Value>
            <Value label={t("versionStatus")}>
              <Badge
                variant={
                  registry.versionStatus === "not_validated"
                    ? "destructive"
                    : "secondary"
                }
              >
                {t(`versions.${registry.versionStatus}`)}
              </Badge>
            </Value>
          </dl>
          {registry.expectedVersionAllowed === false && (
            <Alert variant="destructive">
              <AlertDescription>{t("expectedNotValidated")}</AlertDescription>
            </Alert>
          )}
          <ul className="space-y-3">
            {Object.entries(registry.configurationLocks).map(
              ([key, verdict]) => (
                <li
                  key={key}
                  className="flex flex-wrap items-center gap-2 text-sm"
                >
                  <code>{key}</code>
                  <Badge
                    variant={verdict.allowed ? "secondary" : "destructive"}
                  >
                    {t(verdict.allowed ? "allowed" : "denied")}
                  </Badge>
                  {verdict.message !== null && (
                    <span>
                      {t("lockReason", {
                        level: t(`levels.${verdict.reachedLevel}`),
                      })}
                    </span>
                  )}
                </li>
              ),
            )}
          </ul>
          <p className="text-sm text-muted-foreground">{t("locksPending")}</p>
          <p className="text-sm text-muted-foreground">{t("otaPending")}</p>
        </CardContent>
      </Card>
    </section>
  );
}

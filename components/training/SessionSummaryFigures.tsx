"use client";

import type { ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";
import type { FunctionReturnType } from "convex/server";
import type { api } from "@/convex/_generated/api";
import { useCatalogLabel } from "@/components/dashboard/statusLabels";
import { useTrainingLabel } from "./TrainingBadges";
import { formatDuration, formatNumber, percentOf } from "@/lib/sessionReport";

export type SessionSummary = Exclude<
  NonNullable<FunctionReturnType<typeof api.sessionReports.getSummary>>,
  { absent: string }
>;
export type SummaryFigures = SessionSummary["figures"];

/**
 * The figures of a session summary (ANH-89), as the session page and the
 * report show them. Each figure is the server's (convex/lib/sessionSummary.ts
 * defines them); a figure the source does not have is said to be missing,
 * never drawn as zero.
 */
export function SessionSummaryFigures({
  figures,
}: {
  figures: SummaryFigures;
}) {
  const t = useTranslations("sessionReport");
  const locale = useLocale();
  const endReason = useCatalogLabel("sessionReport.endReasons");
  const warning = useCatalogLabel("sessionReport.warnings");
  const label = useTrainingLabel();
  const number = (value: number, digits: number) =>
    formatNumber(value, locale, digits);
  const { zone, heartRate } = figures;
  const inZoneShare = zone ? percentOf(zone.inZoneS, figures.durationS) : null;

  return (
    <div className="space-y-5 text-sm">
      <dl className="grid grid-cols-2 gap-4 md:grid-cols-4 print:grid-cols-4">
        <Figure label={t("duration")}>
          {formatDuration(figures.durationS)}
        </Figure>
        <Figure
          label={t("timeInZone")}
          detail={
            zone
              ? t("zoneBounds", { low: zone.lowBpm, high: zone.highBpm })
              : undefined
          }
        >
          {zone === null
            ? t("noZone")
            : inZoneShare === null
              ? formatDuration(zone.inZoneS)
              : t("inZoneShare", {
                  duration: formatDuration(zone.inZoneS),
                  percent: inZoneShare,
                })}
        </Figure>
        <Figure label={t("heartRate")}>
          {heartRate === null
            ? t("noHeartRate")
            : t("heartRateValue", {
                min: heartRate.minBpm,
                mean: number(heartRate.meanBpm, 1),
                max: heartRate.maxBpm,
              })}
        </Figure>
        <Figure
          label={t("speed")}
          detail={
            figures.maxMotorRpm === null
              ? undefined
              : t("motorSpeed", { rpm: number(figures.maxMotorRpm, 0) })
          }
        >
          {figures.maxOutputRpm === null
            ? "-"
            : t("rpm", { value: number(figures.maxOutputRpm, 2) })}
        </Figure>
      </dl>

      {zone && (
        <dl className="grid grid-cols-3 gap-4 rounded-md border p-3">
          <Figure label={t("belowZone")}>
            {formatDuration(zone.belowZoneS)}
          </Figure>
          <Figure label={t("inZone")}>{formatDuration(zone.inZoneS)}</Figure>
          <Figure label={t("aboveZone")}>
            {formatDuration(zone.aboveZoneS)}
          </Figure>
        </dl>
      )}

      <div>
        <p className="mb-2 font-semibold">{t("gTitle")}</p>
        <table className="w-full text-left">
          <thead className="text-muted-foreground">
            <tr>
              <th className="py-1 font-normal">{t("where")}</th>
              <th className="py-1 font-normal">{t("gMax")}</th>
              <th className="py-1 font-normal">{t("gRate")}</th>
            </tr>
          </thead>
          <tbody>
            {(
              [
                ["reference", figures.reference, null],
                ["legTip", figures.legTip, figures.legTip.measured],
              ] as const
            ).map(([where, radius, measured]) => (
              <tr key={where} className="border-t">
                <td className="py-1">
                  {t(where)}
                  {radius.radiusM !== null && (
                    <span className="text-muted-foreground">
                      {" "}
                      ({t("radius", { radius: number(radius.radiusM, 3) })}
                      {measured === false && `, ${t("estimated")}`})
                    </span>
                  )}
                </td>
                <td className="py-1 font-medium">
                  {radius.maxG === null ? "-" : number(radius.maxG, 3)}
                </td>
                <td className="py-1 font-medium">
                  {radius.maxGRate === null
                    ? t("notYet")
                    : t("gRateValue", { value: number(radius.maxGRate, 4) })}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div>
        <p className="mb-2 font-semibold">{t("verdicts")}</p>
        {figures.verdicts.length === 0 ? (
          <p className="text-muted-foreground">{t("noVerdicts")}</p>
        ) : (
          <ul className="space-y-1">
            {figures.verdicts.map((verdict) => (
              <li key={verdict.rule}>
                <span className="font-mono">{verdict.rule}</span>
                {verdict.action !== null && (
                  <span>
                    {" "}
                    ({label("safety", verdict.action.toLowerCase())})
                  </span>
                )}
                {verdict.firstAtS !== null && (
                  <span className="text-muted-foreground">
                    {" "}
                    {t("verdictAt", { seconds: number(verdict.firstAtS, 1) })}
                  </span>
                )}
              </li>
            ))}
            {figures.verdictsOmitted > 0 && (
              <li className="text-muted-foreground">
                {t("verdictsOmitted", { count: figures.verdictsOmitted })}
              </li>
            )}
          </ul>
        )}
      </div>

      <dl className="grid grid-cols-2 gap-4">
        <Figure label={t("endReason")}>
          {figures.endReason === null ? "-" : endReason(figures.endReason)}
        </Figure>
        <Figure label={t("endRule")}>
          {figures.endRule === null ? (
            "-"
          ) : (
            <span className="font-mono">{figures.endRule}</span>
          )}
        </Figure>
      </dl>

      {figures.warnings.length > 0 && (
        <ul className="space-y-1 rounded-md border border-amber-500/50 p-3 text-amber-800 dark:text-amber-300 print:text-black">
          {figures.warnings.map((code) => (
            <li key={code}>{warning(code)}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Figure({
  label,
  detail,
  children,
}: {
  label: string;
  detail?: string;
  children: ReactNode;
}) {
  return (
    <div>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-medium">{children}</dd>
      {detail !== undefined && (
        <dd className="text-xs text-muted-foreground">{detail}</dd>
      )}
    </div>
  );
}

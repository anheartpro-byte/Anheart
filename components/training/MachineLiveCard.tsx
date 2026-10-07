"use client";

import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations, useLocale } from "next-intl";
import { formatDistanceToNow } from "date-fns";
import { fr, enUS } from "date-fns/locale";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Heart, Gauge, Radio, ShieldAlert, AlertTriangle } from "lucide-react";
import { useFreshness } from "@/hooks/use-freshness";
import { type LiveState } from "@/lib/training";
import { LiveFreshBadge, useTrainingLabel } from "./TrainingBadges";

/**
 * The machine's live readouts. `bpm` absent renders a dash, never a stale
 * number. `stale` (from useFreshness) greys every value and says why.
 */
export function LiveReadouts({
  live,
  stale,
}: {
  live: LiveState;
  stale: boolean;
}) {
  const t = useTranslations("training");
  const label = useTrainingLabel();
  const safetyActive = live.safetyAction !== "none";

  return (
    <>
      {stale && (
        <p
          role="status"
          className="flex items-center gap-2 text-sm text-amber-600 dark:text-amber-400"
        >
          <AlertTriangle className="h-4 w-4 shrink-0" />
          {t("live.staleDesc")}
        </p>
      )}
      <div
        className={`grid grid-cols-2 sm:grid-cols-3 gap-4 ${stale ? "opacity-60" : ""}`}
      >
        <Readout label={t("live.runMode")}>
          {label("runMode", live.runMode)}
        </Readout>
        <Readout label={t("live.phase")}>{label("phase", live.phase)}</Readout>
        <Readout label={t("live.heartRate")}>
          <span className="flex items-center gap-1">
            <Heart
              className={`h-4 w-4 ${live.bpm !== undefined && !stale ? "text-red-500" : "text-muted-foreground"}`}
            />
            {live.bpm !== undefined ? Math.round(live.bpm) : "-"}
            <span className="text-xs text-muted-foreground font-normal">
              {t("units.bpm")}
            </span>
          </span>
        </Readout>
        <Readout label={t("live.outputRpm")}>
          <span>
            {live.outputRpm.toFixed(1)}{" "}
            <span className="text-xs text-muted-foreground font-normal">
              {t("units.rpm")}
            </span>
          </span>
          <span className="block text-xs text-muted-foreground font-normal">
            {t("live.motorRpm")} {Math.round(live.motorRpm)} {t("units.rpm")} ·{" "}
            {t("live.setpoint")} {Math.round(live.setpointMotorRpm)}
          </span>
        </Readout>
        <Readout label={t("live.gLoad")}>
          <span className="flex items-center gap-1">
            <Gauge className="h-4 w-4 text-muted-foreground" />
            {live.gLoad.toFixed(2)}
            <span className="text-xs text-muted-foreground font-normal">
              {t("units.g")}
            </span>
          </span>
        </Readout>
        <Readout label={t("live.safetyAction")}>
          <span
            className={`flex items-center gap-1 ${safetyActive ? "text-amber-600 dark:text-amber-400" : ""}`}
          >
            {safetyActive && <ShieldAlert className="h-4 w-4" />}
            {label("safety", live.safetyAction)}
          </span>
          {live.driveState && (
            <span className="block text-xs text-muted-foreground font-normal">
              {t("live.driveState")} : {label("driveState", live.driveState)}
            </span>
          )}
        </Readout>
      </div>
    </>
  );
}

function Readout({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <p className="text-sm text-muted-foreground">{label}</p>
      <div className="font-medium text-lg">{children}</div>
    </div>
  );
}

/** Live card for the machine detail page (getMachineLive). */
export function MachineLiveCard({ machineId }: { machineId: Id<"machines"> }) {
  const t = useTranslations("training.live");
  const locale = useLocale();
  const data = useQuery(api.training.getMachineLive, { machineId });
  // On the clock: the query only runs again when the machine's record changes,
  // so its own `stale` lags a machine that has gone quiet. It still counts when
  // it says stale: that catches a browser clock running behind.
  const { fresh } = useFreshness(data?.live?.updatedAt);
  const stale = !fresh || data?.stale === true;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2">
              <Radio className="h-4 w-4" />
              {t("title")}
            </CardTitle>
            <CardDescription>{t("description")}</CardDescription>
          </div>
          {data && data.live && <LiveFreshBadge stale={stale} />}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {data === undefined ? (
          <Skeleton className="h-28 w-full" />
        ) : data === null || data.live === null ? (
          <p className="text-sm text-muted-foreground">{t("noData")}</p>
        ) : (
          <>
            <LiveReadouts live={data.live} stale={stale} />
            <p className="text-xs text-muted-foreground">
              {t("updated", {
                time: formatDistanceToNow(data.live.updatedAt, {
                  addSuffix: true,
                  locale: locale === "fr" ? fr : enUS,
                }),
              })}
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}

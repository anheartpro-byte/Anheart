"use client";

import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations } from "next-intl";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import { Gauge, Hand } from "lucide-react";
import { formatMinutes } from "@/lib/training";
import { SessionKindBadge, SessionOriginBadge } from "./TrainingBadges";
import { TelemetryCharts } from "./TelemetryCharts";

/** Session detail: training parameters, end reason and the recorded telemetry. */
export function TrainingDetailsCard({
  sessionId,
}: {
  sessionId: Id<"sessions">;
}) {
  const t = useTranslations("training");
  const training = useQuery(api.training.getTrainingSession, { sessionId });
  const telemetry = useQuery(api.training.getSessionTelemetry, {
    sessionId,
    limit: 7200,
  });

  if (training === undefined) return <Skeleton className="h-64 w-full" />;
  if (training === null) return null;

  const hasZone =
    training.zoneLowBpm !== undefined && training.zoneHighBpm !== undefined;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Gauge className="h-4 w-4" />
          {t("session.trainingInfo")}
        </CardTitle>
        <CardDescription className="flex flex-wrap items-center gap-2">
          <SessionKindBadge kind={training.kind} />
          <SessionOriginBadge origin={training.origin} />
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
          <Field label={t("session.program")}>
            {training.profileName ?? training.profileId ?? "-"}
          </Field>
          <Field label={t("session.zone")}>
            {hasZone
              ? `${training.zoneLowBpm}-${training.zoneHighBpm} ${t("units.bpm")}`
              : "-"}
          </Field>
          <Field label={t("session.plannedDuration")}>
            {training.totalDurationS !== undefined
              ? formatMinutes(training.totalDurationS)
              : "-"}
          </Field>
          <Field label={t("session.subjectHrMax")}>
            {training.subjectHrMax !== undefined
              ? `${training.subjectHrMax} ${t("units.bpm")}`
              : "-"}
          </Field>
          <Field label={t("session.operator")}>
            {training.operatorName ?? "-"}
          </Field>
          <Field label={t("session.origin")}>
            {training.origin === "remote" || training.origin === "local"
              ? t(`origin.${training.origin}`)
              : "-"}
          </Field>
        </div>
        {training.endReason && (
          <div
            className={`rounded-md border p-3 text-sm ${
              training.status === "failed"
                ? "border-red-500/50 bg-red-50 text-red-800 dark:bg-red-950/40 dark:text-red-200"
                : "bg-muted/30"
            }`}
          >
            <p className="text-xs text-muted-foreground">
              {t("session.endReason")}
            </p>
            <p className="font-mono text-sm">{training.endReason}</p>
          </div>
        )}
        {training.kind === "manual" && (
          <p className="flex items-center gap-2 text-xs text-muted-foreground">
            <Hand className="h-3.5 w-3.5" />
            {t("manualOnlyAtMachine")}
          </p>
        )}
        <Separator />
        <p className="text-sm font-semibold">{t("session.telemetry")}</p>
        {telemetry === undefined ? (
          <Skeleton className="h-[460px] w-full" />
        ) : telemetry.length === 0 ? (
          <p className="text-sm text-muted-foreground text-center py-6">
            {t("session.noTelemetry")}
          </p>
        ) : (
          <TelemetryCharts
            points={telemetry}
            zoneLowBpm={training.zoneLowBpm}
            zoneHighBpm={training.zoneHighBpm}
          />
        )}
      </CardContent>
    </Card>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="font-medium">{children}</p>
    </div>
  );
}

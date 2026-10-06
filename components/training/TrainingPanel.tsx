"use client";

import { useState } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { useMutationWithFeedback } from "@/hooks/use-mutation-with-feedback";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations } from "next-intl";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  Clock,
  Gauge,
  Hand,
  Heart,
  Loader2,
  Octagon,
  RotateCw,
  ShieldAlert,
  Timer,
} from "lucide-react";
import { useFreshness } from "@/hooks/use-freshness";
import { formatClock, type TelemetryPoint } from "@/lib/training";
import {
  SessionKindBadge,
  SessionOriginBadge,
  useTrainingLabel,
} from "./TrainingBadges";
import { TelemetryCharts } from "./TelemetryCharts";

/**
 * A telemetry point older than this no longer stands for "now" on a live
 * session (the machine sends its points every 5 s).
 */
const TELEMETRY_FRESH_MS = 20_000;

/**
 * Training view for auto and manual sessions: big readouts, the heart rate
 * against the target zone, arm speed against setpoint, and the stop control.
 */
export function TrainingPanel({ sessionId }: { sessionId: Id<"sessions"> }) {
  const t = useTranslations();
  const label = useTrainingLabel();

  const training = useQuery(api.training.getTrainingSession, { sessionId });
  const telemetry = useQuery(api.training.getSessionTelemetry, {
    sessionId,
    limit: 3600,
  });
  const requestStop = useMutationWithFeedback(api.training.requestStop);

  const points: TelemetryPoint[] = telemetry ?? [];
  const last = points.length > 0 ? points[points.length - 1] : null;
  // The machine's last sign of life on this session: its latest point, or the
  // start of the session while none has arrived yet.
  const { fresh: signalFresh, now } = useFreshness(
    last?.t ?? training?.startedAt,
    TELEMETRY_FRESH_MS,
  );

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (training === undefined) {
    return <Skeleton className="h-64 w-full" />;
  }
  if (training === null) return null;

  const isActive = training.status === "active";
  const isPending = training.status === "pending";
  // An active session whose machine has gone quiet: nothing here is current.
  const stale = isActive && !signalFresh;
  const lastFresh = last !== null && !stale;
  // Only a fresh point may stand for the current heart rate.
  const bpm = lastFresh && last?.bpm !== undefined ? last.bpm : undefined;

  const elapsedS = isPending
    ? 0
    : isActive
      ? (now - training.startedAt) / 1000
      : ((training.endedAt ?? training.startedAt) - training.startedAt) / 1000;
  const remainingS =
    training.totalDurationS !== undefined
      ? training.totalDurationS - elapsedS
      : undefined;

  const hasZone =
    training.zoneLowBpm !== undefined && training.zoneHighBpm !== undefined;
  let zoneState: "in" | "below" | "above" | null = null;
  if (bpm !== undefined && hasZone) {
    zoneState =
      bpm < training.zoneLowBpm!
        ? "below"
        : bpm > training.zoneHighBpm!
          ? "above"
          : "in";
  }
  const zoneColor =
    zoneState === "in"
      ? "text-green-600 dark:text-green-400"
      : zoneState === "above"
        ? "text-red-600 dark:text-red-400"
        : zoneState === "below"
          ? "text-blue-600 dark:text-blue-400"
          : "text-muted-foreground";

  const stopRequested = training.stopRequestedAt !== undefined;

  const handleStop = async () => {
    setStopping(true);
    setError(null);
    const result = await requestStop(
      { sessionId },
      {
        success: isPending
          ? t("feedback.sessionCancelled")
          : t("feedback.stopRequested"),
      },
    );
    if (!result.ok) setError(result.message);
    setConfirmOpen(false);
    setStopping(false);
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="space-y-1">
            <CardTitle className="flex items-center gap-2">
              <RotateCw
                className={`h-4 w-4 ${isActive && !stopRequested && !stale ? "animate-spin [animation-duration:3s]" : ""}`}
              />
              {t("training.session.panelTitle")}
              {training.profileName && (
                <span className="font-normal text-muted-foreground">
                  · {training.profileName}
                </span>
              )}
            </CardTitle>
            <CardDescription className="flex flex-wrap items-center gap-2">
              <SessionKindBadge kind={training.kind} />
              <SessionOriginBadge origin={training.origin} />
              {hasZone && (
                <span>
                  {t("training.session.target", {
                    low: training.zoneLowBpm!,
                    high: training.zoneHighBpm!,
                  })}
                </span>
              )}
              {training.operatorName && (
                <span>
                  · {t("training.session.operator")} {training.operatorName}
                </span>
              )}
            </CardDescription>
          </div>
          {training.canStop && (
            <Button
              variant="destructive"
              size="lg"
              disabled={stopRequested || stopping}
              onClick={() => setConfirmOpen(true)}
            >
              {stopRequested ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : (
                <Octagon className="h-4 w-4 mr-2" />
              )}
              {isPending
                ? t("training.session.cancel")
                : t("training.session.stop")}
            </Button>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-6">
        {error && (
          <div className="bg-destructive/10 text-destructive p-3 rounded-md text-sm">
            {error}
          </div>
        )}

        {isPending && (
          <Banner tone="info" icon={<Clock className="h-5 w-5" />}>
            {t("training.session.pending")}
          </Banner>
        )}
        {isActive && stopRequested && (
          <Banner
            tone="warn"
            icon={<Loader2 className="h-5 w-5 animate-spin" />}
          >
            {t("training.session.stopRequested")}
          </Banner>
        )}
        {stale && (
          <Banner tone="warn" icon={<AlertTriangle className="h-5 w-5" />}>
            {t("training.live.staleDesc")}
          </Banner>
        )}
        {training.status === "failed" && (
          <Banner tone="error" icon={<AlertCircle className="h-5 w-5" />}>
            <span className="font-semibold">
              {t("training.session.failed")}
            </span>
            {training.endReason && (
              <span className="block font-mono text-xs mt-1">
                {training.endReason}
              </span>
            )}
          </Banner>
        )}
        {training.status === "completed" && (
          <Banner tone="ok" icon={<CheckCircle2 className="h-5 w-5" />}>
            <span className="font-semibold">
              {t("training.session.completed")}
            </span>
            {training.endReason && (
              <span className="block text-xs mt-1">{training.endReason}</span>
            )}
          </Banner>
        )}
        {training.kind === "manual" && (
          <p className="flex items-center gap-2 text-xs text-muted-foreground">
            <Hand className="h-3.5 w-3.5" />
            {t("training.manualOnlyAtMachine")}
          </p>
        )}

        {/* Big readouts */}
        <div
          className={`grid grid-cols-2 lg:grid-cols-5 gap-4 ${stale ? "opacity-60" : ""}`}
        >
          <BigReadout
            icon={<Heart className={`h-5 w-5 ${zoneColor}`} />}
            label={t("training.live.heartRate")}
            value={bpm !== undefined ? String(Math.round(bpm)) : "-"}
            unit={t("training.units.bpm")}
            valueClass={zoneColor}
            sub={
              zoneState === "in"
                ? t("training.session.inZone")
                : zoneState === "below"
                  ? t("training.session.belowZone")
                  : zoneState === "above"
                    ? t("training.session.aboveZone")
                    : bpm === undefined && isActive
                      ? t("training.live.noHeartRate")
                      : hasZone
                        ? `${training.zoneLowBpm}-${training.zoneHighBpm} ${t("training.units.bpm")}`
                        : undefined
            }
          />
          <BigReadout
            icon={<RotateCw className="h-5 w-5 text-blue-500" />}
            label={t("training.live.outputRpm")}
            value={lastFresh && last ? last.outputRpm.toFixed(1) : "-"}
            unit={t("training.units.rpm")}
            sub={
              lastFresh && last
                ? `${t("training.live.motorRpm")} ${Math.round(last.motorRpm)} ${t("training.units.rpm")}`
                : undefined
            }
          />
          <BigReadout
            icon={<Gauge className="h-5 w-5 text-purple-500" />}
            label={t("training.live.gLoad")}
            value={lastFresh && last ? last.gLoad.toFixed(2) : "-"}
            unit={t("training.units.g")}
          />
          <BigReadout
            icon={
              last && last.safetyAction !== "none" ? (
                <ShieldAlert className="h-5 w-5 text-amber-500" />
              ) : (
                <Clock className="h-5 w-5 text-muted-foreground" />
              )
            }
            label={t("training.live.phase")}
            value={last ? label("phase", last.phase) : "-"}
            sub={
              last && last.safetyAction !== "none"
                ? `${t("training.live.safetyAction")} : ${label("safety", last.safetyAction)}`
                : undefined
            }
          />
          <BigReadout
            icon={<Timer className="h-5 w-5 text-blue-500" />}
            label={t("training.session.elapsed")}
            value={formatClock(elapsedS)}
            sub={
              remainingS !== undefined
                ? `${t("training.session.remaining")} ${formatClock(remainingS)}`
                : undefined
            }
          />
        </div>

        {/* Charts */}
        {telemetry === undefined ? (
          <Skeleton className="h-[460px] w-full" />
        ) : points.length === 0 ? (
          <p className="text-sm text-muted-foreground text-center py-8">
            {t("training.session.noTelemetry")}
          </p>
        ) : (
          <TelemetryCharts
            points={points}
            zoneLowBpm={training.zoneLowBpm}
            zoneHighBpm={training.zoneHighBpm}
          />
        )}
      </CardContent>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {isPending
                ? t("training.session.cancelConfirm")
                : t("training.session.stopConfirm")}
            </DialogTitle>
            <DialogDescription>
              {isPending
                ? t("training.session.cancelConfirmDesc")
                : t("training.session.stopConfirmDesc")}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="destructive"
              onClick={handleStop}
              disabled={stopping}
            >
              {stopping && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              {isPending
                ? t("training.session.cancel")
                : t("training.session.stop")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function BigReadout({
  icon,
  label,
  value,
  unit,
  sub,
  valueClass,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  unit?: string;
  sub?: string;
  valueClass?: string;
}) {
  return (
    <div className="rounded-lg border p-4">
      <div className="flex items-center gap-2">
        {icon}
        <span className={`text-3xl font-bold tabular-nums ${valueClass ?? ""}`}>
          {value}
        </span>
        {unit && <span className="text-sm text-muted-foreground">{unit}</span>}
      </div>
      <p className="text-xs text-muted-foreground mt-1">{label}</p>
      {sub && <p className="text-xs mt-0.5">{sub}</p>}
    </div>
  );
}

function Banner({
  tone,
  icon,
  children,
}: {
  tone: "info" | "warn" | "error" | "ok";
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  const styles = {
    info: "border-blue-500/50 bg-blue-50 text-blue-800 dark:bg-blue-950/40 dark:text-blue-200",
    warn: "border-amber-500/50 bg-amber-50 text-amber-800 dark:bg-amber-950/40 dark:text-amber-200",
    error:
      "border-red-500/50 bg-red-50 text-red-800 dark:bg-red-950/40 dark:text-red-200",
    ok: "border-green-500/50 bg-green-50 text-green-800 dark:bg-green-950/40 dark:text-green-200",
  }[tone];
  return (
    <div
      className={`flex items-start gap-3 rounded-md border p-3 text-sm ${styles}`}
    >
      <span className="shrink-0">{icon}</span>
      <div>{children}</div>
    </div>
  );
}

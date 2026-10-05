"use client";

import { useState, use, useMemo } from "react";
import { useQuery, useMutation } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations, useLocale, useFormatter } from "next-intl";
import { useRouter, Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Activity,
  Heart,
  AlertCircle,
  StopCircle,
  Wifi,
  Clock,
  Database,
  AlertTriangle,
  CheckCircle2,
} from "lucide-react";
import { formatDistanceToNow } from "date-fns";
import { fr, enUS } from "date-fns/locale";
import { LiveSensorDisplay } from "@/components/charts/LiveSensorDisplay";
import { type SignalQuality } from "@/lib/ecg";
import {
  formatSampleRates,
  isPartialRecording,
  summarizeRecording,
} from "@/lib/ecg/stats";
import { isTrainingKind } from "@/lib/training";
import { TrainingPanel } from "@/components/training/TrainingPanel";
import {
  SessionKindBadge,
  SessionOriginBadge,
} from "@/components/training/TrainingBadges";

/**
 * ECG Live Session Page
 *
 * Data Flow:
 * 1. BITalino sensor captures the ECG signal at the machine's acquisition rate
 * 2. The RPi client treats each batch (filter, mV, downsample) and sends it to
 *    Convex with the rate it was resampled to
 * 3. This page queries the last 10 seconds of data and displays it
 *
 * Chart Axes:
 * - X-axis: Time in seconds (0-5s window, scrolling)
 * - Y-axis: treated signal in its unit (mV); legacy raw sessions show ADC values
 */

export default function LiveSessionPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const t = useTranslations();
  const locale = useLocale();
  const intl = useFormatter();
  const router = useRouter();

  const sessionId = id as Id<"sessions">;
  const session = useQuery(api.sessions.getSession, { sessionId });
  const user = useQuery(api.users.getCurrentUser);
  const training = useQuery(api.training.getTrainingSession, { sessionId });
  const endSession = useMutation(api.sessions.endSession);

  // Fetch real-time ECG data (updates every time new data arrives)
  const ecgData = useQuery(api.ecgData.getRecentEcgData, {
    sessionId,
    seconds: 10, // Get last 10 seconds of data
  });

  // Get session stats
  const stats = useQuery(api.ecgData.getSessionDataStats, { sessionId });

  const [showEndDialog, setShowEndDialog] = useState(false);
  const [ending, setEnding] = useState(false);

  const isDelayed = user?.role === "gestionnaire";

  // Data arrives ALREADY TREATED from the Raspberry Pi: filtered, in mV, at the
  // transmitted rate (~250 Hz), with heart rate + quality computed on-device.
  // The UI just reads those; it does not re-filter or re-detect.
  const sampleRate = useMemo(() => {
    let rate = session?.sampleRate ?? 250;
    for (const batch of ecgData ?? []) {
      if (batch.sampleRate) rate = batch.sampleRate;
    }
    return rate;
  }, [ecgData, session?.sampleRate]);

  // Latest on-device ECG metrics (heart rate, HRV, signal quality).
  const { signalQuality, heartRate } = useMemo(() => {
    let quality: SignalQuality = "no_signal";
    let bpm: number | null = null;
    for (const batch of ecgData ?? []) {
      const m = batch.metrics?.ECG;
      if (m) {
        quality = (m.quality as SignalQuality | undefined) ?? "good";
        bpm = m.heartRate ?? null;
      }
    }
    return { signalQuality: quality, heartRate: bpm };
  }, [ecgData]);

  // Unit of the treated ECG values (e.g. "mV"); absent on legacy raw sessions.
  const ecgUnit = useMemo(() => {
    for (const batch of ecgData ?? []) {
      for (const sample of batch.samples) {
        if (sample.channel.toUpperCase() === "ECG" && sample.unit) {
          return sample.unit;
        }
      }
    }
    return undefined;
  }, [ecgData]);

  const dateLocale = locale === "fr" ? fr : enUS;

  // "Poor" groups the two actionable bad states (hum-dominated / noisy).
  const isPoorSignal =
    signalQuality === "mains_dominated" || signalQuality === "noisy";
  const isGoodSignal = signalQuality === "good";

  const handleEndSession = async () => {
    setEnding(true);
    try {
      await endSession({ sessionId });
      router.push(`/dashboard/sessions/${sessionId}`);
    } catch (err) {
      console.error(err);
      setEnding(false);
    }
  };

  if (session === undefined) {
    return <LiveSessionSkeleton />;
  }

  if (session === null) {
    return (
      <div className="text-center py-12">
        <p className="text-muted-foreground">{t("sessions.notFound")}</p>
        <Link
          href="/dashboard/sessions"
          className="text-primary hover:underline mt-2 inline-block"
        >
          {t("common.back")}
        </Link>
      </div>
    );
  }

  const isTraining = isTrainingKind(session.kind);
  const riderName = session.patient
    ? `${session.patient.firstName} ${session.patient.lastName}`
    : (session.subjectLabel ?? t("training.session.riderNotSpecified"));
  // Recording sessions are ended with endSession (managers only); training
  // sessions are stopped from the training panel (requestStop).
  const canEndRecording =
    !isTraining && (user?.role === "admin" || user?.role === "gestionnaire");

  if (isTraining && session.status !== "active") {
    // Pending (waiting for the machine to arm), or already over.
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold">{riderName}</h1>
            <p className="text-muted-foreground flex flex-wrap items-center gap-2">
              {session.machine.name}
              <SessionKindBadge kind={session.kind} />
              <SessionOriginBadge origin={training?.origin} />
            </p>
          </div>
          {session.status !== "pending" && (
            <Link href={`/dashboard/sessions/${sessionId}`}>
              <Button variant="outline">
                {t("training.session.viewDetails")}
              </Button>
            </Link>
          )}
        </div>
        <TrainingPanel sessionId={sessionId} />
      </div>
    );
  }

  if (session.status !== "active") {
    return (
      <div className="text-center py-12 space-y-4">
        <AlertCircle className="h-12 w-12 text-muted-foreground mx-auto" />
        <p className="text-lg">{t("sessionLive.notActive")}</p>
        <Link href={`/dashboard/sessions/${sessionId}`}>
          <Button>{t("sessionLive.viewDetails")}</Button>
        </Link>
      </div>
    );
  }

  const hasData = ecgData && ecgData.length > 0;
  const totalBatches = stats?.totalBatches ?? 0;
  const durationSeconds = stats?.durationSeconds ?? 0;

  // Samples and rates are counted from the batches on screen (the last 10 s),
  // so the count is flagged as partial instead of being extrapolated.
  const recording = summarizeRecording(ecgData ?? [], session.sampleRate);
  const recordedRates = formatSampleRates(recording.sampleRates);
  const isPartial = isPartialRecording(
    recording.batchCount,
    stats?.totalBatches,
  );

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">{riderName}</h1>
          <p className="text-muted-foreground flex flex-wrap items-center gap-2">
            <span>
              {session.machine.name} •{" "}
              {t("sessionLive.started", {
                time: formatDistanceToNow(session.startedAt, {
                  addSuffix: true,
                  locale: dateLocale,
                }),
              })}
            </span>
            {isTraining && (
              <>
                <SessionKindBadge kind={session.kind} />
                <SessionOriginBadge origin={training?.origin} />
              </>
            )}
          </p>
        </div>
        <div className="flex items-center gap-3">
          {isDelayed && (
            <Badge variant="secondary" className="gap-1">
              <AlertCircle className="h-3 w-3" />
              {t("ecg.delay")}
            </Badge>
          )}
          <Badge variant={hasData ? "default" : "secondary"} className="gap-1">
            {hasData ? (
              <>
                <span className="h-2 w-2 bg-green-400 rounded-full animate-pulse" />
                <span>{t("ecg.live")}</span>
              </>
            ) : (
              <>
                <Wifi className="h-3 w-3" />
                <span>{t("sessionLive.connecting")}</span>
              </>
            )}
          </Badge>
          {canEndRecording && (
            <Button
              variant="destructive"
              onClick={() => setShowEndDialog(true)}
            >
              <StopCircle className="h-4 w-4 mr-2" />
              {t("sessions.end")}
            </Button>
          )}
        </div>
      </div>

      {/* Training panel (auto / manual): readouts, zone chart, stop */}
      {isTraining && <TrainingPanel sessionId={sessionId} />}

      {/* Signal Quality Warning */}
      {isPoorSignal && (
        <Card className="border-yellow-500 bg-yellow-50 dark:bg-yellow-950">
          <CardContent className="py-4">
            <div className="flex items-start gap-3">
              <AlertTriangle className="h-6 w-6 text-yellow-600 flex-shrink-0 mt-0.5" />
              <div>
                <h3 className="font-semibold text-yellow-800 dark:text-yellow-200">
                  {signalQuality === "mains_dominated"
                    ? t("sessionLive.mainsTitle")
                    : t("sessionLive.poorTitle")}
                </h3>
                <p className="text-sm text-yellow-700 dark:text-yellow-300 mt-1">
                  {t("sessionLive.warningDescription")}
                </p>
                <ul className="text-sm text-yellow-700 dark:text-yellow-300 mt-2 list-disc list-inside space-y-1">
                  <li>{t("sessionLive.cause1")}</li>
                  <li>{t("sessionLive.cause2")}</li>
                  <li>{t("sessionLive.cause3")}</li>
                </ul>
                <p className="text-sm text-yellow-700 dark:text-yellow-300 mt-2">
                  <strong>{t("sessionLive.tipLabel")}</strong>{" "}
                  {t("sessionLive.tipText")}
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Stats Cards */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
        <Card>
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2">
              {isGoodSignal ? (
                <CheckCircle2 className="h-5 w-5 text-green-500" />
              ) : isPoorSignal ? (
                <AlertTriangle className="h-5 w-5 text-yellow-500" />
              ) : (
                <Wifi className="h-5 w-5 text-muted-foreground" />
              )}
              <span className="text-lg font-bold capitalize">
                {isGoodSignal
                  ? t("sessionLive.qualityGood")
                  : isPoorSignal
                    ? t("sessionLive.qualityPoor")
                    : "---"}
              </span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              {t("sessionLive.signalQuality")}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2">
              <Heart
                className={`h-5 w-5 ${heartRate ? "text-red-500 animate-pulse" : "text-muted-foreground"}`}
              />
              <span className="text-2xl font-bold">{heartRate ?? "--"}</span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              {t("sessionLive.heartRateBpm")}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2">
              <Clock className="h-5 w-5 text-blue-500" />
              <span className="text-2xl font-bold">
                {Math.floor(durationSeconds / 60)}:
                {String(durationSeconds % 60).padStart(2, "0")}
              </span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              {t("sessions.duration")}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2">
              <Database className="h-5 w-5 text-green-500" />
              <span className="text-2xl font-bold">{totalBatches}</span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              {t("recording.dataBatches")}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2">
              <Activity className="h-5 w-5 text-purple-500" />
              <span className="text-2xl font-bold">
                {ecgData === undefined
                  ? "-"
                  : intl.number(recording.totalSamples)}
              </span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              {isPartial
                ? t("recording.samplesPartial", {
                    loaded: recording.batchCount,
                    total: totalBatches,
                  })
                : t("recording.samples")}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Live Sensor Charts - Professional visualizations for each sensor type */}
      <LiveSensorDisplay
        sessionId={sessionId}
        ecgData={ecgData ?? []}
        sampleRate={sampleRate}
        channels={session.channels}
      />

      {/* No data message */}
      {!hasData && (
        <Card className="border-dashed border-2">
          <CardContent className="py-12 text-center">
            <Activity className="h-16 w-16 mx-auto text-muted-foreground mb-4 animate-pulse" />
            <h3 className="text-xl font-medium mb-2">
              {t("sessionLive.waitingTitle")}
            </h3>
            <p className="text-muted-foreground max-w-lg mx-auto mb-4">
              {t("sessionLive.waitingDescription")}
            </p>
            {session.sampleRate !== undefined && (
              <p className="text-sm text-muted-foreground">
                {t("recording.acquisitionRate", {
                  rate: String(session.sampleRate),
                })}
              </p>
            )}
          </CardContent>
        </Card>
      )}

      {/* Data Explanation Card */}
      <Card className="bg-muted/30">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">
            {t("sessionLive.explainTitle")}
          </CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground space-y-2">
          <p>
            {ecgUnit ? (
              <>
                <strong>
                  {t("sessionLive.yAxisLabelTreated", { unit: ecgUnit })}
                </strong>{" "}
                {t("sessionLive.yAxisTextTreated")}
              </>
            ) : (
              <>
                <strong>{t("sessionLive.yAxisLabelRaw")}</strong>{" "}
                {t("sessionLive.yAxisTextRaw")}
              </>
            )}
          </p>
          <p>
            <strong>{t("sessionLive.xAxisLabel")}</strong>{" "}
            {t("sessionLive.xAxisText")}
          </p>
          {recordedRates && (
            <p>
              <strong>{t("sessionLive.sampleRateLabel")}</strong>{" "}
              {t("sessionLive.sampleRateText", { rate: recordedRates })}
            </p>
          )}
          <p>
            <strong>{t("sessionLive.heartRateLabel")}</strong>{" "}
            {t("sessionLive.heartRateText")}
          </p>
        </CardContent>
      </Card>

      {/* End Session Dialog */}
      <Dialog open={showEndDialog} onOpenChange={setShowEndDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("sessionLive.endDialogTitle")}</DialogTitle>
            <DialogDescription>
              {t("sessionLive.endDialogDescription", { name: riderName })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowEndDialog(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="destructive"
              onClick={handleEndSession}
              disabled={ending}
            >
              {ending ? t("sessionLive.ending") : t("sessions.end")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function LiveSessionSkeleton() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-4 w-64 mt-2" />
        </div>
        <div className="flex items-center gap-3">
          <Skeleton className="h-6 w-16" />
          <Skeleton className="h-10 w-32" />
        </div>
      </div>
      <div className="grid grid-cols-4 gap-4">
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
      </div>
      <Skeleton className="h-[400px]" />
    </div>
  );
}

"use client";

import { use } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations, useLocale, useFormatter } from "next-intl";
import { Link } from "@/i18n/navigation";

import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import {
  User,
  Cpu,
  Clock,
  FileText,
  Activity,
  Database,
  ArrowLeft,
  Play,
  Heart,
  Calendar,
  Timer,
  Radio,
  AlertCircle,
} from "lucide-react";
import { format } from "date-fns";
import { fr, enUS } from "date-fns/locale";
import { ECGWaveform } from "@/components/ECGWaveform";
import { useSessionStatusLabel } from "@/components/dashboard/statusLabels";
import {
  formatSampleRates,
  recordingCoverage,
  summarizeRecording,
} from "@/lib/ecg/stats";
import { isTrainingKind } from "@/lib/training";
import { TrainingDetailsCard } from "@/components/training/TrainingDetailsCard";
import {
  SessionKindBadge,
  SessionOriginBadge,
} from "@/components/training/TrainingBadges";

export default function SessionDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const t = useTranslations();
  const locale = useLocale();
  const intl = useFormatter();

  const sessionId = id as Id<"sessions">;
  const session = useQuery(api.sessions.getSession, { sessionId });
  const stats = useQuery(api.ecgData.getSessionDataStats, { sessionId });
  const training = useQuery(api.training.getTrainingSession, { sessionId });

  // For active sessions, get recent data; for completed/failed, get all data
  const recentEcgData = useQuery(
    api.ecgData.getRecentEcgData,
    session?.status === "active" ? { sessionId, seconds: 30 } : "skip",
  );

  const allEcgData = useQuery(
    api.ecgData.getSessionAllData,
    session?.status !== "active" ? { sessionId, maxBatches: 200 } : "skip",
  );

  // Use the appropriate data based on session status
  const ecgData = session?.status === "active" ? recentEcgData : allEcgData;

  const dateLocale = locale === "fr" ? fr : enUS;

  if (session === undefined) {
    return <SessionDetailSkeleton />;
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

  const duration = session.endedAt
    ? formatDuration(session.endedAt - session.startedAt)
    : t("sessions.inProgress");

  // Process ECG data for display
  const ecgSamples: Record<string, number[]> = {};
  let isTreated = false;
  let transmitRate: number | undefined;
  if (ecgData) {
    for (const batch of ecgData) {
      if (batch.sampleRate) transmitRate = batch.sampleRate;
      for (const sample of batch.samples) {
        if (sample.unit) isTreated = true;
        if (!ecgSamples[sample.channel]) {
          ecgSamples[sample.channel] = [];
        }
        ecgSamples[sample.channel].push(...sample.values);
      }
    }
  }
  // Treated data arrives at the transmit rate (~250 Hz); legacy raw is 1000 Hz.
  const displayRate = isTreated
    ? (transmitRate ?? 250)
    : (session.sampleRate ?? 1000);

  // Sample counts and rates are read from the loaded batches, never assumed.
  // Only part of a long recording is loaded here, so the count says when it is partial.
  const recording = summarizeRecording(ecgData ?? [], session.sampleRate);
  const recordedRates = formatSampleRates(recording.sampleRates);
  const coverage = recordingCoverage(recording.batchCount, stats?.totalBatches);
  const isPartial = coverage === "partial";
  // Until the batches and the server's batch count are both in, a count over
  // the loaded batches cannot be labelled total or partial: show a placeholder.
  const countsReady = ecgData !== undefined && coverage !== "unknown";
  const countValues = {
    channels: recording.channelCount,
    loaded: recording.batchCount,
    total: stats?.totalBatches ?? recording.batchCount,
  };

  return (
    <div className="space-y-6">
      {/* Back button and Header */}
      <div className="flex items-center gap-4">
        <Link href="/dashboard/sessions">
          <Button variant="ghost" size="icon">
            <ArrowLeft className="h-5 w-5" />
          </Button>
        </Link>
        <div className="flex-1">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold">{riderName}</h1>
            <SessionStatusBadge status={session.status} />
            <SessionKindBadge kind={session.kind} />
            <SessionOriginBadge origin={training?.origin} />
          </div>
          <p className="text-muted-foreground">
            {session.machine.name} •{" "}
            {format(session.startedAt, "PPP", { locale: dateLocale })}
          </p>
        </div>
        {(session.status === "active" ||
          (isTraining && session.status === "pending")) && (
          <Link href={`/dashboard/sessions/${sessionId}/live`}>
            <Button>
              <Radio className="h-4 w-4 mr-2 animate-pulse" />
              {t("sessions.viewLive")}
            </Button>
          </Link>
        )}
      </div>

      {/* Failed Session Warning */}
      {session.status === "failed" && (
        <Card className="border-red-500 bg-red-50 dark:bg-red-950">
          <CardContent className="py-4">
            <div className="flex items-start gap-3">
              <AlertCircle className="h-6 w-6 text-red-600 flex-shrink-0 mt-0.5" />
              <div>
                <h3 className="font-semibold text-red-800 dark:text-red-200">
                  {t("sessionDetail.failedTitle")}
                </h3>
                <p className="text-sm text-red-700 dark:text-red-300 mt-1">
                  {t("sessionDetail.failedDescription")}
                </p>
                {training?.endReason && (
                  <p className="text-sm text-red-600 dark:text-red-400 mt-2 font-mono">
                    {training.endReason}
                  </p>
                )}
                {session.notes && session.notes.includes("Failure reason:") && (
                  <p className="text-sm text-red-600 dark:text-red-400 mt-2 font-mono">
                    {session.notes.split("Failure reason:")[1]?.trim()}
                  </p>
                )}
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Pending Session Info */}
      {session.status === "pending" && (
        <Card className="border-yellow-500 bg-yellow-50 dark:bg-yellow-950">
          <CardContent className="py-4">
            <div className="flex items-start gap-3">
              <Clock className="h-6 w-6 text-yellow-600 flex-shrink-0 mt-0.5" />
              <div>
                <h3 className="font-semibold text-yellow-800 dark:text-yellow-200">
                  {t("sessionDetail.pendingTitle")}
                </h3>
                <p className="text-sm text-yellow-700 dark:text-yellow-300 mt-1">
                  {isTraining
                    ? t("training.session.pending")
                    : t("sessionDetail.pendingDescription")}
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Quick Stats */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
        <Card>
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2">
              <Timer className="h-5 w-5 text-blue-500" />
              <span className="text-xl font-bold">{duration}</span>
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
              <span className="text-xl font-bold">
                {stats?.totalBatches ?? 0}
              </span>
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
              <span className="text-xl font-bold">
                {countsReady ? intl.number(recording.totalSamples) : "-"}
              </span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              {isPartial
                ? t("recording.samplesPartial", countValues)
                : t("recording.samples", countValues)}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2">
              <Activity className="h-5 w-5 text-orange-500" />
              <span className="text-xl font-bold">
                {session.channels.length}
              </span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              {t("machines.channels")}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2">
              <Heart className="h-5 w-5 text-red-500" />
              <span className="text-xl font-bold">
                {countsReady ? (recordedRates ?? "-") : "-"}
              </span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              {isPartial
                ? t("recording.sampleRateHzPartial")
                : t("recording.sampleRateHz")}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Training parameters, end reason and telemetry (auto / manual) */}
      {isTraining && <TrainingDetailsCard sessionId={sessionId} />}

      {/* ECG Data Visualization */}
      {Object.keys(ecgSamples).length > 0 ? (
        <div className="space-y-4">
          <div>
            <h2 className="text-lg font-semibold flex items-center gap-2">
              <Activity className="h-5 w-5" />
              {t("sessionDetail.ecgRecording")}
            </h2>
            {isPartial && (
              <p className="text-sm text-muted-foreground">
                {t("recording.partialNote", countValues)}
              </p>
            )}
          </div>
          {session.channels.map((channel) => (
            <Card key={channel}>
              <CardHeader className="pb-2">
                <CardTitle className="text-base flex items-center gap-2">
                  <Activity className="h-4 w-4 text-green-500" />
                  {t("sessionDetail.channelTitle", { channel })}
                </CardTitle>
                <CardDescription>
                  {t("sessionDetail.samplesRecorded", {
                    count: ecgSamples[channel]?.length ?? 0,
                  })}
                </CardDescription>
              </CardHeader>
              <CardContent>
                {ecgSamples[channel] && ecgSamples[channel].length > 0 ? (
                  <ECGWaveform
                    data={ecgSamples[channel]}
                    channel={channel}
                    sampleRate={displayRate}
                    displaySeconds={10}
                    height={250}
                    preFiltered={isTreated}
                  />
                ) : (
                  <div className="h-[250px] flex items-center justify-center bg-muted/20 rounded-lg border border-dashed">
                    <p className="text-muted-foreground">
                      {t("sessionDetail.noChannelData", { channel })}
                    </p>
                  </div>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Activity className="h-5 w-5" />
              {t("sessionDetail.ecgRecording")}
            </CardTitle>
          </CardHeader>
          <CardContent className="h-48 flex items-center justify-center text-muted-foreground">
            {session.status === "active" ? (
              <div className="text-center">
                <Activity className="h-12 w-12 mx-auto mb-3 animate-pulse" />
                <p>{t("sessionDetail.sessionActive")}</p>
                <Link href={`/dashboard/sessions/${sessionId}/live`}>
                  <Button variant="link" className="mt-2">
                    <Play className="h-4 w-4 mr-2" />
                    {t("sessionDetail.viewLiveRecording")}
                  </Button>
                </Link>
              </div>
            ) : session.status === "pending" ? (
              <div className="text-center">
                <Clock className="h-12 w-12 mx-auto mb-3" />
                <p>{t("sessionDetail.waitingForDevice")}</p>
              </div>
            ) : (
              <div className="text-center">
                <Database className="h-12 w-12 mx-auto mb-3" />
                <p>{t("sessionDetail.noEcgData")}</p>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Session Details Grid */}
      <div className="grid md:grid-cols-2 gap-6">
        {/* Patient Info */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <User className="h-4 w-4" />
              {t("reports.patientInfo")}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div>
              <p className="text-sm text-muted-foreground">
                {t("common.name")}
              </p>
              <p className="font-medium">{riderName}</p>
            </div>
            {session.patient && (
              <div>
                <p className="text-sm text-muted-foreground">
                  {t("users.email")}
                </p>
                <p className="font-medium">{session.patient.email}</p>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Session Timing */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Calendar className="h-4 w-4" />
              {t("sessionDetail.timing")}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <p className="text-sm text-muted-foreground">
                  {t("sessionDetail.started")}
                </p>
                <p className="font-medium">
                  {format(session.startedAt, "PPp", { locale: dateLocale })}
                </p>
              </div>
              <div>
                <p className="text-sm text-muted-foreground">
                  {t("sessions.duration")}
                </p>
                <p className="font-medium">{duration}</p>
              </div>
            </div>
            {session.endedAt && (
              <div>
                <p className="text-sm text-muted-foreground">
                  {t("sessionDetail.ended")}
                </p>
                <p className="font-medium">
                  {format(session.endedAt, "PPp", { locale: dateLocale })}
                </p>
              </div>
            )}
            {session.startedBy && (
              <div>
                <p className="text-sm text-muted-foreground">
                  {t("sessions.startedBy")}
                </p>
                <p className="font-medium">
                  {session.startedBy.firstName} {session.startedBy.lastName}
                </p>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Machine and Channels */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Cpu className="h-4 w-4" />
            {t("sessionDetail.recordingDevice")}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex justify-between items-center">
            <div>
              <p className="font-medium text-lg">{session.machine.name}</p>
              {session.sampleRate !== undefined && (
                <p className="text-sm text-muted-foreground">
                  {t("recording.acquisitionRate", {
                    rate: String(session.sampleRate),
                  })}
                </p>
              )}
            </div>
          </div>
          <Separator />
          <div>
            <p className="text-sm text-muted-foreground mb-2">
              {t("sessionDetail.recordedChannels")}
            </p>
            <div className="flex gap-2 flex-wrap">
              {session.channels.map((ch) => (
                <Badge key={ch} variant="secondary" className="text-sm">
                  <Activity className="h-3 w-3 mr-1" />
                  {ch}
                </Badge>
              ))}
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Notes */}
      {session.notes && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <FileText className="h-4 w-4" />
              {t("sessionDetail.notes")}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="whitespace-pre-wrap">{session.notes}</p>
          </CardContent>
        </Card>
      )}

      {/* Data Summary */}
      {stats && stats.totalBatches > 0 && (
        <Card className="bg-muted/30">
          <CardHeader>
            <CardTitle className="text-sm">
              {t("sessionDetail.summary")}
            </CardTitle>
          </CardHeader>
          <CardContent className="text-sm space-y-2">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div>
                <p className="text-muted-foreground">
                  {t("recording.dataBatches")}
                </p>
                <p className="font-medium">{stats.totalBatches}</p>
              </div>
              <div>
                <p className="text-muted-foreground">
                  {t("recording.samples", countValues)}
                </p>
                <p className="font-medium">
                  {countsReady ? intl.number(recording.totalSamples) : "-"}
                </p>
                {isPartial && (
                  <p className="text-xs text-muted-foreground">
                    {t("recording.partialNote", countValues)}
                  </p>
                )}
              </div>
              <div>
                <p className="text-muted-foreground">
                  {t("sessionDetail.dataDuration")}
                </p>
                <p className="font-medium">
                  {t("sessionDetail.seconds", {
                    seconds: stats.durationSeconds,
                  })}
                </p>
              </div>
              <div>
                <p className="text-muted-foreground">
                  {t("machines.channels")}
                </p>
                <p className="font-medium">{stats.channels.join(", ")}</p>
              </div>
            </div>
            {stats.firstTimestamp && stats.lastTimestamp && (
              <div className="pt-2 border-t">
                <p className="text-muted-foreground">
                  {t("sessionDetail.recordedRange", {
                    start: format(stats.firstTimestamp, "HH:mm:ss"),
                    end: format(stats.lastTimestamp, "HH:mm:ss"),
                  })}
                </p>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function SessionStatusBadge({ status }: { status: string }) {
  const statusLabel = useSessionStatusLabel();
  const variants: Record<
    string,
    "default" | "secondary" | "destructive" | "outline"
  > = {
    active: "default",
    completed: "secondary",
    pending: "outline",
    failed: "destructive",
  };

  // Unknown statuses are shown as received rather than hidden.
  return (
    <Badge variant={variants[status] || "outline"} className="text-sm">
      {statusLabel(status)}
    </Badge>
  );
}

function formatDuration(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);

  if (hours > 0) {
    return `${hours}h ${minutes % 60}m ${seconds % 60}s`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds % 60}s`;
  }
  return `${seconds}s`;
}

function SessionDetailSkeleton() {
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <Skeleton className="h-10 w-10" />
        <div className="flex-1">
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-4 w-32 mt-2" />
        </div>
        <Skeleton className="h-6 w-24" />
      </div>
      <div className="grid grid-cols-5 gap-4">
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
      </div>
      <Skeleton className="h-[300px]" />
      <div className="grid md:grid-cols-2 gap-6">
        <Skeleton className="h-40" />
        <Skeleton className="h-40" />
      </div>
      <Skeleton className="h-32" />
    </div>
  );
}

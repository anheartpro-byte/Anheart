"use client";

import { use } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations, useLocale } from "next-intl";
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
import {
  User,
  Clock,
  FileText,
  Activity,
  Archive,
  ArrowLeft,
  Calendar,
  Radio,
  AlertCircle,
} from "lucide-react";
import { format } from "date-fns";
import { fr, enUS } from "date-fns/locale";
import { useSessionStatusLabel } from "@/components/dashboard/statusLabels";
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

  const sessionId = id as Id<"sessions">;
  const session = useQuery(api.sessions.getSession, { sessionId });
  const training = useQuery(api.training.getTrainingSession, { sessionId });
  // Read-only history of the former ECG recording mode: only its sessions
  // have ECG batches, so nothing is asked for a training session.
  const legacyStats = useQuery(
    api.ecgData.getSessionDataStats,
    session && !isTrainingKind(session.kind) ? { sessionId } : "skip",
  );

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

  // A session of the former recording mode that never ended is not running.
  const duration = session.endedAt
    ? formatDuration(session.endedAt - session.startedAt)
    : isTraining
      ? t("sessions.inProgress")
      : "-";

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
        {isTraining &&
          (session.status === "active" || session.status === "pending") && (
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
      {isTraining && session.status === "pending" && (
        <Card className="border-yellow-500 bg-yellow-50 dark:bg-yellow-950">
          <CardContent className="py-4">
            <div className="flex items-start gap-3">
              <Clock className="h-6 w-6 text-yellow-600 flex-shrink-0 mt-0.5" />
              <div>
                <h3 className="font-semibold text-yellow-800 dark:text-yellow-200">
                  {t("sessionDetail.pendingTitle")}
                </h3>
                <p className="text-sm text-yellow-700 dark:text-yellow-300 mt-1">
                  {t("training.session.pending")}
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Training parameters, end reason and telemetry (auto / manual) */}
      {isTraining && <TrainingDetailsCard sessionId={sessionId} />}

      {/* Former ECG recording mode: read-only history */}
      {!isTraining && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Archive className="h-4 w-4" />
              {t("sessionDetail.legacyTitle")}
            </CardTitle>
            <CardDescription>
              {t("sessionDetail.legacyDescription")}
            </CardDescription>
          </CardHeader>
          <CardContent className="text-sm space-y-3">
            <div>
              <p className="text-muted-foreground mb-2">
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
            {legacyStats && legacyStats.totalBatches > 0 ? (
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <p className="text-muted-foreground">
                    {t("sessionDetail.dataBatches")}
                  </p>
                  <p className="font-medium">{legacyStats.totalBatches}</p>
                </div>
                <div>
                  <p className="text-muted-foreground">
                    {t("sessionDetail.dataDuration")}
                  </p>
                  <p className="font-medium">
                    {t("sessionDetail.seconds", {
                      seconds: legacyStats.durationSeconds,
                    })}
                  </p>
                </div>
                {legacyStats.firstTimestamp && legacyStats.lastTimestamp && (
                  <p className="col-span-2 pt-2 border-t text-muted-foreground">
                    {t("sessionDetail.recordedRange", {
                      start: format(legacyStats.firstTimestamp, "HH:mm:ss"),
                      end: format(legacyStats.lastTimestamp, "HH:mm:ss"),
                    })}
                  </p>
                )}
              </div>
            ) : (
              legacyStats && (
                <p className="text-muted-foreground">
                  {t("sessionDetail.noEcgData")}
                </p>
              )
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
      <Skeleton className="h-[300px]" />
      <div className="grid md:grid-cols-2 gap-6">
        <Skeleton className="h-40" />
        <Skeleton className="h-40" />
      </div>
      <Skeleton className="h-32" />
    </div>
  );
}

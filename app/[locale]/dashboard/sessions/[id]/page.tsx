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
  const stats = useQuery(api.ecgData.getSessionDataStats, { sessionId });

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
        <p className="text-muted-foreground">Session not found</p>
        <Link
          href="/dashboard/sessions"
          className="text-primary hover:underline mt-2 inline-block"
        >
          {t("common.back")}
        </Link>
      </div>
    );
  }

  const duration = session.endedAt
    ? formatDuration(session.endedAt - session.startedAt)
    : "In progress";

  // Process ECG data for display
  const ecgSamples: Record<string, number[]> = {};
  if (ecgData) {
    for (const batch of ecgData) {
      for (const sample of batch.samples) {
        if (!ecgSamples[sample.channel]) {
          ecgSamples[sample.channel] = [];
        }
        ecgSamples[sample.channel].push(...sample.values);
      }
    }
  }

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
            <h1 className="text-2xl font-bold">
              {session.patient.firstName} {session.patient.lastName}
            </h1>
            <SessionStatusBadge status={session.status} />
          </div>
          <p className="text-muted-foreground">
            {session.machine.name} •{" "}
            {format(session.startedAt, "PPP", { locale: dateLocale })}
          </p>
        </div>
        {session.status === "active" && (
          <Link href={`/dashboard/sessions/${sessionId}/live`}>
            <Button>
              <Radio className="h-4 w-4 mr-2 animate-pulse" />
              View Live
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
                  Session Failed
                </h3>
                <p className="text-sm text-red-700 dark:text-red-300 mt-1">
                  This session encountered an error and was terminated. Any data
                  recorded before the failure is shown below.
                </p>
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
                  Waiting for Device
                </h3>
                <p className="text-sm text-yellow-700 dark:text-yellow-300 mt-1">
                  This session is waiting for the recording device to connect
                  and start capturing data.
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
            <p className="text-xs text-muted-foreground mt-1">Duration</p>
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
            <p className="text-xs text-muted-foreground mt-1">Data Batches</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2">
              <Activity className="h-5 w-5 text-purple-500" />
              <span className="text-xl font-bold">
                {(((stats?.totalBatches ?? 0) * 1000) / 1000).toFixed(0)}K
              </span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">Total Samples</p>
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
            <p className="text-xs text-muted-foreground mt-1">Channels</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2">
              <Heart className="h-5 w-5 text-red-500" />
              <span className="text-xl font-bold">1000</span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              Sample Rate (Hz)
            </p>
          </CardContent>
        </Card>
      </div>

      {/* ECG Data Visualization */}
      {Object.keys(ecgSamples).length > 0 ? (
        <div className="space-y-4">
          <h2 className="text-lg font-semibold flex items-center gap-2">
            <Activity className="h-5 w-5" />
            ECG Recording
          </h2>
          {session.channels.map((channel) => (
            <Card key={channel}>
              <CardHeader className="pb-2">
                <CardTitle className="text-base flex items-center gap-2">
                  <Activity className="h-4 w-4 text-green-500" />
                  {channel} Channel
                </CardTitle>
                <CardDescription>
                  {ecgSamples[channel]?.length ?? 0} samples recorded
                </CardDescription>
              </CardHeader>
              <CardContent>
                {ecgSamples[channel] && ecgSamples[channel].length > 0 ? (
                  <ECGWaveform
                    data={ecgSamples[channel]}
                    channel={channel}
                    sampleRate={100}
                    displaySeconds={10}
                    height={250}
                  />
                ) : (
                  <div className="h-[250px] flex items-center justify-center bg-muted/20 rounded-lg border border-dashed">
                    <p className="text-muted-foreground">
                      No data for {channel}
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
              ECG Recording
            </CardTitle>
          </CardHeader>
          <CardContent className="h-48 flex items-center justify-center text-muted-foreground">
            {session.status === "active" ? (
              <div className="text-center">
                <Activity className="h-12 w-12 mx-auto mb-3 animate-pulse" />
                <p>Session is active</p>
                <Link href={`/dashboard/sessions/${sessionId}/live`}>
                  <Button variant="link" className="mt-2">
                    <Play className="h-4 w-4 mr-2" />
                    View Live Recording
                  </Button>
                </Link>
              </div>
            ) : session.status === "pending" ? (
              <div className="text-center">
                <Clock className="h-12 w-12 mx-auto mb-3" />
                <p>Waiting for device to start recording...</p>
              </div>
            ) : (
              <div className="text-center">
                <Database className="h-12 w-12 mx-auto mb-3" />
                <p>No ECG data recorded for this session</p>
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
              Patient Information
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div>
              <p className="text-sm text-muted-foreground">Name</p>
              <p className="font-medium">
                {session.patient.firstName} {session.patient.lastName}
              </p>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">Email</p>
              <p className="font-medium">{session.patient.email}</p>
            </div>
          </CardContent>
        </Card>

        {/* Session Timing */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Calendar className="h-4 w-4" />
              Session Timing
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <p className="text-sm text-muted-foreground">Started</p>
                <p className="font-medium">
                  {format(session.startedAt, "PPp", { locale: dateLocale })}
                </p>
              </div>
              <div>
                <p className="text-sm text-muted-foreground">Duration</p>
                <p className="font-medium">{duration}</p>
              </div>
            </div>
            {session.endedAt && (
              <div>
                <p className="text-sm text-muted-foreground">Ended</p>
                <p className="font-medium">
                  {format(session.endedAt, "PPp", { locale: dateLocale })}
                </p>
              </div>
            )}
            {session.startedBy && (
              <div>
                <p className="text-sm text-muted-foreground">Started By</p>
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
            Recording Device
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex justify-between items-center">
            <div>
              <p className="font-medium text-lg">{session.machine.name}</p>
              <p className="text-sm text-muted-foreground">
                Sample Rate: 1000 Hz • Resolution: 10-bit
              </p>
            </div>
          </div>
          <Separator />
          <div>
            <p className="text-sm text-muted-foreground mb-2">
              Recorded Channels
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
              Session Notes
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
            <CardTitle className="text-sm">Recording Summary</CardTitle>
          </CardHeader>
          <CardContent className="text-sm space-y-2">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div>
                <p className="text-muted-foreground">Total Batches</p>
                <p className="font-medium">{stats.totalBatches}</p>
              </div>
              <div>
                <p className="text-muted-foreground">Total Samples</p>
                <p className="font-medium">
                  {(stats.totalBatches * 1000).toLocaleString()}
                </p>
              </div>
              <div>
                <p className="text-muted-foreground">Data Duration</p>
                <p className="font-medium">{stats.durationSeconds}s</p>
              </div>
              <div>
                <p className="text-muted-foreground">Channels</p>
                <p className="font-medium">{stats.channels.join(", ")}</p>
              </div>
            </div>
            {stats.firstTimestamp && stats.lastTimestamp && (
              <div className="pt-2 border-t">
                <p className="text-muted-foreground">
                  Data recorded from {format(stats.firstTimestamp, "HH:mm:ss")}{" "}
                  to {format(stats.lastTimestamp, "HH:mm:ss")}
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
  const variants: Record<
    string,
    "default" | "secondary" | "destructive" | "outline"
  > = {
    active: "default",
    completed: "secondary",
    pending: "outline",
    failed: "destructive",
  };

  const labels: Record<string, string> = {
    active: "Active",
    completed: "Completed",
    pending: "Pending",
    failed: "Failed",
  };

  return (
    <Badge variant={variants[status] || "outline"} className="text-sm">
      {labels[status] || status}
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

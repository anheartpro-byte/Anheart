"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import {
  FileText,
  Download,
  Calendar,
  Clock,
  User,
  Cpu,
  Loader2,
  Eye,
} from "lucide-react";
import { Link } from "@/i18n/navigation";
import { generateSessionPdf } from "@/lib/generatePdf";

export default function ReportsPage() {
  const t = useTranslations("reports");
  const user = useQuery(api.users.getCurrentUser);
  const sessions = useQuery(api.sessions.getCompletedSessionsForUser);

  const [downloadingId, setDownloadingId] = useState<string | null>(null);

  if (user === undefined || sessions === undefined) {
    return <ReportsPageSkeleton />;
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
        <p className="text-muted-foreground">{t("description")}</p>
      </div>

      {sessions && sessions.length > 0 ? (
        <div className="grid gap-4">
          {sessions.map((session) => (
            <SessionReportCard
              key={session._id}
              session={session}
              isDownloading={downloadingId === session._id}
              onDownload={() => setDownloadingId(session._id)}
              onDownloadComplete={() => setDownloadingId(null)}
            />
          ))}
        </div>
      ) : (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-16">
            <FileText className="h-12 w-12 text-muted-foreground/50 mb-4" />
            <p className="text-muted-foreground">{t("noReports")}</p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

interface SessionReportCardProps {
  session: {
    _id: Id<"sessions">;
    status: string;
    startedAt: number;
    endedAt?: number;
    channels: string[];
    notes?: string;
    patientName: string;
    machineName: string;
  };
  isDownloading: boolean;
  onDownload: () => void;
  onDownloadComplete: () => void;
}

function SessionReportCard({
  session,
  isDownloading,
  onDownload,
  onDownloadComplete,
}: SessionReportCardProps) {
  const t = useTranslations("reports");

  // Fetch session details and ECG data for PDF
  const sessionDetails = useQuery(api.sessions.getSession, {
    sessionId: session._id,
  });
  const ecgStats = useQuery(api.ecgData.getSessionDataStats, {
    sessionId: session._id,
  });
  const ecgData = useQuery(api.ecgData.getSessionAllData, {
    sessionId: session._id,
    maxBatches: 50, // Limit for PDF preview
  });

  const handleDownload = async () => {
    if (!sessionDetails) return;

    onDownload();

    try {
      // Process ECG data for PDF
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

      // Generate PDF
      generateSessionPdf({
        sessionId: session._id,
        patientName:
          sessionDetails.patient.firstName +
          " " +
          sessionDetails.patient.lastName,
        patientEmail: sessionDetails.patient.email,
        machineName: sessionDetails.machine.name,
        startedAt: sessionDetails.startedAt,
        endedAt: sessionDetails.endedAt,
        channels: sessionDetails.channels,
        notes: sessionDetails.notes,
        ecgStats: ecgStats
          ? {
              totalBatches: ecgStats.totalBatches,
              durationSeconds: ecgStats.durationSeconds,
              channels: ecgStats.channels,
            }
          : undefined,
        ecgSamples,
      });
    } catch (error) {
      console.error("Error generating PDF:", error);
    } finally {
      onDownloadComplete();
    }
  };

  const duration = session.endedAt
    ? Math.round((session.endedAt - session.startedAt) / 60000)
    : 0;

  return (
    <Card className="hover:shadow-md transition-shadow">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="text-base font-medium flex items-center gap-2">
            <FileText className="h-4 w-4 text-muted-foreground" />
            {t("sessionReport")} - {session._id.slice(-8).toUpperCase()}
          </CardTitle>
          <div className="flex items-center gap-2">
            <Link href={`/dashboard/sessions/${session._id}`}>
              <Button variant="ghost" size="sm">
                <Eye className="h-4 w-4 mr-2" />
                {t("view") || "View"}
              </Button>
            </Link>
            <Button
              variant="outline"
              size="sm"
              onClick={handleDownload}
              disabled={isDownloading || !sessionDetails}
            >
              {isDownloading ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : (
                <Download className="h-4 w-4 mr-2" />
              )}
              {t("download")}
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <div className="flex flex-wrap gap-4 text-sm">
          <div className="flex items-center gap-2 text-muted-foreground">
            <User className="h-4 w-4" />
            <span className="font-medium text-foreground">
              {session.patientName}
            </span>
          </div>
          <div className="flex items-center gap-2 text-muted-foreground">
            <Cpu className="h-4 w-4" />
            <span>{session.machineName}</span>
          </div>
          <div className="flex items-center gap-2 text-muted-foreground">
            <Calendar className="h-4 w-4" />
            <span>{new Date(session.startedAt).toLocaleDateString()}</span>
          </div>
          <div className="flex items-center gap-2 text-muted-foreground">
            <Clock className="h-4 w-4" />
            <span>{duration > 0 ? `${duration} min` : "-"}</span>
          </div>
          <div className="flex gap-1">
            {session.channels.map((ch) => (
              <Badge key={ch} variant="secondary" className="text-xs">
                {ch}
              </Badge>
            ))}
          </div>
        </div>
        {ecgStats && ecgStats.totalBatches > 0 && (
          <div className="mt-3 pt-3 border-t text-xs text-muted-foreground">
            {ecgStats.totalBatches} data batches • {ecgStats.durationSeconds}s
            of recording • ~{ecgStats.totalBatches * 100} samples
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ReportsPageSkeleton() {
  return (
    <div className="space-y-6">
      <div>
        <Skeleton className="h-8 w-48 mb-2" />
        <Skeleton className="h-4 w-72" />
      </div>
      <div className="grid gap-4">
        {[1, 2, 3].map((i) => (
          <Card key={i}>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <Skeleton className="h-5 w-48" />
                <Skeleton className="h-9 w-28" />
              </div>
            </CardHeader>
            <CardContent>
              <div className="flex gap-6">
                <Skeleton className="h-4 w-32" />
                <Skeleton className="h-4 w-24" />
                <Skeleton className="h-4 w-20" />
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

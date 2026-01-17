"use client";

import { use } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations, useLocale } from "next-intl";
import { Link } from "@/i18n/navigation";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import { User, Cpu, Clock, FileText } from "lucide-react";
import { format } from "date-fns";
import { fr, enUS } from "date-fns/locale";

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

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">
            {session.patient.firstName} {session.patient.lastName}
          </h1>
          <p className="text-muted-foreground">{session.machine.name}</p>
        </div>
        <SessionStatusBadge status={session.status} />
      </div>

      {/* Session Info Grid */}
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
              <p className="text-sm text-muted-foreground">Name</p>
              <p className="font-medium">
                {session.patient.firstName} {session.patient.lastName}
              </p>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">
                {t("users.email")}
              </p>
              <p className="font-medium">{session.patient.email}</p>
            </div>
          </CardContent>
        </Card>

        {/* Session Info */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <FileText className="h-4 w-4" />
              {t("reports.sessionInfo")}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex justify-between">
              <div>
                <p className="text-sm text-muted-foreground">
                  {t("sessions.startedAt")}
                </p>
                <p className="font-medium">
                  {format(session.startedAt, "PPpp", { locale: dateLocale })}
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
                  {t("sessions.endedAt")}
                </p>
                <p className="font-medium">
                  {format(session.endedAt, "PPpp", { locale: dateLocale })}
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
            {t("sessions.machine")}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex justify-between items-center">
            <div>
              <p className="font-medium">{session.machine.name}</p>
              {session.technician && (
                <p className="text-sm text-muted-foreground">
                  {t("sessions.technician")}: {session.technician.firstName}{" "}
                  {session.technician.lastName}
                </p>
              )}
            </div>
          </div>
          <Separator />
          <div>
            <p className="text-sm text-muted-foreground mb-2">
              {t("machines.channels")}
            </p>
            <div className="flex gap-2 flex-wrap">
              {session.channels.map((ch) => (
                <Badge key={ch} variant="secondary">
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
            <CardTitle>{t("sessions.notes")}</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="whitespace-pre-wrap">{session.notes}</p>
          </CardContent>
        </Card>
      )}

      {/* ECG Placeholder */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Clock className="h-4 w-4" />
            {t("reports.ecgOverview")}
          </CardTitle>
        </CardHeader>
        <CardContent className="h-48 flex items-center justify-center text-muted-foreground">
          ECG visualization would appear here
        </CardContent>
      </Card>
    </div>
  );
}

function SessionStatusBadge({ status }: { status: string }) {
  const t = useTranslations("sessions");

  const variants: Record<
    string,
    "default" | "secondary" | "destructive" | "outline"
  > = {
    active: "default",
    completed: "secondary",
    pending: "outline",
    failed: "destructive",
  };

  return (
    <Badge variant={variants[status] || "outline"} className="text-sm">
      {t(status as "active" | "completed" | "pending" | "failed")}
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
    <div className="max-w-3xl mx-auto space-y-6">
      <div className="flex items-center gap-4">
        <Skeleton className="h-10 w-10" />
        <div className="flex-1">
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-4 w-32 mt-2" />
        </div>
        <Skeleton className="h-6 w-24" />
      </div>
      <div className="grid md:grid-cols-2 gap-6">
        <Skeleton className="h-40" />
        <Skeleton className="h-40" />
      </div>
      <Skeleton className="h-32" />
      <Skeleton className="h-48" />
    </div>
  );
}

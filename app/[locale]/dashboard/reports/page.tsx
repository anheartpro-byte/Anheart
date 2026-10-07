"use client";

import { useTranslations, useLocale } from "next-intl";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { FileText, Calendar, Clock, User, Cpu, Eye } from "lucide-react";
import { Link } from "@/i18n/navigation";

export default function ReportsPage() {
  const t = useTranslations("reports");
  const user = useQuery(api.users.getCurrentUser);
  const sessions = useQuery(api.sessions.getCompletedSessionsForUser);

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
            <SessionReportCard key={session._id} session={session} />
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
    startedAt: number;
    endedAt?: number;
    patientName: string;
    machineName: string;
  };
}

/** One completed session, with a link to its detail page. */
function SessionReportCard({ session }: SessionReportCardProps) {
  const t = useTranslations("reports");
  const locale = useLocale();

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
          <Link href={`/dashboard/sessions/${session._id}`}>
            <Button variant="ghost" size="sm">
              <Eye className="h-4 w-4 mr-2" />
              {t("view")}
            </Button>
          </Link>
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
            <span>
              {new Date(session.startedAt).toLocaleDateString(locale)}
            </span>
          </div>
          <div className="flex items-center gap-2 text-muted-foreground">
            <Clock className="h-4 w-4" />
            <span>
              {duration > 0 ? t("durationMinutes", { minutes: duration }) : "-"}
            </span>
          </div>
        </div>
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

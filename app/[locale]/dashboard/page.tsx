"use client";

import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { useTranslations } from "next-intl";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Link } from "@/i18n/navigation";
import { Cpu, Activity, Users, Clock } from "lucide-react";
import { formatDistanceToNow } from "date-fns";
import { fr, enUS } from "date-fns/locale";
import { useLocale } from "next-intl";

export default function DashboardPage() {
  const t = useTranslations();
  const locale = useLocale();
  const user = useQuery(api.users.getCurrentUser);
  const machines = useQuery(api.machines.listMachines, {});
  const sessions = useQuery(api.sessions.listSessions, { limit: 10 });

  const dateLocale = locale === "fr" ? fr : enUS;

  if (user === undefined) {
    return <DashboardSkeleton />;
  }

  const onlineMachines =
    machines?.filter((m) => m.status === "online").length ?? 0;
  const activeSessions =
    sessions?.filter((s) => s.status === "active").length ?? 0;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t("dashboard.title")}</h1>
        <p className="text-muted-foreground">
          {t("dashboard.welcome", { name: user?.firstName || "" })}
        </p>
      </div>

      {/* Stats Cards */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {(user?.role === "admin" ||
          user?.role === "gestionnaire" ||
          user?.role === "technician") && (
          <Link href="/dashboard/machines">
            <Card className="hover:bg-accent/50 transition-colors cursor-pointer">
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium">
                  {t("dashboard.onlineMachines")}
                </CardTitle>
                <Cpu className="h-4 w-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{onlineMachines}</div>
                <p className="text-xs text-muted-foreground">
                  / {machines?.length ?? 0} total
                </p>
              </CardContent>
            </Card>
          </Link>
        )}

        <Link href="/dashboard/sessions">
          <Card className="hover:bg-accent/50 transition-colors cursor-pointer">
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <CardTitle className="text-sm font-medium">
                {t("dashboard.activeSessions")}
              </CardTitle>
              <Activity className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{activeSessions}</div>
              <p className="text-xs text-muted-foreground">
                {t("sessions.active")}
              </p>
            </CardContent>
          </Card>
        </Link>

        {(user?.role === "admin" || user?.role === "gestionnaire") && (
          <Link
            href={
              user?.role === "admin"
                ? "/dashboard/users"
                : "/dashboard/patients"
            }
          >
            <Card className="hover:bg-accent/50 transition-colors cursor-pointer">
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium">
                  {user?.role === "admin" ? t("nav.users") : t("nav.patients")}
                </CardTitle>
                <Users className="h-4 w-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">-</div>
                <p className="text-xs text-muted-foreground">
                  {t("common.loading")}
                </p>
              </CardContent>
            </Card>
          </Link>
        )}
      </div>

      {/* Recent Sessions */}
      <Card>
        <CardHeader>
          <CardTitle>{t("dashboard.recentSessions")}</CardTitle>
        </CardHeader>
        <CardContent>
          {sessions === undefined ? (
            <div className="space-y-2">
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </div>
          ) : sessions.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-8">
              {t("sessions.noSessions")}
            </p>
          ) : (
            <div className="space-y-3">
              {sessions.slice(0, 5).map((session) => (
                <Link
                  key={session._id}
                  href={
                    session.status === "active"
                      ? `/dashboard/sessions/${session._id}/live`
                      : `/dashboard/sessions/${session._id}`
                  }
                  className="flex items-center justify-between p-3 rounded-lg border hover:bg-accent/50 transition-colors"
                >
                  <div className="flex items-center gap-3">
                    <Activity className="h-4 w-4 text-muted-foreground" />
                    <div>
                      <p className="font-medium text-sm">
                        {session.patientName}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {session.machineName}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <StatusBadge status={session.status} />
                    <span className="text-xs text-muted-foreground flex items-center gap-1">
                      <Clock className="h-3 w-3" />
                      {formatDistanceToNow(session.startedAt, {
                        addSuffix: true,
                        locale: dateLocale,
                      })}
                    </span>
                  </div>
                </Link>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
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
    <Badge variant={variants[status] || "outline"} className="text-xs">
      {t(status as "active" | "completed" | "pending" | "failed")}
    </Badge>
  );
}

function DashboardSkeleton() {
  return (
    <div className="space-y-6">
      <div>
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-4 w-64 mt-2" />
      </div>
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        <Skeleton className="h-32" />
        <Skeleton className="h-32" />
        <Skeleton className="h-32" />
      </div>
      <Skeleton className="h-64" />
    </div>
  );
}

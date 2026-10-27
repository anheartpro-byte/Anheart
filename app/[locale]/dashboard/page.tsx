"use client";

import type { ReactNode } from "react";
import { useQuery } from "convex/react";
import type { FunctionReturnType } from "convex/server";
import { api } from "@/convex/_generated/api";
import {
  isAnheartAdmin,
  isOrganizationAdmin,
  isStaff,
} from "@/lib/organizationAccess";
import { useTranslations } from "next-intl";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { useSessionStatusLabel } from "@/components/dashboard/statusLabels";
import { Skeleton } from "@/components/ui/skeleton";
import { Link } from "@/i18n/navigation";
import { Cpu, Activity, Users, Clock, type LucideIcon } from "lucide-react";
import { formatDistanceToNow } from "date-fns";
import { fr, enUS } from "date-fns/locale";
import { useLocale } from "next-intl";
import { useConnectedMachines } from "@/components/machines/MachineSignal";
import { useQueryOutcome, type QueryOutcome } from "@/hooks/use-query-outcome";

type Counters = FunctionReturnType<typeof api.dashboard.counters>;

export default function DashboardPage() {
  const t = useTranslations();
  const locale = useLocale();
  const user = useQuery(api.users.getCurrentUser);
  // The figures of the cards: counted by the server, for the caller's
  // organisation (every organisation for the Anheart admin). A failure leaves
  // the cards saying so, and the rest of the page standing.
  const counters = useQueryOutcome(api.dashboard.counters, user ? {} : "skip");
  const sessions = useQuery(api.sessions.listSessions, user ? { limit: 10 } : "skip");

  const dateLocale = locale === "fr" ? fr : enUS;

  if (user === undefined) {
    return <DashboardSkeleton />;
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t("dashboard.title")}</h1>
        <p className="text-muted-foreground">
          {t("dashboard.welcome", { name: user?.firstName || "" })}
        </p>
        {isAnheartAdmin(user?.role) && (
          <p className="text-xs text-muted-foreground">
            {t("dashboard.allOrganizations")}
          </p>
        )}
      </div>

      {/* Stats Cards */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {isStaff(user?.role) && (
          <CounterCard
            href="/dashboard/machines"
            title={t("dashboard.onlineMachines")}
            icon={Cpu}
          >
            <Counter
              outcome={counters}
              pick={(c) =>
                c.machines && { ...c.machines, serverNow: c.serverNow }
              }
            >
              {(machines) => <MachinesFigure {...machines} />}
            </Counter>
          </CounterCard>
        )}

        <CounterCard
          href="/dashboard/sessions"
          title={t("dashboard.activeSessions")}
          icon={Activity}
        >
          <Counter outcome={counters} pick={(c) => c.sessions}>
            {(open) => (
              <Figure
                value={open.active}
                detail={t("dashboard.pendingSessions", { count: open.pending })}
              />
            )}
          </Counter>
        </CounterCard>

        {isOrganizationAdmin(user?.role) && (
          <CounterCard
            href="/dashboard/users"
            title={t("nav.users")}
            icon={Users}
          >
            <Counter outcome={counters} pick={(c) => c.members}>
              {(members) => (
                <Figure
                  // The accounts, each once: an account holding two roles
                  // counts in both below, so the roles can add up to more.
                  value={members.total}
                  detail={t("dashboard.membersByRole", {
                    admins: members.admin,
                    managers: members.gestionnaire,
                    patients: members.user,
                  })}
                />
              )}
            </Counter>
          </CounterCard>
        )}

        {isStaff(user?.role) && !isOrganizationAdmin(user?.role) && (
          <CounterCard
            href="/dashboard/patients"
            title={t("nav.patients")}
            icon={Users}
          >
            <Counter outcome={counters} pick={(c) => c.patients}>
              {(patients) => (
                <Figure value={patients} detail={t("dashboard.ownPatients")} />
              )}
            </Counter>
          </CounterCard>
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

/** A card of figures, leading to the page that lists what it counts. */
function CounterCard({
  href,
  title,
  icon: Icon,
  children,
}: {
  href: string;
  title: string;
  icon: LucideIcon;
  children: ReactNode;
}) {
  return (
    <Link href={href}>
      <Card className="hover:bg-accent/50 transition-colors cursor-pointer">
        <CardHeader className="flex flex-row items-center justify-between pb-2">
          <CardTitle className="text-sm font-medium">{title}</CardTitle>
          <Icon className="h-4 w-4 text-muted-foreground" />
        </CardHeader>
        <CardContent>{children}</CardContent>
      </Card>
    </Link>
  );
}

/**
 * One figure of the counters as it stands: a placeholder while they load, a
 * dash and "unavailable" when the server failed them (or sent none for this
 * card), the figure otherwise.
 */
function Counter<T>({
  outcome,
  pick,
  children,
}: {
  outcome: QueryOutcome<Counters>;
  pick: (counters: Counters) => T | null;
  children: (value: T) => ReactNode;
}) {
  const t = useTranslations();
  if (outcome.status === "loading") {
    return (
      <div role="status">
        <Skeleton className="h-8 w-12" />
        <Skeleton className="mt-2 h-3 w-24" />
        <span className="sr-only">{t("common.loading")}</span>
      </div>
    );
  }
  const value = outcome.status === "ready" ? pick(outcome.value) : null;
  if (value === null) {
    return <Figure value="-" detail={t("dashboard.counterUnavailable")} />;
  }
  return <>{children(value)}</>;
}

function Figure({ value, detail }: { value: ReactNode; detail: string }) {
  return (
    <>
      <div className="text-2xl font-bold">{value}</div>
      <p className="text-xs text-muted-foreground">{detail}</p>
    </>
  );
}

/**
 * The machines shown connected ("En ligne" or "En session") out of all of
 * them, judged every second on the server's clock like their badges: a silent
 * machine leaves the count 90 s after its last signal, whatever its record
 * still says.
 */
function MachinesFigure({
  total,
  signals,
  serverNow,
}: NonNullable<Counters["machines"]> & { serverNow: number }) {
  const t = useTranslations("dashboard");
  const { connected, inSession } = useConnectedMachines(
    signals.map((signal) => ({ ...signal, serverNow })),
  );
  const all = t("machinesTotal", { total });
  return (
    <Figure
      value={connected}
      detail={
        inSession > 0
          ? `${all}, ${t("machinesInSession", { count: inSession })}`
          : all
      }
    />
  );
}

function StatusBadge({ status }: { status: string }) {
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

  return (
    <Badge variant={variants[status] || "outline"} className="text-xs">
      {statusLabel(status)}
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

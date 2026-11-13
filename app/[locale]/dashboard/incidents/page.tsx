"use client";

import { useLocale, useTranslations } from "next-intl";
import { useQuery } from "convex/react";
import { format } from "date-fns";
import { FileDown, ShieldAlert } from "lucide-react";
import { api } from "@/convex/_generated/api";
import { Link } from "@/i18n/navigation";
import { save } from "@/lib/download";
import { isAnheartAdmin, isStaff } from "@/lib/organizationAccess";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  IncidentSeverityBadge,
  IncidentStatusBadge,
} from "@/components/incidents/IncidentBadges";
import { incidentsCsv } from "@/components/incidents/incidentsCsv";

/** As many incidents as the server lists at most (`INCIDENT_LIST_LIMIT`). */
const LIST_LIMIT = 1000;

/**
 * The incidents of the machines the visitor reaches (ANH-145): every
 * organisation's for the Anheart admin, who also exports them as CSV; their
 * organisation's for its admin; their machines' for a gestionnaire.
 */
export default function IncidentsPage() {
  const t = useTranslations("incidents");
  const locale = useLocale();
  const user = useQuery(api.users.getCurrentUser);
  const staff = isStaff(user?.role);
  const incidents = useQuery(api.incidents.list, staff ? {} : "skip");

  if (user === undefined) return <IncidentsSkeleton />;
  if (!user || !staff) {
    return (
      <div className="text-center py-12">
        <ShieldAlert className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
        <p className="text-muted-foreground">{t("accessDenied")}</p>
      </div>
    );
  }
  if (incidents === undefined) return <IncidentsSkeleton />;

  const at = (time: number) =>
    new Date(time).toLocaleString(locale, {
      dateStyle: "short",
      timeStyle: "short",
    });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
          <p className="text-muted-foreground">{t("description")}</p>
        </div>
        {isAnheartAdmin(user.role) && (
          <div className="space-y-1">
            <Button
              variant="outline"
              onClick={() =>
                save(
                  new Blob([incidentsCsv(incidents)], {
                    type: "text/csv;charset=utf-8",
                  }),
                  `incidents-${format(new Date(), "yyyy-MM-dd")}.csv`,
                )
              }
            >
              <FileDown className="mr-2 h-4 w-4" />
              {t("export")}
            </Button>
            <p className="text-xs text-muted-foreground">{t("exportHint")}</p>
          </div>
        )}
      </div>

      {incidents.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-16">
            <ShieldAlert className="h-12 w-12 text-muted-foreground/50 mb-4" />
            <p className="text-muted-foreground">{t("empty")}</p>
          </CardContent>
        </Card>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t("openedAt")}</TableHead>
              <TableHead>{t("machine")}</TableHead>
              <TableHead>{t("kind")}</TableHead>
              <TableHead>{t("rule")}</TableHead>
              <TableHead>{t("severity")}</TableHead>
              <TableHead>{t("status")}</TableHead>
              <TableHead>{t("occupied")}</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {incidents.map((incident) => (
              <TableRow key={incident._id} data-incident={incident.severity}>
                <TableCell>{at(incident.openedAt)}</TableCell>
                <TableCell>{incident.machineName}</TableCell>
                <TableCell>{t(`kinds.${incident.kind}`)}</TableCell>
                <TableCell className="font-mono text-xs">{incident.rule}</TableCell>
                <TableCell>
                  <IncidentSeverityBadge severity={incident.severity} />
                </TableCell>
                <TableCell>
                  <IncidentStatusBadge status={incident.status} />
                </TableCell>
                <TableCell>{t(incident.occupied ? "yes" : "no")}</TableCell>
                <TableCell>
                  <Button variant="ghost" size="sm" asChild>
                    <Link href={`/dashboard/incidents/${incident._id}`}>
                      {t("open")}
                    </Link>
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {incidents.length >= LIST_LIMIT && (
        <p className="text-sm text-muted-foreground">
          {t("listCut", { count: LIST_LIMIT })}
        </p>
      )}
    </div>
  );
}

function IncidentsSkeleton() {
  return (
    <div className="space-y-6" aria-busy="true">
      <div>
        <Skeleton className="h-8 w-48 mb-2" />
        <Skeleton className="h-4 w-72" />
      </div>
      <Skeleton className="h-40 w-full" />
    </div>
  );
}

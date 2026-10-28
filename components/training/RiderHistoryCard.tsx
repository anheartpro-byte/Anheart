"use client";

import { useQuery } from "convex/react";
import { useLocale, useTranslations } from "next-intl";
import { format } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import { TrendingUp } from "lucide-react";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { Link } from "@/i18n/navigation";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useSessionStatusLabel } from "@/components/dashboard/statusLabels";
import {
  formatDuration,
  formatNumber,
  percentOf,
  progressionOf,
} from "@/lib/sessionReport";
import { RiderProgressChart } from "./RiderProgressChart";

/**
 * The history and progression of a rider on their page (ANH-89): their
 * finished sessions the visitor may read, newest first, with the key figures
 * of each summary, the nausea score of the rider's feedback (ANH-146), and
 * the time in zone session after session. The server lists them under the
 * rights of each session (`sessionReports.riderHistory`).
 */
export function RiderHistoryCard({ userId }: { userId: Id<"users"> }) {
  const t = useTranslations("sessionReport");
  const status = useSessionStatusLabel();
  const locale = useLocale();
  const dateLocale = locale === "fr" ? fr : enUS;
  const history = useQuery(api.sessionReports.riderHistory, { userId });
  const progression = history === undefined ? [] : progressionOf(history);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <TrendingUp className="h-4 w-4" />
          {t("historyTitle")}
        </CardTitle>
        <CardDescription>{t("historyDescription")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {history === undefined ? (
          <Skeleton className="h-48 w-full" />
        ) : history.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">
            {t("historyEmpty")}
          </p>
        ) : (
          <>
            <div>
              <p className="mb-2 text-sm font-medium">
                {t("progressionTitle")} ({t("progressionUnit")})
              </p>
              {progression.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  {t("progressionEmpty")}
                </p>
              ) : (
                <RiderProgressChart points={progression} />
              )}
            </div>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("date")}</TableHead>
                  <TableHead>{t("program")}</TableHead>
                  <TableHead>{t("status")}</TableHead>
                  <TableHead>{t("duration")}</TableHead>
                  <TableHead>{t("timeInZone")}</TableHead>
                  <TableHead>{t("meanHr")}</TableHead>
                  <TableHead>{t("nausea")}</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {history.map((row) => {
                  const summary = row.summary;
                  const share = percentOf(summary?.inZoneS, summary?.durationS);
                  return (
                    <TableRow key={row.sessionId}>
                      <TableCell>
                        {format(row.startedAt, "PPp", { locale: dateLocale })}
                      </TableCell>
                      <TableCell>
                        {row.profileName ?? "-"}
                        <span className="block text-xs text-muted-foreground">
                          {row.machineName}
                        </span>
                      </TableCell>
                      <TableCell>{status(row.status)}</TableCell>
                      {summary === null ? (
                        <TableCell
                          colSpan={3}
                          className="text-muted-foreground"
                        >
                          {t(row.notStarted ? "notStarted" : "notComputed")}
                        </TableCell>
                      ) : (
                        <>
                          <TableCell>
                            {formatDuration(summary.durationS)}
                            {summary.provisional && (
                              <Badge variant="outline" className="ml-2">
                                {t("provisional")}
                              </Badge>
                            )}
                          </TableCell>
                          <TableCell>
                            {summary.inZoneS === null
                              ? "-"
                              : share === null
                                ? formatDuration(summary.inZoneS)
                                : t("inZoneShare", {
                                    duration: formatDuration(summary.inZoneS),
                                    percent: share,
                                  })}
                          </TableCell>
                          <TableCell>
                            {summary.meanBpm === null
                              ? "-"
                              : `${formatNumber(summary.meanBpm, locale, 1)} bpm`}
                          </TableCell>
                        </>
                      )}
                      <TableCell>{row.nauseaScore ?? "-"}</TableCell>
                      <TableCell>
                        <Link
                          href={`/dashboard/sessions/${row.sessionId}/report`}
                        >
                          <Button variant="outline" size="sm">
                            {t("report")}
                          </Button>
                        </Link>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </>
        )}
      </CardContent>
    </Card>
  );
}

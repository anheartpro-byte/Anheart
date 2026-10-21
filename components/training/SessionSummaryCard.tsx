"use client";

import { useQuery } from "convex/react";
import { useTranslations } from "next-intl";
import { ClipboardList, FileDown } from "lucide-react";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { Link } from "@/i18n/navigation";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { SessionSummaryFigures } from "./SessionSummaryFigures";

/**
 * The summary of a finished session on its page (ANH-89), with the way to its
 * report. Provisional while it comes from the telemetry, then the summary of
 * the deposited record; the server serves it to whoever may read the session.
 */
export function SessionSummaryCard({
  sessionId,
}: {
  sessionId: Id<"sessions">;
}) {
  const t = useTranslations("sessionReport");
  const summary = useQuery(api.sessionReports.getSummary, { sessionId });

  if (summary === undefined) return <Skeleton className="h-48 w-full" />;
  const report = summary !== null && !("absent" in summary) ? summary : null;
  const notStarted =
    summary !== null && "absent" in summary && summary.absent === "not_started";

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <ClipboardList className="h-4 w-4" />
          {t("summaryTitle")}
          {report !== null && (
            <Badge variant={report.provisional ? "outline" : "secondary"}>
              {report.provisional ? t("provisional") : t("final")}
            </Badge>
          )}
        </CardTitle>
        <CardDescription>
          {report === null
            ? t(notStarted ? "notStartedNote" : "none")
            : report.provisional
              ? t("provisionalNote")
              : t("finalNote")}
        </CardDescription>
        <CardAction>
          <Link href={`/dashboard/sessions/${sessionId}/report`}>
            <Button variant="outline" size="sm">
              <FileDown className="mr-2 h-4 w-4" />
              {t("openReport")}
            </Button>
          </Link>
        </CardAction>
      </CardHeader>
      {report !== null && (
        <CardContent className="space-y-3">
          {report.recordProblem !== null && (
            <p className="text-sm text-amber-700 dark:text-amber-400">
              {t("recordProblem", { problem: report.recordProblem })}
            </p>
          )}
          <SessionSummaryFigures figures={report.figures} />
        </CardContent>
      )}
    </Card>
  );
}

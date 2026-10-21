"use client";

import { use, type ReactNode } from "react";
import { useQuery } from "convex/react";
import { useLocale, useTranslations } from "next-intl";
import { format } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import { ArrowLeft, Printer } from "lucide-react";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { Link, useRouter } from "@/i18n/navigation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useCatalogLabel,
  useSessionStatusLabel,
} from "@/components/dashboard/statusLabels";
import { SessionSummaryFigures } from "@/components/training/SessionSummaryFigures";
import { TelemetryCharts } from "@/components/training/TelemetryCharts";

/** The languages a report is written in. */
const LANGUAGES = [
  { code: "fr", label: "Français" },
  { code: "en", label: "English" },
] as const;

/**
 * The report of a training session (ANH-89), laid out for printing: the
 * button opens the browser's print dialog, where "Save as PDF" writes the
 * file. Nothing on it comes from anywhere but the queries the session page
 * reads, under the same rights (`canAccessSession`); the dashboard's
 * navigation and this page's buttons are not printed (`print:hidden`, and
 * app/globals.css for the sidebar). The language is the page's: the buttons
 * open the same report in the other one.
 */
export default function SessionReportPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const sessionId = id as Id<"sessions">;
  const t = useTranslations("sessionReport");
  const kind = useCatalogLabel("training.kind");
  const status = useSessionStatusLabel();
  const locale = useLocale();
  const router = useRouter();
  const session = useQuery(api.sessions.getSession, { sessionId });
  const training = useQuery(api.training.getTrainingSession, { sessionId });
  const summary = useQuery(api.sessionReports.getSummary, { sessionId });
  // The curves of the session page, under the same rights.
  const telemetry = useQuery(api.training.getSessionTelemetry, {
    sessionId,
    limit: 7200,
  });

  if (session === undefined || training === undefined || summary === undefined)
    return <Skeleton className="mx-auto h-[600px] max-w-4xl" />;

  if (session === null || training === null)
    return (
      <div className="py-12 text-center">
        <p className="text-muted-foreground">{t("notFound")}</p>
        <Link
          href="/dashboard/sessions"
          className="mt-2 inline-block text-primary hover:underline"
        >
          {t("back")}
        </Link>
      </div>
    );

  const finished =
    session.status === "completed" || session.status === "failed";
  const dateLocale = locale === "fr" ? fr : enUS;
  const rider = session.patient
    ? `${session.patient.firstName} ${session.patient.lastName}`
    : (session.subjectLabel ?? "-");
  const zone =
    training.zoneLowBpm !== undefined && training.zoneHighBpm !== undefined
      ? t("zoneBounds", {
          low: training.zoneLowBpm,
          high: training.zoneHighBpm,
        })
      : "-";

  return (
    <div className="mx-auto max-w-4xl space-y-4 print:max-w-none">
      <div className="flex flex-wrap items-center gap-2 print:hidden">
        <Link href={`/dashboard/sessions/${sessionId}`}>
          <Button variant="ghost" size="sm">
            <ArrowLeft className="mr-2 h-4 w-4" />
            {t("back")}
          </Button>
        </Link>
        <div className="flex-1" />
        <span className="text-sm text-muted-foreground">{t("language")}</span>
        {LANGUAGES.map(({ code, label }) => (
          <Button
            key={code}
            size="sm"
            variant={code === locale ? "secondary" : "ghost"}
            aria-pressed={code === locale}
            lang={code}
            onClick={() =>
              router.replace(`/dashboard/sessions/${sessionId}/report`, {
                locale: code,
              })
            }
          >
            {label}
          </Button>
        ))}
        <Button size="sm" disabled={!finished} onClick={() => window.print()}>
          <Printer className="mr-2 h-4 w-4" />
          {t("download")}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground print:hidden">
        {t("downloadHint")}
      </p>

      <article className="space-y-6 rounded-lg border bg-card p-6 text-card-foreground print:border-0 print:p-0">
        <header className="space-y-3">
          <h1 className="text-2xl font-bold">{t("reportTitle")}</h1>
          <dl className="grid grid-cols-2 gap-3 text-sm md:grid-cols-3 print:grid-cols-3">
            <Item label={t("rider")}>{rider}</Item>
            <Item label={t("machine")}>{session.machine.name}</Item>
            <Item label={t("date")}>
              {format(session.startedAt, "PPPp", { locale: dateLocale })}
            </Item>
            <Item label={t("kind")}>{kind(training.kind)}</Item>
            <Item label={t("program")}>
              {training.profileName ?? training.profileId ?? "-"}
            </Item>
            <Item label={t("zone")}>{zone}</Item>
            <Item label={t("status")}>{status(session.status)}</Item>
          </dl>
        </header>

        <section className="space-y-3 border-t pt-4">
          <h2 className="flex flex-wrap items-center gap-2 text-lg font-semibold">
            {t("summaryTitle")}
            {finished && summary !== null && !("absent" in summary) && (
              <Badge variant={summary.provisional ? "outline" : "secondary"}>
                {summary.provisional ? t("provisional") : t("final")}
              </Badge>
            )}
          </h2>
          {!finished ? (
            <p className="text-sm text-muted-foreground">{t("notFinished")}</p>
          ) : summary === null || "absent" in summary ? (
            <p className="text-sm text-muted-foreground">
              {t(summary?.absent === "not_started" ? "notStartedNote" : "none")}
            </p>
          ) : (
            <>
              <p className="text-sm text-muted-foreground">
                {summary.provisional ? t("provisionalNote") : t("finalNote")}
              </p>
              {summary.recordProblem !== null && (
                <p className="text-sm text-amber-700 dark:text-amber-400 print:text-black">
                  {t("recordProblem", { problem: summary.recordProblem })}
                </p>
              )}
              <SessionSummaryFigures figures={summary.figures} />
              <p className="text-xs text-muted-foreground">
                {t("computedAt", {
                  date: format(summary.computedAt, "PPPp", {
                    locale: dateLocale,
                  }),
                })}
              </p>
            </>
          )}
        </section>

        {telemetry !== undefined && telemetry.length > 0 && (
          <section className="space-y-3 border-t pt-4 print:break-inside-avoid">
            <h2 className="text-lg font-semibold">{t("telemetry")}</h2>
            {/* A fixed width that fits the printed page: the curves are
                drawn at the width measured on screen, not redrawn for A4. */}
            <div className="mx-auto w-full max-w-[640px]">
              <TelemetryCharts
                points={telemetry}
                zoneLowBpm={training.zoneLowBpm}
                zoneHighBpm={training.zoneHighBpm}
                height={180}
              />
            </div>
          </section>
        )}

        <section className="space-y-1 border-t pt-4 text-sm">
          <h2 className="font-semibold">{t("notAvailableTitle")}</h2>
          <p className="text-muted-foreground">{t("preflight")}</p>
          <p className="text-muted-foreground">{t("feedback")}</p>
        </section>
      </article>
    </div>
  );
}

function Item({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-medium">{children}</dd>
    </div>
  );
}

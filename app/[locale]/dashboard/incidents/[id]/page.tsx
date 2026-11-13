"use client";

import { use, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { useQuery } from "convex/react";
import type { FunctionReturnType } from "convex/server";
import { AlertTriangle, ArrowLeft } from "lucide-react";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { Link } from "@/i18n/navigation";
import { useMutationWithFeedback } from "@/hooks/use-mutation-with-feedback";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import {
  IncidentSeverityBadge,
  IncidentStatusBadge,
} from "@/components/incidents/IncidentBadges";
import { SessionRecordCard } from "@/components/training/SessionRecordCard";

type Sheet = NonNullable<FunctionReturnType<typeof api.incidents.get>>;

/**
 * One incident (ANH-145): what opened it, its machine and its session, the
 * session's events within 60 s of its instant, the session's record, and the
 * form of its analysis and, for the Anheart admin, of its closing.
 */
export default function IncidentPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const t = useTranslations("incidents");
  const sheet = useQuery(api.incidents.get, {
    incidentId: id as Id<"incidents">,
  });

  if (sheet === undefined) return <Skeleton className="h-64 w-full" />;
  if (sheet === null) {
    return (
      <div className="text-center py-12">
        <p className="text-muted-foreground">{t("notFound")}</p>
        <Link
          href="/dashboard/incidents"
          className="text-primary hover:underline mt-2 inline-block"
        >
          {t("back")}
        </Link>
      </div>
    );
  }
  return <IncidentSheet sheet={sheet} />;
}

function IncidentSheet({ sheet }: { sheet: Sheet }) {
  const t = useTranslations("incidents");
  const locale = useLocale();
  const { incident, session } = sheet;
  const at = (time: number) =>
    new Date(time).toLocaleString(locale, {
      dateStyle: "short",
      timeStyle: "medium",
    });
  const blocking = incident.severity === "high" && incident.status !== "closed";

  return (
    <div className="space-y-6">
      <Button variant="ghost" size="sm" asChild>
        <Link href="/dashboard/incidents">
          <ArrowLeft className="mr-2 h-4 w-4" />
          {t("back")}
        </Link>
      </Button>
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t(`kinds.${incident.kind}`)}
        </h1>
        <IncidentSeverityBadge severity={incident.severity} />
        <IncidentStatusBadge status={incident.status} />
      </div>
      {blocking && (
        <p
          role="status"
          className="flex items-center gap-2 rounded-lg border border-destructive p-3 text-sm"
        >
          <AlertTriangle className="h-4 w-4 text-destructive" />
          {t("blocked")}
        </p>
      )}

      <Card>
        <CardHeader>
          <CardTitle>{t("details")}</CardTitle>
          <CardDescription>
            {incident.machineName} · {t("openedAt")} {at(incident.openedAt)}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-2 text-sm sm:grid-cols-2">
          <p>
            <span className="text-muted-foreground">{t("rule")}</span>{" "}
            <span className="font-mono">{incident.rule}</span>
          </p>
          <p>
            <span className="text-muted-foreground">{t("occupied")}</span>{" "}
            {t(incident.occupied ? "yes" : "no")}
          </p>
          {sheet.analyzedByName && (
            <p>{t("analyzedBy", { name: sheet.analyzedByName })}</p>
          )}
          {sheet.closedByName && incident.closedAt !== undefined && (
            <p>
              {t("closedBy", {
                name: sheet.closedByName,
                date: at(incident.closedAt),
              })}
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("session")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4 text-sm">
          {session === null ? (
            <p className="text-muted-foreground">{t("noSession")}</p>
          ) : (
            <Button variant="outline" size="sm" asChild>
              <Link href={`/dashboard/sessions/${session._id}`}>
                {t("openSession")}
              </Link>
            </Button>
          )}
          <h2 className="font-medium">{t("events")}</h2>
          {sheet.events.length === 0 ? (
            <p className="text-muted-foreground">{t("noEvents")}</p>
          ) : (
            <ol className="space-y-1 font-mono text-xs" aria-label={t("events")}>
              {sheet.events.map((event) => (
                <li key={`${event.t}-${event.kind}-${event.detail}`}>
                  {t("offset", {
                    seconds: ((event.t - incident.t) / 1000).toFixed(1),
                  })}{" "}
                  · {event.kind} · {event.detail}
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>

      {session !== null && <SessionRecordCard sessionId={session._id} />}

      <ReviewForm sheet={sheet} />
    </div>
  );
}

/** The cause and the action: saved by the staff, closed by the Anheart admin. */
function ReviewForm({ sheet }: { sheet: Sheet }) {
  const t = useTranslations("incidents");
  const { incident } = sheet;
  const [analysis, setAnalysis] = useState(incident.analysis ?? "");
  const [action, setAction] = useState(incident.action ?? "");
  const analyze = useMutationWithFeedback(api.incidents.analyze);
  const close = useMutationWithFeedback(api.incidents.close);
  const closed = incident.status === "closed";
  const review = { incidentId: incident._id, analysis, action };

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("review")}</CardTitle>
        {!sheet.canClose && !closed && (
          <CardDescription>{t("closeAdminOnly")}</CardDescription>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="incident-analysis">{t("analysis")}</Label>
          <Textarea
            id="incident-analysis"
            value={analysis}
            disabled={closed}
            maxLength={2000}
            onChange={(event) => setAnalysis(event.target.value)}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="incident-action">{t("action")}</Label>
          <Textarea
            id="incident-action"
            value={action}
            disabled={closed}
            maxLength={2000}
            onChange={(event) => setAction(event.target.value)}
          />
        </div>
        {!closed && (
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              onClick={() => analyze(review, { success: t("analyzed") })}
            >
              {t("saveAnalysis")}
            </Button>
            {sheet.canClose && (
              <Button onClick={() => close(review, { success: t("closed") })}>
                {t("close")}
              </Button>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

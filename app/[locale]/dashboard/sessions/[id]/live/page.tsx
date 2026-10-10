"use client";

import { use } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations, useLocale } from "next-intl";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { AlertCircle } from "lucide-react";
import { formatDistanceToNow } from "date-fns";
import { fr, enUS } from "date-fns/locale";
import { isTrainingKind } from "@/lib/training";
import { TrainingPanel } from "@/components/training/TrainingPanel";
import {
  SessionKindBadge,
  SessionOriginBadge,
} from "@/components/training/TrainingBadges";

/**
 * Live view of a training session (auto or manual): the training panel, fed by
 * the telemetry the machine reports. A session of the former ECG recording
 * mode has no live view: it is read-only history, shown on its detail page.
 */

export default function LiveSessionPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const t = useTranslations();
  const locale = useLocale();

  const sessionId = id as Id<"sessions">;
  const session = useQuery(api.sessions.getSession, { sessionId });
  const training = useQuery(api.training.getTrainingSession, { sessionId });

  const dateLocale = locale === "fr" ? fr : enUS;

  if (session === undefined) {
    return <LiveSessionSkeleton />;
  }

  if (session === null) {
    return (
      <div className="text-center py-12">
        <p className="text-muted-foreground">{t("sessions.notFound")}</p>
        <Link
          href="/dashboard/sessions"
          className="text-primary hover:underline mt-2 inline-block"
        >
          {t("common.back")}
        </Link>
      </div>
    );
  }

  if (!isTrainingKind(session.kind)) {
    return (
      <div className="text-center py-12 space-y-4">
        <AlertCircle className="h-12 w-12 text-muted-foreground mx-auto" />
        <p className="text-lg">{t("sessionLive.legacyRecording")}</p>
        <Link href={`/dashboard/sessions/${sessionId}`}>
          <Button>{t("sessionLive.viewDetails")}</Button>
        </Link>
      </div>
    );
  }

  const riderName = session.patient
    ? `${session.patient.firstName} ${session.patient.lastName}`
    : (session.subjectLabel ?? t("training.session.riderNotSpecified"));
  const isActive = session.status === "active";
  // Pending: waiting for the machine to arm. Otherwise the session is over.
  const isOver = !isActive && session.status !== "pending";

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">{riderName}</h1>
          <p className="text-muted-foreground flex flex-wrap items-center gap-2">
            <span>
              {session.machine.name}
              {isActive && (
                <>
                  {" "}
                  •{" "}
                  {t("sessionLive.started", {
                    time: formatDistanceToNow(session.startedAt, {
                      addSuffix: true,
                      locale: dateLocale,
                    }),
                  })}
                </>
              )}
            </span>
            <SessionKindBadge kind={session.kind} />
            <SessionOriginBadge origin={training?.origin} />
          </p>
        </div>
        {isOver && (
          <Link href={`/dashboard/sessions/${sessionId}`}>
            <Button variant="outline">
              {t("training.session.viewDetails")}
            </Button>
          </Link>
        )}
      </div>
      <TrainingPanel sessionId={sessionId} />
    </div>
  );
}

function LiveSessionSkeleton() {
  return (
    <div className="space-y-6">
      <div>
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-4 w-64 mt-2" />
      </div>
      <Skeleton className="h-[400px]" />
    </div>
  );
}

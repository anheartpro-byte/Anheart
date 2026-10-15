"use client";

import { useMessages, useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import { Gauge, Hand, Activity, Monitor, Cpu } from "lucide-react";
import { hasOwnMessage } from "@/components/dashboard/statusLabels";

/** Translate an enumerated wire value (phase, run mode, safety action, drive state), falling back to the raw value. */
export function useTrainingLabel() {
  const messages = useMessages();
  const t = useTranslations("training");
  return (
    group: "phase" | "runMode" | "safety" | "driveState",
    value: string,
  ) =>
    // Own keys only: "constructor" or "toString" must come back as received.
    hasOwnMessage(messages, ["training", group, value])
      ? t(`${group}.${value}`)
      : value;
}

/**
 * Why a session ended, for a person: the sentence of its stable end code (a
 * launch refused before its machine armed it, ANH-144), translated like any
 * code of the server's table (`errors.<code>`, convex/lib/errors.ts), else
 * the reason as it was written (the machine's words or the server's), else
 * nothing.
 */
export function useEndReason() {
  const messages = useMessages();
  const t = useTranslations("errors");
  return (training: { endCode?: string; endReason?: string }) =>
    training.endCode !== undefined &&
    hasOwnMessage(messages, ["errors", training.endCode])
      ? t(training.endCode)
      : training.endReason;
}

export function SessionKindBadge({ kind }: { kind: string | undefined }) {
  const t = useTranslations("training.kind");
  const k = kind ?? "recording";
  if (k === "auto") {
    return (
      <Badge variant="default" className="gap-1">
        <Gauge className="h-3 w-3" />
        {t("auto")}
      </Badge>
    );
  }
  if (k === "manual") {
    return (
      <Badge
        variant="outline"
        className="gap-1 border-amber-500/60 text-amber-700 dark:text-amber-400"
      >
        <Hand className="h-3 w-3" />
        {t("manual")}
      </Badge>
    );
  }
  return (
    <Badge variant="secondary" className="gap-1">
      <Activity className="h-3 w-3" />
      {t("recording")}
    </Badge>
  );
}

export function SessionOriginBadge({ origin }: { origin: string | undefined }) {
  const t = useTranslations("training.origin");
  if (origin !== "remote" && origin !== "local") return null;
  return (
    <Badge variant="outline" className="gap-1 text-muted-foreground">
      {origin === "remote" ? (
        <Monitor className="h-3 w-3" />
      ) : (
        <Cpu className="h-3 w-3" />
      )}
      {t(origin)}
    </Badge>
  );
}

export function LiveFreshBadge({ stale }: { stale: boolean }) {
  const t = useTranslations("training.live");
  return stale ? (
    <Badge variant="outline" className="gap-1 text-muted-foreground">
      <span className="h-2 w-2 rounded-full bg-muted-foreground/60" />
      {t("stale")}
    </Badge>
  ) : (
    <Badge variant="default" className="gap-1">
      <span className="h-2 w-2 rounded-full bg-green-400 animate-pulse" />
      {t("fresh")}
    </Badge>
  );
}

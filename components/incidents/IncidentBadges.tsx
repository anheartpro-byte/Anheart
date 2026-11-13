"use client";

import { useTranslations } from "next-intl";
import type { Doc } from "@/convex/_generated/dataModel";
import { Badge } from "@/components/ui/badge";

const SEVERITY_VARIANT = {
  high: "destructive",
  medium: "default",
  low: "secondary",
} as const;

/** An incident's severity (ANH-145): red when it is high. */
export function IncidentSeverityBadge({
  severity,
}: {
  severity: Doc<"incidents">["severity"];
}) {
  const t = useTranslations("incidents.severities");
  return <Badge variant={SEVERITY_VARIANT[severity]}>{t(severity)}</Badge>;
}

/** An incident's status: open, analysed or closed. */
export function IncidentStatusBadge({
  status,
}: {
  status: Doc<"incidents">["status"];
}) {
  const t = useTranslations("incidents.statuses");
  return (
    <Badge variant={status === "closed" ? "outline" : "secondary"}>
      {t(status)}
    </Badge>
  );
}

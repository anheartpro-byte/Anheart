"use client";

import { useTranslations } from "next-intl";

/** Translate the raw machine status, falling back to the raw value. */
export function useMachineStatusLabel() {
  const t = useTranslations("machines");
  return (status: string) => {
    const labels: Record<string, string> = {
      online: t("online"),
      offline: t("offline"),
      in_session: t("inSession"),
    };
    return labels[status] || status;
  };
}

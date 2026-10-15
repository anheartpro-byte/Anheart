"use client";

import type { FunctionReturnType } from "convex/server";
import { useTranslations } from "next-intl";
import type { api } from "@/convex/_generated/api";
import { Badge } from "@/components/ui/badge";

export function MachineRegistryBadges({
  badge,
}: {
  badge: FunctionReturnType<
    typeof api.machineRegistry.listRegistryBadges
  >[number];
}) {
  const t = useTranslations("machineRegistry");
  return (
    <div className="flex flex-wrap gap-1">
      <Badge variant="outline">
        {t("badges.validation", { level: t(`levels.${badge.level}`) })}
      </Badge>
      <Badge
        variant={
          badge.versionStatus === "not_validated" ? "destructive" : "secondary"
        }
      >
        {t("badges.version", { status: t(`versions.${badge.versionStatus}`) })}
      </Badge>
      <Badge variant="outline">
        {t(`badges.configuration.${badge.configurationStatus}`)}
      </Badge>
    </div>
  );
}

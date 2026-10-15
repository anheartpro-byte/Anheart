"use client";

import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations } from "next-intl";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Heart, Timer, Gauge, ListChecks, Ban, Hand } from "lucide-react";
import { armRpm, formatMinutes, type TrainingProfile } from "@/lib/training";

/** Synced presets, read-only. Speeds are shown as arm rpm, motor rpm underneath. */
export function ProfileList({
  profiles,
  programsEnabled,
  medicalParametersStatus,
}: {
  profiles: TrainingProfile[];
  programsEnabled: boolean;
  medicalParametersStatus?:
    | "unknown"
    | "unsigned"
    | "no_reference"
    | "current"
    | "obsolete"
    | "ahead";
}) {
  const t = useTranslations("training");
  const medicalIssue =
    medicalParametersStatus === "unsigned"
      ? t("launch.medicalParametersUnsigned")
      : medicalParametersStatus === "no_reference"
        ? t("launch.medicalParametersUnavailable")
        : null;
  const programsUnavailable = !programsEnabled || medicalIssue !== null;
  const unavailableMessage = medicalIssue ?? t("programs.disabled");

  return (
    <div className="space-y-3">
      {programsUnavailable && (
        <div className="flex items-start gap-2 rounded-md border border-amber-500/50 bg-amber-50 dark:bg-amber-950/40 p-3 text-sm text-amber-800 dark:text-amber-200">
          <Ban className="h-4 w-4 mt-0.5 shrink-0" />
          <span>{unavailableMessage}</span>
        </div>
      )}
      {profiles.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("programs.none")}</p>
      ) : (
        <div className="divide-y rounded-md border">
          {profiles.map((p) => (
            <div
              key={p.profileId}
              className={`p-3 ${programsUnavailable ? "opacity-60" : ""}`}
            >
              <p className="font-medium">{p.name}</p>
              <div className="mt-1 grid grid-cols-1 sm:grid-cols-3 gap-2 text-sm text-muted-foreground">
                <span className="flex items-center gap-1">
                  <Heart className="h-3.5 w-3.5 text-red-500" />
                  {p.zoneLowBpm}-{p.zoneHighBpm} {t("units.bpm")}
                  <span className="text-xs">
                    ({t("programs.hardMax")} {p.hardMaxBpm})
                  </span>
                </span>
                <span className="flex items-center gap-1">
                  <Timer className="h-3.5 w-3.5" />
                  {formatMinutes(p.totalDurationS)}
                </span>
                <span className="flex items-center gap-1">
                  <Gauge className="h-3.5 w-3.5" />
                  <span>
                    {t("programs.armRpm", { rpm: armRpm(p.maxRpm).toFixed(1) })}
                    <span className="block text-xs">
                      {t("programs.motorRpm", { rpm: Math.round(p.maxRpm) })}
                    </span>
                  </span>
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
      <p className="flex items-center gap-2 text-xs text-muted-foreground">
        <Hand className="h-3.5 w-3.5" />
        {t("manualOnlyAtMachine")}
      </p>
    </div>
  );
}

/** "Programmes" card for the machine detail page. */
export function MachineProgramsCard({
  machineId,
  action,
}: {
  machineId: Id<"machines">;
  action?: React.ReactNode;
}) {
  const t = useTranslations("training.programs");
  const profiles = useQuery(api.training.listMachineProfiles, { machineId });
  const live = useQuery(api.training.getMachineLive, { machineId });
  const medicalParametersStatus =
    live?.medicalParametersReferenceVersion === null
      ? "no_reference"
      : live?.medicalParametersStatus;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2">
              <ListChecks className="h-4 w-4" />
              {t("title")}
            </CardTitle>
            <CardDescription>{t("description")}</CardDescription>
          </div>
          {action}
        </div>
      </CardHeader>
      <CardContent>
        {profiles === undefined || live === undefined ? (
          <Skeleton className="h-24 w-full" />
        ) : (
          <ProfileList
            profiles={profiles}
            programsEnabled={live?.programsEnabled ?? false}
            medicalParametersStatus={medicalParametersStatus}
          />
        )}
      </CardContent>
    </Card>
  );
}

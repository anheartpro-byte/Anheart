"use client";

import type { FunctionReturnType } from "convex/server";
import { api } from "@/convex/_generated/api";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Cpu, MapPin, Play, Eye } from "lucide-react";
import { useFreshnessJudge } from "@/hooks/use-freshness";
import { shownMachineStatus } from "@/lib/training";
import { ShownStatusBadge } from "@/components/machines/MachineSignal";
import { LiveReadouts } from "./MachineLiveCard";
import { ProfileList } from "./ProfileList";
import { LiveFreshBadge } from "./TrainingBadges";

type LaunchableMachine = FunctionReturnType<
  typeof api.training.listLaunchableMachines
>[number];

/**
 * One machine of "My machines": its status, its live state, its programmes,
 * the launch button. A component of its own so that each machine has its own
 * clock: the server writes "offline" and drops `live` only when its job runs,
 * the hook says so 90 s after the last signal.
 */
export function LaunchableMachineCard({
  machine: m,
  isUser,
  onLaunch,
}: {
  machine: LaunchableMachine;
  isUser: boolean;
  onLaunch: () => void;
}) {
  const t = useTranslations();
  const judge = useFreshnessJudge();
  // One clock reading for the status, its badge, the launch button and the
  // values: they cannot disagree.
  const signalFresh = judge(m.lastHeartbeat, m.serverNow).fresh;
  const status = shownMachineStatus(m.status, signalFresh);
  const stale = !signalFresh || !judge(m.live?.updatedAt, m.serverNow).fresh;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2">
              <Cpu className="h-4 w-4" />
              {m.name}
            </CardTitle>
            <CardDescription className="flex items-center gap-1">
              {m.location && (
                <>
                  <MapPin className="h-3 w-3" />
                  {m.location}
                </>
              )}
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <ShownStatusBadge status={status} />
            {m.live && <LiveFreshBadge stale={stale} />}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {m.live ? (
          <LiveReadouts live={m.live} stale={stale} />
        ) : (
          <p className="text-sm text-muted-foreground">
            {/* "Never reported" would be false of a machine that went silent:
                the server withholds a state once it is stale. */}
            {status === "offline" && m.lastHeartbeat > 0
              ? t("training.live.offlineNoData")
              : t("training.live.noData")}
          </p>
        )}
        <Separator />
        <div>
          <p className="text-sm font-medium mb-2">
            {t("training.programs.title")}{" "}
            <span className="text-muted-foreground font-normal">
              ·{" "}
              {t("training.myMachines.programsCount", {
                count: m.profiles.length,
              })}
            </span>
          </p>
          <ProfileList
            profiles={m.profiles}
            programsEnabled={m.programsEnabled}
          />
        </div>
        <div className="flex gap-2 pt-2">
          <Button
            className="flex-1"
            onClick={onLaunch}
            disabled={
              status !== "online" ||
              !m.programsEnabled ||
              m.profiles.length === 0
            }
          >
            <Play className="h-4 w-4 mr-2" />
            {t("training.launch.button")}
          </Button>
          {!isUser && (
            <Link href={`/dashboard/machines/${m._id}`}>
              <Button variant="outline">
                <Eye className="h-4 w-4 mr-2" />
                {t("training.myMachines.details")}
              </Button>
            </Link>
          )}
        </div>
        {status === "in_session" && (
          <p className="text-xs text-muted-foreground">
            {t("training.launch.inSession")}
          </p>
        )}
        {status === "offline" && (
          <p className="text-xs text-muted-foreground">
            {t("training.launch.offline")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

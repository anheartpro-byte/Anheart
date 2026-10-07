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
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Cpu, MapPin, Play, Eye } from "lucide-react";
import { useFreshness } from "@/hooks/use-freshness";
import { LiveReadouts } from "./MachineLiveCard";
import { ProfileList } from "./ProfileList";
import { LiveFreshBadge } from "./TrainingBadges";

type LaunchableMachine = FunctionReturnType<
  typeof api.training.listLaunchableMachines
>[number];

/**
 * One machine of "My machines": its live state, its programmes, the launch
 * button. A component of its own so that each machine has its own clock: the
 * query drops `live` only when it runs again, the hook marks it stale on time.
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
  const { fresh } = useFreshness(m.live?.updatedAt);
  const stale = !fresh;

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
            <MachineStatusBadge status={m.status} />
            {m.live && <LiveFreshBadge stale={stale} />}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {m.live ? (
          <LiveReadouts live={m.live} stale={stale} />
        ) : (
          <p className="text-sm text-muted-foreground">
            {t("training.live.noData")}
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
              m.status !== "online" ||
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
        {m.status === "in_session" && (
          <p className="text-xs text-muted-foreground">
            {t("training.launch.inSession")}
          </p>
        )}
        {m.status === "offline" && (
          <p className="text-xs text-muted-foreground">
            {t("training.launch.offline")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function MachineStatusBadge({ status }: { status: string }) {
  const t = useTranslations("machines");
  const variants: Record<
    string,
    "default" | "secondary" | "destructive" | "outline"
  > = {
    online: "default",
    offline: "destructive",
    in_session: "secondary",
  };
  const labels: Record<string, string> = {
    online: t("online"),
    offline: t("offline"),
    in_session: t("inSession"),
  };
  return (
    <Badge variant={variants[status] || "outline"}>
      {labels[status] || status}
    </Badge>
  );
}

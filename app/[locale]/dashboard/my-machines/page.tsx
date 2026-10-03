"use client";

import { useState } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
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
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import { Cpu, HeartPulse, KeyRound, MapPin, Play, Eye } from "lucide-react";
import { LaunchTrainingModal } from "@/components/modals/LaunchTrainingModal";
import { LiveReadouts } from "@/components/training/MachineLiveCard";
import { ProfileList } from "@/components/training/ProfileList";
import { LiveFreshBadge } from "@/components/training/TrainingBadges";

export default function MyMachinesPage() {
  const t = useTranslations();
  const machines = useQuery(api.training.listLaunchableMachines, {});
  const user = useQuery(api.users.getCurrentUser);
  const [launchFor, setLaunchFor] = useState<Id<"machines"> | null>(null);

  if (machines === undefined || user === undefined) {
    return <MyMachinesSkeleton />;
  }

  const isUser = user?.role === "user";
  const myHrMax = machines[0]?.myHrMax ?? null;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap justify-between items-start gap-4">
        <div>
          <h1 className="text-2xl font-bold">
            {t("training.myMachines.title")}
          </h1>
          <p className="text-muted-foreground">
            {t("training.myMachines.description")}
          </p>
        </div>
        {isUser && machines.length > 0 && (
          <Badge
            variant={myHrMax !== null ? "secondary" : "destructive"}
            className="gap-1 text-sm"
          >
            <HeartPulse className="h-3.5 w-3.5" />
            {myHrMax !== null
              ? t("training.myMachines.myHrMax", {
                  value: `${myHrMax} ${t("training.units.bpm")}`,
                })
              : t("training.physiology.notSet")}
          </Badge>
        )}
      </div>

      {isUser && machines.length > 0 && myHrMax === null && (
        <Card className="border-amber-500/50 bg-amber-50 dark:bg-amber-950/40">
          <CardContent className="py-4 text-sm text-amber-800 dark:text-amber-200">
            {t("training.myMachines.myHrMaxMissing")}
          </CardContent>
        </Card>
      )}

      {machines.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-12 text-center">
            {isUser ? (
              <>
                <KeyRound className="h-12 w-12 text-muted-foreground mb-4" />
                <p className="text-muted-foreground max-w-md">
                  {t("training.myMachines.noRights")}
                </p>
              </>
            ) : (
              <>
                <Cpu className="h-12 w-12 text-muted-foreground mb-4" />
                <p className="text-muted-foreground">
                  {t("training.myMachines.noMachines")}
                </p>
              </>
            )}
          </CardContent>
        </Card>
      ) : (
        <div className="grid xl:grid-cols-2 gap-6">
          {machines.map((m) => (
            <Card key={m._id}>
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
                    {m.live && <LiveFreshBadge stale={false} />}
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                {m.live ? (
                  <LiveReadouts live={m.live} stale={false} />
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
                    onClick={() => setLaunchFor(m._id)}
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
          ))}
        </div>
      )}

      {launchFor && (
        <LaunchTrainingModal
          open={launchFor !== null}
          onOpenChange={(open) => !open && setLaunchFor(null)}
          machineId={launchFor}
        />
      )}
    </div>
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

function MyMachinesSkeleton() {
  return (
    <div className="space-y-6">
      <div>
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-4 w-72 mt-2" />
      </div>
      <div className="grid xl:grid-cols-2 gap-6">
        <Skeleton className="h-80" />
        <Skeleton className="h-80" />
      </div>
    </div>
  );
}

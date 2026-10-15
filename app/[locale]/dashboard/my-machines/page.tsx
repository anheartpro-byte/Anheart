"use client";

import { useState } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations } from "next-intl";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Cpu, HeartPulse, KeyRound } from "lucide-react";
import { LaunchTrainingModal } from "@/components/modals/LaunchTrainingModal";
import { ConsentCard } from "@/components/training/ConsentCard";
import { FitnessCard } from "@/components/training/FitnessCard";
import { LaunchableMachineCard } from "@/components/training/LaunchableMachineCard";

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
            <LaunchableMachineCard
              key={m._id}
              machine={m}
              isUser={isUser}
              onLaunch={() => setLaunchFor(m._id)}
            />
          ))}
        </div>
      )}

      {user && (
        <section aria-labelledby="my-readiness-title" className="space-y-3">
          <h2 id="my-readiness-title" className="text-xl font-semibold">
            {t("training.myMachines.readinessTitle")}
          </h2>
          <div className="grid gap-6 xl:grid-cols-2">
            <FitnessCard userId={user._id} />
            <ConsentCard userId={user._id} />
          </div>
        </section>
      )}

      {launchFor && (
        <LaunchTrainingModal
          open
          onOpenChange={(open) => !open && setLaunchFor(null)}
          machineId={launchFor}
        />
      )}
    </div>
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

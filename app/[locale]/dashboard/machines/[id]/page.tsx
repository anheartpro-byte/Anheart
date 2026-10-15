"use client";

import { useState, use } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import {
  isAnheartAdmin,
  isOrganizationAdmin,
  isStaff,
} from "@/lib/organizationAccess";
import { useMutationWithFeedback } from "@/hooks/use-mutation-with-feedback";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations, useLocale } from "next-intl";
import { useRouter, Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { AlertTriangle, Trash2, Pencil, RotateCcw, Play } from "lucide-react";
import { format } from "date-fns";
import { fr, enUS } from "date-fns/locale";
import {
  LastSignal,
  MachineStatusBadge,
  MachineStatusText,
  VersionsSeen,
} from "@/components/machines/MachineSignal";
import { MachineFormModal } from "@/components/modals/MachineFormModal";
import { LaunchTrainingModal } from "@/components/modals/LaunchTrainingModal";
import { MachineLiveCard } from "@/components/training/MachineLiveCard";
import { MachineHealthCard } from "@/components/machines/MachineHealthCard";
import { MachineKeysCard } from "@/components/machines/MachineKeysCard";
import { MachineProgramsCard } from "@/components/training/ProfileList";
import { LaunchRightsCard } from "@/components/training/LaunchRightsCard";

export default function MachineDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const t = useTranslations();
  const locale = useLocale();
  const router = useRouter();

  const machineId = id as Id<"machines">;
  const machine = useQuery(api.machines.getMachine, { machineId });
  const user = useQuery(api.users.getCurrentUser);

  const [showEditModal, setShowEditModal] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [showRestoreDialog, setShowRestoreDialog] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [showLaunchModal, setShowLaunchModal] = useState(false);

  const deleteMachine = useMutationWithFeedback(api.machines.deleteMachine);
  const restoreMachine = useMutationWithFeedback(api.machines.restoreMachine);

  const dateLocale = locale === "fr" ? fr : enUS;
  const medicalStatusText =
    machine === undefined || machine === null
      ? null
      : {
          unknown: null,
          unsigned: t("machines.medicalParametersUnsigned"),
          no_reference: t("machines.medicalParametersNoReference"),
          current: t("machines.medicalParametersCurrent"),
          obsolete: t("machines.medicalParametersObsolete", {
            reference: machine.medicalParametersReferenceVersion ?? "-",
          }),
          ahead: t("machines.medicalParametersAhead"),
        }[machine.medicalParametersStatus];
  const medicalStatusIsAlert =
    machine !== undefined &&
    machine !== null &&
    ["obsolete", "unsigned", "ahead"].includes(
      machine.medicalParametersStatus,
    );

  const canManage = isStaff(user?.role);
  // Restoring a machine is the Anheart admin's; the other admin rights are
  // also those of an organisation admin (lib/organizationAccess.ts).
  const isAdmin = isAnheartAdmin(user?.role);

  const handleDelete = async () => {
    const result = await deleteMachine(
      { machineId },
      { success: t("machines.deleteSuccess") },
    );
    if (result.ok) router.push("/dashboard/machines");
  };

  const handleRestore = async () => {
    setRestoring(true);
    const result = await restoreMachine(
      { machineId },
      { success: t("feedback.machineRestored") },
    );
    if (result.ok) setShowRestoreDialog(false);
    setRestoring(false);
  };

  if (machine === undefined) {
    return <MachineDetailSkeleton />;
  }

  if (machine === null) {
    return (
      <div className="text-center py-12">
        <p className="text-muted-foreground">{t("machines.notFound")}</p>
        <Link
          href="/dashboard/machines"
          className="text-primary hover:underline mt-2 inline-block"
        >
          {t("common.back")}
        </Link>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Deleted Machine Alert */}
      {machine.isDeleted && (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>{t("machines.deleted")}</AlertTitle>
          <AlertDescription className="flex items-center justify-between">
            <span>
              {t("machines.deletedAt")}:{" "}
              {machine.deletedAt
                ? format(machine.deletedAt, "PPpp", { locale: dateLocale })
                : "-"}
            </span>
            {isAdmin && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => setShowRestoreDialog(true)}
              >
                <RotateCcw className="h-4 w-4 mr-2" />
                {t("machines.restore")}
              </Button>
            )}
          </AlertDescription>
        </Alert>
      )}

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">{machine.name}</h1>
          <p className="text-muted-foreground">{machine.location || "-"}</p>
        </div>
        <div className="flex items-center gap-3">
          {canManage && !machine.isDeleted && (
            <Button onClick={() => setShowLaunchModal(true)}>
              <Play className="h-4 w-4 mr-2" />
              {t("training.launch.button")}
            </Button>
          )}
          <MachineStatusBadge
            machine={machine}
            isDeleted={machine.isDeleted}
            className="text-sm"
          />
        </div>
      </div>

      {/* Machine Info - Full width grid layout */}
      <div className="grid lg:grid-cols-2 gap-6">
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle>{t("machines.config")}</CardTitle>
              {canManage && !machine.isDeleted && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setShowEditModal(true)}
                >
                  <Pencil className="h-4 w-4 mr-2" />
                  {t("common.edit")}
                </Button>
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <div>
                <p className="text-sm text-muted-foreground">
                  {t("machines.status")}
                </p>
                <p className="font-medium">
                  <MachineStatusText machine={machine} />
                </p>
              </div>
              <div>
                <p className="text-sm text-muted-foreground">
                  {t("machines.lastHeartbeat")}
                </p>
                <p className="font-medium">
                  <LastSignal machine={machine} />
                </p>
              </div>
            </div>
            <Separator />
            <div>
              <p className="text-sm text-muted-foreground">
                {t("machines.createdAt")}
              </p>
              <p className="font-medium">
                {format(machine.createdAt, "PPP", { locale: dateLocale })}
              </p>
            </div>
            <Separator />
            <div className="grid grid-cols-2 gap-4">
              <div>
                <p className="text-sm text-muted-foreground">
                  {t("machines.softwareVersion")}
                </p>
                <p className="font-medium">{machine.softwareVersion ?? "-"}</p>
              </div>
              <div>
                <p className="text-sm text-muted-foreground">
                  {t("machines.contractVersion")}
                </p>
                <p className="font-medium">{machine.contractVersion ?? "-"}</p>
              </div>
              <div>
                <p className="text-sm text-muted-foreground">
                  {t("machines.medicalParametersVersion")}
                </p>
                <p className="font-medium">
                  {machine.medicalParametersVersion ?? "-"}
                </p>
                {medicalStatusText &&
                  (medicalStatusIsAlert ? (
                    <Badge variant="destructive">{medicalStatusText}</Badge>
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      {medicalStatusText}
                    </p>
                  ))}
              </div>
            </div>
            {machine.lastVersionSeenAt !== undefined && (
              <p className="text-xs text-muted-foreground">
                <VersionsSeen
                  at={machine.lastVersionSeenAt}
                  serverNow={machine.serverNow}
                />
              </p>
            )}
          </CardContent>
        </Card>

        {/* Gestionnaires Card */}
        {machine.gestionnaires.length > 0 && (
          <Card>
            <CardHeader>
              <CardTitle>{t("machines.gestionnaires")}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="space-y-3">
                {machine.gestionnaires.map((g) => (
                  <div
                    key={g._id}
                    className="flex items-center justify-between"
                  >
                    <div>
                      <p className="font-medium">
                        {g.firstName} {g.lastName}
                      </p>
                    </div>
                    {g.isOwner && (
                      <Badge variant="outline">{t("machines.owner")}</Badge>
                    )}
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        )}
      </div>

      {/* Training: live state and synced programmes */}
      {!machine.isDeleted && (
        <div className="grid lg:grid-cols-2 gap-6">
          <MachineLiveCard machineId={machineId} />
          <MachineProgramsCard machineId={machineId} />
        </div>
      )}

      {/* Launch rights - admins and gestionnaires of this machine */}
      {canManage && !machine.isDeleted && (
        <MachineHealthCard machineId={machineId} />
      )}
      {canManage && !machine.isDeleted && (
        <LaunchRightsCard
          machineId={machineId}
          isAdmin={isOrganizationAdmin(user?.role)}
        />
      )}

      {/* Keys of the machine (ANH-165): its admins only */}
      {isOrganizationAdmin(user?.role) && !machine.isDeleted && (
        <MachineKeysCard machineId={machineId} />
      )}

      {/* Danger Zone - Only show if not deleted */}
      {canManage && !machine.isDeleted && (
        <Card className="border-destructive/50">
          <CardHeader>
            <CardTitle className="text-destructive">
              {t("machines.dangerZone")}
            </CardTitle>
            <CardDescription>{t("machines.dangerZoneDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="font-medium">{t("common.delete")}</p>
                <p className="text-sm text-muted-foreground">
                  {t("machines.deleteDesc")}
                </p>
              </div>
              <Button
                variant="destructive"
                onClick={() => setShowDeleteDialog(true)}
                disabled={machine.status === "in_session"}
              >
                <Trash2 className="h-4 w-4 mr-2" />
                {t("common.delete")}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Delete Confirmation Dialog */}
      <Dialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("machines.deleteConfirm")}</DialogTitle>
            <DialogDescription>
              {t("machines.deleteConfirmDesc")}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setShowDeleteDialog(false)}
            >
              {t("common.cancel")}
            </Button>
            <Button variant="destructive" onClick={handleDelete}>
              {t("common.delete")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Restore Confirmation Dialog */}
      <Dialog open={showRestoreDialog} onOpenChange={setShowRestoreDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("machines.restoreConfirm")}</DialogTitle>
            <DialogDescription>
              {t("machines.restoreConfirmDesc")}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setShowRestoreDialog(false)}
            >
              {t("common.cancel")}
            </Button>
            <Button onClick={handleRestore} disabled={restoring}>
              {restoring ? t("common.loading") : t("machines.restore")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Launch Auto Session Modal */}
      <LaunchTrainingModal
        open={showLaunchModal}
        onOpenChange={setShowLaunchModal}
        machineId={machineId}
      />

      {/* Edit Machine Modal: its notification says that the machine was saved */}
      <MachineFormModal
        open={showEditModal}
        onOpenChange={setShowEditModal}
        machine={machine}
      />
    </div>
  );
}

function MachineDetailSkeleton() {
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <div className="flex-1">
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-4 w-32 mt-2" />
        </div>
        <Skeleton className="h-6 w-20" />
      </div>
      <div className="grid lg:grid-cols-2 gap-6">
        <Card>
          <CardHeader>
            <Skeleton className="h-6 w-32" />
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              <Skeleton className="h-20 w-full" />
              <Skeleton className="h-20 w-full" />
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <Skeleton className="h-6 w-32" />
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

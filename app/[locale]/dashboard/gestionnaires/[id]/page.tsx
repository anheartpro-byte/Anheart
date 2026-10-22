"use client";

import { useState, use } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { isOrganizationAdmin } from "@/lib/organizationAccess";
import { useMutationWithFeedback } from "@/hooks/use-mutation-with-feedback";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
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
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Shield, Users, Cpu, Pencil, ArrowLeft, Loader2 } from "lucide-react";
import { MachineStatusBadge } from "@/components/machines/MachineSignal";
import { machineIdsToSave, tickedBoxes } from "@/lib/gestionnaireMachines";
import { patientIdsToSave } from "@/lib/gestionnairePatients";

export default function GestionnaireDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const t = useTranslations();

  const gestionnaireId = id as Id<"users">;
  const user = useQuery(api.users.getCurrentUser);
  const gestionnaires = useQuery(api.users.listGestionnaires, {});
  const allMachines = useQuery(api.machines.listMachines, {
    includeDeleted: false,
  });
  const allPatients = useQuery(api.users.listUsers, { role: "user" });
  const gestionnairePatients = useQuery(api.users.getPatientsForGestionnaire, {
    gestionnaireId,
  });
  const gestionnaireMachines = useQuery(
    api.machines.getMachinesForGestionnaire,
    { gestionnaireId },
  );

  const setGestionnaireMachines = useMutationWithFeedback(
    api.machines.setGestionnaireMachines,
  );
  const assignPatients = useMutationWithFeedback(
    api.users.assignPatientsToGestionnaire,
  );

  const [showMachineDialog, setShowMachineDialog] = useState(false);
  const [showPatientDialog, setShowPatientDialog] = useState(false);
  // What the visitor clicked in each window since it opened, box by box. A
  // box clicked keeps that choice until the window is opened again; the
  // others show the server as it is now (`tickedBoxes`). A new answer of the
  // server while a window is open (a heartbeat of one of the machines is
  // enough) never undoes an edit in progress.
  const [machineChoices, setMachineChoices] = useState<
    ReadonlyMap<Id<"machines">, boolean>
  >(new Map());
  const [patientChoices, setPatientChoices] = useState<
    ReadonlyMap<Id<"users">, boolean>
  >(new Map());
  const [saving, setSaving] = useState(false);
  const [machinesError, setMachinesError] = useState<string | null>(null);
  const [patientsError, setPatientsError] = useState<string | null>(null);

  const gestionnaire = gestionnaires?.find((g) => g._id === gestionnaireId);

  // The account and the lists are still loading.
  if (
    user === undefined ||
    gestionnaires === undefined ||
    allMachines === undefined
  ) {
    return <GestionnaireDetailSkeleton />;
  }

  if (!isOrganizationAdmin(user?.role)) {
    return (
      <div className="text-center py-12">
        <Shield className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
        <p className="text-muted-foreground">
          {t("gestionnaires.accessDenied")}
        </p>
      </div>
    );
  }

  if (!gestionnaire) {
    return (
      <div className="text-center py-12">
        <p className="text-muted-foreground">{t("gestionnaires.notFound")}</p>
        <Link
          href="/dashboard/gestionnaires"
          className="text-primary hover:underline mt-2 inline-block"
        >
          {t("common.back")}
        </Link>
      </div>
    );
  }

  // The machines the window shows a box for, and those ticked.
  const listedMachines = allMachines.filter((m) => !m.isDeleted);
  const tickedMachines = new Set(
    tickedBoxes({
      listed: listedMachines.map((m) => m._id),
      linked: (gestionnaireMachines ?? []).map((m) => m._id),
      choices: machineChoices,
    }),
  );

  const openMachineDialog = () => {
    // The window always opens on the gestionnaire's current machines, never on
    // boxes left from a cancelled edit.
    if (!gestionnaireMachines) return;
    setMachineChoices(new Map());
    setMachinesError(null);
    setShowMachineDialog(true);
  };

  const handleSaveMachines = async () => {
    if (!gestionnaireMachines) return;
    setSaving(true);
    setMachinesError(null);
    try {
      // The server sets this gestionnaire's list exactly and touches no other
      // gestionnaire's link.
      const result = await setGestionnaireMachines(
        {
          gestionnaireId,
          machineIds: machineIdsToSave({
            checked: [...tickedMachines],
            listed: listedMachines.map((m) => m._id),
            linked: gestionnaireMachines.map((m) => m._id),
          }),
        },
        { success: (saved) => t("gestionnaires.machinesSaved", saved) },
      );
      if (!result.ok) {
        setMachinesError(result.message);
        return;
      }
      // The counts are said once, by the notification.
      setShowMachineDialog(false);
    } finally {
      setSaving(false);
    }
  };

  // The patients window needs both lists: the gestionnaire's own patients,
  // which tick the boxes, and the patients to choose from, which are the
  // boxes. Until both are known it is neither opened nor saved.
  const patientsLoaded =
    gestionnairePatients !== undefined && allPatients !== undefined;
  const tickedPatients = new Set(
    tickedBoxes({
      listed: (allPatients ?? []).map((p) => p._id),
      linked: (gestionnairePatients ?? []).map((p) => p._id),
      choices: patientChoices,
    }),
  );

  const openPatientDialog = () => {
    // The window always opens on the gestionnaire's current patients, never on
    // boxes left from a cancelled edit.
    if (!gestionnairePatients || !allPatients) return;
    setPatientChoices(new Map());
    setPatientsError(null);
    setShowPatientDialog(true);
  };

  const handleSavePatients = async () => {
    if (!gestionnairePatients || !allPatients) return;
    setSaving(true);
    setPatientsError(null);
    try {
      // The server sets this gestionnaire's list exactly and writes only the
      // links that differ from it.
      const result = await assignPatients(
        {
          gestionnaireId,
          patientIds: patientIdsToSave({
            checked: [...tickedPatients],
            listed: allPatients.map((p) => p._id),
            linked: gestionnairePatients.map((p) => p._id),
          }),
        },
        { success: (saved) => t("gestionnaires.patientsSaved", saved) },
      );
      if (!result.ok) {
        setPatientsError(result.message);
        return;
      }
      // The counts are said once, by the notification.
      setShowPatientDialog(false);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-4">
        <Link href="/dashboard/gestionnaires">
          <Button variant="ghost" size="icon">
            <ArrowLeft className="h-4 w-4" />
          </Button>
        </Link>
        <div>
          <h1 className="text-2xl font-bold">
            {gestionnaire.firstName} {gestionnaire.lastName}
          </h1>
          <p className="text-muted-foreground">{gestionnaire.email}</p>
        </div>
        <Badge variant="secondary" className="ml-auto">
          {t("users.roles.gestionnaire")}
        </Badge>
      </div>

      {/* Stats */}
      <div className="grid md:grid-cols-2 gap-6">
        {/* Machines Card */}
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <div>
                <CardTitle className="flex items-center gap-2">
                  <Cpu className="h-4 w-4" />
                  {t("nav.machines")}
                </CardTitle>
                {gestionnaire.machineCount > 0 && (
                  <CardDescription>
                    {t("gestionnaires.machinesAssigned", {
                      count: gestionnaire.machineCount,
                    })}
                  </CardDescription>
                )}
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={openMachineDialog}
                disabled={gestionnaireMachines === undefined}
              >
                <Pencil className="h-4 w-4 mr-2" />
                {t("common.edit")}
              </Button>
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            {gestionnaireMachines === undefined && (
              <p className="text-muted-foreground text-sm">
                {t("common.loading")}
              </p>
            )}
            {gestionnaireMachines?.length === 0 && (
              <p className="text-muted-foreground text-sm">
                {t("gestionnaires.noMachinesAssigned")}
              </p>
            )}
            {gestionnaireMachines && gestionnaireMachines.length > 0 && (
              <div className="space-y-2">
                {gestionnaireMachines.map((m) => (
                  <div
                    key={m._id}
                    className="flex items-center justify-between p-2 rounded-md bg-muted/50"
                  >
                    <span className="font-medium">{m.name}</span>
                    <MachineStatusBadge machine={m} />
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Patients Card */}
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <div>
                <CardTitle className="flex items-center gap-2">
                  <Users className="h-4 w-4" />
                  {t("nav.patients")}
                </CardTitle>
                {gestionnaire.patientCount > 0 && (
                  <CardDescription>
                    {t("gestionnaires.patientsAssigned", {
                      count: gestionnaire.patientCount,
                    })}
                  </CardDescription>
                )}
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={openPatientDialog}
                disabled={!patientsLoaded}
              >
                <Pencil className="h-4 w-4 mr-2" />
                {t("common.edit")}
              </Button>
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            {gestionnairePatients === undefined && (
              <p className="text-muted-foreground text-sm">
                {t("common.loading")}
              </p>
            )}
            {gestionnairePatients?.length === 0 && (
              <p className="text-muted-foreground text-sm">
                {t("gestionnaires.noPatientsAssigned")}
              </p>
            )}
            {gestionnairePatients && gestionnairePatients.length > 0 && (
              <div className="space-y-2">
                {gestionnairePatients.map((p) => (
                  <div
                    key={p._id}
                    className="flex items-center justify-between p-2 rounded-md bg-muted/50"
                  >
                    <span className="font-medium">
                      {p.firstName} {p.lastName}
                    </span>
                    <span className="text-sm text-muted-foreground">
                      {p.email}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Machine Assignment Dialog */}
      <Dialog open={showMachineDialog} onOpenChange={setShowMachineDialog}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{t("gestionnaires.assignMachines")}</DialogTitle>
            <DialogDescription>
              {t("gestionnaires.assignMachinesDesc")}
            </DialogDescription>
          </DialogHeader>
          {machinesError && (
            <div
              role="alert"
              className="bg-destructive/10 text-destructive p-3 rounded-md text-sm"
            >
              {machinesError}
            </div>
          )}
          <div className="max-h-64 overflow-y-auto space-y-2 py-4">
            {listedMachines.map((machine) => (
              <div key={machine._id} className="flex items-center space-x-2">
                <Checkbox
                  id={`machine-${machine._id}`}
                  checked={tickedMachines.has(machine._id)}
                  onCheckedChange={(checked) =>
                    setMachineChoices((choices) =>
                      new Map(choices).set(machine._id, checked === true),
                    )
                  }
                />
                <label
                  htmlFor={`machine-${machine._id}`}
                  className="flex-1 cursor-pointer"
                >
                  <span className="font-medium">{machine.name}</span>
                  {machine.location && (
                    <span className="text-muted-foreground ml-2">
                      ({machine.location})
                    </span>
                  )}
                </label>
                <MachineStatusBadge machine={machine} />
              </div>
            ))}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setShowMachineDialog(false)}
            >
              {t("common.cancel")}
            </Button>
            <Button onClick={handleSaveMachines} disabled={saving}>
              {saving && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              {t("common.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Patient Assignment Dialog */}
      <Dialog open={showPatientDialog} onOpenChange={setShowPatientDialog}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{t("gestionnaires.assignPatients")}</DialogTitle>
            <DialogDescription>
              {t("gestionnaires.assignPatientsDesc")}
            </DialogDescription>
          </DialogHeader>
          {patientsError && (
            <div
              role="alert"
              className="bg-destructive/10 text-destructive p-3 rounded-md text-sm"
            >
              {patientsError}
            </div>
          )}
          <div className="max-h-64 overflow-y-auto space-y-2 py-4">
            {!patientsLoaded && (
              <p className="text-muted-foreground text-center py-4">
                {t("common.loading")}
              </p>
            )}
            {patientsLoaded && allPatients.length === 0 && (
              <p className="text-muted-foreground text-center py-4">
                {t("users.noPatients")}
              </p>
            )}
            {patientsLoaded &&
              allPatients.map((patient) => (
                <div key={patient._id} className="flex items-center space-x-2">
                  <Checkbox
                    id={`patient-${patient._id}`}
                    checked={tickedPatients.has(patient._id)}
                    onCheckedChange={(checked) =>
                      setPatientChoices((choices) =>
                        new Map(choices).set(patient._id, checked === true),
                      )
                    }
                  />
                  <label
                    htmlFor={`patient-${patient._id}`}
                    className="flex-1 cursor-pointer"
                  >
                    <span className="font-medium">
                      {patient.firstName} {patient.lastName}
                    </span>
                    <span className="text-muted-foreground ml-2">
                      ({patient.email})
                    </span>
                  </label>
                </div>
              ))}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setShowPatientDialog(false)}
            >
              {t("common.cancel")}
            </Button>
            <Button
              onClick={handleSavePatients}
              disabled={saving || !patientsLoaded}
            >
              {saving && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              {t("common.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function GestionnaireDetailSkeleton() {
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <Skeleton className="h-10 w-10" />
        <div>
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-4 w-32 mt-2" />
        </div>
      </div>
      <div className="grid md:grid-cols-2 gap-6">
        <Card>
          <CardHeader>
            <Skeleton className="h-6 w-32" />
          </CardHeader>
          <CardContent>
            <Skeleton className="h-20 w-full" />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <Skeleton className="h-6 w-32" />
          </CardHeader>
          <CardContent>
            <Skeleton className="h-20 w-full" />
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

"use client";

import { useState, use, useEffect } from "react";
import { useQuery, useMutation } from "convex/react";
import { api } from "@/convex/_generated/api";
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

export default function GestionnaireDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const t = useTranslations();

  const gestionnaireId = id as Id<"users">;
  const user = useQuery(api.users.getCurrentUser);
  const gestionnaires = useQuery(api.users.listGestionnaires);
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

  const assignMachines = useMutation(api.machines.assignMachineToGestionnaires);
  const assignPatients = useMutation(api.users.assignPatientsToGestionnaire);

  const [showMachineDialog, setShowMachineDialog] = useState(false);
  const [showPatientDialog, setShowPatientDialog] = useState(false);
  const [selectedMachines, setSelectedMachines] = useState<string[]>([]);
  const [selectedPatients, setSelectedPatients] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  const gestionnaire = gestionnaires?.find((g) => g._id === gestionnaireId);

  // Initialize selections when data loads
  useEffect(() => {
    if (gestionnaireMachines) {
      setSelectedMachines(gestionnaireMachines.map((m) => m._id));
    }
  }, [gestionnaireMachines]);

  useEffect(() => {
    if (gestionnairePatients) {
      setSelectedPatients(gestionnairePatients.map((p) => p._id));
    }
  }, [gestionnairePatients]);

  // Only admin can access this page
  if (
    user === undefined ||
    gestionnaires === undefined ||
    allMachines === undefined
  ) {
    return <GestionnaireDetailSkeleton />;
  }

  if (user?.role !== "admin") {
    return (
      <div className="text-center py-12">
        <Shield className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
        <p className="text-muted-foreground">Admin access required</p>
      </div>
    );
  }

  if (!gestionnaire) {
    return (
      <div className="text-center py-12">
        <p className="text-muted-foreground">Gestionnaire not found</p>
        <Link
          href="/dashboard/gestionnaires"
          className="text-primary hover:underline mt-2 inline-block"
        >
          {t("common.back")}
        </Link>
      </div>
    );
  }

  const handleSaveMachines = async () => {
    setSaving(true);
    try {
      // For each selected machine, ensure this gestionnaire is assigned
      // This is a simplified approach - ideally we'd have a bulk update
      for (const machineId of selectedMachines) {
        const machine = allMachines.find((m) => m._id === machineId);
        if (machine) {
          // Get current gestionnaires and add this one if not present
          await assignMachines({
            machineId: machineId as Id<"machines">,
            gestionnaireIds: [gestionnaireId],
          });
        }
      }
      setShowMachineDialog(false);
    } catch (error) {
      console.error(error);
    } finally {
      setSaving(false);
    }
  };

  const handleSavePatients = async () => {
    setSaving(true);
    try {
      await assignPatients({
        gestionnaireId,
        patientIds: selectedPatients as Id<"users">[],
      });
      setShowPatientDialog(false);
    } catch (error) {
      console.error(error);
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
                <CardDescription>
                  {gestionnaire.machineCount}{" "}
                  {t("gestionnaires.machinesAssigned")}
                </CardDescription>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setShowMachineDialog(true)}
              >
                <Pencil className="h-4 w-4 mr-2" />
                {t("common.edit")}
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            {gestionnaireMachines && gestionnaireMachines.length > 0 ? (
              <div className="space-y-2">
                {gestionnaireMachines.map((m) => (
                  <div
                    key={m._id}
                    className="flex items-center justify-between p-2 rounded-md bg-muted/50"
                  >
                    <span className="font-medium">{m.name}</span>
                    <Badge
                      variant={m.status === "online" ? "default" : "secondary"}
                    >
                      {m.status}
                    </Badge>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-muted-foreground text-sm">
                {t("gestionnaires.noMachinesAssigned")}
              </p>
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
                <CardDescription>
                  {gestionnaire.patientCount}{" "}
                  {t("gestionnaires.patientsAssigned")}
                </CardDescription>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setShowPatientDialog(true)}
              >
                <Pencil className="h-4 w-4 mr-2" />
                {t("common.edit")}
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            {gestionnairePatients && gestionnairePatients.length > 0 ? (
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
            ) : (
              <p className="text-muted-foreground text-sm">
                {t("gestionnaires.noPatientsAssigned")}
              </p>
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
          <div className="max-h-64 overflow-y-auto space-y-2 py-4">
            {allMachines
              .filter((m) => !m.isDeleted)
              .map((machine) => (
                <div key={machine._id} className="flex items-center space-x-2">
                  <Checkbox
                    id={`machine-${machine._id}`}
                    checked={selectedMachines.includes(machine._id)}
                    onCheckedChange={(checked) => {
                      if (checked) {
                        setSelectedMachines([...selectedMachines, machine._id]);
                      } else {
                        setSelectedMachines(
                          selectedMachines.filter((id) => id !== machine._id),
                        );
                      }
                    }}
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
                  <Badge
                    variant={
                      machine.status === "online" ? "default" : "outline"
                    }
                  >
                    {machine.status}
                  </Badge>
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
          <div className="max-h-64 overflow-y-auto space-y-2 py-4">
            {allPatients && allPatients.length > 0 ? (
              allPatients.map((patient) => (
                <div key={patient._id} className="flex items-center space-x-2">
                  <Checkbox
                    id={`patient-${patient._id}`}
                    checked={selectedPatients.includes(patient._id)}
                    onCheckedChange={(checked) => {
                      if (checked) {
                        setSelectedPatients([...selectedPatients, patient._id]);
                      } else {
                        setSelectedPatients(
                          selectedPatients.filter((id) => id !== patient._id),
                        );
                      }
                    }}
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
              ))
            ) : (
              <p className="text-muted-foreground text-center py-4">
                {t("users.noPatients")}
              </p>
            )}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setShowPatientDialog(false)}
            >
              {t("common.cancel")}
            </Button>
            <Button onClick={handleSavePatients} disabled={saving}>
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

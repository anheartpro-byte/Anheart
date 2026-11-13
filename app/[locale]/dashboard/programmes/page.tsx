"use client";

import { useState } from "react";
import { useQuery } from "convex/react";
import type { FunctionReturnType } from "convex/server";
import { useTranslations } from "next-intl";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import {
  programmeProfileFields,
  type ProgrammeProfile,
} from "@/convex/lib/programmes";
import { useMutationWithFeedback } from "@/hooks/use-mutation-with-feedback";
import { useRegistryUpload } from "@/components/machines/MachineRegistryForms";
import { ProgrammeEditor } from "@/components/programmes/ProgrammeEditor";
import { ProgrammePreview } from "@/components/programmes/ProgrammePreview";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { armRpm, formatMinutes } from "@/lib/training";

type Programme = FunctionReturnType<typeof api.programmes.list>[number];

/** The Pi's shipped `standard_30_min`, as the start of a new programme. */
const NEW_PROGRAMME: ProgrammeProfile = {
  profileId: "",
  name: "",
  totalDurationS: 1800,
  baselineS: 180,
  warmupMaxS: 300,
  holdMinS: 300,
  cooldownS: 240,
  recoveryS: 300,
  zoneLowBpm: 118,
  zoneHighBpm: 138,
  hardMaxBpm: 148,
  criticalBpm: 158,
  subjectHrMax: 162,
  minRunRpm: 55,
  maxRpm: 276,
  warmupRpmCeilingFraction: 0.6,
  channels: ["ECG"],
  allowAboveNameplate: false,
};

const profileOf = (programme: Programme) =>
  Object.fromEntries(
    Object.keys(programmeProfileFields).map((key) => [
      key,
      programme[key as keyof ProgrammeProfile],
    ]),
  ) as ProgrammeProfile;

/**
 * The programme catalogue (ANH-137). The Anheart admin writes it: create,
 * copy, edit a draft, attach its simulation report, validate (a second
 * admin), publish, retire. Every other account reads what is published.
 */
export default function ProgrammesPage() {
  const t = useTranslations("programmes");
  const user = useQuery(api.users.getCurrentUser);
  const ready = user?.organizationAccess === "granted";
  const programmes = useQuery(api.programmes.list, ready ? {} : "skip");
  const admin = user?.role === "admin";
  const [draft, setDraft] = useState<{
    programmeId: Id<"programmes"> | null;
    profile: ProgrammeProfile;
    /** A new form each time one is opened, even over an open one. */
    opened: number;
  } | null>(null);
  const open = (programmeId: Id<"programmes"> | null, profile: ProgrammeProfile) =>
    setDraft((current) => ({
      programmeId,
      profile,
      opened: (current?.opened ?? 0) + 1,
    }));
  const [shown, setShown] = useState<Id<"programmes"> | null>(null);
  const [validating, setValidating] = useState<Id<"programmes"> | null>(null);
  const publish = useMutationWithFeedback(api.programmes.publish);
  const retire = useMutationWithFeedback(api.programmes.retire);
  const attach = useMutationWithFeedback(api.programmes.attachSimulationReport);
  const upload = useRegistryUpload();

  if (!ready || programmes === undefined)
    return <Skeleton className="h-40 w-full" />;
  const selected = programmes.find((programme) => programme._id === shown);

  async function attachReport(programmeId: Id<"programmes">, file?: File) {
    const storageId = await upload(file);
    if (storageId !== null)
      await attach({ programmeId, storageId }, { success: t("attached") });
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">{t("title")}</h1>
          <p className="text-muted-foreground">
            {admin ? t("description") : t("readOnly")}
          </p>
        </div>
        {admin && (
          <Button
            onClick={() => open(null, NEW_PROGRAMME)}
          >
            {t("actions.new")}
          </Button>
        )}
      </div>

      {draft && (
        <ProgrammeEditor
          key={draft.opened}
          programmeId={draft.programmeId}
          initial={draft.profile}
          onDone={() => setDraft(null)}
        />
      )}

      <Card>
        <CardContent>
          {programmes.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t("empty")}</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("columns.name")}</TableHead>
                  <TableHead>{t("columns.status")}</TableHead>
                  <TableHead>{t("columns.zone")}</TableHead>
                  <TableHead>{t("columns.duration")}</TableHead>
                  <TableHead>{t("columns.maxRpm")}</TableHead>
                  <TableHead>{t("columns.revision")}</TableHead>
                  <TableHead>{t("columns.actions")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {programmes.map((programme) => (
                  <TableRow key={programme._id}>
                    <TableCell>
                      <p className="font-medium">{programme.name}</p>
                      <p className="text-xs text-muted-foreground">
                        {programme.profileId}
                      </p>
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={
                          programme.status === "published" ? "default" : "outline"
                        }
                      >
                        {t(`status.${programme.status}`)}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      {t("zone", {
                        low: programme.zoneLowBpm,
                        high: programme.zoneHighBpm,
                      })}
                    </TableCell>
                    <TableCell>{formatMinutes(programme.totalDurationS)}</TableCell>
                    <TableCell>
                      {t("maxRpm", {
                        arm: armRpm(programme.maxRpm).toFixed(1),
                        motor: programme.maxRpm,
                      })}
                    </TableCell>
                    <TableCell>{programme.revision}</TableCell>
                    <TableCell>
                      <div className="flex flex-wrap items-center gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => setShown(programme._id)}
                        >
                          {t("actions.view")}
                        </Button>
                        {admin && (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() =>
                              open(null, {
                                ...profileOf(programme),
                                profileId: `${programme.profileId}_copie`.slice(0, 64),
                                name: t("copyName", { name: programme.name }),
                              })
                            }
                          >
                            {t("actions.duplicate")}
                          </Button>
                        )}
                        {admin && programme.status === "draft" && (
                          <>
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() =>
                                open(programme._id, profileOf(programme))
                              }
                            >
                              {t("actions.edit")}
                            </Button>
                            <Label className="text-xs font-normal">
                              {t("actions.attach")}
                              <Input
                                type="file"
                                accept="application/pdf,application/json"
                                className="h-8 w-48"
                                onChange={(event) => {
                                  void attachReport(
                                    programme._id,
                                    event.target.files?.[0],
                                  );
                                  event.target.value = "";
                                }}
                              />
                            </Label>
                            <Button
                              size="sm"
                              onClick={() => setValidating(programme._id)}
                            >
                              {t("actions.validate")}
                            </Button>
                          </>
                        )}
                        {admin && programme.status === "validated" && (
                          <Button
                            size="sm"
                            onClick={() =>
                              publish(
                                { programmeId: programme._id },
                                { success: t("published") },
                              )
                            }
                          >
                            {t("actions.publish")}
                          </Button>
                        )}
                        {admin && programme.status === "published" && (
                          <Button
                            size="sm"
                            variant="destructive"
                            onClick={() =>
                              retire(
                                { programmeId: programme._id },
                                { success: t("retired") },
                              )
                            }
                          >
                            {t("actions.retire")}
                          </Button>
                        )}
                        {programme.simulationReportUrl && (
                          <a
                            href={programme.simulationReportUrl}
                            target="_blank"
                            rel="noreferrer"
                            className="text-sm underline"
                          >
                            {t("actions.report")}
                          </a>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {selected && (
        <Card>
          <CardHeader>
            <CardTitle>{selected.name}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">
              {t("medicalVersion", { version: selected.medicalParametersVersion })}
              {selected.medicalSignoff &&
                ` · ${t("signedOff", {
                  by: selected.medicalSignoff.by,
                  at: new Date(selected.medicalSignoff.at).toISOString().slice(0, 10),
                })}`}
            </p>
            <ProgrammePreview profile={profileOf(selected)} />
          </CardContent>
        </Card>
      )}

      {validating && (
        <ValidateDialog
          programmeId={validating}
          onClose={() => setValidating(null)}
        />
      )}
    </div>
  );
}

/** The second admin's validation, with the medical sign-off it records. */
function ValidateDialog({
  programmeId,
  onClose,
}: {
  programmeId: Id<"programmes">;
  onClose: () => void;
}) {
  const t = useTranslations("programmes.validation");
  const tProgrammes = useTranslations("programmes");
  const validate = useMutationWithFeedback(api.programmes.validate);
  const [by, setBy] = useState("");
  const [at, setAt] = useState("");
  const [note, setNote] = useState("");

  async function submit() {
    const outcome = await validate(
      { programmeId, medicalSignoff: { by, at: Date.parse(at), note } },
      { success: tProgrammes("validated") },
    );
    if (outcome.ok) onClose();
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("title")}</DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="signoff-by">{t("by")}</Label>
            <Input id="signoff-by" value={by} onChange={(event) => setBy(event.target.value)} />
          </div>
          <div className="space-y-1">
            <Label htmlFor="signoff-at">{t("at")}</Label>
            <Input
              id="signoff-at"
              type="date"
              value={at}
              onChange={(event) => setAt(event.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="signoff-note">{t("note")}</Label>
            <Textarea
              id="signoff-note"
              value={note}
              onChange={(event) => setNote(event.target.value)}
            />
          </div>
          <Button onClick={submit}>{t("submit")}</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

"use client";

import { useState } from "react";
import { useQuery, useMutation } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations } from "next-intl";
import { useRouter } from "@/i18n/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { AlertCircle, AlertTriangle, Hand, Info, Loader2 } from "lucide-react";
import {
  MIN_RIDER_AGE,
  armRpm,
  convexErrorMessage,
  effectiveHrMax,
  formatMinutes,
  readOptionalNumber,
  zoneCeiling,
} from "@/lib/training";

interface LaunchTrainingModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  machineId: Id<"machines">;
  /** Pre-select a programme (e.g. from a preset's own launch button). */
  profileId?: string;
  onLaunched?: (sessionId: Id<"sessions">) => void;
}

const SELF = "self";

/**
 * Launch an AUTO session: the only kind the dashboard can start. Manual
 * sessions are started at the machine's console, never from here.
 */
export function LaunchTrainingModal({
  open,
  onOpenChange,
  machineId,
  profileId,
  onLaunched,
}: LaunchTrainingModalProps) {
  const t = useTranslations("training.launch");

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("title")}</DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>
        {/* Mounted only while open, so every opening starts from a clean form. */}
        {open && (
          <LaunchForm
            machineId={machineId}
            initialProfileId={profileId}
            onClose={() => onOpenChange(false)}
            onLaunched={onLaunched}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

type RiderHrMax =
  | { state: "known"; value: number; estimated: boolean }
  | { state: "missing" }
  | { state: "unknown" };

function LaunchForm({
  machineId,
  initialProfileId,
  onClose,
  onLaunched,
}: {
  machineId: Id<"machines">;
  initialProfileId?: string;
  onClose: () => void;
  onLaunched?: (sessionId: Id<"sessions">) => void;
}) {
  const t = useTranslations();
  const router = useRouter();

  const me = useQuery(api.users.getCurrentUser);
  const machines = useQuery(api.training.listLaunchableMachines, {});
  const isManager = me?.role === "admin" || me?.role === "gestionnaire";
  const isAdmin = me?.role === "admin";

  const allUsers = useQuery(
    api.users.listUsers,
    me && isAdmin ? { role: "user" } : "skip",
  );
  const myPatients = useQuery(
    api.users.getPatientsForGestionnaire,
    me && me.role === "gestionnaire" ? {} : "skip",
  );
  const rights = useQuery(
    api.training.listLaunchRights,
    me && isManager ? { machineId } : "skip",
  );
  const launch = useMutation(api.training.launchAutoSession);

  const [selectedProfile, setSelectedProfile] = useState(
    initialProfileId ?? "",
  );
  const [rider, setRider] = useState<string>(SELF);
  const [minutes, setMinutes] = useState("");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const riderIsPatient = isManager && rider !== SELF && rider !== "";
  const riderDoc = useQuery(
    api.users.getUserById,
    riderIsPatient ? { userId: rider as Id<"users"> } : "skip",
  );

  if (me === undefined || machines === undefined) {
    return (
      <div className="flex items-center justify-center py-8">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const machine = machines.find((m) => m._id === machineId) ?? null;
  const profile =
    machine?.profiles.find((p) => p.profileId === selectedProfile) ?? null;
  const riderOptions = (isAdmin ? allUsers : myPatients) ?? [];

  // The rider's effective max heart rate, from the most authoritative source
  // available client-side. The server re-checks it at launch either way.
  let riderHrMax: RiderHrMax;
  if (!riderIsPatient) {
    riderHrMax =
      machine?.myHrMax !== null && machine?.myHrMax !== undefined
        ? { state: "known", value: machine.myHrMax, estimated: false }
        : { state: "missing" };
  } else {
    const right = rights?.find((r) => r.userId === rider);
    const hrMax = riderDoc ? readOptionalNumber(riderDoc, "hrMax") : undefined;
    const birthYear = riderDoc
      ? readOptionalNumber(riderDoc, "birthYear")
      : undefined;
    const computed =
      hrMax !== undefined || birthYear !== undefined
        ? effectiveHrMax(hrMax, birthYear, new Date().getTime())
        : null;
    if (computed) {
      riderHrMax = {
        state: "known",
        value: computed.value,
        estimated: computed.source === "estimated",
      };
    } else if (right) {
      riderHrMax =
        right.hrMax !== null
          ? { state: "known", value: right.hrMax, estimated: false }
          : { state: "missing" };
    } else {
      riderHrMax = { state: "unknown" };
    }
  }

  // Why the launch cannot go ahead at all (the server would refuse).
  let blocker: string | null = null;
  if (!machine) blocker = t("training.launch.notLaunchable");
  else if (machine.status === "offline") blocker = t("training.launch.offline");
  else if (machine.status === "in_session")
    blocker = t("training.launch.inSession");
  else if (!machine.programsEnabled)
    blocker = t("training.launch.programsDisabled");
  else if (machine.profiles.length === 0)
    blocker = t("training.launch.noPrograms");
  else if (riderHrMax.state === "missing")
    blocker = t("training.launch.hrMaxMissing");
  else if (riderIsPatient && riderDoc) {
    // Mirrors the server's age gate (MIN_RIDER_AGE): birth year required, adults only.
    const birthYear = readOptionalNumber(riderDoc, "birthYear");
    if (birthYear === undefined)
      blocker = t("training.launch.birthYearMissing");
    else if (new Date().getUTCFullYear() - birthYear - 1 < MIN_RIDER_AGE)
      blocker = t("training.launch.tooYoung", { min: MIN_RIDER_AGE });
  }

  // Client-side preview of the server's zone check.
  let zoneWarning: string | null = null;
  if (profile && riderHrMax.state === "known") {
    const ceiling = zoneCeiling(riderHrMax.value);
    if (profile.zoneHighBpm > ceiling) {
      zoneWarning = t("training.launch.zoneTooHigh", {
        zoneHigh: profile.zoneHighBpm,
        hrMax: riderHrMax.value,
        ceiling,
      });
    } else if (profile.hardMaxBpm > riderHrMax.value) {
      zoneWarning = t("training.launch.hardMaxTooHigh", {
        hardMax: profile.hardMaxBpm,
        hrMax: riderHrMax.value,
      });
    }
  }

  const trimmedMinutes = minutes.trim();
  const parsedMinutes = trimmedMinutes === "" ? null : Number(trimmedMinutes);
  const durationInvalid =
    parsedMinutes !== null &&
    (!Number.isFinite(parsedMinutes) || parsedMinutes <= 0);

  const canSubmit =
    blocker === null &&
    profile !== null &&
    rider !== "" &&
    !durationInvalid &&
    !submitting;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit || !profile) return;
    setSubmitting(true);
    setError(null);
    try {
      const sessionId = await launch({
        machineId,
        profileId: profile.profileId,
        userId: riderIsPatient ? (rider as Id<"users">) : undefined,
        totalDurationS:
          parsedMinutes !== null ? Math.round(parsedMinutes * 60) : undefined,
        notes: notes.trim() || undefined,
      });
      onClose();
      if (onLaunched) onLaunched(sessionId);
      else router.push(`/dashboard/sessions/${sessionId}/live`);
    } catch (err) {
      setError(convexErrorMessage(err, t("common.error")));
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      {error && (
        <div className="bg-destructive/10 text-destructive p-3 rounded-md text-sm">
          {error}
        </div>
      )}

      {blocker && (
        <div className="flex items-start gap-2 rounded-md border border-amber-500/50 bg-amber-50 dark:bg-amber-950/40 p-3 text-sm text-amber-800 dark:text-amber-200">
          <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" />
          <span>{blocker}</span>
        </div>
      )}

      {machine && (
        <>
          <div className="space-y-2">
            <Label>{t("training.launch.program")} *</Label>
            <Select
              value={selectedProfile}
              onValueChange={setSelectedProfile}
              disabled={machine.profiles.length === 0}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder={t("training.launch.selectProgram")} />
              </SelectTrigger>
              <SelectContent>
                {machine.profiles.map((p) => (
                  <SelectItem key={p.profileId} value={p.profileId}>
                    {p.name} · {p.zoneLowBpm}-{p.zoneHighBpm}{" "}
                    {t("training.units.bpm")} ·{" "}
                    {formatMinutes(p.totalDurationS)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {profile && (
              <p className="text-xs text-muted-foreground">
                {t("training.programs.zone")} {profile.zoneLowBpm}-
                {profile.zoneHighBpm} {t("training.units.bpm")} ·{" "}
                {t("training.programs.hardMax")} {profile.hardMaxBpm}{" "}
                {t("training.units.bpm")} · {t("training.programs.maxRpm")}{" "}
                {t("training.programs.armRpm", {
                  rpm: armRpm(profile.maxRpm).toFixed(1),
                })}
              </p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="launch-duration">
              {t("training.launch.duration")}
            </Label>
            <Input
              id="launch-duration"
              type="number"
              inputMode="decimal"
              min={1}
              step={1}
              value={minutes}
              onChange={(e) => setMinutes(e.target.value)}
              placeholder={
                profile ? String(Math.round(profile.totalDurationS / 60)) : ""
              }
            />
            <p
              className={`text-xs ${durationInvalid ? "text-destructive" : "text-muted-foreground"}`}
            >
              {durationInvalid
                ? t("training.launch.durationInvalid")
                : t("training.launch.durationHint", {
                    minutes: profile
                      ? formatMinutes(profile.totalDurationS)
                      : "-",
                  })}
            </p>
          </div>

          <div className="space-y-2">
            <Label>{t("training.launch.rider")} *</Label>
            {isManager ? (
              <Select value={rider} onValueChange={setRider}>
                <SelectTrigger className="w-full">
                  <SelectValue placeholder={t("training.launch.selectRider")} />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={SELF}>
                    {t("training.launch.myself")} ({me?.firstName}{" "}
                    {me?.lastName})
                  </SelectItem>
                  {riderOptions.map((u) => (
                    <SelectItem key={u._id} value={u._id}>
                      {u.firstName} {u.lastName} ({u.email})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <Input
                value={`${me?.firstName ?? ""} ${me?.lastName ?? ""}`.trim()}
                disabled
              />
            )}
            <p className="text-xs text-muted-foreground">
              {t("training.launch.riderHrMax")} :{" "}
              {riderHrMax.state === "known" ? (
                <span className="font-medium text-foreground">
                  {riderHrMax.value} {t("training.units.bpm")}
                  {riderHrMax.estimated &&
                    ` (${t("training.launch.hrMaxEstimated")})`}
                </span>
              ) : riderHrMax.state === "missing" ? (
                <span className="text-destructive">
                  {t("training.physiology.notSet")}
                </span>
              ) : riderIsPatient && riderDoc === undefined ? (
                "…"
              ) : (
                t("training.launch.hrMaxUnknown")
              )}
            </p>
          </div>

          {zoneWarning && (
            <div className="flex items-start gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
              <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
              <span>{zoneWarning}</span>
            </div>
          )}

          <div className="space-y-2">
            <Label htmlFor="launch-notes">{t("training.launch.notes")}</Label>
            <Textarea
              id="launch-notes"
              rows={2}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder={t("training.launch.notesPlaceholder")}
            />
          </div>
        </>
      )}

      <div className="space-y-1 text-xs text-muted-foreground">
        <p className="flex items-start gap-2">
          <Info className="h-3.5 w-3.5 mt-0.5 shrink-0" />
          {t("training.launch.pendingNote")}
        </p>
        <p className="flex items-start gap-2">
          <Hand className="h-3.5 w-3.5 mt-0.5 shrink-0" />
          {t("training.manualOnlyAtMachine")}
        </p>
      </div>

      <div className="flex gap-3 pt-2">
        <Button type="submit" disabled={!canSubmit} className="flex-1">
          {submitting && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
          {t("training.launch.submit")}
        </Button>
        <Button type="button" variant="outline" onClick={onClose}>
          {t("common.cancel")}
        </Button>
      </div>
    </form>
  );
}

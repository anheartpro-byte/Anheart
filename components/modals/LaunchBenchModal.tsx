"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { AlertTriangle, Info, Loader2 } from "lucide-react";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { useMutationWithFeedback } from "@/hooks/use-mutation-with-feedback";
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
import { MAX_BALLAST_KG, MAX_LAUNCH_NOTES } from "@/lib/training";

/**
 * The mass typed in the form: null when the field is empty (no mass
 * declared), undefined when it is no mass the server takes (ANH-250: a
 * number of kg in (0, `MAX_BALLAST_KG`]).
 */
export function parseBallastKg(text: string): number | null | undefined {
  const trimmed = text.trim().replace(",", ".");
  if (trimmed === "") return null;
  const mass = Number(trimmed);
  return Number.isFinite(mass) && mass > 0 && mass <= MAX_BALLAST_KG
    ? mass
    : undefined;
}

/**
 * Launch a ballasted bench session (ANH-250): manual, capsule ballasted,
 * nobody on board, no rider. Offered to the staff who reach the machine;
 * the server checks it again (`training.launchBenchSession`), and the console
 * starts it only after its operator's physical confirmation and attestation.
 * The speed is set at the console, never from here.
 */
export function LaunchBenchModal({
  open,
  onOpenChange,
  machineId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  machineId: Id<"machines">;
}) {
  const t = useTranslations("training.bench");
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("title")}</DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>
        {/* Mounted only while open, so every opening starts from a clean form. */}
        {open && (
          <BenchForm
            machineId={machineId}
            onClose={() => onOpenChange(false)}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

function BenchForm({
  machineId,
  onClose,
}: {
  machineId: Id<"machines">;
  onClose: () => void;
}) {
  const t = useTranslations();
  const router = useRouter();
  const launch = useMutationWithFeedback(api.training.launchBenchSession);
  const [mass, setMass] = useState("");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const ballastKg = parseBallastKg(mass);
  const notesTooLong = notes.trim().length > MAX_LAUNCH_NOTES;
  const canSubmit = !submitting && ballastKg !== undefined && !notesTooLong;

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    const result = await launch(
      {
        machineId,
        ballastKg: ballastKg ?? undefined,
        notes: notes.trim() || undefined,
      },
      { success: t("training.bench.launched") },
    );
    if (!result.ok) {
      setError(result.message);
      setSubmitting(false);
      return;
    }
    onClose();
    router.push(`/dashboard/sessions/${result.value}/live`);
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      {error && (
        <div className="bg-destructive/10 text-destructive p-3 rounded-md text-sm">
          {error}
        </div>
      )}

      <div
        role="note"
        className="flex items-start gap-2 rounded-md border border-amber-500/50 bg-amber-50 dark:bg-amber-950/40 p-3 text-sm text-amber-800 dark:text-amber-200"
      >
        <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
        <div className="space-y-1">
          <p className="font-medium">{t("training.bench.warningTitle")}</p>
          <p>
            {t("training.bench.warning", {
              attestation: t("training.bench.attestation"),
            })}
          </p>
        </div>
      </div>

      <div className="space-y-2">
        <Label htmlFor="bench-mass">{t("training.bench.mass")}</Label>
        <Input
          id="bench-mass"
          type="number"
          inputMode="decimal"
          min={0}
          max={MAX_BALLAST_KG}
          step="any"
          value={mass}
          onChange={(e) => setMass(e.target.value)}
          placeholder="80"
          aria-invalid={ballastKg === undefined}
        />
        <p
          className={`text-xs ${ballastKg === undefined ? "text-destructive" : "text-muted-foreground"}`}
        >
          {ballastKg === undefined
            ? t("training.bench.massInvalid", { max: MAX_BALLAST_KG })
            : t("training.bench.massHint", { max: MAX_BALLAST_KG })}
        </p>
      </div>

      <div className="space-y-2">
        <Label htmlFor="bench-notes">{t("training.bench.notes")}</Label>
        <Textarea
          id="bench-notes"
          rows={2}
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder={t("training.bench.notesPlaceholder")}
          aria-invalid={notesTooLong}
        />
        {notesTooLong && (
          <p className="text-xs text-destructive">
            {t("training.bench.notesTooLong", { max: MAX_LAUNCH_NOTES })}
          </p>
        )}
      </div>

      <p className="flex items-start gap-2 text-xs text-muted-foreground">
        <Info className="h-3.5 w-3.5 mt-0.5 shrink-0" />
        {t("training.bench.pendingNote")}
      </p>

      <div className="flex gap-3 pt-2">
        <Button type="submit" disabled={!canSubmit} className="flex-1">
          {submitting && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
          {t("training.bench.submit")}
        </Button>
        <Button type="button" variant="outline" onClick={onClose}>
          {t("common.cancel")}
        </Button>
      </div>
    </form>
  );
}

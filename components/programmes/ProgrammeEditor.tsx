"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import {
  programmeRefusal,
  type ProgrammeProfile,
} from "@/convex/lib/programmes";
import { useMutationWithFeedback } from "@/hooks/use-mutation-with-feedback";
import { ProgrammePreview } from "@/components/programmes/ProgrammePreview";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const NUMBERS = [
  "totalDurationS",
  "baselineS",
  "warmupMaxS",
  "holdMinS",
  "cooldownS",
  "recoveryS",
  "zoneLowBpm",
  "zoneHighBpm",
  "hardMaxBpm",
  "criticalBpm",
  "subjectHrMax",
  "minRunRpm",
  "maxRpm",
  "warmupRpmCeilingFraction",
] as const;
type NumberField = (typeof NUMBERS)[number];
/** What the form holds: every number as typed, so that a half-typed one stays. */
type Form = Omit<ProgrammeProfile, NumberField> & Record<NumberField, string>;
const CHANNELS = ["ECG", "EDA", "SpO2", "RESP", "EMG", "LUX"] as const;

function toForm(profile: ProgrammeProfile): Form {
  const numbers = Object.fromEntries(
    NUMBERS.map((key) => [key, String(profile[key])]),
  ) as Record<NumberField, string>;
  return { ...profile, ...numbers };
}

/** The fields the Pi reads as whole numbers (`_Reader.integer` in plan.py). */
const WHOLE = new Set<NumberField>([
  "zoneLowBpm",
  "zoneHighBpm",
  "hardMaxBpm",
  "criticalBpm",
  "subjectHrMax",
  "minRunRpm",
  "maxRpm",
]);
const WHOLE_NUMBER = /^[0-9]+$/;
const DECIMAL_NUMBER = /^[0-9]+(?:[.,][0-9]+)?$/;

/**
 * A number as typed, or NaN: digits only, and in a duration or the fraction
 * one `.` or `,` between digits. No sign (no field takes one), space,
 * exponent or hexadecimal, all of which `Number` would accept.
 */
function parseNumber(key: NumberField, text: string): number {
  const pattern = WHOLE.has(key) ? WHOLE_NUMBER : DECIMAL_NUMBER;
  return pattern.test(text) ? Number(text.replace(",", ".")) : NaN;
}

function toProfile(form: Form): ProgrammeProfile {
  const numbers = Object.fromEntries(
    NUMBERS.map((key) => [key, parseNumber(key, form[key])]),
  ) as Record<NumberField, number>;
  return { ...form, ...numbers };
}

/**
 * Write a draft of the catalogue (ANH-137): a new one, a copy, or the draft
 * `programmeId`. The Pi's verdict is shown as the fields change, in the Pi's
 * words (`programmeRefusal`, the same check the server runs), and a refused
 * programme cannot be saved.
 */
export function ProgrammeEditor({
  programmeId,
  initial,
  onDone,
}: {
  programmeId: Id<"programmes"> | null;
  initial: ProgrammeProfile;
  onDone: () => void;
}) {
  const t = useTranslations("programmes");
  const [form, setForm] = useState(() => toForm(initial));
  const create = useMutationWithFeedback(api.programmes.create);
  const update = useMutationWithFeedback(api.programmes.updateDraft);
  const profile = toProfile(form);
  const invalid = NUMBERS.filter((key) => Number.isNaN(profile[key]));
  const refused = programmeRefusal(profile);
  const [saving, setSaving] = useState(false);

  async function save() {
    setSaving(true);
    const saved = { success: t("saved") };
    const outcome =
      programmeId === null
        ? await create(profile, saved)
        : await update({ programmeId, ...profile }, saved);
    setSaving(false);
    if (outcome.ok) onDone();
  }

  const field = (key: "profileId" | "name" | NumberField) => (
    <div key={key} className="space-y-1">
      <Label htmlFor={`programme-${key}`}>{t(`fields.${key}`)}</Label>
      <Input
        id={`programme-${key}`}
        inputMode={key === "profileId" || key === "name" ? "text" : "decimal"}
        aria-invalid={invalid.some((item) => item === key)}
        value={form[key]}
        onChange={(event) => setForm({ ...form, [key]: event.target.value })}
      />
    </div>
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          {t(programmeId === null ? "editor.createTitle" : "editor.editTitle")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {field("profileId")}
          {field("name")}
          {NUMBERS.map(field)}
        </div>
        <fieldset className="space-y-2">
          <legend className="text-sm font-medium">{t("fields.channels")}</legend>
          <div className="flex flex-wrap gap-4">
            {CHANNELS.map((channel) => (
              <Label key={channel} className="flex items-center gap-2 font-normal">
                <Checkbox
                  checked={form.channels.includes(channel)}
                  onCheckedChange={(checked) =>
                    setForm({
                      ...form,
                      channels: checked
                        ? [...form.channels, channel]
                        : form.channels.filter((item) => item !== channel),
                    })
                  }
                />
                {channel}
              </Label>
            ))}
          </div>
        </fieldset>
        <Label className="flex items-center gap-2 font-normal">
          <Checkbox
            checked={form.allowAboveNameplate}
            onCheckedChange={(checked) =>
              setForm({ ...form, allowAboveNameplate: checked === true })
            }
          />
          {t("fields.allowAboveNameplate")}
        </Label>
        <Alert
          variant={invalid.length > 0 || refused ? "destructive" : "default"}
          role="status"
        >
          <AlertDescription>
            {invalid.length > 0 ? (
              t("editor.invalid", {
                fields: invalid.map((key) => t(`fields.${key}`)).join(", "),
              })
            ) : refused ? (
              <>
                {t("editor.refused")} <code>{refused.detail}</code>
              </>
            ) : (
              t("editor.accepted")
            )}
          </AlertDescription>
        </Alert>
        <ProgrammePreview profile={profile} />
        {programmeId !== null && (
          <p className="text-sm text-muted-foreground">{t("editor.evidenceCleared")}</p>
        )}
        <div className="flex gap-2">
          <Button
            onClick={save}
            disabled={invalid.length > 0 || refused !== null || saving}
          >
            {t("editor.save")}
          </Button>
          <Button variant="outline" onClick={onDone}>
            {t("editor.cancel")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

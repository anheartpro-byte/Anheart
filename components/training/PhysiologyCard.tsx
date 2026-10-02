"use client";

import { useState } from "react";
import { useMutation } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations } from "next-intl";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Check, HeartPulse, Loader2 } from "lucide-react";
import {
  convexErrorMessage,
  effectiveHrMax,
  readOptionalNumber,
} from "@/lib/training";

/**
 * Max heart rate / birth year of a rider, used to vet a programme's zone
 * before an auto session. Only for admins and the rider's gestionnaires.
 *
 * Only the fields the manager touched are sent, so a value the page could not
 * read is never overwritten by an empty input.
 */
export function PhysiologyCard({
  userId,
  user,
}: {
  userId: Id<"users">;
  /** The user document as returned by users.getUserById (hrMax / birthYear read if present). */
  user: object;
}) {
  const t = useTranslations();
  const setPhysiology = useMutation(api.training.setUserPhysiology);

  const currentHrMax = readOptionalNumber(user, "hrMax");
  const currentBirthYear = readOptionalNumber(user, "birthYear");

  const [hrMax, setHrMax] = useState(
    currentHrMax !== undefined ? String(currentHrMax) : "",
  );
  const [birthYear, setBirthYear] = useState(
    currentBirthYear !== undefined ? String(currentBirthYear) : "",
  );
  const [dirty, setDirty] = useState({ hrMax: false, birthYear: false });
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [nowYear] = useState(() => new Date().getFullYear());
  const nowMs = Date.UTC(nowYear, 6, 1);

  const parse = (s: string) => (s.trim() === "" ? null : Number(s));
  const hrMaxValue = parse(hrMax);
  const birthYearValue = parse(birthYear);
  const hrMaxInvalid =
    hrMaxValue !== null &&
    (!Number.isInteger(hrMaxValue) || hrMaxValue < 100 || hrMaxValue > 220);
  const age = birthYearValue !== null ? nowYear - birthYearValue : null;
  const birthYearInvalid =
    birthYearValue !== null &&
    (!Number.isInteger(birthYearValue) ||
      age === null ||
      age < 10 ||
      age > 100);

  // Preview of what the server will use: the form values once edited,
  // otherwise what the backend returned.
  const effective = effectiveHrMax(
    dirty.hrMax ? hrMaxValue : currentHrMax,
    dirty.birthYear ? birthYearValue : currentBirthYear,
    nowMs,
  );

  const canSave =
    (dirty.hrMax || dirty.birthYear) &&
    !hrMaxInvalid &&
    !birthYearInvalid &&
    !saving;

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSave) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      await setPhysiology({
        userId,
        ...(dirty.hrMax ? { hrMax: hrMaxValue } : {}),
        ...(dirty.birthYear ? { birthYear: birthYearValue } : {}),
      });
      setDirty({ hrMax: false, birthYear: false });
      setSaved(true);
    } catch (err) {
      setError(convexErrorMessage(err, t("common.error")));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <HeartPulse className="h-4 w-4" />
          {t("training.physiology.title")}
        </CardTitle>
        <CardDescription>
          {t("training.physiology.description")}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSave} className="space-y-4">
          {error && (
            <div className="bg-destructive/10 text-destructive p-3 rounded-md text-sm">
              {error}
            </div>
          )}
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label htmlFor="phys-hrmax">
                {t("training.physiology.hrMax")}
              </Label>
              <Input
                id="phys-hrmax"
                type="number"
                inputMode="numeric"
                min={100}
                max={220}
                value={hrMax}
                onChange={(e) => {
                  setHrMax(e.target.value);
                  setDirty((d) => ({ ...d, hrMax: true }));
                  setSaved(false);
                }}
              />
              {hrMaxInvalid && (
                <p className="text-xs text-destructive">
                  {t("training.physiology.invalidHrMax")}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="phys-birthyear">
                {t("training.physiology.birthYear")}
              </Label>
              <Input
                id="phys-birthyear"
                type="number"
                inputMode="numeric"
                min={nowYear - 100}
                max={nowYear - 10}
                value={birthYear}
                onChange={(e) => {
                  setBirthYear(e.target.value);
                  setDirty((d) => ({ ...d, birthYear: true }));
                  setSaved(false);
                }}
              />
              {birthYearInvalid && (
                <p className="text-xs text-destructive">
                  {t("training.physiology.invalidBirthYear")}
                </p>
              )}
            </div>
          </div>

          <div className="rounded-md bg-muted/40 p-3">
            <p className="text-sm text-muted-foreground">
              {t("training.physiology.effective")}
            </p>
            <p className="font-medium text-lg">
              {effective ? (
                <>
                  {effective.value} {t("training.units.bpm")}{" "}
                  <span className="text-sm font-normal text-muted-foreground">
                    (
                    {effective.source === "measured"
                      ? t("training.physiology.measured")
                      : t("training.physiology.estimated")}
                    )
                  </span>
                </>
              ) : (
                <span className="text-muted-foreground">
                  {t("training.physiology.notSet")}
                </span>
              )}
            </p>
          </div>

          <p className="text-xs text-muted-foreground">
            {t("training.physiology.hint")}
          </p>

          <Button type="submit" disabled={!canSave}>
            {saving ? (
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            ) : saved ? (
              <Check className="h-4 w-4 mr-2" />
            ) : null}
            {saved ? t("training.physiology.saved") : t("common.save")}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

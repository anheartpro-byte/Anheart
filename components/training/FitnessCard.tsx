"use client";

import { useState } from "react";
import { useQuery } from "convex/react";
import { format } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import { useLocale, useTranslations } from "next-intl";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { useMutationWithFeedback } from "@/hooks/use-mutation-with-feedback";
import { useLocalClock } from "@/hooks/use-local-clock";
import { serverClockAt } from "@/lib/server-clock";
import {
  CURRENT_QUESTIONNAIRE,
  LATEST_FITNESS_DATE,
  LATEST_FITNESS_MILLIS,
} from "@/lib/fitnessQuestionnaire";
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
import { Textarea } from "@/components/ui/textarea";
import { AlertTriangle, HeartPulse, Loader2 } from "lucide-react";

function endOfLocalDay(dateValue: string): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateValue);
  if (!match || dateValue > LATEST_FITNESS_DATE) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const localDate = new Date(year, month - 1, day, 23, 59, 59, 999);
  if (
    localDate.getFullYear() !== year ||
    localDate.getMonth() !== month - 1 ||
    localDate.getDate() !== day
  ) {
    return null;
  }
  return Math.min(localDate.getTime(), LATEST_FITNESS_MILLIS);
}

export function FitnessCard({ userId }: { userId: Id<"users"> }) {
  const t = useTranslations();
  const locale = useLocale();
  const dateLocale = locale === "fr" ? fr : enUS;
  const readiness = useQuery(api.fitness.getRiderReadiness, { userId });
  const clock = useLocalClock();
  const serverNow = readiness ? serverClockAt(readiness.serverNow, clock) : 0;
  const submitAnswers = useMutationWithFeedback(api.fitness.submitFitnessAnswers);
  const validateAssessment = useMutationWithFeedback(
    api.fitness.validateFitnessAssessment,
  );

  const [answers, setAnswers] = useState<Record<string, boolean>>({});
  const [validUntilDate, setValidUntilDate] = useState("");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [validating, setValidating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (readiness === undefined) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>{t("training.fitness.title")}</CardTitle>
        </CardHeader>
        <CardContent className="flex justify-center py-6">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </CardContent>
      </Card>
    );
  }

  const fitness = readiness?.fitness ?? null;
  const complete = CURRENT_QUESTIONNAIRE.questions.every(
    (question) => typeof answers[question.id] === "boolean",
  );
  const selectedValidUntil = endOfLocalDay(validUntilDate);
  const needsValidity = fitness?.outcome === "fit";
  const invalidValidity =
    needsValidity &&
    (selectedValidUntil === null || selectedValidUntil <= serverNow);
  const canValidate =
    readiness?.canValidate === true &&
    fitness !== null &&
    fitness.current &&
    !fitness.validated;
  const answersToSubmit: Record<string, boolean> = {};
  for (const question of CURRENT_QUESTIONNAIRE.questions) {
    const answer = answers[question.id];
    if (typeof answer === "boolean") answersToSubmit[question.id] = answer;
  }

  const displayDate = (timestamp: number): string => {
    const date = new Date(timestamp);
    return Number.isNaN(date.getTime())
      ? t("training.fitness.unknownDate")
      : format(date, "PPP", { locale: dateLocale });
  };

  let state: string;
  if (readiness === null) {
    state = t("training.fitness.unavailable");
  } else if (fitness === null) {
    state = t("training.fitness.noSubmission");
  } else if (!fitness.current) {
    state = t("training.fitness.outdated", {
      version: fitness.questionnaireVersion,
    });
  } else if (fitness.outcome === "needs_medical_opinion") {
    state = t("training.fitness.needsMedicalOpinion");
  } else if (fitness.outcome === "unfit") {
    state = t("training.fitness.unfit");
  } else if (!fitness.validated) {
    state = t("training.fitness.pending", {
      date: displayDate(fitness.submittedAt),
      name: fitness.submittedByName || t("training.fitness.unknownName"),
    });
  } else if (
    readiness.launchRefusal === "fitness_expired" ||
    (fitness.validUntil !== undefined &&
      (Number.isNaN(new Date(fitness.validUntil).getTime()) ||
        fitness.validUntil <= serverNow))
  ) {
    state = t("training.fitness.expired", {
      date: displayDate(fitness.validUntil ?? serverNow),
    });
  } else if (fitness.validUntil !== undefined) {
    state = t("training.fitness.fitUntil", {
      date: displayDate(fitness.validUntil),
    });
  } else {
    state = t("training.fitness.needsMedicalOpinion");
  }

  const handleSubmitAnswers = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!complete || submitting) return;
    setSubmitting(true);
    setError(null);
    const result = await submitAnswers(
      {
        userId,
        questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
        answers: answersToSubmit,
      },
      { success: t("training.fitness.submitted") },
    );
    if (!result.ok) setError(result.message);
    else setAnswers({});
    setSubmitting(false);
  };

  const handleValidate = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canValidate || !fitness || invalidValidity || validating) return;
    setValidating(true);
    setError(null);
    const result = await validateAssessment(
      {
        assessmentId: fitness.assessmentId,
        ...(needsValidity && selectedValidUntil !== null
          ? { validUntil: selectedValidUntil }
          : {}),
        ...(notes.trim() ? { notes: notes.trim() } : {}),
      },
      { success: t("training.fitness.validated") },
    );
    if (!result.ok) setError(result.message);
    setValidating(false);
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <HeartPulse className="h-4 w-4" />
          {t("training.fitness.title")}
        </CardTitle>
        <CardDescription>{t("training.fitness.description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-start gap-2 rounded-md border border-amber-500/50 bg-amber-50 p-3 text-sm text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <p>
            {t("training.fitness.provisional", {
              version: CURRENT_QUESTIONNAIRE.version,
            })}
          </p>
        </div>

        <div className="rounded-md bg-muted/40 p-3">
          <p className="text-sm font-medium">{t("training.fitness.status")}</p>
          <p className="text-sm text-muted-foreground">{state}</p>
        </div>

        {fitness && readiness?.canValidate && fitness.answers && (
          <div className="space-y-2 rounded-md border p-3">
            <p className="font-medium">{t("training.fitness.answersTitle")}</p>
            {!fitness.current && (
              <p className="text-sm text-muted-foreground">
                {t("training.fitness.answersOutdated", {
                  version: fitness.questionnaireVersion,
                })}
              </p>
            )}
            <ul className="space-y-2 text-sm">
              {Object.entries(fitness.answers).map(([questionId, answer]) => {
                const question = CURRENT_QUESTIONNAIRE.questions.find(
                  (question) => question.id === questionId,
                );
                return (
                  <li key={questionId}>
                    <span>
                      {question?.label[locale === "fr" ? "fr" : "en"] ?? questionId}
                    </span>
                    <span className="ml-2 font-medium">
                      {t(answer ? "training.fitness.yes" : "training.fitness.no")}
                    </span>
                  </li>
                );
              })}
            </ul>
            {fitness.notes && (
              <p className="text-sm">
                {t("training.fitness.reviewNotesValue", { notes: fitness.notes })}
              </p>
            )}
          </div>
        )}

        {error && (
          <p className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
            {error}
          </p>
        )}

        {readiness !== null && (
          <form onSubmit={handleSubmitAnswers} className="space-y-4">
            <h3 className="font-medium">
              {fitness
                ? t("training.fitness.update")
                : t("training.fitness.fill")}
            </h3>
            {fitness && (
              <p className="text-sm text-muted-foreground">
                {t("training.fitness.updateWarning")}
              </p>
            )}
            <div className="space-y-4">
              {CURRENT_QUESTIONNAIRE.questions.map((question, index) => (
                <fieldset key={question.id} className="space-y-2">
                  <legend className="text-sm font-medium">
                    {index + 1}. {question.label[locale === "fr" ? "fr" : "en"]}
                  </legend>
                  <div className="flex gap-2">
                    {([true, false] as const).map((value) => (
                      <Button
                        key={String(value)}
                        type="button"
                        variant={answers[question.id] === value ? "default" : "outline"}
                        aria-pressed={answers[question.id] === value}
                        onClick={() =>
                          setAnswers((current) => ({
                            ...current,
                            [question.id]: value,
                          }))
                        }
                      >
                        {t(value ? "training.fitness.yes" : "training.fitness.no")}
                      </Button>
                    ))}
                  </div>
                </fieldset>
              ))}
            </div>
            <Button type="submit" disabled={!complete || submitting}>
              {submitting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {fitness
                ? t("training.fitness.updateSubmit")
                : t("training.fitness.submit")}
            </Button>
          </form>
        )}

        {canValidate && fitness && (
          <form onSubmit={handleValidate} className="space-y-3 border-t pt-4">
            <h3 className="font-medium">{t("training.fitness.validate")}</h3>
            {needsValidity && (
              <div className="space-y-2">
                <Label htmlFor="fitness-valid-until">
                  {t("training.fitness.validUntil")}
                </Label>
                <Input
                  id="fitness-valid-until"
                  type="date"
                  max={LATEST_FITNESS_DATE}
                  required
                  aria-invalid={invalidValidity}
                  aria-describedby={
                    invalidValidity ? "fitness-valid-until-error" : undefined
                  }
                  value={validUntilDate}
                  onChange={(event) => setValidUntilDate(event.target.value)}
                />
                {invalidValidity && (
                  <p
                    id="fitness-valid-until-error"
                    className="text-xs text-destructive"
                  >
                    {t("training.fitness.validityRequired")}
                  </p>
                )}
              </div>
            )}
            <div className="space-y-2">
              <Label htmlFor="fitness-notes">
                {t("training.fitness.reviewNotes")}
              </Label>
              <Textarea
                id="fitness-notes"
                maxLength={1000}
                rows={3}
                value={notes}
                onChange={(event) => setNotes(event.target.value)}
              />
            </div>
            <Button type="submit" disabled={invalidValidity || validating}>
              {validating && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {t("training.fitness.validateSubmit")}
            </Button>
          </form>
        )}
      </CardContent>
    </Card>
  );
}

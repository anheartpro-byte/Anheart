"use client";

import { useState, type FormEvent, type ReactNode } from "react";
import { useQuery } from "convex/react";
import type { FunctionReturnType } from "convex/server";
import { useLocale, useTranslations } from "next-intl";
import { format } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import { Loader2, MessageSquareHeart } from "lucide-react";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import {
  FEEDBACK_CONTRACT,
  FREE_TEXT_MAX_LENGTH,
  type FeedbackComfort,
} from "@/convex/lib/feedback";
import { useMutationWithFeedback } from "@/hooks/use-mutation-with-feedback";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";

type Read = NonNullable<
  FunctionReturnType<typeof api.sessionFeedback.getSessionFeedback>
>;
export type SessionFeedback = NonNullable<Read["feedback"]>;
type Language = "fr" | "en";
type ScaleName = keyof typeof FEEDBACK_CONTRACT.scales;

const SCALES: ScaleName[] = ["nausea", "dizziness"];
const SCORE_OF: Record<ScaleName, "nauseaScore" | "dizzinessScore"> = {
  nausea: "nauseaScore",
  dizziness: "dizzinessScore",
};

/** "3 - Légère à modérée": the score and its label on the scale of the contract. */
function levelLabel(scale: ScaleName, score: number, language: Language) {
  const level = FEEDBACK_CONTRACT.scales[scale].levels.find(
    (candidate) => candidate.value === score,
  );
  return level === undefined
    ? String(score)
    : `${score} - ${level.label[language]}`;
}

function comfortLabel(comfort: FeedbackComfort, language: Language) {
  const item = FEEDBACK_CONTRACT.comfort.find((c) => c.value === comfort);
  return item === undefined ? comfort : item.label[language];
}

function useLanguage(): Language {
  return useLocale() === "fr" ? "fr" : "en";
}

/**
 * The rider's feedback as it was given, read only: both scores with their
 * label, the comfort, the comment, who entered it and when. Shared by the
 * card of the session page and the printed report.
 */
export function FeedbackReadout({ feedback }: { feedback: SessionFeedback }) {
  const t = useTranslations("sessionFeedback");
  const language = useLanguage();
  const dateLocale = language === "fr" ? fr : enUS;
  return (
    <dl className="grid grid-cols-2 gap-3 text-sm md:grid-cols-3 print:grid-cols-3">
      {SCALES.map((scale) => (
        <Fact
          key={scale}
          label={FEEDBACK_CONTRACT.scales[scale].title[language]}
        >
          {levelLabel(scale, feedback[SCORE_OF[scale]], language)}
        </Fact>
      ))}
      <Fact label={t("comfort")}>
        {comfortLabel(feedback.comfort, language)}
      </Fact>
      <Fact label={t("submittedBy")}>{t(`by.${feedback.submittedBy}`)}</Fact>
      <Fact label={t("submittedAt")}>
        {format(feedback.submittedAt, "PPp", { locale: dateLocale })}
      </Fact>
      {feedback.freeText !== undefined && (
        <div className="col-span-full">
          <dt className="text-muted-foreground">{t("freeText")}</dt>
          <dd className="whitespace-pre-wrap font-medium">
            {feedback.freeText}
          </dd>
        </div>
      )}
    </dl>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-medium">{children}</dd>
    </div>
  );
}

/**
 * The rider's feedback after a finished training session (ANH-146), on its
 * page. Everyone who may read the session reads it; only its rider gives or
 * changes it, until 24 h after the end (`editableUntil`), and the server
 * decides. A session that had no one on board takes none: no card.
 */
export function SessionFeedbackCard({
  sessionId,
}: {
  sessionId: Id<"sessions">;
}) {
  const t = useTranslations("sessionFeedback");
  const language = useLanguage();
  const read = useQuery(api.sessionFeedback.getSessionFeedback, { sessionId });
  // Read once: the server refuses a late write whatever this page believes.
  const [now] = useState(() => Date.now());

  if (read === undefined) return <Skeleton className="h-40 w-full" />;
  if (read === null || !read.eligible) return null;

  const open =
    read.canEdit && read.editableUntil !== null && now <= read.editableUntil;
  const dateLocale = language === "fr" ? fr : enUS;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <MessageSquareHeart className="h-4 w-4" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {read.feedback === null ? (
          <p className="text-sm text-muted-foreground">{t("notProvided")}</p>
        ) : (
          <FeedbackReadout feedback={read.feedback} />
        )}
        {open && read.editableUntil !== null ? (
          <>
            <p className="text-sm text-muted-foreground">
              {t("editableUntil", {
                date: format(read.editableUntil, "PPp", { locale: dateLocale }),
              })}
            </p>
            <FeedbackForm sessionId={sessionId} current={read.feedback} />
          </>
        ) : (
          read.canEdit && (
            <p className="text-sm text-muted-foreground">{t("windowClosed")}</p>
          )
        )}
      </CardContent>
    </Card>
  );
}

/** The rider's form: both scales, the comfort, an optional comment. */
function FeedbackForm({
  sessionId,
  current,
}: {
  sessionId: Id<"sessions">;
  current: SessionFeedback | null;
}) {
  const t = useTranslations("sessionFeedback");
  const language = useLanguage();
  const submit = useMutationWithFeedback(
    api.sessionFeedback.submitSessionFeedback,
  );
  const [scores, setScores] = useState<Record<ScaleName, string>>({
    nausea: current === null ? "" : String(current.nauseaScore),
    dizziness: current === null ? "" : String(current.dizzinessScore),
  });
  const [comfort, setComfort] = useState<string>(current?.comfort ?? "");
  const [freeText, setFreeText] = useState(current?.freeText ?? "");
  const [saving, setSaving] = useState(false);

  const complete =
    scores.nausea !== "" && scores.dizziness !== "" && comfort !== "";

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!complete || saving) return;
    setSaving(true);
    const text = freeText.trim();
    await submit(
      {
        sessionId,
        nauseaScore: Number(scores.nausea),
        dizzinessScore: Number(scores.dizziness),
        comfort,
        ...(text === "" ? {} : { freeText: text }),
      },
      { success: t("saved") },
    );
    // A refusal is shown by the hook; the form keeps what was entered.
    setSaving(false);
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4 border-t pt-4">
      <div className="grid gap-4 md:grid-cols-3">
        {SCALES.map((scale) => (
          <div key={scale} className="space-y-2">
            <Label htmlFor={`feedback-${scale}`}>
              {FEEDBACK_CONTRACT.scales[scale].title[language]}
            </Label>
            <Select
              value={scores[scale]}
              onValueChange={(value) =>
                setScores((previous) => ({ ...previous, [scale]: value }))
              }
            >
              <SelectTrigger id={`feedback-${scale}`} className="w-full">
                <SelectValue placeholder={t("choose")} />
              </SelectTrigger>
              <SelectContent>
                {FEEDBACK_CONTRACT.scales[scale].levels.map((level) => (
                  <SelectItem key={level.value} value={String(level.value)}>
                    {levelLabel(scale, level.value, language)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        ))}
        <div className="space-y-2">
          <Label htmlFor="feedback-comfort">{t("comfort")}</Label>
          <Select value={comfort} onValueChange={setComfort}>
            <SelectTrigger id="feedback-comfort" className="w-full">
              <SelectValue placeholder={t("choose")} />
            </SelectTrigger>
            <SelectContent>
              {FEEDBACK_CONTRACT.comfort.map((item) => (
                <SelectItem key={item.value} value={item.value}>
                  {item.label[language]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
      <div className="space-y-2">
        <Label htmlFor="feedback-text">{t("freeText")}</Label>
        <Textarea
          id="feedback-text"
          maxLength={FREE_TEXT_MAX_LENGTH}
          value={freeText}
          aria-describedby="feedback-text-count"
          onChange={(event) => setFreeText(event.target.value)}
        />
        <p id="feedback-text-count" className="text-xs text-muted-foreground">
          {t("characters", {
            count: freeText.length,
            max: FREE_TEXT_MAX_LENGTH,
          })}
        </p>
      </div>
      <Button type="submit" disabled={!complete || saving}>
        {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
        {t("save")}
      </Button>
    </form>
  );
}

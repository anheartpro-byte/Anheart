/**
 * The rider's feedback after a session (ANH-146): two scales from 0 to 10
 * (nausea, discomfort or dizziness), a comfort, an optional free text.
 *
 * The scale, its labels and its bounds are read from `contracts/feedback.json`,
 * their single source, for Convex and the site alike. A file that is not of the
 * shape below stops this module at load: a deployment error, never a guess.
 * The comfort values and who may submit are also literals here, because the
 * schema's validators need them; the load check holds them equal to the file.
 *
 * Health data: what this module validates is stored only in the table
 * `session_feedback` (`convex/sessionFeedback.ts`). Nothing here logs or
 * words a value.
 */
import { v } from "convex/values";
import { z } from "zod";
import contract from "../../contracts/feedback.json";

export const FEEDBACK_COMFORTS = ["good", "acceptable", "bad"] as const;
export type FeedbackComfort = (typeof FEEDBACK_COMFORTS)[number];

/** `rider` from the site, `operator` from the machine's console. */
export const FEEDBACK_SUBMITTERS = ["rider", "operator"] as const;
export type FeedbackSubmitter = (typeof FEEDBACK_SUBMITTERS)[number];

export const feedbackComfortValidator = v.union(
  ...FEEDBACK_COMFORTS.map((comfort) => v.literal(comfort)),
);
export const feedbackSubmitterValidator = v.union(
  ...FEEDBACK_SUBMITTERS.map((submitter) => v.literal(submitter)),
);

const label = z.strictObject({
  fr: z.string().trim().min(1),
  en: z.string().trim().min(1),
});

const scaleSchema = z
  .strictObject({
    title: label,
    min: z.int(),
    max: z.int(),
    levels: z.array(z.strictObject({ value: z.int(), label })),
  })
  .refine(
    (scale) =>
      scale.min < scale.max &&
      scale.levels.length === scale.max - scale.min + 1 &&
      scale.levels.every((level, index) => level.value === scale.min + index),
    "One label per level, from min to max in order",
  );

const contractSchema = z.strictObject({
  description: z.string(),
  version: z.string().trim().min(1),
  provisional: z.boolean(),
  medicalStatus: z.string().trim().min(1),
  scales: z.strictObject({ nausea: scaleSchema, dizziness: scaleSchema }),
  comfort: z
    .array(z.strictObject({ value: z.enum(FEEDBACK_COMFORTS), label }))
    .refine(
      (list) =>
        list.length === FEEDBACK_COMFORTS.length &&
        list.every((item, index) => item.value === FEEDBACK_COMFORTS[index]),
      "The comfort values, each once, in order",
    ),
  freeTextMaxLength: z.int().positive(),
  editWindowHours: z.int().positive(),
  submittedBy: z.tuple([z.literal("rider"), z.literal("operator")]),
});

export type FeedbackContract = z.infer<typeof contractSchema>;

/** The contract in `raw`, checked; null when it is not of the expected shape. */
export function parseFeedbackContract(raw: unknown): FeedbackContract | null {
  const parsed = contractSchema.safeParse(raw);
  return parsed.success ? parsed.data : null;
}

const loaded = parseFeedbackContract(contract);
if (loaded === null) {
  throw new Error("Invalid feedback contract: contracts/feedback.json");
}

export const FEEDBACK_CONTRACT: FeedbackContract = loaded;
export const FREE_TEXT_MAX_LENGTH = loaded.freeTextMaxLength;
/** How long after the end of its session a feedback can be given or changed. */
export const EDIT_WINDOW_MS = loaded.editWindowHours * 60 * 60 * 1000;

/** A feedback as stored: whole scores of the scale, a comfort, a trimmed text or none. */
export type Feedback = {
  nauseaScore: number;
  dizzinessScore: number;
  comfort: FeedbackComfort;
  freeText?: string;
};

const scoreOf = (scale: FeedbackContract["scales"]["nausea"]) =>
  z.int().min(scale.min).max(scale.max);

// Trimmed before its length is checked: the bound is on what is stored.
const feedbackSchema = z.strictObject({
  nauseaScore: scoreOf(loaded.scales.nausea),
  dizzinessScore: scoreOf(loaded.scales.dizziness),
  comfort: z.enum(FEEDBACK_COMFORTS),
  freeText: z.string().trim().max(FREE_TEXT_MAX_LENGTH).optional(),
});

/**
 * The feedback in `input`, as it is stored, or null when anything is not of
 * the scale: a key it does not know, a score that is not a whole number of
 * the scale, a comfort not of the list, a text over the limit once trimmed.
 * A text empty or blank once trimmed is no text.
 */
export function validateFeedback(input: unknown): Feedback | null {
  const parsed = feedbackSchema.safeParse(input);
  if (!parsed.success) return null;
  const { freeText, ...rest } = parsed.data;
  return freeText ? { ...rest, freeText } : rest;
}

/**
 * The feedback a console's `feedback` event carries in its `detail`: strict
 * JSON of an object with the keys of `Feedback` and no other. Null for
 * anything else, without saying what.
 */
export function parseMachineFeedback(detail: string): Feedback | null {
  let raw: unknown;
  try {
    raw = JSON.parse(detail);
  } catch {
    return null;
  }
  return validateFeedback(raw);
}

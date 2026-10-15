import questionnaireContract from "../contracts/fitness-questionnaire.json";

export type FitnessOutcome = "fit" | "needs_medical_opinion" | "unfit";

export type Question = Readonly<{
  id: string;
  type: "yes_no";
  label: Readonly<{ fr: string; en: string }>;
  outcomes: Readonly<{ yes: FitnessOutcome; no: FitnessOutcome }>;
}>;

export type Questionnaire = Readonly<{
  version: string;
  provisional: boolean;
  medicalStatus: string;
  description: string;
  validity: Readonly<{ maxDays: number | null; note: string }>;
  outcomes: readonly FitnessOutcome[];
  severity: readonly FitnessOutcome[];
  questions: readonly Question[];
  rule: string;
}>;

export type FitnessAnswers = Readonly<Record<string, boolean>>;

export type ParseQuestionnaireResult =
  | Readonly<{ ok: true; questionnaire: Questionnaire }>
  | Readonly<{ ok: false; reason: string }>;

export type ComputeOutcomeResult =
  | Readonly<{ ok: true; outcome: FitnessOutcome }>
  | Readonly<{
      ok: false;
      reason:
        | "version_mismatch"
        | "not_an_object"
        | "missing_answer"
        | "unknown_question"
        | "invalid_answer";
    }>;

const FITNESS_OUTCOMES: readonly FitnessOutcome[] = [
  "fit",
  "needs_medical_opinion",
  "unfit",
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isFitnessOutcome(value: unknown): value is FitnessOutcome {
  return FITNESS_OUTCOMES.some((outcome) => outcome === value);
}

function parseQuestion(raw: unknown):
  | Readonly<{ ok: true; question: Question }>
  | Readonly<{ ok: false; reason: string }> {
  if (!isRecord(raw)) return { ok: false, reason: "invalid_question" };

  const { id, type, label, outcomes } = raw;
  if (typeof id !== "string" || !/^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/.test(id)) {
    return { ok: false, reason: "invalid_question_id" };
  }
  if (type !== "yes_no") {
    return { ok: false, reason: "unknown_question_type" };
  }
  if (!isRecord(label) || typeof label.fr !== "string" || !label.fr.trim() || typeof label.en !== "string" || !label.en.trim()) {
    return { ok: false, reason: "invalid_label" };
  }
  if (
    !isRecord(outcomes) ||
    !isFitnessOutcome(outcomes.yes) ||
    !isFitnessOutcome(outcomes.no)
  ) {
    return { ok: false, reason: "unknown_outcome" };
  }

  return {
    ok: true,
    question: {
      id,
      type,
      label: { fr: label.fr, en: label.en },
      outcomes: { yes: outcomes.yes, no: outcomes.no },
    },
  };
}

export function parseQuestionnaire(raw: unknown): ParseQuestionnaireResult {
  if (!isRecord(raw)) return { ok: false, reason: "not_an_object" };

  const { version, provisional, medicalStatus, description, validity, outcomes, severity, questions, rule } = raw;
  if (typeof version !== "string" || version.trim() === "") {
    return { ok: false, reason: "invalid_version" };
  }
  if (
    typeof provisional !== "boolean" ||
    typeof medicalStatus !== "string" ||
    typeof description !== "string" ||
    typeof rule !== "string"
  ) {
    return { ok: false, reason: "invalid_questionnaire_metadata" };
  }
  if (!isRecord(validity) || typeof validity.note !== "string") {
    return { ok: false, reason: "invalid_validity" };
  }
  if (
    validity.maxDays !== null &&
    (!Number.isSafeInteger(validity.maxDays) || (validity.maxDays as number) <= 0)
  ) {
    return { ok: false, reason: "invalid_max_days" };
  }
  if (!Array.isArray(outcomes) || outcomes.length === 0 || !outcomes.every(isFitnessOutcome)) {
    return { ok: false, reason: "unknown_outcome" };
  }
  const uniqueOutcomes = [...new Set(outcomes)];
  if (uniqueOutcomes.length !== outcomes.length) {
    return { ok: false, reason: "invalid_outcomes" };
  }
  if (
    !Array.isArray(severity) ||
    severity.length !== uniqueOutcomes.length ||
    !severity.every((item, index) => item === FITNESS_OUTCOMES[index]) ||
    !uniqueOutcomes.every((item) => severity.includes(item))
  ) {
    return { ok: false, reason: "invalid_severity" };
  }
  if (!Array.isArray(questions) || questions.length === 0) {
    return { ok: false, reason: "invalid_questions" };
  }

  const parsedQuestions: Question[] = [];
  const questionIds = new Set<string>();
  for (const rawQuestion of questions) {
    const parsed = parseQuestion(rawQuestion);
    if (!parsed.ok) return parsed;
    if (questionIds.has(parsed.question.id)) {
      return { ok: false, reason: "duplicate_question_id" };
    }
    if (
      !uniqueOutcomes.includes(parsed.question.outcomes.yes) ||
      !uniqueOutcomes.includes(parsed.question.outcomes.no)
    ) {
      return { ok: false, reason: "unknown_outcome" };
    }
    questionIds.add(parsed.question.id);
    parsedQuestions.push(parsed.question);
  }

  return {
    ok: true,
    questionnaire: {
      version,
      provisional,
      medicalStatus,
      description,
      validity: {
        maxDays: validity.maxDays as number | null,
        note: validity.note,
      },
      outcomes: [...uniqueOutcomes],
      severity: [...severity] as FitnessOutcome[],
      questions: parsedQuestions,
      rule,
    },
  };
}

const parsedCurrentQuestionnaire = parseQuestionnaire(questionnaireContract);
if (!parsedCurrentQuestionnaire.ok) {
  throw new Error(`Invalid fitness questionnaire contract: ${parsedCurrentQuestionnaire.reason}`);
}

export const CURRENT_QUESTIONNAIRE = parsedCurrentQuestionnaire.questionnaire;

export function computeOutcome(
  questionnaire: Questionnaire,
  version: string,
  answers: unknown,
): ComputeOutcomeResult {
  if (version !== questionnaire.version) return { ok: false, reason: "version_mismatch" };
  if (!isRecord(answers)) return { ok: false, reason: "not_an_object" };

  const questionIds = new Set(questionnaire.questions.map((question) => question.id));
  if (Object.keys(answers).some((id) => !questionIds.has(id))) {
    return { ok: false, reason: "unknown_question" };
  }
  for (const question of questionnaire.questions) {
    if (!Object.prototype.hasOwnProperty.call(answers, question.id)) {
      return { ok: false, reason: "missing_answer" };
    }
  }

  let mostSevere: FitnessOutcome = questionnaire.severity[0];
  for (const question of questionnaire.questions) {
    const answer = answers[question.id];
    if (typeof answer !== "boolean") return { ok: false, reason: "invalid_answer" };

    const outcome = question.outcomes[answer ? "yes" : "no"];
    if (questionnaire.severity.indexOf(outcome) > questionnaire.severity.indexOf(mostSevere)) {
      mostSevere = outcome;
    }
  }

  return { ok: true, outcome: mostSevere };
}

export function maxValidUntil(questionnaire: Questionnaire, validatedAt: number): number | null {
  if (questionnaire.validity.maxDays === null) return null;
  return validatedAt + questionnaire.validity.maxDays * 24 * 60 * 60 * 1000;
}

import casesContract from "../contracts/fitness-questionnaire-cases.json";
import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import {
  CURRENT_QUESTIONNAIRE,
  LATEST_FITNESS_DATE,
  LATEST_FITNESS_MILLIS,
  computeOutcome,
  maxValidUntil,
  parseQuestionnaire,
  type FitnessOutcome,
  type Questionnaire,
} from "./fitnessQuestionnaire";

type QuestionnaireCase = {
  name: string;
  version: string;
  answers: unknown;
  expected: { outcome: FitnessOutcome } | { refused: string };
};

const questionnaireCases = casesContract.cases as QuestionnaireCase[];
const questionnaireWithoutVersion = Object.fromEntries(
  Object.entries(CURRENT_QUESTIONNAIRE).filter(([key]) => key !== "version"),
);

function syntheticQuestionnaire(): Questionnaire {
  return {
    version: "test-1",
    provisional: true,
    medicalStatus: "[MED] test",
    description: "test",
    validity: { maxDays: 2, note: "test" },
    outcomes: ["fit", "needs_medical_opinion", "unfit"],
    severity: ["fit", "needs_medical_opinion", "unfit"],
    questions: [
      {
        id: "first",
        type: "yes_no",
        label: { fr: "[MED] première", en: "[MED] first" },
        outcomes: { yes: "needs_medical_opinion", no: "fit" },
      },
      {
        id: "second",
        type: "yes_no",
        label: { fr: "[MED] deuxième", en: "[MED] second" },
        outcomes: { yes: "unfit", no: "fit" },
      },
      {
        id: "third",
        type: "yes_no",
        label: { fr: "[MED] troisième", en: "[MED] third" },
        outcomes: { yes: "fit", no: "fit" },
      },
    ],
    rule: "test",
  };
}

function currentAnswers(answer: boolean): Record<string, boolean> {
  return Object.fromEntries(
    CURRENT_QUESTIONNAIRE.questions.map((question) => [question.id, answer]),
  );
}

describe("versioned fitness questionnaire", () => {
  it("shares the console's technical fitness expiry limit", () => {
    const roster = readFileSync(
      new URL("../raspberry-pi/src/roster.py", import.meta.url),
      "utf8",
    );
    const millis = roster.match(
      /^_LATEST_FITNESS_MILLIS: Final\[int\] = ([\d_]+)/m,
    )?.[1];
    expect(Number(millis?.replaceAll("_", ""))).toBe(LATEST_FITNESS_MILLIS);
    expect(LATEST_FITNESS_MILLIS).toBe(Date.parse("2101-01-01T00:00:00Z"));
    expect(LATEST_FITNESS_DATE).toBe("2101-01-01");
  });

  it("loads and validates the provisional 0.1.0 contract", () => {
    expect(CURRENT_QUESTIONNAIRE.version).toBe("0.1.0");
    expect(CURRENT_QUESTIONNAIRE.provisional).toBe(true);
    expect(CURRENT_QUESTIONNAIRE.questions).toHaveLength(9);
    expect(CURRENT_QUESTIONNAIRE.severity).toEqual([
      "fit",
      "needs_medical_opinion",
      "unfit",
    ]);
  });

  it.each([
    ["not an object", null, "not_an_object"],
    ["missing version", questionnaireWithoutVersion, "invalid_version"],
    [
      "missing question id",
      {
        ...CURRENT_QUESTIONNAIRE,
        questions: [{ ...CURRENT_QUESTIONNAIRE.questions[0], id: "" }],
      },
      "invalid_question_id",
    ],
    [
      "duplicate question id",
      {
        ...CURRENT_QUESTIONNAIRE,
        questions: [
          CURRENT_QUESTIONNAIRE.questions[0],
          CURRENT_QUESTIONNAIRE.questions[0],
        ],
      },
      "duplicate_question_id",
    ],
    [
      "unknown question type",
      {
        ...CURRENT_QUESTIONNAIRE,
        questions: [
          { ...CURRENT_QUESTIONNAIRE.questions[0], type: "multiple_choice" },
        ],
      },
      "unknown_question_type",
    ],
    [
      "unknown outcome",
      {
        ...CURRENT_QUESTIONNAIRE,
        outcomes: ["fit", "needs_medical_opinion", "unknown"],
      },
      "unknown_outcome",
    ],
    [
      "missing French label",
      {
        ...CURRENT_QUESTIONNAIRE,
        questions: [
          {
            ...CURRENT_QUESTIONNAIRE.questions[0],
            label: { en: "[MED] label" },
          },
        ],
      },
      "invalid_label",
    ],
    [
      "missing English label",
      {
        ...CURRENT_QUESTIONNAIRE,
        questions: [
          {
            ...CURRENT_QUESTIONNAIRE.questions[0],
            label: { fr: "[MED] libellé" },
          },
        ],
      },
      "invalid_label",
    ],
    [
      "incoherent severity",
      {
        ...CURRENT_QUESTIONNAIRE,
        severity: ["fit", "unfit", "needs_medical_opinion"],
      },
      "invalid_severity",
    ],
    [
      "zero maxDays",
      {
        ...CURRENT_QUESTIONNAIRE,
        validity: { ...CURRENT_QUESTIONNAIRE.validity, maxDays: 0 },
      },
      "invalid_max_days",
    ],
    [
      "fractional maxDays",
      {
        ...CURRENT_QUESTIONNAIRE,
        validity: { ...CURRENT_QUESTIONNAIRE.validity, maxDays: 1.5 },
      },
      "invalid_max_days",
    ],
    [
      "string maxDays",
      {
        ...CURRENT_QUESTIONNAIRE,
        validity: { ...CURRENT_QUESTIONNAIRE.validity, maxDays: "30" },
      },
      "invalid_max_days",
    ],
  ])("refuses %s with its reason", (_name, raw, reason) => {
    expect(parseQuestionnaire(raw)).toEqual({ ok: false, reason });
  });

  it.each(questionnaireCases)(
    "applies reference case: $name",
    ({ version, answers, expected }) => {
      const result = computeOutcome(CURRENT_QUESTIONNAIRE, version, answers);
      if ("outcome" in expected) {
        expect(result).toEqual({ ok: true, outcome: expected.outcome });
      } else {
        expect(result).toEqual({ ok: false, reason: expected.refused });
      }
    },
  );

  it("has a yes reference case for every current question", () => {
    for (const question of CURRENT_QUESTIONNAIRE.questions) {
      expect(
        questionnaireCases.some(
          (referenceCase) =>
            typeof referenceCase.answers === "object" &&
            referenceCase.answers !== null &&
            !Array.isArray(referenceCase.answers) &&
            (referenceCase.answers as Record<string, unknown>)[question.id] ===
              true,
        ),
        question.id,
      ).toBe(true);
    }
  });

  it("returns the most severe answer regardless of key order and preserves inputs", () => {
    const questionnaire = syntheticQuestionnaire();
    const answers = { third: false, second: true, first: true };
    const questionnaireBefore = structuredClone(questionnaire);
    const answersBefore = structuredClone(answers);
    const reorderedAnswers = { first: true, second: true, third: false };

    expect(computeOutcome(questionnaire, "test-1", answers)).toEqual({
      ok: true,
      outcome: "unfit",
    });
    expect(computeOutcome(questionnaire, "test-1", reorderedAnswers)).toEqual({
      ok: true,
      outcome: "unfit",
    });
    expect(computeOutcome(questionnaire, "test-1", answers)).toEqual(
      computeOutcome(questionnaire, "test-1", answers),
    );
    expect(questionnaire).toEqual(questionnaireBefore);
    expect(answers).toEqual(answersBefore);
  });

  it("does not read the system clock when computing an outcome", () => {
    const dateNowSpy = vi.spyOn(Date, "now");
    try {
      const result = computeOutcome(
        CURRENT_QUESTIONNAIRE,
        "0.1.0",
        currentAnswers(false),
      );
      expect(dateNowSpy).not.toHaveBeenCalled();
      expect(result).toEqual({ ok: true, outcome: "fit" });
    } finally {
      dateNowSpy.mockRestore();
    }
  });

  it("calculates fixed validity only when maxDays is set", () => {
    const questionnaire = syntheticQuestionnaire();
    const validatedAt = 1_000_000;
    expect(maxValidUntil(questionnaire, validatedAt)).toBe(
      validatedAt + 2 * 24 * 60 * 60 * 1000,
    );
    expect(
      maxValidUntil(
        {
          ...questionnaire,
          validity: { ...questionnaire.validity, maxDays: null },
        },
        validatedAt,
      ),
    ).toBeNull();
  });

  it.each([
    ["version_mismatch", "test-2", currentAnswers(false)],
    ["not_an_object", "0.1.0", null],
    ["not_an_object", "0.1.0", []],
    ["missing_answer", "0.1.0", { heart_condition: false }],
    ["unknown_question", "0.1.0", { ...currentAnswers(false), extra: false }],
    [
      "invalid_answer",
      "0.1.0",
      { ...currentAnswers(false), heart_condition: "yes" },
    ],
  ])("returns refusal %s", (reason, version, answers) => {
    expect(computeOutcome(CURRENT_QUESTIONNAIRE, version, answers)).toEqual({
      ok: false,
      reason,
    });
  });
});

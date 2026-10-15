import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FunctionReturnType } from "convex/server";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import {
  click,
  messages,
  render,
  submit,
  type,
} from "@/test-support/render";
import { CURRENT_QUESTIONNAIRE } from "@/lib/fitnessQuestionnaire";
import { dismissFeedback, getFeedbackToasts } from "@/lib/feedback";
import { answer, mutation, mutationCalls, resetConvex } from "@/test-support/convex";
import { FitnessCard } from "./FitnessCard";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/convex")).convexReact,
);

const fr = messages.fr;
const userId = "rider-1" as Id<"users">;
const READINESS = "fitness:getRiderReadiness";
const SUBMIT = "fitness:submitFitnessAnswers";
const VALIDATE = "fitness:validateFitnessAssessment";
type Readiness = NonNullable<
  FunctionReturnType<typeof api.fitness.getRiderReadiness>
>;
type Fitness = NonNullable<Readiness["fitness"]>;
type ReadinessOverrides = Omit<Partial<Readiness>, "consent"> & {
  consent?: Partial<Readiness["consent"]>;
};

const SERVER_NOW = new Date(2026, 9, 15, 12).getTime();
const questionAnswers = Object.fromEntries(
  CURRENT_QUESTIONNAIRE.questions.map((question) => [question.id, true]),
);

function readiness(overrides: ReadinessOverrides = {}): Readiness {
  const { consent, ...rest } = overrides;
  return {
    serverNow: SERVER_NOW,
    questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
    consentTextVersion: "0.1.0",
    fitness: null,
    launchRefusal: "rider_not_fit",
    canValidate: false,
    ...rest,
    consent: {
      active: null,
      lastWithdrawnAt: undefined,
      ...consent,
    },
  };
}

function assessment(overrides: Partial<Fitness> = {}): Fitness {
  return {
    assessmentId: "assessment-1" as Id<"fitness_assessments">,
    questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
    outcome: "fit",
    submittedAt: SERVER_NOW - 86_400_000,
    submittedByName: "Paul Patient",
    validated: false,
    current: true,
    ...overrides,
  };
}

function show(value: Readiness | null) {
  answer(READINESS, value);
  return render(<FitnessCard userId={userId} />);
}

beforeEach(() => {
  resetConvex();
  mutation(SUBMIT).mockResolvedValue({ assessmentId: "assessment-1", outcome: "fit" });
  mutation(VALIDATE).mockResolvedValue(null);
});

afterEach(() => {
  vi.useRealTimers();
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
});

describe("FitnessCard status", () => {
  it("shows the provisional version and an empty state before the first submission", () => {
    const screen = show(readiness());

    expect(screen.text()).toContain(
      fr.training.fitness.provisional.replace(
        "{version}",
        CURRENT_QUESTIONNAIRE.version,
      ),
    );
    expect(screen.text()).toContain(fr.training.fitness.noSubmission);
  });

  it("uses the English labels and messages for an English-speaking passenger", () => {
    answer(READINESS, readiness());
    const screen = render(<FitnessCard userId={userId} />, { locale: "en" });

    expect(screen.text()).toContain(messages.en.training.fitness.noSubmission);
    expect(screen.text()).toContain(
      CURRENT_QUESTIONNAIRE.questions[0].label.en,
    );
    expect(screen.text()).not.toContain(
      CURRENT_QUESTIONNAIRE.questions[0].label.fr,
    );
  });

  it("shows a pending submission with its date and submitter", () => {
    const screen = show(readiness({ fitness: assessment() }));

    expect(screen.text()).toContain("à valider");
    expect(screen.text()).toContain("Paul Patient");
  });

  it("shows a valid assessment until its server supplied date", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2030, 0, 1));
    const until = new Date(2026, 10, 4, 23, 59, 59, 999).getTime();
    const screen = show(
      readiness({
        fitness: assessment({ validated: true, validUntil: until }),
        launchRefusal: null,
      }),
    );

    expect(screen.text()).toContain(fr.training.fitness.fitUntil.split("{")[0]);
    expect(screen.text()).not.toContain(fr.training.fitness.expired.split("{")[0]);
  });

  it("judges expiry using the readiness server clock", () => {
    const screen = show(
      readiness({
        fitness: assessment({
          validated: true,
          validUntil: SERVER_NOW - 1,
        }),
        launchRefusal: "fitness_expired",
      }),
    );

    expect(screen.text()).toContain(fr.training.fitness.expired.split("{")[0]);
  });

  it.each([
    ["medical opinion", "needs_medical_opinion", fr.training.fitness.needsMedicalOpinion],
    ["unfit", "unfit", fr.training.fitness.unfit],
  ] as const)("shows the %s outcome", (_name, outcome, expected) => {
    const screen = show(
      readiness({
        fitness: assessment({ outcome, validated: true }),
        launchRefusal: "rider_not_fit",
      }),
    );

    expect(screen.text()).toContain(expected);
  });

  it("asks the passenger to repeat a submission from an older questionnaire version", () => {
    const screen = show(
      readiness({
        canValidate: true,
        fitness: assessment({ questionnaireVersion: "0.0.9", current: false }),
      }),
    );

    expect(screen.text()).toContain("0.0.9");
    expect(screen.text()).toContain(fr.training.fitness.outdated.split("{")[0]);
    expect(screen.hasButton(fr.training.fitness.validateSubmit)).toBe(false);
  });

  it("renders answers and review notes only when the server returns them to a manager", () => {
    const hidden = show(
      readiness({
        fitness: assessment({ answers: questionAnswers, notes: "À vérifier" }),
      }),
    );
    expect(hidden.text()).not.toContain(fr.training.fitness.answersTitle);
    expect(hidden.text()).not.toContain("À vérifier");
    hidden.unmount();

    const visible = show(
      readiness({
        canValidate: true,
        fitness: assessment({ answers: questionAnswers, notes: "À vérifier" }),
      }),
    );
    expect(visible.text()).toContain(fr.training.fitness.answersTitle);
    expect(visible.text()).toContain(CURRENT_QUESTIONNAIRE.questions[0].label.fr);
    expect(visible.text()).toContain("À vérifier");
  });
});

describe("FitnessCard questionnaire and validation", () => {
  it("does not send an incomplete questionnaire, then sends every required answer and the rider id", async () => {
    const screen = show(readiness());
    expect(screen.button(fr.training.fitness.submit).hasAttribute("disabled")).toBe(true);
    await submit(screen.form());
    expect(mutationCalls()).toEqual({});

    const choices = screen.all(
      (element) => element.localName === "button" && element.getAttribute("aria-pressed") !== null,
    );
    for (const index of CURRENT_QUESTIONNAIRE.questions.map((_, i) => i * 2)) {
      await click(choices[index]);
    }
    await submit(screen.form());

    expect(mutationCalls()[SUBMIT]).toEqual([
      [
        {
          userId,
          questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
          answers: questionAnswers,
        },
      ],
    ]);
  });

  it("offers the update wording and sends the new submission for an existing assessment", async () => {
    const screen = show(readiness({ fitness: assessment({ validated: true }) }));

    expect(screen.text()).toContain(fr.training.fitness.update);
    const choices = screen.all(
      (element) => element.localName === "button" && element.getAttribute("aria-pressed") !== null,
    );
    for (const index of CURRENT_QUESTIONNAIRE.questions.map((_, i) => i * 2)) {
      await click(choices[index]);
    }
    await submit(screen.form());

    expect(mutationCalls()[SUBMIT]).toEqual([
      [
        {
          userId,
          questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
          answers: questionAnswers,
        },
      ],
    ]);
  });

  it("requires a future end-of-local-day date to validate a fit assessment", async () => {
    const screen = show(
      readiness({ canValidate: true, fitness: assessment() }),
    );
    expect(screen.button(fr.training.fitness.validateSubmit).hasAttribute("disabled")).toBe(true);
    const forms = screen.all((element) => element.localName === "form");
    await submit(forms[1]);
    expect(mutationCalls()).toEqual({});

    await type(screen.field("fitness-valid-until"), "2026-10-14");
    expect(screen.button(fr.training.fitness.validateSubmit).hasAttribute("disabled")).toBe(true);
    await submit(forms[1]);
    expect(mutationCalls()).toEqual({});

    await type(screen.field("fitness-valid-until"), "2026-10-20");
    await submit(forms[1]);
    expect(mutationCalls()[VALIDATE]).toEqual([
      [
        {
          assessmentId: "assessment-1",
          validUntil: new Date(2026, 9, 20, 23, 59, 59, 999).getTime(),
        },
      ],
    ]);
  });

  it("validates other outcomes without a validity date", async () => {
    const screen = show(
      readiness({
        canValidate: true,
        fitness: assessment({ outcome: "needs_medical_opinion" }),
      }),
    );
    expect(screen.all((element) => element.id === "fitness-valid-until")).toHaveLength(0);
    const forms = screen.all((element) => element.localName === "form");
    await submit(forms[1]);

    expect(mutationCalls()[VALIDATE]).toEqual([[{ assessmentId: "assessment-1" }]]);
  });
});

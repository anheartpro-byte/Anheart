import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { CONSENT_TEXT_VERSION } from "../lib/consentText";
import { CURRENT_QUESTIONNAIRE } from "../lib/fitnessQuestionnaire";
import { activeConsent, riderFitness, riderLaunchRefusal } from "./lib/fitness";
import {
  as,
  makeRiderReady,
  modules,
  NOW,
  organizationClaims,
  seedWorld,
} from "./test.setup";
import type { FitnessLaunchRefusal } from "./lib/fitness";

type World = Awaited<ReturnType<typeof seedWorld>>;

type AssessmentOptions = Readonly<{
  userId?: Id<"users">;
  organizationId?: Id<"organizations">;
  questionnaireVersion?: string;
  outcome?: "fit" | "unfit" | "needs_medical_opinion";
  validated?: boolean;
  validUntil?: number;
}>;

type LaunchCase = Readonly<{
  name: string;
  refusal: FitnessLaunchRefusal | null;
  prepare: (w: World) => Promise<void>;
}>;

function fitAnswers(): Record<string, boolean> {
  return Object.fromEntries(
    CURRENT_QUESTIONNAIRE.questions.map((question) => [
      question.id,
      question.outcomes.yes === "fit",
    ]),
  );
}

async function insertAssessment(
  w: World,
  options: AssessmentOptions = {},
): Promise<Id<"fitness_assessments">> {
  const userId = options.userId ?? w.patient;
  const organizationId = options.organizationId ?? w.orgA;
  return await w.t.run((ctx) =>
    ctx.db.insert("fitness_assessments", {
      userId,
      organizationId,
      questionnaireVersion:
        options.questionnaireVersion ?? CURRENT_QUESTIONNAIRE.version,
      answers: fitAnswers(),
      outcome: options.outcome ?? "fit",
      submittedBy: userId,
      submittedAt: NOW,
      ...(options.validated
        ? { validatedBy: w.manager, validatedAt: NOW }
        : {}),
      ...(options.validUntil === undefined
        ? {}
        : { validUntil: options.validUntil }),
    }),
  );
}

async function insertConsent(
  w: World,
  options: Readonly<{ textVersion?: string; withdrawn?: boolean }> = {},
) {
  return await w.t.run((ctx) =>
    ctx.db.insert("consents", {
      userId: w.patient,
      organizationId: w.orgA,
      textVersion: options.textVersion ?? CONSENT_TEXT_VERSION,
      signedAt: NOW,
      signedVia: "site",
      ...(options.withdrawn
        ? { withdrawnAt: NOW, withdrawnBy: w.patient }
        : {}),
    }),
  );
}

async function expectCode(operation: Promise<unknown>, code: string) {
  await expect(operation).rejects.toMatchObject({ data: code });
}

function launch(w: World, actor: "patient" | "manager", userId?: Id<"users">) {
  return as(w.t, actor).mutation(api.training.launchAutoSession, {
    machineId: w.machine,
    profileId: "p1",
    ...(userId === undefined ? {} : { userId }),
  });
}

const launchCases: LaunchCase[] = [
  {
    name: "no assessment",
    refusal: "rider_not_fit",
    prepare: async () => undefined,
  },
  {
    name: "unvalidated assessment",
    refusal: "rider_not_fit",
    prepare: async (w) => {
      await insertAssessment(w, { validUntil: NOW + 60_000 });
    },
  },
  {
    name: "validated needs-medical-opinion assessment",
    refusal: "rider_not_fit",
    prepare: async (w) => {
      await insertAssessment(w, {
        outcome: "needs_medical_opinion",
        validated: true,
      });
    },
  },
  {
    name: "validated unfit assessment",
    refusal: "rider_not_fit",
    prepare: async (w) => {
      await insertAssessment(w, { outcome: "unfit", validated: true });
    },
  },
  {
    name: "assessment from an older questionnaire",
    refusal: "rider_not_fit",
    prepare: async (w) => {
      await insertAssessment(w, {
        questionnaireVersion: `${CURRENT_QUESTIONNAIRE.version}-old`,
        validated: true,
        validUntil: NOW + 60_000,
      });
    },
  },
  {
    name: "expired aptitude",
    refusal: "fitness_expired",
    prepare: async (w) => {
      await makeRiderReady(w.t, w.patient, w.orgA, {
        now: NOW,
        validUntil: NOW,
      });
    },
  },
  {
    name: "missing consent",
    refusal: "consent_missing",
    prepare: async (w) => {
      await insertAssessment(w, {
        validated: true,
        validUntil: NOW + 60_000,
      });
    },
  },
  {
    name: "withdrawn consent",
    refusal: "consent_missing",
    prepare: async (w) => {
      await insertAssessment(w, {
        validated: true,
        validUntil: NOW + 60_000,
      });
      await insertConsent(w, { withdrawn: true });
    },
  },
  {
    name: "consent for an older text version",
    refusal: "consent_missing",
    prepare: async (w) => {
      await insertAssessment(w, {
        validated: true,
        validUntil: NOW + 60_000,
      });
      await insertConsent(w, { textVersion: `${CONSENT_TEXT_VERSION}-old` });
    },
  },
  {
    name: "current fit assessment with active consent",
    refusal: null,
    prepare: async (w) => {
      await makeRiderReady(w.t, w.patient, w.orgA, {
        now: NOW,
        validUntil: NOW + 60_000,
      });
    },
  },
];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("ANH-143 launch fitness and consent", () => {
  it.each(launchCases)(
    "$name matches riderFitness and riderLaunchRefusal",
    async ({ prepare, refusal }) => {
      const w = await seedWorld(modules);
      await prepare(w);
      const facts = await w.t.run((ctx) =>
        riderFitness(ctx, w.patient, w.orgA, NOW),
      );
      const launchRefusal = await w.t.run((ctx) =>
        riderLaunchRefusal(ctx, w.patient, w.orgA, NOW),
      );
      const consent = await w.t.run(
        async (ctx) => (await activeConsent(ctx, w.patient, w.orgA)) !== null,
      );
      expect(facts.consent).toBe(consent);
      expect(launchRefusal === null).toBe(facts.fit && facts.consent);
      expect(launchRefusal).toBe(refusal);

      if (refusal === null) {
        const sessionId = await launch(w, "patient");
        expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
          userId: w.patient,
          status: "pending",
        });
      } else {
        await expectCode(launch(w, "patient"), refusal);
      }
    },
  );

  it("returns the passenger's refusal when a manager launches for them", async () => {
    const w = await seedWorld(modules);
    await expectCode(launch(w, "manager", w.patient), "rider_not_fit");
  });

  it("moves from refused to launchable after validation and consent", async () => {
    const w = await seedWorld(modules);
    const submitted = await as(w.t, "patient").mutation(
      api.fitness.submitFitnessAnswers,
      {
        questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
        answers: fitAnswers(),
      },
    );
    const machineState = async () =>
      (
        await as(w.t, "patient").query(api.training.listLaunchableMachines, {})
      ).find((machine) => machine._id === w.machine);

    expect((await machineState())?.myLaunchRefusal).toBe("rider_not_fit");
    await expectCode(launch(w, "patient"), "rider_not_fit");

    await as(w.t, "manager").mutation(api.fitness.validateFitnessAssessment, {
      assessmentId: submitted.assessmentId,
      validUntil: NOW + 60_000,
    });
    expect((await machineState())?.myLaunchRefusal).toBe("consent_missing");
    await expectCode(launch(w, "patient"), "consent_missing");

    await as(w.t, "patient").mutation(api.fitness.signConsent, {
      textVersion: CONSENT_TEXT_VERSION,
    });
    expect((await machineState())?.myLaunchRefusal).toBe(null);
    const sessionId = await launch(w, "patient");
    expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
      userId: w.patient,
      status: "pending",
    });
  });

  it("checks the caller's fitness in each machine's organisation", async () => {
    const w = await seedWorld(modules);
    await makeRiderReady(w.t, w.patient, w.orgA, {
      now: NOW,
      validUntil: NOW + 60_000,
    });
    await w.t.run(async (ctx) => {
      await ctx.db.insert("memberships", {
        userId: w.patient,
        organizationId: w.orgB,
        role: "user",
        active: true,
      });
      await ctx.db.insert("machine_user_permissions", {
        organizationId: w.orgB,
        machineId: w.orgBMachine,
        userId: w.patient,
        grantedBy: w.orgBManager,
        createdAt: NOW,
      });
    });

    const inA = as(w.t, "patient");
    const inB = w.t.withIdentity({
      subject: "patient",
      ...organizationClaims("orgBPatient"),
    });
    const inAMachines = await inA.query(
      api.training.listLaunchableMachines,
      {},
    );
    const inBMachines = await inB.query(
      api.training.listLaunchableMachines,
      {},
    );
    expect(
      inAMachines.find((machine) => machine._id === w.machine)?.myLaunchRefusal,
    ).toBe(null);
    expect(
      inBMachines.find((machine) => machine._id === w.orgBMachine)
        ?.myLaunchRefusal,
    ).toBe("rider_not_fit");
  });
});

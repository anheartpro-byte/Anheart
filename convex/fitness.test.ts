import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import type { CurrentUser } from "./lib/auth";
import { riderOrganizationId } from "./lib/fitness";
import {
  CURRENT_QUESTIONNAIRE,
  type FitnessOutcome,
} from "../lib/fitnessQuestionnaire";
import { CONSENT_TEXT_VERSION } from "../lib/consentText";
import { mockClerkInvitations } from "./patientInvitation.fixtures";
import {
  as,
  makeRiderReady,
  modules,
  NOW,
  seedWorld,
  type Actor,
  type World,
} from "./test.setup";

type AssessmentOptions = Readonly<{
  userId?: Id<"users">;
  organizationId?: Id<"organizations">;
  questionnaireVersion?: string;
  outcome?: FitnessOutcome;
  submittedAt?: number;
  validated?: boolean;
  validUntil?: number;
  notes?: string;
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
  const outcome = options.outcome ?? "fit";
  return await w.t.run(async (ctx) => {
    return await ctx.db.insert("fitness_assessments", {
      userId,
      organizationId,
      questionnaireVersion:
        options.questionnaireVersion ?? CURRENT_QUESTIONNAIRE.version,
      answers: fitAnswers(),
      outcome,
      submittedBy: userId,
      submittedAt: options.submittedAt ?? NOW,
      ...(options.validated
        ? {
            validatedBy: w.manager,
            validatedAt: NOW - 1,
          }
        : {}),
      ...(options.validUntil === undefined
        ? {}
        : { validUntil: options.validUntil }),
      ...(options.notes === undefined ? {} : { notes: options.notes }),
    });
  });
}

async function currentUser(
  w: World,
  id: Id<"users">,
  role: CurrentUser["role"],
  organizationId: Id<"organizations">,
): Promise<CurrentUser> {
  const user = await w.t.run((ctx) => ctx.db.get(id));
  if (user === null) throw new Error("Missing test caller");
  return { ...user, role, organizationId };
}

async function expectCode(operation: Promise<unknown>, code: string) {
  await expect(operation).rejects.toMatchObject({ data: code });
}

async function readiness(
  w: World,
  actor: Actor,
  userId: Id<"users">,
  machineId?: Id<"machines">,
) {
  return await as(w.t, actor).query(api.fitness.getRiderReadiness, {
    userId,
    ...(machineId === undefined ? {} : { machineId }),
  });
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  mockClerkInvitations();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("ANH-143 fitness assessments", () => {
  it("stores required assessment and consent fields in indexed histories", async () => {
    const w = await seedWorld(modules);
    await insertAssessment(w, { submittedAt: NOW - 1 });
    const latestAssessmentId = await insertAssessment(w, {
      submittedAt: NOW,
      validated: true,
      validUntil: NOW + 60_000,
      notes: "Synthetic review note",
    });
    const latestAssessment = await w.t.run((ctx) =>
      ctx.db
        .query("fitness_assessments")
        .withIndex("by_user_and_organization", (q) =>
          q.eq("userId", w.patient).eq("organizationId", w.orgA),
        )
        .order("desc")
        .first(),
    );
    expect(latestAssessment?._id).toBe(latestAssessmentId);
    expect(latestAssessment).toMatchObject({
      userId: w.patient,
      organizationId: w.orgA,
      questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
      answers: fitAnswers(),
      outcome: "fit",
      submittedBy: w.patient,
      submittedAt: NOW,
      validatedBy: w.manager,
      validatedAt: NOW - 1,
      validUntil: NOW + 60_000,
      notes: "Synthetic review note",
    });

    const withdrawnConsentId = await w.t.run((ctx) =>
      ctx.db.insert("consents", {
        userId: w.patient,
        organizationId: w.orgA,
        textVersion: `${CONSENT_TEXT_VERSION}-old`,
        signedAt: NOW - 1,
        signedVia: "console",
        withdrawnAt: NOW,
        withdrawnBy: w.manager,
      }),
    );
    const activeConsentId = await as(w.t, "patient").mutation(
      api.fitness.signConsent,
      { textVersion: CONSENT_TEXT_VERSION },
    );
    const latestConsent = await w.t.run((ctx) =>
      ctx.db
        .query("consents")
        .withIndex("by_user_and_organization", (q) =>
          q.eq("userId", w.patient).eq("organizationId", w.orgA),
        )
        .order("desc")
        .first(),
    );
    expect(latestConsent?._id).toBe(activeConsentId);
    expect(latestConsent).toMatchObject({
      userId: w.patient,
      organizationId: w.orgA,
      textVersion: CONSENT_TEXT_VERSION,
      signedAt: NOW,
      signedVia: "site",
    });
    expect(
      await w.t.run((ctx) => ctx.db.get(withdrawnConsentId)),
    ).toMatchObject({
      withdrawnAt: NOW,
      withdrawnBy: w.manager,
    });
  });

  it("resolves the passenger's organization for Anheart admin, organization admin, manager, and passenger calls", async () => {
    const w = await seedWorld(modules);
    const cases = [
      {
        caller: await currentUser(w, w.admin, "admin", w.anheartOrg),
        userId: w.patient,
        expected: w.orgA,
      },
      {
        caller: await currentUser(w, w.orgAdmin, "org_admin", w.orgA),
        userId: w.patient,
        expected: w.orgA,
      },
      {
        caller: await currentUser(w, w.manager, "gestionnaire", w.orgA),
        userId: w.patient,
        expected: w.orgA,
      },
      {
        caller: await currentUser(w, w.patient, "user", w.orgA),
        userId: w.patient,
        expected: w.orgA,
      },
      {
        caller: await currentUser(w, w.admin, "admin", w.anheartOrg),
        userId: w.admin,
        expected: w.anheartOrg,
      },
    ] as const;
    for (const testCase of cases) {
      const organizationId = await w.t.run((ctx) =>
        riderOrganizationId(ctx, testCase.userId, testCase.caller),
      );
      expect(organizationId).toBe(testCase.expected);
    }
  });

  it("stores a passenger submission and lets their manager validate it without changing the outcome", async () => {
    const w = await seedWorld(modules);
    const submitted = await as(w.t, "patient").mutation(
      api.fitness.submitFitnessAnswers,
      {
        questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
        answers: fitAnswers(),
      },
    );
    expect(submitted.outcome).toBe("fit");
    const before = await w.t.run((ctx) => ctx.db.get(submitted.assessmentId));
    expect(before).toMatchObject({
      userId: w.patient,
      organizationId: w.orgA,
      questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
      submittedBy: w.patient,
      submittedAt: NOW,
      outcome: "fit",
    });
    expect(before?.validatedAt).toBeUndefined();

    await as(w.t, "manager").mutation(api.fitness.validateFitnessAssessment, {
      assessmentId: submitted.assessmentId,
      validUntil: NOW + 60_000,
      notes: "  synthetic review note  ",
    });
    const after = await w.t.run((ctx) => ctx.db.get(submitted.assessmentId));
    expect(after).toMatchObject({
      validatedBy: w.manager,
      validatedAt: NOW,
      validUntil: NOW + 60_000,
      notes: "synthetic review note",
      outcome: "fit",
    });
    const managerView = await readiness(w, "manager", w.patient);
    expect(managerView?.fitness).toMatchObject({
      outcome: "fit",
      validated: true,
      answers: fitAnswers(),
      notes: "synthetic review note",
    });
    const passengerView = await readiness(w, "patient", w.patient);
    expect(passengerView?.fitness).toMatchObject({
      outcome: "fit",
      validated: true,
    });
    expect(passengerView?.fitness).not.toHaveProperty("answers");
    expect(passengerView?.fitness).not.toHaveProperty("notes");
  });

  it("records a manager review of a non-fit outcome without requiring validity or changing the outcome", async () => {
    const w = await seedWorld(modules);
    const assessmentId = await insertAssessment(w, {
      outcome: "needs_medical_opinion",
    });
    await as(w.t, "manager").mutation(api.fitness.validateFitnessAssessment, {
      assessmentId,
      notes: "  reviewed  ",
    });
    const row = await w.t.run((ctx) => ctx.db.get(assessmentId));
    expect(row).toMatchObject({
      outcome: "needs_medical_opinion",
      validatedBy: w.manager,
      validatedAt: NOW,
      notes: "reviewed",
    });
    expect(row?.validUntil).toBeUndefined();
  });

  it.each(["manager", "admin"] as const)(
    "%s cannot validate their own assessment",
    async (actor) => {
      const w = await seedWorld(modules);
      const submitted = await as(w.t, actor).mutation(
        api.fitness.submitFitnessAnswers,
        {
          questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
          answers: fitAnswers(),
        },
      );
      await expectCode(
        as(w.t, actor).mutation(api.fitness.validateFitnessAssessment, {
          assessmentId: submitted.assessmentId,
          validUntil: NOW + 60_000,
        }),
        "fitness_self_validation",
      );
    },
  );

  it("refuses missing or unknown answers and an old questionnaire version by code", async () => {
    const w = await seedWorld(modules);
    await expectCode(
      as(w.t, "patient").mutation(api.fitness.submitFitnessAnswers, {
        questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
        answers: {},
      }),
      "fitness_answers_invalid",
    );
    await expectCode(
      as(w.t, "patient").mutation(api.fitness.submitFitnessAnswers, {
        questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
        answers: { ...fitAnswers(), unknown_question: true },
      }),
      "fitness_answers_invalid",
    );
    await expectCode(
      as(w.t, "patient").mutation(api.fitness.submitFitnessAnswers, {
        questionnaireVersion: `${CURRENT_QUESTIONNAIRE.version}-old`,
        answers: fitAnswers(),
      }),
      "questionnaire_outdated",
    );
  });

  it("refuses a missing or out-of-scope assessment", async () => {
    const w = await seedWorld(modules);
    const removed = await insertAssessment(w);
    await w.t.run((ctx) => ctx.db.delete(removed));
    await expectCode(
      as(w.t, "manager").mutation(api.fitness.validateFitnessAssessment, {
        assessmentId: removed,
        validUntil: NOW + 60_000,
      }),
      "fitness_assessment_not_found",
    );

    const assessmentId = await insertAssessment(w, {
      userId: w.orgBPatient,
      organizationId: w.orgB,
    });
    await expectCode(
      as(w.t, "manager").mutation(api.fitness.validateFitnessAssessment, {
        assessmentId,
        validUntil: NOW + 60_000,
      }),
      "fitness_assessment_not_found",
    );
  });

  it("refuses a superseded assessment and a second validation", async () => {
    const w = await seedWorld(modules);
    const older = await insertAssessment(w, { submittedAt: NOW - 2 });
    await insertAssessment(w, { submittedAt: NOW - 1 });
    await expectCode(
      as(w.t, "manager").mutation(api.fitness.validateFitnessAssessment, {
        assessmentId: older,
        validUntil: NOW + 60_000,
      }),
      "fitness_assessment_superseded",
    );

    const validated = await insertAssessment(w, {
      submittedAt: NOW,
      validated: true,
      validUntil: NOW + 60_000,
    });
    await expectCode(
      as(w.t, "manager").mutation(api.fitness.validateFitnessAssessment, {
        assessmentId: validated,
        validUntil: NOW + 60_000,
      }),
      "fitness_already_validated",
    );
  });

  it.each([
    ["fit assessment has no validity", undefined, "fit"],
    ["fit assessment is already expired", NOW, "fit"],
    ["fit assessment has a fractional validity", NOW + 1.5, "fit"],
    [
      "non-fit assessment has a validity",
      NOW + 60_000,
      "needs_medical_opinion",
    ],
  ] as const)(
    "refuses invalid validity when %s",
    async (_label, validUntil, outcome) => {
      const w = await seedWorld(modules);
      const assessmentId = await insertAssessment(w, { outcome });
      await expectCode(
        as(w.t, "manager").mutation(api.fitness.validateFitnessAssessment, {
          assessmentId,
          ...(validUntil === undefined ? {} : { validUntil }),
        }),
        "invalid_validity",
      );
    },
  );

  it("trims review notes and refuses notes longer than 1000 characters", async () => {
    const w = await seedWorld(modules);
    const assessmentId = await insertAssessment(w);
    await expectCode(
      as(w.t, "manager").mutation(api.fitness.validateFitnessAssessment, {
        assessmentId,
        validUntil: NOW + 60_000,
        notes: ` ${"n".repeat(1001)} `,
      }),
      "fitness_notes_too_long",
    );
  });

  it("refuses validation of an assessment from an old questionnaire version", async () => {
    const w = await seedWorld(modules);
    const assessmentId = await insertAssessment(w, {
      questionnaireVersion: `${CURRENT_QUESTIONNAIRE.version}-old`,
    });
    await expectCode(
      as(w.t, "manager").mutation(api.fitness.validateFitnessAssessment, {
        assessmentId,
        validUntil: NOW + 60_000,
      }),
      "questionnaire_outdated",
    );
  });

  it("uses only the latest assessment and returns launch refusal codes in order", async () => {
    const w = await seedWorld(modules);
    expect((await readiness(w, "manager", w.patient))?.launchRefusal).toBe(
      "rider_not_fit",
    );

    const ready = await makeRiderReady(w.t, w.patient, w.orgA, {
      now: NOW,
      validUntil: NOW + 60_000,
    });
    expect((await readiness(w, "manager", w.patient))?.launchRefusal).toBe(
      null,
    );
    vi.setSystemTime(NOW + 1);
    const submitted = await as(w.t, "patient").mutation(
      api.fitness.submitFitnessAnswers,
      {
        questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
        answers: fitAnswers(),
      },
    );
    expect(submitted.outcome).toBe("fit");
    expect((await readiness(w, "manager", w.patient))?.launchRefusal).toBe(
      "rider_not_fit",
    );
    await w.t.run((ctx) =>
      ctx.db.patch(submitted.assessmentId, {
        questionnaireVersion: `${CURRENT_QUESTIONNAIRE.version}-old`,
      }),
    );
    expect((await readiness(w, "manager", w.patient))?.launchRefusal).toBe(
      "rider_not_fit",
    );
    await expectCode(
      as(w.t, "manager").mutation(api.fitness.validateFitnessAssessment, {
        assessmentId: submitted.assessmentId,
        validUntil: NOW + 60_000,
      }),
      "questionnaire_outdated",
    );

    await insertAssessment(w, {
      submittedAt: NOW + 2,
      outcome: "needs_medical_opinion",
      validated: true,
    });
    expect((await readiness(w, "manager", w.patient))?.launchRefusal).toBe(
      "rider_not_fit",
    );
    expect(ready.assessmentId).not.toBe(submitted.assessmentId);
  });

  it("reports expiry at the validity boundary and consent missing after expiry is cleared", async () => {
    const w = await seedWorld(modules);
    await makeRiderReady(w.t, w.patient, w.orgA, {
      now: NOW,
      validUntil: NOW,
    });
    expect((await readiness(w, "manager", w.patient))?.launchRefusal).toBe(
      "fitness_expired",
    );

    const row = await w.t.run((ctx) =>
      ctx.db
        .query("fitness_assessments")
        .withIndex("by_user_and_organization", (q) =>
          q.eq("userId", w.patient).eq("organizationId", w.orgA),
        )
        .first(),
    );
    if (row === null) throw new Error("Missing seeded assessment");
    await w.t.run((ctx) => ctx.db.patch(row._id, { validUntil: NOW + 60_000 }));
    await w.t.run(async (ctx) => {
      const consents = await ctx.db
        .query("consents")
        .withIndex("by_user_and_organization", (q) =>
          q.eq("userId", w.patient).eq("organizationId", w.orgA),
        )
        .collect();
      for (const consent of consents) await ctx.db.delete(consent._id);
    });
    expect((await readiness(w, "manager", w.patient))?.launchRefusal).toBe(
      "consent_missing",
    );
  });
});

describe("ANH-143 passenger consent", () => {
  it("lets a manager sign their own consent and launch after an admin validates them", async () => {
    const w = await seedWorld(modules);
    await w.t.run((ctx) =>
      ctx.db.patch(w.manager, { hrMax: 180, birthYear: 1990 }),
    );
    const assessmentId = await w.t.run((ctx) =>
      ctx.db.insert("fitness_assessments", {
        userId: w.manager,
        organizationId: w.orgA,
        questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
        answers: fitAnswers(),
        outcome: "fit",
        submittedBy: w.manager,
        submittedAt: NOW,
      }),
    );
    await as(w.t, "admin").mutation(api.fitness.validateFitnessAssessment, {
      assessmentId,
      validUntil: NOW + 60_000,
    });
    await as(w.t, "manager").mutation(api.fitness.signConsent, {
      textVersion: CONSENT_TEXT_VERSION,
    });

    const sessionId = await as(w.t, "manager").mutation(
      api.training.launchAutoSession,
      { machineId: w.machine, profileId: "p1" },
    );
    const session = await w.t.run((ctx) => ctx.db.get(sessionId));
    expect(session).toMatchObject({
      userId: w.manager,
      startedById: w.manager,
      status: "pending",
    });
  });

  it("signs with the current text and returns the same id without another write", async () => {
    const w = await seedWorld(modules);
    const first = await as(w.t, "patient").mutation(api.fitness.signConsent, {
      textVersion: CONSENT_TEXT_VERSION,
    });
    const second = await as(w.t, "patient").mutation(api.fitness.signConsent, {
      textVersion: CONSENT_TEXT_VERSION,
    });
    expect(second).toBe(first);
    const rows = await w.t.run((ctx) =>
      ctx.db
        .query("consents")
        .withIndex("by_user_and_organization", (q) =>
          q.eq("userId", w.patient).eq("organizationId", w.orgA),
        )
        .collect(),
    );
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      textVersion: CONSENT_TEXT_VERSION,
      signedAt: NOW,
      signedVia: "site",
    });
  });

  it("rejects a signature for an old text version", async () => {
    const w = await seedWorld(modules);
    await expectCode(
      as(w.t, "patient").mutation(api.fitness.signConsent, {
        textVersion: `${CONSENT_TEXT_VERSION}-old`,
      }),
      "consent_text_outdated",
    );
  });

  it.each(["patient", "manager"] as const)(
    "%s can withdraw consent and the launch becomes blocked",
    async (actor) => {
      const w = await seedWorld(modules);
      await insertAssessment(w, {
        validated: true,
        validUntil: NOW + 60_000,
      });
      const consentId = await as(w.t, "patient").mutation(
        api.fitness.signConsent,
        {
          textVersion: CONSENT_TEXT_VERSION,
        },
      );
      const olderConsentId = await w.t.run((ctx) =>
        ctx.db.insert("consents", {
          userId: w.patient,
          organizationId: w.orgA,
          textVersion: `${CONSENT_TEXT_VERSION}-old`,
          signedAt: NOW - 1,
          signedVia: "console",
        }),
      );
      await as(w.t, actor).mutation(api.fitness.withdrawConsent, {
        ...(actor === "patient" ? {} : { userId: w.patient }),
      });
      const consent = await w.t.run((ctx) => ctx.db.get(consentId));
      expect(consent).toMatchObject({
        withdrawnAt: NOW,
        withdrawnBy: userIdFor(w, actor),
      });
      const olderConsent = await w.t.run((ctx) => ctx.db.get(olderConsentId));
      expect(olderConsent).toMatchObject({
        withdrawnAt: NOW,
        withdrawnBy: userIdFor(w, actor),
      });
      expect((await readiness(w, "manager", w.patient))?.launchRefusal).toBe(
        "consent_missing",
      );
    },
  );

  it("refuses withdrawal when no consent is active", async () => {
    const w = await seedWorld(modules);
    await expectCode(
      as(w.t, "patient").mutation(api.fitness.withdrawConsent, {}),
      "consent_not_active",
    );
  });

  it("does not treat a different text version as active consent", async () => {
    const w = await seedWorld(modules);
    await insertAssessment(w, { validated: true, validUntil: NOW + 60_000 });
    await w.t.run((ctx) =>
      ctx.db.insert("consents", {
        userId: w.patient,
        organizationId: w.orgA,
        textVersion: `${CONSENT_TEXT_VERSION}-old`,
        signedAt: NOW,
        signedVia: "console",
      }),
    );
    const view = await readiness(w, "manager", w.patient);
    expect(view?.consent.active).toBe(null);
    expect(view?.launchRefusal).toBe("consent_missing");
  });
});

describe("ANH-143 readiness visibility and logging", () => {
  it("returns sensitive fields to the manager and no answers or notes to the passenger", async () => {
    const w = await seedWorld(modules);
    await makeRiderReady(w.t, w.patient, w.orgA, {
      now: NOW,
      validUntil: NOW + 60_000,
    });
    const managerView = await readiness(w, "manager", w.patient);
    expect(managerView?.fitness?.answers).toEqual(fitAnswers());
    expect(managerView?.fitness?.notes).toBe("Synthetic review note");
    expect(managerView?.canValidate).toBe(true);
    const passengerView = await readiness(w, "patient", w.patient);
    expect(passengerView?.fitness?.answers).toBeUndefined();
    expect(passengerView?.fitness?.notes).toBeUndefined();
    expect(passengerView?.canValidate).toBe(false);
  });

  it("uses a machine's organisation and hides missing, deleted, or foreign machines", async () => {
    const w = await seedWorld(modules);
    await makeRiderReady(w.t, w.patient, w.orgA, {
      now: NOW,
      validUntil: NOW + 60_000,
    });
    expect(
      (await readiness(w, "manager", w.patient, w.machine))?.fitness,
    ).not.toBeNull();
    expect(await readiness(w, "orgBManager", w.patient, w.machine)).toBe(null);
    expect(await readiness(w, "manager", w.patient, w.orgBMachine)).toBe(null);
    await w.t.run((ctx) =>
      ctx.db.patch(w.machine, { isDeleted: true, deletedAt: NOW }),
    );
    expect(await readiness(w, "manager", w.patient, w.machine)).toBe(null);
  });

  it("keeps answer keys, answer values, and review notes out of console calls", async () => {
    const w = await seedWorld(modules);
    const spies = [
      vi.spyOn(console, "log").mockImplementation(() => undefined),
      vi.spyOn(console, "info").mockImplementation(() => undefined),
      vi.spyOn(console, "warn").mockImplementation(() => undefined),
      vi.spyOn(console, "error").mockImplementation(() => undefined),
      vi.spyOn(console, "debug").mockImplementation(() => undefined),
    ];
    const submitted = await as(w.t, "patient").mutation(
      api.fitness.submitFitnessAnswers,
      {
        questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
        answers: fitAnswers(),
      },
    );
    await as(w.t, "manager").mutation(api.fitness.validateFitnessAssessment, {
      assessmentId: submitted.assessmentId,
      validUntil: NOW + 60_000,
      notes: "SENSITIVE_REVIEW_NOTE",
    });
    await readiness(w, "manager", w.patient);
    const calls = spies.flatMap((spy) =>
      spy.mock.calls.map((args) => JSON.stringify(args)),
    );
    const logged = calls.join("\n");
    expect(logged).not.toContain("SENSITIVE_REVIEW_NOTE");
    for (const question of CURRENT_QUESTIONNAIRE.questions) {
      expect(logged).not.toContain(question.id);
    }
  });
});

function userIdFor(w: World, actor: "patient" | "manager"): Id<"users"> {
  return actor === "patient" ? w.patient : w.manager;
}

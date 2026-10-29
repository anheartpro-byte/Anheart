import { CONSENT_TEXT_VERSION } from "../../lib/consentText";
import { CURRENT_QUESTIONNAIRE } from "../../lib/fitnessQuestionnaire";
import { callAs } from "./convex";
import { convexUserId, identityOf, type AccountKey, type RunInfo } from "./run";

/**
 * A passenger made ready to launch by the harness, through the same Convex
 * functions the site calls (the site's own path is played in
 * parcours.spec.ts): the manager's patient, a fit assessment validated by the
 * manager, consent signed, a physiology, the launch right on each machine.
 * Played again on a rider already ready, it leaves them ready.
 */
export async function prepareRider(
  run: RunInfo,
  rider: {
    account: AccountKey;
    manager: AccountKey;
    /** A staff account that may assign the manager: the organisation's admin, or the Anheart admin. */
    assigner: AccountKey;
    /** Who grants the launch rights, when the manager does not manage the machines. */
    granter?: AccountKey;
    machineIds: string[];
    hrMax: number;
    birthYear: number;
  },
): Promise<string> {
  const as = (account: AccountKey) => identityOf(run, account);
  const riderId = await convexUserId(run, rider.account);
  const managerId = await convexUserId(run, rider.manager);
  const managers = await callAs<{ _id: string }[]>(run.backend, as(rider.assigner), "query", "users:getGestionnairesForPatient", {
    userId: riderId,
  });
  if (!managers.some((manager) => manager._id === managerId)) {
    await callAs(run.backend, as(rider.assigner), "mutation", "users:assignGestionnaireToUser", {
      userId: riderId,
      gestionnaireId: managerId,
    });
  }
  const answers = Object.fromEntries(CURRENT_QUESTIONNAIRE.questions.map((q) => [q.id, q.outcomes.yes === "fit"]));
  const { assessmentId } = await callAs<{ assessmentId: string }>(run.backend, as(rider.account), "mutation", "fitness:submitFitnessAnswers", {
    questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
    answers,
  });
  await callAs(run.backend, as(rider.manager), "mutation", "fitness:validateFitnessAssessment", {
    assessmentId,
    validUntil: Date.now() + 30 * 24 * 3600 * 1000,
  });
  await callAs(run.backend, as(rider.account), "mutation", "fitness:signConsent", { textVersion: CONSENT_TEXT_VERSION });
  await callAs(run.backend, as(rider.manager), "mutation", "training:setUserPhysiology", {
    userId: riderId,
    hrMax: rider.hrMax,
    birthYear: rider.birthYear,
  });
  // Ready means no launch of their own still waiting either (one session at a
  // time): one left by an earlier file is cancelled, as the rider would.
  const waiting = await callAs<{ _id: string }[]>(run.backend, as(rider.account), "query", "sessions:listSessions", {
    status: "pending",
  });
  for (const session of waiting) {
    await callAs(run.backend, as(rider.account), "mutation", "training:requestStop", { sessionId: session._id });
  }
  for (const machineId of rider.machineIds) {
    await callAs(run.backend, as(rider.granter ?? rider.manager), "mutation", "training:grantLaunchRight", { machineId, userId: riderId });
  }
  return riderId;
}

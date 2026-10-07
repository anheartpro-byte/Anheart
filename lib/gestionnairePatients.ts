/**
 * Rule of the "Assigner des patients" window on a gestionnaire's page.
 *
 * `users.assignPatientsToGestionnaire` sets the gestionnaire's list exactly:
 * a patient who is not sent loses the link. This function builds the list to
 * send. It is the rule of the machines window of the same page, so that the
 * two windows save the same way.
 */
import { machineIdsToSave } from "./gestionnaireMachines";

/**
 * The patients to send when the window is saved.
 *
 * - `checked`: the boxes checked in the window.
 * - `listed`: the patients the window shows a box for.
 * - `linked`: the patients the gestionnaire manages today.
 *
 * A checked box adds the link and an unchecked box removes it. A patient the
 * gestionnaire manages but the window does not list has no box: nobody
 * unchecked it, so saving must not remove it. An identifier with no box is
 * never added.
 */
export function patientIdsToSave<T extends string>(selection: {
  checked: readonly T[];
  listed: readonly T[];
  linked: readonly T[];
}): T[] {
  return machineIdsToSave(selection);
}

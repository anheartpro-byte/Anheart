/**
 * Submit rule of the machine form (`components/modals/MachineFormModal.tsx`).
 *
 * Editing a machine calls `machines.updateMachine` (admin, or a gestionnaire
 * of the machine). Choosing the machine's gestionnaires is a separate call,
 * `machines.assignMachineToGestionnaires`, which the server reserves to an
 * admin. These functions decide when the form makes that second call, and
 * make it. They take the mutation as an argument so the rule is tested
 * without a browser.
 */

/**
 * Whether this role may choose a machine's gestionnaires in the form.
 *
 * Admin only today. This is the single place to extend when another role may
 * do it. An unknown role (account not loaded yet) may not.
 */
export function canAssignGestionnaires(role: string | undefined): boolean {
  return role === "admin";
}

/**
 * The gestionnaire list to send after a machine edit, or `null` when
 * `machines.assignMachineToGestionnaires` must not be called.
 *
 * - `role`: the caller's role.
 * - `current`: the machine's gestionnaires at the time of saving, in the
 *   machine's order (the first one is its owner).
 * - `selected`: the form's list at the time of saving, in the order the boxes
 *   were checked.
 *
 * The list is sent only if the caller may choose the gestionnaires and the
 * form's list differs from the machine's current one. The order is part of
 * the list: the server makes the first gestionnaire the owner, so the same
 * people in another order is a change. An identifier repeated in a list is
 * read once.
 */
export function gestionnaireIdsToSubmit<T extends string>(edit: {
  role: string | undefined;
  current: readonly T[];
  selected: readonly T[] | undefined;
}): T[] | null {
  if (!canAssignGestionnaires(edit.role) || edit.selected === undefined) {
    return null;
  }
  // A Set keeps the first occurrence of each identifier, in order.
  const current = [...new Set(edit.current)];
  const selected = [...new Set(edit.selected)];
  const unchanged =
    current.length === selected.length &&
    selected.every((id, index) => id === current[index]);
  return unchanged ? null : selected;
}

/**
 * The gestionnaire step of a machine edit, run after `machines.updateMachine`.
 *
 * `assign` is the admin-only `machines.assignMachineToGestionnaires` call. It
 * is made only when the rule above yields a list, so a gestionnaire saving the
 * name or the place never makes it. A refusal of `assign` is not caught here:
 * it reaches the form, which shows it.
 */
export async function submitGestionnaireList<T extends string>(edit: {
  role: string | undefined;
  current: readonly T[];
  selected: readonly T[] | undefined;
  assign: (gestionnaireIds: T[]) => Promise<unknown>;
}): Promise<void> {
  const gestionnaireIds = gestionnaireIdsToSubmit(edit);
  if (gestionnaireIds === null) return;
  await edit.assign(gestionnaireIds);
}

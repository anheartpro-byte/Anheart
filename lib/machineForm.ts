/**
 * Submit rule of the machine form (`components/modals/MachineFormModal.tsx`).
 *
 * Editing a machine calls `machines.updateMachine` (admin, or a gestionnaire
 * of the machine). Choosing the machine's gestionnaires is a separate call,
 * `machines.assignMachineToGestionnaires`, which the server reserves to an
 * admin. These functions decide when the form makes that second call. They
 * are pure so the rule is tested without a browser.
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
 * - `current`: the machine's gestionnaires when the form opened.
 * - `selected`: the boxes checked when the form is saved.
 *
 * The list is sent only if the caller may choose the gestionnaires and the
 * checked boxes differ from the machine's current gestionnaires. The order of
 * the boxes is not a change: checking the same people sends nothing.
 */
export function gestionnaireIdsToSubmit<T extends string>(edit: {
  role: string | undefined;
  current: readonly T[];
  selected: readonly T[] | undefined;
}): T[] | null {
  if (!canAssignGestionnaires(edit.role) || edit.selected === undefined) {
    return null;
  }
  const current = new Set(edit.current);
  const selected = new Set(edit.selected);
  const unchanged =
    current.size === selected.size &&
    [...selected].every((id) => current.has(id));
  // Sent in the order of the boxes, each gestionnaire once.
  return unchanged ? null : [...selected];
}

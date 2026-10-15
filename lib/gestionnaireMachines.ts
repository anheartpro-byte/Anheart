/**
 * Rule of the "Assigner des machines" window on a gestionnaire's page.
 *
 * `machines.setGestionnaireMachines` sets the gestionnaire's list exactly:
 * what is not sent is removed. This function builds the list to send. It is
 * pure so the rule is tested without a browser.
 */

/**
 * The machines to send when the window is saved.
 *
 * - `checked`: the boxes checked in the window.
 * - `listed`: the machines the window shows a box for.
 * - `linked`: the machines the gestionnaire manages today.
 *
 * A checked box adds the link and an unchecked box removes it. A machine the
 * gestionnaire manages but the window does not list (a deleted machine has no
 * box) is kept: nobody unchecked it, so saving must not remove it. An
 * identifier with no box is never added.
 */
export function machineIdsToSave<T extends string>(selection: {
  checked: readonly T[];
  listed: readonly T[];
  linked: readonly T[];
}): T[] {
  const listed = new Set(selection.listed);
  const keptWithoutBox = selection.linked.filter((id) => !listed.has(id));
  const checkedBoxes = selection.checked.filter((id) => listed.has(id));
  return [...new Set([...keptWithoutBox, ...checkedBoxes])];
}

/**
 * The boxes ticked in the window, in the order the window lists them.
 *
 * - `listed`: the identifiers the window shows a box for.
 * - `linked`: the links on the server now.
 * - `choices`: what the visitor clicked since the window opened, box by box.
 *
 * A box the visitor clicked shows their choice, whatever the server says
 * since: an edit in progress is never undone by a new answer of the server
 * (ANH-194). Any other box shows the server as it is now, so a link another
 * admin adds or removes meanwhile appears in the window, and what is saved
 * is what is on screen.
 */
export function tickedBoxes<T extends string>(window: {
  listed: readonly T[];
  linked: readonly T[];
  choices: ReadonlyMap<T, boolean>;
}): T[] {
  const linked = new Set(window.linked);
  return window.listed.filter((id) => window.choices.get(id) ?? linked.has(id));
}

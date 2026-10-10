/**
 * Stand-ins for the Radix UI primitives that need a browser, for the tests of
 * the components built on them.
 *
 * A window of Radix (dialog, sheet, menu, select, tooltip) asks a browser for
 * a portal, a layout and a scroll lock: the tests of this site have none.
 * What the site's own code decides does not depend on them: which primitive
 * it draws, inside which other, with which props. A stand-in shows exactly
 * that. The test names the primitives it replaces and the element each draws:
 *
 *     vi.mock("@radix-ui/react-tooltip", async () =>
 *       (await import("@/test-support/radix")).standIns("Tooltip", {
 *         Provider: "div",
 *         Root: "div",
 *         Trigger: "button",
 *         Portal: "div",
 *         Content: "div",
 *         Arrow: "span",
 *       }),
 *     );
 *
 *     const page = draw(<TooltipContent>Stop</TooltipContent>);
 *     expect(page.only(isPrimitive("Tooltip.Arrow")).parent).toBe(
 *       page.slot("tooltip-content"),
 *     );
 *     expect(given("Tooltip.Content").sideOffset).toBe(0);
 *
 * Each stand-in draws one plain element, in place and at once (no portal,
 * nothing waits to be opened), marked `data-primitive="Tooltip.Content"`. The
 * props a document understands (`class`, `id`, `role`, `data-*`, `aria-*`,
 * `onClick`...) go on the element; every prop, those included, is kept for
 * the test to read with `given`. `asChild` is honoured with the real Slot of
 * Radix: the child is drawn in place of the element.
 */

import { createElement, type FunctionComponent } from "react";
import { Slot } from "@radix-ui/react-slot";
import { afterEach } from "vitest";

/** The props a stand-in received, as the component under test wrote them. */
export type GivenProps = Record<string, unknown>;

const renders = new Map<string, GivenProps[]>();

afterEach(() => renders.clear());

/** The props a plain element takes without a warning of React. */
const ON_THE_ELEMENT = new Set([
  "children",
  "className",
  "id",
  "style",
  "role",
  "title",
  "hidden",
  "disabled",
  "type",
  "htmlFor",
  "href",
  "src",
  "alt",
  "tabIndex",
  "dir",
  "ref",
  "onClick",
]);

function onTheElement(props: GivenProps): GivenProps {
  return Object.fromEntries(
    Object.entries(props).filter(
      ([name]) =>
        ON_THE_ELEMENT.has(name) ||
        name.startsWith("data-") ||
        name.startsWith("aria-"),
    ),
  );
}

/**
 * One stand-in: `standIn("Dialog.Close", "button")`.
 * @param name what the test calls the primitive, written on the element as `data-primitive`
 * @param tag the element drawn
 */
export function standIn(
  name: string,
  tag: string,
): FunctionComponent<GivenProps> {
  function StandIn(props: GivenProps) {
    renders.set(name, [...(renders.get(name) ?? []), props]);
    const shown = { "data-primitive": name, ...onTheElement(props) };
    return props.asChild === true ? (
      <Slot {...shown} />
    ) : (
      createElement(tag, shown)
    );
  }
  StandIn.displayName = name;
  return StandIn;
}

/**
 * The stand-ins of one package, as the module a `vi.mock` factory returns:
 * `standIns("Dialog", { Root: "div", Close: "button" })` gives `Root` and
 * `Close`, named `Dialog.Root` and `Dialog.Close`. A primitive the test did
 * not name is missing from the module, and Vitest says so when it is used.
 */
export function standIns(
  family: string,
  tags: Record<string, string>,
): Record<string, FunctionComponent<GivenProps>> {
  return Object.fromEntries(
    Object.entries(tags).map(([part, tag]) => [
      part,
      standIn(`${family}.${part}`, tag),
    ]),
  );
}

/** The props of every render of a stand-in since the test began, oldest first. */
export function allGiven(name: string): GivenProps[] {
  return renders.get(name) ?? [];
}

/** The props a stand-in received when it was last drawn. Throws if it never was. */
export function given(name: string): GivenProps {
  const last = allGiven(name).at(-1);
  if (last === undefined) {
    throw new Error(
      `${name} was never drawn. Drawn: ${[...renders.keys()].join(", ") || "nothing"}`,
    );
  }
  return last;
}

/**
 * The test that finds the element of a stand-in, for `page.all(...)`,
 * `page.only(...)` (test-support/markup) or `screen.all(...)` (test-support/render).
 */
export function isPrimitive(name: string) {
  return (element: { getAttribute(attribute: string): string | null }) =>
    element.getAttribute("data-primitive") === name;
}

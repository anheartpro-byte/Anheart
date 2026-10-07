/**
 * Mounting a component of the site in a test and acting on it: the real React,
 * the real texts of `messages/`, on the document of `test-support/dom` (no
 * browser, no DOM library).
 *
 *     const screen = render(<PhysiologyCard userId={id} user={user} />);
 *     await type(screen.field("phys-hrmax"), "185");
 *     await submit(screen.form());
 *     expect(mutation("training:setUserPhysiology")).toHaveBeenCalledWith(...);
 *     expect(screen.text()).toContain("Enregistré");
 *
 * Every component mounted here is unmounted after each test.
 */

// First, before React is loaded: React decides at load time whether it has a document.
import {
  elementsOf,
  markupOf,
  testDocument,
  TestElement,
  TestEvent,
  TestNode,
  textOf,
  type EventInit,
} from "./dom";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach } from "vitest";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";

export { elementsOf, markupOf, textOf, testDocument, TestElement };

export type Locale = "fr" | "en";

/** The texts of the site, as the catalogs hold them: a test names a text by its key, never by a copy. */
export const messages = { fr, en };

type Mounted = { root: Root; container: TestElement };
const mounted: Mounted[] = [];

/** What listens to `window`: the page-wide listeners of a component (a key, a resize). */
const windowListeners = new TestNode(1, "#window", null);
{
  const globals = globalThis as Record<string, unknown>;
  globals.addEventListener =
    windowListeners.addEventListener.bind(windowListeners);
  globals.removeEventListener =
    windowListeners.removeEventListener.bind(windowListeners);
}

export type Screen = {
  /** The element the component was mounted in. */
  container: TestElement;
  /** Everything a reader sees on the page, windows opened outside the container included. */
  text(): string;
  /** The markup of the page, to read a class name or an attribute. */
  markup(): string;
  /** Every element of the page that passes the test given. */
  all(test: (element: TestElement) => boolean): TestElement[];
  /** The elements of one tag: `tag("button")`. */
  tag(name: string): TestElement[];
  /** The one button that reads `label`. Throws if there is none, or more than one. */
  button(label: string): TestElement;
  /** Whether a button reads `label`. */
  hasButton(label: string): boolean;
  /** The one element with this `id`. */
  field(id: string): TestElement;
  /** The text a reader sees in one element of the page. */
  textOf(element: TestElement): string;
  /** The innermost elements that read exactly `text`. */
  reading(text: string): TestElement[];
  /** The one form of the page. */
  form(): TestElement;
  /** Renders again, with other props or the same: what a new answer of the server does. */
  rerender(ui: ReactNode): void;
  unmount(): void;
};

function wrap(ui: ReactNode, locale: Locale) {
  return (
    <NextIntlClientProvider
      locale={locale}
      messages={messages[locale]}
      timeZone="Europe/Paris"
    >
      {ui}
    </NextIntlClientProvider>
  );
}

/** The one element a search must find: the error says what the page holds instead. */
function only(found: TestElement[], what: string): TestElement {
  if (found.length === 1) return found[0];
  throw new Error(
    `${found.length} elements for ${what}, expected one. The page: ${markupOf(testDocument.body)}`,
  );
}

/** Mounts a component under the provider of the site's texts (French unless told otherwise). */
export function render(
  ui: ReactNode,
  { locale = "fr" }: { locale?: Locale } = {},
): Screen {
  const container = testDocument.body.appendChild(
    testDocument.createElement("div"),
  );
  const root = createRoot(container as unknown as Element);
  const entry = { root, container };
  mounted.push(entry);
  act(() => root.render(wrap(ui, locale)));
  const all = (test: (element: TestElement) => boolean) =>
    elementsOf(testDocument.body).filter(test);
  const buttons = (label: string) =>
    all(
      (element) => element.localName === "button" && textOf(element) === label,
    );
  return {
    container,
    text: () => textOf(testDocument.body),
    markup: () => markupOf(testDocument.body),
    all,
    tag: (name) => all((element) => element.localName === name),
    button: (label) => only(buttons(label), `the button "${label}"`),
    hasButton: (label) => buttons(label).length > 0,
    field: (id) =>
      only(
        all((element) => element.id === id),
        `the id "${id}"`,
      ),
    textOf: (element) => textOf(element),
    reading: (text) =>
      all(
        (element) =>
          textOf(element) === text &&
          !element.children.some((child) => textOf(child) === text),
      ),
    form: () =>
      only(
        all((element) => element.localName === "form"),
        "the form",
      ),
    rerender(next) {
      act(() => root.render(wrap(next, locale)));
    },
    unmount() {
      const at = mounted.indexOf(entry);
      if (at === -1) return;
      mounted.splice(at, 1);
      act(() => root.unmount());
      container.remove();
    },
  };
}

/** Unmounts what the test mounted and empties the page. */
export function cleanup() {
  for (const { root, container } of mounted.splice(0)) {
    act(() => root.unmount());
    container.remove();
  }
  for (const child of [...testDocument.body.childNodes]) child.remove();
  testDocument.activeElement = null;
}

afterEach(cleanup);

/**
 * Fires an event at an element and waits for what it starts: the handlers of
 * React, the promises they await (a mutation), the renders that follow.
 * @returns false if a handler prevented the default action
 */
export async function fire(
  target: TestNode,
  type: string,
  init: EventInit = {},
): Promise<boolean> {
  let allowed = true;
  await act(async () => {
    allowed = target.dispatchEvent(new TestEvent(type, init));
  });
  return allowed;
}

/** Fires an event at `window`: a key pressed anywhere, a resized page. */
export function fireWindow(type: string, init: EventInit = {}) {
  return fire(windowListeners, type, init);
}

/** A click of the main button. A disabled button hears nothing, as in a browser. */
export async function click(element: TestElement) {
  if (element.hasAttribute("disabled")) return;
  await fire(element, "click", { button: 0 });
}

/** Replaces what a field holds, as typing does. */
export async function type(element: TestElement, value: string) {
  element.value = value;
  await fire(element, "input");
  await fire(element, "change");
}

/** Submits a form: what Enter in a field, or its submit button, does. */
export async function submit(form: TestElement) {
  await fire(form, "submit");
}

/** Lets pending promises and timers of the component settle, under fake timers or not. */
export async function settle(run: () => unknown = () => {}) {
  await act(async () => {
    await run();
  });
}

/**
 * What the tests of the pages under app/ share: a page is rendered in jsdom,
 * under the real next-intl provider and the real message catalogs, with Convex
 * and the navigation replaced by stand-ins.
 *
 * A test file asks for jsdom itself and replaces the two modules by name:
 *
 *     // @vitest-environment jsdom
 *     vi.mock("convex/react", async () => (await import("@/test-support/pages")).convexReact);
 *     vi.mock("@/i18n/navigation", async () => (await import("@/test-support/pages")).navigation);
 *
 * Convex is the stand-in of the whole site, `test-support/convex.ts`: there is
 * one. This file only names its queries and mutations by their reference
 * (`api.machines.getMachine`) instead of by a text, so that an answer has the
 * type the server returns.
 *
 * Nothing here knows a page: what a page shows, asks and sends is said in its
 * own test file.
 */
import {
  cloneElement,
  Suspense,
  type AnchorHTMLAttributes,
  type ReactElement,
  type ReactNode,
} from "react";
import { act, cleanup, render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import {
  getFunctionName,
  type FunctionArgs,
  type FunctionReference,
  type FunctionReturnType,
} from "convex/server";
import {
  afterEach,
  beforeEach,
  expect,
  vi,
  type Mock,
  type MockInstance,
} from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import * as convex from "@/test-support/convex";
import { dismissFeedback, getFeedbackToasts } from "@/lib/feedback";
import { forgetServerAnswers } from "@/lib/server-clock";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";

/** The instant every test starts at, on this computer's clock and on the server's. */
export const NOW = 1_800_000_000_000;

// --- Convex ------------------------------------------------------------------

type Query = FunctionReference<"query">;
type Mutation = FunctionReference<"mutation">;
/** What a query answers: a value, or a value worked out from its arguments. */
type Answer<Q extends Query> =
  | FunctionReturnType<Q>
  | undefined
  | ((args: FunctionArgs<Q>) => FunctionReturnType<Q> | undefined);

/** Stands in for `convex/react`: the stand-in of the whole site. */
export const convexReact = convex.convexReact;

/** Says whether the visitor is signed in, signed out, or not known yet (the landing page). */
export const sessionIs = convex.sessionIs;

/**
 * What a query answers from now on, typed by what the server returns for it.
 * `undefined` is a query still loading, as in Convex. A query nobody answered
 * is loading too.
 */
export function answer<Q extends Query>(query: Q, value: Answer<Q>): void {
  convex.answer(getFunctionName(query), value);
}

/**
 * The arguments each render gave a query, oldest first: `"skip"` when the page
 * did not ask, `{}` when the query takes none.
 */
export function argsAsked<Q extends Query>(query: Q): unknown[] {
  return convex
    .asks(getFunctionName(query))
    .map((args) => (args === undefined ? {} : args));
}

/** The arguments of the latest render, or `undefined` if the query was never used. */
export function lastArgsAsked<Q extends Query>(query: Q): unknown {
  return argsAsked(query).at(-1);
}

/**
 * The function the page receives for a mutation: tell it what the server
 * answers (`mockResolvedValue`, `mockRejectedValue`), read what it was sent.
 * It answers `null` unless told otherwise. What a mutation answers is not
 * typed: only the answers of the queries are.
 */
export function mutation<M extends Mutation>(reference: M): Mock {
  return convex.mutation(getFunctionName(reference));
}

/** Every mutation the page sent, in the order it sent them, by function name. */
export function mutationsSent(): { name: string; args: unknown }[] {
  return Object.keys(convex.mutationCalls())
    .flatMap((name) => {
      const sent = convex.mutation(name).mock;
      return sent.calls.map((call, index) => ({
        name,
        args: call[0] as unknown,
        order: sent.invocationCallOrder[index],
      }));
    })
    .sort((a, b) => a.order - b.order)
    .map(({ name, args }) => ({ name, args }));
}

/** The identifier Convex gives a request, as a failure of the server carries it. */
export const REQUEST_ID = "8f3a9c1d2b4e6f70";

/**
 * What the Convex client rejects a mutation with when the function of the
 * server threw anything but a `ConvexError`. A production deployment masks
 * what was thrown; a development deployment appends it (`thrown`).
 */
export function serverError(reference: Mutation, thrown?: string): Error {
  const detail =
    thrown === undefined
      ? ""
      : `\nUncaught Error: ${thrown}\n    at handler (../convex/module.ts:1:1)\n`;
  return new Error(
    `[CONVEX M(${getFunctionName(reference)})] [Request ID: ${REQUEST_ID}] Server Error${detail}\n  Called by client`,
  );
}

/**
 * The two forms such a failure takes, each with the message the site then
 * shows a French visitor: the generic sentence and the reference of the
 * request in production, the sentence of the server on a development
 * deployment. For `it.each`.
 */
export function serverFailures(reference: Mutation, thrown: string) {
  return [
    {
      where: "in production, where what the server threw is masked",
      error: serverError(reference),
      shown: fr.feedback.failedWithReference.replace("{requestId}", REQUEST_ID),
    },
    {
      where: "on a development deployment, which sends what the server threw",
      error: serverError(reference, thrown),
      shown: thrown,
    },
  ];
}

// --- Navigation ----------------------------------------------------------------

/** The router the pages receive: read where they sent the visitor. */
export const router = {
  push: vi.fn(),
  replace: vi.fn(),
  back: vi.fn(),
  refresh: vi.fn(),
  prefetch: vi.fn(),
};

/**
 * Stands in for `@/i18n/navigation`. A link is a plain anchor carrying the
 * address the page gave, before the locale prefix; following it goes nowhere.
 */
export const navigation = {
  Link({
    href,
    children,
    ...rest
  }: Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & { href: string }) {
    return (
      <a {...rest} href={href} onClick={(event) => event.preventDefault()}>
        {children}
      </a>
    );
  },
  useRouter: () => router,
  usePathname: () => "/dashboard",
  redirect: vi.fn(),
};

// --- Components a page mounts without being their test -------------------------

const mounted = new Map<string, unknown[]>();

/**
 * A component standing in for one the page mounts: it keeps the props of each
 * render, so that a test reads what the page gave it and calls back what the
 * page listens to. It draws `body` if given, nothing otherwise.
 */
export function standIn<Props extends object>(
  name: string,
  body?: (props: Props) => ReactNode,
) {
  function StandIn(props: Props) {
    const renders = mounted.get(name) ?? [];
    mounted.set(name, renders);
    renders.push(props);
    return <div data-stand-in={name}>{body?.(props)}</div>;
  }
  StandIn.displayName = `StandIn(${name})`;
  return StandIn;
}

/** The props the page gave a stand-in at its latest render; throws if it never mounted it. */
export function propsOf<Props extends object>(name: string): Props {
  const last = mounted.get(name)?.at(-1);
  if (last === undefined) throw new Error(`The page never mounted ${name}`);
  return last as Props;
}

/** Whether the page mounted a stand-in at all. */
export function wasMounted(name: string): boolean {
  return (mounted.get(name)?.length ?? 0) > 0;
}

// --- Rendering -----------------------------------------------------------------

const catalogs = { fr, en };
const missingMessages: string[] = [];
let consoleError: MockInstance<typeof console.error> | undefined;

/**
 * The same function at every render, as the site's own provider has: a new
 * one would give every component a new translator, and so new callbacks.
 */
function noteMissingMessage(error: { message: string }): void {
  missingMessages.push(error.message);
}

/**
 * Renders a page as a visitor of `locale` sees it and waits for what it reads
 * with `use()` (its `params`). `refresh` renders it again after the answers of
 * the queries changed, as a Convex subscription would.
 */
export async function renderPage(
  page: ReactElement,
  { locale = "fr" }: { locale?: "fr" | "en" } = {},
) {
  const tree = (element: ReactElement) => (
    <NextIntlClientProvider
      locale={locale}
      messages={catalogs[locale]}
      timeZone="Europe/Paris"
      onError={noteMissingMessage}
    >
      <Suspense fallback={null}>{element}</Suspense>
    </NextIntlClientProvider>
  );
  // Awaited as a whole: a page that reads its `params` suspends at once.
  const view = await act(async () => render(tree(page)));
  return {
    ...view,
    user: userEvent.setup(),
    /** A new element each time: React would skip one it has already drawn. */
    async refresh() {
      await act(async () => view.rerender(tree(cloneElement(page))));
    },
  };
}

/** The `params` a dynamic route gives its page. */
export function routeParams<Params extends object>(
  params: Params,
): Promise<Params> {
  return Promise.resolve(params);
}

/** The messages shown by the feedback toaster, oldest first. */
export function feedbackShown(): { kind: string; message: string }[] {
  return getFeedbackToasts().map(({ kind, message }) => ({ kind, message }));
}

const MUTATION_FAILURE = "[mutation] ";
/** How many of each kind of line written to `console.error` a test has read. */
const consoleRead = { failures: 0, others: 0 };

function consoleLines(mutationFailures: boolean): string[] {
  return (consoleError?.mock.calls ?? [])
    .map((call) => call.map(String).join(" "))
    .filter((line) => line.startsWith(MUTATION_FAILURE) === mutationFailures);
}

/**
 * The failures `useMutationWithFeedback` wrote to the console since the last
 * reading, by mutation name. A failure nobody read fails the test when it
 * ends: a mutation the test did not expect to fail did.
 */
export function takeLoggedFailures(): string[] {
  const failures = consoleLines(true).slice(consoleRead.failures);
  consoleRead.failures += failures.length;
  return failures.map((line) =>
    line.slice(MUTATION_FAILURE.length).replace(/ failed [\s\S]*$/, ""),
  );
}

/**
 * Anything else written to `console.error` since the last reading. A test
 * that expects such a line reads it here; a line nobody read fails the test
 * when it ends (a warning of React, an error nobody caught).
 */
export function takeConsoleErrors(): string[] {
  const others = consoleLines(false).slice(consoleRead.others);
  consoleRead.others += others.length;
  return others;
}

// --- People and things the server answers with --------------------------------

export type CurrentUser = NonNullable<
  FunctionReturnType<typeof api.users.getCurrentUser>
>;

const NAMES: Record<CurrentUser["role"], [first: string, last: string]> = {
  admin: ["Alice", "Admin"],
  org_admin: ["Olivia", "Centre"],
  gestionnaire: ["Gaston", "Gestion"],
  user: ["Paul", "Patient"],
};

/** A signed-in account as `users.getCurrentUser` describes it, in a client organisation. */
export function account(
  role: CurrentUser["role"],
  overrides: Partial<CurrentUser> = {},
): CurrentUser {
  const [firstName, lastName] = NAMES[role];
  return {
    _id: `user-${role}` as Id<"users">,
    _creationTime: NOW - 86_400_000,
    clerkId: `clerk-${role}`,
    role,
    organization:
      role === "admin"
        ? {
            _id: "org-anheart" as Id<"organizations">,
            name: "Anheart",
            slug: "anheart",
          }
        : {
            _id: "org-centre-a" as Id<"organizations">,
            name: "Centre A",
            slug: "centre-a",
          },
    gestionnaireId: undefined,
    firstName,
    lastName,
    email: `${firstName.toLowerCase()}@example.test`,
    language: "fr",
    createdAt: NOW - 86_400_000,
    hrMax: undefined,
    birthYear: undefined,
    ...overrides,
  };
}

/** The visitor is this account from now on: `users.getCurrentUser` answers it. */
export function signedInAs(
  role: CurrentUser["role"],
  overrides: Partial<CurrentUser> = {},
): CurrentUser {
  const user = account(role, overrides);
  answer(api.users.getCurrentUser, user);
  return user;
}

export type Person = NonNullable<
  FunctionReturnType<typeof api.users.getUserById>
>;

/** An account as `users.getUserById` describes it to someone allowed to read it. */
export function person(overrides: Partial<Person> = {}): Person {
  return {
    _id: "user-rose" as Id<"users">,
    firstName: "Rose",
    lastName: "Rider",
    email: "rose@example.test",
    role: "user",
    language: "fr",
    gestionnaireId: undefined,
    createdAt: NOW - 30 * 86_400_000,
    hrMax: 175,
    birthYear: 1990,
    effectiveHrMax: 175,
    ...overrides,
  };
}

// --- Before and after each test -------------------------------------------------

// What jsdom lacks and the dashboard's Radix components call.
if (typeof window !== "undefined") {
  const element = window.Element.prototype;
  element.hasPointerCapture ??= () => false;
  element.setPointerCapture ??= () => {};
  element.releasePointerCapture ??= () => {};
  element.scrollIntoView ??= () => {};
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

beforeEach(() => {
  // Only the date is held still: timers run, so that what a page does after
  // a promise or a click is awaited as in a browser.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  convex.resetConvex();
  mounted.clear();
  for (const spy of Object.values(router)) spy.mockReset();
  missingMessages.length = 0;
  forgetServerAnswers();
  consoleRead.failures = 0;
  consoleRead.others = 0;
  consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
  vi.useRealTimers();
  const unexpected = takeConsoleErrors();
  const unread = takeLoggedFailures();
  consoleError?.mockRestore();
  expect(unexpected, "written to console.error").toEqual([]);
  expect(unread, "mutation failures the test did not read").toEqual([]);
  expect(
    missingMessages.splice(0),
    "messages missing from the catalog",
  ).toEqual([]);
});

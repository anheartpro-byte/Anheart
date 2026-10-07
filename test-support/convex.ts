/**
 * Convex seen from a component under test: what each query answers, what each
 * mutation is called with, and whether the visitor is signed in. No server
 * and no network.
 *
 * In the test file, before anything else:
 *
 *     vi.mock("convex/react", async () =>
 *       (await import("@/test-support/convex")).convexReact,
 *     );
 *
 * then `answer("training:listLaunchRights", [...])` before the render, and
 * `mutation("training:grantLaunchRight")` to read what the site sent.
 * Functions are named as Convex names them: `module:function`.
 *
 * The hook of the site that wraps every mutation (`useMutationWithFeedback`)
 * is not replaced: it runs for real over `useMutation` below, so the message
 * a test reads is the one the site would show.
 *
 * What this stand-in does not provide, so that no test leans on it:
 * - no subscription: an answer changes only when the test calls `answer` and
 *   renders again; nothing is pushed;
 * - no server: arguments are not checked against the validators of a
 *   function, a name is not checked to exist (a mistyped name in `answer`
 *   answers nothing), no rule of `convex/` runs;
 * - only what the site imports from `convex/react` today: `useQuery`,
 *   `useMutation`, `Authenticated`, `Unauthenticated`, `AuthLoading` and
 *   `useConvexAuth`. No `useAction`, no paginated query, no optimistic update.
 */

import { getFunctionName, type FunctionReference } from "convex/server";
import type { ReactNode } from "react";
import { vi, type Mock } from "vitest";

/** What a query answers: a value, or a function of the arguments the page asked with. */
type Answer = unknown | ((args: Record<string, unknown>) => unknown);

/** Whether Convex knows who the visitor is: signed in, signed out, or not known yet. */
export type Session = "signed-in" | "signed-out" | "loading";

const answers = new Map<string, Answer>();
const mutations = new Map<string, Mock>();
const asked: { name: string; args: unknown }[] = [];
/**
 * The function `useMutation` hands the page for each mutation, by name: the
 * same one at every render, as the real hook does. Kept for the whole test
 * file, so that a component still mounted never holds a function of the past.
 */
const callers = new Map<string, (...args: unknown[]) => unknown>();
let session: Session = "signed-in";

/** Sets what a query answers from now on. `undefined` is "still loading". */
export function answer(name: string, value: Answer) {
  answers.set(name, value);
}

/** The function the site calls when it runs this mutation. It resolves to `null` unless told otherwise. */
export function mutation(name: string): Mock {
  let known = mutations.get(name);
  if (known === undefined) {
    known = vi.fn(async () => null);
    mutations.set(name, known);
  }
  return known;
}

/**
 * The arguments the page asked a query with, at each render, oldest first:
 * `"skip"` when the page chose not to ask.
 */
export function asks(name: string): unknown[] {
  return asked
    .filter((entry) => entry.name === name)
    .map((entry) => entry.args);
}

/** Every mutation the site called, by name, with the arguments of each call. */
export function mutationCalls(): Record<string, unknown[][]> {
  return Object.fromEntries(
    [...mutations]
      .filter(([, sent]) => sent.mock.calls.length > 0)
      .map(([name, sent]) => [name, sent.mock.calls]),
  );
}

/** Says whether the visitor is signed in, for `Authenticated`, `Unauthenticated`, `AuthLoading` and `useConvexAuth`. */
export function sessionIs(state: Session) {
  session = state;
}

/** Forgets every answer, every mutation and every question, and signs the visitor in: call it before each test. */
export function resetConvex() {
  answers.clear();
  mutations.clear();
  asked.length = 0;
  session = "signed-in";
}

/** Draws its content in one state of the session, and nothing in the others. */
const shownWhen =
  (state: Session) =>
  ({ children }: { children?: ReactNode }) =>
    session === state ? children : null;

/** What stands for the `convex/react` module in a test. */
export const convexReact = {
  useQuery(reference: FunctionReference<"query">, args?: unknown) {
    const name = getFunctionName(reference);
    asked.push({ name, args });
    if (args === "skip") return undefined;
    const known = answers.get(name);
    return typeof known === "function"
      ? known((args ?? {}) as Record<string, unknown>)
      : known;
  },
  useMutation(reference: FunctionReference<"mutation">) {
    const name = getFunctionName(reference);
    let caller = callers.get(name);
    if (caller === undefined) {
      // What it calls is looked up at each call, so that a test may set the
      // mutation after the render, or reset it between two tests.
      caller = (...args: unknown[]) => mutation(name)(...args);
      callers.set(name, caller);
    }
    return caller;
  },
  Authenticated: shownWhen("signed-in"),
  Unauthenticated: shownWhen("signed-out"),
  AuthLoading: shownWhen("loading"),
  useConvexAuth: () => ({
    isLoading: session === "loading",
    isAuthenticated: session === "signed-in",
  }),
};

/**
 * Convex seen from a component under test: what each query answers, and what
 * each mutation is called with. No server and no network.
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
 */

import { getFunctionName, type FunctionReference } from "convex/server";
import { vi, type Mock } from "vitest";

/** What a query answers: a value, or a function of the arguments the page asked with. */
type Answer = unknown | ((args: Record<string, unknown>) => unknown);

const answers = new Map<string, Answer>();
const mutations = new Map<string, Mock>();
const asked: { name: string; args: unknown }[] = [];

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

/** Forgets every answer, every mutation and every question: call it before each test. */
export function resetConvex() {
  answers.clear();
  mutations.clear();
  asked.length = 0;
}

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
    // Looked up at each call, so that a test may set the mutation after the render.
    return (...args: unknown[]) => mutation(name)(...args);
  },
};

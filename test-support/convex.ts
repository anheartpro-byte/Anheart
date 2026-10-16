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
 *   `useQueries`, `usePaginatedQuery`, `useMutation`, `useAction`,
 *   `Authenticated`, `Unauthenticated`, `AuthLoading` and `useConvexAuth`. No
 *   optimistic update.
 *
 * A query the server failed is answered with an `Error`: `useQuery` throws
 * it, as Convex does, and `useQueries` hands it over as a value.
 *
 * A paginated query is answered page by page, as the server answers it: the
 * answer is a function of the arguments of one page (those of the page, plus
 * `paginationOpts: {numItems, cursor}`) that returns `{page, isDone,
 * continueCursor}`, `undefined` for a page still loading, or an `Error`, which
 * this stand-in of `usePaginatedQuery` always throws (`pagesOf` in
 * `test-support/pages.tsx` cuts a list into such pages). The hook asks for
 * the first page with `initialNumItems`, then for one more page at each
 * `loadMore`, from the `continueCursor` of the page before; each page it asks
 * for is recorded by `asks`. New arguments start again from the first page.
 * What it does not do: split a page, keep the end of a page when the data
 * changes (a reactive server does both), or send the `id` the real hook adds
 * to `paginationOpts`. The real hook handles an `InvalidCursor` error before
 * `throwOnError`: it logs the error and restarts from the first page. This
 * stand-in throws that error like any other `Error`.
 */

import { getFunctionName, type FunctionReference } from "convex/server";
import { useState, type ReactNode } from "react";
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

/** Sets what a query answers from now on. `undefined` is "still loading", an `Error` a failure of the server. */
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

/** What a query answers to these arguments, the question noted. */
function answerTo(
  reference: FunctionReference<"query">,
  args: unknown,
): unknown {
  const name = getFunctionName(reference);
  asked.push({ name, args });
  if (args === "skip") return undefined;
  const known = answers.get(name);
  return typeof known === "function"
    ? known((args ?? {}) as Record<string, unknown>)
    : known;
}

/** One page of a paginated query, as the server answers it. */
type Page = { page: unknown[]; isDone: boolean; continueCursor: string };

/** Where a paginated list stands, with the names `usePaginatedQuery` gives. */
type PaginationStatus =
  | "LoadingFirstPage"
  | "CanLoadMore"
  | "LoadingMore"
  | "Exhausted";

/**
 * The pages a paginated list holds: the first one, then one per `loadMore`
 * (`sizes`, the number of items of each), up to the first page still loading.
 */
function pagesHeld(
  reference: FunctionReference<"query">,
  args: Record<string, unknown>,
  sizes: number[],
): { results: unknown[]; status: PaginationStatus } {
  const results: unknown[] = [];
  let cursor: string | null = null;
  let status: PaginationStatus = "LoadingFirstPage";
  for (const [index, numItems] of sizes.entries()) {
    const pageArgs = { ...args, paginationOpts: { numItems, cursor } };
    const page = answerTo(reference, pageArgs) as Page | Error | undefined;
    if (page instanceof Error) throw page;
    if (page === undefined) {
      status = index === 0 ? "LoadingFirstPage" : "LoadingMore";
      break;
    }
    results.push(...page.page);
    cursor = page.continueCursor;
    status = page.isDone ? "Exhausted" : "CanLoadMore";
    if (page.isDone) break;
  }
  return { results, status };
}

/** Draws its content in one state of the session, and nothing in the others. */
const shownWhen =
  (state: Session) =>
  ({ children }: { children?: ReactNode }) =>
    session === state ? children : null;

/** What stands for the `convex/react` module in a test. */
export const convexReact = {
  useAction(reference: FunctionReference<"action">) {
    const name = getFunctionName(reference);
    let caller = callers.get(name);
    if (caller === undefined) {
      caller = (...args: unknown[]) => mutation(name)(...args);
      callers.set(name, caller);
    }
    return caller;
  },
  useQuery(reference: FunctionReference<"query">, args?: unknown) {
    const answered = answerTo(reference, args);
    if (answered instanceof Error) throw answered;
    return answered;
  },
  /** Each query of the request, by key: its answer, `undefined` while it loads, or its `Error`. */
  useQueries(
    request: Record<
      string,
      { query: FunctionReference<"query">; args: Record<string, unknown> }
    >,
  ) {
    return Object.fromEntries(
      Object.entries(request).map(([key, { query, args }]) => [
        key,
        answerTo(query, args),
      ]),
    );
  },
  usePaginatedQuery(
    reference: FunctionReference<"query">,
    args: Record<string, unknown> | "skip",
    options: { initialNumItems: number },
  ) {
    const key = JSON.stringify(args);
    // The sizes of the pages asked after the first, for these arguments only.
    const [more, setMore] = useState<{ key: string; sizes: number[] }>({
      key,
      sizes: [],
    });
    if (args === "skip") {
      answerTo(reference, args);
      return {
        results: [],
        status: "LoadingFirstPage" as const,
        isLoading: true,
        loadMore: () => {},
      };
    }
    const added = more.key === key ? more.sizes : [];
    const { results, status } = pagesHeld(reference, args, [
      options.initialNumItems,
      ...added,
    ]);
    return {
      results,
      status,
      isLoading: status === "LoadingFirstPage" || status === "LoadingMore",
      // As in Convex, a page is added only while the list can grow.
      loadMore: (numItems: number) => {
        if (status !== "CanLoadMore") return;
        setMore({ key, sizes: [...added, numItems] });
      },
    };
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

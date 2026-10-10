import { render } from "@/test-support/render";
import { useEffect } from "react";
import {
  AuthLoading,
  Authenticated,
  Unauthenticated,
  useConvexAuth,
} from "convex/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  mutation,
  mutationCalls,
  resetConvex,
  sessionIs,
} from "@/test-support/convex";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { dismissFeedback, getFeedbackToasts } from "@/lib/feedback";
import { useMutationWithFeedback } from "./use-mutation-with-feedback";

/**
 * The function `useMutationWithFeedback` hands a page is the same one at
 * every render: a page may name it in the dependency list of an effect or of
 * a callback without that effect running again at each render.
 *
 * The real `useMutation` of Convex returns a stable function, and the hook
 * keeps it so. The stand-in of `test-support/convex` does the same, which is
 * what lets this be proven without a server; the last tests hold the stand-in
 * to what it says of itself.
 */

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/convex")).convexReact,
);

const STOP = "training:requestStop";
const sessionId = "session-1" as Id<"sessions">;

type Run = ReturnType<
  typeof useMutationWithFeedback<typeof api.training.requestStop>
>;
/** The function the hook returned at each render, in order. */
const returned: Run[] = [];
/** One entry each time an effect that depends on that function ran. */
const effects: string[] = [];

/** A page that keeps the function and has an effect depending on it. */
function Page({ render: count }: { render: number }) {
  const requestStop = useMutationWithFeedback(api.training.requestStop);
  returned.push(requestStop);
  useEffect(() => {
    effects.push("ran");
  }, [requestStop]);
  return <p>{count}</p>;
}

beforeEach(() => {
  vi.useFakeTimers();
  resetConvex();
  returned.length = 0;
  effects.length = 0;
});
afterEach(() => {
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
  vi.useRealTimers();
});

describe("ANH-203 useMutationWithFeedback: the function it returns", () => {
  it("is the same function at every render of the page", () => {
    const screen = render(<Page render={1} />);
    screen.rerender(<Page render={2} />);
    screen.rerender(<Page render={3} />);

    expect(returned).toHaveLength(3);
    expect(returned[1]).toBe(returned[0]);
    expect(returned[2]).toBe(returned[0]);
  });

  it("does not make an effect that depends on it run again when the page is drawn again", () => {
    const screen = render(<Page render={1} />);
    screen.rerender(<Page render={2} />);
    screen.rerender(<Page render={3} />);

    expect(screen.text()).toBe("3");
    expect(effects).toEqual(["ran"]);
  });

  it("kept from the first render, still sends the mutation and reports its outcome after later renders", async () => {
    const screen = render(<Page render={1} />);
    screen.rerender(<Page render={2} />);
    // The answer of the server is decided after the page was drawn.
    mutation(STOP).mockResolvedValue(null);

    const outcome = await returned[0]({ sessionId }, { success: "Envoyé" });

    expect(mutationCalls()).toEqual({ [STOP]: [[{ sessionId }]] });
    expect(outcome).toEqual({ ok: true, value: null });
    expect(getFeedbackToasts().map(({ message }) => message)).toEqual([
      "Envoyé",
    ]);
  });

  it("is another function for another mutation", () => {
    const seen: unknown[] = [];
    function Two() {
      seen.push(
        useMutationWithFeedback(api.training.requestStop),
        useMutationWithFeedback(api.training.grantLaunchRight),
      );
      return null;
    }

    render(<Two />);

    expect(seen[0]).not.toBe(seen[1]);
  });
});

describe("ANH-203 the stand-in for Convex: who the visitor is", () => {
  function Gate() {
    const { isLoading, isAuthenticated } = useConvexAuth();
    return (
      <p>
        <Authenticated>signed in</Authenticated>
        <Unauthenticated>signed out</Unauthenticated>
        <AuthLoading>not known yet</AuthLoading>
        <i>
          {String(isLoading)}/{String(isAuthenticated)}
        </i>
      </p>
    );
  }

  it("draws the part of the page of a signed-in visitor unless a test says otherwise", () => {
    expect(render(<Gate />).text()).toBe("signed in false/true");
  });

  it.each([
    ["signed-out", "signed out false/false"],
    ["loading", "not known yet true/false"],
    ["signed-in", "signed in false/true"],
  ] as const)("a visitor who is %s sees only that part", (state, shown) => {
    sessionIs(state);

    expect(render(<Gate />).text()).toBe(shown);
  });

  it("signs the visitor in again when a test starts", () => {
    sessionIs("signed-out");

    resetConvex();

    expect(render(<Gate />).text()).toBe("signed in false/true");
  });
});

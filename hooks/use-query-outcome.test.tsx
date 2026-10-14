import { render } from "@/test-support/render";
import { getFunctionName, makeFunctionReference } from "convex/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useQueryOutcome, type QueryOutcome } from "./use-query-outcome";

/**
 * A query whose failure the page shows instead of falling over: the outcome
 * says loading, failed or ready, and the subscription is the one `useQuery`
 * would hold, kept while the arguments keep their value.
 */

type Request = Record<string, { query: unknown; args: Record<string, unknown> }>;

const convex = vi.hoisted(() => ({
  requests: [] as Request[],
  answer: undefined as unknown,
}));

vi.mock("convex/react", () => ({
  useQueries: (request: Request) => {
    convex.requests.push(request);
    return Object.fromEntries(
      Object.keys(request).map((key) => [key, convex.answer]),
    );
  },
}));

const counters = makeFunctionReference<
  "query",
  { scope?: string },
  { active: number }
>("dashboard:counters");

const seen: QueryOutcome<{ active: number }>[] = [];

function Probe({ args }: { args: { scope?: string } | "skip" }) {
  seen.push(useQueryOutcome(counters, args));
  return null;
}

beforeEach(() => {
  convex.requests.length = 0;
  convex.answer = undefined;
  seen.length = 0;
});

describe("useQueryOutcome", () => {
  it("is loading until the server answers, then ready with the answer", () => {
    const screen = render(<Probe args={{}} />);
    expect(seen.at(-1)).toEqual({ status: "loading" });

    convex.answer = { active: 2 };
    screen.rerender(<Probe args={{}} />);
    expect(seen.at(-1)).toEqual({ status: "ready", value: { active: 2 } });
  });

  it("hands over a failure of the server instead of throwing it", () => {
    const failure = new Error("Server Error");
    convex.answer = failure;
    render(<Probe args={{}} />);
    expect(seen.at(-1)).toEqual({ status: "failed", error: failure });
  });

  it("asks nothing for a skipped query, and stays loading", () => {
    convex.answer = { active: 2 };
    render(<Probe args="skip" />);
    expect(convex.requests.at(-1)).toEqual({});
    expect(seen.at(-1)).toEqual({ status: "loading" });
  });

  it("asks the query by its name with its arguments", () => {
    render(<Probe args={{ scope: "centre-a" }} />);
    const [request] = Object.values(convex.requests.at(-1) ?? {});
    expect(getFunctionName(request.query as typeof counters)).toBe(
      "dashboard:counters",
    );
    expect(request.args).toEqual({ scope: "centre-a" });
  });

  it("keeps one request while the arguments keep their value, a new one when they change", () => {
    const screen = render(<Probe args={{ scope: "centre-a" }} />);
    screen.rerender(<Probe args={{ scope: "centre-a" }} />);
    expect(convex.requests).toHaveLength(2);
    expect(convex.requests[1]).toBe(convex.requests[0]);

    screen.rerender(<Probe args={{ scope: "centre-b" }} />);
    expect(convex.requests[2]).not.toBe(convex.requests[1]);
    expect(Object.values(convex.requests[2])[0].args).toEqual({
      scope: "centre-b",
    });
  });
});

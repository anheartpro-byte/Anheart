import { useMemo } from "react";
import { useQueries, type RequestForQueries } from "convex/react";
import {
  getFunctionName,
  makeFunctionReference,
  type FunctionArgs,
  type FunctionReference,
  type FunctionReturnType,
} from "convex/server";
import { convexToJson, jsonToConvex, type Value } from "convex/values";

/** Where a query stands: still loading, failed on the server, or answered. */
export type QueryOutcome<T> =
  | { status: "loading" }
  | { status: "failed"; error: Error }
  | { status: "ready"; value: T };

const KEY = "query";

/**
 * A Convex query whose failure is a state for the page to show, not an
 * exception that takes the whole page down: `useQuery` throws what the server
 * threw, this hook returns it. A query skipped stays "loading", as `useQuery`
 * answers `undefined`.
 *
 * The subscription is the one `useQuery` would open: arguments equal in value
 * keep it, whatever object carries them.
 */
export function useQueryOutcome<Q extends FunctionReference<"query">>(
  query: Q,
  args: FunctionArgs<Q> | "skip",
): QueryOutcome<FunctionReturnType<Q>> {
  const name = getFunctionName(query);
  const serialized =
    args === "skip" ? null : JSON.stringify(convexToJson(args as Value));
  const request = useMemo((): RequestForQueries => {
    if (serialized === null) return {};
    return {
      [KEY]: {
        query: makeFunctionReference<"query">(name),
        args: jsonToConvex(JSON.parse(serialized)) as Record<string, Value>,
      },
    };
  }, [name, serialized]);
  const answered: unknown = useQueries(request)[KEY];
  if (answered === undefined) return { status: "loading" };
  if (answered instanceof Error) return { status: "failed", error: answered };
  return { status: "ready", value: answered as FunctionReturnType<Q> };
}

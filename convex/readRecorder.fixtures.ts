/**
 * Records the reads a query makes: the table, the index and what was asked of
 * each range. convex-test counts returned documents, not rows a filter
 * rejects, so a filtered scan only shows here (sessionListBounds.test.ts).
 */
import type { QueryCtx } from "./_generated/server";

type RecordedRead = {
  readonly table: string;
  readonly index: unknown;
  readonly calls: (
    | { method: "filter" | "paginate" | "collect" }
    | { method: "take"; limit: unknown }
  )[];
};

export function recordReads(ctx: QueryCtx) {
  const reads: RecordedRead[] = [];
  function record<T extends object>(
    query: T,
    table: string,
    read?: RecordedRead,
  ): T {
    return new Proxy(query, {
      get(target, key, receiver) {
        const method: unknown = Reflect.get(target, key, receiver);
        if (typeof method !== "function") return method;
        return (...args: unknown[]) => {
          let source = read;
          if (key === "withIndex" || source === undefined) {
            source = {
              table,
              index: key === "withIndex" ? args[0] : undefined,
              calls: [],
            };
            reads.push(source);
          }
          if (key === "filter" || key === "paginate" || key === "collect") {
            source.calls.push({ method: key });
          } else if (key === "take") {
            source.calls.push({ method: key, limit: args[0] });
          }
          const result: unknown = Reflect.apply(method, target, args);
          if (
            (key === "withIndex" || key === "order" || key === "filter") &&
            typeof result === "object" &&
            result !== null
          ) {
            return record(result, table, source);
          }
          return result;
        };
      },
    });
  }
  const db = new Proxy(ctx.db, {
    get(target, key, receiver) {
      if (key === "query") {
        return (table: Parameters<QueryCtx["db"]["query"]>[0]) =>
          record(target.query(table), table);
      }
      return Reflect.get(target, key, receiver);
    },
  });
  return { ctx: { ...ctx, db }, reads };
}

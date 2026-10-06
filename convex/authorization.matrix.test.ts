/// <reference types="vite/client" />
/**
 * ANH-132: drives the authorization matrix. One test per cell of
 * `authorization.matrix.ts`. Each cell seeds a fresh in-memory world, acts as
 * the cell's actor, and asserts the behaviour (effect or returned data), not
 * the implementation. Cells marked as a known harmless defect run with
 * `it.fails`, asserting the intended policy so they flip the day it is fixed.
 */
import { describe, expect, it } from "vitest";
import {
  MATRIX,
  type Entry,
  type Expectation,
} from "./authorization.matrix";
import { as, modules, seedWorld, type World } from "./test.setup";

function isEmpty(res: unknown): boolean {
  return (
    res === null ||
    res === undefined ||
    (Array.isArray(res) && res.length === 0)
  );
}

function invoke(
  entry: Entry,
  reader: ReturnType<typeof as>,
  args: Record<string, unknown>,
): Promise<unknown> {
  // The matrix holds heterogeneous function references; the per-cell `build`
  // produces the matching args, so a single untyped dispatch is used here.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const r = reader as any;
  return entry.kind === "query"
    ? r.query(entry.ref, args)
    : r.mutation(entry.ref, args);
}

for (const entry of MATRIX) {
  describe(entry.id, () => {
    for (const c of entry.cases) {
      const scopeLabel = c.scope && c.scope !== "none" ? `/${c.scope}` : "";
      const title = `${c.actor}${scopeLabel}: ${c.expect.outcome} - ${c.note}`;
      const runner = c.knownDefect ? it.fails : it;
      runner(title, async () => {
        const w: World = await seedWorld(modules);
        const scope = c.scope ?? "none";
        const args = await entry.build(w, c.actor, scope);
        const reader = as(w.t, c.actor);
        const exp: Expectation = c.knownDefect ? c.knownDefect.intended : c.expect;

        if (exp.outcome === "refuse") {
          await expect(invoke(entry, reader, args)).rejects.toThrow(exp.message);
          return;
        }

        const res = await invoke(entry, reader, args);
        if (exp.outcome === "empty") {
          expect(isEmpty(res)).toBe(true);
        } else if (exp.outcome === "filtered") {
          expect(isEmpty(res)).toBe(false);
          await entry.onFiltered?.(res, w, c.actor, scope);
        } else {
          if (entry.onSuccess) {
            await entry.onSuccess(res, w, c.actor, scope, args);
          } else {
            expect(isEmpty(res)).toBe(false);
          }
        }
      });
    }
  });
}

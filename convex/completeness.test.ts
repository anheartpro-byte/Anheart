/// <reference types="vite/client" />
/**
 * ANH-132 completeness gate. It enumerates the PUBLIC exports of the top-level
 * `convex/*.ts` modules (query, mutation, action) and the HTTP routes of
 * `convex/http.ts`, and fails if any one of them is missing from the
 * authorization matrix (`authorization.matrix.ts`) or the route-coverage list.
 *
 * Adding a new public function or route without a matrix row / named test makes
 * this test fail, which is the point: the policy table can never silently fall
 * behind the surface it is meant to describe.
 */
import { describe, expect, it } from "vitest";
import http from "./http";
import { MATRIX, ROUTE_COVERAGE } from "./authorization.matrix";
import { ORG_B_ACTORS } from "./test.setup";

type Registration = {
  isPublic?: boolean;
  isQuery?: boolean;
  isMutation?: boolean;
  isAction?: boolean;
};

// Every top-level Convex module, eagerly, minus the test-only files (which must
// never be imported here, or their top-level suites would run again).
const topLevel = import.meta.glob(
  [
    "./*.ts",
    "!./*.test.ts",
    "!./*.fixtures.ts",
    "!./*.setup.ts",
    "!./*.matrix.ts",
  ],
  { eager: true },
) as Record<string, Record<string, unknown>>;

/** Public function identifiers actually registered in the backend, e.g. "users.listUsers". */
function discoverPublicFunctions(
  mods: Record<string, Record<string, unknown>>,
): string[] {
  const found: string[] = [];
  for (const [path, mod] of Object.entries(mods)) {
    const moduleName = path.replace(/^\.\//, "").replace(/\.ts$/, "");
    for (const [exportName, value] of Object.entries(mod)) {
      const f = value as Registration | null;
      // Registered Convex functions are callable, so allow function or object.
      if (
        f &&
        (typeof f === "object" || typeof f === "function") &&
        f.isPublic === true &&
        (f.isQuery || f.isMutation || f.isAction)
      ) {
        found.push(`${moduleName}.${exportName}`);
      }
    }
  }
  return found.sort();
}

/** Identifiers in `required` that are absent from `covered`. The gate's core. */
function missing(required: string[], covered: Set<string>): string[] {
  return required.filter((id) => !covered.has(id)).sort();
}

const coveredFunctions = new Set(MATRIX.map((entry) => entry.id));
const coveredRoutes = new Set(
  ROUTE_COVERAGE.map((r) => `${r.method} ${r.path}`),
);

function discoverRoutes(): string[] {
  return http
    .getRoutes()
    .map(([path, method]) => `${method} ${path}`)
    .sort();
}

describe("ANH-132 completeness of the authorization matrix", () => {
  it("covers every public Convex function", () => {
    const discovered = discoverPublicFunctions(topLevel);
    expect(discovered.length).toBeGreaterThanOrEqual(54);
    expect(missing(discovered, coveredFunctions)).toEqual([]);
  });

  it("has no matrix row for a function that no longer exists", () => {
    const discovered = new Set(discoverPublicFunctions(topLevel));
    const stale = MATRIX.map((e) => e.id).filter((id) => !discovered.has(id));
    expect(stale).toEqual([]);
  });

  it("covers every machine HTTP route", () => {
    const discovered = discoverRoutes();
    expect(discovered.length).toBe(10);
    expect(missing(discovered, coveredRoutes)).toEqual([]);
  });

  it("has no route-coverage entry for a route that no longer exists", () => {
    const discovered = new Set(discoverRoutes());
    const stale = [...coveredRoutes].filter((r) => !discovered.has(r)).sort();
    expect(stale).toEqual([]);
  });

  // ANH-114: no public function escapes the two-organisation check. Every
  // entry has a cell acted by a member of centre B, and wherever the function
  // takes an identifier or lists rows, such a cell is refused, empty or
  // filtered to centre B.
  it("checks every public function from another organisation", () => {
    const foreign = new Set<string>(ORG_B_ACTORS);
    const unchecked = MATRIX.filter(
      (entry) => !entry.cases.some((c) => foreign.has(c.actor)),
    ).map((entry) => entry.id);
    expect(unchecked).toEqual([]);
  });

  it("never lets another organisation succeed on a resource named by identifier", () => {
    const leaking = MATRIX.flatMap((entry) =>
      entry.cases
        .filter((c) => c.scope === "foreign" && c.expect.outcome === "success")
        .map((c) => `${entry.id} (${c.actor})`),
    );
    expect(leaking).toEqual([]);
  });

  // A function that takes the identifier of an organisation-scoped resource
  // has a "foreign" cell: called from centre B with an identifier of centre A.
  // The only entries without one are the functions listed here, which act on
  // the caller alone or list the caller's own organisation.
  it("calls every function that takes an identifier with one of another organisation", () => {
    const withoutForeignCell = MATRIX.filter(
      (entry) => !entry.cases.some((c) => c.scope === "foreign"),
    )
      .map((entry) => entry.id)
      .sort();
    expect(withoutForeignCell).toEqual([
      "machines.createMachine", // Anheart admin only; refused to centre B by role
      "machines.listMachines",
      "sessions.getCompletedSessionsForUser",
      "softwareReleases.listReleases", // Anheart-wide register, Anheart admin only
      "softwareReleases.recordRelease", // Anheart-wide register, Anheart admin only
      "training.listLaunchableMachines",
      "users.createPatient", // creates in the caller's organisation
      "users.getCurrentUser",
      "users.getOrCreateUser",
      "users.listGestionnaires",
      "users.updateUserProfile",
    ]);
  });

  // Proof that the gate has teeth: a throwaway public export that no matrix row
  // mentions is reported as missing. (Demonstrated end-to-end in a scratch copy
  // of `convex/` as well; see the pull request.)
  it("reports an uncovered public function as missing", () => {
    const withScratch = {
      ...topLevel,
      "./scratch.ts": {
        hiddenQuery: { isPublic: true, isQuery: true },
      } as Record<string, unknown>,
    };
    const discovered = discoverPublicFunctions(withScratch);
    expect(discovered).toContain("scratch.hiddenQuery");
    expect(missing(discovered, coveredFunctions)).toEqual(["scratch.hiddenQuery"]);
  });
});

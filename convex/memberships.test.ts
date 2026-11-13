/// <reference types="vite/client" />
/**
 * ANH-152: every write of a `memberships` row goes through
 * `convex/lib/memberships.ts`, which releases a member's console operator rows
 * when the membership no longer lets them be one. This pins it: a module that
 * inserts a membership, or patches, replaces or deletes one, by itself fails
 * here, before a member can leave the operator roles and keep their code.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const CONVEX = path.dirname(new URL(import.meta.url).pathname);
const HELPER = path.join("lib", "memberships.ts");

/** The deployed sources: neither the tests nor what only the tests load. */
function sources(directory: string, relative = ""): string[] {
  return readdirSync(path.join(directory, relative)).flatMap((entry) => {
    const file = path.join(relative, entry);
    if (statSync(path.join(directory, file)).isDirectory())
      return entry === "_generated" ? [] : sources(directory, file);
    const testOnly =
      entry.endsWith(".test.ts") ||
      entry.endsWith(".fixtures.ts") ||
      entry.endsWith(".matrix.ts") ||
      entry === "test.setup.ts";
    return entry.endsWith(".ts") && !testOnly ? [file] : [];
  });
}

/** What writes a membership row other than through the helper, as `file: the call`. */
const WRITES = [
  /insert\(\s*"memberships"/g,
  /\.(?:patch|replace|delete)\(\s*[\w.]*[mM]embership\w*\._id/g,
  /\.(?:patch|replace)\([^;]*?\{[^}]*\bactive\b\s*:/g,
];

describe("ANH-152 the writes of a membership", () => {
  it("all go through convex/lib/memberships.ts", () => {
    const found = sources(CONVEX)
      .filter((file) => file !== HELPER)
      .flatMap((file) => {
        const text = readFileSync(path.join(CONVEX, file), "utf8");
        return WRITES.flatMap((pattern) =>
          [...text.matchAll(pattern)].map((match) => `${file}: ${match[0]}`),
        );
      });
    expect(found).toEqual([]);
  });

  it("finds a write that does not", () => {
    const text = 'await ctx.db.patch(membership._id, { role: "user" });\n' +
      'await ctx.db.insert("memberships", fields);\n' +
      "await ctx.db.patch(row._id, { active: false });";
    expect(WRITES.flatMap((pattern) => [...text.matchAll(pattern)]).length).toBe(3);
  });
});

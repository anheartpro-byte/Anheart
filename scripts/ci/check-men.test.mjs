import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

const scenarios = [
  { name: "pending threat catalogue", files: {}, status: 0 },
  { name: "heading identifier resolves", files: { "menaces.md": "## MEN-01\n", "rule.md": "MEN-01\n" }, status: 0 },
  { name: "table identifier resolves", files: { "menaces.md": "| MEN-01 | see MEN-02 |\n## MEN-02\n" }, status: 0 },
  { name: "duplicate identifier refuses", files: { "menaces.md": "## MEN-01\n| MEN-01 | duplicate |\n" }, status: 1 },
  { name: "undefined identifier refuses", files: { "menaces.md": "## MEN-01\n", "rule.md": "MEN-02\n" }, status: 1 },
  { name: "empty catalogue refuses", files: { "menaces.md": "# Catalogue\n" }, status: 1 },
];

for (const { name, files, status } of scenarios) {
  test(name, () => {
    const directory = mkdtempSync(join(tmpdir(), "anheart-men-test-"));
    try {
      for (const [path, content] of Object.entries(files)) {
        writeFileSync(join(directory, path), content);
      }
      const result = spawnSync("bash", ["scripts/ci/check-men.sh", directory], { encoding: "utf8" });
      assert.ifError(result.error);
      assert.equal(result.status, status, result.stderr);
    } finally {
      rmSync(directory, { recursive: true });
    }
  });
}

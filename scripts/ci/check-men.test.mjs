import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

const registry = readFileSync(new URL("men-linear-issues.tsv", import.meta.url), "utf8");
const issue = "[ANH-121](https://linear.app/anheart/issue/ANH-121/cle-machine-reversible-et-machine-supprimee-encore-authentifiee)";
const open = `## MEN-01\n- Status: OPEN\n- Issues: ${issue}\n`;
const mitigated = open.replace("OPEN", "MITIGATED") + "- Evidence: [proof](proof.md)\n";
const accepted = mitigated.replace("MITIGATED", "ACCEPTED");
const scenarios = [
  { name: "refuses missing catalogue", files: {}, status: 1 },
  { name: "resolves open threat to verified existing issue", files: { "menaces.md": open, "rule.md": "MEN-01\n" }, status: 0 },
  { name: "refuses duplicate definition", files: { "menaces.md": open + open }, status: 1 },
  { name: "refuses undefined reference", files: { "menaces.md": open, "rule.md": "MEN-02\n" }, status: 1 },
  { name: "refuses empty catalogue", files: { "menaces.md": "# Catalogue\n" }, status: 1 },
  { name: "refuses open threat without issue", files: { "menaces.md": "## MEN-01\n- Status: OPEN\n" }, status: 1 },
  { name: "refuses nonexistent Linear issue with valid format", files: { "menaces.md": open.replaceAll("ANH-121", "ANH-999999") }, status: 1 },
  { name: "refuses known issue linked to another issue", files: { "menaces.md": open.replace("/ANH-121/", "/ANH-165/") }, status: 1 },
  { name: "refuses known issue linked to foreign host", files: { "menaces.md": open.replace("linear.app", "example.org") }, status: 1 },
  { name: "refuses status omission", files: { "menaces.md": open.replace("- Status: OPEN\n", "") }, status: 1 },
  { name: "refuses unknown status", files: { "menaces.md": open.replace("OPEN", "OPNE") }, status: 1 },
  { name: "refuses unsigned acceptance", files: { "menaces.md": open.replace("OPEN", "ACCEPTED") }, status: 1 },
  { name: "refuses mitigation without evidence", files: { "menaces.md": open.replace("OPEN", "MITIGATED") }, status: 1 },
  { name: "refuses duplicate status", files: { "menaces.md": open + "- Status: MITIGATED\n" }, status: 1 },
  { name: "refuses duplicate issue fields", files: { "menaces.md": open + `- Issues: ${issue}\n` }, status: 1 },
  { name: "refuses one uncovered threat among covered threats", files: { "menaces.md": open + "## MEN-02\n- Status: OPEN\n" }, status: 1 },
  { name: "refuses malformed identifier", files: { "menaces.md": open.replace("MEN-01", "MEN-1") }, status: 1 },
  { name: "refuses malformed registry", files: { "menaces.md": open }, registry: "ANH-121\n", status: 1 },
  { name: "refuses duplicate registry issue", files: { "menaces.md": open }, registry: registry + registry, status: 1 },
  { name: "refuses one unverified issue beside a verified issue", files: { "menaces.md": open.trimEnd() + ", [ANH-999999](https://linear.app/anheart/issue/ANH-999999/unknown)\n" }, status: 1 },
  { name: "resolves multiple covered threats", files: { "menaces.md": open + open.replace("MEN-01", "MEN-02") }, status: 0 },
  { name: "accepts documented mitigation with evidence", files: { "menaces.md": mitigated, "proof.md": "Synthetic verification record\n" }, status: 0 },
  { name: "refuses empty mitigation evidence", files: { "menaces.md": mitigated, "proof.md": "" }, status: 1 },
  { name: "refuses missing mitigation evidence file", files: { "menaces.md": mitigated }, status: 1 },
  { name: "refuses evidence escaping documentation", files: { "menaces.md": mitigated.replace("(proof.md)", "(../proof.md)") }, status: 1 },
  { name: "refuses acceptance without named signer", files: { "menaces.md": accepted + "- Reviewed-on: 2026-10-05\n", "proof.md": "Synthetic decision fixture\n" }, status: 1 },
  { name: "refuses acceptance without review date", files: { "menaces.md": accepted + "- Signed-by: Synthetic fixture signer\n", "proof.md": "Synthetic decision fixture\n" }, status: 1 },
  { name: "accepts acceptance metadata with evidence for human review", files: { "menaces.md": accepted + "- Signed-by: Synthetic fixture signer\n- Reviewed-on: 2026-10-05\n", "proof.md": "Synthetic decision fixture, not a human risk acceptance\n" }, status: 0 },
];

for (const scenario of scenarios) {
  test(scenario.name, () => {
    // Given: isolated documents and the actual verified issue snapshot.
    const directory = mkdtempSync(join(tmpdir(), "anheart-men-test-"));
    try {
      for (const [path, content] of Object.entries(scenario.files)) {
        writeFileSync(join(directory, path), content);
      }
      const issueFile = join(directory, "issues.tsv");
      writeFileSync(issueFile, scenario.registry ?? registry);
      // When: the same command used by the documentation CI runs.
      const result = spawnSync("bash", ["scripts/ci/check-men.sh", directory, issueFile], { encoding: "utf8" });
      // Then: it accepts covered threats and refuses broken traceability.
      assert.ifError(result.error);
      assert.equal(result.status, scenario.status, result.stderr || result.stdout);
    } finally {
      rmSync(directory, { recursive: true });
    }
  });
}

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, readFileSync } from "node:fs";
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
  // ANH-221: a ticket named in the text, outside any Issues field, once escaped the registry.
  { name: "accepts a ticket named in the text that the registry carries", files: { "menaces.md": open + "\nVoir aussi ANH-165.\n" }, status: 0 },
  { name: "refuses a ticket named in the text that the registry does not carry", files: { "menaces.md": open + "\nVoir aussi ANH-999999.\n" }, status: 1 },
  { name: "refuses a ticket named in a heading that the registry does not carry", files: { "menaces.md": "# Menaces (ANH-999999)\n\n" + open }, status: 1 },
  { name: "accepts a Linear link in the text that matches the registry", files: { "menaces.md": open + "\nVoir [ANH-165](https://linear.app/anheart/issue/ANH-165/convex-rotation-revocation-et-expiration-des-cles-machine-avec).\n" }, status: 0 },
  { name: "refuses a Linear link in the text whose address is not the registry's", files: { "menaces.md": open + "\nVoir [ANH-165](https://linear.app/anheart/issue/ANH-165/convex-rotation).\n" }, status: 1 },
  { name: "refuses a Linear link in the text whose label names another ticket", files: { "menaces.md": open + `\nVoir ${issue.replace("[ANH-121]", "[ANH-165]")}.\n` }, status: 1 },
  { name: "refuses a Linear link in the text to a ticket the registry does not carry", files: { "menaces.md": open + "\nVoir [la décision](https://linear.app/anheart/issue/ANH-999999/unknown).\n" }, status: 1 },
];

for (const scenario of scenarios) {
  test(scenario.name, () => {
    // Given: isolated documents and the actual verified issue snapshot.
    const sandbox = mkdtempSync(join(tmpdir(), "anheart-men-test-"));
    const directory = join(sandbox, "docs");
    try {
      mkdirSync(directory);
      writeFileSync(join(sandbox, "proof.md"), "Synthetic sibling evidence\n");
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
      rmSync(sandbox, { recursive: true });
    }
  });
}

// ANH-221: the page says, in one sentence, when the tickets of the registry
// were read, and how many rows carry each date. That sentence had fallen behind
// the registry once already, which the review of the first release found: the
// two are held together here.
test("docs/menaces.md gives the read dates the registry carries, with the number of rows of each, and no other", () => {
  // Given: the number of rows of the real registry for each date, and the sentence of the real page.
  /** @type {Map<string, number>} */
  const carried = new Map();
  for (const row of registry.split(/\r?\n/)) {
    if (!row || row.startsWith("#")) continue;
    const date = row.split("\t")[2] ?? "";
    carried.set(date, (carried.get(date) ?? 0) + 1);
  }
  const page = readFileSync(new URL("../../docs/menaces.md", import.meta.url), "utf8").replace(/\s+/g, " ");
  // When: the sentence is read from its colon up to its full stop.
  const sentence = /Chaque ligne porte sa date de lecture[^:]*:([^.]+)\./.exec(page);
  assert.ok(sentence, "the sentence that gives the read dates");
  const dates = sentence[1]?.match(/\d{4}-\d{2}-\d{2}/g) ?? [];
  const stated = new Map([...(sentence[1] ?? "").matchAll(/(\d{4}-\d{2}-\d{2}) pour (\d+) lignes?/g)].map((m) => [m[1], Number(m[2])]));
  // Then: each date comes with its number of rows, and the dates and numbers are those of the registry.
  assert.equal(stated.size, dates.length, `each date of the sentence gives its number of rows: ${sentence[1]}`);
  assert.deepEqual([...stated].sort(), [...carried].sort());
});

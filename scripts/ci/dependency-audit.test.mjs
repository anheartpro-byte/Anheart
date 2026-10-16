// ANH-153: the exceptions of the dependency audit, what Dependabot watches,
// the one JavaScript package manager, and the files of secrets Git ignores.
//
// Run by the `docs` job, without any install and without the network: the
// files are read as text, and scripts/ci/audit-exceptions.mjs is driven with
// throwaway tables and reports. The audits themselves (npm audit, pip-audit,
// gitleaks) run in the `audit` job, which reaches the registries; what that
// job runs is held by scripts/ci/ci-workflow.test.mjs.

import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { COLUMNS, FILE, MAX_DAYS, SECTION, judgeNpm, main, parse, pipAuditArgs, readNpmReport } from "./audit-exceptions.mjs";

const root = fileURLToPath(new URL("../..", import.meta.url));
const atRoot = (/** @type {string} */ path) => readFileSync(join(root, path), "utf8");
const tracked = execFileSync("git", ["ls-files", "-z"], { cwd: root, encoding: "utf8" }).split("\0").filter(Boolean);

const TODAY = "2026-10-14";
const LINK = "[ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets)";
const HEAD = `| ${COLUMNS.join(" | ")} |\n|${COLUMNS.map(() => "---").join("|")}|\n`;
/** A file of exceptions holding these rows. @param {string[]} rows */
const table = (rows) => `# Exceptions\n\nRègles.\n\n${SECTION}\n\n${HEAD}${rows.map((row) => `${row}\n`).join("")}\nTexte.\n\n## Après\n\n| a | b |\n|---|---|\n`;
/** One row; each cell can be replaced. @param {Partial<Record<string, string>>} cells */
const row = (cells = {}) => {
  const values = { id: "GHSA-jf85-cpcp-j695", ecosystem: "npm", name: "lodash", added: "2026-10-01", expires: "2026-12-01", ticket: LINK, reason: "corrigé par ANH-153", ...cells };
  return `| ${values.id} | ${values.ecosystem} | ${values.name} | ${values.added} | ${values.expires} | ${values.ticket} | ${values.reason} |`;
};

// --- The table of exceptions -----------------------------------------------

test("the committed table of exceptions follows the rules and holds no expired exception", () => {
  const text = atRoot(FILE);
  assert.deepEqual(parse(text, new Date().toISOString().slice(0, 10)).problems, []);
  assert.ok(text.includes(`\n${SECTION}\n\n${HEAD}`), "the section and the header of the table are not as the script reads them");
  // The rules a reader is given are the ones the script applies.
  assert.match(text, new RegExp(`\\*\\*${MAX_DAYS} jours au plus\\*\\*`));
  assert.match(text, /Dès le jour d'expiration, le job `audit` échoue/);
});

test("a well-formed exception is read, and pip-audit is told to skip the Python ones only", () => {
  const pip = row({ id: "PYSEC-2019-132", ecosystem: "pip", name: "urllib3" });
  const { exceptions, problems } = parse(table([row(), pip, row({ id: "GHSA-aaaa-bbbb-cccc", ecosystem: "pip", name: "numpy" })]), TODAY);
  assert.deepEqual(problems, []);
  assert.deepEqual(exceptions[0], {
    id: "GHSA-jf85-cpcp-j695",
    ecosystem: "npm",
    package: "lodash",
    added: "2026-10-01",
    expires: "2026-12-01",
    ticket: "ANH-153",
    reason: "corrigé par ANH-153",
  });
  assert.equal(pipAuditArgs(exceptions), "--ignore-vuln PYSEC-2019-132 --ignore-vuln GHSA-aaaa-bbbb-cccc");
  assert.equal(pipAuditArgs([]), "");
  // An empty table is the usual case.
  assert.deepEqual(parse(table([]), TODAY), { exceptions: [], problems: [] });
});

test("an exception lasts 90 days at most, and stops applying on its expiry day", () => {
  const last = (/** @type {string} */ added, /** @type {string} */ expires, /** @type {string} */ today) =>
    parse(table([row({ added, expires })]), today).problems;
  assert.deepEqual(last("2026-10-01", "2026-12-30", "2026-10-14"), [], "90 days");
  assert.match(last("2026-10-01", "2026-12-31", "2026-10-14").join(), /lasts 91 days, 90 at most/);
  assert.deepEqual(last("2026-10-01", "2026-10-15", "2026-10-14"), [], "the day before its expiry");
  assert.match(last("2026-10-01", "2026-10-14", "2026-10-14").join(), /expired on 2026-10-14/);
  assert.match(last("2026-10-01", "2026-10-14", "2027-03-01").join(), /expired on 2026-10-14/);
  assert.match(last("2026-10-20", "2026-11-20", "2026-10-14").join(), /added on 2026-10-20, after today/);
  assert.match(last("2026-10-10", "2026-10-10", "2026-10-09").join(), /not after the day it was added/);
});

for (const [name, cells, refusal] of /** @type {const} */ ([
  ["an unknown ecosystem", { ecosystem: "pypi" }, /ecosystem "pypi", npm or pip expected/],
  ["an npm exception named as pip-audit names one", { id: "PYSEC-2019-132" }, /is not one npm audit prints/],
  ["a Python exception named by no identifier pip-audit prints", { id: "urllib3-1", ecosystem: "pip" }, /is not one pip-audit prints/],
  // pip-audit would skip every advisory that carries the CVE as an alias, in any package.
  ["a Python exception named by a CVE", { id: "CVE-2019-11324", ecosystem: "pip", name: "urllib3" }, /is not one pip-audit prints/],
  ["no package", { name: "" }, /package ""/],
  ["a date that does not exist", { added: "2026-02-30" }, /added "2026-02-30" is not a date/],
  ["a date written otherwise", { expires: "01/12/2026" }, /expires "01\/12\/2026" is not a date/],
  ["a ticket that is no link", { ticket: "ANH-153" }, /ticket "ANH-153" is not a link/],
  ["a link to another ticket than it names", { ticket: "[ANH-153](https://linear.app/anheart/issue/ANH-154/autre)" }, /is not a link/],
  ["a link outside Linear", { ticket: "[ANH-153](https://example.com/issue/ANH-153)" }, /is not a link/],
  ["no reason", { reason: "" }, /no reason/],
])) {
  test(`an exception is refused for ${name}`, () => {
    const { exceptions, problems } = parse(table([row(cells)]), TODAY);
    assert.deepEqual(exceptions, []);
    assert.equal(problems.length, 1);
    assert.match(problems[0] ?? "", refusal);
  });
}

test("a row with a cell too many, or written twice, is refused; the other rows are still read", () => {
  const { exceptions, problems } = parse(table([row({ reason: "a | b" }), row(), row(), row({ ecosystem: "pip", id: "GHSA-jf85-cpcp-j695" })]), TODAY);
  assert.deepEqual(problems.map((problem) => problem.replace(/^.*": /, "")), ["8 cells, 7 expected (no \"|\" in a cell)", "written twice"]);
  // The same advisory may be excepted in each ecosystem, once.
  assert.deepEqual(exceptions.map(({ ecosystem, id }) => `${ecosystem} ${id}`), ["npm GHSA-jf85-cpcp-j695", "pip GHSA-jf85-cpcp-j695"]);
});

test("a file without the section, the header or the separator of its table is refused", () => {
  assert.match(parse("# Exceptions\n\n| a |\n", TODAY).problems.join(), /no section "## Exceptions en cours"/);
  assert.match(parse(`${SECTION}\n\nAucune.\n`, TODAY).problems.join(), /the table must start with/);
  assert.match(parse(`${SECTION}\n\n| Identifiant | Paquet |\n|---|---|\n`, TODAY).problems.join(), /the table must start with/);
  assert.match(parse(`${SECTION}\n\n| ${COLUMNS.join(" | ")} |\n${row()}\n`, TODAY).problems.join(), /no separator row/);
  // The table of another section is not read as the table of exceptions.
  assert.match(parse(`## Autre\n\n${HEAD}\n${SECTION}\n\nAucune.\n`, TODAY).problems.join(), /the table must start with/);
  assert.throws(() => parse(table([]), "14/10/2026"), /not a day/);
});

test("only the rows of the one table count: a row hidden from the rendered page, or standing apart, is refused", () => {
  const around = (/** @type {string} */ before, /** @type {string} */ after) =>
    parse(`${SECTION}\n\n${before}${HEAD}${row({ id: "GHSA-35jh-r3h4-6jhm" })}\n${after}\n## Après\n`, TODAY);
  for (const [name, before, after, refusal] of [
    ["in a fenced code block", "", "\n```\n" + row() + "\n```\n", /holds a code block or an HTML comment \("```"\)/],
    ["in a code block fenced by tildes", "", "\n~~~md\n" + row() + "\n~~~\n", /holds a code block or an HTML comment \("~~~md"\)/],
    ["in an HTML comment", "", "\n<!--\n" + row() + "\n-->\n", /holds an HTML comment \("<!--"\)/],
    ["in an HTML comment on one line", "", `\n<!-- ${row()} -->\n`, /holds an HTML comment/],
    ["after a blank line, under the table", "", `\n${row()}\n`, /begins like a row but is not a row of the table/],
    ["after a sentence, in a second table", "", `\nAutre :\n\n${HEAD}${row()}\n`, /begins like a row but is not a row of the table/],
    ["indented, as a code block", "", `\n    ${row()}\n`, /begins like a row but is not a row of the table/],
    ["indented, as the table itself", "  ", "", /begins like a row but is not a row of the table/],
  ]) {
    const { exceptions, problems } = around(String(before), String(after));
    assert.deepEqual(exceptions, [], String(name));
    assert.ok(problems.length > 0 && problems.every((problem) => refusal instanceof RegExp && refusal.test(problem)), `${name}: ${problems.join(" / ")}`);
  }
  // The table followed by text, then the next section, is read as usual.
  assert.deepEqual(around("Avant.\n\n", "\nAprès le tableau.\n").exceptions.map(({ id }) => id), ["GHSA-35jh-r3h4-6jhm"]);
  // A code block elsewhere in the file changes nothing.
  assert.deepEqual(parse(`# Exceptions\n\n\`\`\`sh\nnode x\n\`\`\`\n\n${table([row()])}`, TODAY).problems, []);
});

test("the page shows the one section that is read: no HTML that hides a line, one heading, outside any code block", () => {
  const real = table([]);
  const hidden = `${SECTION}\n\n${HEAD}${row()}\n${row({ id: "GHSA-35jh-r3h4-6jhm" })}\n`;
  for (const [name, text, refusal] of [
    // Reported by the independent review: a comment before the heading hid a whole section, and that one was read.
    ["a section hidden in a comment before the real one", `# Exceptions\n\n<!--\n${hidden}-->\n\n${real}`, /holds an HTML comment/],
    ["a comment opened before the heading and never closed", `# Exceptions\n\n<!--\n\n${real}`, /holds an HTML comment/],
    ["a comment in another section", `${real}\n## Notes\n\n<!-- rien -->\n`, /holds an HTML comment/],
    ["a second section, shown", `# Exceptions\n\n${hidden}\n${real}`, /is written 2 times \(lines 3, \d+\)/],
    ["a second heading at another level", `${real}\n### Exceptions en cours\n\nAucune.\n`, /is written 2 times/],
    ["a heading in a code block before the real one", `# Exceptions\n\n\`\`\`md\n${hidden}\`\`\`\n\n${real}`, /is written 2 times/],
    ["the only heading in a code block left open", `# Exceptions\n\n\`\`\`\n${hidden}`, /must be the line "## Exceptions en cours", outside any code block/],
    ["the only heading written otherwise", hidden.replace(SECTION, "## exceptions en cours"), /must be the line "## Exceptions en cours"/],
    // Reported by the second delta review: blocks of HTML that GitHub renders as nothing, or as an attribute.
    ["the table in a CDATA block", `${SECTION}\n\n<![CDATA[\n${HEAD}${row()}\n]]>\n`, /line 3 begins with HTML \("<!\[CDATA\["\)/],
    ["the table in a processing instruction", `${SECTION}\n\n<?x\n${HEAD}${row()}\n?>\n`, /line 3 begins with HTML \("<\?x"\)/],
    ["the table in a declaration", `${SECTION}\n\n<!X\n${HEAD}${row()}\n>\n`, /line 3 begins with HTML \("<!X"\)/],
    ["the rows in an attribute", `${SECTION}\n\n<div title="\n${HEAD}${row()}\n"></div>\n`, /line 3 begins with HTML \("<div title=""\)/],
    ["the table in a folded details block", `${SECTION}\n\n<details><summary>Rien</summary>\n\n${HEAD}${row()}\n\n</details>\n`, /line 3 begins with HTML/],
    ["a block of HTML indented by three spaces", `${SECTION}\n\n   <![CDATA[\n${HEAD}${row()}\n]]>\n`, /line 3 begins with HTML/],
    ["a block of HTML before the section", `<div>\n\n</div>\n\n${real}`, /line 1 begins with HTML/],
    ["a CDATA section opened in the middle of a line", `${SECTION}\n\nTexte <![CDATA[\n${HEAD}${row()}\n]]>\n`, /line 3 holds "<!"/],
    ["a processing instruction in the middle of a line", `${real}\nTexte <?x ?>\n`, /holds "<\?"/],
  ]) {
    const { exceptions, problems } = parse(String(text), TODAY);
    assert.deepEqual(exceptions, [], String(name));
    assert.equal(problems.length, 1, `${name}: ${problems.join(" / ")}`);
    assert.ok(refusal instanceof RegExp && refusal.test(problems[0] ?? ""), `${name}: ${problems.join(" / ")}`);
  }
  // The committed page holds no HTML opening, code included, and its heading once.
  const committed = atRoot(FILE);
  assert.ok(!committed.includes("<!--"));
  assert.doesNotMatch(committed, /^ {0,3}<|<[\p{L}/!?]/mu);
  assert.equal(committed.split(`\n${SECTION}\n`).length, 2);
  const refusalRules = committed.split("**Le tableau est le seul de la section**")[1]?.split("\n\n")[0] ?? "";
  assert.match(refusalRules, /suivi d'une lettre Unicode, de `\/`, de `!` ou de `\?`/);
  assert.match(refusalRules, /code compris/);
});

for (const [name, text, refusal] of /** @type {const} */ ([
  ["a folded details block opened and closed in the middle of a line", `${SECTION}\n\nTexte <details><summary>Rien</summary>\n\n${HEAD}${row()}\n\nFin </details>\n`, /line 3 holds "<d"/],
  ["a folded details block opened in the middle of a line and never closed", `${SECTION}\n\nTexte <details><summary>Rien</summary>\n\n${HEAD}${row()}\n`, /line 3 holds "<d"/],
  ["a closing tag alone in the middle of a line", `${SECTION}\n\n${HEAD}${row()}\n\nFin </details>\n`, /line 7 holds "<\/"/],
  ["a tag in the middle of a line whose attribute holds the table", `${SECTION}\n\nTexte <span title="\n${HEAD}${row()}\n">\n`, /line 3 holds "<s"/],
  ["an uppercase tag in the middle of a line", `${SECTION}\n\nTexte <DETAILS><SUMMARY>Rien</SUMMARY>\n\n${HEAD}${row()}\n\nFin </DETAILS>\n`, /line 3 holds "<D"/],
  ["a details tag between backticks", `${SECTION}\n\nUn bloc \`<details>\` replié.\n\n${HEAD}${row()}\n`, /line 3 holds "<d"/],
  // Escaped backticks outside code are literal backticks around real HTML on GitHub.
  // Exempting them as if they were code would let a folded block hide rows again.
  ["a details tag between escaped backticks", `${SECTION}\n\nUn bloc \\\`<details>\\\` replié.\n\n${HEAD}${row()}\n`, /line 3 holds "<d"/],
  ["a tag in a fenced code block in another section", `${table([row()])}\n## Notes\n\n\`\`\`md\nx <details>\n\`\`\`\n`, /holds "<d"/],
  ["a tag in an indented code block in another section", `${table([row()])}\n## Notes\n\n    x <details>\n`, /holds "<d"/],
  ["an automatic link between angle brackets", `${SECTION}\n\nTexte <https://example.com>\n\n${HEAD}${row()}\n`, /line 3 holds "<h"/],
  ["a non-ASCII letter after the opening sign", `${SECTION}\n\nTexte <é\n\n${HEAD}${row()}\n`, /line 3 holds "<é"/],
])) {
  test(`the page refuses HTML anywhere, code included: ${name}`, () => {
    const { exceptions, problems } = parse(text, TODAY);
    assert.deepEqual(exceptions, []);
    assert.equal(problems.length, 1);
    assert.match(problems[0] ?? "", refusal);
    assert.match(problems[0] ?? "", /^\.github\/audit-exceptions\.md: line \d+ holds /);
    assert.match(problems[0] ?? "", /a "<" followed by a letter, "\/", "!" or "\?" opens HTML wherever it stands, a folded details block in the middle of a line included, and none is allowed in this file, code included: the page must show all that is read$/);
  });
}

test("a sign that does not open HTML in the middle of a line leaves the real table readable", () => {
  for (const text of ["1 < 2", "a <= b", "<3", "<-", "`<`"]) {
    const { exceptions, problems } = parse(`${SECTION}\n\nTexte ${text}\n\n${HEAD}${row()}\n`, TODAY);
    assert.deepEqual(problems, [], text);
    assert.deepEqual(exceptions.map(({ id }) => id), ["GHSA-jf85-cpcp-j695"], text);
  }
});

// --- The verdict on npm -------------------------------------------------------

/** An advisory as `npm audit --json` lists it in a `via` list. @param {string} id @param {string} severity @param {string} name */
const advisory = (id, severity, name = "lodash") => ({
  source: 1106913,
  name,
  dependency: name,
  title: `${severity} advisory`,
  url: `https://github.com/advisories/${id}`,
  severity,
  cwe: ["CWE-77"],
  cvss: { score: 7.2, vectorString: "CVSS:3.1/AV:N" },
  range: "<4.17.21",
});
const SEVERITY = ["info", "low", "moderate", "high", "critical"];
/**
 * A report of `npm audit --json`, with the shape npm 10 and 11 give it: lodash holds the advisories, and
 * lodash-wrapper is vulnerable through it. Each entry has the severity of its worst advisory, and the
 * counts of `metadata` are those of the entries, unless `counts` says otherwise.
 * @param {object[]} via @param {Record<string, number>} [counts]
 */
const report = (via, counts) => {
  const worst = via.reduce((max, item) => Math.max(max, SEVERITY.indexOf(String(/** @type {{severity?: string}} */ (item).severity))), -1);
  const severity = SEVERITY[worst] ?? "info";
  const vulnerabilities =
    via.length === 0
      ? {}
      : {
          lodash: { name: "lodash", severity, isDirect: true, via, effects: ["lodash-wrapper"], range: "<=4.17.23", nodes: ["node_modules/lodash"], fixAvailable: true },
          // A package that is vulnerable through another names it, and holds no advisory of its own.
          "lodash-wrapper": { name: "lodash-wrapper", severity, isDirect: false, via: ["lodash"], effects: [], range: "*", nodes: ["node_modules/lodash-wrapper"], fixAvailable: true },
        };
  const listed = Object.fromEntries(SEVERITY.map((name) => [name, Object.values(vulnerabilities).filter((entry) => entry.severity === name).length]));
  return {
    auditReportVersion: 2,
    vulnerabilities,
    metadata: { vulnerabilities: { ...listed, total: Object.keys(vulnerabilities).length, ...counts }, dependencies: { prod: 1, dev: 0, total: 1 } },
  };
};
const LODASH = report([advisory("GHSA-35jh-r3h4-6jhm", "high"), advisory("GHSA-jf85-cpcp-j695", "critical"), advisory("GHSA-29mw-wpgm-hmr9", "moderate")]);
const excepted = (/** @type {string[]} */ rows) => parse(table(rows), TODAY).exceptions;

test("npm: a high or critical advisory fails the audit unless it is an exception for that same package", () => {
  const none = judgeNpm(LODASH, []);
  assert.equal(none.ok, false);
  assert.deepEqual(
    none.lines.filter((line) => line.startsWith("NOT EXCEPTED")).map((line) => line.split(" ")[2]),
    ["GHSA-35jh-r3h4-6jhm", "GHSA-jf85-cpcp-j695"],
    "a moderate advisory is not judged",
  );
  const both = judgeNpm(LODASH, excepted([row({ id: "GHSA-35jh-r3h4-6jhm" }), row()]));
  assert.equal(both.ok, true, both.lines.join("\n"));
  assert.match(both.lines.join("\n"), /^excepted until 2026-12-01 \(ANH-153\): GHSA-jf85-cpcp-j695 \(critical\) in lodash/m);
  assert.equal(judgeNpm(LODASH, excepted([row()])).ok, false, "one advisory is still not excepted");
  // An exception names its package: the same advisory in another package is not excepted.
  assert.equal(judgeNpm(LODASH, excepted([row({ id: "GHSA-35jh-r3h4-6jhm", name: "lodash-es" }), row()])).ok, false);
  // A Python exception never excepts an npm advisory.
  assert.equal(judgeNpm(LODASH, excepted([row({ id: "GHSA-35jh-r3h4-6jhm", ecosystem: "pip" }), row()])).ok, false);
});

test("npm: moderate and low advisories fail nothing, and an unused exception is said", () => {
  const mild = report([advisory("GHSA-29mw-wpgm-hmr9", "moderate"), advisory("GHSA-aaaa-bbbb-cccc", "low")]);
  const verdict = judgeNpm(mild, excepted([row()]));
  assert.equal(verdict.ok, true);
  assert.match(verdict.lines.join("\n"), /exception GHSA-jf85-cpcp-j695 for lodash matches no high or critical advisory/);
  assert.equal(judgeNpm(report([]), []).ok, true);
});

test("npm: a report that cannot be read whole excepts nothing", () => {
  const whole = structuredClone(LODASH);
  assert.equal("unread" in readNpmReport(whole), false);
  const without = (/** @type {(copy: any) => void} */ change) => {
    const copy = structuredClone(LODASH);
    change(copy);
    return copy;
  };
  for (const [name, unread, said] of [
    ["an error of npm", { error: { code: "ENOLOCK", summary: "This command requires an existing lockfile." } }, /npm answered This command requires an existing lockfile/],
    ["a message and nothing else", { message: "registry down" }, /auditReportVersion is undefined/],
    ["a list", [], /not a JSON object/],
    ["null", null, /not a JSON object/],
    ["another version of the format", without((copy) => { copy.auditReportVersion = 1; }), /auditReportVersion is 1, 2 expected/],
    ["no metadata", without((copy) => { delete copy.metadata; }), /no `metadata.vulnerabilities` object/],
    ["no counts in the metadata", without((copy) => { delete copy.metadata.vulnerabilities; }), /no `metadata.vulnerabilities` object/],
    ["a count missing", without((copy) => { delete copy.metadata.vulnerabilities.high; }), /metadata counts undefined high/],
    ["a count written as text", without((copy) => { copy.metadata.vulnerabilities.critical = "2"; }), /metadata counts "2" critical/],
    ["counts that are not those of the entries", report(LODASH.vulnerabilities.lodash.via, { critical: 1 }), /metadata counts 1 critical vulnerabilities, the report lists 2/],
    ["no `vulnerabilities`", without((copy) => { delete copy.vulnerabilities; }), /no `vulnerabilities` object/],
    ["an entry of an unknown severity", without((copy) => { copy.vulnerabilities.lodash.severity = "severe"; }), /entry of lodash has no known severity/],
    ["an entry without its `via` list", without((copy) => { delete copy.vulnerabilities["lodash-wrapper"].via; }), /entry of lodash-wrapper has no known severity or no `via` list/],
    ["a package vulnerable through one that has no entry", without((copy) => { copy.vulnerabilities["lodash-wrapper"].via = ["dep"]; }), /lodash-wrapper is vulnerable through dep, which has no entry/],
    ["an advisory without its address", without((copy) => { delete copy.vulnerabilities.lodash.via[0].url; }), /an advisory of lodash has no known severity, package or address/],
    ["an advisory of an unknown severity", without((copy) => { copy.vulnerabilities.lodash.via[1].severity = "urgent"; }), /an advisory of lodash has no known severity/],
    ["counts that call moderate entries critical", report([advisory("GHSA-29mw-wpgm-hmr9", "moderate")], { moderate: 0, critical: 2 }), /metadata counts/],
    ["a critical entry whose advisories are all moderate", without((copy) => { copy.vulnerabilities.lodash.via = [advisory("GHSA-29mw-wpgm-hmr9", "moderate")]; }), /lodash is critical, and no high or critical advisory explains it/],
    // Reported by the independent review: no metadata, and a `via` list of names only.
    ["a critical entry reached through a name only", { vulnerabilities: { lodash: { severity: "critical", via: ["dep"] } } }, /auditReportVersion is undefined/],
  ]) {
    const verdict = judgeNpm(unread, excepted([row(), row({ id: "GHSA-35jh-r3h4-6jhm" })]));
    assert.equal(verdict.ok, false, String(name));
    assert.ok(said instanceof RegExp && said.test(verdict.lines.join("\n")), `${name}: ${verdict.lines.join(" / ")}`);
    assert.match(verdict.lines.join("\n"), /^npm audit: the report cannot be read whole \(.*\)\. Nothing is excepted\.$/, String(name));
  }
  // Two packages vulnerable through each other are explained by the advisory one of them holds.
  const cycle = without((copy) => { copy.vulnerabilities.lodash.via.push("lodash-wrapper"); });
  assert.equal(judgeNpm(cycle, excepted([row(), row({ id: "GHSA-35jh-r3h4-6jhm" })])).ok, true);
  // An advisory with no GitHub identifier is named by its npm number, and cannot be excepted.
  const unnamed = report([{ ...advisory("GHSA-jf85-cpcp-j695", "high"), url: "https://www.npmjs.com/advisories/1179" }]);
  assert.match(judgeNpm(unnamed, excepted([row()])).lines.join("\n"), /^NOT EXCEPTED: npm-1106913 \(high\)/m);
});

// --- The command line, as the `audit` job runs it ---------------------------

/** Run `main` in a throwaway root holding this table, its output captured. @param {import("node:test").TestContext} context */
function run(context, /** @type {string[]} */ argv, /** @type {string | undefined} */ text, audit = () => JSON.stringify(report([]))) {
  const directory = mkdtempSync(join(tmpdir(), "anheart-audit-exceptions-"));
  context.after(() => rmSync(directory, { recursive: true, force: true }));
  if (text !== undefined) {
    mkdirSync(dirname(join(directory, FILE)), { recursive: true });
    writeFileSync(join(directory, FILE), text);
  }
  /** @type {string[]} */
  const printed = [];
  const { log, error } = console;
  console.log = console.error = (/** @type {unknown} */ line) => void printed.push(String(line));
  try {
    return { status: main(argv, { root: directory, today: TODAY, audit }), output: printed.join("\n") };
  } finally {
    console.log = log;
    console.error = error;
  }
}

test("every command refuses a table that is malformed or holds an expired exception, and nothing is skipped", (context) => {
  const expired = table([row({ added: "2026-08-01", expires: "2026-10-14" })]);
  for (const command of ["check", "pip-audit-args", "npm"]) {
    const refused = run(context, [command], expired, () => assert.fail("npm audit ran on a table that was refused"));
    assert.equal(refused.status, 1, command);
    assert.match(refused.output, /expired on 2026-10-14/, command);
    assert.equal(run(context, [command], undefined).status, 1, `${command} without the file`);
  }
  assert.equal(run(context, [], table([])).status, 2);
  assert.equal(run(context, ["check", "--today=2026-01-01"], table([])).status, 2);
  assert.equal(run(context, ["ignore"], table([])).status, 2);
});

test("the commands answer what the job reads from them", (context) => {
  const rows = table([row(), row({ id: "PYSEC-2019-132", ecosystem: "pip", name: "urllib3" })]);
  assert.deepEqual(run(context, ["check"], rows), {
    status: 0,
    output: `${FILE}: 2 exception(s), none expired on ${TODAY}.\n  npm GHSA-jf85-cpcp-j695 (lodash) until 2026-12-01, ANH-153\n  pip PYSEC-2019-132 (urllib3) until 2026-12-01, ANH-153`,
  });
  assert.deepEqual(run(context, ["pip-audit-args"], rows), { status: 0, output: "--ignore-vuln PYSEC-2019-132" });
  assert.equal(run(context, ["npm"], rows, () => JSON.stringify(LODASH)).status, 1);
  assert.equal(run(context, ["npm"], table([row(), row({ id: "GHSA-35jh-r3h4-6jhm" })]), () => JSON.stringify(LODASH)).status, 0);
  const garbled = run(context, ["npm"], rows, () => "npm ERR! not json");
  assert.equal(garbled.status, 1);
  assert.match(garbled.output, /^npm audit --json: .*Nothing is excepted\.$/);
});

// --- Dependabot ------------------------------------------------------------------

/** The entries of .github/dependabot.yml, each as its own lines. */
const DEPENDABOT = atRoot(".github/dependabot.yml");
const entries = DEPENDABOT.slice(DEPENDABOT.indexOf("\nupdates:\n"))
  .split(/^ {2}- /m)
  .slice(1)
  .map((text) => {
    const key = (/** @type {string} */ name) => new RegExp(`^ *${name}: *(.*)$`, "m").exec(text)?.[1]?.trim();
    return { text, ecosystem: key("package-ecosystem"), directory: key("directory"), branch: key("target-branch"), interval: key("interval") };
  });

test("Dependabot watches every place the audit audits, and nothing else, against develop", () => {
  assert.match(DEPENDABOT, /^version: 2$/m);
  assert.deepEqual(
    entries.map(({ ecosystem, directory }) => `${ecosystem} ${directory}`),
    ["npm /", "pip /raspberry-pi", "pip /deploy/simulation-vercel", "pip /simulation/cad", "github-actions /"],
  );
  for (const { ecosystem, directory, branch, interval, text } of entries) {
    assert.equal(branch, "develop", `${ecosystem} ${directory}: pull requests go to develop`);
    assert.ok(["daily", "weekly", "monthly"].includes(interval ?? ""), `${ecosystem} ${directory}: no schedule`);
    // Nothing is hidden from it: no dependency is ignored, no update type left out of what is watched.
    assert.doesNotMatch(text, /^ +(ignore|allow):/m, `${ecosystem} ${directory}`);
  }
  // Every directory that holds a manifest has an entry; the root holds a copy of the hosted simulation
  // for the Git layout of Vercel, which vercel.json turns off: said in the file, with no entry of its own.
  const manifests = tracked.filter((path) => /(^|\/)(package\.json|requirements[^/]*\.txt)$/.test(path));
  for (const path of manifests) {
    const directory = `/${dirname(path)}`.replace(/\/\.$/, "/");
    const ecosystem = path.endsWith("package.json") ? "npm" : "pip";
    if (ecosystem === "pip" && directory === "/") continue;
    assert.ok(entries.some((entry) => entry.ecosystem === ecosystem && entry.directory === directory), `${path}: no ${ecosystem} entry for ${directory}`);
  }
  assert.equal(atRoot("requirements.txt").trim(), "-r deploy/simulation-vercel/requirements-lock.txt");
  assert.ok(DEPENDABOT.replaceAll("\n# ", " ").includes("pyproject.toml of the root repeat the hosted simulation"));
  assert.match(atRoot("vercel.json"), /"deploymentEnabled": false/);
});

// --- One JavaScript package manager ------------------------------------------

test("one JavaScript package manager: npm, with package-lock.json for its lock", () => {
  const locks = tracked.filter((path) => /(^|\/)(package-lock\.json|bun\.lockb?|yarn\.lock|pnpm-lock\.yaml|npm-shrinkwrap\.json)$/.test(path));
  assert.deepEqual(locks, ["package-lock.json"]);
  const manifest = JSON.parse(atRoot("package.json"));
  assert.ok(manifest.packageManager === undefined || String(manifest.packageManager).startsWith("npm@"));
  // A name npm accepts: npm 11 refuses to write the bill of materials of a package named "-t", as this one was.
  assert.match(manifest.name, /^[a-z0-9][a-z0-9._-]*$/);
  const lock = JSON.parse(atRoot("package-lock.json"));
  assert.deepEqual([lock.name, lock.packages[""].name], [manifest.name, manifest.name]);
  // What installs the site, in a workflow or in a page of the documentation, is npm.
  const installers = /\b(bun|yarn|pnpm)( install| add| run|x)\b/;
  for (const path of tracked.filter((file) => /^\.github\/workflows\/.*\.ya?ml$|^docs\/.*\.md$|^README\.md$/.test(file))) {
    assert.doesNotMatch(atRoot(path), installers, path);
  }
});

// --- The files of secrets that Git ignores ------------------------------------------

test("the files that hold secrets are ignored by Git, and only their examples are tracked", () => {
  const ignored = (/** @type {string} */ path) =>
    spawnSync("git", ["check-ignore", "--quiet", "--no-index", path], { cwd: root }).status === 0;
  for (const path of [".env.local", ".env", ".env.production", "raspberry-pi/.env", "raspberry-pi/.env.pi", ".vercel-cli/auth.json", ".clerk/.tmp/keyless.json", ".codex/config.toml"]) {
    assert.ok(ignored(path), `${path} is not ignored`);
  }
  for (const path of ["raspberry-pi/.env.example", "raspberry-pi/.env.pi.example"]) {
    assert.ok(!ignored(path), `${path} is ignored`);
    assert.ok(tracked.includes(path), `${path} is not tracked`);
  }
  assert.deepEqual(
    tracked.filter((path) => /(^|\/)\.env/.test(path)).filter((path) => !path.endsWith(".example")),
    [],
    "a file of secrets is tracked",
  );
});

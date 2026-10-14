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

import { COLUMNS, FILE, MAX_DAYS, SECTION, judgeNpm, main, parse, pipAuditArgs } from "./audit-exceptions.mjs";

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
  const { exceptions, problems } = parse(table([row(), pip, row({ id: "CVE-2026-12345", ecosystem: "pip", name: "numpy" })]), TODAY);
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
  assert.equal(pipAuditArgs(exceptions), "--ignore-vuln PYSEC-2019-132 --ignore-vuln CVE-2026-12345");
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
/** A report of `npm audit --json` (npm 11). @param {object[]} via @param {Record<string, number>} counts */
const report = (via, counts) => ({
  auditReportVersion: 2,
  vulnerabilities: {
    lodash: { name: "lodash", severity: "critical", isDirect: true, via, effects: ["lodash-wrapper"], range: "<=4.17.23", nodes: ["node_modules/lodash"], fixAvailable: true },
    // A package that is vulnerable through another names it, and holds no advisory of its own.
    "lodash-wrapper": { name: "lodash-wrapper", severity: "critical", isDirect: false, via: ["lodash"], effects: [], range: "*", nodes: ["node_modules/lodash-wrapper"], fixAvailable: true },
  },
  metadata: { vulnerabilities: { info: 0, low: 0, moderate: 0, high: 0, critical: 0, total: 0, ...counts } },
});
const LODASH = report(
  [advisory("GHSA-35jh-r3h4-6jhm", "high"), advisory("GHSA-jf85-cpcp-j695", "critical"), advisory("GHSA-29mw-wpgm-hmr9", "moderate")],
  { critical: 2, total: 2 },
);
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
  const mild = report([advisory("GHSA-29mw-wpgm-hmr9", "moderate"), advisory("GHSA-aaaa-bbbb-cccc", "low")], { moderate: 1, low: 1, total: 2 });
  const verdict = judgeNpm(mild, excepted([row()]));
  assert.equal(verdict.ok, true);
  assert.match(verdict.lines.join("\n"), /exception GHSA-jf85-cpcp-j695 for lodash matches no high or critical advisory/);
  assert.equal(judgeNpm(report([], {}), []).ok, true);
});

test("npm: a report that cannot be read excepts nothing", () => {
  for (const unread of [
    { error: { code: "ENOLOCK", summary: "This command requires an existing lockfile." } },
    { message: "registry down" },
    [],
    null,
    // npm counts a high vulnerability, and no advisory explains it: the report changed shape.
    { ...report([], { high: 1, total: 1 }) },
    { ...report([{ name: "lodash", severity: "critical" }], { critical: 1, total: 1 }) },
  ]) {
    assert.equal(judgeNpm(unread, excepted([row()])).ok, false, JSON.stringify(unread));
  }
  assert.match(judgeNpm({ error: { summary: "no lockfile" } }, []).lines.join(), /npm audit gave no report: no lockfile/);
  // An advisory with no GitHub identifier is named by its npm number, and cannot be excepted.
  const unnamed = report([{ ...advisory("GHSA-jf85-cpcp-j695", "high"), url: "https://www.npmjs.com/advisories/1179" }], { high: 1 });
  assert.match(judgeNpm(unnamed, excepted([row()])).lines.join("\n"), /^NOT EXCEPTED: npm-1106913 \(high\)/m);
});

// --- The command line, as the `audit` job runs it ---------------------------

/** Run `main` in a throwaway root holding this table, its output captured. @param {import("node:test").TestContext} context */
function run(context, /** @type {string[]} */ argv, /** @type {string | undefined} */ text, audit = () => JSON.stringify(report([], {}))) {
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
  assert.equal(atRoot("requirements.txt").trim(), "-r deploy/simulation-vercel/requirements.txt");
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

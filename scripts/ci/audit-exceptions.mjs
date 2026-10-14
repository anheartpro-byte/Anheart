// The exceptions of the dependency audit, written in .github/audit-exceptions.md
// (ANH-153), and what the `audit` job of .github/workflows/ci.yml does with them.
//
//     node scripts/ci/audit-exceptions.mjs check            the table is well formed and no exception has expired
//     node scripts/ci/audit-exceptions.mjs pip-audit-args   `--ignore-vuln <id>` for each Python exception, on one line
//     node scripts/ci/audit-exceptions.mjs npm              runs `npm audit --json`: exit 1 on a high or critical
//                                                           advisory that no exception covers
//
// Every form refuses a malformed table or an exception past its expiry day:
// nothing is skipped on the word of a table that is not what the rules say.
//
// The job runs `npm audit --audit-level=high` first, and the `npm` form only
// when npm found something. With no exception written, the verdict of the job
// is therefore the verdict of npm itself. pip-audit fails on any known
// vulnerability, whatever its severity: it is handed the `pip-audit-args` form,
// and skips nothing else.
//
// An exception is one row of the table under "## Exceptions en cours": the
// advisory, its ecosystem, the package, the day it was written, the day it
// expires (90 days later at most), the Linear ticket that removes it and why it
// is accepted until then. From its expiry day on, the `audit` job fails, on
// every pull request, and on the nightly run once the row is on `main`,
// until a pull request removes the row or renews it.

import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

/** The file, from the root of the repository. */
export const FILE = ".github/audit-exceptions.md";
/** The heading of the section that holds the table. */
export const SECTION = "## Exceptions en cours";
/** The columns of the table, in this order. */
export const COLUMNS = ["Identifiant", "Écosystème", "Paquet", "Ajoutée le", "Expire le", "Ticket", "Raison"];
/** The longest an exception may last, in days. */
export const MAX_DAYS = 90;
/** The severities of npm that fail the audit (`npm audit --audit-level=high`). */
export const BLOCKING = ["high", "critical"];

/** What an advisory is named, per ecosystem: what `npm audit` and `pip-audit` print. */
const IDENTIFIERS = {
  npm: /^GHSA(-[0-9a-z]{4}){3}$/,
  pip: /^(GHSA(-[0-9a-z]{4}){3}|PYSEC-\d{4}-\d+|CVE-\d{4}-\d{4,})$/,
};
const TICKET = /^\[(ANH-[1-9]\d*)\]\(https:\/\/linear\.app\/anheart\/issue\/(ANH-[1-9]\d*)(\/[a-z0-9-]+)?\)$/;
const PACKAGE = /^[@a-zA-Z0-9][\w.@/-]*$/;
const DAY = 24 * 60 * 60 * 1000;

/**
 * @typedef {{ id: string, ecosystem: "npm" | "pip", package: string, added: string, expires: string, ticket: string, reason: string }} Exception
 * @typedef {{ id: string, severity: string, package: string, title: string, url: string }} Advisory
 */

/** @param {string} text @returns {number | undefined} the day, at midnight UTC, of a date written AAAA-MM-JJ */
function dayOf(text) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return undefined;
  const time = Date.parse(`${text}T00:00:00Z`);
  return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === text ? time : undefined;
}

/** @param {string} line a row of a Markdown table @returns {string[]} its cells */
const cellsOf = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());

/**
 * The exceptions of the file, and what is wrong with it on that day.
 * @param {string} text the content of .github/audit-exceptions.md
 * @param {string} today AAAA-MM-JJ, in UTC
 * @returns {{ exceptions: Exception[], problems: string[] }}
 */
export function parse(text, today) {
  /** @type {Exception[]} */
  const exceptions = [];
  /** @type {string[]} */
  const problems = [];
  const now = dayOf(today);
  if (now === undefined) throw new Error(`not a day: ${today}`);
  const lines = text.replaceAll("\r\n", "\n").split("\n");
  const start = lines.indexOf(SECTION);
  if (start < 0) return { exceptions, problems: [`${FILE}: no section "${SECTION}"`] };
  const after = lines.slice(start + 1);
  const end = after.findIndex((line) => line.startsWith("## "));
  const rows = (end < 0 ? after : after.slice(0, end)).filter((line) => line.trimStart().startsWith("|"));
  const [header, separator, ...body] = rows;
  if (header === undefined || cellsOf(header).join("|") !== COLUMNS.join("|")) {
    return { exceptions, problems: [`${FILE}: the table must start with | ${COLUMNS.join(" | ")} |`] };
  }
  if (separator === undefined || !cellsOf(separator).every((cell) => /^:?-{3,}:?$/.test(cell)) || cellsOf(separator).length !== COLUMNS.length) {
    return { exceptions, problems: [`${FILE}: the header of the table has no separator row`] };
  }
  /** @type {Set<string>} */
  const seen = new Set();
  for (const row of body) {
    const cells = cellsOf(row);
    const where = `${FILE}: row "${row.trim()}"`;
    if (cells.length !== COLUMNS.length) {
      problems.push(`${where}: ${cells.length} cells, ${COLUMNS.length} expected (no "|" in a cell)`);
      continue;
    }
    const [id = "", ecosystem = "", name = "", added = "", expires = "", ticket = "", reason = ""] = cells;
    if (ecosystem !== "npm" && ecosystem !== "pip") {
      problems.push(`${where}: ecosystem "${ecosystem}", npm or pip expected`);
      continue;
    }
    const wrong = [];
    if (!IDENTIFIERS[ecosystem].test(id)) wrong.push(`identifier "${id}" is not one ${ecosystem === "npm" ? "npm audit" : "pip-audit"} prints`);
    if (!PACKAGE.test(name)) wrong.push(`package "${name}"`);
    const from = dayOf(added);
    const until = dayOf(expires);
    if (from === undefined) wrong.push(`added "${added}" is not a date AAAA-MM-JJ`);
    if (until === undefined) wrong.push(`expires "${expires}" is not a date AAAA-MM-JJ`);
    if (from !== undefined && until !== undefined) {
      if (from > now) wrong.push(`added on ${added}, after today (${today})`);
      if (until <= from) wrong.push(`expires on ${expires}, not after the day it was added (${added})`);
      else if ((until - from) / DAY > MAX_DAYS) wrong.push(`lasts ${(until - from) / DAY} days, ${MAX_DAYS} at most`);
      if (until <= now) wrong.push(`expired on ${expires}: remove the row, or renew it by a pull request that says why`);
    }
    const linked = TICKET.exec(ticket);
    if (!linked || linked[1] !== linked[2]) wrong.push(`ticket "${ticket}" is not a link [ANH-n](https://linear.app/anheart/issue/ANH-n/...)`);
    if (reason === "") wrong.push("no reason");
    const key = `${ecosystem} ${id}`;
    if (seen.has(key)) wrong.push("written twice");
    seen.add(key);
    if (wrong.length > 0) {
      problems.push(`${where}: ${wrong.join("; ")}`);
      continue;
    }
    exceptions.push({ id, ecosystem, package: name, added, expires, ticket: linked?.[1] ?? "", reason });
  }
  return { exceptions, problems };
}

/** @param {readonly Exception[]} exceptions @returns {string} what pip-audit is told to skip, on one line */
export const pipAuditArgs = (exceptions) =>
  exceptions
    .filter((exception) => exception.ecosystem === "pip")
    .flatMap((exception) => ["--ignore-vuln", exception.id])
    .join(" ");

/** @param {unknown} value @returns {value is Record<string, unknown>} */
const isRecord = (value) => typeof value === "object" && value !== null && !Array.isArray(value);

/**
 * The advisories of a report of `npm audit --json` (npm 7 and later), each
 * once. An advisory is an object of a `via` list; a name in such a list is a
 * package that depends on a vulnerable one, not an advisory of its own.
 * @param {Record<string, unknown>} vulnerabilities
 * @returns {Advisory[]}
 */
function advisoriesOf(vulnerabilities) {
  /** @type {Map<string, Advisory>} */
  const found = new Map();
  for (const entry of Object.values(vulnerabilities)) {
    const via = isRecord(entry) && Array.isArray(entry.via) ? entry.via : [];
    for (const item of via) {
      if (!isRecord(item)) continue;
      const url = typeof item.url === "string" ? item.url : "";
      // An advisory without a GitHub identifier cannot be excepted: it is named after its npm number.
      const id = /\/advisories\/(GHSA(?:-[0-9a-z]{4}){3})$/.exec(url)?.[1] ?? `npm-${String(item.source)}`;
      const advisory = {
        id,
        severity: typeof item.severity === "string" ? item.severity : "unknown",
        package: typeof item.name === "string" ? item.name : "",
        title: typeof item.title === "string" ? item.title : "",
        url,
      };
      found.set(`${id} ${advisory.package}`, advisory);
    }
  }
  return [...found.values()];
}

/**
 * The verdict on a report of `npm audit --json`, the exceptions applied.
 * @param {unknown} report the parsed JSON
 * @param {readonly Exception[]} exceptions
 * @returns {{ ok: boolean, lines: string[] }}
 */
export function judgeNpm(report, exceptions) {
  if (!isRecord(report) || report.error !== undefined || !isRecord(report.vulnerabilities)) {
    const error = isRecord(report) && isRecord(report.error) ? report.error : {};
    const said = typeof error.summary === "string" ? `: ${error.summary}` : "";
    return { ok: false, lines: [`npm audit gave no report${said}. Nothing is excepted.`] };
  }
  const counts = isRecord(report.metadata) && isRecord(report.metadata.vulnerabilities) ? report.metadata.vulnerabilities : {};
  const counted = BLOCKING.reduce((sum, severity) => sum + (typeof counts[severity] === "number" ? counts[severity] : 0), 0);
  const blocking = advisoriesOf(report.vulnerabilities).filter((advisory) => BLOCKING.includes(advisory.severity));
  if (counted > 0 && blocking.length === 0) {
    return { ok: false, lines: [`npm audit counts ${counted} high or critical vulnerabilities, and this script found none of their advisories: its report is not read as it should be. Nothing is excepted.`] };
  }
  const npm = exceptions.filter((exception) => exception.ecosystem === "npm");
  /** @type {string[]} */
  const lines = [];
  let ok = true;
  for (const advisory of blocking) {
    const exception = npm.find((candidate) => candidate.id === advisory.id && candidate.package === advisory.package);
    const named = `${advisory.id} (${advisory.severity}) in ${advisory.package}: ${advisory.title} ${advisory.url}`.trim();
    if (exception) {
      lines.push(`excepted until ${exception.expires} (${exception.ticket}): ${named}`);
    } else {
      ok = false;
      lines.push(`NOT EXCEPTED: ${named}`);
    }
  }
  for (const exception of npm) {
    if (!blocking.some((advisory) => advisory.id === exception.id && advisory.package === exception.package)) {
      lines.push(`exception ${exception.id} for ${exception.package} matches no high or critical advisory of this report: remove it once it is no longer needed`);
    }
  }
  lines.push(
    ok
      ? `Every high or critical advisory (${blocking.length}) is an exception of ${FILE} that has not expired.`
      : `A high or critical advisory is not an exception of ${FILE}: update the package, or write a dated exception (rules at the top of that file).`,
  );
  return { ok, lines };
}

/** @returns {string} today, AAAA-MM-JJ, in UTC */
const todayInUtc = () => new Date().toISOString().slice(0, 10);

/** @param {string} root @returns {string} the stdout of `npm audit --json`, whatever its exit code */
function npmAudit(root) {
  const result = spawnSync("npm", ["audit", "--json"], { cwd: root, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
  if (result.error) throw result.error;
  return result.stdout;
}

/**
 * @param {readonly string[]} argv
 * @param {{ root: string, today?: string, audit?: (root: string) => string }} options
 * @returns {number} the exit code
 */
export function main(argv, { root, today = todayInUtc(), audit = npmAudit }) {
  const [command, ...rest] = argv;
  if (rest.length > 0 || !["check", "pip-audit-args", "npm"].includes(command ?? "")) {
    console.error("Usage: node scripts/ci/audit-exceptions.mjs check | pip-audit-args | npm");
    return 2;
  }
  let text;
  try {
    text = readFileSync(join(root, FILE), "utf8");
  } catch (error) {
    console.error(`${FILE}: ${error instanceof Error ? error.message : String(error)}`);
    return 1;
  }
  const { exceptions, problems } = parse(text, today);
  if (problems.length > 0) {
    for (const problem of problems) console.error(problem);
    return 1;
  }
  if (command === "check") {
    console.log(`${FILE}: ${exceptions.length} exception(s), none expired on ${today}.`);
    for (const exception of exceptions) {
      console.log(`  ${exception.ecosystem} ${exception.id} (${exception.package}) until ${exception.expires}, ${exception.ticket}`);
    }
    return 0;
  }
  if (command === "pip-audit-args") {
    console.log(pipAuditArgs(exceptions));
    return 0;
  }
  let report;
  try {
    report = JSON.parse(audit(root));
  } catch (error) {
    console.error(`npm audit --json: ${error instanceof Error ? error.message : String(error)}. Nothing is excepted.`);
    return 1;
  }
  const verdict = judgeNpm(report, exceptions);
  for (const line of verdict.lines) (verdict.ok ? console.log : console.error)(line);
  return verdict.ok ? 0 : 1;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  process.exitCode = main(process.argv.slice(2), { root: fileURLToPath(new URL("../..", import.meta.url)) });
}

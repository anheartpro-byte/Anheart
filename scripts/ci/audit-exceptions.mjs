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

/**
 * What an advisory is named, per ecosystem: what `npm audit` and `pip-audit`
 * print. pip-audit skips a finding whose identifier, or one of whose aliases,
 * is the one it is given, whatever the package and in every environment: a
 * CVE is refused here because it is the alias of advisories of several
 * packages, when a vulnerable library is shipped by more than one.
 */
const IDENTIFIERS = {
  npm: /^GHSA(-[0-9a-z]{4}){3}$/,
  pip: /^(GHSA(-[0-9a-z]{4}){3}|PYSEC-\d{4}-\d+)$/,
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
  // Refuse HTML comments, lines beginning with "<" after at most three spaces, and any "<" followed
  // by a Unicode letter, "/", "!" or "?" anywhere in the file so the page shows all that is read.
  // Code is included because exempting it would require exactly matching GitHub's backtick and
  // escape rules, and any mismatch could hide rows.
  if (text.includes("<!--")) {
    return { exceptions, problems: [`${FILE}: holds an HTML comment ("<!--"): the page must show all that is read, and no comment is allowed in it`] };
  }
  const markup = lines.findIndex((line) => /^ {0,3}</.test(line));
  if (markup >= 0) {
    return { exceptions, problems: [`${FILE}: line ${markup + 1} begins with HTML ("${lines[markup]?.trim()}"): the page must show all that is read, and no HTML block is allowed in it`] };
  }
  const inline = /<[\p{L}/!?]/u.exec(text);
  if (inline !== null) {
    const line = text.slice(0, inline.index).split("\n").length;
    return { exceptions, problems: [`${FILE}: line ${line} holds "${inline[0]}": a "<" followed by a letter, "/", "!" or "?" opens HTML wherever it stands, a folded details block in the middle of a line included, and none is allowed in this file, code included: the page must show all that is read`] };
  }
  // The heading of the section, written once, and not inside a code block, where it would be text.
  /** @type {{ index: number, line: string, fenced: boolean }[]} */
  const headings = [];
  let fenced = false;
  lines.forEach((line, index) => {
    if (/^\s*(```|~~~)/.test(line)) fenced = !fenced;
    else if (/^ {0,3}#{1,6}[ \t]+Exceptions en cours[ \t#]*$/i.test(line)) headings.push({ index, line, fenced });
  });
  if (headings.length > 1) {
    return { exceptions, problems: [`${FILE}: the heading "${SECTION}" is written ${headings.length} times (lines ${headings.map(({ index }) => index + 1).join(", ")}): one section, read by the job and shown by the page`] };
  }
  const [heading] = headings;
  if (heading !== undefined && (heading.fenced || heading.line !== SECTION)) {
    return { exceptions, problems: [`${FILE}: the heading of the section must be the line "${SECTION}", outside any code block (line ${heading.index + 1})`] };
  }
  const start = heading?.index ?? -1;
  if (start < 0) return { exceptions, problems: [`${FILE}: no section "${SECTION}"`] };
  const after = lines.slice(start + 1);
  const end = after.findIndex((line) => line.startsWith("## "));
  const section = end < 0 ? after : after.slice(0, end);
  // What the rendered page does not show must not hold a row: no code block, no HTML comment.
  const hiding = section.find((line) => /^\s*(```|~~~)/.test(line) || line.includes("<!--"));
  if (hiding !== undefined) {
    return { exceptions, problems: [`${FILE}: the section "${SECTION}" holds a code block or an HTML comment ("${hiding.trim()}"): only the table and plain text may stand there`] };
  }
  // The table is one block of rows, one after another, from its header on. Any other line that
  // begins like a row is refused: it would be read by no one, or shown as another table.
  const first = section.findIndex((line) => line.trimStart().startsWith("|"));
  let last = first;
  while (first >= 0 && section[last + 1]?.startsWith("|")) last += 1;
  const rows = first < 0 ? [] : section.slice(first, last + 1);
  const stray = section.filter((line, index) => line.trimStart().startsWith("|") && (index > last || !line.startsWith("|")));
  if (stray.length > 0) {
    return { exceptions, problems: stray.map((line) => `${FILE}: "${line.trim()}" begins like a row but is not a row of the table of exceptions: one table, its rows one after another, none indented`) };
  }
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

/** The severities npm gives, from the lowest. */
const SEVERITIES = ["info", "low", "moderate", "high", "critical"];

/**
 * A report of `npm audit --json` (npm 7 to 11, `auditReportVersion` 2), read
 * whole, or what in it could not be read. Every entry and every advisory must
 * have the shape npm gives them, the counts of `metadata` must be those of the
 * entries, and every high or critical entry must lead, through the packages it
 * names, to a high or critical advisory. Anything else is refused: a report
 * that is not read whole excepts nothing.
 * @param {unknown} report the parsed JSON
 * @returns {{ advisories: Advisory[] } | { unread: string }}
 */
export function readNpmReport(report) {
  if (!isRecord(report)) return { unread: "the report is not a JSON object" };
  if (report.error !== undefined) {
    const said = isRecord(report.error) && typeof report.error.summary === "string" ? report.error.summary : "an error";
    return { unread: `npm answered ${said}` };
  }
  if (report.auditReportVersion !== 2) return { unread: `auditReportVersion is ${JSON.stringify(report.auditReportVersion)}, 2 expected` };
  const { vulnerabilities, metadata } = report;
  if (!isRecord(vulnerabilities)) return { unread: "no `vulnerabilities` object" };
  const counts = isRecord(metadata) && isRecord(metadata.vulnerabilities) ? metadata.vulnerabilities : undefined;
  if (counts === undefined) return { unread: "no `metadata.vulnerabilities` object" };
  /** @type {Map<string, Advisory>} */
  const found = new Map();
  /** @type {Record<string, number>} */
  const entries = Object.fromEntries(SEVERITIES.map((severity) => [severity, 0]));
  for (const [name, entry] of Object.entries(vulnerabilities)) {
    if (!isRecord(entry) || typeof entry.severity !== "string" || !SEVERITIES.includes(entry.severity) || !Array.isArray(entry.via)) {
      return { unread: `the entry of ${name} has no known severity or no \`via\` list` };
    }
    entries[entry.severity] = (entries[entry.severity] ?? 0) + 1;
    for (const item of entry.via) {
      if (typeof item === "string") {
        if (!isRecord(vulnerabilities[item])) return { unread: `${name} is vulnerable through ${item}, which has no entry` };
        continue;
      }
      if (!isRecord(item) || typeof item.severity !== "string" || !SEVERITIES.includes(item.severity) || typeof item.name !== "string" || typeof item.url !== "string") {
        return { unread: `an advisory of ${name} has no known severity, package or address` };
      }
      // An advisory without a GitHub identifier cannot be excepted: it is named after its npm number.
      const id = /\/advisories\/(GHSA(?:-[0-9a-z]{4}){3})$/.exec(item.url)?.[1] ?? `npm-${String(item.source)}`;
      found.set(`${id} ${item.name}`, { id, severity: item.severity, package: item.name, title: typeof item.title === "string" ? item.title : "", url: item.url });
    }
  }
  for (const severity of [...SEVERITIES, "total"]) {
    const expected = severity === "total" ? Object.keys(vulnerabilities).length : entries[severity];
    if (counts[severity] !== expected) {
      return { unread: `metadata counts ${JSON.stringify(counts[severity])} ${severity} vulnerabilities, the report lists ${expected}` };
    }
  }
  /** @param {string} name @param {Set<string>} seen @returns {boolean} */
  const explained = (name, seen) => {
    const entry = /** @type {{ via: unknown[] }} */ (vulnerabilities[name]);
    seen.add(name);
    return entry.via.some((item) =>
      typeof item === "string" ? !seen.has(item) && explained(item, seen) : isRecord(item) && BLOCKING.includes(String(item.severity)),
    );
  };
  for (const [name, entry] of Object.entries(vulnerabilities)) {
    if (isRecord(entry) && BLOCKING.includes(String(entry.severity)) && !explained(name, new Set())) {
      return { unread: `${name} is ${String(entry.severity)}, and no high or critical advisory explains it` };
    }
  }
  return { advisories: [...found.values()] };
}

/**
 * The verdict on a report of `npm audit --json`, the exceptions applied.
 * @param {unknown} report the parsed JSON
 * @param {readonly Exception[]} exceptions
 * @returns {{ ok: boolean, lines: string[] }}
 */
export function judgeNpm(report, exceptions) {
  const read = readNpmReport(report);
  if ("unread" in read) {
    return { ok: false, lines: [`npm audit: the report cannot be read whole (${read.unread}). Nothing is excepted.`] };
  }
  const blocking = read.advisories.filter((advisory) => BLOCKING.includes(advisory.severity));
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

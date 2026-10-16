import { readFileSync, readdirSync, statSync } from "node:fs";
import { isAbsolute, join, relative, resolve } from "node:path";

/** @param {string} message @returns {never} */
function refuse(message) {
  console.error(`MEN validation: ${message}`);
  process.exit(1);
}

/** @param {string} block @param {string} key @returns {string} */
function field(block, key) {
  const values = [...block.matchAll(new RegExp(`^- ${key}: (.+)$`, "gm"))];
  const value = values[0]?.[1]?.trim();
  if (values.length !== 1 || !value) refuse(`expected exactly one ${key} field`);
  return value;
}

const docs = resolve(process.argv[2] ?? "docs");
const registryFile = process.argv[3] ?? new URL("men-linear-issues.tsv", import.meta.url);
// The docs CI deliberately has no npm install or Linear credentials. Parse the
// small TSV boundary with the standard library rather than a runtime dependency.
/** @type {Map<string, string>} */
const registry = new Map();
/** @type {Set<string>} */
const uuids = new Set();
for (const row of readFileSync(registryFile, "utf8").split(/\r?\n/)) {
  if (!row || row.startsWith("#")) continue;
  const parsed = /^(ANH-[1-9][0-9]*)\t([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\t(\d{4}-\d{2}-\d{2})\t(https:\/\/linear\.app\/anheart\/issue\/ANH-[1-9][0-9]*\/[a-z0-9-]+)$/.exec(row);
  if (!parsed) refuse("malformed Linear registry row");
  const [, id, uuid, date, url] = parsed;
  if (!id || !uuid || !date || !url) refuse("incomplete Linear registry row");
  if (url.split("/")[5] !== id) refuse(`registry URL mismatch: ${id}`);
  const timestamp = Date.parse(date);
  if (!Number.isFinite(timestamp) || new Date(timestamp).toISOString().slice(0, 10) !== date) {
    refuse(`invalid verification date: ${id}`);
  }
  if (registry.has(id) || uuids.has(uuid)) refuse(`duplicate Linear registry identity: ${id}`);
  registry.set(id, url);
  uuids.add(uuid);
}
if (registry.size === 0) refuse("empty Linear registry");

const catalogue = readFileSync(join(docs, "menaces.md"), "utf8").replaceAll("\r\n", "\n");
// Correctly written tickets must have been read wherever they occur, including
// code and addresses, so prose cannot bypass the verified registry.
for (const [, issueId] of catalogue.matchAll(/\b(ANH-[1-9][0-9]*)\b/g)) {
  if (!issueId || !registry.has(issueId)) refuse(`Linear issue named but not verified: ${issueId}`);
}

/** @param {string} text */
function* ticketNames(text) {
  for (const match of text.matchAll(/(?<![\p{L}\p{N}])a[*_~`\\]*n[*_~`\\]*h[\p{Zs}\s\p{Pd}−_\p{Cf}\\*~`#./:·()[\]]*\p{Nd}(?:[*_~`\\]*\p{Nd})*[\p{L}\p{N}_]*/giu)) {
    const before = text.slice(Math.max(0, match.index - 3), match.index);
    if (match[0].startsWith("anh") && (/[/#.-]/.test(text[match.index - 1] ?? "") || /[\p{L}\p{N}]_$/u.test(before))) continue;
    yield match[0];
  }
}

/** @param {string} url @returns {string} */
function issueAddress(url) {
  return url.split("#")[0];
}

/** @param {string} address @returns {URL | undefined} */
function linearUrl(address) {
  let parsed;
  try {
    parsed = new URL(/^www\./i.test(address) ? `http://${address}` : address);
  } catch {
    if (/linear\.app/i.test(address)) refuse(`unverified Linear issue link: ${JSON.stringify(address)}`);
    return;
  }
  const host = parsed.hostname.replace(/\.$/, "");
  if (host === "linear.app" || host.endsWith(".linear.app")) return parsed;
}

// Reference definitions and footnotes are outside the catalogue's inline link subset.
if (catalogue.includes("]:")) refuse("reference definition or footnote in the catalogue: write inline links");

/** @type {{url: string, start: number, end: number}[]} */
const destinations = [];
// Require a complete destination token so punctuation cannot hide its real host.
const destinationToken = /[^\s()<>]+\)/y;
for (const match of catalogue.matchAll(/\]\(/g)) {
  const start = match.index + 2;
  destinationToken.lastIndex = start;
  const token = destinationToken.exec(catalogue);
  if (!token) {
    refuse(`link destination not written as one token closed by a parenthesis: ${JSON.stringify(catalogue.slice(match.index, match.index + 200))}`);
  }
  const url = token[0].slice(0, -1);
  destinations.push({ url, start, end: start + url.length });
}

for (const match of catalogue.matchAll(/<(https?:\/\/[^<>\s]+|www\.[^<>\s]+)>/gi)) {
  const url = match[1];
  const start = match.index + 1;
  destinations.push({ url, start, end: start + url.length });
}

// Compare Linear issue addresses by host and exact registry spelling, ignoring
// fragments; only bare addresses may shed trailing prose punctuation.
const addresses = [...destinations];
for (const match of catalogue.matchAll(/(?:https?:\/\/|www\.)[^\s<>()[\]`"'*]+/gi)) {
  if (destinations.some(({ start, end }) => match.index >= start && match.index < end)) continue;
  addresses.push({ url: match[0].replace(/[.,;:!?_~]+$/g, ""), start: match.index, end: match.index + match[0].length });
}
for (const { url: address } of addresses) {
  const parsed = linearUrl(address);
  if (!parsed) continue;
  const issuePath = /(?:\/|%2f)issue(?:\/|%2f)([^/]*)/i.exec(parsed.pathname);
  if (!issuePath) refuse(`Linear address other than a registered ticket: ${address}`);
  const issueId = issuePath[1];
  if (!/^ANH-[1-9][0-9]*$/.test(issueId) || registry.get(issueId) !== issueAddress(address)) {
    refuse(`unverified Linear issue link: ${issueId}`);
  }
}

// Reject alternate ticket spellings, escapes and emphasis outside address tokens.
// Lowercase path fragments are exempt; an underscore must follow a letter or number.
let ticketText = catalogue;
for (const { url, start, end } of destinations) {
  if (!/[./#:]/.test(url)) continue;
  ticketText = ticketText.slice(0, start) + " ".repeat(end - start) + ticketText.slice(end);
}
ticketText = ticketText.replace(/<https?:\/\/[^<>\s]*>/gi, " ").replace(/(?:https?:\/\/|www\.)[^\s<>()[\]`"']*/gi, " ");
for (const name of ticketNames(ticketText)) {
  if (!/^ANH-[1-9][0-9]*$/.test(name)) {
    const readable = JSON.stringify(name).replace(/[\p{Cf}\p{Zs}]/gu, (char) => char === " " ? char : `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`);
    refuse(`ticket written other than ANH-n: ${readable}`);
  }
  if (!registry.has(name)) refuse(`Linear issue named but not verified: ${name}`);
}

// Character references can hide ticket letters or separators, so require literal characters.
const characterReference = /&(?:#(?:[0-9]+|x[0-9a-f]+)|[a-z][a-z0-9]*);/i.exec(catalogue);
if (characterReference) refuse(`character reference in the catalogue: write the character itself: ${JSON.stringify(characterReference[0])}`);

// HTML can hide ticket text; only HTTP(S) autolinks remain subject to the address checks.
// Code is not exempt: recognizing GitHub code spans would require reproducing rules this page does not need.
if (/<(?!https?:\/\/[^<>\s]+>)[a-z/!?]/i.test(catalogue)) refuse("HTML in the catalogue: write Markdown");

// Inline Linear links require the literal registry key and an unescaped opening bracket.
// Inspect only the adjacent label characters; destinations cannot start with whitespace.
for (const { url, start } of addresses) {
  const parsed = linearUrl(url);
  if (!parsed) continue;
  const issueId = /(?:\/|%2f)issue(?:\/|%2f)([^/]*)/i.exec(parsed.pathname)?.[1];
  const label = `[${issueId}](`;
  const labelStart = start - label.length;
  if (catalogue.slice(start - 2, start) === "](" && (catalogue.slice(Math.max(0, labelStart), start) !== label || catalogue[labelStart - 1] === "\\")) {
    refuse(`Linear link not written [ANH-n](its registry address): ${url}`);
  }
}
/** @type {Set<string>} */
const definitions = new Set();
for (const block of catalogue.split(/(?=^## )/m)) {
  const heading = /^## (MEN-[0-9]{2,})(?:[ \t].*)?$/m.exec(block);
  if (!heading) continue;
  const id = heading[1];
  if (!id) refuse("missing threat identifier");
  if (definitions.has(id)) refuse(`duplicate threat identifier: ${id}`);
  definitions.add(id);
  const status = field(block, "Status");
  if (!["OPEN", "MITIGATED", "ACCEPTED"].includes(status)) refuse(`unknown status: ${id}`);
  const issues = field(block, "Issues");
  const links = [...issues.matchAll(/\[(ANH-[1-9][0-9]*)\]\((https:\/\/[^\s)]+)\)/g)];
  if (!links.length || issues.replace(/\[(ANH-[1-9][0-9]*)\]\((https:\/\/[^\s)]+)\)/g, "").replace(/[,\s]/g, "")) {
    refuse(`expected Linear issue links: ${id}`);
  }
  for (const link of links) {
    const [, issueId, url] = link;
    if (!issueId || registry.get(issueId) !== url) refuse(`unverified Linear issue link: ${id} / ${issueId}`);
  }
  // Closed/accepted states cannot bypass coverage with a spelling change. The
  // referenced evidence is still assessed by the independent reviewer.
  if (status !== "OPEN") {
    const evidence = field(block, "Evidence");
    const path = /^\[[^\]]+\]\(([^\s)#]+)\)$/.exec(evidence)?.[1];
    if (!path || isAbsolute(path) || path.includes(":")) refuse(`expected local evidence link: ${id}`);
    const target = resolve(docs, path);
    if (relative(docs, target).startsWith("..")) refuse(`evidence outside docs: ${id}`);
    const file = statSync(target);
    if (!file.isFile() || file.size === 0) refuse(`empty evidence: ${id}`);
  }
  if (status === "ACCEPTED") {
    field(block, "Signed-by");
    const reviewed = field(block, "Reviewed-on");
    if (!/^\d{4}-\d{2}-\d{2}$/.test(reviewed) || !Number.isFinite(Date.parse(reviewed))) {
      refuse(`invalid acceptance date: ${id}`);
    }
  }
}
if (definitions.size === 0) refuse("catalogue defines no threat identifiers");

// Traverse documents without following directory symlinks outside the catalogue.
const directories = [docs];
for (const directory of directories) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) directories.push(path);
    if (!entry.isFile() || !entry.name.endsWith(".md")) continue;
    for (const reference of readFileSync(path, "utf8").matchAll(/\bMEN-[0-9]+\b/g)) {
      if (!definitions.has(reference[0])) refuse(`undefined ${reference[0]} in ${path}`);
    }
  }
}

// Keep the catalogue in its explicit character subset, after the more specific diagnostics.
const outsideAlphabet = /[^\n\x20-\x7e\u00a0\u00ab\u00b7\u00bb\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff\u0152\u0153\u2014\u2019\u202f\u2192\u2194]/u.exec(catalogue);
if (outsideAlphabet) {
  const point = outsideAlphabet[0].codePointAt(0).toString(16).toUpperCase().padStart(4, "0");
  const line = catalogue.slice(0, outsideAlphabet.index).split("\n").length;
  refuse(`character outside the catalogue alphabet: U+${point} at line ${line}`);
}
console.log(`MEN validation passed: ${definitions.size} threats; ${registry.size} verified Linear issues.`);

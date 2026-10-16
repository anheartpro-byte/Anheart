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
  const normalized = text.normalize("NFKC");
  for (const match of normalized.matchAll(/(?<![\p{L}\p{N}])anh[\p{Zs}\s\p{Pd}−_\p{Cf}]*\p{Nd}+[\p{L}\p{N}_]*/giu)) {
    if (match[0].startsWith("anh") && /[/#._-]/.test(normalized[match.index - 1] ?? "")) continue;
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
  if (parsed.hostname === "linear.app" || parsed.hostname.endsWith(".linear.app")) return parsed;
}

/** @param {string} label @returns {string} */
function referenceLabel(label) {
  return label.trim().replace(/\s+/g, " ").toLowerCase();
}
/** @type {Map<string, string[]>} */
const references = new Map();
/** @type {{url: string, start: number, end: number}[]} */
const destinations = [];
for (const match of catalogue.matchAll(/\[([^\]]+)\]:[ \t]*(?:\n[ \t]*(?:>[ \t]*)?)?(?:<([^<>\n]*)>|(\S+))/g)) {
  const url = match[2] ?? match[3];
  const start = match.index + match[0].lastIndexOf(url);
  destinations.push({ url, start, end: start + url.length });
  const key = referenceLabel(match[1]);
  const addresses = references.get(key) ?? [];
  addresses.push(url);
  references.set(key, addresses);
}

/** @param {number} start @returns {{url: string, start: number, end: number} | undefined} */
function inlineDestination(start) {
  let cursor = start + 1;
  while (/\s/.test(catalogue[cursor] ?? "")) cursor++;
  const angled = catalogue[cursor] === "<";
  if (angled) cursor++;
  const addressStart = cursor;
  let depth = 0;
  for (; cursor < catalogue.length; cursor++) {
    const char = catalogue[cursor];
    if (char === "\\") { cursor++; continue; }
    if (angled) {
      if (char === ">") break;
    } else {
      if (char === "(") depth++;
      if (char === ")") {
        if (depth === 0) break;
        depth--;
      }
      if (/\s/.test(char)) break;
    }
  }
  const end = cursor;
  if (angled && catalogue[cursor++] !== ">") return;
  if (!/^\s*(?:(?:"[^"]*"|'[^']*'|\([^)]*\))\s*)?\)/.test(catalogue.slice(cursor))) return;
  return { url: catalogue.slice(addressStart, end), start: addressStart, end };
}

/** @type {{label: string, url: string}[]} */
const markdownLinks = [];
const brackets = [];
for (let index = 0; index < catalogue.length; index++) {
  const char = catalogue[index];
  if (char === "\\") { index++; continue; }
  if (char === "[") brackets.push(index);
  if (char !== "]" || !brackets.length) continue;
  const start = brackets.pop();
  const label = catalogue.slice(start + 1, index);
  const after = catalogue.slice(index + 1);
  if (after.startsWith(":")) continue;
  if (after.startsWith("(")) {
    const destination = inlineDestination(index + 1);
    if (destination) {
      destinations.push(destination);
      markdownLinks.push({ label, url: destination.url });
    }
  } else {
    const reference = /^\[([^\]]*)\]/.exec(after);
    const addresses = references.get(referenceLabel(reference?.[1] || label)) ?? [];
    for (const url of addresses) markdownLinks.push({ label, url });
    if (reference) index += reference[0].length;
  }
}

for (const match of catalogue.matchAll(/<(https?:\/\/[^<>\s]+|www\.[^<>\s]+)>/gi)) {
  const url = match[1];
  const start = match.index + 1;
  destinations.push({ url, start, end: start + url.length });
}

// Compare Linear issue addresses by host and exact registry spelling, ignoring
// fragments; only bare addresses may shed trailing prose punctuation.
const addresses = destinations.map(({ url }) => url);
for (const match of catalogue.matchAll(/(?:https?:\/\/|www\.)[^\s<>()[\]`"'*]+/gi)) {
  if (destinations.some(({ start, end }) => match.index >= start && match.index < end)) continue;
  addresses.push(match[0].replace(/[.,;:!?_~]+$/g, ""));
}
for (const address of addresses) {
  const parsed = linearUrl(address);
  if (!parsed) continue;
  const issueId = /^\/[^/]+\/issue\/([^/]+)/i.exec(parsed.pathname)?.[1];
  if (issueId && (!/^ANH-[1-9][0-9]*$/.test(issueId) || registry.get(issueId) !== issueAddress(address))) {
    refuse(`unverified Linear issue link: ${issueId}`);
  }
}

// Reject alternate ticket spellings in visible text, retaining link titles.
// Lowercase path, anchor and filename fragments are deliberately not ticket names.
let ticketText = catalogue;
for (const { start, end } of destinations.sort((a, b) => b.start - a.start)) {
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

// Every ticket named by a Markdown label must identify its Linear destination,
// including formatted labels, images and all three reference link forms.
for (const { label, url } of markdownLinks) {
  if (!linearUrl(url)) continue;
  for (const name of ticketNames(label)) {
    const number = /\p{Nd}+/u.exec(name)?.[0];
    const issueId = `ANH-${number}`;
    if (registry.get(issueId) !== issueAddress(url)) {
      refuse(`Linear link labelled ${issueId} points to another address`);
    }
  }
}

// HTML anchors bypass Markdown label checks, so require Markdown links instead.
if (/<a(?=[\s>/])/i.test(catalogue)) refuse("HTML link in the catalogue: write Markdown links");
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
console.log(`MEN validation passed: ${definitions.size} threats; ${registry.size} verified Linear issues.`);

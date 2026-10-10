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

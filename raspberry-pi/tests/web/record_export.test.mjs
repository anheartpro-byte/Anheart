// The « Exporter l'enregistrement » button of the console page: it asks the machine for its
// records, downloads the latest one with the token header, and says what happened.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const assets = new URL("../../src/web/static/", import.meta.url);
const html = readFileSync(new URL("index.html", assets), "utf8");
const source = readFileSync(new URL("app.js", assets), "utf8");

class Element {
  constructor(text = "") {
    this.className = "";
    this.textContent = text;
    this.clicked = 0;
  }
  click() {
    this.clicked += 1;
  }
}

// The shipped page and script, with a machine that answers what the test says.
function page({ records, archive }) {
  const nodes = new Map(
    [...html.matchAll(/<[a-z0-9]+\b[^>]*\bid="([^"]+)"[^>]*>([^<]*)/g)].map((tag) => [
      tag[1],
      new Element(tag[2].trim()),
    ]),
  );
  const links = [];
  const fetched = [];
  const revoked = [];
  const context = vm.createContext({
    document: {
      title: "",
      documentElement: { style: { setProperty: () => undefined } },
      getElementById: (id) => nodes.get(id) ?? null,
      createElement: () => {
        const link = new Element();
        links.push(link);
        return link;
      },
    },
    window: { addEventListener: () => undefined, sessionStorage: { getItem: () => null, setItem: () => undefined } },
    performance: { now: () => 0 },
    URL: { createObjectURL: (blob) => `blob:${blob.size}`, revokeObjectURL: (url) => revoked.push(url) },
    encodeURIComponent,
    fetch: (url, init) => {
      fetched.push({ url, init });
      return Promise.resolve(archive);
    },
  });
  vm.runInContext(source, context, { filename: fileURLToPath(new URL("app.js", assets)) });
  context.api = (path) => {
    assert.equal(path, "/api/records");
    return records instanceof Error ? Promise.reject(records) : Promise.resolve(records);
  };
  return { context, nodes, links, fetched, revoked };
}

const settled = () => new Promise((resolve) => setTimeout(resolve, 0));

test("the page has the export button, labelled as the operator is told", () => {
  assert.match(html, /<button id="record-export"[^>]*>Exporter l'enregistrement<\/button>/);
  assert.match(source, /el\("record-export"\)\.onclick = exportRecord;/);
});

test("the button downloads the latest record with the token and names the file after it", async () => {
  const name = "2026-10-06T101112Z_0123456789abcdef";
  const { context, nodes, links, fetched, revoked } = page({
    records: { recording: true, records: [{ name, closed: true }, { name: "2026-10-01T000000Z_older", closed: true }] },
    archive: { ok: true, blob: () => Promise.resolve({ size: 4096 }) },
  });
  context.state.token = "a-sixteen-char-token";
  context.exportRecord();
  await settled();
  assert.equal(fetched.length, 1);
  assert.equal(fetched[0].url, `/api/records/${name}/archive`);
  assert.equal(fetched[0].init.headers["X-Anheart-Token"], "a-sixteen-char-token");
  assert.equal(links.length, 1);
  assert.equal(links[0].download, `${name}.tar.gz`);
  assert.equal(links[0].href, "blob:4096");
  assert.equal(links[0].clicked, 1);
  assert.deepEqual(revoked, ["blob:4096"]);
  assert.equal(nodes.get("record-note").textContent, `exporte : ${name}.tar.gz`);
  assert.equal(nodes.get("record-note").className, "note");
});

test("the machine's refusal is shown, and nothing is downloaded", async () => {
  const refused = page({
    records: { recording: true, records: [{ name: "2026-10-06T101112Z_abc", closed: true }] },
    archive: {
      ok: false,
      status: 409,
      json: () => Promise.resolve({ detail: "export refuse pendant une seance : attendre le retour au repos" }),
    },
  });
  refused.context.exportRecord();
  await settled();
  assert.equal(refused.links.length, 0);
  assert.equal(
    refused.nodes.get("record-note").textContent,
    "export refuse pendant une seance : attendre le retour au repos",
  );
  assert.equal(refused.nodes.get("record-note").className, "note note-bad");
  assert.equal(refused.fetched[0].init.headers["X-Anheart-Token"], undefined, "no token, no header");
});

test("no record, or no recording at all, is said in words", async () => {
  const empty = page({ records: { recording: true, records: [] }, archive: null });
  empty.context.exportRecord();
  await settled();
  assert.equal(empty.fetched.length, 0);
  assert.equal(empty.nodes.get("record-note").textContent, "aucun enregistrement sur cette machine");

  const off = page({ records: { recording: false, records: [] }, archive: null });
  off.context.exportRecord();
  await settled();
  assert.equal(off.nodes.get("record-note").textContent, "cette console n'enregistre pas");

  const down = page({ records: new Error("a valid x-anheart-token header is required"), archive: null });
  down.context.exportRecord();
  await settled();
  assert.equal(down.nodes.get("record-note").textContent, "a valid x-anheart-token header is required");
});

// The commissioning card of the console page (/api/commissioning, ANH-115): hidden on a console
// linked to no dashboard, why none can start, what the dashboard recorded, a run asked once.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const assets = new URL("../../src/web/static/", import.meta.url);
const html = readFileSync(new URL("index.html", assets), "utf8");
const source = readFileSync(new URL("app.js", assets), "utf8");

// A node as the renderer uses it: classes, text, children, a value and a disabled flag.
class Element {
  constructor(classes = "", text = "") {
    this.className = classes;
    this.children = [];
    this.text = text;
    this.value = "";
    this.disabled = false;
    this.classList = {
      toggle: (name, enabled) => {
        if (enabled) this.classes.add(name);
        else this.classes.delete(name);
      },
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
      contains: (name) => this.classes.has(name),
    };
  }
  get className() {
    return [...this.classes].join(" ");
  }
  set className(value) {
    this.classes = new Set(String(value).split(/\s+/).filter(Boolean));
  }
  get textContent() {
    return this.text;
  }
  set textContent(value) {
    this.text = String(value);
    this.children = [];
  }
  set innerHTML(value) {
    throw new Error(`the page wrote markup: ${value}`);
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  setAttribute() {}
  addEventListener() {}
}

function page(answer = () => new Promise(() => undefined)) {
  const nodes = new Map(
    [...html.matchAll(/<[a-z0-9]+\b[^>]*\bid="([^"]+)"[^>]*>([^<]*)/g)].map((tag) => [
      tag[1],
      new Element(/\bclass="([^"]*)"/.exec(tag[0])?.[1], tag[2].trim()),
    ]),
  );
  const asked = [];
  const context = vm.createContext({
    document: {
      title: "",
      documentElement: { style: { setProperty: () => undefined } },
      getElementById: (id) => nodes.get(id) ?? null,
      createElement: () => new Element(),
    },
    window: {
      addEventListener: () => undefined,
      sessionStorage: { getItem: () => null, setItem: () => undefined },
      setTimeout: () => undefined,
      alert: () => undefined,
    },
    performance: { now: () => 0 },
  });
  vm.runInContext(source, context, { filename: fileURLToPath(new URL("app.js", assets)) });
  context.api = (path, options) => {
    asked.push([path, options]);
    return answer(path, options);
  };
  const node = (id) => {
    assert.ok(nodes.has(id), `the page has no #${id}`);
    return nodes.get(id);
  };
  return { context, node, asked };
}

const CONTROLS = [
  { id: "drive_link", label: "Liaison variateur et parametres relus", state: "ok", detail: "variateur lu il y a 0.4 s" },
  { id: "sto_source", label: "Source de la chaine STO", state: "block", detail: "STO ponte declare" },
];
const RECORDED = {
  at: 1_800_000_000_000,
  operator: "user-1",
  controls: CONTROLS,
  recorded: true,
  record_id: "record-1",
  verdict: "nonconforming",
  findings: [{ id: "sto_bridged", expected: "drive_input", read: "bridged_declared" }],
  signed: false,
  reason: null,
};
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("the card sits under the pre-flight, hidden until the console says it offers one", () => {
  const view = /<section id="view-preflight"[\s\S]*?<\/section>/.exec(html)?.[0] ?? "";
  assert.match(view, /<div id="commissioning-card" class="card hidden">/);
  assert.match(view, /<p id="commissioning-note" class="note" role="status"><\/p>/);
  assert.match(view, /STO ponte ou simule n'est jamais enregistree conforme/);
  assert.match(source, /el\("commissioning-run"\)\.onclick = doRunCommissioning;/);
  assert.match(source, /window\.setInterval\(loadCommissioning, PANEL_MS\);/);
  assert.match(source, /\n  loadCommissioning\(\);\n/);
});

test("a console linked to no dashboard keeps the card hidden", () => {
  const { context, node } = page();
  context.renderCommissioning(null);
  assert.ok(node("commissioning-card").classes.has("hidden"));
});

test("before any commissioning: why none can start, the button disabled; then offered", () => {
  const { context, node } = page();
  context.renderCommissioning({ running: false, refusal: "console pas au repos : attendre l'arret complet", last: null });
  assert.equal(node("commissioning-card").classes.has("hidden"), false);
  assert.equal(node("commissioning-state").textContent, "aucune");
  assert.equal(
    node("commissioning-summary").textContent,
    "Aucune mise en service depuis le demarrage de la console. Pas de mise en service maintenant : console pas au repos : attendre l'arret complet.",
  );
  assert.equal(node("commissioning-run").disabled, true);
  context.renderCommissioning({ running: false, refusal: null, last: null });
  assert.equal(node("commissioning-run").disabled, false);
  assert.equal(node("commissioning-run").textContent, "Lancer la mise en service");
});

test("a recorded one: its verdict, signed or not, each finding and each control", () => {
  const { context, node } = page();
  context.renderCommissioning({ running: false, refusal: null, last: RECORDED });
  assert.equal(node("commissioning-state").textContent, "non conforme");
  assert.ok(node("commissioning-state").classes.has("pill-bad"));
  assert.equal(node("commissioning-summary").textContent, "Enregistree par le tableau de bord, non signee : 1 ecart(s).");
  assert.deepEqual(
    node("commissioning-findings").children.map((item) => item.textContent),
    ["sto_bridged : attendu drive_input, lu bridged_declared"],
  );
  const rows = node("commissioning-rows").children;
  assert.equal(rows.length, 2);
  assert.equal(rows[1].children[0].textContent, "Source de la chaine STO");
  assert.equal(rows[1].children[1].children[0].textContent, "BLOQUANT");
  assert.equal(rows[1].children[2].textContent, "STO ponte declare");
  context.renderCommissioning({
    running: false,
    refusal: null,
    last: { ...RECORDED, verdict: "conforming", findings: [], signed: true },
  });
  assert.equal(node("commissioning-state").textContent, "conforme");
  assert.ok(node("commissioning-state").classes.has("pill-good"));
  assert.equal(node("commissioning-summary").textContent, "Enregistree par le tableau de bord, signee : aucun ecart.");
  assert.equal(node("commissioning-findings").children.length, 0);
});

test("one the dashboard did not record says why", () => {
  const { context, node } = page();
  context.renderCommissioning({
    running: false,
    refusal: null,
    last: { ...RECORDED, recorded: false, record_id: null, verdict: null, findings: [], signed: null, reason: "tableau de bord injoignable : ConnectError" },
  });
  assert.equal(node("commissioning-state").textContent, "non enregistree");
  assert.equal(node("commissioning-summary").textContent, "Non enregistree : tableau de bord injoignable : ConnectError.");
});

test("a run asked for disables the button until its answer, then shows it", async () => {
  const { context, node, asked } = page(() =>
    Promise.resolve({ running: false, refusal: null, last: RECORDED }),
  );
  context.renderCommissioning({ running: false, refusal: null, last: null });
  context.doRunCommissioning();
  assert.equal(node("commissioning-run").disabled, true);
  assert.match(node("commissioning-note").textContent, /^mise en service en cours/);
  context.renderCommissioning({ running: false, refusal: null, last: null });
  assert.equal(node("commissioning-run").textContent, "Mise en service en cours...");
  await settle();
  assert.equal(asked[0][0], "/api/commissioning/run");
  assert.equal(asked[0][1].method, "POST");
  assert.equal(node("commissioning-note").textContent, "mise en service terminee");
  assert.equal(node("commissioning-state").textContent, "non conforme");
  assert.equal(node("commissioning-run").disabled, false);
});

test("a run refused says the console's words, and reads the card again", async () => {
  let calls = 0;
  const { context, node, asked } = page((path) => {
    calls += 1;
    return path === "/api/commissioning/run"
      ? Promise.reject(new Error("mise en service interrompue, rien n'est envoye : la console a quitte le repos"))
      : Promise.resolve({ running: false, refusal: null, last: null });
  });
  context.doRunCommissioning();
  await settle();
  await settle();
  assert.equal(node("commissioning-note").textContent, "mise en service interrompue, rien n'est envoye : la console a quitte le repos");
  assert.ok(node("commissioning-note").classes.has("note-bad"));
  assert.deepEqual(asked.map(([path]) => path), ["/api/commissioning/run", "/api/commissioning"]);
  assert.equal(calls, 2);
  assert.equal(node("commissioning-run").disabled, false);
});

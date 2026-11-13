// The pre-flight page (ANH-142): the console's list of controls, for the bench and for a
// person on board, as GET /api/preflight sends it; and the starts it holds back. The page
// writes the console's words and decides nothing: the console judges every start again.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const assets = new URL("../../src/web/static/", import.meta.url);
const html = readFileSync(new URL("index.html", assets), "utf8");
const source = readFileSync(new URL("app.js", assets), "utf8");

// A node as the renderer uses it: classes, text and children. It has no way to take markup.
class Element {
  constructor(classes = "", text = "") {
    this.className = classes;
    this.children = [];
    this.style = {};
    this.value = "";
    this.text = text;
    this.disabled = false;
    this.offsetHeight = 0;
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

function page() {
  const nodes = new Map(
    [...html.matchAll(/<[a-z0-9]+\b[^>]*\bid="([^"]+)"[^>]*>([^<]*)/g)].map((tag) => [
      tag[1],
      new Element(/\bclass="([^"]*)"/.exec(tag[0])?.[1], tag[2].trim()),
    ]),
  );
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
    },
    performance: { now: () => 1000 },
  });
  vm.runInContext(source, context, { filename: fileURLToPath(new URL("app.js", assets)) });
  context.api = () => new Promise(() => undefined);
  const node = (id) => {
    assert.ok(nodes.has(id), `the page has no #${id}`);
    return nodes.get(id);
  };
  return { context, node };
}

const control = (id, state, detail) => ({ id, label: `controle ${id}`, state, detail });

// What the console sends (src/web/preflight_routes.py), cut to three controls.
function preflightRow({ running = false, occupiedBlock = "pre-vol : batterie BITalino 20 % < 30 %" } = {}) {
  return {
    running,
    last: "block",
    battery_pct: 20.4,
    bench: {
      occupancy: "bench",
      verdict: "warn",
      first_block: null,
      controls: [
        control("estop", "ok", "attestation de l'arret d'urgence donnee"),
        control("bitalino", "warn", "batterie BITalino 20 % < 30 %"),
        control("ecg", "ok", "sans objet au banc : aucune personne a bord"),
      ],
    },
    occupied: {
      occupancy: "occupied",
      verdict: occupiedBlock ? "block" : "ok",
      first_block: occupiedBlock,
      controls: [
        control("estop", "ok", "attestation de l'arret d'urgence donnee"),
        control("bitalino", occupiedBlock ? "block" : "ok", "batterie BITalino 20 % < 30 %"),
        control("ecg", running ? "pending" : "ok", "qualite ECG good depuis 12 s, 30 s continues requises"),
      ],
    },
  };
}

test("the pre-flight page is in the navigation and has its list and its button", () => {
  assert.match(html, /<button id="nav-preflight" class="nav-item" type="button">Pre-vol<\/button>/);
  assert.match(html, /<section id="view-preflight" class="view hidden">/);
  assert.match(html, /<button id="preflight-run" class="btn" type="button">Relancer<\/button>/);
});

test("each control is one line: its label, its state at the bench and on board, and why", () => {
  const { context, node } = page();
  context.renderPreflight(preflightRow());
  const rows = node("preflight-rows").children;
  assert.equal(rows.length, 3);
  const battery = rows[1].children;
  assert.equal(battery[0].textContent, "controle bitalino");
  assert.equal(battery[1].children[0].textContent, "avertissement");
  assert.equal(battery[2].children[0].textContent, "BLOQUANT");
  assert.ok(battery[2].children[0].classes.has("pill-bad"));
  assert.equal(battery[3].textContent, "batterie BITalino 20 % < 30 %");
  assert.equal(node("preflight-state").textContent, "BLOQUANT");
  assert.match(node("preflight-summary").textContent, /Batterie BITalino : 20 %/);
  assert.equal(node("preflight-run").disabled, false);
});

test("a red blocking control holds back the start it concerns, with the console's reason", () => {
  const { context, node } = page();
  context.renderPreflight(preflightRow());
  assert.equal(node("start").disabled, true);
  assert.equal(node("start-preflight-note").textContent, "Depart refuse, pre-vol : batterie BITalino 20 % < 30 %");
  // A warning at the bench holds nothing back.
  assert.equal(node("manual-preflight-note").textContent, "");
  context.renderPreflight(preflightRow({ occupiedBlock: null }));
  assert.equal(node("start-preflight-note").textContent, "");
});

test("a run under way says so, its controls waited for", () => {
  const { context, node } = page();
  context.renderPreflight(preflightRow({ running: true, occupiedBlock: null }));
  assert.equal(node("preflight-state").textContent, "en cours");
  assert.match(node("preflight-summary").textContent, /^Pre-vol en cours\./);
  assert.equal(node("preflight-rows").children[2].children[2].children[0].textContent, "en cours");
});

test("a console without a pre-flight says so and offers no run", () => {
  const { context, node } = page();
  context.renderPreflight(null);
  assert.equal(node("preflight-state").textContent, "aucun");
  assert.equal(node("preflight-run").disabled, true);
  assert.equal(node("preflight-rows").children.length, 0);
  assert.equal(node("start-preflight-note").textContent, "");
});

test("a stop the dashboard may not count is said, with its acknowledgement by name (ANH-145)", async () => {
  const { context, node } = page();
  const held = "seance terminee sur emergency_stop pas encore recue par le tableau de bord";
  context.renderPreflight(preflightRow({ occupiedBlock: null }));
  assert.equal(node("preflight-ack").hidden, true);
  assert.equal(node("preflight-held").hidden, true);
  context.renderPreflight({ ...preflightRow(), unreported_stop: held });
  assert.equal(node("preflight-ack").hidden, false);
  assert.equal(node("preflight-held").textContent, `Personne a bord retenue : ${held}`);
  const sent = [];
  context.api = (path, options) => {
    sent.push([path, options]);
    return Promise.resolve({ ...preflightRow({ occupiedBlock: null }), unreported_stop: null });
  };
  node("manual-operator").value = "Mme Durand";
  context.doAcknowledgeStops();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(sent.length, 1);
  assert.equal(sent[0][0], "/api/preflight/acknowledge");
  assert.equal(JSON.stringify(sent[0][1]), JSON.stringify({ method: "POST", body: { operator: "Mme Durand" } }));
  assert.equal(node("preflight-ack").hidden, true);
  assert.match(node("preflight-note").textContent, /acquittee/);
});

test("a refused acknowledgement is said where the page says what it did", async () => {
  const { context, node } = page();
  context.api = () => Promise.reject(new Error("aucune seance mal terminee n'est retenue"));
  context.doAcknowledgeStops();
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(node("preflight-note").textContent, /aucune seance mal terminee/);
});

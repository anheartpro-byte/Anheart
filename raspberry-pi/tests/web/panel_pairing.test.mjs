// The first-power-up screen of the console page (/api/pairing, ANH-115): shown only while the
// console has no key, the Pi's serial number, the address field when the console has none, the
// steps still to do, and the code sent once and never kept on the page.
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

const STEPS = [
  { id: "ota", label: "Inscription au service de mise a jour", detail: "pas encore faite (ANH-169)" },
  { id: "network", label: "Reseau de la console", detail: "pas encore cree (ANH-151)" },
];
const UNPAIRED = {
  serial: "10000000c0ffee42",
  serial_problem: null,
  address: "https://deploiement.convex.site",
  key_prefix: null,
  steps: STEPS,
};
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("the card is the first of the console's page, hidden until the console says it has no key", () => {
  const view = /<section id="view-console"[\s\S]*?<\/section>/.exec(html)?.[0] ?? "";
  assert.match(view, /<h1 class="page-title">Tableau de bord<\/h1>\s*<!--[\s\S]*?-->\s*<div id="pairing-card" class="card hidden">/);
  assert.match(view, /<p id="pairing-note" class="note" role="status"><\/p>/);
  assert.match(source, /el\("pairing-submit"\)\.onclick = doPair;/);
  assert.match(source, /window\.setInterval\(loadPairing, PANEL_MS\);/);
});

test("a console with no key shows its Pi and the steps to come, and asks no address it has", () => {
  const { context, node } = page();
  context.renderPairing(UNPAIRED);
  assert.equal(node("pairing-card").classes.has("hidden"), false);
  assert.equal(node("pairing-serial").textContent, "10000000c0ffee42");
  assert.ok(node("pairing-address-row").classes.has("hidden"));
  assert.equal(node("pairing-submit").disabled, false);
  assert.deepEqual(
    node("pairing-steps").children.map((item) => item.textContent),
    [
      "Inscription au service de mise a jour : pas encore faite (ANH-169)",
      "Reseau de la console : pas encore cree (ANH-151)",
    ],
  );
});

test("a console with no address asks for it; one that cannot read its serial offers no pairing", () => {
  const { context, node } = page();
  context.renderPairing({ ...UNPAIRED, address: null });
  assert.equal(node("pairing-address-row").classes.has("hidden"), false);
  context.renderPairing({ ...UNPAIRED, serial: null, serial_problem: "illisible (OSError:ENOENT)" });
  assert.equal(node("pairing-serial").textContent, "illisible : illisible (OSError:ENOENT)");
  assert.ok(node("pairing-serial").classes.has("bad"));
  assert.equal(node("pairing-submit").disabled, true);
});

test("a console just paired says the key's prefix and offers nothing more", () => {
  const { context, node } = page();
  context.renderPairing({ ...UNPAIRED, key_prefix: "anh1.ab12cd34" });
  assert.equal(
    node("pairing-note").textContent,
    "console appairee (cle anh1.ab12cd34) : elle redemarre au repos, reliee au tableau de bord",
  );
  assert.equal(node("pairing-submit").disabled, true);
});

test("a console that has a key hides the card", async () => {
  const { context, node } = page(() => Promise.reject(Object.assign(new Error("deja une cle"), { status: 409 })));
  node("pairing-card").classes.delete("hidden");
  context.loadPairing();
  await settle();
  assert.ok(node("pairing-card").classes.has("hidden"));
});

test("pairing sends the code and the address typed, then never keeps the code", async () => {
  const { context, node, asked } = page(() =>
    Promise.resolve({ key_prefix: "anh1.ab12cd34", steps: STEPS }),
  );
  node("pairing-code").value = "7KQ2-M4XP";
  node("pairing-address").value = "  https://deploiement.convex.site ";
  context.doPair();
  await settle();
  assert.equal(asked[0][0], "/api/pairing");
  assert.equal(asked[0][1].method, "POST");
  assert.deepEqual({ ...asked[0][1].body }, { code: "7KQ2-M4XP", address: "https://deploiement.convex.site" });
  assert.match(node("pairing-note").textContent, /^console appairee \(cle anh1\.ab12cd34\)/);
  assert.equal(node("pairing-code").value, "");
  assert.equal(node("pairing-submit").disabled, true);
});

test("a refusal is shown as the console says it, the code is cleared, and the button is back", async () => {
  const { context, node, asked } = page(() => Promise.reject(new Error("code refuse : aucun code en cours pour ce Pi")));
  node("pairing-code").value = "7KQ2-M4XP";
  context.doPair();
  await settle();
  assert.equal(asked[0][1].body.address, null);
  assert.match(node("pairing-note").textContent, /^code refuse/);
  assert.ok(node("pairing-note").classes.has("note-bad"));
  assert.equal(node("pairing-code").value, "");
  assert.equal(node("pairing-submit").disabled, false);
});

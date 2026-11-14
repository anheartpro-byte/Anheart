// What the console page shows of the machine's configuration from the dashboard (/api/config,
// ANH-141): its name and place in the sidebar, and on the safety page a revision that changes a
// safety key, each change, and the button an operator signed in presses at rest. Nothing else on
// the page applies it.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const assets = new URL("../../src/web/static/", import.meta.url);
const html = readFileSync(new URL("index.html", assets), "utf8");
const source = readFileSync(new URL("app.js", assets), "utf8");

// A node as the renderer uses it: classes, text, children and a click. It takes no markup.
class Element {
  constructor(classes = "", text = "") {
    this.className = classes;
    this.children = [];
    this.text = text;
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

const ENV_CONFIG = {
  revision: null,
  config_hash: null,
  name: null,
  location: null,
  pending: null,
  refusal: null,
  restarting: false,
};
const WAITING = {
  ...ENV_CONFIG,
  revision: 3,
  config_hash: "ab".repeat(32),
  name: "Banc B",
  location: "Lyon",
  pending: {
    revision: 4,
    changes: [
      { key: "location", safety: false, before: "Lyon", after: "Paris" },
      { key: "MOTOR_MAX_RPM", safety: true, before: "300", after: "400" },
    ],
  },
};

test("the button is on the safety page, hidden until a revision waits", () => {
  const pages = [...html.matchAll(/<section id="view-[\s\S]*?<\/section>/g)].map((view) => view[0]);
  const safety = pages.find((view) => view.startsWith('<section id="view-safety"')) ?? "";
  assert.match(safety, /<div id="config-card" class="hidden">/);
  assert.match(
    safety,
    /<button id="config-apply" class="btn hidden" type="button">Appliquer la configuration de sûreté<\/button>/,
  );
  assert.match(source, /el\("config-apply"\)\.onclick = doApplyConfig;/);
});

test("a console on its .env says so, and shows no card", () => {
  const { context, node } = page();
  context.renderConfig(ENV_CONFIG);
  assert.equal(node("machine-identity").textContent, "configuration .env");
  assert.ok(node("config-card").classes.has("hidden"));
});

test("a revision that changes a safety key shows each change and the button", () => {
  const { context, node } = page();
  context.renderConfig(WAITING);
  assert.equal(node("machine-identity").textContent, "Banc B, Lyon");
  assert.equal(node("config-card").classes.has("hidden"), false);
  assert.equal(node("config-apply").classes.has("hidden"), false);
  assert.deepEqual(
    node("config-changes").children.map((item) => item.textContent),
    ["location : Lyon -> Paris", "MOTOR_MAX_RPM : 300 -> 400 (surete)"],
  );
  assert.match(node("config-summary").textContent, /^revision 4 : cles de surete changees/);
});

test("a refusal and a restart are said, with no button", () => {
  const { context, node } = page();
  context.renderConfig({ ...ENV_CONFIG, revision: 2, refusal: "revision 3 refusee : MOTOR_MAX_RPM" });
  assert.equal(node("machine-identity").textContent, "revision 2");
  assert.ok(node("config-apply").classes.has("hidden"));
  assert.match(node("config-summary").textContent, /^configuration refusee, la console garde la sienne/);
  context.renderConfig({ ...WAITING, restarting: true });
  assert.ok(node("config-apply").classes.has("hidden"));
  assert.equal(node("config-summary").textContent, "configuration appliquee : la console redemarre au repos");
});

test("applying sends no body, says the revision, and asks the console again", async () => {
  const { context, node, asked } = page((path) =>
    Promise.resolve(path === "/api/config/apply" ? { revision: 4 } : WAITING),
  );
  context.doApplyConfig();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(asked[0][0], "/api/config/apply");
  assert.equal(asked[0][1].method, "POST");
  assert.equal(asked[0][1].body, undefined);
  assert.equal(node("config-note").textContent, "revision 4 appliquee : la console redemarre au repos");
  assert.equal(asked[1][0], "/api/config");
  assert.equal(node("config-apply").disabled, false);
});

test("a refusal of the console is shown as it says it", async () => {
  const { context, node } = page(() =>
    Promise.reject(new Error("console pas au repos : terminer la seance avant d'appliquer")),
  );
  context.doApplyConfig();
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(node("config-note").textContent, /pas au repos/);
  assert.ok(node("config-note").classes.has("note-bad"));
});

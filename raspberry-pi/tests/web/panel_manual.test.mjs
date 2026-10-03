import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const assets = new URL("../../src/web/static/", import.meta.url);
const html = readFileSync(new URL("index.html", assets), "utf8");
const source = readFileSync(new URL("app.js", assets), "utf8");

class Element {
  constructor() {
    this.classes = new Set();
    this.children = [];
    this.textContent = "";
    this.className = "";
    this.innerHTML = "";
    this.value = "";
    this.listeners = new Map();
    this.classList = {
      toggle: (name, enabled) => {
        if (enabled) this.classes.add(name);
        else this.classes.delete(name);
      },
      contains: (name) => this.classes.has(name),
    };
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  addEventListener(name, listener) {
    this.listeners.set(name, listener);
  }
}

function panel(storage = new Map()) {
  const nodes = new Map(
    [...html.matchAll(/id="([^"]+)"/g)].map((match) => [match[1], new Element()]),
  );
  const context = vm.createContext({
    document: {
      getElementById: (id) => nodes.get(id) ?? null,
      createElement: () => new Element(),
    },
    window: {
      addEventListener: () => undefined,
      sessionStorage: {
        getItem: (key) => storage.get(key) ?? null,
        setItem: (key, value) => storage.set(key, value),
      },
    },
  });
  vm.runInContext(source, context, { filename: fileURLToPath(new URL("app.js", assets)) });
  context.state.panel = { radius_m: 1.5 };
  return { context, nodes };
}

function wiredPanel(storage = new Map()) {
  const current = panel(storage);
  current.context.boot = () => undefined;
  current.context.loadCamera = () => undefined;
  current.context.showView = () => undefined;
  current.context.window.setInterval = () => undefined;
  current.context.window.requestAnimationFrame = () => undefined;
  current.context.document.querySelector = () => new Element();
  current.context.start();
  return current;
}

const speed = {
  motor_rpm: 0,
  output_rpm: 0,
  hertz: 0,
  g_load: 0,
  resultant_g: 1,
};
const manual = {
  occupancy_label: "bench",
  target: speed,
  ceiling: speed,
  min_run: speed,
  ramping: false,
  ramp_eta_s: 0,
};

for (const mode of ["repos", "manuel", "arret"]) {
  test(`manual controls follow ${mode}, not retained session data`, () => {
    // Given the shipped markup and renderer with a retained manual record.
    const { context, nodes } = panel();
    context.state.manualDraft = 5;
    const snapshot = { mode, manual };
    context.state.snapshot = snapshot;
    // When the next machine snapshot is rendered.
    context.renderManual(snapshot);
    // Then rest restores start controls; active/ramping sessions retain controls.
    assert.equal(nodes.get("manual-idle").classes.has("hidden"), mode !== "repos");
    assert.equal(nodes.get("manual-controls").classes.has("hidden"), mode === "repos");
    if (mode === "repos") assert.equal(context.state.manualDraft, null);
  });
}

test("a machine without a manual session shows start controls", () => {
  // Given a fresh renderer and no previous manual session.
  const { context, nodes } = panel();
  // When an idle snapshot is rendered.
  context.renderManual({ mode: "repos", manual: null });
  // Then named start controls are visible.
  assert.equal(nodes.get("manual-idle").classes.has("hidden"), false);
});

test("a typed operator survives same-tab reload without borrowing another actor", () => {
  const storage = new Map();
  const first = panel(storage);
  first.nodes.get("manual-operator").value = " Synthetic operator ";
  assert.equal(first.context.rememberOperator(first.nodes.get("manual-operator").value), "Synthetic operator");
  const reloaded = panel(storage);
  assert.equal(reloaded.context.operatorName(), "Synthetic operator");
  reloaded.nodes.get("attest-operator").value = "Different operator";
  assert.equal(reloaded.context.rememberOperator(reloaded.nodes.get("attest-operator").value), "Different operator");
  assert.equal(panel(storage).context.operatorName(), "Different operator");
  assert.equal(panel().context.operatorName(), "");
});

test("unavailable browser storage does not invent an operator", () => {
  const { context, nodes } = panel();
  context.window.sessionStorage = {
    getItem: () => { throw new Error("storage unavailable"); },
    setItem: () => { throw new Error("storage unavailable"); },
  };
  assert.equal(context.operatorName(), "");
  nodes.get("manual-operator").value = "Synthetic operator";
  assert.equal(context.operatorName(), "Synthetic operator");
});

test("editing the shared operator preserves spaces and restores the declared name after reload", () => {
  const storage = new Map([["anheart-operator", "Previous actor"]]);
  const { nodes } = wiredPanel(storage);
  for (const id of ["manual-operator", "operator", "ack-operator", "attest-operator"]) {
    assert.equal(nodes.get(id).value, "Previous actor");
  }
  nodes.get("operator").value = " Declared actor ";
  nodes.get("operator").listeners.get("input")();
  for (const id of ["manual-operator", "operator", "ack-operator", "attest-operator"]) {
    assert.equal(nodes.get(id).value, " Declared actor ");
  }
  const reloaded = wiredPanel(storage);
  assert.equal(reloaded.nodes.get("manual-operator").value, "Declared actor");
});

test("clearing the shared operator removes retained attribution without inventing a replacement", () => {
  const storage = new Map([["anheart-operator", "Previous actor"]]);
  const { context, nodes } = wiredPanel(storage);
  nodes.get("attest-operator").value = "";
  nodes.get("attest-operator").listeners.get("input")();
  assert.equal(context.operatorName(), "");
  assert.equal(wiredPanel(storage).context.operatorName(), "");
});

for (const mode of ["manual", "programmed"]) {
  test(`${mode} START retains its declared actor before presence or reload`, async () => {
    const storage = new Map([["anheart-operator", "Previous actor"]]);
    const { context, nodes } = panel(storage);
    context.state.profiles = [{ profile_id: "standard_30_min" }];
    nodes.get("profile").value = "standard_30_min";
    nodes.get("rider-age").value = "30";
    nodes.get("manual-bench").checked = true;
    const current = mode === "manual" ? "manual-operator" : "operator";
    const other = mode === "manual" ? "operator" : "manual-operator";
    nodes.get(other).value = "Other form actor";
    nodes.get(current).value = "Declared actor";
    const requests = [];
    context.api = (path, options) => {
      requests.push({ path, body: options.body });
      return Promise.resolve({ kind: "start", detail: "accepted" });
    };
    context.loadStatus = () => undefined;
    context.showView = () => undefined;
    if (mode === "manual") context.doManualStart();
    else context.doStart();
    assert.equal(requests[0].body.operator, "Declared actor");
    assert.equal(storage.get("anheart-operator"), "Declared actor");
    context.ping();
    assert.equal(storage.get("anheart-operator"), "Declared actor");
    const reloaded = panel(storage);
    assert.equal(reloaded.context.operatorName(), "Declared actor");
    await Promise.resolve();
  });
}

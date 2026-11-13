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

// ANH-152: the page sends no name; the console names the operator signed in on this device.
for (const mode of ["manual", "programmed"]) {
  test(`${mode} START sends no operator: the console names who is signed in`, async () => {
    const { context, nodes } = panel();
    context.state.profiles = [{ profile_id: "standard_30_min" }];
    nodes.get("profile").value = "standard_30_min";
    nodes.get("rider").value = "k17rider0synthetic";
    nodes.get("manual-bench").checked = true;
    const requests = [];
    context.api = (path, options) => {
      requests.push({ path, body: options.body });
      return Promise.resolve({ kind: "start", detail: "accepted" });
    };
    context.loadStatus = () => undefined;
    context.showView = () => undefined;
    if (mode === "manual") context.doManualStart();
    else context.doStart();
    assert.equal("operator" in requests[0].body, false);
    if (mode === "programmed") assert.equal(requests[0].body.subject_id, "k17rider0synthetic");
    await Promise.resolve();
  });
}

test("the device token is kept by this browser, not by one tab", () => {
  const kept = new Map([["anheart-token", "a-token-of-this-tablet"]]);
  const current = panel();
  current.context.window.localStorage = {
    getItem: (key) => kept.get(key) ?? null,
    setItem: (key, value) => kept.set(key, value),
  };
  current.context.boot = () => undefined;
  current.context.loadCamera = () => undefined;
  current.context.showView = () => undefined;
  current.context.window.setInterval = () => undefined;
  current.context.window.requestAnimationFrame = () => undefined;
  current.context.document.querySelector = () => new Element();
  current.context.start();
  assert.equal(current.context.state.token, "a-token-of-this-tablet");
  current.nodes.get("token").value = "another-token-of-it";
  current.nodes.get("save-token").onclick();
  assert.equal(kept.get("anheart-token"), "another-token-of-it");
});

test("without a list the sign-in is not offered and the page says what still works", () => {
  const { context, nodes } = panel();
  context.state.access = { received: false, operators: [], signed_in: null, device: "local" };
  context.renderAccess();
  assert.equal(nodes.get("access-sign-in").classes.has("hidden"), true);
  assert.match(nodes.get("access-who").textContent, /aucune liste d'operateurs recue/);
  context.state.access = null;
  context.renderAccess();
  assert.equal(nodes.get("access-who").textContent, "connexion : -");
});

test("the sign-in lists the operators by name, a locked one said as such", () => {
  const { context, nodes } = panel();
  context.state.access = {
    received: true,
    device: "local",
    operators: [
      { user_id: "u1", name: "Mme Durand", locked_for_s: null },
      { user_id: "u2", name: "M. Martin", locked_for_s: 600 },
    ],
    signed_in: null,
  };
  context.renderAccess();
  assert.equal(nodes.get("access-sign-in").classes.has("hidden"), false);
  assert.equal(nodes.get("access-signed").classes.has("hidden"), true);
  const options = nodes.get("access-operator").children;
  assert.deepEqual(options.map((o) => o.value), ["u1", "u2"]);
  assert.deepEqual(options.map((o) => o.textContent), ["Mme Durand", "M. Martin (verrouille 10 min)"]);
  assert.match(nodes.get("access-who").textContent, /aucun operateur connecte/);
});

test("signed in, the page names the operator and the time left without an action", () => {
  const { context, nodes } = panel();
  const operators = [{ user_id: "u1", name: "Mme Durand", locked_for_s: null }];
  context.state.access = {
    received: true,
    device: "local",
    operators,
    signed_in: { user_id: "u1", name: "Mme Durand", idle_left_s: 1500, lifetime_left_s: 28000 },
  };
  context.renderAccess();
  assert.equal(nodes.get("access-signed").classes.has("hidden"), false);
  assert.equal(nodes.get("access-who").textContent, "connecte : Mme Durand (deconnexion sans action dans 25 min)");
  assert.equal(nodes.get("access-who").className, "note");
  context.state.access.signed_in.idle_left_s = 120;
  context.renderAccess();
  assert.equal(nodes.get("access-who").className, "note note-bad");
});

test("the code leaves its field at once and is sent once, with the operator chosen", async () => {
  const { context, nodes } = panel();
  const sent = [];
  context.api = (path, options) => {
    sent.push([path, options]);
    return Promise.reject(Object.assign(new Error("code faux (4 essai(s) avant le verrouillage)"), { status: 401 }));
  };
  context.loadAccess = () => undefined;
  nodes.get("access-operator").value = "u1";
  nodes.get("access-code").value = "482915";
  context.doSignIn();
  assert.equal(nodes.get("access-code").value, "");
  assert.deepEqual(JSON.parse(JSON.stringify(sent[0])), [
    "/api/access/sign-in",
    { method: "POST", body: { user_id: "u1", code: "482915" } },
  ]);
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(nodes.get("access-note").textContent, /code faux/);
});

test("signing in, out and staying in each keep the console's answer", async () => {
  const { context, nodes } = panel();
  const answer = { received: true, device: "local", operators: [], signed_in: null };
  const paths = [];
  context.api = (path) => {
    paths.push(path);
    return Promise.resolve(answer);
  };
  nodes.get("access-operator").value = "u1";
  context.doSignIn();
  context.doSignOut();
  context.doStaySignedIn();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(paths, ["/api/access/sign-in", "/api/access/sign-out", "/api/access/refresh"]);
  assert.equal(context.state.access, answer);
  let reloaded = 0;
  context.loadAccess = () => {
    reloaded += 1;
  };
  context.api = () => Promise.reject(new Error("refused"));
  context.doSignOut();
  context.doStaySignedIn();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(nodes.get("access-note").textContent, "refused");
  assert.equal(reloaded, 1);
});

test("the attendant's ping is an operator's, and nobody's without one signed in", () => {
  const { context } = panel();
  const sent = [];
  context.api = (path, options) => {
    sent.push([path, options]);
    return Promise.resolve({ at: 1 });
  };
  context.state.access = { received: true, operators: [], signed_in: null };
  context.ping();
  assert.equal(sent.length, 0);
  context.state.access.signed_in = { user_id: "u1", name: "A", idle_left_s: 100, lifetime_left_s: 100 };
  context.ping();
  assert.deepEqual(JSON.parse(JSON.stringify(sent)), [["/api/presence", { method: "POST" }]]);
});

test("an account and a tablet are said by name", () => {
  const { context } = panel();
  context.state.access = { received: true, operators: [{ user_id: "u1", name: "Mme Durand" }], signed_in: null };
  assert.equal(context.actorName("u1"), "Mme Durand");
  assert.equal(context.actorName("device:tablette-2"), "tablette tablette-2");
  assert.equal(context.actorName("u9"), "u9");
  assert.equal(context.actorName(""), "");
  context.state.access = null;
  assert.equal(context.actorName("u1"), "u1");
});

test("an action refused for want of a sign-in is said where the sign-in is", async () => {
  const { context, nodes } = panel();
  let reloaded = 0;
  context.loadAccess = () => {
    reloaded += 1;
  };
  context.fetch = () =>
    Promise.resolve({
      ok: false,
      status: 401,
      statusText: "Unauthorized",
      json: () => Promise.resolve({ detail: "connexion d'un operateur requise", code: "operator_required" }),
    });
  await assert.rejects(context.api("/api/manual/start", { method: "POST", body: {} }), /connexion/);
  assert.equal(reloaded, 1);
  assert.match(nodes.get("access-note").textContent, /connexion d'un operateur requise/);
});

test("the sign-in is read again, and kept when it is read", async () => {
  const { context, nodes } = panel();
  const answer = { received: false, operators: [], signed_in: null };
  context.api = () => Promise.resolve(answer);
  await context.loadAccess();
  assert.equal(context.state.access, answer);
  assert.match(nodes.get("access-who").textContent, /aucune liste/);
  context.api = () => Promise.reject(new Error("down"));
  await context.loadAccess();
  assert.equal(context.state.access, answer);
});

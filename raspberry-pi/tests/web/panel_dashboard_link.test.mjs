// The two chips the console page shows on every page about the console itself: « Serveur », the
// state of its link with the dashboard as the console reports it in /api/panel, and « Version »,
// the build it runs. A standing state, read at any moment, not an event that scrolls away.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const assets = new URL("../../src/web/static/", import.meta.url);
const html = readFileSync(new URL("index.html", assets), "utf8");
const source = readFileSync(new URL("app.js", assets), "utf8");

// A node as the renderer uses it: classes, text and children. It has no way to take markup:
// a page that wrote a reason with innerHTML would fail here instead of running it in a browser.
class Element {
  constructor(classes = "", text = "") {
    this.className = classes;
    this.children = [];
    this.style = {};
    this.value = "";
    this.text = text;
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

// The shipped page and script, every element carrying the classes and the text its markup gives it.
function page() {
  const nodes = new Map(
    [...html.matchAll(/<[a-z0-9]+\b[^>]*\bid="([^"]+)"[^>]*>([^<]*)/g)].map((tag) => [
      tag[1],
      new Element(/\bclass="([^"]*)"/.exec(tag[0])?.[1], tag[2].trim()),
    ]),
  );
  const clock = { now: 1000 };
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
    performance: { now: () => clock.now },
  });
  vm.runInContext(source, context, { filename: fileURLToPath(new URL("app.js", assets)) });
  context.api = () => new Promise(() => undefined);
  context.state.connected = true;
  context.state.lastFrameAt = clock.now;
  const node = (id) => {
    assert.ok(nodes.has(id), `the page has no #${id}`);
    return nodes.get(id);
  };
  return {
    context,
    node,
    // Time passing: frames keep coming on the socket, /api/panel answers only when the test says so.
    wait: (ms) => {
      clock.now += ms;
      context.state.lastFrameAt = clock.now;
      context.refreshLiveness();
    },
  };
}

const link = (state, label, detail = "", age = 2) => ({ state, label, detail, last_answer_age_s: age });

function panelRow(dashboard, version = "pi-0.0.0-dev") {
  return {
    motion_enabled: true,
    programs_enabled: true,
    motor_backend: "sim",
    drive: { description: null, latency_ms: 0, reads: 1, failures: 0, consecutive_failures: 0, last_error: null },
    ecg: {
      source: "sim",
      address: null,
      connected: true,
      acquiring: true,
      connect_attempts: 1,
      last_error: null,
      link: null,
      batches: 1,
      samples: 200,
      missing_channel: 0,
      last_batch_age_s: 0.1,
      dsp_seq: 1,
      dsp_quality: "good",
    },
    heart_rate_trend_bpm_per_min: 0,
    manual_rise_hold: null,
    radius_m: 1.5,
    gear_ratio: 49.79,
    motor_max_rpm: 300,
    software_version: version,
    dashboard,
  };
}

const INCOMPATIBLE = "serveur incompatible (contrat 1.1 vs 2)";

test("the two chips are in the sidebar, outside every page, and the link is in the narrow-screen bar too", () => {
  const sidebar = /<aside id="sidebar"[\s\S]*?<\/aside>/.exec(html)?.[0] ?? "";
  assert.match(sidebar, /<dt>Serveur<\/dt><dd><span id="dashboard-link" class="pill">/);
  assert.match(sidebar, /<dt>Version<\/dt><dd><span id="software-version" class="pill">/);
  assert.match(sidebar, /<div id="dashboard-link-detail" class="bind">/);
  const bar = /<div class="mobilebar">[\s\S]*?<\/div>/.exec(html)?.[0] ?? "";
  assert.match(bar, /<span id="mobile-dashboard" class="pill">/);
  // Not inside a page that navigation hides: they are read whichever page is shown.
  const pages = [...html.matchAll(/<section id="view-[\s\S]*?<\/section>/g)].map((view) => view[0]).join("");
  assert.ok(pages.length > 0, "the pages of the console were not found");
  for (const id of ["dashboard-link", "software-version", "dashboard-link-detail", "mobile-dashboard"]) {
    assert.equal(pages.includes(`id="${id}"`), false, id);
  }
});

// Every state the console reports (src/link_state.py), its word, and the colour the page gives it.
const STATES = [
  ["reachable", "joignable", "pill-good"],
  ["unreachable", "injoignable", "pill-bad"],
  ["incompatible", "incompatible", "pill-bad"],
  ["key_refused", "cle refusee", "pill-bad"],
  ["server_error", "en erreur", "pill-bad"],
  ["waiting", "en attente", "pill-warn"],
  ["not_configured", "non configure", null],
];

for (const [state, label, colour] of STATES) {
  test(`a link the console reports as ${state} reads « ${label} » on both chips`, () => {
    const { context, node } = page();
    context.renderPanel(panelRow(link(state, label)));
    const side = node("dashboard-link");
    const narrow = node("mobile-dashboard");
    assert.equal(side.textContent, label);
    assert.equal(narrow.textContent, `serveur ${label}`);
    for (const chip of [side, narrow]) {
      const colours = [...chip.classes].filter((name) => name.startsWith("pill-"));
      assert.deepEqual(colours, colour === null ? [] : [colour]);
    }
  });
}

test("only a reachable link is green", () => {
  const green = STATES.filter((entry) => entry[2] === "pill-good").map((entry) => entry[0]);
  assert.deepEqual(green, ["reachable"]);
});

test("a state this page has no colour for is shown by its word, in no colour", () => {
  const { context, node } = page();
  context.renderPanel(panelRow(link("a_state_added_later", "un etat nouveau")));
  assert.equal(node("dashboard-link").textContent, "un etat nouveau");
  assert.deepEqual([...node("dashboard-link").classes], ["pill"]);
});

test("the chip follows the console: incompatible while it lasts, joignable when it is over", () => {
  const { context, node } = page();
  context.renderPanel(panelRow(link("reachable", "joignable")));
  assert.equal(node("dashboard-link").textContent, "joignable");
  context.renderPanel(panelRow(link("incompatible", "incompatible", INCOMPATIBLE)));
  assert.equal(node("dashboard-link").textContent, "incompatible");
  assert.ok(node("dashboard-link").classes.has("pill-bad"));
  assert.equal(node("dashboard-link-detail").textContent, `${INCOMPATIBLE} · derniere reponse il y a 2 s`);
  context.renderPanel(panelRow(link("reachable", "joignable", "", 0)));
  assert.equal(node("dashboard-link").textContent, "joignable");
  assert.ok(node("dashboard-link").classes.has("pill-good"));
  assert.equal(node("dashboard-link-detail").textContent, "derniere reponse il y a 0 s");
});

test("the line under the chips says why, and how old the last answer is", () => {
  const { context, node } = page();
  const line = (dashboard) => {
    context.renderPanel(panelRow(dashboard));
    return node("dashboard-link-detail").textContent;
  };
  assert.equal(line(link("reachable", "joignable", "", 7.4)), "derniere reponse il y a 7 s");
  assert.equal(line(link("reachable", "joignable", "", 99.4)), "derniere reponse il y a 99 s");
  assert.equal(
    line(link("unreachable", "injoignable", "GET /api/machine/training/poll: ConnectTimeout('')", 190)),
    "GET /api/machine/training/poll: ConnectTimeout('') · derniere reponse il y a 3 min",
  );
  // A dashboard that never answered has no last answer: no age is invented.
  assert.equal(line(link("waiting", "en attente", "", null)), "");
  assert.equal(
    line(link("not_configured", "non configure", "aucune cle de machine (MACHINE_API_KEY) : rien n'est echange", null)),
    "aucune cle de machine (MACHINE_API_KEY) : rien n'est echange",
  );
});

test("a reason that carries markup is written as text", () => {
  const { context, node } = page();
  const hostile = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
  // The node used here throws on innerHTML: rendering at all is already the proof.
  context.renderPanel(panelRow(link("server_error", "en erreur", hostile, null)));
  assert.equal(node("dashboard-link-detail").textContent, hostile);
  assert.deepEqual(node("dashboard-link-detail").children, []);
  context.renderPanel(panelRow(link("incompatible", hostile, "", null), hostile));
  assert.equal(node("dashboard-link").textContent, hostile);
  assert.equal(node("software-version").textContent, hostile);
});

test("the version chip shows the build as the console names it", () => {
  const { context, node } = page();
  context.renderPanel(panelRow(link("reachable", "joignable"), "pi-1.4.2"));
  assert.equal(node("software-version").textContent, "pi-1.4.2");
  context.renderPanel(panelRow(link("reachable", "joignable"), "pi-unknown"));
  assert.equal(node("software-version").textContent, "pi-unknown");
});

test("when the console stops answering, the link chips are struck instead of going on saying joignable", () => {
  const { context, node, wait } = page();
  context.renderPanel(panelRow(link("reachable", "joignable")));
  wait(1000);
  assert.equal(node("dashboard-link").classes.has("stale"), false);
  // /api/panel has not answered for longer than its answers stay current (3.5 s).
  wait(3000);
  assert.equal(node("dashboard-link").textContent, "joignable");
  assert.ok(node("dashboard-link").classes.has("stale"));
  assert.ok(node("mobile-dashboard").classes.has("stale"));
  // The build does not change while the console is silent: its chip is not struck.
  assert.equal(node("software-version").classes.has("stale"), false);
  // The next answer is believed again.
  context.renderPanel(panelRow(link("unreachable", "injoignable")));
  wait(200);
  assert.equal(node("dashboard-link").textContent, "injoignable");
  assert.equal(node("dashboard-link").classes.has("stale"), false);
  assert.equal(node("mobile-dashboard").classes.has("stale"), false);
});

test("before the console has answered, the chips claim nothing", () => {
  const { context, node, wait } = page();
  wait(200);
  assert.equal(node("dashboard-link").textContent, "-");
  assert.equal(node("mobile-dashboard").textContent, "serveur -");
  assert.equal(node("software-version").textContent, "-");
  assert.deepEqual([...node("dashboard-link").classes], ["pill"]);
  // A build with no console wired answers null: still nothing is claimed about a dashboard.
  context.renderPanel(null);
  assert.equal(node("dashboard-link").textContent, "-");
  assert.deepEqual(
    [...node("dashboard-link").classes].filter((name) => name.startsWith("pill-")),
    [],
  );
});

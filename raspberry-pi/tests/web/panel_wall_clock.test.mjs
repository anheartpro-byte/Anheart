// The line under the chips that says what the machine's wall clock is worth, as the console
// reports it in /api/panel (`wall_clock`): kept by a time service or not. A standing state, read
// on every page; the page writes the console's words and decides nothing.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const assets = new URL("../../src/web/static/", import.meta.url);
const html = readFileSync(new URL("index.html", assets), "utf8");
const css = readFileSync(new URL("app.css", assets), "utf8");
const source = readFileSync(new URL("app.js", assets), "utf8");

// A node as the renderer uses it: classes, text and children. It has no way to take markup.
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
    wait: (ms) => {
      clock.now += ms;
      context.state.lastFrameAt = clock.now;
      context.refreshLiveness();
    },
  };
}

function panelRow(wallClock) {
  const row = {
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
    software_version: "pi-0.0.0-dev",
    dashboard: { state: "not_configured", label: "non configure", detail: "", last_answer_age_s: null },
    service: {
      out_of_service: false,
      unclean_starts: 0,
      threshold: 3,
      window_s: 600,
      unclean_stop: false,
      open_record: null,
      persisted: true,
    },
    watchdog: { enabled: false, sent: 0, failed: 0, last_error: null },
  };
  if (wallClock !== undefined) {
    row.wall_clock = wallClock;
  }
  return row;
}

// What the console sends for each standing (src/wall_clock.py, src/web/schemas.py).
const SYNCHRONISED = {
  state: "synchronised",
  trusted: true,
  label: "synchronisee",
  detail: "erreur estimee 0.4 ms, erreur au plus 45.0 ms",
  estimated_error_s: 0.0004,
  max_error_s: 0.045,
};
const CUT = {
  state: "unsynchronised",
  trusted: false,
  label: "NON FIABLE",
  detail:
    "aucune source de temps jointe : l'heure peut etre fausse de la duree pendant laquelle le Pi etait eteint",
  estimated_error_s: null,
  max_error_s: null,
};
const UNREADABLE = {
  state: "unreadable",
  trusted: false,
  label: "NON VERIFIEE",
  detail: "etat de l'horloge illisible (adjtimex refuse par le systeme)",
  estimated_error_s: null,
  max_error_s: null,
};

test("the clock line is in the sidebar, under the chips, outside every page", () => {
  const sidebar = /<aside id="sidebar"[\s\S]*?<\/aside>/.exec(html)?.[0] ?? "";
  assert.match(sidebar, /<div id="dashboard-link-detail" class="bind"><\/div>\s*<div id="wall-clock" class="bind"><\/div>/);
  const pages = [...html.matchAll(/<section id="view-[\s\S]*?<\/section>/g)].map((view) => view[0]).join("");
  assert.ok(pages.length > 0, "the pages of the console were not found");
  assert.equal(pages.includes('id="wall-clock"'), false);
  // The warning has a colour of its own to be seen with.
  assert.match(css, /\.bind-warn \{[^}]*color: var\(--warn\)/);
});

test("a clock a time service keeps reads synchronisee, with the error it answers for, in no warning colour", () => {
  const { context, node } = page();
  context.renderPanel(panelRow(SYNCHRONISED));
  const line = node("wall-clock");
  assert.equal(line.textContent, "horloge synchronisee · erreur estimee 0.4 ms, erreur au plus 45.0 ms");
  assert.deepEqual([...line.classes], ["bind"]);
});

for (const [name, clock] of [
  ["nobody answers for", CUT],
  ["cannot be read", UNREADABLE],
]) {
  test(`a clock that ${name} is said in the console's words, in the warning colour`, () => {
    const { context, node } = page();
    context.renderPanel(panelRow(clock));
    const line = node("wall-clock");
    assert.equal(line.textContent, `horloge ${clock.label} · ${clock.detail}`);
    assert.ok(line.classes.has("bind-warn"));
    // Back in step: the warning goes with the next answer.
    context.renderPanel(panelRow(SYNCHRONISED));
    assert.equal(line.classes.has("bind-warn"), false);
    assert.equal(line.textContent.startsWith("horloge synchronisee"), true);
  });
}

test("the page colours on the console's own word for trusted, not on a state it would have to know", () => {
  const { context, node } = page();
  context.renderPanel(panelRow({ ...SYNCHRONISED, state: "a-state-of-tomorrow", trusted: false, label: "DOUTEUSE", detail: "" }));
  assert.equal(node("wall-clock").textContent, "horloge DOUTEUSE");
  assert.ok(node("wall-clock").classes.has("bind-warn"));
});

test("the console's words are written as text, never as markup", () => {
  const { context, node } = page();
  const hostile = '<img src=x onerror="alert(1)">';
  context.renderPanel(panelRow({ ...UNREADABLE, detail: hostile }));
  assert.equal(node("wall-clock").textContent, `horloge NON VERIFIEE · ${hostile}`);
  assert.deepEqual(node("wall-clock").children, []);
});

test("a console that does not judge its clock leaves the line empty", () => {
  const { context, node } = page();
  context.renderPanel(panelRow(null));
  assert.equal(node("wall-clock").textContent, "");
  context.renderPanel(panelRow());
  assert.equal(node("wall-clock").textContent, "");
  assert.deepEqual([...node("wall-clock").classes], ["bind"]);
});

test("when the console stops answering, the line is struck instead of going on saying synchronisee", () => {
  const { context, node, wait } = page();
  context.renderPanel(panelRow(SYNCHRONISED));
  wait(1000);
  assert.equal(node("wall-clock").classes.has("stale"), false);
  // /api/panel has not answered for longer than its answers stay current (3.5 s).
  wait(3000);
  assert.ok(node("wall-clock").classes.has("stale"));
  context.renderPanel(panelRow(CUT));
  wait(200);
  assert.equal(node("wall-clock").classes.has("stale"), false);
  assert.ok(node("wall-clock").classes.has("bind-warn"));
});

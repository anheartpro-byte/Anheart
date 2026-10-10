// What the console page shows of the console's own restarts (/api/panel, `service` and `watchdog`):
// a chip « Service » on every page, a red banner on every page while the console is out of service,
// and the form that puts it back in service, by name, shown only then. A standing state, read at
// any moment: a page opened an hour after the restart loop reads it at once.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const assets = new URL("../../src/web/static/", import.meta.url);
const html = readFileSync(new URL("index.html", assets), "utf8");
const source = readFileSync(new URL("app.js", assets), "utf8");

// A node as the renderer uses it: classes, text, a value and a click. It has no way to take markup.
class Element {
  constructor(classes = "", text = "") {
    this.className = classes;
    this.children = [];
    this.style = {};
    this.value = "";
    this.text = text;
    this.offsetHeight = 0;
    this.onclick = null;
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

// The shipped page and script. `answer` is what the console answers to a request, or an Error.
function page(answer = () => new Promise(() => undefined)) {
  const nodes = new Map(
    [...html.matchAll(/<[a-z0-9]+\b[^>]*\bid="([^"]+)"[^>]*>([^<]*)/g)].map((tag) => [
      tag[1],
      new Element(/\bclass="([^"]*)"/.exec(tag[0])?.[1], tag[2].trim()),
    ]),
  );
  const clock = { now: 1000 };
  const asked = [];
  const alerts = [];
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
      alert: (message) => alerts.push(message),
    },
    performance: { now: () => clock.now },
  });
  vm.runInContext(source, context, { filename: fileURLToPath(new URL("app.js", assets)) });
  context.api = (path, options) => {
    asked.push([path, options]);
    return answer(path, options);
  };
  context.state.connected = true;
  context.state.lastFrameAt = clock.now;
  const node = (id) => {
    assert.ok(nodes.has(id), `the page has no #${id}`);
    return nodes.get(id);
  };
  return {
    context,
    node,
    asked,
    alerts,
    wait: (ms) => {
      clock.now += ms;
      context.state.lastFrameAt = clock.now;
      context.refreshLiveness();
    },
  };
}

const IN_SERVICE = {
  out_of_service: false,
  unclean_starts: 0,
  threshold: 3,
  window_s: 600,
  unclean_stop: false,
  open_record: null,
  persisted: true,
};
const OUT_OF_SERVICE = { ...IN_SERVICE, out_of_service: true, unclean_starts: 3, unclean_stop: true };
const UNWATCHED = { enabled: false, sent: 0, failed: 0, last_error: null };
const WATCHED = { enabled: true, sent: 1200, failed: 0, last_error: null };

function panelRow(service = IN_SERVICE, watchdog = UNWATCHED) {
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
    software_version: "pi-0.0.0-dev",
    dashboard: { state: "not_configured", label: "non configure", detail: "", last_answer_age_s: null },
    service,
    watchdog,
  };
}

const RECORD = "2026-10-10T101112Z_0123456789abcdef";

test("the chip and its line are in the sidebar, the banner above every page, the form on the safety page", () => {
  const sidebar = /<aside id="sidebar"[\s\S]*?<\/aside>/.exec(html)?.[0] ?? "";
  assert.match(sidebar, /<dt>Service<\/dt><dd><span id="service-state" class="pill">/);
  assert.match(sidebar, /<div id="service-detail" class="bind">/);
  const banners = /<div id="banners"[\s\S]*?\n<\/div>/.exec(html)?.[0] ?? "";
  assert.match(banners, /<div id="service-banner" class="banner hidden" role="alert"/);
  const pages = [...html.matchAll(/<section id="view-[\s\S]*?<\/section>/g)].map((view) => view[0]);
  assert.ok(pages.length > 0, "the pages of the console were not found");
  for (const id of ["service-state", "service-detail", "service-banner"]) {
    assert.equal(pages.join("").includes(`id="${id}"`), false, id);
  }
  const safety = pages.find((view) => view.startsWith('<section id="view-safety"')) ?? "";
  assert.match(safety, /<div id="service-restore" class="hidden">/);
  assert.match(safety, /<input id="service-operator" type="text"/);
  assert.match(safety, /<button id="service-restore-button" class="btn" type="button">Remettre en service<\/button>/);
});

test("a console in service reads « en service », in green, with no banner and no form", () => {
  const { context, node } = page();
  context.renderPanel(panelRow());
  assert.equal(node("service-state").textContent, "en service");
  assert.ok(node("service-state").classes.has("pill-good"));
  assert.ok(node("service-banner").classes.has("hidden"));
  assert.ok(node("service-restore").classes.has("hidden"));
  assert.equal(node("service-detail").textContent, "");
});

test("a console out of service says so on the chip, in the banner, and offers the form", () => {
  const { context, node } = page();
  context.renderPanel(panelRow(OUT_OF_SERVICE));
  assert.equal(node("service-state").textContent, "HORS SERVICE");
  assert.ok(node("service-state").classes.has("pill-bad"));
  assert.equal(node("service-banner").classes.has("hidden"), false);
  assert.equal(
    node("service-banner-detail").textContent,
    "3 demarrages apres un arret non propre en moins de 10 min : aucun depart n'est accepte. " +
      "Faire verifier la machine, puis la remettre en service (page Securite), par un operateur nomme",
  );
  assert.equal(node("service-restore").classes.has("hidden"), false);
});

test("out of service follows the console: the banner goes when the console says in service again", () => {
  const { context, node } = page();
  context.renderPanel(panelRow(OUT_OF_SERVICE));
  assert.equal(node("service-banner").classes.has("hidden"), false);
  context.renderPanel(panelRow());
  assert.ok(node("service-banner").classes.has("hidden"));
  assert.ok(node("service-restore").classes.has("hidden"));
  assert.equal(node("service-state").textContent, "en service");
});

test("the line under the chips says what the last start found, and nothing when there is nothing", () => {
  const { context, node } = page();
  const line = (service, watchdog = UNWATCHED) => {
    context.renderPanel(panelRow(service, watchdog));
    return node("service-detail").textContent;
  };
  assert.equal(line(IN_SERVICE), "");
  assert.equal(line(IN_SERVICE, WATCHED), "");
  assert.equal(
    line({ ...IN_SERVICE, unclean_stop: true, unclean_starts: 1 }),
    "demarre apres un arret non propre · 1 sur 3 en 10 min avant la mise hors service",
  );
  assert.equal(
    line({ ...IN_SERVICE, unclean_stop: true, open_record: RECORD }),
    `demarre apres un arret non propre (enregistrement ${RECORD} non clos)`,
  );
  assert.equal(
    line({ ...IN_SERVICE, persisted: false }),
    "etat des redemarrages non enregistre sur le disque",
  );
  assert.equal(
    line(IN_SERVICE, { ...WATCHED, failed: 4, last_error: "BlockingIOError" }),
    "chien de garde : 4 notification(s) non envoyee(s)",
  );
  // A console nobody watches has nothing to say about a watchdog, whatever its counters.
  assert.equal(line(IN_SERVICE, { ...UNWATCHED, failed: 4 }), "");
});

test("a record name that carries markup is written as text", () => {
  const { context, node } = page();
  const hostile = '<img src=x onerror="alert(1)">';
  // The node used here throws on innerHTML: rendering at all is already the proof.
  context.renderPanel(panelRow({ ...IN_SERVICE, unclean_stop: true, open_record: hostile }));
  assert.equal(
    node("service-detail").textContent,
    `demarre apres un arret non propre (enregistrement ${hostile} non clos)`,
  );
  assert.deepEqual(node("service-detail").children, []);
});

test("when the console stops answering, the service chip is struck instead of going on saying en service", () => {
  const { context, node, wait } = page();
  context.renderPanel(panelRow());
  wait(1000);
  assert.equal(node("service-state").classes.has("stale"), false);
  wait(3000);
  assert.equal(node("service-state").textContent, "en service");
  assert.ok(node("service-state").classes.has("stale"));
  context.renderPanel(panelRow(OUT_OF_SERVICE));
  wait(200);
  assert.equal(node("service-state").textContent, "HORS SERVICE");
  assert.equal(node("service-state").classes.has("stale"), false);
});

test("before the console has answered, the chip claims nothing and no banner is shown", () => {
  const { context, node, wait } = page();
  wait(200);
  assert.equal(node("service-state").textContent, "-");
  assert.deepEqual([...node("service-state").classes], ["pill"]);
  assert.ok(node("service-banner").classes.has("hidden"));
  context.renderPanel(null);
  assert.equal(node("service-state").textContent, "-");
  assert.ok(node("service-restore").classes.has("hidden"));
});

test("putting the console back in service sends the name typed, and asks the console again", async () => {
  const { context, node, asked } = page((path) =>
    Promise.resolve(path === "/api/service/restore" ? IN_SERVICE : panelRow()),
  );
  context.renderPanel(panelRow(OUT_OF_SERVICE));
  node("service-operator").value = "Synthetic operator";
  context.doRestoreService();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(asked[0][0], "/api/service/restore");
  assert.equal(asked[0][1].method, "POST");
  assert.deepEqual({ ...asked[0][1].body }, { operator: "Synthetic operator" });
  assert.equal(node("service-note").textContent, "console remise en service");
  // The chip is not flipped by the page: the console is asked, and its answer is what is shown.
  assert.equal(asked[1][0], "/api/panel");
  assert.equal(node("service-state").textContent, "en service");
  assert.ok(node("service-banner").classes.has("hidden"));
});

test("a state the disk did not take is said when the console is put back in service", async () => {
  const { context, node } = page(() => Promise.resolve({ ...IN_SERVICE, persisted: false }));
  context.renderPanel(panelRow(OUT_OF_SERVICE));
  context.doRestoreService();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(
    node("service-note").textContent,
    "console remise en service jusqu'a son prochain redemarrage : l'etat n'a pas pu etre enregistre",
  );
});

test("a refusal of the console is shown, and the banner stays", async () => {
  const { context, node, alerts } = page(() =>
    Promise.reject(new Error("an operator name is required: an unattributable session record is not one")),
  );
  context.renderPanel(panelRow(OUT_OF_SERVICE));
  context.doRestoreService();
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(node("service-note").textContent, /an operator name is required/);
  assert.ok(node("service-note").classes.has("note-bad"));
  assert.equal(alerts.length, 1);
  assert.match(alerts[0], /^remise en service refusee : /);
  assert.equal(node("service-banner").classes.has("hidden"), false);
  assert.equal(node("service-state").textContent, "HORS SERVICE");
});

test("the name typed for the console is the one name of the page", () => {
  assert.match(source, /var OPERATOR_INPUT_IDS = \[[^\]]*"service-operator"[^\]]*\];/);
  assert.match(source, /el\("service-restore-button"\)\.onclick = doRestoreService;/);
});

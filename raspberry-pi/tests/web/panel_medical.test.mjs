// What the console page shows of its medical parameters (/api/panel, `medical_parameters`): their
// version in the footer of every page, and, when their signature did not verify at startup, a red
// banner above every page that says the console is in bench mode only (ANH-139, EX-3 and EX-4).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const assets = new URL("../../src/web/static/", import.meta.url);
const html = readFileSync(new URL("index.html", assets), "utf8");
const source = readFileSync(new URL("app.js", assets), "utf8");

// A node as the renderer uses it: classes and text. It has no way to take markup.
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

// The shipped page and script, with nothing answering.
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
      alert: () => undefined,
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

const SIGNED = {
  version: "0.1.0",
  signed: true,
  issued_at: "2026-10-14",
  signed_by: "cle-de-test",
  warning: null,
};
const WARNING =
  "parametres medicaux non signes (la signature ne correspond pas au fichier) : " +
  "console en mode banc seulement, aucune seance programmee ni personne a bord";
const UNSIGNED = { ...SIGNED, signed: false, warning: WARNING };

function panelRow(medical) {
  return {
    motion_enabled: true,
    programs_enabled: medical !== null && medical.signed,
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
    wall_clock: null,
    medical_parameters: medical,
  };
}

test("the version is in the footer, outside every page, and the banner in the stack above them", () => {
  const footer = /<footer[\s\S]*?<\/footer>/.exec(html)?.[0] ?? "";
  assert.match(footer, /<span id="footer-medical" class="footer-medical">/);
  const banners = /<div id="banners"[\s\S]*?\n<\/div>/.exec(html)?.[0] ?? "";
  assert.match(banners, /<div id="medical-banner" class="banner hidden" role="alert"/);
  assert.match(banners, /<strong>PARAMETRES MEDICAUX NON SIGNES<\/strong>/);
  const pages = [...html.matchAll(/<section id="view-[\s\S]*?<\/section>/g)].map((view) => view[0]);
  assert.ok(pages.length > 0, "the pages of the console were not found");
  for (const id of ["footer-medical", "medical-banner"]) {
    assert.equal(pages.join("").includes(`id="${id}"`), false, id);
  }
});

test("signed parameters: the footer names their version, dim, and no banner", () => {
  const { context, node } = page();
  context.renderPanel(panelRow(SIGNED));
  assert.equal(node("footer-medical").textContent, "parametres medicaux 0.1.0");
  assert.equal(node("footer-medical").classes.has("bad"), false);
  assert.ok(node("medical-banner").classes.has("hidden"));
});

test("unsigned parameters: the footer says so in red, and the banner says the bench mode", () => {
  const { context, node } = page();
  context.renderPanel(panelRow(UNSIGNED));
  assert.equal(node("footer-medical").textContent, "parametres medicaux 0.1.0 NON SIGNES");
  assert.ok(node("footer-medical").classes.has("bad"));
  assert.equal(node("medical-banner").classes.has("hidden"), false);
  assert.equal(node("medical-banner-detail").textContent, WARNING);
});

test("a console that reports none shows none, and no banner", () => {
  const { context, node } = page();
  context.renderPanel(panelRow(UNSIGNED));
  context.renderPanel(panelRow(null));
  assert.equal(node("footer-medical").textContent, "parametres medicaux -");
  assert.equal(node("footer-medical").classes.has("bad"), false);
  assert.ok(node("medical-banner").classes.has("hidden"));
});

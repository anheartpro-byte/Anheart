// STO_SOURCE=bridged_declared on the console page (ANH-92): the permanent banner, the
// confirmations of the console's mode and none other, and the starts it admits.
//
// The page decides nothing about the mode: it reads the source from /api/status and the
// frames, the confirmations from attestation_fields and estop_released_label.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const assets = new URL("../../src/web/static/", import.meta.url);
const html = readFileSync(new URL("index.html", assets), "utf8");
const source = readFileSync(new URL("app.js", assets), "utf8");

// A node as the renderer uses it: classes, text and children, nothing more.
class Element {
  constructor(classes = "", text = "") {
    this.className = classes;
    this.children = [];
    this.style = {};
    this.value = "";
    this.checked = false;
    this.disabled = false;
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
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  setAttribute() {}
  addEventListener() {}
}

const script = new vm.Script(source, { filename: fileURLToPath(new URL("app.js", assets)) });
const marked = [...html.matchAll(/<[a-z0-9]+\b[^>]*\bid="([^"]+)"[^>]*>([^<]*)/g)].map((tag) => [
  tag[1],
  /\bclass="([^"]*)"/.exec(tag[0])?.[1],
  tag[2].trim(),
]);
const banners = [...html.matchAll(/<div id="([^"]+)" class="banner(?: [^"]*)?"[^>]*>([\s\S]*?)<\/div>/g)].map(
  (banner) => ({ id: banner[1], inner: banner[2] }),
);

const BANNER = "STO PONTE : aucun arret d'urgence materiel";
const NOT_READY = "CHAINE STO NON PRETE";
const BENCH_ONLY =
  "chaine STO pontee (declaree), aucun arret d'urgence materiel : seule une seance manuelle de banc, " +
  "capsule vide ou lestee, aucune personne a bord, peut demarrer";
const BENCH_STATEMENT =
  "le STO est ponte, aucun arret d'urgence materiel ; l'arret d'urgence est la coupure de la prise " +
  "secteur ; capsule vide ou lestee, aucune personne a bord ; personne dans le rayon du bras pendant la rotation";
const ESTOP_STATEMENT =
  "a latching mushroom emergency stop is wired normally-closed into P24 -> STO and the STO jumper has been removed";
const BENCH_LABEL =
  "aucun coup de poing n'est cable : la cause de l'arret d'urgence de l'ecran est ecartee et personne n'est dans le rayon du bras";
const MUSHROOM_LABEL = "le coup de poing a ete deverrouille (tire)";

function panel() {
  const nodes = new Map(marked.map(([id, classes, text]) => [id, new Element(classes, text)]));
  const clock = { now: 1000 };
  const context = vm.createContext({
    document: {
      title: /<title>([^<]*)<\/title>/.exec(html)[1],
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
    performance: { now: () => clock.now },
  });
  script.runInContext(context);
  context.api = () => new Promise(() => undefined);
  context.state.connected = true;
  context.state.lastFrameAt = clock.now;
  context.state.motionEnabled = true;
  context.state.programsEnabled = true;
  const node = (id) => {
    assert.ok(nodes.has(id), `the page has no #${id}`);
    return nodes.get(id);
  };
  const said = (banner) =>
    banner.inner
      .replace(/<[a-z]+ id="([^"]+)"[^>]*>[^<]*<\/[a-z]+>/g, (_whole, id) => nodes.get(id).textContent)
      .replace(/<[^>]+>/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  return {
    context,
    clock,
    node,
    shown: (id) => !node(id).classes.has("hidden"),
    // Whether a banner saying `phrase` is on screen, whichever element carries it.
    announced: (phrase) =>
      banners.some((banner) => !nodes.get(banner.id).classes.has("hidden") && said(banner).includes(phrase)),
    frame: (snapshot) => {
      clock.now += 200;
      context.state.lastFrameAt = clock.now;
      context.renderSnapshot(snapshot);
      context.refreshLiveness();
    },
  };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

const speed = { motor_rpm: 0, output_rpm: 0, hertz: 0, g_load: 0, resultant_g: 1 };

const bridgedRow = {
  state: "bridged",
  source: "bridged_declared",
  age_s: null,
  stale_after_s: 2,
  refusal: BENCH_ONLY,
  unknown_refusal: "etat STO inconnu : retablir une lecture fiable de la chaine de securite",
};

const readRow = (state, source) => ({
  state,
  source,
  age_s: state === "not_configured" ? null : 0.2,
  stale_after_s: 2,
  refusal: state === "ready" ? null : `STO refusal ${state}`,
  unknown_refusal: "STO unknown refusal",
});

const BENCH_MODE = {
  sto: bridgedRow,
  attestation_statement: BENCH_STATEMENT,
  attestation_fields: ["bench_bridged_declared"],
  estop_released_label: BENCH_LABEL,
};

const WIRED_MODE = {
  sto: readRow("ready", "sim_ready"),
  attestation_statement: ESTOP_STATEMENT,
  attestation_fields: ["sto_jumper_removed", "mushroom_wired_nc"],
  estop_released_label: MUSHROOM_LABEL,
};

function snapshot(overrides = {}) {
  return {
    sto: bridgedRow,
    at: 50,
    wall_clock: 0,
    phase: "done",
    elapsed_s: 0,
    remaining_s: 0,
    heart_rate: { bpm: 70, quality: "good", age_s: 0.1, stale: false, seq: 81 },
    target_bpm: null,
    live_bpm: 70,
    setpoint: speed,
    measured: speed,
    setpoint_confirmed: true,
    drive_state: "switch_on_disabled",
    drive_status_age_s: 0.2,
    drive_status_stale: false,
    current_a: 0,
    fault: null,
    safety: null,
    safety_action: "none",
    safety_rank: 0,
    counters: { in_zone_s: 0, above_zone_s: 0, below_zone_s: 0, total_s: 0 },
    mode: "repos",
    manual: null,
    ...overrides,
  };
}

function status(mode, overrides = {}) {
  return {
    at: 50,
    run_state: "idle",
    estop_latched: false,
    supervisor_estop: null,
    attested: true,
    attestation: null,
    standing: null,
    floor: null,
    live: [],
    retained_hr_samples: 0,
    pending: null,
    requests: [],
    session_request: null,
    attendant_last_seen: null,
    clients: 1,
    evictions: 0,
    ecg_fs_hz: 250,
    ecg_seq: 0,
    profile_rev: 0,
    profile_ids: [],
    ports: [],
    bind: { host: "127.0.0.1", port: 8080, loopback: true, token_required: false },
    counters: [0, 0, 0],
    ...mode,
    ...overrides,
  };
}

// A page that has read one /api/status answer, with no live frame to lead over it.
async function answered(answer) {
  const page = panel();
  page.context.state.connected = false;
  page.context.api = () => Promise.resolve(answer);
  await page.context.loadStatus();
  return page;
}

/* ================= EX-B4: the banner, on every page, for as long as the source says so ================= */

test("the banner reads exactly the declared words and sits in the stack every page shows", () => {
  const entry = banners.find((banner) => banner.id === "bridged-banner");
  assert.ok(entry, "the page has no bridged banner");
  assert.equal(entry.inner.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim(), BANNER);
  const stack = html.slice(html.indexOf('<div id="banners"'), html.indexOf('<div class="mobilebar">'));
  assert.ok(stack.includes('id="bridged-banner"'), "outside the banner stack, a view could hide it");
  assert.ok(/id="bridged-banner" class="banner hidden" role="alert"/.test(html));
});

test("under the declared bridge the banner is up, the not-ready one is not, and the STO card names no reading", async () => {
  const page = await answered(status(BENCH_MODE));
  assert.ok(page.announced(BANNER));
  assert.equal(page.shown("bridged-banner"), true);
  assert.equal(page.announced(NOT_READY), false);
  assert.equal(page.shown("sto-banner"), false);
  assert.equal(page.node("sto-pill").textContent, "pontee (declaree) (bridged_declared)");
  assert.equal(page.node("side-sto").textContent, "pontee (declaree)");
  assert.equal(page.node("sto-age").textContent, "Aucune lecture : chaine declaree pontee");
  assert.ok(!/\d+(\.\d+)? s/.test(page.node("sto-age").textContent), "no reading, so no age of one");
  assert.ok(page.node("sto-note").textContent.startsWith(BENCH_ONLY));
  assert.ok(page.node("sto-note").textContent.includes("prise secteur"));
  assert.ok(page.node("sto-note").textContent.includes("roue libre, sans frein"));
});

test("frames alone raise it too, before any status answer", () => {
  const page = panel();
  page.frame(snapshot());
  assert.ok(page.announced(BANNER));
  assert.equal(page.announced(NOT_READY), false);
});

test("a dropped link, a late status or a failed request never takes the banner away", async () => {
  const page = await answered(status(BENCH_MODE));
  page.frame(snapshot());
  // The link drops and the frames stop.
  page.context.state.connected = false;
  page.clock.now += 3000;
  page.context.refreshLiveness();
  assert.ok(page.announced("NO LIVE DATA"));
  assert.ok(page.announced(BANNER));
  // The status is older than two refreshes.
  page.clock.now += 13000;
  page.context.refreshLiveness();
  assert.ok(page.announced(BANNER));
  // A status request fails.
  page.context.api = () => Promise.reject(new Error("no answer"));
  await page.context.loadStatus().catch(() => undefined);
  page.context.refreshLiveness();
  assert.ok(page.announced(BANNER));
  assert.equal(page.announced(NOT_READY), false);
});

for (const [kind, row] of [
  ["sim_ready", readRow("ready", "sim_ready")],
  ["sim_open", readRow("open", "sim_open")],
  ["none", readRow("not_configured", "none")],
  ["drive_input", readRow("unknown", "drive_input")],
]) {
  test(`no banner of the bridge under ${kind}`, async () => {
    const page = await answered(status({ ...WIRED_MODE, sto: row }));
    assert.equal(page.announced(BANNER), false);
    assert.equal(page.shown("bridged-banner"), false);
    // The not-ready banner of ANH-92, unchanged.
    assert.equal(page.shown("sto-banner"), row.state !== "ready");
  });
}

test("a bridged state from a source that declares nothing is not the declaration", async () => {
  const page = await answered(status({ ...WIRED_MODE, sto: { ...bridgedRow, source: "drive_input" } }));
  assert.equal(page.announced(BANNER), false);
  assert.equal(page.shown("sto-banner"), true);
  assert.equal(page.node("manual-start").disabled, true);
});

test("only an answer naming another source takes the banner down", async () => {
  const page = await answered(status(BENCH_MODE));
  assert.ok(page.announced(BANNER));
  page.context.api = () => Promise.resolve(status(WIRED_MODE, { at: 60 }));
  await page.context.loadStatus();
  assert.equal(page.announced(BANNER), false);
});

/* ================= EX-B3: the bench declaration alone, under the bridge ================= */

test("before the console has said its mode, no confirmation is offered", () => {
  const page = panel();
  for (const id of ["attest-jumper-row", "attest-mushroom-row", "attest-bench-row", "ack-released-row"]) {
    assert.equal(page.shown(id), false, id);
  }
  assert.equal(page.node("ack-released-label").textContent, "");
  assert.ok(!html.includes(MUSHROOM_LABEL), "the mushroom's words are the console's, not the markup's");
});

test("under the bridge the card offers the bench declaration, in the console's exact words, and a name", async () => {
  const page = await answered(status(BENCH_MODE, { attested: false }));
  assert.equal(page.shown("attest-bench-row"), true);
  assert.equal(page.shown("attest-jumper-row"), false);
  assert.equal(page.shown("attest-mushroom-row"), false);
  assert.equal(page.node("attest-statement").textContent, BENCH_STATEMENT);
  assert.equal(page.node("attest-title").textContent, "Declaration de banc : STO ponte");
  assert.equal(page.shown("attest-bench-important"), true);
  assert.equal(page.shown("attest-important"), false);
  assert.equal(page.node("attest-state").textContent, "non atteste");
});

test("the bench body carries the name and the bench declaration, never the two boxes", async () => {
  const page = await answered(status(BENCH_MODE, { attested: false }));
  const sent = [];
  page.context.api = (path, options) => {
    sent.push([path, options && options.body]);
    return Promise.resolve(path === "/api/status" ? status(BENCH_MODE) : null);
  };
  page.node("attest-operator").value = "Operateur";
  page.node("attest-bench").checked = true;
  // Ticked once, under another console: still never sent under this one.
  page.node("attest-jumper").checked = true;
  page.node("attest-mushroom").checked = true;
  page.context.doAttest();
  await settle();
  const [path, body] = sent[0];
  assert.equal(path, "/api/safety/attest");
  assert.deepEqual(JSON.parse(JSON.stringify(body)), { operator: "Operateur", bench_bridged_declared: true });
});

test("under every other source only ANH-92's two boxes are offered and sent", async () => {
  const page = await answered(status(WIRED_MODE, { attested: false }));
  assert.equal(page.shown("attest-jumper-row"), true);
  assert.equal(page.shown("attest-mushroom-row"), true);
  assert.equal(page.shown("attest-bench-row"), false);
  assert.equal(page.node("attest-title").textContent, "Cablage de l'arret d'urgence");
  assert.equal(page.shown("attest-important"), true);
  assert.equal(page.shown("attest-bench-important"), false);
  const sent = [];
  page.context.api = (path, options) => {
    sent.push([path, options && options.body]);
    return Promise.resolve(path === "/api/status" ? status(WIRED_MODE) : null);
  };
  page.node("attest-operator").value = "Operateur";
  page.node("attest-jumper").checked = true;
  page.node("attest-mushroom").checked = true;
  page.node("attest-bench").checked = true;
  page.context.doAttest();
  await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(sent[0][1])), {
    operator: "Operateur",
    sto_jumper_removed: true,
    mushroom_wired_nc: true,
  });
});

test("a field the page does not know is not invented: the name alone goes, and the console judges", async () => {
  const page = await answered(status({ ...WIRED_MODE, attestation_fields: ["something_new"] }));
  const sent = [];
  page.context.api = (_path, options) => {
    sent.push(options && options.body);
    return Promise.reject(new Error("refused"));
  };
  page.node("attest-operator").value = "Operateur";
  page.context.doAttest();
  await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(sent[0])), { operator: "Operateur" });
  assert.equal(page.node("attest-note").textContent, "refused");
});

/* ================= Q2: the acknowledgement's words are the mode's ================= */

for (const [name, mode, label] of [
  ["the bridge", BENCH_MODE, BENCH_LABEL],
  ["a wired chain", WIRED_MODE, MUSHROOM_LABEL],
]) {
  test(`under ${name} the release confirmation says ${label.slice(0, 24)}... and sends estop_released`, async () => {
    const page = await answered(status(mode));
    assert.equal(page.shown("ack-released-row"), true);
    assert.equal(page.node("ack-released-label").textContent, label);
    const sent = [];
    page.context.api = (path, options) => {
      sent.push([path, options && options.body]);
      return Promise.reject(new Error("nothing is latched to acknowledge"));
    };
    page.node("ack-operator").value = "Operateur";
    page.node("ack-released").checked = true;
    page.context.doAcknowledge();
    await settle();
    assert.deepEqual(sent[0][0], "/api/safety/acknowledge");
    assert.deepEqual(JSON.parse(JSON.stringify(sent[0][1])), { operator: "Operateur", estop_released: true });
  });
}

test("the mushroom's words are never offered under the bridge, nor the bridge's elsewhere", async () => {
  const bench = await answered(status(BENCH_MODE));
  assert.ok(!bench.node("ack-released-label").textContent.includes("deverrouille"));
  const wired = await answered(status(WIRED_MODE));
  assert.ok(!wired.node("ack-released-label").textContent.includes("aucun coup de poing"));
});

test("new words untick the box: a tick given to other words is no answer to these", async () => {
  const page = await answered(status(WIRED_MODE));
  page.node("ack-released").checked = true;
  page.context.api = () => Promise.resolve(status(BENCH_MODE, { at: 60 }));
  await page.context.loadStatus();
  assert.equal(page.node("ack-released-label").textContent, BENCH_LABEL);
  assert.equal(page.node("ack-released").checked, false);
  // The same words again leave the operator's tick alone.
  page.node("ack-released").checked = true;
  page.context.api = () => Promise.resolve(status(BENCH_MODE, { at: 70 }));
  await page.context.loadStatus();
  assert.equal(page.node("ack-released").checked, true);
});

/* ================= the starts: a bench session, and a reason for the rest ================= */

test("under the bridge a bench start stays possible and a programme shows the named reason", async () => {
  const page = await answered(status(BENCH_MODE));
  assert.equal(page.node("manual-start").disabled, false);
  assert.equal(page.node("start").disabled, true);
  assert.equal(page.node("start-sto-note").textContent, BENCH_ONLY);
  assert.equal(page.node("manual-sto-note").textContent, BENCH_ONLY);
  // An acknowledgement is taken by name there: no reading is waited for.
  assert.equal(page.node("ack-sto-note").textContent, "");
});

test("on a read-only console the bench start stays closed under the bridge too", async () => {
  const page = await answered(status(BENCH_MODE));
  page.context.state.motionEnabled = false;
  page.context.renderSto();
  assert.equal(page.node("manual-start").disabled, true);
});

test("the bench start sends a bench session, and a refusal of the console is said as written", async () => {
  const page = await answered(status(BENCH_MODE));
  const sent = [];
  page.context.api = (path, options) => {
    sent.push([path, options && options.body]);
    return Promise.reject(new Error(BENCH_ONLY));
  };
  page.node("manual-bench").checked = true;
  page.node("manual-operator").value = "Operateur";
  page.context.doManualStart();
  await settle();
  assert.equal(sent[0][0], "/api/manual/start");
  assert.equal(sent[0][1].occupancy, "bench");
  assert.equal(page.node("manual-note").textContent, BENCH_ONLY);
});

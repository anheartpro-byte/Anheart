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
    this.text = text;
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

// The shipped page and script, with every element carrying the classes and the text the markup gives it.
function panel() {
  const nodes = new Map(
    [...html.matchAll(/<[a-z0-9]+\b[^>]*\bid="([^"]+)"[^>]*>([^<]*)/g)].map((tag) => [
      tag[1],
      new Element(/\bclass="([^"]*)"/.exec(tag[0])?.[1], tag[2].trim()),
    ]),
  );
  const clock = { now: 1000 };
  const context = vm.createContext({
    document: {
      title: /<title>([^<]*)<\/title>/.exec(html)[1],
      getElementById: (id) => nodes.get(id) ?? null,
      createElement: () => new Element(),
    },
    window: {
      addEventListener: () => undefined,
      sessionStorage: { getItem: () => null, setItem: () => undefined },
    },
    performance: { now: () => clock.now },
  });
  vm.runInContext(source, context, { filename: fileURLToPath(new URL("app.js", assets)) });
  // A page whose socket is open and delivering frames.
  context.state.connected = true;
  context.state.lastFrameAt = clock.now;
  const node = (id) => {
    assert.ok(nodes.has(id), `the page has no #${id}`);
    return nodes.get(id);
  };
  return {
    context,
    clock,
    node,
    shown: (id) => !node(id).classes.has("hidden"),
    // One frame from the machine, as the socket handler renders it.
    frame: (snapshot) => {
      clock.now += 200;
      context.state.lastFrameAt = clock.now;
      context.renderSnapshot(snapshot);
      context.refreshLiveness();
    },
  };
}

function rows(grid) {
  const pairs = {};
  for (let i = 0; i < grid.children.length; i += 2) {
    pairs[grid.children[i].textContent] = grid.children[i + 1];
  }
  return pairs;
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

const speed = { motor_rpm: 0, output_rpm: 0, hertz: 0, g_load: 0, resultant_g: 1 };

function snapshot(overrides = {}) {
  return {
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

function verdict(action, rule, latched = true) {
  const ranks = { none: 0, freeze: 1, reduce: 2, ramp_down: 3, quick_stop: 4, go_silent: 5 };
  const row = { action, rank: ranks[action], rule, detail: rule, latched, age_s: 1 };
  return { safety: row, safety_action: action, safety_rank: row.rank };
}

function status(overrides = {}) {
  return {
    run_state: "idle",
    estop_latched: false,
    attested: true,
    attestation: null,
    attestation_statement: "",
    standing: null,
    floor: null,
    live: [],
    retained_hr_samples: 0,
    pending: null,
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
    ...overrides,
  };
}

function panelRow(overrides = {}) {
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
    radius_m: 1.5,
    gear_ratio: 49.79,
    motor_max_rpm: 300,
    ...overrides,
  };
}

const receipt = { operator: "", reason: "", at: 60, wall_clock: 0, action: "quick_stop", rule: "operator_estop", detail: "" };

/* ----------------------------------------- the emergency-stop banner (1) */

test("the emergency-stop banner outlives the liveness refresh for as long as the verdict is latched", async () => {
  // Given a live page whose operator presses E-STOP, and a machine that latches it.
  const { context, frame, shown } = panel();
  frame(snapshot({ at: 59.9 }));
  assert.equal(shown("estop-banner"), false);
  context.api = () => Promise.resolve(receipt);
  context.loadStatus = () => Promise.resolve();
  context.doEstop();
  await settle();
  assert.equal(shown("estop-banner"), true);
  // When frames keep arriving and the liveness check keeps running, well past half a second.
  for (let tick = 1; tick <= 50; tick += 1) {
    frame(snapshot({ at: 60 + tick * 0.2, mode: "arret", ...verdict("quick_stop", "operator_estop") }));
    // Then the banner stays, and the page does not claim it has lost the machine.
    assert.equal(shown("estop-banner"), true, `hidden after ${tick} frames`);
    assert.equal(shown("banner"), false);
  }
  // And it goes only once the machine reports the verdict acknowledged.
  frame(snapshot({ at: 71 }));
  assert.equal(shown("estop-banner"), false);
});

test("a frame taken before the latch does not drop the banner the receipt raised", async () => {
  // Given an accepted E-STOP whose receipt overtook a frame already on its way.
  const { context, frame, shown } = panel();
  context.api = () => Promise.resolve(receipt);
  context.loadStatus = () => Promise.resolve();
  context.doEstop();
  await settle();
  // When that older frame, taken before the latch, is rendered.
  frame(snapshot({ at: 59.9 }));
  // Then the banner still stands on the receipt.
  assert.equal(shown("estop-banner"), true);
  // And a frame taken after the latch decides from then on.
  frame(snapshot({ at: 60.1 }));
  assert.equal(shown("estop-banner"), false);
});

test("a screen that did not press the button shows a stop latched elsewhere", () => {
  // Given a page with no receipt of its own (another screen, the camera, a rule).
  const { frame, shown } = panel();
  // When the machine reports a latched emergency stop.
  frame(snapshot({ ...verdict("quick_stop", "operator_estop") }));
  // Then the banner is raised here too.
  assert.equal(shown("estop-banner"), true);
});

test("the banner is kept for an emergency stop, not for every latched verdict", () => {
  const { frame, shown } = panel();
  // A latched drive fault ramps down: red in the safety chip, no emergency-stop banner.
  frame(snapshot({ ...verdict("ramp_down", "drive_fault") }));
  assert.equal(shown("estop-banner"), false);
  // A quick stop that is not latched has nothing to hold.
  frame(snapshot({ ...verdict("quick_stop", "operator_estop", false) }));
  assert.equal(shown("estop-banner"), false);
});

test("go_silent standing in front of a latched e-stop does not hide it", async () => {
  // Given go_silent, which outranks an emergency stop and replaces it as the standing verdict.
  const { context, frame, shown } = panel();
  frame(snapshot({ ...verdict("go_silent", "comms_lost") }));
  assert.equal(shown("estop-banner"), false);
  // When /api/status reports the e-stop latched behind it.
  context.api = () => Promise.resolve(status({ run_state: "stopping", estop_latched: true }));
  await context.loadStatus();
  frame(snapshot({ at: 51, ...verdict("go_silent", "comms_lost") }));
  // Then the banner is shown.
  assert.equal(shown("estop-banner"), true);
});

test("losing the machine keeps the last latched stop on screen, next to NO LIVE DATA", () => {
  // Given a latched emergency stop on a live page.
  const { context, clock, frame, shown } = panel();
  frame(snapshot({ ...verdict("quick_stop", "operator_estop") }));
  // When frames stop arriving.
  clock.now += 5000;
  context.refreshLiveness();
  // Then both banners are up: the page is not live, and its last word was a latched stop.
  assert.equal(shown("banner"), true);
  assert.equal(shown("estop-banner"), true);
});

test("what sticks below the banners is told the room they take", () => {
  // Given the page wired as on load, in a browser that reports element resizes.
  const { context, node } = panel();
  const published = new Map();
  const observers = [];
  context.document.documentElement = { style: { setProperty: (name, value) => published.set(name, value) } };
  context.document.querySelector = () => new Element();
  context.window.ResizeObserver = class {
    constructor(callback) {
      this.callback = callback;
    }
    observe(target) {
      observers.push([target, this.callback]);
    }
  };
  context.window.setInterval = () => undefined;
  context.window.requestAnimationFrame = () => undefined;
  context.boot = () => undefined;
  context.loadCamera = () => undefined;
  context.start();
  // When a banner appears and the stack grows.
  assert.equal(observers.length, 1);
  assert.equal(observers[0][0], node("banners"));
  node("banners").offsetHeight = 54;
  observers[0][1]();
  // Then the sidebar and the mobile bar are given that height to start under.
  assert.equal(published.get("--banners-h"), "54px");
});

/* -------------------------------------------- the heart-rate grade (2) */

for (const id of ["console-hr-quality", "hr-quality"]) {
  test(`#${id} says "perime", not the last grade, once the heart rate is stale`, () => {
    const { frame, node } = panel();
    // Given a fresh reading graded good.
    frame(snapshot());
    assert.equal(node(id).textContent, "good");
    assert.ok(node(id).classes.has("pill-good"));
    // When the BITalino drops and the same sample is carried forward, stale.
    frame(snapshot({ heart_rate: { bpm: 72, quality: "good", age_s: 14.7, stale: true, seq: 423 }, live_bpm: null }));
    // Then the badge no longer vouches for it.
    assert.equal(node(id).textContent, "perime");
    assert.equal(node(id).classes.has("pill-good"), false);
    assert.ok(node(id).classes.has("pill-bad"));
  });

  test(`#${id} keeps the grade of a fresh reading and names a missing one`, () => {
    const { frame, node } = panel();
    frame(snapshot({ heart_rate: { bpm: null, quality: "noisy", age_s: 0.2, stale: false, seq: 9 }, live_bpm: null }));
    assert.equal(node(id).textContent, "noisy");
    assert.ok(node(id).classes.has("pill-bad"));
    frame(snapshot({ heart_rate: null, live_bpm: null }));
    assert.equal(node(id).textContent, "pas de signal");
    assert.ok(node(id).classes.has("pill-bad"));
  });
}

/* ----------------------------------------- the e-stop latch line (3) */

for (const id of ["verdicts-grid", "system-grid"]) {
  test(`#${id} reports the e-stop latched when the camera latched it`, async () => {
    // Given an emergency stop latched by the camera: the interface's own flag stays false.
    const { context, node } = panel();
    context.api = () =>
      Promise.resolve(
        status({
          estop_latched: false,
          standing: verdict("quick_stop", "operator_estop").safety,
          floor: verdict("quick_stop", "presence_intrusion").safety,
        }),
      );
    // When the status is rendered.
    await context.loadStatus();
    // Then the line agrees with the acknowledgement, which demands the mushroom box.
    assert.equal(rows(node(id))["e-stop verrouille"].textContent, "OUI");
  });

  test(`#${id} follows the interface flag, and says "non" when nothing holds an e-stop`, async () => {
    const { context, node } = panel();
    context.api = () => Promise.resolve(status({ run_state: "stopping", estop_latched: true }));
    await context.loadStatus();
    assert.equal(rows(node(id))["e-stop verrouille"].textContent, "OUI");
    // A latched drive fault is a latched verdict, not a latched e-stop.
    context.api = () => Promise.resolve(status({ standing: verdict("ramp_down", "drive_fault").safety }));
    await context.loadStatus();
    assert.equal(rows(node(id))["e-stop verrouille"].textContent, "non");
  });
}

/* --------------------------------------------- the drive fault text (4) */

for (const id of ["console-fault", "fault"]) {
  test(`#${id} states the meaning of a drive fault once`, () => {
    // Given the fault row as the API sends it: `message` already carries the meaning.
    const meaning = "surtension du bus continu au freinage : allonger la rampe de deceleration.";
    const fault = { name: "dc_bus_overvoltage", mnemonic: "ObF", meaning, raw_code: 18, message: "ObF (LFT 18): " + meaning };
    const { frame, node, shown } = panel();
    // When it is rendered.
    frame(snapshot({ drive_state: "fault", fault }));
    // Then the box names the code the drive displays, and says what it means one time.
    const shownText = node(id).textContent;
    assert.equal(shown(id), true);
    assert.ok(shownText.includes("ObF"));
    assert.ok(shownText.includes("18"));
    assert.equal(shownText.split(meaning).length - 1, 1, shownText);
    assert.equal(shownText.split("ObF").length - 1, 1, shownText);
  });
}

/* ------------------------------------------------ the preview note (5) */

test("the preview result is still there after the status refresh, programmes disabled or not", async () => {
  for (const programs_enabled of [false, true]) {
    // Given a console, and a profile previewed on it.
    const { context, node, shown } = panel();
    const profile = { profile_id: "standard_30_min", zone_low_bpm: 118, zone_high_bpm: 138 };
    context.state.profiles = [profile];
    node("profile").value = "standard_30_min";
    context.renderPanel(panelRow({ programs_enabled }));
    context.api = (path) =>
      Promise.resolve(
        path === "/api/plan/preview"
          ? { profile, spans: [], ceiling: speed, warmup_ceiling: speed, min_run: speed, total_overridden: false }
          : status(),
      );
    context.doPreview();
    await settle();
    assert.equal(node("start-note").textContent, "plan resolu ; rien n'a ete demarre");
    // When the periodic status and panel refreshes run.
    await context.loadStatus();
    context.renderPanel(panelRow({ programs_enabled }));
    // Then the result of the click is still the note under the buttons.
    assert.equal(node("start-note").textContent, "plan resolu ; rien n'a ete demarre");
    // And the "programmes disabled" notice is said next to it, only when it is true.
    assert.equal(shown("programs-note"), !programs_enabled);
  }
});

/* ------------------------------------------------ the phase at rest (6) */

test("at rest the footer and the phase chip name no phase", () => {
  const { frame, node } = panel();
  // Given a machine at rest: the runtime's idle phase value is "done".
  frame(snapshot({ mode: "repos", phase: "done" }));
  // Then neither place prints it as if a programme had just ended.
  assert.equal(node("footer-status").textContent, "liaison ouverte · repos");
  assert.equal(node("phase").textContent, "-");
});

test("during a session the footer and the phase chip name the phase, done included", () => {
  const { frame, node } = panel();
  frame(snapshot({ mode: "manuel", phase: "hold" }));
  assert.equal(node("footer-status").textContent, "liaison ouverte · manuel · hold");
  assert.equal(node("phase").textContent, "hold");
  // A session that is over while the machine is still being stopped: "done" is a fact here.
  frame(snapshot({ mode: "arret", phase: "done" }));
  assert.equal(node("footer-status").textContent, "liaison ouverte · arret · done");
  assert.equal(node("phase").textContent, "done");
});

/* --------------------------------------------------- the tab title (7) */

test("the tab is titled after the page it shows", () => {
  // Given the heading each page of the markup carries.
  const heading = (view) =>
    new RegExp(`<section id="view-${view}"[^>]*>\\s*<h1 class="page-title">([^<]+)`).exec(html)[1].trim();
  const { context } = panel();
  context.api = () => Promise.resolve(status());
  // Then the title served with the page is the heading of the page shown on load.
  assert.equal(context.document.title, heading("console") + " - AnHeart");
  // And it follows the navigation, page after page.
  for (const view of ["sensors", "run", "setup", "safety", "console"]) {
    context.showView(view);
    assert.equal(context.document.title, heading(view) + " - AnHeart");
  }
  context.showView("sensor", "RESP");
  assert.equal(context.document.title, "RESP - AnHeart");
});

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

// Every banner of the markup, found by its class: its id and what it contains.
const banners = [...html.matchAll(/<div id="([^"]+)" class="banner(?: [^"]*)?"[^>]*>([\s\S]*?)<\/div>/g)].map(
  (banner) => ({ id: banner[1], inner: banner[2] }),
);

// The shipped page and script, with every element carrying the classes and the text the markup gives it.
function panel() {
  const nodes = new Map(
    [...html.matchAll(/<[a-z0-9]+\b[^>]*\bid="([^"]+)"[^>]*>([^<]*)/g)].map((tag) => [
      tag[1],
      new Element(/\bclass="([^"]*)"/.exec(tag[0])?.[1], tag[2].trim()),
    ]),
  );
  const clock = { now: 1000 };
  const published = new Map();
  const context = vm.createContext({
    document: {
      title: /<title>([^<]*)<\/title>/.exec(html)[1],
      documentElement: { style: { setProperty: (name, value) => published.set(name, value) } },
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
  // A machine that answers nothing until the test says what it answers.
  context.api = () => new Promise(() => undefined);
  // A page whose socket is open and delivering frames.
  context.state.connected = true;
  context.state.lastFrameAt = clock.now;
  const node = (id) => {
    assert.ok(nodes.has(id), `the page has no #${id}`);
    return nodes.get(id);
  };
  // What a banner reads on screen: its markup, with the parts the script rewrites as they are now.
  const said = (banner) =>
    banner.inner
      .replace(/<[a-z]+ id="([^"]+)"[^>]*>[^<]*<\/[a-z]+>/g, (whole, id) => nodes.get(id).textContent)
      .replace(/<[^>]+>/g, " ");
  return {
    context,
    clock,
    node,
    published,
    shown: (id) => !node(id).classes.has("hidden"),
    // Whether a banner saying `phrase` is on screen, whichever element carries it.
    announced: (phrase) =>
      banners.some((banner) => !nodes.get(banner.id).classes.has("hidden") && said(banner).includes(phrase)),
    // One frame from the machine, as the socket handler renders it.
    frame: (snapshot) => {
      clock.now += 200;
      context.state.lastFrameAt = clock.now;
      context.renderSnapshot(snapshot);
      context.refreshLiveness();
    },
  };
}

function deferred() {
  const answer = {};
  answer.promise = new Promise((resolve, reject) => Object.assign(answer, { resolve, reject }));
  return answer;
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

const silent = () => verdict("go_silent", "comms_lost");

// The operator presses E-STOP and the machine latches it. The status the page then asks for is
// answered with `statusAnswer`, or not at all.
async function pressEstop(context, statusAnswer = new Promise(() => undefined)) {
  context.api = (path) => (path === "/api/session/estop" ? Promise.resolve(receipt) : statusAnswer);
  context.doEstop();
  await settle();
}

/* ----------------------------------------- the emergency-stop banner (1) */

test("the notice of a latched stop is still on screen after the liveness check has run", async () => {
  // Given a live page whose operator presses E-STOP, and a machine that latches it.
  const { context, frame, announced } = panel();
  frame(snapshot({ at: 59.9 }));
  await pressEstop(context);
  assert.equal(announced("ARRET D'URGENCE VERROUILLE"), true);
  // When the next frames arrive, each followed by the liveness check that used to hide the notice.
  for (let tick = 1; tick <= 5; tick += 1) {
    frame(snapshot({ at: 60 + tick * 0.2, mode: "arret", ...verdict("quick_stop", "operator_estop") }));
    // Then a banner still says so, whichever element carries it.
    assert.equal(announced("ARRET D'URGENCE VERROUILLE"), true, `gone after ${tick} frames`);
    assert.equal(announced("NO LIVE DATA"), false);
  }
});

test("the emergency-stop banner outlives the liveness refresh for as long as the verdict is latched", async () => {
  // Given a live page whose operator presses E-STOP, and a machine that latches it.
  const { context, frame, shown } = panel();
  frame(snapshot({ at: 59.9 }));
  assert.equal(shown("estop-banner"), false);
  await pressEstop(context);
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
  await pressEstop(context);
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
  frame(snapshot({ ...silent() }));
  assert.equal(shown("estop-banner"), false);
  // When /api/status reports the e-stop latched behind it.
  context.api = () => Promise.resolve(status({ run_state: "stopping", estop_latched: true, standing: silent().safety }));
  await context.loadStatus();
  // Then the banner is shown as soon as that answer is in, and the next frame does not take it down.
  assert.equal(shown("estop-banner"), true);
  frame(snapshot({ at: 51, ...silent() }));
  assert.equal(shown("estop-banner"), true);
});

test("a screen that showed a latched stop keeps it when go_silent takes over before its next status poll", async () => {
  // Given a screen that did not press the button, whose last status poll predates the stop.
  const { context, frame, shown } = panel();
  context.api = () => Promise.resolve(status());
  await context.loadStatus();
  frame(snapshot({ at: 60.2, mode: "arret", ...verdict("quick_stop", "operator_estop") }));
  assert.equal(shown("estop-banner"), true);
  // When go_silent becomes the standing verdict: nothing can have acknowledged the stop since.
  context.api = () => new Promise(() => undefined);
  for (let tick = 1; tick <= 25; tick += 1) {
    frame(snapshot({ at: 61 + tick * 0.2, mode: "arret", ...silent() }));
    // Then the banner stays, on the old status and on no status answer at all.
    assert.equal(shown("estop-banner"), true, `hidden after ${tick} go_silent frames`);
  }
});

test("a frame that turns to go_silent asks for the status at once, and only once", () => {
  // Given a live page, and a count of what it asks the machine.
  const { context, frame } = panel();
  const asked = [];
  context.api = (path) => {
    asked.push(path);
    return new Promise(() => undefined);
  };
  frame(snapshot());
  assert.deepEqual(asked, []);
  // When go_silent becomes the standing verdict.
  frame(snapshot({ at: 51, ...silent() }));
  // Then the status is asked now, not at its next 5 s turn, and not again on every frame.
  assert.deepEqual(asked, ["/api/status"]);
  frame(snapshot({ at: 51.2, ...silent() }));
  frame(snapshot({ at: 51.4, ...silent() }));
  assert.deepEqual(asked, ["/api/status"]);
});

for (const outcome of ["slow", "failed"]) {
  test(`the clicking screen keeps its banner through go_silent when the status answer is ${outcome}`, async () => {
    // Given an E-STOP pressed as the link to the drive is lost: the status asked next has not answered.
    const { context, frame, shown } = panel();
    frame(snapshot({ at: 59.9, mode: "manuel", phase: "hold" }));
    const answer = deferred();
    await pressEstop(context, answer.promise);
    assert.equal(shown("estop-banner"), true);
    // When the first frames after the latch already carry go_silent, not the stop.
    for (let tick = 1; tick <= 10; tick += 1) {
      frame(snapshot({ at: 60 + tick * 0.2, mode: "arret", ...silent() }));
      assert.equal(shown("estop-banner"), true, `hidden after ${tick} go_silent frames`);
      // The receipt is not given up on a frame that cannot speak about the stop.
      assert.notEqual(context.state.estopReceipt, null);
    }
    // Then the answer, when it comes or fails, does not take the banner down either.
    if (outcome === "slow") {
      answer.resolve(status({ run_state: "stopping", estop_latched: true, standing: silent().safety }));
    } else {
      answer.reject(new Error("unreachable"));
    }
    await settle();
    assert.equal(shown("estop-banner"), true);
    // A status asked after the click takes over from the receipt; a failed one leaves it in place.
    assert.equal(context.state.estopReceipt === null, outcome === "slow");
    frame(snapshot({ at: 63, mode: "arret", ...silent() }));
    assert.equal(shown("estop-banner"), true);
  });
}

test("a stop latched by the camera stays announced once go_silent stands, whatever the status then says", async () => {
  // Given a stop latched by the camera: the frames show it, the interface's own flag stays false.
  const { context, frame, shown } = panel();
  frame(snapshot({ at: 60.2, mode: "arret", ...verdict("quick_stop", "operator_estop") }));
  assert.equal(shown("estop-banner"), true);
  // When go_silent takes over, and a fresh status can only repeat go_silent with the flag still false.
  context.api = () => Promise.resolve(status({ estop_latched: false, standing: silent().safety }));
  frame(snapshot({ at: 61, mode: "arret", ...silent() }));
  await settle();
  await context.loadStatus();
  // Then the banner stays: go_silent refuses every acknowledgement, so the stop is still latched.
  for (let tick = 1; tick <= 5; tick += 1) {
    frame(snapshot({ at: 61 + tick * 0.2, mode: "arret", ...silent() }));
    assert.equal(shown("estop-banner"), true, `hidden after ${tick} frames`);
  }
});

test("a latched e-stop reported by the status alone raises the banner, and a later answer lowers it", async () => {
  // Given a page that has not received a single frame yet.
  const { context, shown } = panel();
  // When the status says an emergency stop is latched.
  context.api = () => Promise.resolve(status({ run_state: "stopping", estop_latched: true }));
  await context.loadStatus();
  // Then the banner does not wait for a frame.
  assert.equal(shown("estop-banner"), true);
  // And with still no frame, only a later status saying it is released takes it down.
  context.api = () => Promise.resolve(status());
  await context.loadStatus();
  assert.equal(shown("estop-banner"), false);
});

test("with the socket down, a status that reports a latched e-stop raises the banner", async () => {
  // Given a last frame that showed no stop, then frames that stop coming.
  const { context, clock, frame, shown } = panel();
  frame(snapshot());
  context.state.connected = false;
  clock.now += 5000;
  context.refreshLiveness();
  assert.equal(shown("banner"), true);
  assert.equal(shown("estop-banner"), false);
  // When HTTP still answers, and says a stop latched by the camera is the standing verdict.
  context.api = () => Promise.resolve(status({ standing: verdict("quick_stop", "operator_estop").safety }));
  await context.loadStatus();
  // Then the banner is raised under NO LIVE DATA.
  assert.equal(shown("estop-banner"), true);
  // And a later answer where go_silent has taken over cannot say it was released: the banner stays.
  context.api = () => Promise.resolve(status({ standing: silent().safety }));
  await context.loadStatus();
  assert.equal(shown("estop-banner"), true);
});

test("with no frame to say so, a status asked after the click speaks for the receipt", async () => {
  // Given frames that stopped just before an E-STOP the machine still took over HTTP.
  const { context, clock, frame, shown } = panel();
  frame(snapshot({ at: 59.9 }));
  context.state.connected = false;
  clock.now += 5000;
  context.refreshLiveness();
  const answer = deferred();
  await pressEstop(context, answer.promise);
  assert.equal(shown("estop-banner"), true);
  // When the status asked after the click says the latch is gone: acknowledged from another screen.
  answer.resolve(status());
  await settle();
  // Then that answer is newer than the receipt, and the banner follows it.
  assert.equal(shown("estop-banner"), false);
});

test("a frame saying released against a status saying latched asks again instead of deciding", async () => {
  // Given a latched stop known from the frames and from the status.
  const { context, frame, shown } = panel();
  context.api = () => Promise.resolve(status({ run_state: "stopping", estop_latched: true }));
  await context.loadStatus();
  frame(snapshot({ at: 60.2, ...verdict("quick_stop", "operator_estop") }));
  // When a frame shows it released: acknowledged elsewhere, or a frame older than that status.
  const answer = deferred();
  const asked = [];
  context.api = (path) => {
    asked.push(path);
    return answer.promise;
  };
  frame(snapshot({ at: 61 }));
  // Then the banner stays while the two disagree, and the status is asked at once.
  assert.equal(shown("estop-banner"), true);
  assert.deepEqual(asked, ["/api/status"]);
  // And it goes when the status agrees.
  answer.resolve(status());
  await settle();
  assert.equal(shown("estop-banner"), false);
});

test("a status answer older than the one on screen is not applied", async () => {
  // Given two status requests in flight: one sent before a stop was latched, one after.
  const { context, node, shown } = panel();
  const before = deferred();
  const after = deferred();
  const answers = [before.promise, after.promise];
  context.api = () => answers.shift();
  const first = context.loadStatus();
  const second = context.loadStatus();
  // When the later one answers first.
  after.resolve(status({ run_state: "stopping", estop_latched: true }));
  await second;
  assert.equal(shown("estop-banner"), true);
  // Then the earlier one, arriving late, changes nothing.
  before.resolve(status());
  await first;
  assert.equal(shown("estop-banner"), true);
  assert.equal(node("run-state").textContent, "stopping");
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
  const { context, node, published } = panel();
  const observers = [];
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

test("without ResizeObserver the banner stack is still measured when a banner appears or goes", () => {
  // Given a browser with no ResizeObserver, and a stack that is 54 px tall with one banner in it.
  const { frame, node, published } = panel();
  node("banners").offsetHeight = 54;
  // When the emergency-stop banner appears.
  frame(snapshot({ ...verdict("quick_stop", "operator_estop") }));
  // Then the height is published for the sidebar and the mobile bar.
  assert.equal(published.get("--banners-h"), "54px");
  // And again when it goes.
  node("banners").offsetHeight = 0;
  frame(snapshot({ at: 51 }));
  assert.equal(published.get("--banners-h"), "0px");
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
  });

  test(`#${id} shows "perime" the way a stale sensor card does`, () => {
    // Given a sensor channel that has never delivered a window: its card says "perime".
    const { context, frame, node } = panel();
    context.renderSensors([
      { kind: "ECG", channel: 0, label: "Electrocardiogramme", unit: "mV", description: "", display_rate: 250, at: null, waveform: [], quality: "good", detail: "", metrics: [] },
    ]);
    const card = context.state.sensors.ECG.card.badge;
    assert.equal(card.textContent, "perime");
    // When the heart rate in control is stale too.
    frame(snapshot({ heart_rate: { bpm: 72, quality: "good", age_s: 14.7, stale: true, seq: 423 }, live_bpm: null }));
    // Then the same word has the same look in both places.
    assert.equal(node(id).textContent, card.textContent);
    assert.equal(node(id).className, card.className);
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

  test(`#${id} keeps the hexadecimal form of a code the table does not know`, () => {
    // Given an unknown code: its `message` is the only field that carries the hexadecimal form.
    const meaning = "code de defaut non reconnu : lire le code affiche sur le variateur.";
    const fault = { name: "unknown", mnemonic: "?", meaning, raw_code: 27, message: "code de defaut inconnu 27 (0x001B) : " + meaning };
    const { frame, node } = panel();
    // When it is rendered.
    frame(snapshot({ drive_state: "fault", fault }));
    // Then the code is there both ways, and the meaning still one time.
    const shownText = node(id).textContent;
    assert.ok(shownText.includes("27"));
    assert.ok(shownText.includes("0x001B"), shownText);
    assert.equal(shownText.split(meaning).length - 1, 1, shownText);
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

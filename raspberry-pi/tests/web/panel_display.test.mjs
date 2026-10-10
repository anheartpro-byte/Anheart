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
  const listeners = [];
  // One-shot timers the page sets: kept here, and fired by `wait` when their time has come.
  const timers = [];
  const context = vm.createContext({
    document: {
      title: /<title>([^<]*)<\/title>/.exec(html)[1],
      documentElement: { style: { setProperty: (name, value) => published.set(name, value) } },
      getElementById: (id) => nodes.get(id) ?? null,
      createElement: () => new Element(),
    },
    window: {
      addEventListener: (name, listener) => listeners.push([name, listener]),
      sessionStorage: { getItem: () => null, setItem: () => undefined },
      setTimeout: (callback, delay) => timers.push({ callback, due: clock.now + delay }),
    },
    performance: { now: () => clock.now },
  });
  vm.runInContext(source, context, { filename: fileURLToPath(new URL("app.js", assets)) });
  // The page's own request code, for a test that answers at the network instead (`context.fetch`).
  const send = context.api;
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
  // A crude layout, so that a height measured too early is a wrong height: a banner on screen takes
  // one line per `screen.columns` characters it reads, and the stack is the sum of what is shown.
  const screen = { columns: 40 };
  const stack = () =>
    banners
      .filter((banner) => !nodes.get(banner.id).classes.has("hidden"))
      .map((banner) => said(banner).replace(/\s+/g, " ").trim().length)
      .reduce((height, length) => height + 27 + 27 * Math.ceil(length / screen.columns), 0);
  if (nodes.has("banners")) {
    Object.defineProperty(nodes.get("banners"), "offsetHeight", { get: stack });
  }
  return {
    context,
    send,
    clock,
    node,
    published,
    screen,
    stack,
    // What the browser does on a window event: call whoever listens to it.
    fire: (name) => listeners.filter((entry) => entry[0] === name).forEach((entry) => entry[1]()),
    // Time passing with nothing arriving: the timers that have come due fire, in the order they were set.
    wait: (ms) => {
      clock.now += ms;
      const due = timers.filter((timer) => timer.due <= clock.now);
      due.forEach((timer) => timers.splice(timers.indexOf(timer), 1));
      due.forEach((timer) => timer.callback());
    },
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

// One /api/status answer. `at` is the machine's clock when it was written, the clock of a frame's
// `at`: like a frame's, it is the instant of the default frame unless the test says another.
function status(overrides = {}) {
  return {
    at: 50,
    run_state: "idle",
    estop_latched: false,
    supervisor_estop: null,
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
    manual_rise_hold: null,
    radius_m: 1.5,
    gear_ratio: 49.79,
    motor_max_rpm: 300,
    software_version: "pi-0.0.0-dev",
    dashboard: { state: "not_configured", label: "non configure", detail: "", last_answer_age_s: null },
    ...overrides,
  };
}

const receipt = { operator: "", reason: "", at: 60, wall_clock: 0, action: "quick_stop", rule: "operator_estop", detail: "" };

const silent = () => verdict("go_silent", "comms_lost");

// What /api/status answers when the camera latched a stop and the link to the drive was then lost:
// go_silent is the standing verdict and the floor, and the stop shows in the supervisor's slot alone.
const cameraStopBehindSilence = (at = 50) =>
  status({
    at,
    estop_latched: false,
    supervisor_estop: verdict("quick_stop", "operator_estop").safety,
    standing: silent().safety,
    floor: silent().safety,
  });

// What it answers under go_silent with no emergency stop held behind it.
const silenceAlone = (at = 50) => status({ at, standing: silent().safety, floor: silent().safety });

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
  context.api = () =>
    Promise.resolve(status({ at: 50.1, run_state: "stopping", estop_latched: true, standing: silent().safety }));
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
      answer.resolve(status({ at: 62.1, run_state: "stopping", estop_latched: true, standing: silent().safety }));
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

test("a stop latched by the camera stays announced once go_silent stands: the status still carries it", async () => {
  // Given a stop latched by the camera: the frames show it, the interface's own flag stays false.
  const { context, frame, shown } = panel();
  frame(snapshot({ at: 60.2, mode: "arret", ...verdict("quick_stop", "operator_estop") }));
  assert.equal(shown("estop-banner"), true);
  // When go_silent takes over: the status asked at once, and those after it, show the stop in the
  // supervisor's own slot, with the flag still false.
  context.api = () => Promise.resolve(cameraStopBehindSilence(61.1));
  frame(snapshot({ at: 61, mode: "arret", ...silent() }));
  await settle();
  context.api = () => Promise.resolve(cameraStopBehindSilence(61.6));
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
  context.api = () => Promise.resolve(status({ at: 60, run_state: "stopping", estop_latched: true }));
  await context.loadStatus();
  // Then the banner does not wait for a frame.
  assert.equal(shown("estop-banner"), true);
  // And with still no frame, only a later status saying it is released takes it down.
  context.api = () => Promise.resolve(status({ at: 65 }));
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
  const stop = verdict("quick_stop", "operator_estop").safety;
  context.api = () => Promise.resolve(status({ at: 55, supervisor_estop: stop, standing: stop }));
  await context.loadStatus();
  // Then the banner is raised under NO LIVE DATA: that answer is newer than the last frame.
  assert.equal(shown("estop-banner"), true);
  // And a later answer where go_silent has taken over still carries the stop: the banner stays.
  context.api = () => Promise.resolve(cameraStopBehindSilence(60));
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
  answer.resolve(status({ at: 62 }));
  await settle();
  // Then that answer is newer than the receipt, and the banner follows it.
  assert.equal(shown("estop-banner"), false);
});

test("a status asked before the click does not take over from the receipt", async () => {
  // Given frames that have stopped, and a status request still in flight when E-STOP is pressed.
  const { context, clock, frame, shown } = panel();
  frame(snapshot({ at: 59.9 }));
  context.state.connected = false;
  clock.now += 5000;
  context.refreshLiveness();
  const early = deferred();
  context.api = () => early.promise;
  const asked = context.loadStatus();
  await pressEstop(context);
  // When that request is answered after the receipt, with what the machine said before the latch.
  early.resolve(status({ at: 59.95 }));
  await asked;
  // Then it is older than the latch: the receipt keeps the banner up.
  assert.equal(shown("estop-banner"), true);
  assert.notEqual(context.state.estopReceipt, null);
});

/* ------ a frame and a status that disagree: the one the machine wrote last ------ */

const latchedStatus = (at) =>
  status({
    at,
    run_state: "stopping",
    estop_latched: true,
    supervisor_estop: verdict("quick_stop", "operator_estop").safety,
    standing: verdict("quick_stop", "operator_estop").safety,
  });

test("a frame that shows the stop released outdates an older status that said latched", async () => {
  // Given a latched stop known from the frames and from the status.
  const { context, frame, node, shown } = panel();
  context.api = () => Promise.resolve(latchedStatus(60.1));
  await context.loadStatus();
  frame(snapshot({ at: 60.2, ...verdict("quick_stop", "operator_estop") }));
  // When a frame taken after that status shows it released: acknowledged on another screen.
  const answer = deferred();
  const asked = [];
  context.api = (path) => {
    asked.push(path);
    return answer.promise;
  };
  frame(snapshot({ at: 61 }));
  // Then the banner goes with the frame, which is the newer of the two.
  assert.equal(shown("estop-banner"), false);
  // And the status is asked at once, not at its next turn: the lines it feeds still say latched.
  assert.deepEqual(asked, ["/api/status"]);
  assert.equal(rows(node("verdicts-grid"))["e-stop verrouille"].textContent, "OUI");
  answer.resolve(status({ at: 61.1 }));
  await settle();
  assert.equal(rows(node("verdicts-grid"))["e-stop verrouille"].textContent, "non");
  // Once for that turn of the frames, not once a frame.
  frame(snapshot({ at: 61.2 }));
  assert.deepEqual(asked, ["/api/status"]);
});

test("a status written after the frame on screen and saying latched raises the banner over it", async () => {
  // Given a live page whose latest frame shows no stop.
  const { context, frame, shown } = panel();
  frame(snapshot({ at: 60 }));
  // When a status written after that frame says a stop is latched: the frame is the older of the two.
  const asked = [];
  context.api = (path) => {
    asked.push(path);
    return Promise.resolve(latchedStatus(60.1));
  };
  await context.loadStatus();
  // Then the banner is up, and nothing is asked again: the next frame will say it too.
  assert.equal(shown("estop-banner"), true);
  assert.deepEqual(asked, ["/api/status"]);
  frame(snapshot({ at: 60.2, mode: "arret", ...verdict("quick_stop", "operator_estop") }));
  assert.equal(shown("estop-banner"), true);
});

test("a status saying latched and a frame saying released at the same instant: the banner is up", async () => {
  // Neither is the older one. The prudent reading stands until the next frame settles it.
  const { context, frame, shown } = panel();
  frame(snapshot({ at: 60 }));
  context.api = () => Promise.resolve(latchedStatus(60));
  await context.loadStatus();
  assert.equal(shown("estop-banner"), true);
});

/* ------ EX-1, a status answer that arrives late, after a quick acknowledgement ------ */

test("a status asked before a quick acknowledgement and arriving after it does not bring the banner back", async () => {
  // Given a screen whose last status predates the stop, and a stop latched from another screen.
  const { context, frame, node, shown } = panel();
  context.api = () => Promise.resolve(status({ at: 58 }));
  await context.loadStatus();
  frame(snapshot({ at: 60.2, mode: "arret", ...verdict("quick_stop", "operator_estop") }));
  assert.equal(shown("estop-banner"), true);
  // And a periodic status asked while it is latched, whose answer is slow to come back.
  const late = deferred();
  context.api = () => late.promise;
  const periodic = context.loadStatus();
  // When the stop is acknowledged at once, and the frames show it released.
  frame(snapshot({ at: 61 }));
  assert.equal(shown("estop-banner"), false);
  // And that answer, written while the stop was latched, reaches the screen only now.
  const again = deferred();
  const asked = [];
  context.api = (path) => {
    asked.push(path);
    return again.promise;
  };
  late.resolve(latchedStatus(60.6));
  await periodic;
  // Then the banner stays down: on the machine's clock the frame on screen is the newer one.
  assert.equal(shown("estop-banner"), false);
  for (let tick = 1; tick <= 5; tick += 1) {
    frame(snapshot({ at: 61 + tick * 0.2 }));
    assert.equal(shown("estop-banner"), false, `back after ${tick} frames`);
  }
  // And the lines that answer feeds are not left to say "latched" until the next turn: asked again, once.
  assert.deepEqual(asked, ["/api/status"]);
  again.resolve(status({ at: 62.1 }));
  await settle();
  assert.equal(rows(node("verdicts-grid"))["e-stop verrouille"].textContent, "non");
  assert.equal(node("run-state").classes.has("pill-bad"), false);
});

/* ------ EX-1, two status answers: ordered by when the machine wrote them ------ */

// Two status requests in flight together, on a page that receives no frame: `first` was asked before `second`.
function crossedStatuses(context) {
  const one = deferred();
  const two = deferred();
  const answers = [one.promise, two.promise];
  context.api = () => answers.shift();
  const first = context.loadStatus();
  const second = context.loadStatus();
  return { one, two, first, second };
}

test("a status asked first but written last by the machine is the one kept", async () => {
  // Given two requests that crossed: the machine answered the second, a stop was latched, then it
  // answered the first.
  const { context, node, shown } = panel();
  const { one, two, first, second } = crossedStatuses(context);
  // When the answers come back in the order the machine wrote them.
  two.resolve(status({ at: 60 }));
  await second;
  assert.equal(shown("estop-banner"), false);
  one.resolve(latchedStatus(60.4));
  await first;
  // Then the stop is announced: the answer asked first is the newer one, and it is not dropped.
  assert.equal(shown("estop-banner"), true);
  assert.equal(node("run-state").textContent, "stopping");
});

test("a status asked last but written first by the machine does not replace the newer one", async () => {
  // Given the same two requests, and the same stop latched between the two answers.
  const { context, node, shown } = panel();
  const { one, two, first, second } = crossedStatuses(context);
  // When the newer answer arrives first.
  one.resolve(latchedStatus(60.4));
  await first;
  assert.equal(shown("estop-banner"), true);
  // And the older one, asked after it and written before the stop, arrives last.
  two.resolve(status({ at: 60 }));
  await second;
  // Then the banner is not taken down by it, and nothing of it is on screen.
  assert.equal(shown("estop-banner"), true);
  assert.equal(node("run-state").textContent, "stopping");
  assert.equal(rows(node("verdicts-grid"))["e-stop verrouille"].textContent, "OUI");
});

test("a status asked first, written first and arriving last is not applied", async () => {
  // Given two status requests in flight: one answered before a stop was latched, one after.
  const { context, node, shown } = panel();
  const { one, two, first, second } = crossedStatuses(context);
  // When the later one answers first.
  two.resolve(latchedStatus(60.4));
  await second;
  assert.equal(shown("estop-banner"), true);
  // Then the earlier one, arriving late, changes nothing.
  one.resolve(status({ at: 60 }));
  await first;
  assert.equal(shown("estop-banner"), true);
  assert.equal(node("run-state").textContent, "stopping");
});

test("after the machine's clock restarts, its answers are not dropped as older than the one on screen", async () => {
  // Given a status on screen from a machine that has been up for hours, and a last frame from it.
  const { context, clock, frame, node, shown } = panel();
  frame(snapshot({ at: 19999, mode: "manuel", phase: "hold" }));
  context.state.connected = false;
  clock.now += 5000;
  context.refreshLiveness();
  context.api = () => Promise.resolve(status({ at: 20000, run_state: "running" }));
  await context.loadStatus();
  assert.equal(node("run-state").textContent, "running");
  // When the Pi reboots: the next request, sent once that answer was in, is answered on a clock
  // that started again from zero.
  context.api = () => Promise.resolve(latchedStatus(42));
  await context.loadStatus();
  // Then that answer is the present, whatever its clock reads: it is not dropped, and the frame of
  // before the reboot, whose clock reads later, does not outdate the stop it reports.
  assert.equal(node("run-state").textContent, "stopping");
  assert.equal(shown("estop-banner"), true);
  // And so are the ones after it.
  context.api = () => Promise.resolve(status({ at: 47 }));
  await context.loadStatus();
  assert.equal(node("run-state").textContent, "idle");
  assert.equal(shown("estop-banner"), false);
});

/* ------ EX-1, a console restarted, then go_silent again ------ */

test("a banner from before a restart of the console goes once the status says no stop is held behind go_silent", async () => {
  // Given a latched stop on screen, from the frames of a console that is then restarted.
  const { context, frame, shown } = panel();
  context.api = () => Promise.resolve(latchedStatus(60.3));
  await context.loadStatus();
  frame(snapshot({ at: 60.4, mode: "arret", ...verdict("quick_stop", "operator_estop") }));
  assert.equal(shown("estop-banner"), true);
  // When the new console has gone silent before this screen saw one frame without a verdict: its
  // frames cannot say whether a stop is latched. A restarted console holds none.
  const answer = deferred();
  const asked = [];
  context.api = (path) => {
    asked.push(path);
    return answer.promise;
  };
  frame(snapshot({ at: 75, mode: "repos", ...silent() }));
  // Then the status is asked at once, and until it answers the banner stays: unknown is not released.
  assert.deepEqual(asked, ["/api/status"]);
  frame(snapshot({ at: 75.2, mode: "repos", ...silent() }));
  assert.equal(shown("estop-banner"), true);
  // And it goes with the answer written under that go_silent: no emergency stop is held behind it.
  // Written after the first of those frames, it need not be newer than the latest one.
  answer.resolve(silenceAlone(75.1));
  await settle();
  assert.equal(shown("estop-banner"), false);
  for (let tick = 2; tick <= 6; tick += 1) {
    frame(snapshot({ at: 75 + tick * 0.2, mode: "repos", ...silent() }));
    assert.equal(shown("estop-banner"), false, `back after ${tick} frames`);
  }
});

test("a banner raised by the status of a console since restarted goes with the first status of the new one", async () => {
  // Given a page opened under go_silent, told by the status that a stop is latched behind it.
  const { context, frame, shown } = panel();
  context.api = () => Promise.resolve(cameraStopBehindSilence(70.1));
  frame(snapshot({ at: 70, mode: "arret", ...silent() }));
  await settle();
  assert.equal(shown("estop-banner"), true);
  // When the console is restarted and goes silent again: the frames say the same as before.
  context.api = () => new Promise(() => undefined);
  frame(snapshot({ at: 90, mode: "repos", ...silent() }));
  assert.equal(shown("estop-banner"), true);
  // Then the next status, from the new console, takes the banner down.
  context.api = () => Promise.resolve(silenceAlone(92));
  await context.loadStatus();
  assert.equal(shown("estop-banner"), false);
});

test("under go_silent a status written before the frames turned to it cannot take the banner down", async () => {
  // Given a stop latched by the camera, shown by the frames, then go_silent standing in front of it.
  const { context, frame, shown } = panel();
  const early = deferred();
  context.api = () => early.promise;
  const asked = context.loadStatus();
  frame(snapshot({ at: 60.2, mode: "arret", ...verdict("quick_stop", "operator_estop") }));
  context.api = () => new Promise(() => undefined);
  frame(snapshot({ at: 61, mode: "arret", ...silent() }));
  assert.equal(shown("estop-banner"), true);
  // When a status written before the stop was latched arrives only now, saying nothing is latched.
  early.resolve(status({ at: 60.1 }));
  await asked;
  // Then the banner stays: that answer has not seen what the frames showed.
  assert.equal(shown("estop-banner"), true);
  frame(snapshot({ at: 61.2, mode: "arret", ...silent() }));
  assert.equal(shown("estop-banner"), true);
});

/* ------ go_silent replacing the latched quick stop of a rule: the same console, or another ------ */

// What /api/status answers under go_silent with no emergency stop held, from a console that has
// published `published` snapshots since it started.
const silenceAfter = (at, published) => ({ ...silenceAlone(at), counters: [1, 0, published] });

// A latched quick stop of a rule as the status shows it, from a console that has published `published` snapshots.
const ruleStopStatus = (at, published) =>
  status({
    at,
    standing: verdict("quick_stop", "reverse_rotation").safety,
    floor: verdict("quick_stop", "reverse_rotation").safety,
    counters: [1, 0, published],
  });

test("go_silent replacing the latched quick stop of a rule keeps the banner, in the console that showed it", async () => {
  // Given the latched quick stop of a rule, shown by the frames and by the status of that console.
  const { context, frame, shown } = panel();
  frame(snapshot({ at: 60.2, mode: "arret", ...verdict("quick_stop", "reverse_rotation") }));
  context.api = () => Promise.resolve(ruleStopStatus(60.3, 300));
  await context.loadStatus();
  assert.equal(shown("estop-banner"), true);
  // When go_silent replaces it in that same console: the status then shows go_silent, and no
  // emergency stop held behind it. Nobody acknowledged anything, and nothing can be from then on.
  context.api = () => Promise.resolve(silenceAfter(61.1, 304));
  frame(snapshot({ at: 61, mode: "arret", ...silent() }));
  await settle();
  // Then the banner does not go by itself as the machine reaches its most severe state.
  assert.equal(shown("estop-banner"), true);
  // Nor with the frames and the status answers that follow, for as long as that console runs.
  for (let tick = 1; tick <= 25; tick += 1) {
    frame(snapshot({ at: 61 + tick * 0.2, mode: "repos", ...silent() }));
  }
  context.api = () => Promise.resolve(silenceAfter(66.1, 329));
  await context.loadStatus();
  assert.equal(shown("estop-banner"), true);
});

test("a link that drops and comes back proves no restart: the banner of a rule's quick stop stays", async () => {
  // Given the latched quick stop of a rule on screen, then a socket that drops.
  const { context, clock, frame, shown } = panel();
  frame(snapshot({ at: 60.2, mode: "arret", ...verdict("quick_stop", "reverse_rotation") }));
  context.api = () => Promise.resolve(ruleStopStatus(60.3, 300));
  await context.loadStatus();
  context.state.connected = false;
  clock.now += 5000;
  context.refreshLiveness();
  // When the frames come back under go_silent, from a console whose count of snapshots went on rising.
  context.state.connected = true;
  context.api = () => Promise.resolve(silenceAfter(66.1, 329));
  frame(snapshot({ at: 66, mode: "repos", ...silent() }));
  await settle();
  // Then nothing says the console restarted, and the banner stays.
  assert.equal(shown("estop-banner"), true);
});

test("the banner of a rule's quick stop goes with a console proven restarted: its count of snapshots went down", async () => {
  // Given the latched quick stop of a rule, shown by the frames and by the status of a console
  // that has been running for a while.
  const { context, frame, shown } = panel();
  frame(snapshot({ at: 60.2, mode: "arret", ...verdict("quick_stop", "reverse_rotation") }));
  context.api = () => Promise.resolve(ruleStopStatus(60.3, 5000));
  await context.loadStatus();
  // The frames go on showing it, five a second, until that console is stopped.
  context.api = () => new Promise(() => undefined);
  for (let tick = 1; tick <= 20; tick += 1) {
    frame(snapshot({ at: 60.2 + tick * 0.2, mode: "repos", ...verdict("quick_stop", "reverse_rotation") }));
  }
  assert.equal(shown("estop-banner"), true);
  // When the console is restarted and goes silent before this screen sees a frame without a verdict:
  // a new socket, and a status written under that go_silent by a console that has published far
  // fewer snapshots.
  context.state.stream += 1;
  context.api = () => Promise.resolve(silenceAfter(75.1, 40));
  frame(snapshot({ at: 75, mode: "repos", ...silent() }));
  await settle();
  // Then the latch the frames showed was another console's, and the banner goes.
  assert.equal(shown("estop-banner"), false);
});

test("a latch the new console itself shows is its own: the restart before it does not speak for it", async () => {
  // Given the latched quick stop of a rule shown by a console, then that console restarted.
  const { context, frame, shown } = panel();
  frame(snapshot({ at: 60.2, mode: "arret", ...verdict("quick_stop", "reverse_rotation") }));
  context.api = () => Promise.resolve(ruleStopStatus(60.3, 5000));
  await context.loadStatus();
  // And the same rule latched again in the new console, shown over the new socket.
  context.state.stream += 1;
  context.api = () => new Promise(() => undefined);
  frame(snapshot({ at: 75, mode: "arret", ...verdict("quick_stop", "reverse_rotation") }));
  // When go_silent replaces it there, and the first status of that console comes in: fewer
  // snapshots than the old console had published, which proves a restart, but one that came
  // before this latch.
  context.api = () => Promise.resolve(silenceAfter(76.1, 40));
  frame(snapshot({ at: 76, mode: "repos", ...silent() }));
  await settle();
  // Then the banner stays: it is the console that showed this latch that went silent.
  assert.equal(shown("estop-banner"), true);
});

test("with no status between the latch and the restart, nothing proves the restart and the banner stays", async () => {
  // Given the latched quick stop of a rule seen in the frames only: no status answer came after it.
  const { context, frame, shown } = panel();
  context.api = () => Promise.resolve(status({ at: 58, counters: [0, 0, 4900] }));
  await context.loadStatus();
  frame(snapshot({ at: 60.2, mode: "arret", ...verdict("quick_stop", "reverse_rotation") }));
  // When the console is restarted and goes silent, and its status answers come in.
  context.api = () => Promise.resolve(silenceAfter(75.1, 40));
  frame(snapshot({ at: 75, mode: "repos", ...silent() }));
  await settle();
  // Then the count seen before the latch says nothing of what followed it: the page cannot tell
  // this from the same console, and keeps the banner.
  assert.equal(shown("estop-banner"), true);
  context.api = () => Promise.resolve(silenceAfter(80.1, 65));
  await context.loadStatus();
  assert.equal(shown("estop-banner"), true);
  // A frame without a verdict takes it down, as always.
  frame(snapshot({ at: 81 }));
  assert.equal(shown("estop-banner"), false);
});

test("a status asked before the frames showed the latch proves nothing about what followed it", async () => {
  // Given a status request in flight when the frames show the latched quick stop of a rule.
  const { context, frame, shown } = panel();
  const early = deferred();
  context.api = () => early.promise;
  const asked = context.loadStatus();
  frame(snapshot({ at: 60.2, mode: "arret", ...verdict("quick_stop", "reverse_rotation") }));
  // Its answer, whichever console wrote it, was not asked of the one that showed the latch.
  early.resolve(status({ at: 60.1, counters: [0, 0, 5000] }));
  await asked;
  // When go_silent stands and the status then shows fewer snapshots published.
  context.api = () => Promise.resolve(silenceAfter(61.1, 40));
  frame(snapshot({ at: 61, mode: "repos", ...silent() }));
  await settle();
  // Then that is no proof that the console which showed the latch has been replaced.
  assert.equal(shown("estop-banner"), true);
});

test("the socket of the telemetry is counted each time it is opened", () => {
  // The frames of one socket come from one console: the page tells sockets apart by this count.
  const { context } = panel();
  context.WebSocket = class {
    close() {}
  };
  context.window.location = { protocol: "http:", host: "127.0.0.1:8080" };
  const before = context.state.stream;
  context.connect();
  context.connect();
  assert.equal(context.state.stream, before + 2);
});

test("the banner of a rule's quick stop goes with a machine whose clock restarted", async () => {
  // Given the latched quick stop of a rule on screen, on a machine that has been up for hours.
  const { context, frame, shown } = panel();
  frame(snapshot({ at: 20001, mode: "arret", ...verdict("quick_stop", "reverse_rotation") }));
  assert.equal(shown("estop-banner"), true);
  // When the Pi reboots and its console goes silent: the frames come back on a clock that restarted.
  context.api = () => Promise.resolve(silenceAfter(33, 160));
  frame(snapshot({ at: 31, mode: "repos", ...silent() }));
  await settle();
  // Then the latch was shown before the reboot, and the status of the new console decides.
  assert.equal(shown("estop-banner"), false);
});

test("a latch the frames have shown released is forgotten: it does not hold a later banner under go_silent", async () => {
  // Given the latched quick stop of a rule, then frames showing it released: it was acknowledged.
  const { context, frame, shown } = panel();
  frame(snapshot({ at: 60, mode: "arret", ...verdict("quick_stop", "reverse_rotation") }));
  frame(snapshot({ at: 61 }));
  assert.equal(shown("estop-banner"), false);
  // And an emergency stop latched from another screen, which this screen learns from the status.
  context.api = () => Promise.resolve({ ...latchedStatus(61.5), counters: [1, 0, 300] });
  await context.loadStatus();
  assert.equal(shown("estop-banner"), true);
  // When that stop is acknowledged and go_silent stands, before a frame has shown either.
  context.api = () => Promise.resolve(silenceAfter(62.1, 304));
  frame(snapshot({ at: 62, mode: "repos", ...silent() }));
  await settle();
  // Then the status speaks for the emergency stop, and the rule's old latch holds nothing.
  assert.equal(shown("estop-banner"), false);
});

test("an emergency stop released just before go_silent: the status carries that latch and takes the banner down", async () => {
  // Given a latched emergency stop shown by the frames, in a console that keeps running.
  const { context, frame, shown } = panel();
  frame(snapshot({ at: 60.2, mode: "arret", ...verdict("quick_stop", "operator_estop") }));
  context.api = () => Promise.resolve({ ...latchedStatus(60.3), counters: [1, 0, 300] });
  await context.loadStatus();
  assert.equal(shown("estop-banner"), true);
  // When it is acknowledged, and go_silent stands before this screen has seen a frame without it.
  context.api = () => Promise.resolve(silenceAfter(61.1, 304));
  frame(snapshot({ at: 61, mode: "repos", ...silent() }));
  await settle();
  // Then the status, which holds the emergency stop itself, says it is released: the banner goes.
  assert.equal(shown("estop-banner"), false);
});

test("a status from before a restart of the machine's clock does not speak for the frames after it", async () => {
  // Given a latched stop on screen from the frames, and an older status that said nothing was latched.
  const { context, frame, shown } = panel();
  context.api = () => Promise.resolve(status({ at: 20000 }));
  await context.loadStatus();
  frame(snapshot({ at: 20001, mode: "arret", ...verdict("quick_stop", "operator_estop") }));
  assert.equal(shown("estop-banner"), true);
  // When the Pi reboots and its console goes silent: the frames come back on a clock that restarted.
  context.api = () => new Promise(() => undefined);
  frame(snapshot({ at: 31, mode: "repos", ...silent() }));
  // Then the status of before the reboot, whose clock reads later, is not taken for a newer one.
  assert.equal(shown("estop-banner"), true);
  // And the status of after it decides.
  context.api = () => Promise.resolve(silenceAlone(33));
  await context.loadStatus();
  assert.equal(shown("estop-banner"), false);
});

/* ------ a latched stop the status showed: the same rule, whatever raised the banner ------ */

// The telemetry socket of this screen drops while its requests are still answered.
function dropSocket({ context, clock }) {
  context.state.connected = false;
  clock.now += 5000;
  context.refreshLiveness();
}

// One /api/status answer, asked and applied now.
async function answer(context, row) {
  context.api = () => Promise.resolve(row);
  await context.loadStatus();
}

// How the screen comes to show a banner the status alone raised: after frames that then stopped,
// or with no frame ever received.
const STATUS_ALONE = [
  ["with the socket down", (page) => {
    page.frame(snapshot({ at: 58, mode: "manuel" }));
    dropSocket(page);
    assert.equal(page.shown("banner"), true);
  }],
  ["with no frame ever received", () => undefined],
];

for (const [how, given] of STATUS_ALONE) {
  test(`go_silent replacing a rule's quick stop keeps a banner the status raised, ${how}`, async () => {
    // Given the latched quick stop of a rule, which this screen learns from the status alone.
    const page = panel();
    const { context, shown } = page;
    given(page);
    await answer(context, ruleStopStatus(60.3, 39));
    assert.equal(shown("estop-banner"), true);
    // When go_silent replaces it in that same console: its count of snapshots goes on rising, and
    // its status shows go_silent with no emergency stop held behind it.
    await answer(context, silenceAfter(65.3, 96));
    // Then the banner stays: nobody acknowledged anything, and nothing can be from then on.
    assert.equal(shown("estop-banner"), true);
    // And with every answer that follows, for as long as that console runs.
    await answer(context, silenceAfter(70.3, 166));
    assert.equal(shown("estop-banner"), true);
  });

  test(`that banner goes with a console proven restarted, ${how}`, async () => {
    // Given the same banner, kept under the go_silent of the console that showed the latch.
    const page = panel();
    const { context, shown } = page;
    given(page);
    await answer(context, ruleStopStatus(60.3, 39));
    await answer(context, silenceAfter(65.3, 96));
    assert.equal(shown("estop-banner"), true);
    // When a status shows fewer snapshots published than the console that showed the latch had:
    // a console started since, which holds nothing of what the other held.
    await answer(context, silenceAfter(80.3, 15));
    // Then the banner goes.
    assert.equal(shown("estop-banner"), false);
  });
}

test("frames that come back under go_silent do not take down a banner the status raised", async () => {
  // Given a banner raised by the status with the socket down, and kept under go_silent.
  const page = panel();
  const { context, frame, shown } = page;
  frame(snapshot({ at: 58, mode: "manuel" }));
  dropSocket(page);
  await answer(context, ruleStopStatus(60.3, 39));
  await answer(context, silenceAfter(65.3, 96));
  // When the socket comes back: the frames are under go_silent, and a status is written after them.
  context.state.connected = true;
  context.state.stream += 1;
  context.api = () => Promise.resolve(silenceAfter(71.1, 170));
  frame(snapshot({ at: 71, mode: "repos", ...silent() }));
  await settle();
  // Then the banner stays: that console showed the latch, and has not restarted.
  assert.equal(shown("estop-banner"), true);
});

test("a rule's quick stop the status showed, then shown released outside go_silent, takes its banner down", async () => {
  // Given a banner raised by the status alone.
  const { context, shown } = panel();
  await answer(context, ruleStopStatus(60.3, 39));
  assert.equal(shown("estop-banner"), true);
  // When a later status shows no latched stop and no go_silent: it was acknowledged.
  await answer(context, status({ at: 65.3, counters: [1, 0, 96] }));
  // Then the banner goes.
  assert.equal(shown("estop-banner"), false);
});

test("a latch the status has since shown released is forgotten: it does not hold a later banner under go_silent", async () => {
  // Given the latched quick stop of a rule the status showed, then showed released: acknowledged.
  const { context, frame, shown } = panel();
  await answer(context, ruleStopStatus(60.3, 100));
  await answer(context, status({ at: 65.3, counters: [1, 0, 150] }));
  assert.equal(shown("estop-banner"), false);
  // And an emergency stop latched since, which the frames show.
  context.api = () => new Promise(() => undefined);
  frame(snapshot({ at: 70, mode: "arret", ...verdict("quick_stop", "operator_estop") }));
  assert.equal(shown("estop-banner"), true);
  // When that stop is acknowledged and go_silent stands, before a frame has shown either.
  context.api = () => Promise.resolve(silenceAfter(71.1, 400));
  frame(snapshot({ at: 71, mode: "repos", ...silent() }));
  await settle();
  // Then the status speaks for the emergency stop, and the rule's old latch holds nothing.
  assert.equal(shown("estop-banner"), false);
});

test("the status shows an emergency stop, then a rule's quick stop alone: go_silent keeps the banner of the second", async () => {
  // Given an emergency stop the status showed, which the status carries behind go_silent.
  const { context, shown } = panel();
  await answer(context, { ...latchedStatus(60.3), counters: [1, 0, 39] });
  // And, at the next answer, the latched quick stop of a rule with no emergency stop held: the
  // first was acknowledged and the rule latched in between, unseen by this screen.
  await answer(context, ruleStopStatus(65.3, 96));
  assert.equal(shown("estop-banner"), true);
  // When go_silent replaces it in that console.
  await answer(context, silenceAfter(70.3, 166));
  // Then the banner stays: the latch last shown is not one the status carries.
  assert.equal(shown("estop-banner"), true);
});

test("a rule's quick stop the status shows again after a restart is the new console's own", async () => {
  // Given the latched quick stop of a rule shown by the status of a console that ran for a while.
  const { context, shown } = panel();
  await answer(context, ruleStopStatus(60.3, 5000));
  // And the same rule latched in the console started since: fewer snapshots published, which
  // proves a restart, but one that came before this latch.
  await answer(context, ruleStopStatus(75.3, 30));
  // When go_silent replaces it there.
  await answer(context, silenceAfter(80.3, 60));
  // Then the banner stays: it is the console that showed this latch that went silent.
  assert.equal(shown("estop-banner"), true);
});

test("the status that showed the latch gives the first count: the very next answer can prove the restart", async () => {
  // Given the latched quick stop of a rule shown by the status of a console that ran for a while.
  const { context, shown } = panel();
  await answer(context, ruleStopStatus(60.3, 5000));
  assert.equal(shown("estop-banner"), true);
  // When the next answer, under go_silent, shows far fewer snapshots published.
  await answer(context, silenceAfter(75.3, 40));
  // Then the console that wrote the first has been replaced, and the banner goes.
  assert.equal(shown("estop-banner"), false);
});

test("a status asked before the one that showed the latch proves nothing about what followed it", async () => {
  // Given a status request in flight when another, asked after it, shows the latched quick stop of a rule.
  const { context, shown } = panel();
  const early = deferred();
  context.api = () => early.promise;
  const asked = context.loadStatus();
  await answer(context, ruleStopStatus(60.3, 5000));
  assert.equal(shown("estop-banner"), true);
  // When the first one is answered at last, under go_silent, with fewer snapshots published.
  early.resolve(silenceAfter(61.1, 40));
  await asked;
  // Then the banner stays: only answers asked after the latch was shown count as proof.
  assert.equal(shown("estop-banner"), true);
  // And one asked after it proves the restart.
  await answer(context, silenceAfter(66.1, 65));
  assert.equal(shown("estop-banner"), false);
});

test("an emergency stop the status showed goes when the status under go_silent no longer carries it", async () => {
  // Given an emergency stop the status alone showed, then go_silent standing in front of it.
  const { context, shown } = panel();
  await answer(context, { ...latchedStatus(60.3), counters: [1, 0, 39] });
  await answer(context, { ...cameraStopBehindSilence(65.3), counters: [1, 0, 96] });
  assert.equal(shown("estop-banner"), true);
  // When a status under go_silent shows no emergency stop held behind it: the status carries that
  // latch itself, and a console cannot release it under go_silent, so it is another console.
  await answer(context, silenceAfter(70.3, 166));
  // Then the banner goes, with no other proof.
  assert.equal(shown("estop-banner"), false);
});

test("the banner of a rule's quick stop the status showed goes with a machine whose clock restarted", async () => {
  // Given the latched quick stop of a rule shown by the status, on a machine up for hours.
  const { context, shown } = panel();
  await answer(context, ruleStopStatus(20000.3, 500));
  assert.equal(shown("estop-banner"), true);
  // When the Pi reboots and its console goes silent: the next answer, asked once that one was in,
  // is written on a clock that started again, by a console that has published more snapshots.
  await answer(context, silenceAfter(33, 600));
  // Then the latch was shown before the reboot, and the status of the new console decides.
  assert.equal(shown("estop-banner"), false);
});

test("a restart of the clock seen before a latch the status showed does not speak for it", async () => {
  // Given a machine whose clock restarted, as this screen saw from two status answers.
  const { context, shown } = panel();
  await answer(context, status({ at: 20000, counters: [0, 0, 900] }));
  await answer(context, status({ at: 33, counters: [0, 0, 20] }));
  // And, after that, the latched quick stop of a rule shown by the status.
  await answer(context, ruleStopStatus(40.3, 60));
  assert.equal(shown("estop-banner"), true);
  // When go_silent replaces it in that console.
  await answer(context, silenceAfter(45.3, 110));
  // Then the banner stays: the restart came before the latch, and proves nothing about it.
  assert.equal(shown("estop-banner"), true);
});

test("a restart of the clock seen before a latch the frames showed does not speak for it", async () => {
  // Given a machine whose clock restarted, as this screen saw from its frames.
  const { context, frame, shown } = panel();
  frame(snapshot({ at: 20001 }));
  frame(snapshot({ at: 31 }));
  // And, after that, the latched quick stop of a rule shown by the frames.
  frame(snapshot({ at: 32, mode: "arret", ...verdict("quick_stop", "reverse_rotation") }));
  assert.equal(shown("estop-banner"), true);
  // When go_silent replaces it in that console, and its status is written under that go_silent.
  context.api = () => Promise.resolve(silenceAfter(33.1, 160));
  frame(snapshot({ at: 33, mode: "repos", ...silent() }));
  await settle();
  // Then the banner stays: the restart came before the latch, and proves nothing about it.
  assert.equal(shown("estop-banner"), true);
});

test("the same count of snapshots twice proves no restart: a latch the frames showed keeps its banner", async () => {
  // Given the latched quick stop of a rule, shown by the frames and by the status of that console.
  const { context, frame, shown } = panel();
  frame(snapshot({ at: 60.2, mode: "arret", ...verdict("quick_stop", "reverse_rotation") }));
  await answer(context, ruleStopStatus(60.3, 300));
  // When go_silent replaces it and the next status shows the very same count: two answers within
  // one tick, or a loop that has stalled.
  context.api = () => Promise.resolve(silenceAfter(61.1, 300));
  frame(snapshot({ at: 61, mode: "repos", ...silent() }));
  await settle();
  // Then nothing says the console restarted, and the banner stays.
  assert.equal(shown("estop-banner"), true);
});

test("the same count of snapshots twice proves no restart: a latch the status showed keeps its banner", async () => {
  const { context, shown } = panel();
  await answer(context, ruleStopStatus(60.3, 96));
  await answer(context, silenceAfter(60.35, 96));
  assert.equal(shown("estop-banner"), true);
});

test("a restart proven by one status still holds when the next shows the count rising again", async () => {
  // Given the latched quick stop of a rule shown by the frames, then a socket that drops on it.
  const page = panel();
  const { context, frame, shown } = page;
  frame(snapshot({ at: 60.2, mode: "arret", ...verdict("quick_stop", "reverse_rotation") }));
  await answer(context, ruleStopStatus(60.3, 5000));
  dropSocket(page);
  // And a status from a console started since, at rest: far fewer snapshots published. The last
  // frame still shows the latch, so the banner stays.
  await answer(context, status({ at: 70, counters: [0, 0, 40] }));
  assert.equal(shown("estop-banner"), true);
  // When the frames come back under the go_silent of that new console, whose count has gone on rising.
  context.state.connected = true;
  context.state.stream += 1;
  context.api = () => Promise.resolve(silenceAfter(75.1, 65));
  frame(snapshot({ at: 75, mode: "repos", ...silent() }));
  await settle();
  // Then the restart proven before still holds: the latch was another console's, and the banner goes.
  assert.equal(shown("estop-banner"), false);
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

// The page wired as on load, with no network and no timers behind it.
function wire(context) {
  context.document.querySelector = () => new Element();
  context.window.setInterval = () => undefined;
  context.window.requestAnimationFrame = () => undefined;
  context.boot = () => undefined;
  context.loadCamera = () => undefined;
  context.start();
}

test("what sticks below the banners is told the room they take", () => {
  // Given the page wired as on load, in a browser that reports element resizes.
  const { context, frame, node, published, screen, stack } = panel();
  const observers = [];
  context.window.ResizeObserver = class {
    constructor(callback) {
      this.callback = callback;
    }
    observe(target) {
      observers.push([target, this.callback]);
    }
  };
  wire(context);
  assert.equal(observers.length, 1);
  assert.equal(observers[0][0], node("banners"));
  frame(snapshot({ ...verdict("quick_stop", "operator_estop") }));
  const wide = stack();
  // When the banner wraps onto more lines with nothing shown or hidden, and the browser reports it.
  screen.columns = 20;
  assert.ok(stack() > wide);
  observers[0][1]();
  // Then the sidebar and the mobile bar are given the new height to start under.
  assert.equal(published.get("--banners-h"), stack() + "px");
});

test("without ResizeObserver the banner stack is still measured when a banner appears or goes", () => {
  // Given a browser with no ResizeObserver.
  const { frame, published, stack } = panel();
  // When the emergency-stop banner appears.
  frame(snapshot({ ...verdict("quick_stop", "operator_estop") }));
  // Then the height published for the sidebar and the mobile bar is that of the stack with it in.
  assert.ok(stack() > 0);
  assert.equal(published.get("--banners-h"), stack() + "px");
  // And again when it goes.
  frame(snapshot({ at: 51 }));
  assert.equal(published.get("--banners-h"), "0px");
});

test("without ResizeObserver the NO LIVE DATA banner is measured as it reads, its sentence included", () => {
  // Given a latched stop on screen, in a browser with no ResizeObserver, on a narrow screen.
  const { context, clock, frame, published, screen, shown, stack } = panel();
  screen.columns = 30;
  frame(snapshot({ ...verdict("quick_stop", "operator_estop") }));
  const one = stack();
  // When frames stop: NO LIVE DATA appears above it, with a sentence longer than the markup's.
  clock.now += 5000;
  context.refreshLiveness();
  // Then the height published is that of the two banners as they now read.
  assert.equal(shown("banner"), true);
  assert.ok(stack() > one);
  assert.equal(published.get("--banners-h"), stack() + "px");
  // And it follows the sentence when it changes while the banner stays up.
  const open = stack();
  context.state.connected = false;
  context.refreshLiveness();
  assert.notEqual(stack(), open);
  assert.equal(published.get("--banners-h"), stack() + "px");
});

test("without ResizeObserver the banner stack is measured again when the window is resized", () => {
  // Given the page wired as on load in a browser with no ResizeObserver, a banner on screen.
  const { context, fire, frame, published, screen, stack } = panel();
  wire(context);
  frame(snapshot({ ...verdict("quick_stop", "operator_estop") }));
  const wide = stack();
  // When the window narrows and the banner wraps onto more lines.
  screen.columns = 20;
  fire("resize");
  // Then the new height is published.
  assert.ok(stack() > wide);
  assert.equal(published.get("--banners-h"), stack() + "px");
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

/* ======================= what the loop made of a manual target ======================= */

const RATIO = 49.79;

// One speed as the API renders it, from the whole motor rpm the machine counts in.
const turning = (motor) => ({ motor_rpm: motor, output_rpm: motor / RATIO, hertz: motor / 27.6, g_load: 0, resultant_g: 1 });

function manualRow(target = 0, overrides = {}) {
  return {
    occupancy: "bench",
    occupancy_label: "BANC - personne a bord : NON",
    target: turning(target),
    ceiling: turning(300),
    min_run: turning(55),
    ramping: false,
    ramp_eta_s: null,
    ...overrides,
  };
}

// A manual session as one frame shows it, the applied target in motor rpm.
const manualFrame = (at, target = 0, overrides = {}) =>
  snapshot({ at, mode: "manuel", phase: "hold", manual: manualRow(target), ...overrides });

// What the loop publishes when it refuses a command, or takes a target back (no operator then).
const refusal = (at, detail, operator = "op") => ({ kind: "refused", at, wall_clock: 0, operator, detail });

const HELD = "consigne refusee : le verdict hr_stale tient le bras a l'arret. Attendre qu'il soit leve, puis redonner la cible";
const TAKEN_BACK =
  "cible de 249 tr/min moteur remise a 0 : pas de frequence cardiaque utilisable, rien ne monte depuis l'arret. " +
  "Attendre une frequence cardiaque fiable, puis redonner la cible";

// The operator composes `rpm` and presses "Appliquer"; the console takes the target into its mailbox at `at`.
async function apply(context, rpm, at) {
  context.state.manualDraft = rpm;
  context.api = (path, options) =>
    path === "/api/manual/target"
      ? Promise.resolve({ kind: "manual_target", operator: "op", detail: options.body.output_rpm.toFixed(2) + " output rpm", at })
      : new Promise(() => undefined);
  context.doManualApply();
  await settle();
}

test("the 202 of Appliquer says the target is sent, not that the machine is on its way", async () => {
  // Given a manual session at standstill, and a target the console has only put in its mailbox.
  const { context, frame, node } = panel();
  frame(manualFrame(59.8));
  await apply(context, 5, 60);
  // Then the note claims nothing the loop has not said yet.
  assert.equal(node("manual-note").textContent, "cible envoyee : 5.00 output rpm - pas encore prise par la machine");
  assert.equal(node("manual-note").classes.has("note-bad"), false);
  // And a frame taken before the command changes nothing: it cannot speak about it.
  frame(manualFrame(59.9));
  assert.equal(node("manual-note").textContent, "cible envoyee : 5.00 output rpm - pas encore prise par la machine");
});

test("a target the loop refuses turns the note into that refusal", async () => {
  // Given a target sent over an arm that a verdict holds at standstill.
  const { context, frame, node } = panel();
  frame(manualFrame(59.8, 0, verdict("freeze", "hr_stale", false)));
  await apply(context, 5, 60);
  // When the loop refuses it on its next tick, as an event.
  context.addEvent(refusal(60.1, HELD));
  // Then the note is the refusal, in red, word for word, and no longer says "sent".
  const note = node("manual-note");
  assert.ok(note.textContent.includes(HELD), note.textContent);
  assert.ok(note.textContent.startsWith("refus de la machine"), note.textContent);
  assert.equal(note.textContent.includes("cible envoyee"), false);
  assert.ok(note.classes.has("note-bad"));
  // And the frames that follow, target still at 0, leave it there.
  for (let tick = 1; tick <= 10; tick += 1) {
    frame(manualFrame(60 + tick * 0.2, 0, verdict("freeze", "hr_stale", false)));
  }
  assert.ok(note.textContent.includes(HELD), note.textContent);
});

test("a refusal that reaches the screen before the 202 is not overwritten by it", async () => {
  // Given a refusal the socket delivered first: it is stamped after the command it answers.
  const { context, frame, node } = panel();
  frame(manualFrame(59.8));
  context.addEvent(refusal(60.1, HELD));
  // When the 202 of that command arrives.
  await apply(context, 5, 60);
  // Then the note still says refused, and nothing is awaited any more.
  assert.ok(node("manual-note").textContent.includes(HELD), node("manual-note").textContent);
  assert.equal(context.state.manualSent, null);
  // And a later target is not taken for answered by that older refusal.
  await apply(context, 4, 70);
  assert.equal(node("manual-note").textContent, "cible envoyee : 4.00 output rpm - pas encore prise par la machine");
});

test("the note says the machine took the target once a frame shows it applied", async () => {
  // Given a target sent: 5.00 tr/min at the arm is 249 tr/min at the motor.
  const { context, frame, node } = panel();
  frame(manualFrame(59.8));
  await apply(context, 5, 60);
  // When the first frame taken after the command shows that target applied.
  frame(manualFrame(60.2, 249));
  // Then the note says so, from the machine's own report.
  assert.equal(node("manual-note").textContent, "cible prise par la machine : 5.00 output rpm (suivie aux limites de mouvement)");
  assert.equal(node("manual-note").classes.has("note-bad"), false);
  assert.equal(context.state.manualSent, null);
});

test("the note stops saying the machine follows a target once the machine has dropped it", async () => {
  // Given a target the machine took, the arm on its way to it.
  const { context, frame, node } = panel();
  frame(manualFrame(59.8));
  await apply(context, 5, 60);
  frame(manualFrame(60.2, 249, { setpoint: turning(60), measured: turning(58) }));
  assert.ok(node("manual-note").textContent.startsWith("cible prise par la machine"));
  // While the machine holds it, the note stays.
  frame(manualFrame(62, 249, { setpoint: turning(120), measured: turning(118) }));
  assert.ok(node("manual-note").textContent.startsWith("cible prise par la machine"));
  // When the operator presses STOP: the target is 0 from the next frame, the arm still turning.
  frame(manualFrame(62.2, 0, { mode: "arret", phase: "cooldown", setpoint: turning(119), measured: turning(120) }));
  // Then the card no longer says that the machine took 5 tr/min and follows it.
  assert.equal(node("manual-note").textContent, "");
});

test("a target taken while a verdict holds the setpoint is not said to be followed", async () => {
  // Given an arm turning at 120 tr/min moteur under a warning that holds its speed.
  const { context, frame, node } = panel();
  const held = verdict("freeze", "attendant_absent", false);
  const at120 = { setpoint: turning(120), measured: turning(120) };
  frame(manualFrame(59.8, 120, { ...at120, ...held }));
  await apply(context, 5, 60);
  // When the machine takes the higher target and the freeze keeps the setpoint where it is.
  frame(manualFrame(60.2, 249, { ...at120, ...held }));
  // Then the note says taken, and does not say followed.
  assert.equal(node("manual-note").textContent, "cible prise par la machine : 5.00 output rpm");
  assert.equal(node("manual-note").classes.has("note-bad"), false);
  // A reduce walks the setpoint down whatever the target is: not followed either.
  frame(manualFrame(60.4, 249, { setpoint: turning(110), measured: turning(112), ...verdict("reduce", "hr_stale", false) }));
  assert.equal(node("manual-note").textContent, "cible prise par la machine : 5.00 output rpm");
  // When the warning lifts, the setpoint walks to the target again, and the note says so.
  frame(manualFrame(60.6, 249, { setpoint: turning(112), measured: turning(112) }));
  assert.equal(node("manual-note").textContent, "cible prise par la machine : 5.00 output rpm (suivie aux limites de mouvement)");
  // And the words come off again the moment a verdict holds the setpoint, latched or not.
  frame(manualFrame(60.8, 249, { setpoint: turning(118), measured: turning(116), ...verdict("freeze", "loop_stall", true) }));
  assert.equal(node("manual-note").textContent, "cible prise par la machine : 5.00 output rpm");
});

test("under a freeze a target of 0 is a stop asked for, and the note says it is followed", async () => {
  // Given an arm at 249 tr/min moteur under a freeze, and the operator's own zero.
  const { context, frame, node } = panel();
  const held = verdict("freeze", "attendant_absent", false);
  frame(manualFrame(59.8, 249, { setpoint: turning(249), measured: turning(249), ...held }));
  await apply(context, 0, 60);
  // When the machine takes it: under a freeze a stop asked for is walked down all the same.
  frame(manualFrame(60.2, 0, { setpoint: turning(240), measured: turning(246), ...held }));
  // Then, of all targets under a freeze, this one is said followed.
  assert.equal(node("manual-note").textContent, "cible prise par la machine : 0.00 output rpm (suivie aux limites de mouvement)");
});

test("wiping a taken note leaves alone what was written there since", async () => {
  const { context, frame, node } = panel();
  frame(manualFrame(59.8));
  await apply(context, 5, 60);
  frame(manualFrame(60.2, 249));
  // The route refuses a later click: its answer replaces the note.
  context.api = () => Promise.reject(new Error("the machine is running, not idle"));
  context.doManualApply();
  await settle();
  // When the machine then drops the target, that answer is not the note to wipe.
  frame(manualFrame(62.2, 0, { mode: "arret", phase: "cooldown" }));
  assert.equal(node("manual-note").textContent, "the machine is running, not idle");
});

test("an applied target one motor rpm away from the one sent is not read as taken", async () => {
  // Given a turning arm holding 249 tr/min moteur, and 5.02 tr/min sent (250 at the motor).
  const { context, frame, node } = panel();
  frame(manualFrame(59.8, 249));
  await apply(context, 5.02, 60);
  // When the frames keep showing 249: the new target was not taken.
  frame(manualFrame(60.2, 249));
  // Then the note does not say it was.
  assert.equal(node("manual-note").textContent.includes("prise par la machine :"), false, node("manual-note").textContent);
  frame(manualFrame(60.4, 250));
  assert.ok(node("manual-note").textContent.startsWith("cible prise par la machine"), node("manual-note").textContent);
});

test("a target the machine takes back is written in the note of every screen showing the session", async () => {
  for (const clicked of [true, false]) {
    // Given a target the machine took, on the screen that sent it and on one that did not.
    const { context, frame, node } = panel();
    frame(manualFrame(59.8));
    if (clicked) await apply(context, 5, 60);
    frame(manualFrame(60.2, 249));
    // When the machine puts it back to 0 before the first step: nobody's command, no operator.
    context.addEvent(refusal(60.4, TAKEN_BACK, ""));
    frame(manualFrame(60.4));
    // Then both notes say so, in red.
    const note = node("manual-note");
    assert.ok(note.textContent.includes(TAKEN_BACK), `clicked=${clicked}: ${note.textContent}`);
    assert.ok(note.classes.has("note-bad"));
  }
});

test("with no word from the loop for a second of its clock, the note says the target was not taken", async () => {
  // Given a target sent, and a refusal that never reached this screen (its socket reconnected).
  const { context, frame, node } = panel();
  frame(manualFrame(59.8));
  await apply(context, 5, 60);
  // While the loop may still answer, the note waits.
  frame(manualFrame(60.8));
  assert.equal(node("manual-note").textContent, "cible envoyee : 5.00 output rpm - pas encore prise par la machine");
  // When more than a second of the machine's clock has passed and the applied target is still 0.
  frame(manualFrame(61.2));
  // Then the note stops waiting, and says what the machine holds instead.
  const note = node("manual-note");
  assert.ok(note.textContent.startsWith("cible NON prise par la machine"), note.textContent);
  assert.ok(note.textContent.includes("0.00 tr/min de sortie"), note.textContent);
  assert.ok(note.classes.has("note-bad"));
});

test("a target still unanswered when the session is over is said not taken", async () => {
  const { context, frame, node } = panel();
  frame(manualFrame(59.8));
  await apply(context, 5, 60);
  // When the machine is back at rest with no manual session, a second later.
  frame(snapshot({ at: 61.2 }));
  assert.equal(node("manual-note").textContent, "cible NON prise par la machine : la seance manuelle est terminee.");
  assert.ok(node("manual-note").classes.has("note-bad"));
});

test("a refusal that is not about a manual target is left to the event list", () => {
  // Given a machine at rest: no manual session on screen, no target awaited.
  const { context, frame, node } = panel();
  frame(snapshot());
  // When the loop refuses a programme somebody asked for.
  context.addEvent(refusal(51, "demarrage refuse : age du passager requis pour une seance programmee"));
  // Then the manual card says nothing about it, and the event is listed as before.
  assert.equal(node("manual-note").textContent, "");
  assert.equal(node("events").children.length, 1);
});

// What the dashboard link publishes when it turns something down (ANH-133): nobody's command, and
// never about a manual target. It has its own kind for that reason.
const dashboardNews = (at, detail) => ({ kind: "dashboard", at, wall_clock: 0, operator: "", detail });

const INCOMPATIBLE = "serveur incompatible (contrat 1.0 vs inconnu)";

test("news from the dashboard link is listed and never read as the machine's answer about a target", async () => {
  // Given a manual session on screen and a target just sent, the loop's answer still awaited.
  const { context, frame, node } = panel();
  frame(manualFrame(59.8));
  await apply(context, 5, 60);
  // When the link says, in that same instant, that the dashboard is of another contract.
  context.addEvent(dashboardNews(60.1, INCOMPATIBLE));
  // Then the manual card still waits for the machine's own answer, and claims no refusal.
  const note = node("manual-note");
  assert.equal(note.textContent, "cible envoyee : 5.00 output rpm - pas encore prise par la machine");
  assert.equal(note.classes.has("note-bad"), false);
  assert.notEqual(context.state.manualSent, null);
  // And the frame that shows the target applied is still read as the machine taking it.
  frame(manualFrame(60.2, 249));
  assert.ok(note.textContent.startsWith("cible prise par la machine"), note.textContent);
  // The news itself is in the event list, under its own kind.
  const listed = node("events").children;
  assert.equal(listed.length, 1);
  assert.ok(listed[0].textContent.includes("dashboard  " + INCOMPATIBLE), listed[0].textContent);
  assert.ok(listed[0].classes.has("event-dashboard"));
});

test("the same words under the kind `refused` would be taken for the machine's answer", async () => {
  // The witness for the test above: this is what the page does with a `refused` event, and why
  // the link does not publish one.
  const { context, frame, node } = panel();
  frame(manualFrame(59.8));
  await apply(context, 5, 60);
  context.addEvent(refusal(60.1, INCOMPATIBLE, ""));
  assert.ok(node("manual-note").textContent.startsWith("refus de la machine"), node("manual-note").textContent);
  assert.equal(context.state.manualSent, null);
});

test("an Appliquer the console refuses outright shows that answer and awaits nothing", async () => {
  const { context, frame, node } = panel();
  frame(manualFrame(59.8));
  await apply(context, 5, 60);
  // When the next click is refused by the route itself (409).
  context.api = () => Promise.reject(new Error("the machine is already stopping"));
  context.doManualApply();
  await settle();
  // Then the note is that answer, and no frame rewrites it with the fate of the earlier target.
  assert.equal(node("manual-note").textContent, "the machine is already stopping");
  frame(manualFrame(60.2, 249));
  assert.equal(node("manual-note").textContent, "the machine is already stopping");
});

/* ================== what the heart rate holds, before a target is typed ================== */

// A manual session with a person declared on board, which the page itself cannot start yet.
const riderFrame = (at, target = 0, overrides = {}) =>
  manualFrame(at, target, {
    manual: manualRow(target, { occupancy: "occupied", occupancy_label: "PERSONNE A BORD" }),
    ...overrides,
  });

const HOLD_WORDS = {
  no_heart_rate: "pas de frequence cardiaque utilisable",
  trend_unknown: "tendance de la frequence cardiaque pas encore connue",
  heart_rate_falling: "la frequence cardiaque baisse trop vite",
};

for (const [hold, words] of Object.entries(HOLD_WORDS)) {
  test(`a rise held by the heart rate (${hold}) is shown on the manual card before any target is typed`, () => {
    // Given a person on board, an arm at standstill, no verdict, and nothing typed or sent.
    const { context, frame, node, shown } = panel();
    const asked = [];
    context.api = (path) => {
      asked.push(path);
      return new Promise(() => undefined);
    };
    frame(riderFrame(60));
    context.renderPanel(panelRow());
    assert.equal(shown("manual-hold"), false);
    // When the console reports that the heart rate holds a rise.
    context.renderPanel(panelRow({ manual_rise_hold: hold }));
    // Then the card says so, in French, with what it means for a target.
    assert.equal(shown("manual-hold"), true);
    assert.equal(node("manual-hold-title").textContent, "MONTEE RETENUE PAR LA FREQUENCE CARDIAQUE");
    assert.ok(node("manual-hold-detail").textContent.startsWith(words + "."), node("manual-hold-detail").textContent);
    assert.ok(node("manual-hold-detail").textContent.includes("une cible non nulle est refusee"));
    assert.ok(node("manual-hold-detail").textContent.includes("remonte seule"));
    assert.deepEqual(asked, [], "the hold was learnt from a request, not shown before one");
    // And it stays over the frames that follow, then goes when the console says nothing holds.
    frame(riderFrame(60.2));
    assert.equal(shown("manual-hold"), true);
    context.renderPanel(panelRow());
    assert.equal(shown("manual-hold"), false);
  });
}

test("a hold the page has no words for is shown by its name rather than hidden", () => {
  const { context, frame, node, shown } = panel();
  frame(riderFrame(60));
  context.renderPanel(panelRow({ manual_rise_hold: "a_hold_added_later" }));
  assert.equal(shown("manual-hold"), true);
  assert.ok(node("manual-hold-detail").textContent.startsWith("a_hold_added_later."));
});

test("the hold is shown only while a manual session can take a target", () => {
  const { context, frame, shown } = panel();
  context.renderPanel(panelRow({ manual_rise_hold: "no_heart_rate" }));
  // At rest there is no target to type, whatever the last session left behind.
  frame(snapshot({ at: 60, manual: manualRow(0, { occupancy: "occupied" }) }));
  assert.equal(shown("manual-hold"), false);
  // In a session it is shown.
  frame(riderFrame(60.2));
  assert.equal(shown("manual-hold"), true);
  // Once the session is ending every target is refused anyway: the card stops announcing a hold.
  frame(riderFrame(60.4, 0, { mode: "arret" }));
  assert.equal(shown("manual-hold"), false);
});

test("with a person on board, a console that stops answering is shown as an unknown hold", () => {
  // Given a session with a person on board, and a console that reports no hold.
  const { context, clock, frame, node, shown } = panel();
  frame(riderFrame(60));
  context.renderPanel(panelRow());
  assert.equal(shown("manual-hold"), false);
  // When /api/panel has not answered for longer than its answers stay current, frames still coming.
  for (let tick = 1; tick <= 20; tick += 1) {
    frame(riderFrame(60 + tick * 0.2));
  }
  assert.ok(clock.now - context.state.panelAt > 3500);
  // Then the card does not go on showing "nothing holds": it says it no longer knows.
  assert.equal(shown("manual-hold"), true);
  assert.equal(node("manual-hold-title").textContent, "RETENUE PAR LA FREQUENCE CARDIAQUE : INCONNUE");
  // And the next answer puts the indicator back to what the console says.
  context.renderPanel(panelRow());
  assert.equal(shown("manual-hold"), false);
});

test("while the heart rate holds a rise, a higher target taken is not said to be followed", async () => {
  // Given a person on board, an arm turning at 120 tr/min moteur, and the console saying the rise is held.
  const { context, frame, node } = panel();
  const at120 = { setpoint: turning(120), measured: turning(120) };
  frame(riderFrame(59.6, 120, at120));
  context.renderPanel(panelRow({ manual_rise_hold: "heart_rate_falling" }));
  // When a higher target is taken: no verdict stands, and yet the speed does not climb.
  await apply(context, 5, 60);
  frame(riderFrame(60.2, 249, at120));
  assert.equal(node("manual-note").textContent, "cible prise par la machine : 5.00 output rpm");
  // A lower target is a descent, and the heart rate holds no descent: followed.
  await apply(context, 2, 61);
  frame(riderFrame(61.2, 100, at120));
  assert.equal(node("manual-note").textContent, "cible prise par la machine : 2.00 output rpm (suivie aux limites de mouvement)");
  // Once the console says nothing holds any more, the rise is followed, and the note says so.
  await apply(context, 5, 62);
  frame(riderFrame(62.2, 249, at120));
  assert.equal(node("manual-note").textContent, "cible prise par la machine : 5.00 output rpm");
  context.renderPanel(panelRow());
  frame(riderFrame(62.4, 249, at120));
  assert.equal(node("manual-note").textContent, "cible prise par la machine : 5.00 output rpm (suivie aux limites de mouvement)");
});

test("with nobody on board the heart rate holds nothing, answered or not", () => {
  const { context, frame, shown } = panel();
  frame(manualFrame(60));
  context.renderPanel(panelRow());
  for (let tick = 1; tick <= 20; tick += 1) {
    frame(manualFrame(60 + tick * 0.2));
  }
  assert.equal(shown("manual-hold"), false);
});

/* ============ a stop the camera latched, on a page opened once go_silent stands ============ */

test("a page opened once go_silent stands announces the stop the camera latched behind it", async () => {
  // Given a page just opened: it never saw the stop latched, and every frame it gets says go_silent.
  const { context, frame, shown } = panel();
  context.api = () => Promise.resolve(cameraStopBehindSilence(70.1));
  frame(snapshot({ at: 70, mode: "arret", ...silent() }));
  await settle();
  // Then the banner is up, from the status alone, and the frames that follow do not take it down.
  assert.equal(shown("estop-banner"), true);
  for (let tick = 1; tick <= 5; tick += 1) {
    frame(snapshot({ at: 70 + tick * 0.2, mode: "arret", ...silent() }));
    assert.equal(shown("estop-banner"), true, `hidden after ${tick} frames`);
  }
});

test("go_silent with no stop latched behind it raises no emergency-stop banner", async () => {
  const { context, frame, shown } = panel();
  context.api = () => Promise.resolve(silenceAlone(70.1));
  frame(snapshot({ at: 70, mode: "arret", ...silent() }));
  await settle();
  assert.equal(shown("estop-banner"), false);
});

for (const id of ["verdicts-grid", "system-grid"]) {
  test(`#${id} reports the e-stop latched when go_silent hides the stop the camera latched`, async () => {
    const { context, node } = panel();
    context.api = () => Promise.resolve(cameraStopBehindSilence());
    await context.loadStatus();
    // The standing verdict no longer names it: only the supervisor's slot does.
    assert.equal(rows(node(id))["verdict retenu"].textContent, "comms_lost / go_silent");
    assert.equal(rows(node(id))["e-stop verrouille"].textContent, "OUI");
  });
}

test("the state chip is red for a latched emergency stop whoever latched it", async () => {
  const { context, node } = panel();
  // A stop latched by the camera leaves the interface idle: the chip read "idle", in grey.
  context.api = () =>
    Promise.resolve(
      status({ supervisor_estop: verdict("quick_stop", "operator_estop").safety, standing: verdict("quick_stop", "operator_estop").safety }),
    );
  await context.loadStatus();
  assert.equal(node("run-state").textContent, "idle");
  assert.ok(node("run-state").classes.has("pill-bad"));
  // Behind go_silent as well.
  context.api = () => Promise.resolve(cameraStopBehindSilence());
  await context.loadStatus();
  assert.ok(node("run-state").classes.has("pill-bad"));
  // And grey again once nothing is latched.
  context.api = () => Promise.resolve(status());
  await context.loadStatus();
  assert.equal(node("run-state").classes.has("pill-bad"), false);
});

/* ======================= an E-STOP the console does not answer ======================= */

// The operator presses E-STOP and nothing comes back: the request stays out until the test answers it.
function pressUnanswered(context) {
  const request = deferred();
  const sent = [];
  context.api = (path) => {
    sent.push(path);
    return path === "/api/session/estop" ? request.promise : new Promise(() => undefined);
  };
  context.doEstop();
  return { request, sent };
}

test("an E-STOP the console does not answer is said so after two seconds, with the wired stop named", async () => {
  // Given a live page, and a console that takes the request and answers nothing.
  const { context, frame, announced, published, stack, wait } = panel();
  frame(snapshot({ at: 59.9, mode: "manuel", phase: "hold" }));
  const { sent } = pressUnanswered(context);
  await settle();
  assert.deepEqual(sent, ["/api/session/estop"], "the request did not leave on the first click");
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), false);
  // While an answer can still be on its way, the page does not cry wolf.
  wait(1900);
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), false);
  // When two seconds have gone by with nothing back.
  wait(100);
  // Then a banner says the stop is not confirmed, for how long, and what to use instead.
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), true);
  assert.equal(announced("aucune reponse de la console depuis 2 s"), true);
  assert.equal(announced("UTILISEZ L'ARRET CABLE"), true);
  assert.equal(published.get("--banners-h"), stack() + "px");
  // And it claims no latch: the banner of a latched stop is not up.
  assert.equal(announced("ARRET D'URGENCE VERROUILLE"), false);
});

test("the notice of an unanswered E-STOP keeps counting, frames or no frames, and outlives further clicks", async () => {
  const { context, clock, frame, announced, wait } = panel();
  frame(snapshot({ at: 59.9 }));
  pressUnanswered(context);
  wait(2000);
  assert.equal(announced("depuis 2 s"), true);
  // A second click, also unanswered, must not make the page look reassured for two more seconds.
  context.doEstop();
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), true);
  // Live frames with no stop in them keep it up and current.
  for (let tick = 1; tick <= 10; tick += 1) {
    frame(snapshot({ at: 60 + tick * 0.2 }));
  }
  assert.equal(announced("depuis 4 s"), true);
  // So does the liveness check alone, once the frames have stopped as well.
  clock.now += 3000;
  context.refreshLiveness();
  assert.equal(announced("depuis 7 s"), true);
  assert.equal(announced("NO LIVE DATA"), true);
});

test("the notice appears even in a browser whose timer never fires", () => {
  // Given a page whose one-shot timer is lost; its periodic liveness check still runs.
  const { context, clock, announced } = panel();
  pressUnanswered(context);
  clock.now += 2500;
  context.refreshLiveness();
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), true);
});

test("a late answer takes the unanswered notice down and raises the banner of the latched stop", async () => {
  const { context, frame, announced, wait } = panel();
  frame(snapshot({ at: 59.9 }));
  const { request } = pressUnanswered(context);
  wait(2000);
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), true);
  // When the request gets through after all.
  request.resolve(receipt);
  await settle();
  // Then the stop is known latched, and said so.
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), false);
  assert.equal(announced("ARRET D'URGENCE VERROUILLE"), true);
  // And the timers still pending from the clicks bring nothing back.
  wait(5000);
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), false);
});

test("a frame that shows a stop latched ends the doubt; one under go_silent does not", async () => {
  const { context, frame, announced, wait } = panel();
  frame(snapshot({ at: 59.9 }));
  pressUnanswered(context);
  wait(2000);
  // go_silent hides whatever is latched behind it: the notice stays.
  frame(snapshot({ at: 62.2, mode: "arret", ...silent() }));
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), true);
  // A latched quick stop, whichever way it came, is the stop the click was asking for.
  frame(snapshot({ at: 62.4, mode: "arret", ...verdict("quick_stop", "operator_estop") }));
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), false);
  assert.equal(announced("ARRET D'URGENCE VERROUILLE"), true);
});

test("a status answer that shows the stop latched ends the doubt as well", async () => {
  // Given frames that have stopped and an E-STOP left unanswered.
  const { context, clock, frame, announced, wait } = panel();
  frame(snapshot({ at: 59.9 }));
  pressUnanswered(context);
  wait(2000);
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), true);
  // When HTTP answers a status in which the stop is latched.
  context.api = () => Promise.resolve(status({ at: 62, run_state: "stopping", estop_latched: true }));
  await context.loadStatus();
  clock.now += 100;
  context.refreshLiveness();
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), false);
  assert.equal(announced("ARRET D'URGENCE VERROUILLE"), true);
});

test("a failed E-STOP request is said at once and stays on screen after its alert", async () => {
  // Given a console that cannot be reached at all.
  const { context, frame, announced, wait } = panel();
  const alerts = [];
  context.window.alert = (message) => alerts.push(message);
  frame(snapshot({ at: 59.9 }));
  context.api = () => Promise.reject(new Error("Failed to fetch"));
  context.doEstop();
  await settle();
  // Then the alert is raised as before, and the banner stays once it is dismissed.
  assert.equal(alerts.length, 1);
  assert.ok(alerts[0].includes("UTILISEZ L'ARRET CABLE"));
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), true);
  assert.equal(announced("Failed to fetch - UTILISEZ L'ARRET CABLE"), true);
  for (let tick = 1; tick <= 10; tick += 1) {
    frame(snapshot({ at: 60 + tick * 0.2 }));
  }
  wait(3000);
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), true);
  assert.equal(announced("Failed to fetch - UTILISEZ L'ARRET CABLE"), true);
});

/* ------ EX-2, a failed E-STOP request: what the banner says is what is true of it ------ */

test("a failed E-STOP request is said to have failed, how long ago, and to be pending no more", async () => {
  // Given a console that cannot be reached, and an E-STOP that failed.
  const { context, clock, frame, announced, published, stack, wait } = panel();
  context.window.alert = () => undefined;
  frame(snapshot({ at: 59.9, mode: "manuel", phase: "hold" }));
  context.api = () => Promise.reject(new Error("Failed to fetch"));
  context.doEstop();
  await settle();
  // Then the banner says so, in full: the request is over, nothing is waiting for an answer.
  assert.equal(
    announced("la demande a echoue il y a 0 s et n'est plus en attente : Failed to fetch - UTILISEZ L'ARRET CABLE"),
    true,
  );
  assert.equal(announced("aucune reponse de la console"), false);
  // And it does not go on reading as the news of this instant: its age is kept current.
  wait(4000);
  context.refreshLiveness();
  assert.equal(announced("la demande a echoue il y a 4 s et n'est plus en attente"), true);
  assert.equal(published.get("--banners-h"), stack() + "px");
  clock.now += 176000;
  context.refreshLiveness();
  assert.equal(announced("la demande a echoue il y a 3 min et n'est plus en attente"), true);
  // It claims no latch, and frames with no stop in them do not take it down.
  frame(snapshot({ at: 240, mode: "manuel", phase: "hold" }));
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), true);
  assert.equal(announced("ARRET D'URGENCE VERROUILLE"), false);
});

test("an E-STOP asked again after a failure says that a request is out again, and that the other failed", async () => {
  // Given an E-STOP that failed, then a second click the console does not answer.
  const { context, frame, announced, wait } = panel();
  context.window.alert = () => undefined;
  frame(snapshot({ at: 59.9 }));
  context.api = () => Promise.reject(new Error("Failed to fetch"));
  context.doEstop();
  await settle();
  wait(5000);
  const { request } = pressUnanswered(context);
  await settle();
  // Then the banner does not wait two seconds to say the new one is out, nor forget the failure.
  assert.equal(
    announced("nouvelle demande sans reponse depuis 0 s, la precedente a echoue : Failed to fetch - UTILISEZ L'ARRET CABLE"),
    true,
  );
  assert.equal(announced("n'est plus en attente"), false);
  wait(3000);
  context.refreshLiveness();
  assert.equal(announced("nouvelle demande sans reponse depuis 3 s"), true);
  // And when that one fails as well, nothing is said to be pending any more.
  request.reject(new Error("Load failed"));
  await settle();
  assert.equal(announced("la demande a echoue il y a 0 s et n'est plus en attente : Load failed"), true);
});

test("an E-STOP that gets through after a failure takes the notice down and raises the latched banner", async () => {
  const { context, frame, announced } = panel();
  context.window.alert = () => undefined;
  frame(snapshot({ at: 59.9 }));
  context.api = () => Promise.reject(new Error("Failed to fetch"));
  context.doEstop();
  await settle();
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), true);
  await pressEstop(context);
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), false);
  assert.equal(announced("ARRET D'URGENCE VERROUILLE"), true);
});

test("the failure of a request left behind says nothing about a doubt opened since", async () => {
  // Given an E-STOP left unanswered, then a frame showing a stop latched: that doubt is over.
  const { context, frame, announced, wait } = panel();
  context.window.alert = () => undefined;
  frame(snapshot({ at: 59.9 }));
  const old = pressUnanswered(context);
  wait(2000);
  frame(snapshot({ at: 62.4, mode: "arret", ...verdict("quick_stop", "operator_estop") }));
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), false);
  // When the request left behind fails at last: nothing is in doubt, the stop is latched and shown so.
  old.request.reject(new Error("Failed to fetch"));
  await settle();
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), false);
  assert.equal(announced("ARRET D'URGENCE VERROUILLE"), true);
  // And when it fails after a later click of its own, once the stop was acknowledged.
  const earlier = pressUnanswered(context);
  frame(snapshot({ at: 70, mode: "arret", ...verdict("quick_stop", "operator_estop") }));
  frame(snapshot({ at: 80 }));
  pressUnanswered(context);
  wait(2000);
  earlier.request.reject(new Error("Failed to fetch"));
  await settle();
  // Then the notice of that later click still says its request is out: it is, and it has not failed.
  assert.equal(announced("aucune reponse de la console depuis 2 s - UTILISEZ L'ARRET CABLE"), true);
});

test("a status asked before the click does not end the doubt of an unanswered E-STOP", async () => {
  // Given a stop latched from another screen, a status asked while it is latched and slow to answer,
  // then that stop acknowledged: nothing is latched any more.
  const { context, clock, frame, announced, wait } = panel();
  frame(snapshot({ at: 59.9, mode: "manuel", phase: "hold" }));
  const late = deferred();
  context.api = () => late.promise;
  const periodic = context.loadStatus();
  // When the operator presses E-STOP, the console does not answer, and the old status arrives.
  pressUnanswered(context);
  wait(2000);
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), true);
  late.resolve(latchedStatus(55));
  await periodic;
  clock.now += 100;
  context.refreshLiveness();
  // Then the doubt stands: that answer was written before the click and proves nothing about it.
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), true);
  // A status asked after the click, showing a stop latched, does end it.
  context.api = () => Promise.resolve(latchedStatus(63));
  await context.loadStatus();
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), false);
});

test("an E-STOP answered in time never shows the unanswered notice", async () => {
  const { context, frame, announced, wait } = panel();
  frame(snapshot({ at: 59.9 }));
  await pressEstop(context);
  assert.equal(announced("ARRET D'URGENCE VERROUILLE"), true);
  wait(5000);
  context.refreshLiveness();
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), false);
});

/* ================== EX-3, a STOP the console does not answer ================== */

// A manual session under way at 200 tr/min moteur, as one frame shows it.
const turningFrame = (at, overrides = {}) =>
  snapshot({ at, mode: "manuel", phase: "hold", setpoint: turning(200), measured: turning(200), ...overrides });

// The operator presses STOP, and the request leaves through the page's own request code: `request`
// is what the network then does with it, an answer of the console or none, when the test says so.
function pressStop({ context, send }) {
  const request = deferred();
  const sent = [];
  const alerts = [];
  context.window.alert = (message) => alerts.push(message);
  context.api = send;
  context.fetch = (path) => {
    sent.push(path);
    return path === "/api/session/stop" ? request.promise : new Promise(() => undefined);
  };
  context.doStop();
  return { request, sent, alerts };
}

// An answer of the console as the browser hands it over: accepted with a body, or refused with a reason.
const answered = (code, body) => ({
  ok: code < 400,
  status: code,
  statusText: "",
  json: () => Promise.resolve(body),
});
const stopTaken = (at) => answered(202, { kind: "end", detail: "", at });

test("a STOP the console does not answer is said so after two seconds, with the stops that remain named", async () => {
  // Given a session under way, and a console that takes the request and answers nothing.
  const page = panel();
  const { context, frame, announced, published, stack, wait } = page;
  frame(turningFrame(59.9));
  const { sent } = pressStop(page);
  await settle();
  assert.deepEqual(sent, ["/api/session/stop"], "the request did not leave on the first click");
  // While an answer can still be on its way, the page does not cry wolf.
  wait(1900);
  assert.equal(announced("STOP NON CONFIRME"), false);
  // When two seconds have gone by with nothing back.
  wait(100);
  // Then a banner says the stop is not confirmed, for how long, and what remains.
  assert.equal(announced("STOP NON CONFIRME"), true);
  assert.equal(
    announced("aucune reponse de la console depuis 2 s - rien ne dit que la seance s'arrete : E-STOP, ou l'arret cable"),
    true,
  );
  assert.equal(published.get("--banners-h"), stack() + "px");
  // It keeps counting while the session runs on, and a second click does not start the count again.
  context.doStop();
  for (let tick = 1; tick <= 10; tick += 1) {
    frame(turningFrame(60 + tick * 0.2));
  }
  assert.equal(announced("aucune reponse de la console depuis 4 s"), true);
  // And it says nothing of an emergency stop: none was asked for, none is latched.
  assert.equal(announced("ARRET D'URGENCE"), false);
});

test("the notice of an unanswered STOP appears even in a browser whose timer never fires", () => {
  const page = panel();
  const { context, clock, announced } = page;
  pressStop(page);
  clock.now += 2500;
  context.refreshLiveness();
  assert.equal(announced("STOP NON CONFIRME"), true);
});

test("a STOP answered in time shows no notice, and a late answer takes the notice down", async () => {
  const page = panel();
  const { context, frame, announced, wait } = page;
  frame(turningFrame(59.9));
  const quick = pressStop(page);
  quick.request.resolve(stopTaken(60));
  await settle();
  wait(5000);
  context.refreshLiveness();
  assert.equal(announced("STOP NON CONFIRME"), false);
  // A request the console takes its time over: said after two seconds, withdrawn by the answer.
  const slow = pressStop(page);
  wait(2000);
  assert.equal(announced("STOP NON CONFIRME"), true);
  slow.request.resolve(stopTaken(68));
  await settle();
  assert.equal(announced("STOP NON CONFIRME"), false);
  assert.deepEqual(slow.alerts, []);
});

test("a STOP the console refuses is answered: its refusal is said as before, and no notice stays", async () => {
  // Given a STOP the console is slow to answer, so that the notice is up.
  const page = panel();
  const { frame, announced, wait } = page;
  frame(turningFrame(59.9));
  const { request, alerts } = pressStop(page);
  wait(2000);
  assert.equal(announced("STOP NON CONFIRME"), true);
  // When the console answers that a stop is already under way.
  request.resolve(answered(409, { detail: "the machine is already stopping" }));
  await settle();
  // Then the alert says so in the words it always used, and nothing is left unanswered.
  assert.deepEqual(alerts, ["STOP refuse : the machine is already stopping"]);
  assert.equal(announced("STOP NON CONFIRME"), false);
});

test("a failed STOP request is said at once, with its age, and stays on screen after its alert", async () => {
  // Given a console that cannot be reached at all.
  const page = panel();
  const { context, frame, announced, wait } = page;
  frame(turningFrame(59.9));
  const { request, alerts } = pressStop(page);
  request.reject(new TypeError("Failed to fetch"));
  await settle();
  // Then the alert no longer calls a request that got no answer a refusal.
  assert.deepEqual(alerts, [
    "la demande d'arret (STOP) a echoue : Failed to fetch - rien ne dit que la seance s'arrete : E-STOP, ou l'arret cable",
  ]);
  // And the banner stays once the alert is dismissed, saying the request is over and when it failed.
  assert.equal(announced("STOP NON CONFIRME"), true);
  assert.equal(announced("la demande a echoue il y a 0 s et n'est plus en attente : Failed to fetch"), true);
  for (let tick = 1; tick <= 15; tick += 1) {
    frame(turningFrame(60 + tick * 0.2));
  }
  assert.equal(announced("la demande a echoue il y a 3 s et n'est plus en attente : Failed to fetch"), true);
  wait(60000);
  context.refreshLiveness();
  assert.equal(announced("STOP NON CONFIRME"), true);
});

test("an error of the console's own is not a refusal: the STOP stays unconfirmed", async () => {
  const page = panel();
  const { frame, announced } = page;
  frame(turningFrame(59.9));
  const { request, alerts } = pressStop(page);
  request.resolve(answered(500, null));
  await settle();
  assert.equal(alerts.length, 1);
  assert.ok(alerts[0].startsWith("la demande d'arret (STOP) a echoue : 500"), alerts[0]);
  assert.equal(announced("STOP NON CONFIRME"), true);
});

test("a frame showing the session ending ends the doubt about STOP; a frame at rest does not", async () => {
  // Given a STOP left unanswered, with the notice up.
  const page = panel();
  const { context, frame, announced, wait } = page;
  frame(turningFrame(59.9));
  pressStop(page);
  wait(2000);
  assert.equal(announced("STOP NON CONFIRME"), true);
  // A machine at rest may have a start waiting behind it: that frame proves no stop.
  frame(snapshot({ at: 62.2, mode: "repos" }));
  assert.equal(announced("STOP NON CONFIRME"), true);
  // When a frame shows the session ending, whichever way the stop came.
  frame(turningFrame(62.4, { mode: "arret", phase: "cooldown" }));
  // Then the stop is under way and the notice goes. It says nothing about the shaft, and neither
  // does its going: the measured speed does.
  assert.equal(announced("STOP NON CONFIRME"), false);
  wait(5000);
  context.refreshLiveness();
  assert.equal(announced("STOP NON CONFIRME"), false);
});

test("a status asked after the click and showing no session running ends the doubt about STOP; one asked before does not", async () => {
  // Given frames that have stopped, a status request in flight, then a STOP left unanswered.
  const page = panel();
  const { context, clock, frame, announced, wait } = page;
  frame(turningFrame(59.9));
  context.state.connected = false;
  const early = deferred();
  context.api = () => early.promise;
  const asked = context.loadStatus();
  pressStop(page);
  wait(2000);
  assert.equal(announced("STOP NON CONFIRME"), true);
  // When the request sent before the click is answered: it says what stood before the session began.
  early.resolve(status({ at: 40 }));
  await asked;
  clock.now += 100;
  context.refreshLiveness();
  // Then the notice stays: that answer proves nothing about a click it did not see.
  assert.equal(announced("STOP NON CONFIRME"), true);
  // A status asked after the click that still shows the session running leaves it up too.
  context.api = () => Promise.resolve(status({ at: 62.5, run_state: "running" }));
  await context.loadStatus();
  assert.equal(announced("STOP NON CONFIRME"), true);
  // And one that shows the machine stopping takes it down.
  context.api = () => Promise.resolve(status({ at: 63, run_state: "stopping" }));
  await context.loadStatus();
  assert.equal(announced("STOP NON CONFIRME"), false);
});

test("a status showing the machine idle ends the doubt about STOP, unless a request still waits in its mailbox", async () => {
  // Given frames that have stopped, and a STOP left unanswered.
  const page = panel();
  const { context, frame, announced, wait } = page;
  frame(turningFrame(59.9));
  context.state.connected = false;
  pressStop(page);
  wait(2000);
  assert.equal(announced("STOP NON CONFIRME"), true);
  // When a status asked after the click says "idle" with a start still in the mailbox: the loop
  // takes it on its next tick, and the session it starts is one this STOP did not end.
  const waiting = { kind: "manual_start", operator: "op", detail: "bench", at: 62.4 };
  context.api = () => Promise.resolve(status({ at: 62.5, run_state: "idle", pending: waiting }));
  await context.loadStatus();
  // Then the notice stays.
  assert.equal(announced("STOP NON CONFIRME"), true);
  // And it goes on a status that shows the machine idle with nothing waiting: no session is left to stop.
  context.api = () => Promise.resolve(status({ at: 63, run_state: "idle", pending: null }));
  await context.loadStatus();
  assert.equal(announced("STOP NON CONFIRME"), false);
});

test("the STOP notice is in the stack every page shows, as an alarm, under that of the E-STOP", () => {
  const ids = banners.map((entry) => entry.id);
  assert.deepEqual(ids, ["banner", "estop-unanswered", "stop-unanswered", "estop-banner", "resume-banner"]);
  assert.ok(/id="stop-unanswered" class="banner hidden" role="alert"/.test(html));
});

/* ================= a speed that can come back up with nobody clicking ================= */

const RESUME = "REPRISE AUTOMATIQUE POSSIBLE";

// A programme under way at 200 tr/min moteur, as one frame shows it.
const programmeFrame = (at, phase, overrides = {}) =>
  snapshot({ at, mode: "seance", phase, setpoint: turning(200), measured: turning(200), ...overrides });

test("a programme whose speed an unlatched warning holds says the speed can climb again by itself", () => {
  // Given a programme in its warm-up, and a banner stack that takes no room.
  const { frame, announced, published, stack } = panel();
  frame(programmeFrame(60, "warmup"));
  assert.equal(announced(RESUME), false);
  // When a warning that is not latched holds the speed.
  frame(programmeFrame(60.2, "warmup", verdict("freeze", "hr_stale", false)));
  // Then a banner says so in French, names the warning, and the page makes room for it.
  assert.equal(announced(RESUME), true);
  assert.equal(announced("l'avertissement hr_stale tient la vitesse et n'est pas verrouille"), true);
  assert.equal(announced("la vitesse remonte alors sans aucun clic"), true);
  assert.equal(published.get("--banners-h"), stack() + "px");
  // And when the warning lowers the speed instead, it says that.
  frame(programmeFrame(60.4, "warmup", verdict("reduce", "hr_stale", false)));
  assert.equal(announced("l'avertissement hr_stale baisse la vitesse et n'est pas verrouille"), true);
  // It goes with the warning.
  frame(programmeFrame(60.6, "warmup"));
  assert.equal(announced(RESUME), false);
  assert.equal(published.get("--banners-h"), "0px");
});

test("the banner is shown in the phases that can still be asked for speed, and in no other", () => {
  const { frame, announced } = panel();
  const held = verdict("freeze", "attendant_absent", false);
  for (const [phase, expected] of [
    ["baseline", true],
    ["warmup", true],
    ["hold", true],
    ["cooldown", false],
    ["recovery", false],
    ["done", false],
  ]) {
    frame(programmeFrame(60, phase, held));
    assert.equal(announced(RESUME), expected, phase);
  }
});

test("nothing is said to resume behind a latched verdict, a stop, or a session that is ending", () => {
  const { frame, announced } = panel();
  // Latched: it stands until a named operator clears it.
  frame(programmeFrame(60, "hold", verdict("freeze", "loop_stall", true)));
  assert.equal(announced(RESUME), false);
  frame(programmeFrame(60.2, "hold", verdict("reduce", "hr_stale", true)));
  assert.equal(announced(RESUME), false);
  // A stop ends the session, latched or not.
  frame(programmeFrame(60.4, "hold", verdict("ramp_down", "drive_fault", false)));
  assert.equal(announced(RESUME), false);
  // Ending, or at rest: the speed follows nothing upwards from there.
  frame(programmeFrame(60.6, "hold", { mode: "arret", ...verdict("freeze", "hr_stale", false) }));
  assert.equal(announced(RESUME), false);
  frame(programmeFrame(60.8, "hold", { mode: "repos", ...verdict("freeze", "hr_stale", false) }));
  assert.equal(announced(RESUME), false);
});

test("a programme coming down in its cooldown under a latched freeze is nowhere said to be held", () => {
  // Given the sentence the supervisor writes once for a stalled loop, as the snapshot carries it
  // for as long as the freeze is latched (ANH-189: under it the programme's own descent is followed).
  const sentence =
    "the control loop took 1.20 s between ticks against a period of 0.20 s: the setpoint is held " +
    "where it was (it still comes down on a stop asked for, and on the programme's own descent)";
  const latched = verdict("freeze", "loop_stall", true);
  const frozen = { ...latched, safety: { ...latched.safety, detail: sentence } };
  const { frame, announced, node, shown } = panel();
  // When two frames of the cooldown show the setpoint coming down under that freeze.
  frame(programmeFrame(60, "cooldown", { setpoint: turning(190), measured: turning(192), ...frozen }));
  frame(programmeFrame(60.2, "cooldown", { setpoint: turning(188), measured: turning(190), ...frozen }));
  // Then the Seance page and the Securite page print the verdict's own sentence, whole.
  for (const grid of ["run-safety-grid", "safety-grid"]) {
    assert.equal(rows(node(grid)).detail.textContent, sentence);
    assert.equal(rows(node(grid)).verrouille.textContent, "oui");
  }
  assert.equal(node("run-safety-action").textContent, "freeze");
  assert.equal(node("phase").textContent, "cooldown");
  // And the page adds no "held" of its own: no banner, and no manual card to say "consigne maintenue".
  assert.equal(announced(RESUME), false);
  assert.equal(announced("tient la vitesse"), false);
  assert.equal(shown("ramp-banner"), false);
  assert.equal(shown("manual-controls"), false);
});

test("in manual the banner takes a target above the setpoint: a held arm at standstill has none", () => {
  const { frame, announced } = panel();
  const held = verdict("freeze", "attendant_absent", false);
  // A turning arm whose rise towards 249 tr/min moteur is held at 120: it will climb when the warning lifts.
  frame(manualFrame(60, 249, { setpoint: turning(120), measured: turning(120), ...held }));
  assert.equal(announced(RESUME), true);
  // A reduce walking the setpoint down under the target: the same.
  frame(manualFrame(60.2, 249, { setpoint: turning(90), measured: turning(95), ...verdict("reduce", "hr_stale", false) }));
  assert.equal(announced(RESUME), true);
  // An arm at its target: nothing above it to climb to.
  frame(manualFrame(60.4, 120, { setpoint: turning(120), measured: turning(120), ...held }));
  assert.equal(announced(RESUME), false);
  // An arm held at standstill: its target is 0, nothing waits, nothing will move.
  frame(manualFrame(60.6, 0, held));
  assert.equal(announced(RESUME), false);
});

/* ================== chips that go on vouching for a machine gone quiet ================== */

const css = readFileSync(new URL("app.css", assets), "utf8");

// The chips of the cards that the frames colour, with what a healthy machine at rest makes them say.
const FRAME_CHIPS = {
  "console-hr-quality": "good",
  "hr-quality": "good",
  "console-motion": "a l'arret",
  motion: "a l'arret",
  "setpoint-confirmed": "confirmee par le variateur",
  "safety-action": "none",
  "run-safety-action": "none",
};

test("under NO LIVE DATA the green chips of the cards are struck with the numbers beside them", () => {
  // Given a live page on a healthy machine: the chips of its cards are green.
  const { context, clock, frame, node, shown } = panel();
  frame(snapshot());
  for (const [id, label] of Object.entries(FRAME_CHIPS)) {
    assert.equal(node(id).textContent, label, id);
    assert.ok(node(id).classes.has("pill-good"), `#${id} is not green on a healthy machine`);
    assert.equal(node(id).classes.has("stale"), false, id);
  }
  // When the frames stop.
  clock.now += 5000;
  context.refreshLiveness();
  // Then every one of them is struck, like the large numbers and the chips of the sidebar.
  assert.equal(shown("banner"), true);
  for (const id of [...Object.keys(FRAME_CHIPS), "console-drive-state", "drive-state", "phase", "manual-state"]) {
    assert.ok(node(id).classes.has("stale"), `#${id} still vouches for the machine under NO LIVE DATA`);
  }
  for (const id of ["console-hr", "console-output", "side-motion", "side-safety", "run-mode"]) {
    assert.ok(node(id).classes.has("stale"), id);
  }
  // And they come back with the frames.
  frame(snapshot({ at: 56 }));
  for (const id of Object.keys(FRAME_CHIPS)) {
    assert.equal(node(id).classes.has("stale"), false, id);
    assert.ok(node(id).classes.has("pill-good"), id);
  }
});

test("a struck chip loses its colour: the stylesheet takes the green away", () => {
  // The chips keep their colour class when struck; this rule, placed after them, is what greys them.
  const colours = css.indexOf(".pill-good {");
  const struck = /\.pill\.stale\s*\{([^}]*)\}/.exec(css);
  assert.ok(struck, "no rule for a struck chip");
  assert.ok(struck.index > colours, "the rule comes before the colours it has to override");
  assert.ok(/background:\s*var\(--panel-2\)/.test(struck[1]), struck[1]);
  assert.ok(/border-color:\s*var\(--line\)/.test(struck[1]), struck[1]);
});

test("the chips fed by the link panel are struck when the panel stops answering, and only then", () => {
  // Given a BITalino acquiring: its chip is green.
  const { context, clock, frame, node } = panel();
  context.renderPanel(panelRow());
  frame(snapshot());
  assert.equal(node("console-ecg-link").textContent, "acquisition");
  assert.ok(node("console-ecg-link").classes.has("pill-good"));
  assert.equal(node("console-ecg-link").classes.has("stale"), false);
  // When the frames stop but the panel still answers over HTTP: its chips are current.
  clock.now += 3000;
  context.renderPanel(panelRow());
  context.refreshLiveness();
  assert.ok(node("console-motion").classes.has("stale"));
  assert.equal(node("console-ecg-link").classes.has("stale"), false);
  assert.equal(node("console-mode").classes.has("stale"), false);
  // When the panel has not answered for 3.5 s, frames or no frames.
  for (let tick = 1; tick <= 18; tick += 1) {
    frame(snapshot({ at: 60 + tick * 0.2 }));
  }
  // Then "acquisition" no longer stands in green for a link nobody has heard of.
  assert.equal(node("console-motion").classes.has("stale"), false);
  assert.ok(node("console-ecg-link").classes.has("stale"));
  assert.ok(node("console-mode").classes.has("stale"));
  // And the next answer brings them back.
  context.renderPanel(panelRow());
  context.refreshLiveness();
  assert.equal(node("console-ecg-link").classes.has("stale"), false);
});

test("the chips fed by the status are struck when the status stops answering", async () => {
  const { context, clock, node } = panel();
  context.api = () => Promise.resolve(status());
  await context.loadStatus();
  context.refreshLiveness();
  assert.equal(node("attest-state").textContent, "atteste");
  assert.ok(node("attest-state").classes.has("pill-good"));
  assert.equal(node("attest-state").classes.has("stale"), false);
  // One refresh missed is not yet silence.
  clock.now += 9000;
  context.refreshLiveness();
  assert.equal(node("attest-state").classes.has("stale"), false);
  // Two are: "atteste" and the state no longer stand for the present.
  clock.now += 3000;
  context.refreshLiveness();
  assert.ok(node("attest-state").classes.has("stale"));
  assert.ok(node("run-state").classes.has("stale"));
  await context.loadStatus();
  context.refreshLiveness();
  assert.equal(node("attest-state").classes.has("stale"), false);
});

test("every element the liveness check strikes exists in the page", () => {
  const { context, node } = panel();
  const lists = vm.runInContext("[FRAME_FED, PANEL_FED, STATUS_FED]", context);
  assert.equal(lists.length, 3);
  for (const ids of lists) {
    assert.ok(ids.length > 0);
    for (const id of ids) node(id);
  }
});

/* ============================ "perime", in the colour of stale data ============================ */

const sensorRow = (overrides = {}) => ({
  kind: "RESP",
  channel: 3,
  label: "Respiration",
  unit: "%",
  description: "",
  display_rate: 50,
  at: null,
  waveform: [],
  quality: "good",
  detail: "",
  metrics: [],
  ...overrides,
});

for (const id of ["console-hr-quality", "hr-quality"]) {
  test(`#${id} shows a stale heart rate in amber before any verdict speaks about it`, () => {
    // Given a reading 6 s old: stale since 4 s, and no warning before 10 s.
    const { frame, node } = panel();
    frame(snapshot({ heart_rate: { bpm: 72, quality: "good", age_s: 6, stale: true, seq: 423 }, live_bpm: null }));
    // Then the card is not left without a colour: its chip is amber, the page's colour for stale data.
    assert.equal(node(id).textContent, "perime");
    assert.ok(node(id).classes.has("pill-warn"), node(id).className);
    assert.equal(node("safety-action").textContent, "none");
  });
}

test("a stale sensor says perime in amber on its card, on its page and in the menu", () => {
  // Given a sensor channel that has delivered nothing: its reading is stale.
  const { context, node } = panel();
  context.api = () => new Promise(() => undefined);
  context.renderSensors([sensorRow()]);
  const entry = context.state.sensors.RESP;
  assert.equal(entry.card.badge.textContent, "perime");
  assert.ok(entry.card.badge.classes.has("pill-warn"), entry.card.badge.className);
  // On its own page, the chip of the title.
  context.showView("sensor", "RESP");
  assert.equal(node("sensor-quality").textContent, "perime");
  assert.ok(node("sensor-quality").classes.has("pill-warn"), node("sensor-quality").className);
  // In the menu its dot is the stale one, which the stylesheet draws as an amber ring.
  assert.ok(entry.nav.dot.classes.has("dot-stale"));
  assert.ok(/\.dot-stale\s*\{[^}]*var\(--warn\)/.test(css));
});

test("a sensor that delivers again leaves amber for its own grade", () => {
  const { context, clock } = panel();
  context.renderSensors([sensorRow({ at: 10 })]);
  clock.now += 1000;
  context.renderSensors([sensorRow({ at: 11 })]);
  const badge = context.state.sensors.RESP.card.badge;
  assert.equal(badge.textContent, "bon signal");
  assert.ok(badge.classes.has("pill-good"));
});

test("the resume banner is part of the stack every page shows, and is not an alarm", () => {
  const banner = banners.find((entry) => entry.id === "resume-banner");
  assert.ok(banner, "the banner is not in the stack above the pages");
  const stackMarkup = /<div id="banners"[\s\S]*?\n<\/div>/.exec(html)[0];
  assert.ok(stackMarkup.includes('id="resume-banner"'));
  assert.ok(/id="resume-banner" class="banner banner-warn hidden"/.test(html));
});

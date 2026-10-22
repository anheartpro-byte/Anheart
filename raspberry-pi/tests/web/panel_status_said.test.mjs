// What the page does with what the console now SAYS (ANH-190, third part).
//
// The console reports four things the page used to guess, or could not know:
//
// - the verdict in force and the latched floor WHOEVER latched them, in /api/status: a screen with
//   no live frame learns a stop the runtime latched itself;
// - what go_silent replaced (`silenced`): a screen opened under go_silent announces the stop of a
//   rule as one that watched the sequence does;
// - the name of its own run (`instance`), in every status answer and every frame: a restart of the
//   console is then proven, not inferred from a clock or a count;
// - the drive's state, in /api/status as in the frames: a fault at rest is on screen before
//   anybody clicks, and a start is refused by the console at the click, with the reason.
//
// And one thing the page now judges on the status when no frame is live: whether an
// acknowledgement held.
//
// A file of its own, with its own copy of the small page harness of panel_display.test.mjs: the
// banner rules pinned there (three review rounds) are left as they are, and every test there
// passes unchanged against this page except the one that lists the banners of the stack.

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

// The shipped page and script, with every element carrying the classes and the text the markup gives it.
function panel() {
  const nodes = new Map(marked.map(([id, classes, text]) => [id, new Element(classes, text)]));
  const clock = { now: 1000 };
  const published = new Map();
  const timers = [];
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
      setTimeout: (callback, delay) => timers.push({ callback, due: clock.now + delay }),
    },
    performance: { now: () => clock.now },
  });
  script.runInContext(context);
  // A machine that answers nothing until the test says what it answers.
  context.api = () => new Promise(() => undefined);
  // A page whose socket is open and has been delivering frames for a long time.
  context.state.connected = true;
  context.state.lastFrameAt = clock.now;
  context.state.streamFrom = { stream: context.state.stream, at: Number.NEGATIVE_INFINITY };
  const node = (id) => {
    assert.ok(nodes.has(id), `the page has no #${id}`);
    return nodes.get(id);
  };
  const said = (banner) =>
    banner.inner
      .replace(/<[a-z]+ id="([^"]+)"[^>]*>[^<]*<\/[a-z]+>/g, (whole, id) => nodes.get(id).textContent)
      .replace(/<[^>]+>/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  const stack = () =>
    banners
      .filter((banner) => !nodes.get(banner.id).classes.has("hidden"))
      .map((banner) => said(banner).length)
      .reduce((height, length) => height + 27 + 27 * Math.ceil(length / 40), 0);
  Object.defineProperty(nodes.get("banners"), "offsetHeight", { get: stack });
  return {
    context,
    clock,
    node,
    published,
    stack,
    shown: (id) => !node(id).classes.has("hidden"),
    // Whether a banner saying `phrase` is on screen, whichever element carries it.
    announced: (phrase) =>
      banners.some((banner) => !nodes.get(banner.id).classes.has("hidden") && said(banner).includes(phrase)),
    // What the banner `id` reads, whole.
    reads: (id) => said(banners.find((banner) => banner.id === id)),
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

function deferred() {
  const answer = {};
  answer.promise = new Promise((resolve, reject) => Object.assign(answer, { resolve, reject }));
  return answer;
}

const speed = { motor_rpm: 0, output_rpm: 0, hertz: 0, g_load: 0, resultant_g: 1 };

function snapshot(overrides = {}) {
  return {
    sto: { state: "ready", source: "sim_ready", age_s: 0, stale_after_s: 2, refusal: null, unknown_refusal: "STO unknown refusal" },
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

// One /api/status answer as the console of this branch writes it: the name of its run, what
// go_silent stands over, and the drive's state are always there.
function status(overrides = {}) {
  return {
    sto: { state: "ready", source: "sim_ready", age_s: 0, stale_after_s: 2, refusal: null, unknown_refusal: "STO unknown refusal" },
    at: 50,
    instance: "run-a",
    run_state: "idle",
    estop_latched: false,
    supervisor_estop: null,
    attested: true,
    attestation: null,
    attestation_statement: "",
    standing: null,
    floor: null,
    silenced: null,
    drive_state: "switch_on_disabled",
    drive_fault: null,
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
    ...overrides,
  };
}

// An answer as a console from before this branch wrote it: none of the three.
function olderStatus(overrides = {}) {
  const row = status(overrides);
  delete row.instance;
  delete row.silenced;
  delete row.drive_state;
  delete row.drive_fault;
  return row;
}

const LATCHED = "ARRET D'URGENCE VERROUILLE";
const silent = () => verdict("go_silent", "comms_lost");
const ruleStop = (rule = "reverse_rotation") => verdict("quick_stop", rule);

// A frame of the run `instance`.
const frameOf = (instance, overrides) => snapshot({ instance, ...overrides });

// What the status answers while the latched quick stop of a rule stands.
const ruleStopStatus = (at, instance, published = 0, rule = "reverse_rotation") =>
  status({ at, instance, standing: ruleStop(rule).safety, floor: ruleStop(rule).safety, counters: [1, 0, published] });

// What it answers once go_silent has replaced that stop in the SAME console: the stop is behind it.
const silencedStatus = (at, instance, published = 0, rule = "reverse_rotation") =>
  status({
    at,
    instance,
    standing: silent().safety,
    floor: silent().safety,
    silenced: ruleStop(rule).safety,
    counters: [1, 0, published],
  });

// What it answers under go_silent with nothing latched behind it.
const silenceAlone = (at, instance, published = 0) =>
  status({ at, instance, standing: silent().safety, floor: silent().safety, counters: [1, 0, published] });

// One /api/status answer, asked and applied now.
async function answer(context, row) {
  context.api = () => Promise.resolve(row);
  await context.loadStatus();
}

// The telemetry socket of this screen drops while its requests are still answered.
function dropSocket({ context, clock }) {
  context.state.connected = false;
  clock.now += 5000;
  context.refreshLiveness();
}

// It opens again: a new socket.
function reopenSocket({ context }) {
  context.state.connected = true;
  context.state.stream += 1;
}

/* ================= item 1: the status says what stands, whoever latched it ================= */

for (const [how, given] of [
  ["with the socket down", (page) => {
    page.frame(snapshot({ at: 58 }));
    dropSocket(page);
    assert.equal(page.shown("banner"), true);
  }],
  ["with no frame ever received", () => undefined],
]) {
  test(`a stop the runtime latched itself raises the banner and fills the lines from the status alone, ${how}`, async () => {
    // Given a screen that has no live frame.
    const page = panel();
    const { context, node, announced } = page;
    given(page);
    assert.equal(announced(LATCHED), false);
    // When the console finds its drive running at start-up and latches a stop of its own: the
    // status now carries it as the verdict in force and as the latched floor.
    const own = ruleStop("drive_precommanded").safety;
    await answer(context, status({ at: 60, standing: own, floor: own }));
    // Then the banner is up, and neither line says "aucun".
    assert.equal(announced(LATCHED), true);
    for (const id of ["verdicts-grid", "system-grid"]) {
      assert.equal(rows(node(id))["verdict retenu"].textContent, "drive_precommanded / quick_stop");
      assert.equal(rows(node(id))["plancher verrouille"].textContent, "drive_precommanded / quick_stop");
    }
    // And once it is acknowledged, a later answer takes all of it down.
    await answer(context, status({ at: 65 }));
    assert.equal(announced(LATCHED), false);
    assert.equal(rows(node("verdicts-grid"))["verdict retenu"].textContent, "aucun");
    assert.equal(rows(node("verdicts-grid"))["plancher verrouille"].textContent, "aucun");
  });
}

test("the runtime's own go_silent is go_silent for a screen with no frame, and what it replaced stays announced", async () => {
  // Given a stop of the runtime's own, learnt from the status with the socket down.
  const page = panel();
  const { context, node, announced } = page;
  page.frame(snapshot({ at: 58 }));
  dropSocket(page);
  const own = ruleStop("drive_precommanded").safety;
  await answer(context, status({ at: 60, standing: own, floor: own, counters: [0, 0, 40] }));
  assert.equal(announced(LATCHED), true);
  // When the runtime then gives up on its link and goes silent, in that same console.
  await answer(context, silencedStatus(65, "run-a", 65, "drive_precommanded"));
  // Then the banner stays, on what the status itself shows behind go_silent.
  assert.equal(announced(LATCHED), true);
  assert.equal(rows(node("verdicts-grid"))["verdict retenu"].textContent, "comms_lost / go_silent");
  assert.equal(
    rows(node("verdicts-grid"))["verrouille derriere go_silent"].textContent,
    "drive_precommanded / quick_stop",
  );
});

/* ================= item 2: what go_silent replaced ================= */

test("a screen opened under go_silent announces the stop of a rule that go_silent replaced", async () => {
  // Given a page opened once go_silent stands: its frames show go_silent and nothing else.
  const { context, frame, announced } = panel();
  context.api = () => Promise.resolve(silencedStatus(70.1, "run-a"));
  frame(frameOf("run-a", { at: 70, mode: "arret", ...silent() }));
  await settle();
  // Then the stop the console still holds behind it is announced, as on a screen that watched.
  assert.equal(announced(LATCHED), true);
  // And it stays, frame after frame and answer after answer, for as long as that console runs.
  for (let tick = 1; tick <= 10; tick += 1) {
    frame(frameOf("run-a", { at: 70 + tick * 0.2, mode: "arret", ...silent() }));
  }
  await answer(context, silencedStatus(75.1, "run-a", 25));
  assert.equal(announced(LATCHED), true);
});

test("the same with no frame at all: the first status answer raises the banner", async () => {
  const { context, announced } = panel();
  await answer(context, silencedStatus(70.1, "run-a"));
  assert.equal(announced(LATCHED), true);
  // What the page remembers is the stop itself, with the run that holds it: not go_silent.
  assert.equal(context.state.statusLatch.rule, "reverse_rotation");
  assert.equal(context.state.statusLatch.instance, "run-a");
});

test("a screen that watched the sequence and one opened after it show the same thing", async () => {
  // The screen that watched: the stop of a rule in its frames, then go_silent in the same console.
  const watched = panel();
  watched.frame(frameOf("run-a", { at: 60.2, mode: "arret", ...ruleStop() }));
  watched.context.api = () => Promise.resolve(silencedStatus(61.1, "run-a", 304));
  watched.frame(frameOf("run-a", { at: 61, mode: "arret", ...silent() }));
  await settle();
  // The one opened after: go_silent in its frames from the first, and the same answer.
  const late = panel();
  late.context.api = () => Promise.resolve(silencedStatus(61.1, "run-a", 304));
  late.frame(frameOf("run-a", { at: 61, mode: "arret", ...silent() }));
  await settle();
  for (const page of [watched, late]) {
    assert.equal(page.announced(LATCHED), true);
    assert.equal(
      rows(page.node("verdicts-grid"))["verrouille derriere go_silent"].textContent,
      "reverse_rotation / quick_stop",
    );
    assert.equal(rows(page.node("verdicts-grid"))["verdict retenu"].textContent, "comms_lost / go_silent");
  }
});

test("go_silent with nothing latched behind it raises no banner and adds no line", async () => {
  const { context, frame, node, announced } = panel();
  context.api = () => Promise.resolve(silenceAlone(70.1, "run-a"));
  frame(frameOf("run-a", { at: 70, mode: "repos", ...silent() }));
  await settle();
  assert.equal(announced(LATCHED), false);
  for (const id of ["verdicts-grid", "system-grid"]) {
    assert.equal("verrouille derriere go_silent" in rows(node(id)), false);
  }
});

test("a latched verdict behind go_silent that is not a stop is listed, and raises no stop banner", async () => {
  // A drive fault (ramp_down) latched before the link was lost: latched still, and not a quick stop.
  const { context, node, announced } = panel();
  await answer(
    context,
    status({
      at: 70,
      standing: silent().safety,
      floor: silent().safety,
      silenced: verdict("ramp_down", "drive_fault").safety,
    }),
  );
  assert.equal(announced(LATCHED), false);
  for (const id of ["verdicts-grid", "system-grid"]) {
    const line = rows(node(id))["verrouille derriere go_silent"];
    assert.equal(line.textContent, "drive_fault / ramp_down");
    assert.equal(line.classes.has("flag-bad"), true);
  }
});

test("the line comes between the latched floor and the count of rules, and goes when nothing is behind", async () => {
  const { context, node } = panel();
  await answer(context, silencedStatus(70, "run-a"));
  const labels = () => node("verdicts-grid").children.filter((_, index) => index % 2 === 0).map((cell) => cell.textContent);
  assert.deepEqual(labels(), [
    "e-stop verrouille",
    "verdict retenu",
    "plancher verrouille",
    "verrouille derriere go_silent",
    "regles actives",
    "accompagnant vu",
  ]);
  await answer(context, status({ at: 80, instance: "run-b" }));
  assert.deepEqual(labels(), ["e-stop verrouille", "verdict retenu", "plancher verrouille", "regles actives", "accompagnant vu"]);
});

test("an answer of a console that does not say what go_silent replaced is read as it always was", async () => {
  // No `silenced` at all: nothing is raised on it, and no line is added.
  const { context, frame, node, announced } = panel();
  context.api = () => Promise.resolve(olderStatus({ at: 70.1, standing: silent().safety, floor: silent().safety }));
  frame(snapshot({ at: 70, mode: "repos", ...silent() }));
  await settle();
  assert.equal(announced(LATCHED), false);
  assert.equal("verrouille derriere go_silent" in rows(node("verdicts-grid")), false);
});

test("an emergency stop and the stop of a rule both behind go_silent: the banner is that of the emergency stop", async () => {
  // Given both held: the emergency stop in its own slot, the rule's stop in what go_silent replaced.
  const { context, node, announced } = panel();
  const both = silencedStatus(70, "run-a", 40);
  both.supervisor_estop = verdict("quick_stop", "operator_estop").safety;
  await answer(context, both);
  assert.equal(announced(LATCHED), true);
  assert.equal(rows(node("verdicts-grid"))["e-stop verrouille"].textContent, "OUI");
  assert.equal(context.state.statusLatch.rule, "operator_estop");
  // When the same console goes on answering the same.
  await answer(context, { ...both, at: 75, counters: [1, 0, 65] });
  assert.equal(announced(LATCHED), true);
});

test("a stop shown behind go_silent ends the doubt about an E-STOP of this page, as any latched stop does", async () => {
  // Given an E-STOP the console never answered, on a screen with no frame.
  const page = panel();
  const { context, clock, announced } = page;
  page.frame(snapshot({ at: 59.9 }));
  dropSocket(page);
  context.window.alert = () => undefined;
  context.api = (path) => (path === "/api/session/estop" ? new Promise(() => undefined) : new Promise(() => undefined));
  context.doEstop();
  clock.now += 2500;
  context.renderEstopUnanswered();
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), true);
  // When a status asked after the click shows a stop held behind go_silent.
  await answer(context, silencedStatus(70, "run-a"));
  // Then a stop is latched, whichever way it came: that banner is up and the doubt is over.
  assert.equal(announced(LATCHED), true);
  assert.equal(announced("ARRET D'URGENCE NON CONFIRME"), false);
});

/* ================= item 3: the run of the console, and the proof of a restart ================= */

test("the old console died before answering a status after the latch: the name of the new one proves the restart", async () => {
  // Given the latched quick stop of a rule seen in the frames only: no status answer came after it.
  const page = panel();
  const { context, frame, announced } = page;
  await answer(context, status({ at: 58, instance: "run-a", counters: [0, 0, 4900] }));
  frame(frameOf("run-a", { at: 60.2, mode: "arret", ...ruleStop() }));
  assert.equal(announced(LATCHED), true);
  // When that console dies there, and the new one goes silent before this screen sees a frame
  // without a verdict: a new socket, frames under go_silent, and the status of the new console.
  reopenSocket(page);
  context.api = () => Promise.resolve(silenceAlone(75.1, "run-b", 40));
  frame(frameOf("run-b", { at: 75, mode: "repos", ...silent() }));
  await settle();
  // Then the latch was another console's, and the banner goes with that first answer.
  assert.equal(announced(LATCHED), false);
});

test("without the names the same sequence proves nothing, and the banner stays", async () => {
  // The residue as it was: no status between the latch and the restart, and no name to compare.
  const page = panel();
  const { context, frame, announced } = page;
  await answer(context, olderStatus({ at: 58, counters: [0, 0, 4900] }));
  frame(snapshot({ at: 60.2, mode: "arret", ...ruleStop() }));
  reopenSocket(page);
  context.api = () => Promise.resolve(olderStatus({ at: 75.1, standing: silent().safety, floor: silent().safety, counters: [1, 0, 40] }));
  frame(snapshot({ at: 75, mode: "repos", ...silent() }));
  await settle();
  assert.equal(announced(LATCHED), true);
});

test("the new console has already published more snapshots than the old one had: its name proves the restart all the same", async () => {
  // Given the latched quick stop of a rule, shown by the frames and by the status of a console
  // that had published few snapshots.
  const page = panel();
  const { context, frame, announced } = page;
  frame(frameOf("run-a", { at: 60.2, mode: "arret", ...ruleStop() }));
  await answer(context, ruleStopStatus(60.3, "run-a", 300));
  dropSocket(page);
  // When the screen comes back much later: the console was restarted meanwhile, has run for a
  // while and gone silent. Its count of snapshots is far above the old one's: no count went down.
  reopenSocket(page);
  context.api = () => Promise.resolve(silenceAlone(900.1, "run-b", 4200));
  frame(frameOf("run-b", { at: 900, mode: "repos", ...silent() }));
  await settle();
  // Then the banner goes: the answer names another run than the one that showed the latch.
  assert.equal(announced(LATCHED), false);
});

test("the same run under go_silent proves no restart, whatever its count does", async () => {
  // Given the stop of a rule shown by the frames and the status of one run of the console.
  const page = panel();
  const { context, frame, announced } = page;
  frame(frameOf("run-a", { at: 60.2, mode: "arret", ...ruleStop() }));
  await answer(context, ruleStopStatus(60.3, "run-a", 300));
  dropSocket(page);
  // When the socket comes back under go_silent, and that same run answers with nothing behind it:
  // the stop was acknowledged while this screen was cut off, or so that answer reads.
  reopenSocket(page);
  context.api = () => Promise.resolve(silenceAlone(66.1, "run-a", 329));
  frame(frameOf("run-a", { at: 66, mode: "repos", ...silent() }));
  await settle();
  // Then nothing proves a restart, and the banner stays: the prudent side, kept.
  assert.equal(announced(LATCHED), true);
  await answer(context, silenceAlone(71.1, "run-a", 354));
  assert.equal(announced(LATCHED), true);
});

test("a status asked before the frames showed the latch proves nothing, whatever run wrote it", async () => {
  // Given a status request in flight when the frames of a new console show the stop of a rule.
  const { context, frame, announced } = panel();
  const early = deferred();
  context.api = () => early.promise;
  const asked = context.loadStatus();
  frame(frameOf("run-b", { at: 60.2, mode: "arret", ...ruleStop() }));
  // Its answer was written by the console of before: another name, and asked before the latch.
  early.resolve(status({ at: 60.1, instance: "run-a", counters: [0, 0, 5000] }));
  await asked;
  // When go_silent then stands in the console that showed the latch.
  context.api = () => new Promise(() => undefined);
  frame(frameOf("run-b", { at: 61, mode: "repos", ...silent() }));
  // Then the other name is no proof that THAT console has been replaced: the banner stays.
  assert.equal(announced(LATCHED), true);
  // And the answer of that console, which still holds the stop, keeps it.
  await answer(context, silencedStatus(61.1, "run-b", 40));
  assert.equal(announced(LATCHED), true);
});

test("a latch the new console itself shows is its own: the restart before it does not speak for it", async () => {
  // Given the stop of a rule shown by one run, then that console restarted.
  const page = panel();
  const { context, frame, announced } = page;
  frame(frameOf("run-a", { at: 60.2, mode: "arret", ...ruleStop() }));
  await answer(context, ruleStopStatus(60.3, "run-a", 5000));
  // And the same rule latched again in the new one, shown over the new socket.
  reopenSocket(page);
  context.api = () => new Promise(() => undefined);
  frame(frameOf("run-b", { at: 75, mode: "arret", ...ruleStop() }));
  assert.equal(context.state.estopLatch.instance, "run-b");
  // When go_silent replaces it there, and an older console says nothing of what it replaced.
  const reply = silenceAlone(76.1, "run-b", 40);
  delete reply.silenced;
  context.api = () => Promise.resolve(reply);
  frame(frameOf("run-b", { at: 76, mode: "repos", ...silent() }));
  await settle();
  // Then the banner stays: it is the run that showed this latch that went silent.
  assert.equal(announced(LATCHED), true);
});

for (const [how, given] of [
  ["with the socket down", (page) => {
    page.frame(frameOf("run-a", { at: 58, mode: "manuel" }));
    dropSocket(page);
  }],
  ["with no frame ever received", () => undefined],
]) {
  test(`a stop the status showed goes with the first answer of another run, ${how}`, async () => {
    // Given the stop of a rule this screen learnt from the status alone, kept under go_silent.
    const page = panel();
    const { context, announced } = page;
    given(page);
    await answer(context, ruleStopStatus(60.3, "run-a", 39));
    await answer(context, silencedStatus(65.3, "run-a", 96));
    assert.equal(announced(LATCHED), true);
    // When the next answer comes from a console started since, silent again, its count higher.
    await answer(context, silenceAlone(900.3, "run-b", 4200));
    // Then the banner goes.
    assert.equal(announced(LATCHED), false);
  });
}

test("a status asked before the one that showed the latch proves nothing, even under another name", async () => {
  // Given a status request in flight when another, asked after it, shows the stop of a rule.
  const { context, announced } = panel();
  const early = deferred();
  context.api = () => early.promise;
  const asked = context.loadStatus();
  await answer(context, ruleStopStatus(60.3, "run-b", 30));
  assert.equal(announced(LATCHED), true);
  // When the first one is answered at last, by the console of before, under go_silent, on a
  // clock that reads later (so that it is not dropped as older).
  early.resolve(silenceAlone(61.1, "run-a", 5000));
  await asked;
  // Then the banner stays: only an answer asked after the latch was shown is a proof.
  assert.equal(announced(LATCHED), true);
});

test("a name on one side only proves nothing", async () => {
  // The frames name their run and the status does not: the count is all there is, and it rose.
  const page = panel();
  const { context, frame, announced } = page;
  frame(frameOf("run-a", { at: 60.2, mode: "arret", ...ruleStop() }));
  reopenSocket(page);
  context.api = () =>
    Promise.resolve(olderStatus({ at: 75.1, standing: silent().safety, floor: silent().safety, counters: [1, 0, 40] }));
  frame(frameOf("run-b", { at: 75, mode: "repos", ...silent() }));
  await settle();
  assert.equal(announced(LATCHED), true);
  // And the other way round: a status that names its run, over frames that did not.
  const other = panel();
  other.frame(snapshot({ at: 60.2, mode: "arret", ...ruleStop() }));
  reopenSocket(other);
  other.context.api = () => Promise.resolve(silenceAlone(75.1, "run-b", 40));
  other.frame(snapshot({ at: 75, mode: "repos", ...silent() }));
  await settle();
  assert.equal(other.announced(LATCHED), true);
});

test("a restart proven by the name still holds when the next answer comes from that same new run", async () => {
  // Given the stop of a rule shown by the frames, then a socket that drops on it.
  const page = panel();
  const { context, frame, announced } = page;
  frame(frameOf("run-a", { at: 60.2, mode: "arret", ...ruleStop() }));
  await answer(context, ruleStopStatus(60.3, "run-a", 40));
  dropSocket(page);
  // And a status from a console started since, at rest, which has already published more
  // snapshots than the first: only its name says it is another one. The last frame still shows
  // the latch, so the banner stays.
  await answer(context, status({ at: 900, instance: "run-b", counters: [0, 0, 4200] }));
  assert.equal(announced(LATCHED), true);
  // When the frames come back under the go_silent of that new console.
  reopenSocket(page);
  context.api = () => Promise.resolve(silenceAlone(905.1, "run-b", 4225));
  frame(frameOf("run-b", { at: 905, mode: "repos", ...silent() }));
  await settle();
  // Then the restart proven before still holds, and the banner goes.
  assert.equal(announced(LATCHED), false);
});

test("the stop of a rule the new console holds behind its own go_silent is announced after the restart too", async () => {
  // Given a banner from the frames of one run, then a console restarted that stops for the same
  // rule and goes silent before this screen sees any of it.
  const page = panel();
  const { context, frame, announced } = page;
  frame(frameOf("run-a", { at: 60.2, mode: "arret", ...ruleStop() }));
  reopenSocket(page);
  context.api = () => Promise.resolve(silencedStatus(900.1, "run-b", 30));
  frame(frameOf("run-b", { at: 900, mode: "arret", ...silent() }));
  await settle();
  // Then the banner is up for the stop that console holds, not kept from the old one.
  assert.equal(announced(LATCHED), true);
  assert.equal(context.state.statusLatch.instance, "run-b");
  // And it goes when that console is restarted in its turn.
  await answer(context, silenceAlone(1800.1, "run-c", 12));
  assert.equal(announced(LATCHED), false);
});

/* ================= EX-8: a drive fault at rest, seen before the click ================= */

const FAULT = "VARIATEUR EN DEFAUT";
const OBF = { name: "dc_bus_overvoltage", mnemonic: "ObF", meaning: "surtension", raw_code: 18, message: "ObF (LFT 18): surtension" };
const faulted = (at, overrides = {}) => snapshot({ at, drive_state: "fault", fault: OBF, ...overrides });

test("a drive fault at rest is said in a banner on every page, with the code on the drive's own display", () => {
  // Given a console at rest that has armed nothing: a fault raises no verdict there.
  const { frame, node, announced, reads } = panel();
  frame(snapshot({ at: 59.8 }));
  assert.equal(announced(FAULT), false);
  // When the drive shows a fault.
  frame(faulted(60));
  // Then it is said before anybody clicks, while the safety chip still says nothing is asked.
  assert.equal(
    reads("drive-fault-banner"),
    "VARIATEUR EN DEFAUT ObF (LFT brut 18) : aucun demarrage n'est accepte tant que le variateur signale ce defaut. " +
      "Le traiter sur la carte Variateur (Tableau de bord)",
  );
  assert.equal(node("side-safety").textContent, "none");
  assert.equal(node("run-mode").textContent, "REPOS");
  // And for as long as the drive shows it.
  for (let tick = 1; tick <= 10; tick += 1) {
    frame(faulted(60 + tick * 0.2));
    assert.equal(announced(FAULT), true);
  }
});

test("a fault that clears clears on the screen with the first frame that no longer shows it", () => {
  const { frame, announced } = panel();
  frame(faulted(60));
  assert.equal(announced(FAULT), true);
  frame(snapshot({ at: 60.2 }));
  assert.equal(announced(FAULT), false);
});

test("the banner names a fault whose code was not read, or is not known, as that", () => {
  const { frame, reads } = panel();
  frame(faulted(60, { fault: null }));
  assert.match(reads("drive-fault-banner"), /^VARIATEUR EN DEFAUT code non lu sur le variateur : aucun demarrage/);
  frame(faulted(60.2, { fault: { name: "unknown", mnemonic: "?", meaning: "", raw_code: 250, message: "LFT 250 (0x00FA)" } }));
  assert.match(reads("drive-fault-banner"), /^VARIATEUR EN DEFAUT code inconnu \(LFT brut 250\) : aucun demarrage/);
});

test("a drive whose state is unknown raises no fault banner", () => {
  const { frame, announced } = panel();
  frame(snapshot({ at: 60, drive_state: "comm_lost", drive_status_stale: true }));
  assert.equal(announced(FAULT), false);
});

test("the banner is red, in the stack every page shows, under the out-of-service one", () => {
  const ids = banners.map((entry) => entry.id);
  assert.equal(ids.indexOf("drive-fault-banner"), ids.indexOf("service-banner") + 1);
  assert.ok(/id="drive-fault-banner" class="banner hidden" role="alert"/.test(html));
  const stackMarkup = /<div id="banners"[\s\S]*?\n<\/div>/.exec(html)[0];
  assert.ok(stackMarkup.includes('id="drive-fault-banner"'));
});

test("the page makes room for the banner when it appears, and takes it back when it goes", () => {
  const { frame, published, stack } = panel();
  frame(snapshot({ at: 59.8 }));
  frame(faulted(60));
  assert.ok(stack() > 0);
  assert.equal(published.get("--banners-h"), stack() + "px");
  frame(snapshot({ at: 60.2 }));
  assert.equal(published.get("--banners-h"), "0px");
});

test("with the socket down, the status raises the banner for a fault that appeared since, and takes it down when it is reset", async () => {
  // Given a last frame that showed a healthy drive, then frames that stop coming.
  const page = panel();
  const { context, announced } = page;
  page.frame(snapshot({ at: 58 }));
  dropSocket(page);
  assert.equal(announced(FAULT), false);
  // When the status, written after that frame, says the drive is in fault.
  await answer(context, status({ at: 60, drive_state: "fault", drive_fault: OBF }));
  // Then the fault is on screen, next to NO LIVE DATA.
  assert.equal(announced(FAULT), true);
  assert.equal(announced("NO LIVE DATA"), true);
  // And a later answer where the drive shows no fault takes it down.
  await answer(context, status({ at: 65 }));
  assert.equal(announced(FAULT), false);
});

test("with no frame at all, the first status that shows the fault raises the banner", async () => {
  const { context, clock, announced, reads } = panel();
  context.state.connected = false;
  clock.now += 5000;
  await answer(context, status({ at: 60, drive_state: "fault", drive_fault: null }));
  assert.equal(announced(FAULT), true);
  assert.match(reads("drive-fault-banner"), /code non lu sur le variateur/);
});

test("a status written before the last frame does not speak over it, either way", async () => {
  // Given a last frame that showed the fault, and a status request still in flight then.
  const page = panel();
  const { context, announced } = page;
  const early = deferred();
  context.api = () => early.promise;
  const asked = context.loadStatus();
  page.frame(faulted(60));
  dropSocket(page);
  // When its answer, written before that frame, says the drive is healthy.
  early.resolve(status({ at: 59.9 }));
  await asked;
  // Then the banner stays on the frame, which is the newer of the two.
  assert.equal(announced(FAULT), true);
  // The other way round: a healthy last frame, and an older answer that says "fault".
  const other = panel();
  const late = deferred();
  other.context.api = () => late.promise;
  const pending = other.context.loadStatus();
  other.frame(snapshot({ at: 60 }));
  dropSocket(other);
  late.resolve(status({ at: 59.9, drive_state: "fault", drive_fault: OBF }));
  await pending;
  assert.equal(other.announced(FAULT), false);
});

test("while frames are arriving they decide, whatever the status last said", async () => {
  // Given a status that says "fault", and live frames that show the fault reset since.
  const { context, frame, announced } = panel();
  frame(faulted(60));
  await answer(context, status({ at: 60.1, drive_state: "fault", drive_fault: OBF }));
  assert.equal(announced(FAULT), true);
  frame(snapshot({ at: 60.4 }));
  // Then the banner follows the frame, without waiting five seconds for the next status.
  assert.equal(announced(FAULT), false);
});

test("a console that does not give the drive's state in its status leaves the last frame's word in place", async () => {
  const page = panel();
  const { context, announced } = page;
  page.frame(faulted(60));
  dropSocket(page);
  await answer(context, olderStatus({ at: 65 }));
  assert.equal(announced(FAULT), true);
});

test("the last frame's word stays next to NO LIVE DATA when no status has answered since", () => {
  const page = panel();
  page.frame(faulted(60));
  dropSocket(page);
  assert.equal(page.announced("NO LIVE DATA"), true);
  assert.equal(page.announced(FAULT), true);
});

test("a start the console refuses for the fault shows the console's own sentence, and awaits nothing", async () => {
  // Given the fault on screen, and an operator who clicks Demarrer MANUEL all the same.
  const { context, frame, node } = panel();
  frame(faulted(60));
  node("manual-bench").checked = true;
  node("manual-operator").value = "op";
  const refusal =
    "demarrage refuse : variateur en defaut (ObF, LFT 18). Rien n'a ete envoye a la machine : " +
    "traiter le defaut (carte Variateur), puis redemander.";
  context.api = (path) =>
    path === "/api/manual/start"
      ? Promise.reject(Object.assign(new Error(refusal), { status: 409 }))
      : new Promise(() => undefined);
  context.doManualStart();
  await settle();
  // Then the note says why, in red, at the click: the loop was never asked.
  assert.equal(node("manual-note").textContent, refusal);
  assert.equal(node("manual-note").classes.has("note-bad"), true);
  assert.deepEqual(Object.keys(context.state.asked), []);
});

/* ================= item 5: an acknowledgement judged on the status when no frame is live ================= */

// The operator presses Acquitter; the console answers `record`.
async function acknowledge(context, record) {
  context.window.alert = () => undefined;
  context.api = (path) => (path === "/api/safety/acknowledge" ? Promise.resolve(record) : new Promise(() => undefined));
  context.doAcknowledge();
  await settle();
}

const ackOf = (at, cleared) => ({ operator: "op", at, wall_clock: 0, cleared });
const faultStatus = (at) =>
  status({ at, standing: verdict("ramp_down", "drive_fault").safety, floor: verdict("ramp_down", "drive_fault").safety });
const REPRIS = "acquittement repris par la machine : drive_fault est de nouveau verrouille, sa regle s'est declenchee de nouveau.";

// A latched drive fault this screen knows from its last frame and from the status, then no frame.
async function faultKnownThenNoFrame() {
  const page = panel();
  page.frame(snapshot({ at: 60, drive_state: "fault", fault: OBF, ...verdict("ramp_down", "drive_fault") }));
  await answer(page.context, faultStatus(60.1));
  dropSocket(page);
  return page;
}

test("with no live frame, an acknowledgement the machine takes back is said from the status", async () => {
  // Given a latched drive fault, its cause still there, and a screen whose socket is down.
  const page = await faultKnownThenNoFrame();
  const { context, node } = page;
  await acknowledge(context, ackOf(70, ["drive_fault"]));
  const note = node("ack-note");
  assert.equal(note.textContent, "acquitte par op : drive_fault");
  // The status asked at once is written before the next tick: the verdict is gone for now.
  await answer(context, status({ at: 70.05 }));
  assert.equal(note.textContent, "acquitte par op : drive_fault");
  // When the next one shows the rule latched again.
  await answer(context, faultStatus(75));
  // Then the note no longer says "acquitte" under a verdict that stands.
  assert.equal(note.textContent, REPRIS);
  assert.equal(note.classes.has("note-bad"), true);
  // It is kept for as long as the status shows that rule latched.
  await answer(context, faultStatus(80));
  assert.equal(note.textContent, REPRIS);
  // And wiped when it no longer does: acknowledged again, from another screen.
  await answer(context, status({ at: 85 }));
  assert.equal(note.textContent, "");
});

test("the same for a rule that ends a session: said taken back, with nothing about REPOS that a status cannot know", async () => {
  const page = panel();
  const { context, node } = page;
  const standstill = verdict("ramp_down", "session_standstill");
  page.frame(snapshot({ at: 60, mode: "arret", phase: "recovery", ...standstill }));
  await answer(context, status({ at: 60.1, run_state: "running", standing: standstill.safety, floor: standstill.safety }));
  dropSocket(page);
  await acknowledge(context, ackOf(70, ["session_standstill"]));
  await answer(context, status({ at: 75, run_state: "running", standing: standstill.safety, floor: standstill.safety }));
  assert.equal(
    node("ack-note").textContent,
    "acquittement repris par la machine : session_standstill est de nouveau verrouille, sa regle s'est declenchee de nouveau.",
  );
});

test("with no live frame, an acknowledgement that held keeps its note, and a later latch of that rule is a new one", async () => {
  const page = await faultKnownThenNoFrame();
  const { context, node } = page;
  await acknowledge(context, ackOf(70, ["drive_fault"]));
  // When a status written more than a second after it still shows nothing latched: it held.
  await answer(context, status({ at: 75 }));
  assert.equal(node("ack-note").textContent, "acquitte par op : drive_fault");
  assert.equal(context.state.ackGiven, null);
  // Then the same rule, later, is a new latch: the note is wiped, not said "taken back".
  await answer(context, faultStatus(300));
  assert.equal(node("ack-note").textContent, "");
});

test("an emergency stop acknowledged then latched again is said taken back from the status too", async () => {
  // The emergency stop is in a slot of its own in the status, not in the floor.
  const page = panel();
  const { context, node } = page;
  const pressed = (at) =>
    status({
      at,
      run_state: "stopping",
      estop_latched: true,
      supervisor_estop: verdict("quick_stop", "operator_estop").safety,
      standing: verdict("quick_stop", "operator_estop").safety,
    });
  await answer(context, pressed(60));
  dropSocket(page);
  await acknowledge(context, ackOf(70, ["operator_estop"]));
  await answer(context, status({ at: 70.05 }));
  await answer(context, pressed(75));
  assert.equal(
    node("ack-note").textContent,
    "acquittement repris par la machine : operator_estop est de nouveau verrouille, sa regle s'est declenchee de nouveau.",
  );
});

test("a latch taken back under a more severe warning that is not latched is read on the floor", async () => {
  // Given a latched freeze (loop_stall) acknowledged, with no live frame.
  const page = panel();
  const { context, node } = page;
  const stall = verdict("freeze", "loop_stall").safety;
  await answer(context, status({ at: 60, standing: stall, floor: stall }));
  dropSocket(page);
  await acknowledge(context, ackOf(70, ["loop_stall"]));
  // When it latches again while hr_stale, lowering the speed and not latched, is the verdict in
  // force: the latch is on the floor, not in the standing verdict.
  await answer(context, status({ at: 75, standing: verdict("reduce", "hr_stale", false).safety, floor: stall }));
  // Then it is said taken back all the same.
  assert.equal(
    node("ack-note").textContent,
    "acquittement repris par la machine : loop_stall est de nouveau verrouille, sa regle s'est declenchee de nouveau.",
  );
});

test("while frames are arriving the status does not judge the acknowledgement: the frames do", async () => {
  // Given live frames, and an acknowledgement of a drive fault.
  const { context, frame, node } = panel();
  frame(snapshot({ at: 60, drive_state: "fault", fault: OBF, ...verdict("ramp_down", "drive_fault") }));
  await acknowledge(context, ackOf(70, ["drive_fault"]));
  // When a status shows the rule latched again while the frames have not shown it yet.
  await answer(context, faultStatus(70.3));
  // Then the note waits for the frames, as it did.
  assert.equal(node("ack-note").textContent, "acquitte par op : drive_fault");
  frame(snapshot({ at: 70.4, drive_state: "fault", fault: OBF, ...verdict("ramp_down", "drive_fault") }));
  assert.equal(node("ack-note").textContent, REPRIS);
});

test("a status written before the acknowledgement says nothing of it, however late it arrives", async () => {
  const page = await faultKnownThenNoFrame();
  const { context, node } = page;
  const early = deferred();
  context.api = () => early.promise;
  const asked = context.loadStatus();
  context.window.alert = () => undefined;
  // The acknowledgement is answered while that request is still out.
  const api = context.api;
  context.api = (path) => (path === "/api/safety/acknowledge" ? Promise.resolve(ackOf(70, ["drive_fault"])) : api(path));
  context.doAcknowledge();
  await settle();
  // When the old answer arrives: written before the acknowledgement, the verdict still latched.
  early.resolve(faultStatus(69.9));
  await asked;
  // Then the note is not turned into "taken back" on it.
  assert.equal(node("ack-note").textContent, "acquitte par op : drive_fault");
});

test("a status written before the last frame does not judge the acknowledgement either", async () => {
  // Given an acknowledgement the frames showed taken back, then a socket that drops.
  const page = panel();
  const { context, frame, node } = page;
  frame(snapshot({ at: 60, drive_state: "fault", fault: OBF, ...verdict("ramp_down", "drive_fault") }));
  const early = deferred();
  await acknowledge(context, ackOf(70, ["drive_fault"]));
  context.api = () => early.promise;
  const asked = context.loadStatus();
  frame(snapshot({ at: 70.4, drive_state: "fault", fault: OBF, ...verdict("ramp_down", "drive_fault") }));
  assert.equal(node("ack-note").textContent, REPRIS);
  dropSocket(page);
  // When a status written between the acknowledgement and that frame arrives: nothing latched then.
  early.resolve(status({ at: 70.1 }));
  await asked;
  // Then the note the newer frame wrote is not wiped by it.
  assert.equal(node("ack-note").textContent, REPRIS);
});

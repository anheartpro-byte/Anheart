/*
  ANH-190. A model of the console's mailbox, every interleaving of a few requests over a few
  control cycles, and the real page code held, message after message, against what the model
  knows became of each request.

  Not a test file: the two that play it are panel_requests_family_1.test.mjs and
  panel_requests_family_2.test.mjs. They are two so that the runner plays them side by side.

  It carries a harness of its own, like every file here, reduced to what a start note needs.
*/
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
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
  }
  // Made when asked for: most nodes a page creates (a line of a list, a cell of a grid) never are.
  get classList() {
    return {
      toggle: (name, enabled) => (enabled ? this.classes.add(name) : this.classes.delete(name)),
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
// Every element of the markup that carries an id: its classes and its text.
const marked = [...html.matchAll(/<[a-z0-9]+\b[^>]*\bid="([^"]+)"[^>]*>([^<]*)/g)].map((tag) => [
  tag[1],
  /\bclass="([^"]*)"/.exec(tag[0])?.[1],
  tag[2].trim(),
]);

/*
  One page: the shipped script, run on the shipped markup.

  The enumeration plays some ten thousand pages, and a JavaScript context of its own for each is
  what would cost most. So the context is made once, and each page is the script run again in it
  over a new set of nodes: every `var` of the script is assigned afresh (all the page knows is in
  `state` and in the nodes), and nothing of the page before it is left. `a page played after
  another starts from nothing` in the first test file holds that.
*/
const nodes = new Map();
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
const never = () => new Promise(() => undefined);

export function panel() {
  nodes.clear();
  for (const [id, classes, text] of marked) nodes.set(id, new Element(classes, text));
  clock.now = 1000;
  script.runInContext(context);
  // A machine that answers nothing until the test says what it answers, and a socket that is open.
  context.api = never;
  context.state.connected = true;
  context.state.lastFrameAt = clock.now;
  return {
    context,
    node: (id) => {
      assert.ok(nodes.has(id), `the page has no #${id}`);
      return nodes.get(id);
    },
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

const speed = { motor_rpm: 0, output_rpm: 0, hertz: 0, g_load: 0, resultant_g: 1 };
const turning = (motor) => ({ motor_rpm: motor, output_rpm: motor / 49.79, hertz: motor / 27.6, g_load: 0, resultant_g: 1 });

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

// A manual session as one frame shows it, the applied target in motor rpm.
const manualFrame = (at, target) =>
  snapshot({
    at,
    mode: "manuel",
    phase: "hold",
    manual: {
      occupancy: "bench",
      occupancy_label: "BANC - personne a bord : NON",
      target: turning(target),
      ceiling: turning(300),
      min_run: turning(55),
      ramping: false,
      ramp_eta_s: null,
    },
  });

// The two starts: the click, the route it goes to, and the note under its button.
const STARTS = {
  true: {
    note: "manual-note",
    path: "/api/manual/start",
    press: (page) => {
      page.node("manual-bench").checked = true;
      page.node("manual-operator").value = "op";
      page.context.doManualStart();
    },
  },
  false: {
    note: "start-note",
    path: "/api/session/start",
    press: (page) => {
      page.context.state.profiles = [{ profile_id: "standard_30_min", name: "Standard", zone_low_bpm: 100, zone_high_bpm: 120 }];
      page.node("profile").value = "standard_30_min";
      page.node("operator").value = "op";
      page.context.doStart();
    },
  },
};

// The console's fixed sentences, as src/web/routes.py and src/control_surface.py write them.
export const START_PENDING =
  "demande refusee par la console : un demarrage est deja demande, et la machine ne l'a pas encore pris " +
  "ou est en train de l'armer. Rien n'a ete envoye.";
export const REQUEST_WAITING =
  "demande refusee par la console : une autre demande attend encore la machine (une demande a la fois), " +
  "rien n'a ete envoye. Reessayer dans un instant.";
export const CANCELLED = "un STOP est arrive avant que la machine prenne cette demande, rien n'a ete fait";
export const DRIVE_FAULT = "demarrage refuse : variateur en defaut (ObF, LFT 18)";

/*
  What the console does with its mailbox, as src/control_surface.py and the control step of
  src/local_panel.py do it, reduced to what bears on the note of a start:

  - one request waits at a time, and each one accepted is given an identifier, which its 202 and
    every event about it carry;
  - a start is accepted only when the surface reads idle AND the mailbox is empty. While another
    start waits or is being armed it is refused at once (`start_pending`), and so is one that
    finds a reset still waiting (`request_waiting`). It never takes the place of anything;
  - the surface keeps reading "starting" at the end of a tick that never saw the start, and
    "stopping" while a stop waits: it reads idle only when nothing accepted is left (`note_idle`).
    Only the refusal of a START frees it for another one (`note_refused`);
  - a stop is accepted whenever a session is starting or running, whatever waits: it takes the
    place, and the request it displaces is told so (`cancelled`);
  - a tick empties the mailbox, judges the request and says what became of it (`taken` or
    `refused`, under its identifier), takes the frame's instant AFTER that, ticks the runtime,
    publishes the frame, then reports the session running or idle, naming the start it came from;
  - requests are accepted while the loop awaits the drive: during the judging, and during the
    runtime's tick;
  - a reset is accepted when the mailbox is empty and no start is in flight, a target when a
    session runs and the mailbox is empty.

  Two things the loop does for reasons of its own are switches of the model: it refuses every
  start of one screen (a drive in fault), and it refuses a reset at rest (no fault to reset). The
  model is harder than the console on the end of a session: over in one tick. The real console
  shows a stop for a few seconds, also after a stop that displaced a start, and refuses a start
  for that long.

  Every message goes out in the order the console emits it, with what is true at that moment:
  the session the runtime runs, and which requests the loop has taken, refused, or never seen.
  That truth is kept apart from the identifiers the events carry: it is what a note is held
  against.
*/
function mailboxModel(manual, loop = {}) {
  const truth = { taken: [], refused: [], cancelled: [] };
  const model = { clock: 100, pending: null, starting: false, live: false, ending: false, session: null, from: null, target: 0, out: [], accepted: 0 };
  const reads = () => (model.ending ? "stopping" : model.live ? "running" : model.starting ? "starting" : "idle");
  const emit = (message) => {
    message.index = model.out.length;
    message.session = model.session;
    message.truth = { taken: truth.taken.slice(), refused: truth.refused.slice(), cancelled: truth.cancelled.slice() };
    model.out.push(message);
  };
  const publish = (kind, operator, detail, id) => emit({ event: { kind, at: model.clock, wall_clock: 0, operator, detail, request_id: id } });
  // What the page's request code hands over for a refusal at once. Not an Error: the page reads
  // these four fields and nothing else, and an Error built for each of them is most of the model's time.
  const conflict = (message, code, waiting = null) => ({ ok: false, error: { message, status: 409, code, waiting } });
  const accept = (request) => {
    model.clock += 0.004;
    model.accepted += 1;
    request.id = `m0de1-${model.accepted}`;
    model.pending = request;
  };

  // An HTTP request: the console's answer, after whatever it published for it.
  model.submit = (request) => {
    const state = reads();
    const waiting = model.pending;
    if (request.kind === "start") {
      if (state === "starting") return conflict(START_PENDING, "start_pending", model.from);
      if (state !== "idle") return conflict(`the machine is ${state}, not idle`, `machine_${state}`);
      if (waiting) return conflict(REQUEST_WAITING, "request_waiting", waiting.id);
      accept(request);
      model.starting = true;
      model.from = request.id;
      publish("start_requested", request.operator, request.published, request.id);
      return { ok: true, body: { kind: request.reply, operator: request.operator, detail: request.detail, at: model.clock, request_id: request.id } };
    }
    if (request.kind === "reset") {
      if (waiting) return conflict(REQUEST_WAITING, "request_waiting", waiting.id);
      if (state === "starting") return conflict(START_PENDING, "start_pending", model.from);
      accept(request);
      publish("fault_reset_requested", request.operator, "reset defaut", request.id);
      return { ok: true };
    }
    if (request.kind === "target") {
      if (state !== "running") return conflict(`no manual session is running (the machine is ${state})`, "no_manual_session");
      if (waiting) return conflict(REQUEST_WAITING, "request_waiting", waiting.id);
      accept(request);
      return { ok: true };
    }
    if (state === "idle") return conflict(`there is no session to end (the machine is ${state})`, "nothing_running");
    if (state === "stopping") return conflict(`the machine is already ${state}`, "already_stopping");
    accept(request);
    model.ending = true;
    publish("end_requested", request.operator, "stop", request.id);
    if (waiting) {
      // The stop took the place of a request the loop never saw.
      if (waiting.kind === "start") {
        model.starting = false;
        model.from = null;
      }
      truth.cancelled.push(waiting);
      publish("cancelled", waiting.operator, CANCELLED, waiting.id);
    }
    return { ok: true };
  };

  const refuse = (command, detail) => {
    if (command.kind === "start") {
      model.starting = false;
      model.from = null;
    }
    truth.refused.push(command);
    publish("refused", command.operator, detail, command.id);
  };
  const take = (command) => {
    truth.taken.push(command);
    publish("taken", command.operator, command.kind, command.id);
  };

  // One tick, in three steps, so that requests can be placed between them.
  model.take = () => {
    model.taken = model.pending;
    model.pending = null;
    model.clock += 0.02;
  };
  model.judge = () => {
    const command = model.taken;
    model.clock += 0.03;
    if (command && command.kind === "start") {
      if (loop.refusesStartsOf === command.screen) {
        refuse(command, DRIVE_FAULT);
      } else {
        model.session = command;
        take(command);
      }
    } else if (command && command.kind === "reset") {
      if (model.session) refuse(command, "reset refuse : mouvement encore commande (running)");
      else if (loop.refusesResets) refuse(command, "reset refuse : aucun defaut a acquitter (ready_to_switch_on)");
      else take(command);
    } else if (command && command.kind === "target") {
      if (model.session && manual) {
        model.target = 249;
        take(command);
      } else {
        refuse(command, "consigne refusee : pas de session manuelle");
      }
    } else if (command && command.kind === "stop") {
      model.session = null;
      model.target = 0;
      take(command);
    }
    model.clock += 0.01;
    // The frame's instant: taken once the request has been judged.
    model.instant = model.clock;
  };
  model.publish = (cycle) => {
    model.clock += 0.03;
    emit({ frame: { at: model.instant, running: Boolean(model.session), target: model.target }, cycle });
    if (model.session && !model.live) {
      model.live = true;
      model.starting = false;
      publish("session_running", "", "session running", model.from);
    } else if (!model.session) {
      // Idle is said when the last intention goes, and not while the request it stands for waits.
      const waiting = model.pending ? model.pending.kind : null;
      const starting = model.starting && waiting === "start";
      const ending = model.ending && waiting === "stop";
      if (model.live || model.starting !== starting || model.ending !== ending) {
        model.live = false;
        model.starting = starting;
        model.ending = ending;
        if (!starting && !ending) {
          publish("session_idle", "", "session idle", model.from);
          model.from = null;
        }
      }
    }
    model.clock = 100 + 0.2 * (cycle + 1);
  };
  return model;
}

const PLACES = ["before the tick", "during the judging", "during the runtime's tick"];

// One request of an interleaving, as the model takes it. `index` makes the words of a programme unique.
function familyRequest({ kind, screen }, index, manual) {
  const operator = kind === "start" ? `ecran ${screen}` : "un ecran";
  if (kind !== "start") {
    return { kind, operator };
  }
  const seconds = 1800 + 60 * (index + 1);
  return {
    kind,
    screen,
    operator,
    said: manual ? "manual_start bench" : `start standard_30_min over ${seconds} s`,
    reply: manual ? "manual_start" : "start",
    detail: manual ? "bench" : `standard_30_min over ${seconds} s`,
    published: manual ? "manuel bench" : "start standard_30_min",
  };
}

// What the note of a start taken reads at the end of a run: the request was the `index`-th of its interleaving.
export const takenBy = (index, manual) =>
  manual ? "demarrage pris par la machine : manual_start bench (cible 0)" : `demarrage pris par la machine : start standard_30_min over ${1800 + 60 * (index + 1)} s`;

export const describeRun = ({ dropped = [], late = [], loop = {}, cycles = 3 } = {}) =>
  (dropped.length ? `; frame of cycle ${dropped.map((cycle) => cycle + 1).join(" and ")} not delivered` : "") +
  (late.length ? `; frame of cycle ${late.map((cycle) => cycle + 1).join(" and ")} delivered after the events of the next cycle` : "") +
  (loop.refusesStartsOf ? `; the loop refuses the starts of screen ${loop.refusesStartsOf}` : "") +
  (loop.refusesResets ? "; the loop refuses a reset at rest" : "") +
  (cycles !== 3 ? `; over ${cycles} cycles` : "");

export const describeFamily = (requests, run = {}) =>
  requests
    .map(({ kind, screen, slot }) => `${kind}${kind === "start" ? ` by screen ${screen}` : ""} ${PLACES[slot % 3]} of cycle ${Math.floor(slot / 3) + 1}`)
    .join(", ") + describeRun(run);

/*
  One interleaving, in two steps.

  First the model alone: what each screen that asks for a start lives through, in order. Its own
  clicks and the console's answers to them, every event, and every frame the run delivers. A run
  can withhold the frame of a cycle (the console coalesced it away for a slow socket), or deliver
  it after the events of the next cycle (the console sends a socket its events first, so a frame
  still waiting is overtaken by them). Each message carries what was true when the console emitted
  it, and the place it was emitted at.
*/
function familyHistories(requests, manual, { dropped = [], late = [], loop = {}, cycles = 3 } = {}) {
  const model = mailboxModel(manual, loop);
  const histories = new Map();
  for (const { kind, screen } of requests) {
    if (kind === "start") histories.set(screen, []);
  }
  let sent = 0;
  let heldBack = null;
  const push = (message) => {
    for (const history of histories.values()) history.push(message);
  };
  const deliver = () => {
    for (; sent < model.out.length; sent += 1) {
      const message = model.out[sent];
      if (message.frame && dropped.includes(message.cycle)) continue;
      if (message.frame && heldBack) {
        push(heldBack);
        heldBack = null;
      }
      if (message.frame && late.includes(message.cycle)) heldBack = message;
      else push(message);
    }
  };
  const made = requests.map((request, index) => ({ ...familyRequest(request, index, manual), slot: request.slot }));
  // One quiet tick after the last: what was accepted last is judged and shown.
  for (let cycle = 0; cycle < cycles + 1; cycle += 1) {
    for (let place = 0; place < 3; place += 1) {
      for (const request of made) {
        if (request.slot !== cycle * 3 + place) continue;
        // A click is sent before the console publishes anything for it, and answered after.
        if (request.kind === "start") histories.get(request.screen).push({ press: true });
        const outcome = model.submit(request);
        deliver();
        if (request.kind === "start") histories.get(request.screen).push({ answer: outcome, request });
      }
      if (place === 0) model.take();
      else if (place === 1) model.judge();
      else model.publish(cycle);
      deliver();
    }
  }
  if (heldBack) push(heldBack);
  return histories;
}

/*
  Then the page code: one real page lives through one history, and its note is held, after every
  message, against what the model knows.

  The invariant. Let "its request" be the last start of this screen the console answered 202. The
  note says:

  - "pris par la machine", with the words of its request, IF AND ONLY IF the loop took that
    request, the console has said so, and nothing delivered since shows its session over (a
    `session_idle`, or a frame emitted after the answer that shows no session);
  - "refus de la machine", with the loop's reason, IF AND ONLY IF the loop refused that request;
  - "demande annulee" IF AND ONLY IF a stop took its place before the loop saw it;
  - the console's own sentence, after a click the console refused at once (409), until the answer
    about its request arrives;
  - "demande envoyee" while the console has said nothing of its request yet;
  - and never that the screen does not know: on a socket that stays up it always knows.

  "If and only if" is held in both directions at every message. What the note must read is worked
  out from the model's truth (the requests the loop took, refused or never saw, and the session
  the runtime runs), never from the identifiers the events carry, and compared with the note.

  Between the end of a session and the first message that says so, the note still says "pris" of
  the session that has just ended: that is the console's own delay, and the request was taken.
  Returns what the note read at the end.
*/
async function liveFamilyHistory(history, screen, manual, where) {
  const start = STARTS[manual];
  const page = panel();
  const note = page.node(start.note);
  // The request the note speaks of, what the note must read, and what the console has said of it.
  let mine = null;
  let expected = { reads: "nothing" };
  let takenAt = -1;
  let answered = {};
  let newest = { index: -1 };
  // Whether this message is the first that carries `what` as true of this screen's request.
  const became = (what, step) => {
    if (!mine || answered[what] || !step.truth[what].includes(mine)) return false;
    answered[what] = true;
    return true;
  };
  const classOf = (text) => {
    if (text === "") return "nothing";
    if (text.startsWith("demande envoyee : ")) return "sent";
    if (text.startsWith("demarrage pris par la machine : ")) return "taken";
    if (text.startsWith("refus de la machine (")) return "refused";
    if (text.startsWith("demande annulee (")) return "cancelled";
    return "the console's sentence";
  };
  const hold = (step) => {
    const text = note.textContent;
    const saying = () => `${where()}: screen ${screen} says "${text}"`;
    if (text.includes("ne sait pas")) assert.fail(`${saying()} on a socket that never dropped`);
    if (classOf(text) !== expected.reads) assert.fail(`${saying()}, expected: ${expected.reads}`);
    if ((expected.reads === "sent" || expected.reads === "taken") && !text.includes(mine.said)) {
      assert.fail(`${saying()}, not the words of its request (${mine.said})`);
    }
    if ((expected.reads === "refused" || expected.reads === "the console's sentence") && !text.endsWith(expected.words)) {
      assert.fail(`${saying()}, expected ${expected.words}`);
    }
    if (expected.reads === "cancelled" && !text.endsWith(") : " + CANCELLED)) assert.fail(`${saying()}, expected ${CANCELLED}`);
    if (!step.truth) return;
    // And against the model's truth, which no identifier went through: the newest this screen
    // has been sent (a frame delivered late carries what was true when it was emitted).
    if (step.index > newest.index) newest = step;
    const { taken, refused, cancelled } = newest.truth;
    if (expected.reads === "taken") {
      if (!taken.includes(mine)) assert.fail(`${saying()} of a request the loop never took`);
      if (step.frame && step.index > takenAt && step.session !== mine) {
        assert.fail(`${saying()} while the frame it has just received shows another session, or none`);
      }
    }
    if (expected.reads === "refused" && !refused.includes(mine)) assert.fail(`${saying()} of a request the loop did not refuse`);
    if (expected.reads === "cancelled" && !cancelled.includes(mine)) assert.fail(`${saying()} of a request no stop displaced`);
    if (expected.reads === "sent" && (taken.includes(mine) || refused.includes(mine) || cancelled.includes(mine))) {
      assert.fail(`${saying()} of a request the console has answered`);
    }
  };
  let answer = null;
  for (const step of history) {
    if (step.press) {
      answer = deferred();
      const held = answer.promise;
      page.context.api = (path) => (path === start.path ? held : never());
      start.press(page);
      continue;
    }
    if (step.event) {
      page.context.addEvent(step.event);
      // The message the console emitted as the model's truth about this screen's request changed
      // is the console's answer about it. Which message that is, is read off the truth alone.
      if (became("taken", step)) {
        expected = { reads: "taken" };
        takenAt = step.index;
      } else if (became("refused", step)) {
        expected = { reads: "refused", words: ") : " + step.event.detail };
      } else if (became("cancelled", step)) {
        expected = { reads: "cancelled" };
      } else if (step.event.kind === "session_idle" && expected.reads === "taken") {
        expected = { reads: "nothing" };
      }
    } else if (step.frame) {
      const { at, running, target } = step.frame;
      page.frame(!running ? snapshot({ at }) : manual ? manualFrame(at, target) : snapshot({ at, mode: "seance", phase: "baseline" }));
      if (expected.reads === "taken" && step.index > takenAt && !running) {
        expected = { reads: "nothing" };
      }
    } else {
      if (step.answer.ok) {
        answer.resolve(step.answer.body);
        mine = step.request;
        answered = {};
        expected = { reads: "sent" };
      } else {
        answer.reject(step.answer.error);
        expected = { reads: "the console's sentence", words: step.answer.error.message };
      }
      // The page handles the answer in the microtasks that follow: no turn of the event loop is needed.
      for (let hop = 0; hop < 4; hop += 1) await null;
    }
    hold(step);
  }
  return note.textContent;
}

// What tells two histories apart for a page and for the invariant: everything the page receives,
// with the identifiers named in the order this screen meets them, and what is true of this
// screen's own requests at each message.
function familyHistoryKey(history, screen) {
  const names = new Map();
  const name = (id) => {
    if (id === null || id === undefined) return null;
    if (!names.has(id)) names.set(id, names.size);
    return names.get(id);
  };
  const own = (requests) => {
    let named = "";
    for (const request of requests) if (request.screen === screen) named += name(request.id) + "|";
    return named;
  };
  // Where a message was emitted matters only against the others of this history: its rank.
  const places = [];
  for (const step of history) if (step.index !== undefined) places.push(step.index);
  places.sort((a, b) => a - b);
  const parts = [];
  for (const step of history) {
    if (step.press) {
      parts.push("P");
    } else if (step.answer) {
      const { ok, body, error } = step.answer;
      parts.push(ok ? `A${body.kind},${body.detail},${body.at},${name(body.request_id)}` : `R${error.message},${error.code},${name(error.waiting)}`);
    } else {
      const { taken, refused, cancelled } = step.truth;
      const runs = step.session ? (step.session.screen === screen ? name(step.session.id) : "another") : null;
      const truth = `${own(taken)};${own(refused)};${own(cancelled)};${runs};${places.indexOf(step.index)}`;
      if (step.frame) {
        parts.push(`F${step.frame.at},${step.frame.running},${step.frame.target};${truth}`);
      } else {
        const { kind, at, operator, detail, request_id: id } = step.event;
        parts.push(`E${kind},${at},${operator},${detail},${name(id)};${truth}`);
      }
    }
  }
  return parts.join("\n");
}

/*
  Plays one interleaving: each screen's history on a real page. A history already lived through,
  message for message and with the same truth behind it, is not played again (`played` keeps what
  its note read): the page has no other input. Returns what each note read at the end.
*/
export async function playFamily(requests, manual, run = {}, played = new Map()) {
  const notes = new Map();
  for (const [screen, history] of familyHistories(requests, manual, run)) {
    const key = `${manual}\n${familyHistoryKey(history, screen)}`;
    if (!played.has(key)) {
      played.set(key, await liveFamilyHistory(history, screen, manual, () => describeFamily(requests, run)));
    }
    notes.set(screen, played.get(key));
  }
  return notes;
}

// Every interleaving of `count` requests over `cycles` cycles: their kinds, where each falls, and
// which screen asks for each start (screens are named in the order they first appear).
export function* familyInterleavings(count, cycles = 3, kinds = ["start", "reset", "target", "stop"]) {
  const slots = cycles * 3;
  function* build(prefix, screens) {
    if (prefix.length === count) {
      if (prefix.some((request) => request.kind === "start")) yield prefix;
      return;
    }
    const from = prefix.length ? prefix[prefix.length - 1].slot : 0;
    for (let slot = from; slot < slots; slot += 1) {
      for (const kind of kinds) {
        if (kind !== "start") {
          yield* build([...prefix, { kind, slot }], screens);
          continue;
        }
        for (let screen = 1; screen <= screens + 1; screen += 1) {
          yield* build([...prefix, { kind, slot, screen }], Math.max(screens, screen));
        }
      }
    }
  }
  yield* build([], 0);
}

// The orderings the reviews found, each of them one of the interleavings of the enumeration.
export const FAMILY_NAMED = {
  twoStarts: [{ kind: "start", slot: 1, screen: 1 }, { kind: "start", slot: 3, screen: 2 }],
  threeRequests: [{ kind: "reset", slot: 0 }, { kind: "start", slot: 1, screen: 1 }, { kind: "start", slot: 4, screen: 2 }],
  startStopStart: [{ kind: "start", slot: 0, screen: 1 }, { kind: "stop", slot: 3 }, { kind: "start", slot: 6, screen: 2 }],
  startCancelled: [{ kind: "start", slot: 1, screen: 1 }, { kind: "stop", slot: 2 }, { kind: "start", slot: 8, screen: 1 }],
};

/*
  Every interleaving of each run of `runs`, played. A run is what the console and the socket do
  around the requests ({dropped, late, loop, cycles}), how many requests (`counts`), which starts
  (`manual`: [false] for programme starts, whose words are their own; [true] for manual ones), and
  optionally which interleavings of them (`only`).

  Returns what was played: how many interleavings, how many distinct page histories, what the
  notes read at the end, and the interleavings of the plain run that were among them.
*/
export async function enumerate(runs) {
  const lived = new Map();
  const seen = new Set();
  const ends = { taken: 0, refused: 0, cancelled: 0, "at once": 0, sent: 0, nothing: 0 };
  let played = 0;
  const began = Date.now();
  for (const { counts, manual: modes, run, only = () => true } of runs) {
    for (const count of counts) {
      for (const requests of familyInterleavings(count, run.cycles)) {
        if (!only(requests)) continue;
        for (const manual of modes) {
          const notes = await playFamily(requests, manual, run, lived);
          played += 1;
          for (const text of notes.values()) {
            if (text === "") ends.nothing += 1;
            else if (text.startsWith("demande envoyee")) ends.sent += 1;
            else if (text.startsWith("demarrage pris par la machine")) ends.taken += 1;
            else if (text.startsWith("refus de la machine")) ends.refused += 1;
            else if (text.startsWith("demande annulee")) ends.cancelled += 1;
            else ends["at once"] += 1;
          }
          if (!manual && !describeRun(run)) seen.add(describeFamily(requests));
        }
      }
    }
  }
  return { played, histories: lived.size, ends, seen, ms: Date.now() - began };
}

export const summary = ({ played, histories, ends, ms }) =>
  `${played} interleavings, ${histories} distinct page histories played; at the end "pris" ${ends.taken}, ` +
  `"refus" ${ends.refused}, "annulee" ${ends.cancelled}, refused at once ${ends["at once"]}, nothing left to say ${ends.nothing}; in ${ms} ms`;

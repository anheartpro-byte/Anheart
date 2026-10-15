/*
  The operator page. Plain JavaScript, no framework, no build step, no CDN:
  the Pi serves this offline and a dashboard that fails to render because a
  script did not download is a dashboard that is absent at the worst moment.

  Three rules this file exists to enforce on the display, all of them from the
  design and none of them cosmetic:

  1. NOTHING is shown as live unless it demonstrably is. Two independent
     staleness checks run: the server's own age fields, and a local watchdog
     that marks the whole screen stale when no frame has arrived for
     STALE_FRAME_MS. A socket can stay open while the control loop behind it
     has stopped, and that is exactly the case where a frozen page reads as a
     calm one. The sensor pages apply the same idea to /api/sensors: a channel
     whose `at` stops advancing is greyed and struck, whatever it last said.
  2. "Is it stopped" is answered by the MEASURED output speed, never by the
     setpoint. A commanded zero on a coasting mass is not a measured zero.
  3. Motor rpm, output rpm, Hz and g are rendered TOGETHER wherever a speed is
     shown, because the gearbox ratio is 49.79 and any one of them alone hides
     a fiftyfold error.

  The emergency stop calls the API immediately, with no confirmation of any
  kind. That is deliberate and must stay that way.

  Drawing: every canvas is redrawn from one requestAnimationFrame callback and
  only when its data changed, so a Raspberry Pi browser is not repainting six
  waveforms sixty times a second.
*/

"use strict";

/* ------------------------------------------------------------------ state */

var STALE_FRAME_MS = 2000;      // no frame for this long: the screen is not live
var RECONNECT_MS = 1500;        // socket retry interval
var ESTOP_ANSWER_MS = 2000;     // an E-STOP request with no answer for this long is said so, loudly
var STOP_ANSWER_MS = 2000;      // and so is a STOP request
var PRESENCE_MS = 5000;         // attendant ping; the rule freezes after 60 s
var STATUS_MS = 5000;           // status refresh (run state, verdicts, attestation)
var STATUS_STALE_MS = 12000;    // two refreshes missed: what the status last said is not the present
var PANEL_MS = 1000;            // console link panel refresh
var PANEL_STALE_MS = 3500;      // no /api/panel answer for this long: what it last said is not the present
var SENSORS_MS = 1000;          // /api/sensors poll
var ROSTER_MS = 10000;          // riders' list refresh (the console asks the dashboard once a minute)
var SENSOR_STALE_MS = 3500;     // a channel whose `at` has not moved for this long is stale
var ECG_CAPACITY = 1600;        // samples kept for the trace (~6 s at 250 Hz)
var STANDSTILL_RPM = 0.05;      // below this, the output shaft is called stopped
var STANDARD_G = 9.80665;       // m/s2, for the 0.1 Gr manual step
var LOOP_ANSWER_S = 1;          // the loop empties its mailbox every 0.2 s: past this, on its clock, no answer is coming
var OPERATOR_INPUT_IDS = ["manual-operator", "operator", "ack-operator", "attest-operator", "service-operator"];

var state = {
  token: "",
  socket: null,
  connected: false,
  lastFrameAt: 0,
  snapshot: null,
  zone: null,            // {low, high} from the chosen profile, for the band
  profiles: [],
  roster: null,          // the last /api/roster answer: names live here, in this page's memory only
  rosterShown: null,     // the riders the list shows, as one string: rebuilt only when it changes
  riderLost: false,      // the rider chosen here is no longer in the list
  ecg: [],               // numbers, with nulls marking a discontinuity
  ecgSeq: 0,
  events: [],
  reconnectTimer: null,
  motionEnabled: true,   // from /api/panel; false on the read-only console
  programsEnabled: true, // from /api/panel; false until milestone M5
  panel: null,           // the last /api/panel answer
  panelAt: 0,            // performance.now() when it came
  status: null,          // the last /api/status answer
  statusAt: 0,           // performance.now() when it came
  statusSent: 0,         // /api/status requests sent so far
  statusShownSent: 0,    // statusSent when `status` arrived: a request sent later is answered after it
  statusBoot: 0,         // machineBoot when `status` arrived
  machineBoot: 0,        // restarts of the machine's clock seen so far: a later one outdates an earlier one
  estopReceipt: null,    // this page's own E-STOP, until a frame or a status taken after it takes over
  estopReceiptSent: 0,   // statusSent when that receipt arrived: later requests are answered after the latch
  estopShown: false,     // what the emergency-stop banner shows; kept while nothing newer can tell
  estopFrame: null,      // what the frames say about it, and since when: {says, since, at, boot, asked}
  stream: 0,             // telemetry sockets opened so far: the frames of one socket come from one console
  estopLatch: null,      // the latched stop the frames last showed, until they show it released (see noteConsoleRun): {rule, boot, stream, sent, count, restarted, instance}
  statusLatch: null,     // the latched stop the status last showed, until a status shows it released (see noteStatusLatch): {rule, boot, sent, count, restarted, instance}
  estopAsked: null,      // this page's E-STOP with no proof of a latch yet: a doubt (see openDoubt)
  stopAsked: null,       // this page's STOP with no answer and no proof it was taken: a doubt as well
  view: "console",
  sensorKind: null,      // the sensor shown on the per-sensor page
  sensors: {},           // kind -> {row, lastAt, advancedAt, stale, nav, card}
  sensorOrder: [],       // kinds, in configuration order
  sensorsOkAt: 0,        // performance.now() of the last successful poll
  manualDraft: null,     // the target being edited, output rpm; null = follow the machine
  riseHeld: false,       // whether the heart rate holds a manual rise, or may: the console says so, or no longer answers
  asked: {},             // a request this page sent, until the console has said what became of it: kind -> {id, at, said, stream, rpm, lost}
  held: {},              // the note saying one was taken, for as long as that is true: kind -> {id, said, at, stream, rpm, sure, cleared, text}
  fates: [],             // the console's last answers about requests, this page's or not: {id, fate, at, wall_clock, detail, stream}
  sessionsOver: [],      // the starts whose session the console has said over (`session_idle`), newest last
  streamOpened: null,    // the first message of the current socket: {stream, sent}, `sent` being statusSent then
  ackGiven: null,        // the acknowledgement this page gave, until frames show whether it held: {at, cleared, text}
  ackTaken: null,        // the note saying the machine took one back, for as long as the frames show that latch: {rule, waits}
  latchSeen: {},         // the latched rule each source last reported: a rule new to all of them is a new latch
  verdictShown: "",      // the frames' verdict as the status lines last showed it
  dirty: {},             // canvas id -> true when it needs a redraw
};

/* ----------------------------------------------------------- tiny helpers */

function el(id) {
  return document.getElementById(id);
}

function show(node, visible) {
  node.classList.toggle("hidden", !visible);
}

function text(node, value) {
  node.textContent = value;
}

function make(tag, className, content) {
  var node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (content !== undefined) {
    node.textContent = content;
  }
  return node;
}

/** A number for display, or an em dash. Never "NaN", never "null". */
function num(value, digits) {
  if (value === null || value === undefined || !isFinite(value)) {
    return "-";
  }
  return value.toFixed(digits === undefined ? 1 : digits);
}

function secs(value) {
  if (value === null || value === undefined || !isFinite(value)) {
    return "-";
  }
  var total = Math.max(0, Math.round(value));
  var mins = Math.floor(total / 60);
  var rest = total % 60;
  return mins + ":" + (rest < 10 ? "0" : "") + rest;
}

function pill(node, label, kind) {
  text(node, label);
  node.className = "pill" + (kind ? " pill-" + kind : "");
}

/** Fill a <dl> from [[label, value], ...]. One place, so every grid matches. */
function grid(node, rows) {
  node.textContent = "";
  rows.forEach(function (row) {
    var dt = document.createElement("dt");
    dt.textContent = row[0];
    var dd = document.createElement("dd");
    dd.textContent = row[1];
    if (row[2]) {
      dd.className = row[2];
    }
    node.appendChild(dt);
    node.appendChild(dd);
  });
}

/**
 * The four-way speed rendering. Used for every speed on the page, so the
 * simultaneous display cannot be forgotten at one call site.
 */
function speedRows(view) {
  if (!view) {
    return [["moteur", "-"], ["sortie", "-"], ["variateur", "-"], ["Gc", "-"], ["Gr", "-"]];
  }
  return [
    ["moteur", num(view.motor_rpm, 0) + " tr/min"],
    ["sortie", num(view.output_rpm, 2) + " tr/min"],
    ["variateur", num(view.hertz, 2) + " Hz"],
    ["Gc", num(view.g_load, 3) + " g (centripete)"],
    ["Gr", num(view.resultant_g, 3) + " g (resultant)"],
  ];
}

/** The same four figures on one line, for grids that hold several speeds. */
function speedLine(view) {
  if (!view) {
    return "-";
  }
  return num(view.output_rpm, 2) + " tr/min · " + num(view.motor_rpm, 0) + " moteur · " +
    num(view.hertz, 2) + " Hz · Gc " + num(view.g_load, 3) + " · Gr " + num(view.resultant_g, 3);
}

function rememberOperator(value) {
  var name = value.trim();
  try {
    window.sessionStorage.setItem("anheart-operator", name);
  } catch {
    // This page can still attribute a command when storage is unavailable.
  }
  return name;
}

/** The shared operator name, retained in this tab across a reload. */
function operatorName() {
  for (var i = 0; i < OPERATOR_INPUT_IDS.length; i += 1) {
    var value = document.getElementById(OPERATOR_INPUT_IDS[i]).value.trim();
    if (value) return value;
  }
  try {
    return window.sessionStorage.getItem("anheart-operator") || "";
  } catch {
    return "";
  }
}

/* -------------------------------------------------------------------- api */

function api(path, options) {
  var init = options || {};
  var headers = { "Content-Type": "application/json" };
  if (state.token) {
    headers["X-Anheart-Token"] = state.token;
  }
  return fetch(path, {
    method: init.method || "GET",
    headers: headers,
    body: init.body ? JSON.stringify(init.body) : undefined,
    cache: "no-store",
  }).then(function (response) {
    if (response.status === 204) {
      return null;
    }
    return response.json().catch(function () {
      return null;
    }).then(function (body) {
      if (!response.ok) {
        var detail = body && body.detail ? body.detail : response.status + " " + response.statusText;
        var refusal = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        // The console answered. A request that got no answer at all throws with no status.
        refusal.status = response.status;
        // What a start, a stop, a target or a reset refused at once carries next to its sentence:
        // a code that does not change with the wording, and the request found in the way, if any.
        refusal.code = body && body.code ? body.code : null;
        refusal.waiting = body && body.waiting_request_id ? body.waiting_request_id : null;
        throw refusal;
      }
      return body;
    });
  });
}

/* The latest session record, as a file. Fetched rather than linked: a link
   cannot carry the token header. */
function exportRecord() {
  var note = el("record-note");
  var headers = {};
  if (state.token) {
    headers["X-Anheart-Token"] = state.token;
  }
  api("/api/records").then(function (body) {
    if (!body.records.length) {
      throw new Error(
        body.recording ? "aucun enregistrement sur cette machine" : "cette console n'enregistre pas"
      );
    }
    var name = body.records[0].name;
    return fetch("/api/records/" + encodeURIComponent(name) + "/archive", {
      headers: headers,
      cache: "no-store",
    }).then(function (response) {
      if (!response.ok) {
        return response.json().then(function (refusal) {
          throw new Error(refusal && refusal.detail ? refusal.detail : String(response.status));
        });
      }
      return response.blob();
    }).then(function (archive) {
      var link = document.createElement("a");
      link.href = URL.createObjectURL(archive);
      link.download = name + ".tar.gz";
      link.click();
      URL.revokeObjectURL(link.href);
      ok(note, "exporte : " + name + ".tar.gz");
    });
  }).catch(function (error) {
    problem(note, error);
  });
}

function problem(node, error) {
  node.className = "note note-bad";
  text(node, error && error.message ? error.message : String(error));
}

function ok(node, message) {
  node.className = "note";
  text(node, message);
}

/* ------------------------------------------------------------------ views */

var VIEWS = ["console", "sensors", "sensor", "run", "setup", "safety"];

/*
  One page at a time. `kind` names the sensor for the "sensor" page. The
  footer stops are outside every view, so no navigation can hide them.
*/
function showView(name, kind) {
  state.view = name;
  if (name === "sensor") {
    state.sensorKind = kind || state.sensorKind;
  }
  // The tab is named after the page shown: by the label of its entry in the navigation, and for a
  // sensor by the title its page carries.
  var shown = state.sensors[state.sensorKind];
  var title = name !== "sensor" ? el("nav-" + name).textContent : shown ? sensorTitle(shown.row) : state.sensorKind;
  document.title = title + " - AnHeart";
  VIEWS.forEach(function (view) {
    show(el("view-" + view), view === name);
  });
  el("nav-console").classList.toggle("nav-active", name === "console");
  el("nav-sensors").classList.toggle("nav-active", name === "sensors");
  el("nav-run").classList.toggle("nav-active", name === "run");
  el("nav-setup").classList.toggle("nav-active", name === "setup");
  el("nav-safety").classList.toggle("nav-active", name === "safety");
  state.sensorOrder.forEach(function (sensorKind) {
    var entry = state.sensors[sensorKind];
    entry.nav.classList.toggle("nav-active", name === "sensor" && sensorKind === state.sensorKind);
  });
  if (name === "sensor") {
    renderSensorPage();
  }
  markAllDirty();
  closeNav();
  if (name === "setup" || name === "safety") {
    loadStatus().catch(function () {
      /* shown by the banner and the token note */
    });
  }
}

function openNav() {
  el("sidebar").classList.add("open");
  el("nav-toggle").setAttribute("aria-expanded", "true");
}

function closeNav() {
  el("sidebar").classList.remove("open");
  el("nav-toggle").setAttribute("aria-expanded", "false");
}

/* -------------------------------------------------------- the loud banner */

/*
  What each source feeds: the large numbers, and every chip that goes green,
  amber or red on its word. When a source goes quiet, all it fed is struck
  and greyed together. A number struck next to a chip still saying "good",
  "a l'arret" or "acquisition" in green is a page that still vouches for the
  machine.

  The frames feed most of it. The link panel and the status are asked over
  HTTP, each at its own pace, and are judged on their own answers: with the
  socket down they may still be current, and with the socket up they may not.
*/
var FRAME_FED = [
  "hr", "measured-output", "setpoint-output", "console-hr", "console-output",
  "run-mode", "side-motion", "side-safety", "mobile-mode", "mobile-motion",
  "console-hr-quality", "hr-quality", "console-motion", "motion",
  "console-drive-state", "drive-state", "setpoint-confirmed",
  "safety-action", "run-safety-action", "phase", "manual-state",
  "console-ecg-state", "ecg-state",
];
var PANEL_FED = ["console-mode", "console-ecg-link", "dashboard-link", "mobile-dashboard", "wall-clock", "service-state"];
var STATUS_FED = ["run-state", "attest-state"];

function markStale(ids, stale) {
  ids.forEach(function (id) {
    el(id).classList.toggle("stale", stale);
  });
}

/*
  The socket dropping, or the frames stopping, is shown as a full-width red
  banner and every live number is struck through at the same time. Both, not
  one: the banner says "the page is not live", the struck values say which
  numbers you must not believe.
*/
function refreshLiveness() {
  var fresh = state.connected && performance.now() - state.lastFrameAt < STALE_FRAME_MS;
  var reworded = false;
  if (!fresh) {
    var detail = el("banner-detail");
    var notice = state.connected
      ? "la liaison est ouverte mais aucune donnee depuis " +
        Math.round((performance.now() - state.lastFrameAt) / 1000) +
        " s - la machine tourne peut-etre encore"
      : "la liaison avec la machine est coupee - la machine tourne peut-etre encore";
    reworded = detail.textContent !== notice;
    text(detail, notice);
  }
  // After its text: the banner is measured as it reads, not as it read last time.
  showBanner(el("banner"), !fresh, reworded);
  renderEstopUnanswered();
  renderStopUnanswered();
  renderDriveFault();
  markStale(FRAME_FED, !fresh);
  markStale(PANEL_FED, performance.now() - state.panelAt >= PANEL_STALE_MS);
  markStale(STATUS_FED, performance.now() - state.statusAt >= STATUS_STALE_MS);
  if (fresh) {
    pill(el("link-state"), "en direct", "good");
  } else {
    pill(el("link-state"), state.connected ? "donnees figees" : "hors ligne", "bad");
  }
  var phase = state.snapshot ? shownPhase(state.snapshot) : "";
  text(
    el("footer-status"),
    (state.connected ? "liaison ouverte" : "liaison coupee") +
      (state.snapshot ? " · " + state.snapshot.mode : "") +
      (phase ? " · " + phase : "")
  );
  refreshSensorStaleness();
}

/*
  What one frame says about a latched emergency stop: "latched", "clear", or
  "silent" when it cannot say.

  go_silent outranks an emergency stop and takes its place as the verdict a
  frame carries, so a frame under go_silent hides any stop latched behind it.
  It also refuses every acknowledgement: a stop that was latched when
  go_silent took over stays latched for as long as that console runs.
*/
function frameOpinion(snapshot) {
  var standing = snapshot.safety;
  if (standing && standing.latched && standing.action === "quick_stop") {
    return "latched";
  }
  return standing && standing.action === "go_silent" ? "silent" : "clear";
}

/*
  The latched quick stop a /api/status answer shows held BEHIND go_silent, or
  null. go_silent takes the place of whatever was latched before it, and the
  console says what that was (`silenced`): the stop of a rule, or of the
  runtime, that nobody acknowledged and that nothing can acknowledge any more.
  A screen that watched the sequence had seen that stop; one opened afterwards
  reads it here. An answer without the field shows nothing either way.
*/
function silencedStop(status) {
  var behind = status.silenced;
  return behind && behind.latched && behind.action === "quick_stop" ? behind : null;
}

/*
  What a /api/status answer says about it: "latched" or "clear". An answer can
  always say, go_silent or not: it carries the latch itself, whoever set it
  (`supervisor_estop`, and the interface's flag `estop_latched`), and the stop
  that go_silent replaced (silencedStop). Under go_silent an answer that shows
  none of them says that no emergency stop is held behind it.
*/
function statusOpinion(status) {
  var standing = status.standing;
  var stopped = Boolean(standing && standing.latched && standing.action === "quick_stop");
  return estopLatched(status) || stopped || Boolean(silencedStop(status)) ? "latched" : "clear";
}

/*
  What the frames say about a latched emergency stop, kept with the machine's
  own clock: `at` is the instant of the latest frame, `since` that of the
  first frame saying the same. Frames arrive in the order they were taken, so
  a clock that has gone back is a machine that restarted (machineBoot).

  A frame also releases the receipt of this page's own E-STOP, once one taken
  after the latch can speak. Only a frame that has just arrived does: the last
  one of a socket that has gone quiet proves nothing about a stop latched
  since.
*/
function noteEstopFrame(snapshot) {
  var last = state.estopFrame;
  if (last && last.boot === state.machineBoot && snapshot.at < last.at) {
    state.machineBoot += 1;
  }
  var says = frameOpinion(snapshot);
  var same = Boolean(last) && last.says === says && last.boot === state.machineBoot;
  state.estopFrame = {
    says: says,
    since: same ? last.since : snapshot.at,
    at: snapshot.at,
    boot: state.machineBoot,
    asked: same && last.asked,
  };
  if (state.estopReceipt && says !== "silent" && snapshot.at >= state.estopReceipt.at) {
    state.estopReceipt = null;
  }
  var latch = state.estopLatch;
  if (says === "latched" && !(latch && latch.rule === snapshot.safety.rule && latch.stream === state.stream)) {
    // Which stop it is, and which console showed it: the one at the other end of this socket,
    // by the name that run of the console gives itself in every frame.
    state.estopLatch = {
      rule: snapshot.safety.rule,
      boot: state.machineBoot,
      stream: state.stream,
      sent: state.statusSent,
      count: null,
      restarted: false,
      instance: snapshot.instance || null,
    };
  } else if (says === "clear") {
    // The frames themselves have shown it released.
    state.estopLatch = null;
  }
}

/*
  The latched stop this page was last shown, by the frames (estopLatch) or by
  the status (statusLatch), when go_silent then stands without the page having
  been shown it released: may the status speak for it?

  For the emergency stop itself, yes. The status carries that latch
  (statusOpinion), go_silent or not, so it says whether the stop is still
  held.

  For any other latched quick stop, a rule's or the runtime's own, an answer
  that still shows it behind go_silent says so itself (silencedStop): that is
  "latched", and the banner stays on it. An answer under go_silent that shows
  none is not taken, alone, for a release. Nothing is acknowledged under
  go_silent: from the console that showed the latch, such an answer can only
  mean an acknowledgement this screen never saw, given just before go_silent,
  and a banner that went on it would go as the machine reaches its most severe
  state. So that banner stays for as long as it is the same console that
  showed the latch, whichever of the frames and the status showed it. It goes
  with a console that restarted, which holds nothing of what the previous one
  held, and only when the restart is proven. The proof is one of:

  - the machine's clock restarted after the latch was shown (machineBoot); or
  - a status answer asked after the latch was shown names another run of the
    console than the one that showed it (anotherRun). Every frame and every
    status answer carries the name its console drew when it started
    (`instance`), which no later console draws again: this is the certain
    proof, and the two below are what is left for an answer that carries no
    name; or
  - the count of snapshots the console has published, which only ever rises
    while it runs (`counters` of the status), went down. For a latch the
    frames showed, remembered with the socket that delivered it (one socket,
    one console): between two status answers that were both asked after that
    socket first showed it. Asked then, the first comes from the console that
    showed it or a later one, and the second from a later one still. For a
    latch the status showed: between that answer, which the console holding
    the latch wrote, and one asked after it.

  Equal counts prove nothing: two answers within one tick, or a loop that has
  stalled, give the same count. A frame stream that broke and came back proves
  no restart either: the link can drop under a console that keeps running.
  And a latch shown again, over a new socket or by a later status, is the own
  latch of the console that shows it: the proof starts again from there.
  Unproven, the banner stays, until the frames show no verdict, a status shows
  the stop released outside go_silent, or the page is reloaded.

  Every proof holds for one console behind the address the page talks to,
  which is how the console is run: with two behind it, an answer from the
  other would read as a restart.
*/
function noteConsoleRun(status, sent) {
  var published = status.counters[2];
  [state.estopLatch, state.statusLatch].forEach(function (latch) {
    if (latch && sent > latch.sent) {
      latch.restarted =
        latch.restarted || (latch.count !== null && published < latch.count) || anotherRun(latch, status);
      latch.count = published;
    }
  });
}

/*
  Whether a status answer was written by another run of the console than the
  one that showed a latch. Only for an answer asked after the latch was shown
  (the caller's test): the console that showed it was running then, one
  console answers at a time, and a run that has ended does not answer again.
  So an answer asked from then on under another name comes from a console
  started since, which holds nothing of what the first one held. Without a
  name on either side nothing is proven.
*/
function anotherRun(latch, status) {
  return Boolean(latch.instance) && Boolean(status.instance) && status.instance !== latch.instance;
}

/*
  A status answer that shows a latched stop is remembered as the frames' is:
  which stop, the name of the run that wrote the answer, its count of
  snapshots then, and the machine's clock. Each such answer is the console of
  that moment showing its own latch, standing or held behind go_silent, so it
  replaces what was remembered. An answer that shows no latched stop and no
  go_silent has seen it released: only an acknowledgement, or a console that
  restarted, takes a latched quick stop away without go_silent. Under
  go_silent it is kept, for statusSpeaksForLatch to judge.
*/
function underGoSilent(status) {
  return Boolean(status.standing) && status.standing.action === "go_silent";
}

function noteStatusLatch(status, sent) {
  if (statusOpinion(status) === "latched") {
    // Which stop: the emergency stop, else the one go_silent stands over, else the standing one.
    var behind = silencedStop(status);
    state.statusLatch = {
      rule: estopLatched(status) ? "operator_estop" : behind ? behind.rule : status.standing.rule,
      boot: state.machineBoot,
      sent: sent,
      count: status.counters[2],
      restarted: false,
      instance: status.instance || null,
    };
  } else if (!underGoSilent(status)) {
    state.statusLatch = null;
  }
}

function statusSpeaksFor(latch) {
  return !latch || latch.rule === "operator_estop" || latch.restarted || state.machineBoot > latch.boot;
}

function statusSpeaksForLatch() {
  return statusSpeaksFor(state.estopLatch) && statusSpeaksFor(state.statusLatch);
}

/*
  Which of the latest frame and the status on screen the machine wrote last.
  Both carry its clock: a frame the instant its tick began (its verdict is
  read at the end of that tick, so it is never older than `at`), a status
  answer the instant it was written. What arrived after a restart of that
  clock is newer than what arrived before it, whatever the two clocks read.
*/
function frameOutdatesStatus(frame) {
  return frame.boot > state.statusBoot || (frame.boot === state.statusBoot && frame.at > state.status.at);
}

/*
  Whether the status on screen was written once the frames had turned to what
  they say now. `since` is a tick that began after the last frame saying
  anything else had read its verdict, so such an answer has seen everything
  those earlier frames showed.
*/
function statusCoversFrames(frame) {
  return state.statusBoot > frame.boot || (state.statusBoot === frame.boot && state.status.at >= frame.since);
}

/*
  A latched emergency stop has its own banner, for as long as it is latched.
  It is decided here and nowhere else, from three sources: the frames, the
  latest /api/status answer, and the receipt of this page's own E-STOP. The
  screen must never look safer than what the machine reports: the banner goes
  only on a report that says the stop is released and that nothing newer
  denies.

  What RAISES it, whoever latched the stop (this page, another screen, the
  camera, a rule, the runtime itself): the receipt, a frame showing a latched
  stop, and a status answer showing one, standing or held behind go_silent,
  unless a frame taken after that answer shows it released (asked before an
  acknowledgement, the answer arrived after it).

  What takes it DOWN, and what each proves:

  - a frame with no latched stop and no go_silent, when no status has
    answered yet or when it was taken after the status on screen: the frames
    themselves show the stop released;
  - a status answer that shows no latched stop, when no frame is newer, and
    only where it can speak for every latched stop this page was shown
    (statusTakesDown, statusSpeaksForLatch). Outside go_silent it can:
    nothing but an acknowledgement, or a console that restarted, takes a
    latched quick stop away there. Under go_silent it can for the emergency
    stop itself, which the status carries behind go_silent, and for a latch
    of a console proven restarted; under frames turned to go_silent the
    answer must also have been written after they turned. The latched quick
    stop of a rule that go_silent replaced, in the console that showed it,
    keeps its banner, whether the frames or the status had shown it.

  In every other case it KEEPS what it shows; unknown is never read as
  released. So does a socket that goes quiet on a frame showing a latched
  stop: only a later frame takes that banner down.

  The receipt holds the banner from the click until a frame taken after the
  latch can speak, or a status asked after it answers: the banner neither
  waits for a frame nor drops on one that was already on its way.

  The banner says "latched", never "stopped": the measured speed alone speaks
  about the shaft.
*/
function renderEstopBanner() {
  var frame = state.estopFrame;
  var said = frame ? frame.says : null;
  var polled = state.status ? statusOpinion(state.status) : null;
  if (state.estopReceipt || said === "latched") {
    state.estopShown = true;
  } else if (polled === null) {
    if (said === "clear") {
      state.estopShown = false;
    }
  } else if (said === "clear" && frameOutdatesStatus(frame)) {
    state.estopShown = false;
  } else if (polled === "latched") {
    state.estopShown = true;
  } else if (statusTakesDown(frame)) {
    state.estopShown = false;
  }
  showBanner(el("estop-banner"), state.estopShown);
  askStatusIfUnsettled();
}

/*
  The status on screen shows no latched stop, and no frame is newer: may it
  take the banner down? One gate for every such answer, go_silent or not: it
  must speak for every latched stop this page was shown. Outside go_silent it
  always does, having just released what the status had shown (noteStatusLatch)
  while frames that are not under go_silent hold no latch of their own.
*/
function statusTakesDown(frame) {
  var seen = !(frame && frame.says === "silent") || statusCoversFrames(frame);
  return seen && statusSpeaksForLatch();
}

/*
  When the frames have turned to something the status on screen does not
  settle, the status is asked at once rather than at its next 5 s turn, once
  for each turn of the frames:

  - frames under go_silent, and no status written since: a stop may be latched
    behind it that this page never saw, or the one it shows may be gone;
  - frames saying "released" after a status that said "latched": that answer
    is out of date, and so are the lines of the page it feeds.
*/
function askStatusIfUnsettled() {
  var frame = state.estopFrame;
  if (!frame || frame.asked) {
    return;
  }
  var status = state.status;
  var unsettled = frame.says === "silent"
    ? !(status && statusCoversFrames(frame))
    : frame.says === "clear" && Boolean(status) && statusOpinion(status) === "latched" && frameOutdatesStatus(frame);
  if (unsettled) {
    frame.asked = true;
    loadStatus().catch(function () {
      /* the banner keeps its state; the periodic refresh asks again */
    });
  }
}

/*
  A stop this page asked for, STOP or E-STOP, and has neither an answer to nor
  proof of: a doubt. `pending` holds the instant of each request still out,
  oldest first; `failed` is what the last one to fail said, and `failedAt`
  when. `sent` counts the status requests sent when the doubt began: one sent
  later was answered after the click.

  A request is answered in a few milliseconds when everything works. When the
  console is stuck or the network is mute it simply stays out, and nothing
  used to tell the operator, who was left waiting for an answer that was not
  coming. So a doubt is said in a banner of its own, and the banner says
  exactly where the request stands:

  - still out: for how long, counted from the oldest request still out,
    however many clicks follow it. A second click must not make the page look
    reassured for two more seconds;
  - failed, with nothing still out: that it failed, how long ago, and that it
    is no longer pending. A failed request is over. The page does not send it
    again, and the banner stays until there is proof of a stop;
  - failed, then asked again: both.

  A request still out is never withdrawn: it may still get through.
*/
function openDoubt(doubt, clicked) {
  var asked = doubt || { pending: [], failed: null, failedAt: 0, sent: state.statusSent };
  asked.pending.push(clicked);
  return asked;
}

/** The request sent at `clicked` ended with no answer, or with an error: it is no longer out. */
function failDoubt(asked, clicked, message) {
  asked.pending.splice(asked.pending.indexOf(clicked), 1);
  asked.failed = message;
  asked.failedAt = performance.now();
}

function doubtNotice(asked, use) {
  var now = performance.now();
  if (!asked.pending.length) {
    return "la demande a echoue il y a " + answerAge((now - asked.failedAt) / 1000) +
      " et n'est plus en attente : " + asked.failed + " - " + use;
  }
  var waited = Math.round((now - asked.pending[0]) / 1000) + " s";
  if (asked.failed === null) {
    return "aucune reponse de la console depuis " + waited + " - " + use;
  }
  return "nouvelle demande sans reponse depuis " + waited + ", la precedente a echoue : " + asked.failed +
    " - " + use;
}

/*
  One doubt, in its banner: shown at once when a request has failed, and
  `limit` after the oldest request still out otherwise. The text is written
  before the banner is measured, like the other banners.
*/
function renderDoubt(asked, limit, banner, detail, use) {
  var overdue = Boolean(asked) && (asked.failed !== null || performance.now() - asked.pending[0] >= limit);
  var reworded = false;
  if (overdue) {
    var notice = doubtNotice(asked, use);
    reworded = detail.textContent !== notice;
    text(detail, notice);
  }
  showBanner(banner, overdue, reworded);
}

/*
  The E-STOP in doubt. Its receipt takes the notice down, and so does a frame,
  or a status asked after the click, that shows a stop latched: the machine
  has it, whichever way it came. A frame under go_silent shows nothing either
  way and leaves the notice up. The notice names the wired stop.
*/
function renderEstopUnanswered() {
  renderDoubt(
    state.estopAsked, ESTOP_ANSWER_MS, el("estop-unanswered"), el("estop-unanswered-detail"),
    "UTILISEZ L'ARRET CABLE"
  );
}

/** A report that has just arrived shows a stop latched: this page's E-STOP is no longer in doubt. */
function estopSeenLatched(opinion) {
  if (opinion === "latched" && state.estopAsked) {
    state.estopAsked = null;
    renderEstopUnanswered();
  }
}

/*
  The STOP in doubt, treated as the E-STOP is. An answer takes the notice
  down, acceptance or refusal: a refusal is said in its own alert, as before.
  So does a frame showing the session ending, or a status asked after the
  click showing no session running: the stop is under way, whichever way it
  came. A frame at rest is not that proof: a start may be waiting behind it.
  The notice names the two stops that remain.
*/
var STOP_DOUBT_USE = "rien ne dit que la seance s'arrete : E-STOP, ou l'arret cable";

function renderStopUnanswered() {
  renderDoubt(
    state.stopAsked, STOP_ANSWER_MS, el("stop-unanswered"), el("stop-unanswered-detail"),
    STOP_DOUBT_USE
  );
}

/** This page's STOP is no longer in doubt: it was answered, or the session is seen ending. */
function stopSettled() {
  if (state.stopAsked) {
    state.stopAsked = null;
    renderStopUnanswered();
  }
}

/*
  A speed held or lowered by a warning that is NOT latched can climb again
  with nobody clicking: the warning lifts when its cause ends, and the loop
  then follows the programme or the manual target again, upwards too. To
  somebody about to walk up to the arm, a held speed reads as "stopped for
  good". So it is said in a banner, on every page, for as long as it is true.

  True means: a freeze or a reduce that is not latched, in a session that is
  running (a programme or a manual one, not one that is ending) and in a
  phase that can still be asked for speed. In manual it also takes a target
  above the setpoint: over a manual arm held at standstill the target is 0,
  nothing is waiting, and nothing will climb.

  A latched verdict never shows it: nothing resumes behind one.
*/
var CAN_STILL_MOVE = { baseline: true, warmup: true, hold: true };

function resumePossible(snapshot) {
  var safety = snapshot.safety;
  if (!safety || safety.latched || (safety.action !== "freeze" && safety.action !== "reduce")) {
    return false;
  }
  if (!CAN_STILL_MOVE[snapshot.phase]) {
    return false;
  }
  if (snapshot.mode === "seance") {
    return true;
  }
  return snapshot.mode === "manuel" && Boolean(snapshot.manual) &&
    snapshot.manual.target.motor_rpm > snapshot.setpoint.motor_rpm;
}

function renderResumeBanner(snapshot) {
  var possible = resumePossible(snapshot);
  var reworded = false;
  if (possible) {
    var detail = el("resume-banner-detail");
    var notice = "l'avertissement " + snapshot.safety.rule + " " +
      (snapshot.safety.action === "reduce" ? "baisse" : "tient") +
      " la vitesse et n'est pas verrouille : il se leve seul, et la vitesse remonte alors sans aucun clic";
    reworded = detail.textContent !== notice;
    text(detail, notice);
  }
  showBanner(el("resume-banner"), possible, reworded);
}

/*
  A drive in fault takes no start: the console refuses one with that reason,
  at the click. It has to be seen BEFORE the click, and on a console that has
  armed nothing a fault at rest raises no verdict: the safety chip stays
  green, and the fault was only on the drive's own card, on two of the pages.
  So it is said in a banner, on every page, for as long as the drive shows it.

  Which report is believed, between the frames and the status (the console
  gives the drive's state in both):

  - live frames: the latest one, five a second;
  - no live frame: the status on screen when the machine wrote it at or after
    the last frame (frameOutdatesStatus, on the machine's own clock): a fault
    that appears, or is reset, while this screen's socket is down is then
    shown, or taken down, at the next status;
  - otherwise the last frame: next to NO LIVE DATA its word stays, as that of
    a latched stop does. Unknown is never read as "no fault".

  The state `comm_lost` means the console does not know: no banner is raised
  on it, and the drive's chip says so in red.
*/
function driveSeen() {
  var status = state.status;
  if (statusLeads() && status.drive_state !== undefined) {
    return { state: status.drive_state, fault: status.drive_fault };
  }
  return state.snapshot ? { state: state.snapshot.drive_state, fault: state.snapshot.fault } : null;
}

/** The fault as the banner names it: the code on the drive's own display, and no more. */
function faultCode(fault) {
  if (!fault) {
    return "code non lu sur le variateur";
  }
  return (fault.name === "unknown" ? "code inconnu" : fault.mnemonic) + " (LFT brut " + fault.raw_code + ")";
}

function renderDriveFault() {
  var seen = driveSeen();
  var faulted = Boolean(seen) && seen.state === "fault";
  var reworded = false;
  if (faulted) {
    var detail = el("drive-fault-banner-detail");
    var notice = faultCode(seen.fault) +
      " : aucun demarrage n'est accepte tant que le variateur signale ce defaut. Le traiter sur la carte Variateur (Tableau de bord)";
    reworded = detail.textContent !== notice;
    text(detail, notice);
  }
  showBanner(el("drive-fault-banner"), faulted, reworded);
}

/*
  The banners take room at the top of the page, and the emergency-stop one
  stays for as long as a stop is latched. The stylesheet starts the sidebar
  and the mobile bar under them, at --banners-h: this keeps that height current.
*/
function fitBanners() {
  document.documentElement.style.setProperty("--banners-h", el("banners").offsetHeight + "px");
}

/*
  The footer is fixed over the bottom of the page, and the stylesheet keeps
  the page and the sidebar clear of it by its height, --footer-h. A height
  written in the stylesheet was a guess, and a short one: the two stops are
  taller than it, and on a narrow screen the footer has another height again.
  The last lines of a page then sat under the footer. So it is measured. The
  value of the stylesheet stays until a height can be read.
*/
function fitFooter() {
  var height = el("footer").offsetHeight;
  if (height) {
    document.documentElement.style.setProperty("--footer-h", height + "px");
  }
}

/*
  Show or hide one banner, and measure the stack when that changes its height:
  the banner appears, goes, or stays with a text that was just `reworded` and
  may wrap differently. Measured after the change, never before. The
  ResizeObserver set up in start() sees the same changes; this is what a
  browser without one still gets.
*/
function showBanner(node, visible, reworded) {
  var changes = node.classList.contains("hidden") === visible;
  show(node, visible);
  if (changes || (visible && reworded)) {
    fitBanners();
  }
}

/* ------------------------------------------------------------- websocket  */

function socketUrl() {
  var scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  var query = state.token ? "?token=" + encodeURIComponent(state.token) : "";
  return scheme + "//" + window.location.host + "/ws/telemetry" + query;
}

function connect() {
  if (state.socket) {
    try {
      state.socket.close();
    } catch {
      /* already closing; nothing to do */
    }
  }
  var socket = new WebSocket(socketUrl());
  state.socket = socket;
  // A new socket may be another console's: what the frames of this one show is its own.
  state.stream += 1;

  socket.onopen = function () {
    state.connected = true;
    state.lastFrameAt = performance.now();
    refreshLiveness();
  };

  socket.onmessage = function (message) {
    state.lastFrameAt = performance.now();
    var envelope;
    try {
      envelope = JSON.parse(message.data);
    } catch {
      return;
    }
    handleEnvelope(envelope);
    refreshLiveness();
  };

  socket.onclose = function () {
    state.connected = false;
    refreshLiveness();
    scheduleReconnect();
  };

  socket.onerror = function () {
    state.connected = false;
    refreshLiveness();
  };
}

function scheduleReconnect() {
  if (state.reconnectTimer) {
    return;
  }
  state.reconnectTimer = window.setTimeout(function () {
    state.reconnectTimer = null;
    connect();
  }, RECONNECT_MS);
}

function handleEnvelope(envelope) {
  if (envelope.kind === "snapshot" && envelope.snapshot) {
    renderSnapshot(envelope.snapshot);
  } else if (envelope.kind === "event" && envelope.event) {
    addEvent(envelope.event);
  } else if (envelope.kind === "ecg" && envelope.ecg) {
    addEcg(envelope.ecg);
  } else if (envelope.kind === "resync") {
    // The server evicted this client for falling behind. It is about to close
    // the socket; say so plainly rather than letting the page look merely idle.
    state.connected = false;
    text(el("banner-detail"), envelope.notice || "cet ecran a pris du retard ; reconnexion");
  }
}

/* ------------------------------------------------------------- rendering  */

var MODE_KINDS = { repos: "", manuel: "warn", seance: "warn", arret: "bad" };

function renderMode(snapshot) {
  var mode = snapshot.mode || "repos";
  var kind = MODE_KINDS[mode] === undefined ? "bad" : MODE_KINDS[mode];
  pill(el("run-mode"), mode.toUpperCase(), kind);
  pill(el("mobile-mode"), mode.toUpperCase(), kind);
}

function safetyKind(rank) {
  return rank >= 3 ? "bad" : rank >= 1 ? "warn" : "good";
}

/*
  The grade of the heart rate, or "perime" once the reading is stale. The
  grade belongs to the last sample: when nothing new is measured it would go
  on saying "good" about a signal that is no longer there. "perime" is amber
  here as on a stale sensor card, one word, one look: amber is this page's
  colour for stale data, and in grey nothing on the card was coloured between
  the reading going stale (4 s) and the first verdict about it (10 s).
*/
function renderHrQuality(node, hr) {
  if (!hr) {
    pill(node, "pas de signal", "bad");
  } else if (hr.stale) {
    pill(node, "perime", "warn");
  } else {
    pill(node, hr.quality, hr.quality === "good" ? "good" : "bad");
  }
}

/*
  The phase to print, or "" at rest. With no session the runtime reports its
  idle value, "done": printed as is, it reads as a programme that has just
  ended, on a machine that may never have run one.
*/
function shownPhase(snapshot) {
  return snapshot.mode === "repos" && snapshot.phase === "done" ? "" : snapshot.phase;
}

/*
  One line for a drive fault. For a code the table knows, `message` is the
  mnemonic, the code and the meaning over again, so it is not added. For an
  unknown code it has another form: it is the one place that gives the code in
  hexadecimal, and it already ends with the meaning. It is shown as it is.
*/
function faultText(fault) {
  if (fault.name === "unknown") {
    return "DEFAUT " + fault.message;
  }
  return "DEFAUT " + fault.mnemonic + " (LFT brut " + fault.raw_code + ") : " + fault.meaning;
}

function renderSnapshot(snapshot) {
  state.snapshot = snapshot;
  renderMode(snapshot);
  renderConsole(snapshot);
  renderManual(snapshot);
  settleManualTarget(snapshot);
  settleLoopAsks(snapshot);
  settleAcknowledgement(frameReport(snapshot));
  noteLatch("frame", snapshot.safety && snapshot.safety.latched ? snapshot.safety.rule : null);
  var carried = snapshot.safety
    ? snapshot.safety.rule + "/" + snapshot.safety.action + "/" + snapshot.safety.latched
    : "";
  if (carried !== state.verdictShown) {
    // The status lines list the verdict the frames carry when it is the worse one: kept in step.
    state.verdictShown = carried;
    if (state.status) {
      renderStatusGrids(state.status);
    }
  }
  renderManualHold();
  noteEstopFrame(snapshot);
  estopSeenLatched(state.estopFrame.says);
  if (snapshot.mode === "arret") {
    stopSettled();
  }
  renderEstopBanner();
  renderResumeBanner(snapshot);

  /* --- heart rate, with its age carried alongside --------------------- */
  var hr = snapshot.heart_rate;
  var bpm = snapshot.live_bpm;
  text(el("hr"), bpm === null || bpm === undefined ? "-" : String(bpm));
  el("hr").classList.toggle("stale", !hr || hr.stale || bpm === null || bpm === undefined);
  renderHrQuality(el("hr-quality"), hr);
  grid(el("hr-grid"), [
    ["cible", snapshot.target_bpm === null ? "-" : snapshot.target_bpm + " bpm"],
    ["age", hr ? num(hr.age_s, 1) + " s" : "-", hr && hr.stale ? "stale" : ""],
    ["brut", hr && hr.bpm !== null ? hr.bpm + " bpm" : "-"],
    ["dans la zone", secs(snapshot.counters.in_zone_s)],
    ["au-dessus", secs(snapshot.counters.above_zone_s)],
    ["en dessous", secs(snapshot.counters.below_zone_s)],
  ]);
  renderZoneBand(bpm);

  /* --- phase and progress --------------------------------------------- */
  var phase = shownPhase(snapshot);
  pill(el("phase"), phase || "-", phase === "done" ? "warn" : "");
  var elapsed = snapshot.elapsed_s || 0;
  var remaining = snapshot.remaining_s || 0;
  var total = elapsed + remaining;
  el("progress-fill").style.width = total > 0 ? (100 * elapsed) / total + "%" : "0";
  grid(el("time-grid"), [
    ["ecoule", secs(snapshot.elapsed_s)],
    ["restant", secs(snapshot.remaining_s)],
    ["securite", snapshot.safety_action],
  ]);

  /* --- measured: the "is it stopped" indicator ------------------------ */
  var measured = snapshot.measured;
  text(el("measured-output"), num(measured.output_rpm, 2));
  grid(el("measured-grid"), speedRows(measured).concat([
    ["courant", num(snapshot.current_a, 2) + " A"],
  ]));
  renderMotion(snapshot);

  /* --- setpoint: what was commanded, which is a different fact -------- */
  text(el("setpoint-output"), num(snapshot.setpoint.output_rpm, 2));
  grid(el("setpoint-grid"), speedRows(snapshot.setpoint));
  pill(
    el("setpoint-confirmed"),
    snapshot.setpoint_confirmed ? "confirmee par le variateur" : "non confirmee",
    snapshot.setpoint_confirmed ? "good" : "warn"
  );

  /* --- drive ---------------------------------------------------------- */
  pill(
    el("drive-state"),
    snapshot.drive_state,
    snapshot.drive_state === "comm_lost" || snapshot.drive_state === "fault"
      ? "bad"
      : snapshot.drive_state === "operation_enabled"
        ? "warn"
        : ""
  );
  grid(el("drive-grid"), [
    ["age du statut", num(snapshot.drive_status_age_s, 1) + " s", snapshot.drive_status_stale ? "stale" : ""],
    ["courant", num(snapshot.current_a, 2) + " A"],
    ["mesure", num(measured.motor_rpm, 0) + " tr/min moteur"],
  ]);
  var fault = el("fault");
  show(fault, Boolean(snapshot.fault));
  if (snapshot.fault) {
    text(fault, faultText(snapshot.fault));
  }

  /* --- safety: on the Securite page, the Seance page and the sidebar --- */
  var safety = snapshot.safety;
  var kind = safetyKind(snapshot.safety_rank);
  pill(el("safety-action"), snapshot.safety_action, kind);
  pill(el("run-safety-action"), snapshot.safety_action, kind);
  pill(el("side-safety"), snapshot.safety_action, kind);
  var safetyRows = safety
    ? [
        ["regle", safety.rule],
        ["detail", safety.detail],
        ["verrouille", safety.latched ? "oui" : "non"],
        ["depuis", num(safety.age_s, 1) + " s"],
      ]
    : [["regle", "aucune demande"], ["verrouille", "non"]];
  grid(el("safety-grid"), safetyRows);
  grid(el("run-safety-grid"), safetyRows);

  refreshLiveness();
}

/*
  The console view: heart rate with grade and age, MEASURED speed five ways,
  and the drive as read. Rendered from the same snapshot as the run view.
*/
function renderConsole(snapshot) {
  var hr = snapshot.heart_rate;
  var bpm = snapshot.live_bpm;
  text(el("console-hr"), bpm === null || bpm === undefined ? "-" : String(bpm));
  el("console-hr").classList.toggle("stale", !hr || hr.stale || bpm === null || bpm === undefined);
  renderHrQuality(el("console-hr-quality"), hr);
  grid(el("console-hr-grid"), [
    ["age", hr ? num(hr.age_s, 1) + " s" : "-", hr && hr.stale ? "stale" : ""],
    ["brut", hr && hr.bpm !== null ? hr.bpm + " bpm" : "-"],
    ["seq", hr ? String(hr.seq) : "-"],
  ]);

  var measured = snapshot.measured;
  text(el("console-output"), num(measured.output_rpm, 2));
  grid(el("console-speed-grid"), speedRows(measured).concat([
    ["courant", num(snapshot.current_a, 2) + " A"],
  ]));

  pill(
    el("console-drive-state"),
    snapshot.drive_state,
    snapshot.drive_state === "comm_lost" || snapshot.drive_state === "fault" ? "bad" : ""
  );
  var fault = el("console-fault");
  show(fault, Boolean(snapshot.fault));
  if (snapshot.fault) {
    text(fault, faultText(snapshot.fault));
  }
  show(el("fault-reset"), Boolean(snapshot.fault) && state.motionEnabled);
  renderDriveGrid();
}

function renderDriveGrid() {
  var snapshot = state.snapshot;
  var panel = state.panel;
  var rows = [];
  if (snapshot) {
    rows.push(["age du statut", num(snapshot.drive_status_age_s, 1) + " s", snapshot.drive_status_stale ? "stale" : ""]);
    rows.push(["LFT brut", snapshot.fault ? String(snapshot.fault.raw_code) : "aucun"]);
  }
  if (panel) {
    var drive = panel.drive;
    rows.push(["liaison", panel.motor_backend + (drive.description ? " · " + drive.description : "")]);
    rows.push(["latence", drive.latency_ms === null ? "-" : num(drive.latency_ms, 0) + " ms"]);
    rows.push(["lectures / echecs", drive.reads + " / " + drive.failures, drive.consecutive_failures ? "stale" : ""]);
    rows.push(["derniere erreur", drive.last_error || "-"]);
    rows.push(["rayon / rapport", num(panel.radius_m, 2) + " m / i = " + num(panel.gear_ratio, 2)]);
    rows.push(["plafond moteur", panel.motor_max_rpm + " tr/min"]);
  }
  grid(el("console-drive-grid"), rows);
}

/*
  The console's link with the dashboard (the Convex site), which is not this
  page's link with the console (the "Liaison" chip). The console says the state
  and its word (`dashboard.state`, `dashboard.label`); the page only chooses
  the colour. A state this page has no colour for is shown by its word, in
  no colour, rather than hidden.

  The reason under the chips may carry words the dashboard sent: it is written
  with textContent, as everything on this page, never as markup.
*/
var DASHBOARD_KINDS = {
  reachable: "good",
  unreachable: "bad",
  incompatible: "bad",
  key_refused: "bad",
  server_error: "bad",
  waiting: "warn",
  not_configured: "",
};

function answerAge(seconds) {
  return seconds < 100 ? Math.round(seconds) + " s" : Math.round(seconds / 60) + " min";
}

function renderDashboardLink(link) {
  var kind = DASHBOARD_KINDS[link.state] || "";
  pill(el("dashboard-link"), link.label, kind);
  pill(el("mobile-dashboard"), "serveur " + link.label, kind);
  var said = [];
  if (link.detail) {
    said.push(link.detail);
  }
  if (link.last_answer_age_s !== null) {
    said.push("derniere reponse il y a " + answerAge(link.last_answer_age_s));
  }
  text(el("dashboard-link-detail"), said.join(" · "));
}

/*
  What the machine's wall clock is worth, as the console states it
  (`wall_clock` in /api/panel): its word, why, and whether the dates the
  console writes can be believed. The page decides nothing: it writes the
  console's words, in orange when the clock is not one to believe. A console
  that does not judge its clock sends none, and the line stays empty.
*/
function renderWallClock(clock) {
  var node = el("wall-clock");
  text(node, "horloge " + clock.label + (clock.detail ? " · " + clock.detail : ""));
  node.className = "bind" + (clock.trusted ? "" : " bind-warn");
}

/*
  Whether the console is in service, as it says in /api/panel (`service`), and
  what it has told the service manager that watches it (`watchdog`).

  Out of service is a standing state the console keeps across its restarts: it
  has started several times after a stop that was not clean, and it arms
  nothing until a named operator puts it back in service. So it is said three
  times: a chip on every page, a red banner on every page, and the form that
  lifts it on the safety page, shown only then.

  The line under the chips says what the last start found: an unclean stop, the
  session record it left open, a state the disk did not take, notifications the
  service manager did not get. Nothing is written when there is nothing to say.
*/
function renderService(service, watchdog) {
  var out = service.out_of_service;
  var minutes = Math.round(service.window_s / 60);
  pill(el("service-state"), out ? "HORS SERVICE" : "en service", out ? "bad" : "good");
  show(el("service-restore"), out);
  var detail = el("service-banner-detail");
  var notice = service.threshold + " demarrages apres un arret non propre en moins de " + minutes +
    " min : aucun depart n'est accepte. Faire verifier la machine, puis la remettre en service (page Securite), par un operateur nomme";
  var reworded = detail.textContent !== notice;
  text(detail, notice);
  // In the stack with the other banners: the page makes room for it as for them.
  showBanner(el("service-banner"), out, reworded);
  var said = [];
  if (service.unclean_stop) {
    said.push(
      "demarre apres un arret non propre" +
        (service.open_record ? " (enregistrement " + service.open_record + " non clos)" : "")
    );
  }
  if (service.unclean_starts > 0) {
    said.push(service.unclean_starts + " sur " + service.threshold + " en " + minutes + " min avant la mise hors service");
  }
  if (!service.persisted) {
    said.push("etat des redemarrages non enregistre sur le disque");
  }
  if (watchdog.enabled && watchdog.failed > 0) {
    said.push("chien de garde : " + watchdog.failed + " notification(s) non envoyee(s)");
  }
  text(el("service-detail"), said.join(" · "));
}

function doRestoreService() {
  api("/api/service/restore", { method: "POST", body: { operator: el("service-operator").value } })
    .then(function (service) {
      ok(
        el("service-note"),
        service.persisted
          ? "console remise en service"
          : "console remise en service jusqu'a son prochain redemarrage : l'etat n'a pas pu etre enregistre"
      );
      loadPanel();
    })
    .catch(function (error) {
      problem(el("service-note"), error);
      window.alert("remise en service refusee : " + error.message);
    });
}

/*
  The medical parameters the console runs with, from /api/panel: the version
  in the footer of every page, and, when their signature did not verify at
  startup, the red banner of the bench mode. Both standing states: they
  change only with a restart of the console.
*/
function renderMedical(medical) {
  var footer = el("footer-medical");
  if (!medical) {
    text(footer, "parametres medicaux -");
    footer.classList.remove("bad");
    showBanner(el("medical-banner"), false, false);
    return;
  }
  text(
    footer,
    "parametres medicaux " + medical.version + (medical.signed ? "" : " NON SIGNES")
  );
  footer.classList.toggle("bad", !medical.signed);
  var detail = el("medical-banner-detail");
  var notice = medical.warning || "";
  var reworded = detail.textContent !== notice;
  text(detail, notice);
  showBanner(el("medical-banner"), !medical.signed, reworded);
}

function renderPanel(panel) {
  state.panel = panel;
  state.panelAt = performance.now();
  renderManualHold();
  if (!panel) {
    pill(el("console-mode"), "pas de console", "");
    return;
  }
  pill(el("software-version"), panel.software_version, "");
  renderMedical(panel.medical_parameters);
  renderDashboardLink(panel.dashboard);
  if (panel.wall_clock) {
    renderWallClock(panel.wall_clock);
  }
  renderService(panel.service, panel.watchdog);
  state.motionEnabled = panel.motion_enabled;
  state.programsEnabled = panel.programs_enabled;
  pill(el("console-mode"), panel.motion_enabled ? "mouvement actif" : "LECTURE SEULE", panel.motion_enabled ? "warn" : "good");
  show(el("console-readonly"), !panel.motion_enabled);
  show(el("programs-note"), !panel.programs_enabled);
  el("start").disabled = el("start").disabled || !panel.motion_enabled || !panel.programs_enabled;
  el("manual-start").disabled = !panel.motion_enabled;
  el("manual-step-up").disabled = panel.radius_m === null;
  el("manual-step-down").disabled = panel.radius_m === null;

  var ecg = panel.ecg;
  pill(
    el("console-ecg-link"),
    ecg.acquiring ? "acquisition" : ecg.connected ? "connecte" : "deconnecte",
    ecg.acquiring ? "good" : "bad"
  );
  var rows = [
    ["source", ecg.source + (ecg.address ? " · " + ecg.address : "")],
    ["tentatives", String(ecg.connect_attempts)],
    ["lots traites", ecg.batches + " (" + ecg.samples + " echantillons)"],
    ["dernier lot", ecg.last_batch_age_s === null ? "-" : num(ecg.last_batch_age_s, 1) + " s"],
    ["DSP", ecg.dsp_seq === null ? "-" : "seq " + ecg.dsp_seq + " · " + ecg.dsp_quality],
    ["tendance", panel.heart_rate_trend_bpm_per_min === null ? "-" : num(panel.heart_rate_trend_bpm_per_min, 1) + " bpm/min"],
  ];
  if (ecg.link) {
    rows = rows.concat([
      ["trames", String(ecg.link.frames)],
      ["pertes de synchro", String(ecg.link.sync_losses), ecg.link.sync_losses ? "stale" : ""],
      ["octets ignores", String(ecg.link.skipped_bytes)],
      ["echantillons combles", String(ecg.link.filled_samples)],
      ["reconnexions", String(ecg.link.reconnects)],
    ]);
  }
  if (ecg.missing_channel) {
    rows.push(["lots sans ECG", String(ecg.missing_channel), "stale"]);
  }
  if (ecg.last_error) {
    rows.push(["erreur", ecg.last_error, "stale"]);
  }
  grid(el("console-ecg-grid"), rows);
  renderDriveGrid();
}

function loadPanel() {
  return api("/api/panel").then(renderPanel).catch(function () {
    /* the banner and the token note already say the API is unreachable */
  });
}

/* ------------------------------------------------------------ manual mode */

/*
  Whether the ramp the machine announces leads somewhere else than the target:
  under a `reduce` the setpoint is walked down towards zero whatever the
  target is, and the machine says so (`ramp_to`: where the setpoint is going,
  and `ramp_eta_s` the time to get there). The line said "en cours, arrivee
  ~..." there, with the time to climb back to a target the arm was moving away
  from. An answer without `ramp_to` is read as before.
*/
function rampedAway(manual) {
  return Boolean(manual.ramp_to) && manual.ramp_to.motor_rpm !== manual.target.motor_rpm;
}

/** That descent, in words: where it leads, under which warning, and when it gets there if the warning stays. */
function loweredWords(snapshot, manual) {
  var warning = snapshot.safety ? "l'avertissement " + snapshot.safety.rule : "un avertissement";
  return "vers " + num(manual.ramp_to.output_rpm, 2) + " tr/min de sortie sous " + warning +
    ", la cible n'est pas suivie ; arrivee dans ~" + secs(manual.ramp_eta_s) + " s'il dure";
}

/*
  The "rampe" line of the manual card. While a `freeze` holds the setpoint
  away from a target that is not zero, the machine announces no ramp and no
  arrival time (`ramping` false, `ramp_eta_s` null): nothing is on its way.
  That is a setpoint HELD, not a target reached, so "cible atteinte" is said
  only when the setpoint of this same snapshot is the target. And a setpoint
  a `reduce` walks down is said to go down, not to be on its way to the target.
*/
function rampLine(snapshot, manual) {
  if (manual.ramping) {
    if (rampedAway(manual)) {
      return "en baisse " + loweredWords(snapshot, manual);
    }
    return "en cours, arrivee ~" + secs(manual.ramp_eta_s);
  }
  var setpoint = snapshot.setpoint;
  if (setpoint && setpoint.motor_rpm !== manual.target.motor_rpm) {
    return "consigne maintenue, cible non atteinte";
  }
  return "cible atteinte";
}

/*
  The manual card. The draft is only ever a number on this screen until
  "Appliquer": nothing is sent on a +/- click. The target is what the machine
  holds, the draft is what the operator is composing, and the draft is shown
  in amber whenever the two differ so an unapplied edit cannot pass for the
  machine's state. When the manual session ends (STOP, E-STOP, a verdict) the
  draft is dropped, because after any stop the target is 0.
*/
function renderManual(snapshot) {
  var manual = snapshot.mode === "repos" ? null : snapshot.manual;
  show(el("manual-idle"), !manual);
  show(el("manual-controls"), Boolean(manual));
  if (!manual) {
    state.manualDraft = null;
    pill(el("manual-state"), snapshot.mode === "manuel" ? "demarrage" : "inactif", "");
    show(el("ramp-banner"), false);
    return;
  }
  pill(el("manual-state"), manual.occupancy_label, "warn");
  if (state.manualDraft === null) {
    state.manualDraft = manual.target.output_rpm || 0;
  }
  renderDraft();
  grid(el("manual-grid"), [
    ["cible appliquee", speedLine(manual.target)],
    ["plafond", speedLine(manual.ceiling)],
    ["minimum de rotation", speedLine(manual.min_run)],
    ["rampe", rampLine(snapshot, manual)],
  ]);
  // The speed is changing either way, so the head must not move: the banner says where it goes.
  show(el("ramp-banner"), manual.ramping);
  if (manual.ramping) {
    text(
      el("ramp-banner-detail"),
      rampedAway(manual)
        ? "la vitesse baisse " + loweredWords(snapshot, manual)
        : "vers " + num(manual.target.output_rpm, 2) + " tr/min de sortie (Gr " +
          num(manual.target.resultant_g, 3) + "), arrivee dans ~" + secs(manual.ramp_eta_s)
    );
  }
}

/*
  What the heart rate of a person on board holds, said BEFORE a target is
  typed. With nobody on board there is no such gate. With a person on board
  no setpoint rises without a usable heart rate, nor while its short trend is
  unknown or falling fast: over a stopped arm a target is then refused, and
  over a turning one the speed waits, and climbs again by itself when the hold
  goes. None of that is a verdict, so nothing else on the page shows it, and
  the reason used to be learnt from the refusal.

  The console states the hold itself, in /api/panel (`manual_rise_hold`), from
  the very test the loop applies: this page does not work it out again from
  the numbers it shows, which are not the ones the gate reads. It is asked
  once a second. When no answer has come for PANEL_STALE_MS the page no longer
  knows, and with a person on board it says that rather than showing nothing:
  an indicator that is off must mean "nothing holds".
*/
var RISE_HOLDS = {
  no_heart_rate: "pas de frequence cardiaque utilisable",
  trend_unknown: "tendance de la frequence cardiaque pas encore connue",
  heart_rate_falling: "la frequence cardiaque baisse trop vite",
};

function renderManualHold() {
  var snapshot = state.snapshot;
  var manual = snapshot && snapshot.mode === "manuel" ? snapshot.manual : null;
  var known = Boolean(state.panel) && performance.now() - state.panelAt < PANEL_STALE_MS;
  var hold = known ? state.panel.manual_rise_hold : null;
  var unknown = Boolean(manual) && manual.occupancy !== "bench" && !known;
  // Unknown counts as held for what the note of a target may say: see followsTarget().
  state.riseHeld = Boolean(manual) && (Boolean(hold) || unknown);
  show(el("manual-hold"), Boolean(manual) && (Boolean(hold) || unknown));
  if (unknown) {
    text(el("manual-hold-title"), "RETENUE PAR LA FREQUENCE CARDIAQUE : INCONNUE");
    text(
      el("manual-hold-detail"),
      "pas de reponse recente de la console : rien ne dit ici si la frequence cardiaque retient une montee"
    );
  } else if (hold) {
    text(el("manual-hold-title"), "MONTEE RETENUE PAR LA FREQUENCE CARDIAQUE");
    text(
      el("manual-hold-detail"),
      (RISE_HOLDS[hold] || hold) + ". Bras a l'arret : une cible non nulle est refusee. " +
        "Bras en rotation : la vitesse ne monte pas, puis remonte seule vers la cible quand la retenue cesse."
    );
  }
}

function renderDraft() {
  var manual = state.snapshot ? state.snapshot.manual : null;
  var node = el("manual-draft");
  text(node, num(state.manualDraft, 2));
  var applied = manual ? manual.target.output_rpm : null;
  node.classList.toggle(
    "draft",
    applied === null || Math.abs((applied || 0) - (state.manualDraft || 0)) > 0.005
  );
}

function outputRpmToGr(rpm, radius) {
  var omega = (rpm * 2 * Math.PI) / 60;
  var gc = (omega * omega * radius) / STANDARD_G;
  return Math.sqrt(gc * gc + 1);
}

function grToOutputRpm(gr, radius) {
  if (gr <= 1) {
    return 0;
  }
  var gc = Math.sqrt(gr * gr - 1);
  return (60 / (2 * Math.PI)) * Math.sqrt((gc * STANDARD_G) / radius);
}

/*
  Keep the draft inside the domain the machine accepts, {0} union
  [min_run, ceiling], by moving it the way the operator was already going:
  up from below the minimum lands on the minimum, down below it lands on 0.
  The loop refuses anything else anyway; this only avoids composing a target
  that is certain to be refused.
*/
function nudgeDraft(next, upward) {
  var manual = state.snapshot ? state.snapshot.manual : null;
  if (!manual) {
    return;
  }
  var ceiling = manual.ceiling.output_rpm || 0;
  var minRun = manual.min_run.output_rpm || 0;
  if (next > ceiling) {
    next = ceiling;
  }
  if (next > 0 && next < minRun) {
    next = upward ? minRun : 0;
  }
  state.manualDraft = Math.max(0, Math.round(next * 100) / 100);
  renderDraft();
}

function stepRpm(delta) {
  nudgeDraft((state.manualDraft || 0) + delta, delta > 0);
}

function stepGr(direction) {
  var radius = state.panel ? state.panel.radius_m : null;
  if (!radius) {
    return;
  }
  var gr = outputRpmToGr(state.manualDraft || 0, radius);
  var tenth = direction > 0 ? Math.floor(gr * 10 + 1e-6) + 1 : Math.ceil(gr * 10 - 1e-6) - 1;
  nudgeDraft(grToOutputRpm(tenth / 10, radius), direction > 0);
}

/*
  What became of a request the loop must answer: a manual start, a programme
  start, a manual target, a fault reset.

  The console answers 202 as soon as its mailbox holds the request, and the
  202 carries the identifier the console gave it (`request_id`). The loop then
  takes the request or refuses it on its next tick, or a STOP takes its place
  in the mailbox before that. The console says which, once, in an event that
  carries the same identifier: `taken`, `refused` (with the loop's reason) or
  `cancelled`. So the note under a button says what became of THIS screen's
  request, from the console's own answer:

  - at the 202: sent, not taken yet;
  - at the answer: taken, refused (the reason, word for word), or cancelled by
    a STOP. An answer can reach the screen before the 202 of what it answers:
    the last answers are kept (state.fates) and looked up at the 202.

  The page deduces nothing from a frame: a session on screen is not proof that
  the loop took this screen's request, and the page does not say "pris" for
  it. Nor does it read another screen's answer as its own: an answer that
  carries another identifier goes to the list of events and nowhere else. One
  `refused` event carries no identifier, the manual target the machine takes
  back by itself. Nobody asked for that, and every screen showing the manual
  session writes it under the card.

  The one thing a screen cannot know by itself is what the console published
  while its socket was down: those events are not sent again. So the page asks
  the console. /api/status lists what became of the last requests, by
  identifier, and the page asks for it at the first message of a socket that
  is not the one open at the click, and when a second of the machine's clock
  has gone by after a request with no answer. Until the console's answer has
  settled it, the note says that this screen does not know, and nothing else:
  nothing from the frames.

  "Taken" is kept only for as long as it is true:

  - of a start, while the session it started is the one in progress. The
    console says when that ends (`session_idle`), and a frame taken after the
    answer that no longer shows the session says it too. After a change of
    socket the page cannot tell whether the session on screen is still that
    one (it may have ended, and another begun, unseen): it says so and asks
    the console, whose `session_request` is the start the session in progress
    came from. The note is taken up again, or wiped, on an answer asked for
    after the new socket delivered its first message. An older answer may
    have been written before something this screen then missed;
  - of a target, while the machine holds that target (the frames);
  - of a reset, as what the frames have shown since: the fault gone, or still
    reported a second later. A fault that shows again after it went is
    another fault, and the note is wiped.

  What a note adds from the frames follows them: "(cible 0)" after a manual
  start, "(suivie aux limites de mouvement)" after a target. A frame can be
  delivered after an event that is newer than it: a frame taken before the
  answer says nothing about it, and is left out.
*/
var FATES_KEPT = 32; // answers kept for a 202 that arrives after them; the console itself keeps 16
var REQUESTS = {
  manual: {
    note: function () {
      return el("manual-note");
    },
    sent: "demande envoyee : ",
    session: true,
    shows: function (snapshot) {
      return snapshot.mode === "manuel";
    },
    taken: function (held, frame) {
      var atZero = Boolean(frame) && Boolean(frame.manual) && frame.manual.target.motor_rpm === 0;
      return "demarrage pris par la machine : " + held.said + (atZero ? " (cible 0)" : "");
    },
    lost: "cet ecran ne sait pas ce que la machine a fait de sa demande de demarrage : sa liaison a ete coupee.",
  },
  programme: {
    note: function () {
      return el("start-note");
    },
    sent: "demande envoyee : ",
    session: true,
    shows: function (snapshot) {
      return snapshot.mode === "seance";
    },
    taken: function (held) {
      return "demarrage pris par la machine : " + held.said;
    },
    lost: "cet ecran ne sait pas ce que la machine a fait de sa demande de demarrage : sa liaison a ete coupee.",
  },
  target: {
    note: function () {
      return el("manual-note");
    },
    sent: "cible envoyee : ",
    shows: function (snapshot, held) {
      var manual = snapshot.mode === "repos" ? null : snapshot.manual;
      return Boolean(manual) && holdsTarget(manual, held.rpm);
    },
    taken: function (held, frame) {
      var manual = frame ? frame.manual : null;
      return takenNote(held.said, Boolean(manual) && followsTarget(frame, manual));
    },
    lost: "cet ecran ne sait pas ce que la machine a fait de sa cible : sa liaison a ete coupee.",
  },
  reset: {
    note: function () {
      return el("fault-note");
    },
    sent: "demande envoyee : ",
    shows: function (snapshot, held) {
      return !(held.cleared && snapshot.fault);
    },
    taken: function (held, frame) {
      if (frame && !frame.fault) {
        held.cleared = true;
        return "reset pris par la machine : le variateur ne signale plus de defaut";
      }
      return "reset pris par la machine" + (stillFaulted(held, frame) ? " : le variateur signale encore le defaut" : "");
    },
    alarm: function (held, frame) {
      return stillFaulted(held, frame);
    },
    lost: "cet ecran ne sait pas ce que la machine a fait de sa demande de reset : sa liaison a ete coupee.",
  },
};

/** A drive can take a moment to drop its fault after a reset: said still faulted past a second. */
function stillFaulted(held, frame) {
  return Boolean(frame) && Boolean(frame.fault) && frame.at - held.at > LOOP_ANSWER_S;
}

function refusalWords(answer) {
  return "refus de la machine (" + new Date(answer.wall_clock).toLocaleTimeString() + ") : " + answer.detail;
}

function cancelledWords(answer) {
  return "demande annulee (" + new Date(answer.wall_clock).toLocaleTimeString() + ") : " + answer.detail;
}

/** The socket delivering frames and events as a request leaves, or null when none is open. */
function liveStream() {
  return state.connected ? state.stream : null;
}

function askConsole() {
  loadStatus().catch(function () {
    /* the periodic refresh asks again */
  });
}

/** What the taken note of `kind` reads now. A frame taken before the answer says nothing of it. */
function writeHeld(kind, held) {
  var ask = REQUESTS[kind];
  var snapshot = state.snapshot;
  var frame = snapshot && snapshot.at >= held.at ? snapshot : null;
  var said = ask.taken(held, frame);
  held.text = said;
  if (ask.alarm && ask.alarm(held, frame)) {
    problem(ask.note(), said);
  } else {
    ok(ask.note(), said);
  }
}

/**
 * A start was taken, and this screen cannot tell whether the session in progress is still the one
 * it started: its socket changed since, or the answer came from the status and not from an event.
 */
function doubtHeld(kind, held) {
  held.sure = false;
  held.text =
    "la machine a pris le demarrage demande par cet ecran (" + held.said + "), mais sa liaison a ete coupee depuis : " +
    "cet ecran ne sait pas si la seance en cours est encore celle-la.";
  problem(REQUESTS[kind].note(), held.text);
}

/** The 202 of a request: sent, not taken yet. `stream` is liveStream() at the click. */
function requestSent(kind, command, stream, rpm) {
  var ask = REQUESTS[kind];
  var said = kind === "target" ? command.detail : (command.kind + " " + command.detail).trim();
  var asked = { id: command.request_id, at: command.at, said: said, stream: stream, rpm: rpm, lost: false };
  delete state.held[kind];
  state.asked[kind] = asked;
  var answer = answerFor(asked.id);
  if (answer) {
    // The console's answer reached the screen before this 202.
    settleRequest(kind, asked, answer);
  } else {
    ok(ask.note(), ask.sent + said + " - pas encore prise par la machine");
  }
}

function answerFor(id) {
  for (var i = 0; i < state.fates.length; i += 1) {
    if (state.fates[i].id === id) {
      return state.fates[i];
    }
  }
  return null;
}

/**
 * One answer of the console about one request, from an event (`stream` is the socket that carried
 * it) or from the status (`stream` is null). Kept, then applied to the request of this page it
 * answers, if any. The same answer told twice, by an event and by the status, is applied once.
 */
function noteFate(answer, stream) {
  if (answerFor(answer.id)) {
    return;
  }
  answer.stream = stream;
  state.fates.push(answer);
  state.fates = state.fates.slice(-FATES_KEPT);
  Object.keys(state.asked).forEach(function (kind) {
    if (state.asked[kind].id === answer.id) {
      settleRequest(kind, state.asked[kind], answer);
    }
  });
}

/** This page's request `asked` has its answer: the note says it. */
function settleRequest(kind, asked, answer) {
  var ask = REQUESTS[kind];
  delete state.asked[kind];
  if (answer.fate === "refused") {
    problem(ask.note(), refusalWords(answer));
    return;
  }
  if (answer.fate === "cancelled") {
    problem(ask.note(), cancelledWords(answer));
    return;
  }
  if (ask.session && state.sessionsOver.indexOf(asked.id) !== -1) {
    // Taken, and the console has since said that session over: there is nothing left to say of it.
    ok(ask.note(), "");
    return;
  }
  var held = { id: asked.id, said: asked.said, at: answer.at, stream: answer.stream, rpm: asked.rpm, sure: true, cleared: false, text: "" };
  state.held[kind] = held;
  if (ask.session && held.stream !== state.stream) {
    // Learnt from the status, or over a socket that is no longer the one open.
    doubtHeld(kind, held);
  } else {
    writeHeld(kind, held);
  }
}

/** The session is over, the console says (`request` is the start it came from, when it knows). */
function sessionOver(request) {
  if (request) {
    state.sessionsOver.push(request);
    state.sessionsOver = state.sessionsOver.slice(-FATES_KEPT);
  }
  Object.keys(REQUESTS).forEach(function (kind) {
    var ask = REQUESTS[kind];
    if (!ask.session) {
      return;
    }
    var held = state.held[kind];
    if (held) {
      delete state.held[kind];
      if (ask.note().textContent === held.text) {
        ok(ask.note(), "");
      }
    }
    var asked = state.asked[kind];
    if (asked && asked.id === request) {
      // The session this request started is over before this screen heard that it was taken.
      delete state.asked[kind];
      ok(ask.note(), "");
    }
  });
}

/**
 * The first message of a socket. What the console published while this screen had no socket is
 * lost: a request still unanswered has lost its answer, and a session said taken may be over.
 */
function noteStream() {
  var opened = state.streamOpened;
  if (opened && opened.stream === state.stream) {
    return;
  }
  state.streamOpened = { stream: state.stream, sent: state.statusSent };
  var ask = false;
  Object.keys(REQUESTS).forEach(function (kind) {
    var asked = state.asked[kind];
    if (asked && asked.stream !== state.stream) {
      loseRequest(kind, asked);
      ask = true;
    }
    var held = state.held[kind];
    if (held && REQUESTS[kind].session && held.stream !== state.stream) {
      if (REQUESTS[kind].note().textContent !== held.text) {
        delete state.held[kind];
      } else if (held.sure) {
        doubtHeld(kind, held);
      }
      ask = true;
    }
  });
  if (ask) {
    askConsole();
  }
}

/** The answer to `asked` did not reach this screen: the note says so until the console is heard. */
function loseRequest(kind, asked) {
  if (!asked.lost) {
    asked.lost = true;
    problem(REQUESTS[kind].note(), REQUESTS[kind].lost);
  }
}

/** One event, for this page's requests and for the notes saying one was taken. */
function followRequests(event) {
  noteStream();
  if (event.request_id && (event.kind === "taken" || event.kind === "refused" || event.kind === "cancelled")) {
    noteFate(
      { id: event.request_id, fate: event.kind, at: event.at, wall_clock: event.wall_clock, detail: event.detail },
      state.stream
    );
  } else if (event.kind === "refused") {
    // Nobody's request: the machine took back a target it held. It is about the session on screen.
    if (manualOnScreen()) {
      problem(el("manual-note"), refusalWords(event));
    }
  } else if (event.kind === "session_idle") {
    sessionOver(event.request_id);
  }
}

/**
 * One /api/status answer, `sent` being its rank among the requests this page sent. It lists what
 * became of the last requests, and names the start the session in progress came from.
 */
function settleFromStatus(status, sent) {
  status.requests.forEach(function (row) {
    noteFate({ id: row.request_id, fate: row.fate, at: row.at, wall_clock: row.wall_clock, detail: row.detail }, null);
  });
  var opened = state.streamOpened;
  if (!state.connected || !opened || opened.stream !== state.stream || sent <= opened.sent) {
    // Asked before this socket delivered anything: the session it names may have ended unseen since.
    return;
  }
  Object.keys(REQUESTS).forEach(function (kind) {
    var ask = REQUESTS[kind];
    var held = state.held[kind];
    if (!held || !ask.session || held.sure) {
      return;
    }
    if (ask.note().textContent !== held.text) {
      delete state.held[kind];
    } else if (status.session_request === held.id) {
      // The session in progress is the one this screen started, and this socket has delivered
      // everything the console published since it was asked: its end will be heard.
      held.sure = true;
      held.stream = state.stream;
      writeHeld(kind, held);
    } else {
      delete state.held[kind];
      ok(ask.note(), "");
    }
  });
}

/** One frame, for the request of `kind` still unanswered and for the note saying one was taken. */
function settleRequestFrame(kind, snapshot) {
  var ask = REQUESTS[kind];
  var note = ask.note();
  var held = state.held[kind];
  if (held && note.textContent !== held.text) {
    // Something else has been written there since: the note is no longer this one's to keep.
    delete state.held[kind];
  } else if (held && snapshot.at >= held.at && !ask.shows(snapshot, held)) {
    // No longer true: the session is over, the machine has dropped the target, a fault is back.
    delete state.held[kind];
    ok(note, "");
  } else if (held && snapshot.at >= held.at && held.sure) {
    // Still true. What the note adds from the frames is said for as long as they show it.
    if (ask.taken(held, snapshot) !== held.text) {
      writeHeld(kind, held);
    }
  }
  var asked = state.asked[kind];
  if (asked && snapshot.at - asked.at > LOOP_ANSWER_S && !asked.lost) {
    // On one socket the answer comes before any frame taken after the request: it was lost.
    loseRequest(kind, asked);
    askConsole();
  }
}

/** One frame, for the starts and resets. */
function settleLoopAsks(snapshot) {
  noteStream();
  settleRequestFrame("manual", snapshot);
  settleRequestFrame("programme", snapshot);
  settleRequestFrame("reset", snapshot);
}

function doManualStart() {
  var note = el("manual-note");
  if (!el("manual-bench").checked) {
    problem(note, "cochez la declaration BANC (personne a bord : NON) avant de demarrer");
    return;
  }
  var stream = liveStream();
  api("/api/manual/start", {
    method: "POST",
    body: { occupancy: "bench", operator: rememberOperator(el("manual-operator").value) },
  })
    .then(function (command) {
      state.manualDraft = null;
      requestSent("manual", command, stream);
      loadStatus();
    })
    .catch(function (error) {
      // A refusal of the console's own is a fixed sentence of its own: shown as it is written.
      problem(note, error);
    });
}

/*
  What became of a manual target follows the rule above, with what the frames
  add to a target taken.

  Taken is true only for as long as the machine holds that target. Once the
  applied target is another one (a STOP, a verdict, another screen), the note
  is wiped rather than left to say that the machine follows a target it has
  dropped. The machine can also take a target back by itself, before the
  first step: that is a `refused` event with no identifier, written under the
  card of every screen that shows the session.

  Taken is not followed. The note adds "(suivie aux limites de mouvement)"
  only while the machine is walking the setpoint to the target, and takes the
  words off, frame by frame, for as long as something holds the setpoint
  (followsTarget): the note must not say of a speed that is held that it is
  on its way.
*/
function manualOnScreen() {
  var snapshot = state.snapshot;
  return Boolean(snapshot && snapshot.mode !== "repos" && snapshot.manual);
}

/*
  Whether the applied target is `rpm`, in the motor rpm the machine counts in:
  it rounds a target to a whole motor rpm, so the two differ by half of one at
  most. The ratio is read off the ceiling, which is never 0 in a session.
*/
function holdsTarget(manual, rpm) {
  var ceiling = manual.ceiling;
  if (!ceiling.output_rpm) {
    return false;
  }
  return Math.abs(manual.target.motor_rpm - (rpm * ceiling.motor_rpm) / ceiling.output_rpm) < 0.501;
}

/*
  Whether the machine is walking the setpoint to the target it holds, as of
  this frame. A target taken is not always a target followed:

  - a verdict that has the setpoint keeps it from the target. A reduce walks
    it down whatever the target is. A freeze holds it away from every target
    but one: zero is a stop asked for, and a stop is followed under a freeze;
  - with no verdict, the heart rate of a person on board can hold a rise: the
    box above the target says so, and a target above the setpoint waits. When
    the console no longer says whether it does, the box says "unknown", and a
    rise is not said to be followed either: the note may say less than is
    true, never more.
*/
function followsTarget(snapshot, manual) {
  var safety = snapshot.safety;
  if (safety) {
    return safety.action === "freeze" && manual.target.motor_rpm === 0;
  }
  return !(state.riseHeld && manual.target.motor_rpm > snapshot.setpoint.motor_rpm);
}

function takenNote(detail, followed) {
  return "cible prise par la machine : " + detail + (followed ? " (suivie aux limites de mouvement)" : "");
}

/** One frame, for the manual target. */
function settleManualTarget(snapshot) {
  noteStream();
  settleRequestFrame("target", snapshot);
}

function doManualApply() {
  var note = el("manual-note");
  var rpm = state.manualDraft || 0;
  var stream = liveStream();
  api("/api/manual/target", {
    method: "POST",
    body: { output_rpm: rpm, operator: operatorName() },
  })
    .then(function (command) {
      requestSent("target", command, stream, rpm);
    })
    .catch(function (error) {
      var first = state.asked.target;
      if (error.code === "request_waiting" && first && error.waiting === first.id) {
        // A second click before the loop took the first: the console names the request in the way
        // by its identifier, and it is this screen's own. The note will say what becomes of it.
        problem(
          note,
          "seconde demande refusee par la console : la cible " + first.said +
            " attend encore la machine (une demande a la fois). Reessayer dans un instant."
        );
        return;
      }
      problem(note, error);
    });
}

function doFaultReset() {
  var stream = liveStream();
  api("/api/drive/fault-reset", { method: "POST", body: { operator: operatorName() } })
    .then(function (command) {
      requestSent("reset", command, stream);
    })
    .catch(function (error) {
      problem(el("fault-note"), error);
    });
}

/*
  The motion indicator. It reads the MEASURED output speed, and it refuses to
  say "stopped" on a stale reading: an old zero is not a zero now. With STO
  jumpered there is no independent removal of torque, so this line is the only
  thing on the page that speaks about the shaft.
*/
function renderMotion(snapshot) {
  var nodes = [el("motion"), el("console-motion"), el("side-motion"), el("mobile-motion")];
  var measured = snapshot.measured.output_rpm;
  var label = "EN ROTATION";
  var kind = "warn";
  if (snapshot.drive_status_stale || measured === null || measured === undefined) {
    label = "VITESSE INCONNUE";
    kind = "bad";
  } else if (Math.abs(measured) < STANDSTILL_RPM) {
    label = "a l'arret";
    kind = "good";
  }
  nodes.forEach(function (node) {
    pill(node, label, kind);
  });
}

function renderZoneBand(bpm) {
  var zone = state.zone;
  var fill = el("zoneband-fill");
  var marker = el("zoneband-marker");
  if (!zone) {
    fill.style.left = "0";
    fill.style.width = "0";
    marker.style.left = "-10px";
    return;
  }
  var low = zone.low - 30;
  var high = zone.high + 30;
  var span = high - low;
  fill.style.left = (100 * (zone.low - low)) / span + "%";
  fill.style.width = (100 * (zone.high - zone.low)) / span + "%";
  if (bpm === null || bpm === undefined) {
    marker.style.left = "-10px";
    return;
  }
  var clamped = Math.min(high, Math.max(low, bpm));
  marker.style.left = (100 * (clamped - low)) / span + "%";
}

function addEvent(event) {
  followRequests(event);
  state.events.unshift(event);
  state.events = state.events.slice(0, 40);
  var list = el("events");
  list.textContent = "";
  state.events.forEach(function (item) {
    var li = document.createElement("li");
    li.className = "event-" + item.kind;
    var when = new Date(item.wall_clock).toLocaleTimeString();
    li.textContent =
      when + "  " + item.kind + (item.operator ? " [" + item.operator + "]" : "") +
      (item.detail ? "  " + item.detail : "");
    list.appendChild(li);
  });
}

/* ------------------------------------------------------------------- ECG  */

function addEcg(frame) {
  if (frame.gap) {
    // A break in the record, drawn as a break. Joining the two ends would put
    // a vertical stroke on the trace that reads exactly like a QRS complex.
    state.ecg.push(null);
  }
  for (var i = 0; i < frame.samples.length; i += 1) {
    state.ecg.push(frame.samples[i]);
  }
  if (state.ecg.length > ECG_CAPACITY) {
    state.ecg = state.ecg.slice(state.ecg.length - ECG_CAPACITY);
  }
  state.ecgSeq = frame.seq;
  pill(el("ecg-state"), frame.fs_hz + " Hz · seq " + frame.seq, frame.gap ? "warn" : "");
  pill(el("console-ecg-state"), frame.fs_hz + " Hz · seq " + frame.seq, frame.gap ? "warn" : "");
  state.dirty.ecg = true;
}

function drawEcg(canvas) {
  var ctx = canvas.getContext("2d");
  var width = canvas.width;
  var height = canvas.height;
  ctx.clearRect(0, 0, width, height);

  var values = state.ecg.filter(function (value) {
    return value !== null;
  });
  if (values.length < 2) {
    return;
  }
  var min = Math.min.apply(null, values);
  var max = Math.max.apply(null, values);
  var span = Math.max(max - min, 0.2);   // a floor, so a flat lead is not amplified
  var mid = (max + min) / 2;

  // Baseline, so a flat trace is visibly flat rather than absent.
  ctx.strokeStyle = "#2a2f37";
  ctx.beginPath();
  ctx.moveTo(0, height / 2);
  ctx.lineTo(width, height / 2);
  ctx.stroke();

  ctx.strokeStyle = "#6ea8fe";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  var started = false;
  for (var i = 0; i < state.ecg.length; i += 1) {
    var value = state.ecg[i];
    var x = (width * i) / Math.max(1, state.ecg.length - 1);
    if (value === null) {
      started = false;
      continue;
    }
    var y = height / 2 - ((value - mid) / span) * (height * 0.8);
    if (started) {
      ctx.lineTo(x, y);
    } else {
      ctx.moveTo(x, y);
      started = true;
    }
  }
  ctx.stroke();
}

/* --------------------------------------------------------------- sensors  */

var QUALITY_TEXT = {
  good: "bon signal",
  noisy: "bruite",
  mains_dominated: "parasite secteur",
  no_signal: "pas de signal",
};

var QUALITY_KIND = { good: "good", noisy: "warn", mains_dominated: "warn", no_signal: "bad" };

function qualityText(quality) {
  return QUALITY_TEXT[quality] || quality;
}

function qualityKind(entry) {
  if (entry.stale) {
    return "stale";
  }
  return QUALITY_KIND[entry.row.quality] || "bad";
}

function channelName(channel) {
  return "A" + (channel + 1);
}

/** The title of a sensor, on its page, on its card and in the tab: one wording for the three. */
function sensorTitle(row) {
  return row.kind + " · " + row.label;
}

function metricValue(metric) {
  if (metric.value === null || metric.value === undefined || !isFinite(metric.value)) {
    return "-";
  }
  var magnitude = Math.abs(metric.value);
  var digits = magnitude >= 100 ? 0 : magnitude >= 10 ? 1 : 2;
  return metric.value.toFixed(digits);
}

function loadSensors() {
  return api("/api/sensors")
    .then(function (listing) {
      state.sensorsOkAt = performance.now();
      renderSensors(listing ? listing.sensors : []);
    })
    .catch(function () {
      /* the staleness check below greys every channel once polls stop succeeding */
      refreshSensorStaleness();
    });
}

/*
  One poll's worth of rows. Entries (nav item and overview card) are built
  once per kind and then updated in place; a channel is "advanced" when its
  `at` moved since the previous poll, and stale when it has not for
  SENSOR_STALE_MS - the same rule as the socket watchdog, applied per channel.
*/
function renderSensors(rows) {
  var now = performance.now();
  var kinds = rows.map(function (row) {
    return row.kind;
  });
  if (kinds.join(",") !== state.sensorOrder.join(",")) {
    rebuildSensorEntries(rows);
  }
  rows.forEach(function (row) {
    var entry = state.sensors[row.kind];
    if (row.at !== null && row.at !== entry.lastAt) {
      entry.lastAt = row.at;
      entry.advancedAt = now;
    }
    entry.row = row;
    state.dirty["spark-" + row.kind] = true;
  });
  show(el("sensors-empty"), rows.length === 0);
  refreshSensorStaleness();
  state.sensorOrder.forEach(function (kind) {
    updateSensorCard(state.sensors[kind]);
  });
  if (state.view === "sensor") {
    renderSensorPage();
  }
}

function rebuildSensorEntries(rows) {
  var list = el("nav-sensor-list");
  var cards = el("sensor-cards");
  list.textContent = "";
  cards.textContent = "";
  var previous = state.sensors;
  state.sensors = {};
  state.sensorOrder = [];
  rows.forEach(function (row) {
    var old = previous[row.kind];
    var entry = {
      row: row,
      lastAt: old ? old.lastAt : null,
      advancedAt: old ? old.advancedAt : 0,
      stale: true,
      nav: null,
      card: null,
    };
    entry.nav = buildSensorNav(row);
    entry.card = buildSensorCard(row);
    list.appendChild(entry.nav);
    cards.appendChild(entry.card.root);
    state.sensors[row.kind] = entry;
    state.sensorOrder.push(row.kind);
  });
  if (state.view === "sensor" && !state.sensors[state.sensorKind]) {
    showView("sensors");
  } else {
    showView(state.view, state.sensorKind);
  }
}

function buildSensorNav(row) {
  var button = make("button", "nav-item nav-sensor");
  button.type = "button";
  var dot = make("span", "dot dot-stale");
  button.appendChild(dot);
  button.appendChild(make("span", "", row.kind));
  button.appendChild(make("span", "nav-channel", channelName(row.channel)));
  button.onclick = function () {
    showView("sensor", row.kind);
  };
  button.dot = dot;
  return button;
}

function buildSensorCard(row) {
  var root = make("button", "card sensor-card");
  root.type = "button";
  var head = make("h2");
  var title = make("span", "", sensorTitle(row));
  var badge = make("span", "pill");
  head.appendChild(title);
  head.appendChild(badge);
  root.appendChild(head);
  var canvas = make("canvas", "spark");
  canvas.width = 480;
  canvas.height = 90;
  canvas.id = "spark-" + row.kind;
  root.appendChild(canvas);
  var unit = make("div", "unit", "");
  root.appendChild(unit);
  var detail = make("p", "note", "");
  root.appendChild(detail);
  var metrics = make("dl", "grid");
  root.appendChild(metrics);
  root.onclick = function () {
    showView("sensor", row.kind);
  };
  return { root: root, badge: badge, canvas: canvas, unit: unit, detail: detail, metrics: metrics };
}

function updateSensorCard(entry) {
  var row = entry.row;
  var card = entry.card;
  var kind = qualityKind(entry);
  pill(card.badge, entry.stale ? "perime" : qualityText(row.quality), kind === "stale" ? "warn" : kind);
  text(
    card.unit,
    channelName(row.channel) + " · " + row.unit + " · " + row.display_rate + " ech/s" +
      (row.waveform.length ? " · " + num(row.waveform.length / Math.max(1, row.display_rate), 1) + " s" : "")
  );
  text(card.detail, entry.stale ? "lecture figee : aucune nouvelle fenetre depuis " + ageText(entry) : row.detail || "-");
  var metrics = row.metrics.slice(0, 4).map(function (metric) {
    return [metric.label, metricValue(metric) + (metric.unit ? " " + metric.unit : ""), entry.stale ? "stale" : ""];
  });
  if (!metrics.length) {
    metrics = [["mesures", "-"]];
  }
  grid(card.metrics, metrics);
  card.root.classList.toggle("is-stale", entry.stale);
}

function ageText(entry) {
  if (!entry.advancedAt) {
    return "le debut";
  }
  return Math.round((performance.now() - entry.advancedAt) / 1000) + " s";
}

function refreshSensorStaleness() {
  var now = performance.now();
  var pollOk = now - state.sensorsOkAt < SENSOR_STALE_MS;
  var goodCount = 0;
  state.sensorOrder.forEach(function (kind) {
    var entry = state.sensors[kind];
    var stale = !pollOk || entry.row.at === null || !entry.advancedAt || now - entry.advancedAt > SENSOR_STALE_MS;
    if (stale !== entry.stale) {
      entry.stale = stale;
      updateSensorCard(entry);
      state.dirty["spark-" + kind] = true;
      state.dirty["sensor-wave"] = true;
    }
    var dotKind = qualityKind(entry);
    entry.nav.dot.className = "dot dot-" + dotKind;
    entry.nav.title = entry.stale ? "perime" : qualityText(entry.row.quality);
    if (!stale && entry.row.quality === "good") {
      goodCount += 1;
    }
  });
  if (!state.sensorOrder.length) {
    pill(el("sensors-state"), pollOk ? "aucun canal" : "hors ligne", pollOk ? "" : "bad");
  } else if (!pollOk) {
    pill(el("sensors-state"), "hors ligne", "bad");
  } else {
    pill(
      el("sensors-state"),
      goodCount + " / " + state.sensorOrder.length + " bon signal",
      goodCount === state.sensorOrder.length ? "good" : "warn"
    );
  }
  if (state.view === "sensor" && state.sensors[state.sensorKind]) {
    renderSensorHeader(state.sensors[state.sensorKind]);
  }
}

function renderSensorHeader(entry) {
  var row = entry.row;
  var kind = qualityKind(entry);
  pill(el("sensor-quality"), entry.stale ? "perime" : qualityText(row.quality), kind === "stale" ? "warn" : kind);
  el("sensor-detail").classList.toggle("note-bad", entry.stale);
  if (entry.stale) {
    text(el("sensor-detail"), "Lecture figee : aucune nouvelle fenetre depuis " + ageText(entry) + ". Derniere qualite connue : " + qualityText(row.quality) + ".");
  }
  pill(
    el("sensor-age"),
    entry.stale ? "fige depuis " + ageText(entry) : row.display_rate + " ech/s · en direct",
    entry.stale ? "bad" : ""
  );
}

function renderSensorPage() {
  var entry = state.sensors[state.sensorKind];
  if (!entry) {
    return;
  }
  var row = entry.row;
  text(el("sensor-title"), sensorTitle(row));
  pill(el("sensor-channel"), "canal " + channelName(row.channel), "");
  text(el("sensor-detail"), row.detail ? "Qualite : " + qualityText(row.quality) + " - " + row.detail : "Qualite : " + qualityText(row.quality));
  renderSensorHeader(entry);
  text(el("sensor-description"), row.description);
  grid(el("sensor-info"), [
    ["canal", channelName(row.channel) + " (entree analogique " + (row.channel + 1) + ")"],
    ["unite", row.unit],
    ["cadence affichee", row.display_rate + " echantillons/s"],
    ["fenetre", num(row.waveform.length / Math.max(1, row.display_rate), 1) + " s (" + row.waveform.length + " points)"],
    ["qualite", qualityText(row.quality) + " (" + row.quality + ")"],
    ["role", "surveillance uniquement : ne commande pas le moteur"],
  ]);

  var tiles = el("sensor-metrics");
  tiles.textContent = "";
  row.metrics.forEach(function (metric) {
    var tile = make("div", "tile");
    tile.appendChild(make("div", "tile-label", metric.label));
    var value = make("div", "tile-value", metricValue(metric));
    if (entry.stale || metric.value === null) {
      value.classList.add("stale");
    }
    tile.appendChild(value);
    tile.appendChild(make("div", "tile-unit", metric.unit || ""));
    tiles.appendChild(tile);
  });
  show(el("sensor-metrics-empty"), row.metrics.length === 0);
  state.dirty["sensor-wave"] = true;
}

/* ------------------------------------------------------------- waveforms  */

/** Size a canvas's backing store to its displayed size, once per change. */
function fitCanvas(canvas) {
  var ratio = window.devicePixelRatio || 1;
  var width = Math.max(1, Math.round(canvas.clientWidth * ratio));
  var height = Math.max(1, Math.round(canvas.clientHeight * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  return ratio;
}

function niceStep(span, count) {
  var raw = span / Math.max(1, count);
  var power = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
  var scaled = raw / power;
  var nice = scaled < 1.5 ? 1 : scaled < 3 ? 2 : scaled < 7 ? 5 : 10;
  return nice * power;
}

function extent(values) {
  var min = Infinity;
  var max = -Infinity;
  for (var i = 0; i < values.length; i += 1) {
    var v = values[i];
    if (v < min) {
      min = v;
    }
    if (v > max) {
      max = v;
    }
  }
  return [min, max];
}

function drawSpark(canvas, entry) {
  var ratio = fitCanvas(canvas);
  var ctx = canvas.getContext("2d");
  var width = canvas.width;
  var height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  var values = entry.row.waveform;
  if (values.length < 2) {
    ctx.fillStyle = "#6b7280";
    ctx.font = 12 * ratio + "px sans-serif";
    ctx.fillText("pas de donnees", 8 * ratio, height / 2);
    return;
  }
  var range = extent(values);
  var span = range[1] - range[0];
  if (span <= 0) {
    // Flat: drawn through the middle, so "flat" and "absent" do not look alike.
    range = [range[0] - 1, range[0] + 1];
    span = 2;
  }
  var pad = 4 * ratio;
  ctx.strokeStyle = entry.stale ? "#6b7280" : "#6ea8fe";
  ctx.lineWidth = 1.25 * ratio;
  ctx.beginPath();
  for (var i = 0; i < values.length; i += 1) {
    var x = (width * i) / (values.length - 1);
    var y = height - pad - ((values[i] - range[0]) / span) * (height - 2 * pad);
    if (i === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();
}

/*
  The large trace of one sensor: auto-scaled with a little headroom, a time
  axis in seconds before now computed from display_rate, and the unit on the
  value axis. A stale channel is drawn grey and labelled, never left looking live.
*/
function drawWave(canvas, entry) {
  var ratio = fitCanvas(canvas);
  var ctx = canvas.getContext("2d");
  var width = canvas.width;
  var height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  var row = entry.row;
  var values = row.waveform;
  var left = 64 * ratio;
  var right = 12 * ratio;
  var top = 12 * ratio;
  var bottom = 30 * ratio;
  var plotW = width - left - right;
  var plotH = height - top - bottom;
  ctx.font = 12 * ratio + "px sans-serif";

  if (values.length < 2) {
    ctx.fillStyle = "#9aa2ae";
    ctx.fillText("pas encore de donnees pour ce canal", left, top + plotH / 2);
    return;
  }

  var range = extent(values);
  var span = range[1] - range[0];
  if (span <= 0) {
    span = Math.abs(range[0]) * 0.1 || 1;
  }
  var low = range[0] - span * 0.08;
  var high = range[1] + span * 0.08;
  var step = niceStep(high - low, 5);
  var duration = (values.length - 1) / Math.max(1, row.display_rate);

  // Value grid and labels.
  ctx.strokeStyle = "#262a31";
  ctx.fillStyle = "#6b7280";
  ctx.lineWidth = 1;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  var digits = step >= 1 ? 0 : Math.min(4, Math.ceil(-Math.log(step) / Math.LN10));
  for (var tick = Math.ceil(low / step) * step; tick <= high; tick += step) {
    var ty = top + plotH - ((tick - low) / (high - low)) * plotH;
    ctx.beginPath();
    ctx.moveTo(left, ty);
    ctx.lineTo(left + plotW, ty);
    ctx.stroke();
    ctx.fillText(tick.toFixed(digits), left - 6 * ratio, ty);
  }
  // Unit on the value axis.
  ctx.save();
  ctx.translate(14 * ratio, top + plotH / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.textAlign = "center";
  ctx.fillStyle = "#9aa2ae";
  ctx.fillText(row.unit, 0, 0);
  ctx.restore();

  // Time grid: seconds before the newest sample.
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  var tStep = niceStep(duration, 8);
  for (var t = 0; t <= duration + 1e-9; t += tStep) {
    var tx = left + plotW - (t / duration) * plotW;
    ctx.beginPath();
    ctx.moveTo(tx, top);
    ctx.lineTo(tx, top + plotH);
    ctx.stroke();
    ctx.fillText(t === 0 ? "0 s" : "-" + (tStep < 1 ? t.toFixed(1) : t.toFixed(0)), tx, top + plotH + 6 * ratio);
  }

  // The trace.
  ctx.strokeStyle = entry.stale ? "#6b7280" : "#6ea8fe";
  ctx.lineWidth = 1.5 * ratio;
  ctx.beginPath();
  for (var i = 0; i < values.length; i += 1) {
    var x = left + (plotW * i) / (values.length - 1);
    var y = top + plotH - ((values[i] - low) / (high - low)) * plotH;
    if (i === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();

  ctx.strokeStyle = "#32373f";
  ctx.strokeRect(left, top, plotW, plotH);

  if (entry.stale) {
    ctx.fillStyle = "rgba(15, 17, 20, 0.85)";
    ctx.fillRect(left + plotW / 2 - 230 * ratio, top + plotH / 2 - 18 * ratio, 460 * ratio, 36 * ratio);
    ctx.fillStyle = "rgba(245, 165, 36, 0.95)";
    ctx.font = "bold " + 16 * ratio + "px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("DONNEES FIGEES - NE PAS CROIRE CE TRACE", left + plotW / 2, top + plotH / 2);
  }
}

function markAllDirty() {
  state.dirty.ecg = true;
  state.dirty["sensor-wave"] = true;
  state.sensorOrder.forEach(function (kind) {
    state.dirty["spark-" + kind] = true;
  });
}

/*
  The single drawing callback. Only canvases on the visible page are drawn;
  a hidden canvas keeps its dirty flag and is drawn when its page is shown.
*/
function drawFrame() {
  var dirty = state.dirty;
  if (dirty.ecg) {
    if (state.view === "console") {
      drawEcg(el("console-ecg"));
      dirty.ecg = false;
    } else if (state.view === "run") {
      drawEcg(el("ecg"));
      dirty.ecg = false;
    }
  }
  if (state.view === "sensors") {
    state.sensorOrder.forEach(function (kind) {
      if (dirty["spark-" + kind]) {
        var entry = state.sensors[kind];
        drawSpark(entry.card.canvas, entry);
        dirty["spark-" + kind] = false;
      }
    });
  }
  if (state.view === "sensor" && dirty["sensor-wave"] && state.sensors[state.sensorKind]) {
    drawWave(el("sensor-wave"), state.sensors[state.sensorKind]);
    dirty["sensor-wave"] = false;
  }
  window.requestAnimationFrame(drawFrame);
}

/* --------------------------------------------------------------- setup UI */

/*
  The attendant's last presence ping is a monotonic instant on the Pi; the
  snapshot carries the same clock, so the difference is an honest age.
*/
function attendantSeen(status) {
  if (status.attendant_last_seen === null) {
    return "jamais";
  }
  if (!state.snapshot) {
    return "-";
  }
  return "il y a " + num(Math.max(0, state.snapshot.at - status.attendant_last_seen), 0) + " s";
}

var CAMERA_STATES = {
  absent: ["non branchee", "pill-warn", "Aucune camera n'est connectee : rien ne surveille la capsule. Les regles ci-dessous ne sont PAS actives."],
  waiting: ["en attente", "pill-warn", "Camera configuree, aucune image encore jugee."],
  clear: ["zone degagee", "pill-good", "Camera active : zone degagee, aucune regle ne s'oppose."],
  start_blocked: ["demarrage bloque", "pill-warn", ""],
  ramp_down: ["ralentissement", "pill-bad", ""],
  emergency_stop: ["ARRET D'URGENCE", "pill-bad", ""],
};

function loadCamera() {
  api("/api/camera")
    .then(function (row) {
      var shown = CAMERA_STATES[row.state] || ["inconnu", "pill-warn", ""];
      var pill = el("camera-pill");
      pill.textContent = shown[0] + (row.configured ? " (" + row.camera + ")" : "");
      pill.className = "pill " + shown[1];
      el("camera-note").textContent = row.detail || shown[2];
      var latched = el("camera-latched");
      latched.textContent = row.latched_rule
        ? "Verdict verrouille : " + row.latched_rule + " - acquitter dans Securite apres verification."
        : "";
      show(latched, Boolean(row.latched_rule));
    })
    .catch(function () {
      var pill = el("camera-pill");
      pill.textContent = "etat inconnu";
      pill.className = "pill pill-warn";
    });
}

/*
  Whether the emergency-stop latch is set, from /api/status. `estop_latched`
  is the interface's own flag: it stays false for a stop the camera latched,
  although that is the same latch and its acknowledgement demands the same
  "mushroom released" confirmation. `supervisor_estop` is the latch itself,
  whoever set it, and it still shows when go_silent has taken the standing
  verdict over. The standing verdict is read as well: it names the latch too,
  and it is all an answer without that field has.
*/
function estopLatched(status) {
  var standing = status.standing;
  return status.estop_latched || Boolean(status.supervisor_estop) ||
    Boolean(standing && standing.latched && standing.rule === "operator_estop");
}

/*
  The verdict in force and the most severe latched one, as the Securite and
  Configuration pages list them. /api/status gives both, whoever latched
  them: the supervisor, or the runtime itself (a drive found running at
  start-up, an enable that was not confirmed, a link it gave up on). It used
  to give the supervisor's alone, and these lines said "aucun" under a
  verdict of the runtime's that stood, on a screen with no live frame.

  The status is asked every five seconds and the frames arrive five a second,
  so while frames are arriving, a verdict they carry that is at least as
  severe as the status's is the one listed: the lines do not wait for the next
  status to follow a verdict the frames already show. With no live frame the
  status is all there is, and it now says what stands.

  Under go_silent a third line says what it replaced, when the console names
  one (`silenced`): that verdict is still latched, and neither of the two
  lines above shows it any more.
*/
function worseVerdict(polled, carried) {
  return carried && (!polled || carried.rank >= polled.rank) ? carried : polled;
}

function verdictWords(verdict) {
  return verdict ? verdict.rule + " / " + verdict.action : "aucun";
}

/** Whether frames are arriving: the socket is open and its last message is recent. */
function framesLive() {
  return state.connected && performance.now() - state.lastFrameAt < STALE_FRAME_MS;
}

function liveVerdict() {
  return framesLive() && state.snapshot ? state.snapshot.safety : null;
}

function renderStatusGrids(status) {
  var estop = estopLatched(status);
  var live = liveVerdict();
  var standing = verdictWords(worseVerdict(status.standing, live));
  var floor = verdictWords(worseVerdict(status.floor, live && live.latched ? live : null));
  // Shown only when there is one: with nothing behind go_silent the two grids are what they were.
  var behind = status.silenced ? [["verrouille derriere go_silent", verdictWords(status.silenced), "flag-bad"]] : [];
  grid(el("verdicts-grid"), [
    ["e-stop verrouille", estop ? "OUI" : "non", estop ? "flag-bad" : ""],
    ["verdict retenu", standing],
    ["plancher verrouille", floor],
  ].concat(behind, [
    ["regles actives", String(status.live.length)],
    ["accompagnant vu", attendantSeen(status)],
  ]));
  grid(el("system-grid"), [
    ["etat", status.run_state],
    ["e-stop verrouille", estop ? "OUI" : "non"],
    ["verdict retenu", standing],
    ["plancher verrouille", floor],
  ].concat(behind, [
    ["regles actives", String(status.live.length)],
    ["echantillons FC retenus", String(status.retained_hr_samples)],
    ["accompagnant vu", attendantSeen(status)],
    ["clients telemetrie", String(status.clients)],
    ["clients evinces", String(status.evictions)],
    ["ecg", status.ecg_fs_hz + " Hz, seq " + status.ecg_seq],
    ["profils", status.profile_ids.join(", ") || "aucun"],
    ["revision", String(status.profile_rev)],
    ["commandes acc/ref", status.counters[0] + " / " + status.counters[1]],
    ["snapshots", String(status.counters[2])],
  ]));
}

/*
  One /api/status answer, applied unless the one on screen is newer.

  Newer means written later by the machine, and the machine says when it
  wrote each answer (`at`, its own clock). Two requests in flight together can
  be answered in either order and arrive in either order: neither the order
  they were sent in nor the order they come back in says which is the present.

  One case the clock cannot settle alone. A request sent after the answer on
  screen had arrived was answered after it, whatever the clock reads: if its
  `at` is earlier, the machine's clock restarted (the Pi rebooted), and the
  answer is the newer one. Without that, every answer after a reboot would be
  dropped until the new clock caught up with the old one.
*/
function loadStatus() {
  var sent = (state.statusSent += 1);
  return api("/api/status").then(function (status) {
    if (state.status && status.at < state.status.at) {
      if (sent <= state.statusShownSent) {
        // In flight when the answer on screen arrived, and written before it: it can only say something older.
        return status;
      }
      if (state.statusBoot === state.machineBoot) {
        state.machineBoot += 1;
      }
    }
    state.statusShownSent = state.statusSent;
    state.statusBoot = state.machineBoot;
    state.status = status;
    state.statusAt = performance.now();
    if (state.estopReceipt && sent > state.estopReceiptSent) {
      // Asked after the E-STOP was latched: this answer carries it from here on.
      state.estopReceipt = null;
    }
    // An answer asked before a click says nothing about that click, even when it arrives after it.
    if (state.estopAsked && sent > state.estopAsked.sent) {
      estopSeenLatched(statusOpinion(status));
    }
    // "idle" with a request still in the mailbox is not a machine at rest: a start accepted during
    // a tick waits there for the next one, and the session it starts is the one a STOP was meant for.
    var atRest = status.run_state === "idle" && !status.pending;
    if (state.stopAsked && sent > state.stopAsked.sent && (status.run_state === "stopping" || atRest)) {
      stopSettled();
    }
    settleFromStatus(status, sent);
    noteConsoleRun(status, sent);
    noteStatusLatch(status, sent);
    // With no live frame the status is all that says whether an acknowledgement held.
    if (statusLeads()) {
      settleAcknowledgement(statusReport(status));
    }
    noteLatch("floor", status.floor ? status.floor.rule : null);
    noteLatch("estop", estopLatched(status) ? "operator_estop" : null);
    renderEstopBanner();
    renderDriveFault();
    // Red for a latched emergency stop whoever latched it: after the camera's, the state reads "idle".
    pill(
      el("run-state"),
      status.run_state,
      estopLatched(status) ? "bad" : status.run_state === "running" ? "warn" : ""
    );
    text(
      el("bind"),
      status.bind.host + ":" + status.bind.port +
        (status.bind.loopback ? " boucle locale" : " RESEAU") +
        (status.bind.token_required ? " · jeton" : " · sans jeton")
    );
    pill(
      el("attest-state"),
      status.attested ? "atteste" : "non atteste",
      status.attested ? "good" : "bad"
    );
    text(el("attest-statement"), status.attestation_statement);
    if (status.attestation) {
      ok(
        el("attest-note"),
        "atteste par " + status.attestation.operator + " a " +
          new Date(status.attestation.wall_clock).toLocaleTimeString() +
          " (valable jusqu'au prochain redemarrage du Pi)"
      );
    }
    el("start").disabled =
      !status.attested || status.run_state !== "idle" || !state.motionEnabled || !state.programsEnabled;
    renderStatusGrids(status);
    var live = el("verdicts-live");
    live.textContent = "";
    status.live.forEach(function (verdict) {
      var li = make("li", verdict.rank >= 3 ? "event-emergency_stop" : verdict.rank >= 1 ? "event-refused" : "");
      li.textContent = verdict.action + "  " + verdict.rule + (verdict.detail ? "  " + verdict.detail : "") +
        (verdict.latched ? "  [verrouille]" : "");
      live.appendChild(li);
    });
    var ports = el("ports");
    ports.textContent = "";
    if (!status.ports.length) {
      var none = document.createElement("li");
      none.textContent = "aucun port serie trouve";
      ports.appendChild(none);
    }
    status.ports.forEach(function (port) {
      var li = document.createElement("li");
      li.textContent = port.device + "  " + port.description;
      ports.appendChild(li);
    });
    return status;
  });
}

function loadProfiles() {
  return api("/api/profiles").then(function (listing) {
    state.profiles = listing.profiles;
    var select = el("profile");
    select.textContent = "";
    listing.profiles.forEach(function (profile) {
      var option = document.createElement("option");
      option.value = profile.profile_id;
      option.textContent = profile.name + " (" + profile.profile_id + ")";
      select.appendChild(option);
    });
    onProfileChange();
  });
}

function selectedProfile() {
  var id = el("profile").value;
  for (var i = 0; i < state.profiles.length; i += 1) {
    if (state.profiles[i].profile_id === id) {
      return state.profiles[i];
    }
  }
  return null;
}

function onProfileChange() {
  var profile = selectedProfile();
  if (!profile) {
    state.zone = null;
    return;
  }
  state.zone = { low: profile.zone_low_bpm, high: profile.zone_high_bpm };
  renderPlanLimits(profile, null);
}

/*
  The limits an operator approves before anybody is in the machine: the zone,
  the hard max, the critical line, the rpm ceiling - and the ceiling expressed
  all four ways, so the g-load it implies is on the screen rather than in
  somebody's head.
*/
function renderPlanLimits(profile, preview) {
  var rows = [
    ["zone", profile.zone_low_bpm + "-" + profile.zone_high_bpm + " bpm"],
    ["max absolu", profile.hard_max_bpm + " bpm"],
    ["critique", profile.critical_bpm + " bpm"],
    ["FC max du sujet", profile.subject_hr_max + " bpm"],
    ["plafond", profile.max_rpm + " tr/min moteur"],
    ["plafond echauffement", profile.warmup_rpm_ceiling + " tr/min moteur"],
    ["minimum de rotation", profile.min_run_rpm + " tr/min moteur"],
    ["HSP variateur", num(profile.hsp_hertz, 2) + " Hz"],
    ["total", secs(profile.total_duration_s)],
    ["palier", secs(profile.hold_s)],
  ];
  if (preview) {
    rows = rows.concat([
      ["charge au plafond", num(preview.ceiling.g_load, 3) + " g a " + num(preview.ceiling.output_rpm, 2) + " tr/min de sortie"],
      ["charge echauffement", num(preview.warmup_ceiling.g_load, 3) + " g"],
      ["charge minimum", num(preview.min_run.g_load, 3) + " g"],
      ["duree modifiee", preview.total_overridden ? "oui" : "non"],
    ]);
  }
  grid(el("plan-limits"), rows);

  var table = el("plan-timeline");
  table.textContent = "";
  var spans = preview ? preview.spans : null;
  if (!spans) {
    return;
  }
  var head = document.createElement("tr");
  ["phase", "de", "a", "duree"].forEach(function (label) {
    var th = document.createElement("th");
    th.textContent = label;
    head.appendChild(th);
  });
  table.appendChild(head);
  spans.forEach(function (span) {
    var tr = document.createElement("tr");
    [span.phase, secs(span.start_s), secs(span.end_s), secs(span.duration_s)].forEach(function (cell) {
      var td = document.createElement("td");
      td.textContent = cell;
      tr.appendChild(td);
    });
    table.appendChild(tr);
  });
}

function durationSeconds() {
  var minutes = parseFloat(el("duration").value);
  return isFinite(minutes) && minutes > 0 ? minutes * 60 : null;
}

/* --------------------------------------------------------------- actions  */

/*
  The rider. The list is the dashboard's (GET /api/roster): who holds the right
  on this machine, with the age and the max heart rate a manager set on the
  site. They are shown here and never typed: the console vets a programme on
  the list's values, when it arms it, whatever this page shows. A rider the
  list no longer holds is taken out of the choice, and the page says so.
*/
function loadRoster() {
  return api("/api/roster").then(renderRoster).catch(function (error) {
    problem(el("roster-note"), error);
  });
}

function rosterAge(seconds) {
  if (seconds === null) {
    return "?";
  }
  return seconds < 120 ? Math.round(seconds) + " s" : Math.floor(seconds / 60) + " min";
}

function renderRoster(roster) {
  state.roster = roster;
  var select = el("rider");
  var chosen = select.value;
  var shown = roster.riders
    .map(function (rider) {
      return rider.user_id + "\u0000" + rider.name;
    })
    .join("\u0001");
  if (shown !== state.rosterShown) {
    // Rebuilt only when the list changes: a refresh must not close a choice being made.
    state.rosterShown = shown;
    select.textContent = "";
    var none = document.createElement("option");
    none.value = "";
    none.textContent = roster.riders.length ? "- choisir le passager -" : "- aucun passager propose -";
    select.appendChild(none);
    roster.riders.forEach(function (rider) {
      var option = document.createElement("option");
      option.value = rider.user_id;
      option.textContent = rider.name;
      select.appendChild(option);
    });
  }
  var kept = selectedRider(chosen) ? chosen : "";
  if (chosen && !kept) {
    state.riderLost = true;
  }
  select.value = kept;
  var note = el("roster-note");
  if (roster.state !== "current") {
    problem(note, roster.detail);
  } else if (roster.problem) {
    problem(
      note,
      "liste du tableau de bord recue il y a " + rosterAge(roster.age_s) +
        " ; derniere demande sans reponse : " + roster.problem
    );
  } else {
    ok(note, "liste du tableau de bord recue il y a " + rosterAge(roster.age_s));
  }
  renderRiderFacts();
}

function selectedRider(id) {
  var wanted = id === undefined ? el("rider").value : id;
  var riders = state.roster ? state.roster.riders : [];
  for (var i = 0; i < riders.length; i += 1) {
    if (riders[i].user_id === wanted) {
      return riders[i];
    }
  }
  return null;
}

function renderRiderFacts() {
  var node = el("rider-facts");
  var rider = selectedRider();
  if (!rider) {
    if (state.riderLost) {
      problem(node, "le passager choisi n'est plus dans la liste du tableau de bord : en choisir un");
    } else {
      ok(node, "aucun passager choisi");
    }
    return;
  }
  var facts =
    "age " + (rider.age === null ? "inconnu" : rider.age + " ans") +
    " · FC max " + (rider.hr_max === null ? "inconnue" : rider.hr_max + " bpm") +
    " (reglees sur le site par un gestionnaire, non modifiables ici)";
  if (rider.programme) {
    problem(node, facts + " - " + rider.programme);
  } else {
    ok(node, facts);
  }
}

function onRiderChange() {
  state.riderLost = false;
  renderRiderFacts();
}

function riderId() {
  // null when none is chosen: the console refuses a programme for nobody, and says so.
  return el("rider").value || null;
}

function doPreview() {
  var profile = selectedProfile();
  if (!profile) {
    return;
  }
  api("/api/plan/preview", {
    method: "POST",
    body: { profile_id: profile.profile_id, total_duration_s: durationSeconds(), subject_id: riderId() },
  })
    .then(function (preview) {
      state.zone = { low: preview.profile.zone_low_bpm, high: preview.profile.zone_high_bpm };
      renderPlanLimits(preview.profile, preview);
      ok(el("start-note"), "plan resolu ; rien n'a ete demarre");
    })
    .catch(function (error) {
      problem(el("start-note"), error);
    });
}

function doStart() {
  var profile = selectedProfile();
  if (!profile) {
    return;
  }
  var stream = liveStream();
  api("/api/session/start", {
    method: "POST",
    body: {
      profile_id: profile.profile_id,
      operator: rememberOperator(el("operator").value),
      total_duration_s: durationSeconds(),
      subject_id: riderId(),
    },
  })
    .then(function (command) {
      requestSent("programme", command, stream);
      showView("run");
      loadStatus();
    })
    .catch(function (error) {
      problem(el("start-note"), error);
    });
}

/*
  STOP. Like the emergency stop, it does not wait in silence for its answer:
  see renderStopUnanswered().

  The console can refuse a STOP (nothing to end, a stop already under way, no
  operator name): that is an answer, said in an alert as it always was. A
  request that got no answer, or an error of the console's own, is something
  else: nothing says the session is ending, and the page says exactly that.
*/
function doStop() {
  state.manualDraft = null;
  var clicked = performance.now();
  var asked = (state.stopAsked = openDoubt(state.stopAsked, clicked));
  renderStopUnanswered();
  window.setTimeout(renderStopUnanswered, STOP_ANSWER_MS);
  api("/api/session/stop", {
    method: "POST",
    body: { operator: operatorName(), reason: "operator pressed STOP" },
  }).then(
    function () {
      // Accepted. A doubt that proof has ended since, and a later one, are left alone.
      if (state.stopAsked === asked) {
        stopSettled();
      }
      loadStatus().catch(function () {
        /* the frames show the session ending; the periodic refresh asks again */
      });
    },
    function (error) {
      var refused = error.status >= 400 && error.status < 500;
      if (state.stopAsked === asked) {
        if (refused) {
          stopSettled();
        } else {
          failDoubt(asked, clicked, error.message);
          renderStopUnanswered();
        }
      }
      window.alert(
        refused
          ? "STOP refuse : " + error.message
          : "la demande d'arret (STOP) a echoue : " + error.message + " - " + STOP_DOUBT_USE
      );
    }
  );
}

/*
  The emergency stop. No confirmation, no validation, no name required: it
  fires on the first click. One that asks a question first is not an emergency
  stop.

  It is a CONVENIENCE stop all the same - it depends on this browser, the
  network, the web server and the session process, any one of which can be the
  thing that has failed - and it is never safety-rated. The safety-rated stop
  is the wired mushroom, and while STO is jumpered even that one is a ramp.

  For the same reason the page does not wait in silence for the answer: see
  renderEstopUnanswered().
*/
function doEstop() {
  state.manualDraft = null;
  var clicked = performance.now();
  var asked = (state.estopAsked = openDoubt(state.estopAsked, clicked));
  // At once: a notice already up for a request that failed must not go on saying nothing is pending.
  renderEstopUnanswered();
  window.setTimeout(renderEstopUnanswered, ESTOP_ANSWER_MS);
  // The two outcomes are two handlers of the same `then`, not a `then` and a `catch` after it:
  // a fault of this page while it draws a receipt must not be reported as a request that failed.
  api("/api/session/estop", {
    method: "POST",
    body: { operator: operatorName(), reason: "operator pressed E-STOP" },
  }).then(
    function (receipt) {
      // What the receipt proves is recorded before anything is drawn.
      state.estopAsked = null;
      state.estopReceipt = receipt;
      state.estopReceiptSent = state.statusSent;
      renderEstopUnanswered();
      renderEstopBanner();
      loadStatus().catch(function () {
        /* the receipt keeps the banner up; the periodic refresh asks again */
      });
    },
    function (error) {
      // A doubt that proof has ended since is not reopened by an old request failing.
      if (state.estopAsked === asked) {
        failDoubt(asked, clicked, error.message);
      }
      renderEstopUnanswered();
      window.alert("la demande d'arret d'urgence a echoue : " + error.message + " - UTILISEZ L'ARRET CABLE");
    }
  );
}

function doAcknowledge() {
  api("/api/safety/acknowledge", {
    method: "POST",
    body: { operator: el("ack-operator").value, estop_released: el("ack-released").checked },
  })
    .then(function (record) {
      var said = "acquitte par " + record.operator + (record.cleared.length ? " : " + record.cleared.join(", ") : "");
      ok(el("ack-note"), said);
      // The 200 says the latches were cleared, not that they stay so: the next frames do.
      state.ackGiven = { at: record.at, cleared: record.cleared, text: said };
      loadStatus();
    })
    .catch(function (error) {
      state.ackGiven = null;
      problem(el("ack-note"), error);
      window.alert("acquittement refuse : " + error.message);
    });
}

/*
  The note under Acquitter speaks of the moment it was written. Two things
  make it false afterwards, and the page sees both in what it receives.

  A new latch. "nothing is latched to acknowledge", or "acquitte par ...",
  left under the button while a verdict has latched since, reads as the state
  of the machine. Each source says which rule it shows latched (the frames:
  the verdict in force; the status: the latched floor, whoever holds it, and
  the emergency stop). A rule that none of them showed is a new latch, and
  wipes the note.

  An acknowledgement that does not hold. The console answers 200 as soon as
  the latches are cleared, and a rule whose cause is still there latches again
  on the next tick: the frames may never show the verdict gone, so nothing
  above sees a new latch. So after a 200 the reports written after it are
  read: when one shows a rule the acknowledgement cleared latched again, the
  note says so. For the two rules that end a session (session_standstill,
  session_overrun) it also says when the acknowledgement will hold: once the
  session is over, which the page shows as Mode back to REPOS.

  The reports are the frames, five a second. With no live frame they are the
  status answers, one every five seconds (statusLeads): the note used to stay
  as it was written, "acquitte par ...", under a verdict the status showed
  latched again. A status answer names the latches the console holds and no
  mode, so the sentence about REPOS is neither added nor taken off on its
  word: the note may say less than is true there, never more.

  The two meet. A frame or a status may show the verdict gone between the
  acknowledgement and the tick that takes it back: that latch is the one the
  note then speaks of, not a new one that wipes it. And once a report written
  more than a second after the acknowledgement shows the verdict gone, what a
  source showed latched before the acknowledgement no longer stands: the same
  rule, later, is a new latch, also for a source that has not answered since.
*/
var SESSION_END_RULES = { session_standstill: true, session_overrun: true };

/*
  What one report says for that note: when the machine wrote it (its own
  clock), the rules it shows latched, the mode when it gives one, and under
  which source noteLatch is told of a rule it shows (a status answer feeds
  two: the emergency stop apart from the floor).
*/
function frameReport(snapshot) {
  var safety = snapshot.safety;
  return {
    at: snapshot.at,
    latched: safety && safety.latched ? [safety.rule] : [],
    mode: snapshot.mode,
    source: function () {
      return "frame";
    },
  };
}

function statusReport(status) {
  // The two places a console holds a latch: the emergency stop, and the latched floor. Not the
  // verdict in force: a latched one is one of those two, and a more severe warning that is not
  // latched can stand over the floor.
  var latched = estopLatched(status) ? ["operator_estop"] : [];
  if (status.floor) {
    latched.push(status.floor.rule);
  }
  return {
    at: status.at,
    latched: latched,
    mode: null,
    source: function (rule) {
      return rule === "operator_estop" ? "estop" : "floor";
    },
  };
}

/*
  Whether the status on screen is the newest word this page has of the
  machine: no frame is arriving, and the machine wrote that answer at or after
  the last frame it sent here, on its own clock. An answer written before that
  frame, however late it arrives, does not speak over it.
*/
function statusLeads() {
  var frame = state.estopFrame;
  return Boolean(state.status) && !framesLive() && !(frame && frameOutdatesStatus(frame));
}

function noteLatch(source, rule) {
  var seen = state.latchSeen;
  var known = Object.keys(seen).some(function (other) {
    return seen[other] === rule;
  });
  seen[source] = rule;
  if (rule && !known) {
    state.ackGiven = null;
    ok(el("ack-note"), "");
  }
}

var ACK_HOLDS_AT_REST = " La seance n'est pas terminee : l'acquittement tiendra une fois Mode revenu a REPOS.";

function takenBackNote(taken) {
  return "acquittement repris par la machine : " + taken.rule +
    " est de nouveau verrouille, sa regle s'est declenchee de nouveau." + (taken.waits ? ACK_HOLDS_AT_REST : "");
}

/*
  The note saying an acknowledgement was taken back speaks in the present: it
  is kept for as long as the reports show that rule latched, and wiped when
  they no longer do (acknowledged again, from this screen or another). The
  sentence about the session goes as soon as a frame shows Mode back to REPOS:
  left there, it would go on saying that the session is not over.
*/
function followTakenBack(report) {
  var taken = state.ackTaken;
  if (!taken) {
    return;
  }
  var note = el("ack-note");
  if (note.textContent !== takenBackNote(taken)) {
    // Something else has been written there since.
    state.ackTaken = null;
  } else if (report.latched.indexOf(taken.rule) === -1) {
    state.ackTaken = null;
    ok(note, "");
  } else if (taken.waits && report.mode === "repos") {
    taken.waits = false;
    problem(note, takenBackNote(taken));
  }
}

/** One report, a frame's or a status answer's (frameReport, statusReport), for the note under Acquitter. */
function settleAcknowledgement(report) {
  followTakenBack(report);
  var given = state.ackGiven;
  if (!given || report.at <= given.at) {
    return;
  }
  var note = el("ack-note");
  var again = report.latched.filter(function (rule) {
    return given.cleared.indexOf(rule) !== -1;
  })[0];
  if (note.textContent !== given.text) {
    // Something else has been written there since.
    state.ackGiven = null;
  } else if (again !== undefined) {
    state.ackGiven = null;
    // Known from here on: the noteLatch that follows this report must not read it as a new latch.
    state.latchSeen[report.source(again)] = again;
    state.ackTaken = {
      rule: again,
      // Only a frame says the mode: a status answer neither adds the sentence nor takes it off.
      waits: Boolean(SESSION_END_RULES[again]) && report.mode !== null && report.mode !== "repos",
    };
    problem(note, takenBackNote(state.ackTaken));
  } else if (report.at - given.at > LOOP_ANSWER_S) {
    // A second of the machine's clock with the verdict gone: it held.
    var seen = state.latchSeen;
    Object.keys(seen).forEach(function (source) {
      if (given.cleared.indexOf(seen[source]) !== -1) {
        seen[source] = null;
      }
    });
    state.ackGiven = null;
  }
}

function doAttest() {
  api("/api/safety/attest", {
    method: "POST",
    body: {
      operator: el("attest-operator").value,
      sto_jumper_removed: el("attest-jumper").checked,
      mushroom_wired_nc: el("attest-mushroom").checked,
    },
  })
    .then(function () {
      loadStatus();
    })
    .catch(function (error) {
      problem(el("attest-note"), error);
    });
}

function ping() {
  api("/api/presence", { method: "POST", body: { operator: operatorName() } }).catch(
    function () {
      /* the attendant rule will notice; nothing useful to show here */
    }
  );
}

/* ----------------------------------------------------------------- wiring */

function start() {
  var rememberedOperator = operatorName();
  OPERATOR_INPUT_IDS.forEach(function (id) {
    el(id).value = rememberedOperator;
    el(id).addEventListener("input", function () {
      var value = el(id).value;
      OPERATOR_INPUT_IDS.forEach(function (other) {
        if (other !== id) el(other).value = value;
      });
      rememberOperator(value);
    });
  });
  try {
    state.token = window.sessionStorage.getItem("anheart-token") || "";
  } catch {
    state.token = "";
  }
  el("token").value = state.token;

  el("save-token").onclick = function () {
    state.token = el("token").value;
    try {
      window.sessionStorage.setItem("anheart-token", state.token);
    } catch {
      /* kept for this page only */
    }
    boot();
  };
  el("record-export").onclick = exportRecord;
  el("nav-console").onclick = function () {
    showView("console");
  };
  el("nav-sensors").onclick = function () {
    showView("sensors");
  };
  el("nav-run").onclick = function () {
    showView("run");
  };
  el("nav-setup").onclick = function () {
    showView("setup");
  };
  el("nav-safety").onclick = function () {
    showView("safety");
  };
  el("run-goto-safety").onclick = function () {
    showView("safety");
  };
  el("nav-toggle").onclick = function () {
    if (el("sidebar").classList.contains("open")) {
      closeNav();
    } else {
      openNav();
    }
  };
  el("profile").onchange = onProfileChange;
  el("rider").onchange = onRiderChange;
  el("preview").onclick = doPreview;
  el("start").onclick = doStart;
  el("stop").onclick = doStop;
  el("estop").onclick = doEstop;
  el("ack").onclick = doAcknowledge;
  el("service-restore-button").onclick = doRestoreService;
  el("attest").onclick = doAttest;
  el("manual-start").onclick = doManualStart;
  el("manual-apply").onclick = doManualApply;
  el("manual-plus").onclick = function () {
    stepRpm(1);
  };
  el("manual-minus").onclick = function () {
    stepRpm(-1);
  };
  el("manual-step-up").onclick = function () {
    stepGr(1);
  };
  el("manual-step-down").onclick = function () {
    stepGr(-1);
  };
  el("fault-reset").onclick = doFaultReset;
  window.addEventListener("resize", markAllDirty);
  document.querySelector("main").addEventListener("click", closeNav);
  if (window.ResizeObserver) {
    // Fires when a banner is shown, hidden or re-wrapped, and only then.
    new window.ResizeObserver(fitBanners).observe(el("banners"));
    // And when the footer changes height: its status line goes on a narrow screen.
    new window.ResizeObserver(fitFooter).observe(el("footer"));
  } else {
    // showBanner() already measures on show and hide; a resize can re-wrap the text.
    window.addEventListener("resize", fitBanners);
    window.addEventListener("resize", fitFooter);
  }
  fitFooter();

  showView("console");
  boot();

  window.setInterval(refreshLiveness, 500);
  window.setInterval(loadPanel, PANEL_MS);
  window.setInterval(loadSensors, SENSORS_MS);
  window.setInterval(loadRoster, ROSTER_MS);
  window.setInterval(loadCamera, 1000);
  loadCamera();
  window.setInterval(ping, PRESENCE_MS);
  window.setInterval(function () {
    loadStatus().catch(function () {
      /* shown by the banner and the token note */
    });
  }, STATUS_MS);
  window.requestAnimationFrame(drawFrame);
}

function boot() {
  loadPanel();
  loadSensors();
  loadRoster();
  loadStatus()
    .then(loadProfiles)
    .then(function () {
      ok(el("token-note"), "connecte a la machine");
    })
    .catch(function (error) {
      problem(el("token-note"), error);
    });
  connect();
}

window.addEventListener("DOMContentLoaded", start);

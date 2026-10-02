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
var PRESENCE_MS = 5000;         // attendant ping; the rule freezes after 60 s
var STATUS_MS = 5000;           // status refresh (run state, verdicts, attestation)
var PANEL_MS = 1000;            // console link panel refresh
var SENSORS_MS = 1000;          // /api/sensors poll
var SENSOR_STALE_MS = 3500;     // a channel whose `at` has not moved for this long is stale
var ECG_CAPACITY = 1600;        // samples kept for the trace (~6 s at 250 Hz)
var STANDSTILL_RPM = 0.05;      // below this, the output shaft is called stopped
var STANDARD_G = 9.80665;       // m/s2, for the 0.1 Gr manual step

var state = {
  token: "",
  socket: null,
  connected: false,
  lastFrameAt: 0,
  snapshot: null,
  zone: null,            // {low, high} from the chosen profile, for the band
  profiles: [],
  ecg: [],               // numbers, with nulls marking a discontinuity
  ecgSeq: 0,
  events: [],
  reconnectTimer: null,
  motionEnabled: true,   // from /api/panel; false on the read-only console
  programsEnabled: true, // from /api/panel; false until milestone M5
  panel: null,           // the last /api/panel answer
  status: null,          // the last /api/status answer
  view: "console",
  sensorKind: null,      // the sensor shown on the per-sensor page
  sensors: {},           // kind -> {row, lastAt, advancedAt, stale, nav, card}
  sensorOrder: [],       // kinds, in configuration order
  sensorsOkAt: 0,        // performance.now() of the last successful poll
  manualDraft: null,     // the target being edited, output rpm; null = follow the machine
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

/** The first operator name typed anywhere on the page, for attribution. */
function operatorName() {
  var ids = ["manual-operator", "operator", "ack-operator", "attest-operator"];
  for (var i = 0; i < ids.length; i += 1) {
    var value = document.getElementById(ids[i]).value.trim();
    if (value) {
      return value;
    }
  }
  return "";
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
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      }
      return body;
    });
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
  The socket dropping, or the frames stopping, is shown as a full-width red
  banner and every live number is struck through at the same time. Both, not
  one: the banner says "the page is not live", the struck values say which
  numbers you must not believe.
*/
function refreshLiveness() {
  var fresh = state.connected && performance.now() - state.lastFrameAt < STALE_FRAME_MS;
  var banner = el("banner");
  show(banner, !fresh);
  if (!fresh) {
    text(
      el("banner-detail"),
      state.connected
        ? "la liaison est ouverte mais aucune donnee depuis " +
          Math.round((performance.now() - state.lastFrameAt) / 1000) +
          " s - la machine tourne peut-etre encore"
        : "la liaison avec la machine est coupee - la machine tourne peut-etre encore"
    );
  }
  ["hr", "measured-output", "setpoint-output", "console-hr", "console-output"].forEach(function (id) {
    el(id).classList.toggle("stale", !fresh);
  });
  ["run-mode", "side-motion", "side-safety", "mobile-mode", "mobile-motion"].forEach(function (id) {
    el(id).classList.toggle("stale", !fresh);
  });
  if (fresh) {
    pill(el("link-state"), "en direct", "good");
  } else {
    pill(el("link-state"), state.connected ? "donnees figees" : "hors ligne", "bad");
  }
  text(
    el("footer-status"),
    (state.connected ? "liaison ouverte" : "liaison coupee") +
      (state.snapshot ? " · " + state.snapshot.mode + " · " + state.snapshot.phase : "")
  );
  refreshSensorStaleness();
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
    } catch (err) {
      /* already closing; nothing to do */
    }
  }
  var socket = new WebSocket(socketUrl());
  state.socket = socket;

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
    } catch (err) {
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

function renderSnapshot(snapshot) {
  state.snapshot = snapshot;
  renderMode(snapshot);
  renderConsole(snapshot);
  renderManual(snapshot);

  /* --- heart rate, with its age carried alongside --------------------- */
  var hr = snapshot.heart_rate;
  var bpm = snapshot.live_bpm;
  text(el("hr"), bpm === null || bpm === undefined ? "-" : String(bpm));
  el("hr").classList.toggle("stale", !hr || hr.stale || bpm === null || bpm === undefined);
  if (hr) {
    pill(el("hr-quality"), hr.quality, hr.quality === "good" ? "good" : "bad");
  } else {
    pill(el("hr-quality"), "pas de signal", "bad");
  }
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
  pill(el("phase"), snapshot.phase, snapshot.phase === "done" ? "warn" : "");
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
    text(
      fault,
      "DEFAUT " + snapshot.fault.mnemonic + " (" + snapshot.fault.raw_code + ") : " +
        snapshot.fault.meaning + " : " + snapshot.fault.message
    );
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
  if (hr) {
    pill(el("console-hr-quality"), hr.quality, hr.quality === "good" ? "good" : "bad");
  } else {
    pill(el("console-hr-quality"), "pas de signal", "bad");
  }
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
    text(
      fault,
      "DEFAUT " + snapshot.fault.mnemonic + " (LFT brut " + snapshot.fault.raw_code + ") : " +
        snapshot.fault.meaning + " : " + snapshot.fault.message
    );
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

function renderPanel(panel) {
  state.panel = panel;
  if (!panel) {
    pill(el("console-mode"), "pas de console", "");
    return;
  }
  state.motionEnabled = panel.motion_enabled;
  state.programsEnabled = panel.programs_enabled;
  pill(el("console-mode"), panel.motion_enabled ? "mouvement actif" : "LECTURE SEULE", panel.motion_enabled ? "warn" : "good");
  show(el("console-readonly"), !panel.motion_enabled);
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
  The manual card. The draft is only ever a number on this screen until
  "Appliquer": nothing is sent on a +/- click. The target is what the machine
  holds, the draft is what the operator is composing, and the draft is shown
  in amber whenever the two differ so an unapplied edit cannot pass for the
  machine's state. When the manual session ends (STOP, E-STOP, a verdict) the
  draft is dropped, because after any stop the target is 0.
*/
function renderManual(snapshot) {
  var manual = snapshot.manual;
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
    ["rampe", manual.ramping ? "en cours, arrivee ~" + secs(manual.ramp_eta_s) : "cible atteinte"],
  ]);
  show(el("ramp-banner"), manual.ramping);
  if (manual.ramping) {
    text(
      el("ramp-banner-detail"),
      "vers " + num(manual.target.output_rpm, 2) + " tr/min de sortie (Gr " +
        num(manual.target.resultant_g, 3) + "), arrivee dans ~" + secs(manual.ramp_eta_s)
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

function doManualStart() {
  var note = el("manual-note");
  if (!el("manual-bench").checked) {
    problem(note, "cochez la declaration BANC (personne a bord : NON) avant de demarrer");
    return;
  }
  api("/api/manual/start", {
    method: "POST",
    body: { occupancy: "bench", operator: el("manual-operator").value },
  })
    .then(function (command) {
      state.manualDraft = null;
      ok(note, "accepte : " + command.kind + " " + command.detail + " (cible 0)");
      loadStatus();
    })
    .catch(function (error) {
      problem(note, error);
    });
}

function doManualApply() {
  var note = el("manual-note");
  api("/api/manual/target", {
    method: "POST",
    body: { output_rpm: state.manualDraft || 0, operator: el("manual-operator").value },
  })
    .then(function (command) {
      ok(note, "cible envoyee : " + command.detail + " - la machine y va aux limites de mouvement");
    })
    .catch(function (error) {
      problem(note, error);
    });
}

function doFaultReset() {
  api("/api/drive/fault-reset", { method: "POST", body: { operator: operatorName() } })
    .then(function (command) {
      ok(el("fault-note"), "reset demande : " + command.kind + " (le variateur doit le confirmer)");
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
  var title = make("span", "", row.kind + " · " + row.label);
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
  pill(card.badge, entry.stale ? "perime" : qualityText(row.quality), kind === "stale" ? "" : kind);
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
  pill(el("sensor-quality"), entry.stale ? "perime" : qualityText(row.quality), kind === "stale" ? "" : kind);
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
  text(el("sensor-title"), row.kind + " · " + row.label);
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

function loadStatus() {
  return api("/api/status").then(function (status) {
    state.status = status;
    pill(
      el("run-state"),
      status.run_state,
      status.estop_latched ? "bad" : status.run_state === "running" ? "warn" : ""
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
    if (!state.programsEnabled) {
      ok(el("start-note"), "seances programmees desactivees sur cette console (jalon M5) : utiliser le mode MANUEL");
    }
    grid(el("verdicts-grid"), [
      ["e-stop verrouille", status.estop_latched ? "OUI" : "non", status.estop_latched ? "flag-bad" : ""],
      ["verdict retenu", status.standing ? status.standing.rule + " / " + status.standing.action : "aucun"],
      ["plancher verrouille", status.floor ? status.floor.rule + " / " + status.floor.action : "aucun"],
      ["regles actives", String(status.live.length)],
      ["accompagnant vu", attendantSeen(status)],
    ]);
    var live = el("verdicts-live");
    live.textContent = "";
    status.live.forEach(function (verdict) {
      var li = make("li", verdict.rank >= 3 ? "event-emergency_stop" : verdict.rank >= 1 ? "event-refused" : "");
      li.textContent = verdict.action + "  " + verdict.rule + (verdict.detail ? "  " + verdict.detail : "") +
        (verdict.latched ? "  [verrouille]" : "");
      live.appendChild(li);
    });
    grid(el("system-grid"), [
      ["etat", status.run_state],
      ["e-stop verrouille", status.estop_latched ? "OUI" : "non"],
      ["verdict retenu", status.standing ? status.standing.rule + " / " + status.standing.action : "aucun"],
      ["plancher verrouille", status.floor ? status.floor.rule + " / " + status.floor.action : "aucun"],
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
    ]);
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

function doPreview() {
  var profile = selectedProfile();
  if (!profile) {
    return;
  }
  api("/api/plan/preview", {
    method: "POST",
    body: { profile_id: profile.profile_id, total_duration_s: durationSeconds() },
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

function riderAge() {
  // null when blank: the console refuses a programme for a rider of unknown age.
  var raw = el("rider-age").value.trim();
  if (raw === "") {
    return null;
  }
  var age = Number(raw);
  return Number.isInteger(age) ? age : null;
}

function doStart() {
  var profile = selectedProfile();
  if (!profile) {
    return;
  }
  api("/api/session/start", {
    method: "POST",
    body: {
      profile_id: profile.profile_id,
      operator: el("operator").value,
      total_duration_s: durationSeconds(),
      subject_age: riderAge(),
    },
  })
    .then(function (command) {
      ok(el("start-note"), "accepte : " + command.kind + " " + command.detail);
      showView("run");
      loadStatus();
    })
    .catch(function (error) {
      problem(el("start-note"), error);
    });
}

function doStop() {
  state.manualDraft = null;
  api("/api/session/stop", {
    method: "POST",
    body: { operator: operatorName(), reason: "operator pressed STOP" },
  })
    .then(function () {
      loadStatus();
    })
    .catch(function (error) {
      window.alert("STOP refuse : " + error.message);
    });
}

/*
  The emergency stop. No confirmation, no validation, no name required: it
  fires on the first click. One that asks a question first is not an emergency
  stop.

  It is a CONVENIENCE stop all the same - it depends on this browser, the
  network, the web server and the session process, any one of which can be the
  thing that has failed - and it is never safety-rated. The safety-rated stop
  is the wired mushroom, and while STO is jumpered even that one is a ramp.
*/
function doEstop() {
  state.manualDraft = null;
  api("/api/session/estop", {
    method: "POST",
    body: { operator: operatorName(), reason: "operator pressed E-STOP" },
  })
    .then(function (receipt) {
      text(
        el("banner-detail"),
        "ARRET D'URGENCE VERROUILLE (" + receipt.action + ") - surveillez la vitesse MESUREE : " +
          "la machine decelere, elle n'est pas arretee"
      );
      show(el("banner"), true);
      loadStatus();
    })
    .catch(function (error) {
      window.alert("la demande d'arret d'urgence a echoue : " + error.message + " - UTILISEZ L'ARRET CABLE");
    });
}

function doAcknowledge() {
  api("/api/safety/acknowledge", {
    method: "POST",
    body: { operator: el("ack-operator").value, estop_released: el("ack-released").checked },
  })
    .then(function (record) {
      ok(el("ack-note"), "acquitte par " + record.operator + (record.cleared.length ? " : " + record.cleared.join(", ") : ""));
      loadStatus();
    })
    .catch(function (error) {
      problem(el("ack-note"), error);
      window.alert("acquittement refuse : " + error.message);
    });
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
  try {
    state.token = window.sessionStorage.getItem("anheart-token") || "";
  } catch (err) {
    state.token = "";
  }
  el("token").value = state.token;

  el("save-token").onclick = function () {
    state.token = el("token").value;
    try {
      window.sessionStorage.setItem("anheart-token", state.token);
    } catch (err) {
      /* kept for this page only */
    }
    boot();
  };
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
  el("preview").onclick = doPreview;
  el("start").onclick = doStart;
  el("stop").onclick = doStop;
  el("estop").onclick = doEstop;
  el("ack").onclick = doAcknowledge;
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

  showView("console");
  boot();

  window.setInterval(refreshLiveness, 500);
  window.setInterval(loadPanel, PANEL_MS);
  window.setInterval(loadSensors, SENSORS_MS);
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

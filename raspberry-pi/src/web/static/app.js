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
     calm one.
  2. "Is it stopped" is answered by the MEASURED output speed, never by the
     setpoint. A commanded zero on a coasting mass is not a measured zero.
  3. Motor rpm, output rpm, Hz and g are rendered TOGETHER wherever a speed is
     shown, because the gearbox ratio is 49.79 and any one of them alone hides
     a fiftyfold error.

  The emergency stop calls the API immediately, with no confirmation of any
  kind. That is deliberate and must stay that way.
*/

"use strict";

/* ------------------------------------------------------------------ state */

var STALE_FRAME_MS = 2000;      // no frame for this long: the screen is not live
var RECONNECT_MS = 1500;        // socket retry interval
var PRESENCE_MS = 5000;         // attendant ping; the rule freezes after 60 s
var STATUS_MS = 5000;           // setup-view status refresh
var ECG_CAPACITY = 1600;        // samples kept for the trace (~6 s at 250 Hz)
var STANDSTILL_RPM = 0.05;      // below this, the output shaft is called stopped

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

/** A number for display, or an em dash. Never "NaN", never "null". */
function num(value, digits) {
  if (value === null || value === undefined || !isFinite(value)) {
    return "—";
  }
  return value.toFixed(digits === undefined ? 1 : digits);
}

function secs(value) {
  if (value === null || value === undefined || !isFinite(value)) {
    return "—";
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
    return [["motor", "—"], ["output", "—"], ["drive", "—"], ["load", "—"]];
  }
  return [
    ["motor", num(view.motor_rpm, 0) + " rpm"],
    ["output", num(view.output_rpm, 2) + " rpm"],
    ["drive", num(view.hertz, 2) + " Hz"],
    ["load", num(view.g_load, 3) + " g"],
  ];
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

function showView(name) {
  show(el("view-setup"), name === "setup");
  show(el("view-run"), name === "run");
  el("tab-setup").classList.toggle("tab-active", name === "setup");
  el("tab-run").classList.toggle("tab-active", name === "run");
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
        ? "the connection is open but no data has arrived for " +
          Math.round((performance.now() - state.lastFrameAt) / 1000) +
          " s - the machine may still be turning"
        : "the connection to the machine has dropped - the machine may still be turning"
    );
  }
  ["hr", "measured-output", "setpoint-output"].forEach(function (id) {
    el(id).classList.toggle("stale", !fresh);
  });
  text(
    el("footer-status"),
    (state.connected ? "socket open" : "socket down") +
      (state.snapshot ? " · " + state.snapshot.phase : "")
  );
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
    text(el("banner-detail"), envelope.notice || "this screen fell behind; reconnecting");
  }
}

/* ------------------------------------------------------------- rendering  */

function renderSnapshot(snapshot) {
  state.snapshot = snapshot;

  /* --- heart rate, with its age carried alongside --------------------- */
  var hr = snapshot.heart_rate;
  var bpm = snapshot.live_bpm;
  text(el("hr"), bpm === null || bpm === undefined ? "—" : String(bpm));
  el("hr").classList.toggle("stale", !hr || hr.stale || bpm === null || bpm === undefined);
  if (hr) {
    pill(el("hr-quality"), hr.quality, hr.quality === "good" ? "good" : "bad");
  } else {
    pill(el("hr-quality"), "no signal", "bad");
  }
  grid(el("hr-grid"), [
    ["target", snapshot.target_bpm === null ? "—" : snapshot.target_bpm + " bpm"],
    ["age", hr ? num(hr.age_s, 1) + " s" : "—", hr && hr.stale ? "stale" : ""],
    ["raw", hr && hr.bpm !== null ? hr.bpm + " bpm" : "—"],
    ["in zone", secs(snapshot.counters.in_zone_s)],
    ["above", secs(snapshot.counters.above_zone_s)],
    ["below", secs(snapshot.counters.below_zone_s)],
  ]);
  renderZoneBand(bpm);

  /* --- phase and progress --------------------------------------------- */
  pill(el("phase"), snapshot.phase, snapshot.phase === "done" ? "warn" : "");
  var elapsed = snapshot.elapsed_s || 0;
  var remaining = snapshot.remaining_s || 0;
  var total = elapsed + remaining;
  el("progress-fill").style.width = total > 0 ? (100 * elapsed) / total + "%" : "0";
  grid(el("time-grid"), [
    ["elapsed", secs(snapshot.elapsed_s)],
    ["remaining", secs(snapshot.remaining_s)],
    ["safety", snapshot.safety_action],
  ]);

  /* --- measured: the "is it stopped" indicator ------------------------ */
  var measured = snapshot.measured;
  text(el("measured-output"), num(measured.output_rpm, 2));
  grid(el("measured-grid"), speedRows(measured).concat([
    ["current", num(snapshot.current_a, 2) + " A"],
  ]));
  renderMotion(snapshot);

  /* --- setpoint: what was commanded, which is a different fact -------- */
  text(el("setpoint-output"), num(snapshot.setpoint.output_rpm, 2));
  grid(el("setpoint-grid"), speedRows(snapshot.setpoint));
  pill(
    el("setpoint-confirmed"),
    snapshot.setpoint_confirmed ? "echoed by the drive" : "not confirmed",
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
    ["status age", num(snapshot.drive_status_age_s, 1) + " s", snapshot.drive_status_stale ? "stale" : ""],
    ["current", num(snapshot.current_a, 2) + " A"],
    ["measured", num(measured.motor_rpm, 0) + " motor rpm"],
  ]);
  var fault = el("fault");
  show(fault, Boolean(snapshot.fault));
  if (snapshot.fault) {
    text(
      fault,
      "FAULT " + snapshot.fault.mnemonic + " (" + snapshot.fault.raw_code + "): " +
        snapshot.fault.meaning + " — " + snapshot.fault.message
    );
  }

  /* --- safety --------------------------------------------------------- */
  var safety = snapshot.safety;
  pill(
    el("safety-action"),
    snapshot.safety_action,
    snapshot.safety_rank >= 3 ? "bad" : snapshot.safety_rank >= 1 ? "warn" : "good"
  );
  grid(el("safety-grid"), safety
    ? [
        ["rule", safety.rule],
        ["detail", safety.detail],
        ["latched", safety.latched ? "yes" : "no"],
        ["standing for", num(safety.age_s, 1) + " s"],
      ]
    : [["rule", "nothing is asking"], ["latched", "no"]]);

  refreshLiveness();
}

/*
  The motion indicator. It reads the MEASURED output speed, and it refuses to
  say "stopped" on a stale reading: an old zero is not a zero now. With STO
  jumpered there is no independent removal of torque, so this line is the only
  thing on the page that speaks about the shaft.
*/
function renderMotion(snapshot) {
  var node = el("motion");
  var measured = snapshot.measured.output_rpm;
  if (snapshot.drive_status_stale || measured === null || measured === undefined) {
    pill(node, "SPEED UNKNOWN", "bad");
    return;
  }
  if (Math.abs(measured) < STANDSTILL_RPM) {
    pill(node, "standstill", "good");
    return;
  }
  pill(node, "TURNING", "warn");
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
  drawEcg();
}

function drawEcg() {
  var canvas = el("ecg");
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

/* --------------------------------------------------------------- setup UI */

function loadStatus() {
  return api("/api/status").then(function (status) {
    pill(
      el("run-state"),
      status.run_state,
      status.estop_latched ? "bad" : status.run_state === "running" ? "warn" : ""
    );
    text(
      el("bind"),
      status.bind.host + ":" + status.bind.port +
        (status.bind.loopback ? " loopback" : " NETWORK") +
        (status.bind.token_required ? " token" : " no token")
    );
    pill(
      el("attest-state"),
      status.attested ? "attested" : "not attested",
      status.attested ? "good" : "bad"
    );
    text(el("attest-statement"), status.attestation_statement);
    if (status.attestation) {
      ok(
        el("start-note"),
        "attested by " + status.attestation.operator + " at " +
          new Date(status.attestation.wall_clock).toLocaleTimeString()
      );
    }
    el("start").disabled = !status.attested || status.run_state !== "idle";
    grid(el("system-grid"), [
      ["run state", status.run_state],
      ["e-stop latched", status.estop_latched ? "YES" : "no"],
      ["standing verdict", status.standing ? status.standing.rule + " / " + status.standing.action : "none"],
      ["latched floor", status.floor ? status.floor.rule + " / " + status.floor.action : "none"],
      ["live rules", String(status.live.length)],
      ["hr samples retained", String(status.retained_hr_samples)],
      ["attendant last seen", status.attendant_last_seen === null ? "never" : num(status.attendant_last_seen, 1)],
      ["telemetry clients", String(status.clients)],
      ["clients evicted", String(status.evictions)],
      ["ecg", status.ecg_fs_hz + " Hz, seq " + status.ecg_seq],
      ["profiles", status.profile_ids.join(", ") || "none"],
      ["store rev", String(status.profile_rev)],
      ["commands acc/ref", status.counters[0] + " / " + status.counters[1]],
      ["snapshots", String(status.counters[2])],
    ]);
    var ports = el("ports");
    ports.textContent = "";
    if (!status.ports.length) {
      var none = document.createElement("li");
      none.textContent = "no serial ports found";
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
    ["zone", profile.zone_low_bpm + " – " + profile.zone_high_bpm + " bpm"],
    ["hard max", profile.hard_max_bpm + " bpm"],
    ["critical", profile.critical_bpm + " bpm"],
    ["subject max", profile.subject_hr_max + " bpm"],
    ["rpm ceiling", profile.max_rpm + " motor rpm"],
    ["warmup ceiling", profile.warmup_rpm_ceiling + " motor rpm"],
    ["min run", profile.min_run_rpm + " motor rpm"],
    ["drive HSP", num(profile.hsp_hertz, 2) + " Hz"],
    ["total", secs(profile.total_duration_s)],
    ["hold", secs(profile.hold_s)],
  ];
  if (preview) {
    rows = rows.concat([
      ["ceiling load", num(preview.ceiling.g_load, 3) + " g at " + num(preview.ceiling.output_rpm, 2) + " output rpm"],
      ["warmup load", num(preview.warmup_ceiling.g_load, 3) + " g"],
      ["min-run load", num(preview.min_run.g_load, 3) + " g"],
      ["duration overridden", preview.total_overridden ? "yes" : "no"],
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
  ["phase", "from", "to", "for"].forEach(function (label) {
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
      ok(el("start-note"), "plan resolved; nothing has been started");
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
  api("/api/session/start", {
    method: "POST",
    body: {
      profile_id: profile.profile_id,
      operator: el("operator").value,
      total_duration_s: durationSeconds(),
    },
  })
    .then(function (command) {
      ok(el("start-note"), "accepted: " + command.kind + " " + command.detail);
      showView("run");
      loadStatus();
    })
    .catch(function (error) {
      problem(el("start-note"), error);
    });
}

function doStop() {
  api("/api/session/stop", {
    method: "POST",
    body: { operator: el("operator").value || el("ack-operator").value, reason: "operator pressed STOP" },
  })
    .then(function () {
      loadStatus();
    })
    .catch(function (error) {
      window.alert("STOP refused: " + error.message);
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
  api("/api/session/estop", {
    method: "POST",
    body: { operator: el("operator").value || el("ack-operator").value, reason: "operator pressed E-STOP" },
  })
    .then(function (receipt) {
      text(
        el("banner-detail"),
        "EMERGENCY STOP LATCHED (" + receipt.action + ") - watch the MEASURED output speed: " +
          "the machine is ramping down, it has not stopped"
      );
      show(el("banner"), true);
      loadStatus();
    })
    .catch(function (error) {
      window.alert("the emergency stop request failed: " + error.message + " - USE THE WIRED STOP");
    });
}

function doAcknowledge() {
  api("/api/safety/acknowledge", {
    method: "POST",
    body: { operator: el("ack-operator").value, estop_released: el("ack-released").checked },
  })
    .then(function () {
      loadStatus();
    })
    .catch(function (error) {
      window.alert("acknowledgement refused: " + error.message);
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
      problem(el("start-note"), error);
    });
}

function ping() {
  api("/api/presence", { method: "POST", body: { operator: el("operator").value } }).catch(
    function () {
      /* the attendant rule will notice; nothing useful to show here */
    }
  );
}

/* ----------------------------------------------------------------- wiring */

function start() {
  state.token = window.sessionStorage.getItem("anheart-token") || "";
  el("token").value = state.token;

  el("save-token").onclick = function () {
    state.token = el("token").value;
    window.sessionStorage.setItem("anheart-token", state.token);
    boot();
  };
  el("tab-setup").onclick = function () {
    showView("setup");
  };
  el("tab-run").onclick = function () {
    showView("run");
  };
  el("profile").onchange = onProfileChange;
  el("preview").onclick = doPreview;
  el("start").onclick = doStart;
  el("stop").onclick = doStop;
  el("estop").onclick = doEstop;
  el("ack").onclick = doAcknowledge;
  el("attest").onclick = doAttest;

  showView("setup");
  boot();

  window.setInterval(refreshLiveness, 500);
  window.setInterval(ping, PRESENCE_MS);
  window.setInterval(function () {
    if (!el("view-setup").classList.contains("hidden")) {
      loadStatus().catch(function () {
        /* shown by the banner and the token note */
      });
    }
  }, STATUS_MS);
}

function boot() {
  loadStatus()
    .then(loadProfiles)
    .then(function () {
      ok(el("token-note"), "connected to the machine");
    })
    .catch(function (error) {
      problem(el("token-note"), error);
    });
  connect();
}

window.addEventListener("DOMContentLoaded", start);

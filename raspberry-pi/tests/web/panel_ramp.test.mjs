// The manual card says what the setpoint is doing, not what the target is (ANH-175).
//
// Under a `freeze` the console announced "RAMPE EN COURS ... arrivee dans ~0:20"
// over a setpoint that did not move. The runtime now reports no ramp and no
// arrival time for a target a freeze holds the setpoint away from; this file
// pins what the page does with it: no banner, no time, and never "cible
// atteinte" while target and setpoint differ. A descent to zero under a freeze
// is a real ramp, and is announced as one.
//
// A file of its own, with only the part of the DOM the manual card touches.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const assets = new URL("../../src/web/static/", import.meta.url);
const html = readFileSync(new URL("index.html", assets), "utf8");
const source = readFileSync(new URL("app.js", assets), "utf8");

class Element {
  constructor() {
    this.classes = new Set();
    this.children = [];
    this.textContent = "";
    this.className = "";
    this.classList = {
      toggle: (name, enabled) => {
        if (enabled) this.classes.add(name);
        else this.classes.delete(name);
      },
    };
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
}

function speed(motor) {
  return { motor_rpm: motor, output_rpm: motor / 49.79, hertz: 0, g_load: 0, resultant_g: 1 };
}

/**
 * Render one manual snapshot; return what the operator reads on the card.
 * `rampTo` is where the machine says the setpoint is going (motor rpm), when it says so;
 * `rule` names the warning in force, when the snapshot carries one.
 */
function card({ mode = "manuel", action = "none", setpoint, target, ramping, eta, rampTo, rule }) {
  const nodes = new Map(
    [...html.matchAll(/id="([^"]+)"/g)].map((match) => [match[1], new Element()]),
  );
  const context = vm.createContext({
    document: {
      getElementById: (id) => nodes.get(id) ?? null,
      createElement: () => new Element(),
    },
    window: { addEventListener: () => undefined },
  });
  vm.runInContext(source, context, { filename: fileURLToPath(new URL("app.js", assets)) });
  context.state.panel = { radius_m: 1.5 };
  const snapshot = {
    mode,
    safety_action: action,
    safety: rule === undefined ? null : { rule, action, latched: false },
    setpoint: setpoint === undefined ? undefined : speed(setpoint),
    manual: {
      occupancy_label: "bench",
      target: speed(target),
      ceiling: speed(300),
      min_run: speed(55),
      ramping,
      ramp_eta_s: eta,
      ...(rampTo === undefined ? {} : { ramp_to: rampTo === null ? null : speed(rampTo) }),
    },
  };
  context.state.snapshot = snapshot;
  context.renderManual(snapshot);
  const rows = nodes.get("manual-grid").children;
  const label = rows.findIndex((row) => row.textContent === "rampe");
  return {
    line: rows[label + 1].textContent,
    banner: !nodes.get("ramp-banner").classes.has("hidden"),
    detail: nodes.get("ramp-banner-detail").textContent,
  };
}

test("a target a freeze holds the setpoint away from: no banner, no time, not reached", () => {
  // Given 300 asked for, 130 in force, and the machine saying nothing walks there.
  const shown = card({ action: "freeze", setpoint: 130, target: 300, ramping: false, eta: null });
  // Then the card says the setpoint is held, and announces neither ramp nor arrival.
  assert.equal(shown.line, "consigne maintenue, cible non atteinte");
  assert.equal(shown.banner, false);
  assert.doesNotMatch(shown.line, /\d:\d\d/);
});

test("a target reached is still said reached, freeze or not", () => {
  for (const action of ["none", "freeze"]) {
    const shown = card({ action, setpoint: 200, target: 200, ramping: false, eta: 0 });
    assert.equal(shown.line, "cible atteinte");
    assert.equal(shown.banner, false);
  }
});

for (const mode of ["manuel", "arret"]) {
  test(`a descent to zero under a freeze is announced as the ramp it is (${mode})`, () => {
    // Given a target of zero being followed under a freeze: STOP, or a zero typed.
    const shown = card({ mode, action: "freeze", setpoint: 150, target: 0, ramping: true, eta: 65 });
    // Then the banner and the line carry the arrival time, as with no verdict.
    assert.equal(shown.line, "en cours, arrivee ~1:05");
    assert.equal(shown.banner, true);
    assert.match(shown.detail, /arrivee dans ~1:05/);
  });
}

test("an ordinary ramp keeps its banner and its arrival time", () => {
  const shown = card({ setpoint: 100, target: 300, ramping: true, eta: 30 });
  assert.equal(shown.line, "en cours, arrivee ~0:30");
  assert.equal(shown.banner, true);
});

test("a snapshot with no setpoint to compare with keeps the wording it had", () => {
  const shown = card({ target: 200, ramping: false, eta: 0 });
  assert.equal(shown.line, "cible atteinte");
});

// Under a `reduce` the setpoint is walked down towards zero whatever the target is (ANH-190).
// The card said "en cours, arrivee ~0:03": the time to climb back to a target the arm was moving
// away from. The machine now says where the setpoint is going (`ramp_to`) and how long that takes.

const LOWERED =
  "vers 0.00 tr/min de sortie sous l'avertissement hr_stale, la cible n'est pas suivie ; arrivee dans ~0:20 s'il dure";

test("a setpoint a reduce walks down is said to go down, to zero, and not to be on its way to the target", () => {
  // Given 300 asked for, 250 in force and coming down under hr_stale, 20 s from zero.
  const shown = card({ action: "reduce", rule: "hr_stale", setpoint: 250, target: 300, ramping: true, eta: 20, rampTo: 0 });
  // Then the line says so, with the destination and the time it would take if the warning stays.
  assert.equal(shown.line, "en baisse " + LOWERED);
  assert.doesNotMatch(shown.line, /en cours/);
  // And the banner stays up, since the speed is changing: it says the same, not "vers 6.03".
  assert.equal(shown.banner, true);
  assert.equal(shown.detail, "la vitesse baisse " + LOWERED);
  assert.doesNotMatch(shown.detail, /vers 6\.03/);
});

test("a reduce over a target of zero is the ramp to the target, said as it always was", () => {
  const shown = card({ action: "reduce", rule: "hr_stale", setpoint: 150, target: 0, ramping: true, eta: 65, rampTo: 0 });
  assert.equal(shown.line, "en cours, arrivee ~1:05");
  assert.match(shown.detail, /^vers 0\.00 tr\/min de sortie \(Gr 1\.000\), arrivee dans ~1:05$/);
});

test("with no verdict the destination is the target, and the wording is unchanged", () => {
  const shown = card({ setpoint: 100, target: 300, ramping: true, eta: 30, rampTo: 300 });
  assert.equal(shown.line, "en cours, arrivee ~0:30");
  assert.match(shown.detail, /^vers 6\.03 tr\/min de sortie/);
});

test("a freeze that holds the setpoint names no destination, and the card says held", () => {
  const shown = card({ action: "freeze", rule: "hr_stale", setpoint: 130, target: 300, ramping: false, eta: null, rampTo: null });
  assert.equal(shown.line, "consigne maintenue, cible non atteinte");
  assert.equal(shown.banner, false);
});

test("a descent whose warning the snapshot does not name is still said to be one", () => {
  const shown = card({ action: "reduce", setpoint: 250, target: 300, ramping: true, eta: 20, rampTo: 0 });
  assert.equal(
    shown.line,
    "en baisse vers 0.00 tr/min de sortie sous un avertissement, la cible n'est pas suivie ; arrivee dans ~0:20 s'il dure",
  );
});

test("a console that does not say where the setpoint is going is read as before", () => {
  // No `ramp_to` at all: the page cannot tell, and says what it said.
  const shown = card({ action: "reduce", rule: "hr_stale", setpoint: 250, target: 300, ramping: true, eta: 3 });
  assert.equal(shown.line, "en cours, arrivee ~0:03");
});

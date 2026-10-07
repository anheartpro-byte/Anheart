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

/** Render one manual snapshot; return what the operator reads on the card. */
function card({ mode = "manuel", action = "none", setpoint, target, ramping, eta }) {
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
    setpoint: setpoint === undefined ? undefined : speed(setpoint),
    manual: {
      occupancy_label: "bench",
      target: speed(target),
      ceiling: speed(300),
      min_run: speed(55),
      ramping,
      ramp_eta_s: eta,
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

// ANH-138: the programmes the Seance page offers change while it is open, when the console
// accepts a signed catalogue. The page asks for them again on its own cadence, rebuilds the
// choice only when they changed, and keeps the programme chosen while it is still offered.
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
    this.className = "";
    this.value = "";
    this.classList = {
      toggle: (name, enabled) => {
        if (enabled) this.classes.add(name);
        else this.classes.delete(name);
      },
      contains: (name) => this.classes.has(name),
    };
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  addEventListener() {}
}

function panel() {
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
  // Clearing a node's text clears its children, as the DOM does.
  for (const node of nodes.values()) {
    let words = "";
    Object.defineProperty(node, "textContent", {
      get: () => words,
      set: (value) => {
        words = value;
        if (value === "") node.children = [];
      },
    });
  }
  return { context, nodes };
}

function profile(profileId, name, zoneLow = 118) {
  return {
    profile_id: profileId,
    name,
    zone_low_bpm: zoneLow,
    zone_high_bpm: 138,
    hard_max_bpm: 148,
    critical_bpm: 158,
    subject_hr_max: 162,
    max_rpm: 276,
    warmup_rpm_ceiling: 165,
    min_run_rpm: 55,
    hsp_hertz: 10,
    total_duration_s: 1800,
    hold_s: 900,
  };
}

const listing = (...profiles) => ({ rev: 1, profiles });
const options = (node) => node.children.map((option) => option.value);

test("the programmes are offered, the first one chosen, its zone drawn", () => {
  const { context, nodes } = panel();
  context.renderProfiles(listing(profile("standard_30_min", "30 min"), profile("jog_court", "Jog")));
  assert.deepEqual(options(nodes.get("profile")), ["standard_30_min", "jog_court"]);
  assert.equal(nodes.get("profile").value, "standard_30_min");
  assert.equal(context.state.zone.low, 118);
});

test("a refresh that changes nothing rebuilds nothing and keeps the choice", () => {
  const { context, nodes } = panel();
  const same = listing(profile("standard_30_min", "30 min"), profile("jog_court", "Jog"));
  context.renderProfiles(same);
  nodes.get("profile").value = "jog_court";
  const before = nodes.get("profile").children;
  context.renderProfiles(JSON.parse(JSON.stringify(same)));
  assert.equal(nodes.get("profile").children, before, "the options were not rebuilt");
  assert.equal(nodes.get("profile").value, "jog_court");
});

test("a new catalogue replaces the choice, keeping the programme chosen while it is offered", () => {
  const { context, nodes } = panel();
  context.renderProfiles(listing(profile("standard_30_min", "30 min"), profile("jog_court", "Jog")));
  nodes.get("profile").value = "jog_court";
  context.renderProfiles(listing(profile("jog_court", "Jog"), profile("jog_long", "Jog long", 120)));
  assert.deepEqual(options(nodes.get("profile")), ["jog_court", "jog_long"]);
  assert.equal(nodes.get("profile").value, "jog_court");
  // Withdrawn from the catalogue: the first one offered is chosen, and its zone drawn.
  context.renderProfiles(listing(profile("jog_long", "Jog long", 120)));
  assert.equal(nodes.get("profile").value, "jog_long");
  assert.equal(context.state.zone.low, 120);
  // None left: nothing is chosen, and no zone is drawn.
  context.renderProfiles(listing());
  assert.equal(nodes.get("profile").value, "");
  assert.equal(context.state.zone, null);
});

test("the programmes are asked for at boot and again on their own cadence", async () => {
  assert.match(source, /window\.setInterval\(refreshProfiles, PROFILES_MS\);/);
  assert.match(/function boot\(\) \{[\s\S]*?\n\}/.exec(source)?.[0] ?? "", /\.then\(loadProfiles\)/);
  const { context } = panel();
  const asked = [];
  context.api = (path) => {
    asked.push(path);
    return path === "/api/profiles"
      ? Promise.resolve(listing(profile("standard_30_min", "30 min")))
      : Promise.reject(new Error("unexpected"));
  };
  await context.refreshProfiles();
  assert.deepEqual(asked, ["/api/profiles"]);
  // A refresh that fails says nothing more than the banner already does.
  context.api = () => Promise.reject(new Error("console unreachable"));
  await context.refreshProfiles();
});

// ANH-138: the programmes the Seance page offers change while it is open, when the console
// accepts a signed catalogue. The page asks for them again on its own cadence and rebuilds the
// choice only when they changed. The programme chosen, and its preview, stand while it is
// offered unchanged; withdrawn or changed, the page says so, chooses nothing in its place, and
// Start waits for a preview of what the operator chooses then.
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
    this.style = {};
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
      documentElement: { style: { setProperty: () => undefined } },
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
const settle = () => new Promise((resolve) => setImmediate(resolve));
const speed = { g_load: 1, output_rpm: 10 };

// The console's answers, and every request the page sends, as [path, body].
function console_(context) {
  const sent = [];
  context.api = (path, init) => {
    sent.push([path, init?.body]);
    if (path === "/api/plan/preview") {
      const profile = context.state.profiles.find((p) => p.profile_id === init.body.profile_id);
      return Promise.resolve({ profile, spans: [], ceiling: speed, warmup_ceiling: speed, min_run: speed, total_overridden: false });
    }
    return new Promise(() => {});
  };
  context.liveStream = () => null;
  context.rememberOperator = (value) => value;
  return sent;
}

const starts = (sent) => sent.filter(([path]) => path === "/api/session/start").map(([, body]) => body.profile_id);

// The operator chooses `id` from the list and previews it.
async function chooseAndPreview(context, nodes, id) {
  nodes.get("profile").value = id;
  context.onProfileChosen();
  context.doPreview();
  await settle();
  assert.equal(nodes.get("start-note").textContent, "plan resolu ; rien n'a ete demarre");
}

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

test("a new catalogue keeps the programme chosen, and its preview, while it is offered unchanged", async () => {
  const { context, nodes } = panel();
  const sent = console_(context);
  context.renderProfiles(listing(profile("standard_30_min", "30 min"), profile("jog_court", "Jog")));
  await chooseAndPreview(context, nodes, "jog_court");
  const drawn = nodes.get("plan-limits").children.length;
  context.renderProfiles(listing(profile("jog_court", "Jog"), profile("jog_long", "Jog long", 120)));
  assert.deepEqual(options(nodes.get("profile")), ["jog_court", "jog_long"]);
  assert.equal(nodes.get("profile").value, "jog_court");
  assert.equal(nodes.get("start-note").textContent, "plan resolu ; rien n'a ete demarre");
  assert.equal(nodes.get("plan-limits").children.length, drawn, "the preview's rows are still drawn");
  context.doStart();
  assert.deepEqual(starts(sent), ["jog_court"]);
});

test("the programme previewed is withdrawn: nothing is chosen in its place and Start waits", async () => {
  const { context, nodes } = panel();
  const sent = console_(context);
  context.renderProfiles(listing(profile("jog_long", "Jog long", 120), profile("jog_court", "Jog")));
  await chooseAndPreview(context, nodes, "jog_court");
  // The refresh after the console accepted a catalogue without it.
  context.renderProfiles(listing(profile("jog_long", "Jog long", 120)));
  assert.equal(nodes.get("profile").value, "");
  assert.equal(context.state.zone, null);
  assert.deepEqual(nodes.get("plan-limits").children, [], "its preview is no longer drawn");
  assert.match(nodes.get("start-note").textContent, /^Programme Jog \(jog_court\) retire par le catalogue/);
  assert.equal(nodes.get("start-note").className, "note note-bad");
  assert.equal(nodes.get("start").disabled, true);
  context.doStart();
  assert.deepEqual(starts(sent), [], "nothing is started");
  assert.match(nodes.get("start-note").textContent, /^Depart refuse/);
  // A later refresh that offers programmes again still chooses none of them.
  context.renderProfiles(listing(profile("jog_long", "Jog long", 120), profile("jog_neuf", "Neuf")));
  assert.equal(nodes.get("profile").value, "");
  // Chosen but not previewed: still no Start.
  nodes.get("profile").value = "jog_long";
  context.onProfileChosen();
  assert.equal(nodes.get("start-note").textContent, "Faire l'apercu du programme choisi avant de demarrer.");
  context.doStart();
  assert.deepEqual(starts(sent), []);
  // Chosen and previewed: Start sends that one.
  context.doPreview();
  await settle();
  assert.equal(nodes.get("start-note").textContent, "plan resolu ; rien n'a ete demarre");
  assert.equal(context.state.previewNeeded, false);
  context.doStart();
  assert.deepEqual(starts(sent), ["jog_long"]);
});

test("the programme previewed changes under the same id: the preview no longer holds", async () => {
  const { context, nodes } = panel();
  const sent = console_(context);
  context.renderProfiles(listing(profile("jog_court", "Jog")));
  await chooseAndPreview(context, nodes, "jog_court");
  context.renderProfiles(listing(profile("jog_court", "Jog", 124)));
  assert.equal(nodes.get("profile").value, "jog_court");
  assert.equal(context.state.zone.low, 124, "the new limits are drawn");
  assert.match(nodes.get("start-note").textContent, /modifie par le catalogue de la console : l'apercu ne vaut plus/);
  assert.equal(nodes.get("start").disabled, true);
  context.doStart();
  assert.deepEqual(starts(sent), []);
  context.doPreview();
  await settle();
  context.doStart();
  assert.deepEqual(starts(sent), ["jog_court"]);
});

test("a preview answered after its programme was withdrawn resolves nothing", async () => {
  const { context, nodes } = panel();
  const sent = console_(context);
  context.renderProfiles(listing(profile("jog_long", "Jog long", 120), profile("jog_court", "Jog")));
  nodes.get("profile").value = "jog_court";
  context.onProfileChosen();
  context.doPreview();
  context.renderProfiles(listing(profile("jog_long", "Jog long", 120)));
  await settle();
  assert.match(nodes.get("start-note").textContent, /retire par le catalogue/);
  assert.equal(context.state.previewNeeded, true);
  assert.deepEqual(nodes.get("plan-limits").children, []);
  context.doStart();
  assert.deepEqual(starts(sent), []);
});

test("an operator's own choice clears a preview said under the previous one", async () => {
  const { context, nodes } = panel();
  console_(context);
  context.renderProfiles(listing(profile("jog_long", "Jog long", 120), profile("jog_court", "Jog")));
  await chooseAndPreview(context, nodes, "jog_court");
  nodes.get("profile").value = "jog_long";
  context.onProfileChosen();
  assert.equal(nodes.get("start-note").textContent, "");
  assert.equal(context.state.zone.low, 120);
});

test("none left: nothing is chosen, and no zone is drawn", () => {
  const { context, nodes } = panel();
  context.renderProfiles(listing(profile("standard_30_min", "30 min")));
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

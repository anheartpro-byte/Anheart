// ANH-84: the Seance page chooses the rider from the dashboard's list, and only shows
// the age and the max heart rate the site set. Nothing about the rider is typed here.
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

function panel(storage = new Map()) {
  const nodes = new Map(
    [...html.matchAll(/id="([^"]+)"/g)].map((match) => [match[1], new Element()]),
  );
  const context = vm.createContext({
    document: {
      getElementById: (id) => nodes.get(id) ?? null,
      createElement: () => new Element(),
    },
    window: {
      addEventListener: () => undefined,
      sessionStorage: {
        getItem: (key) => storage.get(key) ?? null,
        setItem: (key, value) => storage.set(key, value),
      },
    },
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
  return { context, nodes, storage };
}

const ADULT = {
  user_id: "k17adult",
  name: "Synthetic Adult",
  hr_max: 186,
  age: 34,
  programme: null,
};
const UNSET = {
  user_id: "k17unset",
  name: "Synthetic Unset",
  hr_max: null,
  age: 41,
  programme:
    "demarrage refuse : FC max du passager non renseignee sur le site (a regler par un gestionnaire)",
};

function roster(riders, overrides = {}) {
  return { state: "current", detail: "", age_s: 12.4, problem: null, riders, ...overrides };
}

const options = (node) => node.children.map((option) => [option.value, option.textContent]);

test("the list of the dashboard is offered, with nobody chosen", () => {
  const { context, nodes } = panel();
  context.renderRoster(roster([ADULT, UNSET]));
  assert.deepEqual(options(nodes.get("rider")), [
    ["", "- choisir le passager -"],
    ["k17adult", "Synthetic Adult"],
    ["k17unset", "Synthetic Unset"],
  ]);
  assert.equal(nodes.get("rider").value, "");
  assert.equal(nodes.get("rider-facts").textContent, "aucun passager choisi");
  assert.equal(nodes.get("roster-note").textContent, "liste du tableau de bord recue il y a 12 s");
});

test("the rider's age and max heart rate are shown, not offered for typing", () => {
  const { context, nodes } = panel();
  context.renderRoster(roster([ADULT, UNSET]));
  nodes.get("rider").value = "k17adult";
  context.onRiderChange();
  const facts = nodes.get("rider-facts");
  assert.equal(
    facts.textContent,
    "age 34 ans · FC max 186 bpm (reglees sur le site par un gestionnaire, non modifiables ici)",
  );
  assert.equal(facts.className, "note");
  // No field of the page takes an age or a max heart rate any more.
  assert.equal(nodes.has("rider-age"), false);
  assert.doesNotMatch(html, /id="rider-age"/);
});

test("a rider the site has not finished is said, in red, before any click", () => {
  const { context, nodes } = panel();
  context.renderRoster(roster([ADULT, UNSET]));
  nodes.get("rider").value = "k17unset";
  context.onRiderChange();
  const facts = nodes.get("rider-facts");
  assert.equal(
    facts.textContent,
    "age 41 ans · FC max inconnue (reglees sur le site par un gestionnaire, non modifiables ici)" +
      " - " + UNSET.programme,
  );
  assert.equal(facts.className, "note note-bad");
});

test("a rider the list no longer holds is taken out of the choice, and the page says so", () => {
  const { context, nodes } = panel();
  context.renderRoster(roster([ADULT, UNSET]));
  nodes.get("rider").value = "k17adult";
  context.onRiderChange();
  context.renderRoster(roster([UNSET]));
  assert.equal(nodes.get("rider").value, "");
  assert.deepEqual(options(nodes.get("rider")), [
    ["", "- choisir le passager -"],
    ["k17unset", "Synthetic Unset"],
  ]);
  assert.equal(
    nodes.get("rider-facts").textContent,
    "le passager choisi n'est plus dans la liste du tableau de bord : en choisir un",
  );
  // Choosing again lifts it.
  nodes.get("rider").value = "k17unset";
  context.onRiderChange();
  assert.match(nodes.get("rider-facts").textContent, /^age 41 ans/);
});

test("a refresh that changes nothing keeps the choice and the options as they are", () => {
  const { context, nodes } = panel();
  context.renderRoster(roster([ADULT]));
  const before = nodes.get("rider").children;
  nodes.get("rider").value = "k17adult";
  context.renderRoster(roster([ADULT], { age_s: 200 }));
  assert.equal(nodes.get("rider").children, before, "the options were not rebuilt");
  assert.equal(nodes.get("rider").value, "k17adult");
  assert.equal(nodes.get("roster-note").textContent, "liste du tableau de bord recue il y a 3 min");
});

test("no list offered: the reason is said, and nobody can be chosen", () => {
  const { context, nodes } = panel();
  context.renderRoster(
    roster([], {
      state: "unlinked",
      age_s: null,
      detail: "aucun tableau de bord configure sur cette console : la liste des passagers vient du site",
    }),
  );
  assert.deepEqual(options(nodes.get("rider")), [["", "- aucun passager propose -"]]);
  const note = nodes.get("roster-note");
  assert.equal(note.className, "note note-bad");
  assert.match(note.textContent, /^aucun tableau de bord configure/);
});

test("a list kept without a fresh answer says how old it is and why", () => {
  const { context, nodes } = panel();
  context.renderRoster(
    roster([ADULT], { age_s: 30, problem: "tableau de bord injoignable : ConnectTimeout" }),
  );
  const note = nodes.get("roster-note");
  assert.equal(
    note.textContent,
    "liste du tableau de bord recue il y a 30 s ; derniere demande sans reponse : " +
      "tableau de bord injoignable : ConnectTimeout",
  );
  assert.equal(note.className, "note note-bad");
  assert.equal(context.rosterAge(null), "?");
});

test("a start and a preview name the rider chosen, or nobody, and nothing else of them", async () => {
  const { context, nodes, storage } = panel();
  context.state.profiles = [{ profile_id: "standard_30_min", zone_low_bpm: 118, zone_high_bpm: 138 }];
  nodes.get("profile").value = "standard_30_min";
  nodes.get("operator").value = "Declared actor";
  context.renderRoster(roster([ADULT]));
  const sent = [];
  context.api = (path, options) => {
    sent.push({ path, body: options.body });
    return new Promise(() => undefined);
  };
  context.doStart();
  context.doPreview();
  nodes.get("rider").value = "k17adult";
  context.doStart();
  context.doPreview();
  assert.deepEqual(
    sent.map(({ path, body }) => [path, body.subject_id]),
    [
      ["/api/session/start", null],
      ["/api/plan/preview", null],
      ["/api/session/start", "k17adult"],
      ["/api/plan/preview", "k17adult"],
    ],
  );
  for (const { body } of sent) {
    assert.equal("subject_age" in body, false);
    assert.equal("subject_hr_max" in body, false);
  }
  // The name stays in this page's memory: the browser's storage never holds it.
  assert.equal([...storage.values()].some((value) => value.includes("Synthetic Adult")), false);
});

test("a list that cannot be read is said where the list is", async () => {
  const { context, nodes } = panel();
  context.api = () => Promise.reject(new Error("401 Unauthorized"));
  await context.loadRoster();
  assert.equal(nodes.get("roster-note").textContent, "401 Unauthorized");
  context.api = () => Promise.resolve(roster([ADULT]));
  await context.loadRoster();
  assert.equal(nodes.get("roster-note").textContent, "liste du tableau de bord recue il y a 12 s");
});

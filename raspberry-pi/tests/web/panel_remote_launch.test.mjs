import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const assets = new URL("../../src/web/static/", import.meta.url);
const html = readFileSync(new URL("index.html", assets), "utf8");
const source = readFileSync(new URL("app.js", assets), "utf8");

class Element {
  constructor(classes = "", text = "") {
    this.classes = new Set(String(classes).split(/\s+/).filter(Boolean));
    this.children = [];
    this.text = text;
    this.disabled = false;
    this.classList = {
      toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name),
      contains: (name) => this.classes.has(name),
    };
  }
  get className() { return [...this.classes].join(" "); }
  set className(value) { this.classes = new Set(String(value).split(/\s+/).filter(Boolean)); }
  get textContent() { return this.text; }
  set textContent(value) {
    this.text = String(value);
    this.children = [];
  }
  set innerHTML(value) { throw new Error(`markup is unsafe: ${value}`); }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener() {}
  setAttribute() {}
}

function page() {
  const nodes = new Map(
    [...html.matchAll(/<[a-z0-9]+\b[^>]*\bid="([^"]+)"[^>]*>([^<]*)/g)].map((tag) => [
      tag[1],
      new Element(/\bclass="([^"]*)"/.exec(tag[0])?.[1], tag[2].trim()),
    ]),
  );
  const context = vm.createContext({
    document: {
      documentElement: { style: { setProperty: () => undefined } },
      getElementById: (id) => nodes.get(id) ?? null,
      createElement: () => new Element(),
    },
    window: { addEventListener: () => undefined },
  });
  vm.runInContext(source, context, { filename: fileURLToPath(new URL("app.js", assets)) });
  const node = (id) => {
    assert.ok(nodes.has(id), `missing #${id}`);
    return nodes.get(id);
  };
  return { context, node };
}

const offer = (changes = {}) => ({
  session_id: "remote-session",
  profile_id: "standard_30_min",
  program: "30 min",
  launched_by: "Dr Manager",
  passenger_name: "Roster Name",
  passenger_reason: null,
  state: "waiting_confirmation",
  reason: null,
  can_confirm: true,
  ...changes,
});

test("the Seance page has one remote confirmation button with the required wording", () => {
  const card = /<div class="card remote-launch hidden" id="remote-launch-card">([\s\S]*?)<\/div>/.exec(html)?.[1] ?? "";
  assert.match(card, /Cette personne est a bord et prete/);
  assert.equal((card.match(/<button\b/g) ?? []).length, 1);
});

test("an offered launch shows its roster passenger, programme, launcher, and confirmation", () => {
  const { context, node } = page();
  context.renderRemoteLaunch(offer());
  const facts = node("remote-launch-facts").children.map((child) => child.textContent);
  assert.deepEqual(facts, [
    "passager prevu",
    "Roster Name",
    "programme",
    "30 min",
    "lance par",
    "Dr Manager",
  ]);
  assert.equal(node("remote-launch-state").textContent, "en attente de confirmation");
  assert.equal(node("remote-launch-confirm").disabled, false);
  assert.equal(node("remote-launch-card").classList.contains("hidden"), false);
});

test("requesting, refused, and unconfirmed states show their reason", () => {
  const { context, node } = page();
  context.renderRemoteLaunch(offer({ state: "request_in_progress", can_confirm: false }));
  assert.equal(node("remote-launch-state").textContent, "demande en cours");
  assert.equal(node("remote-launch-confirm").disabled, true);

  context.renderRemoteLaunch(offer({
    state: "refused",
    reason: "depart distant refuse (HTTP 409)",
    can_confirm: false,
  }));
  assert.equal(node("remote-launch-status").textContent, "refuse : depart distant refuse (HTTP 409)");
  assert.equal(node("remote-launch-confirm").disabled, true);

  context.renderRemoteLaunch(offer({
    state: "unconfirmed",
    reason: "confirmation indisponible",
    can_confirm: true,
  }));
  assert.equal(
    node("remote-launch-status").textContent,
    "non confirme faute de reponse : confirmation indisponible",
  );
  assert.equal(node("remote-launch-confirm").disabled, false);
});

test("waiting confirmation explains an offer or passenger reason and clears recovered absence", () => {
  const { context, node } = page();
  context.renderRemoteLaunch(offer({
    reason: "l'offre a change, une nouvelle confirmation est requise",
  }));
  assert.equal(
    node("remote-launch-status").textContent,
    "en attente de confirmation : l'offre a change, une nouvelle confirmation est requise",
  );

  context.renderRemoteLaunch(offer({
    passenger_name: null,
    passenger_reason: "le passager n'est pas dans la liste",
    can_confirm: false,
  }));
  assert.equal(
    node("remote-launch-status").textContent,
    "en attente de confirmation : le passager n'est pas dans la liste",
  );

  context.renderRemoteLaunch(offer());
  assert.equal(node("remote-launch-status").textContent, "en attente de confirmation");
  assert.equal(node("remote-launch-passenger-note").textContent, "");
});

test("the initial remote launch status uses a plain hyphen", () => {
  const { node } = page();
  assert.equal(node("remote-launch-state").textContent, "-");
});

test("an absent roster passenger is explained and cannot be confirmed", () => {
  const { context, node } = page();
  context.renderRemoteLaunch(offer({
    passenger_name: null,
    passenger_reason: "liste des passagers perimee",
    can_confirm: false,
  }));
  assert.equal(node("remote-launch-passenger-note").textContent, "liste des passagers perimee");
  assert.equal(node("remote-launch-facts").children[1].textContent, "indisponible");
  assert.equal(node("remote-launch-confirm").disabled, true);
});

test("poll removal hides the remote launch and restores the local start button", () => {
  const { context, node } = page();
  context.renderRemoteLaunch(offer());
  context.renderRemoteLaunch(null);
  assert.equal(node("remote-launch-card").classList.contains("hidden"), true);
  assert.equal(node("start").classList.contains("hidden"), false);
});

test("passenger data is rendered as text and never parsed as markup", () => {
  const { context, node } = page();
  context.renderRemoteLaunch(offer({ passenger_name: "<img src=x>" }));
  assert.equal(node("remote-launch-facts").children[1].textContent, "<img src=x>");
});

/* ===== ANH-250: a ballasted bench session launched from the site ===== */

const ATTESTATION = "capsule lestee, aucune personne a bord, personne dans le rayon du bras";

const bench = (changes = {}) => offer({
  mode: "bench",
  profile_id: null,
  program: null,
  passenger_name: null,
  passenger_reason: null,
  ballast_kg: 80,
  notes: "sac de sable sangle",
  attestation: ATTESTATION,
  ...changes,
});

test("a bench launch shows the ballast, the note and the attestation, nobody on board", () => {
  const { context, node } = page();
  context.renderRemoteLaunch(bench());
  const facts = node("remote-launch-facts").children.map((child) => child.textContent);
  assert.deepEqual(facts, [
    "seance",
    "BANC LESTE - personne a bord : NON",
    "lest declare",
    "80 kg",
    "note",
    "sac de sable sangle",
    "lance par",
    "Dr Manager",
  ]);
  assert.equal(node("remote-launch-attest-row").classList.contains("hidden"), false);
  assert.equal(node("remote-launch-attest-text").textContent, `J'atteste : ${ATTESTATION}`);
  assert.equal(node("remote-launch-confirm").textContent, "Confirmer le depart du banc leste");
});

test("a bench launch cannot be confirmed before the attestation is ticked", () => {
  const { context, node } = page();
  context.renderRemoteLaunch(bench());
  assert.equal(node("remote-launch-confirm").disabled, true);
  node("remote-launch-attest").checked = true;
  context.renderRemoteLaunch(bench());
  assert.equal(node("remote-launch-confirm").disabled, false);
});

test("the tick belongs to the offer it was given for: another launch, or a changed one, clears it", () => {
  const { context, node } = page();
  context.renderRemoteLaunch(bench());
  node("remote-launch-attest").checked = true;
  context.renderRemoteLaunch(bench({ ballast_kg: 90 }));
  assert.equal(node("remote-launch-attest").checked, false);
  assert.equal(node("remote-launch-confirm").disabled, true);
  node("remote-launch-attest").checked = true;
  context.renderRemoteLaunch(bench({ session_id: "another" }));
  assert.equal(node("remote-launch-attest").checked, false);
  node("remote-launch-attest").checked = true;
  context.renderRemoteLaunch(null);
  assert.equal(node("remote-launch-attest").checked, false);
  assert.equal(node("remote-launch-attest-row").classList.contains("hidden"), true);
});

test("a mass not declared and an empty note are said as such", () => {
  const { context, node } = page();
  context.renderRemoteLaunch(bench({ ballast_kg: null, notes: null }));
  const facts = node("remote-launch-facts").children.map((child) => child.textContent);
  assert.equal(facts[3], "masse non declaree");
  assert.equal(facts[5], "-");
});

test("the note of a bench launch is rendered as text and never parsed as markup", () => {
  const { context, node } = page();
  context.renderRemoteLaunch(bench({ notes: "<script>x</script>" }));
  assert.equal(node("remote-launch-facts").children[5].textContent, "<script>x</script>");
});

test("a programme keeps its own wording and needs no attestation", () => {
  const { context, node } = page();
  context.renderRemoteLaunch(bench());
  context.renderRemoteLaunch(offer());
  assert.equal(node("remote-launch-attest-row").classList.contains("hidden"), true);
  assert.equal(node("remote-launch-confirm").textContent, "Cette personne est a bord et prete");
  assert.equal(node("remote-launch-confirm").disabled, false);
});

test("the confirmation of a bench launch says the attestation was ticked; a programme's never does", async () => {
  const { context, node } = page();
  const sent = [];
  context.api = (path, options) => {
    sent.push([path, options]);
    return Promise.resolve(null);
  };
  node("operator").value = "Operateur";
  context.renderRemoteLaunch(bench());
  node("remote-launch-attest").checked = true;
  context.doConfirmRemoteLaunch();
  await new Promise((resolve) => setImmediate(resolve));
  // Built in the page's own realm: compared by value, as JSON.
  assert.deepEqual(JSON.parse(JSON.stringify(sent[0])), [
    "/api/remote-launch/confirm",
    {
      method: "POST",
      body: { session_id: "remote-session", operator: "Operateur", bench_attested: true },
    },
  ]);
  context.renderRemoteLaunch(offer({ session_id: "programme" }));
  node("remote-launch-attest").checked = true;
  context.doConfirmRemoteLaunch();
  await new Promise((resolve) => setImmediate(resolve));
  const confirmations = sent.filter(([path]) => path === "/api/remote-launch/confirm");
  assert.equal(confirmations.length, 2);
  assert.equal(confirmations[1][1].body.bench_attested, false);
});

// ANH-142: a launch is held while the pre-flight of its kind of start has a red blocking
// control, the bench's for a bench launch, a person on board's for a programme. The console
// judges the confirmation again whatever the page shows.
test("a remote launch is held while the pre-flight of its kind has a red blocking control", () => {
  const { context, node } = page();
  const preflight = (benchBlock, onBoardBlock) => ({
    running: false,
    bench: { first_block: benchBlock },
    occupied: { first_block: onBoardBlock },
  });
  context.renderRemoteLaunch(bench());
  node("remote-launch-attest").checked = true;
  const disk = "espace disque insuffisant pour l'enregistrement de seance : 100 Mo libres";
  context.state.preflight = preflight(disk, disk);
  context.renderRemoteLaunch(bench());
  assert.equal(node("remote-launch-confirm").disabled, true);
  assert.equal(node("remote-launch-preflight-note").textContent, `Confirmation retenue par le pre-vol : ${disk}`);
  // Red with a person on board only: the bench is not held for it.
  context.state.preflight = preflight(null, "batterie BITalino 20 % < 30 %");
  context.renderRemoteLaunch(bench());
  assert.equal(node("remote-launch-confirm").disabled, false);
  assert.equal(node("remote-launch-preflight-note").textContent, "");
  // A programme is held by that same red control.
  context.renderRemoteLaunch(offer());
  assert.equal(node("remote-launch-confirm").disabled, true);
  assert.equal(node("remote-launch-preflight-note").textContent, "Confirmation retenue par le pre-vol : batterie BITalino 20 % < 30 %");
  context.renderRemoteLaunch(null);
  assert.equal(node("remote-launch-preflight-note").textContent, "");
});

// ANH-146 P10: the rider's feedback card of the console page, after a session with someone on board.
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
    this.value = "";
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

const scales = {
  nausea: {
    title: "Nausée",
    labels: ["Aucune", "Très légère", "Légère", "Légère à modérée", "Modérée", "Moyenne",
      "Assez forte", "Forte", "Très forte", "Extrême", "Maximale imaginable"],
  },
  dizziness: {
    title: "Malaise ou vertige",
    labels: ["Aucun", "Très léger", "Léger", "Léger à modéré", "Modéré", "Moyen",
      "Assez fort", "Fort", "Très fort", "Extrême", "Maximal imaginable"],
  },
};

const answer = (current, givenUp = 0) => ({
  current,
  given_up: givenUp,
  scales,
  comfort: [
    { value: "good", label: "Bon" },
    { value: "acceptable", label: "Acceptable" },
    { value: "bad", label: "Mauvais" },
  ],
  free_text_max: 500,
});

const toFill = (changes = {}) => ({
  ended_at_ms: 1_700_000_600_000,
  status: "to_fill",
  status_text: "",
  code: null,
  can_submit: true,
  ...changes,
});

/** The page in a sandbox: its own markup, no storage at all, and the console answered by `reply`. */
function page(reply) {
  const nodes = new Map(
    [...html.matchAll(/<[a-z0-9]+\b[^>]*\bid="([^"]+)"[^>]*>([^<]*)/g)].map((tag) => [
      tag[1],
      new Element(/\bclass="([^"]*)"/.exec(tag[0])?.[1], tag[2].trim()),
    ]),
  );
  const logged = [];
  const context = vm.createContext({
    document: {
      documentElement: { style: { setProperty: () => undefined } },
      getElementById: (id) => nodes.get(id) ?? null,
      createElement: () => new Element(),
    },
    // No storage: the card must not need any, and must never write any.
    window: { addEventListener: () => undefined },
    console: { log: (...args) => logged.push(args), error: (...args) => logged.push(args) },
  });
  vm.runInContext(source, context, { filename: fileURLToPath(new URL("app.js", assets)) });
  const calls = [];
  context.api = (path, options) => {
    // What goes on the wire: the body as JSON, as api() sends it.
    const body = options?.body === undefined ? undefined : JSON.parse(JSON.stringify(options.body));
    calls.push({ path, method: options?.method ?? "GET", body });
    return reply(path, options);
  };
  // The buttons and fields, wired as start() wires them.
  const node = (id) => {
    assert.ok(nodes.has(id), `missing #${id}`);
    return nodes.get(id);
  };
  node("feedback-send").onclick = context.doSendFeedback;
  node("feedback-skip").onclick = context.doSkipFeedback;
  node("feedback-close").onclick = context.doSkipFeedback;
  for (const id of ["feedback-nausea", "feedback-dizziness", "feedback-comfort"]) {
    node(id).onchange = context.feedbackChanged;
  }
  node("feedback-text").oninput = context.feedbackChanged;
  return { context, node, calls, logged };
}

const hidden = (node) => node.classList.contains("hidden");
const texts = (select) => select.children.map((option) => option.textContent);

test("the card opens for a session with someone on board that ended, with the scales labelled", async () => {
  const { context, node } = page(() => Promise.resolve(answer(toFill())));
  await context.loadFeedback();
  assert.equal(hidden(node("feedback-card")), false);
  assert.equal(hidden(node("feedback-form")), false);
  assert.equal(hidden(node("feedback-close")), true);
  assert.equal(node("feedback-nausea-title").textContent, "Nausée");
  assert.equal(node("feedback-dizziness-title").textContent, "Malaise ou vertige");
  const nausea = texts(node("feedback-nausea"));
  assert.equal(nausea.length, 12);
  assert.equal(nausea[0], "choisir");
  assert.equal(nausea[4], "3 - Légère à modérée");
  assert.equal(nausea[11], "10 - Maximale imaginable");
  assert.equal(texts(node("feedback-dizziness"))[11], "10 - Maximal imaginable");
  assert.deepEqual(texts(node("feedback-comfort")), ["choisir", "Bon", "Acceptable", "Mauvais"]);
  assert.equal(node("feedback-text").maxLength, 500);
  // Nothing is prefilled, so nothing can be sent yet.
  assert.equal(node("feedback-nausea").value, "");
  assert.equal(node("feedback-send").disabled, true);
  assert.equal(node("feedback-count").textContent, "0 / 500");
});

test("the card stays closed for a bench session, for which the console offers nothing", async () => {
  const { context, node } = page(() => Promise.resolve(answer(null)));
  await context.loadFeedback();
  assert.equal(hidden(node("feedback-card")), true);
});

test("the counter follows the text, and Envoyer waits for both scores and the comfort", async () => {
  const { context, node } = page(() => Promise.resolve(answer(toFill())));
  await context.loadFeedback();
  node("feedback-text").value = "abcé";
  node("feedback-text").oninput();
  assert.equal(node("feedback-count").textContent, "4 / 500");
  node("feedback-nausea").value = "0";
  node("feedback-nausea").onchange();
  node("feedback-dizziness").value = "10";
  node("feedback-dizziness").onchange();
  assert.equal(node("feedback-send").disabled, true);
  node("feedback-comfort").value = "good";
  node("feedback-comfort").onchange();
  assert.equal(node("feedback-send").disabled, false);
});

test("Envoyer posts whole scores, the comfort and the trimmed comment", async () => {
  const { context, node, calls, logged } = page((path) =>
    Promise.resolve(path === "/api/feedback" ? answer(toFill()) : answer(null)),
  );
  await context.loadFeedback();
  node("feedback-nausea").value = "3";
  node("feedback-dizziness").value = "10";
  node("feedback-comfort").value = "bad";
  node("feedback-text").value = "  Nausée à la fin  ";
  node("feedback-text").oninput();
  await node("feedback-send").onclick();
  assert.deepEqual(calls.at(-1), {
    path: "/api/feedback",
    method: "POST",
    body: { nausea_score: 3, dizziness_score: 10, comfort: "bad", free_text: "Nausée à la fin" },
  });
  assert.equal(typeof calls.at(-1).body.nausea_score, "number");
  assert.deepEqual(logged, [], "nothing typed is logged");
});

test("a blank comment is left out of what is sent", async () => {
  const { context, node, calls } = page(() => Promise.resolve(answer(toFill())));
  await context.loadFeedback();
  node("feedback-nausea").value = "0";
  node("feedback-dizziness").value = "0";
  node("feedback-comfort").value = "acceptable";
  node("feedback-text").value = "   ";
  await node("feedback-send").onclick();
  assert.deepEqual(calls.at(-1).body, { nausea_score: 0, dizziness_score: 0, comfort: "acceptable" });
});

test("Ignorer posts the skip and the card closes, its fields emptied", async () => {
  const { context, node, calls } = page((path) =>
    Promise.resolve(path === "/api/feedback/skip" ? answer(null) : answer(toFill())),
  );
  await context.loadFeedback();
  node("feedback-nausea").value = "7";
  node("feedback-text").value = "rien";
  await node("feedback-skip").onclick();
  assert.deepEqual(calls.at(-1), { path: "/api/feedback/skip", method: "POST", body: {} });
  assert.equal(hidden(node("feedback-card")), true);
  assert.equal(node("feedback-nausea").value, "");
  assert.equal(node("feedback-text").value, "");
});

test("a refused send shows the console's words and keeps what was typed", async () => {
  const { context, node } = page((path, options) =>
    options?.method === "POST"
      ? Promise.reject(new Error("note de nausée hors de l'échelle"))
      : Promise.resolve(answer(toFill())),
  );
  await context.loadFeedback();
  node("feedback-nausea").value = "3";
  node("feedback-dizziness").value = "4";
  node("feedback-comfort").value = "good";
  node("feedback-text").value = "garde";
  await node("feedback-send").onclick();
  assert.equal(node("feedback-status").textContent, "note de nausée hors de l'échelle");
  assert.equal(node("feedback-text").value, "garde");
  assert.equal(node("feedback-nausea").value, "3");
  assert.equal(node("feedback-send").disabled, false);
  // The next reading of the console keeps the sentence and what was typed.
  await context.loadFeedback();
  assert.equal(node("feedback-status").textContent, "note de nausée hors de l'échelle");
  assert.equal(node("feedback-text").value, "garde");
});

test("a feedback sent shows its status and Fermer, without the form", async () => {
  const sent = toFill({ status: "sent", status_text: "envoyé au tableau de bord", can_submit: false });
  const { context, node, calls } = page((path) =>
    Promise.resolve(path === "/api/feedback/skip" ? answer(null) : answer(sent, 2)),
  );
  await context.loadFeedback();
  assert.equal(hidden(node("feedback-form")), true);
  assert.equal(hidden(node("feedback-close")), false);
  assert.equal(node("feedback-status").textContent, "envoyé au tableau de bord");
  assert.equal(
    node("feedback-given-up").textContent,
    "2 retour(s) abandonné(s) : trop de retours en attente",
  );
  await node("feedback-close").onclick();
  assert.equal(calls.at(-1).path, "/api/feedback/skip");
  assert.equal(hidden(node("feedback-card")), true);
});

test("once entered, the card offers to change it and Fermer, which keeps it to send", async () => {
  const waiting = toFill({ status: "waiting_link", status_text: "En attente du tableau de bord" });
  let current = toFill();
  const { context, node, calls } = page((path, options) => {
    if (path === "/api/feedback/skip") {
      return Promise.resolve(answer(null));
    }
    if (options?.method === "POST") {
      current = waiting;
    }
    return Promise.resolve(answer(current));
  });
  await context.loadFeedback();
  assert.equal(hidden(node("feedback-skip")), false, "Ignorer before anything is entered");
  assert.equal(hidden(node("feedback-close")), true);
  node("feedback-nausea").value = "3";
  node("feedback-dizziness").value = "4";
  node("feedback-comfort").value = "good";
  await node("feedback-send").onclick();
  // Entered: no Ignorer that would drop it, the form to change it, and Fermer.
  assert.equal(hidden(node("feedback-skip")), true);
  assert.equal(hidden(node("feedback-close")), false);
  assert.equal(hidden(node("feedback-form")), false);
  assert.equal(node("feedback-nausea").value, "3", "what was entered stays to be changed");
  assert.equal(node("feedback-status").textContent, "En attente du tableau de bord");
  await node("feedback-close").onclick();
  assert.deepEqual(calls.at(-1), { path: "/api/feedback/skip", method: "POST", body: {} });
  assert.equal(hidden(node("feedback-card")), true);
});

test("another session's card starts empty", async () => {
  let current = toFill();
  const { context, node } = page(() => Promise.resolve(answer(current)));
  await context.loadFeedback();
  node("feedback-nausea").value = "5";
  await context.loadFeedback();
  assert.equal(node("feedback-nausea").value, "5", "the same session keeps what is typed");
  current = toFill({ ended_at_ms: 1_700_000_900_000 });
  await context.loadFeedback();
  assert.equal(node("feedback-nausea").value, "");
});

test("the end of a session said by the console reads the feedback again", async () => {
  const { context, calls } = page(() => Promise.resolve(answer(null)));
  context.followRequests({ kind: "session_idle", request_id: null });
  await Promise.resolve();
  assert.equal(calls.filter((call) => call.path === "/api/feedback").length, 1);
});

test("the card is wired at start and read at boot and on the panel's cadence", () => {
  assert.match(source, /el\("feedback-send"\)\.onclick = doSendFeedback;/);
  assert.match(source, /el\("feedback-skip"\)\.onclick = doSkipFeedback;/);
  assert.match(source, /window\.setInterval\(loadFeedback, PANEL_MS\);/);
  assert.match(/function boot\(\) \{[\s\S]*?\n\}/.exec(source)?.[0] ?? "", /loadFeedback\(\);/);
});

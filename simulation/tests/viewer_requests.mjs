// What the viewer page asks its server for, and what it then puts in its
// document, for a given address of the page.
//
//     node viewer_requests.mjs <index.html> [option]... <page address>...
//
//     --served <file>     the server answers every request but the scenario
//                         list with this file (a trace, or a record as
//                         /api/record gives it)
//     --listed <file>     the server answers the scenario list with this file,
//                         a JSON array of names; without it the list is empty
//     --chosen <file>     the person picks this file with the file button
//     --streamed <file>   the live stream plays this file: each line to the
//                         listeners of its `type`, then `end`
//     --click <n>         then the person clicks line n of the event list
//
// The page's own script is run as it is shipped, in a context that holds the
// few browser objects it touches. Nothing is drawn and nothing leaves this
// process: `fetch` and `EventSource` only write down the address they are
// given, resolved against the address of the page as a browser resolves it.
//
// The document is a stand-in that keeps what the script builds: elements,
// their class, and text. It reads no markup: whatever the script hands to be
// read as markup (innerHTML and the like) is kept apart, as it was given.
//
// Prints one JSON line: for each page address, the requests made, what the
// page then shows as its source, every element the script reached by its id
// (`shown`) and what it handed over as markup (`markup`). Read by
// test_viewer_page.py.

import { readFileSync } from "node:fs";
import { basename } from "node:path";
import vm from "node:vm";

const options = { served: null, listed: null, chosen: null, streamed: null, click: null };
const [pagePath, ...rest] = process.argv.slice(2);
while (rest[0]?.startsWith("--")) {
  const name = rest.shift().slice(2);
  if (!Object.hasOwn(options, name)) throw new Error(`unknown option --${name}`);
  options[name] = rest.shift();
}
const addresses = rest;
const html = readFileSync(pagePath, "utf8");
const fileOf = (path) => (path === null ? null : readFileSync(path, "utf8"));
const served = fileOf(options.served);
const listed = fileOf(options.listed) ?? "[]";
const chosen = fileOf(options.chosen);
const streamed = fileOf(options.streamed);

// The page holds one inline script, between a bare opening tag and its
// closing tag. Anything else (a second script, an attribute, no script) is
// said: this harness would otherwise run something other than the page does.
const OPENING = "<script>";
const CLOSING = "</script>";
const lower = html.toLowerCase();
const opening = lower.indexOf(OPENING);
const closing = lower.indexOf(CLOSING, opening);
const tags = lower.split("<script").length - 1;
if (opening === -1 || closing === -1 || tags !== 1 || lower.split(CLOSING).length - 1 !== 1) {
  throw new Error(`${pagePath}: expected exactly one inline <script>...</script>`);
}
const script = html.slice(opening + OPENING.length, closing);

/** The text of a node of the stand-in: its own, or that of everything under it. */
const textOf = (node) => ("text" in node ? node.text : node.childNodes.map(textOf).join(""));

/** A node as it is printed: a text as a string, an element with what it holds. */
const shownAs = (node) =>
  "text" in node
    ? node.text
    : { tag: node.tag, classes: node.className, children: node.childNodes.map(shownAs) };

/**
 * An element as the script uses it: built by the script, or found by its id.
 * @param {string} tag
 * @param {string[]} markup where what the script hands over as markup goes
 */
function element(tag, markup) {
  const node = {
    tag,
    value: "",
    className: "",
    disabled: false,
    title: "",
    width: 0,
    height: 0,
    childNodes: [],
    classList: { contains: () => false },
    appendChild: (child) => node.childNodes.push(child),
    querySelector: () => null,
    getBoundingClientRect: () => ({ width: 0, height: 0 }),
    getContext: () => new Proxy({}, { get: () => () => {} }),
    insertAdjacentHTML: (_position, text) => markup.push(String(text)),
    get textContent() {
      return textOf(node);
    },
    // As in a browser: the text replaces whatever the element held, and no
    // text (the empty one, or none given) leaves it empty.
    set textContent(text) {
      node.childNodes = (text ?? "") === "" ? [] : [{ text: String(text) }];
    },
    // Markup is not read here: the element loses what it held, as in a
    // browser, and what a browser would have read into it is kept apart.
    set innerHTML(text) {
      node.childNodes = [];
      markup.push(String(text));
    },
    set outerHTML(text) {
      markup.push(String(text));
    },
  };
  return node;
}

/** @param {string} address the page, as the browser shows it */
async function requestsOf(address) {
  const page = new URL(address);
  const elements = new Map();
  const requests = [];
  const markup = [];
  const streams = [];
  const note = (kind, url) =>
    requests.push({ kind, asked: String(url), resolved: new URL(String(url), page).href });
  const found = (id) => elements.get(id) ?? elements.set(id, element(`#${id}`, markup)).get(id);
  const context = vm.createContext({
    URLSearchParams,
    location: { search: page.search, href: page.href, origin: page.origin },
    document: {
      documentElement: {},
      getElementById: found,
      createElement: (tag) => element(tag, markup),
      createTextNode: (text) => ({ text: String(text) }),
      write: (text) => markup.push(String(text)),
    },
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    performance: { now: () => 0 },
    requestAnimationFrame: () => 0,
    window: { devicePixelRatio: 1 },
    fetch: async (url) => {
      note("fetch", url);
      // The scenario list is the listed one, or empty; anything else is the
      // served file, or not found.
      if (String(url) === "/api/scenarios") {
        const list = { json: async () => JSON.parse(listed), text: async () => listed };
        return { ok: true, status: 200, ...list };
      }
      return served === null
        ? { ok: false, status: 404, json: async () => ({}), text: async () => "" }
        : { ok: true, status: 200, json: async () => ({}), text: async () => served };
    },
    EventSource: class {
      listeners = new Map();
      constructor(url) {
        note("stream", url);
        streams.push(this);
      }
      addEventListener(type, listener) {
        this.listeners.set(type, listener);
      }
      close() {}
    },
  });
  vm.runInContext(script, context, { filename: pagePath });
  // The page starts by itself (an async function called at load): let it finish.
  for (let turn = 0; turn < 50; turn++) await new Promise((resolve) => setImmediate(resolve));
  if (chosen !== null) {
    const file = { name: basename(options.chosen), text: async () => chosen };
    await found("file").onchange({ target: { files: [file] } });
  }
  if (streamed !== null) {
    for (const stream of streams) {
      for (const line of streamed.split("\n").filter((text) => text.trim())) {
        stream.listeners.get(JSON.parse(line).type)?.({ data: line });
      }
      stream.listeners.get("end")?.({});
    }
  }
  if (options.click !== null) found("events").childNodes[Number(options.click)].onclick();
  const shown = Object.fromEntries([...elements].map(([id, node]) => [id, shownAs(node)]));
  const source = elements.get("source")?.textContent ?? "";
  return { address, requests, source, shown, markup };
}

const results = [];
for (const address of addresses) results.push(await requestsOf(address));
process.stdout.write(JSON.stringify(results) + "\n");

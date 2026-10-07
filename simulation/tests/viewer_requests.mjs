// What the viewer page asks its server for, for a given address of the page.
//
//     node viewer_requests.mjs <index.html> <page address>...
//
// The page's own script is run as it is shipped, in a context that holds the
// few browser objects it touches. Nothing is drawn and nothing leaves this
// process: `fetch` and `EventSource` only write down the address they are
// given, resolved against the address of the page as a browser resolves it.
// Prints one JSON line: for each page address, the requests made and what the
// page then shows as its source. Read by test_viewer_page.py.

import { readFileSync } from "node:fs";
import vm from "node:vm";

const [pagePath, ...addresses] = process.argv.slice(2);
const html = readFileSync(pagePath, "utf8");
const script = /<script>([\s\S]*?)<\/script>/.exec(html)?.[1];
if (!script) throw new Error(`${pagePath}: no inline script`);

/** An element as the script uses it before any trace is loaded. */
function element() {
  const node = {
    value: "",
    textContent: "",
    innerHTML: "",
    className: "",
    disabled: false,
    title: "",
    width: 0,
    height: 0,
    children: [],
    classList: { contains: () => false },
    appendChild: (child) => node.children.push(child),
    querySelector: () => null,
    getBoundingClientRect: () => ({ width: 0, height: 0 }),
    getContext: () => new Proxy({}, { get: () => () => {} }),
  };
  return node;
}

/** @param {string} address the page, as the browser shows it */
async function requestsOf(address) {
  const page = new URL(address);
  const elements = new Map();
  const requests = [];
  const note = (kind, url) =>
    requests.push({ kind, asked: String(url), resolved: new URL(String(url), page).href });
  const context = vm.createContext({
    URLSearchParams,
    location: { search: page.search, href: page.href, origin: page.origin },
    document: {
      documentElement: {},
      getElementById: (id) => elements.get(id) ?? elements.set(id, element()).get(id),
      createElement: () => element(),
    },
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    performance: { now: () => 0 },
    requestAnimationFrame: () => 0,
    window: { devicePixelRatio: 1 },
    fetch: async (url) => {
      note("fetch", url);
      // The scenario list is empty; anything else is not found.
      return String(url) === "/api/scenarios"
        ? { ok: true, status: 200, json: async () => [], text: async () => "[]" }
        : { ok: false, status: 404, json: async () => ({}), text: async () => "" };
    },
    EventSource: class {
      constructor(url) {
        note("stream", url);
      }
      addEventListener() {}
      close() {}
    },
  });
  vm.runInContext(script, context, { filename: pagePath });
  // The page starts by itself (an async function called at load): let it finish.
  for (let turn = 0; turn < 50; turn++) await new Promise((resolve) => setImmediate(resolve));
  return { address, requests, source: elements.get("source")?.textContent ?? "" };
}

const results = [];
for (const address of addresses) results.push(await requestsOf(address));
process.stdout.write(JSON.stringify(results) + "\n");

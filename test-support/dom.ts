/**
 * A document for the tests of the site, without a browser and without a DOM
 * library: the repository has neither. React runs for real on it (state,
 * effects, renders after an event), so a test can open a form, type in it,
 * submit it, and read what the screen then shows.
 *
 * It holds what React itself asks of a document, and nothing of what a
 * browser adds: no layout, no focus rules, no CSS, no parsing of HTML. A
 * component that needs more (a Radix popover, a canvas, a media query) gets it
 * from the test that mounts it, as a stand-in the test names.
 *
 * Importing this file installs the document as `document` and `window`, for
 * the test file that imports it and for no other: Vitest gives each test file
 * its own globals. It must be imported before React, so `test-support/render`
 * imports it first.
 */

const ELEMENT_NODE = 1;
const TEXT_NODE = 3;
const COMMENT_NODE = 8;
const DOCUMENT_NODE = 9;

const HTML_NAMESPACE = "http://www.w3.org/1999/xhtml";

type Listener = (event: TestEvent) => void;
type Registered = { type: string; listener: Listener; capture: boolean };

/** What a test says of an event when it fires one: any field the handler reads. */
export type EventInit = Record<string, unknown>;

/** An event as React reads it: the fields of the browser's, set by the test. */
export class TestEvent {
  readonly type: string;
  readonly bubbles: boolean;
  readonly cancelable = true;
  readonly isTrusted = true;
  readonly timeStamp = Date.now();
  target: TestNode | null = null;
  currentTarget: TestNode | null = null;
  eventPhase = 0;
  defaultPrevented = false;
  /** Set once a listener stopped the event: no later node hears it. */
  stopped = false;
  [field: string]: unknown;

  constructor(type: string, init: EventInit = {}) {
    this.type = type;
    this.bubbles = init.bubbles !== false;
    Object.assign(this, init);
  }

  preventDefault() {
    this.defaultPrevented = true;
  }

  stopPropagation() {
    this.stopped = true;
  }

  stopImmediatePropagation() {
    this.stopped = true;
  }
}

export class TestNode {
  readonly nodeType: number;
  readonly nodeName: string;
  readonly ownerDocument: TestDocument | null;
  readonly childNodes: TestNode[] = [];
  parentNode: TestNode | null = null;
  private readonly registered: Registered[] = [];

  constructor(
    nodeType: number,
    nodeName: string,
    ownerDocument: TestDocument | null,
  ) {
    this.nodeType = nodeType;
    this.nodeName = nodeName;
    this.ownerDocument = ownerDocument;
  }

  get firstChild(): TestNode | null {
    return this.childNodes[0] ?? null;
  }

  get lastChild(): TestNode | null {
    return this.childNodes[this.childNodes.length - 1] ?? null;
  }

  get nextSibling(): TestNode | null {
    const siblings = this.parentNode?.childNodes ?? [];
    return siblings[siblings.indexOf(this) + 1] ?? null;
  }

  get previousSibling(): TestNode | null {
    const siblings = this.parentNode?.childNodes ?? [];
    return siblings[siblings.indexOf(this) - 1] ?? null;
  }

  get parentElement(): TestElement | null {
    return this.parentNode instanceof TestElement ? this.parentNode : null;
  }

  get isConnected(): boolean {
    return this.getRootNode() === this.ownerDocument;
  }

  getRootNode(): TestNode {
    return this.parentNode === null ? this : this.parentNode.getRootNode();
  }

  appendChild<T extends TestNode>(child: T): T {
    return this.insertBefore(child, null);
  }

  insertBefore<T extends TestNode>(child: T, before: TestNode | null): T {
    child.parentNode?.removeChild(child);
    const at = before === null ? -1 : this.childNodes.indexOf(before);
    if (before !== null && at === -1) {
      throw new Error("insertBefore: the reference node is not a child");
    }
    if (at === -1) this.childNodes.push(child);
    else this.childNodes.splice(at, 0, child);
    child.parentNode = this;
    return child;
  }

  removeChild<T extends TestNode>(child: T): T {
    const at = this.childNodes.indexOf(child);
    if (at === -1) throw new Error("removeChild: the node is not a child");
    this.childNodes.splice(at, 1);
    child.parentNode = null;
    return child;
  }

  remove() {
    this.parentNode?.removeChild(this);
  }

  contains(other: TestNode | null): boolean {
    for (let node = other; node !== null; node = node.parentNode) {
      if (node === this) return true;
    }
    return false;
  }

  /** The text of the node as a browser gives it: every text below it, joined as written. */
  get textContent(): string {
    return this.childNodes.map((child) => child.textContent).join("");
  }

  set textContent(text: string) {
    for (const child of [...this.childNodes]) this.removeChild(child);
    if (text !== "" && this.ownerDocument !== null) {
      this.appendChild(this.ownerDocument.createTextNode(text));
    }
  }

  addEventListener(
    type: string,
    listener: Listener,
    options?: boolean | { capture?: boolean },
  ) {
    const capture =
      typeof options === "boolean" ? options : options?.capture === true;
    const known = this.registered.some(
      (entry) =>
        entry.type === type &&
        entry.listener === listener &&
        entry.capture === capture,
    );
    if (!known) this.registered.push({ type, listener, capture });
  }

  removeEventListener(
    type: string,
    listener: Listener,
    options?: boolean | { capture?: boolean },
  ) {
    const capture =
      typeof options === "boolean" ? options : options?.capture === true;
    const at = this.registered.findIndex(
      (entry) =>
        entry.type === type &&
        entry.listener === listener &&
        entry.capture === capture,
    );
    if (at !== -1) this.registered.splice(at, 1);
  }

  /** How many listeners of an event this node holds: what a test reads to prove a clean-up. */
  listenerCount(type: string): number {
    return this.registered.filter((entry) => entry.type === type).length;
  }

  /**
   * Sends an event down to this node and back up, as a browser does: the
   * listeners that capture from the outermost node, then the others from this
   * node outwards when the event bubbles.
   * @returns false if a listener prevented the default action
   */
  dispatchEvent(event: TestEvent): boolean {
    // From this node to the outermost one.
    const path: TestNode[] = [this];
    for (let node = this.parentNode; node !== null; node = node.parentNode) {
      path.push(node);
    }
    event.target = this;
    const hear = (node: TestNode, capture: boolean) => {
      event.currentTarget = node;
      for (const entry of [...node.registered]) {
        if (event.stopped) return;
        if (entry.type === event.type && entry.capture === capture) {
          entry.listener(event);
        }
      }
    };
    event.eventPhase = 1;
    for (const node of [...path].reverse()) hear(node, true);
    event.eventPhase = 3;
    for (const node of event.bubbles ? path : path.slice(0, 1))
      hear(node, false);
    event.currentTarget = null;
    return !event.defaultPrevented;
  }
}

export class TestText extends TestNode {
  nodeValue: string;

  constructor(text: string, ownerDocument: TestDocument) {
    super(TEXT_NODE, "#text", ownerDocument);
    this.nodeValue = text;
  }

  get data(): string {
    return this.nodeValue;
  }

  set data(text: string) {
    this.nodeValue = text;
  }

  get textContent(): string {
    return this.nodeValue;
  }

  set textContent(text: string) {
    this.nodeValue = text;
  }
}

export class TestComment extends TestNode {
  nodeValue: string;

  constructor(text: string, ownerDocument: TestDocument) {
    super(COMMENT_NODE, "#comment", ownerDocument);
    this.nodeValue = text;
  }

  get textContent(): string {
    return "";
  }

  set textContent(text: string) {
    this.nodeValue = text;
  }
}

/** The inline style of an element: what React writes, kept by name. */
export class TestStyle {
  [name: string]: unknown;

  setProperty(name: string, value: string) {
    this[name] = value;
  }

  removeProperty(name: string) {
    delete this[name];
  }

  getPropertyValue(name: string): string {
    const value = this[name];
    return typeof value === "string" ? value : "";
  }
}

export class TestElement extends TestNode {
  readonly tagName: string;
  readonly localName: string;
  readonly namespaceURI: string;
  readonly attributes = new Map<string, string>();
  readonly style = new TestStyle();
  /** What a form field holds. React writes them; a test writes `value` before it fires `change`. */
  value = "";
  defaultValue = "";
  checked = false;
  defaultChecked = false;
  /** Where the page is scrolled, for the components that read it. */
  scrollTop = 0;
  scrollLeft = 0;
  /** React asks for it to be `null` before it sets its own. */
  onclick: unknown = null;

  constructor(
    localName: string,
    ownerDocument: TestDocument,
    namespaceURI: string = HTML_NAMESPACE,
  ) {
    const html = namespaceURI === HTML_NAMESPACE;
    super(
      ELEMENT_NODE,
      html ? localName.toUpperCase() : localName,
      ownerDocument,
    );
    this.localName = localName;
    this.tagName = this.nodeName;
    this.namespaceURI = namespaceURI;
  }

  get children(): TestElement[] {
    return this.childNodes.filter(
      (child): child is TestElement => child instanceof TestElement,
    );
  }

  get id(): string {
    return this.getAttribute("id") ?? "";
  }

  get className(): string {
    return this.getAttribute("class") ?? "";
  }

  /** The name of a form field. React writes it as a property; a test finds the field by it. */
  get name(): string {
    return this.getAttribute("name") ?? "";
  }

  set name(name: string) {
    this.setAttribute("name", name);
  }

  /**
   * The type of a field or of a button, with the browser's defaults: React
   * reads it to decide that an `<input>` without a type is a text field,
   * whose typing it must report.
   */
  get type(): string {
    const written = this.getAttribute("type");
    if (written !== null) return written;
    if (this.localName === "input") return "text";
    return this.localName === "button" ? "submit" : "";
  }

  set type(type: string) {
    this.setAttribute("type", type);
  }

  getAttribute(name: string): string | null {
    return this.attributes.get(name) ?? null;
  }

  hasAttribute(name: string): boolean {
    return this.attributes.has(name);
  }

  /** The names of the attributes written: what Vitest reads to print an element in a failed assertion. */
  getAttributeNames(): string[] {
    return [...this.attributes.keys()];
  }

  setAttribute(name: string, value: unknown) {
    this.attributes.set(name, String(value));
  }

  removeAttribute(name: string) {
    this.attributes.delete(name);
  }

  setAttributeNS(_namespace: string | null, name: string, value: unknown) {
    this.setAttribute(name, value);
  }

  removeAttributeNS(_namespace: string | null, name: string) {
    this.removeAttribute(name);
  }

  focus() {
    if (this.ownerDocument !== null) this.ownerDocument.activeElement = this;
  }

  blur() {
    if (this.ownerDocument?.activeElement === this) {
      this.ownerDocument.activeElement = null;
    }
  }

  /** No layout here: every element is a point at the origin unless a test says otherwise. */
  getBoundingClientRect() {
    return {
      x: 0,
      y: 0,
      top: 0,
      left: 0,
      right: 0,
      bottom: 0,
      width: 0,
      height: 0,
    };
  }
}

export class TestDocument extends TestNode {
  readonly documentElement: TestElement;
  readonly head: TestElement;
  readonly body: TestElement;
  activeElement: TestElement | null = null;
  /** React asks whether the browser knows the `input` event by reading this field. */
  readonly oninput = null;
  defaultView: unknown = null;
  cookie = "";

  constructor() {
    super(DOCUMENT_NODE, "#document", null);
    this.documentElement = this.appendChild(new TestElement("html", this));
    this.head = this.documentElement.appendChild(new TestElement("head", this));
    this.body = this.documentElement.appendChild(new TestElement("body", this));
  }

  createElement(localName: string): TestElement {
    return new TestElement(localName, this);
  }

  createElementNS(namespaceURI: string, localName: string): TestElement {
    return new TestElement(localName, this, namespaceURI);
  }

  createTextNode(text: string): TestText {
    return new TestText(text, this);
  }

  createComment(text: string): TestComment {
    return new TestComment(text, this);
  }

  get textContent(): string {
    return "";
  }

  set textContent(_text: string) {
    // A document has no text of its own.
  }
}

/** The document of the test file that imported this module. */
export const testDocument = new TestDocument();

function install() {
  const globals = globalThis as Record<string, unknown>;
  testDocument.defaultView = globalThis;
  globals.window = globalThis;
  globals.document = testDocument;
  globals.Node = TestNode;
  globals.Element = TestElement;
  globals.HTMLElement = TestElement;
  globals.HTMLIFrameElement = class HTMLIFrameElement {};
  // What tells React that the updates of a test are wrapped in `act`.
  globals.IS_REACT_ACT_ENVIRONMENT = true;
}

install();

// --- Reading what was rendered -------------------------------------------------

/** Every element under a node, the node included, in the order of the document. */
export function elementsOf(root: TestNode): TestElement[] {
  const found: TestElement[] = [];
  const walk = (node: TestNode) => {
    if (node instanceof TestElement) found.push(node);
    node.childNodes.forEach(walk);
  };
  walk(root);
  return found;
}

/**
 * The text a reader sees under a node. The pieces of text of one element are
 * joined as written ("110", "-", "140" read "110-140"); an element is set
 * apart from what surrounds it by a space ("Lancer" and "Annuler" in two
 * buttons read "Lancer Annuler", never "LancerAnnuler"). Spaces are collapsed.
 */
export function textOf(root: TestNode): string {
  const read = (node: TestNode): string =>
    node instanceof TestText
      ? node.nodeValue
      : ` ${node.childNodes.map(read).join("")} `;
  return read(root).replaceAll(/\s+/g, " ").trim();
}

/** The markup of a node, for the message of a failed search and for reading class names. */
export function markupOf(node: TestNode): string {
  if (node instanceof TestText) return node.nodeValue;
  if (!(node instanceof TestElement))
    return node.childNodes.map(markupOf).join("");
  const attributes = [...node.attributes]
    .map(([name, value]) => ` ${name}="${value}"`)
    .join("");
  const inner = node.childNodes.map(markupOf).join("");
  return `<${node.localName}${attributes}>${inner}</${node.localName}>`;
}

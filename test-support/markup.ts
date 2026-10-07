/**
 * Reading what a component draws as a tree, in a test, without a browser and
 * without a DOM library: React writes the static markup, this file reads it
 * back one tag at a time.
 *
 *     const page = draw(<Card className="py-2">Solde</Card>);
 *     expect(page.slot("card").localName).toBe("div");
 *     expect(page.slot("card").classes).toContain("py-2");
 *
 * For a component with no state and no effect: what it draws from its props.
 * One that answers an event is mounted with `test-support/render` instead.
 *
 * It reads what React writes and nothing more: tags closed in the order they
 * were opened, attribute values between double quotes, the five characters
 * React escapes.
 */

import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";

/** The characters React escapes in a text and in an attribute. `&` last: it starts the others. */
const ESCAPED = [
  ["&quot;", '"'],
  ["&#x27;", "'"],
  ["&lt;", "<"],
  ["&gt;", ">"],
  ["&amp;", "&"],
] as const;

function asWritten(text: string): string {
  let plain = text;
  for (const [escaped, character] of ESCAPED) {
    plain = plain.replaceAll(escaped, character);
  }
  return plain;
}

/** An element of the markup: what a test reads of it. */
export class MarkupElement {
  /** The elements directly inside, in the order they were drawn. */
  readonly children: MarkupElement[] = [];
  /** The elements and the pieces of text directly inside, in order. */
  private readonly content: (MarkupElement | string)[] = [];

  constructor(
    readonly localName: string,
    private readonly attributes: ReadonlyMap<string, string>,
    readonly parent: MarkupElement | null,
  ) {}

  /** Used while the markup is read. */
  append(piece: MarkupElement | string) {
    this.content.push(piece);
    if (piece instanceof MarkupElement) this.children.push(piece);
  }

  getAttribute(name: string): string | null {
    return this.attributes.get(name) ?? null;
  }

  hasAttribute(name: string): boolean {
    return this.attributes.has(name);
  }

  /**
   * The class names, one by one: `toContain("h-9")` matches the class `h-9`,
   * never a part of `min-h-9` or of `data-[size=default]:h-9`.
   */
  get classes(): string[] {
    return (this.getAttribute("class") ?? "").split(" ").filter(Boolean);
  }

  /**
   * The text a reader sees under this element, by the rule of `textOf` in
   * `test-support/dom`: the text of one element is read as written, an element
   * is set apart from what surrounds it by a space, spaces are collapsed.
   */
  get text(): string {
    const read = (piece: MarkupElement | string): string =>
      typeof piece === "string"
        ? piece
        : ` ${piece.content.map(read).join("")} `;
    return read(this).replaceAll(/\s+/g, " ").trim();
  }

  /** Every element under this one, in the order of the document. */
  get descendants(): MarkupElement[] {
    return this.children.flatMap((child) => [child, ...child.descendants]);
  }

  /** Whether this element is somewhere under `ancestor`. */
  isInside(ancestor: MarkupElement): boolean {
    for (let above = this.parent; above !== null; above = above.parent) {
      if (above === ancestor) return true;
    }
    return false;
  }

  /** Every element under this one that passes the test given. */
  all(test: (element: MarkupElement) => boolean): MarkupElement[] {
    return this.descendants.filter(test);
  }

  /** The one element under this one that passes the test. Throws if there is none, or more than one. */
  only(
    test: (element: MarkupElement) => boolean,
    what = "the test",
  ): MarkupElement {
    const found = this.all(test);
    if (found.length === 1) return found[0];
    throw new Error(
      `${found.length} elements for ${what}, expected one. The markup: ${this.markup}`,
    );
  }

  /** The elements of one tag: `tag("button")`. */
  tag(name: string): MarkupElement[] {
    return this.all((element) => element.localName === name);
  }

  /** The elements that carry one class: an icon (`lucide-x`), a text for screen readers (`sr-only`). */
  withClass(name: string): MarkupElement[] {
    return this.all((element) => element.classes.includes(name));
  }

  /** The elements a component of `components/ui` marked `data-slot="name"`. */
  slots(name: string): MarkupElement[] {
    return this.all((element) => element.getAttribute("data-slot") === name);
  }

  /** The one element marked `data-slot="name"`. */
  slot(name: string): MarkupElement {
    return this.only(
      (element) => element.getAttribute("data-slot") === name,
      `data-slot="${name}"`,
    );
  }

  /** The markup under this element, written again: for the message of a failed search. */
  get markup(): string {
    return this.content
      .map((piece) => {
        if (typeof piece === "string") return piece;
        const attributes = [...piece.attributes]
          .map(([name, value]) => ` ${name}="${value}"`)
          .join("");
        return `<${piece.localName}${attributes}>${piece.markup}</${piece.localName}>`;
      })
      .join("");
  }
}

/** The attributes of an opening tag, once its name is removed: ` id="a" class="b c"`. */
function readAttributes(source: string): Map<string, string> {
  const attributes = new Map<string, string>();
  let at = 0;
  for (;;) {
    const equals = source.indexOf('="', at);
    if (equals === -1) return attributes;
    const end = source.indexOf('"', equals + 2);
    attributes.set(
      source.slice(at, equals).trim(),
      asWritten(source.slice(equals + 2, end)),
    );
    at = end + 1;
  }
}

/**
 * Reads a static markup written by React.
 * @returns an element that stands for the whole markup: what was drawn is under it
 */
export function readMarkup(html: string): MarkupElement {
  const root = new MarkupElement("#markup", new Map(), null);
  let open = root;
  let at = 0;
  while (at < html.length) {
    const tagAt = html.indexOf("<", at);
    const textEnd = tagAt === -1 ? html.length : tagAt;
    if (textEnd > at) open.append(asWritten(html.slice(at, textEnd)));
    if (tagAt === -1) break;
    // React escapes `>` in an attribute: the first one closes the tag.
    const tagEnd = html.indexOf(">", tagAt);
    const tag = html.slice(tagAt + 1, tagEnd);
    at = tagEnd + 1;
    if (tag.startsWith("/")) {
      open = open.parent ?? root;
      continue;
    }
    // An element with no content (`<input/>`) is closed where it opens.
    const empty = tag.endsWith("/");
    const source = empty ? tag.slice(0, -1) : tag;
    const nameEnd = source.indexOf(" ");
    const element = new MarkupElement(
      nameEnd === -1 ? source : source.slice(0, nameEnd),
      nameEnd === -1 ? new Map() : readAttributes(source.slice(nameEnd)),
      open,
    );
    open.append(element);
    if (!empty) open = element;
  }
  return root;
}

/** Draws a component once, as the server does, and reads what it drew. */
export function draw(ui: ReactNode): MarkupElement {
  return readMarkup(renderToStaticMarkup(ui));
}

import {
  fire,
  fireWindow,
  render,
  settle,
  testDocument,
  type TestElement,
} from "@/test-support/render";
import {
  frames,
  installLayout,
  watchWindowListeners,
} from "@/test-support/browser";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MagicCard } from "./magic-card";

/**
 * The card whose border and background light up under the pointer: where
 * the light is drawn as the pointer moves over the card, and how it is put
 * out when the pointer leaves the card, the window, or the tab.
 *
 * Motion runs for real, on the frame clock of the tests and fake timers: the
 * light is read where the page draws it, in the `background` of the two
 * layers of the card. The card is given a box at (20, 30) on the page.
 */

const LEFT = 20;
const TOP = 30;

type Props = ComponentProps<typeof MagicCard>;

function mount(props: Props = {}) {
  const screen = render(<MagicCard {...props}>Sessions</MagicCard>);
  const card = screen.container.children[0];
  return { screen, card };
}

/** A gradient as written, on one line. */
function gradient(layer: TestElement): string {
  return String(layer.style.background).replaceAll(/\s+/g, " ").trim();
}

/** The layer that lights the border, and the one that lights the inside. */
const border = (card: TestElement) => card.children[0];
const glow = (card: TestElement) => card.children[2];

/** Where the light is centred, read in the border layer: "100px 50px". */
function lightAt(card: TestElement): string {
  return (
    /circle at (-?[\d.]+px -?[\d.]+px)/.exec(gradient(border(card)))?.[1] ?? ""
  );
}

/** Lets Motion write what changed: it does so on the next frame. */
function nextFrame() {
  return settle(() => frames.advance(32));
}

async function moveOver(card: TestElement, clientX: number, clientY: number) {
  await fire(card, "pointermove", { clientX, clientY });
  await nextFrame();
}

const OUT = "-200px -200px";

beforeEach(() => {
  vi.useFakeTimers();
  installLayout({ width: 300, height: 200, left: LEFT, top: TOP });
});
afterEach(() => {
  Reflect.deleteProperty(testDocument, "visibilityState");
  vi.useRealTimers();
});

describe("ANH-203 magic card: what is drawn", () => {
  it("draws its content over a light kept out of the card until the pointer comes", async () => {
    const { card } = mount({ className: "p-6" });
    await nextFrame();

    expect(card.textContent).toBe("Sessions");
    expect(card.className.split(" ")).toEqual(
      expect.arrayContaining(["p-6", "group"]),
    );
    expect(gradient(border(card))).toBe(
      "radial-gradient(200px circle at -200px -200px, #9E7AFF, #FE8BBB, var(--border) 100% )",
    );
    expect(gradient(glow(card))).toBe(
      "radial-gradient(200px circle at -200px -200px, #262626, transparent 100%)",
    );
    expect(Number(glow(card).style.opacity)).toBe(0.8);
  });

  it("takes the size, the colors and the strength of the light it is given", async () => {
    const { card } = mount({
      gradientSize: 320,
      gradientFrom: "#0ea5e9",
      gradientTo: "#22c55e",
      gradientColor: "#111827",
      gradientOpacity: 0.4,
    });
    await nextFrame();

    // A larger light is kept further out of the card.
    expect(gradient(border(card))).toBe(
      "radial-gradient(320px circle at -320px -320px, #0ea5e9, #22c55e, var(--border) 100% )",
    );
    expect(gradient(glow(card))).toBe(
      "radial-gradient(320px circle at -320px -320px, #111827, transparent 100%)",
    );
    expect(Number(glow(card).style.opacity)).toBe(0.4);
  });

  it("moves the light out again at its new size when the size changes", async () => {
    const { screen, card } = mount();
    await moveOver(card, 120, 80);

    screen.rerender(<MagicCard gradientSize={90}>Sessions</MagicCard>);
    await nextFrame();

    expect(lightAt(card)).toBe("-90px -90px");
    expect(gradient(glow(card))).toContain("90px circle at -90px -90px");
  });
});

describe("ANH-203 magic card: under the pointer", () => {
  it("centres the light on the pointer, measured from the corner of the card", async () => {
    const { card } = mount();

    await moveOver(card, LEFT + 100, TOP + 50);

    expect(lightAt(card)).toBe("100px 50px");
    expect(gradient(glow(card))).toContain("circle at 100px 50px");

    await moveOver(card, LEFT + 10, TOP + 180);
    expect(lightAt(card)).toBe("10px 180px");
  });

  it("puts the light out when the pointer leaves the card", async () => {
    const { card } = mount();
    await moveOver(card, 120, 80);

    await fire(card, "pointerout", { relatedTarget: null });
    await nextFrame();

    expect(lightAt(card)).toBe(OUT);
  });

  it("starts from a light put out when the pointer comes back in", async () => {
    const { card } = mount();
    await moveOver(card, 120, 80);

    await fire(card, "pointerover", { relatedTarget: null });
    await nextFrame();

    expect(lightAt(card)).toBe(OUT);
  });
});

describe("ANH-203 magic card: when the pointer is lost", () => {
  it("puts the light out when the pointer leaves the window", async () => {
    const { card } = mount();
    await moveOver(card, 120, 80);

    await fireWindow("pointerout", { relatedTarget: null });
    await nextFrame();

    expect(lightAt(card)).toBe(OUT);
  });

  it("keeps the light when the pointer only goes from one element of the page to another", async () => {
    const { card } = mount();
    await moveOver(card, 120, 80);

    await fireWindow("pointerout", { relatedTarget: testDocument.body });
    await nextFrame();

    expect(lightAt(card)).toBe("100px 50px");
  });

  it("puts the light out when the window loses the focus", async () => {
    const { card } = mount();
    await moveOver(card, 120, 80);

    await fireWindow("blur");
    await nextFrame();

    expect(lightAt(card)).toBe(OUT);
  });

  it("puts the light out when the tab is hidden, not when it is shown again", async () => {
    const { card } = mount();
    await moveOver(card, 120, 80);

    Object.assign(testDocument, { visibilityState: "visible" });
    await fire(testDocument, "visibilitychange");
    await nextFrame();
    expect(lightAt(card)).toBe("100px 50px");

    Object.assign(testDocument, { visibilityState: "hidden" });
    await fire(testDocument, "visibilitychange");
    await nextFrame();
    expect(lightAt(card)).toBe(OUT);
  });
});

describe("ANH-203 magic card: leaving the page", () => {
  it("takes back its listeners of the window and of the tab", () => {
    const listeners = watchWindowListeners();
    const { screen } = mount();
    expect(listeners.left().sort()).toEqual(["blur", "pointerout"]);
    expect(testDocument.listenerCount("visibilitychange")).toBe(1);

    screen.unmount();

    expect(listeners.left()).toEqual([]);
    expect(testDocument.listenerCount("visibilitychange")).toBe(0);
  });
});

import { render, settle, type TestElement } from "@/test-support/render";
import { frames, installIntersectionObserver } from "@/test-support/browser";
import type { ComponentProps } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BlurFade } from "./blur-fade";

/**
 * The blocks of the landing page that come in blurred and sharpen: how each
 * is drawn before it appears (from which side, how far, how blurred), when it
 * appears (after which delay, in how long, at once or when it enters the
 * page), and how it ends.
 *
 * Motion runs for real, on the frame clock of the tests and fake timers. The
 * first drawing is read from the markup of the server, where nothing moves
 * yet. Nothing is in view until the test says the block entered the page.
 */

type Props = Partial<ComponentProps<typeof BlurFade>>;

/** The inline style of the block as the server draws it: before anything moves. */
function firstDrawing(props: Props = {}): Record<string, string> {
  const html = renderToStaticMarkup(<BlurFade {...props}>Sessions</BlurFade>);
  const style = /style="([^"]*)"/.exec(html)?.[1] ?? "";
  return Object.fromEntries(
    style
      .split(";")
      .filter((declaration) => declaration !== "")
      .map((declaration) => {
        const colon = declaration.indexOf(":");
        return [declaration.slice(0, colon), declaration.slice(colon + 1)];
      }),
  );
}

let view: ReturnType<typeof installIntersectionObserver>;

function mount(props: Props = {}) {
  const screen = render(<BlurFade {...props}>Sessions</BlurFade>);
  return screen.container.children[0];
}

/** How far the block is into its appearance: 0 hidden, 1 fully shown. */
function opacity(block: TestElement): number {
  return Number(block.style.opacity);
}

function seconds(time: number) {
  return settle(() => frames.advance(time * 1000));
}

beforeEach(() => {
  vi.useFakeTimers();
  view = installIntersectionObserver();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("ANH-203 blur fade: before it appears", () => {
  it("is transparent, blurred, and a little above where it will be", () => {
    expect(firstDrawing()).toEqual({
      opacity: "0",
      filter: "blur(6px)",
      transform: "translateY(-6px)",
    });
  });

  it.each([
    ["down", "translateY(-6px)"],
    ["up", "translateY(6px)"],
    ["right", "translateX(-6px)"],
    ["left", "translateX(6px)"],
  ] as const)(
    "starts on the side it comes from when it moves %s",
    (direction, transform) => {
      expect(firstDrawing({ direction }).transform).toBe(transform);
    },
  );

  it("starts as far and as blurred as told", () => {
    const drawing = firstDrawing({ direction: "up", offset: 24, blur: "12px" });

    expect(drawing.transform).toBe("translateY(24px)");
    expect(drawing.filter).toBe("blur(12px)");
  });

  it("starts as the variant given says, in place of its own blur and fade", () => {
    const drawing = firstDrawing({
      variant: { hidden: { y: 40 }, visible: { y: 0 } },
    });

    expect(drawing).toEqual({ transform: "translateY(40px)" });
  });

  it("draws its content in an element that keeps the class and the style it is given", () => {
    const html = renderToStaticMarkup(
      <BlurFade className="mt-8" style={{ color: "red" }}>
        <p>Sessions</p>
      </BlurFade>,
    );

    expect(html).toContain("<p>Sessions</p>");
    expect(html).toMatch(/^<div class="mt-8"/);
    expect(html).toContain("color:red");
  });
});

describe("ANH-203 blur fade: appearing", () => {
  it("ends opaque, sharp and in place", async () => {
    const block = mount();
    expect(opacity(block)).toBe(0);

    await seconds(0.6);

    expect(opacity(block)).toBe(1);
    expect(block.style.filter).toBe("blur(0px)");
    expect(block.style.transform).toBe("none");
  });

  it("appears at once, whether in view or not, unless told to wait for it", async () => {
    const block = mount();

    await seconds(0.6);

    // Nothing ever entered the page.
    expect(opacity(block)).toBe(1);
  });

  it("waits for the delay given, in seconds, on top of its own 40 ms", async () => {
    const block = mount({ delay: 1 });

    await seconds(1.02);
    expect(opacity(block)).toBe(0);

    await seconds(0.2);
    expect(opacity(block)).toBeGreaterThan(0);
    expect(opacity(block)).toBeLessThan(1);

    await seconds(0.4);
    expect(opacity(block)).toBe(1);
  });

  it("takes 0.4 s unless given another duration", async () => {
    const usual = mount();
    const slow = mount({ duration: 3 });

    await seconds(0.6);

    expect(opacity(usual)).toBe(1);
    expect(opacity(slow)).toBeGreaterThan(0);
    expect(opacity(slow)).toBeLessThan(1);

    await seconds(2.6);
    expect(opacity(slow)).toBe(1);
  });

  it("moves to its place from the offset of the variant given", async () => {
    const block = mount({ variant: { hidden: { y: 40 }, visible: { y: 0 } } });
    expect(block.style.transform).toBe("translateY(40px)");

    await seconds(0.6);

    expect(block.style.transform).toBe("none");
  });
});

describe("ANH-203 blur fade: waiting to be seen", () => {
  it("stays hidden until it enters the page, then appears", async () => {
    const block = mount({ inView: true });

    await seconds(2);
    expect(opacity(block)).toBe(0);
    expect(block.style.filter).toBe("blur(6px)");

    await settle(() => view.enter());
    await seconds(0.6);

    expect(opacity(block)).toBe(1);
    expect(block.style.filter).toBe("blur(0px)");
  });

  it("appears once and for all: leaving the page does not hide it again", async () => {
    const block = mount({ inView: true });
    await settle(() => view.enter());
    await seconds(0.6);

    // Nothing is watched any more once it was seen.
    expect(view.watching().length).toBe(0);
    await settle(() => view.leave());
    await seconds(0.6);

    expect(opacity(block)).toBe(1);
  });

  it("counts as seen 50 px inside the page unless given another margin", () => {
    const usual = mount({ inView: true });
    const deeper = mount({ inView: true, inViewMargin: "-120px" });

    const [first, second] = view.created;
    expect(first.targets.has(usual)).toBe(true);
    expect(first.options).toMatchObject({ rootMargin: "-50px" });
    expect(second.targets.has(deeper)).toBe(true);
    expect(second.options).toMatchObject({ rootMargin: "-120px" });
  });
});

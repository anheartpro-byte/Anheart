import { render, settle } from "@/test-support/render";
import { frames, installIntersectionObserver } from "@/test-support/browser";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NumberTicker } from "./number-ticker";

/**
 * The figures of the landing page that count up when they come into view:
 * the number shown before, when the count starts, where it ends, in which
 * direction it goes and how the number is written.
 *
 * Motion runs for real: its spring is driven by the frame clock of the tests
 * and by fake timers. The one thing it lacks is a viewport, so nothing is in
 * view until the test says the element entered the page.
 */

let view: ReturnType<typeof installIntersectionObserver>;

/** Lets the count run, and gives every number shown on the way. */
async function count(
  screen: ReturnType<typeof render>,
  ms: number,
): Promise<number[]> {
  const shown: number[] = [];
  for (let elapsed = 0; elapsed < ms; elapsed += 16) {
    await settle(() => frames.advance(16));
    shown.push(Number(screen.text().replaceAll(",", "")));
  }
  return shown;
}

beforeEach(() => {
  vi.useFakeTimers();
  view = installIntersectionObserver();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("ANH-203 number ticker: before it is seen", () => {
  it("shows zero, or the start value it is given", () => {
    const zero = render(<NumberTicker value={1200} />);
    expect(zero.text()).toBe("0");
    zero.unmount();

    const ten = render(<NumberTicker value={1200} startValue={10} />);
    expect(ten.text()).toBe("10");
  });

  it("does not count while it is out of view", async () => {
    const screen = render(<NumberTicker value={1200} />);

    expect(new Set(await count(screen, 2000))).toEqual(new Set([0]));
  });

  it("watches for the moment any part of it enters the page", () => {
    const screen = render(<NumberTicker value={1200} />);

    expect(view.created.length).toBe(1);
    expect(view.created[0].targets.has(screen.tag("span")[0])).toBe(true);
    expect(view.created[0].options).toMatchObject({
      rootMargin: "0px",
      threshold: 0,
    });
  });

  it("is a span that keeps the classes and the attributes it is given", () => {
    const screen = render(
      <NumberTicker
        value={5}
        className="text-4xl"
        id="riders"
        aria-label="Riders"
      />,
    );

    const figure = screen.field("riders");
    expect(figure.localName).toBe("span");
    expect(figure.getAttribute("aria-label")).toBe("Riders");
    expect(figure.className.split(" ")).toEqual(
      expect.arrayContaining(["text-4xl", "tabular-nums"]),
    );
  });
});

describe("ANH-203 number ticker: counting up", () => {
  it("counts from the start value up to the value once in view, and stops there", async () => {
    const screen = render(<NumberTicker value={1200} startValue={10} />);

    await settle(() => view.enter());
    const shown = await count(screen, 6000);

    // Never below where it started, never back down, and all the way.
    expect(shown[0]).toBeGreaterThanOrEqual(10);
    expect(shown.some((number) => number > 10 && number < 1200)).toBe(true);
    expect([...shown].sort((a, b) => a - b)).toEqual(shown);
    expect(screen.text()).toBe("1,200");
  });

  it("waits for the delay it is given, in seconds, before it starts", async () => {
    const screen = render(<NumberTicker value={1200} delay={1.5} />);
    await settle(() => view.enter());

    await count(screen, 1480);
    expect(screen.text()).toBe("0");

    await count(screen, 200);
    expect(Number(screen.text())).toBeGreaterThan(0);
  });

  it("counts on to a new value when the value changes", async () => {
    const screen = render(<NumberTicker value={100} />);
    await settle(() => view.enter());
    await count(screen, 6000);
    expect(screen.text()).toBe("100");

    screen.rerender(<NumberTicker value={250} />);
    const shown = await count(screen, 6000);

    expect(shown.every((number) => number >= 100 && number <= 250)).toBe(true);
    expect(screen.text()).toBe("250");
  });
});

describe("ANH-203 number ticker: counting down", () => {
  it("counts from the value down to the start value", async () => {
    const screen = render(
      <NumberTicker value={100} startValue={20} direction="down" />,
    );

    await settle(() => view.enter());
    const shown = await count(screen, 6000);

    // Until the count starts, the span still shows the start value: 20, where
    // the count will end. Then it jumps to the top and comes down.
    const counting = shown.slice(shown.findIndex((number) => number !== 20));
    expect(counting[0]).toBeGreaterThan(90);
    expect(counting[0]).toBeLessThanOrEqual(100);
    expect([...counting].sort((a, b) => b - a)).toEqual(counting);
    expect(screen.text()).toBe("20");
  });
});

describe("ANH-203 number ticker: how the number is written", () => {
  it("groups thousands the English way and rounds to a whole number", async () => {
    const screen = render(<NumberTicker value={12345.6} />);

    await settle(() => view.enter());
    await count(screen, 9000);

    expect(screen.text()).toBe("12,346");
  });

  it("keeps the decimal places asked, zeros included", async () => {
    const screen = render(<NumberTicker value={9.5} decimalPlaces={2} />);

    await settle(() => view.enter());
    const shown = await count(screen, 6000);

    expect(screen.text()).toBe("9.50");
    // On the way too: hundredths were shown, not whole numbers only.
    expect(shown.some((number) => !Number.isInteger(number))).toBe(true);
  });
});

describe("ANH-203 number ticker: leaving the page", () => {
  it("drops the count it was about to start", async () => {
    const screen = render(<NumberTicker value={1200} delay={2} />);
    await settle(() => view.enter());
    expect(vi.getTimerCount()).toBe(1);

    screen.unmount();

    expect(vi.getTimerCount()).toBe(0);
  });
});

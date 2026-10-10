import { render, settle } from "@/test-support/render";
import { frames } from "@/test-support/browser";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BorderBeam } from "./border-beam";

/**
 * The beam of light that runs around the border of a card: its size and its
 * colors, where on the border it starts, which way it goes and how fast.
 *
 * Motion runs for real, on the frame clock of the tests and fake timers: the
 * place of the beam is read on the element, as a share of the way around the
 * border (`offset-distance`), after the time the test lets pass.
 */

type Props = ComponentProps<typeof BorderBeam>;

function mount(props: Props = {}) {
  const frame = render(<BorderBeam {...props} />).container.children[0];
  return { frame, beam: frame.children[0] };
}

/** Where the beam is around the border after `time` seconds: 0 to 100. */
async function placeAfter(props: Props, time: number): Promise<number> {
  const { beam } = mount(props);
  await settle(() => frames.advance(time * 1000));
  const place = String(beam.style.offsetDistance);
  expect(place.endsWith("%")).toBe(true);
  return Number.parseFloat(place);
}

/** Frames come every 16 ms: a place is right within one percent of the lap. */
function near(place: number, expected: number) {
  expect(Math.abs(place - expected)).toBeLessThan(1);
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("ANH-203 border beam: what is drawn", () => {
  it("draws a beam of 50 px, from orange to violet, on a border of 1 px, unless told otherwise", () => {
    const { frame, beam } = mount();

    expect(frame.style["--border-beam-width"]).toBe("1px");
    expect(beam.style.width).toBe("50px");
    // The corners of its path are rounded by its own size.
    expect(beam.style.offsetPath).toBe("rect(0 auto auto 0 round 50px)");
    expect(beam.style["--color-from"]).toBe("#ffaa40");
    expect(beam.style["--color-to"]).toBe("#9c40ff");
  });

  it("takes the size, the colors and the border width it is given", () => {
    const { frame, beam } = mount({
      size: 120,
      colorFrom: "#0ea5e9",
      colorTo: "#22c55e",
      borderWidth: 3,
    });

    expect(frame.style["--border-beam-width"]).toBe("3px");
    expect(beam.style.width).toBe("120px");
    expect(beam.style.offsetPath).toBe("rect(0 auto auto 0 round 120px)");
    expect(beam.style["--color-from"]).toBe("#0ea5e9");
    expect(beam.style["--color-to"]).toBe("#22c55e");
  });

  it("keeps the classes it is given, and lets the style given replace its own", () => {
    const { beam } = mount({
      className: "blur-sm",
      style: { width: 80, opacity: 0.5 },
    });

    expect(beam.className.split(" ")).toEqual(
      expect.arrayContaining(["blur-sm", "aspect-square"]),
    );
    expect(beam.style.width).toBe("80px");
    expect(Number(beam.style.opacity)).toBe(0.5);
  });

  it("starts at the top left corner, or as far around the border as told", () => {
    expect(mount().beam.style.offsetDistance).toBe("0%");
    expect(mount({ initialOffset: 20 }).beam.style.offsetDistance).toBe("20%");
  });
});

describe("ANH-203 border beam: how it runs", () => {
  it("goes around the border in six seconds, at a steady pace", async () => {
    near(await placeAfter({}, 1.5), 25);
    near(await placeAfter({}, 3), 50);
    near(await placeAfter({}, 4.5), 75);
  });

  it("starts another lap as soon as one ends", async () => {
    // A lap and a quarter.
    near(await placeAfter({}, 7.5), 25);
  });

  it("goes around in the time it is given", async () => {
    near(await placeAfter({ duration: 2 }, 1), 50);
    near(await placeAfter({ duration: 12 }, 3), 25);
  });

  it("runs the other way when reversed", async () => {
    near(await placeAfter({ reverse: true }, 1.5), 75);
    near(await placeAfter({ reverse: true }, 4.5), 25);
  });

  it("runs a full lap from where it starts", async () => {
    // From 20 % to 120 %: half-way is 70 %.
    near(await placeAfter({ initialOffset: 20 }, 3), 70);
    // Reversed, from 80 % down to -20 %: half-way is 30 %.
    near(await placeAfter({ initialOffset: 20, reverse: true }, 3), 30);
  });

  it("starts as if the delay given had already passed", async () => {
    // Three seconds into a lap of six, after a tenth of a second on the page.
    near(await placeAfter({ delay: 3 }, 0.1), 50 + (0.1 / 6) * 100);
  });

  it("lets the transition given replace its own", async () => {
    // A lap of twelve seconds, and no second lap.
    const once = { transition: { duration: 12, repeat: 0 } };

    near(await placeAfter(once, 3), 25);
    expect(await placeAfter(once, 13)).toBe(100);
  });
});

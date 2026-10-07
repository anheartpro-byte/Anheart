import { render, type TestElement } from "@/test-support/render";
import { describe, expect, it } from "vitest";
import { Ripple } from "./ripple";

/**
 * The rings that spread behind a block of the landing page: how many are
 * drawn, how each one is larger, fainter and later than the one inside it.
 */

function drawn(ui: React.ReactElement) {
  const root = render(ui).container.children[0];
  return { root, rings: root.children };
}

function pixels(ring: TestElement, side: "width" | "height"): number {
  const value = String(ring.style[side]);
  expect(value.endsWith("px")).toBe(true);
  return Number.parseFloat(value);
}

describe("ANH-203 ripple", () => {
  it("draws eight rings unless told how many", () => {
    expect(drawn(<Ripple />).rings.length).toBe(8);
    expect(drawn(<Ripple numCircles={3} />).rings.length).toBe(3);
    expect(drawn(<Ripple numCircles={0} />).rings.length).toBe(0);
  });

  it("makes each ring 70 px larger than the one inside it, from 210 px", () => {
    const { rings } = drawn(<Ripple numCircles={4} />);

    expect(rings.map((ring) => pixels(ring, "width"))).toEqual([
      210, 280, 350, 420,
    ]);
    // Rings, not ovals.
    expect(rings.map((ring) => pixels(ring, "height"))).toEqual([
      210, 280, 350, 420,
    ]);
  });

  it("starts from the size given for the first ring", () => {
    const { rings } = drawn(<Ripple numCircles={3} mainCircleSize={100} />);

    expect(rings.map((ring) => pixels(ring, "width"))).toEqual([100, 170, 240]);
  });

  it("makes each ring fainter than the one inside it, from the opacity given", () => {
    const usual = drawn(<Ripple numCircles={4} />).rings.map((ring) =>
      Number(ring.style.opacity),
    );
    expect(usual[0]).toBe(0.24);
    expect(usual[1]).toBeCloseTo(0.21);
    expect(usual[2]).toBeCloseTo(0.18);
    expect(usual[3]).toBeCloseTo(0.15);

    const stronger = drawn(
      <Ripple numCircles={2} mainCircleOpacity={0.5} />,
    ).rings.map((ring) => Number(ring.style.opacity));
    expect(stronger[0]).toBe(0.5);
    expect(stronger[1]).toBeCloseTo(0.47);
  });

  it("starts each ring 60 ms after the one inside it, and numbers them for the styles", () => {
    const { rings } = drawn(<Ripple numCircles={4} />);

    const delays = rings.map((ring) => String(ring.style.animationDelay));
    expect(delays[0]).toBe("0s");
    expect(delays.map((delay) => Number.parseFloat(delay))).toEqual([
      0,
      expect.closeTo(0.06),
      expect.closeTo(0.12),
      expect.closeTo(0.18),
    ]);
    expect(rings.map((ring) => Number(ring.style["--i"]))).toEqual([
      0, 1, 2, 3,
    ]);
  });

  it("keeps the classes and the attributes it is given, and stays out of the way of the mouse", () => {
    const { root } = drawn(
      <Ripple className="opacity-50" id="waves" aria-hidden />,
    );

    expect(root.id).toBe("waves");
    expect(root.getAttribute("aria-hidden")).toBe("true");
    expect(root.className.split(" ")).toEqual(
      expect.arrayContaining(["opacity-50", "pointer-events-none"]),
    );
  });
});

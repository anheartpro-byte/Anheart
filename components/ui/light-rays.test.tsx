import { render, settle, type TestElement } from "@/test-support/render";
import { frames } from "@/test-support/browser";
import type { ComponentProps } from "react";
import { createRef } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LightRays } from "./light-rays";

/**
 * The rays of light behind the top of the landing page: how many there are,
 * the color, blur and length they share, where chance puts each of them, and
 * how fast they light up and fade.
 *
 * Motion runs for real, on the frame clock of the tests and fake timers.
 * `Math.random` answers what the test says, so that each ray is the one the
 * test expects: with 0.5 everywhere, a ray stands in the middle (50 %), is
 * 240 px wide, upright, as bright as 0.85 at its peak, starts half a cycle
 * late and takes one cycle to light up and fade.
 */

type Props = ComponentProps<typeof LightRays>;

function mount(props: Props = {}) {
  const screen = render(<LightRays {...props} />);
  const root = screen.container.children[0];
  // The two glows of the background come first, then one element per ray.
  const rays = () => root.children[0].children.slice(2);
  return { screen, root, rays };
}

function brightness(ray: TestElement): number {
  return Number(ray.style.opacity ?? 0);
}

function seconds(time: number) {
  return settle(() => frames.advance(time * 1000));
}

function chance(answer: number) {
  vi.spyOn(Math, "random").mockReturnValue(answer);
}

beforeEach(() => {
  vi.useFakeTimers();
  chance(0.5);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("ANH-203 light rays: how many", () => {
  it("draws seven rays unless told how many", () => {
    expect(mount().rays().length).toBe(7);
    expect(mount({ count: 3 }).rays().length).toBe(3);
  });

  it.each([0, -2])(
    "draws no ray for a count of %i, only the glow behind",
    (count) => {
      const { root, rays } = mount({ count });

      expect(rays().length).toBe(0);
      expect(root.children[0].children.length).toBe(2);
    },
  );

  it("draws the rays anew when the count changes", () => {
    const { screen, rays } = mount({ count: 2 });

    screen.rerender(<LightRays count={5} />);

    expect(rays().length).toBe(5);
  });

  it("draws no ray on the server: chance places them, in the browser only", () => {
    const html = renderToStaticMarkup(<LightRays count={4} />);

    expect(html).not.toContain("--ray-left");
    expect(html.split('aria-hidden="true"').length - 1).toBe(2);
  });
});

describe("ANH-203 light rays: what the rays share", () => {
  it("is pale blue, blurred by 36 px and 70 % of the window high unless told otherwise", () => {
    const { root } = mount();

    expect(root.style["--light-rays-color"]).toBe("rgba(160, 210, 255, 0.2)");
    expect(root.style["--light-rays-blur"]).toBe("36px");
    expect(root.style["--light-rays-length"]).toBe("70vh");
  });

  it("takes the color, the blur and the length it is given", () => {
    const { root } = mount({ color: "#fde68a", blur: 12, length: "40vh" });

    expect(root.style["--light-rays-color"]).toBe("#fde68a");
    expect(root.style["--light-rays-blur"]).toBe("12px");
    expect(root.style["--light-rays-length"]).toBe("40vh");
  });

  it("lets the page add to its style, and keeps the classes, attributes and ref it is given", () => {
    const ref = createRef<HTMLDivElement>();
    const { root } = mount({
      ref,
      style: {
        opacity: 0.5,
        "--light-rays-blur": "4px",
      } as React.CSSProperties,
      className: "z-0",
      id: "rays",
    });

    expect(root.style.opacity).toBe("0.5");
    expect(root.style["--light-rays-blur"]).toBe("4px");
    expect(root.style["--light-rays-length"]).toBe("70vh");
    expect(root.id).toBe("rays");
    expect(root.className.split(" ")).toEqual(
      expect.arrayContaining(["z-0", "pointer-events-none"]),
    );
    expect((ref.current as unknown) === root).toBe(true);
  });
});

describe("ANH-203 light rays: where chance puts a ray", () => {
  it.each([
    ["at its lowest", 0, "8%", "160px"],
    ["in the middle", 0.5, "50%", "240px"],
    ["at its highest", 1, "92%", "320px"],
  ])(
    "places and sizes a ray within its bounds: chance %s",
    (_name, answer, left, width) => {
      chance(answer);

      const [ray] = mount({ count: 1 }).rays();

      expect(ray.style["--ray-left"]).toBe(left);
      expect(ray.style["--ray-width"]).toBe(width);
    },
  );

  it("tilts a ray by up to 28 degrees to either side", async () => {
    chance(0);
    const [leaning] = mount({ count: 1, speed: 10 }).rays();
    chance(0.5);
    const [upright] = mount({ count: 1, speed: 10 }).rays();

    // Before a ray starts to swing, it stands at its own angle.
    expect(leaning.style.transform).toBe("rotate(-28deg)");
    expect(upright.style.transform).toBe("none");
  });
});

describe("ANH-203 light rays: lighting up and fading", () => {
  it("lights a ray up to its own brightness half-way through its cycle, then fades it", async () => {
    // With a speed of 10: the ray starts after 5 s and takes 10 s.
    const [ray] = mount({ count: 1, speed: 10 }).rays();

    await seconds(4.9);
    expect(brightness(ray)).toBe(0);

    await seconds(5.1);
    expect(brightness(ray)).toBeCloseTo(0.85, 2);
    // At its peak it has swung 1.7 degrees to one side.
    expect(String(ray.style.transform)).toMatch(/^rotate\(1\.[67]\d*deg\)$/);

    await seconds(5);
    expect(brightness(ray)).toBeLessThan(0.01);
  });

  it("lights up sooner and faster when the speed given is a shorter cycle", async () => {
    const [slow] = mount({ count: 1, speed: 10 }).rays();
    const [fast] = mount({ count: 1, speed: 2 }).rays();

    // 2 s: the peak of a cycle of 2 s, and before the start of a cycle of 10 s.
    await seconds(2);

    expect(brightness(fast)).toBeCloseTo(0.85, 1);
    expect(brightness(slow)).toBe(0);
  });

  it("takes a cycle of 14 s when no speed is given", async () => {
    const [ray] = mount({ count: 1 }).rays();

    await seconds(6.9);
    expect(brightness(ray)).toBe(0);

    await seconds(7.1);
    expect(brightness(ray)).toBeCloseTo(0.85, 2);
  });

  it("never cycles faster than a tenth of a second, whatever the speed given", async () => {
    const [ray] = mount({ count: 1, speed: 0 }).rays();
    let brightest = 0;

    // A cycle of 0.1 s: it starts after 50 ms and peaks 50 ms later.
    for (let frame = 0; frame < 10; frame += 1) {
      await seconds(0.016);
      brightest = Math.max(brightest, brightness(ray));
    }

    expect(brightest).toBeGreaterThan(0.5);
  });
});

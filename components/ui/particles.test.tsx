import { fireWindow, render, type TestElement } from "@/test-support/render";
import {
  frames,
  installCanvas,
  installLayout,
  watchWindowListeners,
  type CanvasCalls,
} from "@/test-support/browser";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Particles } from "./particles";

/**
 * The field of drifting dots behind the landing page: how many circles each
 * frame draws, where, how opaque and in which color, how they follow the
 * mouse, and what is given back when the page is left.
 *
 * No browser draws anything here. The canvas is a stand-in that records what
 * it is asked (`installCanvas`), the frames are run by the test (`frames`),
 * the container is given a box of 400 by 300 on a screen of density 2
 * (`installLayout`), and `Math.random` answers what the test says, so that
 * each circle is where the test put it.
 */

const WIDTH = 400;
const HEIGHT = 300;
const DENSITY = 2;

/** What `Math.random` answers for one circle, in the order the component asks. */
type Dice = {
  x: number;
  y: number;
  size: number;
  alpha: number;
  dx: number;
  dy: number;
  magnetism: number;
};

/**
 * A circle in the middle of the canvas: at (200, 150), of radius 1 + `size`,
 * opaque at 0.4 once faded in, drifting nowhere, pulled 2.1 times by the mouse.
 */
const MIDDLE: Dice = {
  x: 0.5,
  y: 0.5,
  size: 0.5,
  alpha: 0.5,
  dx: 0.5,
  dy: 0.5,
  magnetism: 0.5,
};
const ORDER = ["x", "y", "size", "alpha", "dx", "dy", "magnetism"] as const;

/** The answer of `Math.random` that puts a circle on this pixel of an axis. */
function at(pixel: number, length: number): number {
  return (pixel + 0.5) / length;
}

let canvas: CanvasCalls;
let layout: ReturnType<typeof installLayout>;
/** What the next circles are made of. */
let dice: Dice;

/** Runs one frame and gives what it drew: the circles, their colors, their shifts. */
function frame() {
  canvas.arc.mockClear();
  canvas.translate.mockClear();
  canvas.clearRect.mockClear();
  canvas.fills.length = 0;
  frames.run();
  return {
    circles: canvas.arc.mock.calls.map(([x, y, radius]) => ({ x, y, radius })),
    colors: [...canvas.fills],
    shifts: canvas.translate.mock.calls.map(([x, y]) => ({ x, y })),
  };
}

function canvasOf(screen: ReturnType<typeof render>) {
  return screen.tag("canvas")[0] as TestElement & {
    width: number;
    height: number;
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  canvas = installCanvas();
  layout = installLayout(
    { width: WIDTH, height: HEIGHT },
    { pixelRatio: DENSITY },
  );
  dice = MIDDLE;
  let asked = 0;
  vi.spyOn(Math, "random").mockImplementation(() => {
    const answer = dice[ORDER[asked % ORDER.length]];
    asked += 1;
    return answer;
  });
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("ANH-203 particles: the canvas", () => {
  it("takes the size of its container, at the density of the screen", () => {
    const screen = render(<Particles quantity={1} />);

    const drawn = canvasOf(screen);
    // Twice the pixels on a screen of density 2, shown at the size of the box.
    expect(drawn.width).toBe(WIDTH * DENSITY);
    expect(drawn.height).toBe(HEIGHT * DENSITY);
    expect(drawn.style.width).toBe("400px");
    expect(drawn.style.height).toBe("300px");
    expect(canvas.scale).toHaveBeenLastCalledWith(DENSITY, DENSITY);
  });

  it("is decoration: hidden from screen readers, out of the way of the mouse", () => {
    const screen = render(
      <Particles quantity={1} className="absolute inset-0" id="dots" />,
    );

    const container = screen.field("dots");
    expect(container.getAttribute("aria-hidden")).toBe("true");
    expect(container.className.split(" ")).toEqual(
      expect.arrayContaining(["pointer-events-none", "absolute", "inset-0"]),
    );
    expect(container.children.map((child) => child.localName)).toEqual([
      "canvas",
    ]);
  });

  it("is drawn empty on the server, where there is no window to measure", () => {
    vi.stubGlobal("window", undefined);
    try {
      const html = renderToStaticMarkup(<Particles className="absolute" />);

      expect(html).toContain('aria-hidden="true"');
      expect(html).toContain("<canvas");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("draws nothing, and does not fail, when the browser gives no 2D context", () => {
    canvas = installCanvas({ supported: false });

    const screen = render(<Particles quantity={3} />);
    frame();

    expect(canvas.arc).not.toHaveBeenCalled();
    expect(canvas.clearRect).not.toHaveBeenCalled();
    expect(canvasOf(screen).width).toBeUndefined();
  });
});

describe("ANH-203 particles: what a frame draws", () => {
  it.each([1, 3, 10])(
    "draws the circles of a quantity of %i on each frame",
    (quantity) => {
      render(<Particles quantity={quantity} />);

      const first = frame();
      const second = frame();

      // The canvas is filled twice each time it is set up (once when it is sized,
      // once when it is drawn), so it holds twice the quantity asked.
      expect(first.circles.length).toBe(2 * quantity);
      expect(second.circles.length).toBe(2 * quantity);
    },
  );

  it("draws for a quantity of a hundred when none is given", () => {
    render(<Particles />);

    expect(frame().circles.length).toBe(200);
  });

  it("draws no circle at all for a quantity of zero, and still wipes the canvas", () => {
    render(<Particles quantity={0} />);
    // Neither when the canvas is set up, nor on a frame.
    expect(canvas.arc).not.toHaveBeenCalled();

    expect(frame().circles).toEqual([]);
    expect(canvas.clearRect).toHaveBeenCalledTimes(1);
  });

  it("wipes the whole canvas before it draws a frame", () => {
    render(<Particles quantity={1} />);

    frame();

    expect(canvas.clearRect.mock.calls).toEqual([[0, 0, WIDTH, HEIGHT]]);
  });

  it("draws each circle where it is, as a full disc of its radius", () => {
    dice = { ...MIDDLE, x: at(120, WIDTH), y: at(40, HEIGHT) };
    render(<Particles quantity={1} size={3} />);

    const { circles } = frame();

    // The radius is the size asked plus 0 or 1: here 1.
    expect(circles).toEqual([
      { x: 120, y: 40, radius: 4 },
      { x: 120, y: 40, radius: 4 },
    ]);
    expect(canvas.arc).toHaveBeenLastCalledWith(120, 40, 4, 0, 2 * Math.PI);
    // Each circle is drawn in its own shift, undone before the next one.
    expect(canvas.setTransform).toHaveBeenLastCalledWith(
      DENSITY,
      0,
      0,
      DENSITY,
      0,
      0,
    );
  });

  it("keeps asking for the next frame", () => {
    render(<Particles quantity={1} />);
    expect(frames.pending).toBe(1);
    const [before] = frames.waiting;

    frames.run();

    expect(frames.pending).toBe(1);
    expect(frames.waiting[0]).not.toBe(before);
  });

  it("moves each circle by the speed given, frame after frame", () => {
    render(<Particles quantity={1} vx={2} vy={-1} />);

    expect(frame().circles[0]).toEqual({ x: 202, y: 149, radius: 1.4 });
    expect(frame().circles[0]).toEqual({ x: 204, y: 148, radius: 1.4 });
  });

  it("lets a circle drift on its own when no speed is given", () => {
    // A drift of 0.05 px per frame to the right and to the bottom.
    dice = { ...MIDDLE, dx: 1, dy: 1 };
    render(<Particles quantity={1} />);

    const { circles } = frame();

    expect(circles[0].x).toBeCloseTo(200.05);
    expect(circles[0].y).toBeCloseTo(150.05);
  });
});

describe("ANH-203 particles: the color and the opacity of a circle", () => {
  it.each([
    ["white unless told otherwise", undefined, "255, 255, 255"],
    ["a color written with six digits", "#ff8800", "255, 136, 0"],
    ["a color written with three digits", "#f80", "255, 136, 0"],
    ["a color written without the hash", "0080ff", "0, 128, 255"],
  ])("draws in %s", (_name, color, rgb) => {
    render(<Particles quantity={2} color={color} />);

    const { colors } = frame();

    expect(colors.length).toBe(4);
    for (const fill of colors) expect(fill).toBe(`rgba(${rgb}, 0.02)`);
  });

  it("fades a circle in, up to the opacity drawn for it and no further", () => {
    render(<Particles quantity={1} />);

    expect(frame().colors[0]).toBe("rgba(255, 255, 255, 0.02)");
    expect(frame().colors[0]).toBe("rgba(255, 255, 255, 0.04)");
    frames.run(40);
    expect(frame().colors[0]).toBe("rgba(255, 255, 255, 0.4)");
  });

  it("dims a circle as it nears an edge", () => {
    // 5 px from the left edge, radius 1.4: 3.6 px of room out of the 20 that
    // count as near, so 18 % of its opacity of 0.4.
    dice = { ...MIDDLE, x: at(5, WIDTH) };
    render(<Particles quantity={1} />);

    expect(frame().colors).toEqual([
      "rgba(255, 255, 255, 0.072)",
      "rgba(255, 255, 255, 0.072)",
    ]);
  });

  it("hides a circle that touches an edge", () => {
    dice = { ...MIDDLE, y: at(0, HEIGHT) };
    render(<Particles quantity={1} />);

    expect(frame().colors).toEqual([
      "rgba(255, 255, 255, 0)",
      "rgba(255, 255, 255, 0)",
    ]);
  });

  it("draws again in the new color when the color changes, on one loop of frames", () => {
    const screen = render(<Particles quantity={1} color="#ff0000" />);
    const [before] = frames.waiting;

    screen.rerender(<Particles quantity={1} color="#0000ff" />);

    // The loop of the old color is stopped: one frame waits, not two.
    expect(frames.cancelled).toEqual([before]);
    expect(frames.pending).toBe(1);
    const { colors } = frame();
    expect(colors.length).toBe(2);
    expect(colors.map((fill) => fill.slice(0, fill.lastIndexOf(",")))).toEqual([
      "rgba(0, 0, 255",
      "rgba(0, 0, 255",
    ]);
  });
});

describe("ANH-203 particles: following the mouse", () => {
  it("shifts each circle a little towards the mouse on each frame", async () => {
    render(<Particles quantity={1} />);
    expect(frame().shifts[0]).toEqual({ x: 0, y: 0 });

    // 100 px right of the centre of the canvas and 50 px below it.
    await fireWindow("mousemove", { clientX: 300, clientY: 200 });
    const first = frame().shifts[0];
    const second = frame().shifts[0];

    // The pull is the distance over staticity / magnetism: 100 / (50 / 2.1)
    // = 4.2 px. A fiftieth of what is left to cover is covered per frame.
    expect(first.x).toBeCloseTo(4.2 / 50);
    expect(first.y).toBeCloseTo(2.1 / 50);
    expect(second.x).toBeCloseTo(4.2 / 50 + (4.2 - 4.2 / 50) / 50);
    expect(second.x).toBeGreaterThan(first.x);
  });

  it("measures the mouse from where the canvas is on the page", async () => {
    layout.resize({ width: WIDTH, height: HEIGHT, left: 100, top: 50 });
    render(<Particles quantity={1} />);

    // The centre of the canvas is at (300, 200) on the page: no pull at all.
    await fireWindow("mousemove", { clientX: 300, clientY: 200 });
    expect(frame().shifts[0]).toEqual({ x: 0, y: 0 });

    await fireWindow("mousemove", { clientX: 250, clientY: 200 });
    const { x, y } = frame().shifts[0];
    expect(x).toBeCloseTo(-2.1 / 50);
    expect(y).toBe(0);
  });

  it.each([
    ["right of", { clientX: WIDTH + 1, clientY: 150 }],
    ["left of", { clientX: -1, clientY: 150 }],
    ["below", { clientX: 200, clientY: HEIGHT + 1 }],
    ["above", { clientX: 200, clientY: -1 }],
  ])("ignores a mouse %s the canvas", async (_name, position) => {
    render(<Particles quantity={1} />);

    await fireWindow("mousemove", position);

    expect(frame().shifts[0]).toEqual({ x: 0, y: 0 });
  });

  it("keeps the last place the mouse had inside the canvas once it leaves", async () => {
    render(<Particles quantity={1} />);
    await fireWindow("mousemove", { clientX: 300, clientY: 150 });
    const inside = frame().shifts[0].x;

    await fireWindow("mousemove", { clientX: 2000, clientY: 150 });

    expect(frame().shifts[0].x).toBeGreaterThan(inside);
  });

  it("pulls less when staticity is higher, and faster when ease is lower", async () => {
    render(<Particles quantity={1} staticity={100} ease={10} />);

    await fireWindow("mousemove", { clientX: 300, clientY: 150 });

    // 100 / (100 / 2.1) = 2.1 px to cover, a tenth of it per frame.
    expect(frame().shifts[0].x).toBeCloseTo(2.1 / 10);
  });
});

describe("ANH-203 particles: a circle that leaves the canvas", () => {
  it.each([
    ["right", { x: at(WIDTH - 1, WIDTH) }, { vx: 1 }],
    ["left", { x: at(0, WIDTH) }, { vx: -1 }],
    ["bottom", { y: at(HEIGHT - 1, HEIGHT) }, { vy: 1 }],
    ["top", { y: at(0, HEIGHT) }, { vy: -1 }],
  ])(
    "is replaced by a new one when it goes out by the %s",
    (_name, start, speed) => {
      // Given circles one pixel from an edge, moving towards it.
      dice = { ...MIDDLE, ...start };
      render(<Particles quantity={2} {...speed} />);
      // When new circles are made in the middle from now on.
      dice = MIDDLE;

      frames.run(12);
      const { circles } = frame();

      // Then the canvas still holds as many circles, all of them back inside.
      expect(circles.length).toBe(4);
      for (const { x, y } of circles) {
        expect(Math.abs(x - 200)).toBeLessThan(20);
        expect(Math.abs(y - 150)).toBeLessThan(20);
      }
    },
  );
});

describe("ANH-203 particles: when the window is resized", () => {
  it("sets the canvas up again 200 ms after the last resize of a burst", async () => {
    const screen = render(<Particles quantity={1} />);
    canvas.scale.mockClear();
    layout.resize({ width: 800, height: 600 });

    await fireWindow("resize");
    vi.advanceTimersByTime(150);
    await fireWindow("resize");
    vi.advanceTimersByTime(199);

    // 349 ms after the first resize, but 199 ms after the last: nothing yet.
    expect(canvas.scale).not.toHaveBeenCalled();
    expect(canvasOf(screen).width).toBe(WIDTH * DENSITY);

    vi.advanceTimersByTime(1);

    expect(canvas.scale).toHaveBeenCalledTimes(1);
    expect(canvasOf(screen).width).toBe(800 * DENSITY);
    expect(canvasOf(screen).style.height).toBe("600px");
    expect(frame().circles).toEqual([
      { x: 400, y: 300, radius: 1.4 },
      { x: 400, y: 300, radius: 1.4 },
    ]);
  });

  it("sets the canvas up again when asked to refresh", () => {
    const screen = render(<Particles quantity={1} />);
    frames.run(5);
    canvas.scale.mockClear();

    screen.rerender(<Particles quantity={1} refresh />);

    expect(canvas.scale).toHaveBeenCalledTimes(1);
    // New circles: they fade in from nothing again.
    expect(frame().colors[0]).toBe("rgba(255, 255, 255, 0.02)");
  });
});

describe("ANH-203 particles: leaving the page", () => {
  it("cancels the frame it waits for", () => {
    const screen = render(<Particles quantity={1} />);
    frames.run(3);
    const [waiting] = frames.waiting;

    screen.unmount();

    expect(frames.cancelled).toEqual([waiting]);
    expect(frames.pending).toBe(0);
  });

  it("takes back its listeners of the mouse and of the size of the window", () => {
    const listeners = watchWindowListeners();
    const screen = render(<Particles quantity={1} />);
    expect(listeners.left().sort()).toEqual(["mousemove", "resize"]);

    screen.unmount();

    expect(listeners.left()).toEqual([]);
  });

  it("forgets a resize it had not handled yet", async () => {
    const screen = render(<Particles quantity={1} />);
    await fireWindow("resize");
    canvas.scale.mockClear();

    screen.unmount();
    vi.advanceTimersByTime(1000);

    expect(vi.getTimerCount()).toBe(0);
    expect(canvas.scale).not.toHaveBeenCalled();
  });
});

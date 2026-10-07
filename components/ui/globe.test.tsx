import {
  fire,
  fireWindow,
  render,
  settle,
  type TestElement,
} from "@/test-support/render";
import {
  frames,
  installLayout,
  watchWindowListeners,
} from "@/test-support/browser";
import type { COBEOptions } from "cobe";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type Mock,
} from "vitest";
import { Globe } from "./globe";

/**
 * The globe of the landing page: the globe it asks cobe for, how it turns by
 * itself, how it follows a drag of the mouse or of a finger, and what it gives
 * back when the page is left.
 *
 * cobe draws with WebGL, which nothing provides here, so it is replaced by a
 * stand-in that records the canvas and the options our component hands it and
 * the call that destroys it. What cobe would do on each frame, the test does:
 * it calls the `onRender` it was given and reads what our code wrote in the
 * state. The spring that smooths a drag is the real one of Motion, run on the
 * frame clock of the tests.
 */

type Created = { canvas: unknown; options: COBEOptions; destroy: Mock };

const created = vi.hoisted(() => [] as Created[]);

vi.mock("cobe", () => ({
  default: (canvas: unknown, options: COBEOptions) => {
    const globe: Created = { canvas, options, destroy: vi.fn() };
    created.push(globe);
    return { destroy: globe.destroy };
  },
}));

const WIDTH = 300;
/** What the globe turns by on each frame when nobody holds it, in radians. */
const SPIN = 0.005;
/** How many pixels of drag turn the globe by one radian. */
const DAMPING = 1400;

let layout: ReturnType<typeof installLayout>;

/** Draws one frame as cobe would: asks our code for the state of the globe. */
function drawFrame(globe: Created = created.at(-1) as Created) {
  const state: Record<string, number> = {};
  globe.options.onRender(state);
  return state;
}

function canvasOf(screen: ReturnType<typeof render>): TestElement {
  return screen.tag("canvas")[0];
}

beforeEach(() => {
  vi.useFakeTimers();
  created.length = 0;
  layout = installLayout({ width: WIDTH, height: WIDTH });
});
afterEach(() => {
  vi.useRealTimers();
});

describe("ANH-203 globe: the globe asked of cobe", () => {
  it("asks for one globe on its canvas, twice as large as the canvas is wide", () => {
    const screen = render(<Globe />);

    expect(created.length).toBe(1);
    expect(created[0].canvas === canvasOf(screen)).toBe(true);
    expect(created[0].options.width).toBe(2 * WIDTH);
    expect(created[0].options.height).toBe(2 * WIDTH);
  });

  it("asks for the globe of the landing page unless given another one", () => {
    render(<Globe />);

    const { options } = created[0];
    expect(options.devicePixelRatio).toBe(2);
    expect(options.theta).toBe(0.3);
    expect(options.dark).toBe(0);
    expect(options.mapSamples).toBe(16000);
    expect(options.markers.length).toBe(10);
    // The first city marked: Manila.
    expect(options.markers[0]).toEqual({
      location: [14.5995, 120.9842],
      size: 0.03,
    });
  });

  it("asks for the globe it is given, but keeps the size and the frames to itself", () => {
    const theirs = vi.fn();
    const config: COBEOptions = {
      width: 10,
      height: 10,
      onRender: theirs,
      devicePixelRatio: 1,
      phi: 1,
      theta: 0,
      dark: 1,
      diffuse: 1.2,
      mapSamples: 8000,
      mapBrightness: 6,
      baseColor: [0.3, 0.3, 0.3],
      markerColor: [1, 0, 0],
      glowColor: [0, 0, 1],
      markers: [{ location: [48.8566, 2.3522], size: 0.1 }],
    };

    render(<Globe config={config} />);
    const state = drawFrame();

    const { options } = created[0];
    expect(options.dark).toBe(1);
    expect(options.mapSamples).toBe(8000);
    expect(options.markers).toEqual([
      { location: [48.8566, 2.3522], size: 0.1 },
    ]);
    // The size is the one of the canvas, and each frame is ours to write.
    expect(options.width).toBe(2 * WIDTH);
    expect(state.phi).toBe(SPIN);
    expect(theirs).not.toHaveBeenCalled();
  });

  it("keeps the same globe when it is drawn again with the same configuration", () => {
    const screen = render(<Globe className="top-10" />);

    screen.rerender(<Globe className="top-20" />);

    expect(created.length).toBe(1);
    expect(created[0].destroy).not.toHaveBeenCalled();
    expect(canvasOf(screen).parentElement?.className.split(" ")).toContain(
      "top-20",
    );
  });

  it("replaces the globe when its configuration changes", () => {
    const first = { ...({} as COBEOptions), dark: 0, markers: [] };
    const second = { ...first, dark: 1 };
    const screen = render(<Globe config={first} />);

    screen.rerender(<Globe config={second} />);

    expect(created.length).toBe(2);
    expect(created[0].destroy).toHaveBeenCalledTimes(1);
    expect(created[1].destroy).not.toHaveBeenCalled();
    expect(created[1].options.dark).toBe(1);
  });

  it("fades the canvas in once the globe is there", async () => {
    const screen = render(<Globe />);
    expect(canvasOf(screen).style.opacity).toBeUndefined();

    await settle(() => vi.advanceTimersByTime(0));

    expect(canvasOf(screen).style.opacity).toBe("1");
  });
});

describe("ANH-203 globe: turning by itself", () => {
  it("turns a little more on each frame, at the size of the canvas", () => {
    render(<Globe />);

    const first = drawFrame();
    const second = drawFrame();

    expect(first).toEqual({ phi: SPIN, width: 2 * WIDTH, height: 2 * WIDTH });
    expect(second.phi).toBeCloseTo(2 * SPIN);
  });

  it("draws at the new size of the canvas once the window is resized", async () => {
    render(<Globe />);
    layout.resize({ width: 500, height: 500 });

    // Not before the window says so: the width is read on a resize only.
    expect(drawFrame().width).toBe(2 * WIDTH);

    await fireWindow("resize");

    expect(drawFrame()).toMatchObject({ width: 1000, height: 1000 });
  });
});

describe("ANH-203 globe: dragging it", () => {
  it("shows a hand that grabs while the pointer is down, and an open hand after", async () => {
    const screen = render(<Globe />);
    const canvas = canvasOf(screen);

    await fire(canvas, "pointerdown", { clientX: 100 });
    expect(canvas.style.cursor).toBe("grabbing");

    await fire(canvas, "pointerup");
    expect(canvas.style.cursor).toBe("grab");

    await fire(canvas, "pointerdown", { clientX: 100 });
    await fire(canvas, "pointerout");
    expect(canvas.style.cursor).toBe("grab");
  });

  it("stops turning by itself while it is held, and starts again once released", async () => {
    const screen = render(<Globe />);
    const held = drawFrame().phi;

    await fire(canvasOf(screen), "pointerdown", { clientX: 100 });
    expect(drawFrame().phi).toBe(held);
    expect(drawFrame().phi).toBe(held);

    await fire(canvasOf(screen), "pointerup");
    expect(drawFrame().phi).toBeCloseTo(held + SPIN);
  });

  it("turns by the distance dragged, smoothly: not at once, and all the way in the end", async () => {
    const screen = render(<Globe />);
    const canvas = canvasOf(screen);
    await fire(canvas, "pointerdown", { clientX: 100 });
    const before = drawFrame().phi;

    // 140 px to the right of where the pointer went down.
    await fire(canvas, "mousemove", { clientX: 240 });

    // The spring has not moved yet.
    expect(drawFrame().phi).toBe(before);

    await settle(() => frames.advance(300));
    const partWay = drawFrame().phi - before;
    expect(partWay).toBeGreaterThan(0);
    expect(partWay).toBeLessThan(140 / DAMPING);

    await settle(() => frames.advance(5000));
    expect(drawFrame().phi - before).toBeCloseTo(140 / DAMPING, 4);
  });

  it("turns the other way when dragged to the left", async () => {
    const screen = render(<Globe />);
    const canvas = canvasOf(screen);
    await fire(canvas, "pointerdown", { clientX: 400 });
    const before = drawFrame().phi;

    await fire(canvas, "mousemove", { clientX: 190 });
    await settle(() => frames.advance(5000));

    expect(drawFrame().phi - before).toBeCloseTo(-210 / DAMPING, 4);
  });

  it("follows a finger as it follows the mouse", async () => {
    const screen = render(<Globe />);
    const canvas = canvasOf(screen);
    await fire(canvas, "pointerdown", { clientX: 100 });
    const before = drawFrame().phi;

    // A touch that ended has no finger left: nothing to follow.
    await fire(canvas, "touchmove", { touches: [] });
    await fire(canvas, "touchmove", { touches: [{ clientX: 170 }] });
    await settle(() => frames.advance(5000));

    expect(drawFrame().phi - before).toBeCloseTo(70 / DAMPING, 4);
  });

  it("ignores a mouse that moves over it without holding it", async () => {
    const screen = render(<Globe />);
    const canvas = canvasOf(screen);

    await fire(canvas, "mousemove", { clientX: 240 });
    await settle(() => frames.advance(5000));
    // Only the turn of the frame drawn now: no drag was added.
    expect(drawFrame().phi).toBe(SPIN);

    await fire(canvas, "pointerdown", { clientX: 100 });
    await fire(canvas, "pointerup");
    await fire(canvas, "mousemove", { clientX: 240 });
    await settle(() => frames.advance(5000));
    expect(drawFrame().phi).toBeCloseTo(2 * SPIN);
  });
});

describe("ANH-203 globe: leaving the page", () => {
  it("destroys the globe and stops listening to the size of the window", async () => {
    const listeners = watchWindowListeners();
    const screen = render(<Globe />);
    await settle(() => vi.advanceTimersByTime(0));
    expect(listeners.left()).toEqual(["resize"]);

    screen.unmount();

    expect(created.length).toBe(1);
    expect(created[0].destroy).toHaveBeenCalledTimes(1);
    expect(listeners.left()).toEqual([]);
  });
});

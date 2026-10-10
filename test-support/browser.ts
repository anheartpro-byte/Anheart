/**
 * What a browser adds to the document of `test-support/dom`, for the
 * components that ask for it: a clock of animation frames, a canvas, a media
 * query, a size, a list of classes, the observers of what enters the page or
 * changes on it, the listeners left on the window.
 *
 * Nothing here imitates a browser. Each piece is a stand-in a test installs by
 * name, drives itself, and reads afterwards:
 *
 *     const canvas = installCanvas();
 *     installLayout({ width: 400, height: 300 });
 *     render(<Particles quantity={3} />);
 *     frames.run();
 *     expect(canvas.arc).toHaveBeenCalledTimes(...);
 *
 * Everything a test installs is removed before the next one. The one
 * exception is the frame clock, installed when this file is imported: an
 * animation library reads `requestAnimationFrame` once, when it is loaded, so
 * the clock must be there before the component under test is imported. Import
 * this file right after `test-support/render`.
 */

import { afterAll, beforeEach, vi, type Mock } from "vitest";
import { TestElement, TestEvent, TestNode } from "./dom";

const globals = globalThis as Record<string, unknown>;

/** What puts back what a test replaced. */
const restores: (() => void)[] = [];

function setGlobal(name: string, value: unknown) {
  const had = Object.prototype.hasOwnProperty.call(globals, name);
  const before = globals[name];
  globals[name] = value;
  restores.push(() => {
    if (had) globals[name] = before;
    else delete globals[name];
  });
}

function setOnElements(name: string, descriptor: PropertyDescriptor) {
  const before = Object.getOwnPropertyDescriptor(TestElement.prototype, name);
  Object.defineProperty(TestElement.prototype, name, {
    configurable: true,
    ...descriptor,
  });
  restores.push(() => {
    if (before === undefined) {
      delete (TestElement.prototype as unknown as Record<string, unknown>)[
        name
      ];
    } else {
      Object.defineProperty(TestElement.prototype, name, before);
    }
  });
}

// --- The clock of animation frames ---------------------------------------------

type FrameCallback = (time: number) => void;

const waiting = new Map<number, FrameCallback>();
let lastFrame = 0;

/**
 * The frames a component asked for. None runs by itself: the test runs them,
 * one screen refresh at a time.
 */
export const frames = {
  /** The frames cancelled since the test started, by the number their request returned. */
  cancelled: [] as number[],

  /** How many frames wait to be run. */
  get pending(): number {
    return waiting.size;
  },

  /** The numbers of the frames that wait, oldest first. */
  get waiting(): number[] {
    return [...waiting.keys()];
  },

  /**
   * Runs the frames asked for so far, as one refresh of the screen does. The
   * frames they ask for in turn wait for the next call.
   */
  run(times = 1) {
    for (let refresh = 0; refresh < times; refresh += 1) {
      const due = [...waiting.values()];
      waiting.clear();
      for (const callback of due) callback(performance.now());
    }
  },

  /**
   * Lets `ms` milliseconds of animation pass under fake timers: a refresh
   * every 16 ms of the clock of the test, each in its own turn as in a
   * browser. To be awaited: `await settle(() => frames.advance(300))`.
   */
  async advance(ms: number) {
    for (let elapsed = 0; elapsed < ms; elapsed += 16) {
      vi.advanceTimersByTime(16);
      frames.run();
      await Promise.resolve();
    }
  },
};

function requestFrame(callback: FrameCallback): number {
  lastFrame += 1;
  waiting.set(lastFrame, callback);
  return lastFrame;
}

function cancelFrame(frame: number) {
  if (waiting.delete(frame)) frames.cancelled.push(frame);
}

// Installed so that nothing replaces them: fake timers would otherwise take
// the frames over when a test starts them, and run them every 16 ms of their
// own clock, behind the back of a library that kept the first function. Here
// a frame runs when the test says so, fake timers or not.
for (const [name, standIn] of [
  ["requestAnimationFrame", requestFrame],
  ["cancelAnimationFrame", cancelFrame],
] as const) {
  Object.defineProperty(globalThis, name, {
    configurable: true,
    get: () => standIn,
    set: () => {},
  });
}

// --- A canvas --------------------------------------------------------------------

/** The drawing calls of a 2D canvas, each one recorded. `fillStyle` is what the component last set. */
export type CanvasCalls = {
  fillStyle: string;
  /** The `fillStyle` in force at each `fill`, in order. */
  fills: string[];
  arc: Mock;
  beginPath: Mock;
  clearRect: Mock;
  fill: Mock;
  scale: Mock;
  setTransform: Mock;
  translate: Mock;
};

/**
 * Gives every `<canvas>` of the page one 2D context that draws nothing and
 * records what it is asked. With `supported: false` the canvas answers `null`,
 * as a browser does when it cannot give a 2D context.
 */
export function installCanvas({ supported = true } = {}): CanvasCalls {
  const calls: CanvasCalls = {
    fillStyle: "",
    fills: [],
    arc: vi.fn(),
    beginPath: vi.fn(),
    clearRect: vi.fn(),
    fill: vi.fn(() => {
      calls.fills.push(calls.fillStyle);
    }),
    scale: vi.fn(),
    setTransform: vi.fn(),
    translate: vi.fn(),
  };
  setOnElements("getContext", {
    value(this: TestElement, kind: string) {
      const asked = supported && this.localName === "canvas" && kind === "2d";
      return asked ? calls : null;
    },
  });
  return calls;
}

// --- A size ----------------------------------------------------------------------

export type Layout = {
  width: number;
  height: number;
  /** Where the element starts on the page. */
  left?: number;
  top?: number;
};

/**
 * Gives every element of the page the same box, since nothing here lays
 * anything out, and the screen a density of pixels. `resize` changes the box,
 * as a resized window would.
 */
export function installLayout(initial: Layout, { pixelRatio = 1 } = {}) {
  let box = initial;
  setGlobal("devicePixelRatio", pixelRatio);
  const rect = () => {
    const left = box.left ?? 0;
    const top = box.top ?? 0;
    return {
      x: left,
      y: top,
      left,
      top,
      right: left + box.width,
      bottom: top + box.height,
      width: box.width,
      height: box.height,
    };
  };
  setOnElements("offsetWidth", { get: () => box.width });
  setOnElements("offsetHeight", { get: () => box.height });
  setOnElements("getBoundingClientRect", { value: rect });
  return {
    resize(next: Layout) {
      box = next;
    },
  };
}

// --- The classes of an element -----------------------------------------------------

/**
 * Gives every element a `classList` over its `class` attribute: what a
 * component reads and toggles on `<html>` to know and to set the theme.
 */
export function installClassList() {
  setOnElements("classList", {
    get(this: TestElement) {
      const read = () =>
        this.className.split(" ").filter((name) => name !== "");
      const write = (names: string[]) =>
        this.setAttribute("class", names.join(" "));
      const list = {
        contains: (name: string) => read().includes(name),
        add: (name: string) => {
          if (!list.contains(name)) write([...read(), name]);
        },
        remove: (name: string) =>
          write(read().filter((other) => other !== name)),
        toggle(name: string) {
          const had = list.contains(name);
          if (had) list.remove(name);
          else list.add(name);
          return !had;
        },
      };
      return list;
    },
  });
}

// --- A window of a given width ---------------------------------------------------

/**
 * Answers `matchMedia` and `innerWidth` for a window of the width given.
 * `resizeTo` changes the width and tells the listeners of every query asked
 * so far, as a browser does when a breakpoint is crossed.
 */
export function installViewport(width: number, height = 800) {
  const queries: { query: string; node: TestNode }[] = [];
  setGlobal("innerWidth", width);
  setGlobal("innerHeight", height);
  setGlobal("matchMedia", (query: string) => {
    const node = new TestNode(1, "#media-query", null);
    queries.push({ query, node });
    return {
      media: query,
      addEventListener: node.addEventListener.bind(node),
      removeEventListener: node.removeEventListener.bind(node),
    };
  });
  return {
    /** The queries the page asked, as written. */
    asked: () => queries.map(({ query }) => query),
    /** How many listeners the queries still hold: what proves a clean-up. */
    listeners: () =>
      queries.reduce((sum, { node }) => sum + node.listenerCount("change"), 0),
    resizeTo(next: number) {
      globals.innerWidth = next;
      for (const { node } of queries)
        node.dispatchEvent(new TestEvent("change"));
    },
  };
}

// --- The listeners of the window ---------------------------------------------------

/**
 * Watches what a component puts on `window` from now on. `left()` names the
 * events still listened to: what a test reads after an unmount to prove that
 * every listener was taken back, with the function it was added with.
 */
export function watchWindowListeners() {
  const added = vi.spyOn(globalThis, "addEventListener");
  const removed = vi.spyOn(globalThis, "removeEventListener");
  restores.push(() => {
    added.mockRestore();
    removed.mockRestore();
  });
  return {
    left: (): string[] =>
      added.mock.calls
        .filter(
          ([type, listener]) =>
            !removed.mock.calls.some(
              ([goneType, gone]) => goneType === type && gone === listener,
            ),
        )
        .map(([type]) => type),
  };
}

// --- Observers -------------------------------------------------------------------

/** An observer the page created: what it watches, the options it was given, whether it was disconnected. */
export type Observed = {
  callback: (entries: unknown[], observer: unknown) => void;
  options: unknown;
  targets: Set<unknown>;
  disconnected: boolean;
};

function observerStandIn(name: string) {
  const created: Observed[] = [];
  class Observer {
    private readonly self: Observed;

    constructor(callback: Observed["callback"], options?: unknown) {
      this.self = {
        callback,
        options,
        targets: new Set(),
        disconnected: false,
      };
      created.push(this.self);
    }

    observe(target: unknown, options?: unknown) {
      this.self.targets.add(target);
      if (options !== undefined) this.self.options = options;
    }

    unobserve(target: unknown) {
      this.self.targets.delete(target);
    }

    disconnect() {
      this.self.targets.clear();
      this.self.disconnected = true;
    }

    takeRecords() {
      return [];
    }
  }
  setGlobal(name, Observer);
  return {
    /** Every observer the page created, oldest first. */
    created,
    /** The observers that still watch something. */
    watching: () => created.filter((observer) => observer.targets.size > 0),
    /** Tells every observer that still watches something what the test says happened. */
    notify(entry: Record<string, unknown>) {
      for (const observer of created) {
        if (observer.targets.size === 0) continue;
        const entries = [...observer.targets].map((target) => ({
          target,
          ...entry,
        }));
        observer.callback(entries, observer);
      }
    },
  };
}

/**
 * `IntersectionObserver`: nothing is in view until the test says so with
 * `enter()`. Libraries that check an element is an `EventTarget` before they
 * watch it are told the nodes of this document are.
 */
export function installIntersectionObserver() {
  setGlobal("EventTarget", TestNode);
  const observers = observerStandIn("IntersectionObserver");
  return {
    ...observers,
    /** Every watched element enters the page. */
    enter: () => observers.notify({ isIntersecting: true }),
    /** Every watched element leaves the page. */
    leave: () => observers.notify({ isIntersecting: false }),
  };
}

/** `ResizeObserver`: `resize` tells the watchers the size the test gives. */
export function installResizeObserver() {
  const observers = observerStandIn("ResizeObserver");
  return {
    ...observers,
    resize: (width: number, height: number) =>
      observers.notify({ contentRect: { width, height } }),
  };
}

/** `MutationObserver`: `change` tells the watchers that what they watch changed. */
export function installMutationObserver() {
  const observers = observerStandIn("MutationObserver");
  return {
    ...observers,
    change: () => observers.notify({ type: "attributes" }),
  };
}

/**
 * Takes back what the last test installed. Done before each test rather than
 * after: the components of a test are unmounted after its own hooks, and they
 * may still ask the stand-ins something while they leave.
 */
function takeBack() {
  for (const restore of restores.splice(0).reverse()) restore();
  // The frames still waiting are kept: a library that remembers it asked for
  // one would wait for it forever if it were dropped here. A component that
  // leaves one behind shows in `frames.pending` of the next test.
  frames.cancelled.length = 0;
}

beforeEach(takeBack);
afterAll(takeBack);

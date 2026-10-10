import { act, createElement, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  useFreshness,
  useFreshnessJudge,
  type Freshness,
} from "./use-freshness";
import { forgetServerAnswers } from "@/lib/server-clock";
import { LIVE_FRESH_MS, TELEMETRY_FRESH_MS } from "@/lib/training";

/** The server's clock when the tests start: the only clock that dates anything. */
const T0 = 1_800_000_000_000;
/** The machine reports its state every 10 s. */
const HEARTBEAT_MS = 10_000;

/** How far this computer's clock is from the server's, in the tests that say so. */
const VIEWER_CLOCKS = [
  ["on time", 0],
  ["15 s ahead", 15_000],
  ["80 s ahead", 80_000],
  ["10 min ahead", 600_000],
  ["15 s behind", -15_000],
  ["10 min behind", -600_000],
] as const;

/** The server's clock, kept by the tests: it runs with `elapse`. */
let server = T0;

type Props = {
  /** A date the server wrote. */
  updatedAt: number | null | undefined;
  /** The server's clock in the answer that carries `updatedAt`. */
  serverNow: number | null | undefined;
  freshMs?: number;
};

/** Calls the hook and reports each verdict it commits. Renders nothing. */
function Probe({
  updatedAt,
  serverNow,
  freshMs,
  report,
}: Props & { report: (verdict: Freshness) => void }) {
  const verdict = useFreshness(updatedAt, serverNow, freshMs);
  useEffect(() => report(verdict));
  return null;
}

/** Judges two data of one answer on one clock reading. */
function JudgeProbe({
  report,
}: {
  report: (verdicts: [Freshness, Freshness]) => void;
}) {
  const judge = useFreshnessJudge();
  const verdicts: [Freshness, Freshness] = [
    judge(T0, T0),
    judge(T0 - 60_000, T0),
  ];
  useEffect(() => report(verdicts));
  return null;
}

/**
 * Runs a component in real React, without a browser. The probes render
 * nothing, so React needs no more of a host than this container and the few
 * `window` fields stubbed in `beforeEach`. To be replaced by a DOM test
 * library when the repository has one.
 */
function mount(render: () => ReturnType<typeof createElement>) {
  const container = {
    nodeType: 1,
    tagName: "DIV",
    ownerDocument: null,
    addEventListener() {},
    removeEventListener() {},
  } as unknown as Element;
  const root = createRoot(container);
  act(() => root.render(render()));
  return {
    rerender() {
      act(() => root.render(render()));
    },
    unmount() {
      act(() => root.unmount());
    },
  };
}

function mountFreshness(initial: Props) {
  let latest: Freshness | undefined;
  let props = initial;
  const report = (verdict: Freshness) => {
    latest = verdict;
  };
  const view = mount(() => createElement(Probe, { ...props, report }));
  return {
    get fresh() {
      return latest?.fresh;
    },
    get now() {
      return latest?.now;
    },
    /** A new answer reaches the page (the subscription pushes it). */
    receive(next: Props) {
      props = next;
      view.rerender();
    },
    unmount: view.unmount,
  };
}

/** Lets time pass, for the server and for this computer: no datum changes. */
function elapse(ms: number) {
  server += ms;
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

/** Opens the page on a computer whose clock is `offsetMs` away from the server's. */
function viewerClock(offsetMs: number) {
  vi.setSystemTime(server + offsetMs);
}

beforeEach(() => {
  server = T0;
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  forgetServerAnswers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("window", {
    event: undefined,
    document: { activeElement: null },
    HTMLIFrameElement: class {},
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("useFreshness", () => {
  it("turns stale 90 s after the last heartbeat, on the clock alone", () => {
    // Given a machine whose last state was just received
    const view = mountFreshness({ updatedAt: T0, serverNow: T0 });
    expect(view.fresh).toBe(true);

    // When it stops sending and no other datum changes
    elapse(LIVE_FRESH_MS - 1000);
    expect(view.fresh).toBe(true);
    elapse(1000);

    // Then the verdict is stale at 90 s, not at the next change of data
    expect(view.fresh).toBe(false);
    view.unmount();
  });

  it("is live again on the render that brings the next heartbeat", () => {
    // Given a machine shown stale
    const view = mountFreshness({ updatedAt: T0, serverNow: T0 });
    elapse(LIVE_FRESH_MS + 30_000);
    expect(view.fresh).toBe(false);

    // When a heartbeat arrives
    view.receive({ updatedAt: server, serverNow: server });

    // Then it is fresh at once, without waiting for a tick
    expect(view.fresh).toBe(true);
    view.unmount();
  });

  it("stays live as long as the heartbeats keep coming", () => {
    const view = mountFreshness({ updatedAt: T0, serverNow: T0 });
    for (let beat = 1; beat <= 30; beat++) {
      elapse(HEARTBEAT_MS);
      expect(view.fresh).toBe(true);
      view.receive({ updatedAt: server, serverNow: server });
    }
    expect(view.fresh).toBe(true);
    view.unmount();
  });

  it("is stale from the first render for a datum the server says is already old", () => {
    const view = mountFreshness({
      updatedAt: T0 - LIVE_FRESH_MS,
      serverNow: T0,
    });
    expect(view.fresh).toBe(false);
    view.unmount();
  });

  it("adds the age the server gives to the time counted since", () => {
    // The page opens on a state the server says is 60 s old.
    const view = mountFreshness({ updatedAt: T0 - 60_000, serverNow: T0 });
    expect(view.fresh).toBe(true);
    elapse(29_000);
    expect(view.fresh).toBe(true);
    elapse(1000);
    expect(view.fresh).toBe(false);
    view.unmount();
  });

  it.each([undefined, null])("is never fresh without a datum (%s)", (none) => {
    const view = mountFreshness({ updatedAt: none, serverNow: T0 });
    expect(view.fresh).toBe(false);
    elapse(5000);
    expect(view.fresh).toBe(false);
    view.unmount();
  });

  it.each([undefined, null, Number.NaN])(
    "is never fresh without the server's clock, even for a datum this computer finds recent (%s)",
    (none) => {
      const view = mountFreshness({ updatedAt: T0, serverNow: none });
      expect(view.fresh).toBe(false);
      // The durations shown still have a clock to count on.
      expect(view.now).toBe(T0);
      view.unmount();
    },
  );

  it("applies the threshold of the datum it is given (20 s telemetry)", () => {
    const view = mountFreshness({
      updatedAt: T0,
      serverNow: T0,
      freshMs: TELEMETRY_FRESH_MS,
    });
    elapse(19_000);
    expect(view.fresh).toBe(true);
    elapse(1000);
    expect(view.fresh).toBe(false);
    view.unmount();
  });

  it("stops its clock when the view goes away", () => {
    const view = mountFreshness({ updatedAt: T0, serverNow: T0 });
    expect(vi.getTimerCount()).toBe(1);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe.each(VIEWER_CLOCKS)(
  "useFreshness on a computer whose clock is %s",
  (_name, offsetMs) => {
    it("hands out the server's clock, not this computer's", () => {
      viewerClock(offsetMs);
      const view = mountFreshness({ updatedAt: T0, serverNow: T0 });
      expect(view.now).toBe(T0);
      elapse(7000);
      expect(view.now).toBe(T0 + 7000);
      view.unmount();
    });

    it("shows a machine that keeps sending as live, all along", () => {
      viewerClock(offsetMs);
      const view = mountFreshness({ updatedAt: T0, serverNow: T0 });
      for (let beat = 1; beat <= 12; beat++) {
        elapse(HEARTBEAT_MS - 1000);
        // Just before each heartbeat: the oldest the state ever gets.
        expect(view.fresh).toBe(true);
        elapse(1000);
        view.receive({ updatedAt: server, serverNow: server });
        expect(view.fresh).toBe(true);
      }
      view.unmount();
    });

    it("shows a machine that stops sending as stale at 90 s, not later", () => {
      viewerClock(offsetMs);
      const view = mountFreshness({ updatedAt: T0, serverNow: T0 });
      elapse(LIVE_FRESH_MS - 1000);
      expect(view.fresh).toBe(true);
      elapse(1000);
      expect(view.fresh).toBe(false);
      elapse(600_000);
      expect(view.fresh).toBe(false);
      view.unmount();
    });

    it("shows a session that stops sending as stale at 20 s, not later", () => {
      viewerClock(offsetMs);
      const view = mountFreshness({
        updatedAt: T0,
        serverNow: T0,
        freshMs: TELEMETRY_FRESH_MS,
      });
      elapse(TELEMETRY_FRESH_MS - 1000);
      expect(view.fresh).toBe(true);
      elapse(1000);
      expect(view.fresh).toBe(false);
      view.unmount();
    });

    it("shows as stale, from the first render, a state the server says is old", () => {
      viewerClock(offsetMs);
      const view = mountFreshness({
        updatedAt: T0 - LIVE_FRESH_MS,
        serverNow: T0,
      });
      expect(view.fresh).toBe(false);
      view.unmount();
    });
  },
);

describe("useFreshness when this computer's clock is set while the page is open", () => {
  it("still turns stale on time when the clock is set back an hour", () => {
    const view = mountFreshness({ updatedAt: T0, serverNow: T0 });
    elapse(30_000);
    vi.setSystemTime(Date.now() - 3_600_000);
    elapse(LIVE_FRESH_MS - 30_000 - 1000);
    expect(view.fresh).toBe(true);
    elapse(1000);
    expect(view.fresh).toBe(false);
    view.unmount();
  });

  it("errs on the stale side when the clock is set forward, and recovers at the next heartbeat", () => {
    const view = mountFreshness({ updatedAt: T0, serverNow: T0 });
    elapse(5000);
    vi.setSystemTime(Date.now() + 3_600_000);
    elapse(1000);
    // Nothing tells a clock set forward from an hour of sleep.
    expect(view.fresh).toBe(false);
    elapse(4000);
    view.receive({ updatedAt: server, serverNow: server });
    expect(view.fresh).toBe(true);
    expect(view.now).toBe(server);
    view.unmount();
  });
});

describe("useFreshnessJudge", () => {
  it("judges several data on one clock reading, again every second", () => {
    let latest: [Freshness, Freshness] | undefined;
    const view = mount(() =>
      createElement(JudgeProbe, {
        report: (verdicts) => {
          latest = verdicts;
        },
      }),
    );
    expect(latest?.map((v) => v.fresh)).toEqual([true, true]);
    expect(latest?.[0].now).toBe(latest?.[1].now);

    elapse(30_000);
    expect(latest?.map((v) => v.fresh)).toEqual([true, false]);
    elapse(60_000);
    expect(latest?.map((v) => v.fresh)).toEqual([false, false]);
    expect(vi.getTimerCount()).toBe(1);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});

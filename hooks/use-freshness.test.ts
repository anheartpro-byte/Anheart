import { act, createElement, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useFreshness, type Freshness } from "./use-freshness";
import { LIVE_FRESH_MS } from "@/lib/training";

/** The instant of the machine's last heartbeat in these tests. */
const T0 = 1_800_000_000_000;
/** The machine reports its state every 10 s. */
const HEARTBEAT_MS = 10_000;

type Props = { updatedAt: number | null | undefined; freshMs?: number };

/** Calls the hook and reports each verdict it commits. Renders nothing. */
function Probe({
  updatedAt,
  freshMs,
  report,
}: Props & { report: (verdict: Freshness) => void }) {
  const verdict = useFreshness(updatedAt, freshMs);
  useEffect(() => report(verdict));
  return null;
}

/**
 * Runs the hook in real React, without a browser. The probe renders nothing,
 * so React needs no more of a host than this container and the few `window`
 * fields stubbed in `beforeEach`. To be replaced by a DOM test library when the
 * repository has one.
 */
function mountFreshness(initial: Props) {
  let latest: Freshness | undefined;
  const report = (verdict: Freshness) => {
    latest = verdict;
  };
  const container = {
    nodeType: 1,
    tagName: "DIV",
    ownerDocument: null,
    addEventListener() {},
    removeEventListener() {},
  } as unknown as Element;
  const root = createRoot(container);
  act(() => root.render(createElement(Probe, { ...initial, report })));
  return {
    get fresh() {
      return latest?.fresh;
    },
    get now() {
      return latest?.now;
    },
    /** A new datum reaches the page (the subscription pushes it). */
    receive(props: Props) {
      act(() => root.render(createElement(Probe, { ...props, report })));
    },
    unmount() {
      act(() => root.unmount());
    },
  };
}

/** Lets the clock run: nothing else happens, no datum changes. */
function elapse(ms: number) {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
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
    const view = mountFreshness({ updatedAt: T0 });
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
    const view = mountFreshness({ updatedAt: T0 });
    elapse(LIVE_FRESH_MS + 30_000);
    expect(view.fresh).toBe(false);

    // When a heartbeat arrives
    view.receive({ updatedAt: Date.now() });

    // Then it is fresh at once, without waiting for a tick
    expect(view.fresh).toBe(true);
    view.unmount();
  });

  it("stays live as long as the heartbeats keep coming", () => {
    const view = mountFreshness({ updatedAt: T0 });
    for (let beat = 1; beat <= 30; beat++) {
      elapse(HEARTBEAT_MS);
      expect(view.fresh).toBe(true);
      view.receive({ updatedAt: Date.now() });
    }
    expect(view.fresh).toBe(true);
    view.unmount();
  });

  it("is stale from the first render for a datum that is already old", () => {
    const view = mountFreshness({ updatedAt: T0 - LIVE_FRESH_MS });
    expect(view.fresh).toBe(false);
    view.unmount();
  });

  it.each([undefined, null])("is never fresh without a datum (%s)", (none) => {
    const view = mountFreshness({ updatedAt: none });
    expect(view.fresh).toBe(false);
    elapse(5000);
    expect(view.fresh).toBe(false);
    view.unmount();
  });

  it("applies the threshold of the datum it is given (20 s telemetry)", () => {
    const view = mountFreshness({ updatedAt: T0, freshMs: 20_000 });
    elapse(19_000);
    expect(view.fresh).toBe(true);
    elapse(1000);
    expect(view.fresh).toBe(false);
    view.unmount();
  });

  it("hands out the clock reading its verdict was made on", () => {
    const view = mountFreshness({ updatedAt: T0 });
    expect(view.now).toBe(T0);
    elapse(7000);
    expect(view.now).toBe(T0 + 7000);
    view.unmount();
  });

  it("stops its clock when the view goes away", () => {
    const view = mountFreshness({ updatedAt: T0 });
    expect(vi.getTimerCount()).toBe(1);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});

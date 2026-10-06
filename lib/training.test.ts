import { describe, expect, it } from "vitest";
import { isFresh, LIVE_FRESH_MS } from "./training";

const NOW = 1_800_000_000_000;

describe("isFresh", () => {
  it("holds a machine state for 90 s, the delay the guides give", () => {
    expect(LIVE_FRESH_MS).toBe(90_000);
  });

  it("is fresh below the threshold and stale from the threshold on", () => {
    expect(isFresh(NOW - (LIVE_FRESH_MS - 1), NOW)).toBe(true);
    expect(isFresh(NOW - LIVE_FRESH_MS, NOW)).toBe(false);
    expect(isFresh(NOW - (LIVE_FRESH_MS + 1), NOW)).toBe(false);
  });

  it("applies the threshold it is given", () => {
    expect(isFresh(NOW - 19_999, NOW, 20_000)).toBe(true);
    expect(isFresh(NOW - 20_000, NOW, 20_000)).toBe(false);
  });

  it.each([undefined, null, Number.NaN])(
    "is never fresh without a usable timestamp (%s)",
    (updatedAt) => {
      expect(isFresh(updatedAt, NOW)).toBe(false);
    },
  );

  it("keeps a datum stamped slightly ahead of this clock fresh", () => {
    // The server stamps the state; a browser clock a little behind sees it in
    // the future. That is a state just received, not a stale one.
    expect(isFresh(NOW + 500, NOW)).toBe(true);
  });
});

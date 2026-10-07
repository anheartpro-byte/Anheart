import { describe, expect, it } from "vitest";
import {
  datedAfterReception,
  FUTURE_TOLERANCE_MS,
  isFresh,
  LIVE_FRESH_MS,
  shownMachineStatus,
  TELEMETRY_FRESH_MS,
} from "./training";

const NOW = 1_800_000_000_000;

describe("isFresh", () => {
  it("holds a machine state for 90 s, the delay the guides give", () => {
    expect(LIVE_FRESH_MS).toBe(90_000);
  });

  it("holds the last sign of life of a session for 20 s, the delay the guides give", () => {
    expect(TELEMETRY_FRESH_MS).toBe(20_000);
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
    // Two servers of one deployment may differ by a few milliseconds: that is
    // a state just received, not a stale one.
    expect(isFresh(NOW + 500, NOW)).toBe(true);
    expect(isFresh(NOW + FUTURE_TOLERANCE_MS, NOW)).toBe(true);
  });

  it("never calls fresh a datum dated from the future beyond the tolerance", () => {
    // Such a date was not written by the clock it is compared with (a machine
    // whose clock is ahead, say): nothing can be said of its age.
    expect(isFresh(NOW + FUTURE_TOLERANCE_MS + 1, NOW)).toBe(false);
    expect(isFresh(NOW + 600_000, NOW)).toBe(false);
    expect(isFresh(NOW + 600_000, NOW, TELEMETRY_FRESH_MS)).toBe(false);
  });
});

describe("datedAfterReception", () => {
  it("accepts a point measured before the server received it, however long before", () => {
    expect(datedAfterReception(NOW - 1000, NOW)).toBe(false);
    expect(datedAfterReception(NOW - 3_600_000, NOW)).toBe(false);
  });

  it("accepts a point dated up to the tolerance after its reception", () => {
    expect(datedAfterReception(NOW + FUTURE_TOLERANCE_MS, NOW)).toBe(false);
  });

  it("refuses a point dated after its reception beyond the tolerance: the machine's clock is ahead", () => {
    expect(datedAfterReception(NOW + FUTURE_TOLERANCE_MS + 1, NOW)).toBe(true);
    expect(datedAfterReception(NOW + 600_000, NOW)).toBe(true);
  });

  it.each([undefined, null])(
    "says nothing without a reception date (%s)",
    (receivedAt) => {
      expect(datedAfterReception(NOW + 600_000, receivedAt)).toBe(false);
    },
  );
});

describe("shownMachineStatus", () => {
  it.each(["online", "in_session", "offline", "some_future_status"])(
    "shows the record's status while the signal is fresh (%s)",
    (status) => {
      expect(shownMachineStatus(status, true)).toBe(status);
    },
  );

  it.each(["online", "in_session", "offline", "some_future_status"])(
    "shows offline once the signal is not fresh, whatever the record says (%s)",
    (status) => {
      expect(shownMachineStatus(status, false)).toBe("offline");
    },
  );
});

import { describe, expect, it } from "vitest";
import {
  formatDuration,
  formatNumber,
  percentOf,
  progressionOf,
} from "./sessionReport";

describe("session report helpers (ANH-89)", () => {
  it("writes a duration in hours and minutes, minutes and seconds, or seconds", () => {
    expect(formatDuration(3_912)).toBe("1 h 05 min");
    expect(formatDuration(723)).toBe("12 min 03 s");
    expect(formatDuration(44.6)).toBe("45 s");
    expect(formatDuration(-3)).toBe("0 s");
    expect(formatDuration(null)).toBe("-");
    expect(formatDuration(undefined)).toBe("-");
    expect(formatDuration(Number.NaN)).toBe("-");
  });

  it("says a share as a whole percentage, and nothing when it cannot be said", () => {
    expect(percentOf(420, 600)).toBe(70);
    expect(percentOf(0, 600)).toBe(0);
    expect(percentOf(null, 600)).toBeNull();
    expect(percentOf(undefined, 600)).toBeNull();
    expect(percentOf(10, 0)).toBeNull();
    expect(percentOf(10, null)).toBeNull();
    expect(percentOf(10, undefined)).toBeNull();
  });

  it("writes a number in the visitor's language", () => {
    expect(formatNumber(1.23456, "fr", 2)).toBe("1,23");
    expect(formatNumber(1.23456, "en", 2)).toBe("1.23");
    expect(formatNumber(140, "en", 1)).toBe("140");
  });

  it("draws the progression oldest first, in minutes, without the sessions that have no time in zone", () => {
    const summary = (inZoneS: number | null, provisional = false) => ({
      provisional,
      durationS: 600,
      inZoneS,
    });
    expect(
      progressionOf([
        { sessionId: "c", startedAt: 3, summary: summary(450, true) },
        { sessionId: "manual", startedAt: 2, summary: summary(null) },
        { sessionId: "none", startedAt: 2, summary: null },
        { sessionId: "a", startedAt: 1, summary: summary(125) },
      ]),
    ).toEqual([
      { sessionId: "a", startedAt: 1, inZoneMin: 2.1, provisional: false },
      { sessionId: "c", startedAt: 3, inZoneMin: 7.5, provisional: true },
    ]);
  });
});

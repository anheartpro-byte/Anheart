import { enUS, fr } from "date-fns/locale";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  formatDuration,
  sessionDuration,
  type TimedSession,
} from "./sessionDuration";

/** The Duration column of the lists of sessions, for a session of each status. */

const NOW = 1_800_000_000_000;
const UNKNOWN_END = "Fin inconnue";

/** Started 20 minutes ago; `endedAt` is left out unless given. */
function started(status: string, over: Partial<TimedSession> = {}) {
  return { status, startedAt: NOW - 1_200_000, ...over };
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
});

describe("the length shown for a session", () => {
  it.each(["completed", "failed", "interrupted", "active"])(
    "a %s session with an end shows how long it lasted",
    (status) => {
      const session = started(status, { endedAt: NOW - 900_000 });

      expect(sessionDuration(session, UNKNOWN_END, fr)).toBe("5m 0s");
    },
  );

  it("an interrupted session without an end says its end is unknown, and counts no time since its start", () => {
    // Twenty minutes ago, and it keeps growing: never shown for it.
    const session = started("interrupted");

    expect(sessionDuration(session, UNKNOWN_END, fr)).toBe(UNKNOWN_END);
    vi.setSystemTime(NOW + 86_400_000);
    expect(sessionDuration(session, UNKNOWN_END, fr)).toBe(UNKNOWN_END);
  });

  it.each(["active", "pending", "aborted"])(
    "a %s session without an end counts the time since its start, on this computer's clock",
    (status) => {
      const session = started(status);

      expect(sessionDuration(session, UNKNOWN_END, fr)).toBe("20 minutes");
      vi.setSystemTime(NOW + 3_600_000);
      expect(sessionDuration(session, UNKNOWN_END, fr)).toBe("environ 1 heure");
    },
  );

  it("counts that time in the language asked for", () => {
    expect(sessionDuration(started("active"), "End unknown", enUS)).toBe(
      "20 minutes",
    );
    vi.setSystemTime(NOW + 3_600_000);
    expect(sessionDuration(started("active"), "End unknown", enUS)).toBe(
      "about 1 hour",
    );
  });
});

describe("a length in words", () => {
  it.each([
    ["under a minute", 45_999, "45s"],
    ["a minute and more", 61_000, "1m 1s"],
    ["half an hour", 1_800_000, "30m 0s"],
    ["over an hour, without its seconds", 3_725_000, "1h 2m"],
    ["nothing", 0, "0s"],
  ])("%s", (_case, ms, shown) => {
    expect(formatDuration(ms)).toBe(shown);
  });
});

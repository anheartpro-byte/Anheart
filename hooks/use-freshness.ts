import { useLocalClock } from "@/hooks/use-local-clock";
import { serverClockAt, type LocalClock } from "@/lib/server-clock";
import { isFresh, LIVE_FRESH_MS } from "@/lib/training";

export type Freshness = {
  /**
   * False once the datum is `freshMs` old on the server's clock, or absent, or
   * when the answer that brought it carries no reading of that clock.
   */
  fresh: boolean;
  /**
   * The server's clock when the verdict was made (ms since the epoch), for the
   * durations shown next to it. This computer's own clock when no answer
   * carried the server's: good enough to show a duration, never to call a
   * datum fresh.
   */
  now: number;
};

/** A verdict on one datum of an answer, at the clock reading it was made on. */
export type FreshnessJudge = (
  updatedAt: number | null | undefined,
  serverNow: number | null | undefined,
  freshMs?: number,
) => Freshness;

function judgeAt(clock: LocalClock): FreshnessJudge {
  return (updatedAt, serverNow, freshMs = LIVE_FRESH_MS) => {
    if (typeof serverNow !== "number" || !Number.isFinite(serverNow)) {
      return { fresh: false, now: clock.wall };
    }
    const now = serverClockAt(serverNow, clock);
    return { fresh: isFresh(updatedAt, now, freshMs), now };
  };
}

/**
 * The dashboard's one freshness verdict: every "live" / "stale" badge, every
 * machine status and the live view read it, so none of them can call a silent
 * machine live.
 *
 * `updatedAt` is a date the server wrote, `serverNow` the server's clock in
 * the answer that brought it. The age is `serverNow - updatedAt` plus the time
 * this page has counted since it first saw that answer: the clock of the
 * computer never dates anything, so it may be ahead or behind without effect.
 *
 * The verdict is made again every second. A machine that stops sending turns
 * stale `freshMs` after its last datum, without waiting for some other datum
 * to change; a newer answer is judged on the very render that brings it.
 */
export function useFreshness(
  updatedAt: number | null | undefined,
  serverNow: number | null | undefined,
  freshMs: number = LIVE_FRESH_MS,
): Freshness {
  return judgeAt(useLocalClock(1000))(updatedAt, serverNow, freshMs);
}

/**
 * The same verdict for several data at once (the rows of a list, or two dates
 * of one machine): one clock reading for all, so they cannot disagree.
 */
export function useFreshnessJudge(): FreshnessJudge {
  return judgeAt(useLocalClock(1000));
}

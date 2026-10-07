import { useNow } from "@/hooks/use-now";
import { isFresh, LIVE_FRESH_MS } from "@/lib/training";

export type Freshness = {
  /** False once the datum is `freshMs` old on this browser's clock, or absent. */
  fresh: boolean;
  /** The clock reading the verdict was made on (ms since the epoch). */
  now: number;
};

/**
 * The dashboard's one freshness verdict: every "live" / "stale" badge and the
 * live view read it, so none of them can call a silent machine live.
 *
 * It is re-evaluated every second on the clock. A machine that stops sending
 * therefore turns stale `freshMs` after its last datum, without waiting for
 * some other datum to change; a newer `updatedAt` is fresh on the very render
 * that brings it.
 */
export function useFreshness(
  updatedAt: number | null | undefined,
  freshMs: number = LIVE_FRESH_MS,
): Freshness {
  const now = useNow(1000);
  return { fresh: isFresh(updatedAt, now, freshMs), now };
}

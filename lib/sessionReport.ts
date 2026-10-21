/**
 * What the dashboard does with the summary of a training session (ANH-89):
 * the numbers written in the visitor's language, and the progression of a
 * rider from their history. The figures themselves are computed by Convex
 * (convex/lib/sessionSummary.ts, where each is defined); nothing here
 * computes one again.
 */

/** "1 h 05 min", "12 min 03 s", "45 s"; "-" for a duration not known. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds))
    return "-";
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h} h ${String(m).padStart(2, "0")} min`;
  if (m > 0) return `${m} min ${String(s).padStart(2, "0")} s`;
  return `${s} s`;
}

/**
 * The share of `part` in `whole` as a whole percentage, or null when it
 * cannot be said (either unknown, or nothing to share).
 */
export function percentOf(
  part: number | null | undefined,
  whole: number | null | undefined,
): number | null {
  if (part === null || part === undefined) return null;
  if (whole === null || whole === undefined || !(whole > 0)) return null;
  return Math.round((part / whole) * 100);
}

/** A number in the visitor's language, with at most `digits` decimals. */
export function formatNumber(
  value: number,
  locale: string,
  digits: number,
): string {
  return new Intl.NumberFormat(locale, {
    maximumFractionDigits: digits,
  }).format(value);
}

/** One session of a rider's history, as `sessionReports.riderHistory` lists it. */
export type HistorySession = {
  sessionId: string;
  startedAt: number;
  summary: {
    provisional: boolean;
    durationS: number | null;
    inZoneS: number | null;
  } | null;
};

/** One bar of a rider's progression. */
export type ProgressionPoint = {
  sessionId: string;
  startedAt: number;
  inZoneMin: number;
  provisional: boolean;
};

/**
 * The progression of a rider: the time in zone of each session that has
 * one, in minutes, oldest first. A session without a summary, duration, or
 * zone (a manual session), has no bar: absence is never drawn as zero.
 */
export function progressionOf(history: HistorySession[]): ProgressionPoint[] {
  const points: ProgressionPoint[] = [];
  for (const { sessionId, startedAt, summary } of history) {
    if (
      summary === null ||
      summary.durationS === null ||
      summary.inZoneS === null
    )
      continue;
    points.push({
      sessionId,
      startedAt,
      inZoneMin: Math.round((summary.inZoneS / 60) * 10) / 10,
      provisional: summary.provisional,
    });
  }
  return points.sort((a, b) => a.startedAt - b.startedAt);
}

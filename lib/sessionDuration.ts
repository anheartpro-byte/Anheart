import { formatDistanceToNow, type Locale } from "date-fns";

/** What a list of sessions knows of one session to say how long it lasted. */
export type TimedSession = {
  status: string;
  startedAt: number;
  endedAt?: number;
};

/**
 * The Duration column of the lists of sessions: the Sessions page, and the
 * recent sessions of a patient or of a user. One rule for the three.
 *
 * A session that ended shows its length. An interrupted one has had no news
 * for a while: its end is unknown (ANH-216), and no time since its start is
 * counted, as it may have stopped long ago. Any other (active, pending) shows
 * the time since its start, on this computer's clock.
 */
export function sessionDuration(
  session: TimedSession,
  unknownEnd: string,
  locale: Locale,
): string {
  if (session.endedAt !== undefined) {
    return formatDuration(session.endedAt - session.startedAt);
  }
  if (session.status === "interrupted") return unknownEnd;
  return formatDistanceToNow(session.startedAt, { locale });
}

/** A length in hours and minutes, or in minutes and seconds under an hour. */
export function formatDuration(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);

  if (hours > 0) {
    return `${hours}h ${minutes % 60}m`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds % 60}s`;
  }
  return `${seconds}s`;
}

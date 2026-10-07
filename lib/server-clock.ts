/**
 * The server's clock, as a page can tell it without trusting its own.
 *
 * Every answer that carries a datum whose age matters also carries
 * `serverNow`: the reading of the server's clock when the answer was computed.
 * The page never compares a date written by the server with the clock of the
 * computer it runs on. It only counts the time that has passed since it first
 * saw that answer, and adds it to the server's own reading. A computer whose
 * clock is minutes ahead or behind therefore shows what one on time shows.
 *
 * The time counted is the larger of two accounts: the wall clock's (it keeps
 * running while the computer sleeps, but it can be set) and the monotonic
 * clock's (it cannot be set, but it may stop while the computer sleeps).
 * Counting too much shows a datum older than it is, never younger.
 *
 * Site only: nothing here is imported by the Convex functions.
 */

/** One reading of this computer's two clocks, in milliseconds. */
export type LocalClock = {
  /** The wall clock (`Date.now()`): only ever subtracted from another reading. */
  wall: number;
  /** The monotonic clock (`performance.now()`). */
  mono: number;
};

export function readLocalClock(): LocalClock {
  return { wall: Date.now(), mono: performance.now() };
}

/**
 * When this page first saw each answer, by the answer's `serverNow`. Shared by
 * every component: an answer one of them has held for a minute is a minute old
 * for a component that mounts now, and the same answer delivered again after a
 * reconnection is not a new one.
 */
const firstSeen = new Map<number, LocalClock>();

/** Answers remembered. One on screen is looked up every second, so it stays. */
const REMEMBERED_ANSWERS = 256;

function receptionOf(serverNow: number, clock: LocalClock): LocalClock {
  const known = firstSeen.get(serverNow);
  // Put back at the end: the map forgets the answer unused for longest.
  firstSeen.delete(serverNow);
  firstSeen.set(serverNow, known ?? clock);
  if (firstSeen.size > REMEMBERED_ANSWERS) {
    const oldest = firstSeen.keys().next();
    if (!oldest.done) firstSeen.delete(oldest.value);
  }
  return known ?? clock;
}

/**
 * The server's clock at the local reading `clock`: the reading the answer
 * carried, plus the time this page has counted since it first saw that answer.
 */
export function serverClockAt(serverNow: number, clock: LocalClock): number {
  const received = receptionOf(serverNow, clock);
  const counted = Math.max(
    clock.wall - received.wall,
    clock.mono - received.mono,
    0,
  );
  return serverNow + counted;
}

/** Forget every answer seen. For tests: a page never needs it. */
export function forgetServerAnswers(): void {
  firstSeen.clear();
}

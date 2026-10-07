import { beforeEach, describe, expect, it } from "vitest";
import {
  forgetServerAnswers,
  serverClockAt,
  type LocalClock,
} from "./server-clock";

/** The server's clock when it computed the answer of these tests. */
const SERVER = 1_800_000_000_000;

/** A computer whose wall clock is `offsetMs` away from the server's. */
function computer(offsetMs: number) {
  let wall = SERVER + offsetMs;
  let mono = 4_321;
  return {
    get clock(): LocalClock {
      return { wall, mono };
    },
    /** Time passes: both clocks run. */
    run(ms: number) {
      wall += ms;
      mono += ms;
    },
    /** The computer sleeps: its monotonic clock stops, the wall clock does not. */
    sleep(ms: number) {
      wall += ms;
    },
    /** Someone, or a time service, sets the wall clock. */
    set(byMs: number) {
      wall += byMs;
    },
  };
}

beforeEach(() => {
  forgetServerAnswers();
});

describe("serverClockAt", () => {
  it("is the server's own reading when the answer is first seen", () => {
    const pc = computer(0);
    expect(serverClockAt(SERVER, pc.clock)).toBe(SERVER);
  });

  it.each([
    ["on time", 0],
    ["80 s ahead", 80_000],
    ["10 min ahead", 600_000],
    ["10 min behind", -600_000],
    ["a year behind", -365 * 86_400_000],
  ])(
    "adds the time counted since, whatever this computer's clock says (%s)",
    (_name, offsetMs) => {
      const pc = computer(offsetMs);
      expect(serverClockAt(SERVER, pc.clock)).toBe(SERVER);
      pc.run(42_000);
      expect(serverClockAt(SERVER, pc.clock)).toBe(SERVER + 42_000);
    },
  );

  it("keeps counting when the wall clock is set back", () => {
    const pc = computer(0);
    serverClockAt(SERVER, pc.clock);
    pc.run(30_000);
    pc.set(-3_600_000);
    pc.run(30_000);
    expect(serverClockAt(SERVER, pc.clock)).toBe(SERVER + 60_000);
  });

  it("counts the time the computer slept", () => {
    const pc = computer(0);
    serverClockAt(SERVER, pc.clock);
    pc.run(10_000);
    pc.sleep(600_000);
    expect(serverClockAt(SERVER, pc.clock)).toBe(SERVER + 610_000);
  });

  it("errs on the old side when the wall clock is set forward, until the next answer", () => {
    const pc = computer(-120_000);
    serverClockAt(SERVER, pc.clock);
    pc.run(5_000);
    pc.set(120_000);
    // Indistinguishable from a sleep: counted as time passed.
    expect(serverClockAt(SERVER, pc.clock)).toBe(SERVER + 125_000);
    // The next answer brings the server's clock again.
    pc.run(5_000);
    expect(serverClockAt(SERVER + 10_000, pc.clock)).toBe(SERVER + 10_000);
  });

  it("does not take an answer seen a minute ago for a new one", () => {
    // One component saw the answer; another mounts a minute later and reads
    // the same answer, or the connection delivers it again.
    const pc = computer(600_000);
    serverClockAt(SERVER, pc.clock);
    pc.run(60_000);
    expect(serverClockAt(SERVER, pc.clock)).toBe(SERVER + 60_000);
  });

  it("never runs backwards on one answer", () => {
    const pc = computer(0);
    serverClockAt(SERVER, pc.clock);
    pc.set(-5_000);
    expect(serverClockAt(SERVER, pc.clock)).toBe(SERVER);
  });

  it("remembers an answer still on screen however many others pass", () => {
    const pc = computer(0);
    serverClockAt(SERVER, pc.clock);
    for (let second = 1; second <= 2_000; second++) {
      pc.run(1_000);
      // A silent machine's answer, looked up every second...
      serverClockAt(SERVER, pc.clock);
      // ...while another query brings a new answer every second.
      serverClockAt(SERVER + 7 + second * 1_000, pc.clock);
    }
    expect(serverClockAt(SERVER, pc.clock)).toBe(SERVER + 2_000_000);
  });
});

import { describe, expect, it } from "vitest";
import {
  armRpm,
  effectiveHrMax,
  formatClock,
  formatMinutes,
  GEAR_RATIO,
  isTrainingKind,
  MIN_RIDER_AGE,
  readOptionalNumber,
  readOptionalString,
  ZONE_CEILING_FRACTION,
  zoneCeiling,
} from "./training";

/**
 * The rules the site applies before it asks the server to launch a session,
 * and the way it writes a duration and a speed. The server applies the same
 * rules again (convex/training.ts): these are the ones that decide what the
 * launch window warns of and what it lets a manager send.
 *
 * The freshness rules of the same file are in training.test.ts.
 */

/** 1 July 2026, noon UTC: a rider born in 1986 is 40. */
const NOW = Date.UTC(2026, 6, 1, 12);

describe("ANH-203 effectiveHrMax: the max heart rate a launch is vetted against", () => {
  it.each([100, 172, 220])(
    "keeps a measured value of %i bpm, whatever the birth year",
    (hrMax) => {
      expect(effectiveHrMax(hrMax, 1986, NOW)).toEqual({
        value: hrMax,
        source: "measured",
      });
      expect(effectiveHrMax(hrMax, undefined, NOW)).toEqual({
        value: hrMax,
        source: "measured",
      });
    },
  );

  it.each([99, 221, 0, -150])(
    "gives nothing for a measured value of %i bpm: it is not replaced by an estimate",
    (hrMax) => {
      // An implausible measure with a usable birth year: still nothing.
      expect(effectiveHrMax(hrMax, 1986, NOW)).toBeNull();
    },
  );

  it("estimates 208 - 0.7 x age from the birth year when nothing was measured", () => {
    expect(effectiveHrMax(undefined, 1986, NOW)).toEqual({
      value: 180,
      source: "estimated",
    });
    expect(effectiveHrMax(null, 1986, NOW)).toEqual({
      value: 180,
      source: "estimated",
    });
    // 208 - 0.7 x 33 = 184.9, rounded.
    expect(effectiveHrMax(undefined, 1993, NOW)).toEqual({
      value: 185,
      source: "estimated",
    });
  });

  it.each([
    [2016, 10, 201],
    [1926, 100, 138],
  ])(
    "estimates for a rider born in %i (%i years old)",
    (birthYear, _age, value) => {
      expect(effectiveHrMax(undefined, birthYear, NOW)).toEqual({
        value,
        source: "estimated",
      });
    },
  );

  it.each([
    [2017, "9 years old"],
    [1925, "101 years old"],
    [2030, "born in the future"],
  ])("gives nothing for a birth year of %i (%s)", (birthYear) => {
    expect(effectiveHrMax(undefined, birthYear, NOW)).toBeNull();
  });

  it.each([
    [undefined, undefined],
    [null, null],
    [undefined, null],
  ])(
    "gives nothing when neither value is known (%s, %s)",
    (hrMax, birthYear) => {
      expect(effectiveHrMax(hrMax, birthYear, NOW)).toBeNull();
    },
  );

  it("counts the age on the year of the date it is given, in UTC", () => {
    // One second before 2027 in UTC, the rider born in 2017 is still 9.
    expect(
      effectiveHrMax(undefined, 2017, Date.UTC(2026, 11, 31, 23, 59, 59)),
    ).toBeNull();
    expect(effectiveHrMax(undefined, 2017, Date.UTC(2027, 0, 1))).toEqual({
      value: 201,
      source: "estimated",
    });
  });
});

describe("ANH-203 zoneCeiling: the highest zone a rider may be given", () => {
  it("is 90 % of the max heart rate, rounded down", () => {
    expect(ZONE_CEILING_FRACTION).toBe(0.9);
    expect(zoneCeiling(180)).toBe(162);
    // 166.5 is not rounded up: a zone of 167 would be above 90 %.
    expect(zoneCeiling(185)).toBe(166);
    expect(zoneCeiling(100)).toBe(90);
  });

  it("an auto launch asks for an adult", () => {
    expect(MIN_RIDER_AGE).toBe(18);
  });
});

describe("ANH-203 speeds and kinds", () => {
  it("turns a motor speed into the speed of the arm through the gearbox", () => {
    expect(GEAR_RATIO).toBe(49.79);
    expect(armRpm(4979)).toBeCloseTo(100, 10);
    expect(armRpm(1200)).toBeCloseTo(24.1012, 4);
    expect(armRpm(0)).toBe(0);
  });

  it.each([
    ["auto", true],
    ["manual", true],
    ["recording", false],
    ["", false],
    [undefined, false],
  ])("the kind %s is a training session: %s", (kind, expected) => {
    expect(isTrainingKind(kind)).toBe(expected);
  });
});

describe("ANH-203 formatClock: an elapsed time", () => {
  it.each([
    [0, "0:00"],
    [5, "0:05"],
    [59.9, "0:59"],
    [60, "1:00"],
    [125, "2:05"],
    [3599, "59:59"],
    [3600, "1:00:00"],
    [3725, "1:02:05"],
    [36_000, "10:00:00"],
  ])("%s seconds reads %s", (seconds, text) => {
    expect(formatClock(seconds)).toBe(text);
  });

  it("never writes a negative time: a session that ran over reads 0:00 remaining", () => {
    expect(formatClock(-1)).toBe("0:00");
    expect(formatClock(-3600)).toBe("0:00");
  });
});

describe("ANH-203 formatMinutes: the length of a programme", () => {
  it.each([
    [0, "0 min"],
    [29, "0 min"],
    [30, "1 min"],
    [1800, "30 min"],
    [3540, "59 min"],
    [3600, "1 h 00"],
    [3900, "1 h 05"],
    [5400, "1 h 30"],
    [7200, "2 h 00"],
  ])("%i seconds reads %s", (seconds, text) => {
    expect(formatMinutes(seconds)).toBe(text);
  });
});

describe("ANH-203 reading a field the server may not send yet", () => {
  const record = {
    hrMax: 172,
    birthYear: "1986",
    name: "Paul",
    empty: "",
    nothing: null,
    zero: 0,
  };

  it("reads a number only when the field is there and is a number", () => {
    expect(readOptionalNumber(record, "hrMax")).toBe(172);
    expect(readOptionalNumber(record, "zero")).toBe(0);
    // A year sent as text is not taken for a number.
    expect(readOptionalNumber(record, "birthYear")).toBeUndefined();
    expect(readOptionalNumber(record, "nothing")).toBeUndefined();
    expect(readOptionalNumber(record, "absent")).toBeUndefined();
  });

  it("reads a text only when the field is there and is a text", () => {
    expect(readOptionalString(record, "name")).toBe("Paul");
    expect(readOptionalString(record, "empty")).toBe("");
    expect(readOptionalString(record, "hrMax")).toBeUndefined();
    expect(readOptionalString(record, "nothing")).toBeUndefined();
    expect(readOptionalString(record, "absent")).toBeUndefined();
  });
});

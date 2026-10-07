/// <reference types="vite/client" />
/**
 * ANH-203: `convex/training.ts` once the caller is let in.
 *
 * The role x resource matrix (`authorization.matrix.test.ts`) and the
 * isolation suite (`organizationIsolation.test.ts`) prove WHO may call. This
 * suite proves what the Convex part of the safety chain does next: each reason
 * a launch is refused, with its words and with nothing queued; what a stop
 * request writes and what it leaves alone; what a machine's report changes.
 *
 * A refusal is asserted with its exact words (or its stable code, for what a
 * machine is answered) and with what the store holds afterwards.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConvexError } from "convex/values";
import { api, internal } from "./_generated/api";
import type { Doc, Id } from "./_generated/dataModel";
import { CONTRACT_VERSION } from "./lib/contract";
import { machineHeaders } from "./machineAuth.fixtures";
import { ageFrom, effectiveHrMax, zoneRefusal } from "./training";
import {
  ANHEART_CLERK_ORG,
  addSession,
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  seedMachineWorld,
  seedWorld,
  type World,
} from "./test.setup";

beforeEach(() => {
  // The server's clock only: ages, dates and freshness are all read on it.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  configureAnheartOrganization(null);
});

const YEAR = new Date(NOW).getUTCFullYear();
/** Birth year of someone who turns `age` during the year of the fixed clock. */
const turning = (age: number) => YEAR - age;

type Physiology = { hrMax?: number; birthYear?: number };

/** The words a call is refused with, or null when it is accepted. */
async function refusalOf(call: Promise<unknown>): Promise<string | null> {
  return await call.then(
    () => null,
    (error: unknown) =>
      error instanceof Error ? error.message : String(error),
  );
}

/** The stable code and the words a machine's write is refused with. */
async function machineRefusalOf(call: Promise<unknown>): Promise<unknown> {
  return await call.then(
    () => null,
    (error: unknown) => (error instanceof ConvexError ? error.data : error),
  );
}

const rowOf = <Table extends "sessions" | "machines" | "users">(
  w: Pick<World, "t">,
  id: Id<Table>,
) => w.t.run((ctx) => ctx.db.get(id));

/** Both physiology values of an account, as the store holds them. */
async function physiologyOf(w: World, userId: Id<"users">) {
  const user = await rowOf(w, userId);
  return { hrMax: user?.hrMax, birthYear: user?.birthYear };
}

/** Write both physiology values of an account, absent ones included. */
const withPhysiology = (
  w: World,
  userId: Id<"users">,
  physiology: Physiology,
) =>
  w.t.run((ctx) =>
    ctx.db.patch(userId, {
      hrMax: physiology.hrMax,
      birthYear: physiology.birthYear,
    }),
  );

type LaunchArgs = {
  machineId: Id<"machines">;
  profileId: string;
  userId: Id<"users">;
  totalDurationS: number;
};

/** The manager launches the seeded programme on their machine for their patient. */
const launch = (w: World, changes: Partial<LaunchArgs> = {}) =>
  as(w.t, "manager").mutation(api.training.launchAutoSession, {
    machineId: w.machine,
    profileId: w.profileId,
    userId: w.patient,
    ...changes,
  });

/** What a refused launch must leave as it was: every session, and the machine. */
const queueOf = (w: World) =>
  w.t.run(async (ctx) => ({
    sessions: await ctx.db.query("sessions").collect(),
    machine: await ctx.db.get(w.machine),
  }));

/** The call is refused with exactly these words, and queues nothing. */
async function expectNothingQueued(
  w: World,
  call: () => Promise<unknown>,
  words: string,
) {
  const before = await queueOf(w);
  expect(before.sessions).toEqual([]);
  expect(await refusalOf(call())).toBe(words);
  expect(await queueOf(w)).toEqual(before);
}

/** What the machine is offered when it polls for a launch. */
const offeredTo = (w: Pick<World, "t">, machineId: Id<"machines">) =>
  w.t.query(internal.training.getPendingTrainingSession, { machineId });

/** A programme as a machine syncs it from its store. */
const PROGRAMME = {
  profileId: "p2",
  name: "Programme p2",
  totalDurationS: 900,
  zoneLowBpm: 120,
  zoneHighBpm: 140,
  hardMaxBpm: 160,
  criticalBpm: 180,
  subjectHrMax: 190,
  minRunRpm: 20,
  maxRpm: 1400,
};

const point = (t: number) => ({
  t,
  elapsedS: (t - NOW) / 1000,
  phase: "hold",
  bpm: 137,
  motorRpm: 1200,
  outputRpm: 24,
  setpointMotorRpm: 1200,
  gLoad: 1.2,
  safetyAction: "none",
});

// ---------------------------------------------------------------------------
// A launch refused: the machine, the programme, the rider, the duration
// ---------------------------------------------------------------------------

const PROGRAMMES_OFF =
  "This machine does not accept programmed sessions yet (manual only, at the machine)";
const NOT_ON_MACHINE = "This programme is not on the machine";
const PHYSIOLOGY_MISSING =
  "The rider's max heart rate (or birth year) must be set by a manager before an auto session";
const BIRTH_YEAR_MISSING =
  "The rider's birth year must be set by a manager before an auto session";

describe("a launch is refused when the machine cannot take it", () => {
  const UNFIT: Array<[string, Partial<Doc<"machines">>, string]> = [
    ["is offline", { status: "offline" }, "Machine is offline"],
    [
      "is already in a session",
      { status: "in_session" },
      "Machine is already in a session",
    ],
    ["has its programmes disabled", { programsEnabled: false }, PROGRAMMES_OFF],
    [
      "has never announced its programmes",
      { programsEnabled: undefined },
      PROGRAMMES_OFF,
    ],
  ];

  it.each(UNFIT)(
    "refuses a launch on a machine that %s",
    async (_name, state, words) => {
      const w = await seedWorld(modules);
      await w.t.run((ctx) => ctx.db.patch(w.machine, state));

      await expectNothingQueued(w, () => launch(w), words);
    },
  );

  it("refuses a launch on a deleted machine, for the rider who still holds the right on it too", async () => {
    const w = await seedWorld(modules);
    await as(w.t, "orgAdmin").mutation(api.machines.deleteMachine, {
      machineId: w.machine,
    });
    const own = { machineId: w.machine, profileId: w.profileId };

    await expectNothingQueued(w, () => launch(w), "Machine not found");
    await expectNothingQueued(
      w,
      () => as(w.t, "patient").mutation(api.training.launchAutoSession, own),
      "Machine not found",
    );
    await expectNothingQueued(
      w,
      () =>
        as(w.t, "admin").mutation(api.training.launchAutoSession, {
          ...own,
          userId: w.patient,
        }),
      "Machine not found",
    );
  });

  it("refuses the Anheart admin a launch on a machine whose row is gone", async () => {
    const w = await seedWorld(modules);
    await w.t.run((ctx) => ctx.db.delete(w.machine));

    await expectNothingQueued(
      w,
      () =>
        as(w.t, "admin").mutation(api.training.launchAutoSession, {
          machineId: w.machine,
          profileId: w.profileId,
          userId: w.patient,
        }),
      "Machine not found",
    );
  });
});

describe("a launch is refused when the machine does not hold the programme", () => {
  it("refuses a programme no machine holds, and one only another machine holds", async () => {
    const w = await seedWorld(modules);
    await w.t.mutation(internal.training.syncProfiles, {
      machineId: w.otherMachine,
      storeRev: 2,
      programsEnabled: true,
      profiles: [PROGRAMME],
    });

    await expectNothingQueued(
      w,
      () => launch(w, { profileId: "p-unknown" }),
      NOT_ON_MACHINE,
    );
    await expectNothingQueued(
      w,
      () => launch(w, { profileId: PROGRAMME.profileId }),
      NOT_ON_MACHINE,
    );
  });

  it("follows the machine's store: a withdrawn programme is refused, the new one is queued with its own zone", async () => {
    const w = await seedWorld(modules);
    expect(
      await w.t.mutation(internal.training.syncProfiles, {
        machineId: w.machine,
        storeRev: 2,
        programsEnabled: true,
        profiles: [PROGRAMME],
      }),
    ).toEqual({ count: 1 });

    await expectNothingQueued(w, () => launch(w), NOT_ON_MACHINE);
    const sessionId = await launch(w, { profileId: PROGRAMME.profileId });

    expect(await rowOf(w, sessionId)).toMatchObject({
      status: "pending",
      profileId: "p2",
      profileName: "Programme p2",
      zoneLowBpm: 120,
      zoneHighBpm: 140,
      totalDurationS: 900,
    });
  });
});

describe("a launch is refused when the rider's physiology does not allow the programme", () => {
  // The seeded programme: zone up to 150 bpm, hard maximum 170 bpm.
  const UNFIT: Array<[string, Physiology, string]> = [
    ["has neither a max heart rate nor a birth year", {}, PHYSIOLOGY_MISSING],
    [
      "has a stored max heart rate below the plausible range, whatever their birth year",
      { hrMax: 99, birthYear: turning(37) },
      PHYSIOLOGY_MISSING,
    ],
    [
      "has a stored max heart rate above the plausible range, whatever their birth year",
      { hrMax: 221, birthYear: turning(37) },
      PHYSIOLOGY_MISSING,
    ],
    [
      "is too young for an estimated max heart rate (turns 9)",
      { birthYear: turning(9) },
      PHYSIOLOGY_MISSING,
    ],
    [
      "is too old for an estimated max heart rate (turns 101)",
      { birthYear: turning(101) },
      PHYSIOLOGY_MISSING,
    ],
    [
      "has a max heart rate but no birth year",
      { hrMax: 180 },
      BIRTH_YEAR_MISSING,
    ],
    [
      "has a max heart rate whose 90% is below the top of the zone",
      { hrMax: 160, birthYear: turning(37) },
      "Zone up to 150 bpm exceeds 90% of this rider's max heart rate (160 bpm → ceiling 144 bpm)",
    ],
    [
      "has a max heart rate below the programme's hard maximum",
      { hrMax: 168, birthYear: turning(37) },
      "Programme hard maximum 170 bpm is above this rider's max heart rate (168 bpm)",
    ],
    [
      "is 16",
      { hrMax: 180, birthYear: turning(17) },
      "Rider is 16: auto sessions require at least 18 years",
    ],
    [
      "only turns 18 this year (the later birthday is assumed)",
      { hrMax: 180, birthYear: turning(18) },
      "Rider is 17: auto sessions require at least 18 years",
    ],
    [
      "is a minor whose max heart rate is only estimated",
      { birthYear: turning(17) },
      "Rider is 16: auto sessions require at least 18 years",
    ],
  ];

  it.each(UNFIT)(
    "refuses a launch for a rider who %s",
    async (_name, physiology, words) => {
      const w = await seedWorld(modules);
      await withPhysiology(w, w.patient, physiology);

      await expectNothingQueued(w, () => launch(w), words);
      // The rider is refused the same way when launching for themselves.
      await expectNothingQueued(
        w,
        () =>
          as(w.t, "patient").mutation(api.training.launchAutoSession, {
            machineId: w.machine,
            profileId: w.profileId,
          }),
        words,
      );
    },
  );

  it("refuses a launch for a rider whose account no longer exists", async () => {
    const w = await seedWorld(modules);
    // The account row is gone; its membership and its link to the manager stay.
    await w.t.run((ctx) => ctx.db.delete(w.patient));

    await expectNothingQueued(w, () => launch(w), "Rider not found");
  });

  // ANH-205. A value on record counts only as a finite whole number. Any
  // other one is read as not set: it is compared with nothing, and a max
  // heart rate that cannot be used is not replaced by the estimate.
  const NOT_WHOLE: Array<[string, number]> = [
    ["not a number", Number.NaN],
    ["infinite", Number.POSITIVE_INFINITY],
    ["infinite and negative", Number.NEGATIVE_INFINITY],
  ];
  const UNUSABLE_ON_RECORD: Array<[string, Physiology, string]> = [
    ...NOT_WHOLE.concat([["a fraction of a year", 1990.5]]).flatMap(
      ([what, birthYear]): Array<[string, Physiology, string]> => [
        [
          `a birth year on record that is ${what}, and a measured max heart rate`,
          { hrMax: 180, birthYear },
          BIRTH_YEAR_MISSING,
        ],
        [
          `a birth year on record that is ${what}, and no max heart rate`,
          { birthYear },
          PHYSIOLOGY_MISSING,
        ],
      ],
    ),
    ...NOT_WHOLE.concat([["a fraction of a beat", 180.5]]).flatMap(
      ([what, hrMax]): Array<[string, Physiology, string]> => [
        [
          `a max heart rate on record that is ${what}, whatever their birth year`,
          { hrMax, birthYear: turning(37) },
          PHYSIOLOGY_MISSING,
        ],
        [
          `a max heart rate on record that is ${what}, and no birth year`,
          { hrMax },
          PHYSIOLOGY_MISSING,
        ],
      ],
    ),
  ];

  it.each(UNUSABLE_ON_RECORD)(
    "refuses a launch for a rider with %s",
    async (_name, physiology, words) => {
      const w = await seedWorld(modules);
      await withPhysiology(w, w.patient, physiology);

      await expectNothingQueued(w, () => launch(w), words);
      // The rider is refused the same way when launching for themselves.
      await expectNothingQueued(
        w,
        () =>
          as(w.t, "patient").mutation(api.training.launchAutoSession, {
            machineId: w.machine,
            profileId: w.profileId,
          }),
        words,
      );
      // The machine is offered nothing.
      expect(await offeredTo(w, w.machine)).toBeNull();
    },
  );

  it("queues the estimated max heart rate when only the birth year is known, and offers it to the machine", async () => {
    const w = await seedWorld(modules);
    await withPhysiology(w, w.patient, { birthYear: turning(37) });

    const sessionId = await launch(w);

    // Tanaka: 208 - 0.7 x 37 = 182.1.
    expect(await rowOf(w, sessionId)).toMatchObject({
      organizationId: w.orgA,
      machineId: w.machine,
      userId: w.patient,
      startedById: w.manager,
      status: "pending",
      startedAt: NOW,
      kind: "auto",
      origin: "remote",
      profileId: "p1",
      profileName: "Programme p1",
      zoneLowBpm: 130,
      zoneHighBpm: 150,
      totalDurationS: 600,
      subjectHrMax: 182,
      subjectAge: 36,
      subjectLabel: "patient Synthetic",
      operatorName: "manager Synthetic",
    });
    expect(await offeredTo(w, w.machine)).toEqual({
      sessionId,
      profileId: "p1",
      totalDurationS: 600,
      subjectId: w.patient,
      subjectLabel: "patient Synthetic",
      subjectHrMax: 182,
      subjectAge: 36,
      operatorName: "manager Synthetic",
    });
    // Queuing a launch does not put the machine in session: the machine does.
    expect((await rowOf(w, w.machine))?.status).toBe("online");
  });

  it("accepts a rider who is 18 by the later birthday, with the measured max heart rate rather than the estimate", async () => {
    const w = await seedWorld(modules);
    await withPhysiology(w, w.patient, { hrMax: 180, birthYear: turning(19) });

    const sessionId = await launch(w);

    expect(await rowOf(w, sessionId)).toMatchObject({
      status: "pending",
      subjectHrMax: 180,
      subjectAge: 18,
    });
  });
});

describe("a launch is refused when the duration asked for is not a positive number", () => {
  it.each([
    0,
    -1,
    Number.NaN,
    Number.POSITIVE_INFINITY,
    Number.NEGATIVE_INFINITY,
  ])("refuses a duration of %s seconds", async (totalDurationS) => {
    const w = await seedWorld(modules);

    await expectNothingQueued(
      w,
      () => launch(w, { totalDurationS }),
      "Duration must be positive",
    );
  });

  it("queues the duration asked for in place of the programme's, and offers it to the machine", async () => {
    const w = await seedWorld(modules);

    const sessionId = await launch(w, { totalDurationS: 300 });

    expect((await rowOf(w, sessionId))?.totalDurationS).toBe(300);
    expect(await offeredTo(w, w.machine)).toMatchObject({
      sessionId,
      totalDurationS: 300,
    });
  });
});

// ---------------------------------------------------------------------------
// The physiology rules themselves
// ---------------------------------------------------------------------------

describe("the physiology rules mirrored from the machine (raspberry-pi/src/training/plan.py)", () => {
  it.each<[Physiology, number | null]>([
    [{ hrMax: 100 }, 100],
    [{ hrMax: 220 }, 220],
    [{ hrMax: 99.9 }, null],
    [{ hrMax: 220.1 }, null],
    // A measured value wins over the estimate...
    [{ hrMax: 150, birthYear: turning(37) }, 150],
    // ...and an implausible one is not replaced by it.
    [{ hrMax: 99, birthYear: turning(37) }, null],
    // Tanaka, 208 - 0.7 x age, for an age of 10 to 100.
    [{ birthYear: turning(10) }, 201],
    [{ birthYear: turning(37) }, 182],
    [{ birthYear: turning(100) }, 138],
    [{ birthYear: turning(9) }, null],
    [{ birthYear: turning(101) }, null],
    [{}, null],
    // ANH-205: only a finite whole number is a value. A measured one that is
    // not gives nothing, with a usable birth year too...
    [{ hrMax: Number.NaN }, null],
    [{ hrMax: Number.NaN, birthYear: turning(37) }, null],
    [{ hrMax: Number.POSITIVE_INFINITY, birthYear: turning(37) }, null],
    [{ hrMax: Number.NEGATIVE_INFINITY, birthYear: turning(37) }, null],
    [{ hrMax: 150.5 }, null],
    [{ hrMax: 150.5, birthYear: turning(37) }, null],
    // ...and no estimate is made from a birth year that is not one.
    [{ birthYear: Number.NaN }, null],
    [{ birthYear: Number.POSITIVE_INFINITY }, null],
    [{ birthYear: Number.NEGATIVE_INFINITY }, null],
    [{ birthYear: turning(37) + 0.5 }, null],
  ])("effectiveHrMax(%o) is %s", (physiology, expected) => {
    expect(effectiveHrMax(physiology, NOW)).toBe(expected);
  });

  it("refuses a zone above 90% of the max heart rate, rounded down, then a hard maximum above it", () => {
    // 90% of 181 bpm is 162.9: the ceiling is 162, never 163.
    expect(zoneRefusal({ zoneHighBpm: 162, hardMaxBpm: 181 }, 181)).toBeNull();
    expect(zoneRefusal({ zoneHighBpm: 163, hardMaxBpm: 181 }, 181)).toBe(
      "Zone up to 163 bpm exceeds 90% of this rider's max heart rate (181 bpm → ceiling 162 bpm)",
    );
    expect(zoneRefusal({ zoneHighBpm: 162, hardMaxBpm: 182 }, 181)).toBe(
      "Programme hard maximum 182 bpm is above this rider's max heart rate (181 bpm)",
    );
    // Both at once: the zone is told first.
    expect(zoneRefusal({ zoneHighBpm: 163, hardMaxBpm: 182 }, 181)).toMatch(
      /^Zone up to 163 bpm/,
    );
  });

  it("ANH-205: refuses a zone when the max heart rate, the top of the zone or the hard maximum is not a number", () => {
    const fits = { zoneHighBpm: 162, hardMaxBpm: 181 };
    expect(zoneRefusal(fits, 181)).toBeNull();

    expect(zoneRefusal(fits, Number.NaN)).toMatch(/^Zone up to 162 bpm/);
    expect(zoneRefusal({ ...fits, zoneHighBpm: Number.NaN }, 181)).toMatch(
      /^Zone up to NaN bpm/,
    );
    expect(zoneRefusal({ ...fits, hardMaxBpm: Number.NaN }, 181)).toMatch(
      /^Programme hard maximum NaN bpm/,
    );
  });

  it.each([
    Number.NaN,
    Number.POSITIVE_INFINITY,
    Number.NEGATIVE_INFINITY,
    turning(37) + 0.5,
  ])("ANH-205: counts no age from a birth year of %s", (birthYear) => {
    expect(ageFrom(birthYear, NOW)).toBeNull();
  });

  it("counts an age in whole years on the UTC year, the later birthday assumed", () => {
    const lastInstantOfLastYear = Date.UTC(YEAR, 0, 1) - 1;

    expect(ageFrom(turning(37), NOW)).toBe(36);
    expect(ageFrom(turning(37), lastInstantOfLastYear)).toBe(35);
    expect(ageFrom(undefined, NOW)).toBeNull();
    // The estimate reads the same year.
    expect(effectiveHrMax({ birthYear: turning(37) }, NOW)).toBe(182);
    expect(
      effectiveHrMax({ birthYear: turning(37) }, lastInstantOfLastYear),
    ).toBe(183);
  });
});

// ---------------------------------------------------------------------------
// The physiology a manager sets
// ---------------------------------------------------------------------------

describe("the physiology a manager sets for a rider", () => {
  const SEEDED = { hrMax: 180, birthYear: 1990 };
  const set = (
    w: World,
    values: { hrMax?: number | null; birthYear?: number | null },
  ) =>
    as(w.t, "manager").mutation(api.training.setUserPhysiology, {
      userId: w.patient,
      ...values,
    });

  it.each([99, 221, 0, -150])(
    "refuses a max heart rate of %s bpm and keeps the stored one",
    async (hrMax) => {
      const w = await seedWorld(modules);

      expect(await refusalOf(set(w, { hrMax }))).toBe(
        "Max heart rate must be within 100-220 bpm",
      );
      expect(await physiologyOf(w, w.patient)).toEqual(SEEDED);
    },
  );

  // ANH-205. A whole number first, then the range: a value that is not a
  // finite whole number is refused as such, in the range or out of it.
  it.each([
    Number.NaN,
    Number.POSITIVE_INFINITY,
    Number.NEGATIVE_INFINITY,
    150.5,
    99.5,
    220.5,
  ])(
    "requires a whole number of bpm: refuses a max heart rate of %s and keeps the stored one",
    async (hrMax) => {
      const w = await seedWorld(modules);

      expect(await refusalOf(set(w, { hrMax }))).toBe(
        "Max heart rate must be a whole number of bpm",
      );
      expect(await physiologyOf(w, w.patient)).toEqual(SEEDED);
    },
  );

  it.each([
    Number.NaN,
    Number.POSITIVE_INFINITY,
    Number.NEGATIVE_INFINITY,
    1990.5,
    turning(9) + 0.5,
    turning(101) - 0.5,
  ])(
    "requires a whole year: refuses a birth year of %s and keeps the stored one",
    async (birthYear) => {
      const w = await seedWorld(modules);

      expect(await refusalOf(set(w, { birthYear }))).toBe(
        "Birth year must be a whole number",
      );
      expect(await physiologyOf(w, w.patient)).toEqual(SEEDED);
    },
  );

  it.each<[string, unknown]>([
    ["a text", "180"],
    ["a 64-bit integer", BigInt(180)],
    ["a boolean", true],
    ["a list", [180]],
  ])(
    "takes a number or null for each value: %s is refused before anything is read or written",
    async (_name, value) => {
      const w = await seedWorld(modules);
      const given = value as number;

      expect(await refusalOf(set(w, { hrMax: given }))).toMatch(/Validator/);
      expect(await refusalOf(set(w, { birthYear: given }))).toMatch(
        /Validator/,
      );
      expect(await physiologyOf(w, w.patient)).toEqual(SEEDED);
    },
  );

  it.each([100, 220])(
    "accepts a max heart rate of %s bpm, a bound of the plausible range",
    async (hrMax) => {
      const w = await seedWorld(modules);

      expect(await set(w, { hrMax })).toBeNull();
      expect(await physiologyOf(w, w.patient)).toEqual({ ...SEEDED, hrMax });
    },
  );

  it.each([
    ["turns 9", turning(9)],
    ["is born next year", turning(-1)],
    ["turns 101", turning(101)],
  ])(
    "refuses the birth year of someone who %s and keeps the stored one",
    async (_name, birthYear) => {
      const w = await seedWorld(modules);

      expect(await refusalOf(set(w, { birthYear }))).toBe(
        "Birth year gives an implausible age",
      );
      expect(await physiologyOf(w, w.patient)).toEqual(SEEDED);
    },
  );

  it.each([
    ["turns 10", turning(10)],
    ["turns 100", turning(100)],
  ])("accepts the birth year of someone who %s", async (_name, birthYear) => {
    const w = await seedWorld(modules);

    expect(await set(w, { birthYear })).toBeNull();
    expect(await physiologyOf(w, w.patient)).toEqual({
      ...SEEDED,
      birthYear,
    });
  });

  it("writes neither value when one of the two is refused", async () => {
    const w = await seedWorld(modules);

    expect(await refusalOf(set(w, { hrMax: 150, birthYear: turning(9) }))).toBe(
      "Birth year gives an implausible age",
    );
    expect(await refusalOf(set(w, { hrMax: 300, birthYear: 1985 }))).toBe(
      "Max heart rate must be within 100-220 bpm",
    );
    expect(await physiologyOf(w, w.patient)).toEqual(SEEDED);
    // ANH-205: the same when one of the two is not a whole number.
    expect(await refusalOf(set(w, { hrMax: 150, birthYear: Number.NaN }))).toBe(
      "Birth year must be a whole number",
    );
    expect(
      await refusalOf(set(w, { hrMax: Number.NaN, birthYear: 1985 })),
    ).toBe("Max heart rate must be a whole number of bpm");
    expect(await physiologyOf(w, w.patient)).toEqual(SEEDED);
  });

  it("changes only the value it is given", async () => {
    const w = await seedWorld(modules);

    await set(w, { hrMax: 175 });
    expect(await physiologyOf(w, w.patient)).toEqual({
      hrMax: 175,
      birthYear: 1990,
    });
    await set(w, { birthYear: 1985 });
    expect(await physiologyOf(w, w.patient)).toEqual({
      hrMax: 175,
      birthYear: 1985,
    });
    await set(w, {});
    expect(await physiologyOf(w, w.patient)).toEqual({
      hrMax: 175,
      birthYear: 1985,
    });
  });

  it("clears a value given as null: the launch falls back on the estimate, then is refused", async () => {
    const w = await seedWorld(modules);

    await set(w, { hrMax: null });
    expect(await physiologyOf(w, w.patient)).toEqual({
      hrMax: undefined,
      birthYear: 1990,
    });
    const estimated = await launch(w);
    expect((await rowOf(w, estimated))?.subjectHrMax).toBe(182);
    await as(w.t, "manager").mutation(api.training.requestStop, {
      sessionId: estimated,
    });

    await set(w, { birthYear: null });
    expect(await physiologyOf(w, w.patient)).toEqual({
      hrMax: undefined,
      birthYear: undefined,
    });
    expect(await refusalOf(launch(w))).toBe(PHYSIOLOGY_MISSING);
  });
});

// ---------------------------------------------------------------------------
// Launch rights
// ---------------------------------------------------------------------------

describe("launch rights are granted once and revoked quietly", () => {
  const rightsOn = (w: World, machineId: Id<"machines">) =>
    w.t.run((ctx) =>
      ctx.db
        .query("machine_user_permissions")
        .withIndex("by_machine", (q) => q.eq("machineId", machineId))
        .collect(),
    );

  it("keeps the first grant when the right is granted again", async () => {
    const w = await seedWorld(modules);
    const before = await rightsOn(w, w.machine);
    vi.setSystemTime(NOW + 60_000);

    // The patient already holds the right, granted by their manager at NOW.
    expect(
      await as(w.t, "orgAdmin").mutation(api.training.grantLaunchRight, {
        machineId: w.machine,
        userId: w.patient,
      }),
    ).toBeNull();
    expect(await rightsOn(w, w.machine)).toEqual(before);
    expect(before).toMatchObject([
      { userId: w.patient, grantedBy: w.manager, createdAt: NOW },
    ]);

    // A first grant is written, dated and attributed to whoever granted it.
    await as(w.t, "orgAdmin").mutation(api.training.grantLaunchRight, {
      machineId: w.machine,
      userId: w.stranger,
    });
    expect(await rightsOn(w, w.machine)).toMatchObject([
      { userId: w.patient, grantedBy: w.manager, createdAt: NOW },
      {
        organizationId: w.orgA,
        userId: w.stranger,
        grantedBy: w.orgAdmin,
        createdAt: NOW + 60_000,
      },
    ]);
  });

  it("revokes nothing for a user who holds no right, and a revoked rider can no longer launch", async () => {
    const w = await seedWorld(modules);
    const everyRight = () =>
      w.t.run((ctx) => ctx.db.query("machine_user_permissions").collect());
    const before = await everyRight();
    const revoke = (userId: Id<"users">) =>
      as(w.t, "manager").mutation(api.training.revokeLaunchRight, {
        machineId: w.machine,
        userId,
      });

    expect(await revoke(w.stranger)).toBeNull();
    expect(await everyRight()).toEqual(before);

    // The patient's right is removed; removing it again is quiet too.
    expect(await revoke(w.patient)).toBeNull();
    expect(await revoke(w.patient)).toBeNull();
    expect(await rightsOn(w, w.machine)).toEqual([]);
    // The rights on the other machines are untouched.
    expect(await everyRight()).toEqual(
      before.filter((right) => right.machineId !== w.machine),
    );
    await expectNothingQueued(
      w,
      () =>
        as(w.t, "patient").mutation(api.training.launchAutoSession, {
          machineId: w.machine,
          profileId: w.profileId,
        }),
      "You have not been given the right to launch sessions on this machine",
    );
  });

  it("lists each rider with the max heart rate a launch would use", async () => {
    const w = await seedWorld(modules);
    await withPhysiology(w, w.stranger, { birthYear: turning(37) });
    await withPhysiology(w, w.otherPatient, {});
    await w.t.run(async (ctx) => {
      for (const userId of [w.stranger, w.otherPatient]) {
        await ctx.db.insert("machine_user_permissions", {
          organizationId: w.orgA,
          machineId: w.machine,
          userId,
          grantedBy: w.manager,
          createdAt: NOW,
        });
      }
    });

    const listed = await as(w.t, "manager").query(
      api.training.listLaunchRights,
      { machineId: w.machine },
    );

    expect(
      Object.fromEntries(listed.map((right) => [right.userId, right.hrMax])),
    ).toEqual({
      [w.patient]: 180, // measured
      [w.stranger]: 182, // estimated from the birth year
      [w.otherPatient]: null, // unknown
    });
  });

  it("serves the machine its riders by name, each with the max heart rate a launch would use", async () => {
    const w = await seedWorld(modules);
    await withPhysiology(w, w.stranger, { birthYear: turning(37) });
    await withPhysiology(w, w.otherPatient, {});
    await w.t.run(async (ctx) => {
      // Granted after the patient's right: the order of the rights is not
      // the order of the names.
      for (const userId of [w.stranger, w.otherPatient]) {
        await ctx.db.insert("machine_user_permissions", {
          organizationId: w.orgA,
          machineId: w.machine,
          userId,
          grantedBy: w.manager,
          createdAt: NOW,
        });
      }
    });

    expect(
      await w.t.query(internal.training.getRoster, { machineId: w.machine }),
    ).toEqual([
      { userId: w.otherPatient, name: "otherPatient Synthetic", hrMax: null },
      { userId: w.patient, name: "patient Synthetic", hrMax: 180 },
      { userId: w.stranger, name: "stranger Synthetic", hrMax: 182 },
    ]);
  });

  it.each<[string, Physiology]>([
    ["a birth year that is not a number", { birthYear: Number.NaN }],
    ["a fraction of a year as birth year", { birthYear: turning(37) + 0.5 }],
    [
      "a max heart rate that is not a number",
      { hrMax: Number.NaN, birthYear: turning(37) },
    ],
    [
      "a fraction of a beat as max heart rate",
      { hrMax: 180.5, birthYear: turning(37) },
    ],
  ])(
    "ANH-205: gives no max heart rate, wherever one is shown or served, for a rider with %s on record",
    async (_name, physiology) => {
      const w = await seedWorld(modules);
      await withPhysiology(w, w.patient, physiology);

      // The manager's list of rights, and the manager's view of the account.
      const listed = await as(w.t, "manager").query(
        api.training.listLaunchRights,
        { machineId: w.machine },
      );
      expect(listed.map((right) => [right.userId, right.hrMax])).toEqual([
        [w.patient, null],
      ]);
      expect(
        await as(w.t, "manager").query(api.users.getUserById, {
          userId: w.patient,
        }),
      ).toMatchObject({ effectiveHrMax: null });
      // The rider's own machines.
      const mine = await as(w.t, "patient").query(
        api.training.listLaunchableMachines,
        {},
      );
      expect(mine.map((machine) => [machine._id, machine.myHrMax])).toEqual([
        [w.machine, null],
      ]);
      // The machine's list of riders.
      expect(
        await w.t.query(internal.training.getRoster, { machineId: w.machine }),
      ).toEqual([
        { userId: w.patient, name: "patient Synthetic", hrMax: null },
      ]);
    },
  );

  it("leaves out a right whose account no longer exists", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      await ctx.db.insert("machine_user_permissions", {
        organizationId: w.orgA,
        machineId: w.machine,
        userId: w.stranger,
        grantedBy: w.manager,
        createdAt: NOW,
      });
      await ctx.db.delete(w.stranger);
    });

    const listed = await as(w.t, "manager").query(
      api.training.listLaunchRights,
      { machineId: w.machine },
    );

    expect(listed.map((right) => right.userId)).toEqual([w.patient]);
  });

  it("still lists a right whose granter's account was deleted, naming nobody", async () => {
    const w = await seedWorld(modules);
    await as(w.t, "orgAdmin").mutation(api.users.deleteUser, {
      userId: w.manager,
    });

    const listed = await as(w.t, "orgAdmin").query(
      api.training.listLaunchRights,
      { machineId: w.machine },
    );

    expect(listed).toEqual([
      {
        userId: w.patient,
        name: "patient Synthetic",
        email: "patient@example.invalid",
        hrMax: 180,
        grantedByName: "-",
        createdAt: NOW,
      },
    ]);
  });
});

// ---------------------------------------------------------------------------
// The stop request
// ---------------------------------------------------------------------------

const machineWorld = () => seedMachineWorld(modules);
type MachineWorld = Awaited<ReturnType<typeof machineWorld>>;

/** A request of the first machine, with its key and the contract it speaks. */
function send(
  w: MachineWorld,
  method: "GET" | "POST",
  path: string,
  body?: unknown,
) {
  return w.t.fetch(path, {
    method,
    headers: {
      ...machineHeaders(w.machineKey),
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

/** The rider of the machine world, acting in their organisation. */
const riderOf = (w: MachineWorld) =>
  w.t.withIdentity({
    subject: "http-patient",
    org_id: ANHEART_CLERK_ORG,
    org_role: "org:patient",
  });

describe("the stop request", () => {
  const stop = (
    w: World,
    actor: "manager" | "patient",
    sessionId: Id<"sessions">,
  ) => as(w.t, actor).mutation(api.training.requestStop, { sessionId });

  it("reaches the machine through its status route, and the session ends only when the machine reports it", async () => {
    const w = await machineWorld();
    const statusOf = async (sessionId: Id<"sessions">) =>
      (await (
        await send(
          w,
          "GET",
          `/api/machine/training/status?sessionId=${sessionId}`,
        )
      ).json()) as unknown;

    // Launched from the dashboard, picked up and armed by the machine.
    const sessionId = await w.admin.mutation(api.training.launchAutoSession, {
      machineId: w.machine,
      profileId: w.profileId,
      userId: w.patient,
    });
    const started = await send(w, "POST", "/api/machine/training/start", {
      sessionId,
    });
    expect(started.status).toBe(200);
    expect(await statusOf(sessionId)).toEqual({
      status: "active",
      active: true,
      stopRequested: false,
      server_contract_version: CONTRACT_VERSION,
    });

    // The rider asks for the stop 30 s into the session.
    vi.setSystemTime(NOW + 30_000);
    expect(
      await riderOf(w).mutation(api.training.requestStop, { sessionId }),
    ).toBeNull();

    // The arm is still turning: the session stays active, the machine in session.
    const asked = await rowOf(w, sessionId);
    expect(asked).toMatchObject({
      status: "active",
      stopRequestedAt: NOW + 30_000,
    });
    expect(asked?.endedAt).toBeUndefined();
    expect(asked?.endReason).toBeUndefined();
    expect((await rowOf(w, w.machine))?.status).toBe("in_session");
    expect(await statusOf(sessionId)).toEqual({
      status: "active",
      active: true,
      stopRequested: true,
      server_contract_version: CONTRACT_VERSION,
    });

    // The machine has ramped down: it reports the end, dated on its own clock.
    vi.setSystemTime(NOW + 50_000);
    const ended = await send(w, "POST", "/api/machine/training/end", {
      sessionId,
      failed: false,
      reason: "stop_requested",
      endedAt: NOW + 48_000,
    });
    expect(ended.status).toBe(200);
    expect(await rowOf(w, sessionId)).toMatchObject({
      status: "completed",
      stopRequestedAt: NOW + 30_000,
      endedAt: NOW + 48_000,
      endReason: "stop_requested",
    });
    expect((await rowOf(w, w.machine))?.status).toBe("online");
    expect(await statusOf(sessionId)).toEqual({
      status: "completed",
      active: false,
      stopRequested: true,
      server_contract_version: CONTRACT_VERSION,
    });
  });

  it("keeps the date of the first stop request when the stop is asked again", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      kind: "auto",
    });

    vi.setSystemTime(NOW + 5000);
    await stop(w, "patient", sessionId);
    vi.setSystemTime(NOW + 9000);
    expect(await stop(w, "manager", sessionId)).toBeNull();

    expect(await rowOf(w, sessionId)).toMatchObject({
      status: "active",
      stopRequestedAt: NOW + 5000,
    });
    expect(
      await w.t.query(internal.training.getTrainingStatus, {
        machineId: w.machine,
        sessionId,
      }),
    ).toEqual({ status: "active", active: true, stopRequested: true });
  });

  it.each(["completed", "failed"] as const)(
    "changes nothing on a session that has already ended (%s)",
    async (status) => {
      const w = await seedWorld(modules);
      const sessionId = await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status,
        kind: "auto",
      });
      const before = await queueOf(w);

      vi.setSystemTime(NOW + 5000);
      expect(await stop(w, "manager", sessionId)).toBeNull();

      // No stop request is dated on it, and the machine is not told to stop.
      expect(await queueOf(w)).toEqual(before);
      expect(
        await w.t.query(internal.training.getTrainingStatus, {
          machineId: w.machine,
          sessionId,
        }),
      ).toEqual({ status, active: false, stopRequested: false });
    },
  );

  it("cancels a pending launch: the machine is offered nothing and cannot arm it afterwards", async () => {
    const w = await seedWorld(modules);
    const sessionId = await as(w.t, "patient").mutation(
      api.training.launchAutoSession,
      { machineId: w.machine, profileId: w.profileId },
    );
    expect((await offeredTo(w, w.machine))?.sessionId).toBe(sessionId);

    vi.setSystemTime(NOW + 10_000);
    expect(await stop(w, "patient", sessionId)).toBeNull();

    const cancelled = await rowOf(w, sessionId);
    expect(cancelled).toMatchObject({
      status: "failed",
      endedAt: NOW + 10_000,
      endReason: "Cancelled before start by patient Synthetic",
    });
    // A cancellation is not a stop request: nothing was turning.
    expect(cancelled?.stopRequestedAt).toBeUndefined();
    expect(await offeredTo(w, w.machine)).toBeNull();

    // A machine that had already read the launch is refused when it arms it.
    expect(
      await machineRefusalOf(
        w.t.mutation(internal.training.markTrainingStarted, {
          machineId: w.machine,
          sessionId,
        }),
      ),
    ).toEqual({
      code: "session_not_pending",
      message: "Session is not pending (status: failed)",
    });
    expect(await rowOf(w, sessionId)).toEqual(cancelled);
    expect((await rowOf(w, w.machine))?.status).toBe("online");

    // The cancelled launch no longer holds the machine: another one is queued.
    const next = await launch(w);
    expect((await offeredTo(w, w.machine))?.sessionId).toBe(next);
  });

  it("records the machine's refusal of a launch as a failed session, which no later report rewrites", async () => {
    const w = await seedWorld(modules);
    const sessionId = await launch(w);

    vi.setSystemTime(NOW + 3000);
    await w.t.mutation(internal.training.endTrainingSession, {
      machineId: w.machine,
      sessionId,
      failed: true,
      reason: "estop_not_attested",
    });

    // No date from the machine: the end is dated on the server's clock.
    const refused = await rowOf(w, sessionId);
    expect(refused).toMatchObject({
      status: "failed",
      endedAt: NOW + 3000,
      endReason: "estop_not_attested",
    });
    expect(await offeredTo(w, w.machine)).toBeNull();

    // The machine is in another session by the time a late report arrives.
    await w.t.run((ctx) => ctx.db.patch(w.machine, { status: "in_session" }));
    vi.setSystemTime(NOW + 9000);
    expect(
      await w.t.mutation(internal.training.endTrainingSession, {
        machineId: w.machine,
        sessionId,
        failed: false,
        reason: "programme_complete",
        endedAt: NOW + 8000,
      }),
    ).toBeNull();
    expect(await rowOf(w, sessionId)).toEqual(refused);
    expect((await rowOf(w, w.machine))?.status).toBe("in_session");
  });

  it("offers the stop only while a session is pending or active", async () => {
    const w = await seedWorld(modules);
    const canStop: Record<string, boolean | undefined> = {};
    for (const status of [
      "pending",
      "active",
      "completed",
      "failed",
    ] as const) {
      const sessionId = await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status,
        kind: "auto",
      });
      canStop[status] = (
        await as(w.t, "patient").query(api.training.getTrainingSession, {
          sessionId,
        })
      )?.canStop;
    }

    expect(canStop).toEqual({
      pending: true,
      active: true,
      completed: false,
      failed: false,
    });
  });
});

// ---------------------------------------------------------------------------
// What the dashboard is shown
// ---------------------------------------------------------------------------

describe("what the dashboard is shown about machines and sessions", () => {
  const launchable = (
    w: World,
    actor: "manager" | "patient" | "otherPatient",
  ) => as(w.t, actor).query(api.training.listLaunchableMachines, {});

  it("no longer offers a deleted machine to those who could launch on it", async () => {
    const w = await seedWorld(modules);
    expect((await launchable(w, "patient")).map((m) => m._id)).toEqual([
      w.machine,
    ]);

    // Deleted from the dashboard: the row stays, marked; the right stays too.
    await as(w.t, "orgAdmin").mutation(api.machines.deleteMachine, {
      machineId: w.machine,
    });
    // A row removed outright while a right still points to it.
    await w.t.run((ctx) => ctx.db.delete(w.otherMachine));

    expect(await launchable(w, "patient")).toEqual([]);
    expect(await launchable(w, "manager")).toEqual([]);
    expect(await launchable(w, "otherPatient")).toEqual([]);
  });

  it("reports programmes as disabled until the machine has announced them", async () => {
    const w = await seedWorld(modules);
    await w.t.run((ctx) =>
      ctx.db.patch(w.machine, { programsEnabled: undefined }),
    );

    expect(await launchable(w, "patient")).toMatchObject([
      { _id: w.machine, programsEnabled: false, myHrMax: 180 },
    ]);
    expect(
      await as(w.t, "patient").query(api.training.getMachineLive, {
        machineId: w.machine,
      }),
    ).toEqual({
      status: "online",
      programsEnabled: false,
      live: null,
      stale: true,
      serverNow: NOW,
    });
  });

  it("lists a machine's programmes by name, whatever order the machine synced them in", async () => {
    const w = await seedWorld(modules);
    await w.t.mutation(internal.training.syncProfiles, {
      machineId: w.machine,
      storeRev: 2,
      programsEnabled: true,
      profiles: ["Zone 3", "Recovery", "Warm-up"].map((name, rank) => ({
        ...PROGRAMME,
        profileId: `p${rank}`,
        name,
      })),
    });

    const ordered = ["Recovery", "Warm-up", "Zone 3"];
    expect(
      (
        await as(w.t, "patient").query(api.training.listMachineProfiles, {
          machineId: w.machine,
        })
      ).map((profile) => profile.name),
    ).toEqual(ordered);
    const [listed] = await launchable(w, "patient");
    expect(listed?.profiles.map((profile) => profile.name)).toEqual(ordered);
  });

  it("answers nothing about a machine whose row is gone, to the Anheart admin too", async () => {
    const w = await seedWorld(modules);
    await w.t.run((ctx) => ctx.db.delete(w.machine));

    expect(
      await as(w.t, "admin").query(api.training.getMachineLive, {
        machineId: w.machine,
      }),
    ).toBeNull();
  });

  it("serves the telemetry measured after `sinceT`, oldest first, and the latest points when a limit cuts them", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      kind: "auto",
    });
    expect(
      await w.t.mutation(internal.training.storeTelemetry, {
        machineId: w.machine,
        sessionId,
        points: [0, 1, 2, 3, 4].map((second) => point(NOW + second * 1000)),
      }),
    ).toEqual({ stored: 5 });
    /** Seconds into the session of each point served. */
    const read = async (args: { sinceT?: number; limit?: number }) =>
      (
        await as(w.t, "patient").query(api.training.getSessionTelemetry, {
          sessionId,
          ...args,
        })
      ).map((served) => served.elapsedS);

    expect(await read({})).toEqual([0, 1, 2, 3, 4]);
    // Strictly after: the point at `sinceT` was already read.
    expect(await read({ sinceT: NOW + 2000 })).toEqual([3, 4]);
    expect(await read({ sinceT: NOW + 4000 })).toEqual([]);
    expect(await read({ limit: 2 })).toEqual([3, 4]);
    expect(await read({ sinceT: NOW + 1000, limit: 2 })).toEqual([3, 4]);
    // A limit below one still serves the latest point.
    expect(await read({ limit: 0 })).toEqual([4]);
  });

  it("shows a session of the retired recording mode as a recording", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "completed",
    });

    expect(
      await as(w.t, "manager").query(api.training.getTrainingSession, {
        sessionId,
      }),
    ).toMatchObject({
      _id: sessionId,
      kind: "recording",
      machineName: "Synthetic machine",
      status: "completed",
      canStop: false,
      lastSignalAt: null,
      lastMeasuredAt: null,
    });
  });

  it("still shows the Anheart admin a session whose machine row is gone, without a machine name", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "completed",
      kind: "auto",
    });
    await w.t.run((ctx) => ctx.db.delete(w.machine));

    expect(
      await as(w.t, "admin").query(api.training.getTrainingSession, {
        sessionId,
      }),
    ).toMatchObject({
      _id: sessionId,
      machineId: w.machine,
      machineName: "-",
      kind: "auto",
      status: "completed",
    });
  });
});

// ---------------------------------------------------------------------------
// What a machine reports, through its routes
// ---------------------------------------------------------------------------

describe("what a machine reports through its routes", () => {
  const LIVE = {
    runMode: "seance",
    phase: "hold",
    bpm: 137,
    motorRpm: 1200,
    outputRpm: 24,
    setpointMotorRpm: 1200,
    gLoad: 1.2,
    safetyAction: "none",
  };

  it("dates the live state on the server's clock and keeps the programmes setting a heartbeat does not carry", async () => {
    const w = await machineWorld();
    const beat = (body: unknown) =>
      send(w, "POST", "/api/machine/heartbeat", body);

    vi.setSystemTime(NOW + 10_000);
    const reported = { ...LIVE, driveState: "run", sessionId: "local-7" };
    const first = await beat({ live: { ...reported, updatedAt: 1 } });

    expect(first.status).toBe(200);
    expect(await rowOf(w, w.machine)).toMatchObject({
      live: { ...reported, updatedAt: NOW + 10_000 },
      lastHeartbeat: NOW + 10_000,
      programsEnabled: true,
    });

    // A setting that is not a boolean is no setting either.
    await beat({ live: LIVE, programsEnabled: "no" });
    expect((await rowOf(w, w.machine))?.programsEnabled).toBe(true);
    // The machine's own word is taken.
    await beat({ live: LIVE, programsEnabled: false });
    expect((await rowOf(w, w.machine))?.programsEnabled).toBe(false);
  });

  it.each([
    ["is not an object", "seance"],
    ["carries a drive state that is not text", { ...LIVE, driveState: 7 }],
    ["carries a session that is not text", { ...LIVE, sessionId: 12 }],
    ["carries a heart rate that is not a number", { ...LIVE, bpm: "137" }],
    ["lacks the motor speed", { ...LIVE, motorRpm: undefined }],
  ])(
    "records the heartbeat but not a live state that %s",
    async (_name, live) => {
      const w = await machineWorld();
      await send(w, "POST", "/api/machine/heartbeat", { live: LIVE });
      const accepted = (await rowOf(w, w.machine))?.live;
      expect(accepted).toEqual({ ...LIVE, updatedAt: NOW });

      vi.setSystemTime(NOW + 10_000);
      const response = await send(w, "POST", "/api/machine/heartbeat", {
        live,
        programsEnabled: false,
      });

      expect(response.status).toBe(200);
      const machine = await rowOf(w, w.machine);
      // The last state the server accepted stays, with its own date...
      expect(machine?.live).toEqual(accepted);
      // ...and the setting that travelled with the refused state is not taken.
      expect(machine?.programsEnabled).toBe(true);
      expect(machine?.lastHeartbeat).toBe(NOW + 10_000);
    },
  );

  // CURRENT BEHAVIOUR, reported in ANH-203 and left as it is: the route checks
  // the fields of `live` it knows and lets any other through, then the write
  // refuses the object as a whole. By then the heartbeat is recorded, so the
  // machine is seen alive while its request fails and its state is not stored.
  it("current behaviour: a live state carrying a field the server does not know fails the request after the heartbeat is recorded", async () => {
    const w = await machineWorld();

    vi.setSystemTime(NOW + 10_000);
    await expect(
      send(w, "POST", "/api/machine/heartbeat", {
        live: { ...LIVE, armAngleDeg: 12 },
      }),
    ).rejects.toThrow(/armAngleDeg/);

    const machine = await rowOf(w, w.machine);
    expect(machine?.lastHeartbeat).toBe(NOW + 10_000);
    expect(machine?.live).toBeUndefined();
  });

  it("offers an older pending launch with neutral values for what it lacks, never invented ones", async () => {
    const w = await machineWorld();
    // A launch queued before the age, the label and the operator were stored.
    const sessionId = await w.t.run((ctx) =>
      ctx.db.insert("sessions", {
        organizationId: w.organizationId,
        machineId: w.machine,
        userId: w.patient,
        status: "pending",
        startedAt: NOW,
        channels: ["ECG"],
        kind: "auto",
        origin: "remote",
        profileId: "p1",
        subjectHrMax: 180,
      }),
    );

    const response = await send(w, "GET", "/api/machine/training/poll");

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      session: {
        sessionId,
        profileId: "p1",
        totalDurationS: null,
        subjectId: w.patient,
        subjectLabel: "",
        subjectHrMax: 180,
        subjectAge: null,
        operatorName: "dashboard",
      },
      server_contract_version: CONTRACT_VERSION,
    });
  });

  it("records a session started at the machine with what the machine says of it, the occupancy as its note", async () => {
    const w = await machineWorld();
    const register = async (body: Record<string, unknown>) =>
      (
        (await (
          await send(w, "POST", "/api/machine/training/local", {
            kind: "auto",
            startedAt: NOW - 60_000,
            operatorName: "Operator",
            ...body,
          })
        ).json()) as { sessionId: Id<"sessions"> }
      ).sessionId;

    const described = await register({
      localRef: "local-described",
      userId: w.patient,
      subjectLabel: "Synthetic Patient",
      profileId: "p1",
      profileName: "Programme p1",
      zoneLowBpm: 130,
      zoneHighBpm: 150,
      totalDurationS: 600,
      subjectHrMax: 180,
      occupancy: "duo",
    });
    // What is not of the expected type is left out, not stored as it came.
    const mistyped = await register({
      localRef: "local-mistyped",
      userId: 42,
      subjectLabel: null,
      profileId: ["p1"],
      zoneLowBpm: "130",
      totalDurationS: null,
      occupancy: 2,
    });

    expect(await rowOf(w, described)).toMatchObject({
      organizationId: w.organizationId,
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      startedAt: NOW - 60_000,
      kind: "auto",
      origin: "local",
      localRef: "local-described",
      notes: "Occupancy: duo",
      profileId: "p1",
      profileName: "Programme p1",
      zoneLowBpm: 130,
      zoneHighBpm: 150,
      totalDurationS: 600,
      subjectHrMax: 180,
      subjectLabel: "Synthetic Patient",
      operatorName: "Operator",
    });
    const bare = await rowOf(w, mistyped);
    expect(bare).toMatchObject({
      status: "active",
      kind: "auto",
      origin: "local",
      localRef: "local-mistyped",
      operatorName: "Operator",
    });
    for (const field of [
      "userId",
      "notes",
      "subjectLabel",
      "profileId",
      "zoneLowBpm",
      "totalDurationS",
    ] as const) {
      expect(bare?.[field]).toBeUndefined();
    }
    expect((await rowOf(w, w.machine))?.status).toBe("in_session");
  });

  it("dates the end of a session as the machine wrote it, or on the server's clock when it wrote no date", async () => {
    const w = await machineWorld();
    const active = () =>
      w.t.run((ctx) =>
        ctx.db.insert("sessions", {
          organizationId: w.organizationId,
          machineId: w.machine,
          userId: w.patient,
          status: "active",
          startedAt: NOW,
          channels: ["ECG"],
          kind: "auto",
        }),
      );
    const dated = await active();
    const undated = await active();
    const misdated = await active();

    vi.setSystemTime(NOW + 90_000);
    for (const [sessionId, endedAt] of [
      [dated, NOW + 60_000],
      [undated, undefined],
      [misdated, "a minute ago"],
    ] as const) {
      const response = await send(w, "POST", "/api/machine/training/end", {
        sessionId,
        failed: true,
        reason: "drive_fault",
        endedAt,
      });
      expect(response.status).toBe(200);
    }

    expect(await rowOf(w, dated)).toMatchObject({
      status: "failed",
      endedAt: NOW + 60_000,
      endReason: "drive_fault",
    });
    expect((await rowOf(w, undated))?.endedAt).toBe(NOW + 90_000);
    expect((await rowOf(w, misdated))?.endedAt).toBe(NOW + 90_000);
  });

  it.each([
    ["null", null],
    ["a number", 5],
    ["text", "hold"],
  ])(
    "refuses a telemetry batch holding %s in place of a point, and stores none of it",
    async (_name, intruder) => {
      const w = await machineWorld();
      const sessionId = await w.t.run((ctx) =>
        ctx.db.insert("sessions", {
          organizationId: w.organizationId,
          machineId: w.machine,
          userId: w.patient,
          status: "active",
          startedAt: NOW,
          channels: ["ECG"],
          kind: "auto",
        }),
      );

      const response = await send(
        w,
        "POST",
        "/api/machine/training/telemetry",
        {
          sessionId,
          points: [point(NOW), intruder, point(NOW + 1000)],
        },
      );

      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({
        error: "invalid_request",
        message: "Malformed telemetry point",
      });
      expect(
        await w.t.run((ctx) => ctx.db.query("training_telemetry").collect()),
      ).toEqual([]);
    },
  );
});

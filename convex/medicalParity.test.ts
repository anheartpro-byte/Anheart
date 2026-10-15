import { ConvexError } from "convex/values";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import parityFixture from "../raspberry-pi/tests/fixtures/medical_zone_parity.json";
import { api } from "./_generated/api";
import { currentMedicalParameters } from "./lib/medicalParameters";
import { ageFrom, effectiveHrMax, zoneRefusal } from "./training";
import {
  as,
  modules,
  NOW,
  registerShippedMedicalParameters,
  seedWorld,
  backendOf,
} from "./test.setup";
import { estimateHrMax } from "../lib/medicalParameters";

const YEAR = new Date(NOW).getUTCFullYear();

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
});

async function shippedParameters() {
  const t = backendOf(modules);
  await registerShippedMedicalParameters(t);
  const current = await t.run((ctx) => currentMedicalParameters(ctx));
  if (current === null) throw new Error("The shipped medical version was not registered");
  return current;
}

describe("Pi and Convex medical parity", () => {
  it("uses the current shipped version for every zone verdict", async () => {
    const medical = await shippedParameters();
    for (const testCase of parityFixture.zone_cases) {
      const verdict = zoneRefusal(
        {
          zoneHighBpm: testCase.zone_high_bpm,
          hardMaxBpm: testCase.hard_max_bpm,
        },
        testCase.hr_max,
        medical.zoneCeilingFraction,
      );
      expect(verdict).toBe(testCase.expected[0] ?? null);
    }
  });

  it("uses the current shipped formula for every age estimate", async () => {
    const medical = await shippedParameters();
    for (const testCase of parityFixture.age_estimates) {
      expect(estimateHrMax(testCase.age, medical.hrMaxFormula)).toBe(testCase.hr_max);
    }
  });

  it("uses the current minimum age for every age refusal", async () => {
    const medical = await shippedParameters();
    for (const testCase of parityFixture.age_minimum) {
      const birthYear = YEAR - testCase.age - 1;
      const age = ageFrom(birthYear, NOW);
      const refused = age === null || age < medical.minRiderAge;
      expect(refused).toBe(testCase.refused);
    }
  });

  it("refuses an underage rider through the real launch mutation", async () => {
    const underage = parityFixture.age_minimum.find((testCase) => testCase.refused);
    if (underage === undefined) throw new Error("The fixture needs a refused age");
    const world = await seedWorld(modules);
    await world.t.run((ctx) =>
      ctx.db.patch(world.patient, {
        birthYear: YEAR - underage.age - 1,
      }),
    );

    const verdict = await as(world.t, "manager")
      .mutation(api.training.launchAutoSession, {
        machineId: world.machine,
        profileId: world.profileId,
        userId: world.patient,
      })
      .then(
        () => null,
        (error: unknown) =>
          error instanceof ConvexError ? error.data : error instanceof Error ? error.message : String(error),
      );
    expect(verdict).toBe("rider_too_young");
  });

  it("can use the effective maximum helper with the shipped formula", async () => {
    const medical = await shippedParameters();
    for (const testCase of parityFixture.age_estimates) {
      expect(
        effectiveHrMax(
          { hrMax: undefined, birthYear: YEAR - testCase.age },
          NOW,
          medical.hrMaxFormula,
        ),
      ).toBe(testCase.hr_max);
    }
  });
});

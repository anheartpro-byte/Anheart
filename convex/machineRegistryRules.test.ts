import { describe, expect, it } from "vitest";
import type { Doc, Id } from "./_generated/dataModel";
import {
  CONFIGURATION_LOCKS,
  configurationLockVerdict,
  downgradeRefusal,
  levelAt,
  MEDICAL_KEY_IS_TEST,
  nextValidationLevel,
  personOnBoard,
  radiiProposal,
  requireConfigurationAllowed,
  reviewRefusal,
  validationRank,
  versionStatus,
} from "./lib/machineRegistry";
import {
  MACHINE_VALIDATION_LEVELS,
  type MachineValidationLevel,
} from "./lib/releaseValidation";
import { modules, seedWorld } from "./test.setup";

const corrupt = "corrupt" as MachineValidationLevel;

describe("machine registry rules", () => {
  it("orders levels, finds their successor and rejects corrupt levels", () => {
    expect(MACHINE_VALIDATION_LEVELS.map(validationRank)).toEqual([0, 1, 2, 3]);
    expect(MACHINE_VALIDATION_LEVELS.map(nextValidationLevel)).toEqual([
      "bench",
      "auto",
      "occupied",
      null,
    ]);
    expect(nextValidationLevel(corrupt)).toBeNull();
  });

  it("evaluates every configuration key and level, including false and corrupt data", () => {
    for (const level of [...MACHINE_VALIDATION_LEVELS, corrupt]) {
      for (const key of Object.keys(CONFIGURATION_LOCKS) as Array<
        keyof typeof CONFIGURATION_LOCKS
      >) {
        for (const value of [false, true]) {
          const verdict = configurationLockVerdict(level, key, value);
          const allowed =
            level !== corrupt &&
            (!value ||
              validationRank(level) >=
                validationRank(CONFIGURATION_LOCKS[key]));
          expect(verdict).toEqual({
            allowed,
            reachedLevel: level,
            requiredLevel: CONFIGURATION_LOCKS[key],
            message: allowed
              ? null
              : `machine validée jusqu'au niveau ${level}`,
          });
        }
      }
    }
  });

  it("requires consecutive reviews and production signed parameters for occupied", () => {
    expect(MEDICAL_KEY_IS_TEST).toBe(true);
    expect(reviewRefusal("none", "bench", true, null)).toBeNull();
    expect(reviewRefusal("bench", "auto", true, null)).toBeNull();
    expect(reviewRefusal("none", "auto", false, "TEST-MED")).toBe(
      "validation_level_not_next",
    );
    expect(reviewRefusal("bench", "bench", false, null)).toBe(
      "validation_level_not_next",
    );
    expect(reviewRefusal("auto", "occupied", true, "TEST-MED")).toBe(
      "occupied_requires_signed_medical_parameters",
    );
    expect(reviewRefusal("auto", "occupied", false, null)).toBe(
      "occupied_requires_signed_medical_parameters",
    );
    expect(reviewRefusal("auto", "occupied", false, "TEST-MED")).toBeNull();
  });

  it("only lowers levels with a trimmed reason between 3 and 500 characters", () => {
    expect(downgradeRefusal("auto", corrupt, "Test reason")).toBe(
      "validation_level_not_lower",
    );
    expect(downgradeRefusal("auto", "auto", "Test reason")).toBe(
      "validation_level_not_lower",
    );
    expect(downgradeRefusal("auto", "occupied", "Test reason")).toBe(
      "validation_level_not_lower",
    );
    expect(downgradeRefusal(corrupt, "none", "Test reason")).toBe(
      "validation_level_not_lower",
    );
    for (const reason of ["", " x ", "x".repeat(501)])
      expect(downgradeRefusal("auto", "bench", reason)).toBe(
        "downgrade_reason_required",
      );
    for (const reason of [" abc ", "x".repeat(500)])
      expect(downgradeRefusal("auto", "none", reason)).toBeNull();
  });

  it("reconstructs historical levels in chronological order", () => {
    const by = "TEST-USER" as Id<"users">;
    const review = {
      by,
      reportStorageId: "TEST-REPORT" as Id<"_storage">,
      softwareVersion: "pi-0.1.0",
      medicalParametersVersion: null,
    };
    const validation: Doc<"machine_registry">["validation"] = {
      level: "bench",
      reviews: [
        { ...review, level: "auto", at: 20 },
        { ...review, level: "bench", at: 10 },
      ],
      downgrades: [
        { from: "auto", to: "bench", by, reason: "Test downgrade", at: 30 },
      ],
    };
    expect(
      [0, 10, 19, 20, 29, 30, 40].map((at) => levelAt(validation, at)),
    ).toEqual(["none", "bench", "bench", "auto", "auto", "bench", "bench"]);
    expect(levelAt({ reviews: [], downgrades: [] }, 10)).toBe("none");
  });

  it("orders simultaneous reviews and downgrades by their valid transitions", () => {
    const by = "TEST-USER" as Id<"users">;
    const report = {
      by,
      reportStorageId: "TEST-REPORT" as Id<"_storage">,
      softwareVersion: "pi-0.1.0",
      medicalParametersVersion: null,
    };
    const initial = { ...report, level: "bench" as const, at: 1 };
    const auto = { ...report, level: "auto" as const, at: 2 };
    const down = {
      from: "auto" as const,
      to: "bench" as const,
      by,
      reason: "TEST reason",
      at: 2,
    };
    expect(levelAt({ reviews: [initial, auto], downgrades: [down] }, 2)).toBe(
      "bench",
    );
    expect(
      levelAt(
        { reviews: [initial, { ...auto, at: 1 }, auto], downgrades: [down] },
        2,
      ),
    ).toBe("auto");
    expect(
      levelAt({ reviews: [initial, auto, auto], downgrades: [down] }, 2),
    ).toBe("auto");
    expect(
      levelAt(
        {
          reviews: [initial, auto],
          downgrades: [{ ...down, from: "bench", to: "none", at: 3 }, down],
        },
        3,
      ),
    ).toBe("none");
  });

  it("fails closed when the historical transitions cannot form a valid chain", () => {
    const by = "TEST-USER" as Id<"users">;
    const report = {
      by,
      reportStorageId: "TEST-REPORT" as Id<"_storage">,
      softwareVersion: "pi-0.1.0",
      medicalParametersVersion: null,
    };
    const bench = { ...report, level: "bench" as const, at: 1 };
    const auto = { ...report, level: "auto" as const, at: 2 };
    const down = {
      from: "auto" as const,
      to: "bench" as MachineValidationLevel,
      by,
      reason: "TEST reason",
      at: 3,
    };
    expect(levelAt({ reviews: [auto], downgrades: [] }, 4)).toBe("none");
    expect(levelAt({ reviews: [], downgrades: [down] }, 4)).toBe("none");
    expect(
      levelAt(
        { reviews: [bench, auto], downgrades: [{ ...down, from: "occupied" }] },
        4,
      ),
    ).toBe("none");
    expect(
      levelAt(
        { reviews: [bench, auto], downgrades: [{ ...down, to: "auto" }] },
        4,
      ),
    ).toBe("none");
    expect(
      levelAt(
        { reviews: [bench, auto], downgrades: [{ ...down, to: corrupt }] },
        4,
      ),
    ).toBe("none");
    expect(
      levelAt({ reviews: [bench, auto], downgrades: [{ ...down, at: 0 }] }, 4),
    ).toBe("none");
    expect(
      levelAt({ reviews: [auto], downgrades: [{ ...down, at: 4 }] }, 4),
    ).toBe("none");
  });

  it("prioritizes unknown, not validated, current and outdated software", () => {
    const base = {
      reported: "pi-0.1.0",
      expected: "pi-0.1.0",
      reportedReleaseLevel: "bench" as const,
      machineLevel: "bench" as const,
    };
    expect(versionStatus({ ...base, reported: undefined })).toBe("unknown");
    expect(versionStatus({ ...base, expected: undefined })).toBe("unknown");
    expect(versionStatus({ ...base, reportedReleaseLevel: undefined })).toBe(
      "not_validated",
    );
    expect(versionStatus(base)).toBe("up_to_date");
    expect(versionStatus({ ...base, expected: "pi-0.2.0" })).toBe("outdated");
  });

  it("recognizes a rider only in auto sessions or an exact occupancy segment", () => {
    expect(personOnBoard({ kind: "auto" })).toBe(true);
    expect(personOnBoard({ kind: "manual" })).toBe(false);
    expect(personOnBoard({})).toBe(false);
    expect(
      personOnBoard({ notes: "TEST; Occupancy: occupied; Other: test" }),
    ).toBe(true);
    for (const notes of [
      "Occupancy: empty",
      "not Occupancy: occupied",
      "Occupancy: occupied-extra",
      "TEST;Occupancy: occupied",
      "",
    ])
      expect(personOnBoard({ kind: "manual", notes })).toBe(false);
  });

  it("proposes only radii that were measured", () => {
    expect(radiiProposal(null)).toEqual({
      ARM_RADIUS_M: null,
      LEG_TIP_RADIUS_M: null,
    });
    expect(radiiProposal({})).toEqual({
      ARM_RADIUS_M: null,
      LEG_TIP_RADIUS_M: null,
    });
    expect(radiiProposal({ armRadiusM: 1.234, legTipRadiusM: 2.345 })).toEqual({
      ARM_RADIUS_M: 1.234,
      LEG_TIP_RADIUS_M: 2.345,
    });
  });

  it("provides the server configuration guard with and without a registry", async () => {
    const w = await seedWorld(modules);
    await expect(
      w.t.run((ctx) =>
        requireConfigurationAllowed(ctx, w.machine, "PROGRAMS_ENABLED", true),
      ),
    ).rejects.toThrow("machine_validation_insufficient");
    await expect(
      w.t.run(async (ctx) => {
        await requireConfigurationAllowed(
          ctx,
          w.machine,
          "PROGRAMS_ENABLED",
          false,
        );
        return true;
      }),
    ).resolves.toBe(true);
    await w.t.run((ctx) =>
      ctx.db.insert("machine_registry", {
        machineId: w.machine,
        organizationId: w.orgA,
        contacts: [],
        driveParameterSheetHistory: [],
        validation: { level: "auto", reviews: [], downgrades: [] },
        updatedAt: Date.now(),
        updatedBy: w.admin,
      }),
    );
    await expect(
      w.t.run(async (ctx) => {
        await requireConfigurationAllowed(
          ctx,
          w.machine,
          "PROGRAMS_ENABLED",
          true,
        );
        return true;
      }),
    ).resolves.toBe(true);
    await expect(
      w.t.run((ctx) =>
        requireConfigurationAllowed(
          ctx,
          w.machine,
          "OCCUPANCY_OCCUPIED_ENABLED",
          true,
        ),
      ),
    ).rejects.toThrow("machine_validation_insufficient");
  });
});

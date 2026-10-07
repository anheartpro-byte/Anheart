/// <reference types="vite/client" />
/**
 * ANH-134 EX-4 and EX-5: the register of released versions and the rule that
 * decides which machine may receive a Pi version. Who may call the two public
 * functions is in `authorization.matrix.ts`; this file checks what they accept
 * and store.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./_generated/api";
import {
  MACHINE_VALIDATION_LEVELS,
  VALIDATION_LEVELS,
  isReleaseVersion,
  releaseAllowedOnMachine,
  requiredReleaseLevel,
  type MachineValidationLevel,
  type ValidationLevel,
} from "./lib/releaseValidation";
import { ANHEART_CLERK_ORG, as, modules, NOW, seedWorld } from "./test.setup";

const PI = {
  component: "pi" as const,
  version: "pi-0.1.0",
  validationLevel: "bench" as const,
  releasedAt: NOW,
};

async function world() {
  const w = await seedWorld(modules);
  return { w, admin: as(w.t, "admin") };
}

describe("ANH-134 EX-4 softwareReleases.recordRelease", () => {
  it("stores a Pi version with its validation level and who recorded it", async () => {
    const { w, admin } = await world();
    const id = await admin.mutation(api.softwareReleases.recordRelease, {
      ...PI,
      notes: "  first bench release  ",
    });
    const row = await w.t.run((ctx) => ctx.db.get(id));
    expect(row).toMatchObject({
      component: "pi",
      version: "pi-0.1.0",
      validationLevel: "bench",
      releasedAt: NOW,
      notes: "first bench release",
      recordedBy: w.admin,
    });
  });

  it("stores a cloud or web version without a validation level", async () => {
    const { w, admin } = await world();
    for (const component of ["cloud", "web"] as const) {
      const id = await admin.mutation(api.softwareReleases.recordRelease, {
        component,
        version: `${component}-1.2.3`,
        releasedAt: NOW,
      });
      const row = await w.t.run((ctx) => ctx.db.get(id));
      expect(row?.validationLevel).toBeUndefined();
      expect(row?.version).toBe(`${component}-1.2.3`);
    }
  });

  it("refuses a Pi version without a validation level", async () => {
    const { w, admin } = await world();
    await expect(
      admin.mutation(api.softwareReleases.recordRelease, {
        component: "pi",
        version: "pi-0.1.0",
        releasedAt: NOW,
      }),
    ).rejects.toThrow(/needs a validation level/);
    expect(await count(w)).toBe(0);
  });

  it.each(["cloud", "web"] as const)(
    "refuses a validation level on a %s version",
    async (component) => {
      const { w, admin } = await world();
      await expect(
        admin.mutation(api.softwareReleases.recordRelease, {
          component,
          version: `${component}-0.1.0`,
          validationLevel: "occupied_validated",
          releasedAt: NOW,
        }),
      ).rejects.toThrow(/Only a Pi version/);
      expect(await count(w)).toBe(0);
    },
  );

  it.each([
    "0.1.0",
    "pi-0.1",
    "pi-0.1.0.0",
    "pi-0.1.0-dev",
    "pi-01.0.0",
    "pi-0.1.0 ",
    "web-0.1.0",
    "PI-0.1.0",
    "",
  ])("refuses the malformed Pi version %j", async (version) => {
    const { w, admin } = await world();
    await expect(
      admin.mutation(api.softwareReleases.recordRelease, { ...PI, version }),
    ).rejects.toThrow(/Invalid version/);
    expect(await count(w)).toBe(0);
  });

  it.each([0, -1, Number.NaN, Number.POSITIVE_INFINITY])(
    "refuses the release date %s",
    async (releasedAt) => {
      const { w, admin } = await world();
      await expect(
        admin.mutation(api.softwareReleases.recordRelease, {
          ...PI,
          releasedAt,
        }),
      ).rejects.toThrow(/Invalid release date/);
      expect(await count(w)).toBe(0);
    },
  );

  it("refuses notes longer than the limit", async () => {
    const { w, admin } = await world();
    await expect(
      admin.mutation(api.softwareReleases.recordRelease, {
        ...PI,
        notes: "x".repeat(2001),
      }),
    ).rejects.toThrow(/limited to 2000/);
    expect(await count(w)).toBe(0);
  });

  it("keeps one row per component version when recorded twice", async () => {
    const { w, admin } = await world();
    const first = await admin.mutation(api.softwareReleases.recordRelease, PI);
    const second = await admin.mutation(api.softwareReleases.recordRelease, {
      ...PI,
      releasedAt: NOW + 1,
    });
    expect(second).toBe(first);
    expect(await count(w)).toBe(1);
    const row = await w.t.run((ctx) => ctx.db.get(first));
    expect(row?.releasedAt).toBe(NOW + 1);
  });

  it("changes the validation level of a known version only with notes", async () => {
    const { w, admin } = await world();
    const id = await admin.mutation(api.softwareReleases.recordRelease, PI);

    await expect(
      admin.mutation(api.softwareReleases.recordRelease, {
        ...PI,
        validationLevel: "auto_validated",
      }),
    ).rejects.toThrow(/needs notes/);
    expect((await w.t.run((ctx) => ctx.db.get(id)))?.validationLevel).toBe(
      "bench",
    );

    await admin.mutation(api.softwareReleases.recordRelease, {
      ...PI,
      validationLevel: "auto_validated",
      notes: "M5 review of 2027-01-15",
    });
    const row = await w.t.run((ctx) => ctx.db.get(id));
    expect(row?.validationLevel).toBe("auto_validated");
    expect(row?.notes).toBe("M5 review of 2027-01-15");
    expect(await count(w)).toBe(1);
  });
});

describe("ANH-195 EX-7 the history of the validation levels of a Pi version", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  /** A second admin of Anheart, so that a decision can be told from another. */
  async function secondAdmin(w: Awaited<ReturnType<typeof seedWorld>>) {
    const id = await w.t.run(async (ctx) => {
      const userId = await ctx.db.insert("users", {
        clerkId: "second-admin",
        role: "admin" as const,
        organizationId: w.anheartOrg,
        firstName: "Second",
        lastName: "Synthetic",
        email: "second-admin@example.invalid",
        language: "en" as const,
        createdAt: NOW,
      });
      await ctx.db.insert("memberships", {
        userId,
        organizationId: w.anheartOrg,
        role: "admin",
        active: true,
      });
      return userId;
    });
    const identity = w.t.withIdentity({
      subject: "second-admin",
      org_id: ANHEART_CLERK_ORG,
      org_role: "org:admin",
    });
    return { id, identity };
  }

  const levels = (w: Awaited<ReturnType<typeof seedWorld>>) =>
    w.t.run((ctx) => ctx.db.query("software_release_levels").collect());

  it("keeps every level a version has held, with who decided it, when and why", async () => {
    const { w, admin } = await world();
    const reviewer = await secondAdmin(w);

    await admin.mutation(api.softwareReleases.recordRelease, {
      ...PI,
      notes: "bench check-list",
    });
    vi.setSystemTime(NOW + 1000);
    await reviewer.identity.mutation(api.softwareReleases.recordRelease, {
      ...PI,
      validationLevel: "auto_validated",
      notes: "M5 review of 2027-01-15",
    });
    vi.setSystemTime(NOW + 2000);
    await admin.mutation(api.softwareReleases.recordRelease, {
      ...PI,
      validationLevel: "bench",
      notes: "withdrawn after the review of 2027-02-01",
    });
    const listed = await admin.query(api.softwareReleases.listReleases, {});

    expect(listed).toHaveLength(1);
    // The row says what is in force, and who wrote it last.
    expect(listed[0]).toMatchObject({
      validationLevel: "bench",
      notes: "withdrawn after the review of 2027-02-01",
      recordedBy: w.admin,
      updatedAt: NOW + 2000,
    });
    // The history says how the version got there: nothing was replaced.
    expect(listed[0].levelHistory).toEqual([
      {
        validationLevel: "bench",
        reason: "bench check-list",
        decidedBy: w.admin,
        decidedAt: NOW,
      },
      {
        validationLevel: "auto_validated",
        reason: "M5 review of 2027-01-15",
        decidedBy: reviewer.id,
        decidedAt: NOW + 1000,
      },
      {
        validationLevel: "bench",
        reason: "withdrawn after the review of 2027-02-01",
        decidedBy: w.admin,
        decidedAt: NOW + 2000,
      },
    ]);
  });

  it("adds nothing to the history when a correction leaves the level alone", async () => {
    const { w, admin } = await world();
    await admin.mutation(api.softwareReleases.recordRelease, {
      ...PI,
      notes: "bench check-list",
    });

    vi.setSystemTime(NOW + 1000);
    await admin.mutation(api.softwareReleases.recordRelease, {
      ...PI,
      releasedAt: NOW + 5,
      notes: "release date corrected",
    });
    const listed = await admin.query(api.softwareReleases.listReleases, {});
    const stored = await levels(w);

    expect(listed[0].notes).toBe("release date corrected");
    expect(listed[0].levelHistory).toEqual([
      {
        validationLevel: "bench",
        reason: "bench check-list",
        decidedBy: w.admin,
        decidedAt: NOW,
      },
    ]);
    expect(stored).toHaveLength(1);
  });

  it("adds nothing to the history when a level change is refused", async () => {
    const { w, admin } = await world();
    await admin.mutation(api.softwareReleases.recordRelease, PI);

    const refused = admin.mutation(api.softwareReleases.recordRelease, {
      ...PI,
      validationLevel: "occupied_validated",
    });

    await expect(refused).rejects.toThrow(/needs notes/);
    const stored = await levels(w);
    expect(stored.map((row) => row.validationLevel)).toEqual(["bench"]);
  });

  it("writes down the level of a row recorded before the history existed, then the new one", async () => {
    const { w, admin } = await world();
    const reviewer = await secondAdmin(w);
    // A row as the register wrote them before this history: no decision stored.
    const id = await w.t.run((ctx) =>
      ctx.db.insert("software_releases", {
        ...PI,
        notes: "first bench release",
        recordedBy: reviewer.id,
        updatedAt: NOW - 86_400_000,
      }),
    );
    const first = {
      validationLevel: "bench",
      reason: "first bench release",
      decidedBy: reviewer.id,
      decidedAt: NOW - 86_400_000,
    };

    // Read as it is: the row itself is the first entry of its history.
    const before = await admin.query(api.softwareReleases.listReleases, {});
    expect(before[0].levelHistory).toEqual([first]);
    expect(await levels(w)).toEqual([]);

    await admin.mutation(api.softwareReleases.recordRelease, {
      ...PI,
      validationLevel: "auto_validated",
      notes: "M5 review of 2027-01-15",
    });
    const after = await admin.query(api.softwareReleases.listReleases, {});
    const stored = await levels(w);

    expect(after[0]._id).toBe(id);
    expect(after[0].levelHistory).toEqual([
      first,
      {
        validationLevel: "auto_validated",
        reason: "M5 review of 2027-01-15",
        decidedBy: w.admin,
        decidedAt: NOW,
      },
    ]);
    expect(stored.map((row) => row.releaseId)).toEqual([id, id]);
  });

  it("keeps no level history for a cloud or web version", async () => {
    const { w, admin } = await world();
    for (const component of ["cloud", "web"] as const) {
      await admin.mutation(api.softwareReleases.recordRelease, {
        component,
        version: `${component}-0.1.0`,
        releasedAt: NOW,
        notes: "released",
      });
    }

    const listed = await admin.query(api.softwareReleases.listReleases, {});
    const stored = await levels(w);

    expect(listed.map((row) => row.levelHistory)).toEqual([[], []]);
    expect(stored).toEqual([]);
  });

  it("clears a level a cloud row should never have carried, without inventing a decision", async () => {
    const { w, admin } = await world();
    // Written by hand: the mutation refuses a level on a cloud version.
    const id = await w.t.run((ctx) =>
      ctx.db.insert("software_releases", {
        component: "cloud" as const,
        version: "cloud-0.1.0",
        validationLevel: "bench" as const,
        releasedAt: NOW,
        recordedBy: w.admin,
        updatedAt: NOW,
      }),
    );
    const correction = {
      component: "cloud" as const,
      version: "cloud-0.1.0",
      releasedAt: NOW,
    };

    const silent = admin.mutation(
      api.softwareReleases.recordRelease,
      correction,
    );
    await expect(silent).rejects.toThrow(/needs notes/);
    await admin.mutation(api.softwareReleases.recordRelease, {
      ...correction,
      notes: "a cloud version carries no level",
    });
    const row = await w.t.run((ctx) => ctx.db.get(id));
    const stored = await levels(w);

    expect(row?.validationLevel).toBeUndefined();
    expect(stored).toEqual([]);
  });
});

describe("ANH-134 EX-4 softwareReleases.listReleases", () => {
  it("lists the most recent first and filters by component", async () => {
    const { admin } = await world();
    await admin.mutation(api.softwareReleases.recordRelease, PI);
    await admin.mutation(api.softwareReleases.recordRelease, {
      ...PI,
      version: "pi-0.2.0",
      releasedAt: NOW + 2,
    });
    await admin.mutation(api.softwareReleases.recordRelease, {
      component: "web",
      version: "web-0.1.0",
      releasedAt: NOW + 1,
    });

    const all = await admin.query(api.softwareReleases.listReleases, {});
    expect(all.map((row) => row.version)).toEqual([
      "pi-0.2.0",
      "web-0.1.0",
      "pi-0.1.0",
    ]);
    const pi = await admin.query(api.softwareReleases.listReleases, {
      component: "pi",
    });
    expect(pi.map((row) => row.version)).toEqual(["pi-0.2.0", "pi-0.1.0"]);
  });
});

describe("ANH-134 EX-5 which machine may receive a Pi version", () => {
  // Rows: the machine's validation. Columns: the version's, `undefined` first.
  const allowed: Record<MachineValidationLevel, boolean[]> = {
    none: [true, true, true, true],
    bench: [false, true, true, true],
    auto: [false, false, true, true],
    occupied: [false, false, false, true],
  };
  const releases: Array<ValidationLevel | undefined> = [
    undefined,
    ...VALIDATION_LEVELS,
  ];

  for (const machine of MACHINE_VALIDATION_LEVELS) {
    releases.forEach((release, column) => {
      const verdict = allowed[machine][column];
      it(`${verdict ? "offers" : "refuses"} a ${release ?? "unrecorded"} version to a machine validated "${machine}"`, () => {
        expect(releaseAllowedOnMachine(release, machine)).toBe(verdict);
      });
    });
  }

  // ANH-195 EX-7. The types forbid these values; stored data does not.
  const unknownStates = [
    "OCCUPIED",
    "validated",
    "",
    "constructor",
    "__proto__",
    undefined,
    null,
    3,
  ];
  for (const state of unknownStates) {
    it(`ANH-195 EX-7 offers no version, whatever its level, to a machine in the unknown state ${JSON.stringify(state) ?? "undefined"}`, () => {
      const verdicts = releases.map((release) =>
        releaseAllowedOnMachine(release, state as MachineValidationLevel),
      );
      expect(verdicts).toEqual([false, false, false, false]);
    });
  }

  it("ANH-195 EX-7 counts a version level it does not know as no level", () => {
    const unknown = "validated" as ValidationLevel;
    const verdicts = MACHINE_VALIDATION_LEVELS.map((machine) =>
      releaseAllowedOnMachine(unknown, machine),
    );
    // As for an unrecorded version: only a machine with no validation to lose.
    expect(verdicts).toEqual([true, false, false, false]);
  });

  it("names the lowest level each machine state accepts", () => {
    expect(MACHINE_VALIDATION_LEVELS.map(requiredReleaseLevel)).toEqual([
      null,
      "bench",
      "auto_validated",
      "occupied_validated",
    ]);
  });

  it("recognises a released version by its component prefix", () => {
    expect(isReleaseVersion("pi", "pi-10.0.3")).toBe(true);
    expect(isReleaseVersion("cloud", "cloud-0.1.0")).toBe(true);
    expect(isReleaseVersion("web", "cloud-0.1.0")).toBe(false);
    expect(isReleaseVersion("pi", "xpi-0.1.0")).toBe(false);
  });
});

async function count(w: Awaited<ReturnType<typeof seedWorld>>) {
  const rows = await w.t.run((ctx) =>
    ctx.db.query("software_releases").collect(),
  );
  return rows.length;
}

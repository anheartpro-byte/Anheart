/// <reference types="vite/client" />
/**
 * ANH-134 EX-4 and EX-5: the register of released versions and the rule that
 * decides which machine may receive a Pi version. Who may call the two public
 * functions is in `authorization.matrix.ts`; this file checks what they accept
 * and store.
 */
import { describe, expect, it } from "vitest";
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
import { as, modules, NOW, seedWorld } from "./test.setup";

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

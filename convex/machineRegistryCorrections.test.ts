import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./_generated/api";
import { requireConfigurationAllowed } from "./lib/machineRegistry";
import { registryRelease, registryUpload } from "./machineRegistry.fixtures";
import { as, modules, NOW, seedWorld } from "./test.setup";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => vi.useRealTimers());

describe("ANH-147 registry corrections", () => {
  it.each(["reassigned", "without organization"] as const)(
    "hides unreadable rider sessions from an organisation admin when %s",
    async (scenario) => {
      // Given a session its reader cannot access, on a machine they can read.
      const w = await seedWorld(modules);
      const sessionId = await w.t.run(async (ctx) => {
        const id = await ctx.db.insert("sessions", {
          machineId: w.machine,
          organizationId: scenario === "reassigned" ? w.orgA : undefined,
          userId: w.patient,
          status: "completed",
          kind: "auto",
          startedAt: NOW - 1000,
          channels: [],
        });
        await ctx.db.patch(w.machine, {
          organizationId: scenario === "reassigned" ? w.orgB : w.orgA,
          programsEnabled: false,
        });
        return id;
      });
      const reader = as(
        w.t,
        scenario === "reassigned" ? "orgBAdmin" : "orgAdmin",
      );
      expect(
        await reader.query(api.training.getTrainingSession, { sessionId }),
      ).toBeNull();

      // When both authorised machine readers open the registry.
      const local = await reader.query(api.machineRegistry.getRegistry, {
        machineId: w.machine,
      });
      const global = await as(w.t, "admin").query(
        api.machineRegistry.getRegistry,
        { machineId: w.machine },
      );

      // Then only Anheart sees the session-derived information.
      expect(local?.alerts).toEqual([]);
      expect(global?.alerts).toEqual([
        {
          code: "person_on_board_without_occupied",
          count: 1,
          mostRecentAt: NOW - 1000,
        },
      ]);
    },
  );

  it("counts the older readable session when more than 50 newer sessions are unreadable", async () => {
    // Given 51 foreign sessions followed by one older readable auto session.
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      await ctx.db.patch(w.machine, { programsEnabled: false });
      for (let index = 0; index < 51; index += 1)
        await ctx.db.insert("sessions", {
          machineId: w.machine,
          organizationId: w.orgB,
          status: "completed",
          kind: "manual",
          startedAt: NOW - index,
          channels: [],
        });
      await ctx.db.insert("sessions", {
        machineId: w.machine,
        organizationId: w.orgA,
        status: "completed",
        kind: "auto",
        startedAt: NOW - 1000,
        channels: [],
      });
    });

    // When the organisation admin reads the machine registry.
    const registry = await as(w.t, "orgAdmin").query(
      api.machineRegistry.getRegistry,
      { machineId: w.machine },
    );

    // Then inaccessible sessions do not consume the reader's limit.
    expect(registry?.alerts).toEqual([
      {
        code: "person_on_board_without_occupied",
        count: 1,
        mostRecentAt: NOW - 1000,
      },
    ]);
  });

  it("fails closed throughout the public registry and server guard when stored auto has no reviews", async () => {
    // Given a directly inserted auto level without supporting reviews.
    const w = await seedWorld(modules);
    await registryRelease(w, "pi-0.1.0", "bench");
    await w.t.run(async (ctx) => {
      await ctx.db.patch(w.machine, { softwareVersion: "pi-0.1.0" });
      await ctx.db.insert("machine_registry", {
        machineId: w.machine,
        contacts: [],
        driveParameterSheetHistory: [],
        expectedSoftwareVersion: "pi-0.1.0",
        validation: { level: "auto", reviews: [], downgrades: [] },
        updatedAt: NOW,
        updatedBy: w.admin,
      });
    });
    const admin = as(w.t, "admin");

    // When all configuration decision surfaces read this state.
    const registry = await admin.query(api.machineRegistry.getRegistry, {
      machineId: w.machine,
    });
    const verdict = await admin.query(
      api.machineRegistry.checkConfigurationLock,
      {
        machineId: w.machine,
        key: "PROGRAMS_ENABLED",
        value: true,
      },
    );
    const badges = await admin.query(
      api.machineRegistry.listRegistryBadges,
      {},
    );

    // Then none is effective everywhere and enabling programs is refused.
    expect(registry).toMatchObject({
      validation: { level: "auto" },
      effectiveLevel: "none",
      versionStatus: "up_to_date",
      expectedVersionAllowed: true,
      configurationLocks: {
        PROGRAMS_ENABLED: {
          allowed: false,
          reachedLevel: "none",
          message: "machine validée jusqu'au niveau none",
        },
      },
      alerts: [
        { code: "programs_enabled_without_auto", count: 1, mostRecentAt: null },
      ],
    });
    expect(verdict).toMatchObject({
      allowed: false,
      reachedLevel: "none",
      message: "machine validée jusqu'au niveau none",
    });
    expect(badges.find((entry) => entry.machineId === w.machine)).toEqual({
      machineId: w.machine,
      level: "none",
      versionStatus: "up_to_date",
      configurationStatus: "unavailable",
    });
    await expect(
      w.t.run((ctx) =>
        requireConfigurationAllowed(ctx, w.machine, "PROGRAMS_ENABLED", true),
      ),
    ).rejects.toThrow("machine_validation_insufficient");
    expect(
      await admin.query(api.machineRegistry.checkConfigurationLock, {
        machineId: w.machine,
        key: "PROGRAMS_ENABLED",
        value: false,
      }),
    ).toMatchObject({ allowed: true, reachedLevel: "none" });
    await expect(
      w.t.run((ctx) =>
        requireConfigurationAllowed(ctx, w.machine, "PROGRAMS_ENABLED", false),
      ),
    ).resolves.toBeNull();
  });

  it("repairs a stored auto level without reviews through the next effective bench review", async () => {
    const w = await seedWorld(modules);
    await registryRelease(w);
    await w.t.run((ctx) =>
      ctx.db.insert("machine_registry", {
        machineId: w.machine,
        contacts: [],
        driveParameterSheetHistory: [],
        validation: { level: "auto", reviews: [], downgrades: [] },
        updatedAt: NOW,
        updatedBy: w.admin,
      }),
    );
    await as(w.t, "admin").mutation(api.machineRegistry.addValidationReview, {
      machineId: w.machine,
      level: "bench",
      reportStorageId: await registryUpload(w),
      softwareVersion: "pi-0.1.0",
      medicalParametersVersion: null,
    });
    expect(
      await as(w.t, "admin").query(api.machineRegistry.getRegistry, {
        machineId: w.machine,
      }),
    ).toMatchObject({
      effectiveLevel: "bench",
      validation: { level: "bench", reviews: [{ level: "bench" }] },
    });
  });

  it("counts every rider session at none when the current history and stored level disagree", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      const reportStorageId = await ctx.storage.store(new Blob(["TEST"]));
      await ctx.db.patch(w.machine, { programsEnabled: false });
      await ctx.db.insert("machine_registry", {
        machineId: w.machine,
        contacts: [],
        driveParameterSheetHistory: [],
        validation: {
          level: "auto",
          reviews: (["bench", "auto", "occupied"] as const).map(
            (level, at) => ({
              level,
              at,
              by: w.admin,
              reportStorageId,
              softwareVersion: "pi-0.1.0",
              medicalParametersVersion: "TEST-MED",
            }),
          ),
          downgrades: [],
        },
        updatedAt: NOW,
        updatedBy: w.admin,
      });
      await ctx.db.insert("sessions", {
        machineId: w.machine,
        organizationId: w.orgA,
        kind: "auto",
        status: "completed",
        startedAt: NOW - 1000,
        channels: [],
      });
    });
    expect(
      (
        await as(w.t, "orgAdmin").query(api.machineRegistry.getRegistry, {
          machineId: w.machine,
        })
      )?.alerts,
    ).toEqual([
      {
        code: "person_on_board_without_occupied",
        count: 1,
        mostRecentAt: NOW - 1000,
      },
    ]);
  });

  it("refuses a review that would leave an inconsistent history and refuses lowering from effective none", async () => {
    const w = await seedWorld(modules);
    await registryRelease(w);
    const reportStorageId = await registryUpload(w);
    await w.t.run((ctx) =>
      ctx.db.insert("machine_registry", {
        machineId: w.machine,
        contacts: [],
        driveParameterSheetHistory: [],
        validation: {
          level: "auto",
          reviews: [
            {
              level: "bench",
              at: NOW - 1000,
              by: w.admin,
              reportStorageId,
              softwareVersion: "pi-0.1.0",
              medicalParametersVersion: null,
            },
          ],
          downgrades: [],
        },
        updatedAt: NOW,
        updatedBy: w.admin,
      }),
    );
    const admin = as(w.t, "admin");
    await expect(
      admin.mutation(api.machineRegistry.addValidationReview, {
        machineId: w.machine,
        level: "bench",
        reportStorageId: await registryUpload(w),
        softwareVersion: "pi-0.1.0",
        medicalParametersVersion: null,
      }),
    ).rejects.toThrow("validation_level_not_next");
    await expect(
      admin.mutation(api.machineRegistry.lowerValidationLevel, {
        machineId: w.machine,
        level: "none",
        reason: "TEST lower corrupt level",
      }),
    ).rejects.toThrow("validation_level_not_lower");
    expect(
      await admin.query(api.machineRegistry.getRegistry, {
        machineId: w.machine,
      }),
    ).toMatchObject({
      effectiveLevel: "none",
      validation: {
        level: "auto",
        reviews: [{ level: "bench" }],
        downgrades: [],
      },
    });
  });
});

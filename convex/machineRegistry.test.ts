import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import type { Doc, Id } from "./_generated/dataModel";
import {
  as,
  modules,
  NOW,
  seedLaunchWorld,
  seedWorld,
  type World,
} from "./test.setup";
import { registryRelease, registryUpload } from "./machineRegistry.fixtures";
import { levelAt } from "./lib/machineRegistry";
import { pendingSessionTtlMs } from "./training";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
});

const read = (w: World) =>
  as(w.t, "admin").query(api.machineRegistry.getRegistry, {
    machineId: w.machine,
  });

async function review(w: World, level: "bench" | "auto" | "occupied") {
  return await as(w.t, "admin").mutation(
    api.machineRegistry.addValidationReview,
    {
      machineId: w.machine,
      level,
      reportStorageId: await registryUpload(w),
      softwareVersion: "pi-0.1.0",
      medicalParametersVersion: null,
    },
  );
}

async function bench(w: World) {
  await registryRelease(w);
  await review(w, "bench");
}

describe("ANH-147 machine registry", () => {
  it("reads a missing registry as none with unavailable configuration and no invented geometry", async () => {
    const w = await seedWorld(modules);
    expect(await read(w)).toMatchObject({
      machineId: w.machine,
      validation: { level: "none", reviews: [], downgrades: [] },
      contacts: [],
      driveParameterSheet: null,
      driveParameterSheetHistory: [],
      updatedAt: null,
      updatedBy: null,
      versionStatus: "unknown",
      expectedVersionAllowed: null,
      configurationStatus: "unavailable",
      configurationProposal: { ARM_RADIUS_M: null, LEG_TIP_RADIUS_M: null },
      medicalKeyIsTest: true,
      canEdit: true,
    });
    expect(
      (
        await as(w.t, "orgAdmin").query(api.machineRegistry.getRegistry, {
          machineId: w.machine,
        })
      )?.canEdit,
    ).toBe(false);
  });

  it("passes none to bench to auto using two attached reviews and enforces the acceptance locks", async () => {
    const w = await seedWorld(modules);
    await bench(w);
    const lock = (
      key: "PROGRAMS_ENABLED" | "OCCUPANCY_OCCUPIED_ENABLED",
      value = true,
    ) =>
      as(w.t, "admin").query(api.machineRegistry.checkConfigurationLock, {
        machineId: w.machine,
        key,
        value,
      });
    expect(await lock("PROGRAMS_ENABLED")).toMatchObject({
      allowed: false,
      message: "machine validée jusqu'au niveau bench",
    });
    vi.setSystemTime(NOW + 1);
    await review(w, "auto");
    expect(await lock("PROGRAMS_ENABLED")).toMatchObject({
      allowed: true,
      message: null,
    });
    expect(await lock("OCCUPANCY_OCCUPIED_ENABLED")).toEqual({
      allowed: false,
      requiredLevel: "occupied",
      reachedLevel: "auto",
      message: "machine validée jusqu'au niveau auto",
    });
    expect(await lock("OCCUPANCY_OCCUPIED_ENABLED", false)).toMatchObject({
      allowed: true,
    });
    const registry = await read(w);
    expect(registry?.validation.reviews.map((entry) => entry.level)).toEqual([
      "bench",
      "auto",
    ]);
    for (const entry of registry?.validation.reviews ?? []) {
      expect(entry.by).toBe(w.admin);
      expect(entry.reportUrl).toBeTypeOf("string");
      expect(
        await w.t.run(
          async (ctx) => (await ctx.storage.get(entry.reportStorageId))?.size,
        ),
      ).toBe(12);
    }
    expect(registry?.validation.level).toBe("auto");
  });

  it("refuses skips, repeated reviews and occupied with the embedded test key", async () => {
    const w = await seedWorld(modules);
    await registryRelease(w);
    await expect(review(w, "auto")).rejects.toThrow(
      "validation_level_not_next",
    );
    await review(w, "bench");
    await expect(review(w, "bench")).rejects.toThrow(
      "validation_level_not_next",
    );
    await expect(review(w, "occupied")).rejects.toThrow(
      "validation_level_not_next",
    );
    await review(w, "auto");
    await expect(review(w, "occupied")).rejects.toThrow(
      "occupied_requires_signed_medical_parameters",
    );
    expect((await read(w))?.validation.reviews).toHaveLength(2);
  });

  it("requires a known software and medical version on reviews", async () => {
    const w = await seedWorld(modules);
    const args = {
      machineId: w.machine,
      level: "bench" as const,
      reportStorageId: await registryUpload(w),
      softwareVersion: "pi-0.1.0",
      medicalParametersVersion: null,
    };
    await expect(
      as(w.t, "admin").mutation(api.machineRegistry.addValidationReview, args),
    ).rejects.toThrow("unknown_software_version");
    await registryRelease(w);
    await expect(
      as(w.t, "admin").mutation(api.machineRegistry.addValidationReview, {
        ...args,
        medicalParametersVersion: "TEST-UNKNOWN",
      }),
    ).rejects.toThrow("unknown_medical_parameters_version");
    await as(w.t, "admin").mutation(api.machineRegistry.addValidationReview, {
      ...args,
      medicalParametersVersion: "0.1.0",
    });
    expect(
      (await read(w))?.validation.reviews[0].medicalParametersVersion,
    ).toBe("0.1.0");
  });

  it("records an admin downgrade with its reason and reconstructs historical validation", async () => {
    const w = await seedWorld(modules);
    await bench(w);
    vi.setSystemTime(NOW + 10);
    await review(w, "auto");
    const args = {
      machineId: w.machine,
      level: "bench" as const,
      reason: "TEST: nouvel essai requis",
    };
    for (const reason of ["", " x ", "x".repeat(501)])
      await expect(
        as(w.t, "admin").mutation(api.machineRegistry.lowerValidationLevel, {
          ...args,
          reason,
        }),
      ).rejects.toThrow("downgrade_reason_required");
    for (const level of ["auto", "occupied"] as const)
      await expect(
        as(w.t, "admin").mutation(api.machineRegistry.lowerValidationLevel, {
          ...args,
          level,
        }),
      ).rejects.toThrow("validation_level_not_lower");
    await expect(
      as(w.t, "orgAdmin").mutation(
        api.machineRegistry.lowerValidationLevel,
        args,
      ),
    ).rejects.toThrow("not_authorized");
    vi.setSystemTime(NOW + 20);
    await as(w.t, "admin").mutation(api.machineRegistry.lowerValidationLevel, {
      ...args,
      reason: `  ${args.reason}  `,
    });
    const registry = await read(w);
    expect(registry?.validation).toMatchObject({
      level: "bench",
      downgrades: [
        {
          from: "auto",
          to: "bench",
          by: w.admin,
          at: NOW + 20,
          reason: args.reason,
        },
      ],
    });
    expect(levelAt(registry!.validation, NOW + 15)).toBe("auto");
    expect(levelAt(registry!.validation, NOW + 20)).toBe("bench");
  });

  it("normalizes identity, preserves absent fields and clears null fields", async () => {
    const w = await seedWorld(modules);
    await registryRelease(w);
    await as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
      machineId: w.machine,
      serialPi: " TEST-PI-0001 ",
      serialDrive: "TEST-DRIVE",
      serialMotor: "TEST-MOTOR",
      serialGearbox: "TEST-GEARBOX",
      bitalinoAddress: " aa:bb:cc:dd:ee:ff ",
      site: " Adresse de test ",
      contacts: [w.orgAdmin, w.manager],
      commissionedAt: NOW,
      expectedSoftwareVersion: " pi-0.1.0 ",
    });
    expect(await read(w)).toMatchObject({
      serialPi: "TEST-PI-0001",
      bitalinoAddress: "AA:BB:CC:DD:EE:FF",
      site: "Adresse de test",
      commissionedAt: NOW,
      expectedSoftwareVersion: "pi-0.1.0",
      updatedBy: w.admin,
    });
    expect((await read(w))?.contacts.map((contact) => contact.userId)).toEqual([
      w.orgAdmin,
      w.manager,
    ]);
    for (const contact of (await read(w))!.contacts)
      expect(Object.keys(contact).sort()).toEqual([
        "firstName",
        "lastName",
        "userId",
      ]);
    await as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
      machineId: w.machine,
      serialPi: null,
      serialDrive: null,
      serialMotor: null,
      serialGearbox: null,
      bitalinoAddress: null,
      commissionedAt: null,
      contacts: null,
      expectedSoftwareVersion: null,
    });
    const registry = await read(w);
    expect(registry?.serialPi).toBeUndefined();
    expect(registry?.bitalinoAddress).toBeUndefined();
    expect(registry?.commissionedAt).toBeUndefined();
    expect(registry?.expectedSoftwareVersion).toBeUndefined();
    expect(registry?.contacts).toEqual([]);
    expect(registry?.site).toBe("Adresse de test");
    await as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
      machineId: w.machine,
      site: null,
    });
    expect((await read(w))?.site).toBeUndefined();
  });

  it("rejects malformed identity, timestamps, contacts and unknown versions without writing", async () => {
    const w = await seedWorld(modules);
    for (const fields of [
      { serialPi: " " },
      { expectedSoftwareVersion: " " },
      { expectedSoftwareVersion: "x".repeat(201) },
      { serialDrive: "x".repeat(201) },
      { site: "x".repeat(501) },
      { bitalinoAddress: "aa:bb:cc:dd:ee:gg" },
      { contacts: [w.manager, w.manager] },
      { contacts: Array.from({ length: 21 }, () => w.manager) },
      ...[
        NaN,
        Infinity,
        NOW + 0.5,
        Date.UTC(2023, 11, 31),
        NOW + 86_400_001,
      ].map((commissionedAt) => ({ commissionedAt })),
    ])
      await expect(
        as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
          machineId: w.machine,
          ...fields,
        }),
      ).rejects.toThrow("invalid_registry_field");
    for (const expectedSoftwareVersion of [
      "garbage",
      "cloud-0.1.0",
      "pi-9.9.9",
    ])
      await expect(
        as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
          machineId: w.machine,
          expectedSoftwareVersion,
        }),
      ).rejects.toThrow("unknown_software_version");
    await expect(
      as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
        machineId: w.machine,
        contacts: [w.orgBAdmin],
      }),
    ).rejects.toThrow("contact_not_in_organization");
    await w.t.run(async (ctx) => {
      const member = await ctx.db
        .query("memberships")
        .withIndex("by_user_and_organization", (q) =>
          q.eq("userId", w.manager).eq("organizationId", w.orgA),
        )
        .unique();
      await ctx.db.patch(member!._id, { active: false });
    });
    await expect(
      as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
        machineId: w.machine,
        contacts: [w.manager],
      }),
    ).rejects.toThrow("contact_not_in_organization");
    await w.t.run((ctx) => ctx.db.patch(w.patient, { clerkDeletedAt: NOW }));
    await expect(
      as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
        machineId: w.machine,
        contacts: [w.patient],
      }),
    ).rejects.toThrow("contact_not_in_organization");
    const missingContact = await w.t.run(async (ctx) => {
      await ctx.db.delete(w.stranger);
      return w.stranger;
    });
    await expect(
      as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
        machineId: w.machine,
        contacts: [missingContact],
      }),
    ).rejects.toThrow("contact_not_in_organization");
    expect(
      await w.t.run((ctx) => ctx.db.query("machine_registry").collect()),
    ).toEqual([]);
  });

  it("proposes measured radii and checks plausibility, date and measurer", async () => {
    const w = await seedWorld(modules);
    const args = {
      machineId: w.machine,
      armRadiusM: 1.234,
      legTipRadiusM: 2.345,
      measuredAt: NOW,
      measuredBy: " TEST mesureur ",
    };
    for (const bad of [NaN, Infinity, 0, -1, 10.001]) {
      for (const key of ["armRadiusM", "legTipRadiusM"] as const)
        await expect(
          as(w.t, "admin").mutation(api.machineRegistry.recordRadii, {
            ...args,
            [key]: bad,
          }),
        ).rejects.toThrow("invalid_registry_field");
    }
    for (const fields of [
      { measuredBy: " " },
      { measuredBy: "x".repeat(201) },
      { measuredAt: NOW + 86_400_001 },
    ])
      await expect(
        as(w.t, "admin").mutation(api.machineRegistry.recordRadii, {
          ...args,
          ...fields,
        }),
      ).rejects.toThrow("invalid_registry_field");
    await as(w.t, "admin").mutation(api.machineRegistry.recordRadii, args);
    expect(await read(w)).toMatchObject({
      armRadiusM: 1.234,
      legTipRadiusM: 2.345,
      radiiMeasuredBy: "TEST mesureur",
      radiiMeasuredAt: NOW,
      configurationProposal: { ARM_RADIUS_M: 1.234, LEG_TIP_RADIUS_M: 2.345 },
    });
    await as(w.t, "admin").mutation(api.machineRegistry.recordRadii, {
      ...args,
      armRadiusM: 10,
      measuredAt: NOW + 86_400_000,
    });
    expect((await read(w))?.armRadiusM).toBe(10);
  });

  it("keeps replaced PDF/JSON sheets and their URLs without deleting storage", async () => {
    const w = await seedWorld(modules);
    const first = await registryUpload(w);
    const second = await registryUpload(w, "application/json");
    const attach = (storageId: Id<"_storage">, version: string) =>
      as(w.t, "admin").mutation(api.machineRegistry.attachDriveParameterSheet, {
        machineId: w.machine,
        storageId,
        version,
        signedBy: " TEST signataire ",
        at: NOW,
      });
    await attach(first, " TEST-v1 ");
    await attach(second, "TEST-v2");
    const registry = await read(w);
    expect(registry?.driveParameterSheet).toMatchObject({
      storageId: second,
      contentType: "application/json",
      attachedBy: w.admin,
      signedBy: "TEST signataire",
      version: "TEST-v2",
    });
    expect(registry?.driveParameterSheet?.url).toBeTypeOf("string");
    expect(registry?.driveParameterSheetHistory[0]).toMatchObject({
      storageId: first,
      version: "TEST-v1",
      contentType: "application/pdf",
    });
    expect(registry?.driveParameterSheetHistory[0].url).toBeTypeOf("string");
    expect(
      await w.t.run(async (ctx) => (await ctx.storage.get(first))?.size),
    ).toBe(12);
    await expect(attach(first, "TEST-v3")).rejects.toThrow(
      "invalid_registry_file",
    );
    await expect(attach(second, "TEST-v3")).rejects.toThrow(
      "invalid_registry_file",
    );
  });

  it("refuses missing, wrong-type, oversized, old and reused review files", async () => {
    const w = await seedWorld(modules);
    await registryRelease(w);
    const absent = await registryUpload(w);
    await w.t.run((ctx) => ctx.storage.delete(absent));
    const wrong = await registryUpload(w, "text/plain");
    const large = await registryUpload(
      w,
      "application/pdf",
      10 * 1024 * 1024 + 1,
    );
    const old = await registryUpload(w);
    vi.setSystemTime(NOW + 3_600_001);
    const args = {
      machineId: w.machine,
      level: "bench" as const,
      softwareVersion: "pi-0.1.0",
      medicalParametersVersion: null,
    };
    for (const reportStorageId of [absent, wrong, large, old])
      await expect(
        as(w.t, "admin").mutation(api.machineRegistry.addValidationReview, {
          ...args,
          reportStorageId,
        }),
      ).rejects.toThrow("invalid_registry_file");
    const used = await registryUpload(w);
    await as(w.t, "admin").mutation(api.machineRegistry.addValidationReview, {
      ...args,
      reportStorageId: used,
    });
    await expect(
      as(w.t, "admin").mutation(api.machineRegistry.attachDriveParameterSheet, {
        machineId: w.otherMachine,
        storageId: used,
        version: "TEST-v1",
        signedBy: "TEST",
        at: NOW,
      }),
    ).rejects.toThrow("invalid_registry_file");
    await expect(
      as(w.t, "admin").mutation(api.machineRegistry.addValidationReview, {
        ...args,
        level: "auto",
        reportStorageId: used,
      }),
    ).rejects.toThrow("invalid_registry_file");
  });

  it("validates sheet metadata and the exact upload-age and size bounds", async () => {
    const w = await seedWorld(modules);
    const storageId = await registryUpload(
      w,
      "application/json",
      10 * 1024 * 1024,
    );
    const args = {
      machineId: w.machine,
      storageId,
      version: "TEST-v1",
      signedBy: "TEST",
      at: NOW,
    };
    for (const fields of [
      { version: "" },
      { version: "x".repeat(51) },
      { signedBy: "" },
      { signedBy: "x".repeat(201) },
      { at: NOW + 86_400_001 },
    ])
      await expect(
        as(w.t, "admin").mutation(
          api.machineRegistry.attachDriveParameterSheet,
          { ...args, ...fields },
        ),
      ).rejects.toThrow("invalid_registry_field");
    vi.setSystemTime(NOW + 3_599_999);
    await as(w.t, "admin").mutation(
      api.machineRegistry.attachDriveParameterSheet,
      args,
    );
    expect((await read(w))?.driveParameterSheet?.storageId).toBe(storageId);
  });

  it("compares software versions and flags an expected version without blocking its selection", async () => {
    const w = await seedWorld(modules);
    await bench(w);
    await registryRelease(w, "pi-0.2.0", "bench");
    const setExpected = (expectedSoftwareVersion: string) =>
      as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
        machineId: w.machine,
        expectedSoftwareVersion,
      });
    await setExpected("pi-0.1.0");
    expect((await read(w))?.versionStatus).toBe("unknown");
    await w.t.run((ctx) =>
      ctx.db.patch(w.machine, { softwareVersion: "pi-0.1.0" }),
    );
    expect(await read(w)).toMatchObject({
      versionStatus: "up_to_date",
      expectedVersionAllowed: true,
    });
    await setExpected("pi-0.2.0");
    expect((await read(w))?.versionStatus).toBe("outdated");
    await review(w, "auto");
    expect(await read(w)).toMatchObject({
      versionStatus: "outdated",
      expectedVersionAllowed: false,
    });
    await w.t.run((ctx) =>
      ctx.db.patch(w.machine, { softwareVersion: "pi-0.2.0" }),
    );
    expect(await read(w)).toMatchObject({
      versionStatus: "not_validated",
      expectedVersionAllowed: false,
    });
    await w.t.run((ctx) =>
      ctx.db.patch(w.machine, { softwareVersion: "pi-9.9.9" }),
    );
    expect((await read(w))?.versionStatus).toBe("not_validated");
  });

  it("flags bench software on an auto machine without an expected version in the registry and badges", async () => {
    const w = await seedWorld(modules);
    await bench(w);
    await review(w, "auto");
    await registryRelease(w, "pi-0.2.0", "bench");
    await w.t.run((ctx) =>
      ctx.db.patch(w.machine, { softwareVersion: "pi-0.2.0" }),
    );
    const registry = await read(w);
    expect(registry?.expectedSoftwareVersion).toBeUndefined();
    expect(registry).toMatchObject({
      versionStatus: "not_validated",
      expectedVersionAllowed: null,
    });
    expect(
      (
        await as(w.t, "admin").query(api.machineRegistry.listRegistryBadges, {})
      ).find((entry) => entry.machineId === w.machine)?.versionStatus,
    ).toBe("not_validated");
  });

  it.each([
    "pending",
    "cancelled",
    "expired",
    "active",
    "completed",
    "failed",
  ] as const)(
    "counts a remote auto session only after arming: %s",
    async (state) => {
      const w = await seedLaunchWorld(modules);
      await bench(w);
      await review(w, "auto");
      const sessionId = await as(w.t, "manager").mutation(
        api.training.launchAutoSession,
        {
          machineId: w.machine,
          profileId: w.profileId,
          userId: w.patient,
        },
      );
      expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
        kind: "auto",
        origin: "remote",
        status: "pending",
      });
      if (state === "cancelled") {
        await as(w.t, "manager").mutation(api.training.requestStop, {
          sessionId,
        });
        const cancelled = await w.t.run((ctx) => ctx.db.get(sessionId));
        expect(cancelled).toMatchObject({
          status: "failed",
          endReason: "Cancelled before start by manager Synthetic",
        });
        expect(cancelled?.endCode).toBeUndefined();
      } else if (state === "expired") {
        vi.setSystemTime(NOW + pendingSessionTtlMs() + 1);
        expect(
          await w.t.mutation(internal.training.markTrainingStarted, {
            machineId: w.machine,
            sessionId,
          }),
        ).toMatchObject({ armed: false, code: "expired_before_start" });
        expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
          status: "failed",
          endCode: "expired_before_start",
        });
      } else if (
        state === "active" ||
        state === "completed" ||
        state === "failed"
      ) {
        expect(
          await w.t.mutation(internal.training.markTrainingStarted, {
            machineId: w.machine,
            sessionId,
          }),
        ).toEqual({ armed: true });
        if (state !== "active")
          await w.t.mutation(internal.training.endTrainingSession, {
            machineId: w.machine,
            sessionId,
            failed: state === "failed",
            reason: "TEST end after arming",
          });
        expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
          status: state,
        });
      }
      const ran =
        state === "active" || state === "completed" || state === "failed";
      expect((await read(w))?.alerts).toEqual(
        ran
          ? [
              {
                code: "person_on_board_without_occupied",
                count: 1,
                mostRecentAt: NOW,
              },
            ]
          : [],
      );
      expect(
        (
          await as(w.t, "admin").query(
            api.machineRegistry.listRegistryBadges,
            {},
          )
        ).find((entry) => entry.machineId === w.machine)?.alerts,
      ).toEqual(ran ? ["person_on_board_without_occupied"] : []);
    },
  );

  it("reports programs and historical rider sessions, not an occupied session before a downgrade", async () => {
    const w = await seedWorld(modules);
    await bench(w);
    await w.t.run((ctx) => ctx.db.patch(w.machine, { programsEnabled: true }));
    const stored = await w.t.run((ctx) =>
      ctx.db
        .query("machine_registry")
        .withIndex("by_machine", (q) => q.eq("machineId", w.machine))
        .unique(),
    );
    const validation: Doc<"machine_registry">["validation"] = {
      level: "bench",
      reviews: [
        stored!.validation.reviews[0],
        { ...stored!.validation.reviews[0], level: "auto", at: NOW + 10 },
        {
          ...stored!.validation.reviews[0],
          level: "occupied",
          at: NOW + 20,
          medicalParametersVersion: "0.1.0",
        },
      ],
      downgrades: [
        {
          from: "occupied",
          to: "bench",
          by: w.admin,
          at: NOW + 40,
          reason: "TEST downgrade",
        },
      ],
    };
    await w.t.run(async (ctx) => {
      await ctx.db.patch(stored!._id, { validation });
      for (const [startedAt, kind, notes] of [
        [NOW + 5, "auto", undefined],
        [NOW + 6, "manual", "TEST; Occupancy: occupied"],
        [NOW + 30, "manual", "Occupancy: occupied"],
        [NOW + 50, "manual", "Occupancy: empty"],
      ] as const)
        await ctx.db.insert("sessions", {
          machineId: w.machine,
          organizationId: w.orgA,
          status: "completed",
          startedAt,
          kind,
          notes,
          channels: [],
        });
    });
    expect((await read(w))?.alerts).toEqual([
      { code: "programs_enabled_without_auto", count: 1, mostRecentAt: null },
      {
        code: "person_on_board_without_occupied",
        count: 2,
        mostRecentAt: NOW + 6,
      },
    ]);
    expect(
      (
        await as(w.t, "orgAdmin").query(
          api.machineRegistry.listRegistryBadges,
          {},
        )
      ).find((entry) => entry.machineId === w.machine)?.alerts,
    ).toEqual([
      "programs_enabled_without_auto",
      "person_on_board_without_occupied",
    ]);
  });

  it("limits rider alerts to the latest 50 sessions", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      for (let n = 0; n < 51; n++)
        await ctx.db.insert("sessions", {
          machineId: w.machine,
          status: "completed",
          kind: n === 0 ? "auto" : "manual",
          startedAt: NOW + n,
          channels: [],
        });
    });
    expect((await read(w))?.alerts.map((alert) => alert.code)).toEqual([
      "programs_enabled_without_auto",
    ]);
  });

  it("isolates reads and badges by organization and hides deleted machines except from admin", async () => {
    const w = await seedWorld(modules);
    for (const actor of [
      "manager",
      "patient",
      "stranger",
      "orgBAdmin",
      "orgBManager",
      "orgBPatient",
    ] as const) {
      expect(
        await as(w.t, actor).query(api.machineRegistry.getRegistry, {
          machineId: w.machine,
        }),
      ).toBeNull();
      expect(
        await as(w.t, actor).query(api.machineRegistry.checkConfigurationLock, {
          machineId: w.machine,
          key: "PROGRAMS_ENABLED",
          value: true,
        }),
      ).toBeNull();
    }
    for (const actor of [
      "manager",
      "patient",
      "stranger",
      "orgBManager",
      "orgBPatient",
    ] as const)
      expect(
        await as(w.t, actor).query(api.machineRegistry.listRegistryBadges, {}),
      ).toEqual([]);
    expect(
      (
        await as(w.t, "orgBAdmin").query(
          api.machineRegistry.listRegistryBadges,
          {},
        )
      ).map((entry) => entry.machineId),
    ).toEqual([w.orgBMachine]);
    expect(
      await as(w.t, "admin").mutation(
        api.machineRegistry.generateUploadUrl,
        {},
      ),
    ).toMatch(/^https?:\/\//);
    await w.t.run((ctx) => ctx.db.patch(w.machine, { isDeleted: true }));
    expect(await read(w)).not.toBeNull();
    expect(
      await as(w.t, "orgAdmin").query(api.machineRegistry.getRegistry, {
        machineId: w.machine,
      }),
    ).toBeNull();
    expect(
      (await as(w.t, "admin").query(api.machineRegistry.listRegistryBadges, {}))
        .map((entry) => entry.machineId)
        .sort(),
    ).toEqual([w.otherMachine, w.orgBMachine].sort());
    await expect(
      as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
        machineId: w.machine,
        serialPi: "TEST",
      }),
    ).rejects.toThrow("machine_not_found");
    await w.t.run((ctx) => ctx.db.delete(w.machine));
    expect(await read(w)).toBeNull();
    await expect(
      as(w.t, "admin").mutation(api.machineRegistry.recordRadii, {
        machineId: w.machine,
        armRadiusM: 1,
        legTipRadiusM: 2,
        measuredAt: NOW,
        measuredBy: "TEST",
      }),
    ).rejects.toThrow("machine_not_found");
    await expect(
      w.t.query(api.machineRegistry.getRegistry, { machineId: w.machine }),
    ).rejects.toThrow("not_authenticated");
  });

  it("updates organization on every write and hides contacts that ceased membership", async () => {
    const w = await seedWorld(modules);
    await as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
      machineId: w.machine,
      contacts: [w.manager],
    });
    await w.t.run((ctx) => ctx.db.patch(w.machine, { organizationId: w.orgB }));
    await as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
      machineId: w.machine,
      serialPi: "TEST-PI-REASSIGNED",
    });
    const registry = await read(w);
    expect(registry?.organizationId).toBe(w.orgB);
    expect(registry?.contacts).toEqual([]);
    expect(
      await as(w.t, "orgAdmin").query(api.machineRegistry.getRegistry, {
        machineId: w.machine,
      }),
    ).toBeNull();
    expect(
      (
        await as(w.t, "orgBAdmin").query(api.machineRegistry.getRegistry, {
          machineId: w.machine,
        })
      )?.serialPi,
    ).toBe("TEST-PI-REASSIGNED");
  });
});

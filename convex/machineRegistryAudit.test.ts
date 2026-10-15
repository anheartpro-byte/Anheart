import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./_generated/api";
import { registryRelease, registryUpload } from "./machineRegistry.fixtures";
import { as, modules, NOW, seedWorld } from "./test.setup";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => vi.useRealTimers());

describe("ANH-147 registry authors", () => {
  it.each(["deleted", "missing"] as const)(
    "resolves every audit author without email and keeps only the identifier when %s",
    async (state) => {
      const w = await seedWorld(modules);
      const admin = as(w.t, "admin");
      await registryRelease(w);
      await admin.mutation(api.machineRegistry.addValidationReview, {
        machineId: w.machine,
        level: "bench",
        reportStorageId: await registryUpload(w),
        softwareVersion: "pi-0.1.0",
        medicalParametersVersion: null,
      });
      await admin.mutation(api.machineRegistry.lowerValidationLevel, {
        machineId: w.machine,
        level: "none",
        reason: "TEST audit downgrade",
      });
      for (const version of ["TEST-v1", "TEST-v2"])
        await admin.mutation(api.machineRegistry.attachDriveParameterSheet, {
          machineId: w.machine,
          storageId: await registryUpload(w),
          version,
          signedBy: "TEST signataire",
          at: NOW,
        });
      const authors = (
        registry: NonNullable<Awaited<ReturnType<typeof read>>>,
      ) => [
        registry.updatedBy,
        registry.validation.reviews[0].by,
        registry.validation.downgrades[0].by,
        registry.driveParameterSheet?.attachedBy,
        registry.driveParameterSheetHistory[0].attachedBy,
      ];
      const read = () =>
        as(w.t, "orgAdmin").query(api.machineRegistry.getRegistry, {
          machineId: w.machine,
        });
      const before = await read();
      expect(before).not.toBeNull();
      if (before === null) throw new Error("TEST registry missing");
      expect(authors(before)).toEqual(
        Array(5).fill({
          userId: w.admin,
          firstName: "admin",
          lastName: "Synthetic",
        }),
      );
      await w.t.run(async (ctx) => {
        if (state === "deleted")
          await ctx.db.patch(w.admin, { clerkDeletedAt: NOW });
        else await ctx.db.delete(w.admin);
      });
      const after = await read();
      expect(after).not.toBeNull();
      if (after === null) throw new Error("TEST registry missing");
      expect(authors(after)).toEqual(Array(5).fill({ userId: w.admin }));
    },
  );
});

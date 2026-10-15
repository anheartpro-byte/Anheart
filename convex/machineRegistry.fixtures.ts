import type { GenericDataModel, GenericMutationCtx } from "convex/server";
import type { World } from "./test.setup";

export async function registryUpload(
  w: World,
  type = "application/pdf",
  size = 12,
) {
  const storageId = await w.t.run((ctx) =>
    ctx.storage.store(new Blob([new Uint8Array(size)], { type })),
  );
  await w.t.mutation(async (ctx: GenericMutationCtx<GenericDataModel>) => {
    await ctx.db.patch(storageId, { contentType: type });
  });
  return storageId;
}

export async function registryRelease(
  w: World,
  version = "pi-0.1.0",
  validationLevel:
    | "bench"
    | "auto_validated"
    | "occupied_validated" = "auto_validated",
) {
  return await w.t.run((ctx) =>
    ctx.db.insert("software_releases", {
      component: "pi",
      version,
      validationLevel,
      releasedAt: Date.now(),
      updatedAt: Date.now(),
      recordedBy: w.admin,
    }),
  );
}

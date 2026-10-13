import { internalMutationGeneric } from "convex/server";
import { v } from "convex/values";

// convex-test 0.0.60 storeBlob omits contentType, unlike native uploads.
// This test-only mutation restores that native metadata; never deployed.
export const nativeUploadType = internalMutationGeneric({
  args: { storageId: v.id("_storage"), contentType: v.string() },
  returns: v.null(),
  handler: async (ctx, args) => {
    await ctx.db.patch(args.storageId, { contentType: args.contentType });
    return null;
  },
});

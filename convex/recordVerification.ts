import { v } from "convex/values";
import { internalAction } from "./_generated/server";
import { internal } from "./_generated/api";
import {
  depositManifest,
  MAX_RECORD_FILE_BYTES,
  MANIFEST_PAGE_SIZE,
  recordBucket,
  sha256,
} from "./lib/recordStorage";

export const verify = internalAction({
  args: { fileId: v.id("session_record_files"), storageId: v.id("_storage") },
  returns: v.null(),
  handler: async (ctx, args) => {
    const file = await ctx.runQuery(
      internal.recordStorage.verificationInput,
      args,
    );
    if (!file) return null;
    const blob = await ctx.storage.get(args.storageId);
    let rejection: string | undefined;
    let expected: string[] | undefined;
    if (!blob || blob.size !== file.size || blob.size > MAX_RECORD_FILE_BYTES)
      rejection = "size_mismatch";
    else {
      const bytes = await blob.arrayBuffer();
      if ((await sha256(bytes)) !== file.sha256) rejection = "sha256_mismatch";
      else if (file.path === "manifest.json") {
        let parsed: unknown;
        try {
          parsed = JSON.parse(
            new TextDecoder("utf-8", { fatal: true }).decode(bytes),
          );
        } catch {
          rejection = "invalid_manifest";
        }
        const manifest = depositManifest.safeParse(parsed);
        if (manifest.success) expected = manifest.data.files;
        else rejection = "invalid_manifest";
      }
    }
    if (expected !== undefined && rejection === undefined) {
      const buckets = new Map<number, string[]>();
      for (const path of expected) {
        const bucket = recordBucket(path);
        const paths = buckets.get(bucket) ?? [];
        paths.push(path);
        buckets.set(bucket, paths);
      }
      for (const [bucket, paths] of buckets) {
        for (
          let offset = 0;
          offset < paths.length;
          offset += MANIFEST_PAGE_SIZE
        ) {
          if (
            !(await ctx.runMutation(internal.recordStorage.manifestPage, {
              ...args,
              bucket,
              page: offset / MANIFEST_PAGE_SIZE,
              paths: paths.slice(offset, offset + MANIFEST_PAGE_SIZE),
            }))
          )
            return null;
        }
      }
    }
    await ctx.runMutation(internal.recordStorage.finishVerification, {
      ...args,
      rejection,
      expectedCount: expected?.length,
    });
    return null;
  },
});

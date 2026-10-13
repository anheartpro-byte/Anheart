import { v } from "convex/values";
import {
  internalMutation,
  internalQuery,
  mutation,
  query,
  type MutationCtx,
  type QueryCtx,
} from "./_generated/server";
import { internal } from "./_generated/api";
import type { Doc, Id } from "./_generated/dataModel";
import {
  canAccessMachine,
  getCurrentUser,
  getCurrentUserOrThrow,
  requireRole,
} from "./lib/auth";
import {
  MAX_RECORD_FILES,
  MANIFEST_PAGE_SIZE,
  MAX_MANIFEST_PAGES,
  recordBucket,
  randomNonce,
  recordState,
  storageNonce,
  uploadMediaType,
  uploadRequest,
} from "./lib/recordStorage";

const fileMetadata = v.object({
  id: v.id("session_record_files"),
  path: v.string(),
  sha256: v.string(),
  size: v.number(),
  verified: v.boolean(),
  rejection: v.optional(v.string()),
});
const statusValidator = v.object({
  record: recordState,
  files: v.array(fileMetadata),
  expectedCount: v.number(),
  verifiedCount: v.number(),
  cursor: v.union(v.string(), v.null()),
  viewerFiles: v.array(fileMetadata),
  deleted: v.boolean(),
});
const failure = v.object({ error: v.string() });
const requestArgs = {
  machineId: v.id("machines"),
  sessionId: v.string(),
  path: v.string(),
  sha256: v.string(),
  size: v.number(),
};

async function ownedSession(
  ctx: QueryCtx,
  machineId: Id<"machines">,
  rawId: string,
) {
  const id = ctx.db.normalizeId("sessions", rawId);
  const session = id === null ? null : await ctx.db.get(id);
  const machine = await ctx.db.get(machineId);
  if (
    !session ||
    !machine ||
    machine.isDeleted ||
    machine.authenticationEnabled === false ||
    session.machineId !== machineId ||
    !session.organizationId ||
    session.organizationId !== machine.organizationId ||
    !["completed", "failed"].includes(session.status) ||
    session.recordDeleted
  )
    return null;
  return { ...session, organizationId: session.organizationId };
}
async function sessionFiles(ctx: QueryCtx, sessionId: Id<"sessions">) {
  return await ctx.db
    .query("session_record_files")
    .withIndex("by_session", (q) => q.eq("sessionId", sessionId))
    .take(MAX_RECORD_FILES);
}
function metadata(file: Doc<"session_record_files">) {
  return {
    id: file._id,
    path: file.path,
    sha256: file.sha256,
    size: file.size,
    verified: file.verified,
    rejection: file.rejection,
  };
}
async function status(
  ctx: QueryCtx,
  session: Doc<"sessions">,
  cursor?: string | null,
) {
  if (session.recordDeleted)
    return {
      record: "none" as const,
      files: [],
      expectedCount: 0,
      verifiedCount: 0,
      cursor: null,
      viewerFiles: [],
      deleted: true,
    };
  const result = await ctx.db
    .query("session_record_files")
    .withIndex("by_session", (q) => q.eq("sessionId", session._id))
    .order("desc")
    .paginate({ cursor: cursor ?? null, numItems: 256 });
  const viewerFiles = [];
  for (const path of ["manifest.json", "ticks.csv", "events.jsonl"]) {
    const file = await ctx.db
      .query("session_record_files")
      .withIndex("by_session_and_path", (q) =>
        q.eq("sessionId", session._id).eq("path", path),
      )
      .unique();
    if (file?.verified) viewerFiles.push(metadata(file));
  }
  return {
    record: session.record ?? "none",
    expectedCount:
      session.recordExpectedCount ?? session.recordExpectedFiles?.length ?? 0,
    verifiedCount:
      session.recordVerifiedCount ??
      result.page.filter((file) => file.verified).length,
    cursor: result.isDone ? null : result.continueCursor,
    viewerFiles,
    deleted: session.recordDeleted ?? false,
    files: result.page.map(metadata),
  };
}
async function refreshStatus(
  ctx: MutationCtx,
  sessionId: Id<"sessions">,
  before: { verified: boolean; rejection?: string } | null,
  after: { verified: boolean; rejection?: string },
) {
  const session = await ctx.db.get(sessionId);
  if (!session || session.recordDeleted) return;
  let verified = session.recordVerifiedCount;
  let rejected = session.recordRejectedCount;
  if (verified === undefined || rejected === undefined) {
    const files = await sessionFiles(ctx, sessionId);
    verified = files.filter((file) => file.verified).length;
    rejected = files.filter((file) => file.rejection !== undefined).length;
  } else {
    const verifiedDelta =
      Number(after.verified) - Number(before?.verified ?? false);
    const rejectedDelta =
      Number(after.rejection !== undefined) -
      Number(before?.rejection !== undefined);
    if (verifiedDelta === 0 && rejectedDelta === 0) return;
    verified += verifiedDelta;
    rejected += rejectedDelta;
  }
  const expected =
    session.recordExpectedCount ?? session.recordExpectedFiles?.length;
  const record =
    rejected > 0
      ? "rejected"
      : expected && verified === expected
        ? "complete"
        : "partial";
  await ctx.db.patch(sessionId, {
    record,
    recordVerifiedCount: verified,
    recordRejectedCount: rejected,
  });
}

export const requestUpload = internalMutation({
  args: requestArgs,
  returns: v.union(
    failure,
    v.object({ uploadUrl: v.string(), contentType: v.string() }),
    v.object({ storageId: v.id("_storage"), verified: v.boolean() }),
  ),
  handler: async (ctx, args) => {
    if (!uploadRequest.safeParse(args).success)
      return { error: "Invalid upload metadata" };
    const session = await ownedSession(ctx, args.machineId, args.sessionId);
    if (!session) return { error: "Closed session not found" };
    if (args.path !== "manifest.json") {
      const pages =
        session.recordExpectedCount === undefined
          ? []
          : await ctx.db
              .query("session_record_manifest_pages")
              .withIndex("by_session_and_bucket", (q) =>
                q
                  .eq("sessionId", session._id)
                  .eq("bucket", recordBucket(args.path)),
              )
              .take(MAX_MANIFEST_PAGES);
      if (
        !session.recordExpectedFiles?.includes(args.path) &&
        !pages.some((page) => page.paths.includes(args.path))
      )
        return { error: "Verified manifest required" };
    }
    const previous = await ctx.db
      .query("session_record_files")
      .withIndex("by_session_and_path", (q) =>
        q.eq("sessionId", session._id).eq("path", args.path),
      )
      .unique();
    if (
      previous &&
      previous.sha256 === args.sha256 &&
      previous.size === args.size &&
      !previous.rejection
    ) {
      return { storageId: previous.storageId, verified: previous.verified };
    }
    const ticket = await ctx.db
      .query("session_record_uploads")
      .withIndex("by_session_and_path", (q) =>
        q.eq("sessionId", session._id).eq("path", args.path),
      )
      .unique();
    if (
      ticket &&
      (ticket.sha256 !== args.sha256 ||
        ticket.size !== args.size ||
        ticket.revoked)
    )
      return { error: "Record files are immutable" };
    const nonce = ticket?.nonce ?? randomNonce();
    if (!ticket)
      await ctx.db.insert("session_record_uploads", {
        sessionId: session._id,
        machineId: args.machineId,
        organizationId: session.organizationId,
        path: args.path,
        sha256: args.sha256,
        size: args.size,
        nonce,
        createdAt: Date.now(),
        revoked: false,
      });
    if (session.record === undefined || session.record === "none")
      await ctx.db.patch(session._id, { record: "partial" });
    return {
      uploadUrl: await ctx.storage.generateUploadUrl(),
      contentType: uploadMediaType(nonce),
    };
  },
});

async function attachUpload(
  ctx: MutationCtx,
  ticket: Doc<"session_record_uploads">,
  storageId: Id<"_storage">,
) {
  const metadata = await ctx.db.system.get(storageId);
  if (!metadata || metadata.contentType !== uploadMediaType(ticket.nonce))
    return { error: "Upload does not belong to this file" };
  const previous = await ctx.db
    .query("session_record_files")
    .withIndex("by_session_and_path", (q) =>
      q.eq("sessionId", ticket.sessionId).eq("path", ticket.path),
    )
    .unique();
  if (previous?.verified && !ticket.revoked) {
    if (previous.storageId !== storageId) await ctx.storage.delete(storageId);
    return { accepted: true };
  }
  const session = await ownedSession(ctx, ticket.machineId, ticket.sessionId);
  if (ticket.revoked || !session) {
    await ctx.storage.delete(storageId);
    return { error: "Deposit revoked" };
  }
  if (previous && !previous.rejection) {
    if (previous.storageId !== storageId) await ctx.storage.delete(storageId);
    if (
      !previous.verified &&
      Date.now() - (previous.lastVerificationAt ?? previous.uploadedAt) > 30_000
    ) {
      await ctx.db.patch(previous._id, { lastVerificationAt: Date.now() });
      await ctx.scheduler.runAfter(0, internal.recordVerification.verify, {
        fileId: previous._id,
        storageId: previous.storageId,
      });
    }
    return { accepted: true };
  }
  const row = {
    sessionId: ticket.sessionId,
    machineId: ticket.machineId,
    organizationId: ticket.organizationId,
    path: ticket.path,
    sha256: ticket.sha256,
    size: ticket.size,
    storageId,
    uploadedAt: Date.now(),
    lastVerificationAt: Date.now(),
    verified: false,
    rejection: undefined,
  };
  const fileId = previous
    ? previous._id
    : await ctx.db.insert("session_record_files", row);
  if (previous) await ctx.db.replace(previous._id, row);
  if (metadata.size !== ticket.size) {
    await ctx.storage.delete(storageId);
    await ctx.db.patch(fileId, { rejection: "size_mismatch" });
  } else
    await ctx.scheduler.runAfter(0, internal.recordVerification.verify, {
      fileId,
      storageId,
    });
  await refreshStatus(ctx, ticket.sessionId, previous, {
    verified: false,
    rejection: metadata.size !== ticket.size ? "size_mismatch" : undefined,
  });
  return { accepted: true };
}

export const commit = internalMutation({
  args: { ...requestArgs, storageId: v.string() },
  returns: v.union(failure, v.object({ accepted: v.boolean() })),
  handler: async (ctx, args) => {
    const session = await ownedSession(ctx, args.machineId, args.sessionId);
    if (!session) return { error: "Closed session not found" };
    const ticket = await ctx.db
      .query("session_record_uploads")
      .withIndex("by_session_and_path", (q) =>
        q.eq("sessionId", session._id).eq("path", args.path),
      )
      .unique();
    const storageId = ctx.db.system.normalizeId("_storage", args.storageId);
    if (
      !ticket ||
      !storageId ||
      ticket.sha256 !== args.sha256 ||
      ticket.size !== args.size
    )
      return { error: "Upload reservation not found" };
    return await attachUpload(ctx, ticket, storageId);
  },
});

export const machineStatus = internalQuery({
  args: {
    machineId: v.id("machines"),
    sessionId: v.string(),
    cursor: v.optional(v.union(v.string(), v.null())),
  },
  returns: v.union(statusValidator, v.null()),
  handler: async (ctx, args) => {
    const id = ctx.db.normalizeId("sessions", args.sessionId);
    const session = id === null ? null : await ctx.db.get(id);
    if (!session || session.machineId !== args.machineId) return null;
    return await status(ctx, session, args.cursor);
  },
});
export const list = query({
  args: {
    sessionId: v.id("sessions"),
    cursor: v.optional(v.union(v.string(), v.null())),
  },
  returns: v.union(statusValidator, v.null()),
  handler: async (ctx, args) => {
    const user = await getCurrentUserOrThrow(ctx);
    const session = await ctx.db.get(args.sessionId);
    if (!session || !(await canAccessMachine(ctx, session.machineId, user)))
      return null;
    return await status(ctx, session, args.cursor);
  },
});
export const authorizedFile = internalQuery({
  args: { fileId: v.string() },
  returns: v.union(
    v.null(),
    v.object({
      storageId: v.id("_storage"),
      path: v.string(),
      size: v.number(),
      sha256: v.string(),
    }),
  ),
  handler: async (ctx, args) => {
    const found = await getCurrentUser(ctx);
    if (!found?.session) return null;
    const user = {
      ...found.user,
      role: found.session.role,
      organizationId: found.session.organization._id,
    };
    const id = ctx.db.normalizeId("session_record_files", args.fileId);
    const file = id === null ? null : await ctx.db.get(id);
    if (
      !file ||
      !file.verified ||
      !(await canAccessMachine(ctx, file.machineId, user))
    )
      return null;
    const session = await ctx.db.get(file.sessionId);
    if (
      !session ||
      session.recordDeleted ||
      session.organizationId !== file.organizationId
    )
      return null;
    return {
      storageId: file.storageId,
      path: file.path,
      size: file.size,
      sha256: file.sha256,
    };
  },
});
export const verificationInput = internalQuery({
  args: { fileId: v.id("session_record_files"), storageId: v.id("_storage") },
  returns: v.union(
    v.null(),
    v.object({ path: v.string(), sha256: v.string(), size: v.number() }),
  ),
  handler: async (ctx, args) => {
    const file = await ctx.db.get(args.fileId);
    if (
      !file ||
      file.storageId !== args.storageId ||
      file.verified ||
      file.rejection
    )
      return null;
    return { path: file.path, sha256: file.sha256, size: file.size };
  },
});
export const finishVerification = internalMutation({
  args: {
    fileId: v.id("session_record_files"),
    storageId: v.id("_storage"),
    rejection: v.optional(v.string()),
    expectedCount: v.optional(v.number()),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const file = await ctx.db.get(args.fileId);
    if (
      !file ||
      file.storageId !== args.storageId ||
      file.verified ||
      file.rejection
    )
      return null;
    const session = await ctx.db.get(file.sessionId);
    if (!session || session.recordDeleted) return null;
    if (!args.rejection && file.path === "manifest.json") {
      const pages = await ctx.db
        .query("session_record_manifest_pages")
        .withIndex("by_session", (q) => q.eq("sessionId", session._id))
        .take(MAX_MANIFEST_PAGES);
      const count = pages
        .filter((page) => page.storageId === args.storageId)
        .reduce((count, page) => count + page.paths.length, 0);
      if (count !== args.expectedCount)
        throw Error("Manifest pages are incomplete");
      await ctx.db.patch(session._id, {
        recordExpectedCount: count,
        recordExpectedFiles: undefined,
      });
    }
    if (args.rejection) await ctx.storage.delete(file.storageId);
    await ctx.db.patch(file._id, {
      verified: !args.rejection,
      rejection: args.rejection,
    });
    await refreshStatus(ctx, file.sessionId, file, {
      verified: !args.rejection,
      rejection: args.rejection,
    });
    return null;
  },
});

export const manifestPage = internalMutation({
  args: {
    fileId: v.id("session_record_files"),
    storageId: v.id("_storage"),
    bucket: v.number(),
    page: v.number(),
    paths: v.array(v.string()),
  },
  returns: v.boolean(),
  handler: async (ctx, args) => {
    const file = await ctx.db.get(args.fileId);
    if (
      !file ||
      file.path !== "manifest.json" ||
      file.storageId !== args.storageId ||
      file.verified ||
      file.rejection
    )
      return false;
    const session = await ctx.db.get(file.sessionId);
    if (!session || session.recordDeleted) return false;
    if (
      args.paths.length > MANIFEST_PAGE_SIZE ||
      args.paths.some((path) => recordBucket(path) !== args.bucket)
    )
      throw Error("Invalid manifest page");
    const previous = await ctx.db
      .query("session_record_manifest_pages")
      .withIndex("by_session_bucket_page", (q) =>
        q
          .eq("sessionId", file.sessionId)
          .eq("bucket", args.bucket)
          .eq("page", args.page),
      )
      .unique();
    const row = {
      sessionId: file.sessionId,
      storageId: args.storageId,
      bucket: args.bucket,
      page: args.page,
      paths: args.paths,
    };
    if (previous) await ctx.db.replace(previous._id, row);
    else await ctx.db.insert("session_record_manifest_pages", row);
    return true;
  },
});

export const sweepUploads = internalMutation({
  args: {
    cursor: v.union(v.string(), v.null()),
    since: v.optional(v.number()),
    until: v.optional(v.number()),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const checkpoint = await ctx.db
      .query("record_upload_sweeps")
      .withIndex("by_name", (q) => q.eq("name", "uploads"))
      .unique();
    const since = args.since ?? checkpoint?.cutoff ?? 0;
    const until = args.until ?? Date.now();
    const page = await ctx.db.system
      .query("_storage")
      .withIndex("by_creation_time", (q) =>
        q.gt("_creationTime", since).lte("_creationTime", until),
      )
      .paginate({ cursor: args.cursor, numItems: 50 });
    for (const stored of page.page) {
      const nonce = storageNonce(stored.contentType);
      if (!nonce) continue;
      const ticket = await ctx.db
        .query("session_record_uploads")
        .withIndex("by_nonce", (q) => q.eq("nonce", nonce))
        .unique();
      if (ticket) await attachUpload(ctx, ticket, stored._id);
    }
    if (!page.isDone)
      await ctx.scheduler.runAfter(0, internal.recordStorage.sweepUploads, {
        cursor: page.continueCursor,
        since,
        until,
      });
    else if (checkpoint)
      await ctx.db.patch(checkpoint._id, {
        cutoff: Math.max(checkpoint.cutoff, until),
      });
    else
      await ctx.db.insert("record_upload_sweeps", {
        name: "uploads",
        cutoff: until,
      });
    return null;
  },
});

export const remove = mutation({
  args: { sessionId: v.id("sessions") },
  returns: v.null(),
  handler: async (ctx, args) => {
    const user = await requireRole(ctx, ["admin", "org_admin"]);
    const session = await ctx.db.get(args.sessionId);
    if (!session || !(await canAccessMachine(ctx, session.machineId, user)))
      throw Error("Session not found");
    await ctx.db.patch(session._id, {
      record: "none",
      recordDeleted: true,
      recordExpectedFiles: undefined,
      recordExpectedCount: undefined,
      recordVerifiedCount: undefined,
      recordRejectedCount: undefined,
    });
    await ctx.scheduler.runAfter(0, internal.recordStorage.removeBatch, {
      sessionId: session._id,
    });
    return null;
  },
});
export const removeBatch = internalMutation({
  args: { sessionId: v.id("sessions") },
  returns: v.null(),
  handler: async (ctx, args) => {
    const session = await ctx.db.get(args.sessionId);
    if (!session?.recordDeleted) return null;
    const files = await ctx.db
      .query("session_record_files")
      .withIndex("by_session", (q) => q.eq("sessionId", args.sessionId))
      .take(100);
    for (const file of files) {
      if (await ctx.db.system.get(file.storageId))
        await ctx.storage.delete(file.storageId);
      await ctx.db.delete(file._id);
    }
    const pages = await ctx.db
      .query("session_record_manifest_pages")
      .withIndex("by_session", (q) => q.eq("sessionId", args.sessionId))
      .take(100);
    for (const page of pages) await ctx.db.delete(page._id);
    if (files.length === 100 || pages.length === 100)
      await ctx.scheduler.runAfter(0, internal.recordStorage.removeBatch, args);
    return null;
  },
});

export const enforceRetention = internalMutation({
  args: { cursor: v.union(v.string(), v.null()) },
  returns: v.null(),
  handler: async (ctx, args) => {
    const configured = process.env.RECORD_RETENTION_DAYS;
    if (!configured || !/^[1-9][0-9]{0,4}$/.test(configured)) return null;
    const cutoff = Date.now() - Number(configured) * 86_400_000;
    const page = await ctx.db
      .query("sessions")
      .paginate({ cursor: args.cursor, numItems: 50 });
    for (const session of page.page) {
      if (
        session.recordDeleted ||
        !session.record ||
        session.record === "none" ||
        session.endedAt === undefined ||
        session.endedAt > cutoff
      )
        continue;
      await ctx.db.patch(session._id, {
        record: "none",
        recordDeleted: true,
        recordExpectedFiles: undefined,
        recordExpectedCount: undefined,
        recordVerifiedCount: undefined,
        recordRejectedCount: undefined,
      });
      await ctx.scheduler.runAfter(0, internal.recordStorage.removeBatch, {
        sessionId: session._id,
      });
    }
    if (!page.isDone)
      await ctx.scheduler.runAfter(0, internal.recordStorage.enforceRetention, {
        cursor: page.continueCursor,
      });
    return null;
  },
});

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import {
  addSession,
  as,
  modules as productionModules,
  NOW,
  seedWorld,
  seedMachineWorld,
  type World,
} from "./test.setup";
import { sha256, uploadRequest } from "./lib/recordStorage";
import { makeFunctionReference } from "convex/server";
import type { Id } from "./_generated/dataModel";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
});
const encoder = new TextEncoder();
const modules = {
  ...productionModules,
  "./recordStorage.fixtures.ts": () => import("./recordStorage.fixtures"),
};
const nativeUploadType = makeFunctionReference<
  "mutation",
  { storageId: Id<"_storage">; contentType: string },
  null
>("recordStorage.fixtures:nativeUploadType");
async function nativeUpload(w: World, text: Blob) {
  const storageId = await w.t.run((ctx) => ctx.storage.store(text));
  await w.t.mutation(nativeUploadType, { storageId, contentType: text.type });
  return storageId;
}
const expected = [
  "manifest.json",
  "ticks.csv",
  "events.jsonl",
  "checksums.sha256",
];
const manifest = JSON.stringify({
  schema_version: 2,
  ended_at: "2026-10-13T10:00:00Z",
  files: expected,
});
async function fixture() {
  const w = await seedWorld(modules);
  const sessionId = await addSession(w, {
    machineId: w.machine,
    status: "completed",
    kind: "manual",
  });
  return { ...w, sessionId };
}
async function prepare(
  w: World,
  sessionId: Id<"sessions">,
  path: string,
  text: string,
) {
  const bytes = encoder.encode(text);
  const args = {
    machineId: w.machine,
    sessionId,
    path,
    sha256: await sha256(bytes.buffer),
    size: bytes.byteLength,
  };
  const reservation = await w.t.mutation(
    internal.recordStorage.requestUpload,
    args,
  );
  if (!("contentType" in reservation) || !reservation.contentType)
    throw Error("No upload reservation");
  const storageId = await nativeUpload(
    w,
    new Blob([bytes], { type: reservation.contentType }),
  );
  return { ...args, storageId };
}
async function upload(
  w: World,
  sessionId: Id<"sessions">,
  path: string,
  text: string,
) {
  const args = await prepare(w, sessionId, path, text);
  expect(await w.t.mutation(internal.recordStorage.commit, args)).toEqual({
    accepted: true,
  });
  await w.t.finishAllScheduledFunctions(vi.runAllTimers);
  return args;
}
describe("ANH-130 verified session deposits", () => {
  it("verifies an inventory larger than the native 8192-element limit using bounded pages", async () => {
    const w = await fixture();
    const paths = [
      ...expected,
      ...Array.from(
        { length: 9000 },
        (_, i) => `ecg_raw/${i.toString().padStart(6, "0")}.bin.gz`,
      ),
    ];
    await upload(
      w,
      w.sessionId,
      "manifest.json",
      JSON.stringify({
        schema_version: 2,
        ended_at: "2026-10-13T10:00:00Z",
        files: paths,
      }),
    );
    const result = await as(w.t, "manager").query(api.recordStorage.list, {
      sessionId: w.sessionId,
    });
    expect(result?.expectedCount).toBe(9004);
    expect(result?.verifiedCount).toBe(1);
    const pages = await w.t.run(async (ctx) =>
      (await ctx.db.query("session_record_manifest_pages").collect()).map(
        (page) => page.paths.length,
      ),
    );
    expect(Math.max(...pages)).toBeLessThanOrEqual(1024);
    expect(pages.reduce((total, count) => total + count, 0)).toBe(9004);
    expect(
      await w.t.mutation(internal.recordStorage.requestUpload, {
        machineId: w.machine,
        sessionId: w.sessionId,
        path: "ecg_raw/008999.bin.gz",
        sha256: "a".repeat(64),
        size: 1,
      }),
    ).toHaveProperty("uploadUrl");
  });
  it("authenticates every machine route and bounds its request metadata", async () => {
    const w = await seedMachineWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      status: "completed",
      kind: "manual",
    });
    const headers = {
      Authorization: `Bearer ${w.machineKey}`,
      [CONTRACT_HEADER]: CONTRACT_VERSION,
    };
    const body = {
      sessionId,
      path: "manifest.json",
      size: manifest.length,
      sha256: await sha256(encoder.encode(manifest).buffer),
    };
    for (const path of ["upload-url", "commit"]) {
      const route = `/api/machine/records/${path}`;
      expect(
        (await w.t.fetch(route, { method: "POST", body: JSON.stringify(body) }))
          .status,
      ).toBe(401);
      expect(
        (await w.t.fetch(route, { method: "POST", headers, body: "{" })).status,
      ).toBe(400);
    }
    expect(
      (
        await w.t.fetch("/api/machine/records/upload-url", {
          method: "POST",
          headers,
          body: JSON.stringify(body),
        })
      ).status,
    ).toBe(200);
    expect((await w.t.fetch("/api/machine/records/status")).status).toBe(401);
    expect(
      (await w.t.fetch("/api/machine/records/status", { headers })).status,
    ).toBe(400);
    expect(
      (
        await w.t.fetch("/api/machine/records/status?sessionId=missing", {
          headers,
        })
      ).status,
    ).toBe(404);
    expect(
      (
        await w.t.fetch(`/api/machine/records/status?sessionId=${sessionId}`, {
          headers,
        })
      ).status,
    ).toBe(200);
    const preflight = await w.t.fetch("/api/records/download", {
      method: "OPTIONS",
    });
    expect(preflight.status).toBe(204);
    expect(await preflight.text()).toBe("");
  });
  it("requires a closed owned session and a verified manifest before data files", async () => {
    const w = await fixture();
    const args = {
      machineId: w.machine,
      sessionId: w.sessionId,
      path: "ticks.csv",
      sha256: "a".repeat(64),
      size: 0,
    };
    expect(
      await w.t.mutation(internal.recordStorage.requestUpload, args),
    ).toHaveProperty("error", "Verified manifest required");
    expect(
      await w.t.mutation(internal.recordStorage.requestUpload, {
        ...args,
        path: "../secret",
      }),
    ).toHaveProperty("error");
    expect(
      await w.t.mutation(internal.recordStorage.requestUpload, {
        ...args,
        machineId: w.otherMachine,
        path: "manifest.json",
      }),
    ).toHaveProperty("error");
    await w.t.run((ctx) => ctx.db.patch(w.sessionId, { status: "active" }));
    expect(
      await w.t.mutation(internal.recordStorage.requestUpload, {
        ...args,
        path: "manifest.json",
      }),
    ).toHaveProperty("error");
  });
  it("becomes complete only after every expected file is verified and commits are idempotent", async () => {
    const w = await fixture();
    const first = await upload(w, w.sessionId, "manifest.json", manifest);
    expect(await w.t.mutation(internal.recordStorage.commit, first)).toEqual({
      accepted: true,
    });
    let state = await as(w.t, "manager").query(api.recordStorage.list, {
      sessionId: w.sessionId,
    });
    expect(state?.record).toBe("partial");
    for (const path of expected.slice(1))
      await upload(w, w.sessionId, path, "sample\n");
    state = await as(w.t, "manager").query(api.recordStorage.list, {
      sessionId: w.sessionId,
    });
    expect(state?.record).toBe("complete");
    expect(state?.files).toHaveLength(4);
    expect(state?.files.every((file) => file.verified)).toBe(true);
    expect(JSON.stringify(state)).not.toContain("storageId");
  });
  it("rejects a hash mismatch, removes only its upload, and permits a correct retry", async () => {
    const w = await fixture();
    const good = await prepare(w, w.sessionId, "manifest.json", manifest);
    const ticket = await w.t.run((ctx) =>
      ctx.db.query("session_record_uploads").first(),
    );
    expect(ticket).not.toBeNull();
    if (!ticket) throw Error("Missing ticket");
    const wrong = await nativeUpload(
      w,
      new Blob(["x".repeat(good.size)], {
        type: `application/x-anheart-record;nonce=${ticket.nonce}`,
      }),
    );
    expect(
      await w.t.mutation(internal.recordStorage.commit, {
        ...good,
        storageId: wrong,
      }),
    ).toEqual({ accepted: true });
    await w.t.finishAllScheduledFunctions(vi.runAllTimers);
    expect(
      (
        await as(w.t, "manager").query(api.recordStorage.list, {
          sessionId: w.sessionId,
        })
      )?.record,
    ).toBe("rejected");
    expect(
      await w.t.mutation(internal.recordStorage.requestUpload, {
        ...uploadRequest.parse(good),
        machineId: w.machine,
      }),
    ).toHaveProperty("rejected", true);
    expect(
      await w.t.run(async (ctx) => (await ctx.storage.get(wrong)) !== null),
    ).toBe(false);
    expect(
      await w.t.run(
        async (ctx) => (await ctx.storage.get(good.storageId)) !== null,
      ),
    ).toBe(true);
    await w.t.mutation(internal.recordStorage.commit, good);
    await w.t.finishAllScheduledFunctions(vi.runAllTimers);
    expect(
      (
        await as(w.t, "manager").query(api.recordStorage.list, {
          sessionId: w.sessionId,
        })
      )?.record,
    ).toBe("partial");
  });
  it("does not claim or delete a foreign storage object", async () => {
    const w = await fixture();
    const own = await prepare(w, w.sessionId, "manifest.json", manifest);
    const foreign = await w.t.run((ctx) =>
      ctx.storage.store(new Blob([manifest], { type: "application/json" })),
    );
    expect(
      await w.t.mutation(internal.recordStorage.commit, {
        ...own,
        storageId: foreign,
      }),
    ).toHaveProperty("error");
    expect(
      await w.t.run(async (ctx) => (await ctx.storage.get(foreign)) !== null),
    ).toBe(true);
  });
  it("recovers an upload whose acknowledgement was lost and removes duplicate blobs", async () => {
    const w = await fixture();
    const first = await prepare(w, w.sessionId, "manifest.json", manifest);
    const second = await prepare(w, w.sessionId, "manifest.json", manifest);
    vi.setSystemTime(NOW + 1000);
    await w.t.mutation(internal.recordStorage.sweepUploads, { cursor: null });
    await w.t.finishAllScheduledFunctions(vi.runAllTimers);
    const rows = await w.t.run((ctx) =>
      ctx.db.query("session_record_files").collect(),
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].verified).toBe(true);
    const survivors = await w.t.run(async (ctx) => [
      (await ctx.storage.get(first.storageId)) !== null,
      (await ctx.storage.get(second.storageId)) !== null,
    ]);
    expect(survivors.filter(Boolean)).toHaveLength(1);
  });
  it("downloads through Clerk authorization and denies other organisations and patients", async () => {
    const w = await fixture();
    await upload(w, w.sessionId, "manifest.json", manifest);
    const file = await w.t.run((ctx) =>
      ctx.db.query("session_record_files").first(),
    );
    if (!file) throw Error("Missing file");
    for (const actor of ["manager", "admin", "orgAdmin"] as const) {
      const response = await as(w.t, actor).fetch(
        `/api/records/download?fileId=${file._id}`,
      );
      expect(response.status).toBe(200);
      expect(await response.text()).toBe(manifest);
      expect(response.headers.get("Cache-Control")).toBe("no-store");
    }
    for (const actor of [
      "patient",
      "otherManager",
      "orgBAdmin",
      "orgBManager",
      "orgBPatient",
    ] as const) {
      expect(
        await as(w.t, actor).query(api.recordStorage.list, {
          sessionId: w.sessionId,
        }),
      ).toBeNull();
      expect(
        (await as(w.t, actor).fetch(`/api/records/download?fileId=${file._id}`))
          .status,
      ).toBe(404);
    }
    expect(
      (await w.t.fetch(`/api/records/download?fileId=${file._id}`)).status,
    ).toBe(401);
  });
  it("paginates large file inventories without dropping or repeating rows", async () => {
    const w = await fixture();
    const first = await upload(w, w.sessionId, "manifest.json", manifest);
    await w.t.run(async (ctx) => {
      const machine = await ctx.db.get(w.machine);
      if (!machine?.organizationId) throw Error("Machine organisation missing");
      for (let i = 0; i < 300; i++) {
        await ctx.db.insert("session_record_files", {
          sessionId: w.sessionId,
          machineId: w.machine,
          organizationId: machine.organizationId,
          path: `ecg_raw/${i}.bin.gz`,
          storageId: first.storageId,
          sha256: first.sha256,
          size: first.size,
          uploadedAt: NOW,
          verified: true,
        });
      }
    });
    const page1 = await as(w.t, "manager").query(api.recordStorage.list, {
      sessionId: w.sessionId,
    });
    expect(page1?.files).toHaveLength(256);
    expect(page1?.cursor).toBeTruthy();
    if (!page1) throw Error("First page missing");
    const page2 = await as(w.t, "manager").query(api.recordStorage.list, {
      sessionId: w.sessionId,
      cursor: page1?.cursor,
    });
    expect(page2?.files).toHaveLength(45);
    expect(page2?.cursor).toBeNull();
    if (!page2) throw Error("Second page missing");
    expect(
      new Set([...page1.files, ...page2.files].map((file) => file.id)).size,
    ).toBe(301);
    expect(page1?.viewerFiles.map((file) => file.path)).toEqual([
      "manifest.json",
    ]);
  });
  it("keeps records by default and only purges expired records when retention is explicitly enabled", async () => {
    const w = await fixture();
    const old = await upload(w, w.sessionId, "manifest.json", manifest);
    await w.t.run((ctx) =>
      ctx.db.patch(w.sessionId, { endedAt: NOW - 2 * 86_400_000 }),
    );
    const recentId = await addSession(w, {
      machineId: w.machine,
      status: "completed",
      kind: "manual",
    });
    const recent = await upload(w, recentId, "manifest.json", manifest);
    await w.t.run((ctx) => ctx.db.patch(recentId, { endedAt: NOW }));
    for (const setting of ["", "0", "-1", "1.5", "forever"]) {
      vi.stubEnv("RECORD_RETENTION_DAYS", setting);
      await w.t.mutation(internal.recordStorage.enforceRetention, {
        cursor: null,
      });
      await w.t.finishAllScheduledFunctions(vi.runAllTimers);
      expect(
        await w.t.run(
          async (ctx) => (await ctx.storage.get(old.storageId)) !== null,
        ),
      ).toBe(true);
    }
    vi.stubEnv("RECORD_RETENTION_DAYS", "1");
    await w.t.mutation(internal.recordStorage.enforceRetention, {
      cursor: null,
    });
    await w.t.finishAllScheduledFunctions(vi.runAllTimers);
    expect(await w.t.run((ctx) => ctx.storage.get(old.storageId))).toBeNull();
    expect(
      await w.t.run(
        async (ctx) => (await ctx.storage.get(recent.storageId)) !== null,
      ),
    ).toBe(true);
    const removed = await as(w.t, "admin").query(api.recordStorage.list, {
      sessionId: w.sessionId,
    });
    expect(removed).toMatchObject({
      deleted: true,
      record: "none",
      files: [],
      expectedCount: 0,
      verifiedCount: 0,
    });
    expect(
      await w.t.run((ctx) =>
        ctx.db
          .query("session_record_manifest_pages")
          .withIndex("by_session", (q) => q.eq("sessionId", w.sessionId))
          .collect(),
      ),
    ).toHaveLength(0);
  });
  it("admin deletion revokes downloads and late uploads without touching unrelated files", async () => {
    const w = await fixture();
    const committed = await upload(w, w.sessionId, "manifest.json", manifest);
    await expect(
      as(w.t, "manager").mutation(api.recordStorage.remove, {
        sessionId: w.sessionId,
      }),
    ).rejects.toThrow();
    await expect(
      as(w.t, "orgBAdmin").mutation(api.recordStorage.remove, {
        sessionId: w.sessionId,
      }),
    ).rejects.toThrow();
    await as(w.t, "orgAdmin").mutation(api.recordStorage.remove, {
      sessionId: w.sessionId,
    });
    await w.t.finishAllScheduledFunctions(vi.runAllTimers);
    expect(
      await w.t.run((ctx) => ctx.storage.get(committed.storageId)),
    ).toBeNull();
    expect(
      await w.t.run((ctx) => ctx.db.query("session_record_files").collect()),
    ).toHaveLength(0);
    expect(
      await w.t.mutation(internal.recordStorage.requestUpload, {
        ...uploadRequest.parse(committed),
        machineId: w.machine,
      }),
    ).toHaveProperty("error");
  });
});

/// <reference types="vite/client" />
/**
 * ANH-134 EX-1: the backend carries its own version. `convex/VERSION` is the
 * file `scripts/release.sh` writes; `convex/cloudVersion.ts` is the constant the
 * deployed code reads. They must never differ.
 */
import { describe, expect, it } from "vitest";
import { convexTest } from "convex-test";
import schema from "./schema";
import { internal } from "./_generated/api";
import { modules } from "./test.setup";
import { CLOUD_VERSION } from "./cloudVersion";
import versionFile from "./VERSION?raw";

describe("ANH-134 EX-1 version of the Convex backend", () => {
  it("is a cloud-X.Y.Z version, or the development value before a first release", () => {
    expect(CLOUD_VERSION).toMatch(/^cloud-\d+\.\d+\.\d+(-dev)?$/);
  });

  it("is the same in convex/VERSION and in the deployed constant", () => {
    expect(versionFile).toBe(`${CLOUD_VERSION}\n`);
  });

  it("is what the deployed backend answers", async () => {
    const t = convexTest(schema, modules);
    const deployed = await t.query(
      internal.softwareReleases.deployedCloudVersion,
      {},
    );
    expect(deployed).toBe(CLOUD_VERSION);
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * The version the site writes in its footer. It is read from the build's
 * configuration once, when the module is loaded: each case loads it afresh.
 */

async function versionWith(value: string | undefined) {
  vi.resetModules();
  if (value === undefined) vi.stubEnv("NEXT_PUBLIC_WEB_VERSION", undefined);
  else vi.stubEnv("NEXT_PUBLIC_WEB_VERSION", value);
  return (await import("./version")).WEB_VERSION;
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("ANH-203 version of the site", () => {
  it("is the one the build was given", async () => {
    expect(await versionWith("web-0.4.2")).toBe("web-0.4.2");
  });

  it("says that it is unknown when the build was given none, never a made-up number", async () => {
    expect(await versionWith(undefined)).toBe("web-inconnue");
  });
});

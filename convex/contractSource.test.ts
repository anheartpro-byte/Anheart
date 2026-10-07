/// <reference types="vite/client" />
/**
 * ANH-133 EX-1: `convex/lib/contract.ts` READS the shared file
 * `contracts/machine-api.json`, it does not keep a copy of its values.
 *
 * The shared file is replaced here by a different contract: if the module held
 * its own literals, it would still announce the real ones and these tests
 * would fail.
 */
import { describe, expect, it, vi } from "vitest";

vi.mock("../contracts/machine-api.json", () => ({
  default: {
    contract_version: "7.3",
    header: "X-Substituted-Contract",
    error_codes: { contract_unsupported: {}, substituted_code: {} },
  },
}));

describe("ANH-133 EX-1 the contract is read from contracts/machine-api.json", () => {
  it("takes its version, header, served major and codes from that file", async () => {
    const contract = await import("./lib/contract");
    expect(contract.CONTRACT_VERSION).toBe("7.3");
    expect(contract.CONTRACT_HEADER).toBe("X-Substituted-Contract");
    expect(contract.SUPPORTED_MAJORS).toEqual(["7"]);
    expect(contract.MACHINE_ERROR_CODES).toEqual([
      "contract_unsupported",
      "substituted_code",
    ]);
  });

  it("checks requests against the file's header and major", async () => {
    const { contractUnsupported, servedContract } =
      await import("./lib/contract");
    const request = (headers: Record<string, string>) =>
      new Request("https://example.invalid/api/machine/heartbeat", { headers });

    expect(servedContract(request({ "X-Substituted-Contract": "7.4" }))).toBe(
      "7.4",
    );
    expect(servedContract(request({ "X-Anheart-Contract": "1.0" }))).toBeNull();
    const refused = contractUnsupported();
    expect(refused.status).toBe(426);
    expect(await refused.json()).toMatchObject({
      error: "contract_unsupported",
      supported: ["7"],
    });
  });
});

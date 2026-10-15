/// <reference types="vite/client" />
/**
 * ANH-139 (Pi layer): the console now fills `medical_parameters_version` in its
 * heartbeat, with the version of its signed medical file, or `null` when the
 * signature did not verify. The field was already in the contract
 * (`heartbeat_version_fields` of `contracts/machine-api.json`): this backend
 * must keep accepting a heartbeat with a version, with `null` and without the
 * field, and change nothing else it records. Storing and comparing the version
 * is the Convex layer of the ticket, not tested here.
 */
import { describe, expect, it } from "vitest";
import sharedText from "../contracts/machine-api.json?raw";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";
import { modules, seedMachineWorld } from "./test.setup";

const shared = JSON.parse(sharedText) as { heartbeat_version_fields: string[] };

const world = () => seedMachineWorld(modules);
type MachineWorld = Awaited<ReturnType<typeof world>>;

function heartbeat(w: MachineWorld, body: Record<string, unknown>) {
  return w.t.fetch("/api/machine/heartbeat", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${w.machineKey}`,
      "Content-Type": "application/json",
      [CONTRACT_HEADER]: CONTRACT_VERSION,
    },
    body: JSON.stringify(body),
  });
}

describe("ANH-139 a heartbeat that carries the medical parameters' version", () => {
  it("is a field of the shared contract", () => {
    expect(shared.heartbeat_version_fields).toContain(
      "medical_parameters_version",
    );
  });

  it.each([
    {
      label: "a signed version",
      medical: { medical_parameters_version: "0.1.0" },
    },
    {
      label: "null (bench mode)",
      medical: { medical_parameters_version: null },
    },
    { label: "no field (an older console)", medical: {} },
  ])(
    "is accepted with $label, and the rest is recorded as before",
    async ({ medical }) => {
      const w = await world();
      const response = await heartbeat(w, {
        software_version: "pi-1.0.0",
        contract_version: CONTRACT_VERSION,
        config_hash: null,
        ...medical,
      });
      expect(response.status).toBe(200);
      const machine = await w.t.run((ctx) => ctx.db.get(w.machine));
      expect(machine?.softwareVersion).toBe("pi-1.0.0");
      expect(machine?.contractVersion).toBe(CONTRACT_VERSION);
    },
  );
});

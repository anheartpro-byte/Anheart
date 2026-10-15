/// <reference types="vite/client" />
/**
 * ANH-139: the console sends its signed medical version, or `null` when its
 * signature does not verify. The field has been in the shared contract since
 * 1.5 (`heartbeat_version_fields` of `contracts/machine-api.json`). Convex
 * keeps accepting absent and malformed values as unstated, stores the result
 * only after the existing contract gate, exposes the resulting machine status,
 * and refuses remote launches from unsigned machines.
 */
import { describe, expect, it } from "vitest";
import sharedText from "../contracts/machine-api.json?raw";
import { api } from "./_generated/api";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";
import { modules, seedMachineWorld } from "./test.setup";

const shared = JSON.parse(sharedText) as { heartbeat_version_fields: string[] };

const world = () => seedMachineWorld(modules);
type MachineWorld = Awaited<ReturnType<typeof world>>;

function heartbeat(
  w: MachineWorld,
  body: Record<string, unknown>,
  contractVersion = CONTRACT_VERSION,
) {
  return w.t.fetch("/api/machine/heartbeat", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${w.machineKey}`,
      "Content-Type": "application/json",
      [CONTRACT_HEADER]: contractVersion,
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
      expected: "0.1.0",
    },
    {
      label: "null (bench mode)",
      medical: { medical_parameters_version: null },
      expected: null,
    },
    { label: "no field (an older console)", medical: {}, expected: undefined },
    {
      label: "a malformed version",
      medical: { medical_parameters_version: "00.1.0" },
      expected: undefined,
    },
    {
      label: "a value of the wrong type",
      medical: { medical_parameters_version: 7 },
      expected: undefined,
    },
  ])(
    "is accepted with $label and stores the version state without changing other heartbeat data",
    async ({ medical, expected }) => {
      const w = await world();
      await w.t.run((ctx) =>
        ctx.db.patch(w.machine, { medicalParametersVersion: "0.0.1" }),
      );
      const response = await heartbeat(w, {
        batteryLevel: 82,
        wifiStrength: -41,
        software_version: "pi-1.0.0",
        contract_version: CONTRACT_VERSION,
        config_hash: null,
        ...medical,
      });
      expect(response.status).toBe(200);
      const machine = await w.t.run((ctx) => ctx.db.get(w.machine));
      expect(machine?.medicalParametersVersion).toBe(expected);
      expect(machine?.softwareVersion).toBe("pi-1.0.0");
      expect(machine?.contractVersion).toBe(CONTRACT_VERSION);
      expect(typeof machine?.lastVersionSeenAt).toBe("number");
      const history = await w.t.run((ctx) =>
        ctx.db
          .query("machine_heartbeats")
          .withIndex("by_machine_and_timestamp", (q) =>
            q.eq("machineId", w.machine),
          )
          .collect(),
      );
      expect(history).toHaveLength(1);
      expect(history[0]).toMatchObject({ batteryLevel: 82, wifiStrength: -41 });
    },
  );

  it("does not write the medical version when the contract gate rejects the heartbeat", async () => {
    const w = await world();
    await w.t.run((ctx) =>
      ctx.db.patch(w.machine, { medicalParametersVersion: "0.1.0" }),
    );
    const response = await heartbeat(
      w,
      { medical_parameters_version: "0.2.0" },
      "2.0",
    );
    expect(response.status).toBe(426);
    expect(await response.json()).toMatchObject({ error: "contract_unsupported" });
    const machine = await w.t.run((ctx) => ctx.db.get(w.machine));
    expect(machine?.medicalParametersVersion).toBe("0.1.0");
    expect(
      await w.t.run((ctx) => ctx.db.query("machine_heartbeats").collect()),
    ).toEqual([]);
  });

  it("returns all six medical statuses and the reference version on the machine page", async () => {
    const w = await world();
    const cases = [
      { version: undefined, status: "unknown" },
      { version: null, status: "unsigned" },
      { version: "0.0.9", status: "obsolete" },
      { version: "0.1.0", status: "current" },
      { version: "0.1.1", status: "ahead" },
    ] as const;
    for (const { version, status } of cases) {
      await w.t.run((ctx) =>
        ctx.db.patch(w.machine, { medicalParametersVersion: version }),
      );
      const shown = await w.admin.query(api.machines.getMachine, {
        machineId: w.machine,
      });
      expect(shown?.medicalParametersVersion).toBe(version);
      expect(shown?.medicalParametersStatus).toBe(status);
      expect(shown?.medicalParametersReferenceVersion).toBe("0.1.0");
    }

    await w.t.run(async (ctx) => {
      for (const row of await ctx.db.query("medical_parameters_versions").collect()) {
        await ctx.db.delete(row._id);
        await ctx.storage.delete(row.storageId);
      }
      await ctx.db.patch(w.machine, { medicalParametersVersion: "0.1.0" });
    });
    const withoutReference = await w.admin.query(api.machines.getMachine, {
      machineId: w.machine,
    });
    expect(withoutReference).toMatchObject({
      medicalParametersVersion: "0.1.0",
      medicalParametersStatus: "no_reference",
      medicalParametersReferenceVersion: null,
    });
  });

  it("includes medical status and reference on both launch-screen queries", async () => {
    const w = await world();
    await w.t.run((ctx) =>
      ctx.db.patch(w.machine, { medicalParametersVersion: null }),
    );
    const list = await w.admin.query(api.training.listLaunchableMachines, {});
    expect(list.find((machine) => machine._id === w.machine)).toMatchObject({
      medicalParametersVersion: null,
      medicalParametersStatus: "unsigned",
      medicalParametersReferenceVersion: "0.1.0",
    });
    const live = await w.admin.query(api.training.getMachineLive, {
      machineId: w.machine,
    });
    expect(live).toMatchObject({
      medicalParametersVersion: null,
      medicalParametersStatus: "unsigned",
      medicalParametersReferenceVersion: "0.1.0",
    });
  });
});

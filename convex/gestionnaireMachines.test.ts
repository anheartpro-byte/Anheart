/// <reference types="vite/client" />
/**
 * ANH-154: `machines.setGestionnaireMachines` sets the exact list of machines
 * one gestionnaire manages and touches only that gestionnaire's links.
 *
 * The role cells (who may call it) live in `authorization.matrix.ts`. This
 * suite checks the effect on `machine_gestionnaires`, through the registered
 * mutation and the in-memory tables.
 */
import { describe, expect, it } from "vitest";
import { api } from "./_generated/api";
import type { Doc, Id } from "./_generated/dataModel";
import { as, modules, NOW, seedWorld, type World } from "./test.setup";

type Link = Doc<"machine_gestionnaires">;

async function links(w: World): Promise<Link[]> {
  return await w.t.run((ctx) => ctx.db.query("machine_gestionnaires").collect());
}

function linkOf(
  rows: Link[],
  machineId: Id<"machines">,
  gestionnaireId: Id<"users">,
): Link | undefined {
  const found = rows.filter(
    (r) => r.machineId === machineId && r.gestionnaireId === gestionnaireId,
  );
  expect(found.length).toBeLessThanOrEqual(1);
  return found[0];
}

/** Every link that does not belong to `gestionnaireId`, in a stable order. */
function linksOfOthers(rows: Link[], gestionnaireId: Id<"users">): Link[] {
  return rows
    .filter((r) => r.gestionnaireId !== gestionnaireId)
    .sort((a, b) => a._id.localeCompare(b._id));
}

/** `otherManager` joins `manager` on `machine`: two gestionnaires, one machine. */
async function shareMachine(w: World): Promise<void> {
  await w.t.run((ctx) =>
    ctx.db.insert("machine_gestionnaires", {
      machineId: w.machine,
      gestionnaireId: w.otherManager,
      isOwner: false,
      createdAt: NOW,
      createdBy: w.admin,
    }),
  );
}

function setMachines(
  w: World,
  gestionnaireId: Id<"users">,
  machineIds: Id<"machines">[],
) {
  return as(w.t, "admin").mutation(api.machines.setGestionnaireMachines, {
    gestionnaireId,
    machineIds,
  });
}

describe("ANH-154 machines.setGestionnaireMachines", () => {
  it("removes one of two gestionnaires of a machine without touching the other", async () => {
    // Given `manager` and `otherManager` both manage `machine`.
    const w = await seedWorld(modules);
    await shareMachine(w);
    const before = await links(w);

    // When the admin unchecks every machine of `manager`.
    const result = await setMachines(w, w.manager, []);

    // Then only `manager`'s link is gone; `otherManager`'s rows are the same rows.
    const after = await links(w);
    expect(result).toEqual({ added: 0, removed: 1 });
    expect(linkOf(after, w.machine, w.manager)).toBeUndefined();
    expect(linksOfOthers(after, w.manager)).toEqual(
      linksOfOthers(before, w.manager),
    );
    expect(linkOf(after, w.machine, w.otherManager)).toBeDefined();
    expect(linkOf(after, w.otherMachine, w.otherManager)?.isOwner).toBe(true);
  });

  it("adds a machine without removing the gestionnaire who already manages it", async () => {
    // Given `otherMachine` is managed by `otherManager` only.
    const w = await seedWorld(modules);
    const before = await links(w);

    // When the admin checks `otherMachine` for `manager`, keeping `machine`.
    const result = await setMachines(w, w.manager, [w.machine, w.otherMachine]);

    // Then both manage `otherMachine`, and nobody else's row moved.
    const after = await links(w);
    expect(result).toEqual({ added: 1, removed: 0 });
    expect(linksOfOthers(after, w.manager)).toEqual(
      linksOfOthers(before, w.manager),
    );
    const added = linkOf(after, w.otherMachine, w.manager);
    expect(added).toMatchObject({ isOwner: false, createdBy: w.admin });
    expect(after).toHaveLength(before.length + 1);
  });

  it("keeps an existing link as it is, with its isOwner", async () => {
    // Given `manager` owns `machine` since the seed.
    const w = await seedWorld(modules);
    const owned = linkOf(await links(w), w.machine, w.manager);
    expect(owned?.isOwner).toBe(true);

    // When the list is saved with that machine still checked.
    await setMachines(w, w.manager, [w.machine, w.otherMachine]);

    // Then the very same row is still there, unchanged.
    expect(linkOf(await links(w), w.machine, w.manager)).toEqual(owned);
  });

  it("checks and unchecks in one save", async () => {
    // Given `manager` manages `machine` only.
    const w = await seedWorld(modules);

    // When the admin unchecks `machine` and checks `otherMachine`.
    const result = await setMachines(w, w.manager, [w.otherMachine]);

    // Then the gestionnaire manages exactly the checked machine.
    const after = await links(w);
    expect(result).toEqual({ added: 1, removed: 1 });
    expect(
      after
        .filter((r) => r.gestionnaireId === w.manager)
        .map((r) => r.machineId),
    ).toEqual([w.otherMachine]);
  });

  it("changes nothing when the same list is saved again", async () => {
    const w = await seedWorld(modules);
    await setMachines(w, w.manager, [w.machine, w.otherMachine]);
    const before = await links(w);

    const result = await setMachines(w, w.manager, [w.otherMachine, w.machine]);

    expect(result).toEqual({ added: 0, removed: 0 });
    expect(await links(w)).toEqual(before);
  });

  it("links a machine once when its identifier is sent twice", async () => {
    const w = await seedWorld(modules);

    const result = await setMachines(w, w.manager, [
      w.machine,
      w.otherMachine,
      w.otherMachine,
    ]);

    expect(result).toEqual({ added: 1, removed: 0 });
    expect(linkOf(await links(w), w.otherMachine, w.manager)).toBeDefined();
  });

  it("refuses a machine that does not exist and writes nothing", async () => {
    // Given the identifier of a machine that was removed from the table.
    const w = await seedWorld(modules);
    const gone = await w.t.run(async (ctx) => {
      const id = await ctx.db.insert("machines", {
        name: "Removed machine",
        apiKey: "synthetic-hash",
        status: "offline" as const,
        lastHeartbeat: 0,
        config: { sampleRate: 1000, channels: ["ECG"], batchInterval: 1000 },
        createdAt: NOW,
      });
      await ctx.db.delete(id);
      return id;
    });
    const before = await links(w);

    // When / Then the save is refused and no link was added or removed.
    await expect(setMachines(w, w.manager, [w.otherMachine, gone])).rejects.toThrow(
      /Machine not found/,
    );
    expect(await links(w)).toEqual(before);
  });

  it.each([
    { target: "patient", message: /Target user is not a gestionnaire/ },
    { target: "admin", message: /Target user is not a gestionnaire/ },
  ] as const)(
    "refuses a target account that is not a gestionnaire ($target)",
    async ({ target, message }) => {
      const w = await seedWorld(modules);
      const before = await links(w);

      await expect(setMachines(w, w[target], [w.machine])).rejects.toThrow(
        message,
      );
      expect(await links(w)).toEqual(before);
    },
  );

  it("refuses a gestionnaire account that does not exist", async () => {
    const w = await seedWorld(modules);
    await w.t.run((ctx) => ctx.db.delete(w.stranger));

    await expect(setMachines(w, w.stranger, [w.machine])).rejects.toThrow(
      /Gestionnaire not found/,
    );
  });

  it("refuses a caller who is not an admin before reading the target", async () => {
    // Given a target that does not exist: an allowed caller would be told so.
    const w = await seedWorld(modules);
    await w.t.run((ctx) => ctx.db.delete(w.stranger));

    // When a gestionnaire calls / Then the refusal is the role refusal.
    await expect(
      as(w.t, "manager").mutation(api.machines.setGestionnaireMachines, {
        gestionnaireId: w.stranger,
        machineIds: [],
      }),
    ).rejects.toThrow(/Unauthorized/);
  });
});

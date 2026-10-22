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
        createdAt: NOW,
      });
      await ctx.db.delete(id);
      return id;
    });
    const before = await links(w);

    // When / Then the save is refused and no link was added or removed.
    await expect(setMachines(w, w.manager, [w.otherMachine, gone])).rejects.toThrow(
      /machine_not_found/,
    );
    expect(await links(w)).toEqual(before);
  });

  it.each([
    { target: "patient", message: /not_a_gestionnaire/ },
    { target: "admin", message: /not_a_gestionnaire/ },
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
      /gestionnaire_not_found/,
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
    ).rejects.toThrow(/not_authorized/);
  });
});

/** Mark a machine deleted, as `machines.deleteMachine` does. */
async function deleteMachine(w: World, machineId: Id<"machines">) {
  await w.t.run((ctx) =>
    ctx.db.patch(machineId, {
      isDeleted: true,
      deletedAt: NOW,
      deletedBy: w.admin,
      status: "offline" as const,
    }),
  );
}

describe("ANH-194 EX-4 a deleted machine on the gestionnaire's page", () => {
  it("is neither listed nor counted, and its link is kept for a restore", async () => {
    // Given `manager` manages `machine` and `otherMachine`, then `otherMachine` is deleted.
    const w = await seedWorld(modules);
    await setMachines(w, w.manager, [w.machine, w.otherMachine]);
    await deleteMachine(w, w.otherMachine);

    // Then the page's two readings leave it out.
    const listed = await as(w.t, "admin").query(
      api.machines.getMachinesForGestionnaire,
      { gestionnaireId: w.manager },
    );
    expect(listed.map((m) => m._id)).toEqual([w.machine]);
    const ownList = await as(w.t, "manager").query(
      api.machines.getMachinesForGestionnaire,
      {},
    );
    expect(ownList.map((m) => m._id)).toEqual([w.machine]);
    const card = (
      await as(w.t, "admin").query(api.users.listGestionnaires, {})
    ).find((g) => g._id === w.manager);
    expect(card?.machineCount).toBe(1);
    // And the link itself is still in the table.
    expect(linkOf(await links(w), w.otherMachine, w.manager)).toBeDefined();
  });

  it("keeps its link when the saved list does not name it", async () => {
    // Given `manager` also manages `otherMachine`, which is then deleted.
    const w = await seedWorld(modules);
    await setMachines(w, w.manager, [w.machine, w.otherMachine]);
    await deleteMachine(w, w.otherMachine);
    const kept = linkOf(await links(w), w.otherMachine, w.manager);

    // When the page saves the list it can build: the live machines only.
    const result = await setMachines(w, w.manager, [w.machine]);

    // Then nothing was removed, and the deleted machine's row is the same row.
    expect(result).toEqual({ added: 0, removed: 0 });
    expect(linkOf(await links(w), w.otherMachine, w.manager)).toEqual(kept);
  });

  it("keeps its link when the list names it, without counting it", async () => {
    const w = await seedWorld(modules);
    await setMachines(w, w.manager, [w.machine, w.otherMachine]);
    await deleteMachine(w, w.otherMachine);
    const before = await links(w);

    const result = await setMachines(w, w.manager, [w.machine, w.otherMachine]);

    expect(result).toEqual({ added: 0, removed: 0 });
    expect(await links(w)).toEqual(before);
  });

  it("is never linked, and the refused save writes nothing", async () => {
    // Given `otherMachine`, not managed by `manager`, is deleted.
    const w = await seedWorld(modules);
    await deleteMachine(w, w.otherMachine);
    const before = await links(w);

    // When an admin names it for `manager`, with `machine` unchecked.
    await expect(setMachines(w, w.manager, [w.otherMachine])).rejects.toThrow(
      /machine_not_found/,
    );

    // Then the save is refused as a whole: `machine` is still linked.
    expect(await links(w)).toEqual(before);
  });
});

describe("ANH-232 orphaned gestionnaire machine links", () => {
  const auditEntries = (w: World) =>
    w.t.run((ctx) => ctx.db.query("audit_log").collect());
  const seed = async () => {
    const w = await seedWorld(modules);
    const auditBefore = await auditEntries(w);
    return { w, auditBefore };
  };

  it("T01-a removes an unnamed orphan, counts only visible removals, and audits both", async () => {
    const { w, auditBefore } = await seed();
    await w.t.run(async (ctx) => {
      await ctx.db.insert("machine_gestionnaires", {
        organizationId: w.orgA,
        machineId: w.otherMachine,
        gestionnaireId: w.manager,
        isOwner: false,
        createdAt: NOW,
        createdBy: w.admin,
      });
      await ctx.db.delete(w.otherMachine);
    });
    const before = await links(w);

    expect(await setMachines(w, w.manager, [])).toEqual({ added: 0, removed: 1 });

    const after = await links(w);
    expect(linkOf(after, w.otherMachine, w.manager)).toBeUndefined();
    expect(linkOf(after, w.machine, w.manager)).toBeUndefined();
    expect(linksOfOthers(after, w.manager)).toEqual(
      linksOfOthers(before, w.manager),
    );
    const entries = (await auditEntries(w)).slice(auditBefore.length);
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({
      action: "gestionnaire_machines.set",
      organizationId: w.orgA,
      targetId: w.manager,
      details: {
        added: [],
        removed: expect.arrayContaining([w.machine, w.otherMachine]),
      },
    });
    expect(entries[0].details?.removed).toHaveLength(2);
  });

  it("T01-b removes a named orphan without error or recreating its link", async () => {
    const { w, auditBefore } = await seed();
    await w.t.run((ctx) => ctx.db.delete(w.machine));

    expect(
      await setMachines(w, w.manager, [w.machine, w.otherMachine, w.machine]),
    ).toEqual({ added: 1, removed: 0 });

    const after = await links(w);
    expect(linkOf(after, w.machine, w.manager)).toBeUndefined();
    expect(linkOf(after, w.otherMachine, w.manager)).toMatchObject({
      organizationId: w.orgA,
      isOwner: false,
    });
    expect((await auditEntries(w)).slice(auditBefore.length)).toMatchObject([
      {
        action: "gestionnaire_machines.set",
        details: { added: [w.otherMachine], removed: [w.machine] },
      },
    ]);
  });

  it("T01-c audits an orphan-only cleanup despite zero visible changes", async () => {
    const { w, auditBefore } = await seed();
    await w.t.run((ctx) => ctx.db.delete(w.machine));

    expect(await setMachines(w, w.manager, [])).toEqual({ added: 0, removed: 0 });
    expect(linkOf(await links(w), w.machine, w.manager)).toBeUndefined();
    const entries = (await auditEntries(w)).slice(auditBefore.length);
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({
      action: "gestionnaire_machines.set",
      organizationId: w.orgA,
      targetId: w.manager,
      details: { added: [], removed: [w.machine] },
    });

    expect(await setMachines(w, w.manager, [])).toEqual({ added: 0, removed: 0 });
    expect(await auditEntries(w)).toEqual([...auditBefore, ...entries]);
  });

  it.each([false, true])(
    "T01-d preserves a soft-deleted link without counting or auditing it (named: %s)",
    async (named) => {
      const { w, auditBefore } = await seed();
      await deleteMachine(w, w.machine);
      const before = await links(w);

      expect(await setMachines(w, w.manager, named ? [w.machine] : [])).toEqual({
        added: 0,
        removed: 0,
      });
      expect(await links(w)).toEqual(before);
      expect(await auditEntries(w)).toEqual(auditBefore);
    },
  );

  it("T01-e cleans local orphans and leaves the same gestionnaire's foreign orphan untouched", async () => {
    const { w, auditBefore } = await seed();
    const foreignLink = await w.t.run(async (ctx) => {
      const id = await ctx.db.insert("machine_gestionnaires", {
        organizationId: w.orgB,
        machineId: w.orgBMachine,
        gestionnaireId: w.manager,
        isOwner: false,
        createdAt: NOW,
        createdBy: w.admin,
      });
      await ctx.db.delete(w.orgBMachine);
      await ctx.db.delete(w.machine);
      return await ctx.db.get(id);
    });

    expect(await setMachines(w, w.manager, [w.otherMachine])).toEqual({
      added: 1,
      removed: 0,
    });
    const after = await links(w);
    expect(linkOf(after, w.machine, w.manager)).toBeUndefined();
    expect(linkOf(after, w.orgBMachine, w.manager)).toEqual(foreignLink);
    expect(linkOf(after, w.otherMachine, w.manager)?.organizationId).toBe(w.orgA);
    expect((await auditEntries(w)).slice(auditBefore.length)).toMatchObject([
      {
        organizationId: w.orgA,
        details: { added: [w.otherMachine], removed: [w.machine] },
      },
    ]);
  });

  it.each(["live", "deleted", "missing"] as const)(
    "T01-e refuses an unlinked foreign machine (%s) before any cleanup or audit",
    async (state) => {
      const { w, auditBefore } = await seed();
      await w.t.run((ctx) => ctx.db.delete(w.machine));
      if (state === "deleted") await deleteMachine(w, w.orgBMachine);
      if (state === "missing") {
        await w.t.run((ctx) => ctx.db.delete(w.orgBMachine));
      }
      const before = await links(w);

      for (const actor of ["orgAdmin", "admin"] as const) {
        await expect(
          as(w.t, actor).mutation(api.machines.setGestionnaireMachines, {
            gestionnaireId: w.manager,
            machineIds: [w.otherMachine, w.orgBMachine],
          }),
        ).rejects.toThrow(/machine_not_found/);
        expect(await links(w)).toEqual(before);
        expect(await auditEntries(w)).toEqual(auditBefore);
      }
    },
  );

  it.each(["deleted", "missing"] as const)(
    "T01-e refuses an unlinked local machine (%s) before any cleanup or audit",
    async (state) => {
      const { w, auditBefore } = await seed();
      await w.t.run((ctx) => ctx.db.delete(w.machine));
      if (state === "deleted") await deleteMachine(w, w.otherMachine);
      else await w.t.run((ctx) => ctx.db.delete(w.otherMachine));
      const before = await links(w);

      await expect(setMachines(w, w.manager, [w.otherMachine])).rejects.toThrow(
        /machine_not_found/,
      );
      expect(await links(w)).toEqual(before);
      expect(await auditEntries(w)).toEqual(auditBefore);
    },
  );

  it.each([
    { actor: "manager", message: /not_authorized/ },
    { actor: "patient", message: /not_authorized/ },
    { actor: "orgBAdmin", message: /gestionnaire_not_found/ },
  ] as const)(
    "T01-e preserves the refusal for $actor without cleanup or audit",
    async ({ actor, message }) => {
      const { w, auditBefore } = await seed();
      await w.t.run((ctx) => ctx.db.delete(w.machine));
      const before = await links(w);

      await expect(
        as(w.t, actor).mutation(api.machines.setGestionnaireMachines, {
          gestionnaireId: w.manager,
          machineIds: [],
        }),
      ).rejects.toThrow(message);
      expect(await links(w)).toEqual(before);
      expect(await auditEntries(w)).toEqual(auditBefore);
    },
  );
});

describe("ANH-194 EX-4 machines.assignMachineToGestionnaires", () => {
  function assign(w: World, gestionnaireIds: Id<"users">[]) {
    return as(w.t, "admin").mutation(api.machines.assignMachineToGestionnaires, {
      machineId: w.machine,
      gestionnaireIds,
    });
  }

  it("writes one row for an identifier sent twice", async () => {
    const w = await seedWorld(modules);

    await assign(w, [w.manager, w.otherManager, w.otherManager, w.manager]);

    const rows = (await links(w)).filter((r) => r.machineId === w.machine);
    expect(
      rows.map((r) => ({ id: r.gestionnaireId, isOwner: r.isOwner })),
    ).toEqual([
      { id: w.manager, isOwner: true },
      { id: w.otherManager, isOwner: false },
    ]);
  });

  it("keeps the row of a gestionnaire who stays, and writes nothing for the same list", async () => {
    // Given `manager` owns `machine` since the seed.
    const w = await seedWorld(modules);
    const owned = linkOf(await links(w), w.machine, w.manager);

    // When the admin adds `otherManager`, then saves the same list again.
    await assign(w, [w.manager, w.otherManager]);
    const afterAdd = await links(w);
    await assign(w, [w.manager, w.otherManager]);

    // Then `manager`'s row was never recreated, and the second save changed nothing.
    expect(linkOf(afterAdd, w.machine, w.manager)).toEqual(owned);
    expect(await links(w)).toEqual(afterAdd);
  });

  it("removes a second row left for the same gestionnaire", async () => {
    // Given a duplicate row written before this rule.
    const w = await seedWorld(modules);
    await w.t.run((ctx) =>
      ctx.db.insert("machine_gestionnaires", {
        organizationId: w.orgA,
        machineId: w.machine,
        gestionnaireId: w.manager,
        isOwner: false,
        createdAt: NOW,
        createdBy: w.admin,
      }),
    );

    await assign(w, [w.manager]);

    const rows = (await links(w)).filter(
      (r) => r.machineId === w.machine && r.gestionnaireId === w.manager,
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].isOwner).toBe(true);
  });

  it("makes the first gestionnaire kept the owner when the first one sent is not a gestionnaire", async () => {
    const w = await seedWorld(modules);

    await assign(w, [w.patient, w.otherManager, w.manager]);

    const machine = await as(w.t, "admin").query(api.machines.getMachine, {
      machineId: w.machine,
    });
    expect(
      machine?.gestionnaires.map((g) => ({ id: g._id, isOwner: g.isOwner })),
    ).toEqual([
      { id: w.otherManager, isOwner: true },
      { id: w.manager, isOwner: false },
    ]);
  });

  it("creates a machine with one row for an identifier sent twice", async () => {
    const w = await seedWorld(modules);

    const { machineId } = await as(w.t, "admin").mutation(
      api.machines.createMachine,
      {
        name: "Banc",
        organizationId: w.orgA,
        gestionnaireIds: [w.manager, w.manager, w.otherManager],
      },
    );

    const rows = (await links(w)).filter((r) => r.machineId === machineId);
    expect(
      rows.map((r) => ({ id: r.gestionnaireId, isOwner: r.isOwner })),
    ).toEqual([
      { id: w.manager, isOwner: true },
      { id: w.otherManager, isOwner: false },
    ]);
  });
});

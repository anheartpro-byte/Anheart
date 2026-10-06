/// <reference types="vite/client" />
/**
 * ANH-155: what the machine form relies on, replayed against the registered
 * functions and the in-memory tables.
 *
 * The form saves the name and the place with `machines.updateMachine`. It
 * makes the admin-only `machines.assignMachineToGestionnaires` call only for a
 * caller allowed to choose the gestionnaires, and only when the list changed,
 * order included (rule in `lib/machineForm.ts`). This suite fixes the server
 * side of that contract. The role cells of both functions are in
 * `authorization.matrix.ts`.
 */
import { describe, expect, it } from "vitest";
import { api } from "./_generated/api";
import { as, modules, seedWorld, type World } from "./test.setup";

/** The form's edit call, with the two fields a gestionnaire changes. */
function saveNameAndPlace(w: World) {
  return as(w.t, "manager").mutation(api.machines.updateMachine, {
    machineId: w.machine,
    name: "Centri Lyon",
    location: "Salle 2",
  });
}

function links(w: World) {
  return w.t.run((ctx) => ctx.db.query("machine_gestionnaires").collect());
}

describe("ANH-155 a gestionnaire edits the name and the place of a machine", () => {
  it("saves without error and the machine page reads the new name and place", async () => {
    // Given `manager` manages `machine`.
    const w = await seedWorld(modules);

    // When they save the form: one call, the edit.
    await expect(saveNameAndPlace(w)).resolves.toBeNull();

    // Then the query behind the machine page returns the new values to them.
    const machine = await as(w.t, "manager").query(api.machines.getMachine, {
      machineId: w.machine,
    });
    expect(machine).toMatchObject({ name: "Centri Lyon", location: "Salle 2" });
  });

  it("leaves every gestionnaire link as it was", async () => {
    const w = await seedWorld(modules);
    const before = await links(w);

    await saveNameAndPlace(w);

    expect(await links(w)).toEqual(before);
  });

  it("keeps the gestionnaire list call reserved to an admin", async () => {
    // Given the same list the form holds for this machine.
    const w = await seedWorld(modules);
    const before = await links(w);

    // When a gestionnaire makes the call the form no longer makes for them.
    await expect(
      as(w.t, "manager").mutation(api.machines.assignMachineToGestionnaires, {
        machineId: w.machine,
        gestionnaireIds: [w.manager],
      }),
    ).rejects.toThrow(/Unauthorized/);

    // Then nothing moved: the refusal is why the form must not make it.
    expect(await links(w)).toEqual(before);
  });
});

describe("ANH-155 the order of the gestionnaire list decides the owner", () => {
  it("makes the first gestionnaire of the list the owner shown on the machine page", async () => {
    // Given `manager` owns `machine` and `otherManager` joins it.
    const w = await seedWorld(modules);
    const admin = as(w.t, "admin");
    await admin.mutation(api.machines.assignMachineToGestionnaires, {
      machineId: w.machine,
      gestionnaireIds: [w.manager, w.otherManager],
    });

    // When an admin saves the same two people in the other order, which is
    // what the form sends after the owner is unchecked then checked again.
    await admin.mutation(api.machines.assignMachineToGestionnaires, {
      machineId: w.machine,
      gestionnaireIds: [w.otherManager, w.manager],
    });

    // Then the owner badge moved: this is why the form treats the order as a
    // change (rule in `lib/machineForm.ts`).
    const machine = await admin.query(api.machines.getMachine, {
      machineId: w.machine,
    });
    expect(
      machine?.gestionnaires.map((g) => ({ id: g._id, isOwner: g.isOwner })),
    ).toEqual([
      { id: w.otherManager, isOwner: true },
      { id: w.manager, isOwner: false },
    ]);
  });
});

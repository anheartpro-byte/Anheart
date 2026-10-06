/// <reference types="vite/client" />
/**
 * ANH-155: what the machine form does for a gestionnaire, replayed against the
 * registered functions and the in-memory tables.
 *
 * The form saves the name and the place with `machines.updateMachine`. It
 * makes the admin-only `machines.assignMachineToGestionnaires` call only for a
 * caller allowed to choose the gestionnaires (rule in `lib/machineForm.ts`).
 * This suite fixes the server side of that contract. The role cells of both
 * functions are in `authorization.matrix.ts`.
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

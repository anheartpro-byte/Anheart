import { describe, expect, it, vi } from "vitest";
import {
  canAssignGestionnaires,
  gestionnaireIdsToSubmit,
  submitGestionnaireList,
} from "./machineForm";

describe("ANH-155 canAssignGestionnaires", () => {
  it.each([
    { role: "admin", allowed: true },
    { role: "gestionnaire", allowed: false },
    { role: "user", allowed: false },
    { role: undefined, allowed: false },
  ])("is $allowed for the role $role", ({ role, allowed }) => {
    expect(canAssignGestionnaires(role)).toBe(allowed);
  });
});

describe("ANH-155 gestionnaireIdsToSubmit", () => {
  it("sends nothing when a gestionnaire edits the name and the place", () => {
    // Given a gestionnaire saves the form of a machine managed by G1 and G2.
    // The form holds the machine's gestionnaires even though it shows no box.
    const sent = gestionnaireIdsToSubmit({
      role: "gestionnaire",
      current: ["G1", "G2"],
      selected: ["G1", "G2"],
    });
    // Then the admin-only call is not made.
    expect(sent).toBeNull();
  });

  it.each(["gestionnaire", "user", undefined])(
    "sends nothing for the role %s even if the list differs",
    (role) => {
      expect(
        gestionnaireIdsToSubmit({ role, current: ["G1"], selected: ["G2"] }),
      ).toBeNull();
    },
  );

  it("sends nothing when an admin leaves the boxes as they were", () => {
    expect(
      gestionnaireIdsToSubmit({
        role: "admin",
        current: ["G1", "G2"],
        selected: ["G1", "G2"],
      }),
    ).toBeNull();
  });

  it("sends the reordered list when an admin unchecks the owner and checks it again", () => {
    // Given G1 is first, so it is the owner. The admin unchecks G1 then checks
    // it again: the form's list is now G2 then G1.
    const sent = gestionnaireIdsToSubmit({
      role: "admin",
      current: ["G1", "G2"],
      selected: ["G2", "G1"],
    });
    // Then the list is sent in that order: the server makes G2 the owner.
    expect(sent).toEqual(["G2", "G1"]);
  });

  it("sends nothing when a repeated identifier leaves the same list", () => {
    // Read once, G1 G1 G2 is the machine's current list G1 G2.
    expect(
      gestionnaireIdsToSubmit({
        role: "admin",
        current: ["G1", "G2"],
        selected: ["G1", "G1", "G2"],
      }),
    ).toBeNull();
    expect(
      gestionnaireIdsToSubmit({
        role: "admin",
        current: ["G1", "G1", "G2"],
        selected: ["G1", "G2"],
      }),
    ).toBeNull();
  });

  it("sends nothing when a machine without gestionnaire keeps none", () => {
    expect(
      gestionnaireIdsToSubmit({ role: "admin", current: [], selected: [] }),
    ).toBeNull();
  });

  it("sends nothing when the form holds no list", () => {
    expect(
      gestionnaireIdsToSubmit({
        role: "admin",
        current: ["G1"],
        selected: undefined,
      }),
    ).toBeNull();
  });

  it("sends the checked list when an admin adds a gestionnaire", () => {
    expect(
      gestionnaireIdsToSubmit({
        role: "admin",
        current: ["G1"],
        selected: ["G1", "G2"],
      }),
    ).toEqual(["G1", "G2"]);
  });

  it("sends the checked list when an admin removes a gestionnaire", () => {
    expect(
      gestionnaireIdsToSubmit({
        role: "admin",
        current: ["G1", "G2"],
        selected: ["G2"],
      }),
    ).toEqual(["G2"]);
  });

  it("sends an empty list when an admin unchecks every gestionnaire", () => {
    expect(
      gestionnaireIdsToSubmit({ role: "admin", current: ["G1"], selected: [] }),
    ).toEqual([]);
  });

  it("sends the list when an admin swaps one gestionnaire for another", () => {
    // Same number of boxes, different people.
    expect(
      gestionnaireIdsToSubmit({
        role: "admin",
        current: ["G1", "G2"],
        selected: ["G1", "G3"],
      }),
    ).toEqual(["G1", "G3"]);
  });

  it("sends each gestionnaire once", () => {
    expect(
      gestionnaireIdsToSubmit({
        role: "admin",
        current: [],
        selected: ["G1", "G1", "G2"],
      }),
    ).toEqual(["G1", "G2"]);
  });
});

/**
 * The step the form runs after `machines.updateMachine`. `assign` stands in
 * for the admin-only `machines.assignMachineToGestionnaires` mutation.
 */
describe("ANH-155 submitGestionnaireList", () => {
  const assignStandIn = () => vi.fn(async (ids: string[]) => ids.length);

  it("does not call the admin mutation when a gestionnaire saves the form", async () => {
    // Given the form of a gestionnaire: it holds the machine's gestionnaires
    // (G1, G2) although it shows no box for them.
    const assign = assignStandIn();

    // When the gestionnaire saves the name and the place.
    await submitGestionnaireList({
      role: "gestionnaire",
      current: ["G1", "G2"],
      selected: ["G1", "G2"],
      assign,
    });

    // Then the admin-only mutation was never called.
    expect(assign).not.toHaveBeenCalled();
  });

  it.each(["gestionnaire", "user", undefined])(
    "does not call the admin mutation for the role %s even if the list differs",
    async (role) => {
      const assign = assignStandIn();

      await submitGestionnaireList({
        role,
        current: ["G1"],
        selected: ["G2"],
        assign,
      });

      expect(assign).not.toHaveBeenCalled();
    },
  );

  it("does not call the admin mutation when an admin leaves the boxes as they were", async () => {
    const assign = assignStandIn();

    await submitGestionnaireList({
      role: "admin",
      current: ["G1", "G2"],
      selected: ["G1", "G2"],
      assign,
    });

    expect(assign).not.toHaveBeenCalled();
  });

  it("calls the admin mutation once with the form's list when an admin changes it", async () => {
    const assign = assignStandIn();

    await submitGestionnaireList({
      role: "admin",
      current: ["G1", "G2"],
      selected: ["G2", "G1"],
      assign,
    });

    expect(assign).toHaveBeenCalledTimes(1);
    expect(assign).toHaveBeenCalledWith(["G2", "G1"]);
  });

  it("calls the admin mutation with an empty list when an admin unchecks everyone", async () => {
    const assign = assignStandIn();

    await submitGestionnaireList({
      role: "admin",
      current: ["G1"],
      selected: [],
      assign,
    });

    expect(assign).toHaveBeenCalledWith([]);
  });

  it("lets a refusal of the admin mutation reach the form", async () => {
    const refusal = new Error("Unauthorized. Required roles: admin.");
    const assign = vi.fn(async () => {
      throw refusal;
    });

    await expect(
      submitGestionnaireList({
        role: "admin",
        current: ["G1"],
        selected: ["G2"],
        assign,
      }),
    ).rejects.toBe(refusal);
  });

  it("waits for the admin mutation before the form reports success", async () => {
    // Given a mutation that settles only when released.
    let release: () => void = () => undefined;
    const assign = vi.fn(
      () => new Promise<void>((resolve) => (release = resolve)),
    );
    let settled = false;

    const pending = submitGestionnaireList({
      role: "admin",
      current: ["G1"],
      selected: ["G2"],
      assign,
    }).then(() => {
      settled = true;
    });
    await Promise.resolve();
    await Promise.resolve();

    // Then the step is still pending until the mutation settles.
    expect(settled).toBe(false);
    release();
    await pending;
    expect(settled).toBe(true);
  });
});

import { describe, expect, it } from "vitest";
import { canAssignGestionnaires, gestionnaireIdsToSubmit } from "./machineForm";

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

  it("does not treat the order of the boxes as a change", () => {
    // Given an admin unchecked G1 then checked it again: same people.
    expect(
      gestionnaireIdsToSubmit({
        role: "admin",
        current: ["G1", "G2"],
        selected: ["G2", "G1"],
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

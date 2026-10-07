import { describe, expect, it } from "vitest";
import { patientIdsToSave } from "./gestionnairePatients";

describe("ANH-208 patientIdsToSave", () => {
  it("sends exactly the checked boxes", () => {
    // Given the gestionnaire manages A, and the window lists A, B and C.
    // When B is checked and A unchecked.
    const sent = patientIdsToSave({
      checked: ["B"],
      listed: ["A", "B", "C"],
      linked: ["A"],
    });
    // Then B is sent (added) and A is not (removed).
    expect(sent).toEqual(["B"]);
  });

  it("sends the gestionnaire's own patients when no box was touched", () => {
    expect(
      patientIdsToSave({
        checked: ["A", "B"],
        listed: ["A", "B", "C"],
        linked: ["A", "B"],
      }),
    ).toEqual(["A", "B"]);
  });

  it("keeps a managed patient who has no box in the window", () => {
    // Given the gestionnaire manages A and a patient D the window does not list.
    const sent = patientIdsToSave({
      checked: ["A"],
      listed: ["A", "B"],
      linked: ["A", "D"],
    });
    // Then saving does not remove D.
    expect([...sent].sort()).toEqual(["A", "D"]);
  });

  it("never adds a patient who has no box", () => {
    // Given a checked identifier that the window no longer lists.
    const sent = patientIdsToSave({
      checked: ["A", "X"],
      listed: ["A"],
      linked: [],
    });
    expect(sent).toEqual(["A"]);
  });

  it("sends each patient once", () => {
    const sent = patientIdsToSave({
      checked: ["A", "A", "B"],
      listed: ["A", "B"],
      linked: ["A"],
    });
    expect([...sent].sort()).toEqual(["A", "B"]);
  });
});

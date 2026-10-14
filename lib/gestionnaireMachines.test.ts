import { describe, expect, it } from "vitest";
import { machineIdsToSave, tickedBoxes } from "./gestionnaireMachines";

describe("ANH-154 machineIdsToSave", () => {
  it("sends exactly the checked boxes", () => {
    // Given the gestionnaire manages A, and the window lists A, B and C.
    // When B is checked and A unchecked.
    const sent = machineIdsToSave({
      checked: ["B"],
      listed: ["A", "B", "C"],
      linked: ["A"],
    });
    // Then B is sent (added) and A is not (removed).
    expect(sent).toEqual(["B"]);
  });

  it("sends an empty list when every box is unchecked", () => {
    expect(
      machineIdsToSave({ checked: [], listed: ["A", "B"], linked: ["A", "B"] }),
    ).toEqual([]);
  });

  it("keeps a managed machine that has no box in the window", () => {
    // Given the gestionnaire manages A and a deleted machine D with no box.
    const sent = machineIdsToSave({
      checked: ["A"],
      listed: ["A", "B"],
      linked: ["A", "D"],
    });
    // Then saving does not remove D.
    expect([...sent].sort()).toEqual(["A", "D"]);
  });

  it("never adds a machine that has no box", () => {
    // Given a stale checked identifier that the window no longer lists.
    const sent = machineIdsToSave({
      checked: ["A", "X"],
      listed: ["A"],
      linked: [],
    });
    expect(sent).toEqual(["A"]);
  });

  it("sends each machine once", () => {
    const sent = machineIdsToSave({
      checked: ["A", "A", "B"],
      listed: ["A", "B"],
      linked: ["A"],
    });
    expect([...sent].sort()).toEqual(["A", "B"]);
  });
});

describe("ANH-194 tickedBoxes", () => {
  it("ticks the boxes of the links on the server when nothing was clicked", () => {
    expect(
      tickedBoxes({
        listed: ["A", "B", "C"],
        linked: ["C", "A", "D"],
        choices: new Map(),
      }),
    ).toEqual(["A", "C"]);
  });

  it("keeps what the visitor clicked, whatever the server says since", () => {
    // Given A was unticked and B ticked, then the server answers again:
    // A still linked, B still not.
    const ticked = tickedBoxes({
      listed: ["A", "B", "C"],
      linked: ["A"],
      choices: new Map([
        ["A", false],
        ["B", true],
      ]),
    });
    expect(ticked).toEqual(["B"]);
  });

  it("follows the server for a box nobody clicked", () => {
    // Given the visitor ticked B; another admin then links C and unlinks A.
    const ticked = tickedBoxes({
      listed: ["A", "B", "C"],
      linked: ["C"],
      choices: new Map([["B", true]]),
    });
    expect(ticked).toEqual(["B", "C"]);
  });

  it("ticks no box for an identifier the window does not list", () => {
    expect(
      tickedBoxes({
        listed: ["A"],
        linked: ["D"],
        choices: new Map([["X", true]]),
      }),
    ).toEqual([]);
  });
});

import { click, messages, render } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  answer,
  asks,
  mutation,
  mutationCalls,
  resetConvex,
} from "@/test-support/convex";
import { choose, closeWindow, options, windowOpen } from "@/test-support/ui";
import type { Id } from "@/convex/_generated/dataModel";
import { dismissFeedback, getFeedbackToasts } from "@/lib/feedback";
import { LaunchRightsCard } from "./LaunchRightsCard";

/**
 * The card where a manager says which patients may launch an auto session on
 * a machine by themselves: who holds the right, who can be given it by an
 * administrator and by a gestionnaire, what granting and revoking send, and
 * what is shown when the server refuses.
 *
 * The server decides in the end (convex/training.ts). What is proven here is
 * that a gestionnaire is only ever offered his own patients, that nothing is
 * sent before a patient is chosen or a revocation confirmed, and that the
 * right named on the screen is the one sent.
 */

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/convex")).convexReact,
);
vi.mock(
  "@/components/ui/dialog",
  async () => (await import("@/test-support/ui")).dialog,
);
vi.mock(
  "@/components/ui/select",
  async () => (await import("@/test-support/ui")).select,
);

const fr = messages.fr;
const t = fr.training.rights;
const GRANT = "training:grantLaunchRight";
const REVOKE = "training:revokeLaunchRight";
const machineId = "machine-1" as Id<"machines">;

const PAUL = {
  _id: "paul",
  firstName: "Paul",
  lastName: "Martin",
  email: "paul@anheart.test",
};
const LINA = {
  _id: "lina",
  firstName: "Lina",
  lastName: "Roy",
  email: "lina@anheart.test",
};
const PAUL_OPTION = "Paul Martin (paul@anheart.test)";
const LINA_OPTION = "Lina Roy (lina@anheart.test)";

/** A right as `training.listLaunchRights` answers it. Granted on 3 March 2026. */
function right(user: typeof PAUL, hrMax: number | null) {
  return {
    userId: user._id,
    name: `${user.firstName} ${user.lastName}`,
    email: user.email,
    hrMax,
    grantedByName: "Ada Lovelace",
    createdAt: Date.UTC(2026, 2, 3, 12),
  };
}

function card(isAdmin: boolean, locale: "fr" | "en" = "fr") {
  return render(<LaunchRightsCard machineId={machineId} isAdmin={isAdmin} />, {
    locale,
  });
}

function shown() {
  return getFeedbackToasts().map(({ kind, message }) => ({ kind, message }));
}

beforeEach(() => {
  vi.useFakeTimers();
  resetConvex();
});
afterEach(() => {
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
  vi.useRealTimers();
});

describe("ANH-203 launch rights: who holds the right", () => {
  it("shows a placeholder while the rights load, and no holder", () => {
    answer("users:listUsers", [PAUL]);

    const screen = card(true);

    expect(
      screen.all((element) => element.getAttribute("data-slot") === "skeleton"),
    ).toHaveLength(1);
    expect(screen.text()).not.toContain(t.none);
    expect(screen.hasButton(t.revoke)).toBe(false);
  });

  it("says that no patient holds the right when the list is empty", () => {
    answer("training:listLaunchRights", []);
    answer("users:listUsers", []);

    const screen = card(true);

    expect(screen.text()).toContain(t.none);
    expect(screen.hasButton(t.revoke)).toBe(false);
  });

  it("lists each holder with his max heart rate, who granted the right and when", () => {
    answer("training:listLaunchRights", [right(PAUL, 172), right(LINA, null)]);
    answer("users:listUsers", [PAUL, LINA]);

    const screen = card(true);

    expect(screen.text()).toContain(
      "Paul Martin paul@anheart.test · FC max : 172 bpm",
    );
    expect(screen.text()).toContain(
      `Lina Roy lina@anheart.test · ${t.hrMaxMissing}`,
    );
    expect(screen.text()).toContain("Accordé par Ada Lovelace, 3 mars 2026");
    expect(
      screen.all(
        (element) =>
          screen.textOf(element) === t.revoke && element.localName === "button",
      ),
    ).toHaveLength(2);
  });

  it("writes the date of the grant in the language of the page", () => {
    answer("training:listLaunchRights", [right(PAUL, 172)]);
    answer("users:listUsers", []);

    const screen = card(true, "en");

    expect(screen.text()).toContain("Granted by Ada Lovelace, Mar 3, 2026");
  });
});

describe("ANH-203 launch rights: who can be given the right", () => {
  it("an administrator is offered every patient who does not hold it yet", () => {
    answer("training:listLaunchRights", [right(PAUL, 172)]);
    answer("users:listUsers", [PAUL, LINA]);

    const screen = card(true);

    expect(options(screen)).toEqual([LINA_OPTION]);
    expect(asks("users:listUsers")).toEqual([{ role: "user" }]);
    expect(asks("users:getPatientsForGestionnaire")).toEqual(["skip"]);
    expect(asks("training:listLaunchRights")).toEqual([{ machineId }]);
  });

  it("a gestionnaire is offered his own patients only: the list of all users is never asked", () => {
    answer("training:listLaunchRights", []);
    answer("users:getPatientsForGestionnaire", [LINA]);
    // Even if the list of all users were there, it is not the one offered.
    answer("users:listUsers", [PAUL, LINA]);

    const screen = card(false);

    expect(options(screen)).toEqual([LINA_OPTION]);
    expect(asks("users:listUsers")).toEqual(["skip"]);
    expect(asks("users:getPatientsForGestionnaire")).toEqual([{}]);
  });

  it("says that no other patient is eligible once all of them hold the right", () => {
    answer("training:listLaunchRights", [right(PAUL, 172)]);
    answer("users:listUsers", [PAUL]);

    const screen = card(true);

    expect(options(screen)).toEqual([]);
    expect(screen.text()).toContain(t.noCandidates);
    expect(screen.text()).not.toContain(t.selectUser);
  });

  it("does not say that nobody is eligible while the patients are still loading", () => {
    answer("training:listLaunchRights", []);

    const screen = card(true);

    expect(screen.text()).toContain(t.selectUser);
    expect(screen.text()).not.toContain(t.noCandidates);
  });
});

describe("ANH-203 launch rights: granting", () => {
  it("cannot grant before a patient is chosen", async () => {
    answer("training:listLaunchRights", []);
    answer("users:listUsers", [PAUL]);
    const screen = card(true);

    expect(screen.button(t.grant).hasAttribute("disabled")).toBe(true);
    await click(screen.button(t.grant));

    expect(mutationCalls()).toEqual({});
  });

  it("grants the right on this machine to the patient chosen, then empties the choice", async () => {
    answer("training:listLaunchRights", []);
    answer("users:listUsers", [PAUL, LINA]);
    const screen = card(true);

    await choose(screen, LINA_OPTION);
    await click(screen.button(t.grant));

    expect(mutationCalls()).toEqual({
      [GRANT]: [[{ machineId, userId: "lina" }]],
    });
    expect(shown()).toEqual([
      { kind: "success", message: fr.feedback.launchRightGranted },
    ]);
    // Back to "choose a patient": a second click grants nothing more.
    expect(screen.text()).toContain(t.selectUser);
    expect(screen.button(t.grant).hasAttribute("disabled")).toBe(true);
  });

  it("shows the server's refusal in the card and keeps the patient chosen", async () => {
    answer("training:listLaunchRights", []);
    answer("users:getPatientsForGestionnaire", [PAUL]);
    mutation(GRANT).mockRejectedValue(
      new Error("You do not manage this patient"),
    );
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const screen = card(false);

    await choose(screen, PAUL_OPTION);
    await click(screen.button(t.grant));

    expect(screen.text()).toContain("You do not manage this patient");
    expect(shown()).toEqual([
      { kind: "error", message: "You do not manage this patient" },
    ]);
    expect(screen.button(t.grant).hasAttribute("disabled")).toBe(false);
    logged.mockRestore();
  });

  it("cannot grant twice, nor revoke, while the server has not answered", async () => {
    answer("training:listLaunchRights", [right(LINA, 180)]);
    answer("users:listUsers", [PAUL, LINA]);
    let answerGrant: (value: null) => void = () => {};
    mutation(GRANT).mockImplementation(
      () => new Promise((resolve) => (answerGrant = resolve)),
    );
    const screen = card(true);
    await choose(screen, PAUL_OPTION);

    await click(screen.button(t.grant));
    expect(screen.button(t.grant).hasAttribute("disabled")).toBe(true);
    expect(screen.button(t.revoke).hasAttribute("disabled")).toBe(true);
    await click(screen.button(t.grant));

    expect(mutation(GRANT)).toHaveBeenCalledTimes(1);
    answerGrant(null);
  });
});

describe("ANH-203 launch rights: revoking", () => {
  /** The card with Paul and Lina as holders, and the window opened on Lina's right. */
  async function revoking() {
    answer("training:listLaunchRights", [right(PAUL, 172), right(LINA, null)]);
    answer("users:listUsers", [PAUL, LINA]);
    const screen = card(true);
    const revoke = () =>
      screen.all(
        (element) =>
          element.localName === "button" && screen.textOf(element) === t.revoke,
      );
    await click(revoke()[1]);
    return { screen, revoke };
  }

  it("asks for a confirmation that names the patient: the first click sends nothing", async () => {
    const { screen } = await revoking();

    expect(windowOpen(screen)).toBe(true);
    expect(screen.text()).toContain(t.revokeConfirm);
    expect(screen.text()).toContain(
      "Lina Roy ne pourra plus lancer de séance auto sur cette machine.",
    );
    expect(mutationCalls()).toEqual({});
  });

  it("revokes the right of the patient named in the window, and of no other", async () => {
    const { screen, revoke } = await revoking();

    // The last "Retirer" is the red button of the window.
    await click(revoke().at(-1)!);

    expect(mutationCalls()).toEqual({
      [REVOKE]: [[{ machineId, userId: "lina" }]],
    });
    expect(shown()).toEqual([
      { kind: "success", message: fr.feedback.launchRightRevoked },
    ]);
    expect(windowOpen(screen)).toBe(false);
  });

  it.each([
    [
      "Cancel",
      (screen: ReturnType<typeof card>) =>
        click(screen.button(fr.common.cancel)),
    ],
    [
      "the window's own close control",
      (screen: ReturnType<typeof card>) => closeWindow(screen),
    ],
  ])("revokes nothing when the window is left by %s", async (_how, leave) => {
    const { screen } = await revoking();

    await leave(screen);

    expect(windowOpen(screen)).toBe(false);
    expect(mutationCalls()).toEqual({});
  });

  it("shows the server's refusal in the card and closes the window", async () => {
    mutation(REVOKE).mockRejectedValue(new Error("Not authorized"));
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const { screen, revoke } = await revoking();

    await click(revoke().at(-1)!);

    expect(screen.text()).toContain("Not authorized");
    expect(shown()).toEqual([{ kind: "error", message: "Not authorized" }]);
    expect(windowOpen(screen)).toBe(false);
    logged.mockRestore();
  });
});

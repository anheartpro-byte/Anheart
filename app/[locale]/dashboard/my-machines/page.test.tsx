// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import { ConvexError } from "convex/values";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import fr from "@/messages/fr.json";
import {
  answer,
  feedbackShown,
  lastArgsAsked,
  mutation,
  mutationsSent,
  NOW,
  person,
  renderPage,
  router,
  signedInAs,
  takeLoggedFailures,
  type CurrentUser,
} from "@/test-support/pages";
import MyMachinesPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);

/**
 * "My machines": where a patient, a manager or an admin launches an auto
 * session. The machine cards and the launch window are the real ones, so that
 * the launch is the one the page really sends.
 */

type Machine = FunctionReturnType<
  typeof api.training.listLaunchableMachines
>[number];
type Profile = Machine["profiles"][number];

const paris = "machine-paris" as Id<"machines">;
const lyon = "machine-lyon" as Id<"machines">;
const newSession = "session-new" as Id<"sessions">;

function profile(name: string, overrides: Partial<Profile> = {}): Profile {
  return {
    profileId: `p-${name.toLowerCase()}`,
    name,
    totalDurationS: 1800,
    zoneLowBpm: 110,
    zoneHighBpm: 130,
    hardMaxBpm: 150,
    criticalBpm: 160,
    subjectHrMax: 180,
    minRunRpm: 300,
    maxRpm: 1200,
    ...overrides,
  };
}

function machine(
  id: Id<"machines">,
  name: string,
  overrides: Partial<Machine> = {},
): Machine {
  return {
    _id: id,
    name,
    location: `Salle ${name}`,
    status: "online",
    lastHeartbeat: NOW - 5_000,
    serverNow: NOW,
    programsEnabled: true,
    live: null,
    profiles: [profile("Endurance"), profile("Tonus")],
    myHrMax: 180,
    ...overrides,
  };
}

/** The card of the machine called `name`. */
function cardOf(name: string) {
  const card = screen.getByText(name).closest("[data-slot=card]");
  if (card === null) throw new Error(`No card holds "${name}"`);
  return within(card as HTMLElement);
}

const launchButton = { name: fr.training.launch.button };

/** Chooses `option` in the list opened by the field showing `current`. */
async function choose(
  user: Awaited<ReturnType<typeof renderPage>>["user"],
  current: string | RegExp,
  option: string | RegExp,
) {
  const dialog = within(screen.getByRole("dialog"));
  const field = dialog
    .getAllByRole("combobox")
    .find((box) =>
      typeof current === "string"
        ? box.textContent?.includes(current)
        : current.test(box.textContent ?? ""),
    );
  if (field === undefined) throw new Error(`No field shows ${current}`);
  await user.click(field);
  await user.click(screen.getByRole("option", { name: option }));
}

describe("my machines: what each visitor sees", () => {
  it("asks for the machines the visitor may launch on, and shows nothing until both answers are in", async () => {
    signedInAs("user");
    answer(api.training.listLaunchableMachines, undefined);
    const view = await renderPage(<MyMachinesPage />);
    expect(screen.queryByRole("heading")).toBeNull();
    expect(lastArgsAsked(api.training.listLaunchableMachines)).toEqual({});

    answer(api.training.listLaunchableMachines, [machine(paris, "Paris")]);
    answer(api.users.getCurrentUser, undefined);
    await view.refresh();
    expect(screen.queryByRole("heading")).toBeNull();

    signedInAs("user");
    await view.refresh();
    expect(
      screen.getByRole("heading", { name: fr.training.myMachines.title }),
    ).toBeTruthy();
  });

  it("shows one card per machine, with its place and its programmes", async () => {
    signedInAs("gestionnaire");
    answer(api.training.listLaunchableMachines, [
      machine(paris, "Paris"),
      machine(lyon, "Lyon", { profiles: [profile("Tonus")] }),
    ]);
    await renderPage(<MyMachinesPage />);

    expect(cardOf("Paris").getByText("Salle Paris")).toBeTruthy();
    expect(cardOf("Paris").getByText("· 2 programmes")).toBeTruthy();
    expect(cardOf("Paris").getByText("Endurance")).toBeTruthy();
    expect(cardOf("Lyon").getByText("· 1 programme")).toBeTruthy();
    expect(cardOf("Lyon").queryByText("Endurance")).toBeNull();
  });

  it("tells a patient their max heart rate", async () => {
    signedInAs("user");
    answer(api.training.listLaunchableMachines, [machine(paris, "Paris")]);
    await renderPage(<MyMachinesPage />);

    expect(screen.getByText("Votre FC max : 180 bpm")).toBeTruthy();
    expect(
      screen.queryByText(fr.training.myMachines.myHrMaxMissing),
    ).toBeNull();
  });

  it("warns a patient whose max heart rate is not set, before any launch", async () => {
    signedInAs("user");
    answer(api.training.listLaunchableMachines, [
      machine(paris, "Paris", { myHrMax: null }),
    ]);
    await renderPage(<MyMachinesPage />);

    expect(
      screen.getByText(fr.training.myMachines.myHrMaxMissing),
    ).toBeTruthy();
    expect(screen.getByText(fr.training.physiology.notSet)).toBeTruthy();
    expect(screen.queryByText(/Votre FC max :/)).toBeNull();
  });

  it.each(["gestionnaire", "admin", "org_admin"] as const)(
    "shows a %s neither their max heart rate nor its absence",
    async (role) => {
      signedInAs(role);
      answer(api.training.listLaunchableMachines, [
        machine(paris, "Paris", { myHrMax: null }),
      ]);
      await renderPage(<MyMachinesPage />);

      expect(screen.queryByText(/Votre FC max/)).toBeNull();
      expect(screen.queryByText(fr.training.physiology.notSet)).toBeNull();
    },
  );

  it.each(["gestionnaire", "admin", "org_admin"] as const)(
    "gives a %s the way to the page of each machine",
    async (role) => {
      signedInAs(role);
      answer(api.training.listLaunchableMachines, [machine(lyon, "Lyon")]);
      await renderPage(<MyMachinesPage />);

      expect(
        cardOf("Lyon")
          .getByText(fr.training.myMachines.details)
          .closest("a")
          ?.getAttribute("href"),
      ).toBe(`/dashboard/machines/${lyon}`);
    },
  );

  it("gives a patient no way to the page of a machine", async () => {
    signedInAs("user");
    answer(api.training.listLaunchableMachines, [machine(lyon, "Lyon")]);
    await renderPage(<MyMachinesPage />);

    expect(
      cardOf("Lyon").queryByText(fr.training.myMachines.details),
    ).toBeNull();
    expect(cardOf("Lyon").queryByRole("link")).toBeNull();
  });

  it("tells a patient without any machine that a manager must grant the right", async () => {
    signedInAs("user");
    answer(api.training.listLaunchableMachines, []);
    await renderPage(<MyMachinesPage />);

    expect(screen.getByText(fr.training.myMachines.noRights)).toBeTruthy();
    expect(screen.queryByText(fr.training.myMachines.noMachines)).toBeNull();
    // Nothing is said of a max heart rate nobody can use yet.
    expect(screen.queryByText(/Votre FC max/)).toBeNull();
    expect(
      screen.queryByText(fr.training.myMachines.myHrMaxMissing),
    ).toBeNull();
    expect(screen.queryByRole("button", launchButton)).toBeNull();
  });

  it.each(["gestionnaire", "admin", "org_admin"] as const)(
    "tells a %s without any machine that none is available",
    async (role) => {
      signedInAs(role);
      answer(api.training.listLaunchableMachines, []);
      await renderPage(<MyMachinesPage />);

      expect(screen.getByText(fr.training.myMachines.noMachines)).toBeTruthy();
      expect(screen.queryByText(fr.training.myMachines.noRights)).toBeNull();
    },
  );

  // What the page does today for an account Convex has no row for yet (the
  // row is created from the landing page): the manager's wording.
  it("shows an account without a row the manager's empty list", async () => {
    answer(api.users.getCurrentUser, null);
    answer(api.training.listLaunchableMachines, []);
    await renderPage(<MyMachinesPage />);

    expect(screen.getByText(fr.training.myMachines.noMachines)).toBeTruthy();
  });
});

describe("my machines: what cannot be launched", () => {
  it.each([
    [
      "offline",
      { status: "offline", lastHeartbeat: NOW - 600_000 },
      fr.training.launch.offline,
    ],
    [
      "silent for more than 90 s, whatever status the server still holds",
      { status: "online", lastHeartbeat: NOW - 91_000 },
      fr.training.launch.offline,
    ],
    [
      "already in a session",
      { status: "in_session" },
      fr.training.launch.inSession,
    ],
  ] as const)(
    "does not let a session be launched on a machine that is %s, and says why",
    async (_name, state, reason) => {
      signedInAs("gestionnaire");
      answer(api.training.listLaunchableMachines, [
        machine(paris, "Paris", state),
      ]);
      const { user } = await renderPage(<MyMachinesPage />);
      const button = cardOf("Paris").getByRole("button", launchButton);

      expect(button.hasAttribute("disabled")).toBe(true);
      expect(cardOf("Paris").getByText(reason)).toBeTruthy();
      await user.click(button);
      expect(screen.queryByRole("dialog")).toBeNull();
    },
  );

  it.each<[string, Partial<Machine>]>([
    ["whose programmes are disabled", { programsEnabled: false }],
    ["without any programme", { profiles: [] }],
  ])(
    "does not let a session be launched on a machine %s",
    async (_name, state) => {
      signedInAs("user");
      answer(api.training.listLaunchableMachines, [
        machine(paris, "Paris", state),
      ]);
      await renderPage(<MyMachinesPage />);

      expect(
        cardOf("Paris")
          .getByRole("button", launchButton)
          .hasAttribute("disabled"),
      ).toBe(true);
    },
  );
});

describe("my machines: launching a session", () => {
  function twoMachines(me: CurrentUser["role"]) {
    const user = signedInAs(me);
    answer(api.training.listLaunchableMachines, [
      machine(paris, "Paris", { profiles: [profile("Endurance")] }),
      machine(lyon, "Lyon", { profiles: [profile("Tonus")] }),
    ]);
    mutation(api.training.launchAutoSession).mockResolvedValue(newSession);
    return user;
  }

  it("a patient launches a programme of the machine they chose, for themselves, and is taken to the live view", async () => {
    twoMachines("user");
    const { user } = await renderPage(<MyMachinesPage />);

    await user.click(cardOf("Lyon").getByRole("button", launchButton));
    const dialog = within(screen.getByRole("dialog"));
    // A patient rides for themselves: there is no rider to choose.
    expect(
      dialog.getByDisplayValue("Paul Patient").hasAttribute("disabled"),
    ).toBe(true);
    expect(
      dialog
        .getByRole("button", { name: fr.training.launch.submit })
        .hasAttribute("disabled"),
    ).toBe(true);

    await choose(user, fr.training.launch.selectProgram, /Tonus/);
    await user.click(
      dialog.getByRole("button", { name: fr.training.launch.submit }),
    );

    expect(mutationsSent()).toEqual([
      {
        name: "training:launchAutoSession",
        args: {
          machineId: lyon,
          profileId: "p-tonus",
          userId: undefined,
          totalDurationS: undefined,
          notes: undefined,
        },
      },
    ]);
    expect(feedbackShown()).toEqual([
      { kind: "success", message: fr.feedback.sessionLaunched },
    ]);
    expect(router.push.mock.calls).toEqual([
      [`/dashboard/sessions/${newSession}/live`],
    ]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("offers only the programmes of the machine whose button was pressed", async () => {
    twoMachines("user");
    const { user } = await renderPage(<MyMachinesPage />);

    await user.click(cardOf("Paris").getByRole("button", launchButton));
    await user.click(within(screen.getByRole("dialog")).getByRole("combobox"));

    expect(
      screen.getAllByRole("option").map((option) => option.textContent),
    ).toEqual(["Endurance · 110-130 bpm · 30 min"]);
  });

  it("a manager launches for one of their patients, with a length and notes", async () => {
    twoMachines("gestionnaire");
    const patient = "user-patient-7" as Id<"users">;
    answer(api.users.getPatientsForGestionnaire, [
      {
        _id: patient,
        firstName: "Rose",
        lastName: "Rider",
        email: "rose@example.test",
        language: "fr",
        createdAt: NOW,
      },
    ]);
    answer(api.training.listLaunchRights, []);
    answer(api.users.getUserById, person({ _id: patient }));
    const { user } = await renderPage(<MyMachinesPage />);

    await user.click(cardOf("Paris").getByRole("button", launchButton));
    const dialog = within(screen.getByRole("dialog"));
    await choose(user, fr.training.launch.selectProgram, /Endurance/);
    await choose(user, fr.training.launch.myself, /Rose Rider/);
    await user.type(dialog.getByLabelText(fr.training.launch.duration), "20");
    await user.type(
      dialog.getByLabelText(fr.training.launch.notes),
      "  Reprise après blessure  ",
    );
    await user.click(
      dialog.getByRole("button", { name: fr.training.launch.submit }),
    );

    expect(mutationsSent()).toEqual([
      {
        name: "training:launchAutoSession",
        args: {
          machineId: paris,
          profileId: "p-endurance",
          userId: patient,
          totalDurationS: 1200,
          notes: "Reprise après blessure",
        },
      },
    ]);
    expect(router.push.mock.calls).toEqual([
      [`/dashboard/sessions/${newSession}/live`],
    ]);
  });

  it("shows the server's refusal in the window, which stays open, and goes nowhere", async () => {
    twoMachines("user");
    // The sentence of the server when the machine was taken meanwhile.
    mutation(api.training.launchAutoSession).mockRejectedValue(
      new ConvexError("Machine is already in a session"),
    );
    const { user } = await renderPage(<MyMachinesPage />);

    await user.click(cardOf("Paris").getByRole("button", launchButton));
    const dialog = within(screen.getByRole("dialog"));
    await choose(user, fr.training.launch.selectProgram, /Endurance/);
    await user.click(
      dialog.getByRole("button", { name: fr.training.launch.submit }),
    );

    const refusal = "Machine is already in a session";
    // Read again from the screen: the window is still there.
    const stillOpen = within(screen.getByRole("dialog"));
    expect(stillOpen.getByText(refusal)).toBeTruthy();
    expect(feedbackShown()).toEqual([{ kind: "error", message: refusal }]);
    expect(takeLoggedFailures()).toEqual(["training:launchAutoSession"]);
    expect(router.push).not.toHaveBeenCalled();
    // The launch can be tried again.
    expect(
      stillOpen
        .getByRole("button", { name: fr.training.launch.submit })
        .hasAttribute("disabled"),
    ).toBe(false);
  });

  it("refuses to launch for a patient whose max heart rate is not set", async () => {
    signedInAs("user");
    answer(api.training.listLaunchableMachines, [
      machine(paris, "Paris", { myHrMax: null }),
    ]);
    const { user } = await renderPage(<MyMachinesPage />);

    await user.click(cardOf("Paris").getByRole("button", launchButton));
    const dialog = within(screen.getByRole("dialog"));
    await choose(user, fr.training.launch.selectProgram, /Endurance/);

    expect(dialog.getByText(fr.training.launch.hrMaxMissing)).toBeTruthy();
    expect(
      dialog
        .getByRole("button", { name: fr.training.launch.submit })
        .hasAttribute("disabled"),
    ).toBe(true);
    expect(mutationsSent()).toEqual([]);
  });

  it("sends nothing when the window is closed, and opens it afresh for another machine", async () => {
    twoMachines("user");
    const { user } = await renderPage(<MyMachinesPage />);

    await user.click(cardOf("Paris").getByRole("button", launchButton));
    await choose(user, fr.training.launch.selectProgram, /Endurance/);
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: fr.common.cancel,
      }),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mutationsSent()).toEqual([]);

    await user.click(cardOf("Lyon").getByRole("button", launchButton));
    const dialog = within(screen.getByRole("dialog"));
    // Nothing is left of the programme chosen for the other machine.
    expect(dialog.getByRole("combobox").textContent).toBe(
      fr.training.launch.selectProgram,
    );
    await user.click(dialog.getByRole("combobox"));
    expect(
      screen.getAllByRole("option").map((option) => option.textContent),
    ).toEqual(["Tonus · 110-130 bpm · 30 min"]);
  });
});

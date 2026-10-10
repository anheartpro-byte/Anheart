// @vitest-environment jsdom
import { act, fireEvent, screen, within } from "@testing-library/react";
import { format, formatDistance } from "date-fns";
import { enUS, fr as frLocale } from "date-fns/locale";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";
import {
  answer,
  feedbackShown,
  lastArgsAsked,
  mutation,
  mutationsSent,
  NOW,
  propsOf,
  renderPage,
  routeParams,
  router,
  serverFailures,
  signedInAs,
  takeLoggedFailures,
  wasMounted,
  type CurrentUser,
} from "@/test-support/pages";
import MachineDetailPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);
// The cards below ask their own queries and send their own mutations: the
// page only mounts them, for the right machine and the right visitor.
vi.mock("@/components/training/MachineLiveCard", async () => ({
  MachineLiveCard: (await import("@/test-support/pages")).standIn(
    "MachineLiveCard",
  ),
}));
vi.mock("@/components/training/ProfileList", async (original) => ({
  ...(await original<typeof import("@/components/training/ProfileList")>()),
  MachineProgramsCard: (await import("@/test-support/pages")).standIn(
    "MachineProgramsCard",
  ),
}));
vi.mock("@/components/training/LaunchRightsCard", async () => ({
  LaunchRightsCard: (await import("@/test-support/pages")).standIn(
    "LaunchRightsCard",
  ),
}));
vi.mock("@/components/modals/MachineFormModal", async () => ({
  MachineFormModal: (await import("@/test-support/pages")).standIn(
    "MachineFormModal",
  ),
}));

/**
 * The page of one machine: its state, who manages it, and what a manager does
 * to it (launch a session, edit, renew its key, delete, restore). The launch
 * window is the real one.
 */

type Machine = NonNullable<FunctionReturnType<typeof api.machines.getMachine>>;
type EditWindow = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  machine?: Machine;
  onSuccess?: () => void;
};

const machineId = "machine-1" as Id<"machines">;
const CREATED = NOW - 90 * 86_400_000;

function machine(overrides: Partial<Machine> = {}): Machine {
  return {
    _id: machineId,
    _creationTime: CREATED,
    name: "Centri Paris",
    status: "online",
    lastHeartbeat: NOW - 5_000,
    location: "Salle 101",
    createdAt: CREATED,
    isDeleted: undefined,
    deletedAt: undefined,
    softwareVersion: "pi-1.4.0",
    contractVersion: "2",
    lastVersionSeenAt: NOW - 120_000,
    serverNow: NOW,
    gestionnaires: [
      {
        _id: "user-gestionnaire" as Id<"users">,
        firstName: "Gaston",
        lastName: "Gestion",
        isOwner: true,
      },
      {
        _id: "user-g2" as Id<"users">,
        firstName: "Gisèle",
        lastName: "Second",
        isOwner: false,
      },
    ],
    ...overrides,
  };
}

const deleted = (overrides: Partial<Machine> = {}) =>
  machine({ isDeleted: true, deletedAt: NOW - 86_400_000, ...overrides });

/** Opens the page as `role`; `shown` is what the server answers for the machine. */
async function open(
  role: CurrentUser["role"] | null,
  shown: Machine | null | "loading" = machine(),
  locale: "fr" | "en" = "fr",
) {
  if (role === null) answer(api.users.getCurrentUser, null);
  else signedInAs(role);
  answer(api.machines.getMachine, shown === "loading" ? undefined : shown);
  return renderPage(
    <MachineDetailPage params={routeParams({ id: machineId })} />,
    { locale },
  );
}

/** The value shown under a label of the configuration card. */
function valueUnder(label: string): string | null {
  const value = screen.getByText(label).nextElementSibling;
  return value === null ? null : value.textContent;
}

const launchButton = { name: fr.training.launch.button };
const editButton = { name: fr.common.edit };
const regenerateButton = { name: fr.machines.regenerate };
const deleteButton = { name: fr.common.delete };
const restoreButton = { name: fr.machines.restore };

describe("machine page: which machine it shows", () => {
  it("asks for the machine of the address and shows nothing while it loads", async () => {
    await open("admin", "loading");

    expect(lastArgsAsked(api.machines.getMachine)).toEqual({ machineId });
    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.queryByText(fr.machines.notFound)).toBeNull();
    expect(wasMounted("MachineLiveCard")).toBe(false);
  });

  it("says the machine is not found, with the way back, when the server answers none", async () => {
    // What the server answers for a machine that does not exist, and for one
    // of another organisation or that the visitor does not manage.
    await open("gestionnaire", null);

    expect(screen.getByText(fr.machines.notFound)).toBeTruthy();
    expect(
      screen.getByText(fr.common.back).closest("a")?.getAttribute("href"),
    ).toBe("/dashboard/machines");
    expect(screen.queryByRole("button")).toBeNull();
    expect(wasMounted("MachineLiveCard")).toBe(false);
    expect(wasMounted("LaunchRightsCard")).toBe(false);
  });

  it("shows its name, place, status, last signal, creation date and versions", async () => {
    await open("admin");

    expect(
      screen.getByRole("heading", { level: 1, name: "Centri Paris" }),
    ).toBeTruthy();
    expect(screen.getByText("Salle 101")).toBeTruthy();
    expect(valueUnder(fr.machines.status)).toBe(fr.machines.online);
    expect(valueUnder(fr.machines.lastHeartbeat)).toBe(
      formatDistance(NOW - 5_000, NOW, { addSuffix: true, locale: frLocale }),
    );
    expect(valueUnder(fr.machines.createdAt)).toBe(
      format(CREATED, "PPP", { locale: frLocale }),
    );
    expect(valueUnder(fr.machines.softwareVersion)).toBe("pi-1.4.0");
    expect(valueUnder(fr.machines.contractVersion)).toBe("2");
    expect(
      screen.getByText("Versions annoncées il y a 2 minutes"),
    ).toBeTruthy();
  });

  it("dates in English for an English visitor", async () => {
    await open("admin", deleted(), "en");

    expect(valueUnder(en.machines.createdAt)).toBe(
      format(CREATED, "PPP", { locale: enUS }),
    );
    expect(
      within(screen.getByRole("alert")).getByText(
        `${en.machines.deletedAt}: ${format(NOW - 86_400_000, "PPpp", { locale: enUS })}`,
      ),
    ).toBeTruthy();
  });

  it("shows a dash for what a machine never reported", async () => {
    await open(
      "admin",
      machine({
        location: undefined,
        lastHeartbeat: 0,
        status: "offline",
        softwareVersion: undefined,
        contractVersion: undefined,
        lastVersionSeenAt: undefined,
      }),
    );

    expect(
      screen.getByRole("heading", { level: 1 }).nextElementSibling?.textContent,
    ).toBe("-");
    expect(valueUnder(fr.machines.status)).toBe(fr.machines.offline);
    expect(valueUnder(fr.machines.lastHeartbeat)).toBe("-");
    expect(valueUnder(fr.machines.softwareVersion)).toBe("-");
    expect(valueUnder(fr.machines.contractVersion)).toBe("-");
    expect(screen.queryByText(/Versions annoncées/)).toBeNull();
  });

  it("lists who manages the machine and marks its owner", async () => {
    await open("admin");

    const card = within(
      screen
        .getByText(fr.machines.gestionnaires)
        .closest("[data-slot=card]") as HTMLElement,
    );
    const gaston = card.getByText("Gaston Gestion").closest("div")
      ?.parentElement as HTMLElement;
    const gisele = card.getByText("Gisèle Second").closest("div")
      ?.parentElement as HTMLElement;
    expect(within(gaston).getByText(fr.machines.owner)).toBeTruthy();
    expect(within(gisele).queryByText(fr.machines.owner)).toBeNull();
  });

  it("shows no card of managers for a machine nobody manages", async () => {
    await open("admin", machine({ gestionnaires: [] }));

    expect(screen.queryByText(fr.machines.gestionnaires)).toBeNull();
  });

  it("mounts the live state and the programmes of this machine", async () => {
    await open("user");

    expect(propsOf("MachineLiveCard")).toEqual({ machineId });
    expect(propsOf("MachineProgramsCard")).toEqual({ machineId });
  });
});

describe("machine page: what each role may do", () => {
  it("lets an admin launch, edit, renew the key, delete, and grant rights as an admin", async () => {
    await open("admin");

    expect(screen.getByRole("button", launchButton)).toBeTruthy();
    expect(screen.getByRole("button", editButton)).toBeTruthy();
    expect(screen.getByRole("button", regenerateButton)).toBeTruthy();
    expect(screen.getByRole("button", deleteButton)).toBeTruthy();
    expect(propsOf("LaunchRightsCard")).toEqual({ machineId, isAdmin: true });
    expect(screen.queryByRole("button", restoreButton)).toBeNull();
  });

  it("lets a manager do the same, with the rights of a manager only", async () => {
    await open("gestionnaire");

    expect(screen.getByRole("button", launchButton)).toBeTruthy();
    expect(screen.getByRole("button", editButton)).toBeTruthy();
    expect(screen.getByRole("button", regenerateButton)).toBeTruthy();
    expect(screen.getByRole("button", deleteButton)).toBeTruthy();
    expect(propsOf("LaunchRightsCard")).toEqual({ machineId, isAdmin: false });
  });

  it.each([
    ["a patient", "user"],
    // What the page does today: the admin of a client organisation, whom the
    // server lets manage the machines of that organisation, gets none of the
    // controls here.
    ["the admin of a client organisation", "org_admin"],
    ["an account without a row", null],
  ] as const)(
    "shows %s the machine and nothing to act on it",
    async (_name, role) => {
      await open(role);

      expect(
        screen.getByRole("heading", { level: 1, name: "Centri Paris" }),
      ).toBeTruthy();
      expect(screen.queryByRole("button")).toBeNull();
      expect(screen.queryByText(fr.machines.dangerZone)).toBeNull();
      expect(wasMounted("LaunchRightsCard")).toBe(false);
      expect(wasMounted("MachineLiveCard")).toBe(true);
    },
  );
});

describe("machine page: a deleted machine", () => {
  it("says when it was deleted and leaves nothing to act on it but its restoration", async () => {
    await open("admin", deleted());

    const alert = within(screen.getByRole("alert"));
    expect(alert.getByText(fr.machines.deleted)).toBeTruthy();
    expect(
      alert.getByText(
        `${fr.machines.deletedAt}: ${format(NOW - 86_400_000, "PPpp", { locale: frLocale })}`,
      ),
    ).toBeTruthy();
    expect(
      screen.getAllByRole("button").map((button) => button.textContent),
    ).toEqual([fr.machines.restore]);
    expect(screen.queryByText(fr.machines.dangerZone)).toBeNull();
    expect(wasMounted("MachineLiveCard")).toBe(false);
    expect(wasMounted("MachineProgramsCard")).toBe(false);
    expect(wasMounted("LaunchRightsCard")).toBe(false);
  });

  it("shows a dash when the date of the deletion is unknown", async () => {
    await open("admin", deleted({ deletedAt: undefined }));

    expect(
      within(screen.getByRole("alert")).getByText(
        `${fr.machines.deletedAt}: -`,
      ),
    ).toBeTruthy();
  });

  it("does not offer a manager to restore it", async () => {
    await open("gestionnaire", deleted());

    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("restores it once an admin confirms, and closes the window", async () => {
    const { user } = await open("admin", deleted());

    await user.click(screen.getByRole("button", restoreButton));
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText(fr.machines.restoreConfirm)).toBeTruthy();
    expect(mutationsSent()).toEqual([]);
    await user.click(dialog.getByRole("button", restoreButton));

    expect(mutationsSent()).toEqual([
      { name: "machines:restoreMachine", args: { machineId } },
    ]);
    expect(feedbackShown()).toEqual([
      { kind: "success", message: fr.feedback.machineRestored },
    ]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("cannot be asked twice while the server has not answered", async () => {
    // The server answers only when the test says so.
    const server = Promise.withResolvers<null>();
    mutation(api.machines.restoreMachine).mockReturnValue(server.promise);
    const { user } = await open("admin", deleted());

    await user.click(screen.getByRole("button", restoreButton));
    const dialog = within(screen.getByRole("dialog"));
    await user.click(dialog.getByRole("button", restoreButton));

    const waiting = dialog.getByRole("button", { name: fr.common.loading });
    expect(waiting.hasAttribute("disabled")).toBe(true);
    await user.click(waiting);
    expect(mutationsSent()).toHaveLength(1);

    await act(async () => server.resolve(null));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it.each(
    serverFailures(api.machines.restoreMachine, "Machine is not deleted"),
  )(
    "keeps the window open and shows the failure when the restoration fails $where",
    async ({ error, shown }) => {
      mutation(api.machines.restoreMachine).mockRejectedValue(error);
      const { user } = await open("admin", deleted());

      await user.click(screen.getByRole("button", restoreButton));
      await user.click(
        within(screen.getByRole("dialog")).getByRole("button", restoreButton),
      );

      expect(feedbackShown()).toEqual([{ kind: "error", message: shown }]);
      expect(takeLoggedFailures()).toEqual(["machines:restoreMachine"]);
      // Read again from the screen: the window is still there, on the same
      // question, and the restoration can be asked again.
      const stillOpen = within(screen.getByRole("dialog"));
      expect(stillOpen.getByText(fr.machines.restoreConfirm)).toBeTruthy();
      expect(
        stillOpen.getByRole("button", restoreButton).hasAttribute("disabled"),
      ).toBe(false);
    },
  );

  it("sends nothing when the restoration is declined", async () => {
    const { user } = await open("admin", deleted());

    await user.click(screen.getByRole("button", restoreButton));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: fr.common.cancel,
      }),
    );

    expect(mutationsSent()).toEqual([]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("machine page: deleting the machine", () => {
  it("deletes it once confirmed, says so and goes back to the list", async () => {
    const { user } = await open("gestionnaire");

    await user.click(screen.getByRole("button", deleteButton));
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText(fr.machines.deleteConfirm)).toBeTruthy();
    expect(mutationsSent()).toEqual([]);
    await user.click(dialog.getByRole("button", deleteButton));

    expect(mutationsSent()).toEqual([
      { name: "machines:deleteMachine", args: { machineId } },
    ]);
    expect(feedbackShown()).toEqual([
      { kind: "success", message: fr.machines.deleteSuccess },
    ]);
    expect(router.push.mock.calls).toEqual([["/dashboard/machines"]]);
  });

  it("sends nothing when the deletion is declined", async () => {
    const { user } = await open("admin");

    await user.click(screen.getByRole("button", deleteButton));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: fr.common.cancel,
      }),
    );

    expect(mutationsSent()).toEqual([]);
    expect(router.push).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it.each(
    serverFailures(
      api.machines.deleteMachine,
      "Cannot delete machine with active session",
    ),
  )(
    "stays on the page, on its question, and shows the failure when the deletion fails $where",
    async ({ error, shown }) => {
      mutation(api.machines.deleteMachine).mockRejectedValue(error);
      const { user } = await open("admin");

      await user.click(screen.getByRole("button", deleteButton));
      await user.click(
        within(screen.getByRole("dialog")).getByRole("button", deleteButton),
      );

      expect(feedbackShown()).toEqual([{ kind: "error", message: shown }]);
      expect(takeLoggedFailures()).toEqual(["machines:deleteMachine"]);
      expect(router.push).not.toHaveBeenCalled();
      expect(
        within(screen.getByRole("dialog")).getByText(fr.machines.deleteConfirm),
      ).toBeTruthy();
    },
  );

  it("does not offer to delete a machine that is in a session", async () => {
    const { user } = await open("admin", machine({ status: "in_session" }));
    const button = screen.getByRole("button", deleteButton);

    expect(button.hasAttribute("disabled")).toBe(true);
    await user.click(button);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("machine page: renewing the key of the machine", () => {
  // Made up, and plain enough for the secret scan of the repository not to
  // take it for a real key.
  const KEY = "cle-aaaa-bbbb-cccc";

  async function renew(user: Awaited<ReturnType<typeof open>>["user"]) {
    await user.click(screen.getByRole("button", regenerateButton));
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText(fr.machines.regenerateKeyConfirmDesc)).toBeTruthy();
    expect(mutationsSent()).toEqual([]);
    await user.click(dialog.getByRole("button", { name: fr.common.confirm }));
  }

  it("asks for a new key once confirmed and shows it, with the warning that it is shown once", async () => {
    mutation(api.machines.regenerateApiKey).mockResolvedValue({ apiKey: KEY });
    const { user } = await open("gestionnaire");

    await renew(user);

    expect(mutationsSent()).toEqual([
      { name: "machines:regenerateApiKey", args: { machineId } },
    ]);
    expect(feedbackShown()).toEqual([
      { kind: "success", message: fr.feedback.apiKeyRegenerated },
    ]);
    const shown = within(screen.getByRole("dialog"));
    expect(shown.getByText(KEY)).toBeTruthy();
    expect(shown.getByText(fr.machines.apiKeyWarning)).toBeTruthy();
  });

  it("copies the key and says so for two seconds", async () => {
    mutation(api.machines.regenerateApiKey).mockResolvedValue({ apiKey: KEY });
    const { user } = await open("admin");
    await renew(user);
    const copied = vi.spyOn(navigator.clipboard, "writeText");
    const shown = within(screen.getByRole("dialog"));

    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    fireEvent.click(shown.getByRole("button", { name: fr.common.copy }));
    await act(async () => {});

    expect(copied.mock.calls).toEqual([[KEY]]);
    expect(shown.getByRole("button", { name: fr.common.copied })).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(1_999);
    });
    expect(shown.getByRole("button", { name: fr.common.copied })).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(shown.getByRole("button", { name: fr.common.copy })).toBeTruthy();
  });

  it("no longer shows the key once its window is closed", async () => {
    mutation(api.machines.regenerateApiKey).mockResolvedValue({ apiKey: KEY });
    const { user } = await open("admin");
    await renew(user);

    await user.click(
      within(screen.getByRole("dialog")).getByText(fr.common.close),
    );

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText(KEY)).toBeNull();
  });

  it("no longer shows the key once its window is dismissed from the keyboard", async () => {
    mutation(api.machines.regenerateApiKey).mockResolvedValue({ apiKey: KEY });
    const { user } = await open("admin");
    await renew(user);
    expect(screen.getByText(KEY)).toBeTruthy();

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText(KEY)).toBeNull();
  });

  it.each(
    serverFailures(
      api.machines.regenerateApiKey,
      "Not authorized to manage this machine",
    ),
  )(
    "shows no key, the failure, and its question still open when the renewal fails $where",
    async ({ error, shown }) => {
      mutation(api.machines.regenerateApiKey).mockRejectedValue(error);
      const { user } = await open("gestionnaire");

      await renew(user);

      expect(feedbackShown()).toEqual([{ kind: "error", message: shown }]);
      expect(takeLoggedFailures()).toEqual(["machines:regenerateApiKey"]);
      expect(screen.queryByText(fr.machines.apiKeyWarning)).toBeNull();
      // The confirmation did not close on the refusal: it can be tried again.
      const stillOpen = within(screen.getByRole("dialog"));
      expect(
        stillOpen.getByText(fr.machines.regenerateKeyConfirmDesc),
      ).toBeTruthy();
      expect(
        stillOpen.getByRole("button", { name: fr.common.confirm }),
      ).toBeTruthy();
    },
  );

  it("sends nothing when the renewal is declined", async () => {
    const { user } = await open("admin");

    await user.click(screen.getByRole("button", regenerateButton));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: fr.common.cancel,
      }),
    );

    expect(mutationsSent()).toEqual([]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("machine page: editing the machine", () => {
  it("opens the edit window on this machine, closed until asked for", async () => {
    const shown = machine();
    const { user } = await open("gestionnaire", shown);
    expect(propsOf<EditWindow>("MachineFormModal").open).toBe(false);

    await user.click(screen.getByRole("button", editButton));

    expect(propsOf<EditWindow>("MachineFormModal").open).toBe(true);
    expect(propsOf<EditWindow>("MachineFormModal").machine).toEqual(shown);
  });

  it("confirms on the page that the machine was saved, until it is edited again", async () => {
    const { user } = await open("admin");
    expect(screen.queryByRole("status")).toBeNull();

    await user.click(screen.getByRole("button", editButton));
    act(() => {
      propsOf<EditWindow>("MachineFormModal").onSuccess?.();
      propsOf<EditWindow>("MachineFormModal").onOpenChange(false);
    });

    expect(screen.getByRole("status").textContent).toBe(
      fr.machines.updateSuccess,
    );
    expect(propsOf<EditWindow>("MachineFormModal").open).toBe(false);

    await user.click(screen.getByRole("button", editButton));
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("machine page: launching a session on the machine", () => {
  const newSession = "session-new" as Id<"sessions">;

  it("launches the chosen programme on this machine and goes to the live view", async () => {
    answer(api.training.listLaunchableMachines, [
      {
        _id: machineId,
        name: "Centri Paris",
        location: "Salle 101",
        status: "online",
        lastHeartbeat: NOW - 5_000,
        serverNow: NOW,
        programsEnabled: true,
        live: null,
        profiles: [
          {
            profileId: "p-endurance",
            name: "Endurance",
            totalDurationS: 1800,
            zoneLowBpm: 110,
            zoneHighBpm: 130,
            hardMaxBpm: 150,
            criticalBpm: 160,
            subjectHrMax: 180,
            minRunRpm: 300,
            maxRpm: 1200,
          },
        ],
        myHrMax: 180,
      },
    ]);
    answer(api.users.getPatientsForGestionnaire, []);
    answer(api.training.listLaunchRights, []);
    mutation(api.training.launchAutoSession).mockResolvedValue(newSession);
    const { user } = await open("gestionnaire");
    expect(screen.queryByRole("dialog")).toBeNull();

    await user.click(screen.getByRole("button", launchButton));
    const dialog = within(screen.getByRole("dialog"));
    await user.click(dialog.getAllByRole("combobox")[0]);
    await user.click(screen.getByRole("option", { name: /Endurance/ }));
    await user.click(
      dialog.getByRole("button", { name: fr.training.launch.submit }),
    );

    expect(mutationsSent()).toEqual([
      {
        name: "training:launchAutoSession",
        args: {
          machineId,
          profileId: "p-endurance",
          userId: undefined,
          totalDurationS: undefined,
          notes: undefined,
        },
      },
    ]);
    expect(router.push.mock.calls).toEqual([
      [`/dashboard/sessions/${newSession}/live`],
    ]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

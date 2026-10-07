// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import { format } from "date-fns";
import { enUS, fr as frLocale } from "date-fns/locale";
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
  propsOf,
  renderPage,
  routeParams,
  router,
  serverFailures,
  signedInAs,
  takeLoggedFailures,
  wasMounted,
  type CurrentUser,
  type Person,
} from "@/test-support/pages";
import PatientDetailPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);
// The edit window and the physiology card send their own mutations: the page
// only mounts them, on the right patient and for the right visitor.
vi.mock("@/components/modals/PatientFormModal", async () => ({
  PatientFormModal: (await import("@/test-support/pages")).standIn(
    "PatientFormModal",
  ),
}));
vi.mock("@/components/training/PhysiologyCard", async () => ({
  PhysiologyCard: (await import("@/test-support/pages")).standIn(
    "PhysiologyCard",
  ),
}));

/** The page of one patient: who they are, their sessions, and what a manager does to the account. */

type Listed = FunctionReturnType<typeof api.sessions.listSessions>[number];
type EditWindow = { open: boolean; patient?: Person };

const patientId = "user-rose" as Id<"users">;
const rose = person({ _id: patientId });

function listed(name: string, overrides: Partial<Listed> = {}): Listed {
  return {
    _id: `session-${name}` as Id<"sessions">,
    status: "completed",
    startedAt: NOW - 7_200_000,
    endedAt: NOW - 7_200_000 + 1_800_000,
    channels: [],
    patientName: "Rose Rider",
    machineName: `Machine ${name}`,
    kind: "auto",
    origin: "remote",
    ...overrides,
  };
}

/** Opens the page as `role`; `shown` is what the server answers for the patient. */
async function open(
  role: CurrentUser["role"] | null,
  shown: Person | null | "loading" = rose,
  locale: "fr" | "en" = "fr",
) {
  if (role === null) answer(api.users.getCurrentUser, null);
  else signedInAs(role);
  answer(api.users.getUserById, shown === "loading" ? undefined : shown);
  return renderPage(
    <PatientDetailPage params={routeParams({ id: patientId })} />,
    { locale },
  );
}

/** The row of the sessions table that names `text`. */
function rowOf(text: string) {
  const row = screen.getByText(text).closest("tr");
  if (row === null) throw new Error(`No row holds "${text}"`);
  return within(row);
}

const editButton = { name: fr.common.edit };
const deleteButton = { name: fr.common.delete };

describe("patient page: which patient it shows", () => {
  it("asks for the patient of the address and for their last ten sessions", async () => {
    answer(api.sessions.listSessions, []);
    await open("gestionnaire");

    expect(lastArgsAsked(api.users.getUserById)).toEqual({ userId: patientId });
    expect(lastArgsAsked(api.sessions.listSessions)).toEqual({
      userId: patientId,
      limit: 10,
    });
  });

  it("shows nothing until it has the patient and knows who the visitor is", async () => {
    const view = await open("gestionnaire", "loading");
    expect(screen.queryByRole("heading")).toBeNull();

    answer(api.users.getUserById, rose);
    answer(api.users.getCurrentUser, undefined);
    await view.refresh();
    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.queryByText("rose@example.test")).toBeNull();
  });

  it("says no account is found, with the way back, when the server answers none", async () => {
    // What the server answers for an account that does not exist, and for one
    // the visitor may not read (another organisation, another manager).
    await open("gestionnaire", null);

    expect(screen.getByText(fr.users.noUsers)).toBeTruthy();
    expect(
      screen.getByText(fr.common.back).closest("a")?.getAttribute("href"),
    ).toBe("/dashboard/patients");
    expect(screen.queryByRole("button")).toBeNull();
    expect(wasMounted("PhysiologyCard")).toBe(false);
  });

  it("shows who the patient is", async () => {
    answer(api.sessions.listSessions, []);
    await open("gestionnaire");

    expect(
      screen.getByRole("heading", { level: 1, name: "Rose Rider" }),
    ).toBeTruthy();
    const identity = within(
      screen
        .getByText(fr.reports.patientInfo)
        .closest("[data-slot=card]") as HTMLElement,
    );
    expect(identity.getByText("Rose Rider")).toBeTruthy();
    expect(identity.getByText(fr.users.roles.user)).toBeTruthy();
    expect(identity.getByText("rose@example.test")).toBeTruthy();
    // Written without its cedilla: what the page does today.
    expect(identity.getByText("Francais")).toBeTruthy();
    expect(
      identity.getByText(
        `${fr.users.createdAt}: ${format(rose.createdAt, "PPP", { locale: frLocale })}`,
      ),
    ).toBeTruthy();
    expect(screen.getAllByRole("link")[0].getAttribute("href")).toBe(
      "/dashboard/patients",
    );
  });

  it("shows an English-speaking patient as such, and dates in English for an English visitor", async () => {
    answer(api.sessions.listSessions, []);
    await open("admin", person({ _id: patientId, language: "en" }), "en");

    expect(screen.getByText("English")).toBeTruthy();
    expect(
      screen.getByText(
        `Created: ${format(rose.createdAt, "PPP", { locale: enUS })}`,
      ),
    ).toBeTruthy();
  });
});

describe("patient page: what each role may do", () => {
  it.each(["admin", "gestionnaire"] as const)(
    "lets a %s edit and delete the account, and set the physiology of this patient",
    async (role) => {
      answer(api.sessions.listSessions, []);
      await open(role);

      expect(screen.getByRole("button", editButton)).toBeTruthy();
      expect(screen.getByRole("button", deleteButton)).toBeTruthy();
      expect(propsOf("PhysiologyCard")).toEqual({
        userId: patientId,
        user: rose,
      });
    },
  );

  it.each([
    ["a patient", "user"],
    // What the page does today: the admin of a client organisation, whom the
    // server lets manage the accounts of that organisation, gets none of the
    // controls here.
    ["the admin of a client organisation", "org_admin"],
    ["an account without a row", null],
  ] as const)(
    "shows %s the account and nothing to act on it",
    async (_name, role) => {
      answer(api.sessions.listSessions, []);
      await open(role);

      expect(
        screen.getByRole("heading", { level: 1, name: "Rose Rider" }),
      ).toBeTruthy();
      expect(screen.queryByRole("button", editButton)).toBeNull();
      expect(screen.queryByRole("button", deleteButton)).toBeNull();
      expect(wasMounted("PhysiologyCard")).toBe(false);
    },
  );
});

describe("patient page: the sessions of the patient", () => {
  it("claims neither sessions nor their absence while they are loading", async () => {
    answer(api.sessions.listSessions, undefined);
    await open("gestionnaire");

    expect(screen.getByText("0 sessions")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText(fr.sessions.noSessions)).toBeNull();
  });

  it("says so when the patient has no session", async () => {
    answer(api.sessions.listSessions, []);
    await open("gestionnaire");

    expect(screen.getByText(fr.sessions.noSessions)).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("lists each session with its machine, status, start and length", async () => {
    answer(api.sessions.listSessions, [listed("A"), listed("B")]);
    await open("gestionnaire");

    expect(screen.getByText("2 sessions")).toBeTruthy();
    expect(
      rowOf("Machine A")
        .getAllByRole("cell")
        .slice(0, 4)
        .map((cell) => cell.textContent),
    ).toEqual([
      "Machine A",
      fr.sessions.status.completed,
      format(NOW - 7_200_000, "PPp", { locale: frLocale }),
      "30m 0s",
    ]);
  });

  it.each([
    ["45 s", 45_000, "45s"],
    ["1 h 02 min 05 s", 3_725_000, "1h 2m"],
  ])("gives the length of a session of %s", async (_name, ms, shown) => {
    answer(api.sessions.listSessions, [
      listed("A", { endedAt: NOW - 7_200_000 + ms }),
    ]);
    await open("gestionnaire");

    expect(rowOf("Machine A").getAllByRole("cell")[3].textContent).toBe(shown);
  });

  // Known and filed: the time since the start is counted on this computer's
  // clock, not on the server's. This is what the page does today.
  it("counts the time since the start of a session that has not ended, on this computer's clock", async () => {
    answer(api.sessions.listSessions, [
      listed("A", {
        status: "active",
        startedAt: NOW - 600_000,
        endedAt: undefined,
      }),
    ]);
    await open("gestionnaire");

    expect(rowOf("Machine A").getAllByRole("cell")[3].textContent).toBe(
      "10 minutes",
    );
  });

  it("leads to the live view of an active session and to the report of a completed one, and nowhere for the others", async () => {
    answer(api.sessions.listSessions, [
      listed("A", { status: "active", endedAt: undefined }),
      listed("B"),
      listed("C", { status: "failed" }),
      listed("D", { status: "pending", endedAt: undefined }),
    ]);
    await open("gestionnaire");

    const live = rowOf("Machine A").getByRole("link");
    expect(live.getAttribute("href")).toBe(
      "/dashboard/sessions/session-A/live",
    );
    expect(live.textContent).toBe(fr.sessions.viewLive);
    const report = rowOf("Machine B").getByRole("link");
    expect(report.getAttribute("href")).toBe("/dashboard/sessions/session-B");
    expect(report.textContent).toBe(fr.sessions.viewReport);
    for (const machine of ["Machine C", "Machine D"]) {
      expect(rowOf(machine).queryByRole("link")).toBeNull();
      expect(rowOf(machine).getAllByRole("cell")[4].textContent).toBe("-");
    }
    expect(
      rowOf("Machine C").getByText(fr.sessions.status.failed),
    ).toBeTruthy();
    expect(
      rowOf("Machine D").getByText(fr.sessions.status.pending),
    ).toBeTruthy();
  });

  it("shows a status the catalog does not know as the server sent it", async () => {
    // A status a later server could add: the list declares it a plain string.
    const unknown = "aborted" as Listed["status"];
    answer(api.sessions.listSessions, [listed("A", { status: unknown })]);
    await open("gestionnaire");

    expect(rowOf("Machine A").getByText("aborted")).toBeTruthy();
  });
});

describe("patient page: editing and deleting the account", () => {
  it("opens the edit window on this patient, closed until asked for", async () => {
    answer(api.sessions.listSessions, []);
    const { user } = await open("gestionnaire");
    expect(propsOf<EditWindow>("PatientFormModal").open).toBe(false);

    await user.click(screen.getByRole("button", editButton));

    expect(propsOf<EditWindow>("PatientFormModal").open).toBe(true);
    expect(propsOf<EditWindow>("PatientFormModal").patient).toEqual(rose);
  });

  it("deletes the account once confirmed, says so and goes back to the list", async () => {
    answer(api.sessions.listSessions, []);
    const { user } = await open("gestionnaire");

    await user.click(screen.getByRole("button", deleteButton));
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText(fr.users.deleteConfirm)).toBeTruthy();
    // Written in English whatever the visitor's language: what the page does today.
    expect(
      dialog.getByText(
        'This action cannot be undone. This will permanently delete the patient "Rose Rider" and all associated data.',
      ),
    ).toBeTruthy();
    expect(mutationsSent()).toEqual([]);
    await user.click(dialog.getByRole("button", deleteButton));

    expect(mutationsSent()).toEqual([
      { name: "users:deleteUser", args: { userId: patientId } },
    ]);
    expect(feedbackShown()).toEqual([
      { kind: "success", message: fr.feedback.userDeleted },
    ]);
    expect(router.push.mock.calls).toEqual([["/dashboard/patients"]]);
  });

  it("sends nothing when the deletion is declined", async () => {
    answer(api.sessions.listSessions, []);
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
    serverFailures(api.users.deleteUser, "Not authorized to delete this user"),
  )(
    "stays on the page, on its question, and shows the failure when the deletion fails $where",
    async ({ error, shown }) => {
      answer(api.sessions.listSessions, []);
      mutation(api.users.deleteUser).mockRejectedValue(error);
      const { user } = await open("gestionnaire");

      await user.click(screen.getByRole("button", deleteButton));
      await user.click(
        within(screen.getByRole("dialog")).getByRole("button", deleteButton),
      );

      expect(feedbackShown()).toEqual([{ kind: "error", message: shown }]);
      expect(takeLoggedFailures()).toEqual(["users:deleteUser"]);
      expect(router.push).not.toHaveBeenCalled();
      expect(
        within(screen.getByRole("dialog")).getByText(fr.users.deleteConfirm),
      ).toBeTruthy();
    },
  );
});

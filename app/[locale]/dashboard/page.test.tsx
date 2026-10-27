// @vitest-environment jsdom
import { act, screen, within } from "@testing-library/react";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";
import {
  answer,
  argsAsked,
  lastArgsAsked,
  NOW,
  renderPage,
  signedInAs,
} from "@/test-support/pages";
import DashboardPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);

/** The first page of the dashboard: the counts of the server, and the last sessions. */

type Counters = FunctionReturnType<typeof api.dashboard.counters>;
type Listed = FunctionReturnType<typeof api.sessions.listSessions>[number];

/** What `dashboard.counters` answers an organisation admin, with these changes. */
function counted(overrides: Partial<Counters> = {}): Counters {
  return {
    serverNow: NOW,
    members: { total: 16, admin: 2, gestionnaire: 3, user: 11 },
    patients: null,
    machines: {
      total: 4,
      signals: [{ status: "online", lastHeartbeat: NOW - 5_000 }],
    },
    sessions: { active: 0, pending: 0 },
    ...overrides,
  };
}

/** What `dashboard.counters` answers a gestionnaire. */
function countedForManager(overrides: Partial<Counters> = {}): Counters {
  return counted({ members: null, patients: 7, ...overrides });
}

/** What `dashboard.counters` answers a patient. */
function countedForPatient(overrides: Partial<Counters> = {}): Counters {
  return counted({
    members: null,
    patients: null,
    machines: null,
    ...overrides,
  });
}

function listed(name: string, overrides: Partial<Listed> = {}): Listed {
  return {
    _id: `session-${name}` as Id<"sessions">,
    status: "completed",
    startedAt: NOW - 3_600_000,
    endedAt: NOW - 1_800_000,
    channels: [],
    patientName: `Patient ${name}`,
    machineName: `Machine ${name}`,
    kind: "auto",
    origin: "remote",
    ...overrides,
  };
}

/** The card titled `title`, and the address it leads to. */
function card(title: string) {
  const link = screen.getByText(title).closest("a");
  if (link === null) throw new Error(`No card is titled "${title}"`);
  return { href: link.getAttribute("href"), ...within(link) };
}

/** The value a card shows in large figures. */
function figureOf(title: string): string | null | undefined {
  return screen.getByText(title).closest("a")?.querySelector(".text-2xl")
    ?.textContent;
}

/** The line under a card's figure. */
function detailOf(title: string): string | null | undefined {
  return screen.getByText(title).closest("a")?.querySelector("p")?.textContent;
}

describe("dashboard home: what it asks", () => {
  it("asks nothing of the counters and of the sessions until it knows who the visitor is", async () => {
    answer(api.users.getCurrentUser, undefined);
    const view = await renderPage(<DashboardPage />);

    expect(screen.queryByRole("heading")).toBeNull();
    expect(argsAsked(api.dashboard.counters)).toEqual([]);
    expect(argsAsked(api.sessions.listSessions)).toEqual(["skip"]);

    signedInAs("gestionnaire");
    await view.refresh();
    expect(lastArgsAsked(api.dashboard.counters)).toEqual({});
    expect(lastArgsAsked(api.sessions.listSessions)).toEqual({ limit: 10 });
    // The machines are counted by the server: their list is not read here.
    expect(argsAsked(api.machines.listMachines)).toEqual([]);
  });

  // What the page does today for an account Convex has no row for yet (the
  // row is created from the landing page): it asks nothing, so the counts
  // and the sessions stay "loading" for ever, under a welcome that names
  // nobody.
  it("shows an account without a row a welcome without a name, and counts and sessions that never load", async () => {
    answer(api.users.getCurrentUser, null);
    answer(api.dashboard.counters, countedForPatient());
    answer(api.sessions.listSessions, []);
    await renderPage(<DashboardPage />);

    expect(screen.getByText("Bienvenue,")).toBeTruthy();
    expect(argsAsked(api.dashboard.counters)).toEqual([]);
    expect(lastArgsAsked(api.sessions.listSessions)).toBe("skip");
    expect(screen.queryByText(fr.sessions.noSessions)).toBeNull();
    expect(
      card(fr.dashboard.activeSessions).getByText(fr.common.loading),
    ).toBeTruthy();
    expect(screen.queryByText(fr.dashboard.onlineMachines)).toBeNull();
  });
});

describe("dashboard home: what each role sees", () => {
  it("welcomes the visitor by their first name", async () => {
    signedInAs("user", { firstName: "Paul" });
    answer(api.dashboard.counters, countedForPatient());
    answer(api.sessions.listSessions, []);
    const french = await renderPage(<DashboardPage />);
    expect(
      screen.getByRole("heading", { name: fr.dashboard.title }),
    ).toBeTruthy();
    expect(screen.getByText("Bienvenue, Paul")).toBeTruthy();
    french.unmount();

    await renderPage(<DashboardPage />, { locale: "en" });
    expect(screen.getByText("Welcome, Paul")).toBeTruthy();
  });

  it.each([
    ["an admin", "admin"],
    // ANH-187: the admin of a client organisation manages its machines and
    // its members, as the server lets them.
    ["the admin of a client organisation", "org_admin"],
  ] as const)("shows %s the machines online and the way to the users", async (_name, role) => {
    signedInAs(role);
    answer(api.dashboard.counters, counted());
    answer(api.sessions.listSessions, []);
    await renderPage(<DashboardPage />);

    expect(card(fr.dashboard.onlineMachines).href).toBe("/dashboard/machines");
    expect(card(fr.nav.users).href).toBe("/dashboard/users");
    expect(screen.queryByText(fr.nav.patients)).toBeNull();
    expect(card(fr.dashboard.activeSessions).href).toBe("/dashboard/sessions");
  });

  it("tells the Anheart admin alone that the counts cover every organisation", async () => {
    signedInAs("admin");
    answer(api.dashboard.counters, counted());
    answer(api.sessions.listSessions, []);
    const anheart = await renderPage(<DashboardPage />);
    expect(screen.getByText(fr.dashboard.allOrganizations)).toBeTruthy();
    anheart.unmount();

    signedInAs("org_admin");
    await renderPage(<DashboardPage />);
    expect(screen.queryByText(fr.dashboard.allOrganizations)).toBeNull();
  });

  it("shows a manager the machines online and the way to the patients", async () => {
    signedInAs("gestionnaire");
    answer(api.dashboard.counters, countedForManager());
    answer(api.sessions.listSessions, []);
    await renderPage(<DashboardPage />);

    expect(card(fr.dashboard.onlineMachines).href).toBe("/dashboard/machines");
    expect(card(fr.nav.patients).href).toBe("/dashboard/patients");
    expect(screen.queryByText(fr.nav.users)).toBeNull();
    expect(screen.queryByText(fr.dashboard.allOrganizations)).toBeNull();
  });

  // The card counts the machines in a session too: "connected", not "online".
  it("titles the machines card « Machines connectées » in French and « Connected machines » in English", async () => {
    signedInAs("admin");
    answer(api.dashboard.counters, counted());
    answer(api.sessions.listSessions, []);
    const french = await renderPage(<DashboardPage />);
    expect(card("Machines connectées").href).toBe("/dashboard/machines");
    french.unmount();

    await renderPage(<DashboardPage />, { locale: "en" });
    expect(card("Connected machines").href).toBe("/dashboard/machines");
  });

  it.each([["a patient", "user"]] as const)(
    "shows %s the active sessions only, neither machines nor people",
    async (_name, role) => {
      signedInAs(role);
      answer(api.dashboard.counters, countedForPatient());
      answer(api.sessions.listSessions, []);
      await renderPage(<DashboardPage />);

      expect(card(fr.dashboard.activeSessions).href).toBe(
        "/dashboard/sessions",
      );
      expect(screen.queryByText(fr.dashboard.onlineMachines)).toBeNull();
      expect(screen.queryByText(fr.nav.users)).toBeNull();
      expect(screen.queryByText(fr.nav.patients)).toBeNull();
      expect(screen.getAllByRole("link")).toHaveLength(1);
    },
  );
});

describe("dashboard home: the counts", () => {
  it("counts online the machines shown online or in a session, heard from in the last 90 s, out of all of them", async () => {
    signedInAs("gestionnaire");
    answer(
      api.dashboard.counters,
      countedForManager({
        machines: {
          total: 5,
          signals: [
            { status: "online", lastHeartbeat: NOW - 5_000 },
            { status: "online", lastHeartbeat: NOW - 89_000 },
            // The server has not written "offline" yet: silent for 91 s.
            { status: "online", lastHeartbeat: NOW - 91_000 },
            { status: "in_session", lastHeartbeat: NOW - 5_000 },
          ],
        },
      }),
    );
    answer(api.sessions.listSessions, []);
    const english = await renderPage(<DashboardPage />, { locale: "en" });
    expect(figureOf(en.dashboard.onlineMachines)).toBe("3");
    expect(detailOf(en.dashboard.onlineMachines)).toBe(
      "/ 5 in total, including 1 in session",
    );
    english.unmount();

    await renderPage(<DashboardPage />);
    expect(figureOf(fr.dashboard.onlineMachines)).toBe("3");
    expect(detailOf(fr.dashboard.onlineMachines)).toBe(
      "/ 5 au total, dont 1 en session",
    );
  });

  it("takes a machine out of the count 90 s after its last signal, with no new answer from the server", async () => {
    signedInAs("admin");
    answer(
      api.dashboard.counters,
      counted({
        machines: {
          total: 2,
          signals: [
            { status: "online", lastHeartbeat: NOW - 89_000 },
            { status: "online", lastHeartbeat: NOW - 1_000 },
          ],
        },
      }),
    );
    answer(api.sessions.listSessions, []);
    // The page reads its clock again every second.
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    vi.setSystemTime(NOW);
    await renderPage(<DashboardPage />);
    expect(figureOf(fr.dashboard.onlineMachines)).toBe("2");

    act(() => {
      vi.advanceTimersByTime(999);
    });
    expect(figureOf(fr.dashboard.onlineMachines)).toBe("2");
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(figureOf(fr.dashboard.onlineMachines)).toBe("1");
    expect(detailOf(fr.dashboard.onlineMachines)).toBe("/ 2 au total");
  });

  it("counts no machine online when none is heard from", async () => {
    signedInAs("org_admin");
    answer(
      api.dashboard.counters,
      counted({ machines: { total: 3, signals: [] } }),
    );
    answer(api.sessions.listSessions, []);
    await renderPage(<DashboardPage />);

    expect(figureOf(fr.dashboard.onlineMachines)).toBe("0");
    expect(detailOf(fr.dashboard.onlineMachines)).toBe("/ 3 au total");
  });

  it("counts the active sessions, and says how many are pending", async () => {
    signedInAs("user");
    answer(
      api.dashboard.counters,
      countedForPatient({ sessions: { active: 2, pending: 1 } }),
    );
    // The recent sessions are not what is counted.
    answer(api.sessions.listSessions, [listed("A", { status: "active" })]);
    const french = await renderPage(<DashboardPage />);
    expect(figureOf(fr.dashboard.activeSessions)).toBe("2");
    expect(detailOf(fr.dashboard.activeSessions)).toBe("1 en attente");
    french.unmount();

    answer(api.dashboard.counters, countedForPatient());
    await renderPage(<DashboardPage />, { locale: "en" });
    expect(figureOf(en.dashboard.activeSessions)).toBe("0");
    expect(detailOf(en.dashboard.activeSessions)).toBe("None pending");
  });

  it("counts an organisation's members, and each role apart", async () => {
    signedInAs("org_admin");
    answer(api.dashboard.counters, counted());
    answer(api.sessions.listSessions, []);
    const french = await renderPage(<DashboardPage />);
    expect(figureOf(fr.nav.users)).toBe("16");
    expect(detailOf(fr.nav.users)).toBe(
      "2 administrateurs · 3 gestionnaires · 11 patients",
    );
    french.unmount();

    answer(
      api.dashboard.counters,
      counted({ members: { total: 2, admin: 1, gestionnaire: 0, user: 1 } }),
    );
    await renderPage(<DashboardPage />, { locale: "en" });
    expect(figureOf(en.nav.users)).toBe("2");
    expect(detailOf(en.nav.users)).toBe("1 admin · 0 managers · 1 patient");
  });

  it("shows the accounts the server counted, not the sum of the roles, which can be more", async () => {
    // One account is a gestionnaire in one centre and a patient in another:
    // counted once in all, and once in each role.
    signedInAs("admin");
    answer(
      api.dashboard.counters,
      counted({ members: { total: 10, admin: 3, gestionnaire: 3, user: 5 } }),
    );
    answer(api.sessions.listSessions, []);
    const french = await renderPage(<DashboardPage />);
    expect(figureOf(fr.nav.users)).toBe("10");
    expect(detailOf(fr.nav.users)).toBe(
      "3 administrateurs · 3 gestionnaires · 5 patients",
    );
    french.unmount();

    await renderPage(<DashboardPage />, { locale: "en" });
    expect(figureOf(en.nav.users)).toBe("10");
    expect(detailOf(en.nav.users)).toBe("3 admins · 3 managers · 5 patients");
  });

  it("counts a manager's own patients", async () => {
    signedInAs("gestionnaire");
    answer(api.dashboard.counters, countedForManager());
    answer(api.sessions.listSessions, []);
    await renderPage(<DashboardPage />);

    expect(figureOf(fr.nav.patients)).toBe("7");
    expect(detailOf(fr.nav.patients)).toBe(fr.dashboard.ownPatients);
  });

  it("shows every card loading until the counts arrive, and no figure", async () => {
    signedInAs("org_admin");
    answer(api.dashboard.counters, undefined);
    answer(api.sessions.listSessions, []);
    const view = await renderPage(<DashboardPage />);

    for (const title of [
      fr.dashboard.onlineMachines,
      fr.dashboard.activeSessions,
      fr.nav.users,
    ]) {
      expect(card(title).getByRole("status").textContent).toBe(
        fr.common.loading,
      );
      expect(figureOf(title)).toBeUndefined();
    }

    answer(api.dashboard.counters, counted());
    await view.refresh();
    expect(screen.queryAllByRole("status")).toEqual([]);
    expect(figureOf(fr.nav.users)).toBe("16");
  });

  it("says each count is unavailable when the server failed them, and keeps the rest of the page", async () => {
    signedInAs("gestionnaire");
    answer(
      api.dashboard.counters,
      new Error(
        "[CONVEX Q(dashboard:counters)] [Request ID: 8f3a9c1d2b4e6f70] Server Error\n  Called by client",
      ),
    );
    answer(api.sessions.listSessions, [listed("A")]);
    const view = await renderPage(<DashboardPage />);

    for (const title of [
      fr.dashboard.onlineMachines,
      fr.dashboard.activeSessions,
      fr.nav.patients,
    ]) {
      expect(figureOf(title)).toBe("-");
      expect(detailOf(title)).toBe(fr.dashboard.counterUnavailable);
    }
    expect(screen.getByText("Patient A")).toBeTruthy();

    // The query answers again: the figures come back without a reload.
    answer(api.dashboard.counters, countedForManager());
    await view.refresh();
    expect(figureOf(fr.nav.patients)).toBe("7");
  });

  it("says a count is unavailable when the server sent none for the card", async () => {
    // A server whose rule for the role differs from the site's.
    signedInAs("gestionnaire");
    answer(api.dashboard.counters, countedForPatient());
    answer(api.sessions.listSessions, []);
    await renderPage(<DashboardPage />);

    expect(figureOf(fr.dashboard.onlineMachines)).toBe("-");
    expect(detailOf(fr.dashboard.onlineMachines)).toBe(
      fr.dashboard.counterUnavailable,
    );
    expect(figureOf(fr.nav.patients)).toBe("-");
    expect(figureOf(fr.dashboard.activeSessions)).toBe("0");
  });
});

describe("dashboard home: the recent sessions", () => {
  /** The rows under "recent sessions": each is a link. */
  function recent() {
    const section = screen
      .getByText(fr.dashboard.recentSessions)
      .closest("[data-slot=card]") as HTMLElement;
    return within(section).queryAllByRole("link");
  }

  it("claims neither sessions nor their absence while they are loading", async () => {
    signedInAs("user");
    answer(api.sessions.listSessions, undefined);
    await renderPage(<DashboardPage />);

    expect(screen.getByText(fr.dashboard.recentSessions)).toBeTruthy();
    expect(recent()).toEqual([]);
    expect(screen.queryByText(fr.sessions.noSessions)).toBeNull();
  });

  it("says so when there is no session", async () => {
    signedInAs("user");
    answer(api.sessions.listSessions, []);
    await renderPage(<DashboardPage />);

    expect(screen.getByText(fr.sessions.noSessions)).toBeTruthy();
    expect(recent()).toEqual([]);
  });

  it("lists the five most recent, each with its rider, machine and status", async () => {
    signedInAs("user");
    answer(
      api.sessions.listSessions,
      ["A", "B", "C", "D", "E", "F", "G"].map((name) => listed(name)),
    );
    await renderPage(<DashboardPage />);

    expect(
      recent().map((row) => within(row).getByText(/^Patient/).textContent),
    ).toEqual([
      "Patient A",
      "Patient B",
      "Patient C",
      "Patient D",
      "Patient E",
    ]);
    const first = within(recent()[0]);
    expect(first.getByText("Machine A")).toBeTruthy();
    expect(first.getByText(fr.sessions.status.completed)).toBeTruthy();
  });

  it("leads to the live view of an active session, and to the detail of any other", async () => {
    signedInAs("user");
    answer(api.sessions.listSessions, [
      listed("A", { status: "active", endedAt: undefined }),
      listed("B"),
      listed("C", { status: "pending", endedAt: undefined }),
      listed("D", { status: "failed" }),
    ]);
    await renderPage(<DashboardPage />);

    expect(recent().map((row) => row.getAttribute("href"))).toEqual([
      "/dashboard/sessions/session-A/live",
      "/dashboard/sessions/session-B",
      "/dashboard/sessions/session-C",
      "/dashboard/sessions/session-D",
    ]);
    expect(
      within(recent()[0]).getByText(fr.sessions.status.active),
    ).toBeTruthy();
    expect(
      within(recent()[2]).getByText(fr.sessions.status.pending),
    ).toBeTruthy();
    expect(
      within(recent()[3]).getByText(fr.sessions.status.failed),
    ).toBeTruthy();
  });

  it("shows a status the catalog does not know as the server sent it", async () => {
    signedInAs("user");
    // A status a later server could add: the list declares it a plain string.
    const unknown = "aborted" as Listed["status"];
    answer(api.sessions.listSessions, [listed("A", { status: unknown })]);
    await renderPage(<DashboardPage />);

    expect(within(recent()[0]).getByText("aborted")).toBeTruthy();
  });

  // Known and filed: the age of a session is counted on this computer's
  // clock, not on the server's. This is what the page does today.
  it("says how long ago each started, on this computer's clock and in the visitor's language", async () => {
    signedInAs("user");
    answer(api.sessions.listSessions, [
      listed("A", { startedAt: NOW - 600_000 }),
    ]);
    const french = await renderPage(<DashboardPage />);
    expect(within(recent()[0]).getByText("il y a 10 minutes")).toBeTruthy();
    french.unmount();

    await renderPage(<DashboardPage />, { locale: "en" });
    expect(screen.getByText("10 minutes ago")).toBeTruthy();
  });
});

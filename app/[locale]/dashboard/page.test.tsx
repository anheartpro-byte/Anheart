// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
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

/** The first page of the dashboard: a count of what runs, and the last sessions. */

type Machine = FunctionReturnType<typeof api.machines.listMachines>[number];
type Listed = FunctionReturnType<typeof api.sessions.listSessions>[number];

function machine(name: string, overrides: Partial<Machine> = {}): Machine {
  return {
    _id: `machine-${name}` as Id<"machines">,
    name,
    status: "online",
    lastHeartbeat: NOW - 5_000,
    serverNow: NOW,
    location: undefined,
    isDeleted: undefined,
    ...overrides,
  };
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

describe("dashboard home: what it asks", () => {
  it("asks nothing of the machines and of the sessions until it knows who the visitor is", async () => {
    answer(api.users.getCurrentUser, undefined);
    const view = await renderPage(<DashboardPage />);

    expect(screen.queryByRole("heading")).toBeNull();
    expect(argsAsked(api.machines.listMachines)).toEqual(["skip"]);
    expect(argsAsked(api.sessions.listSessions)).toEqual(["skip"]);

    signedInAs("gestionnaire");
    await view.refresh();
    expect(lastArgsAsked(api.machines.listMachines)).toEqual({});
    expect(lastArgsAsked(api.sessions.listSessions)).toEqual({ limit: 10 });
  });

  // What the page does today for an account Convex has no row for yet (the
  // row is created from the landing page): it asks nothing, so the sessions
  // stay "loading" for ever, under a welcome that names nobody.
  it("shows an account without a row a welcome without a name and sessions that never load", async () => {
    answer(api.users.getCurrentUser, null);
    answer(api.sessions.listSessions, []);
    await renderPage(<DashboardPage />);

    expect(screen.getByText("Bienvenue,")).toBeTruthy();
    expect(lastArgsAsked(api.sessions.listSessions)).toBe("skip");
    expect(screen.queryByText(fr.sessions.noSessions)).toBeNull();
    expect(screen.queryByText(fr.dashboard.onlineMachines)).toBeNull();
  });
});

describe("dashboard home: what each role sees", () => {
  it("welcomes the visitor by their first name", async () => {
    signedInAs("user", { firstName: "Paul" });
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

  it("shows an admin the machines online and the way to the users", async () => {
    signedInAs("admin");
    answer(api.machines.listMachines, [machine("A")]);
    answer(api.sessions.listSessions, []);
    await renderPage(<DashboardPage />);

    expect(card(fr.dashboard.onlineMachines).href).toBe("/dashboard/machines");
    expect(card(fr.nav.users).href).toBe("/dashboard/users");
    expect(screen.queryByText(fr.nav.patients)).toBeNull();
    expect(card(fr.dashboard.activeSessions).href).toBe("/dashboard/sessions");
  });

  it("shows a manager the machines online and the way to the patients", async () => {
    signedInAs("gestionnaire");
    answer(api.machines.listMachines, [machine("A")]);
    answer(api.sessions.listSessions, []);
    await renderPage(<DashboardPage />);

    expect(card(fr.dashboard.onlineMachines).href).toBe("/dashboard/machines");
    expect(card(fr.nav.patients).href).toBe("/dashboard/patients");
    expect(screen.queryByText(fr.nav.users)).toBeNull();
  });

  it.each([
    ["a patient", "user"],
    // What the page does today: the admin of a client organisation, who
    // manages its machines and its members on the server, sees neither card.
    ["the admin of a client organisation", "org_admin"],
  ] as const)(
    "shows %s the active sessions only, neither machines nor people",
    async (_name, role) => {
      signedInAs(role);
      answer(api.machines.listMachines, [machine("A")]);
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

  // Written in the dashboard's guide: this counter is not implemented.
  it.each([
    ["an admin", "admin", fr.nav.users],
    ["a manager", "gestionnaire", fr.nav.patients],
  ] as const)(
    "counts no people for %s: a dash, said to be loading",
    async (_name, role, title) => {
      signedInAs(role);
      answer(api.machines.listMachines, []);
      answer(api.sessions.listSessions, []);
      await renderPage(<DashboardPage />);

      expect(figureOf(title)).toBe("-");
      expect(card(title).getByText(fr.common.loading)).toBeTruthy();
    },
  );
});

describe("dashboard home: the counts", () => {
  it("counts online the machines heard from in the last 90 s, out of all of them", async () => {
    signedInAs("gestionnaire");
    answer(api.machines.listMachines, [
      machine("A"),
      machine("B", { lastHeartbeat: NOW - 89_000 }),
      // The server has not written "offline" yet: silent for 91 s.
      machine("C", { lastHeartbeat: NOW - 91_000 }),
      machine("D", { status: "offline", lastHeartbeat: NOW - 3_600_000 }),
      machine("E", { status: "in_session" }),
    ]);
    answer(api.sessions.listSessions, []);
    await renderPage(<DashboardPage />);

    expect(figureOf(fr.dashboard.onlineMachines)).toBe("2");
    // "total" is written in the page, in every language: what it does today.
    expect(
      card(fr.dashboard.onlineMachines).getByText("/ 5 total"),
    ).toBeTruthy();
  });

  it("counts no machine while their list is loading", async () => {
    signedInAs("admin");
    answer(api.machines.listMachines, undefined);
    answer(api.sessions.listSessions, []);
    await renderPage(<DashboardPage />);

    expect(figureOf(fr.dashboard.onlineMachines)).toBe("0");
    expect(
      card(fr.dashboard.onlineMachines).getByText("/ 0 total"),
    ).toBeTruthy();
  });

  it("counts the active sessions among the last ten", async () => {
    signedInAs("user");
    answer(api.sessions.listSessions, [
      listed("A", { status: "active", endedAt: undefined }),
      listed("B"),
      listed("C", { status: "active", endedAt: undefined }),
      listed("D", { status: "pending", endedAt: undefined }),
      listed("E", { status: "failed" }),
    ]);
    await renderPage(<DashboardPage />);

    expect(figureOf(fr.dashboard.activeSessions)).toBe("2");
  });

  it("counts no active session while the sessions are loading", async () => {
    signedInAs("user");
    answer(api.sessions.listSessions, undefined);
    await renderPage(<DashboardPage />);

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

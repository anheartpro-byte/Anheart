// @vitest-environment jsdom
import { Component, type ReactNode } from "react";
import { screen, within } from "@testing-library/react";
import { format, formatDistanceToNow } from "date-fns";
import { enUS, fr as frLocale } from "date-fns/locale";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";
import {
  answer,
  argsAsked,
  NOW,
  renderPage,
  router,
  takeConsoleErrors,
} from "@/test-support/pages";
import SessionsPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);

/**
 * The list of sessions: what a manager opens to find a session that is
 * running, and the way into its live view.
 */

type Listed = FunctionReturnType<typeof api.sessions.listSessions>[number];

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

const running = listed("A", {
  status: "active",
  startedAt: NOW - 600_000,
  endedAt: undefined,
});
const finished = listed("B");
const failed = listed("C", {
  status: "failed",
  kind: "manual",
  origin: "local",
});
const waiting = listed("D", {
  status: "pending",
  startedAt: NOW - 30_000,
  endedAt: undefined,
});

/** The row of the table that names `text`. */
function rowOf(text: string): HTMLElement {
  const row = screen.getByText(text).closest("tr");
  if (row === null) throw new Error(`No row holds "${text}"`);
  return row;
}

/** The patients listed, in the order of the rows. */
function patientsListed(): string[] {
  return screen
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0].textContent ?? "");
}

describe("sessions list: what it asks and shows", () => {
  it("asks for the last 100 sessions, once for all tabs", async () => {
    answer(api.sessions.listSessions, [running]);
    const { user } = await renderPage(<SessionsPage />);
    await user.click(screen.getByRole("tab", { name: fr.sessions.completed }));

    expect(
      new Set(
        argsAsked(api.sessions.listSessions).map((a) => JSON.stringify(a)),
      ),
    ).toEqual(new Set([JSON.stringify({ limit: 100 })]));
  });

  it("shows no table and no count while the list is loading", async () => {
    answer(api.sessions.listSessions, undefined);
    await renderPage(<SessionsPage />);

    expect(
      screen.queryByRole("heading", { name: fr.sessions.title }),
    ).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText(fr.sessions.noSessions)).toBeNull();
  });

  it("says so when there is no session", async () => {
    answer(api.sessions.listSessions, []);
    await renderPage(<SessionsPage />);

    expect(screen.getByText(fr.sessions.noSessions)).toBeTruthy();
    expect(screen.getByText("0 sessions")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("shows each session with its rider, machine, kind, origin and status", async () => {
    answer(api.sessions.listSessions, [running, failed]);
    await renderPage(<SessionsPage />);

    expect(screen.getByText("2 sessions")).toBeTruthy();
    const first = within(rowOf("Patient A"));
    expect(first.getByText("Machine A")).toBeTruthy();
    expect(first.getByText(fr.training.kind.auto)).toBeTruthy();
    expect(first.getByText(fr.training.origin.remote)).toBeTruthy();
    expect(first.getByText(fr.sessions.status.active)).toBeTruthy();
    const second = within(rowOf("Patient C"));
    expect(second.getByText(fr.training.kind.manual)).toBeTruthy();
    expect(second.getByText(fr.training.origin.local)).toBeTruthy();
    expect(second.getByText(fr.sessions.status.failed)).toBeTruthy();
  });

  it("shows a status the catalog does not know as the server sent it", async () => {
    // A status a later server could add: the list declares it a plain string.
    const unknown = "aborted" as Listed["status"];
    answer(api.sessions.listSessions, [listed("E", { status: unknown })]);
    await renderPage(<SessionsPage />);

    expect(within(rowOf("Patient E")).getByText("aborted")).toBeTruthy();
  });

  it("dates the start in the visitor's language", async () => {
    answer(api.sessions.listSessions, [finished]);
    const french = await renderPage(<SessionsPage />);
    expect(
      within(rowOf("Patient B")).getByText(
        format(finished.startedAt, "PPp", { locale: frLocale }),
      ),
    ).toBeTruthy();
    french.unmount();

    await renderPage(<SessionsPage />, { locale: "en" });
    expect(
      within(rowOf("Patient B")).getByText(
        format(finished.startedAt, "PPp", { locale: enUS }),
      ),
    ).toBeTruthy();
    expect(
      screen.getByRole("heading", { name: en.sessions.title }),
    ).toBeTruthy();
  });

  it.each([
    ["45 s", 45_000, "45s"],
    ["30 min", 1_800_000, "30m 0s"],
    ["1 h 02 min 05 s", 3_725_000, "1h 2m"],
  ])(
    "shows the length of a finished session (%s)",
    async (_name, ms, shown) => {
      answer(api.sessions.listSessions, [
        listed("B", {
          startedAt: NOW - 7_200_000,
          endedAt: NOW - 7_200_000 + ms,
        }),
      ]);
      await renderPage(<SessionsPage />);

      expect(within(rowOf("Patient B")).getByText(shown)).toBeTruthy();
    },
  );

  // Known and filed: the time since the start is counted on this computer's
  // clock, not on the server's. This is what the page does today.
  it("counts the time since the start of a session that has not ended, on this computer's clock", async () => {
    answer(api.sessions.listSessions, [running]);
    await renderPage(<SessionsPage />);

    const since = formatDistanceToNow(running.startedAt, { locale: frLocale });
    expect(since).toBe("10 minutes");
    expect(within(rowOf("Patient A")).getByText(since)).toBeTruthy();
  });
});

describe("sessions list: when the server refuses the list", () => {
  /** What surrounds the page in this test: it keeps the error it catches. */
  class Surroundings extends Component<
    { children: ReactNode },
    { caught: Error | null }
  > {
    state = { caught: null as Error | null };
    static getDerivedStateFromError(caught: Error) {
      return { caught };
    }
    render() {
      return this.state.caught ? (
        <p role="alert">{this.state.caught.message}</p>
      ) : (
        this.props.children
      );
    }
  }

  // What the page does today, like every page of the dashboard: a query that
  // fails throws while the page is drawn, and the page catches nothing. No
  // page under app/ has an error screen of its own: the error reaches
  // whatever surrounds the page.
  it("has no error state of its own: the failure reaches what surrounds the page", async () => {
    answer(api.sessions.listSessions, () => {
      throw new Error("User not found");
    });
    await renderPage(
      <Surroundings>
        <SessionsPage />
      </Surroundings>,
    );

    expect(screen.getByRole("alert").textContent).toBe("User not found");
    expect(screen.queryByText(fr.sessions.title)).toBeNull();
    expect(screen.queryByText(fr.sessions.noSessions)).toBeNull();
    // React reports the error it handed to the surroundings.
    expect(takeConsoleErrors().join("\n")).toContain("User not found");
  });
});

describe("sessions list: the way to a session", () => {
  it("offers the live view of an active session, and the detail of any other", async () => {
    answer(api.sessions.listSessions, [running, finished, failed, waiting]);
    await renderPage(<SessionsPage />);

    const live = within(rowOf("Patient A")).getByRole("link");
    expect(live.getAttribute("href")).toBe(
      "/dashboard/sessions/session-A/live",
    );
    expect(within(live).getByText(fr.sessions.viewLive)).toBeTruthy();
    for (const [patient, id] of [
      ["Patient B", "session-B"],
      ["Patient C", "session-C"],
      ["Patient D", "session-D"],
    ]) {
      const link = within(rowOf(patient)).getByRole("link");
      expect(link.getAttribute("href")).toBe(`/dashboard/sessions/${id}`);
      expect(within(link).queryByText(fr.sessions.viewLive)).toBeNull();
    }
  });

  it("opens the live view when the row of an active session is clicked", async () => {
    answer(api.sessions.listSessions, [running, finished]);
    const { user } = await renderPage(<SessionsPage />);

    await user.click(screen.getByText("Machine A"));

    expect(router.push.mock.calls).toEqual([
      ["/dashboard/sessions/session-A/live"],
    ]);
  });

  it.each([
    ["finished", finished],
    ["failed", failed],
    ["pending", waiting],
  ])(
    "opens the detail when the row of a %s session is clicked",
    async (_name, session) => {
      answer(api.sessions.listSessions, [session]);
      const { user } = await renderPage(<SessionsPage />);

      await user.click(screen.getByText(session.machineName));

      expect(router.push.mock.calls).toEqual([
        [`/dashboard/sessions/${session._id}`],
      ]);
    },
  );
});

describe("sessions list: tabs and search", () => {
  it.each([
    [
      "all",
      fr.common.all,
      ["Patient A", "Patient B", "Patient C", "Patient D"],
    ],
    ["active", fr.sessions.active, ["Patient A"]],
    ["completed", fr.sessions.completed, ["Patient B"]],
    ["failed", fr.sessions.failed, ["Patient C"]],
  ])(
    "the %s tab lists the sessions of that status and counts them",
    async (_name, tab, expected) => {
      answer(api.sessions.listSessions, [running, finished, failed, waiting]);
      const { user } = await renderPage(<SessionsPage />);

      await user.click(screen.getByRole("tab", { name: tab }));

      expect(patientsListed()).toEqual(expected);
      expect(screen.getByText(`${expected.length} sessions`)).toBeTruthy();
    },
  );

  it("has no tab for the sessions waiting for the machine: they are under 'all' only", async () => {
    answer(api.sessions.listSessions, [waiting]);
    await renderPage(<SessionsPage />);

    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      fr.common.all,
      fr.sessions.active,
      fr.sessions.completed,
      fr.sessions.failed,
    ]);
    expect(patientsListed()).toEqual(["Patient D"]);
  });

  it("says there is no session when a tab is empty", async () => {
    answer(api.sessions.listSessions, [finished]);
    const { user } = await renderPage(<SessionsPage />);

    await user.click(screen.getByRole("tab", { name: fr.sessions.failed }));

    expect(screen.getByText(fr.sessions.noSessions)).toBeTruthy();
    expect(screen.getByText("0 sessions")).toBeTruthy();
  });

  it("keeps the rows that match the search, by rider or by machine", async () => {
    answer(api.sessions.listSessions, [running, finished, failed]);
    const { user } = await renderPage(<SessionsPage />);
    const search = screen.getByPlaceholderText(fr.common.search);

    await user.type(search, "patient b");
    expect(patientsListed()).toEqual(["Patient B"]);

    await user.clear(search);
    await user.type(search, "Machine C");
    expect(patientsListed()).toEqual(["Patient C"]);

    await user.clear(search);
    await user.type(search, "nobody");
    expect(screen.getByText(fr.sessions.noSessions)).toBeTruthy();
  });
});

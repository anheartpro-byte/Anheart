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
  pagesOf,
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
 * running, and the way into its live view. The server lists them page by
 * page, newest start first, the sessions of one status per tab (ANH-158).
 */

const LIST = api.sessions.listSessionsPaginated;
type Listed = FunctionReturnType<typeof LIST>["page"][number];

/** The server lists these sessions, whatever the tab. */
function serverLists(...sessions: Listed[]) {
  answer(LIST, pagesOf(sessions));
}

/** The server lists these sessions, each tab those of its status. */
function serverListsByStatus(...sessions: Listed[]) {
  answer(LIST, (args) =>
    pagesOf(
      sessions.filter(
        (s) => args.status === undefined || s.status === args.status,
      ),
    )(args),
  );
}

/** What the page asked of the server for a first page, with these filters. */
const firstPage = (filters: object = {}) => ({
  ...filters,
  paginationOpts: { numItems: 50, cursor: null },
});

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
/** No news of it for 10 minutes: its end is unknown (ANH-216). */
const interrupted = listed("E", {
  status: "interrupted",
  startedAt: NOW - 7_200_000,
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
  it("asks for the 50 newest sessions, then for those of the status of the tab chosen", async () => {
    serverLists(running);
    const { user } = await renderPage(<SessionsPage />);
    expect(argsAsked(LIST)).toContainEqual(firstPage());
    expect(argsAsked(LIST)).not.toContainEqual(
      firstPage({ status: "completed" }),
    );

    await user.click(screen.getByRole("tab", { name: fr.sessions.completed }));

    expect(argsAsked(LIST).at(-1)).toEqual(firstPage({ status: "completed" }));
    // Only first pages were asked: nobody asked for more.
    expect(
      argsAsked(LIST).every(
        (args) =>
          (args as ReturnType<typeof firstPage>).paginationOpts.cursor === null,
      ),
    ).toBe(true);
  });

  it("shows the title and the tabs, but no table and no count, while the first page loads", async () => {
    answer(LIST, undefined);
    await renderPage(<SessionsPage />);

    expect(
      screen.getByRole("heading", { name: fr.sessions.title }),
    ).toBeTruthy();
    expect(screen.getAllByRole("tab")).toHaveLength(6);
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText(fr.sessions.noSessions)).toBeNull();
    expect(screen.queryByText(/sessions$/)).toBeNull();
    expect(
      screen.queryByRole("button", { name: fr.common.showMore }),
    ).toBeNull();
  });

  it("says so when there is no session", async () => {
    serverLists();
    await renderPage(<SessionsPage />);

    expect(screen.getByText(fr.sessions.noSessions)).toBeTruthy();
    expect(screen.getByText("0 sessions")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("shows each session with its rider, machine, kind, origin and status", async () => {
    serverLists(running, failed);
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
    serverLists(listed("E", { status: unknown }));
    await renderPage(<SessionsPage />);

    expect(within(rowOf("Patient E")).getByText("aborted")).toBeTruthy();
  });

  it("dates the start in the visitor's language", async () => {
    serverLists(finished);
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
      serverLists(
        listed("B", {
          startedAt: NOW - 7_200_000,
          endedAt: NOW - 7_200_000 + ms,
        }),
      );
      await renderPage(<SessionsPage />);

      expect(within(rowOf("Patient B")).getByText(shown)).toBeTruthy();
    },
  );

  it("gives an interrupted session no length and no time since its start: its end is unknown", async () => {
    serverLists(interrupted);
    await renderPage(<SessionsPage />);

    const row = within(rowOf("Patient E"));
    expect(row.getByText(fr.sessions.status.interrupted)).toBeTruthy();
    expect(row.getByText(fr.sessions.unknownEnd)).toBeTruthy();
    expect(
      row.queryByText(
        formatDistanceToNow(interrupted.startedAt, { locale: frLocale }),
      ),
    ).toBeNull();
  });

  // Known and filed: the time since the start is counted on this computer's
  // clock, not on the server's. This is what the page does today.
  it("counts the time since the start of a session that has not ended, on this computer's clock", async () => {
    serverLists(running);
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
    answer(LIST, () => {
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
    serverLists(running, finished, failed, waiting, interrupted);
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
      ["Patient E", "session-E"],
    ]) {
      const link = within(rowOf(patient)).getByRole("link");
      expect(link.getAttribute("href")).toBe(`/dashboard/sessions/${id}`);
      expect(within(link).queryByText(fr.sessions.viewLive)).toBeNull();
    }
  });

  it("opens the live view when the row of an active session is clicked", async () => {
    serverLists(running, finished);
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
    ["interrupted", interrupted],
  ])(
    "opens the detail when the row of a %s session is clicked",
    async (_name, session) => {
      serverLists(session);
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
      ["Patient A", "Patient B", "Patient C", "Patient D", "Patient E"],
      {},
    ],
    ["pending", fr.sessions.pending, ["Patient D"], { status: "pending" }],
    ["active", fr.sessions.active, ["Patient A"], { status: "active" }],
    [
      "interrupted",
      fr.sessions.interrupted,
      ["Patient E"],
      { status: "interrupted" },
    ],
    [
      "completed",
      fr.sessions.completed,
      ["Patient B"],
      { status: "completed" },
    ],
    ["failed", fr.sessions.failed, ["Patient C"], { status: "failed" }],
  ])(
    "the %s tab asks the server for the sessions of that status, lists them and counts them",
    async (_name, tab, expected, filters) => {
      serverListsByStatus(running, finished, failed, waiting, interrupted);
      const { user } = await renderPage(<SessionsPage />);

      await user.click(screen.getByRole("tab", { name: tab }));

      expect(argsAsked(LIST).at(-1)).toEqual(firstPage(filters));
      expect(patientsListed()).toEqual(expected);
      expect(screen.getByText(`${expected.length} sessions`)).toBeTruthy();
    },
  );

  it("has a tab for the sessions waiting for the machine, after 'all'", async () => {
    serverListsByStatus(waiting);
    await renderPage(<SessionsPage />);

    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      fr.common.all,
      fr.sessions.pending,
      fr.sessions.active,
      fr.sessions.interrupted,
      fr.sessions.completed,
      fr.sessions.failed,
    ]);
    expect(fr.sessions.pending).toBe("En attente");
    expect(fr.sessions.interrupted).toBe("Interrompues");
    expect(patientsListed()).toEqual(["Patient D"]);
  });

  it("says there is no session when a tab is empty", async () => {
    serverListsByStatus(finished);
    const { user } = await renderPage(<SessionsPage />);

    await user.click(screen.getByRole("tab", { name: fr.sessions.failed }));

    expect(screen.getByText(fr.sessions.noSessions)).toBeTruthy();
    expect(screen.getByText("0 sessions")).toBeTruthy();
  });

  it("keeps the rows that match the search, by rider or by machine", async () => {
    serverLists(running, finished, failed);
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

describe("sessions list: page by page", () => {
  /** 60 finished sessions, newest first, as the server lists them. */
  const many = Array.from({ length: 60 }, (_, i) =>
    listed(String(i).padStart(3, "0"), { startedAt: NOW - (i + 1) * 60_000 }),
  );
  const names = (count: number) =>
    many.slice(0, count).map((s) => s.patientName);
  // Read straight from the table: role queries over hundreds of cells are
  // what made these tests slow.
  const riders = () =>
    Array.from(document.querySelectorAll("tbody tr td:first-child")).map(
      (cell) => cell.textContent,
    );
  const showMore = () => screen.queryByText(fr.common.showMore);

  it("shows the first 50 sessions, says there are more, and adds the next ones at 'Show more'", async () => {
    serverLists(...many);
    const { user } = await renderPage(<SessionsPage />);

    expect(riders()).toEqual(names(50));
    expect(screen.getByText("50+ sessions")).toBeTruthy();

    await user.click(showMore()!);
    expect(argsAsked(LIST).at(-1)).toEqual({
      paginationOpts: { numItems: 50, cursor: "50" },
    });
    expect(riders()).toEqual(names(60));
    expect(screen.getByText("60 sessions")).toBeTruthy();
    expect(showMore()).toBeNull();
  });

  it("keeps the sessions shown and offers nothing more while the next page loads", async () => {
    // The server has answered the first page only.
    answer(LIST, (args) =>
      args.paginationOpts.cursor === null ? pagesOf(many)(args) : undefined,
    );
    const { user } = await renderPage(<SessionsPage />);

    await user.click(showMore()!);

    expect(riders()).toHaveLength(50);
    const waitingButton = screen.getByText(fr.common.loading);
    expect((waitingButton as HTMLButtonElement).disabled).toBe(true);
    expect(showMore()).toBeNull();
  });

  it("starts again from the first page of a tab when the tab changes", async () => {
    serverListsByStatus(...many);
    const { user } = await renderPage(<SessionsPage />);
    await user.click(showMore()!);
    expect(riders()).toHaveLength(60);

    await user.click(screen.getByText(fr.sessions.completed));

    expect(argsAsked(LIST).at(-1)).toEqual(firstPage({ status: "completed" }));
    expect(riders()).toEqual(names(50));
    expect(screen.getByText("50+ sessions")).toBeTruthy();
  });

  it("says there is no session only once the list is done, not on an empty page with more to come", async () => {
    // The server cut its first page short: nothing in it, older ones remain.
    answer(LIST, (args) =>
      args.paginationOpts.cursor === null
        ? { page: [], isDone: false, continueCursor: "0" }
        : pagesOf([finished])(args),
    );
    const { user } = await renderPage(<SessionsPage />);

    expect(screen.queryByText(fr.sessions.noSessions)).toBeNull();
    expect(screen.getByText("0+ sessions")).toBeTruthy();

    await user.click(showMore()!);

    expect(riders()).toEqual(["Patient B"]);
    expect(screen.getByText("1 sessions")).toBeTruthy();
    expect(showMore()).toBeNull();
  });
});

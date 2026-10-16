// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
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
  pagesOf,
  renderPage,
  signedInAs,
} from "@/test-support/pages";
import ReportsPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);

/**
 * The completed sessions the visitor may read, each leading to its detail.
 * The server lists them page by page, newest start first (ANH-158).
 */

const LIST = api.sessions.listSessionsPaginated;
type Completed = FunctionReturnType<typeof LIST>["page"][number];

const STARTED = NOW - 86_400_000;

function completed(id: string, overrides: Partial<Completed> = {}): Completed {
  return {
    _id: id as Id<"sessions">,
    status: "completed",
    startedAt: STARTED,
    endedAt: STARTED + 30 * 60_000,
    channels: [],
    patientName: "Rose Rider",
    machineName: "Centri Paris",
    kind: "auto",
    origin: "remote",
    ...overrides,
  };
}

/** The server lists these completed sessions. */
function serverLists(...sessions: Completed[]) {
  answer(LIST, pagesOf(sessions));
}

/** The card of the report whose title ends with `reference`. */
function report(reference: string) {
  const title = screen.getByText(`${fr.reports.sessionReport} - ${reference}`);
  return within(title.closest("[data-slot=card]") as HTMLElement);
}

describe("reports", () => {
  it("asks for the 50 newest completed sessions of the visitor, and shows nothing until they and the visitor are known", async () => {
    signedInAs("user");
    answer(LIST, undefined);
    const view = await renderPage(<ReportsPage />);
    expect(lastArgsAsked(LIST)).toEqual({
      status: "completed",
      paginationOpts: { numItems: 50, cursor: null },
    });
    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.queryByText(fr.reports.noReports)).toBeNull();

    serverLists();
    answer(api.users.getCurrentUser, undefined);
    await view.refresh();
    expect(screen.queryByRole("heading")).toBeNull();

    signedInAs("user");
    await view.refresh();
    expect(
      screen.getByRole("heading", { name: fr.reports.title }),
    ).toBeTruthy();
  });

  it("says so when there is no report", async () => {
    signedInAs("user");
    serverLists();
    await renderPage(<ReportsPage />);

    expect(screen.getByText(fr.reports.noReports)).toBeTruthy();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("throws a failed page during rendering instead of showing an empty report list", async () => {
    signedInAs("user");
    const failure = new Error("Test report page failed");
    answer(LIST, failure);

    await expect(renderPage(<ReportsPage />)).rejects.toBe(failure);

    expect(screen.queryByText(fr.reports.noReports)).toBeNull();
    expect(screen.queryByText(fr.reports.nothingYet)).toBeNull();
  });

  it("shows each session with a reference, its rider, its machine, its date and its length", async () => {
    signedInAs("gestionnaire");
    serverLists(
      completed("k57abcd1234efgh5678"),
      completed("k57zzzz0000yyyy1111", {
        patientName: "Remi Martin",
        machineName: "Centri Lyon",
        endedAt: STARTED + 44 * 60_000 + 40_000,
      }),
    );
    await renderPage(<ReportsPage />);

    const first = report("EFGH5678");
    expect(first.getByText("Rose Rider")).toBeTruthy();
    expect(first.getByText("Centri Paris")).toBeTruthy();
    expect(
      first.getByText(new Date(STARTED).toLocaleDateString("fr")),
    ).toBeTruthy();
    expect(first.getByText("30 min")).toBeTruthy();
    const second = report("YYYY1111");
    expect(second.getByText("Remi Martin")).toBeTruthy();
    expect(second.getByText("Centri Lyon")).toBeTruthy();
    // 44 min 40 s is shown as the nearest minute.
    expect(second.getByText("45 min")).toBeTruthy();
    expect(screen.queryByText(fr.reports.noReports)).toBeNull();
  });

  it("leads from each report to the detail of its session and to its printable report", async () => {
    signedInAs("user");
    serverLists(completed("k57abcd1234efgh5678"));
    await renderPage(<ReportsPage />);

    const [detail, printable] = report("EFGH5678").getAllByRole("link");
    expect(detail.getAttribute("href")).toBe(
      "/dashboard/sessions/k57abcd1234efgh5678",
    );
    expect(detail.textContent).toBe(fr.reports.view);
    expect(printable.getAttribute("href")).toBe(
      "/dashboard/sessions/k57abcd1234efgh5678/report",
    );
    expect(printable.textContent).toBe(fr.sessionReport.openReport);
  });

  it.each([
    ["has no end", { endedAt: undefined }],
    ["lasted less than half a minute", { endedAt: STARTED + 29_000 }],
  ])(
    "shows a dash for the length of a session that %s",
    async (_name, overrides) => {
      signedInAs("user");
      serverLists(completed("k57abcd1234efgh5678", overrides));
      await renderPage(<ReportsPage />);

      expect(report("EFGH5678").getByText("-")).toBeTruthy();
      expect(report("EFGH5678").queryByText(/min$/)).toBeNull();
    },
  );

  it("writes the date as the visitor's language does", async () => {
    signedInAs("user");
    serverLists(completed("k57abcd1234efgh5678"));
    await renderPage(<ReportsPage />, { locale: "en" });

    const card = within(
      screen
        .getByText(`${en.reports.sessionReport} - EFGH5678`)
        .closest("[data-slot=card]") as HTMLElement,
    );
    expect(
      card.getByText(new Date(STARTED).toLocaleDateString("en")),
    ).toBeTruthy();
    expect(new Date(STARTED).toLocaleDateString("en")).not.toBe(
      new Date(STARTED).toLocaleDateString("fr"),
    );
  });

  // What the page does today: it shows whatever the server lists, to every
  // role alike. The server decides whose sessions those are.
  it.each(["admin", "org_admin", "gestionnaire", "user"] as const)(
    "shows a %s the sessions the server lists for them",
    async (role) => {
      signedInAs(role);
      serverLists(completed("k57abcd1234efgh5678"));
      await renderPage(<ReportsPage />);

      expect(report("EFGH5678").getByText("Rose Rider")).toBeTruthy();
    },
  );
});

describe("reports: page by page", () => {
  /** 70 completed sessions, newest first, as the server lists them. */
  const many = Array.from({ length: 70 }, (_, i) =>
    completed(`k57report${String(i).padStart(10, "0")}`, {
      startedAt: STARTED - i * 60_000,
    }),
  );
  const cards = () =>
    screen.getAllByText(fr.reports.view, { selector: "button" });

  it("shows the first 50 reports, then the next ones at 'Show more'", async () => {
    signedInAs("gestionnaire");
    serverLists(...many);
    const { user } = await renderPage(<ReportsPage />);
    expect(cards()).toHaveLength(50);

    await user.click(
      screen.getByText(fr.common.showMore, { selector: "button" }),
    );

    expect(argsAsked(LIST).at(-1)).toEqual({
      status: "completed",
      paginationOpts: { numItems: 50, cursor: "50" },
    });
    expect(cards()).toHaveLength(70);
    expect(
      screen.queryByText(fr.common.showMore, { selector: "button" }),
    ).toBeNull();
  });

  it("keeps the reports shown and offers nothing more while the next page loads", async () => {
    signedInAs("user");
    answer(LIST, (args) =>
      args.paginationOpts.cursor === null
        ? { page: many.slice(0, 2), isDone: false, continueCursor: "2" }
        : undefined,
    );
    const { user } = await renderPage(<ReportsPage />);

    await user.click(
      screen.getByText(fr.common.showMore, { selector: "button" }),
    );

    expect(cards()).toHaveLength(2);
    expect(report("00000000").getByText("Rose Rider")).toBeTruthy();
    expect(report("00000001").getByText("Rose Rider")).toBeTruthy();
    const waiting = screen.getByText(fr.common.loading, { selector: "button" });
    expect((waiting as HTMLButtonElement).disabled).toBe(true);
  });

  it("says there is no report only once the list is done, not on an empty page with more to come", async () => {
    signedInAs("user");
    // The server cut its first page short: nothing in it, older ones remain.
    answer(LIST, (args) =>
      args.paginationOpts.cursor === null
        ? { page: [], isDone: false, continueCursor: "0" }
        : pagesOf(many.slice(0, 1))(args),
    );
    const { user } = await renderPage(<ReportsPage />);

    expect(screen.queryByText(fr.reports.noReports)).toBeNull();
    await user.click(
      screen.getByText(fr.common.showMore, { selector: "button" }),
    );

    expect(cards()).toHaveLength(1);
    expect(screen.queryByText(fr.reports.noReports)).toBeNull();
  });

  it("offers no 'Show more' when every report is shown", async () => {
    signedInAs("user");
    serverLists(...many.slice(0, 2));
    await renderPage(<ReportsPage />);

    expect(cards()).toHaveLength(2);
    expect(
      screen.queryByText(fr.common.showMore, { selector: "button" }),
    ).toBeNull();
  });
});

describe.each([
  ["fr", fr],
  ["en", en],
] as const)("reports: empty pages (%s, T02-f)", (locale, messages) => {
  it("T02-e explains an empty unfinished page and keeps the message while reading more, until the list is done", async () => {
    signedInAs("gestionnaire");
    answer(LIST, (args) =>
      args.paginationOpts.cursor === null
        ? { page: [], isDone: false, continueCursor: "0" }
        : undefined,
    );
    const view = await renderPage(<ReportsPage />, { locale });
    expect(screen.getByRole("status").textContent).toBe(
      messages.reports.nothingYet,
    );
    expect(screen.queryByText(messages.reports.noReports)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();

    await view.user.click(
      screen.getByRole("button", { name: messages.common.showMore }),
    );
    expect(screen.getByText(messages.reports.nothingYet)).toBeTruthy();
    expect(screen.queryByText(messages.reports.noReports)).toBeNull();
    expect(
      (
        screen.getByRole("button", {
          name: messages.common.loading,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);

    serverLists();
    await view.refresh();
    expect(screen.getByText(messages.reports.noReports)).toBeTruthy();
    expect(screen.queryByText(messages.reports.nothingYet)).toBeNull();
    expect(
      screen.queryByRole("button", { name: messages.common.showMore }),
    ).toBeNull();
  });
});

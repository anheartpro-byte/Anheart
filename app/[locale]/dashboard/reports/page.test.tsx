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
  lastArgsAsked,
  NOW,
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

/** The completed sessions the visitor may read, each leading to its detail. */

type Completed = FunctionReturnType<
  typeof api.sessions.getCompletedSessionsForUser
>[number];

const STARTED = NOW - 86_400_000;

function completed(id: string, overrides: Partial<Completed> = {}): Completed {
  return {
    _id: id as Id<"sessions">,
    status: "completed",
    startedAt: STARTED,
    endedAt: STARTED + 30 * 60_000,
    channels: [],
    notes: undefined,
    patientName: "Rose Rider",
    machineName: "Centri Paris",
    ...overrides,
  };
}

/** The card of the report whose title ends with `reference`. */
function report(reference: string) {
  const title = screen.getByText(`${fr.reports.sessionReport} - ${reference}`);
  return within(title.closest("[data-slot=card]") as HTMLElement);
}

describe("reports", () => {
  it("asks for the completed sessions of the visitor, and shows nothing until they and the visitor are known", async () => {
    signedInAs("user");
    answer(api.sessions.getCompletedSessionsForUser, undefined);
    const view = await renderPage(<ReportsPage />);
    expect(lastArgsAsked(api.sessions.getCompletedSessionsForUser)).toEqual({});
    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.queryByText(fr.reports.noReports)).toBeNull();

    answer(api.sessions.getCompletedSessionsForUser, []);
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
    answer(api.sessions.getCompletedSessionsForUser, []);
    await renderPage(<ReportsPage />);

    expect(screen.getByText(fr.reports.noReports)).toBeTruthy();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("shows each session with a reference, its rider, its machine, its date and its length", async () => {
    signedInAs("gestionnaire");
    answer(api.sessions.getCompletedSessionsForUser, [
      completed("k57abcd1234efgh5678"),
      completed("k57zzzz0000yyyy1111", {
        patientName: "Remi Martin",
        machineName: "Centri Lyon",
        endedAt: STARTED + 44 * 60_000 + 40_000,
      }),
    ]);
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

  it("leads from each report to the detail of its session", async () => {
    signedInAs("user");
    answer(api.sessions.getCompletedSessionsForUser, [
      completed("k57abcd1234efgh5678"),
    ]);
    await renderPage(<ReportsPage />);

    const link = report("EFGH5678").getByRole("link");
    expect(link.getAttribute("href")).toBe(
      "/dashboard/sessions/k57abcd1234efgh5678",
    );
    expect(link.textContent).toBe(fr.reports.view);
  });

  it.each([
    ["has no end", { endedAt: undefined }],
    ["lasted less than half a minute", { endedAt: STARTED + 29_000 }],
  ])(
    "shows a dash for the length of a session that %s",
    async (_name, overrides) => {
      signedInAs("user");
      answer(api.sessions.getCompletedSessionsForUser, [
        completed("k57abcd1234efgh5678", overrides),
      ]);
      await renderPage(<ReportsPage />);

      expect(report("EFGH5678").getByText("-")).toBeTruthy();
      expect(report("EFGH5678").queryByText(/min$/)).toBeNull();
    },
  );

  it("writes the date as the visitor's language does", async () => {
    signedInAs("user");
    answer(api.sessions.getCompletedSessionsForUser, [
      completed("k57abcd1234efgh5678"),
    ]);
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
      answer(api.sessions.getCompletedSessionsForUser, [
        completed("k57abcd1234efgh5678"),
      ]);
      await renderPage(<ReportsPage />);

      expect(report("EFGH5678").getByText("Rose Rider")).toBeTruthy();
    },
  );
});

// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import fr from "@/messages/fr.json";
import en from "@/messages/en.json";
import { answer, NOW, renderPage, signedInAs } from "@/test-support/pages";
import {
  incidentsCsv,
  type IncidentRow,
} from "@/components/incidents/incidentsCsv";
import IncidentsPage from "./page";

const download = vi.hoisted(() => ({ save: vi.fn() }));
vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);
vi.mock("@/lib/download", () => ({ save: download.save }));

/**
 * ANH-145 EX-5 and EX-6: the incidents of the machines the visitor reaches,
 * which the server chooses (convex/incidents.test.ts and the matrix), and the
 * CSV export of the Anheart admin.
 */

const t = fr.incidents;

function incident(overrides: Partial<IncidentRow> = {}): IncidentRow {
  return {
    _id: "incident-1" as Id<"incidents">,
    _creationTime: NOW - 60_000,
    organizationId: "org-centre-a" as Id<"organizations">,
    machineId: "machine-1" as Id<"machines">,
    machineName: "Centri Paris",
    sessionId: "session-1" as Id<"sessions">,
    kind: "safety_verdict",
    rule: "hr_critical",
    t: NOW - 61_000,
    severity: "high",
    occupied: true,
    status: "open",
    openedAt: NOW - 60_000,
    ...overrides,
  };
}

describe("the incidents page", () => {
  it.each(["user"] as const)("refuses a %s", async (role) => {
    signedInAs(role);
    await renderPage(<IncidentsPage />);
    expect(screen.getByText(t.accessDenied)).toBeTruthy();
  });

  it("says when there is none", async () => {
    signedInAs("gestionnaire");
    answer(api.incidents.list, []);
    await renderPage(<IncidentsPage />);
    expect(screen.getByText(t.empty)).toBeTruthy();
    expect(screen.queryByRole("button", { name: t.export })).toBeNull();
  });

  it("lists each incident with its kind, rule, severity, status and a link to its sheet", async () => {
    signedInAs("org_admin");
    answer(api.incidents.list, [
      incident(),
      incident({
        _id: "incident-2" as Id<"incidents">,
        kind: "drive_fault",
        rule: "OHF",
        severity: "medium",
        status: "closed",
        occupied: false,
      }),
    ]);
    await renderPage(<IncidentsPage />);
    const [, first, second] = screen.getAllByRole("row");
    expect(within(first).getByText(t.kinds.safety_verdict)).toBeTruthy();
    expect(within(first).getByText("hr_critical")).toBeTruthy();
    expect(within(first).getByText(t.severities.high)).toBeTruthy();
    expect(within(first).getByText(t.statuses.open)).toBeTruthy();
    expect(within(first).getByText(t.yes)).toBeTruthy();
    expect(
      within(first).getByRole("link", { name: t.open }).getAttribute("href"),
    ).toBe("/dashboard/incidents/incident-1");
    expect(within(second).getByText(t.kinds.drive_fault)).toBeTruthy();
    expect(within(second).getByText(t.statuses.closed)).toBeTruthy();
    expect(within(second).getByText(t.no)).toBeTruthy();
    // Only the Anheart admin exports.
    expect(screen.queryByRole("button", { name: t.export })).toBeNull();
  });

  it("lets the Anheart admin export what is listed as CSV", async () => {
    signedInAs("admin");
    const rows = [incident()];
    answer(api.incidents.list, rows);
    const view = await renderPage(<IncidentsPage />, { locale: "en" });
    await view.user.click(
      screen.getByRole("button", { name: en.incidents.export }),
    );
    expect(download.save).toHaveBeenCalledTimes(1);
    const [blob, name] = download.save.mock.calls[0] as [Blob, string];
    expect(name).toMatch(/^incidents-\d{4}-\d{2}-\d{2}\.csv$/);
    expect(await blob.text()).toBe(incidentsCsv(rows).replace(/^﻿/, ""));
  });

  it("says when the list is cut", async () => {
    signedInAs("gestionnaire");
    answer(
      api.incidents.list,
      Array.from({ length: 1000 }, (_, index) =>
        incident({ _id: `incident-${index}` as Id<"incidents"> }),
      ),
    );
    await renderPage(<IncidentsPage />);
    expect(
      screen.getByText(t.listCut.replace("{count}", "1000")),
    ).toBeTruthy();
  });
});

describe("the CSV of the incidents", () => {
  it("holds a header and one row per incident, dates in ISO UTC, no cell read as a formula", () => {
    const csv = incidentsCsv([
      incident({
        analysis: '=HYPERLINK("x")',
        action: "electrodes, changees",
        closedAt: Date.UTC(2026, 10, 13, 9, 0),
        closedBy: "user-admin" as Id<"users">,
      }),
    ]);
    expect(csv.startsWith("﻿")).toBe(true);
    const [header, row, end] = csv.slice(1).split("\r\n");
    expect(header.split(",")).toContain("identifiant_incident");
    expect(header.split(",")).toContain("gravite");
    expect(row).toContain("incident-1");
    expect(row).toContain(new Date(NOW - 60_000).toISOString());
    expect(row).toContain("2026-11-13T09:00:00.000Z");
    expect(row).toContain(`"'=HYPERLINK(""x"")"`);
    expect(row).toContain('"electrodes, changees"');
    expect(row).toContain(",true,");
    expect(end).toBe("");
  });
});

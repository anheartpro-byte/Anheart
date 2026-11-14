// @vitest-environment jsdom
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
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
  renderPage,
  routeParams,
  takeConsoleErrors,
} from "@/test-support/pages";
import MachineLogbookPrintPage from "@/app/[locale]/dashboard/machines/[id]/logbook/page";
import { logbookCsv, LOGBOOK_CSV_COLUMNS } from "./logbookCsv";
import { MachineLogbookCard } from "./MachineLogbookCard";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);

/**
 * ANH-149: a machine's logbook on its page, its form, its export, and the
 * print view for a PDF. The server decides who reads and writes it
 * (convex/machineLogbook.ts); the page shows what it is given.
 */
type Book = NonNullable<FunctionReturnType<typeof api.machineLogbook.list>>;

const machineId = "machine-1" as Id<"machines">;
const incidentId = "incident-9" as Id<"incidents">;
const t = fr.machineLogbook;

function book(overrides: Partial<Book> = {}): Book {
  return {
    entries: [
      {
        _id: "entry-2" as Id<"machine_logbook">,
        at: NOW - 1000,
        kind: "intervention",
        summary: "cable moteur remplace",
        details: "isolement mesure a 500 V",
        by: "user-admin" as Id<"users">,
        byName: "Alice Martin",
        refs: { incidentId, storageId: "storage-1" as Id<"_storage"> },
        attachmentUrl: "https://files.example/rapport.pdf",
      },
      {
        _id: "entry-1" as Id<"machine_logbook">,
        at: NOW - 5000,
        kind: "software_update",
        summary: "logiciel pi-0.1.0 -> pi-0.2.0",
        details: undefined,
        by: undefined,
        byName: null,
        refs: { softwareVersion: "pi-0.2.0" },
        attachmentUrl: null,
      },
    ],
    interventionsDue: [],
    writable: ["intervention", "calibration", "part_replaced", "note"],
    ...overrides,
  };
}

async function card(given: Book | null | undefined, exportable = true) {
  answer(api.machineLogbook.list, given);
  return renderPage(<MachineLogbookCard machineId={machineId} exportable={exportable} />);
}

describe("the logbook card", () => {
  it("shows nothing while it loads, nor to whoever the server answers nothing to", async () => {
    const loading = await card(undefined);
    expect(lastArgsAsked(api.machineLogbook.list)).toEqual({ machineId });
    expect(loading.container.textContent).toBe("");
    loading.unmount();
    expect((await card(null)).container.textContent).toBe("");
  });

  it("shows each entry with its type, author, incident and attachment, most recent first", async () => {
    await card(book());
    const items = screen.getAllByRole("listitem");
    expect(within(items[0]).getByText("cable moteur remplace")).toBeTruthy();
    expect(within(items[0]).getByText(t.kinds.intervention)).toBeTruthy();
    expect(within(items[0]).getByText("Alice Martin")).toBeTruthy();
    expect(within(items[0]).getByText("Incident incident-9")).toBeTruthy();
    expect(within(items[0]).getByText(t.attachment).closest("a")?.getAttribute("href")).toBe(
      "https://files.example/rapport.pdf",
    );
    expect(within(items[1]).getByText(t.byServer)).toBeTruthy();
    expect(screen.queryByText(t.dueTitle)).toBeNull();
  });

  it("says when programmed sessions are closed, and offers the incident to the intervention", async () => {
    await card(book({ interventionsDue: [incidentId] }));
    expect(screen.getByText(t.dueTitle)).toBeTruthy();
    expect(screen.getByText(t.incidentLabel)).toBeTruthy();
  });

  it("offers no form to whoever may write nothing, and no export but to an Anheart admin", async () => {
    await card(book({ writable: [] }), false);
    expect(screen.queryByText(t.add)).toBeNull();
    expect(screen.queryByText(t.exportCsv)).toBeNull();
    expect(screen.queryByText(t.print)).toBeNull();
    expect(screen.getByText("cable moteur remplace")).toBeTruthy();
  });

  it("writes an entry of the first kind the caller may write, then clears the form", async () => {
    mutation(api.machineLogbook.addEntry).mockResolvedValue("entry-3");
    await card(book({ writable: ["note"] }));
    fireEvent.change(screen.getByLabelText(t.summaryLabel), {
      target: { value: "machine deplacee" },
    });
    fireEvent.click(screen.getByRole("button", { name: t.add }));
    await waitFor(() =>
      expect(mutationsSent()).toEqual([
        {
          name: "machineLogbook:addEntry",
          args: {
            machineId,
            kind: "note",
            summary: "machine deplacee",
            details: undefined,
            incidentId: undefined,
            storageId: undefined,
          },
        },
      ]),
    );
    await waitFor(() => expect(feedbackShown()).toContainEqual({ kind: "success", message: t.added }));
    expect((screen.getByLabelText(t.summaryLabel) as HTMLInputElement).value).toBe("");
  });

  it("refuses an attachment of another type before sending anything", async () => {
    await card(book({ writable: ["note"] }));
    fireEvent.change(screen.getByLabelText(t.summaryLabel), { target: { value: "photo du banc" } });
    fireEvent.change(screen.getByLabelText(t.fileLabel), {
      target: { files: [new File(["x"], "notes.txt", { type: "text/plain" })] },
    });
    fireEvent.click(screen.getByRole("button", { name: t.add }));
    await waitFor(() => expect(feedbackShown()).toHaveLength(1));
    expect(feedbackShown()[0]).toMatchObject({ kind: "error", message: fr.errors.invalid_logbook_file });
    expect(mutationsSent()).toEqual([]);
    expect(takeConsoleErrors()).toEqual([
      expect.stringMatching(/^\[logbook upload\] failed/),
    ]);
  });
});

describe("the logbook export", () => {
  it("writes identifiers only, oldest first, one column per field", () => {
    const csv = logbookCsv(book().entries);
    const lines = csv.replace(/^﻿/, "").trimEnd().split("\r\n");
    expect(lines[0]).toBe(LOGBOOK_CSV_COLUMNS.map(([header]) => header).join(","));
    expect(lines[1]).toContain("software_update");
    expect(lines[2]).toContain("user-admin");
    expect(csv).not.toContain("Alice Martin");
    expect(csv.startsWith("﻿")).toBe(true);
  });

  it("prints for an Anheart admin, and for nobody else", async () => {
    answer(api.machineLogbook.list, book());
    answer(api.users.getCurrentUser, { role: "admin" } as never);
    const printed = await renderPage(<MachineLogbookPrintPage params={routeParams({ id: machineId })} />);
    expect(screen.getByText(t.printTitle)).toBeTruthy();
    expect(screen.getByText("user-admin")).toBeTruthy();
    expect(screen.queryByText("Alice Martin")).toBeNull();
    printed.unmount();
    answer(api.users.getCurrentUser, { role: "org_admin" } as never);
    await renderPage(<MachineLogbookPrintPage params={routeParams({ id: machineId })} />);
    expect(screen.getByText(t.printNotAllowed)).toBeTruthy();
  });
});

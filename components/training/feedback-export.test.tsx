// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { format } from "date-fns";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import fr from "@/messages/fr.json";
import { NOW, renderPage } from "@/test-support/pages";
import {
  FeedbackExportButton,
  FEEDBACK_EXPORT_MAX_ROWS,
} from "./FeedbackExportButton";
import {
  FEEDBACK_CSV_COLUMNS,
  feedbackCsv,
  type FeedbackExportRow,
} from "./feedbackCsv";

const client = vi.hoisted(() => ({ query: vi.fn(), save: vi.fn() }));
vi.mock("convex/react", async () => ({
  ...(await import("@/test-support/pages")).convexReact,
  useConvex: () => client,
}));
vi.mock("@/lib/download", () => ({ save: client.save }));

/**
 * ANH-146 S7: the export of the riders' feedback, for medical analysis. Every
 * page of the server is read, the file holds identifiers and figures only,
 * and no cell can be read as a formula.
 */

const t = fr.reports;
const SECRET_NAME = "Synthetic Rider Name";
const SECRET_TEXT = "synthetic-private-feedback-sentinel";

const row: FeedbackExportRow = {
  sessionId: "session-1" as Id<"sessions">,
  organizationId: "org-a" as Id<"organizations">,
  machineId: "machine-1" as Id<"machines">,
  userId: "user-rose" as Id<"users">,
  startedAt: Date.UTC(2026, 9, 20, 8, 30),
  durationS: 1800,
  legTipMaxG: 2.25,
  legTipMaxGRate: 0.125,
  nauseaScore: 3,
  dizzinessScore: 9,
  comfort: "bad",
  submittedBy: "operator",
  submittedAt: Date.UTC(2026, 9, 20, 9, 5, 7, 250),
};

/** What a server that let a name or a comment slip would send: never written. */
const leaking = {
  ...row,
  patientName: SECRET_NAME,
  email: "rose@example.test",
  freeText: SECRET_TEXT,
} as FeedbackExportRow;

function text(blob: Blob): Promise<string> {
  const reader = new FileReader();
  const read = new Promise<string>((resolve, reject) => {
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = reject;
  });
  reader.readAsText(blob);
  return read;
}

beforeEach(() => {
  client.query
    .mockReset()
    .mockResolvedValue({ page: [], isDone: true, continueCursor: "" });
  client.save.mockReset();
});

describe("ANH-146 S7 the CSV of the riders' feedback", () => {
  it("has a French header per field of the export, and one line per row", () => {
    const csv = feedbackCsv([row]);
    expect(csv.startsWith("﻿")).toBe(true);
    expect(csv.endsWith("\r\n")).toBe(true);
    const [header, line, end] = csv.slice(1).split("\r\n");
    expect(header).toBe(
      "identifiant_seance,identifiant_organisation,identifiant_machine,identifiant_passager,debut_utc,duree_s,pic_g_pointe_pied,pic_variation_g_pointe_pied_g_par_s,nausee_0_10,malaise_vertige_0_10,ressenti,saisi_par,saisi_le_utc",
    );
    expect(line).toBe(
      "session-1,org-a,machine-1,user-rose,2026-10-20T08:30:00.000Z,1800,2.25,0.125,3,9,bad,operator,2026-10-20T09:05:07.250Z",
    );
    expect(end).toBe("");
    // Every field the server exports has its column, and only those.
    expect(FEEDBACK_CSV_COLUMNS.map(([, field]) => field).sort()).toEqual(
      Object.keys(row).sort(),
    );
  });

  it("leaves a missing figure or rider empty", () => {
    const line = feedbackCsv([
      {
        ...row,
        userId: null,
        organizationId: null,
        durationS: null,
        legTipMaxG: null,
        legTipMaxGRate: null,
      },
    ]).split("\r\n")[1];
    expect(line).toBe(
      "session-1,,machine-1,,2026-10-20T08:30:00.000Z,,,,3,9,bad,operator,2026-10-20T09:05:07.250Z",
    );
  });

  it("writes no name, address or comment, even if the server sent one", () => {
    const csv = feedbackCsv([leaking]);
    for (const secret of [SECRET_NAME, SECRET_TEXT, "rose@example.test"])
      expect(csv).not.toContain(secret);
  });

  it("neutralises a cell that a spreadsheet would read as a formula, and quotes what must be", () => {
    const line = feedbackCsv([
      {
        ...row,
        machineId: "=HYPERLINK(1)" as Id<"machines">,
        sessionId: '+a,"b"' as Id<"sessions">,
        userId: "@x" as Id<"users">,
      },
    ]).split("\r\n")[1];
    expect(line.startsWith(`"'+a,""b""",org-a,'=HYPERLINK(1),'@x,`)).toBe(true);
  });
});

describe("ANH-146 S7 the export button", () => {
  it("reads every page of the server, then downloads a file named by the day", async () => {
    client.query
      .mockResolvedValueOnce({
        page: [row],
        isDone: false,
        continueCursor: "c1",
      })
      .mockResolvedValueOnce({
        page: [{ ...row, sessionId: "session-2" as Id<"sessions"> }],
        isDone: false,
        continueCursor: "c2",
      })
      .mockResolvedValueOnce({
        page: [leaking],
        isDone: true,
        continueCursor: "",
      });
    const view = await renderPage(<FeedbackExportButton />);
    await view.user.click(
      screen.getByRole("button", { name: t.exportFeedback }),
    );

    expect(client.query.mock.calls).toEqual([
      [
        api.sessionFeedback.exportFeedback,
        { paginationOpts: { cursor: null, numItems: 100 } },
      ],
      [
        api.sessionFeedback.exportFeedback,
        { paginationOpts: { cursor: "c1", numItems: 100 } },
      ],
      [
        api.sessionFeedback.exportFeedback,
        { paginationOpts: { cursor: "c2", numItems: 100 } },
      ],
    ]);
    expect(client.save).toHaveBeenCalledTimes(1);
    const [blob, name] = client.save.mock.calls[0] as [Blob, string];
    expect(name).toBe(`retours-passagers-${format(NOW, "yyyy-MM-dd")}.csv`);
    expect(blob.type).toBe("text/csv;charset=utf-8");
    const csv = await text(blob);
    expect(csv).toBe(
      feedbackCsv([
        row,
        { ...row, sessionId: "session-2" as Id<"sessions"> },
        leaking,
      ]).slice(1),
    );
    for (const secret of [SECRET_NAME, SECRET_TEXT])
      expect(csv).not.toContain(secret);
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByText(t.exportFeedbackHint)).toBeTruthy();
  });

  it(`stops at ${FEEDBACK_EXPORT_MAX_ROWS} rows and says the oldest are missing`, async () => {
    client.query.mockResolvedValue({
      page: Array.from({ length: 100 }, () => row),
      isDone: false,
      continueCursor: "next",
    });
    const view = await renderPage(<FeedbackExportButton />);
    await view.user.click(
      screen.getByRole("button", { name: t.exportFeedback }),
    );

    expect(client.query).toHaveBeenCalledTimes(FEEDBACK_EXPORT_MAX_ROWS / 100);
    expect(screen.getByRole("status").textContent).toBe(
      t.exportFeedbackTruncated,
    );
    const csv = await text(client.save.mock.calls[0][0] as Blob);
    expect(csv.split("\r\n")).toHaveLength(FEEDBACK_EXPORT_MAX_ROWS + 2);
  });

  it("says the export failed, downloads nothing, and lets the admin try again", async () => {
    client.query.mockRejectedValueOnce(new Error("not_authorized"));
    const view = await renderPage(<FeedbackExportButton />);
    await view.user.click(
      screen.getByRole("button", { name: t.exportFeedback }),
    );

    expect(screen.getByRole("status").textContent).toBe(t.exportFeedbackError);
    expect(client.save).not.toHaveBeenCalled();
    await view.user.click(
      screen.getByRole("button", { name: t.exportFeedback }),
    );
    expect(client.save).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("is disabled while the pages arrive", async () => {
    let finish: (value: unknown) => void = () => {};
    client.query.mockReturnValueOnce(
      new Promise((resolve) => (finish = resolve)),
    );
    const view = await renderPage(<FeedbackExportButton />);
    const click = view.user.click(
      screen.getByRole("button", { name: t.exportFeedback }),
    );
    await vi.waitFor(() =>
      expect(
        (
          screen.getByRole("button", {
            name: t.exportingFeedback,
          }) as HTMLButtonElement
        ).disabled,
      ).toBe(true),
    );
    finish({ page: [], isDone: true, continueCursor: "" });
    await click;
    await vi.waitFor(() => expect(client.save).toHaveBeenCalledTimes(1));
  });
});

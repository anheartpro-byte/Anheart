"use client";

import { useState } from "react";
import { useConvex } from "convex/react";
import type { FunctionReturnType } from "convex/server";
import { useTranslations } from "next-intl";
import { format } from "date-fns";
import { FileDown } from "lucide-react";
import { api } from "@/convex/_generated/api";
import { save } from "@/lib/download";
import { Button } from "@/components/ui/button";
import { feedbackCsv, type FeedbackExportRow } from "./feedbackCsv";

/** Rows an export holds at most, as the audit export. */
export const FEEDBACK_EXPORT_MAX_ROWS = 10_000;
/** Rows asked per page: the most the server serves (`EXPORT_PAGE_MAX`). */
const PAGE_SIZE = 100;

/**
 * Downloads the riders' feedback of every session the admin may read (their
 * organisation, every organisation for the Anheart admin), page by page, as
 * a CSV file (ANH-146). The page shows the button to admins only; the server
 * refuses anyone else. The file name carries the day, nothing personal.
 */
export function FeedbackExportButton() {
  const t = useTranslations("reports");
  const convex = useConvex();
  const [exporting, setExporting] = useState(false);
  const [notice, setNotice] = useState<"truncated" | "error" | null>(null);

  async function exportCsv() {
    setExporting(true);
    setNotice(null);
    try {
      const rows: FeedbackExportRow[] = [];
      let cursor: string | null = null;
      let isDone = false;
      do {
        const page: FunctionReturnType<
          typeof api.sessionFeedback.exportFeedback
        > = await convex.query(api.sessionFeedback.exportFeedback, {
          paginationOpts: { cursor, numItems: PAGE_SIZE },
        });
        rows.push(
          ...page.page.slice(0, FEEDBACK_EXPORT_MAX_ROWS - rows.length),
        );
        cursor = page.continueCursor;
        isDone = page.isDone;
      } while (!isDone && rows.length < FEEDBACK_EXPORT_MAX_ROWS);
      save(
        new Blob([feedbackCsv(rows)], { type: "text/csv;charset=utf-8" }),
        `retours-passagers-${format(new Date(), "yyyy-MM-dd")}.csv`,
      );
      if (!isDone) setNotice("truncated");
    } catch {
      // Nothing of the rows is said or logged: they are health data.
      setNotice("error");
    } finally {
      setExporting(false);
    }
  }

  return (
    <div className="space-y-2">
      <Button variant="outline" disabled={exporting} onClick={exportCsv}>
        <FileDown className="mr-2 h-4 w-4" />
        {t(exporting ? "exportingFeedback" : "exportFeedback")}
      </Button>
      <p className="text-xs text-muted-foreground">{t("exportFeedbackHint")}</p>
      {notice && (
        <p role="status" className="text-sm">
          {t(
            notice === "truncated"
              ? "exportFeedbackTruncated"
              : "exportFeedbackError",
          )}
        </p>
      )}
    </div>
  );
}

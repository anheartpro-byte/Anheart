"use client";

import { useState } from "react";
import { useConvex, usePaginatedQuery, useQuery } from "convex/react";
import type { FunctionArgs, FunctionReturnType } from "convex/server";
import { useTranslations } from "next-intl";
import { format } from "date-fns";
import { Shield } from "lucide-react";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { isOrganizationAdmin } from "@/lib/organizationAccess";
import { auditCsv, type AuditRow } from "@/lib/auditCsv";
import { save } from "@/lib/download";
import {
  AuditFilters,
  EMPTY_AUDIT_FILTERS,
  type AuditFilterState,
} from "@/components/audit/AuditFilters";
import { AuditTable } from "@/components/audit/AuditTable";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

function localDay(date: string, next = false) {
  const [year, month, day] = date.split("-").map(Number);
  return new Date(year, month - 1, day + (next ? 1 : 0)).getTime();
}

export default function AuditPage() {
  const t = useTranslations("audit");
  const user = useQuery(api.users.getCurrentUser);
  const allowed =
    isOrganizationAdmin(user?.role) && user?.organizationAccess === "granted";
  const admin = user?.role === "admin";
  const convex = useConvex();
  const [state, setState] = useState<AuditFilterState>(EMPTY_AUDIT_FILTERS);
  const [exporting, setExporting] = useState(false);
  const [notice, setNotice] = useState<"truncated" | "error" | null>(null);
  const filters: Omit<FunctionArgs<typeof api.audit.list>, "paginationOpts"> = {
    ...(state.actor === "system" || state.actor === "machine"
      ? { actorRole: state.actor }
      : state.actor
        ? { actorId: state.actor as Id<"users"> | Id<"machines"> }
        : {}),
    ...(state.action ? { action: state.action } : {}),
    ...(state.targetType ? { targetType: state.targetType } : {}),
    ...(state.targetId ? { targetId: state.targetId } : {}),
    ...(state.from ? { from: localDay(state.from) } : {}),
    ...(state.to ? { to: localDay(state.to, true) } : {}),
    ...(admin && state.organizationId
      ? { organizationId: state.organizationId as Id<"organizations"> }
      : {}),
  };
  const { results, status, loadMore } = usePaginatedQuery(
    api.audit.list,
    allowed ? filters : "skip",
    { initialNumItems: 50 },
  );

  async function exportCsv() {
    setExporting(true);
    setNotice(null);
    try {
      const rows: AuditRow[] = [];
      let cursor: string | null = null;
      let isDone = false;
      do {
        const page: FunctionReturnType<typeof api.audit.list> =
          await convex.query(api.audit.list, {
            ...filters,
            paginationOpts: { cursor, numItems: 200 },
          });
        rows.push(...page.page.slice(0, 10_000 - rows.length));
        cursor = page.continueCursor;
        isDone = page.isDone;
      } while (!isDone && rows.length < 10_000);
      save(
        new Blob([auditCsv(rows)], { type: "text/csv;charset=utf-8" }),
        `audit-${format(new Date(), "yyyy-MM-dd")}.csv`,
      );
      if (!isDone) setNotice("truncated");
    } catch {
      setNotice("error");
    } finally {
      setExporting(false);
    }
  }

  if (user === undefined) return <Skeleton className="h-96 w-full" />;
  if (!allowed)
    return (
      <div className="text-center py-12">
        <Shield className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
        <p className="text-muted-foreground">{t("accessDenied")}</p>
      </div>
    );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">{t("title")}</h1>
          <p className="text-muted-foreground">{t("description")}</p>
        </div>
        <Button disabled={exporting} onClick={exportCsv}>
          {t(exporting ? "exporting" : "export")}
        </Button>
      </div>
      {notice && (
        <p role="status">
          {t(notice === "truncated" ? "exportTruncated" : "exportError")}
        </p>
      )}
      <fieldset disabled={exporting}>
        <AuditFilters value={state} onChange={setState} admin={admin} />
      </fieldset>
      {status === "LoadingFirstPage" ? (
        <Skeleton className="h-64 w-full" />
      ) : results.length > 0 ? (
        <Card>
          <CardContent className="p-0">
            <AuditTable
              rows={results}
              admin={admin}
              disabled={exporting}
              onTarget={(row) =>
                setState({
                  ...state,
                  targetType: row.targetType,
                  targetId: row.targetId,
                  targetLabel: row.targetLabel ?? "",
                })
              }
            />
          </CardContent>
        </Card>
      ) : (
        status === "Exhausted" && (
          <p className="py-12 text-center text-muted-foreground">
            {t("empty")}
          </p>
        )
      )}
      {(status === "CanLoadMore" || status === "LoadingMore") && (
        <div className="flex justify-center">
          <Button
            variant="outline"
            disabled={status === "LoadingMore"}
            onClick={() => loadMore(50)}
          >
            {t(status === "LoadingMore" ? "loading" : "showMore")}
          </Button>
        </div>
      )}
    </div>
  );
}

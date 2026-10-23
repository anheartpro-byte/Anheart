"use client";

import { useState, useMemo } from "react";
import { usePaginatedQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations, useLocale } from "next-intl";
import { Link, useRouter } from "@/i18n/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Activity, Search, Eye, Radio } from "lucide-react";
import { format } from "date-fns";
import { fr, enUS } from "date-fns/locale";
import { SessionKindCell } from "@/components/training/SessionKindCell";
import { sessionDuration } from "@/lib/sessionDuration";
import { useSessionStatusLabel } from "@/components/dashboard/statusLabels";
import {
  useReactTable,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  flexRender,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";

type Session = {
  _id: Id<"sessions">;
  status: string;
  startedAt: number;
  endedAt?: number;
  channels: string[];
  patientName: string;
  machineName: string;
};

type StatusFilter =
  | "all"
  | "pending"
  | "active"
  | "interrupted"
  | "completed"
  | "failed";

/** Sessions asked for at once: the first page, then each "Show more". */
const PAGE_SIZE = 50;

export default function SessionsPage() {
  const t = useTranslations();
  const locale = useLocale();
  const router = useRouter();
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [globalFilter, setGlobalFilter] = useState("");
  const [sorting, setSorting] = useState<SortingState>([]);

  // The server lists the sessions of the tab, newest start first, page by
  // page: an older session of any status is reached with "Show more", never
  // cut off by the sessions of other tabs.
  const {
    results: sessions,
    status,
    loadMore,
  } = usePaginatedQuery(
    api.sessions.listSessionsPaginated,
    statusFilter === "all" ? {} : { status: statusFilter },
    { initialNumItems: PAGE_SIZE },
  );
  const loading = status === "LoadingFirstPage";
  const canGrow = status === "CanLoadMore" || status === "LoadingMore";
  const searching = globalFilter.trim().length > 0;

  const dateLocale = locale === "fr" ? fr : enUS;

  const columns = useMemo<ColumnDef<Session>[]>(
    () => [
      {
        accessorKey: "patientName",
        header: t("sessions.patient"),
        cell: ({ row }) => (
          <span className="font-medium">{row.original.patientName}</span>
        ),
      },
      {
        accessorKey: "machineName",
        header: t("sessions.machine"),
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {row.original.machineName}
          </span>
        ),
      },
      {
        id: "kind",
        header: t("training.session.kind"),
        cell: ({ row }) => <SessionKindCell row={row.original} />,
      },
      {
        accessorKey: "status",
        header: t("machines.status"),
        cell: ({ row }) => <SessionStatusBadge status={row.original.status} />,
      },
      {
        accessorKey: "startedAt",
        header: t("sessions.startedAt"),
        cell: ({ row }) => (
          <span className="text-muted-foreground text-sm">
            {format(row.original.startedAt, "PPp", { locale: dateLocale })}
          </span>
        ),
      },
      {
        id: "duration",
        header: t("sessions.duration"),
        cell: ({ row }) => (
          <span className="text-muted-foreground text-sm">
            {sessionDuration(
              row.original,
              t("sessions.unknownEnd"),
              dateLocale,
            )}
          </span>
        ),
      },
      {
        id: "actions",
        header: () => <div className="text-right">{t("common.actions")}</div>,
        cell: ({ row }) => (
          <div className="flex justify-end gap-2">
            {row.original.status === "active" ? (
              <Link href={`/dashboard/sessions/${row.original._id}/live`}>
                <Button variant="default" size="sm">
                  <Radio className="h-4 w-4 mr-1" />
                  {t("sessions.viewLive")}
                </Button>
              </Link>
            ) : (
              <Link href={`/dashboard/sessions/${row.original._id}`}>
                <Button
                  variant="ghost"
                  size="icon"
                  title={t("sessions.viewReport")}
                >
                  <Eye className="h-4 w-4" />
                </Button>
              </Link>
            )}
          </div>
        ),
      },
    ],
    [t, dateLocale],
  );

  const table = useReactTable({
    data: sessions,
    columns,
    state: {
      globalFilter: globalFilter.trim(),
      sorting,
    },
    onGlobalFilterChange: setGlobalFilter,
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t("sessions.title")}</h1>
        {loading ? (
          <Skeleton className="h-4 w-24 mt-2" />
        ) : (
          <p className="text-muted-foreground">
            {/* "50+": more sessions than those loaded so far. */}
            {sessions.length}
            {canGrow ? "+" : ""} {t("nav.sessions").toLowerCase()}
          </p>
        )}
      </div>

      {/* Filters */}
      <div className="flex flex-col sm:flex-row gap-4">
        <Tabs
          value={statusFilter}
          onValueChange={(v) => setStatusFilter(v as StatusFilter)}
        >
          <TabsList>
            <TabsTrigger value="all">{t("common.all")}</TabsTrigger>
            <TabsTrigger value="pending">{t("sessions.pending")}</TabsTrigger>
            <TabsTrigger value="active">{t("sessions.active")}</TabsTrigger>
            {/* No news of them for a while: their end is unknown (ANH-216). */}
            <TabsTrigger value="interrupted">
              {t("sessions.interrupted")}
            </TabsTrigger>
            <TabsTrigger value="completed">
              {t("sessions.completed")}
            </TabsTrigger>
            <TabsTrigger value="failed">{t("sessions.failed")}</TabsTrigger>
          </TabsList>
        </Tabs>
        <div className="relative max-w-sm">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder={t("common.search")}
            value={globalFilter}
            onChange={(e) => setGlobalFilter(e.target.value)}
            className="pl-9"
          />
        </div>
      </div>

      {loading ? (
        <TableSkeleton />
      ) : table.getRowModel().rows.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-12">
            <Activity className="h-12 w-12 text-muted-foreground mb-4" />
            <p role="status" className="text-muted-foreground text-center">
              {canGrow
                ? searching
                  ? t("sessions.searchNoMatchLoaded", {
                      count: sessions.length,
                    })
                  : t("sessions.nothingYet")
                : searching
                  ? t("sessions.searchNoMatch")
                  : t("sessions.noSessions")}
            </p>
          </CardContent>
        </Card>
      ) : (
        <>
          {canGrow && searching && (
            <p role="status" className="text-sm text-muted-foreground">
              {t("sessions.searchLoadedOnly", { count: sessions.length })}
            </p>
          )}
          <Card>
            <CardContent className="p-0">
              <Table>
                <TableHeader>
                  {table.getHeaderGroups().map((headerGroup) => (
                    <TableRow key={headerGroup.id}>
                      {headerGroup.headers.map((header) => (
                        <TableHead key={header.id}>
                          {header.isPlaceholder
                            ? null
                            : flexRender(
                                header.column.columnDef.header,
                                header.getContext(),
                              )}
                        </TableHead>
                      ))}
                    </TableRow>
                  ))}
                </TableHeader>
                <TableBody>
                  {table.getRowModel().rows.map((row) => (
                    <TableRow
                      key={row.id}
                      className="cursor-pointer hover:bg-accent/50"
                      onClick={() => {
                        if (row.original.status === "active") {
                          router.push(
                            `/dashboard/sessions/${row.original._id}/live`,
                          );
                        } else {
                          // All other statuses (completed, pending, interrupted,
                          // failed) go to details
                          router.push(
                            `/dashboard/sessions/${row.original._id}`,
                          );
                        }
                      }}
                    >
                      {row.getVisibleCells().map((cell) => (
                        <TableCell key={cell.id}>
                          {flexRender(
                            cell.column.columnDef.cell,
                            cell.getContext(),
                          )}
                        </TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </>
      )}

      {canGrow && (
        <div className="flex justify-center">
          <Button
            variant="outline"
            disabled={status === "LoadingMore"}
            onClick={() => loadMore(PAGE_SIZE)}
          >
            {status === "LoadingMore"
              ? t("common.loading")
              : t("common.showMore")}
          </Button>
        </div>
      )}
    </div>
  );
}

function SessionStatusBadge({ status }: { status: string }) {
  const statusLabel = useSessionStatusLabel();

  const variants: Record<
    string,
    "default" | "secondary" | "destructive" | "outline"
  > = {
    active: "default",
    completed: "secondary",
    pending: "outline",
    failed: "destructive",
  };

  // Unknown statuses are shown as received rather than hidden.
  return (
    <Badge variant={variants[status] || "outline"}>{statusLabel(status)}</Badge>
  );
}

/** The table while the first page of the tab is loading. */
function TableSkeleton() {
  return (
    <Card>
      <CardContent className="p-0">
        <div className="space-y-2 p-4">
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
        </div>
      </CardContent>
    </Card>
  );
}

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
import { formatDistanceToNow, format } from "date-fns";
import { fr, enUS } from "date-fns/locale";
import { SessionKindCell } from "@/components/training/SessionKindCell";
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

type StatusFilter = "all" | "pending" | "active" | "completed" | "failed";

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
            {row.original.endedAt
              ? formatDuration(row.original.endedAt - row.original.startedAt)
              : formatDistanceToNow(row.original.startedAt, {
                  locale: dateLocale,
                })}
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
      globalFilter,
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
        // A page can come back empty while older sessions remain (the
        // server split it, or left out a row): "none" only once the list is
        // done; until then "Show more" alone.
        status === "Exhausted" && (
          <Card>
            <CardContent className="flex flex-col items-center justify-center py-12">
              <Activity className="h-12 w-12 text-muted-foreground mb-4" />
              <p className="text-muted-foreground">
                {t("sessions.noSessions")}
              </p>
            </CardContent>
          </Card>
        )
      ) : (
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
                        // All other statuses (completed, pending, failed) go to details
                        router.push(`/dashboard/sessions/${row.original._id}`);
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

function formatDuration(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);

  if (hours > 0) {
    return `${hours}h ${minutes % 60}m`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds % 60}s`;
  }
  return `${seconds}s`;
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

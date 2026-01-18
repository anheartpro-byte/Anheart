"use client";

import { useState, useMemo } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations, useLocale } from "next-intl";
import { Link, useRouter } from "@/i18n/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Plus, Cpu, Search, Eye } from "lucide-react";
import { formatDistanceToNow } from "date-fns";
import { fr, enUS } from "date-fns/locale";
import { MachineFormModal } from "@/components/modals/MachineFormModal";
import {
  useReactTable,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  flexRender,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";

type Machine = {
  _id: Id<"machines">;
  name: string;
  status: string;
  lastHeartbeat: number;
  location?: string;
  isDeleted?: boolean;
};

export default function MachinesPage() {
  const t = useTranslations();
  const locale = useLocale();
  const router = useRouter();
  const user = useQuery(api.users.getCurrentUser);

  const [globalFilter, setGlobalFilter] = useState("");
  const [sorting, setSorting] = useState<SortingState>([]);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showDeleted, setShowDeleted] = useState(false);

  const isAdmin = user?.role === "admin";

  // For admin: always fetch all machines (including deleted) and filter client-side
  // For others: fetch only non-deleted machines
  const allMachines = useQuery(api.machines.listMachines, {
    includeDeleted: isAdmin ? true : undefined,
  });

  // Filter client-side based on toggle to avoid refetch
  const machines = useMemo(() => {
    if (!allMachines) return undefined;
    if (isAdmin && !showDeleted) {
      return allMachines.filter((m) => !m.isDeleted);
    }
    return allMachines;
  }, [allMachines, isAdmin, showDeleted]);

  const dateLocale = locale === "fr" ? fr : enUS;
  const canCreate = user?.role === "admin"; // Only admin can create machines

  const columns = useMemo<ColumnDef<Machine>[]>(
    () => [
      {
        accessorKey: "name",
        header: t("machines.name"),
        cell: ({ row }) => (
          <span className="font-medium">{row.original.name}</span>
        ),
      },
      {
        accessorKey: "status",
        header: t("machines.status"),
        cell: ({ row }) => (
          <MachineStatusBadge
            status={row.original.status}
            isDeleted={row.original.isDeleted}
          />
        ),
      },
      {
        accessorKey: "location",
        header: t("machines.location"),
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {row.original.location || "-"}
          </span>
        ),
      },
      {
        accessorKey: "lastHeartbeat",
        header: t("machines.lastHeartbeat"),
        cell: ({ row }) => (
          <span className="text-muted-foreground text-sm">
            {row.original.lastHeartbeat > 0
              ? formatDistanceToNow(row.original.lastHeartbeat, {
                  addSuffix: true,
                  locale: dateLocale,
                })
              : "-"}
          </span>
        ),
      },
      {
        id: "actions",
        header: () => <div className="text-right">{t("common.actions")}</div>,
        cell: ({ row }) => (
          <div className="flex justify-end gap-2">
            <Link href={`/dashboard/machines/${row.original._id}`}>
              <Button variant="ghost" size="icon" title={t("common.view")}>
                <Eye className="h-4 w-4" />
              </Button>
            </Link>
          </div>
        ),
      },
    ],
    [t, dateLocale],
  );

  const table = useReactTable({
    data: machines ?? [],
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

  if (machines === undefined) {
    return <MachinesSkeleton />;
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold">{t("machines.title")}</h1>
          <p className="text-muted-foreground">
            {machines.length} {t("nav.machines").toLowerCase()}
          </p>
        </div>
        {canCreate && (
          <Button onClick={() => setShowCreateModal(true)}>
            <Plus className="h-4 w-4 mr-2" />
            {t("machines.create")}
          </Button>
        )}
      </div>

      {/* Search and Filters */}
      <div className="flex flex-col sm:flex-row gap-4 items-start sm:items-center">
        <div className="relative flex-1 max-w-sm">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder={t("common.search")}
            value={globalFilter}
            onChange={(e) => setGlobalFilter(e.target.value)}
            className="pl-9"
          />
        </div>
        {isAdmin && (
          <div className="flex items-center gap-2">
            <Switch
              id="show-deleted"
              checked={showDeleted}
              onCheckedChange={setShowDeleted}
            />
            <Label
              htmlFor="show-deleted"
              className="text-sm text-muted-foreground"
            >
              {t("machines.showDeleted")}
            </Label>
          </div>
        )}
      </div>

      {table.getRowModel().rows.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-12">
            <Cpu className="h-12 w-12 text-muted-foreground mb-4" />
            <p className="text-muted-foreground">{t("machines.noMachines")}</p>
            {canCreate && (
              <Button
                variant="outline"
                className="mt-4"
                onClick={() => setShowCreateModal(true)}
              >
                <Plus className="h-4 w-4 mr-2" />
                {t("machines.create")}
              </Button>
            )}
          </CardContent>
        </Card>
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
                      router.push(`/dashboard/machines/${row.original._id}`);
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

      {/* Create Machine Modal */}
      <MachineFormModal
        open={showCreateModal}
        onOpenChange={setShowCreateModal}
      />
    </div>
  );
}

function MachineStatusBadge({
  status,
  isDeleted,
}: {
  status: string;
  isDeleted?: boolean;
}) {
  const t = useTranslations("machines");

  if (isDeleted) {
    return <Badge variant="destructive">{t("deleted")}</Badge>;
  }

  const variants: Record<
    string,
    "default" | "secondary" | "destructive" | "outline"
  > = {
    online: "default",
    offline: "destructive",
    in_session: "secondary",
  };

  const labels: Record<string, string> = {
    online: t("online"),
    offline: t("offline"),
    in_session: t("inSession"),
  };

  return (
    <Badge variant={variants[status] || "outline"}>
      {labels[status] || status}
    </Badge>
  );
}

function MachinesSkeleton() {
  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-4 w-24 mt-2" />
        </div>
        <Skeleton className="h-10 w-40" />
      </div>
      <Skeleton className="h-10 w-64" />
      <Card>
        <CardContent className="p-0">
          <div className="space-y-2 p-4">
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

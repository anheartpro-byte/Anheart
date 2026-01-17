"use client";

import { useState, useMemo } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations } from "next-intl";
import { Link, useRouter } from "@/i18n/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Plus, Users, Search, Pencil, Eye } from "lucide-react";
import { PatientFormModal } from "@/components/modals/PatientFormModal";
import {
  useReactTable,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  flexRender,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import { format } from "date-fns";

type Patient = {
  _id: Id<"users">;
  firstName: string;
  lastName: string;
  email: string;
  role: string;
  language: string;
  createdAt: number;
};

export default function PatientsPage() {
  const t = useTranslations();
  const router = useRouter();
  const [globalFilter, setGlobalFilter] = useState("");
  const [sorting, setSorting] = useState<SortingState>([]);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [editingPatient, setEditingPatient] = useState<Patient | null>(null);

  const patients = useQuery(api.users.listUsers, { role: "user" });

  const columns = useMemo<ColumnDef<Patient>[]>(
    () => [
      {
        accessorKey: "firstName",
        header: t("users.firstName"),
        cell: ({ row }) => (
          <div className="font-medium">
            {row.original.firstName} {row.original.lastName}
          </div>
        ),
      },
      {
        accessorKey: "email",
        header: t("users.email"),
        cell: ({ row }) => (
          <span className="text-muted-foreground">{row.original.email}</span>
        ),
      },
      {
        accessorKey: "language",
        header: t("users.language"),
        cell: ({ row }) => (
          <span className="uppercase text-sm text-muted-foreground">
            {row.original.language}
          </span>
        ),
      },
      {
        accessorKey: "createdAt",
        header: t("users.createdAt"),
        cell: ({ row }) => (
          <span className="text-sm text-muted-foreground">
            {format(row.original.createdAt, "PP")}
          </span>
        ),
      },
      {
        id: "actions",
        header: () => <div className="text-right">{t("common.actions")}</div>,
        cell: ({ row }) => (
          <div className="flex justify-end gap-2">
            <Button
              variant="ghost"
              size="icon"
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                setEditingPatient(row.original);
              }}
              title={t("common.edit")}
            >
              <Pencil className="h-4 w-4" />
            </Button>
            <Link href={`/dashboard/patients/${row.original._id}`}>
              <Button
                variant="ghost"
                size="icon"
                title={t("common.view") || "View"}
              >
                <Eye className="h-4 w-4" />
              </Button>
            </Link>
          </div>
        ),
      },
    ],
    [t],
  );

  const table = useReactTable({
    data: patients ?? [],
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

  if (patients === undefined) {
    return <PatientsSkeleton />;
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold">{t("users.patients")}</h1>
          <p className="text-muted-foreground">
            {patients.length} {t("nav.patients").toLowerCase()}
          </p>
        </div>
        <Button onClick={() => setShowCreateModal(true)}>
          <Plus className="h-4 w-4 mr-2" />
          {t("users.createPatient")}
        </Button>
      </div>

      {/* Search */}
      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
        <Input
          placeholder={t("common.search")}
          value={globalFilter}
          onChange={(e) => setGlobalFilter(e.target.value)}
          className="pl-9"
        />
      </div>

      {table.getRowModel().rows.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-12">
            <Users className="h-12 w-12 text-muted-foreground mb-4" />
            <p className="text-muted-foreground">{t("users.noPatients")}</p>
            <Button
              variant="outline"
              className="mt-4"
              onClick={() => setShowCreateModal(true)}
            >
              <Plus className="h-4 w-4 mr-2" />
              {t("users.createPatient")}
            </Button>
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
                      router.push(`/dashboard/patients/${row.original._id}`);
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

      {/* Create Patient Modal */}
      <PatientFormModal
        open={showCreateModal}
        onOpenChange={setShowCreateModal}
      />

      {/* Edit Patient Modal */}
      <PatientFormModal
        open={editingPatient !== null}
        onOpenChange={(open) => {
          if (!open) setEditingPatient(null);
        }}
        patient={editingPatient ?? undefined}
      />
    </div>
  );
}

function PatientsSkeleton() {
  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <Skeleton className="h-8 w-32" />
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

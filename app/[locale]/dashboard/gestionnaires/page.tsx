"use client";

import { useState } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";

import { useTranslations } from "next-intl";
import { Link, useRouter } from "@/i18n/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Search, Users, Cpu, Eye, Shield } from "lucide-react";

export default function GestionnairesPage() {
  const t = useTranslations();
  const router = useRouter();
  const user = useQuery(api.users.getCurrentUser);
  const gestionnaires = useQuery(api.users.listGestionnaires);

  const [searchFilter, setSearchFilter] = useState("");

  // Only admin can access this page
  if (user === undefined || gestionnaires === undefined) {
    return <GestionnairesSkeleton />;
  }

  if (user?.role !== "admin") {
    return (
      <div className="text-center py-12">
        <Shield className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
        <p className="text-muted-foreground">
          {t("common.error")}: Admin access required
        </p>
      </div>
    );
  }

  const filteredGestionnaires = gestionnaires.filter(
    (g) =>
      g.firstName.toLowerCase().includes(searchFilter.toLowerCase()) ||
      g.lastName.toLowerCase().includes(searchFilter.toLowerCase()) ||
      g.email.toLowerCase().includes(searchFilter.toLowerCase()),
  );

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold">{t("gestionnaires.title")}</h1>
          <p className="text-muted-foreground">
            {gestionnaires.length} {t("gestionnaires.total")}
          </p>
        </div>
      </div>

      {/* Search */}
      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
        <Input
          placeholder={t("common.search")}
          value={searchFilter}
          onChange={(e) => setSearchFilter(e.target.value)}
          className="pl-9"
        />
      </div>

      {filteredGestionnaires.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-12">
            <Users className="h-12 w-12 text-muted-foreground mb-4" />
            <p className="text-muted-foreground">
              {t("gestionnaires.noGestionnaires")}
            </p>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("users.firstName")}</TableHead>
                  <TableHead>{t("users.email")}</TableHead>
                  <TableHead className="text-center">
                    <div className="flex items-center justify-center gap-1">
                      <Cpu className="h-4 w-4" />
                      {t("nav.machines")}
                    </div>
                  </TableHead>
                  <TableHead className="text-center">
                    <div className="flex items-center justify-center gap-1">
                      <Users className="h-4 w-4" />
                      {t("nav.patients")}
                    </div>
                  </TableHead>
                  <TableHead className="text-right">
                    {t("common.actions")}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filteredGestionnaires.map((g) => (
                  <TableRow
                    key={g._id}
                    className="cursor-pointer hover:bg-accent/50"
                    onClick={() =>
                      router.push(`/dashboard/gestionnaires/${g._id}`)
                    }
                  >
                    <TableCell className="font-medium">
                      {g.firstName} {g.lastName}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {g.email}
                    </TableCell>
                    <TableCell className="text-center">
                      <Badge variant="secondary">{g.machineCount}</Badge>
                    </TableCell>
                    <TableCell className="text-center">
                      <Badge variant="outline">{g.patientCount}</Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      <Link href={`/dashboard/gestionnaires/${g._id}`}>
                        <Button variant="ghost" size="icon">
                          <Eye className="h-4 w-4" />
                        </Button>
                      </Link>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function GestionnairesSkeleton() {
  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-4 w-24 mt-2" />
        </div>
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

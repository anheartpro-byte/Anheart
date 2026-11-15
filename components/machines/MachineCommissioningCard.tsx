"use client";

import { useQuery } from "convex/react";
import { useLocale, useTranslations } from "next-intl";
import { format } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import { ClipboardCheck } from "lucide-react";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { Link } from "@/i18n/navigation";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * A machine's commissionings (ANH-115), for whoever reads its registry: each
 * record, most recent first, its verdict and its findings, and its page to
 * print or export. The server decides who reads them
 * (`convex/machineCommissioning.ts`); the page mounts this card for admins.
 */
export function MachineCommissioningCard({
  machineId,
}: {
  machineId: Id<"machines">;
}) {
  const t = useTranslations("machineCommissioning");
  const locale = useLocale();
  const records = useQuery(api.machineCommissioning.listCommissionings, { machineId });
  const dateLocale = locale === "fr" ? fr : enUS;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ClipboardCheck className="h-5 w-5" aria-hidden="true" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent>
        {records === undefined ? (
          <Skeleton className="h-16" />
        ) : records.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("none")}</p>
        ) : (
          <ul className="divide-y">
            {records.map((record) => (
              <li key={record.recordId} className="flex flex-wrap items-center gap-2 py-2">
                <span className="text-sm">
                  {format(record.recordedAt, "PPp", { locale: dateLocale })}
                </span>
                <Badge variant={record.verdict === "conforming" ? "default" : "destructive"}>
                  {t(`verdicts.${record.verdict}`)}
                </Badge>
                {record.findings > 0 && (
                  <span className="text-sm text-muted-foreground">
                    {t("findings", { count: record.findings })}
                  </span>
                )}
                {!record.signed && (
                  <span className="text-sm text-muted-foreground">{t("unsigned")}</span>
                )}
                <Link
                  href={`/dashboard/machines/${machineId}/commissioning/${record.recordId}`}
                  className="ml-auto text-sm text-primary hover:underline"
                >
                  {t("open")}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

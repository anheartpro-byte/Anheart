"use client";

import { use } from "react";
import { useQuery } from "convex/react";
import { useLocale, useTranslations } from "next-intl";
import { format } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import { ArrowLeft, Printer } from "lucide-react";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { isAnheartAdmin } from "@/lib/organizationAccess";

/**
 * A machine's logbook to print (ANH-149): the browser's print to PDF, as for
 * the session report, oldest entry first, identifiers only. For an Anheart
 * admin, who exports the logbook for the technical file.
 */
export default function MachineLogbookPrintPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const machineId = id as Id<"machines">;
  const t = useTranslations("machineLogbook");
  const locale = useLocale();
  const user = useQuery(api.users.getCurrentUser);
  const book = useQuery(api.machineLogbook.list, { machineId });
  if (user === undefined || book === undefined)
    return <Skeleton className="mx-auto h-[600px] max-w-4xl" />;
  if (book === null || !isAnheartAdmin(user?.role))
    return (
      <div className="py-12 text-center">
        <p className="text-muted-foreground">{t("printNotAllowed")}</p>
        <Link
          href={`/dashboard/machines/${machineId}`}
          className="mt-2 inline-block text-primary hover:underline"
        >
          {t("back")}
        </Link>
      </div>
    );
  const dateLocale = locale === "fr" ? fr : enUS;
  const entries = [...book.entries].sort((a, b) => a.at - b.at);
  return (
    <div className="mx-auto max-w-4xl space-y-4 print:max-w-none">
      <div className="flex flex-wrap items-center gap-2 print:hidden">
        <Link href={`/dashboard/machines/${machineId}`}>
          <Button variant="ghost" size="sm">
            <ArrowLeft className="mr-2 h-4 w-4" />
            {t("back")}
          </Button>
        </Link>
        <Button size="sm" onClick={() => window.print()}>
          <Printer className="mr-2 h-4 w-4" />
          {t("printButton")}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground print:hidden">{t("printHint")}</p>
      <article className="space-y-4 rounded-lg border bg-card p-6 print:border-0 print:p-0">
        <header>
          <h1 className="text-2xl font-bold">{t("printTitle")}</h1>
          <p className="text-sm text-muted-foreground">
            {t("printMachine", { id: machineId })}
          </p>
        </header>
        <table className="w-full text-left text-sm">
          <thead>
            <tr>
              <th className="py-1 pr-2">{t("columns.at")}</th>
              <th className="py-1 pr-2">{t("columns.kind")}</th>
              <th className="py-1 pr-2">{t("columns.summary")}</th>
              <th className="py-1 pr-2">{t("columns.by")}</th>
              <th className="py-1">{t("columns.refs")}</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={entry._id} className="border-t align-top print:break-inside-avoid">
                <td className="py-1 pr-2 whitespace-nowrap">
                  {format(entry.at, "PPp", { locale: dateLocale })}
                </td>
                <td className="py-1 pr-2">{t(`kinds.${entry.kind}`)}</td>
                <td className="py-1 pr-2">
                  <p>{entry.summary}</p>
                  {entry.details !== undefined && (
                    <p className="whitespace-pre-line text-muted-foreground">{entry.details}</p>
                  )}
                </td>
                <td className="py-1 pr-2 font-mono text-xs">{entry.by ?? t("byServer")}</td>
                <td className="py-1 font-mono text-xs">
                  {[
                    entry.refs.incidentId,
                    entry.refs.configRevision,
                    entry.refs.softwareVersion,
                    entry.refs.storageId,
                  ]
                    .filter((ref) => ref !== undefined)
                    .join(" ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </article>
    </div>
  );
}

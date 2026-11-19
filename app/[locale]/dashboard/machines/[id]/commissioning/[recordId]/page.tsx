"use client";

import { use } from "react";
import { useQuery } from "convex/react";
import { useLocale, useTranslations } from "next-intl";
import { format, isValid } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import { ArrowLeft, Download, Printer } from "lucide-react";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { Link } from "@/i18n/navigation";
import { save } from "@/lib/download";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  commissioningFileName,
  commissioningJson,
  type CommissioningRecord,
} from "@/components/machines/commissioningJson";

/** The drive values a sheet carries and a console reads, in the keypad's order. */
const DRIVE_ROWS = [
  ["tFr", "tfrHz", "Hz"],
  ["HSP", "hspHz", "Hz"],
  ["LSP", "lspHz", "Hz"],
  ["ACC", "accS", "s"],
  ["dEC", "decS", "s"],
] as const;

const FINDING_IDS = [
  "sto_bridged",
  "sto_source",
  "drive_unread",
  "drive_tfr",
  "drive_hsp",
  "drive_lsp",
  "drive_acc",
  "drive_dec",
  "bitalino_address",
  "software_version",
] as const;

type Person = CommissioningRecord["operator"];

function nameOf(person: Person): string {
  return [person.firstName, person.lastName].filter(Boolean).join(" ") || person.userId;
}

/**
 * A commissioning record to print (ANH-115): the browser's print to PDF, as
 * for the session report and the logbook, and its signed text to download.
 * For whoever reads the machine's registry; the server says who
 * (`machineCommissioning.getCommissioning`).
 */
export default function CommissioningPrintPage({
  params,
}: {
  params: Promise<{ id: string; recordId: string }>;
}) {
  const { id, recordId } = use(params);
  const machineId = id as Id<"machines">;
  const t = useTranslations("machineCommissioning");
  const locale = useLocale();
  const record = useQuery(api.machineCommissioning.getCommissioning, {
    recordId: recordId as Id<"machine_commissioning">,
  });
  if (record === undefined) return <Skeleton className="mx-auto h-[600px] max-w-4xl" />;
  if (record === null || record.machineId !== machineId)
    return (
      <div className="py-12 text-center">
        <p className="text-muted-foreground">{t("notAllowed")}</p>
        <Link
          href={`/dashboard/machines/${machineId}`}
          className="mt-2 inline-block text-primary hover:underline"
        >
          {t("back")}
        </Link>
      </div>
    );
  const dateLocale = locale === "fr" ? fr : enUS;
  // A date the console wrote is shown only when a date holds it (`max_date_ms`).
  const dated = (at: number, pattern: string) =>
    isValid(new Date(at)) ? format(at, pattern, { locale: dateLocale }) : t("missingValue");
  const when = (at: number) => dated(at, "PPp");
  const tenth = new Intl.NumberFormat(locale, {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  });
  const { report, reference } = record;
  const findingLabel = (findingId: string) =>
    findingId.startsWith("control:")
      ? t("controlFinding", { id: findingId.slice("control:".length) })
      : (FINDING_IDS as readonly string[]).includes(findingId)
        ? t(`findingIds.${findingId as (typeof FINDING_IDS)[number]}`)
        : findingId;
  const stateLabel = (state: string) =>
    state === "ok" || state === "warn" || state === "block" ? t(`states.${state}`) : state;
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
        <Button
          size="sm"
          variant="outline"
          onClick={() =>
            save(
              new Blob([commissioningJson(record)], { type: "application/json" }),
              commissioningFileName(record),
            )
          }
        >
          <Download className="mr-2 h-4 w-4" />
          {t("download")}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground print:hidden">{t("printHint")}</p>
      <article className="space-y-6 rounded-lg border bg-card p-6 print:border-0 print:p-0">
        <header className="space-y-1">
          <h1 className="text-2xl font-bold">{t("printTitle")}</h1>
          <p className="text-sm text-muted-foreground">{t("printMachine", { id: machineId })}</p>
          <p className="text-sm">{t("recordedAt", { at: when(record.recordedAt) })}</p>
          <p className="text-sm">{t("ranAt", { at: when(report.ranAt) })}</p>
          <Badge variant={record.verdict === "conforming" ? "default" : "destructive"}>
            {t(`verdicts.${record.verdict}`)}
          </Badge>
        </header>

        <section className="space-y-1 print:break-inside-avoid">
          <h2 className="font-semibold">{t("findingsTitle")}</h2>
          {record.findings.length === 0 ? (
            <p className="text-sm">{t("noFindings")}</p>
          ) : (
            <ul className="list-disc pl-5 text-sm">
              {record.findings.map((finding) => (
                <li key={finding.id}>
                  {t("finding", {
                    label: findingLabel(finding.id),
                    expected: finding.expected,
                    read: finding.read,
                  })}
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="space-y-1 print:break-inside-avoid">
          <h2 className="font-semibold">{t("people")}</h2>
          <p className="text-sm">{t("ranBy", { name: nameOf(record.operator) })}</p>
          <p className="text-sm">
            {t("attestedBy", { name: nameOf(record.attestedBy), at: when(report.attestedAt) })}
          </p>
          <blockquote className="border-l-2 pl-3 text-sm italic">{report.statement}</blockquote>
        </section>

        <section className="space-y-1 print:break-inside-avoid">
          <h2 className="font-semibold">{t("sto")}</h2>
          <p className="text-sm">{t("stoSource", { source: report.stoSource })}</p>
          {report.stoSource === "bridged_declared" && (
            <p className="text-sm font-medium text-destructive">{t("stoBridged")}</p>
          )}
        </section>

        <section className="space-y-1 print:break-inside-avoid">
          <h2 className="font-semibold">{t("drive")}</h2>
          <p className="text-sm">
            {t("sheet", {
              version: reference.sheet.version,
              by: reference.sheet.signedBy,
              at: dated(reference.sheet.at, "PP"),
            })}
          </p>
          <table className="w-full text-left text-sm">
            <thead>
              <tr>
                <th className="py-1 pr-2">{t("parameter")}</th>
                <th className="py-1 pr-2">{t("expected")}</th>
                <th className="py-1">{t("read")}</th>
              </tr>
            </thead>
            <tbody>
              {DRIVE_ROWS.map(([label, field, unit]) => (
                <tr key={field} className="border-t">
                  <td className="py-1 pr-2">{label}</td>
                  <td className="py-1 pr-2">
                    {tenth.format(reference.sheet.parameters[field])} {unit}
                  </td>
                  <td className="py-1">
                    {report.drive === null
                      ? t("notRead")
                      : `${tenth.format(report.drive[field])} ${unit}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="space-y-1">
          <h2 className="font-semibold">{t("controls")}</h2>
          <table className="w-full text-left text-sm">
            <thead>
              <tr>
                <th className="py-1 pr-2">{t("control")}</th>
                <th className="py-1 pr-2">{t("state")}</th>
                <th className="py-1">{t("detail")}</th>
              </tr>
            </thead>
            <tbody>
              {report.controls.map((control) => (
                <tr key={control.id} className="border-t align-top print:break-inside-avoid">
                  <td className="py-1 pr-2 font-mono text-xs">{control.id}</td>
                  <td className="py-1 pr-2">{stateLabel(control.state)}</td>
                  <td className="py-1">{control.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="space-y-1 print:break-inside-avoid">
          <h2 className="font-semibold">{t("versions")}</h2>
          <p className="text-sm">
            {t("softwareVersion", {
              version: report.softwareVersion,
              expected: reference.expectedSoftwareVersion ?? t("missingValue"),
            })}
          </p>
          <p className="text-sm">
            {t("medicalVersion", {
              version: report.medicalParametersVersion ?? t("missingValue"),
            })}
          </p>
          <p className="text-sm">
            {t("configHash", { hash: report.configHash ?? t("missingValue") })}
          </p>
          <p className="text-sm">
            {t("bitalino", {
              address: report.bitalinoAddress ?? t("missingValue"),
              expected: reference.bitalinoAddress ?? t("missingValue"),
            })}
          </p>
        </section>

        <section className="space-y-1 print:break-inside-avoid">
          <h2 className="font-semibold">{t("signature")}</h2>
          <p className="text-sm">{record.signature === null ? t("notSigned") : t("signed")}</p>
        </section>
      </article>
    </div>
  );
}

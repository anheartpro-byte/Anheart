"use client";

import { useState } from "react";
import { useQuery } from "convex/react";
import { ConvexError } from "convex/values";
import { useLocale, useTranslations } from "next-intl";
import { format } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import { AlertTriangle, Download, Paperclip, Printer } from "lucide-react";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { Link } from "@/i18n/navigation";
import {
  runWithFeedback,
  useMutationWithFeedback,
} from "@/hooks/use-mutation-with-feedback";
import { save } from "@/lib/download";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { logbookCsv, type LogbookEntry } from "./logbookCsv";

type Kind = LogbookEntry["kind"];

/** What an attachment may be (convex/machineLogbook.ts checks it again). */
const ATTACHMENT_TYPES = ["application/pdf", "image/jpeg", "image/png"];
const ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024;

/** Upload one attachment; its storage id, or null when refused (said once). */
function useLogbookUpload() {
  const t = useTranslations();
  const generateUploadUrl = useMutationWithFeedback(
    api.machineLogbook.generateUploadUrl,
  );
  return async (file: File): Promise<Id<"_storage"> | null> => {
    const refused = async () => {
      throw new ConvexError({ code: "invalid_logbook_file" });
    };
    if (!ATTACHMENT_TYPES.includes(file.type) || file.size > ATTACHMENT_MAX_BYTES) {
      await runWithFeedback("[logbook upload]", refused, undefined, t);
      return null;
    }
    const url = await generateUploadUrl({});
    if (!url.ok) return null;
    const uploaded = await runWithFeedback(
      "[logbook upload]",
      async () => {
        let result: unknown = null;
        try {
          const response = await fetch(url.value, {
            method: "POST",
            headers: { "Content-Type": file.type },
            body: file,
          });
          if (response.ok) result = await response.json();
        } catch {
          return await refused();
        }
        if (
          typeof result !== "object" ||
          result === null ||
          !("storageId" in result) ||
          typeof result.storageId !== "string" ||
          result.storageId.length === 0
        )
          return await refused();
        return result.storageId as Id<"_storage">;
      },
      undefined,
      t,
    );
    return uploaded.ok ? uploaded.value : null;
  };
}

/**
 * A machine's logbook (ANH-149): its line of time, most recent first, the
 * drive faults that still close its programmed sessions, the form to write an
 * entry by hand for whoever may, and, for an Anheart admin, the export (CSV,
 * and the print view for a PDF). The server decides who reads and writes.
 */
export function MachineLogbookCard({
  machineId,
  exportable,
}: {
  machineId: Id<"machines">;
  exportable: boolean;
}) {
  const t = useTranslations("machineLogbook");
  const locale = useLocale();
  const book = useQuery(api.machineLogbook.list, { machineId });
  const addEntry = useMutationWithFeedback(api.machineLogbook.addEntry);
  const upload = useLogbookUpload();
  const [kind, setKind] = useState<Kind | "">("");
  const [summary, setSummary] = useState("");
  const [details, setDetails] = useState("");
  const [incidentId, setIncidentId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  if (!book) return null;

  const when = (at: number) =>
    format(at, "PPp", { locale: locale === "fr" ? fr : enUS });
  const chosen = kind === "" ? book.writable[0] : kind;

  async function submit() {
    if (chosen === undefined) return;
    setBusy(true);
    const storageId = file === null ? undefined : await upload(file);
    if (storageId !== null) {
      const result = await addEntry(
        {
          machineId,
          kind: chosen,
          summary,
          details: details.trim() === "" ? undefined : details,
          incidentId:
            chosen === "intervention" && incidentId !== ""
              ? (incidentId as Id<"incidents">)
              : undefined,
          storageId,
        },
        { success: t("added") },
      );
      if (result.ok) {
        setSummary("");
        setDetails("");
        setIncidentId("");
        setFile(null);
      }
    }
    setBusy(false);
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
        <div className="space-y-1.5">
          <CardTitle>{t("title")}</CardTitle>
          <CardDescription>{t("description")}</CardDescription>
        </div>
        {exportable && (
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() =>
                save(
                  new Blob([logbookCsv(book.entries)], {
                    type: "text/csv;charset=utf-8",
                  }),
                  `carnet-${machineId}.csv`,
                )
              }
            >
              <Download className="mr-2 h-4 w-4" />
              {t("exportCsv")}
            </Button>
            <Button size="sm" variant="outline" asChild>
              <Link href={`/dashboard/machines/${machineId}/logbook`}>
                <Printer className="mr-2 h-4 w-4" />
                {t("print")}
              </Link>
            </Button>
          </div>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        {book.interventionsDue.length > 0 && (
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" />
            <AlertTitle>{t("dueTitle")}</AlertTitle>
            <AlertDescription>
              <p>{t("dueText")}</p>
              <ul className="list-disc pl-5">
                {book.interventionsDue.map((id) => (
                  <li key={id}>
                    <Link href={`/dashboard/incidents/${id}`} className="underline">
                      {t("incident", { id })}
                    </Link>
                  </li>
                ))}
              </ul>
            </AlertDescription>
          </Alert>
        )}

        {chosen !== undefined && (
          <form
            className="grid gap-3 rounded-md border p-3 sm:grid-cols-2"
            onSubmit={(event) => {
              event.preventDefault();
              void submit();
            }}
          >
            <div className="space-y-1">
              <Label htmlFor="logbook-kind">{t("kindLabel")}</Label>
              <Select value={chosen} onValueChange={(value) => setKind(value as Kind)}>
                <SelectTrigger id="logbook-kind">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {book.writable.map((option) => (
                    <SelectItem key={option} value={option}>
                      {t(`kinds.${option}`)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {chosen === "intervention" && book.interventionsDue.length > 0 && (
              <div className="space-y-1">
                <Label htmlFor="logbook-incident">{t("incidentLabel")}</Label>
                <Select value={incidentId} onValueChange={setIncidentId}>
                  <SelectTrigger id="logbook-incident">
                    <SelectValue placeholder={t("incidentNone")} />
                  </SelectTrigger>
                  <SelectContent>
                    {book.interventionsDue.map((id) => (
                      <SelectItem key={id} value={id}>
                        {t("incident", { id })}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
            <div className="space-y-1 sm:col-span-2">
              <Label htmlFor="logbook-summary">{t("summaryLabel")}</Label>
              <Input
                id="logbook-summary"
                value={summary}
                maxLength={200}
                onChange={(event) => setSummary(event.target.value)}
              />
            </div>
            <div className="space-y-1 sm:col-span-2">
              <Label htmlFor="logbook-details">{t("detailsLabel")}</Label>
              <Textarea
                id="logbook-details"
                value={details}
                maxLength={2000}
                onChange={(event) => setDetails(event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="logbook-file">{t("fileLabel")}</Label>
              <Input
                id="logbook-file"
                type="file"
                accept={ATTACHMENT_TYPES.join(",")}
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              />
            </div>
            <div className="flex items-end justify-end">
              <Button type="submit" disabled={busy || summary.trim().length < 3}>
                {t("add")}
              </Button>
            </div>
          </form>
        )}

        {book.entries.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("empty")}</p>
        ) : (
          <ol className="max-h-[32rem] space-y-3 overflow-y-auto">
            {book.entries.map((entry) => (
              <li key={entry._id} className="border-l-2 pl-3">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="text-muted-foreground">{when(entry.at)}</span>
                  <Badge variant="outline">{t(`kinds.${entry.kind}`)}</Badge>
                  <span className="text-muted-foreground">
                    {entry.by === undefined ? t("byServer") : (entry.byName ?? t("byUnknown"))}
                  </span>
                </div>
                <p className="font-medium">{entry.summary}</p>
                {entry.details !== undefined && (
                  <p className="whitespace-pre-line text-sm">{entry.details}</p>
                )}
                <div className="flex flex-wrap gap-3 text-sm">
                  {entry.refs.incidentId !== undefined && (
                    <Link
                      href={`/dashboard/incidents/${entry.refs.incidentId}`}
                      className="underline"
                    >
                      {t("incident", { id: entry.refs.incidentId })}
                    </Link>
                  )}
                  {entry.attachmentUrl !== null && (
                    <a href={entry.attachmentUrl} className="inline-flex items-center underline">
                      <Paperclip className="mr-1 h-3 w-3" />
                      {t("attachment")}
                    </a>
                  )}
                </div>
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}

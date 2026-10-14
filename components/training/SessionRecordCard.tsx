"use client";

import { useEffect, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { useConvex, useQuery } from "convex/react";
import type { FunctionReturnType } from "convex/server";
import { useMutationWithFeedback } from "@/hooks/use-mutation-with-feedback";
import { useTranslations } from "next-intl";
import { Archive, Download, ExternalLink } from "lucide-react";
import { api } from "@/convex/_generated/api";
import { isOrganizationAdmin } from "@/lib/organizationAccess";
import type { Id } from "@/convex/_generated/dataModel";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

function save(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

export function SessionRecordCard({
  sessionId,
}: {
  sessionId: Id<"sessions">;
}) {
  const t = useTranslations("sessionRecord");
  const { getToken } = useAuth();
  const convex = useConvex();
  const [cursor, setCursor] = useState<string | null>(null);
  const [previous, setPrevious] = useState<Array<string | null>>([]);
  const data = useQuery(api.recordStorage.list, { sessionId, cursor });
  const user = useQuery(api.users.getCurrentUser);
  const remove = useMutationWithFeedback(api.recordStorage.remove);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState(false);
  const [showDelete, setShowDelete] = useState(false);
  const [limit, setLimit] = useState(50);
  const current = useRef<AbortController | null>(null);
  useEffect(() => () => current.current?.abort(), []);

  async function run(work: (signal: AbortSignal) => Promise<void>) {
    const controller = new AbortController();
    current.current = controller;
    setBusy(true);
    setError(false);
    setProgress(0);
    try {
      await work(controller.signal);
    } catch (error: unknown) {
      if (!(error instanceof DOMException && error.name === "AbortError"))
        setError(true);
    } finally {
      if (current.current === controller) current.current = null;
      setBusy(false);
    }
  }
  async function download(
    file: { id: string; sha256: string; size: number },
    signal: AbortSignal,
  ) {
    const token = await getToken({ template: "convex" });
    if (!token) throw Error("Sign-in required");
    const { fetchRecordFile } = await import("@/lib/session-record");
    const bytes = await fetchRecordFile(file, token, signal);
    setProgress((value) => value + 1);
    return bytes;
  }
  const verified = data?.viewerFiles ?? [];
  const canView = ["manifest.json", "ticks.csv", "events.jsonl"].every((path) =>
    verified.some((file) => file.path === path),
  );

  function openViewer() {
    const viewer = window.open("/viewer/index.html", "_blank");
    if (!viewer) {
      setError(true);
      return;
    }
    void run(async (signal) => {
      let cancelReady = () => {};
      const ready = new Promise<void>((resolve, reject) => {
        const listener = (event: MessageEvent<unknown>) => {
          if (
            event.source === viewer &&
            event.origin === window.location.origin &&
            event.data === "anheart-viewer-ready"
          ) {
            cancelReady();
            resolve();
          }
        };
        const timeout = setTimeout(() => {
          cancelReady();
          reject(Error("Viewer did not load"));
        }, 20_000);
        cancelReady = () => {
          clearTimeout(timeout);
          window.removeEventListener("message", listener);
        };
        window.addEventListener("message", listener);
      });
      try {
        const sources = Promise.all(
          ["manifest.json", "ticks.csv", "events.jsonl"].map(async (path) => {
            const file = verified.find((file) => file.path === path);
            if (!file) throw Error("Record incomplete");
            return new TextDecoder("utf-8", { fatal: true }).decode(
              await download(file, signal),
            );
          }),
        );
        const [texts] = await Promise.all([sources, ready]);
        const { recordTrace } = await import("@/lib/session-record");
        viewer.postMessage(
          {
            type: "anheart-record",
            trace: recordTrace(texts[0], texts[1], texts[2]),
          },
          window.location.origin,
        );
      } catch (error: unknown) {
        viewer.close();
        throw error;
      } finally {
        cancelReady();
      }
    });
  }

  if (data === null) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Archive className="size-4" aria-hidden="true" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {data === undefined ? (
          <Skeleton className="h-24 w-full motion-reduce:animate-none" />
        ) : (
          <>
            <div className="flex flex-wrap gap-2 items-center">
              <Badge
                variant={
                  data.record === "rejected" ? "destructive" : "secondary"
                }
              >
                {t(data.deleted ? "deleted" : data.record)}
              </Badge>
              <span className="text-sm text-muted-foreground">
                {t("count", {
                  verified: data.verifiedCount,
                  total: data.expectedCount,
                })}
              </span>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                disabled={busy || data.record !== "complete"}
                onClick={() =>
                  void run(async (signal) => {
                    const files: Array<{ path: string; bytes: Uint8Array }> =
                      [];
                    let next: string | null = null;
                    const seen = new Set<string>();
                    do {
                      signal.throwIfAborted();
                      if (next !== null) {
                        if (seen.has(next)) throw Error("Repeated file page");
                        seen.add(next);
                      }
                      const page: FunctionReturnType<
                        typeof api.recordStorage.list
                      > = await convex.query(api.recordStorage.list, {
                        sessionId,
                        cursor: next,
                      });
                      if (!page || page.record !== "complete" || page.deleted)
                        throw Error("Record no longer complete");
                      for (
                        let offset = 0;
                        offset < page.files.length;
                        offset += 4
                      ) {
                        const group = await Promise.all(
                          page.files
                            .slice(offset, offset + 4)
                            .map(async (file) => ({
                              path: file.path,
                              bytes: await download(file, signal),
                            })),
                        );
                        files.push(...group);
                      }
                      next = page.cursor;
                    } while (next !== null);
                    if (files.length !== data.expectedCount)
                      throw Error("Incomplete archive");
                    const { recordArchive } =
                      await import("@/lib/session-record");
                    save(recordArchive(files), `record-${sessionId}.tar`);
                  })
                }
              >
                <Download className="size-4" aria-hidden="true" />
                {t("archive")}
              </Button>
              <Button
                variant="outline"
                disabled={busy || !canView}
                onClick={openViewer}
              >
                <ExternalLink className="size-4" aria-hidden="true" />
                {t("viewer")}
              </Button>
              {isOrganizationAdmin(user?.role) &&
                data.files.length > 0 && (
                  <Button
                    variant="outline"
                    disabled={busy}
                    onClick={() => setShowDelete(true)}
                  >
                    {t("delete")}
                  </Button>
                )}
              {busy && (
                <Button
                  variant="outline"
                  onClick={() => current.current?.abort()}
                >
                  {t("cancel")}
                </Button>
              )}
            </div>
            {busy && (
              <p role="status" className="text-sm text-muted-foreground">
                {t("progress", { count: progress })}
              </p>
            )}
            {error && (
              <p role="alert" className="text-sm text-destructive">
                {t("error")}
              </p>
            )}
            {data.files.length > 0 && (
              <details>
                <summary className="cursor-pointer text-sm rounded-sm focus-visible:outline-ring focus-visible:outline-2">
                  {t("files", { count: data.files.length })}
                </summary>
                <ul className="divide-y mt-2">
                  {data.files.slice(0, limit).map((file) => (
                    <li
                      key={file.id}
                      className="flex flex-wrap items-center justify-between gap-2 py-3"
                    >
                      <div className="min-w-0">
                        <p className="font-mono text-sm break-all">
                          {file.path}
                        </p>
                        <p className="text-xs text-muted-foreground">
                          {(file.size / 1024).toFixed(1)} KiB ·{" "}
                          {t(
                            file.verified
                              ? "verified"
                              : file.rejection
                                ? "rejected"
                                : "checking",
                          )}
                        </p>
                      </div>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={busy || !file.verified}
                        aria-label={t("downloadFile", { path: file.path })}
                        onClick={() =>
                          void run(async (signal) => {
                            const bytes = await download(file, signal);
                            save(
                              new Blob([new Uint8Array(bytes)]),
                              file.path.split("/").pop() ?? "record",
                            );
                          })
                        }
                      >
                        <Download className="size-4" aria-hidden="true" />
                        {t("download")}
                      </Button>
                    </li>
                  ))}
                </ul>
                {limit < data.files.length && (
                  <Button
                    variant="outline"
                    onClick={() => setLimit((value) => value + 50)}
                  >
                    {t("more")}
                  </Button>
                )}
                <div className="flex flex-wrap gap-2 mt-2">
                  {previous.length > 0 && (
                    <Button
                      variant="outline"
                      disabled={busy}
                      onClick={() => {
                        setCursor(previous[previous.length - 1]);
                        setPrevious(previous.slice(0, -1));
                        setLimit(50);
                      }}
                    >
                      {t("previous")}
                    </Button>
                  )}
                  {data.cursor !== null && (
                    <Button
                      variant="outline"
                      disabled={busy}
                      onClick={() => {
                        setPrevious([...previous, cursor]);
                        setCursor(data.cursor);
                        setLimit(50);
                      }}
                    >
                      {t("next")}
                    </Button>
                  )}
                </div>
              </details>
            )}
          </>
        )}
      </CardContent>
      <Dialog open={showDelete} onOpenChange={setShowDelete}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("deleteTitle")}</DialogTitle>
            <DialogDescription>{t("deleteDescription")}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowDelete(false)}>
              {t("cancel")}
            </Button>
            <Button
              variant="destructive"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  const result = await remove({ sessionId });
                  if (result.ok) setShowDelete(false);
                })
              }
            >
              {t("delete")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

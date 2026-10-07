"use client";

import { useSyncExternalStore } from "react";
import { useTranslations } from "next-intl";
import { AlertCircle, CheckCircle2, X } from "lucide-react";
import {
  NO_FEEDBACK,
  dismissFeedback,
  getFeedbackToasts,
  subscribeFeedback,
} from "@/lib/feedback";

/**
 * Shows the success and failure messages of the site's mutations, above
 * dialogs. A message leaves by itself; while a dialog is open it cannot be
 * clicked, so that closing a message never closes the dialog under it.
 */
export function FeedbackToaster() {
  const t = useTranslations("common");
  const toasts = useSyncExternalStore(
    subscribeFeedback,
    getFeedbackToasts,
    () => NO_FEEDBACK,
  );

  return (
    <div
      aria-live="polite"
      className="fixed bottom-4 right-4 z-[100] flex w-[calc(100vw-2rem)] max-w-sm flex-col gap-2"
    >
      {toasts.map((toast) => (
        <div
          key={toast.id}
          role={toast.kind === "error" ? "alert" : "status"}
          className={`flex items-start gap-3 rounded-lg border bg-background p-3 text-sm shadow-lg ${
            toast.kind === "error"
              ? "border-destructive/50 text-destructive"
              : "border-green-600/40 text-green-700 dark:text-green-400"
          }`}
        >
          {toast.kind === "error" ? (
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          ) : (
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
          )}
          <p className="flex-1 break-words">{toast.message}</p>
          <button
            type="button"
            aria-label={t("close")}
            className="shrink-0 opacity-70 hover:opacity-100"
            onClick={() => dismissFeedback(toast.id)}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      ))}
    </div>
  );
}

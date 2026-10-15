"use client";

import { useSyncExternalStore } from "react";
import { useTranslations } from "next-intl";
import { AlertCircle, CheckCircle2, X } from "lucide-react";
import {
  NO_FEEDBACK,
  dismissFeedback,
  getFeedbackToasts,
  subscribeFeedback,
  type FeedbackToast,
} from "@/lib/feedback";

/**
 * Shows the success and failure messages of the site's mutations, above
 * dialogs. A success leaves by itself after 5 s; a failure stays until its
 * cross is pressed. The cross can be pressed while a dialog is open: the
 * dialog does not take that click for a click outside it, and stays open
 * (`isFeedbackElement`, read by `components/ui/dialog.tsx`).
 *
 * Two live regions, present before any message: the successes are announced
 * politely (`role="status"`), the failures at once (`role="alert"`).
 */
export function FeedbackToaster() {
  const t = useTranslations("common");
  const toasts = useSyncExternalStore(
    subscribeFeedback,
    getFeedbackToasts,
    () => NO_FEEDBACK,
  );

  const list = (kind: FeedbackToast["kind"]) =>
    toasts
      .filter((toast) => toast.kind === kind)
      .map((toast) => (
        <div
          key={toast.id}
          data-kind={toast.kind}
          // A modal dialog turns pointer events off on the page: a message,
          // drawn above it, turns them on for itself.
          style={{ pointerEvents: "auto" }}
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
      ));

  return (
    <div
      data-feedback-toaster=""
      style={{ pointerEvents: "none" }}
      className="fixed bottom-4 right-4 z-[100] flex w-[calc(100vw-2rem)] max-w-sm flex-col gap-2"
    >
      <div
        role="status"
        aria-live="polite"
        aria-atomic="false"
        className="flex flex-col gap-2"
      >
        {list("success")}
      </div>
      <div
        role="alert"
        aria-live="assertive"
        aria-atomic="false"
        className="flex flex-col gap-2"
      >
        {list("error")}
      </div>
    </div>
  );
}

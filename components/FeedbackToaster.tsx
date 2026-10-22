"use client";

import { useEffect, useRef, useSyncExternalStore } from "react";
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
 * (`isFeedbackElement`, read by DialogContent and SheetContent).
 *
 * Two live regions, present before any message: the successes are announced
 * politely (`role="status"`), the failures at once (`role="alert"`).
 * Keep both mounted with aria-live even when empty, before any modal opens:
 * Radix's hideOthers exempts existing [aria-live] regions. A region mounted
 * later, or without aria-live, would be hidden with the page. This exposure
 * was checked in Chromium's accessibility tree, not with a screen reader.
 */
export function FeedbackToaster() {
  const t = useTranslations("common");
  const toasts = useSyncExternalStore(
    subscribeFeedback,
    getFeedbackToasts,
    () => NO_FEEDBACK,
  );
  const stackRef = useRef<HTMLDivElement>(null);
  const hasToasts = toasts.length > 0;

  useEffect(() => {
    const stack = stackRef.current;
    if (!stack) return;
    let touchY: number | null = null;
    const scroll = (event: Event, delta: number) => {
      const limit = Math.max(0, stack.scrollHeight - stack.clientHeight);
      const next = Math.max(0, Math.min(limit, stack.scrollTop + delta));
      if (next !== stack.scrollTop) {
        stack.scrollTop = next;
        event.preventDefault();
      }
    };
    const wheel = (event: WheelEvent) => {
      const line = Number.parseFloat(getComputedStyle(stack).lineHeight) || 16;
      const unit = event.deltaMode === 1 ? line : event.deltaMode === 2 ? stack.clientHeight : 1;
      scroll(event, event.deltaY * unit);
    };
    const start = (event: TouchEvent) => {
      touchY = event.touches.length === 1 ? event.touches[0].clientY : null;
    };
    const move = (event: TouchEvent) => {
      if (event.touches.length !== 1) return;
      const next = event.touches[0].clientY;
      if (touchY !== null) scroll(event, touchY - next);
      touchY = next;
    };
    stack.addEventListener("wheel", wheel, { passive: false });
    stack.addEventListener("touchstart", start, { passive: false });
    stack.addEventListener("touchmove", move, { passive: false });
    return () => {
      stack.removeEventListener("wheel", wheel);
      stack.removeEventListener("touchstart", start);
      stack.removeEventListener("touchmove", move);
    };
  }, []);

  useEffect(() => {
    const stack = stackRef.current;
    if (!hasToasts || !stack || typeof ResizeObserver === "undefined") return;
    const root = document.documentElement;
    const measure = () => {
      const bottom = Number.parseFloat(getComputedStyle(stack).bottom) || 0;
      root.style.setProperty(
        "--feedback-toaster-space",
        `${stack.getBoundingClientRect().height + bottom + 8}px`,
      );
    };
    const observer = new ResizeObserver(measure);
    observer.observe(stack);
    measure();
    return () => {
      observer.disconnect();
      root.style.removeProperty("--feedback-toaster-space");
    };
  }, [hasToasts]);

  const list = (kind: FeedbackToast["kind"]) =>
    toasts
      .filter((toast) => toast.kind === kind)
      .map((toast) => (
        <div
          key={toast.id}
          data-feedback-message=""
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
      ref={stackRef}
      data-feedback-toaster=""
      style={{ pointerEvents: "none" }}
      className="fixed bottom-4 right-4 z-[100] flex max-h-[calc(50dvh-1.5rem)] w-[calc(100vw-2rem)] max-w-sm flex-col gap-2 overflow-y-auto overscroll-contain"
    >
      <div
        role="status"
        aria-live="polite"
        aria-atomic="false"
        className="flex shrink-0 flex-col gap-2"
      >
        {list("success")}
      </div>
      <div
        role="alert"
        aria-live="assertive"
        aria-atomic="false"
        className="flex shrink-0 flex-col gap-2"
      >
        {list("error")}
      </div>
    </div>
  );
}

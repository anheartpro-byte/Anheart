import { ConvexError } from "convex/values";

/**
 * Visible feedback for the dashboard's mutations: the toast list read by
 * `components/FeedbackToaster.tsx`, and the wording of a failed mutation.
 * Mutations reach it through `hooks/use-mutation-with-feedback.ts`.
 */

export type FeedbackToast = {
  id: number;
  kind: "success" | "error";
  message: string;
};

/** A success leaves by itself; a failure stays until it is closed. */
const SUCCESS_VISIBLE_MS = 5_000;
const MAX_TOASTS = 4;

/**
 * Whether a click or a focus landed on the messages (`data-feedback-toaster`
 * on their container): a dialog must not take it for a click outside itself.
 */
export function isFeedbackElement(target: EventTarget | null): boolean {
  const element = target as { closest?: unknown } | null;
  return (
    typeof element?.closest === "function" &&
    (target as Element).closest("[data-feedback-toaster], [data-feedback-message]") !== null
  );
}

/** The list before any message, also what the server renders. */
export const NO_FEEDBACK: readonly FeedbackToast[] = [];

let toasts = NO_FEEDBACK;
let nextId = 1;
const listeners = new Set<() => void>();

function publish(next: readonly FeedbackToast[]): void {
  toasts = next;
  for (const listener of listeners) listener();
}

/**
 * The list with its newest message, kept to `MAX_TOASTS`: an older success
 * leaves first, a failure only when no older success is left to go.
 */
function withNewest(
  list: readonly FeedbackToast[],
  newest: FeedbackToast,
): FeedbackToast[] {
  const kept = [...list, newest];
  while (kept.length > MAX_TOASTS) {
    const success = kept
      .slice(0, -1)
      .findIndex((toast) => toast.kind === "success");
    kept.splice(success === -1 ? 0 : success, 1);
  }
  return kept;
}

/**
 * Show a message. A success leaves after 5 s. A failure stays until its cross
 * is pressed: for some failures it is the only place the reason is shown.
 */
export function pushFeedback(kind: FeedbackToast["kind"], message: string) {
  const id = nextId++;
  publish(withNewest(toasts, { id, kind, message }));
  if (kind === "success") {
    setTimeout(() => dismissFeedback(id), SUCCESS_VISIBLE_MS);
  }
}

export function dismissFeedback(id: number): void {
  if (toasts.some((toast) => toast.id === id)) {
    publish(toasts.filter((toast) => toast.id !== id));
  }
}

export function subscribeFeedback(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getFeedbackToasts(): readonly FeedbackToast[] {
  return toasts;
}

/** The part of a next-intl translator the error wording needs. */
export type FeedbackTranslator = {
  (key: string, values?: Record<string, string | number>): string;
  has(key: string): boolean;
};

export type MutationFailure = {
  /** What the user reads. */
  message: string;
  /** Stable error code sent by the server, when there is one. */
  code?: string;
  /** Convex request identifier, to find the failure in the server logs. */
  requestId?: string;
};

/** A stable code is a single token: free text never reaches the catalog lookup. */
const CODE_PATTERN = /^[A-Za-z][A-Za-z0-9_-]*$/;
const REQUEST_ID_PATTERN = /\[Request ID: ([^\]]+)\]/;

function asCode(value: unknown): string | undefined {
  return typeof value === "string" && CODE_PATTERN.test(value)
    ? value
    : undefined;
}

/** The code and the text the server sent, as far as the client can read them. */
function readServerError(error: unknown): { code?: string; text?: string } {
  if (error instanceof ConvexError) {
    const data: unknown = error.data;
    if (typeof data === "string") return { code: asCode(data), text: data };
    if (typeof data !== "object" || data === null) return {};
    const fields = data as Record<string, unknown>;
    return {
      code: asCode(fields.code) ?? asCode(fields.error),
      text: typeof fields.message === "string" ? fields.message : undefined,
    };
  }
  if (!(error instanceof Error)) return {};
  // A development deployment appends the thrown message to the server error.
  const uncaught = error.message.match(/Uncaught \w*Error: ([^\n]+)/);
  if (uncaught) return { text: uncaught[1].trim() };
  // In production a plain Error is masked: nothing in it is meant for a user.
  if (error.message.startsWith("[CONVEX ")) return {};
  return { text: error.message || undefined };
}

/**
 * The message of a failed mutation: the translation of the server's stable
 * code when `messages/*.json` has one under `errors.<code>`, else the text the
 * server sent, else a generic sentence carrying the request identifier.
 */
export function describeMutationError(
  error: unknown,
  t: FeedbackTranslator,
): MutationFailure {
  const requestId =
    error instanceof Error
      ? error.message.match(REQUEST_ID_PATTERN)?.[1]
      : undefined;
  const { code, text } = readServerError(error);

  let message: string;
  if (code !== undefined && t.has(`errors.${code}`)) {
    message = t(`errors.${code}`);
  } else if (text) {
    message = text;
  } else if (requestId) {
    message = t("feedback.failedWithReference", { requestId });
  } else {
    message = t("feedback.failed");
  }
  return { message, code, requestId };
}

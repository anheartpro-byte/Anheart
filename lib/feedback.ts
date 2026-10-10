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

const SUCCESS_VISIBLE_MS = 5_000;
const ERROR_VISIBLE_MS = 12_000;
const MAX_TOASTS = 4;

/** The list before any message, also what the server renders. */
export const NO_FEEDBACK: readonly FeedbackToast[] = [];

let toasts = NO_FEEDBACK;
let nextId = 1;
const listeners = new Set<() => void>();

function publish(next: readonly FeedbackToast[]): void {
  toasts = next;
  for (const listener of listeners) listener();
}

export function pushFeedback(kind: FeedbackToast["kind"], message: string) {
  const id = nextId++;
  publish([...toasts, { id, kind, message }].slice(-MAX_TOASTS));
  setTimeout(
    () => dismissFeedback(id),
    kind === "error" ? ERROR_VISIBLE_MS : SUCCESS_VISIBLE_MS,
  );
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

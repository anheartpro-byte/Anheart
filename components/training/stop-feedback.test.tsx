import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { getFunctionName, type FunctionReference } from "convex/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Id } from "@/convex/_generated/dataModel";
import { dismissFeedback, getFeedbackToasts } from "@/lib/feedback";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";
import { TrainingPanel } from "./TrainingPanel";

/**
 * The message shown after "Arrêter la séance" / "Annuler la séance".
 *
 * `training.requestStop` answers the same thing whether it cancelled a pending
 * session, asked an active one to stop, or found the session already over. The
 * page cannot know which: the machine may have armed the session after the
 * page last heard of it. So the message must be the same, and true, in each
 * case.
 */

/** What each Convex query answers, by function name. */
const answers = vi.hoisted(() => new Map<string, unknown>());
const requestStop = vi.hoisted(() => vi.fn());
type DrawnButton = { variant?: string; onClick?: () => unknown };
const buttons = vi.hoisted(() => [] as DrawnButton[]);

vi.mock("convex/react", () => ({
  useQuery: (ref: FunctionReference<"query">) =>
    answers.get(getFunctionName(ref)),
  useMutation: () => requestStop,
}));
vi.mock("./TelemetryCharts", () => ({ TelemetryCharts: () => null }));
// No browser here: each button is collected with its handler instead of being
// drawn, and the confirmation window is rendered as if it were open.
vi.mock("@/components/ui/button", () => ({
  Button: (props: DrawnButton) => {
    buttons.push(props);
    return null;
  },
}));
vi.mock("@/components/ui/dialog", () => {
  const Open = ({ children }: { children?: ReactNode }) => children;
  return {
    Dialog: Open,
    DialogContent: Open,
    DialogDescription: Open,
    DialogFooter: Open,
    DialogHeader: Open,
    DialogTitle: Open,
  };
});

const NOW = 1_800_000_000_000;
const sessionId = "session-1" as Id<"sessions">;

function session(status: "pending" | "active" | "completed") {
  answers.set("training:getTrainingSession", {
    _id: sessionId,
    machineId: "machine-1",
    machineName: "Centri Paris",
    status,
    kind: "auto",
    origin: "remote",
    totalDurationS: 1800,
    startedAt: NOW - 60_000,
    endedAt: status === "completed" ? NOW - 1_000 : undefined,
    canStop: status !== "completed",
  });
  answers.set("training:getSessionTelemetry", []);
}

/** Draws the panel as the page last saw the session, then confirms the stop. */
async function confirmStop(locale: "fr" | "en" = "fr") {
  buttons.length = 0;
  renderToStaticMarkup(
    <NextIntlClientProvider
      locale={locale}
      messages={locale === "fr" ? fr : en}
      timeZone="Europe/Paris"
    >
      <TrainingPanel sessionId={sessionId} />
    </NextIntlClientProvider>,
  );
  // The red button of the confirmation window is the last one drawn.
  const confirm = buttons.filter((b) => b.variant === "destructive").at(-1);
  expect(confirm?.onClick).toBeTypeOf("function");
  await confirm?.onClick?.();
  return getFeedbackToasts().map(({ kind, message }) => ({ kind, message }));
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  answers.clear();
  requestStop.mockReset();
  requestStop.mockResolvedValue(null);
});
afterEach(() => {
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
  vi.useRealTimers();
});

describe("ANH-156 message after a stop or a cancellation is confirmed", () => {
  it("does not say the session was cancelled when the machine armed it during the call", async () => {
    // Given a session the page still shows as pending.
    session("pending");
    // When the machine arms it while the request travels: the server finds an
    // active session, records the stop request, and answers as it always does.
    requestStop.mockImplementation(async () => {
      session("active");
      return null;
    });

    const shown = await confirmStop();

    // Then the message claims nothing the page cannot know.
    expect(requestStop).toHaveBeenCalledWith({ sessionId });
    expect(shown).toEqual([
      { kind: "success", message: fr.feedback.stopRequestSent },
    ]);
    expect(shown[0].message).not.toMatch(/annul/i);
  });

  it.each(["pending", "active", "completed"] as const)(
    "is the same whatever the page believed of the session (%s)",
    async (status) => {
      session(status);

      expect(await confirmStop()).toEqual([
        { kind: "success", message: fr.feedback.stopRequestSent },
      ]);
    },
  );

  it("is shown in English too", async () => {
    session("active");

    expect(await confirmStop("en")).toEqual([
      { kind: "success", message: en.feedback.stopRequestSent },
    ]);
  });

  it.each([
    ["fr", fr.feedback.stopRequestSent],
    ["en", en.feedback.stopRequestSent],
  ])("names no outcome the server did not report (%s)", (_locale, text) => {
    // Neither cancelled, nor stopped, nor slowing down: only that the request
    // left, and where to read what happened.
    expect(text).not.toMatch(
      /annul|arrêt|décél|termin|\b(cancel|stop|decel|end)/i,
    );
  });

  it("shows the server's refusal instead when the request fails", async () => {
    session("active");
    requestStop.mockRejectedValue(new Error("offline"));
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});

    expect(await confirmStop()).toEqual([
      { kind: "error", message: "offline" },
    ]);
    logged.mockRestore();
  });
});

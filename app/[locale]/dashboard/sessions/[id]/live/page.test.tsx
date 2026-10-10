// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import { ConvexError } from "convex/values";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";
import {
  answer,
  argsAsked,
  feedbackShown,
  mutation,
  mutationsSent,
  NOW,
  propsOf,
  renderPage,
  routeParams,
  takeLoggedFailures,
  wasMounted,
} from "@/test-support/pages";
import LiveSessionPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);
// The curves are drawn by a charting library that needs a real layout.
vi.mock("@/components/training/TelemetryCharts", async () => ({
  TelemetryCharts: (await import("@/test-support/pages")).standIn(
    "TelemetryCharts",
  ),
}));

/**
 * The live view of a session: where a manager watches a machine that is
 * running, and stops it. The training panel is the real one, so that the stop
 * is the one the page really sends.
 */

type Session = NonNullable<FunctionReturnType<typeof api.sessions.getSession>>;
type Training = NonNullable<
  FunctionReturnType<typeof api.training.getTrainingSession>
>;
type Point = FunctionReturnType<
  typeof api.training.getSessionTelemetry
>[number];

const sessionId = "session-1" as Id<"sessions">;
const machineId = "machine-1" as Id<"machines">;
const STARTED = NOW - 600_000;

function session(overrides: Partial<Session> = {}): Session {
  return {
    _id: sessionId,
    _creationTime: STARTED,
    machineId,
    userId: "user-user" as Id<"users">,
    startedById: "user-gestionnaire" as Id<"users">,
    status: "active",
    startedAt: STARTED,
    endedAt: undefined,
    channels: [],
    sampleRate: undefined,
    notes: undefined,
    patient: {
      _id: "user-user" as Id<"users">,
      firstName: "Paul",
      lastName: "Patient",
      email: "paul@example.test",
    },
    kind: "auto",
    subjectLabel: undefined,
    machine: { _id: machineId, name: "Centri Paris" },
    startedBy: {
      _id: "user-gestionnaire" as Id<"users">,
      firstName: "Gaston",
      lastName: "Gestion",
    },
    ...overrides,
  };
}

function training(overrides: Partial<Training> = {}): Training {
  return {
    _id: sessionId,
    machineId,
    machineName: "Centri Paris",
    status: "active",
    kind: "auto",
    origin: "remote",
    profileId: "p-endurance",
    profileName: "Endurance",
    zoneLowBpm: 110,
    zoneHighBpm: 130,
    totalDurationS: 1800,
    subjectHrMax: 180,
    subjectLabel: undefined,
    operatorName: undefined,
    startedAt: STARTED,
    lastSignalAt: NOW - 1_000,
    lastMeasuredAt: NOW - 2_000,
    serverNow: NOW,
    endedAt: undefined,
    stopRequestedAt: undefined,
    endReason: undefined,
    canStop: true,
    ...overrides,
  };
}

const point: Point = {
  t: NOW - 2_000,
  elapsedS: 598,
  phase: "hold",
  bpm: 121,
  motorRpm: 1200,
  outputRpm: 24.1,
  setpointMotorRpm: 1200,
  gLoad: 1.42,
  safetyAction: "none",
};

/** The page of the session, as the server describes it. */
function given(
  state: {
    session?: Session | null;
    training?: Training | null;
    points?: Point[];
  } = {},
) {
  answer(
    api.sessions.getSession,
    state.session === undefined ? session() : state.session,
  );
  answer(
    api.training.getTrainingSession,
    state.training === undefined ? training() : state.training,
  );
  answer(api.training.getSessionTelemetry, state.points ?? [point]);
}

function open(locale: "fr" | "en" = "fr") {
  return renderPage(
    <LiveSessionPage params={routeParams({ id: sessionId })} />,
    { locale },
  );
}

/** The address of the link that holds `label`. */
function linkTo(label: string): string | null {
  const link = screen.getByText(label).closest("a");
  return link === null ? null : link.getAttribute("href");
}

describe("live view: which session it shows", () => {
  it("asks for the session of the address, and for its training fields", async () => {
    given();
    await open();

    expect(argsAsked(api.sessions.getSession).at(-1)).toEqual({ sessionId });
    expect(argsAsked(api.training.getTrainingSession)).toContainEqual({
      sessionId,
    });
    expect(argsAsked(api.training.getSessionTelemetry).at(-1)).toEqual({
      sessionId,
      limit: 3600,
    });
  });

  it("shows nothing of a session while it is loading", async () => {
    given();
    answer(api.sessions.getSession, undefined);
    await open();

    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.queryByText(fr.training.session.panelTitle)).toBeNull();
    expect(screen.queryByText(fr.sessions.notFound)).toBeNull();
    // The panel is not mounted: it never asked for the telemetry.
    expect(argsAsked(api.training.getSessionTelemetry)).toEqual([]);
  });

  it("says the session is not found, with the way back, when the server answers none", async () => {
    // What the server answers for a session that does not exist, and for one
    // of another organisation or that the visitor may not see.
    given({ session: null, training: null });
    await open();

    expect(screen.getByText(fr.sessions.notFound)).toBeTruthy();
    expect(linkTo(fr.common.back)).toBe("/dashboard/sessions");
    expect(screen.queryByText(fr.training.session.panelTitle)).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(argsAsked(api.training.getSessionTelemetry)).toEqual([]);
  });

  it("gives a session of the former recording mode no live view, only the way to its detail", async () => {
    given({
      session: session({ kind: "recording", status: "completed" }),
      training: null,
    });
    await open();

    expect(screen.getByText(fr.sessionLive.legacyRecording)).toBeTruthy();
    expect(linkTo(fr.sessionLive.viewDetails)).toBe(
      `/dashboard/sessions/${sessionId}`,
    );
    expect(screen.queryByText(fr.training.session.panelTitle)).toBeNull();
    expect(argsAsked(api.training.getSessionTelemetry)).toEqual([]);
  });
});

describe("live view: the header", () => {
  it("names the rider, the machine, the kind and the origin of an active session", async () => {
    given();
    await open();

    expect(
      screen.getByRole("heading", { level: 1, name: "Paul Patient" }),
    ).toBeTruthy();
    const header = within(
      screen.getByRole("heading", { level: 1 }).parentElement as HTMLElement,
    );
    expect(header.getByText(/Centri Paris/)).toBeTruthy();
    expect(header.getByText(fr.training.kind.auto)).toBeTruthy();
    expect(header.getByText(fr.training.origin.remote)).toBeTruthy();
  });

  it("shows the origin only once the training fields have arrived", async () => {
    given();
    answer(api.training.getTrainingSession, undefined);
    await open();

    const header = within(
      screen.getByRole("heading", { level: 1 }).parentElement as HTMLElement,
    );
    expect(header.getByText(fr.training.kind.auto)).toBeTruthy();
    expect(header.queryByText(fr.training.origin.remote)).toBeNull();
  });

  it.each([
    ["the label typed at the console", "Visiteur 12", "Visiteur 12"],
    ["'rider not specified'", undefined, fr.training.session.riderNotSpecified],
  ])(
    "names a session without an account by %s",
    async (_name, subjectLabel, shown) => {
      given({
        session: session({
          patient: null,
          userId: undefined,
          kind: "manual",
          subjectLabel,
        }),
        training: training({ kind: "manual", origin: "local" }),
      });
      await open();

      expect(
        screen.getByRole("heading", { level: 1, name: shown }),
      ).toBeTruthy();
    },
  );

  // Known and filed: "started ... ago" is counted on this computer's clock,
  // not on the server's. This is what the page does today.
  it("says since when an active session runs, on this computer's clock", async () => {
    given();
    const french = await open();
    expect(screen.getByText(/Démarrée il y a 10 minutes/)).toBeTruthy();
    french.unmount();

    await open("en");
    expect(screen.getByText(/Started 10 minutes ago/)).toBeTruthy();
  });

  it.each(["pending", "completed", "failed"] as const)(
    "does not say since when a %s session runs",
    async (status) => {
      given({
        session: session({ status }),
        training: training({ status }),
      });
      await open();

      expect(screen.queryByText(/Démarrée/)).toBeNull();
    },
  );

  it.each(["completed", "failed"] as const)(
    "offers the detail of a %s session",
    async (status) => {
      given({
        session: session({ status, endedAt: NOW - 60_000 }),
        training: training({ status, endedAt: NOW - 60_000, canStop: false }),
      });
      await open();

      expect(linkTo(fr.training.session.viewDetails)).toBe(
        `/dashboard/sessions/${sessionId}`,
      );
    },
  );

  it.each(["active", "pending"] as const)(
    "does not offer the detail while the session is %s",
    async (status) => {
      given({
        session: session({ status }),
        training: training({ status }),
      });
      await open();

      expect(screen.queryByText(fr.training.session.viewDetails)).toBeNull();
    },
  );
});

describe("live view: the training panel of the session", () => {
  it("shows the current values of the session and gives its points to the curves", async () => {
    given();
    await open();

    expect(screen.getByText(fr.training.session.panelTitle)).toBeTruthy();
    expect(screen.getByText("121")).toBeTruthy();
    expect(screen.getByText("24.1")).toBeTruthy();
    expect(screen.getByText(fr.training.session.inZone)).toBeTruthy();
    expect(propsOf("TelemetryCharts")).toEqual({
      points: [point],
      zoneLowBpm: 110,
      zoneHighBpm: 130,
    });
  });

  it("shows a pending session as waiting for the machine, with nothing to draw", async () => {
    given({
      session: session({ status: "pending" }),
      training: training({
        status: "pending",
        lastSignalAt: null,
        lastMeasuredAt: null,
      }),
      points: [],
    });
    await open();

    expect(screen.getByText(fr.training.session.pending)).toBeTruthy();
    expect(screen.getByText(fr.training.session.noTelemetry)).toBeTruthy();
    expect(wasMounted("TelemetryCharts")).toBe(false);
  });
});

describe("live view: stopping the session", () => {
  it("sends the stop of this session once it is confirmed, and says the request left", async () => {
    given();
    const { user } = await open();

    await user.click(
      screen.getByRole("button", { name: fr.training.session.stop }),
    );
    const confirmation = within(screen.getByRole("dialog"));
    expect(
      confirmation.getByText(fr.training.session.stopConfirm),
    ).toBeTruthy();
    expect(mutationsSent()).toEqual([]);

    await user.click(
      confirmation.getByRole("button", { name: fr.training.session.stop }),
    );

    expect(mutationsSent()).toEqual([
      { name: "training:requestStop", args: { sessionId } },
    ]);
    expect(feedbackShown()).toEqual([
      { kind: "success", message: fr.feedback.stopRequestSent },
    ]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("sends nothing when the confirmation is declined", async () => {
    given();
    const { user } = await open();

    await user.click(
      screen.getByRole("button", { name: fr.training.session.stop }),
    );
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: fr.common.cancel,
      }),
    );

    expect(mutationsSent()).toEqual([]);
    expect(feedbackShown()).toEqual([]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("cancels a session that is still pending with the same request", async () => {
    given({
      session: session({ status: "pending" }),
      training: training({
        status: "pending",
        lastSignalAt: null,
        lastMeasuredAt: null,
      }),
      points: [],
    });
    const { user } = await open();

    await user.click(
      screen.getByRole("button", { name: fr.training.session.cancel }),
    );
    const confirmation = within(screen.getByRole("dialog"));
    expect(
      confirmation.getByText(fr.training.session.cancelConfirm),
    ).toBeTruthy();
    await user.click(
      confirmation.getByRole("button", { name: fr.training.session.cancel }),
    );

    expect(mutationsSent()).toEqual([
      { name: "training:requestStop", args: { sessionId } },
    ]);
  });

  it("shows the server's refusal, on the page and as a message, and claims no stop", async () => {
    given();
    mutation(api.training.requestStop).mockRejectedValue(
      new ConvexError("Not authorized to stop this session"),
    );
    const { user } = await open();

    await user.click(
      screen.getByRole("button", { name: fr.training.session.stop }),
    );
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: fr.training.session.stop,
      }),
    );

    const refusal = "Not authorized to stop this session";
    expect(screen.getByText(refusal)).toBeTruthy();
    expect(feedbackShown()).toEqual([{ kind: "error", message: refusal }]);
    expect(takeLoggedFailures()).toEqual(["training:requestStop"]);
    expect(screen.queryByText(fr.training.session.stopRequested)).toBeNull();
  });

  it("shows the stop as requested, and its button as used, once the server has recorded it", async () => {
    given();
    const view = await open();

    given({ training: training({ stopRequestedAt: NOW }) });
    await view.refresh();

    expect(screen.getByText(fr.training.session.stopRequested)).toBeTruthy();
    expect(
      screen
        .getByRole("button", { name: fr.training.session.stop })
        .hasAttribute("disabled"),
    ).toBe(true);
  });

  it("offers no stop to a visitor the server does not let stop the session", async () => {
    given({ training: training({ canStop: false }) });
    await open();

    expect(screen.getByText(fr.training.session.panelTitle)).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: fr.training.session.stop }),
    ).toBeNull();
  });

  it("offers no stop once the session is over", async () => {
    given({
      session: session({ status: "completed", endedAt: NOW - 60_000 }),
      training: training({
        status: "completed",
        endedAt: NOW - 60_000,
        canStop: false,
      }),
    });
    await open();

    expect(screen.getByText(fr.training.session.completed)).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: fr.training.session.stop }),
    ).toBeNull();
  });

  it("is in English for an English visitor", async () => {
    given();
    const { user } = await open("en");

    await user.click(
      screen.getByRole("button", { name: en.training.session.stop }),
    );
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: en.training.session.stop,
      }),
    );

    expect(feedbackShown()).toEqual([
      { kind: "success", message: en.feedback.stopRequestSent },
    ]);
  });
});

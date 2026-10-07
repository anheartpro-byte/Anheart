import { click, messages, render } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  answer,
  mutation,
  mutationCalls,
  resetConvex,
} from "@/test-support/convex";
import { closeWindow, windowOpen } from "@/test-support/ui";
import type { Id } from "@/convex/_generated/dataModel";
import { dismissFeedback, getFeedbackToasts } from "@/lib/feedback";
import { forgetServerAnswers } from "@/lib/server-clock";
import type { TelemetryPoint } from "@/lib/training";
import { TrainingPanel } from "./TrainingPanel";

/**
 * The panel of a session, as a manager uses it: the stop control from the
 * first click to the answer of the server, who is shown that control, and
 * what the panel says of a session in each of its states.
 *
 * The freshness of what it shows (clocks, late points, silence) is proven in
 * live-freshness.test.tsx, and the wording after a stop in
 * stop-feedback.test.tsx: neither is repeated here. This file adds the
 * confirmation window with its real state (it opens on a click, it closes on
 * the answer), which those two could not follow.
 */

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/convex")).convexReact,
);
vi.mock(
  "@/components/ui/dialog",
  async () => (await import("@/test-support/ui")).dialog,
);
// The charts need a browser to size themselves: what they are given is read instead.
const charts = vi.hoisted(() => ({ props: [] as unknown[] }));
vi.mock("./TelemetryCharts", () => ({
  TelemetryCharts: (props: unknown) => {
    charts.props.push(props);
    return <div data-charts="" />;
  },
}));

const fr = messages.fr;
const t = fr.training.session;
const STOP = "training:requestStop";
const NOW = 1_800_000_000_000;
const sessionId = "session-1" as Id<"sessions">;

type Session = {
  status: "pending" | "active" | "completed" | "failed";
  kind: "auto" | "manual";
  canStop: boolean;
  startedAt: number;
  endedAt?: number;
  totalDurationS?: number;
  zoneLowBpm?: number;
  zoneHighBpm?: number;
  stopRequestedAt?: number;
  endReason?: string;
  profileName?: string;
  operatorName?: string;
};

/** The session as `training.getTrainingSession` answers it, heard from a moment ago. */
function session(over: Partial<Session> = {}) {
  answer("training:getTrainingSession", {
    _id: sessionId,
    machineId: "machine-1",
    machineName: "Centri Paris",
    status: "active",
    kind: "auto",
    origin: "remote",
    canStop: true,
    startedAt: NOW - 125_000,
    totalDurationS: 1800,
    zoneLowBpm: 110,
    zoneHighBpm: 140,
    lastSignalAt: NOW - 1_000,
    lastMeasuredAt: NOW - 1_000,
    serverNow: NOW,
    ...over,
  });
}

function point(over: Partial<TelemetryPoint> = {}): TelemetryPoint {
  return {
    t: NOW - 1_000,
    elapsedS: 124,
    phase: "hold",
    bpm: 128,
    motorRpm: 996,
    outputRpm: 20.0,
    setpointMotorRpm: 1000,
    gLoad: 1.52,
    safetyAction: "none",
    ...over,
  };
}

function telemetry(points: TelemetryPoint[] | undefined) {
  answer("training:getSessionTelemetry", points);
}

const panel = () => render(<TrainingPanel sessionId={sessionId} />);

function shown() {
  return getFeedbackToasts().map(({ kind, message }) => ({ kind, message }));
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  forgetServerAnswers();
  resetConvex();
  charts.props.length = 0;
});
afterEach(() => {
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
  vi.useRealTimers();
});

describe("ANH-203 training panel: stopping a session", () => {
  it("asks for a confirmation first: the first click sends nothing", async () => {
    session();
    telemetry([point()]);
    const screen = panel();
    expect(windowOpen(screen)).toBe(false);

    await click(screen.button(t.stop));

    expect(windowOpen(screen)).toBe(true);
    expect(screen.text()).toContain(t.stopConfirm);
    expect(screen.text()).toContain(t.stopConfirmDesc);
    expect(mutationCalls()).toEqual({});
  });

  it("sends the stop request for this session once confirmed, then closes the window", async () => {
    session();
    telemetry([point()]);
    const screen = panel();
    await click(screen.button(t.stop));

    // Two buttons read "Arrêter la séance" now: the one of the panel, then the one of the window.
    const [, confirm] = screen.all(
      (element) =>
        element.localName === "button" && screen.textOf(element) === t.stop,
    );
    await click(confirm);

    expect(mutationCalls()).toEqual({ [STOP]: [[{ sessionId }]] });
    expect(windowOpen(screen)).toBe(false);
    expect(shown()).toEqual([
      { kind: "success", message: fr.feedback.stopRequestSent },
    ]);
  });

  it.each([
    [
      "Cancel",
      (screen: ReturnType<typeof panel>) =>
        click(screen.button(fr.common.cancel)),
    ],
    [
      "the window's own close control",
      (screen: ReturnType<typeof panel>) => closeWindow(screen),
    ],
  ])("sends nothing when the window is left by %s", async (_how, leave) => {
    session();
    telemetry([point()]);
    const screen = panel();
    await click(screen.button(t.stop));

    await leave(screen);

    expect(windowOpen(screen)).toBe(false);
    expect(mutationCalls()).toEqual({});
    // The session can still be stopped afterwards.
    expect(screen.button(t.stop).hasAttribute("disabled")).toBe(false);
  });

  it("cannot be sent twice while the server has not answered", async () => {
    session();
    telemetry([point()]);
    let answerStop: (value: null) => void = () => {};
    mutation(STOP).mockImplementation(
      () => new Promise((resolve) => (answerStop = resolve)),
    );
    const screen = panel();
    await click(screen.button(t.stop));
    const buttons = () =>
      screen.all(
        (element) =>
          element.localName === "button" && screen.textOf(element) === t.stop,
      );

    await click(buttons()[1]);
    // While the request travels both buttons are disabled.
    expect(buttons().map((button) => button.hasAttribute("disabled"))).toEqual([
      true,
      true,
    ]);
    await click(buttons()[1]);

    expect(mutation(STOP)).toHaveBeenCalledTimes(1);
    answerStop(null);
  });

  it("shows the server's refusal in the panel, closes the window and lets the manager ask again", async () => {
    session();
    telemetry([point()]);
    mutation(STOP).mockRejectedValueOnce(
      new Error("Not authorized to stop this session"),
    );
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const screen = panel();
    await click(screen.button(t.stop));
    await click(
      screen.all(
        (element) =>
          element.localName === "button" && screen.textOf(element) === t.stop,
      )[1],
    );

    expect(screen.text()).toContain("Not authorized to stop this session");
    expect(shown()).toEqual([
      { kind: "error", message: "Not authorized to stop this session" },
    ]);
    expect(windowOpen(screen)).toBe(false);
    expect(screen.button(t.stop).hasAttribute("disabled")).toBe(false);
    logged.mockRestore();
  });

  it("once a stop was requested, says so and offers no second request", () => {
    session({ stopRequestedAt: NOW - 2_000 });
    telemetry([point()]);

    const screen = panel();

    expect(screen.text()).toContain(t.stopRequested);
    expect(screen.button(t.stop).hasAttribute("disabled")).toBe(true);
  });
});

describe("ANH-203 training panel: who is shown the stop control", () => {
  it("someone the server does not allow to stop sees no stop button", () => {
    session({ canStop: false });
    telemetry([point()]);

    const screen = panel();

    expect(screen.hasButton(t.stop)).toBe(false);
    expect(screen.hasButton(t.cancel)).toBe(false);
    expect(screen.tag("button")).toEqual([]);
  });

  it("a session that is over offers nothing to stop", () => {
    session({ status: "completed", canStop: false, endedAt: NOW - 5_000 });
    telemetry([point()]);

    const screen = panel();

    expect(screen.tag("button")).toEqual([]);
  });

  it("a session not yet armed is cancelled, not stopped: other words, same request", async () => {
    session({ status: "pending" });
    telemetry([]);
    const screen = panel();
    expect(screen.text()).toContain(t.pending);
    expect(screen.hasButton(t.stop)).toBe(false);

    await click(screen.button(t.cancel));
    expect(screen.text()).toContain(t.cancelConfirm);
    expect(screen.text()).toContain(t.cancelConfirmDesc);
    expect(screen.text()).not.toContain(t.stopConfirmDesc);
    await click(
      screen.all(
        (element) =>
          element.localName === "button" && screen.textOf(element) === t.cancel,
      )[1],
    );

    expect(mutationCalls()).toEqual({ [STOP]: [[{ sessionId }]] });
  });
});

describe("ANH-203 training panel: what it says of a session", () => {
  it("shows a placeholder while the session loads, and nothing for a session this person cannot see", () => {
    answer("training:getTrainingSession", undefined);
    const loading = panel();
    expect(loading.text()).toBe("");
    expect(
      loading.all(
        (element) => element.getAttribute("data-slot") === "skeleton",
      ),
    ).toHaveLength(1);
    loading.unmount();

    answer("training:getTrainingSession", null);
    const hidden = panel();
    expect(hidden.container.childNodes).toEqual([]);
  });

  it("names the programme, the target zone and the operator when the session has them", () => {
    session({ profileName: "Endurance", operatorName: "Ada Lovelace" });
    telemetry([point()]);

    const screen = panel();

    expect(screen.text()).toContain(`${t.panelTitle} · Endurance`);
    expect(screen.text()).toContain("Cible 110-140 bpm");
    expect(screen.text()).toContain(`· ${t.operator} Ada Lovelace`);
  });

  it("a manual session has no zone, no operator line, and says where it is driven from", () => {
    session({
      kind: "manual",
      zoneLowBpm: undefined,
      zoneHighBpm: undefined,
      totalDurationS: undefined,
      canStop: false,
    });
    telemetry([point({ bpm: 128 })]);

    const screen = panel();

    expect(screen.text()).toContain(fr.training.manualOnlyAtMachine);
    expect(screen.text()).not.toContain("Cible");
    expect(screen.text()).not.toContain(t.operator);
    // The heart rate is shown without a verdict on a zone, and no remaining time is invented.
    expect(screen.text()).toContain("128 bpm");
    expect(screen.text()).not.toContain(t.inZone);
    expect(screen.text()).not.toContain(t.remaining);
  });

  it.each([
    [104, t.belowZone, "text-blue-600"],
    [110, t.inZone, "text-green-600"],
    [140, t.inZone, "text-green-600"],
    [141, t.aboveZone, "text-red-600"],
  ])(
    "a heart rate of %i against a zone of 110 to 140 reads « %s »",
    (bpm, verdict, color) => {
      session();
      telemetry([point({ bpm })]);

      const screen = panel();

      expect(screen.text()).toContain(`${bpm} bpm`);
      expect(screen.text()).toContain(verdict);
      expect(screen.reading(String(bpm))[0].className).toContain(color);
    },
  );

  it("without a reliable heart rate, says so instead of judging the zone", () => {
    session();
    telemetry([point({ bpm: undefined })]);

    const screen = panel();

    expect(screen.text()).toContain(fr.training.live.noHeartRate);
    expect(screen.text()).not.toContain(t.inZone);
  });

  it("shows the arm speed, the motor speed, the load and the phase of the latest point", () => {
    session();
    telemetry([
      point({ phase: "warmup" }),
      point({ outputRpm: 21.456, motorRpm: 1068.4, gLoad: 1.756 }),
    ]);

    const screen = panel();

    expect(screen.text()).toContain("21.5 tr/min");
    expect(screen.text()).toContain("Moteur 1068 tr/min");
    expect(screen.text()).toContain("1.76 g");
    expect(screen.text()).toContain(fr.training.phase.hold);
    expect(screen.text()).not.toContain(fr.training.live.safetyAction);
  });

  it("names the safety action the machine reports", () => {
    session();
    telemetry([point({ safetyAction: "ramp_down" })]);

    const screen = panel();

    expect(screen.text()).toContain(
      `${fr.training.live.safetyAction} : ${fr.training.safety.ramp_down}`,
    );
  });

  it("counts the elapsed and the remaining time of an active session on the server's clock", () => {
    // Started 125 s ago, planned for 30 minutes.
    session();
    telemetry([point()]);

    const screen = panel();

    expect(screen.text()).toContain(`2:05 ${t.elapsed}`);
    expect(screen.text()).toContain(`${t.remaining} 27:55`);
  });

  it("a pending session has not started: zero elapsed, the whole duration remaining", () => {
    session({ status: "pending" });
    telemetry([]);

    const screen = panel();

    expect(screen.text()).toContain(`0:00 ${t.elapsed}`);
    expect(screen.text()).toContain(`${t.remaining} 30:00`);
    expect(screen.text()).toContain(t.noTelemetry);
  });

  it("a completed session shows how long it lasted and why it ended", () => {
    session({
      status: "completed",
      canStop: false,
      startedAt: NOW - 600_000,
      endedAt: NOW - 300_000,
      endReason: "operator_stop",
    });
    telemetry([point({ t: NOW - 300_000 })]);

    const screen = panel();

    expect(screen.text()).toContain(t.completed);
    expect(screen.text()).toContain("operator_stop");
    expect(screen.text()).toContain(`5:00 ${t.elapsed}`);
    expect(screen.text()).not.toContain(t.failed);
  });

  it("a completed session without an end date or a reason shows neither", () => {
    session({ status: "completed", canStop: false });
    telemetry([]);

    const screen = panel();

    expect(screen.text()).toContain(t.completed);
    expect(screen.text()).toContain(`0:00 ${t.elapsed}`);
  });

  it.each([
    ["with the reason the machine gave", "drive_fault", true],
    ["without a reason", undefined, false],
  ])("a failed session is said failed, %s", (_case, endReason, hasReason) => {
    session({
      status: "failed",
      canStop: false,
      endedAt: NOW - 1_000,
      endReason,
    });
    telemetry([]);

    const screen = panel();

    expect(screen.text()).toContain(t.failed);
    expect(screen.text().includes("drive_fault")).toBe(hasReason);
    expect(screen.text()).not.toContain(t.completed);
  });

  it("hands the charts every point and the zone, and shows a placeholder while the points load", () => {
    session();
    telemetry(undefined);
    const screen = panel();
    expect(charts.props).toEqual([]);
    expect(screen.text()).not.toContain(t.noTelemetry);

    const points = [point({ t: NOW - 6_000 }), point()];
    telemetry(points);
    screen.rerender(<TrainingPanel sessionId={sessionId} />);

    expect(charts.props.at(-1)).toEqual({
      points,
      zoneLowBpm: 110,
      zoneHighBpm: 140,
    });
  });
});

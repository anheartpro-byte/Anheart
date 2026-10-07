import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { getFunctionName, type FunctionReference } from "convex/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Id } from "@/convex/_generated/dataModel";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";
import { forgetServerAnswers } from "@/lib/server-clock";
import {
  LIVE_FRESH_MS,
  TELEMETRY_FRESH_MS,
  type LiveState,
} from "@/lib/training";
import { buttonDisabled, textOf } from "../markup.test-helpers";
import { LaunchableMachineCard } from "./LaunchableMachineCard";
import { MachineLiveCard } from "./MachineLiveCard";
import { TrainingPanel } from "./TrainingPanel";

/**
 * What the pages show of a machine or a session as time passes, whatever the
 * clock of the computer that displays them and whatever the clock of the
 * machine. Only the server dates anything here: `SERVER` is its clock when a
 * test starts, and `elapse` makes it run together with the computer's.
 */

/** What each Convex query answers in a test, by function name. */
const answers = vi.hoisted(() => new Map<string, unknown>());

vi.mock("convex/react", () => ({
  useQuery: (ref: FunctionReference<"query">) =>
    answers.get(getFunctionName(ref)),
  useMutation: () => async () => null,
}));
vi.mock("@/i18n/navigation", () => ({
  Link: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("./TelemetryCharts", () => ({ TelemetryCharts: () => null }));

const SERVER = 1_800_000_000_000;
const machineId = "machine-1" as Id<"machines">;
const sessionId = "session-1" as Id<"sessions">;

/** How far the computer's clock is from the server's. */
const VIEWER_CLOCKS = [
  ["on time", 0],
  ["15 s ahead", 15_000],
  ["80 s ahead", 80_000],
  ["10 min ahead", 600_000],
  ["10 min behind", -600_000],
] as const;

/** How far the machine's clock is from the server's. */
const MACHINE_CLOCKS = [
  ["on time", 0],
  ["10 min ahead", 600_000],
  ["10 min behind", -600_000],
] as const;

/** The texts these tests rely on, as the French site shows them. */
const LIVE = "En direct";
const STALE = "Données périmées";
const ONLINE = "En ligne";
const OFFLINE = "Hors ligne";
const NO_SIGNAL =
  "Aucun signal récent de la machine : les valeurs affichées peuvent être dépassées.";
const NEVER_REPORTED = "La machine n&#x27;a encore rapporté aucun état.";
const STOPPED_SENDING =
  "Aucun état en direct : la machine n&#x27;envoie plus de signal.";
/** The pulsing green dot of the "live" badge. */
const LIVE_DOT = "bg-green-400";
/** How a stale value is greyed. */
const GREYED = "opacity-60";

/** The server's clock: it runs with `elapse`. */
let server = SERVER;

/** Time passes, for the server and for the computer. No datum changes. */
function elapse(ms: number) {
  server += ms;
  vi.advanceTimersByTime(ms);
}

/** The page is open on a computer whose clock is `offsetMs` away from the server's. */
function viewerClock(offsetMs: number) {
  vi.setSystemTime(server + offsetMs);
}

function liveState(ageMs: number): LiveState {
  return {
    runMode: "seance",
    phase: "hold",
    bpm: 148,
    motorRpm: 1200,
    outputRpm: 24.1,
    setpointMotorRpm: 1200,
    gLoad: 1.42,
    safetyAction: "none",
    updatedAt: server - ageMs,
  };
}

/** The page as the browser paints it at this moment. */
function paint(node: ReactNode, locale: "fr" | "en" = "fr") {
  const html = renderToStaticMarkup(
    <NextIntlClientProvider
      locale={locale}
      messages={locale === "fr" ? fr : en}
      timeZone="Europe/Paris"
    >
      {node}
    </NextIntlClientProvider>,
  );
  return { html, text: textOf(html) };
}

/**
 * The page as it paints `ms` after it received the answers it holds: nothing
 * new has arrived in between (the machine is silent, no query runs again).
 */
function paintAfter(ms: number, node: ReactNode) {
  paint(node);
  elapse(ms);
  return paint(node);
}

beforeEach(() => {
  server = SERVER;
  vi.useFakeTimers();
  vi.setSystemTime(SERVER);
  forgetServerAnswers();
  answers.clear();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("machine page: live state card", () => {
  /** getMachineLive as it last ran: it does not run again while the machine is silent. */
  function machineLive(ageMs: number, serverStale = false) {
    answers.set("training:getMachineLive", {
      status: "online",
      programsEnabled: true,
      live: liveState(ageMs),
      stale: serverStale,
      serverNow: server,
    });
  }
  const card = <MachineLiveCard machineId={machineId} />;

  it("shows a machine heard 30 s ago as live", () => {
    machineLive(30_000);
    const { html, text } = paint(card);
    expect(text).toContain(LIVE);
    expect(html).toContain(LIVE_DOT);
    expect(text).not.toContain(STALE);
    expect(text).not.toContain(NO_SIGNAL);
    expect(html).not.toContain(GREYED);
  });

  it("shows stale data, greyed, with the no-signal notice, 90 s after the last state even though the query still says fresh", () => {
    machineLive(5000);
    expect(paintAfter(LIVE_FRESH_MS - 6000, card).text).toContain(LIVE);
    elapse(1000);
    const { html, text } = paint(card);
    expect(text).toContain(STALE);
    expect(text).toContain(NO_SIGNAL);
    expect(html).toContain(GREYED);
    expect(text).not.toContain(LIVE);
    expect(html).not.toContain(LIVE_DOT);
    // The heart is no longer drawn as beating.
    expect(html).not.toContain("text-red-500");
  });

  it("shows as stale, at once, a state the server says is 90 s old when the page opens", () => {
    machineLive(LIVE_FRESH_MS);
    const { text } = paint(card);
    expect(text).toContain(STALE);
    expect(text).not.toContain(LIVE);
  });

  it("keeps the server's stale verdict", () => {
    machineLive(10_000, true);
    const { html, text } = paint(card);
    expect(text).toContain(STALE);
    expect(text).toContain(NO_SIGNAL);
    expect(html).not.toContain(LIVE_DOT);
  });

  it("keeps counting how long ago the state was updated while the machine is silent", () => {
    machineLive(5000);
    expect(paint(card).text).toContain("Mis à jour il y a moins d’une minute");
    elapse(180_000);
    expect(paint(card).text).toContain("Mis à jour il y a 3 minutes");
  });

  it("says so in English too", () => {
    machineLive(LIVE_FRESH_MS);
    const { text } = paint(card, "en");
    expect(text).toContain("Stale");
    expect(text).toContain("No recent heartbeat from the machine");
  });

  describe.each(VIEWER_CLOCKS)("on a computer whose clock is %s", (_n, off) => {
    it("shows a machine that sends as live, with its age on the server's clock", () => {
      viewerClock(off);
      machineLive(9000);
      const { text } = paint(card);
      expect(text).toContain(LIVE);
      expect(text).not.toContain(STALE);
      expect(text).toContain("Mis à jour il y a moins d’une minute");
    });

    it("shows a machine that has stopped sending as stale at 90 s, not later", () => {
      viewerClock(off);
      machineLive(0);
      expect(paintAfter(LIVE_FRESH_MS - 1000, card).text).toContain(LIVE);
      elapse(1000);
      const { text } = paint(card);
      expect(text).toContain(STALE);
      expect(text).not.toContain(LIVE);
    });
  });
});

describe("my machines: one card per machine", () => {
  type Shown = Parameters<typeof LaunchableMachineCard>[0]["machine"];

  /** One entry of listLaunchableMachines as it last ran. */
  function machine(over: Partial<Shown> = {}): Shown {
    return {
      _id: machineId,
      name: "Centri Paris",
      location: "Paris",
      status: "online",
      lastHeartbeat: server,
      serverNow: server,
      programsEnabled: true,
      live: liveState(0),
      profiles: [
        {
          profileId: "p1",
          name: "Jog",
          totalDurationS: 1800,
          zoneLowBpm: 145,
          zoneHighBpm: 155,
          hardMaxBpm: 170,
          criticalBpm: 180,
          subjectHrMax: 190,
          minRunRpm: 5,
          maxRpm: 30,
        },
      ],
      myHrMax: null,
      ...over,
    };
  }
  const card = (m: Shown) => (
    <LaunchableMachineCard machine={m} isUser={false} onLaunch={() => {}} />
  );
  /** Whether the launch button is drawn disabled. */
  const launchDisabled = (html: string) =>
    buttonDisabled(html, "Lancer une séance");

  it("shows a machine heard 30 s ago as online and live, ready to launch", () => {
    const m = machine({
      lastHeartbeat: server - 30_000,
      live: liveState(30_000),
    });
    const { html, text } = paint(card(m));
    expect(text).toContain(ONLINE);
    expect(text).toContain(LIVE);
    expect(text).not.toContain(OFFLINE);
    expect(text).not.toContain(STALE);
    expect(html).not.toContain(GREYED);
    expect(launchDisabled(html)).toBe(false);
  });

  it("shows a machine offline and its data stale at the same second, 90 s after its last signal, while its record still says online", () => {
    const m = machine();
    const before = paintAfter(LIVE_FRESH_MS - 1000, card(m));
    expect(before.text).toContain(ONLINE);
    expect(before.text).toContain(LIVE);

    elapse(1000);
    const { html, text } = paint(card(m));
    expect(text).toContain(OFFLINE);
    expect(text).toContain(STALE);
    expect(text).toContain(NO_SIGNAL);
    expect(html).toContain(GREYED);
    // Never "online" next to "stale", nor "live" next to "offline".
    expect(text).not.toContain(ONLINE);
    expect(text).not.toContain(LIVE);
    expect(html).not.toContain(LIVE_DOT);
    // The card says why nothing can be launched, and offers no launch.
    expect(text).toContain("La machine est hors ligne.");
    expect(launchDisabled(html)).toBe(true);
  });

  it("does not show as live a state older than the machine's last signal", () => {
    // The machine still signals but no longer reports its state.
    const m = machine({ live: liveState(LIVE_FRESH_MS) });
    const { text } = paint(card(m));
    expect(text).toContain(ONLINE);
    expect(text).toContain(STALE);
    expect(text).not.toContain(LIVE);
  });

  it("does not say a machine that went offline never reported a state", () => {
    // The server withholds the state once it is stale.
    const m = machine({
      status: "offline",
      lastHeartbeat: server - 300_000,
      live: null,
    });
    const { html, text } = paint(card(m));
    expect(text).toContain(OFFLINE);
    expect(html).toContain(STOPPED_SENDING);
    expect(html).not.toContain(NEVER_REPORTED);
  });

  it("says so as soon as the signal is stale, before the record says offline", () => {
    const m = machine({ lastHeartbeat: server - LIVE_FRESH_MS, live: null });
    const { html, text } = paint(card(m));
    expect(text).toContain(OFFLINE);
    expect(html).toContain(STOPPED_SENDING);
    expect(html).not.toContain(NEVER_REPORTED);
  });

  it("still says of a machine that never connected that it has reported no state", () => {
    const m = machine({ status: "offline", lastHeartbeat: 0, live: null });
    const { html } = paint(card(m));
    expect(html).toContain(NEVER_REPORTED);
    expect(html).not.toContain(STOPPED_SENDING);
  });

  it("says so in English too", () => {
    const m = machine({
      status: "offline",
      lastHeartbeat: server - 300_000,
      live: null,
    });
    expect(paint(card(m), "en").text).toContain(
      "No live state: the machine has stopped sending.",
    );
  });

  describe.each(VIEWER_CLOCKS)("on a computer whose clock is %s", (_n, off) => {
    it("shows a machine that sends as online and live", () => {
      viewerClock(off);
      const m = machine({
        lastHeartbeat: server - 9000,
        live: liveState(9000),
      });
      const { text } = paint(card(m));
      expect(text).toContain(ONLINE);
      expect(text).toContain(LIVE);
      expect(text).not.toContain(OFFLINE);
      expect(text).not.toContain(STALE);
    });

    it("shows a machine that has stopped sending as offline and stale at 90 s, not later", () => {
      viewerClock(off);
      const m = machine();
      expect(paintAfter(LIVE_FRESH_MS - 1000, card(m)).text).toContain(LIVE);
      elapse(1000);
      const { text } = paint(card(m));
      expect(text).toContain(OFFLINE);
      expect(text).toContain(STALE);
      expect(text).not.toContain(ONLINE);
      expect(text).not.toContain(LIVE);
    });
  });
});

describe("live view: training panel", () => {
  type Telemetry = "none" | "loading" | { receivedAgoMs: number };

  /**
   * getTrainingSession and getSessionTelemetry as they last ran. The server
   * dates the reception of the last point (`lastSignalAt`); the point's own
   * `t` is the machine's clock, `machineClockMs` away from the server's.
   */
  function session(
    status: string,
    telemetry: Telemetry,
    { machineClockMs = 0, startedAgoMs = 600_000 } = {},
  ) {
    const startedAt = server - startedAgoMs;
    const received =
      typeof telemetry === "object" ? server - telemetry.receivedAgoMs : null;
    answers.set("training:getTrainingSession", {
      _id: sessionId,
      machineId,
      machineName: "Centri Paris",
      status,
      kind: "auto",
      origin: "remote",
      zoneLowBpm: 145,
      zoneHighBpm: 155,
      totalDurationS: 1800,
      startedAt,
      lastSignalAt: status === "active" ? (received ?? startedAt) : null,
      serverNow: server,
      endedAt: status === "active" ? undefined : server - 300_000,
      canStop: status === "active",
    });
    answers.set(
      "training:getSessionTelemetry",
      telemetry === "loading"
        ? undefined
        : received === null
          ? []
          : [
              {
                t: received + machineClockMs,
                elapsedS: 600,
                phase: "hold",
                bpm: 148,
                motorRpm: 1200,
                outputRpm: 24.1,
                setpointMotorRpm: 1200,
                gLoad: 1.42,
                safetyAction: "none",
              },
            ],
    );
  }
  const panel = <TrainingPanel sessionId={sessionId} />;

  function expectCurrent({ html, text }: ReturnType<typeof paint>) {
    expect(text).toContain("148");
    expect(text).toContain("24.1");
    expect(text).not.toContain(NO_SIGNAL);
    expect(html).not.toContain(GREYED);
    expect(html).toContain("animate-spin");
  }

  function expectSilent({ html, text }: ReturnType<typeof paint>) {
    expect(text).toContain(NO_SIGNAL);
    expect(html).toContain(GREYED);
    // No number stands for "now", and the arm is no longer drawn as turning.
    expect(text).not.toContain("148");
    expect(text).not.toContain("24.1");
    expect(html).not.toContain("animate-spin");
  }

  it("shows the values of a point received 5 s ago, without notice", () => {
    session("active", { receivedAgoMs: 5000 });
    expectCurrent(paint(panel));
  });

  it("greys the readouts and shows the no-signal notice 20 s after the last point of an active session", () => {
    session("active", { receivedAgoMs: 0 });
    expectCurrent(paintAfter(TELEMETRY_FRESH_MS - 1000, panel));
    elapse(1000);
    expectSilent(paint(panel));
  });

  it("shows the notice at once when the server says the last point is 20 s old", () => {
    session("active", { receivedAgoMs: TELEMETRY_FRESH_MS });
    expectSilent(paint(panel));
  });

  it("shows the notice for an active session that has sent no point for 20 s", () => {
    session("active", "none");
    const { html, text } = paint(panel);
    expect(text).toContain(NO_SIGNAL);
    expect(html).toContain(GREYED);
  });

  it("gives a session that has just started 20 s to send its first point", () => {
    session("active", "none", { startedAgoMs: 0 });
    expect(paintAfter(TELEMETRY_FRESH_MS - 1000, panel).text).not.toContain(
      NO_SIGNAL,
    );
    elapse(1000);
    expect(paint(panel).text).toContain(NO_SIGNAL);
  });

  it("shows no notice while the telemetry of a session that sends is still loading", () => {
    // The page has just opened on a session started ten minutes ago: the
    // session is known, its points are not there yet.
    session("active", "loading");
    answers.set("training:getTrainingSession", {
      ...(answers.get("training:getTrainingSession") as object),
      lastSignalAt: server - 3000,
    });
    const { html, text } = paint(panel);
    expect(text).not.toContain(NO_SIGNAL);
    expect(html).not.toContain(GREYED);
    // No value is invented meanwhile.
    expect(text).not.toContain("148");
  });

  it("leaves a finished session alone: its last values are its result", () => {
    session("completed", { receivedAgoMs: 300_000 });
    const { html, text } = paintAfter(600_000, panel);
    expect(text).toContain("148");
    expect(text).not.toContain(NO_SIGNAL);
    expect(html).not.toContain(GREYED);
  });

  describe.each(MACHINE_CLOCKS)(
    "when the machine's clock is %s",
    (_name, machineClockMs) => {
      it("shows the values of a session that sends", () => {
        session("active", { receivedAgoMs: 4000 }, { machineClockMs });
        expectCurrent(paint(panel));
      });

      it("stops showing the last values as current 20 s after the last point received, not later", () => {
        session("active", { receivedAgoMs: 0 }, { machineClockMs });
        expectCurrent(paintAfter(TELEMETRY_FRESH_MS - 1000, panel));
        elapse(1000);
        expectSilent(paint(panel));
        elapse(600_000);
        expectSilent(paint(panel));
      });
    },
  );

  describe.each(VIEWER_CLOCKS)("on a computer whose clock is %s", (_n, off) => {
    it("shows the values of a session that sends, and its time on the server's clock", () => {
      viewerClock(off);
      session("active", { receivedAgoMs: 4000 });
      const view = paint(panel);
      expectCurrent(view);
      // Started 10 min ago on the server's clock, 30 min planned.
      expect(view.text).toContain("10:00");
      expect(view.text).toContain("20:00");
    });

    it("stops showing the last values as current 20 s after the last point received, not later", () => {
      viewerClock(off);
      session("active", { receivedAgoMs: 0 });
      expectCurrent(paintAfter(TELEMETRY_FRESH_MS - 1000, panel));
      elapse(1000);
      expectSilent(paint(panel));
    });

    it("stops showing the last values as current when the machine's clock is 10 min ahead too", () => {
      viewerClock(off);
      session("active", { receivedAgoMs: 0 }, { machineClockMs: 600_000 });
      expectCurrent(paintAfter(TELEMETRY_FRESH_MS - 1000, panel));
      elapse(1000);
      expectSilent(paint(panel));
    });
  });
});

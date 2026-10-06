import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { getFunctionName, type FunctionReference } from "convex/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Id } from "@/convex/_generated/dataModel";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";
import { LIVE_FRESH_MS, type LiveState } from "@/lib/training";
import { LaunchableMachineCard } from "./LaunchableMachineCard";
import { MachineLiveCard } from "./MachineLiveCard";
import { TrainingPanel } from "./TrainingPanel";

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

const NOW = 1_800_000_000_000;
const machineId = "machine-1" as Id<"machines">;
const sessionId = "session-1" as Id<"sessions">;

/** The texts this ticket relies on, as the French site shows them. */
const LIVE = "En direct";
const STALE = "Données périmées";
const NO_SIGNAL =
  "Aucun signal récent de la machine : les valeurs affichées peuvent être dépassées.";
/** The pulsing green dot of the "live" badge. */
const LIVE_DOT = "bg-green-400";
/** How a stale value is greyed. */
const GREYED = "opacity-60";

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
    updatedAt: NOW - ageMs,
  };
}

/** The page as the browser first paints it, at the clock of the test. */
function render(node: ReactNode, locale: "fr" | "en" = "fr") {
  const html = renderToStaticMarkup(
    <NextIntlClientProvider
      locale={locale}
      messages={locale === "fr" ? fr : en}
      timeZone="Europe/Paris"
    >
      {node}
    </NextIntlClientProvider>,
  );
  return { html, text: html.replace(/<[^>]+>/g, " ") };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  answers.clear();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("machine page: live state card", () => {
  /** getMachineLive as it last ran: it does not run again while the machine is silent. */
  function machineLive(ageMs: number, serverStale: boolean) {
    answers.set("training:getMachineLive", {
      status: "online",
      programsEnabled: true,
      live: liveState(ageMs),
      stale: serverStale,
    });
  }

  it("shows a machine heard 30 s ago as live", () => {
    machineLive(30_000, false);
    const { html, text } = render(<MachineLiveCard machineId={machineId} />);
    expect(text).toContain(LIVE);
    expect(html).toContain(LIVE_DOT);
    expect(text).not.toContain(STALE);
    expect(text).not.toContain(NO_SIGNAL);
    expect(html).not.toContain(GREYED);
  });

  it("shows stale data, greyed, with the no-signal notice, 90 s after the last state even though the query still says fresh", () => {
    machineLive(LIVE_FRESH_MS, false);
    const { html, text } = render(<MachineLiveCard machineId={machineId} />);
    expect(text).toContain(STALE);
    expect(text).toContain(NO_SIGNAL);
    expect(html).toContain(GREYED);
    expect(text).not.toContain(LIVE);
    expect(html).not.toContain(LIVE_DOT);
    // The heart is no longer drawn as beating.
    expect(html).not.toContain("text-red-500");
  });

  it("keeps the server's stale verdict when this browser's clock runs behind", () => {
    // The browser believes the state is 10 s old; the server knows better.
    machineLive(10_000, true);
    const { html, text } = render(<MachineLiveCard machineId={machineId} />);
    expect(text).toContain(STALE);
    expect(text).toContain(NO_SIGNAL);
    expect(html).not.toContain(LIVE_DOT);
  });

  it("says so in English too", () => {
    machineLive(LIVE_FRESH_MS, false);
    const { text } = render(<MachineLiveCard machineId={machineId} />, "en");
    expect(text).toContain("Stale");
    expect(text).toContain("No recent heartbeat from the machine");
  });
});

describe("my machines: one card per machine", () => {
  function card(ageMs: number) {
    return render(
      <LaunchableMachineCard
        machine={{
          _id: machineId,
          name: "Centri Paris",
          location: "Paris",
          status: "online",
          programsEnabled: true,
          live: liveState(ageMs),
          profiles: [],
          myHrMax: null,
        }}
        isUser={false}
        onLaunch={() => {}}
      />,
    );
  }

  it("shows a machine heard 30 s ago as live", () => {
    const { html, text } = card(30_000);
    expect(text).toContain(LIVE);
    expect(text).not.toContain(STALE);
    expect(html).not.toContain(GREYED);
  });

  it("no longer shows a machine as live 90 s after its last state", () => {
    const { html, text } = card(LIVE_FRESH_MS);
    expect(text).toContain(STALE);
    expect(text).toContain(NO_SIGNAL);
    expect(html).toContain(GREYED);
    expect(text).not.toContain(LIVE);
    expect(html).not.toContain(LIVE_DOT);
  });
});

describe("live view: training panel", () => {
  function session(status: string, lastPointAgeMs: number | null) {
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
      startedAt: NOW - 600_000,
      endedAt: status === "active" ? undefined : NOW - 300_000,
      canStop: status === "active",
    });
    answers.set(
      "training:getSessionTelemetry",
      lastPointAgeMs === null
        ? []
        : [
            {
              t: NOW - lastPointAgeMs,
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
    return render(<TrainingPanel sessionId={sessionId} />);
  }

  it("shows the values of a point received 5 s ago, without notice", () => {
    const { html, text } = session("active", 5000);
    expect(text).toContain("148");
    expect(text).not.toContain(NO_SIGNAL);
    expect(html).not.toContain(GREYED);
    expect(html).toContain("animate-spin");
  });

  it("greys the readouts and shows the no-signal notice 20 s after the last point of an active session", () => {
    const { html, text } = session("active", 20_000);
    expect(text).toContain(NO_SIGNAL);
    expect(html).toContain(GREYED);
    // No number stands for "now", and the arm is no longer drawn as turning.
    expect(text).not.toContain("148");
    expect(text).not.toContain("24.1");
    expect(html).not.toContain("animate-spin");
  });

  it("shows the notice for an active session that has sent no point for 20 s", () => {
    const { html, text } = session("active", null);
    expect(text).toContain(NO_SIGNAL);
    expect(html).toContain(GREYED);
  });

  it("leaves a finished session alone: its last values are its result", () => {
    const { html, text } = session("completed", 300_000);
    expect(text).toContain("148");
    expect(text).not.toContain(NO_SIGNAL);
    expect(html).not.toContain(GREYED);
  });
});

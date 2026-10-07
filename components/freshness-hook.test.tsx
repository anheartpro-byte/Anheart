import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { getFunctionName, type FunctionReference } from "convex/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Id } from "@/convex/_generated/dataModel";
import fr from "@/messages/fr.json";
import { TELEMETRY_FRESH_MS } from "@/lib/training";
import { textOf } from "./markup.test-helpers";
import {
  LastSignal,
  MachineStatusBadge,
  MachineStatusText,
  OnlineMachinesCount,
  VersionsSeen,
} from "./machines/MachineSignal";
import { LaunchableMachineCard } from "./training/LaunchableMachineCard";
import { MachineLiveCard } from "./training/MachineLiveCard";
import { TrainingPanel } from "./training/TrainingPanel";

/**
 * Every component that shows something as current takes its verdict from the
 * freshness hook, and from nothing else.
 *
 * The hook is replaced here by one that says what the test tells it to. A
 * component that worked the age out by itself, once, when it renders, would
 * pass every test made at a fixed instant and then never turn stale: here it
 * fails, because its data and the hook disagree and the hook must win. The
 * hook's own tests (hooks/use-freshness.test.ts) show that its verdict
 * changes as time passes.
 */

type Verdict = { fresh: boolean; now: number };
type Call = [
  updatedAt: number | null | undefined,
  serverNow: number | null | undefined,
  freshMs?: number,
];

const hook = vi.hoisted(() => ({
  verdict: { fresh: true, now: 0 } as Verdict,
  calls: [] as Call[],
}));

vi.mock("@/hooks/use-freshness", () => {
  const judge = (...call: Call): Verdict => {
    hook.calls.push(call);
    return hook.verdict;
  };
  return { useFreshness: judge, useFreshnessJudge: () => judge };
});

/** What each Convex query answers, by function name. */
const answers = vi.hoisted(() => new Map<string, unknown>());

vi.mock("convex/react", () => ({
  useQuery: (ref: FunctionReference<"query">) =>
    answers.get(getFunctionName(ref)),
  useMutation: () => async () => null,
}));
vi.mock("@/i18n/navigation", () => ({
  Link: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("./training/TelemetryCharts", () => ({ TelemetryCharts: () => null }));

const SERVER = 1_800_000_000_000;
const machineId = "machine-1" as Id<"machines">;
const sessionId = "session-1" as Id<"sessions">;

const LIVE = "En direct";
const STALE = "Données périmées";
const ONLINE = "En ligne";
const OFFLINE = "Hors ligne";
const NO_SIGNAL =
  "Aucun signal récent de la machine : les valeurs affichées peuvent être dépassées.";

/** A state received this instant: fresh for anyone who works it out alone. */
const justNow = {
  runMode: "seance",
  phase: "hold",
  bpm: 148,
  motorRpm: 1200,
  outputRpm: 24.1,
  setpointMotorRpm: 1200,
  gLoad: 1.42,
  safetyAction: "none",
  updatedAt: SERVER,
};
/** A state an hour old: stale for anyone who works it out alone. */
const anHourAgo = { ...justNow, updatedAt: SERVER - 3_600_000 };

function paint(node: ReactNode) {
  const html = renderToStaticMarkup(
    <NextIntlClientProvider locale="fr" messages={fr} timeZone="Europe/Paris">
      {node}
    </NextIntlClientProvider>,
  );
  return textOf(html);
}

/** The hook's answer for the rest of the test. */
function hookSays(fresh: boolean, now = SERVER) {
  hook.verdict = { fresh, now };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(SERVER);
  hook.calls.length = 0;
  hookSays(true);
  answers.clear();
  return () => {
    vi.useRealTimers();
  };
});

describe("machine page: live state card", () => {
  const card = <MachineLiveCard machineId={machineId} />;
  const answer = (live: typeof justNow) =>
    answers.set("training:getMachineLive", {
      status: "online",
      programsEnabled: true,
      live,
      stale: false,
      serverNow: SERVER + 7,
    });

  it("shows stale a state received this instant when the hook says stale", () => {
    answer(justNow);
    hookSays(false);
    const text = paint(card);
    expect(text).toContain(STALE);
    expect(text).toContain(NO_SIGNAL);
    expect(text).not.toContain(LIVE);
  });

  it("shows live an hour-old state when the hook says fresh", () => {
    answer(anHourAgo);
    expect(paint(card)).toContain(LIVE);
  });

  it("gives the hook the state's date and the server's clock of the same answer", () => {
    answer(justNow);
    paint(card);
    expect(hook.calls).toEqual([[SERVER, SERVER + 7]]);
  });

  it("counts 'updated ... ago' on the clock the hook hands out", () => {
    answer(justNow);
    hookSays(true, SERVER + 180_000);
    expect(paint(card)).toContain("Mis à jour il y a 3 minutes");
  });
});

describe("my machines: one card per machine", () => {
  const card = (live: typeof justNow) => (
    <LaunchableMachineCard
      machine={{
        _id: machineId,
        name: "Centri Paris",
        location: "Paris",
        status: "online",
        lastHeartbeat: live.updatedAt - 3,
        serverNow: SERVER + 7,
        programsEnabled: true,
        live,
        profiles: [],
        myHrMax: null,
      }}
      isUser={false}
      onLaunch={() => {}}
    />
  );

  it("shows offline and stale a machine heard this instant when the hook says stale", () => {
    hookSays(false);
    const text = paint(card(justNow));
    expect(text).toContain(OFFLINE);
    expect(text).toContain(STALE);
    expect(text).not.toContain(ONLINE);
    expect(text).not.toContain(LIVE);
  });

  it("shows online and live a machine heard an hour ago when the hook says fresh", () => {
    const text = paint(card(anHourAgo));
    expect(text).toContain(ONLINE);
    expect(text).toContain(LIVE);
    expect(text).not.toContain(OFFLINE);
  });

  it("gives the hook the last signal and the state's date, each with the server's clock", () => {
    paint(card(justNow));
    expect(hook.calls).toContainEqual([SERVER - 3, SERVER + 7]);
    expect(hook.calls).toContainEqual([SERVER, SERVER + 7]);
  });
});

describe("live view: training panel", () => {
  const panel = <TrainingPanel sessionId={sessionId} />;
  /** A session whose last point the server received `agoMs` ago. */
  function session(agoMs: number) {
    answers.set("training:getTrainingSession", {
      _id: sessionId,
      machineId,
      machineName: "Centri Paris",
      status: "active",
      kind: "auto",
      origin: "remote",
      startedAt: SERVER - 600_000,
      lastSignalAt: SERVER - agoMs,
      // The machine measured the point a second before the server received it.
      lastMeasuredAt: SERVER - agoMs - 1000,
      serverNow: SERVER + 7,
      canStop: true,
    });
    answers.set("training:getSessionTelemetry", [
      { ...justNow, t: SERVER - agoMs - 1000, elapsedS: 600 },
    ]);
  }

  it("stops showing as current a point received this instant when the hook says stale", () => {
    session(0);
    hookSays(false);
    const text = paint(panel);
    expect(text).toContain(NO_SIGNAL);
    expect(text).not.toContain("148");
    expect(text).not.toContain("24.1");
  });

  it("shows as current a point received an hour ago when the hook says fresh", () => {
    session(3_600_000);
    const text = paint(panel);
    expect(text).not.toContain(NO_SIGNAL);
    expect(text).toContain("148");
  });

  it("gives the hook the reception of the last point, its measurement date and the date of the point it prints, each with the server's clock and the 20 s threshold", () => {
    session(4000);
    paint(panel);
    expect(hook.calls).toEqual([
      // When the server received the last point.
      [SERVER - 4000, SERVER + 7, TELEMETRY_FRESH_MS],
      // When the machine says it measured it.
      [SERVER - 5000, SERVER + 7, TELEMETRY_FRESH_MS],
      // The date of the point whose values are printed.
      [SERVER - 5000, SERVER + 7, TELEMETRY_FRESH_MS],
    ]);
  });

  it("counts the elapsed time on the clock the hook hands out", () => {
    session(0);
    hookSays(true, SERVER + 125_000);
    // Started 10 min before SERVER.
    expect(paint(panel)).toContain("12:05");
  });
});

describe("machine status, count and last signal", () => {
  const heardNow = {
    status: "online",
    lastHeartbeat: SERVER,
    serverNow: SERVER + 7,
  };
  const heardAnHourAgo = { ...heardNow, lastHeartbeat: SERVER - 3_600_000 };

  it.each([
    ["badge", <MachineStatusBadge key="b" machine={heardNow} />],
    ["plain words", <MachineStatusText key="t" machine={heardNow} />],
  ])(
    "shows offline a machine heard this instant when the hook says stale (%s)",
    (_name, node) => {
      hookSays(false);
      expect(paint(node)).toContain(OFFLINE);
      expect(hook.calls).toEqual([[SERVER, SERVER + 7]]);
    },
  );

  it.each([
    ["badge", <MachineStatusBadge key="b" machine={heardAnHourAgo} />],
    ["plain words", <MachineStatusText key="t" machine={heardAnHourAgo} />],
  ])(
    "shows online a machine heard an hour ago when the hook says fresh (%s)",
    (_name, node) => {
      expect(paint(node)).toContain(ONLINE);
    },
  );

  it("counts online the machines the hook calls fresh, and none when it says stale", () => {
    const count = <OnlineMachinesCount machines={[heardNow, heardAnHourAgo]} />;
    expect(paint(count).trim()).toBe("2");
    expect(hook.calls).toEqual([
      [SERVER, SERVER + 7],
      [SERVER - 3_600_000, SERVER + 7],
    ]);
    hookSays(false);
    expect(paint(count).trim()).toBe("0");
  });

  it("counts 'last signal' and 'versions seen' on the clock the hook hands out", () => {
    hookSays(true, SERVER + 180_000);
    expect(paint(<LastSignal machine={heardNow} />)).toContain(
      "il y a 3 minutes",
    );
    expect(
      paint(<VersionsSeen at={SERVER} serverNow={SERVER + 7} />),
    ).toContain("il y a 3 minutes");
    expect(hook.calls).toEqual([
      [SERVER, SERVER + 7],
      [SERVER, SERVER + 7],
    ]);
  });
});

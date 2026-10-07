import { click, messages, render } from "@/test-support/render";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { answer, asks, resetConvex } from "@/test-support/convex";
import type { Id } from "@/convex/_generated/dataModel";
import { forgetServerAnswers } from "@/lib/server-clock";
import type { LiveState, TrainingProfile } from "@/lib/training";
import { LaunchableMachineCard } from "./LaunchableMachineCard";
import { LiveReadouts, MachineLiveCard } from "./MachineLiveCard";
import { MachineProgramsCard, ProfileList } from "./ProfileList";
import { SessionKindCell } from "./SessionKindCell";
import {
  LiveFreshBadge,
  SessionKindBadge,
  SessionOriginBadge,
} from "./TrainingBadges";
import { TrainingDetailsCard } from "./TrainingDetailsCard";

/**
 * The read-only cards of the training pages: the detail of a session, the
 * programmes of a machine, the kind and origin of a session in a list, the
 * live readouts, the launch button of "My machines".
 *
 * The freshness of the live state (clocks, silence) is proven in
 * live-freshness.test.tsx and freshness-hook.test.tsx. This file proves the
 * rest: what each card says in each state, what it never invents (a dash for
 * an absent value, the raw word for a value the catalog does not know), and
 * when the launch button can be pressed.
 */

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/convex")).convexReact,
);
vi.mock("@/i18n/navigation", () => ({
  Link: ({ href, children }: { href: string; children: ReactNode }) => (
    <a href={href}>{children}</a>
  ),
}));
const charts = vi.hoisted(() => ({ props: [] as unknown[] }));
vi.mock("./TelemetryCharts", () => ({
  TelemetryCharts: (props: unknown) => {
    charts.props.push(props);
    return <div data-charts="" />;
  },
}));

const fr = messages.fr;
const t = fr.training;
const NOW = 1_800_000_000_000;
const sessionId = "session-1" as Id<"sessions">;
const machineId = "machine-1" as Id<"machines">;

const ENDURANCE: TrainingProfile = {
  profileId: "endurance",
  name: "Endurance",
  totalDurationS: 1800,
  zoneLowBpm: 110,
  zoneHighBpm: 140,
  hardMaxBpm: 165,
  criticalBpm: 175,
  subjectHrMax: 180,
  minRunRpm: 300,
  maxRpm: 1200,
};

function live(over: Partial<LiveState> = {}): LiveState {
  return {
    runMode: "seance",
    phase: "hold",
    bpm: 128.4,
    motorRpm: 996.4,
    outputRpm: 20.04,
    setpointMotorRpm: 1000.2,
    gLoad: 1.523,
    safetyAction: "none",
    updatedAt: NOW - 2_000,
    ...over,
  };
}

const skeletons = (screen: ReturnType<typeof render>) =>
  screen.all((element) => element.getAttribute("data-slot") === "skeleton");

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  forgetServerAnswers();
  resetConvex();
  charts.props.length = 0;
});
afterEach(() => {
  vi.useRealTimers();
});

describe("ANH-203 session detail card", () => {
  function session(over: object = {}) {
    answer("training:getTrainingSession", {
      _id: sessionId,
      status: "completed",
      kind: "auto",
      origin: "remote",
      profileId: "endurance",
      profileName: "Endurance",
      zoneLowBpm: 110,
      zoneHighBpm: 140,
      totalDurationS: 1800,
      subjectHrMax: 180,
      operatorName: "Ada Lovelace",
      ...over,
    });
  }

  it("shows a placeholder while the session loads, and nothing for a session this person cannot see", () => {
    const loading = render(<TrainingDetailsCard sessionId={sessionId} />);
    expect(skeletons(loading)).toHaveLength(1);
    expect(loading.text()).toBe("");
    loading.unmount();

    answer("training:getTrainingSession", null);
    const hidden = render(<TrainingDetailsCard sessionId={sessionId} />);
    expect(hidden.container.childNodes).toEqual([]);
  });

  it("gives the programme, the zone, the planned duration, the rider's max heart rate, the operator and the origin", () => {
    session();
    answer("training:getSessionTelemetry", []);

    const screen = render(<TrainingDetailsCard sessionId={sessionId} />);

    for (const pair of [
      `${t.session.program} Endurance`,
      `${t.session.zone} 110-140 bpm`,
      `${t.session.plannedDuration} 30 min`,
      `${t.session.subjectHrMax} 180 bpm`,
      `${t.session.operator} Ada Lovelace`,
      `${t.session.origin} ${t.origin.remote}`,
    ]) {
      expect(screen.text()).toContain(pair);
    }
    expect(screen.text()).toContain(t.session.noTelemetry);
    expect(asks("training:getSessionTelemetry")).toEqual([
      { sessionId, limit: 7200 },
    ]);
  });

  it("writes a dash for each value the session does not have, never a zero or an empty label", () => {
    session({
      origin: undefined,
      profileId: undefined,
      profileName: undefined,
      zoneHighBpm: undefined,
      totalDurationS: undefined,
      subjectHrMax: undefined,
      operatorName: undefined,
    });
    answer("training:getSessionTelemetry", []);

    const screen = render(<TrainingDetailsCard sessionId={sessionId} />);

    for (const label of [
      t.session.program,
      t.session.zone,
      t.session.plannedDuration,
      t.session.subjectHrMax,
      t.session.operator,
      t.session.origin,
    ]) {
      expect(screen.text()).toContain(`${label} -`);
    }
    expect(screen.text()).not.toContain("bpm");
  });

  it("falls back on the programme's identifier when its name is unknown", () => {
    session({ profileName: undefined });
    answer("training:getSessionTelemetry", []);

    const screen = render(<TrainingDetailsCard sessionId={sessionId} />);

    expect(screen.text()).toContain(`${t.session.program} endurance`);
  });

  it("shows why the session ended, in red when it failed", () => {
    session({ status: "failed", endReason: "drive_fault" });
    answer("training:getSessionTelemetry", []);
    const failed = render(<TrainingDetailsCard sessionId={sessionId} />);
    expect(failed.text()).toContain(`${t.session.endReason} drive_fault`);
    expect(failed.reading("drive_fault")[0].parentElement?.className).toContain(
      "border-red-500/50",
    );
    failed.unmount();

    session({ status: "completed", endReason: "operator_stop" });
    const completed = render(<TrainingDetailsCard sessionId={sessionId} />);
    expect(
      completed.reading("operator_stop")[0].parentElement?.className,
    ).not.toContain("red");
  });

  it("shows no end reason while there is none, and says where a manual session is driven from", () => {
    session({ kind: "manual", origin: "local" });
    answer("training:getSessionTelemetry", []);

    const screen = render(<TrainingDetailsCard sessionId={sessionId} />);

    expect(screen.text()).not.toContain(t.session.endReason);
    expect(screen.text()).toContain(t.manualOnlyAtMachine);
    expect(screen.text()).toContain(`${t.session.origin} ${t.origin.local}`);
  });

  it("hands the charts the recorded points and the zone, after a placeholder while they load", () => {
    session();
    const screen = render(<TrainingDetailsCard sessionId={sessionId} />);
    expect(skeletons(screen)).toHaveLength(1);
    expect(charts.props).toEqual([]);

    const points = [{ t: NOW, elapsedS: 5, bpm: 120 }];
    answer("training:getSessionTelemetry", points);
    screen.rerender(<TrainingDetailsCard sessionId={sessionId} />);

    expect(charts.props.at(-1)).toEqual({
      points,
      zoneLowBpm: 110,
      zoneHighBpm: 140,
    });
    expect(skeletons(screen)).toHaveLength(0);
  });
});

describe("ANH-203 kind and origin of a session", () => {
  it.each([
    ["auto", t.kind.auto],
    ["manual", t.kind.manual],
    ["recording", t.kind.recording],
    // A session of the retired recording mode has no kind at all.
    [undefined, t.kind.recording],
    // A kind this version of the site does not know is not shown as auto or manual.
    ["future_kind", t.kind.recording],
  ])("kind %s reads « %s »", (kind, label) => {
    const screen = render(<SessionKindBadge kind={kind} />);

    expect(screen.text()).toBe(label);
  });

  it("a manual session is set apart from an auto one by its colour", () => {
    const manual = render(<SessionKindBadge kind="manual" />);
    expect(manual.markup()).toContain("text-amber-700");
    manual.unmount();
    const auto = render(<SessionKindBadge kind="auto" />);
    expect(auto.markup()).not.toContain("amber");
  });

  it.each([
    ["remote", t.origin.remote],
    ["local", t.origin.local],
  ])("origin %s reads « %s »", (origin, label) => {
    expect(render(<SessionOriginBadge origin={origin} />).text()).toBe(label);
  });

  it.each([undefined, "unknown"])(
    "origin %s shows no badge at all",
    (origin) => {
      expect(
        render(<SessionOriginBadge origin={origin} />).container.childNodes,
      ).toEqual([]);
    },
  );

  it("the live badge says stale or live, with a pulsing dot only when live", () => {
    const fresh = render(<LiveFreshBadge stale={false} />);
    expect(fresh.text()).toBe(t.live.fresh);
    expect(fresh.markup()).toContain("animate-pulse");
    fresh.unmount();

    const stale = render(<LiveFreshBadge stale />);
    expect(stale.text()).toBe(t.live.stale);
    expect(stale.markup()).not.toContain("animate-pulse");
  });

  it("a list row that carries its kind and origin is shown without asking the server", () => {
    const row = { _id: sessionId, kind: "manual", origin: "local" };

    const screen = render(<SessionKindCell row={row} />);

    expect(screen.text()).toBe(`${t.kind.manual} ${t.origin.local}`);
    expect(asks("training:getTrainingSession")).toEqual(["skip"]);
  });

  it("a list row without them asks for the session, and shows nothing until it answers", () => {
    const screen = render(<SessionKindCell row={{ _id: sessionId }} />);
    expect(screen.container.childNodes).toEqual([]);
    expect(asks("training:getTrainingSession")).toEqual([{ sessionId }]);

    answer("training:getTrainingSession", { kind: "auto", origin: "remote" });
    screen.rerender(<SessionKindCell row={{ _id: sessionId }} />);

    expect(screen.text()).toBe(`${t.kind.auto} ${t.origin.remote}`);
  });

  it("a row whose kind is not text is not trusted: the session is asked for", () => {
    render(
      <SessionKindCell
        row={{ _id: sessionId, kind: 3 } as { _id: Id<"sessions"> }}
      />,
    );

    expect(asks("training:getTrainingSession")).toEqual([{ sessionId }]);
  });
});

describe("ANH-203 programmes of a machine", () => {
  it("lists each programme with its zone, its limit, its duration and its top speed at the arm and at the motor", () => {
    const screen = render(
      <ProfileList profiles={[ENDURANCE]} programsEnabled />,
    );

    expect(screen.text()).toContain(
      "Endurance 110-140 bpm (FC limite 165) 30 min 24.1 tr/min bras 1200 tr/min moteur",
    );
    expect(screen.text()).not.toContain(t.programs.disabled);
    expect(screen.text()).toContain(t.manualOnlyAtMachine);
  });

  it("says that auto programmes are disabled, and greys the programmes, when the machine says so", () => {
    const screen = render(
      <ProfileList profiles={[ENDURANCE]} programsEnabled={false} />,
    );

    expect(screen.text()).toContain(t.programs.disabled);
    expect(screen.reading("Endurance")[0].parentElement?.className).toContain(
      "opacity-60",
    );
  });

  it("does not grey the programmes of a machine that accepts them", () => {
    const screen = render(
      <ProfileList profiles={[ENDURANCE]} programsEnabled />,
    );

    expect(
      screen.reading("Endurance")[0].parentElement?.className,
    ).not.toContain("opacity-60");
  });

  it("says that no programme was synchronised when the list is empty", () => {
    const screen = render(<ProfileList profiles={[]} programsEnabled />);

    expect(screen.text()).toContain(t.programs.none);
  });

  it("the card waits for both the programmes and the machine's state before it shows either", () => {
    answer("training:listMachineProfiles", [ENDURANCE]);
    const screen = render(
      <MachineProgramsCard
        machineId={machineId}
        action={<button>Lancer</button>}
      />,
    );

    expect(skeletons(screen)).toHaveLength(1);
    expect(screen.text()).not.toContain("Endurance");
    // What the page puts next to the title is drawn at once.
    expect(screen.hasButton("Lancer")).toBe(true);
    expect(asks("training:listMachineProfiles")).toEqual([{ machineId }]);
    expect(asks("training:getMachineLive")).toEqual([{ machineId }]);

    answer("training:getMachineLive", { programsEnabled: true, live: null });
    screen.rerender(<MachineProgramsCard machineId={machineId} />);
    expect(screen.text()).toContain("Endurance");
    expect(screen.text()).not.toContain(t.programs.disabled);
  });

  it("the card treats a machine this person cannot read as one with programmes disabled", () => {
    answer("training:listMachineProfiles", [ENDURANCE]);
    answer("training:getMachineLive", null);

    const screen = render(<MachineProgramsCard machineId={machineId} />);

    expect(screen.text()).toContain(t.programs.disabled);
  });
});

describe("ANH-203 live readouts", () => {
  it("shows each value with its unit, rounded as the machine's display does", () => {
    const screen = render(<LiveReadouts live={live()} stale={false} />);

    expect(screen.text()).toContain(`${t.live.runMode} ${t.runMode.seance}`);
    expect(screen.text()).toContain(`${t.live.phase} ${t.phase.hold}`);
    expect(screen.text()).toContain(`${t.live.heartRate} 128 bpm`);
    expect(screen.text()).toContain(
      `${t.live.outputRpm} 20.0 tr/min Moteur 996 tr/min · Consigne 1000`,
    );
    expect(screen.text()).toContain(`${t.live.gLoad} 1.52 g`);
    expect(screen.text()).toContain(`${t.live.safetyAction} ${t.safety.none}`);
    expect(screen.text()).not.toContain(t.live.staleDesc);
  });

  it("writes a dash for an absent heart rate, never a number", () => {
    const screen = render(
      <LiveReadouts live={live({ bpm: undefined })} stale={false} />,
    );

    expect(screen.text()).toContain(`${t.live.heartRate} - bpm`);
  });

  it("names the safety action in amber, and the state of the drive when the machine reports it", () => {
    const screen = render(
      <LiveReadouts
        live={live({ safetyAction: "quick_stop", driveState: "fault" })}
        stale={false}
      />,
    );

    expect(screen.text()).toContain(t.safety.quick_stop);
    expect(screen.reading(t.safety.quick_stop)[0].className).toContain(
      "text-amber-600",
    );
    expect(screen.text()).toContain(
      `${t.live.driveState} : ${t.driveState.fault}`,
    );
  });

  it("no action and no drive state: no amber, no drive line", () => {
    const screen = render(<LiveReadouts live={live()} stale={false} />);

    expect(screen.reading(t.safety.none)[0].className).not.toContain("amber");
    expect(screen.text()).not.toContain(t.live.driveState);
  });

  it("shows a value the catalog does not know as the machine sent it, never as a key or as another word", () => {
    const screen = render(
      <LiveReadouts
        live={live({
          runMode: "calibration",
          phase: "constructor",
          safetyAction: "toString",
          driveState: "new_state",
        })}
        stale={false}
      />,
    );

    expect(screen.text()).toContain(`${t.live.runMode} calibration`);
    expect(screen.text()).toContain(`${t.live.phase} constructor`);
    expect(screen.text()).toContain(`${t.live.safetyAction} toString`);
    expect(screen.text()).toContain(`${t.live.driveState} : new_state`);
  });

  it("the live card says when the state was last updated, in the language of the page", () => {
    answer("training:getMachineLive", {
      live: live(),
      stale: false,
      serverNow: NOW,
    });

    const french = render(<MachineLiveCard machineId={machineId} />);
    expect(french.text()).toContain("Mis à jour il y a moins d’une minute");
    french.unmount();

    const english = render(<MachineLiveCard machineId={machineId} />, {
      locale: "en",
    });
    expect(english.text()).toContain("Updated less than a minute ago");
  });

  it("the live card shows a placeholder while the machine's state loads: no value, no badge, no « nothing reported »", () => {
    const screen = render(<MachineLiveCard machineId={machineId} />);

    expect(skeletons(screen)).toHaveLength(1);
    expect(screen.text()).toBe(`${t.live.title} ${t.live.description}`);
  });

  it("the live card says that the machine reported nothing, for a machine without a state and for one this person cannot read", () => {
    answer("training:getMachineLive", {
      live: null,
      stale: true,
      serverNow: NOW,
    });
    const silent = render(<MachineLiveCard machineId={machineId} />);
    expect(silent.text()).toContain(t.live.noData);
    expect(silent.text()).not.toContain(t.live.stale);
    silent.unmount();

    answer("training:getMachineLive", null);
    const hidden = render(<MachineLiveCard machineId={machineId} />);
    expect(hidden.text()).toContain(t.live.noData);
  });
});

describe("ANH-203 a machine of « My machines »: the launch button", () => {
  type Machine = Parameters<typeof LaunchableMachineCard>[0]["machine"];

  function machine(over: Partial<Machine> = {}): Machine {
    return {
      _id: machineId,
      name: "Centri Paris",
      location: "Salle 2",
      status: "online",
      lastHeartbeat: NOW - 3_000,
      serverNow: NOW,
      programsEnabled: true,
      live: live(),
      profiles: [ENDURANCE],
      myHrMax: 185,
      ...over,
    };
  }

  const launch = (screen: ReturnType<typeof render>) =>
    screen.button(t.launch.button);

  it("an online machine with programmes can be launched: the click reaches the page", async () => {
    const onLaunch = vi.fn();
    const screen = render(
      <LaunchableMachineCard machine={machine()} isUser onLaunch={onLaunch} />,
    );

    expect(launch(screen).hasAttribute("disabled")).toBe(false);
    await click(launch(screen));

    expect(onLaunch).toHaveBeenCalledTimes(1);
    expect(screen.text()).toContain("Centri Paris Salle 2");
    expect(screen.text()).toContain("· 1 programme");
  });

  it.each([
    ["is in a session", { status: "in_session" }, t.launch.inSession],
    ["is offline", { status: "offline" }, t.launch.offline],
    [
      "has auto programmes disabled",
      { programsEnabled: false },
      t.programs.disabled,
    ],
    ["has no programme", { profiles: [] }, t.programs.none],
  ])(
    "a machine that %s cannot be launched, and the card says why",
    async (_case, over, reason) => {
      const onLaunch = vi.fn();
      const screen = render(
        <LaunchableMachineCard
          machine={machine(over as Partial<Machine>)}
          isUser
          onLaunch={onLaunch}
        />,
      );

      expect(launch(screen).hasAttribute("disabled")).toBe(true);
      await click(launch(screen));
      expect(onLaunch).not.toHaveBeenCalled();
      expect(screen.text()).toContain(reason);
    },
  );

  it("a patient gets no link to the machine's page, a manager does", () => {
    const patient = render(
      <LaunchableMachineCard machine={machine()} isUser onLaunch={() => {}} />,
    );
    expect(patient.tag("a")).toEqual([]);
    expect(patient.hasButton(t.myMachines.details)).toBe(false);
    patient.unmount();

    const manager = render(
      <LaunchableMachineCard
        machine={machine()}
        isUser={false}
        onLaunch={() => {}}
      />,
    );
    expect(manager.tag("a").map((link) => link.getAttribute("href"))).toEqual([
      `/dashboard/machines/${machineId}`,
    ]);
  });

  it("a machine without a place shows its name alone, and counts its programmes in words", () => {
    const screen = render(
      <LaunchableMachineCard
        machine={machine({ location: undefined, profiles: [] })}
        isUser
        onLaunch={() => {}}
      />,
    );

    expect(screen.text()).toContain("· Aucun programme");
    expect(screen.text()).not.toContain("Salle 2");
  });

  it("a machine that never reported is not said to have stopped sending", () => {
    const screen = render(
      <LaunchableMachineCard
        machine={machine({ status: "offline", lastHeartbeat: 0, live: null })}
        isUser
        onLaunch={() => {}}
      />,
    );

    expect(screen.text()).toContain(t.live.noData);
    expect(screen.text()).not.toContain(t.live.offlineNoData);
  });
});

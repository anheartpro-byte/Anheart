// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import { format } from "date-fns";
import { enUS, fr as frLocale } from "date-fns/locale";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import fr from "@/messages/fr.json";
import {
  answer,
  argsAsked,
  lastArgsAsked,
  NOW,
  propsOf,
  renderPage,
  routeParams,
  wasMounted,
} from "@/test-support/pages";
import SessionDetailPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);
// The card of the training fields asks its own queries and draws the curves:
// the page only mounts it for the right session.
vi.mock("@/components/training/TrainingDetailsCard", async () => ({
  TrainingDetailsCard: (await import("@/test-support/pages")).standIn(
    "TrainingDetailsCard",
  ),
}));

/** The detail of one session, running or over, of training or of the former recording mode. */

type Session = NonNullable<FunctionReturnType<typeof api.sessions.getSession>>;
type Training = NonNullable<
  FunctionReturnType<typeof api.training.getTrainingSession>
>;
type LegacyStats = NonNullable<
  FunctionReturnType<typeof api.ecgData.getSessionDataStats>
>;

const sessionId = "session-1" as Id<"sessions">;
const machineId = "machine-1" as Id<"machines">;
const STARTED = NOW - 7_200_000;

function session(overrides: Partial<Session> = {}): Session {
  return {
    _id: sessionId,
    _creationTime: STARTED,
    machineId,
    userId: "user-user" as Id<"users">,
    startedById: "user-gestionnaire" as Id<"users">,
    status: "completed",
    startedAt: STARTED,
    endedAt: STARTED + 1_800_000,
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
    status: "completed",
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
    lastSignalAt: null,
    lastMeasuredAt: null,
    serverNow: NOW,
    endedAt: STARTED + 1_800_000,
    stopRequestedAt: undefined,
    endReason: undefined,
    canStop: false,
    ...overrides,
  };
}

const recording = (overrides: Partial<Session> = {}) =>
  session({ kind: "recording", channels: ["ECG", "SpO2"], ...overrides });

function stats(overrides: Partial<LegacyStats> = {}): LegacyStats {
  return {
    totalBatches: 12,
    firstTimestamp: STARTED + 1_000,
    lastTimestamp: STARTED + 126_000,
    channels: ["ECG", "SpO2"],
    durationSeconds: 125,
    ...overrides,
  };
}

function open(locale: "fr" | "en" = "fr") {
  return renderPage(
    <SessionDetailPage params={routeParams({ id: sessionId })} />,
    { locale },
  );
}

/** The value shown under a label of a card. */
function valueUnder(label: string): string | null {
  const value = screen.getByText(label).nextElementSibling;
  return value === null ? null : value.textContent;
}

/** The address of the link that holds `label`. */
function linkTo(label: string): string | null {
  const link = screen.getByText(label).closest("a");
  return link === null ? null : link.getAttribute("href");
}

describe("session detail: which session it shows", () => {
  it("asks for the session of the address and for its training fields", async () => {
    answer(api.sessions.getSession, session());
    answer(api.training.getTrainingSession, training());
    await open();

    expect(lastArgsAsked(api.sessions.getSession)).toEqual({ sessionId });
    expect(lastArgsAsked(api.training.getTrainingSession)).toEqual({
      sessionId,
    });
  });

  it("shows nothing of a session while it is loading", async () => {
    answer(api.sessions.getSession, undefined);
    await open();

    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.queryByText(fr.sessions.notFound)).toBeNull();
    expect(wasMounted("TrainingDetailsCard")).toBe(false);
  });

  it("says the session is not found, with the way back, when the server answers none", async () => {
    answer(api.sessions.getSession, null);
    answer(api.training.getTrainingSession, null);
    await open();

    expect(screen.getByText(fr.sessions.notFound)).toBeTruthy();
    expect(linkTo(fr.common.back)).toBe("/dashboard/sessions");
    expect(wasMounted("TrainingDetailsCard")).toBe(false);
    expect(screen.queryByText(fr.reports.patientInfo)).toBeNull();
  });
});

describe("session detail: a training session", () => {
  it("shows who rode, on which machine, when, and the session's badges", async () => {
    answer(api.sessions.getSession, session());
    answer(api.training.getTrainingSession, training());
    await open();

    const header = within(
      screen.getByRole("heading", { level: 1, name: "Paul Patient" })
        .parentElement?.parentElement as HTMLElement,
    );
    expect(header.getByText(fr.sessions.status.completed)).toBeTruthy();
    expect(header.getByText(fr.training.kind.auto)).toBeTruthy();
    expect(header.getByText(fr.training.origin.remote)).toBeTruthy();
    expect(
      header.getByText(
        `Centri Paris • ${format(STARTED, "PPP", { locale: frLocale })}`,
      ),
    ).toBeTruthy();
    expect(valueUnder(fr.common.name)).toBe("Paul Patient");
    expect(valueUnder(fr.users.email)).toBe("paul@example.test");
    expect(valueUnder(fr.sessions.startedBy)).toBe("Gaston Gestion");
  });

  it("mounts the training card for this session, and no card of the former recording mode", async () => {
    answer(api.sessions.getSession, session());
    answer(api.training.getTrainingSession, training());
    await open();

    expect(propsOf("TrainingDetailsCard")).toEqual({ sessionId });
    expect(screen.queryByText(fr.sessionDetail.legacyTitle)).toBeNull();
    // Only the former recording mode has ECG batches: nothing is asked.
    expect(argsAsked(api.ecgData.getSessionDataStats)).toEqual(["skip"]);
  });

  it("dates the start and the end, and gives the length of a finished session", async () => {
    answer(api.sessions.getSession, session({ endedAt: STARTED + 3_725_000 }));
    answer(api.training.getTrainingSession, training());
    await open();

    expect(valueUnder(fr.sessionDetail.started)).toBe(
      format(STARTED, "PPp", { locale: frLocale }),
    );
    expect(valueUnder(fr.sessionDetail.ended)).toBe(
      format(STARTED + 3_725_000, "PPp", { locale: frLocale }),
    );
    expect(valueUnder(fr.sessions.duration)).toBe("1h 2m 5s");
  });

  it.each([
    ["under a minute", 45_000, "45s"],
    ["under an hour", 1_800_000, "30m 0s"],
  ])("gives a length %s in its own unit", async (_name, ms, shown) => {
    answer(api.sessions.getSession, session({ endedAt: STARTED + ms }));
    answer(api.training.getTrainingSession, training());
    await open();

    expect(valueUnder(fr.sessions.duration)).toBe(shown);
  });

  it("dates in English for an English visitor", async () => {
    answer(api.sessions.getSession, session());
    answer(api.training.getTrainingSession, training());
    await open("en");

    expect(valueUnder("Started")).toBe(
      format(STARTED, "PPp", { locale: enUS }),
    );
  });

  it.each(["active", "pending"] as const)(
    "offers the live view of a session that is %s, and shows it in progress",
    async (status) => {
      answer(api.sessions.getSession, session({ status, endedAt: undefined }));
      answer(
        api.training.getTrainingSession,
        training({ status, endedAt: undefined, canStop: true }),
      );
      await open();

      expect(linkTo(fr.sessions.viewLive)).toBe(
        `/dashboard/sessions/${sessionId}/live`,
      );
      expect(valueUnder(fr.sessions.duration)).toBe(fr.sessions.inProgress);
      expect(screen.queryByText(fr.sessionDetail.ended)).toBeNull();
    },
  );

  it.each(["completed", "failed"] as const)(
    "offers no live view of a %s session",
    async (status) => {
      answer(api.sessions.getSession, session({ status }));
      answer(api.training.getTrainingSession, training({ status }));
      await open();

      expect(screen.queryByText(fr.sessions.viewLive)).toBeNull();
    },
  );

  it("says a pending session waits for the machine", async () => {
    answer(
      api.sessions.getSession,
      session({ status: "pending", endedAt: undefined }),
    );
    answer(
      api.training.getTrainingSession,
      training({ status: "pending", endedAt: undefined }),
    );
    await open();

    expect(screen.getByText(fr.sessionDetail.pendingTitle)).toBeTruthy();
    expect(screen.getByText(fr.training.session.pending)).toBeTruthy();
  });

  it("does not say an active session waits for the machine", async () => {
    answer(
      api.sessions.getSession,
      session({ status: "active", endedAt: undefined }),
    );
    answer(api.training.getTrainingSession, training({ status: "active" }));
    await open();

    expect(screen.queryByText(fr.sessionDetail.pendingTitle)).toBeNull();
    expect(screen.queryByText(fr.sessionDetail.failedTitle)).toBeNull();
  });

  it("shows a status the catalog does not know as the server sent it", async () => {
    // A status a later server could add: the session declares it a plain string.
    const unknown = "aborted" as Session["status"];
    answer(api.sessions.getSession, session({ status: unknown }));
    answer(api.training.getTrainingSession, training());
    await open();

    expect(screen.getByText("aborted")).toBeTruthy();
  });

  it.each([
    ["the label typed at the console", "Visiteur 12", "Visiteur 12"],
    ["'rider not specified'", undefined, fr.training.session.riderNotSpecified],
  ])(
    "names a session without an account by %s, and shows no e-mail",
    async (_name, subjectLabel, shown) => {
      answer(
        api.sessions.getSession,
        session({
          patient: null,
          userId: undefined,
          subjectLabel,
          startedBy: undefined,
          startedById: undefined,
        }),
      );
      answer(api.training.getTrainingSession, training({ origin: "local" }));
      await open();

      expect(
        screen.getByRole("heading", { level: 1, name: shown }),
      ).toBeTruthy();
      expect(valueUnder(fr.common.name)).toBe(shown);
      expect(screen.queryByText(fr.users.email)).toBeNull();
      expect(screen.queryByText(fr.sessions.startedBy)).toBeNull();
    },
  );
});

describe("session detail: a failed session", () => {
  const failed = { status: "failed" } as const;

  it("says the session failed and gives the reason the machine reported", async () => {
    answer(api.sessions.getSession, session(failed));
    answer(
      api.training.getTrainingSession,
      training({ ...failed, endReason: "drive_fault" }),
    );
    await open();

    expect(screen.getByText(fr.sessionDetail.failedTitle)).toBeTruthy();
    expect(screen.getByText(fr.sessionDetail.failedDescription)).toBeTruthy();
    expect(screen.getByText("drive_fault")).toBeTruthy();
  });

  it("gives the reason written in the notes of a session of the former mode", async () => {
    answer(
      api.sessions.getSession,
      recording({
        ...failed,
        notes: "Arrêt machine. Failure reason:  no heartbeat for 120 s ",
      }),
    );
    answer(api.training.getTrainingSession, null);
    answer(api.ecgData.getSessionDataStats, stats());
    await open();

    expect(screen.getByText("no heartbeat for 120 s")).toBeTruthy();
  });

  it("gives no reason when none was recorded", async () => {
    answer(api.sessions.getSession, session({ ...failed, notes: "RAS" }));
    answer(api.training.getTrainingSession, training(failed));
    await open();

    const warning = screen.getByText(fr.sessionDetail.failedTitle)
      .parentElement as HTMLElement;
    expect(
      Array.from(warning.children).map((child) => child.textContent),
    ).toEqual([
      fr.sessionDetail.failedTitle,
      fr.sessionDetail.failedDescription,
    ]);
  });

  it("shows no failure for a session that completed", async () => {
    answer(api.sessions.getSession, session());
    answer(
      api.training.getTrainingSession,
      training({ endReason: "duration_reached" }),
    );
    await open();

    expect(screen.queryByText(fr.sessionDetail.failedTitle)).toBeNull();
    expect(screen.queryByText("duration_reached")).toBeNull();
  });
});

describe("session detail: the notes", () => {
  it("shows the notes of the session when there are some", async () => {
    answer(
      api.sessions.getSession,
      session({ notes: "Première séance.\nBonne tolérance." }),
    );
    answer(api.training.getTrainingSession, training());
    await open();

    const card = screen
      .getByText(fr.sessionDetail.notes)
      .closest("[data-slot=card]") as HTMLElement;
    expect(within(card).getByText(/Première séance\./).textContent).toBe(
      "Première séance.\nBonne tolérance.",
    );
  });

  it("shows no notes card when there are none", async () => {
    answer(api.sessions.getSession, session());
    answer(api.training.getTrainingSession, training());
    await open();

    expect(screen.queryByText(fr.sessionDetail.notes)).toBeNull();
  });
});

describe("session detail: a session of the former recording mode", () => {
  it("asks the server for its ECG counts, and shows the read-only card instead of the training card", async () => {
    answer(api.sessions.getSession, recording());
    answer(api.training.getTrainingSession, null);
    answer(api.ecgData.getSessionDataStats, stats());
    await open();

    expect(lastArgsAsked(api.ecgData.getSessionDataStats)).toEqual({
      sessionId,
    });
    expect(screen.getByText(fr.sessionDetail.legacyTitle)).toBeTruthy();
    expect(screen.getByText(fr.sessionDetail.legacyDescription)).toBeTruthy();
    expect(screen.getByText(fr.training.kind.recording)).toBeTruthy();
    expect(wasMounted("TrainingDetailsCard")).toBe(false);
  });

  it("lists the recorded channels, the batches, the length and the hours of the data", async () => {
    answer(api.sessions.getSession, recording());
    answer(api.training.getTrainingSession, null);
    answer(api.ecgData.getSessionDataStats, stats());
    await open();

    const channels = screen.getByText(fr.sessionDetail.recordedChannels)
      .nextElementSibling as HTMLElement;
    expect(
      Array.from(channels.children).map((channel) => channel.textContent),
    ).toEqual(["ECG", "SpO2"]);
    expect(valueUnder(fr.sessionDetail.dataBatches)).toBe("12");
    expect(valueUnder(fr.sessionDetail.dataDuration)).toBe("125 s");
    expect(
      screen.getByText(
        `Données enregistrées de ${format(STARTED + 1_000, "HH:mm:ss")} à ${format(STARTED + 126_000, "HH:mm:ss")}`,
      ),
    ).toBeTruthy();
    expect(screen.queryByText(fr.sessionDetail.noEcgData)).toBeNull();
  });

  it("gives no hours when the server has none for the data", async () => {
    answer(api.sessions.getSession, recording());
    answer(api.training.getTrainingSession, null);
    answer(
      api.ecgData.getSessionDataStats,
      stats({ firstTimestamp: undefined, lastTimestamp: undefined }),
    );
    await open();

    expect(valueUnder(fr.sessionDetail.dataBatches)).toBe("12");
    expect(screen.queryByText(/Données enregistrées de/)).toBeNull();
  });

  it("says no ECG data was recorded when the server counts none", async () => {
    answer(api.sessions.getSession, recording());
    answer(api.training.getTrainingSession, null);
    answer(
      api.ecgData.getSessionDataStats,
      stats({
        totalBatches: 0,
        durationSeconds: 0,
        firstTimestamp: undefined,
        lastTimestamp: undefined,
      }),
    );
    await open();

    expect(screen.getByText(fr.sessionDetail.noEcgData)).toBeTruthy();
    expect(screen.queryByText(fr.sessionDetail.dataBatches)).toBeNull();
  });

  it.each([
    ["while the counts are loading", undefined],
    ["when the server gives no counts", null],
  ])("claims neither data nor their absence %s", async (_name, counts) => {
    answer(api.sessions.getSession, recording());
    answer(api.training.getTrainingSession, null);
    answer(api.ecgData.getSessionDataStats, counts);
    await open();

    expect(screen.getByText(fr.sessionDetail.legacyTitle)).toBeTruthy();
    expect(screen.queryByText(fr.sessionDetail.dataBatches)).toBeNull();
    expect(screen.queryByText(fr.sessionDetail.noEcgData)).toBeNull();
  });

  it.each(["active", "pending"] as const)(
    "shows a recording left %s as neither running nor waiting: no live view, no length",
    async (status) => {
      answer(
        api.sessions.getSession,
        recording({ status, endedAt: undefined }),
      );
      answer(api.training.getTrainingSession, null);
      answer(api.ecgData.getSessionDataStats, stats());
      await open();

      expect(screen.queryByText(fr.sessions.viewLive)).toBeNull();
      expect(screen.queryByText(fr.sessionDetail.pendingTitle)).toBeNull();
      expect(valueUnder(fr.sessions.duration)).toBe("-");
    },
  );
});

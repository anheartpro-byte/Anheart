// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { format } from "date-fns";
import { enUS, fr as frLocale } from "date-fns/locale";
import type { FunctionReturnType } from "convex/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";
import {
  answer,
  lastArgsAsked,
  NOW,
  propsOf,
  renderPage,
  routeParams,
  router,
  wasMounted,
} from "@/test-support/pages";
import SessionReportPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);
// The figures and the curves have their own tests: the page gives them what
// it read.
vi.mock("@/components/training/SessionSummaryFigures", async () => ({
  SessionSummaryFigures: (await import("@/test-support/pages")).standIn(
    "SessionSummaryFigures",
  ),
}));
vi.mock("@/components/training/TelemetryCharts", async () => ({
  TelemetryCharts: (await import("@/test-support/pages")).standIn(
    "TelemetryCharts",
  ),
}));

/**
 * ANH-89: the report of a session, to print or save as PDF. It reads what the
 * session page reads, under the same rights, and nothing else; it says what
 * it does not have yet; it is written in French or in English.
 */

type Session = NonNullable<FunctionReturnType<typeof api.sessions.getSession>>;
type Training = NonNullable<
  FunctionReturnType<typeof api.training.getTrainingSession>
>;
type Summary = Exclude<
  NonNullable<FunctionReturnType<typeof api.sessionReports.getSummary>>,
  { absent: string }
>;

const sessionId = "session-1" as Id<"sessions">;
const machineId = "machine-1" as Id<"machines">;
const STARTED = NOW - 7_200_000;
const r = fr.sessionReport;

function session(overrides: Partial<Session> = {}): Session {
  return {
    _id: sessionId,
    _creationTime: STARTED,
    machineId,
    userId: "user-user" as Id<"users">,
    startedById: undefined,
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
    startedBy: undefined,
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
    zoneLowBpm: 130,
    zoneHighBpm: 150,
    totalDurationS: 1800,
    subjectHrMax: 180,
    subjectLabel: undefined,
    operatorName: "Gaston Gestion",
    startedAt: STARTED,
    lastSignalAt: null,
    lastMeasuredAt: null,
    serverNow: NOW,
    endedAt: STARTED + 1_800_000,
    stopRequestedAt: undefined,
    endReason: "programme_complete",
    endCode: undefined,
    interruptedAt: undefined,
    canStop: false,
    ...overrides,
  };
}

function summary(overrides: Partial<Summary> = {}): Summary {
  return {
    provisional: false,
    computedAt: NOW,
    recordProblem: null,
    figures: {
      durationS: 1800,
      samples: 9000,
      heartRate: { minBpm: 92, meanBpm: 138.5, maxBpm: 152, samples: 8800 },
      zone: {
        lowBpm: 130,
        highBpm: 150,
        inZoneS: 1260,
        aboveZoneS: 30,
        belowZoneS: 480,
      },
      maxOutputRpm: 26.99,
      maxMotorRpm: 1344,
      reference: { maxG: 1.22, maxGRate: 0.02, radiusM: null },
      legTip: { maxG: 1.98, maxGRate: 0.03, radiusM: null, measured: null },
      verdicts: [],
      verdictsOmitted: 0,
      endReason: "programme_complete",
      endRule: null,
      warnings: [],
    },
    ...overrides,
  };
}

function serve(
  over: {
    session?: Session | null;
    training?: Training | null;
    summary?: FunctionReturnType<typeof api.sessionReports.getSummary>;
  } = {},
) {
  answer(
    api.sessions.getSession,
    over.session === undefined ? session() : over.session,
  );
  answer(
    api.training.getTrainingSession,
    over.training === undefined ? training() : over.training,
  );
  answer(
    api.sessionReports.getSummary,
    over.summary === undefined ? summary() : over.summary,
  );
}

function open(locale: "fr" | "en" = "fr") {
  return renderPage(
    <SessionReportPage params={routeParams({ id: sessionId })} />,
    { locale },
  );
}

/** The value written under a label of the header. */
function valueUnder(label: string): string | null {
  return screen.getByText(label).nextElementSibling?.textContent ?? null;
}

afterEach(() => vi.unstubAllGlobals());

describe("the report of a session", () => {
  it.each(["fr", "en"] as const)(
    "distinguishes a session that did not start from an uncomputed summary in %s",
    async (locale) => {
      const catalog = locale === "fr" ? fr.sessionReport : en.sessionReport;
      serve({
        session: session({ status: "failed" }),
        summary: { absent: "not_started" },
      });
      const view = await open(locale);
      expect(screen.getByText(catalog.notStartedNote)).toBeTruthy();
      expect(screen.queryByText(catalog.none)).toBeNull();
      expect(screen.queryByText(catalog.notComputed)).toBeNull();
      expect(wasMounted("SessionSummaryFigures")).toBe(false);
      answer(api.sessionReports.getSummary, { absent: "not_computed" });
      await view.refresh();
      expect(screen.getByText(catalog.none)).toBeTruthy();
      expect(screen.queryByText(catalog.notStartedNote)).toBeNull();
    },
  );
  it("reads the session, its training fields and its summary, for the session of the address", async () => {
    serve();
    await open();

    for (const query of [
      api.sessions.getSession,
      api.training.getTrainingSession,
      api.sessionReports.getSummary,
    ])
      expect(lastArgsAsked(query)).toEqual({ sessionId });
    expect(lastArgsAsked(api.training.getSessionTelemetry)).toEqual({
      sessionId,
      limit: 7200,
    });
  });

  it("draws the curves of the session against its zone, once its telemetry is there", async () => {
    serve();
    const view = await open();
    expect(wasMounted("TelemetryCharts")).toBe(false);

    const points = [
      {
        t: STARTED + 1000,
        elapsedS: 1,
        phase: "hold",
        bpm: 140,
        motorRpm: 900,
        outputRpm: 18.1,
        setpointMotorRpm: 900,
        gLoad: 0.55,
        safetyAction: "none",
      },
    ];
    answer(api.training.getSessionTelemetry, points);
    await view.refresh();
    expect(screen.getByText(r.telemetry)).toBeTruthy();
    expect(propsOf("TelemetryCharts")).toEqual({
      points,
      zoneLowBpm: 130,
      zoneHighBpm: 150,
      height: 180,
    });
  });

  it("draws no curve for a session without telemetry", async () => {
    serve();
    answer(api.training.getSessionTelemetry, []);
    await open();

    expect(screen.queryByText(r.telemetry)).toBeNull();
    expect(wasMounted("TelemetryCharts")).toBe(false);
  });

  it("shows nothing while one of them is loading", async () => {
    serve();
    answer(api.sessionReports.getSummary, undefined);
    await open();

    expect(screen.queryByRole("heading")).toBeNull();
    expect(wasMounted("SessionSummaryFigures")).toBe(false);
  });

  it("says the session is not found, with the way back, when the server answers none", async () => {
    serve({ session: null, training: null, summary: null });
    await open();

    expect(screen.getByText(r.notFound)).toBeTruthy();
    expect(screen.getByText(r.back).closest("a")?.getAttribute("href")).toBe(
      "/dashboard/sessions",
    );
    expect(screen.queryByText(r.download)).toBeNull();
  });

  it("names the rider, the machine, the start, the kind, the programme, the zone and the status", async () => {
    serve();
    await open();

    expect(
      screen.getByRole("heading", { level: 1, name: r.reportTitle }),
    ).toBeTruthy();
    expect(valueUnder(r.rider)).toBe("Paul Patient");
    expect(valueUnder(r.machine)).toBe("Centri Paris");
    expect(valueUnder(r.date)).toBe(
      format(STARTED, "PPPp", { locale: frLocale }),
    );
    expect(valueUnder(r.kind)).toBe(fr.training.kind.auto);
    expect(valueUnder(r.program)).toBe("Endurance");
    expect(valueUnder(r.zone)).toBe("Zone 130-150 bpm");
    expect(valueUnder(r.status)).toBe(fr.sessions.status.completed);
    // Nothing the report does not need: no address, no operator.
    expect(screen.queryByText("paul@example.test")).toBeNull();
    expect(screen.queryByText("Gaston Gestion")).toBeNull();
  });

  it("gives the figures of the final summary, and when it was computed", async () => {
    serve();
    await open();

    expect(screen.getByText(r.final)).toBeTruthy();
    expect(screen.getByText(r.finalNote)).toBeTruthy();
    expect(propsOf("SessionSummaryFigures")).toEqual({
      figures: summary().figures,
    });
    expect(
      screen.getByText(
        `Calculé le ${format(NOW, "PPPp", { locale: frLocale })}`,
      ),
    ).toBeTruthy();
  });

  it("marks a provisional summary, with why the record could not be read", async () => {
    serve({
      summary: summary({ provisional: true, recordProblem: "missing_file" }),
    });
    await open();

    expect(screen.getByText(r.provisional)).toBeTruthy();
    expect(screen.getByText(r.provisionalNote)).toBeTruthy();
    expect(
      screen.getByText(r.recordProblem.replace("{problem}", "missing_file")),
    ).toBeTruthy();
  });

  it("says what is not in the report yet: the preflight and the rider's feedback", async () => {
    serve();
    await open();

    expect(screen.getByText(r.preflight)).toBeTruthy();
    expect(screen.getByText(r.feedback)).toBeTruthy();
  });

  it("says the summary is not computed yet, and still lets the report be printed", async () => {
    serve({
      session: session({
        status: "failed",
        patient: null,
        subjectLabel: undefined,
      }),
      training: training({
        status: "failed",
        zoneLowBpm: undefined,
        profileName: undefined,
        profileId: undefined,
        kind: "manual",
      }),
      summary: null,
    });
    await open();

    expect(screen.getByText(r.none)).toBeTruthy();
    expect(valueUnder(r.rider)).toBe("-");
    expect(valueUnder(r.zone)).toBe("-");
    expect(valueUnder(r.program)).toBe("-");
    expect(valueUnder(r.kind)).toBe(fr.training.kind.manual);
    expect(
      screen.getByRole("button", { name: r.download }).hasAttribute("disabled"),
    ).toBe(false);
  });

  it("is not printed before the session has ended", async () => {
    serve({
      session: session({
        status: "active",
        endedAt: undefined,
        patient: null,
        subjectLabel: "Rider 7",
      }),
      training: training({ status: "active" }),
      summary: null,
    });
    await open();

    expect(screen.getByText(r.notFinished)).toBeTruthy();
    expect(valueUnder(r.rider)).toBe("Rider 7");
    expect(
      screen.getByRole("button", { name: r.download }).hasAttribute("disabled"),
    ).toBe(true);
    expect(wasMounted("SessionSummaryFigures")).toBe(false);
  });

  it("opens the browser's print dialog, where it is saved as PDF", async () => {
    const print = vi.fn();
    vi.stubGlobal("print", print);
    serve();
    const { user } = await open();

    await user.click(screen.getByRole("button", { name: r.download }));
    expect(print).toHaveBeenCalledTimes(1);
    expect(screen.getByText(r.downloadHint)).toBeTruthy();
    expect(screen.getByText(r.back).closest("a")?.getAttribute("href")).toBe(
      `/dashboard/sessions/${sessionId}`,
    );
  });

  it("is written in English, and opens the same report in the other language", async () => {
    serve();
    const { user } = await open("en");

    expect(
      screen.getByRole("heading", {
        level: 1,
        name: en.sessionReport.reportTitle,
      }),
    ).toBeTruthy();
    expect(valueUnder(en.sessionReport.date)).toBe(
      format(STARTED, "PPPp", { locale: enUS }),
    );
    const english = screen.getByRole("button", { name: "English" });
    expect(english.getAttribute("aria-pressed")).toBe("true");

    await user.click(screen.getByRole("button", { name: "Français" }));
    expect(router.replace).toHaveBeenCalledWith(
      `/dashboard/sessions/${sessionId}/report`,
      { locale: "fr" },
    );
  });
});

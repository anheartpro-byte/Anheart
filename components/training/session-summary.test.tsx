import type { ReactElement, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FunctionReturnType } from "convex/server";
import type { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { messages, render } from "@/test-support/render";
import { answer, asks, resetConvex } from "@/test-support/convex";
import { SessionSummaryCard } from "./SessionSummaryCard";
import {
  SessionSummaryFigures,
  type SummaryFigures,
} from "./SessionSummaryFigures";
import { RiderHistoryCard } from "./RiderHistoryCard";
import { RiderProgressChart } from "./RiderProgressChart";

/**
 * ANH-89: the summary of a session on its page, and the history of a rider
 * on theirs. What is ours is what each says of the figures the server
 * computed: every figure written, a missing one said missing (never a zero),
 * provisional or final, and the way to the report.
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

type Props = Record<string, unknown> & { children?: ReactNode };
/** What each part of the progression chart was given, in the order drawn. */
const drawn = vi.hoisted(() => [] as { part: string; props: Props }[]);
vi.mock("recharts", () => {
  const part = (name: string) => (props: Props) => {
    drawn.push({ part: name, props });
    return props.children ?? null;
  };
  return {
    BarChart: part("BarChart"),
    Bar: part("Bar"),
    XAxis: part("XAxis"),
    YAxis: part("YAxis"),
    CartesianGrid: part("CartesianGrid"),
  };
});
vi.mock("@/components/ui/chart", () => {
  const part = (name: string) => (props: Props) => {
    drawn.push({ part: name, props });
    return props.children ?? null;
  };
  return {
    ChartContainer: part("ChartContainer"),
    ChartTooltip: (props: Props) => {
      drawn.push({ part: "ChartTooltip", props });
      return props.content as ReactElement;
    },
    ChartTooltipContent: part("ChartTooltipContent"),
  };
});

const r = messages.fr.sessionReport;
const en = messages.en.sessionReport;
const sessionId = "session-1" as Id<"sessions">;
const userId = "user-rose" as Id<"users">;
const NOW = 1_800_000_000_000;

function figures(over: Partial<SummaryFigures> = {}): SummaryFigures {
  return {
    durationS: 1800,
    samples: 9000,
    heartRate: { minBpm: 92, meanBpm: 138.46, maxBpm: 152, samples: 8800 },
    zone: {
      lowBpm: 130,
      highBpm: 150,
      inZoneS: 1260,
      aboveZoneS: 30,
      belowZoneS: 480,
    },
    maxOutputRpm: 26.9937,
    maxMotorRpm: 1344,
    reference: { maxG: 1.2222, maxGRate: 0.0199, radiusM: 1.5 },
    legTip: {
      maxG: 1.9762,
      maxGRate: 0.0322,
      radiusM: 2.4254,
      measured: false,
    },
    verdicts: [
      { rule: "hr_hard_max", action: "RAMP_DOWN", firstAtS: 912.4 },
      { rule: "mystery_rule", action: null, firstAtS: null },
    ],
    verdictsOmitted: 2,
    endReason: "safety_verdict",
    endRule: "hr_critical",
    warnings: ["record_degraded", "unknown_code"],
    ...over,
  };
}

beforeEach(() => {
  resetConvex();
  drawn.length = 0;
});
afterEach(() => vi.useRealTimers());

describe("the figures of a summary", () => {
  it.each(["fr", "en"] as const)(
    "renders truncation and tick ordering warnings in %s",
    (locale) => {
      const warnings = ["events_truncated", "ticks_out_of_order"] as const;
      const screen = render(
        <SessionSummaryFigures
          provisional={false}
          recordProblem={null}
          figures={figures({ warnings: [...warnings] })}
        />,
        { locale },
      );
      for (const warning of warnings)
        expect(screen.text()).toContain(
          messages[locale].sessionReport.warnings[warning],
        );
    },
  );

  it("writes every figure of the record, in the visitor's language", () => {
    const screen = render(
      <SessionSummaryFigures
        figures={figures()}
        provisional={false}
        recordProblem={null}
      />,
    );
    const text = screen.text();
    expect(text).toContain("30 min 00 s");
    expect(text).toContain("21 min 00 s (70 %)");
    expect(text).toContain("Zone 130-150 bpm");
    expect(text).toContain("92 / 138,5 / 152 bpm");
    expect(text).toContain("26,99 tr/min");
    expect(text).toContain("moteur 1 344 tr/min");
    // Below, in, above the zone.
    expect(text).toContain(`${r.belowZone} 8 min 00 s`);
    expect(text).toContain(`${r.aboveZone} 30 s`);
    // Both radii: g, its variation, the radius, estimated or not.
    expect(text).toContain(`${r.reference} (1,5 m) 1,222 0,0199 g/s`);
    expect(text).toContain(
      `${r.legTip} (2,425 m, ${r.estimated}) 1,976 0,0322 g/s`,
    );
    // The verdicts: the rule, the action in words, when it was first seen.
    expect(text).toContain(
      `hr_hard_max (${messages.fr.training.safety.ramp_down}) à 912,4 s`,
    );
    expect(text).toContain("mystery_rule");
    expect(text).toContain("et 2 autres règles");
    expect(text).toContain(r.endReasons.safety_verdict);
    expect(text).toContain("hr_critical");
    expect(text).toContain(r.warnings.record_degraded);
    // A code the catalog does not know is shown as received.
    expect(text).toContain("unknown_code");
  });

  it("says what a provisional summary or a manual session does not have", () => {
    const screen = render(
      <SessionSummaryFigures
        provisional={true}
        recordProblem={null}
        figures={figures({
          durationS: null,
          heartRate: null,
          zone: null,
          maxOutputRpm: null,
          maxMotorRpm: null,
          reference: { maxG: null, maxGRate: null, radiusM: null },
          legTip: { maxG: null, maxGRate: null, radiusM: null, measured: null },
          verdicts: [],
          verdictsOmitted: 0,
          endReason: null,
          endRule: null,
          warnings: [],
        })}
      />,
    );
    const text = screen.text();
    expect(text).toContain(r.noZone);
    expect(text).toContain(r.noHeartRate);
    expect(text).toContain(r.noVerdicts);
    expect(text).toContain(`${r.reference} - ${r.notYet}`);
    expect(text).toContain(`${r.legTip} - ${r.notYet}`);
    expect(text).not.toContain(r.belowZone);
    expect(text).not.toContain(r.estimated);
    expect(text).not.toContain("0 bpm");
  });

  it("writes a time in zone without a share when the duration is not known", () => {
    const screen = render(
      <SessionSummaryFigures
        figures={figures({ durationS: null })}
        provisional={false}
        recordProblem={null}
      />,
    );
    expect(screen.text()).toContain(`${r.timeInZone} 21 min 00 s`);
    expect(screen.text()).not.toContain("%");
  });

  it("writes an end reason it does not know as the machine sent it", () => {
    const screen = render(
      <SessionSummaryFigures
        provisional={false}
        recordProblem={null}
        figures={figures({
          endReason: "Arrêt demandé à distance",
          endRule: null,
        })}
      />,
      { locale: "en" },
    );
    expect(screen.text()).toContain("Arrêt demandé à distance");
    expect(screen.text()).toContain(en.endRule);
  });
});

describe("the summary card of a session", () => {
  describe.each(["fr", "en"] as const)(
    "T03-a: missing g variation in %s",
    (locale) => {
      it.each([
        ["final", false, null, "gRateNoWindow", "gRateNoWindowNote"],
        ["waiting for the record", true, null, "notYet", null],
        [
          "unreadable record",
          true,
          "invalid_ticks",
          "gRateUnavailable",
          "gRateUnavailableNote",
        ],
      ] as const)(
        "explains a %s summary at both radii",
        (_state, provisional, recordProblem, key, note) => {
          const catalog = messages[locale].sessionReport;
          answer(
            "sessionReports:getSummary",
            summary({
              provisional,
              recordProblem,
              figures: figures({
                reference: { maxG: 0.5, maxGRate: null, radiusM: null },
                legTip: {
                  maxG: 0.8,
                  maxGRate: null,
                  radiusM: null,
                  measured: null,
                },
              }),
            }),
          );
          const screen = render(<SessionSummaryCard sessionId={sessionId} />, {
            locale,
          });
          const rows = screen.tag("tr").slice(1).map(screen.textOf);
          expect(rows).toHaveLength(2);
          for (const row of rows) expect(row).toContain(catalog[key]);
          if (key !== "notYet")
            expect(screen.text()).not.toContain(catalog.notYet);
          if (note) expect(screen.text()).toContain(catalog[note]);
          else {
            expect(screen.text()).not.toContain(catalog.gRateNoWindowNote);
            expect(screen.text()).not.toContain(catalog.gRateUnavailableNote);
          }
        },
      );

      it("keeps measured values in g/s and explains only a missing radius", () => {
        const catalog = messages[locale].sessionReport;
        answer("sessionReports:getSummary", summary());
        const screen = render(<SessionSummaryCard sessionId={sessionId} />, {
          locale,
        });
        expect(screen.text()).toContain(
          locale === "fr" ? "0,0199 g/s" : "0.0199 g/s",
        );
        expect(screen.text()).toContain(
          locale === "fr" ? "0,0322 g/s" : "0.0322 g/s",
        );
        expect(screen.text()).not.toContain(catalog.gRateNoWindowNote);
        answer(
          "sessionReports:getSummary",
          summary({
            figures: figures({
              legTip: {
                maxG: 0.8,
                maxGRate: null,
                radiusM: null,
                measured: null,
              },
            }),
          }),
        );
        screen.rerender(<SessionSummaryCard sessionId={sessionId} />);
        expect(screen.text()).toContain(catalog.gRateNoWindowNote);
        expect(screen.text()).toContain(
          locale === "fr" ? "0,0199 g/s" : "0.0199 g/s",
        );
        expect(screen.text()).not.toContain(catalog.notYet);
      });
    },
  );

  it.each(["fr", "en"] as const)(
    "distinguishes a session that did not start from an uncomputed summary in %s",
    (locale) => {
      const catalog = messages[locale].sessionReport;
      answer("sessionReports:getSummary", { absent: "not_started" });
      const screen = render(<SessionSummaryCard sessionId={sessionId} />, {
        locale,
      });
      expect(screen.text()).toContain(catalog.notStartedNote);
      expect(screen.text()).not.toContain(catalog.none);
      expect(screen.text()).not.toContain(catalog.notComputed);
      expect(screen.text()).not.toContain(catalog.final);
      answer("sessionReports:getSummary", { absent: "not_computed" });
      screen.rerender(<SessionSummaryCard sessionId={sessionId} />);
      expect(screen.text()).toContain(catalog.none);
      expect(screen.text()).not.toContain(catalog.notStartedNote);
    },
  );
  type Summary = Exclude<
    NonNullable<FunctionReturnType<typeof api.sessionReports.getSummary>>,
    { absent: string }
  >;
  const summary = (over: Partial<Summary> = {}): Summary => ({
    provisional: false,
    computedAt: NOW,
    recordProblem: null,
    figures: figures(),
    ...over,
  });
  const reportLink = (screen: ReturnType<typeof render>) =>
    screen.tag("a").map((a) => a.getAttribute("href"));

  it("waits for the summary, then says it is not computed yet, with the way to the report", () => {
    const screen = render(<SessionSummaryCard sessionId={sessionId} />);
    expect(asks("sessionReports:getSummary")).toEqual([{ sessionId }]);
    expect(screen.text()).toBe("");

    answer("sessionReports:getSummary", null);
    screen.rerender(<SessionSummaryCard sessionId={sessionId} />);
    expect(screen.text()).toContain(r.none);
    expect(screen.text()).not.toContain(r.provisional);
    expect(reportLink(screen)).toEqual([
      `/dashboard/sessions/${sessionId}/report`,
    ]);
  });

  it("marks a summary of the telemetry provisional, and says why the record was not read", () => {
    answer(
      "sessionReports:getSummary",
      summary({ provisional: true, recordProblem: "invalid_ticks" }),
    );
    const screen = render(<SessionSummaryCard sessionId={sessionId} />);
    expect(screen.text()).toContain(r.provisional);
    expect(screen.text()).toContain(r.provisionalNote);
    expect(screen.text()).toContain("(invalid_ticks)");
    expect(screen.text()).toContain("30 min 00 s");
  });

  it("marks the summary of the record final", () => {
    answer("sessionReports:getSummary", summary());
    const screen = render(<SessionSummaryCard sessionId={sessionId} />);
    expect(screen.text()).toContain(r.final);
    expect(screen.text()).toContain(r.finalNote);
    expect(screen.text()).not.toContain(r.provisionalNote);
  });
});

describe("the history of a rider", () => {
  type Row = FunctionReturnType<typeof api.sessionReports.riderHistory>[number];
  const row = (over: Partial<Row> = {}): Row => ({
    sessionId,
    status: "completed",
    kind: "auto",
    startedAt: NOW - 86_400_000,
    endedAt: NOW - 86_400_000 + 1_800_000,
    machineName: "Centri Paris",
    notStarted: false,
    profileName: "Endurance",
    summary: {
      provisional: false,
      durationS: 1800,
      inZoneS: 900,
      zoneLowBpm: 130,
      zoneHighBpm: 150,
      meanBpm: 138.46,
      maxBpm: 152,
      maxGReference: 1.2,
    },
    nauseaScore: null,
    ...over,
  });

  it.each(["fr", "en"] as const)(
    "labels a session that did not start and draws no progress bar in %s",
    (locale) => {
      const catalog = messages[locale].sessionReport;
      answer("sessionReports:riderHistory", [
        row({ status: "failed", notStarted: true, summary: null }),
      ]);
      const screen = render(<RiderHistoryCard userId={userId} />, { locale });
      expect(screen.text()).toContain(catalog.notStarted);
      expect(screen.text()).not.toContain(catalog.notComputed);
      expect(drawn.some((d) => d.part === "BarChart")).toBe(false);
      answer("sessionReports:riderHistory", [row({ summary: null })]);
      screen.rerender(<RiderHistoryCard userId={userId} />);
      expect(screen.text()).toContain(catalog.notComputed);
      expect(screen.text()).not.toContain(catalog.notStarted);
    },
  );

  it("waits for the history, then says when the rider has no finished session", () => {
    const screen = render(<RiderHistoryCard userId={userId} />);
    expect(asks("sessionReports:riderHistory")).toEqual([{ userId }]);
    expect(screen.text()).not.toContain(r.historyEmpty);

    answer("sessionReports:riderHistory", []);
    screen.rerender(<RiderHistoryCard userId={userId} />);
    expect(screen.text()).toContain(r.historyEmpty);
    expect(screen.text()).toContain(r.nausea);
  });

  it("lists each session with its figures and its report, and draws the time in zone, oldest first", () => {
    const second = "session-2" as Id<"sessions">;
    const manual = "session-3" as Id<"sessions">;
    const cancelled = "session-4" as Id<"sessions">;
    answer("sessionReports:riderHistory", [
      row({
        sessionId: second,
        startedAt: NOW,
        summary: { ...row().summary!, provisional: true, inZoneS: 1500 },
      }),
      row({
        sessionId: manual,
        kind: "manual",
        profileName: undefined,
        summary: {
          ...row().summary!,
          inZoneS: null,
          zoneLowBpm: null,
          zoneHighBpm: null,
          meanBpm: null,
          durationS: null,
        },
      }),
      row(),
      row({ sessionId: cancelled, status: "failed", summary: null }),
    ]);
    const screen = render(<RiderHistoryCard userId={userId} />);
    const text = screen.text();
    expect(text).toContain("25 min 00 s (83 %)");
    expect(text).toContain("15 min 00 s (50 %)");
    expect(text).toContain("138,5 bpm");
    expect(text).toContain(r.provisional);
    expect(text).toContain(r.notComputed);
    expect(text).toContain(messages.fr.sessions.status.failed);
    expect(screen.tag("a").map((a) => a.getAttribute("href"))).toEqual(
      [second, manual, sessionId, cancelled].map(
        (id) => `/dashboard/sessions/${id}/report`,
      ),
    );
    const bars = drawn.find((d) => d.part === "BarChart")?.props.data;
    expect(bars).toEqual([
      {
        sessionId,
        startedAt: NOW - 86_400_000,
        inZoneMin: 15,
        provisional: false,
      },
      { sessionId: second, startedAt: NOW, inZoneMin: 25, provisional: true },
    ]);
  });

  it("draws no chart when no session has a time in zone", () => {
    answer("sessionReports:riderHistory", [
      row({ summary: { ...row().summary!, inZoneS: null } }),
    ]);
    const screen = render(<RiderHistoryCard userId={userId} />);
    expect(screen.text()).toContain(r.progressionEmpty);
    expect(screen.text()).toContain("30 min 00 s - 138,5 bpm");
    expect(drawn.some((d) => d.part === "BarChart")).toBe(false);
  });

  it("writes a time in zone without a share when the duration is not known", () => {
    answer("sessionReports:riderHistory", [
      row({ summary: { ...row().summary!, durationS: null } }),
    ]);
    const screen = render(<RiderHistoryCard userId={userId} />, {
      locale: "en",
    });
    expect(screen.text()).toContain("- 15 min 00 s 138.5 bpm");
    expect(drawn.some((d) => d.part === "BarChart")).toBe(false);
  });

  it("draws no zero-minute bar for a summary without a duration", () => {
    answer("sessionReports:riderHistory", [
      row({ summary: { ...row().summary!, durationS: null, inZoneS: 0 } }),
    ]);
    const screen = render(<RiderHistoryCard userId={userId} />);
    expect(screen.text()).toContain(r.progressionEmpty);
    expect(drawn.some((d) => d.part === "BarChart")).toBe(false);
  });
});

describe("the progression chart", () => {
  it("draws one bar per session, named by its day, with a tooltip of the same day", () => {
    render(
      <RiderProgressChart
        points={[
          { sessionId, startedAt: NOW, inZoneMin: 12.5, provisional: false },
        ]}
      />,
    );
    const axis = drawn.find((d) => d.part === "XAxis")?.props;
    const format = axis?.tickFormatter as (value: number) => string;
    expect(format(NOW)).toBe("15 janv.");
    const tooltip = drawn.find((d) => d.part === "ChartTooltipContent")?.props;
    const label = tooltip?.labelFormatter as (
      value: unknown,
      payload: { payload: { startedAt: number } }[],
    ) => string;
    expect(label(null, [{ payload: { startedAt: NOW } }])).toBe("15 janv.");
    expect(label(null, [])).toBe("1 janv.");
    const config = drawn.find((d) => d.part === "ChartContainer")?.props
      .config as { inZoneMin: { label: string } };
    expect(config.inZoneMin.label).toBe(
      `${r.timeInZone} (${r.progressionUnit})`,
    );
  });
});

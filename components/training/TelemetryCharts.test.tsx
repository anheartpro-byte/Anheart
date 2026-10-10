import type { ReactElement, ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import fr from "@/messages/fr.json";
import type { TelemetryPoint } from "@/lib/training";
import { textOf } from "../markup.test-helpers";
import { TelemetryCharts } from "./TelemetryCharts";

/**
 * The two charts of a session: heart rate against the target zone, arm speed
 * against its setpoint. A chart is drawn by the browser; what is ours is what
 * it is given to draw, and that is what is proven here: the rows, the scale
 * of the heart-rate axis, the zone band, the legend texts, the time written
 * on the axis and in the tooltip.
 *
 * The chart library and our wrapper of it are replaced by stand-ins that
 * record what they receive (the wrapper has its own tests in components/ui).
 */

type Props = Record<string, unknown> & { children?: ReactNode };
/** What each part of a chart was given, in the order drawn. */
const drawn = vi.hoisted(() => [] as { part: string; props: Props }[]);

vi.mock("recharts", () => {
  const part = (name: string) => (props: Props) => {
    drawn.push({ part: name, props });
    return props.children ?? null;
  };
  return {
    LineChart: part("LineChart"),
    Line: part("Line"),
    XAxis: part("XAxis"),
    YAxis: part("YAxis"),
    CartesianGrid: part("CartesianGrid"),
    ReferenceArea: part("ReferenceArea"),
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
      // The content of a tooltip is drawn by the library on hover: drawn here at once.
      return props.content as ReactElement;
    },
    ChartTooltipContent: part("ChartTooltipContent"),
    ChartLegend: part("ChartLegend"),
    ChartLegendContent: part("ChartLegendContent"),
  };
});

function point(over: Partial<TelemetryPoint>): TelemetryPoint {
  return {
    t: 0,
    elapsedS: 0,
    phase: "hold",
    motorRpm: 1000,
    outputRpm: 20,
    setpointMotorRpm: 1000,
    gLoad: 1.5,
    safetyAction: "none",
    ...over,
  };
}

function draw(props: Parameters<typeof TelemetryCharts>[0]) {
  drawn.length = 0;
  return renderToStaticMarkup(
    <NextIntlClientProvider locale="fr" messages={fr} timeZone="Europe/Paris">
      <TelemetryCharts {...props} />
    </NextIntlClientProvider>,
  );
}

/** What the parts of one name were given, the heart-rate chart first, the speed chart second. */
const given = (part: string) =>
  drawn.filter((entry) => entry.part === part).map((entry) => entry.props);

beforeEach(() => {
  drawn.length = 0;
});

describe("ANH-203 telemetry charts: the rows handed to both charts", () => {
  it("one row per point: elapsed time, heart rate, arm speed and setpoint in arm turns", () => {
    draw({
      points: [
        point({
          elapsedS: 5,
          bpm: 120,
          outputRpm: 20.456,
          setpointMotorRpm: 995.8,
        }),
        point({
          elapsedS: 10,
          bpm: 124,
          outputRpm: 20.004,
          setpointMotorRpm: 1095.38,
        }),
      ],
    });

    const rows = [
      // 995.8 / 49.79 = 20, 1095.38 / 49.79 = 22: the setpoint is drawn in turns of the arm.
      { elapsedS: 5, bpm: 120, outputRpm: 20.46, setpointRpm: 20 },
      { elapsedS: 10, bpm: 124, outputRpm: 20, setpointRpm: 22 },
    ];
    expect(given("LineChart").map((props) => props.data)).toEqual([rows, rows]);
  });

  it("a point without a heart rate stays a gap: never a zero, and the line is not joined across it", () => {
    draw({
      points: [
        point({ bpm: 120 }),
        point({ elapsedS: 5 }),
        point({ elapsedS: 10, bpm: 126 }),
      ],
    });

    const [rows] = given("LineChart").map(
      (props) => props.data as { bpm: number | null }[],
    );
    expect(rows.map((row) => row.bpm)).toEqual([120, null, 126]);
    const [heartRate] = given("Line");
    expect(heartRate).toMatchObject({ dataKey: "bpm", connectNulls: false });
  });

  it("draws the setpoint and the measured speed on the second chart, each under its own name", () => {
    draw({ points: [point({ bpm: 120 })] });

    expect(given("Line").map((props) => props.dataKey)).toEqual([
      "bpm",
      "setpointRpm",
      "outputRpm",
    ]);
    const [heartRate, speed] = given("ChartContainer").map(
      (props) => props.config as Record<string, { label: string }>,
    );
    expect(
      Object.fromEntries(
        Object.entries(heartRate).map(([key, value]) => [key, value.label]),
      ),
    ).toEqual({
      bpm: "Fréquence cardiaque (bpm)",
    });
    expect(
      Object.fromEntries(
        Object.entries(speed).map(([key, value]) => [key, value.label]),
      ),
    ).toEqual({
      outputRpm: "Mesurée (tr/min)",
      setpointRpm: "Consigne (tr/min)",
    });
    // Only the speed chart has two lines to tell apart: it alone carries a legend.
    expect(given("ChartLegend")).toHaveLength(1);
  });
});

describe("ANH-203 telemetry charts: the scale of the heart-rate axis", () => {
  /** The heart-rate axis is the first Y axis drawn. */
  const domain = () => given("YAxis")[0].domain;

  it("always shows 60 to 120 at least, with a margin of ten, even without any heart rate", () => {
    draw({ points: [point({})] });

    expect(domain()).toEqual([50, 130]);
  });

  it("widens to the lowest and highest heart rate measured", () => {
    draw({ points: [point({ bpm: 48 }), point({ bpm: 171 })] });

    // 48 - 10 rounded down to the ten, 171 + 10 rounded up to the ten.
    expect(domain()).toEqual([30, 190]);
  });

  it("widens to the target zone when no point reaches it", () => {
    draw({ points: [point({ bpm: 100 })], zoneLowBpm: 55, zoneHighBpm: 150 });

    expect(domain()).toEqual([40, 160]);
  });

  it("leaves the speed axis to scale itself", () => {
    draw({ points: [point({ bpm: 100 })] });

    expect(given("YAxis")[1].domain).toBeUndefined();
  });
});

describe("ANH-203 telemetry charts: the target zone", () => {
  it("shades the zone and names it above the chart when the session has one", () => {
    const html = draw({
      points: [point({ bpm: 120 })],
      zoneLowBpm: 110,
      zoneHighBpm: 140,
    });

    expect(given("ReferenceArea")).toHaveLength(1);
    expect(given("ReferenceArea")[0]).toMatchObject({ y1: 110, y2: 140 });
    expect(textOf(html, "")).toContain("Zone cible 110-140 bpm");
  });

  it.each([
    ["no zone at all", {}],
    ["only a lower bound", { zoneLowBpm: 110 }],
    ["only an upper bound", { zoneHighBpm: 140 }],
  ])("shades nothing and names no zone with %s", (_case, zone) => {
    const html = draw({ points: [point({ bpm: 120 })], ...zone });

    expect(given("ReferenceArea")).toEqual([]);
    expect(textOf(html, "")).not.toContain("Zone cible");
  });
});

describe("ANH-203 telemetry charts: time and size", () => {
  it("writes the elapsed time as a clock on both axes", () => {
    draw({ points: [point({ bpm: 120 })] });

    for (const axis of given("XAxis")) {
      expect(axis.dataKey).toBe("elapsedS");
      const tick = axis.tickFormatter as (seconds: number) => string;
      expect([tick(0), tick(125), tick(3725)]).toEqual([
        "0:00",
        "2:05",
        "1:02:05",
      ]);
    }
  });

  it("titles each tooltip with the elapsed time of the point under the pointer", () => {
    draw({ points: [point({ bpm: 120 })] });

    const formatters = given("ChartTooltipContent").map(
      (props) =>
        props.labelFormatter as (label: unknown, payload?: unknown[]) => string,
    );
    expect(formatters).toHaveLength(2);
    for (const format of formatters) {
      expect(format("ignored", [{ payload: { elapsedS: 754 } }])).toBe("12:34");
      // Nothing under the pointer: the start, not "NaN".
      expect(format("ignored", [])).toBe("0:00");
      expect(format("ignored", undefined)).toBe("0:00");
    }
  });

  it("gives both charts the height asked for, 220 by default", () => {
    draw({ points: [point({ bpm: 120 })] });
    expect(given("ChartContainer").map((props) => props.style)).toEqual([
      { height: 220 },
      { height: 220 },
    ]);

    draw({ points: [point({ bpm: 120 })], height: 300 });
    expect(given("ChartContainer").map((props) => props.style)).toEqual([
      { height: 300 },
      { height: 300 },
    ]);
  });
});

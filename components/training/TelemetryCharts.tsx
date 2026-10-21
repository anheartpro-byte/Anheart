"use client";

import { useMemo, type ReactElement } from "react";
import { useTranslations } from "next-intl";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  XAxis,
  YAxis,
} from "recharts";
import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { armRpm, formatClock, type TelemetryPoint } from "@/lib/training";

type Row = {
  elapsedS: number;
  bpm: number | null;
  outputRpm: number;
  setpointRpm: number;
};

/**
 * Heart rate over time against the target zone (shaded band), and the arm
 * speed against its setpoint. Missing heart rate stays a gap in the line:
 * absence is never drawn as a value.
 */
export function TelemetryCharts({
  points,
  zoneLowBpm,
  zoneHighBpm,
  height = 220,
  width,
}: {
  points: TelemetryPoint[];
  zoneLowBpm?: number;
  zoneHighBpm?: number;
  height?: number;
  /** Explicit SVG dimensions for the report's separate print rendition. */
  width?: number;
}) {
  const t = useTranslations("training");

  const rows = useMemo<Row[]>(
    () =>
      points.map((p) => ({
        elapsedS: p.elapsedS,
        bpm: p.bpm ?? null,
        outputRpm: Number(p.outputRpm.toFixed(2)),
        setpointRpm: Number(armRpm(p.setpointMotorRpm).toFixed(2)),
      })),
    [points],
  );

  const hrConfig = {
    bpm: {
      label: `${t("session.hrChart")} (${t("units.bpm")})`,
      theme: { light: "#dc2626", dark: "#f87171" },
    },
  } satisfies ChartConfig;

  const speedConfig = {
    outputRpm: {
      label: `${t("session.measured")} (${t("units.rpm")})`,
      theme: { light: "#2563eb", dark: "#60a5fa" },
    },
    setpointRpm: {
      label: `${t("session.setpoint")} (${t("units.rpm")})`,
      theme: { light: "#737373", dark: "#a3a3a3" },
    },
  } satisfies ChartConfig;

  const bpmValues = rows
    .map((r) => r.bpm)
    .filter((b): b is number => b !== null);
  const hrMin = Math.min(...bpmValues, zoneLowBpm ?? Infinity, 60);
  const hrMax = Math.max(...bpmValues, zoneHighBpm ?? -Infinity, 120);
  const hrDomain: [number, number] = [
    Math.floor((hrMin - 10) / 10) * 10,
    Math.ceil((hrMax + 10) / 10) * 10,
  ];

  const xTick = (v: number) => formatClock(v);

  return (
    <div className="space-y-6">
      <div className="print:break-inside-avoid">
        <p className="text-sm font-medium mb-2">
          {t("session.hrChart")}
          {zoneLowBpm !== undefined && zoneHighBpm !== undefined && (
            <span className="ml-2 text-xs font-normal text-muted-foreground">
              <span className="inline-block h-2 w-3 rounded-sm bg-green-500/25 align-middle mr-1" />
              {t("session.zoneBand")} {zoneLowBpm}-{zoneHighBpm}{" "}
              {t("units.bpm")}
            </span>
          )}
        </p>
        <ChartFrame config={hrConfig} height={height} width={width}>
          <LineChart
            data={rows}
            width={width}
            height={width === undefined ? undefined : height}
            margin={{ left: 0, right: 12, top: 8 }}
          >
            <CartesianGrid vertical={false} />
            <XAxis
              dataKey="elapsedS"
              type="number"
              domain={["dataMin", "dataMax"]}
              tickFormatter={xTick}
              tickLine={false}
              axisLine={false}
              minTickGap={32}
            />
            <YAxis
              domain={hrDomain}
              tickLine={false}
              axisLine={false}
              width={36}
            />
            {zoneLowBpm !== undefined && zoneHighBpm !== undefined && (
              <ReferenceArea
                y1={zoneLowBpm}
                y2={zoneHighBpm}
                fill="#22c55e"
                fillOpacity={0.15}
                strokeOpacity={0}
                ifOverflow="extendDomain"
              />
            )}
            {width === undefined && (
              <ChartTooltip
                content={
                  <ChartTooltipContent
                    labelFormatter={(_, payload) =>
                      formatClock(Number(payload?.[0]?.payload?.elapsedS ?? 0))
                    }
                  />
                }
              />
            )}
            <Line
              dataKey="bpm"
              type="monotone"
              stroke={
                width === undefined
                  ? "var(--color-bpm)"
                  : hrConfig.bpm.theme.light
              }
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
              connectNulls={false}
            />
          </LineChart>
        </ChartFrame>
      </div>

      <div className="print:break-inside-avoid">
        <p className="text-sm font-medium mb-2">{t("session.speedChart")}</p>
        <ChartFrame config={speedConfig} height={height} width={width}>
          <LineChart
            data={rows}
            width={width}
            height={width === undefined ? undefined : height}
            margin={{ left: 0, right: 12, top: 8 }}
          >
            <CartesianGrid vertical={false} />
            <XAxis
              dataKey="elapsedS"
              type="number"
              domain={["dataMin", "dataMax"]}
              tickFormatter={xTick}
              tickLine={false}
              axisLine={false}
              minTickGap={32}
            />
            <YAxis tickLine={false} axisLine={false} width={36} />
            {width === undefined && (
              <ChartTooltip
                content={
                  <ChartTooltipContent
                    labelFormatter={(_, payload) =>
                      formatClock(Number(payload?.[0]?.payload?.elapsedS ?? 0))
                    }
                  />
                }
              />
            )}
            <ChartLegend
              content={
                width === undefined ? (
                  <ChartLegendContent />
                ) : (
                  <div className="flex justify-center gap-4 pt-3 text-xs">
                    {Object.entries(speedConfig).map(([key, item]) => (
                      <span key={key} className="flex items-center gap-1.5">
                        <svg width="12" height="8" aria-hidden="true">
                          <line
                            x1="0"
                            y1="4"
                            x2="12"
                            y2="4"
                            stroke={item.theme.light}
                            strokeWidth="2"
                            strokeDasharray={
                              key === "setpointRpm" ? "4 4" : undefined
                            }
                          />
                        </svg>
                        {item.label}
                      </span>
                    ))}
                  </div>
                )
              }
            />
            <Line
              dataKey="setpointRpm"
              type="stepAfter"
              stroke={
                width === undefined
                  ? "var(--color-setpointRpm)"
                  : speedConfig.setpointRpm.theme.light
              }
              strokeWidth={1.5}
              strokeDasharray="4 4"
              dot={false}
              isAnimationActive={false}
            />
            <Line
              dataKey="outputRpm"
              type="monotone"
              stroke={
                width === undefined
                  ? "var(--color-outputRpm)"
                  : speedConfig.outputRpm.theme.light
              }
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
          </LineChart>
        </ChartFrame>
      </div>
    </div>
  );
}

function ChartFrame({
  config,
  width,
  height,
  children,
}: {
  config: ChartConfig;
  width?: number;
  height: number;
  children: ReactElement;
}) {
  if (width !== undefined)
    return (
      <div
        data-slot="chart"
        className="text-xs [&_.recharts-cartesian-axis-tick_text]:fill-muted-foreground [&_.recharts-cartesian-grid_line]:stroke-border"
        style={{ width, height }}
      >
        {children}
      </div>
    );

  return (
    <ChartContainer
      config={config}
      className="aspect-auto w-full"
      style={{ height }}
    >
      {children}
    </ChartContainer>
  );
}

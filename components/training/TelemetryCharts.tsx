"use client";

import { useMemo } from "react";
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
}: {
  points: TelemetryPoint[];
  zoneLowBpm?: number;
  zoneHighBpm?: number;
  height?: number;
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
      <div>
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
        <ChartContainer
          config={hrConfig}
          className="aspect-auto w-full"
          style={{ height }}
        >
          <LineChart data={rows} margin={{ left: 0, right: 12, top: 8 }}>
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
            <ChartTooltip
              content={
                <ChartTooltipContent
                  labelFormatter={(_, payload) =>
                    formatClock(Number(payload?.[0]?.payload?.elapsedS ?? 0))
                  }
                />
              }
            />
            <Line
              dataKey="bpm"
              type="monotone"
              stroke="var(--color-bpm)"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
              connectNulls={false}
            />
          </LineChart>
        </ChartContainer>
      </div>

      <div>
        <p className="text-sm font-medium mb-2">{t("session.speedChart")}</p>
        <ChartContainer
          config={speedConfig}
          className="aspect-auto w-full"
          style={{ height }}
        >
          <LineChart data={rows} margin={{ left: 0, right: 12, top: 8 }}>
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
            <ChartTooltip
              content={
                <ChartTooltipContent
                  labelFormatter={(_, payload) =>
                    formatClock(Number(payload?.[0]?.payload?.elapsedS ?? 0))
                  }
                />
              }
            />
            <ChartLegend content={<ChartLegendContent />} />
            <Line
              dataKey="setpointRpm"
              type="stepAfter"
              stroke="var(--color-setpointRpm)"
              strokeWidth={1.5}
              strokeDasharray="4 4"
              dot={false}
              isAnimationActive={false}
            />
            <Line
              dataKey="outputRpm"
              type="monotone"
              stroke="var(--color-outputRpm)"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
          </LineChart>
        </ChartContainer>
      </div>
    </div>
  );
}

"use client";

import { useLocale, useTranslations } from "next-intl";
import { format } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import type { ProgressionPoint } from "@/lib/sessionReport";

/**
 * A rider's progression (ANH-89): the time in zone of each programmed
 * session, oldest first, one bar per session. One series: no legend, the
 * title names it; the history table beside it holds every value.
 */
export function RiderProgressChart({
  points,
  height = 200,
}: {
  points: ProgressionPoint[];
  height?: number;
}) {
  const t = useTranslations("sessionReport");
  const dateLocale = useLocale() === "fr" ? fr : enUS;
  const day = (startedAt: number) =>
    format(startedAt, "d MMM", { locale: dateLocale });

  const config = {
    inZoneMin: {
      label: `${t("timeInZone")} (${t("progressionUnit")})`,
      theme: { light: "#2563eb", dark: "#60a5fa" },
    },
  } satisfies ChartConfig;

  return (
    <ChartContainer
      config={config}
      className="aspect-auto w-full"
      style={{ height }}
    >
      <BarChart data={points} margin={{ left: 0, right: 12, top: 8 }}>
        <CartesianGrid vertical={false} />
        <XAxis
          dataKey="startedAt"
          tickFormatter={day}
          tickLine={false}
          axisLine={false}
          minTickGap={16}
        />
        <YAxis tickLine={false} axisLine={false} width={36} />
        <ChartTooltip
          content={
            <ChartTooltipContent
              labelFormatter={(_, payload) =>
                day(Number(payload?.[0]?.payload?.startedAt ?? 0))
              }
            />
          }
        />
        <Bar
          dataKey="inZoneMin"
          fill="var(--color-inZoneMin)"
          radius={[4, 4, 0, 0]}
          maxBarSize={24}
          isAnimationActive={false}
        />
      </BarChart>
    </ChartContainer>
  );
}

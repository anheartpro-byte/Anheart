"use client";

import { useQuery } from "convex/react";
import type { FunctionReturnType } from "convex/server";
import { useLocale, useTranslations } from "next-intl";
import { AlertTriangle, Activity } from "lucide-react";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { useFreshness } from "@/hooks/use-freshness";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";

type HealthData = NonNullable<FunctionReturnType<typeof api.machineHealth.get>>;
type Health = NonNullable<HealthData["health"]>;
const DAY_MS = 86_400_000;
const BUCKET_MS = 300_000;

export function healthWarnings(health: Health): string[] {
  const warnings: string[] = [];
  if (health.cpu_temp_c !== null && health.cpu_temp_c > 70)
    warnings.push("hot");
  if (health.disk_free_mb !== null && health.disk_free_mb < 1024)
    warnings.push("lowDisk");
  if (health.clock_offset_s !== null && Math.abs(health.clock_offset_s) > 5)
    warnings.push("clock");
  if (health.bitalino_battery_pct !== null && health.bitalino_battery_pct < 30)
    warnings.push("battery");
  if (health.record_degraded === true) warnings.push("degraded");
  if (health.restart_loop === true) warnings.push("restart");
  if (health.drive_link === "down") warnings.push("driveDown");
  if (health.bitalino_link === "down") warnings.push("bitalinoDown");
  return warnings;
}

function History({
  history,
  now,
}: {
  history: HealthData["history"];
  now: number;
}) {
  const t = useTranslations("machineHealth");
  const locale = useLocale();
  const start = now - DAY_MS;
  const rows = history.filter(
    (row) => row.updatedAt >= start && row.bucket <= now,
  );
  const date = (time: number) =>
    new Intl.DateTimeFormat(locale, {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(time);
  return (
    <section className="space-y-4" aria-label={t("history")}>
      <h3 className="font-medium text-sm">{t("history")}</h3>
      {rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("noHistory")}</p>
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-2">
            {(["cpu_temp_c", "disk_free_mb"] as const).map((key) => {
              const values = rows
                .flatMap((row) => [row.min[key], row.max[key]])
                .filter((value): value is number => value !== null);
              if (values.length === 0)
                return (
                  <figure key={key} className="min-w-0 space-y-2">
                    <figcaption className="text-sm text-muted-foreground">
                      {t(key)}
                    </figcaption>
                    <p className="text-sm text-muted-foreground">
                      {t("noHistory")}
                    </p>
                  </figure>
                );
              const low = Math.min(0, ...values);
              const high = Math.max(1, ...values);
              const y = (value: number) =>
                96 - ((value - low) / (high - low)) * 88;
              const x = (time: number) =>
                Math.max(0, ((time - start) / DAY_MS) * 300);
              let previous: number | null = null;
              const path = rows
                .map((row) => {
                  const value = row.last[key];
                  if (value === null) {
                    previous = null;
                    return "";
                  }
                  const command =
                    previous !== null && row.bucket - previous === BUCKET_MS
                      ? "L"
                      : "M";
                  previous = row.bucket;
                  return `${command}${x(row.bucket)},${y(value)}`;
                })
                .join(" ");
              return (
                <figure key={key} className="min-w-0 space-y-2">
                  <figcaption className="text-sm text-muted-foreground">
                    {t(key)}
                  </figcaption>
                  <svg
                    className="h-32 w-full text-chart-3"
                    viewBox="0 0 300 104"
                    role="img"
                    aria-label={t("plot", { metric: t(key) })}
                    preserveAspectRatio="none"
                  >
                    {rows.map((row) => {
                      const min = row.min[key];
                      const max = row.max[key];
                      return min === null || max === null ? null : (
                        <line
                          key={row.bucket}
                          x1={x(row.bucket)}
                          x2={x(row.bucket)}
                          y1={y(min)}
                          y2={y(max)}
                          stroke="currentColor"
                          strokeOpacity="0.3"
                          strokeWidth="2"
                        />
                      );
                    })}
                    <path
                      d={path}
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.5"
                    />
                    {rows.map((row) =>
                      row.last[key] === null ? null : (
                        <circle
                          key={row.bucket}
                          cx={x(row.bucket)}
                          cy={y(row.last[key])}
                          r="1"
                          fill="currentColor"
                        />
                      ),
                    )}
                  </svg>
                  <p className="text-xs text-muted-foreground tabular-nums">
                    {low.toFixed(0)}–{high.toFixed(0)}{" "}
                    {key === "cpu_temp_c" ? "°C" : "MiB"}
                  </p>
                </figure>
              );
            })}
          </div>
          <p className="text-xs text-muted-foreground">
            {date(start)} — {date(now)} · {t("legend")}
          </p>
          <details className="text-sm">
            <summary className="cursor-pointer rounded-sm focus-visible:outline-ring focus-visible:outline-2">
              {t("values")}
            </summary>
            <div className="overflow-x-auto mt-2">
              <table className="w-full text-left text-xs tabular-nums">
                <caption className="sr-only">{t("history")}</caption>
                <thead>
                  <tr>
                    <th scope="col">{t("time")}</th>
                    <th scope="col">{t("cpu_temp_c")}</th>
                    <th scope="col">{t("disk_free_mb")}</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.bucket}>
                      <th scope="row" className="py-2 font-normal">
                        {date(row.bucket)}
                      </th>
                      {(["cpu_temp_c", "disk_free_mb"] as const).map((key) => (
                        <td key={key}>
                          {[row.min[key], row.max[key], row.last[key]]
                            .map((value) =>
                              value === null ? "—" : value.toFixed(1),
                            )
                            .join(" / ")}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </>
      )}
    </section>
  );
}

export function HealthReadouts({ data }: { data: HealthData }) {
  const t = useTranslations("machineHealth");
  const locale = useLocale();
  const { fresh, now } = useFreshness(data.updatedAt, data.serverNow, 30_000);
  const health = data.health;
  if (health === null)
    return <p className="text-sm text-muted-foreground">{t("noData")}</p>;
  const number = (value: number | null, unit = "") =>
    value === null
      ? t("unknown")
      : `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(value)}${unit}`;
  const state = (value: boolean | null) =>
    value === null ? t("unknown") : value ? t("yes") : t("no");
  const link = (value: "up" | "down" | null) =>
    value === null ? t("unknown") : t(value);
  const warnings = healthWarnings(health);
  const readings: [string, string][] = [
    ["cpu_temp_c", number(health.cpu_temp_c, " °C")],
    ["cpu_load_1m", number(health.cpu_load_1m)],
    ["mem_used_pct", number(health.mem_used_pct, " %")],
    ["disk_free_mb", number(health.disk_free_mb, " MiB")],
    [
      "uptime_s",
      number(health.uptime_s === null ? null : health.uptime_s / 3600, " h"),
    ],
    ["clock_offset_s", number(health.clock_offset_s, " s")],
    ["link_rtt_ms", number(health.link_rtt_ms, " ms")],
    ["bitalino_battery_pct", number(health.bitalino_battery_pct, " %")],
    ["bitalino_link", link(health.bitalino_link)],
    ["drive_link", link(health.drive_link)],
    ["drive_thermal_state", number(health.drive_thermal_state, " %")],
    ["records_pending", number(health.records_pending)],
    ["record_degraded", state(health.record_degraded)],
    ["restart_loop", state(health.restart_loop)],
    [
      "preflight_last",
      health.preflight_last === null ? t("unknown") : t(health.preflight_last),
    ],
    ["incidents_open_high", number(health.incidents_open_high)],
    ["rotation_hours", number(health.counters.rotation_hours, " h")],
    ["sessions_total", number(health.counters.sessions_total)],
    ["sessions_failed", number(health.counters.sessions_failed)],
  ];
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-2" role="status">
        <Badge variant="secondary">{t(fresh ? "fresh" : "stale")}</Badge>
        {data.updatedAt !== null && (
          <time
            className="text-xs text-muted-foreground"
            dateTime={new Date(data.updatedAt).toISOString()}
          >
            {new Intl.DateTimeFormat(locale, {
              dateStyle: "short",
              timeStyle: "medium",
            }).format(data.updatedAt)}
          </time>
        )}
      </div>
      {warnings.length > 0 && (
        <ul className="space-y-2 text-sm">
          {warnings.map((warning) => (
            <li
              key={warning}
              className="flex items-start gap-2 text-amber-600 dark:text-amber-400"
            >
              <AlertTriangle className="size-4 shrink-0" aria-hidden="true" />
              {t(warning)}
            </li>
          ))}
        </ul>
      )}
      <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {readings.map(([key, value]) => (
          <div key={key} className="min-w-0">
            <dt className="text-sm text-muted-foreground">{t(key)}</dt>
            <dd className="text-lg font-medium tabular-nums break-words">
              {value}
            </dd>
          </div>
        ))}
        <div className="min-w-0">
          <dt className="text-sm text-muted-foreground">
            {t("drive_faults_by_code")}
          </dt>
          <dd className="text-sm tabular-nums break-words">
            {health.counters.drive_faults_by_code === null
              ? t("unknown")
              : Object.entries(health.counters.drive_faults_by_code)
                  .map(([code, count]) => `${code}: ${count}`)
                  .join(" · ") || t("noFaults")}
          </dd>
        </div>
      </dl>
      <History history={data.history} now={now} />
    </div>
  );
}

export function MachineHealthCard({
  machineId,
}: {
  machineId: Id<"machines">;
}) {
  const t = useTranslations("machineHealth");
  const data = useQuery(api.machineHealth.get, { machineId });
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Activity className="size-4" aria-hidden="true" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent>
        {data === undefined ? (
          <Skeleton className="h-32 w-full motion-reduce:animate-none" />
        ) : data === null ? (
          <p className="text-sm text-muted-foreground">{t("noData")}</p>
        ) : (
          <HealthReadouts data={data} />
        )}
      </CardContent>
    </Card>
  );
}

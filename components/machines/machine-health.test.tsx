import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";
import type { FunctionReturnType } from "convex/server";
import { api } from "@/convex/_generated/api";
import fr from "@/messages/fr.json";
import en from "@/messages/en.json";
import { HealthReadouts, healthWarnings } from "./MachineHealthCard";

type Data = NonNullable<FunctionReturnType<typeof api.machineHealth.get>>;
const now = Date.now();
const health: NonNullable<Data["health"]> = {
  cpu_temp_c: null,
  cpu_load_1m: null,
  mem_used_pct: null,
  disk_free_mb: null,
  uptime_s: null,
  clock_offset_s: null,
  link_rtt_ms: null,
  bitalino_battery_pct: null,
  bitalino_link: null,
  drive_link: null,
  drive_thermal_state: null,
  records_pending: null,
  record_degraded: null,
  restart_loop: null,
  preflight_last: null,
  incidents_open_high: null,
  counters: {
    rotation_hours: null,
    sessions_total: null,
    sessions_failed: null,
    drive_faults_by_code: null,
  },
};
function paint(data: Data, locale: "fr" | "en" = "fr") {
  return renderToStaticMarkup(
    <NextIntlClientProvider
      locale={locale}
      messages={locale === "fr" ? fr : en}
      timeZone="Europe/Paris"
    >
      <HealthReadouts data={data} />
    </NextIntlClientProvider>,
  );
}
describe("machine health", () => {
  it("never turns absent readings into zero or a healthy state", () => {
    const html = paint({ health, updatedAt: now, serverNow: now, history: [] });
    expect(html).toContain("Indisponible");
    expect(html).not.toContain("Aucun défaut observé");
    expect(html).not.toContain(">0<");
    expect(healthWarnings(health)).toEqual([]);
    expect(
      paint({ health: null, updatedAt: null, serverNow: now, history: [] }),
    ).toContain("Aucun rapport");
  });
  it("warns only beyond the documented numeric thresholds", () => {
    expect(
      healthWarnings({
        ...health,
        cpu_temp_c: 70,
        disk_free_mb: 1024,
        clock_offset_s: -5,
        bitalino_battery_pct: 30,
      }),
    ).toEqual([]);
    expect(
      healthWarnings({
        ...health,
        cpu_temp_c: 71,
        disk_free_mb: 1023,
        clock_offset_s: -6,
        bitalino_battery_pct: 29,
      }),
    ).toEqual(["hot", "lowDisk", "clock", "battery"]);
    expect(
      healthWarnings({
        ...health,
        record_degraded: true,
        restart_loop: true,
        drive_link: "down",
        bitalino_link: "down",
      }),
    ).toEqual(["degraded", "restart", "driveDown", "bitalinoDown"]);
  });
  it("labels old values and translates the complete card in English", () => {
    const html = paint(
      {
        health: { ...health, cpu_temp_c: 80 },
        updatedAt: now - 90_000,
        serverNow: now,
        history: [],
      },
      "en",
    );
    expect(html).toContain("Old report");
    expect(html).toContain("CPU temperature above 70");
    expect(html).not.toContain("machineHealth.");
  });
});

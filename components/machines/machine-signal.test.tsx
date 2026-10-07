import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";
import { forgetServerAnswers } from "@/lib/server-clock";
import { LIVE_FRESH_MS } from "@/lib/training";
import { textOf } from "../markup.test-helpers";
import {
  LastSignal,
  MachineStatusBadge,
  MachineStatusText,
  OnlineMachinesCount,
  VersionsSeen,
  type MachineSignal,
} from "./MachineSignal";

/**
 * A machine's status, the "machines online" count and "last signal", as the
 * machine list, a machine's page, a manager's page and the dashboard show
 * them: on the server's clock, again every second, whatever the record still
 * says and whatever the clock of the computer.
 */

const SERVER = 1_800_000_000_000;

const VIEWER_CLOCKS = [
  ["on time", 0],
  ["80 s ahead", 80_000],
  ["10 min ahead", 600_000],
  ["10 min behind", -600_000],
] as const;

const ONLINE = "En ligne";
const OFFLINE = "Hors ligne";
const IN_SESSION = "En session";

let server = SERVER;

function elapse(ms: number) {
  server += ms;
  vi.advanceTimersByTime(ms);
}

function viewerClock(offsetMs: number) {
  vi.setSystemTime(server + offsetMs);
}

/** What an answer said of a machine when the server last computed it. */
function machine(status: string, heardAgoMs: number | null): MachineSignal {
  return {
    status,
    lastHeartbeat: heardAgoMs === null ? 0 : server - heardAgoMs,
    serverNow: server,
  };
}

function paint(node: ReactNode, locale: "fr" | "en" = "fr") {
  const html = renderToStaticMarkup(
    <NextIntlClientProvider
      locale={locale}
      messages={locale === "fr" ? fr : en}
      timeZone="Europe/Paris"
    >
      {node}
    </NextIntlClientProvider>,
  );
  return textOf(html, "");
}

/** The page `ms` after it received the answer it holds: nothing new arrived. */
function paintAfter(ms: number, node: ReactNode) {
  paint(node);
  elapse(ms);
  return paint(node);
}

beforeEach(() => {
  server = SERVER;
  vi.useFakeTimers();
  vi.setSystemTime(SERVER);
  forgetServerAnswers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe.each([
  ["badge", (m: MachineSignal) => <MachineStatusBadge machine={m} />],
  ["plain words", (m: MachineSignal) => <MachineStatusText machine={m} />],
])("machine status (%s)", (_name, show) => {
  it("shows the record's status while the machine sends", () => {
    expect(paint(show(machine("online", 8000)))).toBe(ONLINE);
    expect(paint(show(machine("in_session", 8000)))).toBe(IN_SESSION);
    expect(paint(show(machine("offline", 300_000)))).toBe(OFFLINE);
  });

  it.each(["online", "in_session"])(
    "shows offline 90 s after the last signal, while the record still says %s",
    (status) => {
      const m = machine(status, 0);
      expect(paintAfter(LIVE_FRESH_MS - 1000, show(m))).not.toBe(OFFLINE);
      elapse(1000);
      expect(paint(show(m))).toBe(OFFLINE);
    },
  );

  it("shows offline at once when the server says the last signal is 90 s old", () => {
    expect(paint(show(machine("online", LIVE_FRESH_MS)))).toBe(OFFLINE);
  });

  it("shows a machine that never connected as offline", () => {
    expect(paint(show(machine("offline", null)))).toBe(OFFLINE);
  });

  it("shows a status it does not know as received", () => {
    expect(paint(show(machine("maintenance", 1000)))).toBe("maintenance");
  });

  describe.each(VIEWER_CLOCKS)("on a computer whose clock is %s", (_n, off) => {
    it("shows a machine that sends as online", () => {
      viewerClock(off);
      expect(paint(show(machine("online", 9000)))).toBe(ONLINE);
    });

    it("shows a machine that has stopped sending as offline at 90 s, not later", () => {
      viewerClock(off);
      const m = machine("online", 0);
      expect(paintAfter(LIVE_FRESH_MS - 1000, show(m))).toBe(ONLINE);
      elapse(1000);
      expect(paint(show(m))).toBe(OFFLINE);
    });
  });
});

describe("machine status badge", () => {
  it("shows a deleted machine as deleted", () => {
    expect(
      paint(<MachineStatusBadge machine={machine("online", 0)} isDeleted />),
    ).toBe("Supprimée");
  });

  it("says so in English too", () => {
    const m = machine("online", LIVE_FRESH_MS);
    expect(paint(<MachineStatusBadge machine={m} />, "en")).toBe("Offline");
  });
});

describe("machines online count", () => {
  const fleet = () => [
    machine("online", 2000),
    machine("online", 60_000),
    machine("in_session", 2000),
    machine("offline", 400_000),
  ];

  it("counts the machines shown online: one in a session is not", () => {
    expect(paint(<OnlineMachinesCount machines={fleet()} />)).toBe("2");
  });

  it("counts none while the list loads", () => {
    expect(paint(<OnlineMachinesCount machines={[]} />)).toBe("0");
  });

  it("stops counting each machine 90 s after its last signal, while its record still says online", () => {
    const machines = fleet();
    const count = <OnlineMachinesCount machines={machines} />;
    expect(paintAfter(29_000, count)).toBe("2");
    elapse(1000);
    expect(paint(count)).toBe("1");
    elapse(58_000);
    expect(paint(count)).toBe("0");
  });

  it.each(VIEWER_CLOCKS)(
    "counts the same on a computer whose clock is %s",
    (_name, off) => {
      viewerClock(off);
      const machines = fleet();
      const count = <OnlineMachinesCount machines={machines} />;
      expect(paintAfter(29_000, count)).toBe("2");
      elapse(1000);
      expect(paint(count)).toBe("1");
    },
  );
});

describe("last signal", () => {
  it("keeps counting while the machine is silent", () => {
    const m = machine("online", 8000);
    const cell = <LastSignal machine={m} />;
    expect(paint(cell)).toBe("il y a moins d’une minute");
    elapse(180_000);
    expect(paint(cell)).toBe("il y a 3 minutes");
    elapse(3 * 3_600_000);
    expect(paint(cell)).toBe("il y a environ 3 heures");
  });

  it("shows a dash for a machine that never connected", () => {
    expect(paint(<LastSignal machine={machine("offline", null)} />)).toBe("-");
  });

  it.each(VIEWER_CLOCKS)(
    "counts on the server's clock on a computer whose clock is %s",
    (_name, off) => {
      viewerClock(off);
      const cell = <LastSignal machine={machine("online", 8000)} />;
      expect(paint(cell)).toBe("il y a moins d’une minute");
      elapse(180_000);
      expect(paint(cell)).toBe("il y a 3 minutes");
    },
  );

  it("counts in English too", () => {
    const cell = <LastSignal machine={machine("online", 180_000)} />;
    expect(paint(cell, "en")).toBe("3 minutes ago");
  });
});

describe("versions seen", () => {
  it("keeps counting while the machine is silent", () => {
    const line = <VersionsSeen at={server - 8000} serverNow={server} />;
    const first = paint(line);
    expect(first).toContain("il y a moins d’une minute");
    elapse(180_000);
    expect(paint(line)).toContain("il y a 3 minutes");
  });
});

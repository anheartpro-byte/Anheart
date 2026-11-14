// @vitest-environment jsdom
import { act, screen, within } from "@testing-library/react";
import { format } from "date-fns";
import { enUS, fr as frLocale } from "date-fns/locale";
import { ConvexError } from "convex/values";
import type { FunctionReturnType } from "convex/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import fr from "@/messages/fr.json";
import { mutation as convexCall } from "@/test-support/convex";
import {
  answer,
  feedbackShown,
  lastArgsAsked,
  mutation,
  mutationsSent,
  NOW,
  renderPage,
  takeConsoleErrors,
} from "@/test-support/pages";
import { MachinePairingCard } from "./MachinePairingCard";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);

/**
 * The pairing card of a machine (ANH-115): the Pi its registry names, the
 * open code (who issued it, until when, never the code), a new code shown
 * once in its window with the time it has left, and the revocation. The
 * server decides who may do what (convex/machinePairing.ts).
 */

const machineId = "machine-1" as Id<"machines">;
const t = fr.machinePairing;
const ISSUE = "pairingCodes:issuePairingCode";
const PI = "10000000c0ffee42";
const MINUTES_30 = 30 * 60_000;
// Made up, and of the form of a code, not of a key.
const CODE = "7KQ2-M4XP";

type Registry = NonNullable<
  FunctionReturnType<typeof api.machineRegistry.getRegistry>
>;

afterEach(() => {
  vi.useRealTimers();
});

const when = (time: number) => format(time, "PPp", { locale: frLocale });

async function card({
  serialPi = PI,
  open = null,
  locale = "fr",
}: {
  serialPi?: string | null;
  open?: FunctionReturnType<typeof api.machinePairing.pairingState>;
  locale?: "fr" | "en";
} = {}) {
  // The card reads the registry's serial number only; null: none.
  answer(api.machineRegistry.getRegistry, {
    serialPi: serialPi ?? undefined,
  } as unknown as Registry);
  answer(api.machinePairing.pairingState, open);
  return renderPage(<MachinePairingCard machineId={machineId} />, { locale });
}

function issued() {
  convexCall(ISSUE).mockResolvedValue({
    code: CODE,
    expiresAt: NOW + MINUTES_30,
    serverNow: NOW,
  });
}

describe("machine pairing: what the card shows", () => {
  it("asks for the machine's code and registry, and shows a placeholder while they load", async () => {
    answer(api.machineRegistry.getRegistry, undefined);
    answer(api.machinePairing.pairingState, undefined);
    await renderPage(<MachinePairingCard machineId={machineId} />);

    expect(lastArgsAsked(api.machinePairing.pairingState)).toEqual({
      machineId,
    });
    expect(lastArgsAsked(api.machineRegistry.getRegistry)).toEqual({
      machineId,
    });
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("names the Pi of the registry and says no code is open", async () => {
    await card();

    expect(screen.getByText(PI)).toBeTruthy();
    expect(screen.getByText(t.none)).toBeTruthy();
    expect(
      screen.getByRole("button", { name: t.issue }).hasAttribute("disabled"),
    ).toBe(false);
    expect(screen.queryByRole("button", { name: t.revoke })).toBeNull();
    expect(screen.getByText(t.revokesKeys)).toBeTruthy();
  });

  it.each([
    ["no serial number", null],
    ["one a console cannot send", "not a serial"],
  ])("offers no code for a registry with %s", async (_case, serialPi) => {
    await card({ serialPi });

    const reason = screen.getByText(t.noSerial);
    const issue = screen.getByRole("button", { name: t.issue });
    expect(issue.hasAttribute("disabled")).toBe(true);
    // The reason it is greyed is the button's description.
    expect(reason.id).not.toBe("");
    expect(issue.getAttribute("aria-describedby")).toBe(reason.id);
  });

  it("says who issued the open code and until when, never the code, and offers to revoke it", async () => {
    await card({
      open: {
        createdAt: NOW - 60_000,
        expiresAt: NOW + MINUTES_30,
        createdByName: "Alice Admin",
        serverNow: NOW,
      },
    });

    expect(
      screen.getByText(
        `Code en cours, émis le ${when(NOW - 60_000)} par Alice Admin, valable jusqu'au ${when(NOW + MINUTES_30)}.`,
      ),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: t.reissue })).toBeTruthy();
    expect(screen.getByRole("button", { name: t.revoke })).toBeTruthy();
  });

  it("says an open code of an author who is gone, without a name", async () => {
    await card({
      open: {
        createdAt: NOW - 60_000,
        expiresAt: NOW + MINUTES_30,
        createdByName: null,
        serverNow: NOW,
      },
    });

    expect(
      screen.getByText(
        `Code en cours, émis le ${when(NOW - 60_000)}, valable jusqu'au ${when(NOW + MINUTES_30)}.`,
      ),
    ).toBeTruthy();
  });

  it("says no open code once it has expired on the server's clock", async () => {
    await card({
      open: {
        createdAt: NOW - MINUTES_30,
        expiresAt: NOW,
        createdByName: "Alice Admin",
        serverNow: NOW,
      },
    });

    expect(screen.getByText(t.none)).toBeTruthy();
    expect(screen.queryByRole("button", { name: t.revoke })).toBeNull();
  });
});

describe("machine pairing: a new code, shown once", () => {
  it("issues it for this machine with no message, and shows it large with the time it has left", async () => {
    issued();
    const { user } = await card();

    await user.click(screen.getByRole("button", { name: t.issue }));

    expect(mutationsSent()).toEqual([{ name: ISSUE, args: { machineId } }]);
    expect(feedbackShown()).toEqual([]);
    const shown = within(screen.getByRole("dialog"));
    expect(shown.getByText(t.codeTitle)).toBeTruthy();
    expect(shown.getByText(CODE).getAttribute("aria-hidden")).toBe("true");
    expect(
      shown.getByText(
        `Code d'appairage, caractère par caractère : ${[...CODE].join(" ")}`,
      ),
    ).toBeTruthy();
    expect(shown.getByRole("timer").textContent).toBe("Expire dans 30:00");
    expect(shown.getByText(t.codeWarning)).toBeTruthy();
    // Typed on the console: nothing to copy it with, and not in the address.
    expect(shown.queryByRole("button", { name: fr.common.copy })).toBeNull();
    expect(window.location.href).not.toContain(CODE);
  });

  it("counts down on the server's clock, then shows the code no more", async () => {
    // The clock of the page, faked before it is read; user events keep theirs.
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    issued();
    const { user } = await card();
    await user.click(screen.getByRole("button", { name: t.issue }));
    // The status region is there, empty, before the code expires.
    expect(screen.getByRole("status").textContent).toBe("");

    await act(async () => {
      vi.advanceTimersByTime(61_000);
    });
    expect(screen.getByRole("timer").textContent).toBe("Expire dans 28:59");
    await act(async () => {
      vi.advanceTimersByTime(MINUTES_30);
    });

    expect(screen.queryByText(CODE)).toBeNull();
    expect(screen.queryByRole("timer")).toBeNull();
    expect(screen.getByRole("status").textContent).toBe(t.expired);
  });

  it("forgets the code once its window is closed", async () => {
    issued();
    const { user } = await card();
    await user.click(screen.getByRole("button", { name: t.issue }));

    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: t.codeDone,
      }),
    );

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText(CODE)).toBeNull();
  });

  it("forgets the code when its window is dismissed", async () => {
    issued();
    const { user } = await card();
    await user.click(screen.getByRole("button", { name: t.issue }));

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText(CODE)).toBeNull();
  });

  it("writes the dates in the language of the page", async () => {
    await card({
      locale: "en",
      open: {
        createdAt: NOW - 60_000,
        expiresAt: NOW + MINUTES_30,
        createdByName: "Alice Admin",
        serverNow: NOW,
      },
    });

    const inEnglish = (time: number) => format(time, "PPp", { locale: enUS });
    expect(
      screen.getByText(
        `Open code, issued on ${inEnglish(NOW - 60_000)} by Alice Admin, valid until ${inEnglish(NOW + MINUTES_30)}.`,
      ),
    ).toBeTruthy();
  });

  it("shows the refusal, and no code, when the server issues none", async () => {
    convexCall(ISSUE).mockRejectedValue(
      new ConvexError("pairing_serial_in_use"),
    );
    const { user } = await card();

    await user.click(screen.getByRole("button", { name: t.issue }));

    expect(feedbackShown()).toEqual([
      { kind: "error", message: fr.errors.pairing_serial_in_use },
    ]);
    expect(takeConsoleErrors()).toEqual([
      expect.stringMatching(`^\\[action\\] ${ISSUE} failed`),
    ]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("machine pairing: revoking the open code", () => {
  it("revokes the machine's open code and says so", async () => {
    mutation(api.machinePairing.revokePairingCode).mockResolvedValue(null);
    const { user } = await card({
      open: {
        createdAt: NOW - 60_000,
        expiresAt: NOW + MINUTES_30,
        createdByName: "Alice Admin",
        serverNow: NOW,
      },
    });

    await user.click(screen.getByRole("button", { name: t.revoke }));

    expect(mutationsSent()).toEqual([
      { name: "machinePairing:revokePairingCode", args: { machineId } },
    ]);
    expect(feedbackShown()).toEqual([
      { kind: "success", message: fr.feedback.pairingCodeRevoked },
    ]);
  });
});

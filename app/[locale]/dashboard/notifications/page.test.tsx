// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";
import {
  answer,
  argsAsked,
  codedFailures,
  feedbackShown,
  mutation,
  mutationsSent,
  NOW,
  renderPage,
  signedInAs,
  takeLoggedFailures,
} from "@/test-support/pages";
import NotificationsPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);

/**
 * ANH-90: the alerts of the machines the visitor manages, and the choice of
 * those also sent by e-mail. The server decides who receives what
 * (convex/notifications.test.ts); the page shows it, links to where the rest
 * is, and sends the visitor's choices.
 */

type Alert = FunctionReturnType<typeof api.notifications.list>[number];

function alert(id: string, overrides: Partial<Alert> = {}): Alert {
  return {
    _id: id as Id<"notifications">,
    type: "drive_fault",
    machineId: "machine-1" as Id<"machines">,
    machineName: "Centri Paris",
    machineDeleted: false,
    sessionId: undefined,
    detail: undefined,
    reason: undefined,
    reasonCode: undefined,
    count: 1,
    createdAt: NOW - 60_000,
    updatedAt: NOW - 60_000,
    read: false,
    ...overrides,
  };
}

const DEFAULTS = {
  machine_offline: false,
  session_failed: true,
  drive_fault: true,
  safety_verdict: true,
};

function serverHas(alerts: Alert[], emailAvailable = true) {
  answer(api.notifications.list, alerts);
  answer(api.notifications.getSettings, {
    emailAvailable,
    email: DEFAULTS,
  });
}

/** The list item of the alert whose kind reads `title`. */
function item(title: string) {
  const items = screen.getAllByRole("listitem");
  const found = items.find((element) =>
    within(element).queryByText(title, { exact: false }),
  );
  if (!found) throw new Error(`No alert ${title}`);
  return within(found);
}

describe("the alerts page", () => {
  describe.each(["fr", "en"] as const)("T04-c: end reasons in %s", (locale) => {
    const messages = locale === "fr" ? fr : en;
    it.each([
      [
        "safety_verdict: hr_drop (RAMP_DOWN)",
        undefined,
        `${messages.sessionReport.endReasons.safety_verdict} : hr_drop (RAMP_DOWN)`,
      ],
      [
        "emergency_stop",
        undefined,
        messages.sessionReport.endReasons.emergency_stop,
      ],
      ["unknown_reason: x", undefined, "unknown_reason: x"],
      [
        "refusee par la machine : console occupee",
        undefined,
        "refusee par la machine : console occupee",
      ],
      [
        "safety_verdict: hr_drop (RAMP_DOWN)",
        "expired_before_start",
        messages.errors.expired_before_start,
      ],
      [
        "emergency_stop",
        "unknown_code",
        messages.sessionReport.endReasons.emergency_stop,
      ],
      ["constructor: x", undefined, "constructor: x"],
      ["toString: x", undefined, "toString: x"],
      ["emergency_stop_extra: x", undefined, "emergency_stop_extra: x"],
      ["emergency_stop extra", undefined, "emergency_stop extra"],
      [
        "emergency_stop",
        "constructor",
        messages.sessionReport.endReasons.emergency_stop,
      ],
    ])(
      "renders %s with reasonCode %s",
      async (reason, reasonCode, expected) => {
        signedInAs("gestionnaire");
        serverHas([
          alert("end-reason", { type: "session_failed", reason, reasonCode }),
        ]);

        await renderPage(<NotificationsPage />, { locale });

        expect(
          screen.getByText(
            messages.notifications.reason.replace("{reason}", expected),
          ),
        ).toBeTruthy();
      },
    );
  });

  it.each(["fr", "en"] as const)(
    "shows a deleted-machine notice in %s while keeping the readable session link",
    async (locale) => {
      signedInAs("gestionnaire");
      serverHas([
        alert("a1", { machineDeleted: true }),
        alert("a2", {
          machineDeleted: true,
          sessionId: "session-1" as Id<"sessions">,
        }),
      ]);

      await renderPage(<NotificationsPage />, { locale });

      const messages = locale === "fr" ? fr.notifications : en.notifications;
      expect(screen.getAllByText(messages.machineDeleted)).toHaveLength(2);
      expect(
        screen.queryByRole("link", { name: messages.openMachine }),
      ).toBeNull();
      expect(
        screen
          .getByRole("link", { name: messages.openSession })
          .getAttribute("href"),
      ).toBe("/dashboard/sessions/session-1");
      expect(screen.queryByText(messages.empty)).toBeNull();
      expect(
        screen.queryByText(messages.listCut.replace("{count}", "50")),
      ).toBeNull();
    },
  );

  it("shows a single visible alert without an empty or truncated-list message", async () => {
    signedInAs("gestionnaire");
    serverHas([alert("visible")]);

    await renderPage(<NotificationsPage />);

    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.queryByText(fr.notifications.empty)).toBeNull();
    expect(
      screen.queryByText(fr.notifications.listCut.replace("{count}", "50")),
    ).toBeNull();
  });

  it("is refused to a patient, who is never asked for alerts", async () => {
    signedInAs("user");

    await renderPage(<NotificationsPage />);

    expect(screen.getByText(fr.notifications.accessDenied)).toBeTruthy();
    expect(argsAsked(api.notifications.list)).toEqual(["skip"]);
    expect(argsAsked(api.notifications.getSettings)).toEqual(["skip"]);
  });

  it("shows nothing but a skeleton until the visitor and the alerts are known", async () => {
    answer(api.users.getCurrentUser, undefined);
    const view = await renderPage(<NotificationsPage />);
    expect(screen.queryByRole("heading")).toBeNull();

    signedInAs("gestionnaire");
    answer(api.notifications.list, undefined);
    await view.refresh();
    expect(screen.queryByRole("heading")).toBeNull();

    serverHas([]);
    await view.refresh();
    expect(
      screen.getByRole("heading", { name: fr.notifications.title }),
    ).toBeTruthy();
    expect(screen.getByText(fr.notifications.empty)).toBeTruthy();
    // Nothing unread: nothing to mark.
    expect(
      screen.queryByRole("button", { name: fr.notifications.markAllRead }),
    ).toBeNull();
  });

  it("says what happened, on which machine, and links to the session or the machine", async () => {
    signedInAs("gestionnaire");
    serverHas([
      alert("a1", {
        type: "session_failed",
        sessionId: "session-1" as Id<"sessions">,
        reason: "arrêt d'urgence",
      }),
      alert("a2", { type: "safety_verdict", detail: "QUICK_STOP", count: 3, updatedAt: NOW }),
      alert("a3", { type: "drive_fault", detail: "OHF", read: true }),
      alert("a4", { type: "machine_offline", machineName: "Banc 2" }),
      alert("a5", { type: "safety_verdict", detail: "SOMETHING_NEW" }),
    ]);

    await renderPage(<NotificationsPage />);

    const failed = item(fr.notifications.types.session_failed);
    expect(
      failed.getByText(
        fr.notifications.reason.replace("{reason}", "arrêt d'urgence"),
      ),
    ).toBeTruthy();
    expect(
      failed.getByRole("link", { name: fr.notifications.openSession }).getAttribute("href"),
    ).toBe("/dashboard/sessions/session-1");
    expect(failed.getByText(fr.notifications.unread)).toBeTruthy();

    const verdicts = screen
      .getAllByRole("listitem")
      .filter((element) => element.getAttribute("data-alert") === "safety_verdict");
    expect(within(verdicts[0]).getByText(fr.notifications.verdict.QUICK_STOP)).toBeTruthy();
    expect(within(verdicts[0]).getByText(/3 fois, dernière à/)).toBeTruthy();
    expect(within(verdicts[1]).getByText(fr.notifications.verdict.unknown)).toBeTruthy();

    const fault = item(fr.notifications.types.drive_fault);
    expect(
      fault.getByText(fr.notifications.faultCode.replace("{code}", "OHF")),
    ).toBeTruthy();
    // Read already: neither marked unread nor offered to be marked read.
    expect(fault.queryByText(fr.notifications.unread)).toBeNull();
    expect(
      fault.queryByRole("button", { name: fr.notifications.markRead }),
    ).toBeNull();
    expect(
      fault.getByRole("link", { name: fr.notifications.openMachine }).getAttribute("href"),
    ).toBe("/dashboard/machines/machine-1");

    expect(item(fr.notifications.types.machine_offline).getByText(/Banc 2/)).toBeTruthy();
  });

  it("words the code of a launch refused before its machine armed it, not its sentence", async () => {
    signedInAs("gestionnaire");
    serverHas([
      alert("a1", {
        type: "session_failed",
        sessionId: "session-1" as Id<"sessions">,
        reason: "expired before the machine armed it",
        reasonCode: "expired_before_start",
      }),
      alert("a2", {
        type: "session_failed",
        reason: "refusee par la machine : capsule ouverte",
        reasonCode: "a_code_this_site_does_not_know",
      }),
    ]);

    await renderPage(<NotificationsPage />);

    const [coded, unknown] = screen.getAllByRole("listitem");
    expect(within(coded).getByText(
      fr.notifications.reason.replace("{reason}", fr.errors.expired_before_start),
    )).toBeTruthy();
    expect(within(coded).queryByText(/expired before/)).toBeNull();
    expect(within(unknown).getByText(
      fr.notifications.reason.replace(
        "{reason}",
        "refusee par la machine : capsule ouverte",
      ),
    )).toBeTruthy();
  });

  it("marks one alert, or all of them, as read", async () => {
    signedInAs("org_admin");
    serverHas([alert("a1"), alert("a2", { type: "machine_offline" })]);
    mutation(api.notifications.markAllRead).mockResolvedValue({ marked: 2 });
    const view = await renderPage(<NotificationsPage />);

    await view.user.click(
      item(fr.notifications.types.machine_offline).getByRole("button", {
        name: fr.notifications.markRead,
      }),
    );
    await view.user.click(
      screen.getByRole("button", { name: fr.notifications.markAllRead }),
    );

    expect(mutationsSent()).toEqual([
      { name: "notifications:markRead", args: { notificationId: "a2" } },
      { name: "notifications:markAllRead", args: {} },
    ]);
    expect(feedbackShown()).toEqual([
      { kind: "success", message: fr.notifications.allRead },
    ]);
  });

  it.each(codedFailures(api.notifications.markRead, "notification_not_found"))(
    "says why an alert could not be marked as read, $where",
    async ({ error, shown }) => {
      signedInAs("gestionnaire");
      serverHas([alert("a1")]);
      mutation(api.notifications.markRead).mockRejectedValue(error);
      const view = await renderPage(<NotificationsPage />);

      await view.user.click(
        screen.getByRole("button", { name: fr.notifications.markRead }),
      );

      expect(feedbackShown()).toEqual([{ kind: "error", message: shown }]);
      expect(takeLoggedFailures()).toEqual(["notifications:markRead"]);
    },
  );

  it("says how many alerts are shown when the server cut the list", async () => {
    signedInAs("gestionnaire");
    serverHas(
      Array.from({ length: 50 }, (_, index) =>
        alert(`a${index}`, { read: true }),
      ),
    );

    await renderPage(<NotificationsPage />);

    expect(
      screen.getByText(fr.notifications.listCut.replace("{count}", "50")),
    ).toBeTruthy();
  });
});

describe("the choice of alerts sent by e-mail", () => {
  it("shows each kind with its choice, and sends a change", async () => {
    signedInAs("gestionnaire");
    serverHas([]);
    const view = await renderPage(<NotificationsPage />);

    const offline = screen.getByRole("switch", {
      name: fr.notifications.settings.email.replace(
        "{type}",
        fr.notifications.types.machine_offline,
      ),
    });
    expect(offline.getAttribute("aria-checked")).toBe("false");
    expect(
      screen
        .getByRole("switch", {
          name: fr.notifications.settings.email.replace(
            "{type}",
            fr.notifications.types.drive_fault,
          ),
        })
        .getAttribute("aria-checked"),
    ).toBe("true");
    expect(screen.queryByText(fr.notifications.settings.unavailable)).toBeNull();

    await view.user.click(offline);

    expect(mutationsSent()).toEqual([
      {
        name: "notifications:updateSettings",
        args: { type: "machine_offline", email: true },
      },
    ]);
    expect(feedbackShown()).toEqual([
      { kind: "success", message: fr.notifications.settings.saved },
    ]);
  });

  it("says when the deployment sends no e-mail at all", async () => {
    signedInAs("gestionnaire");
    serverHas([], false);

    await renderPage(<NotificationsPage />, { locale: "en" });

    expect(screen.getByRole("status").textContent).toBe(
      en.notifications.settings.unavailable,
    );
    expect(screen.getAllByRole("switch")).toHaveLength(4);
  });
});

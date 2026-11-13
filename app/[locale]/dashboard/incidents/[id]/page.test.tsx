// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import fr from "@/messages/fr.json";
import {
  answer,
  argsAsked,
  mutationsSent,
  NOW,
  propsOf,
  renderPage,
  routeParams,
  signedInAs,
  wasMounted,
} from "@/test-support/pages";
import IncidentPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);
vi.mock("@/components/training/SessionRecordCard", async () => ({
  SessionRecordCard: (await import("@/test-support/pages")).standIn(
    "SessionRecordCard",
  ),
}));

/**
 * ANH-145 EX-5: an incident's sheet, its session, the events around its
 * instant, the record, and the form of its analysis and its closing.
 */

const t = fr.incidents;
type Sheet = NonNullable<FunctionReturnType<typeof api.incidents.get>>;

function sheet(overrides: Partial<Sheet> = {}, incident: Partial<Sheet["incident"]> = {}): Sheet {
  return {
    incident: {
      _id: "incident-1" as Id<"incidents">,
      _creationTime: NOW - 60_000,
      organizationId: "org-centre-a" as Id<"organizations">,
      machineId: "machine-1" as Id<"machines">,
      machineName: "Centri Paris",
      sessionId: "session-1" as Id<"sessions">,
      kind: "safety_verdict",
      rule: "hr_critical",
      t: NOW - 61_000,
      severity: "high",
      occupied: true,
      status: "open",
      openedAt: NOW - 60_000,
      ...incident,
    },
    analyzedByName: null,
    closedByName: null,
    session: {
      _id: "session-1" as Id<"sessions">,
      status: "failed",
      startedAt: NOW - 600_000,
      endedAt: NOW - 55_000,
      endReason: "safety_verdict",
      record: "complete",
    },
    events: [
      { t: NOW - 62_500, kind: "phase", detail: "hold" },
      { t: NOW - 61_000, kind: "verdict", detail: "hr_critical (QUICK_STOP): 190" },
    ],
    canClose: false,
    ...overrides,
  };
}

const page = () => (
  <IncidentPage params={routeParams({ id: "incident-1" })} />
);

describe("an incident's sheet", () => {
  it("says when the incident is not found", async () => {
    signedInAs("gestionnaire");
    answer(api.incidents.get, null);
    await renderPage(page());
    expect(screen.getByText(t.notFound)).toBeTruthy();
    expect(argsAsked(api.incidents.get)).toContainEqual({
      incidentId: "incident-1",
    });
  });

  it("shows why programmed sessions are closed, the session, the events and the record", async () => {
    signedInAs("gestionnaire");
    answer(api.incidents.get, sheet());
    await renderPage(page());
    expect(screen.getByRole("heading", { name: t.kinds.safety_verdict })).toBeTruthy();
    expect(screen.getByRole("status").textContent).toContain(t.blocked);
    expect(
      screen.getByRole("link", { name: t.openSession }).getAttribute("href"),
    ).toBe("/dashboard/sessions/session-1");
    const events = screen.getByRole("list", { name: t.events });
    expect(events.textContent).toContain("-1.5 s · phase · hold");
    expect(events.textContent).toContain("0.0 s · verdict · hr_critical (QUICK_STOP): 190");
    expect(propsOf<{ sessionId: string }>("SessionRecordCard").sessionId).toBe(
      "session-1",
    );
    // A gestionnaire analyses; only the Anheart admin closes.
    expect(screen.getByText(t.closeAdminOnly)).toBeTruthy();
    expect(screen.queryByRole("button", { name: t.close })).toBeNull();
  });

  it("sends the analysis the staff writes", async () => {
    signedInAs("gestionnaire");
    answer(api.incidents.get, sheet());
    const view = await renderPage(page());
    await view.user.type(screen.getByLabelText(t.analysis), "electrodes decollees");
    await view.user.type(screen.getByLabelText(t.action), "electrodes changees");
    await view.user.click(screen.getByRole("button", { name: t.saveAnalysis }));
    expect(mutationsSent()).toEqual([
      {
        name: "incidents:analyze",
        args: {
          incidentId: "incident-1",
          analysis: "electrodes decollees",
          action: "electrodes changees",
        },
      },
    ]);
  });

  it("lets the Anheart admin close it with its cause and its action", async () => {
    signedInAs("admin");
    answer(
      api.incidents.get,
      sheet(
        { canClose: true, analyzedByName: "Gaston Gestion" },
        { status: "analyzed", analysis: "cause", action: "action" },
      ),
    );
    const view = await renderPage(page());
    expect(
      screen.getByText(t.analyzedBy.replace("{name}", "Gaston Gestion")),
    ).toBeTruthy();
    await view.user.click(screen.getByRole("button", { name: t.close }));
    expect(mutationsSent()).toEqual([
      {
        name: "incidents:close",
        args: { incidentId: "incident-1", analysis: "cause", action: "action" },
      },
    ]);
  });

  it("shows a closed incident read only, without a session it cannot read", async () => {
    signedInAs("org_admin");
    answer(
      api.incidents.get,
      sheet(
        { session: null, events: [], closedByName: "Alice Admin" },
        { status: "closed", closedAt: NOW, severity: "low", occupied: false },
      ),
    );
    await renderPage(page());
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByText(t.noSession)).toBeTruthy();
    expect(screen.getByText(t.noEvents)).toBeTruthy();
    expect(screen.getByText(t.closedBy.split(" {name}")[0], { exact: false })).toBeTruthy();
    expect(wasMounted("SessionRecordCard")).toBe(false);
    expect(screen.queryByRole("button", { name: t.saveAnalysis })).toBeNull();
    expect((screen.getByLabelText(t.analysis) as HTMLTextAreaElement).disabled).toBe(true);
  });
});

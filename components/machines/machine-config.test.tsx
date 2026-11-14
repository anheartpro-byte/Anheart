// @vitest-environment jsdom
/**
 * ANH-141 EX-3: the machine's configuration card. A form generated from
 * contracts/machine-config.json, every problem the console would raise shown
 * at once in its words, the diff with the revision applied, Propose, and
 * Approve held for the author when a safety key changes. The server decides;
 * the card follows what it says.
 */
import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import type { ConfigValues } from "@/convex/lib/machineConfig";
import fr from "@/messages/fr.json";
import en from "@/messages/en.json";
import cases from "@/contracts/machine-config-cases.json";
import {
  answer,
  feedbackShown,
  lastArgsAsked,
  mutation,
  NOW,
  renderPage,
} from "@/test-support/pages";
import { MachineConfigCard, type MachineConfigView } from "./MachineConfigCard";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
afterEach(() => {
  vi.unstubAllGlobals();
});

const machineId = "TEST-machine" as Id<"machines">;
const authorId = "TEST-author" as Id<"users">;
const otherId = "TEST-other" as Id<"users">;
const t = fr.machineConfig;
const BASE = cases.base as ConfigValues;

function revision(
  number: number,
  status: MachineConfigView["revisions"][number]["status"],
  overrides: Partial<MachineConfigView["revisions"][number]> = {},
): MachineConfigView["revisions"][number] {
  return {
    _id: `TEST-config-${number}` as Id<"machine_configs">,
    _creationTime: NOW,
    machineId,
    revision: number,
    values: BASE,
    canonical: "{}",
    hash: "a".repeat(64),
    status,
    changedKeys: [],
    proposedBy: { userId: authorId, firstName: "TEST", lastName: "Auteur" },
    proposedByRole: "admin",
    proposedAt: NOW,
    changedFromApplied: [],
    safetyChange: false,
    approveBlocked: status === "draft" ? null : "machine_config_wrong_status",
    ...overrides,
  } as MachineConfigView["revisions"][number];
}

function view(overrides: Partial<MachineConfigView> = {}): MachineConfigView {
  return {
    machineId,
    revisions: [],
    appliedRevision: null,
    inForceRevision: null,
    reportedHash: null,
    hashMatches: null,
    configurationStatus: "unavailable",
    defaults: {
      ...BASE,
      MOTOR_BACKEND: null,
      ECG_SOURCE: null,
      ARM_RADIUS_M: null,
    },
    validationLevel: "none",
    locksAboveLevel: [],
    canPropose: "all",
    ...overrides,
  };
}

async function card(
  value: MachineConfigView | null | undefined,
  role = "admin",
  locale: "fr" | "en" = "fr",
) {
  answer(api.machineConfigs.getMachineConfig, value);
  return renderPage(
    <MachineConfigCard machineId={machineId} role={role} userId={authorId} />,
    { locale },
  );
}

const form = () => within(screen.getByRole("form", { name: t.form }));
const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const field = (key: string) =>
  form().getByLabelText(new RegExp(`^${escape(t.keys[key as keyof typeof t.keys])} ${key}`));
const group = (key: string) => form().getByRole("group", { name: key });
const problems = () =>
  within(screen.getByRole("list", { name: t.problemList }))
    .getAllByRole("listitem")
    .map((item) => item.textContent);

describe("ANH-141 the configuration card", () => {
  it("asks only for an admin, shows loading, and draws nothing for null", async () => {
    await card(view(), "gestionnaire");
    expect(lastArgsAsked(api.machineConfigs.getMachineConfig)).toBe("skip");
    expect(screen.queryByRole("region")).toBeNull();
    const loading = await card(undefined);
    expect(screen.getByLabelText(t.loading)).toBeTruthy();
    expect(lastArgsAsked(api.machineConfigs.getMachineConfig)).toEqual({ machineId });
    answer(api.machineConfigs.getMachineConfig, null);
    await loading.refresh();
    expect(screen.queryByLabelText(t.loading)).toBeNull();
    expect(screen.queryByRole("heading", { name: t.title })).toBeNull();
  });

  it("shows every problem at once, in the console's words, and holds Propose", async () => {
    await card(view());
    expect(screen.getByText(t.status.unavailable)).toBeTruthy();
    expect(problems()).toEqual([
      "ECG_SOURCE : obligatoire",
      "MOTOR_BACKEND : obligatoire",
      "ARM_RADIUS_M : obligatoire",
    ]);
    fireEvent.change(field("MOTOR_MAX_RPM"), { target: { value: "2000" } });
    fireEvent.change(field("ARM_RADIUS_M"), { target: { value: "0" } });
    expect(problems()).toContain("ARM_RADIUS_M : 0.0 m hors de ]0, 5.0]");
    expect(problems()).toContain(
      "MOTOR_MAX_RPM : 2000 tr/min hors de 0..1380 (plaque signaletique)",
    );
    expect(field("MOTOR_MAX_RPM").getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByRole("button", { name: t.propose })).toHaveProperty("disabled", true);
    expect(screen.getByText(t.diffNone)).toBeTruthy();
  });

  it("proposes the values the console accepts, and says the diff with the applied revision", async () => {
    const applied = revision(1, "applied");
    await card(
      view({
        revisions: [applied],
        appliedRevision: 1,
        inForceRevision: 1,
        configurationStatus: "applied",
        hashMatches: true,
      }),
    );
    expect(screen.getByRole("button", { name: t.propose })).toHaveProperty("disabled", true);
    expect(screen.getByText(t.unchanged)).toBeTruthy();
    fireEvent.change(field("location"), { target: { value: "Atelier 2" } });
    fireEvent.change(field("MOTOR_MAX_RPM"), { target: { value: "400" } });
    fireEvent.click(within(group("PROGRAMS_ENABLED")).getByRole("checkbox"));
    const diff = within(screen.getByRole("list", { name: t.diff }));
    expect(diff.getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "location : Atelier Anheart → Atelier 2",
      `MOTOR_MAX_RPM : 300 → 400${t.safety}`,
      `PROGRAMS_ENABLED : false → true${t.safety}`,
    ]);
    // Above the machine's level: the lock is refused before anything is sent.
    expect(problems()).toEqual(["PROGRAMS_ENABLED : machine validée jusqu'au niveau none"]);
    fireEvent.click(within(group("PROGRAMS_ENABLED")).getByRole("checkbox"));
    const propose = mutation(api.machineConfigs.proposeConfig).mockResolvedValue({
      configId: "TEST-config-2",
      revision: 2,
    });
    fireEvent.click(screen.getByRole("button", { name: t.propose }));
    await vi.waitFor(() => expect(propose).toHaveBeenCalled());
    expect(propose.mock.calls[0][0]).toEqual({
      machineId,
      values: { ...BASE, location: "Atelier 2", MOTOR_MAX_RPM: 400 },
    });
    await vi.waitFor(() =>
      expect(feedbackShown()).toContainEqual({
        kind: "success",
        message: t.proposed.replace("{revision}", "2"),
      }),
    );
  });

  it("chooses a value of a list and the sensors among the contract's", async () => {
    const view_ = await card(view());
    await view_.user.click(field("MOTOR_BACKEND"));
    await view_.user.click(screen.getByRole("option", { name: "serial" }));
    const sensors = within(group("SENSORS"));
    fireEvent.click(sensors.getByRole("checkbox", { name: "EDA" }));
    fireEvent.click(sensors.getByRole("checkbox", { name: "ECG" }));
    expect(problems()).toContain(
      "SENSORS : ECG obligatoire : la frequence cardiaque pilote le moteur",
    );
    expect(problems()).not.toContain("MOTOR_BACKEND : obligatoire");
  });

  it("says under the form a problem of the drive link, which no single field owns", async () => {
    await card(view({ defaults: { ...BASE, MOTOR_BACKEND: "serial", MOTOR_SLAVE_ID: 0 } }));
    const link = /^MOTOR_\* : Modbus slave address 0 is neither in 1\.\.247/;
    expect(problems().some((line) => link.test(line ?? ""))).toBe(true);
    expect(screen.getByText(/^MOTOR_\* : Modbus slave address 0/, { selector: "p" })).toBeTruthy();
  });

  it("lets an organisation admin edit the name and the place only", async () => {
    await card(
      view({
        revisions: [revision(1, "applied")],
        appliedRevision: 1,
        inForceRevision: 1,
        canPropose: "name_location",
      }),
      "org_admin",
    );
    expect(field("name")).toHaveProperty("disabled", false);
    expect(field("location")).toHaveProperty("disabled", false);
    expect(field("MOTOR_MAX_RPM")).toHaveProperty("disabled", true);
    expect(field("MOTOR_BACKEND")).toHaveProperty("disabled", true);
  });

  it("holds Approve for the author of a safety change, and says why", async () => {
    await card(
      view({
        revisions: [
          revision(2, "draft", {
            safetyChange: true,
            changedFromApplied: ["MOTOR_MAX_RPM"],
            approveBlocked: "machine_config_self_approval",
          }),
          revision(1, "applied", {
            approvedBy: [{ userId: otherId }],
            approvedAt: NOW,
            appliedAt: NOW,
          }),
        ],
        appliedRevision: 1,
        inForceRevision: 1,
        configurationStatus: "mismatch",
        hashMatches: false,
      }),
    );
    expect(screen.getByText(t.mismatch)).toBeTruthy();
    const draft = within(screen.getByRole("listitem", { name: "Révision 2" }));
    expect(draft.getByRole("button", { name: t.approve })).toHaveProperty("disabled", true);
    expect(draft.getByText(t.blocked.machine_config_self_approval)).toBeTruthy();
    expect(draft.getByText(t.safetyChange)).toBeTruthy();
    expect(draft.getByText(/MOTOR_MAX_RPM/)).toBeTruthy();
    const applied = within(screen.getByRole("listitem", { name: "Révision 1" }));
    expect(applied.queryByRole("button", { name: t.approve })).toBeNull();
    expect(applied.getByText(/TEST-other/)).toBeTruthy();
  });

  it("approves a draft the caller may approve, and rejects one with a reason", async () => {
    await card(
      view({
        revisions: [revision(2, "draft"), revision(1, "rejected", { rejectReason: "erreur" })],
      }),
    );
    const draft = within(screen.getByRole("listitem", { name: "Révision 2" }));
    const approve = mutation(api.machineConfigs.approveConfig).mockResolvedValue(null);
    fireEvent.click(draft.getByRole("button", { name: t.approve }));
    await vi.waitFor(() =>
      expect(approve).toHaveBeenCalledWith({ configId: "TEST-config-2" }),
    );
    const reject = mutation(api.machineConfigs.rejectConfig).mockResolvedValue(null);
    const button = draft.getByRole("button", { name: t.reject });
    expect(button).toHaveProperty("disabled", true);
    fireEvent.change(draft.getByLabelText(t.reason), { target: { value: "trop tôt" } });
    fireEvent.click(button);
    await vi.waitFor(() =>
      expect(reject).toHaveBeenCalledWith({ configId: "TEST-config-2", reason: "trop tôt" }),
    );
    expect(screen.getByText(t.rejectReason.replace("{reason}", "erreur"))).toBeTruthy();
  });

  it("lets an organisation admin reject only their own draft", async () => {
    await card(
      view({
        revisions: [
          revision(3, "draft", { proposedBy: { userId: authorId } }),
          revision(2, "approved", { proposedBy: { userId: otherId } }),
        ],
        canPropose: "name_location",
      }),
      "org_admin",
    );
    expect(
      within(screen.getByRole("listitem", { name: "Révision 3" })).getByRole("button", {
        name: t.reject,
      }),
    ).toBeTruthy();
    expect(
      within(screen.getByRole("listitem", { name: "Révision 2" })).queryByRole("button", {
        name: t.reject,
      }),
    ).toBeNull();
  });

  it("speaks English and keeps a deleted machine read only", async () => {
    await card(
      view({ canPropose: "none", configurationStatus: "pending", revisions: [revision(1, "approved")] }),
      "admin",
      "en",
    );
    expect(screen.getByRole("heading", { name: en.machineConfig.title })).toBeTruthy();
    expect(screen.getByText(en.machineConfig.status.pending)).toBeTruthy();
    expect(screen.queryByRole("form")).toBeNull();
  });
});

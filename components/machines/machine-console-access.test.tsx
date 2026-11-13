// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import { format } from "date-fns";
import { fr as frLocale } from "date-fns/locale";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import fr from "@/messages/fr.json";
import {
  answer,
  codedFailures,
  feedbackShown,
  lastArgsAsked,
  mutation,
  mutationsSent,
  NOW,
  renderPage,
  takeLoggedFailures,
} from "@/test-support/pages";
import { MachineConsoleAccessCard } from "./MachineConsoleAccessCard";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);

/**
 * Who may act at a machine's console (ANH-152), for its admins: the operators
 * enabled and disabled, the members who could be, the tablets with their token
 * shown once. The server decides who may do it (convex/consoleAccess.ts).
 */

type Access = NonNullable<
  FunctionReturnType<typeof api.consoleAccess.listConsoleAccess>
>;

const machineId = "machine-1" as Id<"machines">;
const t = fr.consoleAccess;
const DAY = 86_400_000;
// Made up, and plain enough for the secret scan not to take it for a token.
const PLAIN = "jeton-aaaa-bbbb";
const when = (time: number) => format(time, "PPp", { locale: frLocale });

function access(overrides: Partial<Access> = {}): Access {
  return {
    operators: [
      {
        userId: "user-alice" as Id<"users">,
        name: "Alice Martin",
        enabled: true,
        eligible: true,
        codeSet: true,
        enabledAt: NOW - 3 * DAY,
        disabledAt: null,
        codeSetAt: NOW - 2 * DAY,
        trainedAt: null,
      },
      {
        userId: "user-bruno" as Id<"users">,
        name: "Bruno Petit",
        enabled: false,
        eligible: false,
        codeSet: false,
        enabledAt: NOW - 5 * DAY,
        disabledAt: NOW - DAY,
        codeSetAt: null,
        trainedAt: null,
      },
    ],
    candidates: [{ userId: "user-chloe" as Id<"users">, name: "Chloé Roux" }],
    devices: [
      {
        _id: "device-1" as Id<"machine_devices">,
        name: "Tablette du banc",
        prefix: "anhd1.ab12cd34",
        createdAt: NOW - 4 * DAY,
        createdByName: "Alice Martin",
        revokedAt: null,
      },
      {
        _id: "device-0" as Id<"machine_devices">,
        name: "Ancienne tablette",
        prefix: "anhd1.00000000",
        createdAt: NOW - 9 * DAY,
        createdByName: "Alice Martin",
        revokedAt: NOW - 8 * DAY,
      },
    ],
    ...overrides,
  };
}

async function card(...given: [Access | null | undefined] | []) {
  // An explicit `undefined` is "still loading", not the default answer.
  answer(api.consoleAccess.listConsoleAccess, given.length === 0 ? access() : given[0]);
  return renderPage(<MachineConsoleAccessCard machineId={machineId} />);
}

const rowOf = (text: string) =>
  within(screen.getByText(text).closest("li") as HTMLElement);

describe("console access: what the card shows", () => {
  it("shows nothing to anyone the server answers nothing to, nor while it loads", async () => {
    const loading = await card(undefined);
    expect(lastArgsAsked(api.consoleAccess.listConsoleAccess)).toEqual({ machineId });
    expect(loading.container.textContent).toBe("");
    loading.unmount();
    const refused = await card(null);
    expect(refused.container.textContent).toBe("");
  });

  it("shows each operator with their state and their code's, never the code", async () => {
    await card();

    expect(screen.getByText(t.codeNotice)).toBeTruthy();
    const alice = rowOf("Alice Martin");
    expect(alice.getByText(t.enabled)).toBeTruthy();
    expect(alice.getByText(`Code défini le ${when(NOW - 2 * DAY)}`)).toBeTruthy();
    expect(alice.getByRole("button", { name: t.disable })).toBeTruthy();
    const bruno = rowOf("Bruno Petit");
    expect(bruno.getByText(t.disabled)).toBeTruthy();
    expect(bruno.getByText(t.codeMissing)).toBeTruthy();
    expect(bruno.getByText(t.notEligible)).toBeTruthy();
    expect(bruno.queryByRole("button")).toBeNull();
  });

  it("shows each tablet by its name and prefix, and offers to revoke a valid one only", async () => {
    await card();

    const bench = rowOf("Tablette du banc");
    expect(bench.getByText("anhd1.ab12cd34")).toBeTruthy();
    expect(bench.getByText(`Créée le ${when(NOW - 4 * DAY)} par Alice Martin`)).toBeTruthy();
    expect(bench.getByRole("button", { name: t.revoke })).toBeTruthy();
    const old = rowOf("Ancienne tablette");
    expect(old.getByText(t.revokedBadge)).toBeTruthy();
    expect(
      old.getByText(
        `Créée le ${when(NOW - 9 * DAY)} par Alice Martin · révoquée le ${when(NOW - 8 * DAY)}`,
      ),
    ).toBeTruthy();
    expect(old.queryByRole("button")).toBeNull();
  });

  it("says when there is no operator, no candidate and no tablet", async () => {
    await card(access({ operators: [], candidates: [], devices: [] }));

    expect(screen.getByText(t.noOperators)).toBeTruthy();
    expect(screen.getByText(t.noCandidates)).toBeTruthy();
    expect(screen.getByText(t.noDevices)).toBeTruthy();
    expect(screen.queryByRole("combobox")).toBeNull();
  });
});

describe("console access: operators", () => {
  it("enables the member chosen, and nothing before one is chosen", async () => {
    const { user } = await card();
    const enable = screen.getByRole("button", { name: t.enable });
    expect(enable.hasAttribute("disabled")).toBe(true);

    await user.click(screen.getByRole("combobox", { name: t.enableLabel }));
    await user.click(screen.getByRole("option", { name: "Chloé Roux" }));
    await user.click(screen.getByRole("button", { name: t.enable }));

    expect(mutationsSent()).toEqual([
      {
        name: "consoleAccess:enableOperator",
        args: { machineId, userId: "user-chloe" },
      },
    ]);
    expect(feedbackShown()).toEqual([
      { kind: "success", message: fr.feedback.operatorEnabled },
    ]);
  });

  it.each(codedFailures(api.consoleAccess.enableOperator, "operator_not_eligible"))(
    "shows why an enablement failed $where",
    async ({ error, shown }) => {
      mutation(api.consoleAccess.enableOperator).mockRejectedValue(error);
      const { user } = await card();
      await user.click(screen.getByRole("combobox", { name: t.enableLabel }));
      await user.click(screen.getByRole("option", { name: "Chloé Roux" }));
      await user.click(screen.getByRole("button", { name: t.enable }));

      expect(feedbackShown()).toEqual([{ kind: "error", message: shown }]);
      expect(takeLoggedFailures()).toEqual(["consoleAccess:enableOperator"]);
    },
  );

  it("disables an operator after a confirmation that says when it takes effect", async () => {
    const { user } = await card();
    await user.click(rowOf("Alice Martin").getByRole("button", { name: t.disable }));
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText("Désactiver Alice Martin ?")).toBeTruthy();
    expect(
      dialog.getByText(t.disableDescription.replace("{name}", "Alice Martin")),
    ).toBeTruthy();
    expect(mutationsSent()).toEqual([]);

    await user.click(dialog.getByRole("button", { name: t.disable }));

    expect(mutationsSent()).toEqual([
      {
        name: "consoleAccess:disableOperator",
        args: { machineId, userId: "user-alice" },
      },
    ]);
    expect(feedbackShown()).toEqual([
      { kind: "success", message: fr.feedback.operatorDisabled },
    ]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps the confirmation open when the disablement fails, and sends nothing on cancel", async () => {
    mutation(api.consoleAccess.disableOperator).mockRejectedValue(new Error("down"));
    const { user } = await card();
    await user.click(rowOf("Alice Martin").getByRole("button", { name: t.disable }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: t.disable }));
    expect(takeLoggedFailures()).toEqual(["consoleAccess:disableOperator"]);
    expect(screen.getByRole("dialog")).toBeTruthy();

    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: fr.common.cancel }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mutationsSent()).toHaveLength(1);
  });
});

describe("console access: tablets", () => {
  async function create(name = "Tablette 2") {
    const view = await card();
    await view.user.click(screen.getByRole("button", { name: t.createDevice }));
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText(t.createDeviceTitle)).toBeTruthy();
    expect(
      dialog.getByRole("button", { name: t.confirmCreateDevice }).hasAttribute("disabled"),
    ).toBe(true);
    await view.user.type(dialog.getByLabelText(t.deviceName), name);
    await view.user.click(dialog.getByRole("button", { name: t.confirmCreateDevice }));
    return view;
  }

  it("creates it by its name, then shows its token once, gone once the window is closed", async () => {
    mutation(api.consoleAccess.createDevice).mockResolvedValue({
      deviceId: "device-2",
      token: PLAIN,
      prefix: "anhd1.jeton-aa",
    });
    const { user } = await create();

    expect(mutationsSent()).toEqual([
      { name: "consoleAccess:createDevice", args: { machineId, name: "Tablette 2" } },
    ]);
    expect(feedbackShown()).toEqual([
      { kind: "success", message: fr.feedback.deviceCreated },
    ]);
    const shown = within(screen.getByRole("dialog"));
    expect(shown.getByText(PLAIN)).toBeTruthy();
    expect(shown.getByText(t.tokenWarning)).toBeTruthy();
    const copied = vi.spyOn(navigator.clipboard, "writeText");
    await user.click(shown.getByRole("button", { name: fr.common.copy }));
    expect(copied.mock.calls).toEqual([[PLAIN]]);
    expect(shown.getByRole("button", { name: fr.common.copied })).toBeTruthy();

    await user.click(shown.getByRole("button", { name: fr.common.close }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText(PLAIN)).toBeNull();
  });

  it.each(codedFailures(api.consoleAccess.createDevice, "invalid_device_name"))(
    "shows no token and keeps the question open when the creation fails $where",
    async ({ error, shown }) => {
      mutation(api.consoleAccess.createDevice).mockRejectedValue(error);
      await create();

      expect(feedbackShown()).toEqual([{ kind: "error", message: shown }]);
      expect(takeLoggedFailures()).toEqual(["consoleAccess:createDevice"]);
      expect(screen.queryByText(t.tokenWarning)).toBeNull();
      expect(within(screen.getByRole("dialog")).getByText(t.createDeviceTitle)).toBeTruthy();
    },
  );

  it("sends nothing when the creation is cancelled", async () => {
    const { user } = await card();
    await user.click(screen.getByRole("button", { name: t.createDevice }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: fr.common.cancel }));
    expect(mutationsSent()).toEqual([]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("revokes the tablet named in the confirmation, and keeps it open on a failure", async () => {
    const { user } = await card();
    await user.click(rowOf("Tablette du banc").getByRole("button", { name: t.revoke }));
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText("Révoquer la tablette Tablette du banc ?")).toBeTruthy();
    expect(dialog.getByText(t.revokeDescription)).toBeTruthy();
    expect(mutationsSent()).toEqual([]);

    mutation(api.consoleAccess.revokeDevice).mockRejectedValueOnce(new Error("down"));
    await user.click(dialog.getByRole("button", { name: t.revoke }));
    expect(takeLoggedFailures()).toEqual(["consoleAccess:revokeDevice"]);
    expect(screen.getByRole("dialog")).toBeTruthy();

    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: t.revoke }));
    expect(mutationsSent()).toEqual([
      { name: "consoleAccess:revokeDevice", args: { deviceId: "device-1" } },
      { name: "consoleAccess:revokeDevice", args: { deviceId: "device-1" } },
    ]);
    expect(feedbackShown().at(-1)).toEqual({
      kind: "success",
      message: fr.feedback.deviceRevoked,
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("sends nothing when the revocation is cancelled", async () => {
    const { user } = await card();
    await user.click(rowOf("Tablette du banc").getByRole("button", { name: t.revoke }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: fr.common.cancel }));
    expect(mutationsSent()).toEqual([]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

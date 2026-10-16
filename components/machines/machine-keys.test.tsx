// @vitest-environment jsdom
import { act, fireEvent, screen, within } from "@testing-library/react";
import { format } from "date-fns";
import { enUS, fr as frLocale } from "date-fns/locale";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import en from "@/messages/en.json";
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
import { MachineKeysCard } from "./MachineKeysCard";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);

/**
 * The card of a machine's keys (ANH-165): what it shows of each key, what a
 * new key sends and how it is shown (once), and what a revocation sends after
 * its confirmation. The server decides who may do it (convex/machineKeys.ts).
 */

type Key = FunctionReturnType<typeof api.machineKeys.listMachineKeys>[number];

const machineId = "machine-1" as Id<"machines">;
const t = fr.machineKeys;
const DAY = 86_400_000;
// Made up, and plain enough for the secret scan of the repository not to take
// it for a real key.
const PLAIN = "cle-aaaa-bbbb-cccc";

type TableKey = Extract<Key, { kind: "table" }>;
function key(overrides: Partial<TableKey> = {}): TableKey {
  return {
    kind: "table",
    _id: "key-1" as Id<"machine_api_keys">,
    prefix: "anh1.ab12cd34",
    state: "active",
    createdAt: NOW - 30 * DAY,
    createdByName: "Alice Admin",
    lastUsedAt: NOW - 60_000,
    expiresAt: null,
    revokedAt: null,
    ...overrides,
  };
}

const when = (time: number, locale = frLocale) =>
  format(time, "PPp", { locale });

async function card(keys: Key[] | undefined, locale: "fr" | "en" = "fr") {
  answer(api.machineKeys.listMachineKeys, keys);
  return renderPage(<MachineKeysCard machineId={machineId} />, { locale });
}

/** The line of the list that shows this prefix. */
function rowOf(prefix: string) {
  return within(screen.getByText(prefix).closest("li") as HTMLElement);
}

describe("machine keys: what the card shows", () => {
  it("asks for the keys of this machine and shows a placeholder while they load", async () => {
    await card(undefined);

    expect(lastArgsAsked(api.machineKeys.listMachineKeys)).toEqual({
      machineId,
    });
    expect(screen.queryByText(t.none)).toBeNull();
    expect(screen.queryByRole("listitem")).toBeNull();
  });

  it("says that the machine has no key", async () => {
    await card([]);

    expect(screen.getByText(t.none)).toBeTruthy();
    expect(screen.queryByRole("button", { name: t.revoke })).toBeNull();
  });

  it("shows each key by its prefix, with its state, creation, last use and expiry", async () => {
    await card([
      key({ expiresAt: NOW + 90 * DAY }),
      key({
        _id: "key-0" as Id<"machine_api_keys">,
        prefix: "anh1.0000ffff",
        state: "replaced",
        createdByName: null,
        lastUsedAt: null,
        expiresAt: NOW + DAY,
      }),
    ]);

    const active = rowOf("anh1.ab12cd34");
    expect(active.getByText(t.state.active)).toBeTruthy();
    expect(
      active.getByText(`Créée le ${when(NOW - 30 * DAY)} par Alice Admin`),
    ).toBeTruthy();
    expect(
      active.getByText(
        `Dernière utilisation : ${when(NOW - 60_000)} · Expire le ${when(NOW + 90 * DAY)}`,
      ),
    ).toBeTruthy();
    const replaced = rowOf("anh1.0000ffff");
    expect(replaced.getByText(t.state.replaced)).toBeTruthy();
    expect(replaced.getByText(`Créée le ${when(NOW - 30 * DAY)}`)).toBeTruthy();
    expect(
      replaced.getByText(`${t.neverUsed} · Expire le ${when(NOW + DAY)}`),
    ).toBeTruthy();
    // A valid key, active or in its overlap, can be revoked.
    expect(screen.getAllByRole("button", { name: t.revoke })).toHaveLength(2);
  });

  it("offers no revocation of a key that opens nothing any more", async () => {
    await card([
      key({ state: "expired", expiresAt: NOW - DAY }),
      key({
        _id: "key-0" as Id<"machine_api_keys">,
        prefix: "anh1.0000ffff",
        state: "revoked",
        revokedAt: NOW - 2 * DAY,
        expiresAt: NOW - DAY,
      }),
      key({
        _id: "key-z" as Id<"machine_api_keys">,
        prefix: "anh1.zzzz0000",
        state: "active",
      }),
    ]);

    expect(
      rowOf("anh1.ab12cd34").getByText(
        `Dernière utilisation : ${when(NOW - 60_000)} · Expirée le ${when(NOW - DAY)}`,
      ),
    ).toBeTruthy();
    expect(
      rowOf("anh1.0000ffff").getByText(
        `Dernière utilisation : ${when(NOW - 60_000)} · Révoquée le ${when(NOW - 2 * DAY)}`,
      ),
    ).toBeTruthy();
    expect(
      rowOf("anh1.zzzz0000").getByText(
        `Dernière utilisation : ${when(NOW - 60_000)} · ${t.noExpiry}`,
      ),
    ).toBeTruthy();
    expect(rowOf("anh1.ab12cd34").queryByRole("button")).toBeNull();
    expect(rowOf("anh1.0000ffff").queryByRole("button")).toBeNull();
    expect(rowOf("anh1.zzzz0000").getByRole("button")).toBeTruthy();
  });

  it("writes the dates in the language of the page", async () => {
    await card([key()], "en");

    expect(
      screen.getByText(
        `Created on ${when(NOW - 30 * DAY, enUS)} by Alice Admin`,
      ),
    ).toBeTruthy();
    expect(screen.getByText(en.machineKeys.state.active)).toBeTruthy();
  });
});

describe("machine keys: a new key", () => {
  async function create(
    choose?: (user: Awaited<ReturnType<typeof card>>["user"]) => Promise<void>,
  ) {
    const view = await card([key()]);
    await view.user.click(screen.getByRole("button", { name: t.create }));
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText(t.createTitle)).toBeTruthy();
    await choose?.(view.user);
    expect(mutationsSent()).toEqual([]);
    await view.user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: t.confirmCreate,
      }),
    );
    return view;
  }

  it("creates it with a 24 h overlap and no expiry by default, then shows it once with the warning", async () => {
    mutation(api.machineKeys.createMachineKey).mockResolvedValue({
      keyId: "key-2",
      apiKey: PLAIN,
      prefix: "anh1.cle-aaaa",
    });

    await create();

    expect(mutationsSent()).toEqual([
      {
        name: "machineKeys:createMachineKey",
        args: { machineId, overlapMinutes: 1440, lifetimeMinutes: undefined },
      },
    ]);
    expect(feedbackShown()).toEqual([
      { kind: "success", message: fr.feedback.machineKeyCreated },
    ]);
    const shown = within(screen.getByRole("dialog"));
    expect(shown.getByText(PLAIN)).toBeTruthy();
    expect(shown.getByText(fr.machines.apiKeyWarning)).toBeTruthy();
  });

  it("sends the overlap and the expiry chosen", async () => {
    mutation(api.machineKeys.createMachineKey).mockResolvedValue({
      keyId: "key-2",
      apiKey: PLAIN,
      prefix: "anh1.cle-aaaa",
    });

    await create(async (user) => {
      await user.click(screen.getByRole("combobox", { name: t.overlap }));
      await user.click(screen.getByRole("option", { name: t.overlap0 }));
      await user.click(screen.getByRole("combobox", { name: t.lifetime }));
      await user.click(screen.getByRole("option", { name: t.lifetime90 }));
    });

    expect(mutationsSent()).toEqual([
      {
        name: "machineKeys:createMachineKey",
        // A duration: the server dates the expiry on its own clock.
        args: { machineId, overlapMinutes: 0, lifetimeMinutes: 90 * 1440 },
      },
    ]);
  });

  it("copies the key and says so for two seconds", async () => {
    mutation(api.machineKeys.createMachineKey).mockResolvedValue({
      keyId: "key-2",
      apiKey: PLAIN,
      prefix: "anh1.cle-aaaa",
    });
    await create();
    const copied = vi.spyOn(navigator.clipboard, "writeText");
    const shown = within(screen.getByRole("dialog"));

    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    fireEvent.click(shown.getByRole("button", { name: fr.common.copy }));
    await act(async () => {});

    expect(copied.mock.calls).toEqual([[PLAIN]]);
    expect(shown.getByRole("button", { name: fr.common.copied })).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(2_000);
    });
    expect(shown.getByRole("button", { name: fr.common.copy })).toBeTruthy();
  });

  it.each([
    [
      "its Close button",
      async (user: Awaited<ReturnType<typeof card>>["user"]) =>
        user.click(
          within(screen.getByRole("dialog")).getByRole("button", {
            name: fr.common.close,
          }),
        ),
    ],
    [
      "the keyboard",
      async (user: Awaited<ReturnType<typeof card>>["user"]) =>
        user.keyboard("{Escape}"),
    ],
  ])(
    "no longer shows the key once its window is closed by %s",
    async (_how, close) => {
      mutation(api.machineKeys.createMachineKey).mockResolvedValue({
        keyId: "key-2",
        apiKey: PLAIN,
        prefix: "anh1.cle-aaaa",
      });
      const { user } = await create();

      await close(user);

      expect(screen.queryByRole("dialog")).toBeNull();
      expect(screen.queryByText(PLAIN)).toBeNull();
    },
  );

  it.each(
    codedFailures(api.machineKeys.createMachineKey, "invalid_key_expiry"),
  )(
    "shows no key, the failure, and keeps its question open when the creation fails $where",
    async ({ error, shown }) => {
      mutation(api.machineKeys.createMachineKey).mockRejectedValue(error);

      await create();

      expect(feedbackShown()).toEqual([{ kind: "error", message: shown }]);
      expect(takeLoggedFailures()).toEqual(["machineKeys:createMachineKey"]);
      expect(screen.queryByText(fr.machines.apiKeyWarning)).toBeNull();
      const stillOpen = within(screen.getByRole("dialog"));
      expect(stillOpen.getByText(t.createTitle)).toBeTruthy();
      expect(
        stillOpen
          .getByRole("button", { name: t.confirmCreate })
          .hasAttribute("disabled"),
      ).toBe(false);
    },
  );

  it("sends nothing when the creation is cancelled", async () => {
    const { user } = await card([key()]);

    await user.click(screen.getByRole("button", { name: t.create }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: fr.common.cancel,
      }),
    );

    expect(mutationsSent()).toEqual([]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("machine keys: revoking a key", () => {
  const other = key({
    _id: "key-0" as Id<"machine_api_keys">,
    prefix: "anh1.0000ffff",
    state: "replaced",
    expiresAt: NOW + DAY,
  });

  async function revoking(keys: Key[], prefix: string) {
    const view = await card(keys);
    await view.user.click(
      rowOf(prefix).getByRole("button", { name: t.revoke }),
    );
    return { ...view, dialog: within(screen.getByRole("dialog")) };
  }

  it("asks for a confirmation that names the key: the first click sends nothing", async () => {
    const { dialog } = await revoking([key(), other], "anh1.0000ffff");

    expect(dialog.getByText("Révoquer la clé anh1.0000ffff ?")).toBeTruthy();
    expect(dialog.getByText(t.revokeDescription)).toBeTruthy();
    // Another key stays valid: no warning that the machine will be cut off.
    expect(dialog.queryByText(t.revokeLast)).toBeNull();
    expect(mutationsSent()).toEqual([]);
  });

  it("warns that the machine will be cut off when the key is its only valid one", async () => {
    const { dialog } = await revoking(
      [key(), key({ ...other, state: "revoked", revokedAt: NOW - DAY })],
      "anh1.ab12cd34",
    );

    expect(dialog.getByText(t.revokeLast)).toBeTruthy();
  });

  it("revokes the key named in the window, and no other", async () => {
    const { user, dialog } = await revoking([key(), other], "anh1.0000ffff");

    await user.click(dialog.getByRole("button", { name: t.revoke }));

    expect(mutationsSent()).toEqual([
      { name: "machineKeys:revokeMachineKey", args: { keyId: "key-0" } },
    ]);
    expect(feedbackShown()).toEqual([
      { kind: "success", message: fr.feedback.machineKeyRevoked },
    ]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it.each(
    codedFailures(api.machineKeys.revokeMachineKey, "machine_key_not_found"),
  )(
    "shows the failure and keeps the window open when the revocation fails $where",
    async ({ error, shown }) => {
      mutation(api.machineKeys.revokeMachineKey).mockRejectedValue(error);
      const { user, dialog } = await revoking([key()], "anh1.ab12cd34");

      await user.click(dialog.getByRole("button", { name: t.revoke }));

      expect(feedbackShown()).toEqual([{ kind: "error", message: shown }]);
      expect(takeLoggedFailures()).toEqual(["machineKeys:revokeMachineKey"]);
      expect(
        within(screen.getByRole("dialog")).getByText(
          "Révoquer la clé anh1.ab12cd34 ?",
        ),
      ).toBeTruthy();
    },
  );

  it("cannot be sent twice while the server has not answered", async () => {
    const server = Promise.withResolvers<null>();
    mutation(api.machineKeys.revokeMachineKey).mockReturnValue(server.promise);
    const { user, dialog } = await revoking([key()], "anh1.ab12cd34");

    await user.click(dialog.getByRole("button", { name: t.revoke }));
    const waiting = dialog.getByRole("button", { name: t.revoke });
    expect(waiting.hasAttribute("disabled")).toBe(true);
    await user.click(waiting);
    expect(mutationsSent()).toHaveLength(1);

    await act(async () => server.resolve(null));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("sends nothing when the revocation is cancelled", async () => {
    const { user, dialog } = await revoking([key()], "anh1.ab12cd34");

    await user.click(dialog.getByRole("button", { name: fr.common.cancel }));

    expect(mutationsSent()).toEqual([]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("machine keys: the historical key and a deleted machine", () => {
  const legacy: Key = {
    kind: "legacy",
    prefix: "anh1.12121212",
    state: "active",
  };

  it.each(["fr", "en"] as const)(
    "shows the historical key with its state and unknown dates in %s",
    async (locale) => {
      await card([legacy], locale);
      const copy = locale === "fr" ? fr : en;
      const row = rowOf(legacy.prefix);

      expect(row.getByText(copy.machineKeys.state.active)).toBeTruthy();
      expect(row.getByText(copy.machineKeys.legacy)).toBeTruthy();
      expect(row.getByText(copy.machineKeys.legacyDescription)).toBeTruthy();
      expect(screen.queryByText(copy.machineKeys.none)).toBeNull();
      expect(row.queryByText(copy.machineKeys.neverUsed)).toBeNull();
    },
  );

  it("names the historical key, warns when it is alone, and sends its machine id only once", async () => {
    const server = Promise.withResolvers<null>();
    mutation(api.machineKeys.revokeLegacyMachineKey).mockReturnValue(
      server.promise,
    );
    const { user } = await card([legacy]);
    await user.click(
      rowOf(legacy.prefix).getByRole("button", { name: t.revoke }),
    );
    const dialog = within(screen.getByRole("dialog"));
    expect(
      dialog.getByText(t.revokeTitle.replace("{prefix}", legacy.prefix)),
    ).toBeTruthy();
    expect(dialog.getByText(t.revokeDescription)).toBeTruthy();
    expect(dialog.getByText(t.revokeLast)).toBeTruthy();
    expect(mutationsSent()).toEqual([]);

    const confirm = dialog.getByRole("button", { name: t.revoke });
    await user.click(confirm);
    expect(confirm.hasAttribute("disabled")).toBe(true);
    await user.click(confirm);
    expect(mutationsSent()).toEqual([
      { name: "machineKeys:revokeLegacyMachineKey", args: { machineId } },
    ]);
    await act(async () => server.resolve(null));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("does not warn of the last valid key when a table key is also valid, and cancels without sending", async () => {
    const { user } = await card([legacy, key()]);
    await user.click(
      rowOf(legacy.prefix).getByRole("button", { name: t.revoke }),
    );
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.queryByText(t.revokeLast)).toBeNull();

    await user.click(dialog.getByRole("button", { name: fr.common.cancel }));

    expect(mutationsSent()).toEqual([]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it.each(
    codedFailures(
      api.machineKeys.revokeLegacyMachineKey,
      "machine_key_not_found",
    ),
  )(
    "keeps the historical confirmation open after a refusal $where",
    async ({ error, shown }) => {
      mutation(api.machineKeys.revokeLegacyMachineKey).mockRejectedValue(error);
      const { user } = await card([legacy]);
      await user.click(
        rowOf(legacy.prefix).getByRole("button", { name: t.revoke }),
      );

      await user.click(
        within(screen.getByRole("dialog")).getByRole("button", {
          name: t.revoke,
        }),
      );

      expect(feedbackShown()).toEqual([{ kind: "error", message: shown }]);
      expect(takeLoggedFailures()).toEqual([
        "machineKeys:revokeLegacyMachineKey",
      ]);
      expect(screen.getByRole("dialog")).toBeTruthy();
    },
  );

  it.each([legacy, key()])(
    "allows revocation alone on a deleted machine for $kind keys",
    async (listed) => {
      answer(api.machineKeys.listMachineKeys, [listed]);
      const { user } = await renderPage(
        <MachineKeysCard machineId={machineId} deleted />,
      );
      expect(screen.queryByRole("button", { name: t.create })).toBeNull();
      expect(screen.getByText(t.deleted)).toBeTruthy();
      await user.click(
        rowOf(listed.prefix).getByRole("button", { name: t.revoke }),
      );

      await user.click(
        within(screen.getByRole("dialog")).getByRole("button", {
          name: t.revoke,
        }),
      );

      expect(mutationsSent()).toEqual([
        listed.kind === "legacy"
          ? { name: "machineKeys:revokeLegacyMachineKey", args: { machineId } }
          : {
              name: "machineKeys:revokeMachineKey",
              args: { keyId: listed._id },
            },
      ]);
    },
  );
});

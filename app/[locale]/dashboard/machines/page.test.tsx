// @vitest-environment jsdom
import { act, screen, within } from "@testing-library/react";
import { formatDistance } from "date-fns";
import { fr as frLocale } from "date-fns/locale";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import fr from "@/messages/fr.json";
import {
  answer,
  argsAsked,
  lastArgsAsked,
  NOW,
  propsOf,
  renderPage,
  router,
  signedInAs,
} from "@/test-support/pages";
import MachinesPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);
// The window that creates a machine sends its own mutation: the page only
// opens it.
vi.mock("@/components/modals/MachineFormModal", async () => ({
  MachineFormModal: (await import("@/test-support/pages")).standIn(
    "MachineFormModal",
  ),
}));

/** The list of machines of an admin or of a manager. */

type Machine = FunctionReturnType<typeof api.machines.listMachines>[number];
type CreateWindow = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  machine?: unknown;
};

function machine(name: string, overrides: Partial<Machine> = {}): Machine {
  return {
    _id: `machine-${name.toLowerCase()}` as Id<"machines">,
    name,
    status: "online",
    lastHeartbeat: NOW - 5_000,
    serverNow: NOW,
    location: `Salle ${name}`,
    isDeleted: undefined,
    ...overrides,
  };
}

const paris = machine("Paris");
const lyon = machine("Lyon", { status: "in_session", location: undefined });
const nice = machine("Nice", {
  status: "offline",
  lastHeartbeat: NOW - 3 * 3_600_000,
});
const retired = machine("Brest", { status: "offline", isDeleted: true });

/** The row of the table that names `text`. */
function rowOf(text: string) {
  const row = screen.getByText(text).closest("tr");
  if (row === null) throw new Error(`No row holds "${text}"`);
  return within(row);
}

/** The text of each cell of the row that names `text`. */
function cellsOf(text: string): (string | null)[] {
  return rowOf(text)
    .getAllByRole("cell")
    .map((cell) => cell.textContent);
}

/** The machines listed, in the order of the rows. */
function machinesListed(): string[] {
  return screen
    .queryAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0].textContent ?? "");
}

const createButton = { name: fr.machines.create };

describe("machines list: what it asks", () => {
  it("asks for the deleted machines too when the visitor is an admin", async () => {
    signedInAs("admin");
    answer(api.machines.listMachines, [paris]);
    await renderPage(<MachinesPage />);

    expect(lastArgsAsked(api.machines.listMachines)).toEqual({
      includeDeleted: true,
    });
  });

  it.each(["gestionnaire", "org_admin", "user"] as const)(
    "never asks for the deleted machines as a %s",
    async (role) => {
      signedInAs(role);
      answer(api.machines.listMachines, [paris]);
      await renderPage(<MachinesPage />);

      expect(argsAsked(api.machines.listMachines)).not.toEqual([]);
      for (const args of argsAsked(api.machines.listMachines)) {
        expect(args).toEqual({ includeDeleted: undefined });
      }
    },
  );

  it("shows no table and no count while the list is loading", async () => {
    signedInAs("admin");
    answer(api.machines.listMachines, undefined);
    await renderPage(<MachinesPage />);

    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText(fr.machines.noMachines)).toBeNull();
    expect(screen.queryByRole("button", createButton)).toBeNull();
  });
});

describe("machines list: what it shows", () => {
  it("shows each machine with its status, its place and its last signal", async () => {
    signedInAs("gestionnaire");
    answer(api.machines.listMachines, [paris, lyon, nice]);
    await renderPage(<MachinesPage />);

    expect(screen.getByText("3 machines")).toBeTruthy();
    expect(cellsOf("Paris").slice(0, 4)).toEqual([
      "Paris",
      fr.machines.online,
      "Salle Paris",
      formatDistance(NOW - 5_000, NOW, { addSuffix: true, locale: frLocale }),
    ]);
    expect(cellsOf("Lyon").slice(0, 3)).toEqual([
      "Lyon",
      fr.machines.inSession,
      "-",
    ]);
    expect(cellsOf("Nice").slice(0, 4)).toEqual([
      "Nice",
      fr.machines.offline,
      "Salle Nice",
      "il y a environ 3 heures",
    ]);
  });

  it("shows offline a machine silent for more than 90 s, whatever status the server still holds", async () => {
    signedInAs("gestionnaire");
    answer(api.machines.listMachines, [
      machine("Paris", { status: "online", lastHeartbeat: NOW - 91_000 }),
    ]);
    await renderPage(<MachinesPage />);

    expect(cellsOf("Paris")[1]).toBe(fr.machines.offline);
  });

  it("shows a dash for the last signal of a machine that never connected", async () => {
    signedInAs("gestionnaire");
    answer(api.machines.listMachines, [
      machine("Paris", { status: "offline", lastHeartbeat: 0 }),
    ]);
    await renderPage(<MachinesPage />);

    expect(cellsOf("Paris")[3]).toBe("-");
  });

  it("says so when there is no machine", async () => {
    signedInAs("gestionnaire");
    answer(api.machines.listMachines, []);
    await renderPage(<MachinesPage />);

    expect(screen.getByText(fr.machines.noMachines)).toBeTruthy();
    expect(screen.getByText("0 machines")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("keeps the machines that match the search, by name or by place", async () => {
    signedInAs("gestionnaire");
    answer(api.machines.listMachines, [paris, lyon, nice]);
    const { user } = await renderPage(<MachinesPage />);
    const search = screen.getByPlaceholderText(fr.common.search);

    await user.type(search, "lyo");
    expect(machinesListed()).toEqual(["Lyon"]);

    await user.clear(search);
    await user.type(search, "salle nice");
    expect(machinesListed()).toEqual(["Nice"]);

    await user.clear(search);
    await user.type(search, "nowhere");
    expect(machinesListed()).toEqual([]);
    expect(screen.getByText(fr.machines.noMachines)).toBeTruthy();
  });

  it("opens the page of a machine from its row and from its link", async () => {
    signedInAs("gestionnaire");
    answer(api.machines.listMachines, [paris, lyon]);
    const { user } = await renderPage(<MachinesPage />);

    expect(rowOf("Lyon").getByRole("link").getAttribute("href")).toBe(
      "/dashboard/machines/machine-lyon",
    );
    await user.click(screen.getByText("Salle Paris"));

    expect(router.push.mock.calls).toEqual([
      ["/dashboard/machines/machine-paris"],
    ]);
  });
});

describe("machines list: the deleted machines", () => {
  it("hides them from an admin until asked for, and does not count them", async () => {
    signedInAs("admin");
    answer(api.machines.listMachines, [paris, retired]);
    const { user } = await renderPage(<MachinesPage />);

    expect(machinesListed()).toEqual(["Paris"]);
    // What the page does today: the count is not put in the singular.
    expect(screen.getByText("1 machines")).toBeTruthy();

    await user.click(
      screen.getByRole("switch", { name: fr.machines.showDeleted }),
    );

    expect(machinesListed()).toEqual(["Paris", "Brest"]);
    expect(screen.getByText("2 machines")).toBeTruthy();
    expect(cellsOf("Brest")[1]).toBe(fr.machines.deleted);

    await user.click(
      screen.getByRole("switch", { name: fr.machines.showDeleted }),
    );
    expect(machinesListed()).toEqual(["Paris"]);
  });

  it.each(["gestionnaire", "org_admin", "user"] as const)(
    "gives a %s no way to ask for them",
    async (role) => {
      signedInAs(role);
      answer(api.machines.listMachines, [paris]);
      await renderPage(<MachinesPage />);

      expect(screen.queryByRole("switch")).toBeNull();
      expect(screen.queryByText(fr.machines.showDeleted)).toBeNull();
    },
  );
});

describe("machines list: creating a machine", () => {
  it("lets an admin open the creation window, on no machine", async () => {
    signedInAs("admin");
    answer(api.machines.listMachines, [paris]);
    const { user } = await renderPage(<MachinesPage />);
    expect(propsOf<CreateWindow>("MachineFormModal").open).toBe(false);

    await user.click(screen.getByRole("button", createButton));

    expect(propsOf<CreateWindow>("MachineFormModal").open).toBe(true);
    expect(propsOf<CreateWindow>("MachineFormModal").machine).toBeUndefined();

    act(() => propsOf<CreateWindow>("MachineFormModal").onOpenChange(false));
    expect(propsOf<CreateWindow>("MachineFormModal").open).toBe(false);
  });

  it("offers an admin to create one from an empty list too", async () => {
    signedInAs("admin");
    answer(api.machines.listMachines, []);
    const { user } = await renderPage(<MachinesPage />);
    const buttons = screen.getAllByRole("button", createButton);
    expect(buttons).toHaveLength(2);

    await user.click(buttons[1]);

    expect(propsOf<CreateWindow>("MachineFormModal").open).toBe(true);
  });

  it.each([
    ["a manager", "gestionnaire"],
    // What the page does today. The server refuses the creation of a machine
    // to the admin of a client organisation as well.
    ["the admin of a client organisation", "org_admin"],
    ["a patient", "user"],
  ] as const)("does not offer %s to create a machine", async (_name, role) => {
    signedInAs(role);
    answer(api.machines.listMachines, []);
    await renderPage(<MachinesPage />);

    expect(screen.getByText(fr.machines.noMachines)).toBeTruthy();
    expect(screen.queryByRole("button", createButton)).toBeNull();
    expect(propsOf<CreateWindow>("MachineFormModal").open).toBe(false);
  });
});

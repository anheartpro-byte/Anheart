// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import fr from "@/messages/fr.json";
import {
  answer,
  NOW,
  renderPage,
  router,
  signedInAs,
} from "@/test-support/pages";
import GestionnairesPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);

/** The list of managers, for an admin. */

type Gestionnaire = FunctionReturnType<
  typeof api.users.listGestionnaires
>[number];

function gestionnaire(
  firstName: string,
  lastName: string,
  overrides: Partial<Gestionnaire> = {},
): Gestionnaire {
  return {
    _id: `user-${firstName.toLowerCase()}` as Id<"users">,
    firstName,
    lastName,
    email: `${firstName.toLowerCase()}@centre.test`,
    createdAt: NOW - 86_400_000,
    machineCount: 0,
    patientCount: 0,
    ...overrides,
  };
}

const gaston = gestionnaire("Gaston", "Lagaffe", {
  machineCount: 2,
  patientCount: 7,
});
const gisele = gestionnaire("Gisèle", "Durand", {
  _id: "user-gisele" as Id<"users">,
  email: "g.durand@clinique.test",
});

/** The text of each cell of the row that names `text`. */
function cellsOf(text: string): (string | null)[] {
  const row = screen.getByText(text).closest("tr");
  if (row === null) throw new Error(`No row holds "${text}"`);
  return within(row)
    .getAllByRole("cell")
    .map((cell) => cell.textContent);
}

/** The managers listed, in the order of the rows. */
function listedNames(): string[] {
  return screen
    .queryAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0].textContent ?? "");
}

describe("gestionnaires list: who may read it", () => {
  it("shows nothing until it knows who the visitor is and has the list", async () => {
    answer(api.users.getCurrentUser, undefined);
    answer(api.users.listGestionnaires, [gaston]);
    const view = await renderPage(<GestionnairesPage />);
    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.queryByText("Gaston Lagaffe")).toBeNull();

    signedInAs("admin");
    answer(api.users.listGestionnaires, undefined);
    await view.refresh();
    expect(screen.queryByRole("heading")).toBeNull();

    answer(api.users.listGestionnaires, [gaston]);
    await view.refresh();
    expect(
      screen.getByRole("heading", { name: fr.gestionnaires.title }),
    ).toBeTruthy();
  });

  it.each([
    ["a manager", "gestionnaire"],
    ["a patient", "user"],
    // What the page does today: the admin of a client organisation, whom the
    // server lets list the managers of that organisation, is refused here.
    ["the admin of a client organisation", "org_admin"],
  ] as const)("refuses %s, and shows none of the list", async (_name, role) => {
    signedInAs(role);
    // Whatever the server would answer, nothing of it is drawn.
    answer(api.users.listGestionnaires, [gaston]);
    await renderPage(<GestionnairesPage />);

    // What the page writes today: half of the sentence is not translated.
    expect(
      screen.getByText(`${fr.common.error}: Admin access required`),
    ).toBeTruthy();
    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.queryByText("Gaston Lagaffe")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("refuses an account without a row", async () => {
    answer(api.users.getCurrentUser, null);
    answer(api.users.listGestionnaires, []);
    await renderPage(<GestionnairesPage />);

    expect(screen.getByText(/Admin access required/)).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });
});

describe("gestionnaires list: what an admin sees", () => {
  it("shows each manager with their e-mail and how many machines and patients they have", async () => {
    signedInAs("admin");
    answer(api.users.listGestionnaires, [gaston, gisele]);
    await renderPage(<GestionnairesPage />);

    expect(screen.getByText("2 gestionnaires")).toBeTruthy();
    expect(cellsOf("Gaston Lagaffe").slice(0, 4)).toEqual([
      "Gaston Lagaffe",
      "gaston@centre.test",
      "2",
      "7",
    ]);
    expect(cellsOf("Gisèle Durand").slice(0, 4)).toEqual([
      "Gisèle Durand",
      "g.durand@clinique.test",
      "0",
      "0",
    ]);
  });

  it("says so when there is no manager", async () => {
    signedInAs("admin");
    answer(api.users.listGestionnaires, []);
    await renderPage(<GestionnairesPage />);

    expect(screen.getByText(fr.gestionnaires.noGestionnaires)).toBeTruthy();
    expect(screen.getByText("0 gestionnaires")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it.each([
    ["a first name, whatever the case", "GAST", ["Gaston Lagaffe"]],
    ["a last name", "durand", ["Gisèle Durand"]],
    ["an e-mail", "clinique", ["Gisèle Durand"]],
    ["what both share", "g", ["Gaston Lagaffe", "Gisèle Durand"]],
  ])("finds a manager by %s", async (_name, typed, expected) => {
    signedInAs("admin");
    answer(api.users.listGestionnaires, [gaston, gisele]);
    const { user } = await renderPage(<GestionnairesPage />);

    await user.type(screen.getByPlaceholderText(fr.common.search), typed);

    expect(listedNames()).toEqual(expected);
    // The count is of all managers, not of those the search keeps.
    expect(screen.getByText("2 gestionnaires")).toBeTruthy();
  });

  it("says there is none when the search matches nobody", async () => {
    signedInAs("admin");
    answer(api.users.listGestionnaires, [gaston, gisele]);
    const { user } = await renderPage(<GestionnairesPage />);

    await user.type(screen.getByPlaceholderText(fr.common.search), "zzz");

    expect(screen.getByText(fr.gestionnaires.noGestionnaires)).toBeTruthy();
    expect(listedNames()).toEqual([]);
  });

  it("opens the page of a manager from their row and from their link", async () => {
    signedInAs("admin");
    answer(api.users.listGestionnaires, [gaston, gisele]);
    const { user } = await renderPage(<GestionnairesPage />);

    const row = screen.getByText("Gisèle Durand").closest("tr") as HTMLElement;
    expect(within(row).getByRole("link").getAttribute("href")).toBe(
      "/dashboard/gestionnaires/user-gisele",
    );
    await user.click(screen.getByText("gaston@centre.test"));

    expect(router.push.mock.calls).toEqual([
      ["/dashboard/gestionnaires/user-gaston"],
    ]);
  });
});

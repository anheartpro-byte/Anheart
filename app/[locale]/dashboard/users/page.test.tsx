// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import { format } from "date-fns";
import { enUS, fr as frLocale } from "date-fns/locale";
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
  renderPage,
  router,
  signedInAs,
} from "@/test-support/pages";
import UsersPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);

/** Every account, for an admin: a list filtered by role and searched. */

type Account = FunctionReturnType<typeof api.users.listUsers>[number];

function listed(
  firstName: string,
  role: Account["role"],
  overrides: Partial<Account> = {},
): Account {
  return {
    _id: `user-${firstName.toLowerCase()}` as Id<"users">,
    firstName,
    lastName: "Durand",
    email: `${firstName.toLowerCase()}@example.test`,
    role,
    language: "fr",
    createdAt: NOW - 30 * 86_400_000,
    ...overrides,
  };
}

const alice = listed("Alice", "admin");
const gaston = listed("Gaston", "gestionnaire", { language: "en" });
const rose = listed("Rose", "user", { email: "rose@club.test" });

/** What the server answers for each role asked. */
function given(accounts: Account[] = [alice, gaston, rose]) {
  answer(api.users.listUsers, ({ role }) =>
    role === undefined
      ? accounts
      : accounts.filter((account) => account.role === role),
  );
}

/** The text of each cell of the row that names `text`. */
function cellsOf(text: string): (string | null)[] {
  const row = screen.getByText(text).closest("tr");
  if (row === null) throw new Error(`No row holds "${text}"`);
  return within(row)
    .getAllByRole("cell")
    .map((cell) => cell.textContent);
}

/** The accounts listed, in the order of the rows. */
function listedNames(): string[] {
  return screen
    .queryAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0].textContent ?? "");
}

type User = Awaited<ReturnType<typeof renderPage>>["user"];

/** Chooses a role in the filter. */
async function filterBy(user: User, role: string) {
  await user.click(screen.getByRole("combobox"));
  await user.click(screen.getByRole("option", { name: role }));
}

describe("users list: what it asks and shows", () => {
  it("asks for every account first, whatever their role", async () => {
    signedInAs("admin");
    given();
    await renderPage(<UsersPage />);

    expect(argsAsked(api.users.listUsers)).not.toEqual([]);
    for (const args of argsAsked(api.users.listUsers)) {
      expect(args).toEqual({ role: undefined });
    }
  });

  it("shows no table and no count while the list is loading", async () => {
    signedInAs("admin");
    answer(api.users.listUsers, undefined);
    await renderPage(<UsersPage />);

    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText(fr.users.noUsers)).toBeNull();
  });

  it("shows each account with its e-mail, role, language and date of creation", async () => {
    signedInAs("admin");
    given();
    await renderPage(<UsersPage />);

    expect(screen.getByRole("heading", { name: fr.users.title })).toBeTruthy();
    expect(screen.getByText("3 utilisateurs")).toBeTruthy();
    const created = format(alice.createdAt, "PP", { locale: frLocale });
    expect(cellsOf("Alice Durand").slice(0, 5)).toEqual([
      "Alice Durand",
      "alice@example.test",
      fr.users.roles.admin,
      "fr",
      created,
    ]);
    expect(cellsOf("Gaston Durand").slice(2, 4)).toEqual([
      fr.users.roles.gestionnaire,
      "en",
    ]);
    expect(cellsOf("Rose Durand")[2]).toBe(fr.users.roles.user);
  });

  it("dates in English for an English visitor", async () => {
    signedInAs("admin");
    given();
    await renderPage(<UsersPage />, { locale: "en" });

    expect(cellsOf("Alice Durand")[4]).toBe(
      format(alice.createdAt, "PP", { locale: enUS }),
    );
  });

  it("says so when there is no account", async () => {
    signedInAs("admin");
    given([]);
    await renderPage(<UsersPage />);

    expect(screen.getByText(fr.users.noUsers)).toBeTruthy();
    expect(screen.getByText("0 utilisateurs")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("opens the page of an account from its row and from its link", async () => {
    signedInAs("admin");
    given();
    const { user } = await renderPage(<UsersPage />);

    const row = screen.getByText("Gaston Durand").closest("tr") as HTMLElement;
    expect(within(row).getByRole("link").getAttribute("href")).toBe(
      "/dashboard/users/user-gaston",
    );
    await user.click(screen.getByText("rose@club.test"));

    expect(router.push.mock.calls).toEqual([["/dashboard/users/user-rose"]]);
  });
});

describe("users list: filter and search", () => {
  it.each([
    [fr.users.roles.admin, "admin", ["Alice Durand"]],
    [fr.users.roles.gestionnaire, "gestionnaire", ["Gaston Durand"]],
    [fr.users.roles.user, "user", ["Rose Durand"]],
  ])(
    "asks the server for the accounts of the role chosen (%s)",
    async (label, role, expected) => {
      signedInAs("admin");
      given();
      const { user } = await renderPage(<UsersPage />);

      await filterBy(user, label);

      expect(lastArgsAsked(api.users.listUsers)).toEqual({ role });
      expect(listedNames()).toEqual(expected);
      // What the page does today: the count is not put in the singular.
      expect(screen.getByText("1 utilisateurs")).toBeTruthy();
    },
  );

  it("asks for every account again when the filter is put back on all", async () => {
    signedInAs("admin");
    given();
    const { user } = await renderPage(<UsersPage />);

    await filterBy(user, fr.users.roles.user);
    await filterBy(user, fr.common.all);

    expect(lastArgsAsked(api.users.listUsers)).toEqual({ role: undefined });
    expect(listedNames()).toEqual([
      "Alice Durand",
      "Gaston Durand",
      "Rose Durand",
    ]);
  });

  it("keeps the accounts that match the search, by name or by e-mail", async () => {
    signedInAs("admin");
    given();
    const { user } = await renderPage(<UsersPage />);
    const search = screen.getByPlaceholderText(fr.common.search);

    await user.type(search, "gast");
    expect(listedNames()).toEqual(["Gaston Durand"]);

    await user.clear(search);
    await user.type(search, "club.test");
    expect(listedNames()).toEqual(["Rose Durand"]);

    await user.clear(search);
    await user.type(search, "nobody");
    expect(listedNames()).toEqual([]);
    expect(screen.getByText(fr.users.noUsers)).toBeTruthy();
  });

  // What the page does today: it does not look at the visitor's role. The
  // server decides what the list holds.
  it.each(["org_admin", "gestionnaire", "user"] as const)(
    "shows a %s whatever the server lists for them, without asking who they are",
    async (role) => {
      signedInAs(role);
      given([rose]);
      await renderPage(<UsersPage />);

      expect(listedNames()).toEqual(["Rose Durand"]);
      expect(argsAsked(api.users.getCurrentUser)).toEqual([]);
    },
  );
});

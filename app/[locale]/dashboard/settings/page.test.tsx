// @vitest-environment jsdom
import { act, screen, within } from "@testing-library/react";
import { ConvexError } from "convex/values";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import fr from "@/messages/fr.json";
import {
  answer,
  feedbackShown,
  lastArgsAsked,
  mutation,
  mutationsSent,
  NOW,
  renderPage,
  signedInAs,
  takeLoggedFailures,
} from "@/test-support/pages";
import SettingsPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);

/** The settings of an admin: their account, the roles of the others, a few facts. */

type Account = FunctionReturnType<typeof api.users.listUsers>[number];

function listed(id: string, firstName: string, role: Account["role"]): Account {
  return {
    _id: id as Id<"users">,
    firstName,
    lastName: "Durand",
    email: `${firstName.toLowerCase()}@example.test`,
    role,
    language: "fr",
    createdAt: NOW,
  };
}

const gaston = listed("user-gaston", "Gaston", "gestionnaire");
const rose = listed("user-rose", "Rose", "user");

/** An admin opens the page; the server lists them with two other accounts. */
function asAdmin() {
  const me = signedInAs("admin");
  answer(api.users.listUsers, [
    listed(me._id, me.firstName, "admin"),
    gaston,
    rose,
  ]);
  return me;
}

/** The card titled `title`. */
function card(title: string) {
  return within(
    screen.getByText(title).closest("[data-slot=card]") as HTMLElement,
  );
}

/** The value shown under a label. */
function valueUnder(label: string): string | null {
  const value = screen.getByText(label).nextElementSibling;
  return value === null ? null : value.textContent;
}

type User = Awaited<ReturnType<typeof renderPage>>["user"];

/** The two lists of the role card: whose role, and which role. */
function fields() {
  const [who, role] = card(fr.settings.roleManagement).getAllByRole("combobox");
  return { who, role };
}

async function choose(user: User, field: HTMLElement, option: string | RegExp) {
  await user.click(field);
  await user.click(screen.getByRole("option", { name: option }));
}

const updateButton = { name: fr.settings.updateRole };

describe("settings: who may read them", () => {
  it("shows nothing until it knows who the visitor is", async () => {
    answer(api.users.getCurrentUser, undefined);
    await renderPage(<SettingsPage />);

    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.queryByText(fr.settings.accessDenied)).toBeNull();
  });

  it.each([
    ["a manager", "gestionnaire"],
    ["a patient", "user"],
    ["the admin of a client organisation", "org_admin"],
  ] as const)("refuses %s, and shows no setting", async (_name, role) => {
    signedInAs(role);
    answer(api.users.listUsers, [gaston, rose]);
    await renderPage(<SettingsPage />);

    expect(screen.getByText(fr.settings.accessDenied)).toBeTruthy();
    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("refuses an account without a row", async () => {
    answer(api.users.getCurrentUser, null);
    await renderPage(<SettingsPage />);

    expect(screen.getByText(fr.settings.accessDenied)).toBeTruthy();
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  // What the page does today: the list of accounts is asked for before the
  // role of the visitor is known, and whatever it is.
  it("asks the server for every account even for a visitor it refuses", async () => {
    signedInAs("user");
    await renderPage(<SettingsPage />);

    expect(lastArgsAsked(api.users.listUsers)).toEqual({});
  });
});

describe("settings: what an admin sees", () => {
  it("shows the admin's own account", async () => {
    const me = asAdmin();
    await renderPage(<SettingsPage />);

    expect(
      screen.getByRole("heading", { name: fr.settings.title }),
    ).toBeTruthy();
    const mine = card(fr.settings.yourAccount);
    expect(mine.getByText(`${me.firstName} ${me.lastName}`)).toBeTruthy();
    expect(mine.getByText(me.email)).toBeTruthy();
    expect(mine.getByText(fr.users.roles.admin)).toBeTruthy();
    expect(valueUnder(fr.settings.language)).toBe("fr");
  });

  // What the page does today: these four lines are written in the page, not
  // read from the software. The version is not the site's version (shown in
  // the footer of the public pages), and the delay is the 90 s after which a
  // silent machine is shown offline.
  it("shows a few fixed facts about the application", async () => {
    asAdmin();
    await renderPage(<SettingsPage />);

    expect(valueUnder(fr.settings.application)).toBe("Gaura ECG Monitoring");
    expect(valueUnder(fr.settings.version)).toBe("1.0.0");
    expect(valueUnder(fr.settings.defaultLanguage)).toBe(fr.settings.french);
    expect(valueUnder(fr.settings.supportedLanguages)).toBe(
      fr.settings.languages,
    );
    expect(
      screen.getByText(`${fr.settings.heartbeatTimeout}: 90s`),
    ).toBeTruthy();
  });
});

describe("settings: changing the role of an account", () => {
  it("offers every other account with its current role, and never the admin's own", async () => {
    asAdmin();
    const { user } = await renderPage(<SettingsPage />);

    await user.click(fields().who);

    expect(
      screen.getAllByRole("option").map((option) => option.textContent),
    ).toEqual([
      `Gaston Durand- ${fr.users.roles.gestionnaire}`,
      `Rose Durand- ${fr.users.roles.user}`,
    ]);
  });

  it("offers the three roles", async () => {
    asAdmin();
    const { user } = await renderPage(<SettingsPage />);

    await user.click(fields().role);

    expect(
      screen.getAllByRole("option").map((option) => option.textContent),
    ).toEqual([
      fr.users.roles.admin,
      fr.users.roles.gestionnaire,
      fr.users.roles.user,
    ]);
  });

  it("sends nothing until an account and a role are both chosen", async () => {
    asAdmin();
    const { user } = await renderPage(<SettingsPage />);
    const update = screen.getByRole("button", updateButton);

    expect(update.hasAttribute("disabled")).toBe(true);
    await choose(user, fields().who, /Rose Durand/);
    expect(update.hasAttribute("disabled")).toBe(true);
    await user.click(update);
    expect(mutationsSent()).toEqual([]);

    await choose(user, fields().role, fr.users.roles.gestionnaire);
    expect(update.hasAttribute("disabled")).toBe(false);
  });

  it("sends the role chosen for the account chosen, says so and empties the form", async () => {
    asAdmin();
    const { user } = await renderPage(<SettingsPage />);

    await choose(user, fields().who, /Rose Durand/);
    await choose(user, fields().role, fr.users.roles.gestionnaire);
    await user.click(screen.getByRole("button", updateButton));

    expect(mutationsSent()).toEqual([
      {
        name: "users:updateUserRole",
        args: { userId: "user-rose", role: "gestionnaire" },
      },
    ]);
    expect(feedbackShown()).toEqual([
      { kind: "success", message: fr.feedback.roleUpdated },
    ]);
    expect(fields().who.textContent).toBe(fr.settings.selectUserPlaceholder);
    expect(fields().role.textContent).toBe(fr.settings.selectRolePlaceholder);
    expect(
      screen.getByRole("button", updateButton).hasAttribute("disabled"),
    ).toBe(true);
  });

  it("keeps the form as it was and shows the server's refusal when the change fails", async () => {
    asAdmin();
    // What the server answers once the roles are held by Clerk Organizations.
    mutation(api.users.updateUserRole).mockRejectedValue(
      new ConvexError("Roles are managed in Clerk Organizations"),
    );
    const { user } = await renderPage(<SettingsPage />);

    await choose(user, fields().who, /Gaston Durand/);
    await choose(user, fields().role, fr.users.roles.admin);
    await user.click(screen.getByRole("button", updateButton));

    expect(feedbackShown()).toEqual([
      { kind: "error", message: "Roles are managed in Clerk Organizations" },
    ]);
    expect(takeLoggedFailures()).toEqual(["users:updateUserRole"]);
    expect(fields().who.textContent).toContain("Gaston Durand");
    expect(fields().role.textContent).toBe(fr.users.roles.admin);
    expect(
      screen.getByRole("button", updateButton).hasAttribute("disabled"),
    ).toBe(false);
  });

  it("cannot be sent twice while the server has not answered", async () => {
    asAdmin();
    // The server answers only when the test says so.
    const server = Promise.withResolvers<null>();
    mutation(api.users.updateUserRole).mockReturnValue(server.promise);
    const { user } = await renderPage(<SettingsPage />);

    await choose(user, fields().who, /Rose Durand/);
    await choose(user, fields().role, fr.users.roles.admin);
    await user.click(screen.getByRole("button", updateButton));

    const waiting = screen.getByRole("button", { name: fr.common.loading });
    expect(waiting.hasAttribute("disabled")).toBe(true);
    await user.click(waiting);
    expect(mutationsSent()).toHaveLength(1);

    await act(async () => server.resolve(null));
    expect(screen.getByRole("button", updateButton)).toBeTruthy();
  });

  it("offers no account while their list is loading", async () => {
    signedInAs("admin");
    answer(api.users.listUsers, undefined);
    const { user } = await renderPage(<SettingsPage />);

    await user.click(fields().who);

    expect(screen.queryAllByRole("option")).toEqual([]);
  });
});

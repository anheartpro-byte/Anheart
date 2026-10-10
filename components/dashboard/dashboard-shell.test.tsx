import { messages, render, type Screen } from "@/test-support/render";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { answer, resetConvex } from "@/test-support/convex";
import { AppSidebar } from "./AppSidebar";
import { DashboardBreadcrumb } from "./DashboardBreadcrumb";
import { Header } from "./Header";
import {
  hasOwnMessage,
  useCatalogLabel,
  useMachineStatusLabel,
  useSessionStatusLabel,
} from "./statusLabels";

/**
 * The frame of the dashboard: the menu each role is given, the entry shown as
 * the current page, the breadcrumb of a page, and the words shown for a
 * status the server sends.
 *
 * The menu is a convenience, not a control: the server refuses what a role
 * may not read whatever the menu shows. What is proven here is that a patient
 * is not shown the entries of a manager, nor a manager those of an
 * administrator.
 */

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/convex")).convexReact,
);
const here = vi.hoisted(() => ({ pathname: "/dashboard" }));
vi.mock("@/i18n/navigation", () => ({
  usePathname: () => here.pathname,
  Link: ({ href, children }: { href: string; children: ReactNode }) => (
    <a href={href}>{children}</a>
  ),
}));
vi.mock("next/image", () => ({
  default: ({ src, alt }: { src: string; alt: string }) => (
    // eslint-disable-next-line @next/next/no-img-element
    <img src={src} alt={alt} />
  ),
}));
// The real sidebar needs its provider and a browser (it has its own tests in
// components/ui): each part is drawn as a plain block that carries what the
// menu gave it.
vi.mock("@/components/ui/sidebar", () => {
  type Part = { children?: ReactNode };
  const block = ({ children }: Part) => <div>{children}</div>;
  return {
    Sidebar: ({ children, collapsible }: Part & { collapsible?: string }) => (
      <nav data-collapsible={collapsible}>{children}</nav>
    ),
    SidebarContent: block,
    SidebarFooter: ({ children }: Part) => <footer>{children}</footer>,
    SidebarGroup: ({ children }: Part) => <section>{children}</section>,
    SidebarGroupContent: block,
    SidebarGroupLabel: ({ children }: Part) => <h3>{children}</h3>,
    SidebarHeader: block,
    SidebarMenu: block,
    SidebarMenuItem: block,
    SidebarMenuButton: ({
      children,
      isActive,
      tooltip,
    }: Part & { isActive?: boolean; tooltip?: string }) => (
      <div data-entry="" data-active={isActive === true} data-tooltip={tooltip}>
        {children}
      </div>
    ),
    SidebarRail: () => null,
    SidebarSeparator: () => <hr />,
    SidebarTrigger: ({ className }: { className?: string }) => (
      <button type="button" aria-label="sidebar" className={className} />
    ),
  };
});
vi.mock("@/components/ui/avatar", () => ({
  Avatar: ({ children }: { children?: ReactNode }) => <span>{children}</span>,
  AvatarFallback: ({ children }: { children?: ReactNode }) => (
    <span data-initials="">{children}</span>
  ),
}));
// The three controls of the header have their own tests: the header only has to hold them.
vi.mock("@clerk/nextjs", () => ({
  UserButton: (props: { afterSwitchSessionUrl?: string }) => (
    <div data-user-button="" data-after-switch={props.afterSwitchSessionUrl} />
  ),
}));
vi.mock("@/components/LanguageSwitcher", () => ({
  LanguageSwitcher: () => <div data-language-switcher="" />,
}));
vi.mock("@/components/ThemeToggle", () => ({
  ThemeToggle: () => <div data-theme-toggle="" />,
}));

const fr = messages.fr;
const nav = fr.nav;

function signedInAs(role: string | undefined) {
  answer(
    "users:getCurrentUser",
    role === undefined
      ? undefined
      : {
          role,
          firstName: "Ada",
          lastName: "Lovelace",
          email: "ada@anheart.test",
        },
  );
}

/** The entries of the menu as they read, with the page each leads to. */
const entries = (screen: Screen) =>
  screen
    .all(
      (element) =>
        element.hasAttribute("data-entry") &&
        element.hasAttribute("data-tooltip"),
    )
    .map((entry) => [
      screen.textOf(entry),
      entry.children[0]?.getAttribute("href") ?? null,
    ]);

const sections = (screen: Screen) =>
  screen.tag("h3").map((title) => screen.textOf(title));

const HOME = [nav.dashboard, "/dashboard"];
const MANAGER_ENTRIES = [
  [nav.patients, "/dashboard/patients"],
  [nav.machines, "/dashboard/machines"],
  [nav.myMachines, "/dashboard/my-machines"],
  [nav.sessions, "/dashboard/sessions"],
  [nav.reports, "/dashboard/reports"],
];
const ADMIN_ENTRIES = [
  [nav.gestionnaires, "/dashboard/gestionnaires"],
  [nav.users, "/dashboard/users"],
  [nav.settings, "/dashboard/settings"],
];
const PATIENT_ENTRIES = [
  [nav.myMachines, "/dashboard/my-machines"],
  [nav.sessions, "/dashboard/sessions"],
  [nav.reports, "/dashboard/reports"],
];

beforeEach(() => {
  resetConvex();
  here.pathname = "/dashboard";
});

describe("ANH-203 dashboard menu: what each role is shown", () => {
  it("a patient is shown his machines, his sessions and his reports, and nothing of the managers", () => {
    signedInAs("user");

    const screen = render(<AppSidebar />);

    expect(sections(screen)).toEqual([fr.sidebar.main, fr.sidebar.myHealth]);
    expect(entries(screen)).toEqual([HOME, ...PATIENT_ENTRIES]);
  });

  it("a gestionnaire is shown the management entries, and nothing of the administration", () => {
    signedInAs("gestionnaire");

    const screen = render(<AppSidebar />);

    expect(sections(screen)).toEqual([fr.sidebar.main, fr.sidebar.management]);
    expect(entries(screen)).toEqual([HOME, ...MANAGER_ENTRIES]);
  });

  it("an administrator is shown the administration and the management entries", () => {
    signedInAs("admin");

    const screen = render(<AppSidebar />);

    expect(sections(screen)).toEqual([
      fr.sidebar.main,
      fr.sidebar.administration,
      fr.sidebar.management,
    ]);
    expect(entries(screen)).toEqual([
      HOME,
      ...ADMIN_ENTRIES,
      ...MANAGER_ENTRIES,
    ]);
  });

  it.each([
    ["someone whose account is still loading", undefined],
    ["an organisation administrator (what the menu does today)", "org_admin"],
    ["a role this version of the site does not know", "auditor"],
  ])("%s is shown the home entry only", (_who, role) => {
    signedInAs(role);

    const screen = render(<AppSidebar />);

    expect(sections(screen)).toEqual([fr.sidebar.main]);
    expect(entries(screen)).toEqual([HOME]);
  });

  it("shows who is signed in at the foot of the menu: initials, name and email", () => {
    signedInAs("user");

    const screen = render(<AppSidebar />);

    const [foot] = screen.tag("footer");
    expect(screen.textOf(foot)).toBe("AL Ada Lovelace ada@anheart.test");
  });
});

describe("ANH-203 dashboard menu: the current page", () => {
  const active = (screen: Screen) =>
    screen
      .all((element) => element.getAttribute("data-active") === "true")
      .map((entry) => screen.textOf(entry));

  it("on the home page, only the home entry is the current one", () => {
    signedInAs("admin");
    here.pathname = "/dashboard";

    expect(active(render(<AppSidebar />))).toEqual([nav.dashboard]);
  });

  it("on a page of a section, that section's entry is the current one and home is not", () => {
    signedInAs("admin");
    here.pathname = "/dashboard/machines";

    expect(active(render(<AppSidebar />))).toEqual([nav.machines]);
  });

  it("on a page below a section (a session watched live), the section stays the current one", () => {
    signedInAs("user");
    here.pathname = "/dashboard/sessions/k57abc/live";

    expect(active(render(<AppSidebar />))).toEqual([nav.sessions]);
  });

  it("gives each entry its own name as the hint shown when the menu is folded", () => {
    signedInAs("user");

    const screen = render(<AppSidebar />);

    const hints = screen
      .all((element) => element.hasAttribute("data-tooltip"))
      .map((entry) => [
        entry.getAttribute("data-tooltip"),
        screen.textOf(entry),
      ]);
    expect(hints.length).toBe(4);
    for (const [hint, label] of hints) expect(hint).toBe(label);
  });
});

describe("ANH-203 breadcrumb of a dashboard page", () => {
  /** The crumbs as they read, each with the page it leads to (none for the current page). */
  function crumbs(pathname: string) {
    here.pathname = pathname;
    const screen = render(<DashboardBreadcrumb />);
    return screen
      .tag("li")
      .filter((item) => item.getAttribute("data-slot") === "breadcrumb-item")
      .map((item) => [
        screen.textOf(item),
        item.children[0]?.getAttribute("href") ?? null,
      ]);
  }

  it("shows nothing on the home page of the dashboard", () => {
    here.pathname = "/dashboard";

    expect(render(<DashboardBreadcrumb />).container.childNodes).toEqual([]);
  });

  it("names each level, links every level but the current page", () => {
    expect(crumbs("/dashboard/machines")).toEqual([
      [nav.dashboard, "/dashboard"],
      [nav.machines, null],
    ]);
  });

  it("writes « Détails » for an identifier and « En direct » for the live view", () => {
    expect(crumbs("/dashboard/sessions/k57e3m9w2x8v1q4t/live")).toEqual([
      [nav.dashboard, "/dashboard"],
      [nav.sessions, "/dashboard/sessions"],
      [fr.breadcrumb.details, "/dashboard/sessions/k57e3m9w2x8v1q4t"],
      [fr.breadcrumb.live, null],
    ]);
  });

  it("a known page with a long name is named, not taken for an identifier", () => {
    expect(crumbs("/dashboard/gestionnaires").at(-1)).toEqual([
      nav.gestionnaires,
      null,
    ]);
    expect(crumbs("/dashboard/my-machines").at(-1)).toEqual([
      nav.myMachines,
      null,
    ]);
  });

  it.each([
    ["new", fr.breadcrumb.new],
    ["edit", fr.breadcrumb.edit],
    ["patients", nav.patients],
    ["users", nav.users],
    ["settings", nav.settings],
    ["reports", nav.reports],
  ])("the level « %s » reads « %s »", (segment, label) => {
    expect(crumbs(`/dashboard/${segment}`).at(-1)).toEqual([label, null]);
  });

  it("a short level it does not know is shown as written", () => {
    expect(crumbs("/dashboard/beta").at(-1)).toEqual(["beta", null]);
  });

  it("puts a separator between two levels, none after the last", () => {
    here.pathname = "/dashboard/machines/k57e3m9w2x8v1q4t";

    const screen = render(<DashboardBreadcrumb />);

    const separators = screen.all(
      (element) => element.getAttribute("data-slot") === "breadcrumb-separator",
    );
    expect(separators).toHaveLength(2);
    expect(screen.tag("li").at(-1)?.getAttribute("data-slot")).toBe(
      "breadcrumb-item",
    );
  });
});

describe("ANH-203 header of the dashboard", () => {
  it("holds the menu button, the breadcrumb of the page, the theme, the language and the account", () => {
    here.pathname = "/dashboard/reports";

    const screen = render(<Header />);

    const has = (attribute: string) =>
      screen.all((element) => element.hasAttribute(attribute)).length;
    expect(
      screen.all((element) => element.getAttribute("aria-label") === "sidebar"),
    ).toHaveLength(1);
    expect(screen.text()).toContain(`${nav.dashboard} ${nav.reports}`);
    expect([
      has("data-theme-toggle"),
      has("data-language-switcher"),
      has("data-user-button"),
    ]).toEqual([1, 1, 1]);
    // After a change of account the visitor lands on the home page, not on a page of the other account.
    expect(
      screen
        .all((element) => element.hasAttribute("data-user-button"))[0]
        .getAttribute("data-after-switch"),
    ).toBe("/");
  });
});

describe("ANH-203 words shown for a status", () => {
  /** Calls a hook of labels and writes what it answers for each value. */
  function Labels({
    use,
    values,
  }: {
    use: () => (value: string) => string;
    values: string[];
  }) {
    const label = use();
    return <p>{values.map(label).join(" | ")}</p>;
  }

  it("a session status the catalog knows is translated, in the language of the page", () => {
    const values = ["active", "completed", "pending", "failed"];

    const french = render(
      <Labels use={useSessionStatusLabel} values={values} />,
    );
    expect(french.text()).toBe("Active | Terminée | En attente | Échouée");
    french.unmount();

    const english = render(
      <Labels use={useSessionStatusLabel} values={["completed"]} />,
      { locale: "en" },
    );
    expect(english.text()).toBe(messages.en.sessions.status.completed);
  });

  it("a status the catalog does not know is shown as the server sent it, never hidden", () => {
    const values = ["cancelled", "constructor", "toString", ""];

    expect(
      render(<Labels use={useSessionStatusLabel} values={values} />).text(),
    ).toBe("cancelled | constructor | toString |");
  });

  it("a machine status is translated, and an unknown one shown as received", () => {
    const values = ["online", "offline", "in_session", "maintenance"];

    expect(
      render(<Labels use={useMachineStatusLabel} values={values} />).text(),
    ).toBe(
      `${fr.machines.online} | ${fr.machines.offline} | ${fr.machines.inSession} | maintenance`,
    );
  });

  it("a label is looked up under the part of the catalog it is asked from", () => {
    const useKind = () => useCatalogLabel("training.kind");

    expect(
      render(<Labels use={useKind} values={["auto", "status"]} />).text(),
    ).toBe(`${fr.training.kind.auto} | status`);
  });

  it("only a text reached through the catalog's own keys counts as a message", () => {
    const catalog = { sessions: { status: { active: "Active" } }, count: 3 };

    expect(hasOwnMessage(catalog, ["sessions", "status", "active"])).toBe(true);
    // A group is not a message, nor is a number, nor what an object inherits.
    expect(hasOwnMessage(catalog, ["sessions", "status"])).toBe(false);
    expect(hasOwnMessage(catalog, ["count"])).toBe(false);
    expect(hasOwnMessage(catalog, ["sessions", "constructor"])).toBe(false);
    expect(
      hasOwnMessage(catalog, ["sessions", "status", "active", "length"]),
    ).toBe(false);
    // A text the catalog only inherits is not one of its messages.
    const inheriting = Object.create({ inherited: "Texte" }) as object;
    expect(hasOwnMessage(inheriting, ["inherited"])).toBe(false);
    expect(hasOwnMessage(null, ["sessions"])).toBe(false);
    expect(hasOwnMessage(undefined, [])).toBe(false);
  });
});

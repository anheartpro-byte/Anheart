import {
  click,
  fireWindow,
  messages,
  render,
  type Screen,
} from "@/test-support/render";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Footer } from "./Footer";
import { Navbar } from "./Navbar";

/**
 * The bar at the top of the public pages and their footer: where each link
 * leads, what a visitor who is signed in or not is offered, the menu of a
 * narrow screen, and the version of the site written at the bottom.
 */

const visitor = vi.hoisted(() => ({ signedIn: false }));
vi.mock("@clerk/nextjs", () => {
  type Part = { children?: ReactNode; mode?: string };
  return {
    SignedIn: ({ children }: Part) => (visitor.signedIn ? children : null),
    SignedOut: ({ children }: Part) => (visitor.signedIn ? null : children),
    SignInButton: ({ children, mode }: Part) => (
      <span data-sign-in={mode}>{children}</span>
    ),
    SignUpButton: ({ children, mode }: Part) => (
      <span data-sign-up={mode}>{children}</span>
    ),
    UserButton: (props: { afterSwitchSessionUrl?: string }) => (
      <div
        data-user-button=""
        data-after-switch={props.afterSwitchSessionUrl}
      />
    ),
  };
});
vi.mock("@/i18n/navigation", () => ({
  Link: ({
    href,
    children,
    onClick,
    className,
  }: {
    href: string;
    children: ReactNode;
    onClick?: () => void;
    className?: string;
  }) => (
    <a href={href} onClick={onClick} className={className}>
      {children}
    </a>
  ),
}));
vi.mock("next/image", () => ({
  default: ({ src, alt }: { src: string; alt: string }) => (
    // eslint-disable-next-line @next/next/no-img-element
    <img src={src} alt={alt} />
  ),
}));
vi.mock("@/components/LanguageSwitcher", () => ({
  LanguageSwitcher: () => <div data-language-switcher="" />,
}));
vi.mock("@/components/ThemeToggle", () => ({
  ThemeToggle: () => <div data-theme-toggle="" />,
}));
vi.mock("@/lib/version", () => ({ WEB_VERSION: "web-9.8.7" }));

const fr = messages.fr;
const nav = fr.nav;
const globals = globalThis as Record<string, unknown>;

/** The links that read `label`, as the pages they lead to. */
const hrefs = (screen: Screen, label: string) =>
  screen
    .tag("a")
    .filter((link) => screen.textOf(link) === label)
    .map((link) => link.getAttribute("href"));

beforeEach(() => {
  visitor.signedIn = false;
  globals.scrollY = 0;
});
afterEach(() => {
  delete globals.scrollY;
  vi.useRealTimers();
});

describe("ANH-203 top bar of the public pages: links", () => {
  it("leads to each part of the home page and to the questions, in the bar and in the narrow-screen menu", () => {
    const screen = render(<Navbar />);

    for (const [label, href] of [
      [nav.solution, "/#solution"],
      [nav.technology, "/#usecases"],
      [nav.benefits, "/#benefits"],
      [nav.markets, "/#markets"],
      [nav.faq, "/faq"],
    ]) {
      expect(hrefs(screen, label)).toEqual([href, href]);
    }
    expect(hrefs(screen, "Gaura")).toEqual(["/"]);
    expect(screen.tag("img").map((image) => image.getAttribute("alt"))).toEqual(
      ["Gaura logo"],
    );
  });

  it("offers a visitor who is not signed in to sign in or to sign up, in a window, and no dashboard", () => {
    const screen = render(<Navbar />);

    expect(
      screen.all((element) => element.getAttribute("data-sign-in") === "modal"),
    ).toHaveLength(2);
    expect(
      screen.all((element) => element.getAttribute("data-sign-up") === "modal"),
    ).toHaveLength(2);
    expect(screen.text()).toContain(fr.auth.signIn);
    expect(screen.text()).toContain(fr.auth.signUp);
    expect(hrefs(screen, nav.dashboard)).toEqual([]);
    expect(
      screen.all((element) => element.hasAttribute("data-user-button")),
    ).toEqual([]);
  });

  it("offers a signed-in visitor his dashboard and his account, and no sign-in", () => {
    visitor.signedIn = true;

    const screen = render(<Navbar />);

    expect(hrefs(screen, nav.dashboard)).toEqual(["/dashboard", "/dashboard"]);
    const [account] = screen.all((element) =>
      element.hasAttribute("data-user-button"),
    );
    expect(account.getAttribute("data-after-switch")).toBe("/");
    expect(screen.text()).not.toContain(fr.auth.signIn);
    expect(screen.text()).not.toContain(fr.auth.signUp);
  });
});

describe("ANH-203 top bar of the public pages: the menu of a narrow screen", () => {
  /** The menu is the last block of the header: folded to no height until opened. */
  const menu = (screen: Screen) => screen.tag("header")[0].children.at(-1)!;
  /** The only button of the bar that has no text: the one that opens the menu. */
  const toggle = (screen: Screen) => screen.button("");

  it("is folded at first, opens on the menu button and folds on the same button", async () => {
    const screen = render(<Navbar />);
    expect(menu(screen).className).toContain("max-h-0");

    await click(toggle(screen));
    expect(menu(screen).className).toContain("max-h-80");
    expect(menu(screen).className).not.toContain("max-h-0");

    await click(toggle(screen));
    expect(menu(screen).className).toContain("max-h-0");
  });

  it("folds again once a link of the menu is followed", async () => {
    const screen = render(<Navbar />);
    await click(toggle(screen));

    // The second link of that name is the one of the menu.
    await click(
      screen.tag("a").filter((link) => screen.textOf(link) === nav.faq)[1],
    );

    expect(menu(screen).className).toContain("max-h-0");
  });

  it.each([
    ["sign in", fr.auth.signIn],
    ["sign up", fr.auth.signUp],
  ])(
    "folds again when a visitor chooses to %s from it",
    async (_what, label) => {
      const screen = render(<Navbar />);
      await click(toggle(screen));

      // The button of the bar comes first, the plain one of the menu second.
      await click(
        screen.all(
          (element) =>
            element.localName === "button" && screen.textOf(element) === label,
        )[1],
      );

      expect(menu(screen).className).toContain("max-h-0");
    },
  );

  it("folds again when a signed-in visitor goes to his dashboard from it", async () => {
    visitor.signedIn = true;
    const screen = render(<Navbar />);
    await click(toggle(screen));

    await click(
      screen
        .tag("a")
        .filter((link) => screen.textOf(link) === nav.dashboard)[1],
    );

    expect(menu(screen).className).toContain("max-h-0");
  });
});

describe("ANH-203 top bar of the public pages: scrolling", () => {
  const bar = (screen: Screen) => screen.tag("header")[0];

  it("is transparent at the top of the page and gets a background once the page is scrolled", async () => {
    const screen = render(<Navbar />);
    expect(bar(screen).className).toContain("bg-transparent");

    globals.scrollY = 11;
    await fireWindow("scroll");
    expect(bar(screen).className).toContain("bg-background/80");
    expect(bar(screen).className).not.toContain("bg-transparent");

    // Ten pixels or less is still the top of the page.
    globals.scrollY = 10;
    await fireWindow("scroll");
    expect(bar(screen).className).toContain("bg-transparent");
  });

  it("stops listening to the page once it is gone", async () => {
    const added = vi.spyOn(globalThis, "addEventListener");
    const removed = vi.spyOn(globalThis, "removeEventListener");
    const screen = render(<Navbar />);
    const [type, listener] =
      added.mock.calls.find(([name]) => name === "scroll") ?? [];
    expect(type).toBe("scroll");

    screen.unmount();

    expect(
      removed.mock.calls.some(
        ([name, gone]) => name === "scroll" && gone === listener,
      ),
    ).toBe(true);
    added.mockRestore();
    removed.mockRestore();
  });
});

describe("ANH-203 footer of the public pages", () => {
  it("leads to the parts of the home page, the questions, the dashboard and the legal pages", () => {
    const screen = render(<Footer />);

    const links = Object.fromEntries(
      screen
        .tag("a")
        .map((link) => [screen.textOf(link), link.getAttribute("href")]),
    );
    expect(links).toEqual({
      [nav.solution]: "/#solution",
      [nav.technology]: "/#technology",
      [nav.benefits]: "/#benefits",
      [nav.markets]: "/#markets",
      [nav.faq]: "/faq",
      [nav.dashboard]: "/dashboard",
      "contact@gauratechnologies.com": "mailto:contact@gauratechnologies.com",
      [fr.footer.privacy]: "/privacy",
      [fr.footer.terms]: "/terms",
    });
  });

  it("writes the version of the site that is running, and the current year", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2031-05-04T10:00:00Z"));

    const screen = render(<Footer />);

    expect(screen.reading("web-9.8.7")).toHaveLength(1);
    expect(screen.text()).toContain(
      `© 2031 Gaura. ${fr.footer.allRightsReserved}`,
    );
  });

  it("is written in the language of the page", () => {
    const screen = render(<Footer />, { locale: "en" });

    expect(screen.text()).toContain(messages.en.footer.description);
    expect(screen.text()).not.toContain(fr.footer.description);
  });
});

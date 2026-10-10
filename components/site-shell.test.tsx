import { click, messages, render, settle } from "@/test-support/render";
import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  dismissFeedback,
  getFeedbackToasts,
  pushFeedback,
} from "@/lib/feedback";
import { FeedbackToaster } from "./FeedbackToaster";
import { LanguageSwitcher } from "./LanguageSwitcher";
import { ThemeProvider } from "./ThemeProvider";
import { ThemeToggle } from "./ThemeToggle";

/**
 * What every page of the site carries: the messages shown after an action,
 * the choice of the language, the theme.
 */

const router = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("@/i18n/navigation", () => ({
  useRouter: () => router,
  usePathname: () => "/dashboard/machines",
}));
// The real menu opens in a layer that needs a browser (it has its own tests in
// components/ui): its entries are drawn as plain buttons, always visible.
vi.mock("@/components/ui/dropdown-menu", () => {
  type Part = { children?: ReactNode };
  return {
    DropdownMenu: ({ children }: Part) => <div>{children}</div>,
    DropdownMenuTrigger: ({ children }: Part) => (
      <div data-trigger="">{children}</div>
    ),
    DropdownMenuContent: ({ children, align }: Part & { align?: string }) => (
      <div role="menu" data-align={align}>
        {children}
      </div>
    ),
    DropdownMenuItem: ({
      children,
      onClick,
      className,
    }: Part & { onClick?: () => void; className?: string }) => (
      <button
        type="button"
        role="menuitem"
        className={className}
        onClick={onClick}
      >
        {children}
      </button>
    ),
  };
});
const themes = vi.hoisted(() => ({ given: [] as Record<string, unknown>[] }));
vi.mock("next-themes", () => ({
  ThemeProvider: ({ children, ...props }: { children?: ReactNode }) => {
    themes.given.push(props);
    return <div data-themes="">{children}</div>;
  },
}));
vi.mock("@/components/ui/animated-theme-toggler", () => ({
  AnimatedThemeToggler: ({ className }: { className?: string }) => (
    <button type="button" data-toggler="" className={className} />
  ),
}));

const fr = messages.fr;

beforeEach(() => {
  vi.useFakeTimers();
  router.replace.mockReset();
  themes.given.length = 0;
});
afterEach(() => {
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
  vi.useRealTimers();
});

describe("ANH-203 messages shown after an action", () => {
  /** Each message on screen: how it is announced, and what it says. */
  const shown = (screen: ReturnType<typeof render>) =>
    screen
      .all((element) =>
        ["alert", "status"].includes(element.getAttribute("role") ?? ""),
      )
      .map((message) => [message.getAttribute("role"), screen.textOf(message)]);

  it("shows nothing before any action", () => {
    const screen = render(<FeedbackToaster />);

    expect(shown(screen)).toEqual([]);
    expect(screen.container.children[0].getAttribute("aria-live")).toBe(
      "polite",
    );
  });

  it("shows a message the moment an action reports it, without the page being drawn again", async () => {
    const screen = render(<FeedbackToaster />);

    await settle(() => pushFeedback("success", "Machine créée avec succès"));

    expect(shown(screen)).toEqual([["status", "Machine créée avec succès"]]);
  });

  it("announces a failure as an alert, in red, and a success as a status, in green", async () => {
    const screen = render(<FeedbackToaster />);

    await settle(() => {
      pushFeedback("success", "Fait");
      pushFeedback("error", "Machine is offline");
    });

    expect(shown(screen)).toEqual([
      ["status", "Fait"],
      ["alert", "Machine is offline"],
    ]);
    const [success, failure] = screen.all((element) =>
      element.hasAttribute("role"),
    );
    expect(success.className).toContain("text-green-700");
    expect(failure.className).toContain("text-destructive");
    expect(success.className).not.toContain("text-destructive");
  });

  it("closes the message whose cross is pressed, and leaves the others", async () => {
    const screen = render(<FeedbackToaster />);
    await settle(() => {
      pushFeedback("success", "Premier");
      pushFeedback("error", "Second");
    });
    const crosses = screen.all(
      (element) =>
        element.localName === "button" &&
        element.getAttribute("aria-label") === fr.common.close,
    );
    expect(crosses).toHaveLength(2);

    await click(crosses[0]);

    expect(shown(screen)).toEqual([["alert", "Second"]]);
  });

  it("lets a message leave by itself, a failure staying longer than a success", async () => {
    const screen = render(<FeedbackToaster />);
    await settle(() => {
      pushFeedback("success", "Fait");
      pushFeedback("error", "Refusé");
    });

    await settle(() => vi.advanceTimersByTime(5_000));
    expect(shown(screen)).toEqual([["alert", "Refusé"]]);
    await settle(() => vi.advanceTimersByTime(7_000));
    expect(shown(screen)).toEqual([]);
  });

  it("is drawn empty by the server, whatever was reported there: a message belongs to one visitor's page", () => {
    pushFeedback("error", "Refus d'une autre requête");

    const html = renderToStaticMarkup(
      <NextIntlClientProvider locale="fr" messages={fr} timeZone="Europe/Paris">
        <FeedbackToaster />
      </NextIntlClientProvider>,
    );

    expect(html).not.toContain("Refus");
    expect(html).not.toContain("role=");
    expect(html).toContain('aria-live="polite"');
  });

  it("names its close button in the language of the page", async () => {
    const screen = render(<FeedbackToaster />, { locale: "en" });
    await settle(() => pushFeedback("success", "Done"));

    expect(screen.tag("button")[0].getAttribute("aria-label")).toBe(
      messages.en.common.close,
    );
  });
});

describe("ANH-203 choice of the language", () => {
  const items = (screen: ReturnType<typeof render>) =>
    screen.all((element) => element.getAttribute("role") === "menuitem");

  it("shows the current language on its button and offers French and English", () => {
    const screen = render(<LanguageSwitcher />);

    const [trigger] = screen.all((element) =>
      element.hasAttribute("data-trigger"),
    );
    expect(screen.textOf(trigger)).toBe("Francais");
    expect(items(screen).map((item) => screen.textOf(item))).toEqual([
      "Francais",
      "English",
    ]);
  });

  it("marks the current language among the two, and only that one", () => {
    const french = render(<LanguageSwitcher />);
    expect(
      items(french).map((item) => item.className.includes("bg-accent")),
    ).toEqual([true, false]);
    french.unmount();

    const english = render(<LanguageSwitcher />, { locale: "en" });
    expect(
      items(english).map((item) => item.className.includes("bg-accent")),
    ).toEqual([false, true]);
    expect(
      english.textOf(
        english.all((element) => element.hasAttribute("data-trigger"))[0],
      ),
    ).toBe("English");
  });

  it("opens the same page in the language chosen", async () => {
    const screen = render(<LanguageSwitcher />);

    await click(items(screen)[1]);

    expect(router.replace.mock.calls).toEqual([
      ["/dashboard/machines", { locale: "en" }],
    ]);
  });

  it("opens its menu from the right edge of its button", () => {
    const screen = render(<LanguageSwitcher />);

    expect(
      screen
        .all((element) => element.getAttribute("role") === "menu")[0]
        .getAttribute("data-align"),
    ).toBe("end");
  });
});

describe("ANH-203 theme", () => {
  it("hands the theme library the settings the page chose, and draws the page inside it", () => {
    const screen = render(
      <ThemeProvider attribute="class" defaultTheme="system" enableSystem>
        <p>Contenu de la page</p>
      </ThemeProvider>,
    );

    expect(themes.given).toEqual([
      { attribute: "class", defaultTheme: "system", enableSystem: true },
    ]);
    expect(
      screen.textOf(
        screen.all((element) => element.hasAttribute("data-themes"))[0],
      ),
    ).toBe("Contenu de la page");
  });

  it("draws the theme button as a square of 40 pixels unless the page asks for another size", () => {
    const usual = render(<ThemeToggle />);
    const [button] = usual.all((element) =>
      element.hasAttribute("data-toggler"),
    );
    expect(button.className).toContain("h-10 w-10");
    usual.unmount();

    const smaller = render(<ThemeToggle className="h-8 w-8" />);
    const [small] = smaller.all((element) =>
      element.hasAttribute("data-toggler"),
    );
    // The size asked for replaces the usual one: both would fight on screen.
    expect(small.className).toContain("h-8 w-8");
    expect(small.className).not.toContain("h-10");
    expect(small.className).toContain("hover:bg-accent");
  });
});

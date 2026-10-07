import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { enUS, frFR } from "@clerk/localizations";
import { beforeEach, describe, expect, it, vi } from "vitest";
import fr from "@/messages/fr.json";
import LocaleLayout, { generateStaticParams, metadata } from "./layout";

/**
 * The root layout of every page. It is a server component: an async function
 * that Next.js calls on the server and whose result it streams. It is called
 * here as the plain function it is, and what it returns is drawn to markup.
 * The providers it nests are replaced by markers that keep what they were
 * given: each has its own job (Clerk, Convex, the theme, the messages) that
 * needs a server or a browser. This file runs without jsdom.
 */

const seen = vi.hoisted(() => ({
  clerk: [] as { localization: unknown; dynamic: unknown }[],
  messages: [] as unknown[],
  theme: [] as Record<string, unknown>[],
  asked: 0,
}));

// The style sheet goes through the site's PostCSS chain, which only the
// build of the site knows how to run: nothing of it is read here.
vi.mock("../globals.css", () => ({}));
vi.mock("next/font/google", () => ({
  Geist: () => ({ variable: "font-sans-variable" }),
  Geist_Mono: () => ({ variable: "font-mono-variable" }),
}));
vi.mock("next/navigation", () => ({
  // As in Next.js: it does not return, it throws what the framework catches.
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));
vi.mock("next-intl/server", () => ({
  getMessages: async () => {
    seen.asked += 1;
    return fr;
  },
}));
vi.mock("next-intl", () => ({
  NextIntlClientProvider: (props: {
    messages: unknown;
    children?: ReactNode;
  }) => {
    seen.messages.push(props.messages);
    return <div data-provider="messages">{props.children}</div>;
  },
}));
vi.mock("@clerk/nextjs", () => ({
  ClerkProvider: (props: {
    localization: unknown;
    dynamic?: boolean;
    children?: ReactNode;
  }) => {
    seen.clerk.push({
      localization: props.localization,
      dynamic: props.dynamic,
    });
    return <div data-provider="clerk">{props.children}</div>;
  },
}));
vi.mock("@/components/ConvexClientProvider", () => ({
  default: ({ children }: { children?: ReactNode }) => (
    <div data-provider="convex">{children}</div>
  ),
}));
vi.mock("@/components/ThemeProvider", () => ({
  ThemeProvider: ({
    children,
    ...settings
  }: {
    children?: ReactNode;
  } & Record<string, unknown>) => {
    seen.theme.push(settings);
    return <div data-provider="theme">{children}</div>;
  },
}));
vi.mock("@/components/FeedbackToaster", () => ({
  FeedbackToaster: () => <output data-toaster="" />,
}));

/** What the layout draws around a page, for the locale of the address. */
async function draw(locale: string): Promise<string> {
  return renderToStaticMarkup(
    await LocaleLayout({
      children: <p>La page</p>,
      params: Promise.resolve({ locale }),
    }),
  );
}

beforeEach(() => {
  seen.clerk.length = 0;
  seen.messages.length = 0;
  seen.theme.length = 0;
  seen.asked = 0;
});

describe("root layout: one document per locale", () => {
  it("is built ahead for the two locales of the site", () => {
    expect(generateStaticParams()).toEqual([
      { locale: "fr" },
      { locale: "en" },
    ]);
  });

  it("names the site and its icon", () => {
    expect(metadata).toEqual({
      title: "Gaura",
      icons: { icon: "/image0.ico" },
    });
  });

  it.each(["fr", "en"])(
    "declares the language of the address (%s) on the document",
    async (locale) => {
      const html = await draw(locale);

      expect(html.startsWith(`<html lang="${locale}">`)).toBe(true);
      expect(html).toContain(
        '<body class="font-sans-variable font-mono-variable antialiased">',
      );
    },
  );

  it("refuses an address whose locale the site does not have, before reading any message", async () => {
    await expect(draw("de")).rejects.toThrow("NEXT_NOT_FOUND");

    expect(seen.asked).toBe(0);
    expect(seen.clerk).toEqual([]);
  });
});

describe("root layout: what surrounds every page", () => {
  it("nests the theme, Clerk, Convex and the messages, in that order, around the page", async () => {
    const html = await draw("fr");

    expect(html).toContain(
      '<div data-provider="theme"><div data-provider="clerk"><div data-provider="convex"><div data-provider="messages"><p>La page</p><output data-toaster=""></output></div></div></div></div>',
    );
  });

  it("gives the pages the messages of the request", async () => {
    await draw("fr");

    expect(seen.asked).toBe(1);
    expect(seen.messages).toEqual([fr]);
  });

  it.each([
    ["fr", frFR],
    ["en", enUS],
  ])(
    "puts Clerk's own windows in the language of the address (%s)",
    async (locale, localization) => {
      await draw(locale);

      expect(seen.clerk).toHaveLength(1);
      expect(seen.clerk[0].localization).toBe(localization);
      expect(seen.clerk[0].dynamic).toBe(true);
    },
  );

  it("follows the theme of the visitor's system, held in a class", async () => {
    await draw("fr");

    expect(seen.theme).toEqual([
      {
        attribute: "class",
        defaultTheme: "system",
        enableSystem: true,
        disableTransitionOnChange: true,
      },
    ]);
  });
});

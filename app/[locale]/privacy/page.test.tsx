// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";
import { renderPage } from "@/test-support/pages";
import PrivacyPage from "./page";

vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);

/** The public privacy policy: seven numbered sections read from the catalog. */

const SECTIONS = [1, 2, 3, 4, 5, 6, 7] as const;

/** Each section as the page draws it: its numbered title, then its text. */
function sectionsShown(): [string | null, string | null | undefined][] {
  return screen
    .getAllByRole("heading", { level: 2 })
    .map((title) => [title.textContent, title.nextElementSibling?.textContent]);
}

describe("privacy page", () => {
  it("shows the policy's seven sections, numbered and in order", async () => {
    await renderPage(<PrivacyPage />);

    expect(
      screen.getByRole("heading", { level: 1, name: fr.privacy.title }),
    ).toBeTruthy();
    expect(screen.getByText(fr.privacy.intro)).toBeTruthy();
    expect(sectionsShown()).toEqual(
      SECTIONS.map((n) => [
        `${n}. ${fr.privacy[`section${n}Title`]}`,
        fr.privacy[`section${n}Content`],
      ]),
    );
  });

  it("is in English for an English visitor", async () => {
    await renderPage(<PrivacyPage />, { locale: "en" });

    expect(
      screen.getByRole("heading", { level: 1, name: en.privacy.title }),
    ).toBeTruthy();
    expect(sectionsShown()).toEqual(
      SECTIONS.map((n) => [
        `${n}. ${en.privacy[`section${n}Title`]}`,
        en.privacy[`section${n}Content`],
      ]),
    );
  });

  it("gives the address to write to, after the text that invites to", async () => {
    await renderPage(<PrivacyPage />);

    const address = screen.getByRole("link", {
      name: "contact@gauratechnologies.com",
    });
    expect(address.getAttribute("href")).toBe(
      "mailto:contact@gauratechnologies.com",
    );
    expect(address.previousElementSibling?.textContent).toBe(
      fr.privacy.section7Content,
    );
  });

  it("leads back to the landing page", async () => {
    await renderPage(<PrivacyPage />);

    expect(
      screen
        .getByRole("button", { name: fr.privacy.backToHome })
        .closest("a")
        ?.getAttribute("href"),
    ).toBe("/");
  });

  // What the page does today: the date of the policy and the rights notice
  // are written in English in the page, whatever the visitor's language.
  it("dates the policy and signs it with the current year", async () => {
    await renderPage(<PrivacyPage />);

    expect(
      screen.getByText(`${fr.privacy.lastUpdated}: January 17, 2026`),
    ).toBeTruthy();
    // The tests start on 15 January 2027.
    expect(screen.getByText("© 2027 Gaura. All rights reserved.")).toBeTruthy();
  });
});

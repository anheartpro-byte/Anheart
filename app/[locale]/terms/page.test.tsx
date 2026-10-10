// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";
import { renderPage } from "@/test-support/pages";
import TermsPage from "./page";

vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);

/** The public terms of use: ten numbered sections read from the catalog. */

const SECTIONS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] as const;

/** Each section as the page draws it: its numbered title, then its text. */
function sectionsShown(): [string | null, string | null | undefined][] {
  return screen
    .getAllByRole("heading", { level: 2 })
    .map((title) => [title.textContent, title.nextElementSibling?.textContent]);
}

describe("terms page", () => {
  it("shows the ten sections of the terms, numbered and in order", async () => {
    await renderPage(<TermsPage />);

    expect(
      screen.getByRole("heading", { level: 1, name: fr.terms.title }),
    ).toBeTruthy();
    expect(screen.getByText(fr.terms.intro)).toBeTruthy();
    expect(sectionsShown()).toEqual(
      SECTIONS.map((n) => [
        `${n}. ${fr.terms[`section${n}Title`]}`,
        fr.terms[`section${n}Content`],
      ]),
    );
  });

  it("is in English for an English visitor", async () => {
    await renderPage(<TermsPage />, { locale: "en" });

    expect(
      screen.getByRole("heading", { level: 1, name: en.terms.title }),
    ).toBeTruthy();
    expect(sectionsShown()).toEqual(
      SECTIONS.map((n) => [
        `${n}. ${en.terms[`section${n}Title`]}`,
        en.terms[`section${n}Content`],
      ]),
    );
  });

  // The section a rider must not miss: the service does not replace a doctor.
  it("carries the medical disclaimer among its sections", async () => {
    await renderPage(<TermsPage />);

    expect(
      screen.getByRole("heading", {
        level: 2,
        name: `4. ${fr.terms.section4Title}`,
      }),
    ).toBeTruthy();
    expect(fr.terms.section4Title).toBe("Avertissement Médical");
    expect(screen.getByText(fr.terms.section4Content)).toBeTruthy();
  });

  it("gives the address to write to, after the text that invites to", async () => {
    await renderPage(<TermsPage />);

    const address = screen.getByRole("link", {
      name: "contact@gauratechnologies.com",
    });
    expect(address.getAttribute("href")).toBe(
      "mailto:contact@gauratechnologies.com",
    );
    expect(address.previousElementSibling?.textContent).toBe(
      fr.terms.section10Content,
    );
  });

  it("leads back to the landing page", async () => {
    await renderPage(<TermsPage />);

    expect(
      screen
        .getByRole("button", { name: fr.terms.backToHome })
        .closest("a")
        ?.getAttribute("href"),
    ).toBe("/");
  });

  // What the page does today: the date of the terms and the rights notice
  // are written in English in the page, whatever the visitor's language.
  it("dates the terms and signs them with the current year", async () => {
    await renderPage(<TermsPage />);

    expect(
      screen.getByText(`${fr.terms.lastUpdated}: January 17, 2026`),
    ).toBeTruthy();
    // The tests start on 15 January 2027.
    expect(screen.getByText("© 2027 Gaura. All rights reserved.")).toBeTruthy();
  });
});

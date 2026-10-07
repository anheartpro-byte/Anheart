// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";
import { renderPage, wasMounted } from "@/test-support/pages";
import FAQPage from "./page";

vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);
vi.mock("@/components/landing/Navbar", async () => ({
  Navbar: (await import("@/test-support/pages")).standIn("Navbar"),
}));
vi.mock("@/components/landing/Footer", async () => ({
  Footer: (await import("@/test-support/pages")).standIn("Footer"),
}));
// Animated: nothing of it is text.
vi.mock("@/components/ui/border-beam", async () => ({
  BorderBeam: (await import("@/test-support/pages")).standIn("BorderBeam"),
}));

/** The public page of frequent questions: its own French and English texts. */

/** The questions under each category, as the page lists them. */
function questionsByCategory(): [string | null, number][] {
  return screen
    .getAllByRole("heading", { level: 2 })
    .slice(0, -1)
    .map((category) => [
      category.textContent,
      within(category.parentElement as HTMLElement).getAllByRole("button")
        .length,
    ]);
}

describe("FAQ page", () => {
  it("lists the French questions under their three categories for a French visitor", async () => {
    await renderPage(<FAQPage />);

    expect(
      screen.getByRole("heading", { level: 1, name: fr.faq.title }),
    ).toBeTruthy();
    expect(questionsByCategory()).toEqual([
      ["Questions Générales", 4],
      ["Technologie et Entraînement", 4],
      ["Athlètes et Performance", 2],
    ]);
    expect(
      screen.getByRole("button", { name: "Qu'est-ce que Gaura ?" }),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "What is Gaura?" })).toBeNull();
  });

  it("lists the English questions for an English visitor", async () => {
    await renderPage(<FAQPage />, { locale: "en" });

    expect(
      screen.getByRole("heading", { level: 1, name: en.faq.title }),
    ).toBeTruthy();
    expect(questionsByCategory()).toEqual([
      ["General Questions", 4],
      ["Technology & Training", 4],
      ["Athletes & Performance", 2],
    ]);
    expect(screen.getByRole("button", { name: "What is Gaura?" })).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Qu'est-ce que Gaura ?" }),
    ).toBeNull();
  });

  it("shows the answer of a question once it is opened, and one answer at a time in a category", async () => {
    const { user } = await renderPage(<FAQPage />);
    const answer = "Gaura a son siège à Paris, en France.";
    expect(screen.queryByText(answer)).toBeNull();

    await user.click(
      screen.getByRole("button", { name: "Où est basée Gaura ?" }),
    );
    expect(screen.getByText(answer)).toBeTruthy();

    await user.click(
      screen.getByRole("button", { name: "Qu'est-ce que Gaura ?" }),
    );
    expect(screen.queryByText(answer)).toBeNull();
    expect(
      screen.getByText(/^Gaura est un système révolutionnaire/),
    ).toBeTruthy();

    // Opened again, it closes.
    await user.click(
      screen.getByRole("button", { name: "Qu'est-ce que Gaura ?" }),
    );
    expect(
      screen.queryByText(/^Gaura est un système révolutionnaire/),
    ).toBeNull();
  });

  // Written in the dashboard's guide: the limits are those of the machine's
  // software, said to be technical, and the six channels are named.
  it("says the limits are technical, not medical, and names the six channels", async () => {
    const { user } = await renderPage(<FAQPage />);

    await user.click(
      screen.getByRole("button", {
        name: "Quelle est la durée d'entraînement ?",
      }),
    );
    expect(
      screen.getByText(
        /dans les limites fixées par le logiciel de la machine\. Ces limites sont techniques, ce ne sont pas des recommandations médicales\./,
      ),
    ).toBeTruthy();

    await user.click(
      screen.getByRole("button", {
        name: "Quels canaux le système de surveillance ECG suit-il?",
      }),
    );
    expect(screen.getByText(/ECG, EDA, SpO2, RESP, EMG et LUX/)).toBeTruthy();
  });

  it("gives the address to write to and the way back to the landing page", async () => {
    await renderPage(<FAQPage />);

    expect(
      screen
        .getByRole("button", { name: fr.faq.contactUs })
        .closest("a")
        ?.getAttribute("href"),
    ).toBe("mailto:contact@gauratechnologies.com");
    expect(
      screen
        .getByRole("button", { name: fr.faq.backToHome })
        .closest("a")
        ?.getAttribute("href"),
    ).toBe("/");
    expect(
      screen.getByRole("heading", { level: 2, name: fr.faq.stillQuestions }),
    ).toBeTruthy();
  });

  it("is framed by the navigation bar and the footer", async () => {
    await renderPage(<FAQPage />);

    expect(wasMounted("Navbar")).toBe(true);
    expect(wasMounted("Footer")).toBe(true);
    expect(
      screen
        .getByRole("main")
        .previousElementSibling?.getAttribute("data-stand-in"),
    ).toBe("Navbar");
  });
});

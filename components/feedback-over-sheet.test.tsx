// @vitest-environment jsdom
import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { dismissFeedback, getFeedbackToasts, pushFeedback } from "@/lib/feedback";
import fr from "@/messages/fr.json";
import { FeedbackToaster } from "./FeedbackToaster";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "./ui/sheet";

afterEach(() => {
  cleanup();
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
});

function page(onInteractOutside?: ComponentProps<typeof SheetContent>["onInteractOutside"]) {
  return render(
    <NextIntlClientProvider locale="fr" messages={fr} timeZone="Europe/Paris">
      <main>Fond de page</main>
      <Sheet>
        <SheetTrigger>Ouvrir le menu</SheetTrigger>
        <SheetContent
          aria-describedby={undefined}
          onInteractOutside={onInteractOutside}
        >
          <SheetTitle>Navigation mobile</SheetTitle>
        </SheetContent>
      </Sheet>
      <FeedbackToaster />
    </NextIntlClientProvider>,
  );
}

describe("ANH-232 feedback with the real mobile sheet", () => {
  it("closes the message, keeps the menu open and still calls the caller", async () => {
    const onInteractOutside = vi.fn((event: Event) => {
      expect(event.defaultPrevented).toBe(false);
    });
    page(onInteractOutside);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Ouvrir le menu" }));
    act(() => pushFeedback("error", "Machine introuvable."));

    await user.click(
      within(screen.getByRole("alert")).getByRole("button", { name: fr.common.close }),
    );

    expect(getFeedbackToasts()).toEqual([]);
    expect(screen.getByRole("dialog", { name: "Navigation mobile" })).toBeTruthy();
    expect(onInteractOutside).toHaveBeenCalled();
    expect(
      onInteractOutside.mock.calls.some(([event]) => event.defaultPrevented),
    ).toBe(true);
  });

  it("still closes the menu when the overlay is clicked", async () => {
    const onInteractOutside = vi.fn();
    page(onInteractOutside);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Ouvrir le menu" }));
    const overlay = document.querySelector<HTMLElement>("[data-slot=sheet-overlay]");
    if (!overlay) throw new Error("The sheet overlay is missing");

    await user.click(overlay);

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onInteractOutside).toHaveBeenCalled();
  });

  it("respects the caller when it prevents closing from the overlay", async () => {
    const onInteractOutside = vi.fn((event: Event) => event.preventDefault());
    page(onInteractOutside);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Ouvrir le menu" }));
    const overlay = document.querySelector<HTMLElement>("[data-slot=sheet-overlay]");
    if (!overlay) throw new Error("The sheet overlay is missing");

    await user.click(overlay);

    expect(screen.getByRole("dialog", { name: "Navigation mobile" })).toBeTruthy();
    expect(onInteractOutside).toHaveBeenCalled();
  });

  it("keeps both pre-existing live regions exposed when a failure arrives in the open menu", async () => {
    page();
    const alert = screen.getByRole("alert");
    const status = screen.getByRole("status");
    expect(alert.getAttribute("aria-live")).toBe("assertive");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(alert.textContent).toBe("");
    expect(status.textContent).toBe("");
    const background = screen.getByRole("main");
    await userEvent.setup().click(
      screen.getByRole("button", { name: "Ouvrir le menu" }),
    );
    act(() => pushFeedback("error", "Échec pendant le menu ouvert."));

    expect(screen.getByRole("alert")).toBe(alert);
    expect(within(alert).getByText("Échec pendant le menu ouvert.")).toBeTruthy();
    expect(screen.getByRole("status")).toBe(status);
    for (const region of [alert, status]) {
      for (
        let element: HTMLElement | null = region;
        element;
        element = element.parentElement
      ) {
        expect(element.hasAttribute("aria-hidden")).toBe(false);
        expect(element.hasAttribute("data-aria-hidden")).toBe(false);
      }
    }
    expect(background.closest('[aria-hidden="true"]')).not.toBeNull();
    expect(background.closest('[data-aria-hidden="true"]')).not.toBeNull();
    expect(screen.queryByRole("main")).toBeNull();
  });
});

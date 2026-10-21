// @vitest-environment jsdom
import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  dismissFeedback,
  getFeedbackToasts,
  isFeedbackElement,
  pushFeedback,
} from "@/lib/feedback";
import fr from "@/messages/fr.json";
import { Dialog, DialogContent, DialogTitle, DialogTrigger } from "./ui/dialog";
import { FeedbackToaster } from "./FeedbackToaster";

/**
 * ANH-194 EX-3: a failure shown while a window is open can be closed without
 * closing the window, with the real Radix dialog the site uses.
 */

afterEach(() => {
  cleanup();
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
});

function page(onOpenChange: (open: boolean) => void) {
  return render(
    <NextIntlClientProvider locale="fr" messages={fr} timeZone="Europe/Paris">
      <button type="button">Derrière la fenêtre</button>
      <Dialog open onOpenChange={onOpenChange}>
        <DialogContent aria-describedby={undefined}>
          <DialogTitle>Assigner des machines</DialogTitle>
        </DialogContent>
      </Dialog>
      <FeedbackToaster />
    </NextIntlClientProvider>,
  );
}

describe("ANH-194 a message above an open window", () => {
  it("closes when its cross is pressed, and the window stays open", async () => {
    const onOpenChange = vi.fn();
    page(onOpenChange);
    act(() => pushFeedback("error", "Machine introuvable."));
    const user = userEvent.setup();

    const alert = screen.getByRole("alert");
    await user.click(
      within(alert).getByRole("button", { name: fr.common.close }),
    );

    expect(getFeedbackToasts()).toEqual([]);
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("is told apart from the rest of the page: a click there still closes the window", () => {
    page(() => {});
    act(() => pushFeedback("error", "Machine introuvable."));

    const message = screen.getByText("Machine introuvable.");
    expect(isFeedbackElement(message)).toBe(true);
    expect(
      isFeedbackElement(screen.getByText("Derrière la fenêtre")),
    ).toBe(false);
    expect(isFeedbackElement(null)).toBe(false);
  });

  it("keeps both pre-existing live regions exposed when a failure arrives in a simple confirmation", async () => {
    render(
      <NextIntlClientProvider locale="fr" messages={fr} timeZone="Europe/Paris">
        <main>Fond de page</main>
        <Dialog>
          <DialogTrigger>Ouvrir la confirmation</DialogTrigger>
          <DialogContent aria-describedby={undefined}>
            <DialogTitle>Confirmer la suppression</DialogTitle>
            <button type="button">Supprimer</button>
          </DialogContent>
        </Dialog>
        <FeedbackToaster />
      </NextIntlClientProvider>,
    );
    const alert = screen.getByRole("alert");
    const status = screen.getByRole("status");
    expect(alert.getAttribute("aria-live")).toBe("assertive");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(alert.textContent).toBe("");
    expect(status.textContent).toBe("");
    const background = screen.getByRole("main");
    await userEvent.setup().click(
      screen.getByRole("button", { name: "Ouvrir la confirmation" }),
    );
    act(() => pushFeedback("error", "Suppression refusée."));

    expect(screen.getByRole("alert")).toBe(alert);
    expect(within(alert).getByText("Suppression refusée.")).toBeTruthy();
    expect(screen.getByRole("status")).toBe(status);
    expect(within(screen.getByRole("dialog")).queryByRole("alert")).toBeNull();
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

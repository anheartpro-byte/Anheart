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
import { Dialog, DialogContent, DialogTitle } from "./ui/dialog";
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

    const alert = screen.getByRole("alert", { hidden: true });
    await user.click(
      within(alert).getByRole("button", { name: fr.common.close, hidden: true }),
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
});

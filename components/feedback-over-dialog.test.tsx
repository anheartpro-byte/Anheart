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
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "./ui/sheet";

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
  it.each(["dialog", "sheet"] as const)(
    "a touch closes only its message above a real %s, while the overlay still closes it",
    async (kind) => {
      const Root = kind === "dialog" ? Dialog : Sheet;
      const Content = kind === "dialog" ? DialogContent : SheetContent;
      const Title = kind === "dialog" ? DialogTitle : SheetTitle;
      const Trigger = kind === "dialog" ? DialogTrigger : SheetTrigger;
      const onOpenChange = vi.fn();
      render(
        <NextIntlClientProvider locale="fr" messages={fr} timeZone="Europe/Paris">
          <Root onOpenChange={onOpenChange}>
            <Trigger>Ouvrir</Trigger>
            <Content aria-describedby={undefined}><Title>Fenêtre tactile</Title></Content>
          </Root>
          <FeedbackToaster />
        </NextIntlClientProvider>,
      );
      const user = userEvent.setup();
      await user.click(screen.getByRole("button", { name: "Ouvrir" }));
      onOpenChange.mockClear();
      act(() => pushFeedback("error", "Refus tactile"));
      const cross = within(screen.getByRole("alert")).getByRole("button", { name: fr.common.close });
      await user.pointer([
        { keys: "[TouchA>]", target: cross },
        { keys: "[/TouchA]" },
      ]);
      expect(getFeedbackToasts()).toEqual([]);
      expect(cross.isConnected).toBe(false);
      expect(isFeedbackElement(cross)).toBe(true);
      expect(onOpenChange).not.toHaveBeenCalled();
      expect(screen.getByRole("dialog")).toBeTruthy();
      const overlay = document.querySelector<HTMLElement>(`[data-slot="${kind}-overlay"]`)!;
      await user.pointer([
        { keys: "[TouchB>]", target: overlay },
        { keys: "[/TouchB]" },
      ]);
      expect(onOpenChange).toHaveBeenCalledWith(false);
      expect(screen.queryByRole("dialog")).toBeNull();
    },
  );

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

describe("ANH-232 the field focused when a real window opens", () => {
  /** Each element scrolled into view, with the options given. */
  function watchScrolls() {
    const scrolled: { element: Element; options: unknown }[] = [];
    const prototype = window.Element.prototype as { scrollIntoView?: unknown };
    prototype.scrollIntoView = function (this: Element, options: unknown) {
      scrolled.push({ element: this, options });
    };
    return {
      scrolled,
      restore: () => delete prototype.scrollIntoView,
    };
  }

  function form(onOpenAutoFocus?: (event: Event) => void) {
    render(
      <Dialog open>
        <DialogContent
          aria-describedby={undefined}
          onOpenAutoFocus={onOpenAutoFocus}
        >
          <DialogTitle>Nouvelle machine</DialogTitle>
          <input aria-label="Nom" />
        </DialogContent>
      </Dialog>,
    );
  }

  it("is brought into view once Radix has focused it, inside the window's scroll padding", async () => {
    const { scrolled, restore } = watchScrolls();
    try {
      form();
      const field = screen.getByRole("textbox", { name: "Nom" });
      expect(document.activeElement).toBe(field);
      await act(async () => {});

      expect(scrolled).toEqual([
        { element: field, options: { block: "nearest" } },
      ]);
    } finally {
      restore();
    }
  });

  it("is left alone when the caller takes the focus on opening", async () => {
    const { scrolled, restore } = watchScrolls();
    const onOpenAutoFocus = vi.fn((event: Event) => event.preventDefault());
    try {
      form(onOpenAutoFocus);
      await act(async () => {});

      expect(onOpenAutoFocus).toHaveBeenCalledOnce();
      expect(scrolled).toEqual([]);
    } finally {
      restore();
    }
  });
});

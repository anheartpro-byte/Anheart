import type { ComponentType, ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { draw } from "@/test-support/markup";
import { given, isPrimitive } from "@/test-support/radix";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
  DialogTrigger,
} from "./dialog";

/**
 * The dialog window of the site: which Radix primitive each part draws and
 * with what, the overlay and the portal the content brings with it, and the
 * close button it adds unless the caller wants none.
 *
 * The real primitives need a browser (a portal, a scroll lock). Each is
 * replaced here by a stand-in that draws a plain element in place and keeps
 * the props it was given: what is proven is what `dialog.tsx` itself decides.
 */

vi.mock("@radix-ui/react-dialog", async () =>
  (await import("@/test-support/radix")).standIns("Dialog", {
    Root: "div",
    Trigger: "button",
    Portal: "div",
    Overlay: "div",
    Content: "div",
    Close: "button",
    Title: "h2",
    Description: "p",
  }),
);

type Part = ComponentType<{ className?: string; children?: ReactNode }>;

describe("ANH-203 dialog: the parts", () => {
  it.each<[string, string, Part]>([
    ["dialog", "Dialog.Root", Dialog],
    ["dialog-trigger", "Dialog.Trigger", DialogTrigger],
    ["dialog-portal", "Dialog.Portal", DialogPortal],
    ["dialog-close", "Dialog.Close", DialogClose],
    ["dialog-overlay", "Dialog.Overlay", DialogOverlay],
    ["dialog-title", "Dialog.Title", DialogTitle],
    ["dialog-description", "Dialog.Description", DialogDescription],
  ])(
    "%s is the primitive %s, with what the caller puts in it",
    (slot, primitive, Part) => {
      const page = draw(<Part>Arrêter la séance</Part>);

      expect(page.slot(slot).getAttribute("data-primitive")).toBe(primitive);
      expect(page.slot(slot).text).toBe("Arrêter la séance");
    },
  );

  it("tells the primitive whether the window is open, and whom to tell when that changes", () => {
    const onOpenChange = vi.fn();
    draw(<Dialog open onOpenChange={onOpenChange} modal={false} />);

    expect(given("Dialog.Root")).toMatchObject({
      open: true,
      onOpenChange,
      modal: false,
    });
  });

  it("lets the caller's own button open the window", () => {
    const page = draw(
      <DialogTrigger asChild>
        <a href="#stop">Arrêter</a>
      </DialogTrigger>,
    );

    expect(given("Dialog.Trigger").asChild).toBe(true);
    expect(page.slot("dialog-trigger").localName).toBe("a");
  });

  it.each<[string, Part, string, string]>([
    ["dialog-overlay", DialogOverlay, "bg-black/50", "bg-black/80"],
    ["dialog-title", DialogTitle, "text-lg", "text-xl"],
    ["dialog-description", DialogDescription, "text-sm", "text-base"],
    ["dialog-header", DialogHeader, "gap-2", "gap-4"],
    ["dialog-footer", DialogFooter, "sm:justify-end", "sm:justify-between"],
  ])(
    "%s: the caller's class replaces the one that sets the same thing",
    (slot, Part, own, instead) => {
      expect(draw(<Part />).slot(slot).classes).toContain(own);

      const page = draw(<Part className={instead} />);
      expect(page.slot(slot).classes).toContain(instead);
      expect(page.slot(slot).classes).not.toContain(own);
    },
  );

  it("draws the header and the footer as plain blocks around what they hold", () => {
    const page = draw(
      <>
        <DialogHeader id="top">Titre</DialogHeader>
        <DialogFooter id="bottom">Actions</DialogFooter>
      </>,
    );

    expect(page.slot("dialog-header").localName).toBe("div");
    expect(page.slot("dialog-header").getAttribute("id")).toBe("top");
    expect(page.slot("dialog-header").text).toBe("Titre");
    expect(page.slot("dialog-footer").localName).toBe("div");
    expect(page.slot("dialog-footer").getAttribute("id")).toBe("bottom");
    expect(page.slot("dialog-footer").text).toBe("Actions");
    // On a phone the main action is at the bottom, under the thumb.
    expect(page.slot("dialog-footer").classes).toContain("flex-col-reverse");
  });
});

describe("ANH-203 dialog: the content", () => {
  it("draws the window in a portal, after the overlay that dims the page", () => {
    const page = draw(<DialogContent>Confirmer ?</DialogContent>);

    const portal = page.slot("dialog-portal");
    expect(portal.getAttribute("data-primitive")).toBe("Dialog.Portal");
    expect(portal.children).toEqual([
      page.slot("dialog-overlay"),
      page.slot("dialog-content"),
    ]);
    expect(page.slot("dialog-content").getAttribute("data-primitive")).toBe(
      "Dialog.Content",
    );
  });

  it("holds what the caller puts in it, then a button that closes it", () => {
    const page = draw(
      <DialogContent>
        <b>Confirmer ?</b>
      </DialogContent>,
    );

    const [body, close] = page.slot("dialog-content").children;
    expect(body.text).toBe("Confirmer ?");
    expect(close).toBe(page.slot("dialog-close"));
    expect(close.getAttribute("data-primitive")).toBe("Dialog.Close");
  });

  it("names the close button for a screen reader, next to its cross", () => {
    const page = draw(<DialogContent />);

    const close = page.slot("dialog-close");
    const [cross, name] = close.children;
    expect(cross.classes).toContain("lucide-x");
    expect(name.classes).toContain("sr-only");
    expect(close.text).toBe("Close");
  });

  it("has no close button when the caller wants none", () => {
    const page = draw(
      <DialogContent showCloseButton={false}>
        <b>Confirmer ?</b>
      </DialogContent>,
    );

    expect(page.slots("dialog-close")).toEqual([]);
    expect(page.all(isPrimitive("Dialog.Close"))).toEqual([]);
    expect(page.slot("dialog-content").text).toBe("Confirmer ?");
    // The choice is the wrapper's own: the primitive never hears of it.
    expect(given("Dialog.Content")).not.toHaveProperty("showCloseButton");
  });

  it("lets the caller's class replace the width of the window", () => {
    expect(draw(<DialogContent />).slot("dialog-content").classes).toContain(
      "sm:max-w-lg",
    );

    const page = draw(<DialogContent className="sm:max-w-3xl" />);
    expect(page.slot("dialog-content").classes).toContain("sm:max-w-3xl");
    expect(page.slot("dialog-content").classes).not.toContain("sm:max-w-lg");
    // The class is the window's: the overlay keeps its own.
    expect(page.slot("dialog-overlay").classes).not.toContain("sm:max-w-3xl");
  });

  it("passes the caller's other props on to the window", () => {
    const onEscapeKeyDown = vi.fn();
    const page = draw(
      <DialogContent
        onEscapeKeyDown={onEscapeKeyDown}
        aria-describedby="why"
      />,
    );

    expect(given("Dialog.Content").onEscapeKeyDown).toBe(onEscapeKeyDown);
    expect(page.slot("dialog-content").getAttribute("aria-describedby")).toBe(
      "why",
    );
  });
});

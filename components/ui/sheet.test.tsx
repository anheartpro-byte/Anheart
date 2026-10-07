import type { ComponentType, ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { draw } from "@/test-support/markup";
import { given, isPrimitive } from "@/test-support/radix";
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "./sheet";

/**
 * The sheet: a dialog window that slides in from one edge of the screen (the
 * menu of the site on a phone). Which Radix primitive each part draws and
 * with what, the edge the content is attached to, the overlay, the portal and
 * the close button it brings with it.
 *
 * The real primitives need a browser (a portal, a scroll lock). Each is
 * replaced here by a stand-in that draws a plain element in place and keeps
 * the props it was given: what is proven is what `sheet.tsx` itself decides.
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

/**
 * Each edge with the classes that attach the sheet to it: where it sits, the
 * border on its inner side, the direction it slides in from.
 */
const SIDES = [
  ["right", ["right-0", "border-l", "data-[state=open]:slide-in-from-right"]],
  ["left", ["left-0", "border-r", "data-[state=open]:slide-in-from-left"]],
  ["top", ["top-0", "border-b", "data-[state=open]:slide-in-from-top"]],
  [
    "bottom",
    ["bottom-0", "border-t", "data-[state=open]:slide-in-from-bottom"],
  ],
] as const;

describe("ANH-203 sheet: the parts", () => {
  it.each<[string, string, Part]>([
    ["sheet", "Dialog.Root", Sheet],
    ["sheet-trigger", "Dialog.Trigger", SheetTrigger],
    ["sheet-close", "Dialog.Close", SheetClose],
    ["sheet-title", "Dialog.Title", SheetTitle],
    ["sheet-description", "Dialog.Description", SheetDescription],
  ])(
    "%s is the primitive %s, with what the caller puts in it",
    (slot, primitive, Part) => {
      const page = draw(<Part>Menu</Part>);

      expect(page.slot(slot).getAttribute("data-primitive")).toBe(primitive);
      expect(page.slot(slot).text).toBe("Menu");
    },
  );

  it("tells the primitive whether the sheet is open, and whom to tell when that changes", () => {
    const onOpenChange = vi.fn();
    draw(<Sheet open onOpenChange={onOpenChange} />);

    expect(given("Dialog.Root")).toMatchObject({ open: true, onOpenChange });
  });

  it("lets the caller's own button open the sheet", () => {
    const page = draw(
      <SheetTrigger asChild>
        <a href="#menu">Menu</a>
      </SheetTrigger>,
    );

    expect(given("Dialog.Trigger").asChild).toBe(true);
    expect(page.slot("sheet-trigger").localName).toBe("a");
  });

  it.each<[string, Part, string, string]>([
    ["sheet-title", SheetTitle, "font-semibold", "font-bold"],
    ["sheet-description", SheetDescription, "text-sm", "text-base"],
    ["sheet-header", SheetHeader, "p-4", "p-6"],
    ["sheet-footer", SheetFooter, "mt-auto", "mt-4"],
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
        <SheetHeader id="top">Titre</SheetHeader>
        <SheetFooter id="bottom">Actions</SheetFooter>
      </>,
    );

    expect(page.slot("sheet-header").localName).toBe("div");
    expect(page.slot("sheet-header").getAttribute("id")).toBe("top");
    expect(page.slot("sheet-header").text).toBe("Titre");
    expect(page.slot("sheet-footer").localName).toBe("div");
    expect(page.slot("sheet-footer").getAttribute("id")).toBe("bottom");
    expect(page.slot("sheet-footer").text).toBe("Actions");
  });
});

describe("ANH-203 sheet: the content", () => {
  it("draws the sheet in a portal, after the overlay that dims the page", () => {
    const page = draw(<SheetContent>Menu</SheetContent>);

    const portal = page.slot("sheet-portal");
    expect(portal.getAttribute("data-primitive")).toBe("Dialog.Portal");
    expect(portal.children).toEqual([
      page.slot("sheet-overlay"),
      page.slot("sheet-content"),
    ]);
    expect(page.slot("sheet-overlay").getAttribute("data-primitive")).toBe(
      "Dialog.Overlay",
    );
    expect(page.slot("sheet-overlay").classes).toContain("bg-black/50");
    expect(page.slot("sheet-content").getAttribute("data-primitive")).toBe(
      "Dialog.Content",
    );
  });

  it("holds what the caller puts in it, then a button that closes it", () => {
    const page = draw(
      <SheetContent>
        <b>Menu</b>
      </SheetContent>,
    );

    const [body, close] = page.slot("sheet-content").children;
    expect(body.text).toBe("Menu");
    expect(close).toBe(page.only(isPrimitive("Dialog.Close")));
  });

  it("names the close button for a screen reader, next to its cross", () => {
    const page = draw(<SheetContent />);

    const close = page.only(isPrimitive("Dialog.Close"));
    const [cross, name] = close.children;
    expect(cross.classes).toContain("lucide-x");
    expect(name.classes).toContain("sr-only");
    expect(close.text).toBe("Close");
  });

  it("slides in from the right unless told otherwise", () => {
    const page = draw(<SheetContent />);

    expect(page.slot("sheet-content").classes).toContain("right-0");
    expect(page.slot("sheet-content").classes).not.toContain("left-0");
  });

  it.each(SIDES)(
    "side %s: is attached to that edge and to no other",
    (side, attached) => {
      const page = draw(<SheetContent side={side} />);

      const classes = page.slot("sheet-content").classes;
      for (const name of attached) expect(classes).toContain(name);
      for (const [other, elsewhere] of SIDES) {
        if (other === side) continue;
        for (const name of elsewhere) expect(classes).not.toContain(name);
      }
      // The edge is the wrapper's own: the primitive never hears of it.
      expect(given("Dialog.Content")).not.toHaveProperty("side");
    },
  );

  it.each([
    ["right", "h-full", "w-3/4"],
    ["left", "h-full", "w-3/4"],
    ["top", "h-auto", "inset-x-0"],
    ["bottom", "h-auto", "inset-x-0"],
  ] as const)(
    "side %s: takes the full length of its edge",
    (side, height, width) => {
      const page = draw(<SheetContent side={side} />);

      expect(page.slot("sheet-content").classes).toContain(height);
      expect(page.slot("sheet-content").classes).toContain(width);
    },
  );

  it("lets the caller's class replace the width of the sheet", () => {
    const page = draw(
      <SheetContent className="w-full" aria-describedby="why" />,
    );

    expect(page.slot("sheet-content").classes).toContain("w-full");
    expect(page.slot("sheet-content").classes).not.toContain("w-3/4");
    expect(page.slot("sheet-content").getAttribute("aria-describedby")).toBe(
      "why",
    );
    // The class is the sheet's: the overlay keeps its own.
    expect(page.slot("sheet-overlay").classes).not.toContain("w-full");
  });
});

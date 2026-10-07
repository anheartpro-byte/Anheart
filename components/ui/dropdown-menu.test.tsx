import type { ComponentType, ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { draw } from "@/test-support/markup";
import { given, isPrimitive } from "@/test-support/radix";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuPortal,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "./dropdown-menu";

/**
 * The dropdown menu of the site (the account menu, the actions of a table
 * row): which Radix primitive each part draws and with what, the distance
 * between the menu and its button, the marks of an inset or destructive item,
 * the tick of a checked item, the chevron of a submenu.
 *
 * The real primitives need a browser (a portal, a layout). Each is replaced
 * here by a stand-in that draws a plain element in place and keeps the props
 * it was given: what is proven is what `dropdown-menu.tsx` itself decides.
 */

vi.mock("@radix-ui/react-dropdown-menu", async () =>
  (await import("@/test-support/radix")).standIns("Menu", {
    Root: "div",
    Portal: "div",
    Trigger: "button",
    Content: "div",
    Group: "div",
    Item: "div",
    CheckboxItem: "div",
    ItemIndicator: "span",
    RadioGroup: "div",
    RadioItem: "div",
    Label: "div",
    Separator: "div",
    Sub: "div",
    SubTrigger: "div",
    SubContent: "div",
  }),
);

type Part = ComponentType<{ className?: string; children?: ReactNode }>;

/** A radio item is drawn with the value it stands for. */
function RadioItem(props: { className?: string; children?: ReactNode }) {
  return <DropdownMenuRadioItem value="fr" {...props} />;
}

describe("ANH-203 dropdown menu: the parts", () => {
  it.each<[string, string, Part]>([
    ["dropdown-menu", "Menu.Root", DropdownMenu],
    ["dropdown-menu-portal", "Menu.Portal", DropdownMenuPortal],
    ["dropdown-menu-trigger", "Menu.Trigger", DropdownMenuTrigger],
    ["dropdown-menu-content", "Menu.Content", DropdownMenuContent],
    ["dropdown-menu-group", "Menu.Group", DropdownMenuGroup],
    ["dropdown-menu-item", "Menu.Item", DropdownMenuItem],
    [
      "dropdown-menu-checkbox-item",
      "Menu.CheckboxItem",
      DropdownMenuCheckboxItem,
    ],
    ["dropdown-menu-radio-group", "Menu.RadioGroup", DropdownMenuRadioGroup],
    ["dropdown-menu-radio-item", "Menu.RadioItem", RadioItem],
    ["dropdown-menu-label", "Menu.Label", DropdownMenuLabel],
    ["dropdown-menu-separator", "Menu.Separator", DropdownMenuSeparator],
    ["dropdown-menu-sub", "Menu.Sub", DropdownMenuSub],
    ["dropdown-menu-sub-trigger", "Menu.SubTrigger", DropdownMenuSubTrigger],
    ["dropdown-menu-sub-content", "Menu.SubContent", DropdownMenuSubContent],
  ])(
    "%s is the primitive %s, with what the caller puts in it",
    (slot, primitive, Part) => {
      const page = draw(<Part>Se déconnecter</Part>);

      expect(page.slot(slot).getAttribute("data-primitive")).toBe(primitive);
      expect(page.slot(slot).text).toBe("Se déconnecter");
    },
  );

  it.each<[string, Part, string, string]>([
    ["dropdown-menu-content", DropdownMenuContent, "min-w-[8rem]", "min-w-56"],
    ["dropdown-menu-item", DropdownMenuItem, "px-2", "px-4"],
    ["dropdown-menu-checkbox-item", DropdownMenuCheckboxItem, "pl-8", "pl-10"],
    ["dropdown-menu-radio-item", RadioItem, "pl-8", "pl-10"],
    ["dropdown-menu-label", DropdownMenuLabel, "font-medium", "font-normal"],
    ["dropdown-menu-separator", DropdownMenuSeparator, "my-1", "my-2"],
    ["dropdown-menu-shortcut", DropdownMenuShortcut, "text-xs", "text-sm"],
    ["dropdown-menu-sub-trigger", DropdownMenuSubTrigger, "px-2", "px-4"],
    [
      "dropdown-menu-sub-content",
      DropdownMenuSubContent,
      "min-w-[8rem]",
      "min-w-56",
    ],
  ])(
    "%s: the caller's class replaces the one that sets the same thing",
    (slot, Part, own, instead) => {
      expect(draw(<Part />).slot(slot).classes).toContain(own);

      const page = draw(<Part className={instead} />);
      expect(page.slot(slot).classes).toContain(instead);
      expect(page.slot(slot).classes).not.toContain(own);
    },
  );

  it("tells the root whether the menu is open, and whom to tell when that changes", () => {
    const onOpenChange = vi.fn();
    draw(<DropdownMenu open onOpenChange={onOpenChange} />);

    expect(given("Menu.Root")).toMatchObject({ open: true, onOpenChange });
  });

  it("lets the caller's own button open the menu", () => {
    const page = draw(
      <DropdownMenuTrigger asChild>
        <a href="#account">Compte</a>
      </DropdownMenuTrigger>,
    );

    expect(given("Menu.Trigger").asChild).toBe(true);
    expect(page.slot("dropdown-menu-trigger").localName).toBe("a");
  });

  it("draws a keyboard shortcut as a plain text pushed to the end of its item", () => {
    const page = draw(
      <DropdownMenuShortcut id="keys">Ctrl+K</DropdownMenuShortcut>,
    );

    const shortcut = page.slot("dropdown-menu-shortcut");
    expect(shortcut.localName).toBe("span");
    expect(shortcut.hasAttribute("data-primitive")).toBe(false);
    expect(shortcut.text).toBe("Ctrl+K");
    expect(shortcut.getAttribute("id")).toBe("keys");
    expect(shortcut.classes).toContain("ml-auto");
  });
});

describe("ANH-203 dropdown menu: the content", () => {
  it("draws the menu in a portal", () => {
    const page = draw(<DropdownMenuContent>Compte</DropdownMenuContent>);

    const portal = page.only(isPrimitive("Menu.Portal"));
    expect(portal.children).toEqual([page.slot("dropdown-menu-content")]);
  });

  it("opens 4 px away from its button unless told otherwise", () => {
    draw(<DropdownMenuContent />);

    expect(given("Menu.Content").sideOffset).toBe(4);
  });

  it("opens at the distance and on the side the caller asks for", () => {
    draw(<DropdownMenuContent sideOffset={12} align="end" side="top" />);

    expect(given("Menu.Content")).toMatchObject({
      sideOffset: 12,
      align: "end",
      side: "top",
    });
  });

  it("draws a submenu in place, with the props the caller gives it", () => {
    const page = draw(
      <DropdownMenuSubContent sideOffset={8}>Langue</DropdownMenuSubContent>,
    );

    expect(given("Menu.SubContent").sideOffset).toBe(8);
    // No portal of its own: Radix places a submenu next to its parent.
    expect(page.all(isPrimitive("Menu.Portal"))).toEqual([]);
  });
});

describe("ANH-203 dropdown menu: an item", () => {
  it("is a default item, not inset, unless told otherwise", () => {
    const page = draw(<DropdownMenuItem>Profil</DropdownMenuItem>);

    const item = page.slot("dropdown-menu-item");
    expect(item.getAttribute("data-variant")).toBe("default");
    expect(item.hasAttribute("data-inset")).toBe(false);
  });

  it("is marked destructive and inset when the caller says so", () => {
    const page = draw(
      <DropdownMenuItem variant="destructive" inset>
        Supprimer
      </DropdownMenuItem>,
    );

    const item = page.slot("dropdown-menu-item");
    expect(item.getAttribute("data-variant")).toBe("destructive");
    expect(item.getAttribute("data-inset")).toBe("true");
    // The classes that paint and indent it read those two marks.
    expect(item.classes).toContain(
      "data-[variant=destructive]:text-destructive",
    );
    expect(item.classes).toContain("data-[inset]:pl-8");
    // The two marks are the wrapper's own: the primitive never hears of them.
    expect(given("Menu.Item")).not.toHaveProperty("variant");
    expect(given("Menu.Item")).not.toHaveProperty("inset");
  });

  it("tells the primitive what to do when it is chosen, and whether it can be", () => {
    const onSelect = vi.fn();
    draw(<DropdownMenuItem onSelect={onSelect} disabled />);

    expect(given("Menu.Item")).toMatchObject({ onSelect, disabled: true });
  });

  it.each<[string, ComponentType<{ inset?: boolean }>]>([
    ["dropdown-menu-label", DropdownMenuLabel],
    ["dropdown-menu-sub-trigger", DropdownMenuSubTrigger],
  ])(
    "%s is indented like an inset item when the caller says so",
    (slot, Part) => {
      expect(
        draw(<Part />)
          .slot(slot)
          .hasAttribute("data-inset"),
      ).toBe(false);

      const page = draw(<Part inset />);
      expect(page.slot(slot).getAttribute("data-inset")).toBe("true");
      expect(page.slot(slot).classes).toContain("data-[inset]:pl-8");
    },
  );

  it("ends the line that opens a submenu with a chevron", () => {
    const page = draw(<DropdownMenuSubTrigger>Langue</DropdownMenuSubTrigger>);

    const line = page.slot("dropdown-menu-sub-trigger");
    expect(line.text).toBe("Langue");
    const [chevron] = line.children;
    expect(chevron.classes).toContain("lucide-chevron-right");
    expect(chevron.classes).toContain("ml-auto");
    expect(given("Menu.SubTrigger")).not.toHaveProperty("inset");
  });
});

describe("ANH-203 dropdown menu: items that hold a choice", () => {
  it("tells a checkbox item whether it is checked, and whom to tell when that changes", () => {
    const onCheckedChange = vi.fn();
    draw(
      <DropdownMenuCheckboxItem checked onCheckedChange={onCheckedChange}>
        Colonne durée
      </DropdownMenuCheckboxItem>,
    );

    expect(given("Menu.CheckboxItem")).toMatchObject({
      checked: true,
      onCheckedChange,
    });
  });

  it("draws the tick of a checkbox item before its label, where Radix shows it when checked", () => {
    const page = draw(
      <DropdownMenuCheckboxItem checked>
        Colonne durée
      </DropdownMenuCheckboxItem>,
    );

    const item = page.slot("dropdown-menu-checkbox-item");
    const [gutter] = item.children;
    const indicator = page.only(isPrimitive("Menu.ItemIndicator"));
    expect(indicator.parent).toBe(gutter);
    expect(indicator.children[0].classes).toContain("lucide-check");
    // The tick sits in the left gutter the item keeps free for it.
    expect(gutter.classes).toContain("absolute");
    expect(gutter.classes).toContain("left-2");
    expect(item.text).toBe("Colonne durée");
  });

  it("tells a radio group which value is chosen, and whom to tell when that changes", () => {
    const onValueChange = vi.fn();
    draw(<DropdownMenuRadioGroup value="fr" onValueChange={onValueChange} />);

    expect(given("Menu.RadioGroup")).toMatchObject({
      value: "fr",
      onValueChange,
    });
  });

  it("draws the dot of a radio item before its label, where Radix shows it when chosen", () => {
    const page = draw(
      <DropdownMenuRadioItem value="en">English</DropdownMenuRadioItem>,
    );

    const item = page.slot("dropdown-menu-radio-item");
    const [gutter] = item.children;
    const indicator = page.only(isPrimitive("Menu.ItemIndicator"));
    expect(indicator.parent).toBe(gutter);
    expect(indicator.children[0].classes).toContain("lucide-circle");
    expect(gutter.classes).toContain("left-2");
    expect(item.text).toBe("English");
    expect(given("Menu.RadioItem").value).toBe("en");
  });
});

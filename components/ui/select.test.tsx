import type { ComponentType, ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { draw } from "@/test-support/markup";
import { given, isPrimitive } from "@/test-support/radix";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectScrollDownButton,
  SelectScrollUpButton,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "./select";

/**
 * The select of the site: which Radix primitive each part draws and with
 * what, the two sizes of its button and the chevron that ends it, how the
 * list is placed (under the button, or over it with the chosen item aligned),
 * the scroll buttons and the viewport the list brings with it, the tick of
 * the chosen item.
 *
 * The real primitives need a browser (a portal, a layout). Each is replaced
 * here by a stand-in that draws a plain element in place and keeps the props
 * it was given: what is proven is what `select.tsx` itself decides.
 */

vi.mock("@radix-ui/react-select", async () =>
  (await import("@/test-support/radix")).standIns("Select", {
    Root: "div",
    Group: "div",
    Value: "span",
    Trigger: "button",
    Icon: "span",
    Portal: "div",
    Content: "div",
    Viewport: "div",
    Label: "div",
    Item: "div",
    ItemIndicator: "span",
    ItemText: "span",
    Separator: "div",
    ScrollUpButton: "div",
    ScrollDownButton: "div",
  }),
);

type Part = ComponentType<{ className?: string; children?: ReactNode }>;

/** An item is drawn with the value it stands for. */
function Item(props: { className?: string; children?: ReactNode }) {
  return <SelectItem value="auto" {...props} />;
}

/** The classes that shift the list one step away from its button, whichever side it opens on. */
const AWAY_FROM_THE_BUTTON = [
  "data-[side=bottom]:translate-y-1",
  "data-[side=top]:-translate-y-1",
  "data-[side=left]:-translate-x-1",
  "data-[side=right]:translate-x-1",
];

/** The classes that make the list as wide as its button, at least. */
const AS_WIDE_AS_THE_BUTTON = [
  "w-full",
  "min-w-[var(--radix-select-trigger-width)]",
];

describe("ANH-203 select: the parts", () => {
  it.each<[string, string, Part]>([
    ["select", "Select.Root", Select],
    ["select-group", "Select.Group", SelectGroup],
    ["select-value", "Select.Value", SelectValue],
    ["select-trigger", "Select.Trigger", SelectTrigger],
    ["select-content", "Select.Content", SelectContent],
    ["select-label", "Select.Label", SelectLabel],
    ["select-item", "Select.Item", Item],
    ["select-separator", "Select.Separator", SelectSeparator],
  ])(
    "%s is the primitive %s, with what the caller puts in it",
    (slot, primitive, Part) => {
      const page = draw(<Part>Automatique</Part>);

      expect(page.slot(slot).getAttribute("data-primitive")).toBe(primitive);
      expect(page.slot(slot).text).toBe("Automatique");
    },
  );

  it.each<[string, Part, string, string]>([
    ["select-trigger", SelectTrigger, "w-fit", "w-full"],
    ["select-content", SelectContent, "max-h-96", "max-h-60"],
    ["select-label", SelectLabel, "text-xs", "text-sm"],
    ["select-item", Item, "pl-2", "pl-4"],
    ["select-separator", SelectSeparator, "my-1", "my-2"],
    ["select-scroll-up-button", SelectScrollUpButton, "py-1", "py-2"],
    ["select-scroll-down-button", SelectScrollDownButton, "py-1", "py-2"],
  ])(
    "%s: the caller's class replaces the one that sets the same thing",
    (slot, Part, own, instead) => {
      expect(draw(<Part />).slot(slot).classes).toContain(own);

      const page = draw(<Part className={instead} />);
      expect(page.slot(slot).classes).toContain(instead);
      expect(page.slot(slot).classes).not.toContain(own);
    },
  );

  it("tells the root which value is chosen, and whom to tell when that changes", () => {
    const onValueChange = vi.fn();
    draw(<Select value="auto" onValueChange={onValueChange} disabled />);

    expect(given("Select.Root")).toMatchObject({
      value: "auto",
      onValueChange,
      disabled: true,
    });
  });

  it("tells the value what to show while nothing is chosen", () => {
    draw(<SelectValue placeholder="Choisir un mode" />);

    expect(given("Select.Value").placeholder).toBe("Choisir un mode");
  });
});

describe("ANH-203 select: the button", () => {
  it("is of the default size unless told otherwise", () => {
    const page = draw(<SelectTrigger />);

    expect(page.slot("select-trigger").getAttribute("data-size")).toBe(
      "default",
    );
  });

  it("is small when the caller asks", () => {
    const page = draw(<SelectTrigger size="sm" id="mode" />);

    const trigger = page.slot("select-trigger");
    expect(trigger.getAttribute("data-size")).toBe("sm");
    // The heights read this mark.
    expect(trigger.classes).toContain("data-[size=sm]:h-8");
    expect(trigger.classes).toContain("data-[size=default]:h-9");
    expect(trigger.getAttribute("id")).toBe("mode");
    // The size is the wrapper's own: the primitive never hears of it.
    expect(given("Select.Trigger")).not.toHaveProperty("size");
  });

  it("ends with a chevron, after what the caller puts in it", () => {
    const page = draw(
      <SelectTrigger>
        <SelectValue />
      </SelectTrigger>,
    );

    const [value, chevron] = page.slot("select-trigger").children;
    expect(value).toBe(page.slot("select-value"));
    expect(chevron.localName).toBe("svg");
    expect(chevron.classes).toContain("lucide-chevron-down");
    // The chevron itself is the icon of the select: no element wraps it.
    expect(given("Select.Icon").asChild).toBe(true);
    expect(chevron.getAttribute("data-primitive")).toBe("Select.Icon");
  });
});

describe("ANH-203 select: the list", () => {
  it("draws the list in a portal", () => {
    const page = draw(<SelectContent />);

    const portal = page.only(isPrimitive("Select.Portal"));
    expect(portal.children).toEqual([page.slot("select-content")]);
  });

  it("holds the items in a viewport, between a button to scroll up and one to scroll down", () => {
    const page = draw(
      <SelectContent>
        <b>Automatique</b>
      </SelectContent>,
    );

    const viewport = page.only(isPrimitive("Select.Viewport"));
    expect(page.slot("select-content").children).toEqual([
      page.slot("select-scroll-up-button"),
      viewport,
      page.slot("select-scroll-down-button"),
    ]);
    expect(viewport.text).toBe("Automatique");
    expect(viewport.classes).toContain("overflow-y-auto");
  });

  it("opens under its button, 4 px away, unless told otherwise", () => {
    const page = draw(<SelectContent />);

    expect(given("Select.Content")).toMatchObject({
      position: "popper",
      sideOffset: 4,
    });
    const content = page.slot("select-content").classes;
    for (const name of AWAY_FROM_THE_BUTTON) expect(content).toContain(name);
    const viewport = page.only(isPrimitive("Select.Viewport")).classes;
    for (const name of AS_WIDE_AS_THE_BUTTON) expect(viewport).toContain(name);
  });

  it("opens over its button, the chosen item aligned, when the caller asks", () => {
    const page = draw(<SelectContent position="item-aligned" />);

    expect(given("Select.Content").position).toBe("item-aligned");
    // Radix places the list itself: nothing shifts it, nothing sets its width.
    const content = page.slot("select-content").classes;
    for (const name of AWAY_FROM_THE_BUTTON)
      expect(content).not.toContain(name);
    const viewport = page.only(isPrimitive("Select.Viewport")).classes;
    for (const name of AS_WIDE_AS_THE_BUTTON)
      expect(viewport).not.toContain(name);
    expect(viewport).toContain("p-1");
  });

  it("opens at the distance and on the side the caller asks for", () => {
    draw(<SelectContent sideOffset={12} align="end" />);

    expect(given("Select.Content")).toMatchObject({
      sideOffset: 12,
      align: "end",
    });
  });

  it.each([
    [
      "select-scroll-up-button",
      "Select.ScrollUpButton",
      SelectScrollUpButton,
      "lucide-chevron-up",
    ],
    [
      "select-scroll-down-button",
      "Select.ScrollDownButton",
      SelectScrollDownButton,
      "lucide-chevron-down",
    ],
  ] as const)(
    "%s is the primitive %s, drawn as a chevron",
    (slot, primitive, Part, icon) => {
      const page = draw(<Part id="scroll" />);

      const button = page.slot(slot);
      expect(button.getAttribute("data-primitive")).toBe(primitive);
      expect(button.getAttribute("id")).toBe("scroll");
      expect(button.children.map((child) => child.localName)).toEqual(["svg"]);
      expect(button.children[0].classes).toContain(icon);
    },
  );
});

describe("ANH-203 select: an item", () => {
  it("tells the primitive the value it stands for, and whether it can be chosen", () => {
    draw(
      <SelectItem value="manual" disabled>
        Manuel
      </SelectItem>,
    );

    expect(given("Select.Item")).toMatchObject({
      value: "manual",
      disabled: true,
    });
  });

  it("gives its label to Radix as the text of the item, the one the button shows once chosen", () => {
    const page = draw(<SelectItem value="manual">Manuel</SelectItem>);

    const text = page.only(isPrimitive("Select.ItemText"));
    expect(text.parent).toBe(page.slot("select-item"));
    expect(text.text).toBe("Manuel");
  });

  it("keeps a place at its end for the tick Radix shows when it is chosen", () => {
    const page = draw(<SelectItem value="manual">Manuel</SelectItem>);

    const place = page.slot("select-item-indicator");
    const indicator = page.only(isPrimitive("Select.ItemIndicator"));
    expect(place.parent).toBe(page.slot("select-item"));
    expect(place.classes).toContain("absolute");
    expect(place.classes).toContain("right-2");
    expect(indicator.parent).toBe(place);
    expect(indicator.children[0].classes).toContain("lucide-check");
    // The tick does not add to the text of the item.
    expect(page.slot("select-item").text).toBe("Manuel");
  });
});

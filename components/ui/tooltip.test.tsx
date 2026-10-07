import { describe, expect, it, vi } from "vitest";
import { draw } from "@/test-support/markup";
import { allGiven, given, isPrimitive } from "@/test-support/radix";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "./tooltip";

/**
 * The tooltip of the site: shown at once (no delay), each tooltip bringing its
 * own provider so that a page needs none; the bubble drawn in a portal, right
 * against its trigger, with an arrow.
 *
 * The real primitives need a browser (a portal, a layout). Each is replaced
 * here by a stand-in that draws a plain element in place and keeps the props
 * it was given: what is proven is what `tooltip.tsx` itself decides.
 */

vi.mock("@radix-ui/react-tooltip", async () =>
  (await import("@/test-support/radix")).standIns("Tooltip", {
    Provider: "div",
    Root: "div",
    Trigger: "button",
    Portal: "div",
    Content: "div",
    Arrow: "span",
  }),
);

describe("ANH-203 tooltip: when it shows", () => {
  it("shows without delay unless the caller sets one", () => {
    const page = draw(<TooltipProvider>Page</TooltipProvider>);

    expect(page.slot("tooltip-provider").getAttribute("data-primitive")).toBe(
      "Tooltip.Provider",
    );
    expect(page.slot("tooltip-provider").text).toBe("Page");
    expect(given("Tooltip.Provider").delayDuration).toBe(0);
  });

  it("waits as long as the caller asks", () => {
    draw(
      <TooltipProvider delayDuration={300} skipDelayDuration={0}>
        Page
      </TooltipProvider>,
    );

    expect(given("Tooltip.Provider")).toMatchObject({
      delayDuration: 300,
      skipDelayDuration: 0,
    });
  });

  it("brings its own provider, so that a tooltip works anywhere in a page", () => {
    const page = draw(<Tooltip>Arrêt</Tooltip>);

    const provider = page.slot("tooltip-provider");
    const root = page.slot("tooltip");
    expect(root.getAttribute("data-primitive")).toBe("Tooltip.Root");
    expect(root.parent).toBe(provider);
    expect(root.text).toBe("Arrêt");
    expect(given("Tooltip.Provider").delayDuration).toBe(0);
  });

  it("tells the root whether it is open, and whom to tell when that changes", () => {
    const onOpenChange = vi.fn();
    draw(<Tooltip open onOpenChange={onOpenChange} />);

    expect(given("Tooltip.Root")).toMatchObject({ open: true, onOpenChange });
    // Those are the root's: the provider around it gets none of them.
    expect(given("Tooltip.Provider")).not.toHaveProperty("open");
  });

  it("gives each tooltip a provider of its own", () => {
    draw(
      <>
        <Tooltip />
        <Tooltip />
      </>,
    );

    expect(allGiven("Tooltip.Provider")).toHaveLength(2);
  });
});

describe("ANH-203 tooltip: the trigger and the bubble", () => {
  it("draws the trigger as the primitive, or as the caller's own element", () => {
    const plain = draw(<TooltipTrigger>Arrêt</TooltipTrigger>);
    expect(plain.slot("tooltip-trigger").getAttribute("data-primitive")).toBe(
      "Tooltip.Trigger",
    );
    expect(plain.slot("tooltip-trigger").text).toBe("Arrêt");

    const own = draw(
      <TooltipTrigger asChild>
        <a href="#stop">Arrêt</a>
      </TooltipTrigger>,
    );
    expect(given("Tooltip.Trigger").asChild).toBe(true);
    expect(own.slot("tooltip-trigger").localName).toBe("a");
  });

  it("draws the bubble in a portal", () => {
    const page = draw(<TooltipContent>Arrêter la séance</TooltipContent>);

    const portal = page.only(isPrimitive("Tooltip.Portal"));
    const bubble = page.slot("tooltip-content");
    expect(bubble.getAttribute("data-primitive")).toBe("Tooltip.Content");
    expect(portal.children).toEqual([bubble]);
  });

  it("ends the bubble with an arrow, after its text", () => {
    const page = draw(
      <TooltipContent>
        <b>Arrêter la séance</b>
      </TooltipContent>,
    );

    const [text, arrow] = page.slot("tooltip-content").children;
    expect(text.text).toBe("Arrêter la séance");
    expect(arrow).toBe(page.only(isPrimitive("Tooltip.Arrow")));
    // The arrow is of the colour of the bubble.
    expect(arrow.classes).toContain("fill-foreground");
    expect(page.slot("tooltip-content").classes).toContain("bg-foreground");
  });

  it("sits right against its trigger unless told otherwise", () => {
    draw(<TooltipContent />);

    expect(given("Tooltip.Content").sideOffset).toBe(0);
  });

  it("sits at the distance and on the side the caller asks for", () => {
    const page = draw(<TooltipContent sideOffset={8} side="right" hidden />);

    expect(given("Tooltip.Content")).toMatchObject({
      sideOffset: 8,
      side: "right",
    });
    expect(page.slot("tooltip-content").hasAttribute("hidden")).toBe(true);
  });

  it("lets the caller's class replace the one of the bubble", () => {
    const page = draw(<TooltipContent className="text-sm" />);

    expect(page.slot("tooltip-content").classes).toContain("text-sm");
    expect(page.slot("tooltip-content").classes).not.toContain("text-xs");
  });
});

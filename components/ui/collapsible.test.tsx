import { describe, expect, it } from "vitest";
import { draw } from "@/test-support/markup";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "./collapsible";

/**
 * The block that folds, on the real Radix primitives, drawn as the server
 * draws it: a button that says whether the block is open, and a content shown
 * only when it is. The three parts add nothing but the mark the styles of the
 * sidebar select them by.
 */

function block(props: { defaultOpen?: boolean; disabled?: boolean } = {}) {
  return draw(
    <Collapsible id="details" {...props}>
      <CollapsibleTrigger>Détails</CollapsibleTrigger>
      <CollapsibleContent>Dernier signal il y a 8 s</CollapsibleContent>
    </Collapsible>,
  );
}

describe("ANH-203 collapsible", () => {
  it("marks its three parts, the button and the content inside the root", () => {
    const page = block({ defaultOpen: true });

    const root = page.slot("collapsible");
    expect(root.getAttribute("id")).toBe("details");
    expect(page.slot("collapsible-trigger").localName).toBe("button");
    expect(page.slot("collapsible-trigger").parent).toBe(root);
    expect(page.slot("collapsible-content").parent).toBe(root);
  });

  it("is closed unless the caller opens it: the content is not shown", () => {
    const page = block();

    expect(page.slot("collapsible").getAttribute("data-state")).toBe("closed");
    expect(page.slot("collapsible-trigger").getAttribute("aria-expanded")).toBe(
      "false",
    );
    expect(page.slot("collapsible-content").hasAttribute("hidden")).toBe(true);
    expect(page.text).toBe("Détails");
  });

  it("shows the content when the caller opens it", () => {
    const page = block({ defaultOpen: true });

    expect(page.slot("collapsible").getAttribute("data-state")).toBe("open");
    expect(page.slot("collapsible-trigger").getAttribute("aria-expanded")).toBe(
      "true",
    );
    expect(page.slot("collapsible-content").text).toBe(
      "Dernier signal il y a 8 s",
    );
  });

  it("ties the button to the content it opens", () => {
    const page = block({ defaultOpen: true });

    expect(page.slot("collapsible-trigger").getAttribute("aria-controls")).toBe(
      page.slot("collapsible-content").getAttribute("id"),
    );
  });

  it("disables the button when the caller disables the block", () => {
    const page = block({ disabled: true });

    expect(page.slot("collapsible-trigger").hasAttribute("disabled")).toBe(
      true,
    );
  });

  it("draws the caller's own element as the button when asked", () => {
    const page = draw(
      <Collapsible>
        <CollapsibleTrigger asChild>
          <a href="#details">Détails</a>
        </CollapsibleTrigger>
        <CollapsibleContent className="mine" forceMount>
          Contenu
        </CollapsibleContent>
      </Collapsible>,
    );

    const trigger = page.slot("collapsible-trigger");
    expect(trigger.localName).toBe("a");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(page.tag("button")).toEqual([]);
    // The props of the content reach the primitive: kept in the page though closed.
    expect(page.slot("collapsible-content").classes).toEqual(["mine"]);
    expect(page.slot("collapsible-content").text).toBe("Contenu");
  });
});

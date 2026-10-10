import { describe, expect, it } from "vitest";
import { draw } from "@/test-support/markup";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "./accordion";

/**
 * The accordion of the help pages, on the real Radix primitives, drawn as the
 * server draws it: a list of questions, each a heading that holds a button
 * with a chevron, and under it an answer shown only when its question is
 * open.
 */

function faq(open?: string) {
  return draw(
    <Accordion type="single" collapsible defaultValue={open}>
      <AccordionItem value="stop">
        <AccordionTrigger>Comment arrêter ?</AccordionTrigger>
        <AccordionContent>Le bouton rouge.</AccordionContent>
      </AccordionItem>
      <AccordionItem value="start">
        <AccordionTrigger>Comment lancer ?</AccordionTrigger>
        <AccordionContent>Le bouton vert.</AccordionContent>
      </AccordionItem>
    </Accordion>,
  );
}

describe("ANH-203 accordion: what is drawn", () => {
  it("holds its items, each closed until the caller opens one", () => {
    const page = faq();

    const items = page.slots("accordion-item");
    expect(items.map((item) => item.parent)).toEqual([
      page.slot("accordion"),
      page.slot("accordion"),
    ]);
    expect(items.map((item) => item.getAttribute("data-state"))).toEqual([
      "closed",
      "closed",
    ]);
    expect(page.text).toBe("Comment arrêter ? Comment lancer ?");
  });

  it("opens the item the caller names, and shows its answer only", () => {
    const page = faq("start");

    expect(
      page
        .slots("accordion-item")
        .map((item) => item.getAttribute("data-state")),
    ).toEqual(["closed", "open"]);
    const [closed, open] = page.slots("accordion-content");
    expect(open.text).toBe("Le bouton vert.");
    expect(closed.text).toBe("");
    expect(closed.hasAttribute("hidden")).toBe(true);
  });

  it("draws each question as a button inside a heading, followed by a chevron", () => {
    const page = faq();

    const [trigger] = page.slots("accordion-trigger");
    expect(trigger.localName).toBe("button");
    expect(trigger.parent?.localName).toBe("h3");
    // The heading is a flex row: the button fills it.
    expect(trigger.parent?.classes).toEqual(["flex"]);
    expect(trigger.text).toBe("Comment arrêter ?");
    const [chevron] = trigger.children;
    expect(chevron.classes).toContain("lucide-chevron-down");
    // The chevron turns over when the question is open.
    expect(trigger.classes).toContain("[&[data-state=open]>svg]:rotate-180");
  });

  it("ties each button to the answer it opens", () => {
    const page = faq("stop");

    const [trigger] = page.slots("accordion-trigger");
    const [content] = page.slots("accordion-content");
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(trigger.getAttribute("aria-controls")).toBe(
      content.getAttribute("id"),
    );
    expect(content.getAttribute("aria-labelledby")).toBe(
      trigger.getAttribute("id"),
    );
  });
});

describe("ANH-203 accordion: the caller's classes and props", () => {
  it("separates the items with a line, which the caller's class can replace", () => {
    const page = draw(
      <Accordion type="multiple">
        <AccordionItem value="a" />
        <AccordionItem value="b" className="border-b-2" id="second" />
      </Accordion>,
    );

    const [first, second] = page.slots("accordion-item");
    expect(first.classes).toContain("border-b");
    expect(second.classes).toContain("border-b-2");
    expect(second.classes).not.toContain("border-b");
    expect(second.getAttribute("id")).toBe("second");
  });

  it("lets the caller's class replace the one of the button, and disable it", () => {
    const page = draw(
      <Accordion type="multiple">
        <AccordionItem value="a">
          <AccordionTrigger className="py-2" disabled>
            Question
          </AccordionTrigger>
        </AccordionItem>
      </Accordion>,
    );

    const trigger = page.slot("accordion-trigger");
    expect(trigger.classes).toContain("py-2");
    expect(trigger.classes).not.toContain("py-4");
    expect(trigger.hasAttribute("disabled")).toBe(true);
  });

  it("gives the caller's class to the padding inside the answer, not to the part that folds", () => {
    const page = draw(
      <Accordion type="multiple" defaultValue={["a"]}>
        <AccordionItem value="a">
          <AccordionContent className="pb-0 mine">Réponse</AccordionContent>
        </AccordionItem>
      </Accordion>,
    );

    const content = page.slot("accordion-content");
    const [padding] = content.children;
    expect(padding.text).toBe("Réponse");
    expect(padding.classes).toContain("mine");
    expect(padding.classes).toContain("pb-0");
    expect(padding.classes).not.toContain("pb-4");
    // The part that folds keeps its own classes: the animation needs them.
    expect(content.classes).not.toContain("mine");
    expect(content.classes).toContain("overflow-hidden");
  });

  it("passes the root's props on to the primitive", () => {
    const page = draw(
      <Accordion type="multiple" id="faq" orientation="horizontal" />,
    );

    expect(page.slot("accordion").getAttribute("id")).toBe("faq");
    expect(page.slot("accordion").getAttribute("data-orientation")).toBe(
      "horizontal",
    );
  });
});

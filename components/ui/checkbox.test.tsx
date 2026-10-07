import { describe, expect, it } from "vitest";
import { draw } from "@/test-support/markup";
import { Checkbox } from "./checkbox";

/**
 * The checkbox, on the real Radix primitive, drawn as the server draws it: a
 * button that says whether it is checked, with a check mark inside when it
 * is.
 */

describe("ANH-203 checkbox", () => {
  it("is a button a screen reader announces as a checkbox, not checked", () => {
    const page = draw(<Checkbox aria-label="Tout sélectionner" />);

    const box = page.slot("checkbox");
    expect(box.localName).toBe("button");
    expect(box.getAttribute("role")).toBe("checkbox");
    expect(box.getAttribute("aria-checked")).toBe("false");
    expect(box.getAttribute("aria-label")).toBe("Tout sélectionner");
  });

  it("shows no check mark while it is not checked", () => {
    const page = draw(<Checkbox />);

    expect(page.slots("checkbox-indicator")).toEqual([]);
    expect(page.tag("svg")).toEqual([]);
  });

  it("shows a check mark inside the box once checked", () => {
    const page = draw(<Checkbox checked />);

    const box = page.slot("checkbox");
    const indicator = page.slot("checkbox-indicator");
    expect(box.getAttribute("aria-checked")).toBe("true");
    expect(indicator.parent).toBe(box);
    expect(indicator.children.map((child) => child.localName)).toEqual(["svg"]);
    expect(indicator.children[0].classes).toContain("lucide-check");
    // The box is painted when checked: the class reads the state Radix writes.
    expect(box.getAttribute("data-state")).toBe("checked");
    expect(box.classes).toContain("data-[state=checked]:bg-primary");
  });

  it("is disabled when the caller disables it", () => {
    const page = draw(<Checkbox disabled />);

    expect(page.slot("checkbox").hasAttribute("disabled")).toBe(true);
  });

  it("lets the caller's class replace the one it sets for the same thing", () => {
    const page = draw(<Checkbox className="size-5" id="all" />);

    expect(page.slot("checkbox").classes).toContain("size-5");
    expect(page.slot("checkbox").classes).not.toContain("size-4");
    expect(page.slot("checkbox").getAttribute("id")).toBe("all");
  });
});

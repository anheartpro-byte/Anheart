import { describe, expect, it } from "vitest";
import { draw } from "@/test-support/markup";
import { Label } from "./label";

/**
 * The label of a field, on the real Radix primitive: a `label` tied to its
 * field, dimmed when the field next to it is disabled.
 */

describe("ANH-203 label", () => {
  it("is a label tied to the field it names", () => {
    const page = draw(<Label htmlFor="phys-hrmax">FC max</Label>);

    const label = page.slot("label");
    expect(label.localName).toBe("label");
    expect(label.getAttribute("for")).toBe("phys-hrmax");
    expect(label.text).toBe("FC max");
  });

  it("is dimmed next to a disabled field", () => {
    const page = draw(<Label />);

    // The field before it is marked `peer`: its disabled state reaches the label.
    expect(page.slot("label").classes).toContain("peer-disabled:opacity-50");
  });

  it("lets the caller's class replace the one it sets for the same thing", () => {
    const page = draw(<Label className="text-base" id="l" />);

    expect(page.slot("label").classes).toContain("text-base");
    expect(page.slot("label").classes).not.toContain("text-sm");
    expect(page.slot("label").getAttribute("id")).toBe("l");
  });
});

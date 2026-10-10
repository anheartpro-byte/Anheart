import { describe, expect, it } from "vitest";
import { draw } from "@/test-support/markup";
import { Separator } from "./separator";

/**
 * The line between two blocks, on the real Radix primitive. By default it is
 * horizontal and decorative: a screen reader does not announce it. A caller
 * can make it vertical, and can make it a separator that is announced.
 */

describe("ANH-203 separator", () => {
  it("is a horizontal line a screen reader skips, unless told otherwise", () => {
    const page = draw(<Separator />);

    const line = page.slot("separator");
    expect(line.getAttribute("data-orientation")).toBe("horizontal");
    expect(line.getAttribute("role")).toBe("none");
  });

  it("is announced as a separator when it is not decorative", () => {
    const page = draw(<Separator decorative={false} />);

    expect(page.slot("separator").getAttribute("role")).toBe("separator");
  });

  it("stands upright when vertical, and says so to a screen reader that hears it", () => {
    const page = draw(<Separator orientation="vertical" decorative={false} />);

    const line = page.slot("separator");
    expect(line.getAttribute("data-orientation")).toBe("vertical");
    expect(line.getAttribute("aria-orientation")).toBe("vertical");
  });

  it("is one pixel thick in the direction it runs across", () => {
    const page = draw(<Separator />);

    // Radix writes the orientation; these classes turn it into a size.
    expect(page.slot("separator").classes).toContain(
      "data-[orientation=horizontal]:h-px",
    );
    expect(page.slot("separator").classes).toContain(
      "data-[orientation=vertical]:w-px",
    );
  });

  it("lets the caller's class replace the one it sets for the same thing", () => {
    const page = draw(<Separator className="bg-primary" id="rule" />);

    expect(page.slot("separator").classes).toContain("bg-primary");
    expect(page.slot("separator").classes).not.toContain("bg-border");
    expect(page.slot("separator").getAttribute("id")).toBe("rule");
  });
});

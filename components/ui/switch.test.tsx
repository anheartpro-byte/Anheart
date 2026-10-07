import { describe, expect, it } from "vitest";
import { draw } from "@/test-support/markup";
import { Switch } from "./switch";

/**
 * The on/off switch, on the real Radix primitive, drawn as the server draws
 * it: a button that says whether it is on, with a thumb inside that slides
 * with the state.
 */

describe("ANH-203 switch", () => {
  it("is a button a screen reader announces as a switch, off", () => {
    const page = draw(<Switch aria-label="Mode sombre" />);

    const control = page.slot("switch");
    expect(control.localName).toBe("button");
    expect(control.getAttribute("role")).toBe("switch");
    expect(control.getAttribute("aria-checked")).toBe("false");
    expect(control.getAttribute("aria-label")).toBe("Mode sombre");
  });

  it("holds a thumb that follows the state of the switch", () => {
    const off = draw(<Switch />);
    const on = draw(<Switch checked />);

    expect(off.slot("switch-thumb").parent).toBe(off.slot("switch"));
    expect(off.slot("switch-thumb").getAttribute("data-state")).toBe(
      "unchecked",
    );
    expect(on.slot("switch").getAttribute("aria-checked")).toBe("true");
    expect(on.slot("switch-thumb").getAttribute("data-state")).toBe("checked");
    // The thumb slides to the right when on: the class reads the state Radix writes.
    expect(on.slot("switch-thumb").classes).toContain(
      "data-[state=checked]:translate-x-4",
    );
  });

  it("is disabled when the caller disables it", () => {
    const page = draw(<Switch disabled />);

    expect(page.slot("switch").hasAttribute("disabled")).toBe(true);
  });

  it("lets the caller's class replace the one of the switch, not of the thumb", () => {
    const page = draw(<Switch className="w-11" id="dark" />);

    expect(page.slot("switch").classes).toContain("w-11");
    expect(page.slot("switch").classes).not.toContain("w-9");
    expect(page.slot("switch").getAttribute("id")).toBe("dark");
    expect(page.slot("switch-thumb").classes).not.toContain("w-11");
  });
});

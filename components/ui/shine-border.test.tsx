import { render } from "@/test-support/render";
import { describe, expect, it } from "vitest";
import { ShineBorder } from "./shine-border";

/**
 * The border of light around a card: its width, the time of one turn and its
 * colors, written in the style of one element from its props.
 */

function drawn(ui: React.ReactElement) {
  return render(ui).container.children[0];
}

describe("ANH-203 shine border", () => {
  it("is one pixel wide, black, and turns in 14 seconds unless told otherwise", () => {
    const { style } = drawn(<ShineBorder />);

    expect(style["--border-width"]).toBe("1px");
    expect(style["--duration"]).toBe("14s");
    expect(style.backgroundImage).toBe(
      "radial-gradient(transparent,transparent, #000000,transparent,transparent)",
    );
    // The border is the padding left around a mask of the content.
    expect(style.padding).toBe("var(--border-width)");
  });

  it("takes the width and the time of a turn it is given", () => {
    const { style } = drawn(<ShineBorder borderWidth={3} duration={6} />);

    expect(style["--border-width"]).toBe("3px");
    expect(style["--duration"]).toBe("6s");
  });

  it("shines in one color, or in several one after the other", () => {
    expect(
      drawn(<ShineBorder shineColor="#f97316" />).style.backgroundImage,
    ).toBe(
      "radial-gradient(transparent,transparent, #f97316,transparent,transparent)",
    );
    expect(
      drawn(<ShineBorder shineColor={["#A07CFE", "#FE8FB5", "#FFBE7B"]} />)
        .style.backgroundImage,
    ).toBe(
      "radial-gradient(transparent,transparent, #A07CFE,#FE8FB5,#FFBE7B,transparent,transparent)",
    );
  });

  it("lets the page override its style, and keeps the classes and attributes it is given", () => {
    const border = drawn(
      <ShineBorder
        style={{ backgroundSize: "200% 200%", opacity: 0.5 }}
        className="rounded-xl"
        id="glow"
        aria-hidden
      />,
    );

    expect(border.style.backgroundSize).toBe("200% 200%");
    expect(border.style.opacity).toBe("0.5");
    // What the page did not override stays.
    expect(border.style["--duration"]).toBe("14s");
    expect(border.id).toBe("glow");
    expect(border.getAttribute("aria-hidden")).toBe("true");
    expect(border.className.split(" ")).toEqual(
      expect.arrayContaining(["rounded-xl", "pointer-events-none"]),
    );
  });
});

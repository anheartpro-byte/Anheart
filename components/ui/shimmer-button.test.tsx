import { click, render } from "@/test-support/render";
import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { ShimmerButton } from "./shimmer-button";

/**
 * The call-to-action button with a light that runs around its border: the
 * CSS variables that set that light from its props, and that it stays an
 * ordinary button for the page that uses it.
 */

/** The variables the styles of the button read. */
function variables(ui: React.ReactElement) {
  const screen = render(ui);
  const { style } = screen.tag("button")[0];
  return {
    color: style["--shimmer-color"],
    cut: style["--cut"],
    speed: style["--speed"],
    radius: style["--radius"],
    background: style["--bg"],
    spread: style["--spread"],
  };
}

describe("ANH-203 shimmer button", () => {
  it("shines in white on black, once every three seconds, unless told otherwise", () => {
    expect(variables(<ShimmerButton>Book a visit</ShimmerButton>)).toEqual({
      color: "#ffffff",
      cut: "0.05em",
      speed: "3s",
      radius: "100px",
      background: "rgba(0, 0, 0, 1)",
      spread: "90deg",
    });
  });

  it("takes the color, the width, the speed, the radius and the background it is given", () => {
    expect(
      variables(
        <ShimmerButton
          shimmerColor="#f97316"
          shimmerSize="0.1em"
          shimmerDuration="1.5s"
          borderRadius="8px"
          background="#0f172a"
        >
          Book a visit
        </ShimmerButton>,
      ),
    ).toEqual({
      color: "#f97316",
      cut: "0.1em",
      speed: "1.5s",
      radius: "8px",
      background: "#0f172a",
      // The width of the beam of light is not a prop.
      spread: "90deg",
    });
  });

  it("is an ordinary button: its label, its click, its attributes, its classes", async () => {
    const onClick = vi.fn();
    const screen = render(
      <ShimmerButton
        onClick={onClick}
        type="submit"
        className="w-full"
        aria-label="Book"
      >
        Book a visit
      </ShimmerButton>,
    );
    const button = screen.button("Book a visit");

    await click(button);

    expect(onClick).toHaveBeenCalledTimes(1);
    expect(button.getAttribute("type")).toBe("submit");
    expect(button.getAttribute("aria-label")).toBe("Book");
    expect(button.className.split(" ")).toContain("w-full");
  });

  it("cannot be clicked once disabled", async () => {
    const onClick = vi.fn();
    const screen = render(
      <ShimmerButton onClick={onClick} disabled>
        Book a visit
      </ShimmerButton>,
    );

    await click(screen.button("Book a visit"));

    expect(screen.button("Book a visit").hasAttribute("disabled")).toBe(true);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("hands the button itself to the page that asks for it", () => {
    const ref = createRef<HTMLButtonElement>();

    const screen = render(
      <ShimmerButton ref={ref}>Book a visit</ShimmerButton>,
    );

    expect((ref.current as unknown) === screen.button("Book a visit")).toBe(
      true,
    );
  });
});

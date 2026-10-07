import { render } from "@/test-support/render";
import { describe, expect, it } from "vitest";
import { AnimatedGradientText } from "./animated-gradient-text";

/**
 * The words of a title colored by a gradient that slides: the two colors and
 * the speed of the slide, written as CSS variables from its props.
 */

function drawn(ui: React.ReactElement) {
  return render(ui).container.children[0];
}

describe("ANH-203 animated gradient text", () => {
  it("slides from orange to violet at the usual speed unless told otherwise", () => {
    const text = drawn(
      <AnimatedGradientText>Train at altitude</AnimatedGradientText>,
    );

    expect(text.localName).toBe("span");
    expect(text.textContent).toBe("Train at altitude");
    expect(text.style["--bg-size"]).toBe("300%");
    expect(text.style["--color-from"]).toBe("#ffaa40");
    expect(text.style["--color-to"]).toBe("#9c40ff");
  });

  it.each([
    [2, "600%"],
    [0.5, "150%"],
  ])("stretches the gradient with the speed: %s gives %s", (speed, size) => {
    const text = drawn(
      <AnimatedGradientText speed={speed}>Train</AnimatedGradientText>,
    );

    expect(text.style["--bg-size"]).toBe(size);
  });

  it("takes the two colors it is given", () => {
    const text = drawn(
      <AnimatedGradientText colorFrom="#0ea5e9" colorTo="#22c55e">
        Train
      </AnimatedGradientText>,
    );

    expect(text.style["--color-from"]).toBe("#0ea5e9");
    expect(text.style["--color-to"]).toBe("#22c55e");
  });

  it("keeps the classes and the attributes it is given", () => {
    const text = drawn(
      <AnimatedGradientText className="text-4xl" id="headline" lang="en">
        Train
      </AnimatedGradientText>,
    );

    expect(text.id).toBe("headline");
    expect(text.getAttribute("lang")).toBe("en");
    expect(text.className.split(" ")).toEqual(
      expect.arrayContaining(["text-4xl", "animate-gradient"]),
    );
  });
});

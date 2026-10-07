import { click, render } from "@/test-support/render";
import { describe, expect, it, vi } from "vitest";
import { draw } from "@/test-support/markup";
import { Button, buttonVariants } from "./button";

/**
 * The button of the site: six ways to paint it, six sizes, drawn as a
 * `button` or, with `asChild`, as the element the caller gives it (a link).
 * The variant and the size are also written on the element, where the styles
 * of a parent and the tests of a page read them.
 */

/** Each variant with a class that paints it and no other variant. */
const VARIANTS = [
  ["default", "bg-primary"],
  ["destructive", "bg-destructive"],
  ["outline", "bg-background"],
  ["secondary", "bg-secondary"],
  ["ghost", "dark:hover:bg-accent/50"],
  ["link", "underline-offset-4"],
] as const;

/** Each size with the class that sets it and no other size. */
const SIZES = [
  ["default", "h-9"],
  ["sm", "h-8"],
  ["lg", "h-10"],
  ["icon", "size-9"],
  ["icon-sm", "size-8"],
  ["icon-lg", "size-10"],
] as const;

describe("ANH-203 button", () => {
  it("is a button that holds its label", () => {
    const page = draw(<Button>Lancer</Button>);

    expect(page.slot("button").localName).toBe("button");
    expect(page.slot("button").text).toBe("Lancer");
  });

  it("is of the default variant and size unless told otherwise", () => {
    const page = draw(<Button />);

    const button = page.slot("button");
    expect(button.getAttribute("data-variant")).toBe("default");
    expect(button.getAttribute("data-size")).toBe("default");
    expect(button.classes).toContain("bg-primary");
    expect(button.classes).toContain("h-9");
  });

  it.each(VARIANTS)(
    "variant %s: says so and carries its own paint only",
    (variant, paint) => {
      const page = draw(<Button variant={variant} />);

      const button = page.slot("button");
      expect(button.getAttribute("data-variant")).toBe(variant);
      expect(button.classes).toContain(paint);
      for (const [, other] of VARIANTS) {
        if (other !== paint) expect(button.classes).not.toContain(other);
      }
    },
  );

  it.each(SIZES)(
    "size %s: says so and carries its own size only",
    (size, measure) => {
      const page = draw(<Button size={size} />);

      const button = page.slot("button");
      expect(button.getAttribute("data-size")).toBe(size);
      expect(button.classes).toContain(measure);
      for (const [, other] of SIZES) {
        if (other !== measure) expect(button.classes).not.toContain(other);
      }
    },
  );

  it("lets the caller's class replace the one of the size", () => {
    const page = draw(<Button className="h-12" />);

    expect(page.slot("button").classes).toContain("h-12");
    expect(page.slot("button").classes).not.toContain("h-9");
  });

  it("passes the button's props on: type, disabled, label for a screen reader", () => {
    const page = draw(
      <Button type="submit" disabled aria-label="Enregistrer" />,
    );

    const button = page.slot("button");
    expect(button.getAttribute("type")).toBe("submit");
    expect(button.hasAttribute("disabled")).toBe(true);
    expect(button.getAttribute("aria-label")).toBe("Enregistrer");
  });
});

describe("ANH-203 button: a click", () => {
  it("calls the caller's handler once per click", async () => {
    const onClick = vi.fn();
    const screen = render(<Button onClick={onClick}>Lancer</Button>);

    await click(screen.button("Lancer"));
    await click(screen.button("Lancer"));

    expect(onClick).toHaveBeenCalledTimes(2);
  });

  it("is drawn disabled when the caller disables it, so a click does nothing", async () => {
    const onClick = vi.fn();
    const screen = render(
      <Button onClick={onClick} disabled>
        Lancer
      </Button>,
    );

    expect(screen.button("Lancer").hasAttribute("disabled")).toBe(true);
    await click(screen.button("Lancer"));
    expect(onClick).not.toHaveBeenCalled();
  });
});

describe("ANH-203 button drawn as the caller's element", () => {
  it("draws the child in place of the button, with the button's paint and marks", () => {
    const page = draw(
      <Button asChild variant="link" size="sm">
        <a href="#session" className="italic">
          Voir la séance
        </a>
      </Button>,
    );

    const link = page.slot("button");
    expect(link.localName).toBe("a");
    expect(link.getAttribute("href")).toBe("#session");
    expect(link.text).toBe("Voir la séance");
    expect(link.getAttribute("data-variant")).toBe("link");
    expect(link.getAttribute("data-size")).toBe("sm");
    expect(link.classes).toContain("underline-offset-4");
    expect(link.classes).toContain("h-8");
    // The child keeps what it brought.
    expect(link.classes).toContain("italic");
    expect(page.tag("button")).toEqual([]);
  });
});

describe("ANH-203 buttonVariants", () => {
  it("gives another element the paint and the size of a button", () => {
    const classes = buttonVariants({ variant: "outline", size: "lg" }).split(
      " ",
    );

    expect(classes).toContain("bg-background");
    expect(classes).toContain("h-10");
    expect(classes).not.toContain("bg-primary");
    expect(classes).not.toContain("h-9");
  });

  it("falls back on the default variant and size", () => {
    const classes = buttonVariants().split(" ");

    expect(classes).toContain("bg-primary");
    expect(classes).toContain("h-9");
  });
});

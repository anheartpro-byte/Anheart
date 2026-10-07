import { describe, expect, it } from "vitest";
import { draw } from "@/test-support/markup";
import { Badge, badgeVariants } from "./badge";

/**
 * The badge: a small label painted in one of four ways, drawn as a `span` or,
 * with `asChild`, as the element the caller gives it (a link, most often).
 */

/** Each variant with the class that paints it and no other variant. */
const VARIANTS = [
  ["default", "bg-primary"],
  ["secondary", "bg-secondary"],
  ["destructive", "bg-destructive"],
  ["outline", "text-foreground"],
] as const;

describe("ANH-203 badge", () => {
  it("is a span that holds its label", () => {
    const page = draw(<Badge>En ligne</Badge>);

    expect(page.slot("badge").localName).toBe("span");
    expect(page.slot("badge").text).toBe("En ligne");
  });

  it("is painted as the default variant unless told otherwise", () => {
    const page = draw(<Badge />);

    expect(page.slot("badge").classes).toContain("bg-primary");
  });

  it.each(VARIANTS)(
    "%s: carries its own paint and no other variant's",
    (variant, paint) => {
      const page = draw(<Badge variant={variant} />);

      const classes = page.slot("badge").classes;
      expect(classes).toContain(paint);
      for (const [, other] of VARIANTS) {
        if (other !== paint) expect(classes).not.toContain(other);
      }
    },
  );

  it("lets the caller's class replace the one it sets for the same thing", () => {
    const page = draw(<Badge className="px-4" id="status" />);

    expect(page.slot("badge").classes).toContain("px-4");
    expect(page.slot("badge").classes).not.toContain("px-2");
    expect(page.slot("badge").getAttribute("id")).toBe("status");
  });
});

describe("ANH-203 badge drawn as the caller's element", () => {
  it("draws the child in place of the span, with the badge's paint", () => {
    const page = draw(
      <Badge asChild variant="secondary">
        <a href="#machines" className="underline">
          3 machines
        </a>
      </Badge>,
    );

    const badge = page.slot("badge");
    expect(badge.localName).toBe("a");
    expect(badge.getAttribute("href")).toBe("#machines");
    expect(badge.text).toBe("3 machines");
    expect(badge.classes).toContain("bg-secondary");
    // The child keeps what it brought.
    expect(badge.classes).toContain("underline");
    expect(page.tag("span")).toEqual([]);
  });
});

describe("ANH-203 badgeVariants", () => {
  it.each(VARIANTS)(
    "%s: gives another element the paint of the badge",
    (variant, paint) => {
      const classes = badgeVariants({ variant }).split(" ");

      expect(classes).toContain(paint);
      expect(classes).toContain("rounded-full");
    },
  );
});

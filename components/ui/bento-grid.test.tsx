import { describe, expect, it } from "vitest";
import { draw } from "@/test-support/markup";
import { BentoCard, BentoGrid } from "./bento-grid";

/**
 * The grid of feature cards of the landing page. A card shows a background, an
 * icon, a name, a description, and a link to follow: drawn twice, once for a
 * small screen (always visible) and once for a large one (shown when the card
 * is hovered).
 */

function Icon({ className }: { className?: string }) {
  return <i data-icon className={className} />;
}

function card(props: { className?: string; id?: string } = {}) {
  return draw(
    <BentoCard
      name="Suivi en direct"
      description="Chaque séance, seconde par seconde."
      href="/training"
      cta="Voir une séance"
      Icon={Icon}
      background={<i data-background="live" />}
      className=""
      {...props}
    />,
  );
}

describe("ANH-203 bento grid", () => {
  it("lays its cards out on three columns", () => {
    const page = draw(
      <BentoGrid id="features">
        <b>one</b>
        <b>two</b>
      </BentoGrid>,
    );

    const [grid] = page.children;
    expect(grid.localName).toBe("div");
    expect(grid.classes).toContain("grid");
    expect(grid.classes).toContain("grid-cols-3");
    expect(grid.getAttribute("id")).toBe("features");
    expect(grid.children.map((child) => child.text)).toEqual(["one", "two"]);
  });

  it("lets the caller choose another number of columns", () => {
    const page = draw(<BentoGrid className="grid-cols-1">x</BentoGrid>);

    expect(page.children[0].classes).toContain("grid-cols-1");
    expect(page.children[0].classes).not.toContain("grid-cols-3");
  });
});

describe("ANH-203 bento card", () => {
  it("shows the name as a heading, the description, and the background", () => {
    const page = card();

    const [heading] = page.tag("h3");
    expect(heading.text).toBe("Suivi en direct");
    expect(page.tag("p").map((p) => p.text)).toEqual([
      "Chaque séance, seconde par seconde.",
    ]);
    const [background] = page.all((element) =>
      element.hasAttribute("data-background"),
    );
    // The background comes first: the text is drawn over it.
    expect(page.children[0].children[0].children).toEqual([background]);
  });

  it("draws the icon the caller gives, at the size of the card", () => {
    const page = card();

    const [icon] = page.all((element) => element.hasAttribute("data-icon"));
    expect(icon.classes).toContain("h-12");
    expect(icon.classes).toContain("w-12");
    // Above the name, in the same block.
    expect(icon.parent).toBe(page.tag("h3")[0].parent);
  });

  it("links to the page given, once for a small screen and once for a large one", () => {
    const page = card();

    const links = page.tag("a");
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/training",
      "/training",
    ]);
    expect(links.map((link) => link.text)).toEqual([
      "Voir une séance",
      "Voir une séance",
    ]);
    const [small, large] = links.map((link) => link.parent?.classes ?? []);
    expect(small).toContain("lg:hidden");
    expect(large).toContain("lg:flex");
    expect(large).toContain("hidden");
  });

  it("draws each link as a small link button with an arrow, and no button around it", () => {
    const page = card();

    for (const link of page.tag("a")) {
      expect(link.getAttribute("data-slot")).toBe("button");
      expect(link.getAttribute("data-variant")).toBe("link");
      expect(link.getAttribute("data-size")).toBe("sm");
      // The link stays clickable in a block that lets the pointer through.
      expect(link.classes).toContain("pointer-events-auto");
      expect(link.children.map((child) => child.localName)).toEqual(["svg"]);
    }
    expect(page.tag("button")).toEqual([]);
  });

  it("spans the three columns unless the caller says otherwise", () => {
    expect(card().children[0].classes).toContain("col-span-3");

    const narrow = card({ className: "col-span-1", id: "live" }).children[0];
    expect(narrow.classes).toContain("col-span-1");
    expect(narrow.classes).not.toContain("col-span-3");
    expect(narrow.getAttribute("id")).toBe("live");
  });
});

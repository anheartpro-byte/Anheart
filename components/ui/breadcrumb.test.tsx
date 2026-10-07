import { describe, expect, it } from "vitest";
import { draw } from "@/test-support/markup";
import {
  Breadcrumb,
  BreadcrumbEllipsis,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "./breadcrumb";

/**
 * The breadcrumb trail: a navigation landmark that holds an ordered list of
 * links, the current page said to be current and not a link to follow, and
 * between them separators and an ellipsis a screen reader skips.
 */

describe("ANH-203 breadcrumb: the trail", () => {
  it("is a navigation landmark named breadcrumb", () => {
    const page = draw(<Breadcrumb />);

    expect(page.slot("breadcrumb").localName).toBe("nav");
    expect(page.slot("breadcrumb").getAttribute("aria-label")).toBe(
      "breadcrumb",
    );
  });

  it("takes the name the caller gives it, in the language of the page", () => {
    const page = draw(
      <Breadcrumb aria-label="Fil d'Ariane" className="mb-4" />,
    );

    expect(page.slot("breadcrumb").getAttribute("aria-label")).toBe(
      "Fil d'Ariane",
    );
    expect(page.slot("breadcrumb").classes).toEqual(["mb-4"]);
  });

  it("holds an ordered list of items", () => {
    const page = draw(
      <Breadcrumb>
        <BreadcrumbList>
          <BreadcrumbItem>Machines</BreadcrumbItem>
          <BreadcrumbItem>Centri Paris</BreadcrumbItem>
        </BreadcrumbList>
      </Breadcrumb>,
    );

    const list = page.slot("breadcrumb-list");
    expect(list.localName).toBe("ol");
    expect(list.parent).toBe(page.slot("breadcrumb"));
    expect(list.children.map((item) => item.localName)).toEqual(["li", "li"]);
    expect(page.slots("breadcrumb-item").map((item) => item.text)).toEqual([
      "Machines",
      "Centri Paris",
    ]);
  });

  it("lets the caller's class replace the one of the list", () => {
    const page = draw(<BreadcrumbList className="text-base" id="trail" />);

    expect(page.slot("breadcrumb-list").classes).toContain("text-base");
    expect(page.slot("breadcrumb-list").classes).not.toContain("text-sm");
    expect(page.slot("breadcrumb-list").getAttribute("id")).toBe("trail");
  });

  it("lets the caller's class replace the one of an item", () => {
    const page = draw(<BreadcrumbItem className="gap-4" id="first" />);

    expect(page.slot("breadcrumb-item").classes).toContain("gap-4");
    expect(page.slot("breadcrumb-item").classes).not.toContain("gap-1.5");
    expect(page.slot("breadcrumb-item").getAttribute("id")).toBe("first");
  });
});

describe("ANH-203 breadcrumb: a link and the current page", () => {
  it("draws a link to the address given", () => {
    const page = draw(
      <BreadcrumbLink href="/machines">Machines</BreadcrumbLink>,
    );

    const link = page.slot("breadcrumb-link");
    expect(link.localName).toBe("a");
    expect(link.getAttribute("href")).toBe("/machines");
    expect(link.text).toBe("Machines");
  });

  it("lets the caller's class replace the one of the link", () => {
    const page = draw(<BreadcrumbLink className="transition-none" />);

    expect(page.slot("breadcrumb-link").classes).toContain("transition-none");
    expect(page.slot("breadcrumb-link").classes).not.toContain(
      "transition-colors",
    );
  });

  it("draws the caller's own link in place of the anchor when asked", () => {
    const page = draw(
      <BreadcrumbLink asChild className="mine">
        <button type="button">Machines</button>
      </BreadcrumbLink>,
    );

    const link = page.slot("breadcrumb-link");
    expect(link.localName).toBe("button");
    expect(link.classes).toContain("hover:text-foreground");
    expect(link.classes).toContain("mine");
    expect(page.tag("a")).toEqual([]);
  });

  it("says the current page is the current one, and a link that cannot be followed", () => {
    const page = draw(<BreadcrumbPage>Centri Paris</BreadcrumbPage>);

    const current = page.slot("breadcrumb-page");
    expect(current.localName).toBe("span");
    expect(current.getAttribute("aria-current")).toBe("page");
    expect(current.getAttribute("role")).toBe("link");
    expect(current.getAttribute("aria-disabled")).toBe("true");
    expect(current.hasAttribute("href")).toBe(false);
    expect(current.text).toBe("Centri Paris");
  });

  it("lets the caller's class replace the one of the current page", () => {
    const page = draw(<BreadcrumbPage className="font-bold" />);

    expect(page.slot("breadcrumb-page").classes).toContain("font-bold");
    expect(page.slot("breadcrumb-page").classes).not.toContain("font-normal");
  });
});

describe("ANH-203 breadcrumb: what stands between the links", () => {
  it("separates two items with a chevron a screen reader skips", () => {
    const page = draw(<BreadcrumbSeparator />);

    const separator = page.slot("breadcrumb-separator");
    expect(separator.localName).toBe("li");
    expect(separator.getAttribute("role")).toBe("presentation");
    expect(separator.getAttribute("aria-hidden")).toBe("true");
    expect(separator.children.map((child) => child.localName)).toEqual(["svg"]);
    expect(separator.children[0].classes).toContain("lucide-chevron-right");
  });

  it("draws the caller's separator in place of the chevron", () => {
    const page = draw(
      <BreadcrumbSeparator className="mine">/</BreadcrumbSeparator>,
    );

    const separator = page.slot("breadcrumb-separator");
    expect(separator.text).toBe("/");
    expect(page.tag("svg")).toEqual([]);
    expect(separator.classes).toContain("mine");
    expect(separator.classes).toContain("[&>svg]:size-3.5");
  });

  it("stands for the links left out with three dots a screen reader skips", () => {
    const page = draw(<BreadcrumbEllipsis />);

    const ellipsis = page.slot("breadcrumb-ellipsis");
    expect(ellipsis.localName).toBe("span");
    expect(ellipsis.getAttribute("role")).toBe("presentation");
    expect(ellipsis.getAttribute("aria-hidden")).toBe("true");
    expect(ellipsis.children[0].classes).toContain("lucide-ellipsis");
  });

  it("names the three dots in a text kept off the screen", () => {
    const page = draw(<BreadcrumbEllipsis />);

    const [label] = page.slot("breadcrumb-ellipsis").withClass("sr-only");
    expect(label.text).toBe("More");
  });

  it("lets the caller's class replace the one of the ellipsis", () => {
    const page = draw(<BreadcrumbEllipsis className="size-6" id="more" />);

    expect(page.slot("breadcrumb-ellipsis").classes).toContain("size-6");
    expect(page.slot("breadcrumb-ellipsis").classes).not.toContain("size-9");
    expect(page.slot("breadcrumb-ellipsis").getAttribute("id")).toBe("more");
  });
});

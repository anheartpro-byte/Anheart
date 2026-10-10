import { render, type TestElement } from "@/test-support/render";
import { describe, expect, it } from "vitest";
import { Marquee } from "./marquee";

/**
 * The band of logos that scrolls on the landing page: how many copies of its
 * content it draws so that the scroll never shows a gap, and what each of its
 * switches (vertical, reverse, pause on hover) changes in what is drawn.
 */

const PAUSED = "group-hover:[animation-play-state:paused]";
const REVERSED = "[animation-direction:reverse]";

function drawn(ui: React.ReactElement) {
  const band = render(ui).container.children[0];
  const copies = band.children;
  return { band, copies };
}

function classes(element: TestElement): string[] {
  return element.className.split(" ");
}

describe("ANH-203 marquee", () => {
  it("draws its content four times unless told how many", () => {
    const { copies } = drawn(
      <Marquee>
        <span>Anheart</span>
      </Marquee>,
    );

    expect(copies.length).toBe(4);
    expect(copies.map((copy) => copy.textContent)).toEqual([
      "Anheart",
      "Anheart",
      "Anheart",
      "Anheart",
    ]);
  });

  it.each([1, 2, 7])("draws its content %i times when asked", (repeat) => {
    const { copies } = drawn(
      <Marquee repeat={repeat}>
        <span>Anheart</span>
        <span>Centri</span>
      </Marquee>,
    );

    expect(copies.length).toBe(repeat);
    for (const copy of copies) {
      expect(copy.children.map((child) => child.textContent)).toEqual([
        "Anheart",
        "Centri",
      ]);
    }
  });

  it("scrolls in a row, forwards and without pause, unless told otherwise", () => {
    const { band, copies } = drawn(<Marquee>Anheart</Marquee>);

    expect(classes(band)).toContain("flex-row");
    expect(classes(band)).not.toContain("flex-col");
    for (const copy of copies) {
      expect(classes(copy)).toContain("animate-marquee");
      expect(classes(copy)).not.toContain("animate-marquee-vertical");
      expect(classes(copy)).not.toContain(REVERSED);
      expect(classes(copy)).not.toContain(PAUSED);
    }
  });

  it("scrolls in a column when vertical", () => {
    const { band, copies } = drawn(<Marquee vertical>Anheart</Marquee>);

    expect(classes(band)).toContain("flex-col");
    expect(classes(band)).not.toContain("flex-row");
    for (const copy of copies) {
      expect(classes(copy)).toContain("animate-marquee-vertical");
      expect(classes(copy)).toContain("flex-col");
      expect(classes(copy)).not.toContain("animate-marquee");
    }
  });

  it("scrolls backwards when reversed, and stops under the mouse when asked", () => {
    const reversed = drawn(<Marquee reverse>Anheart</Marquee>);
    for (const copy of reversed.copies) {
      expect(classes(copy)).toContain(REVERSED);
      expect(classes(copy)).not.toContain(PAUSED);
    }

    const pausing = drawn(<Marquee pauseOnHover>Anheart</Marquee>);
    for (const copy of pausing.copies) {
      expect(classes(copy)).toContain(PAUSED);
      expect(classes(copy)).not.toContain(REVERSED);
    }
    // The pause is a hover on the band as a whole: it is the group.
    expect(classes(pausing.band)).toContain("group");
  });

  it("keeps the classes and the attributes it is given, on the band itself", () => {
    const { band } = drawn(
      <Marquee className="[--duration:20s]" id="partners" aria-label="Partners">
        Anheart
      </Marquee>,
    );

    expect(band.id).toBe("partners");
    expect(band.getAttribute("aria-label")).toBe("Partners");
    // The duration given replaces the 40 s of the band.
    expect(classes(band)).toContain("[--duration:20s]");
    expect(classes(band)).not.toContain("[--duration:40s]");
  });
});

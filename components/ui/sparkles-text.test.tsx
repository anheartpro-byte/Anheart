import { render, settle, type TestElement } from "@/test-support/render";
import { frames } from "@/test-support/browser";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SparklesText } from "./sparkles-text";

/**
 * The title of the landing page with stars that twinkle over it: how many
 * stars, where and in which of its two colors chance puts each one, how a
 * star twinkles, and how each is replaced by a new one when its life ends.
 *
 * Motion runs for real, on the frame clock of the tests and fake timers.
 * `Math.random` answers what the test says for each star, in the order the
 * component asks.
 */

/** What `Math.random` answers for one star. */
type Dice = {
  /** Where it stands, from 0 (left, top) to 1 (right, bottom). */
  x: number;
  y: number;
  /** Above 0.5 the first color, else the second. */
  color: number;
  /** Twice this many seconds before it first twinkles. */
  delay: number;
  /** Its largest size: 0.3 plus this. */
  scale: number;
  /** It lives 5 s plus ten times this. */
  life: number;
};

const ORDER = ["x", "y", "color", "delay", "scale", "life"] as const;
const FIRST = "#9E7AFF";
const SECOND = "#FE8BBB";

/** The nth star made since the test started: each test says what it is made of. */
let star: (n: number) => Partial<Dice>;

/** A star that twinkles at once, up to a size of 0.8, and lives 5 s. */
const usual = (n: number): Dice => ({
  // Never twice at the same place: a star is told from the others by its place.
  x: (n % 50) / 50,
  y: 0.5,
  color: 0.9,
  delay: 0,
  scale: 0.5,
  life: 0,
});

function stars(screen: ReturnType<typeof render>): TestElement[] {
  return screen.tag("svg");
}

function colors(screen: ReturnType<typeof render>): (string | null)[] {
  return stars(screen).map((svg) => svg.children[0].getAttribute("fill"));
}

function seconds(time: number) {
  return settle(() => frames.advance(time * 1000));
}

beforeEach(() => {
  vi.useFakeTimers();
  star = () => ({});
  let asked = 0;
  vi.spyOn(Math, "random").mockImplementation(() => {
    const n = Math.floor(asked / ORDER.length);
    const field = ORDER[asked % ORDER.length];
    asked += 1;
    return { ...usual(n), ...star(n) }[field];
  });
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("ANH-203 sparkles text: the title", () => {
  it("draws its text in bold under the stars, with the classes it is given", () => {
    const screen = render(
      <SparklesText className="text-4xl">Anheart</SparklesText>,
    );

    const title = screen.container.children[0];
    expect(screen.tag("strong").map((strong) => strong.textContent)).toEqual([
      "Anheart",
    ]);
    expect(screen.text()).toBe("Anheart");
    expect(title.className.split(" ")).toContain("text-4xl");
    // The size given replaces the one of the title.
    expect(title.className.split(" ")).not.toContain("text-6xl");
  });

  it("gives its two colors to the styles, violet and pink unless told otherwise", () => {
    const usualColors = render(<SparklesText>Anheart</SparklesText>).container
      .children[0];
    expect(usualColors.style["--sparkles-first-color"]).toBe(FIRST);
    expect(usualColors.style["--sparkles-second-color"]).toBe(SECOND);

    const given = render(
      <SparklesText colors={{ first: "#0ea5e9", second: "#22c55e" }}>
        Anheart
      </SparklesText>,
    ).container.children[0];
    expect(given.style["--sparkles-first-color"]).toBe("#0ea5e9");
    expect(given.style["--sparkles-second-color"]).toBe("#22c55e");
  });
});

describe("ANH-203 sparkles text: the stars", () => {
  it("draws ten stars unless told how many", () => {
    expect(stars(render(<SparklesText>Anheart</SparklesText>)).length).toBe(10);
  });

  it.each([0, 1, 4])("draws %i stars when asked", (count) => {
    const screen = render(
      <SparklesText sparklesCount={count}>Anheart</SparklesText>,
    );

    expect(stars(screen).length).toBe(count);
  });

  it("puts each star where chance says, over the text", () => {
    star = (n) =>
      [
        { x: 0.1, y: 0.25 },
        { x: 0.8, y: 1 },
      ][n];

    const screen = render(
      <SparklesText sparklesCount={2}>Anheart</SparklesText>,
    );

    expect(stars(screen).map((svg) => [svg.style.left, svg.style.top])).toEqual(
      [
        ["10%", "25%"],
        ["80%", "100%"],
      ],
    );
  });

  it("colors each star with one of its two colors, as chance says", () => {
    star = (n) => ({ color: [0.9, 0.2, 0.51, 0.5][n] });

    const screen = render(
      <SparklesText sparklesCount={4}>Anheart</SparklesText>,
    );

    // Above a half the first color; a half itself is the second.
    expect(colors(screen)).toEqual([FIRST, SECOND, FIRST, SECOND]);
  });

  it("colors the stars with the colors it is given", () => {
    star = (n) => ({ color: [0.9, 0.2][n] });

    const screen = render(
      <SparklesText
        sparklesCount={2}
        colors={{ first: "#0ea5e9", second: "#22c55e" }}
      >
        Anheart
      </SparklesText>,
    );

    expect(colors(screen)).toEqual(["#0ea5e9", "#22c55e"]);
  });

  it("draws new stars when the count or the colors change", () => {
    const screen = render(
      <SparklesText sparklesCount={2}>Anheart</SparklesText>,
    );

    screen.rerender(<SparklesText sparklesCount={5}>Anheart</SparklesText>);
    expect(stars(screen).length).toBe(5);

    screen.rerender(
      <SparklesText
        sparklesCount={5}
        colors={{ first: "#0ea5e9", second: "#22c55e" }}
      >
        Anheart
      </SparklesText>,
    );
    expect(new Set(colors(screen))).toEqual(new Set(["#0ea5e9"]));
  });
});

describe("ANH-203 sparkles text: a star twinkles", () => {
  it("is invisible at first, at its brightest and largest 0.4 s later, and gone again at 0.8 s", async () => {
    const screen = render(
      <SparklesText sparklesCount={1}>Anheart</SparklesText>,
    );
    const [svg] = stars(screen);
    expect(Number(svg.style.opacity)).toBe(0);

    await seconds(0.4);
    expect(Number(svg.style.opacity)).toBeGreaterThan(0.9);
    // A size of 0.3 + 0.5, turned by 120 degrees.
    const [, scale, turn] = /^scale\(([\d.]+)\) rotate\(([\d.]+)deg\)$/.exec(
      String(svg.style.transform),
    ) as RegExpExecArray;
    expect(Number(scale)).toBeGreaterThan(0.75);
    expect(Number(scale)).toBeLessThanOrEqual(0.8);
    expect(Math.abs(Number(turn) - 120)).toBeLessThan(5);

    await seconds(0.38);
    expect(Number(svg.style.opacity)).toBeLessThan(0.1);
  });

  it("waits for the delay chance gives it before it first twinkles", async () => {
    // A delay of 2 x 0.5 = 1 s.
    star = () => ({ delay: 0.5 });
    const screen = render(
      <SparklesText sparklesCount={1}>Anheart</SparklesText>,
    );
    const [svg] = stars(screen);

    await seconds(0.9);
    expect(Number(svg.style.opacity)).toBe(0);

    await seconds(0.5);
    expect(Number(svg.style.opacity)).toBeGreaterThan(0.9);
  });

  it("twinkles again and again", async () => {
    const screen = render(
      <SparklesText sparklesCount={1}>Anheart</SparklesText>,
    );
    const [svg] = stars(screen);

    // The middle of the third twinkle.
    await seconds(0.8 + 0.8 + 0.4);

    expect(Number(svg.style.opacity)).toBeGreaterThan(0.9);
  });
});

describe("ANH-203 sparkles text: the life of a star", () => {
  it("replaces a star by a new one, elsewhere, once its life is over", async () => {
    // Given one star at 10 % that lives 5 s, and a next one at 80 %.
    star = (n) => ({ x: n === 0 ? 0.1 : 0.8 });
    const screen = render(
      <SparklesText sparklesCount={1}>Anheart</SparklesText>,
    );
    const first = stars(screen)[0];

    await seconds(4.8);
    expect(stars(screen).length).toBe(1);
    expect(stars(screen)[0] === first).toBe(true);
    expect(first.style.left).toBe("10%");

    await seconds(0.5);
    expect(stars(screen).length).toBe(1);
    expect(stars(screen)[0] === first).toBe(false);
    expect(stars(screen)[0].style.left).toBe("80%");
  });

  it("lets a star live as long as chance says: up to 15 s", async () => {
    // A life of 5 + 10 x 0.5 = 10 s.
    star = (n) => ({ x: n === 0 ? 0.1 : 0.8, life: n === 0 ? 0.5 : 0 });
    const screen = render(
      <SparklesText sparklesCount={1}>Anheart</SparklesText>,
    );

    await seconds(9.8);
    expect(stars(screen)[0].style.left).toBe("10%");

    await seconds(0.5);
    expect(stars(screen)[0].style.left).toBe("80%");
  });

  it("stops counting the lives once the title leaves the page", async () => {
    const before = vi.getTimerCount();
    const screen = render(
      <SparklesText sparklesCount={2}>Anheart</SparklesText>,
    );
    expect(vi.getTimerCount()).toBe(before + 1);

    screen.unmount();

    expect(vi.getTimerCount()).toBe(before);
  });
});

import { click, render, settle, testDocument } from "@/test-support/render";
import {
  installClassList,
  installLayout,
  installMutationObserver,
  installViewport,
} from "@/test-support/browser";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type Mock,
} from "vitest";
import { AnimatedThemeToggler } from "./animated-theme-toggler";

/**
 * The button that switches the site between light and dark: the icon it
 * shows, the `dark` class it toggles on `<html>`, the choice it writes for
 * the next visit, and the circle it opens from itself over the page.
 *
 * What it asks of a browser is given by named stand-ins: the classes of
 * `<html>` and the observer of their changes (`test-support/browser`), a
 * `localStorage` that records what is written, and below a view transition
 * and an `animate` that record what they are asked. The view transition runs
 * the update at once and is ready when the test says so.
 */

const html = testDocument.documentElement;

type Keyframes = { clipPath: string[] };
type Timing = { duration: number; easing: string; pseudoElement: string };

let stored: Mock<(key: string, value: string) => void>;
let animate: Mock<(keyframes: Keyframes, timing: Timing) => void>;
let startViewTransition: Mock<(update: () => void) => { ready: Promise<void> }>;
/** Tells the page the new view is drawn and the transition can be animated. */
let transitionReady: () => void;
let theme: ReturnType<typeof installMutationObserver>;

/** Which icon the button shows: the name of the Lucide icon drawn in it. */
function icon(screen: ReturnType<typeof render>): string | undefined {
  return screen
    .tag("svg")[0]
    .className.split(" ")
    .find((name) => name.startsWith("lucide-"));
}

const toggle = (screen: ReturnType<typeof render>) =>
  screen.button("Toggle theme");

beforeEach(() => {
  installClassList();
  theme = installMutationObserver();
  // The button is 40 px wide and high, at (100, 20), in a window of 1000 by 800.
  installLayout({ width: 40, height: 40, left: 100, top: 20 });
  installViewport(1000, 800);
  html.removeAttribute("class");

  stored = vi.fn();
  vi.stubGlobal("localStorage", { setItem: stored });

  animate = vi.fn();
  startViewTransition = vi.fn((update) => {
    update();
    return {
      ready: new Promise<void>((resolve) => {
        transitionReady = resolve;
      }),
    };
  });
  Object.assign(html, { animate });
  Object.assign(testDocument, { startViewTransition });
});
afterEach(() => {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(html, "animate");
  Reflect.deleteProperty(testDocument, "startViewTransition");
  html.removeAttribute("class");
});

describe("ANH-203 theme toggler: the icon", () => {
  it("offers the moon while the page is light, the sun while it is dark", () => {
    const light = render(<AnimatedThemeToggler />);
    expect(icon(light)).toBe("lucide-moon");
    light.unmount();

    html.setAttribute("class", "dark");
    const dark = render(<AnimatedThemeToggler />);
    expect(icon(dark)).toBe("lucide-sun");
  });

  it("follows the theme when something else changes it", async () => {
    const screen = render(<AnimatedThemeToggler />);
    // It watches the classes of <html>, and nothing else of it.
    expect(theme.created.length).toBe(1);
    expect(theme.created[0].targets.has(html)).toBe(true);
    expect(theme.created[0].options).toEqual({
      attributes: true,
      attributeFilter: ["class"],
    });

    html.setAttribute("class", "dark");
    await settle(() => theme.change());
    expect(icon(screen)).toBe("lucide-sun");

    html.setAttribute("class", "");
    await settle(() => theme.change());
    expect(icon(screen)).toBe("lucide-moon");
  });

  it("is a button a screen reader can name, with the classes and attributes it is given", () => {
    const screen = render(
      <AnimatedThemeToggler className="rounded-full" aria-pressed="false" />,
    );

    const button = toggle(screen);
    expect(button.className.split(" ")).toContain("rounded-full");
    expect(button.getAttribute("aria-pressed")).toBe("false");
  });
});

describe("ANH-203 theme toggler: a click", () => {
  it("turns the page dark, shows the sun and writes the choice for the next visit", async () => {
    const screen = render(<AnimatedThemeToggler />);

    await click(toggle(screen));

    expect(html.className).toBe("dark");
    expect(icon(screen)).toBe("lucide-sun");
    expect(stored.mock.calls).toEqual([["theme", "dark"]]);
  });

  it("turns the page light again on the next click", async () => {
    const screen = render(<AnimatedThemeToggler />);
    await click(toggle(screen));

    await click(toggle(screen));

    expect(html.className).toBe("");
    expect(icon(screen)).toBe("lucide-moon");
    expect(stored.mock.calls).toEqual([
      ["theme", "dark"],
      ["theme", "light"],
    ]);
  });

  it("leaves the other classes of the page alone", async () => {
    html.setAttribute("class", "antialiased dark");
    const screen = render(<AnimatedThemeToggler />);

    await click(toggle(screen));

    expect(html.className).toBe("antialiased");
    expect(stored).toHaveBeenLastCalledWith("theme", "light");
  });

  it("changes the theme inside a view transition, so that the browser can animate from the old page", async () => {
    const screen = render(<AnimatedThemeToggler />);
    startViewTransition.mockImplementationOnce(() => {
      // The update was not run: the page must not have changed by itself.
      return { ready: new Promise<void>(() => {}) };
    });

    await click(toggle(screen));

    expect(startViewTransition).toHaveBeenCalledTimes(1);
    expect(html.className).toBe("");
    expect(stored).not.toHaveBeenCalled();
    expect(icon(screen)).toBe("lucide-moon");
  });
});

describe("ANH-203 theme toggler: the circle that opens over the page", () => {
  it("waits for the new page to be drawn before it animates", async () => {
    const screen = render(<AnimatedThemeToggler />);

    await click(toggle(screen));
    expect(animate).not.toHaveBeenCalled();

    await settle(() => transitionReady());
    expect(animate).toHaveBeenCalledTimes(1);
  });

  it("opens from the centre of the button to the farthest corner of the window", async () => {
    const screen = render(<AnimatedThemeToggler />);

    await click(toggle(screen));
    await settle(() => transitionReady());

    // The centre of the button: (100 + 40 / 2, 20 + 40 / 2). The farthest
    // corner is 900 px to the right of the button and 780 px below it.
    const radius = Math.hypot(900, 780);
    const [keyframes, timing] = animate.mock.calls[0];
    expect(keyframes).toEqual({
      clipPath: [
        "circle(0px at 120px 40px)",
        `circle(${radius}px at 120px 40px)`,
      ],
    });
    expect(timing).toEqual({
      duration: 400,
      easing: "ease-in-out",
      pseudoElement: "::view-transition-new(root)",
    });
  });

  it("reaches the left and the top of the window from a button near the right and the bottom", async () => {
    installLayout({ width: 40, height: 40, left: 900, top: 700 });
    const screen = render(<AnimatedThemeToggler />);

    await click(toggle(screen));
    await settle(() => transitionReady());

    const [keyframes] = animate.mock.calls[0];
    expect(keyframes.clipPath[1]).toBe(
      `circle(${Math.hypot(900, 700)}px at 920px 720px)`,
    );
  });

  it("takes the time it is given", async () => {
    const screen = render(<AnimatedThemeToggler duration={900} />);

    await click(toggle(screen));
    await settle(() => transitionReady());

    expect(animate.mock.calls[0][1].duration).toBe(900);
  });
});

describe("ANH-203 theme toggler: leaving the page", () => {
  it("stops watching the classes of the page", () => {
    const screen = render(<AnimatedThemeToggler />);
    expect(theme.created[0].disconnected).toBe(false);

    screen.unmount();

    expect(theme.created[0].disconnected).toBe(true);
    expect(theme.watching().length).toBe(0);
  });
});

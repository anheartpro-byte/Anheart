import { render, settle } from "@/test-support/render";
import { installViewport } from "@/test-support/browser";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { useIsMobile } from "./use-mobile";

/**
 * Whether the page is shown on a narrow screen: under 768 pixels the menu of
 * the dashboard becomes a drawer. The hook follows the width as it changes
 * and stops listening when its page is gone.
 */

function Probe() {
  return <p>{useIsMobile() ? "narrow" : "wide"}</p>;
}

describe("ANH-203 useIsMobile", () => {
  it.each([
    [320, "narrow"],
    [767, "narrow"],
    [768, "wide"],
    [1280, "wide"],
  ])("a window of %i pixels is %s", (width, expected) => {
    installViewport(width);

    expect(render(<Probe />).text()).toBe(expected);
  });

  it("asks the browser for the breakpoint just under 768 pixels", () => {
    const viewport = installViewport(1024);

    render(<Probe />);

    expect(viewport.asked()).toEqual(["(max-width: 767px)"]);
  });

  it("follows the window when it is narrowed, then widened again", async () => {
    const viewport = installViewport(1024);
    const screen = render(<Probe />);
    expect(screen.text()).toBe("wide");

    await settle(() => viewport.resizeTo(600));
    expect(screen.text()).toBe("narrow");

    await settle(() => viewport.resizeTo(900));
    expect(screen.text()).toBe("wide");

    // The same breakpoint as on opening: 767 is narrow, 768 is not.
    await settle(() => viewport.resizeTo(767));
    expect(screen.text()).toBe("narrow");
    await settle(() => viewport.resizeTo(768));
    expect(screen.text()).toBe("wide");
  });

  it("stops listening to the window once its page is gone", () => {
    const viewport = installViewport(1024);
    const screen = render(<Probe />);
    expect(viewport.listeners()).toBe(1);

    screen.unmount();

    expect(viewport.listeners()).toBe(0);
  });
});

describe("ANH-249 useIsMobile, read from the window as an external store", () => {
  function Recorder({ seen }: { seen: boolean[] }) {
    seen.push(useIsMobile());
    return null;
  }

  it("is right from its first render, follows the breakpoint, then lets go of the window", async () => {
    const viewport = installViewport(767);
    const seen: boolean[] = [];
    const screen = render(<Recorder seen={seen} />);

    // Before any resize, not even one render says wide on a narrow window.
    expect(seen.length).toBeGreaterThan(0);
    expect(seen).not.toContain(false);

    await settle(() => viewport.resizeTo(768));
    expect(seen.at(-1)).toBe(false);
    await settle(() => viewport.resizeTo(767));
    expect(seen.at(-1)).toBe(true);

    screen.unmount();
    expect(viewport.listeners()).toBe(0);
    const renders = seen.length;
    await settle(() => viewport.resizeTo(1280));
    expect(seen).toHaveLength(renders);
  });

  it("says wide on the server, where there is no window to measure", () => {
    const viewport = installViewport(320);

    expect(renderToStaticMarkup(<Probe />)).toBe("<p>wide</p>");
    expect(viewport.asked()).toEqual([]);
  });
});

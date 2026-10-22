// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { dismissFeedback, getFeedbackToasts, pushFeedback } from "@/lib/feedback";
import fr from "@/messages/fr.json";
import { FeedbackToaster } from "./FeedbackToaster";

const property = "--feedback-toaster-space";

afterEach(() => {
  cleanup();
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function mount() {
  const view = render(
    <NextIntlClientProvider locale="fr" messages={fr} timeZone="Europe/Paris">
      <FeedbackToaster />
    </NextIntlClientProvider>,
  );
  const stack = view.container.querySelector<HTMLElement>(
    "[data-feedback-toaster]",
  )!;
  stack.style.bottom = "16px";
  return { ...view, stack };
}

function observer() {
  let resize: () => void = () => {};
  const disconnect = vi.fn();
  const observe = vi.fn();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: () => void) {
        resize = callback;
      }
      observe = observe;
      disconnect = disconnect;
    },
  );
  return { resize: () => act(resize), observe, disconnect };
}

const reserved = () => document.documentElement.style.getPropertyValue(property);

function dimensions(stack: HTMLElement, content = 500, visible = 200) {
  Object.defineProperties(stack, {
    scrollHeight: { configurable: true, value: content },
    clientHeight: { configurable: true, value: visible },
  });
}

function wheel(stack: HTMLElement, deltaY: number, deltaMode = 0) {
  const event = new WheelEvent("wheel", { deltaY, deltaMode, bubbles: true, cancelable: true });
  stack.dispatchEvent(event);
  return event;
}

function touch(stack: HTMLElement, type: "touchstart" | "touchmove", clientY: number) {
  const event = new TouchEvent(type, {
    touches: [point(stack, clientY)], bubbles: true, cancelable: true,
  });
  stack.dispatchEvent(event);
  return event;
}

function point(target: HTMLElement, clientY: number): Touch {
  return {
    identifier: 0, target, clientX: 0, clientY,
    pageX: 0, pageY: clientY, screenX: 0, screenY: clientY,
    radiusX: 1, radiusY: 1, rotationAngle: 0, force: 1,
  };
}

describe("ANH-232 gestures on the feedback stack", () => {
  it("scrolls by pixels, lines and pages without a second native scroll", () => {
    const { stack } = mount();
    dimensions(stack);
    stack.style.lineHeight = "20px";
    expect(wheel(stack, 30).defaultPrevented).toBe(true);
    expect(stack.scrollTop).toBe(30);
    expect(wheel(stack, 2, 1).defaultPrevented).toBe(true);
    expect(stack.scrollTop).toBe(70);
    expect(wheel(stack, 1, 2).defaultPrevented).toBe(true);
    expect(stack.scrollTop).toBe(270);
    expect(wheel(stack, -20).defaultPrevented).toBe(true);
    expect(stack.scrollTop).toBe(250);
  });

  it("scrolls a touch drag from its last point in either direction", () => {
    const { stack } = mount();
    dimensions(stack);
    expect(touch(stack, "touchstart", 200).defaultPrevented).toBe(false);
    expect(touch(stack, "touchmove", 150).defaultPrevented).toBe(true);
    expect(stack.scrollTop).toBe(50);
    expect(touch(stack, "touchmove", 120).defaultPrevented).toBe(true);
    expect(stack.scrollTop).toBe(80);
    expect(touch(stack, "touchmove", 160).defaultPrevented).toBe(true);
    expect(stack.scrollTop).toBe(40);
  });

  it("leaves gestures alone at either boundary or when the stack does not overflow", () => {
    const { stack } = mount();
    dimensions(stack);
    expect(wheel(stack, -30).defaultPrevented).toBe(false);
    touch(stack, "touchstart", 100);
    expect(touch(stack, "touchmove", 150).defaultPrevented).toBe(false);
    expect(stack.scrollTop).toBe(0);
    expect(wheel(stack, 1000).defaultPrevented).toBe(true);
    expect(stack.scrollTop).toBe(300);
    expect(wheel(stack, 30).defaultPrevented).toBe(false);
    touch(stack, "touchstart", 100);
    expect(touch(stack, "touchmove", 50).defaultPrevented).toBe(false);
    expect(stack.scrollTop).toBe(300);
    stack.scrollTop = 0;
    dimensions(stack, 100, 200);
    expect(wheel(stack, 30).defaultPrevented).toBe(false);
    touch(stack, "touchstart", 100);
    expect(touch(stack, "touchmove", 50).defaultPrevented).toBe(false);
    expect(stack.scrollTop).toBe(0);
  });

  it("ignores a touch move without a start and preserves gestures with several fingers", () => {
    const { stack } = mount();
    dimensions(stack);
    expect(touch(stack, "touchmove", 100).defaultPrevented).toBe(false);
    const touches = [point(stack, 100), point(stack, 120)];
    stack.dispatchEvent(new TouchEvent("touchstart", { touches }));
    expect(touch(stack, "touchmove", 80).defaultPrevented).toBe(false);
    const event = new TouchEvent("touchmove", { touches, cancelable: true });
    stack.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(stack.scrollTop).toBe(0);
  });

  it("removes every gesture listener when unmounted", () => {
    const { stack, unmount } = mount();
    dimensions(stack);
    const remove = vi.spyOn(stack, "removeEventListener");
    unmount();
    for (const type of ["wheel", "touchstart", "touchmove"]) {
      expect(remove).toHaveBeenCalledWith(type, expect.any(Function));
    }
    expect(wheel(stack, 30).defaultPrevented).toBe(false);
    touch(stack, "touchstart", 100);
    expect(touch(stack, "touchmove", 50).defaultPrevented).toBe(false);
    expect(stack.scrollTop).toBe(0);
  });
});

describe("ANH-232 space above the feedback stack", () => {
  it("keeps the empty live regions mounted without reserving any space", () => {
    const resize = observer();
    const { stack } = mount();
    expect(reserved()).toBe("");
    expect(resize.observe).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").getAttribute("aria-live")).toBe("assertive");
    expect(screen.getByRole("status").getAttribute("aria-live")).toBe("polite");
    expect(stack.style.pointerEvents).toBe("none");
  });

  it("reserves the measured height, bottom offset and gap, then follows resizes", () => {
    const resize = observer();
    const { stack } = mount();
    const alert = screen.getByRole("alert");
    const bounds = vi.spyOn(stack, "getBoundingClientRect");
    bounds.mockReturnValue(new DOMRect(0, 0, 300, 120));
    act(() => pushFeedback("error", "Refus persistant"));
    expect(resize.observe).toHaveBeenCalledWith(stack);
    expect(reserved()).toBe("144px");
    expect(screen.getByRole("alert")).toBe(alert);
    expect(stack.style.pointerEvents).toBe("none");
    expect(stack.className).toContain("max-h-[calc(50dvh-1.5rem)]");
    expect(stack.className).toContain("overflow-y-auto");
    bounds.mockReturnValue(new DOMRect(0, 0, 300, 200));
    resize.resize();
    expect(reserved()).toBe("224px");
  });

  it("releases the reservation only after the last error is closed", () => {
    const resize = observer();
    const { stack } = mount();
    vi.spyOn(stack, "getBoundingClientRect").mockReturnValue(
      new DOMRect(0, 0, 300, 120),
    );
    act(() => {
      pushFeedback("error", "Premier refus");
      pushFeedback("error", "Second refus");
    });
    act(() => dismissFeedback(getFeedbackToasts()[0].id));
    expect(reserved()).toBe("144px");
    act(() => dismissFeedback(getFeedbackToasts()[0].id));
    expect(reserved()).toBe("");
    expect(resize.disconnect).toHaveBeenCalledOnce();
    expect(screen.getByRole("alert").textContent).toBe("");
  });

  it("releases the space when a success expires after five seconds", () => {
    vi.useFakeTimers();
    observer();
    const { stack } = mount();
    vi.spyOn(stack, "getBoundingClientRect").mockReturnValue(
      new DOMRect(0, 0, 300, 80),
    );
    act(() => pushFeedback("success", "Enregistré"));
    expect(reserved()).toBe("104px");
    act(() => vi.advanceTimersByTime(4999));
    expect(reserved()).toBe("104px");
    act(() => vi.advanceTimersByTime(1));
    expect(reserved()).toBe("");
  });

  it("disconnects and removes its CSS property on unmount", () => {
    const resize = observer();
    const { stack, unmount } = mount();
    vi.spyOn(stack, "getBoundingClientRect").mockReturnValue(
      new DOMRect(0, 0, 300, 120),
    );
    act(() => pushFeedback("error", "Refus"));
    expect(reserved()).toBe("144px");
    unmount();
    expect(resize.disconnect).toHaveBeenCalledOnce();
    expect(reserved()).toBe("");
  });

  it("shows and closes messages without reserving space when ResizeObserver is unavailable", () => {
    vi.stubGlobal("ResizeObserver", undefined);
    mount();
    act(() => pushFeedback("error", "Refus sans observateur"));
    expect(screen.getByRole("alert").textContent).toBe("Refus sans observateur");
    expect(reserved()).toBe("");
    act(() => dismissFeedback(getFeedbackToasts()[0].id));
    expect(reserved()).toBe("");
  });
});

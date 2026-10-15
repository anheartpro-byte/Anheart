import { messages, render } from "@/test-support/render";
import type { AnchorHTMLAttributes } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { answer, asks, resetConvex } from "@/test-support/convex";
import { NotificationBell } from "./NotificationBell";

/**
 * ANH-90: the bell of the header leads to the alerts page, with the number of
 * unread alerts. The server says who receives alerts at all
 * (`notifications.unreadCount` answers null for anyone else).
 */

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/convex")).convexReact,
);
vi.mock("@/i18n/navigation", () => ({
  Link: ({
    href,
    children,
    ...rest
  }: Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & { href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

const fr = messages.fr.notifications;

beforeEach(resetConvex);

function bell(unread: number | null | undefined, locale: "fr" | "en" = "fr") {
  answer("notifications:unreadCount", unread);
  const screen = render(<NotificationBell />, { locale });
  const [link] = screen.tag("a");
  const badge = screen.all((element) => element.hasAttribute("data-unread"));
  return { screen, link, badge };
}

describe("ANH-90 the bell of the header", () => {
  it.each([
    ["while the number is loading", undefined],
    ["for whoever receives no alerts", null],
  ])("is not drawn %s", (_when, unread) => {
    const { screen, link } = bell(unread);

    expect(link).toBeUndefined();
    expect(screen.text()).toBe("");
    expect(asks("notifications:unreadCount")).toHaveLength(1);
  });

  it("leads to the alerts page, without a number when every alert is read", () => {
    const { link, badge } = bell(0);

    expect(link.getAttribute("href")).toBe("/dashboard/notifications");
    expect(link.getAttribute("aria-label")).toBe(fr.bell);
    expect(badge).toEqual([]);
  });

  it("says how many alerts are unread, in the visitor's language", () => {
    const one = bell(1);
    expect(one.link.getAttribute("aria-label")).toBe("Alertes : 1 non lue");
    one.screen.unmount();

    const three = bell(3);
    expect(three.link.getAttribute("aria-label")).toBe("Alertes : 3 non lues");
    expect(three.screen.textOf(three.badge[0])).toBe("3");
    three.screen.unmount();

    const english = bell(2, "en");
    expect(english.link.getAttribute("aria-label")).toBe("Alerts: 2 unread");
  });

  it("writes 99+ beyond 99 unread alerts", () => {
    const { screen, badge } = bell(100);

    expect(screen.textOf(badge[0])).toBe("99+");
  });
});

// @vitest-environment jsdom
import type { ReactNode } from "react";
import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { propsOf, renderPage, wasMounted } from "@/test-support/pages";
import DashboardLayout from "./layout";

// The frame of the dashboard is made of components that have their own
// queries and their own drawing: the layout only places them around the page
// and remembers whether the sidebar was left open.
vi.mock("@/components/ui/sidebar", async () => {
  const { standIn } = await import("@/test-support/pages");
  const children = ({ children }: { children?: ReactNode }) => children;
  return {
    SidebarProvider: standIn("SidebarProvider", children),
    SidebarInset: standIn("SidebarInset", children),
  };
});
vi.mock("@/components/dashboard/AppSidebar", async () => ({
  AppSidebar: (await import("@/test-support/pages")).standIn("AppSidebar"),
}));
vi.mock("@/components/dashboard/Header", async () => ({
  Header: (await import("@/test-support/pages")).standIn("Header"),
}));
vi.mock("@/components/ui/light-rays", async () => ({
  LightRays: (await import("@/test-support/pages")).standIn("LightRays"),
}));

/** The frame every page of the dashboard is drawn in, in a browser. */

type Provider = { defaultOpen: boolean };

/** Forgets every cookie this page holds. */
function forgetCookies() {
  for (const cookie of document.cookie.split("; ")) {
    const name = cookie.split("=")[0];
    if (name) document.cookie = `${name}=; max-age=0; path=/`;
  }
}

afterEach(forgetCookies);

function open() {
  return renderPage(
    <DashboardLayout>
      <p>La page</p>
    </DashboardLayout>,
  );
}

describe("dashboard frame", () => {
  it("draws the page in the main area, beside the sidebar and under the header", async () => {
    await open();

    expect(within(screen.getByRole("main")).getByText("La page")).toBeTruthy();
    expect(wasMounted("AppSidebar")).toBe(true);
    expect(wasMounted("Header")).toBe(true);
    const frame = screen.getByRole("main").parentElement as HTMLElement;
    expect(
      Array.from(frame.children).map(
        (child) => child.getAttribute("data-stand-in") ?? child.tagName,
      ),
    ).toEqual(["LightRays", "Header", "MAIN"]);
  });

  it("opens the sidebar for a visitor who never closed it", async () => {
    await open();

    expect(propsOf<Provider>("SidebarProvider").defaultOpen).toBe(true);
  });

  it("keeps the sidebar closed for a visitor who closed it", async () => {
    document.cookie = "theme=dark; path=/";
    document.cookie = "sidebar_state=false; path=/";
    await open();

    expect(propsOf<Provider>("SidebarProvider").defaultOpen).toBe(false);
  });

  it("opens the sidebar for a visitor who left it open", async () => {
    document.cookie = "sidebar_state=true; path=/";
    await open();

    expect(propsOf<Provider>("SidebarProvider").defaultOpen).toBe(true);
  });

  it("reads the sidebar's own cookie, not another one that ends like it", async () => {
    document.cookie = "old_sidebar_state=false; path=/";
    await open();

    expect(propsOf<Provider>("SidebarProvider").defaultOpen).toBe(true);
  });
});

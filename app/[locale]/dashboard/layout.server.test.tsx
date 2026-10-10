import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import DashboardLayout from "./layout";

/**
 * The frame of the dashboard as the server draws it, before the browser takes
 * over: there is no document, so no cookie to read. This file runs without
 * jsdom for that reason.
 */

const providers = vi.hoisted(() => [] as { defaultOpen: boolean }[]);

vi.mock("@/components/ui/sidebar", () => ({
  SidebarProvider: (props: { defaultOpen: boolean; children?: ReactNode }) => {
    providers.push({ defaultOpen: props.defaultOpen });
    return props.children;
  },
  SidebarInset: ({ children }: { children?: ReactNode }) => children,
}));
vi.mock("@/components/dashboard/AppSidebar", () => ({
  AppSidebar: () => null,
}));
vi.mock("@/components/dashboard/Header", () => ({ Header: () => null }));
vi.mock("@/components/ui/light-rays", () => ({ LightRays: () => null }));

describe("dashboard frame, drawn by the server", () => {
  it("draws the sidebar open and the page in the main area, with no document to read a cookie from", () => {
    // What makes this the server's rendering: with a document, this test
    // would only repeat the one made in jsdom.
    expect(typeof document).toBe("undefined");
    providers.length = 0;

    const html = renderToStaticMarkup(
      <DashboardLayout>
        <p>La page</p>
      </DashboardLayout>,
    );

    expect(providers).toEqual([{ defaultOpen: true }]);
    expect(html).toMatch(/^<main[^>]*><p>La page<\/p><\/main>$/);
  });
});

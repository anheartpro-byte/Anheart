import { describe, expect, it } from "vitest";
import { draw } from "@/test-support/markup";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./tabs";

/**
 * The tabs, on the real Radix primitives, drawn as the server draws them: a
 * list of tab buttons, one of them active, and the panel of the active tab
 * shown alone.
 */

function tabs(active: string) {
  return draw(
    <Tabs defaultValue={active}>
      <TabsList>
        <TabsTrigger value="live">Direct</TabsTrigger>
        <TabsTrigger value="history">Historique</TabsTrigger>
      </TabsList>
      <TabsContent value="live">Séance en cours</TabsContent>
      <TabsContent value="history">Séances passées</TabsContent>
    </Tabs>,
  );
}

describe("ANH-203 tabs: what is drawn", () => {
  it("holds a list of tab buttons and their panels", () => {
    const page = tabs("live");

    const root = page.slot("tabs");
    const list = page.slot("tabs-list");
    expect(list.parent).toBe(root);
    expect(list.getAttribute("role")).toBe("tablist");
    expect(page.slots("tabs-trigger").map((tab) => tab.parent)).toEqual([
      list,
      list,
    ]);
    expect(
      page.slots("tabs-trigger").map((tab) => tab.getAttribute("role")),
    ).toEqual(["tab", "tab"]);
    expect(page.slots("tabs-content").map((panel) => panel.parent)).toEqual([
      root,
      root,
    ]);
  });

  it("marks the tab the caller names as active, and shows its panel alone", () => {
    const page = tabs("history");

    expect(
      page
        .slots("tabs-trigger")
        .map((tab) => tab.getAttribute("aria-selected")),
    ).toEqual(["false", "true"]);
    const [live, history] = page.slots("tabs-content");
    expect(history.text).toBe("Séances passées");
    expect(live.text).toBe("");
    expect(live.hasAttribute("hidden")).toBe(true);
    // The active tab is lifted off the list: the class reads the state Radix writes.
    const [, active] = page.slots("tabs-trigger");
    expect(active.getAttribute("data-state")).toBe("active");
    expect(active.classes).toContain("data-[state=active]:bg-background");
  });

  it("ties each tab to its panel", () => {
    const page = tabs("live");

    const [tab] = page.slots("tabs-trigger");
    const [panel] = page.slots("tabs-content");
    expect(tab.getAttribute("aria-controls")).toBe(panel.getAttribute("id"));
    expect(panel.getAttribute("aria-labelledby")).toBe(tab.getAttribute("id"));
  });
});

describe("ANH-203 tabs: the caller's classes and props", () => {
  it("lets the caller's class replace the one of each part", () => {
    const page = draw(
      <Tabs defaultValue="live" className="gap-6" id="views">
        <TabsList className="h-12">
          <TabsTrigger value="live" className="px-4" disabled>
            Direct
          </TabsTrigger>
        </TabsList>
        <TabsContent value="live" className="flex-none">
          Séance en cours
        </TabsContent>
      </Tabs>,
    );

    expect(page.slot("tabs").classes).toContain("gap-6");
    expect(page.slot("tabs").classes).not.toContain("gap-2");
    expect(page.slot("tabs").getAttribute("id")).toBe("views");
    expect(page.slot("tabs-list").classes).toContain("h-12");
    expect(page.slot("tabs-list").classes).not.toContain("h-9");
    expect(page.slot("tabs-trigger").classes).toContain("px-4");
    expect(page.slot("tabs-trigger").classes).not.toContain("px-2");
    expect(page.slot("tabs-trigger").hasAttribute("disabled")).toBe(true);
    expect(page.slot("tabs-content").classes).toContain("flex-none");
    expect(page.slot("tabs-content").classes).not.toContain("flex-1");
  });

  it("stacks the list above the panels unless the caller's class says otherwise", () => {
    const page = tabs("live");

    expect(page.slot("tabs").classes).toContain("flex-col");
  });
});

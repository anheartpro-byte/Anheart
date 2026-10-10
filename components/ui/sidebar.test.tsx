import {
  click,
  fireWindow,
  render,
  settle,
  testDocument,
  type TestElement,
} from "@/test-support/render";
import { installViewport } from "@/test-support/browser";
import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupAction,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInput,
  SidebarInset,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSkeleton,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
  SidebarProvider,
  SidebarRail,
  SidebarSeparator,
  SidebarTrigger,
  useSidebar,
} from "./sidebar";

/**
 * The sidebar of the dashboard: what opens and closes it (its button, its
 * rail, Ctrl+B or Cmd+B), what it remembers of it (a cookie the server reads
 * on the next page), what it draws on a desk and on a phone, and what each of
 * its parts adds to what it is given.
 *
 * Two of our wrappers of Radix cannot run here (Presence and Portal ask for a
 * browser): the sheet that carries the sidebar on a phone, and the tooltip of
 * a menu button. Each is replaced by a stand-in that draws its children and
 * shows the props it received as attributes. Everything else is the real code.
 */

type StandInProps = Record<string, unknown> & { children?: ReactNode };

// The sheet draws what it holds only when open, as the real one does, and a
// button that closes it the way Escape or a click outside does.
vi.mock("@/components/ui/sheet", async () => {
  const { createElement } = await import("react");
  const part =
    (name: string) =>
    ({ children, className }: StandInProps) =>
      createElement("div", { "data-sheet-part": name, className }, children);
  return {
    Sheet: ({ open, onOpenChange, children }: StandInProps) =>
      createElement(
        "section",
        { "data-stand-in": "sheet", "data-open": String(open) },
        open ? children : null,
        createElement(
          "button",
          { onClick: () => (onOpenChange as (open: boolean) => void)(false) },
          "close the sheet",
        ),
      ),
    SheetContent: ({ children, side, ...props }: StandInProps) =>
      createElement("div", { ...props, "data-sheet-side": side }, children),
    SheetHeader: part("header"),
    SheetTitle: part("title"),
    SheetDescription: part("description"),
  };
});

// The tooltip draws its trigger as it is, and its content beside it with the
// `hidden` it was given: the real one decides from it whether to show.
vi.mock("@/components/ui/tooltip", async () => {
  const { createElement } = await import("react");
  return {
    TooltipProvider: ({ children, delayDuration }: StandInProps) =>
      createElement(
        "div",
        { "data-stand-in": "tooltip-provider", "data-delay": delayDuration },
        children,
      ),
    Tooltip: ({ children }: StandInProps) => children,
    TooltipTrigger: ({ children }: StandInProps) => children,
    TooltipContent: ({ children, hidden, side, align }: StandInProps) =>
      createElement(
        "div",
        {
          "data-stand-in": "tooltip",
          "data-side": side,
          "data-align": align,
          hidden,
        },
        children,
      ),
  };
});

const DESK = 1280;
const PHONE = 500;
const A_WEEK_S = 60 * 60 * 24 * 7;
const TOGGLE = "Toggle Sidebar";

type Screen = ReturnType<typeof render>;

/** The one element drawn with this `data-slot`. */
function slot(screen: Screen, name: string): TestElement {
  const found = screen.all((el) => el.getAttribute("data-slot") === name);
  if (found.length !== 1) {
    throw new Error(
      `${found.length} elements for the slot "${name}": ${screen.markup()}`,
    );
  }
  return found[0];
}

function hasSlot(screen: Screen, name: string): boolean {
  return screen.all((el) => el.getAttribute("data-slot") === name).length > 0;
}

function classes(element: TestElement): string[] {
  return element.className.split(" ");
}

/** A sidebar with its button outside it, as the layout of the dashboard draws them. */
function Page({
  provider,
  sidebar,
  children = "Machines",
}: {
  provider?: Partial<React.ComponentProps<typeof SidebarProvider>>;
  sidebar?: Partial<React.ComponentProps<typeof Sidebar>>;
  children?: ReactNode;
}) {
  return (
    <SidebarProvider {...provider}>
      <Sidebar {...sidebar}>{children}</Sidebar>
      <SidebarTrigger />
    </SidebarProvider>
  );
}

let viewport: ReturnType<typeof installViewport>;

beforeEach(() => {
  viewport = installViewport(DESK);
  testDocument.cookie = "";
});

describe("ANH-203 sidebar: opening and closing on a desk", () => {
  it("starts expanded, and collapsed when told so", () => {
    const open = render(<Page />);
    expect(slot(open, "sidebar").getAttribute("data-state")).toBe("expanded");
    // Nothing is collapsed, so no way of collapsing is announced to the styles.
    expect(slot(open, "sidebar").getAttribute("data-collapsible")).toBe("");
    open.unmount();

    const closed = render(<Page provider={{ defaultOpen: false }} />);
    expect(slot(closed, "sidebar").getAttribute("data-state")).toBe(
      "collapsed",
    );
    expect(slot(closed, "sidebar").getAttribute("data-collapsible")).toBe(
      "offcanvas",
    );
  });

  it("announces the way it collapses once collapsed: to icons when asked", () => {
    const screen = render(
      <Page
        provider={{ defaultOpen: false }}
        sidebar={{ collapsible: "icon" }}
      />,
    );

    expect(slot(screen, "sidebar").getAttribute("data-collapsible")).toBe(
      "icon",
    );
  });

  it("collapses and expands when its button is clicked", async () => {
    const screen = render(<Page />);

    await click(screen.button(TOGGLE));
    expect(slot(screen, "sidebar").getAttribute("data-state")).toBe(
      "collapsed",
    );

    await click(screen.button(TOGGLE));
    expect(slot(screen, "sidebar").getAttribute("data-state")).toBe("expanded");
  });

  it("writes the state in a cookie that lasts a week, each time it changes", async () => {
    const screen = render(<Page />);
    expect(testDocument.cookie).toBe("");

    await click(screen.button(TOGGLE));
    expect(testDocument.cookie).toBe(
      `sidebar_state=false; path=/; max-age=${A_WEEK_S}`,
    );

    await click(screen.button(TOGGLE));
    expect(testDocument.cookie).toBe(
      `sidebar_state=true; path=/; max-age=${A_WEEK_S}`,
    );
  });

  it("runs the click handler given to its button, then toggles", async () => {
    const seen: string[] = [];
    const screen = render(
      <SidebarProvider>
        <Sidebar />
        <SidebarTrigger
          onClick={() =>
            seen.push(slot(screen, "sidebar").getAttribute("data-state") ?? "")
          }
        />
      </SidebarProvider>,
    );

    await click(screen.button(TOGGLE));

    // The handler saw the sidebar before it moved.
    expect(seen).toEqual(["expanded"]);
    expect(slot(screen, "sidebar").getAttribute("data-state")).toBe(
      "collapsed",
    );
  });

  it("toggles when its rail is clicked", async () => {
    const screen = render(
      <SidebarProvider>
        <Sidebar>
          <SidebarRail />
        </Sidebar>
      </SidebarProvider>,
    );
    const rail = slot(screen, "sidebar-rail");
    // The rail is reached with the mouse only: the keyboard has the shortcut.
    expect(rail.getAttribute("tabindex")).toBe("-1");
    expect(rail.getAttribute("aria-label")).toBe(TOGGLE);

    await click(rail);

    expect(slot(screen, "sidebar").getAttribute("data-state")).toBe(
      "collapsed",
    );
  });

  it("gives its state to the parts drawn inside it", async () => {
    function Probe() {
      const { state, open, isMobile, openMobile } = useSidebar();
      return (
        <output>{JSON.stringify({ state, open, isMobile, openMobile })}</output>
      );
    }
    const screen = render(
      <SidebarProvider>
        <Probe />
        <SidebarTrigger />
      </SidebarProvider>,
    );
    const read = () => JSON.parse(screen.tag("output")[0].textContent);

    expect(read()).toEqual({
      state: "expanded",
      open: true,
      isMobile: false,
      openMobile: false,
    });

    await click(screen.button(TOGGLE));

    expect(read()).toMatchObject({ state: "collapsed", open: false });
  });

  it("lets a part set the state itself, and remembers it the same way", async () => {
    function Collapse() {
      const { setOpen } = useSidebar();
      return <button onClick={() => setOpen(false)}>Collapse</button>;
    }
    const screen = render(
      <SidebarProvider>
        <Sidebar>
          <Collapse />
        </Sidebar>
      </SidebarProvider>,
    );

    await click(screen.button("Collapse"));
    expect(slot(screen, "sidebar").getAttribute("data-state")).toBe(
      "collapsed",
    );
    expect(testDocument.cookie).toBe(
      `sidebar_state=false; path=/; max-age=${A_WEEK_S}`,
    );

    // Asking again for the same state is not a toggle.
    await click(screen.button("Collapse"));
    expect(slot(screen, "sidebar").getAttribute("data-state")).toBe(
      "collapsed",
    );
  });

  it("refuses a part drawn outside a provider", () => {
    function Probe() {
      useSidebar();
      return null;
    }

    expect(() => renderToStaticMarkup(<Probe />)).toThrow(
      "useSidebar must be used within a SidebarProvider.",
    );
    expect(() => renderToStaticMarkup(<SidebarTrigger />)).toThrow(
      "useSidebar must be used within a SidebarProvider.",
    );
  });
});

describe("ANH-203 sidebar: the keyboard shortcut", () => {
  it.each([
    ["Ctrl+B", { key: "b", ctrlKey: true }],
    ["Cmd+B", { key: "b", metaKey: true }],
  ])(
    "toggles on %s and keeps the browser from acting on it",
    async (_name, key) => {
      const screen = render(<Page />);

      const allowed = await fireWindow("keydown", key);

      expect(slot(screen, "sidebar").getAttribute("data-state")).toBe(
        "collapsed",
      );
      expect(allowed).toBe(false);

      await fireWindow("keydown", key);
      expect(slot(screen, "sidebar").getAttribute("data-state")).toBe(
        "expanded",
      );
    },
  );

  it.each([
    ["B alone", { key: "b" }],
    ["Ctrl with another key", { key: "k", ctrlKey: true }],
    ["Shift+B", { key: "b", shiftKey: true }],
  ])("leaves %s to the page", async (_name, key) => {
    const screen = render(<Page />);

    const allowed = await fireWindow("keydown", key);

    expect(slot(screen, "sidebar").getAttribute("data-state")).toBe("expanded");
    expect(allowed).toBe(true);
    expect(testDocument.cookie).toBe("");
  });

  it("stops listening to the keyboard once the page is left", async () => {
    const screen = render(<Page />);
    screen.unmount();

    const allowed = await fireWindow("keydown", { key: "b", ctrlKey: true });

    // Nobody heard the key: the browser keeps it, and nothing is written.
    expect(allowed).toBe(true);
    expect(testDocument.cookie).toBe("");
  });
});

describe("ANH-203 sidebar: when the page holds the state", () => {
  it("asks the page to change the state and does not change it itself", async () => {
    const onOpenChange = vi.fn();
    const screen = render(<Page provider={{ open: false, onOpenChange }} />);
    expect(slot(screen, "sidebar").getAttribute("data-state")).toBe(
      "collapsed",
    );

    await click(screen.button(TOGGLE));

    expect(onOpenChange).toHaveBeenCalledTimes(1);
    expect(onOpenChange).toHaveBeenCalledWith(true);
    // The page did not answer yet: the sidebar shows what the page says.
    expect(slot(screen, "sidebar").getAttribute("data-state")).toBe(
      "collapsed",
    );
    // The wish is remembered for the next page all the same.
    expect(testDocument.cookie).toBe(
      `sidebar_state=true; path=/; max-age=${A_WEEK_S}`,
    );

    screen.rerender(<Page provider={{ open: true, onOpenChange }} />);
    expect(slot(screen, "sidebar").getAttribute("data-state")).toBe("expanded");

    await click(screen.button(TOGGLE));
    expect(onOpenChange).toHaveBeenLastCalledWith(false);
  });

  it("shows what the page says even when the page gives no way to change it", () => {
    const screen = render(
      <Page provider={{ open: true, defaultOpen: false }} />,
    );

    expect(slot(screen, "sidebar").getAttribute("data-state")).toBe("expanded");
  });
});

describe("ANH-203 sidebar: on a phone", () => {
  /** The stand-ins of the sheet that carries the sidebar on a phone. */
  const sheets = (screen: Screen) =>
    screen.all((el) => el.getAttribute("data-stand-in") === "sheet");
  const sheet = (screen: Screen) => sheets(screen)[0];

  it("is carried by a sheet, closed at first, instead of the fixed column", () => {
    viewport.resizeTo(PHONE);
    const screen = render(<Page />);

    expect(viewport.asked()).toEqual(["(max-width: 767px)"]);
    expect(sheet(screen).getAttribute("data-open")).toBe("false");
    expect(screen.text()).not.toContain("Machines");
    expect(hasSlot(screen, "sidebar-container")).toBe(false);
  });

  it("opens the sheet when its button is clicked, and writes no cookie", async () => {
    viewport.resizeTo(PHONE);
    const screen = render(<Page sidebar={{ side: "right" }} />);

    await click(screen.button(TOGGLE));

    expect(sheet(screen).getAttribute("data-open")).toBe("true");
    expect(screen.text()).toContain("Machines");
    const content = slot(screen, "sidebar");
    expect(content.getAttribute("data-mobile")).toBe("true");
    expect(content.getAttribute("data-sheet-side")).toBe("right");
    // Wider than on a desk: a finger needs the room.
    expect(content.style["--sidebar-width"]).toBe("18rem");
    // A screen reader is told what the sheet is.
    expect(screen.text()).toContain("Sidebar Displays the mobile sidebar.");
    // The state of the desk is not touched: nothing to remember.
    expect(testDocument.cookie).toBe("");
  });

  it("closes when the sheet asks to", async () => {
    viewport.resizeTo(PHONE);
    const screen = render(<Page />);
    await click(screen.button(TOGGLE));

    await click(screen.button("close the sheet"));

    expect(sheet(screen).getAttribute("data-open")).toBe("false");
    expect(screen.text()).not.toContain("Machines");
  });

  it("opens and closes the sheet with the keyboard shortcut", async () => {
    viewport.resizeTo(PHONE);
    const screen = render(<Page />);

    await fireWindow("keydown", { key: "b", ctrlKey: true });
    expect(sheet(screen).getAttribute("data-open")).toBe("true");

    await fireWindow("keydown", { key: "b", metaKey: true });
    expect(sheet(screen).getAttribute("data-open")).toBe("false");
  });

  it("goes back to the fixed column when the window grows", async () => {
    viewport.resizeTo(PHONE);
    const screen = render(<Page />);
    expect(hasSlot(screen, "sidebar-container")).toBe(false);

    await settle(() => viewport.resizeTo(DESK));

    expect(hasSlot(screen, "sidebar-container")).toBe(true);
    expect(sheets(screen).length).toBe(0);
    expect(screen.text()).toContain("Machines");
  });

  it("stays a plain column when it cannot collapse, phone or not", async () => {
    viewport.resizeTo(PHONE);
    const screen = render(
      <Page
        sidebar={{ collapsible: "none", className: "border-r", id: "nav" }}
      />,
    );

    const column = slot(screen, "sidebar");
    expect(sheets(screen).length).toBe(0);
    expect(screen.text()).toContain("Machines");
    expect(column.id).toBe("nav");
    expect(classes(column)).toContain("border-r");
    // Nothing collapses, so no state is announced to the styles.
    expect(column.hasAttribute("data-state")).toBe(false);

    await click(screen.button(TOGGLE));
    expect(screen.text()).toContain("Machines");
  });
});

describe("ANH-203 sidebar: what the column draws", () => {
  it("sets its widths as CSS variables the page can override", () => {
    const screen = render(
      <SidebarProvider
        className="bg-muted"
        style={{ "--sidebar-width": "20rem" } as React.CSSProperties}
        id="shell"
      />,
    );

    const wrapper = slot(screen, "sidebar-wrapper");
    expect(wrapper.style["--sidebar-width"]).toBe("20rem");
    expect(wrapper.style["--sidebar-width-icon"]).toBe("3rem");
    expect(wrapper.id).toBe("shell");
    expect(classes(wrapper)).toContain("bg-muted");

    screen.unmount();
    const plain = render(<SidebarProvider />);
    expect(slot(plain, "sidebar-wrapper").style["--sidebar-width"]).toBe(
      "16rem",
    );
  });

  it("shows the tooltips of its buttons without delay", () => {
    const screen = render(<SidebarProvider />);

    const provider = screen.all(
      (el) => el.getAttribute("data-stand-in") === "tooltip-provider",
    );
    expect(provider.length).toBe(1);
    expect(provider[0].getAttribute("data-delay")).toBe("0");
  });

  it("stands on the left as a plain column unless told otherwise", () => {
    const screen = render(
      <Page sidebar={{ className: "top-14", id: "nav" }} />,
    );

    const sidebar = slot(screen, "sidebar");
    const container = slot(screen, "sidebar-container");
    expect(sidebar.getAttribute("data-side")).toBe("left");
    expect(sidebar.getAttribute("data-variant")).toBe("sidebar");
    expect(classes(container)).toContain("left-0");
    expect(classes(container)).not.toContain("right-0");
    // A plain column has no margin around it.
    expect(classes(container)).not.toContain("p-2");
    // What the page gives goes to the fixed column, not to the gap beside it.
    expect(container.id).toBe("nav");
    expect(classes(container)).toContain("top-14");
    expect(slot(screen, "sidebar-inner").textContent).toBe("Machines");
  });

  it("stands on the right when asked", () => {
    const screen = render(<Page sidebar={{ side: "right" }} />);

    const container = slot(screen, "sidebar-container");
    expect(slot(screen, "sidebar").getAttribute("data-side")).toBe("right");
    expect(classes(container)).toContain("right-0");
    expect(classes(container)).not.toContain("left-0");
  });

  it.each(["floating", "inset"] as const)(
    "leaves a margin around a %s sidebar, and room for it once collapsed to icons",
    (variant) => {
      // The width of the gap once collapsed to icons, for a plain column: the
      // icons alone. A sidebar with a margin adds the margin to it.
      const iconsAlone =
        "group-data-[collapsible=icon]:w-(--sidebar-width-icon)";
      const plain = render(<Page />);
      expect(classes(slot(plain, "sidebar-gap"))).toContain(iconsAlone);
      plain.unmount();

      const screen = render(<Page sidebar={{ variant }} />);

      expect(slot(screen, "sidebar").getAttribute("data-variant")).toBe(
        variant,
      );
      expect(classes(slot(screen, "sidebar-container"))).toContain("p-2");
      expect(classes(slot(screen, "sidebar-gap"))).not.toContain(iconsAlone);
    },
  );
});

describe("ANH-203 sidebar: a menu button", () => {
  /** The stand-ins of the tooltips drawn on the page. */
  const tooltips = (screen: Screen) =>
    screen.all((el) => el.getAttribute("data-stand-in") === "tooltip");

  function Menu({
    open = true,
    button,
  }: {
    open?: boolean;
    button: React.ComponentProps<typeof SidebarMenuButton>;
  }) {
    return (
      <SidebarProvider defaultOpen={open}>
        <SidebarMenuButton {...button} />
      </SidebarProvider>
    );
  }

  it("has no tooltip unless given one", () => {
    const screen = render(<Menu button={{ children: "Machines" }} />);

    expect(tooltips(screen).length).toBe(0);
    expect(slot(screen, "sidebar-menu-button").localName).toBe("button");
    expect(screen.text()).toBe("Machines");
  });

  it("hides its tooltip while the sidebar is expanded: the label is already read", () => {
    const screen = render(
      <Menu
        button={{ children: "Machines", tooltip: "Machines of the site" }}
      />,
    );

    const [tooltip] = tooltips(screen);
    expect(tooltip.textContent).toBe("Machines of the site");
    expect(tooltip.hasAttribute("hidden")).toBe(true);
    expect(tooltip.getAttribute("data-side")).toBe("right");
    expect(tooltip.getAttribute("data-align")).toBe("center");
  });

  it("shows its tooltip once the sidebar is collapsed to icons", async () => {
    const screen = render(
      <SidebarProvider>
        <SidebarMenuButton tooltip="Machines of the site">
          Machines
        </SidebarMenuButton>
        <SidebarTrigger />
      </SidebarProvider>,
    );
    expect(tooltips(screen)[0].hasAttribute("hidden")).toBe(true);

    await click(screen.button(TOGGLE));

    expect(tooltips(screen)[0].hasAttribute("hidden")).toBe(false);
  });

  it("never shows its tooltip on a phone, collapsed or not", () => {
    viewport.resizeTo(PHONE);
    const screen = render(
      <Menu
        open={false}
        button={{ children: "Machines", tooltip: "Machines" }}
      />,
    );

    expect(tooltips(screen)[0].hasAttribute("hidden")).toBe(true);
  });

  it("takes a tooltip as props too, and lets them replace its own", () => {
    const screen = render(
      <Menu
        open={false}
        button={{
          children: "Machines",
          tooltip: { children: "All machines", side: "bottom" },
        }}
      />,
    );

    const [tooltip] = tooltips(screen);
    expect(tooltip.textContent).toBe("All machines");
    expect(tooltip.getAttribute("data-side")).toBe("bottom");
    expect(tooltip.getAttribute("data-align")).toBe("center");
    expect(tooltip.hasAttribute("hidden")).toBe(false);
  });

  it("says whether it is the current page, and its size", () => {
    const screen = render(
      <Menu button={{ children: "Machines", isActive: true, size: "lg" }} />,
    );
    const plain = render(<Menu button={{ children: "Sessions" }} />);

    const [active, other] = screen.all(
      (el) => el.getAttribute("data-slot") === "sidebar-menu-button",
    );
    expect(active.getAttribute("data-active")).toBe("true");
    expect(active.getAttribute("data-size")).toBe("lg");
    expect(classes(active)).toContain("h-12");
    expect(other.getAttribute("data-active")).toBe("false");
    expect(other.getAttribute("data-size")).toBe("default");
    expect(classes(other)).toContain("h-8");
    plain.unmount();
  });

  it("is outlined when asked, and keeps the classes it is given", () => {
    const screen = render(
      <Menu
        button={{ children: "Machines", variant: "outline", className: "mt-4" }}
      />,
    );

    const button = slot(screen, "sidebar-menu-button");
    expect(classes(button)).toContain("bg-background");
    expect(classes(button)).toContain("mt-4");
  });

  it("becomes the link it is given instead of a button", () => {
    const screen = render(
      <Menu
        button={{
          asChild: true,
          isActive: true,
          tooltip: "Machines",
          children: <a href="#machines">Machines</a>,
        }}
      />,
    );

    const link = slot(screen, "sidebar-menu-button");
    expect(link.localName).toBe("a");
    expect(link.getAttribute("href")).toBe("#machines");
    expect(link.getAttribute("data-active")).toBe("true");
    expect(screen.tag("button").length).toBe(0);
  });
});

describe("ANH-203 sidebar: the parts of a menu", () => {
  it("draws the skeleton of a loading entry at 70 % of the width, with an icon when asked", () => {
    const screen = render(
      <>
        <SidebarMenuSkeleton className="my-1" />
        <SidebarMenuSkeleton showIcon />
      </>,
    );

    const texts = screen.all(
      (el) => el.getAttribute("data-sidebar") === "menu-skeleton-text",
    );
    const icons = screen.all(
      (el) => el.getAttribute("data-sidebar") === "menu-skeleton-icon",
    );
    expect(texts.length).toBe(2);
    expect(texts[0].style["--skeleton-width"]).toBe("70%");
    // One icon, in the second entry only.
    expect(icons.length).toBe(1);
    expect(icons[0].parentElement === texts[1].parentElement).toBe(true);
    expect(classes(texts[0].parentElement as TestElement)).toContain("my-1");
  });

  it("shows an action only on hover when asked", () => {
    const screen = render(
      <>
        <SidebarMenuAction aria-label="More" />
        <SidebarMenuAction aria-label="Delete" showOnHover />
      </>,
    );

    const [always, onHover] = screen.tag("button");
    expect(classes(always)).not.toContain("md:opacity-0");
    expect(classes(onHover)).toContain("md:opacity-0");
    expect(onHover.getAttribute("aria-label")).toBe("Delete");
  });

  it("draws a sub-entry as a link, smaller when asked, marked when current", () => {
    const screen = render(
      <>
        <SidebarMenuSubButton href="/machines/1">
          Centri Paris
        </SidebarMenuSubButton>
        <SidebarMenuSubButton href="/machines/2" size="sm" isActive>
          Centri Lyon
        </SidebarMenuSubButton>
      </>,
    );

    const [medium, small] = screen.tag("a");
    expect(medium.getAttribute("href")).toBe("/machines/1");
    expect(medium.getAttribute("data-size")).toBe("md");
    expect(medium.getAttribute("data-active")).toBe("false");
    expect(classes(medium)).toContain("text-sm");
    expect(classes(medium)).not.toContain("text-xs");
    expect(small.getAttribute("data-size")).toBe("sm");
    expect(small.getAttribute("data-active")).toBe("true");
    expect(classes(small)).toContain("text-xs");
    expect(classes(small)).not.toContain("text-sm");
  });

  it.each([
    ["group label", SidebarGroupLabel, "div", "group-label"],
    ["group action", SidebarGroupAction, "button", "group-action"],
    ["menu action", SidebarMenuAction, "button", "menu-action"],
    ["sub-entry", SidebarMenuSubButton, "a", "menu-sub-button"],
  ] as const)(
    "draws a %s as its own tag, or as the element it is given",
    (_name, Part, tag, name) => {
      const own = render(<Part className="mx-1">Label</Part>);
      const drawn = own.all((el) => el.getAttribute("data-sidebar") === name);
      expect(drawn.length).toBe(1);
      expect(drawn[0].localName).toBe(tag);
      expect(classes(drawn[0])).toContain("mx-1");
      own.unmount();

      const given = render(
        <Part asChild className="mx-1">
          <summary>Label</summary>
        </Part>,
      );
      const merged = given.all(
        (el) => el.getAttribute("data-sidebar") === name,
      );
      expect(merged.length).toBe(1);
      expect(merged[0].localName).toBe("summary");
      expect(merged[0].textContent).toBe("Label");
      expect(classes(merged[0])).toContain("mx-1");
      // The element given is drawn in place of the tag, not inside it.
      expect(merged[0].parentElement === given.container).toBe(true);
    },
  );

  it.each([
    ["inset", SidebarInset, "main"],
    ["header", SidebarHeader, "div"],
    ["footer", SidebarFooter, "div"],
    ["content", SidebarContent, "div"],
    ["group", SidebarGroup, "div"],
    ["group-content", SidebarGroupContent, "div"],
    ["menu", SidebarMenu, "ul"],
    ["menu-item", SidebarMenuItem, "li"],
    ["menu-badge", SidebarMenuBadge, "div"],
    ["menu-sub", SidebarMenuSub, "ul"],
    ["menu-sub-item", SidebarMenuSubItem, "li"],
  ] as const)(
    "draws the %s as one element that keeps what it is given",
    (name, Part, tag) => {
      const screen = render(
        <Part id="part" className="mx-1" aria-label="Navigation">
          Machines
        </Part>,
      );

      const part = slot(screen, `sidebar-${name}`);
      expect(part.localName).toBe(tag);
      expect(part.id).toBe("part");
      expect(part.getAttribute("aria-label")).toBe("Navigation");
      expect(classes(part)).toContain("mx-1");
      expect(part.textContent).toBe("Machines");
    },
  );

  it("draws its search field as a text field that keeps what it is given", () => {
    const screen = render(
      <SidebarInput placeholder="Search a machine" className="mx-1" />,
    );

    const field = slot(screen, "sidebar-input");
    expect(field.localName).toBe("input");
    expect(field.getAttribute("placeholder")).toBe("Search a machine");
    expect(classes(field)).toContain("mx-1");
    // Lower than a field of a form, to fit the column.
    expect(classes(field)).toContain("h-8");
  });

  it("draws its separator with the border of the sidebar, not the one of the page", () => {
    const screen = render(<SidebarSeparator className="my-2" />);

    const separator = slot(screen, "sidebar-separator");
    expect(classes(separator)).toContain("bg-sidebar-border");
    expect(classes(separator)).toContain("my-2");
  });
});

describe("ANH-203 sidebar: leaving the page", () => {
  it("stops listening to the size of the window", () => {
    const screen = render(<Page />);
    expect(viewport.listeners()).toBe(1);

    screen.unmount();

    expect(viewport.listeners()).toBe(0);
  });
});

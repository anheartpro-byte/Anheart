import {
  click,
  render,
  textOf,
  type,
  type TestElement,
} from "@/test-support/render";
import type { ColumnDef } from "@tanstack/react-table";
import { describe, expect, it, vi } from "vitest";
import { DataTable } from "./data-table";

/**
 * The table of the lists of the dashboard (machines, riders, sessions): the
 * rows it shows, what it says when there are none or while they load, the
 * search box, the pages, and the sort of a column when its header is clicked.
 *
 * TanStack Table runs for real: it has no need of a browser.
 */

type Machine = { name: string; site: string; sessions: number };

const machines: Machine[] = [
  { name: "Centri Paris", site: "Paris", sessions: 12 },
  { name: "Centri Lyon", site: "Lyon", sessions: 40 },
  { name: "Atlas", site: "Paris", sessions: 3 },
];

const columns: ColumnDef<Machine, unknown>[] = [
  { accessorKey: "name", header: "Machine" },
  { accessorKey: "site", header: "Site" },
  { accessorKey: "sessions", header: "Sessions" },
  // A column of buttons: it holds no value, so nothing to sort by.
  { id: "actions", header: "Actions", cell: () => "Open" },
];

/** Enough machines for several pages: "Machine 01" to "Machine 11". */
const eleven: Machine[] = Array.from({ length: 11 }, (_, index) => ({
  name: `Machine ${String(index + 1).padStart(2, "0")}`,
  site: "Paris",
  sessions: index,
}));

type Screen = ReturnType<typeof render>;

/** The rows of the body, each as the texts of its cells. */
function rows(screen: Screen): string[][] {
  return screen
    .all(
      (element) =>
        element.localName === "tr" &&
        element.parentElement?.localName === "tbody",
    )
    .map((row) => row.children.map(textOf));
}

/** The first cell of each row: enough to tell which rows are shown, and in which order. */
function names(screen: Screen): string[] {
  return rows(screen).map(([name]) => name);
}

function header(screen: Screen, label: string): TestElement {
  const found = screen.tag("th").filter((cell) => textOf(cell) === label);
  if (found.length !== 1)
    throw new Error(`${found.length} headers read "${label}"`);
  return found[0];
}

/** Which arrow a header shows: the Lucide icon drawn in it. */
function arrow(screen: Screen, label: string): string | undefined {
  const icon = header(screen, label).children[0].children.at(-1)?.children[0];
  return icon?.className
    .split(" ")
    .find((name) => name.startsWith("lucide-chevron"));
}

function searchBox(screen: Screen): TestElement {
  return screen.tag("input")[0];
}

/** The buttons of the pages: the previous one, then the next one. */
function pageButtons(screen: Screen) {
  const [previous, next] = screen.tag("button");
  return { previous, next };
}

describe("ANH-203 data table: the rows", () => {
  it("shows one row per item, under the headers of the columns", () => {
    const screen = render(<DataTable columns={columns} data={machines} />);

    expect(screen.tag("th").map(textOf)).toEqual([
      "Machine",
      "Site",
      "Sessions",
      "Actions",
    ]);
    expect(rows(screen)).toEqual([
      ["Centri Paris", "Paris", "12", "Open"],
      ["Centri Lyon", "Lyon", "40", "Open"],
      ["Atlas", "Paris", "3", "Open"],
    ]);
    expect(screen.text()).toContain("3 of 3 items");
  });

  it("hands the item of a clicked row to the page, and shows that rows can be clicked", async () => {
    const onRowClick = vi.fn();
    const screen = render(
      <DataTable columns={columns} data={machines} onRowClick={onRowClick} />,
    );
    const [, lyon] = screen.all(
      (element) =>
        element.localName === "tr" &&
        element.parentElement?.localName === "tbody",
    );

    await click(lyon);

    expect(onRowClick).toHaveBeenCalledTimes(1);
    expect(onRowClick).toHaveBeenCalledWith(machines[1]);
    expect(lyon.className.split(" ")).toContain("cursor-pointer");
  });

  it("does not invite a click on a row when the page listens to none", async () => {
    const screen = render(<DataTable columns={columns} data={machines} />);
    const [row] = screen.all(
      (element) =>
        element.localName === "tr" &&
        element.parentElement?.localName === "tbody",
    );

    await click(row);

    expect(row.className.split(" ")).not.toContain("cursor-pointer");
    expect(names(screen)).toEqual(["Centri Paris", "Centri Lyon", "Atlas"]);
  });

  it("leaves the cell above a column empty when its neighbours are grouped under a title", () => {
    const grouped: ColumnDef<Machine, unknown>[] = [
      {
        header: "Identity",
        columns: [
          { accessorKey: "name", header: "Machine" },
          { accessorKey: "site", header: "Site" },
        ],
      },
      { accessorKey: "sessions", header: "Sessions" },
    ];

    const screen = render(<DataTable columns={grouped} data={machines} />);

    const [titles, labels] = screen
      .all(
        (element) =>
          element.localName === "tr" &&
          element.parentElement?.localName === "thead",
      )
      .map((row) => row.children.map(textOf));
    expect(titles).toEqual(["Identity", ""]);
    expect(labels).toEqual(["Machine", "Site", "Sessions"]);
  });
});

describe("ANH-203 data table: nothing to show", () => {
  it("says there are no results, in the words of the page when it gives some", () => {
    const plain = render(<DataTable columns={columns} data={[]} />);
    expect(plain.text()).toContain("No results found");
    expect(plain.text()).toContain("0 of 0 items");
    expect(plain.tag("table").length).toBe(0);
    plain.unmount();

    const worded = render(
      <DataTable
        columns={columns}
        data={[]}
        emptyMessage="No machine yet"
        emptyAction={<button>Add a machine</button>}
      />,
    );
    expect(worded.text()).toContain("No machine yet");
    expect(worded.text()).not.toContain("No results found");
    expect(worded.hasButton("Add a machine")).toBe(true);
  });

  it("draws placeholders, and neither the search box nor the rows, while the list loads", () => {
    const screen = render(
      <DataTable columns={columns} data={machines} isLoading />,
    );

    expect(screen.tag("table").length).toBe(0);
    expect(screen.tag("input").length).toBe(0);
    expect(screen.text()).toBe("");
    // The search box and the count, a header of four columns, five rows of four.
    expect(
      screen.all((element) => element.getAttribute("data-slot") === "skeleton")
        .length,
    ).toBe(2 + 4 + 5 * 4);

    screen.rerender(<DataTable columns={columns} data={machines} />);
    expect(names(screen)).toEqual(["Centri Paris", "Centri Lyon", "Atlas"]);
    expect(
      screen.all((element) => element.getAttribute("data-slot") === "skeleton")
        .length,
    ).toBe(0);
  });
});

describe("ANH-203 data table: the search box", () => {
  it("invites to search in the words of the page", () => {
    const plain = render(<DataTable columns={columns} data={machines} />);
    expect(searchBox(plain).getAttribute("placeholder")).toBe("Search...");
    plain.unmount();

    const worded = render(
      <DataTable
        columns={columns}
        data={machines}
        searchPlaceholder="Search a machine"
      />,
    );
    expect(searchBox(worded).getAttribute("placeholder")).toBe(
      "Search a machine",
    );
  });

  it("keeps the rows that hold the text typed, in any column, whatever the case", async () => {
    const screen = render(<DataTable columns={columns} data={machines} />);

    // "paris" is in the name of one machine and in the site of two.
    await type(searchBox(screen), "paris");
    expect(names(screen)).toEqual(["Centri Paris", "Atlas"]);
    expect(screen.text()).toContain("2 of 3 items");
    expect(searchBox(screen).value).toBe("paris");

    await type(searchBox(screen), "LYON");
    expect(names(screen)).toEqual(["Centri Lyon"]);
    expect(screen.text()).toContain("1 of 3 items");
  });

  it("says there are no results when no row holds the text, and shows all rows again once cleared", async () => {
    const screen = render(
      <DataTable
        columns={columns}
        data={machines}
        emptyMessage="No machine found"
      />,
    );

    await type(searchBox(screen), "toulouse");
    expect(screen.text()).toContain("No machine found");
    expect(screen.text()).toContain("0 of 3 items");
    expect(screen.tag("table").length).toBe(0);

    await type(searchBox(screen), "");
    expect(names(screen)).toEqual(["Centri Paris", "Centri Lyon", "Atlas"]);
    expect(screen.text()).toContain("3 of 3 items");
  });
});

describe("ANH-203 data table: the pages", () => {
  it("shows ten rows a page unless told otherwise, and no page buttons for a single page", () => {
    const one = render(
      <DataTable columns={columns} data={eleven.slice(0, 10)} />,
    );
    expect(rows(one).length).toBe(10);
    expect(one.tag("button").length).toBe(0);
    expect(one.text()).not.toContain("Page 1");
    one.unmount();

    const two = render(<DataTable columns={columns} data={eleven} />);
    expect(rows(two).length).toBe(10);
    expect(two.text()).toContain("Page 1 of 2");
    expect(two.text()).toContain("11 of 11 items");
  });

  it("moves from page to page, and stops at both ends", async () => {
    const screen = render(
      <DataTable columns={columns} data={eleven} pageSize={4} />,
    );
    const disabled = () => ({
      previous: pageButtons(screen).previous.hasAttribute("disabled"),
      next: pageButtons(screen).next.hasAttribute("disabled"),
    });

    expect(screen.text()).toContain("Page 1 of 3");
    expect(names(screen)).toEqual([
      "Machine 01",
      "Machine 02",
      "Machine 03",
      "Machine 04",
    ]);
    expect(disabled()).toEqual({ previous: true, next: false });

    await click(pageButtons(screen).next);
    expect(screen.text()).toContain("Page 2 of 3");
    expect(names(screen)).toEqual([
      "Machine 05",
      "Machine 06",
      "Machine 07",
      "Machine 08",
    ]);
    expect(disabled()).toEqual({ previous: false, next: false });

    await click(pageButtons(screen).next);
    expect(screen.text()).toContain("Page 3 of 3");
    expect(names(screen)).toEqual(["Machine 09", "Machine 10", "Machine 11"]);
    expect(disabled()).toEqual({ previous: false, next: true });

    await click(pageButtons(screen).previous);
    expect(screen.text()).toContain("Page 2 of 3");
    expect(names(screen)[0]).toBe("Machine 05");
  });

  it("counts the pages of what the search keeps", async () => {
    const screen = render(
      <DataTable columns={columns} data={eleven} pageSize={4} />,
    );

    // "Machine 1" keeps Machine 10 and Machine 11: a single page.
    await type(searchBox(screen), "machine 1");

    expect(names(screen)).toEqual(["Machine 10", "Machine 11"]);
    expect(screen.tag("button").length).toBe(0);
  });
});

describe("ANH-203 data table: sorting by a column", () => {
  it("sorts a column of text from A to Z, then from Z to A, then not at all", async () => {
    const screen = render(<DataTable columns={columns} data={machines} />);
    expect(arrow(screen, "Machine")).toBe("lucide-chevrons-up-down");

    await click(header(screen, "Machine"));
    expect(names(screen)).toEqual(["Atlas", "Centri Lyon", "Centri Paris"]);
    expect(arrow(screen, "Machine")).toBe("lucide-chevron-up");

    await click(header(screen, "Machine"));
    expect(names(screen)).toEqual(["Centri Paris", "Centri Lyon", "Atlas"]);
    expect(arrow(screen, "Machine")).toBe("lucide-chevron-down");

    await click(header(screen, "Machine"));
    expect(names(screen)).toEqual(["Centri Paris", "Centri Lyon", "Atlas"]);
    expect(arrow(screen, "Machine")).toBe("lucide-chevrons-up-down");
  });

  it("sorts a column of numbers from the largest first", async () => {
    const screen = render(<DataTable columns={columns} data={machines} />);

    await click(header(screen, "Sessions"));

    expect(rows(screen).map((row) => row[2])).toEqual(["40", "12", "3"]);
    expect(arrow(screen, "Sessions")).toBe("lucide-chevron-down");
    // One column sorts at a time: the others show they are not the sort.
    expect(arrow(screen, "Machine")).toBe("lucide-chevrons-up-down");
  });

  it("sorts the whole list, not the page shown", async () => {
    const screen = render(
      <DataTable columns={columns} data={eleven} pageSize={4} />,
    );

    await click(header(screen, "Sessions"));

    expect(names(screen)).toEqual([
      "Machine 11",
      "Machine 10",
      "Machine 09",
      "Machine 08",
    ]);
  });

  it("offers no sort on a column that holds no value", async () => {
    const screen = render(<DataTable columns={columns} data={machines} />);
    const actions = header(screen, "Actions");

    expect(arrow(screen, "Actions")).toBeUndefined();
    expect(actions.className.split(" ")).not.toContain("cursor-pointer");
    expect(header(screen, "Machine").className.split(" ")).toContain(
      "cursor-pointer",
    );

    await click(actions);
    expect(names(screen)).toEqual(["Centri Paris", "Centri Lyon", "Atlas"]);
  });
});

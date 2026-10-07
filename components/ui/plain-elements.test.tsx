import { render, type } from "@/test-support/render";
import type { ComponentType, ReactNode } from "react";
import { describe, expect, it } from "vitest";
import { draw } from "@/test-support/markup";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "./card";
import { Input } from "./input";
import { Skeleton } from "./skeleton";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "./table";
import { Textarea } from "./textarea";

/**
 * The parts of `components/ui` that draw one HTML element each: the card and
 * its parts, the table and its parts, the skeleton, the input, the textarea.
 *
 * What each owes the page that uses it: the right element, marked with the
 * `data-slot` the styles of its parents select it by; the caller's classes
 * added to its own, the caller's winning where both set the same thing; every
 * other prop passed on to the element.
 */

type Part = ComponentType<{
  className?: string;
  id?: string;
  children?: ReactNode;
}>;

/**
 * Each part: its slot, the component, the element it draws, a class it sets by
 * itself, and a class of the caller that sets the same thing otherwise.
 */
const CONTAINERS: [string, Part, string, string, string][] = [
  ["card", Card, "div", "py-6", "py-2"],
  ["card-header", CardHeader, "div", "px-6", "px-4"],
  ["card-title", CardTitle, "div", "font-semibold", "font-normal"],
  ["card-description", CardDescription, "div", "text-sm", "text-xs"],
  ["card-action", CardAction, "div", "self-start", "self-center"],
  ["card-content", CardContent, "div", "px-6", "px-0"],
  ["card-footer", CardFooter, "div", "items-center", "items-end"],
  ["skeleton", Skeleton, "div", "rounded-md", "rounded-full"],
  ["table", Table, "table", "text-sm", "text-xs"],
  [
    "table-header",
    TableHeader,
    "thead",
    "[&_tr]:border-b",
    "[&_tr]:border-b-0",
  ],
  [
    "table-body",
    TableBody,
    "tbody",
    "[&_tr:last-child]:border-0",
    "[&_tr:last-child]:border-2",
  ],
  ["table-footer", TableFooter, "tfoot", "font-medium", "font-bold"],
  ["table-row", TableRow, "tr", "border-b", "border-b-0"],
  ["table-head", TableHead, "th", "h-12", "h-8"],
  ["table-cell", TableCell, "td", "py-4", "py-2"],
  ["table-caption", TableCaption, "caption", "mt-4", "mt-2"],
];

/** The two fields: they hold a value, not children. */
const FIELDS: [string, Part, string, string, string][] = [
  ["input", Input, "input", "h-9", "h-12"],
  ["textarea", Textarea, "textarea", "min-h-16", "min-h-32"],
];

const PARTS = [...CONTAINERS, ...FIELDS];

describe("ANH-203 plain elements: what each part draws", () => {
  it.each(PARTS)(
    "%s: one element of the right tag, marked with its slot",
    (slot, Part, tag) => {
      const page = draw(<Part />);

      expect(page.slot(slot).localName).toBe(tag);
    },
  );

  it.each(PARTS)(
    "%s: passes the caller's other props on to the element",
    (slot, Part) => {
      const page = draw(<Part id="mine" />);

      expect(page.slot(slot).getAttribute("id")).toBe("mine");
    },
  );

  it.each(CONTAINERS)(
    "%s: draws its children inside the element",
    (slot, Part) => {
      const page = draw(
        <Part>
          <b>Solde</b>
        </Part>,
      );

      expect(page.slot(slot).children.map((child) => child.localName)).toEqual([
        "b",
      ]);
      expect(page.slot(slot).text).toBe("Solde");
    },
  );
});

describe("ANH-203 plain elements: the caller's classes", () => {
  it.each(PARTS)(
    "%s: keeps its own classes next to a class of the caller",
    (slot, Part, _tag, own) => {
      const page = draw(<Part className="mine" />);

      expect(page.slot(slot).classes).toContain(own);
      expect(page.slot(slot).classes).toContain("mine");
    },
  );

  it.each(PARTS)(
    "%s: the caller's class replaces the one that sets the same thing",
    (slot, Part, _tag, own, instead) => {
      const page = draw(<Part className={instead} />);

      expect(page.slot(slot).classes).toContain(instead);
      expect(page.slot(slot).classes).not.toContain(own);
    },
  );
});

describe("ANH-203 table: the frame around it", () => {
  it("draws the table inside a frame that scrolls sideways", () => {
    const page = draw(
      <Table>
        <TableBody>
          <TableRow>
            <TableCell>Centri Paris</TableCell>
          </TableRow>
        </TableBody>
      </Table>,
    );

    const frame = page.slot("table-container");
    expect(frame.classes).toContain("overflow-x-auto");
    expect(page.slot("table").parent).toBe(frame);
    // The whole table is in the frame: nothing is drawn beside it.
    expect(page.children).toEqual([frame]);
  });

  it("gives the caller's classes and props to the table, not to the frame", () => {
    const page = draw(<Table className="mine" aria-label="Machines" />);

    expect(page.slot("table").classes).toContain("mine");
    expect(page.slot("table").getAttribute("aria-label")).toBe("Machines");
    expect(page.slot("table-container").classes).not.toContain("mine");
    expect(page.slot("table-container").hasAttribute("aria-label")).toBe(false);
  });

  it("marks a row the caller says is selected", () => {
    const page = draw(<TableRow data-state="selected" />);

    // The class that paints a selected row reads this attribute.
    expect(page.slot("table-row").getAttribute("data-state")).toBe("selected");
    expect(page.slot("table-row").classes).toContain(
      "data-[state=selected]:bg-muted",
    );
  });
});

describe("ANH-203 input", () => {
  it("is of the type the caller asks for", () => {
    const page = draw(<Input type="email" />);

    expect(page.slot("input").getAttribute("type")).toBe("email");
  });

  it("sets no type of its own when the caller gives none", () => {
    const page = draw(<Input />);

    expect(page.slot("input").hasAttribute("type")).toBe(false);
  });

  it("passes the field's props on: name, placeholder, disabled, invalid", () => {
    const page = draw(
      <Input name="hrMax" placeholder="185" disabled aria-invalid />,
    );

    const input = page.slot("input");
    expect(input.getAttribute("name")).toBe("hrMax");
    expect(input.getAttribute("placeholder")).toBe("185");
    expect(input.hasAttribute("disabled")).toBe(true);
    expect(input.getAttribute("aria-invalid")).toBe("true");
  });

  it("tells the caller what is typed in it", async () => {
    const typed: string[] = [];
    const screen = render(
      <Input
        type="text"
        onChange={(event) => typed.push(event.target.value)}
      />,
    );

    await type(screen.tag("input")[0], "185");

    expect(typed.at(-1)).toBe("185");
  });
});

describe("ANH-203 textarea", () => {
  it("passes the field's props on: name, rows, placeholder, disabled", () => {
    const page = draw(
      <Textarea name="note" rows={4} placeholder="Remarque" disabled />,
    );

    const textarea = page.slot("textarea");
    expect(textarea.getAttribute("name")).toBe("note");
    expect(textarea.getAttribute("rows")).toBe("4");
    expect(textarea.getAttribute("placeholder")).toBe("Remarque");
    expect(textarea.hasAttribute("disabled")).toBe(true);
  });

  it("shows the value the caller holds", () => {
    const page = draw(<Textarea value="Reprise progressive" readOnly />);

    expect(page.slot("textarea").text).toBe("Reprise progressive");
  });

  it("tells the caller what is typed in it", async () => {
    const typed: string[] = [];
    const screen = render(
      <Textarea onChange={(event) => typed.push(event.target.value)} />,
    );

    await type(screen.tag("textarea")[0], "Reprise");

    expect(typed.at(-1)).toBe("Reprise");
  });
});

import {
  elementsOf,
  render,
  settle,
  textOf,
  type TestElement,
} from "@/test-support/render";
import { installLayout, installResizeObserver } from "@/test-support/browser";
import type { ComponentProps, ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Bar, BarChart, Line, LineChart, Pie, PieChart, XAxis } from "recharts";
import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartStyle,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "./chart";

/**
 * The charts of the site: the colors of a series written as CSS variables for
 * each theme, and what the tooltip and the legend say of a point, from the
 * labels, colors and icons of the configuration.
 *
 * Recharts runs for real: a bar chart, a pie chart and a line chart are drawn
 * on the document of the tests, and our tooltip and our legend receive what
 * Recharts gives them. All it lacks here is a size, which the tests give: a
 * box for every element, and a `ResizeObserver` that never fires. The tooltip
 * is opened with `defaultIndex`, since no mouse hovers anything. The cases
 * Recharts would need a pointer or another chart for are drawn directly, with
 * entries shaped as the ones those charts were seen to give.
 */

const config = {
  desktop: { label: "Desktop", color: "#2563eb" },
  mobile: { label: "Mobile", theme: { light: "#60a5fa", dark: "#1d4ed8" } },
  visitors: { label: "Visitors" },
  chrome: { label: "Chrome", color: "#ea580c" },
  safari: { label: "Safari", color: "#0891b2" },
} satisfies ChartConfig;

const months = [
  { month: "January", desktop: 186, mobile: 80 },
  { month: "February", desktop: 305, mobile: 200 },
];
const browsers = [
  { browser: "chrome", visitors: 275, fill: "var(--color-chrome)" },
  { browser: "safari", visitors: 200, fill: "var(--color-safari)" },
];

type TooltipProps = ComponentProps<typeof ChartTooltipContent>;
type TooltipEntry = NonNullable<TooltipProps["payload"]>[number];
type LegendProps = ComponentProps<typeof ChartLegendContent>;
type LegendEntry = NonNullable<LegendProps["payload"]>[number];

/** The entries of February in the bar chart below, as Recharts gives them to a tooltip. */
const february = months[1];
const desktop: TooltipEntry = {
  dataKey: "desktop",
  name: "desktop",
  color: "var(--color-desktop)",
  fill: "var(--color-desktop)",
  value: 305,
  payload: february,
};
const mobile: TooltipEntry = {
  dataKey: "mobile",
  name: "mobile",
  color: "var(--color-mobile)",
  fill: "var(--color-mobile)",
  value: 200,
  payload: february,
};

type Screen = ReturnType<typeof render>;

function classes(element: TestElement): string[] {
  return element.className.split(" ");
}

/** The elements of one render that carry a class: other charts of the page are not searched. */
function withClass(screen: Screen, name: string): TestElement[] {
  return elementsOf(screen.container).filter((element) =>
    classes(element).includes(name),
  );
}

/** What our tooltip drew in a real chart: nothing while no point is active. */
function tooltipOf(screen: Screen): TestElement | undefined {
  return withClass(screen, "recharts-tooltip-wrapper")[0]?.children[0];
}

function legendOf(screen: Screen): TestElement | undefined {
  return withClass(screen, "recharts-legend-wrapper")[0]?.children[0];
}

/** Draws one of our parts alone in a container, as Recharts would hand it its props. */
function inChart(part: ReactElement, chartConfig: ChartConfig = config) {
  const screen = render(
    <ChartContainer config={chartConfig}>{part}</ChartContainer>,
  );
  const drawn = withClass(screen, "recharts-responsive-container")[0]
    .children[0] as TestElement | undefined;
  return { screen, drawn };
}

/** The rows of a tooltip: one per series. The label, when above them, is not a row. */
function rowsOf(tooltip: TestElement): TestElement[] {
  const list = tooltip.children[tooltip.children.length - 1];
  return list.children;
}

/** The colored mark of a row, if it has one. */
function indicatorOf(row: TestElement): TestElement | undefined {
  return row.children.find((child) => child.style["--color-bg"] !== undefined);
}

/** A stylesheet as a list of rules: the selector, then the declarations. */
function rulesOf(css: string): { selector: string; declarations: string[] }[] {
  return css
    .split("}")
    .map((block) => block.split("{"))
    .filter((parts) => parts.length === 2)
    .map(([selector, body]) => ({
      selector: selector.trim(),
      declarations: body
        .split("\n")
        .map((line) => line.trim())
        .filter((line) => line !== ""),
    }));
}

function styleOf(html: string): string {
  const start = html.indexOf("<style>");
  const end = html.indexOf("</style>");
  return start === -1 ? "" : html.slice(start + "<style>".length, end);
}

function Flag() {
  return <svg data-icon="flag" />;
}

beforeEach(() => {
  // What the real `ResponsiveContainer` asks for before it draws its chart.
  installResizeObserver();
  installLayout({ width: 600, height: 300 });
});

describe("ANH-203 chart: the colors of the series as CSS variables", () => {
  it("writes one variable per colored series, for the light theme and for the dark one", () => {
    const html = renderToStaticMarkup(
      <ChartStyle id="chart-load" config={config} />,
    );

    expect(rulesOf(styleOf(html))).toEqual([
      {
        selector: "[data-chart=chart-load]",
        declarations: [
          "--color-desktop: #2563eb;",
          "--color-mobile: #60a5fa;",
          "--color-chrome: #ea580c;",
          "--color-safari: #0891b2;",
        ],
      },
      {
        selector: ".dark [data-chart=chart-load]",
        declarations: [
          "--color-desktop: #2563eb;",
          "--color-mobile: #1d4ed8;",
          "--color-chrome: #ea580c;",
          "--color-safari: #0891b2;",
        ],
      },
    ]);
  });

  it("writes nothing at all when no series has a color", () => {
    const html = renderToStaticMarkup(
      <ChartStyle
        id="chart-load"
        config={{ visitors: { label: "Visitors" }, total: {} }}
      />,
    );

    expect(html).toBe("");
  });

  it("leaves a series out of the theme it has no color for", () => {
    const html = renderToStaticMarkup(
      <ChartStyle
        id="chart-load"
        config={{
          desktop: { color: "#2563eb" },
          mobile: { theme: { light: "#60a5fa", dark: "" } },
        }}
      />,
    );

    const [light, dark] = rulesOf(styleOf(html));
    expect(light.declarations).toEqual([
      "--color-desktop: #2563eb;",
      "--color-mobile: #60a5fa;",
    ]);
    expect(dark.declarations).toEqual(["--color-desktop: #2563eb;"]);
  });

  it("names the container after the id it is given, and aims its variables at it", () => {
    const html = renderToStaticMarkup(
      <ChartContainer
        id="load"
        config={config}
        className="h-64"
        aria-label="Load"
      >
        <BarChart data={months} />
      </ChartContainer>,
    );

    expect(html).toContain('data-chart="chart-load"');
    expect(html).toContain('aria-label="Load"');
    expect(html).toMatch(/class="[^"]*\bh-64\b/);
    expect(rulesOf(styleOf(html)).map((rule) => rule.selector)).toEqual([
      "[data-chart=chart-load]",
      ".dark [data-chart=chart-load]",
    ]);
  });

  it("gives each container without an id a name of its own, usable in a selector", () => {
    const html = renderToStaticMarkup(
      <>
        <ChartContainer config={config}>
          <BarChart data={months} />
        </ChartContainer>
        <ChartContainer config={config}>
          <BarChart data={months} />
        </ChartContainer>
      </>,
    );

    const names = [...html.matchAll(/data-chart="([^"]+)"/g)].map(
      (found) => found[1],
    );
    expect(names).toHaveLength(2);
    expect(new Set(names).size).toBe(2);
    for (const name of names) {
      // An attribute selector without quotes takes an identifier: no colon.
      expect(name).toMatch(/^chart-[A-Za-z0-9_-]+$/);
      expect(html).toContain(`.dark [data-chart=${name}] {`);
    }
  });

  it("writes no stylesheet in a container whose series have no color", () => {
    const html = renderToStaticMarkup(
      <ChartContainer id="load" config={{ visitors: { label: "Visitors" } }}>
        <BarChart data={months} />
      </ChartContainer>,
    );

    expect(html).toContain('data-chart="chart-load"');
    expect(html).not.toContain("<style");
  });
});

describe("ANH-203 chart: the tooltip and the legend of a real chart", () => {
  function Bars({
    tooltip = <ChartTooltipContent />,
    legend = <ChartLegendContent />,
    index = 1,
  }: {
    tooltip?: ReactElement;
    legend?: ReactElement;
    index?: number;
  }) {
    return (
      <ChartContainer config={config}>
        <BarChart data={months}>
          <XAxis dataKey="month" hide />
          <Bar
            dataKey="desktop"
            fill="var(--color-desktop)"
            isAnimationActive={false}
          />
          <Bar
            dataKey="mobile"
            fill="var(--color-mobile)"
            isAnimationActive={false}
          />
          <ChartTooltip defaultIndex={index} content={tooltip} />
          <ChartLegend content={legend} />
        </BarChart>
      </ChartContainer>
    );
  }

  it("says the month of the bar pointed at, then each series by its label with its value", () => {
    const screen = render(<Bars />);

    const tooltip = tooltipOf(screen) as TestElement;
    expect(textOf(tooltip)).toBe("February Desktop 305 Mobile 200");
    expect(rowsOf(tooltip).map(textOf)).toEqual(["Desktop 305", "Mobile 200"]);
  });

  it("marks each row of the tooltip with the color of its series", () => {
    const screen = render(<Bars index={0} />);

    const tooltip = tooltipOf(screen) as TestElement;
    expect(textOf(tooltip)).toBe("January Desktop 186 Mobile 80");
    expect(
      rowsOf(tooltip).map((row) => indicatorOf(row)?.style["--color-bg"]),
    ).toEqual(["var(--color-desktop)", "var(--color-mobile)"]);
    expect(
      rowsOf(tooltip).map((row) => indicatorOf(row)?.style["--color-border"]),
    ).toEqual(["var(--color-desktop)", "var(--color-mobile)"]);
  });

  it("lists the series in the legend by their label, each with a square of its color", () => {
    const screen = render(<Bars />);

    const legend = legendOf(screen) as TestElement;
    expect(legend.children.map(textOf)).toEqual(["Desktop", "Mobile"]);
    expect(
      legend.children.map((entry) => entry.children[0].style.backgroundColor),
    ).toEqual(["var(--color-desktop)", "var(--color-mobile)"]);
    // Below the chart unless told otherwise: the space is above the legend.
    expect(classes(legend)).toContain("pt-3");
    expect(classes(legend)).not.toContain("pb-3");
  });

  it("draws no tooltip while no point is active", () => {
    const screen = render(
      <ChartContainer config={config}>
        <BarChart data={months}>
          <Bar dataKey="desktop" isAnimationActive={false} />
          <ChartTooltip content={<ChartTooltipContent />} />
        </BarChart>
      </ChartContainer>,
    );

    expect(withClass(screen, "recharts-tooltip-wrapper").length).toBe(1);
    expect(tooltipOf(screen)).toBeUndefined();
  });

  it("names a slice of a pie by the row of data it comes from", () => {
    // The series of a pie is "visitors" for every slice: what tells them apart
    // is the browser written in each row.
    const screen = render(
      <ChartContainer config={config}>
        <PieChart>
          <Pie
            data={browsers}
            dataKey="visitors"
            nameKey="browser"
            isAnimationActive={false}
          />
          <ChartTooltip
            defaultIndex={1}
            content={<ChartTooltipContent hideLabel nameKey="browser" />}
          />
          <ChartLegend content={<ChartLegendContent nameKey="browser" />} />
        </PieChart>
      </ChartContainer>,
    );

    const tooltip = tooltipOf(screen) as TestElement;
    expect(textOf(tooltip)).toBe("Safari 200");
    // The color of the slice, not the grey Recharts gives a pie as a whole.
    expect(indicatorOf(rowsOf(tooltip)[0])?.style["--color-bg"]).toBe(
      "var(--color-safari)",
    );
    const legend = legendOf(screen) as TestElement;
    expect(legend.children.map(textOf)).toEqual(["Chrome", "Safari"]);
  });

  it("falls back on the label of the series when the axis gives no text for the point", () => {
    // No axis names the points of this line: Recharts gives their rank, a number.
    const screen = render(
      <ChartContainer config={config}>
        <LineChart data={[{ chrome: 4 }, { chrome: 5 }]}>
          <Line
            dataKey="chrome"
            stroke="var(--color-chrome)"
            isAnimationActive={false}
          />
          <ChartTooltip
            defaultIndex={1}
            content={<ChartTooltipContent indicator="line" />}
          />
          <ChartLegend verticalAlign="top" content={<ChartLegendContent />} />
        </LineChart>
      </ChartContainer>,
    );

    const tooltip = tooltipOf(screen) as TestElement;
    // Once as the label of the tooltip, once as the name of the row.
    expect(textOf(tooltip)).toBe("Chrome Chrome 5");
    expect(indicatorOf(rowsOf(tooltip)[0])?.style["--color-bg"]).toBe(
      "var(--color-chrome)",
    );
    // Above the chart: the space is below the legend.
    const legend = legendOf(screen) as TestElement;
    expect(classes(legend)).toContain("pb-3");
    expect(classes(legend)).not.toContain("pt-3");
  });

  it("redraws the chart at the size its container takes", async () => {
    const sizes = installResizeObserver();
    const screen = render(<Bars />);
    const surface = () => withClass(screen, "recharts-surface")[0];
    expect(surface().getAttribute("width")).toBe("600");

    await settle(() => sizes.resize(900, 400));

    expect(surface().getAttribute("width")).toBe("900");
    expect(surface().getAttribute("height")).toBe("400");
  });
});

describe("ANH-203 chart: the label of the tooltip", () => {
  it("draws nothing for an inactive point, or an active one without entries", () => {
    expect(
      inChart(<ChartTooltipContent payload={[desktop]} />).drawn,
    ).toBeUndefined();
    expect(
      inChart(<ChartTooltipContent active payload={[]} />).drawn,
    ).toBeUndefined();
    expect(inChart(<ChartTooltipContent active />).drawn).toBeUndefined();
  });

  it("shows the text of the axis as it is when it names no series", () => {
    const { drawn } = inChart(
      <ChartTooltipContent
        active
        payload={[desktop, mobile]}
        label="February"
      />,
    );

    expect(textOf(drawn as TestElement)).toBe(
      "February Desktop 305 Mobile 200",
    );
  });

  it("shows the label of the series when the text of the axis is the name of one", () => {
    const { drawn } = inChart(
      <ChartTooltipContent active payload={[desktop, mobile]} label="mobile" />,
    );

    expect(textOf((drawn as TestElement).children[0])).toBe("Mobile");
  });

  it("shows no label when asked to hide it", () => {
    const { drawn } = inChart(
      <ChartTooltipContent
        active
        hideLabel
        payload={[desktop, mobile]}
        label="February"
      />,
    );

    expect(textOf(drawn as TestElement)).toBe("Desktop 305 Mobile 200");
    expect((drawn as TestElement).children.length).toBe(1);
  });

  it("takes the label from the entry when told which field holds it", () => {
    const slice: TooltipEntry = {
      dataKey: "visitors",
      name: "visitors",
      value: 200,
      payload: browsers[1],
    };

    const { drawn } = inChart(
      <ChartTooltipContent
        active
        payload={[slice]}
        label="February"
        labelKey="browser"
      />,
    );

    // The row of data says "safari": the label is the one of that series.
    expect(textOf((drawn as TestElement).children[0])).toBe("Safari");
  });

  it("takes the label from the field itself when it names a series directly", () => {
    const { drawn } = inChart(
      <ChartTooltipContent active payload={[desktop]} labelKey="visitors" />,
    );

    expect(textOf((drawn as TestElement).children[0])).toBe("Visitors");
  });

  it("shows no label when nothing gives one", () => {
    const unknown: TooltipEntry = { dataKey: "tablet", value: 12, payload: {} };

    const { drawn } = inChart(
      <ChartTooltipContent active payload={[unknown]} />,
    );

    expect((drawn as TestElement).children.length).toBe(1);
    expect(textOf(drawn as TestElement)).toBe("12");
  });

  it("names an entry without a key after the series called value", () => {
    // The rows are keyed by data key and this entry has none: React says so
    // on the console, once, which this test keeps out of the report.
    vi.spyOn(console, "error").mockImplementation(() => {});

    const { drawn } = inChart(
      <ChartTooltipContent active payload={[{ value: 12, payload: {} }]} />,
      { value: { label: "Load" } },
    );
    vi.restoreAllMocks();

    // The label of the tooltip, then the name of its only row.
    expect(textOf(drawn as TestElement)).toBe("Load Load 12");
  });

  it("hands the label and the entries to the formatter given, and draws what it answers", () => {
    const labelFormatter = vi.fn(
      (label: unknown) => `Month of ${String(label)}`,
    );
    const entries = [desktop, mobile];

    const { drawn } = inChart(
      <ChartTooltipContent
        active
        payload={entries}
        label="mobile"
        labelFormatter={labelFormatter}
        labelClassName="uppercase"
      />,
    );

    // The formatter receives the label already resolved, not the raw key.
    expect(labelFormatter).toHaveBeenCalledWith("Mobile", entries);
    const label = (drawn as TestElement).children[0];
    expect(textOf(label)).toBe("Month of Mobile");
    expect(classes(label)).toContain("uppercase");
  });

  it("asks the formatter even when there is no label to format", () => {
    const labelFormatter = vi.fn(() => "No month");
    const unknown: TooltipEntry = { dataKey: "tablet", value: 12, payload: {} };

    const { drawn } = inChart(
      <ChartTooltipContent
        active
        payload={[unknown]}
        labelFormatter={labelFormatter}
      />,
    );

    expect(labelFormatter).toHaveBeenCalledWith(undefined, [unknown]);
    expect(textOf((drawn as TestElement).children[0])).toBe("No month");
  });

  it("keeps the classes given to the label and to the tooltip", () => {
    const { drawn } = inChart(
      <ChartTooltipContent
        active
        payload={[desktop, mobile]}
        label="February"
        className="w-40"
        labelClassName="uppercase"
      />,
    );

    expect(classes(drawn as TestElement)).toContain("w-40");
    expect(classes((drawn as TestElement).children[0])).toContain("uppercase");
  });
});

describe("ANH-203 chart: the rows of the tooltip", () => {
  it("leaves out the entries Recharts marks as not to be shown", () => {
    const hidden: TooltipEntry = { ...mobile, type: "none" };

    const { drawn } = inChart(
      <ChartTooltipContent active hideLabel payload={[desktop, hidden]} />,
    );

    expect(rowsOf(drawn as TestElement).map(textOf)).toEqual(["Desktop 305"]);
  });

  it("writes a value the way the reader's language groups digits", () => {
    const { drawn } = inChart(
      <ChartTooltipContent
        active
        hideLabel
        payload={[{ ...desktop, value: 1234567 }]}
      />,
    );

    expect(textOf(drawn as TestElement)).toBe(
      `Desktop ${(1234567).toLocaleString()}`,
    );
  });

  it("writes no value for an entry that has none", () => {
    const { drawn } = inChart(
      <ChartTooltipContent
        active
        hideLabel
        payload={[{ ...desktop, value: undefined }]}
      />,
    );

    expect(textOf(drawn as TestElement)).toBe("Desktop");
  });

  it("names a row by its raw name when the configuration has no label for it", () => {
    const tablet: TooltipEntry = {
      dataKey: "tablet",
      name: "tablet",
      value: 12,
      payload: {},
    };

    const { drawn } = inChart(
      <ChartTooltipContent active hideLabel payload={[tablet]} />,
    );

    expect(textOf(drawn as TestElement)).toBe("tablet 12");
  });

  it("names a row by the series its name points at, when that name is a field", () => {
    // "name" holds "mobile" in the entry: the row is the one of that series.
    const renamed: TooltipEntry = { ...desktop, name: "mobile" };

    const { drawn } = inChart(
      <ChartTooltipContent
        active
        hideLabel
        nameKey="name"
        payload={[renamed]}
      />,
    );

    expect(textOf(drawn as TestElement)).toBe("Mobile 305");
  });

  it.each([
    ["dot", ["h-2.5", "w-2.5"], ["w-1", "border-dashed"]],
    ["line", ["w-1"], ["h-2.5", "border-dashed"]],
    ["dashed", ["w-0", "border-dashed"], ["h-2.5", "w-1"]],
  ] as const)(
    "draws a %s as the mark of each row when asked",
    (indicator, has, hasNot) => {
      const { drawn } = inChart(
        <ChartTooltipContent
          active
          indicator={indicator}
          payload={[desktop, mobile]}
          label="February"
        />,
      );

      const marks = rowsOf(drawn as TestElement).map(
        indicatorOf,
      ) as TestElement[];
      expect(marks.length).toBe(2);
      for (const mark of marks) {
        for (const name of has) expect(classes(mark)).toContain(name);
        for (const name of hasNot) expect(classes(mark)).not.toContain(name);
      }
      // Two rows: the label stays above them, whatever the mark.
      expect((drawn as TestElement).children.length).toBe(2);
      expect(textOf((drawn as TestElement).children[0])).toBe("February");
    },
  );

  it("draws a dot unless told otherwise, and keeps the label above a single row", () => {
    const { drawn } = inChart(
      <ChartTooltipContent active payload={[desktop]} label="February" />,
    );

    const [row] = rowsOf(drawn as TestElement);
    expect(classes(indicatorOf(row) as TestElement)).toContain("h-2.5");
    expect(classes(row)).toContain("items-center");
    expect((drawn as TestElement).children.length).toBe(2);
    expect(textOf(drawn as TestElement)).toBe("February Desktop 305");
  });

  it.each(["line", "dashed"] as const)(
    "moves the label into the row when a single series is marked with a %s",
    (indicator) => {
      const { drawn } = inChart(
        <ChartTooltipContent
          active
          indicator={indicator}
          payload={[desktop]}
          label="February"
        />,
      );

      // The tooltip holds the list of rows only: the label is inside the row.
      const tooltip = drawn as TestElement;
      expect(tooltip.children.length).toBe(1);
      const [row] = rowsOf(tooltip);
      expect(textOf(row)).toBe("February Desktop 305");
      expect(classes(row)).not.toContain("items-center");
      // A dashed mark beside two lines of text is given room above and below.
      const mark = indicatorOf(row) as TestElement;
      expect(classes(mark).includes("my-0.5")).toBe(indicator === "dashed");
    },
  );

  it("draws no mark when asked to hide it", () => {
    const { drawn } = inChart(
      <ChartTooltipContent
        active
        hideIndicator
        hideLabel
        payload={[desktop, mobile]}
      />,
    );

    const rows = rowsOf(drawn as TestElement);
    expect(rows.map(indicatorOf)).toEqual([undefined, undefined]);
    expect(rows.map(textOf)).toEqual(["Desktop 305", "Mobile 200"]);
  });

  it("colors every mark with the color given, before the one of the series", () => {
    const { drawn } = inChart(
      <ChartTooltipContent
        active
        hideLabel
        color="#dc2626"
        payload={[desktop, mobile]}
      />,
    );

    expect(
      rowsOf(drawn as TestElement).map(
        (row) => indicatorOf(row)?.style["--color-bg"],
      ),
    ).toEqual(["#dc2626", "#dc2626"]);
  });

  it("colors a mark with the fill of the row of data, then with the color of the series", () => {
    const slice: TooltipEntry = {
      dataKey: "visitors",
      name: "visitors",
      color: "#808080",
      value: 200,
      payload: browsers[1],
    };
    const line: TooltipEntry = {
      dataKey: "chrome",
      name: "chrome",
      color: "var(--color-chrome)",
      value: 5,
      payload: { chrome: 5 },
    };

    const { drawn } = inChart(
      <ChartTooltipContent active hideLabel payload={[slice, line]} />,
    );

    expect(
      rowsOf(drawn as TestElement).map(
        (row) => indicatorOf(row)?.style["--color-bg"],
      ),
    ).toEqual(["var(--color-safari)", "var(--color-chrome)"]);
  });

  it("draws the icon of the series in place of the mark", () => {
    const { drawn } = inChart(
      <ChartTooltipContent active hideLabel payload={[desktop, mobile]} />,
      { ...config, desktop: { label: "Desktop", icon: Flag } },
    );

    const [withIcon, withMark] = rowsOf(drawn as TestElement);
    expect(withIcon.children[0].getAttribute("data-icon")).toBe("flag");
    expect(indicatorOf(withIcon)).toBeUndefined();
    expect(indicatorOf(withMark)?.style["--color-bg"]).toBe(
      "var(--color-mobile)",
    );
  });

  it("hands each entry to the formatter given, and draws what it answers in place of the row", () => {
    const formatter = vi.fn((value: unknown, name: unknown) => (
      <span>{`${String(name)}: ${String(value)} bpm`}</span>
    ));

    const { drawn } = inChart(
      <ChartTooltipContent
        active
        hideLabel
        payload={[desktop, mobile]}
        formatter={formatter}
      />,
    );

    expect(formatter.mock.calls).toEqual([
      [305, "desktop", desktop, 0, february],
      [200, "mobile", mobile, 1, february],
    ]);
    const rows = rowsOf(drawn as TestElement);
    expect(rows.map(textOf)).toEqual(["desktop: 305 bpm", "mobile: 200 bpm"]);
    expect(rows.map(indicatorOf)).toEqual([undefined, undefined]);
  });

  it("draws a row itself when it has no value or no name to hand to the formatter", () => {
    const formatter = vi.fn(() => "formatted");
    const noValue: TooltipEntry = { ...desktop, value: undefined };
    const noName: TooltipEntry = { ...mobile, name: undefined };

    const { drawn } = inChart(
      <ChartTooltipContent
        active
        hideLabel
        payload={[noValue, noName]}
        formatter={formatter}
      />,
    );

    expect(formatter).not.toHaveBeenCalled();
    expect(rowsOf(drawn as TestElement).map(textOf)).toEqual([
      "Desktop",
      "Mobile 200",
    ]);
  });
});

describe("ANH-203 chart: the legend", () => {
  /** The entries of the bar chart above, as Recharts gives them to a legend. */
  const desktopEntry: LegendEntry = {
    dataKey: "desktop",
    value: "desktop",
    color: "var(--color-desktop)",
    type: "rect",
  };
  const mobileEntry: LegendEntry = {
    dataKey: "mobile",
    value: "mobile",
    color: "var(--color-mobile)",
    type: "rect",
  };

  it("draws nothing without entries", () => {
    expect(inChart(<ChartLegendContent payload={[]} />).drawn).toBeUndefined();
    expect(inChart(<ChartLegendContent />).drawn).toBeUndefined();
  });

  it("leaves out the series Recharts marks as absent from the legend", () => {
    const { drawn } = inChart(
      <ChartLegendContent
        className="flex-wrap"
        payload={[desktopEntry, { ...mobileEntry, type: "none" }]}
      />,
    );

    expect((drawn as TestElement).children.map(textOf)).toEqual(["Desktop"]);
    expect(classes(drawn as TestElement)).toContain("flex-wrap");
  });

  it("draws the icon of a series in place of its square, unless asked to hide icons", () => {
    const withIcons = { ...config, desktop: { label: "Desktop", icon: Flag } };

    const shown = inChart(
      <ChartLegendContent payload={[desktopEntry, mobileEntry]} />,
      withIcons,
    ).drawn as TestElement;
    const [iconEntry, squareEntry] = shown.children;
    expect(iconEntry.children[0].getAttribute("data-icon")).toBe("flag");
    expect(squareEntry.children[0].style.backgroundColor).toBe(
      "var(--color-mobile)",
    );

    const hidden = inChart(
      <ChartLegendContent hideIcon payload={[desktopEntry, mobileEntry]} />,
      withIcons,
    ).drawn as TestElement;
    expect(
      hidden.children.map((entry) => entry.children[0].style.backgroundColor),
    ).toEqual(["var(--color-desktop)", "var(--color-mobile)"]);
    expect(hidden.children.map(textOf)).toEqual(["Desktop", "Mobile"]);
  });

  it("names an entry by the series its value points at, when told to read that field", () => {
    // What the legend of a pie holds: the browser as value, and no data key.
    const slices: LegendEntry[] = [
      { value: "chrome", color: "var(--color-chrome)" },
      { value: "safari", color: "var(--color-safari)" },
    ];

    const { drawn } = inChart(
      <ChartLegendContent nameKey="value" payload={slices} />,
    );

    expect((drawn as TestElement).children.map(textOf)).toEqual([
      "Chrome",
      "Safari",
    ]);
  });

  it("names an entry without a key after the series called value, and leaves an unknown one bare", () => {
    const { drawn } = inChart(
      <ChartLegendContent
        payload={[
          { value: "total", color: "#111827" },
          { value: "tablet", dataKey: "tablet", color: "#6b7280" },
        ]}
      />,
      { value: { label: "Load" } },
    );

    const [known, unknown] = (drawn as TestElement).children;
    expect(textOf(known)).toBe("Load");
    // Still a square of its color, with no text beside it.
    expect(textOf(unknown)).toBe("");
    expect(unknown.children[0].style.backgroundColor).toBe("#6b7280");
  });
});

describe("ANH-203 chart: outside a container", () => {
  it("refuses to draw a tooltip or a legend that no container configures", () => {
    const message = "useChart must be used within a <ChartContainer />";

    expect(() =>
      renderToStaticMarkup(<ChartTooltipContent active payload={[desktop]} />),
    ).toThrow(message);
    expect(() =>
      renderToStaticMarkup(
        <ChartLegendContent
          payload={[{ value: "desktop", dataKey: "desktop" }]}
        />,
      ),
    ).toThrow(message);
  });
});

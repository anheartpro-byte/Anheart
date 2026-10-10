import { describe, expect, it } from "vitest";
import { draw } from "@/test-support/markup";
import { Alert, AlertDescription, AlertTitle } from "./alert";

/**
 * The alert box: announced to a screen reader as an alert, painted as a plain
 * notice or as an error, with a title and a description placed beside its
 * icon.
 */

describe("ANH-203 alert", () => {
  it("is a box a screen reader announces as an alert", () => {
    const page = draw(<Alert>Machine hors ligne</Alert>);

    const alert = page.slot("alert");
    expect(alert.localName).toBe("div");
    expect(alert.getAttribute("role")).toBe("alert");
    expect(alert.text).toBe("Machine hors ligne");
  });

  it("is a plain notice unless told otherwise", () => {
    const page = draw(<Alert />);

    expect(page.slot("alert").classes).toContain("text-card-foreground");
    expect(page.slot("alert").classes).not.toContain("text-destructive");
  });

  it("is painted as an error when destructive", () => {
    const page = draw(<Alert variant="destructive" />);

    expect(page.slot("alert").classes).toContain("text-destructive");
    expect(page.slot("alert").classes).not.toContain("text-card-foreground");
  });

  it("lets the caller's class replace the one of the variant", () => {
    const page = draw(
      <Alert variant="destructive" className="text-amber-600" />,
    );

    expect(page.slot("alert").classes).toContain("text-amber-600");
    expect(page.slot("alert").classes).not.toContain("text-destructive");
  });

  it("lets the caller choose a quieter role", () => {
    const page = draw(<Alert role="status" id="notice" />);

    expect(page.slot("alert").getAttribute("role")).toBe("status");
    expect(page.slot("alert").getAttribute("id")).toBe("notice");
  });
});

describe("ANH-203 alert title and description", () => {
  it("places the title and the description in the column beside the icon", () => {
    const page = draw(
      <Alert>
        <AlertTitle>Arrêt demandé</AlertTitle>
        <AlertDescription>La machine ralentit.</AlertDescription>
      </Alert>,
    );

    const title = page.slot("alert-title");
    const description = page.slot("alert-description");
    expect(title.text).toBe("Arrêt demandé");
    expect(title.classes).toContain("col-start-2");
    expect(description.text).toBe("La machine ralentit.");
    expect(description.classes).toContain("col-start-2");
    expect(page.slot("alert").children).toEqual([title, description]);
  });

  it("lets the caller's class replace the one of the title", () => {
    const page = draw(<AlertTitle className="font-bold" id="t" />);

    expect(page.slot("alert-title").classes).toContain("font-bold");
    expect(page.slot("alert-title").classes).not.toContain("font-medium");
    expect(page.slot("alert-title").getAttribute("id")).toBe("t");
  });

  it("lets the caller's class replace the one of the description", () => {
    const page = draw(<AlertDescription className="text-base" id="d" />);

    expect(page.slot("alert-description").classes).toContain("text-base");
    expect(page.slot("alert-description").classes).not.toContain("text-sm");
    expect(page.slot("alert-description").getAttribute("id")).toBe("d");
  });
});

import { render, type Screen } from "@/test-support/render";
import { afterEach, describe, expect, it } from "vitest";
import { Avatar, AvatarFallback, AvatarImage } from "./avatar";

/**
 * The avatar, on the real Radix primitives: the picture of a person in a
 * round frame, and their initials while there is no picture to show.
 *
 * Radix asks the browser to load the picture before it draws it. No browser
 * here: the test stands in for the browser's `Image` and says whether the
 * picture is loaded.
 */

const globals = globalThis as Record<string, unknown>;

/** The browser's `Image` as Radix reads it: a picture loaded at once, or one that never comes. */
function theBrowser(answer: "has the picture" | "never gets the picture") {
  const loaded = answer === "has the picture";
  globals.Image = class {
    src = "";
    complete = loaded;
    naturalWidth = loaded ? 96 : 0;
    addEventListener() {
      // Nothing to hear: the picture is loaded already, or never will be.
    }
    removeEventListener() {
      // Nothing was registered.
    }
  };
}

afterEach(() => {
  delete globals.Image;
});

function slots(screen: Screen, name: string) {
  return screen.all((element) => element.getAttribute("data-slot") === name);
}

function classes(screen: Screen, name: string) {
  return slots(screen, name)[0].className.split(" ");
}

function person(props: { className?: string } = {}) {
  return render(
    <Avatar {...props}>
      <AvatarImage src="/ada.png" alt="Ada Lovelace" {...props} />
      <AvatarFallback {...props}>AL</AvatarFallback>
    </Avatar>,
  );
}

describe("ANH-203 avatar", () => {
  it("shows the picture once the browser has it, and no initials", () => {
    theBrowser("has the picture");
    const screen = person();

    const [frame] = slots(screen, "avatar");
    const [picture] = slots(screen, "avatar-image");
    expect(picture.localName).toBe("img");
    expect(picture.getAttribute("src")).toBe("/ada.png");
    expect(picture.getAttribute("alt")).toBe("Ada Lovelace");
    expect(frame.children).toEqual([picture]);
    expect(slots(screen, "avatar-fallback")).toEqual([]);
  });

  it("shows the initials while the picture has not come", () => {
    theBrowser("never gets the picture");
    const screen = person();

    const [frame] = slots(screen, "avatar");
    const [initials] = slots(screen, "avatar-fallback");
    expect(screen.textOf(initials)).toBe("AL");
    expect(frame.children).toEqual([initials]);
    expect(slots(screen, "avatar-image")).toEqual([]);
  });

  it("frames the picture in a circle that cuts what goes past it", () => {
    theBrowser("has the picture");
    const screen = person();

    expect(classes(screen, "avatar")).toContain("rounded-full");
    expect(classes(screen, "avatar")).toContain("overflow-hidden");
    // The picture fills the frame as a square.
    expect(classes(screen, "avatar-image")).toContain("aspect-square");
    expect(classes(screen, "avatar-image")).toContain("size-full");
  });

  it("lets the caller's class replace the size of the frame and of the picture", () => {
    theBrowser("has the picture");
    const screen = person({ className: "size-12" });

    expect(classes(screen, "avatar")).toContain("size-12");
    expect(classes(screen, "avatar")).not.toContain("size-8");
    expect(classes(screen, "avatar-image")).toContain("size-12");
    expect(classes(screen, "avatar-image")).not.toContain("size-full");
  });

  it("lets the caller's class replace the paint of the initials", () => {
    theBrowser("never gets the picture");
    const screen = render(
      <Avatar id="rider">
        <AvatarFallback className="bg-primary">AL</AvatarFallback>
      </Avatar>,
    );

    expect(classes(screen, "avatar-fallback")).toContain("bg-primary");
    expect(classes(screen, "avatar-fallback")).not.toContain("bg-muted");
    expect(slots(screen, "avatar")[0].getAttribute("id")).toBe("rider");
  });
});

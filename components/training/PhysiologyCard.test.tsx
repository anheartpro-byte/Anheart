import {
  buttonsOf,
  click,
  messages,
  render,
  submit,
  type,
} from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mutation, mutationCalls, resetConvex } from "@/test-support/convex";
import type { Id } from "@/convex/_generated/dataModel";
import { dismissFeedback, getFeedbackToasts } from "@/lib/feedback";
import { PhysiologyCard } from "./PhysiologyCard";

/**
 * The card where a manager sets the max heart rate and the birth year of a
 * rider: what it shows of what the server holds, what it sends when saved,
 * what it refuses to send.
 */

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/convex")).convexReact,
);

const fr = messages.fr;
const t = fr.training.physiology;
const userId = "user-1" as Id<"users">;
const save = "training:setUserPhysiology";

/** The messages shown after a mutation, as the site would show them. */
function shown() {
  return getFeedbackToasts().map(({ kind, message }) => ({ kind, message }));
}

beforeEach(() => {
  vi.useFakeTimers();
  // The middle of 2026: the age of a rider born in 1986 is 40.
  vi.setSystemTime(new Date("2026-07-01T12:00:00Z"));
  resetConvex();
});
afterEach(() => {
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
  vi.useRealTimers();
});

describe("ANH-203 physiology card: what it shows", () => {
  it("is a form whose only button submits it", () => {
    // The tests below submit the form themselves: in a browser it is this
    // button, or Enter in a field, that does. Its type is what makes it so.
    const screen = render(<PhysiologyCard userId={userId} user={{}} />);

    expect(buttonsOf(screen.form())).toEqual([
      { label: fr.common.save, type: "submit" },
    ]);
  });

  it("shows the measured max heart rate the server holds, and says it is measured", () => {
    const screen = render(
      <PhysiologyCard userId={userId} user={{ hrMax: 185, birthYear: 1986 }} />,
    );

    expect(screen.field("phys-hrmax").value).toBe("185");
    expect(screen.field("phys-birthyear").value).toBe("1986");
    expect(screen.text()).toContain(`${t.effective} 185 bpm (${t.measured})`);
  });

  it("estimates the max heart rate from the birth year when none was measured", () => {
    const screen = render(
      <PhysiologyCard userId={userId} user={{ birthYear: 1986 }} />,
    );

    // 208 - 0.7 x 40
    expect(screen.field("phys-hrmax").value).toBe("");
    expect(screen.text()).toContain(`${t.effective} 180 bpm (${t.estimated})`);
  });

  it("says the max heart rate is not set when the server holds neither value", () => {
    const screen = render(<PhysiologyCard userId={userId} user={{}} />);

    expect(screen.text()).toContain(`${t.effective} ${t.notSet}`);
    expect(screen.text()).not.toContain("bpm (");
  });

  it("cannot be saved before a field is touched", async () => {
    const screen = render(
      <PhysiologyCard userId={userId} user={{ hrMax: 185 }} />,
    );

    expect(screen.button(fr.common.save).hasAttribute("disabled")).toBe(true);
    // Enter in a field submits the form whatever the button says.
    await submit(screen.form());
    expect(mutationCalls()).toEqual({});
  });

  it("previews the value being typed, before anything is saved", async () => {
    const screen = render(
      <PhysiologyCard userId={userId} user={{ hrMax: 185, birthYear: 1986 }} />,
    );

    await type(screen.field("phys-hrmax"), "");

    // The measured value is erased in the form: the estimate takes over.
    expect(screen.text()).toContain(`${t.effective} 180 bpm (${t.estimated})`);
    expect(mutationCalls()).toEqual({});
  });
});

describe("ANH-203 physiology card: what it sends", () => {
  it("sends only the field the manager touched", async () => {
    const screen = render(
      <PhysiologyCard userId={userId} user={{ hrMax: 185, birthYear: 1986 }} />,
    );

    await type(screen.field("phys-hrmax"), "190");
    await click(screen.button(fr.common.save));
    await submit(screen.form());

    // The birth year, untouched, is not in the call: it cannot be overwritten.
    expect(mutationCalls()).toEqual({ [save]: [[{ userId, hrMax: 190 }]] });
    expect(shown()).toEqual([
      { kind: "success", message: fr.feedback.physiologySaved },
    ]);
    expect(screen.hasButton(t.saved)).toBe(true);
    expect(screen.button(t.saved).hasAttribute("disabled")).toBe(true);
  });

  it("sends null for a field the manager emptied, to erase it", async () => {
    const screen = render(
      <PhysiologyCard userId={userId} user={{ hrMax: 185, birthYear: 1986 }} />,
    );

    await type(screen.field("phys-hrmax"), "  ");
    await type(screen.field("phys-birthyear"), "1990");
    await submit(screen.form());

    expect(mutationCalls()).toEqual({
      [save]: [[{ userId, hrMax: null, birthYear: 1990 }]],
    });
  });

  it('goes back to "save" as soon as a field changes after a save', async () => {
    const screen = render(<PhysiologyCard userId={userId} user={{}} />);
    await type(screen.field("phys-birthyear"), "1986");
    await submit(screen.form());
    expect(screen.hasButton(t.saved)).toBe(true);

    await type(screen.field("phys-birthyear"), "1987");

    expect(screen.hasButton(t.saved)).toBe(false);
    expect(screen.button(fr.common.save).hasAttribute("disabled")).toBe(false);
  });

  it("shows the server's refusal in the card and keeps the form as typed", async () => {
    mutation(save).mockRejectedValue(new Error("offline"));
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const screen = render(<PhysiologyCard userId={userId} user={{}} />);

    await type(screen.field("phys-hrmax"), "190");
    await submit(screen.form());

    expect(screen.text()).toContain("offline");
    expect(shown()).toEqual([{ kind: "error", message: "offline" }]);
    expect(screen.hasButton(t.saved)).toBe(false);
    // Still dirty: the manager can try again without typing again.
    expect(screen.button(fr.common.save).hasAttribute("disabled")).toBe(false);
    logged.mockRestore();
  });
});

describe("ANH-203 physiology card: what it refuses", () => {
  it.each(["99", "221", "180.5", "abc"])(
    "refuses a max heart rate of %s: message shown, nothing sent",
    async (value) => {
      const screen = render(<PhysiologyCard userId={userId} user={{}} />);

      await type(screen.field("phys-hrmax"), value);
      await submit(screen.form());

      expect(screen.text()).toContain(t.invalidHrMax);
      expect(screen.button(fr.common.save).hasAttribute("disabled")).toBe(true);
      expect(mutationCalls()).toEqual({});
    },
  );

  it.each(["100", "220"])("accepts a max heart rate of %s", async (value) => {
    const screen = render(<PhysiologyCard userId={userId} user={{}} />);

    await type(screen.field("phys-hrmax"), value);

    expect(screen.text()).not.toContain(t.invalidHrMax);
    expect(screen.button(fr.common.save).hasAttribute("disabled")).toBe(false);
  });

  it.each([
    ["2017", "a rider of 9"],
    ["1925", "a rider of 101"],
    ["1986.5", "half a year"],
  ])("refuses the birth year %s (%s)", async (value) => {
    const screen = render(<PhysiologyCard userId={userId} user={{}} />);

    await type(screen.field("phys-birthyear"), value);
    await submit(screen.form());

    expect(screen.text()).toContain(t.invalidBirthYear);
    expect(mutationCalls()).toEqual({});
  });

  it.each(["2016", "1926"])("accepts the birth year %s", async (value) => {
    const screen = render(<PhysiologyCard userId={userId} user={{}} />);

    await type(screen.field("phys-birthyear"), value);

    expect(screen.text()).not.toContain(t.invalidBirthYear);
    expect(screen.button(fr.common.save).hasAttribute("disabled")).toBe(false);
  });
});

import {
  click,
  messages,
  render,
  submit,
  type,
  type Screen,
} from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mutation, mutationCalls, resetConvex } from "@/test-support/convex";
import { choose, chosen, closeWindow, windowOpen } from "@/test-support/ui";
import type { Id } from "@/convex/_generated/dataModel";
import { dismissFeedback, getFeedbackToasts } from "@/lib/feedback";
import { PatientFormModal } from "./PatientFormModal";

/**
 * The window where a gestionnaire creates or edits a patient: what it sends
 * for each of the two, what it refuses to send, and what it shows when the
 * server refuses.
 */

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/convex")).convexReact,
);
vi.mock(
  "@/components/ui/dialog",
  async () => (await import("@/test-support/ui")).dialog,
);
vi.mock(
  "@/components/ui/select",
  async () => (await import("@/test-support/ui")).select,
);

const fr = messages.fr;
const t = fr.users;
const CREATE = "users:createPatient";
const UPDATE = "users:updatePatient";
const patientId = "patient-1" as Id<"users">;

const PAUL = {
  _id: patientId,
  firstName: "Paul",
  lastName: "Martin",
  email: "paul@anheart.test",
  language: "fr",
};

function open(props: Partial<Parameters<typeof PatientFormModal>[0]> = {}) {
  const onOpenChange = vi.fn();
  const onSuccess = vi.fn();
  const screen = render(
    <PatientFormModal
      open
      onOpenChange={onOpenChange}
      onSuccess={onSuccess}
      {...props}
    />,
  );
  return { screen, onOpenChange, onSuccess };
}

/** The field of the form with this name. */
const field = (screen: Screen, name: string) => {
  const [found] = screen.all(
    (element) => element.localName === "input" && element.name === name,
  );
  if (found === undefined) throw new Error(`No field "${name}"`);
  return found;
};

async function fill(screen: Screen, values: Record<string, string>) {
  for (const [name, value] of Object.entries(values)) {
    await type(field(screen, name), value);
  }
}

function shown() {
  return getFeedbackToasts().map(({ kind, message }) => ({ kind, message }));
}

beforeEach(() => {
  vi.useFakeTimers();
  resetConvex();
});
afterEach(() => {
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
  vi.useRealTimers();
});

describe("ANH-203 patient window: creating", () => {
  it("draws nothing while closed", () => {
    const { screen } = open({ open: false });

    expect(windowOpen(screen)).toBe(false);
  });

  it("opens on an empty form in French, and says an invitation will be sent", () => {
    const { screen } = open();

    expect(screen.text()).toContain(t.createPatient);
    expect(screen.text()).toContain(t.createPatientDescription);
    expect(screen.text()).toContain(t.patientInviteNote);
    expect(field(screen, "email").hasAttribute("disabled")).toBe(false);
    expect(chosen(screen)).toEqual(["Francais"]);
    expect(screen.hasButton(fr.common.create)).toBe(true);
  });

  it("creates the patient with the four values typed", async () => {
    const { screen, onOpenChange, onSuccess } = open();
    await fill(screen, {
      firstName: "Lina",
      lastName: "Roy",
      email: "lina@anheart.test",
    });
    await choose(screen, "English");

    await submit(screen.form());

    expect(mutationCalls()).toEqual({
      [CREATE]: [
        [
          {
            firstName: "Lina",
            lastName: "Roy",
            email: "lina@anheart.test",
            language: "en",
          },
        ],
      ],
    });
    expect(shown()).toEqual([
      { kind: "success", message: fr.feedback.patientCreated },
    ]);
    expect(onOpenChange.mock.calls).toEqual([[false]]);
    expect(onSuccess).toHaveBeenCalledTimes(1);
  });

  it.each([
    [
      "no first name",
      { lastName: "Roy", email: "lina@anheart.test" },
      "First name is required",
      "firstName",
    ],
    [
      "no last name",
      { firstName: "Lina", email: "lina@anheart.test" },
      "Last name is required",
      "lastName",
    ],
    [
      "no email",
      { firstName: "Lina", lastName: "Roy" },
      "Invalid email address",
      "email",
    ],
    [
      "an email without a domain",
      { firstName: "Lina", lastName: "Roy", email: "lina@" },
      "Invalid email address",
      "email",
    ],
  ])(
    "refuses a patient with %s: message under the field, nothing sent",
    async (_case, values, message, invalid) => {
      const { screen, onOpenChange } = open();
      await fill(screen, values);

      await submit(screen.form());

      expect(screen.text()).toContain(message);
      expect(field(screen, invalid).getAttribute("aria-invalid")).toBe("true");
      expect(mutationCalls()).toEqual({});
      expect(onOpenChange).not.toHaveBeenCalled();
    },
  );

  it("shows the server's refusal in the form, keeps the window open and what was typed", async () => {
    mutation(CREATE).mockRejectedValue(
      new Error("A user with this email already exists"),
    );
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const { screen, onOpenChange, onSuccess } = open();
    await fill(screen, {
      firstName: "Lina",
      lastName: "Roy",
      email: "lina@anheart.test",
    });

    await submit(screen.form());

    expect(screen.text()).toContain("A user with this email already exists");
    expect(shown()).toEqual([
      { kind: "error", message: "A user with this email already exists" },
    ]);
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(onSuccess).not.toHaveBeenCalled();
    expect(field(screen, "email").value).toBe("lina@anheart.test");
    logged.mockRestore();
  });

  it.each([
    ["Cancel", (screen: Screen) => click(screen.button(fr.common.cancel))],
    ["the window's own close control", (screen: Screen) => closeWindow(screen)],
  ])("on %s: closes and sends nothing", async (_how, leave) => {
    const { screen, onOpenChange, onSuccess } = open();
    await fill(screen, { firstName: "Lina" });

    await leave(screen);

    expect(onOpenChange.mock.calls).toEqual([[false]]);
    expect(onSuccess).not.toHaveBeenCalled();
    expect(mutationCalls()).toEqual({});
  });
});

describe("ANH-203 patient window: editing", () => {
  it("opens on the patient's values, with an email that cannot be changed and no invitation note", () => {
    const { screen } = open({ patient: PAUL });

    expect(screen.text()).toContain(t.editPatient);
    expect(screen.text()).toContain(t.editPatientDescription);
    expect(screen.text()).not.toContain(t.patientInviteNote);
    expect(field(screen, "firstName").value).toBe("Paul");
    expect(field(screen, "lastName").value).toBe("Martin");
    expect(field(screen, "email").value).toBe("paul@anheart.test");
    expect(field(screen, "email").hasAttribute("disabled")).toBe(true);
    expect(screen.hasButton(fr.common.save)).toBe(true);
  });

  it("updates the name and the language of this patient, and never sends the email", async () => {
    const { screen, onOpenChange, onSuccess } = open({ patient: PAUL });
    await fill(screen, { firstName: "Jean-Paul", lastName: "Martin-Roy" });
    await choose(screen, "English");

    await submit(screen.form());

    expect(mutationCalls()).toEqual({
      [UPDATE]: [
        [
          {
            userId: patientId,
            firstName: "Jean-Paul",
            lastName: "Martin-Roy",
            language: "en",
          },
        ],
      ],
    });
    expect(shown()).toEqual([
      { kind: "success", message: fr.feedback.patientUpdated },
    ]);
    expect(onOpenChange.mock.calls).toEqual([[false]]);
    expect(onSuccess).toHaveBeenCalledTimes(1);
  });

  it("saved untouched, keeps the patient's own language", async () => {
    const { screen } = open({ patient: { ...PAUL, language: "en" } });

    await submit(screen.form());

    expect(mutation(UPDATE).mock.calls).toEqual([
      [
        {
          userId: patientId,
          firstName: "Paul",
          lastName: "Martin",
          language: "en",
        },
      ],
    ]);
  });

  it("what the window does today: the list of languages is first drawn on French for an English-speaking patient", () => {
    // What is proven: the window hands the list its first choice ("fr", the
    // default of the form) one render before the patient's values reach the
    // form, and only as a first choice (`defaultValue`), never as the value.
    // The stand-in keeps a first choice as the real list is documented to.
    // Whether the screen of a browser then keeps showing French was not
    // checked here: no browser. What is saved is right either way (the test
    // above). Left as it is: reported, not corrected in this ticket.
    const { screen } = open({ patient: { ...PAUL, language: "en" } });

    expect(chosen(screen)).toEqual(["Francais"]);
  });

  it("refuses an emptied name and sends nothing", async () => {
    const { screen } = open({ patient: PAUL });
    await fill(screen, { lastName: "" });

    await submit(screen.form());

    expect(screen.text()).toContain("Last name is required");
    expect(mutationCalls()).toEqual({});
  });

  it("shows the server's refusal and keeps the window open", async () => {
    mutation(UPDATE).mockRejectedValue(
      new Error("You do not manage this user"),
    );
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const { screen, onOpenChange, onSuccess } = open({ patient: PAUL });

    await submit(screen.form());

    expect(screen.text()).toContain("You do not manage this user");
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(onSuccess).not.toHaveBeenCalled();
    logged.mockRestore();
  });

  it("opened again to create, after an edit, starts from an empty form", () => {
    const { screen } = open({ patient: PAUL });

    screen.rerender(<PatientFormModal open onOpenChange={() => {}} />);

    expect(field(screen, "firstName").value).toBe("");
    expect(field(screen, "email").value).toBe("");
    expect(field(screen, "email").hasAttribute("disabled")).toBe(false);
  });
});

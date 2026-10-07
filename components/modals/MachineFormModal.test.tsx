import {
  buttonsOf,
  click,
  messages,
  render,
  settle,
  submit,
  type,
  type Screen,
} from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  answer,
  mutation,
  mutationCalls,
  resetConvex,
} from "@/test-support/convex";
import { closeWindow, windowOpen } from "@/test-support/ui";
import type { Id } from "@/convex/_generated/dataModel";
import { dismissFeedback, getFeedbackToasts } from "@/lib/feedback";
import { MachineFormModal } from "./MachineFormModal";

/**
 * The window that creates or edits a machine: what it sends, what it refuses
 * to send, the API key shown once after a creation, and the list of
 * gestionnaires, which only an administrator may change.
 *
 * The rule that decides whether the list of gestionnaires is sent lives in
 * lib/machineForm.ts and has its own tests; what is proven here is that the
 * window applies it with the role of the person signed in, and never sends
 * the list for anyone else.
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
  "@/components/ui/checkbox",
  async () => (await import("@/test-support/ui")).checkbox,
);

const fr = messages.fr;
const t = fr.machines;
const CREATE = "machines:createMachine";
const UPDATE = "machines:updateMachine";
const ASSIGN = "machines:assignMachineToGestionnaires";

const machineId = "machine-1" as Id<"machines">;
const ANNA = {
  _id: "anna" as Id<"users">,
  firstName: "Anna",
  lastName: "Petit",
  email: "anna@anheart.test",
};
const BORIS = {
  _id: "boris" as Id<"users">,
  firstName: "Boris",
  lastName: "Noir",
  email: "boris@anheart.test",
};

/** The machine being edited: Anna is its only gestionnaire, and so its owner. */
const MACHINE = {
  _id: machineId,
  name: "Centri Paris",
  location: "Salle 2",
  gestionnaires: [{ ...ANNA, isOwner: true }],
};

function signedInAs(role: "admin" | "org_admin" | "gestionnaire") {
  answer("users:getCurrentUser", { _id: "me", role });
}

function open(props: Partial<Parameters<typeof MachineFormModal>[0]> = {}) {
  const onOpenChange = vi.fn();
  const onSuccess = vi.fn();
  const screen = render(
    <MachineFormModal
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

/** The check box of a gestionnaire. */
const box = (screen: Screen, gestionnaire: { _id: string }) =>
  screen.field(gestionnaire._id);

function shown() {
  return getFeedbackToasts().map(({ kind, message }) => ({ kind, message }));
}

beforeEach(() => {
  vi.useFakeTimers();
  resetConvex();
  mutation(CREATE).mockResolvedValue({
    machineId,
    apiKey: "cle-api-de-test",
  });
});
afterEach(() => {
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("ANH-203 machine window: the form", () => {
  it.each([
    ["creating", undefined, fr.common.create],
    ["editing", MACHINE, fr.common.save],
  ])(
    "%s: one button submits the form, « Annuler » and the boxes of the gestionnaires are plain buttons",
    (_case, machine, label) => {
      // The tests below submit the form themselves: in a browser it is the
      // submit button, or Enter in a field, that does.
      signedInAs("admin");
      answer("users:listGestionnaires", [ANNA, BORIS]);
      const { screen } = open({ machine });

      const buttons = buttonsOf(screen.form());
      expect(buttons.filter((button) => button.type !== "button")).toEqual([
        { label, type: "submit" },
      ]);
      // Two boxes (they read nothing), the submit button, then Cancel.
      expect(buttons.map((button) => button.label)).toEqual([
        "",
        "",
        label,
        fr.common.cancel,
      ]);
    },
  );
});

describe("ANH-203 machine window: creating", () => {
  it("draws nothing while closed", () => {
    const { screen } = open({ open: false });

    expect(windowOpen(screen)).toBe(false);
  });

  it("opens on an empty form that says it creates a machine", () => {
    signedInAs("admin");
    const { screen } = open();

    expect(screen.text()).toContain(t.create);
    expect(screen.text()).toContain(t.form.createDescription);
    expect(field(screen, "name").value).toBe("");
    expect(field(screen, "location").value).toBe("");
    expect(screen.hasButton(fr.common.create)).toBe(true);
    expect(screen.hasButton(fr.common.save)).toBe(false);
  });

  it("refuses a machine without a name: message under the field, nothing sent", async () => {
    signedInAs("admin");
    const { screen } = open();
    await type(field(screen, "location"), "Salle 2");

    await submit(screen.form());

    expect(screen.text()).toContain(t.form.nameRequired);
    expect(field(screen, "name").getAttribute("aria-invalid")).toBe("true");
    expect(mutationCalls()).toEqual({});
  });

  it("sends the name alone when the place is left empty and no gestionnaire is ticked", async () => {
    signedInAs("admin");
    const { screen } = open();
    await type(field(screen, "name"), "Centri Lyon");

    await submit(screen.form());

    expect(mutationCalls()).toEqual({
      [CREATE]: [
        [{ name: "Centri Lyon", location: undefined, gestionnaireIds: [] }],
      ],
    });
  });

  it("sends the place and the gestionnaires ticked, in the order they were ticked", async () => {
    signedInAs("admin");
    answer("users:listGestionnaires", [ANNA, BORIS]);
    const { screen } = open();
    expect(screen.text()).toContain("Anna Petit (anna@anheart.test)");
    await type(field(screen, "name"), "Centri Lyon");
    await type(field(screen, "location"), "Salle 3");

    // Boris first: the server makes the first of the list the owner.
    await click(box(screen, BORIS));
    await click(box(screen, ANNA));
    expect(box(screen, BORIS).getAttribute("aria-checked")).toBe("true");
    await submit(screen.form());

    expect(mutationCalls()).toEqual({
      [CREATE]: [
        [
          {
            name: "Centri Lyon",
            location: "Salle 3",
            gestionnaireIds: ["boris", "anna"],
          },
        ],
      ],
    });
  });

  it("a gestionnaire unticked before the creation is not sent", async () => {
    signedInAs("admin");
    answer("users:listGestionnaires", [ANNA, BORIS]);
    const { screen } = open();
    await type(field(screen, "name"), "Centri Lyon");
    await click(box(screen, ANNA));
    await click(box(screen, BORIS));

    await click(box(screen, ANNA));
    expect(box(screen, ANNA).getAttribute("aria-checked")).toBe("false");
    await submit(screen.form());

    expect(mutation(CREATE).mock.calls[0][0].gestionnaireIds).toEqual([
      "boris",
    ]);
  });

  it("offers no list of gestionnaires when the server gives none", () => {
    signedInAs("gestionnaire");
    answer("users:listGestionnaires", []);

    const { screen } = open();

    expect(screen.text()).not.toContain(t.assignGestionnaires);
    expect(
      screen.all((element) => element.getAttribute("role") === "checkbox"),
    ).toEqual([]);
  });

  it("shows the server's refusal in the form and creates nothing more", async () => {
    signedInAs("admin");
    mutation(CREATE).mockRejectedValue(new Error("Machine name already used"));
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const { screen, onOpenChange, onSuccess } = open();
    await type(field(screen, "name"), "Centri Paris");

    await submit(screen.form());

    expect(screen.text()).toContain("Machine name already used");
    expect(screen.text()).not.toContain(t.apiKey);
    expect(shown()).toEqual([
      { kind: "error", message: "Machine name already used" },
    ]);
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(onSuccess).not.toHaveBeenCalled();
    // The name typed is still there to correct.
    expect(field(screen, "name").value).toBe("Centri Paris");
    logged.mockRestore();
  });
});

describe("ANH-203 machine window: the API key, shown once after a creation", () => {
  async function created() {
    signedInAs("admin");
    const opened = open();
    await type(field(opened.screen, "name"), "Centri Lyon");
    await submit(opened.screen.form());
    return opened;
  }

  it("replaces the form with the key and its warning, and keeps the window open", async () => {
    const { screen, onOpenChange, onSuccess } = await created();

    expect(screen.text()).toContain("cle-api-de-test");
    expect(screen.text()).toContain(t.apiKeyWarning);
    expect(screen.tag("form")).toEqual([]);
    expect(shown()).toEqual([{ kind: "success", message: t.createSuccess }]);
    // Not closed yet, and the list behind is not refreshed before the key was read.
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it("copies the key to the clipboard and says so for two seconds", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const { screen } = await created();

    await click(screen.button(fr.common.copy));

    expect(writeText.mock.calls).toEqual([["cle-api-de-test"]]);
    expect(screen.hasButton(fr.common.copied)).toBe(true);
    await settle(() => vi.advanceTimersByTime(1_999));
    expect(screen.hasButton(fr.common.copied)).toBe(true);
    await settle(() => vi.advanceTimersByTime(1));
    expect(screen.hasButton(fr.common.copy)).toBe(true);
  });

  it.each([
    ["Close", (screen: Screen) => click(screen.button(fr.common.close))],
    ["the window's own close control", (screen: Screen) => closeWindow(screen)],
  ])(
    "on %s: closes, tells the page a machine was created, and forgets the key",
    async (_how, leave) => {
      const { screen, onOpenChange, onSuccess } = await created();

      await leave(screen);

      expect(onOpenChange.mock.calls).toEqual([[false]]);
      expect(onSuccess).toHaveBeenCalledTimes(1);
      // Still mounted by its page: the key is gone, the empty form is back.
      expect(screen.text()).not.toContain("cle-api-de-test");
      expect(field(screen, "name").value).toBe("");
    },
  );
});

describe("ANH-203 machine window: editing", () => {
  it("opens on the machine's name and place, and says it edits", () => {
    signedInAs("admin");
    answer("users:listGestionnaires", [ANNA, BORIS]);

    const { screen } = open({ machine: MACHINE });

    expect(screen.text()).toContain(t.form.editDescription);
    expect(field(screen, "name").value).toBe("Centri Paris");
    expect(field(screen, "location").value).toBe("Salle 2");
    expect(box(screen, ANNA).getAttribute("aria-checked")).toBe("true");
    expect(box(screen, BORIS).getAttribute("aria-checked")).toBe("false");
    expect(screen.hasButton(fr.common.save)).toBe(true);
  });

  it("a gestionnaire saves the name and the place: the list of gestionnaires is never sent", async () => {
    signedInAs("gestionnaire");
    // The list is drawn for him too if the server answers it: the rule must hold anyway.
    answer("users:listGestionnaires", [ANNA, BORIS]);
    const { screen, onOpenChange, onSuccess } = open({ machine: MACHINE });
    await type(field(screen, "name"), "Centri Paris Nord");
    await type(field(screen, "location"), "");
    await click(box(screen, BORIS));

    await submit(screen.form());

    expect(mutationCalls()).toEqual({
      [UPDATE]: [
        [{ machineId, name: "Centri Paris Nord", location: undefined }],
      ],
    });
    expect(shown()).toEqual([{ kind: "success", message: t.updateSuccess }]);
    expect(onOpenChange.mock.calls).toEqual([[false]]);
    expect(onSuccess).toHaveBeenCalledTimes(1);
  });

  it("an administrator who changes nothing in the list does not send it", async () => {
    signedInAs("admin");
    answer("users:listGestionnaires", [ANNA, BORIS]);
    const { screen } = open({ machine: MACHINE });

    await submit(screen.form());

    expect(Object.keys(mutationCalls())).toEqual([UPDATE]);
  });

  it("an administrator who ticks a gestionnaire sends the new list after the machine itself", async () => {
    signedInAs("admin");
    answer("users:listGestionnaires", [ANNA, BORIS]);
    const order: string[] = [];
    mutation(UPDATE).mockImplementation(async () => void order.push("update"));
    mutation(ASSIGN).mockImplementation(async () => void order.push("assign"));
    const { screen, onOpenChange } = open({ machine: MACHINE });
    await click(box(screen, BORIS));

    await submit(screen.form());

    expect(mutation(ASSIGN).mock.calls).toEqual([
      [{ machineId, gestionnaireIds: ["anna", "boris"] }],
    ]);
    expect(order).toEqual(["update", "assign"]);
    expect(onOpenChange.mock.calls).toEqual([[false]]);
  });

  it("when the server refuses the machine's own update, the list is not sent and the window stays", async () => {
    signedInAs("admin");
    answer("users:listGestionnaires", [ANNA, BORIS]);
    mutation(UPDATE).mockRejectedValue(
      new Error("Not authorized to edit this machine"),
    );
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const { screen, onOpenChange, onSuccess } = open({ machine: MACHINE });
    await click(box(screen, BORIS));

    await submit(screen.form());

    expect(screen.text()).toContain("Not authorized to edit this machine");
    expect(mutation(ASSIGN)).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(onSuccess).not.toHaveBeenCalled();
    logged.mockRestore();
  });

  it("when the server refuses the list, the refusal is shown and the window stays open", async () => {
    signedInAs("admin");
    answer("users:listGestionnaires", [ANNA, BORIS]);
    mutation(ASSIGN).mockRejectedValue(
      new Error("Only an admin may assign gestionnaires"),
    );
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const { screen, onOpenChange, onSuccess } = open({ machine: MACHINE });
    await click(box(screen, BORIS));

    await submit(screen.form());

    expect(screen.text()).toContain("Only an admin may assign gestionnaires");
    expect(shown()).toEqual([
      { kind: "success", message: t.updateSuccess },
      { kind: "error", message: "Only an admin may assign gestionnaires" },
    ]);
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(onSuccess).not.toHaveBeenCalled();
    logged.mockRestore();
  });

  it("someone whose role is not loaded yet never sends the list", async () => {
    answer("users:listGestionnaires", [ANNA, BORIS]);
    const { screen } = open({ machine: MACHINE });
    await click(box(screen, BORIS));

    await submit(screen.form());

    expect(Object.keys(mutationCalls())).toEqual([UPDATE]);
  });

  it("a machine without a place or a gestionnaire opens on an empty place and no box ticked", () => {
    signedInAs("admin");
    answer("users:listGestionnaires", [ANNA]);

    const { screen } = open({
      machine: { _id: machineId, name: "Centri Lyon" },
    });

    expect(field(screen, "location").value).toBe("");
    expect(box(screen, ANNA).getAttribute("aria-checked")).toBe("false");
  });

  it("an administrator gives a first gestionnaire to a machine that had none", async () => {
    signedInAs("admin");
    answer("users:listGestionnaires", [ANNA, BORIS]);
    const { screen } = open({
      machine: { _id: machineId, name: "Centri Lyon" },
    });
    await click(box(screen, BORIS));

    await submit(screen.form());

    expect(mutation(ASSIGN).mock.calls).toEqual([
      [{ machineId, gestionnaireIds: ["boris"] }],
    ]);
  });

  it("cancelling closes without sending and without telling the page anything changed", async () => {
    signedInAs("admin");
    const { screen, onOpenChange, onSuccess } = open({ machine: MACHINE });
    await type(field(screen, "name"), "Autre nom");

    await click(screen.button(fr.common.cancel));

    expect(onOpenChange.mock.calls).toEqual([[false]]);
    expect(onSuccess).not.toHaveBeenCalled();
    expect(mutationCalls()).toEqual({});
  });

  it("opened again on another machine, shows that machine and not what was typed before", async () => {
    signedInAs("admin");
    const { screen } = open({ machine: MACHINE });
    await type(field(screen, "name"), "Brouillon");

    screen.rerender(
      <MachineFormModal
        open
        onOpenChange={() => {}}
        machine={{
          _id: "machine-2" as Id<"machines">,
          name: "Centri Lyon",
          location: "Salle 9",
        }}
      />,
    );

    expect(field(screen, "name").value).toBe("Centri Lyon");
    expect(field(screen, "location").value).toBe("Salle 9");
  });
});

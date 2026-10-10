// @vitest-environment jsdom
import { act, screen, within } from "@testing-library/react";
import { ConvexError } from "convex/values";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";
import {
  answer,
  feedbackShown,
  lastArgsAsked,
  mutation,
  mutationsSent,
  NOW,
  renderPage,
  routeParams,
  serverFailures,
  signedInAs,
  takeLoggedFailures,
  type CurrentUser,
} from "@/test-support/pages";
import GestionnaireDetailPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);

/**
 * The page of one manager, for an admin: the machines and the patients the
 * manager is given, and the two windows that change them.
 */

type Gestionnaire = FunctionReturnType<
  typeof api.users.listGestionnaires
>[number];
type Machine = FunctionReturnType<typeof api.machines.listMachines>[number];
type Managed = FunctionReturnType<
  typeof api.machines.getMachinesForGestionnaire
>[number];
type Patient = FunctionReturnType<typeof api.users.listUsers>[number];
type OwnPatient = FunctionReturnType<
  typeof api.users.getPatientsForGestionnaire
>[number];

/** The sentence of the server for a call that carries no organisation (convex/lib/auth.ts). */
const NO_ACTIVE_ORGANIZATION =
  "No active organization: select an organization to continue";

const gestionnaireId = "user-gaston" as Id<"users">;
const paris = "machine-paris" as Id<"machines">;
const lyon = "machine-lyon" as Id<"machines">;
const nice = "machine-nice" as Id<"machines">;
const brest = "machine-brest" as Id<"machines">;
const rose = "user-rose" as Id<"users">;
const remi = "user-remi" as Id<"users">;
const zoe = "user-zoe" as Id<"users">;

const gaston: Gestionnaire = {
  _id: gestionnaireId,
  firstName: "Gaston",
  lastName: "Lagaffe",
  email: "gaston@centre.test",
  createdAt: NOW - 86_400_000,
  machineCount: 2,
  patientCount: 1,
};

function machine(
  id: Id<"machines">,
  name: string,
  overrides: Partial<Machine> = {},
): Machine {
  return {
    _id: id,
    name,
    status: "online",
    lastHeartbeat: NOW - 5_000,
    serverNow: NOW,
    location: undefined,
    isDeleted: undefined,
    ...overrides,
  };
}

function managed(id: Id<"machines">, name: string): Managed {
  return {
    _id: id,
    name,
    status: "online",
    lastHeartbeat: NOW - 5_000,
    serverNow: NOW,
    location: undefined,
    isOwner: false,
  };
}

function patient(id: Id<"users">, firstName: string): Patient {
  return {
    _id: id,
    firstName,
    lastName: "Rider",
    email: `${firstName.toLowerCase()}@example.test`,
    role: "user",
    language: "fr",
    createdAt: NOW,
  };
}

/** A patient as the server lists them among the manager's own. */
function ownPatient(id: Id<"users">, firstName: string): OwnPatient {
  return {
    _id: id,
    firstName,
    lastName: "Rider",
    email: `${firstName.toLowerCase()}@example.test`,
    language: "fr",
    createdAt: NOW,
  };
}

/** The server as the page of Gaston reads it: he manages Paris and Lyon, and Rose. */
function given() {
  answer(api.users.listGestionnaires, [
    gaston,
    { ...gaston, _id: "user-other" as Id<"users">, firstName: "Autre" },
  ]);
  answer(api.machines.listMachines, [
    machine(paris, "Paris", { location: "Salle 1" }),
    machine(lyon, "Lyon"),
    machine(nice, "Nice"),
  ]);
  answer(api.machines.getMachinesForGestionnaire, [
    managed(paris, "Paris"),
    managed(lyon, "Lyon"),
  ]);
  answer(api.users.listUsers, [patient(rose, "Rose"), patient(remi, "Remi")]);
  answer(api.users.getPatientsForGestionnaire, [ownPatient(rose, "Rose")]);
  mutation(api.machines.setGestionnaireMachines).mockResolvedValue({
    added: 1,
    removed: 1,
  });
  mutation(api.users.assignPatientsToGestionnaire).mockResolvedValue({
    added: 1,
    removed: 1,
  });
}

function open(
  role: CurrentUser["role"] | null = "admin",
  locale: "fr" | "en" = "fr",
) {
  if (role === null) answer(api.users.getCurrentUser, null);
  else signedInAs(role);
  return renderPage(
    <GestionnaireDetailPage params={routeParams({ id: gestionnaireId })} />,
    { locale },
  );
}

/** The card titled `title`. */
function card(title: string) {
  const found = screen
    .getAllByText(title)
    .map((element) => element.closest("[data-slot=card]"))
    .find((element) => element !== null);
  if (!found) throw new Error(`No card is titled "${title}"`);
  return within(found as HTMLElement);
}

const editButton = { name: fr.common.edit };
const saveButton = { name: fr.common.save };

/** The boxes of the open window, by label, with whether each is ticked. */
function boxes(): Record<string, boolean> {
  return Object.fromEntries(
    within(screen.getByRole("dialog"))
      .getAllByRole("checkbox")
      .map((box) => [
        box.parentElement?.querySelector("label")?.textContent ?? "",
        box.getAttribute("aria-checked") === "true",
      ]),
  );
}

type User = Awaited<ReturnType<typeof open>>["user"];

async function openMachines(user: User) {
  await user.click(card(fr.nav.machines).getByRole("button", editButton));
  return within(screen.getByRole("dialog"));
}

async function openPatients(user: User) {
  await user.click(card(fr.nav.patients).getByRole("button", editButton));
  return within(screen.getByRole("dialog"));
}

describe("gestionnaire page: who may read it", () => {
  it("asks for this manager's machines and patients, and for the machines and patients to choose from", async () => {
    given();
    await open();

    expect(lastArgsAsked(api.users.listGestionnaires)).toEqual({});
    expect(lastArgsAsked(api.machines.listMachines)).toEqual({
      includeDeleted: false,
    });
    expect(lastArgsAsked(api.users.listUsers)).toEqual({ role: "user" });
    expect(lastArgsAsked(api.users.getPatientsForGestionnaire)).toEqual({
      gestionnaireId,
    });
    expect(lastArgsAsked(api.machines.getMachinesForGestionnaire)).toEqual({
      gestionnaireId,
    });
  });

  it.each([
    ["who the visitor is", api.users.getCurrentUser],
    ["the managers", api.users.listGestionnaires],
    ["the machines", api.machines.listMachines],
  ] as const)("shows nothing until it has %s", async (_name, query) => {
    given();
    signedInAs("admin");
    answer(query, undefined);
    await renderPage(
      <GestionnaireDetailPage params={routeParams({ id: gestionnaireId })} />,
    );

    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.queryByText("gaston@centre.test")).toBeNull();
  });

  it.each([
    ["a manager", "gestionnaire"],
    ["a patient", "user"],
    // What the page does today: the admin of a client organisation, whom the
    // server lets assign the machines and patients of that organisation, is
    // refused here.
    ["the admin of a client organisation", "org_admin"],
    ["an account without a row", null],
  ] as const)(
    "refuses %s, and shows nothing of the manager",
    async (_name, role) => {
      given();
      await open(role);

      // Written in English whatever the visitor's language: what the page does today.
      expect(screen.getByText("Admin access required")).toBeTruthy();
      expect(screen.queryByText("gaston@centre.test")).toBeNull();
      expect(screen.queryByRole("button")).toBeNull();
    },
  );

  it("says the manager is not found, with the way back, when the list does not hold them", async () => {
    given();
    answer(api.users.listGestionnaires, []);
    await open();

    // Written in English whatever the visitor's language: what the page does today.
    expect(screen.getByText("Gestionnaire not found")).toBeTruthy();
    expect(
      screen.getByText(fr.common.back).closest("a")?.getAttribute("href"),
    ).toBe("/dashboard/gestionnaires");
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("gestionnaire page: what an admin sees", () => {
  it("shows who the manager is, and the machines and the patients they have", async () => {
    given();
    await open();

    expect(
      screen.getByRole("heading", { level: 1, name: "Gaston Lagaffe" }),
    ).toBeTruthy();
    expect(screen.getByText("gaston@centre.test")).toBeTruthy();
    expect(screen.getByText(fr.users.roles.gestionnaire)).toBeTruthy();
    // The arrow before the name leads back to the list.
    expect(screen.getAllByRole("link")[0].getAttribute("href")).toBe(
      "/dashboard/gestionnaires",
    );

    const machines = card(fr.nav.machines);
    expect(machines.getByText("2 machines assignées")).toBeTruthy();
    expect(machines.getByText("Paris")).toBeTruthy();
    expect(machines.getByText("Lyon")).toBeTruthy();
    expect(machines.queryByText("Nice")).toBeNull();
    expect(machines.getAllByText(fr.machines.online)).toHaveLength(2);

    const patients = card(fr.nav.patients);
    // What the page does today: the count is not put in the singular.
    expect(patients.getByText("1 patients assignés")).toBeTruthy();
    expect(patients.getByText("Rose Rider")).toBeTruthy();
    expect(patients.getByText("rose@example.test")).toBeTruthy();
    expect(patients.queryByText("Remi Rider")).toBeNull();
  });

  it("says so when the manager has neither machine nor patient", async () => {
    given();
    answer(api.machines.getMachinesForGestionnaire, []);
    answer(api.users.getPatientsForGestionnaire, []);
    await open();

    expect(
      card(fr.nav.machines).getByText(fr.gestionnaires.noMachinesAssigned),
    ).toBeTruthy();
    expect(
      card(fr.nav.patients).getByText(fr.gestionnaires.noPatientsAssigned),
    ).toBeTruthy();
  });

  it("does not let the machines be edited before the manager's own are known", async () => {
    given();
    answer(api.machines.getMachinesForGestionnaire, undefined);
    const { user } = await open();
    const edit = card(fr.nav.machines).getByRole("button", editButton);

    expect(edit.hasAttribute("disabled")).toBe(true);
    await user.click(edit);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("gestionnaire page: assigning machines", () => {
  it("shows a box per machine in service, ticked for those the manager has", async () => {
    given();
    answer(api.machines.listMachines, [
      machine(paris, "Paris", { location: "Salle 1" }),
      machine(lyon, "Lyon"),
      machine(nice, "Nice"),
      // A deleted machine the server would still list has no box.
      machine(brest, "Brest", { isDeleted: true }),
    ]);
    const { user } = await open();

    const dialog = await openMachines(user);

    expect(dialog.getByText(fr.gestionnaires.assignMachines)).toBeTruthy();
    expect(boxes()).toEqual({
      "Paris(Salle 1)": true,
      Lyon: true,
      Nice: false,
    });
  });

  it("saves exactly the ticked machines for this manager, and confirms on the page what changed", async () => {
    given();
    const { user } = await open();
    const dialog = await openMachines(user);

    await user.click(dialog.getByRole("checkbox", { name: "Lyon" }));
    await user.click(dialog.getByRole("checkbox", { name: "Nice" }));
    expect(mutationsSent()).toEqual([]);
    await user.click(dialog.getByRole("button", saveButton));

    expect(mutationsSent()).toEqual([
      {
        name: "machines:setGestionnaireMachines",
        args: { gestionnaireId, machineIds: [paris, nice] },
      },
    ]);
    const saved = "Machines enregistrées : 1 ajoutée, 1 retirée.";
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("status").textContent).toBe(saved);
    expect(feedbackShown()).toEqual([{ kind: "success", message: saved }]);
  });

  it("confirms with what the server says it changed, in the visitor's language", async () => {
    given();
    mutation(api.machines.setGestionnaireMachines).mockResolvedValue({
      added: 0,
      removed: 2,
    });
    const { user } = await open("admin", "en");

    await user.click(
      card(en.nav.machines).getByRole("button", { name: en.common.edit }),
    );
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: en.common.save,
      }),
    );

    expect(screen.getByRole("status").textContent).toBe(
      "Machines saved: none added, 2 removed.",
    );
  });

  it("keeps the link to a machine that has no box, which nobody could untick", async () => {
    given();
    // Gaston still manages Brest, deleted since: the window has no box for it.
    answer(api.machines.getMachinesForGestionnaire, [
      managed(paris, "Paris"),
      managed(brest, "Brest"),
    ]);
    const { user } = await open();
    const dialog = await openMachines(user);

    await user.click(dialog.getByRole("checkbox", { name: "Nice" }));
    await user.click(dialog.getByRole("button", saveButton));

    expect(mutationsSent()).toEqual([
      {
        name: "machines:setGestionnaireMachines",
        args: { gestionnaireId, machineIds: [brest, paris, nice] },
      },
    ]);
  });

  it.each([
    {
      where: "with the sentence of a refusal the server words",
      // What every function of an organisation answers a call without one.
      error: new ConvexError(NO_ACTIVE_ORGANIZATION),
      shown: NO_ACTIVE_ORGANIZATION,
    },
    ...serverFailures(
      api.machines.setGestionnaireMachines,
      "Machine not found",
    ),
  ])(
    "shows the failure in the window, which stays open with its boxes, and confirms nothing: $where",
    async ({ error, shown }) => {
      given();
      mutation(api.machines.setGestionnaireMachines).mockRejectedValue(error);
      const { user } = await open();
      const dialog = await openMachines(user);

      await user.click(dialog.getByRole("checkbox", { name: "Nice" }));
      await user.click(dialog.getByRole("button", saveButton));

      // Read again from the screen: the window is still there.
      const stillOpen = within(screen.getByRole("dialog"));
      expect(stillOpen.getByRole("alert").textContent).toBe(shown);
      expect(feedbackShown()).toEqual([{ kind: "error", message: shown }]);
      expect(takeLoggedFailures()).toEqual([
        "machines:setGestionnaireMachines",
      ]);
      expect(screen.queryByRole("status")).toBeNull();
      expect(boxes()).toEqual({
        "Paris(Salle 1)": true,
        Lyon: true,
        Nice: true,
      });
      // The save can be tried again.
      expect(
        stillOpen.getByRole("button", saveButton).hasAttribute("disabled"),
      ).toBe(false);
    },
  );

  it("forgets the refusal and the confirmation of an earlier save when the window is opened again", async () => {
    given();
    const save = mutation(api.machines.setGestionnaireMachines);
    save.mockRejectedValueOnce(new ConvexError(NO_ACTIVE_ORGANIZATION));
    const { user } = await open();

    let dialog = await openMachines(user);
    await user.click(dialog.getByRole("button", saveButton));
    expect(dialog.getByRole("alert")).toBeTruthy();
    takeLoggedFailures();
    await user.click(dialog.getByRole("button", { name: fr.common.cancel }));

    dialog = await openMachines(user);
    expect(dialog.queryByRole("alert")).toBeNull();
    await user.click(dialog.getByRole("button", saveButton));
    expect(screen.getByRole("status")).toBeTruthy();

    await openMachines(user);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("sends nothing when the window is cancelled, and opens again on the manager's own machines", async () => {
    given();
    const { user } = await open();
    const dialog = await openMachines(user);

    await user.click(dialog.getByRole("checkbox", { name: "Lyon" }));
    await user.click(dialog.getByRole("checkbox", { name: "Nice" }));
    await user.click(dialog.getByRole("button", { name: fr.common.cancel }));
    expect(mutationsSent()).toEqual([]);
    expect(screen.queryByRole("dialog")).toBeNull();

    await openMachines(user);
    expect(boxes()).toEqual({
      "Paris(Salle 1)": true,
      Lyon: true,
      Nice: false,
    });
  });

  it("cannot be saved twice while the server has not answered", async () => {
    given();
    // The server answers only when the test says so.
    const server = Promise.withResolvers<{ added: number; removed: number }>();
    mutation(api.machines.setGestionnaireMachines).mockReturnValue(
      server.promise,
    );
    const { user } = await open();
    const dialog = await openMachines(user);

    await user.click(dialog.getByRole("button", saveButton));
    const save = dialog.getByRole("button", saveButton);
    expect(save.hasAttribute("disabled")).toBe(true);
    await user.click(save);
    expect(mutationsSent()).toHaveLength(1);

    await act(async () => server.resolve({ added: 0, removed: 0 }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("gestionnaire page: assigning patients", () => {
  it("shows a box per patient, ticked for those the manager has", async () => {
    given();
    const { user } = await open();

    const dialog = await openPatients(user);

    expect(dialog.getByText(fr.gestionnaires.assignPatients)).toBeTruthy();
    expect(boxes()).toEqual({
      "Rose Rider(rose@example.test)": true,
      "Remi Rider(remi@example.test)": false,
    });
  });

  it("says there is no patient to choose from", async () => {
    given();
    answer(api.users.listUsers, []);
    const { user } = await open();

    const dialog = await openPatients(user);

    expect(dialog.getByText(fr.users.noPatients)).toBeTruthy();
    expect(dialog.queryByRole("checkbox")).toBeNull();
  });

  // A list still loading is not an empty one: the window waits for it.
  it("does not open, and so does not say there is no patient, while the patients to choose from are still loading", async () => {
    given();
    answer(api.users.listUsers, undefined);
    const { user, refresh } = await open();
    const edit = card(fr.nav.patients).getByRole("button", editButton);

    expect(edit.hasAttribute("disabled")).toBe(true);
    await user.click(edit);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText(fr.users.noPatients)).toBeNull();

    // Once the list is there, the window opens on it.
    answer(api.users.listUsers, [patient(rose, "Rose"), patient(remi, "Remi")]);
    await refresh();
    await openPatients(user);
    expect(boxes()).toEqual({
      "Rose Rider(rose@example.test)": true,
      "Remi Rider(remi@example.test)": false,
    });
  });

  it("saves exactly the ticked patients for this manager, and confirms on the page what changed", async () => {
    given();
    const { user } = await open();
    const dialog = await openPatients(user);

    await user.click(dialog.getByRole("checkbox", { name: /Rose Rider/ }));
    await user.click(dialog.getByRole("checkbox", { name: /Remi Rider/ }));
    expect(mutationsSent()).toEqual([]);
    await user.click(dialog.getByRole("button", saveButton));

    expect(mutationsSent()).toEqual([
      {
        name: "users:assignPatientsToGestionnaire",
        args: { gestionnaireId, patientIds: [remi] },
      },
    ]);
    const saved = "Patients enregistrés : 1 ajouté, 1 retiré.";
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(card(fr.nav.patients).getByRole("status").textContent).toBe(saved);
    expect(feedbackShown()).toEqual([{ kind: "success", message: saved }]);
  });

  it("confirms with what the server says it changed, in the visitor's language", async () => {
    given();
    // A ticked patient the server did not link is not announced as added.
    mutation(api.users.assignPatientsToGestionnaire).mockResolvedValue({
      added: 0,
      removed: 2,
    });
    const { user } = await open("admin", "en");

    await user.click(
      card(en.nav.patients).getByRole("button", { name: en.common.edit }),
    );
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: en.common.save,
      }),
    );

    expect(card(en.nav.patients).getByRole("status").textContent).toBe(
      "Patients saved: none added, 2 removed.",
    );
  });

  it("keeps the link to a patient who has no box, whom nobody could untick", async () => {
    given();
    // Gaston also manages Zoe, whom the list of patients to choose from does
    // not hold: the window has no box for her.
    answer(api.users.getPatientsForGestionnaire, [
      ownPatient(rose, "Rose"),
      ownPatient(zoe, "Zoe"),
    ]);
    const { user } = await open();
    const dialog = await openPatients(user);
    expect(boxes()).toEqual({
      "Rose Rider(rose@example.test)": true,
      "Remi Rider(remi@example.test)": false,
    });

    await user.click(dialog.getByRole("checkbox", { name: /Remi Rider/ }));
    await user.click(dialog.getByRole("button", saveButton));

    expect(mutationsSent()).toEqual([
      {
        name: "users:assignPatientsToGestionnaire",
        args: { gestionnaireId, patientIds: [zoe, rose, remi] },
      },
    ]);
  });

  it.each([
    {
      where: "with the sentence of a refusal the server words",
      // What every function of an organisation answers a call without one.
      error: new ConvexError(NO_ACTIVE_ORGANIZATION),
      shown: NO_ACTIVE_ORGANIZATION,
    },
    ...serverFailures(
      api.users.assignPatientsToGestionnaire,
      "Target user is not a gestionnaire",
    ),
  ])(
    "shows the failure in the window, which stays open with its boxes, and confirms nothing: $where",
    async ({ error, shown }) => {
      given();
      mutation(api.users.assignPatientsToGestionnaire).mockRejectedValue(error);
      const { user } = await open();
      const dialog = await openPatients(user);

      await user.click(dialog.getByRole("checkbox", { name: /Remi Rider/ }));
      await user.click(dialog.getByRole("button", saveButton));

      // Read again from the screen: the window is still there.
      const stillOpen = within(screen.getByRole("dialog"));
      expect(stillOpen.getByText(fr.gestionnaires.assignPatients)).toBeTruthy();
      expect(stillOpen.getByRole("alert").textContent).toBe(shown);
      expect(feedbackShown()).toEqual([{ kind: "error", message: shown }]);
      expect(takeLoggedFailures()).toEqual([
        "users:assignPatientsToGestionnaire",
      ]);
      expect(screen.queryByRole("status")).toBeNull();
      expect(boxes()).toEqual({
        "Rose Rider(rose@example.test)": true,
        "Remi Rider(remi@example.test)": true,
      });
      // The save can be tried again.
      expect(
        stillOpen.getByRole("button", saveButton).hasAttribute("disabled"),
      ).toBe(false);
    },
  );

  it("forgets the refusal and the confirmation of an earlier save when the window is opened again", async () => {
    given();
    const save = mutation(api.users.assignPatientsToGestionnaire);
    save.mockRejectedValueOnce(new ConvexError(NO_ACTIVE_ORGANIZATION));
    const { user } = await open();

    let dialog = await openPatients(user);
    await user.click(dialog.getByRole("button", saveButton));
    expect(dialog.getByRole("alert")).toBeTruthy();
    takeLoggedFailures();
    await user.click(dialog.getByRole("button", { name: fr.common.cancel }));

    dialog = await openPatients(user);
    expect(dialog.queryByRole("alert")).toBeNull();
    await user.click(dialog.getByRole("button", saveButton));
    expect(card(fr.nav.patients).getByRole("status")).toBeTruthy();

    await openPatients(user);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("cannot be saved twice while the server has not answered", async () => {
    given();
    // The server answers only when the test says so.
    const server = Promise.withResolvers<{ added: number; removed: number }>();
    mutation(api.users.assignPatientsToGestionnaire).mockReturnValue(
      server.promise,
    );
    const { user } = await open();
    const dialog = await openPatients(user);

    await user.click(dialog.getByRole("button", saveButton));
    const save = within(screen.getByRole("dialog")).getByRole(
      "button",
      saveButton,
    );
    expect(save.hasAttribute("disabled")).toBe(true);
    await user.click(save);
    expect(mutationsSent()).toHaveLength(1);

    await act(async () => server.resolve({ added: 0, removed: 0 }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  // The first way the window used to save a list nobody chose: opened and
  // saved before the manager's own patients had arrived, it sent none.
  it("does not let the patients be edited before the manager's own are known, and sends nothing", async () => {
    given();
    answer(api.users.getPatientsForGestionnaire, undefined);
    const { user, refresh } = await open();
    const patients = card(fr.nav.patients);

    // The card says it is loading, not that the manager has no patient.
    expect(patients.getByText(fr.common.loading)).toBeTruthy();
    expect(
      patients.queryByText(fr.gestionnaires.noPatientsAssigned),
    ).toBeNull();
    const edit = patients.getByRole("button", editButton);
    expect(edit.hasAttribute("disabled")).toBe(true);
    await user.click(edit);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mutationsSent()).toEqual([]);

    // Once they are known, the window opens on them.
    answer(api.users.getPatientsForGestionnaire, [ownPatient(rose, "Rose")]);
    await refresh();
    expect(card(fr.nav.patients).queryByText(fr.common.loading)).toBeNull();
    await openPatients(user);
    expect(boxes()).toEqual({
      "Rose Rider(rose@example.test)": true,
      "Remi Rider(remi@example.test)": false,
    });
  });

  it.each([
    ["the manager's own patients", api.users.getPatientsForGestionnaire],
    ["the patients to choose from", api.users.listUsers],
  ] as const)(
    "cannot be saved once %s are loading again, and says so in place of the boxes",
    async (_name, query) => {
      given();
      const { user, refresh } = await open();
      await openPatients(user);

      answer(query, undefined);
      await refresh();

      const dialog = within(screen.getByRole("dialog"));
      const save = dialog.getByRole("button", saveButton);
      expect(save.hasAttribute("disabled")).toBe(true);
      await user.click(save);
      expect(mutationsSent()).toEqual([]);
      expect(dialog.getByText(fr.common.loading)).toBeTruthy();
      expect(dialog.queryByText(fr.users.noPatients)).toBeNull();
      expect(dialog.queryByRole("checkbox")).toBeNull();
    },
  );

  // The second way: the boxes of a cancelled edit used to be still there when
  // the window was opened again, and a save then removed the unticked patients.
  it("sends nothing when the window is cancelled, and opens again on the manager's own patients", async () => {
    given();
    const { user } = await open();
    let dialog = await openPatients(user);

    await user.click(dialog.getByRole("checkbox", { name: /Rose Rider/ }));
    await user.click(dialog.getByRole("checkbox", { name: /Remi Rider/ }));
    await user.click(dialog.getByRole("button", { name: fr.common.cancel }));
    expect(mutationsSent()).toEqual([]);
    expect(screen.queryByRole("dialog")).toBeNull();

    dialog = await openPatients(user);
    expect(boxes()).toEqual({
      "Rose Rider(rose@example.test)": true,
      "Remi Rider(remi@example.test)": false,
    });
    // Saved as it opened, the window sends the manager's own patients.
    await user.click(dialog.getByRole("button", saveButton));
    expect(mutationsSent()).toEqual([
      {
        name: "users:assignPatientsToGestionnaire",
        args: { gestionnaireId, patientIds: [rose] },
      },
    ]);
  });

  it("takes its boxes from the server again when the manager's patients change while it is open", async () => {
    given();
    const { user, refresh } = await open();
    const dialog = await openPatients(user);
    await user.click(dialog.getByRole("checkbox", { name: /Rose Rider/ }));

    // Another admin gives Remi to this manager meanwhile.
    answer(api.users.getPatientsForGestionnaire, [
      ownPatient(rose, "Rose"),
      ownPatient(remi, "Remi"),
    ]);
    await refresh();

    // The boxes are the server's again: what is saved is what is on screen.
    expect(boxes()).toEqual({
      "Rose Rider(rose@example.test)": true,
      "Remi Rider(remi@example.test)": true,
    });
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", saveButton),
    );
    expect(mutationsSent()).toEqual([
      {
        name: "users:assignPatientsToGestionnaire",
        args: { gestionnaireId, patientIds: [rose, remi] },
      },
    ]);
  });
});

// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import { format } from "date-fns";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import fr from "@/messages/fr.json";
import {
  answer,
  argsAsked,
  mutationsSent,
  NOW,
  renderPage,
  router,
  signedInAs,
} from "@/test-support/pages";
import PatientsPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);
// The window that creates or edits a patient sends its own mutations: the
// page only opens it, on no patient or on the one to edit. It is drawn here
// as a window named after what it was opened on.
vi.mock("@/components/modals/PatientFormModal", async () => {
  const { standIn } = await import("@/test-support/pages");
  type Window = {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    patient?: { firstName: string; lastName: string };
  };
  return {
    PatientFormModal: standIn<Window>("PatientFormModal", (window) =>
      window.open ? (
        <div
          role="dialog"
          aria-label={
            window.patient
              ? `edit ${window.patient.firstName} ${window.patient.lastName}`
              : "new patient"
          }
        >
          <button onClick={() => window.onOpenChange(false)}>
            close the window
          </button>
        </div>
      ) : null,
    ),
  };
});

/** The list of patients of a manager or of an admin. */

type Patient = FunctionReturnType<typeof api.users.listUsers>[number];

function patient(
  firstName: string,
  lastName: string,
  overrides: Partial<Patient> = {},
): Patient {
  return {
    _id: `user-${firstName.toLowerCase()}` as Id<"users">,
    firstName,
    lastName,
    email: `${firstName.toLowerCase()}@example.test`,
    role: "user",
    language: "fr",
    createdAt: NOW - 30 * 86_400_000,
    ...overrides,
  };
}

const rose = patient("Rose", "Rider");
const remi = patient("Remi", "Martin", {
  language: "en",
  email: "r.martin@club.test",
});

/** The row of the table that names `text`. */
function rowOf(text: string) {
  const row = screen.getByText(text).closest("tr");
  if (row === null) throw new Error(`No row holds "${text}"`);
  return within(row);
}

/** The patients listed, in the order of the rows. */
function listedNames(): string[] {
  return screen
    .queryAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0].textContent ?? "");
}

const createButton = { name: fr.users.createPatient };

describe("patients list: what it asks and shows", () => {
  it("asks for the accounts of patients only", async () => {
    signedInAs("gestionnaire");
    answer(api.users.listUsers, [rose]);
    await renderPage(<PatientsPage />);

    expect(argsAsked(api.users.listUsers)).not.toEqual([]);
    for (const args of argsAsked(api.users.listUsers)) {
      expect(args).toEqual({ role: "user" });
    }
  });

  it("shows no table, no count and no way to create while the list is loading", async () => {
    signedInAs("gestionnaire");
    answer(api.users.listUsers, undefined);
    await renderPage(<PatientsPage />);

    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText(fr.users.noPatients)).toBeNull();
    expect(screen.queryByRole("button", createButton)).toBeNull();
  });

  it("shows each patient with their e-mail, language and date of creation", async () => {
    signedInAs("gestionnaire");
    answer(api.users.listUsers, [rose, remi]);
    await renderPage(<PatientsPage />);

    expect(
      screen.getByRole("heading", { name: fr.users.patients }),
    ).toBeTruthy();
    expect(screen.getByText("2 patients")).toBeTruthy();
    expect(
      rowOf("Rose Rider")
        .getAllByRole("cell")
        .slice(0, 4)
        .map((cell) => cell.textContent),
    ).toEqual([
      "Rose Rider",
      "rose@example.test",
      "fr",
      // What the page does today: this date is written in English whatever
      // the visitor's language.
      format(rose.createdAt, "PP"),
    ]);
    expect(rowOf("Remi Martin").getByText("en")).toBeTruthy();
  });

  it("says so when there is no patient, and offers to create one", async () => {
    signedInAs("gestionnaire");
    answer(api.users.listUsers, []);
    await renderPage(<PatientsPage />);

    expect(screen.getByText(fr.users.noPatients)).toBeTruthy();
    expect(screen.getByText("0 patients")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getAllByRole("button", createButton)).toHaveLength(2);
  });

  it("keeps the patients that match the search, by name or by e-mail", async () => {
    signedInAs("gestionnaire");
    answer(api.users.listUsers, [rose, remi]);
    const { user } = await renderPage(<PatientsPage />);
    const search = screen.getByPlaceholderText(fr.common.search);

    await user.type(search, "ros");
    expect(listedNames()).toEqual(["Rose Rider"]);

    await user.clear(search);
    await user.type(search, "club.test");
    expect(listedNames()).toEqual(["Remi Martin"]);

    await user.clear(search);
    await user.type(search, "nobody");
    expect(listedNames()).toEqual([]);
    expect(screen.getByText(fr.users.noPatients)).toBeTruthy();
  });

  it("opens the page of a patient from their row and from their link", async () => {
    signedInAs("gestionnaire");
    answer(api.users.listUsers, [rose, remi]);
    const { user } = await renderPage(<PatientsPage />);

    expect(rowOf("Remi Martin").getByRole("link").getAttribute("href")).toBe(
      "/dashboard/patients/user-remi",
    );
    await user.click(screen.getByText("rose@example.test"));

    expect(router.push.mock.calls).toEqual([["/dashboard/patients/user-rose"]]);
  });
});

describe("patients list: creating and editing", () => {
  it("opens the creation window on no patient, and closes it", async () => {
    signedInAs("gestionnaire");
    answer(api.users.listUsers, [rose]);
    const { user } = await renderPage(<PatientsPage />);
    expect(screen.queryByRole("dialog")).toBeNull();

    await user.click(screen.getByRole("button", createButton));
    expect(screen.getByRole("dialog", { name: "new patient" })).toBeTruthy();

    await user.click(screen.getByText("close the window"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens the creation window from an empty list too", async () => {
    signedInAs("admin");
    answer(api.users.listUsers, []);
    const { user } = await renderPage(<PatientsPage />);

    await user.click(screen.getAllByRole("button", createButton)[1]);

    expect(screen.getByRole("dialog", { name: "new patient" })).toBeTruthy();
  });

  it("opens the edit window on the patient of the row, without leaving the list", async () => {
    signedInAs("gestionnaire");
    answer(api.users.listUsers, [rose, remi]);
    const { user } = await renderPage(<PatientsPage />);

    await user.click(
      rowOf("Remi Martin").getByRole("button", { name: fr.common.edit }),
    );

    expect(
      screen.getByRole("dialog", { name: "edit Remi Martin" }),
    ).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "new patient" })).toBeNull();
    expect(router.push).not.toHaveBeenCalled();

    await user.click(screen.getByText("close the window"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("sends nothing by itself: creating and editing belong to the window", async () => {
    signedInAs("gestionnaire");
    answer(api.users.listUsers, [rose]);
    const { user } = await renderPage(<PatientsPage />);

    await user.click(screen.getByRole("button", createButton));
    await user.click(screen.getByText("close the window"));
    await user.click(
      rowOf("Rose Rider").getByRole("button", { name: fr.common.edit }),
    );

    expect(mutationsSent()).toEqual([]);
  });

  // What the page does today: it does not look at the visitor's role. The
  // server decides what the list holds and refuses what a role may not do.
  it.each(["admin", "org_admin", "gestionnaire", "user"] as const)(
    "offers a %s the same list and the same buttons, whatever the role",
    async (role) => {
      signedInAs(role);
      answer(api.users.listUsers, [rose]);
      await renderPage(<PatientsPage />);

      expect(listedNames()).toEqual(["Rose Rider"]);
      expect(screen.getByRole("button", createButton)).toBeTruthy();
      expect(
        rowOf("Rose Rider").getByRole("button", { name: fr.common.edit }),
      ).toBeTruthy();
      expect(argsAsked(api.users.getCurrentUser)).toEqual([]);
    },
  );
});

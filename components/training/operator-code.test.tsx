// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { format } from "date-fns";
import { fr as frLocale } from "date-fns/locale";
import { ConvexError } from "convex/values";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import fr from "@/messages/fr.json";
import { mutation as convexCall } from "@/test-support/convex";
import {
  answer,
  feedbackShown,
  mutationsSent,
  NOW,
  renderPage,
  takeConsoleErrors,
} from "@/test-support/pages";
import { OperatorCodeCard } from "./OperatorCodeCard";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);

/**
 * The operator's own console code (ANH-152): the machines it opens, and the
 * form that sets it, checked before it is sent and emptied after every
 * attempt. The server hashes it and refuses the weak ones
 * (convex/consoleCodes.ts).
 */

const t = fr.operatorCode;
const ACTION = "consoleCodes:setMyOperatorCode";
const DAY = 86_400_000;
// Made up at each run: no code is written in the repository.
const CODE = String(400_000 + Math.floor(Math.random() * 99_999)).padEnd(8, "7");

function mine(codeSetAt: number | null = NOW - DAY) {
  return [
    {
      machineId: "machine-1" as Id<"machines">,
      machineName: "Centri Paris",
      codeSet: codeSetAt !== null,
      codeSetAt,
    },
    {
      machineId: "machine-2" as Id<"machines">,
      machineName: "Centri Lyon",
      codeSet: false,
      codeSetAt: null,
    },
  ];
}

async function card(...given: [ReturnType<typeof mine> | [] | undefined] | []) {
  // An explicit `undefined` is "still loading", not the default rows.
  answer(api.consoleAccess.listMyOperatorMachines, given.length === 0 ? mine() : given[0]);
  return renderPage(<OperatorCodeCard />);
}

async function typeCodes(
  user: Awaited<ReturnType<typeof card>>["user"],
  code: string,
  again: string,
) {
  await user.type(screen.getByLabelText(t.code), code);
  await user.type(screen.getByLabelText(t.confirm), again);
}

const fieldValues = () =>
  [t.code, t.confirm].map((label) => (screen.getByLabelText(label) as HTMLInputElement).value);

describe("operator code: what the card shows", () => {
  it.each<[string, [] | undefined]>([
    ["while the list loads", undefined],
    ["to a member who operates no machine", []],
  ])("shows nothing %s", async (_when, rows) => {
    const view = await card(rows);
    expect(view.container.textContent).toBe("");
  });

  it("lists the machines it opens, and whether the code is set on each", async () => {
    await card();

    expect(screen.getByText(t.title)).toBeTruthy();
    expect(screen.getByText("Centri Paris")).toBeTruthy();
    expect(
      screen.getByText(`Code défini le ${format(NOW - DAY, "PPp", { locale: frLocale })}`),
    ).toBeTruthy();
    expect(screen.getByText("Centri Lyon")).toBeTruthy();
    expect(screen.getByText(t.codeMissing)).toBeTruthy();
    expect(screen.getByText(t.rules)).toBeTruthy();
    const field = screen.getByLabelText(t.code);
    expect(field.getAttribute("type")).toBe("password");
    expect(field.getAttribute("inputmode")).toBe("numeric");
    expect(field.getAttribute("autocomplete")).toBe("new-password");
  });
});

describe("operator code: setting it", () => {
  it("sends nothing while the code is not 6 to 12 digits, or the two fields differ", async () => {
    const { user } = await card();
    const submit = screen.getByRole("button", { name: t.submit });
    expect(submit.hasAttribute("disabled")).toBe(true);

    await typeCodes(user, "12a45", "12a45");
    expect(screen.getByText(t.notDigits)).toBeTruthy();
    expect(submit.hasAttribute("disabled")).toBe(true);

    await user.clear(screen.getByLabelText(t.code));
    await user.clear(screen.getByLabelText(t.confirm));
    await typeCodes(user, CODE, `${CODE.slice(0, -1)}0`);
    expect(screen.getByText(t.mismatch)).toBeTruthy();
    expect(submit.hasAttribute("disabled")).toBe(true);
    await user.type(screen.getByLabelText(t.confirm), "{Enter}");
    expect(mutationsSent()).toEqual([]);
  });

  it("sends the code once both fields agree, says so, and empties both fields", async () => {
    const { user } = await card(mine(null));
    await typeCodes(user, CODE, CODE);
    expect(screen.queryByText(t.mismatch)).toBeNull();

    await user.click(screen.getByRole("button", { name: t.submit }));

    expect(mutationsSent()).toEqual([{ name: ACTION, args: { code: CODE } }]);
    expect(feedbackShown()).toEqual([
      { kind: "success", message: fr.feedback.operatorCodeSet },
    ]);
    expect(fieldValues()).toEqual(["", ""]);
  });

  it("shows why the server refused it, and empties both fields too", async () => {
    convexCall(ACTION).mockRejectedValue(new ConvexError("invalid_operator_code"));
    const { user } = await card();
    await typeCodes(user, CODE, CODE);

    await user.click(screen.getByRole("button", { name: t.submit }));

    expect(feedbackShown()).toEqual([
      { kind: "error", message: fr.errors.invalid_operator_code },
    ]);
    // An action's failure is logged under its own prefix, never with the code.
    const logged = takeConsoleErrors();
    expect(logged).toEqual([expect.stringMatching(`^\\[action\\] ${ACTION} failed`)]);
    expect(logged.join("")).not.toContain(CODE);
    expect(fieldValues()).toEqual(["", ""]);
  });
});

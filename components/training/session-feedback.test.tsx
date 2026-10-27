// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/react";
import { format } from "date-fns";
import { enUS, fr as frLocale } from "date-fns/locale";
import type { FunctionReturnType } from "convex/server";
import { describe, expect, it, vi } from "vitest";
import { ConvexError } from "convex/values";
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
  takeLoggedFailures,
} from "@/test-support/pages";
import { SessionFeedbackCard } from "./SessionFeedbackCard";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);

/**
 * ANH-146: the rider's feedback on the page of a finished session. Whoever
 * reads the session reads it; the rider alone fills or changes it, within
 * 24 h of the end; the server decides, the card only follows `canEdit` and
 * `editableUntil`.
 */

type Read = NonNullable<
  FunctionReturnType<typeof api.sessionFeedback.getSessionFeedback>
>;
const sessionId = "session-1" as Id<"sessions">;
const ENDED = NOW - 3_600_000;
const UNTIL = ENDED + 86_400_000;
const f = fr.sessionFeedback;
const SUBMIT = api.sessionFeedback.submitSessionFeedback;

const given = {
  nauseaScore: 3,
  dizzinessScore: 1,
  comfort: "acceptable" as const,
  freeText: "Un peu barbouillé",
  submittedBy: "rider" as const,
  submittedAt: ENDED + 600_000,
};

function read(overrides: Partial<Read> = {}): Read {
  return {
    eligible: true,
    feedback: null,
    endedAt: ENDED,
    editableUntil: UNTIL,
    canEdit: true,
    ...overrides,
  };
}

function open(locale: "fr" | "en" = "fr") {
  return renderPage(<SessionFeedbackCard sessionId={sessionId} />, { locale });
}

type View = Awaited<ReturnType<typeof open>>;

async function choose(view: View, label: string, option: string) {
  await view.user.click(screen.getByRole("combobox", { name: label }));
  await view.user.click(screen.getByRole("option", { name: option }));
}

const form = () => screen.queryByRole("button", { name: f.save });

describe("ANH-146 S1 the rider fills the feedback within the window", () => {
  it("asks for the feedback of this session, and waits for it", async () => {
    answer(api.sessionFeedback.getSessionFeedback, undefined);
    await open();
    expect(lastArgsAsked(api.sessionFeedback.getSessionFeedback)).toEqual({
      sessionId,
    });
    expect(screen.queryByText(f.title)).toBeNull();
  });

  it("sends whole scores, the comfort and the trimmed comment, and says it is saved", async () => {
    answer(api.sessionFeedback.getSessionFeedback, read());
    const view = await open();

    expect(screen.getByText(f.notProvided)).toBeTruthy();
    expect(
      screen.getByText(
        f.editableUntil.replace(
          "{date}",
          format(UNTIL, "PPp", { locale: frLocale }),
        ),
      ),
    ).toBeTruthy();
    expect((form() as HTMLButtonElement).disabled).toBe(true);

    await choose(view, "Nausée", "3 - Légère à modérée");
    await choose(view, "Malaise ou vertige", "10 - Maximal imaginable");
    await choose(view, f.comfort, "Mauvais");
    const comment = screen.getByRole("textbox", { name: f.freeText });
    expect(comment.getAttribute("maxlength")).toBe("500");
    await view.user.type(comment, "  Nausée en fin de séance  ");
    expect(screen.getByText("27 / 500 caractères")).toBeTruthy();
    await view.user.click(form() as HTMLButtonElement);

    expect(mutationsSent()).toEqual([
      {
        name: "sessionFeedback:submitSessionFeedback",
        args: {
          sessionId,
          nauseaScore: 3,
          dizzinessScore: 10,
          comfort: "bad",
          freeText: "Nausée en fin de séance",
        },
      },
    ]);
    expect(feedbackShown()).toEqual([{ kind: "success", message: f.saved }]);
  });

  it("sends no comment when it is blank, and starts from the feedback already given", async () => {
    answer(api.sessionFeedback.getSessionFeedback, read({ feedback: given }));
    const view = await open();

    // Already given: shown, and the form holds it to be changed.
    expect(screen.getByRole("combobox", { name: "Nausée" }).textContent).toBe(
      "3 - Légère à modérée",
    );
    expect(
      screen
        .getAllByText("3 - Légère à modérée")
        .some((element) => element.tagName === "DD"),
    ).toBe(true);
    const comment = screen.getByRole("textbox", { name: f.freeText });
    expect((comment as HTMLTextAreaElement).value).toBe(given.freeText);
    await view.user.clear(comment);
    await view.user.type(comment, "   ");
    await choose(view, "Nausée", "0 - Aucune");
    await view.user.click(form() as HTMLButtonElement);

    expect(mutationsSent()).toEqual([
      {
        name: "sessionFeedback:submitSessionFeedback",
        args: {
          sessionId,
          nauseaScore: 0,
          dizzinessScore: 1,
          comfort: "acceptable",
        },
      },
    ]);
  });

  it("shows the refusal of the server in words, and keeps what was entered", async () => {
    mutation(SUBMIT).mockRejectedValue(
      new ConvexError("feedback_window_closed"),
    );
    answer(api.sessionFeedback.getSessionFeedback, read({ feedback: given }));
    const view = await open();
    await view.user.click(form() as HTMLButtonElement);

    expect(feedbackShown()).toEqual([
      { kind: "error", message: fr.errors.feedback_window_closed },
    ]);
    expect(takeLoggedFailures()).toEqual([
      "sessionFeedback:submitSessionFeedback",
    ]);
    expect(
      (screen.getByRole("textbox", { name: f.freeText }) as HTMLTextAreaElement)
        .value,
    ).toBe(given.freeText);
    expect((form() as HTMLButtonElement).disabled).toBe(false);
  });

  it("labels each field, in English too", async () => {
    answer(api.sessionFeedback.getSessionFeedback, read());
    await open("en");
    for (const label of [
      "Nausea",
      "Discomfort or dizziness",
      en.sessionFeedback.comfort,
    ])
      expect(screen.getByRole("combobox", { name: label })).toBeTruthy();
    expect(
      screen.getByRole("textbox", { name: en.sessionFeedback.freeText }),
    ).toBeTruthy();
    expect(screen.getByText(en.sessionFeedback.notProvided)).toBeTruthy();
    expect(
      screen.getByText(
        en.sessionFeedback.editableUntil.replace(
          "{date}",
          format(UNTIL, "PPp", { locale: enUS }),
        ),
      ),
    ).toBeTruthy();
  });
});

describe("ANH-146 S2 staff read the feedback, without a form", () => {
  it.each(["operator", "rider"] as const)(
    "shows a feedback entered by the %s with the labels of the scale",
    async (submittedBy) => {
      answer(
        api.sessionFeedback.getSessionFeedback,
        read({ canEdit: false, feedback: { ...given, submittedBy } }),
      );
      await open();

      expect(screen.getByText("Nausée")).toBeTruthy();
      expect(screen.getByText("3 - Légère à modérée")).toBeTruthy();
      expect(screen.getByText("Malaise ou vertige")).toBeTruthy();
      expect(screen.getByText("1 - Très léger")).toBeTruthy();
      expect(screen.getByText("Acceptable")).toBeTruthy();
      expect(screen.getByText(given.freeText)).toBeTruthy();
      expect(screen.getByText(f.by[submittedBy])).toBeTruthy();
      expect(
        screen.getByText(
          format(given.submittedAt, "PPp", { locale: frLocale }),
        ),
      ).toBeTruthy();
      expect(form()).toBeNull();
      expect(screen.queryByRole("combobox")).toBeNull();
      expect(screen.queryByText(f.windowClosed)).toBeNull();
    },
  );

  it("says Non renseigné to staff when there is no feedback", async () => {
    answer(api.sessionFeedback.getSessionFeedback, read({ canEdit: false }));
    await open();
    expect(screen.getByText(f.notProvided)).toBeTruthy();
    expect(form()).toBeNull();
  });
});

describe("ANH-146 the readout and the form, at their edges", () => {
  it("reads the feedback in English, with the English labels of the scale", async () => {
    answer(
      api.sessionFeedback.getSessionFeedback,
      read({ canEdit: false, feedback: given }),
    );
    await open("en");
    expect(screen.getByText("3 - Mild to moderate")).toBeTruthy();
    expect(screen.getByText("1 - Very mild")).toBeTruthy();
    expect(screen.getByText("Acceptable")).toBeTruthy();
    expect(screen.getByText(en.sessionFeedback.by.rider)).toBeTruthy();
    expect(
      screen.getByText(format(given.submittedAt, "PPp", { locale: enUS })),
    ).toBeTruthy();
  });

  it("shows a value the current scale does not know as it was stored", async () => {
    answer(
      api.sessionFeedback.getSessionFeedback,
      read({
        canEdit: false,
        feedback: {
          ...given,
          nauseaScore: 42,
          comfort: "great" as typeof given.comfort,
        },
      }),
    );
    await open();
    expect(screen.getByText("42")).toBeTruthy();
    expect(screen.getByText("great")).toBeTruthy();
  });

  it("sends nothing for a form submitted before it is complete", async () => {
    answer(api.sessionFeedback.getSessionFeedback, read());
    const view = await open();
    await choose(view, "Nausée", "2 - Légère");
    fireEvent.submit(form()?.closest("form") as HTMLFormElement);
    expect(mutationsSent()).toEqual([]);
  });
});

describe("ANH-146 S3 out of the window, or no one on board", () => {
  it("leaves the rider the feedback to read, and says the window is closed", async () => {
    vi.setSystemTime(UNTIL + 1);
    answer(api.sessionFeedback.getSessionFeedback, read({ feedback: given }));
    await open();

    expect(screen.getByText("3 - Légère à modérée")).toBeTruthy();
    expect(screen.getByText(f.windowClosed)).toBeTruthy();
    expect(form()).toBeNull();
  });

  it("still lets the rider edit at the last millisecond of the window", async () => {
    vi.setSystemTime(UNTIL);
    answer(api.sessionFeedback.getSessionFeedback, read());
    await open();
    expect(form()).toBeTruthy();
  });

  it("says Non renseigné and the window closed when the rider gave none in time", async () => {
    answer(
      api.sessionFeedback.getSessionFeedback,
      read({ editableUntil: NOW - 1 }),
    );
    await open();
    expect(screen.getByText(f.notProvided)).toBeTruthy();
    expect(screen.getByText(f.windowClosed)).toBeTruthy();
    expect(form()).toBeNull();
  });

  it.each([
    [
      "a session with no one on board",
      read({ eligible: false, editableUntil: null, canEdit: false }),
    ],
    ["a session the visitor cannot read", null],
  ])("shows no card for %s", async (_name, value) => {
    answer(api.sessionFeedback.getSessionFeedback, value);
    const view = await open();
    expect(view.container.textContent).toBe("");
  });
});

import { ConvexError } from "convex/values";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  describeMutationError,
  dismissFeedback,
  getFeedbackToasts,
  NO_FEEDBACK,
  pushFeedback,
  subscribeFeedback,
  type FeedbackTranslator,
} from "./feedback";

/**
 * The list of messages the site shows after an action, and the wording of a
 * refusal. The hook that feeds it has its own tests
 * (hooks/use-mutation-with-feedback.test.tsx, with the real catalogs); this
 * file holds the list itself (order, limit, leaving, who is told) and the
 * cases of the wording that hook never meets.
 */

const messages = () =>
  getFeedbackToasts().map(({ kind, message }) => `${kind}: ${message}`);

/** A catalog with one translated error code and the two generic sentences. */
const translate: FeedbackTranslator = Object.assign(
  (key: string, values?: Record<string, string | number>) =>
    ({
      "errors.machine_in_session": "La machine est en séance",
      "feedback.failed": "Échec",
      "feedback.failedWithReference": `Échec, référence ${values?.requestId}`,
    })[key] ?? key,
  { has: (key: string) => key === "errors.machine_in_session" },
);

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
  vi.useRealTimers();
});

describe("ANH-203 list of messages", () => {
  it("is empty before any action, and is the same empty list the server draws", () => {
    expect(getFeedbackToasts()).toBe(NO_FEEDBACK);
    expect(NO_FEEDBACK).toEqual([]);
  });

  it("keeps the messages in the order they came, each with an identifier of its own", () => {
    pushFeedback("success", "Premier");
    pushFeedback("error", "Second");

    expect(messages()).toEqual(["success: Premier", "error: Second"]);
    const [first, second] = getFeedbackToasts();
    expect(first.id).not.toBe(second.id);
  });

  it("shows four messages at most: the oldest leaves when a fifth comes", () => {
    for (const text of ["1", "2", "3", "4", "5"]) pushFeedback("success", text);

    expect(messages()).toEqual([
      "success: 2",
      "success: 3",
      "success: 4",
      "success: 5",
    ]);
  });

  it("lets a success leave after 5 s and a failure after 12 s", () => {
    pushFeedback("success", "Fait");
    pushFeedback("error", "Refusé");

    vi.advanceTimersByTime(4_999);
    expect(messages()).toEqual(["success: Fait", "error: Refusé"]);
    vi.advanceTimersByTime(1);
    expect(messages()).toEqual(["error: Refusé"]);
    vi.advanceTimersByTime(6_999);
    expect(messages()).toEqual(["error: Refusé"]);
    vi.advanceTimersByTime(1);
    expect(messages()).toEqual([]);
  });

  it("removes the one message dismissed", () => {
    pushFeedback("success", "Premier");
    pushFeedback("success", "Second");

    dismissFeedback(getFeedbackToasts()[0].id);

    expect(messages()).toEqual(["success: Second"]);
  });

  it("tells who listens of each change, and of nothing when a message already gone is dismissed", () => {
    const told = vi.fn();
    subscribeFeedback(told);

    pushFeedback("success", "Fait");
    const { id } = getFeedbackToasts()[0];
    dismissFeedback(id);
    expect(told).toHaveBeenCalledTimes(2);

    // Its own timer fires later, and a second click on the cross may come: neither is a change.
    dismissFeedback(id);
    vi.advanceTimersByTime(5_000);
    expect(told).toHaveBeenCalledTimes(2);
  });

  it("stops telling a listener that left: a page that is gone is not drawn again", () => {
    const gone = vi.fn();
    const stays = vi.fn();
    const leave = subscribeFeedback(gone);
    subscribeFeedback(stays);

    leave();
    pushFeedback("success", "Fait");

    expect(gone).not.toHaveBeenCalled();
    expect(stays).toHaveBeenCalledTimes(1);
  });
});

describe("ANH-203 wording of a refusal", () => {
  it("translates a code the catalog knows, whichever field carries it", () => {
    for (const data of [
      "machine_in_session",
      { code: "machine_in_session" },
      { error: "machine_in_session" },
    ]) {
      expect(
        describeMutationError(new ConvexError(data), translate),
      ).toMatchObject({
        message: "La machine est en séance",
        code: "machine_in_session",
      });
    }
  });

  it("prefers `code` to `error` when the server sends both", () => {
    const failure = describeMutationError(
      new ConvexError({ code: "machine_in_session", error: "other_code" }),
      translate,
    );

    expect(failure.code).toBe("machine_in_session");
  });

  it("shows the server's own text for a code the catalog does not know", () => {
    expect(
      describeMutationError(
        new ConvexError({ code: "quota_reached", message: "Quota atteint" }),
        translate,
      ),
    ).toEqual({
      message: "Quota atteint",
      code: "quota_reached",
      requestId: undefined,
    });
  });

  it("never takes a sentence for a code: it is shown as it is", () => {
    expect(
      describeMutationError(new ConvexError("Machine is offline"), translate),
    ).toEqual({
      message: "Machine is offline",
      code: undefined,
      requestId: undefined,
    });
  });

  it.each([
    ["a number", 42],
    ["nothing", null],
    ["a code that is not text", { code: 42 }],
    ["a message that is not text", { message: { nested: true } }],
  ])(
    "falls back on the generic sentence when the server sent %s",
    (_what, data) => {
      expect(
        describeMutationError(new ConvexError(data as never), translate)
          .message,
      ).toBe("Échec");
    },
  );

  it("shows what a development server appends after « Uncaught », without the rest", () => {
    const error = new Error(
      "[CONVEX M(machines:deleteMachine)] [Request ID: abc123] Server Error\nUncaught TypeError:  Cannot read x  \n    at handler",
    );

    expect(describeMutationError(error, translate)).toEqual({
      message: "Cannot read x",
      code: undefined,
      requestId: "abc123",
    });
  });

  it("gives the request reference when the server masked the error, so that it can be found in the logs", () => {
    const error = new Error(
      "[CONVEX M(machines:deleteMachine)] [Request ID: abc123] Server Error",
    );

    expect(describeMutationError(error, translate)).toEqual({
      message: "Échec, référence abc123",
      code: undefined,
      requestId: "abc123",
    });
  });

  it("shows the text of an error that never reached the server (no network)", () => {
    expect(
      describeMutationError(new Error("Failed to fetch"), translate).message,
    ).toBe("Failed to fetch");
  });

  it.each([
    ["an error without any text", new Error("")],
    [
      "a masked error without a reference",
      new Error("[CONVEX M(x:y)] Server Error"),
    ],
    ["something that is not an error", "boom"],
    ["nothing at all", undefined],
  ])("falls back on the generic sentence for %s", (_what, error) => {
    expect(describeMutationError(error, translate)).toEqual({
      message: "Échec",
      code: undefined,
      requestId: undefined,
    });
  });
});

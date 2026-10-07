import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { ConvexError } from "convex/values";
import { makeFunctionReference } from "convex/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { dismissFeedback, getFeedbackToasts } from "@/lib/feedback";
import fr from "@/messages/fr.json";
import en from "@/messages/en.json";
import { useMutationWithFeedback } from "./use-mutation-with-feedback";

const serverMutation = vi.hoisted(() => vi.fn());
vi.mock("convex/react", () => ({ useMutation: () => serverMutation }));

const deleteMachine = makeFunctionReference<
  "mutation",
  { machineId: string },
  { deleted: boolean }
>("machines:deleteMachine");

// A code that only this test knows: the real list comes with the server codes.
const CODED = { errors: { machine_in_session: "Traduction du code" } };

/** Renders the hook under the real next-intl provider and returns its function. */
function renderHook(locale: "fr" | "en", extraMessages: object = {}) {
  const rendered: Run[] = [];
  renderToStaticMarkup(
    <NextIntlClientProvider
      locale={locale}
      timeZone="UTC"
      messages={{ ...(locale === "fr" ? fr : en), ...extraMessages }}
    >
      <Probe onRender={(run) => rendered.push(run)} />
    </NextIntlClientProvider>,
  );
  return rendered[0];
}

type Run = ReturnType<typeof useMutationWithFeedback<typeof deleteMachine>>;

function Probe({ onRender }: { onRender: (run: Run) => void }) {
  onRender(useMutationWithFeedback(deleteMachine));
  return null;
}

function shown() {
  return getFeedbackToasts().map(({ kind, message }) => ({ kind, message }));
}

/** What the Convex client rejects with when a mutation throws a plain Error. */
function serverFailure(detail: string) {
  return new Error(
    `[CONVEX M(machines:deleteMachine)] [Request ID: 8f3a9c1d2b4e6f70] Server Error${detail}\n  Called by client`,
  );
}

let logged: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  vi.useFakeTimers();
  serverMutation.mockReset();
  logged = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
  vi.useRealTimers();
  logged.mockRestore();
});

describe("ANH-156 EX-1 useMutationWithFeedback", () => {
  it("success: runs the mutation, returns its value and shows the caller's message", async () => {
    serverMutation.mockResolvedValue({ deleted: true });
    const run = renderHook("fr");

    const outcome = await run(
      { machineId: "m1" },
      { success: "Machine supprimée avec succès" },
    );

    expect(serverMutation).toHaveBeenCalledWith({ machineId: "m1" });
    expect(outcome).toEqual({ ok: true, value: { deleted: true } });
    expect(shown()).toEqual([
      { kind: "success", message: "Machine supprimée avec succès" },
    ]);
    expect(logged).not.toHaveBeenCalled();
  });

  it("success: the message may be made from what the mutation returned", async () => {
    serverMutation.mockResolvedValue({ deleted: true });
    const describe = vi.fn(
      (value: { deleted: boolean }) => `deleted: ${value.deleted}`,
    );

    await renderHook("fr")({ machineId: "m1" }, { success: describe });

    expect(describe).toHaveBeenCalledWith({ deleted: true });
    expect(shown()).toEqual([{ kind: "success", message: "deleted: true" }]);
  });

  it("failure: the success message is neither made nor shown", async () => {
    serverMutation.mockRejectedValue(new ConvexError("Machine is offline"));
    const describe = vi.fn(() => "never");

    await renderHook("fr")({ machineId: "m1" }, { success: describe });

    expect(describe).not.toHaveBeenCalled();
    expect(shown()).toEqual([{ kind: "error", message: "Machine is offline" }]);
  });

  it("success without a message: shows nothing", async () => {
    serverMutation.mockResolvedValue({ deleted: true });

    const outcome = await renderHook("fr")({ machineId: "m1" });

    expect(outcome.ok).toBe(true);
    expect(shown()).toEqual([]);
  });

  it("coded error: the stable code is translated through the message catalog", async () => {
    serverMutation.mockRejectedValue(new ConvexError("machine_in_session"));

    const outcome = await renderHook("fr", CODED)({ machineId: "m1" });

    expect(outcome).toMatchObject({
      ok: false,
      code: "machine_in_session",
      message: "Traduction du code",
    });
    expect(shown()).toEqual([{ kind: "error", message: "Traduction du code" }]);
  });

  it("coded error in an object: the code is translated, not the server text", async () => {
    serverMutation.mockRejectedValue(
      new ConvexError({
        code: "machine_in_session",
        message: "Cannot delete machine with active session",
      }),
    );

    const outcome = await renderHook("fr", CODED)({ machineId: "m1" });

    expect(outcome).toMatchObject({ ok: false, message: "Traduction du code" });
  });

  it("coded error without a translation: the server text is shown", async () => {
    serverMutation.mockRejectedValue(
      new ConvexError({
        error: "machine_in_session",
        message: "Cannot delete machine with active session",
      }),
    );

    const outcome = await renderHook("fr")({ machineId: "m1" });

    expect(outcome).toMatchObject({
      ok: false,
      code: "machine_in_session",
      message: "Cannot delete machine with active session",
    });
    expect(shown()).toEqual([
      { kind: "error", message: "Cannot delete machine with active session" },
    ]);
  });

  it("raw error: the text thrown by the server is shown without its envelope", async () => {
    serverMutation.mockRejectedValue(
      serverFailure(
        "\nUncaught Error: Cannot delete machine with active session\n    at handler (../convex/machines.ts:399:6)",
      ),
    );

    const outcome = await renderHook("fr")(
      { machineId: "m1" },
      { success: "Machine supprimée avec succès" },
    );

    expect(outcome).toMatchObject({
      ok: false,
      message: "Cannot delete machine with active session",
    });
    expect(shown()).toEqual([
      { kind: "error", message: "Cannot delete machine with active session" },
    ]);
  });

  it("raw ConvexError text is shown as it is", async () => {
    serverMutation.mockRejectedValue(new ConvexError("Machine is offline"));

    const outcome = await renderHook("en")({ machineId: "m1" });

    // Free text is never taken for a code.
    expect(outcome).toMatchObject({
      ok: false,
      code: undefined,
      message: "Machine is offline",
    });
  });

  it.each([
    ["fr", fr.feedback.failedWithReference],
    ["en", en.feedback.failedWithReference],
  ] as const)(
    "error masked by the server (%s): generic sentence with the reference",
    async (locale, template) => {
      serverMutation.mockRejectedValue(serverFailure(""));

      const outcome = await renderHook(locale)({ machineId: "m1" });

      const message = template.replace("{requestId}", "8f3a9c1d2b4e6f70");
      expect(outcome).toMatchObject({ ok: false, message });
      expect(shown()).toEqual([{ kind: "error", message }]);
    },
  );

  it("error with nothing readable: generic sentence", async () => {
    serverMutation.mockRejectedValue("boom");

    const outcome = await renderHook("en")({ machineId: "m1" });

    expect(outcome).toMatchObject({ ok: false, message: en.feedback.failed });
  });

  it("logs the failure with the mutation name and the request identifier", async () => {
    const failure = serverFailure("");
    serverMutation.mockRejectedValue(failure);

    await renderHook("fr")({ machineId: "m1" });

    expect(logged).toHaveBeenCalledWith(
      "[mutation] machines:deleteMachine failed",
      { requestId: "8f3a9c1d2b4e6f70", code: undefined },
      failure,
    );
  });

  it("messages leave by themselves, a failure staying longer than a success", async () => {
    serverMutation.mockResolvedValueOnce({ deleted: true });
    serverMutation.mockRejectedValueOnce(new ConvexError("Machine is offline"));
    const run = renderHook("fr");
    await run({ machineId: "m1" }, { success: "Fait" });
    await run({ machineId: "m1" });

    vi.advanceTimersByTime(5_000);
    expect(shown()).toEqual([{ kind: "error", message: "Machine is offline" }]);
    vi.advanceTimersByTime(7_000);
    expect(shown()).toEqual([]);
  });
});

describe("ANH-156 feedback texts", () => {
  it("exist in French and in English, none empty", () => {
    expect(Object.keys(en.feedback)).toEqual(Object.keys(fr.feedback));
    for (const text of [
      ...Object.values(fr.feedback),
      ...Object.values(en.feedback),
    ]) {
      expect(text.trim()).not.toBe("");
    }
  });
});

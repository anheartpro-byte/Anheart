import { click, messages, render, submit, type } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConvexError } from "convex/values";
import { mutation, mutationCalls, resetConvex } from "@/test-support/convex";
import type { Id } from "@/convex/_generated/dataModel";
import { dismissFeedback, getFeedbackToasts } from "@/lib/feedback";
import { MAX_BALLAST_KG, MAX_LAUNCH_NOTES } from "@/lib/training";
import { LaunchBenchModal, parseBallastKg } from "./LaunchBenchModal";

/**
 * ANH-250: the window that launches a ballasted bench session, nobody on
 * board. What it says before anything is sent (nobody on board, the
 * attestation the console will ask for, the arm's radius), what it refuses to
 * send, exactly what it sends, and what a refusal of the server reads as.
 * The server checks it all again (`training.launchBenchSession`).
 */

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/convex")).convexReact,
);
vi.mock(
  "@/components/ui/dialog",
  async () => (await import("@/test-support/ui")).dialog,
);
const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("@/i18n/navigation", () => ({ useRouter: () => router }));

const fr = messages.fr;
const t = fr.training.bench;
const LAUNCH = "training:launchBenchSession";
const machineId = "machine-1" as Id<"machines">;
const sessionId = "session-9" as Id<"sessions">;

function open(locale: "fr" | "en" = "fr") {
  const onOpenChange = vi.fn();
  const screen = render(
    <LaunchBenchModal open onOpenChange={onOpenChange} machineId={machineId} />,
    { locale },
  );
  return { screen, onOpenChange };
}

const sendDisabled = (screen: ReturnType<typeof open>["screen"]) =>
  screen.button(t.submit).hasAttribute("disabled");

beforeEach(() => {
  resetConvex();
  router.push.mockReset();
  mutation(LAUNCH).mockResolvedValue(sessionId);
});
afterEach(() => {
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
});

describe("ANH-250 bench window: what it says", () => {
  it("says nobody is on board, the attestation the console will ask for and the arm's radius", () => {
    const { screen } = open();
    const text = screen.text();
    expect(text).toContain(t.title);
    expect(text).toContain(t.warningTitle);
    expect(text).toContain(`« ${t.attestation} »`);
    expect(text).toContain("145 s");
    expect(text).toContain(t.description);
  });

  it("is said in English too", () => {
    const { screen } = open("en");
    expect(screen.text()).toContain(messages.en.training.bench.attestation);
  });

  it("is closed, with nothing sent, when it is not open", () => {
    const screen = render(
      <LaunchBenchModal
        open={false}
        onOpenChange={vi.fn()}
        machineId={machineId}
      />,
    );
    expect(screen.text()).not.toContain(t.submit);
  });
});

describe("ANH-250 bench window: what is sent", () => {
  it("the machine, the mass and the trimmed note; the live page of the session opens", async () => {
    const { screen, onOpenChange } = open();
    await type(screen.field("bench-mass"), "80");
    await type(screen.field("bench-notes"), "  sac de sable sanglé  ");
    await submit(screen.form());

    expect(mutationCalls()).toEqual({
      [LAUNCH]: [[{ machineId, ballastKg: 80, notes: "sac de sable sanglé" }]],
    });
    expect(getFeedbackToasts().map(({ message }) => message)).toEqual([
      t.launched,
    ]);
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(router.push.mock.calls).toEqual([
      [`/dashboard/sessions/${sessionId}/live`],
    ]);
  });

  it("no mass and no note: neither is sent", async () => {
    const { screen } = open();
    await submit(screen.form());
    expect(mutationCalls()).toEqual({
      [LAUNCH]: [[{ machineId, ballastKg: undefined, notes: undefined }]],
    });
  });

  it.each(["0", "-5", "201", "abc"])(
    "a mass of %s is refused before anything is sent",
    async (mass) => {
      const { screen } = open();
      await type(screen.field("bench-mass"), mass);
      expect(screen.text()).toContain(
        t.massInvalid.replace("{max}", String(MAX_BALLAST_KG)),
      );
      expect(sendDisabled(screen)).toBe(true);
      await submit(screen.form());
      expect(mutationCalls()).toEqual({});
    },
  );

  it("a note over its length is refused before anything is sent", async () => {
    const { screen } = open();
    await type(screen.field("bench-notes"), "n".repeat(MAX_LAUNCH_NOTES + 1));
    expect(screen.text()).toContain(
      t.notesTooLong.replace("{max}", String(MAX_LAUNCH_NOTES)),
    );
    expect(sendDisabled(screen)).toBe(true);
  });

  it("a refusal of the server is worded from its code, and the window stays open", async () => {
    mutation(LAUNCH).mockRejectedValueOnce(
      new ConvexError("machine_contract_too_old"),
    );
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const { screen, onOpenChange } = open();
    await type(screen.field("bench-mass"), "80");
    await submit(screen.form());

    expect(screen.text()).toContain(fr.errors.machine_contract_too_old);
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(router.push).not.toHaveBeenCalled();
    expect(sendDisabled(screen)).toBe(false);
    logged.mockRestore();
  });

  it("cancel closes the window and sends nothing", async () => {
    const { screen, onOpenChange } = open();
    await click(screen.button(fr.common.cancel));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(mutationCalls()).toEqual({});
  });
});

describe("ANH-250 the mass typed", () => {
  it.each([
    ["", null],
    ["  ", null],
    ["80", 80],
    ["80,5", 80.5],
    [String(MAX_BALLAST_KG), MAX_BALLAST_KG],
    ["0", undefined],
    ["-1", undefined],
    ["200.01", undefined],
    ["Infinity", undefined],
    ["8O", undefined],
  ] as const)("%j reads as %s", (text, mass) => {
    expect(parseBallastKg(text)).toBe(mass);
  });
});

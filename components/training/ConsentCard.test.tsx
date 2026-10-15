import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FunctionReturnType } from "convex/server";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { click, messages, render } from "@/test-support/render";
import {
  closeWindow,
  windowOpen,
} from "@/test-support/ui";
import {
  answer,
  mutation,
  mutationCalls,
  resetConvex,
} from "@/test-support/convex";
import { CONSENT_TEXT_VERSION, consentParagraphs } from "@/lib/consentText";
import { dismissFeedback, getFeedbackToasts } from "@/lib/feedback";
import { ConsentCard } from "./ConsentCard";

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
const userId = "rider-1" as Id<"users">;
const READINESS = "fitness:getRiderReadiness";
const CURRENT_USER = "users:getCurrentUser";
const SIGN = "fitness:signConsent";
const WITHDRAW = "fitness:withdrawConsent";
type Readiness = NonNullable<
  FunctionReturnType<typeof api.fitness.getRiderReadiness>
>;
type ReadinessOverrides = Omit<Partial<Readiness>, "consent"> & {
  consent?: Partial<Readiness["consent"]>;
};

function readiness(overrides: ReadinessOverrides = {}): Readiness {
  const { consent, ...rest } = overrides;
  return {
    serverNow: new Date(2026, 9, 15, 12).getTime(),
    questionnaireVersion: "0.1.0",
    consentTextVersion: CONSENT_TEXT_VERSION,
    fitness: null,
    launchRefusal: "consent_missing",
    canValidate: false,
    ...rest,
    consent: {
      active: null,
      lastWithdrawnAt: undefined,
      ...consent,
    },
  };
}

const self = {
  _id: userId,
  firstName: "Paul",
  lastName: "Patient",
  email: "paul@example.test",
};

function show(
  readinessValue: Readiness | null,
  currentUser: typeof self | null = self,
  locale: "fr" | "en" = "fr",
) {
  answer(READINESS, readinessValue);
  answer(CURRENT_USER, currentUser);
  return render(<ConsentCard userId={userId} />, { locale });
}

beforeEach(() => {
  resetConvex();
  mutation(SIGN).mockResolvedValue("consent-1");
  mutation(WITHDRAW).mockResolvedValue(null);
});

afterEach(() => {
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
});

describe("ConsentCard text and state", () => {
  it("renders each paragraph from the consent source and marks its current version provisional", () => {
    const screen = show(readiness());

    for (const paragraph of consentParagraphs()) {
      expect(screen.reading(paragraph)).toHaveLength(1);
    }
    expect(screen.text()).toContain(fr.training.consent.provisional);
    expect(screen.text()).toContain(CONSENT_TEXT_VERSION);
    expect(screen.text()).toContain(fr.training.consent.notSigned);
  });

  it("says in English that the legal text is still shown in French", () => {
    const screen = show(readiness(), self, "en");

    expect(screen.text()).toContain(messages.en.training.consent.frenchTextNote);
    expect(screen.text()).toContain(consentParagraphs()[0]);
  });

  it("shows the signed date and version", () => {
    const signedAt = new Date(2026, 9, 12, 10).getTime();
    const screen = show(
      readiness({
        consent: {
          active: {
            signedAt,
            textVersion: CONSENT_TEXT_VERSION,
            signedVia: "site",
          },
        },
      }),
    );

    expect(screen.text()).toContain("Signé le");
    expect(screen.text()).toContain(CONSENT_TEXT_VERSION);
  });

  it("marks a signature for an old text version as requiring a new signature", () => {
    const screen = show(
      readiness({
        consent: {
          active: {
            signedAt: Date.now(),
            textVersion: "0.0.9",
            signedVia: "site",
          },
        },
      }),
    );

    expect(screen.text()).toContain("0.0.9");
    expect(screen.text()).toContain("à signer de nouveau");
  });

  it("shows the date of a withdrawn consent", () => {
    const screen = show(
      readiness({ consent: { active: null, lastWithdrawnAt: Date.now() } }),
    );

    expect(screen.text()).toContain(fr.training.consent.withdrawn.split("{")[0]);
  });
});

describe("ConsentCard signing and withdrawal", () => {
  it("keeps Sign disabled until the passenger checks consent, then signs the current version", async () => {
    const screen = show(readiness());

    expect(screen.button(fr.training.consent.sign).hasAttribute("disabled")).toBe(true);
    expect(screen.text()).toContain("Paul Patient");
    expect(screen.text()).toContain("paul@example.test");
    await click(screen.button(fr.training.consent.sign));
    expect(mutationCalls()).toEqual({});

    const checkbox = screen.all(
      (element) => element.getAttribute("role") === "checkbox",
    )[0];
    await click(checkbox);
    expect(screen.button(fr.training.consent.sign).hasAttribute("disabled")).toBe(false);
    await click(screen.button(fr.training.consent.sign));

    expect(mutationCalls()[SIGN]).toEqual([
      [{ textVersion: CONSENT_TEXT_VERSION }],
    ]);
  });

  it("never offers signing to a manager", () => {
    const screen = show(
      readiness({ canValidate: true }),
      {
        _id: "manager-1" as Id<"users">,
        firstName: "Alex",
        lastName: "Manager",
        email: "alex@example.test",
      },
    );

    expect(screen.hasButton(fr.training.consent.sign)).toBe(false);
    expect(screen.text()).not.toContain(fr.training.consent.confirmRead);
  });

  it("withdraws active consent only after confirmation", async () => {
    const screen = show(
      readiness({
        consent: {
          active: {
            signedAt: Date.now(),
            textVersion: CONSENT_TEXT_VERSION,
            signedVia: "site",
          },
        },
      }),
    );

    await click(screen.button(fr.training.consent.withdrawSelf));
    expect(windowOpen(screen)).toBe(true);
    expect(mutationCalls()).toEqual({});
    await closeWindow(screen);
    await click(screen.button(fr.training.consent.withdrawSelf));
    await click(screen.button(fr.training.consent.withdrawConfirm));

    expect(mutationCalls()[WITHDRAW]).toEqual([[{ userId }]]);
  });

  it("lets a manager withdraw a patient's active consent after confirmation", async () => {
    const screen = show(
      readiness({
        canValidate: true,
        consent: {
          active: {
            signedAt: Date.now(),
            textVersion: CONSENT_TEXT_VERSION,
            signedVia: "site",
          },
        },
      }),
      {
        _id: "manager-1" as Id<"users">,
        firstName: "Alex",
        lastName: "Manager",
        email: "alex@example.test",
      },
    );

    await click(screen.button(fr.training.consent.withdrawForRider));
    expect(windowOpen(screen)).toBe(true);
    await click(screen.button(fr.training.consent.withdrawConfirm));

    expect(mutationCalls()[WITHDRAW]).toEqual([[{ userId }]]);
  });
});

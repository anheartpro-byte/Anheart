// @vitest-environment jsdom
import type { ReactNode } from "react";
import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";
import {
  answer,
  argsAsked,
  feedbackShown,
  mutation,
  mutationsSent,
  propsOf,
  renderPage,
  serverFailures,
  sessionIs,
  signedInAs,
  takeLoggedFailures,
  wasMounted,
} from "@/test-support/pages";
import Home from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);
// Clerk's two buttons open its own windows: the page only wraps its buttons
// in them.
vi.mock("@clerk/nextjs", async () => {
  const { standIn } = await import("@/test-support/pages");
  const children = ({ children }: { children?: ReactNode }) => children;
  return {
    SignUpButton: standIn("SignUpButton", children),
    SignInButton: standIn("SignInButton", children),
  };
});
vi.mock("@/components/landing/Navbar", async () => ({
  Navbar: (await import("@/test-support/pages")).standIn("Navbar"),
}));
vi.mock("@/components/landing/Footer", async () => ({
  Footer: (await import("@/test-support/pages")).standIn("Footer"),
}));
// Drawn on a canvas or animated: nothing of them is text.
vi.mock("@/components/ui/globe", async () => ({
  Globe: (await import("@/test-support/pages")).standIn("Globe"),
}));
vi.mock("@/components/ui/particles", async () => ({
  Particles: (await import("@/test-support/pages")).standIn("Particles"),
}));
vi.mock("@/components/ui/border-beam", async () => ({
  BorderBeam: (await import("@/test-support/pages")).standIn("BorderBeam"),
}));

/**
 * The public landing page: the presentation of the machine, the way in for a
 * visitor, and the creation of the account's row for one who has just signed
 * up.
 */

/** The addresses the page links to, in order. */
function links(): (string | null)[] {
  return screen.getAllByRole("link").map((link) => link.getAttribute("href"));
}

/** The Clerk window a button opens, if it is wrapped in one. */
function clerkWindowOf(button: HTMLElement): string | null | undefined {
  return button
    .closest("[data-stand-in=SignUpButton], [data-stand-in=SignInButton]")
    ?.getAttribute("data-stand-in");
}

describe("landing page: a visitor who is not signed in", () => {
  it("offers to sign up and to sign in, in Clerk's windows, and no way to the dashboard", async () => {
    sessionIs("signed-out");
    await renderPage(<Home />);

    const buttons = screen.getAllByRole("button");
    expect(
      buttons.map((button) => [button.textContent, clerkWindowOf(button)]),
    ).toEqual([
      [fr.home.getStarted, "SignUpButton"],
      [fr.home.signIn, "SignInButton"],
      [fr.home.cta.getStarted, "SignUpButton"],
      [fr.home.cta.learnMore, undefined],
    ]);
    expect(propsOf<{ mode: string }>("SignUpButton").mode).toBe("modal");
    expect(propsOf<{ mode: string }>("SignInButton").mode).toBe("modal");
    expect(links()).toEqual(["/faq"]);
    expect(screen.queryByText(fr.home.goToDashboard)).toBeNull();
  });

  it("asks nothing about an account and creates none", async () => {
    sessionIs("signed-out");
    answer(api.users.getCurrentUser, null);
    await renderPage(<Home />);

    expect(argsAsked(api.users.getCurrentUser)).toEqual([]);
    expect(mutationsSent()).toEqual([]);
  });
});

describe("landing page: while it is not known whether the visitor is signed in", () => {
  // What the page does today: until Convex knows, neither group of buttons
  // is drawn, so the presentation shows no way in at all.
  it("offers neither to sign up nor the way to the dashboard, and creates nothing", async () => {
    sessionIs("loading");
    answer(api.users.getCurrentUser, null);
    await renderPage(<Home />);

    expect(screen.getByRole("heading", { level: 1 })).toBeTruthy();
    expect(screen.queryAllByRole("button")).toEqual([]);
    expect(screen.queryAllByRole("link")).toEqual([]);
    expect(wasMounted("SignUpButton")).toBe(false);
    expect(argsAsked(api.users.getCurrentUser)).toEqual([]);
    expect(mutationsSent()).toEqual([]);
  });
});

describe("landing page: a visitor who is signed in", () => {
  it("offers the way to the dashboard, twice, and no sign-up", async () => {
    signedInAs("user");
    await renderPage(<Home />);

    expect(links()).toEqual(["/dashboard", "/dashboard"]);
    expect(
      screen.getAllByRole("button").map((button) => button.textContent),
    ).toEqual([fr.home.goToDashboard, fr.home.goToDashboard]);
    expect(wasMounted("SignUpButton")).toBe(false);
    expect(wasMounted("SignInButton")).toBe(false);
  });

  it("creates the row of an account that has none yet, once", async () => {
    answer(api.users.getCurrentUser, null);
    const view = await renderPage(<Home />);

    expect(mutationsSent()).toEqual([
      { name: "users:getOrCreateUser", args: undefined },
    ]);
    expect(feedbackShown()).toEqual([]);

    // The page is drawn again before the server has pushed the new row.
    await view.refresh();
    await view.refresh();
    expect(mutationsSent()).toHaveLength(1);
  });

  it("creates nothing for an account that has its row", async () => {
    signedInAs("gestionnaire");
    const view = await renderPage(<Home />);
    await view.refresh();

    expect(mutationsSent()).toEqual([]);
  });

  it("creates nothing while it does not know whether the account has a row", async () => {
    answer(api.users.getCurrentUser, undefined);
    await renderPage(<Home />);

    expect(mutationsSent()).toEqual([]);
    // The way to the dashboard does not wait for the answer.
    expect(links()).toEqual(["/dashboard", "/dashboard"]);
  });

  it.each(serverFailures(api.users.getOrCreateUser, "Not authenticated"))(
    "says so when the row could not be created $where",
    async ({ error, shown }) => {
      answer(api.users.getCurrentUser, null);
      mutation(api.users.getOrCreateUser).mockRejectedValue(error);
      await renderPage(<Home />);

      expect(feedbackShown()).toEqual([{ kind: "error", message: shown }]);
      expect(takeLoggedFailures()).toEqual(["users:getOrCreateUser"]);
      // The way to the dashboard stays, and nothing is sent again.
      expect(links()).toEqual(["/dashboard", "/dashboard"]);
      expect(mutationsSent()).toHaveLength(1);
    },
  );
});

describe("landing page: the presentation", () => {
  it("is framed by the navigation bar and the footer", async () => {
    sessionIs("signed-out");
    await renderPage(<Home />);

    expect(wasMounted("Navbar")).toBe(true);
    expect(wasMounted("Footer")).toBe(true);
    expect(
      screen
        .getByRole("main")
        .previousElementSibling?.getAttribute("data-stand-in"),
    ).toBe("Navbar");
    expect(
      screen
        .getByRole("main")
        .nextElementSibling?.getAttribute("data-stand-in"),
    ).toBe("Footer");
  });

  it("has one title, and a section for the principle, the heart rate, the uses, the benefits and the invitation", async () => {
    sessionIs("signed-out");
    await renderPage(<Home />);

    expect(
      screen
        .getAllByRole("heading", { level: 1 })
        .map((heading) => heading.textContent),
    ).toEqual([fr.home.title]);
    expect(
      screen
        .getAllByRole("heading", { level: 2 })
        .map((heading) => heading.textContent),
    ).toEqual([
      fr.home.solution.title,
      fr.home.heartRate.title,
      fr.home.useCases.title,
      fr.home.benefits.title,
      fr.home.cta.title,
    ]);
    expect(screen.getByText(fr.home.description)).toBeTruthy();
    expect(screen.getByText(fr.home.basedIn)).toBeTruthy();
  });

  it("explains the principle in three numbered steps", async () => {
    sessionIs("signed-out");
    await renderPage(<Home />);

    const section = within(document.getElementById("solution") as HTMLElement);
    expect(
      section
        .getAllByRole("heading", { level: 3 })
        .map((step) => [
          step.previousElementSibling?.textContent,
          step.textContent,
          step.nextElementSibling?.textContent,
        ]),
    ).toEqual([
      ["01", fr.home.solution.step1Title, fr.home.solution.step1Description],
      ["02", fr.home.solution.step2Title, fr.home.solution.step2Description],
      ["03", fr.home.solution.step3Title, fr.home.solution.step3Description],
    ]);
  });

  it("gives the three heart-rate ranges, each with its state", async () => {
    sessionIs("signed-out");
    await renderPage(<Home />);

    const section = screen
      .getByRole("heading", { name: fr.home.heartRate.title })
      .closest("section") as HTMLElement;
    expect(
      within(section)
        .getAllByRole("heading", { level: 3 })
        .map((state) => [
          state.textContent,
          state.nextElementSibling?.textContent,
        ]),
    ).toEqual([
      [fr.home.heartRate.lyingDown, "40-60"],
      [fr.home.heartRate.standingUp, "60-80"],
      [fr.home.heartRate.training, "40-160"],
    ]);
    expect(within(section).getAllByText(fr.home.heartRate.bpm)).toHaveLength(3);
    // The range reached in training is the one set apart.
    const highlighted = Array.from(section.querySelectorAll(".bg-primary")).map(
      (state) => within(state as HTMLElement).getByRole("heading"),
    );
    expect(highlighted.map((state) => state.textContent)).toEqual([
      fr.home.heartRate.training,
    ]);
  });

  it("lists the five kinds of users, three points each, and the seven benefits", async () => {
    sessionIs("signed-out");
    await renderPage(<Home />);

    const uses = within(document.getElementById("usecases") as HTMLElement);
    expect(
      uses
        .getAllByRole("heading", { level: 4 })
        .map((kind) => [
          kind.textContent,
          kind.nextElementSibling?.querySelectorAll("li").length,
        ]),
    ).toEqual([
      [fr.home.useCases.injuredAthletes, 3],
      [fr.home.useCases.elderly, 3],
      [fr.home.useCases.disabled, 3],
      [fr.home.useCases.eliteAthletes, 3],
      [fr.home.useCases.regularAthletes, 3],
    ]);
    expect(uses.getByText(fr.home.useCases.ambition)).toBeTruthy();

    const benefits = within(document.getElementById("benefits") as HTMLElement);
    expect(
      benefits
        .getAllByRole("heading", { level: 3 })
        .map((benefit) => [
          benefit.textContent,
          benefit.nextElementSibling?.textContent,
        ]),
    ).toEqual([
      [fr.home.benefits.heartTraining, fr.home.benefits.heartTrainingDesc],
      [fr.home.benefits.recovery, fr.home.benefits.recoveryDesc],
      [fr.home.benefits.vo2max, fr.home.benefits.vo2maxDesc],
      [fr.home.benefits.vascularization, fr.home.benefits.vascularizationDesc],
      [fr.home.benefits.injuries, fr.home.benefits.injuriesDesc],
      [fr.home.benefits.balance, fr.home.benefits.balanceDesc],
      [fr.home.benefits.muscle, fr.home.benefits.muscleDesc],
    ]);
  });

  // Written in the dashboard's guide: no figure of intensity is published
  // before the medical decision.
  it("publishes no figure of speed or of g", async () => {
    sessionIs("signed-out");
    await renderPage(<Home />);

    const text = screen.getByRole("main").textContent ?? "";
    expect(text).not.toMatch(/tr\/min|rpm|\d\s?g\b/i);
  });

  it("is in English for an English visitor", async () => {
    sessionIs("signed-out");
    await renderPage(<Home />, { locale: "en" });

    expect(
      screen.getByRole("heading", { level: 1, name: en.home.title }),
    ).toBeTruthy();
    expect(
      screen.getAllByRole("button").map((button) => button.textContent),
    ).toEqual([
      en.home.getStarted,
      en.home.signIn,
      en.home.cta.getStarted,
      en.home.cta.learnMore,
    ]);
  });
});

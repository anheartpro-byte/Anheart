import { click, messages, render, submit, type } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  answer,
  asks,
  mutation,
  mutationCalls,
  resetConvex,
} from "@/test-support/convex";
import {
  choose,
  chosen,
  closeWindow,
  options,
  windowOpen,
} from "@/test-support/ui";
import type { Id } from "@/convex/_generated/dataModel";
import { dismissFeedback, getFeedbackToasts } from "@/lib/feedback";
import { LaunchTrainingModal } from "./LaunchTrainingModal";

/**
 * The window that launches an auto session from the dashboard: who is offered
 * as rider to each role, what stops the launch before the server is asked,
 * what exactly is sent, and what happens after the answer.
 *
 * The server checks everything again (convex/training.ts): what is proven here
 * is that the window does not send what it shows as refused, and sends what
 * the manager chose, nothing else.
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
  "@/components/ui/select",
  async () => (await import("@/test-support/ui")).select,
);
const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("@/i18n/navigation", () => ({ useRouter: () => router }));

const fr = messages.fr;
const t = fr.training.launch;
const LAUNCH = "training:launchAutoSession";

const machineId = "machine-1" as Id<"machines">;
const patientId = "patient-1" as Id<"users">;
const sessionId = "session-9" as Id<"sessions">;

/** A programme the machine synchronised: 30 minutes between 110 and 140 bpm. */
const ENDURANCE = {
  profileId: "endurance",
  name: "Endurance",
  totalDurationS: 1800,
  zoneLowBpm: 110,
  zoneHighBpm: 140,
  hardMaxBpm: 165,
  criticalBpm: 175,
  subjectHrMax: 180,
  minRunRpm: 300,
  maxRpm: 1200,
};
const SPRINT = {
  ...ENDURANCE,
  profileId: "sprint",
  name: "Sprint",
  totalDurationS: 5400,
  zoneLowBpm: 150,
  zoneHighBpm: 170,
  hardMaxBpm: 190,
};
const ENDURANCE_OPTION = "Endurance · 110-140 bpm · 30 min";
const SPRINT_OPTION = "Sprint · 150-170 bpm · 1 h 30";

type Machine = {
  status: string;
  programsEnabled: boolean;
  profiles: (typeof ENDURANCE)[];
  myHrMax: number | null;
};

/** The machine as `training.listLaunchableMachines` answers it for the person signed in. */
function machine(over: Partial<Machine> = {}) {
  answer("training:listLaunchableMachines", [
    {
      _id: machineId,
      name: "Centri Paris",
      status: "online",
      lastHeartbeat: 0,
      serverNow: 0,
      programsEnabled: true,
      live: null,
      profiles: [ENDURANCE, SPRINT],
      myHrMax: 185,
      ...over,
    },
  ]);
}

function signedInAs(role: "admin" | "org_admin" | "gestionnaire" | "user") {
  answer("users:getCurrentUser", {
    _id: "me",
    role,
    firstName: "Ada",
    lastName: "Lovelace",
    email: "ada@anheart.test",
  });
}

const PATIENT = {
  _id: patientId,
  firstName: "Paul",
  lastName: "Martin",
  email: "paul@anheart.test",
};
const PATIENT_OPTION = "Paul Martin (paul@anheart.test)";
const MYSELF_OPTION = `${t.myself} (Ada Lovelace)`;

/** A gestionnaire with one patient, whose record holds what `record` says. */
function gestionnaireWithPatient(record: object | undefined) {
  signedInAs("gestionnaire");
  machine();
  answer("users:getPatientsForGestionnaire", [PATIENT]);
  answer("training:listLaunchRights", []);
  answer("users:getUserById", record);
}

function open(props: Partial<Parameters<typeof LaunchTrainingModal>[0]> = {}) {
  const onOpenChange = vi.fn();
  const screen = render(
    <LaunchTrainingModal
      open
      onOpenChange={onOpenChange}
      machineId={machineId}
      {...props}
    />,
  );
  return { screen, onOpenChange };
}

function shown() {
  return getFeedbackToasts().map(({ kind, message }) => ({ kind, message }));
}

const launchDisabled = (screen: ReturnType<typeof open>["screen"]) =>
  screen.button(t.submit).hasAttribute("disabled");

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-07-01T12:00:00Z"));
  resetConvex();
  router.push.mockReset();
  mutation(LAUNCH).mockResolvedValue(sessionId);
});
afterEach(() => {
  for (const toast of getFeedbackToasts()) dismissFeedback(toast.id);
  vi.useRealTimers();
});

describe("ANH-203 launch window: opening", () => {
  it("draws nothing while closed, and asks the server nothing", () => {
    signedInAs("user");
    machine();

    const { screen } = open({ open: false });

    expect(windowOpen(screen)).toBe(false);
    expect(screen.text()).toBe("");
    expect(asks("training:listLaunchableMachines")).toEqual([]);
  });

  it("shows no form, and no launch button, until the user and the machines are known", () => {
    // Nothing answered yet.
    const { screen } = open();

    expect(screen.text()).toBe(`${t.title} ${t.description}`);
    expect(screen.hasButton(t.submit)).toBe(false);

    // The user is known, the machines are not.
    signedInAs("user");
    screen.rerender(
      <LaunchTrainingModal
        open
        onOpenChange={() => {}}
        machineId={machineId}
      />,
    );
    expect(screen.hasButton(t.submit)).toBe(false);
  });

  it("closes without sending anything on Cancel, and on the window's own close control", async () => {
    signedInAs("user");
    machine();
    const { screen, onOpenChange } = open();

    await click(screen.button(fr.common.cancel));
    await closeWindow(screen);

    expect(onOpenChange.mock.calls).toEqual([[false], [false]]);
    expect(mutationCalls()).toEqual({});
  });

  it("starts from an empty form each time it is opened again", async () => {
    signedInAs("user");
    machine();
    const { screen } = open();
    await choose(screen, SPRINT_OPTION);
    await type(screen.field("launch-notes"), "genou fragile");

    screen.rerender(
      <LaunchTrainingModal
        open={false}
        onOpenChange={() => {}}
        machineId={machineId}
      />,
    );
    screen.rerender(
      <LaunchTrainingModal
        open
        onOpenChange={() => {}}
        machineId={machineId}
      />,
    );

    expect(chosen(screen)).toEqual([]);
    expect(screen.field("launch-notes").value).toBe("");
  });
});

describe("ANH-203 launch window: who can be chosen as rider", () => {
  it("a patient launches for himself only: no list, his own name, and no list of patients asked", () => {
    signedInAs("user");
    machine();

    const { screen } = open();

    expect(options(screen)).toEqual([ENDURANCE_OPTION, SPRINT_OPTION]);
    const name = screen.all(
      (element) =>
        element.localName === "input" && element.value === "Ada Lovelace",
    );
    expect(name).toHaveLength(1);
    expect(name[0].hasAttribute("disabled")).toBe(true);
    expect(asks("users:listUsers")).toEqual(["skip"]);
    expect(asks("users:getPatientsForGestionnaire")).toEqual(["skip"]);
    expect(asks("training:listLaunchRights")).toEqual(["skip"]);
    expect(asks("users:getUserById")).toEqual(["skip"]);
  });

  it("a gestionnaire is offered himself and his own patients, never the list of all users", () => {
    gestionnaireWithPatient({ hrMax: 180, birthYear: 1986 });

    const { screen } = open();

    expect(options(screen)).toEqual([
      ENDURANCE_OPTION,
      SPRINT_OPTION,
      MYSELF_OPTION,
      PATIENT_OPTION,
    ]);
    expect(chosen(screen)).toEqual([MYSELF_OPTION]);
    expect(asks("users:listUsers")).toEqual(["skip"]);
    expect(asks("users:getPatientsForGestionnaire")).toEqual([{}]);
    expect(asks("training:listLaunchRights")).toEqual([{ machineId }]);
  });

  it("an administrator is offered himself and every user with the role of patient", () => {
    signedInAs("admin");
    machine();
    answer("users:listUsers", [PATIENT]);
    answer("training:listLaunchRights", []);

    const { screen } = open();

    expect(options(screen).slice(2)).toEqual([MYSELF_OPTION, PATIENT_OPTION]);
    expect(asks("users:listUsers")).toEqual([{ role: "user" }]);
    expect(asks("users:getPatientsForGestionnaire")).toEqual(["skip"]);
  });

  it("an organisation administrator is offered no rider but himself (what the window does today)", () => {
    // The server lets this role launch for someone else; the window does not offer it.
    signedInAs("org_admin");
    machine();

    const { screen } = open();

    expect(options(screen)).toEqual([ENDURANCE_OPTION, SPRINT_OPTION]);
    expect(asks("users:listUsers")).toEqual(["skip"]);
    expect(asks("users:getPatientsForGestionnaire")).toEqual(["skip"]);
  });

  it("shows no patient while the list of patients is still loading", () => {
    signedInAs("gestionnaire");
    machine();

    const { screen } = open();

    expect(options(screen).slice(2)).toEqual([MYSELF_OPTION]);
  });
});

describe("ANH-203 launch window: what stops a launch before the server is asked", () => {
  it.each([
    [
      "the machine is not one this person may launch on",
      () => answer("training:listLaunchableMachines", []),
      t.notLaunchable,
    ],
    ["the machine is offline", () => machine({ status: "offline" }), t.offline],
    [
      "the machine is already in a session",
      () => machine({ status: "in_session" }),
      t.inSession,
    ],
    [
      "auto programmes are disabled on the machine",
      () => machine({ programsEnabled: false }),
      t.programsDisabled,
    ],
    [
      "the machine synchronised no programme",
      () => machine({ profiles: [] }),
      t.noPrograms,
    ],
    [
      "the rider has no max heart rate",
      () => machine({ myHrMax: null }),
      t.hrMaxMissing,
    ],
  ])(
    "%s: the reason is shown and nothing can be sent",
    async (_case, given, reason) => {
      signedInAs("user");
      given();

      const { screen } = open({ profileId: ENDURANCE.profileId });

      expect(screen.text()).toContain(reason);
      expect(launchDisabled(screen)).toBe(true);
      await submit(screen.form());
      expect(mutationCalls()).toEqual({});
    },
  );

  it("a machine that is not launchable shows no programme, no duration and no rider to fill in", () => {
    signedInAs("user");
    answer("training:listLaunchableMachines", []);

    const { screen } = open();

    expect(options(screen)).toEqual([]);
    expect(screen.tag("input")).toEqual([]);
    expect(screen.tag("textarea")).toEqual([]);
  });

  it("cannot be sent before a programme is chosen", async () => {
    signedInAs("user");
    machine();
    const { screen } = open();

    expect(launchDisabled(screen)).toBe(true);
    await submit(screen.form());
    expect(mutationCalls()).toEqual({});

    await choose(screen, ENDURANCE_OPTION);
    expect(launchDisabled(screen)).toBe(false);
  });

  it("a programme that is no longer on the machine is not selected for the launch", async () => {
    signedInAs("user");
    machine();

    const { screen } = open({ profileId: "removed-since" });

    expect(chosen(screen)).toEqual([]);
    expect(launchDisabled(screen)).toBe(true);
  });

  it.each(["0", "-5", "abc"])(
    "refuses a duration of %s minutes: message shown, nothing sent",
    async (minutes) => {
      signedInAs("user");
      machine();
      const { screen } = open({ profileId: ENDURANCE.profileId });

      await type(screen.field("launch-duration"), minutes);

      expect(screen.text()).toContain(t.durationInvalid);
      expect(launchDisabled(screen)).toBe(true);
      await submit(screen.form());
      expect(mutationCalls()).toEqual({});
    },
  );

  it("a patient whose birth year is unknown cannot be launched", async () => {
    gestionnaireWithPatient({ hrMax: 180 });
    const { screen } = open({ profileId: ENDURANCE.profileId });

    await choose(screen, PATIENT_OPTION);

    expect(screen.text()).toContain(t.birthYearMissing);
    expect(launchDisabled(screen)).toBe(true);
  });

  it("a patient under 18 cannot be launched, one of 18 can", async () => {
    // In 2026: born in 2008 he may still be 17, born in 2007 he is 18 at least.
    gestionnaireWithPatient({ hrMax: 190, birthYear: 2008 });
    const { screen } = open({ profileId: ENDURANCE.profileId });
    await choose(screen, PATIENT_OPTION);

    expect(screen.text()).toContain("au moins 18 ans");
    expect(launchDisabled(screen)).toBe(true);

    answer("users:getUserById", { hrMax: 190, birthYear: 2007 });
    screen.rerender(
      <LaunchTrainingModal
        open
        onOpenChange={() => {}}
        machineId={machineId}
        profileId={ENDURANCE.profileId}
      />,
    );
    expect(screen.text()).not.toContain("au moins 18 ans");
    expect(launchDisabled(screen)).toBe(false);
  });

  it("a patient with neither a max heart rate nor a birth year, on record or in his launch right, is refused", async () => {
    gestionnaireWithPatient({});
    answer("training:listLaunchRights", [
      { userId: patientId, hrMax: null, name: "Paul Martin" },
    ]);
    const { screen } = open({ profileId: ENDURANCE.profileId });

    await choose(screen, PATIENT_OPTION);

    expect(screen.text()).toContain(t.hrMaxMissing);
    expect(screen.text()).toContain(
      `${t.riderHrMax} : ${fr.training.physiology.notSet}`,
    );
    expect(launchDisabled(screen)).toBe(true);
  });
});

describe("ANH-203 launch window: the rider's max heart rate and the programme's zone", () => {
  it("shows the max heart rate the server holds for the person signed in", () => {
    signedInAs("user");
    machine({ myHrMax: 185 });

    const { screen } = open();

    expect(screen.text()).toContain(`${t.riderHrMax} : 185 bpm`);
    expect(screen.text()).not.toContain(t.hrMaxEstimated);
  });

  it("shows the measured max heart rate of the chosen patient", async () => {
    gestionnaireWithPatient({ hrMax: 172, birthYear: 1986 });
    const { screen } = open();

    await choose(screen, PATIENT_OPTION);

    expect(asks("users:getUserById").at(-1)).toEqual({ userId: patientId });
    expect(screen.text()).toContain(`${t.riderHrMax} : 172 bpm`);
    expect(screen.text()).not.toContain(`(${t.hrMaxEstimated})`);
  });

  it("says the max heart rate is estimated when it comes from the birth year", async () => {
    gestionnaireWithPatient({ birthYear: 1986 });
    const { screen } = open();

    await choose(screen, PATIENT_OPTION);

    // 208 - 0.7 x 40
    expect(screen.text()).toContain(
      `${t.riderHrMax} : 180 bpm (${t.hrMaxEstimated})`,
    );
  });

  it("falls back on the max heart rate of the launch right when the record gives none", async () => {
    gestionnaireWithPatient({});
    answer("training:listLaunchRights", [
      { userId: patientId, hrMax: 168, name: "Paul Martin" },
    ]);
    const { screen } = open();

    await choose(screen, PATIENT_OPTION);

    expect(screen.text()).toContain(`${t.riderHrMax} : 168 bpm`);
  });

  it("waits for the patient's record instead of guessing, then says it is not available here", async () => {
    gestionnaireWithPatient(undefined);
    const { screen } = open({ profileId: ENDURANCE.profileId });

    await choose(screen, PATIENT_OPTION);
    expect(screen.text()).toContain(`${t.riderHrMax} : …`);

    // The record arrives without anything to compute a max heart rate from, and no right names him.
    answer("users:getUserById", { birthYear: 1900 });
    screen.rerender(
      <LaunchTrainingModal
        open
        onOpenChange={() => {}}
        machineId={machineId}
        profileId={ENDURANCE.profileId}
      />,
    );
    expect(screen.text()).toContain(`${t.riderHrMax} : ${t.hrMaxUnknown}`);
  });

  it("warns that the server will refuse a zone above 90 % of the rider's max heart rate", async () => {
    // 90 % of 185 is 166: the sprint zone goes up to 170.
    signedInAs("user");
    machine({ myHrMax: 185 });
    const { screen } = open();

    await choose(screen, SPRINT_OPTION);

    expect(screen.text()).toContain(
      "La zone monte à 170 bpm, au-delà de 90 % de la FC max du pratiquant (185 bpm → plafond 166 bpm).",
    );
  });

  it("warns that the programme's limit heart rate is above the rider's max", async () => {
    // Zone up to 140 is under 90 % of 160 (144), but the limit of 165 is above 160.
    signedInAs("user");
    machine({ myHrMax: 160 });
    const { screen } = open();

    await choose(screen, ENDURANCE_OPTION);

    expect(screen.text()).toContain(
      "La FC limite du programme (165 bpm) dépasse la FC max du pratiquant (160 bpm).",
    );
  });

  it("warns of nothing when the zone and the limit fit the rider", async () => {
    signedInAs("user");
    machine({ myHrMax: 185 });
    const { screen } = open();

    await choose(screen, ENDURANCE_OPTION);

    expect(screen.text()).not.toContain("Le serveur refusera ce lancement");
    // The programme chosen is described: zone, limit, arm speed (1200 / 49.79).
    expect(screen.text()).toContain(
      "Zone cible 110-140 bpm · FC limite 165 bpm · Vitesse max 24.1 tr/min bras",
    );
  });
});

describe("ANH-203 launch window: what is sent", () => {
  it("for oneself, with the programme's own duration: the machine and the programme, nothing else", async () => {
    signedInAs("user");
    machine();
    const { screen, onOpenChange } = open({ profileId: ENDURANCE.profileId });

    expect(screen.text()).toContain(
      "Laisser vide pour la durée du programme (30 min)",
    );
    await click(screen.button(t.submit));
    await submit(screen.form());

    expect(mutationCalls()).toEqual({
      [LAUNCH]: [
        [
          {
            machineId,
            profileId: "endurance",
            userId: undefined,
            totalDurationS: undefined,
            notes: undefined,
          },
        ],
      ],
    });
    expect(shown()).toEqual([
      { kind: "success", message: fr.feedback.sessionLaunched },
    ]);
    // The window closes and the page of the new session opens.
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(router.push.mock.calls).toEqual([
      [`/dashboard/sessions/${sessionId}/live`],
    ]);
  });

  it("for a patient, with a duration in minutes turned into seconds and trimmed notes", async () => {
    gestionnaireWithPatient({ hrMax: 180, birthYear: 1986 });
    const { screen } = open();

    await choose(screen, ENDURANCE_OPTION);
    await choose(screen, PATIENT_OPTION);
    await type(screen.field("launch-duration"), " 12.5 ");
    await type(screen.field("launch-notes"), "  première séance  ");
    await submit(screen.form());

    expect(mutationCalls()).toEqual({
      [LAUNCH]: [
        [
          {
            machineId,
            profileId: "endurance",
            userId: patientId,
            totalDurationS: 750,
            notes: "première séance",
          },
        ],
      ],
    });
  });

  it("a manager who launches for himself sends no rider", async () => {
    gestionnaireWithPatient({ hrMax: 180, birthYear: 1986 });
    const { screen } = open({ profileId: SPRINT.profileId });
    await choose(screen, PATIENT_OPTION);
    await choose(screen, MYSELF_OPTION);

    // 170 is above 90 % of 185: warned, not blocked. The server decides.
    await submit(screen.form());

    expect(mutation(LAUNCH)).toHaveBeenCalledTimes(1);
    expect(mutation(LAUNCH).mock.calls[0][0]).toMatchObject({
      profileId: "sprint",
      userId: undefined,
    });
  });

  it("hands the new session to the caller instead of changing page when asked to", async () => {
    signedInAs("user");
    machine();
    const onLaunched = vi.fn();
    const { screen } = open({ profileId: ENDURANCE.profileId, onLaunched });

    await submit(screen.form());

    expect(onLaunched.mock.calls).toEqual([[sessionId]]);
    expect(router.push).not.toHaveBeenCalled();
  });

  it("cannot be sent twice while the server has not answered", async () => {
    signedInAs("user");
    machine();
    let answerLaunch: (id: Id<"sessions">) => void = () => {};
    mutation(LAUNCH).mockImplementation(
      () => new Promise((resolve) => (answerLaunch = resolve)),
    );
    const { screen } = open({ profileId: ENDURANCE.profileId });

    await submit(screen.form());
    expect(launchDisabled(screen)).toBe(true);
    await submit(screen.form());
    await click(screen.button(t.submit));

    expect(mutation(LAUNCH)).toHaveBeenCalledTimes(1);
    answerLaunch(sessionId);
  });
});

describe("ANH-203 launch window: when the server refuses", () => {
  it("shows the refusal in the window, keeps it open with what was typed, and lets the manager try again", async () => {
    signedInAs("user");
    machine();
    mutation(LAUNCH).mockRejectedValueOnce(new Error("Machine is offline"));
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const { screen, onOpenChange } = open({ profileId: ENDURANCE.profileId });
    await type(screen.field("launch-notes"), "à refaire");

    await submit(screen.form());

    expect(screen.text()).toContain("Machine is offline");
    expect(shown()).toEqual([{ kind: "error", message: "Machine is offline" }]);
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(router.push).not.toHaveBeenCalled();
    expect(screen.field("launch-notes").value).toBe("à refaire");
    expect(launchDisabled(screen)).toBe(false);

    // The second attempt succeeds: the refusal leaves the window.
    await submit(screen.form());
    expect(mutation(LAUNCH)).toHaveBeenCalledTimes(2);
    expect(onOpenChange).toHaveBeenCalledWith(false);
    logged.mockRestore();
  });
});

describe("ANH-203 launch window: in English", () => {
  it("shows the same refusal reason in the language of the page", () => {
    signedInAs("user");
    machine({ status: "offline" });
    const screen = render(
      <LaunchTrainingModal
        open
        onOpenChange={() => {}}
        machineId={machineId}
      />,
      { locale: "en" },
    );

    expect(screen.text()).toContain(messages.en.training.launch.offline);
    expect(screen.text()).not.toContain(t.offline);
  });
});

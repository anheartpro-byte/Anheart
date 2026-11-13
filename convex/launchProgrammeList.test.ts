/// <reference types="vite/client" />
/**
 * Contract 1.12 (ANH-138, `launch_programme_list` of contracts/machine-api.json):
 * an auto launch is tied to the programme list it was made from. The machine
 * sends the digest of its list with its programmes; the launch records the one
 * the machine had; the poll gives it; the start says the one the machine holds
 * now, and another is refused `programme_changed`. A bench launch carries no
 * programme and is armed as before.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";
import { PROFILES_DIGEST } from "./lib/programmes";
import { ARMING_REFUSALS } from "./training";
import {
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  seedLaunchWorld,
  seedMachineWorld,
  type World,
} from "./test.setup";
import machineApi from "../contracts/machine-api.json";

type MachineWorld = Awaited<ReturnType<typeof seedMachineWorld>>;

const A = "a".repeat(64);
const B = "b".repeat(64);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  configureAnheartOrganization(null);
});

/** The programme `seedMachineWorld` syncs, as a machine sends it. */
const P1 = {
  profileId: "p1",
  name: "Programme p1",
  totalDurationS: 600,
  zoneLowBpm: 130,
  zoneHighBpm: 150,
  hardMaxBpm: 170,
  criticalBpm: 185,
  subjectHrMax: 190,
  minRunRpm: 20,
  maxRpm: 1500,
};

function post(w: MachineWorld, path: string, body: Record<string, unknown>) {
  return w.t.fetch(path, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${w.machineKey}`,
      [CONTRACT_HEADER]: CONTRACT_VERSION,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
}

/** One push of the programmes, 10 s after the one before: the route's pace (ANH-228). */
function sendProfiles(w: MachineWorld, extra: Record<string, unknown>) {
  vi.setSystemTime(Date.now() + 10_000);
  return post(w, "/api/machine/profiles", {
    storeRev: 2,
    programsEnabled: true,
    profiles: [P1],
    ...extra,
  });
}

/** The seeded manager launches the seeded programme for the patient. */
const launch = (w: World) =>
  as(w.t, "manager").mutation(api.training.launchAutoSession, {
    machineId: w.machine,
    profileId: w.profileId,
    userId: w.patient,
  });

const holds = (w: World, digest: string | undefined) =>
  w.t.run((ctx) => ctx.db.patch(w.machine, { profilesDigest: digest }));

const arm = (w: World, sessionId: Id<"sessions">, profilesDigest?: string) =>
  w.t.mutation(internal.training.markTrainingStarted, {
    machineId: w.machine,
    sessionId,
    ...(profilesDigest === undefined ? {} : { profilesDigest }),
  });

const row = <Table extends "sessions" | "machines">(w: Pick<World, "t">, id: Id<Table>) =>
  w.t.run((ctx) => ctx.db.get(id));

const changed = {
  armed: false,
  code: "programme_changed",
  message: ARMING_REFUSALS.programme_changed,
};

describe("contract 1.12: the digest of a machine's programme list", () => {
  it("is the shape the shared file gives, and a code of the machine API", () => {
    expect(PROFILES_DIGEST.source).toBe(machineApi.launch_programme_list.digest_pattern);
    expect(machineApi.launch_programme_list.since).toBe("1.12");
    expect(machineApi.arming_refusals.codes).toContain("programme_changed");
    expect(machineApi.error_codes.programme_changed.statuses).toEqual([409]);
    expect(PROFILES_DIGEST.test(A)).toBe(true);
    expect(PROFILES_DIGEST.test(A.toUpperCase())).toBe(false);
    expect(PROFILES_DIGEST.test(`${A}0`)).toBe(false);
  });

  it("is kept with the programmes, cleared when an older console sends none, refused in another shape", async () => {
    const w = await seedMachineWorld(modules);
    expect((await sendProfiles(w, { profilesDigest: A })).status).toBe(200);
    expect((await row(w, w.machine))?.profilesDigest).toBe(A);

    expect((await sendProfiles(w, {})).status).toBe(200);
    expect((await row(w, w.machine))?.profilesDigest).toBeUndefined();

    for (const wrong of [A.toUpperCase(), "abc", 12, null]) {
      const refused = await sendProfiles(w, { profilesDigest: wrong });
      expect(refused.status).toBe(400);
      expect(await refused.json()).toMatchObject({ error: "invalid_request" });
    }
    expect((await row(w, w.machine))?.profilesDigest).toBeUndefined();
  });
});

describe("contract 1.12: an auto launch made from a list", () => {
  it("records the machine's digest, and the poll offers it with the launch", async () => {
    const w = await seedLaunchWorld(modules);
    await holds(w, A);
    const sessionId = await launch(w);
    expect((await row(w, sessionId))?.profilesDigest).toBe(A);
    expect(
      await w.t.query(internal.training.getPendingTrainingSession, { machineId: w.machine }),
    ).toMatchObject({ sessionId, mode: "auto", profilesDigest: A });
  });

  it("records none, and offers null, while the machine sent none", async () => {
    const w = await seedLaunchWorld(modules);
    const sessionId = await launch(w);
    expect((await row(w, sessionId))?.profilesDigest).toBeUndefined();
    expect(
      await w.t.query(internal.training.getPendingTrainingSession, { machineId: w.machine }),
    ).toMatchObject({ sessionId, profilesDigest: null });
  });

  // Launch made from / start confirmed with: armed or refused.
  it.each([
    ["the same list", A, A, true],
    ["another list", A, B, false],
    ["a list, confirmed by a console that sends none", A, undefined, false],
    ["no list, confirmed by a console that sends one", undefined, A, false],
    ["no list, confirmed by an older console", undefined, undefined, true],
  ] as const)("made from %s: armed %s", async (_case, made, confirmed, armed) => {
    const w = await seedLaunchWorld(modules);
    await holds(w, made);
    const sessionId = await launch(w);
    if (armed) {
      expect(await arm(w, sessionId, confirmed)).toEqual({ armed: true });
      expect((await row(w, sessionId))?.status).toBe("active");
      return;
    }
    const machineBefore = await row(w, w.machine);
    expect(await arm(w, sessionId, confirmed)).toEqual(changed);
    expect(await row(w, sessionId)).toMatchObject({
      status: "failed",
      endCode: "programme_changed",
      endReason: ARMING_REFUSALS.programme_changed,
    });
    expect(await row(w, w.machine)).toEqual(machineBefore);
    // Confirmed again, with the right list now: the same refusal. It is launched again.
    expect(await arm(w, sessionId, made)).toEqual(changed);
  });

  it("is checked after every other refusal: an expired launch says it expired", async () => {
    const w = await seedLaunchWorld(modules);
    await holds(w, A);
    const sessionId = await launch(w);
    vi.setSystemTime(NOW + 60 * 60 * 1000);
    expect(await arm(w, sessionId, B)).toMatchObject({ code: "expired_before_start" });
  });
});

describe("contract 1.12: a bench launch", () => {
  it("carries no programme, and is armed whatever list the machine sends", async () => {
    for (const confirmed of [A, undefined]) {
      const w = await seedLaunchWorld(modules);
      await holds(w, B);
      const sessionId = await w.t.run((ctx) =>
        ctx.db.insert("sessions", {
          organizationId: w.orgA,
          machineId: w.machine,
          startedById: w.manager,
          status: "pending",
          startedAt: NOW,
          channels: ["ECG"],
          kind: "manual",
          origin: "remote",
          operatorName: "manager Synthetic",
          ballast: { massKg: 80 },
        }),
      );
      expect(await arm(w, sessionId, confirmed)).toEqual({ armed: true });
    }
  });
});

describe("contract 1.12: POST /api/machine/training/start", () => {
  it("reads the digest the machine holds, and answers 409 programme_changed on another", async () => {
    const w = await seedMachineWorld(modules);
    const pending = (digest: string) =>
      w.t.run((ctx) =>
        ctx.db.insert("sessions", {
          organizationId: w.organizationId,
          machineId: w.machine,
          userId: w.patient,
          startedById: w.patient,
          status: "pending",
          startedAt: Date.now(),
          channels: ["ECG"],
          kind: "auto",
          origin: "remote",
          profileId: "p1",
          profileName: "Programme p1",
          subjectHrMax: 180,
          subjectAge: 36,
          operatorName: "Synthetic Operator",
          profilesDigest: digest,
        }),
      );
    const refused = await pending(A);
    const answer = await post(w, "/api/machine/training/start", {
      sessionId: refused,
      profilesDigest: B,
    });
    expect(answer.status).toBe(409);
    expect(await answer.json()).toEqual({
      error: "programme_changed",
      message: ARMING_REFUSALS.programme_changed,
    });
    // A digest that is not text is not read: the console sent none.
    const unread = await pending(A);
    const ignored = await post(w, "/api/machine/training/start", {
      sessionId: unread,
      profilesDigest: 12,
    });
    expect(ignored.status).toBe(409);

    const taken = await pending(A);
    const armed = await post(w, "/api/machine/training/start", {
      sessionId: taken,
      profilesDigest: A,
    });
    expect(armed.status).toBe(200);
    expect((await row(w, taken))?.status).toBe("active");
  });
});

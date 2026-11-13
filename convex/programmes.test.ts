/// <reference types="vite/client" />
/**
 * ANH-137: the programme catalogue. The Pi's verdict on every shared case
 * (EX-2), the life of a programme and its double validation (EX-3, EX-4,
 * EX-7). Who may call what is in `authorization.matrix.ts`.
 */
import { ConvexError } from "convex/values";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import cases from "../contracts/programme-validation-cases.json";
import { api } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import {
  FALLBACK_PROFILE_IDS,
  MAX_CATALOG_PAYLOAD_BYTES,
  type ProgrammeProfile,
} from "./lib/programmes";
import { registryUpload } from "./machineRegistry.fixtures";
import {
  addSecondAdmin,
  asSecondAdmin,
  insertProgramme,
  JOG,
  SIGNOFF,
} from "./programmes.fixtures";
import { as, modules, NOW, seedWorld, type World } from "./test.setup";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => vi.useRealTimers());

/** The error a call ends with, or null when it succeeds. */
async function failure(call: Promise<unknown>): Promise<unknown> {
  return await call.then(
    () => null,
    (error: unknown) => error,
  );
}

/** A Pi document of the shared file, with Convex's names. */
function camel(document: Record<string, unknown>): ProgrammeProfile {
  return Object.fromEntries(
    Object.entries(document).map(([key, value]) => [
      key.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase()),
      value,
    ]),
  ) as ProgrammeProfile;
}

const programme = (w: World, id: Id<"programmes">) =>
  w.t.run((ctx) => ctx.db.get(id));

describe("ANH-137 EX-2 the Pi's verdict on every shared case", () => {
  it.each(cases.cases)("$name", async (testCase) => {
    const w = await seedWorld(modules);
    const profile = camel({ ...cases.base, ...testCase.changes });
    // The shipped ids are the consoles' fallbacks, which no catalogue programme
    // takes (ANH-138): such a case runs under another id, no verdict reads it.
    if (FALLBACK_PROFILE_IDS.includes(profile.profileId))
      profile.profileId = `case_${profile.profileId}`;
    const error = await failure(
      as(w.t, "admin").mutation(api.programmes.create, profile),
    );
    const expected = testCase.expected;
    if (expected.verdict === "accepted") {
      expect(error).toBeNull();
      return;
    }
    expect(error).toBeInstanceOf(ConvexError);
    expect((error as ConvexError<{ message: string }>).data).toEqual({
      code: "programme_refused",
      message: expected.detail,
      violations: "violations" in expected ? expected.violations : [],
    });
    expect(await w.t.run((ctx) => ctx.db.query("programmes").collect())).toEqual([]);
  });

  it("refuses a name with a surrogate left unpaired, which no JSON a machine writes holds", async () => {
    const w = await seedWorld(modules);
    for (const name of ["\uD800", "jog \uDC00", "\uDE00\uD83D"]) {
      const error = await failure(
        as(w.t, "admin").mutation(api.programmes.create, { ...JOG, name }),
      );
      expect((error as ConvexError<{ message: string }>).data).toEqual({
        code: "programme_refused",
        message: "training profile refused: name_lone_surrogate",
        violations: ["name_lone_surrogate"],
      });
    }
    // A pair is one character: accepted.
    expect(
      await failure(as(w.t, "admin").mutation(api.programmes.create, { ...JOG, name: "jog 😀" })),
    ).toBeNull();
  });

  it("refuses at saving a zone above 0.9 x subject_hr_max with the Pi's message", async () => {
    const w = await seedWorld(modules);
    const programmeId = await insertProgramme(w, "draft", w.admin);
    const error = await failure(
      as(w.t, "admin").mutation(api.programmes.updateDraft, {
        programmeId,
        ...JOG,
        subjectHrMax: 170,
      }),
    );
    expect((error as ConvexError<{ message: string }>).data.message).toBe(
      "training profile refused: zone_above_subject_ceiling",
    );
    expect((await programme(w, programmeId))?.subjectHrMax).toBe(175);
  });
});

describe("ANH-137 acceptance: jog 145-155, 30 min", () => {
  it("is created, validated by a second admin, published, and read by a gestionnaire", async () => {
    const w = await seedWorld(modules);
    await addSecondAdmin(w);
    const author = asSecondAdmin(w);
    const programmeId = await author.mutation(api.programmes.create, JOG);
    await author.mutation(api.programmes.attachSimulationReport, {
      programmeId,
      storageId: await registryUpload(w),
    });

    // The author cannot validate their own draft.
    expect(
      String(
        await failure(
          author.mutation(api.programmes.validate, {
            programmeId,
            medicalSignoff: SIGNOFF,
          }),
        ),
      ),
    ).toMatch(/programme_self_validation/);

    const admin = as(w.t, "admin");
    await admin.mutation(api.programmes.validate, {
      programmeId,
      medicalSignoff: SIGNOFF,
    });
    await admin.mutation(api.programmes.publish, { programmeId });

    const published = await programme(w, programmeId);
    expect(published).toMatchObject({
      status: "published",
      revision: 1,
      medicalParametersVersion: "0.1.0",
      medicalSignoff: SIGNOFF,
      validatedBy: w.admin,
    });
    const [revision] = await w.t.run((ctx) =>
      ctx.db.query("programme_revisions").collect(),
    );
    const { _id, _creationTime, ...copy } = published!;
    expect(revision).toMatchObject({ programmeId: _id, ...copy });
    expect(revision._creationTime).toBeGreaterThanOrEqual(_creationTime);

    const manager = as(w.t, "manager");
    const listed = await manager.query(api.programmes.list, {});
    expect(listed.map((row) => [row._id, row.name])).toEqual([
      [programmeId, "jog 145-155, 30 min"],
    ]);
    expect(
      String(
        await failure(
          manager.mutation(api.programmes.retire, { programmeId }),
        ),
      ),
    ).toMatch(/not_authorized/);
  });
});

describe("ANH-138 what the machines are sent", () => {
  it("keeps the ids of the consoles' fallback programmes out of the catalogue", async () => {
    expect(FALLBACK_PROFILE_IDS).toEqual(["standard_30_min", "standard_45_min"]);
    const w = await seedWorld(modules);
    const admin = as(w.t, "admin");
    for (const profileId of FALLBACK_PROFILE_IDS) {
      expect(
        String(await failure(admin.mutation(api.programmes.create, { ...JOG, profileId }))),
      ).toMatch(/programme_id_taken/);
    }
    const draft = await insertProgramme(w, "draft", w.admin);
    expect(
      String(
        await failure(
          admin.mutation(api.programmes.updateDraft, {
            programmeId: draft,
            ...JOG,
            profileId: "standard_45_min",
          }),
        ),
      ),
    ).toMatch(/programme_id_taken/);
  });

  it("never validates nor publishes a programme that holds a fallback id from before its reservation", async () => {
    const w = await seedWorld(modules);
    const admin = as(w.t, "admin");
    const draft = await insertProgramme(w, "draft", await addSecondAdmin(w), {
      profileId: "standard_30_min",
    });
    expect(
      String(
        await failure(
          admin.mutation(api.programmes.validate, { programmeId: draft, medicalSignoff: SIGNOFF }),
        ),
      ),
    ).toMatch(/programme_id_taken/);
    const validated = await insertProgramme(w, "validated", w.admin, {
      profileId: "standard_45_min",
    });
    expect(
      String(await failure(admin.mutation(api.programmes.publish, { programmeId: validated }))),
    ).toMatch(/programme_id_taken/);
    expect(await programme(w, draft)).toMatchObject({ status: "draft" });
    expect(await programme(w, validated)).toMatchObject({ status: "validated", revision: 0 });
  });

  it("refuses to publish what would make the signed catalogue larger than a console reads", async () => {
    const w = await seedWorld(modules);
    const admin = as(w.t, "admin");
    await insertProgramme(w, "published", w.admin, { profileId: "already_there" });
    const huge = await insertProgramme(w, "validated", w.admin, {
      profileId: "too_long_a_name",
      name: "x".repeat(MAX_CATALOG_PAYLOAD_BYTES),
    });
    expect(
      String(await failure(admin.mutation(api.programmes.publish, { programmeId: huge }))),
    ).toMatch(/programme_catalog_too_large/);
    expect(await programme(w, huge)).toMatchObject({ status: "validated", revision: 0 });
    expect(
      await w.t.run((ctx) => ctx.db.query("programme_revisions").collect()),
    ).toEqual([]);
  });
});

describe("ANH-137 EX-3 EX-4 transitions", () => {
  it("refuses a transition from any other state", async () => {
    const w = await seedWorld(modules);
    const admin = as(w.t, "admin");
    const draft = await insertProgramme(w, "draft", w.admin);
    const retired = await insertProgramme(w, "retired", w.admin, {
      profileId: "jog_retired",
    });
    for (const call of [
      admin.mutation(api.programmes.publish, { programmeId: draft }),
      admin.mutation(api.programmes.retire, { programmeId: draft }),
      admin.mutation(api.programmes.validate, {
        programmeId: retired,
        medicalSignoff: SIGNOFF,
      }),
      admin.mutation(api.programmes.updateDraft, { programmeId: retired, ...JOG }),
    ])
      expect(String(await failure(call))).toMatch(/programme_wrong_status/);
    await w.t.run((ctx) => ctx.db.delete(draft));
    expect(
      String(
        await failure(admin.mutation(api.programmes.publish, { programmeId: draft })),
      ),
    ).toMatch(/programme_not_found/);
  });

  it("lets nobody validate a draft they last wrote", async () => {
    const w = await seedWorld(modules);
    const programmeId = await insertProgramme(w, "draft", await addSecondAdmin(w));
    await as(w.t, "admin").mutation(api.programmes.updateDraft, {
      programmeId,
      ...JOG,
      name: "jog rewritten",
    });
    expect(
      String(
        await failure(
          as(w.t, "admin").mutation(api.programmes.validate, {
            programmeId,
            medicalSignoff: SIGNOFF,
          }),
        ),
      ),
    ).toMatch(/programme_self_validation/);
  });

  it("lets its creator not validate a draft another admin rewrote, nor anyone while the rewrite cleared its report", async () => {
    const w = await seedWorld(modules);
    const programmeId = await insertProgramme(w, "draft", w.admin);
    await addSecondAdmin(w);
    await asSecondAdmin(w).mutation(api.programmes.updateDraft, {
      programmeId,
      ...JOG,
    });
    const validate = () =>
      failure(
        as(w.t, "admin").mutation(api.programmes.validate, {
          programmeId,
          medicalSignoff: SIGNOFF,
        }),
      );
    expect(String(await validate())).toMatch(/programme_self_validation/);
    await w.t.run((ctx) => ctx.db.patch(programmeId, { createdBy: w.orgAdmin, updatedBy: w.orgAdmin }));
    expect(String(await validate())).toMatch(/programme_simulation_report_missing/);
  });

  it("validates only for the current medical parameters version, and says when none is registered", async () => {
    const w = await seedWorld(modules);
    const programmeId = await insertProgramme(w, "draft", await addSecondAdmin(w));
    const validate = () =>
      failure(
        as(w.t, "admin").mutation(api.programmes.validate, {
          programmeId,
          medicalSignoff: SIGNOFF,
        }),
      );
    await w.t.run((ctx) => ctx.db.patch(programmeId, { medicalParametersVersion: "0.0.9" }));
    expect(String(await validate())).toMatch(/programme_medical_version_not_current/);
    await w.t.run(async (ctx) => {
      for (const row of await ctx.db.query("medical_parameters_versions").collect())
        await ctx.db.delete(row._id);
    });
    expect(String(await validate())).toMatch(/programme_medical_version_unavailable/);
  });

  it("re-runs the Pi's validation on the stored draft", async () => {
    const w = await seedWorld(modules);
    const programmeId = await insertProgramme(w, "draft", await addSecondAdmin(w), {
      zoneHighBpm: 158,
    });
    const error = await failure(
      as(w.t, "admin").mutation(api.programmes.validate, {
        programmeId,
        medicalSignoff: SIGNOFF,
      }),
    );
    expect((error as ConvexError<{ code: string }>).data.code).toBe("programme_refused");
  });

  it.each([
    { by: " ", at: SIGNOFF.at, note: "" },
    { by: "x".repeat(201), at: SIGNOFF.at, note: "" },
    { by: "Dr", at: SIGNOFF.at, note: "x".repeat(1001) },
    { by: "Dr", at: 1.5, note: "" },
    { by: "Dr", at: Date.UTC(2023, 11, 31), note: "" },
    { by: "Dr", at: NOW + 86_400_001, note: "" },
  ])("refuses the medical sign-off %o", async (medicalSignoff) => {
    const w = await seedWorld(modules);
    const programmeId = await insertProgramme(w, "draft", await addSecondAdmin(w));
    expect(
      String(
        await failure(
          as(w.t, "admin").mutation(api.programmes.validate, {
            programmeId,
            medicalSignoff,
          }),
        ),
      ),
    ).toMatch(/invalid_medical_signoff/);
  });

  it("keeps one programme per profile id", async () => {
    const w = await seedWorld(modules);
    const admin = as(w.t, "admin");
    const first = await admin.mutation(api.programmes.create, JOG);
    const second = await admin.mutation(api.programmes.create, {
      ...JOG,
      profileId: "jog_copy",
    });
    expect(String(await failure(admin.mutation(api.programmes.create, JOG)))).toMatch(
      /programme_id_taken/,
    );
    expect(
      String(
        await failure(
          admin.mutation(api.programmes.updateDraft, { programmeId: second, ...JOG }),
        ),
      ),
    ).toMatch(/programme_id_taken/);
    await admin.mutation(api.programmes.updateDraft, { programmeId: first, ...JOG });
  });

  it.each([
    ["a text file", "text/plain", 12, 0],
    ["a file over 10 MiB", "application/pdf", 10 * 1024 * 1024 + 1, 0],
    ["a file uploaded over an hour ago", "application/json", 12, 3_600_001],
  ])("refuses %s as the simulation report", async (_name, type, size, age) => {
    const w = await seedWorld(modules);
    const programmeId = await insertProgramme(w, "draft", w.admin);
    const storageId = await registryUpload(w, type, size);
    vi.setSystemTime(NOW + age);
    expect(
      String(
        await failure(
          as(w.t, "admin").mutation(api.programmes.attachSimulationReport, {
            programmeId,
            storageId,
          }),
        ),
      ),
    ).toMatch(/invalid_registry_file/);
  });

  it("retires a published programme out of the catalogue, but not out of the history", async () => {
    const w = await seedWorld(modules);
    const programmeId = await insertProgramme(w, "published", w.admin);
    await as(w.t, "admin").mutation(api.programmes.retire, { programmeId });
    expect(await as(w.t, "patient").query(api.programmes.list, {})).toEqual([]);
    expect((await as(w.t, "admin").query(api.programmes.list, {}))[0]).toMatchObject({
      status: "retired",
    });
  });
});

/// <reference types="vite/client" />
/**
 * ANH-208: `users.assignPatientsToGestionnaire` sets the exact list of
 * patients one gestionnaire manages by writing only the difference with the
 * links that exist.
 *
 * The role cells (who may call it) live in `authorization.matrix.ts`, and the
 * separation of the organisations in `organizationIsolation.test.ts`. This
 * suite checks the effect on `user_gestionnaires`, through the registered
 * mutation and the in-memory tables: a row that is still wanted is the same
 * row after the save, with its identifier, its author and its date.
 */
import { describe, expect, it } from "vitest";
import { api } from "./_generated/api";
import type { Doc, Id } from "./_generated/dataModel";
import {
  as,
  modules,
  NOW,
  seedWorld,
  type Actor,
  type World,
} from "./test.setup";

type Link = Doc<"user_gestionnaires">;

async function links(w: World): Promise<Link[]> {
  return await w.t.run((ctx) => ctx.db.query("user_gestionnaires").collect());
}

function linkOf(
  rows: Link[],
  userId: Id<"users">,
  gestionnaireId: Id<"users">,
): Link | undefined {
  const found = rows.filter(
    (r) => r.userId === userId && r.gestionnaireId === gestionnaireId,
  );
  expect(found.length).toBeLessThanOrEqual(1);
  return found[0];
}

/** The patients `gestionnaireId` is linked to, in the order of the table. */
function patientsOf(rows: Link[], gestionnaireId: Id<"users">): Id<"users">[] {
  return rows
    .filter((r) => r.gestionnaireId === gestionnaireId)
    .map((r) => r.userId);
}

/** Every link that does not belong to `gestionnaireId`, in a stable order. */
function linksOfOthers(rows: Link[], gestionnaireId: Id<"users">): Link[] {
  return rows
    .filter((r) => r.gestionnaireId !== gestionnaireId)
    .sort((a, b) => a._id.localeCompare(b._id));
}

/** `manager` also manages `stranger`, since a link the Anheart admin made earlier. */
async function alsoManagesStranger(w: World): Promise<void> {
  await w.t.run((ctx) =>
    ctx.db.insert("user_gestionnaires", {
      organizationId: w.orgA,
      userId: w.stranger,
      gestionnaireId: w.manager,
      createdAt: NOW,
      createdBy: w.admin,
    }),
  );
}

/** The identifier of an account that no longer exists. */
async function removedAccount(w: World): Promise<Id<"users">> {
  return await w.t.run(async (ctx) => {
    const id = await ctx.db.insert("users", {
      clerkId: "removed-account",
      role: "user" as const,
      organizationId: w.orgA,
      firstName: "Removed",
      lastName: "Synthetic",
      email: "removed-account@example.invalid",
      language: "en" as const,
      createdAt: NOW,
    });
    await ctx.db.delete(id);
    return id;
  });
}

function setPatients(
  w: World,
  patientIds: Id<"users">[],
  {
    actor = "orgAdmin",
    gestionnaireId = w.manager,
  }: {
    actor?: Actor;
    gestionnaireId?: Id<"users">;
  } = {},
) {
  return as(w.t, actor).mutation(api.users.assignPatientsToGestionnaire, {
    gestionnaireId,
    patientIds,
  });
}

describe("ANH-208 users.assignPatientsToGestionnaire", () => {
  it("adds a patient and leaves the link that is already there as it is", async () => {
    // Given `manager` manages `patient` only, since the seed.
    const w = await seedWorld(modules);
    const before = await links(w);
    const kept = linkOf(before, w.patient, w.manager);
    expect(kept).toMatchObject({ createdAt: NOW, createdBy: w.admin });

    // When the admin of the centre ticks `stranger` as well.
    const result = await setPatients(w, [w.patient, w.stranger]);

    // Then one link is new, written in the centre by that admin...
    const after = await links(w);
    expect(result).toEqual({ added: 1, removed: 0 });
    expect(after).toHaveLength(before.length + 1);
    expect(linkOf(after, w.stranger, w.manager)).toMatchObject({
      organizationId: w.orgA,
      createdBy: w.orgAdmin,
    });
    // ... the link to `patient` is the very same row, not a new one...
    expect(linkOf(after, w.patient, w.manager)).toEqual(kept);
    // ... and nobody else's row moved.
    expect(linksOfOthers(after, w.manager)).toEqual(
      linksOfOthers(before, w.manager),
    );
  });

  it("removes the patient who is no longer sent and nothing else", async () => {
    // Given `manager` manages `patient` and `stranger`.
    const w = await seedWorld(modules);
    await alsoManagesStranger(w);
    const before = await links(w);
    const kept = linkOf(before, w.stranger, w.manager);

    // When the admin unticks `patient`.
    const result = await setPatients(w, [w.stranger]);

    // Then that link alone is gone.
    const after = await links(w);
    expect(result).toEqual({ added: 0, removed: 1 });
    expect(linkOf(after, w.patient, w.manager)).toBeUndefined();
    expect(linkOf(after, w.stranger, w.manager)).toEqual(kept);
    expect(after).toHaveLength(before.length - 1);
    expect(linksOfOthers(after, w.manager)).toEqual(
      linksOfOthers(before, w.manager),
    );
  });

  it("adds and removes in one save, without rewriting the link both lists hold", async () => {
    // Given `manager` manages `patient` and `stranger`.
    const w = await seedWorld(modules);
    await alsoManagesStranger(w);
    const kept = linkOf(await links(w), w.stranger, w.manager);

    // When the admin unticks `patient` and ticks `otherPatient`.
    const result = await setPatients(w, [w.stranger, w.otherPatient]);

    // Then the gestionnaire manages exactly the ticked patients.
    const after = await links(w);
    expect(result).toEqual({ added: 1, removed: 1 });
    expect(patientsOf(after, w.manager)).toEqual([w.stranger, w.otherPatient]);
    expect(linkOf(after, w.stranger, w.manager)).toEqual(kept);
    // `otherPatient` keeps the gestionnaire they already had.
    expect(linkOf(after, w.otherPatient, w.otherManager)).toBeDefined();
  });

  it("writes nothing when the list sent is the list the gestionnaire has", async () => {
    const w = await seedWorld(modules);
    await alsoManagesStranger(w);
    const before = await links(w);

    // The same patients, in another order.
    const result = await setPatients(w, [w.stranger, w.patient]);

    // Every row is the row it was: none deleted and written again.
    expect(result).toEqual({ added: 0, removed: 0 });
    expect(await links(w)).toEqual(before);
  });

  it("removes every link of the gestionnaire when an empty list is sent on purpose", async () => {
    const w = await seedWorld(modules);
    await alsoManagesStranger(w);
    const before = await links(w);

    const result = await setPatients(w, []);

    const after = await links(w);
    expect(result).toEqual({ added: 0, removed: 2 });
    expect(patientsOf(after, w.manager)).toEqual([]);
    expect(linksOfOthers(after, w.manager)).toEqual(
      linksOfOthers(before, w.manager),
    );
  });

  it("links a patient once when the identifier is sent twice", async () => {
    const w = await seedWorld(modules);

    const result = await setPatients(w, [
      w.patient,
      w.patient,
      w.stranger,
      w.stranger,
    ]);

    const after = await links(w);
    expect(result).toEqual({ added: 1, removed: 0 });
    expect(patientsOf(after, w.manager)).toEqual([w.patient, w.stranger]);
  });

  it.each(["orgAdmin", "admin"] as const)(
    "leaves out a patient of another organisation exactly as an account that does not exist (%s)",
    async (actor) => {
      // Given two worlds that differ only by the identifier sent: a patient
      // of centre B, or an account that was removed.
      const foreign = await seedWorld(modules);
      const unknown = await seedWorld(modules);
      const gone = await removedAccount(unknown);
      const foreignBefore = await links(foreign);
      const unknownBefore = await links(unknown);

      // When each is sent with the patient `manager` already has.
      const toForeign = await setPatients(
        foreign,
        [foreign.patient, foreign.orgBPatient],
        { actor },
      );
      const toUnknown = await setPatients(unknown, [unknown.patient, gone], {
        actor,
      });

      // Then the two answers are the same, and no link was written in either.
      expect(toForeign).toEqual({ added: 0, removed: 0 });
      expect(toUnknown).toEqual(toForeign);
      const foreignAfter = await links(foreign);
      expect(foreignAfter).toEqual(foreignBefore);
      expect(await links(unknown)).toEqual(unknownBefore);
      expect(
        linkOf(foreignAfter, foreign.orgBPatient, foreign.manager),
      ).toBeUndefined();
      // Centre B's own link is the row it was.
      expect(
        linkOf(foreignAfter, foreign.orgBPatient, foreign.orgBManager),
      ).toEqual(
        linkOf(foreignBefore, foreign.orgBPatient, foreign.orgBManager),
      );
    },
  );

  it("leaves out an account of the organisation that is not a patient", async () => {
    const w = await seedWorld(modules);
    const before = await links(w);

    const result = await setPatients(w, [
      w.patient,
      w.otherManager,
      w.orgAdmin,
    ]);

    expect(result).toEqual({ added: 0, removed: 0 });
    expect(await links(w)).toEqual(before);
  });

  it("answers the admin of another organisation as it answers for a gestionnaire that does not exist, and writes nothing", async () => {
    // Given `manager` is a gestionnaire of centre A, and an account that was removed.
    const w = await seedWorld(modules);
    const gone = await removedAccount(w);
    const before = await links(w);
    const refusal = (gestionnaireId: Id<"users">) =>
      setPatients(w, [w.orgBPatient], { actor: "orgBAdmin", gestionnaireId })
        .then(() => "accepted")
        .catch((error: Error) => error.message);

    // When the admin of centre B names each of them.
    const foreign = await refusal(w.manager);
    const unknown = await refusal(gone);

    // Then the refusal is word for word the same, and no link moved.
    expect(foreign).toMatch(/Gestionnaire not found/);
    expect(unknown).toBe(foreign);
    expect(await links(w)).toEqual(before);
  });

  it("lets the last of two saves set the list, and never rewrites a link both of them hold", async () => {
    // Given two admins who both opened the window on `manager`'s one patient.
    const w = await seedWorld(modules);
    const kept = linkOf(await links(w), w.patient, w.manager);

    // When the admin of the centre adds `stranger`, then the Anheart admin,
    // whose window still showed `patient` alone, adds `otherPatient`.
    const first = await setPatients(w, [w.patient, w.stranger]);
    const second = await setPatients(w, [w.patient, w.otherPatient], {
      actor: "admin",
    });

    // Then the list is the one of the second save: the patient the first
    // admin added is removed by it.
    const after = await links(w);
    expect(first).toEqual({ added: 1, removed: 0 });
    expect(second).toEqual({ added: 1, removed: 1 });
    expect(patientsOf(after, w.manager)).toEqual([w.patient, w.otherPatient]);
    // The link both lists hold was written by neither save.
    expect(linkOf(after, w.patient, w.manager)).toEqual(kept);
  });
});

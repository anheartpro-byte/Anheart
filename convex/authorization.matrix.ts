/// <reference types="vite/client" />
/**
 * ANH-132 authorization matrix: the intended policy of every PUBLIC Convex
 * function, one row per role and - where access depends on ownership or
 * assignment - one row per side (their own resource vs someone else's).
 *
 * This is the single place a reviewer reads to see the whole policy. The
 * `convex/authorization.matrix.test.ts` suite iterates it and checks behaviour
 * (the effect or the returned data), never the implementation. The policy is
 * derived from `docs/convex.md` section 3 and the helpers in
 * `convex/lib/auth.ts`, not from whatever each function happens to do.
 *
 * Roles that exist today: "admin", "gestionnaire", "user", plus the anonymous
 * caller. There is no organisation dimension yet (ANH-114). The named actors
 * below carry both role and relationship so an organisation column can be added
 * later as more named actors without rewriting the table.
 *
 * Error codes: the code has NO stable numeric/string error codes. Training
 * functions throw `ConvexError(message)`; users/machines/sessions/ecg throw
 * `Error(message)`. Refusal cells therefore assert that the call is rejected
 * and, where the exact reason matters, match an English message substring.
 *
 * The file name has two dots so the Convex bundler never deploys it.
 */
import { api } from "./_generated/api";
import type { FunctionReference } from "convex/server";
import type { Id } from "./_generated/dataModel";
import {
  addSession,
  NOW,
  type Actor,
  type Claims,
  type World,
} from "./test.setup";

export type Scope = "self" | "own" | "other" | "none";

export type Expectation =
  | { outcome: "refuse"; message?: RegExp }
  | { outcome: "success" }
  | { outcome: "empty" } // query returns null or [] (no access, nothing leaked)
  | { outcome: "filtered" }; // returns data but excludes other actors' records

export type Case = {
  actor: Actor;
  scope?: Scope;
  expect: Expectation;
  note: string;
  /**
   * Harmless known defect: today the behaviour differs from the intended
   * policy, but it exposes nothing. The test asserts the INTENDED outcome and
   * is marked `it.fails`, so it passes while the defect stands and fails loudly
   * the day the behaviour is corrected.
   */
  knownDefect?: { intended: Expectation; ticket: string };
};

export type Entry = {
  id: string;
  ref: FunctionReference<"query" | "mutation", "public">;
  kind: "query" | "mutation";
  cases: Case[];
  build: (
    w: World,
    actor: Actor,
    scope: Scope,
  ) => Promise<Record<string, unknown>>;
  /**
   * JWT claims the cell adds to its actor's identity. Every cell acts as its
   * own actor (subject = actor); a function whose rule reads claims (e.g. the
   * caller's verified email) supplies them here. Claims never replace the
   * subject, and the anonymous actor carries none: `as` refuses both.
   */
  claims?: (w: World, actor: Actor, scope: Scope) => Claims | null;
  /** Assert the effect/returned data for a "success" cell (behaviour, not code). */
  onSuccess?: (
    res: unknown,
    w: World,
    actor: Actor,
    scope: Scope,
    args: Record<string, unknown>,
  ) => Promise<void> | void;
  /** Assert that a "filtered" result includes the actor's own data and excludes others'. */
  onFiltered?: (
    res: unknown,
    w: World,
    actor: Actor,
    scope: Scope,
  ) => Promise<void> | void;
};

const refuse = (message?: RegExp): Expectation => ({
  outcome: "refuse",
  message,
});
const ok: Expectation = { outcome: "success" };
const empty: Expectation = { outcome: "empty" };
const filtered: Expectation = { outcome: "filtered" };

const NOT_AUTH = /Not authenticated/;
const UNAUTHORIZED = /Unauthorized/;

function userId(w: World, actor: Actor): Id<"users"> {
  const map: Partial<Record<Actor, Id<"users">>> = {
    admin: w.admin,
    manager: w.manager,
    otherManager: w.otherManager,
    patient: w.patient,
    otherPatient: w.otherPatient,
    stranger: w.stranger,
  };
  const id = map[actor];
  if (!id) throw new Error(`No user id for actor ${actor}`);
  return id;
}

function asArray(res: unknown): unknown[] {
  if (!Array.isArray(res)) throw new Error("Expected an array result");
  return res;
}

function ids(res: unknown): string[] {
  return asArray(res).map((row) => String((row as { _id: unknown })._id));
}

async function recentEcgBatch(w: World, sessionId: Id<"sessions">) {
  await w.t.run((ctx) =>
    ctx.db.insert("ecg_data", {
      sessionId,
      timestamp: Date.now() - 2000,
      sampleRate: 250,
      samples: [{ channel: "ECG", values: [1, 2, 3], unit: "mV" }],
    }),
  );
}

export const MATRIX: Entry[] = [
  // =========================================================================
  // users.ts
  // =========================================================================
  {
    id: "users.getOrCreateUser",
    ref: api.users.getOrCreateUser,
    kind: "mutation",
    build: async () => ({}),
    onSuccess: async (res, w, actor) => {
      const row = await w.t.run((ctx) => ctx.db.get(res as Id<"users">));
      if (row?.clerkId !== actor)
        throw new Error("Did not return the caller's own account");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "admin", expect: ok, note: "returns the existing row" },
      { actor: "manager", expect: ok, note: "returns the existing row" },
      { actor: "patient", expect: ok, note: "returns the existing row" },
    ],
  },
  {
    id: "users.getCurrentUser",
    ref: api.users.getCurrentUser,
    kind: "query",
    build: async () => ({}),
    onSuccess: (res, w, actor) => {
      if (String((res as { _id: Id<"users"> })._id) !== String(userId(w, actor)))
        throw new Error("Did not return the caller");
    },
    cases: [
      {
        actor: "anonymous",
        expect: empty,
        note: "null for an anonymous caller (does not throw)",
      },
      { actor: "admin", expect: ok, note: "own account" },
      { actor: "manager", expect: ok, note: "own account" },
      { actor: "patient", expect: ok, note: "own account" },
    ],
  },
  {
    id: "users.updateUserRole",
    ref: api.users.updateUserRole,
    kind: "mutation",
    build: async (w) => ({ userId: w.stranger, role: "gestionnaire" }),
    onSuccess: async (_res, w) => {
      const row = await w.t.run((ctx) => ctx.db.get(w.stranger));
      if (row?.role !== "gestionnaire") throw new Error("Role not changed");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      { actor: "manager", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      { actor: "admin", expect: ok, note: "admin promotes a user" },
    ],
  },
  {
    id: "users.updateUserProfile",
    ref: api.users.updateUserProfile,
    kind: "mutation",
    build: async () => ({ firstName: "Renamed" }),
    onSuccess: async (_res, w, actor) => {
      const row = await w.t.run((ctx) => ctx.db.get(userId(w, actor)));
      if (row?.firstName !== "Renamed")
        throw new Error("Own profile not updated");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "admin", expect: ok, note: "edits own profile" },
      { actor: "manager", expect: ok, note: "edits own profile" },
      { actor: "patient", expect: ok, note: "edits own profile" },
    ],
  },
  {
    id: "users.listUsers",
    ref: api.users.listUsers,
    kind: "query",
    build: async () => ({}),
    onSuccess: (res, w) => {
      if (!ids(res).includes(String(w.otherPatient)))
        throw new Error("Admin should see every account");
    },
    onFiltered: (res, w, actor) => {
      const seen = ids(res);
      if (actor === "manager") {
        if (!seen.includes(String(w.patient)))
          throw new Error("Manager must see their patient");
        if (seen.includes(String(w.otherPatient)) || seen.includes(String(w.stranger)))
          throw new Error("Manager must not see unrelated users");
      } else {
        // user: only themselves
        if (seen.length !== 1 || seen[0] !== String(userId(w, actor)))
          throw new Error("A user must see only themselves");
      }
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "admin", expect: ok, note: "every account" },
      { actor: "manager", expect: filtered, note: "only their patients" },
      { actor: "patient", expect: filtered, scope: "self", note: "only self" },
    ],
  },
  {
    id: "users.createPatient",
    ref: api.users.createPatient,
    kind: "mutation",
    build: async (_w, actor) => ({
      firstName: "New",
      lastName: "Patient",
      email: `np-${actor}@example.invalid`,
    }),
    onSuccess: async (res, w) => {
      const row = await w.t.run((ctx) => ctx.db.get(res as Id<"users">));
      if (row?.role !== "user") throw new Error("Did not create a patient");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "a user cannot create patients" },
      { actor: "manager", expect: ok, note: "manager creates and self-links" },
      { actor: "admin", expect: ok, note: "admin creates" },
    ],
  },
  {
    id: "users.updatePatient",
    ref: api.users.updatePatient,
    kind: "mutation",
    build: async (w) => ({ userId: w.patient, firstName: "Edited" }),
    onSuccess: async (_res, w) => {
      const row = await w.t.run((ctx) => ctx.db.get(w.patient));
      if (row?.firstName !== "Edited") throw new Error("Patient not updated");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "a user cannot edit patients" },
      { actor: "manager", scope: "own", expect: ok, note: "manages this patient" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/Not authorized to edit this patient/),
        note: "does not manage this patient",
      },
      { actor: "admin", expect: ok, note: "admin edits anyone" },
    ],
  },
  {
    id: "users.getUserById",
    ref: api.users.getUserById,
    kind: "query",
    build: async (w, actor, scope) => ({
      userId: scope === "self" ? userId(w, actor) : w.patient,
    }),
    onSuccess: (res, w, actor, scope) => {
      const expected = scope === "self" ? userId(w, actor) : w.patient;
      if (String((res as { _id: Id<"users"> })._id) !== String(expected))
        throw new Error("Wrong user returned");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", scope: "self", expect: ok, note: "own profile" },
      {
        actor: "stranger",
        scope: "other",
        expect: empty,
        note: "null for an unrelated user",
      },
      { actor: "manager", scope: "own", expect: ok, note: "manages this patient" },
      {
        actor: "otherManager",
        scope: "other",
        expect: empty,
        note: "null for a patient they do not manage",
      },
      { actor: "admin", scope: "other", expect: ok, note: "admin reads anyone" },
    ],
  },
  {
    id: "users.deleteUser",
    ref: api.users.deleteUser,
    kind: "mutation",
    build: async (w, actor) => ({
      userId: actor === "admin" || actor === "patient" ? w.stranger : w.patient,
    }),
    onSuccess: async (_res, w, actor) => {
      const target = actor === "admin" ? w.stranger : w.patient;
      const row = await w.t.run((ctx) => ctx.db.get(target));
      if (row !== null) throw new Error("User not deleted");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      {
        actor: "patient",
        expect: refuse(/Not authorized to delete users/),
        note: "a user cannot delete anyone",
      },
      { actor: "manager", scope: "own", expect: ok, note: "deletes their own patient" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/Not authorized to delete this user/),
        note: "not this patient's manager",
      },
      { actor: "admin", scope: "other", expect: ok, note: "admin deletes a user" },
    ],
  },
  {
    id: "users.linkPatientToClerk",
    ref: api.users.linkPatientToClerk,
    kind: "mutation",
    // The record linked is the one whose address is the caller's verified email
    // (identity claims `email` and `email_verified`). The `email` argument is
    // not authoritative. Whenever nothing is linked the result is null. Each
    // cell adds the email claims to its actor's identity.
    build: async (w) => {
      await w.t.run((ctx) =>
        ctx.db.insert("users", {
          clerkId: "",
          role: "user" as const,
          firstName: "Pre",
          lastName: "Created",
          email: "link-target@example.invalid",
          language: "en" as const,
          createdAt: NOW,
        }),
      );
      return { email: "link-target@example.invalid" };
    },
    claims: (_w, actor, scope) => {
      if (actor === "anonymous") return null;
      // self: the caller's verified address is the record's; other: it is not.
      return {
        email:
          scope === "self"
            ? "link-target@example.invalid"
            : "link-other@example.invalid",
        emailVerified: true,
      };
    },
    onSuccess: async (res, w, actor) => {
      const row = await w.t.run((ctx) => ctx.db.get(res as Id<"users">));
      if (row?.email !== "link-target@example.invalid" || row.clerkId !== actor)
        throw new Error("Record not linked to the caller");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      {
        actor: "stranger",
        scope: "self",
        expect: ok,
        note: "links the record whose address is the caller's verified email",
      },
      {
        actor: "stranger",
        scope: "other",
        expect: empty,
        note: "null when the caller's verified email is not the record's, as when no record exists",
      },
    ],
  },
  {
    id: "users.assignGestionnaireToUser",
    ref: api.users.assignGestionnaireToUser,
    kind: "mutation",
    build: async (w) => ({
      userId: w.stranger,
      gestionnaireId: w.manager,
    }),
    onSuccess: async (_res, w) => {
      const rel = await w.t.run((ctx) =>
        ctx.db
          .query("user_gestionnaires")
          .withIndex("by_user_and_gestionnaire", (q) =>
            q.eq("userId", w.stranger).eq("gestionnaireId", w.manager),
          )
          .unique(),
      );
      if (!rel) throw new Error("Link not created");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "a user cannot assign" },
      {
        actor: "manager",
        scope: "self",
        expect: ok,
        note: "a gestionnaire may attach themselves (cross-patient isolation is ANH-114)",
      },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/can only assign themselves/),
        note: "a gestionnaire cannot assign another gestionnaire",
      },
      { actor: "admin", expect: ok, note: "admin assigns" },
    ],
  },
  {
    id: "users.removeGestionnaireFromUser",
    ref: api.users.removeGestionnaireFromUser,
    kind: "mutation",
    build: async (w, actor) => {
      if (actor === "admin")
        return { userId: w.otherPatient, gestionnaireId: w.otherManager };
      return { userId: w.patient, gestionnaireId: w.manager };
    },
    onSuccess: async (_res, w, actor) => {
      const target =
        actor === "admin"
          ? { userId: w.otherPatient, gestionnaireId: w.otherManager }
          : { userId: w.patient, gestionnaireId: w.manager };
      const rel = await w.t.run((ctx) =>
        ctx.db
          .query("user_gestionnaires")
          .withIndex("by_user_and_gestionnaire", (q) =>
            q
              .eq("userId", target.userId)
              .eq("gestionnaireId", target.gestionnaireId),
          )
          .unique(),
      );
      if (rel) throw new Error("Link not removed");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "a user cannot remove" },
      { actor: "manager", scope: "self", expect: ok, note: "removes themselves" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/can only remove themselves/),
        note: "cannot remove another gestionnaire",
      },
      { actor: "admin", expect: ok, note: "admin removes" },
    ],
  },
  {
    id: "users.getGestionnairesForPatient",
    ref: api.users.getGestionnairesForPatient,
    kind: "query",
    build: async (w, actor, scope) => ({
      userId: scope === "self" ? userId(w, actor) : w.patient,
    }),
    onSuccess: (res, w) => {
      if (!ids(res).includes(String(w.manager)))
        throw new Error("Expected the managing gestionnaire");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", scope: "self", expect: ok, note: "own gestionnaires" },
      { actor: "stranger", scope: "other", expect: empty, note: "[] for an unrelated patient" },
      { actor: "manager", scope: "own", expect: ok, note: "manages this patient" },
      { actor: "admin", scope: "other", expect: ok, note: "admin reads" },
    ],
  },
  {
    id: "users.listGestionnaires",
    ref: api.users.listGestionnaires,
    kind: "query",
    build: async () => ({}),
    onSuccess: (res, w) => {
      const seen = ids(res);
      if (!seen.includes(String(w.manager)) || !seen.includes(String(w.otherManager)))
        throw new Error("Admin should see every gestionnaire");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: empty, note: "[] for a non-admin" },
      { actor: "manager", expect: empty, note: "[] for a non-admin" },
      { actor: "admin", expect: ok, note: "all gestionnaires" },
    ],
  },
  {
    id: "users.assignPatientsToGestionnaire",
    ref: api.users.assignPatientsToGestionnaire,
    kind: "mutation",
    build: async (w) => ({ gestionnaireId: w.manager, patientIds: [w.stranger] }),
    onSuccess: async (_res, w) => {
      const rel = await w.t.run((ctx) =>
        ctx.db
          .query("user_gestionnaires")
          .withIndex("by_user_and_gestionnaire", (q) =>
            q.eq("userId", w.stranger).eq("gestionnaireId", w.manager),
          )
          .unique(),
      );
      if (!rel) throw new Error("Replacement assignment not applied");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      { actor: "manager", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      { actor: "admin", expect: ok, note: "admin replaces the list" },
    ],
  },
  {
    id: "users.getPatientsForGestionnaire",
    ref: api.users.getPatientsForGestionnaire,
    kind: "query",
    build: async (w, actor) =>
      actor === "manager" ? {} : { gestionnaireId: w.manager },
    onSuccess: (res, w) => {
      const seen = ids(res);
      if (!seen.includes(String(w.patient)) || seen.includes(String(w.otherPatient)))
        throw new Error("Expected only this gestionnaire's patients");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: empty, note: "[] for a user" },
      { actor: "manager", scope: "own", expect: ok, note: "own patients" },
      {
        actor: "otherManager",
        scope: "other",
        expect: empty,
        note: "[] when asking for another gestionnaire's patients",
      },
      { actor: "admin", scope: "other", expect: ok, note: "admin reads any gestionnaire" },
    ],
  },

  // =========================================================================
  // machines.ts
  // =========================================================================
  {
    id: "machines.createMachine",
    ref: api.machines.createMachine,
    kind: "mutation",
    build: async () => ({ name: "New machine" }),
    onSuccess: async (res, w) => {
      const r = res as { machineId: Id<"machines">; apiKey: string };
      if (!r.apiKey.startsWith("anh1.")) throw new Error("No one-time key issued");
      const m = await w.t.run((ctx) => ctx.db.get(r.machineId));
      if (m?.status !== "offline") throw new Error("New machine should be offline");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      { actor: "manager", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      { actor: "admin", expect: ok, note: "admin creates" },
    ],
  },
  {
    id: "machines.assignMachineToGestionnaires",
    ref: api.machines.assignMachineToGestionnaires,
    kind: "mutation",
    build: async (w) => ({
      machineId: w.machine,
      gestionnaireIds: [w.otherManager],
    }),
    onSuccess: async (_res, w) => {
      const rel = await w.t.run((ctx) =>
        ctx.db
          .query("machine_gestionnaires")
          .withIndex("by_machine_and_gestionnaire", (q) =>
            q.eq("machineId", w.machine).eq("gestionnaireId", w.otherManager),
          )
          .unique(),
      );
      if (!rel) throw new Error("Assignment not applied");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      { actor: "manager", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      { actor: "admin", expect: ok, note: "admin assigns" },
    ],
  },
  {
    id: "machines.setGestionnaireMachines",
    ref: api.machines.setGestionnaireMachines,
    kind: "mutation",
    // The caller asks that `manager` manage exactly `otherMachine`.
    build: async (w) => ({
      gestionnaireId: w.manager,
      machineIds: [w.otherMachine],
    }),
    onSuccess: async (_res, w) => {
      const rows = await w.t.run((ctx) =>
        ctx.db.query("machine_gestionnaires").collect(),
      );
      const linked = (machineId: Id<"machines">, gestionnaireId: Id<"users">) =>
        rows.some(
          (r) => r.machineId === machineId && r.gestionnaireId === gestionnaireId,
        );
      if (!linked(w.otherMachine, w.manager) || linked(w.machine, w.manager))
        throw new Error("List not applied to this gestionnaire");
      if (!linked(w.otherMachine, w.otherManager))
        throw new Error("Another gestionnaire's link was changed");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      {
        actor: "manager",
        scope: "self",
        expect: refuse(UNAUTHORIZED),
        note: "not an admin, even for their own list",
      },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(UNAUTHORIZED),
        note: "not an admin",
      },
      { actor: "admin", expect: ok, note: "admin sets the exact list" },
    ],
  },
  {
    id: "machines.regenerateApiKey",
    ref: api.machines.regenerateApiKey,
    kind: "mutation",
    build: async (w) => ({ machineId: w.machine }),
    onSuccess: (res) => {
      const r = res as { apiKey: string };
      if (!r.apiKey.startsWith("anh1."))
        throw new Error("No replacement key issued");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      {
        actor: "patient",
        expect: refuse(/Not authorized to manage this machine/),
        note: "a user cannot manage a machine",
      },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/Not authorized to manage this machine/),
        note: "does not manage this machine",
      },
      { actor: "admin", expect: ok, note: "admin regenerates" },
    ],
  },
  {
    id: "machines.getMachine",
    ref: api.machines.getMachine,
    kind: "query",
    build: async (w) => ({ machineId: w.machine }),
    onSuccess: (res, w) => {
      if (String((res as { _id: Id<"machines"> })._id) !== String(w.machine))
        throw new Error("Wrong machine returned");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: empty, note: "null for a user" },
      { actor: "stranger", expect: empty, note: "null for a user" },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      { actor: "otherManager", scope: "other", expect: empty, note: "null for an unrelated machine" },
      { actor: "admin", expect: ok, note: "admin reads" },
    ],
  },
  {
    id: "machines.listMachines",
    ref: api.machines.listMachines,
    kind: "query",
    build: async () => ({}),
    onSuccess: (res, w) => {
      const seen = ids(res);
      if (!seen.includes(String(w.machine)) || !seen.includes(String(w.otherMachine)))
        throw new Error("Admin should see every machine");
    },
    onFiltered: (res, w, actor) => {
      const seen = ids(res);
      const mine = actor === "manager" ? w.machine : w.otherMachine;
      const theirs = actor === "manager" ? w.otherMachine : w.machine;
      if (!seen.includes(String(mine)) || seen.includes(String(theirs)))
        throw new Error("A gestionnaire should see only their machines");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: empty, note: "[] for a user" },
      { actor: "manager", scope: "own", expect: filtered, note: "only their machines" },
      { actor: "otherManager", scope: "own", expect: filtered, note: "only their machines" },
      { actor: "admin", expect: ok, note: "every machine" },
    ],
  },
  {
    id: "machines.updateMachine",
    ref: api.machines.updateMachine,
    kind: "mutation",
    build: async (w) => ({ machineId: w.machine, location: "Lab" }),
    onSuccess: async (_res, w) => {
      const m = await w.t.run((ctx) => ctx.db.get(w.machine));
      if (m?.location !== "Lab") throw new Error("Machine not updated");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      {
        actor: "patient",
        expect: refuse(/Not authorized to manage this machine/),
        note: "a user cannot manage a machine",
      },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/Not authorized to manage this machine/),
        note: "does not manage this machine",
      },
      { actor: "admin", expect: ok, note: "admin updates" },
    ],
  },
  {
    id: "machines.deleteMachine",
    ref: api.machines.deleteMachine,
    kind: "mutation",
    build: async (w) => ({ machineId: w.machine }),
    onSuccess: async (_res, w) => {
      const m = await w.t.run((ctx) => ctx.db.get(w.machine));
      if (!m?.isDeleted) throw new Error("Machine not soft-deleted");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      {
        actor: "patient",
        expect: refuse(/Not authorized to manage this machine/),
        note: "a user cannot manage a machine",
      },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/Not authorized to manage this machine/),
        note: "does not manage this machine",
      },
      { actor: "admin", expect: ok, note: "admin deletes" },
    ],
  },
  {
    id: "machines.restoreMachine",
    ref: api.machines.restoreMachine,
    kind: "mutation",
    build: async (w) => {
      await w.t.run((ctx) =>
        ctx.db.patch(w.machine, { isDeleted: true, deletedAt: NOW }),
      );
      return { machineId: w.machine };
    },
    onSuccess: async (_res, w) => {
      const m = await w.t.run((ctx) => ctx.db.get(w.machine));
      if (m?.isDeleted) throw new Error("Machine not restored");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      {
        actor: "manager",
        scope: "own",
        expect: refuse(UNAUTHORIZED),
        note: "restore is admin-only, even for the machine's manager",
      },
      { actor: "admin", expect: ok, note: "admin restores" },
    ],
  },
  {
    id: "machines.getRecentHeartbeats",
    ref: api.machines.getRecentHeartbeats,
    kind: "query",
    build: async (w) => {
      await w.t.run((ctx) =>
        ctx.db.insert("machine_heartbeats", {
          machineId: w.machine,
          timestamp: NOW,
        }),
      );
      return { machineId: w.machine };
    },
    onSuccess: (res) => {
      if (asArray(res).length < 1) throw new Error("Expected heartbeat history");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: empty, note: "[] for a user" },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      { actor: "otherManager", scope: "other", expect: empty, note: "[] for an unrelated machine" },
      { actor: "admin", expect: ok, note: "admin reads" },
    ],
  },
  {
    id: "machines.assignGestionnaireToMachine",
    ref: api.machines.assignGestionnaireToMachine,
    kind: "mutation",
    build: async (w) => ({ machineId: w.machine, gestionnaireId: w.otherManager }),
    onSuccess: async (_res, w) => {
      const rel = await w.t.run((ctx) =>
        ctx.db
          .query("machine_gestionnaires")
          .withIndex("by_machine_and_gestionnaire", (q) =>
            q.eq("machineId", w.machine).eq("gestionnaireId", w.otherManager),
          )
          .unique(),
      );
      if (!rel) throw new Error("Assignment not applied");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "a user cannot assign" },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/Not authorized to manage this machine/),
        note: "does not manage this machine",
      },
      { actor: "admin", expect: ok, note: "admin assigns" },
    ],
  },
  {
    id: "machines.removeGestionnaireFromMachine",
    ref: api.machines.removeGestionnaireFromMachine,
    kind: "mutation",
    build: async (w, actor) => {
      // Add a second gestionnaire so a manager removal never hits the last-one
      // guard, but NOT for the otherManager case, which must stay unrelated to
      // this machine so its refusal is genuine.
      if (actor !== "otherManager")
        await w.t.run((ctx) =>
          ctx.db.insert("machine_gestionnaires", {
            machineId: w.machine,
            gestionnaireId: w.otherManager,
            isOwner: false,
            createdAt: NOW,
            createdBy: w.admin,
          }),
        );
      if (actor === "manager")
        return { machineId: w.machine, gestionnaireId: w.otherManager };
      return { machineId: w.machine, gestionnaireId: w.manager };
    },
    onSuccess: async (_res, w, actor) => {
      const removed = actor === "manager" ? w.otherManager : w.manager;
      const rel = await w.t.run((ctx) =>
        ctx.db
          .query("machine_gestionnaires")
          .withIndex("by_machine_and_gestionnaire", (q) =>
            q.eq("machineId", w.machine).eq("gestionnaireId", removed),
          )
          .unique(),
      );
      if (rel) throw new Error("Gestionnaire not removed");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "a user cannot remove" },
      { actor: "manager", scope: "own", expect: ok, note: "removes a co-gestionnaire" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/Not authorized to manage this machine/),
        note: "does not manage this machine (removing the owner)",
      },
      { actor: "admin", expect: ok, note: "admin removes" },
    ],
  },
  {
    id: "machines.getGestionnairesForMachine",
    ref: api.machines.getGestionnairesForMachine,
    kind: "query",
    build: async (w) => ({ machineId: w.machine }),
    onSuccess: (res, w) => {
      if (!ids(res).includes(String(w.manager)))
        throw new Error("Expected the machine's gestionnaire");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: empty, note: "[] for a user" },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      { actor: "otherManager", scope: "other", expect: empty, note: "[] for an unrelated machine" },
      { actor: "admin", expect: ok, note: "admin reads" },
    ],
  },
  {
    id: "machines.getMachinesForGestionnaire",
    ref: api.machines.getMachinesForGestionnaire,
    kind: "query",
    build: async (w, actor) =>
      actor === "manager" ? {} : { gestionnaireId: w.manager },
    onSuccess: (res, w) => {
      if (!ids(res).includes(String(w.machine)))
        throw new Error("Expected this gestionnaire's machine");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: empty, note: "[] for a user" },
      { actor: "manager", scope: "own", expect: ok, note: "own machines" },
      {
        actor: "otherManager",
        scope: "other",
        expect: empty,
        note: "[] when asking for another gestionnaire's machines",
      },
      { actor: "admin", scope: "other", expect: ok, note: "admin reads any gestionnaire" },
    ],
  },

  // =========================================================================
  // sessions.ts
  // =========================================================================
  {
    id: "sessions.createSession",
    ref: api.sessions.createSession,
    kind: "mutation",
    build: async (w, _actor, scope) => ({
      machineId: w.machine,
      userId: scope === "other" ? w.otherPatient : w.patient,
      channels: ["ECG"],
    }),
    onSuccess: async (res, w) => {
      const s = await w.t.run((ctx) => ctx.db.get(res as Id<"sessions">));
      if (s?.status !== "pending" || s.machineId !== w.machine)
        throw new Error("Recording session not created");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "a user cannot create sessions" },
      { actor: "manager", scope: "own", expect: ok, note: "manages machine and patient" },
      {
        actor: "manager",
        scope: "other",
        expect: refuse(/Not authorized to create session for this patient/),
        note: "manages the machine but not this rider",
      },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/Not authorized to use this machine/),
        note: "does not manage this machine",
      },
      { actor: "admin", expect: ok, note: "admin creates" },
    ],
  },
  {
    id: "sessions.endSession",
    ref: api.sessions.endSession,
    kind: "mutation",
    build: async (w) => {
      const sessionId = await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status: "active",
        kind: "recording",
      });
      return { sessionId };
    },
    onSuccess: async (_res, w) => {
      const rows = await w.t.run((ctx) =>
        ctx.db
          .query("sessions")
          .withIndex("by_machine", (q) => q.eq("machineId", w.machine))
          .collect(),
      );
      if (!rows.some((s) => s.status === "completed"))
        throw new Error("Session not ended");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "a user cannot end sessions" },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/Not authorized to manage this session/),
        note: "does not manage this machine",
      },
      { actor: "admin", expect: ok, note: "admin ends" },
    ],
  },
  {
    id: "sessions.getSession",
    ref: api.sessions.getSession,
    kind: "query",
    build: async (w) => {
      const sessionId = await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status: "active",
        kind: "auto",
      });
      return { sessionId };
    },
    onSuccess: (res, _w, _actor, _scope, args) => {
      if (
        String((res as { _id: Id<"sessions"> })._id) !==
        String((args as { sessionId: Id<"sessions"> }).sessionId)
      )
        throw new Error("Wrong session returned");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", scope: "own", expect: ok, note: "the rider" },
      { actor: "stranger", scope: "other", expect: empty, note: "null for an unrelated user" },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      { actor: "otherManager", scope: "other", expect: empty, note: "null for an unrelated machine" },
      { actor: "admin", expect: ok, note: "admin reads" },
    ],
  },
  {
    id: "sessions.listSessions",
    ref: api.sessions.listSessions,
    kind: "query",
    build: async (w, _actor, scope) => {
      await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status: "completed",
        kind: "auto",
      });
      await addSession(w, {
        machineId: w.otherMachine,
        userId: w.otherPatient,
        status: "completed",
        kind: "auto",
      });
      if (scope === "other") {
        // Three newer sessions for another rider push the caller's own out of a
        // small page: limit is applied BEFORE access filtering (docs section 5).
        for (let i = 0; i < 3; i++)
          await addSession(w, {
            machineId: w.otherMachine,
            userId: w.otherPatient,
            status: "completed",
            kind: "auto",
          });
        return { limit: 2 };
      }
      return {};
    },
    onSuccess: (res) => {
      if (asArray(res).length < 2) throw new Error("Admin should see all sessions");
    },
    onFiltered: (res, _w, actor, scope) => {
      const n = asArray(res).length;
      if (scope === "other") {
        // Intended policy: the caller still sees their own session.
        if (n !== 1) throw new Error("Caller should still see their own session");
        return;
      }
      if (actor === "manager" && n !== 1)
        throw new Error("Manager should see only their machine's session");
      if (actor === "patient" && n !== 1)
        throw new Error("A user should see only their own session");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "admin", expect: ok, note: "all sessions" },
      { actor: "manager", scope: "own", expect: filtered, note: "their machine's sessions" },
      { actor: "patient", scope: "own", expect: filtered, note: "their own sessions" },
      {
        actor: "patient",
        scope: "other",
        expect: empty,
        note: "limit applied before access filter hides the caller's own session",
        knownDefect: {
          intended: filtered,
          ticket: "harmless: under-returns, never exposes another user's data",
        },
      },
    ],
  },
  {
    id: "sessions.getActiveSessionForMachine",
    ref: api.sessions.getActiveSessionForMachine,
    kind: "query",
    build: async (w) => {
      await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status: "active",
        kind: "auto",
      });
      return { machineId: w.machine };
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: empty, note: "null for a user" },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      { actor: "otherManager", scope: "other", expect: empty, note: "null for an unrelated machine" },
      { actor: "admin", expect: ok, note: "admin reads" },
    ],
  },
  {
    id: "sessions.getCompletedSessionsForUser",
    ref: api.sessions.getCompletedSessionsForUser,
    kind: "query",
    // User-own and anonymous paths are already proven by sessions.test.ts
    // (ANH-71); here we cover the admin and gestionnaire roles it does not.
    build: async (w) => {
      await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status: "completed",
        kind: "auto",
      });
      await addSession(w, {
        machineId: w.otherMachine,
        userId: w.otherPatient,
        status: "completed",
        kind: "auto",
      });
      return {};
    },
    onSuccess: (res) => {
      if (asArray(res).length < 2)
        throw new Error("Admin should see all completed sessions");
    },
    onFiltered: (res, _w, actor) => {
      if (actor === "manager" && asArray(res).length !== 1)
        throw new Error("Manager should see only their machine's completed session");
    },
    cases: [
      { actor: "admin", expect: ok, note: "all completed sessions" },
      { actor: "manager", scope: "own", expect: filtered, note: "their machine's completed sessions" },
    ],
  },
  {
    id: "sessions.cancelSession",
    ref: api.sessions.cancelSession,
    kind: "mutation",
    build: async (w) => {
      const sessionId = await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status: "pending",
        kind: "recording",
      });
      return { sessionId };
    },
    onSuccess: async (_res, w, _actor, _scope, args) => {
      const s = await w.t.run((ctx) =>
        ctx.db.get((args as { sessionId: Id<"sessions"> }).sessionId),
      );
      if (s !== null) throw new Error("Pending session not cancelled");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "a user cannot cancel" },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/Not authorized to manage this session/),
        note: "does not manage this machine",
      },
      { actor: "admin", expect: ok, note: "admin cancels" },
    ],
  },

  // =========================================================================
  // training.ts  (live/telemetry masking detail is in trainingPrivacy.test.ts)
  // =========================================================================
  {
    id: "training.grantLaunchRight",
    ref: api.training.grantLaunchRight,
    kind: "mutation",
    build: async (w, actor, scope) => {
      if (actor === "manager" && scope === "own") {
        // A patient the manager manages but who has no right yet.
        await w.t.run((ctx) =>
          ctx.db.insert("user_gestionnaires", {
            userId: w.stranger,
            gestionnaireId: w.manager,
            createdAt: NOW,
            createdBy: w.admin,
          }),
        );
        return { machineId: w.machine, userId: w.stranger };
      }
      // manager/other: manages the machine but NOT this rider.
      if (actor === "manager")
        return { machineId: w.machine, userId: w.otherPatient };
      return { machineId: w.machine, userId: w.stranger };
    },
    onSuccess: async (_res, w) => {
      const row = await w.t.run((ctx) =>
        ctx.db
          .query("machine_user_permissions")
          .withIndex("by_machine_and_user", (q) =>
            q.eq("machineId", w.machine).eq("userId", w.stranger),
          )
          .unique(),
      );
      if (!row) throw new Error("Launch right not granted");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      {
        actor: "patient",
        expect: refuse(/Only an admin or a manager of this machine/),
        note: "a user cannot grant",
      },
      { actor: "manager", scope: "own", expect: ok, note: "manages machine and user" },
      {
        actor: "manager",
        scope: "other",
        expect: refuse(/do not manage this user/),
        note: "manages the machine but not this rider",
      },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/Only an admin or a manager of this machine/),
        note: "does not manage this machine",
      },
      { actor: "admin", expect: ok, note: "admin grants" },
    ],
  },
  {
    id: "training.revokeLaunchRight",
    ref: api.training.revokeLaunchRight,
    kind: "mutation",
    build: async (w) => ({ machineId: w.machine, userId: w.patient }),
    onSuccess: async (_res, w) => {
      const row = await w.t.run((ctx) =>
        ctx.db
          .query("machine_user_permissions")
          .withIndex("by_machine_and_user", (q) =>
            q.eq("machineId", w.machine).eq("userId", w.patient),
          )
          .unique(),
      );
      if (row) throw new Error("Launch right not revoked");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      {
        actor: "patient",
        expect: refuse(/Only an admin or a manager of this machine/),
        note: "a user cannot revoke",
      },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/Only an admin or a manager of this machine/),
        note: "does not manage this machine",
      },
      { actor: "admin", expect: ok, note: "admin revokes" },
    ],
  },
  {
    id: "training.listLaunchRights",
    ref: api.training.listLaunchRights,
    kind: "query",
    build: async (w) => ({ machineId: w.machine }),
    onSuccess: (res, w) => {
      const seen = asArray(res).map((r) => String((r as { userId: unknown }).userId));
      if (!seen.includes(String(w.patient)))
        throw new Error("Expected the rider holding the launch right");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: empty, note: "[] for a user" },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      { actor: "otherManager", scope: "other", expect: empty, note: "[] for an unrelated machine" },
      { actor: "admin", expect: ok, note: "admin reads" },
    ],
  },
  {
    id: "training.setUserPhysiology",
    ref: api.training.setUserPhysiology,
    kind: "mutation",
    build: async (w, actor, scope) => ({
      userId: scope === "self" ? userId(w, actor) : w.patient,
      hrMax: 175,
      birthYear: 1991,
    }),
    onSuccess: async (_res, w) => {
      const u = await w.t.run((ctx) => ctx.db.get(w.patient));
      if (u?.hrMax !== 175) throw new Error("Physiology not set");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      {
        actor: "patient",
        scope: "self",
        expect: refuse(/Only a manager can set physiology/),
        note: "a user may never set physiology, not even their own",
      },
      { actor: "manager", scope: "own", expect: ok, note: "manages this patient" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/do not manage this user/),
        note: "does not manage this patient",
      },
      { actor: "admin", scope: "other", expect: ok, note: "admin sets" },
    ],
  },
  {
    id: "training.listMachineProfiles",
    ref: api.training.listMachineProfiles,
    kind: "query",
    build: async (w) => ({ machineId: w.machine }),
    onSuccess: (res) => {
      if (asArray(res).length < 1) throw new Error("Expected the machine's profiles");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", scope: "own", expect: ok, note: "holds the launch right" },
      { actor: "stranger", expect: empty, note: "[] without access or a right" },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      { actor: "otherManager", scope: "other", expect: empty, note: "[] for an unrelated machine" },
      { actor: "admin", expect: ok, note: "admin reads" },
    ],
  },
  {
    id: "training.listLaunchableMachines",
    ref: api.training.listLaunchableMachines,
    kind: "query",
    build: async () => ({}),
    onSuccess: (res, w) => {
      if (!ids(res).includes(String(w.machine)) || !ids(res).includes(String(w.otherMachine)))
        throw new Error("Admin should see every machine");
    },
    onFiltered: (res, w, actor) => {
      const seen = ids(res);
      const mine = actor === "manager" ? w.machine : w.machine; // patient holds right on w.machine
      if (!seen.includes(String(mine)) || seen.includes(String(w.otherMachine)))
        throw new Error("Should list only machines the caller may launch on");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", scope: "own", expect: filtered, note: "machines they hold the right on" },
      { actor: "stranger", expect: empty, note: "[] without any launch right" },
      { actor: "manager", scope: "own", expect: filtered, note: "machines they manage" },
      { actor: "admin", expect: ok, note: "every machine" },
    ],
  },
  {
    id: "training.launchAutoSession",
    ref: api.training.launchAutoSession,
    kind: "mutation",
    build: async (w, actor, scope) => {
      const base = { machineId: w.machine, profileId: w.profileId };
      if (actor === "patient")
        return scope === "other" ? { ...base, userId: w.otherPatient } : base;
      if (actor === "stranger") return base; // for self, no right
      if (actor === "manager")
        return {
          ...base,
          userId: scope === "other" ? w.otherPatient : w.patient,
        };
      if (actor === "otherManager") return { ...base, userId: w.otherPatient };
      return { ...base, userId: w.patient }; // admin
    },
    onSuccess: async (res, w) => {
      const s = await w.t.run((ctx) => ctx.db.get(res as Id<"sessions">));
      if (s?.kind !== "auto" || s.status !== "pending" || s.userId !== w.patient)
        throw new Error("Auto session not queued for the intended rider");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", scope: "self", expect: ok, note: "launches for themselves with a right" },
      {
        actor: "patient",
        scope: "other",
        expect: refuse(/only launch a session for yourself/),
        note: "a user may not launch for someone else",
      },
      {
        actor: "stranger",
        scope: "self",
        expect: refuse(/have not been given the right/),
        note: "no launch right on the machine",
      },
      { actor: "manager", scope: "own", expect: ok, note: "launches for a managed rider" },
      {
        actor: "manager",
        scope: "other",
        expect: refuse(/Not authorized to launch a session for this rider/),
        note: "manages the machine but not the rider",
      },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/Not authorized to use this machine/),
        note: "does not manage this machine",
      },
      { actor: "admin", scope: "own", expect: ok, note: "admin launches for a rider" },
    ],
  },
  {
    id: "training.requestStop",
    ref: api.training.requestStop,
    kind: "mutation",
    build: async (w) => {
      const sessionId = await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status: "active",
        kind: "auto",
      });
      return { sessionId };
    },
    onSuccess: async (_res, w, _actor, _scope, args) => {
      const s = await w.t.run((ctx) =>
        ctx.db.get((args as { sessionId: Id<"sessions"> }).sessionId),
      );
      if (s?.stopRequestedAt === undefined && s?.status !== "failed")
        throw new Error("Stop was not requested");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", scope: "own", expect: ok, note: "the rider may stop" },
      {
        actor: "stranger",
        scope: "other",
        expect: refuse(/Not authorized to stop this session/),
        note: "not the rider and no machine access",
      },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/Not authorized to stop this session/),
        note: "does not manage this machine",
      },
      { actor: "admin", expect: ok, note: "admin stops" },
    ],
  },
  {
    id: "training.getMachineLive",
    ref: api.training.getMachineLive,
    kind: "query",
    // Content masking of `live` by role is proven by trainingPrivacy.test.ts;
    // here we assert only the access boundary (object vs null).
    build: async (w) => ({ machineId: w.machine }),
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", scope: "own", expect: ok, note: "holds the launch right" },
      { actor: "stranger", expect: empty, note: "null without access or a right" },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      { actor: "otherManager", scope: "other", expect: empty, note: "null for an unrelated machine" },
      { actor: "admin", expect: ok, note: "admin reads" },
    ],
  },
  {
    id: "training.getSessionTelemetry",
    ref: api.training.getSessionTelemetry,
    kind: "query",
    // Full role coverage for retain/deny is in trainingPrivacy.test.ts; here we
    // cover the access boundary with real telemetry rows.
    build: async (w) => {
      const sessionId = await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status: "active",
        kind: "auto",
      });
      await w.t.run((ctx) =>
        ctx.db.insert("training_telemetry", {
          sessionId,
          machineId: w.machine,
          t: NOW,
          elapsedS: 1,
          phase: "hold",
          bpm: 140,
          motorRpm: 1000,
          outputRpm: 20,
          setpointMotorRpm: 1000,
          gLoad: 1.1,
          safetyAction: "none",
        }),
      );
      return { sessionId };
    },
    onSuccess: (res) => {
      if (asArray(res).length < 1) throw new Error("Expected telemetry points");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", scope: "own", expect: ok, note: "the rider" },
      { actor: "stranger", scope: "other", expect: empty, note: "[] for an unrelated user" },
      { actor: "admin", expect: ok, note: "admin reads" },
    ],
  },
  {
    id: "training.getTrainingSession",
    ref: api.training.getTrainingSession,
    kind: "query",
    build: async (w) => {
      const sessionId = await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status: "active",
        kind: "auto",
      });
      return { sessionId };
    },
    onSuccess: (res, _w, _actor, _scope, args) => {
      if (
        String((res as { _id: Id<"sessions"> })._id) !==
        String((args as { sessionId: Id<"sessions"> }).sessionId)
      )
        throw new Error("Wrong session returned");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", scope: "own", expect: ok, note: "the rider" },
      { actor: "stranger", scope: "other", expect: empty, note: "null for an unrelated user" },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      { actor: "otherManager", scope: "other", expect: empty, note: "null for an unrelated machine" },
      { actor: "admin", expect: ok, note: "admin reads" },
    ],
  },

  // =========================================================================
  // ecgData.ts  (identical access rule across the five readers)
  // =========================================================================
  ...(
    [
      { id: "ecgData.getSessionAllData", ref: api.ecgData.getSessionAllData, extra: {} },
      { id: "ecgData.getRecentEcgData", ref: api.ecgData.getRecentEcgData, extra: {} },
      {
        id: "ecgData.getSessionEcgRange",
        ref: api.ecgData.getSessionEcgRange,
        extra: { startTime: () => Date.now() - 60000, endTime: () => Date.now() + 60000 },
      },
      { id: "ecgData.getSessionDataStats", ref: api.ecgData.getSessionDataStats, extra: {} },
      { id: "ecgData.getLatestEcgBatch", ref: api.ecgData.getLatestEcgBatch, extra: {} },
    ] as const
  ).map(
    (fn): Entry => ({
      id: fn.id,
      ref: fn.ref,
      kind: "query",
      build: async (w) => {
        const sessionId = await addSession(w, {
          machineId: w.machine,
          userId: w.patient,
          status: "completed",
          kind: "auto",
        });
        await recentEcgBatch(w, sessionId);
        const extra: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(fn.extra))
          extra[k] = (v as () => number)();
        return { sessionId, ...extra };
      },
      onSuccess: (res) => {
        // Stats returns an object with totalBatches; the others return arrays.
        if (Array.isArray(res)) {
          if (res.length < 1) throw new Error("Expected ECG data");
        } else if (res === null) {
          throw new Error("Expected ECG data");
        }
      },
      cases: [
        { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
        { actor: "patient", scope: "own", expect: ok, note: "the rider" },
        { actor: "stranger", scope: "other", expect: empty, note: "empty for an unrelated user" },
        { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
        { actor: "otherManager", scope: "other", expect: empty, note: "empty for an unrelated machine" },
        { actor: "admin", expect: ok, note: "admin reads" },
      ],
    }),
  ),

  // =========================================================================
  // sessionSummaries.ts
  // =========================================================================
  {
    id: "sessionSummaries.getSummary",
    ref: api.sessionSummaries.getSummary,
    kind: "query",
    build: summaryBuild,
    onSuccess: (res) => {
      if (res === null) throw new Error("Expected a summary");
    },
    cases: summaryCases(),
  },
  {
    id: "sessionSummaries.getSummaryWithEcg",
    ref: api.sessionSummaries.getSummaryWithEcg,
    kind: "query",
    build: summaryBuild,
    onSuccess: (res) => {
      if (res === null) throw new Error("Expected a summary");
    },
    // KNOWN HARMLESS DEFECT: the handler returns the raw document (including the
    // system field `_creationTime`), which its `returns` validator does not
    // declare, so Convex return validation throws for EVERY authorised reader
    // whenever a summary exists. It exposes nothing (the read simply errors).
    // The success cells assert the intended behaviour and run with `it.fails`.
    cases: summaryCases(true),
  },
];

function summaryBuild(w: World) {
  return (async () => {
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "completed",
      kind: "auto",
    });
    await w.t.run((ctx) =>
      ctx.db.insert("session_summaries", {
        sessionId,
        duration: 600,
        metrics: { avgHeartRate: 140, minHeartRate: 120, maxHeartRate: 160 },
        downsampledEcg: [{ timestamp: NOW, value: 1 }],
        createdAt: NOW,
      }),
    );
    return { sessionId };
  })();
}

function summaryCases(brokenReturn = false): Case[] {
  const defect = brokenReturn
    ? {
        knownDefect: {
          intended: ok,
          ticket:
            "harmless: getSummaryWithEcg returns the raw doc; its returns validator omits _creationTime, so the read throws for everyone",
        },
      }
    : {};
  return [
    { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
    { actor: "patient", scope: "own", expect: ok, note: "the rider", ...defect },
    { actor: "stranger", scope: "other", expect: empty, note: "null for an unrelated user" },
    { actor: "manager", scope: "own", expect: ok, note: "manages this machine", ...defect },
    { actor: "otherManager", scope: "other", expect: empty, note: "null for an unrelated machine" },
    { actor: "admin", expect: ok, note: "admin reads", ...defect },
  ];
}

/**
 * Coverage of the machine HTTP routes in `convex/http.ts`, used both to drive
 * `httpRoutes.test.ts` and to satisfy the completeness check. Authentication
 * refusals (missing header, malformed key, deleted/disabled machine,
 * regenerated key) across every route are proven by `machineAuth.test.ts`.
 */
export const ROUTE_COVERAGE: ReadonlyArray<{
  method: "GET" | "POST";
  path: string;
  tests: string;
}> = [
  { method: "POST", path: "/api/machine/heartbeat", tests: "machineAuth.test.ts; httpRoutes.test.ts" },
  { method: "GET", path: "/api/machine/session/poll", tests: "httpRoutes.test.ts (recording only)" },
  { method: "POST", path: "/api/machine/session/start", tests: "httpRoutes.test.ts" },
  { method: "POST", path: "/api/machine/session/end", tests: "httpRoutes.test.ts" },
  { method: "GET", path: "/api/machine/session/status", tests: "httpRoutes.test.ts" },
  { method: "POST", path: "/api/machine/data", tests: "httpRoutes.test.ts (body, timestamp, machine binding)" },
  { method: "GET", path: "/api/machine/training/poll", tests: "httpRoutes.test.ts (auto, this machine)" },
  { method: "GET", path: "/api/machine/roster", tests: "httpRoutes.test.ts" },
  { method: "POST", path: "/api/machine/profiles", tests: "httpRoutes.test.ts (malformed body)" },
  { method: "POST", path: "/api/machine/training/start", tests: "httpRoutes.test.ts (non-pending refused)" },
  { method: "POST", path: "/api/machine/training/local", tests: "httpRoutes.test.ts (idempotent)" },
  { method: "POST", path: "/api/machine/training/end", tests: "httpRoutes.test.ts (idempotent)" },
  { method: "GET", path: "/api/machine/training/status", tests: "httpRoutes.test.ts" },
  { method: "POST", path: "/api/machine/training/telemetry", tests: "httpRoutes.test.ts (other machine refused)" },
];

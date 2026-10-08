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
 * Roles: "admin" (admin of the Anheart organisation, every organisation),
 * "org_admin" (admin of one client organisation), "gestionnaire", "user", plus
 * the anonymous caller. The named actors carry organisation, role and
 * relationship (ANH-114): the world holds Anheart and two client centres, A and
 * B. Every entry has at least one cell where an actor of centre B aims at a
 * resource of centre A by direct identifier (scope "foreign") or lists from
 * centre B; `completeness.test.ts` fails if an entry has none.
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
  ROLE_OF,
  type Actor,
  type Claims,
  type World,
} from "./test.setup";

/**
 * - "self": the caller's own account or data.
 * - "own": a resource the caller is linked to, in their organisation.
 * - "other": a resource of the SAME organisation the caller is not linked to.
 * - "foreign": a resource of ANOTHER organisation, named by its identifier.
 */
export type Scope = "self" | "own" | "other" | "foreign" | "none";

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
    orgAdmin: w.orgAdmin,
    manager: w.manager,
    otherManager: w.otherManager,
    patient: w.patient,
    otherPatient: w.otherPatient,
    stranger: w.stranger,
    orgBAdmin: w.orgBAdmin,
    orgBManager: w.orgBManager,
    orgBPatient: w.orgBPatient,
  };
  const id = map[actor];
  if (!id) throw new Error(`No user id for actor ${actor}`);
  return id;
}

/** The organisation an actor belongs to. */
function organizationOf(w: World, actor: Actor): Id<"organizations"> {
  if (actor === "admin") return w.anheartOrg;
  return actor.startsWith("orgB") ? w.orgB : w.orgA;
}

/** Fail unless `seen` holds exactly `expected`, in any order. */
function expectExactly(seen: string[], expected: unknown[], what: string) {
  const want = expected.map(String).sort();
  const got = [...seen].sort();
  if (got.length !== want.length || got.some((id, i) => id !== want[i]))
    throw new Error(`${what}: expected ${want.length} rows, got ${got.length}`);
}

function asArray(res: unknown): unknown[] {
  if (!Array.isArray(res)) throw new Error("Expected an array result");
  return res;
}

function ids(res: unknown): string[] {
  return asArray(res).map((row) => String((row as { _id: unknown })._id));
}

/** The riders named by a list of sessions (each actor's name is unique). */
function riders(res: unknown): string[] {
  return asArray(res).map((row) =>
    String((row as { patientName: unknown }).patientName),
  );
}

/**
 * With one completed session per machine (`machine` for `patient`,
 * `otherMachine` for `otherPatient`, `orgBMachine` for `orgBPatient`), the
 * riders each actor is listed.
 */
const SESSION_RIDERS_SEEN_BY: Partial<Record<Actor, string[]>> = {
  manager: ["patient Synthetic"],
  patient: ["patient Synthetic"],
  orgAdmin: ["patient Synthetic", "otherPatient Synthetic"],
  orgBAdmin: ["orgBPatient Synthetic"],
  orgBManager: ["orgBPatient Synthetic"],
};

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
      { actor: "orgAdmin", expect: ok, note: "returns the existing row" },
      { actor: "orgBManager", expect: ok, note: "returns their own row, in their own organisation" },
    ],
  },
  {
    id: "users.getCurrentUser",
    ref: api.users.getCurrentUser,
    kind: "query",
    build: async () => ({}),
    onSuccess: (res, w, actor) => {
      const me = res as {
        _id: Id<"users">;
        role: string;
        organization: { _id: Id<"organizations"> } | null;
      };
      if (String(me._id) !== String(userId(w, actor)))
        throw new Error("Did not return the caller");
      if (me.role !== ROLE_OF[actor])
        throw new Error("Role is not the one of the call");
      if (String(me.organization?._id) !== String(organizationOf(w, actor)))
        throw new Error("Organisation is not the one of the call");
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
      { actor: "orgAdmin", expect: ok, note: "own account, as admin of centre A only" },
      { actor: "orgBManager", expect: ok, note: "own account, in centre B" },
    ],
  },
  {
    id: "users.updateUserRole",
    ref: api.users.updateUserRole,
    kind: "mutation",
    // Once Clerk Organizations carries the roles (the world of this matrix),
    // a role is a read-only mirror: nobody changes it here. The transition
    // before that is covered by `organizations.test.ts`.
    build: async (w) => ({ userId: w.stranger, role: "gestionnaire" }),
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      { actor: "manager", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      {
        actor: "admin",
        expect: refuse(/Roles are managed in Clerk Organizations/),
        note: "roles are a read-only mirror of Clerk",
      },
      {
        actor: "orgAdmin",
        expect: refuse(UNAUTHORIZED),
        note: "an organisation admin is not the Anheart admin",
      },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(UNAUTHORIZED),
        note: "an organisation admin is not the Anheart admin",
      },
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
      { actor: "orgBManager", expect: ok, note: "edits own profile" },
    ],
  },
  {
    id: "users.listUsers",
    ref: api.users.listUsers,
    kind: "query",
    // By direct identifier: a gestionnaire of another organisation as the filter.
    build: async (w, _actor, scope) =>
      scope === "foreign" ? { gestionnaireId: w.manager } : {},
    onSuccess: (res, w) => {
      const seen = ids(res);
      if (
        !seen.includes(String(w.otherPatient)) ||
        !seen.includes(String(w.orgBPatient))
      )
        throw new Error("Admin should see every account, in every organisation");
    },
    onFiltered: (res, w, actor) => {
      const seen = ids(res);
      const expected: Partial<Record<Actor, Id<"users">[]>> = {
        manager: [w.patient],
        orgBManager: [w.orgBPatient],
        orgAdmin: [
          w.orgAdmin,
          w.manager,
          w.otherManager,
          w.patient,
          w.otherPatient,
          w.stranger,
        ],
        orgBAdmin: [w.orgBAdmin, w.orgBManager, w.orgBPatient],
      };
      // user: only themselves
      expectExactly(
        seen,
        expected[actor] ?? [userId(w, actor)],
        `Accounts listed to ${actor}`,
      );
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "admin", expect: ok, note: "every account" },
      { actor: "manager", expect: filtered, note: "only their patients" },
      { actor: "patient", expect: filtered, scope: "self", note: "only self" },
      { actor: "orgAdmin", expect: filtered, note: "the members of centre A, nobody else" },
      { actor: "orgBAdmin", expect: filtered, note: "the members of centre B, nobody of centre A" },
      { actor: "orgBManager", expect: filtered, note: "only their patient of centre B" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "[] when filtering on a gestionnaire of another organisation",
      },
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
    onSuccess: async (res, w, actor) => {
      const created = res as Id<"users">;
      const { row, memberships } = await w.t.run(async (ctx) => ({
        row: await ctx.db.get(created),
        memberships: await ctx.db
          .query("memberships")
          .withIndex("by_user", (q) => q.eq("userId", created))
          .collect(),
      }));
      if (row?.role !== "user") throw new Error("Did not create a patient");
      const organizationId = organizationOf(w, actor);
      if (
        row.organizationId !== organizationId ||
        memberships.length !== 1 ||
        memberships[0].organizationId !== organizationId ||
        memberships[0].role !== "user" ||
        !memberships[0].active
      )
        throw new Error("Patient not created in the caller's organisation");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "a user cannot create patients" },
      { actor: "manager", expect: ok, note: "manager creates and self-links" },
      { actor: "admin", expect: ok, note: "admin creates" },
      { actor: "orgAdmin", expect: ok, note: "creates in centre A" },
      { actor: "orgBManager", expect: ok, note: "creates in centre B, never elsewhere" },
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the patient's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/Not authorized to edit this patient/),
        note: "a patient of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/Not authorized to edit this patient/),
        note: "a patient of another organisation, by identifier",
      },
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
      { actor: "orgAdmin", scope: "other", expect: ok, note: "admin of the patient's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "null for a patient of another organisation",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "null for a patient of another organisation",
      },
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "removes a patient of their organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/Not authorized to delete this user/),
        note: "a patient of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/Not authorized to delete this user/),
        note: "a patient of another organisation, by identifier",
      },
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
          organizationId: w.orgA,
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
      // self: the caller's verified address is the record's; other: it is not;
      // foreign: it is, but the record is another organisation's.
      return {
        email:
          scope === "self" || scope === "foreign"
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
      {
        actor: "orgBPatient",
        scope: "foreign",
        expect: empty,
        note: "null for a record of another organisation, even with its verified address",
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
        note: "a gestionnaire may attach themselves to a patient of their organisation",
      },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/can only assign themselves/),
        note: "a gestionnaire cannot assign another gestionnaire",
      },
      { actor: "admin", expect: ok, note: "admin assigns" },
      { actor: "orgAdmin", expect: ok, note: "admin of the organisation assigns" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/Can only assign gestionnaires to patients/),
        note: "the patient is not one of their organisation",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/Can only assign gestionnaires to patients/),
        note: "the patient is not one of their organisation",
      },
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
      { actor: "orgAdmin", expect: ok, note: "admin of the organisation removes" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/is not assigned to this patient/),
        note: "a link of another organisation does not exist for them",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/can only remove themselves/),
        note: "cannot remove another gestionnaire, here of another organisation",
      },
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
      { actor: "orgAdmin", scope: "other", expect: ok, note: "admin of the patient's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "[] for a patient of another organisation",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "[] for a patient of another organisation",
      },
    ],
  },
  {
    id: "users.listGestionnaires",
    ref: api.users.listGestionnaires,
    kind: "query",
    build: async () => ({}),
    onSuccess: (res, w) => {
      expectExactly(
        ids(res),
        [w.manager, w.otherManager, w.orgBManager],
        "Gestionnaires listed to the Anheart admin",
      );
    },
    onFiltered: (res, w, actor) => {
      expectExactly(
        ids(res),
        actor === "orgAdmin" ? [w.manager, w.otherManager] : [w.orgBManager],
        `Gestionnaires listed to ${actor}`,
      );
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: empty, note: "[] for a non-admin" },
      { actor: "manager", expect: empty, note: "[] for a non-admin" },
      { actor: "admin", expect: ok, note: "all gestionnaires" },
      { actor: "orgAdmin", expect: filtered, note: "the gestionnaires of centre A only" },
      { actor: "orgBAdmin", expect: filtered, note: "the gestionnaires of centre B only" },
      { actor: "orgBManager", expect: empty, note: "[] for a non-admin" },
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
      { actor: "orgAdmin", expect: ok, note: "admin of the organisation replaces the list" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/Gestionnaire not found/),
        note: "a gestionnaire of another organisation does not exist for them",
      },
      { actor: "orgBManager", scope: "foreign", expect: refuse(UNAUTHORIZED), note: "not an admin" },
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
      { actor: "orgAdmin", scope: "other", expect: ok, note: "admin of the gestionnaire's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "[] for a gestionnaire of another organisation",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "[] for a gestionnaire of another organisation",
      },
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
      if (m.organizationId !== w.anheartOrg)
        throw new Error("New machine should belong to the admin's organisation");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      { actor: "manager", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      { actor: "admin", expect: ok, note: "admin creates" },
      {
        actor: "orgAdmin",
        expect: refuse(UNAUTHORIZED),
        note: "machines are created by the Anheart admin only",
      },
      {
        actor: "orgBAdmin",
        expect: refuse(UNAUTHORIZED),
        note: "machines are created by the Anheart admin only",
      },
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation assigns" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/Machine not found/),
        note: "a machine of another organisation does not exist for them",
      },
      { actor: "orgBManager", scope: "foreign", expect: refuse(UNAUTHORIZED), note: "not an admin" },
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
      if (!linked(w.orgBMachine, w.orgBManager))
        throw new Error("Another organisation's link was changed");
      const added = rows.find(
        (r) => r.machineId === w.otherMachine && r.gestionnaireId === w.manager,
      );
      if (added?.organizationId !== w.orgA)
        throw new Error("New link not written in the machine's organisation");
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the gestionnaire's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/Gestionnaire not found/),
        note: "a gestionnaire of another organisation does not exist for them",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(UNAUTHORIZED),
        note: "not an admin",
      },
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/Not authorized to manage this machine/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/Not authorized to manage this machine/),
        note: "a machine of another organisation, by identifier",
      },
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "null for a machine of another organisation",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "null for a machine of another organisation",
      },
    ],
  },
  {
    id: "machines.listMachines",
    ref: api.machines.listMachines,
    kind: "query",
    build: async () => ({}),
    onSuccess: (res, w) => {
      expectExactly(
        ids(res),
        [w.machine, w.otherMachine, w.orgBMachine],
        "Machines listed to the Anheart admin",
      );
    },
    onFiltered: (res, w, actor) => {
      const expected: Partial<Record<Actor, Id<"machines">[]>> = {
        manager: [w.machine],
        otherManager: [w.otherMachine],
        orgAdmin: [w.machine, w.otherMachine],
        orgBAdmin: [w.orgBMachine],
        orgBManager: [w.orgBMachine],
      };
      expectExactly(ids(res), expected[actor] ?? [], `Machines listed to ${actor}`);
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: empty, note: "[] for a user" },
      { actor: "manager", scope: "own", expect: filtered, note: "only their machines" },
      { actor: "otherManager", scope: "own", expect: filtered, note: "only their machines" },
      { actor: "admin", expect: ok, note: "every machine" },
      { actor: "orgAdmin", scope: "own", expect: filtered, note: "the machines of centre A only" },
      { actor: "orgBAdmin", scope: "own", expect: filtered, note: "the machines of centre B only" },
      { actor: "orgBManager", scope: "own", expect: filtered, note: "only their machine of centre B" },
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/Not authorized to manage this machine/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/Not authorized to manage this machine/),
        note: "a machine of another organisation, by identifier",
      },
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/Not authorized to manage this machine/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/Not authorized to manage this machine/),
        note: "a machine of another organisation, by identifier",
      },
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
      { actor: "orgAdmin", expect: refuse(UNAUTHORIZED), note: "restore is reserved to the Anheart admin" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(UNAUTHORIZED),
        note: "restore is reserved to the Anheart admin",
      },
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "[] for a machine of another organisation",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "[] for a machine of another organisation",
      },
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/Not authorized to manage this machine/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/Not authorized to manage this machine/),
        note: "a machine of another organisation, by identifier",
      },
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
            organizationId: w.orgA,
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/Not authorized to manage this machine/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/Not authorized to manage this machine/),
        note: "a machine of another organisation, by identifier",
      },
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "[] for a machine of another organisation",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "[] for a machine of another organisation",
      },
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
      { actor: "orgAdmin", scope: "other", expect: ok, note: "admin of the gestionnaire's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "[] for a gestionnaire of another organisation",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "[] for a gestionnaire of another organisation",
      },
    ],
  },

  // =========================================================================
  // sessions.ts
  // =========================================================================
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the session's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "null for a session of another organisation",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "null for a session of another organisation",
      },
      {
        actor: "orgBPatient",
        scope: "foreign",
        expect: empty,
        note: "null for a session of another organisation",
      },
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
      await addSession(w, {
        machineId: w.orgBMachine,
        userId: w.orgBPatient,
        status: "completed",
        kind: "auto",
      });
      // By direct identifier: a machine of another organisation as the filter.
      if (scope === "foreign") return { machineId: w.machine };
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
      if (asArray(res).length !== 3)
        throw new Error("Admin should see all sessions, in every organisation");
    },
    onFiltered: (res, _w, actor, scope) => {
      if (scope === "other") {
        // Intended policy: the caller still sees their own session.
        if (asArray(res).length !== 1)
          throw new Error("Caller should still see their own session");
        return;
      }
      expectExactly(
        riders(res),
        SESSION_RIDERS_SEEN_BY[actor] ?? [],
        `Sessions listed to ${actor}`,
      );
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
      { actor: "orgAdmin", scope: "own", expect: filtered, note: "the sessions of centre A only" },
      { actor: "orgBAdmin", scope: "own", expect: filtered, note: "the sessions of centre B only" },
      { actor: "orgBManager", scope: "own", expect: filtered, note: "their machine's sessions, in centre B" },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "[] when filtering on a machine of another organisation",
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "null for a machine of another organisation",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "null for a machine of another organisation",
      },
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
      await addSession(w, {
        machineId: w.orgBMachine,
        userId: w.orgBPatient,
        status: "completed",
        kind: "auto",
      });
      return {};
    },
    onSuccess: (res) => {
      if (asArray(res).length !== 3)
        throw new Error(
          "Admin should see all completed sessions, in every organisation",
        );
    },
    onFiltered: (res, _w, actor) => {
      expectExactly(
        riders(res),
        SESSION_RIDERS_SEEN_BY[actor] ?? [],
        `Completed sessions listed to ${actor}`,
      );
    },
    cases: [
      { actor: "admin", expect: ok, note: "all completed sessions" },
      { actor: "manager", scope: "own", expect: filtered, note: "their machine's completed sessions" },
      { actor: "orgAdmin", scope: "own", expect: filtered, note: "the completed sessions of centre A only" },
      { actor: "orgBAdmin", scope: "own", expect: filtered, note: "the completed sessions of centre B only" },
      {
        actor: "orgBManager",
        scope: "own",
        expect: filtered,
        note: "their machine's completed sessions, in centre B",
      },
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
            organizationId: w.orgA,
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
      { actor: "orgAdmin", expect: ok, note: "admin of the machine's organisation grants" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/Only an admin or a manager of this machine/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/Only an admin or a manager of this machine/),
        note: "a machine of another organisation, by identifier",
      },
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation revokes" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/Only an admin or a manager of this machine/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/Only an admin or a manager of this machine/),
        note: "a machine of another organisation, by identifier",
      },
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "[] for a machine of another organisation",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "[] for a machine of another organisation",
      },
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
      { actor: "orgAdmin", scope: "other", expect: ok, note: "admin of the patient's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/do not manage this user/),
        note: "a patient of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/do not manage this user/),
        note: "a patient of another organisation, by identifier",
      },
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "[] for a machine of another organisation",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "[] for a machine of another organisation",
      },
      {
        actor: "orgBPatient",
        scope: "foreign",
        expect: empty,
        note: "[] for a machine of another organisation",
      },
    ],
  },
  {
    id: "training.listLaunchableMachines",
    ref: api.training.listLaunchableMachines,
    kind: "query",
    build: async () => ({}),
    onSuccess: (res, w) => {
      expectExactly(
        ids(res),
        [w.machine, w.otherMachine, w.orgBMachine],
        "Machines the Anheart admin may launch on",
      );
    },
    onFiltered: (res, w, actor) => {
      const expected: Partial<Record<Actor, Id<"machines">[]>> = {
        manager: [w.machine],
        patient: [w.machine], // holds the right on w.machine
        orgAdmin: [w.machine, w.otherMachine],
        orgBAdmin: [w.orgBMachine],
        orgBManager: [w.orgBMachine],
        orgBPatient: [w.orgBMachine],
      };
      expectExactly(
        ids(res),
        expected[actor] ?? [],
        `Machines ${actor} may launch on`,
      );
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", scope: "own", expect: filtered, note: "machines they hold the right on" },
      { actor: "stranger", expect: empty, note: "[] without any launch right" },
      { actor: "manager", scope: "own", expect: filtered, note: "machines they manage" },
      { actor: "admin", expect: ok, note: "every machine" },
      { actor: "orgAdmin", scope: "own", expect: filtered, note: "the machines of centre A only" },
      { actor: "orgBAdmin", scope: "own", expect: filtered, note: "the machines of centre B only" },
      { actor: "orgBManager", scope: "own", expect: filtered, note: "the machine they manage, in centre B" },
      {
        actor: "orgBPatient",
        scope: "own",
        expect: filtered,
        note: "the machine they hold the right on, in centre B",
      },
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
      if (actor === "stranger" || actor === "orgBPatient") return base; // for self, no right
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
      {
        actor: "orgAdmin",
        scope: "own",
        expect: ok,
        note: "admin of the machine's organisation launches for a rider",
      },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/Not authorized to use this machine/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/Not authorized to use this machine/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBPatient",
        scope: "foreign",
        expect: refuse(/have not been given the right/),
        note: "no launch right reaches a machine of another organisation",
      },
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the session's organisation stops" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/Session not found/),
        note: "a session of another organisation does not exist for them",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/Session not found/),
        note: "a session of another organisation does not exist for them",
      },
      {
        actor: "orgBPatient",
        scope: "foreign",
        expect: refuse(/Session not found/),
        note: "a session of another organisation does not exist for them",
      },
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "null for a machine of another organisation",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "null for a machine of another organisation",
      },
      {
        actor: "orgBPatient",
        scope: "foreign",
        expect: empty,
        note: "null for a machine of another organisation",
      },
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
          organizationId: w.orgA,
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
      {
        actor: "manager",
        scope: "own",
        expect: ok,
        note: "manages this machine",
      },
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the session's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "[] for a session of another organisation",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "[] for a session of another organisation",
      },
      {
        actor: "orgBPatient",
        scope: "foreign",
        expect: empty,
        note: "[] for a session of another organisation",
      },
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
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the session's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "null for a session of another organisation",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "null for a session of another organisation",
      },
      {
        actor: "orgBPatient",
        scope: "foreign",
        expect: empty,
        note: "null for a session of another organisation",
      },
    ],
  },

  // =========================================================================
  // ecgData.ts  (read-only history; identical access rule across the five readers)
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
        { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the session's organisation" },
        { actor: "orgBAdmin", scope: "foreign", expect: empty, note: "empty for a session of another organisation" },
        { actor: "orgBManager", scope: "foreign", expect: empty, note: "empty for a session of another organisation" },
        { actor: "orgBPatient", scope: "foreign", expect: empty, note: "empty for a session of another organisation" },
      ],
    }),
  ),

  // =========================================================================
  // softwareReleases.ts  (ANH-134: the register of released versions)
  // =========================================================================
  {
    id: "softwareReleases.recordRelease",
    ref: api.softwareReleases.recordRelease,
    kind: "mutation",
    build: async () => ({
      component: "pi",
      version: "pi-0.1.0",
      validationLevel: "bench",
      releasedAt: NOW,
    }),
    onSuccess: async (res, w) => {
      const row = await w.t.run((ctx) =>
        ctx.db.get(res as Id<"software_releases">),
      );
      if (row?.version !== "pi-0.1.0" || row.validationLevel !== "bench")
        throw new Error("Release not recorded");
      if (row.recordedBy !== w.admin)
        throw new Error("Release not attributed to the admin");
      // ANH-195: the level it was recorded with is the first of its history.
      const decisions = await w.t.run((ctx) =>
        ctx.db.query("software_release_levels").collect(),
      );
      if (
        decisions.length !== 1 ||
        decisions[0].releaseId !== row._id ||
        decisions[0].validationLevel !== "bench" ||
        decisions[0].decidedBy !== w.admin
      )
        throw new Error("Level decision not recorded for the admin");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      { actor: "manager", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      { actor: "admin", expect: ok, note: "admin records a version" },
      { actor: "orgAdmin", expect: refuse(UNAUTHORIZED), note: "the register is Anheart's, not an organisation's" },
      { actor: "orgBAdmin", expect: refuse(UNAUTHORIZED), note: "the register is Anheart's, not an organisation's" },
    ],
  },
  {
    id: "softwareReleases.listReleases",
    ref: api.softwareReleases.listReleases,
    kind: "query",
    build: async (w) => {
      await w.t.run((ctx) =>
        ctx.db.insert("software_releases", {
          component: "pi",
          version: "pi-0.1.0",
          validationLevel: "bench",
          releasedAt: NOW,
          recordedBy: w.admin,
          updatedAt: NOW,
        }),
      );
      return {};
    },
    onSuccess: (res) => {
      const rows = asArray(res) as Array<{
        version: string;
        levelHistory: Array<{ validationLevel: string }>;
      }>;
      if (rows.map((row) => row.version).join() !== "pi-0.1.0")
        throw new Error("Expected the recorded version");
      // ANH-195: the levels the version has held are read with it.
      const held = rows[0].levelHistory.map((entry) => entry.validationLevel);
      if (held.join() !== "bench")
        throw new Error("Expected the level history of the version");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      { actor: "manager", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      { actor: "admin", expect: ok, note: "admin reads the register" },
      { actor: "orgAdmin", expect: refuse(UNAUTHORIZED), note: "the register is Anheart's, not an organisation's" },
      { actor: "orgBAdmin", expect: refuse(UNAUTHORIZED), note: "the register is Anheart's, not an organisation's" },
    ],
  },

  // =========================================================================
  // sessionSummaries.ts  (read-only history)
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
    { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the session's organisation", ...defect },
    { actor: "orgBAdmin", scope: "foreign", expect: empty, note: "null for a session of another organisation" },
    { actor: "orgBManager", scope: "foreign", expect: empty, note: "null for a session of another organisation" },
    { actor: "orgBPatient", scope: "foreign", expect: empty, note: "null for a session of another organisation" },
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
  { method: "GET", path: "/api/machine/training/poll", tests: "httpRoutes.test.ts (auto, this machine)" },
  { method: "GET", path: "/api/machine/roster", tests: "httpRoutes.test.ts" },
  { method: "POST", path: "/api/machine/profiles", tests: "httpRoutes.test.ts (malformed body)" },
  { method: "POST", path: "/api/machine/training/start", tests: "httpRoutes.test.ts (non-pending refused)" },
  { method: "POST", path: "/api/machine/training/local", tests: "httpRoutes.test.ts (idempotent)" },
  { method: "POST", path: "/api/machine/training/end", tests: "httpRoutes.test.ts (idempotent)" },
  { method: "GET", path: "/api/machine/training/status", tests: "httpRoutes.test.ts" },
  { method: "POST", path: "/api/machine/training/telemetry", tests: "httpRoutes.test.ts (other machine refused); journalSync.test.ts (idempotent, session window)" },
  { method: "POST", path: "/api/machine/training/events", tests: "httpRoutes.test.ts (other machine refused); journalSync.test.ts (idempotent, sizes, session window)" },
];

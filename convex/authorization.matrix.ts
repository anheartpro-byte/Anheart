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
 * Error codes: every refusal of a function the site calls is a stable code
 * (`ConvexError(code)`, table in `convex/lib/errors.ts`), the sign-in checks
 * included (ANH-90); a refusal cell matches the code.
 *
 * The file name has two dots so the Convex bundler never deploys it.
 */
import { isDeepStrictEqual } from "node:util";
import { api } from "./_generated/api";
import type { FunctionReference } from "convex/server";
import type { Id } from "./_generated/dataModel";
import { CONSENT_TEXT_VERSION } from "../lib/consentText";
import { CURRENT_QUESTIONNAIRE } from "../lib/fitnessQuestionnaire";
import { notifyMachine } from "./lib/notifications";
import { registryRelease, registryUpload } from "./machineRegistry.fixtures";
import {
  addSession,
  as,
  makeRiderReady,
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
  ref: FunctionReference<"query" | "mutation" | "action", "public">;
  kind: "query" | "mutation" | "action";
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

const NOT_AUTH = /not_authenticated/;
const UNAUTHORIZED = /not_authorized/;

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

function fitAnswers(): Record<string, boolean> {
  return Object.fromEntries(
    CURRENT_QUESTIONNAIRE.questions.map((question) => [
      question.id,
      question.outcomes.yes === "fit",
    ]),
  );
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

/** The rows of one page of a paginated query (`{page, isDone, continueCursor}`). */
function pageOf(res: unknown): unknown[] {
  return asArray((res as { page?: unknown } | null)?.page);
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
  orgBPatient: ["orgBPatient Synthetic"],
};

/**
 * The world of the session lists: one completed session per machine, as
 * `SESSION_RIDERS_SEEN_BY` describes. With the scope "other", three newer
 * sessions of `otherPatient` come first and the caller asks for two rows
 * only: rights come before the cut, so the caller's own session is still
 * listed (ANH-158).
 */
async function seedSessionLists(w: World, scope: Scope): Promise<number> {
  for (const [machineId, userId] of [
    [w.machine, w.patient],
    [w.otherMachine, w.otherPatient],
    [w.orgBMachine, w.orgBPatient],
  ] as const) {
    await addSession(w, { machineId, userId, status: "completed", kind: "auto" });
  }
  if (scope !== "other") return 50;
  for (let i = 0; i < 3; i++)
    await addSession(w, {
      machineId: w.otherMachine,
      userId: w.otherPatient,
      status: "completed",
      kind: "auto",
    });
  return 2;
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

/** A key of the machine, valid, as a rotation writes it (ANH-165). */
async function machineKeyOf(
  w: World,
  machineId: Id<"machines">,
): Promise<Id<"machine_api_keys">> {
  return await w.t.run((ctx) =>
    ctx.db.insert("machine_api_keys", {
      machineId,
      selector: "ab".repeat(16),
      keyHash: "synthetic-verifier",
      prefix: "anh1.abababab",
      createdAt: NOW,
    }),
  );
}

export const MATRIX: Entry[] = [
  {
    id: "machineRegistry.listContactCandidates", ref: api.machineRegistry.listContactCandidates, kind: "query",
    build: async (w, actor, scope) => ({ machineId: actor === "admin" && scope === "other" ? w.orgBMachine : w.machine }),
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "admin", scope: "own", expect: ok, note: "members of centre A" },
      { actor: "admin", scope: "other", expect: ok, note: "members of centre B" },
      ...(["orgAdmin", "manager", "otherManager", "patient", "otherPatient", "stranger", "orgBAdmin", "orgBManager", "orgBPatient"] as const)
        .flatMap((actor) => (["own", "foreign"] as const).map((scope) => ({ actor, scope, expect: refuse(UNAUTHORIZED), note: "only Anheart admins select contacts" }))),
    ],
    onSuccess: async (res, w, _actor, scope) => {
      const rows = res as Array<{ userId: string; firstName: string; lastName: string; role: string }>;
      const organizationId = scope === "other" ? w.orgB : w.orgA;
      const expected = await w.t.run(async (ctx) => {
        const memberships = await ctx.db.query("memberships").withIndex("by_organization", (q) => q.eq("organizationId", organizationId)).collect();
        const contacts = [];
        for (const member of memberships.filter((member) => member.active)) {
          const user = await ctx.db.get(member.userId);
          if (user !== null && user.clerkDeletedAt === undefined)
            contacts.push({ userId: user._id, firstName: user.firstName, lastName: user.lastName, role: member.role });
        }
        return contacts.sort((a, b) => a.lastName.localeCompare(b.lastName) || a.firstName.localeCompare(b.firstName) || a.userId.localeCompare(b.userId));
      });
      if (JSON.stringify(rows) !== JSON.stringify(expected)) throw new Error("Contact candidates differ from active organisation members");
    },
  },
  {
    id: "machineRegistry.getRegistry", ref: api.machineRegistry.getRegistry, kind: "query",
    cases: registryReadCases(),
    build: async (w, actor, scope) => ({ machineId: registryTarget(w, actor, scope) }),
    onSuccess: (res, w, actor) => {
      const row = res as { machineId: string; validation: { level: string }; canEdit: boolean };
      if (row.machineId !== w.machine || row.validation.level !== "none" || row.canEdit !== (actor === "admin"))
        throw new Error("Registry visibility or empty state differs");
    },
  },
  {
    id: "machineRegistry.checkConfigurationLock", ref: api.machineRegistry.checkConfigurationLock, kind: "query",
    cases: registryReadCases(),
    build: async (w, actor, scope) => ({ machineId: registryTarget(w, actor, scope), key: "PROGRAMS_ENABLED", value: true }),
    onSuccess: (res) => {
      const verdict = res as { allowed: boolean; reachedLevel: string; requiredLevel: string; message: string };
      if (verdict.allowed || verdict.reachedLevel !== "none" || verdict.requiredLevel !== "auto" || verdict.message !== "machine validée jusqu'au niveau none")
        throw new Error("Missing registry must refuse programs");
    },
  },
  {
    id: "machineRegistry.listRegistryBadges", ref: api.machineRegistry.listRegistryBadges, kind: "query",
    build: async () => ({}),
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "admin", expect: ok, note: "all active machines" },
      { actor: "orgAdmin", expect: ok, note: "centre A only" },
      { actor: "orgBAdmin", expect: ok, note: "centre B only" },
      ...(["manager", "otherManager", "patient", "otherPatient", "stranger", "orgBManager", "orgBPatient"] as const)
        .map((actor) => ({ actor, expect: empty, note: "registry not visible to this role" })),
    ],
    onSuccess: (res, w, actor) => {
      const rows = res as Array<{ machineId: string; level: string; versionStatus: string; configurationStatus: string }>;
      const expected = actor === "admin" ? [w.machine, w.otherMachine, w.orgBMachine] : actor === "orgAdmin" ? [w.machine, w.otherMachine] : [w.orgBMachine];
      expectExactly(rows.map((row) => row.machineId), expected, "registry badge machines");
      if (rows.some((row) => row.level !== "none" || row.versionStatus !== "unknown" || row.configurationStatus !== "unavailable"))
        throw new Error("Empty registry badges differ");
    },
  },
  ...registryMutationEntries(),
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
        expect: refuse(/roles_managed_in_clerk/),
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
    kind: "action",
    build: async (_w, actor) => ({
      firstName: "New",
      lastName: "Patient",
      email: `np-${actor}@example.invalid`,
    }),
    onSuccess: async (res, w, actor) => {
      const result = res as { invitationId: Id<"patient_invitations"> };
      const invitation = await w.t.run((ctx) =>
        ctx.db.get(result.invitationId),
      );
      if (!invitation?.userId || invitation.status !== "sent")
        throw new Error("Invitation was not sent");
      const created = invitation.userId;
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
        memberships[0].active
      )
        throw new Error("Patient not created in the caller's organisation");
    },
    cases: [
      {
        actor: "anonymous",
        expect: refuse(NOT_AUTH),
        note: "sign-in required",
      },
      {
        actor: "patient",
        expect: refuse(UNAUTHORIZED),
        note: "a user cannot create patients",
      },
      {
        actor: "manager",
        expect: ok,
        note: "manager invites; linking waits for acceptance",
      },
      { actor: "admin", expect: ok, note: "admin creates" },
      { actor: "orgAdmin", expect: ok, note: "creates in centre A" },
      {
        actor: "orgBManager",
        expect: ok,
        note: "creates in centre B, never elsewhere",
      },
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
        expect: refuse(/patient_not_managed/),
        note: "does not manage this patient",
      },
      { actor: "admin", expect: ok, note: "admin edits anyone" },
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the patient's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/patient_not_managed/),
        note: "a patient of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/patient_not_managed/),
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
        expect: refuse(/not_authorized/),
        note: "a user cannot delete anyone",
      },
      { actor: "manager", scope: "own", expect: ok, note: "deletes their own patient" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/user_not_managed/),
        note: "not this patient's manager",
      },
      { actor: "admin", scope: "other", expect: ok, note: "admin deletes a user" },
      { actor: "orgAdmin", scope: "own", expect: ok, note: "removes a patient of their organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/user_not_managed/),
        note: "a patient of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/user_not_managed/),
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
      await w.t.run((ctx) => ctx.db.patch(w.stranger, { clerkId: "previous-stranger" }));
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
        expect: refuse(/gestionnaire_self_only/),
        note: "a gestionnaire cannot assign another gestionnaire",
      },
      { actor: "admin", expect: ok, note: "admin assigns" },
      { actor: "orgAdmin", expect: ok, note: "admin of the organisation assigns" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/not_a_patient/),
        note: "the patient is not one of their organisation",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/not_a_patient/),
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
        expect: refuse(/gestionnaire_self_only/),
        note: "cannot remove another gestionnaire",
      },
      { actor: "admin", expect: ok, note: "admin removes" },
      { actor: "orgAdmin", expect: ok, note: "admin of the organisation removes" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/gestionnaire_not_assigned/),
        note: "a link of another organisation does not exist for them",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/gestionnaire_self_only/),
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
    // "foreign": centre A named from centre B, to narrow the list to it.
    build: async (w, _actor, scope) =>
      scope === "foreign" ? { organizationId: w.orgA } : {},
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
      { actor: "orgBAdmin", scope: "foreign", expect: empty, note: "[] when centre B names centre A" },
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
        expect: refuse(/gestionnaire_not_found/),
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
        expect: refuse(/machine_not_found/),
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
        expect: refuse(/gestionnaire_not_found/),
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
  // =========================================================================
  // machineKeys.ts (ANH-165): the keys are the admins' only
  // =========================================================================
  {
    id: "machineKeys.listMachineKeys",
    ref: api.machineKeys.listMachineKeys,
    kind: "query",
    build: async (w) => {
      await machineKeyOf(w, w.machine);
      return { machineId: w.machine };
    },
    onSuccess: (res) => {
      const rows = res as { prefix: string; state: string }[];
      if (rows.length !== 1 || rows[0].prefix !== "anh1.abababab")
        throw new Error("The machine's key is not listed");
      if (rows[0].state !== "active") throw new Error("Wrong state listed");
      const listed = JSON.stringify(rows);
      if (listed.includes("synthetic-verifier") || listed.includes("ab".repeat(16)))
        throw new Error("The verifier or the selector of a key is listed");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: empty, note: "[] for a user" },
      {
        actor: "manager",
        scope: "own",
        expect: empty,
        note: "[] even to a gestionnaire of the machine: the keys are the admins'",
      },
      { actor: "admin", expect: ok, note: "admin lists" },
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
    id: "machineKeys.createMachineKey",
    ref: api.machineKeys.createMachineKey,
    kind: "mutation",
    build: async (w) => ({ machineId: w.machine }),
    onSuccess: async (res, w) => {
      const r = res as {
        keyId: Id<"machine_api_keys">;
        apiKey: string;
        prefix: string;
      };
      if (!r.apiKey.startsWith(`${r.prefix}`) || r.prefix.length !== 13)
        throw new Error("No one-time key issued");
      const key = await w.t.run((ctx) => ctx.db.get(r.keyId));
      if (key?.machineId !== w.machine)
        throw new Error("The key is not the machine's");
      if (JSON.stringify(key).includes(r.apiKey))
        throw new Error("The plain key is stored");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      {
        actor: "manager",
        scope: "own",
        expect: refuse(UNAUTHORIZED),
        note: "a gestionnaire of the machine is not an admin",
      },
      { actor: "admin", expect: ok, note: "admin creates" },
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/machine_not_found/),
        note: "a machine of another organisation does not exist for them",
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
    id: "machineKeys.revokeMachineKey",
    ref: api.machineKeys.revokeMachineKey,
    kind: "mutation",
    build: async (w) => ({ keyId: await machineKeyOf(w, w.machine) }),
    onSuccess: async (_res, w, _actor, _scope, args) => {
      const key = await w.t.run((ctx) =>
        ctx.db.get(args.keyId as Id<"machine_api_keys">),
      );
      if (key?.revokedAt === undefined) throw new Error("Key not revoked");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: refuse(UNAUTHORIZED), note: "not an admin" },
      {
        actor: "manager",
        scope: "own",
        expect: refuse(UNAUTHORIZED),
        note: "a gestionnaire of the machine is not an admin",
      },
      { actor: "admin", expect: ok, note: "admin revokes" },
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/machine_key_not_found/),
        note: "a key of another organisation's machine does not exist for them",
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
        expect: refuse(/machine_not_managed/),
        note: "a user cannot manage a machine",
      },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/machine_not_managed/),
        note: "does not manage this machine",
      },
      { actor: "admin", expect: ok, note: "admin updates" },
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/machine_not_managed/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/machine_not_managed/),
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
        expect: refuse(/machine_not_managed/),
        note: "a user cannot manage a machine",
      },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/machine_not_managed/),
        note: "does not manage this machine",
      },
      { actor: "admin", expect: ok, note: "admin deletes" },
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/machine_not_managed/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/machine_not_managed/),
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
        expect: refuse(/machine_not_managed/),
        note: "does not manage this machine",
      },
      { actor: "admin", expect: ok, note: "admin assigns" },
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/machine_not_managed/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/machine_not_managed/),
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
        expect: refuse(/machine_not_managed/),
        note: "does not manage this machine (removing the owner)",
      },
      { actor: "admin", expect: ok, note: "admin removes" },
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/machine_not_managed/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/machine_not_managed/),
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
      const limit = await seedSessionLists(w, scope);
      // By direct identifier: a machine of another organisation as the filter.
      if (scope === "foreign") return { machineId: w.machine };
      return { limit };
    },
    onSuccess: (res) => {
      if (asArray(res).length !== 3)
        throw new Error("Admin should see all sessions, in every organisation");
    },
    onFiltered: (res, _w, actor) => {
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
      {
        actor: "manager",
        scope: "other",
        expect: filtered,
        note: "newer sessions of a machine they do not manage never push theirs out of a small limit",
      },
      { actor: "patient", scope: "own", expect: filtered, note: "their own sessions" },
      {
        actor: "patient",
        scope: "other",
        expect: filtered,
        note: "newer sessions of another rider never push theirs out of a small limit",
      },
      { actor: "stranger", scope: "other", expect: empty, note: "[] for a user who rode nothing" },
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
    id: "sessions.listSessionsPaginated",
    ref: api.sessions.listSessionsPaginated,
    kind: "query",
    // The same rule as listSessions, page by page. Every page and the order
    // over 300 sessions are proven in sessionLists.test.ts.
    build: async (w, actor, scope) => {
      const numItems = await seedSessionLists(w, scope);
      const paginationOpts = { numItems, cursor: null };
      // By direct identifier: a machine, or a rider, of another organisation.
      if (scope === "foreign")
        return actor === "orgBAdmin"
          ? { userId: w.patient, paginationOpts }
          : { machineId: w.machine, paginationOpts };
      return { paginationOpts };
    },
    onSuccess: (res) => {
      if (pageOf(res).length !== 3)
        throw new Error("Admin should see all sessions, in every organisation");
    },
    onFiltered: (res, _w, actor) => {
      expectExactly(
        riders(pageOf(res)),
        SESSION_RIDERS_SEEN_BY[actor] ?? [],
        `Sessions paged to ${actor}`,
      );
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "admin", expect: ok, note: "all sessions" },
      { actor: "manager", scope: "own", expect: filtered, note: "their machine's sessions" },
      {
        actor: "manager",
        scope: "other",
        expect: filtered,
        note: "newer sessions of a machine they do not manage never push theirs out of a small page",
      },
      { actor: "patient", scope: "own", expect: filtered, note: "their own sessions" },
      {
        actor: "patient",
        scope: "other",
        expect: filtered,
        note: "newer sessions of another rider never push theirs out of a small page",
      },
      { actor: "stranger", scope: "other", expect: empty, note: "an empty page for a user who rode nothing" },
      { actor: "orgAdmin", scope: "own", expect: filtered, note: "the sessions of centre A only" },
      { actor: "orgBAdmin", scope: "own", expect: filtered, note: "the sessions of centre B only" },
      { actor: "orgBManager", scope: "own", expect: filtered, note: "their machine's sessions, in centre B" },
      { actor: "orgBPatient", scope: "own", expect: filtered, note: "their own sessions, in centre B" },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "an empty page when filtering on a machine of another organisation",
      },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "an empty page when filtering on a rider of another organisation",
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
        expect: refuse(/machine_not_managed/),
        note: "a user cannot grant",
      },
      { actor: "manager", scope: "own", expect: ok, note: "manages machine and user" },
      {
        actor: "manager",
        scope: "other",
        expect: refuse(/user_not_managed/),
        note: "manages the machine but not this rider",
      },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/machine_not_managed/),
        note: "does not manage this machine",
      },
      { actor: "admin", expect: ok, note: "admin grants" },
      { actor: "orgAdmin", expect: ok, note: "admin of the machine's organisation grants" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/machine_not_managed/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/machine_not_managed/),
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
        expect: refuse(/machine_not_managed/),
        note: "a user cannot revoke",
      },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/machine_not_managed/),
        note: "does not manage this machine",
      },
      { actor: "admin", expect: ok, note: "admin revokes" },
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the machine's organisation revokes" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/machine_not_managed/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/machine_not_managed/),
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
        expect: refuse(UNAUTHORIZED),
        note: "a user may never set physiology, not even their own",
      },
      { actor: "manager", scope: "own", expect: ok, note: "manages this patient" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/user_not_managed/),
        note: "does not manage this patient",
      },
      { actor: "admin", scope: "other", expect: ok, note: "admin sets" },
      { actor: "orgAdmin", scope: "other", expect: ok, note: "admin of the patient's organisation" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/user_not_managed/),
        note: "a patient of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/user_not_managed/),
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
      if (!(actor === "admin" && scope === "other")) {
        await makeRiderReady(w.t, w.patient, w.orgA, {
          now: NOW,
          validUntil: Date.UTC(2100, 0, 1),
        });
      }
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
      if (actor === "admin" && scope === "other") {
        return { ...base, userId: w.otherPatient };
      }
      return { ...base, userId: w.patient }; // admin
    },
    onSuccess: async (res, w, actor, _scope, args) => {
      const s = await w.t.run((ctx) => ctx.db.get(res as Id<"sessions">));
      const riderId =
        (args.userId as Id<"users"> | undefined) ?? userId(w, actor);
      if (s?.kind !== "auto" || s.status !== "pending" || s.userId !== riderId)
        throw new Error("Auto session not queued for the intended rider");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", scope: "self", expect: ok, note: "launches for themselves with a right" },
      {
        actor: "patient",
        scope: "other",
        expect: refuse(/launch_for_self_only/),
        note: "a user may not launch for someone else",
      },
      {
        actor: "stranger",
        scope: "self",
        expect: refuse(/launch_right_missing/),
        note: "no launch right on the machine",
      },
      { actor: "manager", scope: "own", expect: ok, note: "launches for a managed rider" },
      {
        actor: "manager",
        scope: "other",
        expect: refuse(/user_not_managed/),
        note: "manages the machine but not the rider",
      },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/machine_not_managed/),
        note: "does not manage this machine",
      },
      { actor: "admin", scope: "own", expect: ok, note: "admin launches for a rider" },
      {
        actor: "admin",
        scope: "other",
        expect: refuse(/rider_not_fit/),
        note: "an admin receives the refusal for a passenger who is not ready",
      },
      {
        actor: "orgAdmin",
        scope: "own",
        expect: ok,
        note: "admin of the machine's organisation launches for a rider",
      },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/machine_not_managed/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/machine_not_managed/),
        note: "a machine of another organisation, by identifier",
      },
      {
        actor: "orgBPatient",
        scope: "foreign",
        expect: refuse(/launch_right_missing/),
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
        expect: refuse(/session_not_managed/),
        note: "not the rider and no machine access",
      },
      { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/session_not_managed/),
        note: "does not manage this machine",
      },
      { actor: "admin", expect: ok, note: "admin stops" },
      { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the session's organisation stops" },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/session_not_found/),
        note: "a session of another organisation does not exist for them",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/session_not_found/),
        note: "a session of another organisation does not exist for them",
      },
      {
        actor: "orgBPatient",
        scope: "foreign",
        expect: refuse(/session_not_found/),
        note: "a session of another organisation does not exist for them",
      },
    ],
  },
  {
    id: "recordStorage.list",
    ref: api.recordStorage.list,
    kind: "query",
    build: async (w) => ({sessionId:await addSession(w,{machineId:w.machine,status:"completed",kind:"manual"})}),
    cases: [
      {actor:"anonymous",expect:refuse(NOT_AUTH),note:"sign-in required"},
      {actor:"admin",expect:ok,note:"Anheart administrator"},
      {actor:"orgAdmin",scope:"own",expect:ok,note:"organisation administrator"},
      {actor:"manager",scope:"own",expect:ok,note:"assigned manager"},
      {actor:"patient",scope:"own",expect:empty,note:"raw records require maintenance access"},
      {actor:"otherManager",scope:"other",expect:empty,note:"unassigned manager"},
      {actor:"orgBAdmin",scope:"foreign",expect:empty,note:"other organisation"},
      {actor:"orgBManager",scope:"foreign",expect:empty,note:"other organisation"},
      {actor:"orgBPatient",scope:"foreign",expect:empty,note:"other organisation"},
    ],
  },
  {
    id: "recordStorage.remove",
    ref: api.recordStorage.remove,
    kind: "mutation",
    build: async (w) => ({sessionId:await addSession(w,{machineId:w.machine,status:"completed",kind:"manual"})}),
    cases: [
      {actor:"anonymous",expect:refuse(NOT_AUTH),note:"sign-in required"},
      {actor:"admin",expect:ok,note:"Anheart administrator"},
      {actor:"orgAdmin",scope:"own",expect:ok,note:"organisation administrator"},
      {actor:"manager",scope:"own",expect:refuse(UNAUTHORIZED),note:"admin deletion only"},
      {actor:"patient",scope:"own",expect:refuse(UNAUTHORIZED),note:"admin deletion only"},
      {actor:"orgBAdmin",scope:"foreign",expect:refuse(/session_not_found/),note:"other organisation"},
      {actor:"orgBManager",scope:"foreign",expect:refuse(UNAUTHORIZED),note:"other organisation"},
    ],
    onSuccess: async (_res,w,_actor,_scope,args) => {
      if(typeof args.sessionId!=="string") throw Error("Missing session identifier");
      const rawId=args.sessionId;
      const row=await w.t.run(async(ctx)=>{
        const id=ctx.db.normalizeId("sessions",rawId);
        return id===null?null:await ctx.db.get(id);
      });
      if(!row?.recordDeleted) throw Error("Deposit deletion was not recorded");
    },
  },
  {
    id: "machineHealth.get",
    ref: api.machineHealth.get,
    kind: "query",
    build: async (w) => ({ machineId: w.machine }),
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", scope: "own", expect: empty, note: "a launch right does not grant maintenance access" },
      { actor: "stranger", expect: empty, note: "no machine access" },
      { actor: "manager", scope: "own", expect: ok, note: "assigned machine" },
      { actor: "otherManager", scope: "other", expect: empty, note: "unassigned machine" },
      { actor: "admin", expect: ok, note: "admin reads" },
      { actor: "orgAdmin", scope: "own", expect: ok, note: "same organisation" },
      { actor: "orgBAdmin", scope: "foreign", expect: empty, note: "different organisation" },
      { actor: "orgBManager", scope: "foreign", expect: empty, note: "different organisation" },
      { actor: "orgBPatient", scope: "foreign", expect: empty, note: "different organisation" },
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
  {
    id: "medicalParameters.current",
    ref: api.medicalParameters.current,
    kind: "query",
    build: async () => ({}),
    onSuccess: (res) => {
      const current = res as {
        version: string;
        issuedAt: string;
        signedBy: string;
        zoneCeilingFraction: number;
        minRiderAge: number;
        hrMaxFormula: string;
      } | null;
      if (
        current?.version !== "0.1.0" ||
        current.issuedAt !== "2026-10-14" ||
        current.signedBy !== "cle-de-test" ||
        current.zoneCeilingFraction !== 0.9 ||
        current.minRiderAge !== 18 ||
        current.hrMaxFormula !== "tanaka"
      ) {
        throw new Error("Expected the current signed medical parameters");
      }
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "admin", expect: ok, note: "Anheart admin reads the global current version" },
      { actor: "orgAdmin", expect: ok, note: "centre A reads the global current version" },
      { actor: "manager", expect: ok, note: "a manager reads the global current version" },
      { actor: "patient", expect: ok, note: "a patient reads the global current version" },
      { actor: "orgBAdmin", expect: ok, note: "centre B reads the global current version" },
      { actor: "orgBManager", expect: ok, note: "a centre B manager reads the global current version" },
      { actor: "orgBPatient", expect: ok, note: "a centre B patient reads the global current version" },
    ],
  },

  // =========================================================================
  // organizations.ts
  // =========================================================================
  {
    id: "organizations.listOrganizations",
    ref: api.organizations.listOrganizations,
    kind: "query",
    build: async () => ({}),
    onSuccess: (res, w) => {
      expectExactly(
        ids(res),
        [w.anheartOrg, w.orgA, w.orgB],
        "Organisations listed to the Anheart admin",
      );
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "patient", expect: empty, note: "[]: only the Anheart admin creates machines" },
      { actor: "manager", expect: empty, note: "[]: only the Anheart admin creates machines" },
      { actor: "admin", expect: ok, note: "every organisation, to create a machine in" },
      { actor: "orgAdmin", expect: empty, note: "[]: an organisation admin works in their own" },
      { actor: "orgBAdmin", expect: empty, note: "[]: centre B lists no organisation" },
    ],
  },

  // =========================================================================
  // dashboard.ts
  // =========================================================================
  {
    id: "dashboard.counters",
    ref: api.dashboard.counters,
    kind: "query",
    build: async (w) => {
      await seedCountedWorld(w);
      return {};
    },
    onSuccess: (res, w, actor) => expectCounters(res, actor),
    onFiltered: (res, w, actor) => expectCounters(res, actor),
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "admin", expect: ok, note: "members, machines and sessions of every organisation" },
      { actor: "orgAdmin", scope: "own", expect: filtered, note: "members, machines and sessions of centre A only" },
      { actor: "manager", scope: "own", expect: filtered, note: "their patients, machines and sessions; no member count" },
      { actor: "otherManager", scope: "own", expect: filtered, note: "their patients, machines and sessions; no member count" },
      { actor: "patient", scope: "self", expect: filtered, note: "their own sessions only; no machine, no member" },
      { actor: "otherPatient", scope: "self", expect: filtered, note: "their own sessions only" },
      { actor: "stranger", scope: "self", expect: filtered, note: "no session of their own: zero" },
      { actor: "orgBAdmin", scope: "own", expect: filtered, note: "members, machines and sessions of centre B only" },
      { actor: "orgBManager", scope: "own", expect: filtered, note: "their patient, machine and session of centre B" },
      { actor: "orgBPatient", scope: "self", expect: filtered, note: "their own session of centre B only" },
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
    // ANH-183: the handler once returned the stored document, system field
    // `_creationTime` included, which its `returns` validator does not declare:
    // the read threw for every authorised reader. An authorised reader now
    // gets the summary with its downsampled ECG, and the declared fields only.
    onSuccess: (res) => {
      if (res === null) throw new Error("Expected a summary");
      const summary = res as Record<string, unknown>;
      expectExactly(
        Object.keys(summary).filter((key) => summary[key] !== undefined),
        ["_id", "createdAt", "downsampledEcg", "duration", "metrics", "sessionId"],
        "Fields of the summary with its ECG",
      );
      const ecg = JSON.stringify(summary.downsampledEcg);
      if (ecg !== JSON.stringify([{ timestamp: NOW, value: 1 }]))
        throw new Error("Expected the downsampled ECG of the summary");
      if (summary.duration !== 600)
        throw new Error("Expected the duration of the summary");
    },
    cases: summaryCases(),
  },

  // =========================================================================
  // sessionReports.ts  (ANH-89: summaries of training sessions, health data)
  // =========================================================================
  {
    id: "sessionReports.getSummary",
    ref: api.sessionReports.getSummary,
    kind: "query",
    // The rule of every read of a session (`canAccessSession`): its rider,
    // whoever can access its machine in its organisation, the Anheart admin.
    build: async (w) => ({
      sessionId: await addSummarizedSession(w, w.machine, w.patient),
    }),
    onSuccess: (res) => {
      const summary = res as {
        provisional: boolean;
        figures: { durationS: number | null };
      } | null;
      if (summary === null) throw new Error("Expected the summary");
      if (summary.provisional || summary.figures.durationS !== 600)
        throw new Error("Expected the final summary of the session");
    },
    cases: summaryCases(),
  },
  {
    id: "sessionReports.riderHistory",
    ref: api.sessionReports.riderHistory,
    kind: "query",
    // One finished session with its summary per machine (`patient` on
    // `machine`, `otherPatient` on `otherMachine`, `orgBPatient` on
    // `orgBMachine`). The rider asked for is `patient`, by identifier, unless
    // the cell asks for the caller's own history or for another rider.
    build: async (w, actor, scope) => {
      await addSummarizedSession(w, w.machine, w.patient);
      await addSummarizedSession(w, w.otherMachine, w.otherPatient);
      await addSummarizedSession(w, w.orgBMachine, w.orgBPatient);
      if (scope === "self") return { userId: userId(w, actor) };
      if (actor === "patient" && scope === "other")
        return { userId: w.otherPatient };
      return { userId: w.patient };
    },
    onSuccess: (res) => {
      const rows = asArray(res) as { summary: unknown }[];
      if (rows.length !== 1 || rows[0].summary === null)
        throw new Error("Expected the rider's session with its summary");
    },
    onFiltered: (res, w, actor, scope) => {
      const rider = scope === "self" ? userId(w, actor) : w.patient;
      const rows = asArray(res) as { sessionId: Id<"sessions">; summary: unknown }[];
      if (rows.length !== 1 || rows[0].summary === null)
        throw new Error("Expected exactly one session, with its summary");
      return w.t.run(async (ctx) => {
        const session = await ctx.db.get(rows[0].sessionId);
        if (session?.userId !== rider)
          throw new Error(`History of ${actor} lists another rider's session`);
      });
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "admin", expect: ok, note: "any rider, in every organisation" },
      { actor: "orgAdmin", scope: "own", expect: filtered, note: "a rider of their organisation" },
      { actor: "manager", scope: "own", expect: filtered, note: "the sessions on the machine they manage" },
      { actor: "otherManager", scope: "other", expect: empty, note: "[] for a rider who rode none of their machines" },
      { actor: "patient", scope: "self", expect: filtered, note: "their own history" },
      { actor: "patient", scope: "other", expect: empty, note: "[] for another rider" },
      { actor: "stranger", scope: "other", expect: empty, note: "[] for a rider they have nothing to do with" },
      { actor: "orgBAdmin", scope: "foreign", expect: empty, note: "[] for a rider of another organisation" },
      { actor: "orgBManager", scope: "foreign", expect: empty, note: "[] for a rider of another organisation" },
      { actor: "orgBPatient", scope: "foreign", expect: empty, note: "[] for a rider of another organisation" },
      { actor: "orgBPatient", scope: "self", expect: filtered, note: "their own history, in centre B" },
    ],
  },

  // =========================================================================
  // notifications.ts (ANH-90): the caller's own alerts, in their organisation
  // =========================================================================
  {
    id: "notifications.list",
    ref: api.notifications.list,
    kind: "query",
    build: async (w) => {
      await seedAlerts(w);
      return {};
    },
    onSuccess: (res, w, actor) => expectAlertsOf(res, w, actor),
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "manager", scope: "own", expect: ok, note: "the alert of the machine they manage" },
      { actor: "orgAdmin", scope: "own", expect: ok, note: "the alert of a machine of their organisation" },
      { actor: "otherManager", scope: "other", expect: empty, note: "no alert of a machine they do not manage" },
      { actor: "patient", scope: "self", expect: empty, note: "a patient receives no alert" },
      { actor: "admin", expect: empty, note: "the Anheart team's alerts are ANH-150's" },
      { actor: "orgBManager", scope: "own", expect: ok, note: "their centre B alert only" },
      { actor: "orgBAdmin", scope: "own", expect: ok, note: "their centre B alert only" },
      { actor: "orgBPatient", scope: "self", expect: empty, note: "a patient receives no alert" },
    ],
  },
  {
    id: "notifications.unreadCount",
    ref: api.notifications.unreadCount,
    kind: "query",
    build: async (w) => {
      await seedAlerts(w);
      return {};
    },
    onSuccess: (res, _w, actor) => {
      const expected = actor === "otherManager" ? 0 : 1;
      if (res !== expected)
        throw new Error(`Expected ${expected} unread alerts, got ${String(res)}`);
    },
    cases: [
      { actor: "anonymous", expect: empty, note: "null: the header asks signed out too" },
      { actor: "manager", scope: "own", expect: ok, note: "1: their alert" },
      { actor: "otherManager", scope: "other", expect: ok, note: "0: none of another machine" },
      { actor: "patient", scope: "self", expect: empty, note: "null: a patient receives no alert" },
      { actor: "orgBManager", scope: "own", expect: ok, note: "1: their centre B alert only" },
      { actor: "orgBPatient", scope: "self", expect: empty, note: "null: a patient receives no alert" },
    ],
  },
  {
    id: "notifications.markRead",
    ref: api.notifications.markRead,
    kind: "mutation",
    build: async (w, actor, scope) => {
      await seedAlerts(w);
      // "own": the actor's alert; anything else: the manager's, in centre A.
      const owner = scope === "own" ? userId(w, actor) : w.manager;
      return { notificationId: await alertOf(w, owner) };
    },
    onSuccess: async (_res, w, _actor, _scope, args) => {
      const row = await w.t.run((ctx) =>
        ctx.db.get(args.notificationId as Id<"notifications">),
      );
      if (row?.read !== true) throw new Error("Alert not marked as read");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "manager", scope: "own", expect: ok, note: "their own alert" },
      { actor: "orgAdmin", scope: "own", expect: ok, note: "their own alert" },
      { actor: "orgAdmin", scope: "other", expect: refuse(/notification_not_found/), note: "another account's alert, same organisation" },
      { actor: "otherManager", scope: "other", expect: refuse(/notification_not_found/), note: "another account's alert" },
      { actor: "admin", scope: "other", expect: refuse(/notification_not_found/), note: "even the Anheart admin reads only their own" },
      { actor: "patient", scope: "other", expect: refuse(UNAUTHORIZED), note: "a patient has no alert" },
      { actor: "orgBManager", scope: "foreign", expect: refuse(/notification_not_found/), note: "centre A alert, by identifier" },
      { actor: "orgBAdmin", scope: "foreign", expect: refuse(/notification_not_found/), note: "centre A alert, by identifier" },
    ],
  },
  {
    id: "notifications.markAllRead",
    ref: api.notifications.markAllRead,
    kind: "mutation",
    build: async (w) => {
      await seedAlerts(w);
      return {};
    },
    onSuccess: async (res, w, actor) => {
      if ((res as { marked?: unknown }).marked !== 1)
        throw new Error("Expected one alert marked");
      const unread = await w.t.run(async (ctx) =>
        (await ctx.db.query("notifications").collect())
          .filter((row) => !row.read)
          .map((row) => row.userId),
      );
      if (unread.includes(userId(w, actor)))
        throw new Error("An alert of the caller is still unread");
      if (unread.length !== 3)
        throw new Error("Another account's alert was marked");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "manager", scope: "own", expect: ok, note: "their alert only" },
      { actor: "patient", scope: "self", expect: refuse(UNAUTHORIZED), note: "a patient has no alert" },
      { actor: "orgBManager", scope: "own", expect: ok, note: "their centre B alert only, none of centre A" },
    ],
  },
  {
    id: "notifications.getSettings",
    ref: api.notifications.getSettings,
    kind: "query",
    build: async () => ({}),
    onSuccess: (res) => {
      const settings = res as { emailAvailable: boolean; email: Record<string, boolean> };
      if (settings.emailAvailable !== false)
        throw new Error("No e-mail can be sent without its three variables");
      if (settings.email.drive_fault !== true || settings.email.machine_offline !== false)
        throw new Error("Expected the default choices");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "manager", scope: "self", expect: ok, note: "their own choices" },
      { actor: "orgAdmin", scope: "self", expect: ok, note: "their own choices" },
      { actor: "patient", scope: "self", expect: empty, note: "null: a patient receives no alert" },
      { actor: "orgBManager", scope: "self", expect: ok, note: "their own choices" },
    ],
  },
  {
    id: "notifications.updateSettings",
    ref: api.notifications.updateSettings,
    kind: "mutation",
    build: async () => ({ type: "machine_offline", email: true }),
    onSuccess: async (_res, w, actor) => {
      const rows = await w.t.run((ctx) =>
        ctx.db.query("notification_settings").collect(),
      );
      if (rows.length !== 1 || rows[0].userId !== userId(w, actor))
        throw new Error("Expected the caller's choice only");
      if (rows[0].email.machine_offline !== true)
        throw new Error("Choice not recorded");
    },
    cases: [
      { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
      { actor: "manager", scope: "self", expect: ok, note: "their own choice" },
      { actor: "orgAdmin", scope: "self", expect: ok, note: "their own choice" },
      { actor: "patient", scope: "self", expect: refuse(UNAUTHORIZED), note: "a patient receives no alert" },
      { actor: "orgBManager", scope: "self", expect: ok, note: "their own choice" },
    ],
  },
  {
    id: "fitness.submitFitnessAnswers",
    ref: api.fitness.submitFitnessAnswers,
    kind: "mutation",
    build: async (w, actor, scope) => {
      const ownUserId = actor === "anonymous" ? undefined : userId(w, actor);
      const targetId =
        scope === "self" && actor !== "anonymous"
          ? userId(w, actor)
          : scope === "other" && actor === "patient"
            ? w.otherPatient
            : w.patient;
      return {
        ...(targetId === ownUserId ? {} : { userId: targetId }),
        questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
        answers: fitAnswers(),
      };
    },
    onSuccess: async (res, w, actor, scope) => {
      const result = res as {
        assessmentId: Id<"fitness_assessments">;
        outcome: string;
      };
      const targetId =
        scope === "self" && actor !== "anonymous"
          ? userId(w, actor)
          : scope === "other"
            ? w.otherPatient
            : w.patient;
      const row = await w.t.run((ctx) => ctx.db.get(result.assessmentId));
      if (
        !row ||
        row.userId !== targetId ||
        row.submittedBy !== userId(w, actor) ||
        row.outcome !== result.outcome ||
        row.validatedAt !== undefined
      ) {
        throw new Error(
          "Fitness answers were not submitted as a new assessment",
        );
      }
    },
    cases: [
      {
        actor: "anonymous",
        expect: refuse(NOT_AUTH),
        note: "sign-in required",
      },
      {
        actor: "patient",
        scope: "self",
        expect: ok,
        note: "a passenger submits for themselves",
      },
      {
        actor: "otherPatient",
        scope: "self",
        expect: ok,
        note: "another passenger submits for themselves",
      },
      {
        actor: "patient",
        scope: "other",
        expect: refuse(/user_not_managed/),
        note: "a passenger cannot submit for another",
      },
      {
        actor: "manager",
        scope: "own",
        expect: ok,
        note: "their manager submits for them",
      },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/user_not_managed/),
        note: "an unrelated manager cannot submit",
      },
      {
        actor: "stranger",
        scope: "other",
        expect: refuse(/user_not_managed/),
        note: "an unrelated user cannot submit",
      },
      {
        actor: "admin",
        scope: "own",
        expect: ok,
        note: "the Anheart admin submits for a centre A passenger",
      },
      {
        actor: "orgAdmin",
        scope: "own",
        expect: ok,
        note: "the centre A admin submits for a passenger",
      },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/user_not_managed/),
        note: "centre B cannot submit for centre A",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/user_not_managed/),
        note: "centre B cannot submit for centre A",
      },
      {
        actor: "orgBPatient",
        scope: "foreign",
        expect: refuse(/user_not_managed/),
        note: "a centre B passenger cannot submit for centre A",
      },
    ],
  },
  {
    id: "fitness.validateFitnessAssessment",
    ref: api.fitness.validateFitnessAssessment,
    kind: "mutation",
    build: async (w) => ({
      assessmentId: await w.t.run((ctx) =>
        ctx.db.insert("fitness_assessments", {
          userId: w.patient,
          organizationId: w.orgA,
          questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
          answers: fitAnswers(),
          outcome: "fit",
          submittedBy: w.patient,
          submittedAt: NOW,
        }),
      ),
      validUntil: NOW + 60_000,
      notes: "Reviewed by synthetic manager",
    }),
    onSuccess: async (_res, w, actor, _scope, args) => {
      const row = await w.t.run((ctx) =>
        ctx.db.get(args.assessmentId as Id<"fitness_assessments">),
      );
      if (
        row?.validatedBy !== userId(w, actor) ||
        row.validatedAt === undefined ||
        row.validUntil !== args.validUntil ||
        row.outcome !== "fit"
      ) {
        throw new Error(
          "Assessment validation changed its outcome or missed its review",
        );
      }
    },
    cases: [
      {
        actor: "anonymous",
        expect: refuse(NOT_AUTH),
        note: "sign-in required",
      },
      {
        actor: "patient",
        scope: "self",
        expect: refuse(/fitness_self_validation/),
        note: "a passenger cannot validate themselves",
      },
      {
        actor: "otherPatient",
        scope: "other",
        expect: refuse(/user_not_managed/),
        note: "a passenger cannot validate another",
      },
      {
        actor: "manager",
        scope: "own",
        expect: ok,
        note: "the passenger's manager validates",
      },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/user_not_managed/),
        note: "an unrelated manager cannot validate",
      },
      {
        actor: "admin",
        scope: "own",
        expect: ok,
        note: "the Anheart admin validates across organisations",
      },
      {
        actor: "orgAdmin",
        scope: "own",
        expect: ok,
        note: "the passenger's organisation admin validates",
      },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/fitness_assessment_not_found/),
        note: "centre B cannot access a centre A assessment",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/fitness_assessment_not_found/),
        note: "centre B cannot access a centre A assessment",
      },
      {
        actor: "orgBPatient",
        scope: "foreign",
        expect: refuse(/fitness_assessment_not_found/),
        note: "a centre B passenger cannot access a centre A assessment",
      },
    ],
  },
  {
    id: "fitness.signConsent",
    ref: api.fitness.signConsent,
    kind: "mutation",
    build: async (_w, _actor, scope) => ({
      textVersion:
        scope === "foreign"
          ? `${CONSENT_TEXT_VERSION}-outdated`
          : CONSENT_TEXT_VERSION,
    }),
    onSuccess: async (res, w, actor) => {
      const row = await w.t.run((ctx) => ctx.db.get(res as Id<"consents">));
      if (
        row?.userId !== userId(w, actor) ||
        row.organizationId !== organizationOf(w, actor) ||
        row.textVersion !== CONSENT_TEXT_VERSION ||
        row.signedVia !== "site"
      ) {
        throw new Error(
          "Consent was not signed for the caller in their organisation",
        );
      }
    },
    cases: [
      {
        actor: "anonymous",
        expect: refuse(NOT_AUTH),
        note: "sign-in required",
      },
      {
        actor: "patient",
        scope: "self",
        expect: ok,
        note: "a passenger signs their own consent",
      },
      {
        actor: "otherPatient",
        scope: "self",
        expect: ok,
        note: "another passenger signs their own consent",
      },
      {
        actor: "manager",
        scope: "self",
        expect: ok,
        note: "a manager signs their own consent",
      },
      {
        actor: "orgAdmin",
        scope: "self",
        expect: ok,
        note: "an organisation admin signs their own consent",
      },
      {
        actor: "admin",
        scope: "self",
        expect: ok,
        note: "the Anheart admin signs their own consent",
      },
      {
        actor: "orgBAdmin",
        scope: "self",
        expect: ok,
        note: "a centre B admin signs their own consent",
      },
      {
        actor: "orgBManager",
        scope: "self",
        expect: ok,
        note: "a centre B manager signs their own consent",
      },
      {
        actor: "orgBPatient",
        scope: "self",
        expect: ok,
        note: "a centre B passenger signs their own consent",
      },
      {
        actor: "orgBPatient",
        scope: "foreign",
        expect: refuse(/consent_text_outdated/),
        note: "a text version outside the current contract is refused",
      },
    ],
  },
  {
    id: "fitness.withdrawConsent",
    ref: api.fitness.withdrawConsent,
    kind: "mutation",
    build: async (w, actor, scope) => {
      const targetId = scope === "self" ? userId(w, actor) : w.patient;
      const organizationId =
        scope === "self" ? organizationOf(w, actor) : w.orgA;
      await w.t.run((ctx) =>
        ctx.db.insert("consents", {
          userId: targetId,
          organizationId,
          textVersion: CONSENT_TEXT_VERSION,
          signedAt: NOW,
          signedVia: "site",
        }),
      );
      return scope === "self" ? {} : { userId: targetId };
    },
    onSuccess: async (_res, w, actor, scope) => {
      const targetId = scope === "self" ? userId(w, actor) : w.patient;
      const organizationId =
        scope === "self" ? organizationOf(w, actor) : w.orgA;
      const rows = await w.t.run((ctx) =>
        ctx.db
          .query("consents")
          .withIndex("by_user_and_organization", (q) =>
            q.eq("userId", targetId).eq("organizationId", organizationId),
          )
          .collect(),
      );
      if (
        rows.length === 0 ||
        rows.some(
          (row) =>
            row.withdrawnAt === undefined || row.withdrawnBy === undefined,
        )
      ) {
        throw new Error("Consent withdrawal did not mark every active consent");
      }
    },
    cases: [
      {
        actor: "anonymous",
        expect: refuse(NOT_AUTH),
        note: "sign-in required",
      },
      {
        actor: "patient",
        scope: "self",
        expect: ok,
        note: "a passenger withdraws their own consent",
      },
      {
        actor: "manager",
        scope: "self",
        expect: ok,
        note: "a manager withdraws their own consent",
      },
      {
        actor: "orgAdmin",
        scope: "self",
        expect: ok,
        note: "an organisation admin withdraws their own consent",
      },
      {
        actor: "admin",
        scope: "self",
        expect: ok,
        note: "the Anheart admin withdraws their own consent",
      },
      {
        actor: "orgBAdmin",
        scope: "self",
        expect: ok,
        note: "a centre B admin withdraws their own consent",
      },
      {
        actor: "orgBManager",
        scope: "self",
        expect: ok,
        note: "a centre B manager withdraws their own consent",
      },
      {
        actor: "manager",
        scope: "own",
        expect: ok,
        note: "a manager withdraws their passenger's consent",
      },
      {
        actor: "otherManager",
        scope: "other",
        expect: refuse(/user_not_managed/),
        note: "an unrelated manager cannot withdraw",
      },
      {
        actor: "otherPatient",
        scope: "other",
        expect: refuse(/user_not_managed/),
        note: "another passenger cannot withdraw",
      },
      {
        actor: "admin",
        scope: "own",
        expect: ok,
        note: "the Anheart admin withdraws for a centre A passenger",
      },
      {
        actor: "orgAdmin",
        scope: "own",
        expect: ok,
        note: "the centre A admin withdraws for a passenger",
      },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: refuse(/user_not_managed/),
        note: "centre B cannot withdraw for centre A",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: refuse(/user_not_managed/),
        note: "centre B cannot withdraw for centre A",
      },
      {
        actor: "orgBPatient",
        scope: "foreign",
        expect: refuse(/user_not_managed/),
        note: "a centre B passenger cannot withdraw for centre A",
      },
    ],
  },
  {
    id: "fitness.getRiderReadiness",
    ref: api.fitness.getRiderReadiness,
    kind: "query",
    build: async (w) => {
      await makeRiderReady(w.t, w.patient, w.orgA, {
        now: NOW,
        validUntil: NOW + 60_000,
      });
      return { userId: w.patient };
    },
    onSuccess: (res, _w, actor) => {
      const readiness = res as {
        serverNow: number;
        fitness: { answers?: Record<string, boolean>; notes?: string } | null;
        launchRefusal: string | null;
        canValidate: boolean;
      };
      if (
        typeof readiness.serverNow !== "number" ||
        readiness.launchRefusal !== null
      ) {
        throw new Error("Expected current rider readiness");
      }
      if (actor === "patient") {
        if (
          readiness.fitness?.answers !== undefined ||
          readiness.fitness?.notes !== undefined ||
          readiness.canValidate
        ) {
          throw new Error("Passenger received answers or review notes");
        }
      } else if (
        !readiness.fitness?.answers ||
        readiness.fitness.notes !== "Synthetic review note" ||
        !readiness.canValidate
      ) {
        throw new Error(
          "Manager did not receive fitness answers and review notes",
        );
      }
      if (
        readiness.fitness?.answers &&
        Object.keys(readiness.fitness.answers).length !==
          CURRENT_QUESTIONNAIRE.questions.length
      ) {
        throw new Error("Manager received incomplete fitness answers");
      }
      if (actor === "admin" && !readiness.canValidate) {
        throw new Error(
          "Anheart admin did not resolve the passenger's primary organisation",
        );
      }
    },
    cases: [
      {
        actor: "anonymous",
        expect: refuse(NOT_AUTH),
        note: "sign-in required",
      },
      {
        actor: "patient",
        scope: "self",
        expect: ok,
        note: "passenger sees status without answers or notes",
      },
      {
        actor: "otherPatient",
        scope: "other",
        expect: empty,
        note: "another passenger receives null",
      },
      {
        actor: "manager",
        scope: "own",
        expect: ok,
        note: "the manager sees answers and notes",
      },
      {
        actor: "otherManager",
        scope: "other",
        expect: empty,
        note: "an unrelated manager receives null",
      },
      {
        actor: "admin",
        scope: "own",
        expect: ok,
        note: "the Anheart admin sees a centre A passenger's data",
      },
      {
        actor: "orgAdmin",
        scope: "own",
        expect: ok,
        note: "the centre A admin sees the passenger's data",
      },
      {
        actor: "orgBAdmin",
        scope: "foreign",
        expect: empty,
        note: "centre B receives null for a centre A passenger",
      },
      {
        actor: "orgBManager",
        scope: "foreign",
        expect: empty,
        note: "centre B receives null for a centre A passenger",
      },
      {
        actor: "orgBPatient",
        scope: "foreign",
        expect: empty,
        note: "a centre B passenger receives null for centre A",
      },
    ],
  },
];

function registryTarget(w: World, actor: Actor, scope: Scope) {
  return actor === "orgAdmin" && scope === "foreign" ? w.orgBMachine : w.machine;
}

function registryReadCases(): Case[] {
  return [
    { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
    { actor: "admin", scope: "own", expect: ok, note: "Anheart reads every machine" },
    { actor: "orgAdmin", scope: "own", expect: ok, note: "organisation admin reads their registry" },
    { actor: "orgAdmin", scope: "foreign", expect: empty, note: "another organisation is invisible" },
    ...(["manager", "otherManager", "patient", "otherPatient", "stranger"] as const)
      .map((actor) => ({ actor, scope: "own" as const, expect: empty, note: "registry not visible to this role" })),
    ...(["orgBAdmin", "orgBManager", "orgBPatient"] as const)
      .map((actor) => ({ actor, scope: "foreign" as const, expect: empty, note: "centre A is invisible from centre B" })),
  ];
}

function registryMutationEntries(): Entry[] {
  const entries: Array<Omit<Entry, "cases" | "kind">> = [
    {
      id: "machineRegistry.generateUploadUrl", ref: api.machineRegistry.generateUploadUrl,
      build: async () => ({}),
      onSuccess: (res) => {
        if (typeof res !== "string" || !/^https?:\/\//.test(res)) throw new Error("Upload URL missing");
      },
    },
    {
      id: "machineRegistry.updateIdentity", ref: api.machineRegistry.updateIdentity,
      build: async (w) => ({ machineId: w.machine, serialPi: " TEST-PI-0001 ", contacts: [w.manager] }),
      onSuccess: async (_res, w) => {
        const row = await registryRow(w);
        if (row?.serialPi !== "TEST-PI-0001" || row.contacts.join() !== w.manager || row.updatedBy !== w.admin || row.organizationId !== w.orgA)
          throw new Error("Identity or attribution not stored");
      },
    },
    {
      id: "machineRegistry.recordRadii", ref: api.machineRegistry.recordRadii,
      build: async (w) => ({ machineId: w.machine, armRadiusM: 1.234, legTipRadiusM: 2.345, measuredAt: Date.now(), measuredBy: "TEST mesureur" }),
      onSuccess: async (_res, w, _actor, _scope, args) => {
        const row = await registryRow(w);
        if (row?.armRadiusM !== 1.234 || row.legTipRadiusM !== 2.345 || row.radiiMeasuredBy !== "TEST mesureur" || row.radiiMeasuredAt !== args.measuredAt)
          throw new Error("Measurements not stored");
      },
    },
    {
      id: "machineRegistry.attachDriveParameterSheet", ref: api.machineRegistry.attachDriveParameterSheet,
      build: async (w) => ({ machineId: w.machine, storageId: await registryUpload(w), version: "TEST-v1", signedBy: "TEST signataire", at: Date.now() }),
      onSuccess: async (_res, w, _actor, _scope, args) => {
        const row = await registryRow(w);
        if (!row?.driveParameterSheet || row.driveParameterSheet.storageId !== args.storageId || row.driveParameterSheet.attachedBy !== w.admin || row.driveParameterSheet.version !== "TEST-v1")
          throw new Error("Sheet not attached");
      },
    },
    {
      id: "machineRegistry.addValidationReview", ref: api.machineRegistry.addValidationReview,
      build: async (w) => {
        await registryRelease(w);
        return { machineId: w.machine, level: "bench", reportStorageId: await registryUpload(w), softwareVersion: "pi-0.1.0", medicalParametersVersion: null };
      },
      onSuccess: async (_res, w, _actor, _scope, args) => {
        const row = await registryRow(w);
        if (row?.validation.level !== "bench" || row.validation.reviews.length !== 1 || row.validation.reviews[0].reportStorageId !== args.reportStorageId || row.validation.reviews[0].by !== w.admin)
          throw new Error("Review did not raise the level with its report");
      },
    },
    {
      id: "machineRegistry.lowerValidationLevel", ref: api.machineRegistry.lowerValidationLevel,
      build: async (w) => {
        await registryRelease(w);
        await as(w.t, "admin").mutation(api.machineRegistry.addValidationReview, {
          machineId: w.machine, level: "bench", reportStorageId: await registryUpload(w), softwareVersion: "pi-0.1.0", medicalParametersVersion: null,
        });
        return { machineId: w.machine, level: "none", reason: "TEST: nouvel essai requis" };
      },
      onSuccess: async (_res, w) => {
        const row = await registryRow(w);
        if (row?.validation.level !== "none" || row.validation.reviews.length !== 1 || row.validation.downgrades[0]?.by !== w.admin || row.validation.downgrades[0]?.reason !== "TEST: nouvel essai requis")
          throw new Error("Downgrade did not preserve its author, reason and review");
      },
    },
  ];
  return entries.map((entry) => ({ ...entry, kind: "mutation", cases: [
    { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
    { actor: "admin", expect: ok, note: "only Anheart admins write the registry" },
    ...(["orgAdmin", "manager", "otherManager", "patient", "otherPatient", "stranger"] as const)
      .map((actor) => ({ actor, scope: "own" as const, expect: refuse(UNAUTHORIZED), note: "not an Anheart admin" })),
    ...(["orgBAdmin", "orgBManager", "orgBPatient"] as const)
      .map((actor) => ({ actor, ...(entry.id === "machineRegistry.generateUploadUrl" ? {} : { scope: "foreign" as const }), expect: refuse(UNAUTHORIZED), note: "not an Anheart admin" })),
  ] }));
}

async function registryRow(w: World) {
  return await w.t.run((ctx) => ctx.db.query("machine_registry").withIndex("by_machine", (q) => q.eq("machineId", w.machine)).unique());
}

/**
 * A completed auto session of `riderId` on `machineId`, with the final
 * summary of its record: ten minutes, in a 130-150 bpm zone.
 */
async function addSummarizedSession(
  w: World,
  machineId: Id<"machines">,
  riderId: Id<"users">,
): Promise<Id<"sessions">> {
  const sessionId = await addSession(w, {
    machineId,
    userId: riderId,
    status: "completed",
    kind: "auto",
  });
  await w.t.run(async (ctx) => {
    const session = await ctx.db.get(sessionId);
    await ctx.db.insert("session_summaries_v2", {
      organizationId: session?.organizationId,
      sessionId,
      source: "record",
      computedAt: NOW,
      figures: {
        durationS: 600,
        samples: 3000,
        heartRate: { minBpm: 90, meanBpm: 138.5, maxBpm: 152, samples: 2900 },
        zone: { lowBpm: 130, highBpm: 150, inZoneS: 420, aboveZoneS: 12, belowZoneS: 140 },
        maxOutputRpm: 27.5,
        maxMotorRpm: 1369,
        reference: { maxG: 1.2, maxGRate: 0.02, radiusM: null },
        legTip: { maxG: 1.5, maxGRate: 0.025, radiusM: null, measured: null },
        verdicts: [],
        verdictsOmitted: 0,
        endReason: "programme_complete",
        endRule: null,
        warnings: [],
      },
    });
  });
  return sessionId;
}

/**
 * A drive fault on `machine` (centre A) and on `orgBMachine` (centre B), as
 * the server writes it: an alert each for the machine's gestionnaire and the
 * admin of its organisation.
 */
async function seedAlerts(w: World) {
  await w.t.run(async (ctx) => {
    for (const machineId of [w.machine, w.orgBMachine]) {
      const machine = await ctx.db.get(machineId);
      if (machine === null) throw new Error("Missing machine");
      await notifyMachine(ctx, machine, "drive_fault");
    }
  });
}

/** The alert `seedAlerts` wrote for this account. */
async function alertOf(w: World, owner: Id<"users">) {
  const row = await w.t.run((ctx) =>
    ctx.db
      .query("notifications")
      .withIndex("by_user", (q) => q.eq("userId", owner))
      .first(),
  );
  // An account without one is handed another account's: refused all the same.
  if (row !== null) return row._id;
  const any = await w.t.run((ctx) => ctx.db.query("notifications").first());
  if (any === null) throw new Error("No alert seeded");
  return any._id;
}

/** The alerts of `seedAlerts` an actor is listed: the one of their machine. */
function expectAlertsOf(res: unknown, w: World, actor: Actor) {
  const machineOf: Partial<Record<Actor, Id<"machines">>> = {
    manager: w.machine,
    orgAdmin: w.machine,
    orgBManager: w.orgBMachine,
    orgBAdmin: w.orgBMachine,
  };
  const rows = asArray(res) as { machineId: string; type: string }[];
  if (
    rows.length !== 1 ||
    rows[0].machineId !== machineOf[actor] ||
    rows[0].type !== "drive_fault"
  )
    throw new Error("Expected exactly the alert of the caller's machine");
}

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

function summaryCases(): Case[] {
  return [
    { actor: "anonymous", expect: refuse(NOT_AUTH), note: "sign-in required" },
    { actor: "patient", scope: "own", expect: ok, note: "the rider" },
    { actor: "stranger", scope: "other", expect: empty, note: "null for an unrelated user" },
    { actor: "manager", scope: "own", expect: ok, note: "manages this machine" },
    { actor: "otherManager", scope: "other", expect: empty, note: "null for an unrelated machine" },
    { actor: "admin", expect: ok, note: "admin reads" },
    { actor: "orgAdmin", scope: "own", expect: ok, note: "admin of the session's organisation" },
    { actor: "orgBAdmin", scope: "foreign", expect: empty, note: "null for a session of another organisation" },
    { actor: "orgBManager", scope: "foreign", expect: empty, note: "null for a session of another organisation" },
    { actor: "orgBPatient", scope: "foreign", expect: empty, note: "null for a session of another organisation" },
  ];
}

/**
 * What `dashboard.counters` must count, on top of the seeded world: open
 * sessions in both centres, a completed one, a machine offline, one in a
 * session, a deleted machine and a removed member, none of the last three
 * counted by anyone.
 */
async function seedCountedWorld(w: World): Promise<void> {
  await addSession(w, { machineId: w.machine, userId: w.patient, status: "active", kind: "auto" });
  await addSession(w, { machineId: w.machine, userId: w.patient, status: "completed", kind: "auto" });
  await addSession(w, { machineId: w.otherMachine, userId: w.otherPatient, status: "pending", kind: "auto" });
  await addSession(w, { machineId: w.orgBMachine, userId: w.orgBPatient, status: "active", kind: "auto" });
  await w.t.run(async (ctx) => {
    await ctx.db.patch(w.otherMachine, { status: "offline" });
    await ctx.db.patch(w.orgBMachine, { status: "in_session" });
    await ctx.db.insert("machines", {
      organizationId: w.orgA,
      name: "Deleted machine",
      apiKey: "synthetic-hash",
      status: "offline",
      lastHeartbeat: NOW,
      createdAt: NOW,
      isDeleted: true,
    });
    const removed = await ctx.db.insert("users", {
      clerkId: "removed",
      role: "user",
      organizationId: w.orgA,
      firstName: "removed",
      lastName: "Synthetic",
      email: "removed@example.invalid",
      language: "en",
      createdAt: NOW,
    });
    await ctx.db.insert("memberships", {
      userId: removed,
      organizationId: w.orgA,
      role: "user",
      active: false,
    });
  });
}

type Counted = {
  members: { admin: number; gestionnaire: number; user: number } | null;
  patients: number | null;
  machines: { total: number; signals: string[] } | null;
  sessions: { active: number; pending: number };
};

/** What each actor is counted in the world of `seedCountedWorld`. */
const COUNTED: Partial<Record<Actor, Counted>> = {
  admin: {
    // Anheart: 1 admin. Centre A: 1 admin, 2 gestionnaires, 3 patients.
    // Centre B: 1 of each.
    members: { admin: 3, gestionnaire: 3, user: 4 },
    patients: null,
    machines: { total: 3, signals: ["in_session", "online"] },
    sessions: { active: 2, pending: 1 },
  },
  orgAdmin: {
    members: { admin: 1, gestionnaire: 2, user: 3 },
    patients: null,
    machines: { total: 2, signals: ["online"] },
    sessions: { active: 1, pending: 1 },
  },
  manager: {
    members: null,
    patients: 1,
    machines: { total: 1, signals: ["online"] },
    sessions: { active: 1, pending: 0 },
  },
  otherManager: {
    members: null,
    patients: 1,
    machines: { total: 1, signals: [] },
    sessions: { active: 0, pending: 1 },
  },
  patient: { members: null, patients: null, machines: null, sessions: { active: 1, pending: 0 } },
  otherPatient: { members: null, patients: null, machines: null, sessions: { active: 0, pending: 1 } },
  stranger: { members: null, patients: null, machines: null, sessions: { active: 0, pending: 0 } },
  orgBAdmin: {
    members: { admin: 1, gestionnaire: 1, user: 1 },
    patients: null,
    machines: { total: 1, signals: ["in_session"] },
    sessions: { active: 1, pending: 0 },
  },
  orgBManager: {
    members: null,
    patients: 1,
    machines: { total: 1, signals: ["in_session"] },
    sessions: { active: 1, pending: 0 },
  },
  orgBPatient: { members: null, patients: null, machines: null, sessions: { active: 1, pending: 0 } },
};

/** Fail unless the counters are exactly those of `COUNTED` for this actor. */
function expectCounters(res: unknown, actor: Actor): void {
  const expected = COUNTED[actor];
  if (expected === undefined) throw new Error(`No counters expected for ${actor}`);
  const answer = res as {
    serverNow: unknown;
    members: Counted["members"];
    patients: Counted["patients"];
    machines: { total: number; signals: { status: string }[] } | null;
    sessions: Counted["sessions"];
  };
  if (typeof answer.serverNow !== "number")
    throw new Error("The counters carry no server clock");
  const seen: Counted = {
    members: answer.members,
    patients: answer.patients,
    machines:
      answer.machines === null
        ? null
        : {
            total: answer.machines.total,
            signals: answer.machines.signals.map((s) => s.status).sort(),
          },
    sessions: answer.sessions,
  };
  if (!isDeepStrictEqual(seen, expected))
    throw new Error(
      `Counters of ${actor}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(seen)}`,
    );
}

/**
 * Coverage of the machine HTTP routes in `convex/http.ts`, used both to drive
 * `httpRoutes.test.ts` and to satisfy the completeness check. Authentication
 * refusals (missing header, malformed key, deleted/disabled machine,
 * regenerated key) across every route are proven by `machineAuth.test.ts`.
 */
export const ROUTE_COVERAGE: ReadonlyArray<{
  method: "GET" | "POST" | "OPTIONS";
  path: string;
  tests: string;
}> = [
  { method: "POST", path: "/clerk/webhook", tests: "clerkWebhook.test.ts (signature, timestamp, replay, organization and membership ordering)" },
  {method:"POST",path:"/api/machine/records/upload-url",tests:"recordStorage.test.ts (machine authentication, metadata, closed session); rateLimits.test.ts (limit per machine)"},
  {method:"POST",path:"/api/machine/records/commit",tests:"recordStorage.test.ts (machine authentication, idempotence, hash mismatch)"},
  {method:"GET",path:"/api/machine/records/status",tests:"recordStorage.test.ts (machine authentication, session ownership)"},
  {method:"GET",path:"/api/records/download",tests:"recordStorage.test.ts (Clerk identity, role and organisation)"},
  {method:"OPTIONS",path:"/api/records/download",tests:"recordStorage.test.ts (CORS preflight contains no data)"},
  { method: "POST", path: "/api/machine/heartbeat", tests: "machineAuth.test.ts; httpRoutes.test.ts; rateLimits.test.ts (limit per machine, 16 KiB, 1000 heartbeats in 10 s)" },
  { method: "GET", path: "/api/machine/training/poll", tests: "httpRoutes.test.ts (auto, this machine); rateLimits.test.ts (limit per machine); armingRecheck.test.ts (never a launch refused at arming, expired included)" },
  { method: "GET", path: "/api/machine/roster", tests: "httpRoutes.test.ts" },
  { method: "POST", path: "/api/machine/profiles", tests: "httpRoutes.test.ts (malformed body)" },
  { method: "POST", path: "/api/machine/training/start", tests: "httpRoutes.test.ts (non-pending refused); contract.test.ts and armingRecheck.test.ts (409 with the arming refusal's code)" },
  { method: "POST", path: "/api/machine/training/local", tests: "httpRoutes.test.ts (idempotent)" },
  { method: "POST", path: "/api/machine/training/end", tests: "httpRoutes.test.ts (idempotent)" },
  { method: "GET", path: "/api/machine/training/status", tests: "httpRoutes.test.ts; rateLimits.test.ts (never limited)" },
  { method: "POST", path: "/api/machine/training/telemetry", tests: "httpRoutes.test.ts (other machine refused); journalSync.test.ts (idempotent, session window); rateLimits.test.ts (limit per machine, body size, 600 points)" },
  { method: "POST", path: "/api/machine/training/events", tests: "httpRoutes.test.ts (other machine refused); journalSync.test.ts (idempotent, sizes, session window); rateLimits.test.ts (limit per machine, body size, largest batch)" },
];

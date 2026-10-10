/// <reference types="vite/client" />
/**
 * ANH-132: drives the authorization matrix. One test per cell of
 * `authorization.matrix.ts`. Each cell seeds a fresh in-memory world, acts as
 * the cell's actor, and asserts the behaviour (effect or returned data), not
 * the implementation. Cells marked as a known harmless defect run with
 * `it.fails`, asserting the intended policy so they flip the day it is fixed.
 *
 * ANH-114: the world holds three organisations and every actor carries the
 * organisation claims of a Clerk token, so each cell also proves that its
 * function answers inside the caller's organisation only.
 */
import { describe, expect, it } from "vitest";
import { api } from "./_generated/api";
import {
  MATRIX,
  type Entry,
  type Expectation,
} from "./authorization.matrix";
import {
  as,
  modules,
  NOW,
  seedWorld,
  type Claims,
  type World,
} from "./test.setup";

function isEmpty(res: unknown): boolean {
  return (
    res === null ||
    res === undefined ||
    (Array.isArray(res) && res.length === 0)
  );
}

function invoke(
  entry: Entry,
  reader: ReturnType<typeof as>,
  args: Record<string, unknown>,
): Promise<unknown> {
  // The matrix holds heterogeneous function references; the per-cell `build`
  // produces the matching args, so a single untyped dispatch is used here.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const r = reader as any;
  return entry.kind === "query"
    ? r.query(entry.ref, args)
    : r.mutation(entry.ref, args);
}

for (const entry of MATRIX) {
  describe(entry.id, () => {
    for (const c of entry.cases) {
      const scopeLabel = c.scope && c.scope !== "none" ? `/${c.scope}` : "";
      const title = `${c.actor}${scopeLabel}: ${c.expect.outcome} - ${c.note}`;
      const runner = c.knownDefect ? it.fails : it;
      runner(title, async () => {
        const w: World = await seedWorld(modules);
        const scope = c.scope ?? "none";
        const args = await entry.build(w, c.actor, scope);
        const claims = entry.claims?.(w, c.actor, scope) ?? null;
        const reader = as(w.t, c.actor, claims);
        const exp: Expectation = c.knownDefect ? c.knownDefect.intended : c.expect;

        if (exp.outcome === "refuse") {
          await expect(invoke(entry, reader, args)).rejects.toThrow(exp.message);
          return;
        }

        const res = await invoke(entry, reader, args);
        if (exp.outcome === "empty") {
          expect(isEmpty(res)).toBe(true);
        } else if (exp.outcome === "filtered") {
          expect(isEmpty(res)).toBe(false);
          await entry.onFiltered?.(res, w, c.actor, scope);
        } else {
          if (entry.onSuccess) {
            await entry.onSuccess(res, w, c.actor, scope, args);
          } else {
            expect(isEmpty(res)).toBe(false);
          }
        }
      });
    }
  });
}

describe("matrix identities", () => {
  it("acts as the row's actor when a cell adds claims", async () => {
    const w = await seedWorld(modules);
    const me = await as(w.t, "stranger", {
      email: "claims@example.invalid",
      emailVerified: true,
    }).query(api.users.getCurrentUser, {});
    expect(me?._id).toBe(w.stranger);
  });

  it("refuses claims for the anonymous actor", async () => {
    const w = await seedWorld(modules);
    expect(() =>
      as(w.t, "anonymous", { email: "claims@example.invalid" }),
    ).toThrow(/anonymous/);
  });

  it.each(["subject", "issuer", "tokenIdentifier", "org_id", "org_role"])(
    "refuses a claim that replaces the actor's %s",
    async (claim) => {
      const w = await seedWorld(modules);
      expect(() => as(w.t, "stranger", { [claim]: "admin" })).toThrow(
        /identity/,
      );
    },
  );
});

// users.linkPatientToClerk: the conditions of the verified-address rule that
// the role x resource cells above do not express. Each case seeds one
// pre-created record and calls as `stranger` with the given claims.
const TARGET = "link-target@example.invalid";
// TARGET with U+212A (KELVIN SIGN) in place of "k": another address.
const KELVIN_TARGET =
  "lin" + String.fromCharCode(0x212a) + "-target@example.invalid";

const linkCases: Array<{
  name: string;
  record: { email: string; clerkId: string };
  claims: Claims;
  argument: string;
  linked: boolean;
}> = [
  {
    name: "links when identity, argument and record differ only by ASCII case",
    record: { email: "Link-Target@Example.invalid", clerkId: "" },
    claims: { email: "LINK-TARGET@example.INVALID", emailVerified: true },
    argument: "link-TARGET@EXAMPLE.invalid",
    linked: true,
  },
  {
    name: "links when the addresses differ only by the case of the letter z",
    record: { email: "zeta-target@example.invalid", clerkId: "" },
    claims: { email: "ZETA-TARGET@example.invalid", emailVerified: true },
    argument: "Zeta-Target@example.invalid",
    linked: true,
  },
  {
    name: "does not fold the character that follows Z",
    record: { email: "link{target@example.invalid", clerkId: "" },
    claims: { email: "link[target@example.invalid", emailVerified: true },
    argument: "link[target@example.invalid",
    linked: false,
  },
  {
    name: "does not fold the character that precedes A",
    record: { email: "link-target`example.invalid", clerkId: "" },
    claims: { email: "link-target@example.invalid", emailVerified: true },
    argument: "link-target@example.invalid",
    linked: false,
  },
  {
    name: "refuses when email_verified is false",
    record: { email: TARGET, clerkId: "" },
    claims: { email: TARGET, emailVerified: false },
    argument: TARGET,
    linked: false,
  },
  {
    name: "refuses when the email_verified claim is absent",
    record: { email: TARGET, clerkId: "" },
    claims: { email: TARGET },
    argument: TARGET,
    linked: false,
  },
  {
    name: "refuses when the email claim is absent",
    record: { email: "", clerkId: "" },
    claims: { emailVerified: true },
    argument: "",
    linked: false,
  },
  {
    name: "refuses when the email claim is empty",
    record: { email: "", clerkId: "" },
    claims: { email: "", emailVerified: true },
    argument: "",
    linked: false,
  },
  {
    name: "refuses when the argument designates another address than the verified one",
    record: { email: TARGET, clerkId: "" },
    claims: { email: TARGET, emailVerified: true },
    argument: "link-other@example.invalid",
    linked: false,
  },
  {
    name: "never links a record that is already linked",
    record: { email: TARGET, clerkId: "existing-owner" },
    claims: { email: TARGET, emailVerified: true },
    argument: TARGET,
    linked: false,
  },
  {
    name: "does not fold a non-ASCII character of the verified address",
    record: { email: TARGET, clerkId: "" },
    claims: { email: KELVIN_TARGET, emailVerified: true },
    argument: TARGET,
    linked: false,
  },
  {
    name: "does not fold a non-ASCII character of the argument",
    record: { email: TARGET, clerkId: "" },
    claims: { email: TARGET, emailVerified: true },
    argument: KELVIN_TARGET,
    linked: false,
  },
  {
    name: "does not fold a non-ASCII character of the record's address",
    record: { email: KELVIN_TARGET, clerkId: "" },
    claims: { email: TARGET, emailVerified: true },
    argument: TARGET,
    linked: false,
  },
];

describe("users.linkPatientToClerk verified-address rule", () => {
  it.each(linkCases)("$name", async ({ record, claims, argument, linked }) => {
    const w = await seedWorld(modules);
    const recordId = await w.t.run((ctx) =>
      ctx.db.insert("users", {
        clerkId: record.clerkId,
        role: "user" as const,
        organizationId: w.orgA,
        firstName: "Pre",
        lastName: "Created",
        email: record.email,
        language: "en" as const,
        createdAt: NOW,
      }),
    );

    const result = await as(w.t, "stranger", claims).mutation(
      api.users.linkPatientToClerk,
      { email: argument },
    );

    const row = await w.t.run((ctx) => ctx.db.get(recordId));
    if (linked) {
      expect(result).toBe(recordId);
      expect(row?.clerkId).toBe("stranger");
    } else {
      expect(result).toBeNull();
      expect(row?.clerkId).toBe(record.clerkId);
    }
  });
});

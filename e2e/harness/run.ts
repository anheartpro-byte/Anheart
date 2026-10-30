import { readFileSync } from "node:fs";
import path from "node:path";
import { callAs, type ActingIdentity, type Backend } from "./convex";
import { writeNewFile } from "./processes";

/** Test organisations of a run. */
export const ORGANIZATIONS = {
  anheart: "Anheart",
  clientA: "Client A",
  clientB: "Client B",
} as const;
export type OrganizationKey = keyof typeof ORGANIZATIONS;

/** Test accounts of a run, one per role and organisation. */
export const ACCOUNTS = {
  admin: { organization: "anheart", role: "org:admin", firstName: "Adele", lastName: "Anheart" },
  orgAdminA: { organization: "clientA", role: "org:admin", firstName: "Bruno", lastName: "Arnaud" },
  gestionnaireA: { organization: "clientA", role: "org:gestionnaire", firstName: "Chloe", lastName: "Aubert" },
  passagerA: { organization: "clientA", role: "org:patient", firstName: "Damien", lastName: "Andre" },
  passagerMineurA: { organization: "clientA", role: "org:patient", firstName: "Emma", lastName: "Alard" },
  gestionnaireB: { organization: "clientB", role: "org:gestionnaire", firstName: "Fanny", lastName: "Blanc" },
  passagerB: { organization: "clientB", role: "org:patient", firstName: "Gabriel", lastName: "Bertin" },
} as const satisfies Record<
  string,
  { organization: OrganizationKey; role: string; firstName: string; lastName: string }
>;
export type AccountKey = keyof typeof ACCOUNTS;

export type Account = (typeof ACCOUNTS)[AccountKey] & {
  userId: string;
  email: string;
  organizationId: string;
  /** Browser state of this account, signed in with its organisation active. */
  storageState: string;
};

/** What the runner hands to the tests: written 0600 in the run folder. */
export type RunInfo = {
  runId: string;
  runDir: string;
  repoRoot: string;
  baseURL: string;
  issuer: string;
  backend: Backend;
  organizations: Record<OrganizationKey, string>;
  accounts: Record<AccountKey, Account>;
  /**
   * A machine and its key, made by the harness for the console: created by the
   * Anheart admin without an organisation, so it belongs to Anheart's.
   */
  machine: { id: string; apiKey: string };
  /** The run's Clerk webhook secret, for an account a test adds (`harness/members.ts`). */
  webhookSecret: string;
};

export function writeRunInfo(info: RunInfo): void {
  writeNewFile(path.join(info.runDir, "run.json"), JSON.stringify(info));
}

export function readRunInfo(): RunInfo {
  const runDir = process.env.E2E_RUN_DIR;
  if (!runDir) throw new Error("E2E_RUN_DIR absent : lancer par npm run test:e2e.");
  return JSON.parse(readFileSync(path.join(runDir, "run.json"), "utf8")) as RunInfo;
}

export function identityOf(info: RunInfo, key: AccountKey): ActingIdentity {
  const account = info.accounts[key];
  return {
    subject: account.userId,
    issuer: info.issuer,
    org_id: account.organizationId,
    org_role: account.role,
    email: account.email,
    email_verified: true,
  };
}

/** The Convex user row of an account (`users._id`), as the site's URLs name it. */
export async function convexUserId(info: RunInfo, key: AccountKey): Promise<string> {
  const me = await callAs<{ _id: string } | null>(
    info.backend,
    identityOf(info, key),
    "query",
    "users:getCurrentUser",
    {},
  );
  if (!me) throw new Error(`${key} : aucun compte Convex`);
  return me._id;
}

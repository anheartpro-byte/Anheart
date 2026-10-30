import type { Browser } from "@playwright/test";
import path from "node:path";
import { clerkApi } from "./clerk";
import { callAs, replayClerkEvent, type ActingIdentity } from "./convex";
import type { OrganizationKey, RunInfo } from "./run";
import { loadClerkKeys, rememberFromTest } from "./secrets";
import { signIn } from "./signin";

/**
 * Accounts a test adds during the run, beside the setup's: the same shape of
 * address (`e2e-<run>-<name>+clerk_test@example.com`), so that the end of the
 * run, or the start of the next one, deletes them with the others. The keys
 * come from E2E_ENV_FILE, read by the harness itself, as in the setup.
 */
export function memberEmail(run: RunInfo, name: string): string {
  return `e2e-${run.runId}-${name}+clerk_test@example.com`;
}

export type Member = {
  /** The Clerk account. */
  userId: string;
  email: string;
  /** Its Convex row (`users._id`), as the site's URLs name it. */
  convexId: string;
  /** What a Clerk token of this account would carry. */
  identity: ActingIdentity;
};

/** A Clerk account made a member of one of the run's organisations, mirrored in Convex as the webhook would. */
export async function addMember(
  run: RunInfo,
  member: { name: string; firstName: string; lastName: string; organization: OrganizationKey; role: string },
): Promise<Member> {
  const clerk = clerkApi(loadClerkKeys());
  const email = memberEmail(run, member.name);
  const user = await clerk.createUser(email, member.firstName, member.lastName);
  const membership = await clerk.addMember(run.organizations[member.organization], user.id, member.role);
  await replayClerkEvent(run.backend, run.webhookSecret, "organizationMembership.created", membership);
  const identity: ActingIdentity = {
    subject: user.id,
    issuer: run.issuer,
    org_id: run.organizations[member.organization],
    org_role: member.role,
    email,
    email_verified: true,
  };
  const me = await callAs<{ _id: string } | null>(run.backend, identity, "query", "users:getCurrentUser", {});
  if (!me) throw new Error(`${member.name} : aucun compte Convex après le webhook`);
  return { userId: user.id, email, convexId: me._id, identity };
}

/** Tries a second Clerk account at an address already taken: Clerk's answer, never its body. */
export async function createAccountAt(email: string): Promise<"created" | string> {
  try {
    await clerkApi(loadClerkKeys()).createUser(email, "Second", "Compte");
    return "created";
  } catch (error) {
    return String(error);
  }
}

/** Signs a member in as the setup does its accounts; returns the browser state. */
export async function signInMember(
  browser: Browser,
  run: RunInfo,
  member: { userId: string; organization: OrganizationKey; firstName: string },
): Promise<string> {
  const keys = loadClerkKeys();
  const clerk = clerkApi(keys);
  const storageState = path.join(run.runDir, `state-${member.userId}.json`);
  await signIn(browser, {
    baseURL: run.baseURL,
    frontendApi: keys.frontendApi,
    testingToken: rememberFromTest(await clerk.testingToken()),
    ticket: rememberFromTest(await clerk.signInToken(member.userId)),
    organizationId: run.organizations[member.organization],
    welcome: `Bienvenue, ${member.firstName}`,
    storageState,
  });
  return storageState;
}

import type { ClerkKeys } from "./secrets";
import { remember } from "./secrets";

/**
 * The Clerk Backend API of the development instance, by fetch. What it
 * answers is never printed: an error says its status and Clerk's codes only.
 */
export type Clerk = ReturnType<typeof clerkApi>;

/** Only what a run creates: `e2e-<run>-<role>+clerk_test@example.com`, `e2e-<run> <label>`. */
const TEST_EMAIL = /^e2e-[a-z0-9]+-[a-z0-9-]+\+clerk_test@example\.com$/;
const TEST_ORGANIZATION = /^e2e-[a-z0-9]+ [A-Za-z ]+$/;

type ClerkUser = { id: string; email_addresses: { email_address: string }[] };
type ClerkOrganization = { id: string; name: string };

export function clerkApi(keys: ClerkKeys) {
  async function request<T>(method: string, route: string, body?: unknown): Promise<T> {
    const response = await fetch(`https://api.clerk.com/v1${route}`, {
      method,
      headers: {
        authorization: `Bearer ${keys.secretKey}`,
        "content-type": "application/json",
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(20_000),
    });
    if (!response.ok) {
      const codes = await response
        .json()
        .then((value: { errors?: { code?: string }[] }) =>
          (value.errors ?? []).map((error) => error.code).join(", "),
        )
        .catch(() => "");
      throw new ClerkError(method, route.split("?")[0], response.status, codes);
    }
    return (await response.json()) as T;
  }

  async function testUsers(): Promise<ClerkUser[]> {
    const users: ClerkUser[] = [];
    for (let offset = 0; ; offset += 100) {
      const page = await request<ClerkUser[]>("GET", `/users?query=e2e-&limit=100&offset=${offset}`);
      users.push(...page);
      if (page.length < 100) break;
    }
    return users.filter(
      (user) =>
        user.email_addresses.length > 0 &&
        user.email_addresses.every((email) => TEST_EMAIL.test(email.email_address)),
    );
  }

  async function testOrganizations(): Promise<ClerkOrganization[]> {
    const organizations: ClerkOrganization[] = [];
    for (let offset = 0; ; offset += 100) {
      const page = await request<{ data: ClerkOrganization[] }>(
        "GET",
        `/organizations?query=e2e-&limit=100&offset=${offset}`,
      );
      organizations.push(...page.data);
      if (page.data.length < 100) break;
    }
    return organizations.filter((organization) => TEST_ORGANIZATION.test(organization.name));
  }

  async function testResources(runId?: string) {
    const users = (await testUsers()).filter(
      (user) => runId === undefined || user.email_addresses.every((email) => email.email_address.startsWith(`e2e-${runId}-`)),
    );
    const organizations = (await testOrganizations()).filter(
      (organization) => runId === undefined || organization.name.startsWith(`e2e-${runId} `),
    );
    return { users, organizations };
  }

  return {
    request,

    /**
     * What the site and Convex need of the instance, checked before anything
     * is created: organisations on, the roles of the test accounts, and the
     * `convex` token template carrying the organisation and the verified e-mail.
     */
    async checkInstance(roles: string[]): Promise<void> {
      const problems: string[] = [];
      try {
        await request("GET", "/organizations?limit=1");
      } catch (error) {
        problems.push(`organisations non activées sur l'instance (${String(error)})`);
      }
      try {
        const listed = await request<{ data: { key: string }[] }>("GET", "/organization_roles?limit=100");
        const present = listed.data.map((role) => role.key);
        for (const key of new Set(roles)) {
          if (!present.includes(key)) problems.push(`rôle d'organisation ${key} absent (présents : ${present.join(", ")})`);
        }
      } catch (error) {
        problems.push(`rôles d'organisation illisibles (${String(error)})`);
      }
      const templates = await request<{ name: string; claims: Record<string, unknown> }[]>("GET", "/jwt_templates");
      const convex = templates.find((template) => template.name === "convex");
      if (!convex) {
        problems.push("gabarit de jeton « convex » absent");
      } else {
        for (const claim of ["org_id", "org_role", "email", "email_verified"]) {
          if (!(claim in convex.claims)) problems.push(`le gabarit « convex » ne porte pas ${claim}`);
        }
      }
      if (problems.length > 0) {
        throw new Error(`Instance Clerk de développement incomplète :\n- ${problems.join("\n- ")}`);
      }
    },

    createUser: (email: string, firstName: string, lastName: string) =>
      request<{ id: string }>("POST", "/users", {
        email_address: [email],
        first_name: firstName,
        last_name: lastName,
        skip_password_requirement: true,
      }),

    /** Without a slug: the instance has organisation slugs turned off. */
    createOrganization: (name: string) =>
      request<Record<string, unknown> & { id: string }>("POST", "/organizations", { name }),

    addMember: (organizationId: string, userId: string, role: string) =>
      request<Record<string, unknown>>("POST", `/organizations/${organizationId}/memberships`, {
        user_id: userId,
        role,
      }),

    signInToken: async (userId: string) =>
      remember(
        (
          await request<{ token: string }>("POST", "/sign_in_tokens", {
            user_id: userId,
            expires_in_seconds: 600,
          })
        ).token,
      ),

    testingToken: async () =>
      remember((await request<{ token: string }>("POST", "/testing_tokens")).token),

    /**
     * Users and organisations of the run `runId` (`e2e-<runId>-` addresses,
     * `e2e-<runId> ` names) deleted; without it, those of any run (`e2e-`).
     */
    async deleteTestResources(runId?: string): Promise<{ users: number; organizations: number }> {
      const { users, organizations } = await testResources(runId);
      for (const organization of organizations) {
        await request("DELETE", `/organizations/${organization.id}`);
      }
      for (const user of users) await request("DELETE", `/users/${user.id}`);
      return { users: users.length, organizations: organizations.length };
    },

    async countTestResources(runId?: string): Promise<{ users: number; organizations: number }> {
      const { users, organizations } = await testResources(runId);
      return { users: users.length, organizations: organizations.length };
    },
  };
}

export class ClerkError extends Error {
  constructor(method: string, route: string, status: number, codes: string) {
    super(`Clerk ${method} ${route} : HTTP ${status}${codes ? ` (${codes})` : ""}`);
  }
}

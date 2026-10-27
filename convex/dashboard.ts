/**
 * The figures of the dashboard's first page (ANH-159).
 *
 * Each figure counts within what the caller can already list on another page,
 * never beyond, so that a card never says more than the page it leads to:
 *
 * - the Anheart admin: every organisation;
 * - an organisation admin: their organisation;
 * - a gestionnaire: their patients, their machines, and the sessions they can
 *   open (on their machines, or ridden by themselves); never a figure of the
 *   whole organisation;
 * - a patient: their own sessions only; no machine, no member.
 *
 * Members are counted as accounts: the Anheart admin counts once an account
 * that is an active member of two organisations. Their Users page lists more:
 * every account, with or without an active membership.
 *
 * Every figure is read through an index, never by reading a whole table. The
 * cost of a call grows with what it counts (the members of an organisation,
 * its machines, its open sessions): see docs/convex.md, `dashboard.ts`.
 */
import { query, type QueryCtx } from "./_generated/server";
import { v } from "convex/values";
import type { Doc, Id } from "./_generated/dataModel";
import {
  getCurrentUserOrThrow,
  organizationRoleOf,
  sameOrganization,
  type CurrentUser,
} from "./lib/auth";

/**
 * The accounts that are active members, and those holding each role in at
 * least one of the memberships counted: an account holding two roles counts
 * once in `total` and once in each role, so the roles can add up to more.
 */
const membersValidator = v.object({
  total: v.number(),
  admin: v.number(),
  gestionnaire: v.number(),
  user: v.number(),
});

/** A machine whose record says it is connected, with its last signal. */
const connectedStatusValidator = v.union(
  v.literal("online"),
  v.literal("in_session"),
);

type ConnectedStatus = "online" | "in_session";
type OpenStatus = "active" | "pending";

export const counters = query({
  args: {},
  returns: v.object({
    // The server's clock in this answer: what each `lastHeartbeat` is aged on.
    serverNow: v.number(),
    // Organisation admins (the Anheart admin: every organisation). Null for
    // anyone else: a gestionnaire or a patient lists no member.
    members: v.union(membersValidator, v.null()),
    // A gestionnaire's own patients. Null for anyone else.
    patients: v.union(v.number(), v.null()),
    // The machines the caller lists (deleted ones left out). `signals` holds
    // the machines whose record says "online" or "in_session": the site counts
    // those whose last signal is still fresh, judged every second on
    // `serverNow`, as it shows them. Null for a patient.
    machines: v.union(
      v.object({
        total: v.number(),
        signals: v.array(
          v.object({
            status: connectedStatusValidator,
            lastHeartbeat: v.number(),
          }),
        ),
      }),
      v.null(),
    ),
    // The sessions the caller can open, by status.
    sessions: v.object({ active: v.number(), pending: v.number() }),
  }),
  handler: async (ctx) => {
    const caller = await getCurrentUserOrThrow(ctx);
    const machines = await machinesOf(ctx, caller);
    const listed =
      machines?.filter((machine) => machine.isDeleted !== true) ?? null;
    // A gestionnaire opens the sessions of the machines they manage in their
    // organisation, deleted machines included (`canAccessMachine`).
    const managed = new Set(machines?.map((machine) => machine._id));
    const sessions = {
      active: await openSessions(ctx, caller, "active", managed),
      pending: await openSessions(ctx, caller, "pending", managed),
    };

    return {
      serverNow: Date.now(),
      members:
        caller.role === "admin" || caller.role === "org_admin"
          ? await membersOf(ctx, caller)
          : null,
      patients:
        caller.role === "gestionnaire" ? await patientsOf(ctx, caller) : null,
      machines:
        listed === null
          ? null
          : {
              total: listed.length,
              signals: listed.flatMap((machine) =>
                isConnected(machine.status)
                  ? [
                      {
                        status: machine.status,
                        lastHeartbeat: machine.lastHeartbeat,
                      },
                    ]
                  : [],
              ),
            },
      sessions,
    };
  },
});

function isConnected(status: Doc<"machines">["status"]): status is ConnectedStatus {
  return status === "online" || status === "in_session";
}

/**
 * The machines the caller reaches, as `machines.listMachines` reaches them,
 * deleted ones included (the caller decides what to do with them). Null for a
 * patient, who reaches none.
 */
async function machinesOf(
  ctx: QueryCtx,
  caller: CurrentUser,
): Promise<Doc<"machines">[] | null> {
  switch (caller.role) {
    case "admin": {
      // Every organisation: the machines never deleted (`isDeleted` absent)
      // and those restored (`false`). Deleted ones are never read.
      const never = await ctx.db
        .query("machines")
        .withIndex("by_is_deleted", (q) => q.eq("isDeleted", undefined))
        .collect();
      const restored = await ctx.db
        .query("machines")
        .withIndex("by_is_deleted", (q) => q.eq("isDeleted", false))
        .collect();
      return [...never, ...restored];
    }
    case "org_admin":
      return await ctx.db
        .query("machines")
        .withIndex("by_organization", (q) =>
          q.eq("organizationId", caller.organizationId),
        )
        .collect();
    case "gestionnaire": {
      const links = await ctx.db
        .query("machine_gestionnaires")
        .withIndex("by_gestionnaire", (q) => q.eq("gestionnaireId", caller._id))
        .collect();
      const machines: Doc<"machines">[] = [];
      for (const machineId of new Set(links.map((link) => link.machineId))) {
        const machine = await ctx.db.get(machineId);
        if (machine && sameOrganization(caller, machine.organizationId)) {
          machines.push(machine);
        }
      }
      return machines;
    }
    case "user":
      return null;
  }
}

/**
 * How many sessions of this status the caller can open (`canAccessSession`):
 * every one for the Anheart admin; in the caller's organisation, every one for
 * its admin, and for anyone else those they ride or whose machine they manage.
 */
async function openSessions(
  ctx: QueryCtx,
  caller: CurrentUser,
  status: OpenStatus,
  managed: ReadonlySet<Id<"machines">>,
): Promise<number> {
  if (caller.role === "admin") {
    const every = await ctx.db
      .query("sessions")
      .withIndex("by_status_and_organization", (q) => q.eq("status", status))
      .collect();
    return every.length;
  }
  const inOrganization = await ctx.db
    .query("sessions")
    .withIndex("by_status_and_organization", (q) =>
      q.eq("status", status).eq("organizationId", caller.organizationId),
    )
    .collect();
  if (caller.role === "org_admin") return inOrganization.length;
  return inOrganization.filter(
    (session) =>
      session.userId === caller._id || managed.has(session.machineId),
  ).length;
}

/**
 * The accounts that are active members, in all and by role: of every
 * organisation for the Anheart admin, each account counted once.
 */
async function membersOf(
  ctx: QueryCtx,
  caller: CurrentUser,
): Promise<Record<"total" | Doc<"memberships">["role"], number>> {
  const members =
    caller.role === "admin"
      ? await ctx.db
          .query("memberships")
          .withIndex("by_active_and_organization", (q) =>
            q.eq("active", true),
          )
          .collect()
      : await ctx.db
          .query("memberships")
          .withIndex("by_active_and_organization", (q) =>
            q.eq("active", true).eq("organizationId", caller.organizationId),
          )
          .collect();
  const accounts = new Set<Id<"users">>();
  const byRole = {
    admin: new Set<Id<"users">>(),
    gestionnaire: new Set<Id<"users">>(),
    user: new Set<Id<"users">>(),
  };
  for (const { userId, role } of members) {
    accounts.add(userId);
    byRole[role].add(userId);
  }
  return {
    total: accounts.size,
    admin: byRole.admin.size,
    gestionnaire: byRole.gestionnaire.size,
    user: byRole.user.size,
  };
}

/**
 * A gestionnaire's patients: the accounts they are linked to in their
 * organisation that are still active patients of it, as the Patients page
 * lists them (`users.listUsers` with `role: "user"`).
 */
async function patientsOf(ctx: QueryCtx, caller: CurrentUser): Promise<number> {
  const links = await ctx.db
    .query("user_gestionnaires")
    .withIndex("by_gestionnaire", (q) => q.eq("gestionnaireId", caller._id))
    .collect();
  const linked = new Set(
    links
      .filter((link) => sameOrganization(caller, link.organizationId))
      .map((link) => link.userId),
  );
  let patients = 0;
  for (const userId of linked) {
    const role = await organizationRoleOf(ctx, userId, caller.organizationId);
    if (role === "user") patients += 1;
  }
  return patients;
}

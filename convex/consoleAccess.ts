/**
 * Who may act at a machine's console (ANH-152): its operators and its tablets.
 *
 * - An admin of the machine's organisation, or the Anheart admin, enables and
 *   disables operators among the organisation's active admins and
 *   gestionnaires, and creates and revokes the machine's tablets (a device
 *   token, shown once). Nobody else sees any of it.
 * - An operator sets their own console code (`consoleCodes.setMyOperatorCode`,
 *   a Node action that hashes it), for every machine of their organisation
 *   they are enabled on. No one else can set it, nor read it: only its scrypt
 *   hash is kept, and it is never served to the site.
 * - The machine fetches the list, signed (`GET /api/machine/operators`,
 *   `operatorList` below), and checks a code offline.
 *
 * Rules and the signed text: `convex/lib/consoleAccess.ts`.
 */
import { v } from "convex/values";
import {
  internalMutation,
  internalQuery,
  mutation,
  query,
  type MutationCtx,
  type QueryCtx,
} from "./_generated/server";
import type { Doc, Id } from "./_generated/dataModel";
import {
  getCurrentUserOrThrow,
  inScope,
  ORGANIZATION_ADMIN_ROLES,
  organizationRoleOf,
  requireRole,
  type CurrentUser,
} from "./lib/auth";
import { refusal } from "./lib/errors";
import { limitUser } from "./lib/rateLimits";
import { audit, userActor } from "./lib/audit";
import {
  deviceNameOf,
  MAX_DEVICES,
  MAX_OPERATORS,
  newDeviceToken,
  OPERATOR_ROLES,
  operatorCodeValidator,
  operatorListPayload,
  shownName,
} from "./lib/consoleAccess";

type Ctx = QueryCtx | MutationCtx;

/** How many members of one organisation the candidates are looked for among. */
const CANDIDATE_SCAN = 500;

function nameOf(user: Pick<Doc<"users">, "firstName" | "lastName"> | null): string {
  return user === null ? "-" : `${user.firstName} ${user.lastName}`.trim();
}

/** Whether the member may be an operator of a machine of that organisation, now. */
async function eligible(
  ctx: Ctx,
  userId: Id<"users">,
  organizationId: Id<"organizations">,
): Promise<boolean> {
  const role = await organizationRoleOf(ctx, userId, organizationId);
  return role !== null && OPERATOR_ROLES.includes(role);
}

/**
 * The machine whose console access the caller administers, with its
 * organisation; null when it does not exist, is deleted, has no organisation
 * or is not in the caller's scope. The caller is an admin already.
 */
async function administered(
  ctx: Ctx,
  caller: CurrentUser,
  machineId: Id<"machines">,
): Promise<(Doc<"machines"> & { organizationId: Id<"organizations"> }) | null> {
  const machine = await ctx.db.get(machineId);
  if (
    !machine ||
    machine.isDeleted ||
    machine.organizationId === undefined ||
    !inScope(caller, machine.organizationId)
  ) {
    return null;
  }
  return { ...machine, organizationId: machine.organizationId };
}

async function operatorRow(
  ctx: Ctx,
  machineId: Id<"machines">,
  userId: Id<"users">,
): Promise<Doc<"machine_operators"> | null> {
  return await ctx.db
    .query("machine_operators")
    .withIndex("by_machine_and_user", (q) =>
      q.eq("machineId", machineId).eq("userId", userId),
    )
    .unique();
}

/**
 * Everything an admin manages of a machine's console: its operators (enabled
 * or not, whether each one set a code and is still a member who may be one,
 * never the code), the members who could be enabled, and its tablets (never
 * the token). `null` for anyone but an admin of the machine's organisation or
 * the Anheart admin, and for a machine that does not exist or is deleted.
 */
export const listConsoleAccess = query({
  args: { machineId: v.id("machines") },
  returns: v.union(
    v.object({
      operators: v.array(
        v.object({
          userId: v.id("users"),
          name: v.string(),
          enabled: v.boolean(),
          // Still an active admin or gestionnaire of the machine's organisation:
          // the console lists only those.
          eligible: v.boolean(),
          codeSet: v.boolean(),
          enabledAt: v.number(),
          disabledAt: v.union(v.number(), v.null()),
          codeSetAt: v.union(v.number(), v.null()),
          trainedAt: v.union(v.number(), v.null()),
        }),
      ),
      candidates: v.array(v.object({ userId: v.id("users"), name: v.string() })),
      devices: v.array(
        v.object({
          _id: v.id("machine_devices"),
          name: v.string(),
          prefix: v.string(),
          createdAt: v.number(),
          createdByName: v.string(),
          revokedAt: v.union(v.number(), v.null()),
        }),
      ),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const caller = await getCurrentUserOrThrow(ctx);
    if (!ORGANIZATION_ADMIN_ROLES.includes(caller.role)) return null;
    const machine = await administered(ctx, caller, args.machineId);
    if (machine === null) return null;
    const rows = await ctx.db
      .query("machine_operators")
      .withIndex("by_machine", (q) => q.eq("machineId", machine._id))
      .collect();
    const operators = await Promise.all(
      rows.map(async (row) => ({
        userId: row.userId,
        name: nameOf(await ctx.db.get(row.userId)),
        enabled: row.disabledAt === undefined,
        eligible: await eligible(ctx, row.userId, machine.organizationId),
        codeSet: row.operatorCode !== undefined,
        enabledAt: row.enabledAt,
        disabledAt: row.disabledAt ?? null,
        codeSetAt: row.codeSetAt ?? null,
        trainedAt: row.trainedAt ?? null,
      })),
    );
    const enabled = new Set(
      rows.filter((row) => row.disabledAt === undefined).map((row) => row.userId),
    );
    // shortcut: the first 500 memberships of the organisation; page them past a few hundred.
    const members = await ctx.db
      .query("memberships")
      .withIndex("by_organization", (q) => q.eq("organizationId", machine.organizationId))
      .take(CANDIDATE_SCAN);
    const candidates = await Promise.all(
      members
        .filter(
          (m) => m.active && OPERATOR_ROLES.includes(m.role) && !enabled.has(m.userId),
        )
        .map(async (m) => ({ userId: m.userId, name: nameOf(await ctx.db.get(m.userId)) })),
    );
    const devices = await ctx.db
      .query("machine_devices")
      .withIndex("by_machine", (q) => q.eq("machineId", machine._id))
      .order("desc")
      .collect();
    return {
      operators: operators.sort((a, b) => a.name.localeCompare(b.name)),
      candidates: candidates.sort((a, b) => a.name.localeCompare(b.name)),
      devices: await Promise.all(
        devices.map(async (device) => ({
          _id: device._id,
          name: device.name,
          prefix: device.prefix,
          createdAt: device.createdAt,
          createdByName: nameOf(await ctx.db.get(device.createdBy)),
          revokedAt: device.revokedAt ?? null,
        })),
      ),
    };
  },
});

/**
 * Enable a member on the machine's console. Admins only (the Anheart admin, or
 * the admin of the machine's organisation); the member must be an active
 * admin or gestionnaire of that organisation. An operator who already set
 * their code for another machine of the organisation can sign in at once; one
 * who never did must set it first. Enabling an enabled operator changes
 * nothing.
 */
export const enableOperator = mutation({
  args: { machineId: v.id("machines"), userId: v.id("users") },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);
    const machine = await administered(ctx, caller, args.machineId);
    if (machine === null) throw refusal("machine_not_found");
    if (!(await eligible(ctx, args.userId, machine.organizationId)))
      throw refusal("operator_not_eligible");
    const existing = await operatorRow(ctx, machine._id, args.userId);
    if (existing !== null && existing.disabledAt === undefined) return null;
    const enabled = await ctx.db
      .query("machine_operators")
      .withIndex("by_machine", (q) => q.eq("machineId", machine._id))
      .filter((q) => q.eq(q.field("disabledAt"), undefined))
      .collect();
    if (enabled.length >= MAX_OPERATORS) throw refusal("too_many_operators");
    const now = Date.now();
    // The code is the person's, in this organisation: the latest they set.
    const coded = (
      await ctx.db
        .query("machine_operators")
        .withIndex("by_user", (q) => q.eq("userId", args.userId))
        .collect()
    )
      .filter(
        (row) =>
          row.organizationId === machine.organizationId && row.codeSetAt !== undefined,
      )
      .sort((a, b) => (b.codeSetAt ?? 0) - (a.codeSetAt ?? 0))[0];
    const code = coded === undefined ? {} : { operatorCode: coded.operatorCode, codeSetAt: coded.codeSetAt };
    let rowId: Id<"machine_operators">;
    if (existing === null) {
      rowId = await ctx.db.insert("machine_operators", {
        organizationId: machine.organizationId,
        machineId: machine._id,
        userId: args.userId,
        enabledAt: now,
        enabledBy: caller._id,
        ...code,
      });
    } else {
      rowId = existing._id;
      await ctx.db.patch(existing._id, {
        enabledAt: now,
        enabledBy: caller._id,
        disabledAt: undefined,
        disabledBy: undefined,
        ...code,
      });
    }
    await audit(ctx, {
      actor: userActor(caller),
      source: "site",
      organizationId: machine.organizationId,
      action: "machine_operator.enabled",
      targetId: rowId,
      details: { machineId: machine._id, userId: args.userId },
    });
    return null;
  },
});

/**
 * Disable an operator of the machine's console: from the machine's next
 * synchronisation at rest, they can no longer sign in there, and a session
 * they hold on it ends. Admins only, as `enableOperator`. Disabling a
 * disabled operator changes nothing.
 */
export const disableOperator = mutation({
  args: { machineId: v.id("machines"), userId: v.id("users") },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);
    const machine = await administered(ctx, caller, args.machineId);
    if (machine === null) throw refusal("machine_not_found");
    const row = await operatorRow(ctx, machine._id, args.userId);
    if (row === null) throw refusal("machine_operator_not_found");
    if (row.disabledAt !== undefined) return null;
    await ctx.db.patch(row._id, { disabledAt: Date.now(), disabledBy: caller._id });
    await audit(ctx, {
      actor: userActor(caller),
      source: "site",
      organizationId: machine.organizationId,
      action: "machine_operator.disabled",
      targetId: row._id,
      details: { machineId: machine._id, userId: args.userId },
    });
    return null;
  },
});

/**
 * The machines of the caller's organisation on whose console they are an
 * enabled operator, and whether their code is set for each. Never the code.
 */
export const listMyOperatorMachines = query({
  args: {},
  returns: v.array(
    v.object({
      machineId: v.id("machines"),
      machineName: v.string(),
      codeSet: v.boolean(),
      codeSetAt: v.union(v.number(), v.null()),
    }),
  ),
  handler: async (ctx) => {
    const caller = await getCurrentUserOrThrow(ctx);
    const rows = await enabledRowsOf(ctx, caller);
    const listed = await Promise.all(
      rows.map(async (row) => ({ row, machine: await ctx.db.get(row.machineId) })),
    );
    return listed
      .filter(({ machine }) => machine !== null && !machine.isDeleted)
      .map(({ row, machine }) => ({
        machineId: row.machineId,
        machineName: machine?.name ?? "-",
        codeSet: row.operatorCode !== undefined,
        codeSetAt: row.codeSetAt ?? null,
      }));
  },
});

/** The caller's operator rows in their organisation that are enabled. */
async function enabledRowsOf(ctx: Ctx, caller: CurrentUser) {
  return (
    await ctx.db
      .query("machine_operators")
      .withIndex("by_user", (q) => q.eq("userId", caller._id))
      .collect()
  ).filter(
    (row) => row.organizationId === caller.organizationId && row.disabledAt === undefined,
  );
}

/**
 * Before a code is hashed (`consoleCodes.setMyOperatorCode`): the caller's
 * write limit, then whether they are an operator anywhere in their
 * organisation. Nothing is written but the limiter's count.
 */
export const prepareOperatorCode = internalMutation({
  args: {},
  returns: v.null(),
  handler: async (ctx) => {
    await limitUser(ctx);
    const caller = await getCurrentUserOrThrow(ctx);
    if ((await enabledRowsOf(ctx, caller)).length === 0)
      throw refusal("operator_not_enabled");
    return null;
  },
});

/**
 * The caller's new code, hashed by `consoleCodes.setMyOperatorCode`, on every
 * row of theirs in their organisation, enabled or not: one code per person
 * and organisation. Each machine gets it at its next synchronisation.
 */
export const storeOperatorCode = internalMutation({
  args: { code: operatorCodeValidator },
  returns: v.null(),
  handler: async (ctx, args) => {
    const caller = await getCurrentUserOrThrow(ctx);
    if ((await enabledRowsOf(ctx, caller)).length === 0)
      throw refusal("operator_not_enabled");
    const rows = (
      await ctx.db
        .query("machine_operators")
        .withIndex("by_user", (q) => q.eq("userId", caller._id))
        .collect()
    ).filter((row) => row.organizationId === caller.organizationId);
    const now = Date.now();
    for (const row of rows) {
      await ctx.db.patch(row._id, { operatorCode: args.code, codeSetAt: now });
    }
    await audit(ctx, {
      actor: userActor(caller),
      source: "site",
      organizationId: caller.organizationId,
      action: "machine_operator.code_set",
      targetId: caller._id,
      details: { count: rows.length },
    });
    return null;
  },
});

/**
 * Create a tablet for the machine's console and answer its token, the only
 * time it is shown. The console accepts it from its next synchronisation.
 * Admins only, as `enableOperator`.
 */
export const createDevice = mutation({
  args: { machineId: v.id("machines"), name: v.string() },
  returns: v.object({
    deviceId: v.id("machine_devices"),
    // The plain token: in this answer only.
    token: v.string(),
    prefix: v.string(),
  }),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);
    const machine = await administered(ctx, caller, args.machineId);
    if (machine === null) throw refusal("machine_not_found");
    const name = deviceNameOf(args.name);
    if (name === null) throw refusal("invalid_device_name");
    const unrevoked = await ctx.db
      .query("machine_devices")
      .withIndex("by_machine", (q) => q.eq("machineId", machine._id))
      .filter((q) => q.eq(q.field("revokedAt"), undefined))
      .collect();
    if (unrevoked.length >= MAX_DEVICES) throw refusal("too_many_devices");
    const issued = await newDeviceToken();
    const deviceId = await ctx.db.insert("machine_devices", {
      organizationId: machine.organizationId,
      machineId: machine._id,
      name,
      tokenSha256: issued.sha256,
      prefix: issued.prefix,
      createdAt: Date.now(),
      createdBy: caller._id,
    });
    await audit(ctx, {
      actor: userActor(caller),
      source: "site",
      organizationId: machine.organizationId,
      action: "machine_device.created",
      targetId: deviceId,
      details: { machineId: machine._id },
    });
    return { deviceId, token: issued.token, prefix: issued.prefix };
  },
});

/**
 * Revoke a tablet: from the machine's next synchronisation at rest, its token
 * opens nothing there, its sessions end. Admins only; a tablet of a machine
 * the caller does not administer answers `machine_device_not_found`, as one
 * that does not exist. Revoking a revoked tablet changes nothing.
 */
export const revokeDevice = mutation({
  args: { deviceId: v.id("machine_devices") },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);
    const device = await ctx.db.get(args.deviceId);
    if (device === null || !inScope(caller, device.organizationId))
      throw refusal("machine_device_not_found");
    if (device.revokedAt !== undefined) return null;
    await ctx.db.patch(device._id, { revokedAt: Date.now(), revokedBy: caller._id });
    await audit(ctx, {
      actor: userActor(caller),
      source: "site",
      organizationId: device.organizationId,
      action: "machine_device.revoked",
      targetId: device._id,
      details: { machineId: device.machineId },
    });
    return null;
  },
});

/**
 * The text `GET /api/machine/operators` signs for the machine: each enabled
 * operator who set a code and is still an active admin or gestionnaire of the
 * machine's organisation, and each unrevoked tablet. `issuedAt`: the action's
 * clock, which the console uses to refuse a list older than its own.
 */
export const operatorList = internalQuery({
  args: { machineId: v.id("machines"), issuedAt: v.number() },
  returns: v.string(),
  handler: async (ctx, args) => {
    const machine = await ctx.db.get(args.machineId);
    const organizationId = machine?.organizationId;
    const rows = await ctx.db
      .query("machine_operators")
      .withIndex("by_machine", (q) => q.eq("machineId", args.machineId))
      .collect();
    const operators = [];
    for (const row of rows) {
      if (
        row.disabledAt !== undefined ||
        row.operatorCode === undefined ||
        organizationId === undefined ||
        !(await eligible(ctx, row.userId, organizationId))
      )
        continue;
      operators.push({
        userId: row.userId,
        name: shownName(nameOf(await ctx.db.get(row.userId)), row.userId),
        code: row.operatorCode,
      });
    }
    const devices = (
      await ctx.db
        .query("machine_devices")
        .withIndex("by_machine", (q) => q.eq("machineId", args.machineId))
        .collect()
    )
      .filter((device) => device.revokedAt === undefined)
      .map((device) => ({
        deviceId: device._id,
        name: shownName(device.name, device._id),
        tokenSha256: device.tokenSha256,
      }));
    return operatorListPayload({
      machineId: args.machineId,
      issuedAt: args.issuedAt,
      operators,
      devices,
    });
  },
});

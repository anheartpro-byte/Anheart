/**
 * The pairing codes of the machines (ANH-115, contract 1.17): a console that
 * has no key yet trades a one-time code, with its Pi's serial number, for one
 * (`POST /api/machine/pair`, convex/http.ts). Rules of a code:
 * `convex/lib/machinePairing.ts`; its drawing, hashing and verification:
 * `convex/pairingCodes.ts`, in Node for scrypt.
 *
 * A code is issued by an admin of the machine's organisation (or the Anheart
 * admin), as a key is (`machineKeys.createMachineKey`), for the Pi whose
 * serial number the machine's registry holds. It is open for 30 minutes, one
 * claim, five wrong codes for its serial number; a newer code of the machine
 * replaces it. The code is shown once, in the answer of the action that drew
 * it: no row, list, log or audit entry holds it. A claim answers the new key
 * once, to the console, and revokes every other key of the machine at once.
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
  requireRole,
  type CurrentUser,
} from "./lib/auth";
import { refusal } from "./lib/errors";
import { limitUser } from "./lib/rateLimits";
import { audit, machineActor, SYSTEM_ACTOR, userActor } from "./lib/audit";
import { mayAuthenticate } from "./lib/machineAuth";
import { rotateMachineKey } from "./lib/machineKeys";
import {
  claimOutcome,
  normalizedSerial,
  PAIRING_MAX_FAILURES,
  PAIRING_TTL_MS,
  pairingIsLive,
} from "./lib/machinePairing";

/**
 * The machine whose codes the caller manages, or the refusal
 * `machine_not_found`: a machine that does not exist, is deleted, or belongs
 * to an organisation the caller does not administer answers the same.
 */
async function managedMachine(
  ctx: QueryCtx,
  caller: CurrentUser,
  machineId: Id<"machines">,
): Promise<Doc<"machines">> {
  const machine = await ctx.db.get(machineId);
  if (
    !machine ||
    machine.isDeleted ||
    !inScope(caller, machine.organizationId)
  ) {
    throw refusal("machine_not_found");
  }
  return machine;
}

/** The serial number of the machine's registry, as it is compared; null without one. */
async function registrySerialOf(
  ctx: QueryCtx,
  machineId: Id<"machines">,
): Promise<string | null> {
  const registry = await ctx.db
    .query("machine_registry")
    .withIndex("by_machine", (q) => q.eq("machineId", machineId))
    .unique();
  return normalizedSerial(registry?.serialPi);
}

/**
 * The open codes of a serial number. A machine holds one open code at most
 * (issuing closes the others), so there are as many as machines whose
 * registry gave this number: a handful at most, read whole.
 */
async function openCodesOfSerial(
  ctx: QueryCtx,
  serialPi: string,
): Promise<Doc<"machine_pairing_codes">[]> {
  return await ctx.db
    .query("machine_pairing_codes")
    .withIndex("by_serial_and_closed", (q) =>
      q.eq("serialPi", serialPi).eq("closedAt", undefined),
    )
    .collect();
}

/**
 * What a code of this machine would serve, checked: the caller administers
 * the machine, its registry holds a serial number, and no other machine has a
 * code open for that number now. Refused otherwise, with nothing written.
 */
async function checkedIssue(
  ctx: QueryCtx,
  caller: CurrentUser,
  machineId: Id<"machines">,
  now: number,
): Promise<{
  machine: Doc<"machines">;
  organizationId: Id<"organizations">;
  serialPi: string;
}> {
  const machine = await managedMachine(ctx, caller, machineId);
  // A machine of no organisation (before ANH-114) is no machine to pair.
  const { organizationId } = machine;
  if (organizationId === undefined) throw refusal("machine_not_found");
  const serialPi = await registrySerialOf(ctx, machine._id);
  if (serialPi === null) throw refusal("pairing_serial_missing");
  const others = (await openCodesOfSerial(ctx, serialPi)).filter(
    (code) => code.machineId !== machine._id && pairingIsLive(code, now),
  );
  if (others.length > 0) throw refusal("pairing_serial_in_use");
  return { machine, organizationId, serialPi };
}

/**
 * Before a code is drawn (`pairingCodes.issuePairingCode`): the caller's
 * write limit, their right, and the machine's serial number. Writes nothing.
 */
export const prepareIssue = internalMutation({
  args: { machineId: v.id("machines") },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);
    await checkedIssue(ctx, caller, args.machineId, Date.now());
    return null;
  },
});

/** Close every open code of the machine; audit the ones still live as revoked. */
async function closeOpenCodes(
  ctx: MutationCtx,
  machine: Doc<"machines">,
  caller: CurrentUser,
  closedAs: "replaced" | "revoked",
  now: number,
): Promise<number> {
  const open = await ctx.db
    .query("machine_pairing_codes")
    .withIndex("by_machine_and_closed", (q) =>
      q.eq("machineId", machine._id).eq("closedAt", undefined),
    )
    .collect();
  let revoked = 0;
  for (const code of open) {
    await ctx.db.patch(code._id, {
      closedAt: now,
      closedAs,
      closedBy: caller._id,
    });
    if (!pairingIsLive(code, now)) continue;
    revoked += 1;
    await audit(ctx, {
      actor: userActor(caller),
      source: "site",
      organizationId: machine.organizationId,
      action: "machine_pairing.revoked",
      targetId: code._id,
      details: { machineId: machine._id },
    });
  }
  return revoked;
}

/**
 * The code `pairingCodes.issuePairingCode` drew, as its salt and scrypt hash:
 * every check again, every open code of the machine closed (replaced), then
 * the new one, open for 30 minutes. Answers when it expires.
 */
export const storeIssuedCode = internalMutation({
  args: { machineId: v.id("machines"), salt: v.string(), hash: v.string() },
  returns: v.object({ expiresAt: v.number() }),
  handler: async (ctx, args) => {
    const caller = await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);
    const now = Date.now();
    const { machine, organizationId, serialPi } = await checkedIssue(
      ctx,
      caller,
      args.machineId,
      now,
    );
    await closeOpenCodes(ctx, machine, caller, "replaced", now);
    const expiresAt = now + PAIRING_TTL_MS;
    const pairingId = await ctx.db.insert("machine_pairing_codes", {
      machineId: machine._id,
      organizationId,
      serialPi,
      salt: args.salt,
      hash: args.hash,
      createdBy: caller._id,
      createdAt: now,
      expiresAt,
      failures: 0,
    });
    await audit(ctx, {
      actor: userActor(caller),
      source: "site",
      organizationId: machine.organizationId,
      action: "machine_pairing.issued",
      targetId: pairingId,
      details: { machineId: machine._id, expiresAt },
    });
    return { expiresAt };
  },
});

/**
 * Revoke the machine's open code, if it has one: no console can claim it any
 * more. Admins only, as an issue; a machine the caller does not administer
 * answers `machine_not_found`.
 */
export const revokePairingCode = mutation({
  args: { machineId: v.id("machines") },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);
    const machine = await managedMachine(ctx, caller, args.machineId);
    await closeOpenCodes(ctx, machine, caller, "revoked", Date.now());
    return null;
  },
});

/**
 * Whether the machine has a code a console can claim now, and until when:
 * never the code, its salt or its hash. Null when it has none, and for anyone
 * but an admin of the machine's organisation (or the Anheart admin).
 */
export const pairingState = query({
  args: { machineId: v.id("machines") },
  returns: v.union(
    v.null(),
    v.object({
      createdAt: v.number(),
      expiresAt: v.number(),
      createdByName: v.union(v.string(), v.null()),
    }),
  ),
  handler: async (ctx, args) => {
    const caller = await getCurrentUserOrThrow(ctx);
    if (!ORGANIZATION_ADMIN_ROLES.includes(caller.role)) return null;
    const machine = await ctx.db.get(args.machineId);
    if (
      !machine ||
      machine.isDeleted ||
      !inScope(caller, machine.organizationId)
    )
      return null;
    const now = Date.now();
    const live = (
      await ctx.db
        .query("machine_pairing_codes")
        .withIndex("by_machine_and_closed", (q) =>
          q.eq("machineId", machine._id).eq("closedAt", undefined),
        )
        .collect()
    ).find((code) => pairingIsLive(code, now));
    if (live === undefined) return null;
    const author = await ctx.db.get(live.createdBy);
    return {
      createdAt: live.createdAt,
      expiresAt: live.expiresAt,
      createdByName:
        author === null ? null : `${author.firstName} ${author.lastName}`,
    };
  },
});

/**
 * The open, unburned codes of a serial number, with what verifies them. The
 * action that verifies (`pairingCodes.verifyPairing`) keeps one not expired
 * on its own clock; the claim checks everything again in its transaction.
 */
export const pairingCandidates = internalQuery({
  args: { serialPi: v.string() },
  returns: v.array(
    v.object({
      pairingId: v.id("machine_pairing_codes"),
      salt: v.string(),
      hash: v.string(),
      expiresAt: v.number(),
    }),
  ),
  handler: async (ctx, args) =>
    (await openCodesOfSerial(ctx, args.serialPi))
      .filter((code) => code.failures < PAIRING_MAX_FAILURES)
      .map((code) => ({
        pairingId: code._id,
        salt: code.salt,
        hash: code.hash,
        expiresAt: code.expiresAt,
      })),
});

const REFUSED = { status: "refused" } as const;

/**
 * The verdict of `pairingCodes.verifyPairing` on one code, applied in one
 * transaction that reads the code again: open, not expired, under five
 * failures, or refused whatever the verdict. A wrong code counts one failure
 * (the fifth burns the code); a right one claims it: a new key for the
 * machine, every other key revoked at once (ANH-165, no overlap), the code
 * closed with the key it gave. Two verdicts on the same code are applied one
 * after the other, each on what the previous one left. `pairingId` null (no
 * code open for the serial number) changes nothing and reads nothing.
 */
export const claimPairing = internalMutation({
  args: {
    pairingId: v.union(v.id("machine_pairing_codes"), v.null()),
    matched: v.boolean(),
  },
  returns: claimOutcome,
  handler: async (ctx, args) => {
    if (args.pairingId === null) return REFUSED;
    const code = await ctx.db.get(args.pairingId);
    const now = Date.now();
    if (code === null || !pairingIsLive(code, now)) return REFUSED;
    if (!args.matched) {
      const failures = code.failures + 1;
      if (failures < PAIRING_MAX_FAILURES) {
        await ctx.db.patch(code._id, { failures });
        return REFUSED;
      }
      await ctx.db.patch(code._id, {
        failures,
        closedAt: now,
        closedAs: "burned",
      });
      await audit(ctx, {
        actor: SYSTEM_ACTOR,
        source: "machine",
        organizationId: code.organizationId,
        action: "machine_pairing.burned",
        targetId: code._id,
        details: { machineId: code.machineId, count: failures },
      });
      return REFUSED;
    }
    // The machine the code was issued for, still the same: usable, in the
    // same organisation, its registry still naming the same Pi.
    const machine = await ctx.db.get(code.machineId);
    if (
      !mayAuthenticate(machine) ||
      machine.organizationId !== code.organizationId ||
      (await registrySerialOf(ctx, machine._id)) !== code.serialPi
    ) {
      return REFUSED;
    }
    const { issued, replaced } = await rotateMachineKey(ctx, machine, {
      overlapMs: 0,
      expiresAt: undefined,
      createdBy: code.createdBy,
      now,
    });
    await ctx.db.patch(code._id, {
      closedAt: now,
      closedAs: "claimed",
      keyId: issued.keyId,
    });
    const origin = {
      actor: machineActor(machine._id),
      source: "machine",
      organizationId: machine.organizationId,
    } as const;
    await audit(ctx, {
      ...origin,
      action: "machine_pairing.claimed",
      targetId: code._id,
      details: { machineId: machine._id },
    });
    await audit(ctx, {
      ...origin,
      action: "machine_key.created",
      targetId: issued.keyId,
      details: { machineId: machine._id, count: replaced.length },
    });
    for (const keyId of replaced) {
      await audit(ctx, {
        ...origin,
        action: "machine_key.revoked",
        targetId: keyId,
        details: { machineId: machine._id },
      });
    }
    return {
      status: "claimed" as const,
      machineId: machine._id,
      apiKey: issued.apiKey,
      keyPrefix: issued.prefix,
    };
  },
});

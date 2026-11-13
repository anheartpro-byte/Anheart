/**
 * Data migration for the retirement of the ECG recording mode.
 *
 * One internal mutation, run by hand once per deployment after this code is
 * deployed (never from the application, never by a schedule):
 *
 *     npx convex run migrations/retireLegacyRecording:failOpenRecordingSessions
 *
 * It is idempotent: a second run finds nothing left to change and says so in
 * its result. Its twin `removeMachineConfig` ran on every deployment in
 * October 2026, then left with the field it cleared (ANH-211).
 *
 * What a "recording session" is in the data: a row of `sessions` WITHOUT
 * `kind`. The recording mode never wrote that field; every training session
 * carries "auto" or "manual". The rows keep their shape (no `kind` is added),
 * so the queries that list sessions go on reporting them as "recording".
 */
import { v } from "convex/values";
import { internalMutation } from "../_generated/server";

/** Written as `endReason` on every recording session this migration closes. */
export const LEGACY_END_REASON = "legacy mode retired";

const OPEN_STATUSES = ["pending", "active"] as const;

/**
 * Mark `failed` every recording session still `pending` or `active`.
 *
 * Nothing can start, feed or end such a session any more, and a pending one
 * blocks every auto launch on its machine and the deletion of that machine.
 * Finished recording sessions and all training sessions are left untouched.
 *
 * The machine's own status is deliberately not written: every heartbeat
 * recomputes it, and the offline check takes over when the heartbeats stop.
 *
 * Reads go through `by_machine_and_status`, one machine at a time, so the
 * mutation never scans the whole `sessions` table.
 */
export const failOpenRecordingSessions = internalMutation({
  args: {},
  returns: v.object({
    machinesChecked: v.number(),
    sessionsFailed: v.number(),
  }),
  handler: async (ctx) => {
    const now = Date.now();
    const machines = await ctx.db.query("machines").collect();
    let sessionsFailed = 0;
    for (const machine of machines) {
      for (const status of OPEN_STATUSES) {
        const open = await ctx.db
          .query("sessions")
          .withIndex("by_machine_and_status", (q) =>
            q.eq("machineId", machine._id).eq("status", status),
          )
          .collect();
        for (const session of open) {
          if (session.kind !== undefined) continue; // a training session
          await ctx.db.patch(session._id, {
            status: "failed",
            endedAt: now,
            endReason: LEGACY_END_REASON,
          });
          sessionsFailed += 1;
        }
      }
    }
    return { machinesChecked: machines.length, sessionsFailed };
  },
});

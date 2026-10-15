import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";

const crons = cronJobs();
crons.daily(
  "record-retention",
  { hourUTC: 3, minuteUTC: 37 },
  internal.recordStorage.enforceRetention,
  { cursor: null },
);
crons.interval(
  "recover-record-uploads",
  { minutes: 5 },
  internal.recordStorage.sweepUploads,
  { cursor: null },
);

/**
 * Check for offline machines every minute
 * Machines are marked offline if no heartbeat received in 90 seconds, and
 * training sessions the server has had no news of for 10 minutes are marked
 * interrupted, end unknown (`training.interruptSilentSessions`)
 */
crons.interval(
  "check-offline-machines",
  { minutes: 1 },
  internal.machines.checkOfflineMachines,
  {},
);

/**
 * Every minute: a launch from the dashboard that its machine has not armed
 * within `PENDING_SESSION_TTL_S` (300 s by default) fails, "expired before
 * the machine armed it", and so does one whose rights, rider or machine
 * changed while it waited (`training.sweepPendingSessions`, ANH-144).
 */
crons.interval(
  "sweep-pending-sessions",
  { minutes: 1 },
  internal.training.sweepPendingSessions,
  {},
);

/**
 * Revoke the machine keys whose expiry has passed (ANH-165): the keys a
 * rotation replaced, once their overlap is over, and the keys created with an
 * expiry. Such a key already opens nothing; the job writes it down.
 */
crons.interval(
  "revoke-expired-machine-keys",
  { minutes: 5 },
  internal.machineKeys.revokeExpiredKeys,
  {},
);

crons.daily(
  "purge-machine-health",
  { hourUTC: 3, minuteUTC: 17 },
  internal.machineHealth.purge,
  {},
);

/**
 * Every day: the alerts to the gestionnaires not changed for 90 days are
 * deleted, read or not (`notifications.purge`, ANH-90).
 */
crons.daily(
  "purge-notifications",
  { hourUTC: 3, minuteUTC: 47 },
  internal.notifications.purge,
  {},
);

export default crons;

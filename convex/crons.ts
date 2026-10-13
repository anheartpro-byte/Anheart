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
 * Machines are marked offline if no heartbeat received in 90 seconds
 */
crons.interval(
  "check-offline-machines",
  { minutes: 1 },
  internal.machines.checkOfflineMachines,
  {},
);

crons.daily(
  "purge-machine-health",
  { hourUTC: 3, minuteUTC: 17 },
  internal.machineHealth.purge,
  {},
);

export default crons;

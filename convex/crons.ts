import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";

const crons = cronJobs();

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

export default crons;

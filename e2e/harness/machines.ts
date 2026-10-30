import { expect } from "@playwright/test";
import { startConsole, type PiConsole } from "./console";
import { callAs } from "./convex";
import { identityOf, type RunInfo } from "./run";
import { rememberFromTest } from "./secrets";

/** A machine a test makes, and its key (kept in memory, masked and counted). */
export type Machine = { id: string; name: string; key: string };

/** A machine of one of the run's client organisations, made by the Anheart admin, these managers assigned. */
export async function createMachine(
  run: RunInfo,
  organization: "Client A" | "Client B",
  name: string,
  managerIds: string[],
): Promise<Machine> {
  const admin = identityOf(run, "admin");
  const organizations = await callAs<{ _id: string; name: string }[]>(run.backend, admin, "query", "organizations:listOrganizations", {});
  const organizationId = organizations.find((o) => o.name === `e2e-${run.runId} ${organization}`)?._id;
  if (!organizationId) throw new Error(`organisation ${organization} absente`);
  const made = await callAs<{ machineId: string; apiKey: string }>(run.backend, admin, "mutation", "machines:createMachine", {
    name,
    organizationId,
    gestionnaireIds: managerIds,
  });
  return { id: made.machineId, name, key: rememberFromTest(made.apiKey) };
}

/**
 * The real simulated console of this machine, started, linked to the local
 * backend, the machine online on the server and its programmes there: what a
 * launch needs.
 */
export async function startOnline(run: RunInfo, machine: Machine): Promise<PiConsole> {
  const pi = await startConsole({ runDir: run.runDir, repoRoot: run.repoRoot, backendUrl: run.backend.url, machineApiKey: machine.key });
  await expect
    .poll(async () => (await pi.panel()).body.dashboard?.state, { message: "le lien de la console", timeout: 30_000 })
    .toBe("reachable");
  await expect
    .poll(
      async () =>
        (await callAs<{ status: string } | null>(run.backend, identityOf(run, "admin"), "query", "machines:getMachine", {
          machineId: machine.id,
        }))?.status,
      { message: `${machine.name} en ligne sur le serveur`, timeout: 30_000 },
    )
    .not.toBe("offline");
  // Its programmes too: a launch names one of those the console sent.
  await expect
    .poll(
      async () =>
        (await callAs<unknown[]>(run.backend, identityOf(run, "admin"), "query", "training:listMachineProfiles", {
          machineId: machine.id,
        })).length,
      { message: `les programmes de ${machine.name} sur le serveur`, timeout: 30_000 },
    )
    .toBeGreaterThan(0);
  return pi;
}

/** Every session of a machine, as the Anheart admin lists them, newest first. */
export async function sessionsOn(run: RunInfo, machineId: string) {
  return await callAs<{ _id: string; status: string }[]>(run.backend, identityOf(run, "admin"), "query", "sessions:listSessions", {
    machineId,
  });
}

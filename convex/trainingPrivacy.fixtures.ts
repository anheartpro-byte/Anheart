import { convexTest } from "convex-test";
import { internal } from "./_generated/api";
import schema from "./schema";

export const now = 1_800_000_000_000;

/**
 * A deployment as it was before organisations existed: no row carries an
 * organisation, no token carries an organisation claim, and Clerk
 * Organizations is not configured (`ANHEART_ORG_ID` unset). The
 * multi-organisation migration (ANH-114) then runs once, as an operator would
 * run it, so these suites prove that the single-organisation behaviour they
 * describe is unchanged after it.
 */
export async function trainingFixture(
  modules: Record<string, () => Promise<unknown>>,
) {
  delete process.env.ANHEART_ORG_ID;
  const t = convexTest(schema, modules);
  const ids = await t.run(async (ctx) => {
    const users = await Promise.all(
      (
        [
          "launcher",
          "rider",
          "manager",
          "outsider",
          "otherManager",
          "admin",
        ] as const
      ).map((name) =>
        ctx.db.insert("users", {
          clerkId: name,
          role:
            name === "admin"
              ? "admin"
              : name.endsWith("anager")
                ? "gestionnaire"
                : "user",
          firstName: name,
          lastName: "Synthetic",
          email: `${name}@example.invalid`,
          language: "en",
          createdAt: now,
        }),
      ),
    );
    const [launcher, rider, manager, outsider, otherManager, admin] = users;
    if (
      !launcher ||
      !rider ||
      !manager ||
      !outsider ||
      !otherManager ||
      !admin
    ) {
      throw new TypeError("Incomplete test fixture");
    }
    const machineFields = {
      name: "Synthetic machine",
      apiKey: "synthetic-hash",
      status: "in_session",
      lastHeartbeat: now,
      createdAt: now,
      programsEnabled: true,
    } as const;
    const machineId = await ctx.db.insert("machines", machineFields);
    const otherMachineId = await ctx.db.insert("machines", machineFields);
    for (const userId of [launcher, rider]) {
      await ctx.db.insert("machine_user_permissions", {
        machineId,
        userId,
        grantedBy: manager,
        createdAt: now,
      });
    }
    for (const [gestionnaireId, managedMachine] of [
      [manager, machineId],
      [otherManager, otherMachineId],
    ] as const) {
      await ctx.db.insert("machine_gestionnaires", {
        machineId: managedMachine,
        gestionnaireId,
        isOwner: true,
        createdAt: now,
        createdBy: admin,
      });
    }
    const sessionId = await ctx.db.insert("sessions", {
      machineId,
      userId: rider,
      status: "active",
      startedAt: now,
      channels: ["ECG"],
      kind: "auto",
    });
    const live = {
      runMode: "seance",
      phase: "hold",
      bpm: 137,
      motorRpm: 1200,
      outputRpm: 24,
      setpointMotorRpm: 1200,
      gLoad: 1.2,
      safetyAction: "none",
      sessionId,
      updatedAt: now,
    };
    await ctx.db.patch(machineId, { live });
    await ctx.db.insert("training_telemetry", {
      sessionId,
      machineId,
      t: now,
      elapsedS: 15,
      phase: "hold",
      bpm: 137,
      motorRpm: 1200,
      outputRpm: 24,
      setpointMotorRpm: 1200,
      gLoad: 1.2,
      safetyAction: "none",
    });
    return {
      launcher,
      rider,
      manager,
      machineId,
      otherMachineId,
      sessionId,
      live,
    };
  });
  await t.mutation(internal.migrations.multiOrganization.attachExistingRowsToAnheart, {});
  return { t, ...ids };
}

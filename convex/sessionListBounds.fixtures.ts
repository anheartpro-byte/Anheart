import type { Doc, Id } from "./_generated/dataModel";
import { modules, NOW, seedWorld, type World } from "./test.setup";

export const HISTORY_SIZE = 2400;

export type SeededSession = {
  id: Id<"sessions">;
  rank: number;
  machineId: Id<"machines">;
  userId?: Id<"users">;
  organizationId?: Id<"organizations">;
  startedAt: number;
  status: Doc<"sessions">["status"];
};

export async function boundsWorld(documentsRead: number, sparse = false) {
  const w = await seedWorld(modules, {
    transactionLimits: { documentsRead },
  });
  const seeded = await w.t.run(async (ctx) => {
    const links = await ctx.db.query("machine_gestionnaires").collect();
    for (const link of links) {
      if (link.gestionnaireId === w.manager) await ctx.db.delete(link._id);
      if (link.gestionnaireId === w.otherManager) {
        // This manager's only link points outside their centre.
        await ctx.db.patch(link._id, { machineId: w.orgBMachine });
      }
    }
    const machines = [w.machine, w.otherMachine];
    for (let i = 2; i < 20; i++) {
      machines.push(
        await ctx.db.insert("machines", {
          organizationId: w.orgA,
          name: `Synthetic machine ${i}`,
          apiKey: "synthetic-hash",
          status: "offline",
          lastHeartbeat: NOW,
          createdAt: NOW,
        }),
      );
    }
    if (sparse) {
      for (const machineId of machines.slice(0, 2)) {
        await ctx.db.insert("machine_gestionnaires", {
          organizationId: w.orgA,
          machineId,
          gestionnaireId: w.manager,
          isOwner: false,
          createdAt: NOW,
          createdBy: w.admin,
        });
      }
    }
    const rows: SeededSession[] = [];
    for (let rank = 0; rank < HISTORY_SIZE; rank++) {
      // Sparse readable rows throughout the history, including the page cuts.
      const visible = sparse && (rank % 97 <= 1 || rank % 1000 === 0);
      const machineId = visible
        ? machines[rank % 3]
        : machines[2 + (rank % 18)];
      const userId =
        visible && (rank % 3 === 2 || rank % 4 === 0) ? w.manager : undefined;
      const organizationId =
        sparse && rank % 503 === 0
          ? undefined
          : sparse && rank % 509 === 0
            ? w.orgB
            : w.orgA;
      const status = rank % 2 === 0 ? "completed" : "active";
      // Adjacent rows share a start; the later write must come first.
      const startedAt = NOW - Math.floor(rank / 2) * 1000;
      const id = await ctx.db.insert("sessions", {
        organizationId,
        machineId,
        userId,
        status,
        startedAt,
        kind: "auto",
        channels: ["ECG"],
      });
      rows.push({
        id,
        rank,
        machineId,
        userId,
        organizationId,
        status,
        startedAt,
      });
    }
    if (sparse) {
      // Cross-centre and pre-migration rows on a managed machine.
      for (const organizationId of [w.orgB, undefined]) {
        const rank = rows.length;
        const id = await ctx.db.insert("sessions", {
          organizationId,
          machineId: w.machine,
          userId: w.manager,
          status: "completed",
          startedAt: NOW + 1000,
          channels: ["ECG"],
        });
        rows.push({
          id,
          rank,
          machineId: w.machine,
          userId: w.manager,
          organizationId,
          status: "completed",
          startedAt: NOW + 1000,
        });
      }
    }
    return rows;
  });
  return { ...w, seeded };
}

/** The rule of docs/convex.md section 3, independently of the list helpers. */
export function expectedForManager(w: World, rows: SeededSession[]) {
  return rows
    .filter(
      (s) =>
        s.organizationId === w.orgA &&
        (s.machineId === w.machine ||
          s.machineId === w.otherMachine ||
          s.userId === w.manager),
    )
    .sort((a, b) => b.startedAt - a.startedAt || b.rank - a.rank);
}

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./_generated/api";
import {
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  seedLaunchWorld,
} from "./test.setup";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  configureAnheartOrganization(null);
});

describe("ANH-167 C2 physiology organisation scope", () => {
  it.each([true, false])(
    "R1-15 excludes dormant manager relations with active membership %s",
    async (active) => {
      const w = await seedLaunchWorld(modules);
      await w.t.run(async (ctx) => {
        await ctx.db.insert("user_gestionnaires", {
          userId: w.patient,
          gestionnaireId: w.orgBManager,
          organizationId: w.orgB,
          createdBy: w.admin,
          createdAt: NOW,
        });
        await ctx.db.insert("memberships", {
          userId: w.patient,
          organizationId: w.orgB,
          role: "user",
          active: false,
        });
        if (!active) {
          for (const row of await ctx.db.query("memberships").collect())
            if (row.userId === w.patient)
              await ctx.db.patch(row._id, { active: false });
        }
      });
      await as(w.t, "admin").mutation(api.training.setUserPhysiology, {
        userId: w.patient,
        hrMax: 187,
      });
      expect((await w.t.run((ctx) => ctx.db.get(w.patient)))?.hrMax).toBe(187);
      const written = (
        await w.t.run((ctx) => ctx.db.query("audit_log").collect())
      ).filter((row) => row.action === "physiology.updated");
      expect(written).toHaveLength(1);
      expect(written[0]).toMatchObject({
        action: "physiology.updated",
        targetId: w.patient,
        details: { fields: ["hrMax"] },
      });
      expect(written[0].organizationId).toBe(active ? w.orgA : undefined);
      const page = await as(w.t, "orgBAdmin").query(api.audit.list, {
        action: "physiology.updated",
        paginationOpts: { cursor: null, numItems: 200 },
      });
      expect(page.page).toEqual([]);
    },
  );
});

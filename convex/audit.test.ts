import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FunctionArgs } from "convex/server";
import { api } from "./_generated/api";
import type { Doc } from "./_generated/dataModel";
import { chooseAuditIndex } from "./lib/auditIndex";
import {
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  seedWorld,
  type Actor,
} from "./test.setup";

type Filters = Omit<FunctionArgs<typeof api.audit.list>, "paginationOpts">;
type Row = Omit<Doc<"audit_log">, "_id" | "_creationTime">;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  configureAnheartOrganization(null);
});

async function world() {
  const w = await seedWorld(modules);
  const rows: Row[] = [
    {
      at: NOW + 1,
      organizationId: w.orgA,
      actorId: w.manager,
      actorRole: "gestionnaire",
      action: "user.role_changed",
      targetType: "user",
      targetId: w.patient,
      source: "site",
      requestId: "synthetic-1",
      details: { role: "user" },
    },
    {
      at: NOW + 2,
      organizationId: w.orgB,
      actorId: w.orgBAdmin,
      actorRole: "org_admin",
      action: "user.role_changed",
      targetType: "user",
      targetId: w.orgBPatient,
      source: "site",
      requestId: "synthetic-2",
    },
    {
      at: NOW + 3,
      organizationId: w.orgA,
      actorId: w.machine,
      actorRole: "machine",
      action: "session.started",
      targetType: "session",
      targetId: "synthetic-session",
      source: "machine",
      requestId: "synthetic-3",
    },
    {
      at: NOW + 4,
      organizationId: w.orgA,
      actorRole: "system",
      action: "machine_key.expired_revoked",
      targetType: "machine_key",
      targetId: "synthetic-key",
      source: "cron",
      requestId: "synthetic-4",
    },
    {
      at: NOW + 5,
      actorRole: "system",
      action: "medical_parameters.registered",
      targetType: "medical_parameters_version",
      targetId: "synthetic-version",
      source: "operator",
      requestId: "synthetic-5",
    },
    {
      at: NOW + 6,
      organizationId: w.orgA,
      actorId: w.manager,
      actorRole: "gestionnaire",
      action: "machine.created",
      targetType: "machine",
      targetId: w.machine,
      source: "site",
      requestId: "synthetic-6",
    },
    {
      at: NOW + 7,
      organizationId: w.orgA,
      actorId: w.manager,
      actorRole: "gestionnaire",
      action: "user.role_changed",
      targetType: "user",
      targetId: w.patient,
      source: "site",
      requestId: "synthetic-7",
    },
    {
      at: NOW + 8,
      organizationId: w.orgA,
      actorId: w.patient,
      actorRole: "user",
      action: "user.role_changed",
      targetType: "user",
      targetId: "synthetic-deleted-user",
      source: "site",
      requestId: "synthetic-8",
    },
  ];
  await w.t.run(async (ctx) => {
    for (const row of rows) await ctx.db.insert("audit_log", row);
  });
  const read = (
    filters: Filters = {},
    actor: Actor = "admin",
    cursor: string | null = null,
    numItems = 50,
  ) =>
    as(w.t, actor).query(api.audit.list, {
      from: NOW + 1,
      ...filters,
      paginationOpts: { cursor, numItems },
    });
  return { ...w, rows, read };
}

describe("ANH-167 T03-a audit reads", () => {
  it("orders all organisations and global rows by descending at, with no email in labels", async () => {
    const w = await world();
    const { page } = await w.read();
    expect(page.map((row) => row.at)).toEqual(
      w.rows.map((row) => row.at).reverse(),
    );
    const manager = await w.t.run((ctx) => ctx.db.get(w.manager));
    const machine = await w.t.run((ctx) => ctx.db.get(w.machine));
    const patient = await w.t.run((ctx) => ctx.db.get(w.patient));
    expect(page.find((row) => row.requestId === "synthetic-1")).toMatchObject({
      actorLabel: `${manager?.firstName} ${manager?.lastName}`,
      targetLabel: `${patient?.firstName} ${patient?.lastName}`,
      organizationName: "Centre A",
      details: { role: "user" },
    });
    expect(
      page.find((row) => row.requestId === "synthetic-6")?.targetLabel,
    ).toBe(machine?.name);
    expect(page.find((row) => row.actorRole === "machine")?.actorLabel).toBe(
      machine?.name,
    );
    expect(page.find((row) => row.requestId === "synthetic-5")).toMatchObject({
      organizationName: null,
      actorLabel: null,
      targetLabel: null,
    });
    expect(
      page.find((row) => row.requestId === "synthetic-8")?.targetLabel,
    ).toBeNull();
    expect(JSON.stringify(page)).not.toContain("@");
  });

  it("filters actor, role, action, target type, target id, organisation and their combinations", async () => {
    const w = await world();
    const filters: Filters[] = [
      { actorId: w.manager },
      { actorId: w.machine },
      { actorRole: "machine" },
      { actorRole: "system" },
      { action: "user.role_changed" },
      { targetType: "user" },
      { targetType: "user", targetId: w.patient },
      { organizationId: w.orgB },
      {
        actorId: w.manager,
        actorRole: "gestionnaire",
        action: "user.role_changed",
        targetType: "user",
        targetId: w.patient,
        organizationId: w.orgA,
        from: NOW + 2,
        to: NOW + 8,
      },
    ];
    for (const filter of filters) {
      const expected = w.rows
        .filter((row) =>
          Object.entries(filter).every(([key, value]) =>
            key === "from"
              ? row.at >= Number(value)
              : key === "to"
                ? row.at < Number(value)
                : row[key as keyof Row] === value,
          ),
        )
        .reverse();
      expect(
        (await w.read(filter)).page.map((row) => row.requestId),
        JSON.stringify(filter),
      ).toEqual(expected.map((row) => row.requestId));
    }
  });

  it("uses inclusive from and exclusive to and returns an empty page for invalid ranges or an untyped target", async () => {
    const w = await world();
    expect(
      (await w.read({ from: NOW + 2, to: NOW + 4 })).page.map((row) => row.at),
    ).toEqual([NOW + 3, NOW + 2]);
    expect(
      (await w.read({ from: undefined, to: NOW + 2 })).page.every(
        (row) => row.at < NOW + 2,
      ),
    ).toBe(true);
    for (const filters of [
      { from: NOW + 3, to: NOW + 3 },
      { from: NOW + 4, to: NOW + 2 },
      { targetId: w.patient },
    ]) {
      expect(await w.read(filters)).toEqual({
        page: [],
        isDone: true,
        continueCursor: "",
      });
    }
  });

  it("paginates only matching rows across multiple pages", async () => {
    const w = await world();
    let cursor: string | null = null;
    const ids: string[] = [];
    do {
      const page = await w.read(
        { actorRole: "gestionnaire", action: "user.role_changed" },
        "admin",
        cursor,
        1,
      );
      ids.push(...page.page.map((row) => row.requestId));
      if (page.isDone) break;
      cursor = page.continueCursor;
    } while (true);
    expect(ids).toEqual(["synthetic-7", "synthetic-1"]);
  });

  it("isolates organisation admins, hides global rows and refuses a foreign organisation", async () => {
    const w = await world();
    for (const [actor, organizationId, foreign] of [
      ["orgAdmin", w.orgA, w.orgB],
      ["orgBAdmin", w.orgB, w.orgA],
    ] as const) {
      const { page } = await w.read({}, actor);
      expect(page.length).toBeGreaterThan(0);
      expect(page.every((row) => row.organizationId === organizationId)).toBe(
        true,
      );
      expect((await w.read({ organizationId: foreign }, actor)).page).toEqual(
        [],
      );
    }
  });

  it.each([
    "anonymous",
    "manager",
    "patient",
    "orgBManager",
    "orgBPatient",
  ] as const)("refuses %s", async (actor) => {
    const w = await world();
    await expect(w.read({}, actor)).rejects.toThrow(
      actor === "anonymous" ? "not_authenticated" : "not_authorized",
    );
  });

  it("uses every selected range with an indexed period and remaining predicates", async () => {
    const w = await world();
    for (const organizationId of [undefined, w.orgA]) {
      for (const actorId of [undefined, w.manager]) {
        for (const action of [undefined, "user.role_changed"] as const) {
          for (const target of [
            {},
            { targetType: "user" as const },
            { targetType: "user" as const, targetId: w.patient },
          ]) {
            const filters = {
              organizationId,
              actorId,
              action,
              ...target,
              from: NOW + 1,
              to: NOW + 8,
            };
            const expected = w.rows
              .filter(
                (row) =>
                  row.at < NOW + 8 &&
                  Object.entries({
                    organizationId,
                    actorId,
                    action,
                    ...target,
                  }).every(
                    ([key, value]) =>
                      value === undefined || row[key as keyof Row] === value,
                  ),
              )
              .reverse();
            expect(
              (await w.read(filters)).page.map((row) => row.requestId),
            ).toEqual(expected.map((row) => row.requestId));
          }
        }
      }
    }
  });

  it("falls back to null labels for missing records and invalid target identifiers", async () => {
    const w = await world();
    await w.t.run(async (ctx) => {
      await ctx.db.delete(w.machine);
    });
    expect(
      (await w.read({ actorId: w.machine })).page[0].actorLabel,
    ).toBeNull();
    expect(
      (await w.read({ targetType: "machine", targetId: w.machine })).page[0]
        .targetLabel,
    ).toBeNull();
  });
});

describe("ANH-167 T03-a pure index priority", () => {
  it("selects complete target, actor, action, target type then time for every combination", async () => {
    const w = await world();
    for (const organizationId of [undefined, w.orgA])
      for (const actorId of [undefined, w.manager])
        for (const action of [undefined, "user.role_changed"] as const)
          for (const target of [
            {},
            { targetType: "user" as const },
            { targetType: "user" as const, targetId: w.patient },
          ]) {
            const filters = { organizationId, actorId, action, ...target };
            const suffix =
              "targetId" in target
                ? "target_and_at"
                : actorId
                  ? "actor_and_at"
                  : action
                    ? "action_and_at"
                    : "targetType" in target
                      ? "target_type_and_at"
                      : "at";
            expect(chooseAuditIndex(filters)).toBe(
              `by_${organizationId ? "organization_and_" : ""}${suffix}`,
            );
          }
  });
});

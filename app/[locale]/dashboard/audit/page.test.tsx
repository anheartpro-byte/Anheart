// @vitest-environment jsdom
import { act, fireEvent, screen } from "@testing-library/react";
import type { FunctionReturnType } from "convex/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { format } from "date-fns";
import { enUS, fr as frLocale } from "date-fns/locale";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { auditCsv, type AuditRow } from "@/lib/auditCsv";
import fr from "@/messages/fr.json";
import en from "@/messages/en.json";
import {
  answer,
  argsAsked,
  lastArgsAsked,
  NOW,
  pagesOf,
  renderPage,
  signedInAs,
} from "@/test-support/pages";
import AuditPage from "./page";

const client = vi.hoisted(() => ({ query: vi.fn(), save: vi.fn() }));
vi.mock("convex/react", async () => ({
  ...(await import("@/test-support/pages")).convexReact,
  useConvex: () => client,
}));
vi.mock("@/lib/download", () => ({ save: client.save }));

const row: AuditRow = {
  _id: "synthetic-audit" as Id<"audit_log">,
  at: NOW,
  organizationId: "synthetic-org" as Id<"organizations">,
  organizationName: "Synthetic Centre",
  actorId: "synthetic-person" as Id<"users">,
  actorRole: "org_admin",
  actorLabel: "Synthetic Person",
  action: "user.role_changed",
  targetType: "user",
  targetId: "synthetic-target" as Id<"users">,
  targetLabel: "Synthetic Target",
  details: { fields: ["role"], role: "user", count: 1 },
  requestId: "synthetic-request-full-reference",
  source: "site",
};
const machineId = "synthetic-machine" as Id<"machines">;
const firstPage = (filters: object = {}) => ({
  ...filters,
  paginationOpts: { numItems: 50, cursor: null },
});
const last = () => lastArgsAsked(api.audit.list);

beforeEach(() => {
  signedInAs("admin");
  answer(api.audit.list, pagesOf([row]));
  answer(api.users.listUsers, [
    {
      _id: row.actorId as Id<"users">,
      firstName: "Synthetic",
      lastName: "Person",
      role: "admin",
      email: "synthetic@example.invalid",
      language: "fr",
      createdAt: NOW,
    },
  ]);
  answer(api.machines.listMachines, [
    {
      _id: machineId,
      name: "Synthetic Machine",
      status: "offline",
      lastHeartbeat: NOW,
      serverNow: NOW,
      location: undefined,
      isDeleted: undefined,
    },
  ]);
  answer(api.organizations.listOrganizations, [
    {
      _id: row.organizationId!,
      name: "Synthetic Centre",
      slug: "synthetic-centre",
    },
  ]);
  client.query
    .mockReset()
    .mockResolvedValue({ page: [], isDone: true, continueCursor: "" });
  client.save.mockReset();
});

async function choose(
  view: Awaited<ReturnType<typeof renderPage>>,
  label: string,
  option: string,
) {
  await view.user.click(screen.getByRole("combobox", { name: label }));
  await view.user.click(screen.getByRole("option", { name: option }));
}

describe("ANH-167 T03-c audit page access", () => {
  it.each(["gestionnaire", "user"] as const)(
    "refuses %s without asking audit or filter lookups",
    async (role) => {
      signedInAs(role);
      await renderPage(<AuditPage />);
      expect(screen.getByText(fr.audit.accessDenied)).toBeTruthy();
      expect(last()).toBe("skip");
      expect(argsAsked(api.users.listUsers)).toEqual([]);
    },
  );

  it("skips audit while the role is unknown and refuses an unaccepted organisation or signed-out account", async () => {
    answer(api.users.getCurrentUser, undefined);
    const view = await renderPage(<AuditPage />);
    expect(last()).toBe("skip");
    expect(screen.queryByRole("heading")).toBeNull();
    signedInAs("admin", {
      organizationAccess: "organization_not_synchronized",
    });
    await view.refresh();
    expect(screen.getByText(fr.audit.accessDenied)).toBeTruthy();
    expect(last()).toBe("skip");
    answer(api.users.getCurrentUser, null);
    await view.refresh();
    expect(screen.getByText(fr.audit.accessDenied)).toBeTruthy();
    expect(last()).toBe("skip");
  });

  it.each(["admin", "org_admin"] as const)(
    "shows rows to %s and organisation controls only to the Anheart admin",
    async (role) => {
      signedInAs(role);
      await renderPage(<AuditPage />);
      expect(
        screen.getByRole("heading", { name: fr.audit.title }),
      ).toBeTruthy();
      expect(last()).toEqual(firstPage());
      expect(
        Boolean(
          screen.queryByRole("combobox", {
            name: fr.audit.filters.organization,
          }),
        ),
      ).toBe(role === "admin");
      expect(
        Boolean(
          screen.queryByRole("columnheader", {
            name: fr.audit.columns.organization,
          }),
        ),
      ).toBe(role === "admin");
      expect(screen.getByText(row.actorLabel!)).toBeTruthy();
      expect(screen.getByTitle(row.requestId).textContent).toBe(
        row.requestId.slice(0, 12),
      );
      expect(screen.queryByText("synthetic@example.invalid")).toBeNull();
      expect(screen.queryByText(JSON.stringify(row.details))).toBeNull();
      expect(lastArgsAsked(api.organizations.listOrganizations)).toEqual(
        role === "admin" ? {} : "skip",
      );
    },
  );
});

describe("ANH-167 T03-d exact filter arguments", () => {
  it("filters by a person, a machine, the system and all machine actors", async () => {
    const view = await renderPage(<AuditPage />);
    await choose(view, fr.audit.filters.actor, "Synthetic Person");
    expect(last()).toEqual(firstPage({ actorId: row.actorId }));
    await choose(view, fr.audit.filters.actor, "Synthetic Machine");
    expect(last()).toEqual(firstPage({ actorId: machineId }));
    await choose(view, fr.audit.filters.actor, fr.audit.roles.system);
    expect(last()).toEqual(firstPage({ actorRole: "system" }));
    await choose(view, fr.audit.filters.actor, fr.audit.filters.allMachines);
    expect(last()).toEqual(firstPage({ actorRole: "machine" }));
    await choose(view, fr.audit.filters.actor, fr.audit.all);
    expect(last()).toEqual(firstPage());
  });

  it("combines action, type, row target, dates and organisation and resets all filters", async () => {
    const view = await renderPage(<AuditPage />);
    await choose(
      view,
      fr.audit.filters.action,
      fr.audit.actions.user.role_changed,
    );
    expect(last()).toEqual(firstPage({ action: "user.role_changed" }));
    await choose(view, fr.audit.filters.targetType, fr.audit.targets.machine);
    expect(last()).toEqual(
      firstPage({ action: "user.role_changed", targetType: "machine" }),
    );
    await view.user.click(
      screen.getByRole("button", { name: fr.audit.filterTarget }),
    );
    const target = {
      action: "user.role_changed",
      targetType: "user",
      targetId: row.targetId,
    };
    expect(last()).toEqual(firstPage(target));
    expect(
      screen.getByText(
        fr.audit.filters.target.replace("{target}", row.targetLabel!),
      ),
    ).toBeTruthy();
    fireEvent.change(screen.getByLabelText(fr.audit.filters.from), {
      target: { value: "2026-10-24" },
    });
    expect(last()).toEqual(
      firstPage({ ...target, from: new Date(2026, 9, 24).getTime() }),
    );
    fireEvent.change(screen.getByLabelText(fr.audit.filters.to), {
      target: { value: "2026-10-25" },
    });
    const period = {
      ...target,
      from: new Date(2026, 9, 24).getTime(),
      to: new Date(2026, 9, 26).getTime(),
    };
    expect(last()).toEqual(firstPage(period));
    await choose(view, fr.audit.filters.organization, "Synthetic Centre");
    expect(last()).toEqual(
      firstPage({ ...period, organizationId: row.organizationId }),
    );
    await view.user.click(
      screen.getByRole("button", { name: fr.audit.filters.removeTarget }),
    );
    expect(last()).toEqual(
      firstPage({
        action: "user.role_changed",
        targetType: "user",
        from: period.from,
        to: period.to,
        organizationId: row.organizationId,
      }),
    );
    await view.user.click(
      screen.getByRole("button", { name: fr.audit.filters.reset }),
    );
    expect(last()).toEqual(firstPage());
    expect(
      (screen.getByLabelText(fr.audit.filters.from) as HTMLInputElement).value,
    ).toBe("");
  });

  it("clears a precise target when the target type changes, and can select all values", async () => {
    const view = await renderPage(<AuditPage />);
    await view.user.click(
      screen.getByRole("button", { name: fr.audit.filterTarget }),
    );
    await choose(view, fr.audit.filters.targetType, fr.audit.targets.machine);
    expect(last()).toEqual(firstPage({ targetType: "machine" }));
    expect(
      screen.queryByRole("button", { name: fr.audit.filters.removeTarget }),
    ).toBeNull();
    await choose(view, fr.audit.filters.targetType, fr.audit.all);
    await choose(
      view,
      fr.audit.filters.action,
      fr.audit.actions.user.role_changed,
    );
    await choose(view, fr.audit.filters.action, fr.audit.all);
    expect(last()).toEqual(firstPage());
  });

  it("loads more and shows the empty state only once exhausted", async () => {
    answer(
      api.audit.list,
      pagesOf(
        Array.from({ length: 51 }, (_, index) => ({
          ...row,
          _id: `synthetic-${index}` as Id<"audit_log">,
        })),
      ),
    );
    const view = await renderPage(<AuditPage />);
    expect(screen.getAllByRole("row")).toHaveLength(51);
    await view.user.click(
      screen.getByRole("button", { name: fr.audit.showMore }),
    );
    expect(screen.getAllByRole("row")).toHaveLength(52);
    expect(last()).toEqual({ paginationOpts: { numItems: 50, cursor: "50" } });
    answer(api.audit.list, pagesOf([]));
    await view.refresh();
    expect(screen.getByText(fr.audit.empty)).toBeTruthy();
  });

  it("formats English dates and displays global, machine and missing labels without raw JSON", async () => {
    answer(
      api.audit.list,
      pagesOf([
        {
          ...row,
          organizationId: undefined,
          organizationName: null,
          actorRole: "system",
          actorId: undefined,
          actorLabel: null,
          targetType: "session",
          targetLabel: null,
          details: undefined,
        },
      ]),
    );
    await renderPage(<AuditPage />, { locale: "en" });
    expect(screen.getByText(en.audit.global)).toBeTruthy();
    expect(screen.getByText(format(NOW, "PPp", { locale: enUS }))).toBeTruthy();
    expect(
      screen.getByText(
        `${en.audit.targets.session} : ${row.targetId.slice(0, 12)}`,
      ),
    ).toBeTruthy();
    expect(screen.getAllByText(en.audit.roles.system)).toHaveLength(2);
  });

  it("formats French dates while lookups load and falls back to the short actor identifier", async () => {
    answer(api.users.listUsers, undefined);
    answer(api.machines.listMachines, undefined);
    answer(api.organizations.listOrganizations, undefined);
    answer(api.audit.list, pagesOf([{ ...row, actorLabel: null }]));
    await renderPage(<AuditPage />);
    expect(
      screen.getByText(format(NOW, "PPp", { locale: frLocale })),
    ).toBeTruthy();
    expect(screen.getByText(row.actorId!.slice(0, 12))).toBeTruthy();
  });
});

describe("ANH-167 T03-e filtered CSV export", () => {
  it("R2-4 keeps every precise-target filter disabled during export", async () => {
    const pending =
      Promise.withResolvers<FunctionReturnType<typeof api.audit.list>>();
    client.query.mockReturnValueOnce(pending.promise);
    const view = await renderPage(<AuditPage />);
    await view.user.click(
      screen.getByRole("button", { name: fr.audit.export }),
    );
    const before = last();
    const target = screen.getByRole("button", { name: fr.audit.filterTarget });
    await view.user.click(target);
    const after = last();
    const disabled = (target as HTMLButtonElement).disabled;
    await act(async () =>
      pending.resolve({ page: [row], isDone: true, continueCursor: "" }),
    );
    expect(after).toEqual(before);
    expect(disabled).toBe(true);
    expect((target as HTMLButtonElement).disabled).toBe(false);
    await view.user.click(target);
    expect(last()).toEqual(
      firstPage({ targetType: row.targetType, targetId: row.targetId }),
    );
  });
  it("keeps export and filter controls disabled until the last page arrives", async () => {
    const pending =
      Promise.withResolvers<FunctionReturnType<typeof api.audit.list>>();
    client.query.mockReturnValueOnce(pending.promise);
    const view = await renderPage(<AuditPage />);
    await view.user.click(
      screen.getByRole("button", { name: fr.audit.export }),
    );
    expect(
      (
        screen.getByRole("button", {
          name: fr.audit.exporting,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(
      screen
        .getByRole("combobox", { name: fr.audit.filters.actor })
        .closest("fieldset")?.disabled,
    ).toBe(true);
    expect(client.save).not.toHaveBeenCalled();
    await act(async () =>
      pending.resolve({ page: [row], isDone: true, continueCursor: "" }),
    );
    expect(
      (
        screen.getByRole("button", {
          name: fr.audit.export,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
    expect(client.save).toHaveBeenCalledTimes(1);
  });

  it("reads every page with the current filters and downloads the expected CSV", async () => {
    const second = {
      ...row,
      _id: "synthetic-second" as Id<"audit_log">,
      actorLabel: "=Synthetic formula",
    };
    client.query
      .mockResolvedValueOnce({
        page: [row],
        isDone: false,
        continueCursor: "next",
      })
      .mockResolvedValueOnce({
        page: [second],
        isDone: true,
        continueCursor: "",
      });
    const view = await renderPage(<AuditPage />);
    await choose(view, fr.audit.filters.actor, "Synthetic Person");
    await choose(
      view,
      fr.audit.filters.action,
      fr.audit.actions.user.role_changed,
    );
    await view.user.click(
      screen.getByRole("button", { name: fr.audit.export }),
    );
    expect(client.query.mock.calls).toEqual([
      [
        api.audit.list,
        {
          actorId: row.actorId,
          action: "user.role_changed",
          paginationOpts: { cursor: null, numItems: 200 },
        },
      ],
      [
        api.audit.list,
        {
          actorId: row.actorId,
          action: "user.role_changed",
          paginationOpts: { cursor: "next", numItems: 200 },
        },
      ],
    ]);
    expect(client.save).toHaveBeenCalledTimes(1);
    const [blob, name] = client.save.mock.calls[0] as [Blob, string];
    expect(name).toBe(`audit-${format(NOW, "yyyy-MM-dd")}.csv`);
    expect(blob.type).toBe("text/csv;charset=utf-8");
    const reader = new FileReader();
    const read = new Promise<string>((resolve, reject) => {
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = reject;
    });
    reader.readAsText(blob);
    expect(await read).toBe(auditCsv([row, second]).slice(1));
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("caps the export at 10000 rows and asks to narrow the period", async () => {
    client.query.mockResolvedValue({
      page: Array.from({ length: 200 }, () => row),
      isDone: false,
      continueCursor: "next",
    });
    const view = await renderPage(<AuditPage />);
    await view.user.click(
      screen.getByRole("button", { name: fr.audit.export }),
    );
    expect(client.query).toHaveBeenCalledTimes(50);
    expect(screen.getByRole("status").textContent).toBe(
      fr.audit.exportTruncated,
    );
    const blob = client.save.mock.calls[0][0] as Blob;
    expect(blob.size).toBe(
      new Blob([auditCsv(Array.from({ length: 10000 }, () => row))]).size,
    );
  });

  it("reports an export failure and allows another attempt", async () => {
    client.query.mockRejectedValueOnce(new Error("synthetic failure"));
    const view = await renderPage(<AuditPage />);
    await view.user.click(
      screen.getByRole("button", { name: fr.audit.export }),
    );
    expect(screen.getByRole("status").textContent).toBe(fr.audit.exportError);
    expect(client.save).not.toHaveBeenCalled();
    await view.user.click(
      screen.getByRole("button", { name: fr.audit.export }),
    );
    expect(client.save).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("status")).toBeNull();
  });
});

// @vitest-environment jsdom
import { act, fireEvent, screen, within } from "@testing-library/react";
import type { FunctionReturnType } from "convex/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import fr from "@/messages/fr.json";
import {
  answer,
  feedbackShown,
  lastArgsAsked,
  mutation,
  mutationsSent,
  NOW,
  renderPage,
  signedInAs,
} from "@/test-support/pages";
import ProgrammesPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
afterEach(() => vi.unstubAllGlobals());

const t = fr.programmes;
type Row = FunctionReturnType<typeof api.programmes.list>[number];
const PROFILE = {
  profileId: "jog_145_155_30_min",
  name: "jog 145-155, 30 min",
  totalDurationS: 1800,
  baselineS: 180,
  warmupMaxS: 300,
  holdMinS: 300,
  cooldownS: 240,
  recoveryS: 300,
  zoneLowBpm: 145,
  zoneHighBpm: 155,
  hardMaxBpm: 165,
  criticalBpm: 175,
  subjectHrMax: 175,
  minRunRpm: 55,
  maxRpm: 276,
  warmupRpmCeilingFraction: 0.6,
  channels: ["ECG" as const],
  allowAboveNameplate: false,
};
function row(status: Row["status"], id: string, overrides: Partial<Row> = {}): Row {
  return {
    _id: id as Id<"programmes">,
    _creationTime: NOW,
    ...PROFILE,
    profileId: `${PROFILE.profileId}_${status}`,
    organizationId: null,
    status,
    schemaVersion: 1,
    revision: status === "published" ? 1 : 0,
    medicalParametersVersion: "0.1.0",
    medicalSignoff: null,
    simulationReport: null,
    validatedBy: null,
    createdBy: "user-admin" as Id<"users">,
    updatedBy: "user-admin" as Id<"users">,
    createdAt: NOW,
    updatedAt: NOW,
    simulationReportUrl: null,
    ...overrides,
  };
}
const field = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
const rowOf = (name: string) =>
  within(screen.getByText(name).closest("tr") as HTMLElement);

beforeEach(() => {
  signedInAs("admin");
  answer(api.programmes.list, [
    row("draft", "p-draft"),
    row("validated", "p-validated"),
    row("published", "p-published"),
  ]);
});

describe("ANH-137 programmes page", () => {
  it("waits for an accepted organisation before asking the catalogue", async () => {
    signedInAs("gestionnaire", { organizationAccess: "no_organization" });
    await renderPage(<ProgrammesPage />);
    expect(lastArgsAsked(api.programmes.list)).toBe("skip");
    expect(screen.queryByRole("heading")).toBeNull();
  });

  it("shows a gestionnaire the published catalogue read-only, with its preview", async () => {
    signedInAs("gestionnaire");
    answer(api.programmes.list, [row("published", "p-published")]);
    const view = await renderPage(<ProgrammesPage />);
    expect(screen.getByText(t.readOnly)).toBeTruthy();
    expect(screen.queryByRole("button", { name: t.actions.new })).toBeNull();
    expect(screen.queryByRole("button", { name: t.actions.retire })).toBeNull();
    expect(screen.queryByRole("button", { name: t.actions.duplicate })).toBeNull();
    await view.user.click(screen.getByRole("button", { name: t.actions.view }));
    expect(
      screen.getByRole("img", {
        name: "Phases du programme et zone de 145 à 155 bpm sur 30:00",
      }),
    ).toBeTruthy();
    expect(screen.getByText(/Maintien 13:00/)).toBeTruthy();
  });

  it("shows an empty catalogue", async () => {
    answer(api.programmes.list, []);
    await renderPage(<ProgrammesPage />);
    expect(screen.getByText(t.empty)).toBeTruthy();
  });

  it("creates a draft, refusing with the Pi's message a zone above 0.9 x the subject's maximum", async () => {
    const view = await renderPage(<ProgrammesPage />);
    await view.user.click(screen.getByRole("button", { name: t.actions.new }));
    const save = screen.getByRole("button", { name: t.editor.save });
    expect(screen.getByText("training profile refused: bad_profile_id, empty_name")).toBeTruthy();
    expect((save as HTMLButtonElement).disabled).toBe(true);

    field(t.fields.profileId, "jog_145_155_30_min");
    field(t.fields.name, "jog 145-155, 30 min");
    field(t.fields.zoneLowBpm, "145");
    field(t.fields.zoneHighBpm, "155");
    field(t.fields.hardMaxBpm, "165");
    field(t.fields.criticalBpm, "175");
    field(t.fields.subjectHrMax, "170");
    expect(
      screen.getByText("training profile refused: zone_above_subject_ceiling"),
    ).toBeTruthy();
    expect((save as HTMLButtonElement).disabled).toBe(true);

    field(t.fields.subjectHrMax, "175");
    expect(screen.getByText(t.editor.accepted)).toBeTruthy();
    await view.user.click(screen.getByRole("checkbox", { name: "EDA" }));
    await view.user.click(screen.getByRole("checkbox", { name: "EDA" }));
    await view.user.click(screen.getByRole("checkbox", { name: t.fields.allowAboveNameplate }));
    await view.user.click(save);
    expect(mutationsSent()).toEqual([
      {
        name: "programmes:create",
        args: { ...PROFILE, allowAboveNameplate: true },
      },
    ]);
    expect(feedbackShown()).toEqual([{ kind: "success", message: t.saved }]);
    expect(screen.queryByRole("button", { name: t.editor.save })).toBeNull();
  });

  it("refuses an empty or unreadable number and shows no preview for it", async () => {
    const view = await renderPage(<ProgrammesPage />);
    await view.user.click(rowOf("jog_145_155_30_min_draft").getByRole("button", { name: t.actions.edit }));
    field(t.fields.totalDurationS, "");
    field(t.fields.maxRpm, "27,5");
    expect(
      screen.getByText(
        t.editor.invalid.replace(
          "{fields}",
          `${t.fields.totalDurationS}, ${t.fields.maxRpm}`,
        ),
      ),
    ).toBeTruthy();
    expect(screen.getByLabelText(t.fields.maxRpm).getAttribute("aria-invalid")).toBe("true");
    expect((screen.getByRole("button", { name: t.editor.save }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(t.preview.unusable)).toBeTruthy();
    await view.user.click(screen.getByRole("button", { name: t.editor.cancel }));
    expect(screen.queryByText(t.preview.unusable)).toBeNull();
  });

  it.each([
    ["0x91", false],
    ["1e3", false],
    [" 12 ", false],
    ["-12", false],
    ["1,,8", false],
    ["1,800", true],
    ["1.800", true],
    ["12", true],
  ])("reads « %s » in a duration as a number: %s", async (typed, read) => {
    const view = await renderPage(<ProgrammesPage />);
    await view.user.click(rowOf("jog_145_155_30_min_draft").getByRole("button", { name: t.actions.edit }));
    field(t.fields.baselineS, typed);
    const message = t.editor.invalid.replace("{fields}", t.fields.baselineS);
    expect(screen.queryByText(message) === null).toBe(read);
  });

  it("takes « 1,800 » in a duration as 1.8 s and refuses it in a whole field", async () => {
    const view = await renderPage(<ProgrammesPage />);
    await view.user.click(rowOf("jog_145_155_30_min_draft").getByRole("button", { name: t.actions.edit }));
    field(t.fields.baselineS, "1,800");
    expect(screen.getByText(/Mesure de référence 0:01/)).toBeTruthy();
    field(t.fields.zoneHighBpm, "1,800");
    expect(
      screen.getByText(t.editor.invalid.replace("{fields}", t.fields.zoneHighBpm)),
    ).toBeTruthy();
  });

  it("opens an empty form each time a new programme is asked for", async () => {
    const view = await renderPage(<ProgrammesPage />);
    await view.user.click(screen.getByRole("button", { name: t.actions.new }));
    field(t.fields.profileId, "jog_left_over");
    await view.user.click(screen.getByRole("button", { name: t.actions.new }));
    expect((screen.getByLabelText(t.fields.profileId) as HTMLInputElement).value).toBe("");
  });

  it("rewrites a draft and copies a programme into a new draft", async () => {
    const view = await renderPage(<ProgrammesPage />);
    await view.user.click(rowOf("jog_145_155_30_min_draft").getByRole("button", { name: t.actions.edit }));
    expect(screen.getByText(t.editor.evidenceCleared)).toBeTruthy();
    field(t.fields.totalDurationS, "1860");
    await view.user.click(screen.getByRole("button", { name: t.editor.save }));
    expect(mutationsSent()).toEqual([
      {
        name: "programmes:updateDraft",
        args: {
          programmeId: "p-draft",
          ...PROFILE,
          profileId: "jog_145_155_30_min_draft",
          totalDurationS: 1860,
        },
      },
    ]);

    await view.user.click(rowOf("jog_145_155_30_min_published").getByRole("button", { name: t.actions.duplicate }));
    expect((screen.getByLabelText(t.fields.profileId) as HTMLInputElement).value).toBe(
      "jog_145_155_30_min_published_copie",
    );
    expect((screen.getByLabelText(t.fields.name) as HTMLInputElement).value).toBe(
      "jog 145-155, 30 min (copie)",
    );
  });

  it("attaches a simulation report to a draft", async () => {
    mutation(api.machineRegistry.generateUploadUrl).mockResolvedValue(
      "https://storage.example.invalid/upload",
    );
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ storageId: "storage-report" }),
    });
    vi.stubGlobal("fetch", fetch);
    const view = await renderPage(<ProgrammesPage />);
    const file = new File(["{}"], "report.json", { type: "application/json" });
    await view.user.upload(screen.getByLabelText(t.actions.attach), file);
    expect(fetch).toHaveBeenCalledWith("https://storage.example.invalid/upload", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: file,
    });
    expect(mutationsSent()).toEqual([
      { name: "machineRegistry:generateUploadUrl", args: {} },
      {
        name: "programmes:attachSimulationReport",
        args: { programmeId: "p-draft", storageId: "storage-report" },
      },
    ]);
    expect(feedbackShown()).toEqual([{ kind: "success", message: t.attached }]);
  });

  it("validates with the medical sign-off, publishes and retires", async () => {
    answer(api.programmes.list, [
      row("draft", "p-draft", { simulationReportUrl: "https://storage.example.invalid/report" }),
      row("validated", "p-validated", {
        medicalSignoff: { by: "Dr Synthetic", at: Date.UTC(2026, 9, 1), note: "" },
      }),
      row("published", "p-published"),
    ]);
    const view = await renderPage(<ProgrammesPage />);
    expect(
      rowOf("jog_145_155_30_min_draft").getByRole("link", { name: t.actions.report }).getAttribute("href"),
    ).toBe("https://storage.example.invalid/report");

    await view.user.click(rowOf("jog_145_155_30_min_draft").getByRole("button", { name: t.actions.validate }));
    const dialog = within(screen.getByRole("dialog"));
    fireEvent.change(dialog.getByLabelText(t.validation.by), { target: { value: "Dr Synthetic" } });
    fireEvent.change(dialog.getByLabelText(t.validation.at), { target: { value: "2026-10-01" } });
    fireEvent.change(dialog.getByLabelText(t.validation.note), { target: { value: "avis favorable" } });
    await act(async () =>
      fireEvent.click(dialog.getByRole("button", { name: t.validation.submit })),
    );
    expect(screen.queryByRole("dialog")).toBeNull();

    await view.user.click(rowOf("jog_145_155_30_min_validated").getByRole("button", { name: t.actions.publish }));
    await view.user.click(rowOf("jog_145_155_30_min_published").getByRole("button", { name: t.actions.retire }));
    expect(mutationsSent()).toEqual([
      {
        name: "programmes:validate",
        args: {
          programmeId: "p-draft",
          medicalSignoff: { by: "Dr Synthetic", at: Date.UTC(2026, 9, 1), note: "avis favorable" },
        },
      },
      { name: "programmes:publish", args: { programmeId: "p-validated" } },
      { name: "programmes:retire", args: { programmeId: "p-published" } },
    ]);
    expect(feedbackShown().map((toast) => toast.message)).toEqual([
      t.validated,
      t.published,
      t.retired,
    ]);

    await view.user.click(rowOf("jog_145_155_30_min_validated").getByRole("button", { name: t.actions.view }));
    expect(screen.getByText(/signature médicale de Dr Synthetic le 2026-10-01/)).toBeTruthy();
  });
});

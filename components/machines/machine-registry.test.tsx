// @vitest-environment jsdom
import { act, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConvexError } from "convex/values";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import fr from "@/messages/fr.json";
import en from "@/messages/en.json";
import {
  answer,
  feedbackShown,
  lastArgsAsked,
  mutation,
  mutationsSent,
  NOW,
  renderPage,
  takeConsoleErrors,
  takeLoggedFailures,
} from "@/test-support/pages";
import {
  MachineRegistryCard,
  type MachineRegistry,
} from "./MachineRegistryCard";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const machineId = "TEST-machine" as Id<"machines">;
const contactId = "TEST-contact" as Id<"users">;
const adminId = "TEST-admin" as Id<"users">;
const author = { userId: adminId, firstName: "TEST", lastName: "Audit" };
const storageId = "TEST-storage" as Id<"_storage">;
const t = fr.machineRegistry;

function registry(overrides: Partial<MachineRegistry> = {}): MachineRegistry {
  return {
    machineId,
    organizationId: "TEST-org" as Id<"organizations">,
    contacts: [{ userId: contactId, firstName: "TEST", lastName: "Contact" }],
    serialPi: "TEST-PI-0001",
    serialDrive: "TEST-DRIVE-0001",
    serialMotor: "TEST-MOTOR-0001",
    serialGearbox: "TEST-GEARBOX-0001",
    bitalinoAddress: "00:11:22:33:44:55",
    site: "Adresse de test",
    commissionedAt: NOW - 86_400_000,
    expectedSoftwareVersion: "pi-0.1.0",
    armRadiusM: 1.234,
    legTipRadiusM: 2.345,
    radiiMeasuredAt: NOW,
    radiiMeasuredBy: "TEST mesureur",
    driveParameterSheet: null,
    driveParameterSheetHistory: [],
    validation: { level: "bench", reviews: [], downgrades: [] },
    effectiveLevel: overrides.validation?.level ?? "bench",
    updatedAt: NOW,
    updatedBy: author,
    reportedSoftwareVersion: "pi-0.1.0",
    versionStatus: "up_to_date",
    expectedVersionAllowed: true,
    configurationStatus: "unavailable",
    configurationLocks: {
      PROGRAMS_ENABLED: {
        allowed: false,
        requiredLevel: "auto",
        reachedLevel: "bench",
        message: "machine validée jusqu'au niveau bench",
      },
      OCCUPANCY_OCCUPIED_ENABLED: {
        allowed: false,
        requiredLevel: "occupied",
        reachedLevel: "bench",
        message: "machine validée jusqu'au niveau bench",
      },
    },
    configurationProposal: { ARM_RADIUS_M: 1.234, LEG_TIP_RADIUS_M: 2.345 },
    alerts: [],
    medicalKeyIsTest: true,
    canEdit: true,
    ...overrides,
  };
}

async function card(
  value: MachineRegistry | null | undefined = registry(),
  role = "admin",
  locale: "fr" | "en" = "fr",
) {
  answer(api.machineRegistry.getRegistry, value);
  answer(api.machineRegistry.listContactCandidates, [
    {
      userId: contactId,
      firstName: "TEST",
      lastName: "Contact",
      role: "gestionnaire",
    },
  ]);
  return renderPage(<MachineRegistryCard machineId={machineId} role={role} />, {
    locale,
  });
}

function form(name: string) {
  return within(screen.getByRole("form", { name }));
}
function fill(scope: ReturnType<typeof form>, label: string, value: string) {
  fireEvent.change(scope.getByLabelText(label), { target: { value } });
}
function uploadAnswer() {
  mutation(api.machineRegistry.generateUploadUrl).mockResolvedValue(
    "https://storage.example.invalid/upload",
  );
  const fetch = vi
    .fn()
    .mockResolvedValue({ ok: true, json: async () => ({ storageId }) });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}
function sheetFields() {
  const scope = form(t.attachSheet);
  fill(scope, t.sheetVersion, "TEST-v2");
  fill(scope, t.signedBy, "TEST signataire");
  fill(scope, t.sheetAt, "2026-10-15");
  return scope;
}

describe("machine registry visibility and reading", () => {
  it("shows loading, asks for this machine and draws nothing for null", async () => {
    const view = await card();
    answer(api.machineRegistry.getRegistry, undefined);
    await view.refresh();
    expect(screen.getByLabelText(t.loading)).toBeTruthy();
    expect(lastArgsAsked(api.machineRegistry.getRegistry)).toEqual({
      machineId,
    });
    answer(api.machineRegistry.getRegistry, null);
    await view.refresh();
    expect(screen.queryByRole("region")).toBeNull();
  });

  it.each(["gestionnaire", "user", "unknown"])(
    "draws nothing and skips the query for %s",
    async (role) => {
      await card(registry(), role);
      expect(screen.queryByRole("region")).toBeNull();
      expect(lastArgsAsked(api.machineRegistry.getRegistry)).toBe("skip");
    },
  );

  it.each(["org_admin", "admin"])(
    "keeps %s read-only when the server refuses edits",
    async (role) => {
      await card(registry({ canEdit: false }), role);
      for (const title of [t.identity, t.measurements, t.drive, t.validation])
        expect(screen.getByRole("heading", { name: title })).toBeTruthy();
      expect(screen.getByText("TEST-PI-0001")).toBeTruthy();
      expect(screen.queryByRole("form")).toBeNull();
      expect(screen.queryByRole("button")).toBeNull();
      expect(
        lastArgsAsked(api.machineRegistry.listContactCandidates),
      ).toBeUndefined();
    },
  );

  it("also keeps an organisation admin read-only if canEdit is true", async () => {
    await card(registry(), "org_admin");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("keeps deleted machine records readable without querying editable contacts", async () => {
    answer(api.machineRegistry.getRegistry, registry());
    await renderPage(
      <MachineRegistryCard machineId={machineId} role="admin" readOnly />,
    );
    expect(screen.getByText("TEST-PI-0001")).toBeTruthy();
    expect(screen.queryByRole("form")).toBeNull();
    expect(
      lastArgsAsked(api.machineRegistry.listContactCandidates),
    ).toBeUndefined();
  });

  it("renders English, missing identity and geometry without inventing values", async () => {
    await card(
      registry({
        serialPi: undefined,
        contacts: [],
        commissionedAt: undefined,
        radiiMeasuredAt: undefined,
        radiiMeasuredBy: undefined,
        armRadiusM: undefined,
        legTipRadiusM: undefined,
        reportedSoftwareVersion: null,
        expectedSoftwareVersion: undefined,
        configurationProposal: { ARM_RADIUS_M: null, LEG_TIP_RADIUS_M: null },
        validation: { level: "none", reviews: [], downgrades: [] },
      }),
      "org_admin",
      "en",
    );
    expect(
      screen.getByRole("heading", { name: en.machineRegistry.identity }),
    ).toBeTruthy();
    expect(
      screen.getAllByText(en.machineRegistry.missing).length,
    ).toBeGreaterThan(5);
    expect(screen.getByText(en.machineRegistry.locksPending)).toBeTruthy();
  });

  it.each(["unknown", "up_to_date", "outdated", "not_validated"] as const)(
    "shows the %s version status and the expected-version warning",
    async (versionStatus) => {
      await card(
        registry({ versionStatus, expectedVersionAllowed: false }),
        "org_admin",
      );
      expect(screen.getByText(t.versions[versionStatus])).toBeTruthy();
      expect(
        screen.getByText(t.expectedNotValidated).closest('[role="alert"]'),
      ).toBeTruthy();
      expect(screen.getByText(t.locksPending)).toBeTruthy();
      expect(screen.getByText(t.otaPending)).toBeTruthy();
    },
  );

  it("shows server lock verdicts, both alerts, their count and most recent date", async () => {
    const value = registry();
    value.configurationLocks.PROGRAMS_ENABLED = {
      allowed: true,
      requiredLevel: "auto",
      reachedLevel: "auto",
      message: null,
    };
    value.alerts = [
      { code: "programs_enabled_without_auto", count: 1, mostRecentAt: null },
      { code: "person_on_board_without_occupied", count: 2, mostRecentAt: NOW },
    ];
    await card(value, "org_admin");
    expect(screen.getByText(t.allowed)).toBeTruthy();
    expect(screen.getByText(t.denied)).toBeTruthy();
    expect(
      screen.getByText(t.lockReason.replace("{level}", t.levels.bench)),
    ).toBeTruthy();
    expect(
      screen
        .getByText(t.alerts.programs_enabled_without_auto)
        .closest('[role="alert"]'),
    ).toBeTruthy();
    expect(
      screen
        .getByText("2 séances avec une personne à bord sous le niveau requis.")
        .closest('[role="alert"]'),
    ).toBeTruthy();
    expect(screen.getByText(/Séance la plus récente/)).toBeTruthy();
  });

  it("links the current sheet, historical sheets and review reports and retains downgrade details", async () => {
    const sheet = {
      storageId,
      version: "TEST-v2",
      contentType: "application/pdf" as const,
      signedBy: "TEST signataire",
      at: NOW,
      attachedBy: author,
      attachedAt: NOW,
      url: "https://storage.example.invalid/current",
    };
    await card(
      registry({
        driveParameterSheet: sheet,
        driveParameterSheetHistory: [
          {
            ...sheet,
            storageId: "TEST-old" as Id<"_storage">,
            version: "TEST-v1",
            url: "https://storage.example.invalid/old",
          },
          { ...sheet, storageId: "TEST-missing" as Id<"_storage">, url: null },
        ],
        validation: {
          level: "bench",
          reviews: [
            {
              level: "bench",
              by: author,
              at: NOW,
              reportStorageId: storageId,
              reportUrl: "https://storage.example.invalid/report",
              softwareVersion: "pi-0.1.0",
              medicalParametersVersion: null,
            },
            {
              level: "auto",
              by: { userId: adminId },
              at: NOW + 1,
              reportStorageId: "TEST-report-missing" as Id<"_storage">,
              reportUrl: null,
              softwareVersion: "pi-0.2.0",
              medicalParametersVersion: "0.1.0",
            },
          ],
          downgrades: [
            {
              from: "auto",
              to: "bench",
              by: author,
              at: NOW + 2,
              reason: "TEST nouvel essai requis",
            },
          ],
        },
      }),
      "org_admin",
    );
    expect(
      screen.getByRole("link", { name: "TEST-v2" }).getAttribute("href"),
    ).toBe(sheet.url);
    expect(
      screen.getByRole("link", { name: "TEST-v1" }).getAttribute("href"),
    ).toBe("https://storage.example.invalid/old");
    expect(
      screen.getByRole("link", { name: t.downloadReport }).getAttribute("href"),
    ).toBe("https://storage.example.invalid/report");
    expect(screen.getAllByText(t.fileUnavailable)).toHaveLength(2);
    expect(screen.getByText("TEST nouvel essai requis")).toBeTruthy();
    expect(screen.getAllByText(/TEST-admin/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Par TEST Audit/)).toHaveLength(2);
    expect(screen.getAllByText(/Rattachée par TEST Audit/)).toHaveLength(3);
    expect(
      screen.getByText(/Dernière modification par TEST Audit/),
    ).toBeTruthy();
  });

  it("warns when stored and effective levels differ and proposes the next effective review", async () => {
    await card(
      registry({
        validation: { level: "auto", reviews: [], downgrades: [] },
        effectiveLevel: "none",
      }),
    );
    expect(
      screen
        .getByText(
          t.levelMismatch
            .replace("{stored}", t.levels.auto)
            .replace("{effective}", t.levels.none),
        )
        .closest('[role="alert"]'),
    ).toBeTruthy();
    expect(
      screen.getByText(t.nextLevel.replace("{level}", t.levels.bench)),
    ).toBeTruthy();
    expect(screen.queryByRole("form", { name: t.lowerLevel })).toBeNull();
  });

  it("shows the UTC commissioning day even in a negative timezone", async () => {
    vi.stubEnv("TZ", "America/Los_Angeles");
    await card(
      registry({ commissionedAt: Date.UTC(2026, 9, 15) }),
      "org_admin",
    );
    expect(screen.getByText("15 oct. 2026")).toBeTruthy();
    expect(screen.queryByText("14 oct. 2026")).toBeNull();
  });
});

describe("machine registry editing", () => {
  it.each(["identity", "radii"] as const)(
    "preserves another section's draft when saving %s",
    async (section) => {
      const value = registry();
      const view = await card(value);
      fill(form(t.saveIdentity), t.serialPi, "TEST unsaved identity");
      fill(form(t.saveRadii), t.measuredBy, "TEST unsaved measurer");
      const title = section === "identity" ? t.saveIdentity : t.saveRadii;
      await view.user.click(form(title).getByRole("button", { name: title }));
      answer(api.machineRegistry.getRegistry, {
        ...value,
        updatedAt: NOW + 1,
        ...(section === "identity"
          ? { serialPi: "TEST unsaved identity" }
          : { radiiMeasuredBy: "TEST unsaved measurer" }),
      });
      await view.refresh();
      const draft =
        section === "identity"
          ? form(t.saveRadii).getByLabelText(t.measuredBy)
          : form(t.saveIdentity).getByLabelText(t.serialPi);
      expect(draft).toHaveProperty(
        "value",
        section === "identity"
          ? "TEST unsaved measurer"
          : "TEST unsaved identity",
      );
      expect(mutationsSent().map((entry) => entry.name)).toContain(
        section === "identity"
          ? "machineRegistry:updateIdentity"
          : "machineRegistry:recordRadii",
      );
    },
  );

  it("saves all identity fields, only organisation contact ids and a UTC commissioning date", async () => {
    const view = await card();
    const scope = form(t.saveIdentity);
    expect(lastArgsAsked(api.machineRegistry.listContactCandidates)).toEqual({
      machineId,
    });
    fill(scope, t.serialPi, " TEST-PI-0002 ");
    fill(scope, t.commissionedAt, "2026-10-15");
    await view.user.click(scope.getByRole("button", { name: t.saveIdentity }));
    expect(mutation(api.machineRegistry.updateIdentity)).toHaveBeenCalledWith({
      machineId,
      serialPi: "TEST-PI-0002",
      serialDrive: "TEST-DRIVE-0001",
      serialMotor: "TEST-MOTOR-0001",
      serialGearbox: "TEST-GEARBOX-0001",
      bitalinoAddress: "00:11:22:33:44:55",
      site: "Adresse de test",
      expectedSoftwareVersion: "pi-0.1.0",
      commissionedAt: Date.parse("2026-10-15"),
      contacts: [contactId],
    });
    expect(feedbackShown()).toContainEqual({
      kind: "success",
      message: t.identitySaved,
    });
  });

  it("clears empty fields with null, removes contacts and displays the absence of candidates", async () => {
    const view = await card();
    const scope = form(t.saveIdentity);
    for (const label of [
      t.serialPi,
      t.serialDrive,
      t.serialMotor,
      t.serialGearbox,
      t.bitalinoAddress,
      t.site,
      t.expectedSoftwareVersion,
      t.commissionedAt,
    ])
      fill(scope, label, "");
    await view.user.click(scope.getByRole("checkbox"));
    await view.user.click(scope.getByRole("button"));
    expect(mutation(api.machineRegistry.updateIdentity)).toHaveBeenCalledWith({
      machineId,
      serialPi: null,
      serialDrive: null,
      serialMotor: null,
      serialGearbox: null,
      bitalinoAddress: null,
      site: null,
      expectedSoftwareVersion: null,
      commissionedAt: null,
      contacts: [],
    });
    answer(api.machineRegistry.listContactCandidates, []);
    await view.refresh();
    expect(screen.getByText(t.noContacts)).toBeTruthy();
  });

  it("disables identity saving until contacts have loaded", async () => {
    const view = await card();
    answer(api.machineRegistry.listContactCandidates, undefined);
    await view.refresh();
    expect(
      (form(t.saveIdentity).getByRole("button") as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("saves measured radii with date and author and displays the central configuration proposal", async () => {
    const view = await card();
    const scope = form(t.saveRadii);
    fill(scope, t.armRadiusM, "1.456");
    fill(scope, t.legTipRadiusM, "2.567");
    fill(scope, t.measuredAt, "2026-10-14");
    fill(scope, t.measuredBy, "TEST auteur");
    await view.user.click(scope.getByRole("button"));
    expect(mutation(api.machineRegistry.recordRadii)).toHaveBeenCalledWith({
      machineId,
      armRadiusM: 1.456,
      legTipRadiusM: 2.567,
      measuredAt: Date.parse("2026-10-14"),
      measuredBy: "TEST auteur",
    });
    expect(screen.getByText(t.proposalPending)).toBeTruthy();
    expect(screen.getByText("ARM_RADIUS_M")).toBeTruthy();
  });

  it.each(["application/pdf", "application/json"])(
    "uploads a %s sheet before attaching its storage id and signature",
    async (type) => {
      const fetch = uploadAnswer();
      const view = await card();
      const scope = sheetFields();
      const file = new File(
        ["TEST parameter sheet"],
        type === "application/pdf" ? "TEST.pdf" : "TEST.json",
        { type },
      );
      await view.user.upload(scope.getByLabelText(t.sheetFile), file);
      expect(
        (scope.getByLabelText(t.sheetFile) as HTMLInputElement).files?.[0],
      ).toBe(file);
      expect(
        (scope.getByLabelText(t.sheetFile) as HTMLInputElement).required,
      ).toBe(true);
      await act(async () =>
        fireEvent.submit(screen.getByRole("form", { name: t.attachSheet })),
      );
      expect(fetch).toHaveBeenCalledWith(
        "https://storage.example.invalid/upload",
        { method: "POST", headers: { "Content-Type": type }, body: file },
      );
      expect(mutationsSent()).toEqual([
        { name: "machineRegistry:generateUploadUrl", args: {} },
        {
          name: "machineRegistry:attachDriveParameterSheet",
          args: {
            machineId,
            storageId,
            version: "TEST-v2",
            signedBy: "TEST signataire",
            at: Date.parse("2026-10-15"),
          },
        },
      ]);
      expect(feedbackShown()).toContainEqual({
        kind: "success",
        message: t.sheetSaved,
      });
    },
  );

  it.each(["none", "bench", "auto", "occupied"] as const)(
    "offers only the next review at %s and only lower downgrade targets",
    async (level) => {
      const view = await card(
        registry({
          validation: { level, reviews: [], downgrades: [] },
          medicalKeyIsTest: false,
        }),
      );
      const levels = ["none", "bench", "auto", "occupied"] as const;
      const rank = levels.indexOf(level);
      const next = levels[rank + 1];
      if (next === undefined)
        expect(screen.queryByRole("form", { name: t.addReview })).toBeNull();
      else
        expect(
          form(t.addReview).getByText(
            t.nextLevel.replace("{level}", t.levels[next]),
          ),
        ).toBeTruthy();
      if (rank === 0)
        expect(screen.queryByRole("form", { name: t.lowerLevel })).toBeNull();
      else {
        await view.user.click(form(t.lowerLevel).getByRole("combobox"));
        expect(
          screen.getAllByRole("option").map((option) => option.textContent),
        ).toEqual(levels.slice(0, rank).map((item) => t.levels[item]));
      }
    },
  );

  it.each([null, "0.1.0"])(
    "adds a review with its report, next level and medical version %s",
    async (medicalParametersVersion) => {
      uploadAnswer();
      const view = await card();
      const scope = form(t.addReview);
      await view.user.upload(
        scope.getByLabelText(t.reportFile),
        new File(["TEST report"], "TEST-report.pdf", {
          type: "application/pdf",
        }),
      );
      fill(scope, t.softwareVersion, "pi-0.2.0");
      if (medicalParametersVersion)
        fill(scope, t.medicalVersion, medicalParametersVersion);
      expect(
        (scope.getByLabelText(t.reportFile) as HTMLInputElement).required,
      ).toBe(true);
      await act(async () =>
        fireEvent.submit(screen.getByRole("form", { name: t.addReview })),
      );
      expect(
        mutation(api.machineRegistry.addValidationReview),
      ).toHaveBeenCalledWith({
        machineId,
        level: "auto",
        reportStorageId: storageId,
        softwareVersion: "pi-0.2.0",
        medicalParametersVersion,
      });
      expect(feedbackShown()).toContainEqual({
        kind: "success",
        message: t.reviewSaved,
      });
    },
  );

  it("blocks occupied with the test medical key and explains ANH-98", async () => {
    await card(
      registry({ validation: { level: "auto", reviews: [], downgrades: [] } }),
    );
    expect(
      (form(t.addReview).getByRole("button") as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(screen.getByText(t.medicalKeyIsTest)).toBeTruthy();
    expect(
      (form(t.addReview).getByLabelText(t.medicalVersion) as HTMLInputElement)
        .required,
    ).toBe(true);
  });

  it("requires a meaningful reason before lowering and sends only a lower target", async () => {
    const view = await card(
      registry({ validation: { level: "auto", reviews: [], downgrades: [] } }),
    );
    const scope = form(t.lowerLevel);
    expect((scope.getByRole("button") as HTMLButtonElement).disabled).toBe(
      true,
    );
    fill(scope, t.reason, "   ");
    await view.user.click(scope.getByRole("button"));
    expect(mutationsSent()).toEqual([]);
    fill(scope, t.reason, " TEST contrôle requis ");
    await view.user.click(scope.getByRole("combobox"));
    await view.user.click(screen.getByRole("option", { name: t.levels.bench }));
    await view.user.click(scope.getByRole("button"));
    expect(
      mutation(api.machineRegistry.lowerValidationLevel),
    ).toHaveBeenCalledWith({
      machineId,
      level: "bench",
      reason: "TEST contrôle requis",
    });
    expect(feedbackShown()).toContainEqual({
      kind: "success",
      message: t.levelLowered,
    });
  });

  it("translates a server refusal and keeps the form available", async () => {
    mutation(api.machineRegistry.updateIdentity).mockRejectedValue(
      new ConvexError("invalid_registry_field"),
    );
    const view = await card();
    await view.user.click(form(t.saveIdentity).getByRole("button"));
    expect(feedbackShown()).toContainEqual({
      kind: "error",
      message: fr.errors.invalid_registry_field,
    });
    expect(takeLoggedFailures()).toEqual(["machineRegistry:updateIdentity"]);
    expect(
      (form(t.saveIdentity).getByRole("button") as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it.each([
    "missing",
    "type",
    "size",
    "http",
    "network",
    "json",
    "shape",
    "url",
  ])(
    "stops attachment and shows a translated error for upload failure: %s",
    async (failure) => {
      const fetch = uploadAnswer();
      await card();
      const scope = sheetFields();
      const file = new File(
        [
          failure === "size"
            ? new Uint8Array(10 * 1024 * 1024 + 1)
            : "TEST file",
        ],
        "TEST.pdf",
        { type: failure === "type" ? "text/plain" : "application/pdf" },
      );
      if (failure !== "missing")
        fireEvent.change(scope.getByLabelText(t.sheetFile), {
          target: { files: [file] },
        });
      if (failure === "http") fetch.mockResolvedValue({ ok: false });
      if (failure === "network")
        fetch.mockRejectedValue(new Error("TEST offline"));
      if (failure === "json")
        fetch.mockResolvedValue({
          ok: true,
          json: async () => {
            throw new Error("TEST invalid JSON");
          },
        });
      if (failure === "shape")
        fetch.mockResolvedValue({
          ok: true,
          json: async () => ({ storageId: 42 }),
        });
      if (failure === "url")
        mutation(api.machineRegistry.generateUploadUrl).mockRejectedValue(
          new ConvexError("not_authorized"),
        );
      await act(async () =>
        fireEvent.submit(screen.getByRole("form", { name: t.attachSheet })),
      );
      expect(
        mutation(api.machineRegistry.attachDriveParameterSheet),
      ).not.toHaveBeenCalled();
      expect(feedbackShown()).toContainEqual({
        kind: "error",
        message:
          fr.errors[
            failure === "url" ? "not_authorized" : "invalid_registry_file"
          ],
      });
      if (failure === "url")
        expect(takeLoggedFailures()).toEqual([
          "machineRegistry:generateUploadUrl",
        ]);
      else expect(takeConsoleErrors()).toHaveLength(1);
    },
  );
});

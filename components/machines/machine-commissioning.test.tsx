// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import fr from "@/messages/fr.json";
import en from "@/messages/en.json";
import { answer, lastArgsAsked, NOW, renderPage, routeParams } from "@/test-support/pages";
import CommissioningPrintPage from "@/app/[locale]/dashboard/machines/[id]/commissioning/[recordId]/page";
import {
  commissioningFileName,
  commissioningJson,
  type CommissioningRecord,
} from "./commissioningJson";
import { MachineCommissioningCard } from "./MachineCommissioningCard";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);
const download = vi.hoisted(() => ({ save: vi.fn() }));
vi.mock("@/lib/download", () => ({ save: download.save }));

/**
 * ANH-115 EX-5: a machine's commissionings on its page, and the record to
 * print or export. The server decides who reads them
 * (convex/machineCommissioning.ts); the page shows what it is given.
 */
const machineId = "machine-1" as Id<"machines">;
const recordId = "record-1" as Id<"machine_commissioning">;
const t = fr.machineCommissioning;
const SHEET = { tfrHz: 60, hspHz: 50, lspHz: 0, accS: 3, decS: 3 };

function record(overrides: Partial<CommissioningRecord> = {}): CommissioningRecord {
  return {
    recordId,
    machineId,
    recordedAt: Date.UTC(2026, 10, 15, 9),
    verdict: "nonconforming",
    findings: [
      { id: "sto_bridged", expected: "drive_input", read: "bridged_declared" },
      { id: "control:estop", expected: "ok", read: "arret d'urgence non atteste" },
      { id: "drive_hsp", expected: "50.0 Hz", read: "50.1 Hz" },
      { id: "unknown_one", expected: "a", read: "b" },
    ],
    operator: { userId: "user-1" as Id<"users">, firstName: "Alice", lastName: "Martin" },
    attestedBy: { userId: "user-2" as Id<"users"> },
    reference: {
      sheet: { version: "v3", signedBy: "TEST signataire", at: NOW, parameters: SHEET },
      bitalinoAddress: null,
      expectedSoftwareVersion: "pi-0.4.0",
    },
    report: {
      ranAt: NOW - 60_000,
      statement: "a latching mushroom emergency stop is wired normally-closed into P24 -> STO",
      attestedAt: NOW - 600_000,
      stoSource: "bridged_declared",
      controls: [
        { id: "drive_link", state: "ok", detail: "variateur lu il y a 0.4 s" },
        { id: "estop", state: "block", detail: "arret d'urgence non atteste" },
        { id: "odd", state: "pending", detail: "TEST" },
      ],
      drive: { ...SHEET, hspHz: 50.1 },
      bitalinoAddress: "20:16:07:18:15:52",
      softwareVersion: "pi-0.4.0",
      medicalParametersVersion: null,
      configHash: null,
    },
    payload: '{"kind":"commissioning_record"}',
    signature: null,
    ...overrides,
  };
}

describe("ANH-115 the machine's commissionings", () => {
  it("lists each record, its verdict, its findings and its page", async () => {
    answer(api.machineCommissioning.listCommissionings, [
      { recordId, recordedAt: NOW, verdict: "nonconforming", findings: 2, signed: false },
      { recordId: "record-0" as Id<"machine_commissioning">, recordedAt: NOW - 1, verdict: "conforming", findings: 0, signed: true },
    ]);
    await renderPage(<MachineCommissioningCard machineId={machineId} />);
    expect(lastArgsAsked(api.machineCommissioning.listCommissionings)).toEqual({ machineId });
    expect(screen.getByText(t.description)).toBeTruthy();
    expect(screen.getByText(t.verdicts.nonconforming)).toBeTruthy();
    expect(screen.getByText(t.verdicts.conforming)).toBeTruthy();
    expect(screen.getByText("2 écarts")).toBeTruthy();
    expect(screen.getAllByText(t.unsigned)).toHaveLength(1);
    const links = screen.getAllByRole("link", { name: t.open });
    expect(links[0].getAttribute("href")).toBe(
      `/dashboard/machines/${machineId}/commissioning/${recordId}`,
    );
  });

  it("waits, then says when there is none", async () => {
    answer(api.machineCommissioning.listCommissionings, undefined);
    const view = await renderPage(<MachineCommissioningCard machineId={machineId} />);
    expect(screen.queryByText(t.none)).toBeNull();
    answer(api.machineCommissioning.listCommissionings, []);
    await view.refresh();
    expect(screen.getByText(t.none)).toBeTruthy();
  });
});

describe("ANH-115 the commissioning record, to print and export", () => {
  const page = () =>
    renderPage(<CommissioningPrintPage params={routeParams({ id: machineId, recordId })} />);

  it("shows the findings, the people, the bridged STO, the drive and the controls", async () => {
    answer(api.machineCommissioning.getCommissioning, record());
    await page();
    expect(lastArgsAsked(api.machineCommissioning.getCommissioning)).toEqual({ recordId });
    expect(screen.getByText(t.printTitle)).toBeTruthy();
    expect(screen.getByText(t.verdicts.nonconforming)).toBeTruthy();
    expect(screen.getByText("STO ponté : attendu drive_input, lu bridged_declared")).toBeTruthy();
    expect(screen.getByText("Contrôle estop : attendu ok, lu arret d'urgence non atteste")).toBeTruthy();
    expect(screen.getByText("HSP : attendu 50.0 Hz, lu 50.1 Hz")).toBeTruthy();
    expect(screen.getByText("unknown_one : attendu a, lu b")).toBeTruthy();
    expect(screen.getByText("Lancée par Alice Martin")).toBeTruthy();
    expect(screen.getByText(/Arrêt d'urgence attesté par user-2/)).toBeTruthy();
    expect(screen.getByText(record().report.statement)).toBeTruthy();
    expect(screen.getByText(t.stoBridged)).toBeTruthy();
    expect(screen.getByText("50,1 Hz")).toBeTruthy();
    expect(screen.getByText(t.states.block)).toBeTruthy();
    expect(screen.getByText("pending")).toBeTruthy();
    expect(screen.getByText("BITalino : 20:16:07:18:15:52 (registre : non renseignée)")).toBeTruthy();
    expect(screen.getByText(t.notSigned)).toBeTruthy();
  });

  it("shows a conforming record without findings, its drive unread, its text signed, in English", async () => {
    answer(
      api.machineCommissioning.getCommissioning,
      record({
        verdict: "conforming",
        findings: [],
        signature: btoa("signature"),
        reference: { ...record().reference, bitalinoAddress: "20:16:07:18:15:52", expectedSoftwareVersion: null },
        report: {
          ...record().report,
          stoSource: "drive_input",
          drive: null,
          bitalinoAddress: null,
          medicalParametersVersion: "med-2026.1",
          configHash: "a".repeat(64),
        },
      }),
    );
    await renderPage(<CommissioningPrintPage params={routeParams({ id: machineId, recordId })} />, {
      locale: "en",
    });
    expect(screen.getByText(en.machineCommissioning.noFindings)).toBeTruthy();
    expect(screen.queryByText(en.machineCommissioning.stoBridged)).toBeNull();
    expect(screen.getAllByText(en.machineCommissioning.notRead)).toHaveLength(5);
    expect(screen.getByText(en.machineCommissioning.signed)).toBeTruthy();
    expect(screen.getByText("Software: pi-0.4.0 (expected: not set)")).toBeTruthy();
    expect(screen.getByText("Medical parameters: med-2026.1")).toBeTruthy();
    expect(screen.getByText(`Configuration: ${"a".repeat(64)}`)).toBeTruthy();
    expect(screen.getByText("BITalino: not set (registry: 20:16:07:18:15:52)")).toBeTruthy();
  });

  it("downloads the signed text as the server stored it", async () => {
    answer(api.machineCommissioning.getCommissioning, record({ signature: btoa("signature") }));
    await page();
    fireEvent.click(screen.getByRole("button", { name: t.download }));
    expect(download.save).toHaveBeenCalledTimes(1);
    const [blob, name] = download.save.mock.calls[0] as [Blob, string];
    expect(name).toBe("mise-en-service-machine-1-2026-11-15.json");
    expect(JSON.parse(await blob.text())).toEqual({
      payload: '{"kind":"commissioning_record"}',
      signature: btoa("signature"),
    });
  });

  it("prints from the browser", async () => {
    answer(api.machineCommissioning.getCommissioning, record());
    const print = vi.spyOn(window, "print").mockImplementation(() => undefined);
    await page();
    fireEvent.click(screen.getByRole("button", { name: t.printButton }));
    expect(print).toHaveBeenCalledTimes(1);
    print.mockRestore();
  });

  it("says when the record is not readable, or of another machine", async () => {
    answer(api.machineCommissioning.getCommissioning, undefined);
    const view = await page();
    expect(screen.queryByText(t.printTitle)).toBeNull();
    answer(api.machineCommissioning.getCommissioning, null);
    await view.refresh();
    expect(screen.getByText(t.notAllowed)).toBeTruthy();
    answer(
      api.machineCommissioning.getCommissioning,
      record({ machineId: "machine-2" as Id<"machines"> }),
    );
    await view.refresh();
    expect(screen.getByText(t.notAllowed)).toBeTruthy();
  });

  it("writes the export and its name without rewriting the text", () => {
    const text = commissioningJson({ payload: '{"a": 1}', signature: null });
    expect(text.endsWith("\n")).toBe(true);
    expect(JSON.parse(text)).toEqual({ payload: '{"a": 1}', signature: null });
    expect(commissioningFileName({ machineId, recordedAt: Date.UTC(2026, 0, 2, 23) })).toBe(
      "mise-en-service-machine-1-2026-01-02.json",
    );
  });
});

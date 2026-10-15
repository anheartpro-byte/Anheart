"use client";

import { useId, useState, type ComponentProps, type ReactNode } from "react";
import { useQuery } from "convex/react";
import { ConvexError } from "convex/values";
import { useTranslations } from "next-intl";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import {
  runWithFeedback,
  useMutationWithFeedback,
} from "@/hooks/use-mutation-with-feedback";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { MachineRegistry } from "./MachineRegistryCard";

const VALIDATION_LEVELS = ["none", "bench", "auto", "occupied"] as const;

function Field({
  label,
  ...props
}: ComponentProps<typeof Input> & { label: string }) {
  const id = useId();
  return (
    <div className="space-y-1">
      <Label htmlFor={id}>{label}</Label>
      <Input {...props} id={id} />
    </div>
  );
}

function RegistryForm({
  title,
  children,
  onSave,
  disabled = false,
  canSubmit = true,
}: {
  title: string;
  children: ReactNode;
  onSave: (data: FormData, form: HTMLFormElement) => Promise<void>;
  disabled?: boolean;
  canSubmit?: boolean;
}) {
  const t = useTranslations("machineRegistry");
  const [busy, setBusy] = useState(false);
  return (
    <form
      aria-label={title}
      className="border-t pt-4"
      onSubmit={async (event) => {
        event.preventDefault();
        if (busy || disabled || !canSubmit) return;
        const form = event.currentTarget;
        const data = new FormData(form);
        setBusy(true);
        try {
          await onSave(data, form);
        } finally {
          setBusy(false);
        }
      }}
    >
      <fieldset disabled={busy || disabled} className="space-y-3">
        <legend className="mb-3 font-medium">{title}</legend>
        {children}
        <Button
          type="submit"
          size="sm"
          disabled={busy || disabled || !canSubmit}
        >
          {busy ? t("saving") : title}
        </Button>
      </fieldset>
    </form>
  );
}

const text = (data: FormData, key: string) =>
  String(data.get(key) ?? "").trim();
const dateValue = (value: number | undefined) =>
  value === undefined ? "" : new Date(value).toISOString().slice(0, 10);
const day = (data: FormData, key: string) => Date.parse(text(data, key));

function useRegistryUpload() {
  const t = useTranslations();
  const generateUploadUrl = useMutationWithFeedback(
    api.machineRegistry.generateUploadUrl,
  );
  return async (file: File | undefined) => {
    if (
      file === undefined ||
      !["application/pdf", "application/json"].includes(file.type) ||
      file.size > 10 * 1024 * 1024
    ) {
      await runWithFeedback(
        "[registry upload]",
        async () => {
          throw new ConvexError({ code: "invalid_registry_file" });
        },
        undefined,
        t,
      );
      return null;
    }
    const url = await generateUploadUrl({});
    if (!url.ok) return null;
    const uploaded = await runWithFeedback(
      "[registry upload]",
      async () => {
        try {
          const response = await fetch(url.value, {
            method: "POST",
            headers: { "Content-Type": file.type },
            body: file,
          });
          if (!response.ok) throw new Error();
          const result: unknown = await response.json();
          if (
            typeof result !== "object" ||
            result === null ||
            !("storageId" in result) ||
            typeof result.storageId !== "string" ||
            result.storageId.length === 0
          )
            throw new Error();
          return result.storageId as Id<"_storage">;
        } catch {
          throw new ConvexError({ code: "invalid_registry_file" });
        }
      },
      undefined,
      t,
    );
    return uploaded.ok ? uploaded.value : null;
  };
}

export function IdentityForm({ registry }: { registry: MachineRegistry }) {
  const t = useTranslations("machineRegistry");
  const candidates = useQuery(api.machineRegistry.listContactCandidates, {
    machineId: registry.machineId,
  });
  const save = useMutationWithFeedback(api.machineRegistry.updateIdentity);
  return (
    <RegistryForm
      title={t("saveIdentity")}
      disabled={candidates === undefined}
      onSave={async (data) => {
        await save(
          {
            machineId: registry.machineId,
            serialPi: text(data, "serialPi") || null,
            serialDrive: text(data, "serialDrive") || null,
            serialMotor: text(data, "serialMotor") || null,
            serialGearbox: text(data, "serialGearbox") || null,
            bitalinoAddress: text(data, "bitalinoAddress") || null,
            site: text(data, "site") || null,
            commissionedAt: text(data, "commissionedAt")
              ? day(data, "commissionedAt")
              : null,
            expectedSoftwareVersion:
              text(data, "expectedSoftwareVersion") || null,
            contacts: (candidates ?? [])
              .filter((candidate) =>
                data.getAll("contacts").includes(candidate.userId),
              )
              .map((candidate) => candidate.userId),
          },
          { success: t("identitySaved") },
        );
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        {(
          [
            "serialPi",
            "serialDrive",
            "serialMotor",
            "serialGearbox",
            "bitalinoAddress",
            "site",
            "expectedSoftwareVersion",
          ] as const
        ).map((field) => (
          <Field
            key={field}
            label={t(field)}
            name={field}
            maxLength={field === "site" ? 500 : 200}
            defaultValue={registry[field] ?? ""}
          />
        ))}
        <Field
          label={t("commissionedAt")}
          name="commissionedAt"
          type="date"
          min="2024-01-01"
          defaultValue={dateValue(registry.commissionedAt)}
        />
      </div>
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">{t("contacts")}</legend>
        {candidates === undefined ? (
          <p>{t("loading")}</p>
        ) : candidates.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("noContacts")}</p>
        ) : (
          candidates.map((candidate) => (
            <label
              className="flex items-center gap-2 text-sm"
              key={candidate.userId}
            >
              <Input
                className="h-4 w-4"
                type="checkbox"
                name="contacts"
                value={candidate.userId}
                defaultChecked={registry.contacts.some(
                  (contact) => contact.userId === candidate.userId,
                )}
              />
              {candidate.firstName} {candidate.lastName} (
              {t(`contactRoles.${candidate.role}`)})
            </label>
          ))
        )}
      </fieldset>
      <p className="text-sm text-muted-foreground">{t("expectedHelp")}</p>
    </RegistryForm>
  );
}

export function RadiiForm({ registry }: { registry: MachineRegistry }) {
  const t = useTranslations("machineRegistry");
  const save = useMutationWithFeedback(api.machineRegistry.recordRadii);
  return (
    <RegistryForm
      title={t("saveRadii")}
      onSave={async (data) => {
        await save(
          {
            machineId: registry.machineId,
            armRadiusM: Number(text(data, "armRadiusM")),
            legTipRadiusM: Number(text(data, "legTipRadiusM")),
            measuredAt: day(data, "measuredAt"),
            measuredBy: text(data, "measuredBy"),
          },
          { success: t("radiiSaved") },
        );
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field
          label={t("armRadiusM")}
          name="armRadiusM"
          type="number"
          min="0"
          max="10"
          step="any"
          required
          defaultValue={registry.armRadiusM}
        />
        <Field
          label={t("legTipRadiusM")}
          name="legTipRadiusM"
          type="number"
          min="0"
          max="10"
          step="any"
          required
          defaultValue={registry.legTipRadiusM}
        />
        <Field
          label={t("measuredAt")}
          name="measuredAt"
          type="date"
          min="2024-01-01"
          required
          defaultValue={dateValue(registry.radiiMeasuredAt)}
        />
        <Field
          label={t("measuredBy")}
          name="measuredBy"
          maxLength={200}
          required
          defaultValue={registry.radiiMeasuredBy}
        />
      </div>
    </RegistryForm>
  );
}

export function DriveSheetForm({ registry }: { registry: MachineRegistry }) {
  const t = useTranslations("machineRegistry");
  const [file, setFile] = useState<File>();
  const upload = useRegistryUpload();
  const attach = useMutationWithFeedback(
    api.machineRegistry.attachDriveParameterSheet,
  );
  return (
    <RegistryForm
      title={t("attachSheet")}
      onSave={async (data, form) => {
        const storageId = await upload(file);
        if (storageId === null) return;
        const result = await attach(
          {
            machineId: registry.machineId,
            storageId,
            version: text(data, "sheetVersion"),
            signedBy: text(data, "signedBy"),
            at: day(data, "sheetAt"),
          },
          { success: t("sheetSaved") },
        );
        if (result.ok) {
          form.reset();
          setFile(undefined);
        }
      }}
    >
      <Field
        label={t("sheetFile")}
        type="file"
        accept=".pdf,.json,application/pdf,application/json"
        required
        onChange={(event) => setFile(event.target.files?.[0])}
      />
      <div className="grid gap-3 sm:grid-cols-2">
        <Field
          label={t("sheetVersion")}
          name="sheetVersion"
          maxLength={50}
          required
        />
        <Field label={t("signedBy")} name="signedBy" maxLength={200} required />
        <Field
          label={t("sheetAt")}
          name="sheetAt"
          type="date"
          min="2024-01-01"
          required
        />
      </div>
    </RegistryForm>
  );
}

export function ValidationForms({ registry }: { registry: MachineRegistry }) {
  const t = useTranslations("machineRegistry");
  const [file, setFile] = useState<File>();
  const rank = VALIDATION_LEVELS.indexOf(registry.effectiveLevel);
  const next = (["bench", "auto", "occupied"] as const)[rank];
  const lowerLevels = VALIDATION_LEVELS.slice(0, rank);
  const [target, setTarget] =
    useState<(typeof VALIDATION_LEVELS)[number]>("none");
  const [reason, setReason] = useState("");
  const reasonId = useId();
  const targetId = useId();
  const upload = useRegistryUpload();
  const review = useMutationWithFeedback(
    api.machineRegistry.addValidationReview,
  );
  const lower = useMutationWithFeedback(
    api.machineRegistry.lowerValidationLevel,
  );
  return (
    <>
      {next !== undefined && (
        <RegistryForm
          title={t("addReview")}
          disabled={next === "occupied" && registry.medicalKeyIsTest}
          onSave={async (data, form) => {
            const reportStorageId = await upload(file);
            if (reportStorageId === null) return;
            const result = await review(
              {
                machineId: registry.machineId,
                level: next,
                reportStorageId,
                softwareVersion: text(data, "softwareVersion"),
                medicalParametersVersion:
                  text(data, "medicalParametersVersion") || null,
              },
              { success: t("reviewSaved") },
            );
            if (result.ok) {
              form.reset();
              setFile(undefined);
            }
          }}
        >
          <p>{t("nextLevel", { level: t(`levels.${next}`) })}</p>
          <Field
            label={t("reportFile")}
            type="file"
            accept=".pdf,.json,application/pdf,application/json"
            required
            onChange={(event) => setFile(event.target.files?.[0])}
          />
          <Field
            label={t("softwareVersion")}
            name="softwareVersion"
            required
            maxLength={200}
          />
          <Field
            label={t("medicalVersion")}
            name="medicalParametersVersion"
            required={next === "occupied"}
            maxLength={200}
          />
          <p className="text-sm text-muted-foreground">{t("medicalHelp")}</p>
        </RegistryForm>
      )}
      {lowerLevels.length > 0 && (
        <RegistryForm
          title={t("lowerLevel")}
          canSubmit={reason.trim().length >= 3}
          onSave={async () => {
            if (!lowerLevels.includes(target)) return;
            const result = await lower(
              {
                machineId: registry.machineId,
                level: target,
                reason: reason.trim(),
              },
              { success: t("levelLowered") },
            );
            if (result.ok) setReason("");
          }}
        >
          <Label htmlFor={targetId}>{t("targetLevel")}</Label>
          <Select
            value={target}
            onValueChange={(value) => {
              const level = lowerLevels.find((level) => level === value);
              if (level !== undefined) setTarget(level);
            }}
          >
            <SelectTrigger id={targetId}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {lowerLevels.map((level) => (
                <SelectItem value={level} key={level}>
                  {t(`levels.${level}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Label htmlFor={reasonId}>{t("reason")}</Label>
          <Textarea
            id={reasonId}
            required
            minLength={3}
            maxLength={500}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
          <p className="text-sm text-muted-foreground">{t("reasonHelp")}</p>
        </RegistryForm>
      )}
    </>
  );
}

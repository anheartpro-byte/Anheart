"use client";

import { useState } from "react";
import { useQuery } from "convex/react";
import type { FunctionReturnType } from "convex/server";
import { useLocale, useTranslations } from "next-intl";
import { format } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import { AlertTriangle, Check, Copy, Plus, Tablet, UserCheck } from "lucide-react";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { useMutationWithFeedback } from "@/hooks/use-mutation-with-feedback";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

type Access = NonNullable<
  FunctionReturnType<typeof api.consoleAccess.listConsoleAccess>
>;
type Operator = Access["operators"][number];
type Device = Access["devices"][number];

/**
 * Who may act at the machine's console (ANH-152), for its admins: the
 * operators enabled or disabled, whether each set their code (never the code,
 * which only the operator sets), the members who could be enabled; the
 * tablets, each with its token shown once at its creation, and their
 * revocation. The server decides who may do what (`convex/consoleAccess.ts`)
 * and answers nothing to anyone else: the card then shows nothing.
 */
export function MachineConsoleAccessCard({
  machineId,
}: {
  machineId: Id<"machines">;
}) {
  const t = useTranslations();
  const locale = useLocale();
  const dateLocale = locale === "fr" ? fr : enUS;
  const when = (time: number) => format(time, "PPp", { locale: dateLocale });

  const access = useQuery(api.consoleAccess.listConsoleAccess, { machineId });
  const enable = useMutationWithFeedback(api.consoleAccess.enableOperator);
  const disable = useMutationWithFeedback(api.consoleAccess.disableOperator);
  const createDevice = useMutationWithFeedback(api.consoleAccess.createDevice);
  const revokeDevice = useMutationWithFeedback(api.consoleAccess.revokeDevice);

  const [candidate, setCandidate] = useState("");
  const [busy, setBusy] = useState(false);
  const [toDisable, setToDisable] = useState<Operator | null>(null);
  const [creating, setCreating] = useState(false);
  const [deviceName, setDeviceName] = useState("");
  const [token, setToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [toRevoke, setToRevoke] = useState<Device | null>(null);

  if (!access) return null;

  const handleEnable = async () => {
    setBusy(true);
    const result = await enable(
      { machineId, userId: candidate as Id<"users"> },
      { success: t("feedback.operatorEnabled") },
    );
    setBusy(false);
    if (result.ok) setCandidate("");
  };

  const handleDisable = async () => {
    if (!toDisable) return;
    setBusy(true);
    const result = await disable(
      { machineId, userId: toDisable.userId },
      { success: t("feedback.operatorDisabled") },
    );
    setBusy(false);
    if (result.ok) setToDisable(null);
  };

  const handleCreate = async () => {
    setBusy(true);
    const result = await createDevice(
      { machineId, name: deviceName },
      { success: t("feedback.deviceCreated") },
    );
    setBusy(false);
    if (result.ok) {
      setCreating(false);
      setDeviceName("");
      setToken(result.value.token);
    }
  };

  const handleRevoke = async () => {
    if (!toRevoke) return;
    setBusy(true);
    const result = await revokeDevice(
      { deviceId: toRevoke._id },
      { success: t("feedback.deviceRevoked") },
    );
    setBusy(false);
    if (result.ok) setToRevoke(null);
  };

  const closeToken = () => {
    setToken(null);
    setCopied(false);
  };

  const copyToken = async () => {
    if (token === null) return;
    await navigator.clipboard.writeText(token);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <UserCheck className="h-4 w-4" aria-hidden="true" />
          {t("consoleAccess.title")}
        </CardTitle>
        <CardDescription>{t("consoleAccess.description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <section aria-labelledby="console-operators" className="space-y-3">
          <h3 id="console-operators" className="font-medium">
            {t("consoleAccess.operatorsTitle")}
          </h3>
          <p className="text-sm text-muted-foreground">
            {t("consoleAccess.codeNotice")}
          </p>
          {access.operators.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {t("consoleAccess.noOperators")}
            </p>
          ) : (
            <ul className="divide-y rounded-md border">
              {access.operators.map((operator) => (
                <li
                  key={operator.userId}
                  className="flex flex-wrap items-center justify-between gap-3 p-3"
                >
                  <div className="min-w-0 space-y-1">
                    <p className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{operator.name}</span>
                      <Badge variant={operator.enabled ? "default" : "outline"}>
                        {t(
                          operator.enabled
                            ? "consoleAccess.enabled"
                            : "consoleAccess.disabled",
                        )}
                      </Badge>
                    </p>
                    <p className="text-xs text-muted-foreground">
                      {operator.codeSetAt === null
                        ? t("consoleAccess.codeMissing")
                        : t("consoleAccess.codeSetOn", {
                            date: when(operator.codeSetAt),
                          })}
                    </p>
                    {!operator.eligible && (
                      <p className="flex items-center gap-1 text-xs text-amber-600 dark:text-amber-400">
                        <AlertTriangle className="h-3 w-3" aria-hidden="true" />
                        {t("consoleAccess.notEligible")}
                      </p>
                    )}
                  </div>
                  {operator.enabled && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setToDisable(operator)}
                    >
                      {t("consoleAccess.disable")}
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
          {access.candidates.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {t("consoleAccess.noCandidates")}
            </p>
          ) : (
            <div className="flex flex-wrap items-end gap-2">
              <div className="min-w-56 flex-1 space-y-2">
                <Label id="console-operator-candidate">
                  {t("consoleAccess.enableLabel")}
                </Label>
                <Select value={candidate} onValueChange={setCandidate}>
                  <SelectTrigger
                    className="w-full"
                    aria-labelledby="console-operator-candidate"
                  >
                    <SelectValue
                      placeholder={t("consoleAccess.enablePlaceholder")}
                    />
                  </SelectTrigger>
                  <SelectContent>
                    {access.candidates.map((member) => (
                      <SelectItem key={member.userId} value={member.userId}>
                        {member.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <Button onClick={handleEnable} disabled={busy || candidate === ""}>
                {t("consoleAccess.enable")}
              </Button>
            </div>
          )}
        </section>

        <section aria-labelledby="console-devices" className="space-y-3">
          <div className="flex items-center justify-between gap-3">
            <h3 id="console-devices" className="flex items-center gap-2 font-medium">
              <Tablet className="h-4 w-4" aria-hidden="true" />
              {t("consoleAccess.devicesTitle")}
            </h3>
            <Button variant="outline" size="sm" onClick={() => setCreating(true)}>
              <Plus className="h-4 w-4 mr-1" aria-hidden="true" />
              {t("consoleAccess.createDevice")}
            </Button>
          </div>
          <p className="text-sm text-muted-foreground">
            {t("consoleAccess.devicesDescription")}
          </p>
          {access.devices.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {t("consoleAccess.noDevices")}
            </p>
          ) : (
            <ul className="divide-y rounded-md border">
              {access.devices.map((device) => (
                <li
                  key={device._id}
                  className="flex flex-wrap items-center justify-between gap-3 p-3"
                >
                  <div className="min-w-0 space-y-1">
                    <p className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{device.name}</span>
                      <code className="font-mono text-sm">{device.prefix}</code>
                      {device.revokedAt !== null && (
                        <Badge variant="destructive">
                          {t("consoleAccess.revokedBadge")}
                        </Badge>
                      )}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      {t("consoleAccess.deviceCreated", {
                        date: when(device.createdAt),
                        name: device.createdByName,
                      })}
                      {device.revokedAt !== null &&
                        ` · ${t("consoleAccess.revokedOn", { date: when(device.revokedAt) })}`}
                    </p>
                  </div>
                  {device.revokedAt === null && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setToRevoke(device)}
                    >
                      {t("consoleAccess.revoke")}
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      </CardContent>

      <Dialog
        open={toDisable !== null}
        onOpenChange={(open) => !open && setToDisable(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {t("consoleAccess.disableTitle", { name: toDisable?.name ?? "" })}
            </DialogTitle>
            <DialogDescription>
              {t("consoleAccess.disableDescription", {
                name: toDisable?.name ?? "",
              })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setToDisable(null)}>
              {t("common.cancel")}
            </Button>
            <Button variant="destructive" onClick={handleDisable} disabled={busy}>
              {t("consoleAccess.disable")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={creating} onOpenChange={setCreating}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("consoleAccess.createDeviceTitle")}</DialogTitle>
            <DialogDescription>
              {t("consoleAccess.createDeviceDescription")}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="console-device-name">
              {t("consoleAccess.deviceName")}
            </Label>
            <Input
              id="console-device-name"
              value={deviceName}
              maxLength={100}
              onChange={(event) => setDeviceName(event.target.value)}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreating(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              onClick={handleCreate}
              disabled={busy || deviceName.trim() === ""}
            >
              {t("consoleAccess.confirmCreateDevice")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={token !== null} onOpenChange={(open) => !open && closeToken()}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t("consoleAccess.tokenTitle")}</DialogTitle>
            <DialogDescription className="flex items-center gap-2 text-amber-600">
              <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
              {t("consoleAccess.tokenWarning")}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="bg-muted p-4 rounded-md font-mono text-sm break-all">
              {token}
            </div>
            <div className="flex gap-2">
              <Button onClick={copyToken} variant="outline" className="flex-1">
                {copied ? (
                  <>
                    <Check className="h-4 w-4 mr-2" aria-hidden="true" />
                    {t("common.copied")}
                  </>
                ) : (
                  <>
                    <Copy className="h-4 w-4 mr-2" aria-hidden="true" />
                    {t("common.copy")}
                  </>
                )}
              </Button>
              <Button onClick={closeToken} className="flex-1">
                {t("common.close")}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog
        open={toRevoke !== null}
        onOpenChange={(open) => !open && setToRevoke(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {t("consoleAccess.revokeTitle", { name: toRevoke?.name ?? "" })}
            </DialogTitle>
            <DialogDescription>
              {t("consoleAccess.revokeDescription")}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setToRevoke(null)}>
              {t("common.cancel")}
            </Button>
            <Button variant="destructive" onClick={handleRevoke} disabled={busy}>
              {t("consoleAccess.revoke")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

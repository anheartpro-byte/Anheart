"use client";

import { useState } from "react";
import { useQuery } from "convex/react";
import type { FunctionReturnType } from "convex/server";
import { useLocale, useTranslations } from "next-intl";
import { format } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import { AlertTriangle, Check, Copy, KeyRound, Plus } from "lucide-react";
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
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";

type MachineKey = FunctionReturnType<
  typeof api.machineKeys.listMachineKeys
>[number];

const DAY_MINUTES = 24 * 60;
/** The overlaps offered, in minutes: 24 h (the server's default), 1 h, none. */
const OVERLAPS = ["1440", "60", "0"] as const;
/**
 * The lifetimes offered to a new key, in days, or none. The server counts
 * them from its own clock: the page sends a duration, never a date.
 */
const LIFETIMES = ["none", "90", "365"] as const;

const BADGE: Record<
  MachineKey["state"],
  "default" | "secondary" | "outline" | "destructive"
> = {
  active: "default",
  replaced: "secondary",
  expired: "outline",
  revoked: "destructive",
};

/**
 * The keys of a machine (ANH-165), for its admins: each key by its prefix,
 * with its state, creation, last use and expiry; a new key, shown once, with
 * the overlap during which the keys it replaces stay valid; the revocation of
 * one key, after a confirmation. The server decides who may do what
 * (`convex/machineKeys.ts`); the page mounts this card for admins only.
 */
export function MachineKeysCard({ machineId }: { machineId: Id<"machines"> }) {
  const t = useTranslations();
  const locale = useLocale();
  const dateLocale = locale === "fr" ? fr : enUS;
  const when = (time: number) => format(time, "PPp", { locale: dateLocale });

  const keys = useQuery(api.machineKeys.listMachineKeys, { machineId });
  const createKey = useMutationWithFeedback(api.machineKeys.createMachineKey);
  const revokeKey = useMutationWithFeedback(api.machineKeys.revokeMachineKey);

  const [creating, setCreating] = useState(false);
  const [overlap, setOverlap] = useState<string>(OVERLAPS[0]);
  const [lifetime, setLifetime] = useState<string>(LIFETIMES[0]);
  const [busy, setBusy] = useState(false);
  const [newKey, setNewKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [toRevoke, setToRevoke] = useState<MachineKey | null>(null);

  const validKeys = (keys ?? []).filter(
    (key) => key.state === "active" || key.state === "replaced",
  );

  const openCreation = () => {
    setOverlap(OVERLAPS[0]);
    setLifetime(LIFETIMES[0]);
    setCreating(true);
  };

  const handleCreate = async () => {
    setBusy(true);
    const result = await createKey(
      {
        machineId,
        overlapMinutes: Number(overlap),
        lifetimeMinutes:
          lifetime === "none" ? undefined : Number(lifetime) * DAY_MINUTES,
      },
      { success: t("feedback.machineKeyCreated") },
    );
    setBusy(false);
    if (result.ok) {
      setCreating(false);
      setNewKey(result.value.apiKey);
    }
  };

  const handleRevoke = async () => {
    if (!toRevoke) return;
    setBusy(true);
    const result = await revokeKey(
      { keyId: toRevoke._id },
      { success: t("feedback.machineKeyRevoked") },
    );
    setBusy(false);
    if (result.ok) setToRevoke(null);
  };

  const closeNewKey = () => {
    setNewKey(null);
    setCopied(false);
  };

  const copyNewKey = async () => {
    if (newKey === null) return;
    await navigator.clipboard.writeText(newKey);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="h-4 w-4" aria-hidden="true" />
            {t("machineKeys.title")}
          </CardTitle>
          <Button variant="outline" size="sm" onClick={openCreation}>
            <Plus className="h-4 w-4 mr-1" aria-hidden="true" />
            {t("machineKeys.create")}
          </Button>
        </div>
        <CardDescription>{t("machineKeys.description")}</CardDescription>
      </CardHeader>
      <CardContent>
        {keys === undefined ? (
          <Skeleton className="h-16 w-full" />
        ) : keys.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {t("machineKeys.none")}
          </p>
        ) : (
          <ul className="divide-y rounded-md border">
            {keys.map((key) => (
              <li
                key={key._id}
                className="flex flex-wrap items-center justify-between gap-3 p-3"
              >
                <div className="min-w-0 space-y-1">
                  <p className="flex flex-wrap items-center gap-2">
                    <code className="font-mono text-sm">{key.prefix}</code>
                    <Badge variant={BADGE[key.state]}>
                      {t(`machineKeys.state.${key.state}`)}
                    </Badge>
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {key.createdByName === null
                      ? t("machineKeys.createdNoAuthor", {
                          date: when(key.createdAt),
                        })
                      : t("machineKeys.created", {
                          date: when(key.createdAt),
                          name: key.createdByName,
                        })}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {key.lastUsedAt === null
                      ? t("machineKeys.neverUsed")
                      : t("machineKeys.lastUsed", {
                          date: when(key.lastUsedAt),
                        })}
                    {" · "}
                    {expiryOf(key, t, when)}
                  </p>
                </div>
                {(key.state === "active" || key.state === "replaced") && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setToRevoke(key)}
                  >
                    {t("machineKeys.revoke")}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>

      {/* A new key: the overlap, the expiry, then the key, shown once. */}
      <Dialog open={creating} onOpenChange={setCreating}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("machineKeys.createTitle")}</DialogTitle>
            <DialogDescription>
              {t("machineKeys.createDescription")}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label id="machine-key-overlap">{t("machineKeys.overlap")}</Label>
              <Select value={overlap} onValueChange={setOverlap}>
                <SelectTrigger
                  className="w-full"
                  aria-labelledby="machine-key-overlap"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {OVERLAPS.map((minutes) => (
                    <SelectItem key={minutes} value={minutes}>
                      {t(`machineKeys.overlap${minutes}`)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label id="machine-key-lifetime">
                {t("machineKeys.lifetime")}
              </Label>
              <Select value={lifetime} onValueChange={setLifetime}>
                <SelectTrigger
                  className="w-full"
                  aria-labelledby="machine-key-lifetime"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {LIFETIMES.map((days) => (
                    <SelectItem key={days} value={days}>
                      {days === "none"
                        ? t("machineKeys.lifetimeNone")
                        : t(`machineKeys.lifetime${days}`)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreating(false)}>
              {t("common.cancel")}
            </Button>
            <Button onClick={handleCreate} disabled={busy}>
              {t("machineKeys.confirmCreate")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={newKey !== null}
        onOpenChange={(open) => !open && closeNewKey()}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t("machines.apiKey")}</DialogTitle>
            <DialogDescription className="flex items-center gap-2 text-amber-600">
              <AlertTriangle className="h-4 w-4" aria-hidden="true" />
              {t("machines.apiKeyWarning")}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="bg-muted p-4 rounded-md font-mono text-sm break-all">
              {newKey}
            </div>
            <div className="flex gap-2">
              <Button onClick={copyNewKey} variant="outline" className="flex-1">
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
              <Button onClick={closeNewKey} className="flex-1">
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
              {t("machineKeys.revokeTitle", { prefix: toRevoke?.prefix ?? "" })}
            </DialogTitle>
            <DialogDescription>
              {t("machineKeys.revokeDescription")}
            </DialogDescription>
          </DialogHeader>
          {validKeys.length === 1 && validKeys[0]._id === toRevoke?._id && (
            <p className="flex items-start gap-2 text-sm text-amber-600 dark:text-amber-400">
              <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
              {t("machineKeys.revokeLast")}
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setToRevoke(null)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="destructive"
              onClick={handleRevoke}
              disabled={busy}
            >
              {t("machineKeys.revoke")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

/** The end of a key, as its line says it: revoked, expired, expiring, or never. */
function expiryOf(
  key: MachineKey,
  t: ReturnType<typeof useTranslations>,
  when: (time: number) => string,
): string {
  if (key.revokedAt !== null) {
    return t("machineKeys.revokedOn", { date: when(key.revokedAt) });
  }
  if (key.expiresAt === null) return t("machineKeys.noExpiry");
  return key.state === "expired"
    ? t("machineKeys.expiredOn", { date: when(key.expiresAt) })
    : t("machineKeys.expiresOn", { date: when(key.expiresAt) });
}

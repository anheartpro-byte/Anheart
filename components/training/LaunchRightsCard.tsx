"use client";

import { useState } from "react";
import { useQuery, useMutation } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations, useLocale } from "next-intl";
import { format } from "date-fns";
import { fr, enUS } from "date-fns/locale";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { KeyRound, Loader2, Plus, UserMinus } from "lucide-react";
import { convexErrorMessage } from "@/lib/training";

/**
 * Who may launch auto sessions on this machine. Only rendered for admins and
 * gestionnaires of the machine; a gestionnaire can only grant to patients they
 * manage (the server enforces both).
 */
export function LaunchRightsCard({
  machineId,
  isAdmin,
}: {
  machineId: Id<"machines">;
  isAdmin: boolean;
}) {
  const t = useTranslations();
  const locale = useLocale();
  const dateLocale = locale === "fr" ? fr : enUS;

  const rights = useQuery(api.training.listLaunchRights, { machineId });
  const allUsers = useQuery(
    api.users.listUsers,
    isAdmin ? { role: "user" } : "skip",
  );
  const myPatients = useQuery(
    api.users.getPatientsForGestionnaire,
    isAdmin ? "skip" : {},
  );
  const grant = useMutation(api.training.grantLaunchRight);
  const revoke = useMutation(api.training.revokeLaunchRight);

  const [selected, setSelected] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toRevoke, setToRevoke] = useState<{
    userId: Id<"users">;
    name: string;
  } | null>(null);

  const pool = isAdmin ? allUsers : myPatients;
  const holders = new Set((rights ?? []).map((r) => r.userId as string));
  const candidates = (pool ?? []).filter((u) => !holders.has(u._id));

  const handleGrant = async () => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      await grant({ machineId, userId: selected as Id<"users"> });
      setSelected("");
    } catch (err) {
      setError(convexErrorMessage(err, t("common.error")));
    } finally {
      setBusy(false);
    }
  };

  const handleRevoke = async () => {
    if (!toRevoke) return;
    setBusy(true);
    setError(null);
    try {
      await revoke({ machineId, userId: toRevoke.userId });
      setToRevoke(null);
    } catch (err) {
      setError(convexErrorMessage(err, t("common.error")));
      setToRevoke(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <KeyRound className="h-4 w-4" />
          {t("training.rights.title")}
        </CardTitle>
        <CardDescription>{t("training.rights.description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {error && (
          <div className="bg-destructive/10 text-destructive p-3 rounded-md text-sm">
            {error}
          </div>
        )}

        {rights === undefined ? (
          <Skeleton className="h-16 w-full" />
        ) : rights.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {t("training.rights.none")}
          </p>
        ) : (
          <div className="divide-y rounded-md border">
            {rights.map((r) => (
              <div
                key={r.userId}
                className="flex items-center justify-between gap-3 p-3"
              >
                <div className="min-w-0">
                  <p className="font-medium truncate">{r.name}</p>
                  <p className="text-xs text-muted-foreground truncate">
                    {r.email} ·{" "}
                    {r.hrMax !== null
                      ? t("training.rights.hrMax", {
                          value: `${r.hrMax} ${t("training.units.bpm")}`,
                        })
                      : t("training.rights.hrMaxMissing")}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {t("training.rights.grantedBy", {
                      name: r.grantedByName,
                      date: format(r.createdAt, "PP", { locale: dateLocale }),
                    })}
                  </p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    setToRevoke({ userId: r.userId, name: r.name })
                  }
                >
                  <UserMinus className="h-4 w-4 mr-1" />
                  {t("training.rights.revoke")}
                </Button>
              </div>
            ))}
          </div>
        )}

        <div className="flex gap-2">
          <Select value={selected} onValueChange={setSelected}>
            <SelectTrigger className="flex-1">
              <SelectValue
                placeholder={
                  pool !== undefined && candidates.length === 0
                    ? t("training.rights.noCandidates")
                    : t("training.rights.selectUser")
                }
              />
            </SelectTrigger>
            <SelectContent>
              {candidates.map((u) => (
                <SelectItem key={u._id} value={u._id}>
                  {u.firstName} {u.lastName} ({u.email})
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button onClick={handleGrant} disabled={!selected || busy}>
            {busy ? (
              <Loader2 className="h-4 w-4 mr-1 animate-spin" />
            ) : (
              <Plus className="h-4 w-4 mr-1" />
            )}
            {t("training.rights.grant")}
          </Button>
        </div>
      </CardContent>

      <Dialog
        open={toRevoke !== null}
        onOpenChange={(open) => !open && setToRevoke(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("training.rights.revokeConfirm")}</DialogTitle>
            <DialogDescription>
              {t("training.rights.revokeConfirmDesc", {
                name: toRevoke?.name ?? "",
              })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setToRevoke(null)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="destructive"
              onClick={handleRevoke}
              disabled={busy}
            >
              {t("training.rights.revoke")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

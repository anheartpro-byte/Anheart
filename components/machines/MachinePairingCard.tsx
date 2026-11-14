"use client";

import { useState } from "react";
import { useQuery } from "convex/react";
import { useLocale, useTranslations } from "next-intl";
import { format } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import { Link2 } from "lucide-react";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { normalizedSerial } from "@/convex/lib/machinePairing";
import { useActionWithFeedback } from "@/hooks/use-action-with-feedback";
import { useLocalClock } from "@/hooks/use-local-clock";
import { useMutationWithFeedback } from "@/hooks/use-mutation-with-feedback";
import { serverClockAt } from "@/lib/server-clock";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { PairingCodeDialog, type IssuedPairingCode } from "./PairingCodeDialog";

/**
 * The pairing of a machine's console (ANH-115), for its admins: the Pi its
 * registry names, whether a code is open (who issued it, until when, never
 * the code), a new code shown once, and the revocation of the open one. The
 * server decides who may do what (`convex/machinePairing.ts`); the page
 * mounts this card for admins only.
 */
export function MachinePairingCard({
  machineId,
}: {
  machineId: Id<"machines">;
}) {
  const t = useTranslations("machinePairing");
  const tFeedback = useTranslations("feedback");
  const locale = useLocale();
  const dateLocale = locale === "fr" ? fr : enUS;
  const when = (time: number) => format(time, "PPp", { locale: dateLocale });

  const state = useQuery(api.machinePairing.pairingState, { machineId });
  const registry = useQuery(api.machineRegistry.getRegistry, { machineId });
  const issue = useActionWithFeedback(api.pairingCodes.issuePairingCode);
  const revoke = useMutationWithFeedback(api.machinePairing.revokePairingCode);
  // Once a minute is enough to stop saying an open code once it expires.
  const clock = useLocalClock(15_000);

  const [issued, setIssued] = useState<IssuedPairingCode | null>(null);
  const [busy, setBusy] = useState(false);

  const serial = normalizedSerial(registry?.serialPi);
  const open =
    state != null && state.expiresAt > serverClockAt(state.serverNow, clock)
      ? state
      : null;

  const handleIssue = async () => {
    setBusy(true);
    // No message on success: the code is in its window, never in a message.
    const result = await issue({ machineId });
    setBusy(false);
    if (result.ok) setIssued(result.value);
  };

  const handleRevoke = async () => {
    setBusy(true);
    await revoke({ machineId }, { success: tFeedback("pairingCodeRevoked") });
    setBusy(false);
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Link2 className="h-4 w-4" aria-hidden="true" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {state === undefined || registry === undefined ? (
          <Skeleton className="h-16 w-full" />
        ) : (
          <>
            <p className="text-sm">
              {serial === null ? (
                t("noSerial")
              ) : (
                <>
                  {t("serial")}{" "}
                  <code className="font-mono text-sm">{serial}</code>
                </>
              )}
            </p>
            <p className="text-sm text-muted-foreground">
              {open === null
                ? t("none")
                : open.createdByName === null
                  ? t("openNoAuthor", {
                      date: when(open.createdAt),
                      expiry: when(open.expiresAt),
                    })
                  : t("open", {
                      date: when(open.createdAt),
                      expiry: when(open.expiresAt),
                      name: open.createdByName,
                    })}
            </p>
            <div className="flex flex-wrap gap-2">
              <Button onClick={handleIssue} disabled={busy || serial === null}>
                {open === null ? t("issue") : t("reissue")}
              </Button>
              {open !== null && (
                <Button
                  variant="outline"
                  onClick={handleRevoke}
                  disabled={busy}
                >
                  {t("revoke")}
                </Button>
              )}
            </div>
            <p className="text-xs text-muted-foreground">{t("revokesKeys")}</p>
          </>
        )}
      </CardContent>
      <PairingCodeDialog issued={issued} onClose={() => setIssued(null)} />
    </Card>
  );
}

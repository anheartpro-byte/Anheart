"use client";

import { useTranslations } from "next-intl";
import { AlertTriangle } from "lucide-react";
import { useLocalClock } from "@/hooks/use-local-clock";
import { serverClockAt } from "@/lib/server-clock";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/** A code as the server issued it (`pairingCodes.issuePairingCode`). */
export type IssuedPairingCode = {
  code: string;
  expiresAt: number;
  serverNow: number;
};

/** What is left of a code, as `m:ss`, never below zero. */
function left(milliseconds: number): string {
  const seconds = Math.max(0, Math.ceil(milliseconds / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

/**
 * A pairing code (ANH-115), shown this once: large, in its two groups, with
 * the time it has left on the server's clock. Not in an address, nor a
 * message, nor the clipboard: the technician types it on the console. Past
 * its expiry it is shown no more. Closing the window forgets it.
 */
export function PairingCodeDialog({
  issued,
  onClose,
}: {
  issued: IssuedPairingCode | null;
  onClose: () => void;
}) {
  const t = useTranslations("machinePairing");
  return (
    <Dialog open={issued !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("codeTitle")}</DialogTitle>
          <DialogDescription>{t("codeIntro")}</DialogDescription>
        </DialogHeader>
        {issued !== null && <ShownCode issued={issued} />}
        <DialogFooter>
          <Button onClick={onClose}>{t("codeDone")}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ShownCode({ issued }: { issued: IssuedPairingCode }) {
  const t = useTranslations("machinePairing");
  const clock = useLocalClock();
  const remaining = issued.expiresAt - serverClockAt(issued.serverNow, clock);
  const expired = remaining <= 0;
  return (
    <div className="space-y-4">
      {/* There from the start, filled at expiry: a reader announces what changes in it. */}
      <p role="status" className="text-sm font-medium text-destructive">
        {expired ? t("expired") : ""}
      </p>
      {!expired && <LiveCode issued={issued} remaining={remaining} />}
    </div>
  );
}

function LiveCode({
  issued,
  remaining,
}: {
  issued: IssuedPairingCode;
  remaining: number;
}) {
  const t = useTranslations("machinePairing");
  return (
    <>
      <p
        aria-hidden="true"
        className="rounded-md bg-muted py-6 text-center font-mono text-4xl font-semibold tracking-[0.25em]"
      >
        {issued.code}
      </p>
      <p className="sr-only">
        {t("codeSpelled", { characters: [...issued.code].join(" ") })}
      </p>
      <p role="timer" className="text-center text-sm text-muted-foreground">
        {t("expiresIn", { time: left(remaining) })}
      </p>
      <p className="flex items-start gap-2 text-sm text-amber-600 dark:text-amber-400">
        <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
        {t("codeWarning")}
      </p>
    </>
  );
}

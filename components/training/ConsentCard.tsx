"use client";

import { useState } from "react";
import { useQuery } from "convex/react";
import { format } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import { useLocale, useTranslations } from "next-intl";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import { useMutationWithFeedback } from "@/hooks/use-mutation-with-feedback";
import {
  CONSENT_TEXT_VERSION,
  consentParagraphs,
} from "@/lib/consentText";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { FileText, Loader2 } from "lucide-react";

export function ConsentCard({ userId }: { userId: Id<"users"> }) {
  const t = useTranslations();
  const locale = useLocale();
  const dateLocale = locale === "fr" ? fr : enUS;
  const readiness = useQuery(api.fitness.getRiderReadiness, { userId });
  const currentUser = useQuery(api.users.getCurrentUser);
  const signConsent = useMutationWithFeedback(api.fitness.signConsent);
  const withdrawConsent = useMutationWithFeedback(api.fitness.withdrawConsent);

  const [consentChecked, setConsentChecked] = useState(false);
  const [confirmWithdrawal, setConfirmWithdrawal] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const activeConsent = readiness?.consent.active ?? null;
  const self = currentUser?._id === userId;
  const currentConsent = activeConsent?.textVersion === CONSENT_TEXT_VERSION;
  const canSign =
    self && readiness !== undefined && readiness !== null && !currentConsent;
  const canWithdraw =
    activeConsent !== null && (self || readiness?.canValidate === true);
  const paragraphs = consentParagraphs();
  const signedDate = activeConsent
    ? format(new Date(activeConsent.signedAt), "PPP", { locale: dateLocale })
    : null;

  let status: string;
  if (readiness === undefined || currentUser === undefined) {
    status = t("training.consent.loading");
  } else if (readiness === null) {
    status = t("training.consent.unavailable");
  } else if (activeConsent && !currentConsent) {
    status = t("training.consent.outdated", {
      version: activeConsent.textVersion,
    });
  } else if (activeConsent && signedDate) {
    status = t("training.consent.signed", {
      date: signedDate,
      version: activeConsent.textVersion,
    });
  } else if (readiness.consent.lastWithdrawnAt !== undefined) {
    status = t("training.consent.withdrawn", {
      date: format(new Date(readiness.consent.lastWithdrawnAt), "PPP", {
        locale: dateLocale,
      }),
    });
  } else {
    status = t("training.consent.notSigned");
  }

  const handleSign = async () => {
    if (!canSign || !consentChecked || saving) return;
    setSaving(true);
    setError(null);
    const result = await signConsent(
      { textVersion: CONSENT_TEXT_VERSION },
      { success: t("training.consent.signedSuccess") },
    );
    if (!result.ok) setError(result.message);
    else setConsentChecked(false);
    setSaving(false);
  };

  const handleWithdraw = async () => {
    if (!canWithdraw || saving) return;
    setSaving(true);
    setError(null);
    const result = await withdrawConsent(
      { userId },
      { success: t("training.consent.withdrawnSuccess") },
    );
    if (!result.ok) setError(result.message);
    else setConfirmWithdrawal(false);
    setSaving(false);
  };

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <FileText className="h-4 w-4" />
            {t("training.consent.title")}
          </CardTitle>
          <CardDescription>
            {t("training.consent.description", { version: CONSENT_TEXT_VERSION })}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="rounded-md border border-amber-500/50 bg-amber-50 p-3 text-sm text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
            {t("training.consent.provisional")}
          </p>
          {locale === "en" && (
            <p className="text-sm text-muted-foreground">
              {t("training.consent.frenchTextNote")}
            </p>
          )}
          <div className="space-y-3">
            {paragraphs.map((paragraph, index) => (
              <p key={`${index}-${paragraph}`} className="text-sm">
                {paragraph}
              </p>
            ))}
          </div>
          <div className="rounded-md bg-muted/40 p-3">
            <p className="text-sm font-medium">{t("training.consent.status")}</p>
            <p className="text-sm text-muted-foreground">{status}</p>
          </div>
          {error && (
            <p className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
              {error}
            </p>
          )}

          {canSign && currentUser && (
            <div className="space-y-3 border-t pt-4">
              <div className="flex items-start gap-2">
                <Checkbox
                  id="consent-read"
                  checked={consentChecked}
                  onCheckedChange={(checked) =>
                    setConsentChecked(checked === true)
                  }
                />
                <Label htmlFor="consent-read">
                  {t("training.consent.confirmRead")}
                </Label>
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <Button
                  type="button"
                  onClick={handleSign}
                  disabled={!consentChecked || saving}
                >
                  {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  {t("training.consent.sign")}
                </Button>
                <p className="text-sm text-muted-foreground">
                  {t("training.consent.identity", {
                    name: `${currentUser.firstName} ${currentUser.lastName}`.trim(),
                    email: currentUser.email,
                  })}
                </p>
              </div>
            </div>
          )}

          {canWithdraw && (
            <Button
              type="button"
              variant="outline"
              onClick={() => setConfirmWithdrawal(true)}
              disabled={saving}
            >
              {self
                ? t("training.consent.withdrawSelf")
                : t("training.consent.withdrawForRider")}
            </Button>
          )}
        </CardContent>
      </Card>

      <Dialog open={confirmWithdrawal} onOpenChange={setConfirmWithdrawal}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("training.consent.withdrawConfirmTitle")}</DialogTitle>
            <DialogDescription>
              {t("training.consent.withdrawConfirmDescription")}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setConfirmWithdrawal(false)}
            >
              {t("common.cancel")}
            </Button>
            <Button type="button" onClick={handleWithdraw} disabled={saving}>
              {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {t("training.consent.withdrawConfirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

"use client";

import { useState, type FormEvent } from "react";
import { useQuery } from "convex/react";
import { useLocale, useTranslations } from "next-intl";
import { format } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import { LockKeyhole } from "lucide-react";
import { api } from "@/convex/_generated/api";
import { useActionWithFeedback } from "@/hooks/use-action-with-feedback";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/** The form a code must have before it is sent; the server refuses the weak ones. */
const CODE = /^[0-9]{6,12}$/;

/**
 * The caller's own console code (ANH-152), on « Mes machines »: the machines
 * whose console they are an operator of, whether their code is set, and the
 * form that sets it (`consoleCodes.setMyOperatorCode`, hashed on the server,
 * readable by nobody). Nothing is shown to a member who operates no machine.
 * The code lives in the two fields only, emptied after every attempt.
 */
export function OperatorCodeCard() {
  const t = useTranslations();
  const locale = useLocale();
  const dateLocale = locale === "fr" ? fr : enUS;
  const machines = useQuery(api.consoleAccess.listMyOperatorMachines, {});
  const setCode = useActionWithFeedback(api.consoleCodes.setMyOperatorCode);
  const [code, setCodeField] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);

  if (!machines || machines.length === 0) return null;

  const problem = !CODE.test(code)
    ? t("operatorCode.notDigits")
    : code !== again
      ? t("operatorCode.mismatch")
      : null;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (problem !== null) return;
    setBusy(true);
    await setCode({ code }, { success: t("feedback.operatorCodeSet") });
    setCodeField("");
    setAgain("");
    setBusy(false);
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <LockKeyhole className="h-4 w-4" aria-hidden="true" />
          {t("operatorCode.title")}
        </CardTitle>
        <CardDescription>{t("operatorCode.description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <ul className="divide-y rounded-md border">
          {machines.map((machine) => (
            <li
              key={machine.machineId}
              className="flex flex-wrap items-center justify-between gap-2 p-3"
            >
              <span className="font-medium">{machine.machineName}</span>
              <Badge variant={machine.codeSetAt === null ? "outline" : "secondary"}>
                {machine.codeSetAt === null
                  ? t("operatorCode.codeMissing")
                  : t("operatorCode.codeSetOn", {
                      date: format(machine.codeSetAt, "PPp", { locale: dateLocale }),
                    })}
              </Badge>
            </li>
          ))}
        </ul>
        <form onSubmit={submit} className="space-y-3">
          <p className="text-sm text-muted-foreground">{t("operatorCode.rules")}</p>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="operator-code">{t("operatorCode.code")}</Label>
              <Input
                id="operator-code"
                type="password"
                inputMode="numeric"
                autoComplete="new-password"
                maxLength={12}
                value={code}
                onChange={(event) => setCodeField(event.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="operator-code-again">{t("operatorCode.confirm")}</Label>
              <Input
                id="operator-code-again"
                type="password"
                inputMode="numeric"
                autoComplete="new-password"
                maxLength={12}
                value={again}
                onChange={(event) => setAgain(event.target.value)}
              />
            </div>
          </div>
          {code !== "" && problem !== null && (
            <p className="text-sm text-amber-600 dark:text-amber-400">{problem}</p>
          )}
          <Button type="submit" disabled={busy || problem !== null}>
            {t("operatorCode.submit")}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

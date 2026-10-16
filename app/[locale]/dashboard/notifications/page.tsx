"use client";

import { useLocale, useTranslations } from "next-intl";
import { useQuery } from "convex/react";
import type { FunctionReturnType } from "convex/server";
import { AlertTriangle, Bell, Mail } from "lucide-react";
import { api } from "@/convex/_generated/api";
import { Link } from "@/i18n/navigation";
import { useMutationWithFeedback } from "@/hooks/use-mutation-with-feedback";
import { isStaff } from "@/lib/organizationAccess";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";

type Alert = FunctionReturnType<typeof api.notifications.list>[number];
type AlertType = Alert["type"];

/** The kinds of alert, in the order the page offers their e-mail choice. */
const TYPES: AlertType[] = [
  "drive_fault",
  "safety_verdict",
  "session_failed",
  "machine_offline",
];

/** As many alerts as the server lists at most (`LIST_LIMIT`). */
const LIST_LIMIT = 50;

/**
 * The alerts of the machines the visitor manages (ANH-90): what happened,
 * where, when, and a link to the page that shows the rest to whoever may read
 * it. Then the choice of the alerts also sent by e-mail.
 */
export default function NotificationsPage() {
  const t = useTranslations("notifications");
  const user = useQuery(api.users.getCurrentUser);
  const staff = isStaff(user?.role);
  const alerts = useQuery(api.notifications.list, staff ? {} : "skip");
  const settings = useQuery(api.notifications.getSettings, staff ? {} : "skip");
  const markRead = useMutationWithFeedback(api.notifications.markRead);
  const markAllRead = useMutationWithFeedback(api.notifications.markAllRead);
  const updateSettings = useMutationWithFeedback(
    api.notifications.updateSettings,
  );

  if (user === undefined) return <NotificationsSkeleton />;
  if (!user || !staff) {
    return (
      <div className="text-center py-12">
        <Bell className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
        <p className="text-muted-foreground">{t("accessDenied")}</p>
      </div>
    );
  }
  if (alerts === undefined) return <NotificationsSkeleton />;

  const unread = alerts.some((alert) => !alert.read);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
          <p className="text-muted-foreground">{t("description")}</p>
        </div>
        {unread && (
          <Button
            variant="outline"
            onClick={() => markAllRead({}, { success: t("allRead") })}
          >
            {t("markAllRead")}
          </Button>
        )}
      </div>

      {alerts.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-16">
            <Bell className="h-12 w-12 text-muted-foreground/50 mb-4" />
            <p className="text-muted-foreground">{t("empty")}</p>
          </CardContent>
        </Card>
      ) : (
        <ul className="grid gap-3" aria-label={t("title")}>
          {alerts.map((alert) => (
            <AlertItem
              key={alert._id}
              alert={alert}
              onRead={() => markRead({ notificationId: alert._id })}
            />
          ))}
        </ul>
      )}
      {alerts.length >= LIST_LIMIT && (
        <p className="text-sm text-muted-foreground">
          {t("listCut", { count: LIST_LIMIT })}
        </p>
      )}

      {settings && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Mail className="h-4 w-4" />
              {t("settings.title")}
            </CardTitle>
            <CardDescription>{t("settings.description")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {!settings.emailAvailable && (
              <p
                role="status"
                className="flex items-center gap-2 text-sm text-muted-foreground"
              >
                <AlertTriangle className="h-4 w-4" />
                {t("settings.unavailable")}
              </p>
            )}
            {TYPES.map((type) => {
              const name = t(`types.${type}`);
              return (
                <div
                  key={type}
                  className="flex items-center justify-between gap-4"
                >
                  <Label htmlFor={`email-${type}`}>{name}</Label>
                  <Switch
                    id={`email-${type}`}
                    aria-label={t("settings.email", { type: name })}
                    checked={settings.email[type]}
                    onCheckedChange={(email) =>
                      updateSettings(
                        { type, email },
                        { success: t("settings.saved") },
                      )
                    }
                  />
                </div>
              );
            })}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

/** One alert: its kind, its machine, when, what the machine said, and where to look. */
function AlertItem({ alert, onRead }: { alert: Alert; onRead: () => void }) {
  const t = useTranslations("notifications");
  const tErrors = useTranslations("errors");
  const locale = useLocale();
  const at = (time: number) =>
    new Date(time).toLocaleString(locale, {
      dateStyle: "short",
      timeStyle: "short",
    });

  // A code the site words (a launch refused before its machine armed it),
  // else the sentence of the session as it is.
  const reason =
    alert.reasonCode !== undefined && tErrors.has(alert.reasonCode)
      ? tErrors(alert.reasonCode)
      : alert.reason;

  let detail: string | null = null;
  if (alert.type === "safety_verdict") {
    detail =
      alert.detail !== undefined && t.has(`verdict.${alert.detail}`)
        ? t(`verdict.${alert.detail}`)
        : t("verdict.unknown");
  } else if (alert.type === "drive_fault" && alert.detail !== undefined) {
    detail = t("faultCode", { code: alert.detail });
  }

  return (
    <li
      data-alert={alert.type}
      data-read={alert.read}
      className="rounded-lg border bg-card p-4"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <p className="flex items-center gap-2 font-medium">
            {t(`types.${alert.type}`)}
            {!alert.read && <Badge variant="destructive">{t("unread")}</Badge>}
          </p>
          <p className="text-sm text-muted-foreground">
            {alert.machineName} ·{" "}
            {alert.count > 1
              ? t("repeated", { count: alert.count, time: at(alert.updatedAt) })
              : at(alert.createdAt)}
          </p>
          {detail && <p className="text-sm">{detail}</p>}
          {reason && <p className="text-sm">{t("reason", { reason })}</p>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {alert.machineDeleted && (
            <span className="text-sm text-muted-foreground">
              {t("machineDeleted")}
            </span>
          )}
          {(alert.sessionId || !alert.machineDeleted) && (
            <Button variant="ghost" size="sm" asChild>
              <Link
                href={
                  alert.sessionId
                    ? `/dashboard/sessions/${alert.sessionId}`
                    : `/dashboard/machines/${alert.machineId}`
                }
              >
                {alert.sessionId ? t("openSession") : t("openMachine")}
              </Link>
            </Button>
          )}
          {!alert.read && (
            <Button variant="outline" size="sm" onClick={onRead}>
              {t("markRead")}
            </Button>
          )}
        </div>
      </div>
    </li>
  );
}

function NotificationsSkeleton() {
  return (
    <div className="space-y-6" aria-busy="true">
      <div>
        <Skeleton className="h-8 w-48 mb-2" />
        <Skeleton className="h-4 w-72" />
      </div>
      <div className="grid gap-3">
        {[1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-20 w-full" />
        ))}
      </div>
    </div>
  );
}

"use client";

import { useQuery } from "convex/react";
import { useTranslations } from "next-intl";
import { Bell } from "lucide-react";
import { api } from "@/convex/_generated/api";
import { Link } from "@/i18n/navigation";
import { cn } from "@/lib/utils";

/** The most the badge writes; beyond, "99+". */
const MAX_SHOWN = 99;

/**
 * The bell of the header (ANH-90): the alerts page, with the number of unread
 * alerts. Nothing for whoever receives no alerts (a patient, an account the
 * server places in no organisation, a visitor signed out), nor while the
 * number is loading: `notifications.unreadCount` answers null for them.
 */
export function NotificationBell() {
  const t = useTranslations("notifications");
  const unread = useQuery(api.notifications.unreadCount);
  if (unread === undefined || unread === null) return null;

  return (
    <Link
      href="/dashboard/notifications"
      aria-label={unread > 0 ? t("bellUnread", { count: unread }) : t("bell")}
      className={cn(
        "relative inline-flex h-10 w-10 items-center justify-center rounded-md",
        "hover:bg-accent hover:text-accent-foreground",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
      )}
    >
      <Bell className="h-5 w-5" aria-hidden="true" />
      {unread > 0 && (
        <span
          data-unread=""
          aria-hidden="true"
          className="absolute -right-0.5 -top-0.5 min-w-5 rounded-full bg-destructive px-1 text-center text-xs font-semibold leading-5 text-white"
        >
          {unread > MAX_SHOWN ? `${MAX_SHOWN}+` : unread}
        </span>
      )}
    </Link>
  );
}

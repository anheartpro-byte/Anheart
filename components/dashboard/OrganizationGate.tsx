"use client";

import type { ReactNode } from "react";
import { OrganizationSwitcher, useAuth } from "@clerk/nextjs";
import { useQuery } from "convex/react";
import { useLocale, useTranslations } from "next-intl";
import { Building2, Loader2 } from "lucide-react";
import { api } from "@/convex/_generated/api";
import { organizationView } from "@/lib/organizationAccess";
import { Card, CardContent } from "@/components/ui/card";

/**
 * Draws the pages of the dashboard only for the organisation Clerk shows as
 * active, once the server has answered for it.
 *
 * After a switch, Clerk shows the new organisation at once while the Convex
 * client still holds the previous organisation's answers, until it has sent
 * the new token: nothing is drawn in between. When the server accepts no
 * organisation for the call, the visitor is told why and can choose one.
 * The rule is `organizationView` (lib/organizationAccess.ts).
 */
export function OrganizationGate({ children }: { children: ReactNode }) {
  const t = useTranslations("organization");
  const locale = useLocale();
  const { isLoaded, orgId } = useAuth();
  const account = useQuery(api.users.getCurrentUser);
  const view = organizationView(account, {
    isLoaded,
    activeOrganization: orgId,
  });

  if (view.state === "switching") {
    return (
      <div
        role="status"
        className="flex items-center justify-center gap-2 py-12 text-muted-foreground"
      >
        <Loader2 className="h-4 w-4 animate-spin" />
        <span>{t("switching")}</span>
      </div>
    );
  }

  if (view.state === "refused") {
    return (
      <Card className="mx-auto max-w-lg">
        <CardContent className="flex flex-col items-center gap-4 py-10 text-center">
          <Building2 className="h-10 w-10 text-muted-foreground" />
          <p role="alert">{t(`refused.${view.reason}`)}</p>
          <OrganizationSwitcher
            hidePersonal
            afterSelectOrganizationUrl={`/${locale}/dashboard`}
          />
        </CardContent>
      </Card>
    );
  }

  return <>{children}</>;
}

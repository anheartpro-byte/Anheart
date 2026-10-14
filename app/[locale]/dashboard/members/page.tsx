"use client";

import { OrganizationProfile, OrganizationSwitcher, useAuth } from "@clerk/nextjs";
import { useQuery } from "convex/react";
import { useLocale, useTranslations } from "next-intl";
import { Shield } from "lucide-react";
import { api } from "@/convex/_generated/api";
import { isOrganizationAdmin } from "@/lib/organizationAccess";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * The members of the active organisation, managed in Clerk: invitations,
 * removals and roles. Clerk carries the roles and sends each change to
 * Convex by its webhook (`convex/clerkWebhook.ts`); the site keeps no copy.
 *
 * For the admins of the organisation. Clerk applies its own rights inside
 * the panel: only an `org:admin` may invite, remove or change a role.
 */
export default function MembersPage() {
  const t = useTranslations("members");
  const locale = useLocale();
  const { isLoaded, orgId } = useAuth();
  const user = useQuery(api.users.getCurrentUser);

  if (user === undefined || !isLoaded) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }

  if (!isOrganizationAdmin(user?.role) || user?.organizationAccess !== "granted") {
    return (
      <div className="text-center py-12">
        <Shield className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
        <p className="text-muted-foreground">{t("accessDenied")}</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t("title")}</h1>
        <p className="text-muted-foreground">{t("description")}</p>
      </div>
      {/* Clerk draws the members of the organisation active in its session:
          without one (a deployment before ANHEART_ORG_ID), choose it first. */}
      {orgId ? (
        <OrganizationProfile
          routing="hash"
          afterLeaveOrganizationUrl={`/${locale}/dashboard`}
        />
      ) : (
        <OrganizationSwitcher
          hidePersonal
          afterSelectOrganizationUrl={`/${locale}/dashboard/members`}
        />
      )}
    </div>
  );
}

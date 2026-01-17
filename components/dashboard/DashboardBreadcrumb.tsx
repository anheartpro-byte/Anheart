"use client";

import { useTranslations } from "next-intl";
import { usePathname, Link } from "@/i18n/navigation";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { Fragment } from "react";

export function DashboardBreadcrumb() {
  const t = useTranslations("nav");
  const pathname = usePathname();

  // Parse pathname into segments
  const segments = pathname.split("/").filter(Boolean);

  // Map segment names to translated labels
  const getLabel = (segment: string): string => {
    // Handle IDs (usually the last segment that isn't a known page)
    if (segment.length > 10 && !["new", "live", "edit"].includes(segment)) {
      return "Details";
    }

    const labelMap: Record<string, string> = {
      dashboard: t("dashboard"),
      machines: t("machines"),
      sessions: t("sessions"),
      users: t("users"),
      patients: t("patients"),
      settings: t("settings"),
      reports: t("reports"),
      new: "New",
      live: "Live",
      edit: "Edit",
    };

    return labelMap[segment] || segment;
  };

  // Build breadcrumb items
  const breadcrumbItems = segments.map((segment, idx) => {
    const href = "/" + segments.slice(0, idx + 1).join("/");
    const label = getLabel(segment);
    const isLast = idx === segments.length - 1;

    return { href, label, isLast };
  });

  // Don't show breadcrumb on dashboard root
  if (breadcrumbItems.length <= 1) {
    return null;
  }

  return (
    <Breadcrumb>
      <BreadcrumbList>
        {breadcrumbItems.map((item, index) => (
          <Fragment key={item.href}>
            <BreadcrumbItem>
              {item.isLast ? (
                <BreadcrumbPage>{item.label}</BreadcrumbPage>
              ) : (
                <BreadcrumbLink asChild>
                  <Link href={item.href}>{item.label}</Link>
                </BreadcrumbLink>
              )}
            </BreadcrumbItem>
            {!item.isLast && <BreadcrumbSeparator />}
          </Fragment>
        ))}
      </BreadcrumbList>
    </Breadcrumb>
  );
}

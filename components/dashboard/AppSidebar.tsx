"use client";

import { useTranslations } from "next-intl";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Link, usePathname } from "@/i18n/navigation";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  SidebarSeparator,
} from "@/components/ui/sidebar";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import {
  LayoutDashboard,
  Cpu,
  Activity,
  Users,
  Settings,
  FileText,
} from "lucide-react";
import Image from "next/image";

type NavItem = {
  labelKey: string;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
};

export function AppSidebar() {
  const t = useTranslations("nav");
  const tSidebar = useTranslations("sidebar");
  const pathname = usePathname();
  const user = useQuery(api.users.getCurrentUser);

  // Admin-only menu items
  const adminNavigation: NavItem[] = [
    {
      labelKey: "gestionnaires",
      href: "/dashboard/gestionnaires",
      icon: Users,
    },
    {
      labelKey: "users",
      href: "/dashboard/users",
      icon: Users,
    },
    {
      labelKey: "settings",
      href: "/dashboard/settings",
      icon: Settings,
    },
  ];

  // Manager (gestionnaire) menu items
  const managerNavigation: NavItem[] = [
    {
      labelKey: "patients",
      href: "/dashboard/patients",
      icon: Users,
    },
    {
      labelKey: "machines",
      href: "/dashboard/machines",
      icon: Cpu,
    },
    {
      labelKey: "sessions",
      href: "/dashboard/sessions",
      icon: Activity,
    },
    {
      labelKey: "reports",
      href: "/dashboard/reports",
      icon: FileText,
    },
  ];

  // User (patient) menu items
  const userNavigation: NavItem[] = [
    {
      labelKey: "sessions",
      href: "/dashboard/sessions",
      icon: Activity,
    },
    {
      labelKey: "reports",
      href: "/dashboard/reports",
      icon: FileText,
    },
  ];

  // Determine which sections to show based on role
  const showAdminSection = user?.role === "admin";
  const showManagerSection =
    user?.role === "gestionnaire" || user?.role === "admin";
  const showUserSection = user?.role === "user";

  const renderNavItem = (item: NavItem) => {
    const isActive =
      pathname === item.href ||
      (item.href !== "/dashboard" && pathname.startsWith(item.href));
    return (
      <SidebarMenuItem key={item.href}>
        <SidebarMenuButton
          asChild
          isActive={isActive}
          tooltip={t(item.labelKey)}
        >
          <Link href={item.href}>
            <item.icon className="size-4" />
            <span>{t(item.labelKey)}</span>
          </Link>
        </SidebarMenuButton>
      </SidebarMenuItem>
    );
  };

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg" asChild>
              <Link href="/dashboard">
                <Image
                  src="/logo.png"
                  width={30}
                  height={30}
                  alt="Anheart logo"
                />
                <div className="grid flex-1 text-left text-sm leading-tight">
                  <span className="truncate font-semibold">AnHeart</span>
                </div>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        {/* Main Dashboard - visible to all */}
        <SidebarGroup>
          <SidebarGroupLabel>{tSidebar("main")}</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  asChild
                  isActive={pathname === "/dashboard"}
                  tooltip={t("dashboard")}
                >
                  <Link href="/dashboard">
                    <LayoutDashboard className="size-4" />
                    <span>{t("dashboard")}</span>
                  </Link>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        {/* Admin Section */}
        {showAdminSection && (
          <>
            <SidebarSeparator />
            <SidebarGroup>
              <SidebarGroupLabel>
                {tSidebar("administration")}
              </SidebarGroupLabel>
              <SidebarGroupContent>
                <SidebarMenu>{adminNavigation.map(renderNavItem)}</SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          </>
        )}

        {/* Manager Section */}
        {showManagerSection && (
          <>
            <SidebarSeparator />
            <SidebarGroup>
              <SidebarGroupLabel>{tSidebar("management")}</SidebarGroupLabel>
              <SidebarGroupContent>
                <SidebarMenu>
                  {managerNavigation.map(renderNavItem)}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          </>
        )}

        {/* User (Patient) Section */}
        {showUserSection && (
          <>
            <SidebarSeparator />
            <SidebarGroup>
              <SidebarGroupLabel>{tSidebar("myHealth")}</SidebarGroupLabel>
              <SidebarGroupContent>
                <SidebarMenu>{userNavigation.map(renderNavItem)}</SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          </>
        )}
      </SidebarContent>

      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg">
              <Avatar className="h-8 w-8">
                <AvatarFallback className="bg-primary/10 text-primary">
                  {user?.firstName?.[0]}
                  {user?.lastName?.[0]}
                </AvatarFallback>
              </Avatar>
              <div className="grid flex-1 text-left text-sm leading-tight">
                <span className="truncate font-semibold">
                  {user?.firstName} {user?.lastName}
                </span>
                <span className="truncate text-xs text-muted-foreground">
                  {user?.email}
                </span>
              </div>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  );
}

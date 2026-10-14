"use client";

import { useLocale, useTranslations } from "next-intl";
import { useQuery } from "convex/react";
import { OrganizationSwitcher } from "@clerk/nextjs";
import { api } from "@/convex/_generated/api";
import { Link, usePathname } from "@/i18n/navigation";
import { navigationFor } from "@/lib/organizationAccess";
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
  Gauge,
  UserCog,
} from "lucide-react";
import Image from "next/image";

type NavItem = {
  labelKey: string;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
};

/** Every entry the sidebar can show, by key of the `nav` messages. */
const ENTRIES: Record<string, NavItem> = {
  gestionnaires: {
    labelKey: "gestionnaires",
    href: "/dashboard/gestionnaires",
    icon: Users,
  },
  members: { labelKey: "members", href: "/dashboard/members", icon: UserCog },
  users: { labelKey: "users", href: "/dashboard/users", icon: Users },
  settings: { labelKey: "settings", href: "/dashboard/settings", icon: Settings },
  patients: { labelKey: "patients", href: "/dashboard/patients", icon: Users },
  machines: { labelKey: "machines", href: "/dashboard/machines", icon: Cpu },
  myMachines: {
    labelKey: "myMachines",
    href: "/dashboard/my-machines",
    icon: Gauge,
  },
  sessions: { labelKey: "sessions", href: "/dashboard/sessions", icon: Activity },
  reports: { labelKey: "reports", href: "/dashboard/reports", icon: FileText },
};

export function AppSidebar() {
  const t = useTranslations("nav");
  const tSidebar = useTranslations("sidebar");
  const locale = useLocale();
  const pathname = usePathname();
  const user = useQuery(api.users.getCurrentUser);

  // Which sections and entries a role is given: lib/organizationAccess.ts.
  // Nothing beyond the dashboard while the server accepts no organisation.
  const navigation = navigationFor(user);
  const items = (keys: string[]) => keys.map((key) => ENTRIES[key]);
  const adminNavigation = items(navigation.administration);
  const managerNavigation = items(navigation.management);
  const userNavigation = items(navigation.myHealth);

  const showAdminSection = adminNavigation.length > 0;
  const showManagerSection = managerNavigation.length > 0;
  const showUserSection = userNavigation.length > 0;

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
                  alt="Gaura logo"
                  className="object-contain"
                />
                <div className="grid flex-1 text-left text-sm leading-tight">
                  <span className="truncate font-semibold">Gaura</span>
                </div>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
        {/* The organisation worked in: every answer of the server is for it.
            Members are managed in Clerk, from its "Manage" entry or the
            Members page. Hidden when the sidebar is folded to icons. */}
        {user && (
          <div className="px-1 group-data-[collapsible=icon]:hidden">
            <OrganizationSwitcher
              hidePersonal
              afterSelectOrganizationUrl={`/${locale}/dashboard`}
              afterLeaveOrganizationUrl={`/${locale}/dashboard`}
            />
          </div>
        )}
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

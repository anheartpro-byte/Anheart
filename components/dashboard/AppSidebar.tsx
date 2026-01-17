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
} from "@/components/ui/sidebar";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import {
  LayoutDashboard,
  Cpu,
  Activity,
  Users,
  Settings,
  FileText,
  Heart,
} from "lucide-react";
import Image from 'next/image'
type NavItem = {
  labelKey: string;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  roles: string[];
};

export function AppSidebar() {
  const t = useTranslations("nav");
  const pathname = usePathname();
  const user = useQuery(api.users.getCurrentUser);

  const navigation: NavItem[] = [
    {
      labelKey: "dashboard",
      href: "/dashboard",
      icon: LayoutDashboard,
      roles: ["admin", "gestionnaire", "technician", "user"],
    },
    {
      labelKey: "users",
      href: "/dashboard/users",
      icon: Users,
      roles: ["admin"],
    },
    {
      labelKey: "patients",
      href: "/dashboard/patients",
      icon: Users,
      roles: ["gestionnaire"],
    },
    {
      labelKey: "machines",
      href: "/dashboard/machines",
      icon: Cpu,
      roles: ["admin", "gestionnaire", "technician"],
    },
    {
      labelKey: "sessions",
      href: "/dashboard/sessions",
      icon: Activity,
      roles: ["admin", "gestionnaire", "technician", "user"],
    },
    {
      labelKey: "reports",
      href: "/dashboard/reports",
      icon: FileText,
      roles: ["gestionnaire", "user"],
    },
    {
      labelKey: "settings",
      href: "/dashboard/settings",
      icon: Settings,
      roles: ["admin"],
    },
  ];

  const filteredNav = navigation.filter(
    (item) => user && item.roles.includes(user.role),
  );

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
        <SidebarGroup>
          <SidebarGroupLabel>Navigation</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {filteredNav.map((item) => {
                const isActive =
                  pathname === item.href ||
                  (item.href !== "/dashboard" &&
                    pathname.startsWith(item.href));
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
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
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

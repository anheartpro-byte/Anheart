"use client";

import { UserButton } from "@clerk/nextjs";
import { LanguageSwitcher } from "@/components/LanguageSwitcher";
import { ThemeToggle } from "@/components/ThemeToggle";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { Separator } from "@/components/ui/separator";
import { DashboardBreadcrumb } from "./DashboardBreadcrumb";

export function Header() {
  return (
    <header className="sticky top-0 z-10 flex h-16 shrink-0 items-center gap-2 border-b bg-background px-4">
      <SidebarTrigger className="-ml-1" />
      <Separator orientation="vertical" className="mr-2 h-4" />
      <DashboardBreadcrumb />
      <div className="ml-auto flex items-center gap-2">
        <ThemeToggle />
        <LanguageSwitcher />
        <UserButton afterSwitchSessionUrl="/" />
      </div>
    </header>
  );
}

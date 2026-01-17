"use client";

import { SidebarProvider, SidebarInset } from "@/components/ui/sidebar";
import { AppSidebar } from "@/components/dashboard/AppSidebar";
import { Header } from "@/components/dashboard/Header";
import { LightRays } from "@/components/ui/light-rays";
import { useEffect, useState } from "react";

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const [defaultOpen, setDefaultOpen] = useState(true);

  useEffect(() => {
    const sidebarState = document.cookie
      .split("; ")
      .find((row) => row.startsWith("sidebar_state="));
    if (sidebarState) {
      setDefaultOpen(sidebarState.split("=")[1] !== "false");
    }
  }, []);

  return (
    <SidebarProvider defaultOpen={defaultOpen}>
      <AppSidebar />
      <SidebarInset className="flex flex-col h-screen overflow-hidden relative">
        <LightRays
          className="absolute inset-0 pointer-events-none z-0"
          count={4}
          color="rgba(99, 102, 241, 0.08)"
          blur={60}
          speed={20}
          length="50vh"
        />
        <Header />
        <main className="flex-1 overflow-y-auto p-6 relative z-10">
          {children}
        </main>
      </SidebarInset>
    </SidebarProvider>
  );
}

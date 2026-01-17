# Task 4.2: Dashboard Layout

## Objective

Create the main dashboard layout with sidebar navigation, header, and role-based menu items.

## Dependencies

- Task 4.1 (i18n Setup) completed

---

## Acceptance Criteria

### Layout Structure

- [ ] Sidebar with navigation links
- [ ] Header with user menu and language switcher
- [ ] Main content area
- [ ] Responsive design (mobile sidebar toggle)
- [ ] Dark/light mode support (optional)

### Role-Based Navigation

- [ ] Admin sees: Dashboard, Users, Machines, Sessions, Settings
- [ ] Gestionnaire sees: Dashboard, Patients, Machines, Sessions, Reports
- [ ] Technician sees: Dashboard, Machines, Sessions
- [ ] User sees: Dashboard, My Sessions, My Reports

### Components

- [ ] `DashboardLayout` - main layout wrapper
- [ ] `Sidebar` - navigation sidebar
- [ ] `Header` - top header bar
- [ ] `UserMenu` - dropdown with profile, settings, sign out
- [ ] `RoleGuard` - redirect if wrong role

### Loading States

- [ ] Skeleton loaders for async content
- [ ] Loading indicator for page transitions

---

## Implementation

```typescript
// app/[locale]/dashboard/layout.tsx
import { redirect } from "next/navigation";
import { auth } from "@clerk/nextjs/server";
import { Sidebar } from "@/components/dashboard/Sidebar";
import { Header } from "@/components/dashboard/Header";

export default async function DashboardLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: { locale: string };
}) {
  const { userId } = auth();

  if (!userId) {
    redirect(`/${params.locale}/sign-in`);
  }

  return (
    <div className="flex h-screen bg-gray-100 dark:bg-gray-900">
      <Sidebar locale={params.locale} />
      <div className="flex-1 flex flex-col overflow-hidden">
        <Header />
        <main className="flex-1 overflow-y-auto p-6">
          {children}
        </main>
      </div>
    </div>
  );
}
```

```typescript
// components/dashboard/Sidebar.tsx
"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import {
  HomeIcon,
  CpuChipIcon,
  ClipboardDocumentListIcon,
  UsersIcon,
  DocumentChartBarIcon,
  Cog6ToothIcon,
} from "@heroicons/react/24/outline";

type NavItem = {
  name: string;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  roles: string[];
};

export function Sidebar({ locale }: { locale: string }) {
  const t = useTranslations("nav");
  const pathname = usePathname();
  const user = useQuery(api.users.getCurrentUser);

  const navigation: NavItem[] = [
    {
      name: t("dashboard"),
      href: `/${locale}/dashboard`,
      icon: HomeIcon,
      roles: ["admin", "gestionnaire", "technician", "user"],
    },
    {
      name: t("users"),
      href: `/${locale}/dashboard/users`,
      icon: UsersIcon,
      roles: ["admin"],
    },
    {
      name: t("patients"),
      href: `/${locale}/dashboard/patients`,
      icon: UsersIcon,
      roles: ["gestionnaire"],
    },
    {
      name: t("machines"),
      href: `/${locale}/dashboard/machines`,
      icon: CpuChipIcon,
      roles: ["admin", "gestionnaire", "technician"],
    },
    {
      name: t("sessions"),
      href: `/${locale}/dashboard/sessions`,
      icon: ClipboardDocumentListIcon,
      roles: ["admin", "gestionnaire", "technician", "user"],
    },
    {
      name: t("reports"),
      href: `/${locale}/dashboard/reports`,
      icon: DocumentChartBarIcon,
      roles: ["gestionnaire", "user"],
    },
    {
      name: t("settings"),
      href: `/${locale}/dashboard/settings`,
      icon: Cog6ToothIcon,
      roles: ["admin"],
    },
  ];

  const filteredNav = navigation.filter(
    (item) => user && item.roles.includes(user.role)
  );

  return (
    <div className="hidden md:flex md:w-64 md:flex-col">
      <div className="flex flex-col flex-grow pt-5 bg-white dark:bg-gray-800 overflow-y-auto border-r">
        <div className="flex items-center flex-shrink-0 px-4">
          <span className="text-xl font-bold text-blue-600">AnHeart</span>
        </div>
        <div className="mt-8 flex-grow flex flex-col">
          <nav className="flex-1 px-2 space-y-1">
            {filteredNav.map((item) => {
              const isActive = pathname === item.href ||
                (item.href !== `/${locale}/dashboard` && pathname.startsWith(item.href));
              return (
                <Link
                  key={item.name}
                  href={item.href}
                  className={`
                    group flex items-center px-2 py-2 text-sm font-medium rounded-md
                    ${isActive
                      ? "bg-blue-100 text-blue-600 dark:bg-blue-900 dark:text-blue-200"
                      : "text-gray-600 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700"
                    }
                  `}
                >
                  <item.icon
                    className={`mr-3 h-5 w-5 ${
                      isActive ? "text-blue-600" : "text-gray-400"
                    }`}
                  />
                  {item.name}
                </Link>
              );
            })}
          </nav>
        </div>
      </div>
    </div>
  );
}
```

```typescript
// components/dashboard/Header.tsx
"use client";

import { UserButton } from "@clerk/nextjs";
import { LanguageSwitcher } from "@/components/LanguageSwitcher";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";

export function Header() {
  const user = useQuery(api.users.getCurrentUser);

  return (
    <header className="bg-white dark:bg-gray-800 shadow-sm border-b">
      <div className="flex items-center justify-between px-4 py-3">
        <div className="flex items-center">
          {/* Mobile menu button would go here */}
        </div>
        <div className="flex items-center gap-4">
          <span className="text-sm text-gray-600 dark:text-gray-300">
            {user?.firstName} {user?.lastName}
          </span>
          <LanguageSwitcher />
          <UserButton afterSignOutUrl="/" />
        </div>
      </div>
    </header>
  );
}
```

```typescript
// components/RoleGuard.tsx
"use client";

import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { redirect } from "next/navigation";
import { useEffect } from "react";

type Role = "admin" | "gestionnaire" | "technician" | "user";

export function RoleGuard({
  children,
  allowedRoles,
  locale,
}: {
  children: React.ReactNode;
  allowedRoles: Role[];
  locale: string;
}) {
  const user = useQuery(api.users.getCurrentUser);

  useEffect(() => {
    if (user && !allowedRoles.includes(user.role as Role)) {
      redirect(`/${locale}/dashboard`);
    }
  }, [user, allowedRoles, locale]);

  if (user === undefined) {
    return <div>Loading...</div>;
  }

  if (!user || !allowedRoles.includes(user.role as Role)) {
    return null;
  }

  return <>{children}</>;
}
```

---

## Testing Steps

1. Login as admin - verify sees all menu items
2. Login as gestionnaire - verify sees Patients, Machines, Sessions, Reports
3. Login as technician - verify sees Machines, Sessions
4. Login as user - verify sees only Sessions, Reports
5. Try to access admin page as user - verify redirect
6. Test mobile responsive view (sidebar collapses)
7. Test sign out from user menu

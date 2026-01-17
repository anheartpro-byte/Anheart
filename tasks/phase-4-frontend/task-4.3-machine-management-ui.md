# Task 4.3: Machine Management UI

## Objective

Create UI for managing Raspberry Pi machines including list, create, edit, and view details.

## Dependencies

- Task 4.2 (Dashboard Layout) completed

---

## Acceptance Criteria

### Machine List Page

- [ ] Display all accessible machines in table/grid
- [ ] Show status indicator (online/offline/in-session)
- [ ] Show last heartbeat time (relative)
- [ ] Filter by status
- [ ] Search by name
- [ ] Click to view details
- [ ] "New Machine" button (gestionnaire/admin only)

### Create Machine Page

- [ ] Form with name, location, configuration
- [ ] Submit creates machine and shows API key ONCE
- [ ] Modal with API key and copy button
- [ ] Warning that key won't be shown again
- [ ] Redirect to machine list after close

### Machine Detail Page

- [ ] Display all machine info
- [ ] Edit name, location, config
- [ ] Regenerate API key button (with confirmation)
- [ ] View recent heartbeats
- [ ] View active/recent sessions
- [ ] Delete button (with confirmation)

### Components

- [ ] `MachineStatusBadge` - colored status indicator
- [ ] `MachineCard` - card view option
- [ ] `ApiKeyModal` - one-time API key display
- [ ] `MachineForm` - create/edit form

---

## Implementation

```typescript
// app/[locale]/dashboard/machines/page.tsx
"use client";

import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { MachineStatusBadge } from "@/components/machines/MachineStatusBadge";
import { formatDistanceToNow } from "date-fns";
import { fr, enUS } from "date-fns/locale";

export default function MachinesPage({
  params,
}: {
  params: { locale: string };
}) {
  const t = useTranslations("machines");
  const machines = useQuery(api.machines.listMachines, {});
  const user = useQuery(api.users.getCurrentUser);

  const dateLocale = params.locale === "fr" ? fr : enUS;

  if (machines === undefined) {
    return <div className="animate-pulse">Loading...</div>;
  }

  const canCreate = user?.role === "admin" || user?.role === "gestionnaire";

  return (
    <div>
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-2xl font-bold">{t("title")}</h1>
        {canCreate && (
          <Link
            href={`/${params.locale}/dashboard/machines/new`}
            className="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700"
          >
            {t("create")}
          </Link>
        )}
      </div>

      {machines.length === 0 ? (
        <div className="text-center py-12 text-gray-500">
          No machines found
        </div>
      ) : (
        <div className="bg-white dark:bg-gray-800 shadow rounded-lg overflow-hidden">
          <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
            <thead className="bg-gray-50 dark:bg-gray-900">
              <tr>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                  {t("name")}
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                  {t("status")}
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                  {t("location")}
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                  {t("lastHeartbeat")}
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
              {machines.map((machine) => (
                <tr
                  key={machine._id}
                  className="hover:bg-gray-50 dark:hover:bg-gray-700 cursor-pointer"
                >
                  <td className="px-6 py-4">
                    <Link
                      href={`/${params.locale}/dashboard/machines/${machine._id}`}
                      className="text-blue-600 hover:underline font-medium"
                    >
                      {machine.name}
                    </Link>
                  </td>
                  <td className="px-6 py-4">
                    <MachineStatusBadge status={machine.status} />
                  </td>
                  <td className="px-6 py-4 text-gray-500">
                    {machine.location || "-"}
                  </td>
                  <td className="px-6 py-4 text-gray-500 text-sm">
                    {machine.lastHeartbeat > 0
                      ? formatDistanceToNow(machine.lastHeartbeat, {
                          addSuffix: true,
                          locale: dateLocale,
                        })
                      : "Never"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
```

```typescript
// components/machines/MachineStatusBadge.tsx
"use client";

import { useTranslations } from "next-intl";

type Status = "online" | "offline" | "in_session";

const statusStyles: Record<Status, string> = {
  online: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
  offline: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200",
  in_session: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
};

export function MachineStatusBadge({ status }: { status: string }) {
  const t = useTranslations("machines");

  const statusKey = status as Status;
  const label = status === "in_session" ? t("inSession") : t(statusKey);
  const style = statusStyles[statusKey] || statusStyles.offline;

  return (
    <span className={`px-2 py-1 rounded-full text-xs font-medium ${style}`}>
      {label}
    </span>
  );
}
```

```typescript
// components/machines/ApiKeyModal.tsx
"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";

export function ApiKeyModal({
  apiKey,
  onClose,
}: {
  apiKey: string;
  onClose: () => void;
}) {
  const t = useTranslations("machines");
  const [copied, setCopied] = useState(false);

  const copyToClipboard = async () => {
    await navigator.clipboard.writeText(apiKey);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-white dark:bg-gray-800 rounded-lg p-6 max-w-lg w-full mx-4">
        <h2 className="text-xl font-bold mb-4">{t("apiKey")}</h2>

        <div className="bg-yellow-50 dark:bg-yellow-900 border border-yellow-200 dark:border-yellow-700 rounded-md p-4 mb-4">
          <p className="text-yellow-800 dark:text-yellow-200 text-sm">
            {t("apiKeyWarning")}
          </p>
        </div>

        <div className="bg-gray-100 dark:bg-gray-900 rounded-md p-4 font-mono text-sm break-all">
          {apiKey}
        </div>

        <div className="flex gap-3 mt-6">
          <button
            onClick={copyToClipboard}
            className="flex-1 bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700"
          >
            {copied ? "Copied!" : "Copy"}
          </button>
          <button
            onClick={onClose}
            className="flex-1 bg-gray-200 dark:bg-gray-700 px-4 py-2 rounded-md hover:bg-gray-300"
          >
            {t("close")}
          </button>
        </div>
      </div>
    </div>
  );
}
```

---

## Testing Steps

1. View machines list as gestionnaire - see own machines
2. View as admin - see all machines
3. Create new machine - verify API key shown
4. Copy API key - verify clipboard works
5. Close modal - verify redirect to list
6. View machine details - see all info
7. Edit machine - verify changes saved
8. Regenerate API key - verify confirmation required
9. Delete machine - verify confirmation and removal

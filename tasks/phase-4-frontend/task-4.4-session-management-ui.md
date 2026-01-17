# Task 4.4: Session Management UI

## Objective

Create UI for managing ECG recording sessions including starting sessions, live monitoring, and viewing history.

## Dependencies

- Task 4.2 (Dashboard Layout) completed
- Task 4.3 (Machine Management) completed

---

## Acceptance Criteria

### Session List Page

- [ ] Show all accessible sessions
- [ ] Status filter (active, completed, failed)
- [ ] Search by patient name
- [ ] Sort by date (newest first)
- [ ] Show patient name, machine, status, duration
- [ ] Quick actions: view live, view report

### Create Session Page

- [ ] Machine selector (only online machines)
- [ ] Patient selector (only own patients for gestionnaire)
- [ ] Channel selection (ECG, EMG, etc.)
- [ ] Notes field
- [ ] Start session button
- [ ] Redirect to live view after creation

### Session Detail Page

- [ ] Patient and machine info
- [ ] Session status and duration
- [ ] Live ECG view (if active)
- [ ] Summary metrics (if completed)
- [ ] Notes (editable before completion)
- [ ] End session button (if active)
- [ ] Download report (if completed)

### Active Sessions Widget

- [ ] Dashboard widget showing active sessions
- [ ] Quick access to live view
- [ ] Real-time status updates

---

## Implementation

```typescript
// app/[locale]/dashboard/sessions/page.tsx
"use client";

import { useState } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { formatDistanceToNow, format } from "date-fns";
import { fr, enUS } from "date-fns/locale";

type StatusFilter = "all" | "active" | "completed" | "failed";

export default function SessionsPage({
  params,
}: {
  params: { locale: string };
}) {
  const t = useTranslations("sessions");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");

  const sessions = useQuery(api.sessions.listSessions, {
    status: statusFilter === "all" ? undefined : statusFilter,
    limit: 50,
  });

  const user = useQuery(api.users.getCurrentUser);
  const dateLocale = params.locale === "fr" ? fr : enUS;

  const canCreate =
    user?.role === "admin" ||
    user?.role === "gestionnaire" ||
    user?.role === "technician";

  if (sessions === undefined) {
    return <div className="animate-pulse">Loading...</div>;
  }

  return (
    <div>
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-2xl font-bold">{t("title")}</h1>
        {canCreate && (
          <Link
            href={`/${params.locale}/dashboard/sessions/new`}
            className="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700"
          >
            {t("create")}
          </Link>
        )}
      </div>

      {/* Status Filter Tabs */}
      <div className="flex gap-2 mb-4">
        {(["all", "active", "completed", "failed"] as const).map((status) => (
          <button
            key={status}
            onClick={() => setStatusFilter(status)}
            className={`px-4 py-2 rounded-md ${
              statusFilter === status
                ? "bg-blue-600 text-white"
                : "bg-gray-200 dark:bg-gray-700 hover:bg-gray-300"
            }`}
          >
            {status === "all" ? "All" : t(status)}
          </button>
        ))}
      </div>

      {/* Sessions Table */}
      <div className="bg-white dark:bg-gray-800 shadow rounded-lg overflow-hidden">
        <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
          <thead className="bg-gray-50 dark:bg-gray-900">
            <tr>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                Patient
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                Machine
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                Status
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                {t("startedAt")}
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                {t("duration")}
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                Actions
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
            {sessions.map((session) => (
              <tr key={session._id}>
                <td className="px-6 py-4 font-medium">
                  {session.patientName}
                </td>
                <td className="px-6 py-4 text-gray-500">
                  {session.machineName}
                </td>
                <td className="px-6 py-4">
                  <SessionStatusBadge status={session.status} />
                </td>
                <td className="px-6 py-4 text-sm text-gray-500">
                  {format(session.startedAt, "PPp", { locale: dateLocale })}
                </td>
                <td className="px-6 py-4 text-sm text-gray-500">
                  {session.endedAt
                    ? formatDuration(session.endedAt - session.startedAt)
                    : formatDistanceToNow(session.startedAt, {
                        locale: dateLocale,
                      })}
                </td>
                <td className="px-6 py-4">
                  {session.status === "active" ? (
                    <Link
                      href={`/${params.locale}/dashboard/sessions/${session._id}/live`}
                      className="text-blue-600 hover:underline"
                    >
                      {t("viewLive")}
                    </Link>
                  ) : session.status === "completed" ? (
                    <Link
                      href={`/${params.locale}/dashboard/sessions/${session._id}`}
                      className="text-blue-600 hover:underline"
                    >
                      {t("viewReport")}
                    </Link>
                  ) : (
                    <span className="text-gray-400">-</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SessionStatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    pending: "bg-yellow-100 text-yellow-800",
    active: "bg-green-100 text-green-800",
    completed: "bg-blue-100 text-blue-800",
    failed: "bg-red-100 text-red-800",
  };

  return (
    <span className={`px-2 py-1 rounded-full text-xs font-medium ${styles[status] || styles.pending}`}>
      {status}
    </span>
  );
}

function formatDuration(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);

  if (hours > 0) {
    return `${hours}h ${minutes % 60}m`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds % 60}s`;
  }
  return `${seconds}s`;
}
```

```typescript
// app/[locale]/dashboard/sessions/new/page.tsx
"use client";

import { useState } from "react";
import { useQuery, useMutation } from "convex/react";
import { api } from "@/convex/_generated/api";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { Id } from "@/convex/_generated/dataModel";

export default function NewSessionPage({
  params,
}: {
  params: { locale: string };
}) {
  const t = useTranslations("sessions");
  const router = useRouter();

  const [machineId, setMachineId] = useState<Id<"machines"> | "">("");
  const [patientId, setPatientId] = useState<Id<"users"> | "">("");
  const [channels, setChannels] = useState<string[]>(["ECG"]);
  const [notes, setNotes] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const machines = useQuery(api.machines.listMachines, { status: "online" });
  const patients = useQuery(api.users.listUsers, { role: "user" });
  const createSession = useMutation(api.sessions.createSession);

  const availableChannels = ["ECG", "EMG", "EDA", "EEG", "ACC", "LUX"];

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!machineId || !patientId) {
      setError("Please select machine and patient");
      return;
    }

    setLoading(true);
    setError("");

    try {
      const sessionId = await createSession({
        machineId: machineId as Id<"machines">,
        userId: patientId as Id<"users">,
        channels,
        notes: notes || undefined,
      });

      router.push(`/${params.locale}/dashboard/sessions/${sessionId}/live`);
    } catch (err: any) {
      setError(err.message);
      setLoading(false);
    }
  };

  const toggleChannel = (channel: string) => {
    if (channels.includes(channel)) {
      setChannels(channels.filter((c) => c !== channel));
    } else {
      setChannels([...channels, channel]);
    }
  };

  return (
    <div className="max-w-2xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">{t("create")}</h1>

      <form onSubmit={handleSubmit} className="space-y-6">
        {error && (
          <div className="bg-red-50 text-red-600 p-4 rounded-md">
            {error}
          </div>
        )}

        {/* Machine Selection */}
        <div>
          <label className="block text-sm font-medium mb-2">
            {t("selectMachine")}
          </label>
          <select
            value={machineId}
            onChange={(e) => setMachineId(e.target.value as any)}
            className="w-full border rounded-md p-2"
            required
          >
            <option value="">-- Select --</option>
            {machines?.map((machine) => (
              <option key={machine._id} value={machine._id}>
                {machine.name} {machine.location && `(${machine.location})`}
              </option>
            ))}
          </select>
        </div>

        {/* Patient Selection */}
        <div>
          <label className="block text-sm font-medium mb-2">
            {t("selectPatient")}
          </label>
          <select
            value={patientId}
            onChange={(e) => setPatientId(e.target.value as any)}
            className="w-full border rounded-md p-2"
            required
          >
            <option value="">-- Select --</option>
            {patients?.map((patient) => (
              <option key={patient._id} value={patient._id}>
                {patient.firstName} {patient.lastName} ({patient.email})
              </option>
            ))}
          </select>
        </div>

        {/* Channel Selection */}
        <div>
          <label className="block text-sm font-medium mb-2">
            {t("selectChannels")}
          </label>
          <div className="flex flex-wrap gap-2">
            {availableChannels.map((channel) => (
              <button
                key={channel}
                type="button"
                onClick={() => toggleChannel(channel)}
                className={`px-3 py-1 rounded-md ${
                  channels.includes(channel)
                    ? "bg-blue-600 text-white"
                    : "bg-gray-200"
                }`}
              >
                {channel}
              </button>
            ))}
          </div>
        </div>

        {/* Notes */}
        <div>
          <label className="block text-sm font-medium mb-2">
            {t("notes")}
          </label>
          <textarea
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            className="w-full border rounded-md p-2"
            rows={3}
            placeholder="Optional notes..."
          />
        </div>

        {/* Submit */}
        <button
          type="submit"
          disabled={loading}
          className="w-full bg-blue-600 text-white py-2 rounded-md hover:bg-blue-700 disabled:opacity-50"
        >
          {loading ? "Creating..." : t("start")}
        </button>
      </form>
    </div>
  );
}
```

---

## Testing Steps

1. View sessions list - verify filtering works
2. Create new session with online machine
3. Verify redirect to live view
4. Try creating session with offline machine - verify error
5. View active session - see live ECG
6. End session - verify status changes
7. View completed session - see summary
8. Test as patient - can only see own sessions

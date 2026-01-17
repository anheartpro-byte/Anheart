# Task 4.6: User Management UI

## Objective

Create UI for user management including listing users, creating patients, and viewing profiles.

## Dependencies

- Task 4.2 (Dashboard Layout) completed

---

## Acceptance Criteria

### User List Page

- [ ] Admin sees all users with role filter
- [ ] Gestionnaire sees only their patients
- [ ] Search by name/email
- [ ] Role badge display
- [ ] Click to view profile
- [ ] "New Patient" button for gestionnaire

### Create Patient Page

- [ ] First name, last name, email fields
- [ ] Language selection (French/English)
- [ ] Form validation
- [ ] Success message with next steps

### User Profile Page

- [ ] Display user info
- [ ] Role badge
- [ ] Session history for patients
- [ ] Edit button for own profile
- [ ] Delete button (with confirmation)

### Edit Profile Page

- [ ] Edit own name, language
- [ ] Admin can edit anyone's role

---

## Implementation

```typescript
// app/[locale]/dashboard/users/page.tsx (Admin view)
"use client";

import { useState } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { RoleGuard } from "@/components/RoleGuard";

export default function UsersPage({ params }: { params: { locale: string } }) {
  const t = useTranslations("users");
  const [roleFilter, setRoleFilter] = useState<string>("");
  const [search, setSearch] = useState("");

  const users = useQuery(api.users.listUsers, {
    role: roleFilter || undefined,
  });

  const filteredUsers = users?.filter(
    (u) =>
      u.firstName.toLowerCase().includes(search.toLowerCase()) ||
      u.lastName.toLowerCase().includes(search.toLowerCase()) ||
      u.email.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <RoleGuard allowedRoles={["admin"]} locale={params.locale}>
      <div>
        <div className="flex justify-between items-center mb-6">
          <h1 className="text-2xl font-bold">{t("title")}</h1>
        </div>

        {/* Filters */}
        <div className="flex gap-4 mb-4">
          <input
            type="text"
            placeholder={t("search")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="border rounded-md px-4 py-2 flex-1 max-w-xs"
          />
          <select
            value={roleFilter}
            onChange={(e) => setRoleFilter(e.target.value)}
            className="border rounded-md px-4 py-2"
          >
            <option value="">All Roles</option>
            <option value="admin">{t("roles.admin")}</option>
            <option value="gestionnaire">{t("roles.gestionnaire")}</option>
            <option value="technician">{t("roles.technician")}</option>
            <option value="user">{t("roles.user")}</option>
          </select>
        </div>

        {/* Users Table */}
        <div className="bg-white dark:bg-gray-800 shadow rounded-lg overflow-hidden">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50 dark:bg-gray-900">
              <tr>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                  Name
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                  {t("email")}
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                  {t("role")}
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                  {t("language")}
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {filteredUsers?.map((user) => (
                <tr key={user._id} className="hover:bg-gray-50">
                  <td className="px-6 py-4">
                    <Link
                      href={`/${params.locale}/dashboard/users/${user._id}`}
                      className="text-blue-600 hover:underline font-medium"
                    >
                      {user.firstName} {user.lastName}
                    </Link>
                  </td>
                  <td className="px-6 py-4 text-gray-500">{user.email}</td>
                  <td className="px-6 py-4">
                    <RoleBadge role={user.role} />
                  </td>
                  <td className="px-6 py-4 text-gray-500 uppercase">
                    {user.language}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </RoleGuard>
  );
}

function RoleBadge({ role }: { role: string }) {
  const colors: Record<string, string> = {
    admin: "bg-purple-100 text-purple-800",
    gestionnaire: "bg-blue-100 text-blue-800",
    technician: "bg-green-100 text-green-800",
    user: "bg-gray-100 text-gray-800",
  };

  return (
    <span className={`px-2 py-1 rounded-full text-xs font-medium ${colors[role]}`}>
      {role}
    </span>
  );
}
```

```typescript
// app/[locale]/dashboard/patients/page.tsx (Gestionnaire view)
"use client";

import { useState } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { RoleGuard } from "@/components/RoleGuard";

export default function PatientsPage({ params }: { params: { locale: string } }) {
  const t = useTranslations("users");
  const [search, setSearch] = useState("");

  const patients = useQuery(api.users.listUsers, { role: "user" });

  const filteredPatients = patients?.filter(
    (p) =>
      p.firstName.toLowerCase().includes(search.toLowerCase()) ||
      p.lastName.toLowerCase().includes(search.toLowerCase()) ||
      p.email.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <RoleGuard allowedRoles={["gestionnaire"]} locale={params.locale}>
      <div>
        <div className="flex justify-between items-center mb-6">
          <h1 className="text-2xl font-bold">{t("patients")}</h1>
          <Link
            href={`/${params.locale}/dashboard/patients/new`}
            className="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700"
          >
            {t("createPatient")}
          </Link>
        </div>

        <input
          type="text"
          placeholder={t("search")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="border rounded-md px-4 py-2 mb-4 w-full max-w-xs"
        />

        {/* Patients Grid */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filteredPatients?.map((patient) => (
            <Link
              key={patient._id}
              href={`/${params.locale}/dashboard/patients/${patient._id}`}
              className="bg-white dark:bg-gray-800 rounded-lg shadow p-4 hover:shadow-md transition"
            >
              <h3 className="font-medium">
                {patient.firstName} {patient.lastName}
              </h3>
              <p className="text-sm text-gray-500">{patient.email}</p>
            </Link>
          ))}
        </div>

        {filteredPatients?.length === 0 && (
          <p className="text-center text-gray-500 py-12">
            No patients found
          </p>
        )}
      </div>
    </RoleGuard>
  );
}
```

```typescript
// app/[locale]/dashboard/patients/new/page.tsx
"use client";

import { useState } from "react";
import { useMutation } from "convex/react";
import { api } from "@/convex/_generated/api";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { RoleGuard } from "@/components/RoleGuard";

export default function NewPatientPage({ params }: { params: { locale: string } }) {
  const t = useTranslations("users");
  const router = useRouter();

  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [email, setEmail] = useState("");
  const [language, setLanguage] = useState<"fr" | "en">("fr");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const createPatient = useMutation(api.users.createPatient);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");

    try {
      await createPatient({
        firstName,
        lastName,
        email,
        language,
      });

      router.push(`/${params.locale}/dashboard/patients`);
    } catch (err: any) {
      setError(err.message);
      setLoading(false);
    }
  };

  return (
    <RoleGuard allowedRoles={["admin", "gestionnaire"]} locale={params.locale}>
      <div className="max-w-md mx-auto">
        <h1 className="text-2xl font-bold mb-6">{t("createPatient")}</h1>

        <form onSubmit={handleSubmit} className="space-y-4">
          {error && (
            <div className="bg-red-50 text-red-600 p-4 rounded-md">
              {error}
            </div>
          )}

          <div>
            <label className="block text-sm font-medium mb-1">
              {t("firstName")}
            </label>
            <input
              type="text"
              value={firstName}
              onChange={(e) => setFirstName(e.target.value)}
              className="w-full border rounded-md p-2"
              required
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">
              {t("lastName")}
            </label>
            <input
              type="text"
              value={lastName}
              onChange={(e) => setLastName(e.target.value)}
              className="w-full border rounded-md p-2"
              required
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">
              {t("email")}
            </label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full border rounded-md p-2"
              required
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">
              {t("language")}
            </label>
            <select
              value={language}
              onChange={(e) => setLanguage(e.target.value as "fr" | "en")}
              className="w-full border rounded-md p-2"
            >
              <option value="fr">Français</option>
              <option value="en">English</option>
            </select>
          </div>

          <button
            type="submit"
            disabled={loading}
            className="w-full bg-blue-600 text-white py-2 rounded-md hover:bg-blue-700 disabled:opacity-50"
          >
            {loading ? "Creating..." : t("create")}
          </button>
        </form>
      </div>
    </RoleGuard>
  );
}
```

---

## Testing Steps

1. Login as admin - view all users page
2. Filter by role - verify filtering works
3. Search by name - verify results filter
4. Login as gestionnaire - view patients page
5. Create new patient - verify success
6. Try duplicate email - verify error
7. View patient profile - see session history
8. Edit own profile - verify changes saved

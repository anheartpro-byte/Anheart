# Task 4.1: Internationalization Setup

## Objective

Set up French and English language support using next-intl with the App Router.

## Dependencies

None - can start immediately

---

## Acceptance Criteria

### Setup

- [ ] `next-intl` installed
- [ ] Middleware configured for locale detection
- [ ] Default locale is French (fr)
- [ ] Supported locales: fr, en
- [ ] URL structure: `/fr/dashboard`, `/en/dashboard`

### Message Files

- [ ] `messages/fr.json` created with all UI strings
- [ ] `messages/en.json` created with all UI strings
- [ ] Nested structure for organization (common, dashboard, sessions, etc.)

### Components

- [ ] `useTranslations` hook works in client components
- [ ] `getTranslations` works in server components
- [ ] Language switcher component created
- [ ] User language preference saved to database

### Routing

- [ ] `/` redirects to `/fr` (default locale)
- [ ] Invalid locale redirects to default
- [ ] Locale persists in navigation

---

## Implementation

```bash
npm install next-intl
```

```typescript
// middleware.ts
import createMiddleware from "next-intl/middleware";

export default createMiddleware({
  locales: ["fr", "en"],
  defaultLocale: "fr",
  localePrefix: "always",
});

export const config = {
  matcher: ["/", "/(fr|en)/:path*"],
};
```

```typescript
// i18n.ts
import { getRequestConfig } from "next-intl/server";

export default getRequestConfig(async ({ locale }) => ({
  messages: (await import(`./messages/${locale}.json`)).default,
}));
```

```json
// messages/fr.json
{
  "common": {
    "loading": "Chargement...",
    "error": "Une erreur est survenue",
    "save": "Enregistrer",
    "cancel": "Annuler",
    "delete": "Supprimer",
    "edit": "Modifier",
    "create": "Créer",
    "search": "Rechercher",
    "noResults": "Aucun résultat",
    "confirm": "Confirmer",
    "back": "Retour"
  },
  "auth": {
    "signIn": "Se connecter",
    "signOut": "Se déconnecter",
    "signUp": "S'inscrire"
  },
  "nav": {
    "dashboard": "Tableau de bord",
    "machines": "Machines",
    "sessions": "Sessions",
    "users": "Utilisateurs",
    "patients": "Patients",
    "reports": "Rapports",
    "settings": "Paramètres"
  },
  "dashboard": {
    "title": "Tableau de bord",
    "welcome": "Bienvenue, {name}",
    "activeSessions": "Sessions actives",
    "onlineMachines": "Machines en ligne",
    "totalPatients": "Patients total"
  },
  "machines": {
    "title": "Gestion des machines",
    "create": "Nouvelle machine",
    "name": "Nom de la machine",
    "location": "Emplacement",
    "status": "Statut",
    "online": "En ligne",
    "offline": "Hors ligne",
    "inSession": "En session",
    "lastHeartbeat": "Dernier signal",
    "apiKey": "Clé API",
    "apiKeyWarning": "Cette clé ne sera affichée qu'une seule fois!",
    "regenerateKey": "Régénérer la clé",
    "config": "Configuration",
    "sampleRate": "Fréquence d'échantillonnage",
    "channels": "Canaux"
  },
  "sessions": {
    "title": "Sessions ECG",
    "create": "Nouvelle session",
    "active": "Sessions actives",
    "completed": "Sessions terminées",
    "failed": "Sessions échouées",
    "selectMachine": "Sélectionner une machine",
    "selectPatient": "Sélectionner un patient",
    "selectChannels": "Canaux à enregistrer",
    "start": "Démarrer la session",
    "end": "Terminer la session",
    "duration": "Durée",
    "startedAt": "Démarrée à",
    "endedAt": "Terminée à",
    "notes": "Notes",
    "viewLive": "Voir en direct",
    "viewReport": "Voir le rapport"
  },
  "ecg": {
    "liveView": "Vue en direct",
    "delayedView": "Vue différée (5s)",
    "heartRate": "Fréquence cardiaque",
    "bpm": "BPM",
    "avgHeartRate": "FC moyenne",
    "minHeartRate": "FC minimum",
    "maxHeartRate": "FC maximum",
    "hrv": "Variabilité cardiaque (HRV)"
  },
  "users": {
    "title": "Gestion des utilisateurs",
    "createPatient": "Nouveau patient",
    "firstName": "Prénom",
    "lastName": "Nom",
    "email": "Email",
    "role": "Rôle",
    "language": "Langue",
    "createdAt": "Créé le",
    "roles": {
      "admin": "Administrateur",
      "gestionnaire": "Gestionnaire",
      "technician": "Technicien",
      "user": "Patient"
    }
  },
  "reports": {
    "title": "Rapport de session",
    "download": "Télécharger PDF",
    "generating": "Génération en cours...",
    "patientInfo": "Informations patient",
    "sessionInfo": "Informations session",
    "metrics": "Métriques",
    "ecgOverview": "Aperçu ECG"
  }
}
```

```json
// messages/en.json
{
  "common": {
    "loading": "Loading...",
    "error": "An error occurred",
    "save": "Save",
    "cancel": "Cancel",
    "delete": "Delete",
    "edit": "Edit",
    "create": "Create",
    "search": "Search",
    "noResults": "No results",
    "confirm": "Confirm",
    "back": "Back"
  },
  "auth": {
    "signIn": "Sign in",
    "signOut": "Sign out",
    "signUp": "Sign up"
  },
  "nav": {
    "dashboard": "Dashboard",
    "machines": "Machines",
    "sessions": "Sessions",
    "users": "Users",
    "patients": "Patients",
    "reports": "Reports",
    "settings": "Settings"
  },
  "dashboard": {
    "title": "Dashboard",
    "welcome": "Welcome, {name}",
    "activeSessions": "Active sessions",
    "onlineMachines": "Online machines",
    "totalPatients": "Total patients"
  },
  "machines": {
    "title": "Machine Management",
    "create": "New Machine",
    "name": "Machine Name",
    "location": "Location",
    "status": "Status",
    "online": "Online",
    "offline": "Offline",
    "inSession": "In Session",
    "lastHeartbeat": "Last Heartbeat",
    "apiKey": "API Key",
    "apiKeyWarning": "This key will only be shown once!",
    "regenerateKey": "Regenerate Key",
    "config": "Configuration",
    "sampleRate": "Sample Rate",
    "channels": "Channels"
  },
  "sessions": {
    "title": "ECG Sessions",
    "create": "New Session",
    "active": "Active Sessions",
    "completed": "Completed Sessions",
    "failed": "Failed Sessions",
    "selectMachine": "Select Machine",
    "selectPatient": "Select Patient",
    "selectChannels": "Channels to Record",
    "start": "Start Session",
    "end": "End Session",
    "duration": "Duration",
    "startedAt": "Started At",
    "endedAt": "Ended At",
    "notes": "Notes",
    "viewLive": "View Live",
    "viewReport": "View Report"
  },
  "ecg": {
    "liveView": "Live View",
    "delayedView": "Delayed View (5s)",
    "heartRate": "Heart Rate",
    "bpm": "BPM",
    "avgHeartRate": "Avg Heart Rate",
    "minHeartRate": "Min Heart Rate",
    "maxHeartRate": "Max Heart Rate",
    "hrv": "Heart Rate Variability (HRV)"
  },
  "users": {
    "title": "User Management",
    "createPatient": "New Patient",
    "firstName": "First Name",
    "lastName": "Last Name",
    "email": "Email",
    "role": "Role",
    "language": "Language",
    "createdAt": "Created",
    "roles": {
      "admin": "Administrator",
      "gestionnaire": "Manager",
      "technician": "Technician",
      "user": "Patient"
    }
  },
  "reports": {
    "title": "Session Report",
    "download": "Download PDF",
    "generating": "Generating...",
    "patientInfo": "Patient Information",
    "sessionInfo": "Session Information",
    "metrics": "Metrics",
    "ecgOverview": "ECG Overview"
  }
}
```

```typescript
// components/LanguageSwitcher.tsx
"use client";

import { useLocale } from "next-intl";
import { useRouter, usePathname } from "next/navigation";

export function LanguageSwitcher() {
  const locale = useLocale();
  const router = useRouter();
  const pathname = usePathname();

  const switchLocale = (newLocale: string) => {
    const newPath = pathname.replace(`/${locale}`, `/${newLocale}`);
    router.push(newPath);
  };

  return (
    <div className="flex gap-2">
      <button
        onClick={() => switchLocale("fr")}
        className={`px-2 py-1 rounded ${
          locale === "fr" ? "bg-blue-500 text-white" : "bg-gray-200"
        }`}
      >
        FR
      </button>
      <button
        onClick={() => switchLocale("en")}
        className={`px-2 py-1 rounded ${
          locale === "en" ? "bg-blue-500 text-white" : "bg-gray-200"
        }`}
      >
        EN
      </button>
    </div>
  );
}
```

---

## Testing Steps

1. Navigate to `/` - verify redirect to `/fr`
2. Navigate to `/fr/dashboard` - verify French strings
3. Click EN language switch - verify URL changes to `/en/dashboard`
4. Verify all strings translated
5. Refresh page - verify locale persists
6. Navigate to invalid locale `/de/dashboard` - verify redirect to default

---

## Notes

- All UI strings should be in message files, not hardcoded
- Date/number formatting should also be localized
- User preference should be saved when they switch language

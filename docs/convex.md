# Convex : base de données et fonctions distantes

Convex (`convex/`) est la couche distante d'Anheart. Il stocke les comptes, les
machines, les séances et la télémétrie. Il sert deux clients :

- le **site Next.js** (voir [tableau-de-bord.md](tableau-de-bord.md)), par des
  *queries* et *mutations* publiques, authentifiées par Clerk ;
- le **Raspberry Pi** (voir [raspberry-pi.md](raspberry-pi.md)), par des
  **routes HTTP** authentifiées par la clé API de la machine.

> **État réel.** Le code Convex compile (`npx tsc --noEmit -p convex` : 0 erreur).
> Une **version précédente** de Convex est déjà déployée et sert le site en
> production. Le code décrit ici (nouveau schéma, `convex/training.ts`, routes
> machine) est déployé depuis le 1er octobre 2026 sur le déploiement de
> **développement** (`standing-jay-887`), où il a été testé de bout en bout avec
> une console de Pi en simulation : voir
> [deploiement.md](deploiement.md#4-essai-de-bout-en-bout-du-1er-octobre-2026).
> Il n'est **pas** en production (ticket ANH-82), et n'a jamais été appelé par
> un vrai Pi. Il n'existe **aucun test automatisé** côté Convex. Les fichiers `convex/training.ts`, `lib/training.ts`,
> `components/training/` et la page `my-machines` ne sont pas encore commités
> (état `git status` au moment de la rédaction).

Sommaire :

1. [Principe : le Pi décide, Convex reflète](#1-principe--le-pi-décide-convex-reflète)
2. [Le schéma](#2-le-schéma)
3. [Règles d'autorisation](#3-règles-dautorisation)
4. [Fonctions de `training.ts`](#4-fonctions-de-trainingts)
5. [Autres fonctions publiques](#5-autres-fonctions-publiques)
6. [Routes HTTP machine (`convex/http.ts`)](#6-routes-http-machine-convexhttpts)
7. [Tâche planifiée](#7-tâche-planifiée)
8. [Déployer](#8-déployer)
9. [Défauts connus et reste à faire](#9-défauts-connus-et-reste-à-faire)

---

## 1. Principe : le Pi décide, Convex reflète

- Le Pi est **l'autorité** sur la machine et la sécurité. Convex ne commande
  jamais le moteur.
- Convex vérifie les droits et la physiologie **avant** d'accepter un lancement,
  pour qu'un utilisateur du site ait un refus immédiat. Le Pi **revérifie tout**
  et peut encore refuser (câblage d'arrêt d'urgence non attesté, verdict de
  sécurité en cours, défaut variateur, programmes désactivés…). Un refus revient
  comme une séance `failed` avec la raison du Pi.
- Deux types de séance d'entraînement :
  - **auto** : un programme enregistré sur le Pi ; la fréquence cardiaque pilote
    la vitesse. C'est **le seul** type qu'on peut lancer depuis le site.
  - **manual** : l'opérateur fixe la vitesse. **Aucune mutation Convex ne crée
    une séance manuelle.** Le Pi l'enregistre après l'avoir démarrée lui-même
    (`/api/machine/training/local`), pour que le site l'affiche.
- Le troisième type, `recording`, est l'ancien mode « enregistrement ECG seul »
  du client `src/main.py`. Il est conservé mais n'est pas utilisé par la console
  locale.

Voir le [glossaire](glossaire.md) pour « zone », « palier hard max / critique ».

---

## 2. Le schéma

Fichier : `convex/schema.ts`.

### `users` : comptes

| Champ | Type | Rôle |
|---|---|---|
| `clerkId` | string | Identifiant Clerk (`identity.subject`). Chaîne vide pour un patient créé par un gestionnaire et pas encore lié. |
| `role` | `"admin"` \| `"gestionnaire"` \| `"user"` | Rôle. `user` = patient / pratiquant. |
| `gestionnaireId` | id users, optionnel | **Ancien champ**, gardé pour compatibilité ; remplacé par la table `user_gestionnaires`. |
| `firstName`, `lastName`, `email` | string | Identité. |
| `language` | `"fr"` \| `"en"` | Langue. |
| `hrMax` | number, optionnel | FC max **mesurée** (bpm). Prioritaire sur l'estimation. |
| `birthYear` | number, optionnel | Année de naissance. Sert à l'estimation Tanaka et au contrôle d'âge. |
| `createdAt` | number | ms Unix. |

Index : `by_clerk_id`, `by_gestionnaire`, `by_role`.

### `user_gestionnaires` : quel gestionnaire suit quel patient

Relation plusieurs-à-plusieurs : `userId` (patient), `gestionnaireId`,
`createdAt`, `createdBy`. Index `by_user`, `by_gestionnaire`,
`by_user_and_gestionnaire`.

### `machine_gestionnaires` : quel gestionnaire gère quelle machine

`machineId`, `gestionnaireId`, `isOwner` (le premier gestionnaire assigné à la
création), `createdAt`, `createdBy`. Index `by_machine`, `by_gestionnaire`,
`by_machine_and_gestionnaire`.

### `machine_user_permissions` : droits de lancement

Quel patient (`userId`) peut lancer **lui-même** une séance auto sur quelle
machine (`machineId`). `grantedBy`, `createdAt`. Index `by_machine`, `by_user`,
`by_machine_and_user`. Voir [§3](#3-règles-dautorisation).

### `machine_profiles` : programmes synchronisés depuis le Pi

Copie en lecture seule du `ProfileStore` du Pi. **Remplacée en bloc** à chaque
synchronisation (`syncProfiles`).

| Champ | Rôle |
|---|---|
| `machineId` | Machine d'origine. |
| `profileId`, `name` | Identifiant et nom du programme. |
| `totalDurationS` | Durée totale (s). |
| `zoneLowBpm`, `zoneHighBpm` | Zone cible (bpm). |
| `hardMaxBpm`, `criticalBpm` | Paliers de sécurité cardiaque du programme. |
| `subjectHrMax` | FC max pour laquelle le programme est écrit. |
| `minRunRpm`, `maxRpm` | Vitesse minimale de marche et plafond, en **tr/min moteur**. |
| `storeRev`, `updatedAt` | Révision du magasin côté Pi ; date de réception. |

Le Pi n'envoie que les programmes dont les paliers égalent ceux de son
superviseur (`runnable_profiles` dans `raspberry-pi/src/cloud_sync.py`) : un
programme que le Pi refuserait n'est jamais proposé sur le site.

### `machines`

| Champ | Rôle |
|---|---|
| `name`, `location` | Nom, lieu. |
| `apiKey` | Clé API **transformée** (voir [§9](#9-défauts-connus-et-reste-à-faire) : ce n'est pas un vrai hachage). |
| `status` | `"online"` \| `"offline"` \| `"in_session"`. |
| `lastHeartbeat` | ms Unix du dernier heartbeat. |
| `config` | `{ sampleRate, channels, batchInterval }` (défaut `1000`, `["ECG"]`, `1000`). Hérité du mode enregistrement. |
| `isDeleted`, `deletedAt`, `deletedBy` | Suppression douce. |
| `programsEnabled` | Rapporté par le Pi : accepte-t-il les séances auto ? |
| `live` | Dernier état rapporté par le Pi (voir ci-dessous). |

Index : `by_api_key`, `by_status`, `by_is_deleted`.

**`live`** (validateur `liveStateValidator`) :

| Champ | Contenu envoyé par le Pi |
|---|---|
| `runMode` | `repos`, `manuel`, `seance`, `arret`. |
| `phase` | `baseline`, `warmup`, `hold`, `cooldown`, `recovery`, `done`. |
| `bpm` | **Absent** quand le Pi n'a pas de FC fraîche et fiable (jamais une valeur périmée). |
| `motorRpm`, `outputRpm` | Vitesse **mesurée**, arbre moteur et bras (moteur / 49,79). |
| `setpointMotorRpm` | Consigne, tr/min moteur. |
| `gLoad` | Charge g au rayon configuré. |
| `safetyAction` | Nom en minuscules de l'action de sécurité du Pi : `none`, `freeze`, `reduce`, `ramp_down`, `quick_stop`, `go_silent`. |
| `driveState` | État du variateur (minuscules). |
| `sessionId` | Id Convex de la séance en cours, si connu. |
| `updatedAt` | Posé par Convex à la réception. |

### `sessions`

| Champ | Rôle |
|---|---|
| `machineId` | Machine. |
| `userId` | Pratiquant. Toujours présent pour un lancement distant ; **absent** pour une séance démarrée à la machine (le Pi ne l'envoie pas aujourd'hui, voir [§9](#9-défauts-connus-et-reste-à-faire)). |
| `startedById` | Qui a lancé (site). |
| `status` | `pending` → `active` → `completed` ou `failed`. |
| `startedAt`, `endedAt` | ms Unix. |
| `channels`, `sampleRate`, `notes` | Hérités du mode enregistrement. `notes` contient `Occupancy: bench/occupied` pour une séance locale qui déclare l'occupation. |
| `kind` | `recording` \| `auto` \| `manual` (absent = ancien `recording`). |
| `origin` | `remote` (site) \| `local` (machine). |
| `profileId`, `profileName`, `zoneLowBpm`, `zoneHighBpm`, `totalDurationS` | Programme lancé. |
| `subjectHrMax`, `subjectAge`, `subjectLabel` | FC max retenue, âge, nom du pratiquant. |
| `operatorName` | Qui a lancé. |
| `localRef` | Clé d'idempotence côté Pi (séances locales). |
| `stopRequestedAt` | Posé par `requestStop` : le Pi le lit et arrête. |
| `endReason` | Raison de fin donnée par le Pi (ou « Cancelled before start by … »). |

Index : `by_user`, `by_machine`, `by_machine_and_status`,
`by_machine_and_local_ref`, `by_started_by`.

### `training_telemetry` : télémétrie à 1 Hz

`sessionId`, `machineId`, `t` (ms Unix, horloge du Pi), `elapsedS`, `phase`,
`bpm` (absent = pas de FC fiable), `motorRpm`, `outputRpm`, `setpointMotorRpm`,
`gLoad`, `safetyAction`. Index `by_session_and_t`.

### Tables héritées du mode enregistrement ECG

| Table | Contenu |
|---|---|
| `ecg_data` | Lots d'ECG **déjà traité** sur le Pi (`values` en mV à `sampleRate` Hz) et métriques par canal (`heartRate`, `hrv`, `quality`…). Écrit par `/api/machine/data`. **La console locale n'envoie pas ces lots** : seul l'ancien client `src/main.py` le fait. |
| `session_summaries` | Résumé calculé en fin de séance (FC moyenne/min/max, HRV, ECG sous-échantillonné). Calculé à partir de `ecg_data` : sans ECG, aucun résumé n'est produit. |
| `machine_heartbeats` | Historique des heartbeats (`timestamp`, `batteryLevel`, `wifiStrength`, `activeSessionId`). Aucun nettoyage n'est programmé. |

---

## 3. Règles d'autorisation

Fonctions de `convex/lib/auth.ts` :

| Fonction | Vraie si |
|---|---|
| `requireAuth` | Un jeton Clerk valide est présent (sinon `Not authenticated`). |
| `getCurrentUserOrThrow` | Une ligne `users` existe pour ce `clerkId` (sinon « User not found in database. Please complete registration. »). |
| `requireRole(r)` | Le rôle courant est dans `r`. |
| `canAccessUser(cible)` | admin ; ou soi-même ; ou gestionnaire lié à la cible dans `user_gestionnaires`. |
| `canAccessMachine(m)` | admin ; ou gestionnaire lié à `m` dans `machine_gestionnaires`. **Un `user` n'a jamais accès par cette règle.** |
| `canManageMachine(m)` | Identique à `canAccessMachine`. |

Règles propres aux séances d'entraînement (`convex/training.ts`) :

| Action | Qui |
|---|---|
| Voir la disponibilité et les programmes d'une machine | admin, gestionnaire de la machine, ou patient détenant le droit de lancement sur cette machine. |
| Lire ses mesures live | admin, gestionnaire de la machine, ou pratiquant de la séance live sur cette même machine. Un droit de lancement seul ne donne pas accès aux mesures d'un autre pratiquant. |
| **Accorder / retirer un droit de lancement** | admin, ou gestionnaire de la machine. Pour accorder, le gestionnaire doit **aussi** gérer le patient (`canAccessUser`). Seul un `user` peut recevoir le droit : admins et gestionnaires l'ont déjà. |
| **Lancer une séance auto** | un `user` pour **lui-même seulement**, s'il a le droit sur la machine ; un admin ou un gestionnaire de la machine, pour lui-même ou pour un patient qu'il gère. |
| **Arrêter / annuler** | le pratiquant de la séance, ou un admin / gestionnaire de la machine. |
| Régler FC max et année de naissance | admin, ou gestionnaire du patient. **Jamais le patient lui-même.** |
| Lire la télémétrie et le détail d'une séance | le pratiquant, ou admin / gestionnaire de la machine. |

Autres règles notables :

- `listLaunchableMachines` et `getMachineLive` gardent la disponibilité, les
  programmes et l'indicateur de péremption accessibles aux utilisateurs
  autorisés à lancer. Leur champ `live` vaut `null` si le demandeur n'a pas le
  droit de lire cette séance. Un identifiant de séance absent, invalide,
  supprimé ou appartenant à une autre machine ne débloque jamais ce champ pour
  un simple utilisateur. Les tests `convex/trainingPrivacy.test.ts` passent par
  les vrais handlers et les tables Convex en mémoire, avec identités Clerk
  synthétiques ; ils ne couvrent pas encore la future matrice par organisation
  d'ANH-132.

- **Créer une machine** : admin seulement. Le rôle d'un compte ne change que par
  un admin (`users.updateUserRole`).
- Un compte qui se connecte pour la première fois reçoit le rôle **`user`**. Il
  n'existe **aucun mécanisme d'amorçage** du premier admin : il faut modifier le
  champ `role` à la main dans le tableau de bord Convex.

---

## 4. Fonctions de `training.ts`

Toutes les erreurs de ces fonctions sont des `ConvexError(message)` : le message
arrive intact dans le navigateur, même en production.

### Constantes (miroir de `raspberry-pi/src/training/plan.py`)

| Constante | Valeur | Rôle |
|---|---|---|
| `ZONE_CEILING_FRACTION` | 0,9 | La zone haute ne doit pas dépasser 90 % de la FC max du pratiquant. |
| FC max admise | 100 à 220 bpm | Hors bornes = refusée. |
| Âge admis pour l'estimation | 10 à 100 ans | Hors bornes = pas d'estimation. |
| `MIN_RIDER_AGE` | 18 | Âge minimum d'un lancement auto distant. **[MED]** : l'abaisser est une décision médicale. Le Pi a sa propre valeur (`MIN_RIDER_AGE` dans `.env`) et fait foi. |
| `LIVE_FRESH_MS` | 90 000 ms | Au-delà, l'état en direct est « périmé ». |

**FC max retenue** (`effectiveHrMax`) : la FC max mesurée si elle est dans
100-220 ; sinon, si l'année de naissance est connue, l'estimation de Tanaka
`round(208 − 0,7 × âge)` avec `âge = année courante − année de naissance` ;
sinon rien.

**Âge pour le contrôle** (`ageFrom`) : `année courante − année de naissance − 1`.
On suppose l'anniversaire pas encore passé : l'âge n'est jamais surestimé.

**Refus de zone** (`zoneRefusal`), dans cet ordre :

1. `zoneHighBpm > floor(0,9 × FC max)` → « Zone up to … exceeds 90% of this
   rider's max heart rate … » ;
2. `hardMaxBpm > FC max` → « Programme hard maximum … is above this rider's max
   heart rate … ».

### Queries et mutations publiques

| Fonction | Type | Arguments | Autorisation | Effet / retour |
|---|---|---|---|---|
| `grantLaunchRight` | mutation | `machineId`, `userId` | admin ou gestionnaire de la machine **et** de l'utilisateur ; cible de rôle `user` | Insère le droit. Sans effet s'il existe déjà. |
| `revokeLaunchRight` | mutation | `machineId`, `userId` | admin ou gestionnaire de la machine | Supprime le droit s'il existe. |
| `listLaunchRights` | query | `machineId` | admin ou gestionnaire de la machine (sinon `[]`) | `[{userId, name, email, hrMax (retenue ou null), grantedByName, createdAt}]`. |
| `setUserPhysiology` | mutation | `userId`, `hrMax?` (nombre ou `null` pour effacer), `birthYear?` (idem) | pas un `user` ; `canAccessUser` | Valide FC max 100-220, âge 10-100 ans. Erreurs : « Only a manager can set physiology », « You do not manage this user », « Max heart rate must be within 100-220 bpm », « Birth year gives an implausible age ». |
| `listMachineProfiles` | query | `machineId` | voir la machine (règle entraînement) | Programmes triés par nom. |
| `listLaunchableMachines` | query | - | connecté | Machines où l'on peut lancer : admin = toutes, gestionnaire = les siennes, user = celles où il a le droit. Machines supprimées exclues. Pour chacune : `status`, `programsEnabled`, `live` (ou `null` si plus vieux que 90 s), `profiles`, `myHrMax` (FC max retenue de l'appelant). |
| `launchAutoSession` | mutation | `machineId`, `profileId`, `userId?`, `totalDurationS?`, `notes?` | voir §3 | Crée une séance `pending` (`kind: auto`, `origin: remote`). Retourne son id. Contrôles ci-dessous. |
| `requestStop` | mutation | `sessionId` | pratiquant ou admin / gestionnaire de la machine | `pending` → `failed` avec « Cancelled before start by … ». `active` → pose `stopRequestedAt` (une seule fois). Autres statuts : rien. |
| `getMachineLive` | query | `machineId` | voir la machine | `{status, programsEnabled, live, stale}` ou `null`. `stale` = pas d'état ou plus vieux que 90 s. |
| `getSessionTelemetry` | query | `sessionId`, `sinceT?`, `limit?` | pratiquant ou admin / gestionnaire | Points du plus ancien au plus récent. `limit` par défaut 3600, borné à 1..7200 (les **derniers** points). |
| `getTrainingSession` | query | `sessionId` | pratiquant ou admin / gestionnaire | Champs d'entraînement de la séance, nom de la machine, et `canStop` (statut `pending`/`active` et droit d'arrêt). |

**Contrôles de `launchAutoSession`, dans l'ordre** (message renvoyé) :

1. Droits : « You can only launch a session for yourself », « You have not been
   given the right to launch sessions on this machine », « Not authorized to use
   this machine », « Not authorized to launch a session for this rider ».
2. Machine : « Machine not found » (absente ou supprimée), « Machine is
   offline », « Machine is already in a session », « This machine does not
   accept programmed sessions yet (manual only, at the machine) », « A session
   is already waiting for this machine ».
3. Programme : « This programme is not on the machine ».
4. Pratiquant : « The rider's max heart rate (or birth year) must be set by a
   manager before an auto session », puis le refus de zone, puis « The rider's
   birth year must be set by a manager before an auto session » (l'année est
   exigée **même** si la FC max est mesurée), puis « Rider is N: auto sessions
   require at least 18 years ».
5. Durée : « Duration must be positive ».

La séance créée copie le programme (zone, durée, ou la durée demandée),
`subjectHrMax`, `subjectAge`, `subjectLabel` (nom du pratiquant) et
`operatorName` (nom du lanceur).

### Fonctions internes (appelées par les routes HTTP)

| Fonction | Rôle |
|---|---|
| `updateLive` | Écrit `machines.live` (et `programsEnabled` s'il est fourni). |
| `syncProfiles` | Supprime tous les programmes de la machine, insère la nouvelle liste, met à jour `programsEnabled`. Retourne `{count}`. |
| `getRoster` | Patients détenant le droit sur la machine : `{userId, name, hrMax}`. |
| `getPendingTrainingSession` | Première séance `pending` de `kind: auto` de la machine, au format attendu par le Pi. |
| `markTrainingStarted` | `pending` → `active`, `startedAt` = maintenant, machine `in_session`. Refuse une séance d'une autre machine ou non `pending`. |
| `registerLocalSession` | Crée (une seule fois par `localRef`) une séance `active`, `origin: local`, et passe la machine `in_session`. |
| `endTrainingSession` | `completed` ou `failed` avec `endReason`, machine `online`. Idempotent. Pour une séance `completed` qui était `active`, planifie `sessionSummaries.generateSummary`. |
| `getTrainingStatus` | `{status, active, stopRequested}`. |
| `storeTelemetry` | Insère des points pour la séance (qui doit appartenir à la machine). |

---

## 5. Autres fonctions publiques

Résumé des fonctions les plus utilisées par le site.

### `users.ts`

| Fonction | Autorisation | Rôle |
|---|---|---|
| `getOrCreateUser` (mutation) | connecté | Crée la ligne `users` au premier passage (rôle `user`, langue `fr`). Appelée **uniquement** par la page d'accueil quand l'utilisateur connecté n'a pas encore de ligne. |
| `getCurrentUser` (query) | - | Le compte courant ou `null`. |
| `updateUserRole` | admin | Change le rôle d'un compte. |
| `updateUserProfile` | connecté | Prénom, nom, langue de soi-même. |
| `listUsers` | connecté | admin : tous (ou ceux d'un gestionnaire) ; gestionnaire : ses patients ; user : lui-même. Filtre `role` optionnel. |
| `createPatient` | admin, gestionnaire | Crée un patient (`clerkId` vide). Un gestionnaire s'y lie automatiquement ; un admin peut lier plusieurs gestionnaires. Refuse un e-mail déjà utilisé. **Aucun e-mail d'invitation n'est envoyé**, malgré le texte affiché par le site. |
| `updatePatient` | admin, gestionnaire du patient | Modifie un patient. |
| `getUserById` | `canAccessUser` | Profil, y compris `hrMax`, `birthYear` et `effectiveHrMax`. |
| `deleteUser` | admin ; gestionnaire pour ses patients seulement | Suppression. Pas soi-même. |
| `linkPatientToClerk` | connecté | Lie un patient pré-créé (même e-mail, `clerkId` vide) au compte Clerk. **Aucune page ne l'appelle aujourd'hui.** |
| `assignGestionnaireToUser` / `removeGestionnaireFromUser` | admin ; un gestionnaire pour lui-même | Lien patient ↔ gestionnaire. |
| `listGestionnaires`, `assignPatientsToGestionnaire` | admin | Gestion des gestionnaires. |
| `getPatientsForGestionnaire`, `getGestionnairesForPatient` | admin / gestionnaire concerné | Lectures. |

### `machines.ts`

| Fonction | Autorisation | Rôle |
|---|---|---|
| `createMachine` | admin | Crée la machine, statut `offline`. Retourne `{machineId, apiKey}` : la clé en clair (64 caractères hexadécimaux) **n'est visible qu'à ce moment**. |
| `regenerateApiKey` | admin, gestionnaire de la machine | Nouvelle clé ; l'ancienne cesse de fonctionner. |
| `getMachine`, `listMachines` | admin ; gestionnaire (ses machines) | Lecture. `listMachines` renvoie `[]` à un `user`. Option `includeDeleted` pour l'admin. |
| `updateMachine` | admin, gestionnaire de la machine | Nom, lieu, config. |
| `deleteMachine` | admin, gestionnaire de la machine | Suppression douce. Refusée s'il y a une séance `active` ou `pending`. |
| `restoreMachine` | admin | Annule la suppression. |
| `assignMachineToGestionnaires` | admin | Liste des gestionnaires d'une machine. |
| `assignGestionnaireToMachine` / `removeGestionnaireFromMachine` | admin ; gestionnaire de la machine | Lien machine ↔ gestionnaire. |
| `getRecentHeartbeats`, `getGestionnairesForMachine`, `getMachinesForGestionnaire` | accès à la machine / admin | Lectures. |

### `sessions.ts` (séances d'enregistrement héritées, et listes)

| Fonction | Autorisation | Rôle |
|---|---|---|
| `createSession` | admin, gestionnaire | Crée une séance **`recording`** `pending` (ancien mode ECG). Machine en ligne, non occupée, au moins un canal. |
| `endSession`, `cancelSession` | admin, gestionnaire de la machine | Termine / annule une séance d'enregistrement. |
| `getSession` | pratiquant ou accès machine | Détail avec patient et machine. |
| `listSessions` | connecté | admin : toutes ; user : les siennes ; gestionnaire : celles de ses machines. Retourne `kind` et `origin`. La limite (`limit`, 50 par défaut) s'applique **avant** le filtrage par droits. |
| `getActiveSessionForMachine` | accès machine | Séance active. |
| `getCompletedSessionsForUser` | connecté | Séances `completed` visibles (page Rapports). |

### `ecgData.ts` et `sessionSummaries.ts`

Lectures de l'ECG des séances d'enregistrement (`getRecentEcgData`,
`getSessionAllData`, `getSessionEcgRange`, `getSessionDataStats`,
`getLatestEcgBatch`) et du résumé (`getSummary`, `getSummaryWithEcg`).
Autorisation : pratiquant, admin ou accès à la machine. `getRecentEcgData`
applique un **retard de 5 s** aux gestionnaires sur une séance active.

---

## 6. Routes HTTP machine (`convex/http.ts`)

**Hôte** : l'URL `.convex.site` du déploiement (pas `.convex.cloud` : les routes
HTTP n'y existent pas).

**Authentification** : chaque route exige
`Authorization: Bearer <clé API de la machine>`. Sans en-tête : **401**
`{"error": "Missing Authorization header"}`. Clé inconnue : **401**
`{"error": "Invalid API key"}`.

**Erreurs** : les routes d'entraînement renvoient **400** `{"error": "<message>"}`
pour un corps mal formé **et** pour toute exception interne (par exemple
« Session not found »). Côté Pi, toute réponse ≥ 400 devient `Refused` (on ne
réessaie pas la même requête), une absence de réponse devient `Unreachable`
(on réessaie plus tard).

### Routes utilisées par la console locale (`raspberry-pi/src/cloud_sync.py`)

| Méthode et chemin | Corps / paramètres | Réponse 200 | Cadence côté Pi |
|---|---|---|---|
| `POST /api/machine/heartbeat` | `{live, programsEnabled, activeSessionId?}` (`batteryLevel`, `wifiStrength` acceptés, non envoyés) | `{success: true, serverTime}` | toutes les 10 s |
| `POST /api/machine/profiles` | `{storeRev, programsEnabled, profiles: [...]}` | `{count}` | quand la révision du magasin change ; nouvel essai après 15 s |
| `GET /api/machine/training/poll` | - | `{session: null}` ou `{session: {sessionId, profileId, totalDurationS\|null, subjectId, subjectLabel, subjectHrMax, subjectAge\|null, operatorName}}` | toutes les 3 s, seulement si `PROGRAMS_ENABLED`, sans séance en cours ni lancement en attente |
| `POST /api/machine/training/start` | `{sessionId}` | `{success: true}` | une fois, quand le Pi a armé un lancement distant |
| `POST /api/machine/training/local` | `{localRef, kind: "auto"\|"manual", startedAt, operatorName, profileId?, profileName?, zoneLowBpm?, zoneHighBpm?, totalDurationS?, subjectHrMax?, occupancy?, userId?, subjectLabel?}` | `{sessionId}` | une fois par séance démarrée à la machine ; idempotent par `localRef` |
| `GET /api/machine/training/status?sessionId=…` | - | `{status, active, stopRequested}` ; 404 si inconnue | toutes les 3 s pendant une séance |
| `POST /api/machine/training/telemetry` | `{sessionId, points: [{t, elapsedS, phase, bpm?, motorRpm, outputRpm, setpointMotorRpm, gLoad, safetyAction}]}` (600 points max) | `{stored}` | lots de 300 points max, toutes les 5 s |
| `POST /api/machine/training/end` | `{sessionId, failed, reason, endedAt?}` | `{success: true}` | à la fin, après la télémétrie restante |

Détails du contrat :

- **`live`** n'est pris en compte que s'il est bien formé (`runMode`, `phase`,
  `safetyAction` chaînes ; vitesses et `gLoad` nombres ; `bpm` nombre ou absent).
  Sinon seul le heartbeat est enregistré.
- **Statut de la machine** : chaque heartbeat met `online`, ou `in_session` si
  `activeSessionId` est présent.
- **Arrêt demandé** : si `status` répond `stopRequested: true` ou
  `active: false`, le Pi fait un arrêt ordinaire sur la rampe réglée, attribué à
  l'opérateur « tableau de bord ».
- **Lancement refusé par le Pi** : le Pi termine la séance `pending` par
  `/training/end` avec `failed: true` et une raison qui commence par
  `refusee par la machine : `. Un lancement ni démarré ni refusé en 60 s est
  terminé avec « la boucle n'a ni demarre ni refuse ».
- **Annulé entre-temps** : si `/training/start` est refusé (séance annulée sur le
  site entre le poll et l'armement), le Pi arrête la séance qu'il vient d'armer.
- **Fin** : `failed` vaut `false` pour `programme_complete` et `operator_stop`,
  `true` pour `emergency_stop`, `safety_verdict`, `tick_exception`, `shutdown`.
- **Réseau perdu** : la séance continue sous le seul superviseur local. La
  télémétrie en attente est bornée à 3600 points (1 h), les plus anciens
  sont jetés d'abord ; 20 séances terminées au plus restent dues.

### Routes présentes mais non appelées par la console locale

| Méthode et chemin | Rôle |
|---|---|
| `GET /api/machine/roster` | `{riders: [{userId, name, hrMax}]}` : patients ayant le droit sur la machine. **Aucun code du Pi ne l'appelle.** |
| `GET /api/machine/session/poll` | Ancien mode : `{session: null}` ou `{session: {id, channels, config}}`. Ne renvoie **que** les séances `recording`, jamais une séance auto. |
| `POST /api/machine/session/start` | Ancien mode : `{sessionId}`. |
| `POST /api/machine/session/end` | Ancien mode : `{sessionId, reason?, failed?}`. |
| `GET /api/machine/session/status?sessionId=` | Ancien mode : `{status, endedAt, active}`. |
| `POST /api/machine/data` | Ancien mode : lot ECG `{sessionId, timestamp, sampleRate?, samples, metrics?, batchId?}`. Horodatage refusé s'il est à plus de 1 min dans le futur ou plus de 5 min dans le passé. La séance doit être `active` et appartenir à la machine. |

Ces routes « session » et « data » sont celles de `raspberry-pi/src/convex_client.py`
(client hérité `python -m src.main`).

---

## 7. Tâche planifiée

`convex/crons.ts` : `check-offline-machines`, **toutes les minutes**. Une machine
`online` ou `in_session` sans heartbeat depuis **90 s** passe `offline`.

---

## 8. Déployer

Le déploiement de développement existe et reçoit le code par
`npx convex dev --once` ; la production n'a pas encore reçu le nouveau code. Les
environnements, les clés et les commandes réellement utilisées sont dans
[deploiement.md](deploiement.md#3-déployer-convex). Les étapes ci-dessous sont
celles d'un projet **neuf**.

1. Installer les dépendances à la racine : `npm install` (ou `bun install`).
2. Créer un déploiement de développement et générer `convex/_generated/` :
   ```bash
   npx convex dev
   ```
   La commande demande de se connecter, crée le projet, écrit `CONVEX_DEPLOYMENT`
   et `NEXT_PUBLIC_CONVEX_URL` dans `.env.local`, puis surveille `convex/`.
3. Configurer Clerk : créer un modèle JWT nommé `convex` dans Clerk, puis
   définir `CLERK_JWT_ISSUER_DOMAIN` dans les variables d'environnement **du
   déploiement Convex** (`convex/auth.config.ts` le lit ; `applicationID` vaut
   `convex`).
4. Production :
   ```bash
   npx convex deploy
   ```
5. Nommer le premier admin : se connecter une fois sur le site (page d'accueil),
   puis passer `role` à `admin` dans la table `users` du tableau de bord Convex.
6. Créer la machine sur le site (admin), copier la clé affichée **une seule
   fois**, et la mettre dans `raspberry-pi/.env` : `MACHINE_API_KEY=…` et
   `CONVEX_URL=https://<déploiement>.convex.site`.

`npm run dev` lance ensemble Next.js et `convex dev` ; son `predev` exécute
`convex dev --until-success && convex dashboard` (réseau et compte requis).

---

## 9. Défauts connus et reste à faire

Trouvés à la lecture du code. Aucun n'a été vérifié à l'exécution.

| # | Constat | Conséquence |
|---|---|---|
| 1 | **Clé API non hachée.** `hashApiKey` fait `base64` puis inverse la chaîne (`convex/lib/crypto.ts`). C'est réversible. | Qui lit la table `machines` retrouve toutes les clés. Le commentaire du code recommande lui-même SHA-256. |
| 2 | **Machine supprimée toujours authentifiée.** `getMachineByApiKey` ne regarde pas `isDeleted`. | Un Pi dont la machine est supprimée peut encore envoyer heartbeats, programmes et télémétrie. Il ne peut plus recevoir de lancement (`launchAutoSession` refuse une machine supprimée). |
| 3 | ~~`users.getCurrentUser` : validateur de retour incomplet.~~ **Corrigé.** Le validateur déclare `hrMax` et `birthYear`. | Vérifié sur le déploiement de développement le 1er octobre 2026 : la query répond après réglage de la FC max du compte. |
| 4 | **Séances locales sans pratiquant.** Le Pi n'envoie ni `userId` ni `subjectLabel` à `/training/local` (le champ existe côté Convex). | Une séance démarrée à la machine apparaît sans pratiquant (« Unknown » dans les listes). Le patient ne la voit pas dans ses séances. |
| 5 | **`/api/machine/roster` inutilisé.** | La liste des pratiquants autorisés n'arrive pas sur la console locale. |
| 6 | **Libellés d'actions de sécurité.** Le Pi envoie `freeze`, `quick_stop`, `go_silent` ; les traductions du site connaissent `hold`, `stop`, `estop`. Le commentaire du schéma cite aussi `"hold"`. | Ces trois actions s'affichent en valeur brute. |
| 7 | **Pas d'ECG pour les séances d'entraînement.** La console locale n'envoie que la télémétrie à 1 Hz. | Pas de tracé ECG ni de résumé (`session_summaries`) ni de rapport PDF utile pour une séance auto/manuelle. |
| 8 | **Pas d'amorçage d'admin ni d'invitation.** | Voir [§3](#3-règles-dautorisation) et `createPatient`. Un patient pré-créé qui s'inscrit reçoit une **seconde** ligne `users`, car `linkPatientToClerk` n'est jamais appelé. |
| 9 | **Historique non purgé.** | `machine_heartbeats` grossit d'une ligne toutes les 10 s par machine. |
| 10 | **Aucun test automatisé** Convex ; nouveau code pas encore en production (ANH-82). | Le contrat HTTP a été exercé une fois à la main contre le déploiement de développement ([deploiement.md](deploiement.md#4-essai-de-bout-en-bout-du-1er-octobre-2026)) ; rien ne le rejoue automatiquement. |
| 11 | **Séance orpheline.** Si la console s'arrête pendant une séance (arrêt du conteneur, coupure), elle n'envoie pas `/training/end`, et ne le rattrape pas au redémarrage. Trouvé à l'essai du 1er octobre 2026. | La séance reste `active` dans Convex indéfiniment. La machine repasse `online` au redémarrage. |

Fait le 1er octobre 2026 : déploiement sur l'environnement de développement,
essai de bout en bout avec un Pi en simulation complète. Reste à faire, dans
l'ordre logique : corriger 1, 2, 4 et 11 ; écrire des tests Convex ; déployer
en production. Le site a été ouvert contre le développement le 2 octobre 2026
(voir le [guide du tableau de bord](guides/guide-tableau-de-bord.md#94-ce-que-les-vrais-écrans-ont-montré-2-octobre-2026)). Voir aussi [securite.md](securite.md).

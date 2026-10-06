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
> un vrai Pi. Le retrait de l'ancien mode d'enregistrement ECG décrit ici
> (routes, mutations, champ `config`, migration) n'est déployé **nulle part**.
> Cet état de déploiement est historique ; les tests locaux Convex
> sont désormais exécutables avec `npm run test:convex`. Les fichiers `convex/training.ts`, `lib/training.ts`,
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
10. [Tests automatisés](#10-tests-automatisés)
11. [Versions et compatibilité](#11-versions-et-compatibilité)

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
- L'ancien mode « enregistrement ECG seul » est **retiré** : plus aucune
  fonction ni route ne crée, ne démarre, n'alimente ni ne termine une séance
  d'enregistrement. Ses séances restent en base, sans champ `kind`, en lecture
  seule ; les lectures les rapportent avec le type `recording`.

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

### `software_releases` : versions publiées

Une ligne par version publiée d'un composant. Écrite seulement par un admin
(`softwareReleases.recordRelease`), à la fin d'une release
([release.md](release.md#6-enregistrer-la-version-dans-convex)).

| Champ | Sens |
|---|---|
| `component` | `pi` (Raspberry Pi), `cloud` (Convex) ou `web` (site) |
| `version` | le tag de la version, par exemple `pi-0.1.0` : la même chaîne que le Pi annoncera dans son heartbeat (ANH-133) |
| `validationLevel` | pour une version du Pi seulement : `bench`, `auto_validated` (M5) ou `occupied_validated` (M6) |
| `releasedAt` | date de la release, ms Unix |
| `notes` | texte libre ; obligatoire quand le niveau d'une version déjà enregistrée change |
| `recordedBy`, `updatedAt` | l'admin qui a écrit la ligne, et quand |

Index `by_component_and_version`.

**Règle portée par `validationLevel`.** Une machine validée pour les séances
programmées (M5) ne reçoit qu'une version `auto_validated` ou
`occupied_validated` ; une machine validée pour une personne à bord (M6) ne
reçoit qu'une version `occupied_validated`. La règle est codée dans
`convex/lib/releaseValidation.ts` (`releaseAllowedOnMachine`) et testée, mais
**aucune fonction ne l'appelle encore** : l'état de validation d'une machine
n'existe pas dans le schéma. Le registre machine (ANH-147) et la mise à jour à
distance (ANH-116, ANH-168) l'appliqueront. Détail dans
[release.md](release.md#2-le-niveau-de-validation-dune-version-du-pi).

Cette table n'est déployée sur aucun déploiement.

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
| `apiKey` | Vérificateur salé versionné `hmac-sha256:1:<sel hex>:<digest hex>` pour les clés créées/régénérées. Les anciennes valeurs réversibles doivent être remplacées lors de la migration ANH-82. |
| `apiKeySelector` | Sélecteur public aléatoire de 128 bits, optionnel pour accepter le schéma historique. Ne permet jamais de s'authentifier seul. |
| `authenticationEnabled` | `false` refuse l'authentification ; absent ou `true` l'autorise sous réserve d'une clé valide et d'une machine non supprimée. Distinct de `status` et de `programsEnabled`. Aucun nouveau contrôle public/UI de ce champ. |
| `status` | `"online"` \| `"offline"` \| `"in_session"`. |
| `lastHeartbeat` | ms Unix du dernier heartbeat. |
| `config` | **Obsolète.** `{ sampleRate, channels, batchInterval }`, réglages de l'ancien enregistreur ECG. Facultatif ; aucune fonction ne le lit ni ne l'écrit. Il reste déclaré le temps que la migration `removeMachineConfig` le retire des documents ([§8](#8-déployer)), puis il quittera le schéma. |
| `isDeleted`, `deletedAt`, `deletedBy` | Suppression douce. |
| `programsEnabled` | Rapporté par le Pi : accepte-t-il les séances auto ? |
| `live` | Dernier état rapporté par le Pi (voir ci-dessous). |
| `softwareVersion` | Version logicielle annoncée par le Pi dans son heartbeat (tag git, par exemple `pi-0.4.2`). Absente tant qu'aucun heartbeat ne l'a donnée, et effacée si le dernier heartbeat n'en portait pas de bien formée. |
| `contractVersion` | Version du contrat machine annoncée par le Pi : l'en-tête `X-Anheart-Contract` du dernier heartbeat accepté. |
| `lastVersionSeenAt` | ms Unix du heartbeat qui a porté ces deux valeurs. |

Index : `by_api_key` (historique, inutilisé pour authentifier), `by_apiKeySelector`, `by_status`, `by_is_deleted`.

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
| `channels`, `sampleRate`, `notes` | `channels` vaut `["ECG"]` pour une séance d'entraînement. `sampleRate` n'est plus écrit : il ne reste que sur les séances de l'ancien mode. `notes` contient `Occupancy: bench/occupied` pour une séance locale qui déclare l'occupation. |
| `kind` | `auto` \| `manual`. **Absent** = séance de l'ancien mode d'enregistrement ECG (historique) : `getSession`, `listSessions` et `getTrainingSession` la rapportent `recording`. Le schéma refuse d'écrire cette valeur. |
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

### Historique de l'ancien mode d'enregistrement ECG (lecture seule)

| Table | Contenu |
|---|---|
| `ecg_data` | Lots d'ECG **déjà traité** sur le Pi (`values` en mV à `sampleRate` Hz) et métriques par canal (`heartRate`, `hrv`, `quality`…), écrits autrefois par l'enregistreur. **Plus aucune fonction n'y écrit.** Les lectures de `ecgData.ts` restent pour consulter l'existant. La console locale n'a jamais envoyé ces lots. |
| `session_summaries` | Résumé calculé autrefois à la fin d'une séance d'enregistrement (FC moyenne/min/max, HRV, ECG sous-échantillonné). **Plus rien n'en calcule ni n'en écrit** ; `getSummary` et `getSummaryWithEcg` lisent l'existant. Le résumé d'une séance d'entraînement viendra de son enregistrement (ANH-89). |

### `machine_heartbeats`

Historique des heartbeats (`timestamp`, `batteryLevel`, `wifiStrength`,
`activeSessionId`). Aucun nettoyage n'est programmé.

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
| `requireGestionnaireAdmin(g)` | admin, et `g` est un compte de rôle `gestionnaire` (sinon « Gestionnaire not found » ou « Target user is not a gestionnaire »). Le rôle de l'appelant est vérifié avant toute lecture de `g`. C'est la seule règle à étendre le jour où un rôle limité à une organisation administre les gestionnaires de la sienne ([ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site)). |

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
- **Lier un dossier patient à un compte Clerk** (`users.linkPatientToClerk`) :
  la liaison exige l'adresse **vérifiée** de l'appelant. Elle n'aboutit que si
  l'e-mail vérifié de l'identité (claims `email` et `email_verified`) est celui
  du dossier ; la comparaison ignore la casse des lettres ASCII seulement, et
  deux adresses qui diffèrent par un caractère non ASCII restent distinctes.
  L'argument `email` ne fait pas autorité. Sans e-mail vérifié la liaison est
  refusée, un dossier déjà lié n'est jamais relié, et la réponse ne distingue
  pas « aucun dossier » de « e-mail différent ».
- **Routes machine et appartenance de la séance** : huit routes de
  `convex/http.ts` lisent ou modifient une séance désignée par son
  identifiant. Sous `/api/machine/`, ce sont `session/start`, `session/end`,
  `session/status`, `data`, `training/start`, `training/end`,
  `training/status` et `training/telemetry`. Chacune passe la machine
  authentifiée à sa fonction interne, qui vérifie d'abord que la séance
  appartient à cette machine. Pour ces huit routes, une séance d'une autre
  machine reçoit exactement la réponse d'une séance inconnue (statut, en-têtes
  et corps), quel que soit l'état de la séance, et ni la séance, ni sa machine,
  ni les mesures enregistrées ne changent ; `convex/httpRoutes.test.ts` compare
  les deux réponses pour chaque route.

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
| `LIVE_FRESH_MS` | 90 000 ms | Au-delà, l'état en direct est « périmé ». Une seule définition, dans `lib/training.ts`, importée par `convex/training.ts` et par le site, qui recalcule la fraîcheur chaque seconde ([tableau-de-bord.md §6](tableau-de-bord.md#fraîcheur-recalculée-à-lhorloge)). `lib/training.ts` doit donc rester sans import `@/` ni code réservé au navigateur. |

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
| `listLaunchableMachines` | query | - | connecté | Machines où l'on peut lancer : admin = toutes, gestionnaire = les siennes, user = celles où il a le droit. Machines supprimées exclues. Pour chacune : `status`, `programsEnabled`, `live` (ou `null` si plus vieux que 90 s quand la query s'exécute), `profiles`, `myHrMax` (FC max retenue de l'appelant). |
| `launchAutoSession` | mutation | `machineId`, `profileId`, `userId?`, `totalDurationS?`, `notes?` | voir §3 | Crée une séance `pending` (`kind: auto`, `origin: remote`). Retourne son id. Contrôles ci-dessous. |
| `requestStop` | mutation | `sessionId` | pratiquant ou admin / gestionnaire de la machine | `pending` → `failed` avec « Cancelled before start by … ». `active` → pose `stopRequestedAt` (une seule fois). Autres statuts : rien. |
| `getMachineLive` | query | `machineId` | voir la machine | `{status, programsEnabled, live, stale}` ou `null`. `stale` = pas d'état ou plus vieux que 90 s **au moment où la query s'exécute** : elle ne se relance pas quand une machine se tait, le site recalcule donc la fraîcheur à l'horloge à partir de `live.updatedAt`. |
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
| `endTrainingSession` | `completed` ou `failed` avec `endReason`, machine `online`. Idempotent. Ne planifie rien : aucun résumé n'est calculé à la fin d'une séance. |
| `getTrainingStatus` | `{status, active, stopRequested}`. |
| `storeTelemetry` | Insère des points pour la séance (qui doit appartenir à la machine). |

Les refus de `markTrainingStarted`, `registerLocalSession`,
`endTrainingSession` et `storeTelemetry` sont levés par
`machineError(code, message)` (`convex/lib/contract.ts`) : la route les
renvoie avec leur code stable (voir [section 6](#6-routes-http-machine-convexhttpts)).

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
| `linkPatientToClerk` | connecté, e-mail vérifié | Lie un patient pré-créé (`clerkId` vide) au compte Clerk, uniquement si l'e-mail **vérifié** de l'appelant est celui du dossier (voir [§3](#3-règles-dautorisation)). **Aucune page ne l'appelle aujourd'hui.** |
| `assignGestionnaireToUser` / `removeGestionnaireFromUser` | admin ; un gestionnaire pour lui-même | Lien patient ↔ gestionnaire. |
| `listGestionnaires`, `assignPatientsToGestionnaire` | admin | Gestion des gestionnaires. |
| `getPatientsForGestionnaire`, `getGestionnairesForPatient` | admin / gestionnaire concerné | Lectures. |

### `machines.ts`

| Fonction | Autorisation | Rôle |
|---|---|---|
| `createMachine` | admin | Crée la machine, statut `offline`. Retourne `{machineId, apiKey}` : la clé `anh1.<sélecteur>.<secret>` **n'est visible qu'à ce moment**. |
| `regenerateApiKey` | admin, gestionnaire de la machine | Nouvelle clé ; l'ancienne cesse de fonctionner. |
| `getMachine`, `listMachines` | admin ; gestionnaire (ses machines) | Lecture. `listMachines` renvoie `[]` à un `user`. Option `includeDeleted` pour l'admin. `getMachine` renvoie aussi `softwareVersion`, `contractVersion` et `lastVersionSeenAt`. |
| `updateMachine` | admin, gestionnaire de la machine | Nom, lieu. |
| `deleteMachine` | admin, gestionnaire de la machine | Suppression douce. Refusée s'il y a une séance `active` ou `pending`. |
| `restoreMachine` | admin | Annule la suppression. |
| `assignMachineToGestionnaires` | admin | Remplace la liste complète des gestionnaires d'**une machine**. |
| `setGestionnaireMachines` | admin (`requireGestionnaireAdmin`) | Fixe la liste exacte des machines d'**un gestionnaire** : compare la liste demandée à ses lignes `machine_gestionnaires`, insère les liens manquants (`isOwner: false`) et supprime ceux qui ne sont plus demandés. Seules les lignes de ce gestionnaire sont lues et écrites : les liens des autres gestionnaires ne bougent pas, et un lien déjà présent n'est pas modifié. Une machine inconnue fait refuser l'appel sans rien écrire. Retourne `{added, removed}`. |
| `assignGestionnaireToMachine` / `removeGestionnaireFromMachine` | admin ; gestionnaire de la machine | Lien machine ↔ gestionnaire. |
| `getRecentHeartbeats`, `getGestionnairesForMachine`, `getMachinesForGestionnaire` | accès à la machine / admin | Lectures. |

### `sessions.ts` (lectures seulement)

Aucune fonction de ce module ne crée, ne démarre ni ne termine une séance. Une
séance naît de `training.launchAutoSession` (lancement depuis le site) ou de
`registerLocalSession` (séance démarrée à la machine), et se termine par les
routes d'entraînement.

| Fonction | Autorisation | Rôle |
|---|---|---|
| `getSession` | pratiquant ou accès machine | Détail avec patient et machine. |
| `listSessions` | connecté | admin : toutes ; user : les siennes ; gestionnaire : celles de ses machines. Retourne `kind` et `origin`. La limite (`limit`, 50 par défaut) s'applique **avant** le filtrage par droits. |
| `getActiveSessionForMachine` | accès machine | Séance active. |
| `getCompletedSessionsForUser` | connecté | Séances `completed` visibles (page Rapports). |

### `ecgData.ts` et `sessionSummaries.ts` (historique, lecture seule)

Lectures de l'ECG des séances de l'ancien mode d'enregistrement
(`getRecentEcgData`, `getSessionAllData`, `getSessionEcgRange`,
`getSessionDataStats`, `getLatestEcgBatch`) et de leur résumé (`getSummary`,
`getSummaryWithEcg`). Ces deux modules ne contiennent plus aucune écriture.
Autorisation : pratiquant, admin ou accès à la machine. `getRecentEcgData`
applique un **retard de 5 s** aux gestionnaires sur une séance active. Le site
n'appelle plus que `getSessionDataStats`, pour la carte d'historique du détail
d'une séance.

### `softwareReleases.ts` (registre des versions publiées)

Aucune page du site n'appelle encore ces fonctions.

| Fonction | Autorisation | Rôle |
|---|---|---|
| `recordRelease` (mutation) | admin | Enregistre une version publiée dans `software_releases`, ou corrige la ligne d'une version déjà connue (une seule ligne par composant et version). Refuse une version qui n'est pas `<composant>-X.Y.Z`, une version du Pi sans niveau de validation, un niveau sur une version `cloud` ou `web`, une date invalide, des notes de plus de 2000 caractères, et un changement de niveau sans `notes`. Erreurs en `ConvexError(message)`. |
| `listReleases` (query) | admin | Les versions enregistrées, la plus récente d'abord ; filtre `component` optionnel. |

`deployedCloudVersion` est une query **interne** : elle répond la constante
`CLOUD_VERSION` de `convex/cloudVersion.ts`, donc la version du code déployé
(`cloud-0.0.0-dev` tant qu'aucune release n'a été faite). On la lit avec
`npx convex run softwareReleases:deployedCloudVersion`. `scripts/release.sh`
écrit cette constante en même temps que `convex/VERSION`, et
`convex/cloudVersion.test.ts` échoue si les deux diffèrent.

---

## 6. Routes HTTP machine (`convex/http.ts`)

**Hôte** : l'URL `.convex.site` du déploiement (pas `.convex.cloud` : les routes
HTTP n'y existent pas).

**Authentification** : chaque route exige
`Authorization: Bearer <clé API de la machine>`. Sans en-tête : **401**
`{"error": "unauthorized", "message": "Missing Authorization header"}`.
Clé inconnue : **401**
`{"error": "unauthorized", "message": "Invalid API key"}`.

**Contrat** : chaque route exige ensuite l'en-tête
`X-Anheart-Contract: <majeure.mineure>`, vérifié **après** la clé et avant
tout traitement, par le même point de passage que l'authentification
(`validateMachineAuth`, `convex/lib/machineHttpAuth.ts`). En-tête absent,
illisible, ou d'une majeure non servie : **426**
`{"error": "contract_unsupported", "message": "…", "supported": ["1"]}`,
et rien n'est lu ni écrit. Une mineure plus récente que celle du serveur
est acceptée. Voir [Versions et compatibilité](#11-versions-et-compatibilité).

La suppression (`isDeleted`) ou la désactivation explicite
(`authenticationEnabled: false`) produit aussi **401**, sur les 9 routes et
les deux queries internes d'authentification. Une machine simplement `offline`
peut envoyer son heartbeat avec une clé valide.

La clé contient un sélecteur public aléatoire de 16 octets et un secret
aléatoire de 32 octets, encodés en hexadécimal. `crypto.getRandomValues` génère
ces valeurs ainsi qu'un sel indépendant de 16 octets. Le vérificateur est
exactement `HMAC-SHA-256(key = sel, message = UTF-8(clé complète))` : une
construction SHA-256 salée, et non un simple encodage ni `SHA-256(clé)`.
Le sel est public ; la résistance à la recherche exhaustive vient du secret
aléatoire de 256 bits. Ce mécanisme n'est pas destiné à des mots de passe humains.

L'index `by_apiKeySelector` sélectionne une seule machine, puis WebCrypto
`subtle.verify("HMAC", …)` vérifie le digest ; aucune comparaison JavaScript
de secrets n'est effectuée. Le [runtime Convex](https://docs.convex.dev/functions/runtimes)
prend en charge WebCrypto ; son [vérificateur HMAC natif](https://github.com/get-convex/convex-backend/blob/588c89b23f669e9ab158848d1ecd53707dac09b8/crates/webcrypto/src/hmac.rs)
appelle `aws_lc_rs::hmac::verify`, dont la [comparaison est à temps constant](https://docs.rs/aws-lc-rs/1.16.3/aws_lc_rs/hmac/fn.verify.html).
La recherche du sélecteur et les rejets de format/état ne promettent pas un
temps constant pour la requête entière. Les tests locaux vérifient le résultat
et un vecteur indépendant, pas les temps d'exécution du service hébergé.

La création renvoie `{machineId, apiKey}` et la régénération `{apiKey}` une seule
fois. Les lectures sélectionnent leurs champs sans credential ; aucun journal
applicatif n'enregistre la clé. Une régénération remplace atomiquement sélecteur,
sel et digest : l'ancienne clé cesse de fonctionner immédiatement. Les champs
de suppression/désactivation restent inchangés.

**Erreurs** : tout refus des routes d'entraînement (et de `profiles`,
`roster`, ainsi que les 401 et 426 de toutes les routes) a la forme
`{"error": "<code stable>", "message": "<texte>"}`. Le code ne change pas
d'une version à l'autre ; le texte, en anglais, est fait pour être lu et peut
changer. La liste des codes est dans `contracts/machine-api.json` :

| Code | Statut | Sens |
|---|---|---|
| `contract_unsupported` | 426 | Majeure de contrat absente ou non servie ; la réponse porte `supported`. |
| `unauthorized` | 401 | En-tête `Authorization` absent, clé inconnue, machine supprimée ou désactivée. |
| `invalid_request` | 400 | Corps ou paramètres mal formés. |
| `session_not_found` | 400, ou 404 sur `training/status` | Séance inconnue, ou qui n'appartient pas à la machine authentifiée (même réponse dans les deux cas). |
| `session_not_pending` | 400 | `training/start` sur une séance qui n'attend plus son départ. |
| `machine_not_found` | 400 | La machine a disparu entre l'authentification et l'écriture. |
| `request_failed` | 400 | Toute autre erreur levée pendant le traitement ; `message` porte son texte. |

Les routes de l'ancien mode (`session/*`, `data`) gardent leur forme
`{"error": "<message>"}` pour leurs propres refus. Côté Pi, toute réponse
≥ 400 devient `Refused` (on ne réessaie pas la même requête) et son code est
journalisé ; une absence de réponse devient `Unreachable` (on réessaie plus
tard).

### Routes utilisées par la console locale (`raspberry-pi/src/cloud_sync.py`)

| Méthode et chemin | Corps / paramètres | Réponse 200 | Cadence côté Pi |
|---|---|---|---|
| `POST /api/machine/heartbeat` | `{live, programsEnabled, activeSessionId?, software_version, contract_version, medical_parameters_version\|null, config_hash\|null}` (`batteryLevel`, `wifiStrength` acceptés, non envoyés) | `{success: true, serverTime}` | toutes les 10 s |
| `POST /api/machine/profiles` | `{storeRev, programsEnabled, profiles: [...]}` | `{count}` | quand la révision du magasin change ; nouvel essai après 15 s |
| `GET /api/machine/training/poll` | - | `{session: null, server_contract_version}` ou `{session: {sessionId, profileId, totalDurationS\|null, subjectId, subjectLabel, subjectHrMax, subjectAge\|null, operatorName}, server_contract_version}` | toutes les 3 s, seulement si `PROGRAMS_ENABLED`, sans séance en cours ni lancement en attente |
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
- **Versions** : chaque heartbeat écrit `machines.softwareVersion` (le champ
  `software_version` s'il est bien formé : 64 caractères au plus, lettres,
  chiffres, `.`, `_`, `+`, `-` ; sinon le champ est effacé),
  `machines.contractVersion` (l'en-tête `X-Anheart-Contract`, qui vient d'être
  vérifié, et non la copie `contract_version` du corps) et
  `machines.lastVersionSeenAt`.
- **Serveur d'un autre contrat** : `server_contract_version` vaut la version
  servie par Convex. Le Pi n'arme rien d'une réponse dont la majeure n'est
  pas la sienne ; il termine le lancement par `/training/end` avec la raison
  `refusee par la machine : serveur incompatible (contrat X vs Y)`.
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

### Route présente mais non appelée par la console locale

| Méthode et chemin | Rôle |
|---|---|
| `GET /api/machine/roster` | `{riders: [{userId, name, hrMax}]}` : patients ayant le droit sur la machine. **Aucun code du Pi ne l'appelle.** |

### Routes retirées

Les cinq routes de l'ancien mode d'enregistrement ECG (interrogation, début,
fin et statut d'une séance d'enregistrement, envoi des lots ECG) n'existent
plus : leurs chemins répondent **404**, même avec une clé valide. Le client qui
les appelait a été retiré du Pi.

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
   `convex`). Le jeton envoyé à Convex doit porter les revendications `email`
   et `email_verified` : `users.linkPatientToClerk` y lit l'adresse vérifiée de
   l'appelant, et sans ces deux revendications la liaison est refusée (la
   mutation renvoie `null`).
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

### Migration du retrait de l'ancien mode ECG

**Pas encore exécutée, sur aucun déploiement.** Deux mutations internes de
`convex/migrations/retireLegacyRecording.ts`, à lancer à la main, une fois par
déploiement, **après** avoir déployé ce code :

```bash
npx convex run migrations/retireLegacyRecording:failOpenRecordingSessions
npx convex run migrations/retireLegacyRecording:removeMachineConfig
```

| Mutation | Effet | Résultat renvoyé |
|---|---|---|
| `failOpenRecordingSessions` | Passe `failed` toute séance de l'ancien mode (sans `kind`) encore `pending` ou `active`, avec `endReason = "legacy mode retired"` et `endedAt`. Les séances d'enregistrement finies et toutes les séances d'entraînement ne sont pas touchées ; les lignes gardent leur forme (pas de `kind` ajouté). Le statut de la machine n'est pas écrit : le prochain heartbeat le recalcule. | `{machinesChecked, sessionsFailed}` |
| `removeMachineConfig` | Retire le champ `config` de chaque machine, supprimées comprises. Rien d'autre ne change. | `{machinesChecked, machinesCleared}` |

Les deux sont idempotentes : une seconde exécution renvoie zéro. Pourquoi la
première compte : une séance d'enregistrement restée `pending` refuse tout
lancement auto sur sa machine (« A session is already waiting for this
machine ») et empêche de supprimer cette machine.

Le champ `machines.config` reste déclaré, facultatif, dans le schéma : le
retirer tant que des documents le portent ferait échouer `convex deploy`. Une
fois `removeMachineConfig` passée sur **tous** les déploiements, le champ peut
quitter `convex/schema.ts`. La valeur `recording` de `sessions.kind`, elle, a
pu partir tout de suite : aucun code ne l'a jamais écrite.

`convex/legacyRecordingRetired.test.ts` exerce ces deux mutations en mémoire
(`npm run test:convex`). Ce n'est pas une preuve sur des données réelles.

---

## 9. Défauts connus et reste à faire

### Migration des credentials avec ANH-82 — opération à réaliser

Le correctif logiciel refuse les credentials historiques dès son déploiement.
Il ne transforme pas les clés existantes et ne les accepte jamais en secours.
Les nouvelles colonnes sont optionnelles pour permettre le chargement des
anciens documents ; cela ne leur accorde aucun droit d'authentification.

1. Le responsable ANH-82 valide une fenêtre d'interruption et les machines à
   reprovisionner. Préparer un environnement de recette synthétique et une
   version de repli conservant le nouveau vérificateur. Ne pas exporter les
   anciennes clés, ni les placer dans des tickets, logs, captures ou rapports.
2. Valider le schéma additif et l'index avant la bascule. Sur une grande table,
   préparer d'abord l'index avec `staged: true`, attendre son remplissage puis
   l'activer avant de déployer les queries qui l'utilisent. Le correctif livré
   utilise un index actif ; cette étape préparatoire dépend du volume réel.
3. Après confirmation humaine de l'opération de production, déployer le
   correctif et régénérer chaque clé via l'écran existant, avec un compte autorisé.
   Transmettre le résultat unique au responsable de la machine par le canal de
   provisionnement approuvé, puis mettre à jour sa configuration locale. En cas
   de perte du résultat, régénérer à nouveau ; aucune récupération n'est possible.
4. Vérifier un heartbeat avec la nouvelle clé et **401** avec l'ancienne.
   Traiter aussi les machines supprimées/désactivées : remplacer leur valeur
   historique pour éliminer le stockage réversible, sans les réactiver ni
   distribuer leur credential. Le responsable contrôle l'absence de valeurs
   historiques via des comptes agrégés, sans exporter les champs de clés.
5. En cas d'échec, laisser la synchronisation interrompue ou revenir uniquement
   à une version qui conserve ce schéma et ce vérificateur. Ne jamais restaurer
   la base64 réversible, les anciennes clés ou l'ancien code d'authentification.
   Reprendre le provisionnement/régénérer si nécessaire. Les sauvegardes
   historiques restent sensibles et suivent la politique de rétention du
   responsable ; le correctif ne prétend pas les purger.

Les suites `convex/machineAuth.test.ts` et `convex/machineCredential.test.ts`
exercent ces comportements sur fixtures synthétiques avec les fonctions et
routes enregistrées (`npm run test:convex`). Elles ne prouvent ni une migration
réelle ni un déploiement. ANH-121 reste incomplet jusqu'à l'opération ANH-82.

### Constats historiques et suivi

Constats initiaux relevés à la lecture ; les lignes 1 et 2 ont désormais des
régressions locales exécutables.

| # | Constat | Conséquence |
|---|---|---|
| 1 | **Correctif logiciel ANH-121 : digest HMAC-SHA-256 salé.** | Les anciennes valeurs doivent encore être régénérées lors de l'opération ANH-82 ; aucune migration réelle n'est attestée ici. |
| 2 | **Correctif logiciel ANH-121 : suppression/désactivation refusée.** | Les routes renvoient 401 ; le déploiement reste à effectuer avec ANH-82. |
| 3 | ~~`users.getCurrentUser` : validateur de retour incomplet.~~ **Corrigé.** Le validateur déclare `hrMax` et `birthYear`. | Vérifié sur le déploiement de développement le 1er octobre 2026 : la query répond après réglage de la FC max du compte. |
| 4 | **Séances locales sans pratiquant.** Le Pi n'envoie ni `userId` ni `subjectLabel` à `/training/local` (le champ existe côté Convex). | Une séance démarrée à la machine apparaît sans pratiquant (« Unknown » dans les listes). Le patient ne la voit pas dans ses séances. |
| 5 | **`/api/machine/roster` inutilisé.** | La liste des pratiquants autorisés n'arrive pas sur la console locale. |
| 6 | **Libellés d'actions de sécurité.** Le Pi envoie `freeze`, `quick_stop`, `go_silent` ; les traductions du site connaissent `hold`, `stop`, `estop`. Le commentaire du schéma cite aussi `"hold"`. | Ces trois actions s'affichent en valeur brute. |
| 7 | **Pas d'ECG pour les séances d'entraînement.** La console locale n'envoie que la télémétrie à 1 Hz. | Pas de tracé ECG, pas de résumé et pas de rapport pour une séance auto/manuelle : le résumé et le rapport viendront de l'enregistrement de séance (ANH-89). |
| 8 | **Pas d'amorçage d'admin ni d'invitation.** | Voir [§3](#3-règles-dautorisation) et `createPatient`. Un patient pré-créé qui s'inscrit reçoit une **seconde** ligne `users`, car `linkPatientToClerk` n'est jamais appelé. |
| 9 | **Historique non purgé.** | `machine_heartbeats` grossit d'une ligne toutes les 10 s par machine. |
| 10 | **Tests automatisés Convex présents**, dont authentification machine et confidentialité des séances ; déploiement ANH-82 encore requis. | `npm run test:convex` rejoue les scénarios synthétiques ; ce n'est pas une preuve de production. |
| 11 | **Séance orpheline.** Si la console s'arrête pendant une séance (arrêt du conteneur, coupure), elle n'envoie pas `/training/end`, et ne le rattrape pas au redémarrage. Trouvé à l'essai du 1er octobre 2026. | La séance reste `active` dans Convex indéfiniment. La machine repasse `online` au redémarrage. |

Fait le 1er octobre 2026 : déploiement sur l'environnement de développement,
essai de bout en bout avec un Pi en simulation complète. Reste à faire, dans
l'ordre logique : corriger 1, 2, 4 et 11 ; écrire des tests Convex ; déployer
en production. Le site a été ouvert contre le développement le 2 octobre 2026
(voir le [guide du tableau de bord](guides/guide-tableau-de-bord.md#94-ce-que-les-vrais-écrans-ont-montré-2-octobre-2026)). Voir aussi [securite.md](securite.md).

---

## 10. Tests automatisés

Les tests Convex s'exécutent **entièrement en mémoire** avec `convex-test` ; ils
ne touchent aucun déploiement et n'exigent ni réseau ni compte.

### Lancer

```bash
npm run test:convex
```

C'est aussi le job `convex-tests` de l'intégration continue, déjà requis.

### Les suites

| Fichier | Rôle |
|---|---|
| `convex/test.setup.ts` | Fabriques partagées : un monde d'un centre (admin, deux gestionnaires, trois patients, deux machines, droits de lancement, profils) et un monde de machines avec de vraies clés pour les routes HTTP. Nom à deux points : non déployé. |
| `convex/authorization.matrix.ts` | La **matrice d'autorisation** : la politique de chaque fonction publique, une ligne par rôle (et par côté quand l'accès dépend de la propriété). Nom à deux points : non déployé. |
| `convex/authorization.matrix.test.ts` | Parcourt la matrice : un test par cellule. |
| `convex/httpRoutes.test.ts` | Les 9 routes machine de `http.ts` : corps mal formés, idempotence, liaison ressource-machine, filtrage des séances. |
| `convex/legacyRecordingRetired.test.ts` | Retrait de l'ancien mode ECG : routes disparues (404), modules réduits à leurs lectures, aucune écriture dans `ecg_data` ni `session_summaries`, rien de planifié en fin de séance, et les deux mutations de migration. |
| `convex/contract.test.ts`, `convex/contractSource.test.ts` | Le contrat versionné (ANH-133) : 426 sur chaque route sans majeure servie, clé vérifiée avant le contrat, rien d'écrit pour une requête refusée, `server_contract_version` dans le poll, un code stable pour chaque refus, versions stockées à chaque heartbeat. Le second fichier remplace `contracts/machine-api.json` par un autre contrat et vérifie que le code le suit : la valeur est bien lue dans ce fichier. |
| `convex/crons.test.ts` | Le cron `check-offline-machines`. |
| `convex/machineEdit.test.ts` | Ce que fait le formulaire de machine pour un gestionnaire : `machines.updateMachine` enregistre le nom et le lieu sans toucher aux liens, et `machines.assignMachineToGestionnaires` reste réservé à l'admin (ANH-155). |
| `convex/completeness.test.ts` | Échoue si une fonction publique ou une route n'a pas de cellule de matrice. |
| `convex/machineAuth.test.ts`, `convex/machineCredential.test.ts` | Authentification et clés machine (ANH-121, complétés par ANH-132). |
| `convex/trainingPrivacy.test.ts`, `convex/sessions.test.ts` | Confidentialité des mesures live et des séances (ANH-71). |
| `convex/gestionnaireMachines.test.ts` | `machines.setGestionnaireMachines` : deux gestionnaires sur une machine (retirer l'un ne touche pas l'autre), ajout, liens existants conservés, refus sans écriture (ANH-154). |
| `convex/softwareReleases.test.ts` | Le registre des versions : ce que `recordRelease` accepte et refuse, une ligne par version, et chaque case de la règle « quelle machine reçoit quelle version » (ANH-134). |
| `convex/cloudVersion.test.ts` | La constante `CLOUD_VERSION` est celle de `convex/VERSION` et celle que répond le code déployé (ANH-134). |

### Lire la matrice

Chaque entrée de `authorization.matrix.ts` décrit une fonction publique :
son identifiant (`module.fonction`), son type, une fonction `build` qui prépare
les arguments, et une liste de `cases`. Une cellule est un acteur nommé
(`admin`, `manager`, `otherManager`, `patient`, `otherPatient`, `stranger`, ou
`anonymous`) avec une portée (`own` / `other` / `self`) et un résultat attendu :

- `refuse` : l'appel lève une erreur (avec, si utile, un extrait du message ;
  les fonctions publiques n'ont **aucun code d'erreur stable**, seulement des
  messages anglais ; seules les routes machine en ont, section 6) ;
- `success` : l'effet ou la donnée renvoyée est vérifié ;
- `empty` : la requête renvoie `null` ou `[]` (pas d'accès, rien n'est exposé) ;
- `filtered` : la liste renvoyée contient la donnée du demandeur et exclut
  celle des autres.

Une cellule marquée `knownDefect` est un **défaut inoffensif connu** : le test
affirme la politique **voulue** et tourne avec `it.fails`, donc il passe tant
que le défaut existe et échoue bruyamment le jour où le comportement est corrigé.

Chaque cellule agit sous l'identité de son acteur (sujet = acteur). Une fonction
dont la règle lit une revendication du jeton (par exemple l'e-mail vérifié de
l'appelant pour `users.linkPatientToClerk`) ajoute ces revendications par un
`claims` optionnel sur l'entrée. Elles s'ajoutent à l'identité de l'acteur sans
jamais remplacer son sujet, et l'acteur `anonymous` n'en porte aucune : `as`
refuse les deux cas.

Les rôles existants aujourd'hui sont `admin`, `gestionnaire` et `user`, plus
l'appelant anonyme. Il n'y a pas encore de dimension organisation
([ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site)) :
les acteurs nommés portent rôle et relation, de sorte qu'une organisation
s'ajoutera plus tard comme nouveaux acteurs sans réécrire les tests.

### Ajouter une fonction ou une route

- Nouvelle fonction publique (`query`/`mutation`/`action`) : ajouter une entrée
  dans `authorization.matrix.ts` (identifiant, `ref`, `build`, `cases`, et un
  `onSuccess`/`onFiltered` qui vérifie le comportement), sinon
  `completeness.test.ts` échoue.
- Nouvelle route de `http.ts` : l'ajouter à `ROUTE_COVERAGE` et lui écrire un
  test dans `httpRoutes.test.ts`, sinon la gate de complétude échoue.

---

## 11. Versions et compatibilité

Le contrat entre une machine et Convex porte un numéro `majeure.mineure`. Sa
définition unique est le fichier [`contracts/machine-api.json`](../contracts/machine-api.json)
à la racine du dépôt : `convex/lib/contract.ts` le lit directement (version,
nom de l'en-tête, liste des codes d'erreur), et les constantes de
`raspberry-pi/src/contract.py` y sont épinglées par les tests du Pi.

| Règle | Effet |
|---|---|
| Même **majeure** des deux côtés | Les deux se parlent. |
| **Mineure** différente, même majeure | Acceptée dans les deux sens : dans une majeure, chacun ne s'appuie que sur ce que toutes ses mineures fournissent. |
| Majeure de la machine absente, illisible ou non servie | Convex répond **426** `contract_unsupported` à **toute** route machine, sans rien lire ni écrire d'autre. |
| Majeure du serveur différente de celle de la machine, ou non annoncée | Le Pi **n'arme aucun lancement distant** venu de cette réponse, l'affiche sur la console et renvoie le lancement comme séance échouée (voir [raspberry-pi.md](raspberry-pi.md#14-versions-et-compatibilité)). |

Une évolution compatible (un champ optionnel de plus, un code d'erreur de plus)
monte la **mineure**. Tout ce qui change le sens d'un champ existant, en retire
un, ou rend obligatoire ce qui ne l'était pas, monte la **majeure** : les deux
côtés doivent alors être livrés ensemble, et une machine restée sur l'ancienne
majeure est refusée au lieu d'être comprise de travers.

### Matrice de compatibilité

| Version du Pi (`raspberry-pi/VERSION`) | Majeure de contrat | Version Convex minimale |
|---|---|---|
| `pi-0.0.0-dev` (développement, avant la première release) | 1 (contrat `1.0`) | le code de la branche `develop` qui sert la majeure 1 (aucune version Convex n'est encore numérotée) |

Cette matrice est tenue à jour par le processus de release (ticket ANH-134) :
une ligne par version publiée du Pi. Tant que ce processus n'existe pas, elle
ne contient que la ligne de développement.

Limites à connaître :

- Un Convex antérieur à ce contrat n'annonce pas sa version : un Pi de cette
  branche qui l'interroge n'arme donc **aucun lancement distant** (réponse sans
  version). C'est le cas tant que le nouveau code n'est pas déployé ; mettre
  les deux côtés à niveau relève du redéploiement (ticket ANH-82).
- La « version attendue » d'une machine n'est définie nulle part aujourd'hui :
  la fiche machine du site **affiche** la version annoncée, sans la comparer.
  La comparaison et son badge viendront avec le registre machine (ticket
  ANH-147).
- `medical_parameters_version` et `config_hash` sont acceptés dans le heartbeat
  et **ne sont pas stockés** : le Pi n'en a pas encore (il envoie `null`).

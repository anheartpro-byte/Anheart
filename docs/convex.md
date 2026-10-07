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

### `organizations` : les clients

Une organisation est un client (un centre). C'est le **miroir** d'une
organisation Clerk : Clerk porte l'appartenance, les rôles et les invitations ;
Convex reste l'autorité sur les données.

| Champ | Type | Rôle |
|---|---|---|
| `clerkOrgId` | string, optionnel | Identifiant de l'organisation dans Clerk (revendication `org_id` du jeton). Absent seulement sur l'organisation par défaut créée par la migration, tant qu'elle n'est pas reliée à Clerk. |
| `name`, `slug` | string | Nom et identifiant court. |
| `createdAt` | number | ms Unix. |
| `settings` | objet | `requirePrescription` (booléen, `false` par défaut) et `language` (`"fr"` \| `"en"`). |

Index : `by_clerk_org_id`, `by_slug`.

### `memberships` : qui appartient à quelle organisation

Miroir des appartenances Clerk. Un compte peut appartenir à plusieurs
organisations.

| Champ | Rôle |
|---|---|
| `userId`, `organizationId` | Le compte et l'organisation. |
| `role` | `"admin"` \| `"gestionnaire"` \| `"user"` : le rôle tenu **dans cette organisation** (miroir de `org:admin`, `org:gestionnaire`, `org:patient`). |
| `active` | `false` garde la trace d'un retrait. Une appartenance inactive ne compte plus pour les autres (le compte n'est plus un membre qu'on peut lister, lier ou désigner) ni pour le compte lui-même pendant la transition sans jeton d'organisation. Elle **ne coupe pas** un appelant dont le jeton nomme encore l'organisation : le jeton fait foi, et le retrait prend effet quand Clerk cesse de délivrer la revendication, c'est-à-dire à l'expiration du jeton en cours. |

Index : `by_user`, `by_organization`, `by_user_and_organization`.

### `organizationId` : à quelle organisation appartient une ligne

Huit tables portent un champ `organizationId` et un index `by_organization` :
`users` (organisation principale du compte), `machines`, `sessions`,
`training_telemetry`, `machine_profiles`, `machine_user_permissions`,
`machine_gestionnaires` et `user_gestionnaires`. Toute table créée ensuite le
porte aussi, sauf une table **commune à tous les clients**.

Les tables qui ne le portent pas : `software_releases`, le registre des
versions, commun à tous les clients et réservé à l'admin Anheart ; et
`ecg_data`, `session_summaries`, `machine_heartbeats`, qu'on n'atteint que par
leur séance ou leur machine, donc par l'organisation de celle-ci.

- Une **machine appartient à exactement une organisation**. Ce qu'elle écrit
  par les routes machine (séances locales, télémétrie, programmes) et ce qui
  s'y rattache (liens, droits de lancement, séances lancées du site) **hérite de
  l'organisation de la machine**, calculée sur le serveur.
- Le champ est **optionnel dans le schéma** pour que les lignes écrites avant
  la migration se chargent encore. Une ligne sans organisation n'est servie à
  personne, sauf à l'admin Anheart ([§3](#3-règles-dautorisation)).
- La migration `migrations/multiOrganization:attachExistingRowsToAnheart`
  crée l'organisation « Anheart » et y rattache toutes les lignes existantes
  ([§8](#activer-le-multi-organisation)).

### `users` : comptes

| Champ | Type | Rôle |
|---|---|---|
| `clerkId` | string | Identifiant Clerk (`identity.subject`). Chaîne vide pour un patient créé par un gestionnaire et pas encore lié. |
| `role` | `"admin"` \| `"gestionnaire"` \| `"user"` | **Miroir en lecture seule** du rôle du compte. `user` = patient / pratiquant. Il sert à l'affichage ; il n'est **jamais lu pour autoriser** l'appelant ([§3](#3-règles-dautorisation)). |
| `organizationId` | id organizations, optionnel | Organisation principale du compte. |
| `gestionnaireId` | id users, optionnel | **Ancien champ**, gardé pour compatibilité ; remplacé par la table `user_gestionnaires`. |
| `firstName`, `lastName`, `email` | string | Identité. |
| `language` | `"fr"` \| `"en"` | Langue. |
| `hrMax` | number, optionnel | FC max **mesurée** (bpm), un entier. Prioritaire sur l'estimation. |
| `birthYear` | number, optionnel | Année de naissance, un entier. Sert à l'estimation Tanaka et au contrôle d'âge. |
| `createdAt` | number | ms Unix. |

Index : `by_clerk_id`, `by_gestionnaire`, `by_role`, `by_organization`.

### `user_gestionnaires` : quel gestionnaire suit quel patient

Relation plusieurs-à-plusieurs : `userId` (patient), `gestionnaireId`,
`createdAt`, `createdBy`. Index `by_user`, `by_gestionnaire`,
`by_user_and_gestionnaire`. Le lien vit dans **une** organisation
(`organizationId`) : le même patient et le même gestionnaire peuvent être liés
dans deux organisations, par deux lignes distinctes, et un lien ne vaut que
dans la sienne.

### `machine_gestionnaires` : quel gestionnaire gère quelle machine

`machineId`, `gestionnaireId`, `isOwner` (le premier gestionnaire assigné à la
création), `createdAt`, `createdBy`. Index `by_machine`, `by_gestionnaire`,
`by_machine_and_gestionnaire`.

### `machine_user_permissions` : droits de lancement

Quel patient (`userId`) peut lancer **lui-même** une séance auto sur quelle
machine (`machineId`). `grantedBy`, `createdAt`. Index `by_machine`, `by_user`,
`by_machine_and_user`. Voir [§3](#3-règles-dautorisation).

### `software_releases` : versions publiées

Une ligne par version publiée d'un composant. Écrite seulement par l'admin
Anheart (`softwareReleases.recordRelease`), à la fin d'une release
([release.md](release.md#6-enregistrer-la-version-dans-convex)). Le registre
vaut pour **tous les clients** : la table n'a pas d'`organizationId`, et
l'admin d'une organisation cliente ne la lit ni ne l'écrit.

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
| `startedAt`, `endedAt` | ms Unix, **sur l'horloge du serveur** : le site les lit comme des dates du serveur. La façon dont le serveur les obtient d'une machine dont l'horloge peut être fausse est décrite dans [Deux horloges](#deux-horloges). |
| `machineStartedAt` | Le début tel que **la machine** l'a daté, sur son horloge (ms Unix). Les points et les événements de la séance sont datés sur cette même horloge. Absent quand la machine n'a donné aucune date (lancement du site confirmé par une console antérieure au contrat 1.1) ; une séance enregistrée par une machine avant ce champ porte cette date dans `startedAt`. |
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

`sessionId`, `machineId`, `t`, `elapsedS`, `phase`, `bpm` (absent = pas de FC
fiable), `motorRpm`, `outputRpm`, `setpointMotorRpm`, `gLoad`, `safetyAction`.
Index `by_session_and_t`.

**Une ligne par `(sessionId, t)`.** Un point envoyé une seconde fois (réponse
perdue, console redémarrée) n'est pas inséré de nouveau : la ligne déjà là
reste telle qu'elle est, avec la date à laquelle le serveur l'a reçue la
première fois (`storeTelemetry`).

`t` est une date en ms Unix **sur l'horloge de la machine** : le début de la
séance tel qu'elle l'a daté, plus le temps écoulé. C'est l'identité du point
et son rang dans la séance. Les lectures ne le servent pas tel quel :
`getSessionTelemetry` et `lastMeasuredAt` le placent sur l'horloge du serveur
(voir [Deux horloges](#deux-horloges)). Il ne dit pas qu'une séance envoie
encore : la date de **réception** d'un point est le `_creationTime` que Convex
donne à sa ligne (`lastSignalAt`). Le site exige les deux, mesure et
réception, pour afficher une valeur comme actuelle (voir `getTrainingSession`).

### `training_events` : événements d'une séance

Les événements de l'enregistrement local d'une séance, tels que la machine les
relit dans son journal : verdicts, refus, phases, défauts du variateur,
commandes distantes, pré-vol, avertissements, fin.

`sessionId`, `machineId`, `seq`, `t` (ms Unix sur l'horloge de la machine,
comme `training_telemetry.t`), `kind`, `detail`, `actor` (`system`, `remote`
ou un identifiant opaque d'opérateur, jamais un nom). Index
`by_session_and_seq` et `by_organization`.

`seq` est le rang de l'événement dans l'enregistrement. **Une ligne par
`(sessionId, seq)`** : un événement envoyé une seconde fois n'est pas inséré
de nouveau (`storeEvents`). Aucune fonction publique ne lit encore cette
table : la machine l'écrit, les tests la lisent.

### Deux horloges

Un Raspberry Pi n'a pas d'horloge sauvegardée. Démarré sans réseau, il date ce
qu'il mesure d'une heure fausse, et son horloge est corrigée d'un coup quand
le réseau revient. C'est un cas ordinaire. Convex ne compare donc jamais
l'heure d'une machine à la sienne, ni pour accepter un point, ni pour dire
qu'une mesure est actuelle.

- **La machine date ses mesures sur son propre axe.** Le `t` d'un point ou
  d'un événement est le début de la séance tel que la machine l'a daté, plus
  le temps écoulé depuis. Ce début est gardé dans `sessions.machineStartedAt`.
- **Le serveur date la séance.** `sessions.startedAt` et `endedAt` sont sur
  l'horloge du serveur. La machine dit depuis combien de temps la séance a
  commencé, compté sur son horloge monotone (`sessionAgeMs`) : le début est
  cette durée avant la réception, et l'heure murale de la machine n'est pas
  lue. Sans `sessionAgeMs` (console antérieure au contrat 1.1, ou qui ne peut
  plus le dire), le serveur garde la date écrite par la machine ; si cette
  date est antérieure au 1er janvier 2024 (`EARLIEST_MACHINE_DATE_MS` : une
  horloge jamais réglée), il date la séance à sa déclaration.
- **Le décalage entre les deux** (`startedAt - machineStartedAt`) place toute
  date de la machine sur l'horloge du serveur : le `t` servi par
  `getSessionTelemetry` (et son `sinceT`), `lastMeasuredAt`, et `endedAt`.
  Une séance d'une machine datée de 1970 s'affiche à la date du serveur, et
  ses mesures en direct se lisent comme mesurées maintenant.
- **Une séance sans axe machine** (lancement du site confirmé par une console
  qui n'envoie aucune date) : rien n'est décalé, ses dates sont servies comme
  la machine les a écrites.

Ce que cela ne fait pas : après un redémarrage **du système** de la machine
(et non du seul logiciel), la console ne sait plus dire depuis combien de
temps une séance d'avant le redémarrage a commencé. La date écrite par la
machine est alors gardée telle quelle si elle est postérieure au 1er janvier
2024, même fausse de quelques heures ou de quelques jours.

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

### D'où viennent l'organisation et le rôle d'un appel

Chaque appel se fait **dans une organisation, avec un rôle**. Les deux viennent
de l'identité vérifiée (`ctx.auth.getUserIdentity()`), c'est-à-dire des
revendications `org_id` et `org_role` du modèle JWT `convex` de Clerk, et de
lignes que seul le serveur écrit. **Jamais d'un argument, jamais d'un champ
qu'un utilisateur peut modifier.** `users.role` et `users.organizationId` ne
sont pas lus pour autoriser l'appelant quand le jeton porte une organisation.

| Revendication `org_role` | Rôle tenu dans l'organisation | Rôle de l'appel |
|---|---|---|
| `org:admin` dans l'organisation Anheart | `admin` | **`admin`** : admin Anheart, toutes les organisations |
| `org:admin` dans une autre organisation | `admin` | **`org_admin`** : admin de cette organisation seulement |
| `org:gestionnaire` | `gestionnaire` | `gestionnaire` |
| `org:patient` | `user` | `user` |

L'**organisation Anheart** est celle dont l'identifiant Clerk est dans la
variable d'environnement `ANHEART_ORG_ID` du déploiement Convex. Elle seule
donne le rôle `admin`. Dans le code, `admin` désigne donc toujours l'admin
Anheart : une règle écrite pour `admin` n'ouvre jamais une autre organisation à
un admin d'organisation.

Un appel que le serveur ne sait pas placer dans une organisation est **refusé**,
avec un message explicite (`ConvexError`, transmis au navigateur) :

| Situation | Message |
|---|---|
| `org_id` absent, `null` ou vide alors que `ANHEART_ORG_ID` est définie | `No active organization: select an organization to continue` |
| `org_id` inconnu du miroir `organizations` | `This organization is not known to the server yet` |
| `org_role` absent ou autre que les trois rôles ci-dessus | `Your role in this organization is not recognized` |
| `org_id` qui n'est pas une chaîne | `Malformed organization claim` |
| Compte sans organisation ni appartenance active (transition) | `Your account does not belong to an organization` |

`users.getCurrentUser` ne lève pas dans ces cas : il renvoie le compte avec
`organization: null` et `role: "user"`, pour que le site sache quoi afficher.

Quand le jeton nomme une organisation connue, **le jeton seul décide** pour
l'appelant : son appartenance dans le miroir `memberships` n'est pas consultée,
qu'elle soit absente ou inactive. Un retrait fait dans Clerk prend donc effet
à l'expiration du jeton en cours, pas avant. Le miroir sert à qualifier les
**autres** comptes (qui est membre, avec quel rôle) et, pendant la transition,
l'appelant sans jeton d'organisation.

### Transition : jeton sans organisation

Tant que Clerk Organizations n'est pas configuré, les jetons ne portent aucune
revendication d'organisation. La règle est la suivante.

- **`ANHEART_ORG_ID` non définie** (déploiement pas encore configuré) : un jeton
  sans `org_id` agit dans l'**organisation principale du compte**
  (`users.organizationId`, écrite par la migration) avec le rôle de son
  **appartenance active** (`memberships`). Les deux sont écrits par le serveur
  seul. L'admin de l'organisation par défaut « Anheart » créée par la migration
  est alors l'admin Anheart. Sans organisation ou sans appartenance active,
  l'appel est refusé. Le déploiement mono-organisation se comporte donc comme
  avant, **une fois la migration passée** ; avant, toute fonction liée à une
  organisation refuse.
- **`ANHEART_ORG_ID` définie** : un jeton sans `org_id` est refusé. Le miroir
  n'est plus jamais utilisé à la place du jeton.

Ce choix (accepter le miroir tant que la variable n'est pas définie) est une
**décision du responsable produit** ; l'alternative est de refuser tout jeton
sans organisation dès le déploiement, ce qui impose de configurer Clerk avant.

### Les règles

Fonctions de `convex/lib/auth.ts`. Chaque règle répond pour **une**
organisation : une ressource d'une autre organisation est refusée comme une
ressource qui n'existe pas.

| Fonction | Vraie si |
|---|---|
| `requireAuth` | Un jeton Clerk valide est présent (sinon `Not authenticated`). |
| `getCurrentUserOrThrow` | Une ligne `users` existe pour ce `clerkId` (sinon « User not found in database. Please complete registration. ») et l'appel a une organisation acceptée (voir ci-dessus). Renvoie le compte avec le rôle et l'organisation **de l'appel**. |
| `requireRole(r)` | Le rôle de l'appel est dans `r`. |
| `inScope(organisation)` | admin Anheart ; ou la ligne appartient à l'organisation de l'appelant. Une ligne sans organisation n'est dans le périmètre que de l'admin Anheart. |
| `canManageUser(cible)` | admin Anheart ; ou, si la cible est **membre actif de l'organisation de l'appelant** : admin de cette organisation, ou gestionnaire lié à la cible dans `user_gestionnaires` **dans cette organisation**. |
| `canAccessUser(cible)` | soi-même, ou `canManageUser`. |
| `canAccessMachine(m)` | admin Anheart ; ou, si la machine appartient à l'organisation de l'appelant : admin de cette organisation, ou gestionnaire lié à `m` dans `machine_gestionnaires`. **Un `user` n'a jamais accès par cette règle.** |
| `canManageMachine(m)` | Identique à `canAccessMachine`. |
| `canAccessSession(s)` | admin Anheart ; ou, si la séance appartient à l'organisation de l'appelant : son pratiquant, ou qui a accès à sa machine. |
| `requireGestionnaireAdmin(g)` | admin Anheart, ou admin de l'organisation de `g` ; et `g` tient le rôle `gestionnaire` dans l'organisation où la décision s'applique : celle de l'appelant, ou, pour l'admin Anheart, l'organisation principale de `g`. Sinon « Gestionnaire not found » (compte inconnu, ou, pour un admin d'organisation, compte qui n'est pas membre actif de la sienne : même réponse) ou « Target user is not a gestionnaire ». Le rôle de l'appelant est vérifié avant toute lecture de `g`, et le rôle de `g` est lu dans son appartenance, pas dans le miroir `users.role`. Renvoie l'appelant, `g` et cette organisation. |

Ce que fait un **admin d'organisation** (`org_admin`) : dans son organisation,
il lit et gère toutes les machines, tous les membres et toutes les séances sans
avoir besoin d'un lien, et gère les liens gestionnaire. Restent réservés à
l'admin Anheart : créer une machine, restaurer ou voir une machine supprimée,
changer un rôle pendant la transition, et tout ce qui traverse les
organisations.

Règles propres aux séances d'entraînement (`convex/training.ts`) :

| Action | Qui |
|---|---|
| Voir la disponibilité et les programmes d'une machine | admin, gestionnaire de la machine, ou patient détenant le droit de lancement sur cette machine. |
| Lire ses mesures live | admin, gestionnaire de la machine, ou pratiquant de la séance live sur cette même machine. Un droit de lancement seul ne donne pas accès aux mesures d'un autre pratiquant. |
| **Accorder / retirer un droit de lancement** | admin, ou gestionnaire de la machine. Pour accorder, le gestionnaire doit **aussi** gérer le patient (`canAccessUser`). Seul un `user` **de l'organisation de la machine** peut recevoir le droit : admins et gestionnaires l'ont déjà. |
| **Lancer une séance auto** | un `user` pour **lui-même seulement**, s'il a le droit sur la machine ; un admin ou un gestionnaire de la machine, pour lui-même ou pour un patient qu'il gère. Le pratiquant doit être membre actif de l'organisation de la machine, et un droit de lancement ne vaut que dans cette organisation. |
| **Arrêter / annuler** | le pratiquant de la séance, ou un admin / gestionnaire de la machine. |
| Régler FC max et année de naissance | admin, ou gestionnaire du patient. **Jamais le patient lui-même.** |
| Lire la télémétrie et le détail d'une séance | le pratiquant, ou admin / gestionnaire de la machine. |

Autres règles notables :

- `listLaunchableMachines` et `getMachineLive` gardent la disponibilité, les
  programmes et l'indicateur de péremption accessibles aux utilisateurs
  autorisés à lancer. Leur champ `live` vaut `null` si le demandeur n'a pas le
  droit de lire cette séance. Un identifiant de séance absent, invalide,
  supprimé ou appartenant à une autre machine ne débloque jamais ce champ pour
  un simple utilisateur. Les mesures live ne sortent jamais de l'organisation
  de la machine. Les tests `convex/trainingPrivacy.test.ts` passent par
  les vrais handlers et les tables Convex en mémoire, avec identités Clerk
  synthétiques, sur un déploiement mono-organisation après migration ; la
  matrice par organisation est décrite en [§10](#10-tests-automatisés).

- **Créer une machine** : admin Anheart seulement, dans l'organisation qu'il
  désigne (`organizationId`, par défaut la sienne). C'est la seule fonction
  publique qui prend une organisation en argument.
- **Rôles** : dès que `ANHEART_ORG_ID` est définie, un rôle est un miroir en
  lecture seule de Clerk et `users.updateUserRole` refuse (« Roles are managed
  in Clerk Organizations »). Avant, l'admin Anheart change le rôle d'un membre
  de son organisation (l'appartenance et son miroir `users.role`).
- **Premier passage d'un compte** (`users.getOrCreateUser`) : le compte rejoint
  l'organisation que nomme son jeton, **si le miroir la connaît déjà**, avec le
  rôle que porte le jeton, et son appartenance est écrite dans le miroir. Une
  organisation n'est jamais créée à partir d'un jeton. Sans organisation dans le jeton et
  tant que `ANHEART_ORG_ID` n'est pas définie, il rejoint l'organisation par
  défaut comme **`user`**. Sinon il est créé sans organisation.
- **Premier admin** : avec Clerk Organizations, c'est le premier `org:admin` de
  l'organisation Anheart dans Clerk. Pendant la transition, il faut encore
  passer à la main `role` à `admin` dans le tableau de bord Convex, sur la
  ligne `memberships` du compte (et sur `users` pour l'affichage).
- **Retirer un compte** (`users.deleteUser`) : un admin d'organisation ou un
  gestionnaire retire le compte de **son** organisation seulement
  (appartenance, liens et droits de lancement de cette organisation) ; le
  compte lui-même n'est supprimé que s'il n'appartient à aucune autre.
  L'admin Anheart le supprime partout.
- **E-mail déjà utilisé** (`users.createPatient`) : le message ne nomme le
  compte en conflit qu'à un appelant de la même organisation ; sinon il dit
  seulement « Email already in use ».
- **Lier un dossier patient à un compte Clerk** (`users.linkPatientToClerk`) :
  la liaison exige l'adresse **vérifiée** de l'appelant. Elle n'aboutit que si
  l'e-mail vérifié de l'identité (claims `email` et `email_verified`) est celui
  du dossier ; la comparaison ignore la casse des lettres ASCII seulement, et
  deux adresses qui diffèrent par un caractère non ASCII restent distinctes.
  L'argument `email` ne fait pas autorité. Sans e-mail vérifié la liaison est
  refusée, un dossier déjà lié n'est jamais relié, et la réponse ne distingue
  pas « aucun dossier » de « e-mail différent ». Quand le jeton nomme une
  organisation, seul un dossier **de cette organisation** est lié.
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
| `LIVE_FRESH_MS` | 90 000 ms | Au-delà, l'état en direct est « périmé » et la machine « hors ligne ». Une seule définition, dans `lib/training.ts`, importée par `convex/training.ts`, par la tâche `checkOfflineMachines` de `convex/machines.ts` et par le site, qui recalcule la fraîcheur chaque seconde sur l'horloge du serveur ([tableau-de-bord.md §6](tableau-de-bord.md#fraîcheur-recalculée-à-lhorloge)). `lib/training.ts` doit donc rester sans import `@/` ni code réservé au navigateur. |

**Valeur utilisable.** Une FC max et une année de naissance sont des **entiers
finis** : des battements par minute et des années se comptent.
`setUserPhysiology` n'enregistre rien d'autre, et toute lecture d'un compte
applique la même règle : une valeur enregistrée qui n'est pas un entier fini
compte comme **non renseignée**. Les contrôles ci-dessous comparent donc
toujours des nombres. Pour les valeurs enregistrées avant ce contrôle, voir
[deploiement.md](deploiement.md#35-vérifier-la-physiologie-déjà-enregistrée).

**FC max retenue** (`effectiveHrMax`) : si une FC max mesurée est enregistrée,
c'est elle, à condition d'être un entier de 100 à 220 ; sinon rien (une valeur
mesurée inutilisable n'est pas remplacée par l'estimation). Sans FC max
mesurée, l'estimation de Tanaka `round(208 − 0,7 × âge)` avec
`âge = année courante − année de naissance`, pour un âge de 10 à 100 ans ;
sinon rien.

**Âge pour le contrôle** (`ageFrom`) : `année courante − année de naissance − 1`,
ou rien si l'année n'est pas renseignée ou n'est pas un entier fini. On suppose
l'anniversaire pas encore passé : l'âge n'est jamais surestimé.

**Refus de zone** (`zoneRefusal`), dans cet ordre :

1. `zoneHighBpm > floor(0,9 × FC max)` → « Zone up to … exceeds 90% of this
   rider's max heart rate … » ;
2. `hardMaxBpm > FC max` → « Programme hard maximum … is above this rider's max
   heart rate … ».

Chaque règle est écrite comme ce qui est accepté : un programme ne passe que si
sa valeur est connue comme inférieure ou égale à la limite. Une valeur qui
n'est pas un nombre, d'un côté ou de l'autre, donne donc un refus.

### Queries et mutations publiques

| Fonction | Type | Arguments | Autorisation | Effet / retour |
|---|---|---|---|---|
| `grantLaunchRight` | mutation | `machineId`, `userId` | admin ou gestionnaire de la machine **et** de l'utilisateur ; cible de rôle `user` | Insère le droit. Sans effet s'il existe déjà. |
| `revokeLaunchRight` | mutation | `machineId`, `userId` | admin ou gestionnaire de la machine | Supprime le droit s'il existe. |
| `listLaunchRights` | query | `machineId` | admin ou gestionnaire de la machine (sinon `[]`) | `[{userId, name, email, hrMax (retenue ou null), grantedByName, createdAt}]`. |
| `setUserPhysiology` | mutation | `userId`, `hrMax?` (nombre ou `null` pour effacer), `birthYear?` (idem) | pas un `user` ; `canAccessUser` | Pour chaque valeur fournie : `null` l'efface ; sinon elle doit être un entier fini, puis dans ses bornes (FC max 100-220, âge 10-100 ans). Rien n'est écrit si l'une des deux est refusée. Erreurs : « Only a manager can set physiology », « You do not manage this user », « Max heart rate must be a whole number of bpm », « Max heart rate must be within 100-220 bpm », « Birth year must be a whole number », « Birth year gives an implausible age ». |
| `listMachineProfiles` | query | `machineId` | voir la machine (règle entraînement) | Programmes triés par nom. |
| `listLaunchableMachines` | query | - | connecté | Machines où l'on peut lancer : admin Anheart = toutes ; dans l'organisation de l'appelant seulement : son admin = toutes, gestionnaire = les siennes, user = celles où il a le droit. Machines supprimées exclues. Pour chacune : `status`, `lastHeartbeat`, `serverNow` (voir `getMachineLive`), `programsEnabled`, `live` (ou `null` si plus vieux que 90 s quand la query s'exécute), `profiles`, `myHrMax` (FC max retenue de l'appelant). |
| `launchAutoSession` | mutation | `machineId`, `profileId`, `userId?`, `totalDurationS?`, `notes?` | voir §3 | Crée une séance `pending` (`kind: auto`, `origin: remote`). Retourne son id. Contrôles ci-dessous. |
| `requestStop` | mutation | `sessionId` | pratiquant ou admin / gestionnaire de la machine | `pending` → `failed` avec « Cancelled before start by … ». `active` → pose `stopRequestedAt` (une seule fois). Autres statuts : rien. |
| `getMachineLive` | query | `machineId` | voir la machine | `{status, programsEnabled, live, stale, serverNow}` ou `null`. `stale` = pas d'état ou plus vieux que 90 s **au moment où la query s'exécute** : elle ne se relance pas quand une machine se tait, le site recalcule donc la fraîcheur chaque seconde. `serverNow` = l'heure du serveur dans cette réponse : le site vieillit `live.updatedAt` à partir d'elle et du temps qu'il a compté depuis, jamais à partir de l'heure du poste. |
| `getSessionTelemetry` | query | `sessionId`, `sinceT?`, `limit?` | pratiquant ou admin / gestionnaire | Points du plus ancien au plus récent. `limit` par défaut 3600, borné à 1..7200 (les **derniers** points). Le `t` servi et `sinceT` sont sur l'horloge du serveur ([Deux horloges](#deux-horloges)). |
| `getTrainingSession` | query | `sessionId` | pratiquant ou admin / gestionnaire | Champs d'entraînement de la séance, nom de la machine, et `canStop` (statut `pending`/`active` et droit d'arrêt). Pour une séance **active**, deux dates du point de plus grand `t`, celui que le site affiche (`null` toutes les deux sinon). `lastSignalAt` = sa réception par le serveur (`_creationTime` de sa ligne, pas son `t`), ou, sans point, le début daté par le serveur (`startedAt` d'une séance lancée du site, `_creationTime` d'une séance enregistrée par la machine). `lastMeasuredAt` = sa mesure : son `t`, placé sur l'horloge du serveur ([Deux horloges](#deux-horloges)), `null` sans point : un point reçu à l'instant peut avoir été mesuré une heure plus tôt (envoi après une coupure ou un redémarrage). `serverNow` comme pour `getMachineLive`. Le site n'affiche une valeur comme actuelle que si les deux dates ont moins de 20 s sur `serverNow` ([tableau-de-bord.md §6](tableau-de-bord.md#panneau-dentraînement-vue-en-direct)). La query se relance à chaque paquet de points. |

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
   require at least 18 years ». Une valeur enregistrée qui n'est pas un entier
   fini compte comme non renseignée (voir « Valeur utilisable » plus haut) : le
   lancement est refusé par « The rider's max heart rate (or birth year) must
   be set… » pour la FC max, ou pour l'année de naissance sans FC max mesurée,
   et par « The rider's birth year must be set… » pour l'année de naissance à
   côté d'une FC max mesurée. Rien n'est mis en file.
5. Durée : « Duration must be positive » (un nombre fini, strictement positif).

La séance créée copie le programme (zone, durée, ou la durée demandée),
`subjectHrMax`, `subjectAge`, `subjectLabel` (nom du pratiquant) et
`operatorName` (nom du lanceur).

### Fonctions internes (appelées par les routes HTTP)

| Fonction | Rôle |
|---|---|
| `updateLive` | Écrit `machines.live` (et `programsEnabled` s'il est fourni). |
| `syncProfiles` | Supprime tous les programmes de la machine, insère la nouvelle liste (dans l'organisation de la machine), met à jour `programsEnabled`. Retourne `{count}`. |
| `getRoster` | Patients détenant le droit sur la machine **et membres actifs de son organisation** : `{userId, name, hrMax}`. |
| `getPendingTrainingSession` | Première séance `pending` de `kind: auto` de la machine, au format attendu par le Pi. |
| `markTrainingStarted` | `pending` → `active`, machine `in_session`. `startedAt` = la réception, ou `sessionAgeMs` plus tôt quand la machine le dit (jamais avant le lancement) ; `machineStartedAt` = la date écrite par la machine, si elle en envoie une. Refuse une séance d'une autre machine ou non `pending`. |
| `registerLocalSession` | Crée (une seule fois par `localRef`) une séance `active`, `origin: local`, dans l'organisation de la machine, et passe la machine `in_session`. Le pratiquant nommé par la machine n'est gardé que s'il est membre actif de cette organisation ; la séance est enregistrée dans tous les cas. La date envoyée par la machine devient `machineStartedAt` ; `startedAt` est daté par le serveur ([Deux horloges](#deux-horloges)). Un second appel avec le même `localRef` renvoie la séance sans la redater. |
| `endTrainingSession` | `completed` ou `failed` avec `endReason`, machine `online`. Idempotent. `endedAt` = la date de fin écrite par la machine, placée sur l'horloge du serveur et jamais avant `startedAt` ; la réception si la machine n'en envoie pas ; la date de la machine telle quelle pour une séance sans axe machine. Ne planifie rien : aucun résumé n'est calculé à la fin d'une séance. |
| `getTrainingStatus` | `{status, active, stopRequested}`. |
| `storeTelemetry` | Insère les points de la séance (qui doit appartenir à la machine), dans l'organisation de la séance, **une seule fois chacun**. Un point déjà stocké (même `(sessionId, t)`) est compté dans `duplicates` ; un point daté hors de la séance est compté dans `rejected` ; ni l'un ni l'autre n'est écrit. Retourne `{stored, duplicates, rejected}`. |
| `storeEvents` | La même chose pour les événements : un événement est un `(sessionId, seq)`. Retourne `{stored, duplicates, rejected}`. |

Les refus de `markTrainingStarted`, `registerLocalSession`,
`endTrainingSession`, `storeTelemetry` et `storeEvents` sont levés par
`machineError(code, message)` (`convex/lib/contract.ts`) : la route les
renvoie avec leur code stable (voir [section 6](#6-routes-http-machine-convexhttpts)).

---

## 5. Autres fonctions publiques

Résumé des fonctions les plus utilisées par le site. Dans ces tableaux,
« admin » sans précision veut dire l'admin Anheart **ou** l'admin de
l'organisation concernée, et toute lecture ou liste est limitée à
l'organisation de l'appelant, sauf pour l'admin Anheart
([§3](#3-règles-dautorisation)).

### `users.ts`

| Fonction | Autorisation | Rôle |
|---|---|---|
| `getOrCreateUser` (mutation) | connecté | Crée la ligne `users` au premier passage (langue `fr`), dans l'organisation et avec le rôle du jeton (voir [§3](#3-règles-dautorisation)), et met à jour le miroir de l'appartenance. Appelée **uniquement** par la page d'accueil quand l'utilisateur connecté n'a pas encore de ligne. |
| `getCurrentUser` (query) | - | Le compte courant ou `null`, avec `role` (rôle de l'appel : `admin`, `org_admin`, `gestionnaire`, `user`) et `organization` (`{_id, name, slug}`, ou `null` si le serveur n'accepte aucune organisation pour l'appel). |
| `updateUserRole` | admin Anheart, pendant la transition seulement | Change le rôle d'un membre de son organisation. Refuse dès que `ANHEART_ORG_ID` est définie. |
| `updateUserProfile` | connecté | Prénom, nom, langue de soi-même. |
| `listUsers` | connecté | admin Anheart : tous (ou ceux d'un gestionnaire) ; admin d'organisation : les membres actifs de son organisation ; gestionnaire : ses patients dans son organisation ; user : lui-même. Filtre `role` optionnel. `role` est le rôle tenu dans l'organisation de l'appelant. |
| `createPatient` | admin, gestionnaire | Crée un patient (`clerkId` vide) **dans l'organisation de l'appelant**, avec son appartenance. Un gestionnaire s'y lie automatiquement ; un admin peut lier plusieurs gestionnaires de cette organisation. Refuse un e-mail déjà utilisé. **Aucun e-mail d'invitation n'est envoyé**, malgré le texte affiché par le site. |
| `updatePatient` | admin, gestionnaire du patient | Modifie un patient. |
| `getUserById` | `canAccessUser` | Profil, y compris `hrMax`, `birthYear` et `effectiveHrMax`. |
| `deleteUser` | admin ; gestionnaire pour ses patients seulement | Retrait de l'organisation de l'appelant, ou suppression partout pour l'admin Anheart (voir [§3](#3-règles-dautorisation)). Pas soi-même. |
| `linkPatientToClerk` | connecté, e-mail vérifié | Lie un patient pré-créé (`clerkId` vide) au compte Clerk, uniquement si l'e-mail **vérifié** de l'appelant est celui du dossier (voir [§3](#3-règles-dautorisation)). **Aucune page ne l'appelle aujourd'hui.** |
| `assignGestionnaireToUser` / `removeGestionnaireFromUser` | admin ; un gestionnaire pour lui-même | Lien patient ↔ gestionnaire, dans l'organisation de l'appelant ; le patient et le gestionnaire doivent tous deux en être membres actifs, avec ces rôles. Pour l'admin Anheart, le lien se crée dans l'organisation principale du patient. |
| `listGestionnaires` | admin | Les gestionnaires (tous pour l'admin Anheart, ceux de son organisation pour un admin d'organisation), avec leurs nombres de machines et de patients. `[]` pour tout autre rôle. |
| `assignPatientsToGestionnaire` | admin (`requireGestionnaireAdmin`) | Fixe la liste exacte des patients d'**un gestionnaire**, **dans une organisation** (celle de l'appelant ; pour l'admin Anheart, l'organisation principale du gestionnaire) : compare la liste demandée à ses lignes `user_gestionnaires` de cette organisation, insère les liens manquants et supprime ceux qui ne sont plus demandés. Un lien déjà présent n'est pas réécrit (il garde son auteur et sa date), et la même liste envoyée deux fois n'écrit rien. Seules les lignes de ce gestionnaire dans cette organisation sont lues et écrites : ses patients d'une autre organisation ne bougent pas. Seul un patient actif de cette organisation est lié ; tout autre identifiant (compte inconnu, patient d'une autre organisation, compte qui n'est pas un patient) est laissé de côté, avec la même réponse dans les trois cas. Un identifiant répété ne crée qu'un lien. Retourne `{added, removed}` : ce qui a été écrit. Une mutation Convex est une transaction sérialisable : deux appels concurrents sur le même gestionnaire s'appliquent l'un après l'autre, et la liste du dernier s'applique en entier. |
| `getPatientsForGestionnaire`, `getGestionnairesForPatient` | admin / gestionnaire concerné | Lectures. |

### `machines.ts`

| Fonction | Autorisation | Rôle |
|---|---|---|
| `createMachine` | admin Anheart | Crée la machine, statut `offline`, dans l'organisation `organizationId` (par défaut celle de l'admin). Retourne `{machineId, apiKey}` : la clé `anh1.<sélecteur>.<secret>` **n'est visible qu'à ce moment**. |
| `regenerateApiKey` | admin, gestionnaire de la machine | Nouvelle clé ; l'ancienne cesse de fonctionner. |
| `getMachine`, `listMachines` | admin ; gestionnaire (ses machines) | Lecture. `listMachines` renvoie `[]` à un `user`. Option `includeDeleted` pour l'admin. `getMachine` renvoie aussi `softwareVersion`, `contractVersion` et `lastVersionSeenAt`. Les deux renvoient `serverNow`, l'heure du serveur dans la réponse, sur laquelle le site vieillit `lastHeartbeat` ([tableau-de-bord.md §6](tableau-de-bord.md#fraîcheur-recalculée-à-lhorloge)). |
| `updateMachine` | admin, gestionnaire de la machine | Nom, lieu. |
| `deleteMachine` | admin, gestionnaire de la machine | Suppression douce. Refusée s'il y a une séance `active` ou `pending`. |
| `restoreMachine` | admin Anheart | Annule la suppression. |
| `assignMachineToGestionnaires` | admin | Remplace la liste complète des gestionnaires d'**une machine**, pris dans l'organisation de la machine. |
| `setGestionnaireMachines` | admin (`requireGestionnaireAdmin`) | Fixe la liste exacte des machines d'**un gestionnaire**, **dans une organisation** (celle de l'appelant ; pour l'admin Anheart, l'organisation principale du gestionnaire) : compare la liste demandée à ses lignes `machine_gestionnaires` de cette organisation, insère les liens manquants (`isOwner: false`, dans cette organisation) et supprime ceux qui ne sont plus demandés. Seules les lignes de ce gestionnaire dans cette organisation sont lues et écrites : les liens des autres gestionnaires ne bougent pas, ce que le gestionnaire gère dans une autre organisation non plus, et un lien déjà présent n'est pas modifié. Une machine inconnue, ou d'une autre organisation (même réponse), fait refuser l'appel sans rien écrire. Retourne `{added, removed}`. |
| `assignGestionnaireToMachine` / `removeGestionnaireFromMachine` | admin ; gestionnaire de la machine | Lien machine ↔ gestionnaire. Le gestionnaire doit être membre actif de l'organisation de la machine. |
| `getRecentHeartbeats`, `getGestionnairesForMachine`, `getMachinesForGestionnaire` | accès à la machine / admin | Lectures. `getMachinesForGestionnaire` renvoie aussi `serverNow`. |

### `sessions.ts` (lectures seulement)

Aucune fonction de ce module ne crée, ne démarre ni ne termine une séance. Une
séance naît de `training.launchAutoSession` (lancement depuis le site) ou de
`registerLocalSession` (séance démarrée à la machine), et se termine par les
routes d'entraînement.

| Fonction | Autorisation | Rôle |
|---|---|---|
| `getSession` | pratiquant ou accès machine | Détail avec patient et machine. |
| `listSessions` | connecté | admin Anheart : toutes ; admin d'organisation : celles de son organisation ; user : les siennes ; gestionnaire : celles de ses machines. Retourne `kind` et `origin`. La limite (`limit`, 50 par défaut) s'applique **avant** le filtrage par droits, mais **dans l'organisation de l'appelant** : les séances d'une autre organisation ne remplissent jamais sa page. |
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

Aucune page du site n'appelle encore ces fonctions. Elles ne sont **pas
limitées à une organisation** : le registre est celui d'Anheart, commun à tous
les clients. « admin » veut dire ici l'admin Anheart seul ; l'admin d'une
organisation cliente est refusé, en lecture comme en écriture.

| Fonction | Autorisation | Rôle |
|---|---|---|
| `recordRelease` (mutation) | admin Anheart | Enregistre une version publiée dans `software_releases`, ou corrige la ligne d'une version déjà connue (une seule ligne par composant et version). Refuse une version qui n'est pas `<composant>-X.Y.Z`, une version du Pi sans niveau de validation, un niveau sur une version `cloud` ou `web`, une date invalide, des notes de plus de 2000 caractères, et un changement de niveau sans `notes`. Erreurs en `ConvexError(message)`. |
| `listReleases` (query) | admin Anheart | Les versions enregistrées, la plus récente d'abord ; filtre `component` optionnel. |

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

**Contrat** : chaque route (9 des 10, l'exception suit) exige ensuite l'en-tête
`X-Anheart-Contract: <majeure.mineure>`, vérifié **après** la clé et avant
tout traitement, par le même point de passage que l'authentification
(`validateMachineAuth`, `convex/lib/machineHttpAuth.ts`). En-tête absent,
illisible, ou d'une majeure non servie : **426**
`{"error": "contract_unsupported", "message": "…", "supported": ["1"]}`,
et rien n'est lu ni écrit. Une mineure plus récente que celle du serveur
est acceptée. Voir [Versions et compatibilité](#11-versions-et-compatibilité).

**Une seule exception : la demande d'arrêt.**
`GET /api/machine/training/status` répond quelle que soit la version
annoncée, en-tête absent compris ; la clé de la machine reste exigée. C'est
la route qui porte `stopRequested`, et un arrêt a le même sens dans toutes
les versions : le refuser ne serait jamais le côté sûr. Sans cette exception,
une demande d'arrêt du tableau de bord n'atteindrait plus une séance en cours
dès que le serveur ne sert pas la majeure de la console. La route ne fait que
lire, et sa réponse annonce `server_contract_version` : une console d'une
autre majeure n'en retient que ce qui arrête, `stopRequested: true` ou
`active: false`. Les routes exemptées sont
listées dans `contracts/machine-api.json` (`contract_exempt_routes`), et
passent par `authenticateMachineRequest` au lieu de `validateMachineAuth`.

La suppression (`isDeleted`) ou la désactivation explicite
(`authenticationEnabled: false`) produit aussi **401**, sur les 10 routes et
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

**Erreurs** : tout refus des 10 routes machine (les 401 et les 426 compris)
a la forme
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

Côté Pi, toute réponse ≥ 400 devient `Refused` (on ne réessaie pas la même
requête) et son code est journalisé ; une absence de réponse devient
`Unreachable` (on réessaie plus tard).

### Routes utilisées par la console locale (`raspberry-pi/src/cloud_sync.py`)

| Méthode et chemin | Corps / paramètres | Réponse 200 | Cadence côté Pi |
|---|---|---|---|
| `POST /api/machine/heartbeat` | `{live, programsEnabled, activeSessionId?, software_version, contract_version, medical_parameters_version\|null, config_hash\|null}` (`batteryLevel`, `wifiStrength` acceptés, non envoyés) | `{success: true, serverTime}` | toutes les 10 s |
| `POST /api/machine/profiles` | `{storeRev, programsEnabled, profiles: [...]}` | `{count}` | quand la révision du magasin change ; nouvel essai après 15 s |
| `GET /api/machine/training/poll` | - | `{session: null, server_contract_version}` ou `{session: {sessionId, profileId, totalDurationS\|null, subjectId, subjectLabel, subjectHrMax, subjectAge\|null, operatorName}, server_contract_version}` | toutes les 3 s, seulement si `PROGRAMS_ENABLED`, sans séance en cours ni lancement en attente |
| `POST /api/machine/training/start` | `{sessionId, startedAt?, sessionAgeMs?}` | `{success: true}` | une fois, quand le Pi a armé un lancement distant (la console n'envoie pas encore les deux champs facultatifs) |
| `POST /api/machine/training/local` | `{localRef, kind: "auto"\|"manual", startedAt, sessionAgeMs?, operatorName, profileId?, profileName?, zoneLowBpm?, zoneHighBpm?, totalDurationS?, subjectHrMax?, occupancy?, userId?, subjectLabel?}` | `{sessionId}` | une fois par séance démarrée à la machine ; idempotent par `localRef` |
| `GET /api/machine/training/status?sessionId=…` | - | `{status, active, stopRequested, server_contract_version}` ; 404 si inconnue. Répond quel que soit le contrat annoncé. | toutes les 3 s pendant une séance |
| `POST /api/machine/training/telemetry` | `{sessionId, points: [{t, elapsedS, phase, bpm?, motorRpm, outputRpm, setpointMotorRpm, gLoad, safetyAction}]}` (600 points max) | `{stored, duplicates, rejected}` | lots de 300 points max, toutes les 5 s |
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
  `active: false`, le Pi fait un arrêt ordinaire sur la rampe réglée, attribué
  à l'opérateur « tableau de bord », **quelle que soit la version** annoncée
  par la réponse (même majeure, autre majeure, absente ou illisible). Ces deux
  champs ne peuvent provoquer qu'un arrêt ordinaire : les croire est le côté
  sûr. Rien d'autre n'est retenu d'une réponse d'une autre majeure : rien n'y
  peut lancer, reprendre ou réarmer quoi que ce soit.
- **Lancement refusé par le Pi** : le Pi termine la séance `pending` par
  `/training/end` avec `failed: true` et une raison qui commence par
  `refusee par la machine : `. Un lancement ni démarré ni refusé en 60 s est
  terminé avec « la boucle n'a ni demarre ni refuse ».
- **Annulé entre-temps** : si `/training/start` est refusé (séance annulée sur le
  site entre le poll et l'armement), le Pi arrête la séance qu'il vient d'armer.
- **Fin** : `failed` vaut `false` pour `programme_complete` et `operator_stop`,
  `true` pour `emergency_stop`, `safety_verdict`, `tick_exception`, `shutdown`.
- **Un lot peut être envoyé deux fois.** `training/telemetry` et
  `training/events` n'écrivent un point (`(sessionId, t)`) ou un événement
  (`(sessionId, seq)`) qu'une fois. La réponse compte chaque élément du lot
  dans exactement une case : `stored` (écrit par cet appel), `duplicates`
  (déjà là, laissé tel quel, avec la date de sa première réception) ou
  `rejected` (daté hors de la séance, non écrit). Un 200 acquitte donc tout
  le lot : la machine n'a rien à renvoyer, et renvoyer ne coûte rien.
- **Pas de limite « données trop anciennes ».** Aucune route d'entraînement
  ne refuse un point parce qu'il est vieux : une séance d'il y a une semaine
  s'envoie entière. La seule borne est de cohérence, et elle se lit sur
  l'horloge **de la machine** : un point ou un événement est écrit si son `t`
  est entre une minute avant le début que la machine a daté et une minute
  après la fin (`SESSION_WINDOW_MARGIN_MS`). Pas de borne basse tant que la
  machine n'a daté aucun début, pas de borne haute tant que la séance n'est
  pas finie. Hors de ces bornes il est compté `rejected`, sans refuser le
  lot. L'horloge du serveur n'entre pas dans ce calcul : une machine à
  l'heure fausse, ou remise à l'heure en cours de séance, ne perd aucun point.
- **Dates** : `startedAt` (dans `training/local` et `training/start`) est le
  début tel que la machine l'a daté ; `sessionAgeMs`, facultatif, le temps
  écoulé depuis, compté sur son horloge monotone. Une valeur qui n'est pas un
  nombre n'est pas lue. Ce que le serveur en fait : [Deux horloges](#deux-horloges).
- **Événements** : `seq` entier positif ou nul, `t` nombre, `kind` de 64
  caractères au plus (minuscules, chiffres, `_`, une lettre d'abord), `detail`
  de 2000 caractères au plus, `actor` de 1 à 64 caractères parmi lettres,
  chiffres, `_` et `-`. Un événement mal formé refuse tout le lot (400
  `invalid_request`, « Malformed event »), comme un point mal formé.
- **Réseau perdu** : la séance continue sous le seul superviseur local. La
  télémétrie en attente est bornée à 3600 points (1 h), les plus anciens
  sont jetés d'abord ; 20 séances terminées au plus restent dues.

### Route présente mais non appelée par la console locale

| Méthode et chemin | Rôle |
|---|---|
| `GET /api/machine/roster` | `{riders: [{userId, name, hrMax}]}` : patients ayant le droit sur la machine. **Aucun code du Pi ne l'appelle.** |
| `POST /api/machine/training/events` | Corps `{sessionId, events: [{seq, t, kind, detail, actor}]}` (200 événements max, 2000 caractères de texte par événement), réponse `{stored, duplicates, rejected}`. La séance doit appartenir à la machine. **La console ne l'appelle pas encore** : elle le fera en relisant son enregistrement local. |

### Routes retirées

Les cinq routes de l'ancien mode d'enregistrement ECG (interrogation, début,
fin et statut d'une séance d'enregistrement, envoi des lots ECG) n'existent
plus : leurs chemins répondent **404**, même avec une clé valide. Le client qui
les appelait a été retiré du Pi.

---

## 7. Tâche planifiée

`convex/crons.ts` : `check-offline-machines`, **toutes les minutes**. Une machine
`online` ou `in_session` sans heartbeat depuis **90 s** (`LIVE_FRESH_MS`, le
seuil du site) passe `offline`. Le site n'attend pas ce passage pour
l'afficher hors ligne : il le fait à 90 s, sur `lastHeartbeat` et `serverNow`.

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
5. Créer l'organisation par défaut : lancer une fois la migration
   (`npx convex run migrations/multiOrganization:attachExistingRowsToAnheart '{}'`, voir
   [ci-dessous](#activer-le-multi-organisation)).
6. Nommer le premier admin : se connecter une fois sur le site (page d'accueil),
   puis passer `role` à `admin` dans le tableau de bord Convex, sur la ligne
   `memberships` du compte et sur sa ligne `users`. Avec Clerk Organizations
   configuré, cette étape disparaît : le premier admin est le premier
   `org:admin` de l'organisation Anheart dans Clerk.
7. Créer la machine sur le site (admin), copier la clé affichée **une seule
   fois**, et la mettre dans la configuration de la machine
   (`/etc/anheart/anheart.env` sur un Pi installé, `raspberry-pi/.env` sur un
   poste de développement) : `MACHINE_API_KEY=…` et
   `CONVEX_URL=https://<déploiement>.convex.site`.

`npm run dev` lance ensemble Next.js et `convex dev` ; son `predev` exécute
`convex dev --until-success && convex dashboard` (réseau et compte requis).

### Synchronisation par relecture du journal (contrat 1.1)

Ce code ajoute au schéma la table `training_events` (index
`by_session_and_seq` et `by_organization`) et le champ facultatif
`sessions.machineStartedAt`. Rien d'existant ne change de type : un
déploiement qui contient déjà des données l'accepte sans migration. Ce que le
redéploiement doit savoir est dans
[deploiement.md](deploiement.md#36-ce-que-demande-la-synchronisation-par-relecture-du-journal).

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

### Activer le multi-organisation

Ces étapes sont **manuelles** et reviennent au responsable produit : aucune
n'est faite par le code ni par l'intégration continue. Elles n'ont été
exécutées sur aucun déploiement ; la migration n'a tourné qu'en mémoire, dans
les tests (`convex/multiOrganizationMigration.test.ts`).

**A. Sur chaque déploiement existant, juste après avoir déployé ce code**

1. Lancer la migration :
   ```bash
   npx convex run migrations/multiOrganization:attachExistingRowsToAnheart '{}'
   ```
   Elle crée l'organisation « Anheart », y rattache toutes les lignes sans
   organisation des huit tables, et crée pour chaque compte une appartenance
   active avec son rôle actuel. Elle travaille par lots (500 lignes par
   défaut, argument `batchSize`, 1000 au plus) et se replanifie seule jusqu'à
   ce qu'il ne reste rien : la réponse `done: false` veut dire que la suite est
   planifiée. Elle ne touche jamais une ligne qui a déjà une organisation ; la
   relancer ne change rien. C'est une mutation **interne** : le site ne peut
   pas l'appeler.
2. Vérifier dans le tableau de bord Convex : une ligne dans `organizations`,
   une ligne `memberships` par compte, plus aucune machine ni séance sans
   `organizationId`.

Entre le déploiement et la fin de la migration, tout appel du tableau de bord
qui dépend d'une organisation est refusé pour tout le monde (« Your account
does not belong to an organization »), **y compris une demande d'arrêt à
distance** ; la liste des pratiquants servie à la machine est vide ; une séance
démarrée à la console est enregistrée sans son pratiquant, qui n'est pas
rétabli ensuite. **Déployer quand aucune séance n'est en cours et lancer la
migration aussitôt** (détail dans
[deploiement.md](deploiement.md#31-vers-le-développement)). Tant que
`ANHEART_ORG_ID` n'est pas définie, le site fonctionne ensuite comme avant, en
mono-organisation.

**B. Dans Clerk (tableau de bord Clerk, instance du déploiement)**

1. Activer **Organizations**.
2. Créer trois rôles personnalisés, avec exactement ces clés : `org:admin`
   (existe par défaut), `org:gestionnaire`, `org:patient`. Créer les
   permissions `org:sessions:launch`, `org:patients:manage`,
   `org:machines:manage`, `org:programmes:read` et les attribuer aux rôles.
   Convex décide aujourd'hui sur l'organisation et le **rôle** ; il ne lit pas
   encore les permissions. Choisir `org:patient` comme rôle par défaut des
   nouveaux membres : Convex refuse tout autre rôle que ces trois-là, donc le
   rôle `org:member` que Clerk propose par défaut ne donne accès à rien.
3. Créer l'organisation « Anheart » et noter son identifiant (`org_…`).
4. Dans le modèle JWT nommé `convex`, ajouter aux revendications existantes
   (`email`, `email_verified`…) :
   ```json
   {
     "org_id": "{{org.id}}",
     "org_role": "{{org.role}}",
     "org_permissions": "{{org_membership.permissions}}"
   }
   ```
   `org_id` et `org_role` sont obligatoires. Le nom exact du raccourci des
   permissions est à vérifier dans l'éditeur de modèles de Clerk ; s'il
   n'existe pas, omettre la ligne `org_permissions`, que Convex ne lit pas.
   Quand l'utilisateur n'a pas d'organisation active, Clerk met `org_id` à
   `null` : Convex le traite comme « pas d'organisation active ».

**C. Dans Convex (une fois le site capable de choisir une organisation)**

1. Définir la variable d'environnement du déploiement :
   ```bash
   npx convex env set ANHEART_ORG_ID org_…
   ```
   À partir de là, tout jeton sans organisation est refusé, et seuls les
   `org:admin` de cette organisation sont admins Anheart.
2. Relancer la migration (même commande qu'en A) : elle relie l'organisation
   par défaut à cette organisation Clerk (`clerkOrgId`) au lieu d'en créer une
   autre. Sans cela, les jetons de l'organisation Anheart sont refusés (« This
   organization is not known to the server yet »).
3. Inviter le premier admin dans l'organisation Anheart depuis Clerk, avec le
   rôle `org:admin`.

Ne définir `ANHEART_ORG_ID` qu'une fois le sélecteur d'organisation en place
sur le site : sans organisation active dans sa session Clerk, un utilisateur
n'a plus accès à rien.

**Aucune organisation cliente n'est créée dans le miroir par ce lot**, ni par
la migration, ni au premier passage d'un compte. Tant que le webhook Clerk
([ANH-186](https://linear.app/anheart/issue/ANH-186/multi-organisation-lot-2-webhook-clerk-vers-le-miroir-convex-signature)) n'existe pas, un jeton
qui nomme une organisation cliente est refusé (« This organization is not
known to the server yet »). Seule l'appartenance d'un compte à une organisation
**déjà présente dans le miroir** est écrite à son premier passage
(`users.getOrCreateUser`). De même, le retrait d'un membre dans Clerk n'arrive
dans le miroir (`active: false`) qu'avec ce webhook.

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
| `convex/test.setup.ts` | Fabriques partagées : un monde de **trois organisations** (Anheart et son admin ; le centre A avec son admin, deux gestionnaires, trois patients, deux machines ; le centre B avec son admin, un gestionnaire, un patient, une machine), un monde de machines avec de vraies clés pour les routes HTTP, et un monde « d'avant les organisations » pour la migration. Nom à deux points : non déployé. |
| `convex/authorization.matrix.ts` | La **matrice d'autorisation** : la politique de chaque fonction publique, une ligne par rôle, par côté quand l'accès dépend de la propriété, et par organisation. Nom à deux points : non déployé. |
| `convex/authorization.matrix.test.ts` | Parcourt la matrice : un test par cellule. |
| `convex/organizations.test.ts` | D'où viennent l'organisation et le rôle d'un appel : revendications acceptées et refusées, admin Anheart, premier passage d'un compte, et la transition avant et après `ANHEART_ORG_ID`. |
| `convex/organizationIsolation.test.ts` | Ce que la matrice n'exprime pas : l'autre sens (le centre A sur le centre B), la même réponse pour un identifiant d'une autre organisation et pour un identifiant inconnu, les appels qui mêlent deux organisations dans leurs arguments, un compte membre de deux organisations (liens patient et machine fixés dans une seule), ce qu'une machine écrit, les lignes sans organisation. |
| `convex/multiOrganizationMigration.test.ts` | La migration : rattachement, appartenances, idempotence, lots, liaison avec `ANHEART_ORG_ID`. |
| `convex/httpRoutes.test.ts` | Les 10 routes machine de `http.ts` : corps mal formés, idempotence, liaison ressource-machine, filtrage des séances. |
| `convex/journalSync.test.ts` | Ce que Convex fait d'une séance que la machine relit dans son enregistrement et envoie, tard ou deux fois : une ligne par point et par événement quel que soit le nombre d'envois, la route des événements et ses tailles, la borne de cohérence sur l'horloge de la machine, les dates du serveur pour une machine à l'heure fausse (1970, 10 min d'avance), et les deux dates d'un envoi tardif (reçu maintenant, mesuré avant). |
| `convex/legacyRecordingRetired.test.ts` | Retrait de l'ancien mode ECG : routes disparues (404), modules réduits à leurs lectures, aucune écriture dans `ecg_data` ni `session_summaries`, rien de planifié en fin de séance, et les deux mutations de migration. |
| `convex/contract.test.ts`, `convex/contractSource.test.ts` | Le contrat versionné (ANH-133) : 426 sur chaque route sans majeure servie sauf celle de la demande d'arrêt (qui répond avec la seule clé, sans rien écrire), clé vérifiée avant le contrat, rien d'écrit pour une requête refusée, `server_contract_version` dans le poll, un code stable pour chaque refus, versions stockées à chaque heartbeat. Le second fichier remplace `contracts/machine-api.json` par un autre contrat et vérifie que le code le suit : la valeur est bien lue dans ce fichier. |
| `convex/crons.test.ts` | Le cron `check-offline-machines`. |
| `convex/machineEdit.test.ts` | Ce que fait le formulaire de machine pour un gestionnaire : `machines.updateMachine` enregistre le nom et le lieu sans toucher aux liens, et `machines.assignMachineToGestionnaires` reste réservé à l'admin (ANH-155). |
| `convex/completeness.test.ts` | Échoue si une fonction publique ou une route n'a pas de cellule de matrice, si une fonction n'a aucune cellule appelée depuis une autre organisation, ou si une fonction qui prend un identifiant n'a pas de cellule `foreign`. |
| `convex/machineAuth.test.ts`, `convex/machineCredential.test.ts` | Authentification et clés machine (ANH-121, complétés par ANH-132). Leur monde est un déploiement mono-organisation après migration, sans revendication d'organisation. |
| `convex/trainingPrivacy.test.ts`, `convex/sessions.test.ts` | Confidentialité des mesures live et des séances (ANH-71), sur le même monde mono-organisation migré. |
| `convex/gestionnaireMachines.test.ts` | `machines.setGestionnaireMachines` : deux gestionnaires sur une machine (retirer l'un ne touche pas l'autre), ajout, liens existants conservés, refus sans écriture (ANH-154). |
| `convex/gestionnairePatients.test.ts` | `users.assignPatientsToGestionnaire` : ajout seul, retrait seul, les deux, liste inchangée (aucune ligne réécrite), identifiant répété, patient d'une autre organisation traité comme un compte inconnu, admin d'une autre organisation refusé comme pour un gestionnaire inconnu, deux enregistrements successifs (ANH-208). |
| `convex/softwareReleases.test.ts` | Le registre des versions : ce que `recordRelease` accepte et refuse, une ligne par version, et chaque case de la règle « quelle machine reçoit quelle version » (ANH-134). |
| `convex/cloudVersion.test.ts` | La constante `CLOUD_VERSION` est celle de `convex/VERSION` et celle que répond le code déployé (ANH-134). |

### Lire la matrice

Chaque entrée de `authorization.matrix.ts` décrit une fonction publique :
son identifiant (`module.fonction`), son type, une fonction `build` qui prépare
les arguments, et une liste de `cases`. Une cellule est un acteur nommé avec
une portée et un résultat attendu.

Les acteurs : `anonymous` ; `admin` (admin de l'organisation Anheart) ; au
centre A, `orgAdmin`, `manager`, `otherManager`, `patient`, `otherPatient`,
`stranger` ; au centre B, `orgBAdmin`, `orgBManager`, `orgBPatient`. Chacun
porte les revendications `org_id` et `org_role` qu'un jeton Clerk porterait
pour lui : la matrice tourne comme un déploiement configuré.

Les portées : `self` (soi-même), `own` (une ressource à laquelle l'acteur est
lié, dans son organisation), `other` (une ressource de la **même** organisation
à laquelle il n'est pas lié), `foreign` (une ressource d'une **autre**
organisation, désignée par son identifiant).

Les résultats :

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
jamais remplacer ni son sujet ni son organisation, et l'acteur `anonymous`
n'en porte aucune : `as` refuse ces cas.

Chaque fonction publique a au moins une cellule jouée par un membre du centre
B, et chaque fonction qui prend l'identifiant d'une ressource a une cellule
`foreign` : appelée du centre B avec un identifiant du centre A, elle refuse,
ne renvoie rien, ou ne renvoie que le centre B. Aucune cellule `foreign` n'a le
résultat `success`. `completeness.test.ts` vérifie ces trois points.

### Ajouter une fonction ou une route

- Nouvelle fonction publique (`query`/`mutation`/`action`) : ajouter une entrée
  dans `authorization.matrix.ts` (identifiant, `ref`, `build`, `cases`, et un
  `onSuccess`/`onFiltered` qui vérifie le comportement), avec une cellule
  `foreign` si elle prend un identifiant, sinon `completeness.test.ts` échoue.
- Nouvelle table : lui donner `organizationId` et l'index `by_organization`,
  écrire l'organisation sur le serveur (jamais depuis un argument), et
  l'ajouter à la migration si des lignes existent déjà.
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
| Majeure de la machine absente, illisible ou non servie | Convex répond **426** `contract_unsupported` à 9 des 10 routes machine (toutes **sauf** `GET /api/machine/training/status`), sans rien lire ni écrire d'autre. |
| Arrêt venu du tableau de bord | Il traverse toutes les versions : la route `training/status` répond avec la seule clé, et le Pi arrête sa séance sur `stopRequested: true` ou `active: false` d'une réponse de n'importe quelle majeure, version absente comprise. C'est tout ce qu'il retient d'une réponse d'une autre majeure. |
| Majeure du serveur différente de celle de la machine, ou non annoncée | Le Pi **n'arme aucun lancement distant** venu de cette réponse, l'affiche sur la console et renvoie le lancement comme séance échouée (voir [raspberry-pi.md](raspberry-pi.md#14-versions-et-compatibilité)). |

Une évolution compatible (un champ optionnel de plus, un code d'erreur de plus)
monte la **mineure**. Tout ce qui change le sens d'un champ existant, en retire
un, ou rend obligatoire ce qui ne l'était pas, monte la **majeure** : les deux
côtés doivent alors être livrés ensemble, et une machine restée sur l'ancienne
majeure est refusée au lieu d'être comprise de travers.

### Ce que chaque mineure ajoute

| Mineure | Ajouts, tous compatibles |
|---|---|
| `1.0` | Le contrat de départ. |
| `1.1` | La route `POST /api/machine/training/events`. La réponse `{stored, duplicates, rejected}` de `training/telemetry` (au lieu de `{stored}`), qui n'écrit plus deux fois le même point. Les champs facultatifs `startedAt` et `sessionAgeMs` de `training/start`, et `sessionAgeMs` de `training/local`. |

Une console restée en `1.0` fonctionne sans changement avec un Convex `1.1` :
elle n'envoie aucun des champs ajoutés, ne lit pas ceux de la réponse, et
n'appelle pas la route des événements.

### Matrice de compatibilité

| Version du Pi (`raspberry-pi/VERSION`) | Majeure de contrat | Version Convex minimale |
|---|---|---|
| `pi-0.0.0-dev` (développement, avant la première release) | 1 (contrat `1.1`) | `cloud-0.0.0-dev` (développement) : le code de la branche `develop` qui sert la majeure 1 |

Cette matrice est tenue à jour à chaque release (check-list de
[release.md](release.md)) : une ligne par version publiée du Pi. Aucune
version n'a encore été publiée : elle ne contient que la ligne de
développement.

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

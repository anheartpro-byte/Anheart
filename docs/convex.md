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
- Seul le Pi termine une séance qu'il a démarrée. Une séance dont Convex n'a
  plus de nouvelles depuis 10 min est marquée `interrupted` (« interrompue,
  fin inconnue »), jamais `completed` ni `failed` : le Pi n'en est pas averti,
  et elle redevient `active` ou se termine dès qu'il se manifeste
  ([Tâche planifiée](#7-tâche-planifiée)).
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

Index : `by_user`, `by_organization`, `by_user_and_organization`,
`by_active_and_organization` (les membres actifs d'une organisation, ou de
toutes : ce que compte `dashboard.counters`).

### `organizationId` : à quelle organisation appartient une ligne

Dix tables portent un champ `organizationId` :
`users` (organisation principale du compte), `machines`, `sessions`,
`training_telemetry`, `machine_profiles`, `machine_user_permissions`,
`machine_gestionnaires`, `user_gestionnaires`, `fitness_assessments` et
`consents`. Les huit premières tables ont un index `by_organization` ; les
deux dernières sont indexées par passager et organisation, puis par date, pour
lire son historique sans mélanger les centres. Toute table créée ensuite le
porte aussi, sauf une table **commune à tous les clients** : `training_events`
et `session_summaries_v2` (ANH-89) le portent, copié de leur séance.

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

### `audit_log` : journal d'audit (ANH-167)

Une ligne attribue une action sensible à son acteur, à sa cible et à sa
requête. Les valeurs fermées sont définies dans
[`convex/lib/audit.ts`](../convex/lib/audit.ts) et reprises par le schéma.

| Champ | Type et sens |
|---|---|
| `at` | Nombre, date serveur à l'insertion, en ms Unix. |
| `organizationId` | Identifiant d'organisation, facultatif pour une action globale. Une ligne sans organisation n'est lisible que par l'admin Anheart. |
| `actorId` | Identifiant de `users` ou de `machines`, absent pour le système. |
| `actorRole` | `admin`, `org_admin`, `gestionnaire`, `user`, `machine` ou `system`. Le rôle de l'acteur au moment de l'effet. |
| `action` | Une des 43 valeurs de `AUDIT_ACTIONS`, énumération fermée ; catégories décrites au [§5](#convexlibauditts-anh-167). |
| `targetType` | `user`, `machine`, `machine_key`, `session`, `fitness_assessment`, `consent`, `organization`, `software_release`, `medical_parameters_version` ou `patient_invitation`. Déduit de l'action par `AUDIT_ACTION_TARGET`. `user.invited` cible une `patient_invitation`, y compris quand aucun profil unique ne peut être lié à l'invitation. |
| `targetId` | Chaîne, identifiant de la cible ; il reste dans le journal après la suppression de celle-ci. |
| `details` | Objet facultatif, dont seules les clés ci-dessous sont admises. |
| `requestId` | Identifiant de requête Convex lu par `ctx.meta.getRequestMetadata()`, partagé par la route ou l'action d'origine et la mutation qu'elle appelle, ainsi que par les lignes d'une même requête. |
| `source` | `site`, `machine`, `cron`, `webhook` ou `operator`. |

La source **`operator` est une extension d'EX-1 décidée pendant le
développement faute de valeur adaptée, à relire par le chef de projet**.
Elle désigne les fonctions lancées en ligne de commande avec la clé de
déploiement : `machineKeys.createKeyByOperator`,
`machineKeys.revokeKeyByOperator` et `medicalParameters.registerVersion`
(écriture par `medicalParametersStore.insertVersion`). Ces fonctions sont
internes ; l'acteur est `system`, sans `actorId`. Le site et les routes
machine ne peuvent pas emprunter ce chemin.
La source est attribuée par le chemin de code, pas constatée à l'exécution :
une fonction interne lancée avec la clé de déploiement garde la source de
son chemin, par exemple `machine` pour `training.markTrainingStarted`.

Les clés autorisées de `details` sont `fields`, `role`, `previousRole`,
`level`, `previousLevel`, `version`, `softwareVersion`,
`medicalParametersVersion`, `machineId`, `userId`, `gestionnaireId`, `added`,
`removed`, `count`, `outcome` et `expiresAt`. Toutes sont facultatives.
Les chaînes contiennent des noms de champs, identifiants ou versions,
jamais le contenu libre saisi par un utilisateur. `role` et `previousRole`
reprennent les quatre rôles humains ci-dessus ; `level` et `previousLevel`
reprennent les niveaux fermés de `convex/lib/releaseValidation.ts` ;
`outcome` est seulement `completed` ou `failed`, résultat technique d'une
séance, jamais un verdict d'aptitude.
`fields` ne contient que les noms des champs réellement modifiés. Les listes
`added` et `removed` sont limitées chacune aux 1 000 premiers identifiants ;
si l'une dépasse ce plafond, `count` donne le total des deux listes avant
troncature. Une invitation conserve ses gestionnaires dans `added` et
chaque lien créé à son acceptation a sa propre ligne. Un changement de
propriétaire machine donne le nouveau propriétaire dans `gestionnaireId` ;
une correction du niveau d'une release conserve `previousLevel`.

Les versions de fiche variateur et la version fournie directement à
`medicalParametersStore.insertVersion` ne sont pas copiées dans les détails :
ces mutations ne leur imposent pas de format strict. Les versions de release
restantes passent `isReleaseVersion` ; `softwareVersion` d'une revue machine
passe aussi ce format et doit exister dans le registre des releases Pi.
`medicalParametersVersion` d'une revue n'est pas copiée non plus : son
existence en base ne garantit pas un format, puisque l'insertion interne
accepte un texte libre. Cette vérification métier reste inchangée. La machine
de la fiche et l'identifiant de la version créée restent les cibles de leurs lignes.
La clé `medicalParametersVersion` reste admise par le schéma, mais aucun
appel courant ne la remplit.

Conformément au principe 6 du processus de développement, aucune donnée de
santé ni texte libre ne doit y entrer : ni FC max, année de naissance,
réponses ou verdict d'aptitude, notes, code ou motif de fin de séance,
mesures ECG ou BPM, noms, motifs ou adresses. Les validateurs ferment les
clés ; les appels et leurs tests vérifient les valeurs réellement écrites.
Le classement des données de santé reste à décider dans ANH-172 ; tout ajout
de clé exige une relecture.

Les douze index ordonnent les lectures par `at` : `by_at`,
`by_organization_and_at`, `by_actor_and_at`, `by_action_and_at`,
`by_actor_role_and_at` (`actorRole`, `at`),
`by_organization_and_actor_role_and_at` (`organizationId`, `actorRole`, `at`),
`by_target_and_at` (`targetType`, `targetId`, `at`),
`by_target_type_and_at`, `by_organization_and_target_type_and_at`,
`by_organization_and_actor_and_at`, `by_organization_and_action_and_at` et
`by_organization_and_target_and_at`. Les variantes d'organisation ajoutent
`organizationId` en tête des champs.

La table est **append-only** : seule `audit()` y insère une ligne, dans la
même transaction que l'effet ; aucune fonction ne la modifie ni ne la
supprime. [`convex/auditLog.test.ts`](../convex/auditLog.test.ts) énumère les
fonctions enregistrées dans tout `convex/**/*.ts`, hors fichiers générés,
tests, fixtures, setup et matrices. Le test cherche le littéral `audit_log`
et interdit toute assertion de type (`as ...` ou `<Type>...`, y compris
`GenericId<...>`) dans l'identifiant passé à `ctx.db.delete`, `patch` ou
`replace`. Il refuse aussi ces écritures sur un alias déclaré directement
par `const db = ctx.db`, quel que soit son nom. Cette analyse de source
ne constitue pas une preuve générale : elle ne suit pas le flux des valeurs.
Un identifiant non typé, une assertion faite avant l'appel, un alias obtenu
par affectation, déstructuration ou alias successifs, une méthode extraite,
un accès calculé (`db[method]`) ou un nom de table construit peut lui échapper.
**Aucune purge n'existe.** La rétention envisagée est de **5 ans, à
confirmer par ANH-87a** ; rien n'est supprimé, ni avant ni après cinq ans,
tant que cette durée et sa mise en œuvre ne sont pas décidées. La portée de
l'immutabilité est précisée dans
[securite.md §14](securite.md).

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

### `fitness_assessments` : aptitude du passager (ANH-143)

Chaque soumission ajoute une ligne ; elle ne remplace pas l'historique. L'index
`by_user_and_organization` porte aussi `submittedAt` : la saisie la plus
récente est la seule qui décide de l'aptitude au lancement. Une nouvelle
saisie repasse donc le passager à `rider_not_fit` jusqu'à sa validation.

| Champ | Sens |
|---|---|
| `userId`, `organizationId` | Passager et organisation de la saisie. |
| `questionnaireVersion`, `answers` | Version du questionnaire et réponses booléennes indexées par les identifiants de questions. |
| `outcome` | Résultat calculé : `fit`, `unfit` ou `needs_medical_opinion`. La validation ne le modifie jamais. |
| `submittedBy`, `submittedAt` | Compte qui a envoyé la saisie, et date serveur en ms Unix. |
| `validatedBy`, `validatedAt` | Gestionnaire ou administrateur qui a relu la saisie, et date serveur ; absents avant la validation. |
| `validUntil` | Fin de validité en ms Unix ; requise pour valider un résultat `fit`, absente pour les autres résultats. |
| `notes` | Note facultative de validation, limitée à 1000 caractères après suppression des espaces de bord. |

Les réponses et les notes sont des données de santé. `fitness.ts` ne les
renvoie qu'aux gestionnaires du passager et aux administrateurs, jamais au
passager lui-même, à un tiers ou à un autre centre. Elles ne sont écrites dans
aucun journal, événement ou enregistrement de séance.

### `consents` : consentement du passager (ANH-143)

Chaque ligne garde une signature et son retrait éventuel. L'index
`by_user_and_organization` inclut `signedAt` pour lire les consentements d'un
passager dans un centre. Le lancement n'accepte que le consentement non retiré
dont `textVersion` est la version courante.

| Champ | Sens |
|---|---|
| `userId`, `organizationId` | Passager et organisation de la signature. |
| `textVersion` | Version du texte accepté. |
| `signedAt`, `signedVia` | Date serveur en ms Unix et origine (`site` ou `console`). |
| `withdrawnAt`, `withdrawnBy` | Date du retrait et compte qui l'a effectué ; absents tant que le consentement reste actif. |

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
| `recordedBy`, `updatedAt` | l'admin qui a écrit la ligne en dernier, et quand |

Index `by_component_and_version`.

La ligne dit le niveau **en vigueur**. Ce qu'un changement de niveau remplace
n'est pas perdu : la table `software_release_levels` garde chaque niveau qu'une
version du Pi a porté, une ligne par décision, du plus ancien au plus récent.
Elle ne fait que s'allonger, et n'a pas d'`organizationId` non plus.

| Champ | Sens |
|---|---|
| `releaseId` | la ligne de `software_releases` |
| `validationLevel` | le niveau décidé |
| `reason` | les `notes` données avec la décision (le motif ; facultatif pour le premier niveau) |
| `decidedBy`, `decidedAt` | l'admin qui l'a décidé, et quand |

Index `by_release`. Une ligne de `software_releases` écrite avant cette table
n'y a aucune décision : elle est lue comme sa propre première décision (son
niveau, ses `notes`, son `recordedBy`, son `updatedAt`), et cette première
décision est écrite dans la table à la première écriture qui suit, changement
de niveau ou simple correction, avant que la ligne soit réécrite. Les deux
tables sont un ajout au schéma : aucune donnée existante n'est à migrer.

**Règle portée par `validationLevel`.** Une machine validée pour les séances
programmées (M5) ne reçoit qu'une version `auto_validated` ou
`occupied_validated` ; une machine validée pour une personne à bord (M6) ne
reçoit qu'une version `occupied_validated`. Un état de machine que la règle ne
connaît pas ne reçoit aucune version. La règle est codée dans
`convex/lib/releaseValidation.ts` (`releaseAllowedOnMachine`) : le registre
machine (ANH-147) l'appelle pour comparer les versions annoncée et attendue
au niveau de la machine. Le refus d'installation par la mise à jour à
distance reste bloqué par ANH-116. Détail dans
[release.md](release.md#2-le-niveau-de-validation-dune-version-du-pi).

Ces deux tables ne sont déployées sur aucun déploiement.

### `medical_parameters_versions` : registre global des paramètres médicaux

Une ligne par version signée du fichier médical. Ce registre est commun à tous
les clients : il n'a pas d'`organizationId`.

| Champ | Sens |
|---|---|
| `version` | Version `MAJEUR.MINEUR.CORRECTIF`. |
| `issuedAt`, `signedBy` | Date de décision et identifiant du signataire repris du fichier. |
| `storageId` | Référence au fichier JSON exact dans le stockage Convex. |
| `signature` | Signature Ed25519 détachée, encodée en base64. |
| `zoneCeilingFraction`, `minRiderAge`, `hrMaxFormula` | Valeurs validées que Convex utilise pour la fraction de zone, l'âge minimum et l'estimation Tanaka. |
| `registeredAt` | Date d'inscription, en ms Unix. |

Index : `by_version`. Les versions sont ajoutées, jamais remplacées ;
l'inscription refuse un doublon et toute version qui n'est pas supérieure à la
plus grande déjà inscrite. La version courante est la plus grande dans l'ordre
sémantique des trois nombres.

`medicalParameters.registerVersion` vérifie la signature sur les octets UTF-8
exacts du fichier, puis son format et ses bornes. La clé publique utilisée est
la copie de `raspberry-pi/config/medical.pub` dans
`convex/lib/medicalParameters.ts`. L'action stocke le fichier puis appelle
`medicalParametersStore.insertVersion` ; si cette insertion échoue, elle
supprime le fichier du stockage.

`insertVersion` échoue dans la même transaction les lancements auto `pending`
déjà expirés. Un lancement auto `pending` non expiré bloque l'inscription. Le
contrôle et l'insertion sont dans la même transaction : le message donne leur
nombre et invite à réessayer après leur armement, leur échec ou leur expiration ;
le délai effectif est celui de `PENDING_SESSION_TTL_S` (300 s par défaut,
jusqu'à 600 s si configuré). Cette règle empêche la fraction de zone courante
de changer entre la création et l'armement d'un lancement. La zone courante
reste appliquée aux nouveaux lancements, et n'est donc pas recalculée à
l'armement ; le pratiquant, son âge minimum et sa FC max restent revérifiés.

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

### `machine_registry` : identité, mesures et validation (ANH-147)

Une ligne liée à une seule machine par `machineId`, sans modifier les données
envoyées par le Pi. Les index sont `by_machine` et `by_organization`.
`organizationId` est recopié depuis la machine à chaque écriture, comme pour
`machine_profiles`. Une machine sans ligne a un registre vide et le niveau
`none` ; aucune mesure ni pièce jointe n'est inventée.

| Champs | Contenu |
|---|---|
| `serialPi`, `serialDrive`, `serialMotor`, `serialGearbox`, `bitalinoAddress` | Identité matérielle optionnelle ; adresse BITalino au format MAC, normalisée en majuscules. |
| `armRadiusM`, `legTipRadiusM`, `radiiMeasuredAt`, `radiiMeasuredBy` | Rayons mesurés en mètres, date Unix en ms et nom du mesureur. Valeurs finies strictement positives et au plus 10 m : borne de plausibilité contre une erreur d'unité, pas une limite de sûreté. |
| `commissionedAt`, `site`, `contacts` | Date de mise en service, adresse texte, au plus 20 identifiants de membres actifs de l'organisation, sans doublon. Aucun e-mail dans la lecture du registre. |
| `expectedSoftwareVersion` | Tag `pi-X.Y.Z` présent dans `software_releases`. Une version insuffisamment validée est signalée et reste sélectionnable. |
| `driveParameterSheet` | Fiche variateur avec `storageId`, `contentType` (PDF ou JSON), `version`, `signedBy`, `at`, `attachedBy`, `attachedAt`. |
| `driveParameterSheetHistory` | Toutes les fiches remplacées, conservées avec leur fichier. |
| `validation` | `level` (`none`, `bench`, `auto`, `occupied`), `reviews` et `downgrades`. |
| `updatedAt`, `updatedBy` | Date et administrateur de la dernière écriture. |

Une revue contient `level`, `by`, `at`, `reportStorageId`, `softwareVersion`
et `medicalParametersVersion` (version enregistrée ou `null`). Une descente
contient `from`, `to`, `by`, `at`, `reason`. Ces historiques ne sont jamais
effacés par les mutations. Le niveau monte uniquement par une revue jointe,
exactement au niveau suivant. `occupied` exige une version médicale et reste
refusé tant que `MEDICAL_PUBLIC_KEY_PEM` est la clé publique de test ;
`cle-de-test` désigne le signataire du fichier de test, pas le nom de la clé.
ANH-98 doit remplacer cette clé. Seul l'admin Anheart descend le niveau, avec un
motif de 3 à 500 caractères après suppression des espaces extérieurs.

Les chaînes sont nettoyées et limitées à 200 caractères (adresse : 500,
version de fiche : 50). Les dates saisies sont des entiers Unix en ms entre
le 1er janvier 2024 UTC et maintenant plus 24 h. Une pièce jointe doit
exister dans Convex Storage, être un PDF ou JSON de 10 Mio au plus, avoir
été téléversée depuis moins d'une heure et ne figurer dans aucun registre,
ni comme fiche courante, ni dans un historique, ni comme compte rendu.
Aucune donnée de santé ne doit être déposée dans le registre ou la fiche.
Résiduel Storage : un fichier téléversé puis refusé au rattachement reste
dans le Storage sans référence de registre et sans URL exposée. Point de
suivi : prévoir le nettoyage des dépôts non rattachés ; aucun mécanisme de
purge supplémentaire n'est livré ici.

### `machines`

| Champ | Rôle |
|---|---|
| `name`, `location` | Nom, lieu. |
| `apiKey`, `apiKeySelector` | **Hérités, optionnels (ANH-165).** La clé unique d'une machine écrite avant ANH-165 : vérificateur salé versionné `hmac-sha256:1:<sel hex>:<digest hex>` et sélecteur public de 128 bits. Les clés vivent désormais dans [`machine_api_keys`](#machine_api_keys--les-clés-des-machines-anh-165) ; aucune fonction n'écrit plus ces deux champs. La migration `moveMachineKeysToTable` ([§8](#migration-des-clés-machine-anh-165)) déplace la clé dans la table et vide les deux champs ; tant qu'elle n'a pas tourné, une clé encore ici ouvre les routes machine comme avant (§6). Une valeur réversible d'avant ANH-121 n'ouvre rien ; la migration l'efface sans la déplacer. |
| `authenticationEnabled` | `false` refuse l'authentification ; absent ou `true` l'autorise sous réserve d'une clé valide et d'une machine non supprimée. Distinct de `status` et de `programsEnabled`. Aucun nouveau contrôle public/UI de ce champ. |
| `status` | `"online"` \| `"offline"` \| `"in_session"`. |
| `lastHeartbeat` | ms Unix du dernier heartbeat. |
| `config` | **Obsolète.** `{ sampleRate, channels, batchInterval }`, réglages de l'ancien enregistreur ECG. Facultatif ; aucune fonction ne le lit ni ne l'écrit. Il reste déclaré le temps que la migration `removeMachineConfig` le retire des documents ([§8](#8-déployer)), puis il quittera le schéma. |
| `isDeleted`, `deletedAt`, `deletedBy` | Suppression douce. |
| `programsEnabled` | Rapporté par le Pi : accepte-t-il les séances auto ? |
| `live` | Dernier état rapporté par le Pi (voir ci-dessous). |
| `softwareVersion` | Version logicielle annoncée par le Pi dans son heartbeat (tag git, par exemple `pi-0.4.2`). Absente tant qu'aucun heartbeat ne l'a donnée, et effacée si le dernier heartbeat n'en portait pas de bien formée. |
| `contractVersion` | Version du contrat machine annoncée par le Pi : l'en-tête `X-Anheart-Contract` du dernier heartbeat accepté. |
| `lastVersionSeenAt` | ms Unix du dernier heartbeat qui a actualisé les versions rapportées. |
| `medicalParametersVersion` | Version médicale annoncée par `medical_parameters_version` dans le heartbeat. Une chaîne est la version que le Pi annonce après sa vérification locale du fichier signé ; `null` est classé non signé ; l'absence signifie qu'aucune version bien formée n'a été retenue. |

Index : `by_api_key` (historique, inutilisé pour authentifier), `by_apiKeySelector` (lu seulement pour une clé pas encore déplacée dans `machine_api_keys`), `by_status`, `by_is_deleted`.

Les réponses de `machines.getMachine`, `training.listLaunchableMachines` et
`training.getMachineLive` calculent aussi `medicalParametersStatus` et
`medicalParametersReferenceVersion` à la lecture ; ces deux valeurs ne sont
pas stockées dans `machines`. Les statuts sont `unknown` (champ absent),
`unsigned` (champ `null`), `no_reference` (version de la machine mais aucun
registre courant), `current` (versions égales), `obsolete` (version de la
machine plus ancienne) et `ahead` (version de la machine plus récente). La
référence est la version courante du registre, ou `null`.

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

### `machine_api_keys` : les clés des machines (ANH-165)

Une machine peut avoir **plusieurs clés valides à la fois** : c'est le
chevauchement d'une rotation, pendant lequel l'ancienne clé et la nouvelle
ouvrent toutes deux les routes machine. Chaque clé se révoque seule et peut
expirer. La clé en clair n'est jamais gardée : seulement son sélecteur public,
un vérificateur salé et un préfixe à afficher. Elle n'est montrée qu'une fois,
dans la réponse de l'appel qui l'a créée.

| Champ | Rôle |
|---|---|
| `machineId` | La machine. La ligne ne porte pas d'`organizationId` : l'organisation d'une clé est celle de sa machine, lue à chaque appel, comme pour `machine_heartbeats`. |
| `selector` | Partie publique de la clé `anh1.<sélecteur>.<secret>` (128 bits aléatoires) : ce par quoi une requête cherche sa clé. Seule, elle n'ouvre rien. |
| `keyHash` | `hmac-sha256:1:<sel>:<digest>` de la clé entière, vérifié par `crypto.subtle.verify` (§6). |
| `prefix` | Ce que la fiche de la machine montre : `anh1.` suivi des 8 premiers caractères du sélecteur (par exemple `anh1.3f9c01ab`). |
| `createdAt`, `createdBy` | Création et son auteur. Pas d'auteur pour une clé créée en ligne de commande (`createKeyByOperator`) ou déplacée par la migration ; pour cette dernière, `createdAt` est la date du déplacement, celle de l'émission n'étant pas connue. |
| `expiresAt` | Optionnel. À cet instant ou peu après (§6), la clé n'ouvre plus rien, que la tâche l'ait révoquée ou non. Posé à la création (d'une heure à dix ans plus tard, compté sur l'horloge du serveur), ou par la rotation qui remplace la clé (fin du chevauchement). |
| `revokedAt`, `revokedBy` | La clé n'ouvre plus rien : révoquée par quelqu'un (`revokedBy`), par une rotation sans chevauchement (`revokedBy` est alors l'auteur de la rotation), ou par la tâche `revoke-expired-machine-keys` une fois son expiration passée (sans `revokedBy`). |
| `replacedBy` | La clé créée par la rotation qui a retiré celle-ci. |
| `lastUsedAt` | Dernière requête que la clé a ouverte, écrite **au plus une fois par minute** (§6). |

Index : `by_selector` (l'authentification), `by_machine` (la liste de la
fiche), `by_machine_and_revoked` (les clés qu'une rotation retire),
`by_revoked_and_expiry` (la tâche). Le ticket demandait un index sur
l'empreinte (`by_key_hash`) : l'empreinte est salée (ANH-121), elle change à
chaque calcul et ne peut pas servir à chercher une clé. La recherche se fait
par le sélecteur, puis l'empreinte est vérifiée.

### `sessions`

| Champ | Rôle |
|---|---|
| `machineId` | Machine. |
| `userId` | Pratiquant. Toujours présent pour un lancement distant. Pour une séance démarrée à la machine, le passager que la console a pris dans le roster (ANH-84) ; **absent** pour une séance de banc, qui ne nomme personne, et quand le passager nommé n'a plus le droit sur la machine à la déclaration de la séance (voir plus bas). |
| `startedById` | Qui a lancé (site). |
| `status` | `pending` → `active` → `completed` ou `failed`. Une séance `active` dont Convex n'a plus de nouvelles depuis 10 min passe `interrupted` (fin inconnue) ; elle redevient `active` au premier heartbeat de sa machine qui la nomme, et passe `completed` ou `failed` quand la machine envoie sa fin ([Tâche planifiée](#7-tâche-planifiée)). Un lancement `pending` que Convex refuse avant que la machine l'arme (attente de plus de 5 min, droit retiré...) passe directement `failed`, avec `endCode` ([Revérification à l'armement](#revérification-à-larmement-anh-144)). |
| `startedAt`, `endedAt` | ms Unix. Le site les lit comme des dates du serveur. Pour une séance dont la machine dit l'âge, ce que fait la console de ce dépôt pour toute séance en cours, les deux sont placées sur l'horloge du serveur ([Deux horloges](#deux-horloges)). Avec une console antérieure, qui ne le dit pas : `startedAt` en est une pour une séance lancée du site (la réception de la confirmation de la machine), et c'est la date écrite par la machine pour une séance démarrée à la machine ; `endedAt` est la date écrite par la machine quand elle en envoie une, sinon la réception. |
| `machineStartedAt` | Le début tel que **la machine** l'a daté, sur son horloge (ms Unix), pour une séance dont la machine a dit l'âge. Présent seulement dans ce cas : c'est ce champ qui dit qu'une séance a un axe machine ([Deux horloges](#deux-horloges)). |
| `channels`, `sampleRate`, `notes` | `channels` vaut `["ECG"]` pour une séance d'entraînement. `sampleRate` n'est plus écrit : il ne reste que sur les séances de l'ancien mode. `notes` contient `Occupancy: bench/occupied` pour une séance locale qui déclare l'occupation ; une attribution indisponible à l'enregistrement est également tracée. |
| `kind` | `auto` \| `manual`. **Absent** = séance de l'ancien mode d'enregistrement ECG (historique) : `getSession`, `listSessions`, `listSessionsPaginated` et `getTrainingSession` la rapportent `recording`. Le schéma refuse d'écrire cette valeur. |
| `origin` | `remote` (site) \| `local` (machine). |
| `profileId`, `profileName`, `zoneLowBpm`, `zoneHighBpm`, `totalDurationS` | Programme lancé. |
| `subjectHrMax`, `subjectAge`, `subjectLabel` | FC max retenue, âge, nom du pratiquant. |
| `operatorName` | Qui a lancé. |
| `localRef` | Clé d'idempotence côté Pi (séances locales). |
| `stopRequestedAt` | Posé par `requestStop` : le Pi le lit et arrête. |
| `endReason` | Raison de fin donnée par le Pi (ou « Cancelled before start by … », ou la phrase d'un refus à l'armement, par exemple « expired before the machine armed it »). |
| `endCode` | Code stable d'un lancement refusé avant que sa machine l'arme (ANH-144) : `expired_before_start`, `right_revoked_before_start`... ([Revérification à l'armement](#revérification-à-larmement-anh-144)). Le site le traduit. Absent sur toute autre séance. |
| `interruptedAt` | Quand la tâche planifiée a marqué la séance `interrupted` (horloge du serveur). Présent exactement tant que la séance l'est. |

Index : `by_user`, `by_user_and_status` (les séances ouvertes d'un
pratiquant : une à la fois, ANH-144), `by_machine`, `by_machine_and_status`,
`by_machine_and_local_ref`, `by_started_by`, `by_organization`,
`by_status_and_organization` (les séances actives ou en attente d'une
organisation, ou de toutes : ce que compte `dashboard.counters`, et les
séances actives que parcourt la tâche planifiée). Pour les
listes de séances (ANH-158, ANH-227, [§5](#sessionsts-lectures-seulement)), dix index
qui finissent tous par `startedAt` : `by_started`, `by_status_and_started`,
`by_organization_and_started`, `by_organization_and_status_and_started`,
`by_user_and_started`, `by_machine_and_started`,
`by_organization_and_machine_and_started`,
`by_organization_and_machine_and_status_and_started`,
`by_organization_and_user_and_started`,
`by_organization_and_user_and_status_and_started`. Les quatre derniers servent
à la fusion bornée des listes courtes du gestionnaire : organisation, machine
ou pratiquant et, si demandé, statut sont dans la plage d'index, sans filtre
après lecture. Convex les construit au
déploiement, sur les séances déjà présentes ; aucune migration n'est à lancer.
Ils n'ont encore été poussés sur aucun déploiement : seuls les tests en mémoire
les ont lus. Il en va de même de `by_user_and_status` et du champ `endCode`
(ANH-144), facultatif : les séances déjà présentes n'en ont pas.

Pour une nouvelle séance locale, l'attribution suit les droits courants du roster
de la machine. La console locale y nomme le passager qu'elle a pris dans ce roster
(`userId`, plus `subjectLabel` tant qu'elle a son nom en mémoire), depuis ANH-84 ; une
séance de banc ne nomme personne. Si elle ne peut pas être confirmée, la séance est conservée sans
passager ni libellé d'identité déclaré, avec la mention
`Passenger attribution unavailable at registration` dans `notes`. Les données
de séance restent disponibles selon les droits habituels sur la machine.
Une retransmission de la même référence locale retrouve la séance déjà créée,
sans modifier son attribution historique. Cette règle est appliquée dans le
source d'ANH-180 ; son effet en production dépend du redéploiement ANH-82.

### `training_telemetry` : télémétrie à 1 Hz

`sessionId`, `machineId`, `t`, `elapsedS`, `phase`, `bpm` (absent = pas de FC
fiable), `motorRpm`, `outputRpm`, `setpointMotorRpm`, `gLoad`, `safetyAction`.
Index `by_session_and_t`.

**Une ligne par `(sessionId, t)`.** Un point envoyé une seconde fois (réponse
perdue, console redémarrée) n'est pas inséré de nouveau : la ligne déjà là
reste telle qu'elle est, avec la date à laquelle le serveur l'a reçue la
première fois (`storeTelemetry`).

`t` est une date en ms Unix **sur l'horloge de la machine**. C'est l'identité
du point et son rang dans la séance. Ce qu'elle vaut dépend de la console :

- la console de ce dépôt, qui dit l'âge de sa séance, y écrit le début
  qu'elle a daté plus le temps écoulé sur son horloge monotone. Les lectures
  (`getSessionTelemetry`, `lastMeasuredAt`) placent alors `t` sur l'horloge
  du serveur (voir [Deux horloges](#deux-horloges)) ;
- une console antérieure, qui ne dit pas cet âge, y écrit l'heure murale de
  la machine à l'instant de la mesure. `t` est stocké et servi tel quel : il
  sert à l'axe des courbes et à dire quand un point a été **mesuré**
  (`lastMeasuredAt`).

Dans les deux cas `t` ne dit pas qu'une séance envoie encore : la date de
**réception** d'un point est le `_creationTime` que Convex donne à sa ligne
(`lastSignalAt`). Le site exige les deux, mesure et réception, pour afficher
une valeur comme actuelle (voir `getTrainingSession`).

### `training_events` : événements d'une séance

Les événements de l'enregistrement local d'une séance, tels que la machine les
relit dans son journal : verdicts, refus, phases, défauts du variateur,
commandes distantes, pré-vol, avertissements, fin.

`sessionId`, `machineId`, `seq`, `t` (ms Unix sur l'horloge de la machine,
lu comme `training_telemetry.t`), `kind`, `detail`, `actor` (`system`, `remote`
ou un identifiant opaque d'opérateur, jamais un nom). Index
`by_session_and_seq` et `by_organization`.

`seq` est le rang de l'événement dans l'enregistrement. **Une ligne par
`(sessionId, seq)`** : un événement envoyé une seconde fois n'est pas inséré
de nouveau (`storeEvents`). Aucune fonction publique ne lit encore cette
table : la machine l'écrit, les tests la lisent. Seuls les événements d'une
séance y arrivent, ceux de son enregistrement : rien de ce que la console
noterait hors séance n'est envoyé.

### `session_summaries_v2` : résumé d'une séance d'entraînement (ANH-89)

Une ligne par séance terminée (`completed` ou `failed`) d'entraînement (`auto`
ou `manual`), écrite par les seules fonctions internes de `sessionReports.ts` :
`organizationId` (celui de la séance), `sessionId`, `source`, `computedAt`,
`recordProblem` facultatif, `figures`. Index `by_session` et
`by_organization`. L'ancienne table `session_summaries` reste en lecture seule
([plus bas](#historique-de-lancien-mode-denregistrement-ecg-lecture-seule)).

**Deux sources, une seule ligne.**

1. **Provisoire** (`source: "telemetry"`) : quand la machine envoie la fin de
   la séance, `endTrainingSession` planifie `summarizeTelemetry`, qui lit la
   télémétrie à 1 Hz (par pages de 1000 points, 22 pages au plus, soit six
   heures) et les événements `verdict` (par pages de 500, 20 pages au plus).
   La console n'envoie la fin qu'une fois sa télémétrie acquittée
   ([raspberry-pi.md, 8.2](raspberry-pi.md#82-dans-quel-ordre)) ; un point
   arrivé après la fin n'entre pas dans le résumé provisoire.
2. **Final** (`source: "record"`) : dès que les trois fichiers qu'il lit
   (`manifest.json`, `ticks.csv`, `events.jsonl`) de l'enregistrement déposé
   sont vérifiés, `recordStorage.finishVerification` planifie
   `summarizeRecord`, sans attendre les blocs bruts. Il remplace le résumé
   provisoire ; un résumé provisoire ne remplace jamais un résumé final.

Les deux calculs sont idempotents : recalculés sur les mêmes lignes ou les
mêmes fichiers vérifiés (immuables), ils donnent le même résumé, seule
`computedAt` change. Le calcul final est borné quelle que soit la taille de
l'enregistrement : le manifeste (2 Mio au plus) est lu entier, `ticks.csv` et
`events.jsonl` (16 Mio au plus chacun) tranche par tranche de 1 Mio et ligne
par ligne, et aucun bloc brut n'est ouvert, qu'il y en ait 3 ou 36 000. Un
enregistrement illisible (fichier absent du stockage, trop grand, manifeste
qui n'est pas un objet JSON, octets qui ne sont pas de l'UTF-8, en-tête de
`ticks.csv` sans les colonnes lues) laisse le résumé provisoire, avec la raison
dans `recordProblem` (un code, jamais un contenu). La suppression d'un dépôt
(`recordStorage.remove`, rétention) ne supprime pas le résumé, comme elle
laisse la séance et sa télémétrie ; la durée de conservation relève d'ANH-172.

**Ce que contient `figures`, et d'où vient chaque définition**
(`convex/lib/sessionSummary.ts` les cite une par une) :

| Champ | Définition | Source de la définition |
|---|---|---|
| `durationS` | `t` du dernier tic de séance (`t > 0`), horloge monotone de l'enregistrement | [enregistrement.md, Tics](enregistrement.md#tics-5-hz) |
| `heartRate` | min, moyenne arithmétique, max de `hr_confirmed` sur les tics qui la portent (provisoire : `bpm` des points) | `TelemetrySnapshot.live_bpm` (`raspberry-pi/src/training/types.py`), écrit par `_row` (`raspberry-pi/src/record/session.py`) |
| `zone` | temps dans, au-dessus et en dessous de `[zone_low_bpm, zone_high_bpm]`, bornes comprises : chaque tic ajoute le temps écoulé depuis le tic précédent selon sa FC ; un tic sans FC confirmée ne compte nulle part ; `null` sans programme (séance manuelle) | `TrainingRuntime._accumulate` (`raspberry-pi/src/training/runtime.py`), `ZoneCounters` (`types.py`) |
| `maxOutputRpm`, `maxMotorRpm` | maximum de la vitesse mesurée du bras et du moteur, en valeur absolue | colonnes `output_rpm`, `measured_motor_rpm` |
| `reference.maxG`, `legTip.maxG` | maximum de `g_reference` et `g_leg_tip` : accélération **centripète** ω²r/9,80665 au rayon de référence et à la pointe du pied, pas la résultante avec la pesanteur (provisoire : `gLoad`, au rayon de référence seulement) | `output_rpm_to_g` (`raspberry-pi/src/units.py`) |
| `reference.maxGRate`, `legTip.maxGRate` | variation de g max : sur chaque fenêtre de 1 s (5 intervalles, 6 tics consécutifs) dont tous les tics sont jugés (mesure fraîche, pas de silence, ni `QUICK_STOP` ni `GO_SILENT`, variateur `OPERATION_ENABLED`, vitesse moteur au moins la consigne minimale du programme, 55 tr/min sans programme), \|g fin − g début\| / durée ; `null` dans le résumé provisoire | `arm_windows` (`simulation/invariants.py`) ; 55 tr/min : `min_run_motor_rpm` de `raspberry-pi/config/motion_limits.json` |
| `radiusM`, `legTip.measured` | rayons et rayon de la pointe mesuré ou estimé, si le manifeste porte la géométrie (la simulation ; la console écrit `geometry: null`) | [enregistrement.md, Contexte commun](enregistrement.md#contexte-commun-aux-deux-producteurs) |
| `verdicts`, `verdictsOmitted` | chaque règle de sécurité apparue sur un tic de séance, dans l'ordre d'apparition, avec l'action la plus sévère vue avec elle (32 règles gardées, les autres comptées) ; provisoire : les événements `verdict` | `rules_seen` (`simulation/invariants.py`), ordre de `SafetyAction` (`types.py`) |
| `endReason`, `endRule` | `end_reason` du manifeste (provisoire : `endReason` de la séance) ; pour une fin `safety_verdict`, la règle du premier tic `ending` ou `finished` | `EndReason` (`raspberry-pi/src/training/runtime.py`) |
| `warnings` | `record_degraded` (l'enregistrement dit lui-même qu'il lui manque des données), `lines_skipped` (lignes illisibles ignorées), `telemetry_truncated` | [enregistrement.md, Événements](enregistrement.md#événements) |

Deux écarts connus avec la console, vérifiés le 14 octobre 2026 sur quatre
scénarios de la simulation (`auto_jog_150_nominal`,
`auto_jog_150_leg_tip_estimate`, `hr_critical`, `estop_manual_27`) : le temps
en zone est classé ici sur la FC confirmée que l'enregistrement contient, la
console classe la FC filtrée de son suivi (`HeartRateTracker.usable`), qui
n'est pas enregistrée (1 s d'écart sur 1212 s pour `hr_critical`, aucun pour
`auto_jog_150_nominal`) ; la variation de g est calculée sur les colonnes g
écrites à quatre décimales, la simulation sur la vitesse moteur (écart au
quatrième chiffre). Les maxima de vitesse et de g, les règles et la règle de
fin y sont identiques.

**Pas encore dans le résumé** : le résultat du pré-vol (ANH-142) et le retour
du passager (ANH-146), que rien n'enregistre aujourd'hui. Chacun sera un champ
de `figures` calculé par `convex/lib/sessionSummary.ts` ; le rapport dit déjà
qu'ils manquent.

### Deux horloges

Un Raspberry Pi n'a pas d'horloge sauvegardée. Démarré sans réseau, il date ce
qu'il mesure d'une heure fausse, et son horloge est corrigée d'un coup quand
le réseau revient. C'est un cas ordinaire.

Ce que Convex en fait dépend d'**une** chose : la machine dit-elle depuis
combien de temps sa séance a commencé (`sessionAgeMs`, compté sur son horloge
monotone, envoyé avec `training/local` ou, pour un lancement du site, avec
`training/start` et la date de début `startedAt`) ?

**La console de ce dépôt le dit** depuis qu'elle relit ses enregistrements
pour les envoyer : pour toute séance en cours, et pour toute séance qu'elle
envoie après coup tant que le système du Pi n'a pas redémarré entre-temps
([raspberry-pi.md](raspberry-pi.md#85-les-dates)). Une console d'une version
antérieure ne le dit pas, quel que soit le contrat qu'elle annonce.

#### Une console qui ne dit pas son âge (version antérieure)

Convex se comporte comme avant l'arrivée de `sessionAgeMs`, à une exception
près : un point envoyé deux fois n'est stocké qu'une fois.

- Une séance démarrée à la machine est datée comme la machine l'a écrit
  (`startedAt`), une fin aussi. Une séance lancée du site commence à la
  réception de la confirmation.
- Chaque point porte l'heure murale de la machine à l'instant de sa mesure.
  Il est stocké tel quel, **sans borne** : aucune date n'est refusée, y
  compris après une correction de l'horloge de la machine en cours de séance,
  vers l'avant ou vers l'arrière.
- Les lectures servent ces dates telles quelles : c'est l'heure murale de la
  machine qui dit quand un point a été mesuré. Si elle est juste, un point
  envoyé en retard porte une date de mesure ancienne et le site ne le montre
  pas comme actuel. Si elle est fausse, le verdict du site l'est d'autant :
  en retard de 20 s ou plus, ou en avance de plus de 5 s, la machine n'est
  jamais montrée en direct (sens sûr), et une séance datée de 1970 s'affiche
  en 1970 ; **en avance de X, un point envoyé en retard peut être lu comme
  actuel jusqu'à 20 s + X après sa mesure**. Ce dernier cas, le seul dans le
  sens dangereux, existait avant et reste entier pour une telle console
  ([tableau-de-bord.md §6](tableau-de-bord.md#fraîcheur-recalculée-à-lhorloge)).
- `machineStartedAt` reste absent : la séance n'a pas d'axe machine.

#### Une console qui dit son âge

- **La machine date ses mesures sur son propre axe.** Le `t` d'un point ou
  d'un événement est le début de la séance tel que la machine l'a daté, plus
  le temps écoulé depuis sur son horloge monotone : une seule lecture de son
  heure murale, au début. Ce début est gardé dans `sessions.machineStartedAt`.
- **Le serveur date la séance.** `sessions.startedAt` est `sessionAgeMs` avant
  la réception de la déclaration. L'heure murale de la machine n'est pas lue.
- **Le décalage entre les deux** (`startedAt - machineStartedAt`) place toute
  date de la machine sur l'horloge du serveur : le `t` servi par
  `getSessionTelemetry` (et son `sinceT`), `lastMeasuredAt`, et `endedAt`
  (jamais avant `startedAt`). Une séance d'une machine datée de 1970 s'affiche
  à la date du serveur ; ses mesures en direct se lisent comme mesurées
  maintenant, et celles qu'elle envoie en retard comme mesurées quand elles
  l'ont été.
- **Une borne de cohérence**, lue sur l'axe de la machine : un point ou un
  événement est écrit si son `t` est entre une minute avant
  `machineStartedAt` et une minute après la fin (`SESSION_WINDOW_MARGIN_MS`) ;
  pas de borne haute tant que la séance n'est pas finie. Hors de ces bornes il
  est compté `rejected`. Comme `t` ne dépend que du début et du temps écoulé,
  une correction de l'horloge de la machine en cours de séance ne déplace
  aucun point : aucun n'est perdu pour cette raison.

Un âge n'est pas lu, et la séance est alors traitée comme celle d'une console
qui n'en dit pas, quand il est négatif, quand il placerait le début avant le
1er janvier 2024 (`EARLIEST_SESSION_START_MS`), ou, pour un lancement du site,
avant le lancement lui-même ou sans la date de début. Une séance est datée
**une fois**, à sa première déclaration : la déclarer de nouveau ne la redate
pas.

#### Ce qu'un âge faux peut faire, et ce qui le borne

L'âge vient de la machine. Un âge trop petit (0 pour une séance d'il y a une
heure) fait lire ses mesures comme plus récentes qu'elles ne sont. Pour une
console qui dit son âge, c'est la seule façon de faire lire comme actuelles
des données anciennes : son heure murale ne date plus rien, et le cas « en
avance de X » d'une console qui ne le dit pas n'existe plus pour elle. Convex
ne peut pas fermer cette façon-là contre une machine qui ment.

Un âge honnête, lui, est exact au délai d'acheminement de la déclaration
près : la console le calcule à l'envoi, le serveur le lit à la réception, et
toute mesure de la séance est lue plus récente de ce délai. C'est d'ordinaire
une fraction de seconde. La console n'attend pas une réponse plus de 3 s, mais
une requête que le serveur traiterait plus tard daterait la séance d'autant,
et la séance n'est datée qu'une fois : rien ne borne ce délai côté serveur.

Ce qui borne un âge faux :

- la machine est authentifiée par sa clé et n'écrit que dans ses propres
  séances : l'effet se limite à l'affichage de ses propres mesures ;
- le site ne montre jamais comme actuel un point daté de plus de 5 s **après**
  sa propre réception (`FUTURE_TOLERANCE_MS`). Un âge faux ne peut donc pas
  faire lire une mesure comme plus récente que l'instant où elle a été reçue ;
- le serveur refuse à bas coût l'âge que contredit ce qu'il sait déjà : pour
  un lancement du site, il connaît la date du lancement, et un âge qui ferait
  commencer la séance avant n'est pas lu. Pour une séance déjà déclarée, un
  second âge n'est pas lu du tout. Il ne sait rien d'autre d'une séance
  démarrée à la machine avant qu'elle la déclare : il ne peut pas borner
  davantage.

Rien n'est fait, côté serveur, pour refuser un point dont la date placée sur
son horloge serait postérieure à sa réception : ce serait perdre une mesure
pour une erreur d'âge, et le site écarte déjà ce cas de l'affichage.

### Historique de l'ancien mode d'enregistrement ECG (lecture seule)

| Table | Contenu |
|---|---|
| `ecg_data` | Lots d'ECG **déjà traité** sur le Pi (`values` en mV à `sampleRate` Hz) et métriques par canal (`heartRate`, `hrv`, `quality`…), écrits autrefois par l'enregistreur. **Plus aucune fonction n'y écrit.** Les lectures de `ecgData.ts` restent pour consulter l'existant. La console locale n'a jamais envoyé ces lots. |
| `session_summaries` | Résumé calculé autrefois à la fin d'une séance d'enregistrement (FC moyenne/min/max, HRV, ECG sous-échantillonné). **Plus rien n'en calcule ni n'en écrit** ; `getSummary` et `getSummaryWithEcg` lisent l'existant. Le résumé d'une séance d'entraînement est dans `session_summaries_v2` (ANH-89). |

### `machine_heartbeats`

Historique des heartbeats (`timestamp`, `batteryLevel`, `wifiStrength`,
`activeSessionId`). Les lignes de plus de 24 heures sont supprimées par lots
de 100 à chaque heartbeat de la machine et par la tâche quotidienne à 03:17 UTC.

### Santé machine (ANH-148)

Le heartbeat accepte un objet `health` optionnel. Chaque mesure absente du Pi
est `null`, jamais zéro par défaut : `cpu_temp_c`, `cpu_load_1m`, `mem_used_pct`,
`disk_free_mb` (MiB sur le volume des enregistrements), `uptime_s`,
`clock_offset_s` (erreur estimée du service d'heure), `link_rtt_ms` (dernier
heartbeat réussi), `bitalino_battery_pct`, `drive_thermal_state`,
`records_pending`, `incidents_open_high`. Les champs `bitalino_link` et
`drive_link` valent `up`, `down` ou `null`; `record_degraded` et `restart_loop`
sont booléens ou `null`. `preflight_last` vaut `ok`, `warn`, `block` ou `null`.
`counters` contient `rotation_hours`, `sessions_total`, `sessions_failed` et
`drive_faults_by_code` (au plus 128 codes), chacun nullable.

Un objet mal formé refuse tout le heartbeat (400). `machines.health` et
`healthUpdatedAt` portent le dernier rapport; un ancien client omettant health
efface ces deux champs. `machine_health_history` garde des périodes de cinq
minutes avec minimum, maximum et dernier rapport, pendant 90 jours. Sa purge
quotidienne poursuit les lots pleins par une mutation planifiée. Aucune purge
de `training_telemetry` n'est activée sans politique de conservation distincte.

`machineHealth.get` applique les droits d'accès à la machine et renvoie le
rapport et les dernières 24 heures. La carte indique les valeurs anciennes
après 30 secondes; les seuils d'avertissement sont CPU > 70 °C, disque < 1 Gio,
écart d'heure absolu > 5 s et batterie < 30 %.

### `notifications` et `notification_settings` : alertes aux gestionnaires (ANH-90)

`notifications` : une ligne par destinataire et par alerte, écrite par le
serveur seul ([§5](#notificationsts-alertes-aux-gestionnaires-anh-90)).
`organizationId` (celle de la machine, obligatoire), `userId` (le
destinataire), `type` (`machine_offline`, `session_failed`, `drive_fault`,
`safety_verdict`), `machineId`, `sessionId?`, `detail?`, `count`,
`createdAt`, `updatedAt`, `read`, `emailId?` (l'e-mail mis en file pour cette
ligne). `detail` est un **code envoyé par la machine**, jamais une phrase :
le mnémonique du défaut du variateur (`OHF`) ou l'action exigée par un
verdict (`RAMP_DOWN`). Aucune ligne ne contient de fréquence cardiaque, de
nom de passager ni de raison de fin de séance : la raison reste sur la
séance, et la liste ne la lit qu'au moment de l'afficher, à qui peut lire la
séance. Index `by_user_and_organization_and_updated` (la liste),
`by_user_and_organization_and_read` (le nombre de non lues),
`by_user_and_organization_and_machine_and_updated` et
`by_user_and_organization_and_machine_and_read` (liste et non lues des seules
machines liées au gestionnaire, avant la coupe),
`by_user_and_machine_and_type_and_read` (le regroupement), `by_user`
(suppression d'un compte) et `by_updated` (purge à 90 jours).

`notification_settings` : une ligne par compte, `email` (un booléen
facultatif par type : l'alerte part aussi par e-mail), `updatedAt`. Un type
non choisi prend sa valeur par défaut : oui pour `session_failed`,
`drive_fault` et `safety_verdict`, non pour `machine_offline` (une machine
éteinte chaque soir en enverrait un chaque soir). Index `by_user`.

---

## 3. Règles d'autorisation

### Invitations et identité du patient

`users.createPatient` est une action : elle prépare une fiche et une invitation
privée, puis appelle Clerk avec le rôle `org:patient`. Elle annonce l'envoi
seulement après confirmation de Clerk. Un échec garde la préparation pour une
reprise sans seconde fiche. L'appartenance et les liens gestionnaires restent
inactifs jusqu'à l'acceptation.

Le webhook vérifie auprès de l'API Clerk les adresses vérifiées du compte avant
de relier une fiche pré-créée dans l'organisation. Une adresse présente dans
une autre organisation peut recevoir une invitation sans révéler son nom,
son rôle ni son identifiant Convex ; son compte existant est réutilisé après
acceptation. Les champs communs ne sont pas remplacés par ceux de l'invitation.
Plusieurs anciennes fiches ne produisent pas de refus distinct à l'invitation :
la préparation reste sans fiche attribuée. L'acceptation peut sélectionner un
compte déjà relié à l'identifiant Clerk vérifié ; sinon le conflit exige une
réconciliation humaine. Aucune fusion de dossiers ni réattribution de séances
n'est automatique.
`user.deleted` conserve la fiche et rend ses appartenances inactives ; un
événement ancien ne réactive pas ce compte. L'accueil ne crée plus de fiche.

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
avec un code stable (`ConvexError`, transmis au navigateur, traduit par le site,
voir [Codes d'erreur](#codes-derreur-des-fonctions-du-site-anh-194-et-anh-90)) :

| Situation | Code |
|---|---|
| `org_id` absent, `null` ou vide alors que `ANHEART_ORG_ID` est définie | `no_active_organization` |
| `org_id` inconnu du miroir `organizations` | `organization_not_synchronized` |
| `org_role` absent ou autre que les trois rôles ci-dessus | `unknown_organization_role` |
| `org_id` qui n'est pas une chaîne | `malformed_organization_claim` |
| Compte sans organisation ni appartenance active (transition) | `no_organization` |

`users.getCurrentUser` ne lève pas dans ces cas : il renvoie le compte avec
`organization: null` et `role: "user"`, pour que le site sache quoi afficher.
Son champ `organizationAccess` porte le même code que le tableau, ou `granted`
quand l'organisation est acceptée (ANH-90 : le code levé et le champ sont la
même valeur, une seule liste dans `convex/lib/auth.ts`). Le
site dit au visiteur pourquoi il ne voit rien et lui propose de choisir une
organisation ([tableau-de-bord.md §2](tableau-de-bord.md#2-rôles-et-accès)).

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
| `requireAuth` | Un jeton Clerk valide est présent (sinon le code `not_authenticated`). |
| `getCurrentUserOrThrow` | Une ligne `users` existe pour ce `clerkId` et n'a pas été supprimée dans Clerk (sinon le code `account_not_registered`), et l'appel a une organisation acceptée (sinon un code du tableau ci-dessus). Renvoie le compte avec le rôle et l'organisation **de l'appel**. Depuis ANH-90, ces deux refus sont des `ConvexError` : en production le site lit leur code et demande au visiteur de se reconnecter, au lieu d'une phrase générique avec une référence. |
| `requireRole(r)` | Le rôle de l'appel est dans `r` (sinon le code `not_authorized`). |
| `inScope(organisation)` | admin Anheart ; ou la ligne appartient à l'organisation de l'appelant. Une ligne sans organisation n'est dans le périmètre que de l'admin Anheart. |
| `canManageUser(cible)` | admin Anheart ; ou, si la cible est **membre actif de l'organisation de l'appelant** : admin de cette organisation, ou gestionnaire lié à la cible dans `user_gestionnaires` **dans cette organisation**. |
| `canAccessUser(cible)` | soi-même, ou `canManageUser`. |
| `canAccessMachine(m)` | admin Anheart ; ou, si la machine appartient à l'organisation de l'appelant : admin de cette organisation, ou gestionnaire lié à `m` dans `machine_gestionnaires`. **Un `user` n'a jamais accès par cette règle.** |
| `canManageMachine(m)` | Identique à `canAccessMachine`. |
| `canAccessSession(s)` | admin Anheart ; ou, si la séance appartient à l'organisation de l'appelant : son pratiquant, ou qui a accès à sa machine. |
| `requireGestionnaireAdmin(g)` | admin Anheart, ou admin de l'organisation de `g` ; et `g` tient le rôle `gestionnaire` dans l'organisation où la décision s'applique : celle de l'appelant, ou, pour l'admin Anheart, l'organisation principale de `g`. Sinon le code `gestionnaire_not_found` (compte inconnu, ou, pour un admin d'organisation, compte qui n'est pas membre actif de la sienne : même réponse) ou `not_a_gestionnaire`. Le rôle de l'appelant est vérifié avant toute lecture de `g`, et le rôle de `g` est lu dans son appartenance, pas dans le miroir `users.role`. Renvoie l'appelant, `g` et cette organisation. |

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
- **Clés d'une machine** (ANH-165) : lister, créer, révoquer : admin Anheart,
  ou admin de l'organisation de la machine. Un gestionnaire, même de la
  machine, n'y a pas accès ([`machineKeys.ts`](#machinekeysts-clés-des-machines-anh-165)).
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
  Une appartenance inactive n'est jamais réactivée par ce passage ; un miroir
  déjà reçu par webhook n'est pas réécrit depuis les revendications du jeton.
- **Compte partagé entre organisations** : les champs communs du dossier
  (nom, langue, FC maximale et année de naissance) ne peuvent être modifiés
  par le personnel pour un autre utilisateur que par l'admin Anheart tant que
  plusieurs appartenances actives existent. Le titulaire conserve la possibilité
  de modifier lui-même son nom et sa langue.
  Les gestionnaires conservent leurs droits habituels sur un compte qui n'a
  qu'une organisation active. Les données ne sont pas dupliquées par organisation.
- **Demander l'arrêt de sa séance** : le passager peut le demander depuis une
  autre organisation active de son compte. Cette exception ne donne aucun accès
  supplémentaire aux mesures ou aux séances des autres personnes.
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
  compte en conflit qu'à un appelant ayant le droit de le lire ; sinon il dit
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

Toutes les erreurs des fonctions publiques de ce fichier sont des `ConvexError`
dont la donnée est un code de la table `convex/lib/errors.ts`, levé par
`refusal(code)` : elles arrivent intactes dans le navigateur, même en
production, et le site affiche leur traduction (`errors.<code>` dans
`messages/*.json`). Depuis ANH-90, plus aucune phrase : `convex/errors.test.ts`
échoue si `training.ts` construit encore une `ConvexError` de lui-même. Les
fonctions internes des routes machine refusent avec les codes du contrat
(`machineError`, voir plus bas).

### Paramètres médicaux de la version courante

`convex/training.ts` ne porte plus de constantes médicales. Il lit dans
`medical_parameters_versions` la version courante et utilise ses valeurs pour
les contrôles de lancement :

| Valeur du fichier signé | Usage dans Convex |
|---|---|
| `zone_ceiling_fraction` | Plafond de la zone haute, appliqué à la FC max retenue. |
| `min_rider_age` | Âge minimum d'un lancement auto distant. |
| `hr_max_formula` | Formule d'estimation de la FC max ; actuellement `tanaka`, arrondie comme sur le Pi. |

Sans version courante, le lancement à distance échoue fermé avec
`medical_parameters_unavailable`. Les règles de lecture et de validation
suivent `raspberry-pi/src/medical.py`, `raspberry-pi/src/local_config.py` et
`raspberry-pi/src/training/motion.py`. Les bornes d'entrée (FC max 100-220 bpm,
âge 10-100 ans) restent des limites de validation, pas des paramètres
médicaux modifiables. Les calculs partagés sont dans `lib/medicalParameters.ts` :
`estimateHrMax` et `zoneCeilingBpm`.

`LIVE_FRESH_MS` (90 000 ms) reste une constante technique de fraîcheur, définie
dans `lib/training.ts`, importée par `convex/training.ts`, par la tâche
`checkOfflineMachines` de `convex/machines.ts` et par le site, qui recalcule la
fraîcheur chaque seconde sur l'horloge du serveur
([tableau-de-bord.md §6](tableau-de-bord.md#fraîcheur-recalculée-à-lhorloge)).
`lib/training.ts` reste sans import `@/` ni code réservé au navigateur.

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

1. `zoneHighBpm > floor(0,9 × FC max)` → le code `zone_above_ceiling` ;
2. `hardMaxBpm > FC max` → le code `hard_max_above_hr_max`.

Le code ne porte ni la zone ni la FC max (ANH-90) : la fenêtre de lancement
du site les affiche avant l'envoi, et le site traduit le code.

Chaque règle est écrite comme ce qui est accepté : un programme ne passe que si
sa valeur est connue comme inférieure ou égale à la limite. Une valeur qui
n'est pas un nombre, d'un côté ou de l'autre, donne donc un refus.

### Queries et mutations publiques

| Fonction | Type | Arguments | Autorisation | Effet / retour |
|---|---|---|---|---|
| `grantLaunchRight` | mutation | `machineId`, `userId` | admin ou gestionnaire de la machine **et** de l'utilisateur ; cible de rôle `user` | Insère le droit. Sans effet s'il existe déjà. Codes de refus : `machine_not_managed`, `machine_not_found`, `launch_right_patients_only`, `user_not_managed`. |
| `revokeLaunchRight` | mutation | `machineId`, `userId` | admin ou gestionnaire de la machine | Supprime le droit s'il existe. Code de refus : `machine_not_managed`. |
| `listLaunchRights` | query | `machineId` | admin ou gestionnaire de la machine (sinon `[]`) | `[{userId, name, email, hrMax (retenue ou null), grantedByName, createdAt}]`. |
| `setUserPhysiology` | mutation | `userId`, `hrMax?` (nombre ou `null` pour effacer), `birthYear?` (idem) | pas un `user` ; `canAccessUser` | Pour chaque valeur fournie : `null` l'efface ; sinon elle doit être un entier fini, puis dans ses bornes (FC max 100-220, âge 10-100 ans). Rien n'est écrit si l'une des deux est refusée. Codes de refus (ANH-90) : `not_authorized` (un `user`), `user_not_managed`, `invalid_hr_max` (pas un entier, ou hors bornes), `invalid_birth_year` (idem). |
| `listMachineProfiles` | query | `machineId` | voir la machine (règle entraînement) | Programmes triés par nom. |
| `listLaunchableMachines` | query | - | connecté | Machines où l'on peut lancer : admin Anheart = toutes ; dans l'organisation de l'appelant seulement : son admin = toutes, gestionnaire = les siennes, user = celles où il a le droit. Machines supprimées ou sans organisation exclues, même pour l'admin Anheart. Pour chacune : `status`, `lastHeartbeat`, `serverNow`, `programsEnabled`, `live`, `profiles`, `myHrMax`, `myFitnessValidUntil`, `myLaunchRefusal`, `medicalParametersVersion`, `medicalParametersStatus` et `medicalParametersReferenceVersion` (voir §2, `machines`). `myLaunchRefusal` donne `rider_not_fit`, `fitness_expired`, `consent_missing` ou `null` pour l'appelant dans l'organisation de cette machine. `myFitnessValidUntil` donne l'échéance de son aptitude valide ou `null` ; le site la rejuge avec l'heure serveur qui avance depuis `serverNow`. |
| `launchAutoSession` | mutation | `machineId`, `profileId`, `userId?`, `totalDurationS?`, `notes?` | voir §3 | Crée une séance `pending` (`kind: auto`, `origin: remote`). Retourne son id. Contrôles et codes de refus ci-dessous. |
| `requestStop` | mutation | `sessionId` | pratiquant ou admin / gestionnaire de la machine (sinon `session_not_found` pour une séance d'une autre organisation, `session_not_managed` pour une de la sienne) | `pending` → `failed` avec « Cancelled before start by … » (aucune alerte : c'est une annulation). `active` ou `interrupted` → pose `stopRequestedAt` (une seule fois) ; une séance `interrupted` reste ce qu'elle est, et le Pi lit la demande s'il revient avec la séance encore en cours. Autres statuts : rien. |
| `getMachineLive` | query | `machineId` | voir la machine | `{status, programsEnabled, live, stale, serverNow, medicalParametersVersion, medicalParametersStatus, medicalParametersReferenceVersion}` ou `null`. `stale` = pas d'état ou plus vieux que 90 s **au moment où la query s'exécute**. `serverNow` = l'heure du serveur dans cette réponse. Les champs médicaux sont calculés comme en §2 (`machines`). |
| `getSessionTelemetry` | query | `sessionId`, `sinceT?`, `limit?` | pratiquant ou admin / gestionnaire | Points du plus ancien au plus récent. `limit` par défaut 3600, borné à 1..7200 (les **derniers** points). Pour une séance dont la machine dit l'âge, le `t` servi et `sinceT` sont sur l'horloge du serveur ; sinon ce sont les dates écrites par la machine ([Deux horloges](#deux-horloges)). |
| `getTrainingSession` | query | `sessionId` | pratiquant ou admin / gestionnaire | Champs d'entraînement de la séance, nom de la machine, `interruptedAt` pour une séance `interrupted`, `endCode` pour un lancement refusé avant que sa machine l'arme, et `canStop` (statut `pending`, `active` ou `interrupted`, et droit d'arrêt). Pour une séance **active**, deux dates du point de plus grand `t`, celui que le site affiche (`null` toutes les deux sinon). `lastSignalAt` = sa réception par le serveur (`_creationTime` de sa ligne, pas son `t`), ou, sans point, le début daté par le serveur (`startedAt` d'une séance lancée du site, `_creationTime` d'une séance enregistrée par la machine). `lastMeasuredAt` = sa mesure selon la machine (son `t`), placée sur l'horloge du serveur quand la machine dit l'âge de sa séance ([Deux horloges](#deux-horloges)), `null` sans point : un point reçu à l'instant peut avoir été mesuré une heure plus tôt (renvoi après une coupure). `serverNow` comme pour `getMachineLive`. Le site n'affiche une valeur comme actuelle que si les deux dates ont moins de 20 s sur `serverNow` ([tableau-de-bord.md §6](tableau-de-bord.md#panneau-dentraînement-vue-en-direct)). La query se relance à chaque paquet de points. |

**Contrôles de `launchAutoSession`, dans l'ordre** (code de refus, traduit par
le site) :

1. Droits (`launcherRefusal`, la même règle que celle revérifiée à
   l'armement) : `launch_for_self_only` (un patient lance pour un autre),
   `launch_right_missing` (patient sans droit de lancement sur la machine,
   dans son organisation), `machine_not_managed` (admin ou gestionnaire qui
   n'atteint pas la machine), `user_not_managed` (pratiquant qu'il ne gère
   pas).
2. Machine et appartenance : `machine_not_found` (absente ou supprimée),
   `rider_not_allowed` (pratiquant qui n'est pas membre actif de l'organisation
   de la machine).
3. Aptitude et consentement : `rider_not_fit` si la dernière saisie n'est pas
   courante, `fit` et validée ; `fitness_expired` si sa date de validité est
   passée ; `consent_missing` si le consentement courant est absent ou retiré.
   Cet ordre est aussi celui des codes renvoyés dans `myLaunchRefusal`.
4. Machine : `machine_offline` (statut écrit `offline`).
5. Paramètres médicaux : `medical_parameters_unavailable` si aucune version
   courante n'est inscrite ; `medical_parameters_unsigned` si le heartbeat a
   explicitement annoncé `null` (fichier absent ou signature non vérifiée sur
   le Pi). Ce refus arrive avant celui des programmes désactivés.
6. Programmes : `programs_disabled` (programmes auto désactivés : séances
   manuelles seulement, à la machine).
7. Une séance à la fois (ANH-144) : `machine_busy` si la machine se dit en
   séance (`in_session`) ou a une séance qui peut la tenir ; puis `rider_busy`
   si le pratiquant en a une, sur
   n'importe quelle machine, de n'importe quelle organisation. Ce qui tient,
   et ce qui ne tient pas, est dit [plus bas](#revérification-à-larmement-anh-144).
   Avant ces deux contrôles, les lancements de ce pratiquant et de cette
   machine qui ont attendu plus que le délai sont passés `failed` (expirés),
   comme la tâche planifiée l'aurait fait : ils ne tiennent plus personne.
8. Programme : `programme_not_on_machine`.
9. Pratiquant : `user_not_found` (compte disparu), puis `rider_hr_max_missing`
   (ni FC max utilisable ni année de naissance), puis le refus de zone
   (`zone_above_ceiling`, puis `hard_max_above_hr_max`), puis
   `rider_birth_year_missing` (l'année est exigée **même** si la FC max est
   mesurée), puis `rider_too_young` (moins de `minRiderAge` ans dans la version
   courante). Une valeur enregistrée qui n'est pas un entier fini compte comme non renseignée (voir
   « Valeur utilisable » plus haut) : le lancement est refusé par
   `rider_hr_max_missing` pour la FC max, ou pour l'année de naissance sans FC
   max mesurée, et par `rider_birth_year_missing` pour l'année de naissance à
   côté d'une FC max mesurée. Rien n'est mis en file.
10. Durée : `invalid_duration` (un nombre fini, strictement positif).

La séance créée copie le programme (zone, durée, ou la durée demandée),
`subjectHrMax`, `subjectAge`, `subjectLabel` (nom du pratiquant) et
`operatorName` (nom du lanceur).

### Fonctions internes (appelées par les routes HTTP)

| Fonction | Rôle |
|---|---|
| `updateLive` | Écrit `machines.live` (et `programsEnabled` s'il est fourni). Si l'état dit le variateur en défaut et que le précédent ne le disait pas, planifie l'alerte `drive_fault` (ANH-90). Refuse une machine dont la ligne a disparu (`machine_not_found`). |
| `syncProfiles` | Supprime tous les programmes de la machine, insère la nouvelle liste (dans l'organisation de la machine), met à jour `programsEnabled`. Retourne `{count}`. |
| `getRoster` | Patients détenant le droit sur la machine **et membres actifs de son organisation** : `{userId, name, hrMax, birthYear, fit, fitnessValidUntil, consent, fitnessQuestionnaireVersion}` ([`roster` du contrat 1.6](../contracts/machine-api.json)). `hrMax` est celle que `launchAutoSession` utiliserait (mesurée, sinon estimée depuis l'année de naissance ; `null` si aucune n'est utilisable) ; `birthYear` est un entier ou `null`. `fit`, `fitnessValidUntil` et `consent` décrivent l'aptitude et le consentement dans l'organisation de la machine ; `fitnessQuestionnaireVersion` donne la version de la saisie validée qui rend `fit` vrai, sinon `null`. Aucune réponse au questionnaire ni note de validation n'est envoyée. La console 1.5 ignore ces champs ; la console 1.6 les vérifie avec `cleared_to_board` avant tout départ avec personne à bord ([console-locale.md](console-locale.md)). |
| `getPendingTrainingSession` | Première séance `pending` de `kind: auto` de la machine que la revérification à l'armement accepterait, au format attendu par le Pi. Jamais un lancement expiré ni refusé, même avant le passage de la tâche planifiée, ni un lancement qui expire dans moins de `POLL_MARGIN_S` (45 s). |
| `markTrainingStarted` | Revérifie d'abord le lancement ([ci-dessous](#revérification-à-larmement-anh-144)). Refusé : la séance passe `failed` avec `endCode`, la machine reste comme elle était, et la fonction rend `{armed: false, code, message}`, que la route répond **409**. Une confirmation répétée d'un lancement déjà refusé ainsi reçoit la même réponse. Accepté : `pending` → `active`, `startedAt` = maintenant, machine `in_session`, et `{armed: true}`. Si la machine envoie sa date de début **et** l'âge de la séance : `startedAt` = cet âge avant maintenant (jamais avant le lancement, sinon l'âge n'est pas lu) et `machineStartedAt` = sa date. Refuse une séance d'une autre machine (`session_not_found`) ou qui n'attend plus son départ pour une autre raison (`session_not_pending` : annulée sur le site, déjà démarrée, terminée). |
| `sweepPendingSessions` | Tâche planifiée toutes les minutes : passe `failed`, avec `endCode`, tout lancement `pending` (`kind: auto`) que la revérification refuse, expirés compris, et planifie pour chacun l'alerte `session_failed` (ANH-90). Rend `{failed}`. Les séances de l'ancien mode ne sont pas touchées. |
| `registerLocalSession` | Crée (une seule fois par `localRef`) une séance `active`, `origin: local`, dans l'organisation de la machine, et passe la machine `in_session`. Le pratiquant nommé par la machine n'est gardé que s'il est membre actif de cette organisation ; la séance est enregistrée dans tous les cas. `startedAt` = la date envoyée par la machine. Si elle dit aussi l'âge de la séance : `startedAt` = cet âge avant maintenant, et sa date devient `machineStartedAt` ([Deux horloges](#deux-horloges)). Un second appel avec le même `localRef` renvoie la séance sans la redater. |
| `endTrainingSession` | `completed` ou `failed` avec `endReason`, machine `online`, `interruptedAt` effacé. Une séance `interrupted` est terminée comme une séance active : la fin arrivée tard la clôt avec sa date et sa raison. Idempotent. `endedAt` = la date de fin écrite par la machine, ou la réception si elle n'en envoie pas. Pour une séance qui a un axe machine, la date de la machine est placée sur l'horloge du serveur, et jamais avant `startedAt`. Planifie le résumé provisoire de la séance (`sessionReports.summarizeTelemetry`, ANH-89) et, pour une séance `failed`, l'alerte `session_failed` (ANH-90). |
| `getTrainingStatus` | `{status, active, stopRequested}`. `active` vaut vrai pour une séance `active` **et** pour une séance `interrupted` : le Pi arrête sa séance sur `active: false`, et Convex ne demande jamais l'arrêt d'une séance faute d'en avoir des nouvelles. |
| `storeTelemetry` | Insère les points de la séance (qui doit appartenir à la machine), dans l'organisation de la séance, **une seule fois chacun**. Un point déjà stocké (même `(sessionId, t)`) est compté dans `duplicates` ; pour une séance qui a un axe machine, un point daté hors de la séance est compté dans `rejected` ; ni l'un ni l'autre n'est écrit. Retourne `{stored, duplicates, rejected}`. |
| `storeEvents` | La même chose pour les événements : un événement est un `(sessionId, seq)`. Retourne `{stored, duplicates, rejected}`. Les événements nouvellement stockés peuvent planifier une alerte `safety_verdict` ou `drive_fault` (ANH-90). |

Les refus de `markTrainingStarted`, `registerLocalSession`,
`endTrainingSession`, `storeTelemetry` et `storeEvents` sont levés par
`machineError(code, message)` (`convex/lib/contract.ts`) : la route les
renvoie avec leur code stable (voir [section 6](#6-routes-http-machine-convexhttpts)).
Le refus à l'armement n'est pas levé : il doit écrire l'échec de la séance,
qu'une erreur annulerait. `markTrainingStarted` le rend, et la route répond 409.

### Revérification à l'armement (ANH-144)

Un lancement est vérifié au clic sur le site, puis **de nouveau** au moment
où la machine l'arme : ce qui a changé pendant qu'il attendait compte. La même
fonction (`armingRefusal`) sert à trois endroits : le poll (un lancement
qu'elle refuserait n'est pas offert), la confirmation du départ
(`markTrainingStarted`, route `training/start`) et la tâche planifiée
`sweep-pending-sessions` (toutes les minutes). Dans l'ordre, avec le code
stable du refus (`ARMING_REFUSALS`, `convex/training.ts`, et
`arming_refusals` de `contracts/machine-api.json`) :

| # | Contrôle | Code |
|---|---|---|
| 1 | Le lancement attend depuis **au plus** `PENDING_SESSION_TTL_S` (300 s par défaut), compté depuis le lancement (`startedAt` d'une séance `pending`), sur l'horloge du serveur. `endReason` vaut alors « expired before the machine armed it ». | `expired_before_start` |
| 2 | La machine existe et n'est pas supprimée. Une machine supprimée est refusée à sa clé (401) avant que son départ soit lu : ce code est surtout celui qu'écrit la tâche planifiée. | `machine_deleted_before_start` |
| 3 | La machine est toujours dans l'organisation de la séance. | `organization_changed_before_start` |
| 4 | Celui qui a lancé (`startedById`) pourrait lancer **maintenant** cette séance, dans l'organisation de la machine, sous la règle de `launchAutoSession` : un patient pour lui-même, avec son droit de lancement (`machine_user_permissions`) ; un gestionnaire sur une machine qu'il gère, pour un patient qu'il suit ; un admin de l'organisation ; l'admin Anheart. Son rôle est relu dans ses adhésions actives (`rolesHeldNow`, `convex/lib/auth.ts`), pas dans un jeton : personne n'appelle à ce moment-là. Le pratiquant a toujours son compte (ni effacé, ni marqué supprimé dans Clerk) et reste membre actif de l'organisation. Un lancement sans `startedById` est refusé. | `right_revoked_before_start` |
| 5 | Depuis 1.6 (ANH-143), la dernière saisie d'aptitude est de la version courante du questionnaire, `fit` et validée, sa validité n'est pas dépassée, puis le consentement au texte courant est actif dans l'organisation de la séance. Un changement pendant l'attente est refusé, dans cet ordre. | `rider_not_fit`, `fitness_expired`, `consent_missing` |
| 6 | L'âge du pratiquant est connu et atteint l'âge minimum (`minRiderAge`) de la version médicale courante. | `age_refused_before_start` |
| 7 | La FC max retenue du pratiquant est connue et **égale** à `subjectHrMax`, celle que le lancement a donnée à la machine, qui y ajuste le programme. Une valeur changée, même dans le bon sens, est refusée sans recalcul : la machine ne tourne jamais sur l'ancienne. L'estimation par l'âge peut changer au 1er janvier (UTC), selon l'âge (de 41 à 42 ans, elle reste à 179 bpm) : un lancement en attente à ce moment-là peut être refusé. | `hr_max_changed_before_start` |
| 8 | Le pratiquant n'a aucune autre séance qui peut le tenir (règle ci-dessous), par exemple une séance démarrée à une autre machine pendant que celle-ci attendait. | `rider_busy` |

La zone cible n'est pas revérifiée à l'armement : aucune version ne peut être
inscrite pendant qu'un lancement auto attend.

Les lancements auto déjà expirés sont échoués au moment de l'inscription.

Un lancement refusé passe `failed`, avec `endedAt`, `endReason` (la phrase
anglaise du refus) et `endCode` (le code). Chaque code de refus à l'armement
est aussi dans la table `convex/lib/errors.ts`, et le site le traduit comme
tout code de cette table (`errors.<code>`) : « Séance échouée » puis « Droit
retiré avant le départ », par exemple
([tableau-de-bord.md §5.3](tableau-de-bord.md#53-lancer-une-séance-auto)).
Chaque échec de ce genre, qu'il vienne de la tâche planifiée, de la route de
départ, d'un nouveau lancement qui expire le précédent ou de l'inscription
d'une version qui échoue les lancements expirés, est aussi une alerte
`session_failed` à ses gestionnaires (ANH-90,
[§5](#notificationsts-alertes-aux-gestionnaires-anh-90)) : `failBeforeStart`
ne fait que la planifier, une mutation à part l'écrit, et rien de ce qui s'y
passe ne peut faire échouer ni annuler la tâche, la route ou le lancement.
L'alerte porte l'identifiant de la séance, rien du pratiquant ; la page des
alertes traduit son `endCode`.
Une séance échouée ainsi n'est ni active ni en attente pour les compteurs du
tableau de bord.

**Délai d'attente.** `PENDING_SESSION_TTL_S` est une variable d'environnement
du déploiement Convex (`npx convex env set PENDING_SESSION_TTL_S 300`), en
secondes entières, de 90 à 600. Absente, ou toute autre valeur : 300. Elle
n'est posée sur aucun déploiement aujourd'hui. La confirmation applique le
délai à l'instant même ; la tâche planifiée passe le lancement `failed` dans
la minute qui suit. Le poll cesse de l'offrir **45 s avant** l'échéance
(`POLL_MARGIN_S`) : la console de ce dépôt demande le départ après la confirmation
de son opérateur, avant d'armer, et n'arme que sur un 2xx de `training/start`.
Sur un 409, elle n'arme pas et n'envoie pas de fin ; une console antérieure, qui
arme avant de confirmer, s'arrête aussitôt sur la rampe ordinaire. Une confirmation reste acceptée jusqu'à
l'échéance elle-même. La borne basse de 90 s laisse un lancement offert 45 s
au moins.

**Une séance à la fois.** Une séance **tient** son pratiquant et sa machine
quand elle est :

- `pending` et pas expirée (une séance `pending` de l'ancien mode, sans
  `kind`, tient sa machine comme avant, jusqu'à son annulation ou sa
  migration, mais pas son pratiquant) ;
- `active` (séance d'entraînement) ;
- `interrupted` tant que sa machine ne s'est pas manifestée depuis
  (`lastHeartbeat` ≤ `interruptedAt`) : elle peut encore tourner, sur une
  machine dont le lien est coupé. Une machine qui a envoyé un heartbeat
  depuis sans la nommer ne la fait pas tourner, puisqu'un heartbeat qui la
  nomme l'aurait rendue `active` : elle ne tient plus rien.

`completed`, `failed` et les séances démarrées de l'ancien mode ne tiennent
rien. `launchAutoSession` refuse `machine_busy` et `rider_busy` sur cette
règle ; la revérification à l'armement refuse `rider_busy`. Deux lancements
simultanés ne passent pas tous les deux : une mutation Convex est
transactionnelle, la seconde relit ce que la première a écrit. Le refus
`rider_busy` ne dit ni quelle machine ni quelle organisation tient le
pratiquant.

**Limites.** Une séance `interrupted` dont la machine ne revient jamais (et
dont la fin n'arrive jamais) tient son pratiquant et sa machine sans fin :
aucune fonction ne la clôt. La console de ce dépôt demande le départ après
la confirmation de son opérateur et avant d'armer, et n'arme que sur un 2xx
de `training/start`. Sur un 409, elle n'arme pas et n'envoie pas de fin ;
une console antérieure, qui arme avant de confirmer, s'arrête aussitôt sur
la rampe ordinaire. Le poll n'offrant déjà pas un lancement refusé, il reste
les secondes entre le poll et la confirmation. Une confirmation restée sans
réponse (réseau coupé) ne fait pas armer la console. La tâche planifiée passe
pendant ce temps le lancement `failed` (expiré, ou refusé pour une autre
raison), ce qui libère son pratiquant. Si Convex a accepté la confirmation
mais que son 2xx s'est perdu, la console reste non armée ; une nouvelle
confirmation reçoit `session_not_pending` (400), sans armement, et la machine
peut rester tenue jusqu'à l'interruption.

---

## 5. Autres fonctions publiques

### Codes d'erreur des fonctions du site (ANH-194 et ANH-90)

Convex masque en production le texte d'une `Error` simple : le site n'afficherait
qu'une phrase générique avec une référence. Les refus des fonctions que le site
appelle sont donc des `ConvexError` dont la donnée est un **code stable**, par
exemple `machine_in_session` : `throw refusal("machine_in_session")`. La table
des codes et de leur sens est unique, `convex/lib/errors.ts`, avec les codes
déjà levés ailleurs (invitations, profil en attente, `rate_limited` du
limiteur par compte, levé comme un objet `{code, message, retryAfterMs}`). Le
site traduit chaque code par la clé `errors.<code>` de `messages/fr.json` et
`messages/en.json` (`lib/feedback.ts`). `convex/errors.test.ts` vérifie que
chaque code a ses deux traductions, qu'aucune traduction n'est sans code,
qu'aucun code levé par une source du dossier n'est absent de la table, et
quelles phrases restent (ci-dessous).

Ce qui lève un code : `users.ts` et `machines.ts` (ANH-194) ; depuis ANH-90,
la connexion (`not_authenticated`, `account_not_registered`), les refus
d'organisation (§3), tout `training.ts` (droit de lancement, physiologie,
droits du lanceur, machine hors ligne ou occupée, programme, pratiquant,
zone et FC limite du programme, âge, durée, arrêt d'une séance ; les codes
`machine_busy`, `rider_busy` et ceux du refus à l'armement viennent
d'ANH-144), `softwareReleases.ts`, `recordStorage.remove` et
`notifications.ts`. Aucune fonction que le site appelle ne refuse plus avec
une phrase anglaise. `machines.recordHeartbeat`, interne, garde une `Error` simple : elle
est lue par la route du heartbeat et répondue à la machine avec les codes du
contrat (§6). Les codes de l'API machine sont une autre liste, partagée avec
le Pi : `contracts/machine-api.json`.

ANH-139 ajoute deux codes à `training.launchAutoSession` :

| Code | Sens |
|---|---|
| `medical_parameters_unavailable` | Aucun paramètre médical courant n'est enregistré sur le serveur ; le lancement distant échoue fermé. |
| `medical_parameters_unsigned` | La machine a annoncé `medicalParametersVersion: null` ; le Pi est en mode banc et ne peut pas démarrer une séance auto. Ce contrôle passe avant `programs_disabled`. |

### Fonctions par fichier

Résumé des fonctions les plus utilisées par le site. Dans ces tableaux,
« admin » sans précision veut dire l'admin Anheart **ou** l'admin de
l'organisation concernée, et toute lecture ou liste est limitée à
l'organisation de l'appelant, sauf pour l'admin Anheart
([§3](#3-règles-dautorisation)).

### `audit.ts` (ANH-167)

| Fonction | Type et autorisation | Rôle |
|---|---|---|
| `list` | Query ; admin Anheart : toutes les organisations et les actions globales ; `org_admin` : son organisation seulement ; autres rôles refusés. | Journal par `at` décroissant, filtré et paginé par curseur Convex. Renvoie `page`, `isDone`, `continueCursor`. |

`paginationOpts` est obligatoire (`numItems`, `cursor`, selon le validateur
Convex). Les filtres facultatifs sont `organizationId`, `actorId`
(identifiant d'utilisateur ou de machine), `actorRole`, `action`,
`targetType`, `targetId`, `from` et `to` (ms Unix, début inclus, fin exclue).
L'organisation d'un `org_admin` est imposée par le serveur. Demander une
autre organisation, donner un `targetId` sans `targetType`, ou une période
avec `from >= to` renvoie une page vide.

[`convex/lib/auditIndex.ts`](../convex/lib/auditIndex.ts) choisit l'index
selon cette priorité : cible complète, acteur, rôle d'acteur, action, type
de cible, date ;
avec la variante d'organisation quand elle est fixée. La période porte sur
l'index et les autres filtres sont appliqués avant la pagination.

Chaque ligne renvoyée reprend les champs du journal et ajoute
`organizationName`, `actorLabel`, `targetLabel`, sans e-mail. Les libellés
viennent des comptes et machines actuels, pas d'une copie de leurs noms au
moment de l'action ; une cible autre qu'un utilisateur ou une machine, un
identifiant invalide ou une ligne supprimée donne un libellé `null`.
L'interface est décrite dans
[tableau-de-bord.md](tableau-de-bord.md#audit--frdashboardaudit).

### `convex/lib/audit.ts` (ANH-167)

La bibliothèque définit les actions, leurs cibles et `audit(ctx, entry)`.
Les 43 actions couvrent comptes et invitations, appartenances Clerk, liens
gestionnaires et droits de lancement, changement de physiologie sans les
valeurs, soumission et validation d'aptitude sans réponses ni verdict, consentements, lancement et
arrêt de séances, clés machine, création/suppression/restauration de
machines, registre et niveaux de validation, versions logicielles et
médicales, suppression d'enregistrements.

La liste de référence est `SENSITIVE_MUTATIONS` dans
[`convex/lib/audit.ts`](../convex/lib/audit.ts) : 42 mutations publiques ou
internes, avec leurs actions attendues. Les actions Convex et routes HTTP
font écrire leurs effets par ces mutations. `NON_SENSITIVE_MUTATIONS`
classe toutes les autres avec un motif : télémétrie et états transitoires,
synchronisation opérationnelle, calculs de résumés, dépôts de fichiers,
notifications, changements de nom/langue et les cinq migrations de
`convex/migrations/`. Les purges de
ces autres données ne purgent jamais `audit_log`.

`users.getOrCreateUser` est sensible : chaque écrivain d'une appartenance
trace son propre effet une seule fois. La connexion écrit
`membership.created` lorsqu'elle insère une appartenance active, et
`membership.role_changed` lorsqu'elle change le rôle d'un miroir actif ;
la source est `site`, l'acteur est le compte avec son rôle effectif dans
cette organisation. Le webhook écrit `membership.created` à l'insertion
active ou au passage inactif vers actif, y compris une réintégration ou
l'acceptation d'une invitation, sans ajouter en plus un changement de rôle.
Une ligne déjà active ne donne une ligne de rôle que si celui-ci change et
que l'appartenance reste active ; une suppression ne trace que la désactivation.
Une ligne qui reste inactive ne donne aucune création.
Connexion puis webhook, ou webhook puis connexion, produisent
une seule création au total. Une appartenance ouverte par la seule
connexion est tracée par la connexion. Le webhook trace aussi
`user.linked_to_account` et chaque `patient_gestionnaire.assigned` qu'il
crée, avec l'acteur système et la source `webhook`.
La connexion réutilise l'organisation déjà chargée pour attribuer son audit,
sans la relire ni permettre un saut silencieux de cette écriture.

`fitness.submitFitnessAnswers` écrit `fitness.submitted`, cible
`fitness_assessment`, avec seulement `userId` : une nouvelle saisie remplace
l'aptitude précédemment validée. `recordStorage.enforceRetention` écrit
`record.deleted` par séance dont l'enregistrement est supprimé, avec la
source `cron` et l'organisation de la séance. Cette rétention des fichiers
de séance est distincte du journal d'audit, qui n'a aucune purge.

Les appels de physiologie, de rôle et d'identité du registre sans changement
réel ne donnent aucune ligne. Pour la physiologie changée par l'admin Anheart,
seules les appartenances actives du patient déterminent les organisations ;
sans appartenance active, la ligne n'a pas d'organisation. Les relations
gestionnaires dormantes ne comptent pas. Pour la suppression de compte,
les organisations viennent des appartenances et relations supprimées.
Un appelant non admin attribue ces lignes à son organisation ; le miroir
principal `users.organizationId` ne suffit pas.

Pour ajouter une mutation sensible :

1. Déclarer son action fermée et sa cible si elles n'existent pas encore,
   puis ajouter la mutation et ses actions dans `SENSITIVE_MUTATIONS`.
2. Appeler `await audit(ctx, ...)` après l'effet, dans la même mutation,
   avec l'organisation et l'acteur autorisés, sans donnée de santé ni texte
   libre ; un refus ou un rejeu sans effet ne doit pas ajouter de ligne.
3. Ajouter dans `SCENARIOS` de
   [`convex/auditCompleteness.test.ts`](../convex/auditCompleteness.test.ts)
   un scénario passant par la fonction enregistrée et vérifiant l'effet
   et la ligne attendue. Le test compare exactement les clés des scénarios
   à celles de la liste et observe chaque action déclarée.

ANH-137 (programme publié), ANH-141 (configuration approuvée) et ANH-145
(incident clos) ajouteront leurs actions de cette façon ; elles ne sont pas
implémentées par ANH-167.

`audit()` lit une fois les métadonnées de requête puis insère une ligne,
sans autre lecture, contrôle métier ni chemin de refus ajouté. Son contrat
ne peut donc pas refuser une demande d'arrêt autorisée. Une défaillance de
base ou du runtime reste un échec de transaction, comme pour toute
insertion ; l'audit n'est pas une écriture asynchrone indépendante.

### `fitness.ts` et `convex/lib/fitness.ts` (ANH-143)

| Fonction | Type et autorisation | Rôle |
|---|---|---|
| `submitFitnessAnswers` (mutation) | Connecté ; soi-même, ou gestionnaire / administrateur du passager | Vérifie la version et toutes les réponses avec `CURRENT_QUESTIONNAIRE`, calcule l'outcome, puis ajoute une saisie non validée. Les erreurs sont `questionnaire_outdated` ou `fitness_answers_invalid`. |
| `validateFitnessAssessment` (mutation) | Gestionnaire du passager ou administrateur ; jamais le passager lui-même | Valide uniquement la dernière saisie courante. Un résultat `fit` exige une date de fin entière, future, au plus `4133980800000` ms Unix (`2101-01-01T00:00:00Z`) et dans la limite de `maxValidUntil` si elle existe ; un autre résultat n'en accepte pas. Un dépassement donne `invalid_validity`. Cette borne de représentation est commune au site, à Convex et à la console ; elle ne fixe aucune durée médicale. La validation ne change pas l'outcome. |
| `signConsent` (mutation) | Tout membre actif de l'organisation pour lui-même | N'accepte que la version courante. Une signature active identique est renvoyée sans nouvelle ligne ; sinon le serveur ajoute une signature `site`. Il n'est pas possible de signer pour un autre membre. |
| `withdrawConsent` (mutation) | Tout membre actif pour lui-même ; un admin ou gestionnaire peut aussi retirer celui d'un passager qu'il gère | Date tous les consentements non retirés du passager dans l'organisation concernée. Sans ligne active, refuse par `consent_not_active`. |
| `getRiderReadiness` (query) | Le passager, un gestionnaire ou un administrateur autorisé ; sinon `null` | Renvoie les versions courantes, l'état d'aptitude et de consentement, `serverNow`, `canValidate` et le code qui bloquerait un lancement. `consent.active` est réservé à la version courante ; `consent.latestSigned` donne `{textVersion, signedAt}` pour la dernière signature non retirée, toutes versions, ou `null`. `machineId` choisit l'organisation de la machine et une machine absente, supprimée ou hors périmètre renvoie `null`. |

Pour un appel de gestion sur un autre passager, l'admin Anheart agit dans
l'organisation principale du passager, qui doit encore le compter comme membre
actif. L'admin d'organisation, le gestionnaire et le passager restent dans
l'organisation active de leur appel. `getRiderReadiness` suit la même règle
sans `machineId`; avec une machine, il suit l'organisation de cette machine.

`riderLaunchRefusal` dans `convex/lib/fitness.ts` ne consulte que la dernière
saisie : aucune saisie, une version de questionnaire différente, un outcome
autre que `fit` ou une saisie non validée donnent `rider_not_fit`, dans cet
ordre. Ensuite, `validUntil <= now` donne `fitness_expired`, puis l'absence de
consentement non retiré pour la version courante donne `consent_missing`.
Sinon le résultat est `null`.

Ce contrôle est appliqué au lancement depuis le site et à l'armement : le poll,
la confirmation du départ et la tâche de chaque minute relisent les mêmes faits.
`riderFitness` rend aussi `fitnessQuestionnaireVersion`, la version de la saisie
validée qui rend `fit` vrai, sinon `null`. Le roster expose ces seuls faits selon
[`roster` du contrat machine 1.6](../contracts/machine-api.json), sans réponses
ni notes de validation.

Le champ date du site a pour maximum le 1er janvier 2101. Une date choisie
donne la fin de journée locale, bornée à `2101-01-01T00:00:00Z` pour respecter
la même limite technique, indépendamment du fuseau du navigateur.

Dans `getRiderReadiness`, `answers` et `notes` ne sortent que si l'appelant
gère le passager et n'est pas lui. Le passager voit l'outcome calculé, la
validité et l'état, mais jamais ses réponses ni les notes. Cette règle
s'applique aussi à l'admin qui consulte sa propre fiche. Aucun appel `console`
ne reçoit ces champs.

ANH-172 bloque encore la décision d'hébergement, la durée de conservation et
la purge ; aucune purge n'est codée ici. ANH-98 bloque le contenu définitif du
questionnaire et sa durée de validité. Le questionnaire actuel reste
provisoire et ce code ne fixe aucun critère médical.

### `medicalParameters.ts` et `medicalParametersStore.ts` (ANH-139)

| Fonction | Type et autorisation | Rôle |
|---|---|---|
| `medicalParameters.current` | query publique ; utilisateur accepté par `getCurrentUserOrThrow` | Renvoie `null` si le registre est vide, sinon les métadonnées publiques de la version courante : `version`, `issuedAt`, `signedBy`, `zoneCeilingFraction`, `minRiderAge` et `hrMaxFormula`. |
| `medicalParameters.registerVersion` | action interne ; opérateur du déploiement via la CLI | Reçoit le contenu exact du fichier (`content`) et sa signature détachée base64 (`signatureBase64`), vérifie Ed25519 avec la copie de `raspberry-pi/config/medical.pub`, analyse le document et ses bornes, stocke le fichier, puis appelle `medicalParametersStore.insertVersion`. Si l'insertion échoue, supprime le fichier stocké. Renvoie la version inscrite. |
| `medicalParametersStore.insertVersion` | mutation interne | Refuse une version en double ou qui n'est pas supérieure à la version courante. Dans la même transaction, échoue les lancements auto expirés ; si un lancement attend encore, rend `{ refusal }` sans lever pour valider ces échecs. L'action `registerVersion` lève alors le refus et supprime le fichier stocké. Sinon, ajoute les champs validés et `registeredAt` à `medical_parameters_versions`, puis renvoie la version. |

`convex/lib/medicalParameters.ts` contient les fonctions partagées :
`parseMedicalDocument` valide le JSON strict et les bornes dérivées de
`raspberry-pi/src/medical.py`, `raspberry-pi/src/local_config.py` et
`raspberry-pi/src/training/motion.py` ;
`verifyMedicalParametersSignature` vérifie la signature des octets UTF-8
exacts ; `compareMedicalVersions` ordonne les trois composantes numériques ;
`currentMedicalParameters` choisit la plus grande version ;
`medicalParametersStatus` calcule le statut décrit en §2. Les calculs purs
`estimateHrMax` et `zoneCeilingBpm` sont dans `lib/medicalParameters.ts`.

### `users.ts`

| Fonction | Autorisation | Rôle |
|---|---|---|
| `getOrCreateUser` (mutation) | connecté | Compatibilité historique ; aucun appel depuis l'accueil. Refuse de créer une seconde fiche lorsqu'une adresse correspond déjà à un compte ou à une invitation. Les nouveaux parcours passent par le webhook. |
| `getCurrentUser` (query) | - | Le compte courant ou `null`, avec `role` (rôle de l'appel : `admin`, `org_admin`, `gestionnaire`, `user`) et `organization` (`{_id, name, slug}`, ou `null` si le serveur n'accepte aucune organisation pour l'appel). Trois champs servent au site : `organizationAccess` (`granted` ou la raison du refus, voir ci-dessus), `organizationClaim` (l'`org_id` du jeton, accepté ou non, `null` sans revendication : le site le compare à l'organisation active de Clerk pour ne rien afficher tant qu'un changement d'organisation n'a pas atteint le serveur) et `rolesManagedInClerk` (vrai dès que `ANHEART_ORG_ID` est définie). |
| `updateUserRole` | admin Anheart, pendant la transition seulement | Change le rôle d'un membre de son organisation. Refuse dès que `ANHEART_ORG_ID` est définie. |
| `updateUserProfile` | connecté | Prénom, nom, langue de soi-même. |
| `listUsers` | connecté | admin Anheart : tous (ou ceux d'un gestionnaire) ; admin d'organisation : les membres actifs de son organisation ; gestionnaire : ses patients dans son organisation ; user : lui-même. Filtre `role` optionnel. `role` est le rôle tenu dans l'organisation de l'appelant. |
| `createPatient` (action) | admin, admin d'organisation, gestionnaire | Prépare puis envoie une invitation Clerk dans l'organisation de l'appelant. Retourne l'identifiant d'invitation, l'adresse et `status: sent`. Lie les gestionnaires autorisés après acceptation. Un membre déjà actif de cette organisation est refusé sans divulgation de nom. |
| `updatePatient` | admin, gestionnaire du patient | Modifie un patient. |
| `getUserById` | `canAccessUser` | Profil, y compris `hrMax`, `birthYear` et `effectiveHrMax`. |
| `deleteUser` | admin ; gestionnaire pour ses patients seulement | Retrait de l'organisation de l'appelant, ou suppression partout pour l'admin Anheart (voir [§3](#3-règles-dautorisation)). Pas soi-même. |
| `linkPatientToClerk` | connecté, e-mail vérifié | Lie un patient pré-créé (`clerkId` vide) au compte Clerk, uniquement si l'e-mail **vérifié** de l'appelant est celui du dossier (voir [§3](#3-règles-dautorisation)). **Aucune page ne l'appelle aujourd'hui.** |
| `assignGestionnaireToUser` / `removeGestionnaireFromUser` | admin ; un gestionnaire pour lui-même | Lien patient ↔ gestionnaire, dans l'organisation de l'appelant ; le patient et le gestionnaire doivent tous deux en être membres actifs, avec ces rôles. Pour l'admin Anheart, le lien se crée dans l'organisation principale du patient. |
| `listGestionnaires` | admin | Les gestionnaires (tous pour l'admin Anheart, ceux de son organisation pour un admin d'organisation), avec leurs nombres de machines (machines supprimées non comptées) et de patients. `[]` pour tout autre rôle. Option `organizationId` : les gestionnaires de cette organisation seulement, liens comptés dans celle-ci (le formulaire de machine la passe) ; `[]` pour un admin d'organisation qui en nomme une autre que la sienne. |
| `assignPatientsToGestionnaire` | admin (`requireGestionnaireAdmin`) | Fixe la liste exacte des patients d'**un gestionnaire**, **dans une organisation** (celle de l'appelant ; pour l'admin Anheart, l'organisation principale du gestionnaire) : compare la liste demandée à ses lignes `user_gestionnaires` de cette organisation, insère les liens manquants et supprime ceux qui ne sont plus demandés. Un lien déjà présent n'est pas réécrit (il garde son auteur et sa date), et la même liste envoyée deux fois n'écrit rien. Seules les lignes de ce gestionnaire dans cette organisation sont lues et écrites : ses patients d'une autre organisation ne bougent pas. Seul un patient actif de cette organisation est lié ; tout autre identifiant (compte inconnu, patient d'une autre organisation, compte qui n'est pas un patient) est laissé de côté, avec la même réponse dans les trois cas. Un identifiant répété ne crée qu'un lien. Retourne `{added, removed}` : ce qui a été écrit. Une mutation Convex est une transaction sérialisable : deux appels concurrents sur le même gestionnaire s'appliquent l'un après l'autre, et la liste du dernier s'applique en entier. |
| `getPatientsForGestionnaire`, `getGestionnairesForPatient` | admin / gestionnaire concerné | Lectures. |

### `organizations.ts`

| Fonction | Autorisation | Rôle |
|---|---|---|
| `listOrganizations` (query) | admin Anheart | Les organisations connues du serveur et non supprimées dans Clerk, triées par nom : celles où l'admin Anheart peut créer une machine. `[]` pour tout autre rôle. |

### `dashboard.ts` (compteurs du tableau de bord, ANH-159)

| Fonction | Autorisation | Rôle |
|---|---|---|
| `counters` (query) | connecté | Les chiffres des cartes de la première page du site. Chaque chiffre compte ce que l'appelant peut déjà lister ailleurs, rien de plus. `members` : les membres **actifs** par rôle (`admin`, `gestionnaire`, `user`), pour un admin d'organisation dans la sienne, pour l'admin Anheart dans toutes (un compte membre de deux organisations compte dans chacune) ; `null` pour un gestionnaire ou un patient. `patients` : pour un gestionnaire, ses patients encore patients actifs de son organisation, comme la page Patients les liste ; `null` sinon. `machines` : `total`, les machines non supprimées que l'appelant liste (`listMachines`), et `signals`, le statut (`online` ou `in_session`) et `lastHeartbeat` de chacune de celles que leur fiche dit connectées ; `null` pour un patient. `sessions` : `active` et `pending`, les séances que l'appelant peut ouvrir (`canAccessSession` : toutes pour l'admin Anheart, celles de l'organisation pour son admin, celles de ses machines ou qu'il pratique pour un gestionnaire, les siennes pour un patient). `serverNow` : l'heure du serveur dans la réponse. |

**Machines en ligne.** Le serveur ne décide pas qu'une machine est en ligne :
sa fiche garde `online` ou `in_session` jusqu'au passage de la tâche
`checkOfflineMachines` (jusqu'à 2,5 min après le dernier signal), et une query
qui ne lit que des données inchangées ne se relance pas quand le temps passe.
`counters` renvoie donc le dernier signal de chaque machine que sa fiche dit
connectée, avec `serverNow`, et le site compte celles dont le signal a moins
de 90 s, revu chaque seconde sur l'horloge du serveur, comme il affiche leur
badge ([tableau-de-bord.md §6](tableau-de-bord.md#fraîcheur-recalculée-à-lhorloge)).
Une machine dont la fiche dit `offline` n'est jamais comptée.

**Coût.** Chaque chiffre est lu par un index, jamais par le parcours d'une
table entière : `memberships.by_active_and_organization`,
`sessions.by_status_and_organization`, `machines.by_organization` (admin
d'organisation) ou `machines.by_is_deleted` (admin Anheart, machines jamais
supprimées ou restaurées), `machine_gestionnaires.by_gestionnaire` et
`user_gestionnaires.by_gestionnaire` (gestionnaire). Un appel lit les lignes
qu'il compte (tous les membres actifs pour l'admin Anheart, ceux d'une
organisation pour son admin, les machines et les séances actives ou en attente
de son périmètre), plus quelques-unes qu'il écarte : les machines supprimées
de l'organisation pour son admin, les séances ouvertes de toute
l'organisation pour un gestionnaire ou un patient. La query se relance
à chaque changement d'une ligne lue, donc à chaque heartbeat d'une machine
comptée (toutes les 10 s par machine). Pour quelques machines et quelques
dizaines de comptes, c'est négligeable ; rien n'a été mesuré sur un
déploiement. Le composant d'agrégation de Convex
(`@convex-dev/aggregate`) tiendrait les comptes sans lire les lignes, mais il
se monte par un `convex.config.ts` que `scripts/ci/convex-generated-api.mjs`
refuse aujourd'hui ([§8](#convex_generatedapidts--tenu-par-un-script)) : il
n'est pas utilisé, et le seuil de volume où il deviendrait nécessaire n'a pas
été mesuré.

### `machines.ts`

| Fonction | Autorisation | Rôle |
|---|---|---|
| `createMachine` | admin Anheart | Crée la machine, statut `offline`, dans l'organisation `organizationId` (par défaut celle de l'admin) ; refusée pour une organisation inconnue ou supprimée dans Clerk. Retourne `{machineId, apiKey}` : la clé `anh1.<sélecteur>.<secret>` **n'est visible qu'à ce moment**. Elle est la première ligne de la machine dans `machine_api_keys`, sans expiration. |
| `getMachine`, `listMachines` | admin ; gestionnaire (ses machines) | Lecture. `listMachines` renvoie `[]` à un `user`. Option `includeDeleted` pour l'admin. `getMachine` renvoie aussi `organizationId`, `softwareVersion`, `contractVersion`, `lastVersionSeenAt`, `medicalParametersVersion`, `medicalParametersStatus` et `medicalParametersReferenceVersion` (voir §2, `machines`). Les deux renvoient `serverNow`, l'heure du serveur dans la réponse, sur laquelle le site vieillit `lastHeartbeat` ([tableau-de-bord.md §6](tableau-de-bord.md#fraîcheur-recalculée-à-lhorloge)). |
| `updateMachine` | admin, gestionnaire de la machine | Nom, lieu. |
| `deleteMachine` | admin, gestionnaire de la machine | Suppression douce. Refusée s'il y a une séance `active` ou `pending`. |
| `restoreMachine` | admin Anheart | Annule la suppression. |
| `assignMachineToGestionnaires` | admin | Fixe la liste complète des gestionnaires d'**une machine**, pris dans l'organisation de la machine ; le premier gardé est propriétaire. N'écrit que la différence : un gestionnaire qui reste garde sa ligne (auteur, date ; son `isOwner` suit la nouvelle première place), un identifiant répété ne fait qu'une ligne, et une seconde ligne laissée pour un même gestionnaire est supprimée (ANH-194). `createMachine` applique la même règle à la création. |
| `setGestionnaireMachines` | admin (`requireGestionnaireAdmin`) | Fixe la liste exacte des machines d'**un gestionnaire**, **dans une organisation** (celle de l'appelant ; pour l'admin Anheart, l'organisation principale du gestionnaire) : compare la liste demandée à ses lignes `machine_gestionnaires` de cette organisation, insère les liens manquants (`isOwner: false`, dans cette organisation) et supprime ceux qui ne sont plus demandés. Seules les lignes de ce gestionnaire dans cette organisation sont lues et écrites : les liens des autres gestionnaires ne bougent pas, ce que le gestionnaire gère dans une autre organisation non plus, et un lien déjà présent n'est pas modifié. Une machine inconnue, supprimée, ou d'une autre organisation (même réponse, `machine_not_found`), fait refuser l'appel sans rien écrire. Le lien vers une machine supprimée reste hors de la décision : il n'est jamais retiré, nommé ou non, et ne compte pas dans `{added, removed}` (ANH-194). Retourne `{added, removed}`. |
| `assignGestionnaireToMachine` / `removeGestionnaireFromMachine` | admin ; gestionnaire de la machine | Lien machine ↔ gestionnaire. Le gestionnaire doit être membre actif de l'organisation de la machine. |
| `getRecentHeartbeats`, `getGestionnairesForMachine`, `getMachinesForGestionnaire` | accès à la machine / admin | Lectures. `getMachinesForGestionnaire` renvoie aussi `serverNow` et laisse de côté les machines supprimées. `getMachine` liste le propriétaire en tête de ses gestionnaires. |
| `recordHeartbeat` | mutation interne, appelée par la route HTTP | Enregistre la version médicale bien formée annoncée par le Pi dans `machines.medicalParametersVersion` ; garde `null` comme annonce explicite de paramètres non signés. Les réponses calculent ensuite son statut face à la version courante. |

`regenerateApiKey` (« Régénérer la clé », ouverte aussi au gestionnaire de la
machine) est retirée par ANH-165 : les clés se créent et se révoquent par
`machineKeys.ts`, réservé aux admins.

### `machineKeys.ts` (clés des machines, ANH-165)

Les clés sont aux admins : l'admin Anheart pour toute machine, l'admin d'une
organisation pour les machines de son organisation. Un gestionnaire, même de
la machine, n'y a pas accès. Chaque écriture prend un jeton de la limite du
compte (`limitUser`, ANH-166) avant tout le reste.

| Fonction | Autorisation | Rôle |
|---|---|---|
| `listMachineKeys` | admin Anheart ; admin de l'organisation de la machine | Les clés de la machine, la plus récente en tête, 50 au plus : `prefix`, `state`, `createdAt`, `createdByName` (`null` sans auteur), `lastUsedAt`, `expiresAt`, `revokedAt` (`null` quand absents). `state` vaut `active`, `replaced` (remplacée par une rotation, encore valide jusqu'à `expiresAt`), `expired` ou `revoked`, calculé à l'heure du serveur quand la query tourne : une fiche ouverte voit une clé passer `revoked` quand la tâche l'a révoquée (dans les 5 min après son expiration), ou à la lecture suivante. Jamais la clé, son sélecteur ni son vérificateur. `[]` pour tout autre rôle, pour une machine inconnue ou d'une autre organisation ; les clés d'une machine supprimée ne sont listées qu'à l'admin Anheart. |
| `createMachineKey` | idem | `{machineId, overlapMinutes?, lifetimeMinutes?}` → `{keyId, apiKey, prefix}`. La clé en clair `apiKey` n'est visible **qu'à ce moment**. Chaque clé encore valide de la machine le reste pendant le chevauchement `overlapMinutes` (24 h par défaut ; nombre entier de minutes de 0 à 10 080, soit 7 jours), puis elle n'ouvre plus rien et la tâche la révoque ; avec 0, elles sont révoquées aussitôt. Une clé qui expirait avant la fin du chevauchement garde son expiration ; une clé déjà expirée est laissée à la tâche. `lifetimeMinutes` (nombre entier de minutes, d'une heure à dix ans) fait expirer la nouvelle clé autant de minutes après sa création, comptées sur l'horloge du serveur : le site envoie une durée, jamais une date ; absent, elle n'expire pas. Une clé encore dans `machines.apiKey` est d'abord déplacée dans la table, puis retirée comme les autres. Refus : `not_authorized` (gestionnaire, patient), `machine_not_found` (machine inconnue, supprimée ou d'une autre organisation : même réponse), `invalid_key_overlap`, `invalid_key_expiry` ; rien n'est écrit. |
| `revokeMachineKey` | idem | `{keyId}` : la clé n'ouvre plus rien dès la requête suivante (`revokedAt`, `revokedBy`). Une clé déjà révoquée reste telle quelle. La clé d'une machine supprimée peut être révoquée. Refus : `not_authorized`, `machine_key_not_found` (clé inconnue, ou d'une machine d'une autre organisation ou disparue : même réponse). |

Révoquer la clé qui en a remplacé une autre ne prolonge pas celle-ci : elle
reste `replaced` et expire à la fin de son chevauchement. Pour garder la
machine reliée, créer une nouvelle clé.

Fonctions internes :

- `recordKeyUse` : écrit `lastUsedAt` si la dernière écriture a une minute ou
  plus ; appelée par les routes machine après l'authentification (§6).
- `revokeExpiredKeys` : la tâche `revoke-expired-machine-keys` (§7).
- `createKeyByOperator`, `revokeKeyByOperator` : `createMachineKey` et
  `revokeMachineKey` sans compte, pour qui opère le déploiement avec sa clé de
  déploiement (mise en service, essai de la rotation, tableau de bord hors
  d'atteinte). La clé créée n'a pas d'auteur ; elle est imprimée une fois, dans
  le terminal de la commande ([secrets.md, 3.1](secrets.md#31-clé-de-la-machine)).

### `sessions.ts` (lectures seulement)

Aucune fonction de ce module ne crée, ne démarre ni ne termine une séance. Une
séance naît de `training.launchAutoSession` (lancement depuis le site) ou de
`registerLocalSession` (séance démarrée à la machine), et se termine par les
routes d'entraînement.

| Fonction | Autorisation | Rôle |
|---|---|---|
| `getSession` | pratiquant ou accès machine | Détail avec patient et machine. |
| `listSessions` | connecté | Les `limit` (50 par défaut) séances les plus récentes que l'appelant peut lire, avec les filtres facultatifs `status` (`pending`, `active`, `interrupted`, `completed` ou `failed`), `machineId`, `userId`. Retourne `kind` et `origin`. Pour les listes courtes du site (accueil, fiche patient, fiche utilisateur). |
| `listSessionsPaginated` | connecté | Les mêmes séances, avec les mêmes filtres, **page par page** (`paginationOpts`, pour `usePaginatedQuery`). Page Sessions (un statut par onglet) et page Rapports (`status: "completed"`). |
| `getActiveSessionForMachine` | accès machine | Séance `active` (une séance `interrupted` n'en est pas une). |
| `getCompletedSessionsForUser` | connecté | Les 100 séances `completed` les plus récentes que l'appelant peut lire. Plus appelée par le site de cette version (la page Rapports lit `listSessionsPaginated`) ; reste jusqu'au déploiement du site (ANH-82), car le site de la version précédente l'appelle encore entre le déploiement de Convex et celui du site. |

**Les droits avant la limite (ANH-158, ANH-227).** Les trois listes appliquent
les mêmes droits : qui peut lire quelles séances fait partie des plages et
filtres lus avant de couper la limite ou la page. Une page de 50 contient donc
en général 50 séances que l'appelant peut lire, jamais les 50 dernières de la
base filtrées après coup. Elle peut en contenir moins, et même aucune sans
que la liste soit finie : à la fin de la liste, quand le serveur demande de
couper une page qui lit trop, quand des séances arrivent ou changent pendant
la lecture, ou quand le second contrôle décrit plus bas en retire une. Le
site propose la suite d'après le statut de la liste, pas d'après la taille
d'une page, et n'écrit « aucune séance » qu'une fois la liste finie. Avant ce
ticket, une séance d'un patient ou d'un gestionnaire pouvait manquer à sa
liste parce que des séances plus récentes d'autres personnes remplissaient
la page.

| Appelant | Ce qu'il lit | Index lu |
|---|---|---|
| admin Anheart | toutes les séances, de toutes les organisations | `by_started` (ou `by_status_and_started` avec un statut) |
| admin d'organisation | les séances de son organisation | `by_organization_and_started` (ou `by_organization_and_status_and_started`) |
| gestionnaire | dans son organisation, celles des machines qu'il gère (`machine_gestionnaires`, machines de cette organisation) et celles dont il est le pratiquant | listes courtes sans filtre machine/pratiquant : fusion des plages par machine et pratiquant ci-dessous ; page : même index que l'admin d'organisation, avec ce filtre dans la requête |
| user | dans son organisation, celles dont il est le pratiquant | `by_user_and_started` |

Avec `machineId`, la requête lit `by_machine_and_started` ; avec `userId`,
`by_user_and_started` ; les droits ci-dessus s'appliquent de la même façon.
L'ordre est toujours le début de la séance (`startedAt`) du plus récent au plus
ancien, puis la date d'écriture pour deux séances commencées au même instant.
Chaque séance de la page est encore passée par `canAccessSession`, la règle de
toutes les autres lectures d'une séance ([§3](#les-règles)) : le filtre de la
requête est cette même règle, donc ce contrôle n'enlève rien tant que les deux
s'accordent ; s'ils divergeaient, une séance serait omise plutôt que montrée.
Une séance sans organisation (antérieure à la migration) n'est lue que par
l'admin Anheart. Une séance dont la machine n'existe plus n'est lue que par
l'admin Anheart et par son pratiquant : `canAccessSession` donne la séance à
son pratiquant avant de regarder la machine. Aucune mutation ne produit cet
état (une machine n'est supprimée qu'en douceur), et `getSession` échoue sur
une telle séance pour tout le monde, comme avant ce ticket.

**Les listes courtes du gestionnaire (ANH-227).** Sans `machineId` ni `userId`,
`listSessions` et `getCompletedSessionsForUser` fusionnent une source par
machine gérée dans son organisation, plus une source pour les séances dont
il est le pratiquant. Chaque source lit au plus `limit` séances (50 par défaut
pour `listSessions`, 100 pour l'ancienne liste Rapports), du plus récent au
plus ancien. L'organisation et, si demandé, le statut sont dans l'index :

| Source | Sans statut | Avec statut |
|---|---|---|
| une machine gérée | `by_organization_and_machine_and_started` | `by_organization_and_machine_and_status_and_started` |
| le gestionnaire pratiquant | `by_organization_and_user_and_started` | `by_organization_and_user_and_status_and_started` |

L'union retire les doublons par identifiant (une séance sur sa machine dont
il est aussi le pratiquant), trie par `startedAt` décroissant puis date
d'écriture décroissante, et conserve les `limit` premières. La borne de
lecture des séances est **(nombre de machines gérées + 1) × limit**, à laquelle
s'ajoutent les liens `machine_gestionnaires`, les machines, puis le second
contrôle d'accès et les noms des lignes retournées. Les séances sans
organisation ou d'une autre organisation sur une machine gérée sont exclues
par la plage d'index et ne sont pas parcourues. Le résultat et les droits
restent les mêmes. Dans le pire cas, un gestionnaire sans machine de son
centre et sans séance pratiquée reçoit donc une liste vide sans parcourir
l'historique du centre, même s'il est lié à une machine d'une autre organisation.

**Les pages (ANH-227).** `listSessionsPaginated` conserve une seule plage et la
pagination native Convex, avec ses pages contiguës et ses curseurs. Pour tous
les appelants, le serveur impose `maximumRowsRead: 1000` au plus ; une valeur
client entière d'au moins 1 et inférieure au plafond est conservée (une décimale
est tronquée) ; une valeur absente, inférieure à 1, non finie ou supérieure donne 1000.
Ce plafond compte les séances parcourues avant le filtre des droits, pas
seulement celles affichées. À la limite, Convex retourne une page incomplète
(`SplitRequired`), un `splitCursor` et un `continueCursor` au point d'arrêt.
`usePaginatedQuery` coupe cette page et la suite reste accessible par
« Afficher plus ». Une page vide peut donc précéder la fin de la liste :
le site doit suivre le statut de pagination pour proposer la suite et afficher
l'état vide. Le gestionnaire sans séance visible parcourt encore l'historique
du centre au fil des pages, avec au plus 1000 séances parcourues par page,
plus les lectures d'autorisation et de noms.

**Les filtres explicites.** Avec `machineId` ou `userId`, les listes courtes
conservent leur requête actuelle sur une machine ou un pratiquant, y compris
pour le gestionnaire. Un filtre sur une machine ou un pratiquant que
l'appelant ne voit pas peut parcourir tout son historique sans résultat :
ce coût reste borné par l'historique de cette machine ou de ce pratiquant,
et non celui du centre. Le plafond des pages s'applique aussi à ces filtres ;
les listes courtes ainsi filtrées n'ont pas ce plafond. Aucun droit n'est modifié.

### `sessionReports.ts` (résumés de séance et historique, ANH-89)

Le résumé est une donnée de santé : il n'est lu que par ces deux fonctions,
sous les droits de sa séance, et rien de lui n'est écrit dans les journaux.

| Fonction | Autorisation | Rôle |
|---|---|---|
| `getSummary` | `canAccessSession` : pratiquant, accès à la machine dans l'organisation de la séance, admin Anheart | Le résumé de la séance (`provisional`, `computedAt`, `recordProblem`, `figures`), ou `null` : séance inconnue, non lisible par l'appelant, ou sans résumé encore. |
| `riderHistory` | connecté | Les séances terminées (`completed`, `failed`) d'entraînement d'un pratiquant que l'appelant peut lire, les plus récentes d'abord, 100 au plus, avec les chiffres clés de leur résumé (durée, temps en zone, zone, FC moyenne et max, g max de référence) ou `null` s'il n'est pas encore calculé. Ce sont les séances de `listSessions` pour ce pratiquant (`readableSessions` par statut, puis `canAccessSession` sur chacune) : un gestionnaire lit celles des machines qu'il gère, un pratiquant les siennes, une autre organisation rien. |

Fonctions internes : `summarizeTelemetry` et `summarizeRecord` (actions),
`telemetryInput`, `telemetryPage`, `verdictPage`, `recordInput` (queries),
`storeSummary` et `noteRecordProblem` (mutations). La table et les définitions
sont décrites [au §2](#session_summaries_v2--résumé-dune-séance-dentraînement-anh-89).

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
| `recordRelease` (mutation) | admin Anheart | Enregistre une version publiée dans `software_releases`, ou corrige la ligne d'une version déjà connue (une seule ligne par composant et version). Le niveau d'une version du Pi est écrit aussi dans `software_release_levels` : à l'enregistrement, puis à chaque changement de niveau, avec son motif, son auteur et sa date ; une correction qui laisse le niveau en place n'y ajoute rien. Refuse une version qui n'est pas `<composant>-X.Y.Z`, une version du Pi sans niveau de validation, un niveau sur une version `cloud` ou `web`, une date invalide, des notes de plus de 2000 caractères, et un changement de niveau sans `notes`. Erreurs en `ConvexError(message)`. |
| `listReleases` (query) | admin Anheart | Les versions enregistrées, la plus récente d'abord ; filtre `component` optionnel. Chaque version porte `levelHistory` : ses niveaux successifs, du plus ancien au plus récent (`validationLevel`, `reason`, `decidedBy`, `decidedAt`) ; vide pour une version `cloud` ou `web`. |

`deployedCloudVersion` est une query **interne** : elle répond la constante
`CLOUD_VERSION` de `convex/cloudVersion.ts`, donc la version du code déployé
(`cloud-0.0.0-dev` tant qu'aucune release n'a été faite). On la lit avec
`npx convex run softwareReleases:deployedCloudVersion`. `scripts/release.sh`
écrit cette constante en même temps que `convex/VERSION`, et
`convex/cloudVersion.test.ts` échoue si les deux diffèrent.

### `machineRegistry.ts` : fiche et règles du registre (ANH-147)

| Fonction | Droits et effet |
|---|---|
| `getRegistry` (query) | Admin Anheart : toute machine, même supprimée. `org_admin` : machine non supprimée de sa propre organisation. Autres cas : `null` ; anonyme : `not_authenticated`. Retourne les champs du registre, contacts résolus en `{userId, firstName, lastName}`, auteurs résolus, URL de la fiche, de son historique et des comptes rendus, `effectiveLevel` et `canEdit` (admin et machine non supprimée). |
| `listRegistryBadges` (query) | Machines non supprimées de toutes les organisations pour l'admin Anheart, de sa propre organisation pour `org_admin`, `[]` pour les autres rôles ; anonyme refusé. Par machine : niveau effectif, statut de version et statut de configuration. Aucun champ `alerts` ni lecture des séances : les alertes restent sur la fiche. |
| `listContactCandidates({machineId})` (query) | Admin Anheart uniquement. Membres actifs de l'organisation de la machine, hors comptes supprimés dans Clerk, sous la forme `{userId, firstName, lastName, role}` (rôle dans cette organisation, sans e-mail). Tri par nom, prénom puis identifiant. La même règle contrôle les contacts proposés, lus et enregistrés. Machine absente ou supprimée : `machine_not_found` ; sans organisation : liste vide. |
| `checkConfigurationLock` (query) | Même accès que la fiche ; verdict `{allowed, requiredLevel, reachedLevel, message}` pour une clé et sa valeur booléenne. Point d'entrée prévu pour ANH-141. |
| `generateUploadUrl` (mutation) | Admin Anheart uniquement ; URL de dépôt Convex Storage. |
| `updateIdentity` (mutation) | Admin Anheart uniquement ; champ absent inchangé, `null` efface ; contrôle des contacts et de la version attendue. |
| `recordRadii` (mutation) | Admin Anheart uniquement ; enregistre ensemble les deux rayons, la date et le nom du mesureur. |
| `attachDriveParameterSheet` (mutation) | Admin Anheart uniquement ; vérifie la pièce jointe et conserve l'ancienne fiche dans l'historique. |
| `addValidationReview` (mutation) | Admin Anheart uniquement ; compte rendu joint et versions enregistrées, montée d'un seul niveau. |
| `lowerValidationLevel` (mutation) | Admin Anheart uniquement ; niveau strictement inférieur avec motif conservé. |

Toutes les mutations visant une machine refusent une machine absente ou
supprimée (`machine_not_found`). Les refus métier sont des codes traduits
`errors.<code>` ; les autres rôles ne peuvent écrire (`not_authorized`).

`configurationLocks` expose le verdict pour activer `PROGRAMS_ENABLED`
(niveau requis `auto`) et `OCCUPANCY_OCCUPIED_ENABLED` (niveau requis
`occupied`). Désactiver une clé est autorisé. Un niveau stocké incohérent
avec son historique ou inconnu est lu comme `none` : activer est refusé,
désactiver reste permis. Seule la fonction pure `configurationLockVerdict`,
appelée directement avec un niveau inconnu, refuse toute valeur.
Le message de refus est exactement
`machine validée jusqu'au niveau auto` pour une machine au niveau `auto`.
`requireConfigurationAllowed` dans `convex/lib/machineRegistry.ts` fournit
le garde serveur, avec le refus `machine_validation_insufficient`.
L'écriture de configuration d'ANH-141 doit afficher le `message` renvoyé par
`checkConfigurationLock` (« machine validée jusqu'au niveau X ») ; le garde
conserve son code d'erreur structuré.

Le niveau effectif vaut `validation.level` uniquement s'il égale
`levelAt(validation, maintenant)`, sinon `none` (fermeture sur incohérence).
Tous les verdicts, statuts de version, seuils d'alerte, badges et règles de
montée ou descente utilisent ce niveau effectif. `getRegistry` le renvoie
à côté du niveau stocké ; la fiche avertit si les deux diffèrent.
Un registre incohérent peut rester bloqué lorsque son historique n'est pas
vide, par exemple avec `auto` stocké et une seule revue `bench` : les mutations
publiques refusent l'ajout d'une revue (`validation_level_not_next`) et la
descente de niveau (`validation_level_not_lower`). Dans ce cas, seule une
intervention en base par Anheart permet la réparation ; ce point reste à
suivre.
Les auteurs `by`, `attachedBy` et `updatedBy` sont résolus à la lecture en
`{userId, firstName, lastName}`, sans e-mail. Pour un compte supprimé ou
absent, seul `{userId}` est renvoyé ; les identifiants stockés sont conservés.

`configurationProposal` propose `ARM_RADIUS_M` et `LEG_TIP_RADIUS_M` depuis
les mesures du registre, ou `null` si elles manquent. L'écriture de la
configuration centralisée et sa consommation des rayons restent bloquées
par ANH-141. Le badge `configurationStatus` vaut donc `unavailable`, pas
« appliquée » ou « en attente ». Le refus OTA reste bloqué par ANH-116.
Il n'y a aucun verrou supplémentaire au lancement distant : décision du
chef de projet, suivie par ANH-102 et ANH-107.

`versionStatus` vaut d'abord `unknown` sans version annoncée, puis
`not_validated` si la version annoncée ne satisfait pas
`releaseAllowedOnMachine`, même sans version attendue. Si la version annoncée
est autorisée, le statut vaut `unknown` sans version attendue, `up_to_date`
si elle égale l'attendue, sinon `outdated`. Une version absente des releases
n'a aucun niveau enregistré.
`expectedVersionAllowed` applique la même règle à l'attendue et vaut `null`
si aucune n'est définie. `medicalKeyIsTest` expose le blocage médical.

Deux alertes sont calculées à la lecture. `programs_enabled_without_auto`
signale `machines.programsEnabled === true` sous le niveau `auto`.
`person_on_board_without_occupied` compte, parmi les 50 séances les plus
récentes que le lecteur peut lire sur cette machine, celles avec une personne
à bord alors que le niveau
historique au début était inférieur à `occupied`, et donne la date de la
plus récente. L'admin Anheart lit toutes les séances ; `org_admin` ne compte
que celles dont `organizationId` égale son organisation, jamais celles sans
organisation. Ce filtre Convex précède `.take(50)` pour que les séances
illisibles ne masquent pas les séances lisibles.
Les séances `pending` sont exclues, ainsi que les séances
`failed` avec un `endCode` de `ARMING_REFUSAL_CODES` (refus à l'armement,
expiration comprise), ou sans `endCode` et dont `endReason` commence par
`Cancelled before start by` (annulation par `requestStop` avant départ) ou
`refusee par la machine : ` (refus du Pi avant départ, terminé par
`/training/end`, préfixe écrit par `raspberry-pi/src/cloud_sync.py`).
Seules les fins avant départ connues sont exclues ; toute autre fin compte,
l'alerte penchant vers le signalement (fail closed). Une séance armée reste
comptée même si elle échoue ensuite.
Une séance `auto` a toujours une personne à bord ; pour les
autres, un segment des notes séparé par `; ` doit valoir exactement
`Occupancy: occupied`. Les revues et descentes permettent de retrouver le
niveau à cet instant si le niveau courant est cohérent avec son historique ;
sinon toutes les séances sont évaluées au niveau `none`. À date égale, l'ordre
est celui des transitions valides
en conservant l'ordre de chaque historique ; un historique incohérent est lu
comme `none`. Une descente ultérieure ne dénonce pas une séance
qui avait commencé au niveau `occupied`.

### `notifications.ts` (alertes aux gestionnaires, ANH-90)

**Qui les écrit.** Le serveur, là où il voit l'événement, jamais un client.
La fonction qui voit l'événement ne fait que **planifier** l'alerte
(`scheduleAlert`, `convex/lib/notifications.ts`) ; une mutation à part,
`notifications.deliver` (interne), l'écrit aussitôt (`notifyMachine`). Rien
de ce qui se passe en l'écrivant, ou en mettant son e-mail en file, ne peut
faire échouer ni annuler le heartbeat, le lot d'événements, la fin, le
départ, la tâche planifiée ou le lancement qui l'a demandée. Ne passent que
des identifiants et un code de la machine.

| Type | Demandée par | Quand |
|---|---|---|
| `machine_offline` | `machines.checkOfflineMachines` (tâche de §7) | La machine passe `offline` : aucun heartbeat depuis 90 s (`LIVE_FRESH_MS`). |
| `drive_fault` | `training.updateLive` (route du heartbeat) | L'état en direct du heartbeat dit le variateur en défaut (`driveState: "fault"`) et le précédent ne le disait pas. En séance ou non. La séance nommée par l'état est jointe si c'est une séance de cette machine. |
| `drive_fault` | `training.storeEvents` (route des événements) | Un événement `drive_fault` de l'enregistrement d'une séance, autre que `cleared` ; son mnémonique est gardé (`OHF (LFT 17): …` donne `OHF`). |
| `session_failed` | `training.endTrainingSession` (route de fin) | La machine termine une séance comme échouée (refus de départ par la machine compris). Une seule fois : une fin renvoyée ne change rien. |
| `session_failed` | `failBeforeStart` (`training.ts`, ANH-144) | Un lancement refusé avant que sa machine l'arme : par la tâche `sweepPendingSessions` (expiré, droit retiré, physiologie changée…), par la route de départ (`markTrainingStarted`, 409), ou expiré par un nouveau lancement. La liste rend son `endCode` (`reasonCode`), que la page traduit. Une confirmation répétée n'alerte pas de nouveau. |
| `safety_verdict` | `training.storeEvents` | Un événement `verdict` dont l'action termine la séance : `RAMP_DOWN`, `QUICK_STOP`, `GO_SILENT`, ou une action que ce serveur ne sait pas lire (côté sûr). `FREEZE` et `REDUCE`, qui régulent sans arrêter, et `cleared` ne donnent rien. L'action la plus sévère est gardée. |

Ce que la machine écrit est lu dans le format du Pi
(`raspberry-pi/src/record/session.py` : « règle (ACTION): détail » pour un
verdict, « MNÉMONIQUE (LFT code): message » pour un défaut) ; seul le code
est gardé, et seulement s'il est un jeton en majuscules. Un événement
renvoyé (déjà stocké) n'alerte pas de nouveau. Une annulation par
`requestStop` n'est pas une alerte.

**Qui les reçoit.** Les admins actifs de l'organisation de la machine et les
gestionnaires liés à la machine (`machine_gestionnaires`) qui sont encore
gestionnaires actifs de cette organisation ; pas un compte supprimé dans
Clerk, pas un patient, pas l'équipe Anheart en tant que telle (ses alertes
de flotte sont celles d'ANH-150). Rien pour une machine supprimée, sans
organisation, ou dont l'organisation est supprimée.

**Regroupement.** Un nouvel événement se fond dans l'alerte **non lue** du
même destinataire et du même type (`count` + 1, `updatedAt`) au lieu d'en
écrire une autre : pour la même machine (`machine_offline`, `drive_fault` :
un état de la machine), ou pour la même séance (`session_failed`,
`safety_verdict`). Une alerte lue n'absorbe plus rien : l'événement suivant
en écrit une nouvelle. Un e-mail ne part que pour une alerte nouvelle.

**Délai.** Le défaut du variateur est demandé à la réception du heartbeat qui
le porte (la console en envoie un toutes les 10 s) ou du lot d'événements
(envoyés à chaque étape du lien, toutes les secondes), et écrit par la
mutation planifiée sans délai (`runAfter(0)`) ; la cloche du site se met à
jour par abonnement, sans attendre.
Tant que la console joint Convex, un gestionnaire connecté au site le voit
donc en moins d'une minute. La machine hors ligne est écrite au passage de
la tâche qui suit les 90 s sans heartbeat, donc entre 90 s et 150 s après
le dernier heartbeat. L'e-mail part par le composant d'envoi, par lots,
sans délai garanti. Rien de cela n'a été mesuré contre un déploiement.

| Fonction | Type | Autorisation | Effet / retour |
|---|---|---|---|
| `list` | query | connecté ; vide pour un `user` | Les 50 alertes visibles de l'appelant les plus récemment changées, **dans l'organisation de l'appel**, sélectionnées avant la coupe. Pour chacune : type, machine et son nom actuel, `machineDeleted`, `detail`, `count`, dates, `read` ; la séance (`sessionId`) seulement si l'appelant peut la lire (`canAccessSession`), et pour `session_failed` sa raison de fin et son `endCode` (`reason`, `reasonCode`), lus sur la séance à ce moment. |
| `unreadCount` | query | jamais de refus | Le nombre d'alertes non lues visibles, 100 au plus ; `null` pour un visiteur déconnecté, un compte inconnu, un appel sans organisation acceptée et un `user`. C'est ce que lit la cloche de l'en-tête, sur toutes les pages. |
| `markRead` | mutation | admin ou gestionnaire ; sa propre alerte visible, dans l'organisation de l'appel | Marque l'alerte lue. Une alerte invisible, d'un autre compte ou d'une autre organisation : `notification_not_found`, comme une alerte inexistante. |
| `markAllRead` | mutation | admin ou gestionnaire | Lit et marque au plus 500 alertes visibles non lues par transaction (`MARK_ALL_READ_LIMIT`) ; renvoie `{marked}`, le nombre marqué dans cette transaction. La suite est planifiée sans nouveau clic. Les alertes invisibles restent non lues. |
| `getSettings` | query | connecté ; `null` pour un `user` | `{emailAvailable, email}` : si le déploiement envoie des e-mails (les trois variables du §8), et le choix par type, valeurs par défaut comprises. |
| `updateSettings` | mutation | admin ou gestionnaire | `{type, email}` : choisit si ce type part aussi par e-mail pour l'appelant. |

**Visibilité (ANH-237).** Une même règle, dans
[`convex/lib/notificationVisibility.ts`](../convex/lib/notificationVisibility.ts), sert à `list`, `unreadCount`,
`markRead` et `markAllRead` : l'alerte appartient à l'appelant et à
l'organisation de l'appel ; l'appelant est admin Anheart, admin de cette
organisation, ou gestionnaire lié à la machine par un lien
`machine_gestionnaires` de cette organisation. Cette décision ne lit aucun
document `machines` ni `sessions`. Elle repose sur l'hypothèse du produit :
**une machine ne change pas d'organisation** ; l'organisation de l'alerte et
celle du lien sont donc celles de la machine. Un gestionnaire retiré d'une
machine ne voit plus ses alertes, même si elles sont plus récentes que les
alertes encore visibles.

Pour les admins, les index par destinataire et organisation sélectionnent
directement les alertes visibles. Pour un gestionnaire, les index par
destinataire, organisation et machine ne lisent que les machines liées : la
liste prend au plus 50 lignes par machine, fusionne par `updatedAt`
décroissant puis garde 50 lignes au total ; le compteur s'arrête à 100 non
lues visibles au total. `unreadCount` ne lit ni machines ni séances : un
heartbeat qui réécrit le statut ou le direct d'une machine ne change pas
ses dépendances. `list` lit la machine pour son nom et son état de
suppression ; les droits de la séance restent vérifiés séparément.

Une machine supprimée logiquement conserve ses liens : ses alertes restent
visibles et comptées pour l'admin d'organisation et le gestionnaire encore
lié, **comme ses séances restent lisibles**. `machineDeleted: true` permet
au site de remplacer le lien vers sa fiche par « Machine supprimée » ; le
lien vers une séance lisible reste proposé. Un gestionnaire délié ne voit
ni ses alertes ni les séances dont l'accès dépendait de ce lien.

Quand un lot de `markAllRead` atteint 500 lignes, il planifie
`notifications.continueMarkAllRead` (mutation interne, `runAfter(0)`). Chaque
lot relit l'utilisateur, l'organisation, son appartenance et son rôle
effectif, puis réapplique la règle de visibilité et la même borne de 500
lignes d'alertes. Si le compte, l'organisation ou le rôle ne permet plus
l'accès, la suite s'arrête ; les liens retirés entre deux lots ne donnent
plus accès aux alertes restantes.

Les trois mutations publiques prennent un jeton de la limite par compte (ANH-166).
`notifications.purge` (interne, tâche de §7) supprime les alertes inchangées
depuis 90 jours, lues ou non, par lots de 500. Supprimer un compte
(`users.deleteUser`) supprime ses alertes des organisations retirées, et son
choix d'e-mails quand le compte lui-même est supprimé.

**L'e-mail** (`convex/lib/notificationEmail.ts`) ne dit que le type d'alerte,
le nom de la machine (sur une ligne, 80 caractères au plus) et un lien vers
la page des alertes du site (`{SITE_URL}/{langue}/dashboard/notifications`).
Jamais de fréquence cardiaque, de nom de passager, de raison de fin, ni
d'identifiant de séance : la page derrière le lien les montre à qui peut les
lire. Il est écrit dans la langue du compte (`users.language`). Il n'est mis
en file que si le déploiement a ses trois variables (§8), que le
destinataire veut ce type par e-mail et qu'il a une adresse.

**Extension (ANH-145).** Un incident sur une machine sera un type de plus
(`notificationTypeValidator` et `notificationEmailFields` dans
`convex/schema.ts`, `NOTIFICATION_TYPES`, deux traductions) et un appel de
`scheduleAlert` là où l'incident est créé.

---

## 6. Routes HTTP machine (`convex/http.ts`)

**Hôte** : l'URL `.convex.site` du déploiement (pas `.convex.cloud` : les routes
HTTP n'y existent pas).

**Authentification** : chaque route exige
`Authorization: Bearer <clé API de la machine>`. Sans en-tête : **401**
`{"error": "unauthorized", "message": "Missing Authorization header"}`.
Clé inconnue, révoquée ou expirée : **401**
`{"error": "unauthorized", "message": "Invalid API key"}`, la même réponse
dans les trois cas.

**Contrat** : chaque route (9 des 10, l'exception suit) exige ensuite l'en-tête
`X-Anheart-Contract: <majeure.mineure>`, vérifié **après** la clé et avant
tout traitement, par le même point de passage que l'authentification
(`validateMachineAuth`, `convex/lib/machineHttpAuth.ts`). En-tête absent,
illisible, ou d'une majeure non servie : **426**
`{"error": "contract_unsupported", "message": "…", "supported": ["1"]}`,
et rien n'est lu ni écrit, hors la dernière utilisation de la clé (ci-dessous,
au plus une fois par minute). Une mineure plus récente que celle du serveur
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
les deux queries internes d'authentification, quelle que soit la clé. Une
machine simplement `offline` peut envoyer son heartbeat avec une clé valide.
Une machine restaurée retrouve ses clés qui n'ont été ni révoquées ni
expirées pendant sa suppression.

**Plusieurs clés (ANH-165).** Une machine a ses clés dans `machine_api_keys`
(§2). Toutes celles qui ne sont ni révoquées ni expirées ouvrent les routes :
pendant le chevauchement d'une rotation, l'ancienne et la nouvelle. Une clé
expirée est refusée à son `expiresAt` ou peu après, sans attendre que la
tâche la marque révoquée : la query d'authentification compare l'expiration à
`Date.now()`, et Convex peut servir pendant un court moment une réponse
qu'elle a calculée avant (ANH-238). Une clé révoquée est refusée dès la
requête suivante : la révocation écrit la clé, ce qui invalide toute réponse
gardée.

La clé contient un sélecteur public aléatoire de 16 octets et un secret
aléatoire de 32 octets, encodés en hexadécimal. `crypto.getRandomValues` génère
ces valeurs ainsi qu'un sel indépendant de 16 octets. Le vérificateur est
exactement `HMAC-SHA-256(key = sel, message = UTF-8(clé complète))` : une
construction SHA-256 salée, et non un simple encodage ni `SHA-256(clé)`.
Le sel est public ; la résistance à la recherche exhaustive vient du secret
aléatoire de 256 bits. Ce mécanisme n'est pas destiné à des mots de passe humains.

L'index `by_selector` de `machine_api_keys` sélectionne une seule clé ; une
clé révoquée ou expirée, ou dont la machine est supprimée ou désactivée, est
refusée ; puis WebCrypto `subtle.verify("HMAC", …)` vérifie le digest ; aucune
comparaison JavaScript de secrets n'est effectuée. Un sélecteur présent dans la
table n'est jugé que par elle. Un sélecteur absent peut être la clé unique
d'une machine écrite avant ANH-165, que la migration n'a pas encore déplacée :
elle est cherchée par l'index `by_apiKeySelector` de `machines`, sous les
mêmes règles de machine, et vérifiée de même. Après la migration, ce repli ne
trouve plus rien. Le [runtime Convex](https://docs.convex.dev/functions/runtimes)
prend en charge WebCrypto ; son [vérificateur HMAC natif](https://github.com/get-convex/convex-backend/blob/588c89b23f669e9ab158848d1ecd53707dac09b8/crates/webcrypto/src/hmac.rs)
appelle `aws_lc_rs::hmac::verify`, dont la [comparaison est à temps constant](https://docs.rs/aws-lc-rs/1.16.3/aws_lc_rs/hmac/fn.verify.html).
La recherche du sélecteur et les rejets de format/état ne promettent pas un
temps constant pour la requête entière. Les tests locaux vérifient le résultat
et un vecteur indépendant, pas les temps d'exécution du service hébergé.

La création d'une machine renvoie `{machineId, apiKey}` et celle d'une clé
`{keyId, apiKey, prefix}`, une seule fois. Les lectures sélectionnent leurs
champs sans credential ; aucun journal applicatif n'enregistre la clé, et aucun
refus ne la contient. Une rotation laisse les anciennes clés valides pendant
le chevauchement (24 h par défaut), puis les refuse ; une révocation, ou une
rotation sans chevauchement, les refuse dès la requête suivante. Les champs de
suppression/désactivation de la machine restent inchangés.

**Dernière utilisation, au plus une fois par minute.** L'authentification ne
fait que lire (`machines.getMachineByApiKey` est une query). Quand la dernière
utilisation écrite de la clé a une minute ou plus, la query le dit
(`keyUseDue`), et la route l'écrit ensuite (`machineKeys.recordKeyUse`, qui
revérifie la minute dans sa transaction) : une machine qui envoie une requête
toutes les quelques secondes écrit `lastUsedAt` une fois par minute, pas à
chaque requête. Une clé pas encore déplacée dans la table n'a pas de dernière
utilisation. L'ordre ne change pas : la clé, puis le contrat, puis la limite
de débit de la machine (ANH-166), comptée après l'authentification. La
dernière utilisation est donc écrite pour toute requête dont la clé est
acceptée, même refusée ensuite (426, 429, 413, 400) : « rien n'est écrit »,
ailleurs dans cette page, s'entend hors de cette date.

**Erreurs** : tout refus des 10 routes machine (les 401 et les 426 compris)
a la forme
`{"error": "<code stable>", "message": "<texte>"}`. Le code ne change pas
d'une version à l'autre ; le texte, en anglais, est fait pour être lu et peut
changer. La liste des codes est dans `contracts/machine-api.json` :

| Code | Statut | Sens |
|---|---|---|
| `contract_unsupported` | 426 | Majeure de contrat absente ou non servie ; la réponse porte `supported`. |
| `unauthorized` | 401 | En-tête `Authorization` absent, clé inconnue, révoquée ou expirée, machine supprimée ou désactivée. |
| `invalid_request` | 400 | Corps ou paramètres mal formés. |
| `session_not_found` | 400, ou 404 sur `training/status` | Séance inconnue, ou qui n'appartient pas à la machine authentifiée (même réponse dans les deux cas). |
| `session_not_pending` | 400 | `training/start` sur une séance qui n'attend plus son départ. |
| `machine_not_found` | 400 | La machine a disparu entre l'authentification et l'écriture. |
| `request_failed` | 400 | Toute autre erreur levée pendant le traitement ; `message` porte son texte. |
| `rate_limited` | 429 | La machine a dépassé la limite de débit de la route ; la réponse porte `retryAfterMs` et l'en-tête `Retry-After` (contrat 1.4, [ci-dessous](#limitation-de-débit-et-taille-des-corps-anh-166)). |
| `payload_too_large` | 413 | Corps au-delà de la taille de la route, ou lot de plus de 600 points ou 200 événements, refusé avant d'être parcouru (contrat 1.4). |
| `expired_before_start`, `machine_deleted_before_start`, `organization_changed_before_start`, `right_revoked_before_start`, `age_refused_before_start`, `hr_max_changed_before_start`, `rider_busy` ; depuis 1.6 : `rider_not_fit`, `fitness_expired`, `consent_missing` | 409 | `training/start` seulement (depuis le contrat 1.5) : le lancement, revérifié au moment où la machine l'arme, est refusé ; la séance est `failed` avec ce code, et la machine ne doit pas la faire tourner ([Revérification à l'armement](#revérification-à-larmement-anh-144)). |

Côté Pi, une absence de réponse devient `Unreachable`, toute réponse ≥ 400
devient `Refused`, et son code est journalisé. Pour ce que la console envoie
d'une séance, `answer_of` (`raspberry-pi/src/cloud_sync.py`) en tire l'une de
quatre suites : acquitté ; **retenu** (pas de réponse, 401, 403, 408, 425,
426, 429, 5xx : rien n'est reçu, rien n'est abandonné, la même requête est
refaite après 15 s) ; route inconnue (404 sans code) ; refusé (tout autre
cas, 409 compris, définitif quand le code est `invalid_request`, `session_not_found`,
`session_not_pending`, `machine_not_found` ou `payload_too_large`). Un 429
retient de plus sa route, côté console, jusqu'à la fin de l'attente qu'il
annonce. Détail :
[raspberry-pi.md](raspberry-pi.md#83-ce-que-chaque-réponse-fait-au-curseur).

**Corps** : les sept routes `POST` attendent un objet JSON. Un corps qui n'en
est pas un est refusé par **400** `invalid_request`, et rien n'est écrit :
du texte qui n'est pas du JSON (un objet tronqué compris), ou du JSON valide
qui n'est pas un objet (`null`, un nombre, une chaîne, un booléen, un
tableau). La clé est jugée d'abord, puis le contrat, puis, sur les routes
limitées, le débit (429) et la taille du corps (413), puis le corps : 401,
426, 429, 413, 400, dans cet ordre. Un test tient la règle pour chaque route `POST` du
routeur, celles qui s'y ajouteront comprises (`convex/httpRoutes.test.ts`).

Le heartbeat a une seule tolérance que les autres n'ont pas : un corps
**vide** y vaut un heartbeat sans champ (200). C'est ce qu'envoie le test de
connectivité de `raspberry-pi/README.md` (un `curl` sans corps), et ce
qu'envoyait au repos la console de l'ancien mode d'enregistrement. Il refuse
aussi, par le même 400 et avant d'enregistrer quoi que ce soit, un objet dont
l'un des trois champs stockés tels quels n'a pas son type : `batteryLevel` et
`wifiStrength` sont des nombres, `activeSessionId` une chaîne. Les autres
champs gardent leur règle, dite plus bas : un `live` mal formé est ignoré, un
`software_version` mal formé est effacé, un champ inconnu à la racine du corps
n'est pas lu.

### Limitation de débit et taille des corps (ANH-166)

Une machine ou un compte qui envoie trop est arrêté avant que ce qu'il envoie
soit lu ou écrit. Pas de serveur à tenir : c'est le composant Convex
[`@convex-dev/rate-limiter`](https://www.npmjs.com/package/@convex-dev/rate-limiter)
(version `0.3.2`, épinglée exactement dans `package.json`), monté par
`convex/convex.config.ts` sous le nom `rateLimiter`. Son état vit dans la
table de ce composant, hors du schéma de l'application : un seau à jetons par
limite et par clé. Un appel refusé n'y écrit rien ; seul un appel accepté
prend un jeton. Les limites sont nommées dans `convex/lib/rateLimits.ts`.

**Par machine** (clé : la machine authentifiée), en seau à jetons. Les
valeurs sont lues dans le contrat partagé (`contracts/machine-api.json`,
`rate_limits`), auquel les tests du Pi épinglent ses cadences : une console
qui fonctionne comme prévu ne les atteint jamais.

| Route | Limite | Réserve | Cadence de la console |
|---|---|---|---|
| `POST /api/machine/heartbeat` | 1 toutes les 5 s | 1 | toutes les 10 s |
| `GET /api/machine/training/poll` | 1 toutes les 2 s | 1 | toutes les 3 s au repos |
| `POST /api/machine/training/telemetry` | 2 lots toutes les 5 s | 2 | un lot toutes les 3 s au plus vite |
| `POST /api/machine/training/events` | 10 par seconde | 10 | une fois par étape du lien (1 s) |
| `POST /api/machine/records/upload-url` | 10 par seconde | 64 | 64 fichiers par étape du dépôt, 4 transferts à la fois |
| `POST /api/machine/training/local` | 1 par minute | 20 | une déclaration par séance démarrée à la machine ; rattrapage une séance à la fois, après l'envoi de la précédente |
| `POST /api/machine/profiles` | 1 toutes les 10 s | 5 | une fois par révision des programmes modifiés par l'opérateur ; pas de nouvel essai avant 15 s après un échec |
| `POST /api/machine/records/commit` | 10 par seconde | 64 | au plus un commit par réservation `upload-url`, 64 fichiers par étape du dépôt |

Ces trois plafonds (ANH-228) suivent le trafic attendu d'une console : une
séance avec quelqu'un à bord dure des minutes (embarquement, rampe et plancher
médical du refroidissement). La réserve de 20 déclarations couvre une journée
de séances faites hors ligne ; le rattrapage n'en déclare qu'une à la fois,
après avoir envoyé la précédente. Au-delà, le 429 retient la route et la
console refait la même déclaration : rien n'est perdu et la séance continue
sous l'autorité du Pi. Un seau `training/local` vide retarde toutefois la
déclaration des séances démarrées à la machine. Le tableau de bord ne connaît
pas une séance non déclarée : son affichage, son arrêt depuis le tableau de
bord et l'envoi de sa fin attendent aussi. Sans attaquant, une console qui
rattrape plus de 20 séances d'un coup peut ainsi retarder la séance suivante
d'environ une minute, pouvant atteindre environ 75 s dans un cas limite :
l'attente de `retryAfterMs` dure 60 s au plus à compter de la réception du
429, mais les essais ont lieu toutes les 15 s ; une déclaration du rattrapage
déjà en vol au démarrage de la séance peut ainsi repousser la déclaration
de celle-ci à l'essai suivant. Les délais du réseau et de la boucle du lien
s'y ajoutent ; la séance
en cours est déclarée avant le rattrapage. Avec une clé volée qui garde ce
seau vide, ce retard peut durer jusqu'à sa révocation. L'arrêt local n'en
dépend jamais, et les routes d'arrêt et de fin elles-mêmes ne sont jamais
limitées. Une clé volée peut créer au plus 20 + 1 440 séances par jour, au lieu
d'un nombre illimité. Chaque envoi de `profiles` efface et réécrit les
programmes de la machine. Un dépôt qui fonctionne n'envoie jamais plus de
commits que de réservations, déjà limitées à 10 par seconde avec une réserve
de 64 (une étape du dépôt, `FILES_PER_STEP`) ; chaque commit peut planifier
une vérification des octets dans Storage.

Au-delà : **429** `{"error": "rate_limited", "message": "…", "retryAfterMs": <ms>}`
et l'en-tête `Retry-After` (en secondes, arrondi au-dessus). La limite est
prise après la clé et le contrat, et **avant** que le corps soit lu : une
rafale de requêtes énormes coûte une lecture de clé et un appel au composant
chacune, rien de plus. `retryAfterMs` est le délai avant lequel la même
requête serait encore refusée ; la console ne redemande pas la route avant
([raspberry-pi.md](raspberry-pi.md#86-ce-que-la-synchronisation-ne-peut-pas-coûter)).

Écart au ticket, à confirmer : le ticket demandait **10 réservations par
minute** pour `upload-url`. Le dépôt d'ANH-130, écrit depuis, réserve un
fichier par appel, et un programme de deux heures en compte environ 36 000
([enregistrement.md](enregistrement.md#dépôt-vérifié-dans-convex)) : à 10 par
minute, son dépôt prendrait 60 heures. La limite est donc de 10 par seconde
avec une réserve de 64 (une étape du dépôt), soit une heure pour une telle
séance. La valeur est une ligne du contrat partagé.

**Jamais limitées** : la demande d'arrêt (`GET /api/machine/training/status`),
pour qu'un arrêt passe toujours, et la fin d'une séance
(`POST /api/machine/training/end`). `POST /api/machine/training/start`
n'écrit que sur un lancement en attente de la machine, déjà borné par
`launchAutoSession` ; un 429 y arrêterait une console d'avant 1.5 déjà armée.
`GET /api/machine/records/status` et `GET /api/machine/roster` ne font que
lire : rien n'est écrit et une requête sans clé valide, qu'on ne peut pas
limiter, coûte déjà autant. Une requête sans clé
valide est refusée (401) avant toute limite : Convex ne voit pas l'adresse de
l'appelant, il n'y a donc rien sur quoi limiter un appel anonyme. Chacun coûte
une lecture d'index.

**Taille des corps**, vérifiée avant toute lecture du JSON : d'abord
l'en-tête `Content-Length`, puis les octets reçus, la lecture s'arrêtant au
premier octet de trop (un `Content-Length` plus petit que le corps ne fait
rien passer). Au-delà : **413** `payload_too_large`, rien n'est écrit.

| Route | Corps | Éléments |
|---|---|---|
| `POST /api/machine/heartbeat` | 16 Kio (16 384 octets) | - |
| `POST /api/machine/training/telemetry` | 300 Kio (307 200 octets) | 600 points |
| `POST /api/machine/training/events` | 2,5 Mio (2 621 440 octets) | 200 événements |

Le nombre d'éléments est vérifié aussitôt le JSON lu, avant d'en examiner un
seul : 601 points ou 201 événements reçoivent 413 (400 avant le contrat 1.4).
Les tailles laissent passer le plus grand lot valide : 200 événements de
2 000 caractères écrits `\uXXXX` (6 octets chacun), comme la console écrit un
caractère non ASCII, tiennent sous 2,5 Mio ; 600 points aux nombres les plus
longs sous 300 Kio ; le plus grand heartbeat de la console (128 codes de
défaut du variateur) fait environ 8 Kio. Les autres routes `POST` n'ont pas
de taille propre ; seule la limite de Convex sur le corps d'une action HTTP
les borne (20 Mio selon sa documentation publique, non vérifiée ici).

**Bornes des événements** (revue d'ANH-129) : `seq` vaut au plus 999 999, ce
qui borne aussi le nombre d'événements d'une séance (un événement est un
`(sessionId, seq)`) ; `t` est un nombre entier de millisecondes.

**Par compte** (clé : le sujet du jeton Clerk) :

| Quoi | Limite |
|---|---|
| `training.launchAutoSession` | 5 par minute (et compté aussi dans la ligne suivante) |
| toute mutation publique qui écrit, et l'action `users.createPatient` | 60 par minute, ensemble |
| `training.requestStop` | **jamais limitée** : refuser un arrêt n'est jamais le côté sûr |

Au-delà, l'appel lève une `ConvexError`
`{code: "rate_limited", message, retryAfterMs}` ; le site l'affiche par le
texte `errors.rate_limited` de `messages/*.json`. Dans une mutation, le jeton
pris est rendu si la mutation échoue ensuite (la transaction entière est
annulée) : seule une écriture qui a lieu compte. Un appel sans identité n'en
prend pas : la fonction le refuse d'elle-même sans rien écrire. Dans l'action
`createPatient`, le jeton est pris avant l'appel à Clerk et reste pris.

Ce qui reste ouvert : le plafond de lignes de télémétrie par séance pour une
console qui ne dit pas l'âge de sa séance (contrat 1.0, aucune date n'y est
refusée) n'existe pas ; le débit par machine le borne seul. Le comportement
du composant n'a été vérifié qu'en mémoire (`convex-test`), contre aucun
déploiement.

### Routes utilisées par la console locale (`raspberry-pi/src/cloud_sync.py`)

| Méthode et chemin | Corps / paramètres | Réponse 200 | Cadence côté Pi |
|---|---|---|---|
| `POST /api/machine/heartbeat` | `{live, programsEnabled, activeSessionId?, software_version, contract_version, medical_parameters_version\|null, config_hash\|null, health?}` ; champs de santé détaillés au §2 (`batteryLevel`, `wifiStrength` acceptés, non envoyés) ; 16 Kio max | `{success: true, serverTime, server_contract_version}` | toutes les 10 s |
| `POST /api/machine/profiles` | `{storeRev, programsEnabled, profiles: [...]}` | `{count}` | quand la révision du magasin change ; nouvel essai après 15 s ; limite 1 toutes les 10 s, réserve 5 |
| `GET /api/machine/roster` | - | `{riders: [{userId, name, hrMax\|null, birthYear\|null, fit, fitnessValidUntil\|null, consent, fitnessQuestionnaireVersion\|null}]}` (`getRoster`, [`roster` du contrat 1.6](../contracts/machine-api.json)) | au démarrage de la console, puis toutes les 60 s sans séance en cours ; non limitée en débit. La console garde la liste en mémoire seulement, au plus 15 min sans nouvelle réponse, et la vide sur un 401 ou un 403 ([raspberry-pi.md, 4.5](raspberry-pi.md#45-le-passager--la-liste-du-tableau-de-bord-anh-84)) |
| `GET /api/machine/training/poll` | - | `{session: null, server_contract_version}` ou `{session: {sessionId, profileId, totalDurationS\|null, subjectId, subjectLabel, subjectHrMax, subjectAge\|null, operatorName}, server_contract_version}` | toutes les 3 s, seulement si `PROGRAMS_ENABLED`, sans séance en cours ni lancement en attente |
| `POST /api/machine/training/start` | `{sessionId, startedAt?, sessionAgeMs?}` | `{success: true}` ; **409** `{error: <refus à l'armement>, message}` si le lancement revérifié est refusé (contrat 1.5) | après la confirmation de l'opérateur et avant l'armement ; seul un 2xx autorise l'armement, et sur un 409 la console n'arme pas et n'envoie pas de fin. Les deux champs sont facultatifs. Jamais limitée en débit |
| `POST /api/machine/training/local` | `{localRef, kind: "auto"\|"manual", startedAt, sessionAgeMs?, operatorName, profileId?, profileName?, zoneLowBpm?, zoneHighBpm?, totalDurationS?, subjectHrMax?, occupancy?, userId?, subjectLabel?}` | `{sessionId}` | avant tout autre envoi d'une séance démarrée à la machine, et de nouveau tant qu'elle n'est pas acquittée (après un redémarrage de la console aussi) ; idempotent par `localRef`, qui est la référence de l'enregistrement local ; limite 1 par minute, réserve 20 |
| `GET /api/machine/training/status?sessionId=…` | - | `{status, active, stopRequested, server_contract_version}` ; 404 si inconnue. Répond quel que soit le contrat annoncé. | toutes les 3 s pendant une séance |
| `POST /api/machine/training/telemetry` | `{sessionId, points: [{t, elapsedS, phase, bpm?, motorRpm, outputRpm, setpointMotorRpm, gLoad, safetyAction}]}` (600 points et 300 Kio max) | `{stored, duplicates, rejected}` | lots de 300 points max : toutes les 5 s pour une séance à jour, toutes les 3 s pour ce qui est en retard |
| `POST /api/machine/training/events` | `{sessionId, events: [{seq, t, kind, detail, actor}]}` (200 événements et 2,5 Mio max, 2000 caractères de texte par événement) | `{stored, duplicates, rejected}` | avec la télémétrie, 200 événements max par envoi |
| `POST /api/machine/training/end` | `{sessionId, failed, reason, endedAt?}` | `{success: true}` | quand l'enregistrement de la séance est fermé et sa télémétrie acquittée ; sans `endedAt` pour un lancement que la machine a refusé |

Détails du contrat :

- **`live`** n'est pris en compte que s'il est bien formé (`runMode`, `phase`,
  `safetyAction` chaînes ; vitesses et `gLoad` nombres ; `bpm` nombre ou absent).
  Sinon seul le heartbeat est enregistré.
- **Statut de la machine** : chaque heartbeat met `online`, ou `in_session` si
  `activeSessionId` est présent.
- **Séance nommée** : si `activeSessionId` nomme une séance `interrupted` de
  cette machine, elle redevient `active` (`interruptedAt` effacé). Une séance
  d'une autre machine n'est pas touchée.
- **Versions** : chaque heartbeat écrit `machines.softwareVersion` (le champ
  `software_version` s'il est bien formé : 64 caractères au plus, lettres,
  chiffres, `.`, `_`, `+`, `-` ; sinon le champ est effacé),
  `machines.contractVersion` (l'en-tête `X-Anheart-Contract`, qui vient d'être
  vérifié, et non la copie `contract_version` du corps) et
  `machines.lastVersionSeenAt`. Il retient aussi
  `medical_parameters_version` dans `machines.medicalParametersVersion` : une
  version bien formée est gardée comme chaîne, `null` est gardé comme annonce
  explicite de paramètres non signés, et un champ absent ou mal formé ne donne
  pas de version. Une console antérieure à la PR #103 envoie aussi `null` :
  elle est classée non signée et le refus de lancement est correct, mais le
  libellé « mode banc » ne la décrit pas. `medicalParametersStatus` et
  `medicalParametersReferenceVersion` sont calculés ensuite à la lecture.
- **Serveur d'un autre contrat** : `server_contract_version` vaut la version
  servie par Convex. Le Pi n'arme rien d'une réponse dont la majeure n'est
  pas la sienne ; il termine le lancement par `/training/end` avec la raison
  `refusee par la machine : serveur incompatible (contrat X vs Y)`.
- **Arrêt demandé** : le Pi pose la question `status` toutes les 3 s par une
  tâche à part, qui n'attend derrière aucun envoi. Si `status` répond
  `stopRequested: true` ou
  `active: false`, le Pi fait un arrêt ordinaire sur la rampe réglée, attribué
  à l'opérateur « tableau de bord », **quelle que soit la version** annoncée
  par la réponse (même majeure, autre majeure, absente ou illisible). Ces deux
  champs ne peuvent provoquer qu'un arrêt ordinaire : les croire est le côté
  sûr. Rien d'autre n'est retenu d'une réponse d'une autre majeure : rien n'y
  peut lancer, reprendre ou réarmer quoi que ce soit.
- **Lancement refusé par le Pi** : le Pi termine la séance `pending` par
  `/training/end` avec `failed: true` et une raison qui commence par
  `refusee par la machine : `, sans `endedAt` : le serveur date ce refus. Un
  lancement ni démarré ni refusé en 60 s est terminé avec « la boucle n'a ni
  demarre ni refuse ».
- **Départ non pris** : si `/training/start` reçoit autre chose qu'un oui, le
  Pi arrête la séance qu'il vient d'armer, tout de suite et une fois. Cela
  vaut pour un refus (séance annulée sur le site entre le poll et
  l'armement, 400 `session_not_pending` ; lancement refusé à l'armement,
  409, contrat 1.5) comme pour une réponse retenue (426, 401, 403, 5xx) : une
  séance lancée du site ne tourne pas là où le site ne pourrait pas
  l'arrêter. Une console antérieure à 1.5 lit le 409 de la même façon : son
  code lui est inconnu, son indicateur de liaison montre alors un instant une
  réponse qu'il ne reconnaît pas, mais la confirmation est refusée et la
  séance arrêtée. Vérifié sur le code de la console (`answer_of` donne un
  refus pour un 409, `_confirm` arrête la séance sur tout refus), pas sur
  une machine. Retenue, la confirmation reste due et est refaite. Seule
  l'absence de réponse exploitable (réseau coupé, délai dépassé, réponse 2xx
  qui n'est pas un objet JSON) laisse tourner la séance, sous le superviseur
  local. La confirmation part dès que la séance est armée, par la même tâche
  que la question d'arrêt : elle n'attend aucune lecture d'enregistrement, ni
  la retenue de 15 s posée par une autre requête restée sans réponse.
- **Fin** : `failed` vaut `false` pour `programme_complete` et `operator_stop`,
  `true` pour `emergency_stop`, `safety_verdict`, `tick_exception`, `shutdown`,
  et pour `interrupted` : la raison d'une séance dont la console a été tuée
  avant la fin, envoyée après son redémarrage. La raison est celle de
  l'enregistrement local, suivie du texte de l'arrêt quand il y en a un
  (`operator_stop: <texte>`).
- **Un lot peut être envoyé deux fois.** `training/telemetry` et
  `training/events` n'écrivent un point (`(sessionId, t)`) ou un événement
  (`(sessionId, seq)`) qu'une fois. La réponse compte chaque élément du lot
  dans exactement une case : `stored` (écrit par cet appel), `duplicates`
  (déjà là, laissé tel quel, avec la date de sa première réception) ou
  `rejected` (daté hors de la séance, non écrit). Un 200 acquitte donc tout
  le lot : la machine n'a rien à renvoyer, et renvoyer ne coûte rien.
- **Pas de limite « données trop anciennes ».** Aucune route d'entraînement
  ne refuse un point parce qu'il est vieux : une séance d'il y a une semaine
  s'envoie entière. Pour une console qui ne dit pas l'âge de sa séance,
  **aucune date n'est refusée** et `rejected` vaut toujours 0. La seule borne
  est de cohérence, elle ne vaut que pour une séance dont la machine dit
  l'âge, et elle se lit sur l'horloge **de la machine**
  ([Deux horloges](#deux-horloges)) : hors de ses bornes un point ou un
  événement est compté `rejected`, sans refuser le lot.
- **Dates** : `startedAt` (dans `training/local` et `training/start`) est le
  début tel que la machine l'a daté ; `sessionAgeMs`, facultatif, le temps
  écoulé depuis, compté sur son horloge monotone. Une valeur qui n'est pas un
  nombre n'est pas lue. C'est la présence de `sessionAgeMs` qui change ce que
  le serveur fait des dates : [Deux horloges](#deux-horloges). La console de
  ce dépôt l'envoie toujours quand elle peut le compter.
- **Événements** : `seq` entier de 0 à 999 999, `t` nombre entier (de
  millisecondes), `kind` de 64
  caractères au plus (minuscules, chiffres, `_`, une lettre d'abord), `detail`
  de 2000 caractères au plus, `actor` de 1 à 64 caractères parmi lettres,
  chiffres, `_` et `-`. Un événement mal formé refuse tout le lot (400
  `invalid_request`, « Malformed event »), comme un point mal formé.
- **Réseau perdu, serveur d'un autre contrat, console redémarrée** : la
  séance continue sous le seul superviseur local, et rien de ce qu'elle doit
  au tableau de bord n'attend en mémoire. C'est dans son enregistrement sur
  le disque du Pi, relu depuis un curseur : au retour du lien, la console
  envoie ce qui manque, la séance en cours d'abord, puis les précédentes de
  la plus ancienne à la plus récente, sans limite d'ancienneté ni de nombre
  ([raspberry-pi.md §8](raspberry-pi.md#8-la-synchronisation-avec-le-tableau-de-bord)).

### Routes retirées

Les cinq routes de l'ancien mode d'enregistrement ECG (interrogation, début,
fin et statut d'une séance d'enregistrement, envoi des lots ECG) n'existent
plus : leurs chemins répondent **404**, même avec une clé valide. Le client qui
les appelait a été retiré du Pi.

---

## 7. Tâche planifiée

**Lancements en attente (ANH-144).** `sweep-pending-sessions`, **toutes les
minutes** (`training.sweepPendingSessions`) : tout lancement du site encore
`pending` que la revérification à l'armement refuse passe `failed`, avec
`endedAt`, `endReason` et `endCode` : celui qui attend depuis plus de
`PENDING_SESSION_TTL_S` (300 s par défaut, « expired before the machine armed
it »), et celui dont les droits, le pratiquant ou la machine ont changé
pendant qu'il attendait ([Revérification à
l'armement](#revérification-à-larmement-anh-144)). Une machine hors ligne
10 min : son lancement est expiré dans la minute qui suit ses 5 min
d'attente, et la machine revenue ne se le voit pas offrir ; si elle l'avait
lu avant de se taire, la console ne l'aurait armé qu'après confirmation de
son opérateur et réponse 2xx de `training/start`. Sur un 409, elle n'arme
pas et n'envoie pas de fin ; une console antérieure, qui arme avant de
confirmer, s'arrête aussitôt sur la rampe ordinaire. « Expirée » dit donc que le départ n'a pas
été confirmé à temps, pas que la machine n'a pas tourné.

`convex/crons.ts` : `check-offline-machines`, **toutes les minutes**. Une machine
`online` ou `in_session` sans heartbeat depuis **90 s** (`LIVE_FRESH_MS`, le
seuil du site) passe `offline`. Le site n'attend pas ce passage pour
l'afficher hors ligne : il le fait à 90 s, sur `lastHeartbeat` et `serverNow`.
Ce passage écrit l'alerte `machine_offline` de ses gestionnaires et des
admins de son organisation (ANH-90, [§5](#notificationsts-alertes-aux-gestionnaires-anh-90)) ;
une machine déjà `offline` n'en écrit pas d'autre.

`purge-notifications`, **chaque jour** à 3 h 47 UTC : supprime les alertes
inchangées depuis 90 jours (`notifications.purge`).

**Séance sans nouvelles (ANH-216).** Le même passage marque `interrupted`,
avec `interruptedAt`, toute séance d'entraînement `active` dont Convex n'a eu
**aucune nouvelle depuis 10 min** (`SESSION_SILENCE_MS`, `lib/training.ts` ;
`interruptSilentSessions`, `convex/training.ts`). Une nouvelle d'une séance,
toujours datée par le serveur, c'est :

- un heartbeat de **sa** machine qui la nomme (`activeSessionId`) : une
  console qui fait tourner la séance la nomme à chaque heartbeat, toutes les
  10 s ;
- la réception de son point de plus grand `t` ou de son événement de plus
  grand rang (une console qui rattrape une séance finie ne la nomme plus,
  mais en envoie les mesures) ;
- sa déclaration par la machine (`_creationTime` d'une séance locale) ou la
  confirmation de son départ (`startedAt` d'un lancement du site).

Le passage n'est donc pas celui de la machine hors ligne : une machine qui
répond mais ne nomme plus la séance (console redémarrée sur un enregistrement
illisible) la voit marquée aussi, et une machine muette 2 min ne fait rien
marquer. Les séances de l'ancien mode (sans `kind`) ne sont pas touchées.

`interrupted` n'est **pas une fin** : la séance peut tourner encore sur une
machine dont le lien est coupé depuis 10 min. Rien n'est demandé à la
machine : `getTrainingStatus` répond `active: true`, `requestStop` pose la
demande d'arrêt qu'elle lira si elle revient, le premier heartbeat qui la
nomme la rend `active`, et la fin qu'elle envoie, même tard, la passe
`completed` ou `failed` avec sa date et sa raison. Ses points et ses
événements sont stockés comme avant (aucune borne de fin : `endedAt` est
absent). Le passage rend `{checkedCount, markedOfflineCount,
interruptedSessionCount}`.

**Avec les limites de débit (ANH-166).** Un heartbeat, un lot de points ou
d'événements refusé en 429 n'écrit rien, il n'est donc pas une nouvelle.
Une console qui fonctionne comme prévu n'atteint aucune de ces limites : un
heartbeat toutes les 10 s pour une limite d'un par 5 s, et ses lots de
télémétrie et d'événements sous les leurs. Une console qui en envoie trop
voit les requêtes en trop refusées, pas les autres : il en passe toujours
au moins une par 5 s. La déclaration, la confirmation du départ, la fin, la
question de l'arrêt (`getTrainingStatus`) et `requestStop` ne sont pas
limitées, ni la tâche planifiée.

**Clés expirées (ANH-165).** `revoke-expired-machine-keys`, **toutes les
5 minutes** (`machineKeys.revokeExpiredKeys`) : pose `revokedAt` sur les clés
de `machine_api_keys` dont `expiresAt` est passé, 100 au plus par passage.
Ce sont les clés qu'une rotation a remplacées, une fois leur chevauchement
fini, et celles créées avec une expiration. Une telle clé n'ouvre déjà plus
rien depuis son expiration (§6) : la tâche l'écrit, pour que la fiche la
montre révoquée. Une clé sans expiration n'est jamais touchée. Rend
`{revoked}`.

---

## 8. Déployer

Le déploiement de développement existe et reçoit le code par
`npx convex dev --once` ; la production n'a pas encore reçu le nouveau code. Les
environnements, les clés et les commandes réellement utilisées sont dans
[deploiement.md](deploiement.md#3-déployer-convex). Les étapes ci-dessous sont
celles d'un projet **neuf**.

**Enregistrer une version médicale (ANH-139).** La table
`medical_parameters_versions` et son action d'enregistrement ne sont déployées
sur aucun déploiement à ce jour. Après avoir déployé le code qui les contient,
et avant tout lancement distant, inscrire le fichier signé choisi sur ce
déploiement. Depuis la racine du dépôt, la commande ci-dessous construit les
arguments en lisant le fichier et sa signature détachée, puis appelle l'action
interne. Elle doit rendre la version inscrite. Ne pas relancer la même version
ni en inscrire une version égale ou plus ancienne.

```bash
npx convex run medicalParameters:registerVersion "$(node -e 'const fs = require("node:fs"); const content = fs.readFileSync("raspberry-pi/config/medical_parameters.json", "utf8"); const signatureBase64 = fs.readFileSync("raspberry-pi/config/medical_parameters.sig").toString("base64"); process.stdout.write(JSON.stringify({ content, signatureBase64 }));')"
```

Inscrire le fichier signé livré dès le premier déploiement de développement qui
contient cette action, avant de mettre le code en production. Si ce runtime ne
prend pas en charge Ed25519, l'enregistrement échoue fermé ; le repli est une
action avec la directive `"use node"` qui vérifie la signature avec
`node:crypto`.

Inscrire la version aussitôt après le déploiement. Si un lancement laissé en
attente par le code précédent existe encore, l'inscription est refusée ; le
balayage l'échoue dans la minute, puis réessayez. Les lancements déjà expirés
sont échoués à l'inscription avec `expired_before_start`. Si un lancement
atteint l'armement alors qu'aucune version n'est encore inscrite, il échoue
avec `age_refused_before_start`.

1. Installer les dépendances à la racine : `npm ci`.
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
6. Configurer Organizations puis le webhook selon la procédure ci-dessous,
   dans son ordre : le webhook après `ANHEART_ORG_ID` et la migration relancée.
   Inviter le premier admin dans l'organisation Clerk Anheart avec `org:admin`,
   dont l'identifiant est `ANHEART_ORG_ID`. Le webhook crée sa fiche et son
   appartenance ; son jeton porte le rôle. Aucun champ de base à éditer.
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

### `convex/_generated/api.d.ts` : tenu par un script

Ce fichier est versionné : sans lui, ni les fonctions ni le site ne passent le
contrôle des types. Il liste les modules de `convex/`, un `import` chacun.
`npx convex dev` le réécrit, mais cette commande a besoin d'un déploiement
(depuis la version 1.28 du paquet, écrire `convex/_generated/` passe par le
déploiement) : un fichier ajouté sans elle n'y entrait pas, et le fichier
s'écartait de l'arbre sans que rien le dise.

`scripts/ci/convex-generated-api.mjs` le déduit des fichiers de `convex/`,
sans déploiement et sans réseau, avec la règle du paquet (est un module tout
fichier `.ts` ou `.js` hors de `_generated/`, sauf `schema.ts`, les fichiers
dont le nom a plus d'un point comme `auth.config.ts` ou `*.test.ts`, et les
fichiers sans `import` ni `export`) et dans la forme que le paquet écrit :

```bash
node scripts/ci/convex-generated-api.mjs            # compare, et nomme les modules manquants ou en trop
node scripts/ci/convex-generated-api.mjs --write    # réécrit le fichier
```

Le job `convex-tests` lance la première forme avant le contrôle des types :
une PR qui ajoute, retire ou renomme un module sans réécrire le fichier est
refusée. Après avoir ajouté un fichier à `convex/`, lancer la seconde.

Les composants (ANH-166) : le script lit `convex/convex.config.ts` sous une
seule forme, `import <nom> from "<paquet>/convex.config.js";` puis
`app.use(<nom>, { name: "<composant>" });`, le nom écrit en clair, et écrit
pour chacun la ligne que la commande Convex écrit
(`<composant>: import("<paquet>/_generated/component.js").ComponentApi<"<composant>">;`).
La sortie a été comparée à celle de `npx convex codegen` pour le limiteur de
débit : identique. Un `use` sans nom (la commande prendrait celui du paquet,
qui n'est pas dans l'arbre), un composant défini dans l'arbre (un
`convex.config.ts` dans un dossier sous `convex/`, ou importé par un chemin
relatif) ou deux composants du même nom sont refusés.

Limites : le script ne juge que `api.d.ts`, pas les quatre autres fichiers de
`convex/_generated/`, qui ne dépendent ni de la liste des modules ni des
composants montés. Si une version ultérieure du paquet écrit ce fichier
autrement, le script doit suivre dans la même PR.

### Composant de limitation de débit (ANH-166)

Le premier `npx convex deploy` (ou `npx convex dev`) de ce code installe le
composant `rateLimiter` et sa table ; rien n'est à lancer à la main, et aucune
donnée existante n'est touchée. Ses lignes, une par limite et par machine ou
compte, ne sont jamais purgées : leur nombre est borné par le nombre de
machines et de comptes. Cela n'a été vérifié contre aucun déploiement.

### Composant d'envoi d'e-mails (ANH-90)

Le déploiement de ce code installe aussi le composant `resend`
(`@convex-dev/resend` 0.2.8, monté par `convex/convex.config.ts`, avec ses
propres composants : deux files de travail et un limiteur), et ses tables.
Rien n'est à lancer à la main. **Il n'envoie rien** tant que le déploiement
n'a pas ses trois variables :

| Variable | Contenu |
|---|---|
| `RESEND_API_KEY` | Clé d'API du compte Resend du responsable produit. Secret. |
| `NOTIFICATION_EMAIL_FROM` | Expéditeur, d'un domaine vérifié dans ce compte, par exemple `Gaura <alertes@exemple.fr>`. |
| `SITE_URL` | Adresse du site en `https://`, sans rien après le chemin : le lien de l'e-mail y ajoute `/{langue}/dashboard/notifications`. |

```bash
npx convex env set RESEND_API_KEY re_…
npx convex env set NOTIFICATION_EMAIL_FROM "Gaura <alertes@exemple.fr>"
npx convex env set SITE_URL https://…
```

Sans l'une des trois, aucune alerte n'est mise en file d'envoi et la page
des alertes du site le dit. Le composant envoie par lots, recommence en cas
d'échec, et n'envoie jamais deux fois un e-mail mis en file avec la même clé
(une par alerte). Le webhook de Resend (statuts de remise) n'est pas branché :
le site ne dit pas si un e-mail est arrivé. Aucun compte, aucune clé et aucun
domaine n'existent à ce jour ; rien de cela n'a été vérifié contre un
déploiement ni contre Resend.

### Migration du retrait de l'ancien mode ECG

**Pas encore exécutée, sur aucun déploiement.** Deux mutations internes de
`convex/migrations/retireLegacyRecording.ts`, à lancer à la main, une fois par
déploiement, **après** avoir déployé ce code :

```bash
npx convex run migrations/retireLegacyRecording:failOpenRecordingSessions
npx convex run migrations/retireLegacyRecording:removeMachineConfig
```

**Telles qu'écrites, ces deux lignes visent le déploiement de développement.**
Ce qui le décide est la clé `CONVEX_DEPLOY_KEY` de `.env.local`, celle du
développement
([deploiement.md, section 2](deploiement.md#2-les-clés-et-où-elles-sont-rangées)) :
la CLI charge ce fichier, et une clé de déploiement présente dans
l'environnement désigne à elle seule le déploiement visé. Deux conséquences,
lues dans le paquet installé (convex 1.46.0,
`src/cli/lib/deploymentSelection.ts`) :

- `CONVEX_DEPLOYMENT` n'est pas consulté tant qu'une clé est là ;
- **l'option `--prod` est ignorée.** Dans ce dépôt,
  `npx convex run --prod …` agit sur le développement, sans le dire. Ne pas
  s'en servir pour viser la production.

Pour la production, c'est sa clé qu'il faut passer, **après** le
`convex deploy` de la production
([deploiement.md, section 3.3](deploiement.md#33-vers-la-production)), en
deux temps.

1. **Lire quel déploiement la clé désigne, avant de lancer quoi que ce
   soit.** Une clé de déploiement commence par le type et le nom de son
   déploiement, jusqu'au `|` qui précède sa partie secrète :

   ```bash
   sed -n 's/^CONVEX_DEPLOY_KEY_PROD=\([a-z]*:[a-z0-9-]*\)|.*/\1/p' .env.local
   ```

   La ligne affichée doit être `prod:clean-giraffe-153`, le déploiement de
   production ([deploiement.md, section 1](deploiement.md#1-les-environnements)).
   Si elle commence par `dev:`, porte un autre nom, ou si rien ne s'affiche,
   ne pas continuer. Cette lecture ne passe pas par la CLI et ne contacte
   aucun déploiement : elle ne dépend donc pas de ce que la CLI choisirait,
   et elle n'affiche pas la partie secrète de la clé.
2. **Lancer les deux mutations avec cette clé** :

   ```bash
   CONVEX_DEPLOY_KEY="$(grep '^CONVEX_DEPLOY_KEY_PROD=' .env.local | cut -d= -f2-)" \
     npx convex run migrations/retireLegacyRecording:failOpenRecordingSessions
   CONVEX_DEPLOY_KEY="$(grep '^CONVEX_DEPLOY_KEY_PROD=' .env.local | cut -d= -f2-)" \
     npx convex run migrations/retireLegacyRecording:removeMachineConfig
   ```

   Une variable déjà présente dans l'environnement l'emporte sur celle de
   `.env.local`, que la CLI ne charge que pour les variables absentes : la
   clé passée sur la ligne remplace donc celle du développement, pour cette
   commande seulement.

Cette forme n'a été essayée sur aucun déploiement : la migration n'a tourné
nulle part.

| Mutation | Effet | Résultat renvoyé |
|---|---|---|
| `failOpenRecordingSessions` | Passe `failed` toute séance de l'ancien mode (sans `kind`) encore `pending` ou `active`, avec `endReason = "legacy mode retired"` et `endedAt`. Les séances d'enregistrement finies et toutes les séances d'entraînement ne sont pas touchées ; les lignes gardent leur forme (pas de `kind` ajouté). Le statut de la machine n'est pas écrit : le prochain heartbeat le recalcule. | `{machinesChecked, sessionsFailed}` |
| `removeMachineConfig` | Retire le champ `config` de chaque machine, supprimées comprises. Rien d'autre ne change. | `{machinesChecked, machinesCleared}` |

Les deux sont idempotentes : une seconde exécution renvoie zéro. Pourquoi la
première compte : une séance d'enregistrement restée `pending` refuse tout
lancement auto sur sa machine (« A session is already waiting for this
machine ») et empêche de supprimer cette machine.

**Les compteurs à vérifier**, sur chaque déploiement, en gardant la sortie des
quatre exécutions :

| Compteur | Ce qu'il doit valoir |
|---|---|
| `machinesChecked`, dans les deux résultats | le nombre de documents de la table `machines` du déploiement visé, machines supprimées comprises : la migration a lu toutes ses machines. Le compter à part, avec la commande donnée sous ce tableau. |
| `sessionsFailed`, première exécution | le nombre de séances de l'ancien mode restées `pending` ou `active`. À noter : rien ne permet de le retrouver ensuite. |
| `machinesCleared`, première exécution | le nombre de machines qui portaient `config`. En production, où le schéma en service (branche `main`) exige ce champ sur chaque machine, il doit être égal à `machinesChecked`. |
| `sessionsFailed` et `machinesCleared`, **seconde exécution** | `0`. Relancer chaque mutation une fois : ce zéro est la preuve que la première a tout traité. |

Pour compter les machines d'un déploiement, précéder la commande de la même
variable `CONVEX_DEPLOY_KEY` qu'à l'étape 2 pour la production :

```bash
npx convex data machines --format jsonLines --limit 10000 | wc -l
```

`npx convex data machines` seul n'affiche que les 100 documents les plus
récents : il ne compte pas au-delà. Avec `--format jsonLines` chaque document
tient sur une ligne, et `--limit` repousse la borne. Si le nombre obtenu est
égal à la limite donnée, la relever et recommencer.

Le champ `machines.config` reste déclaré, facultatif, dans le schéma : le
retirer tant que des documents le portent ferait échouer `convex deploy`. Une
fois `removeMachineConfig` passée sur **tous** les déploiements, le champ peut
quitter `convex/schema.ts`. La valeur `recording` de `sessions.kind`, elle, a
pu partir tout de suite : aucun code ne l'a jamais écrite.

`convex/legacyRecordingRetired.test.ts` exerce ces deux mutations en mémoire
(`npm run test:convex`). Ce n'est pas une preuve sur des données réelles.

### Migration des clés machine (ANH-165)

**Pas encore exécutée, sur aucun déploiement.** Une mutation interne de
`convex/migrations/machineApiKeys.ts`, à lancer à la main, une fois par
déploiement, **après** avoir déployé ce code :

```bash
npx convex run migrations/machineApiKeys:moveMachineKeysToTable
```

Comme les lignes de la section précédente, celle-ci vise le déploiement que
désigne la clé `CONVEX_DEPLOY_KEY` chargée ; pour la production, la précéder
de la clé de production, après l'avoir lue comme à l'étape 1 ci-dessus.

Elle déplace la clé unique de chaque machine (`machines.apiKey`,
`machines.apiKeySelector`) dans `machine_api_keys`, sans expiration ni auteur,
puis vide ces deux champs. **Aucune machine n'est coupée, ni avant ni après** :
tant que la migration n'a pas tourné, une clé encore dans `machines` ouvre les
routes comme avant (§6) ; après, la même clé les ouvre comme ligne de la
table. Le déploiement du code ne demande donc pas de fenêtre d'interruption.
Une valeur qui n'est pas un vérificateur avec son sélecteur (les encodages
réversibles refusés depuis ANH-121) n'ouvre rien : elle est effacée sans être
déplacée, et sa machine a besoin d'une nouvelle clé (fiche de la machine,
**Nouvelle clé**).

Résultat : `{machinesChecked, keysMoved, legacyDiscarded}`. `machinesChecked`
doit valoir le nombre de documents de `machines` (commande de la section
précédente). La migration est idempotente : une seconde exécution renvoie
`keysMoved: 0` et `legacyDiscarded: 0`, preuve que la première a tout traité.
Créer une clé sur une machine pas encore migrée y déplace d'abord sa clé, qui
reçoit le chevauchement comme les autres.

Une fois la migration passée sur **tous** les déploiements, les champs
`machines.apiKey` et `machines.apiKeySelector`, l'index `by_apiKeySelector` et
le repli de `convex/lib/machineAuth.ts` peuvent être retirés (pas fait ici).
`convex/machineKeys.test.ts` exerce la migration en mémoire ; ce n'est pas une
preuve sur des données réelles.

### Rotation d'une clé machine (ANH-165)

La procédure (nouvelle clé avec chevauchement, installation sur le Pi,
révocation, clé compromise, commandes `createKeyByOperator` et
`revokeKeyByOperator` sans tableau de bord) et le résultat de son essai sur le
déploiement de développement sont dans
[secrets.md, 3.1](secrets.md#31-clé-de-la-machine).

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
qui dépend d'une organisation est refusé pour tout le monde (code
`no_organization`, « Votre compte n'appartient à aucune organisation »), **y compris une demande d'arrêt à
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
   Ne donner les permissions **système** des membres (`org:sys_memberships:read`,
   `org:sys_memberships:manage`) qu'à `org:admin` : le panneau « Gérer » du
   sélecteur d'organisation du site les applique, et un gestionnaire ou un
   patient qui les aurait verrait le nom et l'adresse de tous les membres.
   Désactiver la création d'organisations par les utilisateurs : sinon
   n'importe quel compte crée la sienne et en devient `org_admin`.
3. Créer l'organisation « Anheart » et noter son identifiant (`org_…`). N'y
   ajouter personne, pas même un propriétaire choisi à la création, avant que
   le webhook soit configuré (C, étape 3) : Clerk ne rejoue pas pour un
   nouvel endpoint les événements produits avant lui, et Convex ne connaîtrait
   pas ces appartenances.
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

**C. Dans Convex (le site sait choisir une organisation depuis ANH-187)**

**Avant de configurer le webhook décrit plus bas.** Un webhook actif avant
`ANHEART_ORG_ID` reçoit l'organisation Anheart de Clerk comme une organisation
quelconque et lui crée sa propre ligne, distincte de l'organisation par
défaut qui porte les données existantes (avec ses membres si on en a ajouté
dans Clerk). La migration de l'étape 2 s'arrête alors sans rien écrire (« Two Anheart organisations… ») au lieu de
relier la mauvaise ligne : les machines, séances et comptes existants restent
dans l'organisation par défaut. Pour en sortir :
[« Sortir de deux organisations Anheart »](#sortir-de-deux-organisations-anheart).
Ordre complet, côté site compris :
[tableau-de-bord.md, « Amorçage d'un déploiement »](tableau-de-bord.md#amorçage-dun-déploiement).

1. Définir la variable d'environnement du déploiement :
   ```bash
   npx convex env set ANHEART_ORG_ID org_…
   ```
   À partir de là, tout jeton sans organisation est refusé (le site affiche
   « Choisissez une organisation pour continuer. »), et seuls les
   `org:admin` de cette organisation sont admins Anheart.
2. Relancer la migration (même commande qu'en A) : elle relie l'organisation
   par défaut à cette organisation Clerk (`clerkOrgId`) au lieu d'en créer une
   autre. Sans cela, les jetons de l'organisation Anheart sont refusés (code
   `organization_not_synchronized`, « Cette organisation n'est pas encore
   connue du serveur »). Vérifier qu'**une seule**
   organisation porte ce `clerkOrgId` (c'est lui qui compte : le slug peut
   devenir celui de Clerk, par exemple après une réparation) et qu'aucune n'a
   le slug `anheart` sans `clerkOrgId`.
3. Configurer le webhook ([ci-dessous](#webhook-clerk-vers-le-miroir)), puis
   seulement inviter le premier admin dans l'organisation Anheart depuis
   Clerk, avec le rôle `org:admin`, et faire entrer chaque compte existant
   dans son organisation Clerk avec son rôle. Jusque-là, un compte existant
   n'a plus accès à rien : enchaîner les étapes 1 à 3 dans une même fenêtre
   annoncée.

Ne définir `ANHEART_ORG_ID` qu'une fois en ligne la version du site qui porte
le sélecteur d'organisation (ANH-187) : sans organisation active dans sa
session Clerk, un utilisateur n'a plus accès à rien, et le site le lui dit en
lui proposant d'en choisir une. Procédure d'amorçage pas à pas, côté site :
[tableau-de-bord.md §2](tableau-de-bord.md#amorçage-dun-déploiement).

Les organisations clientes arrivent par le webhook décrit ci-dessous ; ni la
migration ni la première connexion ne les créent à partir d'un jeton. Tant que
l'événement n'a pas été reçu, une organisation inconnue reste refusée.

#### Sortir de deux organisations Anheart

Si le webhook a reçu l'organisation Anheart de Clerk avant que
`ANHEART_ORG_ID` soit définie, il lui a créé sa propre ligne, et la migration
s'arrête (« Two Anheart organisations… »). La réparation se fait en deux
temps, `ANHEART_ORG_ID` étant posée. C'est une mutation **interne** (le site
ne peut pas l'appeler).

**1. Décrire, sans rien écrire** :

```bash
npx convex run migrations/multiOrganization:repairDuplicatedAnheartOrganization '{}'
```

La réponse `status: "dry_run"` dit ce que la réparation ferait :

- `absorbs` : la ligne qui serait absorbée, celle dont `clerkOrgId` vaut
  `ANHEART_ORG_ID` (`name`, `slug`, `clerkOrgId`, nombre d'appartenances
  `memberships` et d'admins actifs `admins`) ;
- `links` : l'organisation par défaut qui serait reliée à Clerk (`name`,
  `slug`, en principe « Anheart » et `anheart`) ;
- `movedMemberships`, `mergedMemberships`, `movedUsers` : les nombres
  d'appartenances déplacées ou fusionnées et de comptes déplacés.

**Avant d'appliquer, vérifier dans le tableau de bord Clerk que `absorbs`
est bien l'organisation Anheart** : même nom, même slug, même identifiant,
et des admins qui sont ceux d'Anheart. Rien dans la base ne distingue
l'organisation Anheart d'une organisation cliente : si `ANHEART_ORG_ID`
désigne par erreur une cliente déjà reçue par le webhook, la migration
s'arrête de la même façon, et appliquer la réparation absorberait ce client,
dont l'identifiant Clerk désignerait ensuite toutes les données d'avant la
migration, données de santé comprises. Dans ce cas, ne pas appliquer :
corriger `ANHEART_ORG_ID` (`npx convex env set ANHEART_ORG_ID org_…`) et
relancer la description.

**2. Appliquer**, en recopiant exactement le nom que la description a donné
dans `absorbs.name` :

```bash
npx convex run migrations/multiOrganization:repairDuplicatedAnheartOrganization '{"apply": true, "expectedName": "<absorbs.name>"}'
```

Sans `expectedName`, ou avec un autre nom, elle refuse sans rien écrire.
Sinon, en une seule transaction :

- chaque appartenance de cette ligne passe à l'organisation par défaut. Un
  compte qui y avait déjà la sienne (la migration en a donné une à chaque
  compte existant) n'en garde qu'une, qui prend le rôle, l'état et les
  identifiants envoyés par Clerk : à partir de `ANHEART_ORG_ID`, c'est Clerk
  qui fait foi sur les rôles ;
- un compte que le webhook a créé dans cette ligne prend l'organisation par
  défaut pour organisation principale ;
- la ligne est supprimée et l'organisation par défaut est reliée à
  l'organisation Clerk (`clerkOrgId`), avec le nom, le slug et les dates que
  le webhook avait reçus. Son slug devient donc celui de Clerk, qui peut ne
  pas être `anheart`.

Elle répond `status: "repaired"`, avec l'`organizationId` de l'organisation
par défaut et les mêmes trois nombres. Relancer ensuite la migration (même
commande qu'en A) : elle doit répondre avec le même `organizationId`.
Vérifier dans le tableau de bord Convex qu'une seule organisation porte ce
`clerkOrgId` et qu'aucune n'a le slug `anheart` sans `clerkOrgId`. Les
webhooks suivants écrivent dans l'organisation par défaut ; une livraison
plus ancienne que ce qu'elle a repris ne change rien.

La description comme l'application refusent, sans rien écrire :

- si `ANHEART_ORG_ID` n'est pas définie ;
- si Clerk a supprimé l'organisation que nomme `ANHEART_ORG_ID` : vérifier la
  variable ;
- si la ligne du webhook porte d'autres données : machine, séance, télémétrie
  ou événement de séance, programme, droit de lancement, lien entre un
  gestionnaire et une machine ou un patient, invitation de patient. Le message
  nomme la table. Quelqu'un a alors travaillé dans cette organisation, et
  rattacher ou supprimer ces lignes demande de les examiner une à une : aucune
  procédure n'est écrite pour ce cas, qui revient au responsable produit.

Relancée après une réparation, ou quand l'ordre de l'amorçage a été suivi,
elle répond `status: "nothing_to_repair"` et ne change rien. Comme la
migration, elle n'a tourné qu'en mémoire, dans les tests
(`convex/multiOrganizationMigration.test.ts`), sur aucun déploiement.

### Webhook Clerk vers le miroir

`POST /clerk/webhook` reçoit `organization.created`, `organization.updated`,
`organization.deleted`, `organizationMembership.created|updated|deleted`
et `user.deleted`.
La route vérifie la signature Svix HMAC-SHA256 sur le corps brut avec WebCrypto,
avant de décoder le JSON. Les trois en-têtes `svix-id`, `svix-timestamp` et
`svix-signature` sont requis ; l'horodatage de livraison doit être à moins de
cinq minutes de l'horloge du serveur. Le corps est limité à 1 Mio.

Un événement déjà traité (`clerk_webhook_events`, index `by_event_id`) est
acquitté sans nouvelle écriture. Les dates de modification et d'événement
empêchent une livraison ancienne d'écraser un état plus récent. Une organisation
supprimée garde une trace de suppression et ses membres deviennent inactifs ;
une livraison tardive ne la recrée pas. Une nouvelle invitation peut réactiver
une appartenance avec un nouvel identifiant Clerk. Un événement d'appartenance
peut créer l'organisation et le compte manquants ; il ne remplace pas les champs
communs d'un compte existant. L'organisation Anheart par défaut est reliée quand
l'identifiant reçu correspond à `ANHEART_ORG_ID`.

**À configurer par le responsable du déploiement**, une fois `ANHEART_ORG_ID`
posée et la migration relancée (C ci-dessus) : créer un endpoint Clerk
vers l'URL `.convex.site/clerk/webhook` de l'environnement choisi, activer
les sept événements ci-dessus et poser son secret dans
`CLERK_WEBHOOK_SIGNING_SECRET` sur le même déploiement Convex. Ne pas utiliser
le secret de production pour la recette. Envoyer ensuite un événement signé
de test et vérifier les lignes du miroir, puis rejouer la livraison : une seule
ligne d'événement doit subsister. Sans secret, la route répond 503 ; signature
refusée, 401 ; charge illisible, 400 ; événement accepté ou ignoré, 204.
N'ajouter de membre à l'organisation Anheart dans Clerk qu'une fois l'endpoint
créé : Clerk ne lui envoie pas les événements produits avant lui (B, étape 3).

**Délai des droits portés par un jeton** : le rôle du jeton Clerk vérifié reste
l'autorité de l'appelant jusqu'à son expiration. La réception d'un retrait dans
le miroir ne révoque pas à elle seule un jeton déjà émis. Le retrait est immédiat
pour le roster et les nouveaux liens de séance, qui lisent les appartenances
actives ; une appartenance révoquée n'est pas réactivée par une reconnexion avec
un ancien jeton. Une organisation supprimée est refusée dès réception.

Poser également `CLERK_SECRET_KEY` dans les variables Convex, pour l'envoi des
invitations et la vérification des adresses par l'API Clerk. Cette clé appartient
à la même instance que le webhook et l'émetteur JWT. Sans réponse vérifiable,
une création ou mise à jour de membre répond 503 et attend une nouvelle livraison.
Les retraits ne dépendent pas de cet appel externe.

Pour une recette, utiliser l'instance Clerk de développement et une adresse
`qa-…+clerk_test@example.com` : inviter depuis Patients, accepter l'invitation,
ouvrir « Mes machines » et vérifier que l'identifiant Convex est celui de la
fiche pré-créée. Rejouer la livraison pour vérifier l'absence de doublon.
Configurer aussi l'URL d'accueil et les redirections du portail Clerk pour
l'environnement du site. Sans cette configuration, le portail de développement
confirme l'acceptation mais affiche « Start building » ; le patient doit alors
revenir sur le site et s'y connecter pour ouvrir « Mes machines ».

`createPatient` passe de mutation à action : déployer Convex et le site pendant
la même fenêtre ANH-82, puis recharger les anciens onglets. Un ancien site ne
peut pas appeler cette action comme une mutation. Contrôler les éventuelles
anciennes fiches en conflit avant cette opération ; ne pas les fusionner sur
la seule égalité d'adresse.

Références du protocole : [vérification Svix](https://docs.svix.com/receiving/verifying-payloads/how-manual)
et [ordre des événements Clerk](https://clerk.com/docs/guides/development/webhooks/overview).

---

## 9. Défauts connus et reste à faire

### Migration des credentials avec ANH-82 : opération à réaliser

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
   correctif et créer une nouvelle clé pour chaque machine dont la valeur est
   historique (fiche de la machine, carte **Clés de la machine**, **Nouvelle
   clé**), avec un compte d'admin (ANH-165).
   Transmettre le résultat unique au responsable de la machine par le canal de
   provisionnement approuvé, puis mettre à jour sa configuration locale. En cas
   de perte du résultat, créer une autre clé ; aucune récupération n'est possible.
4. Vérifier un heartbeat avec la nouvelle clé et **401** avec l'ancienne.
   Traiter aussi les machines supprimées/désactivées : leur valeur historique
   doit disparaître sans les réactiver ni distribuer leur credential. Depuis
   ANH-165, la migration `moveMachineKeysToTable`
   ([§8](#migration-des-clés-machine-anh-165)) efface toute valeur historique,
   machines supprimées et désactivées comprises, et compte les valeurs effacées
   (`legacyDiscarded`) sans les exporter.
5. En cas d'échec, laisser la synchronisation interrompue ou revenir uniquement
   à une version qui conserve ce schéma et ce vérificateur. Ne jamais restaurer
   la base64 réversible, les anciennes clés ou l'ancien code d'authentification.
   Reprendre le provisionnement, créer une nouvelle clé si nécessaire. Les sauvegardes
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
| 4 | ~~Séances locales sans pratiquant.~~ **Corrigé dans le logiciel par ANH-84.** La console envoie `userId` (et `subjectLabel` tant qu'elle a le nom en mémoire) pour une séance lancée à la machine avec un passager ; une séance de banc n'en porte pas, avec `occupancy = bench`. | Une séance lancée à la machine apparaît au nom du passager choisi, s'il a toujours le droit à sa déclaration (ANH-180). Non vérifié sur un déploiement : effet en production avec le redéploiement ANH-82. |
| 5 | ~~`/api/machine/roster` inutilisé.~~ **Corrigé dans le logiciel par ANH-84.** | La console demande la liste à son démarrage puis toutes les 60 s au repos ; elle y prend le passager, sa FC max et son année de naissance. |
| 6 | **Libellés d'actions de sécurité.** Le Pi envoie `freeze`, `quick_stop`, `go_silent` ; les traductions du site connaissent `hold`, `stop`, `estop`. Le commentaire du schéma cite aussi `"hold"`. | Ces trois actions s'affichent en valeur brute. |
| 7 | **Pas d'ECG pour les séances d'entraînement.** La console locale n'envoie que la télémétrie à 1 Hz. | Pas de tracé ECG pour une séance auto/manuelle. Son résumé et son rapport existent depuis ANH-89, calculés sur la télémétrie puis sur l'enregistrement déposé, sans ECG. |
| 8 | **Logiciel d'invitation et d'amorçage ANH-157.** | Action Clerk, liaison par webhook à adresse vérifiée et premier admin `org:admin`. La configuration et la recette de l'environnement sont requises avant mise en production (§8). |
| 9 | **Corrigé par ANH-148 : historique borné.** | Heartbeats conservés 24 h, santé agrégée par 5 min conservée 90 jours. Purge quotidienne et par machine active, sans purge de télémétrie. |
| 10 | **Tests automatisés Convex présents**, dont authentification machine et confidentialité des séances ; déploiement ANH-82 encore requis. | `npm run test:convex` rejoue les scénarios synthétiques ; ce n'est pas une preuve de production. |
| 11 | **Séance orpheline.** Si la console s'arrête pendant une séance (arrêt du conteneur, coupure), elle n'envoie pas `/training/end` à ce moment-là. Trouvé à l'essai du 1er octobre 2026. Une console qui synchronise par relecture du journal envoie cette fin à son démarrage suivant, en relisant l'enregistrement ([raspberry-pi.md, 8.4](raspberry-pi.md#84-après-un-redémarrage-de-la-console)). | Jusqu'à ce démarrage, la séance reste `active` dans Convex, au plus 10 min sans nouvelles : elle passe alors `interrupted` (« interrompue, fin inconnue », [Tâche planifiée](#7-tâche-planifiée), ANH-216), et le reste si la console ne redémarre pas, ou si l'enregistrement est introuvable ou ne se lit pas ([raspberry-pi.md, 8.7](raspberry-pi.md#87-limites)). Aucune fonction Convex ne termine une séance active sans la fin envoyée par la console. La machine repasse `online` au redémarrage. Ce marquage attend le redéploiement ANH-82 pour la production. |

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
| [`convex/auditLog.test.ts`](../convex/auditLog.test.ts) | Métadonnées et identifiant de requête, valeurs fermées et clés de détails, listes d'identifiants bornées ; énumération des accès au littéral `audit_log` (seul ajout par `audit()`, modules de lecture limités aux queries, aucun `patch`, `replace` ou `delete`), refus des assertions de type sur les identifiants d'écriture, absence de purge et classification des mutations à toute profondeur, migrations comprises ; limites décrites au §2. |
| [`convex/auditCompleteness.test.ts`](../convex/auditCompleteness.test.ts) | Un scénario par mutation sensible, passant par les fonctions enregistrées, avec effets et lignes attendues dans la même transaction ; toutes les actions observées, refus et rejeux sans ligne, arrêt actif et annulation en attente, absence des valeurs privées testées, source `operator` limitée aux chemins internes prévus et absente des routes HTTP. |
| [`convex/audit.test.ts`](../convex/audit.test.ts) | Lecture réservée aux administrateurs, séparation des organisations et des lignes globales, filtres combinés, bornes de période, pagination après filtrage, priorité de chaque index et libellés sans e-mail, y compris les références disparues. |
| `convex/test.setup.ts` | Fabriques partagées : un monde de **trois organisations** (Anheart et son admin ; le centre A avec son admin, deux gestionnaires, trois patients, deux machines ; le centre B avec son admin, un gestionnaire, un patient, une machine), un monde de machines avec de vraies clés pour les routes HTTP, et un monde « d'avant les organisations » pour la migration. Nom à deux points : non déployé. |
| `convex/authorization.matrix.ts` | La **matrice d'autorisation** : la politique de chaque fonction publique, une ligne par rôle, par côté quand l'accès dépend de la propriété, et par organisation. Nom à deux points : non déployé. |
| `convex/authorization.matrix.test.ts` | Parcourt la matrice : un test par cellule. |
| `convex/organizations.test.ts` | D'où viennent l'organisation et le rôle d'un appel : revendications acceptées et refusées, admin Anheart, premier passage d'un compte, et la transition avant et après `ANHEART_ORG_ID`. |
| `convex/organizationChoice.test.ts` | Ce que le site lit pour travailler dans une organisation (ANH-187) : raison du refus de l'organisation de l'appel, `org_id` du jeton, rôles portés par Clerk ; organisations proposées à la création d'une machine ; gestionnaires d'une organisation et leurs liens comptés dans celle-ci. |
| `convex/organizationIsolation.test.ts` | Ce que la matrice n'exprime pas : l'autre sens (le centre A sur le centre B), la même réponse pour un identifiant d'une autre organisation et pour un identifiant inconnu, les appels qui mêlent deux organisations dans leurs arguments, un compte membre de deux organisations (liens patient et machine fixés dans une seule), ce qu'une machine écrit, les lignes sans organisation. |
| `convex/multiOrganizationMigration.test.ts` | La migration : rattachement, appartenances, idempotence, lots, liaison avec `ANHEART_ORG_ID`. |
| `convex/httpRoutes.test.ts` | Les 10 routes machine de `http.ts` : corps mal formés, idempotence, liaison ressource-machine, filtrage des séances. |
| `convex/journalTrace.test.ts` | Le test d'acceptation de la synchronisation, côté Convex : il rejoue, dans l'ordre et aux mêmes instants, **les requêtes que la console du Pi a réellement envoyées** pendant son propre test d'acceptation (`contracts/fixtures/journal-sync-trace.json` : séance, coupure réseau à 3 min, console tuée à 5 min, retour du réseau à 8 min, machine datée de 1970). Convex doit répondre à chacune comme la trace l'a noté, et détenir à la fin chaque point 1 Hz et chaque événement une seule fois, et la fin avec sa raison. Le test du Pi échoue si le fichier n'est plus ce que la console envoie. |
| `convex/journalSync.test.ts` | Ce que Convex fait d'une séance envoyée tard ou deux fois : une ligne par point et par événement quel que soit le nombre d'envois, la route des événements et ses tailles. Puis deux consoles rejouées. Celle qui ne dit pas son âge (contrat 1.0) : rien ne change pour elle, horloge reculée de 10 min en cours de séance (tout est stocké), machine datée de 1970 déclarée en retard (jamais en direct, aucun point refusé). Celle qui dit son âge : borne de cohérence sur son propre axe, dates du serveur, données tardives lues comme tardives, rien de perdu. |
| `convex/legacyRecordingRetired.test.ts` | Retrait de l'ancien mode ECG : routes disparues (404), modules réduits à leurs lectures, aucune écriture dans `ecg_data` ni `session_summaries`, rien de planifié en fin de séance que le résumé provisoire d'ANH-89, et les deux mutations de migration. |
| `convex/sessionSummary.test.ts` | Les chiffres d'un résumé (ANH-89) sur des enregistrements de fixture écrits comme la console les écrit : durée, FC absente ou présente, temps en zone et ses bornes, vitesse, g aux deux rayons avec et sans géométrie, fenêtres de variation de g et chaque raison de ne pas juger un tic, verdicts et règle de fin, colonnes trouvées par leur nom, lignes illisibles, fichiers refusés. |
| `convex/sessionReports.test.ts` | Le résumé de bout en bout (ANH-89) : provisoire à la fin, final après le dépôt des trois fichiers, jamais remplacé par le provisoire, idempotent, enregistrement illisible, séance non terminée ou de l'ancien mode ; et qui lit le résumé et l'historique d'un pratiquant. |
| `convex/rateLimits.test.ts`, `convex/rateLimitsSource.test.ts` | La limitation de débit et la taille des corps (ANH-166) : chaque limite du contrat partagé sur sa route (sa réserve, puis 429 avec l'attente, puis de nouveau acceptée, une autre machine jamais retenue), rien d'écrit pour un heartbeat refusé, la limite avant le corps, la demande d'arrêt jamais limitée, **1000 heartbeats en 10 s** limités sans erreur (2 acceptés, 998 refusés 429), les tailles (16 Kio exactement accepté, un octet de plus refusé, octets et non caractères, `Content-Length` trop grand ou trop petit), 601 points refusés avant d'en lire un, le plus grand lot valide accepté ; par compte, 60 écritures puis `rate_limited`, un refus rend son jeton, 5 lancements par minute, l'arrêt jamais limité, et **chaque** mutation et action publique de la matrice refusée une fois les écritures de son acteur épuisées, l'arrêt excepté. Le second fichier remplace `contracts/machine-api.json` par un autre et vérifie que les limites et les tailles le suivent, et que le module refuse de se charger sans la limite ou la taille d'une route qu'il borne. |
| `convex/contract.test.ts`, `convex/contractSource.test.ts` | Le contrat versionné (ANH-133) : 426 sur chaque route sans majeure servie sauf celle de la demande d'arrêt (qui répond avec la seule clé, sans rien écrire d'autre que la dernière utilisation de la clé), clé vérifiée avant le contrat, rien d'écrit de la machine, de ses séances ni de ses heartbeats pour une requête refusée (la dernière utilisation de la clé, elle, est écrite pour toute requête dont la clé est acceptée, au plus une fois par minute : §6), `server_contract_version` dans le poll, un code stable pour chaque refus, versions stockées à chaque heartbeat. Le second fichier remplace `contracts/machine-api.json` par un autre contrat et vérifie que le code le suit : la valeur est bien lue dans ce fichier. |
| `convex/crons.test.ts` | Le cron `check-offline-machines` (celui des clés expirées est dans `convex/machineKeys.test.ts`). |
| `convex/interruptedSessions.test.ts` | La séance sans nouvelles (ANH-216) : marquée `interrupted` 10 min après la dernière nouvelle et pas avant, une seule fois ; marquée aussi quand la machine répond sans la nommer ; un lancement daté de sa confirmation ; ni l'ancien mode, ni `pending`, ni une séance finie touchés. Jamais marquée tant que sa machine la nomme (trois heures) ni pendant que ses points ou ses événements arrivent ; toujours `active: true` pour la machine ; rendue `active` par le heartbeat qui la nomme ; arrêt demandé transmis ; fin tardive acceptée avec sa date, mesures tardives stockées. Une machine ne ranime ni ne couvre la séance d'une autre ; lecture et arrêt réservés à ceux d'une séance active, jamais à un compte du centre B. |
| `convex/machineEdit.test.ts` | Ce que fait le formulaire de machine pour un gestionnaire : `machines.updateMachine` enregistre le nom et le lieu sans toucher aux liens, et `machines.assignMachineToGestionnaires` reste réservé à l'admin (ANH-155). |
| `convex/dashboard.test.ts` | Les compteurs du tableau de bord (ANH-159) au-delà de la matrice : ce qu'aucun chiffre ne compte (membre retiré, machine supprimée, séance close, autre organisation), ce que l'admin Anheart seul compte (lignes sans organisation, machine restaurée, membre de deux organisations compté dans chacune), de quoi sont faits les chiffres d'un gestionnaire, et une machine silencieuse que sa fiche dit encore en ligne, remise au site avec son dernier signal et `serverNow` pour qu'il la juge hors ligne. |
| `convex/completeness.test.ts` | Échoue si une fonction publique ou une route n'a pas de cellule de matrice, si une fonction n'a aucune cellule appelée depuis une autre organisation, ou si une fonction qui prend un identifiant n'a pas de cellule `foreign`. |
| `convex/machineAuth.test.ts`, `convex/machineCredential.test.ts` | Authentification et clés machine (ANH-121, complétés par ANH-132). Leur monde est un déploiement mono-organisation après migration, sans revendication d'organisation. |
| `convex/machineKeys.test.ts` | Les clés d'une machine (ANH-165), sur le même monde : première clé dans la table et rien sur la machine ; rotation (les deux clés valides pendant le chevauchement, l'ancienne refusée à la milliseconde de sa fin dans le test en mémoire, sans cache de Convex, heartbeats continus à travers le changement de clé, chevauchement 0, expiration jamais allongée, autre machine intacte) ; tâche des clés expirées ; révocation immédiate sur chacune des routes machine, avant la limite de débit ; expiration et ses bornes ; machine supprimée ou désactivée ; `lastUsedAt` écrit au plus une fois par minute et l'authentification qui ne fait que lire ; la liste de la fiche sans aucun secret ni journal qui en contienne ; les deux fonctions de ligne de commande ; la migration (clé historique acceptée avant, déplacée, idempotente, valeur réversible effacée, table seule juge d'un sélecteur, clé pas encore déplacée prise dans une rotation). |
| `convex/trainingPrivacy.test.ts`, `convex/sessions.test.ts` | Confidentialité des mesures live et des séances (ANH-71), sur le même monde mono-organisation migré. |
| `convex/sessionLists.test.ts` | Les listes de séances (ANH-158) : 300 séances sur les trois machines des deux centres, commencées dans un autre ordre que celui de leur écriture, certaines au même instant, d'autres sans organisation ou montées par un pratiquant de l'autre centre. Chaque acteur les lit par pages de 50 et reçoit exactement celles que la règle de §3 lui ouvre, du début le plus récent au plus ancien : sans filtre, par statut, par pratiquant, par machine ; puis `listSessions` et `getCompletedSessionsForUser`. Et ce que la règle écarte : une séance dont la machine est d'une autre organisation, une machine gérée dans une autre organisation ou disparue. |
| `convex/gestionnaireMachines.test.ts` | `machines.setGestionnaireMachines` : deux gestionnaires sur une machine (retirer l'un ne touche pas l'autre), ajout, liens existants conservés, refus sans écriture (ANH-154). Machine supprimée : ni listée ni comptée sur la fiche, lien gardé qu'elle soit nommée ou non, jamais liée ; `assignMachineToGestionnaires` et `createMachine` : une ligne par gestionnaire même envoyé deux fois, lignes gardées, propriétaire = premier gestionnaire gardé (ANH-194). |
| `convex/errors.test.ts` | La table `convex/lib/errors.ts` : chaque code a sa traduction française et anglaise, aucune traduction sans code, aucun code levé hors de la table, plus aucune `Error` simple dans `users.ts` et `machines.ts` hors du heartbeat interne (ANH-194) ; la connexion et les refus d'organisation par code, plus aucune phrase dans `softwareReleases.ts`, `notifications.ts` et `training.ts`, qui ne construit plus aucune `ConvexError` de lui-même (ANH-90). |
| `convex/notifications.test.ts` | Les alertes aux gestionnaires (ANH-90) : qui les reçoit (gestionnaire de la machine et admin de son organisation, jamais un gestionnaire inactif ou rétrogradé, un compte supprimé dans Clerk, une machine supprimée ou sans organisation), chaque événement qui en écrit une et ceux qui n'en écrivent pas (verdict qui régule, `cleared`, séance terminée, événement renvoyé, fin renvoyée), le lancement refusé avant son départ (tâche planifiée, route de départ, expiré par un nouveau lancement ; la tâche n'écrit elle-même aucune alerte, elle en planifie une), le regroupement dans l'alerte non lue, ce que lit l'appelant (plus rien d'une machine qu'il ne gère plus, ni séance ni raison qu'il ne peut lire, nombre plafonné), les choix d'e-mail et leurs valeurs par défaut, l'e-mail mis en file une seule fois et **sans donnée de santé** (ni FC, ni nom, ni identifiant de séance, ni règle du verdict), la purge à 90 jours et la suppression avec le compte. Toutes les horloges sont figées : les livraisons planifiées sont lancées une à une, et le lot qui enverrait l'e-mail ne tourne jamais. |
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

- `refuse` : l'appel lève une erreur, avec, si utile, ce qu'elle doit dire :
  le code stable pour `users.ts`, `machines.ts`, les contrôles de rôle et le
  code `rate_limited` d'un compte au-delà de sa limite (table
  `convex/lib/errors.ts`, [§5](#5-autres-fonctions-publiques) ; limites en
  section 6), un extrait du message anglais pour les autres ; les routes
  machine ont leurs propres codes, section 6 ;
- `success` : l'effet ou la donnée renvoyée est vérifié ;
- `empty` : la requête renvoie `null`, `[]` ou une page vide (pas d'accès, rien
  n'est exposé) ;
- `filtered` : la liste renvoyée contient la donnée du demandeur et exclut
  celle des autres.

Une cellule marquée `knownDefect` est un **défaut inoffensif connu** : le test
affirme la politique **voulue** et tourne avec `it.fails`, donc il passe tant
que le défaut existe et échoue bruyamment le jour où le comportement est corrigé.
Aucune cellule ne l'est plus. La dernière, `sessions.listSessions` qui
appliquait la limite avant le filtre par droits et pouvait cacher à un patient
sa propre séance, est une cellule ordinaire depuis ANH-158 : avec une limite de
deux et trois séances plus récentes d'un autre pratiquant, le patient et son
gestionnaire voient encore la leur. Celles de
`sessionSummaries.getSummaryWithEcg` ne le sont plus depuis ANH-183 : la
requête renvoyait le document stocké, que son validateur de retour refusait,
et échouait pour tout lecteur autorisé ; elle renvoie maintenant les champs
déclarés.

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
  l'ajouter à la migration si des lignes existent déjà. Exception : une table
  dont chaque ligne n'est lue qu'à travers sa machine, dont l'organisation
  est relue à chaque appel (`machine_heartbeats`, `machine_api_keys`).
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
| Majeure de la machine absente, illisible ou non servie | Convex répond **426** `contract_unsupported` à 9 des 10 routes machine (toutes **sauf** `GET /api/machine/training/status`), sans rien lire ni écrire d'autre que la dernière utilisation de la clé ([§6](#6-routes-http-machine-convexhttpts)). |
| Arrêt venu du tableau de bord | Il traverse toutes les versions : la route `training/status` répond avec la seule clé, et le Pi arrête sa séance sur `stopRequested: true` ou `active: false` d'une réponse de n'importe quelle majeure, version absente comprise. C'est tout ce qu'il retient d'une réponse d'une autre majeure. |
| Majeure du serveur différente de celle de la machine, ou non annoncée | Le Pi **n'arme aucun lancement distant** venu de cette réponse, l'affiche sur la console et renvoie le lancement comme séance échouée (voir [raspberry-pi.md](raspberry-pi.md#14-versions-et-compatibilité)). |

Une évolution compatible (un champ optionnel de plus, un code d'erreur de plus)
monte la **mineure**. Tout ce qui change le sens d'un champ existant, en retire
un, ou rend obligatoire ce qui ne l'était pas, monte la **majeure** : les deux
côtés doivent alors être livrés ensemble, et une machine restée sur l'ancienne
majeure est refusée au lieu d'être comprise de travers.

### Dépôt complet des séances (ANH-130)

`session_record_files` rattache chaque fichier à sa séance, sa machine et son
organisation, avec chemin relatif, identifiant Storage privé, taille, SHA-256,
date d'envoi et verdict de vérification. L'index séance/chemin garantit les
reprises idempotentes. `session_record_uploads` réserve les envois par nonce ;
`session_record_manifest_pages` borne l'inventaire et `record_upload_sweeps`
garde le point de reprise du nettoyage des réponses perdues.

Les trois routes machine du contrat 1.2 utilisent l'authentification machine
existante et refusent une séance non close, étrangère ou supprimée. Le manifeste
doit être vérifié avant les autres fichiers. La vérification asynchrone lit les
octets Storage et compare taille et SHA-256 ; elle n'efface jamais un objet dont
la réservation n'est pas propriétaire. `recordStorage.list` et le téléchargement
JWT `/api/records/download` contrôlent l'utilisateur et l'accès à la machine.
La suppression exige `admin` ou `org_admin`. Quand le manifeste, `ticks.csv`
et `events.jsonl` d'une séance sont tous vérifiés, son résumé final est
calculé ([`session_summaries_v2`](#session_summaries_v2--résumé-dune-séance-dentraînement-anh-89)). Les limites, la reprise, la purge
et les réservations conservées sont détaillées dans
[enregistrement.md](enregistrement.md#dépôt-vérifié-dans-convex).

Une console 1.2 face à un serveur antérieur garde ses fichiers localement lorsque
les routes de dépôt répondent 404. La télémétrie continue selon les capacités
de ce serveur. Aucune confirmation de dépôt n'est fabriquée.

### Ce que chaque mineure ajoute

Les trois plafonds par machine ajoutés par ANH-228 gardent le contrat `1.6`
inchangé : aucun champ ni code nouveau, `429 rate_limited` existe depuis
`1.4`. Une console `1.4` ou plus retient la route avec `PacedTransport` ; une
plus ancienne qui relit le journal, dont `pi-1.0.0`, lit le 429 comme une
réponse retenue. Les versions de développement qui envoyaient de mémoire
sont décrites ci-dessous.

| Mineure | Ajouts, tous compatibles |
|---|---|
| `1.6` | Trois refus à l'armement en **409** : `rider_not_fit`, `fitness_expired`, `consent_missing`. Le poll et la tâche de chaque minute appliquent le même contrôle. Le roster ajoute `fit` (booléen), `fitnessValidUntil` (ms Unix ou `null`), `consent` (booléen) et `fitnessQuestionnaireVersion` (chaîne de la saisie validée qui rend `fit` vrai, sinon `null`), sans réponses ni notes. Leur définition est dans [`contracts/machine-api.json`, entrée `roster`](../contracts/machine-api.json) ; une console qui ne reçoit pas ces champs considère le passager non apte et sans consentement. La porte locale `cleared_to_board` applique ce contrôle avant tout départ avec personne à bord. |
| `1.5` | `POST /api/machine/training/start` peut répondre **409** avec l'un des sept codes de refus à l'armement (`arming_refusals` du contrat partagé) : le lancement est revérifié au moment où la machine l'arme et la séance passe `failed` avec ce code (ANH-144). Le poll n'offre plus un lancement expiré, qui expire dans moins de 45 s, ou que ces contrôles refuseraient. |
| `1.4` | Deux codes d'erreur : `rate_limited` (429, avec `retryAfterMs` et `Retry-After`) sur les routes limitées par machine, et `payload_too_large` (413) pour un corps au-delà de la taille de sa route ou un lot de plus de 600 points ou 200 événements (400 avant). Bornes de `seq` (999 999) et de `t` (entier) d'un événement. La liste des limites et des tailles est dans le contrat partagé (`rate_limits`, `max_body_bytes`). |
| `1.3` | Le heartbeat annonce `server_contract_version`. La console vérifie cette annonce pour son état de liaison, même sans programmes. Une réponse sans version lisible affiche un contrat inconnu. |
| `1.2` | `POST /api/machine/records/upload-url`, `POST /api/machine/records/commit` et `GET /api/machine/records/status` : dépôt complet et vérification des enregistrements clos. Les consoles antérieures restent compatibles mais ne déposent pas les fichiers. |
| `1.1` | La route `POST /api/machine/training/events`. La réponse `{stored, duplicates, rejected}` de `training/telemetry` (au lieu de `{stored}`), qui n'écrit plus deux fois le même point. Les champs facultatifs `startedAt` et `sessionAgeMs` de `training/start`, et `sessionAgeMs` de `training/local` : c'est `sessionAgeMs` qui fait dater la séance par le serveur et borner ses points ([Deux horloges](#deux-horloges)). |
| `1.0` | Le contrat de départ. |

Une console restée en `1.0` fonctionne sans changement avec un Convex `1.1` :
elle n'envoie aucun des champs ajoutés, ne lit pas ceux de la réponse, et
n'appelle pas la route des événements. Ses séances sont datées, stockées et
servies comme avant ; la seule différence est qu'un lot qu'elle renvoie n'est
plus stocké deux fois.

La console de ce dépôt est en `1.6`. Elle connaît les codes de refus à
l'armement et demande le départ après confirmation de son opérateur, avant
d'armer ; elle n'arme que sur un 2xx de `training/start` et n'envoie pas de
fin sur un 409. Une console antérieure, qui arme avant de confirmer, s'arrête
aussitôt sur la rampe ordinaire. Une console `1.4`
face à un serveur `1.5` fait de même : son indicateur de liaison ne
reconnaît pas le code, et montre un instant une réponse inconnue. Face à un
serveur `1.4`, une console `1.5` ne reçoit jamais de 409 : rien ne change pour
elle.

Une console `1.5` face à un serveur `1.6` ignore les nouveaux champs du roster
(`src/roster.py`, `_rider`, lecture par `entry.get` des seuls champs connus).
Sur un 409 dont elle ne connaît pas le code, elle n'arme pas et n'envoie pas
de fin : `_confirm_remote_start` de `src/cloud_sync.py` n'arme que sur `Acked`.
La phrase est générique ; une console `1.6` nomme les trois nouveaux refus.

Une console `1.4` face à un serveur `1.3` : rien ne change
pour elle, il ne répond ni 429 ni 413. Une console `1.3` face à un serveur
`1.4` lit un 429 comme elle l'a toujours lu (réponse retenue, la même requête
refaite 15 s plus tard, rien d'abandonné), sans tenir compte de
`retryAfterMs` ; ses cadences et ses lots sont les mêmes, sauf le rattrapage
de télémétrie (un lot toutes les 2 s), que la réserve de deux lots et l'attente
de 15 s après un 429 ramènent sous la limite. Un 413 ne la touche pas : ses
lots sont sous les tailles.

`pi-1.0.0` (commit `80208a0`, contrat `1.1`) se comporte de même sur un 429.
Pour `training/local`, `training/telemetry` et `training/events`, elle garde
le curseur et refait la même requête après 15 s : rien n'est abandonné.
Pour `profiles`, la révision reste à envoyer et un nouvel essai attend 15 s.
Le heartbeat garde sa cadence de 10 s et `training/poll` celle de 3 s ; la
console ignore `retryAfterMs` et ne retient pas toute une route sur place.
Son rattrapage de télémétrie espace les lots de 2 s au moins, toutes séances
confondues, avec la même attente de 15 s après un 429. Ses lots habituels
(300 points ou 200 événements) restent sous les tailles. Si un lot reçoit
quand même un 413, le code `payload_too_large` lui est inconnu : elle refait
deux essais, espacés de 15 s, puis passe ce lot après le troisième refus.
Un 413 sur le heartbeat laisse partir le suivant à sa cadence habituelle.
Cette console ne dépose pas les fichiers : elle n'appelle ni
`records/upload-url` ni `records/commit`, ajoutées au contrat `1.2`.

Les versions de développement **d'avant ANH-129 lot 2** (`e2457b8`, dont le
parent sur `develop` est `3f50b00`) annonçaient déjà le contrat `1.1`, avec
`VERSION` à `pi-0.0.0-dev`, mais envoyaient la télémétrie de mémoire.
Leur `_flush` abandonne **tout lot refusé, 429 ou 413 compris** ; seule une
absence de réponse conserve le lot. Pendant une séance, elles envoient au
plus un lot toutes les 5 s. Après sa fin, elles envoient jusqu'à un lot de
300 points par séance finie à chaque étape du lien, prévue chaque seconde,
sans espacement commun aux séances. Si ce rattrapage dépasse la réserve et
le débit de deux lots toutes les 5 s, les lots refusés en 429 sont abandonnés :
le tableau de bord perd ces points. En revanche, un 429 sur `training/local`
laisse la déclaration à refaire après 15 s, sans identifiant de séance
acquitté ; sur `profiles`, la révision reste à envoyer, avec un nouvel essai
après 15 s. Cette perte de lots ne concerne aucune console publiée : aucun
tag `pi-*` n'est posé, et `pi-1.0.0`, préparée mais non publiée, inclut déjà
ANH-129 lot 2.

Une console `1.3` face à un serveur antérieur sans version dans
le heartbeat conserve les échanges mais affiche un contrat serveur inconnu.
Déployer Convex avant la console permet de maintenir une indication compatible.
Elle conserve les ajouts de `1.1` : elle envoie les événements,
dit l'âge de ses séances, et compte sur l'idempotence pour renvoyer sans
doublon. Face à un Convex resté en `1.0`, elle fonctionne, moins bien : la
route des événements lui répond 404, les événements restent dus sur son
disque (redemandés une fois par minute tant que la séance s'envoie, puis à
chaque démarrage de la console) ; un lot
qu'elle renvoie après une réponse perdue ou un redémarrage est stocké deux
fois ; et ses séances sont datées par elle, puisque ce Convex ne lit pas
`sessionAgeMs`. Rien n'est perdu ni bloqué. D'où l'ordre conseillé pour cette
mineure : Convex d'abord
([deploiement.md](deploiement.md#36-ce-que-demande-la-synchronisation-par-relecture-du-journal)).

### Matrice de compatibilité

| Version du Pi (`raspberry-pi/VERSION`) | Majeure de contrat | Version Convex minimale |
|---|---|---|
| `pi-0.0.0-dev` (développement, avant la première release) | 1 (contrat `1.1`) | `cloud-0.0.0-dev` (développement) : le code de la branche `develop` qui sert la majeure 1 |
| `pi-1.0.0` | 1 (contrat `1.1`) | `cloud-1.0.0`, première version préparée qui sert la majeure 1 |

Cette matrice est tenue à jour à chaque release (check-list de
[release.md](release.md)) : une ligne par version du Pi préparée pour une
release. Une version est **préparée** quand ses fichiers de version et son
changelog sont écrits, et **publiée** quand son tag est posé sur `main`, ce que
fait la pipeline de release ([release.md](release.md#la-pipeline-de-release)).
La première, `pi-1.0.0` avec `cloud-1.0.0`, a été fusionnée dans `main` le
10 octobre 2026 ; aucun tag n'était posé à cette date : elle est préparée, pas
publiée. La ligne de développement reste pour les consoles construites avant
elle.

Limites à connaître :

- Un Convex antérieur à ce contrat n'annonce pas sa version : un Pi de cette
  branche qui l'interroge n'arme donc **aucun lancement distant** (réponse sans
  version). C'est le cas tant que le nouveau code n'est pas déployé ; mettre
  les deux côtés à niveau relève du redéploiement (ticket ANH-82).
- La version logicielle attendue se renseigne dans `machine_registry`
  (ANH-147). Le serveur compare l'annoncée à l'attendue et au niveau de
  validation de la machine ; le refus d'installation OTA attend ANH-116.
- `medical_parameters_version` et `config_hash` sont acceptés dans le heartbeat.
  Depuis ANH-139, Convex stocke et compare la version médicale annoncée à la
  référence courante, puis l'affiche sur la fiche machine. `config_hash` garde
  son statut : il vaut toujours `null` et n'est pas stocké.

# Le tableau de bord distant (site Next.js)

Le site (`app/`, `components/`, `lib/`, `messages/`) est le **tableau de bord
distant** d'Anheart (la marque affichée est « Gaura »). Il permet de gérer les
comptes et les machines, de **lancer une séance auto**, de la suivre en direct
et de l'arrêter. Il lit et écrit dans Convex (voir [convex.md](convex.md)) ; il
ne parle **jamais** directement au Raspberry Pi.

> **État réel.**
> - Le code se compile : `npx tsc --noEmit -p .` ne signale aucune erreur.
> - `npm run test:lib` exécute les tests unitaires de `lib/` : les règles
>   extraites des fenêtres du site, et le test de non-régression du retrait de
>   l'ancien mode d'enregistrement ECG
>   ([portée et limites](framework-de-test.md#tests-unitaires-du-site)).
>   La fraîcheur de l'état en direct (§6) a des tests unitaires, sans
>   navigateur : `npm run test:site`
>   ([portée et limites](framework-de-test.md#fraîcheur-de-létat-en-direct-anh-160)).
>   La même commande exécute les tests du retour des mutations
>   ([§6](#messages-de-succès-et-déchec)).
>   Chaque composant, chaque hook et chaque règle de `lib/` a ses tests, sans
>   navigateur, et la CI exige 80 % de lignes et de branches couvertes sur ces
>   trois dossiers
>   ([seuils](framework-de-test.md#seuils-de-couverture-de-convex-et-du-site-anh-203),
>   [portée et limites](framework-de-test.md#tests-des-composants-du-site-sans-navigateur-anh-203)).
>   La suite navigateur de bout en bout du tableau de bord reste ANH-83.
>   Le 2 octobre 2026, toutes les pages du tableau de bord
>   ont été **ouvertes à la main dans un navigateur**, en local, contre le
>   Convex de développement, avec trois comptes de démonstration et une machine
>   simulée : captures et constats dans
>   [guides/guide-tableau-de-bord.md](guides/guide-tableau-de-bord.md#94-ce-que-les-vrais-écrans-ont-montré-2-octobre-2026).
>   Les **fonctions Convex** que ces pages appellent, elles, ont été exercées
>   sur le déploiement de développement le 1er octobre 2026
>   ([deploiement.md](deploiement.md#4-essai-de-bout-en-bout-du-1er-octobre-2026)).
> - Le **site est en production** avec une version précédente de Convex. Les
>   nouvelles pages (droits de lancement, programmes, séance en direct, Mes
>   machines) arriveront en production au **redéploiement** (ticket ANH-82).
>   Pour les ouvrir en local contre le Convex de développement :
>   [deploiement.md](deploiement.md#53-lancer-le-site-en-local-sur-le-convex-de-développement).
> - Ce document a été écrit à partir du **code** ; il n'a pas été relu ligne à
>   ligne contre les écrans.

Sommaire :

1. [Ce que le site peut et ne peut pas faire](#1-ce-que-le-site-peut-et-ne-peut-pas-faire)
2. [Rôles et accès](#2-rôles-et-accès)
3. [Navigation](#3-navigation)
4. [Les pages, une par une](#4-les-pages-une-par-une)
5. [Scénarios pas à pas](#5-scénarios-pas-à-pas)
6. [Badges, indicateurs et courbes](#6-badges-indicateurs-et-courbes)
7. [Lancer le site en local](#7-lancer-le-site-en-local)
8. [Ce qui n'est pas vérifié ou pas fait](#8-ce-qui-nest-pas-vérifié-ou-pas-fait)

---

## 1. Ce que le site peut et ne peut pas faire

| Le site peut | Le site ne peut pas |
|---|---|
| Lancer une séance **auto** (programme piloté par la fréquence cardiaque). | Lancer une séance **manuelle** : il n'existe aucun bouton ni aucune fonction pour cela. Le manuel se démarre **uniquement** à la console de la machine ([console-locale.md](console-locale.md)). |
| Demander l'arrêt d'une séance (auto ou manuelle). La machine décélère sur sa rampe de sécurité. | Arrêter la machine instantanément. Ce n'est pas un arrêt d'urgence : le Pi reçoit la demande par interrogation toutes les 3 s environ. |
| Annuler une séance encore « en attente ». | Forcer un départ : le Pi revérifie tout et peut refuser. |
| Afficher l'état en direct et la télémétrie à 1 Hz. | Afficher une valeur périmée comme actuelle : un état de plus de 90 s est marqué « Données périmées », 90 s après le dernier signal (§6). |
| Régler la FC max et l'année de naissance d'un patient. | Modifier les programmes : ils viennent du Pi et sont en lecture seule. |

---

## 2. Rôles et accès

Trois rôles dans Clerk (`org:admin`, `org:gestionnaire`, `org:patient`), quatre
pour le site : l'admin de l'organisation Anheart est **admin**, l'admin de toute
autre organisation est **admin d'organisation** (`org_admin`). Un rôle se tient
**dans une organisation** (un client) : Convex le lit dans le jeton Clerk ou,
tant que Clerk Organizations n'est pas configuré, dans l'appartenance du compte
(table `memberships`) ; `users.role` n'en est qu'un miroir pour l'affichage
(voir [convex.md §3](convex.md#3-règles-dautorisation)). Les libellés affichés
sont « Administrateur », « Administrateur d'organisation », « Gestionnaire »,
« Patient ». Un rôle que le site ne connaît pas n'ouvre rien de plus qu'à un
patient : ni entrée de menu, ni bouton (`lib/organizationAccess.ts`).

| Rôle | Peut |
|---|---|
| **admin** | Tout voir et tout gérer, dans toutes les organisations. Créer et restaurer les machines, dans l'organisation choisie. Changer les rôles tant que Clerk ne les porte pas. Gérer les gestionnaires. Lancer une séance auto sur toute machine, pour lui-même ou pour n'importe quel patient. |
| **admin d'organisation** | Tout voir et tout gérer **dans son organisation** : machines (sauf créer et restaurer), patients, gestionnaires et leurs liens, membres (page Membres). Lancer une séance auto sur une machine de l'organisation, pour lui-même ou un patient de l'organisation. Pas de page Paramètres. |
| **gestionnaire** | Voir et gérer **ses** machines (liées par l'admin) et **ses** patients. Créer des patients. Régler leur physiologie. Accorder / retirer le droit de lancement sur ses machines, **à ses patients**. Lancer une séance auto sur ses machines, pour lui-même ou un de ses patients. |
| **user** (patient) | Voir ses propres séances et rapports. Lancer une séance auto **pour lui-même seulement**, sur les machines où un gestionnaire ou un admin lui a donné le droit. Arrêter sa propre séance. |

Le journal **Audit** est réservé aux administrateurs : l'admin Anheart voit
toutes les organisations et les actions globales ; l'admin d'organisation
(`org_admin`) ne voit que son organisation. Gestionnaire et patient n'y ont
pas accès.

**Qui donne le droit de lancement à qui :**

```
admin ──────────────► n'importe quel patient, sur n'importe quelle machine
gestionnaire ───────► un patient qu'il gère, sur une machine qu'il gère
patient ────────────► personne
```

Le droit n'est donné qu'à un compte de rôle `user` ; admins et gestionnaires
l'ont déjà par leur rôle.

**Organisations.** Tout ce tableau vaut **à l'intérieur d'une organisation** :
un gestionnaire ne voit ni machine, ni patient, ni séance d'un autre client,
et un patient ne reçoit un droit que sur une machine de sa propre
organisation. Le rôle **admin** ci-dessus est celui de l'organisation Anheart,
la seule qui voit tous les clients.

On travaille dans **une organisation à la fois**, celle que Clerk tient pour
active. Le **sélecteur d'organisation** de Clerk (`OrganizationSwitcher`, sans
compte personnel) est en haut de la barre latérale ; il est masqué quand la
barre est repliée en icônes. Choisir une autre organisation ramène au tableau
de bord. Toutes les réponses de Convex sont alors celles de cette organisation,
puisque le serveur lit l'organisation dans le jeton. Entre le choix et l'arrivée
du nouveau jeton au serveur, le site n'affiche **aucune** page, seulement
« Changement d'organisation en cours. Le tableau de bord s'affichera dès que le
serveur l'aura pris en compte. », pour ne pas montrer sous le nom de la
nouvelle organisation des données de la précédente (`OrganizationGate`, règle
`organizationView` de `lib/organizationAccess.ts`).

Quand le serveur n'accepte aucune organisation pour l'appel, le site n'affiche
pas les pages et dit pourquoi, avec le sélecteur pour en choisir une :

| Cas | Message |
|---|---|
| Aucune organisation active (après `ANHEART_ORG_ID`) | « Choisissez une organisation pour continuer. » |
| Organisation pas encore reçue par le webhook | « Cette organisation n'est pas encore connue du serveur. Réessayez dans quelques instants. » |
| Rôle Clerk inconnu de Convex (`org:member` par exemple) | « Votre rôle dans cette organisation n'est pas reconnu. Demandez à son administrateur de vérifier votre rôle. » |
| Revendication d'organisation illisible | « Votre session ne permet pas d'identifier votre organisation. Déconnectez-vous puis reconnectez-vous. » |
| Compte sans appartenance (avant `ANHEART_ORG_ID`) | « Votre compte n'appartient à aucune organisation. Demandez une invitation à votre administrateur. » |

La barre latérale ne montre alors que « Tableau de bord », comme pendant un
changement d'organisation. Tant que `ANHEART_ORG_ID` n'est pas définie et que
personne n'a d'organisation active dans Clerk, les pages s'affichent comme
avant ; le sélecteur et l'entrée Membres apparaissent déjà.

**Membres.** Inviter, retirer un membre et changer son rôle se font dans Clerk,
depuis la page **Membres** (admins) ou l'entrée « Gérer » du sélecteur. Clerk
envoie chaque changement à Convex par son webhook. Un gestionnaire est donc un
membre invité avec le rôle Gestionnaire ; la page Gestionnaires décide ensuite
de ses machines et de ses patients. Une fois `ANHEART_ORG_ID` définie, la page
Paramètres ne propose plus de changer un rôle et renvoie vers Membres.

**Protection des pages.** `proxy.ts` (middleware Clerk + next-intl) exige une
connexion pour `/{locale}/dashboard/...`. Les contrôles par rôle se font dans
Convex : une page ouverte sans le bon rôle reçoit des listes vides ou un refus.
Quelques pages affichent aussi un message « accès refusé » (voir §4).

**Premier compte.** La fiche est préparée par l'invitation ou créée par le
webhook Clerk. L'accueil affiche le lien vers le tableau de bord sans créer de
compte. Le premier admin est invité dans l'organisation Clerk Anheart avec
`org:admin` ; `ANHEART_ORG_ID` identifie cette organisation côté Convex. Aucun
champ `role` n'est à modifier en base (voir [convex.md §8](convex.md#8-déployer)).

### Amorçage d'un déploiement

À faire une fois par déploiement (développement, puis production), **dans cet
ordre**. Le détail des réglages est dans
[convex.md, « Activer le multi-organisation »](convex.md#activer-le-multi-organisation).

1. **Convex** : déployer, puis lancer la migration
   `migrations/multiOrganization:attachExistingRowsToAnheart`. Elle crée
   l'organisation par défaut « Anheart » et y range toutes les lignes
   existantes. Le site fonctionne alors comme avant, en une seule organisation.
2. **Clerk, sans webhook pour l'instant** : activer Organizations ;
   désactiver la création d'organisations par les utilisateurs (sinon
   n'importe quel compte peut créer la sienne et en devenir l'admin) ; créer
   les rôles `org:gestionnaire` et `org:patient` à côté d'`org:admin`, avec
   `org:patient` pour rôle par défaut ; ne donner les permissions système des
   membres (lire, gérer) qu'à `org:admin`, sinon l'entrée « Gérer » du
   sélecteur montre à un gestionnaire ou à un patient le nom et l'adresse de
   tous les membres ; ajouter `org_id` et `org_role` au modèle de jeton
   `convex`. Sans `org_id` dans le modèle, le site reste sur « Changement
   d'organisation en cours » pour qui a une organisation active : le serveur
   ne voit jamais l'organisation choisie.
3. **Site** : mettre en ligne cette version, qui porte le sélecteur
   d'organisation et la page Membres. Activer Organizations avant (étape 2).
   Les paquets npm du site (`@clerk/nextjs` 6.39, `@clerk/clerk-react` 5.61,
   `@clerk/shared` 3.48) ne contrôlent pas ce réglage : `OrganizationSwitcher`
   confie l'affichage à `clerk-js`, que le navigateur charge à l'exécution
   depuis l'API Frontend de l'instance
   (`/npm/@clerk/clerk-js@5/dist/clerk.browser.js`, la 5.128.0 au
   2026-10-14). D'après le code de cette version, **lu et non observé** :
   quand Organizations est désactivé, le sélecteur n'est pas monté. Sur une
   instance de développement, une fenêtre propose d'activer Organizations (la
   page se recharge si on l'active), et la fermer lève une erreur (« The
   <OrganizationSwitcher/> cannot be rendered when the feature is turned
   off… ») ; sur une instance de production, rien ne s'affiche. La page
   Membres (`OrganizationProfile`) passe par le même contrôle. Aucune instance
   sans Organizations n'a été essayée, et une autre version 5 peut faire
   autrement.
4. **Créer l'organisation Anheart dans Clerk** et noter son identifiant
   (`org_…`). Le webhook ne doit **pas** encore être configuré : il créerait
   pour elle une seconde organisation « Anheart », distincte de celle de
   l'étape 1 qui porte les données existantes. **N'y ajouter personne avant
   l'étape 6**, pas même un propriétaire choisi à la création : Clerk n'envoie
   un événement qu'aux endpoints qui existent quand il se produit, et ne
   rejoue pas les précédents. Convex ne connaît pas l'appartenance d'un membre
   ajouté plus tôt ; un gestionnaire, par exemple, ne peut alors recevoir ni
   machine ni patient. Si c'est déjà fait : après l'étape 6, changer le rôle
   de ce membre dans Clerk puis le remettre. Chaque changement est un
   événement ; le premier crée l'appartenance dans Convex, ou met à jour celle
   que la migration avait donnée à un compte existant. Si ce membre est le
   seul admin de l'organisation (le propriétaire choisi à la création, par
   exemple), inviter d'abord un second admin (étape 7) : Clerk peut refuser
   de retirer son rôle au dernier membre qui gère les membres (non vérifié).
5. **Poser `ANHEART_ORG_ID`** sur le déploiement Convex
   (`npx convex env set ANHEART_ORG_ID org_…`), puis relancer la migration :
   elle relie l'organisation par défaut à celle de Clerk. Vérifier dans le
   tableau de bord Convex qu'**une seule** organisation porte ce `clerkOrgId`
   (c'est lui qui compte : le slug peut devenir celui de Clerk, par exemple
   après une réparation) et qu'aucune n'a le slug `anheart` sans
   `clerkOrgId`. Si le webhook l'avait déjà reçue, la migration
   s'arrête sans rien écrire (« Two Anheart organisations… ») : la sortie,
   une description sans écriture puis une application qui recopie le nom de
   l'organisation décrite, après avoir vérifié dans Clerk que c'est bien
   celle d'Anheart, est dans
   [convex.md, « Sortir de deux organisations Anheart »](convex.md#sortir-de-deux-organisations-anheart).
   Dès cette étape, un jeton sans organisation est refusé, seuls les
   `org:admin` d'Anheart sont admins Anheart, et les rôles ne se changent plus
   que dans Clerk.
6. **Webhook** : créer l'endpoint Clerk vers `/clerk/webhook` et poser son
   secret dans `CLERK_WEBHOOK_SIGNING_SECRET`. Il reconnaît désormais
   l'organisation Anheart comme celle de l'étape 1. Les membres s'ajoutent à
   partir d'ici.
7. **Comptes** : inviter le premier admin avec `org:admin`, et faire entrer
   chaque compte existant dans son organisation Clerk avec le rôle qu'il a dans
   Convex. Entre l'étape 5 et son entrée, un compte existant ne voit que
   « Choisissez une organisation pour continuer. » (son jeton ne porte aucune
   organisation, ce que le serveur refuse dès que `ANHEART_ORG_ID` est
   définie) : enchaîner les étapes 5 à 7 dans une même fenêtre annoncée.
8. **Chaque client** : l'admin Anheart crée son organisation dans Clerk et y
   invite son premier `org:admin`, qui invite à son tour gestionnaires et
   patients depuis la page Membres (ou crée ses patients depuis la page
   Patients). L'admin Anheart crée les machines du client en choisissant son
   organisation dans le formulaire.

**Version du site.** Le pied de page de l'accueil et de la FAQ affiche la
version du site, par exemple `web-0.1.0` (`web-0.0.0-dev` tant qu'aucune release
n'a été faite). Elle vient du champ `version` de `package.json`, figé à la
construction : voir [release.md](release.md#1-les-trois-composants-et-leur-version).

---

## 3. Navigation

Adresses : toujours préfixées par la langue, `/fr/...` (défaut) ou `/en/...`.

Barre latérale (`components/dashboard/AppSidebar.tsx`) :

| Section | Entrées | Visible pour |
|---|---|---|
| Principal | Tableau de bord | tous |
| Administration | Gestionnaires, Membres, Utilisateurs | admin, admin d'organisation |
| Administration | Audit | admin, admin d'organisation |
| Administration | Paramètres | admin |
| Gestion | Patients, Machines, Mes machines, Sessions, Rapports, Alertes | admin, admin d'organisation, gestionnaire |
| Ma Santé | Mes machines, Sessions, Rapports | patient |

Tant que le compte se charge, ou quand le serveur n'accepte aucune
organisation pour l'appel (§2), seule l'entrée « Tableau de bord » est
affichée. La règle est `navigationFor` dans `lib/organizationAccess.ts`.

Sous le logo : le sélecteur d'organisation (§2). En haut : un fil d'Ariane, la
cloche des alertes (ANH-90), le sélecteur de langue et le thème clair / sombre.
La cloche (`components/dashboard/NotificationBell.tsx`) mène à la page des
alertes et porte le nombre d'alertes non lues (« 99+ » au-delà de 99) ; elle
n'apparaît que pour qui reçoit des alertes : un admin ou un gestionnaire
dont l'organisation est acceptée, jamais un patient.

---

## 4. Les pages, une par une

### Pages publiques

| Adresse | Contenu |
|---|---|
| `/fr` (accueil) | Page de présentation « Gaura » (principe, fréquence cardiaque, cas d'usage, bénéfices, marchés). Boutons Clerk « Commencer » (inscription) et « Se connecter ». Connecté : « Accéder au tableau de bord ». |
| `/fr/faq` | Questions fréquentes. |
| `/fr/privacy`, `/fr/terms` | Confidentialité, conditions. |

Le texte d'accueil et la FAQ ne citent aucun chiffre d'intensité : « L'intensité
se règle par la vitesse de rotation, dans les limites fixées par le logiciel de
la machine. » Pour mémoire, le plafond logiciel est la vitesse nominale moteur
(27,7 tr/min bras, environ 2,1 g au bout des jambes), et beaucoup moins avec une
personne à bord. Voir [securite.md](securite.md). Tout chiffre publié sur le site
dépend de la décision médicale (ANH-98). La FAQ dit ce que la machine mesure,
d'après [raspberry-pi.md](raspberry-pi.md) section 9 : l'ECG d'un capteur
BITalino, dont la machine tire la fréquence cardiaque ; les autres voies
(EDA, respiration, EMG, lumière, pouls) affichées sur la console pour
surveillance seulement, aucune ne commandant la machine ; pas de mesure de la
saturation en oxygène (ANH-194, elle annonçait six canaux d'un « système de
surveillance ECG »).

### Tableau de bord : `/fr/dashboard`

Pour tous. Titre « Tableau de bord », « Bienvenue, {prénom} ».

Les chiffres des cartes viennent d'une seule query, `dashboard.counters`
([convex.md](convex.md#dashboardts-compteurs-du-tableau-de-bord-anh-159)),
comptés par le serveur dans l'organisation de l'appel ; pour l'admin Anheart,
dans toutes, ce que la page dit sous le titre (« Les compteurs portent sur
toutes les organisations. »). Chaque carte compte ce que la page vers laquelle
elle mène liste, rien de plus.

| Carte | Pour | Chiffre | Sous le chiffre |
|---|---|---|---|
| **Machines en ligne** | admin, admin d'organisation, gestionnaire | Machines affichées « En ligne » ou « En session », jugées comme leur badge (§6) : une machine n'y compte plus 90 s après son dernier signal, même si Convex la dit encore en ligne. Admin : celles de l'organisation (toutes pour l'admin Anheart) ; gestionnaire : les siennes. | « / N au total » (machines non supprimées), puis « dont N en session » s'il y en a. |
| **Sessions actives** | tous | Séances actives que l'on peut ouvrir : toutes celles de l'organisation pour un admin, celles de ses machines ou qu'il pratique pour un gestionnaire, les siennes pour un patient. Une séance « Interrompue, fin inconnue » n'y compte pas. | « N en attente », ou « Aucune en attente ». |
| **Utilisateurs** | admin, admin d'organisation | Membres actifs de l'organisation (de toutes pour l'admin Anheart, un compte membre de deux organisations compté dans chacune). | Par rôle : « 2 administrateurs · 3 gestionnaires · 11 patients ». |
| **Patients** | gestionnaire | Ses patients, encore patients actifs de l'organisation, comme la page Patients les liste. Jamais un chiffre de toute l'organisation. | « Patients que vous suivez ». |

Pendant le chargement, chaque carte affiche un emplacement grisé (et
« Chargement... » pour un lecteur d'écran). Si le serveur refuse ou échoue, ou
ne renvoie pas le chiffre d'une carte, la carte affiche « - » et « Compteur
indisponible » ; le reste de la page s'affiche, et les chiffres reviennent dès
que la query répond de nouveau. C'est aussi ce qu'affiche ce site face à un
Convex antérieur, qui n'a pas cette query : Convex se déploie avant le site
([deploiement.md](deploiement.md)). Un patient ne voit que **Sessions actives** :
le serveur ne lui renvoie ni machine ni membre.

- **Sessions récentes** : les 5 dernières, avec statut et ancienneté. Un clic
  ouvre la vue en direct (séance active) ou le détail.

### Machines : `/fr/dashboard/machines`

Admin et gestionnaire. Tableau des machines (nom, lieu, statut, dernier signal).
Le statut et « Dernier signal » sont recalculés chaque seconde (§6).
Recherche ; l'admin peut afficher les machines supprimées.

Les admins Anheart et d'organisation voient aussi trois badges à côté du nom :
le niveau de validation, le statut de version (**À jour**, **Obsolète**,
**Non validée pour cette machine**, **Inconnue**) et **Configuration : non
disponible (ANH-141)**. `machineRegistry.listRegistryBadges` fournit ces badges
pour les machines non supprimées. Le gestionnaire ne les voit pas et le site
ne lance pas cette query pour lui. Une réponse encore en chargement n'invente
aucun badge.

- **Nouvelle machine** (admin Anheart seulement) : nom, lieu, **organisation**
  (la sienne par défaut, ou celle d'un client : `organizations.listOrganizations`),
  gestionnaires. Seuls les gestionnaires de l'organisation choisie sont
  proposés ; changer d'organisation décoche ceux déjà cochés. À la modification
  d'une machine, la liste est celle des gestionnaires de l'organisation de la
  machine, et un admin d'organisation peut la changer comme l'admin Anheart. À la
  création, la **clé API** s'affiche **une seule fois** (« Cette clé ne sera
  affichée qu'une seule fois! ») avec un bouton Copier. Elle va dans la
  configuration de la machine (`MACHINE_API_KEY` dans `/etc/anheart/anheart.env`
  sur un Pi installé, dans `raspberry-pi/.env` sur un poste de développement).
  C'est la première ligne de la carte **Clés de la machine**, sans expiration.

### Détail d'une machine : `/fr/dashboard/machines/{id}`

La carte **Santé de la machine** est visible par les administrateurs et les
gestionnaires autorisés. Elle présente le dernier rapport du Pi : température,
charge et mémoire, espace disque, temps depuis démarrage, horloge, aller-retour
Convex, liaisons et batterie, état thermique, enregistrements en attente et
dégradés, hors-service après redémarrages, préflight, incidents et compteurs.
Une source indisponible est écrite « Indisponible ». Après 30 secondes sans
rapport, les valeurs sont explicitement historiques. Les avertissements portent
sur CPU > 70 °C, disque < 1 Gio, horloge > 5 s en valeur absolue, batterie < 30 %,
liaison coupée, enregistrement dégradé ou état hors service.

L'historique sur 24 heures montre température et disque par périodes de cinq
minutes (minimum, maximum, dernière valeur), avec un tableau accessible sous
« Voir les mesures ». Une absence de mesure n'est pas interpolée ni remplacée
par zéro. La batterie, le thermique, le préflight et les incidents restent
inconnus lorsque leurs producteurs ne fournissent pas encore ces mesures.

Admin et gestionnaire de la machine.

| Bloc | Contenu | Qui agit |
|---|---|---|
| Bandeau « Supprimée » | Date de suppression, bouton **Restaurer**. | admin |
| Bouton **Lancer une séance auto** | Ouvre la fenêtre de lancement (§5). | admin, gestionnaire |
| Configuration | Statut (« En ligne », « Hors ligne », « En session ») et dernier signal, recalculés chaque seconde (§6), « Créée le » ; bouton Modifier. | admin, gestionnaire |
| **Version médicale** | Version annoncée par le Pi, ou `-` s'il n'en a pas annoncé. Le statut calculé par rapport à la référence serveur est `unknown` (pas d'annonce ; pas de libellé dans la fiche), `unsigned` (fichier absent ou signature non vérifiée, mode banc), `no_reference` (pas de version de référence serveur), `current` (à jour), `obsolete` (ancienne, référence affichée) ou `ahead` (version inconnue du serveur). | lecture |
| Gestionnaires | Liste, badge « Propriétaire » pour le premier. | lecture |
| **Registre de la machine** | Quatre sections : **Identité**, **Mesures**, **Variateur**, **Validation** ; version remontée et attendue, verdicts des verrous, signalements. | admin Anheart en écriture ; admin d'organisation en lecture seule ; aucun registre pour le gestionnaire |
| **État en direct** | Mode, phase, fréquence cardiaque, vitesse du bras (et moteur, consigne), charge g, action de sécurité, état du variateur ; badge « En direct » / « Données périmées » et « Mis à jour il y a … », recalculés chaque seconde (§6). L'action de sécurité et l'état du variateur sont traduits ; une valeur que le site ne connaît pas s'affiche telle quelle. | lecture |
| **Programmes** | Programmes synchronisés depuis le Pi (lecture seule) : zone, durée, vitesse max, FC limite. Mention « Manuel : uniquement depuis la console de la machine ». La carte explique aussi si les programmes auto sont désactivés, si la machine annonce un fichier médical non signé ou si aucune version de référence n'est enregistrée. | lecture |
| **Droits de lancement** | Patients autorisés, « Accordé par {nom}, {date} », leur FC max ; boutons **Accorder** (choisir un patient) et **Retirer** (avec confirmation). | admin, gestionnaire |
| **Clés de la machine** | Toutes les clés non révoquées d'abord (historique en tête, puis les plus récentes), puis les révoquées récentes jusqu'à 50 lignes au total : préfixe, état, création et son auteur, dernière utilisation, expiration ; boutons **Nouvelle clé** et **Révoquer** (ci-dessous). Sur une machine supprimée, visible seulement à l'admin Anheart, avec **Révoquer** seul. | admin Anheart, admin de l'organisation si la machine n'est pas supprimée (ANH-165, ANH-238) |
| Zone de danger | **Supprimer** (suppression douce, refusée si une séance est active ou en attente). | admin, gestionnaire |

**Registre (ANH-147).** `MachineRegistryCard` lit
`machineRegistry.getRegistry` ; une réponse `null` ne rend rien. L'écriture
exige le rôle admin Anheart côté site et `canEdit` côté serveur. Un admin
d'organisation ne reçoit aucun formulaire ni bouton d'édition du registre.

| Section | Lecture et écriture |
|---|---|
| **Identité** | Numéros de série Pi, variateur, moteur et réducteur, adresse BITalino, mise en service, adresse du site, contacts, version logicielle attendue. `updateIdentity` enregistre les champs ; un champ vidé est envoyé avec `null`. `listContactCandidates({machineId})`, réservée à l'admin Anheart, propose uniquement les membres actifs de l'organisation de cette machine, avec leur rôle et sans e-mail. |
| **Mesures** | Deux rayons en mètres, date et auteur : `recordRadii`. La proposition `ARM_RADIUS_M` / `LEG_TIP_RADIUS_M` reprend les mesures sans valeur par défaut. Leur application attend la configuration centralisée (ANH-141). |
| **Variateur** | Fiche courante et historique téléchargeables, version, signataire et date. Dépôt PDF ou JSON de 10 Mio maximum : `generateUploadUrl`, POST du fichier vers Convex Storage avec son `Content-Type`, puis `attachDriveParameterSheet`. Aucune donnée de santé dans ces pièces. |
| **Validation** | Niveau courant, revues avec auteur, date, versions logicielle et médicale, compte rendu joint ; historique des descentes avec motif. `addValidationReview` ne propose que le niveau suivant et exige un compte rendu (même dépôt Storage). Version médicale facultative pour les premiers niveaux, obligatoire pour « Personne à bord ». `lowerValidationLevel` propose uniquement un niveau inférieur et exige un motif de 3 à 500 caractères. |

Les niveaux sont **Aucune validation**, **Au banc**, **Automatique**,
**Personne à bord**. La clé médicale de test bloque le dernier niveau, avec
une explication ANH-98. Le niveau affiché est le niveau effectif : si le niveau
stocké ne correspond pas à l'historique, la fiche avertit et applique « Aucune
validation ». Les auteurs des revues, descentes, rattachements de fiches et de
la dernière modification apparaissent par prénom et nom, sans e-mail ; un
compte supprimé ou absent garde son seul identifiant. L'auteur d'une mesure
est le nom saisi. La mise en service est affichée en date UTC. Enregistrer
une section conserve les saisies non enregistrées des autres sections.
Les refus passent par `useMutationWithFeedback` et les codes traduits de
`lib/feedback.ts`. Un dépôt manquant, invalide ou échoué affiche également
une erreur traduite ; aucune mutation d'attachement ne suit un échec du dépôt.

Une version logicielle de niveau inférieur à celui de la machine est marquée
**Non validée pour cette machine**. Une version attendue insuffisamment
validée reste enregistrable et provoque un avertissement. Les verdicts de
`configurationLocks` affichent **Autorisé** ou **Refusé**, avec le niveau
atteint en cas de refus, pour `PROGRAMS_ENABLED` (au moins `auto`) et
`OCCUPANCY_OCCUPIED_ENABLED` (au moins `occupied`). Le site précise que
l'application attend ANH-141 et que le refus OTA attend ANH-116. Il ne
propose aucune commande d'application. Les deux signalements du serveur,
`programs_enabled_without_auto` et `person_on_board_without_occupied`, sont
des alertes visibles ; le second affiche son nombre et sa date la plus récente.

Les scénarios de rôle, saisie, dépôt, transition, version, verrou et badge sont
exercés dans `components/machines/machine-registry.test.tsx` et les tests des
deux pages machines. Ces tests utilisent uniquement des données synthétiques.

**Modifier** (carte Configuration) enregistre les champs de la machine, dont
le nom et le lieu, par `machines.updateMachine`, ouvert à l'admin et au
gestionnaire de la machine.
La liste des gestionnaires de la machine, que seul un admin voit dans la
fenêtre, part dans un second appel réservé à l'admin
(`machines.assignMachineToGestionnaires`) : la fenêtre ne le fait que si
l'appelant est admin **et** si la liste a changé. L'ordre fait partie de la
liste, car le serveur fait du premier gestionnaire le « Propriétaire » ; la
fenêtre s'ouvre avec le propriétaire en tête. Un gestionnaire décoché puis
recoché passe en fin de liste et la liste est alors envoyée : si c'est le
propriétaire, le premier des autres gestionnaires cochés devient propriétaire.
Le serveur n'écrit que la différence : un gestionnaire qui reste garde sa
ligne, avec son auteur et sa date, et un identifiant envoyé deux fois ne fait
qu'une ligne. Un gestionnaire qui modifie le nom ou le lieu ne fait donc que
le premier appel, et un admin qui ne touche pas aux cases ne réécrit pas la
liste.

La fenêtre est remplie à son ouverture, et seulement alors : la machine que
lit la page change à chaque signal de vie d'une machine en ligne, et la saisie
en cours, les cases et le message d'erreur restent tels quels (ANH-194).
« Machine mise à jour avec succès » ne s'affiche, en notification, qu'une fois
les deux appels passés. Si le serveur refuse le premier, rien n'est enregistré
et son message s'affiche en rouge en haut de la fenêtre, qui reste ouverte.
S'il refuse la liste, le nom et le lieu sont déjà enregistrés : la fenêtre le
dit (« Le nom et le lieu sont enregistrés, mais pas la liste des
gestionnaires : » suivi du motif) et reste ouverte. La règle d'envoi est dans
`lib/machineForm.ts`.

Une machine écrite avant la migration multi-organisation n'a pas encore
d'organisation ; seul l'admin Anheart la voit. Sa fenêtre ne demande alors
aucune liste de gestionnaires (sans organisation, le serveur rendrait ceux de
toutes les organisations) et n'envoie jamais la liste : le serveur ne garde
que les gestionnaires de l'organisation de la machine, donc une liste envoyée
retirerait ses gestionnaires sans en mettre aucun. Elle affiche à la place
« Cette machine n'appartient encore à aucune organisation : elle garde ses
gestionnaires actuels, qui pourront être changés ici une fois la migration
multi-organisation passée. » Le nom et le lieu s'enregistrent comme pour toute
machine.

**Clés de la machine (ANH-165).** La machine se présente au tableau de bord
avec une clé (`MACHINE_API_KEY` sur le Pi) ; elle peut en avoir plusieurs
valides à la fois. Chaque ligne montre le préfixe de la clé (`anh1.` et
8 caractères, jamais la clé), son état (« Active » ; « Remplacée, encore
valide » pendant le chevauchement d'une rotation ; « Expirée » ;
« Révoquée »), « Créée le {date} par {nom} » (sans nom pour une clé créée en
ligne de commande ou reprise de l'ancien champ par la migration), « Dernière
utilisation : {date} » ou « Jamais utilisée », et « Expire le », « Expirée
le », « Révoquée le » ou « Sans expiration ». La dernière utilisation est
écrite au plus une fois par minute : elle peut avoir jusqu'à une minute de
retard. L'état est celui du serveur à la lecture ; une clé dont le
chevauchement finit pendant que la page est ouverte passe « Révoquée » quand
la tâche du serveur l'a révoquée, dans les 5 minutes, alors qu'elle est
refusée à la machine dès la fin du chevauchement.

Toutes les clés non révoquées sont listées, même au-delà de 50, la plus
récente en tête ; les clés révoquées les plus récentes complètent jusqu'à
50 lignes au total. Avant la migration, la clé historique figure en tête,
avec son préfixe, son badge « Active » et « Clé d'avant les clés multiples ».
Elle ouvre les routes de la machine jusqu'à sa révocation ; remplacée par
une nouvelle clé, elle reste valide jusqu'à la fin du chevauchement. Ni sa
date de création ni sa dernière utilisation ne sont connues.
**Révoquer** utilise la même confirmation que pour les autres clés et la
déplace dans la table pour la révoquer aussitôt. Une valeur réversible ou un
sélecteur déjà dans la table ne donne pas de ligne historique : la table
décide seule. « Aucune clé n'est enregistrée pour cette machine. » indique
donc une machine vraiment sans clé.

- **Nouvelle clé** ouvre une fenêtre : le **chevauchement** avec les clés
  actuelles (« 24 heures (recommandé) », par défaut ; « 1 heure » ; « Aucun :
  révoquer les clés actuelles tout de suite ») et l'**expiration** de la
  nouvelle clé (« Jamais », par défaut ; « Dans 90 jours » ; « Dans un an »).
  **Créer la clé** affiche la clé **une seule fois**, avec **Copier** et
  l'avertissement « Cette clé ne sera affichée qu'une seule fois! » ; fermer
  la fenêtre la fait disparaître pour de bon. Pendant le chevauchement, les
  anciennes clés et la nouvelle fonctionnent toutes : le temps d'installer la
  nouvelle sur le Pi sans le couper. Un refus du serveur s'affiche en
  notification et laisse la fenêtre ouverte, sans clé.
- **Révoquer** (sur une clé active ou remplacée) demande une confirmation
  « Révoquer la clé {préfixe} ? » : la clé n'ouvre plus rien dès la requête
  suivante. Si c'est la seule clé valide de la machine, la fenêtre prévient
  qu'elle restera coupée du tableau de bord sans nouvelle clé. Révoquer la clé
  qui en a remplacé une autre ne prolonge pas celle-ci : elle expire à la fin
  de son chevauchement.

La procédure complète de rotation est dans
[secrets.md, 3.1](secrets.md#31-clé-de-la-machine). Un gestionnaire ne
voit pas la carte : le serveur lui refuse les clés, même de ses machines. La
régénération immédiate d'avant ANH-165 (« Régénérer la clé », ouverte aussi au
gestionnaire) n'existe plus ; une nouvelle clé avec le chevauchement « Aucun »
fait la même chose.

Sur une machine supprimée, seul l'admin Anheart voit cette carte, comme le
permet déjà la lecture serveur : **Révoquer** reste offert, y compris pour
la clé historique, mais **Nouvelle clé** disparaît. Ses clés n'ouvrent rien
tant que la machine est supprimée ; celles encore valides (ni révoquées ni
expirées) ouvriront de nouveau les routes à la restauration. La confirmation
de restauration demande de révoquer d'abord ici celles qui ne doivent plus
servir. Une rotation proposée à la restauration rouvrirait les clés au
moment même de la restauration, avant tout geste sur elles : la révocation
sur la machine supprimée permet de les couper avant. Aucun droit ne change.

### Mes machines : `/fr/dashboard/my-machines`

Pour tous (c'est la page de lancement des patients). Liste les machines où
l'utilisateur **peut lancer** : admin = toutes, gestionnaire = les siennes,
patient = celles où il a le droit.

- Patient : badge « Votre FC max : … » ou alerte « Votre FC max n'est pas
  renseignée : demandez à votre gestionnaire… ».
- Aucune machine : « Vous n'avez encore aucun droit de lancement… » (patient)
  ou « Aucune machine disponible. ».
- Par machine : nom, lieu, statut, état en direct avec son badge
  « En direct » / « Données périmées » (§6), nombre de programmes, bouton
  **Lancer une séance auto**, lien **Détails**. 90 s après le dernier
  signal, la carte affiche « Hors ligne » et grise le bouton (§6). Elle le grise
  aussi si la machine annonce des paramètres médicaux non signés ou si aucune
  version de référence n'est enregistrée, avec la raison affichée.
- La section **Mon aptitude et mon consentement** est disponible à tous les
  rôles, y compris sans machine. Le passager y remplit ou met à jour son
  questionnaire et signe lui-même le texte de consentement.
- Si le passager ne peut pas lancer, la carte de machine affiche le code de
  refus traduit et renvoie à cette section. Un gestionnaire ou un admin valide
  l'aptitude depuis la fiche Patient ; il peut aussi y retirer le consentement.

### Sessions : `/fr/dashboard/sessions`

Pour tous (chacun voit ce que ses droits permettent). Onglets par statut :
Tous, **En attente** (lancée du site, la machine ne l'a pas encore armée),
Actives, **Interrompues** (aucune nouvelle depuis 10 min, fin inconnue),
Terminées, Échouées. Colonnes : patient, machine,
**Type** (badges Auto / Manuel / Enregistrement et origine), statut, début,
durée (« Fin inconnue » pour une séance interrompue). Un clic ouvre la vue
en direct (séance active) ou le détail.

La liste est servie **par pages de 50**, du début le plus récent au plus ancien
(`sessions.listSessionsPaginated`, [convex.md §5](convex.md#sessionsts-lectures-seulement)).
Chaque onglet demande au serveur les séances de son statut : une séance en
attente ou échouée ancienne se trouve dans son onglet, même si cinquante
séances plus récentes d'un autre statut existent. Le bouton **Afficher plus**
ajoute les 50 suivantes ; tant qu'il reste des séances à charger, le compteur
sous le titre se termine par « + » (« 50+ sessions »). Changer d'onglet repart
de la première page. Le serveur n'applique la page qu'après les droits : un
patient ou un gestionnaire voit toutes ses séances, page après page, quel que
soit le nombre de séances des autres. La recherche ne porte que sur les
séances déjà chargées. Tant qu'il reste des pages, une recherche avec des
résultats rappelle cette limite et le nombre de séances chargées. Sans
résultat, une carte indique qu'aucune des séances chargées ne correspond et
invite à utiliser **Afficher plus** pour lire les plus anciennes. Une fois la
liste finie, elle indique « Aucune séance ne correspond à la recherche ».
Sans recherche, « Aucune session trouvée » ne s'affiche qu'une fois la liste
finie. Le plafond de lecture du serveur (ANH-227) peut rendre une page vide
alors que des séances restent à lire : une carte l'explique et **Afficher
plus** poursuit la lecture, même si plusieurs pages vides se suivent.

Cette page ne crée aucune séance : une séance auto se lance depuis une machine
(« Lancer une séance auto », §5), une séance manuelle depuis la console. Le
badge « Enregistrement » ne concerne plus que l'historique de l'ancien mode
d'enregistrement ECG, retiré.

### Séance en direct : `/fr/dashboard/sessions/{id}/live`

- Séance d'entraînement **en attente** : « En attente que la machine arme la
  séance… » et le bouton **Annuler la séance**.
- Séance d'entraînement **active** : le **panneau d'entraînement** (§6) avec
  gros indicateurs, courbes et bouton **Arrêter la séance**. Sans point reçu
  depuis 20 s, il affiche « Aucun signal récent de la machine… » et grise ses
  indicateurs (§6). Si la machine envoie des points qui n'ont pas été mesurés
  dans les 20 dernières secondes (rattrapage après une coupure, horloge de la
  machine déréglée), il affiche un autre bandeau et ne montre aucune valeur
  comme actuelle (§6).
- Séance d'entraînement **interrompue, fin inconnue** (le serveur n'en a eu
  aucune nouvelle pendant 10 min, [convex.md](convex.md#7-tâche-planifiée)) :
  le même panneau, avec le bandeau orange « Séance interrompue, fin
  inconnue », aucune valeur donnée pour actuelle (indicateurs grisés, « - »),
  le temps écoulé compté jusqu'à son dernier point, le bouton **Arrêter la
  séance** (§5.4) et le bouton **Voir le détail**. Elle redevient active, et
  la page avec elle, dès que la machine la nomme de nouveau ; la fin envoyée
  par la machine, même tard, la clôt.
- Séance d'entraînement **finie** : le même panneau, figé, et le bouton
  **Voir le détail**.
- La page n'affiche rien d'autre : la console locale n'envoie pas l'ECG brut au
  site, seulement la télémétrie.
- Séance de l'**ancien mode d'enregistrement** (historique) : pas de vue en
  direct. La page affiche « Cette séance vient de l'ancien mode
  d'enregistrement ECG : elle n'a pas de vue en direct. » et renvoie au détail.

### Détail d'une séance : `/fr/dashboard/sessions/{id}`

Nom du pratiquant (ou « Pratiquant non précisé »), machine, statut, bandeau
d'échec avec la raison. Pour une séance **interrompue, fin inconnue** : un
bandeau orange qui le dit, avec la date à laquelle le serveur l'a marquée,
la durée « Fin inconnue » au lieu de « En cours », et le bouton **Voir en
direct**, d'où son arrêt peut encore être demandé. Pour une séance
d'entraînement terminée (`completed` ou `failed`), la carte **Résumé de la
séance** (ANH-89, ci-dessous) ; une séance interrompue n'est pas terminée et
n'en a pas. Pour toute séance d'entraînement, la carte
**Entraînement** : type, origine, programme, zone cible, durée prévue, FC max du
pratiquant, opérateur, motif de fin, et les courbes de télémétrie. Suivent les
cartes « Informations patient », « Chronologie de la session » et, s'il y en a,
« Notes de session ».

**Résumé de la séance** (`components/training/SessionSummaryCard.tsx`, lu par
`sessionReports.getSummary`, sous les droits de la séance) : durée, temps en
zone (et sa part de la durée, avec le temps en dessous et au-dessus), FC
confirmée min / moyenne / max, vitesse max du bras et du moteur, g max et
variation de g max au rayon de référence et à la pointe du pied, verdicts de
sécurité (règle, action, instant), raison et règle de fin, avertissements de
l'enregistrement. Le badge dit d'où il vient : **Provisoire** tant qu'il est
calculé sur la télémétrie à 1 Hz (sans g à la pointe du pied ni variation de
g, qu'on ne lit que dans l'enregistrement), **Enregistrement** une fois
l'enregistrement déposé et vérifié. Si l'enregistrement n'a pas pu être lu, la
carte le dit avec la raison et garde le résumé provisoire. Tant qu'aucun
résumé n'est calculé, elle le dit. Pour une annulation ou un refus avant le
départ, la carte et le rapport indiquent « Cette séance n'a pas démarré sur
la machine : elle n'a pas de résumé. » ; l'historique indique « Non démarrée »
et ne dessine aucune barre de progression pour elle. « Pas encore calculé »
reste réservé aux autres séances sans résumé. Les définitions et les
sources sont dans [convex.md](convex.md#session_summaries_v2--résumé-dune-séance-dentraînement-anh-89).
Le bouton **Rapport PDF** ouvre le rapport de la séance.

Pour une variation de g absente aux deux rayons ou à l'un d'eux, la carte et
le rapport affichent « après l'enregistrement » seulement pour un provisoire
sans problème de lecture. Un résumé final indique « aucune fenêtre mesurable » :
la note rappelle qu'il faut six tics consécutifs (1 s), des mesures fraîches,
aucun silence ni action d'urgence, le variateur en marche
(`OPERATION_ENABLED`) et une vitesse moteur au moins égale à la plus lente
consigne de marche. Un provisoire dont l'enregistrement est illisible indique
« non disponible » et en donne la raison. Une valeur présente est écrite en g/s.

### Rapport d'une séance : `/fr/dashboard/sessions/{id}/report`

Le rapport d'une séance d'entraînement, mis en page pour l'impression (ANH-89) :
pratiquant, machine, début, type, programme, zone, statut, puis le résumé
(mêmes chiffres que la carte, avec la date du calcul), les courbes de
télémétrie à 1 Hz (FC face à la zone, vitesse du bras), et ce qui n'y est pas
encore : le résultat du pré-vol (ANH-142) et le retour du passager (ANH-146).
Il ne lit que ce que lit la page de la séance (`sessions.getSession`,
`training.getTrainingSession`, `training.getSessionTelemetry`,
`sessionReports.getSummary`), sous les mêmes droits, et n'en montre pas
l'adresse du pratiquant ni l'opérateur. À l'impression, les courbes sont
redessinées à 680 px dans la largeur utile de 182 mm d'une page A4 (marges
de 14 mm), quelle que soit la largeur de l'écran. À l'écran, elles restent
adaptées à la fenêtre.

- **Télécharger en PDF** ouvre l'impression du navigateur : y choisir
  « Enregistrer au format PDF », avec le papier **A4**. Le menu, l'en-tête et
  les boutons de la page ne sont pas imprimés, la page est en A4 et claire même en thème sombre
  (`app/globals.css`, `print:hidden`). Les graphiques d'arrière-plan ne sont
  pas nécessaires : les traits, la zone cible et les légendes restent lisibles
  sans cette option. L'en-tête du rapport, le tableau des g, les verdicts et
  chaque courbe restent groupés sur leur page. Aucune bibliothèque PDF n'est
  ajoutée : le fichier est celui du navigateur, et sa mise en page dépend de
  lui. Le bouton n'est actif qu'une fois la séance terminée.
- **Français / English** rouvre le même rapport dans l'autre langue.

Le rapport est disponible pour chaque séance d'entraînement `auto` ou `manual`
terminée ou échouée, même avant que son résumé soit calculé (il le dit alors).
Une ancienne séance ECG n'a pas de rapport d'entraînement : une ouverture
directe de cette adresse explique pourquoi, offre le retour à la séance et
n'affiche ni résumé vide ni bouton d'impression.

Pour une séance de l'ancien mode d'enregistrement ECG (historique, lecture
seule), la carte **Ancien enregistrement ECG** remplace la carte Entraînement :
canaux enregistrés et, si des données existent, nombre de lots, durée des
données et plage horaire, comptés par le serveur (`ecgData.getSessionDataStats`).
Le tracé n'est plus affiché.

### Patients : `/fr/dashboard/patients` et `/fr/dashboard/patients/{id}`

Admin et gestionnaire. Liste avec recherche, **Nouveau patient** (prénom, nom,
e-mail, langue). Le formulaire envoie une invitation Clerk avec le rôle patient.
Il confirme l'adresse après l'envoi ; un échec garde le formulaire ouvert pour
réessayer. La fiche apparaît dans la liste après acceptation et rattachement
par le webhook. Un compte déjà présent ailleurs rejoint cette organisation
sans créer un deuxième dossier ni écraser ses champs communs.

Fiche patient : identité, séances, bouton Modifier, suppression, les cartes
**Physiologie** (voir §5.1), **Aptitude** et **Consentement**, et la carte
**Historique et progression** (ANH-89,
`components/training/RiderHistoryCard.tsx`). Celle-ci liste les séances
terminées du pratiquant que le visiteur peut lire, 100 au plus, les plus
récentes d'abord (`sessionReports.riderHistory`, sous les droits de chaque
séance : un gestionnaire n'y voit que les séances des machines qu'il gère) :
date, programme et machine, statut, durée, temps en zone et sa part, FC
moyenne, et le lien vers le rapport. Au-dessus, le temps en zone de chaque
séance programmée, en minutes, de la plus ancienne à la plus récente ; une
séance sans temps en zone (manuelle, ou sans résumé encore) n'y a pas de barre.
La nausée et le ressenti du passager n'y sont pas encore : ils viendront
d'ANH-146, et la carte le dit.

### Utilisateurs : `/fr/dashboard/users` et `/fr/dashboard/users/{id}`

Admin et admin d'organisation (entrée de menu). Tous les comptes pour l'admin,
les membres actifs de son organisation pour un admin d'organisation ; filtre
par rôle, recherche. La fiche affiche le rôle, les séances ; pour un patient :
Modifier, Physiologie ; suppression (admin : tous sauf soi ; admin
d'organisation : les membres de son organisation sauf soi, retirés de cette
organisation seulement ; gestionnaire : ses patients seulement).

### Gestionnaires : `/fr/dashboard/gestionnaires` et `/{id}`

Admin et admin d'organisation (sinon « Accès refusé. Réservé aux
administrateurs de l'organisation. »). C'est la gestion des liens entre
gestionnaires, machines et patients **dans l'organisation** : l'admin
d'organisation n'y voit que les gestionnaires, machines et patients de la
sienne. Devenir gestionnaire, ou cesser de l'être, se fait dans Clerk : la page
le rappelle et renvoie vers **Membres**. Liste des gestionnaires avec leurs
nombres de machines et de patients. Fiche : **Assigner des machines** et
**Assigner des patients**.

La carte **Machines** de la fiche et le nombre de machines de la liste
laissent de côté les machines supprimées (`machines.getMachinesForGestionnaire`
et `users.listGestionnaires`), et la carte affiche « Chargement... » tant que
ses machines ne sont pas arrivées.

**Assigner des machines** : une case par machine non supprimée, cochée si le
gestionnaire la gère déjà. **Enregistrer** applique exactement les cases à ce
gestionnaire, par `machines.setGestionnaireMachines` : cocher ajoute le lien,
décocher le retire. Les autres gestionnaires d'une machine gardent leur lien,
et un lien déjà présent n'est pas modifié (il garde son badge
« Propriétaire »). Une machine supprimée reste hors de la décision, côté
serveur : son lien n'est jamais retiré, qu'elle soit nommée ou non, il
retrouve sa machine si elle est restaurée, et une machine supprimée n'est
jamais liée (refus « Machine introuvable »). Un lien de ce gestionnaire vers
une machine absente de la table, dans l'organisation de la décision, est
retiré dès l'enregistrement, que son identifiant soit nommé ou non dans la
liste envoyée, sans erreur. Les liens dans une autre organisation restent
intacts, même orphelins. Le décompte `{added, removed}` de la notification ne
compte que les machines visibles sur la page, jamais les liens orphelins
nettoyés. L'audit `gestionnaire_machines.set` liste au contraire tous les
identifiants retirés dans `details.removed`, orphelins compris, et écrit une
entrée dès qu'une ligne est ajoutée ou retirée, même si le nettoyage seul
rend `{added: 0, removed: 0}` (ANH-232). La fenêtre s'ouvre toujours sur
les machines actuelles du gestionnaire. Une case que l'admin a cliquée garde
son choix jusqu'à la fermeture ; les autres suivent le serveur, de sorte qu'un
signal de vie des machines n'annule rien et qu'un lien ajouté ou retiré
entre-temps par un autre admin apparaît dans la fenêtre (ANH-194). Après
l'enregistrement, une notification confirme, par exemple « Machines
enregistrées : 1 ajoutée, 1 retirée. » ; en cas de refus, le message du
serveur s'affiche dans la fenêtre, qui reste ouverte. La règle des cases est
dans `lib/gestionnaireMachines.ts`.

**Assigner des patients** : une case par patient de la liste de l'admin,
cochée si le gestionnaire le gère déjà. La fenêtre se comporte comme celle des
machines :

- elle s'ouvre toujours sur les patients actuels du gestionnaire, jamais sur
  les cases d'une modification annulée ; si ces patients changent sur le
  serveur pendant qu'elle est ouverte, une case déjà cliquée garde le choix de
  l'admin et les autres prennent l'état du serveur : ce qui est enregistré est
  ce qui est affiché ;
- tant que les patients du gestionnaire ou la liste des patients à choisir ne
  sont pas chargés, **Modifier** et **Enregistrer** sont inactifs, et la carte
  affiche « Chargement... » au lieu de « Aucun patient assigné » ;
- **Enregistrer** envoie la liste cochée à `users.assignPatientsToGestionnaire`,
  qui n'écrit que la différence : cocher ajoute le lien, décocher le retire, un
  lien déjà présent n'est pas réécrit. Un patient du gestionnaire qui n'a pas
  de case dans la fenêtre garde son lien ;
- après l'enregistrement, une notification confirme ce que le serveur a
  écrit, par exemple « Patients enregistrés : 1 ajouté, 1 retiré. » ; en cas
  de refus, le message du serveur s'affiche dans la fenêtre, qui reste ouverte
  avec ses cases.

Le serveur ne lie que les patients actifs de l'organisation du gestionnaire :
un patient d'une autre organisation coché par l'admin Anheart n'est pas lié, et
le décompte affiché ne le compte pas. Il n'y a ni journal ni annulation de ces
changements. Deux admins qui enregistrent l'un après l'autre : la liste du
dernier enregistrement s'applique en entier ; la fenêtre réduit ce risque en
suivant le serveur pour les cases que l'admin n'a pas touchées, mais un
changement fait entre le dernier rafraîchissement de la fenêtre et
l'enregistrement peut encore être défait. La règle des cases est dans
`lib/gestionnairePatients.ts`.

### Rapports : `/fr/dashboard/reports`

Séances terminées visibles par l'utilisateur : pratiquant, machine, date,
durée et le bouton **Voir** qui ouvre le détail. Le bouton **Rapport PDF**
ouvre le rapport des seules séances d'entraînement (`auto` ou `manual`, ANH-89).
Une séance de l'ancien mode ECG (`recording`) conserve **Voir** et indique
« Ancien enregistrement ECG : aucun rapport disponible. » Elles sont servies par pages
de 50, de la plus récente à la plus ancienne (`sessions.listSessionsPaginated`
avec le statut `completed`), avec le bouton **Afficher plus** tant qu'il en
reste ; comme pour la page Sessions, la page ne se coupe qu'après les droits.
Le plafond de lecture du serveur (ANH-227) peut rendre une page vide avant la
fin de la liste. Si aucun rapport n'est encore chargé, une carte indique
« Aucun rapport à afficher pour l'instant : d'autres séances restent à lire »
et explique qu'**Afficher plus** poursuit la lecture. Le message reste visible
pendant le chargement suivant ; « Aucun rapport disponible pour le moment »
ne s'affiche qu'une fois la liste finie et vide.
L'ancien rapport PDF, construit sur les données ECG, a été retiré avec le mode
d'enregistrement ; le rapport d'une séance d'entraînement est la page
[Rapport d'une séance](#rapport-dune-séance--frdashboardsessionsidreport). La
page Rapports ne liste que les séances `completed`. Une phrase sous la
description, même quand la liste est vide, explique comment ouvrir le rapport
d'un entraînement échoué : **Sessions > Échouées > Voir**, puis la carte
**Résumé de la séance > Rapport PDF**, ou l'historique du passager depuis sa
fiche si vous y avez accès. Tous les rôles, y compris le passager, peuvent
ouvrir l'onglet **Échouées** pour les séances qu'ils sont autorisés à lire.

### Membres : `/fr/dashboard/members`

Admin et admin d'organisation (sinon « Accès refusé. Réservé aux
administrateurs de l'organisation. »). Le panneau de Clerk
(`OrganizationProfile`) de l'organisation active : membres, invitations,
rôles. Clerk applique ses propres droits dans ce panneau. Sans organisation
active dans Clerk (déploiement avant `ANHEART_ORG_ID`), la page propose d'abord
d'en choisir une.

### Audit : `/fr/dashboard/audit`

Admin Anheart et admin d'organisation, avec une organisation acceptée par le
serveur. Les autres comptes lisent « L'accès au journal est réservé aux
administrateurs. » La page appelle `audit.list` ; Convex impose les droits
et le périmètre de l'organisation, y compris pendant un export. L'admin
Anheart voit aussi les actions globales, sans organisation.

Les filtres se combinent : acteur (personne, machine précise, toutes les
machines ou système), action, type de cible, période et, pour l'admin
Anheart seulement, organisation. Les dates sont des journées dans le fuseau
du navigateur : le début est inclus et la fin va jusqu'au début du jour
suivant, exclu. **Filtrer sur cette cible** sur une ligne ajoute sa cible
précise ; changer de type retire cette sélection. Le bouton de
réinitialisation efface tous les filtres.

Les lignes vont de la plus récente à la plus ancienne, par pages de 50,
avec **Afficher plus**. Colonnes : date, organisation (admin Anheart
seulement), acteur et rôle, action, cible, source, détails et identifiant de
requête. Actions, rôles, types et sources sont traduits. Les libellés
d'acteur et de cible n'incluent pas d'e-mail ; les noms sont lus dans les
comptes et machines actuels. Une référence disparue est remplacée par son
identifiant abrégé. Les détails montrent leurs clés traduites, les tableaux
sous forme de nombre d'éléments et les autres valeurs sur 60 caractères au
plus. L'identifiant de requête est abrégé à 12 caractères, complet au survol.
Les soumissions d'aptitude sont visibles comme `fitness.submitted`, sans
réponses ni verdict. Les créations et réactivations d'appartenance sont
tracées par leur écrivain, connexion ou webhook, une seule fois ; une
appartenance ouverte par la seule connexion reste donc visible. La suppression
automatique d'un enregistrement apparaît avec la source `cron`.
Les changements sans effet de physiologie, de rôle et d'identité du registre
n'ajoutent pas de ligne ; les champs affichés sont ceux réellement modifiés.
Les versions libres de fiche et d'enregistrement médical sont omises des
détails, y compris la version médicale citée par une revue machine.
Les invitations affichent le nombre de gestionnaires prévus ; leurs
identifiants figurent seulement dans l'export CSV. Leurs affectations à
l'acceptation ont chacune une ligne. Les changements
de propriétaire machine et de niveau de release indiquent respectivement
le nouveau propriétaire et le niveau précédent. Chaque liste d'identifiants
est bornée à 1 000 éléments ; en cas de troncature, `count` donne le total
des listes `added` et `removed` avant troncature.

**Exporter en CSV** relit les pages avec les filtres courants, par lots de
200, jusqu'à **10 000 lignes au plus** (hors en-tête). S'il reste des lignes,
la page affiche « L'export contient les 10 000 premières lignes. Restreignez
la période pour exporter la suite. » Le bouton, les sélecteurs, la
réinitialisation et tous les boutons « Filtrer sur cette cible » des lignes
restent désactivés pendant l'export ; un échec affiche un message et permet un nouvel
essai.

[`lib/auditCsv.ts`](../lib/auditCsv.ts) produit 13 colonnes stables : `at`,
`organizationId`, `organizationName`, `actorId`, `actorRole`, `actorLabel`,
`action`, `targetType`, `targetId`, `targetLabel`, `details`, `requestId`,
`source`. Les dates sont en ISO UTC et `details` en JSON. Le fichier UTF-8
porte un BOM et des fins de ligne CRLF ; l'échappement suit RFC 4180
(cellules contenant virgules, guillemets ou retours à la ligne entourées de
guillemets, guillemets internes doublés). Un préfixe `=`, `+`, `-`, `@`,
tabulation ou retour chariot est neutralisé par une apostrophe avant
l'échappement pour éviter son interprétation comme formule par un tableur.
[`lib/download.ts`](../lib/download.ts) déclenche le téléchargement puis
libère l'URL temporaire du fichier.

[`lib/auditCsv.test.ts`](../lib/auditCsv.test.ts) vérifie les 13 colonnes,
BOM, CRLF, ISO UTC, RFC 4180 et chaque préfixe de formule. Les tests de
[`app/[locale]/dashboard/audit/page.test.tsx`](../app/[locale]/dashboard/audit/page.test.tsx)
montent la page et ses composants `components/audit/` : accès selon le rôle,
filtres envoyés, pagination, libellés, export filtré par lots de 200 et
plafond de 10 000 lignes avec son message. Cette preuve utilise un DOM
simulé et Convex remplacé ; la vérification de bout en bout dans un
navigateur reste bloquée par ANH-83.

### Paramètres : `/fr/dashboard/settings`

Admin seulement (« Accès refusé. Réservé aux administrateurs. »). Votre compte ;
**Gestion des rôles** (choisir un utilisateur, un nouveau rôle, « Mettre à jour
le Rôle ») tant que `ANHEART_ORG_ID` n'est pas définie ; ensuite, la carte dit
que les rôles se gèrent dans l'organisation et renvoie vers **Membres** ;
informations système (valeurs affichées, pas des réglages).

### Alertes : `/fr/dashboard/notifications`

Admin, admin d'organisation et gestionnaire (un patient lit « Les alertes
sont réservées aux gestionnaires et aux administrateurs d'une
organisation. »). Les alertes des machines que le visiteur gère, dans
l'organisation où il travaille, les plus récemment changées d'abord (50 au
plus, la page le dit quand la liste est coupée) :

- **Défaut variateur** : le variateur de la machine signale un défaut, au
  heartbeat (toutes les 10 s) ou dans les événements d'une séance ; avec son
  code (« Code du variateur : OHF ») quand la machine l'a envoyé ;
- **Arrêt demandé par la sécurité** : un verdict de sécurité a arrêté une
  séance (« Arrêt progressif demandé », « Arrêt rapide demandé », « Commande
  du variateur coupée ») ; un verdict qui régule sans arrêter n'est pas une
  alerte ;
- **Séance échouée** : la machine a terminé une séance en échec, avec sa
  raison (« Raison : … »), ou le serveur a refusé un lancement avant que la
  machine l'arme (expiré, droit retiré, physiologie changée…), raison
  traduite (« Raison : Droit retiré avant le départ ») ;
- **Machine hors ligne** : aucun heartbeat depuis 90 s, constaté par le
  serveur à la minute.

Sur cette page, toute alerte d'une séance échouée que l'on peut lire affiche
sa raison, même après regroupement sous **Défaut variateur** ou **Arrêt
demandé par la sécurité**. Cette raison privilégie le `reasonCode`
traduit. Sinon, un identifiant connu de `sessionReport.endReasons`, seul ou
suivi de `: texte`, est remplacé par son libellé dans la langue du site :
`safety_verdict: hr_drop (RAMP_DOWN)` donne
« Verdict de sécurité : hr_drop (RAMP_DOWN) ». Le reste du texte est conservé,
avec une ponctuation propre à chaque langue (sans espace avant `:` en anglais).
Un identifiant inconnu ou du texte libre, comme
« refusee par la machine : console occupee », reste tel quel. Seules les
clés propres du catalogue comptent, jamais `constructor` ou `toString`.
Les raisons encore brutes sur les pages de séance restent un point de suivi.
Le refus de départ par la console est couvert par **Séance échouée** ; un
code dédié attend un ajout au contrat du Pi. Les alertes d'incident restent
bloquées par ANH-145.

Chaque alerte donne la machine, l'heure, « N fois, dernière à … » quand
plusieurs événements s'y sont fondus pendant qu'elle était non lue, le
badge « Non lue », et un lien **Voir la séance** (quand le visiteur peut la
lire) ou **Voir la machine**. Si la machine a été supprimée, l'alerte reste
visible et comptée, comme sa séance reste lisible : **Machine supprimée**
remplace le lien vers la machine, et **Voir la séance** reste proposé quand
la séance est lisible. Retirer le gestionnaire de la machine lui retire
aussi ces alertes. Les alertes devenues invisibles ne consomment ni les 50
places de la liste ni les 100 non lues comptées par la cloche.
**Marquer comme lue** et **Tout marquer comme lu** l'écrivent sur le serveur.
Le second traite toutes les alertes visibles non lues par lots de 500,
avec une suite automatique sans nouveau clic. Ce que le serveur écrit, quand, et pour qui :
[convex.md](convex.md#notificationsts-alertes-aux-gestionnaires-anh-90).

Pour une même séance, le défaut variateur, le verdict de sécurité et la fin
en échec se regroupent dans une seule alerte non lue par destinataire. Son
type peut devenir plus précis : défaut variateur, puis arrêt demandé par la
sécurité, puis séance échouée, par priorité décroissante. Cela regroupe aussi
un verdict de fréquence cardiaque et la fin en échec. Une alerte déjà lue
reste inchangée ; l'événement suivant en crée une nouvelle. Les alertes sans
séance gardent leur regroupement par machine.

Limite vérifiée : un heartbeat en défaut qui ne nomme pas la séance crée
une alerte `drive_fault` sans séance. Si le lot d'événements de la séance
livre ensuite le verdict avant le défaut (ordre de planification actuel de
`storeEvents`), le verdict crée l'alerte de la séance et le défaut s'y fond :
**2 alertes et 2 e-mails par destinataire** si l'envoi est activé, de
compteurs 1 sans séance et 2 avec séance. Dans l'ordre inverse, défaut puis
verdict, il reste **1 alerte de compteur 3 et 1 e-mail par destinataire**.
L'ordre de planification dans `storeEvents`, ou une séance nommée plus tôt
par le Pi, reste un point de suivi.

En bas, **Alertes par e-mail** : un interrupteur par type d'alerte. Par
défaut tout part aussi par e-mail sauf « Machine hors ligne ». Un e-mail ne
contient que le type d'alerte, le nom de la machine et un lien vers cette
page : jamais de fréquence cardiaque, de nom de passager ni de raison. Tant
que le déploiement n'a pas sa clé d'envoi, la carte l'écrit (« L'envoi
d'e-mails n'est pas configuré sur ce déploiement : les alertes restent dans le
site. ») ; les choix sont gardés pour le jour où elle le sera.
L'e-mail n'est envoyé qu'à la création de l'alerte et dit son type de départ ;
il n'est pas renvoyé si le type devient ensuite plus précis sur le site.

Tests : `app/[locale]/dashboard/notifications/page.test.tsx` (refus au
patient, contenu de chaque type, liens, marquer lu, refus traduit, liste
coupée, interrupteurs, absence de clé) et
`components/dashboard/notification-bell.test.tsx` (cloche absente, sans
nombre, nombre au singulier et au pluriel, « 99+ »).

---

## 5. Scénarios pas à pas

### 5.1 Régler la FC max et l'année de naissance

Qui : admin, ou gestionnaire du patient. Le patient ne peut pas le faire.

1. Ouvrir **Patients** → le patient (ou **Utilisateurs** → le compte).
2. Carte **Physiologie** : « FC max mesurée (bpm) » et « Année de naissance ».
3. Enregistrer. Laisser un champ vide l'efface.

Règles :

- Une FC max **mesurée** prime sur l'estimation par l'âge. Bornes : 100 à
  220 bpm (« La FC max doit être comprise entre 100 et 220 bpm »).
- Sans FC max mesurée, la **FC max retenue** est l'estimation de Tanaka
  `208 − 0,7 × âge` (affichée « estimée (208 − 0,7 × âge) »).
- L'année de naissance doit donner un âge de 10 à 100 ans.
- **L'année de naissance est obligatoire pour toute séance auto**, même avec une
  FC max mesurée : elle sert au contrôle d'âge (18 ans minimum, voir
  [securite.md](securite.md)).
- Les deux valeurs sont des **entiers** : la carte et le serveur refusent tout
  autre nombre. Une valeur déjà enregistrée qui n'en est pas un compte comme
  non renseignée : la carte affiche « Non renseignée », marque le champ, et
  n'enregistre rien tant qu'il n'est pas corrigé ou vidé
  ([convex.md §4](convex.md#4-fonctions-de-trainingts)).

### 5.2 Donner le droit de lancement à un patient

1. **Machines** → la machine → carte **Droits de lancement**.
2. « Choisir un patient » → **Accorder**.
3. Pour retirer : **Retirer** → confirmer (« {nom} ne pourra plus lancer de
   séance auto sur cette machine. »).

Un gestionnaire ne voit dans la liste que ses propres patients.

### 5.3 Lancer une séance auto

Depuis **Mes machines** ou le détail d'une machine : **Lancer une séance auto**.

| Champ | Rôle |
|---|---|
| Programme * | Programmes synchronisés depuis le Pi : nom, zone, durée. Sous la liste : zone, FC limite, vitesse max du bras. |
| Durée (minutes) | Vide = durée du programme. Doit être positive. |
| Pratiquant * | Patient : lui-même, non modifiable. Admin / gestionnaire : « Moi-même » ou un patient. |
| FC max du pratiquant | Affichée, avec « estimée » si c'est l'estimation. |
| Notes | Facultatif. |

La fenêtre **bloque** le bouton **Lancer** et explique pourquoi quand : aucune
version des paramètres médicaux n'est enregistrée sur le serveur, la machine
annonce un fichier non signé (mode banc), elle n'est pas lançable, est hors
ligne, est déjà en séance, a les programmes désactivés, n'a aucun programme,
la FC max ou l'année de naissance manque (ou la valeur enregistrée n'est pas
un entier, pour un patient comme pour « Moi-même »), ou le pratiquant est plus
jeune que l'âge minimum de la version médicale courante. L'absence de version
de référence affiche « Aucune version des paramètres médicaux n'est enregistrée
sur le serveur : aucun lancement à distance n'est possible. » Le mode banc
affiche « Paramètres médicaux non signés : la machine est en mode banc
uniquement. » Elle **avertit** si la zone dépasse la fraction de FC max fixée
par la version courante ou si la FC limite du programme dépasse la FC max
(« Le serveur refusera ce lancement. »).

Après **Lancer** :

1. Convex refait tous les contrôles ([convex.md §4](convex.md#4-fonctions-de-trainingts)) et crée la séance **en attente**.
   Une seule séance à la fois (ANH-144) : si le pratiquant a déjà une séance
   en attente, en cours, ou interrompue sur une machine qui ne s'est pas
   manifestée depuis, sur n'importe quelle machine, le lancement est refusé
   avec « Pratiquant déjà dans une autre séance, en attente ou en cours : une
   seule à la fois. » ; si c'est la machine, avec « Cette
   machine a déjà une séance en attente ou en cours : une seule à la fois. ».
   Le message ne dit pas où est l'autre séance.
2. Le site ouvre la vue en direct : « En attente que la machine arme la
   séance… ».
3. Le Pi interroge Convex toutes les 3 s. Il refait **ses** contrôles
   (câblage d'arrêt d'urgence attesté depuis le démarrage, aucun verdict de
   sécurité en cours, console libre, `PROGRAMS_ENABLED=true`,
   `OCCUPANCY_OCCUPIED_ENABLED=true`, programme sous le plafond « personne à
   bord », zone revalidée pour ce pratiquant, âge).
4. S'il accepte, la séance passe **active** et les courbes arrivent.
5. S'il refuse, la séance passe **échouée** avec la raison du Pi, qui commence
   par « refusee par la machine : ». Sans réponse en 60 s : « la boucle n'a ni
   demarre ni refuse ».
6. Convex **revérifie** le lancement pendant qu'il attend et au moment où la
   machine l'arme ([convex.md, Revérification à l'armement](convex.md#revérification-à-larmement-anh-144)) :
   droit de celui qui a lancé, adhésions, machine, âge et FC max du
   pratiquant, autre séance du pratiquant, délai d'attente. Si l'un a changé,
   la machine ne se voit plus offrir le lancement, et la séance passe
   **échouée** avec sa raison en clair sous « Séance échouée » (sur la page de
   la séance : « Ce lancement a été refusé avant que le serveur n'en confirme
   le départ à la machine. ») :

   | Raison affichée | Cause |
   |---|---|
   | Droit retiré avant le départ | Le droit de lancement du patient a été retiré, le gestionnaire ne gère plus la machine ou le patient, une adhésion a été désactivée, ou un compte supprimé. |
   | Le passager n'a pas de saisie d'aptitude valide et approuvée. | `rider_not_fit` : la dernière saisie n'est plus apte, validée et de la version courante. |
   | La saisie d'aptitude du passager a expiré. | `fitness_expired` : l'échéance de validité a été atteinte pendant l'attente. |
   | Le passager n'a pas de consentement actif pour le texte courant. | `consent_missing` : le consentement a été retiré ou porte sur une ancienne version du texte. |
   | Expirée : départ non confirmé à temps | La machine n'en a pas confirmé le départ en 5 min (`PENDING_SESSION_TTL_S`, [convex.md](convex.md#revérification-à-larmement-anh-144)) : machine hors ligne, par exemple. Passée échouée dans la minute qui suit. La console de ce dépôt demande le départ après la confirmation de son opérateur, avant d'armer, n'arme que sur un 2xx et n'envoie pas de fin sur 409 ; une console antérieure, qui arme d'abord, s'arrête aussitôt sur la rampe ordinaire sur un refus. Le lancement n'est plus offert à la machine 45 s avant l'échéance. |
   | FC max du pratiquant modifiée avant le départ | La FC max retenue n'est plus celle du lancement : relancer la séance. |
   | Âge du pratiquant refusé avant le départ | L'année de naissance a été effacée, ou donne moins de 18 ans. |
   | Pratiquant déjà dans une autre séance, en attente ou en cours : une seule à la fois. | Une séance a démarré pour lui sur une autre machine pendant l'attente. |
   | Machine passée dans une autre organisation avant le départ | La machine a changé d'organisation. |
   | Machine supprimée avant le départ | La machine a été supprimée. |

   Une telle séance n'est comptée ni « active » ni « en attente » sur le
   tableau de bord. Une confirmation de départ qui arriverait quand même est
   refusée (409) ; la console de ce dépôt demande le départ après la
   confirmation de son opérateur, avant d'armer, et n'arme que sur un 2xx.
   Sur ce 409, elle n'arme pas et n'envoie pas de fin ; une console
   antérieure, qui arme avant de confirmer, s'arrête aussitôt sur la rampe
   ordinaire. Pour l'aptitude et le consentement (ANH-143), le serveur refuse
   le lancement depuis le site sans aptitude valide ou sans consentement actif.
   Depuis le contrat 1.6, il refuse aussi à l'armement un passager devenu non
   prêt pendant l'attente : `rider_not_fit`, `fitness_expired` ou `consent_missing`.
   La console applique aussi sa porte `cleared_to_board` avant tout départ
   avec personne à bord ([console-locale.md](console-locale.md)).

**Important** : avec la configuration livrée (`raspberry-pi/.env.example`),
`PROGRAMS_ENABLED=false` et `OCCUPANCY_OCCUPIED_ENABLED=false`. Le site affiche
alors « Programmes auto désactivés sur cette machine » et aucun lancement n'est
possible. C'est voulu tant que les validations d'ingénierie et médicales ne sont
pas faites (voir [raspberry-pi.md](raspberry-pi.md)).

### 5.4 Arrêter ou annuler

Bouton rouge du panneau d'entraînement. Visible pour le pratiquant, et pour un
admin / gestionnaire de la machine.

- **En attente** → **Annuler la séance** : confirmée, la séance devient
  « échouée » avec « Cancelled before start by {nom} ».
- **Active** → **Arrêter la séance** : « La machine va décélérer sur sa rampe de
  sécurité. La séance se terminera quand la machine confirmera l'arrêt. » Le
  bandeau « Arrêt demandé, décélération en cours… » reste jusqu'à ce que le Pi
  signale la fin. Le bras tourne encore pendant ce temps.
- **Interrompue, fin inconnue** → **Arrêter la séance** : la même demande. Le
  serveur la garde ; la machine la lit si elle revient avec la séance encore
  en cours, et l'arrête alors sur sa rampe. Le bouton reste grisé, mais le
  bandeau de décélération ne s'affiche pas : on ne sait rien de la machine.

Une séance **manuelle** peut aussi être arrêtée ainsi : le Pi l'arrête sur sa
rampe. En revanche elle ne peut pas être démarrée depuis le site.

**Ce bouton n'est pas un arrêt d'urgence.** L'arrêt d'urgence est à la machine.

### 5.5 Aptitude et consentement avant un lancement

1. Le passager ouvre **Mes machines** et la section **Mon aptitude et mon
   consentement**. Il répond oui ou non à chaque question puis envoie sa saisie.
   Une saisie d'une ancienne version doit être refaite.
2. Un admin ou le gestionnaire du passager ouvre **Patients** → le passager,
   puis **Aptitude**. Il lit les réponses que le serveur lui présente et valide
   le résultat calculé. Pour le résultat « apte », il fixe une date de fin de
   validité ; les autres résultats n'ont pas de date. Un résultat « avis
   médical nécessaire » ne devient apte que par une nouvelle saisie validée.
   Le parcours de l'avis médical relève d'ANH-98.
3. Le passager lit le texte affiché dans **Consentement**, coche « J'ai lu ce
   texte et j'y consens », puis signe avec l'identité de son compte connecté.
   Un gestionnaire ne peut pas signer à sa place.
4. Avant le lancement, **Mes machines** et la fenêtre de lancement indiquent
   la raison traduite si l'aptitude n'est pas valide ou si le consentement
   manque. Le bouton reste désactivé tant que le serveur ne confirme pas que le
   passager peut lancer.
5. Le passager peut retirer son consentement depuis **Mes machines**. Un
   gestionnaire peut le retirer depuis la fiche Patient après confirmation.
   Après retrait, expiration ou changement de version, le prochain lancement
   est refusé avec sa raison.

Le questionnaire 0.1.0 et le texte de consentement 0.1.0 sont provisoires. Ils
ne permettent pas d'accueillir une personne à bord ; voir
[securite.md, section 6](securite.md#6-avant-la-première-personne-à-bord) et
[consentement.md](consentement.md).

---

## 6. Badges, indicateurs et courbes

### Badges

| Badge | Sens |
|---|---|
| **Auto** (icône jauge) | Séance programmée, pilotée par la fréquence cardiaque. |
| **Manuel** (icône main, ambre) | Séance à vitesse fixée par l'opérateur, démarrée à la machine. |
| **Enregistrement** | Ancienne séance ECG seule. |
| **Tableau de bord** / **Machine** | Origine : lancée depuis le site, ou démarrée à la console. |
| **En direct** (point vert) / **Données périmées** (gris) | État de la machine reçu par le serveur il y a moins de 90 s / 90 s ou plus. Périmé : les valeurs sont grisées et « Aucun signal récent de la machine : les valeurs affichées peuvent être dépassées. » Détail d'une machine et Mes machines. Voir « Fraîcheur » ci-dessous. |
| **En ligne** / **En session** / **Hors ligne** | Statut d'une machine. « Hors ligne » dès que son dernier signal a 90 s, à la même seconde que « Données périmées », quel que soit le statut encore écrit dans Convex. Liste des machines, détail d'une machine, Mes machines, fiche d'un gestionnaire. Voir « Fraîcheur » ci-dessous. |
| Statut de séance | En attente, Active, Interrompue, fin inconnue, Terminée, Échouée (les onglets de la liste gardent le pluriel). Un statut inconnu s'affiche tel quel. |

### Fraîcheur recalculée à l'horloge

Une machine qui se tait n'envoie plus rien : aucune donnée ne change, donc
aucune query Convex ne se relance. Le site ne peut pas attendre un changement de
donnée pour dire qu'un état est périmé. Il le recalcule lui-même **chaque
seconde**, sur l'**horloge du serveur**. L'heure du poste ne date rien. Celle
du Pi ne date pas non plus une séance **en cours** de la console de ce dépôt
face à un serveur au contrat 1.1 : la console dit au serveur depuis combien
de temps sa séance a commencé, compté sur son horloge monotone, et le serveur
place lui-même sur son horloge le début, la fin et la mesure de chaque point
([convex.md, Deux horloges](convex.md#deux-horloges)). L'heure du Pi date
encore la mesure du point affiché sur le panneau d'entraînement dans trois
cas, détaillés sous « Horloge du Pi » plus bas : une console d'une version
antérieure, qui ne dit pas cet âge ; un serveur resté au contrat 1.0, qui ne
le lit pas ; et un enregistrement que la console de ce dépôt envoie après un
redémarrage du système, dont elle ne sait plus compter l'âge.

- **Le serveur date ce qu'il reçoit.** L'état d'une machine
  (`live.updatedAt`) et son dernier signal (`lastHeartbeat`) sont datés par
  Convex à la réception du heartbeat. Le dernier signe de vie d'une séance
  active (`lastSignalAt`) est la date à laquelle Convex a **reçu** son dernier
  point de télémétrie (la date de création de la ligne, pas le `t` que le Pi y
  écrit) ; tant qu'aucun point n'est arrivé, c'est le début de la séance daté
  par le serveur (pour une séance démarrée à la console : son enregistrement
  par le serveur, pas le `startedAt` envoyé par le Pi). Avec lui vient
  `lastMeasuredAt` : la date de mesure de ce même point (voir « Panneau
  d'entraînement » plus bas). Pour une séance dont la machine a dit l'âge,
  ce que fait la console de ce dépôt pour toute séance en cours, c'est son
  `t` placé par le serveur sur sa propre horloge ; sinon, c'est le `t` tel
  que le Pi l'a écrit.
- **Chaque réponse porte l'horloge du serveur.** `getMachineLive`,
  `listLaunchableMachines`, `getTrainingSession`, `getMachine`, `listMachines`,
  `getMachinesForGestionnaire` et `dashboard.counters` renvoient `serverNow` :
  l'heure du serveur au
  moment où la réponse est calculée. L'âge d'une donnée est
  `serverNow - date`, plus le temps que le site a **compté** depuis qu'il a vu
  cette réponse pour la première fois (`lib/server-clock.ts`). L'horloge du
  poste ne sert qu'à compter ce temps, par différence : qu'elle avance ou
  retarde de dix minutes ne change rien. Une réponse déjà vue (par un autre
  composant, ou redonnée après une reconnexion) garde la date de sa première
  apparition.
- **Un seul hook**, `useFreshness` (`hooks/use-freshness.ts` ;
  `useFreshnessJudge` pour juger plusieurs données sur une même lecture
  d'horloge), rend ce verdict chaque seconde (`useLocalClock(1000)`). Sans
  `serverNow`, rien n'est frais.
- **Deux seuils**, définis dans `lib/training.ts` : `LIVE_FRESH_MS` = 90 s pour
  l'état et le signal d'une machine, importé aussi par `convex/training.ts` et
  par la tâche `checkOfflineMachines` de `convex/machines.ts` (le serveur et le
  site ne peuvent pas diverger) ; `TELEMETRY_FRESH_MS` = 20 s pour le dernier
  signe de vie d'une séance active.
- Lisent ce hook, et rien d'autre : la carte « État en direct », chaque carte
  de Mes machines, le panneau d'entraînement, et tout ce qui affiche le statut
  d'une machine ou son dernier signal (`components/machines/MachineSignal.tsx` :
  liste des machines, détail d'une machine, fiche d'un gestionnaire, carte
  « Machines en ligne » du tableau de bord). Le badge « En direct » et le
  statut « En ligne » ne sont jamais affichés sans lui.

| Moment | Ce que le site affiche |
|---|---|
| Moins de 90 s après le dernier signal | Statut écrit dans Convex (« En ligne » ou « En session »), badge « En direct », valeurs normales. |
| 90 s après le dernier signal (page déjà ouverte : à la seconde près ; page qui vient de s'ouvrir : jusqu'à 107 s, voir les limites) | Statut « Hors ligne », badge « Données périmées », valeurs grisées, cœur gris, « Aucun signal récent de la machine : les valeurs affichées peuvent être dépassées. » La machine ne compte plus dans « Machines en ligne ». Sur Mes machines, le bouton de lancement est grisé, avec « La machine est hors ligne. ». « Dernier signal » et « Mis à jour il y a … » continuent d'avancer. |
| Signal suivant reçu (heartbeat toutes les 10 s) | Retour immédiat au statut écrit et à « En direct ». |

**Statut affiché et statut écrit.** Convex n'écrit `offline` sur une machine
qu'au passage de sa tâche, une fois par minute : entre 1,5 et 2,5 min après le
dernier signal. Le site n'attend pas : `shownMachineStatus` (`lib/training.ts`)
affiche « Hors ligne » dès que le dernier signal n'est plus frais. « En ligne »
et « Données périmées » ne s'affichent plus ensemble que dans un cas, qui est
vrai : une machine qui envoie encore ses signaux, mais plus son état.

**Textes sans état.** Sur le détail d'une machine, le verdict `stale` du
serveur compte aussi quand il dit « périmé ». Sur Mes machines, le serveur
retire l'état quand la query se relance après 90 s : la carte affiche alors
« Aucun état en direct : la machine n'envoie plus de signal. ». « La machine
n'a encore rapporté aucun état. » reste le texte d'une machine dont le site n'a
aucun état à montrer sans qu'elle soit hors ligne, ou qui ne s'est jamais
connectée.

Limites :

- **Page qui vient de s'ouvrir : jusqu'à 107 s pour une machine, 37 s pour le
  panneau.** Le site date une réponse du moment où il la traite, pas du moment
  où le serveur l'a calculée : tout délai entre les deux fait paraître la
  donnée plus jeune d'autant, et retarde d'autant « Données périmées »,
  « Hors ligne » ou le bandeau du panneau. Trois délais s'ajoutent :
  - le cache de queries de Convex : une page qui s'ouvre peut recevoir une
    réponse calculée un peu plus tôt. D'après le code source ouvert de Convex,
    une réponse qui a lu l'heure n'est resservie que pendant 17 s au plus
    (réglage par défaut), d'où 90 + 17 = 107 s et 20 + 17 = 37 s. Ce n'est pas
    un engagement de Convex, et rien n'a été mesuré sur un déploiement ;
  - le temps de transit de la réponse jusqu'au navigateur, non mesuré ;
  - un onglet suspendu par le navigateur alors que sa connexion reste
    ouverte : la réponse attend d'être traitée. Non mesuré, non borné.

  Sur une page déjà ouverte qui reçoit les réponses au fil de l'eau, seul le
  transit compte, et le verdict tombe à la seconde près. Dans l'autre sens il
  n'y a pas d'erreur possible : une réponse n'arrive pas avant d'avoir été
  calculée.
- **Horloge du poste réglée pendant l'affichage.** Reculée : sans effet, le
  compteur monotone du navigateur continue. Avancée d'un coup : le site ne
  distingue pas ce saut d'une mise en veille, le compte comme du temps écoulé
  et affiche « Hors ligne » ou « Données périmées » à tort, jusqu'à la réponse
  suivante (10 s au plus pour une machine qui envoie).
- **Horloge du Pi.** Elle ne date ni l'état d'une machine, ni son statut, ni
  la réception d'un point. Quand la console dit l'âge de sa séance et que le
  serveur le lit, elle ne date pas la mesure non plus : le serveur la place
  sur sa propre horloge à partir de cet âge, exact au délai d'acheminement de
  la déclaration près (toute mesure de la séance est lue plus récente de ce
  délai : d'ordinaire une fraction de seconde, sans borne côté serveur,
  [convex.md](convex.md#ce-quun-âge-faux-peut-faire-et-ce-qui-le-borne)), et
  une horloge du Pi fausse, ou corrigée en cours de séance, ne change rien à
  ce qui est affiché. C'est le cas de toute séance **en cours** de la console
  de ce dépôt, et de ce qu'elle envoie après coup tant que le système du Pi
  n'a pas redémarré, face à un serveur au contrat 1.1.

  L'horloge du Pi date encore la **mesure** du point affiché sur le panneau
  d'entraînement (voir plus bas) dans trois cas :

  - une console d'une version antérieure, qui ne dit pas l'âge de sa séance ;
  - un serveur resté au contrat 1.0, qui ne lit pas cet âge, quelle que soit
    la console ;
  - un enregistrement que la console de ce dépôt envoie **après un
    redémarrage du système** et dont la date est en 2024 ou après : elle ne
    sait plus compter son âge, n'en dit aucun, et la date écrite par la
    machine reste ([raspberry-pi.md §8.5](raspberry-pi.md#85-les-dates)).
    Un cas a été mesuré en revue : si l'horloge de la machine avançait alors
    d'exactement le retard de cet envoi, ses points sont lus comme mesurés à
    l'instant, et la séance s'affiche en direct jusqu'à l'arrivée de sa fin,
    soit le temps de l'envoyer (un lot de 300 points toutes les 3 s).

  Dans ces trois cas, les conséquences sont :
  - en retard de plus de 15 s environ : le bandeau « mesures pas datées de
    maintenant » apparaît par intermittence alors que la machine envoie ; en
    retard de 20 s ou plus, en permanence. Sens sûr ;
  - en avance de plus de 5 s environ : même bandeau, en permanence (un point
    daté après sa propre réception n'est jamais cru). Sens sûr ;
  - en avance de X **et** points reçus en retard (renvoi après une coupure) :
    un point peut être affiché comme actuel jusqu'à 20 s + X après sa mesure.
    C'est la seule erreur du Pi dans le sens dangereux ; elle demande les deux
    défauts à la fois ;
  - qui recule pendant une séance : le point de plus grand `t` reste un ancien
    point, le panneau affiche « Aucun signal récent » à tort jusqu'à ce que
    l'horloge ait rattrapé son ancienne valeur. Sens sûr.

  Dans tous ces cas le bandeau signale un vrai défaut de la machine, à faire
  corriger. La fiabilité de l'horloge du Pi est ANH-163. Elle date aussi l'axe
  des courbes et le début (donc le chronomètre) d'une séance démarrée à la
  console.
- **Lancement.** Le refus d'un lancement par le serveur lit le statut écrit, et
  la fenêtre de lancement aussi : ouverte depuis le détail d'une machine muette
  depuis moins de 2,5 min, elle peut encore proposer le lancement, que le
  serveur accepte (ANH-144).
- **Convex antérieur à cette version.** Ses réponses n'ont pas `serverNow` :
  le site affiche alors toutes les machines « Hors ligne » et toutes les
  données périmées. Convex se déploie avant le site
  ([deploiement.md](deploiement.md)).

### Panneau d'entraînement (vue en direct)

Cinq gros indicateurs :

| Indicateur | Contenu |
|---|---|
| Fréquence cardiaque | Valeur du dernier point, **s'il est actuel** : sur une séance active, reçu par le serveur **et** mesuré depuis moins de 20 s (règle ci-dessous ; les trois indicateurs de mesure la suivent). Couleur : vert « Dans la zone », bleu « Sous la zone », rouge « Au-dessus de la zone ». « - » et « Pas de fréquence cardiaque fiable » si le Pi n'en a pas. |
| Vitesse du bras | tr/min bras mesurés, et tr/min moteur. |
| Charge | g au rayon configuré sur le Pi. |
| Phase | Mesure de référence, Échauffement, Maintien, Retour au calme, Récupération, Terminé ; avec l'action de sécurité si elle n'est pas « Aucune ». |
| Écoulé / Restant | Chronomètre et temps restant de la durée prévue. |

Bandeaux : en attente, arrêt demandé, séance échouée (avec motif), séance
terminée (avec motif), séance interrompue, fin inconnue (à la place du bandeau
« Aucun signal récent… » : rien n'y est donné pour actuel), et pour le manuel
« Manuel : uniquement depuis la console de la machine ».

**Quand une valeur est actuelle.** Sur une séance active, le panneau n'affiche
la fréquence cardiaque, la vitesse et la charge du dernier point (celui de plus
grand `t`) que si **les deux** dates de ce point sont récentes sur l'horloge du
serveur, au seuil de la télémétrie : 20 s (`TELEMETRY_FRESH_MS`,
`lib/training.ts` ; le Pi envoie ses points toutes les 5 s) :

1. **sa réception** (`lastSignalAt`, que `getTrainingSession` renvoie avec la
   séance) : la date à laquelle le serveur l'a reçu, ou le début de la séance
   daté par le serveur tant qu'aucun point n'est arrivé ;
2. **sa mesure** (`lastMeasuredAt` : le `t` du point, placé sur l'horloge du
   serveur pour une console qui dit l'âge de sa séance, tel que le Pi l'a
   écrit sinon) :
   moins de 20 s avant l'heure du serveur, et pas plus de 5 s **après** sa
   propre réception (`FUTURE_TOLERANCE_MS`, `datedAfterReception` : un point
   n'est pas mesuré après avoir été reçu ; une telle date vient d'une horloge
   en avance, et ne redevient pas crédible quelques secondes plus tard).

La réception seule ne suffit pas : après une coupure de liaison, ou après son
propre redémarrage, la console relit sur son disque ce que le serveur n'a pas
acquitté et l'envoie par paquets de 300 points, **les plus anciens d'abord**,
un paquet toutes les 3 s. Chaque paquet est une réception fraîche de mesures
anciennes. Le panneau relit aussi le `t` du point qu'il imprime, quoi que
dise la séance.

L'horloge du poste n'entre dans aucun de ces deux verdicts. Ils arrivent avec
la séance : à l'ouverture de la vue, aucun bandeau n'apparaît le temps que les
points se chargent.

| Situation | Ce que le panneau affiche |
|---|---|
| Point reçu et mesuré depuis moins de 20 s | Les valeurs, sans bandeau. |
| Aucun point reçu depuis 20 s (machine muette) | Bandeau orange « Aucun signal récent de la machine : les valeurs affichées peuvent être dépassées. » |
| Points reçus, mais mesurés il y a 20 s ou plus, ou datés de plus de 5 s après leur réception | Bandeau orange « La machine envoie, mais ses mesures ne sont pas datées de maintenant (rattrapage après une coupure de liaison, ou horloge de la machine déréglée) : les valeurs affichées peuvent être dépassées. » Il reste jusqu'au premier point mesuré depuis moins de 20 s : pendant tout un rattrapage, et en permanence si l'horloge de la machine est déréglée. |

Sous l'un ou l'autre bandeau :

- les cinq indicateurs sont grisés ; fréquence cardiaque, vitesse du bras et
  charge passent à « - » ; la phase garde sa dernière valeur connue, grisée ;
- l'icône du titre ne tourne plus ;
- les courbes continuent de se compléter avec les points reçus.

Le chronomètre continue, sur l'horloge du serveur : la séance reste « Active »
côté serveur et la machine suit ses propres règles. Au premier point actuel,
tout revient. Une séance terminée ou échouée n'est pas concernée : ses
dernières valeurs sont son résultat.

### Courbes (`components/training/TelemetryCharts.tsx`)

- **Fréquence cardiaque** : la FC dans le temps, avec la **zone cible** en bande
  verte. Une FC absente reste un **trou** dans la ligne : l'absence n'est jamais
  dessinée comme une valeur.
- **Vitesse du bras** : mesurée (ligne pleine) et consigne (tirets, convertie en
  tr/min bras par le rapport 49,79).
- Axe du temps en minutes:secondes depuis le début. Au plus les 3600 derniers
  points (1 h à 1 Hz).

Le site ne recalcule aucune mesure : il affiche ce que le Pi envoie. L'âge des
données, lui, est calculé par le site (« Fraîcheur » plus haut). Voir le
[glossaire](glossaire.md) pour « zone », « g résultant », « consigne ».

### Messages de succès et d'échec

Aucune **mutation** ni **action** Convex du site n'échoue en silence. Cela ne
vaut que pour elles : une lecture Convex qui échoue, la copie d'une clé dans le
presse-papiers ou un geste de la page qui n'écrit rien ne passent pas par ce
mécanisme et ne sont pas couverts.

Toute mutation Convex appelée depuis `app/`, `components/`, `lib/` ou `hooks/`
passe par le hook `useMutationWithFeedback`
(`hooks/use-mutation-with-feedback.ts`), et toute action par
`useActionWithFeedback` (`hooks/use-action-with-feedback.ts`) : ils exécutent
l'appel, ne lèvent jamais d'exception (même quand le texte de succès fourni
par l'appelant ne peut pas être composé : le succès s'affiche alors comme
« Action effectuée. ») et rendent `{ ok: true, value }` ou
`{ ok: false, message, code, requestId }`. Le message s'affiche en bas à
droite de l'écran (`components/FeedbackToaster.tsx`, monté dans
`app/[locale]/layout.tsx`), au-dessus des fenêtres. Un succès part seul au
bout de 5 s ; un échec reste jusqu'à ce qu'on le ferme par sa croix, car il
est parfois le seul endroit où son motif s'affiche. Au plus quatre messages
sont visibles : un succès plus ancien part avant un échec. La croix reste
cliquable quand une fenêtre est ouverte, sans fermer la fenêtre
(`components/ui/dialog.tsx` ne prend pas ce clic pour un clic à l'extérieur).
Les succès sont annoncés dans une région `role="status"` (polie), les échecs
dans une région `role="alert"` (immédiate) ; les deux existent avant le
premier message. Pendant qu'une fenêtre modale est ouverte, Radix masque le
reste de la page aux lecteurs d'écran, ces deux régions comprises
(comportement documenté par Radix, non vérifié ici) : les fenêtres qui
affichent leur propre encadré d'erreur le donnent aussi dans la fenêtre.

| Cas | Ce qui s'affiche |
|---|---|
| Succès | Le texte fourni par l'appelant, en français ou en anglais (`feedback.*`, `machines.*Success`, `gestionnaires.machinesSaved` et `gestionnaires.patientsSaved` dans `messages/*.json`). L'appelant donne un texte, ou une fonction qui le compose à partir de la réponse de la mutation (le décompte de « Machines enregistrées : … » et de « Patients enregistrés : … »). Une mutation de fond (création de la ligne du compte à l'arrivée sur la page d'accueil) n'en fournit pas : son succès reste muet, son échec non. La modification d'une machine par un admin annonce « Machine mise à jour avec succès » une fois ses deux appels passés, jamais pour la moitié. |
| Échec avec un code stable | La traduction du code, clé `errors.<code>` de `messages/*.json`. Le code est lu dans la `ConvexError` du serveur : la chaîne elle-même, ou le champ `code` (à défaut `error`) d'un objet. Les codes des fonctions du site sont dans une seule table, `convex/lib/errors.ts` (voir [convex.md](convex.md)) ; chacun a sa traduction française et anglaise. |
| Échec avec un texte | Sans traduction du code, le texte du serveur : la chaîne de la `ConvexError`, le champ `message` de l'objet, ou sur un déploiement de développement la ligne « Uncaught Error: … ». Ces textes sont encore en anglais. |
| Échec masqué | En production, Convex masque le texte d'une erreur qui n'est pas une `ConvexError` (comportement documenté par Convex, pas encore observé sur ce projet). Le site affiche alors « L'action a échoué. Réessayez. Référence à transmettre si le problème persiste : {identifiant}. » |

Chaque échec est aussi écrit dans la console du navigateur avec le nom de la
mutation, son code et l'identifiant de requête Convex, celui qui permet de
retrouver l'erreur dans les journaux du déploiement.

Les fenêtres et les cartes qui affichaient déjà l'erreur dans un encadré rouge
(lancement, arrêt, droits de lancement, physiologie, formulaires machine et
patient, fenêtres « Assigner des machines » et « Assigner des patients ») le
gardent : il reçoit le même message que la notification. Un succès, lui,
n'est dit qu'une fois, par la notification : la fiche d'un gestionnaire, la
fiche d'une machine et la carte Physiologie n'ont plus leur propre ligne de
confirmation (ANH-194).

**Arrêter ou annuler une séance** donne un seul message, quel que soit l'état
que la page affichait : « Demande envoyée. L'état de la séance s'affiche sur
cette page. » `training.requestStop` répond de la même façon qu'il ait annulé
une séance en attente, demandé l'arrêt d'une séance active ou trouvé la séance
déjà finie, et la machine peut avoir armé la séance pendant l'envoi : la page ne
peut donc pas affirmer « annulée » ou « arrêt demandé ». Ce sont les bandeaux du
panneau qui disent ce qui s'est passé.

Limites :

- les refus de `convex/machines.ts`, de `convex/users.ts` et des contrôles de
  rôle de `convex/lib/auth.ts` (`requireRole`, `requireGestionnaireAdmin`,
  profil partagé) portent un code traduit depuis ANH-194 ; depuis ANH-90,
  ceux de la connexion, de l'organisation, de tout `convex/training.ts`
  (droits de lancement, physiologie, droits du lanceur, machine hors ligne
  ou occupée, programme, pratiquant, zone, âge, durée, arrêt), du registre
  des versions et des alertes aussi. Une session expirée affiche donc « Votre
  session a expiré ou vous n'êtes plus connecté. Reconnectez-vous pour
  continuer. », en production comme en développement, au lieu d'une phrase
  générique avec une référence. Plus aucun refus du serveur n'arrive en
  anglais ; sur la page des alertes, les identifiants connus des raisons de
  fin sont traduits, en conservant leur éventuel complément. Le texte libre
  reste tel quel ; les raisons brutes des pages de séance restent en suivi ;
- le test de bout en bout dans un navigateur (suppression refusée d'une machine
  en séance) attend l'infrastructure d'ANH-83.

Tests : `hooks/use-mutation-with-feedback.test.tsx` (succès, texte de succès
qui ne peut pas être composé, erreur codée, erreur brute, erreur masquée,
journal), `lib/feedback.test.ts` (durée de vie et limite de la liste),
`components/feedback-over-dialog.test.tsx` (la croix d'un message ferme le
message et pas la fenêtre ouverte dessous, avec le vrai composant Radix),
`components/training/stop-feedback.test.tsx` (le message d'arrêt est le même
que la page ait cru la séance en attente, active ou finie),
`convex/errors.test.ts` (chaque code de `convex/lib/errors.ts` a ses deux
traductions, aucune traduction sans code ; recherche textuelle des codes
écrits en place dans `new ConvexError(...)`, entre guillemets doubles et avec
`code` en tête si c'est un objet ; `refusal(...)` contraint par son type,
`machineError(...)` régi par le contrat machine et hors de cette recherche) et
`hooks/no-silent-mutation.test.ts`, qui refuse dans `app/`, `components/`,
`lib/` et `hooks/` un appel direct à `useMutation` ou à `useAction` (hors des
deux hooks qui les enveloppent). La règle textuelle historique refuse tout
`.mutation(...)` ou `.action(...)` dans un fichier mentionnant `useConvex`,
`ConvexReactClient` ou `ConvexHttpClient`, quel que soit le receveur. En
complément, l'analyse syntaxique suit les clients initialisés par ces hooks ou
constructeurs, ainsi que les variables, paramètres et propriétés annotés
directement `ConvexReactClient` ou `ConvexHttpClient`. Elle refuse l'accès à
`mutation` et `action` avant même leur appel : alias
de méthode, `bind`, `call`, `apply`, chaînage optionnel, crochets à clé
littérale et déstructuration compris, avec ou sans espaces ou retours à la
ligne après le point ; `.query` reste accepté. Il refuse aussi un handler
vide ou réduit à des appels `console` dans un fichier utilisant ces hooks.
Le contrôle reste local et sans analyse de types : ses limites incluent les
clés calculées depuis une variable, les imports renommés, les clients sans
initialisation ni annotation directe reconnue et le masquage lexical des noms
de variables ou de propriétés. La règle textuelle continue de refuser les
appels directs hors de ce suivi ; cette liste n'est pas exhaustive.

---

### Enregistrements complets (ANH-130)

La fiche d'une séance d'entraînement affiche son dépôt : absent, en cours,
complet et vérifié, rejeté ou supprimé, avec le nombre de fichiers vérifiés.
Les administrateurs et gestionnaires autorisés peuvent consulter les fichiers
par pages et les télécharger. L'archive `.tar` n'est proposée qu'après
vérification de tout l'inventaire. Une annulation interrompt les téléchargements
du navigateur. Une perte d'accès ou une empreinte incorrecte produit un échec,
jamais une archive présentée comme complète.

« Ouvrir dans le visualiseur » devient disponible quand le manifeste, les ticks
et les événements sont vérifiés. La nouvelle fenêtre reçoit les données, sans
jeton d'accès. Les valeurs absentes restent inconnues, et une géométrie absente
est explicitement signalée. Les blocs bruts sont dans l'archive, pas chargés
automatiquement par ce visualiseur de séance.

Les rôles `admin` et `org_admin` disposent d'une suppression avec confirmation.
Le serveur vérifie à nouveau leur rôle et leur accès à la machine. Les fichiers
sont supprimés et les téléchargements révoqués ; la séance et la télémétrie
restent consultables. La rétention cloud est optionnelle et désactivée par
défaut, voir [enregistrement.md](enregistrement.md#dépôt-vérifié-dans-convex).

## 7. Lancer le site en local

Prérequis : Node.js, les dépendances (`npm ci`), un
compte Convex et une application Clerk.

Scripts de `package.json` :

| Commande | Effet |
|---|---|
| `npm run dev` | `predev` : `convex dev --until-success && convex dashboard`, puis Next.js (`next dev`) et `convex dev` en parallèle. |
| `npm run build` / `npm run start` | Build et serveur de production Next.js. |
| `npm run lint` | ESLint sur tout le dépôt. |

Variables d'environnement :

| Variable | Où | Rôle |
|---|---|---|
| `NEXT_PUBLIC_CONVEX_URL` | `.env.local` (écrite par `npx convex dev`) | URL `.convex.cloud` du déploiement, lue par `components/ConvexClientProvider.tsx`. |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY` | `.env.local` | Clés Clerk, lues par `@clerk/nextjs` (non citées dans le code du dépôt). |
| `CLERK_JWT_ISSUER_DOMAIN` | variables **du déploiement Convex** | Émetteur du modèle JWT `convex` de Clerk (`convex/auth.config.ts`). |

Pièges :

- `npm run lint` parcourt aussi `raspberry-pi/.venv` et
  `simulation/cad/.venv-cad` : sur 275 erreurs relevées, 273 viennent de
  fichiers JavaScript de ces environnements Python ; les 2 autres étaient dans
  un composant de l'ancien bloc ECG, retiré depuis.
- Aller directement sur `/fr/dashboard` sans être passé par la page d'accueil
  laisse le compte sans ligne Convex : les pages restent vides.

---

## 8. Ce qui n'est pas vérifié ou pas fait

| Sujet | État |
|---|---|
| Rendu dans un navigateur | **Jamais testé.** |
| Journal Audit (ANH-167) | Accès, filtres, pagination et export vérifiés par les tests de page et de composants de `app/[locale]/dashboard/audit/page.test.tsx` ; format CSV et neutralisation des formules par `lib/auditCsv.test.ts`. La vérification de bout en bout de la page accessible et filtrable dans un navigateur est **bloquée par ANH-83**, infrastructure E2E elle-même bloquée par ANH-82. |
| Déploiement Convex / Clerk | **Pas fait.** |
| Tests du site | Unitaires seulement, sans navigateur : `npm run test:lib` (règles de `lib/` extraites des fenêtres, horloge du serveur, retrait de l'ancien mode ECG) et `npm run test:site` (fraîcheur, statut et dernier signal : hook et composants, avec l'horloge du poste et celle du Pi décalées ; retour des mutations : hook, message d'arrêt et garde-fou des erreurs silencieuses ; chaque composant : panneau de séance et arrêt, fenêtre de lancement, droits de lancement, formulaires machine et patient, menu par rôle ; chaque page de `app/` montée dans un DOM simulé, Convex remplacé : ce qu'elle montre à chaque rôle, ce que ses actions envoient, ce qu'elle affiche quand le serveur refuse, voir [framework-de-test.md](framework-de-test.md#tests-des-pages-du-site-anh-204)). La CI exige 80 % de lignes et de branches couvertes sur `lib/`, `hooks/`, `components/` et `app/` réunis. **Aucun test dans un navigateur** : ni la coupure d'une console simulée suivie de 90 s d'attente, ni une suppression refusée par un vrai serveur ne sont rejouées de bout en bout (ANH-83). |
| Fraîcheur et horloges | Jugée sur l'horloge du serveur (§6) : l'horloge du poste ne date rien. Pire cas chiffré sur une page qui vient de s'ouvrir : 107 s pour une machine, 37 s pour le panneau (cache de Convex, 17 s d'après son code source), plus le transit de la réponse et un onglet suspendu, **non mesurés**. Panneau, quand l'horloge du Pi date la mesure (console d'une version antérieure, serveur au contrat 1.0, ou enregistrement envoyé après un redémarrage du système) : en retard de plus de 15 s environ, ou en avance de plus de 5 s environ, elle affiche le bandeau alors que la machine envoie (sens sûr) ; en avance de X, un point renvoyé en retard peut passer pour actuel jusqu'à 20 s + X après sa mesure (ANH-163). Pour une séance en cours de la console de ce dépôt face à un serveur 1.1, l'horloge du Pi ne date pas la mesure. |
| Statut « En ligne » et « Dernier signal » | Recalculés chaque seconde par le site (§6). Convex n'écrit `offline` qu'au passage de sa tâche (jusqu'à 2,5 min) : le refus d'un lancement par le serveur et la fenêtre de lancement lisent encore ce statut écrit (ANH-144). |
| Lancement auto de bout en bout (site → Convex → Pi → moteur) | **Jamais exécuté.** Le contrat HTTP est testé de chaque côté séparément : côté Pi contre un faux transport, côté Convex dans `convex/httpRoutes.test.ts`. |
| Invitation des patients par e-mail | Implémentée avec Clerk Organizations et liaison par webhook. La recette doit être effectuée sur l'instance configurée avant déploiement. |
| Compteurs du tableau de bord | Calculés par `dashboard.counters` et vérifiés par des tests Convex (deux organisations) et des tests de la page sans navigateur. Leur affichage dans un navigateur, avec une console simulée, relève de la suite de bout en bout ANH-83. Coût des lectures non mesuré sur un déploiement ([convex.md](convex.md#dashboardts-compteurs-du-tableau-de-bord-anh-159)). |
| Libellés des actions de sécurité et de l'état du variateur | Traduits (`freeze`, `quick_stop`, `go_silent` compris). Le vocabulaire français (« Vitesse figée », « Arrêt rapide (rampe du variateur) », « Mise en silence (arrêt par le variateur) »…) reste à relire par l'équipe. |
| Pratiquant d'une séance démarrée à la machine | Corrigé dans le code le 15 octobre 2026 (ANH-84), à vérifier au redéploiement ANH-82 : la console envoie le passager choisi dans la liste de la machine (`userId`, et son nom tant qu'elle l'a en mémoire). La séance apparaît à son nom et devient visible du patient, s'il a toujours le droit sur la machine quand elle est déclarée. Une séance de banc, sans personne à bord, n'a pas de pratiquant et reste « Unknown ». En production jusqu'au redéploiement : « Unknown » pour toutes. |
| Rapport d'une séance d'entraînement (ANH-89, ANH-234) | Page et courbes testées ; PDF A4 produits le 22 octobre 2026 avec Chromium, en français et en anglais, thème clair et sombre, depuis une fenêtre de bureau et une fenêtre étroite, avec et sans arrière-plans. Le vrai CSS, la vraie page et le layout du tableau de bord sont utilisés ; Clerk et Convex sont simulés, les données sont fictives et Arial remplace Geist. Les pages rendues en images et le texte extrait vérifient la largeur des courbes, le fond clair et l'absence de navigation. Ce banc local ne prouve pas un déploiement ni l'impression depuis une vraie séance. Le calcul du résumé est testé en mémoire, et comparé le 14 octobre 2026 aux chiffres de la simulation sur quatre scénarios ([convex.md](convex.md#session_summaries_v2--résumé-dune-séance-dentraînement-anh-89)) ; jamais sur l'enregistrement d'une vraie séance. |
| Pages retouchées au retrait de l'ancien mode ECG (Sessions, vue en direct, détail, Rapports, fenêtre machine) | Compilées et couvertes par `npm run test:lib` ; **pas rouvertes dans un navigateur** depuis. |
| Pages Sessions et Rapports servies par pages (ANH-158) | Le serveur est testé en mémoire (`convex/sessionLists.test.ts`), les deux pages dans le DOM simulé, où Convex est remplacé par une doublure qui sert les pages d'une liste. **Pas ouvertes dans un navigateur, ni contre un vrai serveur** : ce que deviennent des pages déjà affichées quand une séance arrive ou change de statut pendant la lecture repose sur Convex et n'est pas rejoué. |
| Textes encore en anglais | Nom « Unknown », motif « Cancelled before start by … », raison de fin brute sur les pages de séance (en suivi). Dans les alertes, l'identifiant connu est traduit ; son complément et le texte libre restent tels quels. Fiches d'un administrateur ou d'un gestionnaire, erreurs de saisie du formulaire patient, confirmation de suppression d'un compte. |
| Alertes aux gestionnaires (ANH-90) | Écrites et lues par Convex, testées en mémoire ; la page et la cloche testées dans le DOM simulé. **Jamais vues dans un navigateur ni contre un vrai serveur** : le délai de moins d'une minute repose sur le heartbeat de 10 s de la console et l'abonnement de Convex, **non mesuré**. **Aucun e-mail n'a été envoyé** : le compte Resend, sa clé, le domaine d'envoi et l'adresse du site sont à fournir par le responsable produit ([convex.md §8](convex.md#envoi-de-mails-anh-90-anh-237)). Les alertes d'un incident attendent ANH-145. |
| Sélecteur d'organisation, page Membres | Composants de Clerk, couverts par des tests unitaires qui les remplacent. Le changement d'organisation de bout en bout (deux organisations, données qui changent) relève du test navigateur ANH-83 et de la recette sur l'instance Clerk de développement. |
| Compte avec FC max ou année de naissance renseignée | Risque d'échec de `users.getCurrentUser` (validateur incomplet), donc de pages vides pour ce compte. **À vérifier en premier** sur un déploiement. Voir [convex.md §9](convex.md#9-défauts-connus-et-reste-à-faire). |
| Questionnaire d'aptitude | Le questionnaire 0.1.0 contient des questions provisoires [MED] ; le médecin doit les remplacer par le contenu signé (ANH-98). |
| Texte de consentement | Le texte 0.1.0 est provisoire et non valide juridiquement ; il doit être remplacé après ANH-87b. Les avis d'hébergement et de rétention des réponses restent à traiter dans ANH-172. |
| Signature à la console | La signature est disponible sur le site seulement. La console ne propose pas de signer un consentement. |
| Aptitude et consentement à l'armement | Depuis le contrat 1.6, le serveur les contrôle au lancement depuis le site et à l'armement. Un passager devenu non prêt pendant l'attente est refusé avec sa raison (ANH-143). Tests en mémoire, jamais sur une vraie machine. |
| Aptitude et consentement au départ de la console | Depuis le contrat 1.6, la console les contrôle avant tout départ avec personne à bord, pour un programme ou une séance manuelle `occupied`. Tests avec variateur simulé, jamais sur une vraie machine ; limites de fraîcheur dans [console-locale.md](console-locale.md). |

Pour la sécurité d'ensemble, voir [securite.md](securite.md).

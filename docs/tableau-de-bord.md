# Le tableau de bord distant (site Next.js)

Le site (`app/`, `components/`, `lib/`, `messages/`) est le **tableau de bord
distant** d'Anheart (la marque affichée est « Gaura »). Il permet de gérer les
comptes et les machines, de **lancer une séance auto**, de la suivre en direct
et de l'arrêter. Il lit et écrit dans Convex (voir [convex.md](convex.md)) ; il
ne parle **jamais** directement au Raspberry Pi.

> **État réel.**
> - Le code se compile : `npx tsc --noEmit -p .` ne signale aucune erreur.
> - Les fonctions ECG du site ont des tests numériques synthétiques :
>   `npm run test:ecg` ([portée et limites](framework-de-test.md#régression-ecg-du-navigateur-anh-71)).
>   La même commande exécute les tests unitaires des règles de `lib/`
>   extraites des fenêtres du site.
>   La fraîcheur de l'état en direct (§6) a des tests unitaires, sans
>   navigateur : `npm run test:site`
>   ([portée et limites](framework-de-test.md#fraîcheur-de-létat-en-direct-anh-160)).
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

Trois rôles, stockés dans Convex (`users.role`). Les libellés affichés sont
« Administrateur », « Gestionnaire », « Patient ».

| Rôle | Peut |
|---|---|
| **admin** | Tout voir et tout gérer. Créer les machines. Changer les rôles. Gérer les gestionnaires. Lancer une séance auto sur toute machine, pour lui-même ou pour n'importe quel patient. |
| **gestionnaire** | Voir et gérer **ses** machines (liées par l'admin) et **ses** patients. Créer des patients. Régler leur physiologie. Accorder / retirer le droit de lancement sur ses machines, **à ses patients**. Lancer une séance auto sur ses machines, pour lui-même ou un de ses patients. |
| **user** (patient) | Voir ses propres séances et rapports. Lancer une séance auto **pour lui-même seulement**, sur les machines où un gestionnaire ou un admin lui a donné le droit. Arrêter sa propre séance. |

**Qui donne le droit de lancement à qui :**

```
admin ──────────────► n'importe quel patient, sur n'importe quelle machine
gestionnaire ───────► un patient qu'il gère, sur une machine qu'il gère
patient ────────────► personne
```

Le droit n'est donné qu'à un compte de rôle `user` ; admins et gestionnaires
l'ont déjà par leur rôle.

**Protection des pages.** `proxy.ts` (middleware Clerk + next-intl) exige une
connexion pour `/{locale}/dashboard/...`. Les contrôles par rôle se font dans
Convex : une page ouverte sans le bon rôle reçoit des listes vides ou un refus.
Quelques pages affichent aussi un message « accès refusé » (voir §4).

**Premier compte.** À la première connexion, un compte reçoit le rôle `user`.
La ligne Convex n'est créée que lorsque l'utilisateur connecté passe par la
**page d'accueil** (bouton « Accéder au tableau de bord »). Il n'y a pas
d'amorçage automatique du premier admin : il faut le nommer dans le tableau de
bord Convex (voir [convex.md §8](convex.md#8-déployer)).

---

## 3. Navigation

Adresses : toujours préfixées par la langue, `/fr/...` (défaut) ou `/en/...`.

Barre latérale (`components/dashboard/AppSidebar.tsx`) :

| Section | Entrées | Visible pour |
|---|---|---|
| Principal | Tableau de bord | tous |
| Administration | Gestionnaires, Utilisateurs, Paramètres | admin |
| Gestion | Patients, Machines, Mes machines, Sessions, Rapports | admin, gestionnaire |
| Ma Santé | Mes machines, Sessions, Rapports | patient |

En haut : un fil d'Ariane, le sélecteur de langue et le thème clair / sombre.

---

## 4. Les pages, une par une

### Pages publiques

| Adresse | Contenu |
|---|---|
| `/fr` (accueil) | Page de présentation « Gaura » (principe, fréquence cardiaque, cas d'usage, bénéfices, marchés). Boutons Clerk « Commencer » (inscription) et « Se connecter ». Connecté : « Accéder au tableau de bord », qui crée aussi la ligne `users` si besoin. |
| `/fr/faq` | Questions fréquentes. |
| `/fr/privacy`, `/fr/terms` | Confidentialité, conditions. |

Le texte d'accueil et la FAQ ne citent aucun chiffre d'intensité : « L'intensité
se règle par la vitesse de rotation, dans les limites fixées par le logiciel de
la machine. » Pour mémoire, le plafond logiciel est la vitesse nominale moteur
(27,7 tr/min bras, environ 2,1 g au bout des jambes), et beaucoup moins avec une
personne à bord. Voir [securite.md](securite.md). Tout chiffre publié sur le site
dépend de la décision médicale (ANH-98). La FAQ nomme les six canaux du
logiciel : ECG, EDA, SpO2, RESP, EMG et LUX.

### Tableau de bord : `/fr/dashboard`

Pour tous. Titre « Tableau de bord », « Bienvenue, {prénom} ».

- Carte **Machines en ligne** (admin, gestionnaire) : nombre en ligne / total.
- Carte **Sessions actives**.
- Carte **Utilisateurs** (admin) ou **Patients** (gestionnaire) : affiche
  toujours « - » et « Chargement… », **le compteur n'est pas implémenté**.
- **Sessions récentes** : les 5 dernières, avec statut et ancienneté. Un clic
  ouvre la vue en direct (séance active) ou le détail.

### Machines : `/fr/dashboard/machines`

Admin et gestionnaire. Tableau des machines (nom, lieu, statut, dernier signal).
Recherche ; l'admin peut afficher les machines supprimées.

- **Nouvelle machine** (admin seulement) : nom, lieu, fréquence
  d'échantillonnage, canaux, intervalle de lot, gestionnaires. À la création,
  la **clé API** s'affiche **une seule fois** (« Cette clé ne sera affichée
  qu'une seule fois! ») avec un bouton Copier. Elle va dans
  `raspberry-pi/.env` (`MACHINE_API_KEY`).

### Détail d'une machine : `/fr/dashboard/machines/{id}`

Admin et gestionnaire de la machine.

| Bloc | Contenu | Qui agit |
|---|---|---|
| Bandeau « Supprimée » | Date de suppression, bouton **Restaurer**. | admin |
| Bouton **Lancer une séance auto** | Ouvre la fenêtre de lancement (§5). | admin, gestionnaire |
| Configuration | Statut (« En ligne », « Hors ligne », « En session »), dernier signal, fréquence, intervalle, canaux, « Créée le » ; bouton Modifier. | admin, gestionnaire |
| Gestionnaires | Liste, badge « Propriétaire » pour le premier. | lecture |
| **État en direct** | Mode, phase, fréquence cardiaque, vitesse du bras (et moteur, consigne), charge g, action de sécurité, état du variateur ; badge « En direct » / « Données périmées » et « Mis à jour il y a … », recalculés chaque seconde (§6). L'action de sécurité et l'état du variateur sont traduits ; une valeur que le site ne connaît pas s'affiche telle quelle. | lecture |
| **Programmes** | Programmes synchronisés depuis le Pi (lecture seule) : zone, durée, vitesse max, FC limite. Mention « Manuel : uniquement depuis la console de la machine ». « Programmes auto désactivés sur cette machine » si le Pi le dit. | lecture |
| **Droits de lancement** | Patients autorisés, « Accordé par {nom}, {date} », leur FC max ; boutons **Accorder** (choisir un patient) et **Retirer** (avec confirmation). | admin, gestionnaire |
| Zone de danger | **Régénérer la clé** (l'ancienne cesse de fonctionner ; la nouvelle s'affiche une fois) ; **Supprimer** (suppression douce, refusée si une séance est active ou en attente). | admin, gestionnaire |

**Modifier** (carte Configuration) enregistre les champs de la machine, dont
le nom et le lieu, par `machines.updateMachine`, ouvert à l'admin et au
gestionnaire de la machine.
La liste des gestionnaires de la machine, que seul un admin voit dans la
fenêtre, part dans un second appel réservé à l'admin
(`machines.assignMachineToGestionnaires`) : la fenêtre ne le fait que si
l'appelant est admin **et** si la liste a changé. L'ordre fait partie de la
liste, car le serveur fait du premier gestionnaire le « Propriétaire » : un
gestionnaire décoché puis recoché passe en fin de liste, et la liste est alors
envoyée. Un gestionnaire qui modifie le nom ou le lieu ne fait donc que le
premier appel, et un admin qui ne touche pas aux cases ne réécrit pas la
liste. Après l'enregistrement, la
fenêtre se ferme et la carte Configuration affiche « Machine mise à jour avec
succès » ; si le serveur refuse, son message s'affiche en rouge en haut de la
fenêtre, qui reste ouverte. La règle d'envoi est dans `lib/machineForm.ts`.

### Mes machines : `/fr/dashboard/my-machines`

Pour tous (c'est la page de lancement des patients). Liste les machines où
l'utilisateur **peut lancer** : admin = toutes, gestionnaire = les siennes,
patient = celles où il a le droit.

- Patient : badge « Votre FC max : … » ou alerte « Votre FC max n'est pas
  renseignée : demandez à votre gestionnaire… ».
- Aucune machine : « Vous n'avez encore aucun droit de lancement… » (patient)
  ou « Aucune machine disponible. ».
- Par machine : nom, lieu, état en direct avec son badge « En direct » /
  « Données périmées » (§6), nombre de programmes, bouton
  **Lancer une séance auto**, lien **Détails**.

### Sessions : `/fr/dashboard/sessions`

Pour tous (chacun voit ce que ses droits permettent). Onglets par statut
(Tous, Actives, Terminées, Échouées ; pas d'onglet « En attente »), colonnes : patient, machine,
**Type** (badges Auto / Manuel / Enregistrement et origine), statut, début,
durée. Un clic ouvre la vue en direct (séance active) ou le détail.

Le bouton **Nouvelle session** (admin, gestionnaire) crée une séance
d'**enregistrement ECG** (ancien mode `recording`, pour le client
`python -m src.main`). **Ce n'est pas une séance d'entraînement** et la console
locale ne la prend pas.

### Séance en direct : `/fr/dashboard/sessions/{id}/live`

- Séance d'entraînement **en attente** : « En attente que la machine arme la
  séance… » et le bouton **Annuler la séance**.
- Séance d'entraînement **active** : le **panneau d'entraînement** (§6) avec
  gros indicateurs, courbes et bouton **Arrêter la séance**. Sans point reçu
  depuis 20 s, il affiche « Aucun signal récent de la machine… » et grise ses
  indicateurs (§6).
- Sous le panneau, la page affiche aussi l'ancien bloc ECG (qualité du signal,
  FC, durée, lots de données, échantillons). **Pour une séance d'entraînement,
  ce bloc reste vide** (« Connexion... ») : la console locale n'envoie pas l'ECG
  brut au site, seulement la télémétrie. Ce bloc est traduit. Le nombre
  d'échantillons est compté sur les lots affichés (les 10 dernières secondes)
  et signalé comme partiel.
- Séance d'**enregistrement** : tracé ECG, badge « 5s de délai » pour un
  gestionnaire (Convex retarde ses données de 5 s), bouton **Terminer la
  session** (admin, gestionnaire).

### Détail d'une séance : `/fr/dashboard/sessions/{id}`

Nom du pratiquant (ou « Pratiquant non précisé »), machine, statut, bandeau
d'échec avec la raison. Pour une séance d'entraînement, la carte
**Entraînement** : type, origine, programme, zone cible, durée prévue, FC max du
pratiquant, opérateur, motif de fin, et les courbes de télémétrie. Pour une
séance d'enregistrement : tracé ECG et résumé.

Les cartes du haut donnent la durée, les lots de données, les échantillons
enregistrés, les canaux et la fréquence d'échantillonnage. Le nombre
d'échantillons et la fréquence sont **lus dans les lots chargés** (200 au plus
sur cette page), jamais calculés à partir d'une constante. Quand la session
compte plus de lots, les cartes l'indiquent (« lots chargés sur … ») ; tant que
le serveur n'a pas donné le nombre de lots, elles affichent « - », y compris
pour le comptage de chaque canal. Avec plusieurs canaux, le libellé précise
« tous canaux confondus ».

### Patients : `/fr/dashboard/patients` et `/fr/dashboard/patients/{id}`

Admin et gestionnaire. Liste avec recherche, **Nouveau patient** (prénom, nom,
e-mail, langue). La fenêtre annonce « Le patient recevra une invitation par
email… » : **aucun e-mail n'est envoyé** par le code.

Fiche patient : identité, séances, bouton Modifier, suppression, et la carte
**Physiologie** (voir §5.1).

### Utilisateurs : `/fr/dashboard/users` et `/fr/dashboard/users/{id}`

Admin (entrée de menu). Tous les comptes, filtre par rôle, recherche. La fiche
affiche le rôle, les séances ; pour un patient : Modifier, Physiologie ;
suppression (admin : tous sauf soi ; gestionnaire : ses patients seulement).

### Gestionnaires : `/fr/dashboard/gestionnaires` et `/{id}`

Admin seulement (sinon message d'accès refusé). Liste des gestionnaires avec
leurs nombres de machines et de patients. Fiche : **Assigner des machines** et
**Assigner des patients**.

**Assigner des machines** : une case par machine non supprimée, cochée si le
gestionnaire la gère déjà. **Enregistrer** applique exactement les cases à ce
gestionnaire, par `machines.setGestionnaireMachines` : cocher ajoute le lien,
décocher le retire. Les autres gestionnaires d'une machine gardent leur lien,
et un lien déjà présent n'est pas modifié (il garde son badge
« Propriétaire »). Une machine supprimée n'a pas de case : son lien n'est pas
retiré. La fenêtre s'ouvre toujours sur les machines actuelles du
gestionnaire. Après l'enregistrement, la fiche confirme, par exemple
« Machines enregistrées : 1 ajoutée, 1 retirée. » ; en cas de refus, le
message du serveur s'affiche dans la fenêtre, qui reste ouverte. La règle des
cases est dans `lib/gestionnaireMachines.ts`.

### Rapports : `/fr/dashboard/reports`

Séances terminées visibles par l'utilisateur, avec **Télécharger PDF** (généré
dans le navigateur par `lib/generatePdf.ts` à partir des données ECG). Le
rapport suit la langue de l'interface : libellés, dates, nombres et nom du
fichier (`Rapport_ECG_{identifiant}_{date}.pdf` en français). Sa fréquence
d'échantillonnage et son nombre d'échantillons sont lus dans les lots chargés
pour le rapport (50 au plus) ; au-delà, la liste et le PDF indiquent un
comptage partiel. Pour une séance d'entraînement, il n'y a pas d'ECG sur le
site : le rapport n'a pas de contenu ECG. **Ce dernier cas n'est pas vérifié.**

### Paramètres : `/fr/dashboard/settings`

Admin seulement (« Accès refusé. Réservé aux administrateurs. »). Votre compte ;
**Gestion des rôles** (choisir un utilisateur, un nouveau rôle, « Mettre à jour
le Rôle ») ; informations système (valeurs affichées, pas des réglages).

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

La fenêtre **bloque** le bouton **Lancer** et explique pourquoi quand : la
machine n'est pas lançable, est hors ligne, est déjà en séance, a les
programmes désactivés, n'a aucun programme, la FC max ou l'année de naissance
manque, ou le pratiquant a moins de 18 ans. Elle **avertit** si la zone dépasse
90 % de la FC max ou si la FC limite du programme dépasse la FC max (« Le
serveur refusera ce lancement. »).

Après **Lancer** :

1. Convex refait tous les contrôles ([convex.md §4](convex.md#4-fonctions-de-trainingts)) et crée la séance **en attente**.
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

Une séance **manuelle** peut aussi être arrêtée ainsi : le Pi l'arrête sur sa
rampe. En revanche elle ne peut pas être démarrée depuis le site.

**Ce bouton n'est pas un arrêt d'urgence.** L'arrêt d'urgence est à la machine.

---

## 6. Badges, indicateurs et courbes

### Badges

| Badge | Sens |
|---|---|
| **Auto** (icône jauge) | Séance programmée, pilotée par la fréquence cardiaque. |
| **Manuel** (icône main, ambre) | Séance à vitesse fixée par l'opérateur, démarrée à la machine. |
| **Enregistrement** | Ancienne séance ECG seule. |
| **Tableau de bord** / **Machine** | Origine : lancée depuis le site, ou démarrée à la console. |
| **En direct** (point vert) / **Données périmées** (gris) | État de la machine reçu il y a moins de 90 s / 90 s ou plus. Périmé : les valeurs sont grisées et « Aucun signal récent de la machine : les valeurs affichées peuvent être dépassées. » Détail d'une machine et Mes machines. Voir « Fraîcheur » ci-dessous. |
| Statut de séance | En attente, Active, Terminée, Échouée (les onglets de la liste gardent le pluriel). Un statut inconnu s'affiche tel quel. |

### Fraîcheur recalculée à l'horloge

Une machine qui se tait n'envoie plus rien : aucune donnée ne change, donc
aucune query Convex ne se relance. Le site ne peut pas attendre un changement de
donnée pour dire qu'un état est périmé. Il le recalcule lui-même **chaque
seconde** :

- un seul hook, `useFreshness` (`hooks/use-freshness.ts`), compare l'horodatage
  de la donnée à l'horloge du navigateur (`useNow(1000)`) ;
- un seul seuil pour l'état de la machine, `LIVE_FRESH_MS` = 90 s, défini dans
  `lib/training.ts` et importé par `convex/training.ts` : le serveur et le site
  ne peuvent pas diverger ;
- la carte « État en direct », chaque carte de Mes machines et le panneau
  d'entraînement lisent ce hook. Aucun badge « En direct » n'est affiché sans
  lui.

| Moment | Ce que le site affiche |
|---|---|
| Moins de 90 s après le dernier état | Badge « En direct », valeurs normales. |
| 90 s après le dernier état, à la seconde près | Badge « Données périmées », valeurs grisées, cœur gris, « Aucun signal récent de la machine : les valeurs affichées peuvent être dépassées. » Sur le détail d'une machine, « Mis à jour il y a … » continue d'avancer. |
| État suivant reçu (heartbeat toutes les 10 s) | Retour immédiat à « En direct ». |

Sur le détail d'une machine, le verdict `stale` du serveur compte aussi quand il
dit « périmé » : si l'horloge du navigateur retarde, le site n'est jamais plus
lent qu'avant ce recalcul. Sur Mes machines, le serveur retire l'état quand la
query se relance (au passage hors ligne) : la carte affiche alors « La machine
n'a encore rapporté aucun état. »

Limites :

- La fraîcheur compare l'heure du serveur (état de la machine) ou du Pi
  (télémétrie) à l'**horloge du navigateur**. Un poste dont l'horloge retarde
  voit « Données périmées » plus tard, un poste dont l'horloge avance le voit
  plus tôt.
- Le **statut** « En ligne » / « Hors ligne » et « Dernier signal » ne sont pas
  concernés : le statut est écrit par le serveur une fois par minute (hors
  ligne entre 1,5 et 2,5 min après le dernier signal) et « Dernier signal » ne
  se rafraîchit qu'au prochain changement de donnée. Entre 90 s et ce passage,
  une machine peut donc être « En ligne » et « Données périmées » à la fois.
- L'ancien bloc ECG de la vue en direct (badge « En direct » de l'en-tête) ne
  lit pas ce hook.

### Panneau d'entraînement (vue en direct)

Cinq gros indicateurs :

| Indicateur | Contenu |
|---|---|
| Fréquence cardiaque | Dernière valeur **fraîche** (point de moins de 20 s sur une séance active). Couleur : vert « Dans la zone », bleu « Sous la zone », rouge « Au-dessus de la zone ». « - » et « Pas de fréquence cardiaque fiable » si le Pi n'en a pas. |
| Vitesse du bras | tr/min bras mesurés, et tr/min moteur. |
| Charge | g au rayon configuré sur le Pi. |
| Phase | Mesure de référence, Échauffement, Maintien, Retour au calme, Récupération, Terminé ; avec l'action de sécurité si elle n'est pas « Aucune ». |
| Écoulé / Restant | Chronomètre et temps restant de la durée prévue. |

Bandeaux : en attente, arrêt demandé, séance échouée (avec motif), séance
terminée (avec motif), et pour le manuel « Manuel : uniquement depuis la console
de la machine ».

**Séance active sans signal récent.** Le panneau lit le même hook de fraîcheur
que les badges, avec le seuil de la télémétrie : 20 s (`TELEMETRY_FRESH_MS`,
`components/training/TrainingPanel.tsx` ; le Pi envoie ses points toutes les
5 s). Le dernier signe de vie est le dernier point reçu, ou le début de la
séance tant qu'aucun point n'est arrivé. Passé 20 s, à la seconde près :

- bandeau orange « Aucun signal récent de la machine : les valeurs affichées
  peuvent être dépassées. » ;
- les cinq indicateurs sont grisés ; fréquence cardiaque, vitesse du bras et
  charge passent à « - » ; la phase garde sa dernière valeur connue, grisée ;
- l'icône du titre ne tourne plus.

Le chronomètre continue : la séance reste « Active » côté serveur et la machine
suit ses propres règles. Au point suivant, tout revient. Une séance terminée ou
échouée n'est pas concernée : ses dernières valeurs sont son résultat.

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

---

## 7. Lancer le site en local

Prérequis : Node.js, les dépendances (`npm install` ou `bun install`), un
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
  fichiers JavaScript de ces environnements Python ; 2 sont dans
  `components/charts/LiveSensorDisplay.tsx`.
- Aller directement sur `/fr/dashboard` sans être passé par la page d'accueil
  laisse le compte sans ligne Convex : les pages restent vides.

---

## 8. Ce qui n'est pas vérifié ou pas fait

| Sujet | État |
|---|---|
| Rendu dans un navigateur | **Jamais testé.** |
| Déploiement Convex / Clerk | **Pas fait.** |
| Tests du site | Unitaires seulement, sans navigateur : `npm run test:ecg` (fonctions ECG et règles de `lib/` extraites des fenêtres) et `npm run test:site` (fraîcheur de l'état en direct : hook et composants). **Aucun test dans un navigateur** : la coupure d'une console simulée suivie de 90 s d'attente n'est pas rejouée de bout en bout (ANH-83). |
| Fraîcheur et horloge du poste | La fraîcheur est jugée sur l'horloge du navigateur (§6) ; l'écart avec l'heure du serveur n'est ni mesuré ni corrigé. |
| Statut « En ligne » et « Dernier signal » | Non recalculés à l'horloge (§6) : le statut suit le serveur (jusqu'à 2,5 min), « Dernier signal » ne bouge qu'au prochain changement de donnée. |
| Lancement auto de bout en bout (site → Convex → Pi → moteur) | **Jamais exécuté.** Le contrat HTTP est testé de chaque côté séparément : côté Pi contre un faux transport, côté Convex dans `convex/httpRoutes.test.ts`. |
| Invitation des patients par e-mail | Annoncée à l'écran, **pas implémentée**. Un patient pré-créé qui s'inscrit obtient une seconde ligne `users` (la liaison `linkPatientToClerk` n'est appelée nulle part). |
| Compteur « Utilisateurs / Patients » du tableau de bord | Pas implémenté (« - »). |
| Libellés des actions de sécurité et de l'état du variateur | Traduits (`freeze`, `quick_stop`, `go_silent` compris). Le vocabulaire français (« Vitesse figée », « Arrêt rapide (rampe du variateur) », « Mise en silence (arrêt par le variateur) »…) reste à relire par l'équipe. |
| Pratiquant d'une séance démarrée à la machine | Le Pi ne l'envoie pas : « Unknown » dans les listes. |
| ECG et rapport PDF d'une séance d'entraînement | Pas d'ECG transmis par la console locale : bloc ECG vide, rapport sans ECG. |
| Vue en direct : textes du bloc ECG | Traduits. La vue en direct d'une session d'enregistrement réellement active n'a pas été observée dans un navigateur depuis la traduction. |
| Nombre d'échantillons d'une longue session | Compté sur les lots chargés (200 sur la fiche, 50 pour un rapport, 10 s en direct) et signalé comme partiel au-delà. Un total exact demande un comptage côté serveur (`getSessionDataStats`, `convex/ecgData.ts`). |
| Textes encore en anglais | Messages du serveur, nom « Unknown », motif « Cancelled before start by … », fiches d'un administrateur ou d'un gestionnaire, messages d'accès des pages Gestionnaires, erreurs de saisie du formulaire patient, confirmation de suppression d'un compte. |
| Compte avec FC max ou année de naissance renseignée | Risque d'échec de `users.getCurrentUser` (validateur incomplet), donc de pages vides pour ce compte. **À vérifier en premier** sur un déploiement. Voir [convex.md §9](convex.md#9-défauts-connus-et-reste-à-faire). |

Pour la sécurité d'ensemble, voir [securite.md](securite.md).

# Le tableau de bord distant (site Next.js)

Le site (`app/`, `components/`, `lib/`, `messages/`) est le **tableau de bord
distant** d'Anheart (la marque affichée est « Gaura »). Il permet de gérer les
comptes et les machines, de **lancer une séance auto**, de la suivre en direct
et de l'arrêter. Il lit et écrit dans Convex (voir [convex.md](convex.md)) ; il
ne parle **jamais** directement au Raspberry Pi.

> **État réel.**
> - Le code se compile : `npx tsc --noEmit -p .` ne signale aucune erreur.
> - `npm run test:ecg` exécute les tests unitaires de `lib/` : les règles
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

Trois rôles. Un rôle se tient **dans une organisation** (un client) : Convex le
lit dans le jeton Clerk ou, tant que Clerk Organizations n'est pas configuré,
dans l'appartenance du compte (table `memberships`) ; `users.role` n'en est
qu'un miroir pour l'affichage (voir
[convex.md §3](convex.md#3-règles-dautorisation)). Les libellés affichés sont
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

**Organisations.** Tout ce tableau vaut **à l'intérieur d'une organisation** :
un gestionnaire ne voit ni machine, ni patient, ni séance d'un autre client,
et un patient ne reçoit un droit que sur une machine de sa propre
organisation. Le rôle **admin** ci-dessus est celui de l'organisation Anheart,
la seule qui voit tous les clients. Convex connaît aussi l'admin d'une
organisation cliente, qui gère tout dans la sienne sauf créer ou restaurer une
machine. Sur un déploiement à un seul client, rien ne change à l'écran. Le
site n'a pas encore de sélecteur d'organisation ni de page de membres : ils
arrivent avec la suite du multi-organisation.

**Protection des pages.** `proxy.ts` (middleware Clerk + next-intl) exige une
connexion pour `/{locale}/dashboard/...`. Les contrôles par rôle se font dans
Convex : une page ouverte sans le bon rôle reçoit des listes vides ou un refus.
Quelques pages affichent aussi un message « accès refusé » (voir §4).

**Premier compte.** À la première connexion, un compte reçoit le rôle `user`
(ou, quand son jeton Clerk porte une organisation, le rôle qu'il y tient).
La ligne Convex n'est créée que lorsque l'utilisateur connecté passe par la
**page d'accueil** (bouton « Accéder au tableau de bord »). Tant que Clerk
Organizations n'est pas configuré, il n'y a pas d'amorçage automatique du
premier admin : il faut le nommer dans le tableau de bord Convex (voir
[convex.md §8](convex.md#8-déployer)).

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
  Une machine n'y compte plus 90 s après son dernier signal (§6).
- Carte **Sessions actives**.
- Carte **Utilisateurs** (admin) ou **Patients** (gestionnaire) : affiche
  toujours « - » et « Chargement… », **le compteur n'est pas implémenté**.
- **Sessions récentes** : les 5 dernières, avec statut et ancienneté. Un clic
  ouvre la vue en direct (séance active) ou le détail.

### Machines : `/fr/dashboard/machines`

Admin et gestionnaire. Tableau des machines (nom, lieu, statut, dernier signal).
Le statut et « Dernier signal » sont recalculés chaque seconde (§6).
Recherche ; l'admin peut afficher les machines supprimées.

- **Nouvelle machine** (admin seulement) : nom, lieu, gestionnaires. À la
  création, la **clé API** s'affiche **une seule fois** (« Cette clé ne sera
  affichée qu'une seule fois! ») avec un bouton Copier. Elle va dans la
  configuration de la machine (`MACHINE_API_KEY` dans `/etc/anheart/anheart.env`
  sur un Pi installé, dans `raspberry-pi/.env` sur un poste de développement).

### Détail d'une machine : `/fr/dashboard/machines/{id}`

Admin et gestionnaire de la machine.

| Bloc | Contenu | Qui agit |
|---|---|---|
| Bandeau « Supprimée » | Date de suppression, bouton **Restaurer**. | admin |
| Bouton **Lancer une séance auto** | Ouvre la fenêtre de lancement (§5). | admin, gestionnaire |
| Configuration | Statut (« En ligne », « Hors ligne », « En session ») et dernier signal, recalculés chaque seconde (§6), « Créée le » ; bouton Modifier. | admin, gestionnaire |
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
- Par machine : nom, lieu, statut, état en direct avec son badge
  « En direct » / « Données périmées » (§6), nombre de programmes, bouton
  **Lancer une séance auto**, lien **Détails**. 90 s après le dernier
  signal, la carte affiche « Hors ligne » et grise le bouton (§6).

### Sessions : `/fr/dashboard/sessions`

Pour tous (chacun voit ce que ses droits permettent). Onglets par statut
(Tous, Actives, Terminées, Échouées ; pas d'onglet « En attente »), colonnes : patient, machine,
**Type** (badges Auto / Manuel / Enregistrement et origine), statut, début,
durée. Un clic ouvre la vue en direct (séance active) ou le détail.

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
- Séance d'entraînement **finie** : le même panneau, figé, et le bouton
  **Voir le détail**.
- La page n'affiche rien d'autre : la console locale n'envoie pas l'ECG brut au
  site, seulement la télémétrie.
- Séance de l'**ancien mode d'enregistrement** (historique) : pas de vue en
  direct. La page affiche « Cette séance vient de l'ancien mode
  d'enregistrement ECG : elle n'a pas de vue en direct. » et renvoie au détail.

### Détail d'une séance : `/fr/dashboard/sessions/{id}`

Nom du pratiquant (ou « Pratiquant non précisé »), machine, statut, bandeau
d'échec avec la raison. Pour une séance d'entraînement, la carte
**Entraînement** : type, origine, programme, zone cible, durée prévue, FC max du
pratiquant, opérateur, motif de fin, et les courbes de télémétrie. Suivent les
cartes « Informations patient », « Chronologie de la session » et, s'il y en a,
« Notes de session ».

Pour une séance de l'ancien mode d'enregistrement ECG (historique, lecture
seule), la carte **Ancien enregistrement ECG** remplace la carte Entraînement :
canaux enregistrés et, si des données existent, nombre de lots, durée des
données et plage horaire, comptés par le serveur (`ecgData.getSessionDataStats`).
Le tracé n'est plus affiché.

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

**Assigner des patients** : une case par patient de la liste de l'admin,
cochée si le gestionnaire le gère déjà. La fenêtre se comporte comme celle des
machines :

- elle s'ouvre toujours sur les patients actuels du gestionnaire, jamais sur
  les cases d'une modification annulée ; si ces patients changent sur le
  serveur pendant qu'elle est ouverte, les cases reprennent celles du serveur
  (une modification en cours et pas encore enregistrée est alors à refaire) ;
- tant que les patients du gestionnaire ou la liste des patients à choisir ne
  sont pas chargés, **Modifier** et **Enregistrer** sont inactifs, et la carte
  affiche « Chargement... » au lieu de « Aucun patient assigné » ;
- **Enregistrer** envoie la liste cochée à `users.assignPatientsToGestionnaire`,
  qui n'écrit que la différence : cocher ajoute le lien, décocher le retire, un
  lien déjà présent n'est pas réécrit. Un patient du gestionnaire qui n'a pas
  de case dans la fenêtre garde son lien ;
- après l'enregistrement, la fiche confirme ce que le serveur a écrit, par
  exemple « Patients enregistrés : 1 ajouté, 1 retiré. » ; en cas de refus, le
  message du serveur s'affiche dans la fenêtre, qui reste ouverte avec ses
  cases.

Le serveur ne lie que les patients actifs de l'organisation du gestionnaire :
un patient d'une autre organisation coché par l'admin Anheart n'est pas lié, et
le décompte affiché ne le compte pas. Il n'y a ni journal ni annulation de ces
changements. Deux admins qui enregistrent l'un après l'autre : la liste du
dernier enregistrement s'applique en entier. La règle des cases est dans
`lib/gestionnairePatients.ts`.

### Rapports : `/fr/dashboard/reports`

Séances terminées visibles par l'utilisateur : pratiquant, machine, date,
durée, et le bouton **Voir** qui ouvre le détail. Il n'y a plus de rapport PDF :
l'ancien, construit sur les données ECG, a été retiré avec le mode
d'enregistrement ; le rapport de séance d'entraînement est à faire (ANH-89).

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

La fenêtre **bloque** le bouton **Lancer** et explique pourquoi quand : la
machine n'est pas lançable, est hors ligne, est déjà en séance, a les
programmes désactivés, n'a aucun programme, la FC max ou l'année de naissance
manque (ou la valeur enregistrée n'est pas un entier, pour un patient comme
pour « Moi-même »), ou le pratiquant a moins de 18 ans. Elle **avertit** si la
zone dépasse 90 % de la FC max ou si la FC limite du programme dépasse la FC
max (« Le serveur refusera ce lancement. »).

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
| **En direct** (point vert) / **Données périmées** (gris) | État de la machine reçu par le serveur il y a moins de 90 s / 90 s ou plus. Périmé : les valeurs sont grisées et « Aucun signal récent de la machine : les valeurs affichées peuvent être dépassées. » Détail d'une machine et Mes machines. Voir « Fraîcheur » ci-dessous. |
| **En ligne** / **En session** / **Hors ligne** | Statut d'une machine. « Hors ligne » dès que son dernier signal a 90 s, à la même seconde que « Données périmées », quel que soit le statut encore écrit dans Convex. Liste des machines, détail d'une machine, Mes machines, fiche d'un gestionnaire. Voir « Fraîcheur » ci-dessous. |
| Statut de séance | En attente, Active, Terminée, Échouée (les onglets de la liste gardent le pluriel). Un statut inconnu s'affiche tel quel. |

### Fraîcheur recalculée à l'horloge

Une machine qui se tait n'envoie plus rien : aucune donnée ne change, donc
aucune query Convex ne se relance. Le site ne peut pas attendre un changement de
donnée pour dire qu'un état est périmé. Il le recalcule lui-même **chaque
seconde**, sur l'**horloge du serveur**. L'heure du poste ne date rien. Celle
du Pi non plus : le serveur place lui-même sur son horloge le début d'une
séance et la mesure de chaque point, à partir du temps écoulé que la machine
compte ([convex.md, Deux horloges](convex.md#deux-horloges)).

- **Le serveur date ce qu'il reçoit.** L'état d'une machine
  (`live.updatedAt`) et son dernier signal (`lastHeartbeat`) sont datés par
  Convex à la réception du heartbeat. Le dernier signe de vie d'une séance
  active (`lastSignalAt`) est la date à laquelle Convex a **reçu** son dernier
  point de télémétrie (la date de création de la ligne, pas le `t` que le Pi y
  écrit) ; tant qu'aucun point n'est arrivé, c'est le début de la séance daté
  par le serveur (pour une séance démarrée à la console : son enregistrement
  par le serveur, pas le `startedAt` envoyé par le Pi). Avec lui vient
  `lastMeasuredAt` : la date de mesure de ce même point, c'est-à-dire son `t`
  placé par le serveur sur son horloge (voir « Panneau d'entraînement » plus
  bas). Pour une séance dont la machine n'a donné aucune date de début
  (console antérieure au contrat 1.1), c'est le `t` tel que le Pi l'a écrit.
- **Chaque réponse porte l'horloge du serveur.** `getMachineLive`,
  `listLaunchableMachines`, `getTrainingSession`, `getMachine`, `listMachines`
  et `getMachinesForGestionnaire` renvoient `serverNow` : l'heure du serveur au
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
  la réception d'un point. Sur le panneau d'entraînement, elle date la
  **mesure** du point affiché (voir plus bas), avec ces conséquences :
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
terminée (avec motif), et pour le manuel « Manuel : uniquement depuis la console
de la machine ».

**Quand une valeur est actuelle.** Sur une séance active, le panneau n'affiche
la fréquence cardiaque, la vitesse et la charge du dernier point (celui de plus
grand `t`) que si **les deux** dates de ce point sont récentes sur l'horloge du
serveur, au seuil de la télémétrie : 20 s (`TELEMETRY_FRESH_MS`,
`lib/training.ts` ; le Pi envoie ses points toutes les 5 s) :

1. **sa réception** (`lastSignalAt`, que `getTrainingSession` renvoie avec la
   séance) : la date à laquelle le serveur l'a reçu, ou le début de la séance
   daté par le serveur tant qu'aucun point n'est arrivé ;
2. **sa mesure** (`lastMeasuredAt`, le `t` du point placé sur l'horloge du
   serveur) :
   moins de 20 s avant l'heure du serveur, et pas plus de 5 s **après** sa
   propre réception (`FUTURE_TOLERANCE_MS`, `datedAfterReception` : un point
   n'est pas mesuré après avoir été reçu ; une telle date vient d'une horloge
   en avance, et ne redevient pas crédible quelques secondes plus tard).

La réception seule ne suffit pas : pendant une coupure de liaison, la console
garde jusqu'à une heure de points (3600) et les renvoie au retour par paquets
de 300, **les plus anciens d'abord**, un paquet toutes les 5 s. Chaque paquet
est une réception fraîche de mesures anciennes. Le panneau relit aussi le `t`
du point qu'il imprime, quoi que dise la séance.

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

Aucune **mutation** du site n'échoue en silence. Cela vaut pour les mutations
seulement : une lecture Convex qui échoue, la copie d'une clé dans le
presse-papiers ou toute action qui n'écrit rien ne passent pas par ce mécanisme
et ne sont pas couvertes.

Toute mutation Convex appelée depuis `app/` ou `components/` passe par le hook
`useMutationWithFeedback` (`hooks/use-mutation-with-feedback.ts`) : il exécute
la mutation, ne lève jamais d'exception et rend `{ ok: true, value }` ou
`{ ok: false, message, code, requestId }`. Le message s'affiche en bas à droite
de l'écran (`components/FeedbackToaster.tsx`, monté dans
`app/[locale]/layout.tsx`), au-dessus des fenêtres ; il part seul (5 s pour un
succès, 12 s pour un échec) ou par sa croix.

| Cas | Ce qui s'affiche |
|---|---|
| Succès | Le texte fourni par l'appelant, en français ou en anglais (`feedback.*`, `machines.*Success`, `gestionnaires.machinesSaved` et `gestionnaires.patientsSaved` dans `messages/*.json`). L'appelant donne un texte, ou une fonction qui le compose à partir de la réponse de la mutation (le décompte de « Machines enregistrées : … » et de « Patients enregistrés : … »). Une mutation de fond (création de la ligne du compte à l'arrivée sur la page d'accueil) n'en fournit pas, pas plus que la liste des gestionnaires envoyée après une modification de machine : leur succès reste muet, leur échec non. |
| Échec avec un code stable | La traduction du code si `messages/*.json` contient la clé `errors.<code>`. Le code est lu dans la `ConvexError` du serveur : la chaîne elle-même, ou le champ `code` (à défaut `error`) d'un objet. |
| Échec avec un texte | Sans traduction du code, le texte du serveur : la chaîne de la `ConvexError`, le champ `message` de l'objet, ou sur un déploiement de développement la ligne « Uncaught Error: … ». Ces textes sont encore en anglais. |
| Échec masqué | En production, Convex masque le texte d'une erreur qui n'est pas une `ConvexError` (comportement documenté par Convex, pas encore observé sur ce projet). Le site affiche alors « L'action a échoué. Réessayez. Référence à transmettre si le problème persiste : {identifiant}. » |

Chaque échec est aussi écrit dans la console du navigateur avec le nom de la
mutation, son code et l'identifiant de requête Convex, celui qui permet de
retrouver l'erreur dans les journaux du déploiement.

Les fenêtres et les cartes qui affichaient déjà l'erreur dans un encadré rouge
(lancement, arrêt, droits de lancement, physiologie, formulaires machine et
patient, fenêtre « Assigner des machines ») le gardent : il reçoit le même
message que la notification. La fenêtre « Assigner des patients » a le même
encadré depuis ANH-208. De même, trois écrans gardent leur propre confirmation
durable en plus de la notification : « Machines enregistrées : … » et
« Patients enregistrés : … » sur la fiche d'un gestionnaire, « Machine mise à
jour avec succès » sur la fiche d'une machine, et le bouton « Enregistré » de la
carte Physiologie.

**Arrêter ou annuler une séance** donne un seul message, quel que soit l'état
que la page affichait : « Demande envoyée. L'état de la séance s'affiche sur
cette page. » `training.requestStop` répond de la même façon qu'il ait annulé
une séance en attente, demandé l'arrêt d'une séance active ou trouvé la séance
déjà finie, et la machine peut avoir armé la séance pendant l'envoi : la page ne
peut donc pas affirmer « annulée » ou « arrêt demandé ». Ce sont les bandeaux du
panneau qui disent ce qui s'est passé.

Limites :

- aucune clé `errors.<code>` n'existe encore : les codes stables arrivent côté
  serveur avec ANH-133 et leurs traductions avec ANH-90 ;
- `convex/machines.ts` et `convex/users.ts` lèvent des `Error` simples : en
  production, leurs refus (suppression d'une machine en séance, par exemple)
  donnent le message masqué ci-dessus, pas leur motif ;
- le test de bout en bout dans un navigateur (suppression refusée d'une machine
  en séance) attend l'infrastructure d'ANH-83.

Tests, dans `npm run test:site` : `hooks/use-mutation-with-feedback.test.tsx`
(succès, erreur codée, erreur brute, erreur masquée, journal),
`components/training/stop-feedback.test.tsx` (le message d'arrêt est le même
que la page ait cru la séance en attente, active ou finie) et
`hooks/no-silent-mutation.test.ts`, qui refuse dans `app/` et `components/` un
appel direct à `useMutation` ainsi qu'un `catch` vide ou réduit à un
`console.error` dans un fichier qui appelle une mutation.

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
  fichiers JavaScript de ces environnements Python ; les 2 autres étaient dans
  un composant de l'ancien bloc ECG, retiré depuis.
- Aller directement sur `/fr/dashboard` sans être passé par la page d'accueil
  laisse le compte sans ligne Convex : les pages restent vides.

---

## 8. Ce qui n'est pas vérifié ou pas fait

| Sujet | État |
|---|---|
| Rendu dans un navigateur | **Jamais testé.** |
| Déploiement Convex / Clerk | **Pas fait.** |
| Tests du site | Unitaires seulement, sans navigateur : `npm run test:ecg` (règles de `lib/` extraites des fenêtres, horloge du serveur, retrait de l'ancien mode ECG) et `npm run test:site` (fraîcheur, statut et dernier signal : hook et composants, avec l'horloge du poste et celle du Pi décalées ; retour des mutations : hook, message d'arrêt et garde-fou des erreurs silencieuses ; chaque composant : panneau de séance et arrêt, fenêtre de lancement, droits de lancement, formulaires machine et patient, menu par rôle ; chaque page de `app/` montée dans un DOM simulé, Convex remplacé : ce qu'elle montre à chaque rôle, ce que ses actions envoient, ce qu'elle affiche quand le serveur refuse, voir [framework-de-test.md](framework-de-test.md#tests-des-pages-du-site-anh-204)). La CI exige 80 % de lignes et de branches couvertes sur `lib/`, `hooks/`, `components/` et `app/` réunis. **Aucun test dans un navigateur** : ni la coupure d'une console simulée suivie de 90 s d'attente, ni une suppression refusée par un vrai serveur ne sont rejouées de bout en bout (ANH-83). |
| Fraîcheur et horloges | Jugée sur l'horloge du serveur (§6) : l'horloge du poste ne date rien. Pire cas chiffré sur une page qui vient de s'ouvrir : 107 s pour une machine, 37 s pour le panneau (cache de Convex, 17 s d'après son code source), plus le transit de la réponse et un onglet suspendu, **non mesurés**. Panneau : une horloge du Pi en retard de plus de 15 s environ, ou en avance de plus de 5 s environ, affiche le bandeau alors que la machine envoie (sens sûr) ; en avance de X, un point renvoyé en retard peut passer pour actuel jusqu'à 20 s + X après sa mesure (ANH-163). |
| Statut « En ligne » et « Dernier signal » | Recalculés chaque seconde par le site (§6). Convex n'écrit `offline` qu'au passage de sa tâche (jusqu'à 2,5 min) : le refus d'un lancement par le serveur et la fenêtre de lancement lisent encore ce statut écrit (ANH-144). |
| Lancement auto de bout en bout (site → Convex → Pi → moteur) | **Jamais exécuté.** Le contrat HTTP est testé de chaque côté séparément : côté Pi contre un faux transport, côté Convex dans `convex/httpRoutes.test.ts`. |
| Invitation des patients par e-mail | Annoncée à l'écran, **pas implémentée**. Un patient pré-créé qui s'inscrit obtient une seconde ligne `users` (la liaison `linkPatientToClerk` n'est appelée nulle part). |
| Compteur « Utilisateurs / Patients » du tableau de bord | Pas implémenté (« - »). |
| Libellés des actions de sécurité et de l'état du variateur | Traduits (`freeze`, `quick_stop`, `go_silent` compris). Le vocabulaire français (« Vitesse figée », « Arrêt rapide (rampe du variateur) », « Mise en silence (arrêt par le variateur) »…) reste à relire par l'équipe. |
| Pratiquant d'une séance démarrée à la machine | Le Pi ne l'envoie pas : « Unknown » dans les listes. |
| Rapport d'une séance d'entraînement | Pas de rapport PDF sur le site : l'ancien rapport ECG est retiré, celui des séances d'entraînement reste à faire (ANH-89). |
| Pages retouchées au retrait de l'ancien mode ECG (Sessions, vue en direct, détail, Rapports, fenêtre machine) | Compilées et couvertes par `npm run test:ecg` ; **pas rouvertes dans un navigateur** depuis. |
| Textes encore en anglais | Messages du serveur, nom « Unknown », motif « Cancelled before start by … », fiches d'un administrateur ou d'un gestionnaire, messages d'accès des pages Gestionnaires, erreurs de saisie du formulaire patient, confirmation de suppression d'un compte. |
| Compte avec FC max ou année de naissance renseignée | Risque d'échec de `users.getCurrentUser` (validateur incomplet), donc de pages vides pour ce compte. **À vérifier en premier** sur un déploiement. Voir [convex.md §9](convex.md#9-défauts-connus-et-reste-à-faire). |

Pour la sécurité d'ensemble, voir [securite.md](securite.md).

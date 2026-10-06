# Guide gestionnaire : le tableau de bord

> **Brouillon, en attente de validation par la semaine pilote.**
> Version du 6 octobre 2026. Aucun client n'a encore utilisé ce guide.
>
> - Les écrans décrits ici (machines en direct, lancement d'une séance auto,
>   suivi en direct, droits de lancement, physiologie) appartiennent à la
>   version du site prévue pour la semaine pilote. À cette date, cette version
>   n'est pas encore en ligne : le site que vous ouvrez aujourd'hui peut
>   différer.
> - Les captures d'écran viennent d'un site d'essai, avec des comptes de
>   démonstration et une machine simulée. Elles ont été prises avant la
>   traduction de plusieurs écrans : certaines montrent encore des textes en
>   anglais que le site affiche maintenant en français. En cas d'écart, votre
>   écran fait foi.
> - Quelques messages sont encore en anglais : ceux que le serveur renvoie
>   quand il refuse une action, et le motif d'une séance annulée. Ce guide
>   décrit alors leur fonction, sans citer leur texte.
> - Certaines informations dont vous avez besoin ne sont pas encore validées.
>   Elles sont signalées par un encadré qui commence par les mots *À compléter
>   par Anheart*. Tant qu'un de ces encadrés vous concerne, demandez la réponse
>   à Anheart avant d'agir.

Les autres guides : [manuel opérateur](manuel-operateur.md) (la console de la
machine), [guide passager](guide-passager.md) (la personne dans la capsule),
[sommaire](README.md).

---

## Sommaire

1. [À qui s'adresse ce guide](#1-à-qui-sadresse-ce-guide)
2. [Ce que le site fait, et ce qu'il ne fait pas](#2-ce-que-le-site-fait-et-ce-quil-ne-fait-pas)
3. [Se connecter et se repérer](#3-se-connecter-et-se-repérer)
4. [Préparer un patient](#4-préparer-un-patient)
5. [Donner ou retirer le droit de lancement](#5-donner-ou-retirer-le-droit-de-lancement)
6. [Lancer une séance auto](#6-lancer-une-séance-auto)
7. [Suivre une séance en direct](#7-suivre-une-séance-en-direct)
8. [Arrêter ou annuler une séance](#8-arrêter-ou-annuler-une-séance)
9. [Lire une séance terminée et les rapports](#9-lire-une-séance-terminée-et-les-rapports)
10. [Quand quelque chose ne va pas](#10-quand-quelque-chose-ne-va-pas)
11. [Qui appeler](#11-qui-appeler)
12. [Petit lexique](#12-petit-lexique)

---

## 1. À qui s'adresse ce guide

Ce guide est pour le **gestionnaire** : la personne qui suit des patients et
des machines depuis le site web. Le site affiche le nom « Gaura ».

Le site connaît trois rôles :

| Rôle affiché | Pour qui |
|---|---|
| **Administrateur** | Gère les comptes, les rôles et les machines. |
| **Gestionnaire** | Vous. Vous voyez les machines qu'on vous a attribuées et les patients qui vous sont rattachés. |
| **Patient** | La personne qui s'entraîne. Elle ne voit que ses propres séances. |

Trois choses ne se font pas avec un compte gestionnaire. Demandez-les à votre
administrateur :

- vous donner le rôle **Gestionnaire** (tout nouveau compte est d'abord
  **Patient**) ;
- vous attribuer une machine ;
- vous rattacher un patient qui a déjà un compte.

> **À compléter par Anheart avant la semaine pilote :** qui tient le rôle
> d'administrateur pour votre établissement, et comment le joindre.

## 2. Ce que le site fait, et ce qu'il ne fait pas

Le site vous permet de :

- voir vos machines et leur état ;
- tenir la fiche de vos patients ;
- dire quels patients peuvent lancer eux-mêmes une séance ;
- lancer une séance auto, la suivre en direct et demander son arrêt ;
- relire les séances passées.

| Le site ne peut pas | Ce qu'il faut savoir |
|---|---|
| Démarrer une séance manuelle | Elle se démarre seulement à la console de la machine. Le site le rappelle : « Manuel : uniquement depuis la console de la machine ». |
| Arrêter la machine tout de suite | Le bouton d'arrêt du site envoie une demande. La machine la lit toutes les 3 secondes environ. En temps normal, elle décélère ensuite progressivement. Mais tant qu'elle garde sa vitesse pour une raison de sécurité (action « Vitesse figée »), la demande ne ralentit pas le bras. L'arrêt d'urgence se fait à la machine, par l'opérateur. |
| Voir la machine | Le site ne voit ni la capsule, ni la salle, ni l'opérateur. Il affiche ce que la machine lui envoie, avec quelques secondes de retard. |
| Forcer un départ | La machine refait tous les contrôles et peut refuser. |
| Créer ou modifier un programme | Les programmes viennent de la machine. Le site les affiche en lecture seule. |
| Joindre une machine sans Internet | C'est la machine qui interroge le site. Si elle n'a plus Internet, le site ne peut rien lui demander. |

## 3. Se connecter et se repérer

> **À compléter par Anheart avant la semaine pilote :** l'adresse du site pour
> votre établissement, et la façon dont votre compte est créé.

1. Ouvrez le site et cliquez **Se connecter** (ou **S'inscrire** si vous
   n'avez pas encore de compte).
2. Suivez la fenêtre de connexion.
3. À votre toute première connexion, restez quelques secondes sur la page
   d'accueil : c'est elle qui crée votre fiche.
4. Cliquez **Accéder au tableau de bord**.

![Le tableau de bord d'un gestionnaire](../guides/img/site-23-gestionnaire-tableau-de-bord.png)

*Le tableau de bord d'un gestionnaire, avec le menu à gauche. Capture du site
d'essai, compte de démonstration, prise avant la traduction : les statuts des
séances y sont encore écrits au pluriel.*

Votre menu, à gauche :

| Entrée | À quoi elle sert |
|---|---|
| **Tableau de bord** | Un résumé : machines en ligne, séances en cours, séances récentes. |
| **Patients** | La liste et la fiche de vos patients. |
| **Machines** | Vos machines, leur état et les droits de lancement. |
| **Mes machines** | La page d'où vous lancez une séance auto. |
| **Sessions** | Toutes les séances que vous pouvez voir. |
| **Rapports** | Les séances terminées. |

Les pages se mettent à jour seules. Vous n'avez pas besoin de les recharger.

## 4. Préparer un patient

### 4.1 Créer la fiche, ou pas

Choisissez d'abord selon le cas :

| Le patient va-t-il se connecter lui-même au site ? | Que faire |
|---|---|
| **Oui** (pour voir ses séances, ou pour lancer lui-même) | Ne créez pas de fiche. Demandez-lui de créer son compte lui-même (**S'inscrire**), puis demandez à votre administrateur de vous le rattacher. |
| **Non** (vous seul lancerez pour lui) | Créez sa fiche, comme ci-dessous. |

Pour créer une fiche :

1. Cliquez **Patients**, puis **Nouveau patient**.
2. Remplissez **Prénom**, **Nom**, **Email** et **Langue**.
3. Cliquez **Créer**.

Le patient apparaît dans votre liste. Il vous est rattaché automatiquement.

> **Attention.** La fenêtre annonce : « Le patient recevra une invitation par
> email pour configurer son compte. » À la date de ce brouillon, aucun courriel
> n'est envoyé. Une fiche créée ainsi ne permet pas de se connecter. Si la
> personne s'inscrit plus tard avec la même adresse, elle obtient une seconde
> fiche, vide, sans lien avec la première.

![La liste des patients d'un gestionnaire](../guides/img/site-24-gestionnaire-patients.png)

*La liste des patients d'un gestionnaire. Capture du site d'essai, comptes de
démonstration.*

### 4.2 Renseigner la physiologie

Le site refuse toute séance auto pour une personne dont la physiologie n'est
pas renseignée. Vous seul pouvez la saisir (ou un administrateur) : le patient
ne le peut pas.

1. Cliquez **Patients**, puis la ligne du patient.
2. Allez à la carte **Physiologie**.
3. **FC max mesurée (bpm)** : remplissez ce champ seulement si la fréquence
   cardiaque maximale du patient a été mesurée. Sinon laissez-le vide.
4. **Année de naissance** : remplissez-la toujours. Elle est obligatoire, même
   avec une FC max mesurée.
5. Lisez **FC max retenue** : c'est la valeur que le site utilisera. Elle est
   « mesurée » si vous avez saisi une valeur. Sinon elle est « estimée
   (208 − 0,7 × âge) » : c'est un calcul du logiciel à partir de l'âge.
6. Cliquez **Enregistrer**. Le bouton affiche « Enregistré ».

Pour effacer une valeur : videz le champ, puis **Enregistrer**.

Si une saisie est refusée, le site affiche « La FC max doit être comprise entre
100 et 220 bpm » ou « Année de naissance invalide ». Ce sont les bornes de
saisie du logiciel.

![La fiche d'un patient et sa carte Physiologie](../guides/img/site-16-patient-fiche.png)

*La fiche d'une patiente de démonstration, avec la carte Physiologie en bas à
gauche. Capture du site d'essai, vue par un administrateur, prise avant la
traduction : le chemin en haut de page y est encore en anglais.*

Ce que le logiciel contrôle ensuite, à chaque lancement :

- la FC max retenue et l'année de naissance sont renseignées ;
- le pratiquant a au moins 18 ans. L'âge est calculé à partir de la seule
  année de naissance, comme si l'anniversaire n'était pas encore passé : une
  personne peut donc être refusée pendant l'année de ses 18 ans ;
- le haut de la zone cible du programme ne dépasse pas 90 % de la FC max
  retenue, et la FC limite du programme ne dépasse pas la FC max retenue.

Ce sont des contrôles du logiciel. Ils ne remplacent pas un avis médical : un
pratiquant que le logiciel accepte n'est pas pour autant apte à monter dans la
machine.

> **À compléter par Anheart avant la semaine pilote :** les critères médicaux
> d'admission d'un patient (qui décide qu'une personne peut monter dans la
> machine, et sur quels examens), et la façon dont la FC max doit être mesurée.

## 5. Donner ou retirer le droit de lancement

Le **droit de lancement** permet à un patient de lancer lui-même une séance
auto sur une machine. Vous n'en avez pas besoin pour lancer vous-même une
séance pour l'un de vos patients.

> **À compléter par Anheart avant la semaine pilote :** si les patients sont
> autorisés à lancer eux-mêmes leurs séances pendant le pilote, et à quelles
> conditions.

Conditions : le patient a un compte **Patient**, il vous est rattaché, et la
machine vous est attribuée.

Pour donner le droit :

1. Cliquez **Machines**, puis la machine.
2. Allez à la carte **Droits de lancement**.
3. Dans la liste **Choisir un patient**, choisissez le patient.
4. Cliquez **Accorder**.

Le patient apparaît dans la carte, avec sa FC max et le nom de la personne qui
a accordé le droit. De son côté, la machine apparaît dans sa page **Mes
machines**.

Pour retirer le droit :

1. Sur la ligne du patient, cliquez **Retirer**.
2. Dans la fenêtre « Retirer le droit de lancement ? », cliquez **Retirer**.

Retirer le droit n'arrête pas une séance déjà en cours.

![Le détail d'une machine](../guides/img/site-06-machine-detail.png)

*Le détail d'une machine : état en direct, programmes, puis la carte Droits de
lancement. Capture du site d'essai, vue par un administrateur, machine
simulée, prise avant la traduction : quelques libellés y sont encore en
anglais. Les valeurs des programmes sont des valeurs d'essai.*

Si quelque chose bloque :

- la liste affiche « Aucun autre patient éligible » : tous vos patients ont
  déjà le droit, ou aucun patient ne vous est rattaché ;
- un message rouge en anglais apparaît dans la carte : il vous manque un
  droit (patient non rattaché, machine non attribuée), ou la personne n'a pas
  un compte **Patient**. Voyez avec votre administrateur.

## 6. Lancer une séance auto

Dans une séance auto, la machine règle seule sa vitesse pour garder la
fréquence cardiaque du pratiquant dans la zone cible du programme.

### Avant de lancer

- La machine est **En ligne**, sans séance en cours, avec au moins un
  programme.
- La physiologie du pratiquant est renseignée
  ([section 4.2](#42-renseigner-la-physiologie)).
- **Aucune alerte n'attend à la console.** À la date de ce brouillon, la
  console verrouille d'elle-même une alerte quelque temps après une séance
  ([manuel opérateur, section 9.2](manuel-operateur.md#92-la-console-agit-seule)).
  Tant qu'une alerte attend, la machine refuse tout lancement : votre séance
  prend le statut « Échouée », avec un motif qui commence par « refusee par la
  machine ». Voyez alors avec l'opérateur.
- **Un opérateur est à côté de la machine, console prête, et le pratiquant est
  installé.** Le site ne voit pas la machine. La machine prend votre demande
  d'elle-même, sans aucune action de l'opérateur à la console : en quelques
  secondes en temps normal, plus tard si elle n'était pas joignable. Si elle
  accepte le départ, elle met ensuite le bras en rotation d'elle-même. Dès
  votre clic, plus personne ne doit s'approcher du bras ni de la capsule.

> **À compléter par Anheart avant la semaine pilote :** les programmes validés
> pour votre établissement, et pour quels patients. Les valeurs des programmes
> visibles aujourd'hui sont des réglages provisoires du logiciel, pas des
> recommandations médicales.

> **À compléter par Anheart avant la semaine pilote :** l'organisation prévue
> entre vous et l'opérateur avant un lancement à distance (qui prévient qui,
> et à quel moment), et la façon dont Anheart garantit que personne ne lance
> une séance depuis le site tant que quelqu'un se trouve près du bras ou de la
> capsule.

### Lancer

1. Cliquez **Mes machines**.
2. Sur la carte de la machine, cliquez **Lancer une séance auto**.
3. **Programme** : choisissez le programme. Une ligne rappelle sa zone cible,
   sa FC limite et sa vitesse max.
4. **Durée (minutes)** : laissez vide pour garder la durée du programme.
5. **Pratiquant** : choisissez le patient. La liste propose d'abord
   « Moi-même ».
6. Lisez la ligne « FC max du pratiquant », et vérifiez qu'aucun encadré
   orange ou rouge n'est affiché.
7. **Notes** : facultatif.
8. Cliquez **Lancer**.

![La fenêtre de lancement](../guides/img/site-08-lancer-seance-auto.png)

*La fenêtre de lancement. Ici le pratiquant sélectionné n'a pas de FC max :
l'encadré orange l'explique et le bouton Lancer est grisé. Capture du site
d'essai.*

### Si le bouton Lancer est grisé

Un encadré orange dit pourquoi.

| Message | Quoi faire |
|---|---|
| « Vous ne pouvez pas lancer de séance sur cette machine. » | La machine ne vous est pas attribuée. Voyez avec votre administrateur. |
| « La machine est hors ligne. » | Vérifiez avec l'opérateur que la machine est allumée, reliée à Internet, et que sa console est démarrée. |
| « La machine est déjà en séance. » | Attendez la fin de la séance en cours. |
| « Programmes auto désactivés sur cette machine : seules les séances manuelles, depuis la console de la machine, sont possibles. » | C'est un réglage de la machine. Il ne se change pas depuis le site. Appelez le support. |
| « Aucun programme n'a été synchronisé depuis la machine. » | La machine n'a envoyé aucun programme. Appelez le support. |
| « La FC max (ou l'année de naissance) du pratiquant n'est pas renseignée : un gestionnaire doit la saisir avant toute séance auto. » | [Section 4.2](#42-renseigner-la-physiologie). |
| « L'année de naissance du pratiquant n'est pas renseignée : un gestionnaire doit la saisir avant toute séance auto. » | [Section 4.2](#42-renseigner-la-physiologie). |
| « Séance auto réservée aux pratiquants d'au moins 18 ans. » | Pas de séance auto pour ce pratiquant. |

Un encadré **rouge** qui se termine par « Le serveur refusera ce lancement. »
veut dire que le programme est trop intense pour ce pratiquant, d'après les
contrôles du logiciel. Choisissez un autre programme, ou voyez le référent
médical.

### Après le clic sur Lancer

- **Un message rouge, en anglais, apparaît en haut de la fenêtre.** Le site a
  refusé. Vérifiez les points du tableau ci-dessus. Autre cause fréquente :
  une séance attend déjà sur cette machine. Cherchez-la dans **Sessions**,
  onglet **Tous**, et annulez-la ([section 8](#8-arrêter-ou-annuler-une-séance)).
- **Sinon la fenêtre se ferme** et la vue en direct de la nouvelle séance
  s'ouvre, avec le bandeau « En attente que la machine arme la séance… ».

## 7. Suivre une séance en direct

Pour ouvrir une séance en cours : **Sessions**, puis **Voir en direct** sur sa
ligne. Ou bien, sur le **Tableau de bord**, cliquez sa ligne dans **Sessions
récentes**.

### En attente

Le bandeau « En attente que la machine arme la séance… » veut dire que la
machine n'a pas encore pris votre demande. En temps normal, cela dure quelques
secondes : au repos, la machine interroge le site toutes les 3 secondes
environ.

Si le bandeau reste affiché, la machine n'a toujours pas pris votre demande.
Annulez la séance ([section 8](#8-arrêter-ou-annuler-une-séance)), puis voyez
avec l'opérateur.

> **Ne laissez jamais une séance en attente.** Elle n'expire pas. Tant qu'elle
> n'est pas annulée, la machine peut la prendre plus tard, par exemple quand
> elle redevient joignable, et démarrer sans aucune action à la console,
> alors que plus personne ne s'y attend.

Il arrive aussi que la machine refuse le départ. La séance prend alors le
statut « Échouée », avec un motif qui commence par « refusee par la
machine » : voir la [section 9.2](#92-lire-le-détail-dune-séance).

### En cours

![Une séance auto en cours](../guides/img/site-11-seance-auto-active.png)

*Une séance auto à son début : phase « Mesure de référence », le bras ne tourne
pas encore. Capture du site d'essai, machine simulée, prise avant la
traduction : le haut de la page et le bloc du bas y sont encore en anglais.*

Le panneau **Séance d'entraînement** affiche :

| Bloc | Ce que vous y lisez |
|---|---|
| **Fréquence cardiaque** | En bpm. En vert « Dans la zone », en bleu « Sous la zone », en rouge « Au-dessus de la zone ». Un tiret et « Pas de fréquence cardiaque fiable » quand la machine n'en a pas, ou qu'aucune valeur n'est arrivée depuis 20 secondes. |
| **Vitesse du bras** | En tr/min. Dessous, la vitesse du moteur. |
| **Charge** | En g. |
| **Phase** | « Mesure de référence », « Échauffement », « Maintien », « Retour au calme », « Récupération », « Terminé ». Une mention « Action de sécurité », suivie de sa valeur, s'ajoute quand la machine applique une action de sécurité. |
| **Écoulé** | Le temps depuis le départ, et dessous le temps « Restant ». |

Sous ces blocs, deux courbes :

- **Fréquence cardiaque** : la ligne rouge, sur la bande verte de la zone
  cible. Un trou dans la ligne veut dire : pas de fréquence fiable à ce
  moment ;
- **Vitesse du bras** : la vitesse « Mesurée » et la « Consigne ».

La séance commence par la phase « Mesure de référence » : le bras ne tourne
pas, la machine mesure la fréquence cardiaque de repos. La rotation commence
ensuite, sans autre action de votre part.

Une vitesse à zéro ne veut pas toujours dire que la séance est finie : au
début de la séance, le bras est à l'arrêt, puis il part de lui-même. Pendant
la séance, la machine peut aussi ralentir le bras, puis le laisser réaccélérer
quand la cause disparaît (par exemple une fréquence cardiaque qui redevient
lisible). Si la machine ramène elle-même le bras jusqu'à l'arrêt avant la fin
du programme, elle termine la séance : le bras ne repart pas, et quelqu'un
devra acquitter l'arrêt à la console avant toute nouvelle séance. La phase
affichée ne prouve pas l'arrêt du bras : lisez **Vitesse du bras**.

Les valeurs arrivent par paquets, toutes les 5 secondes environ. L'écran a
donc toujours plusieurs secondes de retard.

> **Le site n'est pas un écran de sécurité.** Il a du retard et dépend
> d'Internet. La surveillance de la séance se fait à la machine, par
> l'opérateur.

Sous le panneau, un bloc vient d'un ancien mode d'enregistrement : les cartes
« Qualité du signal », « Fréquence cardiaque (BPM) » et « Lots de données »,
puis un encadré « En attente des données ECG ». Il reste vide pour une séance
d'entraînement. Ignorez-le, y compris son message qui demande de vérifier le
capteur : pour la fréquence cardiaque d'une séance d'entraînement, seul le
panneau **Séance d'entraînement** compte. Pour la même raison, le badge
« Connexion... » en haut de la page ne dit rien de la machine.

### Signaux d'alerte

| Vous voyez | Ce que cela veut dire | Quoi faire |
|---|---|---|
| Un tiret et « Pas de fréquence cardiaque fiable » | La machine n'a plus de fréquence cardiaque fiable. Elle applique ses propres règles de sécurité : elle garde sa vitesse, puis ralentit, et termine la séance dès que le bras est à l'arrêt, au plus tard après 60 secondes. Si la fréquence revient alors que le bras tourne encore, la séance reprend seule. | Prévenez l'opérateur. |
| « Au-dessus de la zone », qui dure | La fréquence dépasse la zone cible. La machine baisse sa vitesse et applique ses règles de sécurité. Si elle ramène le bras jusqu'à l'arrêt, elle termine la séance. | Prévenez l'opérateur. Demandez l'arrêt si la situation vous inquiète. |
| La mention « Action de sécurité » | La machine a pris une mesure de sécurité. Sa valeur dit laquelle : « Vitesse figée » (elle garde sa vitesse), « Réduction » (elle la baisse), « Décélération » (elle termine la séance), « Arrêt rapide (rampe du variateur) » (arrêt d'urgence logiciel), « Mise en silence (arrêt par le variateur) » (la console ne commande plus le moteur). Tant que « Vitesse figée » reste affichée, une demande d'arrêt envoyée depuis le site ne ralentit pas le bras. | Prévenez l'opérateur. L'arrêt d'urgence ne se déclenche qu'à la machine. |
| Les nombres remplacés par des tirets, plus de nouveaux points | La machine n'envoie plus rien : liaison Internet coupée, ou machine arrêtée. Le site ne peut plus rien lui demander. | Téléphonez à l'opérateur. |

## 8. Arrêter ou annuler une séance

Peuvent le faire : le pratiquant, vous (pour vos machines) et un
administrateur.

### Annuler une séance en attente

1. Ouvrez la vue en direct de la séance. Juste après le lancement, vous y êtes
   déjà. Sinon : **Sessions**, onglet **Tous**, cliquez la ligne de la séance.
   Vous arrivez sur sa page de détail, qui n'a pas de bouton d'annulation.
   Cliquez alors **Voir en direct**, en haut à droite de cette page.
2. Cliquez **Annuler la séance**.
3. Dans la fenêtre « Annuler la séance ? », cliquez **Annuler la séance**.

La séance prend le statut « Échouée ». C'est normal : le site range ainsi les
séances annulées.

### Arrêter une séance en cours

1. Dans la vue en direct, cliquez **Arrêter la séance** (bouton rouge).
2. Lisez la fenêtre « Arrêter la séance ? » : « La machine va décélérer sur sa
   rampe de sécurité. La séance se terminera quand la machine confirmera
   l'arrêt. »
3. Cliquez **Arrêter la séance**.

![La confirmation d'arrêt](../guides/img/site-13-arreter-la-seance.png)

*La confirmation d'arrêt. Capture du site d'essai, machine simulée.*

Ensuite :

- le bandeau « Arrêt demandé, décélération en cours… » s'affiche et le bouton
  devient grisé ;
- **le bras peut tourner encore** : regardez **Vitesse du bras**, pas le
  bandeau ;
- quand la machine confirme l'arrêt, le bandeau devient « Séance terminée ».

> **Ce bouton n'est pas un arrêt d'urgence.** C'est une demande. La machine la
> lit toutes les 3 secondes environ. En temps normal, elle décélère ensuite
> progressivement. Deux cas où votre demande ne ralentit pas le bras :
>
> - la machine n'a plus Internet : la demande ne lui parvient pas ;
> - la machine garde sa vitesse pour une raison de sécurité (mention « Action
>   de sécurité » avec la valeur « Vitesse figée ») : elle enregistre la fin
>   de séance, mais ne ralentit pas tant que cette valeur reste affichée.
>
> En cas de danger, l'arrêt se fait à la machine, par l'opérateur.

Si la vitesse du bras ne baisse pas après votre demande, ou si le bandeau
« Arrêt demandé, décélération en cours… » reste affiché, téléphonez à
l'opérateur.

Une séance **manuelle**, démarrée à la console, apparaît aussi sur le site.
Vous pouvez demander son arrêt de la même façon. Vous ne pouvez jamais la
démarrer.

## 9. Lire une séance terminée et les rapports

### 9.1 Retrouver une séance

Cliquez **Sessions**.

- Les onglets **Tous**, **Actives**, **Terminées** et **Échouées** filtrent la
  liste. Une séance en attente n'apparaît que sous **Tous**.
- Le champ **Rechercher** filtre sur le nom du patient ou de la machine.
- La colonne **Type** dit de quelle séance il s'agit (**Auto** ou **Manuel**)
  et d'où elle a été lancée (**Tableau de bord** ou **Machine**).

Chaque ligne porte un statut :

| Statut de la ligne | Onglet | Sens |
|---|---|---|
| « En attente » | **Tous** seulement | Lancée depuis le site, pas encore prise par la machine. |
| « Active » | **Actives** | En cours. |
| « Terminée » | **Terminées** | Le programme est allé au bout, ou un arrêt normal a été demandé en premier. Ce statut ne dit pas que la séance s'est passée sans incident : voir le motif de fin. |
| « Échouée » | **Échouées** | Refusée par la machine, annulée, ou terminée d'abord par l'arrêt d'urgence de la console ou par une action de sécurité. Ce statut ne veut pas dire que le logiciel a eu une erreur. |

Cliquez une ligne : une séance en cours s'ouvre en direct, les autres ouvrent
leur détail.

### 9.2 Lire le détail d'une séance

![Le détail d'une séance terminée](../guides/img/site-14-seance-detail.png)

*Le détail d'une séance : la carte Entraînement, le motif de fin et les deux
courbes. Capture du site d'essai, machine simulée, prise avant la
traduction : le statut et les blocs qui entourent la carte Entraînement y
sont encore en anglais.*

Regardez la carte **Entraînement**. Les autres blocs de la page viennent d'un
ancien mode d'enregistrement : les cartes du haut (dont « Lots de données »)
et le bloc « Enregistrement ECG » restent vides ou sans intérêt pour une
séance d'entraînement.

Pour une séance de statut « Échouée », la page affiche en haut un encadré
rouge « Session échouée ». Son texte parle d'une erreur, même quand la séance
a été annulée ou arrêtée par sécurité. Lisez le motif de fin, que cet encadré
reprend.

| Élément | Sens |
|---|---|
| **Programme** | Le programme suivi. |
| **Zone cible** | La zone de fréquence cardiaque visée. |
| **Durée prévue** | La durée demandée au lancement. |
| **FC max du pratiquant** | La FC max retenue au moment du lancement. |
| **Opérateur** | La personne qui a lancé la séance. |
| **Origine** | **Tableau de bord** (lancée depuis le site) ou **Machine** (démarrée à la console). |
| **Motif de fin** | Pourquoi la séance s'est terminée. Voir ci-dessous. |
| **Télémétrie** | Les deux courbes de toute la séance. |

Le motif de fin est écrit par la machine, sans accents. Il commence par un
mot-clé, parfois suivi de deux-points et d'une précision. Il ne garde que la
première cause de la fin de séance.

| Le motif commence par | Sens | Quoi faire |
|---|---|---|
| « programme_complete » | Le programme est allé au bout. | Rien. |
| « operator_stop » | Un arrêt normal a été demandé en premier, à la console ou depuis le site. Si un arrêt d'urgence a suivi, il n'apparaît pas ici. | Rien, sauf si l'opérateur vous signale un incident. |
| « emergency_stop » | L'arrêt d'urgence de la console a été déclenché en premier. | Demandez à l'opérateur ce qui s'est passé. |
| « safety_verdict » | La machine s'est arrêtée seule, pour une raison de sécurité. | Demandez à l'opérateur ce qui s'est passé. La raison précise est sur la console. |
| « refusee par la machine » | La machine a refusé le départ. La suite du texte dit pourquoi. | Voyez avec l'opérateur : le plus souvent, une vérification reste à faire à la console. |
| « la boucle n'a ni demarre ni refuse » | La machine a pris la demande, mais rien ne s'est passé en 60 secondes. | Voyez avec l'opérateur, puis relancez. |
| Un texte en anglais, avec un nom | La séance a été annulée depuis le site avant son départ. | Rien. |
| « tick_exception », « shutdown » | Une erreur du logiciel, ou l'arrêt de la console pendant la séance. | Appelez le support. |
| Tout autre texte | | Notez le texte exact et appelez le support. |

Un arrêt d'urgence câblé, sur la machine, n'apparaît dans aucun motif : le
logiciel ne le voit pas. Seul l'opérateur peut vous dire s'il a été utilisé.

Les courbes se lisent comme dans la vue en direct
([section 7](#7-suivre-une-séance-en-direct)).

### 9.3 Les rapports

Cliquez **Rapports**. La page liste vos séances de statut « Terminée » (pas
celles de statut « Échouée »).

![La page Rapports](../guides/img/site-21-rapports.png)

*La page Rapports. Capture du site d'essai, vue par un administrateur, prise
avant la traduction : la ligne de chiffres sous une carte y est encore en
anglais.*

- **Voir** ouvre le détail de la séance, décrit ci-dessus. C'est là que se
  trouvent les courbes.
- **Télécharger PDF** produit un fichier intitulé « Rapport de session ECG ».
  C'est le rapport de l'ancien mode d'enregistrement : il donne l'identité,
  les horaires et les canaux, mais ni le programme, ni le motif de fin, ni les
  courbes d'une séance d'entraînement. Pour relire une séance, utilisez
  **Voir**.

### 9.4 Ce que vous ne verrez pas

Une séance démarrée à la console de la machine arrive sur le site sans nom de
pratiquant : les listes affichent un mot anglais à la place du nom, et la
séance elle-même affiche « Pratiquant non précisé ». Elle n'est rattachée à
aucun patient. Le patient ne la voit donc pas dans son compte.

## 10. Quand quelque chose ne va pas

| Vous voyez | Cause probable | Quoi faire |
|---|---|---|
| Juste après votre inscription, une page vide ou une erreur | Votre fiche n'existe pas encore. | Revenez à la page d'accueil du site, attendez quelques secondes, puis recommencez. |
| « Aucune machine disponible. » dans **Mes machines** | Aucune machine ne vous est attribuée. | Voyez avec votre administrateur. |
| Une machine **Hors ligne** | Le site n'a reçu aucun signal d'elle depuis plus de 90 secondes. | Vérifiez avec l'opérateur que la machine est allumée, reliée à Internet, et que sa console est démarrée. |
| « Données périmées », et « Aucun signal récent de la machine : les valeurs affichées peuvent être dépassées. » | Même cause. Les valeurs affichées sont anciennes. | Même chose. Ne vous fiez pas à ces valeurs. |
| « Programmes auto désactivés sur cette machine » | Réglage de la machine. | Appelez le support. |
| « Aucun programme synchronisé depuis la machine » | La machine n'a envoyé aucun programme. | Appelez le support. |
| Un patient absent de votre liste | Il ne vous est pas rattaché. | Voyez avec votre administrateur. |
| Sur le **Tableau de bord**, la carte **Patients** affiche un tiret et « Chargement... » | Ce compteur ne fonctionne pas encore. | Ouvrez la page **Patients**. |
| Un message rouge en anglais | Le site a refusé une action. | Notez le texte exact. Vérifiez vos droits. Appelez le support si vous ne comprenez pas. |

> **N'utilisez pas le bouton « Nouvelle session » de la page Sessions.** Il
> vient d'un ancien mode d'enregistrement. Il crée une séance qui reste en
> attente pour toujours, que le site ne permet pas d'annuler, et qui empêche
> ensuite tout lancement de séance auto sur cette machine. Si cela vous
> arrive, appelez le support.

## 11. Qui appeler

- **Un danger pendant une séance** : téléphonez à l'opérateur, à la machine.
  Le site ne peut pas arrêter la machine en urgence.
- **Une urgence médicale** : appelez d'abord les services d'urgence.
- **Un doute médical sur un patient ou un programme** : ne lancez pas.

> **À compléter par Anheart avant la semaine pilote :** le numéro et les
> horaires du support, ce qu'il faut lui transmettre, et le numéro à appeler
> pour un incident de sécurité.

> **À compléter par Anheart avant la semaine pilote :** le référent médical à
> joindre, et les cas où vous devez le joindre avant de lancer une séance.

> **À compléter par Anheart avant la semaine pilote :** les règles de
> confidentialité à appliquer aux données de santé de vos patients, et
> l'information à leur donner.

Ce que le site montre, et à qui : vous voyez la fiche des patients qui vous
sont rattachés et les séances de vos machines. Un patient ne voit que ses
propres séances. Un administrateur voit tout.

## 12. Petit lexique

| Mot | Sens |
|---|---|
| Console | L'écran de la machine, utilisé par l'opérateur sur place. |
| Droit de lancement | L'autorisation, pour un patient, de lancer lui-même une séance auto sur une machine. |
| FC limite | Le seuil de fréquence cardiaque de sécurité d'un programme. |
| FC max | La fréquence cardiaque maximale d'une personne : mesurée, ou estimée par le logiciel à partir de l'âge. |
| Opérateur | La personne à côté de la machine pendant la séance. |
| Patient | Un compte du site, pour la personne qui s'entraîne. |
| Pratiquant | La personne à bord pendant une séance. La console l'appelle « passager ». |
| Programme | Une séance type enregistrée sur la machine : zone cible, durée, vitesse maximale. |
| Séance auto | Séance sur un programme. La machine règle seule sa vitesse d'après la fréquence cardiaque. |
| Séance manuelle | Séance où l'opérateur choisit la vitesse, à la console. Jamais lancée depuis le site. |
| Zone cible | La plage de fréquence cardiaque que le programme cherche à tenir. |

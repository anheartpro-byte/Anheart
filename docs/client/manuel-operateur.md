# Manuel opérateur : la console de la machine

> **Brouillon, en attente de validation par la semaine pilote.**
> Version du 6 octobre 2026. Ce manuel décrit le logiciel tel qu'il existe à
> cette date. Aucun client ne l'a encore utilisé.
>
> - Les captures d'écran viennent d'essais en simulation (machine simulée),
>   pas d'une machine réelle. En cas d'écart, votre écran fait foi.
> - À cette date, aucune séance avec une personne à bord n'a encore eu lieu
>   avec ce logiciel.
> - À cette date, la documentation d'Anheart indique que la machine d'essai n'a
>   aucune sécurité matérielle indépendante du logiciel et du variateur. Lisez
>   les avertissements de la [section 2](#2-les-huit-règles-à-retenir) avant
>   toute séance.
> - Certaines informations dont vous avez besoin ne sont pas encore validées.
>   Elles sont signalées par un encadré qui commence par les mots *À compléter
>   par Anheart*. Tant qu'un de ces encadrés vous concerne, demandez la réponse
>   à Anheart avant d'agir.

Les autres guides : [guide gestionnaire](guide-gestionnaire.md) (le site web),
[guide passager](guide-passager.md) (la personne dans la capsule),
[sommaire](README.md).

---

## Sommaire

1. [À qui s'adresse ce manuel](#1-à-qui-sadresse-ce-manuel)
2. [Les huit règles à retenir](#2-les-huit-règles-à-retenir)
3. [Lire l'écran](#3-lire-lécran)
4. [Avant toute séance, attester l'arrêt d'urgence](#4-avant-toute-séance-attester-larrêt-durgence)
5. [Séance manuelle, capsule vide](#5-séance-manuelle-capsule-vide)
6. [Séance programmée, avec un passager](#6-séance-programmée-avec-un-passager)
7. [Séance lancée depuis le site](#7-séance-lancée-depuis-le-site)
8. [Arrêter la machine](#8-arrêter-la-machine)
9. [Quand quelque chose ne va pas](#9-quand-quelque-chose-ne-va-pas)
10. [Un démarrage est refusé](#10-un-démarrage-est-refusé)
11. [Quand arrêter et appeler le support](#11-quand-arrêter-et-appeler-le-support)
12. [Petit lexique](#12-petit-lexique)

---

## 1. À qui s'adresse ce manuel

Ce manuel est pour **l'opérateur** : la personne qui se tient à côté de la
machine, devant l'écran de la console, pendant toute la séance.

Votre rôle :

- vous préparez la console avant la séance ;
- vous démarrez et vous arrêtez la machine ;
- vous surveillez la vitesse, la fréquence cardiaque et les alertes ;
- vous savez à tout moment comment arrêter la machine
  ([section 8](#8-arrêter-la-machine)).

La console est une page web servie par l'ordinateur de la machine. Elle
fonctionne sans Internet. Elle affiche le nom « AnHeart » en haut à gauche.

Le site web (le tableau de bord à distance) est un autre outil. Il est décrit
dans le [guide gestionnaire](guide-gestionnaire.md).

> **À compléter par Anheart avant la semaine pilote :** comment mettre la
> machine sous tension, démarrer la console et l'ouvrir sur votre installation
> (écran, adresse, jeton d'accès s'il y en a un), et comment la redémarrer.

Si la console vous demande un jeton d'accès : page **Configuration**, carte
**Acces**, champ **Jeton partage**, puis bouton **Utiliser ce jeton**. Le jeton
est gardé pour cet onglet seulement.

## 2. Les huit règles à retenir

1. **Le bouton E-STOP de l'écran est un arrêt logiciel.** Il dépend du
   navigateur, du réseau et du logiciel. L'un d'eux peut être justement ce qui
   est en panne. Il ne remplace pas un arrêt d'urgence câblé sur la machine :
   lisez le premier avertissement placé sous ces règles.
2. **Le bras n'est arrêté que si la vitesse mesurée le dit.** Seules la
   pastille **Rotation** et la vitesse mesurée (carte **Vitesse mesuree**, ou
   carte **Mesure** de la page **Seance**) parlent du bras. Ni une cible à
   zéro, ni la pastille **Mode**, ni le nom de la phase ne prouvent que le bras
   est arrêté. La vitesse mesurée est celle que le variateur transmet à la
   console. À la date de ce brouillon, elle n'a été vérifiée qu'en
   simulation : regardez aussi le bras.
3. **Un nombre barré ne se croit pas.** Si l'écran ne reçoit plus de données,
   un bandeau rouge « NO LIVE DATA » apparaît et les grands nombres sont
   barrés. Regardez alors le bras, pas l'écran.
4. **Un arrêt demandé à l'écran n'est jamais instantané, et il n'est pas
   toujours suivi d'effet.** **E-STOP** demande une décélération immédiate :
   même alors, le bras tourne encore pendant la décélération. **STOP** demande
   un arrêt progressif, mais tant que la pastille **Securite** affiche
   « freeze », la console garde la vitesse et STOP ne ralentit pas le bras.
   Après un clic, regardez le bras et la vitesse mesurée. Si la vitesse ne
   baisse pas après un STOP, l'arrêt logiciel qui reste est **E-STOP**. Si
   elle ne baisse pas après un E-STOP, il ne reste que l'arrêt d'urgence câblé
   de votre machine.
5. **Pendant une séance, le bras peut partir sans prévenir.** Au début d'une
   séance programmée, le bras est à l'arrêt, puis la console le met en
   rotation d'elle-même. En séance manuelle, il part dès qu'une cible est
   acceptée. Un bras que la console a seulement ralenti peut réaccélérer seul.
   Personne ne s'approche du bras ni de la capsule tant que **Mode** affiche
   « SEANCE » ou « MANUEL », ni tant que **Rotation** n'affiche pas
   « a l'arret ».
6. **Un bras que la console a arrêté ne repart pas seul, mais une nouvelle
   séance peut démarrer sans vous.** Si la console ramène elle-même la vitesse
   commandée à zéro avant la fin d'une séance, elle termine la séance :
   **Mode** passe à « ARRET », et rien ne repart sans un acquittement à votre
   nom, puis un nouveau démarrage. À cet instant, le bras peut encore
   tourner : c'est **Rotation** qui dit s'il est arrêté, pas **Mode**. Tant
   que « ARRET » est affiché, la console n'accepte aucun démarrage. À
   « REPOS », en revanche, un démarrage peut venir du site web, sans aucune
   action à la console
   ([section 7](#7-séance-lancée-depuis-le-site)). Ce manuel ne dit pas à
   partir de quand on peut s'approcher du bras ou de la capsule : lisez
   l'avertissement *Avant de s'approcher du bras ou de la capsule*, sous ces
   règles.
7. **Restez devant l'écran, page de la console ouverte et affichée.** Pendant
   une séance, si plus aucune page de la console n'est ouverte, la console
   garde la vitesse au bout de 60 secondes (« freeze »), puis termine la
   séance au bout de 120 secondes.
8. **Tapez votre nom dès que vous ouvrez la console.** Sans nom d'opérateur,
   le bouton **STOP** est refusé. Le bouton **E-STOP**, lui, ne demande ni nom
   ni confirmation.

> **Arrêt d'urgence câblé : ce que le logiciel ne sait pas.**
>
> - Le logiciel ne sait pas si votre machine a un arrêt d'urgence câblé. Il
>   n'en reçoit aucun signal : il ne sait ni quand on l'actionne, ni quand on
>   le réarme.
> - L'attestation de la [section 4](#4-avant-toute-séance-attester-larrêt-durgence)
>   est votre propre déclaration. Le logiciel l'enregistre. Il ne la vérifie
>   pas.
> - À la date de ce brouillon, la documentation d'Anheart indique que la
>   machine d'essai n'a aucune sécurité matérielle indépendante du logiciel et
>   du variateur.
> - Sans arrêt d'urgence câblé installé et vérifié sur votre machine, vous ne
>   devez pas attester. Sans attestation, la console refuse tout démarrage, et
>   c'est voulu.

> **À compléter par Anheart avant la semaine pilote :** l'arrêt d'urgence câblé
> de votre machine : où il se trouve, comment le déclencher et le réarmer, ce
> qu'il fait exactement au moteur, et dans quel ordre l'utiliser avec le bouton
> **E-STOP** de l'écran. Tant que ce point n'est pas complété, ce manuel ne
> fixe aucun ordre entre les deux.

> **Avant de s'approcher du bras ou de la capsule.** Voici ce que fait le
> logiciel, selon ce qu'affiche **Mode**. À lui seul, cela ne dit pas quand on
> peut s'approcher : cette règle reste à fixer par Anheart.
>
> | **Mode** affiche | Ce que fait le logiciel |
> |---|---|
> | « SEANCE » | Le bras peut partir seul : au début du programme, il est à l'arrêt, puis la console le met en rotation d'elle-même. Un bras seulement ralenti peut réaccélérer seul. Si la console ramène elle-même la vitesse commandée à zéro avant la fin du programme, elle termine la séance et **Mode** passe à « ARRET ». |
> | « MANUEL » | Le bras suit la cible appliquée. À l'arrêt, il part dès qu'une cible est acceptée, depuis n'importe quelle page de console ouverte. Un bras seulement ralenti peut réaccélérer seul vers la cible. |
> | « ARRET » | Un arrêt est enregistré. Une fois qu'elle a ramené la vitesse commandée à zéro, la console ne la remonte pas. Elle n'accepte aucun démarrage, ni à la console ni depuis le site web. Mais le bras peut encore tourner : lisez **Rotation**. |
> | « REPOS » | Aucune séance n'est en cours. Un démarrage peut être accepté : à la console, ou depuis le site web sans aucune action à la console, même si aucune page de console n'est ouverte ([section 7](#7-séance-lancée-depuis-le-site)). La console refuse tout démarrage tant que l'attestation n'est pas faite, et tant que **Securite** n'affiche pas « none ». |
>
> À savoir aussi :
>
> - Acquitter une alerte, ou faire l'attestation, lève ces refus : une séance
>   lancée depuis le site peut alors partir dans les secondes qui suivent.
> - **Mode** peut rester longtemps sur « ARRET » : pendant toute la phase
>   « recovery » après un arrêt en séance programmée, y compris un E-STOP ;
>   tant que le variateur reste en défaut
>   ([section 9.5](#95-le-variateur-passe-en-défaut)) ; ou jusqu'au
>   redémarrage de la console avec « go_silent ». Il peut aussi repasser de
>   « REPOS » à « ARRET » après une séance, sans que rien ne soit commandé
>   (alerte « session_overrun », [section 9.2](#92-la-console-agit-seule)).
> - La console ne voit pas l'arrêt d'urgence câblé. À lui seul, il peut ne
>   rien verrouiller dans le logiciel, et la console peut garder une vitesse
>   commandée ([Après un arrêt d'urgence câblé](#après-un-arrêt-durgence-câblé)).
> - Ces comportements ont été vérifiés en simulation seulement.
>
> Deux points restent à compléter par Anheart :
>
> - comment être sûr que personne ne lance une séance depuis le site pendant
>   que quelqu'un se trouve près du bras ou de la capsule (encadré de la
>   [section 7](#7-séance-lancée-depuis-le-site)) ;
> - à partir de quand, et avec quelles précautions, on peut s'approcher du bras
>   ou de la capsule quand **Mode** affiche « ARRET » ou « REPOS », y compris
>   pour porter secours à un passager (dernier encadré de la
>   [section 11](#11-quand-arrêter-et-appeler-le-support)).

## 3. Lire l'écran

![La console au repos, page Tableau de bord](../guides/img/console-01-tableau-de-bord-repos.png)

*La console au repos : les pastilles d'état et le menu à gauche, les mesures au
centre, STOP et E-STOP en bas. Capture prise en simulation.*

La console écrit ses libellés sans accents (par exemple **Securite**,
**Seance**). Ce manuel les reproduit tels qu'ils s'affichent. Les titres des
cartes apparaissent en majuscules à l'écran.

### Les six pastilles d'état

Elles sont à gauche, sur toutes les pages. Vert : normal. Orange : quelque
chose est en cours. Rouge : problème ou arrêt.

| Pastille | Ce qu'elle affiche | Ce que cela veut dire |
|---|---|---|
| **Mode** | « REPOS » | Aucune séance n'est en cours. Une séance peut démarrer, y compris depuis le site web ([section 2](#2-les-huit-règles-à-retenir), règle 6). |
| | « MANUEL » | Une séance manuelle est en cours. |
| | « SEANCE » | Une séance programmée est en cours. |
| | « ARRET » | Un arrêt est enregistré et n'est pas terminé, par exemple une fin de séance en cours. Cela ne dit pas que le bras est arrêté, ni même qu'il ralentit : regardez **Rotation** et **Vitesse mesuree**. Tant que « ARRET » est affiché, aucun démarrage n'est accepté. |
| **Etat** | « idle » | Aucune séance n'est ouverte. C'est le seul état où un démarrage est accepté, mais cela ne suffit pas : il peut être refusé pour une autre raison. |
| | « starting », « running », « stopping » | Un démarrage vient d'être accepté, une séance est ouverte, un arrêt a été demandé. Affichée en rouge : un arrêt d'urgence est verrouillé. **Etat** ne suit pas toujours **Mode** : voir sous ce tableau. |
| **Liaison** | « en direct » | L'écran reçoit des données fraîches. |
| | « donnees figees », « hors ligne » | L'écran ne reçoit plus rien. Ne croyez plus les nombres. |
| **Rotation** | « a l'arret » | À cet instant, la vitesse mesurée est inférieure à 0,05 tr/min. Cela ne dit pas que le bras va rester à l'arrêt : voir les règles 5 et 6 ([section 2](#2-les-huit-règles-à-retenir)). |
| | « EN ROTATION » | Le bras tourne. |
| | « VITESSE INCONNUE » | La vitesse n'a pas pu être lue récemment. Ne concluez jamais que le bras est arrêté. |
| **Securite** | « none » | La console ne demande aucune action de sécurité. |
| | toute autre valeur | La console agit seule : voir la [section 9.2](#92-la-console-agit-seule). |
| **Console** | « mouvement actif » | La console peut commander le moteur. |

**Etat** et **Mode** peuvent se contredire. Quand c'est la console qui décide
de terminer la séance, **Etat** peut rester sur « running » alors que **Mode**
affiche « ARRET ». Et quand **Mode** repasse à « ARRET » après une séance
(alerte « session_overrun », [section 9.2](#92-la-console-agit-seule)),
**Etat** affiche « idle » alors que tout démarrage est refusé. Pour savoir où
en est la machine, lisez **Mode**, **Rotation** et **Securite**, pas **Etat**.

### Les deux bandeaux rouges

Ils s'affichent tout en haut, sur toutes les pages.

| Bandeau | Ce que cela veut dire |
|---|---|
| « NO LIVE DATA » | L'écran ne reçoit plus de données. Les nombres affichés sont anciens : voir la [section 9.3](#93-bandeau-rouge-no-live-data). |
| « ARRET D'URGENCE VERROUILLE » | Un arrêt d'urgence logiciel est verrouillé : un E-STOP, ou un arrêt d'urgence décidé par la console. Le bandeau reste affiché jusqu'à l'acquittement ([section 9.1](#91-un-arrêt-de-sécurité-est-verrouillé)). Il dit que l'arrêt est verrouillé, pas que le bras est arrêté : il le rappelle lui-même par les mots « verrouille ne veut pas dire arrete ». |

### Les pages

| Page (menu de gauche) | À quoi elle sert |
|---|---|
| **Tableau de bord** | La séance manuelle et les mesures principales : fréquence cardiaque, vitesse mesurée, variateur, capteur cardiaque. |
| **Capteurs** | Les autres capteurs, pour surveillance seulement. Aucun ne commande le moteur. |
| **Seance** | La séance programmée, son suivi détaillé et la liste **Evenements**. |
| **Configuration** | L'accès à la console et des informations sur le système. |
| **Securite** | Les alertes de sécurité, l'acquittement et l'attestation du câblage. |

### En bas de l'écran

Deux boutons, présents sur toutes les pages :

- **STOP** (« rampe controlee ») : l'arrêt normal ;
- **E-STOP** (« verrouille immediatement ») : l'arrêt d'urgence de l'écran.

Ils sont décrits à la [section 8](#8-arrêter-la-machine).

À gauche de ces boutons, une ligne d'état rappelle l'état de la liaison et le
mode, en minuscules. Elle ajoute la phase quand une séance est en cours ou se
termine. Au repos, elle n'affiche pas de phase.

### Les vitesses

Le grand nombre est la vitesse du bras, en tours par minute (« tr/min de sortie
(bras) »). Dessous, la console redonne la même vitesse autrement : celle du
moteur (« moteur »), la fréquence du variateur (« variateur », en Hz), et deux
charges en g, « Gc » (centripète) et « Gr » (résultante). Ces charges sont
calculées au point de référence de la machine. La charge est plus forte vers
les pieds de la personne à bord, plus éloignés de l'axe.

Dans ce manuel, « tr/min » veut dire tr/min du bras, sauf quand le texte
précise qu'il s'agit du moteur.

## 4. Avant toute séance, attester l'arrêt d'urgence

À faire **à chaque démarrage de la console**, avant toute séance. La console
oublie l'attestation quand elle redémarre. Sans attestation, tout démarrage est
refusé.

1. Vérifiez sur la machine, de vos yeux, les deux faits que la console vous
   demande de confirmer.
2. Cliquez **Securite** dans le menu de gauche.
3. Dans la carte **Cablage de l'arret d'urgence**, cochez les deux cases,
   seulement si vous êtes sûr des deux faits :
   - **Le pont STO a ete retire du variateur.**
   - **Un arret d'urgence a accrochage est cable normalement ferme sur P24 → STO.**
4. Tapez votre nom dans **Votre nom**.
5. Cliquez **Enregistrer l'attestation**.
6. Vérifiez : la pastille de la carte passe de « non atteste » à « atteste »,
   et une ligne sous le bouton indique votre nom et l'heure.

![Page Securite après l'attestation](../guides/img/console-03-securite-atteste.png)

*Page Securite après l'attestation : pastille « atteste » dans la carte de
droite. Capture prise en simulation.*

> **Attention.** Le logiciel ne vérifie pas le câblage et ne voit pas l'arrêt
> d'urgence câblé. Il enregistre votre déclaration, à votre nom. Ne cochez
> jamais une case dans le seul but de débloquer la console. Si vous ne savez
> pas vérifier l'un des deux faits, ou si l'un des deux est faux, n'attestez
> pas et appelez le support.

> **À compléter par Anheart avant la semaine pilote :** la procédure de
> vérification du câblage de l'arrêt d'urgence sur votre machine : ce que
> l'opérateur doit contrôler, et comment, avant de cocher chacune des deux
> cases.

Si vous ne cochez qu'une case, rien n'est enregistré et un message en anglais
s'affiche sous le bouton.

Une fois l'attestation faite, ce n'est plus elle qui retient un démarrage :
une séance lancée depuis le site web peut alors partir sans aucune action à la
console ([section 7](#7-séance-lancée-depuis-le-site)).

## 5. Séance manuelle, capsule vide

En mode manuel, vous choisissez vous-même la vitesse.

> **Capsule vide, toujours.** La console ne propose le mode manuel qu'avec la
> déclaration « BANC - personne a bord : NON ». Dans ce mode, elle n'applique
> pas ses règles de sécurité sur la fréquence cardiaque. Ne lancez jamais une
> séance manuelle avec une personne dans la capsule.

1. Faites l'attestation ([section 4](#4-avant-toute-séance-attester-larrêt-durgence)).
2. Vérifiez que la capsule est vide et que personne ne se trouve près du bras.
3. Cliquez **Tableau de bord**.
4. Dans la carte **Mode MANUEL**, cochez **BANC - personne a bord : NON
   (moteur decouple, ou bras couple avec la capsule VIDE)**.
5. Tapez votre nom dans **Operateur**.
6. Cliquez **Demarrer MANUEL**. Le mode passe à « MANUEL ». Le bras ne tourne
   pas encore : la cible vaut 0.
7. Composez la vitesse voulue avec **−1 tr/min**, **+1 tr/min**, **− palier**
   et **+ palier** (un palier vaut 0,1 Gr). Le grand nombre orange est un
   brouillon : rien n'est encore envoyé à la machine.
8. Lisez la ligne « plafond » : c'est la vitesse maximale autorisée sur votre
   machine. Les boutons ne la dépassent pas.
9. Cliquez **Appliquer**. La machine rejoint cette vitesse progressivement.
10. Pendant le changement de vitesse, un bandeau orange **RAMPE EN COURS : NE
    PAS BOUGER LA TETE** indique la vitesse visée et le temps restant.
11. Suivez la carte **Vitesse mesuree**. La ligne « rampe » affiche « cible
    atteinte » quand la vitesse commandée a rejoint la cible. Cela ne dit rien
    de la vitesse mesurée : c'est elle qu'il faut lire.
12. Pour finir : **STOP** ([section 8](#8-arrêter-la-machine)).

![Séance manuelle pendant un changement de vitesse](../guides/img/console-08-manuel-rampe.png)

*Séance manuelle pendant un changement de vitesse : le bandeau orange en haut,
le brouillon en orange, la vitesse mesurée à droite. Capture prise en
simulation, capsule vide. Elle peut dater d'une version antérieure de
l'écran.*

À savoir :

- En montant depuis 0, le premier clic saute directement à la plus petite
  vitesse possible (ligne « minimum de rotation »).
- La ligne « cible appliquee » dit ce que la machine vise vraiment. Fiez-vous à
  elle, pas au brouillon orange.
- Une réponse qui commence par « accepte : » ou « cible envoyee : » sous les
  boutons veut seulement dire que la demande est reçue. La machine vérifie
  ensuite. Si elle refuse, le refus apparaît dans la liste **Evenements** de la
  page **Seance**, sur une ligne « refused ».
- Pour changer de vitesse : composez un nouveau brouillon, puis **Appliquer**.
- Vous pouvez ramener vous-même le bras à l'arrêt en appliquant une cible de
  0. Si **Securite** affiche « none » à ce moment, la séance continue :
  **Mode** reste « MANUEL », et une nouvelle cible fera repartir le bras.
- **Sur un bras à l'arrêt, une cible n'est prise que si rien ne retient le
  bras.** Tant que **Securite** affiche autre chose que « none », une cible
  autre que 0 est refusée : la liste **Evenements** de la page **Seance**
  affiche une ligne « refused » qui contient « consigne refusee » et « tient
  le bras a l'arret », avec le nom de la règle et ce qu'il faut attendre. Une
  cible déjà appliquée, que le bras n'avait pas encore commencé à suivre, est
  retirée : la ligne contient alors « remise a 0 ». Dans les deux cas, rien
  ne partira quand la cause disparaîtra. C'est à vous de redonner la cible
  ensuite : les deux lignes se terminent par « puis redonner la cible ».
- **La ligne « remise a 0 » peut aussi apparaître sans aucune alerte.** C'est
  le cas quand le variateur n'a pas confirmé le premier pas de vitesse : la
  ligne contient alors « le variateur n'a pas confirme la consigne », et
  **Securite** peut afficher « none ». La console ne redemande pas cette
  vitesse, mais le variateur peut avoir reçu ce premier pas : lisez
  **Rotation** et **Vitesse mesuree**
  ([section 10](#10-un-démarrage-est-refusé)).
- Si **Securite** affiche « freeze », la console garde la vitesse : ni
  **Appliquer**, ni **STOP** ne la font baisser
  ([section 9.2](#92-la-console-agit-seule)). Sur un bras qui tourne, une
  cible appliquée pendant ce temps est gardée, puis suivie sans autre clic
  quand « freeze » disparaît.
- Si la console a seulement gardé ou baissé la vitesse et que la cause
  disparaît, elle ramène seule le bras vers la cible appliquée. Elle ne le
  fait pas si une fin de séance a été enregistrée entre-temps
  ([section 8](#8-arrêter-la-machine)).
- Si la console ramène elle-même la vitesse commandée à zéro, la séance se
  termine, même capsule vide : **Securite** affiche « ramp_down », avec la
  règle « session_standstill », et **Mode** passe à « ARRET ». Le bras peut
  encore tourner à cet instant : lisez **Rotation**. Il ne repart pas. Il
  faut acquitter une fois **Mode** revenu à « REPOS », puis démarrer une
  nouvelle séance ([section 9.1](#91-un-arrêt-de-sécurité-est-verrouillé)).
- Une séance manuelle s'arrête seule au bout de 60 minutes, comme après un
  STOP.
- Après toute fin de séance, la cible revient à 0. Pour repartir, il faut
  redémarrer une séance.
- Un peu plus de 60 minutes après le départ d'une séance manuelle, même
  arrêtée depuis longtemps, une alerte « session_overrun » apparaît
  d'elle-même si aucune autre séance n'a démarré entre-temps
  ([section 9.2](#92-la-console-agit-seule)).

## 6. Séance programmée, avec un passager

Dans une séance programmée, la console règle seule la vitesse pour garder la
fréquence cardiaque du passager dans la zone du programme.

> **Cette fonction doit avoir été autorisée sur votre machine par Anheart.**
> Par défaut, elle ne l'est pas. Dans ce cas :
>
> - la page **Seance** affiche « seances programmees desactivees sur cette
>   console (jalon M5) : utiliser le mode MANUEL » et le bouton **Demarrer la
>   seance** reste grisé ;
> - ou bien le démarrage est refusé, avec dans la liste **Evenements** un
>   message contenant « personne a bord refusee ».
>
> Vous ne pouvez pas changer ce réglage depuis la console. Appelez le support.

### Ce que ce manuel ne peut pas encore vous dire

> **À compléter par Anheart avant la semaine pilote :** les critères médicaux
> d'admission d'un passager, et les valeurs médicales validées pour vos séances
> (zone cardiaque, seuils cardiaques, charge maximale, durée). Les valeurs que
> la console affiche aujourd'hui sont des réglages provisoires du logiciel, pas
> des recommandations médicales.

> **À compléter par Anheart avant la semaine pilote :** l'installation du
> passager dans la capsule et sa sortie (position, maintien), la pose des
> électrodes du capteur cardiaque, et les contrôles à faire avant de démarrer.

> **À compléter par Anheart avant la semaine pilote :** comment le passager et
> vous communiquez pendant la séance, et le signal convenu pour demander
> l'arrêt. Le logiciel ne gère aucune commande d'arrêt placée dans la capsule.

### Démarrer

1. Faites l'attestation ([section 4](#4-avant-toute-séance-attester-larrêt-durgence)).
2. Installez le passager et le capteur cardiaque (voir les encadrés
   ci-dessus). Une fois l'attestation faite, une séance lancée depuis le site
   peut démarrer pendant cette installation
   ([section 2](#2-les-huit-règles-à-retenir), règle 6, et encadré de la
   [section 7](#7-séance-lancée-depuis-le-site)).
3. Cliquez **Tableau de bord**. Regardez la carte **Frequence cardiaque
   (regulation)** : un nombre doit être affiché, non barré, avec la pastille
   « good ». Sans fréquence cardiaque fiable, ne démarrez pas.
4. Cliquez **Seance**.
5. Choisissez le programme dans la liste **Profil**.
6. Laissez **Duree totale (minutes, vide = celle du profil)** vide, sauf
   consigne contraire.
7. Tapez votre nom dans **Operateur**.
8. Tapez l'âge du passager dans **Age du passager (ans, obligatoire)**. Sans
   âge, ou en dessous de l'âge minimum réglé sur la machine, le démarrage est
   refusé.
9. Cliquez **Previsualiser**. Rien ne démarre. Lisez la carte **Ce que ce
   programme ferait** : « zone » (la zone cardiaque visée), « max absolu » et
   « critique » (les deux seuils cardiaques de sécurité), « plafond » (la
   vitesse maximale, donnée ici en tr/min du moteur), « total » (la durée) et
   « charge au plafond » (la charge à cette vitesse maximale, avec la vitesse
   du bras correspondante). Si une valeur ne correspond pas à ce qui a été
   décidé pour ce passager, ne démarrez pas.
10. Cliquez **Demarrer la seance**. Si le bouton est grisé sans aucun message,
    c'est que l'attestation n'est pas faite, ou que **Etat** n'affiche pas
    « idle ».
11. Regardez la liste **Evenements**, en bas de la page : vous devez voir
    « start_requested », puis « session_running ». Une ligne « refused » veut
    dire que la machine a refusé, avec la raison
    ([section 10](#10-un-démarrage-est-refusé)).

> **Dès que la séance est démarrée, personne ne s'approche du bras ni de la
> capsule.** La séance commence bras à l'arrêt, puis la console le met en
> rotation d'elle-même.

### Suivre

![Page Seance pendant une séance programmée](../guides/img/console-19-seance-programmee-en-cours.png)

*Page Seance pendant une séance programmée (simulation, passager simulé). Les
valeurs affichées sont celles d'un programme d'essai, pas des valeurs validées
pour vos séances. La capture peut dater d'une version antérieure de l'écran.*

| Carte | Ce que vous y lisez |
|---|---|
| **Frequence cardiaque** | La fréquence du passager, en bpm. La bande montre la zone du programme, le trait la fréquence actuelle. Dessous : « cible », et le temps passé « dans la zone », « au-dessus », « en dessous ». |
| **Phase** | La phase en cours (un tiret quand aucune séance n'est en cours), le temps « ecoule » et « restant », et l'action de sécurité en cours (« securite »). |
| **Mesure** | La vitesse mesurée du bras. C'est elle qui dit si le bras tourne. |
| **Consigne** | La vitesse demandée par la console. « non confirmee » est normal pendant une montée. |
| **Securite** | L'action de sécurité en cours, et le bouton **Verdicts et acquittement** qui ouvre la page **Securite**. |
| **Evenements** | Les derniers événements, le plus récent en haut. |

Les phases, telles que la console les nomme :

| Phase affichée | Ce qui se passe |
|---|---|
| « baseline » | Le bras ne tourne pas. La console mesure la fréquence cardiaque de repos. |
| « warmup » | La rotation commence et la vitesse monte progressivement. |
| « hold » | La console ajuste la vitesse pour tenir la fréquence cardiaque dans la zone. Elle peut la baisser puis la remonter. Si elle ramène la vitesse commandée à zéro, la séance se termine ([section 9.2](#92-la-console-agit-seule)). |
| « cooldown » | La vitesse redescend jusqu'à l'arrêt. |
| « recovery » | En fonctionnement normal, le bras est arrêté et le passager est toujours à bord. La console surveille encore sa fréquence cardiaque. |
| « done » | Les phases de la séance sont terminées. À « REPOS », la console n'affiche pas ce mot : la carte **Phase** montre un tiret. Vous ne le lirez donc que si **Mode** affiche encore « ARRET », par exemple tant que le variateur est en défaut ([section 9.5](#95-le-variateur-passe-en-défaut)). |

Ce tableau décrit le déroulement normal. Le nom de la phase ne prouve jamais
que le bras est arrêté : après un arrêt demandé, la console peut afficher
« cooldown » puis « recovery » alors que le bras tourne encore. Lisez
**Rotation** et **Vitesse mesuree**.

Pendant « warmup » et « hold », la console peut aussi ralentir le bras
d'elle-même pour une raison de sécurité, puis le laisser réaccélérer. Si elle
ramène la vitesse commandée à zéro, elle termine la séance : le bras ne repart
pas ([section 9.2](#92-la-console-agit-seule)).

La séance se termine seule à la fin du programme. Pour l'arrêter avant :
**STOP** ([section 8](#8-arrêter-la-machine)).

Après la séance, une alerte « session_overrun » apparaît d'elle-même, environ
30 secondes après la fin d'un programme mené à son terme. Lisez la
[section 9.2](#92-la-console-agit-seule) avant d'enchaîner une autre séance.

## 7. Séance lancée depuis le site

Un administrateur, un gestionnaire, ou un patient qui en a reçu le droit, peut
lancer une séance programmée depuis le site web. La machine va chercher cette
demande elle-même, toutes les 3 secondes environ quand elle est au repos. La
demande passe par les mêmes contrôles qu'un démarrage fait à la console.

> **Une séance lancée depuis le site peut démarrer sans aucune action à la
> console.** Quand **Mode** affiche « REPOS », la console va chercher
> d'elle-même une demande en attente sur le site, même si aucune page de la
> console n'est ouverte. Si les contrôles passent, **Mode** passe à
> « SEANCE », avec la phase « baseline » et le bras à l'arrêt. Ensuite, la
> console peut mettre le bras en rotation d'elle-même, comme pour une séance
> démarrée à la console.
>
> Sur le site, une demande en attente n'expire pas. La console peut donc la
> prendre plus tard que le clic, par exemple quand sa liaison Internet
> revient. Cela vaut aussi juste après une séance : quand **Mode** revient à
> « REPOS » sans arrêt verrouillé, une demande en attente peut partir dans les
> secondes qui suivent.
>
> **Mode** « REPOS » et **Rotation** « a l'arret » ne garantissent donc pas
> que le bras va rester à l'arrêt.

Ce que cela change pour vous :

- La séance ne part que si l'attestation est faite, s'il n'y a aucun arrêt de
  sécurité verrouillé à acquitter, et si **Etat** affiche « idle ». À
  « REPOS », si l'attestation manque ou si un arrêt verrouillé attend, le
  site reçoit un refus, avec la raison.
- Une séance manuelle ne peut jamais être lancée depuis le site.
- Vous restez l'opérateur. Vous devez être à côté de la machine, passager
  installé, avant que la demande soit envoyée. Le site ne voit pas la machine.
- Tapez votre nom dans le champ **Operateur** de la page **Seance** : sans
  nom, votre bouton **STOP** serait refusé.
- Le suivi et l'arrêt se font comme à la [section 6](#6-séance-programmée-avec-un-passager).
  Le site peut aussi demander l'arrêt : la console l'exécute comme un STOP
  ordinaire, avec les mêmes limites ([section 8](#8-arrêter-la-machine)).
- Si la liaison Internet tombe pendant la séance, la séance continue selon les
  règles de la console. L'affichage du site se fige, et le site ne peut plus
  demander l'arrêt.

> **À compléter par Anheart avant la semaine pilote :** l'organisation prévue
> entre la personne qui lance depuis le site et l'opérateur à la machine (qui
> prévient qui, et à quel moment), et la façon dont Anheart garantit que
> personne ne lance une séance depuis le site tant que quelqu'un se trouve
> près du bras ou de la capsule.

## 8. Arrêter la machine

| Situation | Ce que vous faites |
|---|---|
| Fin normale, changement d'avis, le passager demande à s'arrêter sans urgence | **STOP**, puis surveillez la vitesse mesurée. |
| Vous voulez arrêter le bras alors que **Securite** affiche « freeze » | Tant que « freeze » est affiché, STOP ne ralentit pas le bras. Des deux boutons, seul **E-STOP** le ralentit. |
| Danger immédiat, comportement anormal de la machine, doute sérieux | **E-STOP**, et l'arrêt d'urgence câblé de votre machine. L'ordre entre les deux reste à préciser par Anheart ([section 2](#2-les-huit-règles-à-retenir)). |
| L'écran ne répond plus, bandeau rouge « NO LIVE DATA » | Regardez le bras, puis suivez la [section 9.3](#93-bandeau-rouge-no-live-data). |

### STOP, l'arrêt normal

1. Cliquez **STOP**, en bas de n'importe quelle page.
2. **Mode** passe à « ARRET », **Etat** à « stopping ». Cela veut dire que la
   fin de séance est enregistrée. Cela ne dit pas que le bras ralentit.
3. Regardez la carte **Vitesse mesuree**. En fonctionnement normal, la
   vitesse se met à baisser dans les secondes qui suivent. Si elle ne baisse
   pas, quelle qu'en soit la raison, l'arrêt logiciel qui reste est
   **E-STOP**. Une raison connue : tant que **Securite** affiche « freeze »,
   STOP ne ralentit pas le bras.
4. Attendez que **Rotation** affiche « a l'arret » et que **Mode** revienne à
   « REPOS ». Si **Mode** ne revient pas à « REPOS », lisez **Securite** et
   la carte **Variateur**, puis la
   [section 9](#9-quand-quelque-chose-ne-va-pas).

![Après STOP, le bras ralentit](../guides/img/console-11-stop-rampe-arret.png)

*Après STOP : la cible est déjà à zéro, mais la vitesse mesurée montre que le
bras tourne encore. Capture prise en simulation, capsule vide. Elle peut
dater d'une version antérieure de l'écran.*

À savoir :

- En temps normal, STOP termine la séance en douceur, sur une décélération
  progressive. C'est voulu : un freinage plus brutal ferait passer le variateur
  en défaut, et le bras ralentirait alors sans contrôle, plus longtemps.
- **Tant que Securite affiche « freeze », STOP ne ralentit pas le bras.** La
  console enregistre la fin de séance, affiche « ARRET », puis les phases
  « cooldown » et « recovery », mais elle garde la vitesse. Ne vous fiez pas à
  **Mode** : lisez **Rotation** et **Vitesse mesuree**. Des deux boutons, seul
  **E-STOP** ralentit alors le bras.
- Une fois STOP enregistré (**Mode** « ARRET »), la séance ne reprend pas :
  si la cause d'un « freeze » ou d'un « reduce » disparaît ensuite, la console
  ramène la vitesse commandée à zéro au lieu de reprendre la séance. En séance
  programmée, la vitesse commandée peut encore monter un court instant juste
  après le clic, avant de redescendre.
- Une séance programmée arrêtée par STOP passe encore par ses phases de fin
  (« cooldown », puis « recovery »). Le retour à « REPOS » n'est donc pas
  immédiat.
- Si une fenêtre « STOP refuse : » s'ouvre, lisez-la. Soit il n'y a rien à
  arrêter, soit un arrêt est déjà en cours, soit aucun nom d'opérateur n'est
  saisi. **Si le bras tourne et que STOP est refusé, cliquez E-STOP.**

### E-STOP, l'arrêt d'urgence de l'écran

1. Cliquez **E-STOP**. Il part au premier clic : aucune confirmation, aucun
   nom. Il agit aussi quand **Securite** affiche « freeze ».
2. Regardez le bras et la carte **Vitesse mesuree**. En fonctionnement
   normal, la machine décélère : elle n'est pas arrêtée tout de suite. Si le
   bras ne ralentit pas dans les secondes qui suivent le clic, n'attendez
   aucun message : il ne reste que l'arrêt d'urgence câblé de votre machine.
3. En fonctionnement normal, un bandeau rouge « ARRET D'URGENCE VERROUILLE »
   apparaît en haut de l'écran, **Securite** passe à « quick_stop » et
   **Etat** à « stopping » en rouge. Le bandeau dit que l'arrêt est
   verrouillé, pas que le bras est arrêté.
4. Une fois le bras arrêté, la machine reste verrouillée, et le bandeau reste
   affiché. Il faut acquitter
   ([section 9.1](#91-un-arrêt-de-sécurité-est-verrouillé)).

E-STOP est un arrêt logiciel. En cas de danger, utilisez aussi l'arrêt
d'urgence câblé de votre machine. L'ordre entre les deux dépend de ce que
l'arrêt câblé fait au moteur : il reste à préciser par Anheart
([section 2](#2-les-huit-règles-à-retenir)).

![Après E-STOP, la machine décélère](../guides/img/console-12-estop-verrouille.png)

*Après E-STOP en séance manuelle : le bandeau rouge en haut, Mode « ARRET »,
Etat « stopping » en rouge, Securite « quick_stop », et le bras tourne encore
(Rotation « EN ROTATION »). Capture prise en simulation, capsule vide.*

Si une fenêtre annonce que « la demande d'arret d'urgence a echoue » et se
termine par « UTILISEZ L'ARRET CABLE », la demande n'a pas abouti. Il ne
reste que l'arrêt d'urgence câblé de votre machine.

L'absence de cette fenêtre ne prouve rien. Si la console ne répond pas, la
fenêtre peut ne pas s'ouvrir, ou s'ouvrir trop tard. Si le bandeau
« ARRET D'URGENCE VERROUILLE » apparaît, la console a bien verrouillé un
arrêt d'urgence : cela ne dit pas encore que le bras ralentit. Fiez-vous au
bras : c'est son ralentissement qui montre que l'arrêt a lieu.

### Après un arrêt d'urgence câblé

La console ne voit pas l'arrêt d'urgence câblé. Après son utilisation, elle
peut encore croire la séance en cours et garder une vitesse commandée.

1. Avant de réarmer l'arrêt câblé, cliquez **E-STOP** sur la console. Si
   l'écran était figé, faites-le dès qu'il revient.
2. Vérifiez le bandeau rouge « ARRET D'URGENCE VERROUILLE », **Securite**
   « quick_stop » et **Rotation** « a l'arret ».
3. Réarmez l'arrêt câblé seulement ensuite, selon la procédure d'Anheart
   ([section 2](#2-les-huit-règles-à-retenir)).
4. Acquittez ([section 9.1](#91-un-arrêt-de-sécurité-est-verrouillé)).

## 9. Quand quelque chose ne va pas

### 9.1 Un arrêt de sécurité est verrouillé

Après un E-STOP, ou quand la console a arrêté la machine elle-même, tout
nouveau démarrage est refusé. Rien ne se débloque seul : quelqu'un doit
acquitter, par son nom.

1. Attendez la fin complète de la séance : **Rotation** « a l'arret » et
   **Mode** « REPOS ». Après une séance programmée, **Mode** ne revient à
   « REPOS » qu'une fois la phase « recovery » terminée. Sa durée est celle du
   programme : 5 minutes pour les deux programmes fournis par défaut à la date
   de ce brouillon, qui ne sont pas des valeurs validées pour vos séances. Un
   acquittement donné plus tôt peut ne pas tenir. C'est le cas pour la règle
   « session_standstill » : la console répond qu'elle l'a pris, mais l'alerte
   est de nouveau là aussitôt, et il faut acquitter de nouveau une fois
   **Mode** revenu à « REPOS ». **Exception : le variateur est en
   défaut** (pastille « fault » dans la carte **Variateur**). **Mode** peut
   alors rester sur « ARRET » tant que le variateur reste en défaut.
   N'attendez pas « REPOS » : suivez la
   [section 9.5](#95-le-variateur-passe-en-défaut), qui dit quand réarmer et
   quand acquitter.
2. Cliquez **Securite**. Dans la carte **Verdict en cours**, lisez les lignes
   « regle » et « detail » : elles disent pourquoi la machine s'est arrêtée.
   La ligne « detail » est en anglais pour la plupart des règles. Pour les
   règles de la caméra
   ([section 9.8](#98-quelquun-sapproche-du-bras)), elle est en français.
3. Trouvez la cause et supprimez-la. Si vous ne la comprenez pas, n'acquittez
   pas : appelez le support.
4. Si un arrêt d'urgence câblé a été utilisé, suivez d'abord
   [Après un arrêt d'urgence câblé](#après-un-arrêt-durgence-câblé).
5. Tapez votre nom dans le champ « votre nom ».
6. Si l'alerte est un arrêt d'urgence (règle « operator_estop »), la console
   exige la case **le coup de poing a ete deverrouille (tire)**. C'est votre
   déclaration : le logiciel ne voit pas l'arrêt câblé. Ne la cochez que si
   c'est vrai. Sans cette case, l'acquittement est refusé.
7. Cliquez **Acquitter**.
8. Vérifiez : une note qui commence par « acquitte par » s'affiche, **Etat**
   affiche « idle » et **Securite** « none ». La note seule ne suffit pas :
   elle s'affiche aussi quand l'alerte revient aussitôt. C'est **Securite**
   « none » qui dit que l'alerte est levée. Après un arrêt d'urgence, le
   bandeau rouge « ARRET D'URGENCE VERROUILLE » disparaît.

![Page Securite avec un arrêt d'urgence verrouillé](../guides/img/console-13-securite-verdict-verrouille.png)

*Page Securite avec un arrêt d'urgence verrouillé, avant l'acquittement : le
bandeau rouge en haut, la carte Verdict en cours à gauche. Capture prise en
simulation.*

> **Acquitter lève le refus de démarrer.** Une fois l'alerte acquittée, une
> séance lancée depuis le site peut partir sans aucune action à la console
> ([section 7](#7-séance-lancée-depuis-le-site)).

Si la même alerte revient juste après un acquittement donné alors que **Mode**
affichait « REPOS », sa cause est toujours là. N'insistez pas : appelez le
support. Deux cas à part : l'alerte « session_standstill » acquittée avant
« REPOS », qui revient parce que la séance n'est pas finie (étape 1), et
l'alerte « session_overrun », décrite à la
[section 9.2](#92-la-console-agit-seule).

### 9.2 La console agit seule

La console surveille la machine en permanence. Quand une règle se déclenche,
la pastille **Securite** change.

| **Securite** affiche | Ce que fait la console | Ce que vous faites |
|---|---|---|
| « freeze » | Elle garde la vitesse commandée telle qu'elle est : elle ne la monte pas et ne la baisse pas, même après un STOP. À elle seule, cette action ne termine pas la séance. | Cherchez la cause (page **Securite**, ligne « regle »). Tant que « freeze » est affiché, STOP ne ralentit pas le bras : des deux boutons, seul **E-STOP** le ralentit. |
| « reduce » | Elle baisse la vitesse commandée. Si elle la ramène à zéro, elle termine la séance, même si le bras tourne encore à cet instant : **Securite** passe à « ramp_down », avec la règle « session_standstill ». | Cherchez la cause. Pour terminer la séance vous-même : **STOP**, puis surveillez la vitesse mesurée. |
| « ramp_down » | Elle termine la séance : elle ramène progressivement la vitesse commandée à zéro. L'arrêt est verrouillé. | Surveillez la vitesse mesurée. Attendez la fin complète, puis [section 9.1](#91-un-arrêt-de-sécurité-est-verrouillé). |
| « quick_stop » | Arrêt d'urgence logiciel : elle met tout de suite la vitesse commandée à zéro, et la machine décélère. L'arrêt est verrouillé. | Surveillez la vitesse mesurée. Attendez la fin complète, puis [section 9.1](#91-un-arrêt-de-sécurité-est-verrouillé). |
| « go_silent » | Elle n'envoie plus rien au variateur : ni **STOP** ni **E-STOP** n'agissent plus sur le moteur. Cet état ne s'acquitte pas : il faut redémarrer la console. **Mode** peut rester sur « ARRET ». | Regardez le bras. S'il ne ralentit pas, il ne reste que l'arrêt d'urgence câblé de votre machine. Appelez le support. |

> **Avec « freeze » et « reduce », un bras qui tourne encore peut réaccélérer
> seul.** Quand ces actions ne sont pas verrouillées et que leur cause
> disparaît (une fréquence cardiaque qui revient, la page de la console
> rouverte), la console reprend la séance d'elle-même, sans aucun clic, si
> aucune fin de séance n'a été enregistrée entre-temps.
>
> **Un bras que la console a ramené à l'arrêt ne repart pas.** Une fois que le
> bras a tourné, dès que la vitesse commandée revient à zéro en cours de
> séance sans que vous l'ayez demandé, la console termine la séance et
> verrouille l'arrêt (règle « session_standstill »). Cela vaut pour une
> alerte, pour le réglage de la vitesse sur la fréquence cardiaque, et aussi
> capsule vide. La cause peut disparaître ensuite : rien ne repart sans
> acquittement et nouveau démarrage.
>
> Cette règle regarde la vitesse commandée, pas la vitesse mesurée. Quand
> **Mode** passe à « ARRET » et que l'alerte apparaît, le bras peut donc
> encore tourner : c'est **Rotation** qui dit s'il est arrêté. Le délai entre
> les deux n'a pas été mesuré sur une machine réelle.
>
> Deux cas restent en dehors de cette règle :
>
> - le premier mouvement d'une séance programmée. Un « freeze » ou un
>   « reduce » qui survient pendant « baseline », avant que le bras ait bougé,
>   ne termine pas à lui seul la séance : s'il disparaît, le programme continue
>   et le bras peut encore faire son premier mouvement de lui-même ;
> - en séance manuelle, un arrêt que vous avez demandé vous-même par une cible
>   de 0 ([section 5](#5-séance-manuelle-capsule-vide)).
>
> Tant que **Mode** affiche « SEANCE » ou « MANUEL », personne ne s'approche du
> bras ni de la capsule, même si **Rotation** affiche « a l'arret ». Pour la
> suite, lisez les règles 5 et 6 et l'avertissement *Avant de s'approcher du
> bras ou de la capsule* ([section 2](#2-les-huit-règles-à-retenir)).

Les règles que vous verrez le plus souvent sur la ligne « regle » :

| Règle affichée | En clair | Ce que fait la console |
|---|---|---|
| « operator_estop » | Quelqu'un a cliqué E-STOP. | Arrêt d'urgence logiciel. |
| « session_standstill » | La console a ramené elle-même la vitesse commandée à zéro en cours de séance. | Fin de séance verrouillée. Le bras peut encore tourner quand l'alerte apparaît : lisez **Rotation**. Il ne repart pas. |
| « attendant_absent » | Plus aucune page de la console n'est ouverte. | « freeze » après 60 secondes, fin de séance après 120 secondes. Si une page est rouverte avant, la séance reprend seule. |
| « hr_stale » | Plus de fréquence cardiaque fiable (séance avec passager). | « freeze » après 10 secondes, « reduce » après 30 secondes. Fin de séance dès que la vitesse commandée est revenue à zéro, et au plus tard après 60 secondes. Si la fréquence revient avant cette fin de séance, donc tant que la console n'a pas ramené la vitesse commandée à zéro, la séance reprend seule. |
| « hr_hard_max » | La fréquence cardiaque reste au-dessus du seuil « max absolu ». | Fin de séance en douceur. |
| « hr_critical » | La fréquence cardiaque atteint le seuil « critique ». | Arrêt d'urgence logiciel. |
| « hr_drop » | La fréquence cardiaque chute brutalement. | Fin de séance en douceur. |
| « hr_rate » | La fréquence cardiaque monte trop vite. | « reduce ». Quand la montée cesse, la séance reprend seule, sauf si la vitesse commandée est revenue à zéro entre-temps : la séance est alors terminée. |
| « loop_stall » | La console a pris du retard dans son propre fonctionnement. | « freeze » verrouillé, qui ne disparaît pas seul, ou « go_silent ». |
| « drive_fault » | Le variateur est en défaut. | Fin de séance. Voir la [section 9.5](#95-le-variateur-passe-en-défaut). |
| « comms_lost » | La console ne parvient plus à parler au variateur. | « go_silent ». |
| « session_overrun » | La durée prévue de la dernière séance est dépassée de 30 secondes. | Fin de séance verrouillée. Cette alerte apparaît aussi après une séance déjà finie : voir ci-dessous. |

Les délais de ce tableau sont ceux du logiciel à la date de ce brouillon. Ce
ne sont pas des recommandations médicales. Pour toute autre règle : arrêtez la
séance et appelez le support.

**L'alerte « session_overrun » après une séance.** À la date de ce brouillon,
la console continue de compter le temps depuis le départ de la dernière
séance, même quand cette séance est finie. Quand ce temps dépasse de
30 secondes la durée prévue (celle du programme, ou 60 minutes pour une séance
manuelle), elle verrouille l'alerte « session_overrun » : **Securite** affiche
« ramp_down ». Ce que vous verrez :

- après un programme mené à son terme, l'alerte apparaît environ 30 secondes
  après le retour à « REPOS ». **Mode** peut alors repasser à « ARRET »
  pendant la durée d'une phase « recovery ». La console ne commande aucun
  mouvement pendant ce temps ;
- après une séance arrêtée plus tôt, l'alerte apparaît plus tard, console au
  repos ;
- si une nouvelle séance démarre avant, le compte repart de ce nouveau départ.

Cette alerte ne se lève pas comme les autres. Dans les essais sur modèle, elle
revient aussitôt après l'acquittement, et tout démarrage reste refusé, à la
console comme depuis le site. D'après ces essais et la lecture du logiciel, la
console n'offre aucun moyen de l'effacer sans être redémarrée.

> **À compléter par Anheart avant la semaine pilote :** la conduite à tenir
> quand l'alerte « session_overrun » apparaît après une séance, et la façon
> d'enchaîner deux séances tant que la console se comporte ainsi.

> **À compléter par Anheart avant la semaine pilote :** la conduite à tenir
> auprès du passager quand la console arrête la séance pour une raison
> cardiaque (règles qui commencent par « hr_ »).

### 9.3 Bandeau rouge NO LIVE DATA

![Liaison coupée](../guides/img/console-15-liaison-coupee.png)

*Liaison coupée : bandeau rouge en haut, pastilles et grands nombres barrés.
Capture prise en simulation.*

L'écran n'a rien reçu depuis 2 secondes. Les nombres barrés sont les dernières
valeurs reçues, pas les valeurs actuelles. Le bandeau le dit lui-même : « la
machine tourne peut-etre encore ».

1. Regardez le bras, pas l'écran.
2. S'il tourne et que vous devez l'arrêter, cliquez **E-STOP** : la demande
   peut passer même quand l'affichage est figé. Puis regardez le bras. S'il ne
   ralentit pas dans les secondes qui suivent le clic, n'attendez aucun
   message : il ne reste que l'arrêt d'urgence câblé de votre machine. Une
   fenêtre qui se termine par « UTILISEZ L'ARRET CABLE » peut annoncer que la
   demande a échoué, mais elle peut aussi ne pas s'ouvrir. Si un second
   bandeau rouge, « ARRET D'URGENCE VERROUILLE », apparaît, la console a
   verrouillé l'arrêt : regardez quand même le bras.
3. La page essaie de se reconnecter seule. Si la liaison revient, le bandeau
   disparaît. Lisez alors **Rotation**, **Mode** et **Securite** avant toute
   autre action.
4. Si vous avez utilisé l'arrêt d'urgence câblé pendant que l'écran était
   figé, suivez [Après un arrêt d'urgence câblé](#après-un-arrêt-durgence-câblé)
   dès que l'écran revient.
5. Si le bandeau reste, appelez le support.

### 9.4 La fréquence cardiaque disparaît

Vous le voyez à ces signes :

- dans la carte **Frequence cardiaque (regulation)** du **Tableau de bord**,
  ou **Frequence cardiaque** de la page **Seance** : un tiret à la place du
  nombre, la pastille « perime », et la ligne « age » barrée ;
- la pastille « deconnecte » dans la carte **BITalino** (le capteur
  cardiaque), page **Tableau de bord** ;
- dans le menu, les points placés devant les capteurs deviennent creux et
  gris.

![Capteur cardiaque déconnecté](../guides/img/console-17-bitalino-deconnecte.png)

*Capteur cardiaque déconnecté pendant une séance manuelle, capsule vide : un
tiret, la pastille « perime » et la ligne « age » barrée dans la carte de la
fréquence cardiaque, la pastille « deconnecte » dans la carte BITalino.
Capture prise en simulation.*

Ce que fait la console :

- **Séance avec passager.** Sans fréquence cardiaque fiable, la console garde
  la vitesse (« freeze », après 10 secondes), puis la baisse (« reduce »,
  après 30 secondes). Elle termine la séance dès que la vitesse commandée est
  revenue à zéro, et au plus tard après 60 secondes : **Securite** affiche
  « ramp_down », et l'arrêt est verrouillé. Le bras peut encore tourner à cet
  instant. Ce sont les règles « hr_stale » et « session_standstill » de la
  [section 9.2](#92-la-console-agit-seule).
- **Séance manuelle, capsule vide.** La console ne s'arrête pas pour cela.

Quatre choses à savoir avant d'agir :

- **Pendant « freeze », STOP ne ralentit pas le bras.** Il enregistre la fin
  de séance, mais des deux boutons, seul **E-STOP** ralentit le bras tant que
  « freeze » est affiché.
- **Si la fréquence cardiaque revient avant que la console ait ramené la
  vitesse commandée à zéro, la séance reprend seule**, sauf si une fin de
  séance a déjà été enregistrée. Le bras peut réaccélérer. Ne vous approchez
  donc pas de la capsule pour remettre une électrode tant que **Mode** affiche
  « SEANCE ».
- **Une fois la vitesse commandée ramenée à zéro par la console, la séance
  est terminée**, même si le bras tourne encore à cet instant : lisez
  **Rotation**. Le bras ne repart pas quand la fréquence cardiaque revient.
  **Mode** affiche « ARRET » jusqu'à la fin de la phase « recovery », puis
  « REPOS ». Il faudra acquitter, puis démarrer une nouvelle séance. Un
  acquittement donné avant « REPOS » ne tient pas : la console répond qu'elle
  l'a pris, mais l'alerte est de nouveau là aussitôt
  ([section 9.1](#91-un-arrêt-de-sécurité-est-verrouillé)).
- **Ce manuel ne dit pas à partir de quand on peut s'approcher de la capsule
  pour remettre une électrode.** Ce que fait le logiciel quand **Mode**
  affiche « ARRET », puis « REPOS », est dans l'avertissement *Avant de
  s'approcher du bras ou de la capsule*
  ([section 2](#2-les-huit-règles-à-retenir)). La règle reste à fixer par
  Anheart.

La console essaie seule de se reconnecter au capteur.

> **À compléter par Anheart avant la semaine pilote :** les vérifications à
> faire sur le capteur cardiaque de votre installation quand il se déconnecte
> (alimentation, électrodes, liaison sans fil), et la conduite à tenir avec un
> passager à bord : laisser la console terminer la séance, ou l'arrêter
> vous-même plus tôt.

### 9.5 Le variateur passe en défaut

Le variateur est l'appareil qui alimente le moteur. En cas de défaut :

- la carte **Variateur** affiche la pastille « fault » et un encadré rouge qui
  commence par « DEFAUT », avec un code et une phrase d'explication ;
- un bouton **Reset defaut variateur** apparaît ;
- la console termine la séance et verrouille l'arrêt ;
- si une séance était en cours, **Mode** peut rester sur « ARRET » tant que
  le variateur reste en défaut. C'est alors le réarmement réussi qui le ramène
  à « REPOS ».

Selon le défaut, le variateur peut ne plus freiner le moteur : le bras
ralentit alors seul, sans contrôle, plus longtemps qu'après un arrêt normal.

![Défaut du variateur](../guides/img/console-14-defaut-variateur.png)

*Défaut du variateur : pastille « fault », encadré rouge et bouton de
réarmement. Sur cette capture, le défaut a été provoqué console au repos :
Mode affiche « REPOS ». Capture prise en simulation.*

1. Attendez que **Rotation** affiche « a l'arret ». Puis regardez **Mode** :
   - s'il affiche « REPOS », il n'y a pas de phase à attendre : passez à
     l'étape 2. Au repos, l'écran n'affiche pas de phase : la ligne d'état en
     bas de l'écran s'arrête au mode, et la carte **Phase** de la page
     **Seance** affiche un tiret ;
   - s'il affiche « ARRET », attendez que la phase soit « done ». Elle se lit
     à la fin de la ligne d'état en bas de l'écran, à gauche du bouton
     **STOP**, ou sur la carte **Phase** de la page **Seance**. Avant « done »,
     le réarmement est refusé. Après une séance programmée, « done » ne vient
     qu'après « recovery ». N'attendez pas « REPOS » : **Mode** peut rester
     sur « ARRET » jusqu'au réarmement.
2. Lisez la phrase de l'encadré rouge et notez le code.
3. Appelez le support, sauf si la phrase indique une cause que vous êtes
   habilité à corriger.
4. Pour réarmer : vérifiez que votre nom est saisi, puis cliquez **Reset
   defaut variateur**. La pastille du variateur doit quitter « fault », et
   **Mode** doit afficher « REPOS ».
5. Ensuite, acquittez l'alerte dans la page **Securite**, s'il y en a une
   ([section 9.1](#91-un-arrêt-de-sécurité-est-verrouillé)). Pour le seul
   défaut du variateur, la case du coup de poing n'est pas nécessaire. Si vous
   acquittez avant le réarmement, l'alerte « drive_fault » revient aussitôt.
   Après un défaut survenu avant toute séance, il peut ne rester aucune
   alerte : l'acquittement est alors refusé, par un message en anglais.

Si la console refuse le réarmement, une ligne « refused » contenant « reset
refuse » apparaît dans la liste **Evenements**. La suite de la ligne dit
pourquoi :

| La ligne contient | Ce que cela veut dire | Quoi faire |
|---|---|---|
| « mouvement encore commande » | Une séance est en cours, la phase n'est pas encore « done », ou la vitesse commandée n'est pas revenue à zéro. La ligne donne la phase entre parenthèses. | Attendez que la ligne d'état en bas de l'écran se termine par « done » (étape 1), puis recommencez. N'attendez pas **Mode** « REPOS ». |
| « l'arbre tourne encore » | Le variateur mesure encore une vitesse. | Attendez **Rotation** « a l'arret », puis recommencez. Si le refus revient alors que **Rotation** affiche « a l'arret », appelez le support. |
| « acquitter d'abord le verdict » | Une autre alerte attend, par exemple après un E-STOP. | Acquittez d'abord ([section 9.1](#91-un-arrêt-de-sécurité-est-verrouillé)), même si **Mode** affiche encore « ARRET » : l'autre alerte s'efface, « drive_fault » revient. Réarmez ensuite, puis acquittez de nouveau. |
| « aucun defaut a acquitter », suivi de « variateur non lu » | La console n'a pas pu lire le variateur. Cela ne dit pas que le défaut a disparu. | N'en concluez rien sur le défaut. Appelez le support. |
| « aucun defaut a acquitter », suivi d'autre chose | Le variateur n'est pas en défaut d'après la dernière lecture. | Rien à réarmer. Acquittez l'alerte s'il en reste une. |
| « non rearmable depuis la console » | Ce défaut ne se réarme pas depuis la console. | N'insistez pas. Arrêtez-vous et appelez le support. |
| Autre chose | La console n'a pas envoyé la demande au variateur, ou la demande n'est pas arrivée. | Appelez le support. |

> **À compléter par Anheart avant la semaine pilote :** qui est habilité à
> réarmer un défaut du variateur chez vous, et la conduite à tenir quand la
> console refuse le réarmement.

### 9.6 La page de la console a été fermée

Tant qu'une page de la console est ouverte, elle signale sa présence à la
machine toutes les 5 secondes. Si plus aucune page n'est ouverte pendant une
séance :

- après 60 secondes, la console garde la vitesse (« freeze ») ;
- après 120 secondes, elle termine la séance et verrouille l'arrêt.

Que faire :

1. Rouvrez la console.
2. Retapez votre nom : une nouvelle page peut l'avoir oublié.
3. Lisez **Rotation**, **Mode** et **Securite**.
4. Si vous avez rouvert la console avant les 120 secondes et que **Mode**
   affiche encore « SEANCE » ou « MANUEL », la séance n'est pas terminée :
   « freeze » disparaît et la séance reprend seule. Un bras qui tournait peut
   accélérer de nouveau. En séance programmée, un bras qui n'avait pas encore
   fait son premier mouvement peut encore le faire de lui-même.
5. Si la séance a été terminée, acquittez
   ([section 9.1](#91-un-arrêt-de-sécurité-est-verrouillé)).

Ce signal prouve qu'une page est ouverte, pas qu'une personne regarde. Restez
devant l'écran, page de la console affichée. Ce manuel ne peut pas garantir que
le signal continue si la page passe en arrière-plan ou si l'écran se met en
veille.

Fermer la page de la console n'empêche pas un démarrage lancé depuis le site
([section 7](#7-séance-lancée-depuis-le-site)).

### 9.7 La console a redémarré

Après un redémarrage de la console ou une coupure de courant :

1. Rouvrez la console.
2. Regardez le bras, puis la pastille **Rotation**.
3. Refaites l'attestation ([section 4](#4-avant-toute-séance-attester-larrêt-durgence)) :
   la console l'a oubliée.
4. Ouvrez la page **Securite**. S'il reste une alerte verrouillée, lisez-la,
   puis acquittez ([section 9.1](#91-un-arrêt-de-sécurité-est-verrouillé)).
5. Après un redémarrage de la console, la séance interrompue ne reprend pas.
   Redémarrez une séance seulement si tout est normal. Une fois l'attestation
   refaite, une séance lancée depuis le site peut de nouveau démarrer sans
   action à la console ([section 7](#7-séance-lancée-depuis-le-site)).

Cas particuliers :

- Si un démarrage est refusé avec un message contenant « variateur deja en
  marche », la console a trouvé le moteur déjà commandé. Elle l'arrête et
  attend un acquittement.
- Après une coupure de courant, le variateur peut afficher un défaut : voir la
  [section 9.5](#95-le-variateur-passe-en-défaut).

### 9.8 Quelqu'un s'approche du bras

Sur la page **Securite**, la carte **Camera / presence** dit si une caméra
surveille la machine. Si elle affiche « non branchee », rien ne surveille ni la
zone du bras ni la capsule : c'est à vous de le faire. À la date de ce
brouillon, le logiciel ne prend en charge aucune caméra réelle.

Si quelqu'un s'approche du bras pendant la rotation, arrêtez la machine sans
attendre : **E-STOP**, et l'arrêt d'urgence câblé de votre machine
([section 8](#8-arrêter-la-machine)).

Un bras à l'arrêt n'est pas une zone sûre : lisez les règles 5 et 6 et
l'avertissement *Avant de s'approcher du bras ou de la capsule*
([section 2](#2-les-huit-règles-à-retenir)).

> **À compléter par Anheart avant la semaine pilote :** le périmètre de
> sécurité autour du bras, et les règles pour les personnes présentes dans la
> salle pendant une séance.

## 10. Un démarrage est refusé

Un refus peut s'afficher à trois endroits :

- sous le bouton que vous venez de cliquer, en rouge ;
- dans une fenêtre d'alerte ;
- dans la liste **Evenements** de la page **Seance**, sur une ligne
  « refused ». C'est parfois le seul endroit où le refus apparaît.

Plusieurs messages sont encore en anglais. Ce tableau part de ce que vous
voyez.

| Ce que vous voyez | Cause | Quoi faire |
|---|---|---|
| « cochez la declaration BANC (personne a bord : NON) avant de demarrer » | La case de la séance manuelle n'est pas cochée. | Vérifiez que la capsule est vide, puis cochez. |
| Un message en anglais, alors que le champ de nom est vide | Aucun nom d'opérateur. | Tapez votre nom. |
| Le bouton **Demarrer la seance** grisé, sans aucun message | L'attestation n'est pas faite, ou **Etat** n'affiche pas « idle ». Si la page affiche aussi la note sur les séances programmées désactivées, voir plus bas. | [Section 4](#4-avant-toute-séance-attester-larrêt-durgence), ou attendez **Etat** « idle ». |
| En séance manuelle, un message en anglais sous le bouton juste après un redémarrage de la console. Ou, dans **Evenements**, « demarrage refuse : cablage de l'arret d'urgence non atteste » | L'attestation n'est pas faite. | [Section 4](#4-avant-toute-séance-attester-larrêt-durgence). |
| Un message contenant « demarrage refuse : verdict », ou un message en anglais alors que **Securite** n'affiche pas « none » | Un arrêt de sécurité attend un acquittement. | [Section 9.1](#91-un-arrêt-de-sécurité-est-verrouillé). |
| Un message en anglais alors que **Etat** n'affiche pas « idle » | Une séance tourne déjà, ou un arrêt est en cours. | Attendez **Etat** « idle ». |
| « seances programmees desactivees sur cette console (jalon M5) : utiliser le mode MANUEL » | Les séances programmées ne sont pas autorisées sur cette machine. | Appelez le support. |
| Un message contenant « personne a bord refusee » | La machine n'est pas autorisée à tourner avec une personne à bord. | Ne cherchez pas à contourner. Appelez le support. |
| « demarrage refuse : age du passager requis pour une seance programmee » | L'âge n'est pas saisi. | Saisissez l'âge du passager. |
| Un message contenant « demarrage refuse : passager de » | Le passager est plus jeune que l'âge minimum réglé sur la machine. | Pas de séance. |
| Un message contenant « demarrage refuse : programme inadapte a ce passager » | Le programme ne convient pas à ce passager, d'après les contrôles du logiciel. | Pas de séance avec ce programme. Voyez avec le gestionnaire. |
| Un message contenant « demarrage refuse : variateur en defaut » | Le variateur est en défaut. | [Section 9.5](#95-le-variateur-passe-en-défaut). |
| En séance manuelle, dans **Evenements**, une ligne contenant « consigne refusee » et « tient le bras a l'arret » | Ce n'est pas un démarrage qui est refusé, mais une cible : quelque chose retient le bras à l'arrêt. La ligne nomme la règle, et dit s'il faut attendre ou acquitter. | Si la ligne dit d'attendre : attendez que **Securite** revienne à « none ». Si elle dit d'acquitter : l'alerte est verrouillée et ne disparaît pas seule. Supprimez sa cause, puis acquittez par les étapes 2 à 7 de la [section 9.1](#91-un-arrêt-de-sécurité-est-verrouillé), et vérifiez que **Securite** revient à « none ». N'attendez pas « REPOS » comme le demande l'étape 1 : la séance manuelle n'est pas terminée, et **Mode** reste sur « MANUEL ». Dans les essais sur modèle, cet acquittement a tenu et n'a rien fait partir : le bras est resté à l'arrêt jusqu'à la cible suivante. Autre voie : terminer d'abord la séance par **STOP**, suivre la section 9.1 en entier, puis démarrer une nouvelle séance. Dans les deux cas, c'est à vous de redonner la cible ensuite ([section 5](#5-séance-manuelle-capsule-vide)). |
| En séance manuelle, dans **Evenements**, une ligne contenant « remise a 0 » | Une cible déjà appliquée a été retirée. Soit pour la même raison : la ligne nomme alors la règle. Soit parce que le variateur n'a pas confirmé le premier pas de vitesse : la ligne contient alors « le variateur n'a pas confirme la consigne ». Dans ce second cas, il peut n'y avoir aucune alerte, et **Securite** peut afficher « none ». | Si la ligne nomme une règle : comme pour la ligne précédente. Si elle parle du variateur : la console ne redemande pas cette vitesse, mais le variateur peut avoir reçu ce premier pas. Lisez **Rotation** et **Vitesse mesuree**. La ligne demande de vérifier la liaison avec le variateur : si elle revient à la cible suivante, n'insistez pas et appelez le support. |
| Tout autre message | | Notez le texte exact. N'insistez pas. Appelez le support. |

## 11. Quand arrêter et appeler le support

Ce manuel ne peut pas tout prévoir. Arrêtez la séance comme l'indique la
[section 8](#8-arrêter-la-machine), ne redémarrez pas, et appelez le support
si :

- le passager se plaint d'un malaise, ou vous semble aller mal ;
- le bras ne ralentit pas après un E-STOP ;
- « freeze » reste affiché et vous ne savez pas pourquoi ;
- **Mode** reste sur « ARRET » et vous ne savez pas pourquoi ;
- **Rotation** affiche « VITESSE INCONNUE » ;
- **Securite** affiche « go_silent », ou une règle que ce manuel ne décrit
  pas ;
- la même alerte revient juste après un acquittement donné alors que **Mode**
  affichait « REPOS » ;
- la console refuse le réarmement d'un défaut du variateur ;
- l'écran affiche un message que vous ne comprenez pas, ou ne ressemble pas à
  ce que décrit ce manuel ;
- vous n'êtes pas sûr de pouvoir attester le câblage ;
- la machine a un comportement que vous jugez anormal.

En cas d'urgence médicale, appelez d'abord les services d'urgence.

> **À compléter par Anheart avant la semaine pilote :** le numéro et les
> horaires du support, ce qu'il faut lui transmettre, et le numéro à appeler
> pour un incident de sécurité.

> **À compléter par Anheart avant la semaine pilote :** à partir de quand, et
> avec quelles précautions, on peut s'approcher du bras ou de la capsule quand
> **Mode** affiche « ARRET » ou « REPOS » : pour faire sortir un passager,
> pour remettre une électrode, ou pour lui porter secours après un arrêt
> d'urgence ou un malaise. Et la conduite à tenir auprès de lui. Ce que fait
> le logiciel dans ces deux états est décrit à la
> [section 2](#2-les-huit-règles-à-retenir). Cela ne suffit pas à fixer la
> règle : ces comportements n'ont été vérifiés qu'en simulation, et la
> documentation d'Anheart indique que la machine d'essai n'a aucune sécurité
> matérielle indépendante du logiciel et du variateur.

Ce qu'il est utile de noter pour le support :

- l'heure, et ce que vous étiez en train de faire ;
- la valeur des six pastilles ;
- les lignes « regle » et « detail » de la page **Securite** ;
- les dernières lignes de la liste **Evenements** ;
- le texte de l'encadré rouge du variateur, s'il y en a un.

## 12. Petit lexique

| Mot | Sens |
|---|---|
| Acquitter | Confirmer, par son nom, qu'on a pris connaissance d'un arrêt de sécurité et traité sa cause. |
| Arrêt d'urgence câblé | Un arrêt d'urgence physique sur la machine, indépendant de l'écran. La console l'appelle « coup de poing ». Le logiciel ne le voit pas et ne peut pas vérifier qu'il existe. |
| Attestation | Déclaration, par son nom, que le câblage de l'arrêt d'urgence a été vérifié. |
| BANC | Séance sans personne à bord. |
| bpm | Battements par minute. |
| E-STOP | L'arrêt d'urgence de l'écran. C'est un arrêt logiciel. |
| Gc, Gr | Deux façons d'exprimer la charge, en g, calculées au point de référence de la machine. La charge est plus forte vers les pieds de la personne à bord. |
| Passager | La personne dans la capsule. Le site web l'appelle « pratiquant ». |
| Pastille | Petite étiquette colorée qui donne un état. |
| STOP | L'arrêt normal, en douceur. Il ne ralentit pas le bras tant que **Securite** affiche « freeze ». |
| tr/min | Tours par minute. Dans ce manuel : ceux du bras. |
| Variateur | L'appareil qui alimente et pilote le moteur. |
| Verdict | Le nom que la console donne à une décision de sécurité. |

# La console locale du Raspberry Pi

La console locale est la page web servie par le Pi lui-même (`python -m src.local_panel`).
C'est **l'admin de la machine** : toute séance, manuelle ou automatique, passe par elle.
Le tableau de bord distant ([tableau-de-bord.md](tableau-de-bord.md)) ne fait que la refléter
et lui transmettre des demandes.

Ce document décrit chaque page, chaque bouton, chaque indicateur, les messages d'erreur,
et la liste complète des routes HTTP. Les termes techniques (E-STOP, STO, LFT, HSP, verdict,
latch…) sont définis dans le [glossaire](glossaire.md). Le fonctionnement interne
(superviseur, règles, défauts variateur) est dans [raspberry-pi.md](raspberry-pi.md).
Les limites de sécurité sont dans [securite.md](securite.md).

> **Ce qui a été vérifié.** Le texte ci-dessous est tiré du code
> (`raspberry-pi/src/web/`, `src/local_panel.py`, `src/control_surface.py`). Les routes et
> les messages cités ont été rejoués sur la console lancée **en simulation complète**
> (variateur simulé, BITalino simulé, caméra simulée, sans tableau de bord).
> La page n'a pas été vérifiée sur la machine réelle avec une personne à bord :
> la séance « personne à bord » reste refusée par configuration (jalon M6).
> Les passages sur les avertissements non verrouillés, la règle `session_standstill`,
> STOP sous un `freeze` et la cible manuelle refusée ou remise à 0 (sections 4, 5, 6,
> 11 et 13, ajoutés le 6 octobre 2026, ceux sur STOP sous un `freeze` réécrits le
> 7 octobre 2026) viennent du code, des tests automatiques et de rejeux par l'API sur
> la console en simulation ; ils n'ont pas été rejoués dans un navigateur, à
> l'exception de ce que dit le dernier alinéa de cet encadré.
> Les passages sur la règle `session_overrun` (sections 4, 5 et 11, ajoutés le
> 6 octobre 2026 avec ANH-181) viennent du code, des tests automatiques sur la console
> en simulation et de mesures sur le banc d'essai logiciel ; ils n'ont pas été rejoués
> dans un navigateur.
> Ce que la page affiche de plus depuis le 7 octobre 2026 (la note de la carte MANUEL
> pour une cible prise, refusée ou remise à 0, l'encadré de la fréquence cardiaque, les
> bandeaux `ARRET D'URGENCE NON CONFIRME` et `REPRISE AUTOMATIQUE POSSIBLE`, les
> pastilles barrées sous `NO LIVE DATA`, `perime` en orange) a été rejoué dans un
> navigateur sans interface, sur la console en simulation, avec une personne à bord
> déclarée par l'API pour ce qui dépend de la fréquence cardiaque.

---

## Sommaire

1. [Lancer la console](#1-lancer-la-console)
2. [Structure de la page](#2-structure-de-la-page)
3. [Règles d'affichage à connaître](#3-règles-daffichage-à-connaître)
4. [Barre latérale](#4-barre-latérale)
5. [Pied de page : STOP et E-STOP](#5-pied-de-page--stop-et-e-stop)
6. [Page « Tableau de bord »](#6-page--tableau-de-bord-)
7. [Page « Capteurs »](#7-page--capteurs-)
8. [Page d'un capteur](#8-page-dun-capteur)
9. [Page « Seance »](#9-page--seance-)
10. [Page « Configuration »](#10-page--configuration-)
11. [Page « Securite »](#11-page--securite-)
12. [Déroulé type d'une séance manuelle de banc](#12-déroulé-type-dune-séance-manuelle-de-banc)
13. [Messages d'erreur typiques](#13-messages-derreur-typiques)
14. [Référence des routes HTTP et WebSocket](#14-référence-des-routes-http-et-websocket)
15. [Écarts connus entre le code, la page et les README](#15-écarts-connus-entre-le-code-la-page-et-les-readme)

---

## 1. Lancer la console

Depuis `raspberry-pi/`, en natif (sur macOS, Docker ne voit ni le câble FTDI ni le
Bluetooth). Simulation complète, sans aucun matériel ni tableau de bord :

```bash
cd raspberry-pi
MOTOR_BACKEND=sim ECG_SOURCE=sim ARM_RADIUS_M=1.5 UI_PORT=8090 MACHINE_API_KEY= \
  .venv/bin/python -m src.local_panel
```

Puis ouvrir `http://127.0.0.1:8090/`. `Ctrl-C` arrête proprement.

Options utiles en simulation :

```bash
# tous les capteurs BITalino, et une caméra simulée (capsule vide)
MOTOR_BACKEND=sim ECG_SOURCE=sim ARM_RADIUS_M=1.5 UI_PORT=8090 MACHINE_API_KEY= \
  SENSORS=ECG,EDA,SpO2,RESP,EMG,LUX PRESENCE_SOURCE=sim_empty \
  .venv/bin/python -m src.local_panel
```

Au démarrage, le terminal affiche une ligne de résumé (vérifiée) :

```
WARNING __main__: Console du banc sur http://127.0.0.1:8090/ - MANUEL BANC (plafond 300 tr/min moteur; paliers 148/158 bpm) - variateur: simulateur - ECG (sim): simulateur - rayon 1.5 m, i = 49.79 - tableau de bord: aucun
INFO src.web.app: operator interface serving 25 routes on 127.0.0.1:8090 (loopback=True, token=none)
```

Points à savoir :

- Les variables du processus **priment** sur `raspberry-pi/.env`. Si un `.env` existe et
  contient `MACHINE_API_KEY`, la console se relie au tableau de bord Convex. Forcer
  `MACHINE_API_KEY=` (vide) pour rester strictement local.
- `MOTOR_BACKEND`, `ECG_SOURCE` et `ARM_RADIUS_M` sont obligatoires. Une erreur de
  configuration est signalée d'un coup et le processus sort avec le code 2, par exemple :
  `configuration: ARM_RADIUS_M: obligatoire, sans valeur par defaut : rayon mesure de l'axe a l'occupant, en m`.
- Sans `UI_PORT`, le code écoute sur **8080** (`DEFAULT_PORT`, `src/web/deps.py`) ; `.env.example`
  propose 8090. On passe donc `UI_PORT=8090` explicitement. **Jamais 8123** : ce port est pris par
  `scripts/bench_console.py`, et la configuration le refuse.
  Ne jamais faire tourner les deux programmes sur le même câble variateur.
- `PYTHONPATH` n'est pas nécessaire ici (on lance depuis `raspberry-pi/`). Il l'est pour la
  simulation (`simulation/`), voir [demarrage-rapide.md](demarrage-rapide.md).
- À l'arrêt, le terminal dit comment la machine a été laissée, par exemple
  `console stopped: runtime never started: drive link released without a write`
  (rien n'a jamais été commandé) ou
  `console stopped: emergency zero ACKNOWLEDGED; the drive link closed cleanly`.
- Le premier enregistrement d'un profil crée `raspberry-pi/data/profiles.local.json`.
  Tant qu'on n'enregistre rien, la console charge les profils livrés
  (`standard_30_min`, `standard_45_min`) sans écrire de fichier.

Toutes les clés `.env` sont décrites dans [raspberry-pi.md](raspberry-pi.md).

---

## 2. Structure de la page

Une seule page HTML, sans framework, sans CDN, sans étape de build : le Pi la sert
hors ligne (`src/web/static/index.html`, `app.js`, `app.css`). Elle comporte :

| Zone | Contenu |
|---|---|
| Bandeaux en haut | Trois rouges : `NO LIVE DATA`, visible dès que les données ne sont plus fraîches ; `ARRET D'URGENCE NON CONFIRME`, quand une demande E-STOP de cet écran reste sans réponse ; `ARRET D'URGENCE VERROUILLE`, tant que la page sait un arrêt d'urgence verrouillé (section 5). Un orange : `REPRISE AUTOMATIQUE POSSIBLE`, tant qu'un avertissement non verrouillé tient ou baisse une vitesse qui peut remonter seule (section 11). Ils s'empilent ; la barre latérale et la barre mobile commencent sous eux |
| Barre latérale (à gauche) | la marque, six pastilles d'état de la machine, la navigation |
| Zone centrale | une seule page à la fois : Tableau de bord, Capteurs, un capteur, Seance, Configuration, Securite |
| Pied de page fixe | l'état de la liaison, **STOP** et **E-STOP**, présents sur toutes les pages |

Sur un écran étroit, la barre latérale se replie derrière un bouton `☰ Menu`. Une barre
mobile affiche alors le mode et l'état de rotation.

La page reçoit ses données par un WebSocket (`/ws/telemetry`, environ 5 images par
seconde) et par des interrogations périodiques :

| Source | Période | Sert à |
|---|---|---|
| `/ws/telemetry` | continu | instantanés, événements, tracé ECG |
| `/api/panel` | 1 s | liaisons variateur et BITalino, mode console, retenue de la fréquence cardiaque en séance manuelle |
| `/api/sensors` | 1 s | pages Capteurs |
| `/api/camera` | 1 s | carte caméra de la page Securite |
| `/api/status` | 5 s | état, verdicts, attestation, système |
| `/api/presence` (POST) | 5 s | signal « l'accompagnant est là » |

Si le WebSocket tombe, la page réessaie toutes les 1,5 s.

Chaque source est jugée sur ses propres réponses. Ce qu'elle alimente est barré et
grisé quand elle se tait (section 3) : après 2 s sans image pour le WebSocket, 3,5 s
sans réponse pour `/api/panel`, 12 s pour `/api/status`.

---

## 3. Règles d'affichage à connaître

Ces règles ne sont pas cosmétiques. Elles sont écrites en tête de `app.js`.

1. **Rien n'est affiché comme « en direct » s'il ne l'est pas.** Si aucune image n'arrive
   pendant 2 s, la page passe en « données figées » :
   - le bandeau rouge `NO LIVE DATA` apparaît, avec
     `la liaison est ouverte mais aucune donnee depuis N s - la machine tourne peut-etre encore`
     ou `la liaison avec la machine est coupee - la machine tourne peut-etre encore` ;
   - les grands nombres (fréquence cardiaque, vitesse mesurée, consigne) sont barrés et grisés ;
   - les pastilles que les images alimentent le sont aussi, et perdent leur couleur :
     celles de la barre latérale (Mode, Rotation, Securite) et celles des cartes
     (qualité de la fréquence cardiaque, rotation, état du variateur, consigne
     confirmée, action de sécurité, phase, occupation du mode manuel, ECG). Une
     pastille `good` ou `a l'arret` ne reste pas verte à côté d'un nombre barré ;
   - la pastille **Liaison** passe à `donnees figees` ou `hors ligne`.

   La même règle vaut pour les deux autres sources. Sans réponse de `/api/panel`
   depuis 3,5 s, les pastilles **Console** et BITalino (`acquisition`) sont barrées ;
   sans réponse de `/api/status` depuis 12 s, **Etat** et la pastille de
   l'attestation. Le WebSocket peut tomber alors que ces deux sources répondent
   encore : leurs pastilles restent alors lisibles, à juste titre.
2. **« Est-ce arrêté ? » se lit sur la vitesse MESURÉE, jamais sur la consigne.** Une
   consigne à zéro sur une masse qui ralentit n'est pas un zéro mesuré.
3. **Les vitesses sont toujours données ensemble** : tr/min moteur, tr/min de sortie
   (bras), fréquence variateur (Hz), Gc (g centripète) et Gr (g résultant ressenti).
   Le rapport de réduction est 49,79 : un seul de ces nombres masquerait une erreur d'un
   facteur 50. Voir [glossaire](glossaire.md) pour Gc et Gr.
4. **Une fréquence cardiaque périmée est barrée.** Elle est affichée avec son âge, et sa
   pastille de qualité dit `perime` au lieu de la dernière note reçue. Un capteur
   dont la fenêtre n'avance plus depuis 3,5 s est marqué `perime` de la même façon.
   `perime` est en orange, la couleur des données périmées dans cette page : dans le
   menu, le point d'un capteur périmé devient un anneau orange.
5. **L'E-STOP ne demande rien** : ni confirmation, ni nom, ni raison.

L'indicateur de rotation (pastille **Rotation**, et `Mesure` / `Vitesse mesuree`) vaut :

| Libellé | Signification |
|---|---|
| `a l'arret` (vert) | vitesse de sortie mesurée inférieure à 0,05 tr/min, statut variateur frais |
| `EN ROTATION` (orange) | vitesse mesurée non nulle |
| `VITESSE INCONNUE` (rouge) | statut variateur périmé ou vitesse absente : ne jamais le lire comme « arrêté » |

---

## 4. Barre latérale

### Pastilles d'état

| Pastille | Valeurs | Source |
|---|---|---|
| **Mode** | `REPOS`, `MANUEL`, `SEANCE`, `ARRET` | l'instantané (`mode`) |
| **Etat** | `idle`, `starting`, `running`, `stopping`. En rouge tant qu'un arrêt d'urgence est verrouillé, quel qu'en soit l'auteur : après un arrêt posé par la caméra, la pastille dit `idle` en rouge | `/api/status` (`run_state`, `estop_latched`, `supervisor_estop`, `standing`) |
| **Liaison** | `en direct`, `donnees figees`, `hors ligne` | la fraîcheur du WebSocket |
| **Rotation** | `a l'arret`, `EN ROTATION`, `VITESSE INCONNUE` | vitesse mesurée |
| **Securite** | action de sécurité en cours : `none`, `freeze`, `reduce`, `ramp_down`, `quick_stop`, `go_silent` (vert, orange, rouge). Un `freeze` ou un `reduce` non verrouillé se lève seul, et la vitesse d'un bras qui tourne remonte alors sans clic : le bandeau orange `REPRISE AUTOMATIQUE POSSIBLE` le dit tant que c'est le cas ; `ramp_down`, `quick_stop` et `go_silent` terminent la séance (section 11) | l'instantané |
| **Console** | `mouvement actif`, `LECTURE SEULE`, `pas de console` | `/api/panel` |

Les modes :

| Mode | Signification |
|---|---|
| `REPOS` | rien n'est commandé. Le variateur est **lu** (2 Hz), jamais écrit. Une séance finie n'y déclenche aucun verdict, aussi longtemps que la console y reste : le départ suivant ne demande pas de redémarrer la console (section 11, `session_overrun`). |
| `MANUEL` | séance manuelle : l'opérateur fixe une cible, la machine y va aux limites de mouvement |
| `SEANCE` | une séance programmée (AUTO) déroule son programme |
| `ARRET` | une séance se termine. Le mode dure jusqu'à la fin de la séance, c'est-à-dire phase `done` et étage de sortie retiré : en séance manuelle de banc, dès l'arrêt mesuré ; pour un programme, après la phase `recovery` (environ 5 minutes avec le profil standard). **Pas** « arrêté » : lire la vitesse mesurée. Après un STOP ou une règle d'arrêt, la consigne descend encore vers zéro, y compris quand la pastille **Securite** dit `freeze` (section 5). Après la règle `session_standstill` (section 11), la consigne vaut déjà 0 quand `ARRET` s'affiche et la vitesse mesurée suit : dans la fraction de seconde qui suit en simulation, non mesuré sur la vraie machine. C'est la pastille **Rotation** qui dit que le bras est arrêté |

Les états `run_state` (vue « intention » de la surface de commande) :

| État | Signification |
|---|---|
| `idle` | rien de demandé. Seul état où un démarrage est accepté |
| `starting` | un démarrage est accepté, la boucle ne l'a peut-être pas encore pris |
| `running` | la boucle a confirmé une séance, et personne n'a demandé sa fin ni un E-STOP depuis la page. L'état reste `running` pendant tout le mode `ARRET` d'une fin de séance que la console décide elle-même (un verdict d'arrêt, un arrêt posé par la caméra, la règle `session_standstill`) : il ne dit donc pas que la séance avance encore. Lire **Mode** |
| `stopping` | une fin ou un E-STOP est accepté ; un E-STOP garde cet état jusqu'à l'acquittement |

Sous les pastilles, une ligne indique l'adresse d'écoute, par exemple
`127.0.0.1:8090 boucle locale · sans jeton`, ou `… RESEAU · jeton` hors boucle locale.

### Navigation

`Tableau de bord`, `Capteurs` (avec, en dessous, une entrée par capteur acquis : nom,
canal `A1`…`A6` et une pastille de couleur selon la qualité), `Seance`, `Configuration`,
`Securite`. Changer de page ne recharge rien : aucune navigation ne peut figer un nombre.
Le titre de l'onglet suit la page affichée : `Tableau de bord - AnHeart`,
`Securite - AnHeart`, et le type du capteur pour la page d'un capteur (`ECG - AnHeart`).

---

## 5. Pied de page : STOP et E-STOP

Présent sur **toutes** les pages. À gauche, un résumé : `liaison ouverte · manuel · hold`
(mode et phase de l'instantané). Au repos, aucune phase n'est en cours et le résumé
s'arrête au mode : `liaison ouverte · repos`.

### STOP : « rampe controlee »

- Envoie `POST /api/session/stop` avec le premier nom d'opérateur saisi sur la page
  et la raison `operator pressed STOP`.
- Termine la séance **sur la rampe mise en service** (pas plus vite) : une décélération
  plus rapide que la rampe du variateur déclenche une surtension (ObF) et met la
  machine en roue libre, ce qui allonge l'arrêt.
- **STOP agit aussi sous un `freeze`.** Quand la pastille **Securite** dit `freeze`,
  la demande est enregistrée (réponse 202, mode `ARRET`) et la consigne commence à
  descendre au cycle suivant, aux limites de mouvement, comme sans avertissement ; que
  le gel soit verrouillé ou non. Un gel qui apparaît pendant une descente demandée par
  STOP ne la fige pas. Les actions plus sévères (`reduce`, `ramp_down`, `quick_stop`,
  `go_silent`) décident toujours en premier. `ARRET` va maintenant de pair avec une
  consigne qui descend ou qui vaut 0 (sauf sous `go_silent`, où la console n'envoie
  plus rien) ; il ne prouve pas l'arrêt du bras, qui se lit sur la vitesse mesurée.
  Vérifié par les tests automatiques sur la console en simulation : STOP 12 s après
  une perte de l'ECG en séance programmée, consigne plus basse au cycle suivant et à 0
  avant le `reduce` de la règle ; jusqu'au 7 octobre 2026 elle restait inchangée
  environ 17 s. Après la fin, un gel qui était verrouillé reste à acquitter. Détails
  dans [raspberry-pi.md](raspberry-pi.md#7-ce-qui-se-passe-physiquement-à-larrêt).
- En manuel, après STOP la cible repasse à 0. En séance manuelle de banc, une fois
  l'étage de sortie coupé à l'arrêt mesuré, le mode revient à `REPOS` ; pour un
  programme, `REPOS` ne revient qu'après la phase `recovery`. Repartir demande un
  nouveau démarrage.
- **Un STOP donné tard dans un programme se termine par un verdict à acquitter.** Un
  STOP rouvre une récupération complète (5 minutes avec le profil standard), même si le
  programme était déjà dans sa propre phase `recovery`, bras arrêté. Si elle dépasse la
  durée prévue de plus de 30 s, la règle `session_overrun` se verrouille pendant cette
  récupération : avec le profil standard de 30 minutes, c'est le cas de tout STOP donné
  dans les quatre dernières minutes et demie, et le verdict apparaît à 1830 s. Rien ne
  bouge. Acquitter une fois `REPOS` affiché (section 11). Mesuré sur le banc d'essai
  logiciel avec le profil standard, et vérifié par l'API sur la console en simulation
  avec un programme court (`raspberry-pi/tests/test_session_overrun_console.py`) ; pas
  rejoué dans un navigateur. Un arrêt demandé depuis le site passe par ce même STOP. Un
  E-STOP ou un verdict d'arrêt donnés aussi tard rouvrent la même récupération et mènent
  au même verdict (section 11).
- Refus : une boîte d'alerte `STOP refuse : …` (par exemple
  `the machine is already stopping` si un arrêt est déjà en cours, ou
  `there is no session to end (the machine is idle)`).

### E-STOP : « verrouille immediatement »

- Envoie `POST /api/session/estop` **au premier clic**, sans confirmation, sans nom exigé.
- Le verrou est posé **avant** la réponse (code 200), sans attendre la boucle de
  contrôle. À la tick suivante, la boucle met la consigne à zéro (`QUICK_STOP`).
- La page affiche en haut un bandeau rouge qui lui est propre, distinct de `NO LIVE DATA` :
  `ARRET D'URGENCE VERROUILLE` suivi de
  `surveillez la vitesse MESUREE : verrouille ne veut pas dire arrete`.
  Il ne dit pas que la machine est arrêtée : seule la vitesse mesurée le dit.
- Ce bandeau ne dépend pas du clic. Un écran le lève dès qu'une de ses trois sources
  dit qu'un arrêt d'urgence est verrouillé : la réponse à son propre clic, les
  instantanés du WebSocket (verdict `quick_stop` verrouillé), ou `/api/status`
  (`estop_latched`, `supervisor_estop`, ou verdict retenu `quick_stop` verrouillé). Il
  apparaît donc aussi sur un écran qui n'a pas cliqué, après un arrêt posé par la
  caméra, et pour le `quick_stop` verrouillé d'une règle de sécurité
  (`reverse_rotation` par exemple ; la ligne `e-stop verrouille` dit alors `non`).
- Il ne se baisse que sur une donnée plus récente qui montre le verdict levé ; dans le
  doute il reste, sur toutes les pages. Un verdict `go_silent` prend la place d'un arrêt
  d'urgence comme verdict retenu et refuse tout acquittement : un bandeau déjà affiché
  reste alors jusqu'au redémarrage de la console. Si le WebSocket tombe, il reste à
  côté de `NO LIVE DATA`.
- Un écran ouvert ou rechargé alors que `go_silent` est déjà le verdict retenu affiche
  lui aussi le bandeau quand un arrêt d'urgence est verrouillé derrière : `/api/status`
  porte `supervisor_estop`, l'arrêt d'urgence que le superviseur tient verrouillé, quel
  qu'en soit l'auteur (bouton de la page, autre écran, caméra), et qui reste lisible
  quand `go_silent` a pris la place du verdict retenu et du plancher. La ligne
  `e-stop verrouille` dit alors `OUI` et la pastille **Etat** est rouge.
- Ce que l'E-STOP web **n'est pas** : un arrêt de sécurité. Il dépend du navigateur,
  du réseau, du serveur web et du processus. Le STO est ponté sur cette machine : même
  l'arrêt d'urgence le plus rapide est une rampe (voir [securite.md](securite.md)).
  L'arrêt de sécurité est le coup de poing câblé.
- **Une demande restée sans réponse est dite.** Si la console est bloquée ou le réseau
  muet, la requête reste en attente. Deux secondes après le premier clic resté sans
  réponse, un bandeau rouge propre à ce cas s'affiche :
  `ARRET D'URGENCE NON CONFIRME` suivi de
  `aucune reponse de la console depuis N s - UTILISEZ L'ARRET CABLE`. Le compte court
  depuis le premier clic sans réponse, pas depuis le dernier. La requête n'est jamais
  retirée : si elle finit par passer, sa réponse baisse ce bandeau et lève
  `ARRET D'URGENCE VERROUILLE`. Une image ou une réponse de statut, arrivée après le
  clic, qui montre un arrêt verrouillé le baisse aussi ; sous `go_silent` il reste.
  Rejoué dans un navigateur, console suspendue (processus arrêté par un signal, ses
  connexions ouvertes) : bandeau affiché 2,07 s après le clic, puis remplacé par
  `ARRET D'URGENCE VERROUILLE` moins de 0,1 s après la reprise de la console.
- Si la requête échoue : le même bandeau dit tout de suite
  `la demande a echoue : … - UTILISEZ L'ARRET CABLE` et reste affiché ; la boîte
  d'alerte `la demande d'arret d'urgence a echoue : … - UTILISEZ L'ARRET CABLE`
  s'ouvre comme avant.
- Un E-STOP reste verrouillé jusqu'à un **acquittement nommé** (page Securite), avec la
  case « coup de poing déverrouillé » cochée.

---

## 6. Page « Tableau de bord »

La console du banc. Elle montre l'essentiel et porte le mode MANUEL.

### Bandeau « LECTURE SEULE »

Affiché seulement si la console refuse tout mouvement (`motion_enabled=false`). La
console `src.local_panel` actuelle est construite avec `motion_enabled=True` : ce bandeau
reste caché et la pastille **Console** indique `mouvement actif`.

### Bandeau « RAMPE EN COURS : NE PAS BOUGER LA TETE »

Affiché tant que la consigne marche vers la cible manuelle, avec la destination et le
temps d'arrivée estimé, par exemple
`vers 5.00 tr/min de sortie (Gr 1.001), arrivee dans ~0:08`. Un mouvement de tête
pendant un changement de vitesse provoque la nausée (effet Coriolis).

Il n'est pas affiché quand un `freeze` tient la consigne à distance d'une cible non
nulle : rien ne marche alors vers la cible, et la console n'annonce ni rampe ni heure
d'arrivée. Une descente vers 0 sous un `freeze` (STOP, ou cible remise à 0) est une
vraie rampe : le bandeau s'affiche, avec son heure d'arrivée.

### Carte « Mode MANUEL »

**Au repos** (pas de séance manuelle) :

| Élément | Rôle |
|---|---|
| case `BANC - personne a bord : NON (moteur decouple, ou bras couple avec la capsule VIDE)` | déclaration obligatoire avant tout démarrage ; la page refuse de démarrer si elle n'est pas cochée |
| champ `Operateur` | nom de la personne qui pilote ; obligatoire (400 sinon) |
| bouton `Demarrer MANUEL` | `POST /api/manual/start` avec `occupancy: "bench"` ; la cible initiale est 0 |

La page ne propose **que** la déclaration BANC. Une séance manuelle « personne à bord »
(`occupied`) n'existe que par l'API, et elle est refusée tant que
`OCCUPANCY_OCCUPIED_ENABLED=false` (jalon M6).

Pastille à côté du titre : `inactif`, `demarrage`, puis le libellé de l'occupation
(`BANC - personne a bord : NON`).

**Pendant la séance manuelle** :

| Élément | Rôle |
|---|---|
| `− palier` / `+ palier` | baisse ou monte le **brouillon** d'un cran de 0,1 Gr (g résultant) au rayon configuré |
| `−1 tr/min` / `+1 tr/min` | baisse ou monte le brouillon de 1 tr/min **de sortie** (bras) |
| grand nombre central | le brouillon, en tr/min de sortie. En **orange** tant qu'il diffère de la cible appliquée |
| `Appliquer` | envoie le brouillon : `POST /api/manual/target`. Rien n'est envoyé avant ce clic |
| grille | `cible appliquee`, `plafond`, `minimum de rotation` (chacun en tr/min sortie, moteur, Hz, Gc, Gr), `rampe` (`en cours, arrivee ~m:ss`, `cible atteinte`, ou `consigne maintenue, cible non atteinte` quand un `freeze` tient la consigne à distance d'une cible non nulle) |
| encadré orange `MONTEE RETENUE PAR LA FREQUENCE CARDIAQUE` | au-dessus de la cible, seulement avec une personne à bord : la fréquence cardiaque retient toute montée (voir plus bas) |

Règles de la cible :

- La cible est une **destination**, jamais une consigne directe. La machine y marche aux
  limites anti-nausée (accélération angulaire et variation de g, fichier
  `config/motion_limits.json`). Tout verdict de sécurité reste prioritaire.
- Domaine accepté : `0`, ou entre le minimum de rotation et le plafond. En simulation
  avec les valeurs par défaut : minimum 55 tr/min moteur (≈ 1,10 tr/min de sortie),
  plafond `MOTOR_MAX_RPM` = 300 tr/min moteur (≈ 6,03 tr/min de sortie).
- Les boutons ± gardent le brouillon dans ce domaine (sous le minimum : vers le
  minimum en montant, vers 0 en descendant ; au-dessus du plafond : le plafond).
- Une valeur hors domaine envoyée quand même est **refusée, pas arrondie**. Le refus
  arrive comme événement :
  `consigne refusee : 27.00 tr/min de sortie hors de 0 ou [55, 300] tr/min moteur`.
- Après n'importe quelle fin de séance (STOP, E-STOP, verdict d'arrêt), la cible vaut 0,
  et le brouillon est effacé quand le mode revient à `REPOS`.
- **Aucune cible n'attend sur un bras à l'arrêt.** Tant que quelque chose retient une
  montée et que la consigne appliquée vaut 0, une cible non nulle est **refusée** par la
  boucle ; une cible déjà acceptée, que quelque chose vient retenir avant le premier pas,
  est **remise à 0**. Ce qui retient : un verdict en cours, verrouillé ou non ; avec une
  personne à bord, une fréquence cardiaque inutilisable (aucune lecture fiable depuis
  plus de 4 s), de tendance inconnue (moins de 5 lectures) ou en baisse de plus de
  20 bpm/min ; un premier pas que le variateur n'a pas confirmé. Le bras ne part donc ni
  quand un avertissement se lève, ni après un acquittement, ni quand la fréquence
  cardiaque revient : il faut retaper la cible. Une cible de 0 est toujours acceptée, et
  rien ne change pour un bras qui tourne (la cible y est gardée, puis suivie). Une
  exception sous un `freeze` : une cible de 0 y est suivie tout de suite, comme un
  arrêt demandé, alors qu'une cible plus basse mais non nulle reste tenue. Quand la
  consigne arrive ainsi à 0 pendant qu'un avertissement tient, la séance se termine sur
  `session_standstill` (section 11) ; pour seulement terminer la séance, STOP. Les
  messages sont en section 13, la règle et ses mesures dans
  [securite.md](securite.md#76-aucune-cible-manuelle-nattend-sur-un-bras-à-larrêt-anh-178).
- **Ce que la page montre dans ce cas.** Le refus et la remise à 0 arrivent comme
  événements `refused` : ils sont listés dans **Evenements** (page Seance) et écrits,
  en rouge et mot pour mot, dans la note sous la carte (voir ci-dessous).
  `cible appliquee` reste ou revient à `0.00`, le brouillon reste affiché en orange, et
  `rampe` dit `cible atteinte`.

**La note sous la carte dit ce que la boucle a fait de la cible.** `Appliquer` reçoit
une réponse 202 dès que la boîte aux lettres a pris la cible ; la boucle la juge au
cycle suivant. La note suit ce que la machine rapporte :

| Note | Quand | Sens |
|---|---|---|
| `cible envoyee : 5.10 output rpm - pas encore prise par la machine` | à la réponse 202 | la console a reçu la demande ; la boucle n'a rien dit encore |
| `cible prise par la machine : 5.10 output rpm (suivie aux limites de mouvement)` | à la première image prise après la demande dont la `cible appliquee` est la cible envoyée (au tr/min moteur près) | la machine tient cette cible et y mène la consigne. La note est effacée dès que la machine ne tient plus la cible (STOP, verdict, cible d'un autre écran) |
| `cible prise par la machine : 5.10 output rpm` (sans la parenthèse) | même instant, puis image après image tant que cela dure | la machine tient cette cible mais ne la suit pas à cet instant : un verdict tient ou baisse la consigne (un `freeze` avec une cible non nulle, un `reduce`), ou, sans verdict, la fréquence cardiaque retient une montée vers une cible plus haute (encadré ci-dessous). Sous un `freeze`, une cible de 0 est un arrêt demandé : elle est suivie, et la parenthèse est écrite. La parenthèse revient dès que plus rien ne retient la consigne |
| `refus de la machine (<heure>) : <message>` (rouge) | à l'événement `refused` de la boucle | la cible a été refusée, ou remise à 0 : le message est celui de la section 13. Avec une séance manuelle à l'écran, un écran qui n'a pas cliqué l'affiche aussi, et tout autre refus de la boucle s'y écrit de la même façon (un reset de défaut refusé pendant l'arrêt de la séance, par exemple) : le message dit lui-même ce qui est refusé |
| `cible NON prise par la machine : la cible appliquee est <x> tr/min de sortie. La raison n'est pas arrivee a cet ecran.` (rouge) | une seconde de l'horloge de la machine après la demande, sans refus reçu ni cible appliquée | la cible n'a pas été prise et l'événement s'est perdu (un WebSocket qui se reconnecte perd ses événements) |
| `cible NON prise par la machine : la seance manuelle est terminee.` (rouge) | même délai, la séance étant finie | la séance s'est terminée avant que la cible soit prise |

Un refus arrivé avant la réponse 202 n'est pas écrasé par elle. Rejoué dans un
navigateur sur la console en simulation : après le clic, `cible envoyee …` à 49 ms,
puis `cible prise par la machine …` à 231 ms ; pour une cible refusée, `cible
envoyee …` à 67 ms puis `refus de la machine …` à 152 ms.

**L'encadré `MONTEE RETENUE PAR LA FREQUENCE CARDIAQUE`.** Avec une personne à bord, la
fréquence cardiaque retient toute montée de la consigne sans qu'aucun verdict ne
tienne. L'encadré le dit **avant** qu'une cible soit tapée, avec la raison :

| Texte | Raison |
|---|---|
| `pas de frequence cardiaque utilisable` | aucune lecture fiable depuis plus de 4 s |
| `tendance de la frequence cardiaque pas encore connue` | moins de 5 lectures depuis le début de l'ECG, ou depuis un saut confirmé |
| `la frequence cardiaque baisse trop vite` | baisse de plus de 20 bpm/min sur 5 lectures (garde vasovagale) |

Il est suivi de ce que cela implique :
`Bras a l'arret : une cible non nulle est refusee. Bras en rotation : la vitesse ne monte pas, puis remonte seule vers la cible quand la retenue cesse.`
La page ne recalcule pas cette garde : la console la donne elle-même dans `/api/panel`
(`manual_rise_hold`), par le test que la boucle applique, lu une fois par seconde.
L'encadré peut donc avoir jusqu'à une seconde de retard sur la boucle : une cible
envoyée dans cet intervalle est refusée, et la note en donne la raison. Il n'est
affiché qu'en mode `MANUEL`. Si `/api/panel` ne répond plus depuis 3,5 s alors qu'une
personne est à bord, il dit `RETENUE PAR LA FREQUENCE CARDIAQUE : INCONNUE` au lieu de
s'éteindre : éteint, il veut dire que rien ne retient. Avec une capsule vide il
n'apparaît jamais. Rejoué dans un navigateur, personne à bord déclarée par l'API :
4,4 s après la perte de l'ECG, l'encadré dit `pas de frequence cardiaque utilisable`,
avant tout verdict ; une cible envoyée alors est refusée pour cette raison. Sur la
fréquence cardiaque simulée, la raison `baisse trop vite` s'allume et s'éteint d'une
seconde à l'autre : la garde est sensible ([securite.md](securite.md#76-aucune-cible-manuelle-nattend-sur-un-bras-à-larrêt-anh-178)).

Autres messages sous la carte : `accepte : manual_start bench (cible 0)`, ou l'erreur
de la réponse HTTP. Le refus d'un **démarrage** manuel par la boucle n'y apparaît pas :
la note dit encore `accepte : …`, et le refus est dans la liste **Evenements**.

### Carte « Frequence cardiaque (regulation) »

La fréquence cardiaque **qui commande** la machine en séance AUTO (celle des pages
Capteurs n'est que de la surveillance). Grand nombre en bpm, pastille de qualité
(`good`, `noisy`, `mains_dominated`, `no_signal`, `perime`, ou `pas de signal`), et :
`age` (en s, barré si périmé), `brut` (dernière valeur), `seq` (numéro de la mesure).
Une fréquence périmée (plus de 4 s) est barrée, et sa pastille passe à `perime`
(orange) : la dernière note reçue ne dit plus rien du signal. La pastille est colorée
dès la péremption, sans attendre le premier verdict sur la fréquence cardiaque
(`hr_stale`, à 10 s).

### Carte « Vitesse mesuree »

Vitesse de sortie mesurée en grand, pastille de rotation, puis moteur, sortie, variateur
(Hz), Gc, Gr et le courant moteur (A).

### Carte « ECG »

Tracé des 6 dernières secondes environ (250 Hz). Pastille `250 Hz · seq N` (orange si une
discontinuité vient d'arriver). Une coupure est dessinée comme une coupure : relier les
deux bouts ferait un trait vertical qui ressemble à un QRS.

### Carte « Variateur »

Pastille de l'état CiA402 : `not_ready`, `switch_on_disabled`, `ready`, `switched_on`,
`operation_enabled`, `fault`, `comm_lost` (rouge pour `fault` et `comm_lost`).
Grille : `age du statut`, `LFT brut` (code défaut brut ou `aucun`), `liaison`
(`sim` ou `serial · description`), `latence` (ms), `lectures / echecs`,
`derniere erreur`, `rayon / rapport` (ex. `1.50 m / i = 49.79`), `plafond moteur`.

En cas de défaut, un encadré rouge :
`DEFAUT <mnémonique> (LFT brut <code>) : <signification>`.
Pour un code absent de la table, l'encadré reprend le message du pilote tel quel :
`DEFAUT code de defaut inconnu <code> (0x<code en hexadécimal>) : <signification>`.
La table des 66 codes LFT est dans [raspberry-pi.md](raspberry-pi.md).

Bouton **`Reset defaut variateur`** (visible seulement s'il y a un défaut) :
`POST /api/drive/fault-reset`. Nommé, explicite, jamais automatique. La boucle le refuse
sauf si la machine est au repos, que tout autre verdict est acquitté et que le variateur
montre l'arbre arrêté. Certains défauts ne sont **pas réarmables** depuis la console
(`reset refuse : defaut OCF non rearmable depuis la console : couper l'alimentation du variateur et inspecter`).
Message d'accord : `reset demande : fault_reset (le variateur doit le confirmer)`.

### Reprise automatique de la liaison variateur

La console tente d'abord de retrouver automatiquement une observation complète,
sans relancer de séance ni réarmer un défaut. Ouvrir le port local ne prouve pas
que le variateur répond ; une acquisition ETA valide confirme l'adressage,
mais seule la lecture complète du statut rétablit l'observation.

Une panne où aucune requête n'a pu partir continue à être retentée au repos.
Si une inspection échoue après du trafic possible, la console suit cet épisode
d'état inconnu séparément : une nouvelle ouverture réussie ne suffit pas à
l'effacer. Une lecture complète réussie l'efface ; si elle trouve le variateur
déjà activé ou en rotation, la console arrête et verrouille, sans reprise.

Après le nombre d'échecs configuré par la supervision (`comms_lost_failures`,
trois par défaut), un épisode inconnu non résolu devient `GO_SILENT`. Ce compte
n'est pas une garantie de délai physique. Le processus ne transmet ensuite
plus aucune trame, même de lecture ou de fermeture. Le zéro d'urgence existant
est tenté une seule fois en entrant dans ce mode, uniquement si l'adressage a
été prouvé indépendamment ; sans cette preuve, aucune écriture aveugle.

`comm_lost` et `VITESSE INCONNUE` restent inconnus même si une ancienne lecture
à zéro est récente. Un zéro acquitté ne prouve ni l'arrêt de l'arbre ni la
désactivation de la sortie. Une annulation attend la vraie inspection et garde
ses preuves avant de terminer. Après `GO_SILENT`, l'intervention manuelle
consiste à vérifier l'arrêt réel puis redémarrer le processus ; l'acquittement
ne lève pas le silence, et une nouvelle séance exige une nouvelle demande.

### Carte « BITalino »

Pastille `acquisition`, `connecte` ou `deconnecte`. Grille : `source` (et adresse),
`tentatives` de connexion, `lots traites` (et échantillons), `dernier lot` (âge),
`DSP` (seq et qualité), `tendance` (bpm/min). Avec une vraie liaison série ou RFCOMM
s'ajoutent : `trames`, `pertes de synchro`, `octets ignores`, `echantillons combles`,
`reconnexions`, et le cas échéant `lots sans ECG` et `erreur`.

---

## 7. Page « Capteurs »

Vue d'ensemble des canaux BITalino acquis (variable `SENSORS`, par défaut `ECG` seul).
Mention permanente : **surveillance uniquement**, aucune de ces lectures ne commande le
moteur.

Pastille du titre : `N / M bon signal`, `aucun canal` ou `hors ligne`. Une carte par
capteur : nom, pastille de qualité (`bon signal`, `bruite`, `parasite secteur`,
`pas de signal`, ou `perime` en orange), une mini-courbe, le canal, l'unité, la cadence
affichée, et jusqu'à quatre mesures. Cliquer une carte ouvre la page du capteur.

Si aucun canal n'est acquis :
`Aucun canal BITalino supplementaire n'est acquis par cette console (variable SENSORS).`

---

## 8. Page d'un capteur

Titre : `<TYPE> · <libellé>`, pastille du canal (`canal A1`…) et de la qualité.
Encadré `SURVEILLANCE UNIQUEMENT`. Puis :

- **Signal** : tracé grand format, axe du temps en secondes avant maintenant, unité sur
  l'axe vertical. Pastille `N ech/s · en direct` ou `fige depuis N s`. Si le canal est figé,
  le tracé passe en gris avec la mention **`DONNEES FIGEES - NE PAS CROIRE CE TRACE`**.
- **Mesures** : une tuile par mesure dérivée (valeur grisée si absente ou figée).
- **Capteur** : description, canal, unité, cadence, fenêtre, qualité, rôle
  (`surveillance uniquement : ne commande pas le moteur`).

Les six capteurs, tels que la console les expose (libellés vérifiés en simulation) :

| Type | Canal | Libellé | Unité | Fenêtre / cadence affichée | Mesures affichées |
|---|---|---|---|---|---|
| ECG | A1 | Electrocardiogramme | mV | 10 s / 250 ech/s | Fréquence cardiaque (affichage), Intervalle RR moyen, RMSSD (variabilité), Battements dans la fenêtre |
| EDA | A2 | Activite electrodermale | µS | 20 s / 50 ech/s | Niveau tonique (SCL), Fréquence des réponses (SCR), Amplitude moyenne des SCR, Nombre de SCR |
| SpO2 | A3 | Photoplethysmogramme (pouls au doigt) | u.a. | 10 s / 100 ech/s | Fréquence du pouls, Indice de perfusion relatif, Irrégularité du pouls (CV), **SpO2 : non mesurable (une seule longueur d'onde)** |
| RESP | A4 | Respiration | % | 30 s / 50 ech/s | Fréquence respiratoire, Amplitude, Régularité, Plus longue pause |
| EMG | A5 | Electromyogramme | mV | 5 s / 250 ech/s | Amplitude efficace, Activation, Fréquence médiane, Amplitude crête |
| LUX | A6 | Lumiere | % | 60 s / 10 ech/s | Niveau lumineux, Variation lumineuse, Rotation estimée (scintillement), Changements brusques |

Limites honnêtes :

- **SpO2** : le capteur BITalino n'a qu'une longueur d'onde. La saturation n'est **pas**
  calculable ; la mesure affiche toujours `-`.
- **ECG** de cette page : un second calcul de la fréquence pour affichage. La régulation
  utilise toujours `src/ecg_pipeline.py` (carte « Fréquence cardiaque (régulation) »).
- **LUX** : l'estimation de rotation suppose une lampe fixe devant laquelle passe la
  capsule. En simulation, le signal lumineux est synthétique et son estimation ne suit pas
  la vitesse du simulateur de variateur (observé : 3,9 tr/min affichés à l'arrêt). Ne pas
  la lire comme une mesure de vitesse.

---

## 9. Page « Seance »

La séance **programmée** (AUTO), où la fréquence cardiaque pilote la vitesse.

### Carte « Programme »

| Élément | Rôle |
|---|---|
| `Profil` (liste) | les profils stockés, affichés `nom (id)` |
| `Duree totale (minutes, vide = celle du profil)` | surcharge optionnelle de la durée |
| `Operateur` | obligatoire |
| `Age du passager (ans, obligatoire)` | une séance programmée refuse un âge inconnu ou inférieur à `MIN_RIDER_AGE` (18 par défaut) |
| `Previsualiser` | `POST /api/plan/preview` : résout le profil **sans rien démarrer** (fonctionne même pendant une séance). Message : `plan resolu ; rien n'a ete demarre` |
| `Demarrer la seance` | `POST /api/session/start`. En cas d'accord, la page reste sur Seance |

Le bouton **Demarrer la seance** est désactivé tant que : l'arrêt d'urgence n'est pas
attesté, la machine n'est pas `idle`, ou les séances programmées sont désactivées.
Dans ce dernier cas une note dit :
`seances programmees desactivees sur cette console (jalon M5) : utiliser le mode MANUEL`.
Elle a sa propre ligne : le résultat d'une prévisualisation s'affiche sous elle, sans
être effacé.

Pour qu'une séance programmée puisse partir, **toutes** ces conditions doivent tenir
(dans l'ordre où la console les vérifie, `LocalPanel._start_programme`) :

1. `PROGRAMS_ENABLED=true` ;
2. âge du passager connu et ≥ `MIN_RIDER_AGE` ;
3. `OCCUPANCY_OCCUPIED_ENABLED=true` (une séance programmée a toujours une personne à bord) ;
4. le profil existe et se résout (ajusté à la FC max du passager si elle est connue, pour
   une séance lancée depuis le tableau de bord) ;
5. le plafond du profil (`max_rpm`) est sous le plafond « personne à bord » ;
6. la caméra, si configurée, ne bloque pas le démarrage ;
7. les contrôles du runtime : attestation, aucun verdict en attente, paliers cardiaques du
   profil égaux à ceux du superviseur (`HR_HARD_MAX_BPM` / `HR_CRITICAL_BPM`), variateur
   disponible, pas en défaut, pas déjà en marche.

Avec la configuration par défaut, les points 1 et 3 sont faux : **aucune séance
programmée ne démarre** (403). Cela a été vérifié en simulation.

### Carte « Ce que ce programme ferait »

Limites du profil : `zone` (bpm), `max absolu` (palier dur), `critique`, `FC max du sujet`,
`plafond`, `plafond echauffement`, `minimum de rotation` (tr/min moteur), `HSP variateur`
(Hz), `total`, `palier` (durée du HOLD). Après prévisualisation s'ajoutent : `charge au
plafond` (g et tr/min de sortie), `charge echauffement`, `charge minimum`, `duree modifiee`,
et le tableau des phases (`baseline`, `warmup`, `hold`, `cooldown`, `recovery`, avec début,
fin, durée).

Exemple vérifié (`standard_30_min`) : zone 118 à 138 bpm, plafond 276 tr/min moteur =
5,54 tr/min de sortie = 10 Hz = 0,052 g à 1,5 m. Ce plafond est trop bas pour atteindre la
zone sur le sujet modélisé (constat 7 de `simulation/README.md`).

### Cartes de suivi

| Carte | Contenu |
|---|---|
| **Frequence cardiaque** | bpm en grand (barré si périmé), qualité (`perime` si périmée), bande de zone avec un curseur, `cible`, `age`, `brut`, temps `dans la zone`, `au-dessus`, `en dessous` |
| **Phase** | pastille de phase (`baseline`, `warmup`, `hold`, `cooldown`, `recovery`, `done` ; `-` au repos, où aucune phase n'est en cours), barre de progression, `ecoule`, `restant`, `securite` |
| **Mesure** | vitesse de sortie **mesurée** en grand, pastille de rotation, les cinq grandeurs et le courant |
| **Consigne** | la consigne commandée (plus petite, grisée), pastille `confirmee par le variateur` ou `non confirmee` (écho LFRD) |
| **Variateur** | état, `age du statut`, `courant`, `mesure` (tr/min moteur), encadré de défaut |
| **Securite** | action en cours, règle, détail, verrouillé ou non, depuis ; bouton `Verdicts et acquittement` (ouvre la page Securite) |
| **ECG** | même tracé que sur le Tableau de bord |
| **Evenements** | les 40 derniers événements : heure, type, `[opérateur]`, détail |

Types d'événements : `start_requested`, `fault_reset_requested`, `end_requested`,
`emergency_stop`, `acknowledged`, `attested`, `session_running`, `session_idle`,
`refused`. Les refus de la boucle (cible hors domaine, cible refusée sur un bras à
l'arrêt, reset refusé, démarrage refusé) arrivent comme événements `refused` : la
route HTTP a déjà répondu 202. Une cible manuelle que la machine a remise à 0 arrive de
la même façon, sans nom d'opérateur : personne ne l'a demandé. Tant qu'une séance
manuelle est à l'écran, ces refus sont aussi écrits dans la note de la carte Mode
MANUEL (section 6). Hors séance manuelle, un démarrage ou un reset refusé par la
boucle n'apparaît que dans cette liste.

---

## 10. Page « Configuration »

### Carte « Acces »

Champ `Jeton partage` et bouton `Utiliser ce jeton`. Le jeton est gardé dans le
`sessionStorage` du navigateur (onglet courant) et envoyé dans l'en-tête
`X-Anheart-Token` (et en paramètre `?token=` du WebSocket).

- En boucle locale (`UI_HOST=127.0.0.1`), le jeton est facultatif : laisser vide.
- Hors boucle locale, la console **refuse de démarrer** sans `UI_TOKEN` d'au moins
  16 caractères (message vérifié :
  `configuration: UI_HOST/UI_PORT/UI_TOKEN: refusing to serve on '0.0.0.0' with a 5-character token: at least 16 characters are required off loopback`).
- Si `UI_TOKEN` est défini, toute route `/api/*` sans le bon jeton répond 401
  `a valid x-anheart-token header is required`.

La note sous la carte dit `connecte a la machine` quand l'API répond, sinon l'erreur.

### Carte « Systeme »

Grille lue dans `/api/status` : `etat`, `e-stop verrouille`, `verdict retenu`,
`plancher verrouille`, `regles actives`, `echantillons FC retenus`, `accompagnant vu`
(`jamais` ou `il y a N s`), `clients telemetrie`, `clients evinces` (écrans déconnectés
pour retard), `ecg` (fréquence et seq), `profils`, `revision` du stock de profils,
`commandes acc/ref` (acceptées / refusées), `snapshots` publiés.

**Ports serie** : les ports série trouvés par le Pi (`aucun port serie trouve` sinon).

---

## 11. Page « Securite »

### Carte « Verdict en cours »

- Pastille de l'action de sécurité en cours.
- Grille : `regle`, `detail`, `verrouille` (oui/non), `depuis` (s) ; ou
  `aucune demande`.
- **Verdicts retenus** : `e-stop verrouille` (l'arrêt d'urgence de l'opérateur ou de
  la caméra, tenu à part), `verdict retenu` (règle / action : le verdict en vigueur, le
  plus sévère de l'arrêt d'urgence, du plancher verrouillé et des règles actives ; il
  n'est pas forcément verrouillé), `plancher verrouille` (le verdict
  verrouillé le plus sévère du superviseur, quelle que soit la règle : défaut variateur,
  fréquence cardiaque, `session_standstill`, caméra…), `regles actives`,
  `accompagnant vu`. Puis la liste des règles actives (`action règle détail [verrouille]`).

Les règles elles-mêmes (identifiants, seuils, actions) sont décrites dans
[raspberry-pi.md](raspberry-pi.md).

**Un avertissement non verrouillé se lève seul.** Un `freeze` ou un `reduce` dont la
ligne `verrouille` dit `non` (par exemple `hr_stale` avant 60 s, `attendant_absent`
avant 120 s, `hr_rate`, `current_high` au niveau d'alerte) disparaît quand sa cause
disparaît. Tant que le bras tourne, la consigne suit alors de nouveau la régulation ou
la cible, **vers le haut aussi, sans aucun clic**. Tant que la séance peut encore
prendre de la vitesse, la ligne `detail` de ces verdicts se termine par cette phrase,
en anglais comme les phrases de verdict du superviseur (celles des règles caméra
sont en français, et celle d'un défaut variateur reprend le libellé français du
défaut) :
`; NOT LATCHED: it lifts by itself when its cause ends, and the speed then follows the programme or the manual target again, upwards too, with nobody clicking`.
Elle n'est affichée que sur les pages Seance et Securite.

**Le bandeau `REPRISE AUTOMATIQUE POSSIBLE`.** La page le dit en français, sur toutes
les pages, dans un bandeau orange en haut, tant que c'est vrai :
`REPRISE AUTOMATIQUE POSSIBLE` suivi de
`l'avertissement <règle> tient la vitesse et n'est pas verrouille : il se leve seul, et la vitesse remonte alors sans aucun clic`
(`baisse la vitesse` pour un `reduce`). Il est affiché quand, à la fois :

- un `freeze` ou un `reduce` est en cours et n'est pas verrouillé ;
- le mode est `SEANCE` ou `MANUEL` (pas `ARRET` ni `REPOS`) ;
- la phase est `baseline`, `warmup` ou `hold` ;
- en mode `MANUEL`, la cible appliquée est au-dessus de la consigne.

Sur un bras manuel tenu à l'arrêt, la cible vaut 0 (section 6) : rien ne remontera, et
le bandeau n'est pas affiché. Il ne l'est jamais derrière un verdict verrouillé, un
arrêt, ni une séance qui se termine. Rejoué dans un navigateur, séance manuelle avec
une personne à bord déclarée par l'API, bras en montée vers 12 tr/min, ECG coupé :
bandeau `… hr_stale tient la vitesse …` à 10,1 s, `… baisse la vitesse …` à 30,2 s,
bandeau disparu à 40,2 s, quand la baisse a ramené la consigne à 0 ; la séance s'est
terminée au cycle suivant (`session_standstill`).

Limite connue : sur un bras manuel tenu à l'arrêt par un avertissement non verrouillé,
la phrase anglaise `NOT LATCHED …` figure encore dans le détail du verdict, alors que
plus rien ne peut remonter. Le bandeau, lui, n'y est pas affiché. La phrase est
ajoutée par le superviseur, qui ne connaît pas la cible manuelle.

**Un bras arrêté en cours de séance ne repart jamais seul : `session_standstill`.**
Une fois que le bras a tourné dans une séance, une consigne qui revient à 0 sans que
personne l'ait demandé (amenée par un `reduce`, ou par la régulation cardiaque
elle-même) termine la séance au cycle suivant : `ramp_down` verrouillé, règle
`session_standstill`, mode `ARRET` puis `REPOS`. Le détail nomme la cause :
`the arm came to a standstill inside the session (the warning hr_stale brought the setpoint to zero): the session has ended, and a stopped arm never restarts by itself. To be acknowledged by a named operator once the session is over; moving again takes a new start`
(ou `the heart-rate regulation brought the setpoint to zero`). Quand ce verdict
s'affiche, c'est la **consigne** qui vaut déjà 0 : la règle juge la consigne confirmée
par le variateur, pas une mesure. La vitesse mesurée suit. Sur la console en simulation,
elle valait encore 27 tr/min moteur (pastille Rotation `EN ROTATION`) à l'instantané où
le verdict et `ARRET` sont apparus, et 0 à l'instantané suivant, 0,2 s après ; ce délai
n'a pas été mesuré sur la vraie machine. C'est la pastille **Rotation** qui dit que le
bras est arrêté (section 3, règle 2). Le bras ne repart pas quand la cause disparaît
(électrode remise, par exemple). Il faut acquitter par son nom une fois la séance
finie, c'est-à-dire quand **Mode** affiche `REPOS` (pour un programme, après la phase
`recovery` : environ 5 minutes avec le profil standard), puis demander un nouveau
départ. Avant `REPOS`, l'acquittement répond 200 mais le verdict est de nouveau là au
cycle suivant. Ne sont pas concernés : un STOP (donné sous un `freeze` ou non), une
cible manuelle que l'opérateur met lui-même à 0 alors qu'aucun avertissement ne tient,
le retour au calme d'un programme. Une cible mise à 0 pendant qu'un avertissement tient
est concernée : sous un `reduce` comme avant, et maintenant sous un `freeze`, où elle
est suivie jusqu'à 0. Le détail dit alors
`the operator's manual target of zero, followed under the warning loop_stall, brought the setpoint to zero`
(avec le nom de la règle qui tenait).
En séance manuelle, capsule vide comprise, la règle vaut aussi. Décisions et mesures :
[securite.md](securite.md#7-décisions-des-5-et-6-octobre-2026-sur-les-reprises-automatiques).

**Une séance qui dépasse sa durée est arrêtée, une séance finie n'est plus jugée :
`session_overrun`.** La règle termine, sur un `ramp_down` verrouillé, une séance encore
en cours plus de 30 s après sa durée prévue (celle du programme ; 3600 s en séance
manuelle). Le détail donne les durées :
`the session has run 1830 s against a programme of 1800 s plus 30 s of grace: the phase machine has lost track`.
Elle ne juge qu'une séance **en cours**. Une fois la séance finie (phase `done`,
consigne à 0), elle se tait : la console peut rester au mode `REPOS` aussi longtemps
qu'il le faut sans qu'aucun verdict apparaisse, et le départ suivant est accepté sans
redémarrer la console. Avant le correctif du 6 octobre 2026 (ANH-181), ce verdict
apparaissait seul au repos (30 s après la fin d'un programme allé à son terme, 1830,2 s
après le départ du profil standard même arrêté tôt, 3630,2 s après le départ d'une
séance manuelle), l'acquittement ne tenait pas et il fallait redémarrer la console.
Le verdict peut encore apparaître **pendant** une séance : quand un `freeze`
verrouillé que personne n'acquitte (par exemple `loop_stall`) tient le bras en vitesse
au-delà de la fin du programme sans que personne demande l'arrêt, et la règle fait
alors descendre la consigne (un STOP donné sous ce `freeze` la fait descendre tout de
suite, section 5) ; quand
une fin de séance est ouverte tard dans un programme (STOP, E-STOP ou verdict d'arrêt),
parce que toute fin de séance rouvre une récupération complète, qui dépasse alors
l'échéance, bras déjà arrêté (section 5) ; quand une séance manuelle atteint ses 3600 s
à grande vitesse et descend encore 30 s plus tard. Dans tous ces cas : attendre que
**Mode** affiche `REPOS`, acquitter par son nom, puis demander un nouveau départ.
L'acquittement tient alors. Avant `REPOS`, il répond 200 et le verdict est de nouveau
là au cycle suivant. Après un E-STOP donné tard, `session_overrun` se verrouille
derrière l'arrêt d'urgence : un seul acquittement à `REPOS` lève les deux ; si l'E-STOP
est acquitté avant `REPOS`, `session_overrun` reste ou apparaît ensuite comme verdict
retenu (pastille **Securite** `ramp_down`), à acquitter de nouveau à `REPOS`. Après un
verdict d'arrêt donné tard (par exemple `hr_stale` à son niveau `ramp_down`), le
plancher verrouillé garde ce premier verdict : `session_overrun` n'apparaît que dans
la liste des règles actives pendant `ARRET`, et un seul acquittement à `REPOS` suffit.
Mesures et limites :
[securite.md](securite.md#8-une-séance-finie-nest-plus-jugée-sur-sa-durée-anh-181).

### Acquittement

| Élément | Rôle |
|---|---|
| champ `votre nom` | obligatoire |
| case `le coup de poing a ete deverrouille (tire)` | déclaration séparée : le logiciel ne voit pas le contact. Par défaut non cochée, pour échouer du côté sûr |
| bouton `Acquitter` | `POST /api/safety/acknowledge` : efface les verdicts verrouillés. **Rien d'autre ne le fait**, jamais automatiquement |

Résultats possibles (vérifiés en simulation) :

- `acquitte par doc : operator_estop` ;
- `the emergency stop is still latched: confirm the mushroom has been pulled back out (estop_released) before acknowledging` (case non cochée après un E-STOP) ;
- `nothing is latched to acknowledge` ;
- un verdict `GO_SILENT` ne s'acquitte pas : `… demanded GO_SILENT, which is one-way: this process will not command motion again, and recovery is an operator action on a machine that has demonstrably stopped`. Il faut redémarrer la console.

Un refus s'affiche sous le bouton et dans une boîte d'alerte `acquittement refuse : …`.

### Carte « Cablage de l'arret d'urgence » (l'attestation en deux cases)

Avant tout mouvement, à **chaque démarrage du Pi**, une personne nommée doit attester
deux faits distincts :

- case `Le pont STO a ete retire du variateur.`
- case `Un arret d'urgence a accrochage est cable normalement ferme sur P24 → STO.`
- champ `Votre nom`, bouton `Enregistrer l'attestation` (`POST /api/safety/attest`).

Les deux cases sont nécessaires, séparément : une personne sûre d'un seul des deux faits
ne doit pas pouvoir valider les deux d'un clic. Une demi-attestation est refusée **sans
rien enregistrer** (400 :
`both confirmations are required, separately: the STO jumper is removed, AND a latching emergency stop is wired normally-closed into P24 -> STO`).

La phrase attestée est enregistrée mot pour mot dans le journal :
`a latching mushroom emergency stop is wired normally-closed into P24 -> STO and the STO jumper has been removed`.

L'attestation vit dans le processus et meurt avec lui : après un redémarrage, personne n'a
garanti le câblage. La pastille passe de `non atteste` à `atteste`, avec
`atteste par <nom> a <heure> (valable jusqu'au prochain redemarrage du Pi)`.

Sans attestation, tout démarrage (manuel ou programmé) répond 412 :
`nobody has attested the emergency stop this boot: …`.

> **Important.** L'attestation est une déclaration humaine, pas une mesure. Le logiciel ne
> vérifie pas que le pont STO est retiré. Voir [securite.md](securite.md).

### Carte « Camera / presence »

Alimentée par `GET /api/camera` (chaque seconde). Pastille selon l'état :

| État | Pastille | Sens |
|---|---|---|
| `absent` | `non branchee` | `PRESENCE_SOURCE=none` (défaut). Rien ne surveille la capsule ; les règles listées **ne sont pas actives** |
| `waiting` | `en attente` | caméra configurée, aucune image encore jugée |
| `clear` | `zone degagee` | aucune règle ne s'oppose |
| `start_blocked` | `demarrage bloque` | au repos, une règle refuse le démarrage (détail affiché) |
| `ramp_down` | `ralentissement` | une règle a demandé l'arrêt contrôlé (verrouillé) |
| `emergency_stop` | `ARRET D'URGENCE` | une règle a demandé l'arrêt d'urgence (verrouillé) |

Le nom de la source apparaît entre parenthèses (`sim_empty`, `sim_occupied`). Un verdict
caméra verrouillé est rappelé :
`Verdict verrouille : <règle> - acquitter dans Securite apres verification.`

Règles prévues (rappelées sur la page) : intrusion pendant la rotation → arrêt d'urgence ;
personne dans une capsule déclarée vide → arrêt d'urgence ; capsule vide, harnais détaché,
membre dehors (personne à bord) → refus du démarrage ou ralentissement ; caméra perdue ou
figée → ralentissement ; aucune reprise automatique.

> **Il n'existe aujourd'hui qu'une caméra simulée** (`sim_empty`, `sim_occupied`). Elle ne
> regarde jamais la vraie machine. Aucun modèle de vision n'est branché. Détails :
> `raspberry-pi/src/presence/README.md` et [raspberry-pi.md](raspberry-pi.md).

### Présence de l'accompagnant

Il n'y a pas de bouton : tant qu'un onglet de la console est ouvert, la page envoie
`POST /api/presence` toutes les 5 s. La règle `attendant_absent` gèle la consigne
(`FREEZE`) après 60 s sans signal, puis termine la séance (`RAMP_DOWN`, verrouillé)
après 120 s. Le gel n'est pas verrouillé : si un onglet redonne le signal avant 120 s,
il se lève seul et la consigne suit de nouveau la cible ou la régulation.
Cela prouve qu'un onglet est ouvert et joignable, **pas** qu'un humain regarde.

---

## 12. Déroulé type d'une séance manuelle de banc

Rejoué en simulation, dans cet ordre :

1. **Securite** → cocher les deux cases, saisir un nom, `Enregistrer l'attestation`.
2. **Tableau de bord** → cocher `BANC - personne a bord : NON`, saisir `Operateur`,
   `Demarrer MANUEL`. Réponse `accepte : manual_start bench (cible 0)`.
3. Composer une cible avec `+1 tr/min` ou `+ palier`, puis `Appliquer`. Le bandeau
   `RAMPE EN COURS` s'affiche jusqu'à l'arrivée.
4. Surveiller **Vitesse mesuree** (pas la consigne).
5. `STOP` (rampe contrôlée). Attendre `a l'arret` et le mode `REPOS`. STOP fait
   descendre la consigne même si la pastille **Securite** dit `freeze` à ce moment
   (section 5). Pour un arrêt immédiat : coup de poing câblé et E-STOP.
6. En cas d'urgence : `E-STOP`, puis, une fois la machine arrêtée et le coup de poing
   réarmé, **Securite** → nom, case `coup de poing deverrouille`, `Acquitter`.

---

## 13. Messages d'erreur typiques

Les réponses HTTP (codes 4xx) s'affichent sous le bouton concerné. Les refus de la boucle
arrivent comme événements `refused` dans la liste **Evenements** (page Seance). Tant
qu'une séance manuelle est à l'écran, ils sont aussi écrits dans la note sous la carte
Mode MANUEL, précédés de `refus de la machine (<heure>) :` (section 6).

### Refus immédiats (réponse HTTP)

| Message | Code | Sens |
|---|---|---|
| `an operator name is required: an unattributable session record is not one` | 400 | nom d'opérateur vide (démarrage, cible, fin, reset, acquittement) |
| `nobody has attested the emergency stop this boot: …` | 412 | attestation du câblage non faite depuis le démarrage du Pi |
| `a latched safety verdict stands (<règle>): <détail> - it must be acknowledged by name first` | 409 | un verdict verrouillé attend un acquittement |
| `the machine is starting, not idle (pending: …)` | 409 | double clic : une commande attend déjà dans la boîte aux lettres (une seule place) |
| `the machine is running, not idle` | 409 | une séance tourne déjà |
| `no manual session is running (the machine is idle)` | 409 | cible envoyée sans séance manuelle |
| `the machine is already stopping` | 409 | STOP pendant un arrêt |
| `there is no session to end (the machine is idle)` | 409 | STOP sans séance |
| `the target must be a finite, non-negative output speed` | 422 | cible négative ou non finie |
| `unknown occupancy 'x': expected one of bench, occupied` | 422 | occupation inconnue (API) |
| `personne a bord refusee : OCCUPANCY_OCCUPIED_ENABLED=false (jalon M6, accords ingenierie et medical requis)` | 403 | séance manuelle `occupied` refusée par configuration |
| `seances programmees desactivees sur cette console (jalon M5) : utiliser le mode MANUEL.` | 403 | `PROGRAMS_ENABLED=false` |
| `no profile 'x'; known: standard_30_min, standard_45_min` | 404 | profil inconnu |
| `'…' is not a well-formed profile id` | 400 | identifiant de profil mal formé |
| `the store moved on: you edited revision N, it is now M. Reload before saving, or an edit is lost silently` | 409 | modification concurrente d'un profil |
| `both confirmations are required, separately: …` | 400 | attestation à une seule case |
| `the emergency stop is still latched: confirm the mushroom has been pulled back out (estop_released) before acknowledging` | 409 | acquittement sans la case « coup de poing » |
| `nothing is latched to acknowledge` | 409 | rien à acquitter |
| `a valid x-anheart-token header is required` | 401 | jeton absent ou faux |

### Refus de la boucle (événement `refused`)

Démarrage (`describe_start_refusal`, `rider_age_refusal`, `describe_resolve_error`) :

| Message | Sens |
|---|---|
| `demarrage refuse : la machine est deja <état>` | une séance existe déjà |
| `demarrage refuse : cablage de l'arret d'urgence non atteste` | attestation manquante |
| `demarrage refuse : verdict <règle> a acquitter (<détail>)` | verdict en attente |
| `demarrage refuse : seuil <nom> different de celui du superviseur` | paliers cardiaques du profil ≠ `HR_HARD_MAX_BPM` / `HR_CRITICAL_BPM` |
| `demarrage refuse : variateur deja en marche (<n> tr/min), arret demande` | variateur trouvé en marche (processus précédent mort) ; la console le met à zéro et exige un acquittement |
| `demarrage refuse : variateur en defaut (<mnémonique>, LFT <code>)` | défaut variateur présent |
| `demarrage refuse : age du passager requis pour une seance programmee` | âge vide |
| `demarrage refuse : passager de <n> ans, minimum <m> ans (MIN_RIDER_AGE)` | passager trop jeune |
| `demarrage refuse : programme '<id>' inconnu sur cette machine` | profil absent (lancement distant) |
| `demarrage refuse : programme inadapte a ce passager (<détail>)` | profil rejeté pour la FC max du passager (zone au-dessus de 90 % de la FC max, etc.) |
| `demarrage refuse : programme a <n> tr/min moteur, au-dessus du plafond personne a bord (<m> tr/min)` | profil trop rapide pour une personne à bord |
| `demarrage refuse : <règle caméra …>` | la caméra bloque le démarrage |
| `seances programmees desactivees (PROGRAMS_ENABLED=false, jalon M5) : utiliser MANUEL` | lancement distant d'une séance AUTO sur une console où elles sont désactivées |

Cible manuelle (`describe_target_refusal`) :

| Message | Sens |
|---|---|
| `consigne refusee : pas de session manuelle (<état>)` | plus de séance manuelle |
| `consigne refusee : <détail>` | la séance manuelle se termine |
| `consigne refusee : <x> tr/min de sortie hors de 0 ou [<min>, <plafond>] tr/min moteur` | hors domaine : ni arrondie ni bornée |
| `consigne refusee : le verdict <règle> tient le bras a l'arret. Attendre qu'il soit leve, puis redonner la cible` | un avertissement non verrouillé tient le bras à l'arrêt : attendre qu'il se lève, retaper la cible |
| `consigne refusee : le verdict <règle> tient le bras a l'arret. L'acquitter une fois sa cause levee, puis redonner la cible` | un verdict verrouillé tient le bras à l'arrêt : traiter la cause, acquitter (page Securite), retaper la cible |
| `consigne refusee : pas de frequence cardiaque utilisable, rien ne monte depuis l'arret. Attendre une frequence cardiaque fiable, puis redonner la cible` | personne à bord, aucune lecture fiable depuis plus de 4 s, aucun verdict : vérifier les électrodes, attendre la lecture, retaper la cible |
| `consigne refusee : tendance de la frequence cardiaque pas encore connue, rien ne monte depuis l'arret. Attendre quelques secondes de lecture, puis redonner la cible` | personne à bord, moins de 5 lectures depuis le début de l'ECG (ou depuis un saut confirmé) : attendre quelques secondes, retaper la cible |
| `consigne refusee : la frequence cardiaque baisse trop vite, rien ne monte depuis l'arret. Attendre qu'elle se stabilise, puis redonner la cible` | personne à bord, baisse de plus de 20 bpm/min sur 5 lectures (garde vasovagale) : attendre, retaper la cible |

Cible manuelle remise à 0 par la machine (`describe_withdrawn_target`, événement
`refused` sans nom d'opérateur). La cible avait été acceptée ; quelque chose est venu
retenir le bras à l'arrêt avant le premier pas. Dans tous les cas : traiter ce que dit
le message, puis retaper la cible.

| Message | Sens |
|---|---|
| `cible de <n> tr/min moteur remise a 0 : le verdict <règle> tient le bras a l'arret. Attendre qu'il soit leve, puis redonner la cible` (ou `L'acquitter une fois sa cause levee`) | un verdict est apparu avant le premier pas |
| `cible de <n> tr/min moteur remise a 0 : pas de frequence cardiaque utilisable, rien ne monte depuis l'arret. Attendre une frequence cardiaque fiable, puis redonner la cible` | la lecture est devenue trop vieille avant le premier pas |
| `cible de <n> tr/min moteur remise a 0 : tendance de la frequence cardiaque pas encore connue, rien ne monte depuis l'arret. Attendre quelques secondes de lecture, puis redonner la cible` | l'historique de la fréquence cardiaque vient de recommencer (saut confirmé) |
| `cible de <n> tr/min moteur remise a 0 : la frequence cardiaque baisse trop vite, rien ne monte depuis l'arret. Attendre qu'elle se stabilise, puis redonner la cible` | une lecture a fermé la garde vasovagale avant le premier pas |
| `cible de <n> tr/min moteur remise a 0 : le variateur n'a pas confirme la consigne, elle n'est pas redemandee. Verifier la liaison, puis redonner la cible` | le variateur n'a pas confirmé le premier pas (capsule vide comprise). Le pas est demandé une seule fois. Si la trame est arrivée et que seule sa réponse s'est perdue, le variateur l'a gardée un cycle, jusqu'au zéro suivant : lire la vitesse mesurée, vérifier le câble, retaper la cible |

Ces messages sont vérifiés par les tests automatiques de la console en simulation
(`raspberry-pi/tests/test_manual_target_held_console.py`). Trois ont été rejoués dans
un navigateur le 7 octobre 2026, lus dans la note de la carte : le refus sous le
verdict `hr_stale` non verrouillé, et les refus `pas de frequence cardiaque
utilisable` et `la frequence cardiaque baisse trop vite`. Les trois raisons de
fréquence cardiaque ne concernent qu'une séance manuelle « personne à bord », que la
page ne propose pas (section 6).

Reset défaut variateur (`describe_reset_refusal`) :

| Message | Sens |
|---|---|
| `reset refuse : mouvement encore commande (<état>, <phase>)` | une séance est encore active |
| `reset refuse : acquitter d'abord le verdict <règle>` | un autre verdict attend |
| `reset refuse : aucun defaut a acquitter (<état>)` | pas de défaut |
| `reset refuse : l'arbre tourne encore (<n> tr/min moteur)` | l'arbre n'est pas arrêté |
| `reset refuse : defaut <mnémonique> non rearmable depuis la console : couper l'alimentation du variateur et inspecter` | défaut non réarmable |
| `reset refuse : <détail>` | le variateur n'a pas pris le reset |

---

## 14. Référence des routes HTTP et WebSocket

Source : `raspberry-pi/src/web/routes.py` et `src/web/ws.py`. Tous les gestionnaires sont
asynchrones et « minces » : ils lisent l'état ou déposent une intention dans une boîte aux
lettres à une place ; aucun ne parle au variateur. La boucle de contrôle (toutes les 0,2 s)
exécute l'intention.

**Authentification.** Les routes `/api/*` exigent l'en-tête `X-Anheart-Token` quand
`UI_TOKEN` est défini (comparaison en temps constant), sinon 401. `/healthz`, `/`,
`/app.css`, `/app.js` sont publics (la page est inerte sans l'API). `/openapi.json` est
servi ; `/docs` et `/redoc` sont désactivés.

**Codes qui ont un sens.**

| Code | Sens |
|---|---|
| 200 | réponse lue, ou E-STOP **déjà** verrouillé |
| 202 | intention acceptée dans la boîte aux lettres ; la boucle n'a pas encore agi. Un refus de la boucle arrive ensuite comme événement `refused` |
| 400 | requête mal formée : nom vide, attestation incomplète, profil incohérent |
| 401 | jeton absent ou faux |
| 403 | mouvement désactivé, séances programmées désactivées, occupation refusée par configuration |
| 404 | profil inconnu, fichier statique absent |
| 409 | la machine n'est pas dans un état pour ça |
| 412 | arrêt d'urgence non attesté depuis ce démarrage |
| 422 | valeur inutilisable (profil rejeté, cible négative, occupation inconnue) ou corps JSON invalide (validation FastAPI) |
| 500 | stock de profils non inscriptible |

### Public

| Méthode | Chemin | Rôle | Retour |
|---|---|---|---|
| GET | `/healthz` | vivacité pour le healthcheck du conteneur ; ne dit rien de la séance | 200 `{"status":"ok","service":"anheart-operator-interface"}` |
| GET | `/` | la page | 200, 404 si le fichier manque |
| GET | `/app.css` | feuille de style | 200, 404 |
| GET | `/app.js` | script | 200, 404 |

### Lectures (jeton si configuré)

| Méthode | Chemin | Rôle | Retour |
|---|---|---|---|
| GET | `/api/status` | tout ce que la mise en route demande : `run_state`, `estop_latched` (le drapeau de l'interface web, posé par sa route E-STOP seulement), `supervisor_estop` (l'arrêt d'urgence que le superviseur tient verrouillé, quel qu'en soit l'auteur, caméra comprise ; `null` sinon ; il reste lisible quand `go_silent` a pris la place de `standing` et de `floor`), `attested`, `attestation`, `attestation_statement`, `standing`, `floor`, `live` (verdicts actifs), `retained_hr_samples`, `pending`, `attendant_last_seen`, `clients`, `evictions`, `ecg_fs_hz`, `ecg_seq`, `profile_rev`, `profile_ids`, `ports`, `bind`, `counters` | 200 |
| GET | `/api/snapshot` | dernier instantané de télémétrie (`null` avant la première tick) : `phase`, `mode`, `heart_rate`, `live_bpm`, `target_bpm`, `setpoint`, `measured`, `setpoint_confirmed`, `drive_state`, `drive_status_age_s`, `drive_status_stale`, `current_a`, `fault`, `safety`, `safety_action`, `safety_rank`, `counters`, `manual` | 200 |
| GET | `/api/ecg?after=<seq>&limit=<n>` | ECG récent par numéro de séquence ; `gap: true` si l'anneau a dépassé `after` | 200 |
| GET | `/api/panel` | panneau de liaison : `motion_enabled`, `programs_enabled`, `motor_backend`, `drive` (lectures, échecs, latence, dernière erreur), `ecg` (compteurs BITalino, DSP), `heart_rate_trend_bpm_per_min`, `manual_rise_hold` (ce par quoi la fréquence cardiaque retient une montée en séance manuelle : `no_heart_rate`, `trend_unknown` ou `heart_rate_falling` ; `null` si rien ne retient, hors séance manuelle en cours, et toujours avec une capsule vide), `radius_m`, `gear_ratio`, `motor_max_rpm` ; `null` sans console | 200 |
| GET | `/api/camera` | `configured`, `camera`, `state`, `detail`, `latched_rule` | 200 |
| GET | `/api/sensors` | chaque canal acquis : fenêtre, qualité, mesures (surveillance seulement) | 200 |

### Profils

| Méthode | Chemin | Corps | Rôle | Retour |
|---|---|---|---|---|
| GET | `/api/profiles` | - | profils stockés et révision | 200 |
| PUT | `/api/profiles/{profile_id}?rev=<n>` | document de profil JSON (son `profile_id` doit égaler l'URL) | crée ou remplace ; validé par `parse_profile` | 200 ; 400 (id incohérent, document mal formé) ; 422 (profil rejeté, toutes les violations listées) ; 409 (révision dépassée) ; 500 (écriture impossible) |
| DELETE | `/api/profiles/{profile_id}?rev=<n>` | - | supprime un profil | 200 ; 404 ; 409 ; 500 |
| POST | `/api/plan/preview` | `{"profile_id", "total_duration_s"?}` | résout sans rien démarrer : phases et vitesses exprimées en moteur, sortie, Hz, g | 200 ; 404 ; 422 |

La page n'a pas d'éditeur de profils : `PUT` et `DELETE` ne sont accessibles que par l'API.

### Séance

| Méthode | Chemin | Corps | Rôle | Retour |
|---|---|---|---|---|
| POST | `/api/session/start` | `{"profile_id", "operator", "total_duration_s"?, "subject_age"?}` | démarre une séance programmée | 202 ; 403 (mouvement ou programmes désactivés) ; 400 (nom vide, id mal formé) ; 404 ; 422 ; 409 ; 412 |
| POST | `/api/session/stop` | `{"operator", "reason"?}` | fin sur la rampe mise en service | 202 ; 400 ; 409 |
| POST | `/api/session/estop` | `{"operator"?, "reason"?}` | verrouille l'arrêt d'urgence, **jamais refusé** | 200 `{operator, reason, at, wall_clock, action, rule, detail}` |

### Manuel et variateur

| Méthode | Chemin | Corps | Rôle | Retour |
|---|---|---|---|---|
| POST | `/api/manual/start` | `{"occupancy": "bench" \| "occupied", "operator"}` | séance manuelle, cible 0 ; l'occupation est déclarée une fois | 202 ; 403 (mouvement désactivé, occupation refusée) ; 422 (occupation inconnue) ; 400 ; 409 ; 412 |
| POST | `/api/manual/target` | `{"output_rpm", "operator"}` | cible en tr/min **de sortie** ; la consigne y marche aux limites de mouvement | 202 ; 422 (négatif, non fini) ; 400 ; 409 |
| POST | `/api/drive/fault-reset` | `{"operator"}` | reset nommé d'un défaut variateur | 202 ; 400 ; 409 |

### Sécurité

| Méthode | Chemin | Corps | Rôle | Retour |
|---|---|---|---|---|
| POST | `/api/safety/attest` | `{"operator", "sto_jumper_removed", "mushroom_wired_nc"}` | attestation du câblage, pour ce démarrage | 200 ; 400 (une case manque, nom vide) |
| POST | `/api/safety/acknowledge` | `{"operator", "estop_released"?}` (défaut `false`) | efface les verdicts verrouillés | 200 `{operator, at, wall_clock, cleared}` ; 400 ; 409 |
| POST | `/api/presence` | `{"operator"?}` | signal de présence de l'accompagnant (règle `attendant_absent`) | 200 `{"at": …}` |

### WebSocket `/ws/telemetry`

- Paramètre `?token=<jeton>` si `UI_TOKEN` est défini.
- La poignée de main est refusée **avant** acceptation (code 1008) si l'en-tête `Origin`
  n'est pas dans la liste autorisée (l'adresse d'écoute, et `localhost` / `127.0.0.1` en
  boucle locale, en `http` et `https`), ou si le jeton est faux. Un client sans `Origin`
  (outil, test) est admis, mais reste soumis au jeton.
- Messages envoyés : `{"kind": "snapshot"}` (instantanés regroupés), `{"kind": "event"}`
  (chaque événement), `{"kind": "ecg"}` (tranche de l'anneau ECG après chaque instantané,
  jusqu'à 1500 échantillons à la connexion), `{"kind": "resync"}` quand l'écran a pris du
  retard ; le serveur ferme alors avec le code 1013
  (`this screen fell behind and was disconnected; reload to resync`).
- Observé en simulation sur 14 s : environ 5 instantanés et 5 trames ECG par seconde.

---

## 15. Écarts connus entre le code, la page et les README

- Le message 403 de mouvement désactivé (`MOTION_DISABLED_DETAIL`, `routes.py` :
  `mouvement desactive : cette console est en lecture seule. STOP, E-STOP, acquittement et lectures restent disponibles.`)
  et le bandeau « LECTURE SEULE » de la page existent encore, mais ni l'un ni l'autre ne
  sont atteignables avec `src.local_panel`, qui construit la console avec
  `motion_enabled=True`.
- Après un démarrage manuel, un démarrage de programme ou un reset de défaut, la note
  du bouton dit `accepte : …` ou `reset demande : …` dès la réponse 202, même quand la
  boucle refuse ensuite la demande : hors séance manuelle, ce refus n'apparaît que
  dans la liste **Evenements** de la page Seance. Pour une cible manuelle, la note suit
  maintenant la réponse de la boucle (section 6).
- La phrase anglaise ajoutée aux avertissements non verrouillés (section 11) figure
  encore dans le détail du verdict d'un bras manuel tenu à l'arrêt, où plus rien ne
  peut remonter. Le bandeau `REPRISE AUTOMATIQUE POSSIBLE`, lui, suit la cible.
- Un acquittement de `session_standstill` (ou de `session_overrun`) donné avant le
  retour du mode à `REPOS` répond 200 puis est repris, sans que la page dise quand il
  tiendra (section 11).
- Le bandeau `ARRET D'URGENCE VERROUILLE` garde trois cas limites : après un
  redémarrage de la console suivi de `go_silent`, un bandeau affiché avant le
  redémarrage reste jusqu'au rechargement de la page ; après un acquittement très
  rapide, une réponse de statut tardive peut le faire revenir 5 s au plus ; l'ordre de
  deux réponses de statut est jugé à l'envoi des demandes, pas à leur traitement.
- La note `nothing is latched to acknowledge` reste sous le bouton Acquitter après un
  nouveau verrou ; l'onglet de la page d'un capteur porte le type seul (`RESP -
  AnHeart`) alors que la page titre `RESP · Respiration` ; la hauteur du pied de page
  déclarée dans la feuille de style est plus petite que sa hauteur réelle.
- Ces points relèvent de la page. Ils viennent du ticket
  [ANH-182](https://linear.app/anheart/issue/ANH-182/console-locale-suites-daffichage-apres-anh-124-anh-176-et-anh-178),
  qui a traité la note de la cible manuelle, l'encadré de la fréquence cardiaque, la
  demande E-STOP sans réponse, le verrou d'arrêt d'urgence dans `/api/status`, le
  bandeau de reprise automatique, les pastilles barrées et `perime` en orange.
- La page ne permet de déclarer que l'occupation BANC. L'API accepte `occupied`, refusée
  tant que `OCCUPANCY_OCCUPIED_ENABLED=false`.
- La page n'a pas d'éditeur de profils, alors que l'API en fournit un (`PUT`/`DELETE`).
- L'estimation de rotation du capteur LUX ne suit pas le variateur simulé en simulation.

# La console locale du Raspberry Pi

## Santé distante de la machine (ANH-148)

Le heartbeat transmet les mesures système disponibles toutes les dix secondes.
La collecte utilise un worker disque dédié, borné à une seconde; un disque
bloqué produit des valeurs inconnues et ne bloque pas la boucle moteur.
Les compteurs vivent en mémoire sur cette boucle et sont enregistrés à chaque
collecte dans `health-counters.json`, à côté du dossier des enregistrements.
Un fichier illisible donne des compteurs inconnus, sans remise à zéro implicite.
Une séance restée ouverte au dernier point de sauvegarde compte comme échouée
au redémarrage. Une coupure peut perdre au plus les observations faites depuis
la dernière sauvegarde réussie; ces compteurs ne remplacent pas le journal.
Les heures de rotation additionnent uniquement les intervalles observés d'au
plus une seconde avec une vitesse mesurée positive aux deux extrémités.

La batterie BITalino, l'état thermique du variateur et le dernier préflight
restent inconnus tant que leurs producteurs ne les fournissent pas.
`incidents_open_high` reprend le dernier poll compatible qui fournit un entier
positif ou nul; sans ce champ il reste inconnu. Le nombre d'enregistrements en
attente ne baisse qu'après confirmation de dépôt, pas après l'envoi de télémétrie.
La carte distante affiche ces inconnues et l'ancienneté du rapport explicitement.

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
> 6 octobre 2026 avec ANH-181, réécrits le 8 octobre 2026 avec ANH-185 pour les fins
> de séance ouvertes tard et les verdicts qui arrivent au repos) viennent du code, des
> tests automatiques sur la console en simulation et de mesures sur le banc d'essai
> logiciel ; ils n'ont pas été rejoués dans un navigateur.
> L'enregistrement de séance (section 16, la carte de la section 10, les messages et
> les routes qui s'y rapportent, ajoutés le 7 octobre 2026) vient du code et des tests
> automatiques sur la console en simulation ; rien n'a été rejoué dans un navigateur ni
> mesuré sur un vrai Pi.
> Les passages sur le retour au calme d'un programme sous un `freeze` (sections 4 et
> 11, ajoutés le 7 octobre 2026 avec ANH-189) viennent du code et des tests automatiques
> sur la console en simulation (`raspberry-pi/tests/test_cooldown_freeze_console.py`) ;
> ils n'ont pas été rejoués dans un navigateur.
> Ce que la page affiche de plus depuis le 7 octobre 2026 (la note de la carte MANUEL
> pour une cible prise, refusée ou remise à 0, l'encadré de la fréquence cardiaque, les
> bandeaux `ARRET D'URGENCE NON CONFIRME` et `REPRISE AUTOMATIQUE POSSIBLE`, les
> pastilles barrées sous `NO LIVE DATA`, `perime` en orange) a été rejoué dans un
> navigateur sans interface, sur la console en simulation, avec une personne à bord
> déclarée par l'API pour ce qui dépend de la fréquence cardiaque.
> Les règles du bandeau `ARRET D'URGENCE VERROUILLE` face à une réponse de statut en
> retard, à deux réponses croisées et à une console relancée, le texte du bandeau
> `ARRET D'URGENCE NON CONFIRME` après une demande échouée et le bandeau
> `STOP NON CONFIRME` (sections 2, 5, 14 et 15, ajoutés le 10 octobre 2026) ont été
> rejoués de la même façon, avant et après la correction, les réponses de la console
> étant retenues ou coupées entre la page et elle ; jamais sur la machine réelle.
> Le bandeau levé par le statut seul (WebSocket de l'écran coupé, ou aucune image
> reçue) puis gardé sous `go_silent` l'a été aussi, jusqu'à la console relancée.
> Les notes des boutons de démarrage et de reset, la note d'une cible après un refus
> qui n'était pas le sien ou un double clic, la note sous **Acquitter**, les lignes
> `verdict retenu` et `plancher verrouille`, le titre de l'onglet d'un capteur et la
> place du pied de page (sections 2, 4, 6, 9, 11 et 15, ajoutés le 10 octobre 2026)
> ont été rejoués de la même façon. Le refus d'un autre écran y est un événement remis
> à la page par le rejeu, faute de pouvoir le produire à l'instant voulu ; la phrase
> sur `REPOS` d'un acquittement repris et la note d'une cible sous une retenue
> `INCONNUE` ne sont tenues que par les tests de la page.
> Trois cas l'ont été avec deux écrans réels, avant et après leur correction : un
> démarrage (de séance, puis manuel) demandé pendant que la liaison de l'écran était
> coupée et refusé par la boucle, suivi du démarrage d'un autre écran ; une cible
> appliquée par un autre écran pendant que la note disait `(cible 0)` ; un clic sur
> **Appliquer** reçu pendant que la demande d'un autre écran attendait ; et deux
> clics sur **Demarrer MANUEL**, de deux écrans, acceptés tous deux dans le même
> cycle (variateur simulé dont la lecture d'état est retardée de 70 ms, hors dépôt,
> sans toucher à la console : obtenu au deuxième essai).
> Depuis le 11 octobre 2026 la console donne un identifiant à chaque demande et dit
> elle-même ce que chacune est devenue (sections 6, 9, 13, 14 et 15). Rejoué avec
> deux écrans réels, avant et après, sur la console en simulation et le même
> variateur simulé ralenti hors dépôt : les deux clics sur **Demarrer MANUEL** du
> même cycle (le second reçoit maintenant 409, le premier écran lit `pris`) ; le
> reset suivi de deux démarrages de séance ; un démarrage qu'un STOP d'un autre
> écran déplace avant la boucle ; et, avec un écran, un démarrage demandé liaison
> coupée, puis une cible prise, une cible refusée et un reset. Les durées sont en
> section 6. La règle des notes est en outre tenue par un test de la page qui joue
> toutes les suites de trois demandes au plus face à un modèle de la boîte de la
> console (65 643 suites, 13 491 histoires d'écran distinctes). Rien de cela n'a été
> joué sur un vrai variateur ni sur la machine.
> Les pastilles **Serveur** et **Version** (sections 2, 3, 4, 13 et 14, ajoutées le
> 9 octobre 2026 avec ANH-210) viennent du code et des tests automatiques, sur le lien
> réel face à un tableau de bord factice et sur la console en simulation ; elles n'ont
> été rejouées ni dans un navigateur, ni face à un déploiement Convex réel.
> La pastille **Service**, le bandeau `CONSOLE HORS SERVICE` et la remise en service
> (sections 2, 3, 4, 11, 13 et 14, ajoutés le 10 octobre 2026 avec ANH-162) viennent du
> code et des tests automatiques sur la console en simulation ; ils n'ont pas été
> rejoués dans un navigateur.
> La page servie en TLS hors de la boucle locale (sections 1, 10 et 14, ajoutés le
> 10 octobre 2026 avec ANH-151) vient du code et des tests automatiques, avec un client
> TLS de test sur la boucle locale d'une machine de test ; rien n'a été rejoué dans un
> navigateur ni sur une tablette, et ce que le navigateur affiche face à un certificat
> qu'il refuse est donné comme attendu.

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
16. [L'enregistrement de séance (boîte noire locale)](#16-lenregistrement-de-séance-boîte-noire-locale)

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
{"ts": "2026-10-10T16:21:03.870Z", "level": "WARNING", "logger": "__main__", "event": "Console du banc sur http://127.0.0.1:8090/ - MANUEL BANC (plafond 300 tr/min moteur; paliers 148/158 bpm) - variateur: simulateur - ECG (sim): simulateur - rayon 1.5 m, i = 49.79 - tableau de bord: aucun"}
{"ts": "2026-10-10T16:21:04.512Z", "level": "INFO", "logger": "src.web.app", "event": "operator interface serving 28 routes on 127.0.0.1:8090 (loopback=True, token=none, tls=off)"}
```

La seconde ligne dit aussi, depuis le 10 octobre 2026, si la page est servie en
TLS (`tls=on`) ou en clair (`tls=off`) ; la première écrit alors `https://` au
lieu de `http://`.

Depuis le 10 octobre 2026, chaque ligne du journal de la console est un objet JSON
(champs `ts`, `level`, `logger`, `event`) : la phrase vérifiée ci-dessus est dans
`event`, et les heures de cet exemple sont illustratives
([raspberry-pi.md](raspberry-pi.md#124-lhorloge-murale-et-le-journal-de-la-console)).

Points à savoir :

- Les variables du processus **priment** sur `raspberry-pi/.env`. Si un `.env` existe et
  contient `MACHINE_API_KEY`, la console se relie au tableau de bord Convex. Forcer
  `MACHINE_API_KEY=` (vide) pour rester strictement local. Ce qui est enregistré
  pendant ce temps reste sur le disque, mais part au tableau de bord dès qu'une clé est
  de nouveau posée si la console a déjà tourné une fois avec une clé sur ce dossier
  d'enregistrements : aucun réglage ne l'empêche, le seul levier est manuel
  ([raspberry-pi.md, 8.4](raspberry-pi.md#84-après-un-redémarrage-de-la-console)).
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

### Depuis une tablette : la page en TLS

> **État réel au 10 octobre 2026.** Ce qui suit vient du code et des tests
> automatiques (`raspberry-pi/tests/test_console_tls.py`, sur la boucle locale
> d'une machine de test). **Aucune tablette n'a encore ouvert la page ainsi**, et
> sur un Pi installé la page écoute toujours sur la boucle locale
> ([pi-image.md](pi-image.md#le-certificat-de-la-console)). Ce que le
> navigateur affiche est donné comme attendu, pas comme constaté.

Dès que la console écoute ailleurs que sur la boucle locale, elle ne sert sa
page qu'en TLS, avec le certificat fait sur le Pi à la mise en service
([raspberry-pi.md, 12.5](raspberry-pi.md#125-la-page-en-tls-hors-de-la-boucle-locale)).
La tablette, connectée au réseau de la console, ouvre :

```
https://<adresse de la console>:<UI_PORT>/
```

L'adresse est l'une de celles que porte le certificat (`UI_TLS_NAMES`).
`http://` à la même adresse ne donne rien : la connexion est fermée sans
réponse, et rien ne redirige vers `https://`. Le jeton se saisit ensuite comme
d'habitude (page **Configuration**, carte **Acces**).

**Une fois par tablette**, l'autorité de ce Pi doit être installée sur la
tablette, en comparant son empreinte
([pi-image.md](pi-image.md#mettre-lautorité-sur-la-tablette)). Après quoi le
navigateur ouvre la page sans avertissement.

Ce que l'opérateur voit quand ce n'est pas le cas : **une page d'avertissement
du navigateur, à la place de la console**. La console n'y est pour rien et ne
peut rien y afficher.

| Ce que dit le navigateur | Ce que cela veut dire | Quoi faire |
|---|---|---|
| certificat ou autorité **non reconnus** (Chrome : `NET::ERR_CERT_AUTHORITY_INVALID`) | l'autorité de ce Pi n'est pas installée sur cette tablette (sur iOS : installée mais confiance non activée) ; ou le certificat a été refait depuis ; ou ce n'est pas la console qui répond | installer l'autorité. **Ne pas passer l'avertissement** : tant qu'il est là, rien ne distingue la console d'un autre appareil du réseau, et le jeton saisi irait à qui répond |
| le **nom** ne correspond pas (`NET::ERR_CERT_COMMON_NAME_INVALID`) | l'adresse tapée n'est pas l'une de celles du certificat | ouvrir l'adresse notée à la mise en service |
| certificat **expiré**, ou pas encore valable (`NET::ERR_CERT_DATE_INVALID`) | le certificat a dépassé sa durée (825 jours), ou l'horloge de la tablette est fausse | vérifier l'heure de la tablette ; sinon renouveler le certificat ([pi-image.md](pi-image.md#renouveler)) |

**Un certificat qui expire n'arrête rien et n'empêche pas d'arrêter.** La
console ne juge pas ses propres dates : elle continue de servir, et une séance
en cours continue (prouvé par test, sur la console en simulation). TLS ne
rejuge jamais un certificat sur une connexion déjà ouverte : c'est une
connexion **nouvelle** (page rechargée, autre onglet, autre tablette) qui
reçoit l'avertissement. Une page déjà ouverte interroge la console chaque
seconde, ce qui doit garder ses connexions ouvertes et y faire passer **STOP**
et **E-STOP** ; ce point-là dépend du navigateur et **n'a pas été constaté sur
une tablette**. Pour le cas où il faut une connexion nouvelle, la console
n'impose pas au navigateur de refuser définitivement (pas d'en-tête HSTS) : le
navigateur garde son moyen de passer l'avertissement d'un certificat expiré, à
n'employer que pour arrêter une machine. L'arrêt câblé, lui, ne dépend ni de
la tablette ni du certificat. Ce qui est prouvé et ce qui ne l'est pas est en
[securite.md, section 11](securite.md#11-la-console-en-tls-hors-de-la-boucle-locale-anh-151).

---

## 2. Structure de la page

Une seule page HTML, sans framework, sans CDN, sans étape de build : le Pi la sert
hors ligne (`src/web/static/index.html`, `app.js`, `app.css`). Elle comporte :

| Zone | Contenu |
|---|---|
| Bandeaux en haut | Huit rouges : `NO LIVE DATA`, visible dès que les données ne sont plus fraîches ; `ARRET D'URGENCE NON CONFIRME`, quand une demande E-STOP de cet écran reste sans réponse ou a échoué ; `STOP NON CONFIRME`, de même pour une demande STOP ; `ARRET D'URGENCE VERROUILLE`, tant que la page sait un arrêt d'urgence verrouillé (section 5) ; `CONSOLE HORS SERVICE`, tant que la console se dit hors service (section 4) ; `VARIATEUR EN DEFAUT`, tant que le variateur signale un défaut (section 6) ; `CHAINE STO NON PRETE`, avec la raison et l'action pour rétablir une chaîne prête (section 11) ; `PARAMETRES MEDICAUX NON SIGNES`, tant que la console tourne en mode banc parce que la signature de son fichier médical ne s'est pas vérifiée au démarrage, suivi de la raison ([raspberry-pi.md, 12.6](raspberry-pi.md#126-les-paramètres-médicaux-signés-anh-139)) ; il ne part qu'avec un fichier signé de nouveau et un redémarrage. Un orange : `REPRISE AUTOMATIQUE POSSIBLE`, tant qu'un avertissement non verrouillé tient ou baisse une vitesse qui peut remonter seule (section 11). Ils s'empilent ; la barre latérale et la barre mobile commencent sous eux |
| Barre latérale (à gauche) | la marque, dix pastilles (sept sur l'état de la machine, puis **Serveur**, **Version** et **Service** sur la console elle-même), la navigation |
| Zone centrale | une seule page à la fois : Tableau de bord, Capteurs, un capteur, Seance, Configuration, Securite |
| Pied de page fixe | la version des paramètres médicaux (`parametres medicaux 0.1.0`, en rouge avec `NON SIGNES` en mode banc), l'état de la liaison, **STOP** et **E-STOP**, présents sur toutes les pages. La page et la barre latérale s'arrêtent au-dessus de lui : sa hauteur est mesurée par la page (`--footer-h`), plus déclarée dans la feuille de style |

Sur un écran étroit, la barre latérale se replie derrière un bouton `☰ Menu`. Une barre
mobile affiche alors le mode, l'état de rotation et l'état du lien avec le tableau de
bord (`serveur joignable`, `serveur incompatible`…, section 4).

La page reçoit ses données par un WebSocket (`/ws/telemetry`, environ 5 images par
seconde) et par des interrogations périodiques :

| Source | Période | Sert à |
|---|---|---|
| `/ws/telemetry` | continu | instantanés, événements, tracé ECG |
| `/api/panel` | 1 s | liaisons variateur et BITalino, mode console, retenue de la fréquence cardiaque en séance manuelle, version du logiciel, état du lien avec le tableau de bord, console en service ou hors service |
| `/api/sensors` | 1 s | pages Capteurs |
| `/api/camera` | 1 s | carte caméra de la page Securite |
| `/api/status` | 5 s | état, verdicts (quel qu'en soit l'auteur), état du variateur, attestation, système |
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
   depuis 3,5 s, les pastilles **Console**, **Serveur**, **Service** et BITalino
   (`acquisition`) sont barrées, comme celle de la barre mobile (`serveur …`) ;
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

La pastille **Chaine STO** reste visible dans la barre latérale sur chaque page :
`prete`, `ouverte`, `inconnue` ou `non configuree`. La carte du même nom dans Sécurité
donne aussi la source et l'âge de la dernière lecture réussie. Ces états ne disent
pas que le bras est arrêté : seule la vitesse mesurée renseigne sur son mouvement.

| Pastille | Valeurs | Source |
|---|---|---|
| **Mode** | `REPOS`, `MANUEL`, `SEANCE`, `ARRET` | l'instantané (`mode`) |
| **Etat** | `idle`, `starting`, `running`, `stopping`. En rouge tant qu'un arrêt d'urgence est verrouillé, quel qu'en soit l'auteur : après un arrêt posé par la caméra, la pastille dit `idle` en rouge | `/api/status` (`run_state`, `estop_latched`, `supervisor_estop`, `standing`) |
| **Liaison** | `en direct`, `donnees figees`, `hors ligne` | la fraîcheur du WebSocket |
| **Rotation** | `a l'arret`, `EN ROTATION`, `VITESSE INCONNUE` | vitesse mesurée |
| **Securite** | action de sécurité en cours : `none`, `freeze`, `reduce`, `ramp_down`, `quick_stop`, `go_silent` (vert, orange, rouge). Un `freeze` ou un `reduce` non verrouillé se lève seul, et la vitesse d'un bras qui tourne remonte alors sans clic : le bandeau orange `REPRISE AUTOMATIQUE POSSIBLE` le dit tant que c'est le cas ; `ramp_down`, `quick_stop` et `go_silent` terminent la séance (section 11). `freeze` ne veut pas dire que la consigne ne bouge plus : elle descend sous un `freeze` après un STOP (section 5) et, pour un programme, à partir de sa phase `cooldown` (section 11) | l'instantané |
| **Console** | `mouvement actif`, `LECTURE SEULE`, `pas de console` | `/api/panel` |
| **Serveur** | `joignable`, `injoignable`, `incompatible`, `cle refusee`, `en erreur`, `en attente`, `non configure` : le lien de la console avec le tableau de bord ([détail plus bas](#pastille-serveur--le-lien-avec-le-tableau-de-bord)) | `/api/panel` (`dashboard`) |
| **Version** | la version du logiciel de la console, par exemple `pi-0.0.0-dev` ; `pi-unknown` si le fichier `VERSION` manque ou est mal formé | `/api/panel` (`software_version`) |
| **Service** | `en service` (vert) ou `HORS SERVICE` (rouge) : la console arme-t-elle quoi que ce soit ([détail plus bas](#pastille-service--en-service-ou-hors-service)) | `/api/panel` (`service`) |

Les modes :

| Mode | Signification |
|---|---|
| `REPOS` | rien n'est commandé. Le variateur est **lu** (2 Hz), jamais écrit. Une séance finie n'y déclenche aucun verdict, aussi longtemps que la console y reste : le départ suivant ne demande pas de redémarrer la console (section 11, `session_overrun`). Un arrêt d'urgence ou un défaut variateur qui arrive là, séance finie, ne fait pas quitter ce mode : la pastille **Securite** affiche le verdict, verrouillé et à acquitter (section 11). |
| `MANUEL` | séance manuelle : l'opérateur fixe une cible, la machine y va aux limites de mouvement |
| `SEANCE` | une séance programmée (AUTO) déroule son programme. Son retour au calme (phase `cooldown`) se fait dans ce mode, y compris quand la pastille **Securite** dit `freeze` : la consigne descend alors comme sans avertissement (section 11) |
| `ARRET` | une séance se termine. Le mode dure jusqu'à la fin de la séance, c'est-à-dire phase `done` et étage de sortie retiré : en séance manuelle de banc, dès l'arrêt mesuré ; pour un programme, après la phase `recovery` (environ 5 minutes avec le profil standard). **Pas** « arrêté » : lire la vitesse mesurée. Après un STOP ou une règle d'arrêt, la consigne descend encore vers zéro, y compris quand la pastille **Securite** dit `freeze` (section 5). Après la règle `session_standstill` (section 11), la consigne vaut déjà 0 quand `ARRET` s'affiche et la vitesse mesurée suit : dans la fraction de seconde qui suit en simulation, non mesuré sur la vraie machine. C'est la pastille **Rotation** qui dit que le bras est arrêté |

Les états `run_state` (vue « intention » de la surface de commande) :

| État | Signification |
|---|---|
| `idle` | rien de demandé. Seul état où un démarrage est accepté, et seulement si la boîte aux lettres est vide (section 14) |
| `starting` | un démarrage est accepté : il attend dans la boîte, ou la boucle l'arme. L'état le dit jusqu'à la réponse de la boucle, y compris à la fin d'un cycle qui n'a pas vu la demande ; tout autre démarrage est refusé (409 `start_pending`) |
| `running` | la boucle a confirmé une séance, et personne n'a demandé sa fin ni un E-STOP depuis la page. L'état reste `running` pendant tout le mode `ARRET` d'une fin de séance que la console décide elle-même (un verdict d'arrêt, un arrêt posé par la caméra, la règle `session_standstill`) : il ne dit donc pas que la séance avance encore. Lire **Mode** |
| `stopping` | une fin ou un E-STOP est accepté ; un E-STOP garde cet état jusqu'à l'acquittement |

Sous les pastilles, une première ligne donne le motif de l'état du lien avec le
tableau de bord et l'âge de sa dernière réponse (voir ci-dessous). Une deuxième dit ce
que vaut l'horloge de la machine (voir « Ligne horloge », plus bas). La dernière indique
l'adresse d'écoute, par exemple `127.0.0.1:8090 boucle locale · sans jeton`, ou
`… RESEAU · jeton` hors boucle locale.

### Pastille Serveur : le lien avec le tableau de bord

La pastille **Serveur** dit, en permanence et sur toutes les pages, où en est le lien
entre **cette console** et le tableau de bord (Convex). Ce n'est pas la pastille
**Liaison**, qui parle du lien entre la page et la console. C'est un état, pas un
événement : un événement `dashboard` défile et n'est envoyé qu'aux écrans connectés à
ce moment-là, alors qu'une page ouverte une heure après le début d'une incompatibilité
lit `incompatible` tout de suite.

| Valeur | Couleur | Quand | Que faire |
|---|---|---|---|
| `non configure` | sans couleur | la console n'a pas de clé de machine (`MACHINE_API_KEY` vide) : elle n'échange rien avec le tableau de bord | rien si c'est voulu ; sinon régler `MACHINE_API_KEY` et `CONVEX_URL` ([raspberry-pi.md](raspberry-pi.md#121-clés-lues-par-la-console-srclocal_configpy)) et redémarrer la console |
| `en attente` | orange | une clé est réglée, la console vient de démarrer et rien n'est encore revenu du tableau de bord. 25 s au plus | attendre |
| `joignable` | vert | il y a moins de 25 s, le tableau de bord a pris quelque chose que la console lui a envoyé (son heartbeat, un lancement demandé, un envoi de séance) : il a répondu dans sa propre forme, par un succès ou par le refus d'une demande sous l'un de ses codes stables. Rien d'autre ne donne `joignable` | rien |
| `injoignable` | rouge | depuis 25 s, rien de reconnaissable n'est revenu : pas de réseau, délai dépassé, ou une réponse qui n'est pas celle du tableau de bord (un 404 ou un 400 sans code stable, une page HTML) | vérifier le réseau du Pi, puis `CONVEX_URL` si le motif dit `pas une reponse du tableau de bord`. La machine fonctionne comme sans tableau de bord ; ce qu'une séance lui doit reste sur le disque et part au retour du lien |
| `incompatible` | rouge | le tableau de bord n'est pas de la même majeure de contrat : il l'a répondu (426), ou la console l'a lu dans la version qu'il annonce. Affiché dès la première réponse qui le dit | mettre à jour le côté en retard. Aucun lancement distant n'est armé tant que cela dure ; un arrêt demandé du tableau de bord, lui, passe toujours |
| `cle refusee` | rouge | le tableau de bord refuse la clé de la machine (401 ou 403). Affiché dès la première réponse qui le dit | vérifier `MACHINE_API_KEY` : la machine a pu être supprimée ou désactivée sur le tableau de bord, ou sa clé remplacée |
| `en erreur` | rouge | le tableau de bord est là, et depuis 25 s il ne prend rien de ce que la console envoie : il ne répond que par des erreurs de son côté (5xx) ou des demandes d'attendre (408, 425, 429 ; un 429 retient aussi sa route le temps demandé, [raspberry-pi.md 8.6](raspberry-pi.md#86-ce-que-la-synchronisation-ne-peut-pas-coûter)), ou bien il répond encore à la question d'arrêt d'une séance alors que le heartbeat et les envois échouent | attendre ; si cela dure, la panne est du côté du tableau de bord, pas du réseau. Pendant ce temps le tableau de bord ne reçoit rien de la machine |

La ligne sous les pastilles donne le motif, puis l'âge de la dernière réponse :
`serveur incompatible (contrat 1.4 vs 2) · derniere reponse il y a 2 s`,
`HTTP 401 (unauthorized) · derniere reponse il y a 4 s`,
`HTTP 500 · derniere reponse il y a 2 s`,
`HTTP 404 : pas une reponse du tableau de bord`,
`POST /api/machine/heartbeat: ConnectTimeout('') · derniere reponse il y a 31 s`
(en minutes au-delà de 100 s : `il y a 3 min`). Sans clé elle dit
`aucune cle de machine (MACHINE_API_KEY) : rien n'est echange`. Le motif tient sur une
ligne de 160 caractères au plus, écrite comme du texte. De ce que le tableau de bord
envoie, seuls y entrent le code HTTP, son code stable et les majeures qu'il dit servir
(les quatre premières, puis le nombre des autres) ; sa phrase reste dans le journal de
la console. L'âge est celui de la dernière réponse reconnue comme venant du tableau de
bord, question d'arrêt comprise.

Depuis le contrat 1.3, le heartbeat doit annoncer `server_contract_version`.
Une réponse `{}` en 200 ne donne jamais `joignable`, même lorsque les programmes
sont désactivés : la console indique un contrat serveur inconnu. Une version
incompatible annoncée par le heartbeat reste affichée jusqu'à un heartbeat
compatible ; une interrogation réussie ne l'efface pas.

Si le heartbeat échoue tandis que d'autres échanges réussissent, le motif
`heartbeat : …` reste visible sous la pastille jusqu'au prochain heartbeat
compatible. `joignable` peut donc coexister avec ce motif : les autres échanges
aboutissent, mais le dernier heartbeat n'a pas abouti.

Deux règles suffisent pour la lire :

- **L'état est ce qu'ont dit en dernier les routes qui portent le heartbeat et la
  séance**, tant qu'elles le redisent. Ce sont elles que le tableau de bord ne sert qu'à
  une console dont il accepte la clé et sert le contrat. La dernière chose dite
  l'emporte : un 426 après un 401 se lit `incompatible`, puisque le tableau de bord
  vérifie la clé avant le contrat.
- **Ce qui n'est pas la réponse du tableau de bord ne prouve rien.** Un silence, une
  erreur du serveur, un 404 ou un 400 sans code stable, une page HTML : rien de cela ne
  donne `joignable` ni n'efface un refus. Cela ne change la pastille que lorsque ces
  routes n'ont plus rien dit de reconnaissable depuis 25 s.

Ce qui en découle :

- **Elle ne clignote pas sur une requête lente.** 25 s valent deux heartbeats et demi
  (un toutes les 10 s) : un heartbeat perdu conserve la couleur mais affiche son
  motif d'échec ; deux pertes de suite sans autre succès changent l'état. Pendant
  ces 25 s la pastille garde son état et c'est l'âge de la dernière réponse, sur la
  ligne du dessous, qui grandit. Le retour, lui, est immédiat : la première réponse qui
  prend ce que la console envoie la remet à `joignable`.
- **La question d'arrêt ne prouve rien et ne maintient rien.** Le tableau de bord y
  répond sous n'importe quel contrat, toutes les 3 s pendant une séance, pour qu'un
  arrêt passe toujours (`/api/machine/training/status`). Ses réponses gardent fraîche
  l'âge de la dernière réponse, et c'est tout : elles ne donnent pas `joignable`,
  n'effacent pas `incompatible`, et ne remettent pas le délai de 25 s à zéro. Une séance
  dont le heartbeat et les envois échouent se lit donc `en erreur` au bout de 25 s, avec
  le motif, même si la question d'arrêt reçoit toujours sa réponse.
- **Une incompatibilité lue dans une annonce ne se lève qu'à l'annonce suivante.**
  Quand c'est la console qui a refusé la version annoncée par le tableau de bord
  (`/api/machine/training/poll`), seule une annonce de sa propre majeure lève l'état.
  La console ne pose cette question qu'au repos et avec `PROGRAMS_ENABLED=true` :
  pendant une séance, la pastille garde donc ce que la dernière annonce a dit.
- **Un refus qui n'est plus redit ne dure pas.** 25 s après le dernier 426 ou le dernier
  401, si ces routes ne disent plus rien, la pastille passe à `injoignable`, ou à
  `en erreur` si quelque chose du tableau de bord répond encore.
- **`joignable` ne dit pas que tout est arrivé.** Il dit que le tableau de bord prend
  ce que la console envoie en ce moment. Ce qu'il reste à lui envoyer d'une séance n'est
  pas montré sur la page.
- **Aucune requête n'est faite pour elle.** L'état vient des échanges que le lien fait
  de toute façon (heartbeat, lancements, envoi des séances, question d'arrêt). Rien
  n'en est lu ni écrit dans le tic de commande.
- Comme **Console**, elle est barrée quand `/api/panel` ne répond plus depuis 3,5 s :
  la page ne sait alors plus ce que la console dirait.

**Version** affiche la version du logiciel de la console : le contenu de
`raspberry-pi/VERSION` (`/app/VERSION` dans l'image Docker), lu une fois au démarrage,
par exemple `pi-0.0.0-dev`. C'est la même valeur que celle du heartbeat et du
manifeste de chaque enregistrement de séance (`software_version`). Fichier absent,
illisible ou mal formé : `pi-unknown`.

Vérifié par `raspberry-pi/tests/test_link_state.py` (chaque état, les 25 s, l'absence
de clignotement), `raspberry-pi/tests/test_link_indicator.py` (le lien réel face à un
tableau de bord scripté, puis la console en simulation à travers son transport HTTP :
un tableau de bord qui répond 426 au milieu d'une séance, une adresse qui n'est pas le
tableau de bord, une séance dont rien n'arrive alors que la question d'arrêt est
servie) et `raspberry-pi/tests/web/panel_dashboard_link.test.mjs` (la page). Pas
rejoué dans un navigateur, ni face à un déploiement Convex réel.

### Pastille Service : en service ou hors service

Une console qui redémarre trois fois en dix minutes après un arrêt non propre (elle est
morte, elle a levé une exception, sa boucle s'est figée) revient **hors service** : elle
tourne, lit le variateur, arrête et verrouille celui qu'elle trouve en marche, mais
n'arme aucune séance
([raspberry-pi.md](raspberry-pi.md#36-sous-systemd--signe-de-vie-redémarrage-hors-service)).
C'est un état que la console garde d'un redémarrage à l'autre, pas un événement : une
page ouverte une heure après le lit tout de suite.

| Ce que dit la console | Pastille **Service** | Bandeau | Formulaire de la page Securite |
|---|---|---|---|
| en service | `en service`, vert | aucun | caché |
| hors service | `HORS SERVICE`, rouge | rouge, sur toutes les pages : `CONSOLE HORS SERVICE`, puis `3 demarrages apres un arret non propre en moins de 10 min : aucun depart n'est accepte. Faire verifier la machine, puis la remettre en service (page Securite), par un operateur nomme` | visible ([section 11](#remise-en-service-dune-console-hors-service)) |

Le bandeau a sa place dans la pile des bandeaux : sous ceux d'un arrêt (`ARRET D'URGENCE
NON CONFIRME`, `STOP NON CONFIRME`, `ARRET D'URGENCE VERROUILLE`), au-dessus du bandeau
orange. Une console qui redémarre est aussi ce qui laisse à l'écran le bandeau d'un arrêt
verrouillé avant le redémarrage (section 5) : les deux peuvent donc être affichés
ensemble. Aucun ne cache l'autre et aucun ne décide de l'autre : la page fait de la place
aux deux, la remise en service ne fait pas tomber le bandeau d'un arrêt, et un arrêt
acquitté laisse la console dite hors service.

Sous les pastilles, une ligne dit ce que le dernier démarrage a trouvé. Elle est vide
quand il n'y a rien à dire.

| Texte | Sens |
|---|---|
| `demarre apres un arret non propre` | le processus précédent n'a pas fini par un arrêt propre : il est mort, il est sorti en erreur, ou sa boucle de contrôle était restée plus de 3 s sans terminer de tic |
| `(enregistrement <nom> non clos)`, à la suite | la séance coupée a laissé son enregistrement ouvert ; dit par un seul démarrage |
| `N sur 3 en 10 min avant la mise hors service` | le compte des démarrages après un arrêt non propre |
| `etat des redemarrages non enregistre sur le disque` | le fichier d'état n'a pas pu être écrit : ce qui est affiché vaut jusqu'au prochain redémarrage |
| `chien de garde : N notification(s) non envoyee(s)` | la console est surveillée par systemd et N de ses signes de vie n'ont pas pu partir. Rien n'a été attendu ; si cela dure, systemd relance la console |

Pendant qu'une console hors service tourne, tout le reste de la page fonctionne : les
mesures, STOP, E-STOP, l'acquittement, l'attestation. Seuls les départs sont refusés,
avec la phrase de la [section 13](#refus-de-la-boucle-événement-refused). Comme
**Console**, la pastille est barrée quand `/api/panel` ne répond plus depuis 3,5 s.

Vérifié par `raspberry-pi/tests/test_out_of_service_console.py` (la vraie console en
simulation, par ses routes), `raspberry-pi/tests/web/panel_service.test.mjs` (la page) et
`raspberry-pi/tests/web/panel_display.test.mjs` (les deux bandeaux ensemble, leur ordre,
la place que la page leur fait). Pas rejoué dans un navigateur.

### Ligne horloge : l'heure de la machine

Sous les pastilles, la ligne `horloge` dit si l'horloge murale de la machine est tenue
par un service de temps. Cette horloge ne fait que **dater** : les enregistrements de
séance, le journal. Aucune durée, aucune règle de sécurité n'en dépend
([raspberry-pi.md](raspberry-pi.md#124-lhorloge-murale-et-le-journal-de-la-console)).
Un Raspberry Pi n'a pas d'horloge sauvegardée : après une coupure de courant il
redémarre à l'heure de son dernier arrêt, jusqu'au retour du réseau.

| Ce qui est écrit | Couleur | Quand | Que faire |
|---|---|---|---|
| `horloge synchronisee · erreur estimee 0.4 ms, erreur au plus 45.0 ms` | celle du texte | un service de temps tient l'horloge ; les deux nombres sont l'erreur qu'il estime et celle qu'il garantit | rien |
| `horloge NON FIABLE · aucune source de temps jointe : l'heure peut etre fausse de la duree pendant laquelle le Pi etait eteint` | orange | la machine n'a joint aucune source de temps depuis son allumage, ou plus depuis longtemps | vérifier le réseau du Pi, puis attendre : la ligne change d'elle-même, en dix secondes au plus après la synchronisation |
| `horloge NON VERIFIEE · etat de l'horloge illisible (...)` | orange | sur un Pi, la console n'a pas pu lire l'état de l'horloge | lire le journal de la console |
| (ligne vide) | | la console ne tourne pas sous Linux (le Mac du banc et des développeurs) : elle ne juge pas son horloge, ne prévient de rien et ne refuse rien à ce titre | rien |

Tant que la ligne est orange : une séance de **banc** démarre, avec un événement
`horloge murale non fiable (...)` dans la liste, et le manifeste de son enregistrement
porte `wall_trusted: false` ; un départ **avec une personne à bord** est refusé
(`demarrage refuse : horloge murale non fiable (...)`). La console lit l'état toutes
les 10 secondes ; comme **Console** et **Serveur**, la ligne est barrée quand
`/api/panel` ne répond plus depuis 3,5 s.

Vérifié par `raspberry-pi/tests/test_wall_clock_console.py` (la console en simulation,
avec un état d'horloge que le test décide) et
`raspberry-pi/tests/web/panel_wall_clock.test.mjs` (la page). Pas rejoué dans un
navigateur, ni sur un Pi.

### Navigation

`Tableau de bord`, `Capteurs` (avec, en dessous, une entrée par capteur acquis : nom,
canal `A1`…`A6` et une pastille de couleur selon la qualité), `Seance`, `Configuration`,
`Securite`. Changer de page ne recharge rien : aucune navigation ne peut figer un nombre.
Le titre de l'onglet suit la page affichée : `Tableau de bord - AnHeart`,
`Securite - AnHeart`, et le titre de la page pour celle d'un capteur
(`RESP · Respiration - AnHeart` ; jusqu'au 10 octobre 2026, le type seul :
`RESP - AnHeart`).

---

## 5. Pied de page : STOP et E-STOP

Présent sur **toutes** les pages. Tout à gauche, la version des paramètres médicaux
que la console applique, lue au démarrage : `parametres medicaux 0.1.0`, ou en rouge
`parametres medicaux 0.1.0 NON SIGNES` quand leur signature ne s'est pas vérifiée
(mode banc seulement, voir le bandeau du même nom) ; `parametres medicaux -` tant que la
console ne l'a pas dite. Puis un résumé : `liaison ouverte · manuel · hold`
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
- **Un STOP donné tard dans un programme ne laisse plus de verdict à acquitter.** Un
  STOP rouvre une récupération complète (5 minutes avec le profil standard), même si le
  programme était déjà dans sa propre phase `recovery`, bras arrêté : `REPOS` revient
  donc 5 minutes après le STOP, et plus tard que la durée prévue pour un STOP donné
  dans les cinq dernières minutes. Jusqu'au 8 octobre 2026, la règle `session_overrun`
  se verrouillait pendant cette récupération dès qu'elle dépassait la durée prévue de
  plus de 30 s (tout STOP des quatre dernières minutes et demie du profil standard,
  verdict à 1830 s), sans que rien se soit mal passé. Elle juge maintenant cette fin de
  séance sur sa propre échéance (section 11) : aucun verdict, rien à acquitter. Mesuré
  sur le banc d'essai logiciel avec le profil standard (STOP à 1531 s, 1600 s et
  1799 s), et vérifié par l'API sur la console en simulation avec un programme court
  (`raspberry-pi/tests/test_session_overrun_console.py`) ; pas rejoué dans un
  navigateur. Un arrêt demandé depuis le site passe par ce même STOP. Un E-STOP ou un
  verdict d'arrêt donnés aussi tard rouvrent la même récupération : il reste à
  acquitter l'arrêt d'urgence ou le verdict qui a terminé la séance, et lui seul.
- Refus : une boîte d'alerte `STOP refuse : …` (par exemple
  `the machine is already stopping` si un arrêt est déjà en cours, ou
  `there is no session to end (the machine is idle)`). Un refus est une réponse de la
  console (code 4xx) : il ne laisse aucun bandeau.
- **Une demande STOP restée sans réponse est dite**, comme celle d'un E-STOP (plus
  bas). Deux secondes après le plus ancien clic encore sans réponse, un bandeau rouge
  s'affiche : `STOP NON CONFIRME` suivi de
  `aucune reponse de la console depuis N s - rien ne dit que la seance s'arrete : E-STOP, ou l'arret cable`.
  La requête n'est jamais retirée. Le bandeau se baisse sur la réponse de la console
  (acceptation ou refus), sur une image qui montre le mode `ARRET`, ou sur une réponse
  de statut demandée après le clic qui montre l'état `stopping`, ou l'état `idle` sans
  aucune demande en attente (`pending` vide). Ne le baissent pas : une image au mode
  `REPOS` (un démarrage peut attendre derrière), une réponse de statut demandée avant
  le clic, ni un statut `idle` dont `pending` porte encore une demande (un reset au
  repos, par exemple ; depuis le 11 octobre 2026 l'état ne dit plus `idle` pendant
  qu'un démarrage accepté attend, il dit `starting`).
- Si la requête STOP échoue sans réponse de la console (console injoignable), ou sur
  une erreur de la console elle-même (code 5xx), le bandeau dit tout de suite
  `la demande a echoue il y a N s et n'est plus en attente : … - rien ne dit que la seance s'arrete : E-STOP, ou l'arret cable`,
  et la boîte d'alerte dit
  `la demande d'arret (STOP) a echoue : … - rien ne dit que la seance s'arrete : E-STOP, ou l'arret cable`.
  Jusqu'au 10 octobre 2026 l'alerte disait `STOP refuse : Failed to fetch` et rien ne
  restait à l'écran. La page ne renvoie pas la demande ; le bandeau reste jusqu'à l'une
  des preuves ci-dessus.
  Rejoué dans un navigateur : console suspendue en séance manuelle, bandeau affiché
  2,0 s après le clic et baissé à la reprise de la console ; requête coupée, bandeau
  affiché tout de suite, baissé 0,2 s après un STOP donné depuis un autre écran.

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
  (`estop_latched`, `supervisor_estop`, verdict retenu `quick_stop` verrouillé, ou
  `silenced`, le `quick_stop` verrouillé que `go_silent` a remplacé). Il
  apparaît donc aussi sur un écran qui n'a pas cliqué, après un arrêt posé par la
  caméra, et pour le `quick_stop` verrouillé d'une règle de sécurité
  (`reverse_rotation` par exemple ; la ligne `e-stop verrouille` dit alors `non`) ou
  du runtime lui-même (`drive_precommanded`, `enable_unconfirmed`). Depuis le
  11 octobre 2026 `/api/status` porte le verdict en vigueur quel qu'en soit l'auteur
  (section 11) : un écran sans images lève donc aussi le bandeau pour un arrêt que le
  runtime a verrouillé de lui-même. Rejoué dans un navigateur, WebSocket de l'écran
  tombé, variateur simulé mis en marche par un autre maître pendant que la console est
  au repos : la console verrouille `drive_precommanded / quick_stop` ; le bandeau,
  absent pendant les 12 s observées avant ce changement, apparaît 2,9 s après le
  verrou (au tour de statut suivant, 5 s au plus), et les lignes `verdict retenu` et
  `plancher verrouille` passent de `aucun` à `drive_precommanded / quick_stop`.
- Il ne se baisse que sur une donnée qui montre l'arrêt levé et que rien de plus récent
  ne contredit ; dans le doute il reste, sur toutes les pages. « Plus récent » se juge
  à l'horloge de la machine, que portent les images (`at`), le reçu du clic (`at`) et,
  depuis le 10 octobre 2026, chaque réponse de `/api/status` (`at`) :
  - une image qui montre un arrêt verrouillé lève le bandeau, quoi que dise le statut ;
  - quand les images montrent l'arrêt levé, le bandeau suit la plus récente des deux,
    l'image ou la réponse de statut. Une réponse de statut qui dit « verrouillé » mais
    a été écrite avant l'image affichée ne fait donc plus revenir le bandeau (elle a
    été demandée avant un acquittement et arrive après lui) ; le statut est alors
    redemandé tout de suite, pour que la ligne `e-stop verrouille` et la pastille
    **Etat** ne restent pas en retard jusqu'au tour suivant ;
  - de deux réponses de statut, la page garde celle que la machine a écrite en dernier,
    quel que soit l'ordre des demandes et l'ordre d'arrivée. Deux réponses qui portent
    exactement le même `at`, ou une réponse sans `at`, sont appliquées dans l'ordre où
    elles arrivent ; à `at` égal entre une image qui montre l'arrêt levé et un statut
    qui le dit verrouillé, le bandeau est affiché.
- Sous `go_silent`, les images ne peuvent rien dire de l'arrêt d'urgence : ce verdict
  prend sa place comme verdict retenu, et refuse tout acquittement. Le statut, lui, le
  peut : `/api/status` porte `supervisor_estop`, l'arrêt d'urgence que le superviseur
  tient verrouillé, quel qu'en soit l'auteur (bouton de la page, autre écran, caméra),
  et qui reste lisible quand `go_silent` a pris la place du verdict retenu et du
  plancher. Sous `go_silent`, que la page le lise dans les images ou, faute d'images,
  dans le statut, seule une réponse de statut peut baisser le bandeau, et la règle est
  la même quel que soit ce qui l'avait levé, images ou statut. Quand les images sont
  passées à `go_silent`, la réponse doit en plus avoir été écrite après ce passage.
  Cette réponse :
  - lève ou garde le bandeau si un arrêt d'urgence est verrouillé derrière
    `go_silent`, aussi sur un écran ouvert ou rechargé après coup. La ligne
    `e-stop verrouille` dit alors `OUI` et la pastille **Etat** est rouge. Ce bandeau
    reste jusqu'au redémarrage de la console ;
  - le baisse si le dernier verrou montré à la page, par les images ou par le statut,
    était l'arrêt d'urgence lui-même (règle `operator_estop`) et qu'il n'est plus
    tenu : il a été acquitté juste avant `go_silent`, ou la console a été relancée (un
    bandeau affiché avant le redémarrage ne reste plus jusqu'au rechargement de la
    page) ;
  - le lève ou le garde de même pour le `quick_stop` verrouillé d'une règle
    (`reverse_rotation` par exemple) ou du runtime que `go_silent` a remplacé dans
    cette console. `go_silent` prend la place de ce verdict : `standing` et `floor`
    ne montrent plus que lui, personne n'a rien acquitté et plus rien ne s'acquitte.
    Depuis le 11 octobre 2026 la console dit ce qu'il a remplacé (`silenced`,
    section 14) : la réponse de statut le montre, la ligne
    `verrouille derriere go_silent` de la page Securite le nomme, et un écran ouvert
    ou rechargé à ce moment affiche le bandeau comme celui qui a vu la séquence.
    Jusque-là cet écran n'affichait aucun bandeau. Rejoué dans un navigateur
    (`reverse_rotation` puis liaison au variateur perdue, page ouverte ensuite) ;
  - **ne le baisse pas** sur une réponse sous `go_silent` qui ne montre rien derrière
    lui, quand le dernier verrou montré à la page, par les images ou par le statut,
    était le `quick_stop` d'une règle ou du runtime et que c'est la même console qui
    répond. Une telle réponse ne peut vouloir dire qu'un acquittement que cet écran
    n'a pas vu, donné juste avant `go_silent` : le bandeau reste, avec la pastille
    **Securite** `go_silent` en rouge, jusqu'au redémarrage de la console.

  Pour ce dernier cas, la page ne baisse le bandeau sous `go_silent` que si un
  redémarrage de la console est prouvé depuis que le verrou lui a été montré. Depuis
  le 11 octobre 2026 cette preuve est certaine : chaque image et chaque réponse de
  statut portent le nom que la console se tire au hasard à son démarrage (`instance`,
  32 chiffres hexadécimaux, le même tant que le processus vit, un autre après tout
  redémarrage), et la page retient ce nom avec le verrou. Une réponse de statut
  demandée après que le verrou a été montré, et qui porte un autre nom, vient d'une
  console lancée depuis, qui ne tient rien de ce que tenait la précédente : le bandeau
  se baisse à cette réponse. Une réponse demandée avant ne prouve rien, quel que soit
  son nom, et un nom absent d'un côté ou de l'autre non plus.

  Deux preuves plus anciennes restent pour une réponse qui ne porterait pas de nom :
  l'horloge de la machine est repartie de zéro, ou le nombre d'instantanés publiés par
  la console (troisième valeur de `counters`, qui ne fait que monter tant qu'elle
  tourne) a baissé. Pour un verrou montré par les images, retenu avec le WebSocket qui
  l'a porté (un WebSocket, une console) : entre deux réponses de statut demandées après
  que ce WebSocket l'a montré. Pour un verrou montré par le statut : entre la réponse
  qui l'a montré, écrite par la console qui le tenait, et une réponse demandée après
  elle. Deux comptes égaux ne prouvent rien (deux réponses dans le même cycle, ou une
  boucle arrêtée). Une liaison coupée puis revenue ne prouve rien non plus, et un
  verrou montré de nouveau, sur un nouveau WebSocket ou par une réponse de statut
  suivante, est celui de la console qui le montre : la preuve repart de là.

  Ces preuves supposent une seule console derrière l'adresse que la page interroge.
  C'est le cas tel que la console est lancée, un processus par adresse. Si un
  intermédiaire répartissait un jour cette adresse entre deux consoles, la réponse de
  l'autre se lirait comme un redémarrage : elles ne tiendraient plus.

  Les deux cas que le compte laissait sans preuve sont ainsi réglés : une console
  arrêtée avant d'avoir répondu à un statut après le verrou, et une nouvelle console
  qui a déjà publié plus d'instantanés que l'ancienne. Rejoués dans un navigateur, le
  bandeau de l'ancienne console restait jusqu'au rechargement de la page ; il se baisse
  à la première réponse de statut de la nouvelle.

  Une réponse plus ancienne que le passage des images à `go_silent` peut lever le
  bandeau, jamais le baisser ; le statut est redemandé tout de suite quand les images
  passent à `go_silent`.
- Sans images (WebSocket de l'écran tombé alors que ses requêtes passent encore, ou
  aucune image reçue), le bandeau suit le statut : une réponse qui montre un arrêt
  verrouillé le lève, et une réponse suivante qui n'en montre plus, hors `go_silent`,
  le baisse (seuls un acquittement ou un redémarrage de la console retirent un
  `quick_stop` verrouillé sans `go_silent`). Sous `go_silent` la règle ci-dessus
  s'applique telle quelle : le bandeau levé par le statut pour le `quick_stop` d'une
  règle reste, à côté de `NO LIVE DATA`, tant que la même console répond.
- Ce qui reste du côté prudent : si le WebSocket tombe sur une image qui montrait un
  arrêt verrouillé, le bandeau reste à côté de `NO LIVE DATA` jusqu'à la prochaine
  image, même si le statut dit entre-temps l'arrêt levé ; sous `go_silent`, un arrêt
  d'urgence posé depuis un autre écran n'apparaît ici qu'au tour de statut suivant
  (5 s au plus) ; et le `quick_stop` d'une règle acquitté pendant que cet écran ne
  recevait plus d'images, puis suivi de `go_silent` dans cette même console, garde son
  bandeau jusqu'au rechargement de la page (la console répond alors « rien derrière
  `go_silent` », ce que la page ne prend pas seule pour un arrêt levé).
- Si l'horloge de la machine repart de zéro (redémarrage du Pi), la page le voit : une
  image, ou une réponse demandée après celle qu'elle affiche, porte une heure plus
  ancienne. Ce qui arrive après ce redémarrage est alors tenu pour plus récent que ce
  qui est arrivé avant, quelles que soient les deux heures.
- Ce que l'E-STOP web **n'est pas** : un arrêt de sécurité. Il dépend du navigateur,
  du réseau, du serveur web et du processus. Le STO est ponté sur cette machine : même
  l'arrêt d'urgence le plus rapide est une rampe (voir [securite.md](securite.md)).
  L'arrêt de sécurité est le coup de poing câblé.
- **Une demande restée sans réponse est dite.** Si la console est bloquée ou le réseau
  muet, la requête reste en attente. Deux secondes après le premier clic resté sans
  réponse, un bandeau rouge propre à ce cas s'affiche :
  `ARRET D'URGENCE NON CONFIRME` suivi de
  `aucune reponse de la console depuis N s - UTILISEZ L'ARRET CABLE`. Le compte court
  depuis le plus ancien clic encore sans réponse, pas depuis le dernier. La requête
  n'est jamais retirée : si elle finit par passer, sa réponse baisse ce bandeau et lève
  `ARRET D'URGENCE VERROUILLE`. Une image arrivée après le clic, ou une réponse de
  statut demandée après le clic, qui montre un arrêt verrouillé le baisse aussi. Une
  image sous `go_silent` ne le baisse pas, ni une réponse de statut demandée avant le
  clic.
  Rejoué dans un navigateur, console suspendue (processus arrêté par un signal, ses
  connexions ouvertes) : bandeau affiché 2,07 s après le clic, puis remplacé par
  `ARRET D'URGENCE VERROUILLE` moins de 0,1 s après la reprise de la console.
- Si la requête échoue : le même bandeau dit tout de suite
  `la demande a echoue il y a N s et n'est plus en attente : … - UTILISEZ L'ARRET CABLE` ;
  la boîte d'alerte `la demande d'arret d'urgence a echoue : … - UTILISEZ L'ARRET CABLE`
  s'ouvre comme avant. Une demande échouée est terminée : la page ne la renvoie pas, et
  l'âge de l'échec (`N s`, puis `N min`) est tenu à jour. Le bandeau reste jusqu'à un
  E-STOP qui aboutit, un arrêt vu verrouillé (image, ou statut demandé après le clic) ou
  un rechargement de la page. Un nouveau clic resté sans réponse change tout de suite
  le texte en
  `nouvelle demande sans reponse depuis N s, la precedente a echoue : … - UTILISEZ L'ARRET CABLE`.
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

**Sous un `reduce`, la consigne descend vers 0 quelle que soit la cible.** La console
annonçait alors une rampe vers la cible, avec le temps qu'il faudrait pour y remonter
(`vers 6.03 tr/min de sortie …, arrivee dans ~0:03`) alors que le bras s'en éloignait.
Depuis le 11 octobre 2026 l'instantané dit où va la consigne (`manual.ramp_to`,
section 14) et `ramp_eta_s` est le temps pour y arriver. Le bandeau reste affiché,
puisque la vitesse change, et dit
`la vitesse baisse vers 0.00 tr/min de sortie sous l'avertissement <règle>, la cible n'est pas suivie ; arrivee dans ~m:ss s'il dure`.
Si l'avertissement se lève avant l'arrêt, la consigne remonte vers la cible (bandeau
`REPRISE AUTOMATIQUE POSSIBLE`, section 11) ; si elle arrive à 0, la séance se termine
(`session_standstill`).

### Carte « Mode MANUEL »

**Au repos** (pas de séance manuelle) :

| Élément | Rôle |
|---|---|
| case `BANC - personne a bord : NON (moteur decouple, ou bras couple avec la capsule VIDE)` | déclaration obligatoire avant tout démarrage ; la page refuse de démarrer si elle n'est pas cochée |
| champ `Operateur` | nom de la personne qui pilote ; obligatoire (400 sinon) |
| bouton `Demarrer MANUEL` | `POST /api/manual/start` avec `occupancy: "bench"` ; la cible initiale est 0 |

La page ne propose **que** la déclaration BANC, qui ne nomme aucun passager. Une séance
manuelle « personne à bord » (`occupied`) n'existe que par l'API, et elle est refusée
tant que `OCCUPANCY_OCCUPIED_ENABLED=false` (jalon M6) ou que les paramètres médicaux ne
sont pas signés (mode banc). Depuis ANH-84 elle nomme en plus
un passager de la liste du tableau de bord (`subject_id`, section 9), vérifié par la
route puis par la boucle à l'armement ; l'enregistrement et la séance déclarée au
tableau de bord portent son identifiant. L'âge minimum (`min_rider_age` du fichier
médical signé, 18 ans) s'y applique
comme à une séance programmée (décision du responsable produit, 15 octobre 2026),
compté de la même façon depuis l'année de naissance réglée sur le site : sans année de
naissance, la route répond 409 ; un passager trop jeune est refusé par la boucle
(`demarrage refuse : passager de <n> ans, minimum <m> ans (min_rider_age des parametres
medicaux)`). La FC max
n'est pas demandée : rien n'y est ajusté en manuel.

Pastille à côté du titre : `inactif`, `demarrage`, puis le libellé de l'occupation
(`BANC - personne a bord : NON`).

**Pendant la séance manuelle** :

| Élément | Rôle |
|---|---|
| `− palier` / `+ palier` | baisse ou monte le **brouillon** d'un cran de 0,1 Gr (g résultant) au rayon configuré |
| `−1 tr/min` / `+1 tr/min` | baisse ou monte le brouillon de 1 tr/min **de sortie** (bras) |
| grand nombre central | le brouillon, en tr/min de sortie. En **orange** tant qu'il diffère de la cible appliquée |
| `Appliquer` | envoie le brouillon : `POST /api/manual/target`. Rien n'est envoyé avant ce clic |
| grille | `cible appliquee`, `plafond`, `minimum de rotation` (chacun en tr/min sortie, moteur, Hz, Gc, Gr), `rampe` (`en cours, arrivee ~m:ss`, `cible atteinte`, `consigne maintenue, cible non atteinte` quand un `freeze` tient la consigne à distance d'une cible non nulle, ou `en baisse vers 0.00 tr/min de sortie sous l'avertissement <règle>, la cible n'est pas suivie ; arrivee dans ~m:ss s'il dure` quand un `reduce` la fait descendre) |
| encadré orange `MONTEE RETENUE PAR LA FREQUENCE CARDIAQUE` | au-dessus de la cible, seulement avec une personne à bord : la fréquence cardiaque retient toute montée (voir plus bas) |

Règles de la cible :

- La cible est une **destination**, jamais une consigne directe. La machine y marche aux
  limites anti-nausée (accélération angulaire et variation de g, `motion_limits`
  du fichier médical signé `config/medical_parameters.json`). Tout verdict de
  sécurité reste prioritaire.
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
  personne à bord, une fréquence cardiaque inutilisable (aucune lecture acceptée par
  le suiveur, ou dernière acceptation âgée de plus de 2,5 s), de tendance inconnue
  (moins de 5 lectures) ou en baisse de plus de
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
  événements `refused` : ils sont listés dans **Evenements** (page Seance). Le refus
  est écrit, en rouge et mot pour mot, dans la note sous la carte de l'écran qui a
  envoyé la cible ; la remise à 0, que personne n'a demandée, l'est sur tous les
  écrans qui montrent la séance (voir ci-dessous).
  `cible appliquee` reste ou revient à `0.00`, le brouillon reste affiché en orange, et
  `rampe` dit `cible atteinte`.

**La note sous un bouton dit ce que la console a répondu à la demande de cet écran.**
`Demarrer MANUEL`, `Appliquer`, `Demarrer la seance` (section 9) et
`Reset defaut variateur` reçoivent une réponse 202 dès que la boîte aux lettres de la
console a pris la demande ; la boucle la juge au cycle suivant. Depuis le
11 octobre 2026 la console donne à chaque demande qu'elle accepte un **identifiant**
(`request_id`, rendu dans le 202), et dit elle-même ce que la demande est devenue,
une fois, par un événement qui porte cet identifiant :

| Événement | Sens |
|---|---|
| `taken` | la boucle a pris la demande dans la boîte et l'a exécutée : séance armée, cible tenue, reset écrit au variateur, arrêt engagé |
| `refused` | la boucle a pris la demande et l'a refusée ; le détail est sa raison (section 13) |
| `cancelled` | un STOP a pris la place de la demande dans la boîte avant que la boucle la voie : `un STOP est arrive avant que la machine prenne cette demande, rien n'a ete fait` |

La page reconnaît à cet identifiant les réponses faites à ses propres demandes, et
n'écrit sous un bouton que celles-là. Elle ne déduit plus rien d'une image : une
séance à l'écran, ou une cible appliquée égale à celle envoyée, ne prouve pas que la
boucle a pris la demande de **cet** écran, et la page ne dit pas `pris` pour cela.
La réponse faite à un autre écran porte un autre identifiant : elle est listée dans
**Evenements** et n'est écrite sous aucun bouton de cet écran. Une seule réponse ne
porte aucun identifiant, la cible manuelle que la machine remet d'elle-même à 0 :
personne ne l'a demandée, et tout écran qui montre la séance manuelle l'écrit sous la
carte.

Une réponse peut arriver à l'écran avant le 202 de la demande à laquelle elle
répond : la page garde les 32 dernières réponses de la console et les relit au 202.

La note d'une cible :

| Note | Quand | Sens |
|---|---|---|
| `cible envoyee : 5.10 output rpm - pas encore prise par la machine` | à la réponse 202 | la console a reçu la demande ; la boucle n'a rien dit encore |
| `cible prise par la machine : 5.10 output rpm`, puis `cible prise par la machine : 5.10 output rpm (suivie aux limites de mouvement)` | à l'événement `taken` de cette demande ; la parenthèse à la première image prise après cette réponse | la machine tient cette cible et y mène la consigne. La note est effacée dès qu'une image prise après la réponse montre que la machine ne tient plus la cible (STOP, verdict, cible d'un autre écran) |
| `cible prise par la machine : 5.10 output rpm` (sans la parenthèse) | image après image tant que cela dure | la machine tient cette cible mais ne la suit pas à cet instant : un verdict tient ou baisse la consigne (un `freeze` avec une cible non nulle, un `reduce`), ou, sans verdict, la fréquence cardiaque retient une montée vers une cible plus haute (encadré ci-dessous). Sous un `freeze`, une cible de 0 est un arrêt demandé : elle est suivie, et la parenthèse est écrite. La parenthèse revient dès que plus rien ne retient la consigne |
| `refus de la machine (<heure>) : <message>` (rouge) | à l'événement `refused` de cette demande | la boucle a refusé la cible de cet écran : le message est celui de la section 13 |
| `refus de la machine (<heure>) : cible de <n> tr/min moteur remise a 0 : …` (rouge) | à l'événement `refused` sans identifiant, séance manuelle à l'écran | la machine a repris d'elle-même une cible qu'elle tenait. Écrit sur tous les écrans qui montrent la séance, qu'ils aient cliqué ou non |
| `demande annulee (<heure>) : un STOP est arrive avant que la machine prenne cette demande, rien n'a ete fait` (rouge) | à l'événement `cancelled` de cette demande | un STOP, de cet écran ou d'un autre, a pris la place de la cible dans la boîte : la boucle ne l'a jamais vue |
| `cet ecran ne sait pas ce que la machine a fait de sa cible : sa liaison a ete coupee.` (rouge) | la réponse n'est pas arrivée à cet écran (voir « Liaison coupée » plus bas) | la page demande alors le sort de sa demande à la console, et la note devient ce sort |

Un cas reste dit par les images seules : un STOP demandé juste après qu'une cible a
été prise remet la cible à 0, et la note `cible prise par la machine` s'efface à
l'image qui le montre. Rejoué dans un navigateur sur la console en simulation, le
11 octobre 2026 : après le clic, `cible envoyee …` à 2 ms, puis `cible prise par la
machine … (suivie aux limites de mouvement)` à 173 ms ; pour une cible hors domaine,
`cible envoyee …` à 11 ms puis `refus de la machine …` à 80 ms.

**L'encadré `MONTEE RETENUE PAR LA FREQUENCE CARDIAQUE`.** Avec une personne à bord, la
fréquence cardiaque retient toute montée de la consigne sans qu'aucun verdict ne
tienne. L'encadré le dit **avant** qu'une cible soit tapée, avec la raison :

| Texte | Raison |
|---|---|
| `pas de frequence cardiaque utilisable` | aucune lecture acceptée par le suiveur, ou dernière acceptation âgée de plus de 2,5 s ; les 4 s restent la borne de l'affichage |
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
n'apparaît jamais. Rejoué dans un navigateur (rejeu antérieur à la porte de montée
de 2,5 s), personne à bord déclarée par l'API :
4,4 s après la perte de l'ECG, l'encadré dit `pas de frequence cardiaque utilisable`,
avant tout verdict ; une cible envoyée alors est refusée pour cette raison. Sur la
fréquence cardiaque simulée, la raison `baisse trop vite` s'allume et s'éteint d'une
seconde à l'autre : la garde est sensible ([securite.md](securite.md#76-aucune-cible-manuelle-nattend-sur-un-bras-à-larrêt-anh-178)).

**La note d'un démarrage manuel** suit la même règle (jusqu'au 10 octobre 2026 elle
disait `accepte : manual_start bench (cible 0)` dès la réponse 202 ; du 10 au
11 octobre elle déduisait `pris` des images et d'une règle de deux secondes, que
l'identifiant rend inutile) :

| Note | Quand | Sens |
|---|---|---|
| `demande envoyee : manual_start bench - pas encore prise par la machine` | à la réponse 202 | la console a mis la demande dans sa boîte ; la boucle ne l'a pas encore jugée |
| `demarrage pris par la machine : manual_start bench`, puis `demarrage pris par la machine : manual_start bench (cible 0)` | à l'événement `taken` de cette demande ; `(cible 0)` à la première image prise après | la boucle a armé la séance que cet écran a demandée. `(cible 0)` n'est écrit que tant que les images montrent une cible appliquée de 0 : la mention disparaît dès qu'un autre écran en applique une, et revient si la cible revient à 0. La note s'efface à la fin de cette séance : à l'événement `session_idle`, ou à une image prise après la réponse qui ne montre plus la séance manuelle |
| `refus de la machine (<heure>) : demarrage refuse : …` (rouge) | à l'événement `refused` de cette demande | la boucle a refusé le démarrage de cet écran ; le texte est sa raison |
| `demande annulee (<heure>) : un STOP est arrive avant que la machine prenne cette demande, rien n'a ete fait` (rouge) | à l'événement `cancelled` de cette demande | un STOP est arrivé pendant que le démarrage attendait encore dans la boîte : rien n'a été armé. Un nouveau démarrage est possible dès que **Etat** revient à `idle` (la machine affiche `ARRET` quelques secondes) |
| une phrase de la console, en rouge, par exemple `demande refusee par la console : un demarrage est deja demande, et la machine ne l'a pas encore pris ou est en train de l'armer. Rien n'a ete envoye.` | à une réponse 4xx | la console a refusé le clic tout de suite : rien n'est entré dans la boîte (voir « Refus immédiat » plus bas) |
| `cet ecran ne sait pas ce que la machine a fait de sa demande de demarrage : sa liaison a ete coupee.` (rouge) | la réponse n'est pas arrivée à cet écran | voir « Liaison coupée » ci-dessous |
| `la machine a pris le demarrage demande par cet ecran (manual_start bench), mais sa liaison a ete coupee depuis : cet ecran ne sait pas si la seance en cours est encore celle-la.` (rouge) | la note disait `pris`, puis la liaison de l'écran a été coupée et rétablie | voir « Liaison coupée » ci-dessous |

La règle est la même pour **Demarrer la seance** (section 9) et pour
**Reset defaut variateur** (carte Variateur).

**Liaison coupée : ce qu'un écran ne peut pas savoir seul, et comment il le
redemande.** La console ne renvoie pas à un écran les événements publiés pendant que
son WebSocket était coupé. Un écran dont la liaison tombe entre son clic et la
réponse de la boucle n'a donc pas reçu cette réponse. La page le sait dans deux cas :
à la première donnée (image ou événement) d'un WebSocket qui n'est pas celui qui
était ouvert au clic, et quand une image est prise plus d'une seconde de l'horloge
de la machine après la demande sans qu'aucune réponse soit arrivée (sur une même
liaison la réponse précède toujours une telle image). Elle écrit alors, en rouge,
qu'elle ne sait pas ce que la machine a fait de sa demande, et rien d'autre : rien
de ce que les images montrent. Et elle le demande à la console : `/api/status`
liste, dans `requests`, ce que sont devenues les 16 dernières demandes, par
identifiant, avec les mots de l'événement. Dès que la réponse arrive, la note dit
ce sort : `pris`, `refus de la machine`, ou `demande annulee`. Elle continue de
dire que l'écran ne sait pas tant que la console ne répond pas, ou si la console ne
connaît plus la demande (console relancée, ou plus de 16 demandes depuis).

**Après `pris`, la séance à l'écran est-elle encore celle de cet écran ?** Tant que
la liaison reste la même, oui : la fin de la séance arrive toujours comme événement
`session_idle`, même quand les images de l'arrêt n'ont pas été livrées à cet écran.
Après un changement de WebSocket la page ne peut plus l'affirmer : sa séance a pu
finir, et une autre commencer, sans qu'elle le voie. Elle remplace `pris` par la
phrase de la dernière ligne du tableau, et demande le statut : `session_request` y
est l'identifiant du démarrage dont vient la séance en cours (de son acceptation au
retour à `idle`). Si c'est le démarrage de cet écran, la note redit `demarrage pris
par la machine …` ; sinon elle s'efface. Seule compte une réponse de statut
demandée **après** la première donnée du nouveau WebSocket : une réponse plus
ancienne a pu être écrite avant une fin de séance que cet écran a manquée. Une image
qui ne montre plus la séance efface la note sans attendre la console.

Rejoué avec deux écrans réels sur la console en simulation, le 11 octobre 2026, avant
et après ce changement (variateur simulé ralenti hors dépôt, sans toucher à la
console) :

| Suite | Avant | Après |
|---|---|---|
| Deux clics sur **Demarrer MANUEL**, écran B 85 ms après écran A, lecture d'état du variateur simulé retardée de 70 ms | au troisième essai les deux reçoivent 202, un `session_idle` est publié entre les deux demandes, une séance tourne ; les deux écrans écrivent en rouge `… Cet ecran ne sait pas si la machine a pris sa demande : une autre demande de demarrage est aussi arrivee a la machine.`, 341 ms et 249 ms après leur clic | aux mêmes instants, A reçoit 202 et B reçoit 409 (`start_pending`, avec l'identifiant de la demande de A) ; l'écran B lit la phrase de la console 10 ms après son clic ; l'écran A lit `demarrage pris par la machine : manual_start bench` à 260 ms, puis `(cible 0)` à 329 ms ; aucun `session_idle` |
| Un reset, le démarrage de séance de A pendant l'écriture du reset, celui de B (45 min) pendant l'armement de A (lecture 40 ms, écriture 90 ms, limites 60 ms) | les trois reçoivent 202 ; `refused [ecran B] demarrage refuse : la machine est deja running` un cycle plus tard ; les deux écrans écrivent « ne sait pas », à 569 ms et 338 ms | reset 202, A 202, B 409 (`start_pending`) ; l'écran A lit `demarrage pris par la machine : start standard_30_min` à 530 ms ; l'écran B la phrase de la console à 10 ms ; événements : `taken` du reset, `taken` de A, `session_running` |
| **Demarrer MANUEL** sur l'écran A, STOP de l'écran B dès la réponse, avant la boucle | les deux reçoivent 202 ; l'écran A écrit `demarrage NON pris par la machine : la raison n'est pas arrivee a cet ecran.` 1,2 s après, et son démarrage suivant, sept secondes plus tard, dit « ne sait pas » | l'écran A lit `demande annulee (<heure>) : un STOP est arrive avant que la machine prenne cette demande, rien n'a ete fait` 14 ms après son clic ; son démarrage suivant dit `pris` à 240 ms |
| Un écran, WebSocket coupé (ses requêtes passent), **Demarrer MANUEL**, la boucle prend la demande, WebSocket rétabli | `une seance manuelle est en cours. Cet ecran ne sait pas si la machine a pris sa demande : sa liaison a ete coupee.`, jusqu'à la fin de la séance | au retour de la liaison, `demarrage pris par la machine : manual_start bench (cible 0)` : le statut a répondu entre deux relevés de la note, pris toutes les 10 ms |

**Ce que tient le test de la page.** Un modèle de la boîte de la console (une place,
un identifiant par demande, un démarrage refusé tout de suite tant qu'un autre
attend ou est armé, un STOP qui déplace ce qui attend, `idle` seulement quand plus
rien d'accepté n'attend) est joué pour toutes les suites de trois demandes au plus
(démarrages de trois écrans au plus, resets, cibles, arrêts) sur trois cycles, dans
chacune des trois positions d'un cycle où une demande peut arriver. Chaque histoire
d'écran est jouée sur le code réel de la page, et après **chaque** message la note
est comparée à ce que le modèle sait, sans passer par les identifiants : elle dit
`pris`, avec les mots de la demande de cet écran, si et seulement si la boucle a
pris cette demande et que rien de livré depuis ne montre sa séance finie ; `refus
de la machine` si et seulement si la boucle l'a refusée ; `demande annulee` si et
seulement si un STOP l'a déplacée ; la phrase de la console après un refus
immédiat ; et jamais « ne sait pas », la liaison étant restée ouverte. Les suites
sont jouées aussi avec une image non livrée, une image livrée après les événements
du cycle suivant (la console envoie d'abord les événements), une boucle qui refuse
un reset au repos, une boucle qui refuse les démarrages d'un écran pour une raison
à elle, et un quatrième cycle : 65 643 suites, 13 491 histoires d'écran distinctes,
dans deux fichiers que le lanceur joue côte à côte (environ 6 s en local).
`raspberry-pi/tests/web/request_family.mjs` porte le modèle et l'invariant.

**Refus immédiat : la console répond 4xx et rien n'entre dans la boîte.** La note
est alors la phrase de la console, telle quelle. Deux de ces phrases disent qu'une
autre demande est en travers ; la réponse porte un `code` stable et, dans
`waiting_request_id`, l'identifiant de cette autre demande, et rien d'autre d'elle
(ni le nom de l'autre opérateur, ni l'heure, ni l'âge du passager, que la console
écrivait dans cette réponse jusqu'au 11 octobre 2026) :

| `code` | Phrase | Quand |
|---|---|---|
| `start_pending` | `demande refusee par la console : un demarrage est deja demande, et la machine ne l'a pas encore pris ou est en train de l'armer. Rien n'a ete envoye.` | un démarrage, ou un reset, pendant qu'un démarrage accepté attend dans la boîte ou que la boucle l'arme |
| `request_waiting` | `demande refusee par la console : une autre demande attend encore la machine (une demande a la fois), rien n'a ete envoye. Reessayer dans un instant.` | une cible ou un reset pendant qu'une autre demande attend ; un démarrage au repos pendant qu'un reset attend |

Les autres codes sont en section 14. Un second clic sur **Appliquer** avant que la
boucle ait pris la première cible reçoit `request_waiting`, et la demande en travers
est la première cible de cet écran (son identifiant est celui que la page tient).
La note dit alors
`seconde demande refusee par la console : la cible … attend encore la machine (une demande a la fois). Reessayer dans un instant.`,
puis ce que devient la première cible. Si la demande en travers est celle d'un autre
écran, la note est la phrase de la console. Dans tous les cas la demande déjà
acceptée de cet écran reste suivie : sa réponse, quand elle arrive, remplace la note.

Avec une personne à bord, quand l'encadré de la fréquence cardiaque dit `INCONNUE`,
la note d'une cible plus haute que la consigne ne dit plus
`(suivie aux limites de mouvement)` : la page ne sait pas si la montée est retenue.

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
La table des 60 codes LFT est dans
[raspberry-pi.md](raspberry-pi.md#6-les-défauts-du-variateur-table-lft).

**Bandeau `VARIATEUR EN DEFAUT`.** Depuis le 11 octobre 2026, tant que le variateur
signale un défaut, un bandeau rouge le dit en haut de toutes les pages :
`VARIATEUR EN DEFAUT` suivi de
`<mnémonique> (LFT brut <code>) : aucun demarrage n'est accepte tant que le variateur signale ce defaut. Le traiter sur la carte Variateur (Tableau de bord)`
(`code inconnu (LFT brut <code>)` pour un code absent de la table,
`code non lu sur le variateur` quand le variateur dit être en défaut sans que le code
ait été lu). Il suit les images, cinq par seconde : il apparaît à la première image
qui montre le défaut et disparaît à la première qui ne le montre plus (reset réussi,
variateur remis sous tension). Sans images en direct il suit `/api/status`, qui porte
l'état du variateur (`drive_state`, `drive_fault`, section 14) : une réponse écrite
après la dernière image reçue le lève ou le baisse, au tour de statut (5 s au plus) ;
une réponse plus ancienne que cette image ne parle pas à sa place, et sans réponse
plus récente le mot de la dernière image reste, à côté de `NO LIVE DATA`. L'état
`comm_lost` (variateur non lu) ne lève pas ce bandeau : il veut dire « inconnu ».

**Pourquoi un bandeau.** Sur une console qui n'a encore rien armé, un défaut apparu
au repos ne lève aucun verdict : la console au repos lit le variateur deux fois par
seconde sans rien lui écrire, et ne juge que s'il tourne. La pastille **Securite**
reste donc à `none`, et le défaut ne se lisait que sur cette carte. Un démarrage était
alors accepté par la console (202) puis refusé un cycle plus tard par la boucle, qui
relit le variateur avant d'armer ; ce refus laissait en plus un verdict `drive_fault`
à acquitter. Désormais la console refuse ce démarrage elle-même, au clic, sur la
lecture qu'elle montre à la page (section 13) : rien n'est mis dans la boîte aux
lettres, aucun verdict ne se verrouille, et aucune règle de sécurité n'a été ajoutée.
Une fois le défaut levé, le démarrage suivant est pris. Rejoué dans un navigateur,
défaut `ObF` injecté au repos dans le variateur simulé : bandeau affiché 0,5 s après ;
clic sur `Demarrer MANUEL` répondu 409, note rouge sous le bouton tout de suite ;
`Reset defaut variateur`, bandeau disparu, démarrage suivant accepté. Avant ce
changement : aucun bandeau, 202 puis refus 0,16 s plus tard, mode `ARRET` un instant,
et `drive_fault / ramp_down` à acquitter après le reset. Après une séance, la console
écrit au variateur à chaque cycle et un défaut au repos lève toujours le verdict
`drive_fault`, comme avant : le bandeau s'y ajoute.

Bouton **`Reset defaut variateur`** (visible seulement s'il y a un défaut) :
`POST /api/drive/fault-reset`. Nommé, explicite, jamais automatique. La boucle le refuse
sauf si la machine est au repos, que tout autre verdict est acquitté et que le variateur
montre l'arbre arrêté. Certains défauts ne sont **pas réarmables** depuis la console
(`reset refuse : defaut OCF non rearmable depuis la console : couper l'alimentation du variateur et inspecter`).
La note sous le bouton suit la réponse de la console à la demande de cet écran,
comme celle d'un démarrage (jusqu'au 10 octobre 2026 :
`reset demande : fault_reset (le variateur doit le confirmer)` dès la réponse 202,
même quand la boucle refusait) :

| Note | Quand |
|---|---|
| `demande envoyee : fault_reset - pas encore prise par la machine` | à la réponse 202 |
| `reset pris par la machine` | à l'événement `taken` de cette demande : la boucle a écrit le reset au variateur |
| `reset pris par la machine : le variateur ne signale plus de defaut` | dès qu'une image prise après cette réponse ne montre plus de défaut. La note s'efface si un défaut revient ensuite : c'est un autre défaut |
| `reset pris par la machine : le variateur signale encore le defaut` (rouge) | une image prise plus d'une seconde après la réponse montre encore le défaut ; corrigée quand il disparaît |
| `refus de la machine (<heure>) : reset refuse : …` (rouge) | à l'événement `refused` de cette demande |
| `demande annulee (<heure>) : un STOP est arrive avant que la machine prenne cette demande, rien n'a ete fait` (rouge) | à l'événement `cancelled` de cette demande |
| `cet ecran ne sait pas ce que la machine a fait de sa demande de reset : sa liaison a ete coupee.` (rouge) | la réponse n'est pas arrivée à cet écran ; la page la demande à la console (section « Mode MANUEL », « Liaison coupée ») |

Un défaut qui disparaît sans que la console ait dit `taken` ne change pas la note :
la page ne dit pas que le reset de cet écran a été pris parce qu'une image ne montre
plus de défaut. Le refus d'un reset demandé par un autre écran n'est pas écrit sous
ce bouton. Rejoué dans un navigateur sur la console en simulation, le
11 octobre 2026 : `demande envoyee …` 9 ms après le clic, puis
`reset pris par la machine : le variateur ne signale plus de defaut` à 158 ms.

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
| `Passager (liste du tableau de bord, obligatoire)` (liste) | les passagers que le tableau de bord donne à cette machine (`GET /api/roster`, section « Le passager » plus bas), affichés par leur nom. Première ligne `- choisir le passager -` (ou `- aucun passager propose -` quand la liste est vide ou absente) |
| ligne sous la liste | ce que le site a réglé pour le passager choisi : `age <n> ans · FC max <n> bpm (reglees sur le site par un gestionnaire, non modifiables ici)`. En rouge, suivie de la phrase de refus, quand une valeur manque sur le site (`FC max inconnue`, `age inconnu`). `aucun passager choisi` sinon ; `le passager choisi n'est plus dans la liste du tableau de bord : en choisir un`, en rouge, quand une liste plus récente ne le porte plus |
| ligne suivante | l'état de la liste : `liste du tableau de bord recue il y a <n> s` (ou `min`) ; en rouge, la raison pour laquelle personne n'est proposé, ou la dernière demande restée sans réponse |
| `Previsualiser` | `POST /api/plan/preview` : résout le profil **sans rien démarrer** (fonctionne même pendant une séance), ajusté à la FC max du passager choisi s'il y en a un. Message : `plan resolu ; rien n'a ete demarre` |
| `Demarrer la seance` | `POST /api/session/start`. En cas d'accord (202), la page reste sur Seance et la note sous les boutons suit la réponse de la console à la demande de cet écran, comme celle d'un démarrage manuel (section 6) : `demande envoyee : start <profil> - pas encore prise par la machine`, puis `demarrage pris par la machine : start <profil>` à l'événement `taken` de cette demande, ou `refus de la machine (<heure>) : demarrage refuse : …` en rouge à son événement `refused`, ou `demande annulee (<heure>) : un STOP est arrive avant que la machine prenne cette demande, rien n'a ete fait` en rouge à son événement `cancelled`. `pris` s'efface à la fin de la séance. Liaison coupée entre le clic et la réponse : en rouge, `cet ecran ne sait pas ce que la machine a fait de sa demande de demarrage : sa liaison a ete coupee.`, jusqu'à ce que la console ait redit le sort de la demande ; liaison coupée après `pris` : en rouge, `la machine a pris le demarrage demande par cet ecran (start <profil>), mais sa liaison a ete coupee depuis : cet ecran ne sait pas si la seance en cours est encore celle-la.`, jusqu'à la réponse du statut. Si la durée demandée n'est pas celle du profil, elle suit le nom du profil dans la note (`start <profil> over <secondes> s`). Un démarrage refusé tout de suite par la console (un autre démarrage attend, un reset attend) : la phrase de la console, en rouge (section 6, « Refus immédiat »). Jusqu'au 10 octobre 2026 : `accepte : start <profil>` dès le 202 |

### Carte « Depart distant »

Quand un poll propose un lancement AUTO, la carte **Depart distant** apparaît sur
Seance. Le poll ne démarre rien : la carte montre le programme, la personne qui l'a
lancé et le nom associé au `subject_id` dans la liste de passagers que la console
tient alors. `subject_label` envoyé par le tableau de bord ne sert pas à l'affichage.
Le nom passe uniquement dans la réponse JSON de `GET /api/remote-launch` vers la page;
il n'est écrit ni dans l'enregistrement, ni dans `logbook/`, ni dans les journaux du
processus. Si la liste tenue ne contient pas ce passager, la carte montre
`indisponible` avec la raison et le bouton reste désactivé. La route de confirmation
et l'étape CloudSync revérifient cette liste avant `training/start`; le panneau la
revérifie encore au moment d'armer. L'absence avant la demande garde l'offre
récupérable, sans `training/start` ni `training/end`; après le retour du passager,
un nouveau clic est nécessaire. L'absence après un 2xx refuse l'armement et envoie
une fin échouée. L'âge et la FC max restent ceux du lancement reçu.

L'opérateur renseigne le champ **Operateur** déjà présent dans la carte Programme,
puis fait un seul geste sur **Cette personne est a bord et prete**. Le bouton envoie
la confirmation locale; la tâche CloudSync exécute ensuite le contrôle local,
`training/start`, puis soumet le départ à la boîte de la machine seulement après un
2xx. Le bouton **Demarrer la seance** reste masqué tant qu'une offre est affichée.
L'état indique `en attente de confirmation`, `demande en cours`, `refuse : <raison>`
ou `non confirme faute de reponse : <raison>`. Un refus du tableau de bord, un refus
local avant la demande ou un refus local après le 2xx est définitif pour ce lancement;
il ne peut plus être confirmé. Les refus du tableau de bord et les demandes sans
réponse n'envoient pas `training/end`; un refus local envoie une fin échouée. Après
une absence de réponse ou un empêchement temporaire (passager absent, séance déjà en
cours au traitement), un nouveau clic est possible seulement après récupération et
tant que le poll propose l'offre. Aucun essai n'est automatique.

Cette confirmation Pi est le bouton qui sera garanti par le geste physique d'ANH-85;
elle ne réalise pas ce geste et n'ajoute pas son délai. L'opérateur est le nom que la
console connaît aujourd'hui; ANH-152 reste hors de ce parcours. Si Convex a pris le
départ mais que sa réponse 2xx se perd, une nouvelle confirmation reçoit
`session_not_pending` (400) : la console n'arme pas et la machine peut rester tenue
jusqu'à l'interruption. Ce suivi d'idempotence relève d'ANH-240.

Le bouton **Demarrer la seance** est désactivé tant que : l'arrêt d'urgence n'est pas
attesté, la machine n'est pas `idle`, ou les séances programmées sont désactivées.
Dans ce dernier cas une note dit :
`seances programmees desactivees sur cette console (jalon M5) : utiliser le mode MANUEL`.
Elle a sa propre ligne : le résultat d'une prévisualisation s'affiche sous elle, sans
être effacé.

Pour qu'une séance programmée puisse partir, **toutes** ces conditions doivent tenir
(dans l'ordre où la console les vérifie, `LocalPanel._start_programme`) :

1. paramètres médicaux signés, puis `PROGRAMS_ENABLED=true` (en mode banc, le refus
   dit `parametres medicaux non signes`) ;
2. un passager de la liste du tableau de bord, que la liste tenue **au moment où la
   boucle arme** porte encore, avec sa FC max et son année de naissance renseignées sur
   le site ; pour un lancement venu du tableau de bord, les valeurs que porte le
   lancement ;
3. âge du passager (calculé depuis l'année de naissance, comme le site) ≥ `min_rider_age`
   du fichier médical signé ;
4. `OCCUPANCY_OCCUPIED_ENABLED=true` (une séance programmée a toujours une personne à bord) ;
5. le profil existe et se résout, ajusté à la FC max de ce passager : tous les contrôles
   cardiaques sont refaits pour lui (zone haute ≤ 90 % de sa FC max, palier dur ≤ sa FC max) ;
6. le plafond du profil (`max_rpm`) est sous le plafond « personne à bord » ;
7. la caméra, si configurée, ne bloque pas le démarrage ;
8. les contrôles du runtime : attestation, aucun verdict en attente, paliers cardiaques du
   profil égaux à ceux du superviseur (`hr_hard_max_bpm` / `hr_critical_bpm` du fichier
   médical), variateur
   disponible, pas en défaut, pas déjà en marche.

Avec la configuration par défaut, les points 1 et 4 sont faux : **aucune séance
programmée ne démarre** (403). Cela a été vérifié en simulation.

La route vérifie le passager avant de mettre la demande dans la boîte aux lettres, et
répond tout de suite : 422 sans passager, ou avec un âge saisi (`subject_age` n'est plus
lu : `l'age du passager ne se saisit plus a la console : il vient du site, avec sa FC
max, regles par un gestionnaire. Choisir le passager dans la liste (subject_id).`) ; 409
pour un passager que la liste ne porte pas, une liste trop ancienne ou absente, une
valeur manquante sur le site ; 422 pour un profil que la FC max de ce passager refuse.
La boucle refait la vérification quand elle arme : une liste reçue entre le clic et
l'armement qui ne porte plus le passager fait refuser le départ.

### Carte « Retour du passager » (ANH-146)

**Quand elle s'ouvre.** Sur la page Seance, à la fin d'une séance avec une
personne à bord : une séance manuelle déclarée `occupied`, ou toute séance
programmée (la FC du passager la régule : il y a toujours quelqu'un à bord).
**Jamais pour une séance de banc** : la console ne propose rien, et
`POST /api/feedback` répond 409. Elle ne s'ouvre que sur une console reliée
au tableau de bord : sans lien, le retour n'aurait nulle part où aller. La
page la lit au démarrage, à l'événement `session_idle` et à chaque relecture
du panneau ; la séance suivante qui démarre la ferme.

**Ce que l'opérateur saisit.** Deux listes de 0 à 10, la **Nausée** et le
**Malaise ou vertige**, chaque niveau avec son libellé (« 3 - Légère à
modérée ») ; le **Ressenti** (Bon, Acceptable, Mauvais) ; un **Commentaire**
facultatif de 500 caractères au plus, avec son compteur, rogné, absent s'il
est vide. L'échelle et ses libellés sont ceux de
[`contracts/feedback.json`](../contracts/feedback.json), recopiés dans
`src/passenger_feedback.py` et épinglés au fichier par les tests du Pi (la
console est déployée sans ce fichier). Ils sont provisoires, sans validité
médicale, à valider avec ANH-98. **Envoyer** n'est actif qu'une fois les deux
notes et le ressenti choisis ; une seconde saisie avant l'envoi remplace la
première. Tant que rien n'est saisi, **Ignorer (non renseigné)** ferme la
carte : rien n'est envoyé. Une fois le retour saisi, ce bouton disparaît :
la carte garde le formulaire pour le modifier tant qu'il n'est pas parti, et
**Fermer** la ferme **sans jeter le retour**, qui continue de partir en
arrière-plan, comme quand la séance suivante démarre. L'opérateur n'est pas nommé : la
saisie passe par l'accès de la console (son jeton), comme toute requête, et
le tableau de bord la range sous `submittedBy: "operator"` en attendant
l'authentification nommée (ANH-152).

**Où le retour va, et où il ne va jamais.** C'est une donnée de santé. La
console la garde **en mémoire seulement**, jusqu'à ce que le tableau de bord
l'ait : jamais dans l'enregistrement de séance, jamais sur la carte SD, jamais
dans un journal ni dans une réponse d'erreur (un refus nomme le champ, jamais
sa valeur). **Une console qui redémarre avant l'envoi l'a perdu**, sans le
dire : le passager peut encore le donner sur le site, dans les mêmes 24 h.
Elle l'envoie à la route des événements comme le seul événement d'une requête,
de kind `feedback` ([convex.md](convex.md#routes-utilisées-par-la-console-locale-raspberry-pisrccloud_syncpy)),
et seulement à un serveur dont le dernier heartbeat a annoncé le contrat
**1.7** ou une mineure plus récente de la même majeure : un serveur antérieur
le rangerait avec les événements de la séance.

**La ligne d'état** dit où en est l'envoi, relu à chaque tour du lien :

| État | Ce que la console fait |
|---|---|
| (vide) | rien de saisi encore |
| En attente du tableau de bord | lien coupé, ou version du serveur pas encore connue : nouvel essai 15 s après un échec |
| En attente de la séance sur le tableau de bord | une séance démarrée à la machine n'a son identifiant qu'une fois déclarée : envoi dès qu'elle l'est |
| Serveur trop ancien (contrat 1.7 requis) : non envoyé | rien n'est envoyé ; envoi si le serveur annonce 1.7 plus tard |
| En attente de la fin de séance sur le serveur : nouvel essai | le tableau de bord ne prend un retour qu'une fois la fin de la séance reçue (`feedback_session_not_ended`), et la console n'envoie cette fin qu'après l'acquittement de ses points : nouvel essai 15 s plus tard, ce refus n'est pas définitif |
| Envoyé au tableau de bord | fait, une seule fois ; **Fermer** ferme la carte |
| Refusé par le tableau de bord (code) | refus définitif, avec le code stable (`feedback_not_eligible`, `feedback_window_closed`, `session_not_found`, `invalid_request`), sans valeur |
| Abandonné : plus de 24 h depuis la fin de la séance | la fenêtre du tableau de bord est fermée : la saisie est effacée |

La console garde au plus huit retours en attente ; au-delà, le plus ancien est
abandonné et la carte le compte (« retour(s) abandonné(s) : trop de retours en
attente »). Limite connue : si une séance démarrée à la machine se termine sans
avoir été déclarée (lien coupé pendant toute la séance), puis qu'une autre la
suit avant le retour du lien, le retour de la première peut rester en attente
de son identifiant jusqu'à l'abandon de 24 h.

### Le passager : la liste du tableau de bord (ANH-84)

La console demande au tableau de bord la liste de ses passagers
(`GET /api/machine/roster`) à son démarrage, puis toutes les 60 s **tant qu'aucune
séance ne tourne**. Elle la demande par une tâche à elle : une liste lente à venir, ou
qui ne vient pas, ne retarde ni le battement de cœur envoyé au tableau de bord ni
l'interrogation des lancements. La liste porte, pour chaque personne qui détient le droit de
lancement sur cette machine et reste membre active de son organisation : son
identifiant, son nom, sa FC max et son année de naissance, réglées sur le site par un
gestionnaire. La console les affiche et ne permet pas de les modifier.

| Ce qui arrive | Ce que la console fait de la liste |
|---|---|
| une réponse | elle remplace la liste tenue, en entier : un passager absent de la nouvelle n'est plus proposé, et un départ qui le nomme est refusé |
| pas de réponse (réseau, erreur du serveur, réponse illisible) | la dernière liste reçue est gardée, en mémoire, **15 minutes** au plus après sa réception ; au-delà, personne n'est proposé (`liste des passagers perimee`) jusqu'à la réponse suivante |
| clé de la machine refusée (401, 403) | la liste est vidée tout de suite : une machine que le tableau de bord ne reconnaît plus ne propose personne |
| console sans tableau de bord (`MACHINE_API_KEY` vide) | aucune liste : personne n'est proposé, et seules les séances de banc partent |
| redémarrage de la console | rien n'est gardé : la liste revient à la première réponse |

La liste ne vit qu'en mémoire. Le **nom** d'un passager est montré sur cette page pour que
l'opérateur choisisse la bonne personne ; il n'est écrit nulle part sur le disque (ni
dans l'enregistrement, ni dans le journal hors séance, ni dans les journaux de la
console) et aucune phrase de refus ne le cite. L'enregistrement de séance porte
l'identifiant du passager (`subject_id`), un pseudonyme. La durée de 15 minutes est une
décision du responsable produit (15 octobre 2026) ; c'est une constante du logiciel
(`ROSTER_MAX_AGE`), pas un réglage de la machine : aucune clé du `.env` ne la change.
L'âge de la liste se compte sur l'horloge monotone de la console, jamais sur son heure
murale : une heure murale qui saute (poste macOS du banc en avance, Pi sans service de
temps) ne vieillit ni ne rajeunit la liste. Au-delà de 900 s, la phrase donne cet âge à
la seconde, arrondi à la seconde supérieure (`recue il y a 15 min 1 s`) : une liste
périmée ne se lit jamais « 15 min » ou moins. L'âge du **passager**, lui, se compte sur
l'heure murale (année de naissance) : sur le Pi, une horloge que personne ne tient
refuse toute personne à bord ; sur le Mac du banc, qui ne juge pas son horloge
([Ligne horloge](#ligne-horloge--lheure-de-la-machine)), une heure en avance vieillit
le passager.

Ce que la liste ne garantit pas : ce qui change sur le site après la dernière réponse
n'est vu qu'à la réponse suivante. C'est vrai d'un droit retiré, et aussi d'une **FC max
abaissée** ou d'une **année de naissance corrigée** par un gestionnaire : jusqu'à la
réponse suivante, le programme est ajusté à l'ancienne FC max et l'âge compté depuis
l'ancienne année. Au repos, la réponse suivante arrive dans la minute ; sans réseau, une
valeur ancienne sert au plus **15 minutes**, puis personne n'est proposé. Le tableau de bord revérifie le droit quand la
séance lui est déclarée et ne l'attribue pas à un passager qui ne l'a plus (mention
`Passenger attribution unavailable at registration`, [convex.md](convex.md)). La
revérification de l'identité et des droits à l'armement est assurée par ANH-144.

Depuis le contrat 1.6, la liste porte aussi `fit`, `fitnessValidUntil`, `consent`
et `fitnessQuestionnaireVersion`. Seul le booléen JSON `true` rend `fit` ou
`consent` vrai : un champ absent, `null`, `1` ou `"true"` reste faux. L'échéance
doit être un nombre entier de millisecondes Unix entre 0 et le 1er janvier 2101 ;
une valeur inutilisable reste inconnue. La version doit respecter
`[0-9A-Za-z.+-]{1,32}`, sinon elle reste inconnue. Une réponse d'un tableau de bord
1.5 sans ces champs refuse donc tout départ avec personne à bord.

La console vérifie cette porte pour une séance programmée locale et une séance
manuelle `occupied`, au moment d'armer. Pour un lancement distant, elle la vérifie
avant `training/start`, puis de nouveau à l'armement après la réponse du serveur.
L'horloge murale injectée sert à comparer l'échéance : à égalité, l'aptitude est
expirée, même si la liste est encore récente. Une horloge non fiable refuse
toujours une personne à bord. Une séance de banc reste sans passager.

| Refus | Phrase montrée à l'opérateur |
|---|---|
| aptitude fausse, échéance ou version inconnue | `demarrage refuse : aptitude du passager non validee, a valider par un gestionnaire sur le site` |
| aptitude expirée | `demarrage refuse : aptitude du passager expiree, a valider par un gestionnaire sur le site` |
| consentement absent | `demarrage refuse : consentement du passager absent, a signer par le passager sur le site` |

La porte juge la liste tenue : un retrait du consentement ou une nouvelle
saisie sur le site n'est vu qu'à la liste suivante, dans les 60 s en ligne au
repos, jusqu'à 15 min sans réseau. Seule l'échéance déjà reçue est jugée hors
ligne par l'horloge du Pi. « Aptitude expirée » devient « aptitude non validée »
dès la liste suivante : Convex sert alors `fit: false` et
`fitnessValidUntil: null`. Une aptitude qui expire pendant une séance en cours
ne l'arrête pas : cette porte s'applique au départ seulement (EX-5).

Un refus local distant est renvoyé comme fin échouée. Un 409 du serveur portant
`rider_not_fit`, `fitness_expired` ou `consent_missing` montre sa raison et n'arme
pas. Les phrases ne portent ni nom, ni réponses, ni notes, ni valeur de santé.
Le manifeste conserve seulement `subject_fit: true` et la version du questionnaire
du passager admis ; le banc les laisse à `null` ([enregistrement.md](enregistrement.md)).

### Carte « Ce que ce programme ferait »

Limites du profil : `zone` (bpm), `max absolu` (palier dur), `critique`, `FC max du sujet`,
`plafond`, `plafond echauffement`, `minimum de rotation` (tr/min moteur), `HSP variateur`
(Hz), `total`, `palier` (durée du HOLD). Après prévisualisation s'ajoutent : `charge au
plafond` (g et tr/min de sortie), `charge echauffement`, `charge minimum`, `duree modifiee`,
et le tableau des phases (`baseline`, `warmup`, `hold`, `cooldown`, `recovery`, avec début,
fin, durée). `FC max du sujet` est celle du profil stocké tant que rien n'est prévisualisé ;
une prévisualisation faite avec un passager choisi montre le profil ajusté à sa FC max, celui
qu'un départ pour lui armerait.

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
`taken`, `refused`, `cancelled`, `dashboard`, `recording`. La liste montre l'heure,
le type, l'opérateur et le détail ; l'identifiant de la demande (`request_id`) n'y
est pas affiché. Depuis le 11 octobre 2026, ce que la boucle fait d'une demande est
toujours dit : `taken` quand elle l'a prise (le détail redit ce qui était demandé :
`manuel bench`, `start <profil>`, `cible 5.00 tr/min de sortie`, `reset defaut`, ou le
motif d'un arrêt), `refused` quand elle l'a refusée (cible hors domaine, cible refusée
sur un bras à l'arrêt, reset refusé, démarrage refusé : la route HTTP a déjà répondu
202), `cancelled` quand un STOP a pris sa place dans la boîte avant la boucle. Une
cible manuelle que la machine a remise à 0 arrive comme `refused`, sans nom
d'opérateur ni identifiant : personne ne l'a demandé. Sous un bouton, un écran
n'écrit que les réponses à ses propres demandes (section 6) ; les réponses faites
aux autres écrans ne sont que dans cette liste. Seule la remise à 0 d'une cible est
écrite dans la note de la carte Mode MANUEL de tous les écrans qui montrent la séance.
Un tableau de bord d'un autre contrat arrive sous son propre type, `dashboard`,
sans nom d'opérateur (section « Lien avec le tableau de bord » plus bas) : ce
n'est pas une réponse de la boucle, et il n'est jamais écrit dans la note de la
carte Mode MANUEL.

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
- Hors boucle locale, elle refuse aussi de démarrer **sans certificat** : la page,
  les commandes et le jeton n'y circulent qu'en TLS
  ([section 1](#depuis-une-tablette--la-page-en-tls) ; message :
  `configuration: UI_HOST/UI_TLS_CERT/UI_TLS_KEY: refusing to serve the operator interface on '0.0.0.0' in clear HTTP: ...`).
  Les deux refus sont dits ensemble quand les deux manquent.
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

### Carte « Enregistrements de seance »

Une note (`Le dernier enregistrement de cette machine, en archive .tar.gz. Machine au
repos uniquement.`) et le bouton `Exporter l'enregistrement`. Le bouton demande la liste
des enregistrements, télécharge l'archive du plus récent et écrit dans la note
`exporte : <nom>.tar.gz`. Sinon la note passe en rouge avec la raison :
`aucun enregistrement sur cette machine`, `cette console n'enregistre pas`,
`liste des enregistrements refusee pendant une seance : attendre le retour au repos`,
ou une autre erreur de l'API (section 13).
Voir la [section 16](#16-lenregistrement-de-séance-boîte-noire-locale).

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
  Depuis le 11 octobre 2026, `/api/status` donne le verdict en vigueur et le plancher
  verrouillé **quel qu'en soit l'auteur** : le superviseur, ou le runtime lui-même, qui
  peut en verrouiller un sans passer par lui (`drive_precommanded`,
  `enable_unconfirmed`, `disable_refused`, `tick_exception`, son propre `comms_lost`).
  Jusque-là il ne donnait que ceux du superviseur, et ces deux lignes disaient `aucun`
  sous un tel verdict dès que l'écran n'avait plus d'images. Tant que des images
  arrivent, elles affichent toujours le verdict que portent les images quand il est au
  moins aussi sévère que celui du statut (pour `plancher verrouille`, seulement s'il
  est verrouillé) : les images arrivent cinq fois par seconde, le statut toutes les
  cinq secondes. Rejoué dans un navigateur, WebSocket de l'écran tombé puis variateur
  simulé mis en marche par un autre maître : `verdict retenu` et `plancher verrouille`
  disent `drive_precommanded / quick_stop` au tour de statut suivant (2,9 s), au lieu
  de `aucun`.
- **`verrouille derriere go_silent`** : cette ligne n'apparaît, sur Securite et sur
  Configuration, que sous `go_silent` et quand un verdict était verrouillé avant lui.
  `go_silent` prend la place de ce verdict dans `verdict retenu` et dans
  `plancher verrouille`, et refuse tout acquittement : ce verdict est donc toujours
  verrouillé, et la ligne le nomme (`reverse_rotation / quick_stop`, par exemple, ou
  `drive_fault / ramp_down`). L'arrêt d'urgence, tenu à part, reste dans
  `e-stop verrouille`.

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
Elle n'est affichée que sur les pages Seance et Securite. Depuis le 11 octobre 2026
elle n'est plus ajoutée sur un bras manuel à l'arrêt dont la cible vaut 0 : rien n'y
attend, et rien ne remontera quand l'avertissement se lèvera (une cible non nulle y
est refusée tant qu'un verdict tient, section 6). Le verdict lui-même est inchangé :
même règle, même action, même instant, seule cette phrase est retirée.

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

Sur ce bras manuel tenu à l'arrêt, la phrase anglaise `NOT LATCHED …` n'est plus dans
le détail du verdict non plus (11 octobre 2026). Le superviseur, qui l'ajoute, ne
connaît pas la cible manuelle : le runtime lui dit désormais, dans son observation,
que rien n'attend (séance manuelle, consigne à 0 et cible à 0). Ce champ ne sert qu'à
cette phrase ; aucune règle ne le lit. Rejoué dans un navigateur, séance manuelle avec
une personne à bord déclarée par l'API, bras à l'arrêt, cible 0, ECG coupé : le détail
de `hr_stale` se terminait par la phrase ; il ne la porte plus. Si une cible vient
d'être acceptée quand l'avertissement apparaît, la phrase est dite pendant ce seul
cycle : la cible attendait encore quand le verdict a été écrit, et la machine la
remet à 0 à la fin de ce même cycle.

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
**Une fin de séance qui se déroule normalement ne déclenche plus ce verdict**
(8 octobre 2026, ANH-185). Jusque-là il apparaissait pendant deux fins de séance où
rien ne s'était mal passé : une fin ouverte tard dans un programme (STOP, E-STOP ou
verdict d'arrêt), parce que toute fin de séance rouvre une récupération complète, qui
dépassait alors l'échéance, bras déjà arrêté (section 5) ; et une séance manuelle qui
atteignait ses 3600 s à grande vitesse et descendait encore 30 s plus tard. La règle
juge maintenant une fin de séance déjà ouverte sur sa propre échéance : l'instant où
elle a été ouverte, plus la descente attendue depuis la vitesse commandée à cet
instant (4 s bras à l'arrêt ; 30 s depuis 193 tr/min moteur dans un programme ; en
séance manuelle la descente aux limites de mouvement et 4 s de rampe du variateur,
soit 24 s depuis 300 tr/min moteur et 108 s depuis 1344), plus la récupération une
fois la consigne à 0 (300 s avec le profil standard ; 60 s en séance manuelle avec
une personne à bord, aucune capsule vide), plus 30 s. Jamais plus tôt que l'échéance
du programme. Ces fins de séance se terminent donc sans `session_overrun` : après un
STOP il n'y a rien à acquitter, et après un E-STOP ou un verdict d'arrêt il reste
celui-là seul.

Le verdict apparaît encore quand une fin de séance ne se déroule pas comme prévu :
une descente qui n'a pas ramené la consigne à 0 dans le temps qu'une descente prend
depuis cette vitesse, ou une récupération qui ne finit pas. Le détail nomme alors la
fin de séance :
`the session has run 1859 s against an ending opened at 1799 s with 30 s to bring the setpoint back to zero plus 30 s of grace: the phase machine has lost track`
(ou `… with 304 s to finish its descent and its monitored recovery plus 30 s of grace …`
pour une récupération). Demander STOP sur un bras qui ne descend pas ne retarde ce
verdict que du temps de cette descente : mesuré sur le banc d'essai logiciel, bras
tenu à 193 tr/min moteur, STOP à 1799 s, verdict à 1859 s au lieu de 1830 s sans
STOP. Un `freeze` verrouillé que personne n'acquitte n'est pas un de
ces cas : il ne tient plus le bras en vitesse au-delà de la fin du programme (alinéa
suivant). Quand le verdict apparaît : attendre que **Mode** affiche `REPOS`,
acquitter par son nom, puis demander un nouveau départ. L'acquittement tient alors.
Avant `REPOS`, il répond 200 et le verdict est de nouveau là au cycle suivant.

**Un arrêt d'urgence ou un défaut variateur qui arrive au repos, séance finie, ne
rouvre plus de fin de séance** (8 octobre 2026, ANH-185). Après un programme allé à
son terme, ils remettaient **Mode** à `ARRET` et la phase à `recovery` pour toute la
récupération du profil (5 minutes avec le profil standard), et les règles de fréquence
cardiaque jugeaient de nouveau : électrodes retirées, `hr_stale` allait jusqu'à un
`ramp_down` verrouillé de plus. Maintenant **Mode** reste `REPOS`, aucune phase
n'est affichée (`-`, comme toujours au repos ; le champ `phase` de `/api/snapshot`
reste `done`), et la pastille **Securite** affiche le verdict, verrouillé :
`quick_stop` pour un E-STOP, `ramp_down` pour un défaut variateur. Aucune règle de
fréquence cardiaque ni de présence ne se déclenche. Tout départ est refusé (409) avec
le nom du verdict jusqu'à son acquittement par son nom, case « coup de poing » cochée
pour un E-STOP. Le réarmement d'un défaut variateur, qui n'est accepté qu'en phase
`done`, l'est tout de suite : il n'y a plus 5 minutes à attendre. C'est ce que la
console faisait déjà après une séance terminée par un STOP. Vérifié par l'API sur la
console en simulation (`raspberry-pi/tests/test_session_overrun_console.py`) ; pas
rejoué dans un navigateur.

Mesures et limites :
[securite.md, section 8](securite.md#8-une-séance-finie-nest-plus-jugée-sur-sa-durée-anh-181)
et [8.6](securite.md#86-une-alerte-de-fin-de-séance-dit-quelque-chose-de-vrai-anh-185).

**Sous un `freeze`, le retour au calme d'un programme se fait quand même.** Un
`freeze` tient la vitesse d'un programme pendant `warmup` et `hold`. À partir de la
phase `cooldown`, le programme ne demande plus de vitesse : la consigne descend alors
vers 0 aux limites de mouvement, dès le premier cycle de cette phase, comme sans
avertissement, que le `freeze` soit verrouillé ou non. Elle part de la vitesse tenue
et ne remonte pas. À l'écran pendant cette descente : **Mode** `SEANCE`, phase
`cooldown`, pastille **Securite** `freeze`, consigne et vitesse mesurée qui baissent ;
le bandeau `REPRISE AUTOMATIQUE POSSIBLE` n'est pas affiché. La ligne `detail` d'un
`freeze` de `loop_stall` le dit elle-même :
`the control loop took 1.20 s between ticks against a period of 0.20 s: the setpoint is held where it was (it still comes down on a stop asked for, and on the programme's own descent)`.
La séance se termine ensuite comme un programme mené à son terme : phase `recovery`,
puis `REPOS` à la durée prévue, sans `session_standstill` ni `session_overrun`. Un
`freeze` verrouillé reste affiché à `REPOS` et refuse tout départ (409) jusqu'à son
acquittement nominatif ; l'acquitter ne met rien en mouvement. Cette fin vaut pour un
`freeze` verrouillé, et pour un `freeze` non verrouillé dont la cause cesse avant le
`ramp_down` verrouillé de sa règle. Si la cause dure, la règle continue de compter comme
avant : `hr_stale` passe à `reduce` à 30 s, ce qui ne change pas cette fin, puis à
`ramp_down` à 60 s, `attendant_absent` à `ramp_down` à 120 s, et ce `ramp_down` verrouillé
termine la séance (mode `ARRET`,
verdict à acquitter). La descente sous `freeze` ne va jamais plus vite que les limites
de mouvement ; elle peut être en avance sur un retour au calme sans avertissement,
parce que celui-ci est en plus borné par la rampe de la régulation. Mesuré sur le banc
d'essai logiciel avec le profil standard : jusqu'à 2,6 s d'avance depuis 193 tr/min
moteur, et jusqu'à 4,4 s depuis le plafond du profil, 276 tr/min moteur
([securite.md](securite.md#78-la-descente-prévue-dun-programme-est-suivie-sous-freeze-anh-189)). Les actions plus sévères
décident toujours en premier, et une séance manuelle n'a pas de retour au calme
propre : un `freeze` y tient la consigne jusqu'à un STOP ou une cible de 0 (section 5).
Vérifié par l'API sur la console en simulation, avec le programme court des tests et un
`loop_stall` pendant `hold` (`raspberry-pi/tests/test_cooldown_freeze_console.py`) ;
pas rejoué dans un navigateur, ni sur la vraie machine. Règle et vérifications :
[securite.md](securite.md#78-la-descente-prévue-dun-programme-est-suivie-sous-freeze-anh-189).

### Acquittement

Tout acquittement, même celui d'un autre verdict, est refusé tant que la chaîne STO
n'est pas prête. Le motif est affiché avant le clic, puis la réponse exacte du Pi
reste sous le bouton et dans l'alerte en cas de refus (HTTP 409). Relâcher le
champignon, vérifier le relais de sécurité et rétablir une lecture fiable ; pour
`non configuree`, configurer `STO_SOURCE`. Ensuite acquitter par son nom, puis
demander un nouveau départ. La refermeture de la chaîne ne relance aucune séance.

| Élément | Rôle |
|---|---|
| champ `votre nom` | obligatoire |
| case `le coup de poing a ete deverrouille (tire)` | déclaration séparée : la lecture STO ne remplace pas la confirmation humaine du déverrouillage. Par défaut non cochée, pour échouer du côté sûr |
| bouton `Acquitter` | `POST /api/safety/acknowledge` : efface les verdicts verrouillés. **Rien d'autre ne le fait**, jamais automatiquement |

Résultats possibles (vérifiés en simulation) :

- `acquitte par doc : operator_estop` ;
- `the emergency stop is still latched: confirm the mushroom has been pulled back out (estop_released) before acknowledging` (case non cochée après un E-STOP) ;
- `nothing is latched to acknowledge` ;
- un verdict `GO_SILENT` ne s'acquitte pas : `… demanded GO_SILENT, which is one-way: this process will not command motion again, and recovery is an operator action on a machine that has demonstrably stopped`. Il faut redémarrer la console.

Un refus s'affiche sous le bouton et dans une boîte d'alerte `acquittement refuse : …`.

La note sous le bouton parle du moment où elle a été écrite. Depuis le 10 octobre 2026
la page l'efface ou la corrige quand ce qu'elle reçoit la rend fausse :

- **un nouveau verrou l'efface.** Chaque source dit quelle règle elle montre verrouillée
  (les images : le verdict en vigueur ; le statut : le plancher verrouillé, quel qu'en
  soit l'auteur, et l'arrêt d'urgence). Une règle qu'aucune ne montrait est un nouveau
  verrou :
  `nothing is latched to acknowledge`, ou `acquitte par …`, ne reste plus sous le
  bouton. Un refus qui parle d'un verrou toujours là (`the emergency stop is still
  latched…`) reste ;
- **un acquittement repris est dit.** La console répond 200 dès que les verrous sont
  effacés ; une règle dont la cause est encore là se verrouille de nouveau au cycle
  suivant. Quand, dans la seconde qui suit l'acquittement (à l'horloge de la machine),
  une image montre de nouveau verrouillée une règle qu'il avait effacée, la note
  devient, en rouge,
  `acquittement repris par la machine : <règle> est de nouveau verrouille, sa regle s'est declenchee de nouveau.`
  Pour `session_standstill` et `session_overrun`, tant que le mode n'est pas revenu à
  `REPOS`, elle ajoute
  `La seance n'est pas terminee : l'acquittement tiendra une fois Mode revenu a REPOS.`
  Cette phrase est retirée dès qu'une image montre le mode `REPOS`, et la note entière
  s'efface dès que les images ne montrent plus cette règle verrouillée (acquittée de
  nouveau, de cet écran ou d'un autre) : elle parle au présent.
  Passé cette seconde, un verrou qui revient est un nouveau verrou : la note s'efface.
  L'un et l'autre se jugent sur les images tant qu'il en arrive. Rejoué dans un
  navigateur avec `drive_fault` au repos (défaut encore présent) : la note
  `acquitte par banc : drive_fault` restait ; elle passe au rouge 0,1 s après, au
  plus, sur quatre rejeux. La phrase sur `REPOS` n'est tenue que par les tests de la
  page ;
- **sans images en direct, le statut juge à leur place** (11 octobre 2026). Jusque-là
  la note restait telle qu'elle avait été écrite, `acquitte par …`, sous un verdict
  que le statut montrait de nouveau verrouillé. Une réponse de statut écrite après
  l'acquittement, et après la dernière image reçue, est lue comme une image : si elle
  montre de nouveau verrouillée une règle effacée (dans le plancher, ou l'arrêt
  d'urgence), la note devient `acquittement repris par la machine : …` ; elle est
  gardée tant que le statut montre ce verrou et effacée quand il ne le montre plus ;
  une réponse écrite plus d'une seconde après l'acquittement sans ce verrou vaut
  « il a tenu ». Le statut arrive toutes les 5 s : la correction peut donc attendre
  5 s au lieu de 0,2 s, et un verrou revenu pour une autre raison dans cet
  intervalle est dit « repris » lui aussi (la règle s'est bien déclenchée de
  nouveau : la note en dit plus, jamais moins). Il ne porte pas le mode : la phrase
  sur `REPOS` n'est ni ajoutée ni retirée sur sa parole. Rejoué dans un navigateur,
  avant et après ce changement, `drive_fault` verrouillé au repos, défaut encore
  présent, WebSocket de l'écran tombé : la note `acquitte par banc : drive_fault`
  restait sous un `plancher verrouille` revenu à `drive_fault / ramp_down` (12 s
  observées) ; elle passe au rouge 3,9 s après le clic, au tour de statut suivant.

### Remise en service d'une console hors service

Sous l'acquittement, et seulement quand la console se dit hors service
([section 4](#pastille-service--en-service-ou-hors-service)), la carte montre le titre
`Console hors service`, la phrase `La console a redemarre plusieurs fois apres un arret
non propre et n'accepte aucun depart. Faire verifier la machine avant de la remettre en
service.`, un champ `votre nom` et le bouton `Remettre en service`
(`POST /api/service/restore`).

| Résultat | Ce que la page écrit sous le bouton |
|---|---|
| remise en service faite et écrite sur le disque | `console remise en service` |
| faite, mais le disque n'a pas pris le nouvel état | `console remise en service jusqu'a son prochain redemarrage : l'etat n'a pas pu etre enregistre` |
| nom vide | `an operator name is required: an unattributable session record is not one`, et une boîte d'alerte `remise en service refusee : …` |
| la console est déjà en service | `the console is in service: there is nothing to lift` |

La page ne change pas la pastille elle-même : elle redemande `/api/panel`, et c'est la
réponse de la console qui fait disparaître le bandeau. La remise en service ne lance
rien et n'acquitte rien : un départ demande toujours l'attestation de ce démarrage, les
verdicts acquittés, puis un départ. Elle est écrite dans le journal hors séance sous
l'alias de l'opérateur, jamais son nom. Ce n'est pas un geste de routine : une console
qui redémarre en boucle est une machine à faire vérifier.

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

> **Important.** Le logiciel lit le retour de la chaîne STO configurée (simulé avec
> `sim_*`). Cette lecture ne vérifie ni le retrait du pont STO ni le câblage
> normalement fermé du champignon sur P24 -> STO. L'attestation reste une déclaration
> humaine obligatoire, avec ses deux cases distinctes. Voir [securite.md](securite.md).

### Carte « Chaine STO »

| État affiché | Sens et action |
|---|---|
| `prete` | lecture fraîche de la chaîne fermée ; l'attestation humaine reste nécessaire |
| `ouverte` | relâcher le champignon et vérifier le relais ; au repos après un appui, c'est un état normal |
| `inconnue` | lecture absente, en échec ou trop ancienne ; rétablir une lecture fiable |
| `non configuree` | `STO_SOURCE=none` ; configurer la source, ce n'est pas une panne |

La source figure entre parenthèses : `none`, `sim_ready`, `sim_open`, `drive_input`.
`Derniere lecture reussie` donne l'âge en secondes, ou `aucune`.

**Sur le banc réel, aucun départ avant le câblage, et c'est voulu.** Avec
`MOTOR_BACKEND=serial`, `STO_SOURCE` vaut `none` par défaut et `sim_*` est refusé
au démarrage : la console démarre, la carte affiche `non configuree (none)`, et
tout départ comme tout acquittement de verdict est refusé avec la raison STO tant
qu'une source réelle n'est pas câblée, configurée et prête. Le banc actuel, dont le
pont STO est en place, ne peut donc plus armer ni acquitter un verdict. C'est la
décision du responsable du projet du 22 octobre 2026 : il n'existe aucune
exemption banc, parce que la lecture de la chaîne de sécurité est la condition de
l'armement et de l'acquittement. Le banc redevient utilisable une fois la chaîne
câblée, le pont retiré et `STO_SOURCE=drive_input` configuré avec son entrée
([raspberry-pi.md, 2.2](raspberry-pi.md#22-le-variateur-srcmotor)). Une lecture ancienne
ne reste jamais `prete` : l'état devient `unknown` au-delà de 2 s ; la source reste
affichée et les réponses
locales et la page tiennent aussi compte du temps écoulé depuis cette lecture.

Le bandeau rouge **CHAINE STO NON PRETE** apparaît sur toutes les pages tant que
le départ et l'acquittement sont refusés. Il vient après **VARIATEUR EN DEFAUT** et
avant **PARAMETRES MEDICAUX NON SIGNES** : après les alarmes sur un arrêt ou un défaut,
avec les autres conditions qui empêchent le départ. Le texte propose une action et
dit qu'une chaîne ouverte au repos après un appui est normale ; il ne nomme pas cet
état « panne ». Les boutons de départ manuel et **Demarrer la seance** sont grisés
avec la phrase de refus fournie par le Pi. La porte du runtime reste décisive.
Le bouton manuel conserve sa règle de disponibilité du mouvement, complétée
par le seul contrôle STO. L'attestation et la disponibilité de la machine sont
toujours vérifiées par le Pi au clic ; leurs refus restent visibles. Le départ
programmé conserve aussi ses conditions d'attestation et d'état `idle` dans la page.

Si la chaîne s'ouvre alors qu'un mouvement est possible, le verdict existant
`sto_open / quick_stop` apparaît dans **Verdict en cours**, avec son détail et
`verrouille oui`, même quand aucune lecture ne l'a vue ouverte (voir plus bas).
Le matériel a déjà coupé le couple ; la masse peut rester en rotation. Avec
une source configurée et une preuve positive de mouvement
(séance armée, marche tenue par ce runtime ou statut frais en rotation/étage actif),
l'absence de lecture valable au-delà de la durée admise verrouille
`sto_unreadable / ramp_down`. Refermer ne libère pas le verdict : acquitter par son nom puis
demander un nouveau départ. Au repos observé immobile, l'état non prêt refuse
seulement le départ, sans créer un verrou STO, sauf pour la première lecture
qui suit un arrêt (voir plus bas).

La lecture part juste après un tic terminé, au plus une fois par 0,5 s, et
seulement si elle peut finir avant le tic suivant : elle ne retarde pas les
écritures de commande du tic. Si seul le contrôle s'interrompt, la lecture
affichée peut devenir `inconnue` car le sondage attend les tics terminés :
aucune lecture ne part pendant la pause. `loop_stall` juge cet écart ; il reste
seul si aucune tentative postérieure à la dernière réussite n'est en échec ou
en suspens. Dès qu'une lecture commence, aucune nouvelle pause n'est déduite
du budget STO avant une réussite. Une lecture échouée ou en suspens au début
de la pause peut donc ajouter `sto_unreadable / ramp_down` à `loop_stall`.

Pendant un arrêt, la console ne lit pas la chaîne, pour que les écritures
d'arrêt gardent le lien : fin de séance (STOP local ou depuis le site, verdict,
E-STOP, limite) tant que la consigne descend ou que la marche est tenue, verdict
qui arrête la séance, descente vers zéro sous un `freeze` (après un STOP, ou
dans la phase `cooldown` d'un programme), descente vers une cible manuelle de
0, et E-STOP de la page pas encore pris par la boucle, même au repos. Un
`reduce` n'est pas un arrêt, ni un `freeze` qui tient une vitesse : la séance
continue de réguler, la lecture continue aussi, la carte reste `prete` et une
chaîne ouverte pendant un `reduce` verrouille `sto_open / quick_stop` comme
ailleurs. La descente prévue d'un programme sans verdict (`cooldown` puis
`recovery`) est de la régulation : la lecture continue. Un variateur en
défaut n'est jamais un arrêt en cours, même quand l'arbre tourne encore : il
n'accepte que le réarmement et aucune écriture d'arrêt n'y agit, donc la
lecture continue pendant sa roue libre et après. Une chaîne ouverte pendant
cette roue libre est lue et verrouille `sto_open / quick_stop` sans attendre
l'arrêt, et les autres verdicts s'acquittent avant le réarmement sans
redémarrer la console. Pendant un arrêt, la carte passe à
`inconnue` 2 s après la dernière lecture, sans panne, et redevient `prete`
juste après la fin de l'arrêt (le premier tic qui n'en laisse plus en cours) ;
ce temps n'est pas compté contre la lecture.

Une ouverture de la chaîne que la lecture ne montre pas n'échappe pas au
verrou : pendant un arrêt, où la console ne lit pas, et hors arrêt, quand elle
dure moins que l'intervalle entre deux lectures (environ 0,6 s) ou qu'elle se
trouve entre le relais et la borne STO du variateur, que le retour du relais
ne voit pas. Le variateur signale qu'il a retiré son étage de sortie
(`SWITCH_ON_DISABLED`) alors que la console tient la marche : `sto_open /
quick_stop` se verrouille au tic suivant, avec un détail qui dit que
l'ouverture est déduite de l'état du variateur, même si le champignon est
relâché aussitôt. Un défaut du variateur ou une demande de réarmement mènent
aussi à cet état : il n'est alors pas pris pour une ouverture, et la lecture,
qui continue sur un variateur en défaut, juge la chaîne ; le départ reste
refusé tant qu'elle n'est pas `prete`. Cette exception prend fin dès que le
variateur montre un autre état, ou au départ suivant, une fois le variateur
armé : une ouverture juste après ce départ, ou une coupure entre le relais et
la borne STO, verrouille `sto_open` au premier tic. Après un arrêt pendant lequel un
mouvement était possible, la première lecture `ouverte` verrouille aussi
`sto_open`, même au repos ; une inspection du variateur au repos en échec
compte comme un tel arrêt. Tout acquittement attend alors une lecture `prete`
faite après l'arrêt : celle d'avant l'arrêt, même encore récente, ne suffit
pas, et le refus le dit (`inconnue`). Un `sto_open`, lu ou déduit, ne
s'acquitte de même que sur une lecture `prete` faite après son verrou, même
quand l'arrêt a fini dans le tic du verrou. Un nouveau départ suit l'acquittement
nommé, comme ailleurs. Avant lui, un départ demandé depuis la page est refusé au
clic (409 `verdict_latched`, section 13) dès que la chaîne est lue `prete` : rien
n'entre dans la boîte aux lettres. Tant qu'elle ne l'est pas, c'est le refus STO
qui répond (412 `sto_not_ready`). Deux ouvertures ne portent pas le nom `sto_open` :
pendant un arrêt déjà verrouillé `quick_stop` par la console (variateur trouvé
en marche, armement non confirmé), ce verrou tient et refuse tout départ ;
pendant la roue libre d'un variateur en défaut, une ouverture plus courte que
l'intervalle de lecture n'est vue ni par la lecture ni par le variateur, et le
verrou `drive_fault` tient.

Une lecture déjà partie quand l'arrêt est demandé peut retarder les écritures
d'arrêt, répondue ou non : répondue, de quelques millisecondes (au plus
0,05 s) pour celles faites hors du tic, comme l'E-STOP de la caméra ou la
fermeture de la console, ou de son retard pour le tic suivant si elle répond
au-delà de ce budget ; restée sans réponse, du reste de cette lecture et de la
réouverture du port, au pire environ 1,7 s sur `ftdi://`, plus l'appel système
qui ouvre le port et qu'aucun délai ne borne, comme un échange variateur en
échec ([raspberry-pi.md, 2.2](raspberry-pi.md#22-le-variateur-srcmotor)).

Une réussite remet le crédit de pause à zéro ; après la reprise, elle revient
juste après le premier tic terminé. Sans crédit antérieur, la perte
de lecture est détectée au plus tard 2 s plus un intervalle de tic après la
dernière réussite, pauses comprises. Avec un crédit D déjà acquis, la borne
depuis cette réussite est 2 s + D + un intervalle de tic. D additionne les
portions d'intervalles `loop_stall` excusées avant la première tentative non
résolue, sans tentative pendant ces portions ; dès la première
tentative sans réussite suivante, D ne peut plus croître et la détection arrive
au plus tard 2 s plus un intervalle de tic après son début. Si le lien lui-même
est perdu, les règles de communication dominent.

Au premier démarrage, une source configurée apparaît `inconnue` avec son nom
avant le premier tic. Si le variateur est absent ou hors tension et que ce
processus n'a encore rien armé ni activé, la vitesse inconnue seule ne crée
aucun verrou STO : le départ est refusé avec la raison STO et une panne sans
trame reste réessayable. Rétablir le lien et attendre `prete` avant de demander
le départ ; aucun acquittement STO n'est nécessaire dans ce cas.
`non configuree (none)` ne crée jamais `sto_unreadable` : configurer la source
avant le départ. Une lecture `ouverte` réussie peut toujours verrouiller
`sto_open` lorsque la vitesse est inconnue.

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
   `Demarrer MANUEL`. La note dit `demande envoyee : manual_start bench - pas encore prise par la machine`,
   puis `demarrage pris par la machine : manual_start bench (cible 0)` (44 ms après le
   clic sur le variateur simulé).
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
arrivent comme événements `refused` dans la liste **Evenements** (page Seance), et
sous le bouton de l'écran qui a fait la demande, précédés de
`refus de la machine (<heure>) :` (section 6). Une cible que la machine remet à 0 est
écrite sous la carte Mode MANUEL de tous les écrans qui montrent la séance.

### Refus immédiats (réponse HTTP)

Les départs manuel et programmé peuvent répondre `412`, code `sto_not_ready`, avec
l'une des phrases communes suivantes, également affichées à côté des boutons :

- `chaine STO ouverte : retablir la chaine de securite` ;
- `etat STO inconnu : retablir une lecture fiable de la chaine de securite` ;
- `lecture STO non configuree : configurer la source de la chaine de securite`.

L'acquittement répond `409` avec la même phrase tant que la chaîne n'est pas prête,
même si le verdict à libérer porte sur une autre cause.

| Message | Code | Sens |
|---|---|---|
| `an operator name is required: an unattributable session record is not one` | 400 | nom d'opérateur vide (démarrage, cible, fin, reset, acquittement) |
| `nobody has attested the emergency stop this boot: …` | 412 | attestation du câblage non faite depuis le démarrage du Pi |
| `a latched safety verdict stands (<règle>): <détail> - it must be acknowledged by name first` | 409 | `verdict_latched` : un verdict verrouillé attend un acquittement, quel qu'en soit l'auteur : le superviseur, ou la console elle-même (`sto_open`, `drive_precommanded`, `enable_unconfirmed`, son propre `comms_lost`...). Sous un verdict de la console elle-même, la règle nommée est celle de la ligne `plancher verrouille` de la page Securite. La console répond au clic, sans rien déposer dans la boîte aux lettres. Jusqu'au 25 octobre 2026, un verdict verrouillé par la console elle-même n'était pas lu ici : le démarrage était accepté (202) puis refusé un cycle plus tard par la boucle |
| `demande refusee par la console : un demarrage est deja demande, et la machine ne l'a pas encore pris ou est en train de l'armer. Rien n'a ete envoye.` | 409 | `start_pending` : un démarrage ou un reset pendant qu'un démarrage accepté attend ou est armé (double clic, ou un autre écran). Jusqu'au 11 octobre 2026 : `the machine is starting, not idle (pending: …)`, avec la demande en attente imprimée |
| `demande refusee par la console : une autre demande attend encore la machine (une demande a la fois), rien n'a ete envoye. Reessayer dans un instant.` | 409 | `request_waiting` : la boîte aux lettres (une seule place) tient une autre demande jusqu'au prochain cycle |
| `the machine is running, not idle` | 409 | `machine_running` : une séance tourne déjà |
| `the machine is stopping, not idle` | 409 | `machine_stopping` : un arrêt est en cours, ou un arrêt d'urgence est verrouillé |
| `demarrage refuse : variateur en defaut (<mnémonique>, LFT <code>). Rien n'a ete envoye a la machine : traiter le defaut (carte Variateur), puis redemander.` | 409 | démarrage (manuel ou programmé) demandé alors que la dernière lecture du variateur le montre en défaut ; `(?)` quand le code n'a pas été lu. La console répond au clic, sans rien déposer dans la boîte aux lettres. Seule une lecture qui dit `fault` et date de moins de 2 s refuse : un variateur sain, ou d'état inconnu (`comm_lost`), n'est jamais refusé ici, et la boucle le relit elle-même avant d'armer (section 6) |
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
| `liste des enregistrements refusee pendant une seance : attendre le retour au repos` | 409 | liste des enregistrements demandée pendant une séance (c'est ce que voit le bouton d'export) |
| `export refuse pendant une seance : attendre le retour au repos` | 409 | archive d'un enregistrement demandée pendant une séance |
| `enregistrement inconnu sur cette machine` | 404 | le nom demandé n'est pas celui d'un dossier d'enregistrement |
| `cette console n'enregistre pas` | 404 | export demandé à une console construite sans enregistrement (tests) |
| `lecture des enregistrements deja en cours : reessayer dans un instant` | 503 | deux lectures du disque des enregistrements sont déjà en cours ; la requête est refusée tout de suite, jamais mise en attente |
| `le disque des enregistrements ne repond pas` | 504 | le disque n'a pas répondu en 5 s (liste) ou 120 s (archive) |
| `lecture des enregistrements impossible (<erreur>)` | 500 | le disque a refusé la lecture ou la construction de l'archive |

### Refus de la boucle (événement `refused`)

Démarrage (`describe_start_refusal`, `rider_age_refusal`, `describe_resolve_error`) :

| Message | Sens |
|---|---|
| `demarrage refuse : la machine est deja <état>` | une séance existe déjà |
| `demarrage refuse : cablage de l'arret d'urgence non atteste` | attestation manquante |
| `demarrage refuse : verdict <règle> a acquitter (<détail>)` | verdict en attente. Depuis le 25 octobre 2026 un démarrage demandé depuis la page sous un verdict verrouillé est refusé avant, par la console (tableau précédent) ; ce refus de la boucle reste pour un verdict non verrouillé, pour un verrou posé entre la réponse de la console et l'armement, et pour un lancement venu du tableau de bord sous un verdict verrouillé par la console elle-même (section 15) |
| `demarrage refuse : seuil <nom> different de celui du superviseur` | paliers cardiaques du profil ≠ ceux du fichier médical (`hr_hard_max_bpm` / `hr_critical_bpm`) |
| `demarrage refuse : variateur deja en marche (<n> tr/min), arret demande` | variateur trouvé en marche (processus précédent mort) ; la console le met à zéro et exige un acquittement |
| `demarrage refuse : variateur en defaut (<mnémonique>, LFT <code>)` | défaut variateur présent quand la boucle relit le variateur pour armer. Depuis le 11 octobre 2026 un démarrage demandé depuis la page est refusé avant, par la console (tableau précédent) ; ce refus de la boucle reste pour un défaut apparu entre la réponse de la console et l'armement, et pour un lancement venu du tableau de bord |
| `demarrage refuse : console hors service : 3 demarrages apres un arret non propre en moins de 10 min. Faire verifier la machine, puis la remettre en service depuis cette page, par un operateur nomme` | la console est hors service ([section 4](#pastille-service--en-service-ou-hors-service)) ; aucun départ, manuel ou programmé, n'est armé, avant toute autre porte |
| `demarrage refuse : espace disque insuffisant pour l'enregistrement de seance : <n> Mo libres sous <dossier>, 500 Mo requis. Liberer de l'espace` | moins de 500 Mo libres sous le dossier d'enregistrement ([section 16](#16-lenregistrement-de-séance-boîte-noire-locale)) |
| `demarrage refuse : enregistrement de seance impossible, espace libre illisible sous <dossier> (dossier absent, droits, disque)` | le dossier d'enregistrement ne peut pas être créé ou mesuré |
| `demarrage refuse : plus assez de fichiers libres pour l'enregistrement de seance : <n> inodes libres sous <dossier>, 36016 requis (une seance cree des milliers de petits fichiers). Liberer de l'espace` | il reste des octets, mais plus assez d'inodes libres pour les fichiers d'une séance ([section 16](#16-lenregistrement-de-séance-boîte-noire-locale)) |
| `demarrage refuse : enregistrement de seance impossible, espace libre sous <dossier> mesure il y a <n> s : le disque ne repond plus` | aucune mesure du disque depuis plus de 15 s, sans rien d'annoncé |
| `demarrage refuse : enregistrement de seance impossible pour l'instant : la fermeture d'un enregistrement est en cours sous <dossier>, derniere mesure de l'espace libre il y a <n> s. Reessayer dans un instant` | la console finit de fermer l'enregistrement précédent (ou `la purge des enregistrements deposes est en cours`) et une étape a duré plus de 15 s |
| `demarrage refuse : passager requis avec une personne a bord, a choisir dans la liste du tableau de bord` | aucun passager choisi (séance programmée, ou manuelle avec personne à bord) |
| `demarrage refuse : ce passager n'est pas dans la derniere liste du tableau de bord (droit retire, ou liste changee) : le choisir de nouveau` | la liste tenue ne porte pas (ou plus) ce passager |
| `demarrage refuse : liste des passagers pas encore recue du tableau de bord` | aucune réponse depuis le démarrage de la console |
| `demarrage refuse : liste des passagers perimee : recue il y a <n> min <s> s, au-dela de 15 min personne n'est propose` | dernière réponse reçue il y a plus de 900 s, sur l'horloge monotone ; l'âge est arrondi à la seconde supérieure (`15 min 1 s` dès que 900 s sont dépassées) |
| `demarrage refuse : aucun tableau de bord configure sur cette console : la liste des passagers vient du site` | console sans `MACHINE_API_KEY` |
| `demarrage refuse : le tableau de bord refuse la cle de cette machine : aucun passager n'est propose` | clé refusée (401, 403) à la dernière demande de liste |
| `demarrage refuse : FC max du passager non renseignee sur le site (a regler par un gestionnaire)` | séance programmée, FC max absente sur le site |
| `demarrage refuse : annee de naissance du passager non renseignee sur le site (a regler par un gestionnaire)` | séance programmée, ou manuelle avec personne à bord : année de naissance absente sur le site |
| `demarrage refuse : age du passager incalculable avec l'horloge de cette machine` | l'horloge du Pi et l'année de naissance ne donnent pas un âge plausible |
| `demarrage refuse : une seance de banc n'a personne a bord, donc aucun passager` | séance de banc qui nomme un passager |
| `demarrage refuse : age du passager requis pour une seance programmee` | lancement venu du tableau de bord sans âge |
| `demarrage refuse : passager de <n> ans, minimum <m> ans (min_rider_age des parametres medicaux)` | passager trop jeune (séance programmée, ou manuelle avec personne à bord) |
| `parametres medicaux non signes (<raison>) : console en mode banc seulement, aucune seance programmee ni personne a bord` | la signature du fichier médical ne s'est pas vérifiée au démarrage : séance programmée et personne à bord refusées ([raspberry-pi.md, 12.6](raspberry-pi.md#126-les-paramètres-médicaux-signés-anh-139)) |
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
| `consigne refusee : pas de frequence cardiaque utilisable, rien ne monte depuis l'arret. Attendre une frequence cardiaque fiable, puis redonner la cible` | personne à bord, aucune lecture acceptée par le suiveur, ou dernière acceptation âgée de plus de 2,5 s, aucun verdict : vérifier les électrodes, attendre la lecture, retaper la cible ; une fréquence encore affichée entre 2,5 et 4 s ne suffit pas |
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

Lien avec le tableau de bord (`dashboard`). Une offre distante reste sur Seance
jusqu'au prochain poll qui ne la propose plus ou jusqu'au démarrage. Le bouton local
**Demarrer la seance** est masqué tant que l'offre est affichée. La confirmation
est une annotation `warning`, préfixée `console:`, attribuée à l'alias du
confirmateur dans `actor` ; hors séance elle va au `logbook/events.jsonl`, et
l'enregistrement armé porte aussi `console: launch_confirmed` avec cet alias.
La commande effective reste écrite une fois sous son kind d'entrée.
Un refus du tableau de bord ou une demande sans réponse ne
soumet pas le démarrage à la machine et n'envoie pas `training/end`. Un refus local
avant la demande ou après le 2xx envoie une fin échouée; ce lancement est alors
définitif et le bouton reste désactivé. Après une absence de réponse ou un empêchement
temporaire, un nouvel essai exige une nouvelle confirmation explicite après
récupération, tant que le poll propose encore cette offre.

| Message | Sens |
|---|---|
| `serveur incompatible (contrat <X> vs <Y>)` | le tableau de bord ne parle pas la même majeure de contrat que cette console (X : la version de la console ; Y : celle du serveur, les majeures qu'il dit servir, ou `inconnu` s'il n'en annonce aucune ; au-delà de quatre majeures, les quatre premières puis le nombre des autres, par exemple `2, 3, 4, 5 et 596 autres`). La phrase fait 100 caractères au plus, quoi que le serveur envoie. Aucun lancement distant n'est armé tant que cela dure ; la console fonctionne comme sans tableau de bord. Mettre à jour le côté en retard. |
| `lancement expire : depart non confirme a temps (expired_before_start)` | le délai de confirmation côté site a expiré |
| `machine supprimee avant le depart (machine_deleted_before_start)` | la machine a été supprimée avant la confirmation |
| `machine passee dans une autre organisation avant le depart (organization_changed_before_start)` | la machine a changé d'organisation avant la confirmation |
| `droit retire avant le depart (right_revoked_before_start)` | le droit de lancer a été retiré avant la confirmation |
| `age du pratiquant refuse avant le depart (age_refused_before_start)` | l'âge du pratiquant ne permet plus le départ |
| `FC max du pratiquant modifiee avant le depart (hr_max_changed_before_start)` | la FC max a changé depuis la création du lancement |
| `pratiquant deja dans une autre seance en attente ou en cours : une seule a la fois (rider_busy)` | le pratiquant est déjà tenu par une autre séance |
| `depart distant refuse (HTTP <n>, <code>)` (ou `depart distant refuse (HTTP <n>)` sans code) | autre refus de `training/start`; le texte reçu du tableau de bord n'est pas repris |
| `depart distant non confirme : confirmation indisponible` | lien retenu, par exemple réseau, clé, délai, 429 ou 5xx ; aucun départ n'est armé |
| `route training/start inconnue` | le serveur ne connaît pas la route ; aucun départ n'est armé |

L'incompatibilité est rappelée toutes les 60 s tant qu'elle dure ; un refus de
lancement est écrit une fois pour cette séance. Entre deux nouvelles, l'état
se lit sur la pastille **Serveur** (section 4). Les événements `dashboard`
sont écrits dans le journal hors séance quand aucun enregistrement de séance
n'est ouvert.

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
| 202 | intention acceptée dans la boîte aux lettres ; la boucle n'a pas encore agi. Le corps porte `request_id`, l'identifiant de la demande ; ce que la boucle en fait arrive ensuite comme événement `taken`, `refused` ou `cancelled` portant le même identifiant |
| 400 | requête mal formée : nom vide, attestation incomplète, profil incohérent |
| 401 | jeton absent ou faux |
| 403 | mouvement désactivé, séances programmées désactivées, occupation refusée par configuration |
| 404 | profil inconnu, fichier statique absent, enregistrement inconnu |
| 409 | la machine n'est pas dans un état pour ça (dont : liste ou export des enregistrements pendant une séance). Un refus de la boîte aux lettres pour un démarrage, un arrêt, une cible ou un reset porte `{"detail", "code", "waiting_request_id"}` (voir plus bas). Le refus préalable d'un démarrage pour défaut variateur porte seulement `{"detail"}` |
| 412 | arrêt d'urgence non attesté depuis ce démarrage (même corps, `code` `estop_unattested`) |
| 422 | valeur inutilisable (profil rejeté, cible négative, occupation inconnue) ou corps JSON invalide (validation FastAPI) |
| 500 | stock de profils non inscriptible, lecture ou archive d'enregistrement impossible |
| 503 | lecture des enregistrements déjà en cours (deux au plus à la fois) |
| 504 | le disque des enregistrements ne répond pas |

### Public

| Méthode | Chemin | Rôle | Retour |
|---|---|---|---|
| GET | `/healthz` | vivacité pour le healthcheck du conteneur ; ne dit rien de la séance | 200 `{"status":"ok","service":"anheart-operator-interface"}` |
| GET | `/` | la page | 200, 404 si le fichier manque |
| GET | `/app.css` | feuille de style | 200, 404 |
| GET | `/app.js` | script | 200, 404 |

### Lectures (jeton si configuré)

`GET /api/status`, `GET /api/snapshot` et les images de `/ws/telemetry` portent
l'objet local `sto` : `state` (`ready`, `open`, `unknown`, `not_configured`), `source`
(`none`, `sim_ready`, `sim_open`, `drive_input`), `age_s` (dernière réussite, `null`
si absente), `refusal` (phrase commune du refus, `null` si prête), `stale_after_s`
(durée technique de validité, 2 s), `unknown_refusal` (phrase commune à afficher si
la lecture vieillit sur l'écran sans nouvelle réponse). `TelemetrySnapshot.sto` garde la lecture typée
et `sto_age` son âge au moment du snapshot. Aucun de ces champs ne part dans le
heartbeat Convex ni dans son objet strict `live` ; leur contrat reste inchangé.

| Méthode | Chemin | Rôle | Retour |
|---|---|---|---|
| GET | `/api/status` | tout ce que la mise en route demande : `at` (l'horloge de la machine à l'écriture de la réponse, celle du champ `at` des images et du reçu d'E-STOP ; donnée d'affichage, dont la page se sert pour ordonner ce qu'elle reçoit), `instance` (le nom que ce lancement de la console s'est tiré au hasard, 32 chiffres hexadécimaux : le même dans toutes ses réponses et toutes ses images, un autre après tout redémarrage ; donnée d'affichage), `run_state`, `estop_latched` (le drapeau de l'interface web, posé par sa route E-STOP seulement), `supervisor_estop` (l'arrêt d'urgence que le superviseur tient verrouillé, quel qu'en soit l'auteur, caméra comprise ; `null` sinon ; il reste lisible quand `go_silent` a pris la place de `standing` et de `floor`), `attested`, `attestation`, `attestation_statement`, `standing` (le verdict en vigueur, quel qu'en soit l'auteur, superviseur ou runtime : celui qu'une image prise à cet instant porterait), `floor` (le verdict verrouillé le plus sévère, quel qu'en soit l'auteur, arrêt d'urgence à part), `silenced` (le verdict verrouillé le plus sévère dont `go_silent` a pris la place, où qu'il soit tenu ; `null` hors `go_silent` et quand rien n'était verrouillé avant lui), `drive_state` et `drive_fault` (l'état du variateur et son défaut tels que la boucle les a lus en dernier, ceux du dernier instantané ; `comm_lost` avant le premier cycle et dès que cette lecture a plus de 2 s), `live` (verdicts actifs), `retained_hr_samples`, `pending` (la demande qui attend dans la boîte, avec son `request_id`), `requests` (ce que sont devenues les 16 dernières demandes auxquelles la console a répondu, la plus ancienne d'abord : `request_id`, `fate` parmi `taken`, `refused`, `cancelled`, `detail`, `at`, `wall_clock` ; aucun nom d'opérateur), `session_request` (l'identifiant du démarrage dont vient la séance en cours, de son acceptation au retour à `idle` ; `null` au repos), `attendant_last_seen`, `clients`, `evictions`, `ecg_fs_hz`, `ecg_seq`, `profile_rev`, `profile_ids`, `ports`, `bind`, `counters` | 200 |
| GET | `/api/snapshot` | dernier instantané de télémétrie (`null` avant la première tick) : `phase`, `mode`, `heart_rate`, `live_bpm`, `target_bpm`, `setpoint`, `measured`, `setpoint_confirmed`, `drive_state`, `drive_status_age_s`, `drive_status_stale`, `current_a`, `fault`, `safety`, `safety_action`, `safety_rank`, `counters`, `manual` (dont `ramping`, `ramp_eta_s` et `ramp_to`, la vitesse vers laquelle la consigne marche : la cible, 0 sous un `reduce`, `null` quand un `freeze` la tient), `instance` (le même nom que dans `/api/status`) | 200 |
| GET | `/api/ecg?after=<seq>&limit=<n>` | ECG récent par numéro de séquence ; `gap: true` si l'anneau a dépassé `after` | 200 |
| GET | `/api/panel` | panneau de liaison : `motion_enabled`, `programs_enabled`, `motor_backend`, `drive` (lectures, échecs, latence, dernière erreur), `ecg` (compteurs BITalino, DSP), `heart_rate_trend_bpm_per_min`, `manual_rise_hold` (ce par quoi la fréquence cardiaque retient une montée en séance manuelle : `no_heart_rate`, `trend_unknown` ou `heart_rate_falling` ; `null` si rien ne retient, hors séance manuelle en cours, et toujours avec une capsule vide), `radius_m`, `gear_ratio`, `motor_max_rpm`, `software_version` (le contenu de `raspberry-pi/VERSION`, ou `pi-unknown`), `dashboard` (le lien avec le tableau de bord : `state` parmi `reachable`, `unreachable`, `incompatible`, `key_refused`, `server_error`, `waiting`, `not_configured` ; `label`, le mot que la page affiche ; `detail`, le motif, une ligne de texte de 160 caractères au plus, éventuellement vide ; `last_answer_age_s`, `null` tant que le tableau de bord n'a jamais répondu) ; `null` sans console ; `service` (`out_of_service`, `unclean_starts`, `threshold`, `window_s`, `unclean_stop`, `open_record`, `persisted` : la console est-elle en service, et ce que son dernier démarrage a trouvé) ; `watchdog` (`enabled`, `sent`, `failed`, `last_error` : ce que la boucle de contrôle a dit au gestionnaire de service) | 200 |
| GET | `/api/camera` | `configured`, `camera`, `state`, `detail`, `latched_rule` | 200 |
| GET | `/api/sensors` | chaque canal acquis : fenêtre, qualité, mesures (surveillance seulement) | 200 |

### Profils

| Méthode | Chemin | Corps | Rôle | Retour |
|---|---|---|---|---|
| GET | `/api/profiles` | - | profils stockés et révision | 200 |
| PUT | `/api/profiles/{profile_id}?rev=<n>` | document de profil JSON (son `profile_id` doit égaler l'URL) | crée ou remplace ; validé par `parse_profile` | 200 ; 400 (id incohérent, document mal formé) ; 422 (profil rejeté, toutes les violations listées) ; 409 (révision dépassée) ; 500 (écriture impossible) |
| DELETE | `/api/profiles/{profile_id}?rev=<n>` | - | supprime un profil | 200 ; 404 ; 409 ; 500 |
| POST | `/api/plan/preview` | `{"profile_id", "total_duration_s"?, "subject_id"?}` | résout sans rien démarrer, ajusté à la FC max du passager nommé : phases et vitesses exprimées en moteur, sortie, Hz, g | 200 ; 404 ; 422 ; 409 (passager refusé) |

La page n'a pas d'éditeur de profils : `PUT` et `DELETE` ne sont accessibles que par l'API.

### Séance

| Méthode | Chemin | Corps | Rôle | Retour |
|---|---|---|---|---|
| GET | `/api/remote-launch` | - | l'offre du dernier poll pour la carte Seance : `{session_id, profile_id, program, launched_by, passenger_name, passenger_reason, state, reason, can_confirm}` ; `passenger_name` vient du roster tenu par recherche `subject_id`, jamais de `subject_label`; `null` si aucune offre | 200 |
| GET | `/api/feedback` | - | la carte « Retour du passager » (ANH-146) : `{current, given_up, scales, comfort, free_text_max}` ; `current` vaut `null` sans carte, sinon `{ended_at_ms, status, status_text, code, can_submit}` ; jamais les valeurs saisies | 200 |
| POST | `/api/feedback` | `{"nausea_score", "dizziness_score", "comfort", "free_text"?}` | saisit le retour de la séance de la carte, ou remplace la saisie pas encore envoyée ; mémoire seulement | 202 (même corps que la lecture) ; 400 `malformed_body` ; 422 `invalid_feedback` (le champ nommé, jamais sa valeur) ; 409 `nothing_to_fill` |
| POST | `/api/feedback/skip` | `{}` | ferme la carte : « non renseigné » si rien n'était saisi, rien n'est envoyé ; un retour saisi part quand même | 200 (même corps) ; 409 `nothing_to_fill` |
| POST | `/api/remote-launch/confirm` | `{"session_id", "operator"}` | confirme localement l'offre et attribue l'action à l'opérateur; la route ne fait aucun appel réseau, CloudSync traite ensuite la demande | 202 `{session_id, state}` ; 400 (opérateur vide) ; 409 (`launch_not_pending`, `launch_in_progress`, `passenger_unavailable`, `launch_refused`) |
| GET | `/api/roster` | - | la liste des passagers tenue (section 9) : `{state, detail, age_s, problem, riders: [{user_id, name, hr_max, age, programme}]}` ; `programme` vaut `null`, ou la phrase de refus d'une séance programmée pour ce passager | 200 |
| POST | `/api/session/start` | `{"profile_id", "operator", "total_duration_s"?, "subject_id"}` | démarre une séance programmée pour le passager nommé (identifiant de la liste) ; un `subject_age` est refusé | 202 `{kind, operator, detail, at, request_id}` ; 403 (mouvement ou programmes désactivés) ; 400 (nom vide, id mal formé) ; 404 ; 422 (dont : sans passager, âge saisi) ; 409 (dont : passager refusé, variateur en défaut) ; 412 |
| POST | `/api/session/stop` | `{"operator", "reason"?}` | fin sur la rampe mise en service | 202 `{kind, operator, detail, at, request_id}` ; 400 ; 409 |
| POST | `/api/session/estop` | `{"operator"?, "reason"?}` | verrouille l'arrêt d'urgence, **jamais refusé** | 200 `{operator, reason, at, wall_clock, action, rule, detail}` |

### Manuel et variateur

| Méthode | Chemin | Corps | Rôle | Retour |
|---|---|---|---|---|
| POST | `/api/manual/start` | `{"occupancy": "bench" \| "occupied", "operator", "subject_id"?}` | séance manuelle, cible 0 ; l'occupation est déclarée une fois ; `occupied` nomme un passager de la liste, d'au moins `min_rider_age` du fichier médical (vérifié par la boucle), `bench` n'en nomme aucun | 202 (même corps que ci-dessus) ; 403 (mouvement désactivé, occupation refusée) ; 422 (occupation inconnue, passager manquant ou nommé sur le banc) ; 409 (dont : passager absent de la liste, année de naissance absente, variateur en défaut) ; 400 ; 412 |
| POST | `/api/manual/target` | `{"output_rpm", "operator"}` | cible en tr/min **de sortie** ; la consigne y marche aux limites de mouvement | 202 (même corps) ; 422 (négatif, non fini) ; 400 ; 409 |
| POST | `/api/drive/fault-reset` | `{"operator"}` | reset nommé d'un défaut variateur | 202 (même corps) ; 400 ; 409 |

### La boîte aux lettres : une demande, un sort, et la console le dit

La boîte entre la page et la boucle a **une** place (`src/control_surface.py`). La
boucle attend le variateur pendant son cycle, et le serveur web répond pendant ce
temps : deux demandes peuvent se rencontrer dans un même cycle. Depuis le
11 octobre 2026, aucune ne remplace l'autre en silence. Ce que trouve une demande
qui arrive :

| Arrive | Acceptée quand | Sinon | Remplace ce qui attend ? |
|---|---|---|---|
| un démarrage (séance ou manuel) | attestation faite, aucun verdict verrouillé, **Etat** `idle` et boîte vide | 412 `estop_unattested` ; 409 `verdict_latched` ; 409 `start_pending` si un autre démarrage attend ou est armé ; 409 `machine_running` ou `machine_stopping` ; 409 `request_waiting` si un reset attend au repos | jamais |
| une cible | une séance tourne et la boîte est vide | 409 `no_manual_session` ; 409 `request_waiting` | jamais |
| un reset | boîte vide et aucun démarrage en cours | 409 `request_waiting` ; 409 `start_pending` | jamais |
| un STOP | une séance démarre ou tourne, **quoi qui attende dans la boîte** | 409 `nothing_running` (rien à arrêter) ; 409 `already_stopping` (un arrêt est déjà en cours) | oui : il prend la place d'un démarrage, d'une cible ou d'un reset qui attend, et la demande déplacée reçoit l'événement `cancelled` |
| un E-STOP | toujours ; il ne passe pas par la boîte | jamais refusé | non : ce qui attend reste, et la boucle le juge sous l'arrêt verrouillé (un démarrage est alors refusé par un événement `refused`) |

Un STOP n'est donc jamais refusé ni retardé par un démarrage en attente. Avant le
11 octobre 2026, un démarrage accepté pendant qu'un cycle était en cours était
oublié à la fin de ce cycle (**Etat** relisait `idle`, et un `session_idle` était
publié à tort) : un second démarrage était accepté par-dessus, sans rien publier
pour le premier. Un démarrage au repos prenait de même la place d'un reset en
attente. **Etat** garde maintenant `starting` tant qu'un démarrage accepté attend
ou est armé, et `stopping` tant qu'un arrêt accepté attend ; `session_idle` n'est
publié qu'une fois, quand plus rien d'accepté n'attend.

Chaque demande acceptée finit d'une seule façon, publiée une fois : `taken`,
`refused` ou `cancelled`. L'identifiant est fabriqué par la console (quelques
caractères tirés au hasard à chaque lancement, puis un compteur : `3eaa763d-5`), il
est opaque, ne dépend pas du nom de l'opérateur, et n'est qu'une donnée
d'affichage : aucune décision de la boucle n'en dépend, et il n'est pas écrit dans
l'enregistrement de séance. Tenu par `raspberry-pi/tests/test_request_fates.py`
(la table, chaque paire de demandes, et les courses rejouées avec un variateur
simulé dont un échange attend là où le test le décide).

Les codes d'un refus immédiat (`code` du corps 409 ou 412) : `start_pending`,
`request_waiting`, `machine_running`, `machine_stopping`, `nothing_running`,
`already_stopping`, `no_manual_session`, `verdict_latched`, `estop_unattested`.
`waiting_request_id` n'est renseigné que pour les deux premiers.

### Sécurité

| Méthode | Chemin | Corps | Rôle | Retour |
|---|---|---|---|---|
| POST | `/api/safety/attest` | `{"operator", "sto_jumper_removed", "mushroom_wired_nc"}` | attestation du câblage, pour ce démarrage | 200 ; 400 (une case manque, nom vide) |
| POST | `/api/safety/acknowledge` | `{"operator", "estop_released"?}` (défaut `false`) | efface les verdicts verrouillés | 200 `{operator, at, wall_clock, cleared}` ; 400 ; 409 |
| POST | `/api/presence` | `{"operator"?}` | signal de présence de l'accompagnant (règle `attendant_absent`) | 200 `{"at": …}` |
| POST | `/api/service/restore` | `{"operator"}` | remet en service une console hors service ; ne lance rien | 200 `{out_of_service, unclean_starts, threshold, window_s, unclean_stop, open_record, persisted}` (`persisted: false` : vaut jusqu'au prochain redémarrage seulement) ; 400 (nom vide) ; 409 (console en service, ou console qui ne garde pas d'état de redémarrage) |

### Enregistrements

| Méthode | Chemin | Rôle | Retour |
|---|---|---|---|
| GET | `/api/records` | les enregistrements de séance du disque, le plus récent d'abord : `recording` (cette console enregistre-t-elle), `records` (`name`, `closed` : `false` pour un enregistrement interrompu) | 200 ; 409 (séance en cours) ; 503 ; 504 ; 500 |
| GET | `/api/records/{name}/archive` | le dossier `name` en `.tar.gz` (`Content-Disposition: attachment`) | 200 ; 404 (nom inconnu, console sans enregistrement) ; 409 (séance en cours) ; 503 ; 504 ; 500 |

Ces deux routes lisent le disque sur deux fils qui leur sont réservés, jamais
sur la boucle de contrôle ni sur les fils du traitement ECG. 503 : les deux
sont pris, la requête est refusée tout de suite. 504 : le disque n'a pas
répondu à temps. Détail dans
[raspberry-pi.md](raspberry-pi.md#156-export).

### WebSocket `/ws/telemetry`

- Paramètre `?token=<jeton>` si `UI_TOKEN` est défini. Hors boucle locale, cette URL ne
  circule qu'à l'intérieur de TLS (`wss://`), et la console ne tient pas de journal
  d'accès : le jeton n'est lisible ni sur le réseau ni dans le journal du service.
- La poignée de main est refusée **avant** acceptation (code 1008) si l'en-tête `Origin`
  n'est pas dans la liste autorisée, ou si le jeton est faux. La liste : l'adresse
  d'écoute, `localhost` et `127.0.0.1` en boucle locale, et chaque nom de
  `UI_TLS_NAMES`, au port de la page. Une console en clair (boucle locale) les accepte
  en `http` et en `https` ; une console qui sert son certificat, donc toute console
  hors boucle locale, **en `https` seulement** : l'origine `http` de sa propre adresse
  y est refusée. Un client sans `Origin` (outil, test) est admis, mais reste soumis au
  jeton.
- Messages envoyés : `{"kind": "snapshot"}` (instantanés regroupés ; chacun porte
  `instance`, le nom du lancement de la console qui sert ce WebSocket), `{"kind": "event"}`
  (chaque événement : `kind`, `at`, `wall_clock`, `operator`, `detail`, et depuis le
  11 octobre 2026 `request_id`, l'identifiant de la demande dont l'événement parle,
  `null` sinon ; pour `session_running` et `session_idle`, celui du démarrage dont
  vient la séance ; depuis le 7 octobre 2026, un événement de type `recording` porte
  les messages de l'enregistrement de séance), `{"kind": "ecg"}` (tranche de l'anneau ECG après chaque instantané,
  jusqu'à 1500 échantillons à la connexion), `{"kind": "resync"}` quand l'écran a pris du
  retard ; le serveur ferme alors avec le code 1013
  (`this screen fell behind and was disconnected; reload to resync`).
- Un écran reçoit d'abord les événements qui l'attendent, puis l'instantané le plus
  récent : une image peut donc lui arriver après un événement plus récent qu'elle. La
  page écarte, pour ce qu'elle dit d'une demande, toute image prise avant la réponse.
- Observé en simulation sur 14 s : environ 5 instantanés et 5 trames ECG par seconde.

---

## 15. Écarts connus entre le code, la page et les README

- Le message 403 de mouvement désactivé (`MOTION_DISABLED_DETAIL`, `routes.py` :
  `mouvement desactive : cette console est en lecture seule. STOP, E-STOP, acquittement et lectures restent disponibles.`)
  et le bandeau « LECTURE SEULE » de la page existent encore, mais ni l'un ni l'autre ne
  sont atteignables avec `src.local_panel`, qui construit la console avec
  `motion_enabled=True`.
- Un défaut du variateur apparu au repos, sur une console qui n'a encore rien armé,
  ne lève toujours aucun verdict : la pastille **Securite** reste à `none`. Ce n'est
  plus caché à l'opérateur : le bandeau `VARIATEUR EN DEFAUT` le dit sur toutes les
  pages, et la console refuse le démarrage au clic, avec la raison (section 6). Aucune
  règle de sécurité n'a été ajoutée pour cela.
- Depuis le 25 octobre 2026, un démarrage demandé depuis la page alors qu'un verdict
  verrouillé par le runtime lui-même tient (`sto_open`, `drive_precommanded`, par
  exemple) est refusé au clic (409 `verdict_latched`, section 13), comme sous un
  verdict du superviseur ; jusque-là il était accepté (202), puis refusé un cycle plus
  tard par la boucle. Un lancement venu du tableau de bord sous un tel verdict ne
  passe pas par cette porte : la console accepte encore sa confirmation et annonce le
  départ au tableau de bord, puis la boucle le refuse. Rien n'est armé ; la
  vérification faite avant d'annoncer le départ ne lit que les verdicts du
  superviseur.
- Le bandeau `ARRET D'URGENCE VERROUILLE` garde trois cas, du côté prudent (section 5) :
  un WebSocket tombé sur une image qui montrait un arrêt verrouillé ; un arrêt
  d'urgence posé depuis un autre écran sous `go_silent`, vu au tour de statut suivant
  (les images sous `go_silent` ne disent rien de ce qu'il y a derrière) ; et le
  `quick_stop` d'une règle acquitté pendant que l'écran ne recevait plus d'images, puis
  suivi de `go_silent` dans la même console, qui garde son bandeau jusqu'au
  rechargement de la page. Pour ce dernier la console dit maintenant ce qu'il faut
  (« rien derrière `go_silent` ») ; la page ne s'en sert pas encore pour baisser le
  bandeau.
- Traités le 11 octobre 2026, côté console : `/api/status` porte le verdict en vigueur
  et le plancher quel qu'en soit l'auteur, runtime compris (section 11) ; il dit ce que
  `go_silent` a remplacé, et un écran ouvert ou rechargé sous `go_silent` affiche le
  bandeau du `quick_stop` d'une règle comme celui qui a vu la séquence (section 5) ;
  chaque réponse et chaque image portent le nom du lancement de la console, ce qui
  rend certaine la preuve d'un redémarrage, y compris quand l'ancienne console n'a
  répondu à aucun statut après le verrou ou que la nouvelle a déjà publié plus
  d'instantanés (section 5) ; la phrase anglaise `NOT LATCHED …` n'est plus ajoutée
  sur un bras manuel à l'arrêt dont la cible vaut 0 (section 11) ; la ligne `rampe` et
  le bandeau de rampe ne disent plus « en cours » vers la cible sous un `reduce`
  (section 6) ; la note d'un acquittement est jugée sur le statut quand aucune image
  n'arrive (section 11).
- Ce que la page ne peut toujours pas savoir. L'ordre entre une image et une réponse
  de statut, ou entre deux réponses, se juge à l'horloge de la machine ; un
  redémarrage du Pi (horloge repartie de zéro) est déduit d'une heure qui recule, pas
  d'un repère que la console donnerait. Le nom du lancement ne sert qu'à prouver un
  redémarrage pour le bandeau ; il n'ordonne rien. L'état du variateur dans
  `/api/status` est celui du dernier cycle de la boucle, pas une lecture faite pour
  cette réponse. Et sous `NO LIVE DATA` les pastilles alimentées par les images
  (Mode, Rotation, Securite) restent barrées : le statut ne les remplace pas.
- Traités le 10 octobre 2026 : les notes des boutons de démarrage et de reset, qui
  disaient `accepte : …` ou `reset demande : …` avant la réponse de la boucle
  (sections 6 et 9) ; la note d'une cible après un refus qui n'était pas le sien ou un
  double clic (section 6) ; la note `nothing is latched to acknowledge` restée après un
  nouveau verrou, et l'acquittement repris sans que la page le dise (section 11) ;
  l'onglet de la page d'un capteur, qui porte maintenant le titre de la page
  (`RESP · Respiration - AnHeart`) ; la place gardée pour le pied de page, mesurée au
  lieu d'être déclarée (elle valait 88 px pour un pied de page de 103 px, 92 px sur un
  écran étroit).
- Traités le 11 octobre 2026, côté console : chaque demande a un identifiant et un
  seul sort, que la console publie (section 14) ; un second démarrage n'est plus
  accepté par-dessus un premier ; le 409 d'une boîte prise ne contient plus la demande
  en attente ; un démarrage déplacé par un STOP reçoit sa réponse. La page n'a donc
  plus à deviner : le refus fait à un autre écran n'est plus écrit sous un bouton de
  celui-ci, la note `cible prise par la machine` n'est plus remplacée par ce refus,
  et la règle des deux secondes a disparu (section 6).
- Ce que la page ne peut toujours pas savoir, et pourquoi. (1) Une demande dont la
  réponse HTTP elle-même n'est pas arrivée (requête envoyée, 202 perdu) : l'écran n'a
  pas reçu l'identifiant, et ne peut reconnaître aucune réponse ; la note montre
  l'échec de la requête, alors que la console a pu accepter la demande. (2) Après une
  coupure de liaison, le sort d'une demande que la console ne liste plus : elle a été
  relancée, ou a répondu à plus de 16 demandes depuis ; la note dit alors que l'écran
  ne sait pas, et le reste. (3) Après une coupure, tant que `/api/status` ne répond
  pas, ni le sort d'une demande ni si la séance en cours est encore celle de cet
  écran. (4) Liaison coupée, la note `pris` déjà écrite reste telle quelle jusqu'à la
  première donnée du WebSocket suivant : le bandeau `NO LIVE DATA` dit alors que
  rien à l'écran n'est à jour. (5) La note d'une cible ne suit que les images pour
  savoir si la machine la tient encore : après une coupure, une cible égale appliquée
  par un autre écran garde la note `cible prise par la machine`. (6) Un STOP n'a pas
  de note : son sort (`taken`) n'est lu que dans **Evenements**, et le bandeau
  `STOP NON CONFIRME` (section 5) reste tenu par la réponse 202 et les images.
  (7) Une page rechargée ne sait plus quelles demandes étaient les siennes : les
  identifiants ne sont gardés que dans la page ouverte, et ses notes repartent vides.
- La console affiche `ARRET` pendant quelques secondes après un STOP qui a déplacé
  un démarrage, alors que rien n'a été armé : le runtime ouvre une fin de séance sur
  une machine au repos, puis revient à `REPOS` (22 cycles, soit 4,4 s, sur le variateur simulé).
  Pendant ce temps un démarrage reçoit 409 `machine_stopping`. C'était déjà le cas
  avant le 11 octobre 2026 ; relevé, non traité ici.
- Ces points relèvent de la page. Ils viennent du ticket
  [ANH-182](https://linear.app/anheart/issue/ANH-182/console-locale-suites-daffichage-apres-anh-124-anh-176-et-anh-178),
  qui a traité la note de la cible manuelle, l'encadré de la fréquence cardiaque, la
  demande E-STOP sans réponse, le verrou d'arrêt d'urgence dans `/api/status`, le
  bandeau de reprise automatique, les pastilles barrées et `perime` en orange.
- La page ne permet de déclarer que l'occupation BANC. L'API accepte `occupied`, refusée
  tant que `OCCUPANCY_OCCUPIED_ENABLED=false`, avec un passager de la liste.
- La liste des passagers de la page Seance n'a été montrée par aucun navigateur : elle est
  tenue par les tests de la page (`raspberry-pi/tests/web/panel_rider.test.mjs`) et de la
  console (`raspberry-pi/tests/test_panel_rider.py`, `test_roster.py`). Le bouton
  **Demarrer la seance** n'est pas désactivé sans passager : la console refuse (422) et la
  note le dit.
- La page n'a pas d'éditeur de profils, alors que l'API en fournit un (`PUT`/`DELETE`).
- L'estimation de rotation du capteur LUX ne suit pas le variateur simulé en simulation.

---

## 16. L'enregistrement de séance (boîte noire locale)

La console écrit chaque séance sur le disque du Pi pendant qu'elle se déroule :
ce que la machine a fait à chaque tic, les verdicts, les défauts du variateur,
les gestes de l'opérateur, les échantillons bruts du BITalino. C'est la boîte
noire de la machine. Le détail technique (file d'écriture, cadence, format) est
dans [raspberry-pi.md](raspberry-pi.md#15-lenregistrement-de-séance-boîte-noire-locale)
et [enregistrement.md](enregistrement.md).

> Vérifié par les tests automatiques sur la console en simulation, y compris
> les routes et le bouton d'export. **Pas** vérifié dans un navigateur, ni sur
> un vrai Pi avec sa carte SD.

**Où.** Un dossier par séance sous `raspberry-pi/data/records/` (clé
`RECORD_ROOT`), nommé par l'heure UTC du début. Le dossier est créé quand la
séance est armée et fermé quand elle est finie (ou à la sortie de la console).
Il n'y a rien à faire pour que l'enregistrement ait lieu, et rien à l'écran
tant qu'il se passe bien.

**Ce que l'opérateur peut voir.**

| Quand | Où | Texte |
|---|---|---|
| le disque est trop plein pour démarrer | liste **Evenements**, événement `refused` | `demarrage refuse : espace disque insuffisant pour l'enregistrement de seance : <n> Mo libres sous <dossier>, 500 Mo requis. Liberer de l'espace` |
| le dossier d'enregistrement est inutilisable | événement `refused` au départ, et événement `recording` dès le lancement de la console | `demarrage refuse : enregistrement de seance impossible, espace libre illisible sous <dossier> (dossier absent, droits, disque)` |
| le disque n'a plus assez d'inodes libres pour les fichiers d'une séance | événement `refused` au départ | `demarrage refuse : plus assez de fichiers libres pour l'enregistrement de seance : <n> inodes libres sous <dossier>, 36016 requis (une seance cree des milliers de petits fichiers). Liberer de l'espace` |
| le disque ne répond plus depuis plus de 15 s | événement `refused` au départ | `demarrage refuse : enregistrement de seance impossible, espace libre sous <dossier> mesure il y a <n> s : le disque ne repond plus` |
| la console finit de fermer un long enregistrement, ou purge des enregistrements déposés, et une étape dure plus de 15 s | événement `refused` au départ | `demarrage refuse : enregistrement de seance impossible pour l'instant : la fermeture d'un enregistrement est en cours sous <dossier>, derniere mesure de l'espace libre il y a <n> s. Reessayer dans un instant` (ou `la purge des enregistrements deposes est en cours`) |
| l'enregistrement se dégrade pendant une séance | événement `recording` | `enregistrement de seance degrade : …` (disque plein, droits, erreur d'écriture, file pleine, disque qui ne répond plus), toujours suivi de `La seance et la securite continuent.` |
| l'enregistrement redevient normal | événement `recording` | `enregistrement de seance retabli` |

**Un enregistrement dégradé n'arrête rien.** La séance continue, STOP et E-STOP
fonctionnent, toutes les règles de sécurité restent actives. Ce qui est perdu,
c'est une partie de la trace : le noter, finir ou arrêter la séance selon le
protocole du site, et s'occuper du disque avant la suivante. Le tableau de bord
reçoit le même signal (`recordDegraded` dans le battement de cœur), mais ne
l'affiche pas encore. Ce signal parle du dernier enregistrement : il redevient
faux quand la séance suivante s'enregistre normalement, sans redémarrer la
console.

**Le disque se remplit.** Aucun enregistrement n'est supprimé automatiquement
tant qu'il n'a pas été déposé hors de la machine et que ce dépôt n'a pas été
confirmé, et ce dépôt n'existe pas encore. Compter environ 40 Mo par séance de
30 minutes, et près de 9 000 petits fichiers. Sous 500 Mo libres, ou sous
36 016 inodes libres (la place des fichiers de deux séances d'une heure), la
console refuse de démarrer une séance : il faut alors exporter les
enregistrements utiles, puis supprimer leurs dossiers à la main sur le Pi.
Quand le dépôt existera, un enregistrement déposé et confirmé sera gardé
`RECORD_LOCAL_RETENTION_DAYS` jours (30 par défaut) puis supprimé.

**Hors séance.** Ce qui est demandé à la console quand aucune séance n'est
enregistrée (un départ refusé, un acquittement, un réarmement après la fin
d'une séance) est écrit dans un journal à part, `logbook/events.jsonl` sous le
dossier des enregistrements, borné à deux fichiers de 1 Mo
([raspberry-pi.md](raspberry-pi.md#159-le-journal-hors-séance)). Il n'a pas de
bouton d'export.

**Exporter.** Page Configuration, carte « Enregistrements de seance », bouton
`Exporter l'enregistrement` : la console prépare l'archive `.tar.gz` du
**dernier** enregistrement et le navigateur la télécharge sous le nom du
dossier. Seulement machine au repos : pendant une séance, la note dit
`liste des enregistrements refusee pendant une seance : attendre le retour au repos`.
Si le disque ne répond pas, elle dit `le disque des enregistrements ne repond pas`
ou `lecture des enregistrements deja en cours : reessayer dans un instant` :
attendre, ne pas multiplier les clics (deux lectures au plus sont en cours à la
fois, les autres sont refusées tout de suite, et la machine n'en est pas
ralentie). Les autres enregistrements s'exportent par l'API
(`GET /api/records`, puis `GET /api/records/{nom}/archive`).

**Qui peut lire.** Le dossier est réservé au compte qui lance la console
(mode 700), et chaque dossier et fichier que la console y crée l'est aussi
(700 et 600) ; la console ne retouche pas les enregistrements écrits avant ce
changement, que l'installation du Pi referme. Sur un Pi installé, la console
tourne sous le compte `anheart`, qui ne peut pas ouvrir de session : les
dossiers de séance lui appartiennent, et à lui seul. Un technicien les lit par
l'export ci-dessus, ou sur le Pi
avec `sudo` ([raspberry-pi.md](raspberry-pi.md#157-ce-que-lenregistrement-dit-des-personnes-et-sa-protection)).
Il n'est pas chiffré. Une archive exportée ne l'est pas non plus :
elle contient l'ECG brut de la séance et doit être traitée comme une donnée de
santé. Le nom de l'opérateur n'y figure pas (un alias le remplace), mais un
motif tapé à la main y est recopié : ne pas y écrire le nom d'un passager.

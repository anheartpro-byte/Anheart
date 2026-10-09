# Le Raspberry Pi : la console locale et la chaîne de sécurité

Ce document décrit le code Python de `raspberry-pi/` : ce que fait chaque
module, la boucle de contrôle, le superviseur de sécurité et toutes ses règles,
la gestion des défauts du variateur, la synchronisation avec le tableau de bord,
les capteurs, la caméra, la configuration, le contrat de code et
l'enregistrement de séance sur disque (la boîte noire locale).

Tout ce qui suit est tiré du code. Les termes techniques (LFT, ETA, LFRD, ttO,
STO, CiA402, latch…) sont définis dans le [glossaire](glossaire.md). L'usage de
la page web est décrit dans [console-locale.md](console-locale.md), la synthèse
des garanties dans [securite.md](securite.md), le côté Convex dans
[convex.md](convex.md) et les tests dans [framework-de-test.md](framework-de-test.md).

> **État réel, à lire d'abord.** Tout ce qui est décrit ici a été exercé en
> simulation (variateur simulé, BITalino simulé, physiologie simulée) et par les
> tests automatiques. Le variateur réel a été **lu** au banc (adresse Modbus 248,
> décalage de registres 0, rapport 49,79 compté à la main). **Aucune séance avec
> une personne à bord n'a eu lieu.** Le `Dockerfile` et le service systemd
> (`scripts/anheart.service`, qui lance cette image) démarrent la console
> `src.local_panel` ; l'installation est exécutée en CI, en simulation,
> **jamais sur un vrai Pi**. Procédure : [pi-image.md](pi-image.md) et
> [deploiement.md](deploiement.md#7-le-raspberry-pi).

## Sommaire

1. [Un seul programme](#1-un-seul-programme)
2. [Les modules](#2-les-modules)
3. [La boucle de contrôle](#3-la-boucle-de-contrôle)
4. [Les séances : manuelle et programmée](#4-les-séances--manuelle-et-programmée)
5. [Le superviseur de sécurité](#5-le-superviseur-de-sécurité)
6. [Les défauts du variateur (table LFT)](#6-les-défauts-du-variateur-table-lft)
7. [Ce qui se passe physiquement à l'arrêt](#7-ce-qui-se-passe-physiquement-à-larrêt)
8. [La synchronisation avec le tableau de bord](#8-la-synchronisation-avec-le-tableau-de-bord)
9. [Les capteurs BITalino](#9-les-capteurs-bitalino)
10. [La caméra (présence)](#10-la-caméra-présence)
11. [L'âge minimum du passager](#11-lâge-minimum-du-passager)
12. [La configuration (`.env`)](#12-la-configuration-env)
13. [Le contrat de code strict et la gate](#13-le-contrat-de-code-strict-et-la-gate)
14. [Versions et compatibilité](#14-versions-et-compatibilité)
15. [L'enregistrement de séance (boîte noire locale)](#15-lenregistrement-de-séance-boîte-noire-locale)

---

## 1. Un seul programme

| Commande | Programme | Rôle | État |
|---|---|---|---|
| `python -m src.local_panel` | la **console locale** | pilote le variateur, lit le BITalino, sert la page web de l'opérateur, applique la sécurité, se synchronise avec Convex | entrée du `Dockerfile`, dont `scripts/anheart.service` lance l'image ; également lançable à la main |

C'est la seule entrée du dossier : l'ancien enregistreur ECG et ses modules
(tampon SQLite, client Convex d'origine) ont été retirés. La console lit ses clés
dans `raspberry-pi/.env` (voir [section 12](#12-la-configuration-env)) : elle
charge ce fichier quel que soit le dossier courant, puis les variables du
processus **remplacent** celles du fichier (`load_environment` dans
`src/local_panel.py`).

---

## 2. Les modules

Le tableau suit l'ordre du flux : du fil de cuivre vers la décision.
« Porte 100 % » signifie que le module est sous la gate de couverture à 100 % des
branches (liste `[tool.coverage.report] include` de `raspberry-pi/pyproject.toml`).

### 2.1 Fondations

| Module | Rôle | Garanties |
|---|---|---|
| `src/units.py` | Types d'unités (`NewType`) : `Bpm`, `MotorRpm`, `OutputRpm`, `Seconds`, `Monotonic`, `Hertz`, `Amperes`, `GLoad`, `ResultantG`, `Metres`… et les conversions. | Une FC passée à la place d'une vitesse, ou des tr/min moteur confondus avec des tr/min de sortie (facteur 49,79), ne passent pas la vérification de types. Porte 100 %. |
| `src/result.py` | `Ok[T]`, `Err[E]`, `Result[T, E]`. | Les chemins moteur et sécurité renvoient des erreurs typées au lieu de lever des exceptions. Porte 100 %. |
| `src/clock.py` | Le seul module qui lit l'horloge (`RealClock`, `SimClock`, `ManualClock`). | Aucun autre module de `src/` n'appelle `time.monotonic()` ou `time.time()` (test par recherche de texte, `tests/test_clock.py`). Une séance de 30 min se simule en quelques secondes. Porte 100 %. |
| `src/geometry.py` | `MachineGeometry` : rayon (sans défaut) et rapport (49,79 par défaut). Toute vitesse et tout g passent par là. | Un seul endroit convertit tr/min moteur ↔ tr/min de sortie ↔ Hz ↔ g. Porte 100 %. |
| `src/local_config.py` | Lit et valide les clés `.env` de la console. | Signale **toutes** les erreurs d'un coup au démarrage. `ARM_RADIUS_M` n'a pas de défaut. Porte 100 %. |

### 2.2 Le variateur (`src/motor/`)

| Module | Rôle | Garanties |
|---|---|---|
| `motor/drive.py` | Le « contrat » du variateur, sans aucun accès au fil : états CiA402 (`DriveState`), mots de commande (`ControlWord` : `SHUTDOWN`=6, `SWITCH_ON`=7, `ENABLE_OPERATION`=15, `FAULT_RESET`=128), table des défauts (`DriveFault`, `LFT_FAULT_CODES`), carte des registres (`RegisterMap`), lecture des limites (`check_limits`). | Le décodage du mot d'état ETA ne lève jamais d'exception ; le bit de défaut est testé en premier. `check_limits` refuse d'armer si LSP ≠ 0 Hz, si HSP > tFr, ou si HSP > 50,0 Hz (`DEFAULT_MAX_MOTOR_HZ`). Porte 100 %. |
| `motor/atv320.py` | Le pilote Modbus RTU réel (seul module qui importe `pymodbus` et `serial`). | Un seul échange à la fois (RS-485 en semi-duplex). Aucun appel bloquant sur la boucle (exécuteur à un seul fil). Chaque écriture de vitesse est relue. `close()` met LFRD à 0, attend l'arrêt mesuré (RFRD), puis seulement écrit 7 puis 6. Après 3 échanges ratés d'affilée (`DEFAULT_FAILURE_THRESHOLD`), il cesse d'écrire. Porte 100 %. |
| `motor/ftdi_link.py` | Le câble Schneider USB-RS485 via `pyftdi` (macOS n'en crée pas de `/dev/cu.*`). URL : `ftdi://schneider:rs485/1`. | Seul module qui importe `pyftdi`/`usb`. Porte 100 %. |
| `motor/simulated.py` | Un ATV320 simulé : machine d'états CiA402 stricte, rampes, roue libre, chien de garde ttO. | Refuse un mot hors séquence. Rampe d'accélération 10 s pour 1380 tr/min, roue libre en `exp(-dt/20 s)`, ttO de 3 s (valeurs de modèle, pas des mesures). Porte 100 %. |

### 2.3 Le BITalino et l'ECG

| Module | Rôle | Garanties |
|---|---|---|
| `src/bitalino_client.py` | Acquisition BITalino (seul module qui importe `bitalino`). | Échantillons horodatés et étiquetés, compteurs de pertes (trames, octets sautés, échantillons comblés, reconnexions). Porte 100 %. |
| `src/bitalino_rfcomm_macos.py` | Transport RFCOMM via IOBluetooth sur macOS (seul module qui importe pyobjc, chargé à la demande). Adresse `rfcomm:98-d3-91-fe-4e-9f`. | Une liaison muette apparaît comme perdue. Porte 100 %. |
| `src/signal_processing.py` | L'ancien traitement ECG (BioSPPy) qui calcule la FC de régulation, et la note de qualité qui autorise ce calcul. | Une FC n'est extraite que d'une fenêtre notée `good`. Une fenêtre qu'il ne peut pas juger (valeur non finie, moins d'une seconde, tableau qui n'est pas à une dimension, test du secteur impossible à cette fréquence d'échantillonnage ou refusé par le filtre) est notée `no_signal`, jamais `good` ; la FC devient alors « périmée » comme pour toute autre perte. Le journal ne reçoit pas une ligne par fenêtre : un avertissement avec la raison quand une suite de fenêtres non jugeables commence, un autre avec leur nombre quand elle est finie (25 fenêtres jugées d'affilée, soit 5 s). Porte 100 %. **Pas encore** sous la vérification stricte des types. |
| `src/ecg_pipeline.py` | La frontière typée entre ce traitement et le runtime (`EcgBridge`). | Une FC n'est transmise comme utilisable que si (1) BioSPPy la juge `good`, (2) le processeur indépendant `sensors/ecg.py` juge **la même fenêtre** `GOOD`, (3) les deux FC diffèrent de 5 bpm au plus (`AGREEMENT_BPM`), et la fenêtre est continue. Sinon : `NOISY` sans FC, et la FC devient « périmée ». Porte 100 %. |
| `src/dsp.py` | Filtres, pics, spectres pour tous les capteurs (seul module qui importe `scipy`). | Porte 100 %. |
| `src/sensors/*` | Un processeur par voie (ECG, EDA, SpO2, RESP, EMG, LUX), un concentrateur (`hub.py`) rafraîchi chaque seconde. | Surveillance seulement ; aucune mesure ne commande le moteur (voir [section 9](#9-les-capteurs-bitalino)). Porte 100 %. |

### 2.4 La séance et la sécurité (`src/training/`)

| Module | Rôle | Garanties |
|---|---|---|
| `training/types.py` | Vocabulaire commun : `Phase`, `SafetyAction`, `SafetyVerdict`, `Occupancy`, `RunMode`, `TelemetrySnapshot`, `SignalQuality`. | L'ordre des membres de `SafetyAction` **est** la règle de priorité (testé). Porte 100 %. |
| `training/plan.py` | Profils d'entraînement (`TrainingProfile`), magasin de profils (`ProfileStore`), programme résolu (`Program`), estimation de FC max (Tanaka `208 − 0,7 × âge`). | Un profil incohérent ne peut pas exister (19 contrôles, tous signalés d'un coup). Porte 100 %. |
| `training/hr_control.py` | La loi de commande : filtre de FC (`HeartRateTracker`) et PI (`HeartRateController`). | Propose seulement ; n'a aucune autorité sur la sécurité. Porte 100 %. |
| `training/motion.py` | Le profileur de mouvement anti-nausée : accélération angulaire et dérivée de g. | Toute consigne non urgente respecte les deux limites (tests de propriétés). Porte 100 %. |
| `training/tracking.py` | L'enveloppe de vitesse où l'arbre peut légitimement être (pour `tracking_error`). | Suiveur au tiers de la rampe du variateur relue à l'armement (`TRACKING_RAMP_MARGIN` = 3). Porte 100 %. |
| `training/safety.py` | Le superviseur de sécurité (`SafetySupervisor`) : 18 règles indépendantes. | Il ne voit jamais la demande de la loi de commande ; son verdict l'emporte toujours (voir [section 5](#5-le-superviseur-de-sécurité)). Porte 100 %. |
| `training/runtime.py` | `TrainingRuntime` : le tic, l'armement, les arrêts, l'acquittement, le réarmement de défaut. | Les verdicts décident de la consigne ; aucun réarmement automatique de défaut ; aucune reprise après un verdict verrouillé ; une consigne revenue à 0 en cours de séance sans que personne l'ait demandé termine la séance (voir [5.1](#51-principes)). En séance manuelle, une cible non nulle est refusée, et celle déjà saisie remise à 0, tant que quelque chose retient une montée sur un bras à l'arrêt : un verdict, la fréquence cardiaque d'une personne à bord, un premier pas que le variateur n'a pas confirmé ([4.3](#43-séance-manuelle)). Un arrêt demandé (STOP, arrêt venu du site, cible manuelle à 0) fait descendre la consigne même sous FREEZE, et la descente prévue d'un programme (à partir de sa phase COOLDOWN) y est suivie aussi ([section 7](#7-ce-qui-se-passe-physiquement-à-larrêt)). Porte 100 %. |

### 2.5 La console et ses liens

| Module | Rôle | Garanties |
|---|---|---|
| `src/local_panel.py` | La racine de composition : construit et relie runtime, variateur, BITalino, capteurs, caméra, page web et lien Convex sur **une** boucle asyncio. | À la sortie, `needs_stop_before_release` distingue le repos confirmé ou la liaison non acquise (libération sans écriture) d'une inspection acquise mais non confirmée ou d'un runtime sorti de IDLE (passage par `shutdown()`). Porte 100 %. |
| `src/control_surface.py` | La boîte aux lettres entre la page web et la boucle : **un seul** ordre à la fois, plus le dernier instantané de télémétrie. | Aucun `await` (vérifié par test) ; l'E-STOP ne passe pas par la boîte aux lettres, il verrouille le superviseur tout de suite. |
| `src/cloud_sync.py` | Le lien avec le tableau de bord Convex : battement de cœur, programmes, lancements et arrêts venus du site, et ce que chaque réponse du serveur veut dire pour ce qui a été envoyé. | Ne peut pas arrêter la machine en tombant en panne ; ne peut pas lancer de séance manuelle ; n'arme rien d'un serveur d'une autre majeure de contrat ; ne garde aucune file en mémoire ; cherche l'arrêt demandé du site par une tâche à part, qui n'attend derrière aucun envoi ; arrête une séance lancée du site dont le site n'a pas pris le départ. Porte 100 %. |
| `src/record_uplink.py` | L'envoi des séances au tableau de bord, **relues sur le disque** : déclaration, télémétrie, événements, fin, avec un curseur par enregistrement. | La séance en cours d'abord ; rien n'est avancé sans acquittement ; rien n'est abandonné quand le lien retient ; toute lecture et toute écriture de curseur se fait sur un fil `record-io`, bornée en taille et en durée ([section 8](#8-la-synchronisation-avec-le-tableau-de-bord)). Porte 100 %. |
| `src/link_state.py` | L'état du lien que l'opérateur lit sur la page (pastille **Serveur**) : joignable, injoignable, incompatible, clé refusée, en erreur, en attente, non configuré. Tenu à partir de ce que les échanges du lien ont dit en dernier. | Aucune requête n'est faite pour lui et rien n'en tourne dans le tic de commande ; jamais « joignable » sans réponse depuis 25 s, jamais « injoignable » sur une seule requête perdue (tests de propriétés) ; le motif donné à la page est une ligne de 160 caractères au plus. Voir [section 14](#14-versions-et-compatibilité). Porte 100 %. |
| `src/contract.py` | Le contrat versionné de ce lien : version, en-tête, décision « ce serveur est-il de ma majeure ? », lecture de `VERSION`. | Seule une version bien formée de la même majeure est acceptée (test de propriété). Voir [section 14](#14-versions-et-compatibilité). Porte 100 %. |
| `src/telemetry.py` | Diffusion de la télémétrie vers les navigateurs connectés. | Le nombre de clients ne ralentit pas la boucle. |
| `src/panel_status.py` | Ce que la page montre des liaisons (variateur, BITalino), et la retenue d'une montée manuelle par la fréquence cardiaque. | Porte 100 %. |
| `src/presence/*` | Sûreté par caméra (seule une caméra **simulée** existe). | Voir [section 10](#10-la-caméra-présence). Porte 100 %. |
| `src/web/*` | L'API HTTP, la WebSocket, la page (`static/index.html`, `app.js`). | Tous les gestionnaires sont des coroutines ; hors boucle locale, un jeton de 16 caractères minimum est exigé. Détails : [console-locale.md](console-locale.md). |
| `src/sim/*` | Le sujet simulé (`physiology.py`, FC pilotée par le g réel), l'ECG synthétique (`ecg.py`, comptes ADC bruts), le BITalino simulé (`bitalino.py`) et les autres voies (`sim/signals/*`). | Le vrai traitement ECG travaille sur les comptes simulés. Porte 100 %. |

### 2.6 L'enregistrement de séance (`src/record/`)

| Module | Rôle | Garanties |
|---|---|---|
| `record/schema.py`, `rows.py`, `codec.py`, `ecg.py`, `writer.py`, `reader.py` | Le format d'enregistrement, schéma 2, partagé avec la simulation ([enregistrement.md](enregistrement.md)). | Le writer rend ses erreurs par `Result` ; rien n'est poussé vers le disque avant `sync()`. Porte 100 %. |
| `record/journal.py` | La file bornée et le fil d'écriture `record-journal` de la console. | Remettre une valeur ne prend ni verrou ni fichier et n'attend jamais ; `fsync` toutes les 2 s ; une erreur d'écriture coûte l'enregistrement, jamais la séance ([section 15](#15-lenregistrement-de-séance-boîte-noire-locale)). Porte 100 %. |
| `record/session.py` | L'enregistreur côté boucle : ce que la console voit, mis au format. | Ses points d'entrée ne lèvent jamais ; aucun nom d'opérateur n'est écrit. Porte 100 %. |
| `record/retention.py` | La rétention locale. | Un enregistrement sans dépôt confirmé n'est jamais purgé. Porte 100 %. |
| `record/cursor.py` | Le curseur de synchronisation d'un enregistrement : ce que le tableau de bord en a acquitté. | Un fichier à côté du dossier, jamais dedans ; écrit de façon atomique, en mode 600 ; un curseur illisible ne perd rien, il fait renvoyer l'enregistrement depuis son début ([8.1](#81-ce-qui-est-envoyé-vient-du-disque)). Porte 100 %. |
| `record/upload.py` | La relecture d'un enregistrement pour le tableau de bord : télémétrie à 1 Hz tirée de `ticks.csv`, événements de `events.jsonl` avec leur rang. | Seules les lignes complètes sont lues, une quantité bornée par appel ; relire donne les mêmes points aux mêmes dates ; ne lit ni `drive_frames.jsonl`, ni les blocs ECG, ni `sensors.csv`. Porte 100 %. |
| `record/export.py` | La liste des enregistrements et l'archive `.tar.gz` de l'un d'eux, pour les routes d'export, et les fils qui les lisent (`RecordIo`). | Lues sur deux fils réservés (`record-io`), jamais sur la boucle ni sur les fils du traitement ECG ; une lecture de plus est refusée, jamais mise en attente ; seul un dossier d'enregistrement peut être nommé ([15.6](#156-export)). Porte 100 %. |

---

## 3. La boucle de contrôle

### 3.1 Les tâches de la console

`LocalPanel.run` lance sur une seule boucle asyncio :

| Tâche | Période | Ce qu'elle fait |
|---|---|---|
| `control_step` | 0,2 s (5 Hz, `CONTROL_PERIOD`) | le tic de contrôle (ci-dessous) |
| `ecg_step` | 0,2 s (`ECG_PERIOD`) | garde le BITalino connecté (nouvel essai toutes les 5 s au plus, délai de connexion 30 s) et pompe les échantillons vers le traitement ECG (sur un fil de travail) |
| `sensor_step` | 1 s | retraite la fenêtre de chaque capteur |
| `presence_step` | 0,05 s (20 Hz) | lit la caméra et applique son verdict |
| `cloud_step` | 1 s | une étape d'envoi du lien Convex. Ce qu'elle envoie d'une séance est relu sur le disque par un fil `record-io` réservé : la tâche attend ce fil, la boucle non |
| `cloud_stop_step` | 50 ms | dit la séance en cours au tableau de bord (déclaration ou confirmation du départ), puis pose la question de l'arrêt demandé (toutes les 3 s en séance). Une tâche à elle : elle n'attend ni l'envoi, ni une retenue de l'envoi, ni le disque, et ne lit aucun fichier |
| serveur web | - | uvicorn, sur la même boucle |

Quand une tâche se termine (signal SIGINT/SIGTERM, exception, serveur web qui
ne peut pas s'attacher au port), toutes s'arrêtent et le variateur est relâché.
Code de sortie : 0 (normal), 1 (une tâche a échoué), 2 (configuration refusée).

`cloud_step` est la seule tâche qui avale ses exceptions : le tableau de bord
est un observateur, un bogue de report ne doit pas arrêter une séance.

Un fil à part, `record-journal`, écrit l'enregistrement de séance sur disque.
Ce n'est pas une tâche de la boucle : aucune des tâches ci-dessus n'ouvre ni
n'écrit un fichier d'enregistrement, elles remettent des valeurs à une file en
mémoire ([section 15](#15-lenregistrement-de-séance-boîte-noire-locale)).
`LocalPanel.run` le démarre, la sortie de la console l'arrête après avoir
arrêté le variateur.

### 3.2 Au repos : lecture seule

Tant qu'aucune séance n'a été armée, le runtime **lit** le variateur à 2 Hz
(`IDLE_POLL_PERIOD` = 0,5 s) et n'écrit rien. Exception : s'il trouve le
variateur en `OPERATION_ENABLED` ou l'arbre en rotation sans séance, il prend la
liaison, met la consigne à 0 (commande de marche conservée), verrouille
`drive_precommanded` (QUICK_STOP) et refuse tout départ jusqu'à un acquittement
nominatif.

### 3.3 Un tic de la console (`LocalPanel.control_step`)

1. Un E-STOP web est transmis au runtime (`request_estop`) ; le dernier signe
   de présence de la page est transmis (`note_presence`).
2. La boîte aux lettres est vidée et son ordre donné au runtime (départ
   manuel, départ programmé, cible manuelle, réarmement de défaut, fin de
   séance). Un refus revient à la page comme événement.
3. En simulation, le variateur simulé avance, et le sujet simulé reçoit la
   vitesse mesurée.
4. Le runtime fait son tic.
5. Le tic est remis à l'enregistrement de séance (en mémoire seulement), et ce
   que l'enregistrement a à dire à l'opérateur part vers la page comme
   événement `recording`.
6. L'instantané est publié vers la page.

### 3.4 Un tic du runtime (`TrainingRuntime._tick`), dans cet ordre

0. **Au repos seulement** : lecture du variateur (voir 3.2).
1. **Le keepalive d'abord** : écriture de la consigne déjà en vigueur (LFRD),
   puis une lecture d'état (ETA, RFRD, LCR, LFT). Si la boucle cale, les
   écritures cessent et le ttO du variateur l'arrête seul.
2. Deuxième mot d'un réarmement de défaut en cours (`SHUTDOWN` 0,2 s après
   `FAULT_RESET`).
3. Calcul de la phase de la séance. C'est là aussi que le runtime constate
   qu'une séance est finie : le premier tic où sa phase vaut `DONE`. Le
   constat est gardé jusqu'au départ suivant. Deux choses le lisent, avec la
   consigne en vigueur : `session_overrun`, qui ne juge plus une séance finie,
   et l'ouverture d'une fin de séance, qui ne se fait plus sur une séance
   finie (voir `session_overrun` en [5.2](#52-les-18-règles-du-superviseur)).
4. **Évaluation de la sécurité** (`SafetySupervisor.evaluate`) avec une
   observation qui ne contient **aucune** demande de la loi de commande.
5. **Commande** : un `match` sur l'action du verdict (table ci-dessous). La loi
   de commande n'est consultée que dans deux branches, et plafonnée.
6. `_settle` : si la phase ne demande plus de mouvement, la consigne est 0 et
   RFRD montre l'arrêt, alors `SWITCH_ON` (transition 5) puis `SHUTDOWN`
   (transition 2). Sinon rien n'est envoyé.
7. Compteurs de temps en zone, instantané.

Si le tic lève une exception : verdict `tick_exception` (GO_SILENT), consigne
mise à zéro de façon synchrone, silence, puis l'exception remonte et la console
s'arrête.

### 3.5 Les niveaux d'action

L'action la plus sévère l'emporte (`max` sur `SafetyAction`).

| Niveau | Rang | Ce que fait le runtime |
|---|---|---|
| `NONE` | 0 | Suit la loi de commande (programme) ou la cible de l'opérateur (manuel), au rythme du profileur de mouvement. |
| `FREEZE` | 1 | Garde la dernière consigne ; la loi de commande n'est pas consultée. Il ne retient pas un arrêt demandé : si une fin de séance est en cours (STOP à la console ou depuis le site, entre autres) ou si la cible manuelle vaut 0, la consigne descend vers 0 aux limites de mouvement, dès le tic suivant, comme sans verdict. Il ne retient pas non plus la descente prévue d'un programme : à partir de la phase COOLDOWN, la consigne descend de la même façon, dès le premier tic de cette phase. |
| `REDUCE` | 2 | Descend la consigne (programme : 15 tr/min moteur/s ; manuel : aux limites de mouvement) et continue de réguler sans jamais monter. |
| `RAMP_DOWN` | 3 | Fin de séance : phase COOLDOWN, consigne vers 0 par la rampe logicielle (programme 15 tr/min/s, manuel aux limites de mouvement). |
| `QUICK_STOP` | 4 | Consigne à 0 **tout de suite**, commande de marche **gardée** : le variateur décélère sur sa propre rampe mise en service (3-4 s). Ce n'est pas un arrêt « immédiat ». |
| `GO_SILENT` | 5 | Consigne à 0 une dernière fois (synchrone), puis **plus aucune trame**, lectures comprises. Le ttO du variateur prend l'arrêt en charge. Sans retour : ni acquittement, ni nouvel armement ; il faut redémarrer le processus. |

Un `FREEZE` ou un `REDUCE` qui n'est pas verrouillé se lève seul, et le runtime
suit de nouveau la loi de commande ou la cible, sans clic (voir
[5.1](#51-principes)). Cela vaut tant que le bras tourne. Une fois que le bras
a tourné dans une séance, une consigne qui revient à 0 sans que personne l'ait
demandé, sous `REDUCE` ou sous `NONE` par la loi de commande, termine la séance
sur le verrou `session_standstill` (voir
[5.2](#52-les-18-règles-du-superviseur)). Un STOP n'est pas dans ce cas, même
donné sous un `FREEZE` : la séance se termine sur `operator_stop`, sans verrou
de plus. Une cible manuelle mise à 0 pendant qu'un avertissement tient, `FREEZE`
compris, est suivie jusqu'à 0, et cette arrivée à 0 termine la séance sur le
même verrou (lecture prudente, voir
[securite.md](securite.md#77-un-arrêt-demandé-descend-toujours-même-sous-freeze-anh-175)).
Le retour au calme d'un programme n'est pas dans ce cas non plus, qu'il se
fasse sous un `FREEZE` ou sans verdict : la séance se termine sur
`programme_complete`, à la durée prévue, sauf si la cause d'un `FREEZE` non
verrouillé dure jusqu'au niveau suivant de sa règle, dont le RAMP_DOWN
verrouillé termine alors la séance comme avant
([securite.md](securite.md#78-la-descente-prévue-dun-programme-est-suivie-sous-freeze-anh-189)).

---

## 4. Les séances : manuelle et programmée

### 4.1 Occupation déclarée

Avant tout mouvement, l'opérateur déclare l'occupation, figée pour la séance :

| Occupation | Sens | Plafond (`LocalConfig.ceiling_for`) | Règles de FC |
|---|---|---|---|
| `bench` (banc) | personne à bord : moteur désaccouplé, ou bras accouplé capsule vide | `MOTOR_MAX_RPM` (300 par défaut, jamais plus de 1380) | désactivées (la FC n'est pas celle d'un passager) ; toutes les autres règles actives |
| `occupied` (personne à bord) | un passager | **refusé** tant que `OCCUPANCY_OCCUPIED_ENABLED=false` ; sinon le plus bas de `MOTOR_MAX_RPM` et du plafond « premier essai » 1,2 g résultant au rayon de référence (environ 990 tr/min moteur à 1,5 m) | toutes actives ; une FC fraîche et fiable est exigée pour démarrer |

La valeur 1,2 g est marquée `[MED][ING]` dans le code : un paramètre provisoire
en attente de validation médicale et d'ingénierie (jalon M6).

### 4.2 Portes communes à tout départ (`_refuse_arming`, `_arm`)

Dans l'ordre, rien ne touche le variateur avant les cinq premières :

1. Le runtime n'est ni arrêté (`shutdown`) ni silencieux.
2. **Attestation E-STOP** faite depuis ce démarrage du processus (texte exact :
   « a latching mushroom emergency stop is wired normally-closed into P24 -> STO
   and the STO jumper has been removed »), par un opérateur nommé.
3. Aucun verdict en vigueur (sinon il faut acquitter).
4. Runtime au repos (`IDLE` ou `FINISHED`).
5. Sur la console : au moins 500 Mo et 36 016 inodes libres sous le dossier
   d'enregistrement de séance, d'après la dernière mesure du fil du journal ;
   refus aussi si cet espace n'a pas pu être mesuré, ou si la mesure a plus de
   15 s (`RecordStorageLow`, [15.4](#154-départ-refusé-sous-500-mo)). Demandée en
   dernier, pour que l'opérateur entende d'abord ce que lui seul peut lever.
6. Ouverture de la liaison et lecture du variateur. S'il est en
   `OPERATION_ENABLED` : arrêt, verrou `drive_precommanded`, refus. S'il est en
   défaut : refus (`DriveInFault`), **sans réarmement automatique**.
7. Relecture de tFr, HSP, LSP, ACC, dEC ; refus si LSP ≠ 0, HSP > tFr ou
   HSP > 50,0 Hz. ttO et SLL ne sont **pas** relus (adresses Modbus non
   vérifiées) : chaque armement écrit un avertissement dans le journal demandant
   de les vérifier au clavier.
8. Mise sous tension de l'étage de sortie : LFRD = 0, puis `SHUTDOWN` →
   `SWITCH_ON` → `ENABLE_OPERATION`. Si le mot d'activation échoue sans réponse,
   la sortie est supposée active : verrou `enable_unconfirmed` (QUICK_STOP).

### 4.3 Séance manuelle

* Démarrée **uniquement depuis la console** (`/api/manual/start`). Rien dans le
  lien Convex ne sait en construire une.
* Refusée si l'opérateur n'est pas nommé, si le plafond est hors de
  `[min_run, HSP]`, ou (personne à bord) sans FC fiable.
* L'opérateur fixe une **cible en tr/min de sortie** ; elle doit valoir 0 ou,
  convertie en tr/min moteur, être dans `[55, plafond]`. Hors de ce domaine elle
  est **refusée**, jamais arrondie (ex. 0,5 tr/min de sortie = 25 tr/min moteur,
  refusé).
* Sans verdict imposant une autre consigne, celle-ci marche vers la cible au
  rythme du profileur (0,25 tr/min de sortie/s = 12,4 tr/min moteur/s,
  et 0,03 g/s), montée comme descente.
* **Aucune cible n'attend sur un bras à l'arrêt** (ANH-178). Tant que quelque
  chose retient une montée et que la consigne appliquée vaut 0, une cible non
  nulle est refusée (`set_manual_target` rend `HeldAtStandstill`, qui porte ce
  qui retient). Une cible déjà saisie est remise à 0 au tic où quelque chose
  retient le bras à l'arrêt (`_withdraw_waiting_target`), et la console le dit
  une fois (`cible de <n> tr/min moteur remise a 0 : …`), sauf au tic où un
  avertissement vient lui-même d'amener la consigne à 0 : la séance se termine
  au tic suivant et cette fin le dit. À la fin de chaque tic : consigne à 0 et
  montée retenue, donc cible à 0. Ce qui retient, et ce que dit le message
  avant « puis redonner la cible » :
  * un verdict, verrouillé ou non : `le verdict <règle> tient le bras a
    l'arret. Attendre qu'il soit leve` (verrouillé : `L'acquitter une fois sa
    cause levee`) ;
  * personne à bord, pas de FC utilisable (aucune lecture fiable depuis plus
    de 4 s) : `pas de frequence cardiaque utilisable, rien ne monte depuis
    l'arret. Attendre une frequence cardiaque fiable` ;
  * personne à bord, tendance de la FC inconnue (moins de 5 lectures depuis le
    début de l'historique : premières secondes de l'ECG, ou après un saut
    confirmé) : `tendance de la frequence cardiaque pas encore connue, rien ne
    monte depuis l'arret. Attendre quelques secondes de lecture` ;
  * personne à bord, FC en baisse de plus de 20 bpm/min sur 5 lectures (garde
    vasovagale) : `la frequence cardiaque baisse trop vite, rien ne monte
    depuis l'arret. Attendre qu'elle se stabilise` ;
  * le variateur n'a pas confirmé l'écriture du premier pas : `le variateur
    n'a pas confirme la consigne, elle n'est pas redemandee. Verifier la
    liaison`. Ce cas ne peut pas être refusé à la saisie : la cible est
    acceptée, le pas est demandé une fois, puis elle est retirée. Le message
    ne dit pas que le bras est resté à l'arrêt : si la trame est arrivée et
    que seule sa réponse s'est perdue, le variateur garde ce pas pendant un
    tic, jusqu'au zéro du maintien de liaison suivant, et le runtime ne peut
    pas distinguer les deux cas.

  Les trois raisons de FC (`RiseHold`) ne sont pas des verdicts : rien n'est à
  acquitter, l'opérateur attend ce que dit le message et retape sa cible. Ni
  un avertissement qui se lève, ni un acquittement, ni une FC qui revient ou
  se stabilise ne mettent donc le bras en mouvement. Une cible de 0 est
  toujours acceptée. Décisions du 6 octobre 2026, mesures et coûts dans
  [securite.md](securite.md#76-aucune-cible-manuelle-nattend-sur-un-bras-à-larrêt-anh-178).

  La page affiche ces trois raisons avant qu'une cible soit tapée.
  `TrainingRuntime.manual_rise_hold()` rend la réponse de la garde
  (`_heart_rate_hold`) à l'instant de l'appel, comme le fait le refus : `None`
  hors séance manuelle en cours, et toujours avec une capsule vide. La console
  la publie dans `/api/panel` (`manual_rise_hold`). La garde n'est énoncée
  qu'à cet endroit : la page ne la recalcule pas.
* **Rien ne change pour un bras qui tourne.** Une cible tapée pendant qu'un
  FREEZE, un REDUCE, la garde de la FC (pas de FC utilisable, tendance inconnue
  ou en baisse rapide) ou un variateur qui ne confirme pas retiennent la
  montée d'un bras en mouvement est acceptée et gardée ; la consigne reste où
  elle est, puis suit la cible quand la retenue disparaît, sans clic à cet
  instant. Capsule vide, la FC ne retient rien. Sous un FREEZE, une cible plus
  basse mais non nulle est tenue de la même façon. Seule une cible de 0 y est
  suivie tout de suite : c'est un arrêt demandé (section 7).
* **Départ normal avec une personne à bord** : l'ECG tourne avant le départ,
  la FC est utilisable et sa tendance connue. Si elle est stable, la première
  cible est acceptée et, avec les limites livrées, le premier pas est écrit au
  tic suivant, ou au second quand le profileur n'a pas encore de base de temps
  (premier tic de la séance, tic qui suit un FREEZE). La garde vasovagale se
  ferme aussi sur une variation ordinaire de deux battements en cinq
  secondes : la cible est alors refusée et se retape (coût estimé dans
  securite.md, 7.6).
* Phase HOLD pendant toute la séance ; au bout de 3600 s
  (`MANUAL_SESSION_LIMIT`), le runtime demande la fin de séance comme un STOP :
  la consigne descend, même sous FREEZE ([section 7](#7-ce-qui-se-passe-physiquement-à-larrêt)).
  Cette fin est jugée par `session_overrun` sur sa propre échéance, et non
  sur les 3600 s qu'elle vient d'atteindre : la limite, plus la descente
  attendue depuis la vitesse en vigueur à cet instant et la rampe du
  variateur, plus la récupération, plus 30 s. Une séance menée à sa limite se
  termine donc sans alerte, à grande vitesse aussi : voir
  [5.2](#52-les-18-règles-du-superviseur).
* Après une fin de séance, la cible vaut 0 : une nouvelle séance exige un
  nouveau départ. Une consigne ramenée à 0 par un avertissement termine la
  séance sur le verrou `session_standstill`, capsule vide comprise (voir
  section 5). Une cible que l'opérateur met lui-même à 0, sans avertissement,
  ne la termine pas : il peut redonner une cible. Mise à 0 pendant qu'un
  avertissement tient (un FREEZE compris, sous lequel elle est maintenant
  suivie), elle termine la séance sur ce même verrou quand la consigne arrive
  à 0.

### 4.4 Séance programmée (AUTO)

Portes supplémentaires, dans l'ordre (`LocalPanel._start_programme`) :

1. `PROGRAMS_ENABLED=true` (sinon : « seances programmees desactivees
   (PROGRAMS_ENABLED=false, jalon M5) : utiliser MANUEL »).
2. Âge du passager connu et ≥ `MIN_RIDER_AGE` (voir [section 11](#11-lâge-minimum-du-passager)).
3. Plafond « personne à bord » disponible (`OCCUPANCY_OCCUPIED_ENABLED=true`) :
   un programme a toujours quelqu'un à bord.
4. Le profil se résout, ajusté à la FC max de ce passager si elle est connue ;
   tous les contrôles cardiaques sont refaits pour cette personne (notamment
   `zone_high ≤ 0,9 × FC max`).
5. `max_rpm` du profil ≤ plafond personne à bord.
6. Caméra (si configurée) : la porte de départ accepte.
7. Portes du runtime : celles de 4.2, plus les paliers cardiaques du profil
   **égaux** à ceux du superviseur (`HR_HARD_MAX_BPM` / `HR_CRITICAL_BPM`), et un
   plan de commande constructible.

Un refus s'affiche sur la console et, pour un lancement venu du tableau de bord,
est renvoyé comme séance échouée avec la raison.

**Phases.** Durées prises dans le profil :

| Phase | Ce qui se passe |
|---|---|
| `BASELINE` | Moteur arrêté. La FC de repos est mesurée (dernière valeur fraîche et fiable). Si aucune n'est mesurée, la machine reste à l'arrêt : aucune FC de repos n'est inventée. |
| `WARMUP` | La consigne monte vers la zone, plafonnée à `floor(max_rpm × warmup_rpm_ceiling_fraction)`. Se termine tôt dès que la FC atteint `zone_low_bpm`. |
| `HOLD` | La loi de commande module la vitesse pour tenir la FC dans la zone. Si elle ramène la consigne jusqu'à 0 (FC durablement au-dessus de la zone), la séance se termine sur le verrou `session_standstill` : elle ne se repose pas à 0 pour repartir ensuite (voir [5.2](#52-les-18-règles-du-superviseur)). |
| `COOLDOWN` | Consigne vers 0 (fin normale ou verdict). Se termine sur arrêt confirmé, ou au bout de la durée de cooldown du profil. |
| `RECOVERY` | Moteur arrêté, passager toujours à bord, **toutes les règles de FC restent actives** (phase au plus fort risque vasovagal). |
| `DONE` | Programme terminé. Les règles de FC et `attendant_absent` s'arrêtent. `session_overrun` ne juge plus la séance dès que la consigne en vigueur est 0 : une séance finie n'est plus mesurée contre sa durée (voir [5.2](#52-les-18-règles-du-superviseur)). |

**Loi de commande** (`hr_control.py`) :

* PI en forme incrémentale (vitesse), recalé à chaque tic sur la consigne
  réellement appliquée : pas d'intégrateur séparé qui s'emballe, reprise sans à-coup
  après un FREEZE.
* Pas de terme dérivé.
* Une décision toutes les **5 s** (la boucle tourne à 5 Hz, les tics
  intermédiaires répètent la demande).
* Gains **estimés**, en attente d'un essai indiciel sur la vraie machine :
  Kp = 3 tr/min par bpm, Ti = 40 s.
* Bande morte asymétrique : la correction commence 3 bpm sous le haut de zone,
  mais tolère la dérive jusqu'à 1 bpm au-dessus du bas.
* Pente max du programme : 15 tr/min moteur/s (`RUNTIME_LIMITS.slew`) ; chaque
  changement non urgent passe en plus par le profileur anti-nausée.
* **Garde vasovagale** : la consigne ne peut pas **monter** tant que la pente de
  la FC sur les 5 dernières lectures acceptées est inférieure à −20 bpm/min, ou
  inconnue (`RuntimeLimits.falling_trend`, `trend_samples`).
* Filtre d'entrée (`HeartRateTracker`) : FC hors de 25..240 bpm rejetée, saut de
  plus de 25 bpm entre deux lectures rejeté (réinitialisé après 3 rejets
  consécutifs), médiane de 3 lectures, une lecture n'est « fraîche » que 4 s
  (`HEART_RATE_STALE_AFTER`), et seulement si son numéro de séquence a avancé.

**Profils livrés** (`config/profiles.default.json`) : `standard_30_min` et
`standard_45_min`, zone 118-138 bpm, paliers 148/158, `max_rpm` 276 tr/min
moteur (5,5 tr/min de sortie). Ils **ne peuvent pas atteindre leur zone** sur le
sujet modélisé (constat 7 de `simulation/README.md`) : ce sont des profils de
banc. La zone « jog » 145-155 bpm n'est pas livrée ; elle existe dans les
scénarios de simulation. La console copie les profils livrés dans
`data/profiles.local.json` (dossier ignoré par git).

---

## 5. Le superviseur de sécurité

`src/training/safety.py`, une instance partagée entre le runtime et la page web.

### 5.1 Principes

* **Il ne voit pas la demande de la loi de commande.** `SafetyObservation` ne
  contient ni vitesse désirée ni décision ; seulement des mesures (dont
  `commanded_rpm`, ce qui a été écrit au variateur, et la dernière lecture de
  FC utilisable, `last_usable_heart_rate`) et quelques constats du
  runtime sur ce qu'il a lui-même écrit ou décidé (la consigne est en rampe ;
  la consigne est revenue à 0 sans que personne l'ait demandé, champ
  `stopped_by` ; la séance est finie, champ `session_over` ; une fin de
  séance est ouverte, champ `ending`). Aucun de ces
  champs n'est une demande, et `stopped_by` ne peut qu'ajouter un verdict.
  `session_over` ne fait taire qu'une règle, `session_overrun` : il réunit
  deux faits (la séance a atteint la phase `DONE` depuis son départ, et la
  consigne en vigueur est 0) et vaut « non » par défaut. `ending` ne peut que
  retarder cette même règle, d'une durée finie, jamais l'avancer ni la faire
  taire : il porte trois valeurs fixées à l'ouverture de la fin (l'instant,
  la descente attendue depuis la consigne en vigueur à cet instant, la durée
  de la récupération) et vaut « aucune fin » par défaut. Raison de la
  première phrase : lors d'un malaise
  vasovagal la FC **baisse** ; la loi de commande y lit « sous la zone » et
  veut accélérer.
* **Il ne touche jamais le fil** : pas de Modbus, pas d'`await`. C'est une
  fonction pure des mesures et de son historique.
* **Chaque règle est indépendante**, puis on garde la plus sévère.
* **Deux niveaux de persistance** :
  * **verrouillé (latch)** : le verdict monte dans un « plancher » qui ne
    redescend **que** par un acquittement humain nominatif. Tout ce qui est au
    niveau RAMP_DOWN ou au-dessus est verrouillé, ainsi que `comms_lost`,
    `operator_estop`, `loop_stall` et toute alerte venue d'un autre fil ;
  * **non verrouillé** : FREEZE ou REDUCE réévalué à chaque tic, qui disparaît
    quand sa cause disparaît (ex. une électrode qui revient). Raison écrite dans
    le code : si chaque coupure de 10 s exigeait un clic, l'opérateur cliquerait
    par réflexe, y compris sur un défaut variateur. **Quand il disparaît, si
    la séance est encore active, la consigne suit de nouveau seule la
    régulation ou la cible**, sans geste de l'opérateur : c'est voulu tant que
    la vitesse a seulement été maintenue ou baissée, et la phrase du verdict le
    dit à l'écran (`SELF_CLEARING` : « NOT LATCHED: it lifts by itself… »)
    tant que la séance peut encore prendre de la vitesse. Cela s'arrête à
    l'arrêt : une fois que le bras a tourné dans une séance, une consigne qui
    revient à 0 sans que personne l'ait demandé, amenée par un REDUCE ou par la
    régulation cardiaque elle-même, termine la séance sur le verrou
    `session_standstill` (section 5.2). Ce sont les décisions des 5 et 6
    octobre 2026
    ([ANH-176](https://linear.app/anheart/issue/ANH-176/le-bras-peut-repartir-seul-en-cours-de-seance-quand-un-avertissement),
    [securite.md](securite.md#7-décisions-des-5-et-6-octobre-2026-sur-les-reprises-automatiques)).
    Ce que la règle ne couvre pas y est listé : le premier mouvement d'une
    séance programmée, même après un avertissement pendant la BASELINE. En
    séance manuelle, rien n'attend sur un bras à l'arrêt, ni derrière
    l'avertissement ni derrière la fréquence cardiaque : la cible y est
    refusée ou remise à 0 (voir [4.3](#43-séance-manuelle)), donc la cible
    « suivie de nouveau » y vaut 0.
* **Acquittement** (`acknowledge`) : exige un nom ; refusé si rien n'est
  verrouillé ; **refusé pour GO_SILENT** (définitif) ; refusé tant que
  l'opérateur n'a pas déclaré le champignon d'arrêt d'urgence relâché
  (`estop_released=True`) si un E-STOP est verrouillé. Acquitter pendant que la
  cause est encore là est permis : la règle se redéclenche au tic suivant.
  En séance manuelle, acquitter un verdict qui tenait un bras à l'arrêt ne met
  rien en mouvement : aucune cible n'a pu y rester en attente
  ([4.3](#43-séance-manuelle)). Deux cas où le mouvement suit un acquittement
  restent, comme avant : sur un bras qui tourne, acquitter un FREEZE verrouillé
  laisse la consigne suivre de nouveau la régulation ou la cible ; et un
  programme tenu avant son premier mouvement fait ensuite son départ normal.
* Les règles de FC (`hr_*`) ne jugent pas en phase `DONE` ni en occupation
  `bench`.

### 5.2 Les 18 règles du superviseur

Valeurs par défaut de `SafetyLimits`. Seuls `hard_max_bpm` et `critical_bpm`
n'ont pas de défaut dans la classe ; la console les prend de `HR_HARD_MAX_BPM` /
`HR_CRITICAL_BPM` (148 / 158 par défaut).

| Id | Condition | Seuils par défaut | Action | Verrou |
|---|---|---|---|---|
| `operator_estop` | E-STOP de la page (ou autre appel à `latch_estop`) | aucun délai | QUICK_STOP | oui ; l'acquittement exige « champignon relâché » |
| `drive_fault` | l'état CiA402 lu est FAULT | aucun délai | RAMP_DOWN (le variateur a déjà appliqué sa propre réaction) | oui ; se redéclenche tant que le défaut est affiché |
| `comms_lost` | échanges ratés d'affilée ≥ seuil | 3 échanges | GO_SILENT | oui, définitif |
| `hr_drop` | chute confirmée de FC que la baisse de charge n'explique pas (détail ci-dessous) | fenêtre 30 s ; 25 bpm ; médianes de 5 (niveau) et 9 (pic) ; 4 lectures consécutives ; marge repos 15 bpm ; fenêtre de charge 120 s | RAMP_DOWN | oui |
| `hr_hard_max` | FC > palier dur pendant 5 s continues (bande de relâche 5 bpm). La FC jugée est la dernière FC utilisable : une lecture sans FC ne relance pas le délai (détail ci-dessous) | 148 bpm, 5 s ; dernière FC utilisable jugée jusqu'à 10 s d'âge | RAMP_DOWN | oui |
| `hr_critical` | FC ≥ palier critique, sur la même FC jugée que `hr_hard_max` | 158 bpm, aucun délai ; dernière FC utilisable jugée jusqu'à 10 s d'âge | QUICK_STOP | oui |
| `hr_rate` | pente (moindres carrés) des médianes glissantes de 5 lectures sur 60 s, étendue ≥ 20 s, > limite | 25 bpm/min ; relâche sous 15 bpm/min | REDUCE | non |
| `hr_stale` | aucune FC fraîche et fiable depuis… | > 10 s FREEZE ; > 30 s REDUCE ; > 60 s RAMP_DOWN | FREEZE → REDUCE → RAMP_DOWN | seulement le niveau RAMP_DOWN |
| `hr_unresponsive` | sur 300 s (étendue ≥ 240 s, ≥ 10 points par moitié) la charge moyenne monte d'au moins 0,08 g, la seconde moitié est à ≥ 0,15 g, et la FC moyenne monte de moins de 3 bpm | 300 s, 0,08 g, 0,15 g, 3 bpm | REDUCE | non |
| `current_high` | courant moteur (LCR) > seuil d'alerte pendant 10 s (relâche 0,2 A sous le seuil) ; ou > seuil de coupure | alerte 2,4 A / 10 s ; coupure 3,2 A immédiat (plaque 2,15 A) | REDUCE (alerte) ; RAMP_DOWN (coupure) | alerte non ; coupure oui |
| `no_load` | consigne ≥ 100 tr/min, sortie active, courant < 0,2 A pendant 3 s (phase ouverte, pas de moteur, mauvais registre) | 0,2 A, 100 tr/min, 3 s | RAMP_DOWN | oui |
| `tracking_error` | vitesse mesurée hors de l'enveloppe de plus de 60 tr/min moteur pendant 2 s, sortie active | 60 tr/min, 2 s | RAMP_DOWN ; **GO_SILENT** si l'écho LFRD est aussi faux depuis 1 s | oui |
| `reverse_rotation` | vitesse mesurée de signe opposé à la consigne et > 10 tr/min | 10 tr/min, aucun délai | QUICK_STOP | oui |
| `session_overrun` | séance en cours (le runtime ne l'a pas constatée finie : `session_over` faux) **et** durée écoulée depuis le départ > échéance + 30 s. L'échéance est la durée du programme (3600 s en séance manuelle) ; pour une séance qui a ouvert une fin de séance à temps, la plus tardive de cette durée et de l'échéance de la fin (ouverture + descente attendue depuis la consigne en vigueur à l'ouverture, plus la récupération une fois la consigne à 0) | 30 s | RAMP_DOWN | oui ; se redéclenche tant que la séance n'est pas finie |
| `loop_stall` | écart entre deux tics > 3 périodes ; > 15 périodes | 0,6 s → FREEZE ; 3,0 s → GO_SILENT | FREEZE ou GO_SILENT | oui (les deux) |
| `attendant_absent` | aucun signe de présence de la page depuis… (mesuré depuis le départ s'il n'y en a jamais eu) | > 60 s FREEZE ; > 120 s RAMP_DOWN | FREEZE → RAMP_DOWN | seulement RAMP_DOWN |
| `setpoint_unconfirmed` | LFRD relu ≠ LFRD écrit pendant 1 s | 1 s | RAMP_DOWN | oui |
| `session_standstill` | le runtime constate (`stopped_by`) que, dans cette séance et après que le bras a tourné, la consigne est revenue à 0, écriture acquittée par le variateur, sans que personne l'ait demandé : par un REDUCE (`hr_stale`, `hr_rate`, `hr_unresponsive`, `current_high` au niveau d'alerte) ou par la régulation cardiaque | aucun délai | RAMP_DOWN : la séance se termine à l'arrêt, le détail nomme la cause | oui ; se redéclenche tant que la séance arrêtée n'est pas finie (phase `DONE`) |

**`session_standstill` en détail.** La règle ne juge aucune mesure : elle lit
le constat que le runtime fait à un seul endroit, à la fin de l'étape de
commande du tic (`TrainingRuntime._note_standstill`), et le verdict tombe au
tic suivant. Le runtime ne fait pas ce constat : si la séance se terminait
déjà (STOP de l'opérateur, COOLDOWN ou RECOVERY du programme, verdict qui a
terminé la séance) ; en séance manuelle, si aucun avertissement ne tient (la
consigne ne suit alors que la cible de l'opérateur, qui a donc demandé ce 0) ;
si la consigne n'a jamais quitté 0 (BASELINE). Séance `bench` ou `occupied`,
c'est la même règle. Une cible 0 tapée pendant qu'un avertissement baisse la
vitesse est comptée comme l'arrêt de l'avertissement.

**`session_overrun` en détail.** La règle arrête une séance qui dure plus que
son programme : la machine tournerait alors sans plan. La durée qu'elle mesure
est le temps écoulé depuis le départ, et ce temps continue de courir après la
fin de la séance, jusqu'au départ suivant. La règle ne juge donc qu'une séance
**en cours**
([ANH-181](https://linear.app/anheart/issue/ANH-181/la-regle-session-overrun-se-verrouille-apres-la-fin-dune-seance-et)).
Avant ce correctif elle se verrouillait sur une machine au repos : 30 s après
la fin d'un programme allé à son terme, 1830,2 s après le départ du profil
standard même arrêté tôt, 3630,2 s après le départ d'une séance manuelle ;
l'acquittement ne tenait pas, tout départ était refusé et il fallait
redémarrer la console.

* **« En cours »** veut dire : tant que le runtime n'a pas constaté la séance
  finie. Il la constate finie quand deux faits sont vrais ensemble : depuis le
  départ, la phase a atteint `DONE` au moins une fois
  (`TrainingRuntime._advance_phase`, constat gardé jusqu'au départ suivant), et
  la consigne en vigueur est 0. C'est le champ `session_over` de l'observation.
* **La phase seule ne suffit pas.** Un runtime devenu silencieux atteint
  `DONE` avec une consigne qu'il ne peut plus reprendre : la règle continue
  alors de juger. (Jusqu'à ANH-185, un verdict arrivé au repos après un
  programme allé à son terme rouvrait une fin de séance, et la phase repassait
  par `RECOVERY` sur une séance finie depuis longtemps : c'était le second cas
  que la phase ne distinguait pas. Il n'existe plus, voir plus bas.)
* **Rien ne change pendant une séance** : même seuil, même action, même
  verrou, au même instant. Un FREEZE verrouillé ne tient plus un bras en
  vitesse au-delà de la fin du programme : la consigne suit la descente du
  programme sous FREEZE, la séance se termine à la durée prévue et la règle
  n'a pas à intervenir
  ([section 7](#7-ce-qui-se-passe-physiquement-à-larrêt)). Elle reste la
  seconde barrière derrière cette garde : un test retire la garde et vérifie
  qu'elle ramène encore à 0, dès l'échéance, un bras tenu en vitesse
  (RAMP_DOWN l'emporte sur FREEZE).
* **Une fin de séance déjà ouverte est jugée sur sa propre échéance**
  ([ANH-185](https://linear.app/anheart/issue/ANH-185/fausses-alertes-autour-de-la-fin-de-seance-fin-ouverte-tard-limite)).
  Toute fin de séance ouverte tard dans un programme (STOP, E-STOP ou verdict
  d'arrêt) rouvre une `RECOVERY` complète, qui finit après l'échéance du
  programme (avec le profil standard, pour une fin ouverte après 1530 s) ; et
  une séance manuelle qui atteint ses 3600 s à grande vitesse met plus de
  30 s à descendre (environ 104 s depuis 1344 tr/min moteur). La règle se
  verrouillait alors sur une fin qui se déroulait comme prévu. Le runtime dit
  maintenant la fin qu'il a ouverte (champ `ending` de l'observation,
  `TrainingRuntime._ending_in_progress`) : l'instant d'ouverture compté depuis
  le départ, la descente attendue et la durée de la récupération. La règle
  retient la plus tardive de l'échéance du programme et de celle de la fin :
  ouverture + descente tant que la consigne n'est pas à 0, ouverture +
  descente + récupération ensuite, plus les 30 s dans les deux cas
  (`SafetySupervisor._overrun_due`).
* **La descente attendue** est celle que la fin a réellement à faire, depuis
  la consigne en vigueur à son ouverture (enregistrée avec la fin,
  `Ending.setpoint`), et non la plus longue que la séance pourrait demander
  (`TrainingRuntime._expected_descent`, calculée une fois, au premier tic qui
  suit l'ouverture). En séance manuelle : la marche aux limites de mouvement
  depuis cette consigne (`ramp_duration`). Dans un programme : la plus longue
  de cette marche, avec l'attente avant son dernier pas (`min_run / slew`),
  et de la rampe de la loi de commande, bornée par `2 × consigne / slew`
  parce qu'elle avance par tr/min entiers et ne perd jamais plus de la moitié
  de son `slew`. Dans les deux cas plus les 4 s de la rampe du variateur.
  Avec les réglages livrés : 4 s pour une fin ouverte bras à l'arrêt, 29,7 s
  depuis 193 tr/min moteur et 40,8 s depuis le plafond du profil (276), où
  les descentes mesurées prennent au plus 17,6 s et 26,0 s ; en séance
  manuelle 24 s depuis 300 tr/min moteur, 107,8 s depuis 1344, 110,8 s depuis
  1380.
* **La règle reste armée.** L'échéance d'une fin ne peut que retarder la
  règle. Une descente qui ne finit pas n'a droit qu'à sa descente attendue :
  pour le profil standard, l'échéance reste 1830 s sauf pour une fin ouverte
  dans les 41 dernières secondes sur un bras encore en rotation, et vaut au
  plus 1900,8 s (fin ouverte à 1830 s au plafond du profil) ; à la limite
  d'une séance manuelle, 3600 s plus la descente depuis la vitesse en vigueur
  plus 30 s (3654 s depuis 300 tr/min moteur, 3738 s depuis 1344). Une fin
  ouverte après le dépassement (plus de 30 s après la durée prévue) ne
  reporte rien : c'est celle que le verdict de la règle ouvre lui-même. Une
  valeur qui n'est pas un nombre fini laisse l'échéance du programme.
* **Un verdict qui arrive sur une séance finie n'ouvre plus de fin de
  séance** (`TrainingRuntime._begin_ending`). Après un programme allé à son
  terme, un E-STOP ou un défaut variateur au repos mettaient le mode à `ARRET`
  et la phase à `RECOVERY` pour toute la récupération du profil, règles de FC
  de nouveau actives. Ils laissent maintenant le mode à `REPOS` et la phase à
  `DONE`, comme après une séance terminée par un STOP. Le verdict reste
  verrouillé, affiché et à acquitter, et refuse tout départ ; la consigne est
  remise à 0 par l'E-STOP comme avant. « Finie » exige une consigne en vigueur
  à 0 : avec une vitesse commandée, une vraie fin de séance s'ouvre.
* **Un verdict levé pendant la séance s'acquitte une fois la séance finie**,
  et l'acquittement tient : la condition n'est plus vraie. Avant la fin
  (mode `ARRET`), l'acquittement est accepté et le verdict revient au tic
  suivant, comme pour toute règle dont la cause est encore là.
* Avec un variateur en défaut, la fin de séance attend le réarmement par
  l'opérateur (le variateur refuse tout autre mot, et ne produit pas de couple)
  et le mode reste `ARRET` : la séance y est pourtant finie pour cette règle
  dès la phase `DONE`, consigne à 0.

Mesures avant et après, et ce que les correctifs ne couvrent pas :
[securite.md, section 8](securite.md#8-une-séance-finie-nest-plus-jugée-sur-sa-durée-anh-181)
et [8.6](securite.md#86-une-alerte-de-fin-de-séance-dit-quelque-chose-de-vrai-anh-185).

**`hr_hard_max` et `hr_critical` en détail : la dernière fréquence utilisable.**
Une lecture fraîche sans FC (fenêtre bruitée, dérivation plate, électrode
décollée, fenêtre que le traitement ne peut pas noter, FC retenue par la
confirmation indépendante) dit qu'on n'a rien pu mesurer. Elle ne dit pas que le cœur est redescendu. Les deux règles de niveau
jugent donc
([ANH-213](https://linear.app/anheart/issue/ANH-213)) :

* la FC de la dernière lecture quand elle en porte une utilisable (qualité
  `good`), sans limite d'âge, comme avant : une lecture réémise par le
  traitement reste jugée tant qu'elle est la dernière ;
* sinon la **dernière FC utilisable**, tant qu'elle a 10 s au plus. C'est
  `hr_stale_freeze_after`, l'âge à partir duquel `hr_stale` déclare la FC
  périmée : une seule définition de « périmé ». Au-delà, la FC est abandonnée
  (une FC d'il y a une minute, ou du passager précédent, n'est celle de
  personne) et la perte du signal revient à `hr_stale`. Cette règle compte
  depuis la dernière FC utilisable que le superviseur a vue à un tic, jamais
  plus récente que celle jugée ici : elle a donc déjà commencé. Un seul cas y
  échappe : un superviseur qui n'a jamais vu de FC utilisable à un tic compte
  depuis le départ de la séance, et une FC gardée d'avant ce départ est alors
  abandonnée avant que `hr_stale` ne commence. Pendant ces secondes aucune
  des deux règles ne parle, exactement comme quand aucune FC n'était gardée.

Conséquences :

* **le délai de 5 s du palier dur court à travers les lectures sans FC.** Seule
  une FC utilisable sous le niveau de relâche (palier dur − 5 bpm) le relance.
  Sur un signal qui ne donne une FC qu'une lecture sur cinq, ou quand
  l'électrode se décolle pendant le délai, le verdict tombe 5 s après la
  première lecture au-dessus du palier ;
* **un dépassement mesuré n'est pas effacé par une lecture sans FC.**
  `hr_critical` se déclenche au premier tic sur une lecture critique même si
  une lecture sans FC l'a suivie avant ce tic, et se redéclenche après un
  acquittement tant que cette FC a 10 s au plus ;
* **l'ordre des verdicts quand le signal se perd au-dessus du palier dur** :
  RAMP_DOWN `hr_hard_max` à l'échéance des 5 s, verrouillé ; `hr_stale` suit sa
  propre horloge, FREEZE 10 s, REDUCE 30 s et RAMP_DOWN 60 s après la dernière
  FC utilisable, sans être remise à zéro ni par les lectures sans FC ni par la
  règle de niveau. Le verdict en vigueur reste le premier des deux à avoir
  terminé la séance ;
* **la phrase du verdict le dit** : `heart rate 152 bpm (the last usable
  reading, taken 2.0 s ago; no reading since has carried a rate) has been
  above the hard maximum…`. Sur une FC portée par la dernière lecture, la
  phrase est celle d'avant ;
* **voulu : une seule lecture au-dessus du palier dur, suivie de 5 s sans
  aucune FC, termine la séance.** « Inconnu après un dépassement » est traité
  comme le dépassement. La même lecture suivie d'une FC utilisable sous le
  niveau de relâche ne déclenche rien, comme avant ;
* **voulu : une séance démarrée moins de 10 s après une dernière FC utilisable
  au-dessus d'un palier est jugée sur cette FC**, si rien n'a été mesuré
  depuis. La borne de 10 s ne regarde pas dans quelle séance la FC a été lue.
  Une FC critique vieille de 3 s arrête la nouvelle séance à son premier tic.
  Une FC au-dessus du palier dur vieille de 2 s la termine 5 s après ce
  premier tic : le délai est compté dans la séance, pas depuis la lecture.
  Vieille de 6 s, elle est périmée avant l'échéance et ne déclenche rien ;
  `hr_stale` prend la suite.

Le runtime garde cette dernière lecture utilisable là où arrivent toutes les
lectures (`TrainingRuntime.observe_ecg`) et la donne au superviseur dans le
champ `last_usable_heart_rate` de l'observation. L'observation ne montre
qu'une lecture par tic alors que plusieurs peuvent arriver entre deux tics (le
pont ECG tourne dans sa propre tâche et vide un retard en une fois) : ainsi
toute lecture utilisable compte pour les règles de niveau, y compris celle
qu'une lecture sans FC a suivie avant le tic. Ce champ est une mesure, il ne
peut qu'ajouter un verdict, et seules les deux règles de niveau le lisent :
l'horloge de `hr_stale` et l'historique des règles de tendance restent
alimentés par la dernière lecture de chaque tic, comme avant.

`SafetyLimits` refuse un délai du palier dur qui ne serait pas plus court que
`hr_stale_freeze_after` : un tel délai ne pourrait pas s'écouler sur un signal
perdu. Ce que la règle ne couvre pas est dans
[securite.md](securite.md#5-le-résiduel-connu) : une FC qui franchit un palier
pendant que le signal est perdu, et la consigne qui peut encore monter quelques
secondes après la dernière FC utilisable.

**`hr_drop` en détail** (la règle vasovagale). Seules les lectures dont le
numéro de séquence a avancé et dont la qualité est `good` comptent.

* niveau = médiane des 5 dernières lectures ; pic = plus haute médiane glissante
  de 9 dans la fenêtre de 30 s (moins de 9 lectures : pas de jugement) ;
* φ = g commandé maintenant / g le plus haut des 120 dernières secondes, borné à
  [0, 1] (φ = 1 si inconnu) ;
* sans FC de repos connue (séance manuelle) : seuil = pic − 25 bpm ;
* avec FC de repos : seuil = repos + (pic − repos) × φ − (15 + 10 × φ) ;
* la règle se déclenche si niveau ≤ seuil **et** pic − niveau ≥ 15 bpm, et que
  cela tient sur 4 lectures fraîches consécutives.

Sans baisse de charge (HOLD, φ = 1) c'est la règle des 25 bpm en 30 s ; charge
retirée (RECOVERY, φ = 0) elle se déclenche 15 bpm sous la FC de repos.

### 5.3 Les 4 règles ajoutées par le runtime

| Id | Condition | Action | Verrou |
|---|---|---|---|
| `drive_precommanded` | variateur trouvé en `OPERATION_ENABLED` (au départ) ou en marche/rotation au repos, sans séance | QUICK_STOP (consigne 0, marche gardée) | oui |
| `enable_unconfirmed` | le mot qui active l'étage de sortie a pu partir sans réponse | QUICK_STOP | oui |
| `disable_refused` | le variateur refuse 5 fois de suite (`disable_attempts`) les mots d'arrêt à l'arrêt confirmé | RAMP_DOWN si `SHUTDOWN` direct a réussi ; GO_SILENT sinon | oui |
| `tick_exception` | une exception dans le tic | GO_SILENT | oui, définitif |

Les règles `presence_*` de la caméra sont décrites en [section 10](#10-la-caméra-présence).

### 5.4 Réarmement d'un défaut variateur

`/api/drive/fault-reset` → `TrainingRuntime.fault_reset`. Jamais automatique.
Refusé (message entre parenthèses, tel qu'affiché) :

* si un mouvement est encore commandé (« reset refuse : mouvement encore commande ») ;
* si un autre verdict que `drive_fault` attend un acquittement (« reset refuse : acquitter d'abord le verdict … ») ;
* si le variateur n'est pas en FAULT (« reset refuse : aucun defaut a acquitter ») ;
* si l'arbre tourne encore (« reset refuse : l'arbre tourne encore ») ;
* si le défaut n'est pas réarmable, ou si le code n'a pas été lu ou est inconnu
  (« reset refuse : defaut … non rearmable depuis la console : couper
  l'alimentation du variateur et inspecter »).

Sinon : `FAULT_RESET`, puis `SHUTDOWN` 0,2 s plus tard. Rien n'est mis sous
tension ; le départ suivant réarme tout depuis zéro. Le verdict `drive_fault`
s'acquitte **après** le réarmement.

---

## 6. Les défauts du variateur (table LFT)

`LFT_FAULT_CODES` est l'énumération Schneider complète du registre LFT
(paramètre 7121, fichier « ATV32_communication_parameters » ; l'ATV320 reprend
les défauts de l'ATV32) : **66 défauts plus `nOF`** (LFT = 0, « aucun défaut
mémorisé »). Un code absent de la table se lit `UNKNOWN`, avec son numéro, et
n'est jamais réarmable depuis la console.

**Tout défaut arrête la machine de la même façon** : la règle `drive_fault` se
verrouille sur l'état FAULT quel que soit le code. Le code ne décide que du
message affiché et de la possibilité d'un réarmement depuis la console.

Table **générée** par un script qui parcourt `LFT_FAULT_CODES` et
`DriveFault.spec` (26 défauts réarmables, 40 non réarmables, plus `nOF` et
`UNKNOWN`). La colonne « Signification » est le texte exact affiché à
l'opérateur, tel qu'il est écrit dans le code (sans accents).

| LFT | Mnemonique | Membre `DriveFault` | Categorie | Rearmable depuis la console | Signification (texte affiche) |
|---:|---|---|---|---|---|
| 0 | `nOF` | `NO_FAULT_STORED` | aucun | non | aucun defaut memorise (LFT = 0). Si le mot d'etat indique un defaut en meme temps, les deux sont en desaccord : le mot d'etat fait foi. |
| 1 | `InF` | `INTERNAL` | materiel variateur | non | erreur de calibration interne du variateur : couper l'alimentation ; si le defaut revient, le variateur est defaillant. |
| 2 | `EEF1` | `CONTROL_EEPROM` | materiel variateur | non | memoire EEPROM de controle defaillante : couper l'alimentation, verifier la configuration ; remplacer le variateur si le defaut persiste. |
| 3 | `CFF` | `INCORRECT_CONFIG` | configuration | non | configuration incorrecte (carte changee ou parametres incoherents) : ne pas relancer, verifier et recharger la configuration mise en service. |
| 4 | `CFI` | `INVALID_CONFIG` | configuration | non | configuration invalide transferee au variateur : recharger la configuration mise en service avant tout redemarrage. |
| 5 | `SLF1` | `MODBUS_COMM_LOSS` | communication | oui | perte de communication Modbus : le variateur n'entendait plus la console et a applique son arret ttO. Verifier le cable RJ45/RS485 et la liaison 19200 8E1. |
| 6 | `ILF` | `INTERNAL_COM_LINK` | materiel variateur | non | liaison interne du variateur (carte option) en defaut : couper l'alimentation et verifier la carte option. |
| 7 | `CnF` | `COM_NETWORK` | communication | oui | defaut du reseau de communication (carte de communication) : verifier le reseau, puis acquitter. |
| 8 | `EPF1` | `EXTERNAL_FAULT_INPUT` | externe | oui | defaut externe signale par une entree logique ou un bit : trouver et lever la cause externe avant d'acquitter. |
| 9 | `OCF` | `OVERCURRENT` | moteur | non | surintensite en sortie : declenchement instantane. Suspecter un blocage mecanique ou un bobinage en court-circuit ; ne pas rearmer sans inspection. |
| 10 | `CrF` | `PRECHARGE` | materiel variateur | non | defaut du circuit de precharge du bus continu : couper l'alimentation ; defaut materiel du variateur. |
| 11 | `SPF` | `SPEED_FEEDBACK_LOSS` | retour vitesse | non | perte du retour vitesse : la vitesse mesuree n'est plus fiable. Verifier le capteur et son cablage avant tout redemarrage. |
| 16 | `OHF` | `DRIVE_OVERHEAT` | thermique | oui | surchauffe du variateur : laisser refroidir, verifier la ventilation et la temperature ambiante avant d'acquitter. |
| 17 | `OLF` | `MOTOR_OVERLOAD` | thermique | oui | surcharge thermique du moteur : laisser refroidir. Des declenchements repetes signifient une charge trop forte, pas un seuil faux. |
| 18 | `ObF` | `DC_BUS_OVERVOLTAGE` | alimentation | oui | surtension du bus continu au freinage : le variateur est en roue libre, le bras ralentit sans controle. Allonger la rampe de deceleration, ne jamais la raccourcir. |
| 19 | `OSF` | `MAINS_OVERVOLTAGE` | alimentation | oui | surtension du reseau d'alimentation : verifier la tension secteur avant d'acquitter. |
| 20 | `OPF1` | `OUTPUT_PHASE_LOSS` | moteur | non | perte d'une phase moteur en sortie : verifier le cable moteur et le couplage avant tout redemarrage. |
| 21 | `PHF` | `INPUT_PHASE_LOSS` | alimentation | oui | perte d'une phase d'alimentation : verifier l'alimentation et les fusibles avant d'acquitter. |
| 22 | `USF` | `UNDERVOLTAGE` | alimentation | oui | sous-tension secteur : l'alimentation a chute ou a ete coupee. Le moteur ralentit seul ; verifier l'alimentation avant de redemarrer. |
| 23 | `SCF1` | `MOTOR_SHORT_CIRCUIT` | moteur | non | court-circuit moteur : ne pas rearmer. Trouver le court-circuit (cable, bornier, bobinage) d'abord. |
| 24 | `SOF` | `OVERSPEED` | retour vitesse | non | survitesse : le moteur a depasse sa vitesse maximale. Inspecter la mecanique et la configuration avant tout redemarrage. |
| 25 | `tnF` | `AUTO_TUNING` | configuration | non | echec de l'auto-reglage : refaire la mise en service moteur avant toute seance. |
| 26 | `InF1` | `RATING_ERROR` | materiel variateur | non | calibre du variateur incoherent : defaut interne, couper l'alimentation et contacter la maintenance. |
| 27 | `InF2` | `POWER_CALIBRATION` | materiel variateur | non | carte de puissance incompatible ou non calibree : defaut interne, couper l'alimentation. |
| 28 | `InF3` | `INTERNAL_SERIAL_LINK` | materiel variateur | non | liaison serie interne en defaut : defaut interne, couper l'alimentation. |
| 29 | `InF4` | `INTERNAL_MFG_AREA` | materiel variateur | non | zone de fabrication interne invalide : defaut interne, contacter la maintenance. |
| 30 | `EEF2` | `POWER_EEPROM` | materiel variateur | non | memoire EEPROM de puissance defaillante : couper l'alimentation ; remplacer le variateur si le defaut persiste. |
| 31 | `SCF2` | `IMPEDANT_SHORT_CIRCUIT` | moteur | non | court-circuit impedant en sortie : ne pas rearmer, inspecter le cable et le moteur. |
| 32 | `SCF3` | `GROUND_SHORT_CIRCUIT` | moteur | non | court-circuit a la terre : ne pas rearmer, danger electrique. Inspecter l'isolement du moteur et du cable. |
| 33 | `OPF2` | `THREE_PHASE_LOSS` | moteur | non | perte des trois phases moteur : moteur deconnecte ou contacteur ouvert. Verifier le cablage avant tout redemarrage. |
| 34 | `COF` | `CANOPEN_COMM_LOSS` | communication | oui | perte de communication CANopen : verifier le bus, puis acquitter. |
| 35 | `bLF` | `BRAKE_CONTROL` | frein | non | defaut de commande du frein : inspecter le frein et sa commande avant tout redemarrage. |
| 38 | `EPF2` | `EXTERNAL_FAULT_COM` | externe | oui | defaut externe signale par le reseau de communication : lever la cause avant d'acquitter. |
| 41 | `brF` | `BRAKE_FEEDBACK` | frein | non | retour du contact de frein incoherent : inspecter le frein avant tout redemarrage. |
| 42 | `SLF2` | `PC_COMM_LOSS` | communication | oui | perte de communication avec le logiciel PC : verifier la liaison, puis acquitter. |
| 43 | `ECF` | `ENCODER_COUPLING` | retour vitesse | non | defaut d'accouplement du codeur : inspecter la mecanique du codeur. |
| 44 | `SSF` | `TORQUE_CURRENT_LIMIT` | charge | oui | limitation de couple ou de courant prolongee : verifier que rien ne freine le bras avant d'acquitter. |
| 45 | `SLF3` | `HMI_COMM_LOSS` | communication | oui | perte de communication avec le terminal : verifier le terminal, puis acquitter. |
| 46 | `PrF` | `POWER_REMOVAL` | securite (STO) | non | defaut de la fonction de securite STO (suppression de puissance) : ne pas rearmer, faire controler la chaine de securite. |
| 49 | `PtFL` | `PTC_PROBE` | thermique | oui | defaut de la sonde PTC sur LI6 : verifier la sonde et son cablage. |
| 50 | `OtFL` | `PTC_OVERHEAT` | thermique | oui | surchauffe detectee par la sonde PTC du moteur : laisser refroidir avant d'acquitter. |
| 51 | `InF9` | `INTERNAL_CURRENT_MEASURE` | materiel variateur | non | mesure de courant interne en defaut : defaut interne, couper l'alimentation. |
| 52 | `InFA` | `INTERNAL_MAINS_CIRCUIT` | materiel variateur | non | circuit d'entree interne en defaut : defaut interne, couper l'alimentation. |
| 53 | `InFb` | `INTERNAL_THERMAL_SENSOR` | materiel variateur | non | capteur thermique interne en defaut : defaut interne, couper l'alimentation. |
| 54 | `tJF` | `IGBT_OVERHEAT` | thermique | oui | surchauffe des IGBT : laisser refroidir, reduire la charge ; des declenchements repetes signalent un probleme. |
| 55 | `SCF4` | `IGBT_SHORT_CIRCUIT` | materiel variateur | non | court-circuit IGBT : defaut materiel grave, ne pas rearmer. |
| 56 | `SCF5` | `MOTOR_SHORT_CIRCUIT_2` | moteur | non | court-circuit moteur (detection a la mise sous tension) : ne pas rearmer, inspecter. |
| 57 | `SrF` | `TORQUE_TIMEOUT` | charge | oui | delai de couple depasse : verifier la charge mecanique avant d'acquitter. |
| 58 | `FCF1` | `OUTPUT_CONTACTOR_STUCK` | materiel variateur | non | contacteur de sortie colle : ne pas rearmer, faire intervenir la maintenance. |
| 59 | `FCF2` | `OUTPUT_CONTACTOR_OPEN` | materiel variateur | non | contacteur de sortie reste ouvert : verifier le contacteur avant tout redemarrage. |
| 61 | `AI2F` | `AI2_INPUT` | entree analogique | oui | defaut de l'entree analogique AI2 : verifier le signal et son cablage. |
| 64 | `LCF` | `INPUT_CONTACTOR` | alimentation | non | defaut du contacteur de ligne : verifier le contacteur avant tout redemarrage. |
| 66 | `dCF` | `DIFFERENTIAL_CURRENT` | moteur | non | courant differentiel (fuite) detecte : danger electrique, ne pas rearmer, inspecter l'isolement. |
| 67 | `HdF` | `IGBT_DESATURATION` | materiel variateur | non | desaturation IGBT (court-circuit en sortie) : defaut materiel grave, ne pas rearmer. |
| 68 | `InF6` | `INTERNAL_OPTION` | materiel variateur | non | carte option interne en defaut : couper l'alimentation, verifier la carte. |
| 69 | `InFE` | `INTERNAL_CPU` | materiel variateur | non | defaut du processeur interne : couper l'alimentation ; si le defaut revient, le variateur est defaillant. |
| 71 | `LFF3` | `AI3_CURRENT_LOSS` | entree analogique | oui | perte du signal 4-20 mA sur AI3 : verifier le capteur et son cablage. |
| 73 | `HCF` | `CARDS_PAIRING` | configuration | non | appairage des cartes incorrect : verifier les cartes et la configuration. |
| 76 | `dLF` | `LOAD_FAULT` | charge | oui | defaut de charge dynamique : verifier la mecanique entrainee avant d'acquitter. |
| 77 | `CFI2` | `BAD_CONFIG_TRANSFER` | configuration | non | transfert de configuration invalide : recharger la configuration mise en service. |
| 99 | `CSF` | `CHANNEL_SWITCH` | configuration | oui | defaut de commutation de canal de commande : verifier la configuration des canaux. |
| 100 | `ULF` | `PROCESS_UNDERLOAD` | charge | oui | sous-charge du process : le moteur ne rencontre plus la charge attendue (accouplement ?). Inspecter avant d'acquitter. |
| 101 | `OLC` | `PROCESS_OVERLOAD` | charge | oui | surcharge du process : quelque chose freine le bras. Inspecter avant d'acquitter. |
| 105 | `ASF` | `ANGLE_ERROR` | retour vitesse | non | erreur d'angle (moteur synchrone) : refaire le reglage moteur. |
| 107 | `SAFF` | `SAFETY_FUNCTION` | securite (STO) | non | defaut d'une fonction de securite integree : ne pas rearmer, faire controler la chaine de securite. |
| 108 | `FbE` | `FIELDBUS` | communication | oui | defaut du module de bus de terrain : verifier le module, puis acquitter. |
| 109 | `FbES` | `FIELDBUS_STOP` | communication | oui | arret sur defaut du bus de terrain : verifier le reseau, puis acquitter. |
| autre | `?` | `UNKNOWN` | non identifie | non | code de defaut non reconnu : lire le code affiche sur le variateur et le rechercher dans son manuel. Ne pas rearmer depuis la console. |

---

## 7. Ce qui se passe physiquement à l'arrêt

Faits de mise en service repris dans le code (`motor/drive.py`,
`training/plan.py`) :

* L'entrée de sécurité **STO du variateur est pontée**. Il n'existe **aucun
  moyen indépendant de supprimer le couple** : l'arrêt commandé par logiciel est
  le seul arrêt. Aucun frein n'est commandé par le logiciel.
* Le bus continu absorbe environ **11 J** sur environ **420 J** stockés dans le
  rotor en rotation (≈ 2,5 %).
* Rampe de décélération mesurée : 3-4 s ; le code retient
  `COMMISSIONED_DECEL_S` = **4 s**, et refuse un profil dont le cooldown est plus
  court.

Conséquences :

| Situation | Ce qui se passe |
|---|---|
| Arrêt demandé plus vite que la rampe du variateur | défaut **ObF** (surtension du bus, LFT 18) : le variateur passe en **roue libre**, le bras ralentit sans contrôle, plus longtemps. Aucun « arrêt rapide » n'existe donc dans le code. |
| `SHUTDOWN` (6) envoyé sur un arbre qui tourne | transition CiA402 8 : l'étage de sortie tombe, **roue libre**. Mesuré sur le modèle du dépôt : arrêt à t = 144,6 s avec 6, contre t = 10,0 s en gardant la commande de marche. D'où l'ordre `SWITCH_ON` (7) puis `SHUTDOWN` (6), et seulement à l'arrêt confirmé par RFRD. |
| QUICK_STOP, E-STOP, arrêt de sortie du processus | LFRD = 0, commande de marche **gardée** : le variateur suit sa rampe dEC. |
| STOP opérateur, local ou demandé depuis le site | demande de fin enregistrée ; au tic suivant la consigne commence à descendre vers 0 aux limites de mouvement, sans verdict comme sous FREEZE (verrouillé ou non). Un verdict plus sévère décide à sa place : REDUCE et RAMP_DOWN descendent eux-mêmes, QUICK_STOP met la consigne à 0, GO_SILENT se tait. |
| Cible manuelle remise à 0 | même descente, sous FREEZE aussi ; la séance n'est pas en train de se terminer pendant la descente (mode « MANUEL »). À l'arrivée à 0 : la séance continue si aucun avertissement ne tient, elle se termine sur `session_standstill` si un avertissement tient. |
| Programme arrivé à son retour au calme (phase COOLDOWN) | dès le premier tic de la phase, la consigne descend vers 0 aux limites de mouvement, sans verdict comme sous FREEZE (verrouillé ou non) ; le mode reste « SEANCE ». À l'arrivée à 0, rien n'est verrouillé : la séance suit sa chronologie (RECOVERY, puis fin `programme_complete` à la durée prévue). |
| Verdict RAMP_DOWN | consigne descendue par le logiciel (programme 15 tr/min moteur/s ; manuel aux limites de mouvement). |
| Consigne revenue à 0 en cours de séance sans que personne l'ait demandé (`session_standstill`) | la consigne est déjà à 0 quand le verdict tombe, au tic suivant : rien de plus n'est écrit. La séance se termine comme sur tout RAMP_DOWN : marche gardée tant que l'arbre tourne, puis 7 et 6 à l'arrêt confirmé par RFRD. |
| GO_SILENT, boucle bloquée, processus tué | plus de trame : le **ttO** du variateur expire et le variateur applique sa réaction de perte de communication (SLF). Le simulateur suppose ttO = 3 s et une rampe d'arrêt. **Sur le vrai variateur, ttO et SLL ne sont pas relus** (adresses non vérifiées) : ils doivent être contrôlés au clavier avant chaque séance. Si SLL était réglé sur « roue libre » ou « ignorer », une liaison morte laisserait le moteur commandé ou en roue libre. |
| Sortie normale du processus (`shutdown`) | LFRD = 0 synchrone, puis `close()` qui attend l'arrêt mesuré avant de retirer la marche. Si l'arrêt n'est pas confirmé, la marche reste et le ttO finit l'arrêt. |
| Console sans séance, au repos confirmé ou liaison non acquise | libération du transport sans écriture ; l'absence de séance ne prouve pas l'arrêt physique. |
| Console sans séance, liaison acquise mais état non confirmé | `needs_stop_before_release` impose le passage par `shutdown()` ; un état inconnu n'est pas assimilé à un arbre arrêté. |

**Un arrêt demandé descend toujours, même sous FREEZE
([ANH-175](https://linear.app/anheart/issue/ANH-175/stop-operateur-sans-effet-tant-quun-verdict-freeze-est-en-cours-la)).**
Jusqu'au 7 octobre 2026, la branche FREEZE de `TrainingRuntime._command`
réappliquait la dernière consigne sans rien regarder d'autre : un STOP était
enregistré, l'écran affichait « ARRET », et le bras continuait à la même
vitesse tant que le FREEZE durait. Maintenant :

* **Ce qui compte comme un arrêt demandé** (`_stop_asked`) : une fin de séance
  en cours, quelle qu'en soit l'origine (STOP à la console, arrêt demandé depuis
  le site, limite d'une heure de la séance manuelle, verdict de fin acquitté
  avant l'arrêt du bras), ou une cible manuelle à 0. Dans les deux cas, bras
  encore en mouvement. Depuis
  [ANH-189](https://linear.app/anheart/issue/ANH-189/la-descente-prevue-dun-programme-reste-figee-sous-un-verdict-freeze),
  le programme le demande aussi lui-même : toute phase après laquelle il ne
  demande plus de vitesse (`motion_is_over` : COOLDOWN, RECOVERY, DONE).
* **Ce que fait alors le tic sous FREEZE** (`_stop_under_freeze`) : un pas du
  profileur de mouvement vers 0, comme le fait un arrêt sans verdict. Rien ne
  peut y monter, la loi de commande et la FC ne sont pas consultées. Le dernier
  pas d'un programme (de la vitesse minimale à 0) attend comme d'habitude
  (`_passage_too_soon`).
* **Dès le tic suivant.** Le maintien d'un FREEZE ne laisse pas de base de temps
  au profileur, et un REDUCE de programme en laisse une périmée. La descente
  part donc du tic précédent : un tic de mouvement, jamais la durée du maintien.
* **Un FREEZE qui apparaît pendant une descente déjà commencée ne la fige pas** :
  elle continue aux limites de mouvement, jamais au-dessus des consignes d'une
  descente sans FREEZE au même tic (les mêmes aux instants vérifiés).
* **Sans arrêt demandé et avant COOLDOWN, rien ne change** : le FREEZE tient
  la consigne, une cible manuelle plus basse mais non nulle comprise, et la
  reprise se fait comme avant. REDUCE, RAMP_DOWN, QUICK_STOP et GO_SILENT
  décident toujours en premier.

Le mode « ARRET », la phase COOLDOWN ou l'acceptation d'une demande ne prouvent
toujours pas l'arrêt mesuré de l'arbre. Ils vont maintenant de pair avec une
consigne qui descend ou qui vaut 0, sauf sous GO_SILENT, où plus rien n'est
écrit. L'arrêt se lit sur la vitesse mesurée (RFRD).

**La descente prévue d'un programme est suivie sous FREEZE (ANH-189).** Le
retour au calme d'un programme sur sa propre chronologie (phase COOLDOWN sans
qu'aucune fin n'ait été demandée) n'est plus tenu par un FREEZE. Dès le
premier tic de COOLDOWN, la consigne prend la descente de
`_stop_under_freeze` : les limites de mouvement, rien qui puisse monter, le
dernier pas qui attend comme d'habitude. Un FREEZE pris pendant la montée
descend depuis la vitesse qu'il tenait, sans jamais rejoindre le palier. La
descente continue en RECOVERY si elle dure plus que COOLDOWN.

Cette descente n'est jamais plus rapide que les limites de mouvement, et
jamais en retard sur un retour au calme sans verdict : au même tic, sa
consigne est au plus la sienne. Elle peut être **en avance** sur lui, avec les
limites livrées aussi. Un retour au calme sans verdict (comme un STOP sans
verdict sur un programme) a une borne de plus : il marche vers la demande de la
loi de commande, qui descend sur sa propre rampe, `floor(slew × dt)` tr/min
entiers par tic (2 ou 3 à 5 Hz avec les 15 tr/min/s livrés, contre 2,48 pour
les limites de mouvement), après une avance de `slew` fois l'âge de sa
dernière décision (2 à 77 tr/min relevés sur le banc d'essai logiciel). Tant
que cette avance dure, les deux descentes ont les mêmes consignes, tic pour
tic ; quand elle est épuisée, le retour au calme sans verdict va au rythme de
la loi et arrive après. L'écart dépend de la vitesse de départ. Mesuré sur le
banc d'essai logiciel avec le profil livré, sur 27 positions de l'entrée en
COOLDOWN dans la période de régulation : depuis 193 tr/min moteur, jusqu'à
2,6 s plus tard, à 9 positions ; depuis le plafond du profil (276 tr/min
moteur), jusqu'à 4,4 s plus tard, à 16 positions.

La séance se termine alors comme un programme mené à son terme : aucun
`session_standstill`, aucun `session_overrun`, fin `programme_complete` à la
durée prévue ; un FREEZE verrouillé reste à acquitter avant le départ suivant.
Cela vaut pour un FREEZE verrouillé, et pour un FREEZE non verrouillé dont la
cause cesse avant le niveau suivant de sa règle. Si elle dure, `hr_stale`
(RAMP_DOWN à 60 s) ou `attendant_absent` (RAMP_DOWN à 120 s) termine la séance
sur son verrou, comme avant. Une séance manuelle n'a pas de phase de ce
genre : un FREEZE y tient la consigne jusqu'à un arrêt demandé. `session_overrun`
reste la seconde barrière derrière cette garde (vérifié en retirant la garde
dans un test). Détails et vérifications dans
[securite.md](securite.md#78-la-descente-prévue-dun-programme-est-suivie-sous-freeze-anh-189).

Les distinctions de libération du transport sont exercées par `tests/test_initial_inspection_cancellation.py`
et `tests/test_acquisition_evidence.py`, notamment le cas acquis mais illisible
`test_cancelled_unreadable_acquired_drive_is_stopped_without_resumption`.

HSP (vitesse haute du variateur) est la limite qui tient quand le logiciel se
trompe. Le code accepte HSP ≤ 50,0 Hz parce que le moteur est **désaccouplé** au
banc ; le code dit explicitement que cette limite **doit être abaissée avant
d'accoupler le bras** (clavier d'abord, puis le code).

---

## 8. La synchronisation avec le tableau de bord

`src/cloud_sync.py` et `src/record_uplink.py`. Actif seulement si
`MACHINE_API_KEY` est renseignée (et `CONVEX_URL` en `https://…convex.site`,
ou `http://localhost` / `http://127.0.0.1`). Sans clé, la console fonctionne
exactement pareil, sans tableau de bord.

| Sens | Quoi | Route Convex | Cadence |
|---|---|---|---|
| Pi → Convex | battement de cœur avec l'état (mode, phase, FC, vitesses, g, action de sécurité), la version logicielle, la version du contrat et, si la console enregistre, `recordDegraded` : l'enregistrement de séance est incomplet ou ne peut pas être écrit ([15.3](#153-quand-le-disque-refuse-se-remplit-ou-se-tait)) | `POST /api/machine/heartbeat` | toutes les 10 s (seuil hors ligne du tableau de bord : 90 s) |
| Pi → Convex | profils dont les paliers cardiaques égalent ceux du superviseur | `POST /api/machine/profiles` | quand la révision du magasin change |
| Pi → Convex | toute séance lancée ici (MANUEL ou AUTO), sous la référence que porte son enregistrement (`local_ref` du manifeste), avec le début tel que la console l'a daté et l'âge de la séance | `POST /api/machine/training/local` | dès que le dossier de son enregistrement existe, par la tâche de veille (8.6), avant tout autre envoi de la séance ; refaite tant qu'elle n'est pas acquittée, après un redémarrage aussi |
| Pi → Convex | confirmation du départ d'une séance lancée à distance, avec les deux mêmes dates | `POST /api/machine/training/start` | dès que la séance est armée, par la tâche de veille (8.6) ; refaite tant qu'elle est retenue |
| Pi → Convex | télémétrie à 1 Hz **relue dans `ticks.csv`** : le premier tic de chaque seconde entière de la séance | `POST /api/machine/training/telemetry` | toutes les 5 s quand la séance est à jour ; un lot de 300 points au plus toutes les 2 s quand elle a du retard |
| Pi → Convex | événements de la séance **relus dans `events.jsonl`**, chacun avec son rang dans le fichier | `POST /api/machine/training/events` | avec la télémétrie, 200 au plus par envoi |
| Pi → Convex | fin de séance, avec la raison que porte l'enregistrement | `POST /api/machine/training/end` | quand l'enregistrement est fermé et sa télémétrie acquittée |
| Convex → Pi | un lancement AUTO : profil, passager, FC max, âge (déduit de l'année de naissance), avec la version du contrat du serveur | `GET /api/machine/training/poll` | toutes les 3 s au repos |
| Convex → Pi | une demande d'arrêt transmise au chemin STOP ordinaire : la consigne descend aux limites de mouvement, même sous FREEZE (section 7) | `GET /api/machine/training/status` | dès que le tableau de bord tient la séance pour démarrée, puis toutes les 3 s tant qu'elle n'est pas finie, par une **tâche à part** (`cloud_stop_step`) : la question ne passe derrière aucun envoi ([8.6](#86-ce-que-la-synchronisation-ne-peut-pas-coûter)) |

Règles :

* **Rien d'autre ne descend.** Une séance **manuelle ne peut jamais être lancée
  à distance** : le lien ne sait pas en construire une.
* Un lancement distant passe par la même boîte aux lettres qu'un départ tapé à
  la console, donc par les mêmes portes (attestation, pas de verdict, console
  au repos), puis par celles de la séance programmée (4.4). Tout refus revient
  comme séance échouée avec la raison, sans date : c'est le serveur qui la
  date. Un lancement ni démarré ni refusé après 60 s est déclaré échoué. Un
  seul refus attend à la fois, et aucun lancement n'est demandé tant qu'il
  n'est pas parti : le serveur rendrait le même.
* Chaque appel renvoie un `Result` ; un réseau mort ne fait que rendre l'image
  du tableau de bord périmée. **Rien de ce qui est dû au tableau de bord
  n'attend en mémoire** : c'est sur le disque, dans les enregistrements de
  séance (8.1).
* Un arrêt venu du tableau de bord est attribué à « tableau de bord ». Il est
  honoré **quelle que soit la version de contrat** du serveur (section 14).
  Il appartient à sa séance : transmis une fois, il ne masque pas l'arrêt
  demandé pour la suivante, et une réponse arrivée après la fin de la séance
  qu'elle concerne n'arrête pas celle qui a commencé entre-temps.
* **Une séance lancée du tableau de bord reste arrêtable depuis le tableau de
  bord, ou ne tourne pas.** Si le tableau de bord répond à la confirmation de
  son départ par autre chose qu'un oui (séance annulée entre-temps, mais aussi
  426, 401, 403, 5xx, route inconnue), la console arrête la séance tout de
  suite, une fois : personne ne pourrait l'arrêter depuis le site. Sans
  réponse exploitable (réseau coupé, délai de 3 s dépassé, ou réponse 2xx qui
  n'est pas un objet JSON), la séance continue sous le seul superviseur local,
  comme toute séance sans tableau de bord, et la confirmation est refaite.
* Un lancement venu d'un serveur d'une autre majeure de contrat, ou qui
  n'annonce pas sa version, n'est **jamais armé** : voir
  [Versions et compatibilité](#14-versions-et-compatibilité).

### 8.1 Ce qui est envoyé vient du disque

Ce que le tableau de bord reçoit d'une séance n'est plus ce que la console
gardait en mémoire pendant qu'elle tournait : c'est ce que dit
l'enregistrement sur le disque ([section 15](#15-lenregistrement-de-séance-boîte-noire-locale)),
relu depuis l'endroit que le tableau de bord a acquitté en dernier. Une
console tuée au milieu d'une séance doit donc au tableau de bord exactement ce
que son disque contient, et le lui envoie après son redémarrage.

**Le curseur.** Un petit fichier par enregistrement,
`<racine>/<nom du dossier>.sync.json`, **à côté** du dossier et non dedans :
le dossier d'un enregistrement ne contient que les fichiers du format
([enregistrement.md](enregistrement.md#arborescence)), et sa relecture
signalerait tout autre fichier. Il contient l'identifiant de la séance côté
tableau de bord (et si son départ lui a été confirmé), l'identifiant du
démarrage du système pendant lequel l'enregistrement a été fait
([8.5](#85-les-dates)), l'endroit acquitté de `ticks.csv` et le `t` du dernier point
acquitté, l'endroit acquitté de `events.jsonl` et le rang du dernier
événement, l'état de la fin (`pending` ou `sent`), l'état de l'ensemble
(`pending` ou `complete`), et trois compteurs (points et événements acquittés
sans être stockés, requêtes refusées pour de bon). Il ne contient aucune
mesure.

| Propriété | Comment |
|---|---|
| Créé dès que la console relie la séance à son enregistrement, dans la seconde qui suit le départ, avant toute réponse du tableau de bord et même quand le lien est retenu | avec son curseur, un enregistrement est repris où le tableau de bord s'était arrêté, et la console peut encore dire l'âge de la séance |
| Lié à son enregistrement | il porte la référence du manifeste (`local_ref`) : un curseur qui en nomme un autre (un fichier copié ou renommé à la main) n'est pas utilisé, même s'il dit `complete`, et l'enregistrement est renvoyé depuis son début |
| Écrit après chaque acquittement | fichier temporaire privé dans le même dossier, `fsync`, puis renommage : un lecteur voit l'ancien curseur ou le nouveau, jamais la moitié d'un |
| Privé | mode 600, donné à l'appel qui crée le fichier : le curseur et la liste `.sync-baseline.json` sont créés par `create_private` (`src/record/writer.py`), comme tout fichier d'un enregistrement ou posé à côté ([15.7](#157-ce-que-lenregistrement-dit-des-personnes-et-sa-protection)). Le curseur est remplacé à chaque acquittement, ce qu'un fichier écrit une fois ne demande pas : il est donc créé sous un nom à lui, `fsync`, puis renommé sur l'ancien |
| Ce n'est pas la vérité, seulement une économie | un curseur **tronqué, illisible, d'une autre version ou qui ne correspond pas à son fichier** fait renvoyer l'enregistrement **depuis son début** ; le tableau de bord ne stocke qu'une fois ce qu'il a déjà (un point est connu par `(séance, t)`, un événement par `(séance, rang)`) |
| Un curseur dont le dossier n'existe plus | retiré au démarrage suivant de la console, avec tout fichier temporaire laissé par une console tuée pendant une écriture. Rien d'autre n'est retiré à cet endroit : ni le journal hors séance (`logbook/`), ni un marqueur de dépôt, même resté seul (la purge le retire elle-même, [15.5](#155-rétention--rien-nest-purgé-sans-dépôt-confirmé)) |
| Un enregistrement **sans aucun curseur**, déjà là quand la synchronisation a listé le dossier pour la première fois | n'est pas envoyé : il a été fait avant que cette console tourne pour la première fois avec un tableau de bord configuré (version antérieure de ce logiciel, ou pas de clé). Leur liste est écrite une fois à côté des enregistrements (`.sync-baseline.json`, privé, écrit comme un curseur), et la console dit alors combien elle en met de côté (plus bas) |
| Un enregistrement **sans aucun curseur**, apparu depuis | est envoyé, depuis son début, au démarrage suivant de la console : il n'a jamais été lié à son curseur (console tuée dans la première seconde de la séance, ou disque qui a refusé le curseur). Le journal le dit (`was never tied to a cursor`). Supprimer un curseur à la main fait donc **renvoyer** l'enregistrement |

**La télémétrie à 1 Hz.** `ticks.csv` est écrit à 5 Hz. Le point d'une seconde
de la séance est son **premier tic**. La règle ne dépend que du fichier :
relire depuis le curseur, ou depuis le début, donne les mêmes points aux mêmes
dates, ce qui permet au tableau de bord de reconnaître un point qu'il a déjà.
Un trou dans les tics reste un trou dans les points : rien n'est inventé.

**Seules les lignes complètes sont lues.** Une dernière ligne en cours
d'écriture est laissée à la lecture suivante. Une ligne complète
incompréhensible est passée et comptée dans le journal de la console : elle ne
retient pas ce qui la suit. Un événement illisible garde son rang. Une suite
d'octets sans fin de ligne plus longue qu'une lecture (512 kio : des zéros
laissés par une coupure de courant, par exemple) n'est pas une ligne : elle
est passée elle aussi, sur autant de lectures qu'il faut, comptée une fois, et
ce qui la suit est lu. Le curseur retient qu'il est au milieu d'elle
(`ticks_mid_line`, `events_mid_line`).

**Ce qui n'est pas envoyé.** Les trames du variateur (`drive_frames.jsonl`),
les blocs ECG bruts (`ecg_raw/`) et `sensors.csv` ne sont ni lus ni envoyés :
ils voyageront avec le dépôt des enregistrements complets (ANH-130). Seuls les
événements d'une séance, ceux de son `events.jsonl`, sont envoyés. Le journal
hors séance (`logbook/`, à côté des enregistrements,
[15.9](#159-le-journal-hors-séance)) n'est pas un enregistrement : il n'est ni
listé, ni lu, ni envoyé, et n'a pas de curseur.

**Ce que la console dit quand une séance n'arrivera pas comme prévu.** Dans la
liste d'événements de la page, sous la même forme que
`serveur incompatible (...)`, et dans le journal :

| Phrase | Quand |
|---|---|
| `seance declaree sans son enregistrement : le tableau de bord n'en recevra les mesures que si l'enregistrement apparait` | le dossier de l'enregistrement d'une séance démarrée à la machine n'est pas apparu dans les 10 s qui suivent le départ : elle est déclarée sans lui. S'il apparaît plus tard, il est lié à la séance à ce moment-là et ses mesures partent |
| `enregistrement de la seance en cours introuvable ou illisible : le tableau de bord n'en recoit pas les mesures, et n'en apprendra pas la fin si la console s'arrete avant elle` | 10 s après le départ, l'enregistrement de la séance en cours n'a toujours pas pu être lié à elle : son manifeste ne se lit pas, les lectures du disque ne reviennent pas, ou, pour un lancement du tableau de bord, aucun dossier n'a été ouvert. La séance a été dite au tableau de bord et son arrêt y est demandé comme pour une autre ; rien d'elle n'est envoyé et elle n'a pas de curseur. Dit une fois par séance, jamais avec la phrase précédente. Si le manifeste finit par se lire, l'enregistrement est lié à ce moment-là et ses mesures partent. Ce qui clôt une telle séance côté tableau de bord : [8.7](#87-limites) |
| `curseur de synchronisation non ecrit : si la console redemarre, la suite de cette seance pourrait ne pas arriver au tableau de bord` | le disque refuse le curseur. L'envoi continue ; dit une fois par panne. Après un redémarrage, l'enregistrement est renvoyé depuis son début s'il n'a aucun curseur et que la liste des enregistrements antérieurs existe ; si le disque a refusé cette liste aussi, il ne l'est pas |
| `liste des enregistrements anterieurs a la synchronisation etablie : N enregistrement(s) sans curseur mis de cote, non envoyes au tableau de bord` | la console vient de faire la liste des enregistrements antérieurs (`.sync-baseline.json`), faute d'en trouver une lisible : à son premier démarrage avec un tableau de bord configuré sur ce dossier, ou de nouveau si le fichier a été supprimé ou ne se lit plus. Tout ce qui n'a pas de curseur à ce moment-là y entre et n'est pas envoyé. Quand la liste est refaite, un enregistrement fait depuis et qui attendait d'être envoyé en fait partie et ne le sera pas. Dit au démarrage avec le nombre, **chaque fois** que la liste est faite et qu'elle met au moins un enregistrement de côté, qu'un autre enregistrement porte un curseur ou non |

### 8.2 Dans quel ordre

1. **La séance en cours.** Tant qu'elle a quelque chose de prêt à envoyer,
   rien d'autre n'est envoyé.
2. **La séance qui vient de finir** : le tableau de bord la montre encore en
   cours.
3. **Toutes les autres, de la plus ancienne à la plus récente** (le nom d'un
   dossier d'enregistrement commence par la date de son début) : celles qu'un
   lancement précédent de la console a laissées inachevées, et celles qui ont
   fini dans ce lancement pendant que le lien ne répondait pas.

Pour une séance : la **déclaration** (ou la confirmation du départ) d'abord,
puis la télémétrie, les événements, et la **fin** quand l'enregistrement est
fermé et sa télémétrie acquittée.

Débit : **jamais deux lots de télémétrie à moins de 2 s l'un de l'autre,
quelles que soient les séances, chacun de 300 points au plus.** Une séance en
cours qui est à jour envoie toutes les 5 s ce qu'elle a mesuré depuis ; si un
lot de rattrapage vient de partir, elle attend son tour, 2 s au plus. Une
séance en retard n'est pas même relue avant son tour. Ne portent pas de
télémétrie et n'attendent donc pas ce rythme : une déclaration, une fin sans
enregistrement (un lancement refusé), et la fin d'une séance dont tous les
points sont déjà partis.

### 8.3 Ce que chaque réponse fait au curseur

| Réponse | Cas | Effet |
|---|---|---|
| **Acquittée** | 200 | Le curseur avance après tout ce que la requête portait, stocké ou non. Le serveur compte ce qu'il n'a pas stocké parce que daté hors de la séance (`rejected`) : ce nombre est écrit dans le journal de la console (`N points of session ... acknowledged but not stored`) et cumulé dans le curseur (`rejected_points`, `rejected_events`). Ces points sont derrière le curseur : les renvoyer donnerait la même réponse. |
| **Retenue** | pas de réponse (réseau, délai de 3 s), 401, 403, 408, 425, **426**, 429, 5xx | Rien n'a été reçu. **Le curseur ne bouge pas et rien n'est abandonné.** L'étape d'envoi n'envoie plus rien pendant 15 s, puis la même requête est refaite. Le premier contact de la séance en cours (déclaration ou confirmation du départ) et la question de l'arrêt ne sont pas de cette étape et n'attendent pas cette retenue : ils n'attendent que leurs propres échecs ([8.6](#86-ce-que-la-synchronisation-ne-peut-pas-coûter)). |
| **Route inconnue** | 404 sans code stable | Pour les événements (un Convex antérieur au contrat 1.1) : ils restent dus, et la télémétrie et la fin continuent sans eux. La route est redemandée une fois par minute tant que la séance s'envoie ; une fois sa fin partie, l'enregistrement est mis de côté jusqu'au démarrage suivant de la console, qui redemande. Pour toute autre route : comme « retenue ». |
| **Refusée** | tout autre refus | Avec un code qui dit que la même requête sera toujours refusée (`invalid_request`, `session_not_found`, `session_not_pending`, `machine_not_found`) : passée tout de suite. Sinon (`request_failed`, ou pas de code) : essayée **trois fois en tout**, à 15 s d'intervalle, puis passée. Une requête passée est comptée dans le curseur (`refused`) et écrite dans le journal ; ce qui la suit n'est pas retenu. |

Une déclaration acquittée sans identifiant de séance ne sert à rien : elle est
refaite toutes les 15 s. Ni la séance en cours, ni celle qui vient de finir,
ni le refus d'un lancement ne l'attendent ; les séances plus anciennes rangées
derrière elle, si.

Deux requêtes ont un effet propre. La **confirmation d'un départ** : toute
réponse autre qu'un oui fait arrêter la séance en cours, tout de suite et une
seule fois. Refusée (séance annulée sur le site entre le poll et l'armement),
elle est tenue pour réglée ; retenue par le serveur (426, 401, 403, 5xx), elle
reste due et est refaite toutes les 15 s, puis l'enregistrement est envoyé,
quand le serveur l'accepte. Seule l'absence de réponse exploitable (réseau
coupé, délai dépassé, réponse 2xx qui n'est pas un objet JSON) laisse tourner
la séance. Une réponse qui arrive après la fin de la séance qu'elle concerne
n'arrête pas celle qui a été armée entre-temps. Une **déclaration** refusée
pour de bon clôt l'enregistrement sans
rien en envoyer : sans séance côté tableau de bord, rien ne peut être reçu.

### 8.4 Après un redémarrage de la console

À sa première étape où la séance en cours ne lui prend pas la place, le lien
liste une fois, du plus ancien au plus récent, les enregistrements dont le
curseur ne dit pas `complete` et ceux, apparus depuis sa toute première
liste, qui n'ont pas de curseur ; il retire les curseurs orphelins. Chacun
est repris où le tableau de bord s'était arrêté :

- **déjà déclaré** : le curseur porte l'identifiant de la séance, rien n'est
  redéclaré ;
- **jamais déclaré** (le réseau manquait depuis le départ) : il est déclaré
  avec ce que dit son manifeste. L'enregistrement ne contient aucun nom : la
  séance apparaît avec l'alias de l'opérateur (`op-…`) et sans nom de
  programme ;
- **jamais lié à son curseur** (console tuée dans la première seconde, disque
  qui refusait le curseur) : il est envoyé depuis son début ; le tableau de
  bord ne stocke qu'une fois ce qu'il aurait déjà ;
- **jamais fermé** (la console a été tuée pendant la séance) : il se termine
  par `interrupted`, séance échouée, datée de son dernier tic. Si la fin du
  fichier ne contient aucun tic lisible (64 kio ou plus sans fin de ligne
  après la coupure), elle est datée du dernier point que l'enregistrement a
  donné, le premier tic de sa dernière seconde : moins d'une seconde plus tôt.
  La queue du fichier n'est lue qu'une fois, sur 64 kio : la console ne
  remonte pas plus loin chercher le tic exact ;
- **fermé** : il se termine par la raison de son manifeste
  (`operator_stop: <texte de l'arrêt>`, `shutdown`, …), comme si la console
  n'avait pas redémarré.

Une sortie ordinaire de la console (SIGTERM) en cours de séance ferme
l'enregistrement avec `shutdown`, après l'arrêt du variateur ; la fin part au
démarrage suivant.

**Ce qui est envoyé, et depuis quand.** La limite est le premier démarrage de
cette console avec un tableau de bord configuré (`MACHINE_API_KEY`) sur ce
dossier d'enregistrements : c'est à ce moment-là que la liste des
enregistrements antérieurs est écrite, et que la console dit combien elle en
met de côté ([8.1](#81-ce-qui-est-envoyé-vient-du-disque)).

| Enregistrement | Envoyé au tableau de bord |
|---|---|
| fait avant ce premier démarrage | **non**, jamais |
| fait depuis, tableau de bord configuré | oui, pendant la séance, et au démarrage suivant pour ce qui restait |
| fait depuis, **sans** tableau de bord configuré (clé retirée entre-temps) | **oui**, au premier démarrage où une clé est de nouveau configurée : il n'a pas de curseur et n'est pas sur la liste. La séance apparaît alors sur le tableau de bord de la machine que désigne cette clé, sous l'alias de l'opérateur |

Aucun réglage ne permet de garder locaux les enregistrements de la troisième
ligne. Le seul moyen, console arrêtée et avant de la relancer avec une clé,
est de supprimer `.sync-baseline.json` : la liste est refaite de tout ce qui
n'a pas de curseur, ces enregistrements y entrent, et la console dit combien
elle en a mis de côté, qu'un autre enregistrement porte un curseur ou non.

### 8.5 Les dates

La console n'a pas d'horloge sauvegardée : démarrée sans réseau, elle date ce
qu'elle écrit d'une heure fausse, corrigée d'un coup au retour du réseau.

- **Dans l'enregistrement**, un tic et un événement sont datés en secondes
  depuis le début de la séance, sur l'**horloge monotone**. Le manifeste porte
  l'heure murale du début (`clocks.utc_start`) et la lecture monotone du même
  instant.
- **Dans une requête**, le `t` d'un point ou d'un événement est ce début plus
  le temps écoulé : **une seule lecture de l'heure murale, au départ**, puis
  des durées. La fin est datée de même. Une correction de l'heure murale en
  cours de séance ne déplace donc aucun point, et aucun n'est refusé pour
  elle.
- **L'âge de la séance** (`sessionAgeMs`) accompagne la déclaration et la
  confirmation du départ : le temps écoulé depuis le début, sur l'horloge
  monotone. C'est lui qui fait dater la séance par le serveur, sur sa propre
  horloge ([convex.md, Deux horloges](convex.md#deux-horloges)) : une console
  datée de 1970 ne fait apparaître aucune séance en 1970, et ce qu'elle envoie
  en retard est lu comme mesuré quand il l'a été.

Après un redémarrage **de la console seule** (le système n'a pas redémarré),
l'horloge monotone est la même : l'âge reste exact. Le curseur retient pour
cela l'identifiant du démarrage du système (`boot_id`, lu dans
`/proc/sys/kernel/random/boot_id`).

Après un redémarrage **du système**, plus rien ne relie les deux horloges :

| L'enregistrement est daté | Ce que la console dit | Ce que le tableau de bord affiche |
|---|---|---|
| après le 1er janvier 2024 | aucun âge | la date que la console avait écrite, comme avant. Elle peut être fausse de la durée pendant laquelle le Pi était éteint ; rien ne permet de le savoir (une heure fiable est le sujet d'ANH-163) |
| avant (une horloge jamais réglée) | un âge **minimal** : le temps depuis lequel cette console tourne, plus la durée de l'enregistrement, plus une minute | la séance, datée au plus tard qu'elle puisse avoir commencé. Sa vraie date est perdue ; elle n'est ni en 1970, ni lue comme mesurée à l'instant |

### 8.6 Ce que la synchronisation ne peut pas coûter

- **Jamais dans le tic.** `RecordUplink.step` tourne dans la tâche du lien
  (`cloud_step`), qui n'appelle aucune fonction du tic et que le tic n'appelle
  pas.
- **Aucune lecture sur la boucle.** Chaque lecture d'enregistrement et chaque
  écriture de curseur se fait sur un fil `record-io` **réservé à la
  synchronisation**, distinct des fils de l'export et de ceux du traitement
  ECG : une à la fois, bornée en taille (512 kio de `ticks.csv`, 300 points,
  200 événements) et en durée (2 s, 10 s pour la liste du démarrage). La
  mémoire tenue est celle d'un lot.
- **Un disque qui ne répond pas** retient l'envoi et rien d'autre : l'étape
  suivante trouve le fil occupé et rend la main tout de suite, sans rien
  empiler. Une séance démarrée à la machine dont l'enregistrement n'apparaît
  pas en 10 s est déclarée quand même, sans enregistrement, et la console le
  dit (8.1) : le tableau de bord apprend son départ et sa fin, et ses mesures
  seulement si l'enregistrement finit par apparaître.
- **L'arrêt demandé du tableau de bord n'attend derrière aucune lecture ni
  aucun envoi de séance.** Une tâche à elle, `cloud_stop_step`, regarde toutes
  les 50 ms ce qu'elle a à faire, et ne lit rien sur le disque :
  1. **dire la séance au tableau de bord** : confirmer le départ d'un
     lancement dès que la séance est armée, ou déclarer une séance démarrée à
     la machine dès que le dossier de son enregistrement existe, sous la
     référence que porte le nom de ce dossier (`RecordUplink.greet`). C'est la
     première chose que le tableau de bord apprend d'une séance, et ce qui
     permet de lui demander si un arrêt est voulu ;
  2. **poser la question de l'arrêt** (`GET training/status`) : une première
     fois dès que le tableau de bord tient la séance pour démarrée, puis
     toutes les 3 s.

  L'étape d'envoi (`cloud_step`) peut attendre une lecture, une écriture de
  curseur, un envoi ou tout un rattrapage : rien de cela ne retarde ni l'un ni
  l'autre. Mesuré en temps simulé, séance armée au milieu d'un rattrapage,
  temps comptés depuis l'armement (confirmation ou déclaration envoyée /
  première question) : 0,05 / 0,10 s lien et disque sains ; 0,05 / 0,10 s
  avec un rattrapage et des lectures à 1,8 s ; 0,05 / 2,55 s si chaque requête
  prend en plus 2,5 s (la question attend la réponse à la confirmation, rien
  d'autre). Ensuite, intervalle entre deux questions (médiane / maximum) :
  3,00 / 3,05 s dans ces trois cas, pendant que l'étape d'envoi dure jusqu'à
  7,4 s dans le deuxième et 14,7 s dans le troisième.

  **Une retenue de l'étape d'envoi ne les retarde pas non plus.** Une requête
  de cette étape restée sans réponse exploitable (un lot de télémétrie, des
  événements, une fin, de la séance en cours ou d'une séance rattrapée) arrête
  l'étape d'envoi pendant 15 s
  ([8.3](#83-ce-que-chaque-réponse-fait-au-curseur)). Le premier contact et la
  question ne lisent pas cette retenue : ils n'attendent que **leurs propres
  échecs**. Mesuré de même, 100 minutes à rattraper, chaque lot de télémétrie
  recevant un 503 ou restant sans réponse (3 s) pendant que les autres routes
  répondent, séance armée à sept instants, de 0,3 à 14,3 s après le départ du
  premier lot : 0,05 / 0,10 s à chaque fois, pour un lancement comme pour une
  séance démarrée à la machine, puis une question toutes les 3,00 s.

  Aucune réponse faite à une autre requête n'est héritée, pas même celles qui
  parlent de tout le lien (401 ou 403 : clé refusée ; 426 : contrat refusé).
  Le premier contact coûte une requête, et sa réponse décide ce que celle
  d'un lot ne décide pas : une confirmation que le serveur ne prend pas fait
  arrêter la séance tout de suite
  ([8.3](#83-ce-que-chaque-réponse-fait-au-curseur)), alors qu'attendre la fin
  d'une retenue héritée laisserait tourner jusqu'à 15 s une séance que le
  tableau de bord ne tient pas pour démarrée et que personne ne peut y
  arrêter. Une déclaration reçoit, elle, la même réponse que le lot, puis
  attend pour son propre compte.

  Ce qui reste devant la première question, et rien d'autre :

  - le regard de la tâche : 50 ms au plus avant chaque chose ;
  - pour une séance démarrée à la machine, la création du dossier de son
    enregistrement par le fil du journal (10 s au plus : au-delà elle est
    déclarée sans) ;
  - la réponse du tableau de bord à la confirmation ou à la déclaration (une
    requête, 3 s au plus) ;
  - l'échec de ce premier contact lui-même. Resté sans réponse exploitable ou
    retenu par le serveur (réseau, délai, 401, 403, 408, 425, 426, 429, 5xx,
    route inconnue), il est refait 15 s plus tard, et pas avant ; de même une
    déclaration acquittée sans identifiant de séance, ou refusée sans code
    définitif. Tant qu'il n'a pas abouti, l'étape d'envoi n'envoie rien
    d'aucune séance.

  Tant que le tableau de bord ne tient pas la séance pour démarrée, sa réponse
  à la question se lirait comme une demande d'arrêt : elle n'est donc pas
  posée avant, ni pour un lancement dont le serveur n'a pas pris le départ (la
  séance est alors déjà arrêtée). La question elle-même n'a pas d'attente
  d'échec : restée sans réponse, elle est reposée 3 s plus tard.
- **La sortie n'attend pas.** À la sortie de la console, la tâche du lien est
  annulée avant l'arrêt du variateur ; une étape qui attendait le disque ou le
  réseau rend la main à l'instant, et le fil de lecture, démon, ne retient pas
  le processus. L'ordre de sortie reste : variateur arrêté d'abord.

### 8.7 Limites

- Une séance n'est **close** côté tableau de bord qu'une fois son
  enregistrement fermé par le fil du journal. Si le disque ne le ferme jamais,
  la fin part quand même, 10 s après que le runtime a fini la séance, avec la
  raison du runtime.
- Derrière un défaut variateur verrouillé, l'enregistrement est fermé quand la
  phase atteint `DONE` ([15.1](#151-où-et-quand)) : la séance est close côté
  tableau de bord à ce moment-là, alors que le runtime attend encore le
  réarmement. Le battement de cœur continue de la nommer : la machine reste
  « en séance » pour le site tant que le runtime n'a pas fini. La console ne
  demande plus au tableau de bord si un arrêt est voulu : il tient la séance
  pour finie, et sa réponse se lirait comme une demande d'arrêt.
- Une étape d'envoi enchaîne quelques lectures et quelques requêtes, chacune
  bornée (2 s par lecture, 10 s pour la liste du démarrage, 3 s par requête).
  Sur un disque ou un réseau lents sans être morts, elle peut donc durer
  plusieurs secondes. Mesuré en temps simulé, 100 minutes à rattraper : 7,5 s
  avec chaque requête à 2,5 s ; avec en plus des lectures à 1,8 s, 14,7 s
  pour une séance armée en plein rattrapage (le cas de 8.6) et 18,3 s pour une
  séance armée au démarrage même de la console. Le battement de cœur, les
  programmes et le poll, qui sont dans cette étape, attendent d'autant (le
  seuil hors ligne du tableau de bord est à 90 s). Ni la déclaration ou la
  confirmation de la séance en cours, ni la demande d'arrêt du tableau de
  bord n'y sont : elles ont leur tâche (8.6). Le tic, l'arrêt à la console et
  le superviseur n'en dépendent pas non plus.
- Une séance en cours dont l'enregistrement ne peut pas être lié à elle
  (manifeste qui ne se lit pas, lectures du disque qui ne reviennent pas,
  aucun dossier pour un lancement du tableau de bord) est quand même dite au
  tableau de bord et suivie pour l'arrêt. Rien d'elle n'est envoyé, elle n'a
  pas de curseur, et la console le dit 10 s après le départ (8.1). Sa fin
  part de la mémoire de la console, avec la raison du runtime, quand la séance
  finit. **Si la console s'arrête avant** (coupure, processus tué, sortie
  ordinaire), **rien ne clôt cette séance côté tableau de bord** : elle y
  reste active, sans mesures. Aucune fonction du serveur ne clôt une séance
  sans la fin envoyée par la console : un arrêt demandé sur le site n'est
  qu'une demande faite à la machine, et une machine passée hors ligne garde
  ses séances. La console n'envoie cette fin à un démarrage suivant que si
  l'enregistrement se lit de nouveau : sans curseur et absent de la liste des
  enregistrements antérieurs, il est alors envoyé depuis son début, puis clos
  ([8.4](#84-après-un-redémarrage-de-la-console)). Tant qu'il ne se lit pas,
  il est laissé à chaque démarrage, et le journal le dit
  (`cannot be read (...): left until the next start`).
- Une séance déclarée sans son enregistrement, dont l'enregistrement
  n'apparaît qu'après la fin de la séance, est envoyée au démarrage suivant
  sous la référence de son manifeste : le tableau de bord en garde alors deux
  séances, l'une sans mesures.
- Une console qui n'enregistre pas (construite sans journal, ce qui n'arrive
  que dans les tests) déclare et termine ses séances, sans télémétrie.
- Face à un Convex antérieur au contrat 1.1 : les événements restent dus, un
  lot renvoyé peut être stocké deux fois, et la séance est datée par la
  console ([deploiement.md](deploiement.md#36-ce-que-demande-la-synchronisation-par-relecture-du-journal)).
- Une fin sans enregistrement (un lancement refusé, ou une séance sans
  enregistrement) n'a qu'une place en mémoire : si une seconde arrive avant
  que la première soit partie, la plus ancienne est abandonnée, et le journal
  le dit.

Vérifié par les tests automatiques, en simulation et en temps simulé
(`tests/test_record_cursor.py`, `test_record_upload.py`,
`test_record_uplink.py`, `test_cloud_journal_wiring.py`, et le test
d'acceptation `test_cloud_journal_e2e.py`, dont les requêtes sont rejouées
contre les vraies fonctions Convex par `convex/journalTrace.test.ts`). Non
vérifié : aucun déploiement Convex réel n'a été contacté, aucune carte SD
n'a été mesurée, et le test « tue » la console en l'abandonnant en mémoire,
pas par un vrai SIGKILL (celui de l'enregistrement est dans
`test_record_abrupt_stop.py`). Le champ `recordDegraded` est envoyé au premier
niveau du corps du battement de cœur, hors de `live` ; le côté Convex ne le
lit pas encore et ne l'affiche nulle part.

---

## 9. Les capteurs BITalino

Choisis par `SENSORS` (défaut : `ECG` seul). La console acquiert à 1000 Hz
(`ECG_SAMPLE_RATE`, fixe). A1-A4 arrivent sur 10 bits, A5-A6 sur 6 bits.

**Aucune de ces mesures ne commande le moteur.** La FC de régulation vient de
`signal_processing.py` via `ecg_pipeline.py`. Seul le processeur ECG typé sert
de **veto** : il peut rendre une FC inutilisable, jamais en fournir une. Un
indicateur n'est publié que si la qualité est `GOOD` ; sinon un tiret.
Qualités possibles (`SignalQuality`) : `no_signal`, `mains_dominated`, `noisy`,
`good`.

| Voie | Canal | Fenêtre | Mesures | Limites honnêtes |
|---|---|---|---|---|
| ECG | A1, 10 bits | 10 s | FC (affichage), intervalle RR moyen, RMSSD, battements | Détecteur Pan-Tompkins indépendant de BioSPPy. Veto : doit juger `GOOD` et être à 5 bpm près de la FC de régulation. |
| EDA | A2, 10 bits | 20 s | niveau tonique SCL (µS), fréquence des réponses SCR, amplitude moyenne, nombre | Pleine échelle 25 µS. Électrode décollée, artefact de mouvement (> 10 µS/s) détectés. |
| SpO2 (voie « SPO2 ») | A3, 10 bits | 10 s | fréquence du pouls, indice de perfusion **relatif**, irrégularité du pouls (CV) | **La SpO2 n'est pas mesurable** : une seule longueur d'onde, pas de rapport rouge/infrarouge. Le champ `spo2` vaut toujours `None`, avec la raison affichée. L'indice de perfusion n'est pas comparable à un moniteur clinique. |
| RESP | A4, 10 bits | 30 s | fréquence respiratoire (4-60 /min), amplitude (% d'échelle), régularité, plus longue pause | Pas d'étalonnage absolu (dépend du serrage de la ceinture). Une apnée est **montrée**, pas traitée par la sécurité. |
| EMG | A5, **6 bits** | 5 s | amplitude efficace, activation (%), fréquence médiane, crête | Pas de quantification de 0,051 mV : un muscle relâché est sous un pas de quantification et peut se lire « pas de signal ». |
| LUX | A6, **6 bits** | 60 s | niveau (%), variation, **rotation estimée par scintillement** (tr/min de sortie), changements brusques | Suppose une seule source lumineuse fixe ; en pourcentage de l'échelle, pas en lux. Sert de contrôle croisé indépendant de la vitesse. |

---

## 10. La caméra (présence)

`src/presence/`, activé par `PRESENCE_SOURCE` : `none` (défaut, pas de
caméra), `sim_empty` (caméra **simulée**, capsule vide), `sim_occupied`
(caméra simulée, passager attaché). **Il n'existe pas de vraie caméra** : le
module qui lirait un détecteur réel (`src/presence/detector_link.py`) n'est pas
écrit. Une caméra simulée ne regarde jamais la vraie machine.

La caméra est lue à 20 Hz. Deux régimes :

**Mouvement possible** (en rotation, armé, ou état inconnu) : verdicts
**verrouillés**.

| Id | Condition | Délai | Action |
|---|---|---|---|
| `presence_intrusion` | personne dans la zone, confiance ≥ 0,50 (maintenue jusqu'à 0,30) | 0,10 s ; aucun si confiance ≥ 0,80 ou distance ≤ 0,5 m | QUICK_STOP |
| `presence_bench_occupied` | séance banc, capsule vue occupée | 0,2 s | QUICK_STOP |
| `presence_camera_lost` | pas d'image saine récente depuis > 0,5 s, ou détecteur en échec | aucun au-delà | RAMP_DOWN |
| `presence_zone_uncertain` | détection persistante à confiance ≥ 0,30 | 1,0 s | RAMP_DOWN |
| `presence_rider_absent` | séance occupée, capsule vue vide | 1,0 s | RAMP_DOWN |
| `presence_capsule_unknown` | séance occupée, capsule inconnue | 3,0 s | RAMP_DOWN |
| `presence_rider_unbuckled` | séance occupée, harnais vu défait | 0,5 s | RAMP_DOWN |
| `presence_limb_outside` | séance occupée, un membre hors de la capsule | 0,3 s | RAMP_DOWN |

**Au repos** : rien ne se verrouille ; les mêmes règles deviennent une **porte
de départ** qui refuse (`presence_latched` si un verdict caméra n'est pas
acquitté, `presence_zone_not_clear` si la zone n'a pas été vue dégagée pendant
2,0 s, etc.). Avec une caméra, l'acquittement de la console efface aussi le
verrou de présence (`PresenceAcknowledger`).

Limite connue (écrite dans `src/presence/README.md`) : l'intrusion passe par
`request_estop`, donc elle est enregistrée comme `operator_estop` et
l'acquittement demande « champignon relâché » alors que personne ne l'a touché.

---

## 11. L'âge minimum du passager

* `MIN_RIDER_AGE` (défaut **18**, bornes 10..100) : âge minimum pour une séance
  **programmée**. Un âge **inconnu est un refus**.
* L'âge vient du formulaire de la console, ou du lancement distant (calculé à
  partir de l'année de naissance saisie sur le tableau de bord).
* Messages : « demarrage refuse : age du passager requis pour une seance
  programmee » ; « demarrage refuse : passager de N ans, minimum M ans
  (MIN_RIDER_AGE) ».
* Marqué `[MED]` : abaisser ce seuil est une décision médicale, prise dans
  `.env`, jamais depuis le tableau de bord.
* Ne s'applique pas aux séances manuelles (elles ne portent pas d'âge).

---

## 12. La configuration (`.env`)

### 12.1 Clés lues par la console (`src/local_config.py`)

Booléens acceptés : `1/true/yes/on` et `0/false/no/off`. Une valeur vide vaut
« non renseignée ». Toutes les erreurs sont listées d'un coup, puis la console
sort avec le code 2.

| Clé | Défaut | Rôle | Contrainte |
|---|---|---|---|
| `MOTOR_BACKEND` | **aucun (obligatoire)** | `sim` (variateur simulé) ou `serial` (ATV320 réel) | autre valeur refusée |
| `MOTOR_PORT` | `ftdi://schneider:rs485/1` | port du variateur : périphérique série (`/dev/ttyUSB0`, `COM3`, `/dev/cu.usbserial-…`) ou URL pyftdi | lu seulement si `serial` ; non vide |
| `MOTOR_SLAVE_ID` | `248` | adresse Modbus (248 = adresse point à point Schneider, mesurée au banc) | 1..247 ou 248 ; 0 (diffusion) refusé |
| `MODBUS_BAUDRATE` | `19200` | débit | entier |
| `MODBUS_PARITY` | `E` | parité | `N`, `E` ou `O` |
| `MODBUS_TIMEOUT_S` | `0.1` | délai de lecture par échange | nombre fini > 0 |
| `MOTOR_REG_OFFSET` | `0` | décalage de la carte de registres (0 mesuré correct au banc) | entier |
| `MOTOR_MAX_RPM` | `300` | plafond moteur en banc (≈ 6 tr/min de sortie) | 0..1380 (plaque) |
| `ECG_SOURCE` | **aucun (obligatoire)** | `sim`, `serial` (`/dev/rfcomm0`, `COM4`, MAC) ou `rfcomm` (macOS) | autre valeur refusée |
| `BITALINO_ADDRESS` | aucun | adresse du BITalino | `rfcomm` : `rfcomm:XX-XX-XX-XX-XX-XX` ; `serial` : port ou MAC, pas `rfcomm:` ; ignoré pour `sim` |
| `ARM_RADIUS_M` | **aucun (obligatoire, sans défaut)** | rayon mesuré de l'axe à l'occupant ; tout g affiché en dépend | ]0, 5] m |
| `LEG_TIP_RADIUS_M` | aucun | point du passager le plus éloigné ; la limite de dérivée de g y est jugée | [`ARM_RADIUS_M`, 5] m ; vide = jugée à `ARM_RADIUS_M` (sous-estime la charge aux pieds ; la CAO borne à 2,43 m) |
| `GEAR_RATIO` | `49.79` | rapport du réducteur (confirmé au banc) | > 0 |
| `UI_HOST` | `127.0.0.1` | adresse d'écoute de la page | hors boucle locale : `UI_TOKEN` obligatoire |
| `UI_PORT` | **`8080`** | port de la page | ≠ 8123 (port de `scripts/bench_console.py`) ; `.env.example` utilise aussi 8080 |
| `UI_TOKEN` | aucun | jeton d'accès | ≥ 16 caractères si exigé |
| `OCCUPANCY_OCCUPIED_ENABLED` | `false` | autorise « personne à bord » | booléen ; reste `false` jusqu'au jalon M6 |
| `PROGRAMS_ENABLED` | `false` | autorise les séances programmées (jalon M5) | booléen |
| `HR_HARD_MAX_BPM` | `148` | palier dur du superviseur | 100..220 |
| `HR_CRITICAL_BPM` | `158` | palier critique du superviseur | 100..220, > palier dur |
| `MIN_RIDER_AGE` | `18` | âge minimum en séance programmée | 10..100 |
| `PRESENCE_SOURCE` | `none` | caméra | `none`, `sim_empty`, `sim_occupied` |
| `SENSORS` | `ECG` | voies acquises et affichées | liste parmi `ECG,EDA,SpO2,RESP,EMG,LUX` ; `ECG` obligatoire |
| `MACHINE_API_KEY` | vide | clé de la machine sur le tableau de bord ; vide = pas de lien | - |
| `CONVEX_URL` | vide | hôte `.convex.site` | exigée si la clé est présente : `https://…` ou `http://localhost` / `http://127.0.0.1` |
| `MOTION_LIMITS_PATH` | `config/motion_limits.json` | limites anti-nausée (relatif à `raspberry-pi/`) | fichier illisible = la console refuse de démarrer |
| `RECORD_ROOT` | `data/records` | dossier des enregistrements de séance (relatif à `raspberry-pi/` s'il n'est pas absolu) | créé en mode 700 ; s'il est inutilisable, la console démarre et **refuse tout départ** ([15.4](#154-départ-refusé-sous-500-mo)) |
| `RECORD_LOCAL_RETENTION_DAYS` | `30` | durée de garde d'un enregistrement déposé **et** confirmé ; un enregistrement non déposé n'est jamais purgé | entier 0..3650 |
| `RECORD_MACHINE_ID` | `unassigned` | identifiant de la machine écrit dans le manifeste | opaque : lettres ASCII, chiffres, `_`, `-` ; 128 caractères au plus ; jamais un nom |
| `RECORD_ORGANIZATION_ID` | `unassigned` | identifiant de l'organisation écrit dans le manifeste | même règle |
| `ANHEART_SOFTWARE_VERSION` | - | **n'est plus lue.** La version écrite dans le manifeste (`software_version`) est le contenu de `raspberry-pi/VERSION` (`/app/VERSION` dans l'image), la même que celle de la page et du heartbeat ([section 14](#14-versions-et-compatibilité)). Une valeur encore réglée ne change rien et n'arrête plus le démarrage, même mal formée : la console l'écrit une fois dans son journal (`configuration: ANHEART_SOFTWARE_VERSION is set and no longer read`) | - |
| `RECORD_DRIVE_SDK_FRAMES` | `false` | diagnostic de banc de la liaison variateur : enregistre aussi les appels du SDK Modbus avec leurs octets (chaque demande envoyée, chaque fragment reçu), en plus des transactions de registre. Environ 0,95 Mo par minute au lieu de 0,2 Mo avec le vrai pilote ([15.2](#152-aucune-écriture-dans-la-boucle)) ; sans effet sur le simulateur | `true` ou `false` |

Contenu livré de `config/motion_limits.json` (tout est marqué `[MED]`, à valider
par le médical) : 0,25 tr/min de sortie/s, 0,03 g/s, consigne non nulle minimale
55 tr/min moteur, intervalle maximal 1,0 s.

### 12.2 Minimum pour une simulation complète

```bash
cd raspberry-pi
MOTOR_BACKEND=sim ECG_SOURCE=sim ARM_RADIUS_M=1.5 .venv/bin/python -m src.local_panel
```

Sans fichier `.env`, la page écoute sur `http://127.0.0.1:8080/` (défaut du
code et `.env.example`). `UI_PORT` permet de choisir un autre port. Si une
des trois clés manque, la console affiche une ligne `configuration: CLE: raison`
par clé fautive et sort avec le code 2.

### 12.3 Clés lues hors du code Python

Deux clés des fichiers `.env` ne sont pas lues par `src/local_config.py` :

| Clé | Lue par | Rôle |
|---|---|---|
| `BITALINO_MAC` | `docker/entrypoint.sh`, `scripts/pi/preflight.sh`, `scripts/pair_device.sh` | adresse du BITalino appairé ; le conteneur lie `BITALINO_ADDRESS` (`/dev/rfcommN`) à cette adresse au démarrage |
| `MPLBACKEND` | matplotlib (importé par BioSPPy), dans l'environnement du processus | `Agg` : pas d'affichage graphique. Le `Dockerfile` la fixe lui-même ; sur le Pi, le service la transmet aussi depuis `/etc/anheart/anheart.env` |

Les clés de l'ancien enregistreur (`SAMPLE_RATE`, `OUTPUT_SAMPLE_RATE`,
`BATCH_INTERVAL_MS`, `HEARTBEAT_INTERVAL_S`, `BUFFER_DB_PATH`, `LOG_LEVEL`) ne
sont plus lues par rien : restées dans un `.env` existant, elles sont ignorées.
Le niveau de journalisation de la console est fixe (`INFO`).

---

## 13. Le contrat de code strict et la gate

Le contrat complet est dans `.claude/skills/anheart-strict-python/SKILL.md`
(copie dans `.agents/`). En résumé :

1. **Unités typées** : jamais de nombre nu pour une grandeur physique ;
   `MotorRpm` et `OutputRpm` sont des types distincts.
2. **Interdits** : `Any` (explicite ou implicite), `dict`/`list` nus dans les
   signatures, `str` porteur de sens (utiliser une `Enum`), `# type: ignore` sans
   code, `assert` comme contrôle de flux, lecture directe de l'horloge.
3. **Erreurs** : `Result[T, E]` avec unions fermées dans les chemins moteur et
   sécurité ; chaque `match` se termine par `assert_never` (dans un dernier
   cas générique, ou après le `match` par `raise assert_never(sujet)` quand
   chaque cas rend une valeur), si bien qu'une nouvelle variante d'erreur casse
   la vérification de types partout où elle n'est pas traitée.
4. **Horloge injectée** partout (`src/clock.py`).
5. **Une bibliothèque non typée = un seul module** qui l'importe, avec un stub
   écrit à la main dans `stubs/` (`bitalino`, `biosppy`, `scipy`, `pymodbus`,
   `pyftdi`, pyobjc).
6. **Analyser à la frontière matérielle**, faire confiance aux types derrière.
7. **100 % des branches** sur la chaîne de sécurité ; `# pragma: no cover` n'y
   est pas accepté. Les tests de propriétés (`hypothesis`) portent les vraies
   garanties.
8. **Invariants de sécurité** : la sécurité l'emporte toujours sur la loi de
   commande ; mesures fraîches seulement ; keepalive en premier ; ne jamais
   supposer l'état du variateur au démarrage ; aucun réarmement automatique de
   défaut ; les verdicts verrouillés exigent un acquittement. Les avertissements
   non verrouillés laissent la consigne suivre de nouveau seule la régulation
   ou la cible tant que le bras tourne ; une consigne revenue à 0 en cours de
   séance sans que personne l'ait demandé termine la séance (section 5) ; rien
   ne bloque la boucle.

La règle 10 du contrat donne quatre formes d'écriture que l'analyse statique
du dépôt accepte comme les vérificateurs de types : `match` qui rend une
valeur terminé par `raise assert_never(sujet)`, motif de classe qui lit le
champ sur la valeur reconnue, membre de protocole abstrait dont la
documentation est le seul corps, jamais deux modules qui s'importent l'un
l'autre. Elles sont détaillées dans
[framework-de-test.md, « Traiter un constat »](framework-de-test.md#analyse-statique-externe--codeql-anh-196).

Exceptions en cours (dans `pyproject.toml`) : `signal_processing.py` et
`scripts/` sont hors vérification de types, alors que le premier calcule la FC
de régulation. Plus aucun fichier de la chaîne de sécurité n'est hors de la
porte de couverture : la liste `coverage_pending` est vide, et
`tests/test_typing_contract.py` échoue si un fichier y revient.

### La gate

Depuis `raspberry-pi/` :

```bash
./scripts/check.sh
```

Elle enchaîne, en continuant même après un échec :

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/basedpyright
.venv/bin/python -m mypy .
.venv/bin/python -m pytest --cov --cov-branch --cov-fail-under=100
```

et affiche `GATE PASSED` ou `GATE FAILED: <étapes>`. Le script est suivi avec
le mode exécutable `100755` ; `bash scripts/check.sh` reste possible. Les tests
marqués `hardware` (BITalino ou ATV320 branché) sont exclus par défaut
(`-m 'not hardware'`). Pour le nombre de tests du checkout courant :
`.venv/bin/python -m pytest --collect-only -q`.

Résultat historique du 2026-10-01 sur macOS (Apple silicon, Python 3.12.13), sans
matériel : ruff et le format passent, basedpyright « 0 errors, 0 warnings »,
mypy « Success: no issues found in 129 source files », **3187 tests passés**,
couverture de branches **100,00 %** sur le périmètre de la gate, `GATE PASSED`,
en 21 min 26 s pour les tests (machine chargée par d'autres processus ce
jour-là ; la durée varie).

---

## 14. Versions et compatibilité

`src/contract.py`. Le contrat entre la console et Convex porte un numéro
`majeure.mineure` (aujourd'hui `1.1`). Sa définition unique est
[`contracts/machine-api.json`](../contracts/machine-api.json) à la racine du
dépôt ; la description complète des règles est dans
[convex.md](convex.md#11-versions-et-compatibilité).

Ce que fait la console :

* **Chaque requête** porte l'en-tête `X-Anheart-Contract: 1.1`. Il est posé sur
  le client HTTP lui-même, pas requête par requête : aucune ne peut partir sans.
* **Chaque heartbeat** porte `software_version`, `contract_version`,
  `medical_parameters_version` et `config_hash`. Les deux derniers valent `null` :
  la console n'a encore ni fichier de paramètres médicaux signé ni empreinte de
  configuration.
* **La version logicielle** est lue une fois, au démarrage, dans le fichier
  `raspberry-pi/VERSION` (`/app/VERSION` dans l'image Docker). Le dépôt y porte
  `pi-0.0.0-dev` ; le script de release (`scripts/release.sh`, voir
  [release.md](release.md)) y écrit le tag de chaque version. Fichier absent, illisible ou mal formé : la console démarre
  quand même et annonce `pi-unknown`, jamais une version devinée. Cette valeur
  unique sert trois fois : la page l'affiche (pastille **Version**), chaque
  heartbeat l'annonce, et le manifeste de chaque enregistrement de séance la
  porte (`software_version`). Aucune variable d'environnement ne la règle :
  `ANHEART_SOFTWARE_VERSION`, qui réglait autrefois celle du manifeste seul,
  n'est plus lue ([12.1](#121-clés-lues-par-la-console-srclocal_configpy)).
* **Un serveur qui refuse le contrat** (réponse 426, code
  `contract_unsupported`) : la console l'écrit dans son journal et affiche
  `serveur incompatible (contrat X vs Y)`, X étant sa version et Y les
  majeures que le serveur dit servir. Elle continue de fonctionner comme sans
  tableau de bord.
* **Un serveur d'une autre majeure** : chaque réponse de
  `/api/machine/training/poll` annonce `server_contract_version`. Si sa majeure
  n'est pas celle de la console, ou si la réponse n'annonce aucune version
  lisible, **rien n'est armé** : le lancement n'atteint jamais la boîte aux
  lettres. La console affiche `serveur incompatible (contrat X vs Y)` (Y vaut
  `inconnu` sans version lisible) et renvoie le lancement comme séance échouée,
  raison `refusee par la machine : serveur incompatible (contrat X vs Y)`.
* **Un arrêt traverse toutes les versions.** La route
  `/api/machine/training/status` répond quel que soit le contrat annoncé, et
  une réponse qui porte `stopRequested: true` ou `active: false` arrête la
  séance en cours quelle que soit la majeure qu'elle annonce, version absente
  ou illisible comprise : un arrêt a le même sens partout, ces deux champs ne
  peuvent provoquer qu'un arrêt ordinaire, et le refuser ne serait jamais le
  côté sûr. C'est **tout** ce qui est retenu d'une réponse d'une autre
  majeure : aucun autre champ n'est lu, et rien n'y peut lancer, reprendre ou
  réarmer quoi que ce soit.
* **Les refus du serveur** arrivent sous la forme
  `{error: <code stable>, message: <texte>}`. Le code est ce que le journal de
  la console porte (`session_not_found (HTTP 400): Session not found`) ; un
  refus identique au précédent n'est pas réécrit.

L'affichage sur la console est un événement de type `dashboard`, sans nom
d'opérateur, dans la liste **Evenements** (voir
[console-locale.md](console-locale.md)). Ce n'est pas un événement `refused` :
la page lit un `refused` comme la réponse de la machine à propos de la cible
manuelle à l'écran et l'écrit dans la carte Mode MANUEL, ce que cette nouvelle
du lien n'est jamais. Il est
émis pour chaque lancement refusé, quand la phrase change, et sinon rappelé
toutes les 60 s tant que l'incompatibilité dure (`INCOMPATIBLE_REPEAT`) : la
liste n'est envoyée qu'aux écrans connectés à ce moment-là, et une page ouverte
plus tard doit l'apprendre aussi. Le journal, lui, ne l'écrit qu'au changement.

**L'état du lien, en permanence.** En plus de cet événement, la page porte la
pastille **Serveur**, sur toutes les pages : `joignable`, `injoignable`,
`incompatible`, `cle refusee`, `en erreur`, `en attente` ou `non configure`
([console-locale.md](console-locale.md#pastille-serveur--le-lien-avec-le-tableau-de-bord)).
`src/link_state.py` tient cet état à partir de ce que chaque échange du lien a
dit, et `/api/panel` le sert :

* le lien ne fait **aucune requête pour lui** : il note ce que disent le
  heartbeat, les lancements, l'envoi des séances et la question d'arrêt, qu'il
  fait de toute façon. La page le lit par la route qu'elle interroge déjà
  chaque seconde. Rien n'en est lu ni écrit dans le tic de commande ;
* une clé refusée (401, 403) et un contrat refusé (426) sont affichés à la
  première réponse qui le dit, et tiennent jusqu'à la preuve du contraire. La
  route `/api/machine/training/status`, qui répond sous n'importe quel contrat
  pour qu'un arrêt passe toujours, ne prouve rien du contrat : pendant une
  séance sous un 426, la pastille reste `incompatible` alors que cette route
  répond toutes les 3 s ;
* un silence, ou des erreurs du serveur (5xx, 408, 425, 429), ne changent
  l'affichage qu'après **25 s** sans aucune réponse utilisable
  (`UNREACHABLE_AFTER`, deux heartbeats et demi) : une requête lente ou perdue
  ne fait pas clignoter la pastille. Le retour est immédiat ;
* le motif affiché sous la pastille ne reprend, de ce que le serveur envoie,
  que le code HTTP, le code stable et les majeures servies, sur une ligne de
  160 caractères au plus. La phrase du serveur reste dans le journal.

Pourquoi la version du contrat est une constante du code, et non lue dans le
fichier partagé à l'exécution : la console est déployée avec le seul dossier
`raspberry-pi/` (`scripts/pi/deploy.sh`, contexte de construction Docker), donc
`contracts/machine-api.json` n'est pas sur la machine. Les constantes de
`src/contract.py` sont épinglées à ce fichier par `tests/test_contract.py` : un
changement fait d'un seul côté échoue à la gate.

### Matrice de compatibilité

La matrice (version du Pi, majeure de contrat, version Convex minimale) est
tenue dans [convex.md](convex.md#matrice-de-compatibilité), une seule fois pour
les deux côtés.

Non fait par ce logiciel :

* Hors séance, l'incompatibilité n'est écrite dans aucun enregistrement :
  `events.jsonl` n'existe que pendant une séance. Pendant une séance, elle y
  est consignée comme un événement `warning` dont le détail commence par
  `dashboard:` ([15.1](#151-où-et-quand)).
* Aucun déploiement Convex réel n'a été contacté avec ce contrat : les tests
  utilisent un transport factice des deux côtés.
* La pastille **Serveur** dit l'état du lien, pas ce qu'il reste à envoyer :
  le nombre de séances que le tableau de bord n'a pas encore reçues entières
  n'est pas montré sur la page.
* Une incompatibilité que la console a lue dans une annonce du serveur
  (`/api/machine/training/poll`) n'est levée que par l'annonce suivante, et la
  console ne pose cette question qu'au repos avec `PROGRAMS_ENABLED=true` :
  pendant une séance, la pastille garde ce que la dernière annonce a dit.

Sous un 426, rien n'est acquitté et **rien n'est abandonné**
([8.3](#83-ce-que-chaque-réponse-fait-au-curseur)) : la séance en cours
continue d'être enregistrée sur le disque, son curseur ne bouge pas, et la
console redemande toutes les 15 s. Quand le serveur sert de nouveau la majeure
de la console (ou que la console, mise à jour et redémarrée, parle celle du
serveur), la séance est déclarée, envoyée entière et **close avec sa raison**.
D'ici là elle reste `active` côté Convex pour une séance lancée du site, et
inconnue de Convex pour une séance démarrée à la machine.

Le cas d'un changement de majeure **au milieu** d'une séance est rejoué sur la
console entière, en simulation, par
`tests/test_link_indicator.py::test_acceptance_a_dashboard_answers_426_in_the_middle_of_a_session` :
une séance déjà connue du tableau de bord, qui se met à répondre 426. La page
lit `incompatible` tant que cela dure, la séance continue puis se termine à la
console, rien n'atteint le tableau de bord et rien n'est abandonné ; quand la
majeure est de nouveau servie, la page lit `joignable`, chaque seconde de
l'enregistrement est sur le tableau de bord une seule fois, et la séance y est
close avec `operator_stop`. Le tableau de bord de ce test est un double en
mémoire, pas un déploiement Convex.

---

## 15. L'enregistrement de séance (boîte noire locale)

Pendant toute séance, manuelle ou programmée, banc ou personne à bord, la
console écrit l'enregistrement de la séance sur le disque du Pi, au fil de
l'eau, au format décrit dans [enregistrement.md](enregistrement.md). Le but :
qu'un arrêt brutal (processus tué, coupure de courant) laisse un
enregistrement lisible jusqu'aux dernières secondes, **sans que l'écriture
puisse jamais ralentir ni arrêter la boucle de contrôle**.

> **Ce qui a été vérifié, et ce qui ne l'a pas été.** Tout ce qui suit est
> couvert par les tests automatiques, sur la console en simulation
> (`tests/test_record_*.py`, `tests/test_exit_deadline.py`,
> `simulation/tests/test_record_console_parity.py`).
> Rien n'a été mesuré sur un vrai Pi ni sur une carte SD : les durées
> d'écriture réelles, l'usure de la carte, le nombre d'inodes de sa partition,
> la durée de fermeture d'un long enregistrement, le débit des trames du vrai
> variateur et le comportement à une vraie coupure de courant ne sont pas
> connus. Le test de coupure tue le processus (`SIGKILL`) ; il ne coupe pas
> l'alimentation. Le système de fichiers à court d'inodes et le disque qui ne
> répond plus sont simulés dans les tests, jamais provoqués sur un vrai disque.

### 15.1 Où, et quand

Un dossier par séance sous `RECORD_ROOT` (défaut `data/records/`, relatif à
`raspberry-pi/`), nommé par l'heure UTC du début et l'identifiant de la
séance : l'identifiant Convex pour un lancement distant, sinon une référence
locale aléatoire.

| Moment | Ce qui se passe |
|---|---|
| Démarrage de la console | le dossier racine est créé s'il manque, mis en mode 700, et son espace libre est mesuré, en octets et en inodes |
| Le runtime vient d'armer une séance | le dossier de la séance est créé avec son manifeste d'ouverture (`ended_at` et `end_reason` à `null`) |
| Chaque tic (5 Hz) | une ligne de `ticks.csv` ; un événement à chaque changement de phase, de verdict ou de défaut variateur |
| Chaque appel au variateur | une ligne de `drive_frames.jsonl` : ce qui a été demandé, ce qui a été répondu, en combien de temps (15.2) |
| Chaque lot du BITalino | un bloc brut `ecg_raw/NNNNNN.bin.gz`, toutes voies, **avant** tout traitement |
| Chaque seconde | les indicateurs des capteurs dans `sensors.csv` |
| Événement de la console pendant la séance | demande de fin, E-STOP, réarmement, attestation (`operator_action`, ou `remote_command` si elle vient du tableau de bord) ; acquittement (`verdict_ack`) ; refus (`refusal`) ; nouvelle du lien avec le tableau de bord, par exemple `serveur incompatible (contrat 1.1 vs 2.0)` (`warning`, détail préfixé `dashboard:` : ce n'est pas le refus d'une demande faite sur la console, et le format n'a pas de type propre pour elle) |
| La phase de la séance atteint `DONE` | événement `end`, manifeste final (`ended_at`, `end_reason`, observation finale), `checksums.sha256` |
| Sortie de la console en cours de séance | même fermeture, **après** l'arrêt du variateur, avec le motif du runtime (`shutdown` si rien d'autre n'avait déjà mis fin à la séance) et le compte rendu de l'arrêt ; la sortie attend le fil du journal au plus 5 s, sans emprunter de fil à personne |
| Événement de la console sans séance enregistrée | une ligne du journal hors séance, `logbook/events.jsonl` (15.9) : départ demandé, départ refusé, acquittement, réarmement, nouvelle du lien avec le tableau de bord |

La fermeture suit la phase `DONE` et non l'état `FINISHED` du runtime, exprès.
Après un STOP ordinaire, c'est le même tic. Derrière un défaut variateur
verrouillé, le runtime reste `ENDING` jusqu'à ce qu'un opérateur réarme le
défaut, ce qui peut être le lendemain matin : la descente et la récupération
surveillée sont finies, et un enregistrement resté ouvert grossirait de cinq
lignes par seconde sans personne à bord. Conséquence : ce que l'opérateur fait
**après** la fermeture (acquitter, réarmer) n'est pas dans l'enregistrement de
la séance, qui ne change plus une fois fermé. C'est écrit dans le journal hors
séance (15.9), comme les départs refusés.

`end_reason` reprend le motif du runtime (`programme_complete`,
`operator_stop`, `emergency_stop`, `safety_verdict`, `tick_exception`,
`shutdown`). Deux valeurs viennent de l'enregistreur lui-même, quand un
enregistrement est fermé sans que le runtime ait mis fin à sa séance :
`interrupted` (une séance démarre alors que la précédente n'avait pas été vue
finir, ou la console ferme un enregistrement dont le runtime n'a pas donné de
motif) et `superseded` (le fil du journal reçoit l'ouverture d'un
enregistrement alors que le précédent n'a pas été fermé). Aucune des deux
n'est attendue en fonctionnement normal : les voir dans un manifeste signale
un défaut à examiner.

### 15.2 Aucune écriture dans la boucle

Deux objets, deux fils :

- `SessionRecorder` (`src/record/session.py`) vit sur la boucle de la console.
  Il construit des valeurs figées et les remet à une file en mémoire. Il
  n'ouvre, n'écrit et ne vide aucun fichier.
- `Journal` (`src/record/journal.py`) possède la file et **un** fil,
  `record-journal`, seul à toucher le disque. Il vide la file toutes les
  0,2 s, écrit par le writer partagé, et appelle `fsync` **toutes les 2 s,
  jamais plus souvent** (plus une fois à l'ouverture et une à la fermeture
  d'un enregistrement).

Remettre une valeur à la file, c'est un ajout à une `deque` : pas de verrou,
pas d'appel système, pas d'attente. La file est bornée à 4096 valeurs et à
200 000 échantillons bruts en attente (environ une demi-minute de six voies) ;
au-delà, la valeur la plus récente est refusée et comptée.

**Les trames du variateur ne passent pas par cette file**, ni par la boucle.
Elles attendent dans une liste bornée à part (4096 observations,
`src/record/drive_tap.py`) que le fil du journal vient prendre à chaque cycle,
toutes les 0,2 s. Noter une observation, c'est un ajout à un bout d'une
`deque` ; le fil du journal prend par l'autre bout, une observation à la fois,
**sans prendre aucun verrou** : arrêté n'importe où, même au milieu de sa
prise, il ne tient rien dont un appel au variateur a besoin. Le seul verrou
est entre ceux qui notent (pour que la borne et le compte restent exacts si
deux d'entre eux notent en même temps), et le fil du journal n'y touche
jamais. Une première version prenait la liste sous un verrou partagé avec
celui qui note : `test_record_tick_isolation.py`, qui arrête le fil du journal
avant chacune de ses instructions et fait les appels d'un tic, l'a refusée
(l'appel au variateur attendait quand ce fil était arrêté dans sa prise).
Selon le variateur branché :

- le pilote ATV320 rapporte lui-même ses échanges Modbus dans un journal
  d'échanges désormais borné (`ExchangeLog(clock, capacity)`), depuis ses
  propres fils de travail : rien n'est placé entre le runtime et le pilote.
  Par défaut, ce sont les **transactions de registre** (`modbus_read`,
  `modbus_write`) : ce qui a été demandé à quel registre, ce qui a été
  répondu, en combien de temps. Les appels du SDK Modbus (`modbus_send`,
  `modbus_receive_chunk`, avec leurs octets) ne sont gardés que si
  `RECORD_DRIVE_SDK_FRAMES=true` : voir plus bas ;
- un variateur qui ne rapporte rien (le simulateur) est enveloppé par une
  prise (`CallTap`) qui délègue chaque appel, rend la réponse telle quelle,
  puis note l'appel : les mêmes observations d'appel que la simulation
  (`open`, `close`, `command`, `speed`, `emergency_zero`, `read_status`,
  `read_failed`, `read_limits`), avec les mêmes registres. Jamais les deux à
  la fois.

Au-delà de la borne, l'observation la plus récente est refusée et comptée ;
une observation impossible à construire est comptée de même, et ne remonte
jamais vers l'appelant : la réponse du variateur est rendue quoi qu'il arrive
à son observation. Si la liste elle-même ne peut pas être prise (une faute de
ce qui écoute le variateur), le cycle continue : les lignes, les événements et
les blocs bruts sont écrits, les trames de ce cycle sont perdues et comptées
comme une écriture manquée de l'enregistrement (`write_failed`,
`erreur d'ecriture (append : <faute>)`), et la faute est journalisée une fois.

Les trames prises sont remises à un enregistrement à trois moments du cycle :
avant qu'un enregistrement soit fermé, à celui-là (une séance ouverte **et**
fermée pendant que le fil était occupé ailleurs, par exemple par la longue
fermeture de la précédente, garde donc ses trames) ; avant qu'un
enregistrement soit ouvert par-dessus un autre resté ouvert, à celui resté
ouvert ; et une fois la file écrite, à l'enregistrement alors ouvert (les
trames d'un armement vont à la séance qu'il arme, avec un `t` légèrement
négatif). Quand un autre enregistrement est ouvert plus loin dans le même
cycle, les trames commencées à partir de son début l'attendent. Sans aucun
enregistrement ouvert, la scrutation d'une console au repos n'est écrite nulle
part.

Ce que cela ajoute à un enregistrement, mesuré, dans **un** fichier ajouté en
fin, donc sans aucun inode de plus :

| Variateur | `RECORD_DRIVE_SDK_FRAMES` | Lignes | Taille par minute à 5 Hz |
|---|---|---|---|
| simulateur (console en simulation) | sans effet | 11,5 par seconde : les observations d'appel | 80 ko |
| pilote ATV320, liaison FTDI simulée | `false` (défaut) | 6 par tic : une écriture de consigne et une lecture d'état de cinq registres | 0,21 Mo |
| pilote ATV320, liaison FTDI simulée | `true` | 24 par tic : les mêmes six, plus pour chacune la demande envoyée et les fragments de sa réponse | 0,94 Mo |

**`RECORD_DRIVE_SDK_FRAMES` est un diagnostic de banc**, éteint par défaut.
Les transactions de registre disent ce que le variateur a répondu, ce qu'il
faut pour analyser un défaut ; les appels du SDK n'y ajoutent que les octets
du fil et leurs durées, utiles pour chercher une panne de la liaison série
elle-même (réponses fragmentées, délais, octets parasites). Allumé, il
multiplie les lignes par quatre et fait de `drive_frames.jsonl` sept fois le
reste d'un enregistrement de deux voies : à allumer le temps d'un essai, pas à
laisser sur une machine en service. Il ne retire ni ne change aucune ligne de
registre, et ses lignes ne prennent pas de place dans la liste bornée quand il
est éteint. Ces chiffres viennent d'une liaison simulée : le nombre de
fragments par réponse dépendra de la vraie liaison série, à mesurer sur le
banc avant de dimensionner le disque.

Ce que le fil du journal fait aussi, parce que ce sont des accès au disque :
mesurer l'espace libre toutes les 5 s (octets et inodes), écrire le journal
hors séance (15.9), et appliquer la rétention entre deux séances (15.5). La
porte de départ et l'état affiché ne font que lire sa dernière publication.
Pendant un cycle long (un retard à écrire sur une carte lente, la fermeture
d'un enregistrement, la rétention), il mesure et publie quand même, entre deux
fichiers, et il dit avant de commencer ce qu'il commence (`Activity` :
`closing`, `purging`) : voir 15.4.

Ce qu'un arrêt brutal peut faire perdre : ce qui n'avait pas encore été vidé
de la file (0,2 s au plus) si le processus est tué ; jusqu'à 2 s de plus si
l'alimentation tombe avec lui. Le reader relit le reste, signale l'absence de
checksums et, s'il y en a une, la dernière ligne ou le dernier bloc coupé.

### 15.3 Quand le disque refuse, se remplit ou se tait

**La séance et la sécurité continuent dans tous les cas.** Une erreur
d'écriture n'est jamais une exception dans la boucle : elle est comptée par le
fil du journal et lue par la boucle comme un état.

| Cause (`Cause`) | Quand | Message à l'opérateur (événement `recording`) |
|---|---|---|
| `write_failed` | le disque a refusé une écriture ou la création du dossier (plein, droits, E/S) | `enregistrement de seance degrade : disque plein (append). La seance et la securite continuent.` (ou `ecriture refusee, droits insuffisants`, ou `erreur d'ecriture`) |
| `queue_full` | la file bornée a refusé des valeurs, ou la liste bornée des trames du variateur a refusé des observations | `enregistrement de seance degrade : file d'ecriture pleine, des mesures sont perdues. …` |
| `stalled` | des valeurs attendent et le fil n'en a consommé aucune depuis 5 s : le disque ne répond plus | `enregistrement de seance degrade : le disque ne repond plus (<n> elements en attente). …` |
| `storage_unavailable` | le dossier racine ne peut être ni créé, ni mis en 700, ni mesuré | `enregistrement de seance indisponible : dossier <chemin> inutilisable (creation, droits ou mesure de l'espace libre)` |

Le message est dit une fois par changement de cause, puis
`enregistrement de seance retabli` si la cause disparaît. Le même état part
dans le battement de cœur vers le tableau de bord, champ `recordDegraded`
(section 8). Dès que le disque accepte de nouveau une écriture, l'enregistrement
le dit lui-même par un événement `warning` :
`record_degraded: dropped=<n> failures=<n> last=<opération>:<erreur>`, avec
`drive_frames_lost=<n>` avant `last=` quand des trames du variateur manquent.

Une ligne coupée par un disque plein est retirée du fichier (retour à la
dernière ligne complète) : l'enregistrement reste lisible et reprend si de la
place revient. Un bogue de l'enregistreur lui-même est traité de la même
façon : ses points d'entrée ne lèvent jamais, la faute est journalisée une
fois, l'enregistrement est déclaré dégradé
(`… erreur interne de l'enregistreur …`).

**`recordDegraded` parle d'un enregistrement, pas de la console.** Tous les
comptes derrière ce signal (écritures refusées, valeurs et trames perdues,
fautes de l'enregistreur lui-même) repartent de zéro à l'ouverture de
l'enregistrement suivant. Le signal reste donc vrai entre deux séances quand
le dernier enregistrement est incomplet, et redevient faux dès qu'une nouvelle
séance s'enregistre normalement, sans redémarrer la console. Avant, une faute
interne de l'enregistreur le laissait vrai jusqu'au redémarrage.

### 15.4 Départ refusé sous 500 Mo

Porte d'armement du runtime ([4.2](#42-portes-communes-à-tout-départ-_refuse_arming-_arm)),
pour toute séance : si le dernier espace libre mesuré sous le dossier
d'enregistrement est inférieur à 500 Mo (500 000 000 octets), le départ est
refusé et le variateur n'est pas touché :

`demarrage refuse : espace disque insuffisant pour l'enregistrement de seance : <n> Mo libres sous <dossier>, 500 Mo requis. Liberer de l'espace`

Un espace **illisible** refuse de la même façon
(`… enregistrement de seance impossible, espace libre illisible sous <dossier> …`) :
une séance que personne ne peut enregistrer ne part pas sur un chiffre que
personne n'a lu.

Ni sur un chiffre que personne n'a lu **récemment**. La porte ne mesure rien
elle-même, aucun appel au système de fichiers : elle lit la dernière mesure du
fil du journal et son âge. Cette mesure date de 5 s au plus tant que ce fil
tourne. S'il est bloqué sur un disque qui ne répond plus, son dernier chiffre
reste, aussi bon qu'il l'était : au-delà de **15 s** (`STORAGE_STALE_AFTER`,
trois mesures manquées), la porte refuse, quoi que dise ce chiffre :

`demarrage refuse : enregistrement de seance impossible, espace libre sous <dossier> mesure il y a <n> s : le disque ne repond plus`

Le refus se lève de lui-même à la mesure suivante.

**Un cycle long n'est pas un disque mort.** Fermer un enregistrement relit
chacun de ses fichiers pour les sommes de contrôle (18 000 pour une heure), et
la rétention supprime des dossiers entiers. Le fil du journal mesure donc le
disque et publie **entre deux fichiers** de tout ce qui dure : une fermeture
ou une purge longue sur un disque sain ne périme plus la mesure et ne refuse
plus aucun départ. Quand une seule étape dépasse quand même 15 s, la porte
refuse, et dit ce que ce fil avait annoncé commencer, au lieu d'affirmer que
le disque ne répond plus :

`demarrage refuse : enregistrement de seance impossible pour l'instant : la fermeture d'un enregistrement est en cours sous <dossier>, derniere mesure de l'espace libre il y a <n> s. Reessayer dans un instant`

(ou `la purge des enregistrements deposes est en cours`). La phrase
`le disque ne repond plus` reste celle d'une mesure périmée sans rien
d'annoncé : le fil est alors bloqué dans une écriture ordinaire. Mesuré sur le
poste de développement (SSD, cache chaud) : fermer un enregistrement d'une
heure, 18 007 fichiers et 8,8 Mo, prend 1,5 s. Sur une carte SD, cache froid,
ce temps n'est pas connu.

**Inodes.** Un enregistrement est fait de milliers de petits fichiers (un bloc
brut par lot d'acquisition, soit cinq par seconde), et un système de fichiers
peut manquer d'inodes avec des mégaoctets libres : toute écriture est alors
refusée. Le fil du journal mesure donc aussi les inodes libres, et la porte
refuse sous **36 016 inodes libres** (`MIN_FREE_INODES`) :

`demarrage refuse : plus assez de fichiers libres pour l'enregistrement de seance : <n> inodes libres sous <dossier>, 36016 requis (une seance cree des milliers de petits fichiers). Liberer de l'espace`

D'où vient ce seuil : une séance de la plus longue durée que la console mène
seule (l'heure d'une séance manuelle, `MANUAL_SESSION_LIMIT`) crée 18 000
blocs bruts et 8 autres entrées (son dossier, `ecg_raw/`, le manifeste, les
quatre flux, les sommes de contrôle), soit 18 008 inodes. La porte n'est
interrogée qu'une fois, à l'armement, et doit laisser la place de toute la
séance qu'elle arme : une fois ce nombre. Et autant encore pour ce qu'elle ne
connaît pas : un programme de plus d'une heure, la descente et la récupération
après l'heure, le journal hors séance, et le reste de ce qui partage le
système de fichiers (sur le Pi, le système lui-même). Un système de fichiers
qui ne compte pas ses inodes (il en annonce zéro au total) n'est pas refusé
pour cela. Le manque d'octets est dit avant le manque d'inodes.

Ordre de grandeur mesuré en simulation, deux voies : une séance de 10 minutes
fait 3 092 fichiers et 1,3 Mo de contenu, mais 13,5 Mo **occupés** sur un
système de fichiers à blocs de 4 ko, parce que chaque lot brut de 0,2 s est un
petit fichier. Soit environ 40 Mo et 9 000 fichiers pour 30 minutes, sans les
trames du variateur (15.2). Sur un ext4 créé par défaut (un inode pour 16 Kio,
valeur non vérifiée sur la carte du Pi), 500 Mo libres valent environ 30 000
inodes : c'est le seuil des inodes qui refuse le premier.

### 15.5 Rétention : rien n'est purgé sans dépôt confirmé

Un enregistrement n'est retiré du disque que si les trois conditions sont
réunies (`src/record/retention.py`) :

1. il porte un marqueur de dépôt confirmé, un fichier posé à côté de son
   dossier, `<nom du dossier>.deposit.json` (`schema_version`, `storage_id`,
   `confirmed_at` en UTC, `checksum`) ;
2. ce marqueur correspond au contenu présent sur le disque : son `checksum`
   est le SHA-256 du `checksums.sha256` de l'enregistrement au moment du
   dépôt. Un enregistrement encore ouvert, ou modifié depuis, n'est pas
   « celui qui a été déposé » ;
3. la confirmation date de plus de `RECORD_LOCAL_RETENTION_DAYS` jours (30 par
   défaut).

Tout doute garde l'enregistrement. La purge tourne sur le fil du journal, au
démarrage puis toutes les six heures, seulement entre deux séances, et ne
regarde que les dossiers d'enregistrement directement sous la racine.

Le curseur de synchronisation d'un enregistrement
(`<nom du dossier>.sync.json`, [8.1](#81-ce-qui-est-envoyé-vient-du-disque))
est lui aussi à côté du dossier. La purge ne le retire pas : c'est la
synchronisation qui retire, à chaque démarrage de la console, tout curseur
dont le dossier n'existe plus. Les deux ne se gênent pas : la purge retire un
dossier et son marqueur de dépôt, jamais un curseur ; la synchronisation
retire un curseur resté seul, jamais un marqueur. Entre une purge et le
démarrage suivant, le curseur d'un dossier purgé reste seul et ne sert à
personne ; un enregistrement purgé pendant qu'il attendait d'être envoyé est
laissé, le journal le dit, et le suivant est envoyé. Elle tient au même endroit un fichier
`.sync-baseline.json` : la liste, faite une fois, des enregistrements qui
étaient là sans curseur avant elle. La purge ne le touche pas ; le supprimer
ne fait rien envoyer d'ancien (la liste est refaite de ce qui n'a pas de
curseur à ce moment-là).

> **Aujourd'hui, rien n'écrit ce marqueur.** Le dépôt hors de la machine
> (Convex Storage, ANH-130) n'existe pas encore et attend un avis juridique.
> Tant qu'il n'appelle pas `confirm_deposit()`, **aucun enregistrement n'est
> jamais purgé** et le disque se remplit, jusqu'au refus de départ de 15.4.
> D'ici là, libérer de la place est un geste manuel : exporter (15.6), puis
> supprimer le dossier à la main.

### 15.6 Export

`GET /api/records` liste les enregistrements, le plus récent d'abord ;
`GET /api/records/{nom}/archive` rend un dossier en `.tar.gz`. Les deux routes
sont derrière le jeton. Le bouton « Exporter l'enregistrement » de la page
Configuration télécharge le plus récent
([console-locale.md](console-locale.md#16-lenregistrement-de-séance-boîte-noire-locale)).

La liste et l'archive lisent le disque. Elles sont **refusées (409) tant
qu'une séance est en cours**, et hors séance elles ne passent **ni par la
boucle, ni par le groupe de fils par défaut de la boucle**. Ce groupe est
celui du traitement ECG, des capteurs et de la liaison BITalino : un dossier
d'enregistrement qui ne répond plus y prendrait un fil par requête, jusqu'à ce
que la fréquence cardiaque ne sorte plus et que le superviseur termine la
séance sur une fréquence périmée. Une séance arrêtée par le disque, ce que
l'enregistrement ne doit jamais faire. Les lectures d'enregistrements ont donc
leurs propres fils (`RecordIo`, `src/record/export.py`) :

- deux au plus à la fois, chacun un fil démon nommé `record-io` ;
- une lecture de plus est refusée tout de suite (503
  `lecture des enregistrements deja en cours : reessayer dans un instant`),
  jamais mise en attente : rien ne s'empile derrière un disque muet ;
- une requête attend le disque 5 s pour la liste, 120 s pour une archive,
  puis répond 504 `le disque des enregistrements ne repond pas`. Le fil
  qu'elle laisse garde sa place jusqu'à ce que le disque le rende, et rien
  d'autre : ni un fil du traitement ECG, ni la sortie du processus.

La sortie de la console n'emprunte aucun fil non plus : elle demande au fil du
journal de finir et regarde toutes les 50 ms, pendant 5 s au plus, s'il a
fini.

**Rien ne retient le processus une fois la console arrêtée.** Les fils
`record-io` et `record-journal` sont des démons, mais pas ceux du serveur web,
qui lisent et envoient ses fichiers (une archive, puis la suppression de son
fichier temporaire ; la page elle-même). Mesuré dans un processus à part
(`tests/test_exit_deadline.py`) : avec un de ces fils bloqué sur un disque qui
ne répond plus, le variateur est arrêté en 50 ms après le `SIGTERM`, puis le
processus ne sort pas (toujours là après 45 s, jusqu'à être tué ; sur le Pi,
systemd attendrait ses 90 s). Depuis, dès que la console a fini de s'arrêter
(le variateur d'abord, puis l'enregistrement), un fil démon donne 5 s au
processus pour sortir de lui-même (`EXIT_GRACE`), puis le dit
(`console exit forced … a thread did not come back`) et termine le processus
avec le code de sortie qu'il allait rendre. Une sortie ordinaire emporte ce
fil avec elle et rien n'est forcé. L'ordre ne change pas : cette échéance
n'est armée qu'après l'arrêt du variateur.

Seul un dossier d'enregistrement directement sous la racine peut être nommé.
Le nom reçu passe deux contrôles, chacun suffisant seul : il doit avoir la
forme d'un nom d'enregistrement, puis le chemin qu'il donne est normalisé,
refusé s'il n'est pas directement dans le dossier des enregistrements, et seul
ce chemin normalisé est utilisé (`locate`, `src/record/export.py`).
L'archive est un fichier temporaire à côté des enregistrements, supprimé après
l'envoi (ou dès qu'elle est prête, si sa requête n'attend plus) ; elle ne
porte ni le nom ni l'identifiant du compte sous lequel tourne la console.

### 15.7 Ce que l'enregistrement dit des personnes, et sa protection

- **Opérateur** : jamais le nom saisi. Le manifeste et les événements portent
  un alias, `op-` suivi de 16 chiffres hexadécimaux, dérivé du nom (le même
  nom donne le même alias sur toutes les séances). C'est un pseudonyme, pas un
  anonymat : qui a la liste des opérateurs peut le recalculer. Les noms saisis
  pendant la séance (y compris celui d'un opérateur du tableau de bord) et les
  adresses e-mail sont retirés du texte des événements (`[redacted]`), et ce
  texte est coupé à 500 caractères. Un motif tapé à la main reste du texte
  libre : ne pas y écrire le nom d'un passager.
- **Passager** : `subject_id` est l'identifiant transmis par le tableau de
  bord, ou `null` pour un départ tapé à la console. Le nom affiché du
  programme n'est pas écrit.
- **Au repos** : le dossier racine est en mode 700, et tout ce que la console
  y crée est privé dès sa création : chaque dossier en 700 (le dossier d'une
  séance, `ecg_raw/`, `logbook/`), chaque fichier en 600 (manifeste, flux,
  blocs bruts, sommes de contrôle, marqueur de dépôt, curseur de
  synchronisation et liste `.sync-baseline.json` posés à côté, journal hors
  séance, archive temporaire d'un export). Le mode est donné à l'appel qui
  crée le fichier : il n'y a pas d'instant où il est lisible par d'autres, et
  le `umask` du processus ne peut que restreindre. Lancée à la main, la
  console crée le tout sous le compte qui la lance. **Les enregistrements déjà
  sur le disque ne sont pas modifiés** : ils gardent leurs modes 755 / 644,
  sous la racine en 700 qui les protégeait déjà. La console ne les réécrit ni
  au démarrage ni plus tard (ce serait parcourir des milliers de fichiers de
  sa propre initiative) ; les resserrer est un geste manuel, machine au
  repos : `chmod -R go= <dossier racine>`. **Il n'y a pas de chiffrement au
  repos** ([menaces.md](menaces.md), MEN-18). C'est la règle d'attente du
  ticket ; le choix entre chiffrement et purge après dépôt dépend de l'avis
  HDS / RGPD (ANH-172).
- **Sur un Pi installé, la console tourne sous `root`**
  ([pi-image.md](pi-image.md#sous-quel-compte)). Le dossier racine,
  `/var/lib/anheart/records`, appartient au compte `anheart` (mode 700), mais
  tout ce que la console y crée appartient à `root` : chaque dossier de
  séance, ses fichiers, et le journal hors séance. Avec les modes 700 / 600,
  **ils ne sont donc lisibles que par `root`, et plus par le compte
  `anheart`**, qui les lisait tant qu'ils étaient en 755 / 644. Le compte
  `anheart` voit encore les noms des dossiers, pas leur contenu. Pour lire un
  enregistrement, un technicien a deux moyens :
  1. **par la console**, machine au repos, sans compte sur le Pi : le bouton
     « Exporter l'enregistrement » de la page Configuration pour le plus
     récent, ou `GET /api/records` puis `GET /api/records/{nom}/archive` avec
     le jeton de la console pour un autre (15.6). C'est le moyen à préférer :
     l'archive garde les modes privés, et rien n'est changé sur le Pi ;
  2. **sur le Pi, avec `sudo`** : `sudo ls /var/lib/anheart/records`, puis
     lire ou copier le dossier voulu sous `root`. C'est le seul moyen pour le
     journal hors séance, qui n'a pas de route d'export :
     `sudo less /var/lib/anheart/records/logbook/events.jsonl`.

  Ne pas changer le propriétaire ni les modes des dossiers pour les lire : un
  enregistrement contient l'ECG brut de la séance. Cela changera quand la
  console tournera sous le compte `anheart`, sans privilèges (ANH-151) : les
  nouveaux enregistrements appartiendront alors à ce compte.
- `config_hash` et `medical_parameters_version` sont des SHA-256 de la
  configuration appliquée (géométrie, plafonds, paliers cardiaques et autres
  limites de sécurité, limites de mouvement, sources) ; ni la clé machine ni
  le jeton de la console n'y entrent.

### 15.8 Ce qui n'est pas enregistré aujourd'hui

- Les échanges avec le variateur d'une console au repos, et ceux d'un
  armement qui précèdent de plus d'un cycle (0,2 s) l'ouverture de
  l'enregistrement : `drive_frames.jsonl` commence avec l'enregistrement.
- La géométrie détaillée (`geometry: null`), la vérité physiologique
  (`hr_true` vide), la présence de couple (`energised: null`).
- Les contrôles pré-vol (`preflight: null`).
- Hors séance, les changements d'état que personne n'a demandés (un verdict
  ou un défaut variateur qui apparaît ou disparaît console au repos) : le
  journal hors séance (15.9) ne garde que les événements de la console.

### 15.9 Le journal hors séance

Un enregistrement fermé ne change plus : ses sommes de contrôle indexent
chaque fichier, un dépôt y est lié, et le reader signale toute modification.
Ce qui se passe à la console **sans séance enregistrée** n'a donc pas sa place
dans un enregistrement, et n'était écrit nulle part. C'est maintenant une
ligne de `<dossier racine>/logbook/events.jsonl` (`src/record/logbook.py`) :

```json
{"at":"2026-10-08T10:11:12.345Z","monotonic":1234.5,"kind":"refusal","detail":"refused: demarrage refuse : ...","actor":"op-0123456789abcdef"}
```

| Événement de la console | `kind` | `detail` |
|---|---|---|
| départ demandé | `operator_action` (`remote_command` depuis le tableau de bord) | `start_requested`, sans ses mots : ils nomment un programme par son identifiant |
| départ, consigne ou réarmement refusé | `refusal` | `refused: ` puis la phrase montrée à l'opérateur, sans l'identifiant du programme ni l'âge du passager qu'elle cite (`[redacted]`) |
| acquittement | `verdict_ack` | `acknowledged: …` |
| réarmement du défaut variateur demandé | `operator_action` | `fault_reset_requested: reset defaut` |
| attestation, E-STOP, fin demandée | `operator_action` | comme dans un enregistrement |
| nouvelle du lien avec le tableau de bord | `warning` | `dashboard: …` |

**Ce que ce journal peut contenir, et ce qu'on en fait.** Il n'a ni ECG, ni
identifiant de passager, ni nom. Il garde, avec leur heure : l'alias de
l'opérateur de chaque geste (le même pseudonyme stable que dans un
enregistrement, 15.7) ; le nom des verdicts acquittés ; le texte de
l'attestation ; un motif tapé à la main pour une fin ou un E-STOP ; les
nouvelles du lien avec le tableau de bord ; et la phrase de chaque refus. De
cette phrase, l'identifiant du programme et les mots qui donnent l'âge du
passager (`passager de 9 ans`) sont retirés avant l'écriture : l'opérateur les
voit à l'écran, le disque ne les reçoit pas. Elle peut encore citer le détail
d'un verdict resté en vigueur, et donc, pour un verdict cardiaque, une
fréquence cardiaque de la séance précédente. Ce journal est borné à 2 Mo (deux
fichiers de 1 Mo, les lignes les plus anciennes disparaissent d'elles-mêmes),
la rétention ne le purge jamais, il n'est pas chiffré, et **il n'est pas
exporté** : ni la liste, ni l'archive, ni le bouton de la console ne le
donnent. Il se lit sur le disque, sous `root` sur un Pi installé (15.7), et se
traite comme les enregistrements.

`kind`, `detail` et `actor` suivent les règles d'un événement d'enregistrement
(15.7) : l'opérateur est un alias, jamais un nom ; les noms saisis, les
adresses e-mail, l'identifiant du programme demandé et l'âge du passager sont
retirés du texte (`[redacted]`). Il n'y a pas de séance, donc pas d'axe de temps de séance :
`at` est l'heure UTC, `monotonic` l'horloge de la console en secondes, qui
ordonne les lignes d'un même lancement de la console à travers un saut de
l'heure. Pendant une séance, ces mêmes événements vont dans l'enregistrement
de la séance, et pas ici.

- **Écrit hors de la boucle**, comme le reste : la boucle remet la ligne à la
  file du journal (`Journal.log`), bornée par la même limite de 4096 valeurs ;
  une ligne refusée faute de place est comptée avec les valeurs perdues
  (`queue_full`), et le journal l'écrit avant sa ligne suivante
  (`logbook: dropped=<n>`). `fsync` toutes les 2 s au plus, seulement s'il y a
  eu une ligne.
- **Borné sur le disque** : quand la ligne suivante ferait passer
  `events.jsonl` au-delà de 1 Mo, il devient `events.1.jsonl` (qui remplace le
  précédent) et un nouveau fichier commence. Deux fichiers, 1 Mo chacun au
  plus, les lignes les plus récentes toujours gardées. La rétention (15.5) n'y
  touche pas.
- **Pas un enregistrement** : le nom `logbook` n'a ni la forme d'un dossier
  d'enregistrement, ni celle d'un marqueur posé à côté d'un dossier
  (`<dossier>.deposit.json`), ni celle d'un curseur de synchronisation
  (`<dossier>.sync.json`). La liste, l'export et la purge ne le voient pas, et
  la synchronisation avec le tableau de bord non plus : elle ne le liste pas,
  ne l'ouvre pas et n'en envoie rien
  ([8.1](#81-ce-qui-est-envoyé-vient-du-disque)).
  Il n'a pas de route d'export : il se lit sur le disque.

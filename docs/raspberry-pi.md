# Le Raspberry Pi : la console locale et la chaîne de sécurité

Ce document décrit le code Python de `raspberry-pi/` : ce que fait chaque
module, la boucle de contrôle, le superviseur de sécurité et toutes ses règles,
la gestion des défauts du variateur, la synchronisation avec le tableau de bord,
les capteurs, la caméra, la configuration et le contrat de code.

Tout ce qui suit est tiré du code. Les termes techniques (LFT, ETA, LFRD, ttO,
STO, CiA402, latch…) sont définis dans le [glossaire](glossaire.md). L'usage de
la page web est décrit dans [console-locale.md](console-locale.md), la synthèse
des garanties dans [securite.md](securite.md), le côté Convex dans
[convex.md](convex.md) et les tests dans [framework-de-test.md](framework-de-test.md).

> **État réel, à lire d'abord.** Tout ce qui est décrit ici a été exercé en
> simulation (variateur simulé, BITalino simulé, physiologie simulée) et par les
> tests automatiques. Le variateur réel a été **lu** au banc (adresse Modbus 248,
> décalage de registres 0, rapport 49,79 compté à la main). **Aucune séance avec
> une personne à bord n'a eu lieu.** Le `Dockerfile`, `docker-compose.yml` et
> le service systemd (`scripts/anheart.service`) lancent la console
> `src.local_panel` ; l'image a été essayée en simulation dans un conteneur,
> **jamais sur un vrai Pi**. Procédure : [deploiement.md](deploiement.md#7-le-raspberry-pi).

## Sommaire

1. [Deux programmes dans le même dossier](#1-deux-programmes-dans-le-même-dossier)
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

---

## 1. Deux programmes dans le même dossier

| Commande | Programme | Rôle | État |
|---|---|---|---|
| `python -m src.local_panel` | la **console locale** | pilote le variateur, lit le BITalino, sert la page web de l'opérateur, applique la sécurité, se synchronise avec Convex | entrée du `Dockerfile` et de `scripts/anheart.service`, également lançable à la main |
| `python -m src.main` | l'**ancien enregistreur ECG** | lit le BITalino et envoie l'ECG à Convex ; ne touche jamais le moteur | entrée distincte, lancée explicitement |

Les deux lisent `raspberry-pi/.env`, mais pas les mêmes clés (voir
[section 12](#12-la-configuration-env)). La console charge le fichier
`raspberry-pi/.env` quel que soit le dossier courant, puis les variables du
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
| `src/config.py` | Configuration de l'ancien enregistreur `src.main`. | Exige `CONVEX_URL`, `MACHINE_API_KEY`, `BITALINO_MAC`. Hors vérification stricte des types (voir [13](#13-le-contrat-de-code-strict-et-la-gate)). |

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
| `src/signal_processing.py` | L'ancien traitement ECG (BioSPPy) qui calcule la FC de régulation. | **Pas encore** sous la vérification stricte des types ni sous la porte 100 % (listé dans `coverage_pending`). |
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
| `training/safety.py` | Le superviseur de sécurité (`SafetySupervisor`) : 17 règles indépendantes. | Il ne voit jamais la demande de la loi de commande ; son verdict l'emporte toujours (voir [section 5](#5-le-superviseur-de-sécurité)). Porte 100 %. |
| `training/runtime.py` | `TrainingRuntime` : le tic, l'armement, les arrêts, l'acquittement, le réarmement de défaut. | Aucun chemin ne laisse le moteur commandé ; aucune reprise automatique ; aucun réarmement automatique. Porte 100 %. |

### 2.5 La console et ses liens

| Module | Rôle | Garanties |
|---|---|---|
| `src/local_panel.py` | La racine de composition : construit et relie runtime, variateur, BITalino, capteurs, caméra, page web et lien Convex sur **une** boucle asyncio. | À la sortie, `needs_stop_before_release` distingue le repos confirmé ou la liaison non acquise (libération sans écriture) d'une inspection acquise mais non confirmée ou d'un runtime sorti de IDLE (passage par `shutdown()`). Porte 100 %. |
| `src/control_surface.py` | La boîte aux lettres entre la page web et la boucle : **un seul** ordre à la fois, plus le dernier instantané de télémétrie. | Aucun `await` (vérifié par test) ; l'E-STOP ne passe pas par la boîte aux lettres, il verrouille le superviseur tout de suite. |
| `src/cloud_sync.py` | Le lien avec le tableau de bord Convex. | Ne peut pas arrêter la machine en tombant en panne ; ne peut pas lancer de séance manuelle. Porte 100 %. |
| `src/telemetry.py` | Diffusion de la télémétrie vers les navigateurs connectés. | Le nombre de clients ne ralentit pas la boucle. |
| `src/panel_status.py` | Ce que la page montre des liaisons (variateur, BITalino). | Porte 100 %. |
| `src/presence/*` | Sûreté par caméra (seule une caméra **simulée** existe). | Voir [section 10](#10-la-caméra-présence). Porte 100 %. |
| `src/web/*` | L'API HTTP, la WebSocket, la page (`static/index.html`, `app.js`). | Tous les gestionnaires sont des coroutines ; hors boucle locale, un jeton de 16 caractères minimum est exigé. Détails : [console-locale.md](console-locale.md). |
| `src/sim/*` | Le sujet simulé (`physiology.py`, FC pilotée par le g réel), l'ECG synthétique (`ecg.py`, comptes ADC bruts), le BITalino simulé (`bitalino.py`) et les autres voies (`sim/signals/*`). | Le vrai traitement ECG travaille sur les comptes simulés. Porte 100 %. |

### 2.6 L'ancien enregistreur (hors console)

`src/main.py`, `src/session_manager.py`, `src/convex_client.py`,
`src/data_buffer.py` : l'enregistreur ECG d'origine (tampon SQLite hors ligne,
envoi vers Convex). Il ne pilote pas le moteur. Ces fichiers sont exclus des deux
vérificateurs de types (liste figée par `tests/test_typing_contract.py`).
`pyproject.toml` signale un vrai défaut dans `main.py` (RUF006 : une tâche créée
depuis un gestionnaire de signal sans garder la référence).

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
| `cloud_step` | 1 s | une étape du lien Convex |
| serveur web | - | uvicorn, sur la même boucle |

Quand une tâche se termine (signal SIGINT/SIGTERM, exception, serveur web qui
ne peut pas s'attacher au port), toutes s'arrêtent et le variateur est relâché.
Code de sortie : 0 (normal), 1 (une tâche a échoué), 2 (configuration refusée).

`cloud_step` est la seule tâche qui avale ses exceptions : le tableau de bord
est un observateur, un bogue de report ne doit pas arrêter une séance.

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
5. L'instantané est publié vers la page.

### 3.4 Un tic du runtime (`TrainingRuntime._tick`), dans cet ordre

0. **Au repos seulement** : lecture du variateur (voir 3.2).
1. **Le keepalive d'abord** : écriture de la consigne déjà en vigueur (LFRD),
   puis une lecture d'état (ETA, RFRD, LCR, LFT). Si la boucle cale, les
   écritures cessent et le ttO du variateur l'arrête seul.
2. Deuxième mot d'un réarmement de défaut en cours (`SHUTDOWN` 0,2 s après
   `FAULT_RESET`).
3. Calcul de la phase de la séance.
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
| `FREEZE` | 1 | Garde la dernière consigne ; la loi de commande n'est pas consultée. |
| `REDUCE` | 2 | Descend la consigne (programme : 15 tr/min moteur/s ; manuel : aux limites de mouvement) et continue de réguler sans jamais monter. |
| `RAMP_DOWN` | 3 | Fin de séance : phase COOLDOWN, consigne vers 0 par la rampe logicielle (programme 15 tr/min/s, manuel aux limites de mouvement). |
| `QUICK_STOP` | 4 | Consigne à 0 **tout de suite**, commande de marche **gardée** : le variateur décélère sur sa propre rampe mise en service (3-4 s). Ce n'est pas un arrêt « immédiat ». |
| `GO_SILENT` | 5 | Consigne à 0 une dernière fois (synchrone), puis **plus aucune trame**, lectures comprises. Le ttO du variateur prend l'arrêt en charge. Sans retour : ni acquittement, ni nouvel armement ; il faut redémarrer le processus. |

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

Dans l'ordre, rien ne touche le variateur avant les quatre premières :

1. Le runtime n'est ni arrêté (`shutdown`) ni silencieux.
2. **Attestation E-STOP** faite depuis ce démarrage du processus (texte exact :
   « a latching mushroom emergency stop is wired normally-closed into P24 -> STO
   and the STO jumper has been removed »), par un opérateur nommé.
3. Aucun verdict en vigueur (sinon il faut acquitter).
4. Runtime au repos (`IDLE` ou `FINISHED`).
5. Ouverture de la liaison et lecture du variateur. S'il est en
   `OPERATION_ENABLED` : arrêt, verrou `drive_precommanded`, refus. S'il est en
   défaut : refus (`DriveInFault`), **sans réarmement automatique**.
6. Relecture de tFr, HSP, LSP, ACC, dEC ; refus si LSP ≠ 0, HSP > tFr ou
   HSP > 50,0 Hz. ttO et SLL ne sont **pas** relus (adresses Modbus non
   vérifiées) : chaque armement écrit un avertissement dans le journal demandant
   de les vérifier au clavier.
7. Mise sous tension de l'étage de sortie : LFRD = 0, puis `SHUTDOWN` →
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
* La consigne marche vers la cible au rythme du profileur (0,25 tr/min de
  sortie/s = 12,4 tr/min moteur/s, et 0,03 g/s), montée comme descente.
* Phase HOLD pendant toute la séance ; au bout de 3600 s
  (`MANUAL_SESSION_LIMIT`), elle s'arrête seule comme un STOP.
* Après tout arrêt la cible vaut 0 : bouger à nouveau exige un nouveau départ.

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
| `HOLD` | La loi de commande module la vitesse pour tenir la FC dans la zone. |
| `COOLDOWN` | Consigne vers 0 (fin normale ou verdict). Se termine sur arrêt confirmé, ou au bout de la durée de cooldown du profil. |
| `RECOVERY` | Moteur arrêté, passager toujours à bord, **toutes les règles de FC restent actives** (phase au plus fort risque vasovagal). |
| `DONE` | Programme terminé. Seules les règles de FC et `attendant_absent` s'arrêtent. |

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
  `commanded_rpm`, ce qui a été écrit au variateur). Raison : lors d'un malaise
  vasovagal la FC **baisse** ; la loi de commande y lit « sous la zone » et veut
  accélérer.
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
    par réflexe, y compris sur un défaut variateur.
* **Acquittement** (`acknowledge`) : exige un nom ; refusé si rien n'est
  verrouillé ; **refusé pour GO_SILENT** (définitif) ; refusé tant que
  l'opérateur n'a pas déclaré le champignon d'arrêt d'urgence relâché
  (`estop_released=True`) si un E-STOP est verrouillé. Acquitter pendant que la
  cause est encore là est permis : la règle se redéclenche au tic suivant.
* Les règles de FC (`hr_*`) ne jugent pas en phase `DONE` ni en occupation
  `bench`.

### 5.2 Les 17 règles du superviseur

Valeurs par défaut de `SafetyLimits`. Seuls `hard_max_bpm` et `critical_bpm`
n'ont pas de défaut dans la classe ; la console les prend de `HR_HARD_MAX_BPM` /
`HR_CRITICAL_BPM` (148 / 158 par défaut).

| Id | Condition | Seuils par défaut | Action | Verrou |
|---|---|---|---|---|
| `operator_estop` | E-STOP de la page (ou autre appel à `latch_estop`) | aucun délai | QUICK_STOP | oui ; l'acquittement exige « champignon relâché » |
| `drive_fault` | l'état CiA402 lu est FAULT | aucun délai | RAMP_DOWN (le variateur a déjà appliqué sa propre réaction) | oui ; se redéclenche tant que le défaut est affiché |
| `comms_lost` | échanges ratés d'affilée ≥ seuil | 3 échanges | GO_SILENT | oui, définitif |
| `hr_drop` | chute confirmée de FC que la baisse de charge n'explique pas (détail ci-dessous) | fenêtre 30 s ; 25 bpm ; médianes de 5 (niveau) et 9 (pic) ; 4 lectures consécutives ; marge repos 15 bpm ; fenêtre de charge 120 s | RAMP_DOWN | oui |
| `hr_hard_max` | FC > palier dur pendant 5 s continues (bande de relâche 5 bpm) | 148 bpm, 5 s | RAMP_DOWN | oui |
| `hr_critical` | FC ≥ palier critique | 158 bpm, aucun délai | QUICK_STOP | oui |
| `hr_rate` | pente (moindres carrés) des médianes glissantes de 5 lectures sur 60 s, étendue ≥ 20 s, > limite | 25 bpm/min ; relâche sous 15 bpm/min | REDUCE | non |
| `hr_stale` | aucune FC fraîche et fiable depuis… | > 10 s FREEZE ; > 30 s REDUCE ; > 60 s RAMP_DOWN | FREEZE → REDUCE → RAMP_DOWN | seulement le niveau RAMP_DOWN |
| `hr_unresponsive` | sur 300 s (étendue ≥ 240 s, ≥ 10 points par moitié) la charge moyenne monte d'au moins 0,08 g, la seconde moitié est à ≥ 0,15 g, et la FC moyenne monte de moins de 3 bpm | 300 s, 0,08 g, 0,15 g, 3 bpm | REDUCE | non |
| `current_high` | courant moteur (LCR) > seuil d'alerte pendant 10 s (relâche 0,2 A sous le seuil) ; ou > seuil de coupure | alerte 2,4 A / 10 s ; coupure 3,2 A immédiat (plaque 2,15 A) | REDUCE (alerte) ; RAMP_DOWN (coupure) | alerte non ; coupure oui |
| `no_load` | consigne ≥ 100 tr/min, sortie active, courant < 0,2 A pendant 3 s (phase ouverte, pas de moteur, mauvais registre) | 0,2 A, 100 tr/min, 3 s | RAMP_DOWN | oui |
| `tracking_error` | vitesse mesurée hors de l'enveloppe de plus de 60 tr/min moteur pendant 2 s, sortie active | 60 tr/min, 2 s | RAMP_DOWN ; **GO_SILENT** si l'écho LFRD est aussi faux depuis 1 s | oui |
| `reverse_rotation` | vitesse mesurée de signe opposé à la consigne et > 10 tr/min | 10 tr/min, aucun délai | QUICK_STOP | oui |
| `session_overrun` | durée écoulée > durée du programme + 30 s | 30 s | RAMP_DOWN | oui |
| `loop_stall` | écart entre deux tics > 3 périodes ; > 15 périodes | 0,6 s → FREEZE ; 3,0 s → GO_SILENT | FREEZE ou GO_SILENT | oui (les deux) |
| `attendant_absent` | aucun signe de présence de la page depuis… (mesuré depuis le départ s'il n'y en a jamais eu) | > 60 s FREEZE ; > 120 s RAMP_DOWN | FREEZE → RAMP_DOWN | seulement RAMP_DOWN |
| `setpoint_unconfirmed` | LFRD relu ≠ LFRD écrit pendant 1 s | 1 s | RAMP_DOWN | oui |

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
| STOP opérateur, RAMP_DOWN | consigne descendue par le logiciel (programme 15 tr/min moteur/s ; manuel 12,4 tr/min moteur/s et 0,03 g/s). |
| GO_SILENT, boucle bloquée, processus tué | plus de trame : le **ttO** du variateur expire et le variateur applique sa réaction de perte de communication (SLF). Le simulateur suppose ttO = 3 s et une rampe d'arrêt. **Sur le vrai variateur, ttO et SLL ne sont pas relus** (adresses non vérifiées) : ils doivent être contrôlés au clavier avant chaque séance. Si SLL était réglé sur « roue libre » ou « ignorer », une liaison morte laisserait le moteur commandé ou en roue libre. |
| Sortie normale du processus (`shutdown`) | LFRD = 0 synchrone, puis `close()` qui attend l'arrêt mesuré avant de retirer la marche. Si l'arrêt n'est pas confirmé, la marche reste et le ttO finit l'arrêt. |
| Console sans séance, au repos confirmé ou liaison non acquise | libération du transport sans écriture ; l'absence de séance ne prouve pas l'arrêt physique. |
| Console sans séance, liaison acquise mais état non confirmé | `needs_stop_before_release` impose le passage par `shutdown()` ; un état inconnu n'est pas assimilé à un arbre arrêté. |

Ces distinctions sont exercées par `tests/test_initial_inspection_cancellation.py`
et `tests/test_acquisition_evidence.py`, notamment le cas acquis mais illisible
`test_cancelled_unreadable_acquired_drive_is_stopped_without_resumption`.

HSP (vitesse haute du variateur) est la limite qui tient quand le logiciel se
trompe. Le code accepte HSP ≤ 50,0 Hz parce que le moteur est **désaccouplé** au
banc ; le code dit explicitement que cette limite **doit être abaissée avant
d'accoupler le bras** (clavier d'abord, puis le code).

---

## 8. La synchronisation avec le tableau de bord

`src/cloud_sync.py`. Actif seulement si `MACHINE_API_KEY` est renseignée (et
`CONVEX_URL` en `https://…convex.site`, ou `http://localhost` /
`http://127.0.0.1`). Sans clé, la console fonctionne exactement pareil, sans
tableau de bord.

| Sens | Quoi | Route Convex | Cadence |
|---|---|---|---|
| Pi → Convex | battement de cœur avec l'état (mode, phase, FC, vitesses, g, action de sécurité) | `POST /api/machine/heartbeat` | toutes les 10 s (seuil hors ligne du tableau de bord : 90 s) |
| Pi → Convex | profils dont les paliers cardiaques égalent ceux du superviseur | `POST /api/machine/profiles` | quand la révision du magasin change |
| Pi → Convex | toute séance lancée ici (MANUEL ou AUTO), sous une référence locale (pas de doublon après une coupure) | `POST /api/machine/training/local` | au départ |
| Pi → Convex | confirmation du départ d'une séance lancée à distance | `POST /api/machine/training/start` | au départ |
| Pi → Convex | télémétrie échantillonnée à 1 Hz, envoyée par lots | `POST /api/machine/training/telemetry` | toutes les 5 s (lots de 300 points max) |
| Pi → Convex | fin de séance avec la raison du runtime | `POST /api/machine/training/end` | à la fin |
| Convex → Pi | un lancement AUTO : profil, passager, FC max, âge (déduit de l'année de naissance) | `GET /api/machine/training/poll` | toutes les 3 s au repos |
| Convex → Pi | une demande d'arrêt, exécutée comme un STOP ordinaire | `GET /api/machine/training/status` | toutes les 3 s en séance |

Règles :

* **Rien d'autre ne descend.** Une séance **manuelle ne peut jamais être lancée
  à distance** : le lien ne sait pas en construire une.
* Un lancement distant passe par la même boîte aux lettres qu'un départ tapé à
  la console, donc par les mêmes portes (attestation, pas de verdict, console
  au repos), puis par celles de la séance programmée (4.4). Tout refus revient
  comme séance échouée avec la raison. Un lancement ni démarré ni refusé après
  60 s est déclaré échoué.
* Chaque appel renvoie un `Result` ; un réseau mort ne fait que rendre l'image
  du tableau de bord périmée. Nouvel essai après 15 s. Jusqu'à 3600 points de
  télémétrie (une heure) sont gardés, les plus vieux sont jetés d'abord ; 20
  fins de séance au plus restent en attente.
* Un arrêt venu du tableau de bord est attribué à « tableau de bord ».

Non vérifié : aucun déploiement Convex réel n'a été contacté ; les tests
utilisent un transport factice (voir [convex.md](convex.md)).

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

### 12.3 Clés de l'ancien enregistreur (`src/config.py`, pour `src.main`)

| Clé | Défaut | Contrainte |
|---|---|---|
| `CONVEX_URL` | obligatoire | - |
| `MACHINE_API_KEY` | obligatoire | - |
| `BITALINO_MAC` | obligatoire | - |
| `SAMPLE_RATE` | `1000` | 1, 10, 100 ou 1000 |
| `OUTPUT_SAMPLE_RATE` | `250` | > 0 et ≤ `SAMPLE_RATE` |
| `BATCH_INTERVAL_MS` | `1000` | > 0 |
| `HEARTBEAT_INTERVAL_S` | `30` | > 0 |
| `LOG_LEVEL` | `INFO` | - |
| `BUFFER_DB_PATH` | `buffer.db` | - |

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
   sécurité ; chaque `match` se termine par `assert_never`, si bien qu'une
   nouvelle variante d'erreur casse la vérification de types partout où elle
   n'est pas traitée.
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
   supposer l'état du variateur au démarrage ; aucun réarmement ni reprise
   automatique ; rien ne bloque la boucle.

Exceptions en cours (dans `pyproject.toml`, liste figée par
`tests/test_typing_contract.py`) : `signal_processing.py`, `convex_client.py`,
`data_buffer.py`, `session_manager.py`, `main.py`, trois fichiers de test et
`scripts/` sont hors vérification de types ; `signal_processing.py` est aussi
hors de la porte de couverture (`coverage_pending`), alors qu'il calcule la FC de
régulation.

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

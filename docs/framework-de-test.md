# Framework de test et de simulation

Ce document explique comment le logiciel Anheart est testé, et comment **vous**
lancez, lisez et étendez ces tests. Les termes techniques (LFT, ttO, verdict,
xfail…) sont définis dans le [glossaire](glossaire.md).

> **À retenir.** Tout ce qui est décrit ici tourne **en simulation**. Aucun de
> ces tests ne touche le vrai variateur, le vrai BITalino ni une vraie personne.
> Un test qui passe prouve que le code se comporte comme prévu **face aux
> modèles** (variateur simulé, cœur simulé, ECG simulé). Il ne prouve pas que la
> machine réelle se comporte pareil. Voir [securite.md](securite.md).

## Sommaire

1. [Deux suites de tests](#1-deux-suites-de-tests)
2. [Prérequis communs](#2-prérequis-communs)
3. [Les tests du Raspberry Pi](#3-les-tests-du-raspberry-pi)
4. [Les tests de la simulation](#4-les-tests-de-la-simulation)
5. [Lancer un scénario : `simulation.run`](#5-lancer-un-scénario--simulationrun)
6. [Verdicts instantanés : `simulation.quick`](#6-verdicts-instantanés--simulationquick)
7. [Le visualiseur 2D : `simulation.live`](#7-le-visualiseur-2d--simulationlive)
8. [Écrire un nouveau scénario JSON](#8-écrire-un-nouveau-scénario-json)
9. [La cohorte de 30 personnes](#9-la-cohorte-de-30-personnes)
10. [La matrice de 199 pannes](#10-la-matrice-de-199-pannes)
11. [Les invariants vérifiés sur chaque trace](#11-les-invariants-vérifiés-sur-chaque-trace)
12. [Les « xfail strict » et le cas résiduel S07](#12-les--xfail-strict--et-le-cas-résiduel-s07)
13. [La géométrie extraite de la CAO](#13-la-géométrie-extraite-de-la-cao)
14. [Pièges connus](#14-pièges-connus)
15. [CI](#15-ci)

---

## 1. Deux suites de tests

| Suite | Dossier | Ce qu'elle teste | Nombre de tests (collectés) |
|---|---|---|---|
| Tests du Pi | `raspberry-pi/tests/` | chaque module de `raspberry-pi/src` isolément, plus la console complète (`build_panel`) pilotée par son API HTTP | **3208** |
| Tests de la simulation | `simulation/tests/` | le **vrai** runtime de `raspberry-pi/src` en boucle fermée contre le variateur simulé, la physiologie simulée et l'ECG simulé : scénarios, cohorte, matrice de pannes | **987** |

Les nombres viennent de `pytest --co -q` (voir plus bas). Ils changent à chaque
ajout de test.

La simulation **ne réimplémente pas** la machine. Elle prend dans
`raspberry-pi/src` :

| Rôle | Code de production utilisé |
|---|---|
| loi de commande, superviseur de sécurité, profileur de mouvement, toutes les sorties | `training/runtime.py` (`TrainingRuntime`) |
| le variateur ATV320 (CiA402, rampes, roue libre, chien de garde ttO) | `motor/simulated.py` (`SimulatedDrive`) |
| le cœur du passager (réponse au g, retard, dérive, événements scriptés) | `sim/physiology.py` (`Physiology`) |
| ECG, BITalino, traitement du signal (mode `dsp`) | `sim/bitalino.py`, `sim/ecg.py`, `signal_processing.py` via `ecg_pipeline.EcgBridge` |
| géométrie et conversions d'unités | `geometry.py`, `units.py` |
| programmes, profils, limites anti-nausée | `training/plan.py`, `config/profiles.default.json`, `config/motion_limits.json` |

La simulation ajoute seulement : la boucle, les actions de scénario, un
enregistreur de toutes les trames envoyées au variateur, un modèle rapide de
capteur de fréquence cardiaque, le vérificateur d'invariants et le
visualiseur. Le détail des modules du Pi est dans [raspberry-pi.md](raspberry-pi.md).

Le dossier `simulation/` est versionné ; ses sorties (`simulation/out/`) ne le sont pas.

## 2. Prérequis communs

Les deux suites utilisent le **même** environnement Python :
`raspberry-pi/.venv` (Python 3.12). La création du venv est décrite dans
[demarrage-rapide.md](demarrage-rapide.md).

Pour la simulation, lancez tout **depuis la racine du dépôt**, avec la racine
**et** `raspberry-pi/` dans le chemin d'import :

```sh
cd /chemin/vers/Anheart
export PYTHONPATH=.:raspberry-pi
PY=raspberry-pi/.venv/bin/python
```

Sans ce `PYTHONPATH`, `import simulation` ou `import src` échoue.

## 3. Les tests du Raspberry Pi

### 3.1 Organisation

Un fichier de test par module, plus des fichiers « de bout en bout ». Les plus
gros (nombre de tests collectés) :

| Fichier | Tests | Ce qu'il couvre |
|---|---|---|
| `test_runtime.py`, `test_runtime_manual.py` | 265 + 43 | le runtime de séance (AUTO et MANUEL), toutes les sorties |
| `test_hr_control.py` | 246 | la loi de commande FC → vitesse |
| `test_plan.py` | 244 | profils, programmes, validation pour un passager |
| `test_training_types.py` | 238 | types du domaine (verdicts, qualités de signal…) |
| `test_safety.py` | 182 | le superviseur de sécurité, règle par règle |
| `test_web_api.py`, `test_web_panel.py` | 142 + 11 | les routes HTTP de la console locale |
| `test_atv320.py`, `test_drive_contract.py`, `test_ftdi_link.py` | 139 + 79 + 65 | le pilote Modbus ATV320 et la liaison FTDI |
| `test_simulated_drive.py`, `test_sim.py` | 97 + 114 | les simulateurs (variateur, BITalino, physiologie) |
| `test_drive_faults_complete.py` | 78 | **chacun** des 66 codes LFT injecté sur la vraie console |
| `test_failure_drive.py`, `_ecg`, `_process`, `_operator`, `_rig` | 84 + 14 + 11 + 16 + 1 | pannes injectées sur la vraie racine de composition (`build_panel`) |
| `test_cloud_sync.py` | 83 | le lien avec Convex (lancement, arrêt, réseau mort) |
| `test_sensor_*.py` | 35 à 57 chacun | un fichier par capteur (ECG, EDA, SpO2, RESP, EMG, LUX) |
| `test_presence_*.py`, `test_panel_presence.py` | 147 au total | la caméra / présence opérateur |
| `test_local_panel.py`, `test_local_panel_e2e.py` | 60 + 6 | la console assemblée |
| `test_typing_contract.py` | 8 | le contrat de typage lui-même (voir 3.4) |

Autres éléments :

* `tests/conftest.py` : une fixture `clean_env` automatique vide les variables
  d'environnement lues par le client et remet le singleton de configuration à
  zéro autour de **chaque** test. Un `.env` réel sur votre disque ne peut donc
  pas fuiter dans les tests.
* **hypothesis** (tests par propriétés) est utilisé dans 24 fichiers de tests du Pi.
* Marqueurs pytest : `hardware` (exclu par défaut via `-m 'not hardware'` dans
  `pyproject.toml`) et `slow`. À ce jour **aucun test n'est marqué
  `hardware`** : aucun test automatique ne parle à du matériel réel. Un seul
  est marqué `slow` : l'endurance de l'enregistrement de séance
  (`tests/test_record_endurance.py`, ANH-128), sautée tant que
  `ANHEART_ENDURANCE_HOURS` n'est pas définie. La même boucle tourne quelques
  minutes simulées dans la gate, à chaque run ; la journée simulée entière est
  l'étape nocturne de la CI ([section 15](#15-ci)).
* `tests/typing_fixtures/` contient des modules volontairement faux, sur
  lesquels `test_typing_contract.py` lance les vérificateurs de types pour
  prouver qu'ils détectent bien l'erreur.

### 3.2 Compter les tests

```sh
cd raspberry-pi
.venv/bin/python -m pytest --co -q | tail -1
# 3208 tests collected (instantané du 3 octobre 2026)
```

### 3.3 Lancer un seul fichier, un seul test

```sh
cd raspberry-pi
.venv/bin/python -m pytest tests/test_safety.py                 # un fichier
.venv/bin/python -m pytest tests/test_safety.py -k hr_drop      # les tests dont le nom contient hr_drop
.venv/bin/python -m pytest "tests/test_safety.py::test_nom_exact"   # un test précis
.venv/bin/python -m pytest tests/test_failure_*.py tests/test_local_panel_e2e.py tests/test_cloud_sync.py -q
```

`pyproject.toml` ajoute `-v` par défaut ; ajoutez `-q` pour une sortie courte.

### 3.4 La « gate » du Pi

La gate est le contrôle complet exigé avant toute fusion
(`raspberry-pi/scripts/check.sh`, décrit dans le skill
`.claude/skills/anheart-strict-python/SKILL.md`). Elle enchaîne, **sans
s'arrêter à la première erreur** :

| Étape | Commande |
|---|---|
| lint | `ruff check .` |
| format | `ruff format --check .` |
| types (principal) | `basedpyright` en mode strict, zéro `Any` |
| types (second avis) | `mypy .` en mode strict |
| tests + couverture | `pytest --cov --cov-branch --cov-fail-under=100` |

Elle affiche `GATE PASSED` ou `GATE FAILED: <étapes>`.

```sh
cd raspberry-pi
bash scripts/check.sh          # macOS / Linux
# .\scripts\check.ps1          # Windows
```

Le script a le bit exécutable ; `bash scripts/check.sh` reste utilisable sur
les systèmes qui ne préservent pas ce bit.

**Ce que couvre le 100 % de branches.** Le seuil de 100 % ne s'applique qu'à
la **chaîne de sécurité**, listée dans `[tool.coverage.report] include` de
`raspberry-pi/pyproject.toml` : `units`, `result`, `clock`, `motor/*`,
`training/*`, `sim/*`, `bitalino_client`, `geometry`, `ecg_pipeline`,
`local_config`, `bitalino_rfcomm_macos`, `local_panel`, `panel_status`,
`cloud_sync`, `dsp`, `sensors/*`, `presence/*`, `panel_lifecycle`,
`task_completion` et `web/profile_writer`. Le reste du code web est mesuré mais
ne bloque pas.

**Dette déclarée.** `src/signal_processing.py` fait partie de la chaîne de
sécurité (la fréquence cardiaque qui pilote le moteur le traverse) mais n'est
**pas encore** sous le seuil de 100 % ni sous les vérificateurs de types. Il est
listé dans `[tool.anheart] coverage_pending` ; un test échoue si cette liste
grandit.

Mesure réelle (Mac Apple silicon, 1er octobre 2026) : `GATE PASSED`, 3187
tests passés, 100 % de branches sur la chaîne de sécurité (10386 instructions,
2350 branches), **environ 22 minutes** (1307 s, dont 1296 s de tests).

## 4. Les tests de la simulation

### 4.1 Organisation

| Fichier | Tests | Ce qu'il vérifie |
|---|---|---|
| `test_battery.py` | 182 | chaque scénario de `scenarios/` : invariants, attentes, chemin de sortie, aller-retour JSONL |
| `test_cohort.py` | 202 | les 30 personnes × 3 séances, la reproductibilité du fichier de cohorte, la barrière d'âge et de FC max |
| `test_failures.py` | 405 | les 199 cas de la matrice de pannes, plus « aucun cas ne laisse du couple ou un arbre qui tourne » |
| `test_scenario.py` | 49 | le parseur de scénarios (refus des clés inconnues, des valeurs invalides) |
| `test_limits.py` | 36 | g à 27 et 32 tr/min, limites anti-nausée, profils livrés |
| `test_invariants.py` | 29 | le vérificateur lui-même : chaque invariant doit se déclencher sur une trace corrompue exprès |
| `test_cli_and_live.py` | 19 | les CLI `run`, et le serveur `live` |
| `test_fault_pieces.py` | 18 | les pièces d'injection (`faultdrive.py`, `faultsource.py`) |
| `test_units_of_the_harness.py` | 16 | petites unités du harnais |
| `test_geometry.py` | 14 | la géométrie CAO et sa reproductibilité |
| `test_quick.py` | 9 | `simulation.quick` |
| `test_properties.py` | 5 | propriétés hypothesis : n'importe quelle suite de consignes, n'importe quel arrêt à n'importe quel moment, n'importe quel sujet plausible |
| `test_panel_crosscheck.py` | 1 | la vraie console (`build_panel`) monte à 27 tr/min par **exactement** les mêmes consignes que le harnais |
| **Total** | **985** | |

```sh
PYTHONPATH=.:raspberry-pi raspberry-pi/.venv/bin/python -m pytest simulation/tests --co -q | tail -15
```

(La configuration pytest de `simulation/pyproject.toml` affiche un compte par
fichier au lieu d'un total.)

### 4.2 Commandes

```sh
export PYTHONPATH=.:raspberry-pi; PY=raspberry-pi/.venv/bin/python

simulation/scripts/check.sh                              # la gate simulation (voir 4.3)
$PY -m pytest simulation/tests -q                        # toute la batterie
$PY -m pytest simulation/tests/test_cohort.py -q         # la cohorte seule
$PY -m pytest simulation/tests/test_failures.py -q       # la matrice de pannes seule
$PY -m pytest simulation/tests/test_battery.py -q -k manual_27_rpm   # un seul scénario
```

### 4.3 La gate de la simulation

`simulation/scripts/check.sh` (exécutable, se lance de n'importe où). Il utilise
`raspberry-pi/.venv` et règle lui-même le `PYTHONPATH`. Étapes : `ruff check`,
`ruff format --check`, `basedpyright` strict, `mypy -p simulation` strict,
puis `pytest --cov --cov-branch`. Le seuil est **100 % de branches** sur tout
`simulation/` sauf `cad/`, `tests/` et `scripts/` (`fail_under = 100` dans
`simulation/pyproject.toml`). Les arguments supplémentaires sont passés à
pytest.

Sans variable d'environnement, la batterie reste ce seul pytest : c'est la
référence, et la façon de lancer la gate en local. La CI coupe la batterie en
parts exécutées par plusieurs jobs, avec deux variables que `check.sh` lit
(`SIMULATION_GATE_SHARES` et `SIMULATION_GATE_COMBINE`, plus
`SIMULATION_GATE_EVIDENCE`) : voir
[Gate de simulation répartie](#gate-de-simulation-répartie-anh-184).

Mesure réelle (Mac Apple silicon, 1er octobre 2026) : `GATE PASSED`,
**984 passés, 1 xfail** (S07, section 12), 100 % de branches (2955
instructions, 716 branches), **environ 33 minutes** (1951 s, dont 1941 s de
tests). `simulation/README.md` annonce « ~15 min » pour la batterie : ce
chiffre est dépassé aujourd'hui.

## 5. Lancer un scénario : `simulation.run`

```text
usage: python -m simulation.run [-h] [--all] [--list] [--csv] [--out OUT] [scenario]
```

| Option | Effet |
|---|---|
| `scenario` | nom (`manual_27_rpm`) ou chemin d'un fichier `.json` |
| `--list` | liste les scénarios (60 aujourd'hui) |
| `--all` | lance tous les scénarios et écrit `simulation/out/summary.md` |
| `--csv` | écrit aussi un CSV des lignes de la trace |
| `--out DIR` | dossier de sortie (défaut `simulation/out/`, ignoré par git) |

```sh
$PY -m simulation.run --list
$PY -m simulation.run manual_27_rpm --csv
```

Sortie réelle de la seconde commande (≈ 3 s) :

```text
scenario manual_27_rpm (manual, ecg direct): 1558 ticks, 2629 drive frames
  start: ok; end: operator_stop; final: finished, drive FAULT, shaft 0 rpm
  peak 26.99 out rpm (1344 motor rpm, 48.7 Hz); g 1.222 at 1.500 m, 1.976 at the leg tip (2.425 m)
  peak setpoint rate 0.301, arm accel 0.281 out rpm/s (limit 0.25); peak arm g-dot 0.0201 g/s ref, 0.0324 g/s leg tip (limit 0.03)
  in zone -; rules []
  all invariants and expectations hold
  trace: .../simulation/out/manual_27_rpm.jsonl
```

Comment lire :

* `drive FAULT` à la fin est **normal** en simulation : `SimulatedDrive.close`
  modélise une fermeture minimale, donc après la sortie de la console le
  variateur simulé verrouille SLF par son ttO. Le vrai `ATV320Drive.close`
  écrit la séquence d'arrêt.
* Les pics « arm accel » et « g-dot » bruts peuvent dépasser la limite affichée
  de quelques centièmes : l'invariant tolère une marge (RFRD mesuré, +1 tr/min
  arrondi). C'est la ligne `all invariants and expectations hold` qui fait foi.

**Code de sortie** : 0 si tous les invariants et attentes tiennent. Un
scénario `known_defect` qui échoue comme documenté compte comme attendu ; s'il
passe, la sortie affiche `FIXED? remove known_defect`.

Les traces vont dans `simulation/out/<scénario>.jsonl` (+ `.csv`).

## 6. Verdicts instantanés : `simulation.quick`

```text
usage: python -m simulation.quick [-h] [--cohort] [--failures] [--all] [--dsp]
                                  [--workers WORKERS] [--out OUT] [target]
```

| Option | Effet |
|---|---|
| `target` | un **cas de la matrice** (`drive_fault_overcurrent_manual`), un **scénario** (`manual_27_rpm`) ou un **identifiant de sujet** (`S07` : ses trois séances). Cherché dans cet ordre. |
| `--cohort` | 30 sujets × 3 séances (90 exécutions) |
| `--failures` | la matrice de pannes (199 cas) |
| `--all` | scénarios + pannes + cohorte |
| `--dsp` | exécute la **vraie** chaîne ECG là où elle est demandée. Sans `--dsp`, un scénario `dsp` tourne en mode `direct`, et un cas qui a besoin du vrai DSP est marqué `SKIPPED` |
| `--workers N` | nombre de processus (0 = un par CPU, défaut) |
| `--out DIR` | où écrire `report.json` et `report.html` (défaut `simulation/out/`) |

Exemples vérifiés :

```sh
$PY -m simulation.quick manual_27_rpm
# PASS    manual_27_rpm operator_stop           27.0  1.98     -    0  1.46
# 1 runs: 1 PASS - 1.47 s wall (8 workers)

$PY -m simulation.quick S07
# XFAIL   S07_auto_jog      safety_verdict          18.0  0.88  0.00    4  1.33
# PASS    S07_auto_standard safety_verdict           5.5  0.08  0.00    0  1.26
# PASS    S07_manual_bench  operator_stop           27.0  1.98     -    0  1.51

$PY -m simulation.quick --cohort
# 90 runs: 89 PASS, 1 XFAIL - 26.61 s wall (8 workers)

$PY -m simulation.quick --failures
# 199 runs: 192 PASS, 7 SKIPPED - 37.74 s wall (8 workers)

$PY -m simulation.quick --failures --dsp     # les 7 cas DSP en plus (~15 s chacun)
$PY -m simulation.quick --all --dsp          # tout
```

(Durées mesurées sur un Mac Apple silicon, 8 cœurs.)

**Statuts possibles** :

| Statut | Sens |
|---|---|
| `PASS` | aucun invariant ni attente violé |
| `FAIL` | au moins une violation, sans défaut documenté |
| `XFAIL` | échoue **comme documenté** (`known_defect`) |
| `FIXED?` | un défaut était documenté mais l'exécution passe : retirez le `known_defect` |
| `SKIPPED` | besoin du vrai DSP, relancez avec `--dsp` |

Code de sortie 0 si tout est `PASS`, `XFAIL` ou `SKIPPED`.

Colonnes du tableau : résultat, nom, fin (`end_reason`), pic de vitesse de
sortie (tr/min), pic de g à la pointe du pied, part du HOLD dans la zone,
nombre de violations, durée d'exécution (s).

### 6.1 Lire le rapport HTML

`simulation/out/report.html` est un fichier autonome (aucune dépendance,
thèmes clair et sombre). Ouvrez-le dans un navigateur.

* En tête : nombre d'exécutions par statut et durée totale.
* Un **tableau** : résultat, exécution, groupe, fin, pic tr/min, g à 1,5 m, g à
  la pointe du pied, part dans la zone, règles de sécurité vues, violations.
* Puis **une section repliable par exécution** (cliquez le résumé) : le texte
  du défaut connu s'il y en a un, la liste des violations, des petites
  courbes (vitesse de sortie et consigne, FC mesurée et vraie contre la bande
  de zone, g à 1,5 m et à la pointe du pied) et la liste numérotée des
  **messages opérateur** (ce que la console aurait affiché : verdicts, défauts
  variateur avec mnémonique et code LFT, refus).

`report.json` contient les mêmes données, pour un traitement automatique.

## 7. Le visualiseur 2D : `simulation.live`

> Le même visualiseur est aussi **en ligne**, sans rien installer :
> <https://anheart-simulation.vercel.app> (56 scénarios sur 60, vitesse de 10x
> à 200x). Ses limites et son déploiement sont décrits dans
> [deploiement.md](deploiement.md#6-le-moteur-de-simulation-hébergé).

```text
usage: python -m simulation.live [-h] [--port PORT]
```

```sh
$PY -m simulation.live               # http://127.0.0.1:8765/
$PY -m simulation.live --port 9000
```

Le serveur (bibliothèque standard seulement, écoute sur `127.0.0.1`) sert
`simulation/viewer/index.html` et trois points d'accès :

| Chemin | Rôle |
|---|---|
| `/` | redirige vers `/viewer/index.html` en gardant les paramètres, ramenés sur une seule ligne |
| `/api/scenarios` | liste JSON des scénarios |
| `/stream?scenario=…&speed=…&clock=…` | le flux Server-Sent Events d'une exécution (événements `meta`, `row`, `event`, `final`, `error`, `end`) |
| `/api/record?path=…` | un dossier d'enregistrement de schéma 2, lu par le lecteur partagé et rendu ligne par ligne pour le visualiseur ; `path` est relatif à `simulation/` |

Chaque connexion navigateur a sa propre exécution, dans son propre thread.

`/api/record` ne lit que sous `simulation/`. Le chemin demandé est résolu, liens
symboliques suivis, par `resolve_under`
([`src/record/containment.py`](../raspberry-pi/src/record/containment.py)) ; s'il
sort de ce dossier, s'il désigne le dossier lui-même ou s'il ne peut pas être un
nom de fichier, la réponse est 400 `outside_root` et rien n'est lu. Le lecteur
reçoit le chemin résolu, jamais le texte de la requête. Un dossier de
`simulation/` qui n'est pas un enregistrement donne 400 avec la raison du
lecteur. Tests : `raspberry-pi/tests/test_record_containment.py` pour la
fonction, `simulation/tests/test_cli_and_live.py` pour le serveur.

### 7.1 Paramètres d'URL du visualiseur

| Paramètre | Valeurs | Effet |
|---|---|---|
| `live` | nom de scénario | lance ce scénario en direct via `/stream` |
| `speed` | nombre ; le serveur borne à 0,1 … 200 (défaut 20) | facteur d'accélération du temps simulé |
| `clock` | `sim` (sinon `manual`) | `manual` (défaut) : temps simulé déterministe, affiché à `speed` × ; `sim` : l'horloge `SimClock` du code (temps réel accéléré). Gardez alors une vitesse modeste : une pause de la machine hôte devient un arrêt de boucle auquel le runtime réagit, comme il doit le faire |
| `trace` | chemin relatif à `simulation/` (ex. `out/manual_27_rpm.jsonl`) ou absolu (commençant par `/`) | rejoue une trace écrite par `simulation.run` |

Exemples :

```text
http://127.0.0.1:8765/?live=manual_27_rpm&speed=20
http://127.0.0.1:8765/?live=vasovagal_auto_hold&speed=60
http://127.0.0.1:8765/?live=manual_27_rpm&speed=5&clock=sim
http://127.0.0.1:8765/?trace=out/manual_27_rpm.jsonl
```

Sans serveur : ouvrez `simulation/viewer/index.html` directement et chargez un
`.jsonl` avec le bouton fichier ; ou `cd simulation && python -m http.server`
puis `?trace=out/...`.

> Détail : la liste « vitesse » de l'en-tête ne propose que 1, 5, 20, 60, 120.
> Une autre valeur passée dans `speed` est bien envoyée au serveur en mode
> `live`, mais en relecture (`trace`) la liste reste sans sélection ; choisissez
> alors une vitesse dans la liste.

### 7.2 Ce que montre l'écran

* **Vue de dessus** du bras qui tourne à la vitesse de sortie enregistrée :
  poutres, contrepoids, capsule, silhouette du passager, rayon de référence et
  pointe du pied marqués.
* **Tuiles** : vitesse de sortie, consigne, vitesse moteur, fréquence
  variateur (Hz), g au rayon de référence, g à la pointe du pied, FC vraie /
  mesurée, zone / cible, phase / mode, état du variateur, verdict de sécurité
  en cours, état de fin (comment la machine a été laissée).
* **Quatre courbes** avec curseur : vitesse (mesurée, consigne), FC (vraie,
  mesurée, bande de zone), g (référence, pointe du pied), fréquence variateur.
* **Liste d'événements** : cliquez un événement pour vous y placer.
* Commandes : bouton fichier, choix du scénario live, vitesse, Lecture/Pause,
  barre de position.

## 8. Écrire un nouveau scénario JSON

Un scénario = un fichier `simulation/scenarios/<nom>.json`. Ajouter un
scénario, c'est ajouter un fichier : `test_battery.py` et `simulation.run
--list` le découvrent seuls. Les fichiers qui commencent par `_` (comme
`_profiles.json`) ne sont pas des scénarios.

Le parseur (`simulation/scenario.py`) **refuse toute clé inconnue**, à tous les
niveaux, et rapporte tous les problèmes d'un coup. Une faute de frappe ne peut
donc pas transformer en silence un scénario de panne en scénario nominal.

### 8.1 Exemple

JSON n'accepte pas de commentaires : l'exemple est « propre », les explications
suivent dans les tableaux.

```json
{
  "name": "manual_20_rpm_then_estop",
  "description": "Montée manuelle à 20 tr/min, E-STOP à 120 s, acquittement à 200 s.",
  "tags": ["manual", "estop"],
  "kind": "manual",
  "duration_s": 300,
  "teardown_s": 60,
  "preroll_s": 15,
  "geometry": {"reference_radius_m": 1.5, "leg_tip_radius_m": 2.4254},
  "manual": {"occupancy": "bench", "ceiling_motor_rpm": 1380},
  "ecg": {"mode": "direct", "period_s": 1.0, "noise_bpm": 0, "seed": 1},
  "drive": {"tto_s": 3.0, "acceleration_time_s": 10.0, "hsp_motor_rpm": 1380},
  "actions": [
    {"at_s": 2, "do": "manual_target", "output_rpm": 20.0, "expect": "accepted"},
    {"at_s": 120, "do": "estop"},
    {"at_s": 150, "do": "manual_target", "output_rpm": 10.0, "expect": "refused"},
    {"at_s": 200, "do": "acknowledge", "estop_released": true}
  ],
  "expect": {
    "end_reason": "emergency_stop",
    "rules": ["operator_estop"],
    "reaches_output_rpm": 20.0,
    "max_output_rpm": 20.05
  }
}
```

Puis :

```sh
$PY -m simulation.run manual_20_rpm_then_estop
$PY -m simulation.quick manual_20_rpm_then_estop
```

Un scénario AUTO remplace le bloc `manual` par un `profile` :

```json
{
  "name": "vasovagal_auto_hold",
  "kind": "auto",
  "duration_s": 1500,
  "profile": "jog_150_30_min",
  "subject_events": [{"event": "vasovagal_drop", "at_s": 915, "duration_s": 20}],
  "expect": {"rules": ["hr_drop"]}
}
```

(Ce second exemple est un fichier réel du dépôt, sans ses champs `description`
et `tags`.)

### 8.2 Clés de premier niveau

| Clé | Obligatoire | Type / valeurs | Défaut | Sens |
|---|---|---|---|---|
| `name` | oui | texte | - | nom du scénario |
| `description` | non | texte | `""` | une phrase |
| `tags` | non | liste de textes | `[]` | étiquettes libres |
| `kind` | oui | `"auto"` \| `"manual"` | - | séance programmée (FC → vitesse) ou manuelle |
| `duration_s` | oui | nombre > 0 | - | horizon après le START |
| `teardown_s` | non | nombre ≥ 0 | 60 | temps pendant lequel la machine continue sans personne qui « tick » après la sortie de la console |
| `preroll_s` | non | nombre ≥ 0 | 15 | console au repos (lecture seule) avant le START |
| `geometry` | non | objet (8.3) | géométrie CAO | rayons |
| `profile` | AUTO seulement | id de profil ou objet profil complet | - | le programme. Interdit pour `manual` |
| `profile_overrides` | non | objet | `{}` | champs du profil à remplacer (ex. `{"total_duration_s": 900}`) |
| `manual` | non | objet (8.4) | banc, 1380 | séance manuelle |
| `subject` | non | objet (8.5) | physiologie par défaut | le passager simulé |
| `subject_events` | non | liste (8.6) | `[]` | événements scriptés du cœur |
| `ecg` | non | objet (8.7) | mode `direct` | source de FC |
| `drive` | non | objet (8.8) | - | le variateur simulé |
| `actions` | non | liste (8.9) | `[]` | ce qui arrive, et quand |
| `expect` | non | objet (8.10) | - | ce qui doit être vrai à la fin |
| `known_defect` | non | texte | absent | marque un défaut connu : le test devient un xfail strict (section 12) |

**Profils disponibles** pour `profile` : ceux de
`raspberry-pi/config/profiles.default.json` (ex. `standard_30_min`,
`standard_45_min`) plus ceux de `simulation/scenarios/_profiles.json`
(`jog_150_30_min`, `jog_150_short`). Les profils de `_profiles.json` montent
jusqu'à 1380 tr/min moteur pour que la zone « jog » 145-155 bpm soit
atteignable ; ils sont marqués **« NOT clinically signed off: simulation
only »**.

### 8.3 `geometry`

| Clé | Sens |
|---|---|
| `reference_radius_m` | rayon où le runtime calcule le g (l'équivalent de `ARM_RADIUS_M`) ; défaut 1,5 m |
| `leg_tip_radius_m` | point le plus éloigné du passager ; défaut 2,4254 m (paroi intérieure de la capsule, borne haute) |

### 8.4 `manual`

| Clé | Valeurs | Défaut |
|---|---|---|
| `occupancy` | `"bench"` (personne à bord : non) \| `"occupied"` | `"bench"` |
| `ceiling_motor_rpm` | entier (tr/min moteur) | 1380 |

### 8.5 `subject` (le cœur simulé, `PhysiologyConfig`)

| Clé | Sens |
|---|---|
| `hr_rest` | FC de repos (bpm, entier) |
| `hr_max` | FC max vraie (bpm, entier) |
| `k_g` | gain : bpm gagnés par g au rayon de référence (défaut du modèle : 110) |
| `tau_up`, `tau_down` | constantes de temps de montée / descente (s) |
| `drift_max`, `tau_drift` | dérive cardiaque maximale (bpm) et sa constante de temps (s) |
| `fatigue` | coefficient de fatigue |

Toute clé absente prend la valeur par défaut du modèle de production.

### 8.6 `subject_events`

Chaque entrée : `{"event": …, "at_s": …, "duration_s": …}` (tous obligatoires).

| `event` | Effet dans le modèle |
|---|---|
| `vasovagal_drop` | effondrement vagal (chute rapide de FC) |
| `hr_spike` | pic cardiaque brutal |
| `electrode_off` | électrode décollée (artefact rendu par le synthétiseur ECG) |
| `mains_burst` | bouffée de 50 Hz secteur |
| `nonresponder` | le cœur ne répond presque plus au g |

### 8.7 `ecg`

| Clé | Valeurs | Défaut | Sens |
|---|---|---|---|
| `mode` | `"direct"` \| `"dsp"` | `direct` | `direct` : modèle de capteur rapide (FC vraie arrondie, 1 lecture / `period_s`). `dsp` : vraie chaîne BITalino simulé → `SignalTreatment` (BioSPPy) → `EcgBridge`, ~50 ms de CPU par seconde simulée |
| `period_s` | > 0 | 1,0 | période des lectures (direct) |
| `noise_bpm` | ≥ 0 | 0 | bruit gaussien de mesure |
| `seed` | entier | 0 | graine du bruit |
| `ectopic_rate` | 0..1 | 0 | probabilité qu'une lecture porte un artefact ectopique (direct) |
| `ectopic_bpm` | ≥ 0 | 0 | amplitude de cet artefact, bpm, dans les deux sens (direct) |
| `motion_noise_bpm_per_g` | ≥ 0 | 0 | bruit supplémentaire par g : artefact de mouvement (direct) |
| `connect_fails` | booléen | false | le BITalino ne se connecte jamais |

### 8.8 `drive` (le variateur simulé)

| Clé | Défaut | Sens |
|---|---|---|
| `tto_s` | 3,0 | chien de garde Modbus du variateur (ttO) |
| `acceleration_time_s` | 10,0 | temps de rampe du variateur |
| `hsp_motor_rpm` | 1380 | vitesse haute (HSP) en tr/min moteur |
| `initial_fault` | - | un défaut déjà verrouillé à l'ouverture de la liaison : un nom de `DriveFault` (ex. `"OVERCURRENT"`) |
| `initial_enabled_rpm` | - | variateur trouvé OPERATION_ENABLED et tournant à cette vitesse (un processus précédent est mort moteur commandé). Dans `(0, hsp_motor_rpm]`, exclusif avec `initial_fault` |
| `refuse_commands` | `[]` | mots de commande refusés dès la première trame : `SHUTDOWN`, `SWITCH_ON`, `ENABLE_OPERATION`, `FAULT_RESET` |

Les noms de `DriveFault` sont ceux de `raspberry-pi/src/motor/drive.py`
(la table complète est dans [raspberry-pi.md](raspberry-pi.md)). Un nom
inconnu est refusé avec la liste des noms valides.

### 8.9 `actions`

Chaque action a `at_s` (≥ 0, secondes après le START) et `do`. Les actions
sont triées par instant. Clés autorisées dans une action : `at_s`, `do`,
`output_rpm`, `expect`, `estop_released`, `fault`, `duration_s`, `latency_s`,
`quality`, `bpm`, `word`, `jump_s`, `gap`, `signal`.

**Opérateur et séance**

| `do` | Paramètres | Effet |
|---|---|---|
| `manual_target` | `output_rpm`, `expect` (`accepted` / `refused` / `any`, défaut `accepted`) | consigne manuelle en tr/min de sortie |
| `operator_stop` | - | bouton STOP de la console |
| `remote_stop` | - | arrêt demandé à distance (chemin `EndSession`, comme le tableau de bord) |
| `estop` | - | E-STOP |
| `acknowledge` | `estop_released` (défaut true) | acquittement |
| `start_again` | `expect` (défaut `refused`) | second START pendant la séance |
| `fault_reset` | `expect` (défaut `any`) | demande de réarmement d'un défaut variateur |
| `attendant_leaves` | - | l'opérateur cesse de « pinger » la console |
| `shutdown` | - | le processus reçoit SIGTERM |
| `tick_exception` | - | une exception dans le tick ; la console sort |

**Variateur**

| `do` | Paramètres | Effet |
|---|---|---|
| `drive_fault` | `fault` (nom de `DriveFault`, défaut `MOTOR_OVERLOAD`) | le variateur verrouille ce défaut |
| `comms_loss` | `duration_s` | liaison Modbus morte |
| `drive_latency` | `latency_s`, `duration_s` | chaque échange prend `latency_s` |
| `drive_reverse` | - | phases moteur inversées |
| `register_offset` | - | carte des registres décalée d'un cran |
| `drive_refuse_command` | `word` (défaut `ENABLE_OPERATION`) | le variateur répond ce mot par une exception Modbus |
| `drive_echo_mismatch` | - | l'écho LFRD ne suit plus ce qui est écrit |
| `drive_speed_stuck` | - | RFRD (vitesse mesurée) figé |
| `drive_status_frozen` | - | chaque lecture d'état répond `Ok` avec un état figé |

**ECG / BITalino**

| `do` | Paramètres | Mode | Effet |
|---|---|---|---|
| `ecg_dropout` | `duration_s` | direct | plus aucune FC |
| `ecg_quality` | `quality` (`good`, `noisy`, `mains_dominated`, `no_signal`), `duration_s` | direct | le DSP classe le signal ainsi |
| `ecg_repeat_seq` | `duration_s` | direct | le DSP réémet ses métriques précédentes (même numéro de séquence) |
| `ecg_value` | `bpm`, `duration_s` | direct | le capteur rapporte cette FC (qualité GOOD), quoi que fasse le cœur |
| `ecg_seq_gap` | `gap` (défaut 5) | direct | le numéro de séquence saute |
| `ecg_silent_stop` | - | direct | les lectures s'arrêtent, la liaison se dit toujours active |
| `bitalino_disconnect` | - | tous | la liaison BITalino tombe et ne revient pas |
| `bitalino_signal` | `signal` (`flat`, `saturated`, `corrupted`, `mains`, `stopped`, `gaps`), `duration_s` | **dsp** | les échantillons bruts sont corrompus **avant** le vrai DSP |

**Processus**

| `do` | Paramètres | Effet |
|---|---|---|
| `loop_stall` | `duration_s` | la boucle s'arrête : pas de tick, pas de keepalive, la machine continue |
| `clock_jump` | `jump_s` (peut être négatif) | l'horloge **murale** saute (NTP, Pi sans RTC). Le temps monotone n'est pas touché |

### 8.10 `expect`

| Clé | Sens |
|---|---|
| `start` | `accepted` (défaut) / `refused` / `any` : le START doit-il être accepté ? |
| `end_reason` | `programme_complete`, `operator_stop`, `emergency_stop`, `safety_verdict`, `tick_exception`, `shutdown` |
| `final_state` | `idle`, `running`, `ending`, `finished` |
| `rules` | règles de sécurité qui **doivent** s'être déclenchées (ex. `hr_drop`, `comms_lost`) |
| `forbid_rules` | règles qui **ne doivent pas** se déclencher |
| `reaches_output_rpm` | vitesse de sortie qui doit être atteinte |
| `max_output_rpm` | vitesse de sortie à ne jamais dépasser |
| `min_in_zone_fraction` | part minimale du HOLD passée dans la zone (0..1) |

Les identifiants des règles de sécurité sont listés dans
[raspberry-pi.md](raspberry-pi.md). Les invariants physiques (section 11)
sont toujours vérifiés, même sans bloc `expect`.

## 9. La cohorte de 30 personnes

### 9.1 Ce que c'est

`simulation/cohort/generate.py` tire 30 **personnes fictives** d'une seule
graine (`SEED = 20_260_930`) et les écrit dans `simulation/cohort/cohort.json`.
Le fichier est reproductible au bit près.

* âges 10 à 50 ans : cinq de 10 à 15 ans (le premier a exactement 10 ans),
  puis 25 de 16 à 50 ans ;
* FC max = Tanaka `208 - 0,7 × âge` plus un écart individuel ~N(0, 7) borné à
  ± 15 bpm ;
* FC de repos selon la condition physique, gain `k_g`, `tau_up` / `tau_down`,
  dérive, fatigue, bruit ECG ;
* onze conditions particulières : 2 répondeurs lents, 2 rapides, 1
  non-répondeur, 2 sujets à malaise vagal, 2 à extrasystoles, 2 à fort
  artefact de mouvement.

Chaque trait se traduit uniquement en réglages **existants** du modèle de
production (`PhysiologyConfig`), du modèle de capteur direct et des
événements scriptés. Rien n'est ajouté à la physiologie.

`simulation/cohort/battery.py` lance chaque sujet × {jog AUTO 145-155 bpm 30
min, AUTO `standard_30_min`, MANUEL 27 tr/min au banc}. Avant une séance AUTO,
le programme est résolu pour la FC max du sujet par `ProfileStore.resolve` (le
même appel que la console fait pour un lancement depuis le tableau de bord),
qui doit refuser exactement quand `zone_high > 0,9 × FCmax` ; les sujets sous
`MIN_RIDER_AGE` (18 par défaut) sont refusés. Pour les séances AUTO : jamais
au-dessus du plafond du programme ; `hr_drop` doit se déclencher pour un sujet
à malaise vagal, et **ne doit pas** se déclencher (ni `hr_critical`) pour les
autres.

Résultat actuel : **89 PASS, 1 XFAIL (S07, jog), 0 FAIL**.

```sh
$PY -m simulation.cohort.generate --check   # code 0 si cohort.json est à jour, 1 sinon (aucune sortie)
$PY -m simulation.cohort.generate           # réécrit cohort.json
$PY -m simulation.quick --cohort            # les 90 exécutions + report.html
$PY -m pytest simulation/tests/test_cohort.py -q
```

### 9.2 Ajouter une personne

Le fichier `cohort.json` est **généré** : `test_cohort.py` vérifie qu'il est
identique à ce que produit la graine, et qu'il contient exactement 30 sujets.
**N'éditez pas `cohort.json` à la main** : le test échouera.

Deux façons de faire :

1. **Tester une personne précise, sans toucher à la cohorte (recommandé).**
   Écrivez un scénario (section 8) avec son bloc `subject` et, si besoin, un
   bloc `ecg` et des `subject_events`. La correspondance avec les champs d'un
   sujet de la cohorte :

   | Champ de `cohort.json` | Où le mettre dans un scénario |
   |---|---|
   | `hr_rest`, `hr_max`, `k_g`, `tau_up`, `tau_down`, `drift_max`, `tau_drift`, `fatigue` | `subject` |
   | `ecg_noise_bpm` | `ecg.noise_bpm` |
   | `ectopic_rate`, `ectopic_bpm`, `motion_noise_bpm_per_g` | `ecg` |
   | `vasovagal_at_s` | `subject_events: [{"event": "vasovagal_drop", "at_s": …, "duration_s": 20}]` |
   | condition `nonresponder` | `subject_events: [{"event": "nonresponder", …}]` |
   | `age_years` | pas d'équivalent dans le schéma de scénario : la barrière d'âge est testée par la cohorte et par les tests du Pi |

   Pour partir des nombres d'un sujet existant :
   `python3 -c "import json;print(json.load(open('simulation/cohort/cohort.json'))['subjects'][6])"`.

2. **Agrandir ou changer la cohorte.** Modifiez `SIZE`, `MINORS` ou `SPECIAL`
   dans `generate.py`, régénérez avec `python -m simulation.cohort.generate`,
   puis mettez à jour le test `test_the_cohort_spans_the_brief` (qui exige
   30) et, si un nouveau couple sujet/séance montre un défaut connu, la table
   `KNOWN_VASOVAGAL_ONSET` de `battery.py`. Attention : tous les tirages
   viennent du même générateur, dans un ordre fixe ; changer la taille change
   aussi les sujets suivants, donc les tableaux de `simulation/README.md`.

## 10. La matrice de 199 pannes

### 10.1 Ce que c'est

`simulation/failures.py` construit 199 **cas**. Chaque cas est un document de
scénario dans le même schéma JSON (section 8), plus ce qui doit être vrai
après. Pour chaque cas, `tests/test_failures.py` vérifie :

* tous les invariants physiques (arbre à 0 après démontage, pas de couple,
  LFRD à 0 là où une trame pouvait être écrite, pas de NaN, sécurité
  dominante, plus aucune trame après le silence) ;
* la sortie désactivée, ou, si le runtime s'est tu, que le ttO du variateur a
  visiblement pris le relais ;
* la raison de fin fait partie de celles permises, les règles nommées se sont
  déclenchées ;
* l'opérateur a été prévenu avec les mots attendus (phrase du verdict,
  mnémonique du variateur, texte de refus de la console) ;
* pour les cas de signal ECG, aucune fausse FC classée utilisable (à plus de
  15 bpm de la vérité) ;
* l'injection est tombée dans la phase annoncée ; un délai de détection quand
  il compte.

Un second test vérifie qu'**aucun cas**, défaut connu ou pas, ne laisse du
couple ou un arbre qui tourne.

Familles (catégories `drive`, `ecg`, `process`, `operator`) : perte de
communication à chaque phase ; **chacun des 66 codes LFT** plus un code
inconnu (251), en AUTO et en MANUEL ; variateur trouvé déjà en marche ;
mots de commande refusés ; écho LFRD faux ; vitesse qui ne suit pas ; état
figé ; ECG perdu à chaque phase ; échec de connexion ; signaux corrompus avant
le vrai DSP ; SIGTERM à chaque phase ; exception dans le tick ; arrêt de
boucle 1-5 s ; saut d'horloge ; erreurs opérateur (double START, réarmement
interdit, consignes absurdes).

Résultat : **199 PASS, 0 XFAIL** d'après `simulation/README.md` (avec
`--dsp`). Sans `--dsp`, `simulation.quick --failures` donne 192 PASS et 7
SKIPPED (les 7 cas à vrai DSP).

```sh
$PY -m simulation.quick --failures --dsp
$PY -m simulation.quick drive_fault_overcurrent_manual
$PY -m pytest simulation/tests/test_failures.py -q
$PY -m pytest simulation/tests/test_failures.py -q -k drive_fault_overcurrent
```

Les mêmes familles tournent aussi contre la **vraie racine de composition**
(`build_panel`, API HTTP, `LocalPanel.run`) dans
`raspberry-pi/tests/test_failure_{rig,drive,ecg,process,operator}.py`, avec
les cas que seule la console peut exprimer : serveur web qui meurt, lien
tableau de bord qui lève une exception ou est injoignable, vrai SIGTERM, refus
HTTP 409/422, échantillons NaN, déconnexion BITalino transitoire.

### 10.2 Ajouter une panne

Les cas sont construits en Python, pas en fichiers. Ajoutez un
`FailureCase(...)` dans la fonction de sa famille (`_drive_cases`,
`_ecg_cases`, `_process_cases`, `_operator_cases`) ; `cases()` les
rassemble et le test paramétré le découvre seul.

Aides disponibles : `_auto(nom, actions, at=…)` (le jog court
`jog_150_short` de 13 min), `_manual(nom, actions)` (montée à 27 tr/min, STOP
à 200 s), `_dsp(nom, signal)`. Les instants par phase sont dans
`AUTO_PHASES` (baseline 30, warmup 200, hold 500, cooldown 690, recovery 750 s)
et `MANUAL_PHASES` (ramp_up 40, at_speed 170, ramp_down 230 s).

Champs de `FailureCase` :

| Champ | Sens |
|---|---|
| `name` | nom unique du cas |
| `category` | `Category.DRIVE` / `ECG` / `PROCESS` / `OPERATOR` |
| `description` | une phrase |
| `document` | le scénario (dictionnaire au schéma de la section 8) |
| `end_reasons` | raisons de fin permises (ou `REFUSED` si le START doit être refusé) |
| `rules` | règles qui doivent se déclencher |
| `messages` | sous-chaînes qui doivent apparaître dans au moins un message opérateur |
| `silent` | le runtime doit-il s'être tu (`True`/`False`/`None` = peu importe) |
| `at`, `phase` | instant d'injection et phase du runtime attendue à cet instant |
| `rate_window` | fenêtre (début, fin) où aucune FC fausse ne doit être classée utilisable |
| `deadline` | instant avant lequel chaque règle de `rules` doit avoir tiré |
| `stopped_by` | instant avant lequel l'arbre doit être à l'arrêt (ou le runtime muet) |
| `known_defect` | texte : le cas devient un xfail strict |

Exemple (à placer dans `_operator_cases`, par exemple) :

```python
cases.append(
    FailureCase(
        name="operator_stop_twice_manual",
        category=Category.OPERATOR,
        description="Deux STOP à 1 s d'intervalle pendant la descente.",
        document=_manual(
            "operator_stop_twice_manual",
            [{"at_s": 201.0, "do": "operator_stop"}],
        ),
        end_reasons=frozenset({"operator_stop"}),
    )
)
```

Vérifiez avec `$PY -m simulation.quick operator_stop_twice_manual`, puis
lancez la gate simulation (la couverture à 100 % doit tenir).

## 11. Les invariants vérifiés sur chaque trace

Vérifiés par `simulation/invariants.py` pour **toute** exécution (scénario,
cohorte, panne, propriété) :

* aucun NaN ni infini ;
* consigne dans `{0} ∪ [min_run, plafond]` ; chaque LFRD **écrit au variateur**
  dans le même domaine ; vitesse mesurée sous HSP ; pas de rotation inverse ;
* la consigne monte et descend dans ce que promet le runtime (programme :
  pente de 15 tr/min/s ; manuel : vitesse du profileur de mouvement), le zéro
  d'urgence excepté ;
* l'arbre mesuré ne bat jamais la rampe du variateur ;
* avec un verdict à FREEZE ou plus : la consigne ne monte jamais ; une fois une
  fin commencée, elle ne monte jamais ; pendant un malaise vagal scripté (+60
  s), elle ne monte jamais ;
* séances manuelles : dérivée du g au rayon de référence ≤ 0,03 g/s, et bras
  mesuré ≤ 0,25 tr/min/s de sortie et ≤ 0,03 g/s sur des fenêtres de 1 s ;
* après le passage au silence : plus une seule trame, lectures comprises ;
* un START refusé n'a rien fait bouger ;
* **toute sortie** : après démontage, le variateur ne produit pas de couple,
  l'arbre lit 0 tr/min, et (si une trame pouvait passer) LFRD vaut 0 et le
  runtime ne commande rien.

`test_invariants.py` prouve que chaque invariant se déclenche sur une trace
corrompue exprès : le vérificateur ne peut pas être aveugle en silence.

## 12. Les « xfail strict » et le cas résiduel S07

### 12.1 Ce que c'est

Un **xfail strict** (`pytest.mark.xfail(strict=True)`) est un test qui décrit
le **bon** comportement, que le code **ne fournit pas encore**. Il est attendu
en échec :

* tant que le défaut existe, le test « échoue comme prévu » : la suite reste
  verte, mais le défaut reste **visible** dans chaque rapport ;
* le jour où le défaut est corrigé, le test passe… et `strict=True` le fait
  **devenir rouge**. Il faut alors retirer la marque. Un défaut corrigé ne peut
  donc pas rester étiqueté « connu » par oubli.

Dans le dépôt, la marque vient de la clé `known_defect` (scénario, cas de
panne, ou couple de cohorte via `known_defect_for`). `simulation.run` et
`simulation.quick` affichent le même statut (`XFAIL` / `FIXED?`).

### 12.2 Les xfail stricts actuels

Vérifié en cherchant `xfail(strict=True)` et `known_defect` dans le code :

* **aucun** scénario de `simulation/scenarios/` ne porte `known_defect` ;
* **aucun** cas de `failures.py` n'a de `known_defect` (les deux derniers,
  `ecg_dsp_corrupted` et `ecg_dsp_gaps`, ont été corrigés) ;
* **aucun** xfail dans `raspberry-pi/tests/` (les commentaires « was a strict
  xfail » y marquent d'anciens défauts corrigés) ;
* **un seul** reste : le couple de cohorte **S07 × jog AUTO**
  (`KNOWN_VASOVAGAL_ONSET` dans `simulation/cohort/battery.py`).

### 12.3 Le cas résiduel S07

S07 : femme de 48 ans, sujette au malaise vagal (effondrement scripté à
t = 679 s). Le verrou « vasovagal » du runtime interdit à la consigne de
**monter** tant que la pente des cinq dernières lectures de FC est sous
-20 bpm/min (ou inconnue). Mais une montée **décidée au tick même où
l'effondrement commence**, avant que le capteur ne voie la moindre chute, est
encore exécutée jusqu'au bout.

Chiffres enregistrés dans le code : 825 → 837 tr/min moteur (+0,24 tr/min de
sortie) sur t = 679,0-679,8 s, décidé à t = 679,0 s, FC vraie encore 123 bpm,
lecture 122 ; la première chute visible est à t = 681 s (118 bpm), plus rien ne
monte ensuite, et `hr_drop` termine la séance à t = 698 s. L'invariant violé
est `vasovagal_no_accel`. `simulation.quick S07` le montre bien en `XFAIL`
(4 violations).

Avant ce verrou, le contrôleur continuait d'accélérer pendant tout
l'effondrement, jusqu'à `hr_drop` (constat n° 1 de `simulation/README.md`).

## 13. La géométrie extraite de la CAO

`simulation/cad/extract_geometry.py` lit le fichier STEP
`CAO/Gaura_Assy_2907.STEP` avec le lecteur XCAF d'OCCT (chaque pièce avec son
placement et son nom), calcule les boîtes englobantes et lance des rayons dans
la capsule pour trouver ses parois intérieures. Le résultat,
`simulation/cad/machine_geometry.json`, note pour chaque nombre la pièce
d'origine et une confiance. Il enregistre aussi l'empreinte SHA-256 du STEP
(128 pièces placées, 30 produits distincts).

Régénérer (environnement Python séparé) :

```sh
cd simulation/cad
uv venv --python 3.12 .venv-cad && uv pip install --python .venv-cad/bin/python cadquery-ocp
.venv-cad/bin/python extract_geometry.py ../../CAO/Gaura_Assy_2907.STEP machine_geometry.json
```

Un test relance l'extracteur et vérifie que le JSON commité est reproduit à
l'identique ; il est **sauté** si `.venv-cad` ou le STEP manque.

| Grandeur | Valeur | Source | Confiance |
|---|---|---|---|
| axe de rotation | vertical (+Y), passant par (0, 0) | arbre `AXE KZBF45`, coaxial avec les roulements 6009, la butée 81209, le disque de frein et l'entretoise (0,000 mm d'écart) | haute |
| bras, extrémité extérieure | 1,840 m | deux poutres `Profile Polyester` (nommées « 4000mm », modélisées 3000 mm : **vérifier laquelle est construite**) | haute |
| bras, extrémité intérieure / centre du contrepoids | 1,160 m / 0,936 m | mêmes poutres / `Support Poids` | haute |
| roues d'appui | 1,115 m et 1,795 m | `CGZJ50-N` | haute |
| capsule, paroi intérieure extérieure | 2,378 m au plancher, 2,425 m au max | `Human_Capsule_v2`, rayons à 5..195 mm du plancher | haute |
| capsule, paroi intérieure proche | 0,328 m, de l'autre côté de l'axe | idem | haute |
| posture | allongé le long du bras, tête vers l'axe, pieds vers l'extérieur | déduit de la forme de la capsule et de la verrière | faible à moyenne |
| passager | **absent de la CAO** | - | - |

Comme il n'y a pas de passager dans la CAO, deux rayons sont des
**paramètres**, marqués `must_be_measured` :

* **pointe du pied** (point le plus éloigné, où le g est maximal) : 2,4254 m par
  défaut, la paroi extérieure de la capsule, donc une **borne haute** (pieds
  contre la paroi). Une estimation par la taille est aussi notée : 1,42 m (tête
  contre la paroi proche, 1,75 m de taille supposée) ;
* **rayon de référence** (`ARM_RADIUS_M`, où le runtime calcule le g et applique
  la limite de dérivée du g) : 1,5 m. Il ne vient **pas** de la CAO.

g centripète (voir [glossaire](glossaire.md)) aux vitesses clés, vérifiés par
`test_limits.py` :

| tr/min sortie | tr/min moteur | Hz variateur | g à 1,5 m | g à 2,4254 m | g à 1,42 m |
|---|---|---|---|---|---|
| 27,0 | 1344 | 48,7 | 1,223 | 1,977 | 1,159 |
| 27,7 (plaque) | 1380 | 50,0 | 1,289 | 2,083 | 1,221 |
| 32,0 | 1593 | 57,7 | 1,718 | 2,777 | 1,628 |

32 tr/min de sortie demanderait 57,7 Hz, au-dessus de la plaque moteur (1380
tr/min à 50 Hz) et du HSP de 50 Hz : le runtime le **refuse**, et la batterie
vérifie ce refus (`manual_32_rpm_refused`, `manual_27_then_32_refused`).

## 14. Pièges connus

| Symptôme | Cause | Solution |
|---|---|---|
| `ModuleNotFoundError: simulation` ou `src` | `PYTHONPATH` absent | `export PYTHONPATH=.:raspberry-pi`, depuis la racine |
| `permission denied: ./scripts/check.sh` | le système a perdu le bit exécutable | `bash scripts/check.sh` |
| cas `SKIPPED` dans `quick --failures` | cas à vrai DSP | ajoutez `--dsp` |
| scénario à artefact ECG `SKIPPED` dans `quick --all` | le capteur DIRECT ne simule pas une électrode débranchée ou une perturbation du signal électrique | ajoutez `--dsp` ; la gate simulation complète garde les attentes de sécurité sur le vrai traitement du signal |
| `test_the_committed_cohort_is_exactly_what_the_seed_generates` échoue | `cohort.json` édité à la main | `python -m simulation.cohort.generate` (ou annulez l'édition) |
| test de reproductibilité CAO « skipped » | `simulation/cad/.venv-cad` ou le STEP absent | normal ; voir section 13 pour recréer le venv |
| `drive FAULT` en fin de `simulation.run` | fermeture minimale du variateur simulé : SLF via ttO | attendu en simulation |
| la batterie complète est longue | les scénarios `dsp` dominent | pendant le travail, utilisez `simulation.quick` ou `-k` |

## 15. CI

Le workflow `.github/workflows/ci.yml` s'exécute sur les PR vers `main` et
`develop`, sur les push de ces branches, et à la demande depuis
**Actions → CI → Run workflow**. Le déclenchement nocturne à 01:17 UTC devient
actif lorsque le workflow est présent sur la branche par défaut de GitHub.
Les jobs sont parallèles. Six noms sont exigés par la protection de branche
de `develop` : `pi-gate`, `simulation-gate`, `convex-tests`, `web`, `audit` et
`docs`.

| Job | Contrôles et artefacts |
|---|---|
| `changes` | classe les fichiers changés par la PR et dit aux quatre gates ci-dessous si elles peuvent être sautées (voir [Gates lancées selon les fichiers changés](#gates-lancées-selon-les-fichiers-changés-anh-184)) ; lance d'abord les tests de cette règle, ceux du workflow et ceux du workflow CodeQL |
| `pi-gate` | gate Pi complète, tests répartis sur un processus pytest indépendant par CPU du runner (voir [Gate Pi en parallèle](#gate-pi-en-parallèle-anh-72)), couverture de branches à 100 % sur la chaîne de sécurité, combinée avant le seuil ; `coverage.xml`. Sur le déclenchement nocturne seulement (`github.event_name == 'schedule'`), une étape de plus après la gate : l'endurance de l'enregistrement de séance, une journée simulée de séances avec l'écrivain actif (`tests/test_record_endurance.py -m slow`, `ANHEART_ENDURANCE_HOURS=24`, ANH-128) |
| `simulation (cohort)`, `simulation (battery 1)` à `simulation (battery 3)` | dans chacun : reproductibilité CAO via Git LFS et l'extracteur OCCT, ruff, basedpyright, mypy, puis ses parts de la batterie de scénarios, un processus pytest par part ; artefacts `simulation-evidence-*` (ce que chaque part a collecté, exécuté et mesuré) |
| `simulation (report)` | `simulation.quick --all`, rejeu nocturne des scénarios réels ; artefact `simulation-report` |
| `simulation-gate` | la vérification obligatoire : exige la réussite des cinq jobs précédents, puis prouve que chaque test de la batterie a tourné une fois et une seule, fusionne les mesures et applique le seuil de 100 % de branches (voir [Gate de simulation répartie](#gate-de-simulation-répartie-anh-184)) ; `coverage.xml`, `report.json` et `report.html` |
| `convex-tests` | types des fonctions Convex (`tsc -p convex/tsconfig.json --noEmit`), puis vrais handlers Convex exécutés par `convex-test` : droits d'accès aux mesures live, séances et télémétrie ; aucune connexion au déploiement de production |
| `web` | TypeScript, ESLint hors environnements Python, tests du panneau manuel et des fonctions ECG du site, build Next.js avec configuration publique de test |
| `audit` | `npm audit`, `pip-audit` et `gitleaks` sur l'historique Git ; aucun secret de production requis |
| `docs` | liens locaux et ancres Markdown, résolution des identifiants `MEN-nn` dès que `docs/menaces.md` existe |

Un autre workflow, `codeql.yml`, fait analyser le dépôt par CodeQL sans être
une gate : voir
[Analyse statique externe](#analyse-statique-externe--codeql-anh-196).

`npx tsc --noEmit`, dans `web`, lit les fichiers de `convex/` avec les
réglages du site. `convex/tsconfig.json` est un projet TypeScript à part, avec
ses propres réglages : une erreur de type visible seulement avec eux passait
la CI. `convex-tests` lance maintenant ce second contrôle avant les tests.

**Durées mesurées le 6 octobre 2026.** « Avant » : les cinq push sur `develop`
de la journée (runs 37476229651, 37495298240, 37502103415, 37516988940 et
37528383420). « Après » : les deux exécutions de la PR #27, qui lance toutes
les gates parce qu'elle modifie `.github/`.

| Vérification obligatoire | Avant | Après |
|---|---|---|
| `simulation-gate` (du lancement du run au verdict) | 65 min 38 s à 66 min 46 s sur quatre runs, 49 min 02 s sur le cinquième | 17 min 29 s (run 37539316521, première version, parts distribuées à tour de rôle), puis 13 min 10 s (run 37542166047, parts équilibrées) |
| `pi-gate` | 10 min 21 s à 19 min 21 s | inchangée par ce travail : 18 min 16 s (run 37539316521). C'est maintenant la vérification la plus longue |
| `web` | 0 min 53 s à 1 min 19 s | 1 min 13 s et 1 min 16 s |
| `convex-tests` | 0 min 27 s à 0 min 31 s | 0 min 35 s et 0 min 36 s, contrôle des types compris |
| `audit` | 0 min 50 s à 1 min 27 s | 1 min 08 s et 1 min 03 s |
| `docs` | 0 min 07 s à 0 min 10 s | 0 min 07 s et 0 min 09 s |

Détail de `simulation-gate` sur le run 37542166047 (parts équilibrées) :

| Job | Durée | Processeur du runner |
|---|---|---|
| `simulation (cohort)` | 6 min 25 s | AMD EPYC 7763 |
| `simulation (battery 1)` | 5 min 25 s | AMD EPYC 9V45 |
| `simulation (battery 2)` | 10 min 44 s | AMD EPYC 7763 |
| `simulation (battery 3)` | 7 min 56 s | Intel Xeon Platinum 8573C |
| `simulation (report)` | 3 min 56 s | non affiché par ce job |
| `simulation-gate` (le verdict seul) | 1 min 14 s | non affiché par ce job |

Les trois jobs de la batterie ont à peu près le même travail : l'écart vient
du processeur attribué au runner, qui change d'un job à l'autre. Avec la
première version, `simulation (battery 3)` prenait 15 min 28 s à elle seule
(voir [Gate de simulation répartie](#gate-de-simulation-répartie-anh-184)).

Ce qu'une PR attend dépend maintenant de ce qu'elle touche. Une PR de
documentation seule n'attend que `docs` et `audit`, une PR du site ou de Convex
y ajoute `web` et `convex-tests` : environ une à deux minutes dans les deux
cas, à confirmer sur la première PR de chaque sorte fusionnée après celle-ci.
Une PR qui touche le Pi ou la simulation attend `pi-gate` et
`simulation-gate`.

Les actions de checkout, d'installation de Node, de publication et de
récupération des artefacts utilisent le runtime Node.js 24, avec des commits
complets épinglés dans le workflow. Elles demandent un runner GitHub Actions au
moins en version 2.327.1 ; les jobs utilisent les runners hébergés
`ubuntu-24.04`, pas un runner local. Les entrées existantes restent inchangées :
aucune persistance des identifiants Git, LFS pour la CAO, historique complet
pour l'audit et cache npm explicite. Les artefacts publiés (`pi-gate-N` et
`simulation-gate-N`, N étant le numéro de tentative) gardent leurs chemins,
leur rétention de 14 jours et l'échec si aucun fichier attendu n'est produit ;
les fichiers cachés en restent exclus. Les artefacts intermédiaires
`simulation-evidence-*` font exception : ils portent la mesure de couverture de
chaque part, un fichier nommé `.coverage`, et sont donc publiés avec leurs
fichiers cachés. Ils ne contiennent que le dossier d'enregistrements créé par
le lanceur.

Les dépendances npm et Python sont mises en cache. Chaque exécution garde ses
artefacts pendant 14 jours. Les runs nocturnes et manuels ajoutent `--dsp` au
rapport synthétique. Le rejeu nocturne des scénarios réels s'activera lorsque
ANH-131 aura fourni les fichiers autorisés ; leur absence est signalée dans le
journal, jamais présentée comme un rejeu réussi.

Budgets d'exécution : 60 minutes pour `pi-gate` (dont, la nuit, 35 minutes au
plus pour l'étape d'endurance de l'enregistrement), 45 minutes pour chaque job de
la batterie de simulation, 90 minutes pour `simulation (report)` (le budget de
l'ancien job unique, gardé pour le rapport nocturne avec `--dsp`) et 15 minutes
pour `simulation-gate`. Ces budgets concernent les jobs CI, pas les délais de
sûreté du moteur. Aucun scénario, seuil de couverture ou contrôle n'est
retiré ; la génération et la publication du rapport restent obligatoires :
`simulation-gate` échoue si `simulation (report)` échoue.

L'audit Python résout d'abord les dépendances transitives pour Python 3.12 avec
`uv pip compile --generate-hashes`, puis audite cette liste entièrement épinglée
avec `--disable-pip --require-hashes`. Les dépendances transitives restent donc
incluses. Cela évite un second environnement pip temporaire non utilisé par les
gates. La liste d'audit résolue n'est pas un verrou de release.

`.gitleaksignore` conserve deux empreintes historiques vérifiées :
`YOUR_API_KEY` dans un exemple README et `invalid-key` dans un test HTTP refusé.
La clé publiable synthétique du build CI (`ci-fixture.clerk.accounts.dev`) est
traitée dans `.gitleaks.toml`, qui hérite de toutes les règles par défaut.
Seule sa valeur exacte, au chemin exact `.github/workflows/ci.yml`, est admise
par la règle `generic-api-key` ; les deux conditions doivent correspondre.
Cette exception ne dépend pas d'un SHA ou d'un numéro de ligne : elle survit
à une fusion squash. Le test `scripts/ci/gitleaks-fixture.test.mjs` exerce le
vrai scanner épinglé avant le scan de tout l'historique. Il couvre la fusion
squash, le déplacement de ligne et le refus d'une autre valeur, d'un autre
chemin ou d'un secret voisin ; les autres détecteurs restent actifs.
Ces faux positifs ne sont pas des secrets réels. Aucun audit n'est désactivé.

### Gate Pi en parallèle (ANH-72)

L'exigence EX-5 d'ANH-72 demande une gate Pi sous 25 minutes en CI. En série,
le job `pi-gate` prenait 30 min 38 s (run 37330679190 du 5 octobre 2026), dont
29 min 55 s de pytest : ruff, basedpyright et mypy ne pèsent que 22 s à eux
trois. Un seul test paramétré,
`test_a_drive_fault_at_speed_ends_the_session_with_its_mnemonic` (67 cas
d'environ 10 s chacun), représente 39 % de ce temps.

Le job lance toujours `bash raspberry-pi/scripts/check.sh`, qui reste la seule
définition de la gate. Il lui passe `PI_GATE_PROCESSES`, égal au nombre de CPU
du runner : quatre sur les runners hébergés `ubuntu-24.04` (deux cœurs, deux
fils chacun). Avec cette variable, l'étape de tests de `check.sh` n'est plus un
seul pytest : `scripts/ci/pi_gate_parallel.py` lance autant de processus
`python -m pytest` indépendants, sans aucune dépendance supplémentaire. Sans
la variable, `check.sh` se comporte exactement comme avant.

Chaque processus collecte toute la suite, comme en série, puis ne garde que
les tests dont le rang dans l'ordre de collecte lui revient : un sur quatre
avec quatre processus (`scripts/ci/pi_gate_shard.py`). Les 67 cas lourds, qui
se suivent, sont ainsi distribués à tour de rôle entre tous les processus.
Chaque processus mesure sa propre couverture.

La gate ne passe que si ces trois vérifications réussissent :

* **chaque processus se termine de lui-même avec le code 0.** Un test en
  échec, une erreur de collecte, un processus tué en plein test ou un plantage
  pendant l'arrêt de l'interpréteur, après le dernier test, font échouer la
  gate. Un processus qui a fini ses tests mais ne s'arrête pas (fil non démon
  resté bloqué, bibliothèque native) est tué au bout de 300 s et fait échouer
  la gate avec un message. En série, pytest ne rendrait jamais la main et le
  job mourrait à sa limite de 60 minutes. Ces 300 s ne commencent qu'après le
  dernier test et l'enregistrement de la couverture : il ne reste alors que
  l'arrêt de l'interpréteur, qui prend moins d'une seconde quand rien n'est
  bloqué. Un processus bloqué avant la fin de ses tests n'est pas chronométré :
  comme en série, le job attend sa propre limite ;
* **la preuve de partition.** Chaque processus écrit la liste des tests qu'il
  a collectés et celle des tests qu'il a exécutés. La gate exige que toutes
  les collectes soient identiques, identifiant par identifiant, et que chaque
  test collecté ait été exécuté par un processus et un seul. Cette
  vérification ignore la règle de partage : elle ne compare que les listes. Le
  journal l'affiche (`[gate] partition proven`), avec le nombre de tests de
  chaque processus et le total des verdicts ;
* **le seuil de couverture, une seule fois, sur les seules mesures de cette
  exécution.** Le lanceur réunit lui-même la mesure de chaque processus, une
  par processus : une mesure absente, illisible ou vide fait échouer la gate,
  là où `coverage combine` se contenterait d'un avertissement. Le total est
  écrit dans un fichier privé, seul dans un dossier créé pour l'exécution, et
  le journal affiche `[gate] coverage data merged from 4 of 4 processes`.
  `coverage report --fail-under=100` s'applique ensuite à ce fichier, avec la
  même liste `include` de `pyproject.toml` et la même fonction de décision
  que `pytest --cov-fail-under=100`. Ce fichier privé compte : `coverage
  report` fusionne d'abord tout fichier `.coverage.*` voisin de son fichier de
  données, si bien qu'un fichier resté dans `raspberry-pi/` après une
  exécution interrompue aurait pu combler un vrai trou. Il n'est plus lu.

`check.sh` exécute d'abord les tests du lanceur lui-même
(`scripts/ci/test_pi_gate_parallel.py`). Sur un projet jetable, ils vérifient
qu'un test en échec, un trou dans la couverture (avec ou sans mesure périmée
dans le dossier), un processus tué à l'arrêt de l'interpréteur, un processus
qui ne s'arrête pas, un processus sans enregistrement ou sans mesure
utilisable et des identifiants différents d'un processus à l'autre font bien
échouer la gate. Ils vérifient aussi que les tests voient le même
environnement et le même chemin d'import qu'en série (voir plus bas). Dans les
deux modes, `check.sh` soumet aussi le lanceur, son
greffon et leurs tests aux quatre vérificateurs statiques du Pi, avec les
règles de `raspberry-pi/pyproject.toml` (`scripts/ci/pyproject.toml` ne fait
qu'y renvoyer) ; le fichier de tests reçoit les exemptions de
`raspberry-pi/tests/**`.

Ce qui reste identique à la gate en série : les tests collectés (aucun filtre
ni marqueur n'est ajouté ; dans le résumé de chaque processus, `deselected`
compte les tests confiés aux autres processus), la mesure de branches, le
seuil, ruff, basedpyright et mypy avant les tests, les réglages Hypothesis,
les variables d'environnement et le chemin d'import vus par les tests. Ce qui
diffère : l'ordre et le voisinage des tests dans chaque processus,
l'occupation de tous les CPU pendant les tests, la ligne de commande de
pytest, sa sortie (un tube lu par le lanceur, qui préfixe chaque ligne par
`[p0]` à `[p3]`) et l'absence de `.pytest_cache`, que plusieurs processus
écraseraient et qu'aucun test ne lit.

**Le lanceur ne laisse rien dans l'environnement des tests.** Une variable
ajoutée à un processus pytest est héritée par tout processus qu'un test
démarre, et peut en changer le comportement. La première version exportait
`PYTHONUNBUFFERED=1` pour afficher la sortie plus tôt. Avec cette variable,
`print("OPEN", flush=True)` écrit `OPEN` puis le saut de ligne en deux appels
système au lieu d'un : les tests de verrou du variateur (ANH-74), qui lisent
la ligne `OPEN` de leur processus auxiliaire en une seule lecture, ont échoué
6 fois sur 18 en CI (`assert b'OPEN' == b'OPEN\n'`, run 37384448146). Depuis,
le lanceur transmet ses réglages au greffon par des options de ligne de
commande (`--pi-gate-share`, `--pi-gate-evidence`). Il ne peut pas démarrer
pytest sans deux variables : `PYTHONPATH`, pour que le greffon soit trouvé, et
`COVERAGE_FILE`, lu une fois par pytest-cov au démarrage de la mesure. Le
greffon leur rend la valeur qu'elles avaient, ou les retire, et retire son
dossier de `sys.path`, avant l'import du premier `conftest.py`. La sortie
reste lisible au fil de l'eau : pytest la vide lui-même après chaque test.
Une conséquence à connaître : la couverture n'est mesurée que dans les
processus pytest, comme aujourd'hui en série. Si la mesure des sous-processus
était activée un jour (`patch = ["subprocess"]` dans la configuration de
coverage), leurs mesures ne rejoindraient pas le total du lanceur : la gate
échouerait par manque de couverture, sans jamais passer à tort, et le lanceur
serait à adapter.

Deux limites à connaître :

* **la preuve de partition est relative.** Elle compare les processus entre
  eux, pas avec une collecte en série. Un test que tous les processus écartent
  de la même façon (un `-k` ou un `-m` dans `PYTEST_ADDOPTS`, un test marqué
  `hardware`) passe inaperçu, exactement comme en série : rien ne fixe le
  nombre de tests, seul le seuil de couverture rattrape un test manquant ;
* **le voisinage des tests dépend du nombre de CPU.** Avec N processus,
  chacun exécute un test sur N. Un test qui n'échoue qu'à côté de certains
  voisins peut échouer avec quatre processus et passer avec deux ou en série.
  Pour reproduire un échec de CI, reprendre le nombre affiché par la ligne
  `Runner:` du journal.

La durée se lit dans chaque journal : la ligne `Runner:` donne le nombre de
CPU et le modèle du processeur, chaque processus affiche son résumé
(`[p0] ... passed ... in ...s`) et ses 25 tests les plus lents
(`--durations=25`). Elle dépend d'abord du processeur attribué au runner, qui
change d'un job à l'autre. Les mesures de référence sont consignées dans la
PR #7.

Trois règles gardent la gate stable. Les enfreindre fait le plus souvent
échouer un test sans raison. Un état partagé entre deux tests peut aussi
satisfaire une assertion à tort, en série comme en parallèle : la gate ne le
détecte pas, d'où la deuxième règle.

* **identifiants stables.** L'identifiant d'un cas paramétré ne doit contenir
  ni adresse mémoire (`ids=str` sur une lambda) ni ordre dépendant du hachage.
  Sinon les processus ne collectent pas les mêmes identifiants et la gate
  échoue (`did not collect the same tests`) ;
* **isolement.** Fichiers sous `tmp_path`, aucun chemin ni port fixe partagé
  entre deux tests, aucun état laissé au test suivant : l'ordre et le
  voisinage des tests ne sont plus ceux de la série. Exception connue : deux
  tests ouvrent réellement le port fixe 8099,
  `tests/test_web_api.py::test_the_server_serves_and_stops_without_touching_the_signal_handlers`
  et `tests/test_local_panel.py::test_the_production_web_runner_binds_and_exits`.
  Ils sont nommés dans `SAME_PROCESS` (`scripts/ci/pi_gate_shard.py`) et vont
  toujours dans le même processus, qui les exécute l'un après l'autre. Si
  l'un d'eux est renommé, la gate échoue jusqu'à la mise à jour de cette
  liste. Deux gates lancées en même temps sur une même machine peuvent
  toujours se disputer ce port ;
* **temps réel.** Une borne mesurée en temps réel garde une marge large : tous
  les CPU du runner sont occupés pendant toute la durée des tests.

En local, `check.sh` reste en série par défaut, et `check.ps1` n'a pas ce
mode. Pour reproduire le job avec ses quatre processus :

```sh
PI_GATE_PROCESSES=4 bash raspberry-pi/scripts/check.sh
```

Hors CI, Hypothesis garde son profil par défaut et sa limite de 200 ms par
exemple : sous cette charge, un test par propriétés sans `deadline=None` peut
échouer. La CI utilise le profil `ci`, sans limite de temps.

### Gate de simulation répartie (ANH-184)

En un seul job, `simulation-gate` prenait de 49 à 66 minutes (runs 37502103415
et 37516988940 du 6 octobre 2026 sur `develop`). Sur le second : 13 s de ruff,
basedpyright et mypy, **59 min 50 s de pytest** (1023 tests passés, 1 xfail),
puis 5 min 6 s pour le rapport `simulation.quick --all`. Tout le temps est dans
la batterie, exécutée par un seul processus sur un runner de quatre CPU.
Répartie comme décrit ci-dessous, la gate rend son verdict 13 min 10 s après
le lancement du run (run 37542166047 ; le détail par job est dans le tableau
des durées, au début de cette section 15).

La batterie est maintenant coupée en 13 parts. Le lanceur et le greffon sont
ceux de la gate Pi (`scripts/ci/pi_gate_parallel.py` et
`scripts/ci/pi_gate_shard.py`, voir la section précédente) : chaque part est un
processus pytest indépendant qui collecte toute la suite, n'exécute que ses
tests, écrit ce qu'il a collecté et exécuté, et mesure sa propre couverture.
Deux choses changent par rapport à la gate Pi : les parts sont exécutées par
plusieurs jobs, et la règle de partage est celle de la simulation.

| Job | Parts | Ce qu'il exécute |
|---|---|---|
| `simulation (cohort)` | 0 | `tests/test_cohort.py` en entier, seul sur son runner |
| `simulation (battery 1)` à `simulation (battery 3)` | 1 à 4, 5 à 8, 9 à 12 | le reste de la batterie, quatre processus par runner |
| `simulation (report)` | aucune | le rapport `simulation.quick --all` |
| `simulation-gate` | aucune | le verdict sur les 13 parts |

`simulation/scripts/check.sh` reste la définition de la gate. Deux variables
d'environnement choisissent l'étape :

* `SIMULATION_GATE_SHARES=5-8/13` avec `SIMULATION_GATE_EVIDENCE=<dossier>` :
  ruff, basedpyright et mypy comme d'habitude, puis seulement les parts 5 à 8
  d'une batterie coupée en 13 (`pi_gate_parallel.py --suite simulation
  --shares 5-8/13 --evidence <dossier>`). L'étape échoue si un processus
  plante, même à l'arrêt de l'interpréteur, s'il ne s'arrête pas 300 s après
  son dernier test, si un test échoue, si un test a tourné deux fois parmi ces
  parts, ou si un enregistrement ou une mesure de couverture manque. Elle ne
  dit rien des autres parts : ni preuve de partition, ni seuil ;
* `SIMULATION_GATE_COMBINE=13` avec le même dossier, que le job
  `simulation-gate` remplit avec les artefacts des quatre jobs de la
  batterie : aucun test de la batterie n'est relancé. `check.sh` exécute les
  tests du lanceur lui-même, puis `pi_gate_parallel.py --combine 13` : la
  preuve de partition sur les 13 parts (collectes identiques, chaque test
  exécuté par une part et une seule), la fusion des 13 mesures (une mesure
  absente, illisible ou vide est une erreur) et `coverage report
  --fail-under=100`, une seule fois, sur le total.

Le job `simulation-gate` exige d'abord que les cinq jobs dont il dépend aient
réussi : les codes de sortie des processus sont jugés là où ils ont tourné. Il
juge ensuite les enregistrements même si l'un de ces jobs a échoué, pour
montrer tous les dégâts d'un coup, et reste alors en échec. Les
enregistrements d'un test en échec font de toute façon échouer `--combine`.

Cette première étape tourne quoi qu'il soit arrivé au run, annulation
comprise (`always()` sur le job et sur l'étape). Un job de la batterie annulé,
en échec, arrêté par sa limite de temps ou sauté alors qu'il ne devait pas
l'être laisse donc `simulation-gate` en échec. Avec `!cancelled()`, comme sur
les autres gates, un run annulé pendant la batterie aurait sauté ce job, et
GitHub compte un job sauté comme réussi pour une vérification obligatoire.
Sur un run annulé, les étapes suivantes (récupération des artefacts, verdict)
ne tournent pas.

Les tests du lanceur (`scripts/ci/test_pi_gate_parallel.py`) couvrent ce mode
sur un projet jetable. Des parts exécutées par des appels séparés sont
prouvées ensemble, dans n'importe quel ordre. Font échouer l'appel concerné
ou le verdict : une part qu'aucun appel n'a exécutée, deux appels qui n'ont
pas coupé la suite de la même façon (un test deux fois, un autre jamais), un
test en échec, un processus tué à l'arrêt de l'interpréteur, un processus qui
ne s'arrête pas, une mesure manquante, un trou de couverture, et un
enregistrement resté d'un appel précédent dans le même dossier. Une batterie
jetable vérifie aussi que chaque exécution partagée n'est faite qu'une fois
et que la cohorte a son processus.

**La règle de partage de la simulation** (`simulation_owners` dans
`pi_gate_shard.py`) ne sert qu'à gagner du temps : la preuve de partition ne
la connaît pas et vaut quelle que soit la règle. Les tests de la simulation
réutilisent des exécutions que chaque processus garde en mémoire. Distribués
un par un, comme ceux du Pi, ils referaient ces exécutions dans chaque
processus.

* `tests/test_cohort.py` va en entier à la part 0, qui ne reçoit rien d'autre.
  Le premier test de cohorte qui a besoin d'un résultat lance toute la cohorte
  (90 séances) sur tous les CPU de la machine ; les 180 tests paramétrés
  lisent ensuite ce résultat. La part 0 a donc un runner pour elle seule. Le
  fichier est nommé dans `ALONE_IN_PROCESS_ZERO` : si plus aucun test n'en est
  collecté, la gate échoue jusqu'à la mise à jour de cette liste ;
* ailleurs, les tests d'un même fichier qui portent le même identifiant de
  paramètre vont à la même part : les trois tests de `test_battery.py` sur un
  scénario, les deux tests de `test_failures.py` sur un cas. L'exécution
  qu'ils partagent n'est faite qu'une fois ;
* chaque groupe va à la part qui a le moins de travail jusque-là, parmi les
  parts 1 à 12 : d'abord les groupes les plus lents, du plus lent au moins
  lent, puis les autres dans l'ordre de collecte. Les groupes lents sont
  nommés dans `SLOW_SECONDS`, avec leur durée mesurée en CI : les quatre
  scénarios `dsp` de la batterie, les deux tests de `test_quick.py` qui
  passent par le vrai traitement du signal, les six cas `ecg_dsp_*` de la
  matrice de pannes et trois tests par propriétés. À eux quinze, ils pèsent
  autant que tout le reste. Distribués à tour de rôle, comme dans la première
  version, ils laissaient une part avec 15 minutes de travail et une autre
  avec 3 (run 37539316521).

Limites à connaître :

* les exécutions partagées entre fichiers (`run_file` de `conftest.py`, appelé
  par plusieurs fichiers de tests) peuvent être refaites dans plusieurs parts ;
* `SLOW_SECONDS` ne sert qu'à équilibrer. Un nom qui n'est plus collecté est
  ignoré ; un nouveau test lent qui n'y figure pas est distribué comme les
  autres. Dans les deux cas la gate reste juste et devient moins équilibrée.
  Le signe se lit dans le journal : chaque processus affiche sa durée et ses
  15 tests les plus lents (`--durations=15`). Pour rééquilibrer, reporter ces
  durées dans la table ;
* un nouveau test qui calculerait un résultat pour tout un fichier, comme la
  cohorte, ralentirait la gate sans la fausser, et se lirait au même endroit ;
* le nombre de parts est écrit dans le workflow (`SIMULATION_SHARES`), pas
  déduit du nombre de CPU : le voisinage des tests ne dépend pas du runner.
  Si une part manque, ou si deux jobs n'ont pas coupé la batterie de la même
  façon, la preuve de partition échoue ;
* le test de reproductibilité CAO est « skipped » sans l'extracteur. Chaque
  job de la batterie installe donc l'extracteur : aucun ne sait d'avance
  lequel recevra ce test ;
* les limites de la gate Pi valent ici aussi : la preuve de partition compare
  les processus entre eux, pas avec une collecte en série, et les règles
  d'identifiants stables et d'isolement des tests s'appliquent ;
* relancer un job de la batterie (« Re-run failed jobs ») remplace son
  artefact : `simulation-gate` juge la dernière exécution de chaque job.
  Relancer `simulation-gate` seul après l'expiration des artefacts (14 jours)
  échoue : il faut alors tout relancer.

Pour reproduire la CI sur une seule machine, avec le même découpage :

```sh
SIMULATION_GATE_SHARES=0-12/13 SIMULATION_GATE_EVIDENCE=/tmp/parts bash simulation/scripts/check.sh
SIMULATION_GATE_COMBINE=13 SIMULATION_GATE_EVIDENCE=/tmp/parts bash simulation/scripts/check.sh
```

Pour une seule part en échec, la 7 par exemple : `SIMULATION_GATE_SHARES=7-7/13`.

### Gates lancées selon les fichiers changés (ANH-184)

Sur une PR, le job `changes` liste les fichiers que la PR change et les donne
à `scripts/ci/gates-for-changes.mjs`. La liste vient de `git diff --name-only
--no-renames` entre la branche de base et le commit de fusion que la CI
teste : un renommage compte sous ses deux noms, une suppression comme une
modification. Le script répond deux choses : `python` pour `pi-gate` et
`simulation-gate`, `node` pour `web` et `convex-tests`. `docs` et `audit`
tournent toujours.

Une gate n'est sautée que si **tous** les fichiers changés sont d'une sorte
listée comme ne pouvant pas l'affecter :

| Sorte | Fichiers | `pi-gate`, `simulation-gate` | `web`, `convex-tests` |
|---|---|---|---|
| documentation | tout fichier `.md`, sauf `CHANGELOG.md` (voir plus bas) ; sous `docs/`, les fichiers `.md`, `.png`, `.jpg`, `.jpeg`, `.gif`, `.svg`, `.webp` et `.pdf` | sautées | sautées |
| site | `app/`, `components/`, `hooks/`, `i18n/`, `lib/`, `messages/`, `public/` ; à la racine : `next.config.ts`, `proxy.ts`, `tsconfig.json`, `eslint.config.mjs`, `postcss.config.mjs`, `components.json` et les configurations Vitest `vitest.<suite>.config.mts` | sautées | lancées |
| Convex | `convex/` | sautées | lancées |
| Python | sous `raspberry-pi/` ou `simulation/`, les fichiers `.py` et `.pyi`, et eux seuls | lancées | sautées |

Passent avant ces listes et lancent tout :

* `.github/` et `scripts/ci/` ;
* les manifestes et verrous de dépendances : `package.json`,
  `package-lock.json`, `bun.lock`, `requirements.txt` et `pyproject.toml` à la
  racine, ainsi que les `requirements*.txt` et `pyproject.toml` de
  `raspberry-pi/`, `simulation/` et `deploy/` ;
* les fichiers de la console testés des deux côtés :
  `raspberry-pi/src/web/static/` et `raspberry-pi/tests/web/`, lus par la gate
  Pi et par `web` ;
* `contracts/` : ce sur quoi le Pi et le cloud s'accordent, quelle que soit
  l'extension du fichier. Chaque côté a ses propres tests contre ce contrat ;
* `CHANGELOG.md`, où qu'il soit : c'est du Markdown, mais l'outillage de
  release et ses tests le lisent. Il lance tout tant qu'on ne sait pas quelle
  gate est seule à le lire.

Lancent tout aussi : un fichier d'aucune sorte listée (un scénario JSON,
`raspberry-pi/config/`, un Dockerfile, la CAO, `deploy/`, un fichier de
configuration à la racine), une PR sans aucun fichier changé, un chemin que le
script ne sait pas lire, et tout événement qui n'est pas une PR.

**Chaque push sur `develop` ou `main`, l'exécution nocturne et les lancements
manuels lancent toutes les gates sans consulter la règle** : la condition des
jobs ne regarde la réponse de `changes` que pour l'événement `pull_request`.
Une erreur de classement est donc vue au plus tard à la fusion.

Une gate sautée apparaît « Skipped » dans la PR. GitHub compte un job sauté
par sa propre condition comme réussi pour une vérification obligatoire
(documentation GitHub, « Handling skipped but required checks »). Quatre
précautions du workflow en dépendent ; `scripts/ci/ci-workflow.test.mjs` les
vérifie à chaque exécution :

* la condition de chaque gate est `!cancelled() && (github.event_name !=
  'pull_request' || needs.changes.outputs.<réponse> != 'false')`. Si
  `changes` échoue ou ne répond rien, la gate tourne. Sans `!cancelled()`,
  GitHub sauterait une gate dont la dépendance a échoué et la compterait
  réussie ;
* `simulation-gate`, qui attend d'autres jobs, porte `always()` à la place de
  `!cancelled()` : un run annulé le laisse en échec, jamais sauté (voir
  [Gate de simulation répartie](#gate-de-simulation-répartie-anh-184)). Le
  test exécute l'étape telle qu'elle est écrite dans le workflow, pour chaque
  façon dont un job attendu peut finir ;
* chaque nom exigé est celui d'un job simple, jamais d'une matrice : un job
  de matrice sauté est rapporté sous un autre nom, et la vérification
  resterait en attente ;
* le workflow n'a aucun filtre `paths` : un workflow non déclenché laisse ses
  vérifications obligatoires en attente.

Limites à connaître :

* les listes décrivent ce que les gates lisent aujourd'hui. Un test Python qui
  ouvrirait un fichier de `docs/` ou du site, ou un module du site qui
  importerait un fichier Python, rendrait une liste fausse sans toucher au
  script. Le passage complet sur `develop` et la nuit est le filet ;
* le script et le workflow appliqués sont ceux de la PR elle-même. Une PR qui
  les modifie lance tout, mais selon sa propre version : toute modification de
  `.github/` ou de `scripts/ci/` doit être relue ;
* un run annulé pendant les quelques secondes du job `changes` laisse
  `pi-gate`, `web` et `convex-tests` « Skipped » sur ce commit, sans que la
  règle l'ait décidé : leur condition, `!cancelled()`, est alors fausse.
  `always()` ne leur convient pas : chaque run annulé par un nouveau push
  lancerait quand même ces gates jusqu'au bout. `audit`, lancé en même temps
  que `changes`, est alors annulé lui aussi et bloque la fusion ; relancer le
  run relance tout. Une fois démarrée, une gate annulée est rapportée
  « cancelled », ce qui bloque ;
* avec `always()`, le job `simulation-gate` d'un run annulé demande encore un
  runner, quelques secondes, pour échouer. Ce coût est payé à chaque run
  annulé, y compris quand un nouveau push sur la PR annule le run précédent ;
* la règle ne lit que les noms de fichiers, pas leur contenu : un fichier
  `.md` ne lance aucune gate lourde, même sous `raspberry-pi/` ;
* `convex/` et le Pi partagent un contrat (routes HTTP, format
  d'enregistrement) qu'aucune gate ne teste des deux côtés à la fois. Sauter
  `pi-gate` sur une PR Convex ne retire donc aucun contrôle existant, et n'en
  ajoute aucun.

Le journal du job `changes` donne la raison, par exemple `path rule: python
gates run: raspberry-pi/src/units.py (python)` ou `path rule: python gates
skipped: none of the 3 changed files can affect them`.

### Analyse statique externe : CodeQL (ANH-196)

Un workflow séparé de `ci.yml`, `.github/workflows/codeql.yml`, fait analyser
le dépôt par CodeQL, l'outil d'analyse de sécurité de GitHub, en plus des
gates. **Ce n'est pas une gate** : aucun de ses jobs n'est une vérification
obligatoire, et il ne remplace ni ruff, ni basedpyright, ni mypy, ni ESLint, ni
l'audit des dépendances.

| | CodeQL |
|---|---|
| Ce qui est cherché | des failles de sécurité, avec la suite de requêtes par défaut de GitHub |
| Ce qui est lu | les sources Python et JavaScript/TypeScript, tests compris, et les workflows GitHub Actions du dépôt |
| Périmètre réglé dans | `.github/codeql/codeql-config.yml` |
| Quand | PR vers `develop` ou `main`, push sur ces branches, chaque lundi à 04:37 UTC, à la demande |
| Jobs | `codeql (python)`, `codeql (javascript-typescript)` et `codeql (actions)` |
| Où lire les résultats | onglet **Security → Code scanning** du dépôt |
| Compte ou secret | aucun |

**SonarQube Cloud : envisagé, non retenu.** Le ticket prévoyait aussi une
analyse par SonarQube Cloud. Le chef de projet a décidé le 7 octobre 2026 de
ne garder que CodeQL : avec l'offre gratuite pour dépôt public, le tableau de
bord de SonarQube Cloud est public, constats de sécurité compris, alors que les
résultats de CodeQL ne sont lisibles que par les personnes qui ont accès au
dépôt. Le dépôt ne contient donc ni workflow, ni configuration, ni secret pour
SonarQube Cloud.

**Périmètre.** CodeQL laisse de côté ce qui est généré, installé, ou n'est pas
du code : `convex/_generated`, `node_modules`, les environnements virtuels
(`.venv`, `.venv-*`, `venv`), `.next`, `out`, `build`, `simulation/out` et
`CAO/`. Tout le reste est analysé : un nouveau dossier l'est sans avoir à être
déclaré. Le workflow n'installe aucune dépendance et n'exécute aucun code du
dépôt.

**Jobs et permissions.** Un job par langage, sans compilation
(`build-mode: none`). Seul ce job reçoit la permission
`security-events: write`, qui sert à publier les résultats ; aucun autre job du
dépôt n'a de permission d'écriture, et le workflow ne lit aucun secret. Les
actions sont épinglées par commit complet, celle de checkout sur le même commit
que dans `ci.yml`. Durées mesurées sur la PR #35 (run 37594242883) :
1 min 10 s pour `codeql (javascript-typescript)`, 1 min 49 s pour
`codeql (python)`.

**Lire les résultats.** Sur une PR, CodeQL ne rapporte que ce qui se trouve
dans les lignes que la PR change. GitHub ajoute alors sa propre vérification,
nommée « CodeQL », qui le résume (« No new alerts in code changed by this pull
request » sur la PR #35). Elle n'est pas obligatoire non plus ; d'après la
documentation de GitHub, elle échoue quand la PR introduit une alerte de
sévérité élevée, seuil réglable dans les réglages du dépôt. L'état de tout le
dépôt vient des analyses complètes : chaque push sur `develop` ou `main`, le
passage hebdomadaire et les lancements manuels. Les résultats se lisent dans
**Security → Code scanning**, en choisissant la branche (`develop`) ou la PR
dans les filtres : la vue par défaut montre la branche par défaut du dépôt,
`main`, qui n'est analysée qu'une fois le workflow fusionné dans `main`. Il en
va de même du passage hebdomadaire : comme le déclenchement nocturne de
`ci.yml`, GitHub ne le lance que depuis la branche par défaut.

**Traiter un constat.** Un constat est corrigé dans le code quand la donnée
qu'il suit peut venir d'un tiers, ou quand la correction est simple et rend le
code sûr de façon évidente. Sinon il est classé dans GitHub (**Dismiss alert**)
avec sa raison écrite : la liste des constats ouverts ne doit contenir que ce
qui reste à traiter. `paths-ignore`, dans `.github/codeql/codeql-config.yml`,
ne sert pas à écarter un constat : il reste réservé au code généré ou installé.
Pour un nom de fichier reçu de l'extérieur, la forme que l'analyse reconnaît est
celle de `resolve_under`
([`src/record/containment.py`](../raspberry-pi/src/record/containment.py)) :
normaliser le chemin, vérifier qu'il commence par le dossier permis, puis
n'utiliser que le chemin normalisé.

**Ce que cette analyse bloque.** Rien dans une PR : les jobs `codeql (...)` ne
sont pas dans la protection de branche de `develop`. Les rendre obligatoires
est un réglage du dépôt, décidé par le chef de projet. Un job CodeQL n'échoue
pas parce qu'il trouve quelque chose : il n'échoue que si l'analyse elle-même
échoue. Le workflow n'a aucun filtre `paths` : s'il devient obligatoire un
jour, un workflow non déclenché laisserait ses vérifications en attente. Aucun
de ses jobs ne peut porter le nom d'un job de `ci.yml`.

Une exception à connaître avant une release. `scripts/release.sh`
([release.md](release.md#4-ce-que-fait-le-script)) ne lit pas la protection de
branche : sa fonction `require_green` lit **toutes** les vérifications (check
runs) du commit, obligatoires ou non, et refuse si l'une n'est pas terminée ou
s'est terminée autrement que réussie, ignorée ou neutre. `prepare` et `pr`
l'appliquent au commit de tête de `develop`, `tag` au commit de tête de `main`.
Les jobs `codeql (...)` sont posés sur ces deux commits par les pushs, et sur
la tête de `main` par le passage hebdomadaire. Conséquences :

* un job `codeql (...)` en échec, annulé ou encore en cours sur la tête de
  `develop` fait refuser `prepare` et `pr` ; sur la tête de `main`, il fait
  refuser `tag`. Attendre la fin de l'analyse, ou relancer le job s'il a échoué
  sur une panne ;
* un job CodeQL qui a trouvé quelque chose reste réussi : il ne bloque pas le
  script ;
* tant qu'une PR de release est ouverte, sa tête est la tête de `develop` : la
  vérification « CodeQL » que GitHub y pose est un check run comme un autre, et
  le script la lit aussi. Rouge, elle fait refuser `prepare` et `pr`.

**Tests.** `scripts/ci/analysis-workflows.test.mjs` est lancé par le job
`changes` de `ci.yml` à chaque exécution, avec les deux autres fichiers de test
de la CI (`node --test`, sans installation). Il vérifie ce qu'une modification
pourrait casser sans qu'aucun job ne rougisse : actions épinglées par commit
complet (celle de checkout sur le même commit que `ci.yml`), une seule
permission d'écriture dans tous les workflows du dépôt, déclencheurs, langages,
noms des jobs, et le périmètre confronté aux fichiers suivis par Git (seuls le
code Convex généré et le fichier de CAO sont laissés de côté).

**Réglage du dépôt à ne pas toucher.** Ne pas activer le « Default setup » de
CodeQL (**Settings → Code security**) : GitHub refuserait alors les résultats
envoyés par ce workflow. C'est la seule précaution manuelle ; CodeQL ne demande
ni compte, ni application, ni secret.

**Limites.**

* Le workflow ne se déclenche que pour les PR vers `develop` ou `main`, comme
  `ci.yml` : une PR empilée sur une autre branche n'est analysée qu'une fois
  redirigée vers `develop`.
* Tant que le workflow n'est pas sur `main`, le passage hebdomadaire ne tourne
  pas et la vue par défaut de **Code scanning** reste vide.
* Le tri et la correction de ce que CodeQL remonte ne font pas partie de ce
  ticket.

### Lire un échec et relancer

Dans la PR, ouvrir **Checks**, puis le job rouge et la première étape en échec.
Les gates continuent après une erreur afin de montrer tous les contrôles cassés.
Pour la couverture, télécharger l'artefact et lire les lignes/branches manquantes
avec le rapport terminal ; ne pas baisser le seuil. Pour le rapport simulation,
ouvrir `report.html` et retrouver le scénario par son identifiant. Une entrée
`XFAIL` reste une anomalie connue, pas une réussite de sécurité.

Reproduire avec la commande du job, corriger, puis pousser un nouveau commit.
**Re-run failed jobs** convient seulement à une panne de runner/réseau : il ne
change pas le code. Pour les nouvelles PR, un avis indépendant lié au SHA
courant est requis (`agent-review/R1`), selon la décision utilisateur du
5 octobre 2026. Les autres checks restent obligatoires, sans contournement
administrateur. La PR ANH-71 avait deux avis : son historique de revue reste
inchangé. Un check d'agent n'est pas une approbation humaine fictive.

Un test de la gate Pi qui échoue en parallèle et passe en série révèle un
défaut d'isolement ou une borne de temps trop serrée : le signaler et le
corriger, ne pas relancer jusqu'au vert. Il en va de même pour un test de la
batterie de simulation qui échoue dans sa part et passe en série.

Quand `simulation-gate` est rouge, sa première étape dit lequel de ses jobs a
échoué : ouvrir d'abord ce job (`simulation (battery 2)` par exemple), dont
chaque ligne de sortie est préfixée par le numéro de la part (`[p5]` à
`[p8]`). Si tous ont réussi, l'échec vient du verdict lui-même : lire les
lignes `[gate]` de l'étape « Simulation gate, every test once and combined
coverage ».

Une vérification marquée « Skipped » n'a pas tourné. Le plus souvent, la règle
de chemins a jugé qu'aucun fichier de la PR ne pouvait l'affecter : le job
`changes` a alors réussi et en donne la raison. Si `changes` est lui-même
annulé ou absent, le run a été annulé dans ses premières secondes et rien n'a
été jugé : relancer le run.

Un job `codeql (...)` rouge signale une panne de l'analyse (service, réseau,
configuration), pas un constat de l'outil : lire le journal du job. Les
constats se lisent dans **Security → Code scanning** et ne font pas rougir ce
job ; dans une PR, c'est la vérification « CodeQL » posée par GitHub qui les
signale (voir
[Analyse statique externe](#analyse-statique-externe--codeql-anh-196)).

### Tests unitaires du site

`npm run test:ecg` lance les tests de `lib/**/*.test.ts` (configuration
`vitest.ecg.config.mts`, environnement Node). Le nom du script date de la
bibliothèque ECG du navigateur que ces tests couvraient ; elle a été retirée
avec l'ancien mode d'enregistrement ECG, et la CI appelle toujours le script
sous ce nom.

Les règles des fenêtres du site et les bornes de fraîcheur ont leurs fichiers,
décrits dans les deux sections suivantes. Deux autres gardent le retrait de
l'ancien mode d'enregistrement ECG.

`lib/legacyModeReferences.test.ts` lit **tout le dépôt** (les fichiers texte
suivis par le gestionnaire de versions : Pi, Convex, site, documentation,
scripts) et échoue dès qu'un fichier nomme l'entrée ou les deux modules de
l'ancien enregistreur du Pi, une des routes machine de l'ancien mode, ou la
mutation qui créait ses séances. Sept emplacements font exception, listés dans
le test avec leur raison : la migration (`convex/migrations/`), l'historique
documenté (`docs/suivi-tickets.md`, `docs/reviews/`), et les quatre tests du
retrait, qui nomment ce qu'ils interdisent. Un nouvel emplacement s'ajoute dans
cette liste, avec sa raison, ou la référence se retire.

Limite : ce test lit de la documentation et du Python, mais tourne dans le job
`web`. Sur une PR qui ne change que du Markdown, ou que des fichiers Python,
la règle de chemins saute ce job
([Gates lancées selon les fichiers changés](#gates-lancées-selon-les-fichiers-changés-anh-184)) :
une référence réintroduite par une telle PR n'est vue qu'au push sur `develop`
ou à l'exécution nocturne, où toutes les gates tournent.

`lib/legacyRecordingRetired.test.ts` lit les sources du site (`app/`,
`components/`, `hooks/`, `lib/`, `i18n/`, hors tests) et les deux catalogues de
messages, et échoue si :

* une page appelle une mutation du module `sessions` ou nomme une des trois
  mutations retirées (création, fin, annulation d'une séance d'enregistrement) :
  le site ne crée une séance que par `training.launchAutoSession` ;
* un des fichiers de l'ancien bloc ECG existe ou est importé
  (`components/charts/`, `components/ECGWaveform.tsx`, `lib/ecg.ts`, `lib/ecg/`,
  `lib/generatePdf.ts`, `components/modals/SessionFormModal.tsx`) ;
* les lots ECG sont lus ailleurs que dans la carte d'historique du détail d'une
  séance (`ecgData.getSessionDataStats`) ;
* la fenêtre machine reparle de fréquence, de canaux ou d'intervalle ;
* les catalogues `fr` et `en` n'ont pas les mêmes clés, gardent une clé de
  l'ancien mode, ou n'ont pas une clé que les pages Sessions, détail, vue en
  direct, Rapports ou la fenêtre machine nomment.

Ce sont des lectures de texte : elles ne montent aucun composant et ne prouvent
pas le rendu dans un navigateur. Les deux autres couches ont leur propre test du
retrait : `raspberry-pi/tests/test_legacy_recorder_retired.py` (gate du Pi) et
`convex/legacyRecordingRetired.test.ts` (`npm run test:convex`).

### Règles des fenêtres du site, sans navigateur

`npm run test:ecg` exécute tous les fichiers `lib/**/*.test.ts`. En plus du
test du retrait de l'ancien mode ECG, il couvre donc les règles qu'une fenêtre
du site applique avant d'appeler Convex, extraites en fonctions pures dans
`lib/` pour être testées sans navigateur. Ces tests ne montent aucun composant : le parcours à l'écran
reste à prouver par la suite navigateur (ANH-83).

### Fraîcheur de l'état en direct (ANH-160)

`npm run test:site` (job `web` de la CI, `vitest.site.config.mts`) exécute les
tests de `hooks/` et de `components/`, sans navigateur. Avec les tests des
suites voisines, il couvre la fraîcheur de l'état en direct, le statut d'une
machine et son dernier signal. Dans tous ces tests, seule l'horloge du serveur
date une réception ; l'horloge du poste est mise en avance (15 s, 80 s,
10 min) ou en retard (15 s, 10 min), et le résultat attendu est le même.
L'horloge du Pi est décalée elle aussi : de 4 s, sans effet ; de 12 s, 25 s
ou 10 min, le panneau affiche le bandeau des mesures qui ne sont pas datées
de maintenant, et aucune valeur :

- `lib/server-clock.test.ts` (`npm run test:ecg`, comme tous les tests de
  `lib/`) : l'heure du serveur reconstituée à partir de `serverNow` et du temps
  compté, horloge du poste décalée, reculée, avancée, poste en veille, réponse
  déjà vue par un autre composant ;
- `lib/training.test.ts` (`npm run test:ecg`) fixe les bornes de `isFresh`
  (dont le refus d'une date du futur au-delà de la tolérance), celles de
  `datedAfterReception` (un point daté après sa propre réception), les deux
  seuils et le statut affiché ;
- `hooks/use-freshness.test.ts` monte `useFreshness` et `useFreshnessJudge`
  dans le vrai React avec une **horloge simulée** : l'état devient périmé 90 s
  après le dernier heartbeat sans qu'aucune donnée ne change, redevient frais
  à la réponse suivante, reste frais tant que les heartbeats arrivent, applique
  le seuil de 20 s de la télémétrie, n'est jamais frais sans `serverNow`, et
  arrête son horloge au démontage ;
- `components/training/live-freshness.test.tsx` rend la carte « État en
  direct », une carte de Mes machines et le panneau d'entraînement avec les
  vrais textes de `messages/`, à la réception d'une réponse puis 90 s (20 s
  pour le panneau) plus tard sans réponse nouvelle : badge, statut, valeurs
  grisées, bandeau, bouton de lancement, texte d'une machine passée hors ligne,
  et absence de bandeau pendant le chargement de la télémétrie. Pour le
  panneau, le bloc « points received late (every clock right) » rejoue le
  renvoi d'une file après une coupure : un paquet reçu à l'instant dont le
  point le plus récent a été mesuré il y a plus de 20 s n'est pas montré
  comme actuel, le bandeau tient pendant toute la vidange de la file et ne
  part qu'au premier point mesuré depuis moins de 20 s, y compris pour une
  séance démarrée à la console et enregistrée au retour de la liaison ;
- `components/machines/machine-signal.test.tsx` fait de même pour le badge de
  statut, le compteur « Machines en ligne », « Dernier signal » et la ligne des
  versions ;
- `components/freshness-hook.test.tsx` remplace le hook par un verdict imposé :
  chaque composant ci-dessus doit afficher ce que le hook dit, même quand ses
  données disent le contraire. Un composant qui calculerait l'âge lui-même,
  une fois au rendu, échoue ici ;
- `convex/liveFreshness.test.ts` (`npm run test:convex`) vérifie que le serveur
  juge sur le même seuil `LIVE_FRESH_MS` que le site, que chaque réponse
  concernée porte `serverNow`, que `lastSignalAt` est la réception du
  dernier point quelle que soit la date écrite par la machine, et que
  `lastMeasuredAt` est cette date écrite (paquet de 300 points renvoyé une
  heure après, file qui se vide paquet après paquet, séance enregistrée au
  retour de la liaison) ;
- `convex/offlineThreshold.test.ts` (`npm run test:convex`) remplace le seuil
  partagé par une autre valeur : la tâche `checkOfflineMachines` doit la
  suivre.

Limites. Le dépôt n'a pas de bibliothèque de test avec DOM : le hook tourne sur
un hôte minimal (un composant qui ne rend rien), et les composants sont rendus
en HTML statique, une fois à la réception d'une réponse et une fois plus tard.
Le passage de « En direct » à « Données périmées » **à l'écran**, sans
recharger la page, est donc prouvé en trois morceaux (le hook bascule à
l'horloge, chaque composant affiche ce que le hook dit, le rendu refait plus
tard affiche l'état périmé), pas d'un seul tenant. Le test de bout en bout
prévu (couper la console simulée, attendre 90 s simulées, lire le badge dans un
navigateur) attend l'infrastructure d'ANH-83. Les écarts d'horloge sont
simulés : aucun n'a été mesuré entre un vrai poste, le serveur et un Pi, et la
durée pendant laquelle Convex ressert une réponse de son cache n'est connue que
par la lecture de son code source.

### Retour des mutations du site (ANH-156)

`npm run test:site` (voir la section précédente) exécute aussi les tests du
retour des mutations, chacun une seule fois :

- `hooks/use-mutation-with-feedback.test.tsx` monte le hook sous le vrai
  fournisseur `next-intl` avec les vrais `messages/fr.json` et
  `messages/en.json` ; seul `useMutation` de Convex est remplacé. Il couvre le
  succès (texte fixe, ou composé à partir de la réponse de la mutation),
  l'erreur codée (traduite, ou texte du serveur sans traduction), l'erreur
  brute, l'erreur masquée par le serveur et le journal avec l'identifiant de
  requête ;
- `components/training/stop-feedback.test.tsx` rend le panneau d'entraînement,
  récupère le bouton de confirmation avec son gestionnaire (le bouton et la
  fenêtre sont remplacés, faute de DOM) et l'appelle : le message est le même
  que la page ait cru la séance en attente, active ou finie, y compris quand la
  machine arme la séance pendant l'envoi, et il n'affirme aucune issue ;
- `hooks/no-silent-mutation.test.ts` lit les sources de `app/` et
  `components/`, hors tests : il refuse un appel direct à `useMutation`, et
  dans un fichier qui appelle une mutation un `catch` vide ou réduit à des
  appels `console`. C'est une lecture de texte, pas une analyse du programme :
  elle ne suit pas une erreur avalée dans un autre fichier ;
- `lib/machineForm.test.ts` (`npm run test:ecg`, comme tous les tests de
  `lib/`) vérifie que l'étape « liste des gestionnaires » de la fenêtre machine
  rend la réponse de la mutation, donc un refus que le hook rapporte sans lever
  d'exception.

Ces tests ne montent aucune page : l'affichage réel du message et le scénario
de bout en bout (suppression refusée d'une machine en séance) restent à faire
dans le job navigateur d'ANH-83.

### Infrastructure encore dépendante d'autres tickets

Le job navigateur du tableau de bord arrive avec ANH-83 ; les tests du panneau
local ne le remplacent pas. La matrice Convex par rôle et le contrat machine
sont livrés par ANH-132, et sa dimension organisation par ANH-114 (voir
[convex.md](convex.md#10-tests-automatisés)). L'endurance 24 h de la console entière,
acquisition comprise, reste ANH-164 : aucun job vide ne la simule. L'étape
nocturne de `pi-gate` ne juge que l'enregistrement de séance (ANH-128) : une
journée simulée de séances avec l'écrivain actif, sans le calcul de
l'acquisition. Les règles MEN restent ANH-136 jusqu'à la définition des menaces.
Cette infrastructure préalable à ANH-71 ne clôt donc pas à elle seule ANH-72 ni
ces tickets dépendants.

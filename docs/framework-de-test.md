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
16. [Rejouer une séance enregistrée](#16-rejouer-une-séance-enregistrée)

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
`training/*`, `sim/*`, `bitalino_client`, `signal_processing`, `geometry`,
`ecg_pipeline`, `local_config`, `bitalino_rfcomm_macos`, `local_panel`,
`panel_status`, `cloud_sync`, `dsp`, `sensors/*`, `presence/*`,
`panel_lifecycle`, `task_completion` et `web/profile_writer`. Le reste du code
web est mesuré mais ne bloque pas.

**Dette déclarée.** `src/signal_processing.py` fait partie de la chaîne de
sécurité (la fréquence cardiaque qui pilote le moteur le traverse). Il est
sous le seuil de 100 % (`tests/test_signal_processing.py`), mais **pas encore**
sous les vérificateurs de types. La liste `[tool.anheart] coverage_pending`,
qui nomme les fichiers de la chaîne hors du seuil, est vide ; un test échoue
si elle grandit.

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

Le tableau est l'instantané du 1er octobre 2026. Les tests du rejeu
(`test_replay*.py`, `test_real_records.py`) sont venus après : ils sont
décrits en [section 16](#16-rejouer-une-séance-enregistrée).

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
usage: python -m simulation.run [-h] [--all] [--list] [--csv] [--out OUT]
                                [--replay RECORD] [--json]
                                [--export-real [NAME ...]] [--library LIBRARY]
                                [scenario]
```

| Option | Effet |
|---|---|
| `scenario` | nom (`manual_27_rpm`) ou chemin d'un fichier `.json` |
| `--list` | liste les scénarios (66 aujourd'hui) |
| `--all` | lance tous les scénarios et écrit `simulation/out/summary.md` |
| `--csv` | écrit aussi un CSV des lignes de la trace |
| `--out DIR` | dossier de sortie (défaut `simulation/out/`, ignoré par git) |
| `--replay RECORD` | rejoue un enregistrement (dossier, ou son `.tar.gz`) contre le runtime et compare : [section 16](#16-rejouer-une-séance-enregistrée) |
| `--json` | avec `--replay` : le rapport en une ligne de JSON |
| `--export-real [NAME ...]` | refait les enregistrements de la bibliothèque rejouée en CI ; sans nom, tous ceux qui viennent de la simulation |
| `--library DIR` | dossier de la bibliothèque (défaut `simulation/scenarios/real/`) |

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
> <https://anheart-simulation.vercel.app> (62 scénarios sur 66, vitesse de 10x
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

### 7.3 Ce que la page affiche d'une trace, et ce qu'elle peut charger

Tout ce que la page lit d'une trace, d'un enregistrement ou d'un flux en direct
est inséré comme du **texte**, dans des éléments qu'elle construit elle-même
(`createElement`, `textContent`, `createTextNode`) : nom de la trace et
avertissements du lecteur, libellés et note de géométrie, tuiles, horloge,
liste d'événements. Son script ne confie aucun texte à l'analyseur HTML
(`innerHTML` et équivalents) : un champ qui contient du balisage s'affiche tel
qu'il est écrit.

La page déclare aussi ce qu'elle a le droit de charger, par une politique de
sécurité de contenu écrite dans une balise `meta`, juste après le jeu de
caractères :

| Directive | Valeur | Effet |
|---|---|---|
| `default-src` | `'none'` | rien n'est chargé qui ne soit nommé plus bas : ni cadre, ni police, ni média |
| `script-src` | l'empreinte SHA-256 du script de la page | seul ce script s'exécute : aucun autre script, aucun gestionnaire écrit dans un attribut, pas d'`eval` |
| `style-src` | l'empreinte SHA-256 de la feuille de style de la page | seule cette feuille s'applique : aucune autre feuille, aucun attribut `style` |
| `connect-src` | `'self'` | `fetch` et le flux `/stream` ne vont que vers le serveur qui a servi la page |
| `img-src` | `'self'` | la page n'a pas d'image : c'est l'icône du site, que le navigateur demande de lui-même à ce serveur (sans cette ligne, il signale un refus à chaque ouverture) |
| `object-src` | `'none'` | aucun greffon |
| `base-uri` | `'none'` | l'adresse de base de la page ne peut pas être changée |
| `form-action` | `'none'` | aucun formulaire ne peut être envoyé |

La page est un seul fichier, sans étape de construction : les deux empreintes
sont écrites dans la balise. **Modifier le `<style>` ou le `<script>` de la
page impose de mettre à jour son empreinte**, sinon le navigateur refuse la
feuille ou le script. Le test
`test_ex2_the_policy_names_the_page_s_own_style_and_script` échoue alors et
affiche la valeur à écrire. Un attribut `style` ou `onclick` ajouté dans le
HTML serait refusé de la même façon : passer par une classe de la feuille et
par le script.

Une balise `meta` ne peut pas porter `frame-ancestors`, `sandbox` ni l'envoi de
rapports : ces directives ne se lisent que dans un en-tête de réponse, que ni
`simulation.live` ni l'application hébergée n'envoient.

Tests : `simulation/tests/test_viewer_page.py` exécute le script de la page tel
qu'il est livré, sous Node (`viewer_requests.mjs`), dans un document de
substitution qui garde ce que le script construit et met à part ce qu'il
confierait à l'analyseur HTML. Il lit aussi les adresses demandées et la
politique de la page. Node doit donc être présent là où tourne la batterie de
la simulation.

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
`docs`. La protection de `main` exige les six mêmes, et les deux branches
exigent en plus `agent-review/R1`, qui n'est pas un job
([Lire un échec et relancer](#lire-un-échec-et-relancer)). Ce qui les
distingue, relevé le 7 octobre 2026 : `develop` exige aussi un historique
linéaire et une branche à jour, `main` non, pour que la release s'y fusionne
par un commit de fusion
([deploiement.md, section 5.5, étape 4](deploiement.md#55-réglages-à-faire-une-fois-à-la-main)).

| Job | Contrôles et artefacts |
|---|---|
| `changes` | classe les fichiers changés par la PR et dit aux quatre gates ci-dessous si elles peuvent être sautées (voir [Gates lancées selon les fichiers changés](#gates-lancées-selon-les-fichiers-changés-anh-184)) ; lance d'abord les tests de cette règle, ceux du workflow, ceux du workflow CodeQL, ceux des deux workflows de déploiement, ceux du rapport de qualité et ceux du script qui tient `convex/_generated/api.d.ts` |
| `pi-gate` | gate Pi complète, tests répartis sur un processus pytest indépendant par CPU du runner (voir [Gate Pi en parallèle](#gate-pi-en-parallèle-anh-72)), couverture de branches à 100 % sur la chaîne de sécurité, combinée avant le seuil ; `coverage.xml`. Sur le déclenchement nocturne seulement (`github.event_name == 'schedule'`), une étape de plus après la gate : l'endurance de l'enregistrement de séance, une journée simulée de séances avec l'écrivain actif (`tests/test_record_endurance.py -m slow`, `ANHEART_ENDURANCE_HOURS=24`, ANH-128) |
| `simulation (cohort)`, `simulation (battery 1)` à `simulation (battery 3)` | dans chacun : reproductibilité CAO via Git LFS et l'extracteur OCCT, ruff, basedpyright, mypy, puis ses parts de la batterie de scénarios, un processus pytest par part ; artefacts `simulation-evidence-*` (ce que chaque part a collecté, exécuté et mesuré) |
| `simulation (report)` | `simulation.quick --all` ; artefact `simulation-report` |
| `simulation-gate` | la vérification obligatoire : exige la réussite des cinq jobs précédents, puis prouve que chaque test de la batterie a tourné une fois et une seule, fusionne les mesures et applique le seuil de 100 % de branches (voir [Gate de simulation répartie](#gate-de-simulation-répartie-anh-184)) ; `coverage.xml`, `report.json` et `report.html` |
| `convex-tests` | `convex/_generated/api.d.ts`, versionné, comparé à ce que les fichiers de `convex/` impliquent (`node scripts/ci/convex-generated-api.mjs`, sans déploiement ni réseau : voir [convex.md](convex.md#convex_generatedapidts--tenu-par-un-script)) ; types des fonctions Convex (`tsc -p convex/tsconfig.json --noEmit`), puis vrais handlers Convex exécutés par `convex-test` : droits d'accès aux mesures live, séances et télémétrie ; aucune connexion au déploiement de production. Puis la couverture de ces tests, avec son seuil : 80 % de lignes et de branches sur `convex/` et sur chacun de ses trois fichiers de la chaîne de sécurité (voir [Seuils de couverture de Convex et du site](#seuils-de-couverture-de-convex-et-du-site-anh-203)) |
| `web` | TypeScript, ESLint hors environnements Python, tests du panneau manuel, tests unitaires du site (`lib/`, puis `hooks/`, `components/` et les pages de `app/`), build Next.js avec configuration publique de test. Puis la couverture des tests du site, avec son seuil : 80 % de lignes et de branches (voir [Seuils de couverture de Convex et du site](#seuils-de-couverture-de-convex-et-du-site-anh-203)) |
| `audit` | `npm audit`, `pip-audit` et `gitleaks` sur l'historique Git ; aucun secret de production requis |
| `docs` | liens locaux et ancres Markdown, résolution des identifiants `MEN-nn` dès que `docs/menaces.md` existe ; puis les tests de l'outillage de release (le fichier de `npm run test:release`), dans des dépôts jetables et avec un double de `gh` : rien n'atteint GitHub. Ils sont ici parce que ce job tourne à chaque exécution, quels que soient les fichiers changés, et qu'ils lisent des pages de `docs/` ([release.md](release.md#7-tests)) |
| `quality-report` | n'est pas une gate, et aucune de ses étapes ne peut le faire échouer : attend les six gates, puis écrit sur la page de l'exécution le tableau des tests, de la couverture, du lint et des types de chaque projet (voir [Rapport de qualité](#rapport-de-qualité-anh-199)) ; artefact `quality-report` |

Un autre workflow, `codeql.yml`, fait analyser le dépôt par CodeQL sans être
une gate : voir
[Analyse statique externe](#analyse-statique-externe--codeql-anh-196).

Un troisième, `pi-install.yml`, exécute l'installation du Raspberry Pi de bout
en bout sur un runner arm64 (job `pi-install`) : le vrai `scripts/install.sh`
dans une machine de remplacement, la console démarrée en simulation et
interrogée sur `/healthz`. **Ce n'est pas une gate non plus** : il ne se
déclenche, sur une PR, que si elle modifie de quoi l'installation est faite, et
à chaque push sur `develop`. Ce qu'il vérifie, ses permissions et ce qu'il
implique pour `scripts/release.sh` sont dans
[pi-image.md](pi-image.md#5-ce-que-la-ci-vérifie).

Deux autres, `deploy-production.yml` et `deploy-preview.yml`, déploient sur
Vercel quand une personne le demande, et jamais autrement : voir
[Déploiement Vercel par bouton](#déploiement-vercel-par-bouton-anh-198).

Chaque gate garde aussi, pour le rapport de qualité, ce que ses outils ont
mesuré (artefacts `quality-*`). Rien de ce que le rapport ajoute ne change le
verdict d'une gate : voir [Rapport de qualité](#rapport-de-qualité-anh-199).
La couverture de Convex et celle du site, elles, décident : `convex-tests` et
`web` échouent sous 80 % (voir
[Seuils de couverture de Convex et du site](#seuils-de-couverture-de-convex-et-du-site-anh-203)).

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
rapport synthétique. Les séances enregistrées de `simulation/scenarios/real/`
n'ont pas d'étape nocturne à part : `test_real_records.py` les rejoue dans la
batterie, à chaque exécution (voir [16.5](#165-la-bibliothèque-simulationscenariosreal)).

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
  voisinage des tests ne sont plus ceux de la série. Deux tests ouvrent
  réellement un port,
  `tests/test_web_api.py::test_the_server_serves_and_stops_without_touching_the_signal_handlers`
  et `tests/test_local_panel.py::test_the_production_web_runner_binds_and_exits`.
  Depuis ANH-183, chacun demande un port libre au système (`free_port`, dans
  `tests/test_web_api.py`) au lieu du port fixe 8099 qu'ils partageaient :
  deux gates lancées en même temps sur une même machine ne se le disputent
  plus. Ils restent nommés dans `SAME_PROCESS` (`scripts/ci/pi_gate_shard.py`)
  et vont toujours dans le même processus, qui les exécute l'un après
  l'autre : entre la réponse du système et l'ouverture du port, un autre
  processus pourrait recevoir le même numéro. Si l'un d'eux est renommé, la
  gate échoue jusqu'à la mise à jour de cette liste ;
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
  matrice de pannes, trois tests par propriétés et, depuis ANH-131, les deux
  longs rejeux de séances enregistrées de `test_real_records.py`. Les quinze
  premiers pèsent autant que tout le reste. Distribués à tour de rôle, comme dans la première
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
| site | `app/`, `components/`, `hooks/`, `i18n/`, `lib/`, `messages/`, `public/`, `test-support/` ; à la racine : `next.config.ts`, `proxy.ts`, `tsconfig.json`, `eslint.config.mjs`, `postcss.config.mjs`, `components.json` et les configurations Vitest `vitest.<suite>.config.mts` | sautées | lancées |
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
  gate est seule à le lire ;
* `docs/pi-image.md` : une page de documentation, mais
  `raspberry-pi/tests/test_pi_install.py` compare son tableau de versions aux
  fichiers qui figent ces versions.

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
| Ce qui est cherché | des failles de sécurité et des défauts de qualité du code (vérification sans effet, variable peut-être non initialisée, import inutile), avec la suite `security-and-quality` de GitHub, nommée sous `queries` dans le fichier de périmètre |
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

**Traiter un constat.** La suite de qualité est active : un constat, de
sécurité ou de qualité, se corrige dans le code, de la façon que sa règle
attend, et la liste des constats ouverts ne doit contenir que ce qui reste à
traiter. Trois règles :

* **Corriger, pas cacher.** Pas de commentaire de suppression, pas de
  renommage ni d'enrobage qui ferait seulement taire l'outil. Si le constat
  montre un vrai défaut (une vérification qui n'en était pas une, une variable
  lue avant d'être affectée sur un chemin réel), la correction vient avec un
  test qui échoue sans elle.
* **Un faux positif se classe après relecture.** La personne qui développe ne
  classe rien : elle donne le numéro du constat, sa raison en une ou deux
  phrases et sa catégorie (`false positive`, `used in tests`, `won't fix`). La
  revue relit, puis le constat est classé dans GitHub (**Dismiss alert**) avec
  cette raison écrite.
* **Aucune exclusion pour faire passer.** `paths-ignore`, dans
  `.github/codeql/codeql-config.yml`, reste réservé au code généré ou
  installé ; aucun filtre de règle n'est ajouté
  (`scripts/ci/analysis-workflows.test.mjs` le vérifie).

Formes que l'analyse et les vérificateurs de types stricts acceptent ensemble,
à reprendre dans le code Python nouveau. Les quatre premières sont la règle 10
du contrat de code strict (résumé dans
[raspberry-pi.md, section 13](raspberry-pi.md#13-le-contrat-de-code-strict-et-la-gate)),
qui en donne un exemple chacune :

* **`match` qui rend une valeur.** Une fonction qui rend une valeur dans
  chaque cas d'un `match` exhaustif se termine, après le `match`, par
  `raise assert_never(sujet)`, sans dernier cas générique : l'analyse suppose
  qu'un `match` peut finir sans prendre de cas. Les deux vérificateurs de
  types refusent toujours un cas oublié en le nommant, et une valeur
  impossible lève toujours une `AssertionError`. Chaque cas rend sa valeur,
  au lieu d'affecter une variable lue après le `match`. Un sujet qui est un
  appel ou un `await` est d'abord lié à un nom. Un `match` dont les cas
  agissent sans rien rendre garde son dernier cas générique.
* **Motif de classe.** Le motif reconnaît la classe, et le champ se lit sur la
  valeur reconnue (`case AlreadyStarted():` puis `refusal.state`). Capturer un
  champ sous son propre nom (`case AlreadyStarted(state=state):`) est lu par
  l'analyse comme un usage de `state` avant son affectation ; elle l'a signalé
  sur le premier cas d'un `match` qui ouvre une fonction. Pour une erreur
  portée par un `Result`, la forme imbriquée de la règle 3 reste la règle :
  `case Err(error):`, puis `match error:`, chaque cas lisant son champ sur
  `error`, et chaque garde après son propre `match`. Nommer la variante dans
  `Err(...)` (`case Err(BadResponse() as error):`) est refusé par les deux
  vérificateurs de types, même quand tous les cas sont traités.
* **Membre de protocole.** Un membre de `typing.Protocol` est déclaré
  `@abstractmethod` et a sa documentation pour seul corps, comme les
  protocoles de la bibliothèque standard. Une classe qui satisfait le
  protocole par sa forme n'est pas concernée ; une classe qui en hérite doit
  définir tous ses membres, sinon elle ne peut pas être construite.
* **Imports.** Deux modules ne s'importent jamais l'un l'autre, même sous
  `if TYPE_CHECKING:` : l'analyse compte un import réservé aux types comme
  exécuté et signale un cycle. Le type partagé se définit dans celui des deux
  que l'autre importe déjà, et l'autre le redonne sous son propre nom quand ce
  nom est public.
* **Journal.** Une valeur reçue d'un tiers n'entre dans un journal qu'une fois
  ses sauts de ligne remplacés et sa longueur bornée, comme le fait
  `logged_header` ([`src/web/ws.py`](../raspberry-pi/src/web/ws.py)).
* **Nom de fichier.** Pour un nom de fichier reçu de l'extérieur, la forme que
  l'analyse reconnaît est celle de `resolve_under`
  ([`src/record/containment.py`](../raspberry-pi/src/record/containment.py)) :
  normaliser le chemin, vérifier qu'il commence par le dossier permis, puis
  n'utiliser que le chemin normalisé.

Ce que l'analyse lit mal aujourd'hui et qu'aucune écriture plus simple ne
retire, donc ce qui se remet comme faux positif avec sa raison : l'instruction
`type` et les paramètres de type ne sont pas vus comme des définitions ;
`await` sur une tâche est lu comme une instruction sans effet ; une constante
publique qui n'est lue que par un autre module peut être dite inutilisée.

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
`changes` de `ci.yml` à chaque exécution, avec les autres fichiers de test
de la CI (`node --test`, sans installation). Il vérifie ce qu'une modification
pourrait casser sans qu'aucun job ne rougisse : actions épinglées par commit
complet (celle de checkout sur le même commit que `ci.yml`), une seule
permission d'écriture dans tous les workflows du dépôt, déclencheurs, langages,
noms des jobs, la suite de requêtes (`security-and-quality`, sans filtre de
règle), et le périmètre confronté aux fichiers suivis par Git (seuls le
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

### Déploiement Vercel par bouton (ANH-198)

Deux workflows séparés de `ci.yml` déploient sur Vercel, et seulement quand une
personne le demande : aucun push, aucune PR, aucun horaire ne les lance. **Ce ne
sont pas des gates.** Le mode d'emploi, les réglages à faire à la main et l'état
réel sont dans
[deploiement.md, section 5](deploiement.md#51-comment-il-se-déploie). Aucun des
deux n'a encore tourné : ils ne peuvent être lancés qu'une fois sur `main`.

| | `deploy-production.yml` | `deploy-preview.yml` |
|---|---|---|
| Nom affiché dans Actions | « Déployer en production (main) » | « Déployer la préversion (develop) » |
| Déclencheur | `workflow_dispatch`, et aucun autre | `workflow_dispatch`, et aucun autre |
| Branche acceptée | `main` | `develop` |
| Ce qui est demandé | quoi déployer (`site`, `simulation`, `site et simulation`) ; le mot `production`, à saisir | quoi déployer ; si le site est choisi, les mots `données de production`, à saisir |
| Ce que le formulaire, le nom de l'exécution et le haut de sa page rappellent | le site de production ne se déploie que dans la fenêtre du déploiement de Convex, jamais pendant une séance ; les gates du commit ne sont pas vérifiées | une préversion du site lit et écrit les données de production tant que les variables Preview de Vercel ne sont pas séparées |
| Environnement GitHub | `production` | `preview` |
| Environnement Vercel visé | Production | Preview |
| Jobs | `Vérifier la demande (production)`, `Déployer le site (production)`, `Déployer la simulation (production)` | les trois mêmes, suffixés `(préversion)` |

Par ailleurs un push ne déploie plus rien dès que son commit contient le
`vercel.json` de la racine, qui coupe les déploiements que Vercel lançait à
chaque push, pour les deux projets (constaté sur une branche de travail, pas
encore sur `main` : [deploiement.md](deploiement.md#avant-et-après-larrivée-sur-main)).

**Jobs.** Le premier vérifie la demande sans passer par l'environnement GitHub :
une demande lancée sur une autre branche, sans la confirmation (production) ou
sans l'accord sur les données de production (préversion qui comprend le site),
est refusée avant qu'une approbation soit demandée à qui que ce soit. Il écrit
ensuite en haut de la page de l'exécution ce que le bouton ne vérifie pas. Les
deux jobs de déploiement en dépendent et ne partent que s'il a réussi ; celui du
projet qui n'a pas été choisi est ignoré. Chacun vérifie d'abord que ses secrets
existent, récupère la tête de la branche du bouton, et **refuse de continuer si
ce n'est plus le commit sur lequel l'exécution a été lancée** (« Exécution
périmée ») : une exécution approuvée tard, ou relancée, ne déploie jamais un
commit plus ancien que la tête de la branche. Il installe alors la CLI Vercel à
sa version figée, déploie, puis écrit dans le résumé le commit déployé et
l'adresse obtenue. Un job de déploiement ne parle que de son projet : quand il
refuse, il dit « le site n'a pas été déployé » ou « la simulation n'a pas été
déployée », jamais que rien ne l'a été, puisque l'autre job a pu déployer. Deux
déploiements de la même cible (même environnement, même projet) ne se
chevauchent pas : le second attend, et un déploiement en cours n'est jamais
annulé par le suivant.

Le champ d'accord du bouton de préversion (`production_data`), l'étape qui
l'exige et la phrase qui l'accompagne sont provisoires : ils sont à retirer,
avec leurs tests, une fois les variables Preview de Vercel séparées
([deploiement.md, section 5.5, étape 6](deploiement.md#55-réglages-à-faire-une-fois-à-la-main)).

Le site est construit sur le runner puis envoyé (`vercel pull`, `vercel build`,
`vercel deploy --prebuilt`), la procédure que Vercel documente pour GitHub
Actions. La simulation garde la procédure de son script `deploy.sh` : `build.sh`
assemble `dist/`, que la CLI envoie et que Vercel construit.

**Permissions et secrets.** `contents: read`, rien d'autre : la règle « une
seule permission d'écriture dans tous les workflows du dépôt » tient toujours.
Quatre secrets sont lus par leur nom et passés aux scripts par `env`, jamais
écrits dans un script : `VERCEL_TOKEN`, `VERCEL_ORG_ID`,
`VERCEL_PROJECT_ID_SITE` et `VERCEL_PROJECT_ID_SIMULATION`. Le jeton n'est remis
qu'aux étapes qui appellent Vercel : ni l'installation de la CLI ni la
construction du site, qui exécutent des scripts d'installation de paquets npm,
ne le reçoivent. Les secrets sont attendus dans les deux environnements GitHub,
chacun limité à sa branche, et non parmi les secrets du dépôt
([deploiement.md, section 5.5, étape 2](deploiement.md#55-réglages-à-faire-une-fois-à-la-main)).
Ce que `vercel pull` écrit sur le runner (le dossier `.vercel/`, avec les
variables du projet dont Vercel rend encore la valeur) n'est ni affiché ni
envoyé : aucune étape ne nomme ce dossier, n'affiche l'environnement ni ne
trace ses commandes, et aucune action autre que le checkout et l'installation
de Node n'est utilisée. Aucun cache n'est restauré dans un déploiement. Les
actions sont celles de `ci.yml`, épinglées sur les mêmes commits, avec pour
seules entrées celles que le test connaît.

**Tests.** `scripts/ci/deploy-workflows.test.mjs` est lancé par le job `changes`
à chaque exécution, avec les autres fichiers de test de la CI. Il lit les deux
workflows comme du texte et **exécute les scripts de leurs étapes comme le
ferait le runner**, avec un double à la place de la CLI Vercel : aucun test
n'appelle Vercel. Il vérifie :

* que `vercel.json` coupe les déploiements Git pour toutes les branches et ne
  contient rien d'autre (les deux projets Vercel lisent ce même fichier) ;
* que chaque workflow n'a qu'un déclencheur, le bouton ;
* que la garde de branche est la première étape, qu'elle accepte la branche du
  bouton et refuse les autres références (autre branche, tag du même nom, PR) ;
* que la production exige le mot `production` exact, et que son formulaire, le
  nom de son exécution et le haut de sa page disent ce qu'elle ne vérifie pas ;
* que la préversion exige l'accord saisi dès que le site est choisi, et jamais
  pour la simulation seule ;
* les noms des environnements, les groupes de concurrence, l'absence de toute
  permission d'écriture, et qu'aucun job ne porte le nom d'un job de `ci.yml` ;
* que la CLI est installée à une version exacte, la même dans les deux
  workflows, et que les actions sont épinglées comme dans `ci.yml` ;
* que le checkout prend la branche du bouton, nommée en entier, sans garder de
  jeton, et rien d'autre : toute modification de ses entrées fait échouer le
  test ;
* qu'une exécution dont le commit n'est plus la tête de la branche est refusée
  avant toute installation ;
* que les secrets ne sont lus que par `env`, qu'aucun script ne contient
  d'expression, qu'un secret manquant fait échouer le job par son nom, et
  qu'aucune valeur n'est affichée ;
* qu'un job de déploiement ne dit jamais que rien n'a été déployé ;
* que rien n'affiche ni n'envoie ce que `vercel pull` écrit sur le runner ;
* les commandes passées à la CLI pour chaque cible, le refus d'un déploiement
  qui échoue ou ne rend pas d'adresse, et le contenu du résumé ;
* que les jobs de déploiement des deux workflows exécutent les mêmes scripts :
  ils ne diffèrent que par trois variables, par la branche du checkout et par
  ce que chaque bouton demande avant de déployer.

**Ce que ces tests ne prouvent pas.** Qu'un déploiement réel réussit : aucun
n'a été lancé, et la CLI y est remplacée par un double. Les réglages du dépôt
(environnements, approbation, secrets) et ceux des projets Vercel ne sont pas
dans le dépôt : rien ne les teste.

**Avant une release.** Les jobs de ces deux workflows sont des check runs du
commit de tête de la branche déployée, et `scripts/release.sh` les lit comme
ceux de CodeQL : une exécution en échec, annulée, en cours ou en attente
d'approbation fait refuser `prepare` et `pr` (tête de `develop`) ou `tag` (tête
de `main`). Voir
[release.md](release.md#les-boutons-de-déploiement-et-le-script).

### Rapport de qualité (ANH-199)

Chaque exécution de `ci.yml` écrit sur sa propre page un tableau : pour chaque
projet du dépôt, combien de tests ont tourné, combien ont réussi, la
couverture, l'état du lint et des types, et l'état de la gate. Aucun journal à
ouvrir, aucun service externe.

**Où le lire.** Sur la page de résumé de l'exécution : **Actions → CI**, puis
l'exécution voulue ; depuis une PR, « Details » à côté d'une vérification de la
CI, puis « Summary » en haut de la colonne de gauche. Le tableau « Rapport de
qualité » est écrit par le job `quality-report`, qui attend les six gates : il
apparaît une fois qu'elles sont terminées.

GitHub range les résumés des jobs dans l'ordre où les jobs finissent. Ce
tableau est donc le dernier bloc de résumé de la page : au-dessus de lui
viennent le détail replié de chaque gate et les résumés que vitest écrit de
lui-même. Pour y arriver sans faire défiler, le même job émet une annotation
« Rapport de qualité », que la page liste avec les annotations de l'exécution :
le verdict, une ligne par projet, le seuil de la chaîne de sécurité du Pi avec
ce qu'il juge et ce qu'il ne juge pas encore, et pour finir l'adresse directe
du tableau (`…/actions/runs/<exécution>#summary-<job>`).

Le contrôle des liens du job `docs` n'écrit plus de résumé (`jobSummary:
false`) : c'était un bloc de plus au-dessus du tableau. Son rapport reste
entier dans le journal de son étape, où il était déjà imprimé.

Les mêmes chiffres sont dans l'artefact `quality-report` de l'exécution, gardé
90 jours : `quality-report.json`, pour une machine, et `quality-report.md`, le
tableau tel qu'il est affiché.

**Une ligne par projet.**

| Ligne | Ce qui y est compté | Gate affichée |
|---|---|---|
| Console du Pi (tout `src/`) | les tests Python de `raspberry-pi/tests` (pytest, job `pi-gate`) et les tests JavaScript du panneau local, `raspberry-pi/tests/web` (`node --test`, job `web`). La couverture affichée est celle de tout `raspberry-pi/src/`, pas celle de la chaîne de sécurité, donnée à part | `pi-gate` |
| Simulation | la batterie de `simulation/tests` (pytest, ses 13 parts réunies) | `simulation-gate` |
| Convex | `convex/**/*.test.ts` (vitest) | `convex-tests` |
| Site | les tests de `lib/`, puis ceux de `hooks/`, de `components/` et des pages de `app/` (vitest, deux suites) | `web` |
| Scripts | les tests de `scripts/ci` lancés par `changes`, `audit` et `docs` (`node --test`), et ceux du lanceur des gates Python lancés par `pi-gate` (pytest) | `changes`, `audit`, `docs` |

Sous le tableau, une ligne donne l'état d'`audit` et de `docs`, sans rien de ce
qu'ils ont trouvé, puis trois parties : « Seuils de couverture, chaîne de
sécurité et simulation », « Détail par suite de tests » (une ligne par suite,
avec le job qui la lance) et « Lire ce rapport ».

**Ce que dit chaque colonne.**

| Colonne | Ce qu'elle dit | D'où vient le chiffre |
|---|---|---|
| Tests lancés | le nombre de tests que les suites du projet ont exécutés ou ignorés dans cette exécution | les fichiers JUnit écrits par pytest, vitest et `node --test` eux-mêmes |
| Réussis | ceux qui ont réussi | idem |
| Échoués | ceux qui ont échoué, en gras dès qu'il y en a un ; leurs noms sont dans le détail du job | idem |
| Ignorés | les tests sautés par un marqueur (`skip`) et les défauts connus déclarés (`xfail`), qui ne sont ni des réussites ni des échecs | idem |
| Durée cumulée | la somme des durées de chaque test, tous processus confondus. Ce n'est pas le temps d'attente : `pi-gate` répartit ses tests sur quatre processus, la simulation sur quatre jobs | idem |
| Lignes couvertes | la part des lignes exécutées par les tests, sur tous les fichiers source du projet, y compris ceux qu'aucun test ne charge | `coverage json` de coverage.py pour le Pi et la simulation, `coverage-final.json` de vitest pour Convex et le site |
| Branches couvertes | la part des branches prises (chaque issue d'un `if`, d'un `match`, d'un opérateur ternaire) | idem |
| Lint | l'état des contrôles de style : ruff (`check` et `format`) pour le Pi, la simulation et les fichiers Python des scripts, ESLint pour Convex, le site et les scripts | les étapes enregistrées par `check.sh`, et le résultat de l'étape `npm run lint` du job `web` |
| Types | l'état des contrôles de types : basedpyright et mypy pour le Python, `tsc -p convex/tsconfig.json` pour Convex, `tsc --noEmit` pour le site | les étapes enregistrées par `check.sh`, et le résultat de l'étape `tsc` de chaque job |
| Gate | l'état de la vérification obligatoire du projet, tel que GitHub le donne | le contexte `needs` du job `quality-report` |

Un pourcentage n'est jamais arrondi vers le haut : 99,96 % s'affiche 99,9 %, et
« 100 % » veut dire que rien ne manque.

**Les mots du tableau.**

- « sautée » : sur une PR, la règle de chemins a jugé qu'aucun fichier changé
  ne concerne cette gate
  ([Gates lancées selon les fichiers changés](#gates-lancées-selon-les-fichiers-changés-anh-184)).
  Elle n'a pas tourné : la ligne le dit dans chaque colonne, au lieu d'afficher
  zéro. Un cas particulier : les tests du panneau local de la console sont
  lancés par `web`. Sur une PR qui ne touche que le site, `pi-gate` est sautée
  et ces tests tournent quand même : la ligne de la console les affiche, avec
  « ¹ », et dit « sautée » pour tout ce que `pi-gate` mesure. Le total de
  l'en-tête est toujours la somme des lignes.
- « indisponible » : le job a tourné, mais n'a pas laissé ce chiffre (artefact
  absent, fichier illisible, processus arrêté avant d'écrire son rapport). Le
  rapport ne devine pas : il n'affiche pas de total partiel pour une suite
  dont un processus manque.
- « non lancé » : le job n'a pas tourné, sans que la règle de chemins l'ait
  décidé (exécution annulée, étape précédente en échec).
- « non mesurée » : aucun outil ne mesure cette valeur aujourd'hui (la
  couverture des scripts).
- « ¹ » après un nombre de tests, « (partiel) » après un état : le nombre ne
  compte qu'une partie des suites du projet, ou l'état qu'une partie de ses
  contrôles ; les autres n'ont pas de chiffres dans cette exécution. Exemple :
  sur une PR qui ne touche que le site, les scripts n'ont ni les tests ni les
  contrôles Python que `pi-gate` lance pour eux.
- « rapport indisponible » : l'outil du rapport lui-même a échoué. Aucune gate
  ne dépend de lui ; son journal dit pourquoi.

**Seuils de couverture, chaîne de sécurité et simulation.** Cette partie donne
chaque seuil exigé par une gate, ce qu'il juge et s'il est tenu : la chaîne de
sécurité du Pi, la simulation, puis Convex et le site (leurs lignes sont
décrites dans
[Seuils de couverture de Convex et du site](#seuils-de-couverture-de-convex-et-du-site-anh-203)).
La colonne de couverture du Pi porte sur tout `raspberry-pi/src/`. La chaîne de sécurité est donnée à part, en trois
temps, parce que la configuration peut déclarer un fichier dans la chaîne sans
l'avoir encore mis sous le seuil de 100 % :

- « fichiers sous le seuil » : la liste `include` de
  `raspberry-pi/pyproject.toml`, dont `pi-gate` exige 100 % de lignes et de
  branches. C'est le seul chiffre accompagné de « seuil tenu » ou « non
  tenu ». Ce seuil n'a pas changé : le rapport lit la mesure sur laquelle la
  gate vient de l'appliquer ;
- « hors du seuil » : chaque fichier que la même configuration déclare dans la
  chaîne de sécurité sans l'avoir encore mis sous le seuil (la liste
  `coverage_pending` de `[tool.anheart]`), par son nom, avec sa propre
  couverture. Le rapport lit cette liste dans la configuration à chaque
  exécution ; il ne connaît aucun nom de fichier. Si elle ne peut pas être lue,
  il écrit « indisponible », jamais « aucun » ;
- « en entier » : les deux réunis, sans seuil.

« 100 % » se lit donc « les fichiers sous le seuil sont à 100 % », pas « la
chaîne de sécurité est à 100 % ». Sur l'exécution 37623954237, les 76 fichiers
sous le seuil étaient à 100 %, et `src/signal_processing.py`, seul fichier hors
du seuil, à 82,5 % de lignes (132 sur 160) et 64,0 % de branches (32 sur 50) :
la chaîne entière à 99,7 % de lignes et 99,3 % de branches. Ce fichier est
depuis passé sous le seuil (ANH-207) : la liste est vide, et le rapport écrit
« aucun fichier » hors du seuil, « toute la chaîne est sous le seuil ».

La simulation exige 100 % sur tout son code. La même partie donne la batterie
de simulation (tests réussis, en échec, ignorés, dont les `xfail`) et le nombre
de scénarios joués par `simulation.quick --all`, par verdict (`PASS`, `XFAIL`,
`SKIPPED`, `FAIL`), lus dans son `report.json`.

**Détail par gate.** Chaque job `pi-gate`, `simulation-gate`, `convex-tests` et
`web` écrit aussi son propre résumé, replié sous le titre « Détail de la
qualité » : ses suites, les dix fichiers les moins couverts, les dix tests les
plus lents (à partir d'un dixième de seconde), les noms des tests en échec et
l'état de chaque contrôle de lint et de types. Celui de `pi-gate` nomme aussi
les fichiers de la chaîne de sécurité hors du seuil. La couverture complète, fichier
par fichier, est dans l'artefact `quality-<projet>` du job : `coverage-all.json`
et `coverage-gate.json` pour le Pi et la simulation, le rapport HTML de vitest
pour Convex (`coverage-convex/index.html`) et pour le site.

**Couverture de Convex et du site : exigée.** Ces deux mesures ont été lues
sans seuil pendant une journée (le premier rapport, 7 octobre 2026), puis un
seuil de 80 % leur a été donné : il est décrit, avec ce qu'il juge et la
commande pour le vérifier en local, dans
[Seuils de couverture de Convex et du site](#seuils-de-couverture-de-convex-et-du-site-anh-203).
Le rapport lit la mesure que la gate vient de juger ; il ne la refait pas et
ne décide toujours rien.

**Temps ajouté aux jobs par le rapport.** Mesuré le 7 octobre 2026 sur les trois exécutions
de la PR #41 (37617756948, 37621116038 et 37623954237), comparées aux deux
push sur `develop` de la même heure (37620748521 et 37622721443), qui n'ont pas
le rapport.

| Job | Durée sur la PR | Sur `develop`, sans le rapport | Étapes ajoutées |
|---|---|---|---|
| `convex-tests` | 49 s, 44 s, 56 s | 36 s, 33 s | 6 à 8 s : mesure de couverture 5 à 7 s, résumé et publication 1 s |
| `web` | 73 s, 87 s, 109 s | 88 s, 92 s | 10 à 17 s : mesure de couverture 9 à 14 s, résumé et publication 1 à 3 s |
| `pi-gate` | 24 min 13 s, 19 min 32 s, 25 min 20 s | 23 min 39 s (37617560989), 25 min 54 s | environ 15 s : 8,5 s pour cinq tests ajoutés au lanceur (102 tests en 59,3 s contre 97 en 50,9 s, sur le même processeur), 2,8 s pour écrire la couverture en JSON, 2 à 4 s de résumé et de publication |
| `simulation-gate` (le verdict seul) | 88 s, 62 s, 83 s | 48 s, 64 s | environ 13 s : les mêmes tests du lanceur, que ce job relance, 1 s de JSON, 1 à 3 s de résumé et de publication |
| `changes`, `audit`, `docs` | 10 s et 7 s, 63 s, 15 s et 9 s | 8 s et 6 s, 61 s et 62 s, 15 s et 6 s | 1 à 2 s de publication chacun |
| `quality-report` | 13 s, 9 s, 14 s | n'existait pas | tout le job, après les gates : il ne retarde aucune vérification obligatoire |

À lire avec leur bruit : sur les autres branches du même jour, `convex-tests`
a duré de 25 à 61 s et `web` de 52 à 92 s, selon le runner (l'installation de
Node de 1 à 15 s, `npm ci` de 14 à 24 s). La somme des étapes ajoutées, elle,
se lit dans chaque exécution : 17 s au plus dans `web`, 8 s au plus dans
`convex-tests`. Les tests qui décident ces deux gates durent autant avec ou
sans leur fichier JUnit (4 à 6 s pour Convex, 5 à 8 s pour `hooks/` et
`components/`). Dans `simulation-gate`, l'écart avec `develop` vient surtout du
processeur du runner : les 97 tests du lanceur y ont pris 32 s sur un runner
et 51 s sur un autre. L'étape « Pi gate » a duré de 15 min 06 s à 25 min 19 s
sur les autres branches du jour (37618180391 et 37614363760) : ce que le
rapport y ajoute ne s'y distingue pas. Trois autres tests ont été ajoutés au
lanceur après ces mesures (environ 3 s en local, deux fois par exécution).

**Ce qui n'est pas mesuré.**

- La couverture des scripts (`scripts/`), et celle du JavaScript du panneau
  local (`raspberry-pi/src/web/static/`).
- Le Python hors des deux paquets mesurés. Pour le Pi, seul
  `raspberry-pi/src/` l'est : pas `raspberry-pi/scripts/`. Pour la simulation,
  `simulation/cad/`, `simulation/scripts/` et les tests sont écartés de la
  mesure par sa configuration, et `simulation_app.py`, à la racine du dépôt,
  est hors du paquet. Le « 100 % » de la simulation ne dit rien d'eux.
- `i18n/` et `proxy.ts` (le middleware Clerk et next-intl, à la racine du
  dépôt) : ils sont hors des dossiers mesurés, donc ni comptés ni jugés par le
  seuil du site. Les pages (`app/`) sont mesurées et jugées depuis ANH-204 ; le
  parcours dans un navigateur est le sujet d'ANH-83.
- La couverture des tests de release (`npm run test:release`) : depuis
  ANH-195 le job `docs` les lance et le rapport les compte dans la ligne des
  scripts, mais aucun outil ne mesure ce qu'ils couvrent de
  `scripts/release.sh`, qui est du shell.
- Les types des scripts `.mjs` : `tsc` ne lit que les fichiers `.ts` et `.tsx`.
  La colonne « Types » des scripts ne porte que sur leurs fichiers Python.
- Les tests écartés par configuration (marqueur `hardware`, tests `slow` de
  l'endurance nocturne) : ils ne sont pas lancés, donc pas comptés.
- La qualité du code au sens d'un outil d'analyse (complexité, duplication,
  code mort) : aucun outil du dépôt ne la mesure. CodeQL cherche des failles de
  sécurité et des défauts de qualité, et ses résultats se lisent dans
  **Security → Code scanning**
  ([Analyse statique externe](#analyse-statique-externe--codeql-anh-196)).
- Ce que trouvent `npm audit`, `pip-audit`, gitleaks et CodeQL : le résumé d'une
  exécution d'un dépôt public est public. Le rapport ne porte que des noms de
  tests et de fichiers, des comptes et des durées ; ni message d'échec, ni
  sortie de test.
- Ce que font les autres workflows du dépôt (`codeql.yml`, `pi-install.yml`,
  les workflows de déploiement) : le rapport ne lit que les jobs de `ci.yml`,
  et le dit dans son résumé.
- L'évolution dans le temps : chaque exécution a son `quality-report.json`,
  rien ne les compare encore.

**Format de `quality-report.json`.**

| Champ | Contenu |
|---|---|
| `schema` | `1`. La version du format : elle augmente quand un champ change de sens ou disparaît, pas quand un champ s'ajoute |
| `run` | l'exécution : `id`, `attempt`, `event`, `repository`, `ref`, `sha` (le commit de tête de la PR sur une PR), `pull_request`, `url`, `summary_url` (l'adresse du tableau dans la page de l'exécution, ou `null`) |
| `jobs` | l'état de `changes` et de chaque gate : `passed`, `failed`, `cancelled`, `skipped` (par la règle de chemins), `not_run`, `unknown` |
| `projects` | une entrée par ligne du tableau : `id`, `label`, `gate` (`state`, et `jobs` avec l'état de chacun), `tests` (`total`, `passed`, `failed`, `skipped`, `duration_s`, et `complete`, faux si une suite du projet manque) ou `null`, `tests_state`, `coverage` (`lines` et `branches`, chacun `covered` et `total`) ou `null`, `coverage_state`, `lint` et `types` (`state`, `complete`, `checks`) |
| `suites` | une entrée par suite de tests : `id`, `project`, `job`, `label`, `runner`, `state` (`measured`, `skipped`, `not_run`, `unavailable`) et, si elle est mesurée, `numbers` : `tests`, `passed`, `failed`, `skipped`, `expected_failures`, `duration_s`, `files` (le nombre de fichiers JUnit lus), `slowest`, `failed_tests` |
| `coverage` | une entrée par mesure (`pi` : tout `raspberry-pi/src/` ; `pi-threshold` : les fichiers sous le seuil ; `simulation` ; `convex` ; `convex:<fichier>` : chaque fichier Convex de la chaîne de sécurité pris seul ; `site`) : `id`, `project`, `job`, `label`, `main` (vrai pour celle du tableau), `threshold` s'il y en a un, `held` (vrai si le seuil est tenu ; absent sans seuil ou sans mesure), `state` et, si elle est mesurée, `numbers` : `lines`, `branches`, `files`, `least_covered` |
| `safety_chain` | ce que la chaîne de sécurité du Pi contient hors du seuil : `listed` (la liste `coverage_pending` telle que lue, `null` si elle n'a pas pu l'être), `pending` (chaque fichier mesuré qu'elle nomme, avec `lines` et `branches`), `not_measured` (ses entrées sans fichier mesuré), `whole` (les fichiers sous le seuil et ceux-là réunis, `null` s'il manque une partie) ; `null` si `pi-gate` n'a rien laissé |
| `scenarios` | le rapport synthétique de la simulation : `runs`, `by_status`, `by_group` ; `null` s'il n'a pas été lu |

Les durées sont en secondes. Les comptes de couverture sont des entiers : le
pourcentage se calcule, il n'est pas stocké.

**Comment il est produit.**

- Les lanceurs de tests écrivent eux-mêmes un fichier JUnit : `--junitxml`
  pour chaque processus pytest, `--reporter=junit` pour vitest,
  `--test-reporter=junit` pour `node --test`. Aucun chiffre n'est lu dans un
  journal.
- `raspberry-pi/scripts/check.sh` et `simulation/scripts/check.sh` acceptent
  `QUALITY_REPORT_DIR=<dossier>` : ils y notent comment chaque étape s'est
  terminée (`stages.tsv`) et passent `--report <dossier>` à
  `scripts/ci/pi_gate_parallel.py`, qui y laisse le fichier JUnit de chaque
  processus et la couverture réunie en JSON, une fois pour ce que le seuil juge
  (`coverage-gate.json`), une fois pour tout ce qui a été mesuré
  (`coverage-all.json`). Aucun test n'est relancé : ce sont les mesures de la
  gate. Sans la variable, les deux scripts se comportent comme avant.
- `scripts/ci/quality-report.mjs job <projet>` lit ces fichiers dans le job,
  écrit le détail du job et `part.json` ; l'artefact `quality-<projet>` les
  porte jusqu'au dernier job. Pour le Pi, il lit aussi la liste
  `coverage_pending` de `raspberry-pi/pyproject.toml`, comme du texte (rien
  n'est installé pour lire du TOML) : une liste de chaînes simples, sur une ou
  plusieurs lignes, commentaires admis. Écrite autrement, elle est dite
  « indisponible ». `changes`, `audit` et `docs` publient seulement le fichier
  JUnit de leurs tests de scripts.
- `scripts/ci/quality-report.mjs report`, dans `quality-report`, réunit le tout
  avec l'état de chaque job.

**Ce que le rapport ne peut pas faire.** Il ne décide aucune gate. Les étapes
qu'il ajoute aux gates (« Quality report, the numbers of this job », « Keep the
numbers for the quality report ») portent toutes `continue-on-error: true`.
Les étapes « Enforce the coverage » de `convex-tests` et de `web` ne sont pas
les siennes : elles font partie de leur gate et peuvent la faire échouer. Le workflow ne reçoit aucune permission de plus :
lecture seule, pas de commentaire posté dans la PR. Un dossier de rapport
impossible à créer ou à écrire, ou un fichier de rapport impossible à
remplacer, est signalé dans le journal de la gate, qui juge ensuite comme sans
lui.

Le job `quality-report` n'est pas une vérification obligatoire. Chacune de ses
étapes continue sur erreur et son outil rend toujours le code 0 : aucune étape
ne peut le faire échouer. Il peut encore finir autrement que vert pour une
raison qui n'est pas une étape : sa limite de 5 minutes, une panne du runner,
une exécution annulée. Aucune fusion n'en dépend ; seul `scripts/release.sh` le
verrait (voir les limites plus bas).

Un nom de test, de fichier, d'étape ou de verdict de scénario n'est jamais
écrit tel quel dans un résumé : tout caractère autre qu'une lettre, un chiffre,
une espace ou quelques signes sans effet est écrit comme une référence de
caractère. Un nom ne peut donc ni fermer une cellule, ni produire un lien, une
image, une mention ou une balise.

**Tests.** `scripts/ci/quality-report.test.mjs` (job `changes`, sans
installation) nourrit l'outil avec des fichiers écrits comme les outils les
écrivent : toutes les gates vertes, une gate sautée par la règle de chemins,
une gate en échec, un artefact absent, une exécution dont on ne sait rien. Il
exécute aussi la fonction `stage` des deux `check.sh`, et vérifie que cette
section décrit chaque colonne. `scripts/ci/ci-workflow.test.mjs` vérifie que
chaque étape ajoutée par le rapport continue sur erreur, que chaque suite est
écrite par le job dont le rapport l'attend, et que le job `audit` ne transmet
que les fichiers JUnit de ses deux fichiers de test. `scripts/ci/test_pi_gate_parallel.py`
vérifie que `--report` ne change aucun verdict : dossier impossible à créer ou
fermé en écriture, fichier impossible à remplacer, `coverage json` en échec.
`quality-report.test.mjs` vérifie aussi que la liste des fichiers hors du seuil
vient de la configuration, et qu'un nom hostile ressort en texte.

**Limites.**

- Le tableau n'existe qu'une fois toutes les gates terminées : pendant
  l'exécution, seuls les détails des jobs déjà finis sont visibles.
- Le tableau est le dernier bloc de résumé de la page, pas le premier : l'ordre
  des résumés est celui de la fin des jobs, et il ne se règle pas. L'annotation
  « Rapport de qualité » en reprend l'essentiel, en texte, et se termine par
  l'adresse du tableau. Les résumés « Vitest Test Report », que vitest écrit
  dans `convex-tests` et `web`, restent au-dessus de lui.
- Après « Re-run failed jobs », le rapport est réécrit avec les chiffres de la
  dernière exécution de chaque job.
- `quality-report` démarre aussi sur une exécution annulée (`always()`) : il
  demande un runner quelques secondes, comme `simulation-gate`, et son tableau
  dit alors « annulée » ou « non lancé ».
- `scripts/release.sh` lit toutes les vérifications du commit, obligatoires ou
  non. Un `quality-report` annulé avec son exécution, arrêté par sa limite de
  temps ou victime d'une panne de runner lui fait refuser `prepare`, `pr` ou
  `tag`, comme n'importe quel job dans cet état. Relancer l'exécution.
- Un test `test.fails` de vitest (échec attendu) est écrit comme réussi dans
  son fichier JUnit : il compte dans « Réussis ».
- Python et TypeScript ne comptent pas les lignes de la même façon
  (coverage.py compte les instructions, vitest les lignes où commence une
  instruction) : deux pourcentages de projets différents ne se comparent pas au
  dixième près.

### Seuils de couverture de Convex et du site (ANH-203)

Depuis le 7 octobre 2026, la couverture de Convex et celle du site décident
leurs gates : `convex-tests` et `web` échouent sous **80 % de lignes** ou sous
**80 % de branches**. Décision du chef de projet devant le premier rapport de
qualité, qui montrait le site à 21 % de lignes.

**Ce que chaque seuil juge.**

| Gate | Mesure | Exigé |
|---|---|---|
| `convex-tests` | tout `convex/` | 80 % de lignes et 80 % de branches |
| `convex-tests` | `convex/training.ts`, pris seul | 80 % de lignes et 80 % de branches |
| `convex-tests` | `convex/http.ts`, pris seul | 80 % de lignes et 80 % de branches |
| `convex-tests` | `convex/lib/auth.ts`, pris seul | 80 % de lignes et 80 % de branches |
| `web` | `lib/`, `hooks/`, `components/` et `app/`, réunis | 80 % de lignes et 80 % de branches |

Les trois fichiers Convex jugés seuls sont la part Convex de la chaîne de
sécurité : ce qu'une machine peut écrire, la demande d'arrêt, les contrôles
d'accès. Jugés seulement dans l'ensemble, ils pourraient baisser sans que rien
ne le dise, portés par les autres fichiers : avec trois fichiers de tests en
moins, `convex/` tient encore le seuil (80,7 % de branches) alors que
`training.ts` seul tombe à 75,5 %. 80 % est un plancher, pas un objectif : le
jour où le seuil a été posé, ces trois fichiers étaient à 100 % de lignes et de
branches. Le seuil de 100 % de la chaîne de sécurité du Pi et celui de la
simulation ne changent pas.

Le seuil du site porte sur l'ensemble de ses dossiers mesurés, pas sur chaque
fichier : un fichier peut rester sous 80 % si le reste le compense. Le détail
du job (« Fichiers les moins couverts ») les nomme.

**Ce qui est compté.** Tous les fichiers source des dossiers mesurés, qu'un
test les charge ou non : un fichier sans aucun test compte pour zéro, il ne
disparaît pas de la mesure. Une ligne est une ligne où commence une
instruction ; une branche est chaque issue d'un `if`, d'un opérateur ternaire,
d'un `&&`, d'un `||`, d'un `??` ou d'une valeur par défaut. Vitest affiche aussi
les instructions et les fonctions : le seuil ne les juge pas.

**Ce qui est laissé hors de la mesure**, et pourquoi. Rien d'autre ne l'est,
et rien n'a été retiré pour atteindre le chiffre :

| Mesure | Exclusion | Raison |
|---|---|---|
| Convex | `convex/_generated/**` | liaisons générées par Convex, pas écrites ici |
| Convex | `convex/**/*.test.ts` | les tests |
| Convex | `convex/**/*.fixtures.ts` | données d'essai, chargées par les tests seulement |
| Convex | `convex/test.setup.ts` | mise en place des tests |
| Convex | `convex/authorization.matrix.ts` | le tableau des droits attendus, lu par son test seulement |
| Site | `**/*.test.{ts,tsx}` | les tests |

Ces exclusions existaient dans la mesure d'ANH-199 ; elles sont maintenant
écrites à un seul endroit. Sont hors des dossiers mesurés, donc ni comptés ni
jugés : `i18n/`, `proxy.ts`, et `test-support/`, qui ne contient que des outils
de test. Les pages (`app/`) sont dans la mesure depuis ANH-204. Un cas à
connaître :
`components/markup.test-helpers.ts` est un outil de test rangé dans
`components/` ; il était compté comme une source dans la mesure d'ANH-199 et
il l'est resté (17 lignes).

**Où c'est écrit.** Une seule fois, dans `scripts/ci/coverage-thresholds.mjs` :
le seuil, les dossiers du site (par suite de tests), les fichiers Convex jugés
seuls, les exclusions. Les configurations Vitest et le rapport de qualité le
lisent là ; aucun autre fichier n'en garde une copie. Mettre un autre dossier
du site sous le seuil, c'est ajouter son nom à la suite qui lance ses tests
(`SITE.suites`) : les tests lancés, les fichiers mesurés, le seuil et le
libellé du rapport suivent.

**Comment c'est appliqué.** Dans chaque job, les tests tournent d'abord sans
mesure (`npm run test:convex` ; `npm run test:ecg` puis `npm run test:site`).
Ce sont les commandes des développeurs : elles ne mesurent rien et restent
aussi rapides qu'avant. Une étape « Enforce the coverage … » relance ensuite
les mêmes tests avec la mesure. Vitest compare lui-même le résultat au seuil
de sa configuration et termine en erreur s'il n'est pas atteint ; l'étape n'a
pas de `continue-on-error`, donc le job échoue.

Pour le site, la mesure est **une seule exécution** de tous ses tests
(`vitest.site-coverage.config.mts` : ceux de `lib/`, de `hooks/`, de
`components/` et de `app/`), et non plus deux mesures que le rapport
additionnait. C'est le
choix le plus simple et le plus sûr des deux possibles : aucun code du dépôt
ne calcule ni ne juge le chiffre, et le chiffre jugé est celui que la même
commande affiche sur le poste d'un développeur. L'autre voie, garder deux
mesures et faire juger leur somme par un script, aurait ajouté un programme à
maintenir entre la mesure et le verdict. Ce qui reste écrit ici, ce sont la
liste (`coverage-thresholds.mjs`) et les tests qui tiennent la chaîne, de la
liste à l'étape qui fait échouer le job.

**Un seuil sans objet arrête la commande.** Vitest tient pour atteint le seuil
d'un nom qui ne correspond à aucun fichier mesuré, et un dossier qui ne
contient aucun fichier sort de la mesure sans un mot. Renommer
`convex/training.ts` sans mettre la liste à jour, ou mal écrire un dossier du
site, laisserait donc la gate verte. Chaque configuration qui applique un
seuil appelle pour cela `assertMeasured` (`scripts/ci/coverage-thresholds.mjs`)
avant de se donner à Vitest : sur les listes mêmes qu'elle mesure, elle
vérifie que chaque fichier jugé seul est un fichier mesuré (présent, inclus,
non exclu) et que chaque dossier mesuré contient au moins un fichier mesuré.
Sinon la commande ne démarre pas et rend le code 1 :

```
Startup Error
Error: Coverage threshold without an object (scripts/ci/coverage-thresholds.mjs):
- "convex/training-renamed.ts" must reach the threshold alone but is not a measured file (renamed, removed or excluded?): its threshold would count as reached
```

Le contrôle est donc dans l'étape qui applique le seuil, dans les jobs
obligatoires `convex-tests` et `web`, et non dans le seul job `changes`, qui
n'est pas une vérification obligatoire. Pour Convex, `npm run test:convex`
lit la même configuration : il s'arrête lui aussi, avec le même message.
Observé en local le 7 octobre 2026 avec un nom changé dans la liste : avant
ce contrôle, `npm run coverage:convex` rendait 0 sans rien dire ; avec lui,
1 et le message ci-dessus. Même chose pour `npm run coverage:site` avec un
dossier mal écrit. Le rapport, de son côté, écrit « indisponible », jamais
« tenu », pour un fichier qu'il ne trouve pas dans la mesure.

Ce contrôle lit les listes et le disque, pas le résultat de la mesure : il
tient pour mesuré ce que les listes disent mesurer. Le jour où il a été
écrit, les deux ensembles étaient les mêmes (21 fichiers pour Convex, 78 pour
le site, comparés au rapport de couverture ; 99 pour le site depuis que
`app/` est dans la liste).

**Lancer le même contrôle en local.**

```bash
npm run coverage:convex   # échoue sous le seuil ; rapport dans coverage/convex/index.html
npm run coverage:site     # tous les tests du site en une fois ; rapport dans coverage/site/index.html
```

Les deux commandes rendent le code 1 sous le seuil, comme dans la CI.

**Lire un échec.** L'étape « Enforce the coverage of the Convex functions »
(job `convex-tests`) ou « Enforce the coverage of the site » (job `web`) est
rouge. Ses dernières lignes disent quelle mesure manque, et de combien :

```
ERROR: Coverage for branches (79.25%) does not meet global threshold (80%)
ERROR: Coverage for branches (75.51%) does not meet "convex/training.ts" threshold (80%)
```

« global » désigne l'ensemble de la mesure ; un nom de fichier entre
guillemets, ce fichier pris seul. Si un test échoue, la même étape est rouge
aussi, mais l'étape des tests l'est déjà au-dessus : commencer par elle. Si
l'étape s'arrête sur « Startup Error » et « Coverage threshold without an
object », aucun test n'a tourné : un fichier ou un dossier nommé par la liste
n'est plus mesuré (voir « Un seuil sans objet arrête la commande »).

Pour savoir quoi couvrir : le résumé du job (« Détail de la qualité ») liste
les dix fichiers les moins couverts ; le rapport ligne par ligne est dans
l'artefact du job (`quality-convex` : `coverage-convex/index.html` ;
`quality-site` : `coverage-site/index.html`), et en local dans
`coverage/convex/` ou `coverage/site/`. La réponse est d'écrire le test qui
manque, ou de retirer du code qui ne sert plus. Baisser le seuil ou sortir un
fichier de la mesure est une décision à part : une exclusion nouvelle doit
figurer dans le tableau ci-dessus avec sa raison, sans quoi un test de la CI
échoue.

**Dans le rapport de qualité.** La partie « Seuils de couverture, chaîne de
sécurité et simulation » a une ligne par mesure jugée, dans les mêmes termes
que celles du Pi : « Couverture, `convex/` », une ligne « Couverture, chaîne
de sécurité côté Convex, `convex/training.ts` pris seul » par fichier jugé
seul, et « Couverture, `lib/`, `hooks/`, `components/`, `app/` », chacune avec ses
chiffres et « 80 % exigé par `convex-tests` » ou « par `web` », suivi de
« ✅ tenu » ou de « ❌ non tenu ». L'annotation « Rapport de qualité » le redit
en une ligne par projet, et `quality-report.json` porte `threshold` et `held`
pour chaque mesure.

**Chiffres.** Mesurés le 7 octobre 2026, avant ce travail (commit `fbfc1b5`,
première mesure du rapport de qualité) et après, en lignes puis en branches :

| Mesure | Avant | Après |
|---|---|---|
| `convex/` | 96,6 % (1 366 sur 1 413), 87,4 % (869 sur 994) | 97,8 % (1 382 sur 1 413), 92,7 % (922 sur 994) |
| `convex/training.ts` | 94,3 % (267 sur 283), 80,9 % (195 sur 241) | 100 % (283 sur 283), 100 % (241 sur 241) |
| `convex/http.ts` | 100 % (98 sur 98), 93,2 % (96 sur 103) | 100 % (98 sur 98), 100 % (103 sur 103) |
| `convex/lib/auth.ts` | 100 % (122 sur 122), 100 % (102 sur 102) | inchangé |
| site, `lib/` | 82,4 % (94 sur 114), 65,6 % (65 sur 99) | 100 % (114 sur 114), 98,9 % (98 sur 99) |
| site, `hooks/` | 72,2 % (26 sur 36), 100 % (11 sur 11) | 100 % (36 sur 36), 100 % (11 sur 11) |
| site, `components/` | 13,5 % (153 sur 1 133), 20,7 % (221 sur 1 067) | 99,7 % (1 130 sur 1 133), 98,1 % (1 047 sur 1 067) |
| site, les trois dossiers | 21,2 % (273 sur 1 283), 25,2 % (297 sur 1 177) | 99,7 % (1 280 sur 1 283), 98,2 % (1 156 sur 1 177) |
| site, `app/` (ANH-204) | 0 % (0 sur 573), 0 % (0 sur 470) | 100 % (573 sur 573), 97,0 % (456 sur 470) |
| site, les quatre dossiers, ce que `web` juge | 14,7 % (273 sur 1 856), 18,0 % (297 sur 1 647) | 99,8 % (1 853 sur 1 856), 97,9 % (1 612 sur 1 647) |

Aucun fichier de `lib/`, de `hooks/` ni de `components/` n'est sous 80 % : le
moins couvert en branches est `components/ui/globe.tsx` (84,6 %, 11 sur 13).
Trois fichiers de `app/` le sont en branches, tous à 100 % de lignes, pour des
branches que l'écran ne peut pas produire (la liste est dans
[Tests des pages du site](#tests-des-pages-du-site-anh-204)) : la liste des
patients (11 sur 14), la FAQ (1 sur 2) et la mise en page racine (3 sur 4). Le
seuil, global, les laisse passer. Les branches qui restent sont,
pour l'essentiel, des gardes qu'aucun parcours n'atteint (une référence
d'élément nulle dans un gestionnaire de clic, un texte de repli derrière une
traduction qui existe, un bouton désactivé dont le gestionnaire revérifie la
condition) : elles n'ont pas été forcées. Le site est passé de 329 tests à 1 321, Convex de
954 à 1 044. Avec les tests des pages (ANH-204), le site en compte 1 707.

**Temps ajouté aux jobs.** Mesuré le 7 octobre 2026 sur les deux exécutions
de la PR #45 (37648336769 puis 37648970197), comparées étape par étape au push
sur `develop` qui les précède (37637092583) :

| Job | Étape | Sur `develop` | PR, première exécution | PR, seconde exécution |
|---|---|---|---|---|
| `convex-tests` | `npm run test:convex` | 6 s (954 tests) | 6 s | 6 s (1 044 tests) |
| `convex-tests` | mesure de la couverture | 7 s, sans seuil | 6 s | 7 s, avec le seuil |
| `convex-tests` | le job entier | 49 s | 45 s | 47 s |
| `web` | `npm run test:ecg` | 1 s (102 tests) | 1 s | 2 s (172 tests) |
| `web` | `npm run test:site` | 9 s (227 tests) | 10 s | 17 s (1 132 tests) |
| `web` | mesure de la couverture | 14 s, deux exécutions sans seuil | 13 s | 22 s, une exécution avec le seuil |
| `web` | le job entier | 105 s | 77 s | 127 s |

Pour Convex, rien ne change : la mesure existait depuis le rapport de qualité,
elle est seulement jugée, et 90 tests de plus ne se voient pas. Pour `web`, le
seuil n'ajoute pas d'étape non plus, mais les 975 tests ajoutés au site
tournent deux fois, une fois sans mesure et une fois avec. Ce qu'ils coûtent
dépend du runner. La seconde exécution a eu un runner comparable à celui de
`develop` (`tsc` 11 s contre 10 s, lint 14 s contre 12 s, construction 25 s
contre 24 s) : `npm run test:site` y prend 8 s de plus et la mesure 8 s de
plus, soit environ 16 s sur `web`. La première a eu un runner plus rapide
(`tsc` 7 s, lint 8 s, construction 15 s) et ne montre presque aucun écart.
Trois exécutions en tout : c'est un ordre de grandeur, pas une moyenne.

**Tests du mécanisme.** `scripts/ci/ci-workflow.test.mjs` tient la chaîne :
les deux étapes existent, n'ont pas de `continue-on-error`, lancent le script
du `package.json` sans option qui retire le seuil ou change ce qui est mesuré ;
chaque script lance la configuration attendue avec `--coverage` ; chaque
configuration prend ses fichiers et son seuil dans la liste et n'en fixe aucun
elle-même ; les commandes `npm run test:*` et leurs configurations ne mesurent
rien ; la couverture n'est mesurée nulle part ailleurs dans le workflow.
`scripts/ci/quality-report.test.mjs` vérifie la règle (80 % est tenu à 80 %,
79,9 % ne l'est pas, sur les lignes comme sur les branches), l'affichage
« tenu », « non tenu » et « indisponible », que chaque fichier jugé seul
existe et reste dans la mesure, et que cette section nomme le seuil, chaque
dossier, chaque fichier jugé seul et chaque exclusion. Il exerce aussi
`assertMeasured` sur un petit dépôt d'essai (fichier renommé, fichier exclu,
dossier vide ou ne contenant que des tests), et `ci-workflow.test.mjs`
vérifie que les deux configurations l'appellent, avant de se définir et sans
l'adoucir.

Ces tests lisent des fichiers : ils ne lancent pas Vitest (le job `changes`
n'installe rien). Que la commande échoue réellement sous le seuil a été
vérifié à la main le 7 octobre 2026, avec les commandes ci-dessus et l'option
`--exclude` de Vitest pour retirer des tests : le site sans les tests de
`components/modals/` ni de `components/ui/` (41,0 % de lignes, 47,4 % de
branches) a rendu le code 1 avec les deux lignes « does not meet global
threshold » ; Convex sans trois de ses fichiers de tests a rendu le code 1 sur
`convex/training.ts` seul (75,5 % de branches), l'ensemble tenant encore le
seuil.

**Limites.**

- Une ligne exécutée n'est pas une ligne vérifiée : le seuil compte ce que les
  tests exécutent, pas ce qu'ils affirment. La relecture reste ce qui écarte
  un test sans affirmation.
- Les composants sont testés sans navigateur (voir
  [Tests unitaires du site](#tests-unitaires-du-site)), les pages dans un DOM
  simulé (voir [Tests des pages du site](#tests-des-pages-du-site-anh-204)) : ce
  qui est couvert est leur logique, pas leur rendu dans un navigateur.
- Le seuil du site est global : il ne protège pas un fichier en particulier.
  Seuls les trois fichiers Convex de la chaîne de sécurité ont leur seuil
  propre.

### Lire un échec et relancer

Dans la PR, ouvrir **Checks**, puis le job rouge et la première étape en échec.
Les gates continuent après une erreur afin de montrer tous les contrôles cassés.
Pour la couverture, télécharger l'artefact et lire les lignes/branches manquantes
avec le rapport terminal ; ne pas baisser le seuil. Pour celle de Convex et du
site, voir « Lire un échec » dans
[Seuils de couverture de Convex et du site](#seuils-de-couverture-de-convex-et-du-site-anh-203). Pour le rapport simulation,
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

Un bouton de déploiement rouge dit pourquoi en tête de la page de l'exécution :
« Mauvaise branche », « Confirmation refusée », « Accord manquant », « Secret
manquant », « Exécution périmée », « Simulation absente » ou « Adresse
absente », chaque fois avec ce qu'il faut faire. Les trois premiers viennent du
job qui vérifie la demande : rien n'a été déployé. Les trois suivants viennent
d'un job de déploiement, avant tout appel à Vercel : ce job n'a pas déployé son
projet, mais avec `site et simulation` l'autre job a pu déployer le sien.
« Adresse absente » vient après l'appel à Vercel : vérifier dans Vercel si le
déploiement a eu lieu. Tout autre échec vient de la CLI Vercel ou de la
construction du site : lire le journal de l'étape. **Pour réessayer, lancer une
nouvelle exécution par « Run workflow »** plutôt que « Re-run » : une exécution
relancée garde ses saisies et son commit, et elle est refusée dès que la
branche a avancé (voir
[Déploiement Vercel par bouton](#déploiement-vercel-par-bouton-anh-198)).

### Tests unitaires du site

`npm run test:ecg` lance les tests de `lib/` (configuration
`vitest.ecg.config.mts`, environnement Node), `npm run test:site` ceux de
`hooks/`, de `components/` et des pages de `app/` (`vitest.site.config.mts`).
Aucune des deux ne
mesure la couverture : `npm run coverage:site` lance les deux ensemble avec la
mesure et son seuil (voir
[Seuils de couverture de Convex et du site](#seuils-de-couverture-de-convex-et-du-site-anh-203)). Le nom du script date de la
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
- `convex/journalSync.test.ts` (`npm run test:convex`) vérifie ce que le
  serveur garde d'une séance envoyée tard ou deux fois : un lot renvoyé ne
  change ni les lignes ni la date de leur première réception. Il rejoue deux
  consoles. Celle d'aujourd'hui, qui ne dit pas l'âge de sa séance : ses dates
  sont stockées et servies comme avant (horloge reculée de 10 min en cours de
  séance, machine datée de 1970 déclarée en retard : aucun point refusé,
  jamais lue comme en direct). Celle qui dit son âge : `lastMeasuredAt` et le
  `t` des courbes sont sur l'horloge du serveur quelle que soit l'heure de la
  machine, et ce qu'elle envoie en retard se lit comme mesuré quand il l'a
  été ;
- `convex/offlineThreshold.test.ts` (`npm run test:convex`) remplace le seuil
  partagé par une autre valeur : la tâche `checkOfflineMachines` doit la
  suivre.

Limites. Ces tests n'utilisent pas de bibliothèque de test avec DOM : le
hook tourne sur un hôte minimal (un composant qui ne rend rien), et les
composants sont rendus en HTML statique, une fois à la réception d'une réponse
et une fois plus tard. Les tests écrits depuis montent les composants sur le
document de `test-support/` (voir
[Tests des composants du site, sans navigateur](#tests-des-composants-du-site-sans-navigateur-anh-203)).
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

Ces tests ne montent aucune page. Ceux des pages
([Tests des pages du site](#tests-des-pages-du-site-anh-204)) le font : ils
vérifient, page par page, que le refus du serveur arrive dans la liste des
messages (une suppression refusée d'une machine en séance, par exemple) et que
la page ne fait pas comme si l'action avait réussi. Le dessin du message par
`components/FeedbackToaster.tsx` dans un vrai navigateur, et le scénario de
bout en bout contre un vrai serveur, restent à faire dans le job navigateur
d'ANH-83.

### Tests des composants du site, sans navigateur (ANH-203)

Pour tenir le seuil de 80 %, chaque composant de `components/`, chaque hook et
chaque règle de `lib/` a ses tests. Ils tournent avec `npm run test:site`
(`hooks/`, `components/`) et `npm run test:ecg` (`lib/`), sans navigateur. Ils
n'utilisent aucune bibliothèque de DOM et n'ajoutent aucune dépendance.

**Quel test va sur quel document.** Le dépôt a deux documents de test, et un
test n'en utilise qu'un. Les composants, les hooks et les règles de `lib/` sont
testés sur le document minimal de `test-support/`, décrit ci-dessous. Les
pages (`app/`) sont testées avec la bibliothèque standard (jsdom et
Testing Library, que chaque fichier de test de page demande lui-même), décrite
dans [Tests des pages du site](#tests-des-pages-du-site-anh-204). Le
remplaçant de Convex, lui, est le même pour tous (`test-support/convex.ts`).
Pour un nouveau test de composant : le document minimal convient tant que le
test n'a besoin de rien de ce que ce document n'implémente pas (la liste est
sous « Limites »). Sinon le test s'écrit avec la bibliothèque standard, comme
ceux des pages, ou attend la suite navigateur (ANH-83) : on n'étend pas le
document minimal pour imiter un navigateur.

**Le document des tests : `test-support/`.** Le rendu en HTML statique des
sections précédentes ne suffit pas à un formulaire : il ne garde pas d'état
entre deux actions. `test-support/` fournit donc un document minimal sur
lequel le vrai React tourne (état, effets, nouveau rendu après un événement) :

| Fichier | Ce qu'il fournit |
|---|---|
| `test-support/dom.ts` | le document : ce que React demande à un document (éléments, texte, attributs, événements qui descendent et remontent), et rien de ce qu'un navigateur ajoute (ni mise en page, ni focus, ni CSS) |
| `test-support/render.tsx` | `render(<Composant />)` sous le vrai fournisseur `next-intl` avec les vrais `messages/`, puis `click`, `type`, `submit`, `fire`, et de quoi lire l'écran (`screen.text()`, `screen.button(libellé)`, `screen.field(id)`). Doit être le premier import du fichier de test : React décide au chargement s'il a un document |
| `test-support/convex.ts` | ce qui remplace `convex/react` : `answer(nom, valeur)` fixe la réponse d'une requête, `mutation(nom)` est la fonction appelée, `asks(nom)` les arguments demandés (ou `"skip"`), `sessionIs(état)` dit si le visiteur est connecté (`Authenticated`, `Unauthenticated`, `AuthLoading`, `useConvexAuth`). `useMutation` rend la même fonction à chaque rendu, comme le vrai. `useMutationWithFeedback` reste le vrai, par-dessus |
| `test-support/ui.tsx` | des remplaçants nommés pour nos fenêtres, listes de choix et cases à cocher (`components/ui/dialog`, `select`, `checkbox`), pour tester le composant qui s'en sert |
| `test-support/radix.tsx` | des remplaçants pour les primitives Radix, pour tester nos propres enveloppes de `components/ui/` |
| `test-support/browser.ts` | ce qu'un navigateur ajoute et que certains composants demandent : horloge d'images d'animation, canevas, largeur de fenêtre, observateurs. Chaque pièce est installée et pilotée par le test |
| `test-support/markup.ts` | lecture d'un rendu statique comme un arbre, pour les composants sans état |
| `test-support/pages.tsx` | l'aide des tests de pages, qui tournent dans jsdom et non sur ce document : rendu d'une page, navigation remplacée, et les fonctions de Convex nommées par leur référence, par-dessus `test-support/convex.ts` (voir [Tests des pages du site](#tests-des-pages-du-site-anh-204)) |

Ce dossier est hors de la mesure de couverture (ce ne sont pas des sources du
site) et la règle de chemins le range avec le site.

**Ce qui est remplacé, et pourquoi.** Les primitives Radix (fenêtre, liste,
menu, infobulle) demandent un navigateur : mise en page, focus, portails. Elles
ne tournent pas sur ce document. Un test de composant remplace donc l'enveloppe
concernée par un remplaçant nommé qui garde ce dont le test a besoin (une
fenêtre n'affiche son contenu qu'ouverte et peut être fermée, un choix peut
être fait) ; l'enveloppe réelle a son propre test dans `components/ui/`, où
c'est la primitive Radix qui est remplacée. De même pour les graphiques
(`recharts`), le globe et les animations : le test lit ce que notre code leur
donne à dessiner. Tout le reste est réel : React, `next-intl` et les
catalogues, `react-hook-form` et `zod`, `date-fns`, les règles de `lib/`.

**Ce que chaque test doit affirmer.** Ce que l'écran montre pour un rôle et un
état donnés (chargement, vide, erreur, données périmées), ce qu'une action
envoie (la mutation exacte et ses arguments), ce qui est refusé avant tout
envoi, et le message affiché ensuite. Un test qui monte un composant sans rien
affirmer ne compte pas. Un échantillon de ces tests a été vérifié par mutation
(on casse le comportement dans la source, le test doit échouer, la source est
restaurée).

**Les fichiers.**

- Séance, lancement, arrêt, droits : `components/training/TrainingPanel.test.tsx`
  (la fenêtre de confirmation de l'arrêt avec son état réel, qui voit le
  bouton, ce que le panneau dit de chaque état d'une séance),
  `components/modals/LaunchTrainingModal.test.tsx` (qui peut être choisi comme
  pratiquant par chaque rôle, ce qui bloque un lancement avant tout envoi, ce
  qui est envoyé, le refus du serveur), `components/training/LaunchRightsCard.test.tsx`
  (un gestionnaire ne se voit proposer que ses patients, accorder et retirer),
  `components/training/PhysiologyCard.test.tsx`,
  `components/training/TelemetryCharts.test.tsx`,
  `components/training/training-cards.test.tsx` (détail d'une séance,
  programmes, type et origine, valeurs en direct, bouton de lancement de « Mes
  machines »).
- Formulaires : `components/modals/MachineFormModal.test.tsx` (création, clé
  API affichée une fois, liste des gestionnaires envoyée par un administrateur
  seulement) et `components/modals/PatientFormModal.test.tsx`.
- Cadre du site : `components/dashboard/dashboard-shell.test.tsx` (le menu de
  chaque rôle, le fil d'Ariane, les libellés de statut),
  `components/landing/landing.test.tsx`, `components/site-shell.test.tsx`
  (messages après une action, langue, thème),
  `components/ConvexClientProvider.test.tsx`.
- `components/ui/` : un fichier de test par composant, ou par petit groupe
  (`plain-elements.test.tsx`).
- `hooks/use-mutation-with-feedback.stable.test.tsx` (la fonction rendue par
  le hook est la même à chaque rendu : une page peut la nommer dans les
  dépendances d'un effet) ;
- `hooks/use-mobile.test.tsx` ; `lib/trainingRules.test.ts` (FC max retenue,
  plafond de zone, durées), `lib/feedback.test.ts`, `lib/version.test.ts`.
- Convex : `convex/trainingBranches.test.ts` (`npm run test:convex`) couvre les
  refus d'un lancement, la demande d'arrêt jusqu'à la machine, les droits, la
  physiologie et ce qu'une machine peut écrire.

**Limites.**

- Ce document n'est pas un navigateur. Ce qui est prouvé est la logique des
  composants : ce qu'ils affichent, envoient et refusent. Le rendu réel, le
  focus, le clavier, les fenêtres Radix telles qu'un navigateur les ouvre et
  la mise en page restent à vérifier dans la suite navigateur (ANH-83).
- Un remplaçant ne vaut que par sa fidélité à ce qu'il remplace : un
  comportement attribué à une primitive Radix dans un test l'est d'après sa
  documentation, pas d'après une exécution.
- Le document minimal a été écrit d'après ce que React lui demande. Aucun test
  ne le compare à un vrai DOM.
- Le remplaçant de Convex (`test-support/convex.ts`) n'a pas de serveur : une
  réponse ne change que si le test appelle `answer` et rend de nouveau (aucun
  abonnement), les arguments ne sont pas comparés aux validateurs d'une
  fonction, un nom mal écrit dans `answer` ne répond rien sans le dire, et
  aucune règle de `convex/` ne tourne. Il ne fournit que ce que le site
  importe de `convex/react` aujourd'hui : ni action, ni requête paginée, ni
  mise à jour optimiste.

**Ce que le document minimal n'implémente pas.** Un test écrit dessus ne
prouve donc rien de ce qui suit, quel que soit son résultat :

- **Aucune action par défaut du navigateur.** Un clic sur un bouton d'envoi
  n'envoie pas le formulaire, la touche Entrée non plus : les tests appellent
  `submit(formulaire)`. Un bouton du mauvais `type` n'est donc pas vu par un
  test qui agit seulement : un bouton « Annuler » sans `type="button"`
  enverrait le formulaire dans un navigateur, et rien ici ne le montrerait.
  Les tests des quatre formulaires (lancement, machine, patient, physiologie)
  lisent pour cela le type de chaque bouton (`buttonsOf(formulaire)`) : un
  seul envoie, tous les autres sont de simples boutons. Tout nouveau
  formulaire doit avoir ce test. Un clic sur un libellé ne donne pas le focus
  à son champ, un lien ne navigue pas, une case native ne se coche pas.
- **Aucune mise en page ni CSS.** Chaque élément est un point à l'origine, de
  taille nulle ; une classe est un texte. `screen.text()` lit tout ce qui est
  dans l'arbre, y compris ce qu'une classe ou un style cacherait à l'écran
  (`hidden`, `sr-only`, `display: none`) : un test prouve qu'un élément est
  présent ou absent, pas qu'il est visible.
- **Aucune règle de focus ni de clavier.** `focus()` note l'élément, rien de
  plus : pas d'ordre de tabulation, pas de focus retenu dans une fenêtre.
- **Un seul événement à la fois.** `click` envoie `click`, sans les événements
  de pointeur qui le précèdent dans un navigateur ; `type` envoie `input` et
  `change`, sans les touches. Un élément `disabled` n'est respecté que par
  l'outil `click`.
- **Aucun champ ne filtre ni ne valide.** Dans un navigateur, un champ
  `type="number"` ne rend jamais un texte comme « abc » (il rend une valeur
  vide), et `min`, `max`, `step`, `required` ou `type="email"` empêchent
  l'envoi d'une valeur hors règle. Ici `type(champ, texte)` écrit n'importe
  quel texte dans n'importe quel champ et `submit` l'envoie. Un test qui tape
  « abc » ou « 12.5 » dans la durée d'un lancement prouve ce que le composant
  fait si une telle valeur lui parvient, pas qu'un utilisateur peut la
  saisir : ces cas sont nommés ainsi dans `LaunchTrainingModal.test.tsx`, qui
  lit par ailleurs les attributs `type`, `min` et `step` du champ sans les
  exercer.
- **Aucune liste `<select>` native, aucun sélecteur (`querySelector`), aucune
  lecture de HTML (`innerHTML`).**
- **Aucun arbre d'accessibilité.** Un rôle ou un nom accessible est un
  attribut lu tel qu'il est écrit : rien ne calcule ce qu'un lecteur d'écran
  annoncerait.
- **Rien de ce que le test n'installe pas** : primitives Radix, portails,
  canevas, images d'animation, observateurs, presse-papiers, largeur de
  fenêtre. Ils viennent de remplaçants nommés (`test-support/ui.tsx`,
  `radix.tsx`, `browser.ts`), installés par le test qui en a besoin.

### Tests des pages du site (ANH-204)

`npm run test:site` exécute aussi un fichier de test par page de `app/` : les
19 pages et les 2 mises en page (`layout.tsx`), soit 21 fichiers source, 22
fichiers de test et 386 tests au 7 octobre 2026. Chaque fichier est à côté de la
page qu'il teste (`page.test.tsx` à côté de `page.tsx`). Next.js ne prend pour
une route qu'un fichier nommé exactement `page`, `layout` ou `route` : un
fichier `page.test.tsx` n'en crée pas.

**Un DOM, pour les pages seulement.** Une page a un état, des effets et des
fenêtres Radix réelles : le document minimal des composants ne les fait pas
tourner. Les tests des pages tournent donc dans `jsdom`, avec Testing Library
(`@testing-library/react`, `@testing-library/user-event`) : un clic est un
vrai clic sur le vrai bouton, une fenêtre est la vraie fenêtre, une liste
déroulante la vraie liste. Chaque fichier de test de page demande cet
environnement lui-même, par sa première ligne
(`// @vitest-environment jsdom`) : la suite reste sur l'environnement `node`
pour `hooks/` et `components/`.

**Ce qui est remplacé, et ce qui ne l'est pas.** L'aide des pages est
`test-support/pages.tsx`.

| Élément | Dans les tests des pages |
|---|---|
| Convex (`convex/react`) | le remplaçant de tout le site, `test-support/convex.ts` : il n'y en a qu'un. `test-support/pages.tsx` n'y ajoute qu'une couche qui nomme une fonction par sa référence (`api.machines.getMachine`) au lieu d'un texte : `answer(référence, valeur)`, `argsAsked(référence)` (les arguments donnés par la page, `"skip"` compris), `mutation(référence)`, `mutationsSent()` (chaque mutation envoyée, dans l'ordre, avec ses arguments exacts), `sessionIs(état)` |
| Réponses du serveur | celles des **requêtes** sont typées par le type de retour de la fonction Convex (`FunctionReturnType`) : une réponse de test que le serveur ne peut pas rendre ne compile pas (`npx tsc --noEmit`). Celles des mutations ne le sont pas |
| Échec d'une mutation | sous les formes que le client Convex livre, jamais une erreur nue : une `ConvexError` portant la phrase du serveur quand le serveur formule le refus (lancement, arrêt, rôles, organisation), et sinon l'erreur de serveur masquée en production ou détaillée sur un déploiement de développement (`serverFailures`, qui donne les deux avec le message attendu pour chacune) |
| Navigation (`@/i18n/navigation`) | remplacée : un lien est une ancre qui porte l'adresse donnée par la page, avant le préfixe de langue ; `router.push` est lu par le test |
| Messages | les vrais : fournisseur `next-intl` et catalogues `messages/fr.json` et `messages/en.json`. Un message demandé et absent du catalogue fait échouer le test |
| Retour des mutations | le vrai hook `useMutationWithFeedback` et la vraie liste de messages (`lib/feedback.ts`), lue par `feedbackShown()` |
| Rôle du visiteur | `signedInAs("admin" \| "org_admin" \| "gestionnaire" \| "user")` fait répondre ce compte à `users.getCurrentUser` ; un compte sans ligne Convex se dit `answer(api.users.getCurrentUser, null)` |
| Composants de `components/ui/` (boutons, fenêtres, listes, cases, onglets) | les vrais, Radix compris |
| Fenêtre de lancement et panneau d'entraînement | les vrais sur « Mes machines », la page d'une machine et la vue en direct : le lancement et l'arrêt affirmés sont ceux que la page envoie vraiment |
| Autres cartes et fenêtres de `components/` qui ont leurs propres requêtes et mutations (fenêtres machine et patient, droits de lancement, physiologie, état en direct, programmes, carte Entraînement) | remplacées par un marqueur (`standIn`) qui garde les propriétés reçues : le test lit ce que la page leur donne (`propsOf`) et appelle ce qu'elle écoute. Leur propre comportement a ses tests dans `components/` |
| Courbes (Recharts), globe, particules, faisceaux, rayons | remplacés : ils dessinent sur un canevas ou demandent une vraie mise en page |
| Horloge | la date du poste est fixée (`NOW`) ; les minuteries tournent normalement |
| `console.error` | une ligne inattendue (avertissement de React, erreur que rien n'attrape) fait échouer le test ; l'échec d'une mutation, que le hook journalise, doit être lu par `takeLoggedFailures()`, sans quoi le test échoue aussi |

**Limites du remplaçant de Convex, à connaître avant de s'appuyer sur ces
tests.** Elles s'ajoutent à celles que la section précédente donne pour
`test-support/convex.ts`.

- Une réponse est rangée par nom de fonction, pas par arguments. Une page qui
  demanderait la bonne fonction avec de mauvais arguments recevrait la même
  réponse : un argument faux n'est vu que là où le test lit `argsAsked`. Les
  tests le font pour chaque requête dont les arguments décident de ce que le
  serveur rend (identifiant de la route, `includeDeleted`, filtre de rôle,
  `"skip"`), par discipline et non par construction.
- Changer les arguments d'une requête ne repasse pas par le chargement. Le
  vrai client rend `undefined` jusqu'à la nouvelle réponse (le filtre de rôle
  de la liste des utilisateurs, `includeDeleted` de la liste des machines quand
  le rôle arrive) ; ici la nouvelle réponse est là au rendu suivant. L'état
  intermédiaire de ces deux pages n'est donc pas exercé.
- L'état « on ne sait pas encore si le visiteur est connecté » existe
  (`sessionIs("loading")`) et la page d'accueil est testée dans cet état. Les
  pages du tableau de bord ne le lisent pas : elles attendent
  `users.getCurrentUser`.

**Ce qu'un test de page affirme.** Pour chaque page : ce qu'elle demande à
Convex et avec quels arguments ; ses états de chargement, vide et
introuvable ; ce que chaque action envoie (la mutation et ses arguments
exacts), ce qu'elle affiche ensuite, où elle mène ; ce qu'elle n'envoie pas
quand la confirmation est refusée ou que le bouton est grisé ; ce qu'elle
affiche quand le serveur refuse, la fenêtre concernée relue à l'écran après
le refus. Pour les pages qui lisent le rôle du visiteur (accueil du tableau de
bord, liste et page des machines, « Mes machines », gestionnaires, fiches
patient et utilisateur, paramètres) : ce qu'elles montrent et cachent à chacun
des quatre rôles (admin d'Anheart, admin d'une organisation cliente,
gestionnaire, patient) et à un compte sans ligne Convex. Les trois pages de
séances, les listes des patients et des utilisateurs et Rapports ne lisent
aucun rôle : elles montrent ce que le serveur rend (le droit d'arrêter une
séance, `canStop`, compris), et leurs tests le disent ainsi.

Un test qui monte une page sans rien affirmer d'elle n'a pas sa place : chaque
test porte dans son nom le comportement qu'il vérifie et échoue si ce
comportement est cassé. Deux échantillons de mutations l'ont vérifié le
7 octobre 2026, en cassant un comportement à la fois dans les pages (un rôle
élargi, un argument faux, une confirmation ignorée, un filtre retiré, un bouton
laissé actif pendant l'envoi, une fenêtre fermée malgré un refus) : 56 par
l'auteur, tous détectés ; 71 par la revue indépendante, dont 68 détectés, les
trois autres ayant depuis leur test.

Une organisation ne change rien à ce qu'une page dessine : c'est le serveur qui
ne rend que les machines, les comptes et les séances de l'organisation de
l'appelant ([convex.md](convex.md#3-règles-dautorisation)). Les tests des
pages vérifient ce que la page fait d'une réponse vide ou nulle, qui est ce
que le serveur rend pour une ressource d'une autre organisation.

**Comportement actuel, noté comme tel.** Quand un test décrit ce que la page
fait aujourd'hui sans que ce soit forcément voulu, un commentaire au-dessus de
lui commence par « What the page does today » ou « Known and filed ». Corriger
ce comportement fera échouer ce test, et c'est attendu : le test se change avec
la correction. Sont notés ainsi, entre autres : aucune page ne connaît le rôle
d'admin d'une organisation cliente, qui voit donc ce que voit un patient ;
plusieurs textes sont écrits en anglais dans les pages ; les durées « il y a… »
de quatre pages se comptent sur l'horloge du poste ; aucune page n'a d'écran
d'erreur (il n'y a pas de `error.tsx` sous `app/`), si bien qu'une requête
refusée remonte à ce qui entoure la page.

**Les parties serveur.** `app/` n'a ni gestionnaire de route (`route.ts`), ni
fichier de métadonnées, ni `error.tsx`, ni `loading.tsx`. Ses deux mises en
page sont testées ainsi :

| Fichier | Comment il est testé | Ce que ces tests n'exercent pas |
|---|---|---|
| `app/[locale]/layout.tsx` (composant serveur, fonction asynchrone) | `layout.test.tsx`, sans jsdom : la fonction est appelée comme telle et son résultat est rendu en balisage. Langue du document, refus d'une langue inconnue avant toute lecture de messages, ordre des fournisseurs, langue des fenêtres Clerk, objet `metadata`, `generateStaticParams` | le chargement des polices (`next/font/google` demande le compilateur de Next.js), la feuille de style (elle passe par la chaîne PostCSS de la construction), les vrais fournisseurs (Clerk, client Convex, lecture des messages de la requête), l'injection de `metadata` dans la page par Next.js |
| `app/[locale]/dashboard/layout.tsx` (composant client) | `layout.test.tsx` dans jsdom (témoin `sidebar_state`, place de la page dans le cadre) et `layout.server.test.tsx` sans jsdom (rendu serveur, sans document : barre latérale ouverte) | la barre latérale elle-même (`components/ui/sidebar`), l'en-tête et le menu, remplacés par des marqueurs |

`proxy.ts` (connexion exigée pour le tableau de bord, préfixe de langue) est à
la racine du dépôt, hors de `app/` : il n'est ni testé ici ni mesuré.

**Ce que jsdom ne montre pas.** Ni la mise en page ni le style (une colonne
tronquée, un bouton hors de l'écran), ni le dessin des courbes, ni la
navigation réelle (préfixe de langue, retour arrière), ni un vrai abonnement
Convex (reconnexion, ordre d'arrivée des réponses), ni les fenêtres de Clerk,
ni le presse-papiers du navigateur (celui du test est un double). Tout cela
attend la suite navigateur (ANH-83).

**Couverture de `app/`.** Avant ces tests, aucun test ne chargeait une page :
0 ligne sur 573. Au 7 octobre 2026 : 573 lignes sur 573, et 456 branches sur
470 (97,0 %). Les 14 branches non atteintes sont des cas que la page ne peut
pas produire à l'écran : une garde derrière un bouton grisé ou une fenêtre
fermée (6), l'en-tête de regroupement de la bibliothèque de tableaux, que ces
tableaux à une ligne d'en-tête n'ont pas (4), une langue autre que `fr` et
`en`, que la mise en page racine refuse (2), un libellé du catalogue qui
serait vide (1), un rôle que le serveur ne rend pas (1). Elles ne sont pas
retirées de la mesure. `app/` est sous le seuil de 80 % du site, avec
`lib/`, `hooks/` et `components/`
([Seuils de couverture de Convex et du site](#seuils-de-couverture-de-convex-et-du-site-anh-203)).

**Temps ajouté au job `web`.** Chaque fichier de test de page ouvre son propre
DOM et charge la page avec ses composants, et les tests des pages tournent
deux fois, une fois sans mesure et une fois avec. Mesuré sur l'exécution
37652958943 du 7 octobre 2026 (PR #42, avant la fusion du seuil, quand la
suite ne comptait que les tests d'origine et ceux des pages) :
`npm run test:site` 41 s et la mesure de couverture 50 s, contre 9 s et 14 s
sur `develop` ; le job entier 2 min 54 s, contre 1 min 25 s à 1 min 47 s sur
les trois exécutions de `develop` qui précèdent.

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

## 16. Rejouer une séance enregistrée

### 16.1 Ce que c'est

Un enregistrement de séance au schéma 2 ([enregistrement.md](enregistrement.md))
contient ce que le runtime a reçu pendant la séance : l'instant de chaque tic,
ce que le variateur a répondu, l'ECG brut, et ce qu'on lui a demandé. Le rejeu
redonne tout cela, dans l'ordre et aux instants enregistrés, à un **vrai**
`TrainingRuntime` neuf :

* le variateur est un « magnétophone » (`simulation/replay_tape.py`) qui répond
  à chaque appel ce que `drive_frames.jsonl` a enregistré, et qui vérifie
  d'abord que l'appel est bien celui qui a été enregistré ;
* l'ECG repasse bloc par bloc dans le vrai DSP et le vrai `EcgBridge` ;
* les commandes de `events.jsonl` sont redonnées à leurs instants ;
* l'horloge est une `ManualClock` qui avance par les intervalles enregistrés.

À chaque tic, ce que le runtime **décide** (consigne, action de sécurité,
règle, phase) est comparé à ce que l'enregistrement dit qu'il avait décidé.
Une séance enregistrée devient ainsi un test de non-régression : le runtime
d'aujourd'hui doit décider, sur les mêmes entrées, ce que celui du jour de la
séance a décidé.

> **Limite.** Aujourd'hui, seuls les enregistrements produits par la
> simulation sont rejouables. La console écrit un enregistrement au même
> format depuis ANH-128, mais il ne contient pas encore ce qu'un rejeu doit
> redonner au runtime : le rejeu le refuse et dit ce qui manque. Et les
> échanges Modbus du pilote réel ne sont pas rejoués (section 16.8).

### 16.2 Commande

```sh
$PY -m simulation.run auto_jog_150_dsp                 # écrit simulation/out/<dossier>
$PY -m simulation.run --replay simulation/out/<dossier>
$PY -m simulation.run --replay simulation/scenarios/real/fault_bitalino_disconnect_dsp.tar.gz
$PY -m simulation.run --replay <dossier> --json        # le même rapport, une ligne de JSON
```

Sortie réelle de la troisième commande (environ 17 s) :

```text
replay of record b67725bd54a16d6a0349d3600bf5793b: MATCH
  ticks: 1348 recorded, 1348 replayed
  tolerances: setpoint +/-1 motor rpm, verdict instant +/-1 tick, phase and rule exact
  tolerated: 0 setpoints one rpm apart, 0 verdicts one tick apart
```

| Code de sortie | Sens |
|---|---|
| 0 | `MATCH` : chaque décision rejouée est celle qui a été enregistrée |
| 1 | `DIFFERENCE` ou `DIVERGENCE` : le runtime ne décide plus la même chose. C'est un constat sur le runtime, pas un échec de l'outil |
| 2 | rejeu impossible : l'enregistrement ne peut pas être rejoué du tout. La raison est écrite sur la sortie d'erreur |

Le rapport ne recopie aucun texte libre de l'enregistrement : des nombres, des
valeurs d'énumération du code, et les noms de règles quand ce sont de simples
identifiants. Il peut donc être publié dans un journal de CI.

### 16.3 Ce qui est comparé, et les tolérances

| Quantité | Règle |
|---|---|
| consigne (`setpoint_motor_rpm`) | égale à ± 1 tr/min moteur |
| action de sécurité et règle (`safety_action`, `safety_rule`) | les transitions sont appariées une à une, dans l'ordre ; chacune peut tomber un tic plus tôt ou plus tard ; aucune ne peut manquer, être ajoutée ou changer de nature |
| phase | exacte, tic pour tic |
| appels au variateur | même appel, dans le même ordre ; mot de commande exact ; vitesse écrite à ± 1 tr/min ; demandé à moins d'un tic (0,2 s) de son instant enregistré |

Les deux tolérances chiffrées sont `SETPOINT_TOLERANCE_RPM` et
`VERDICT_TOLERANCE_TICKS` dans `simulation/replay_report.py`. Elles existent
pour les séances réelles, dont les instants sont connus à la milliseconde. Un
enregistrement de la simulation se rejoue exactement : la ligne `tolerated`
du rapport reste à zéro.

### 16.4 Les trois issues

* **`MATCH`.** Rien à faire.
* **`DIFFERENCE`.** Le runtime a posé au variateur les questions enregistrées,
  et a décidé autre chose. Le rapport donne le premier écart : sa nature
  (`setpoint`, `phase`, `transition_timing`, `transition_state`,
  `transition_count`, `safety_state`), son instant, et les deux décisions. Il
  compte les autres par nature, puis dit combien de tics ne sont pas rejoués
  à l'identique et donne leur empreinte (section 16.7).
* **`DIVERGENCE`.** Le runtime a demandé au variateur une trame que
  l'enregistrement ne contient pas à cet endroit. Les réponses enregistrées ne
  correspondent plus aux questions posées : le rejeu **s'arrête**, et dit
  l'instant, ce qui a été demandé et ce que l'enregistrement contient. Les tics
  d'avant restent comparés ; ceux d'après ne sont pas rejoués, et comptent
  parmi les tics « non atteints » de l'empreinte. Un code rejoué qui lève une
  exception sur des entrées enregistrées est rapporté de la même façon.

Sorties réelles, sur une séance manuelle de 9 s dont on a modifié à la main,
à `t = 3 s`, la consigne d'un tic (+2 tr/min), puis la vitesse d'une trame
(+5 tr/min). La dernière ligne signale que le fichier ne correspond plus à sa
somme de contrôle :

```text
replay of record 148716e4b06f4998bab0370bbcbac63a: DIFFERENCE
  ticks: 95 recorded, 95 replayed
  tolerances: setpoint +/-1 motor rpm, verdict instant +/-1 tick, phase and rule exact
  first difference: setpoint at t=3.000 s
    tick index 64
    recorded: setpoint 81 motor rpm, phase hold, safety NONE
    replayed: setpoint 79 motor rpm, phase hold, safety NONE
  failed checks by kind: setpoint 1
  tolerated: 0 setpoints one rpm apart, 0 verdicts one tick apart
  ticks that differ or were not reached: 1 (fingerprint 47fc37e33d0fd417)
  integrity warning: ticks.csv: checksum_mismatch
```

```text
replay of record 148716e4b06f4998bab0370bbcbac63a: DIVERGENCE
  ticks: 95 recorded, 64 replayed
  tolerances: setpoint +/-1 motor rpm, verdict instant +/-1 tick, phase and rule exact
  tolerated: 0 setpoints one rpm apart, 0 verdicts one tick apart
  ticks that differ or were not reached: 31 (fingerprint 022d07a8743235fc)
  divergence at t=3.000 s, after 64 ticks: the runtime requested speed 77 motor rpm; the record holds speed 82 motor rpm at t=3.000 s
    the replay stopped there: later recorded answers fit no question
  integrity warning: drive_frames.jsonl: checksum_mismatch
```

« Rejeu impossible » (code 2) n'est pas une issue du rejeu : l'outil n'a rien
à juger. Les causes sont listées dans
[enregistrement.md](enregistrement.md#ce-quun-enregistrement-doit-contenir-pour-être-rejoué) :
pas de tics d'avant le départ, événement d'entrée qui n'est pas une commande,
aucun échange du variateur, manifeste sans géométrie, fréquence cardiaque sans
ECG brut, échanges Modbus natifs, phase ou action inconnue de cette version.
Quand il en manque plusieurs, le message les donne ensemble, séparées par
« ; » : c'est le cas d'un enregistrement écrit par la console (section 16.8).

### 16.5 La bibliothèque `simulation/scenarios/real/`

Un fichier par séance : `<nom>.tar.gz`, l'archive d'un dossier
d'enregistrement fermé. Le dossier lui-même n'est pas versionné : une séance
de treize minutes contient environ 4000 blocs d'ECG.

`simulation/tests/test_real_records.py` juge **chaque archive présente dans
le dossier** (`judge`, dans `simulation/real_records.py`). Une archive ajoutée
est donc jugée par la gate suivante, et ne peut pas être fusionnée si elle ne
passe pas. Pour chacune, la gate exige, dans cet ordre, et dit laquelle de ces
exigences n'est pas tenue :

1. un enregistrement fermé et intact : le reader ne signale aucun
   avertissement (tous les fichiers sont dans `checksums.sha256` et leur
   correspondent), et le manifeste porte une fin ;
2. un enregistrement anonyme, sous l'une de deux formes fermées :

   | Champ | Enregistrement de la simulation | Séance réelle |
   |---|---|---|
   | `subject_id`, `session_id` | `null` | `null` |
   | `machine_id` | `simulation` | `anonymized` |
   | `organization_id` | `synthetic` | `anonymized` |
   | `operator` | `sim-operator` | `anonymized` |
   | `actor` de chaque événement | `system`, `remote` ou `sim-operator` | `system`, `remote` ou `anonymized` |
   | `record_id`, `local_ref` | ceux de la bibliothèque, dérivés du nom de l'archive (`library_identity`) | les mêmes : les identifiants d'origine ramèneraient à la séance |
   | nom du dossier dans l'archive | se termine par `_<local_ref>` | se termine par `_<local_ref>` |

3. un rejeu conforme à ce que la bibliothèque déclare : `MATCH`, ou l'écart
   accepté décrit en 16.7.

Les identifiants de la bibliothèque sont exigés sous les deux formes : un
enregistrement de la bibliothèque ne porte jamais ceux sous lesquels sa
source le connaît, quelle que soit la forme qu'il déclare.

La date de la séance reste dans le manifeste et dans le nom du dossier : pour
une séance réelle, c'est à l'outil d'anonymisation de la traiter (il n'existe
pas encore, section 16.6).

Une archive est le fichier de quelqu'un d'autre. Elle est extraite membre par
membre (`unpack`), et seuls des fichiers ordinaires et des dossiers sont
écrits, uniquement sous le dossier d'extraction. Un membre au nom absolu ou
qui remonte d'un dossier, un lien (symbolique ou physique), un périphérique,
ou un membre qu'un lien déjà présent ferait écrire ailleurs, arrête
l'extraction : la gate échoue sur cette archive et dit pourquoi.

Contenu au 7 octobre 2026, trois scénarios de la batterie exportés par
`--export-real`, comme preuve de fonctionnement avant toute séance réelle :

| Archive | Ce qu'elle couvre | Tics | Taille | Rejeu (Mac, puis CI) |
|---|---|---|---|---|
| `auto_jog_150_dsp.tar.gz` | un programme complet, la fréquence cardiaque pilote la vitesse, plusieurs `hr_stale` | 4000 | 1,9 Mo | ≈ 75 s, 224 s |
| `fault_bitalino_disconnect_dsp.tar.gz` | le BITalino se déconnecte en WARMUP, fin sur verdict | 1348 | 0,3 Mo | ≈ 17 s, 23 s |
| `fault_ecg_electrode_off_dsp.tar.gz` | une électrode décollée 30 s, `hr_stale` puis reprise | 4000 | 1,8 Mo | ≈ 75 s, 155 s |

Les durées en CI sont celles du run 37548774542 (7 octobre 2026, runner à
4 CPU, quatre processus pytest en même temps). Les archives ont été produites
sur un Mac et se rejouent sans écart sur le runner Linux.

Ce sont les scénarios en `ecg.mode: dsp` : le mode `direct` injecte des bpm
sans acquisition, et ne laisse pas d'ECG brut à rejouer.

Pour regarder une archive dans le visualiseur, l'extraire sous `simulation/out/`
puis ouvrir `?trace=out/<dossier>` (section 7).

### 16.6 Ajouter un enregistrement

**Un scénario de la simulation.**

```sh
$PY -m simulation.run --export-real <nom_du_scénario>
```

La commande lance le scénario, retire `subject_id`, rejoue l'enregistrement
produit et n'écrit l'archive que si ce rejeu est `MATCH`. Le nom est celui
d'un scénario de la batterie (lettres, chiffres, `_`) : un chemin est refusé.

L'export est reproductible : `record_id` et `local_ref` sont dérivés du nom du
scénario, les dates viennent de l'horloge du harness, et `software_version`
vaut toujours `unversioned`, quelle que soit la variable d'environnement.
Exporter deux fois le même scénario donne la même archive, octet pour octet.
Et une archive qui contient déjà exactement cet enregistrement n'est pas
réécrite : la commande affiche `unchanged`, et git n'a rien à ajouter. La
comparaison porte sur le contenu lu, pas sur les octets, parce que deux
versions de zlib ne compriment pas pareil.

**Une séance réelle.** Pas encore possible de bout en bout :
l'enregistrement que la console écrit n'est pas encore rejouable, et les
échanges Modbus natifs ne sont pas rejoués (section 16.8). La marche à suivre,
le jour où c'est possible :

1. récupérer l'archive `.tar.gz` de la séance ;
2. l'anonymiser sous la forme « séance réelle » du tableau de 16.5 (manifeste,
   acteurs des événements, identifiants de la bibliothèque, nom du dossier),
   puis recalculer `checksums.sha256` ;
3. vérifier `python -m simulation.run --replay <archive>` ;
4. déposer l'archive dans `simulation/scenarios/real/` sous un nom qui ne
   désigne ni une personne ni un lieu, et ouvrir la PR. La gate fait le reste.

### 16.7 Un écart de rejeu : la règle

> **Un écart de rejeu non expliqué ouvre un ticket.** On ne régénère pas un
> enregistrement, on n'accepte pas un écart et on n'élargit pas une tolérance
> pour faire passer la gate tant que la cause n'est pas connue.

Quand `test_real_records.py` échoue, ou quand `--replay` sort en 1 :

1. **Lire le rapport** : l'instant, la nature de l'écart, les deux décisions.
2. **Chercher la cause** : quel changement du runtime, du DSP ou de la
   configuration livrée (`raspberry-pi/config/`) explique qu'à cet instant la
   décision ne soit plus la même.
3. **Conclure**, d'une des quatre façons :

| Conclusion | Ce qu'on fait |
|---|---|
| c'est une régression | on corrige le code. L'enregistrement ne change pas |
| c'est un changement voulu, et l'enregistrement vient de la simulation | on le **régénère** : `$PY -m simulation.run --export-real` (sans nom : tous les enregistrements simulés ; seules les archives dont le contenu change sont réécrites). La PR qui change le comportement contient les archives régénérées et dit, dans sa description, quel écart elle a constaté (archive, instant, nature) et quel ticket le justifie. Un fichier `.accepted.json` posé à côté d'un enregistrement de la simulation est refusé par la gate |
| c'est un changement voulu, et l'enregistrement est une séance réelle | une séance réelle ne se refait pas. On **documente l'écart accepté** : un fichier `<nom>.accepted.json` à côté de l'archive (ci-dessous) |
| la cause n'est pas trouvée | on ouvre un ticket avec le rapport JSON. Rien n'est régénéré ni accepté |

Un écart accepté est un fichier à côté de l'archive, qui contient le ticket,
la raison, et **le rapport JSON du rejeu tout entier**, tel que
`--replay <archive> --json` l'affiche :

```json
{
 "ticket": "ANH-000",
 "reason": "une phrase : ce qui a changé et pourquoi c'est voulu",
 "report": {
  "comparison": {
   "actual_ticks": 64, "counts": {}, "deviating_ticks": 31, "expected_ticks": 64,
   "fingerprint": "022d07a8743235fc361e9dad25c54d8122e9295a05d56e90fadac1ce72b5e64a",
   "first": null, "matches": true, "shifted_transitions": [0, 0], "tolerated_rpm": [0, 0]
  },
  "divergence": {
   "recorded": "speed 82 motor rpm at t=3.000 s",
   "requested": "speed 77 motor rpm", "t": 3.0, "tick": 64
  },
  "integrity": [], "outcome": "divergence",
  "record": "148716e4b06f4998bab0370bbcbac63a", "recorded_ticks": 95,
  "tolerances": {"setpoint_motor_rpm": 1, "verdict_ticks": 1}
 }
}
```

La gate exige alors **ce rapport, et aucun autre** : le même premier écart
(nature, tic, les deux décisions), le même décompte de chaque nature d'écart,
les mêmes tolérances consommées, la même divergence (ce qui est demandé, ce
que l'enregistrement contient, l'instant, le nombre de tics rejoués avant),
et la même **empreinte** (`fingerprint`).

L'empreinte est ce qui empêche un écart d'en cacher un autre. Le rapport ne
détaille que le premier écart et compte les suivants par nature : sans elle,
deux rejeux différents pourraient donner le même rapport. C'est un SHA-256
calculé sur :

* chaque tic dont la décision rejouée n'est pas **identique** à la décision
  enregistrée, qu'il soit compté comme un écart ou absorbé par une tolérance :
  son rang, puis l'instant, la consigne, la phase, l'action et la règle de
  chacune des deux décisions ;
* chaque tic enregistré que le rejeu n'a pas atteint, après une divergence ;
* le nombre de tics de chaque côté.

`deviating_ticks` est le nombre de ces tics. Le rapport texte montre les 16
premiers chiffres de l'empreinte, le JSON les 64. Les noms de règles entrent
dans le calcul et n'en sortent pas : l'empreinte ne recopie rien.

Deux rapports égaux disent donc que les deux rejeux s'écartent de leur
enregistrement aux mêmes tics, de la même décision enregistrée vers la même
décision rejouée, et laissent les mêmes tics non rejoués. Un écart de plus ou
de moins, un écart remplacé par un autre de même nature, déplacé d'un tic ou
d'une autre ampleur, une règle renommée autrement, un tr/min toléré qui change
de tic, un enregistrement différent après la divergence : la gate échoue, et
dit quelle partie du rapport diffère (`comparison`, `divergence`,
`outcome`...). Un écart accepté ne peut donc pas en couvrir un autre, ni avant
ni après lui. L'empreinte ne dit rien des tics rejoués à l'identique : ceux-là
sont ce que l'enregistrement contient.

Le jour où le rejeu redevient `MATCH`, la gate échoue aussi, jusqu'à ce que le
fichier soit retiré. Un rapport `match` n'est pas un écart : le fichier est
refusé.

Un écart accepté affaiblit l'enregistrement : après une divergence, la suite
de la séance n'est plus rejouée. Si elle arrive tôt, l'enregistrement ne
protège presque plus rien, et il vaut mieux le retirer de la bibliothèque, par
un ticket qui le dit.

On ne modifie jamais un enregistrement à la main. La gate le verrait
(`checksum_mismatch`), et c'est voulu.

### 16.8 Limites connues

* **Séances réelles.** La console écrit un enregistrement au format commun
  (ANH-128), que le rejeu refuse aujourd'hui (code 2) en donnant ses trois
  raisons : ses événements d'entrée sont du texte (`manual session started`,
  `end_requested: done`) et non les commandes du vocabulaire ; son
  `drive_frames.jsonl` est vide ; son manifeste n'a pas de géométrie. Il lui
  manque aussi les tics du repos et `t_received` sur les blocs d'ECG.
  `test_record_console_parity.py` construit cet enregistrement avec la vraie
  console et vérifie ce refus : le jour où la console écrit ce qu'il faut, ce
  test échoue, et se remplace par un rejeu.
* **Échanges natifs.** Le magnétophone rejoue les observations d'appel du
  variateur (`open`, `speed`, `read_status`...), celles qu'écrit la simulation.
  Les échanges Modbus du pilote réel ne sont pas rejoués : un enregistrement
  qui en contient est refusé (code 2).
* **Mode `direct`.** Les scénarios qui injectent des bpm ne sont pas
  rejouables : seuls les quatre scénarios `*_dsp` de la batterie le sont.
* **Exactitude.** Un enregistrement de la simulation est rejoué sur les mêmes
  nombres flottants que l'original, donc exactement. Une séance réelle est
  connue à la milliseconde : une décision prise à moins d'une milliseconde
  d'un seuil peut tomber un tic plus tôt ou plus tard, ce que les tolérances
  absorbent pour un verdict, pas pour une phase.
* **Ce que l'enregistrement ne porte pas** : la nature d'un échange en échec,
  le courant à mieux que 0,1 A, une exception levée dans le tic d'origine, un
  saut de l'horloge murale pendant l'acquisition. Détail dans
  [enregistrement.md](enregistrement.md#ce-quun-enregistrement-doit-contenir-pour-être-rejoué).
* **Coût.** Rejouer la bibliothèque prend environ trois minutes sur un Mac et
  près de sept minutes de calcul en CI, réparties sur trois parts de la
  batterie (les deux longs rejeux sont dans `SLOW_SECONDS`). Ses trois
  archives pèsent 4 Mo dans le dépôt. Une régénération n'ajoute à
  l'historique que les archives dont le contenu a changé (section 16.6).
* **Dépendances numériques.** Le rejeu repasse l'ECG dans le DSP : une
  nouvelle version de numpy, scipy ou BioSPPy qui changerait une fréquence
  cardiaque calculée se verrait ici comme un écart. Ces dépendances ne sont
  pas épinglées (`>=` dans `raspberry-pi/requirements-base.txt`).

### 16.9 Les tests du rejeu

| Fichier | Ce qu'il vérifie |
|---|---|
| `test_replay.py` | de bout en bout sur de courtes séances réellement enregistrées : rejeu sans écart (y compris boucle bloquée, liaison perdue, défaut variateur, ECG lacunaire ou perdu), écart détecté à son instant quand on altère un tic ou une trame, divergence, déterminisme, refus motivés (toutes les raisons à la fois), événements qui ne sont pas des entrées jamais lus comme des commandes, ligne de commande |
| `test_replay_tape.py` | le magnétophone : lecture des trames, appel conforme ou non, horloge, blocs ECG |
| `raspberry-pi/tests/test_record_commands.py` | le vocabulaire des commandes (`src/record/commands.py`), écrit et relu à l'identique ; il est dans la gate du Pi |
| `test_replay_compare.py` | la comparaison pure : tolérances aux bornes, transitions décalées, perdues ou ajoutées ; l'empreinte, qui distingue deux rejeux que les décomptes confondent |
| `test_real_records.py` | la gate de la bibliothèque et chacune de ses exigences ; les archives et ce que l'extraction refuse (nom absolu, remontée de dossier, lien, périphérique, écriture hors du dossier) ; les deux formes d'anonymat et les identifiants de la bibliothèque ; l'écart accepté (sont refusés : un second écart derrière lui, un écart remplacé par un autre de même nature, un tic modifié après une divergence acceptée, un fichier accepté à côté d'un enregistrement de la simulation) ; l'export reproductible, et son refus d'un nom qui est un chemin |
| `test_record_console_parity.py` | écrit pour ANH-128 (même structure sur la console et en simulation) ; il vérifie aussi que le rejeu refuse l'enregistrement de la console en donnant ses raisons, et que `t_received` est la seule clé d'en-tête de bloc que la simulation écrit en plus |

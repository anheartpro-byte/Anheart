# Démarrage rapide

Ce guide couvre l'installation et le lancement de chaque partie **sans aucun
matériel** : la console locale du Pi en simulation complète, le site, la
simulation 2D et le mode instantané. Les commandes utilisent un shell Unix
(macOS ou Linux). La console simulée et le visualiseur fonctionnent hors ligne ;
le site nécessite des services de développement déjà configurés.

Commencer chaque section depuis la **racine du dépôt** (celle qui contient
`package.json`, `raspberry-pi/` et `simulation/`). Les parenthèses des commandes
Python isolent le changement de dossier : à leur sortie, le terminal reste à
la racine. Dans un nouveau terminal, revenir à cette racine avant de continuer.

[Retour au sommaire](README.md) · termes : [glossaire](glossaire.md)

---

## 1. Prérequis

| Outil | Version de référence | Pour quoi |
|---|---|---|
| Python | 3.12 (le code vise 3.12 : `pythonVersion = "3.12"` dans `raspberry-pi/pyproject.toml`) | le Pi et la simulation |
| Node.js | 24 (comme la CI dans `.github/workflows/ci.yml`) | le site Next.js et la CLI Convex |
| npm ou bun | npm 10+ / bun 1.x (les deux fichiers de verrou existent : `package-lock.json` et `bun.lock`) | installer le site |
| libusb | `brew install libusb` | seulement pour le vrai câble RS485 Schneider sur Mac |
| Configuration Convex et Clerk de développement | fournie par l'équipe | seulement pour faire tourner le site ; voir section 4 |

Il n'y a **qu'un seul environnement Python**, `raspberry-pi/.venv`. La simulation
l'utilise aussi.

## 2. Installer l'environnement Python

Depuis la racine du dépôt (une seule installation) :

```sh
(
cd raspberry-pi
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements-dev.txt
# le module bitalino n'est pas dans requirements-dev.txt (voir la note du fichier) :
.venv/bin/pip install pyserial
.venv/bin/pip install --no-deps bitalino==1.2.6
)
```

`requirements-dev.txt` inclut `requirements-base.txt` et ajoute pytest,
hypothesis, basedpyright, mypy et ruff. `requirements-base.txt` fixe chaque
paquet à une version exacte : ce sont celles de la CI et de l'image du Pi
([pi-image.md](pi-image.md#2-versions-figées)). Après un changement de ce
fichier, relancer la commande `pip install -r requirements-dev.txt`. C'est aussi ce que demande la
[gate](glossaire.md#gate-porte) (`scripts/check.sh`) si le venv manque.

## 3. Lancer la console locale en simulation complète

Depuis la racine du dépôt, entrer dans `raspberry-pi/` pour lancer le module :

```sh
(
cd raspberry-pi
MOTOR_BACKEND=sim ECG_SOURCE=sim ARM_RADIUS_M=1.5 UI_PORT=8090 MACHINE_API_KEY= \
  .venv/bin/python -m src.local_panel
)
```

Puis ouvrir <http://127.0.0.1:8090/>. Ctrl-C arrête proprement.

Ce que fait chaque variable :

| Variable | Valeur | Pourquoi |
|---|---|---|
| `MOTOR_BACKEND` | `sim` | variateur simulé (`SimulatedDrive`). **Obligatoire.** `serial` = le vrai ATV320 |
| `ECG_SOURCE` | `sim` | BITalino simulé. **Obligatoire.** `serial` ou `rfcomm` = le vrai boîtier |
| `ARM_RADIUS_M` | `1.5` | rayon de référence, en mètres. **Obligatoire, sans défaut** : tous les g en dépendent |
| `UI_PORT` | `8090` | port choisi pour cet exemple. Le défaut du code et `.env.example` sont **8080**. 8123 est refusé |
| `MACHINE_API_KEY` | vide | aucune liaison au tableau de bord Convex. **Forcez-la vide** si un `.env` contient une vraie clé |

Options utiles pour voir plus de choses :

```sh
(
cd raspberry-pi
MOTOR_BACKEND=sim ECG_SOURCE=sim ARM_RADIUS_M=1.5 UI_PORT=8090 MACHINE_API_KEY= \
  SENSORS=ECG,EDA,SpO2,RESP,EMG,LUX PRESENCE_SOURCE=sim_empty \
  .venv/bin/python -m src.local_panel
)
```

`SENSORS` affiche les six voies du BITalino (seul l'ECG pilote quoi que ce soit) ;
`PRESENCE_SOURCE=sim_empty` ajoute une caméra **simulée** qui voit une capsule
vide. Toutes les clés sont décrites dans
[raspberry-pi.md](raspberry-pi.md#12-la-configuration-env).

Au démarrage, le terminal affiche une ligne de ce genre (constatée) :

```text
Console du banc sur http://127.0.0.1:8090/ - MANUEL BANC (plafond 300 tr/min moteur; paliers 148/158 bpm) - variateur: simulateur - ECG (sim): simulateur - rayon 1.5 m, i = 49.79 - tableau de bord: aucun
```

S'il manque une clé obligatoire, la console refuse de démarrer, liste **tous**
les problèmes d'un coup et sort avec le code 2.

Pour un premier essai dans la page : attester le câblage E-STOP (deux cases),
démarrer une séance **manuelle banc**, augmenter la cible avec « +1 tr/min »,
cliquer « Appliquer », puis STOP. Le premier incrément rejoint le minimum de
rotation affiché. Chaque écran
est décrit dans [console-locale.md](console-locale.md).

> Avec le plafond par défaut `MOTOR_MAX_RPM=300` (≈ 6 tr/min au bras), une cible de
> 27 tr/min est refusée : `consigne refusee : 27.00 tr/min de sortie hors de 0 ou
> [55, 300] tr/min moteur`. C'est voulu.

## 4. Lancer le site (tableau de bord distant)

### Frontend local, services de développement déjà configurés

Demander à l'équipe une configuration **de développement** compatible avec la
branche, puis renseigner les trois variables du site ci-dessous dans
`.env.local`, à la racine. Ce fichier local n'est pas fourni par un clone et ne
doit pas être commité. Vérifier l'environnement visé avec l'équipe : les essais
des 1er et 2 octobre 2026 consignés dans [deploiement.md](deploiement.md) ne
garantissent ni la configuration de votre clone ni l'état du backend actuel.

Depuis la racine du dépôt :

```sh
npm install              # ou : bun install
npm run dev:frontend     # Next.js uniquement ; aucun déploiement Convex
```

Ouvrir <http://localhost:3000/> (ou le port annoncé par Next.js si 3000 est
occupé). La page d'accueil doit s'afficher ; les pages protégées demandent une
connexion Clerk. Si une variable manque, demander sa valeur de développement
à l'équipe avant de poursuivre. Aucun compte ni aucune machine ne doit être
créé pour simplement vérifier la page d'accueil.

Les scripts de `package.json` :

| Script | Commande |
|---|---|
| `predev` | `convex dev --until-success && convex dashboard` (lancé automatiquement avant `npm run dev`) |
| `dev` | `npm-run-all --parallel dev:frontend dev:backend` |
| `dev:frontend` | `next dev` |
| `dev:backend` | `convex dev` |
| `build` / `start` | `next build` / `next start` |
| `lint` | `eslint .` |

### Configuration ou mise à jour du backend : opération distincte

**Ne pas utiliser `npm run dev` comme simple lancement local** : son `predev`
déploie les fonctions Convex, puis `dev:backend` surveille et déploie les
changements. `npx convex dev --once` déploie aussi. Ces commandes nécessitent
un environnement cible vérifié et l'accord de l'équipe pour le modifier ;
suivre alors [deploiement.md](deploiement.md). Elles ne sont pas nécessaires
pour ouvrir le frontend avec des services déjà configurés.

Variables nécessaires :

| Où | Variable | Rôle |
|---|---|---|
| `.env.local` (site) | `NEXT_PUBLIC_CONVEX_URL` | URL `.convex.cloud` du déploiement de développement existant, lue par `components/ConvexClientProvider.tsx` |
| `.env.local` (site) | `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY` | clés standard de `@clerk/nextjs` |
| variables du déploiement Convex | `CLERK_JWT_ISSUER_DOMAIN` | domaine émetteur des jetons Clerk (`convex/auth.config.ts`, `applicationID: "convex"`) ; il faut un modèle JWT Clerk nommé `convex` |
| `raspberry-pi/.env` (poste de développement) ou `/etc/anheart/anheart.env` (Pi installé) | `CONVEX_URL`, `MACHINE_API_KEY` | pour relier une console : l'hôte **`.convex.site`** (pas `.convex.cloud`) et la clé de 64 caractères affichée une seule fois à la création de la machine |

**Le premier admin** : un nouveau compte reçoit le rôle `user`. Rien dans le code
ne crée un admin ; il faut changer le rôle à la main dans le tableau de bord
Convex (table `users`). Voir [convex.md](convex.md).

## 5. La simulation

Tout se lance **depuis la racine du dépôt**, avec deux dossiers importables :

```sh
export PYTHONPATH=.:raspberry-pi
PY=raspberry-pi/.venv/bin/python
```

### Mode instantané (verdicts en quelques secondes)

```sh
$PY -m simulation.quick manual_27_rpm        # un scénario, par son nom
$PY -m simulation.quick S07                  # une personne de la cohorte, ses trois séances
$PY -m simulation.quick --cohort             # 30 personnes x 3 séances
$PY -m simulation.quick --failures           # la matrice de pannes (sans les cas DSP)
$PY -m simulation.quick --all --dsp          # tout
```

Sortie constatée pour `manual_27_rpm` (1,45 s) :

```text
result  run           end                      rpm g leg  zone viol     s
PASS    manual_27_rpm operator_stop           27.0  1.98     -    0  1.44
1 runs: 1 PASS - 1.45 s wall (8 workers)
report: .../simulation/out/report.html  data: .../simulation/out/report.json
```

Ouvrir `simulation/out/report.html` dans un navigateur pour les courbes.

### Un scénario, trace complète

```sh
$PY -m simulation.run --list                 # la liste des scénarios
$PY -m simulation.run manual_27_rpm --csv    # -> dossier schema 2 + CSV dans simulation/out/
```

### Le visualiseur 2D

```sh
$PY -m simulation.live                       # http://127.0.0.1:8765/  (--port pour changer)
```

Puis ouvrir :

- en direct : <http://127.0.0.1:8765/?live=manual_27_rpm&speed=20>
- en relecture : `http://127.0.0.1:8765/?trace=out/<UTC>_<local_ref>` (remplacer
  le nom du dossier par celui créé dans `simulation/out/`).

Le dossier contient notamment `manifest.json`, `events.jsonl` et `ticks.csv`.
`--csv` ajoute un fichier `<local_ref>.csv`. Les anciennes traces schema 1
restent lisibles avec `?trace=out/<ancien-fichier>.jsonl` ou le sélecteur de
fichier du visualiseur. Voir [le format d'enregistrement](enregistrement.md).

Tous les paramètres d'URL et les options sont dans
[framework-de-test.md](framework-de-test.md).

## 6. Vérifier que tout passe (les gates)

```sh
bash raspberry-pi/scripts/check.sh          # depuis la racine : gate du Pi
bash simulation/scripts/check.sh            # depuis la racine : gate de la simulation
```

Ces scripts se placent eux-mêmes dans leur dossier ; le terminal reste à la
racine. Ils exécutent ruff, basedpyright, mypy et les tests avec couverture.

`raspberry-pi/scripts/check.sh` est suivi avec le mode exécutable `100755` ;
`./scripts/check.sh` et `bash scripts/check.sh` lancent la même gate.
Durées, nombres de tests et lancement d'un seul test :
[framework-de-test.md](framework-de-test.md).

## 7. Pièges connus

| Piège | Symptôme | Solution |
|---|---|---|
| `PYTHONPATH` absent pour la simulation | `ModuleNotFoundError: No module named 'simulation'` ou `'src'` | `export PYTHONPATH=.:raspberry-pi` et lancer depuis la racine |
| Console lancée depuis la racine | `No module named 'src'` | lancer depuis `raspberry-pi/` ; la console n'a pas besoin de `PYTHONPATH` |
| Port 8123, ou 8080 au lieu de 8090 | conflit avec l'outil de banc, ou page introuvable sur 8090 | 8123 est pris par `scripts/bench_console.py` et refusé. Sans `UI_PORT`, la console écoute sur 8080 : passez `UI_PORT=8090` |
| Console et `bench_console.py` en même temps sur le même câble | deux programmes parlent au variateur | n'en lancer qu'un seul |
| Un `raspberry-pi/.env` avec une vraie clé ou `MOTOR_BACKEND=serial` | la console tente le matériel ou Convex en ligne | les variables passées sur la ligne de commande priment sur `.env` : forcez `MOTOR_BACKEND=sim ECG_SOURCE=sim MACHINE_API_KEY=` |
| `ARM_RADIUS_M` oublié | sortie immédiate, code 2 | c'est voulu : il n'y a pas de rayon par défaut |
| `UI_HOST` autre que `127.0.0.1` sans jeton | sortie, code 2 | fournir `UI_TOKEN` d'au moins 16 caractères |
| `CONVEX_URL` en `.convex.cloud` côté Pi | chaque requête du Pi répond 404 | utiliser l'hôte `.convex.site` |
| Docker sur Mac | ne voit ni le câble FTDI ni le Bluetooth | lancer la console en natif |
| Tester l'API à la main dans zsh avec un en-tête `-H` stocké dans une variable | réponses 422 | écrire l'en-tête `Content-Type: application/json` directement dans la commande `curl` |
| Environnements Python dans le dépôt | à exclure de l'analyse JavaScript | `eslint.config.mjs` exclut déjà `**/.venv/**`, `**/.venv-*/**` et `**/venv/**` |

## 8. Installer la console sur un Raspberry Pi

Ce guide ne demande aucun matériel. Pour une vraie machine, un seul chemin :
flasher l'image Raspberry Pi OS figée, copier le dossier `raspberry-pi/`, puis
lancer le script d'installation **sur le Pi**.

```sh
# depuis le poste de développement, dans raspberry-pi/
bash scripts/pi/deploy.sh <utilisateur>@<adresse du Pi>

# sur le Pi
cd ~/anheart/raspberry-pi
sudo bash scripts/install.sh --simulation    # sans matériel : variateur et ECG simulés
systemctl status anheart                     # en marche, avec la version
curl -fsS http://127.0.0.1:8090/healthz
```

Le script installe Docker, construit l'image de la console et installe le
service systemd `anheart`, qui la lance à chaque allumage. Sans `--simulation`,
la configuration créée vise le vrai variateur et le vrai BITalino. Versions
figées, détail du script, passage au matériel réel et état de ce qui a été
vérifié : [pi-image.md](pi-image.md). Cette installation n'a **pas** encore été
faite sur un vrai Pi ; la CI l'exécute dans une machine de remplacement.

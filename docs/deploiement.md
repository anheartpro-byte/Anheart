# Déploiement : Convex, site, simulation hébergée, Raspberry Pi

Ce document dit **où tourne quoi**, **comment on le déploie**, et **ce qui a été
réellement vérifié**. Il complète [convex.md](convex.md),
[tableau-de-bord.md](tableau-de-bord.md), [raspberry-pi.md](raspberry-pi.md) et
[framework-de-test.md](framework-de-test.md).

[Retour au sommaire](README.md) · termes : [glossaire](glossaire.md)

> **État réel au 2 octobre 2026.**
>
> - **Convex, développement** : le code de la branche `feat/pi-training-session`
>   est déployé sur le déploiement de développement et a été testé de bout en
>   bout avec une console de Pi en simulation (section 4).
> - **Convex, production** : **inchangé**. La production sert toujours la
>   version précédente. Le nouveau code n'y est pas.
> - **Site** : la production (branche `main`) est inchangée. Les nouvelles pages
>   ont été ouvertes dans un navigateur, en local, contre le Convex de
>   développement, le 2 octobre 2026 (section 5.4).
> - **Simulation hébergée** : en ligne sur Vercel (section 6).
> - **Raspberry Pi** : l'image Docker lance maintenant la console. Elle a été
>   construite et essayée **en simulation** dans un conteneur ARM64. Elle n'a
>   **jamais** tourné sur un vrai Pi, ni avec le vrai variateur, ni avec le vrai
>   BITalino (section 7).

## Sommaire

1. [Les environnements](#1-les-environnements)
2. [Les clés et où elles sont rangées](#2-les-clés-et-où-elles-sont-rangées)
3. [Déployer Convex](#3-déployer-convex)
4. [Essai de bout en bout du 1er octobre 2026](#4-essai-de-bout-en-bout-du-1er-octobre-2026)
5. [Le site sur Vercel et en local](#5-le-site-sur-vercel-et-en-local)
6. [Le moteur de simulation hébergé](#6-le-moteur-de-simulation-hébergé)
7. [Le Raspberry Pi](#7-le-raspberry-pi)
8. [Reste à faire](#8-reste-à-faire)

---

## 1. Les environnements

| Brique | Développement | Production |
|---|---|---|
| Convex (équipe `anheartpro`, projet `anheart`) | `standing-jay-887` | `clean-giraffe-153` |
| URL des fonctions (site) | `https://standing-jay-887.convex.cloud` | `https://clean-giraffe-153.convex.cloud` |
| URL des routes machine (Pi) | `https://standing-jay-887.convex.site` | `https://clean-giraffe-153.convex.site` |
| Clerk (émetteur des jetons) | `https://major-macaw-92.clerk.accounts.dev` | `https://clerk.gauratechnologies.com` |
| Site (Vercel, projet `anheart`) | en local : `npm run dev` | `www.gauratechnologies.com`, `www.anheart.ai`, `anheart-wine.vercel.app` |
| Simulation hébergée (Vercel, projet `anheart-simulation`) | - | `https://anheart-simulation.vercel.app` |

Le compte Vercel est celui du client (`anheartpro`, offre Hobby, portée
« Anheart's projects »).

**Chaque déploiement Convex fait confiance à une seule instance Clerk** (variable
`CLERK_JWT_ISSUER_DOMAIN` du déploiement). Un site branché sur le Convex de
développement doit donc utiliser les clés Clerk de développement, et
inversement. Mélanger les deux donne un site où l'on se connecte mais où toutes
les requêtes Convex échouent.

---

## 2. Les clés et où elles sont rangées

Aucune clé n'est dans le dépôt. Tous les fichiers ci-dessous sont ignorés par git.

| Fichier | Clé | Rôle |
|---|---|---|
| `.env.local` (racine) | `CONVEX_DEPLOY_KEY` | Clé du déploiement de **développement**. La CLI Convex la lit toute seule : `npx convex ...` vise donc le développement par défaut. |
| `.env.local` | `CONVEX_DEPLOY_KEY_PROD` | Clé du déploiement de **production**. La CLI **ne lit pas** ce nom : une commande ne touche la production que si on la lui passe à la main (section 3.3). |
| `.env.local` | `CONVEX_DEPLOYMENT`, `NEXT_PUBLIC_CONVEX_URL` | Écrites par `npx convex dev`. Le site lit la seconde. |
| `.env.local` | `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Clé publique de l'instance Clerk de développement. |
| `.env.local` | `CLERK_SECRET_KEY` | Clé secrète de l'instance Clerk de **développement** (`sk_test_...`). Sans elle le site ne démarre pas en local. |
| `.vercel-cli/` (racine) | connexion de la CLI Vercel | Connexion au compte du client, valable pour ce dépôt seulement : passer `--global-config .vercel-cli` à chaque commande `vercel`. |
| `raspberry-pi/.env` | `MACHINE_API_KEY`, `CONVEX_URL` | Sur le Pi : la clé de la machine et l'hôte `.convex.site`. |

Les variables du projet Vercel `anheart` (`NEXT_PUBLIC_CONVEX_URL`,
`NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY`,
`CLERK_JWT_ISSUER_DOMAIN`) ont **la même valeur** pour la production, les
préversions et le développement : ce sont les valeurs de production. Conséquence
en section 5.2.

Une clé de déploiement Convex se génère dans le tableau de bord Convex : choisir
le déploiement (Development ou Production) dans le sélecteur du haut, puis
Settings, « URL & Deploy Key ». La clé « Preview » de la page de réglages **du
projet** ne donne accès à aucun déploiement existant.

---

## 3. Déployer Convex

### 3.1 Vers le développement

Depuis la racine du dépôt :

```sh
npx convex dev --once        # pousse convex/ vers standing-jay-887, puis s'arrête
```

La commande valide le schéma contre les données déjà présentes : si un document
existant ne respecte pas le nouveau schéma, le déploiement est refusé et rien ne
change. Le 1er octobre 2026 elle a ajouté sept index (`machine_profiles`,
`machine_user_permissions`, `sessions.by_machine_and_local_ref`,
`training_telemetry`) sans rien refuser.

`npm run dev` fait la même chose en continu, à côté de `next dev`.

### 3.2 Vérifier un déploiement sans navigateur

```sh
npx convex function-spec > /tmp/spec.json   # la liste des fonctions déployées
npx convex data machines                    # lire une table
npx convex env list                         # les variables du déploiement
```

Pour appeler une fonction publique **au nom d'un compte**, la CLI accepte une
identité. `subject` est le `clerkId` du compte (colonne `clerkId` de la table
`users`) :

```sh
npx convex run training:listLaunchableMachines '{}' \
  --identity '{"subject":"user_xxx","issuer":"https://major-macaw-92.clerk.accounts.dev"}'
```

C'est ce qui a servi à l'essai de la section 4. Cela ne marche qu'avec une clé de
déploiement : ce n'est pas un contournement de l'authentification du site.

### 3.3 Vers la production

**Pas encore fait.** La production est en service : le déploiement se décide,
il ne se lance pas par habitude.

```sh
CONVEX_DEPLOY_KEY="$(grep '^CONVEX_DEPLOY_KEY_PROD=' .env.local | cut -d= -f2-)" npx convex deploy
```

Avant de le lancer :

1. Le nouveau schéma n'ajoute que des tables et des champs facultatifs, et rend
   `sessions.userId` et `machines.config` facultatifs. Dans sa version d'avant
   le retrait de l'ancien mode ECG, il a été accepté tel quel par les données
   de développement ; la version actuelle n'a été poussée sur aucun
   déploiement. Les données de production peuvent différer : `convex deploy`
   refusera le tout s'il trouve un document non conforme, sans rien modifier.
2. Le site de production (branche `main`) appelle l'ancienne API, et **elle
   n'est plus entièrement là**. Le retrait de l'ancien mode d'enregistrement
   ECG a supprimé trois mutations du module `sessions` (création, fin et
   annulation d'une séance d'enregistrement), et `machines.createMachine` /
   `updateMachine` refusent maintenant l'argument `config` que l'ancien site
   envoie. Tant que l'ancien site tourne contre le nouveau Convex, son bouton
   « Nouvelle session », la fin d'un enregistrement, et la création ou la
   modification d'une machine échouent. Les autres fonctions gardent leur nom
   et leurs arguments ; `sessions.listSessions` renvoie deux champs de plus
   (`kind`, `origin`).
3. Fusionner la branche dans `main` déploie le site de production (Vercel est
   relié au dépôt GitHub). Ordre : Convex d'abord, le site **aussitôt après**,
   dans la même fenêtre, à cause du point 2.
4. Exécuter les deux mutations de migration du retrait de l'ancien mode ECG
   (voir [convex.md](convex.md#migration-du-retrait-de-lancien-mode-ecg)).
5. Créer le premier admin et la machine (voir [convex.md](convex.md#8-déployer)).

---

## 4. Essai de bout en bout du 1er octobre 2026

Conditions : Convex de développement (`standing-jay-887`), une console de Pi en
**simulation complète** sur un Mac (`MOTOR_BACKEND=sim`, `ECG_SOURCE=sim`,
caméra simulée « capsule occupée »), reliée par une vraie clé machine. Les
fonctions du site ont été appelées par la CLI au nom d'un compte admin
(section 3.2). **Aucun matériel, aucune personne, aucun navigateur.**

Données créées pour l'essai, laissées dans la base de développement : la machine
« Banc de test (simulation) », le patient « Pratiquant Test »
(`pratiquant.test@example.com`, FC max 180, né en 1990, droit de lancement sur
cette machine), et quatre séances. La séance du 2 octobre (section 5.4) y a
ajouté trois comptes de démonstration et quatre séances.

| # | Ce qui a été fait | Résultat |
|---|---|---|
| 1 | La console démarre avec `CONVEX_URL` et la clé | Heartbeat accepté ; la machine passe `online` ; `machines.live` rempli (`runMode: repos`). |
| 2 | Synchronisation des programmes | « 30 min » et « 45 min » arrivent dans `machine_profiles`. Avec des paliers cardiaques différents de ceux des programmes (165/175 au lieu de 148/158), la console n'envoie **aucun** programme : c'est le comportement voulu. |
| 3 | Séance **manuelle** à la console, cible 5 tr/min de sortie | Séance créée dans Convex (`kind: manual`, `origin: local`, sans pratiquant) ; machine `in_session` ; télémétrie à 1 Hz ; après STOP : `completed`, raison `operator_stop: fin du test manuel`. |
| 4 | Manuel déclaré « banc » alors que la caméra simulée voit quelqu'un | Refusé par la console : « une personne est vue dans la capsule alors que BANC est déclaré ». Rien n'est créé dans Convex. |
| 5 | `launchAutoSession` sans physiologie | Refusé : « The rider's max heart rate (or birth year) must be set by a manager before an auto session ». |
| 6 | FC max 150 pour une zone haute de 138 | Refusé : « Zone up to 138 bpm exceeds 90% of this rider's max heart rate (150 bpm → ceiling 135 bpm) ». |
| 7 | FC max 300 | Refusé : « Max heart rate must be within 100-220 bpm ». |
| 8 | Pratiquant né en 2012 | Refusé : « Rider is 13: auto sessions require at least 18 years ». |
| 9 | Programme inconnu | Refusé : « This programme is not on the machine ». |
| 10 | **Lancement AUTO à distance** (programme « 30 min ») | Séance `pending` ; la console la prend au tour d'interrogation suivant, repasse ses propres portes, l'arme ; la séance passe `active` avec le nom du pratiquant, sa FC max, son âge (35) et le nom du lanceur. |
| 11 | Second lancement pendant que le premier attend | Refusé : « A session is already waiting for this machine ». |
| 12 | Télémétrie et état en direct pendant la séance | `getSessionTelemetry` et `getMachineLive` renvoient les points à 1 Hz et un état non périmé. |
| 13 | **Arrêt demandé à distance** (`requestStop`) pendant l'échauffement | La console l'exécute comme un arrêt ordinaire, attribué au tableau de bord ; après la phase de récupération la séance passe `completed`, raison `operator_stop: arret demande depuis le tableau de bord`. |
| 14 | Droits de lancement | Accordé au patient ; listé avec le nom de celui qui l'a accordé ; refusé pour un admin (« Launch rights are granted to users; managers and admins already have them »). |
| 15 | Routes machine sans clé, puis avec une mauvaise clé | 401 dans les deux cas. |
| 16 | `users.getCurrentUser` après réglage de la FC max du compte | Fonctionne. Le défaut n° 3 de [convex.md](convex.md#9-défauts-connus-et-reste-à-faire) est donc corrigé. |

### Ce que l'essai a trouvé

| Constat | Conséquence |
|---|---|
| **Séance orpheline après un arrêt de la console.** Si le processus de la console s'arrête pendant une séance (arrêt du conteneur, coupure de courant), il met bien la consigne à zéro, mais il n'envoie pas la fin de séance à Convex. Au redémarrage il ne la reprend pas non plus. | La séance reste `active` dans Convex indéfiniment. La machine repasse `online` au redémarrage, mais l'historique garde une séance « en cours » qui ne l'est plus. À corriger : soit la console envoie la fin avant de sortir, soit Convex clôt les séances d'une machine passée hors ligne. |
| Avec l'ECG **simulé** de la console, la FC devient par moments « non fiable » : le superviseur lève la règle `hr_stale` (« no fresh trustworthy heart rate »), gèle ou réduit la vitesse. En séance AUTO le moteur n'avait pas démarré après une minute d'échauffement ; en séance manuelle la vitesse a été réduite après le palier. | C'est le superviseur qui fait son travail face à un signal simulé bruité, pas un défaut de Convex ni du site. Mais une démonstration d'une séance AUTO complète n'est pas possible aujourd'hui avec la console en simulation : il faut d'abord comprendre pourquoi son ECG simulé perd la confirmation dès que le bras tourne. |
| Les séances démarrées à la machine n'ont pas de pratiquant (`userId` absent). | Déjà connu (défaut n° 4 de convex.md) ; confirmé. |

### Ce que l'essai ne couvre pas

- Cet essai n'a ouvert aucune page du site. Les pages l'ont été le lendemain
  (section 5.4).
- Les rôles `gestionnaire` et `user` n'ont pas appelé de fonction d'écriture :
  seul un admin l'a fait.
- Rien sur du vrai matériel.

---

## 5. Le site sur Vercel et en local

### 5.1 Comment il se déploie

Le projet Vercel `anheart` est relié au dépôt GitHub `anheartpro-byte/Anheart`.
Un envoi sur `main` déploie la production ; un envoi sur une autre branche crée
une préversion. Il n'y a rien à lancer à la main.

### 5.2 Les préversions pointent sur la production

Comme les variables Vercel sont les mêmes partout (section 2), **une préversion
de la branche parle au Convex de production et au Clerk de production**. Une
préversion de `feat/pi-training-session` appelle donc des fonctions qui
n'existent pas encore en production : ses nouvelles pages ne peuvent pas marcher.

Pour qu'une préversion serve à tester, donner aux préversions leurs propres
valeurs dans les réglages du projet Vercel (environnement « Preview ») :
`NEXT_PUBLIC_CONVEX_URL` du développement, et les deux clés Clerk de
développement. Ne pas toucher aux valeurs « Production ».

### 5.3 Lancer le site en local sur le Convex de développement

```sh
npm install
npm run dev:frontend         # next dev seul ; npm run dev lance aussi convex dev
```

Il faut dans `.env.local` : `NEXT_PUBLIC_CONVEX_URL` (déjà écrit),
`NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` (déjà écrit) et `CLERK_SECRET_KEY` de
l'instance Clerk de **développement**. Sans la clé secrète, toute page répond
500 : « Missing secretKey ».

### 5.4 Séance de captures du 2 octobre 2026

Le site a tourné en local (`next dev`, port 3100) contre le Convex de
développement, avec une console de Pi en simulation complète reliée par sa clé.
Un navigateur piloté par script a ouvert les pages et pris les captures du
[guide du tableau de bord](guides/guide-tableau-de-bord.md) (37 fichiers
`docs/guides/img/site-*.png`).

Trois comptes de démonstration ont été créés dans l'instance Clerk de
**développement**, par son API d'administration (l'inscription par le
formulaire est protégée contre les robots) :

| Compte | Rôle | Réglages |
|---|---|---|
| Claire Martin, `claire.martin@example.com` | admin | - |
| Julien Bernard, `julien.bernard@example.com` | gestionnaire | gère la machine « Banc de test (simulation) », et les patients Léa Dubois et Pratiquant Test |
| Léa Dubois, `lea.dubois@example.com` | user | FC max 182, née en 1992, droit de lancement sur la machine de test |

Ces comptes, leurs lignes dans Convex et les séances de démonstration sont
restés dans les données de développement. Les supprimer : dans Clerk
(Development, Users) puis dans la page **Utilisateurs** du site.

Ce que les écrans ont montré, y compris les défauts, est consigné dans le guide,
[§9.4](guides/guide-tableau-de-bord.md#94-ce-que-les-vrais-écrans-ont-montré-2-octobre-2026).
Limites : les formulaires n'ont pas été soumis depuis le navigateur ; les
écritures (rôles, droits, physiologie, lancement, arrêt) ont été faites par la
ligne de commande, au nom des comptes de démonstration.

---

## 6. Le moteur de simulation hébergé

### 6.1 Ce que c'est

Le même moteur que `python -m simulation.live` (voir
[framework-de-test.md](framework-de-test.md#7-le-visualiseur-2d--simulationlive)),
servi par une fonction Vercel : la page du visualiseur 2D, la liste des
scénarios, et un flux qui exécute **un scénario à la demande** avec le vrai code
de `raspberry-pi/src` contre le variateur, la physiologie et l'ECG simulés.

Il ne parle à **aucune machine** : ni port Modbus, ni BITalino, ni Convex.

- En ligne : <https://anheart-simulation.vercel.app>
- Un scénario en direct : `https://anheart-simulation.vercel.app/viewer/index.html?live=manual_27_rpm&speed=20`
- Le rapport de la dernière batterie lancée en local : `/report.html`

![Le visualiseur hébergé pendant le scénario manual_27_rpm à 40x : capsule, pratiquant, vitesse de sortie, g aux pieds, état du variateur](guides/img/simulation-hebergee-manuel-27.png)

*Capture réelle du 1er octobre 2026, prise sur `anheart-simulation.vercel.app`.*

Le lien court `/?live=...` perd ses paramètres sur la version en ligne
aujourd'hui : passer par `/viewer/index.html?live=...`. C'est corrigé dans
`app.py` et prendra effet au prochain déploiement en production.

> L'adresse est **publique** : quiconque la connaît peut lancer un scénario, ce
> qui consomme du temps de calcul sur le compte Vercel du client. Elle ne montre
> ni donnée de patient ni secret. Pour la restreindre, activer la protection des
> déploiements dans les réglages du projet Vercel, ou supprimer le projet.

### 6.2 Différences avec la version locale

| Local (`python -m simulation.live`) | Hébergé |
|---|---|
| 60 scénarios | 56 : les 4 scénarios en mode `dsp` (chaîne BioSPPy) sont refusés avec un message. BioSPPy n'est pas installé, et ces scénarios coûtent environ 50 ms de calcul par seconde simulée. |
| Un scénario peut être donné par son chemin de fichier | Par son **nom** seulement, dans la liste livrée. |
| Vitesse de 0,1x à 200x | De **10x** à 200x : la fonction s'arrête au bout de 300 s, et 45 minutes de scénario à 10x en font 270. |
| `clock=sim` disponible | Ignoré. |
| Relecture d'une trace (`?trace=out/...`) | Indisponible : les traces ne sont pas publiées. |

### 6.3 Les fichiers

L'application et l'assemblage autonome sont dans `deploy/simulation-vercel/` :

| Fichier | Rôle |
|---|---|
| `app.py` | L'application FastAPI : `/`, `/viewer`, `/api/scenarios`, `/api/catalogue`, `/stream`. |
| `bitalino.py` | Un substitut du paquet `bitalino` : `src/bitalino_client.py` l'importe au chargement, et le vrai paquet demande une pile Bluetooth. Il refuse toute connexion. |
| `requirements.txt` | Sous-ensemble de `raspberry-pi/requirements-base.txt` (sans BioSPPy). |
| `vercel.json` | Durée maximale de la fonction : 300 s. |
| `build.sh` | Assemble `dist/` : ce dossier, `simulation/` (modules, scénarios, cohorte, géométrie CAO), `raspberry-pi/src` et `raspberry-pi/config`. |
| `deploy.sh` | `build.sh`, puis `vercel deploy`. |

Le projet Vercel relié à Git construit depuis la racine du dépôt, avec le
preset **FastAPI**. Vercel installe avec `uv` les dépendances de la table
`[project]` du `pyproject.toml` racine. Elles correspondent au sous-ensemble
hébergé ci-dessus ; garder les deux listes alignées lors d'une mise à jour.
Ce manifeste déclare `simulation_app:app` comme point d'entrée ; `simulation_app.py`
charge cette même application, les sources Pi et le substitut BITalino.
Le `requirements.txt` racine renvoie au fichier ci-dessus pour les installations
pip manuelles ; `.python-version` et le manifeste fixent Python 3.12. Le site
garde son preset **Next.js** et ses commandes npm.
Un push sur la branche de la PR crée une préversion ; il ne fusionne pas `main`.

Le visualiseur est monté depuis `simulation/viewer/`. Vercel peut le promouvoir
sur son CDN, et le même fichier reste accessible en HTTP local. `build.sh`
l'embarque aussi dans le paquet autonome, en plus de `public/viewer/`.
Voir la [documentation FastAPI de Vercel](https://vercel.com/docs/frameworks/backend/fastapi).

### 6.4 Redéployer

```sh
deploy/simulation-vercel/deploy.sh           # une préversion (protégée par la connexion Vercel)
deploy/simulation-vercel/deploy.sh --prod    # la production : anheart-simulation.vercel.app
```

Il faut une connexion de la CLI Vercel dans `.vercel-cli/` :
`npx vercel login --global-config .vercel-cli`.

Le déploiement manuel ci-dessus embarque le code **du disque** au moment de
`build.sh`. Le déploiement Git embarque le commit poussé. Une modification
locale de `raspberry-pi/src` ne change donc pas une version déjà hébergée.

> Le **tout premier** déploiement d'un projet Vercel est toujours affecté à la
> production, même sans `--prod`. C'est ce qui s'est passé le 1er octobre 2026.

Essayer en local avant de déployer :

```sh
deploy/simulation-vercel/build.sh
cd deploy/simulation-vercel/dist
python3.12 -m venv /tmp/simvenv && /tmp/simvenv/bin/pip install -r requirements.txt
/tmp/simvenv/bin/uvicorn app:app --port 8765
curl -N "http://127.0.0.1:8765/stream?scenario=manual_27_rpm&speed=200"
```

Depuis la racine, sans assemblage, on peut aussi lancer
`raspberry-pi/.venv/bin/uvicorn simulation_app:app --port 8765`
avec les dépendances hébergées installées dans cet environnement. Les deux
dispositions servent `/viewer/index.html` et le même flux de simulation.

Contrôles de l'adaptateur hébergé, depuis la racine :

```sh
raspberry-pi/.venv/bin/ruff check simulation_app.py deploy/simulation-vercel/app.py deploy/simulation-vercel/tests
raspberry-pi/.venv/bin/ruff format --check simulation_app.py deploy/simulation-vercel/app.py deploy/simulation-vercel/tests
raspberry-pi/.venv/bin/basedpyright --project pyproject.toml
raspberry-pi/.venv/bin/mypy --config-file pyproject.toml
raspberry-pi/.venv/bin/pytest deploy/simulation-vercel/tests
```

---

## 7. Le Raspberry Pi

### 7.1 Ce qui est prêt, et ce qui ne l'est pas

| Prêt | Vérifié comment |
|---|---|
| L'image Docker lance la **console** (`python -m src.local_panel`), et rien d'autre. | Construite pour ARM64 (la même architecture que le Pi 4 et 5), 1,8 Go. |
| `docker-compose.yml` : périphériques, réseau de l'hôte, redémarrage automatique, délai d'arrêt de 60 s. | `docker compose up` en simulation : conteneur « healthy », heartbeat et programmes reçus par Convex de développement. |
| Arrêt propre. | `docker stop` pendant une séance manuelle simulée : la console met la consigne à zéro, rend la liaison, sort avec le code 0. |
| Jeton d'accès quand la page n'écoute pas que sur la boucle locale. | 401 sans l'en-tête `x-anheart-token`, 200 avec. |
| `scripts/pi/preflight.sh` : contrôle avant démarrage, en lecture seule. | Lancé avec une bonne clé (accepté) puis une mauvaise (refusé). |

| Pas vérifié | Pourquoi |
|---|---|
| Le variateur réel depuis le conteneur (`/dev/ttyUSB0` ou `ftdi://`). | Pas de Pi ni de variateur sous la main. |
| Le BITalino réel depuis le conteneur (liaison `rfcomm`). | Idem. Le script d'entrée tente `rfcomm bind` ; sans Bluetooth il échoue avec un message clair et la console démarre quand même. |
| Le navigateur en plein écran au démarrage du Pi. | Le fichier est fourni, il n'a jamais été essayé. |
| Le temps de construction de l'image sur un Pi. | Construit sur un Mac. |

**`PROGRAMS_ENABLED` et `OCCUPANCY_OCCUPIED_ENABLED` restent à `false`** dans le
modèle de configuration : la machine déployée n'offre que le manuel banc, et le
bouton de lancement du site ne l'atteint pas. Les passer à `true` est une
décision de jalon (M5, M6), pas une étape de déploiement.

### 7.2 Préparer le Pi (une fois)

Sur un Raspberry Pi 4 ou 5, Raspberry Pi OS 64 bits :

```sh
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER        # puis se déconnecter et se reconnecter
sudo systemctl enable --now bluetooth
```

Appairer le BITalino (code 1234), une seule fois :

```sh
bash scripts/pair_device.sh 98:D3:91:FE:4E:9F
```

### 7.3 Copier et construire

Depuis le poste de développement, dans `raspberry-pi/` :

```sh
bash scripts/pi/deploy.sh pi@anheart-pi.local
```

Le script copie le dossier dans `~/anheart/raspberry-pi` du Pi et y construit
l'image. Il ne remplace **jamais** le `.env` ni le dossier `data/` du Pi.

### 7.4 Configurer

Sur le Pi :

```sh
cd ~/anheart/raspberry-pi
cp .env.pi.example .env
nano .env
```

À remplir :

| Clé | Valeur |
|---|---|
| `CONVEX_URL` | L'hôte **`.convex.site`** du déploiement visé (section 1). |
| `MACHINE_API_KEY` | La clé de 64 caractères affichée **une seule fois** à la création de la machine sur le site (admin). Vide = aucune liaison au tableau de bord. |
| `MOTOR_PORT` | `/dev/ttyUSB0`, ou `ftdi://schneider:rs485/1` si le noyau ne reconnaît pas le câble Schneider. |
| `BITALINO_MAC` | L'adresse du BITalino appairé. Le conteneur lie `/dev/rfcomm0` à cette adresse au démarrage. |
| `ARM_RADIUS_M`, `LEG_TIP_RADIUS_M` | **À mesurer** sur la machine. Tous les g affichés en dépendent. |
| `MOTOR_MAX_RPM` | Le plafond de banc. 300 par défaut (environ 6 tr/min au bras). |

Toutes les clés sont décrites dans
[raspberry-pi.md](raspberry-pi.md#12-la-configuration-env).

### 7.5 Vérifier, puis démarrer

```sh
bash scripts/pi/preflight.sh
docker compose up -d
docker compose logs -f
```

`preflight.sh` contrôle Docker, le `.env`, la présence du câble du variateur,
l'appairage du BITalino, la clé de la machine et le contrat que sert le
tableau de bord (il échoue si Convex répond 426 : le serveur ne sert pas la
majeure de contrat de cette console, voir
[Versions et compatibilité](convex.md#11-versions-et-compatibilité)). Il ne parle
jamais au variateur. Il sort avec le code 1 s'il trouve un point bloquant.

La première ligne des journaux résume ce que la console a compris de sa
configuration, par exemple :

```text
Console du banc sur http://127.0.0.1:8090/ - MANUEL BANC (plafond 300 tr/min moteur; paliers 148/158 bpm) - variateur: ... - ECG (serial): ... - rayon 1.5 m, i = 49.79 - tableau de bord: https://standing-jay-887.convex.site
```

La page s'ouvre sur le Pi lui-même : `http://127.0.0.1:8090/`. Pour l'afficher
en plein écran à l'ouverture de session :

```sh
mkdir -p ~/.config/autostart
cp scripts/pi/anheart-kiosk.desktop ~/.config/autostart/
```

### 7.6 Arrêter, mettre à jour

```sh
docker compose down          # arrêt : la console met la consigne à zéro avant de sortir
bash scripts/pi/deploy.sh pi@anheart-pi.local --start   # depuis le poste de développement
```

`--start` refuse d'agir si la console du Pi n'est pas au repos : on ne remplace
pas le logiciel d'une machine qui tourne.

Un redémarrage (du conteneur, du Pi) **ne relance jamais un mouvement** : la
console revient au repos, en lecture seule. Si elle trouve le variateur activé
au démarrage, elle commande zéro, verrouille, et attend un acquittement de
l'opérateur.

> Arrêter la console **pendant une séance** laisse la séance `active` dans
> Convex (section 4). Terminer la séance à la console avant d'arrêter.

### 7.7 Sans Docker

`scripts/anheart.service` lance la même console depuis un environnement virtuel,
par systemd. À n'utiliser que sur un Pi où Docker n'est pas voulu ; les
instructions sont en tête du fichier.

---

## 8. Reste à faire

| Quoi | Bloqué par |
|---|---|
| Relire [guides/guide-tableau-de-bord.md](guides/guide-tableau-de-bord.md) ligne à ligne contre les vrais écrans, et corriger les défauts du §9.4 (textes en anglais…) | Rien. |
| Soumettre les formulaires depuis le navigateur, dans les trois rôles ; en faire des tests automatiques (ANH-83) | Rien. |
| Comprendre pourquoi l'ECG simulé de la console perd la confirmation quand le bras tourne (section 4) | Rien. |
| Déployer Convex en production, puis fusionner la branche | Une décision (section 3.3). |
| Donner aux préversions Vercel les valeurs de développement | Un réglage dans Vercel (section 5.2). |
| Corriger la séance orpheline (section 4) | Un choix de conception : côté Pi ou côté Convex. |
| Premier démarrage sur un vrai Pi, avec le variateur et le BITalino | Le matériel. |
| Vérifier chaque préversion Git de la simulation avant fusion | `simulation/` et `deploy/` sont versionnés ; le projet Git construit depuis la racine avec `simulation_app:app`. |

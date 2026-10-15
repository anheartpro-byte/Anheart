# Secrets : inventaire et rotation

Chaque secret du projet : ce qu'il ouvre, où il vit, qui peut le lire ou le
remplacer, et comment le changer. Ticket
[ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite),
exigence EX-4. Inventaire dressé le 14 octobre 2026, à la lecture du code et
des pages de ce dépôt.

**Aucune des procédures de rotation de cette page n'a été essayée.** Les
essayer demande les vrais secrets et les vrais comptes : c'est au chef de
projet, une fois par secret, en écrivant ici la date et le résultat. Les
étapes viennent du code et des autres pages du dépôt ; ce qui en est déduit
sans être écrit ailleurs est signalé.

[Sommaire](README.md) · [Déploiement, section 2](deploiement.md#2-les-clés-et-où-elles-sont-rangées) · [Sécurité](securite.md) · [Menaces](menaces.md)

## 1. Les règles

* **Aucun secret dans le dépôt.** Le dépôt est public. `.gitignore` écarte
  tout fichier `.env*` sauf les modèles `.env.example`, ainsi que
  `.vercel-cli/`, `.clerk/`, `.codex/config.toml` et les fichiers `*.pem`.
  `scripts/ci/dependency-audit.test.mjs` vérifie que `.env.local`,
  `raspberry-pi/.env` et les autres fichiers de secrets connus sont ignorés,
  et qu'aucun fichier `.env` autre qu'un modèle n'est suivi par Git.
* **Un secret committé est un secret publié.** L'analyse gitleaks de la CI
  ([section 4](#4-détection-dans-le-dépôt)) le signale ; réécrire l'historique
  ne le dépublie pas. Le changer d'abord (section 3), nettoyer ensuite.
* **Dans GitHub, des secrets d'environnement seulement**, jamais des secrets du
  dépôt : un secret d'environnement n'est remis qu'aux jobs qui passent par cet
  environnement, et l'environnement est restreint à une branche
  ([deploiement.md, 5.5, étape 2](deploiement.md#55-réglages-à-faire-une-fois-à-la-main)).
* **Un secret n'est jamais affiché** : ni dans un ticket, un journal, une
  capture, une ligne de commande (que tout utilisateur d'une machine lit dans
  la liste des processus) ou un commentaire de PR. Les scripts du Pi passent
  le jeton et la clé de la machine à `curl` par son entrée standard
  ([pi-image.md](pi-image.md#3-ce-que-fait-scriptsinstallsh)).

## 2. L'inventaire

« Qui y a accès » dit qui peut le lire ou le remplacer, **par rôle**. Les
listes nominatives (membres des équipes Convex, Vercel et Clerk,
administrateurs du dépôt GitHub) n'ont pas été relevées : c'est au chef de
projet de les tenir.

| Secret | Ce qu'il ouvre | Où il vit | Qui y a accès | Rotation |
|---|---|---|---|---|
| Clé de la machine, `MACHINE_API_KEY` | les routes machine de Convex au nom d'une machine : heartbeat, séances, mesures ([MEN-01](menaces.md#men-01-clé-machine-volée-ou-rejouée)) | sur le Pi, `/etc/anheart/anheart.env` (root, mode 600) ; sur un poste de développement, `raspberry-pi/.env`. Convex ne garde qu'un vérificateur salé (HMAC-SHA-256) et un sélecteur, jamais la clé ([convex.md](convex.md#migration-des-credentials-avec-anh-82--opération-à-réaliser)) | root du Pi ; la personne qui l'installe ; l'admin ou le gestionnaire de la machine qui la crée ou la régénère, à qui elle est montrée une fois | [3.1](#31-clé-de-la-machine) |
| Jeton de la page de la console, `UI_TOKEN` (le « jeton d'appareil » du ticket) | la page et l'API de la console ; exigé dès qu'elle écoute hors de la boucle locale ([MEN-05](menaces.md#men-05-appareil-hostile-sur-le-réseau-de-la-console)) | `/etc/anheart/anheart.env` ; chaque tablette qui l'a saisi le garde dans le stockage de session de son navigateur | root du Pi ; chaque opérateur à qui il a été donné | [3.2](#32-jeton-de-la-page-de-la-console) |
| Clé privée TLS de la console | le certificat que les tablettes acceptent pour cette console | `/var/lib/anheart/tls` sur le Pi, au compte `anheart` ; l'autorité qui l'a signée n'est gardée nulle part | compte `anheart`, root ; quiconque a la carte SD | [pi-image.md, Renouveler](pi-image.md#renouveler) |
| Mot de passe du Wi-Fi de la console | le réseau que le Pi crée pour les tablettes | `/etc/anheart/wifi-password` (root, mode 600) ; la fiche privée de mise en service | root du Pi ; le technicien ; chaque tablette jointe | [3.3](#33-mot-de-passe-du-wi-fi-de-la-console) |
| Clé du volume chiffré des données du Pi | les enregistrements de séance au repos | `/etc/anheart/data.key` (root, mode 600), sur la même carte que le volume ([pi-stockage.md](pi-stockage.md)) | root du Pi ; quiconque a la carte SD | aucune procédure écrite ([section 5](#5-ce-qui-reste)) |
| Clés de signature « médical » et « catalogue » | prouver qu'un fichier de paramètres médicaux ou un programme a été signé par la personne habilitée | **n'existent pas encore** dans le code de `develop` | - | [3.4](#34-clés-de-signature) |
| Clé de déploiement Convex de **production** | déployer le backend de production, lire et écrire ses données et ses variables | prévue comme secret `CONVEX_DEPLOY_KEY` de l'environnement GitHub `production`, mais **pas encore créée** ([état au 14 octobre 2026](#ce-que-github-porte-aujourdhui)) ; `CONVEX_DEPLOY_KEY_PROD` dans `.env.local` du poste du responsable | une fois le secret créé : le job `convex` de la pipeline de release, et les administrateurs du dépôt (remplacer, pas relire) ; les membres de l'équipe Convex ; le poste qui a `.env.local` | [3.5](#35-clés-de-déploiement-convex) |
| Clé de déploiement Convex de développement | la même chose sur le déploiement de développement | `CONVEX_DEPLOY_KEY` dans `.env.local` | les membres de l'équipe Convex ; le poste qui a `.env.local` | [3.5](#35-clés-de-déploiement-convex) |
| Jeton Vercel, `VERCEL_TOKEN` | déployer le site **et** la simulation hébergée, en production comme en préversion ; lire les variables des deux projets ([MEN-21](menaces.md#men-21-service-de-simulation-hébergé-ouvert-à-internet)) | prévu comme secret des environnements GitHub `production` **et** `preview`, mais **pas encore créé** ([état au 14 octobre 2026](#ce-que-github-porte-aujourdhui)) | une fois le secret créé : les jobs des deux boutons de déploiement et de la pipeline, et les administrateurs du dépôt (remplacer) ; le compte Vercel qui crée le jeton | [3.6](#36-jeton-et-connexion-vercel) |
| Connexion de la CLI Vercel | la même chose, depuis un poste | `.vercel-cli/` à la racine du dépôt cloné, sur le poste ([deploiement.md, 2](deploiement.md#2-les-clés-et-où-elles-sont-rangées)) | le poste | [3.6](#36-jeton-et-connexion-vercel) |
| Clé secrète Clerk de **production**, `CLERK_SECRET_KEY` | l'API d'administration de l'instance Clerk : comptes, sessions, organisations | variable du projet Vercel `anheart` (le site, côté serveur) ; variable du déploiement Convex de production (`convex/users.ts`, `convex/lib/clerkWebhook.ts`) | les membres du projet Vercel et de l'équipe Convex ; les administrateurs de Clerk ; aujourd'hui aussi chaque préversion du site, qui reçoit les valeurs de production ([deploiement.md, 5.2](deploiement.md#52-les-préversions-pointent-sur-la-production)) | [3.7](#37-clé-secrète-clerk) |
| Clé secrète Clerk de développement | la même chose sur l'instance de développement | `.env.local` ; variable du déploiement Convex de développement | le poste ; l'équipe Convex | [3.7](#37-clé-secrète-clerk) |
| Secret de signature du webhook Clerk, `CLERK_WEBHOOK_SIGNING_SECRET` | faire accepter par Convex un événement d'organisation ou de compte comme venant de Clerk ([convex.md](convex.md#webhook-clerk-vers-le-miroir)) | variable de chaque déploiement Convex qui reçoit le webhook ; l'endpoint du webhook dans Clerk | l'équipe Convex ; les administrateurs de Clerk | [3.8](#38-secret-du-webhook-clerk) |
| Clé d'API Resend, `RESEND_API_KEY` (ANH-90) | envoyer des e-mails au nom du domaine vérifié du compte Resend ; lire les envois de ce compte | variable du déploiement Convex qui envoie les alertes ([convex.md](convex.md#composant-denvoi-de-mails-anh-90)) ; le composant d'envoi en garde une copie dans sa table (ANH-237) ; le compte Resend | l'équipe Convex ; les membres du compte Resend | [3.9](#39-clé-dapi-resend) |
| `GITHUB_TOKEN` | le dépôt, avec les droits que chaque workflow déclare : lecture seule, sauf la publication des résultats de CodeQL et l'écriture des tags par le job des tags de la pipeline de release | créé par GitHub pour chaque job, expire avec lui | le job | aucune |
| Configuration locale des agents, `.codex/config.toml` | ce qu'elle contient : selon `.gitignore`, elle peut porter des accès d'administration à un déploiement | le poste | le poste | celle du secret qu'elle contient |

**Ce qui n'est pas secret mais fait autorité.** Ces valeurs ne donnent aucun
accès à qui les lit ; les changer change le comportement du service, et seul
qui peut écrire les variables de l'hébergement peut le faire.

| Valeur | Où | Pourquoi elle compte |
|---|---|---|
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `NEXT_PUBLIC_CONVEX_URL` | projet Vercel `anheart`, `.env.local` ; figées dans le site à sa construction | publiques par nature : le navigateur les reçoit. Elles désignent l'instance Clerk et le déploiement Convex que le site utilise |
| `CLERK_JWT_ISSUER_DOMAIN` | déploiement Convex (lue par `convex/auth.config.ts`) ; le projet Vercel l'a aussi, le site ne la lit pas | l'émetteur dont Convex accepte les jetons |
| `ANHEART_ORG_ID` | déploiement Convex | l'organisation dont les admins sont admins Anheart, sur toutes les organisations ([MEN-16](menaces.md#men-16-secrets-ci-ou-artefacts-de-livraison-compromis), [convex.md](convex.md#activer-le-multi-organisation)) |
| `NOTIFICATION_EMAIL_FROM`, `SITE_URL` (ANH-90) | déploiement Convex | l'expéditeur des e-mails d'alerte, et l'adresse du site vers laquelle ils renvoient ; sans l'une des deux, ou sans `RESEND_API_KEY`, aucun e-mail n'est mis en file |
| `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID_SITE`, `VERCEL_PROJECT_ID_SIMULATION` | prévus comme secrets des environnements GitHub `production` et `preview`, pas encore créés | des identifiants, rangés en secrets avec le jeton ; ils ne changent que si l'équipe ou un projet Vercel est recréé |
| La clé publiable de test de la CI (`ci-fixture.clerk.accounts.dev`) | `.github/workflows/ci.yml` | synthétique : elle ne désigne aucune instance réelle. Seule exception de gitleaks pour une valeur ([section 4](#4-détection-dans-le-dépôt)) |

### Ce que GitHub porte aujourd'hui

Lu par l'API de GitHub le 14 octobre 2026 : les environnements `Production` et
`Preview` existent (GitHub ne distingue pas les majuscules dans ces noms),
ainsi que `Preview – anheart` et `Preview – anheart-simulation` ; aucun n'a de
règle de protection, **aucun ne porte de secret, et le dépôt non plus**. Les
boutons de déploiement et la pipeline de release ne sont donc pas encore
configurés ([deploiement.md, 5.5](deploiement.md#55-réglages-à-faire-une-fois-à-la-main),
étapes 1 et 2). Sur incident, il n'y a rien à remplacer dans GitHub tant que
ces étapes ne sont pas faites ; les clés vivent alors dans les comptes Convex
et Vercel, et sur le poste du responsable.

## 3. Changer un secret

Quand : à la compromission ou au soupçon, au départ d'une personne qui y avait
accès, à la perte d'un appareil, à l'expiration (le jeton Vercel en a une), et
sinon à un rythme que le chef de projet fixe (aucun n'est fixé à ce jour).

Chaque procédure dit ce qui est coupé pendant le changement. Les changements
qui touchent la production se font **dans la fenêtre d'un déploiement, jamais
pendant une séance** ([deploiement.md](deploiement.md#33-vers-la-production)).

### 3.1 Clé de la machine

Non essayée. Le code est testé (`convex/machineCredential.test.ts`,
`convex/machineAuth.test.ts`), sur des clés synthétiques.

1. Console **au repos**. Dès l'étape 2, la machine ne parle plus au tableau de
   bord jusqu'à l'étape 3 ; la console, elle, continue de fonctionner seule.
2. Sur le site, fiche de la machine, zone de danger : **Régénérer la clé**,
   puis **Confirmer** (admin, ou gestionnaire de la machine). La nouvelle clé
   s'affiche **une seule fois** ; l'ancienne est refusée aussitôt
   (`machines.regenerateApiKey` remplace sélecteur et vérificateur ensemble).
   Perdue avant l'étape 3 : régénérer encore, rien ne se récupère.
3. Sur le Pi : `sudo nano /etc/anheart/anheart.env`, remplacer la valeur de
   `MACHINE_API_KEY`, puis `sudo systemctl restart anheart` (au repos
   seulement).
4. Vérifier : la machine repasse en ligne sur le site. Une requête avec
   l'ancienne clé reçoit **401** ([convex.md, procédure ANH-82, étape 4](convex.md#migration-des-credentials-avec-anh-82--opération-à-réaliser)).

L'opération de redéploiement de la production (ANH-82) régénère de cette façon
la clé de chaque machine ([convex.md](convex.md#migration-des-credentials-avec-anh-82--opération-à-réaliser)).

### 3.2 Jeton de la page de la console

Non essayée. Rien à faire tant que la page n'écoute que sur la boucle locale
(`UI_HOST=127.0.0.1`).

1. Tirer une valeur aléatoire d'au moins 16 caractères, sur le Pi :
   `openssl rand -hex 16`.
2. `sudo nano /etc/anheart/anheart.env`, remplacer `UI_TOKEN`, puis
   `sudo systemctl restart anheart`, **au repos**.
3. Sur chaque tablette, saisir le nouveau jeton. L'ancien, gardé par le
   navigateur, reçoit **401**.

À faire aussi quand une tablette est perdue ou volée
([pi-image.md](pi-image.md#une-tablette-perdue-ou-volée)).

### 3.3 Mot de passe du Wi-Fi de la console

Non essayée, et **déduite du script** `scripts/pi/configure_network.sh` : ni
[pi-reseau.md](pi-reseau.md) ni une autre page ne la décrit. Le script garde
le mot de passe qu'il trouve, et en tire un nouveau (32 caractères
hexadécimaux) quand le fichier n'existe pas.

1. Console arrêtée : `sudo systemctl stop anheart`.
2. `sudo rm /etc/anheart/wifi-password`, puis
   `sudo bash scripts/harden.sh network`, et l'activation comme à la mise en
   service ([pi-reseau.md](pi-reseau.md#activer-et-vérifier)).
3. Reporter le nouveau mot de passe sur la fiche privée de mise en service ;
   rejoindre le réseau depuis chaque tablette.

### 3.4 Clés de signature

Elles n'existent pas encore dans le code de `develop`. D'après les tickets :
[ANH-139](https://linear.app/anheart/issue/ANH-139/parametres-medicaux-signes-fichier-versionne-verification-au-demarrage) (en cours) prévoit une clé
« médical » Ed25519 dont la partie privée reste au signataire, hors du Pi, et
dont la partie publique est livrée avec la console ;
[ANH-137](https://linear.app/anheart/issue/ANH-137/catalogue-de-programmes-dans-convex-ecrit-par-lequipe-anheart-seule) prévoit une clé
« catalogue », distincte. Leur procédure de rotation s'écrit avec ces tickets
et se range ici. Elle devra dire au moins : comment la nouvelle clé publique
arrive sur chaque Pi (par une version de la console), ce que devient un
fichier signé par l'ancienne, et comment l'ancienne est retirée.

### 3.5 Clés de déploiement Convex

Non essayée.

**Production.**

1. Tableau de bord Convex, projet `anheart`, déploiement **Production** choisi
   dans le sélecteur du haut, **Settings**, « URL & Deploy Key » : créer une
   nouvelle clé. Elle commence par `prod:` suivi du nom du déploiement de
   production ([deploiement.md, 5.5, étape 2](deploiement.md#55-réglages-à-faire-une-fois-à-la-main)).
2. GitHub : **Settings**, **Environments**, `production`, « Environment
   secrets » : remplacer `CONVEX_DEPLOY_KEY`. Ni dans `preview`, ni parmi les
   secrets du dépôt.
3. Sur le poste du responsable : remplacer `CONVEX_DEPLOY_KEY_PROD` dans
   `.env.local`.
4. Retirer l'ancienne clé dans la même page du tableau de bord. Que l'écran
   permette de retirer une clé précise n'a pas été vérifié.
5. Vérifier sans rien écrire, depuis le poste, à la racine du dépôt :

   ```sh
   key="$(node -e 'process.stdout.write(require("node:util").parseEnv(require("node:fs").readFileSync(".env.local", "utf8")).CONVEX_DEPLOY_KEY_PROD ?? "")')"
   case "$key" in
     "prod:clean-giraffe-153|"?*) CONVEX_DEPLOY_KEY="$key" npx convex data ;;
     *) echo "CONVEX_DEPLOY_KEY_PROD absente, ou qui ne vise pas la production" ;;
   esac
   ```

   La valeur est lue comme la lit la CLI, guillemets éventuels retirés
   (`util.parseEnv` de Node), et jamais affichée. Une valeur vide ou qui vise
   un autre déploiement est refusée avant d'appeler la CLI : vide, la CLI
   prendrait la clé de développement de `.env.local`. La commande liste les
   tables de la production. Au premier lancement réel de la pipeline, son job
   `convex` refuse de même une clé qui ne vise pas la production, sans rien en
   afficher.

**Développement.** Les mêmes étapes sur le déploiement de développement, pour
`CONVEX_DEPLOY_KEY` dans `.env.local` ; la CLI la lit seule.

### 3.6 Jeton et connexion Vercel

Non essayée. Le jeton est commun aux deux projets et aux deux environnements :
le changer touche tout, et une fuite aussi (MEN-21, décision de séparation
ouverte).

1. Vercel, **Account Settings**, **Tokens** : créer un jeton, portée
   « Anheart's projects », avec une date d'expiration
   ([deploiement.md, 5.5, étape 2](deploiement.md#55-réglages-à-faire-une-fois-à-la-main)).
2. GitHub : remplacer `VERCEL_TOKEN` dans l'environnement `production` **et**
   dans `preview`.
3. Vercel : supprimer l'ancien jeton.
4. Vérifier : le bouton de préversion avec `simulation`, qui ne touche à
   aucune donnée. À l'expiration d'un jeton, les boutons échouent à la
   première étape qui appelle Vercel : ce sont les mêmes étapes.

**La connexion de la CLI sur un poste** :
`vercel logout --global-config .vercel-cli`, puis une nouvelle connexion avec
le même `--global-config`.

### 3.7 Clé secrète Clerk

Non essayée. Pendant le changement, ce que le site fait côté serveur et ce que
Convex demande à l'API de Clerk peut échouer : l'invitation d'un patient
(`createPatient`) est refusée (`invitation_failed`), et l'ajout d'un membre reçu
par le webhook répond 503 en attendant une nouvelle livraison
([convex.md](convex.md#webhook-clerk-vers-le-miroir)).

1. Clerk, instance de production, **API keys** : créer une nouvelle clé
   secrète. Si l'écran ne permet pas d'en avoir deux à la fois, la renouveler
   coupe l'ancienne aussitôt : faire alors les étapes 2 et 3 dans la foulée
   (non vérifié).
2. Vercel, projet `anheart`, **Environment Variables** : remplacer
   `CLERK_SECRET_KEY` (type « Secret ») partout où elle porte la valeur de
   production. Un changement de variable ne touche que les déploiements
   suivants : redéployer le site (bouton « Déployer en production (main) »,
   `site`).
3. Convex, déploiement de production, **Settings**, « Environment
   Variables » : remplacer `CLERK_SECRET_KEY`. Par le tableau de bord plutôt
   que par `npx convex env set`, qui mettrait la valeur sur une ligne de
   commande.
4. Clerk : supprimer l'ancienne clé.
5. Vérifier : une connexion au site ; une invitation de patient envoyée ; un
   ajout de membre reçu par le webhook sans réponse 503.

**Développement** : les mêmes étapes sur l'instance de développement, pour
`.env.local` et le déploiement Convex de développement.

### 3.8 Secret du webhook Clerk

Non essayée.

1. Clerk, **Webhooks**, l'endpoint du déploiement visé : renouveler son secret
   de signature.
2. Convex, le même déploiement, « Environment Variables » : remplacer
   `CLERK_WEBHOOK_SIGNING_SECRET`.
3. Les livraisons refusées entre les deux (401) sont listées par Clerk : les
   rejouer depuis son tableau de bord. Un événement déjà traité est acquitté
   sans nouvelle écriture ([convex.md](convex.md#webhook-clerk-vers-le-miroir)).

Ce que dit le code (`verifyClerkSignature`, `convex/lib/clerkWebhook.ts`) : une
livraison est acceptée si **l'une** des signatures `v1` de son en-tête
correspond au secret configuré. Si Clerk signe un temps avec l'ancien et le
nouveau secret après un renouvellement, l'une ou l'autre valeur suffit pendant
ce temps (non vérifié du côté de Clerk).

### 3.9 Clé d'API Resend

Non essayée ; aucun compte Resend n'existe encore (décision du chef de
projet, ANH-237).

1. Resend, **API Keys** : créer une nouvelle clé, limitée à l'envoi.
2. Convex, le déploiement qui envoie, « Environment Variables » : remplacer
   `RESEND_API_KEY`.
3. Resend : supprimer l'ancienne clé.

Entre 2 et 3, les deux clés sont valides chez Resend. Une alerte écrite
pendant le changement reste visible sur le site quoi qu'il arrive à son
e-mail. Retirer la variable arrête les **nouvelles** mises en file
([convex.md](convex.md#composant-denvoi-de-mails-anh-90)) ; les e-mails déjà
en file partent encore, avec la copie de la clé que garde le composant. Sur
une clé compromise, c'est sa suppression chez Resend (étape 3) qui coupe tout.

## 4. Détection dans le dépôt

* **gitleaks, à chaque exécution de la CI**, PR comprises, dans le job
  `audit`, vérification obligatoire : tout l'historique Git
  (`--log-opts=--all`), avec les règles par défaut de l'outil. La version est
  figée (`v8.24.3`), installée par `go install`, qui vérifie le module contre
  la base de sommes de contrôle de Go. Une exception seulement pour une
  valeur : la clé publiable synthétique de la CI, à sa valeur exacte et à son
  chemin exact (`.gitleaks.toml`) ; et deux faux positifs historiques,
  relus, par leur empreinte (`.gitleaksignore`). Le test
  `scripts/ci/gitleaks-fixture.test.mjs` exerce le vrai scanner sur ces
  exceptions avant l'analyse
  ([framework-de-test.md](framework-de-test.md#15-ci)).
* **L'analyse des secrets de GitHub et sa protection au push** (« Secret
  scanning », « Push protection ») : **désactivées** à la lecture du
  14 octobre 2026 (API de GitHub). Gratuites pour un dépôt public, elles
  arrêteraient un secret connu avant qu'il n'arrive dans l'historique, ce que
  gitleaks, qui juge après le push, ne fait pas. Les activer est un réglage du
  dépôt, au chef de projet.

## 5. Ce qui reste

| Quoi | Qui |
|---|---|
| Essayer chaque procédure de la section 3 une fois, et écrire ici la date et le résultat | le chef de projet, avec les vrais comptes |
| Tenir la liste nominative de qui a accès à chaque compte (GitHub, Convex, Vercel, Clerk) | le chef de projet |
| Fixer un rythme de rotation hors incident | le chef de projet |
| Écrire la rotation des clés de signature | ANH-139, ANH-137 |
| Écrire la rotation de la clé du volume chiffré du Pi | le lot stockage d'ANH-151 |
| Un jeton Vercel par projet, ou un jeton commun assumé par écrit | décision ouverte de MEN-21 |
| Séparer les variables de préversion de Vercel : aujourd'hui une préversion reçoit la clé secrète Clerk de production | [deploiement.md, 5.5, étape 6](deploiement.md#55-réglages-à-faire-une-fois-à-la-main) |
| Activer l'analyse des secrets de GitHub et sa protection au push | le chef de projet (réglage du dépôt) |

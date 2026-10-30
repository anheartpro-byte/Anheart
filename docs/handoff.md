# Passation du projet Anheart (30 octobre 2026)

Ce document est le point de départ de tout agent ou de toute personne qui reprend le développement d'Anheart. Il remplace la mémoire de la machine de campagne, qui a été effacée. Lis-le en entier avant d'agir, puis `AGENTS.md`, `docs/roadmap.md`, `docs/deploiement.md` et `docs/release.md`.

Les détails de sécurité ne sont pas dans ce fichier : le dépôt est public. Ils sont dans Linear (projet Roadmap Software, équipe ANH).

## 1. État au moment de la passation

Fin de la campagne Orca du 15 au 30 octobre 2026 (pilote et coordinateurs Claude Opus 5.5). Tout est sur GitHub et dans Linear ; rien ne dépend plus de la machine de campagne.

- `develop` : `959ecd4` (ANH-83). Toutes les PR de la campagne sont fusionnées par squash, CI verte vérifiée après chaque fusion (CI, Pi install, CodeQL).
- Fusionné depuis le 25 octobre : ANH-249 (avis de sécurité Next.js, undici, zod, setuptools), ANH-231 (séances interrompues), ANH-234 EX-7 (temps en zone compté par le Pi), ANH-92 (lecture STO stricte, puis exception de banc déclarée), ANH-226 (compteurs du tableau de bord), ANH-239 (liste des passagers de la console), ANH-146 (retour du passager, contrat 1.7), ANH-240 (confirmation répétée du départ, contrat 1.8), ANH-101 (`hr_drop` en manuel occupé, décision S07), ANH-83 (tests de bout en bout dans un navigateur, Playwright).
- Production : Convex prod porte le code de `f0bccf7` (aucun changement Convex depuis) ; site et simulation hébergée redéployés le 30 octobre au commit `959ecd4` (`/commit.txt` le confirme sur les quatre domaines).

### Production

- Convex production : déploiement `clean-giraffe-153`. Convex développement : `standing-jay-887`.
- Site : projet Vercel `anheart` (portée `anhearts-projects`), domaines `www.gauratechnologies.com`, `gauratechnologies.com`, `www.anheart.ai`, `anheart.ai`. Chaque déploiement sert son commit à `/commit.txt`.
- Simulation hébergée : projet Vercel `anheart-simulation`, `https://anheart-simulation.vercel.app` (publique, ne lit aucune donnée).
- Clerk : instance de production `clerk.gauratechnologies.com`, instance de développement `major-macaw-92.clerk.accounts.dev`.
- `main` est resté à `cb41ac5` sans tag : la production a été déployée à la main depuis `develop` (chemin « B », voir section 6). La prochaine release officielle (pipeline « Release en production (main) ») réalignera `main`.
- Multi-organisation : migration faite (organisation « Anheart » sans identifiant Clerk, tous les comptes, machines et séances rattachés), mais pas activée : `ANHEART_ORG_ID`, le webhook Clerk et les réinvitations sont la « seconde fenêtre » (section 4). Tant qu'elle n'est pas faite, un nouveau compte n'arrive pas dans Convex et une invitation de patient n'aboutit pas.
- Alertes par e-mail coupées (variables Resend absentes, décision de l'utilisateur).
- Paramètres médicaux : la version du dépôt (signée par une clé de test, valeurs provisoires `[MED]`) n'est PAS enregistrée en production, décision de l'utilisateur. Toute console est en mode banc tant que la clé médicale réelle et les valeurs validées (ANH-98) n'existent pas.

### Matériel et sécurité (décisions de l'utilisateur)

- Périmètre matériel de la V1 : moteur et variateur ATV320, BITalino avec ECG. Tout le reste est en V2 : frein (ANH-93), bouton d'arrêt de la capsule (ANH-94), harnais et porte (ANH-95), caméra réelle (ANH-96), survitesse indépendante (ANH-97). Ces tickets sont en Backlog avec un commentaire « V2 » et des notes de reprise ; la branche `feature/anh-94-bouton-darret-dans-la-capsule-pour-le-passager` garde un premier commit utile au frein.
- STO (Safe Torque Off) : ANH-92 a rendu la console stricte : le moteur réel n'arme que si une source STO réelle est câblée et prête. L'utilisateur n'aura pas d'arrêt d'urgence câblé au départ ; il coupera la prise secteur. La seconde PR d'ANH-92 ajoute pour cela `STO_SOURCE=bridged_declared` : armement permis seulement pour une séance de banc, attestation honnête, bandeau permanent, personne à bord toujours refusée. Sans frein, couper le secteur laisse le bras en roue libre (environ 145 s depuis la vitesse maximale selon le modèle de simulation, jamais mesuré) : personne dans le rayon du bras jusqu'à l'arrêt complet.
- Règle d'urgence sans arrêt d'urgence câblé (décision de l'utilisateur du 30 octobre) : tant que la console répond, l'arrêt d'urgence est l'E-STOP de l'écran (arrêt en 3 à 4 s) ; couper la prise seulement si la console ne répond pas, si l'E-STOP n'est pas confirmé, si la vitesse ne baisse pas ou si le logiciel se comporte anormalement, jamais pendant une descente qui fonctionne. Un STOP normal en séance manuelle dure environ 107 s. Après une coupure secteur : attendre l'arrêt complet constaté, puis suivre `docs/raspberry-pi.md` (procédure de banc sous le pont, étapes 8 et 9) : `sto_open` et `drive_fault` s'acquittent par leur nom ; `comms_lost` et `go_silent` exigent de relancer la console et de refaire la déclaration.
- Premiers essais réels : capsule vide, puis lestée d'un sac de sable d'environ 80 kg, vitesse pilotée à la console, aucune personne à bord. Lancer une séance de banc lestée depuis le site est le ticket ANH-250 (à faire en premier, section 3).
- Aucune personne à bord tant que ANH-98 (valeurs médicales signées), ANH-103, ANH-104, ANH-105 et ANH-107 ne sont pas faits. Ne retire jamais ce verrou sans ces décisions écrites.

## 2. Actions qui attendent l'utilisateur

1. Clerk production : rôles `org:admin`, `org:gestionnaire`, `org:patient` présents, rôle par défaut `org:patient` (pas `org:member`, refusé par Convex) ; modèle de jeton `convex` avec `email`, `email_verified`, `org_id`, `org_role` (voir ANH-177 pour la raison).
2. Clerk production : vérifier la limite de membres par organisation de l'offre Clerk (l'instance de développement plafonne à 5 ; au-delà, le site dit seulement que l'invitation n'a pas été confirmée).
3. Convex production : poser `CLERK_SECRET_KEY` de l'instance Clerk de production (manquante au déploiement ; sans elle, l'invitation d'un patient échoue).
4. Vérifier le site en production connecté (liste dans ANH-82, commentaire du 29 octobre).
5. Seconde fenêtre multi-organisation (section 4).
6. Tests de bout en bout dans la CI : créer une instance Clerk de développement dédiée aux tests, sans webhook, puis le secret `E2E_CLERK_SECRET_KEY` et les variables `E2E_CLERK_PUBLISHABLE_KEY`, `E2E_CLERK_JWT_ISSUER_DOMAIN` dans GitHub, puis reprendre en PR le job `e2e` sauvegardé sur la branche `backup/anh-83-job-e2e` (il n'est pas encore sur `develop` ; une fois fusionné, il saute tant que ces secrets manquent).
7. Décisions humaines ouvertes : ANH-98 (valeurs médicales et clé), ANH-172 (données de santé), ANH-233 (séance interrompue sans retour), ANH-105 (statut réglementaire), ANH-87 (hébergement des données de santé).
8. Essais au banc (jalons B et D) : plan dans ANH-76 à ANH-81, ANH-151, ANH-162 à ANH-164 ; essais moteur seulement avec le mode `bridged_declared` ou un arrêt d'urgence câblé.

## 3. Suite du backlog

Dans cet ordre de priorité :

1. **ANH-250** (créé le 29 octobre à la demande de l'utilisateur) : lancer depuis le site une séance de banc lestée, confirmée à la console. À faire avant les essais réels avec le sac de sable. Contrat Pi-Convex : prochaine mineure (1.9).
2. **ANH-142** (contrôles pré-vol, en Todo) : décisions du pilote et brief du développeur dans le commentaire Linear du 29 octobre ; aucun code n'existe. Décision clé : pour une séance de banc, horloge non fiable, BITalino absent ou batterie basse sont des avertissements ; avec une personne à bord, ils bloquent.
3. **ANH-83, reste** : le job `e2e` de la CI (patch relu sauvegardé sur la branche `backup/anh-83-job-e2e`, à reprendre en PR après l'action 6 de la section 2) et le scénario d'incident (bloqué par ANH-145).
4. **ANH-211** : retirer `machines.config` du schéma ; la migration a tourné sur dev et sur la prod le 28 et le 29 octobre (vérifier d'abord par la requête en lecture seule du ticket).
5. Après la seconde fenêtre multi-organisation : ANH-188 (rendre `organizationId` obligatoire).
6. Pipeline de release (ANH-220) : premier lancement à blanc sur `main`, puis release officielle qui réaligne `main` et pose les tags.
7. Clôtures en attente d'une vérification en production : ANH-82, ANH-121 (clé neuve d'un Pi acceptée, ancienne refusée), ANH-146 (CA-1), ANH-226 (mesure sur un déploiement), ANH-234 (EX-2 avec ANH-82).
8. Suivis sans ticket notés dans les sections « Suivi Orca » des tickets fusionnés (par exemple : mode STO absent de l'enregistrement, ANH-92 ; règle de retour arrière du contrat 1.8, ANH-240 ; relances du tableau de bord par heartbeat au-delà de quelques centres, ANH-226).

- Bloqués par une décision ou du matériel : voir la section 2 et les commentaires Linear de chaque ticket.

## 4. Seconde fenêtre multi-organisation (production)

Source : `docs/tableau-de-bord.md` (« Amorçage d'un déploiement ») et `docs/convex.md` (« Activer le multi-organisation »).

1. L'utilisateur crée l'organisation « Anheart » dans Clerk production, sans membre, et note son identifiant `org_…`.
2. `npx convex env set ANHEART_ORG_ID org_…` sur la production, puis relancer `migrations/multiOrganization:attachExistingRowsToAnheart`.
3. Webhook Clerk vers `https://clean-giraffe-153.convex.site/clerk/webhook` (sept événements listés dans la doc), secret dans `CLERK_WEBHOOK_SIGNING_SECRET` de Convex prod ; tester puis rejouer un événement.
4. Inviter les admins Anheart (`org:admin`), puis faire entrer chaque compte existant avec son rôle.

## 5. Comment travailler sur ce dépôt

### Outils et skills

- Les skills de campagne sont dans `.agents/skills/anheart-backlog` (pilote) et `.agents/skills/anheart-ticket` (coordinateur d'un ticket), avec leurs points d'entrée Claude dans `.claude/skills/`. Les skills `anheart-strict-python` (code Python du Pi, obligatoire) et `anheart-ticket-workflow` sont aussi dans le dépôt.
- Orchestration avec Orca : un pilote, un coordinateur par ticket (Opus 5.5), des workers supervisés (développeur, exécuteur de tests, relecteurs R1 et R2 en sessions distinctes), tous en Claude Opus 5.5 depuis le 23 octobre 2026 (Codex connecté par un compte ChatGPT refusait `gpt-6.1-sol`). Vérifie toujours le modèle effectif dans le transcript du worker, pas seulement le reçu de lancement.
- Un coordinateur passe dans son Run enfant : pour lui écrire, `orca orchestration send --to run:<Run enfant>` ; `dispatch:<id>` est refusé.
- `orca linear ...` sans `--workspace` (refusé depuis le 21 octobre). Linear réécrit la description à l'enregistrement (liens, crochets échappés) et peut répondre `write_unconfirmed` alors que l'écriture est faite : relire avant de réécrire. L'intégration GitHub passe un ticket à Done à la fusion de sa PR : le remettre en In Progress s'il reste des critères.

### Règles de l'utilisateur

- Jamais le caractère tiret cadratin dans un texte ; le moins de tirets possible.
- Aucune mention d'IA dans les commits, PR, fusions ou commentaires (pas de Co-Authored-By, pas de « Generated with »). Fusion squash avec corps vide.
- Passer le ticket Linear à In Progress avant tout travail. Aucun nouveau ticket Linear sans demande explicite de l'utilisateur ; les suites vont dans la section « Suivi Orca » du parent.
- Au plus 3 tickets actifs, un développeur par ticket, une fusion à la fois, au plus 2 processus de calcul lourds par ticket.
- Aucun déploiement, secret ou réglage externe sans accord explicite ; l'accord vaut pour la demande en cours.
- Bloquer en cas de danger : un défaut de sécurité reste strict, toute exception est déclarée, honnête et limitée.

### Qualité et fusion

- `develop` exige : `pi-gate`, `simulation-gate`, `convex-tests`, `web`, `audit`, `docs` et le statut `agent-review/R1`, branche à jour, historique linéaire, conversations résolues. Aucun workflow ne pose `agent-review/R1` : le coordinateur le publie (`gh api repos/anheartpro-byte/Anheart/statuses/<sha>`) après avoir lu le rapport du relecteur indépendant sur le SHA exact.
- Couverture : 80 % de lignes et de branches sur le site et sur Convex (seuil appliqué fichier par fichier, `scripts/ci/coverage-thresholds.mjs`) ; 100 % des branches sur la chaîne de sécurité du Pi, dont la liste fait foi dans `raspberry-pi/pyproject.toml` (`[tool.anheart] safety_chain`). Sur la chaîne de sécurité (Pi, et côté Convex `convex/training.ts`, `convex/lib/auth.ts`, `convex/http.ts`) : 1 développeur et 2 relecteurs qui écrivent les invariants vérifiés.
- CodeQL seul (pas de Sonar) : une alerte ouverte sur une PR bloque la fusion. La seule alerte ouverte sur `develop` est la 407 (`raspberry-pi/src/web/ws.py`), antérieure.
- gitleaks parcourt toutes les branches distantes : un littéral de test au format d'une clé sur n'importe quelle branche bloque l'audit de toutes les PR ; construire ces valeurs à l'exécution.
- Protocole de fusion : vérifier tête, base à jour, checks, `agent-review/R1`, zéro fil, zéro alerte, aucune trace d'IA et périmètre ; `gh pr merge <n> --squash --match-head-commit <sha> --subject "ANH-x: ... (#n)" --body ''` ; puis vérifier la CI du push (CI, Pi install, CodeQL) sur le SHA fusionné avant la fusion suivante.
- `git push --force-with-lease=<branche>` (la forme `ref:sha` est rejetée par git 2.56).
- Vercel ne déploie plus rien sur push (`vercel.json`) ; chaque push de PR ne coûte donc plus de déploiement.

### Environnement local

- Partager les dépendances : un seul `node_modules` et un seul `raspberry-pi/.venv` à jour avec les verrous de `develop`, liés par lien symbolique dans chaque worktree ; ne rien y installer depuis un worktree. Un ticket qui change un verrou travaille avec son propre `node_modules`.
- Gate Pi locale : utiliser un `TMPDIR` court appartenant au groupe de l'utilisateur (sinon 35 tests d'installation échouent sur macOS).
- `npx convex logs` ne s'arrête jamais : le borner.
- Après un redémarrage, git HTTPS peut ne plus avoir d'identifiants : `git -c credential.helper='!gh auth git-credential' push ...`.

## 6. Déployer la production (chemin manuel, dit « chemin B », utilisé le 29 et le 30 octobre)

Sur une nouvelle machine, recréer d'abord (aucune valeur n'est dans le dépôt) : clés de déploiement Convex (tableau de bord Convex, réglages du projet : une pour dev, une pour prod), connexion Vercel (`npx vercel login`), `gh auth login`, connexion Linear dans Orca, clés Clerk de développement pour le site en local.

1. Worktree propre au commit visé, `npm ci --ignore-scripts`.
2. Répétition sur dev : `CONVEX_DEPLOY_KEY=<clé dev> npx convex dev --once`, puis les migrations (ci-dessous).
3. Sauvegarde de prod dans le tableau de bord Convex (« Backup Now »).
4. Site construit sans domaines : `echo <sha> > public/commit.txt`, `npx vercel@62.1.0 link --project anheart`, supprimer tout `.env*` créé, `npx vercel@62.1.0 deploy --prod --skip-domain`.
5. `CONVEX_DEPLOY_KEY=<clé prod> npx convex deploy` (point de non-retour du code : l'ancien code Convex ne doit pas être redéployé).
6. Aussitôt : `migrations/multiOrganization:attachExistingRowsToAnheart`, puis `npx vercel@62.1.0 promote <url du déploiement> --yes` et vérifier `/commit.txt` sur les quatre domaines.
7. Migrations, chacune relancée une seconde fois pour vérifier qu'elle ne change plus rien : `migrations/retireLegacyRecording:failOpenRecordingSessions`, `migrations/retireLegacyRecording:removeMachineConfig`, `migrations/machineApiKeys:moveMachineKeysToTable`, `sessionReportsBackfill:backfillSummaries '{}'` (une seule fois : il se relance seul).
8. Contrôles : `softwareReleases:deployedCloudVersion`, `/api/machine/training/poll` répond 401 sans clé, aucune erreur répétée dans les journaux.

Ordre quand le contrat Pi-Convex change : Convex d'abord, puis le site, puis l'image du Pi.

La pipeline officielle (`.github/workflows/release.yml`) exige l'environnement GitHub `production` avec ses secrets (Vercel et `CONVEX_DEPLOY_KEY`), des approbations et un premier lancement à blanc (ANH-220) ; rien de cela n'est en place.

## 7. Installer la console sur le Raspberry Pi de la machine

`docs/pi-image.md` (section 6) et `docs/raspberry-pi.md`. Sur le site de production : créer la machine, puis « Nouvelle clé » (affichée une seule fois). Sur le Pi : `CONVEX_URL` de production, la clé, `MOTOR_BACKEND=serial`, et pour les essais sans arrêt d'urgence câblé `STO_SOURCE=bridged_declared` (fusionné le 30 octobre, PR #134). Retirer `MIN_RIDER_AGE` et toute autre clé médicale de l'ancien `.env` (sinon la console sort en code 2). L'image doit porter le contrat de `develop` (1.8 au 29 octobre).

## 8. Branches de sauvegarde

Poussées le 29 octobre pour ne rien perdre avec la machine ; à supprimer quand plus personne n'en a besoin :

- `feature/anh-94-bouton-darret-dans-la-capsule-pour-le-passager` : premier commit pour la V2 (frein et bouton de capsule).
- `backup/anh-83-job-e2e` : le job `e2e` de la CI et sa garde, relus mais non fusionnés (ANH-83, en attente de l'instance Clerk de test).
- `backup/anh-190-wip-docs`, `backup/omo-anh-175`, `backup/omo-anh-131`, `backup/omo-anh-128-wip` : travaux locaux non poussés de tickets déjà Done (ANH-190, ANH-175, ANH-131, ANH-128), dont deux états de fichiers non commités. Probablement périmés.

## 9. Leçons de la campagne d'octobre

- Un reçu de lancement d'agent ne prouve pas le modèle : lire le début du transcript.
- Un redémarrage de la machine tue tous les workers sans prévenir : vérifier l'activité des transcripts et relancer par `--retry-of`.
- Le disque plein fait échouer les tests et charge la machine : surveiller l'espace avant de lancer des agents, supprimer les copies d'essai de `node_modules`.
- Les avis de sécurité des dépendances arrivent sans prévenir et rendent l'audit rouge pour toutes les PR : un ticket prioritaire, fusionné en premier, puis les autres rebasent.
- Un texte de ticket peut contredire une règle de sécurité ou une décision récente : le coordinateur le signale, le pilote tranche, l'écart est écrit dans la PR.

# Suivi des tickets logiciels

Instantane du 2 octobre 2026. Linear reste la source de verite des etats et des dependances.

## Perimetre

Projet **Roadmap Software** uniquement : 93 tickets ouverts. La file de developpement autonome contient 62 tickets portant les etiquettes `agent` et `aucun`. Les 31 tickets humains ou necessitant le banc ne sont pas des travaux a executer dans cette file ; leurs decisions peuvent cependant bloquer un ticket logiciel.

Sources : [file d'execution](https://linear.app/anheart/document/file-dexecution-pour-les-agents-ordre-des-tickets-b457cc879745), [processus de developpement](https://linear.app/anheart/document/processus-de-developpement-branches-tickets-pr-revue-par-agents-ci-e1d3adb79659), [roadmap](roadmap.md).

Un seul ticket est implemente a la fois. Avant de commencer : lire sa description complete avec ses EX-n, verifier ses bloqueurs dans Linear, identifier les fichiers et tests existants. Avant de passer au suivant : satisfaire les criteres, lancer les controles et exercer le comportement reel. **Done** exige la PR fusionnee dans `develop`, la CI verte, deux approbations et le commentaire de cloture. Un resultat local ne vaut pas une cloture Linear.

### Convention Linear des prochaines branches et commits

Décision utilisateur du 3 octobre 2026, enregistrée dans le skill
[anheart-ticket-workflow](../.agents/skills/anheart-ticket-workflow/SKILL.md) :
les prochains tickets utilisent le `gitBranchName` généré par Linear, au format
`feature/<identifiant-en-minuscules>-<slug>`, et les nouveaux commits commencent
par l'identifiant réel, par exemple `ANH-72: ajouter la gate CI`. `ENG-123` est
un exemple, pas un préfixe à substituer aux identifiants Anheart.

La PR #3 / ANH-71 garde explicitement son ancien nom de branche. Aucun renommage,
fermeture ou remplacement de cette PR n'est demandé. Les commits déjà publiés
ne sont pas réécrits pour cette nouvelle convention. Les prochains commits de
ce ticket commencent quand même par `ANH-71:`. Le skill est également installé
dans les skills utilisateur Codex pour être retrouvé lors des sessions futures.

## Ticket courant : ANH-71

[ANH-71](https://linear.app/anheart/issue/ANH-71/commiter-et-relire-tout-le-travail-en-cours-branche-featpi-training) : versionner et relire le travail existant de `feat/pi-training-session` avant les nouvelles fonctionnalites.

Etat initial : HEAD `73eb9baa4b1fdbee6b2ff39be1eb046849a653b1`, 72 fichiers suivis modifies et 294 fichiers non suivis. Ces modifications preexistantes ont ete preservees. `develop` a ete cree depuis `origin/main` (`a60e945e5cc61e70d01be4c17a750d65c06883b7`) et publie sur `origin` sans fusion ni modification de la production.

Preparation realisee :

- Exclure `.codex/config.toml`, qui contient une cle administrateur Convex, et le repertoire de donnees local `data/` des futurs commits.
- Conserver les exclusions des environnements Python, `.env.local`, `raspberry-pi/.env`, `simulation/out/` et du repertoire de build de la simulation hebergee.
- Installer Git LFS et gitleaks ; configurer le filtre LFS local et `.gitattributes` pour le fichier STEP dont depend la simulation. Le fichier source de 16 382 483 octets reste intact.
- Inventorier la file logicielle ci-dessous et versionner le logiciel en groupes compilables, verifies depuis des copies de l'index, sans changer le checkout partage.
- Corriger le tampon de `LiveSensorDisplay` : les effets de reinitialisation effacaient le premier lot et echouaient au lint. La synchronisation pendant le rendu conserve les lots sortis de la requete glissante, deduplique les timestamps, borne les echantillons et reinitialise une nouvelle seance avant de rendre ses graphiques.

Controles :

| Controle | Resultat |
| --- | --- |
| `git diff --check` et index de chaque commit | Verts |
| Gate Pi `bash raspberry-pi/scripts/check.sh` | 3187 tests passes, 100 % des branches couvertes, Ruff/format/basedpyright/mypy verts ; `/tmp/anheart-anh71-pi-gate.log` |
| Gate simulation `bash simulation/scripts/check.sh` | 984 tests passes, 1 echec attendu strict preexistant, 100 % des branches couvertes ; `/tmp/anheart-anh71-simulation-gate.log` |
| TypeScript `npx tsc --noEmit` | Aucun diagnostic sur les sources finales et les increments Convex/site isoles |
| ESLint `app components convex lib hooks i18n proxy.ts next.config.ts` | 0 erreur, 22 avertissements preexistants. Le lint racine `eslint .` reste mal borne aux environnements/builds Python : travail distinct ANH-72/ANH-125 |
| Build Next.js | Vert avec Turbopack sur le projet, puis Webpack dans les copies de l'index (dependances liees hors de la copie) ; 28 pages generees dans l'etat final ; `/tmp/anheart-anh71-index-site.log` |
| Increment unites/types | 275 tests passes, basedpyright/mypy/Ruff verts |
| Increment runtime/variateur | 1557 tests passes puis correction du groupe (ajout du fichier de limites oublie) et 53 tests motion repasses ; basedpyright/mypy/Ruff verts ; `/tmp/anheart-anh71-index-runtime-final.log` |
| Increment acquisition | 624 tests passes, types/Ruff verts ; `/tmp/anheart-anh71-index-sensors-final-checked.log` |
| Increment presence | 138 tests passes, types/Ruff verts ; `/tmp/anheart-anh71-index-presence-checked.log` |
| Increment integration Pi | 690 tests passes, basedpyright/mypy/Ruff/format verts ; `/tmp/anheart-anh71-index-panel.log` |
| Reproduction du tampon | Le premier lot echoue sur le code initial ; 7 cas passent apres correction (premier lot, chevauchement, historique glissant, changement de seance, seance vide, limites qui augmentent/diminuent) ; `/tmp/anheart-anh71-sensor-before.log`, `/tmp/anheart-anh71-sensor-after.log` |
| Navigateur Chrome, profil temporaire | Vraie courbe Recharts des le premier lot, boutons lot/seance/seance vide verifies, 0 erreur du fixture ; accueil Next charge, dashboard anonyme HTTP 307 ; `/tmp/anheart-anh71-browser-qa.log` |
| Simulation utilisee en CLI | `manual_27_rpm` PASS ; aide disponible ; nom inconnu refuse avec sortie 1 (traceback preexistant) ; `/tmp/anheart-anh71-quick-qa.log` |
| Simulation hebergee, sans deploiement | Build assemble uniquement dans une copie temporaire ; vrai serveur HTTP : catalogue 200 avec 60 scenarios, flux manuel de 1570 trames jusqu'a final/end, scenario inconnu error/end ; `/tmp/anheart-anh71-hosted-qa.log` |
| Docker Compose | Configuration normale et surcharge de developpement valides avec `--no-env-resolution` ; aucun conteneur ni equipement lance, fichier secret `.env` absent |
| Analyse gitleaks des fichiers candidats | Un exemple de documentation signale dans `raspberry-pi/README.md:435` ; la ligne d'en-tete utilise un placeholder. Le scanner sort 1 : le resultat n'est pas presente comme un passage automatique vert |
| Analyse gitleaks de l'historique | Deux en-tetes d'exemple signales dans les commits `8f0e7f25122d35020687a48fc666e76c5e8f1670` et `232717b006119bb852aca6383c83ad782b027b9f`. Le second est explicitement le test d'une cle invalide, avec reponse 401 attendue. Faux positifs relus, sans masquage du scanner ni reecriture de l'historique |
| Captures des guides | 66 images analysees localement par Apple Vision : aucun candidat aux formats de cle recherches ; domaines de demonstration seulement (dont une erreur OCR). Ceci n'est pas une certification d'absence de secret |
| Git LFS | Pointeur de 133 octets, objet SHA-256 `033b711170872481f195d82612d23e53b2c35cd61d05c71f88f87a18ff338bf0`, taille 16 382 483 octets ; `git lfs fsck` vert, hook pre-push installe |
| GitHub | Push de `develop` et du logiciel reussi, y compris l'objet LFS de 16 Mo. Apres reauthentification confirmee par l'utilisateur, `gh api user` avec `GH_CONFIG_DIR=/Users/elmdimegh/.config/gh` confirme `anheartpro-byte` et `viewerPermission=ADMIN`. PR brouillon ciblee vers `develop` |

Groupes realises (les adaptations de contrat sont versionnees avec leur producteur) :

1. `921ddcd` : exclusions de secrets/donnees, contrat Python et metadonnees LFS.
2. `b2c2b6b` : unites et types de seance/occupation.
3. `38fcae2` : moteur, runtime, supervision, limites et rendu des nouvelles commandes dans l'ancien contrat web.
4. `a0c9c2f` : acquisition BITalino, DSP, six capteurs, stubs, simulations de signaux et tests.
5. `bafb3f1` : presence, surveillance, adaptateurs et tests.
6. `f302ca4` : console locale, synchro cloud, interface web, packaging et tests d'integration.
7. `ac9b100` : schema/fonctions/routes Convex, API generee, traductions et gardes des anciennes pages pour le patient devenu facultatif.
8. `fb5f62c` : nouvelles pages/composants du tableau de bord et correction du tampon capteur.
9. `5c4eb47` : simulation, scenarios, cohorte, visualiseur, tests et source STEP dans Git LFS.
10. `f3efe9b` : assemblage de la simulation hebergee, valide en HTTP sans deploiement.
11. Documentation et guides, avec ce suivi.

Les tests d'integration qui importent la console sont dans son commit, et non dans les commits de bibliotheques capteur/presence. Les modifications preexistantes du checkout final ne sont pas annulees pour fabriquer ces etapes. Aucun changement d'etat Linear, fusion ou deploiement n'est realise par ce travail.

Limite historique importante : `develop`, copie du `main` existant, contient encore l'ancienne base `raspberry-pi/buffer.db` et des caches Python. Le checkpoint preexistant `b3c4caf` les retire de l'arbre de la branche ANH-71, mais les objets demeurent dans l'historique deja publie (base introduite par `a317603`). EX-D ne peut donc pas etre declare entierement satisfait. Aucune reecriture destructive n'est autorisee ni effectuee.

Branche de publication : `mohamdimagh1/anh-71-commiter-et-relire-tout-le-travail-en-cours-branche-featpi`, PR **brouillon** vers `develop`. ANH-71 reste ouvert tant que les deux revues independantes de la chaine de securite, les controles requis et la fusion ne sont pas attestes. Les controles locaux ne remplacent ni ces approbations ni la future CI (ANH-72). Les limites physiques et les defauts logiciels deja documentes, dont ANH-101 et ANH-121, restent ouverts ; aucune validation sur equipement ou de seuil medical n'est revendiquee. Le prochain ticket de la file est ANH-82, avec le correctif ANH-121 dans la meme etape de fondations.

### Correction de la preversion Git de la simulation

La [PR brouillon #3](https://github.com/anheartpro-byte/Anheart/pull/3) est ouverte.
Sur `bac8bd4`, le controle Vercel du site est vert, mais celui de la simulation
echoue : le projet FastAPI construit depuis la racine du depot sans point
d'entree a cet endroit. La connexion GitHub active `anheartpro-byte` est verifiee.

Le correctif ajoute `simulation_app:app` dans le `pyproject.toml` racine, un
point d'entree qui charge l'adaptateur heberge existant et son substitut BITalino,
un fichier racine de dependances renvoyant au fichier heberge, et Python 3.12.
Le visualiseur est servi depuis le paquet simulation, aussi bien dans le checkout
Git que dans l'assemblage autonome. Les reglages distants Vercel restent inchanges.

Verification locale du correctif : 10 tests de l'adaptateur passent ; Ruff,
format, basedpyright et mypy sont verts. Avant le correctif, les regressions
reproduisent le visualiseur HTTP 404 et l'import impossible du point d'entree.
Apres le correctif, deux vrais serveurs HTTP (checkout Git et assemblage temporaire)
servent exactement le fichier du visualiseur, le catalogue de 60 scenarios,
300 lignes de simulation jusqu'a `final/end`, et un scenario inconnu `error/end`.
Le detecteur du SDK Vercel resout `simulation_app:app`. Le site passe de nouveau
`tsc --noEmit` et son build Next.js (28 pages). Ces controles ne remplacent pas
le resultat de la prochaine preversion distante ni les deux approbations requises.

La preversion de `518f146` trouve maintenant le point d'entree, mais son
installation par defaut echoue : `uv lock` requiert la table `[project]`
dans le manifeste racine. Le correctif suivant declare cette table avec les
dependances hebergees existantes et `tool.uv.package = false` : l'application
reste executee depuis ses sources, sans installer un paquet racine editable.
Les installations pip manuelles restent possibles avec le fichier de dependances.

Le SDK Vercel, lance sans commande d'installation forcee dans une copie propre,
resout et installe le projet avec `uv`, puis produit la fonction et l'actif du
visualiseur (collecte CDN activee uniquement pour ce controle local). Les
11 tests passent maintenant, dont la comparaison des contraintes des deux
listes de dependances ; Ruff/format/basedpyright/mypy restent verts. Le paquet
natif construit sur macOS sert uniquement a la verification, pas a un deploiement
preconstruit. La preversion distante reste la preuve du build Linux.

### Revue, corrections et nettoyage autorisés du 3 octobre 2026

L'utilisateur a autorisé la gestion de deux reviewers, les corrections et la
fusion de la PR quand les preuves sont complètes. Il exige la CI avant fusion
et accepte deux agents indépendants sous un seul compte GitHub. Leurs avis
seront deux checks distincts liés au même SHA, pas deux personnes fictives.

La première revue a refusé la gestion de l'annulation, l'accès aux mesures live,
l'écriture de profils sur la boucle moteur et les détails privés dans les logs.
La seconde a reproduit l'annulation pendant l'armement et le démarrage manuel
indisponible après STOP, puis a atteint une limite d'usage avant son verdict.
Les corrections ajoutent des régressions pour ces six comportements. Aucun de
ces anciens résultats ne vaut une approbation du nouveau commit.

Le nettoyage explicitement autorisé a remplacé atomiquement, avec leases, les
cinq historiques de branches `main`, `develop`, `feat/dev`,
`feat/pi-training-session` et ANH-71. Seul `raspberry-pi/buffer.db` a été retiré
de leurs arbres historiques ; les 30 arbres ont été comparés, la CAO LFS est
intacte et une sauvegarde privée permet une récupération. Les dix builds Git
déclenchés par ce remplacement ont été annulés par le hold Vercel ; ses réglages
sont restaurés et les huit alias de production n'ont pas changé de déploiement.
Les anciennes références de PR, non modifiables par push, conservent encore
l'ancien historique côté GitHub : EX-D n'est pas déclaré terminé.

La CI préalable est décrite dans [framework-de-test.md](framework-de-test.md#15-ci).
Les mises à jour de sécurité restent sur les majeures Next.js 16 et Clerk 6.
L'audit Python résolu est vert. L'audit npm reste rouge sur l'unique advisory
`GHSA-vfj7-8cjw-p6xm` de `braces`, propagé en cinq findings dans les outils lint
Next.js ; aucun patch publié ni exception approuvée n'est disponible. La PR reste donc
brouillon et ANH-71 In Progress, sans fusion ni clôture annoncée.

### Reprise automatique de communication : corrections du 4 octobre 2026

La décision utilisateur est de tenter la reprise automatiquement avant de
demander une intervention manuelle. Les corrections conservent la reprise du
repos après une panne sans trame, distinguent le trafic possible de la preuve
d'adressage, et ne réinitialisent plus les échecs natifs sur une simple ouverture
du port. Les preuves survivent aux erreurs d'un wrapper et à l'annulation.

Un statut complet rétablit l'observation, jamais une séance ou un défaut. Un
épisode inconnu qui atteint le compte existant de la supervision devient
terminal : au plus un zéro d'urgence si l'adressage est prouvé, puis aucune
trame. L'interface ne présente pas une ancienne lecture récente comme fraîche
quand l'état courant est `comm_lost`. Le guide de la console décrit cette
distinction et la reprise manuelle après silence.

Les contrôles ciblés passent : 229 cas natifs/typage/FTDI, 357 cas runtime et
annulation incluant le contrat original de panne au repos de 20 secondes,
395 cas types/runtime/sérialisation/API et 323 cas HTTP panel/runtime/manuel.
Ces nombres correspondent à des suites qui se recouvrent ; ils ne s'additionnent
pas et ne remplacent pas les gates complètes à 100 % de branches. Les contrôles
statiques configurés sont verts. Les gates complètes sont maintenant vertes :
3 296 tests Pi, 986 tests simulation et un échec attendu strict préexistant,
avec 100 % des branches configurées couvertes dans les deux gates. Aucune
validation sur équipement réel ou de seuil médical n'est revendiquée.

L'envoi à GitHub Support pour les anciennes références a été reporté à la
demande de l'utilisateur. Aucun message n'a été envoyé et la purge historique
complète n'est pas revendiquée. L'audit reste obligatoire. La PR reste ouverte
et ANH-71 reste en cours jusqu'aux résultats requis et aux deux revues finales.

### Dépendance de lint : correctif ciblé et audit maintenu

L'advisory [GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm)
ne dispose toujours pas de version officielle corrigée de `braces`. Le
remplacement exact `npm:@dieub/braces-depth-guard@3.0.3-pn.2` est appliqué par
override à cette dépendance transitive, sans changer les majeures Next.js ou
Clerk. La chaîne réelle Next → fast-glob → micromatch est celle exercée par le
nouveau test CI ; le test ne dépend pas d'un emplacement hoisté.

La [publication et sa provenance](https://registry.npmjs.org/-/npm/v1/attestations/@dieub%2fbraces-depth-guard@3.0.3-pn.2)
correspondent au commit `a7c294b0535aec8b206bd36ba3f8dba2a7d989cb`. Les fichiers
publiés ont été comparés à cette source ; les signatures du registre et la
provenance ont été vérifiées. Les 764 fixtures amont inchangées passent sous
un harness synchrone contrôlé, sans invocation de Bash ; ce n'est pas une
exécution Mocha standard. Dix-huit cas supplémentaires vérifient les bornes,
les AST et la compatibilité de découverte de répertoires sur la plateforme
locale. Les normalisations de chemins Windows sont exercées, pas un système
de fichiers Windows réel.

Ce fork est récent, avec une journée d'historique de maintenance observée.
Il corrige la profondeur récursive visée par l'advisory ; il ne borne pas tous
les produits d'expansion ni tous les AST malformés ou options exécutables.
Le tag `latest` ne désigne pas ce correctif : la version exacte est verrouillée.
La suppression du finding par changement de nom ne serait pas une preuve ;
les douze régressions du guard font partie du job `audit` requis et échouaient
sur la version amont installée. `npm audit --audit-level=high` reste obligatoire,
sans exclusion des dépendances de développement ni exception d'advisory.

Dans une exportation propre du même arbre d'index, l'installation Node 24,
les douze guards, l'audit npm, TypeScript, le lint (zéro erreur, dix avertissements
préexistants), les 45 tests Convex, les 28 tests ECG, les dix tests de console et
le build Next.js de 28 pages passent. La CI distante et les deux avis finaux
liés au dernier commit restent nécessaires avant fusion.

## File logicielle

L'etape reprend l'ordre de la file Linear. Les parents ANH-122 et ANH-88 se cloturent apres leurs sous-tickets ; ils ne dupliquent pas leur implementation. Les dependances precises sont recontrolees avant chaque ticket. La presence dans cette liste ne signifie pas que ses bloqueurs sont resolus.

| Etape | Ticket | Livrable | Etat constate |
| --- | --- | --- | --- |
| 1 | [ANH-71](https://linear.app/anheart/issue/ANH-71/commiter-et-relire-tout-le-travail-en-cours-branche-featpi-training) | Commiter et relire tout le travail en cours (branche feat/pi-training-session) | Backlog |
| 2 | [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en) | Redéployer Convex avec le nouveau schéma sans casser le site en production | Backlog |
| 2 | [ANH-121](https://linear.app/anheart/issue/ANH-121/cle-machine-reversible-et-machine-supprimee-encore-authentifiee) | Clé machine réversible et machine supprimée encore authentifiée | Backlog |
| 3 | [ANH-72](https://linear.app/anheart/issue/ANH-72/integration-continue-gates-pi-simulation-et-site-a-chaque-pr) | Intégration continue : gates Pi, simulation et site à chaque PR | Backlog |
| 3 | [ANH-125](https://linear.app/anheart/issue/ANH-125/remettre-a-jour-les-readme-scripts-et-chiffres-perimes) | Remettre à jour les README, scripts et chiffres périmés | Backlog |
| 3 | [ANH-132](https://linear.app/anheart/issue/ANH-132/tests-convex-matrice-dautorisation-par-role-et-organisation-contrat) | Tests Convex : matrice d'autorisation par rôle et organisation, contrat des routes machine, dans la gate CI | Backlog |
| 4 | [ANH-133](https://linear.app/anheart/issue/ANH-133/contrat-http-pi-convex-versionne-version-logicielle-dans-le-heartbeat) | Contrat HTTP Pi ↔ Convex versionné, version logicielle dans le heartbeat, matrice de compatibilité | Backlog |
| 4 | [ANH-134](https://linear.app/anheart/issue/ANH-134/processus-de-release-versions-semantiques-pi-convex-site-changelog) | Processus de release : versions sémantiques Pi / Convex / site, changelog, check-list, version validée par machine | Backlog |
| 5 | [ANH-135](https://linear.app/anheart/issue/ANH-135/retirer-lancien-mode-enregistrement-ecg-srcmain-routes-sessiondata) | Retirer l'ancien mode « enregistrement ECG » (src.main, routes session/data, bouton Nouvelle session) | Backlog |
| 6 | [ANH-127](https://linear.app/anheart/issue/ANH-127/format-denregistrement-de-seance-v2-partage-par-le-pi-et-la-simulation) | Format d'enregistrement de séance v2, partagé par le Pi et la simulation | Backlog |
| 6 | [ANH-128](https://linear.app/anheart/issue/ANH-128/boite-noire-locale-la-console-ecrit-lenregistrement-de-seance-sur) | Boîte noire locale : la console écrit l'enregistrement de séance sur disque | Backlog |
| 6 | [ANH-129](https://linear.app/anheart/issue/ANH-129/synchronisation-cloud-par-relecture-du-journal-local-avec-reprise) | Synchronisation cloud par relecture du journal local, avec reprise après redémarrage | Backlog |
| 7 | [ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site) | Multi-organisation : séparer les clients dans Convex et le site | Backlog |
| 8 | [ANH-122](https://linear.app/anheart/issue/ANH-122/corriger-les-defauts-fonctionnels-du-site-releves-par-la-documentation) | Corriger les défauts fonctionnels du site relevés par la documentation | Backlog |
| 8 | [ANH-154](https://linear.app/anheart/issue/ANH-154/site-assigner-des-machines-a-un-gestionnaire-ajoute-au-lieu-de) | Site : assigner des machines à un gestionnaire ajoute au lieu de remplacer, et décocher retire | Backlog |
| 8 | [ANH-155](https://linear.app/anheart/issue/ANH-155/site-un-gestionnaire-qui-modifie-une-machine-nappelle-plus-une) | Site : un gestionnaire qui modifie une machine n'appelle plus une mutation réservée à l'admin | Backlog |
| 8 | [ANH-156](https://linear.app/anheart/issue/ANH-156/site-plus-aucune-erreur-silencieuse-chaque-mutation-affiche-succes-ou) | Site : plus aucune erreur silencieuse, chaque mutation affiche succès ou échec (codes d'erreur traduits) | Backlog |
| 8 | [ANH-157](https://linear.app/anheart/issue/ANH-157/site-invitations-par-clerk-organizations-liaison-dun-patient-pre-cree) | Site : invitations par Clerk Organizations, liaison d'un patient pré-créé à son compte, amorçage du premier admin | Backlog |
| 8 | [ANH-158](https://linear.app/anheart/issue/ANH-158/convex-listes-de-seances-filtrees-par-droits-et-organisation-avant-la) | Convex : listes de séances filtrées par droits et organisation avant la limite, avec pagination | Backlog |
| 8 | [ANH-159](https://linear.app/anheart/issue/ANH-159/site-compteurs-du-tableau-de-bord-utilisateurs-patients-machines-en) | Site : compteurs du tableau de bord (utilisateurs, patients, machines en ligne, séances actives) calculés correctement | Backlog |
| 8 | [ANH-160](https://linear.app/anheart/issue/ANH-160/site-en-direct-et-donnees-perimees-recalcules-a-lhorloge-pas-seulement) | Site : « En direct » et « Données périmées » recalculés à l'horloge, pas seulement au changement de donnée | Backlog |
| 9 | [ANH-83](https://linear.app/anheart/issue/ANH-83/tests-de-bout-en-bout-du-tableau-de-bord-navigateur-avec-une-console) | Tests de bout en bout du tableau de bord (navigateur) avec une console Pi simulée | Backlog |
| 10 | [ANH-88](https://linear.app/anheart/issue/ANH-88/securiser-lapi-machine-cles-limitation-de-debit-journal-daudit) | Sécuriser l'API machine : clés, limitation de débit, journal d'audit | Backlog |
| 10 | [ANH-165](https://linear.app/anheart/issue/ANH-165/convex-rotation-revocation-et-expiration-des-cles-machine-avec) | Convex : rotation, révocation et expiration des clés machine, avec chevauchement et affichage unique | Backlog |
| 10 | [ANH-166](https://linear.app/anheart/issue/ANH-166/convex-limitation-de-debit-par-machine-et-par-utilisateur-taille) | Convex : limitation de débit par machine et par utilisateur, taille maximale des charges (composant rate-limiter) | Backlog |
| 10 | [ANH-167](https://linear.app/anheart/issue/ANH-167/convex-journal-daudit-immuable-des-actions-sensibles-consultable-et) | Convex : journal d'audit immuable des actions sensibles, consultable et exportable | Backlog |
| 11 | [ANH-90](https://linear.app/anheart/issue/ANH-90/messages-derreur-serveur-en-francais-et-alertes-machine-hors-ligne) | Messages d'erreur serveur en français et alertes (machine hors ligne, défaut) | Backlog |
| 11 | [ANH-123](https://linear.app/anheart/issue/ANH-123/textes-restes-en-anglais-et-promesse-jusqua-3-g-sur-le-site) | Textes restés en anglais et promesse « jusqu'à 3 G » sur le site | Backlog |
| 12 | [ANH-137](https://linear.app/anheart/issue/ANH-137/catalogue-de-programmes-dans-convex-ecrit-par-lequipe-anheart-seule) | Catalogue de programmes dans Convex, écrit par l'équipe Anheart seule, avec validation identique au Pi et signature médicale | Backlog |
| 12 | [ANH-138](https://linear.app/anheart/issue/ANH-138/publication-des-programmes-vers-les-machines-le-pi-recupere-revalide) | Publication des programmes vers les machines : le Pi récupère, revalide et accepte ou refuse chaque programme | Backlog |
| 12 | [ANH-139](https://linear.app/anheart/issue/ANH-139/parametres-medicaux-signes-fichier-versionne-verification-au-demarrage) | Paramètres médicaux signés : fichier versionné, vérification au démarrage de la console, version visible partout | Backlog |
| 13 | [ANH-84](https://linear.app/anheart/issue/ANH-84/choix-du-passager-sur-la-console-locale-roster-convex) | Choix du passager sur la console locale (roster Convex) | Backlog |
| 13 | [ANH-85](https://linear.app/anheart/issue/ANH-85/lancement-a-distance-confirmation-physique-obligatoire-a-la-machine) | Lancement à distance : confirmation physique obligatoire à la machine | Backlog |
| 13 | [ANH-144](https://linear.app/anheart/issue/ANH-144/identite-et-droits-reverifies-a-larmement-passager-affiche-et-confirme) | Identité et droits revérifiés à l'armement : passager affiché et confirmé à la console, expiration des séances en attente, une seule séance par passager et par machine | Backlog |
| 13 | [ANH-152](https://linear.app/anheart/issue/ANH-152/console-locale-authentification-de-loperateur-connexion-nommee-par) | Console locale : authentification de l'opérateur (connexion nommée par code, liste d'habilitation synchronisée, expiration, journal) | Backlog |
| 14 | [ANH-142](https://linear.app/anheart/issue/ANH-142/controles-pre-vol-automatiques-avant-chaque-seance-variateur-bitalino) | Contrôles pré-vol automatiques avant chaque séance (variateur, BITalino, batterie, horloge, disque, version, paramètres médicaux) | Backlog |
| 14 | [ANH-143](https://linear.app/anheart/issue/ANH-143/aptitude-et-consentement-du-passager-questionnaire-de-contre) | Aptitude et consentement du passager : questionnaire de contre-indications, validité, blocage du lancement | Backlog |
| 14 | [ANH-145](https://linear.app/anheart/issue/ANH-145/registre-des-incidents-creation-automatique-sur-verdict-defaut-ou) | Registre des incidents : création automatique sur verdict, défaut ou arrêt d'urgence, revue, clôture, blocage de la machine | Backlog |
| 15 | [ANH-141](https://linear.app/anheart/issue/ANH-141/configuration-de-machine-centralisee-dans-convex-appliquee-au-repos) | Configuration de machine centralisée dans Convex, appliquée au repos par le Pi, double validation pour les clés de sûreté | Backlog |
| 15 | [ANH-147](https://linear.app/anheart/issue/ANH-147/registre-machine-identite-materielle-mesures-fiche-variateur-versions) | Registre machine : identité matérielle, mesures, fiche variateur, versions, état de validation M3 / M5 / M6 par machine | Backlog |
| 15 | [ANH-148](https://linear.app/anheart/issue/ANH-148/sante-de-la-machine-dans-le-heartbeat-temperature-disque-horloge) | Santé de la machine dans le heartbeat : température, disque, horloge, liaison, batterie BITalino, compteurs, et purge de l'historique | Backlog |
| 16 | [ANH-89](https://linear.app/anheart/issue/ANH-89/rapports-de-seance-dentrainement-pdf-et-historique-par-passager) | Rapports de séance d'entraînement (PDF) et historique par passager | Backlog |
| 16 | [ANH-130](https://linear.app/anheart/issue/ANH-130/depot-des-enregistrements-de-seance-dans-convex-storage-organise-par) | Dépôt des enregistrements de séance dans Convex Storage, organisé par organisation et machine | Backlog |
| 16 | [ANH-131](https://linear.app/anheart/issue/ANH-131/rejeu-dune-seance-reelle-dans-le-simulateur-et-bibliotheque-de-seances) | Rejeu d'une séance réelle dans le simulateur, et bibliothèque de séances réelles en CI | Backlog |
| 17 | [ANH-118](https://linear.app/anheart/issue/ANH-118/sauvegardes-plan-de-reprise-et-environnements-dev-preprod-prod) | Sauvegardes, plan de reprise et environnements (dev / préprod / prod) | Backlog |
| 17 | [ANH-126](https://linear.app/anheart/issue/ANH-126/cicd-deploiement-vercel-et-convex-automatise-rapports-du-framework-de) | CI/CD : déploiement Vercel et Convex automatisé, rapports du framework de test publiés | Backlog |
| 18 | [ANH-74](https://linear.app/anheart/issue/ANH-74/verrou-unique-sur-le-cable-variateur-console-bench-console) | Verrou unique sur le câble variateur (console ↔ bench_console) | Backlog |
| 18 | [ANH-124](https://linear.app/anheart/issue/ANH-124/defauts-daffichage-de-la-console-locale-releves-en-redigeant-le-guide) | Défauts d'affichage de la console locale relevés en rédigeant le guide | Backlog |
| 18 | [ANH-136](https://linear.app/anheart/issue/ANH-136/modele-de-menaces-ecrit-docsmenacesmd-revu-a-chaque-jalon) | Modèle de menaces écrit (docs/menaces.md), revu à chaque jalon | Backlog |
| 18 | [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite) | Dépendances, secrets, authentification forte et audit de sécurité externe avant le pilote | Backlog |
| 18 | [ANH-161](https://linear.app/anheart/issue/ANH-161/pi-image-reproductible-et-service-systemd-qui-lance-la-console-au) | Pi : image reproductible et service systemd qui lance la console au démarrage | Backlog |
| 19 | [ANH-101](https://linear.app/anheart/issue/ANH-101/residuels-de-securite-connus-cas-s07-et-chute-cardiaque-en-manuel) | Résiduels de sécurité connus : cas S07 et chute cardiaque en manuel occupé | Backlog |
| 19 | [ANH-146](https://linear.app/anheart/issue/ANH-146/retour-du-passager-apres-seance-echelle-de-nausee-et-de-malaise) | Retour du passager après séance : échelle de nausée et de malaise, ressenti, rattachés à la séance | Backlog |
| 19 | [ANH-173](https://linear.app/anheart/issue/ANH-173/donnees-de-sante-implementation-retention-et-purge-export-et) | Données de santé, implémentation : rétention et purge, export et suppression à la demande, consentement, suppression de compte | Backlog |
| 20 | [ANH-140](https://linear.app/anheart/issue/ANH-140/prescription-assigner-un-programme-publie-et-une-serie-de-seances-a-un) | Prescription : assigner un programme publié et une série de séances à un passager | Backlog |
| 20 | [ANH-149](https://linear.app/anheart/issue/ANH-149/carnet-de-bord-machine-interventions-calibrations-changements) | Carnet de bord machine : interventions, calibrations, changements variateur, mises à jour, incidents | Backlog |
| 20 | [ANH-150](https://linear.app/anheart/issue/ANH-150/vues-de-flotte-et-alertes-equipe-liste-filtrable-detail-machine) | Vues de flotte et alertes équipe : liste filtrable, détail machine, alertes par e-mail, suivi des erreurs sans données de santé (absorbe ANH-117) | Backlog |
| 21 | [ANH-168](https://linear.app/anheart/issue/ANH-168/ota-paquets-de-version-signes-par-la-ci-verification-de-signature-sur) | OTA : paquets de version signés par la CI, vérification de signature sur le Pi avant installation | Backlog |
| 21 | [ANH-169](https://linear.app/anheart/issue/ANH-169/ota-service-heberge-mender-ou-equivalent-inscription-des-machines) | OTA : service hébergé (Mender ou équivalent), inscription des machines, partitions A/B, déploiement par groupe | Backlog |
| 21 | [ANH-170](https://linear.app/anheart/issue/ANH-170/ota-jamais-pendant-une-seance-installation-seulement-machine-au-repos) | OTA : jamais pendant une séance, installation seulement machine au repos et saine, auto-test après redémarrage | Backlog |
| 22 | [ANH-119](https://linear.app/anheart/issue/ANH-119/documentation-client-manuel-utilisateur-guide-gestionnaire-et-support) | Documentation client : manuel utilisateur, guide gestionnaire et support | Backlog |
| 24 | [ANH-73](https://linear.app/anheart/issue/ANH-73/relire-et-maintenir-la-documentation-docs-pages-modules-framework-de) | Relire et maintenir la documentation docs/ (pages, modules, framework de test) | Backlog |

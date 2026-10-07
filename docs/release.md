# Release : versions, changelog, check-list

Une release fait passer `develop` dans `main` (la production). Ce document dit
comment chaque composant est versionné, ce que fait `scripts/release.sh`, ce que
le responsable de release doit prouver avant la fusion, et quelle version une
machine a le droit de recevoir.

> **État réel (6 octobre 2026).** Le script, le modèle de PR, le changelog et le
> registre Convex existent et sont testés (`npm run test:release`,
> `npm run test:convex`). **Aucune release n'a encore été faite** : aucun tag
> n'existe, `CHANGELOG.md` n'a aucune section, et les trois composants portent
> la valeur de développement `0.0.0-dev`. La première release réelle
> (`pi-0.1.0`, `cloud-0.1.0`, `web-0.1.0`) reste à faire par le responsable du
> produit en suivant la [section 3](#3-le-déroulé). Le registre Convex n'est
> déployé nulle part. La règle « une machine ne reçoit qu'une version validée
> pour son état » est écrite et testée, mais rien ne l'applique encore : voir
> la [section 2](#2-le-niveau-de-validation-dune-version-du-pi).

Sommaire :

1. [Les trois composants et leur version](#1-les-trois-composants-et-leur-version)
2. [Le niveau de validation d'une version du Pi](#2-le-niveau-de-validation-dune-version-du-pi)
3. [Le déroulé](#3-le-déroulé)
4. [Ce que fait le script](#4-ce-que-fait-le-script)
5. [La check-list de release](#5-la-check-list-de-release)
6. [Enregistrer la version dans Convex](#6-enregistrer-la-version-dans-convex)
7. [Tests](#7-tests)
8. [Limites et reste à faire](#8-limites-et-reste-à-faire)

---

## 1. Les trois composants et leur version

Chaque composant a sa propre version sémantique `X.Y.Z` et son propre tag git.
Les trois avancent séparément : une release peut ne publier qu'un composant.

| Composant | Tag | Écrit par le script dans | Lu par |
|---|---|---|---|
| Raspberry Pi | `pi-X.Y.Z` | `raspberry-pi/VERSION` | la console, qui le lit une fois au démarrage (`src/contract.py`) et l'envoie dans chaque heartbeat (`software_version`) ; la fiche machine du site l'affiche. L'affichage sur la console locale n'a pas encore de ticket ([section 8](#8-limites-et-reste-à-faire)). |
| Convex | `cloud-X.Y.Z` | `convex/VERSION` et la constante `CLOUD_VERSION` de `convex/cloudVersion.ts` | le code déployé : `npx convex run softwareReleases:deployedCloudVersion` répond la version du déploiement visé. |
| Site | `web-X.Y.Z` | `package.json` et `package-lock.json` (champ `version`) | `next.config.ts`, qui la fige à la construction ; le pied de page de l'accueil et de la FAQ l'affiche. |

Les fichiers `VERSION` contiennent le tag entier (`pi-0.1.0`). `package.json`
contient la version sans préfixe (`0.1.0`), parce que npm l'exige ; le site
ajoute `web-`.

Tant qu'un composant n'a jamais été publié, sa version vaut `0.0.0-dev`.

Quel chiffre augmenter :

| Chiffre | Quand |
|---|---|
| `Z` (correctif) | une correction qui ne change ni l'usage ni le contrat entre le Pi et Convex |
| `Y` (mineure) | un comportement nouveau ou modifié, compatible avec les autres composants déjà publiés |
| `X` (majeure) | une rupture : la majeure du contrat Pi et Convex change (ANH-133), ou une donnée doit être migrée |

Les tags sont **annotés** et posés sur `main`, jamais sur `develop`. Le message
d'un tag est la section du changelog de sa version.

---

## 2. Le niveau de validation d'une version du Pi

Seul le Pi fait bouger la machine : seule une version du Pi porte un niveau.

| Niveau | Ce que la version a le droit de faire | Ce qui l'accorde |
|---|---|---|
| `bench` | tourner au banc, capsule vide (jalon M3) | la check-list de la [section 5](#5-la-check-list-de-release) |
| `auto_validated` | piloter des séances programmées (jalon M5) | la revue go/no-go M5 (ANH-102), compte rendu joint |
| `occupied_validated` | tourner avec une personne à bord (jalon M6) | la revue M6 (ANH-107), compte rendu joint |

Le niveau est écrit à deux endroits : dans la section de la version dans
[`CHANGELOG.md`](../CHANGELOG.md) (ligne « Niveau de validation ») et dans la
table Convex `software_releases` ([section 6](#6-enregistrer-la-version-dans-convex)).

Une version naît le plus souvent `bench`. Quand une revue relève son niveau, ou
le lui retire, on ne republie rien : une PR ajoute une ligne datée sous la ligne
« Niveau de validation » de sa section, en citant la revue, et un admin
enregistre la nouvelle valeur dans Convex avec le motif.

### La règle : quelle machine reçoit quelle version

L'état de validation d'une machine sera tenu par le registre machine (ANH-147).

| État de validation de la machine | Version du Pi acceptée |
|---|---|
| aucun (`none`) | toute version |
| banc (`bench`, M3) | `bench`, `auto_validated` ou `occupied_validated` |
| séances programmées (`auto`, M5) | `auto_validated` ou `occupied_validated` |
| personne à bord (`occupied`, M6) | `occupied_validated` seulement |

Une version qui n'est pas enregistrée n'a aucun niveau : seule une machine sans
état de validation l'accepte.

**Ce qui applique la règle.** Aujourd'hui, rien d'automatique. La règle est
codée une seule fois, dans `convex/lib/releaseValidation.ts`
(`releaseAllowedOnMachine`), et chaque case du tableau est un test de
`convex/softwareReleases.test.ts`. Deux tickets l'appelleront :

- le registre machine (ANH-147) affichera « non validée pour cette machine »
  quand la version annoncée par le heartbeat ne satisfait pas l'état de la
  machine ;
- la mise à jour à distance (ANH-116, ANH-168) refusera de proposer et
  d'installer une telle version.

D'ici là, une mise à jour se fait à la main sur la machine
([deploiement.md, section 7.6](deploiement.md#76-arrêter-mettre-à-jour)) : la
personne qui installe vérifie elle-même le niveau de la version dans
`CHANGELOG.md` avant d'agir. Ce contrôle manuel est une limite connue.

---

## 3. Le déroulé

Il faut : `gh` connecté au dépôt, `node`, un clone à jour sans modification en
cours. Chaque commande accepte `--dry-run` : elle lit tout, affiche exactement
ce qu'elle ferait, et n'écrit, ne pousse et ne crée rien (elle rafraîchit
seulement, par `git fetch`, ce que le clone sait de `origin`).

1. **Choisir** les versions à publier et, pour le Pi, le niveau de validation.
2. **Préparer.** Regarder d'abord la simulation, puis lancer pour de bon :
   ```bash
   scripts/release.sh prepare --pi 0.1.0 --cloud 0.1.0 --web 0.1.0 --pi-validation bench --dry-run
   scripts/release.sh prepare --pi 0.1.0 --cloud 0.1.0 --web 0.1.0 --pi-validation bench
   ```
   Le script refuse si `develop` n'est pas vert. Il crée la branche
   `release/pi-0.1.0_cloud-0.1.0_web-0.1.0`, y écrit les fichiers de version et
   les sections de `CHANGELOG.md`, et ouvre une PR vers `develop` intitulée
   `Release : pi-0.1.0, cloud-0.1.0, web-0.1.0`.
3. **Faire relire et fusionner la PR de préparation** comme toute PR (CI verte,
   revue, squash). Garder le titre `Release : …` : c'est ce qui la tient hors
   du changelog suivant.
4. **Ouvrir la PR de release** quand `develop` est de nouveau vert :
   ```bash
   scripts/release.sh pr
   ```
   Le script ouvre la PR `develop` vers `main` avec le modèle
   [`.github/PULL_REQUEST_TEMPLATE/release.md`](../.github/PULL_REQUEST_TEMPLATE/release.md),
   où il a rempli les versions, le candidat et le changelog.
5. **Remplir la check-list** de la PR ([section 5](#5-la-check-list-de-release)),
   preuve après preuve. Deux approbations.
6. **Fusionner par commit de fusion**, jamais en squash ni en rebase : un
   squash couperait `main` de l'historique de `develop`, et le changelog suivant
   reprendrait tout depuis le début.
7. **Poser les tags** dès que la CI de `main` est verte :
   ```bash
   scripts/release.sh tag
   ```
8. **Enregistrer les versions dans Convex** avec les lignes que le script vient
   d'afficher ([section 6](#6-enregistrer-la-version-dans-convex)).
9. **Déployer, dans la fenêtre fixée par la check-list.** La fusion dans
   `main` n'a rien déployé : ni un push ni ce script ne déploient. Dans une
   même fenêtre, **à un moment où aucune séance n'est en cours** : Convex
   d'abord, à la main
   ([deploiement.md, section 3.3](deploiement.md#33-vers-la-production)), puis
   **aussitôt** le site, par le bouton « Déployer en production (main) » de
   l'onglet Actions
   ([section 5.1](deploiement.md#51-comment-il-se-déploie)). La simulation
   hébergée part du même bouton si elle a changé. Le bouton vient après les
   tags de l'étape 7 : lancé avant, il peut les faire refuser
   ([section 4](#les-boutons-de-déploiement-et-le-script)). Un bouton pour
   Convex reste prévu par ANH-126.

Si une étape échoue, rien n'est à défaire à la main sauf à l'étape 2 : quand la
branche `release/…` a été créée mais pas poussée, la supprimer avant de
relancer.

### Si `develop` a bougé pendant la release

Les sections du changelog sont calculées à l'étape 2, sur `develop` tel qu'il
est à cet instant. Or la PR de release part de la branche `develop` : toute PR
de ticket fusionnée ensuite, jusqu'à la fusion dans `main`, est livrée avec la
version. **Le plus simple est de ne rien fusionner d'autre dans `develop` entre
l'étape 2 et l'étape 6.**

Si une PR de ticket est quand même fusionnée, rien n'est perdu : `pr` et `tag`
refusent de continuer tant que le commit à publier contient une PR de ticket
d'un composant que la section de sa version ne cite pas, et ils nomment les
titres manquants. Dans ce cas :

1. **Relancer `prepare` avec les mêmes versions.** Le script voit que ces
   versions sont déjà écrites dans `develop` et attendent leur tag : il ne
   touche pas aux fichiers de version, il réécrit seulement les sections
   concernées de `CHANGELOG.md` (elles gardent leur date) et ouvre une PR vers
   `develop` intitulée `Release : … (changelog complété)`. S'il n'y a rien à
   compléter, il le dit et ne fait rien.
2. **Fusionner cette PR** dans `develop` (squash, titre gardé).
3. **Reprendre où le refus est arrivé :**
   - le refus venait de `pr` : relancer `scripts/release.sh pr` ;
   - une PR de release était déjà ouverte : son corps cite l'ancien candidat et
     l'ancien changelog. La fermer, relancer `scripts/release.sh pr`, et refaire
     la check-list sur le nouveau candidat (les preuves valaient pour l'ancien
     SHA) ;
   - le refus venait de `tag` (la PR de release était déjà fusionnée) : `main`
     porte le code mais pas la ligne. Relancer `scripts/release.sh pr`, fusionner
     cette seconde PR par commit de fusion, puis `scripts/release.sh tag`. Tant
     que `main` n'a pas reçu le changelog complété, `tag` refuse.

Avant la fusion de la PR de préparation, le cas est plus simple : fermer cette
PR, supprimer sa branche `release/…` et relancer `prepare`.

---

## 4. Ce que fait le script

| Étape | Lit | Écrit | Refuse si |
|---|---|---|---|
| `prepare` | `origin/develop` et sa CI | une branche `release/…` (fichiers de version, `CHANGELOG.md`), une PR vers `develop` | `develop` n'est pas vert ; une version n'est pas `X.Y.Z` ou n'est pas supérieure à la version courante ; son tag existe déjà ; la version courante n'a pas son tag ; `--pi` sans `--pi-validation` ; l'arbre de travail a des modifications |
| `prepare`, relancé avec des versions déjà écrites dans `develop` et sans tag | `origin/develop` et sa CI | une branche `release/…-changelog-<sha>` (`CHANGELOG.md` seul), une PR vers `develop` | `develop` n'est pas vert ; les sections citent déjà chaque PR de ticket (rien à changer) |
| `pr` | `origin/develop` et sa CI | la PR `develop` vers `main` | `develop` n'est pas vert ; aucune version de `develop` n'attend son tag ; une version n'a pas sa section dans `CHANGELOG.md` ; **`develop` contient une PR de ticket d'un composant que la section de sa version ne cite pas** |
| `tag` | `origin/main` et sa CI | les tags annotés, poussés | `main` n'est pas vert ; aucune version de `main` n'attend son tag ; une version n'a pas sa section dans `CHANGELOG.md` ; `main` ne contient pas le dernier commit de `develop` qui a écrit `CHANGELOG.md` (PR de release fusionnée en squash, ou changelog complété pas encore dans `main`) ; **`main` contient une PR de ticket d'un composant que la section de sa version ne cite pas** |

Les deux refus en gras tiennent la règle suivante : **aucun tag n'est posé sur
un commit qui contient une PR de ticket d'un composant absente de la section de
sa version.** Le script relit pour cela les titres de `develop` depuis le tag
précédent du composant jusqu'au commit qu'il s'apprête à publier (pour `tag` :
le commit de `develop` le plus récent que `main` contient), et les compare aux
lignes de la section. Quoi faire quand il refuse est dit à la
[section 3](#si-develop-a-bougé-pendant-la-release).

« Vert » veut dire deux choses à la fois :

- aucun *check run* du commit n'a échoué, n'a été annulé ou n'est encore en
  cours ;
- chacune des six gates de la CI (`pi-gate`, `simulation-gate`, `convex-tests`,
  `web`, `audit`, `docs`) a, sur ce commit, au moins une exécution terminée et
  réussie.

Un commit sur lequel la CI n'a pas tourné n'est donc pas vert, même si un check
run tiers y a réussi (les commentaires d'aperçu de Vercel, par exemple,
répondent avant que les gates ne démarrent).

Une gate ignorée (`skipped`) ne compte ni pour ni contre. La CI n'ignore une
gate que sur une PR dont les fichiers changés ne peuvent pas la concerner ; sur
un push vers `develop` ou `main`, toutes tournent
([framework-de-test.md](framework-de-test.md)). Le script lit le commit de tête
de ces deux branches, où le push a donc tout lancé. Deux cas en découlent :

- la PR de release est ouverte sur ce même commit et son exécution a ignoré une
  gate : l'exécution du push, réussie, suffit, et le commit est vert ;
- une gate n'a sur le commit que des exécutions ignorées : le commit n'est pas
  vert, et le script la nomme parmi les gates absentes ou non réussies.

Le script ne lit que les check runs. Il **ne lit pas les statuts de commit**.
Les déploiements que Vercel lançait à chaque push étaient des statuts ; Vercel
n'en lance plus
([deploiement.md, section 5.1](deploiement.md#51-comment-il-se-déploie)).

La liste des gates est écrite en tête du script (`REQUIRED_CHECKS`). Ce sont
les six noms que la protection de branche exige et que
`scripts/ci/ci-workflow.test.mjs` tient ; `npm run test:release` échoue si la
liste du script s'en écarte. Si un job de `.github/workflows/ci.yml` est
renommé, cette liste doit suivre dans la même PR : sinon le script refuse toute
release, ce qui est le sens voulu de la panne.

`pr` et `tag` ne prennent aucune version en argument : ils lisent celles des
fichiers de la branche, et publient celles qui n'ont pas encore de tag.

### Les boutons de déploiement et le script

Les deux boutons de déploiement
([deploiement.md, section 5.1](deploiement.md#51-comment-il-se-déploie)) sont
des workflows GitHub Actions : **leurs jobs sont des check runs, posés sur le
commit de tête de la branche déployée, et le script les lit comme les autres.**
Le bouton de préversion les pose sur la tête de `develop`, que lisent `prepare`
et `pr` ; le bouton de production sur la tête de `main`, que lit `tag`. Sur le
commit que le script s'apprête à lire :

| Exécution d'un bouton | Effet sur le script |
|---|---|
| réussie | aucun |
| un job ignoré (`skipped` : le projet qui n'a pas été choisi) | aucun |
| en cours, ou en attente d'approbation | refus : attendre la fin, ou approuver ou refuser l'exécution |
| en échec, refusée à l'approbation ou annulée | refus, tant que cette exécution reste attachée au commit |

Un bouton en échec ne dit rien du code : un secret manquant, une confirmation
mal saisie ou un quota Vercel épuisé suffisent. Pour lever le refus :

- relancer les jobs en échec de **cette même** exécution (« Re-run failed
  jobs ») jusqu'au succès : seule la dernière tentative compte alors ;
- ou, si l'échec tient à ce qui a été saisi (la confirmation), supprimer
  l'exécution (« Delete workflow run »), ce qui retire ses vérifications du
  commit.

Lancer une **nouvelle** exécution, même réussie, ne suffit pas : celle qui a
échoué reste sur le commit. Ces deux gestes n'ont pas été essayés sur ce dépôt.

D'où l'ordre du [déroulé](#3-le-déroulé) : le bouton de production vient
**après** `scripts/release.sh tag`. Lancé avant, sur la tête de `main`, il fait
attendre `tag`, ou le fait refuser s'il échoue. De même, entre `prepare` et la
fusion de la release, ne pas lancer le bouton de préversion sur la tête de
`develop`, ou attendre qu'il ait réussi.

### Comment le changelog est construit

Le script lit les titres des commits de `develop` (premier parent) depuis le tag
précédent du composant (son tag le plus récent), ou depuis le début pour une
première version. Il ne garde que les titres de la forme `ANH-123 : …`, donc
les fusions squash des PR de ticket. `prepare`, `pr` et `tag` font ce même
calcul : c'est ce qui permet aux deux derniers de voir qu'une section est en
retard.

Une PR est rangée sous chaque composant dont elle modifie un fichier :

| Composant | Chemins |
|---|---|
| `pi` | `raspberry-pi/` |
| `cloud` | `convex/` |
| `web` | `app/`, `components/`, `hooks/`, `i18n/`, `lib/`, `messages/`, `public/`, et à la racine `proxy.ts`, `next.config.ts`, `package.json`, `package-lock.json`, `postcss.config.mjs`, `components.json`, `tsconfig.json` |

Une PR qui ne touche que la documentation, la simulation, les scripts ou la CI
n'apparaît dans aucune section : elle ne change aucun composant livré.

---

## 5. La check-list de release

C'est celle du modèle de PR, mot pour mot (un test le vérifie). Chaque case
cochée porte sa preuve sur la même ligne. Une case sans preuve reste ouverte, et
la PR n'est pas fusionnée tant qu'une case est ouverte.

- [ ] **Gates vertes.** Toutes les vérifications CI du candidat sont terminées
  et réussies : gate Pi, gate simulation, tests Convex, site, audit, docs.
- [ ] **Docs à jour.** Chaque changement de comportement du changelog ci-dessous
  a sa page de `docs/` à jour.
- [ ] **Menaces revues.** `docs/menaces.md` est relu contre le candidat, et la
  check-list `docs/release-threat-review.md` est remplie et jointe.
- [ ] **Matrice de compatibilité à jour.** La section « Versions et
  compatibilité » de `docs/convex.md` et de `docs/raspberry-pi.md` couvre les
  versions de cette release.
- [ ] **Rapport de simulation joint.** `python -m simulation.quick --all --dsp`
  a tourné sur le candidat, sans échec ; `report.html` et `report.json` sont
  joints.
- [ ] **Rejeu des séances réelles vert.** Chaque fichier de
  `simulation/scenarios/real/` est rejoué sur le candidat sans écart
  inexpliqué ; si le dossier est vide, c'est écrit ici.
- [ ] **Aucune valeur `[MED]` modifiée sans décision.** Le diff depuis la
  version précédente est relu ; toute valeur marquée `[MED]` qui change renvoie
  à sa décision médicale signée, jointe.
- [ ] **Aucun verrou modifié par défaut.** `PROGRAMS_ENABLED` et
  `OCCUPANCY_OCCUPIED_ENABLED` valent toujours `false` par défaut dans le code
  et dans les fichiers d'exemple.
- [ ] **Niveau de validation du Pi justifié.** Le niveau annoncé ci-dessus est
  celui que les revues enregistrées permettent ; au-dessus de `bench`, le
  compte rendu de la revue M5 ou M6 est joint.
- [ ] **Fenêtre de déploiement fixée.** Fusionner cette PR ne déploie rien. La
  date et l'heure du déploiement de Convex, puis du site par le bouton
  « Déployer en production (main) », sont écrites ici : une même fenêtre, à un
  moment où aucune séance n'est en cours, avec le nom de la personne qui
  approuve.

### Comment prouver chaque ligne

`<base>` est le tag précédent du Pi (par exemple `pi-0.1.0`), ou `origin/main`
pour une première release.

| Ligne | Preuve |
|---|---|
| Gates vertes | le lien du run CI du candidat. Le script l'a déjà vérifié, la case le rend lisible. |
| Docs à jour | pour chaque ligne du changelog, la page de `docs/` modifiée par sa PR, ou « aucun changement de comportement ». |
| Menaces revues | la [check-list de revue des menaces](release-threat-review.md) remplie : reviewer indépendant, date, SHA, verdict. |
| Matrice de compatibilité | la section « Versions et compatibilité » (créée par ANH-133) cite les versions publiées. |
| Rapport de simulation | la commande et ses options sont décrites dans [framework-de-test.md](framework-de-test.md#6-verdicts-instantanés--simulationquick). La CI nocturne produit le même rapport. |
| Rejeu des séances réelles | `python -m simulation.run <fichier> --out <dossier>` pour chaque fichier de `simulation/scenarios/real/`. |
| Valeurs `[MED]` | `git grep -l '\[MED\]' origin/develop -- raspberry-pi/src convex` liste les fichiers concernés ; `git diff <base>..origin/develop -- <ces fichiers>` montre ce qui a changé. |
| Verrous | `git diff <base>..origin/develop -- raspberry-pi/src/local_config.py raspberry-pi/.env.example raspberry-pi/.env.pi.example` ne change aucun des deux défauts, et les deux fichiers d'exemple portent `=false`. |
| Niveau de validation | `bench` : cette check-list. Au-dessus : le compte rendu de revue. |
| Fenêtre de déploiement | la date, l'heure, et le nom de la personne qui lance et de celle qui approuve. Qu'aucune séance ne soit en cours se vérifie au moment de déployer, pas ici ([étape 9 du déroulé](#3-le-déroulé)). |

Cette check-list ne vaut pas autorisation de personne à bord : cette décision
appartient à la revue M6.

---

## 6. Enregistrer la version dans Convex

Table `software_releases` ([convex.md, section 2](convex.md#2-le-schéma)) :
`component`, `version` (le tag), `validationLevel` (Pi seulement), `releasedAt`,
`notes`, plus `recordedBy` et `updatedAt` pour savoir qui a écrit quoi.

La mutation `softwareReleases.recordRelease` est réservée au rôle `admin`, et
l'autorisation est vérifiée côté serveur. Elle refuse :

- une version qui n'est pas `<composant>-X.Y.Z` ;
- une version du Pi sans niveau, ou un niveau sur une version `cloud` ou `web` ;
- une date invalide, des notes de plus de 2000 caractères ;
- un changement de niveau d'une version déjà enregistrée sans `notes` (le motif).

Enregistrer deux fois la même version ne crée qu'une ligne.
`softwareReleases.listReleases` (admin) relit le registre, la plus récente
d'abord.

`scripts/release.sh tag` affiche, pour chaque version, l'argument à passer, par
exemple :

```json
{"component":"pi","version":"pi-0.1.0","validationLevel":"bench","releasedAt":1791331200000}
```

Aucune page du site n'appelle encore cette mutation, et elle n'a été appelée sur
aucun déploiement : elle n'est prouvée que par les tests en mémoire. À la
première release, l'essayer d'abord sur le déploiement de développement. La
ligne de commande de Convex sait appeler une fonction au nom d'un compte ; cette
forme n'a pas été essayée ici :

```bash
npx convex run softwareReleases:recordRelease '<argument>' --identity '{"subject":"<clerkId du compte admin>"}'
```

---

## 7. Tests

```bash
npm run test:release    # le script, à blanc et pour de bon, dans des dépôts jetables
npm run test:convex     # le registre, sa matrice d'autorisation, la règle, la constante
```

`scripts/release.test.mjs` construit pour chaque test un dépôt git jetable avec
son `origin`, et remplace `gh` par un double : aucun test ne touche GitHub ni le
dépôt réel. Il vérifie aussi les fichiers de version du dépôt, le pied de page
du site, et que la check-list du modèle de PR est celle de ce document.

---

## 8. Limites et reste à faire

| Quoi | Qui ou quel ticket |
|---|---|
| Faire la première release réelle (`pi-0.1.0`, `cloud-0.1.0`, `web-0.1.0`) | le responsable du produit, [section 3](#3-le-déroulé) |
| Afficher la version sur la console locale | ticket à créer ; `read_software_version` (`src/contract.py`) lit déjà `raspberry-pi/VERSION` côté Pi |
| Appliquer la règle de la [section 2](#2-le-niveau-de-validation-dune-version-du-pi) | ANH-147 (registre machine), ANH-116 et ANH-168 (mise à jour à distance) |
| Lancer `npm run test:release` en CI | une ligne à ajouter au workflow |
| Tenir `REQUIRED_CHECKS` égal aux jobs de la CI | toute PR qui renomme un job de `ci.yml` (ANH-184 a gardé les six noms) |
| Versionner une correction urgente partie de `main` (`hotfix/…`) | non outillé : `prepare` ne part que de `develop` ; à décider au premier cas |
| Déployer Convex par un bouton, comme le site et la simulation (ANH-198) | ANH-126 |
| Appeler `softwareReleases.recordRelease` depuis le site | avec la fiche machine d'ANH-147 |

[Sommaire](README.md) · [Déploiement](deploiement.md) · [Menaces](menaces.md) · [Roadmap](roadmap.md)

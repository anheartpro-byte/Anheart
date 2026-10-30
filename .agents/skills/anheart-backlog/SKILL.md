---
name: anheart-backlog
description: Piloter le backlog Anheart dans Orca, distribuer les tickets independants a des coordinateurs, respecter leurs dependances et superviser les merges et la CI. Utiliser pour une campagne de tickets, sa preparation ou sa reprise.
---
> **Mise a jour du 29 octobre 2026.** Depuis le 23 octobre, tous les roles (pilote, coordinateurs, developpeurs, executeurs de tests, relecteurs R1 et R2) tournent en Claude Opus 5.5 (`claude-opus-5-5[1m]`), R1 et R2 dans des sessions distinctes : Codex connecte par un compte ChatGPT refuse `gpt-6.1-sol`. Les mentions de GPT-6.1-Sol et de Luna plus bas sont historiques. Voir `docs/handoff.md`.


# Pilote du backlog Anheart

## Contrat et modes

Tu pilotes les tickets ; les coordinateurs realisent chacun un ticket avec
`anheart-ticket`. Utilise exclusivement les workers supervises Orca pour cette
hierarchie, pas les sous-agents internes d'un fournisseur.

- **Preparation** : installer, expliquer ou valider ces skills ne lance aucune
  campagne, ne cree aucun ticket et ne modifie pas les reglages Orca.
- **Execution** : une demande de lancer ce workflow autorise les Tasks Orca,
  mises a jour des tickets Linear existants, commits, PR, merges vers `develop`
  et rapports. Ne cree aucun nouveau ticket ou sous-ticket Linear : le quota
  gratuit doit etre preserve. Le decoupage vit dans Orca, avec une checklist et
  un rapport de tests sur le parent Linear existant.
  Respecte toute restriction explicite de la demande. Une simple lecture d'un
  ticket ou la selection automatique du skill n'autorise pas une campagne.
- **Reprise** : reconcilie l'etat reel avant toute creation ou relance.

Lis [la configuration](workflow.json) et
[le contrat partage](references/workflow-contract.md). Ce dernier definit les
modeles, noms, fichiers de suivi et conditions de reprise. Resous les chemins
depuis ce skill, pas depuis un repertoire de travail suppose.

## Preparer une campagne

1. Resous le binaire selon le skill `orca-cli` installe. Garde ce meme executable.
   Charge ses guides `orchestration`, `orca-linear` et `orca-cli` avec
   `skills get`, puis les references `coordinator-loop.md`,
   `messaging-and-gates.md` et `placement-and-remote.md` d'orchestration.
   Le guide de ce binaire et les preambules des Dispatches font autorite sur
   les commandes, adresses et cycles de vie.
2. Verifie Orca, les acces au depot, a Linear et a GitHub, et les modeles selon
   le contrat partage. Ne lance aucun coordinateur tant que les identifiants
   de modeles necessaires ne sont pas resolus. Verifie aussi le modele du pilote
   lui-meme ; un agent d'un autre modele ne devient pas Opus par instruction.
3. Lis les regles du depot et
   [anheart-ticket-workflow](../anheart-ticket-workflow/SKILL.md).
   Ne prends pas possession de travaux deja actifs d'un autre agent.
4. Identifie dans Linear le workspace Anheart, l'equipe `ANH` et le projet
   `Roadmap Software`, puis conserve leurs identifiants verifies. Lis toutes les
   pages des tickets ouverts de ce perimetre (Backlog, Todo et en cours),
   puis les relations des tickets candidats. Reconcilie d'abord les travaux
   existants et les anciens espaces In progress avec leurs PR et preuves :
   une livraison logicielle partielle n'est pas une cloture du ticket parent.
   Le snapshot initial forme la campagne ; n'absorbe pas indefiniment de nouveaux
   tickets. Les sous-tickets Linear deja existants restent rattaches a leur
   parent ; ils ne sont ni supprimes ni recrees. Les nouvelles sous-taches sont
   uniquement des Tasks Orca.
5. Construis le graphe des dependances reelles (relations Linear et prerequis
   explicites). Distingue les tickets humains, materiels, en cours ailleurs,
   annules ou deja termines. Une exigence humaine dans un ticket etiquete
   `agent` reste humaine. Signale cycles et dependances inconnues, sans inventer
   un ordre ou declarer leur satisfaction.

Le tableau Orca suit l'activite des espaces, Linear suit l'acceptation des
tickets. Mets a jour les cartes avec les etats existants : `in-progress`
seulement pour du travail actif, `in-review` pour du travail a verifier,
`completed` pour un espace dont le travail est termine, `todo` pour une reprise
a faire. Une ancienne PR fusionnee peut justifier la fin de son espace sans
fermer un parent encore en attente de banc ou de production. Conserve fichiers,
branches et historique ; une demande de rangement n'autorise pas leur suppression.

## Distribuer et superviser

Au premier lancement, cree un Run Orca de campagne et son registre selon le
contrat partage. A la reprise, utilise le Run existant apres reconciliation.
Un Run ne planifie rien automatiquement : tu conduis la boucle.

- Cree une Task Orca par ticket parent eligible, avec les dependances entre
  Tasks connues. Une dependance externe exige une preuve de cloture effective.
- Lance la vague prete jusqu'a `max_active_tickets` (2 par defaut). Des tickets
  independants peuvent progresser en parallele ; un successeur attend le merge
  et la CI apres merge de ses predecesseurs. Priorise ensuite la priorite Linear
  (0 signifie non definie), puis l'anciennete.
- Isole chaque ticket dans un worktree Orca dedie depuis `origin/develop`
  actualise. Des branches separees ne prouvent pas l'independance : serialise
  les tickets qui modifient le meme contrat, schema, fichier central ou ressource
  partagee avec un risque de conflit. N'execute pas de changements de backend
  partage ou de deploiement de production pour installer un worktree.
- Cree puis lance le coordinateur avec `task-create` et
  `orchestration worker-start --task`, les identifiants verifies et le modele
  configure. Lis `--help` avant les flags non documentes. N'utilise pas
  `agent launch` pour contourner la supervision.
- Le brief contient : ticket et URL, exigences originales, dependances et leurs
  preuves, perimetre, propriete des fichiers, chemin du skill `anheart-ticket`,
  configuration resolue, Run parent, adresse de retour et repertoire de preuves.
  Le coordinateur doit lire le skill avant de creer ses propres workers.
- Le coordinateur ouvre son Run enfant selon le guide Orca et conserve son
  Dispatch parent. Enregistre les deux Runs. Respecte la limite de profondeur ;
  `nested_worker_depth_exceeded` exige une remontee et une adaptation explicite,
  jamais un nouveau Run ou un autre outil pour contourner la limite.
- Traite les questions et escalades, verifie les rapports de fin contre le
  Dispatch actif, puis decide reutilisation ou liberation avant d'acquitter la
  livraison. Les attentes vides et `unverifiable` ne prouvent pas un arret.
  Applique le guide de recuperation en cas d'echec de lancement ou d'incertitude.

## File de merge unique

Le pilote est l'unique proprietaire de la file et de son verrou, meme quand
plusieurs coordinateurs sont prets. Un coordinateur demande la place par `ask`.

1. Verifie son rapport de tests, la couverture de toutes les exigences, la revue
   independante et la PR. Accorde la place a un seul coordinateur.
2. Il actualise sa branche par rapport a `origin/develop` et revalide le candidat.
   Le verrou reste detenu pendant cette verification, le merge et la CI apres
   merge. Un changement de SHA invalide la revue et les preuves du candidat
   precedent ; un changement externe de `develop` exige une reconciliation.
3. Avant le merge, verifie a nouveau le SHA de tete de PR, les checks requis,
   les conversations et le statut de revue requis. Aucun contournement de
   protection, merge administrateur ou merge d'un autre SHA.
4. Un parent n'est termine qu'apres les tests sur le commit effectivement
   fusionne et la cloture tracee dans Linear. Alors seulement libere le verrou
   et les tickets dependants.
5. Si la CI apres merge echoue, gele les merges et les nouveaux lancements.
   Les workers deja actifs peuvent finir leur travail isole. Fais diagnostiquer
   et corriger par une PR tracee ; exige le retablissement de `develop` avant
   de poursuivre. Ne masque pas l'echec par un statut local vert.

## Fin et reprise

Le registre global est ecrit uniquement par le pilote. Mets-le a jour apres
chaque transition avec les preuves recues, sans remplacer les etats Orca,
Linear ou GitHub par des suppositions. Une panne ou une limite de budget doit
laisser un point de reprise, pas un faux succes.

Termine quand tous les tickets du snapshot sont termines ou explicitement
bloques, et que tous les Dispatches lances sont regles et leurs ressources
traitees selon Orca. Les tickets bloques restent ouverts. Fournis le bilan par
ticket : Tasks Orca, checklist, PR, SHA, tests, revue, blocages et preuves. Ne declare pas
le backlog entier realise si des decisions humaines ou du materiel manquent.

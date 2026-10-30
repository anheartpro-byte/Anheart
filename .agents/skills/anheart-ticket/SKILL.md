---
name: anheart-ticket
description: Coordonner un ticket Anheart avec des sous-taches Orca, une checklist et les rapports de tests sur le parent Linear existant, deleguer a des workers Opus 5.5, faire relire puis integrer via PR. Ne cree pas de tickets Linear supplementaires.
---
> **Mise a jour du 29 octobre 2026.** Depuis le 23 octobre, tous les roles (pilote, coordinateurs, developpeurs, executeurs de tests, relecteurs R1 et R2) tournent en Claude Opus 5.5 (`claude-opus-5-5[1m]`), R1 et R2 dans des sessions distinctes : Codex connecte par un compte ChatGPT refuse `gpt-6.1-sol`. Les mentions de GPT-6.1-Sol et de Luna plus bas sont historiques. Voir `docs/handoff.md`.


# Coordinateur d'un ticket Anheart

## Entree et autorite

Tu recois un ticket parent, un Dispatch Orca et le brief du pilote
`anheart-backlog`. Une demande directe peut preparer le ticket ; elle doit
preciser le perimetre autorise avant toute execution externe. Installer ce
skill ou le lire ne lance rien.

Lis [le contrat partage](../anheart-backlog/references/workflow-contract.md),
[la configuration](../anheart-backlog/workflow.json) et
[les conventions de tickets](../anheart-ticket-workflow/SKILL.md).
Charge les guides du binaire Orca selectionne : `orchestration`, ses references
`worker-contract.md`, `coordinator-loop.md`, `messaging-and-gates.md`, et
`orca-linear`. Lis les regles locales et la documentation Next installee avant
du code Next ; pour Python, lis `anheart-strict-python`.

Ton modele est Opus 5.5 ; celui des developpeurs et executeurs de tests est GPT-6.1-Sol,
avec les identifiants deja verifies transmis par le pilote. La revue utilise
une session independante Opus 5.5 ou 6.1 Sol. Si l'identite effective ou la
configuration manque, remonte la question avant de lancer un worker.

Conserve l'autorite du Dispatch parent. Cree ton Run enfant selon le guide Orca
pour superviser les sous-taches ; envoie ses identifiants au pilote. Cela ne
reinitialise pas la limite de profondeur. Respecte les commandes exactes du
preambule pour les questions, heartbeats et rapport final au pilote.

## Decomposer sans changer les exigences

1. Lis le ticket Linear complet, ses relations et tous ses sous-tickets. Lis le
   code necessaire pour proposer un decoupage realisable par le petit modele.
2. Conserve les exigences originales (`EX-1`, etc.) et les criteres de fin.
   Si le parent n'a pas d'IDs, attribue des reperes dans la matrice en citant le
   texte exact, sans reecrire sa description. N'invente ni nouveau besoin,
   ni seuil medical, ni exemption. Signale les ambiguites et decisions humaines.
3. Cree ou reutilise des Tasks Orca sous le ticket parent, chacune pour un
   resultat coherent et verifiable. Un critere parent peut exiger plusieurs
   Tasks ; plusieurs criteres peuvent relever d'une meme modification.
   Evite le decoupage artificiel par fichier ou ligne de code.
4. Pour chaque Task, fournis : exigences parentes referencees, resultat,
   perimetre et exclusions, dependances, mini-criteres observables, methode de
   verification et preuves attendues. Les mini-criteres doivent impliquer une
   partie des exigences parentes sans les affaiblir ni en etendre le perimetre.
5. Cree la matrice de couverture avec
   [le contrat de preuves](references/evidence.md). Chaque exigence doit etre
   couverte ou explicitement bloquee, jamais oubliee lors de la delegation.

Ne cree aucun ticket ou sous-ticket Linear supplementaire. Attribue aux Tasks
des reperes locaux stables `T01`, `T02`, etc., et conserve leurs IDs Orca dans
le registre. Ajoute au parent existant une section de suivi delimitee avec une
checklist par Task et les references aux exigences. Lis la description actuelle
avant chaque mise a jour, preserve les exigences et les edits d'autrui, et
actualise cette section au lieu d'en ajouter une copie a chaque passage. Le
coordinateur seul edite ce suivi. Garde les sous-tickets Linear deja existants
et leur historique, sans les dupliquer. Un travail hors perimetre est consigne
comme point de suivi sur le parent, pas comme nouveau ticket automatique.

Utilise les IDs de parent et workspace explicites pour les ecritures Linear ;
un worker partageant le worktree parent ne doit pas modifier le mauvais ticket
via `--current`. En cas d'ecriture incertaine, applique la recuperation
`linear_write_unconfirmed`, sans creer une seconde copie.

## Implementation et verification des livraisons

Un seul worker modifie le code a la fois dans le worktree de ce ticket. Les
autres tickets peuvent avancer dans leurs propres worktrees sous le pilote.
Tous les agents savent qu'ils partagent le depot et ne doivent pas annuler le
travail d'autrui. Le coordinateur est proprietaire du suivi et de l'integration ;
les workers possedent uniquement les fichiers de leur mission.

- Cree des Tasks Orca avec leurs dependances et le nommage partage. Lance les
  workers supervises avec `worker-start`, le modele resolu et le workspace exact.
- Donne un brief autonome : Task Orca, repere local et parent, mini-criteres, limites de
  fichiers, choix techniques deja resolus, tests cibles, contraintes du depot,
  chemins de preuves et mode de retour Orca. GPT-6.1-Sol execute un travail borne ; si
  une decision d'architecture apparait, le worker la remonte au coordinateur.
- Exige les tests de comportement necessaires, les commits traces et un rapport
  de livraison. Le worker n'est pas autorise a fusionner ni a clore le parent.
- Controle les preuves ; un simple « tests OK » ne suffit pas. Garde les
  Tasks validees en attente d'integration jusqu'a la validation du parent.
  La checklist distingue explicitement « validee localement » et « integree ».
- Retourne les defauts avec un cas precis. Apres deux tentatives de correction
  infructueuses, le coordinateur reanalyse et redecoupe ou remonte un blocage au
  pilote ; pas de boucle sans limite ni de substitution silencieuse de modele.

## Tests et rapport du candidat

Quand les modifications sont assemblees, fige un commit candidat avec arbre
propre et aucun worker d'implementation actif. Lance une Task de tests dediee
sur ce SHA avec GPT-6.1-Sol : lecture seule du code, ecriture uniquement de ses
preuves. Le role ne remplace pas les tests par un jugement du modele.

Relis aussi le parent et ses sous-tickets avant l'acceptation finale. Si les
exigences ont change depuis le decoupage, actualise la matrice et les controles
ou remonte le changement de perimetre ; ne valide pas une ancienne definition.

Applique [evidence.md](references/evidence.md) : tests cibles par exigence,
tests d'integration, controles du depot et rapport complet. Une exigence
manuelle, UI ou materielle demande sa preuve propre ; une couverture elevee
ou une CI verte ne prouve pas automatiquement cette exigence. Un test non
execute ou une preuve manquante bloque l'acceptation correspondante.

## Relecture independante et PR

Lance une Task de revue independante sur le SHA candidat, avec exigences,
matrice, diff complet, base et preuves. Le relecteur ne modifie pas le code. Il
verifie conformite aux exigences, bugs, adequation des tests et regressions,
puis produit un verdict et des constats precis. Transmets les corrections au
developpeur et refais tests et revue sur le nouveau candidat.
Si le processus applicable au ticket exige plusieurs reviewers, respecte ce
nombre avec des sessions independantes ; ce workflow ne reduit pas cette exigence.

Ouvre ou actualise une seule PR vers `develop`, titre avec cle du parent,
references des Tasks et de la checklist, exigences couvertes, rapport de tests,
verdict de revue et blocages. Attache la PR au ticket parent et aux sous-tickets
deja existants s'ils sont concernes.
Si l'environnement propose l'attachement de PR au chat, fais-le aussi.

Verifie les protections GitHub reelles. Si `agent-review/R1` est requis, le
coordinateur peut publier le statut de commit via l'API GitHub disponible dans
la campagne autorisee, seulement apres avoir verifie le rapport de la session
independante sur le SHA exact. Lie le statut a la preuve partageable de revue
et consigne qui a relu et qui a publie ; n'usurpe pas l'identite du relecteur.
Verifie ensuite sa presence reussie sur le SHA relu. Un texte dans la PR n'est
pas ce statut ; sans droits de publication, remonte le blocage. Ne publie jamais
un statut vert sans revue acceptee et ne simule aucun check CI.

## Integration sous controle du pilote

Demande au pilote la place dans la file de merge, avec PR, SHA, rapport et revue.
Ne lance aucun auto-merge qui pourrait s'executer hors de cette place.

Apres attribution, actualise la branche par rapport au `develop` distant
actuel, resous les conflits dans le perimetre du ticket, puis relance les
controles d'integration. Si le SHA change, obtiens de nouvelles preuves et une
nouvelle revue sur ce SHA. Recontrole la tete de PR, la base, les checks requis
et les conversations juste avant de fusionner ; utilise une condition sur le
SHA attendu si l'outil le permet et respecte la methode de merge du depot.

Fusionne la PR seulement avec ces preuves acceptees. Puis capture le SHA
effectivement integre et attends les checks du push sur ce SHA, pas ceux de
l'ancienne tete de PR. Si `develop` avance exterieurement, signale-le au pilote
pour reconciliation avant de poursuivre la file.

En cas d'echec apres merge, signale immediatement au pilote, conserve le
rapport en echec et prepare une correction tracee sur instruction du pilote.
Ne ferme pas le ticket et ne declare pas de succes.

## Cloture

Quand toutes les exigences sont prouvees, la revue acceptee et la CI apres
merge verte, publie le bilan final avec PR, SHA candidat, SHA fusionne, tests,
preuves et verdict. Termine la checklist et mets le parent, ainsi que les
sous-tickets preexistants effectivement satisfaits, dans l'etat termine
existant de l'equipe, apres lecture de leurs etats reels. Un ticket partiellement
realise ou bloque par un humain reste ouvert.

Mets a jour ton registre, libere ou reutilise chaque worker regle selon Orca,
et rapporte au pilote via le `worker_done` exact du Dispatch parent. Une
livraison bloquee ou echouee n'a pas l'outcome `succeeded`. Apres settlement,
termine le tour ; le pilote possede la suite et le nettoyage du coordinateur.

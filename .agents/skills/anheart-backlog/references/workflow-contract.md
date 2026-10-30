> **Mise a jour du 29 octobre 2026.** Depuis le 23 octobre, tous les roles (pilote, coordinateurs, developpeurs, executeurs de tests, relecteurs R1 et R2) tournent en Claude Opus 5.5 (`claude-opus-5-5[1m]`), R1 et R2 dans des sessions distinctes : Codex connecte par un compte ChatGPT refuse `gpt-6.1-sol`. Les mentions de GPT-6.1-Sol et de Luna plus bas sont historiques. Voir `docs/handoff.md`.

# Contrat partage : modeles, nommage et reprise

Ce fichier complete les deux skills Anheart. La configuration est
[workflow.json](../workflow.json) ; les guides servis par le binaire Orca
restent la reference des commandes. Ce sont des instructions executees par un
agent, pas un service ni un ordonnanceur installe en arriere-plan.

## Modeles

Les libelles demandes par l'utilisateur sont conserves exactement dans la
configuration. Les `model_id: null` signifient **non resolu**, pas modele par
defaut. N'invente pas un identifiant a partir d'un libelle et ne remplace pas
silencieusement une version indisponible.

Avant le premier ticket, consulte les catalogues accessibles sur l'hote
d'execution et les options de lancement du fournisseur. Le developpement et les
tests utilisent `gpt-6.1-sol`. Si Opus 5.5 n'est pas disponible,
signale-le avant toute delegation et demande le choix de remplacement.
Le relecteur utilise Opus 5.5 de preference ou 6.1 Sol, tous deux autorises ;
consigne celui effectivement choisi. Un meme modele est permis pour le
coordinateur et le relecteur, mais la session de revue est independante du
developpeur. Ne diminue pas les exigences parce que le worker est petit.

Conserve les correspondances validees dans le registre de campagne et transmets
les identifiants resolus aux workers. Utilise `--model` pour chaque nouveau
lancement et compare le modele demande au modele effectif du recu. En cas de
divergence ou d'absence de preuve, n'annonce pas le modele comme confirme et
resous le probleme avant d'autoriser le travail. N'ajoute `--effort` que si
necessaire et supporte. Aucun changement global des modeles Orca n'est requis.

La decision utilisateur du 15 octobre remplace Luna par GPT-6.1-Sol pour toutes
les prochaines Tasks de developpement et de tests, y compris les corrections
et relances. Une Task deja active peut se terminer sur son modele actuel.
Ne reutilise pas une session Luna pour une nouvelle Task : lance une session
Codex avec `--model gpt-6.1-sol`, puis verifie le modele effectif du recu.
Les roles Opus du pilote et des coordinateurs et les choix de revue restent
inchanges. Les anciens briefs/recus Luna sont des traces historiques, pas la
configuration a appliquer aux prochains lancements.

## Quota Linear et suivi

Decision utilisateur du 15 octobre 2026 : garder les tickets parents existants
dans Linear, et toutes les nouvelles sous-taches d'execution dans Orca. Aucun
nouveau ticket Linear, meme pour le decoupage ou un suivi hors perimetre, sans
nouvelle demande explicite de l'utilisateur. Ne supprime pas les sous-tickets
deja crees. Les mini-criteres, dependances, preuves, tests et revues restent requis.

Le parent porte une unique section `Suivi Orca` dans sa description : une
checklist avec reperes locaux stables, exigences couvertes, resultat attendu,
statut de validation/integration et liens des preuves. Le registre associe ces
reperes aux IDs Task/Dispatch. La synthese du rapport de tests et de revue est
publiee sur le parent et dans la PR. N'ecrase jamais la description originale
ou les modifications humaines pour mettre cette section a jour.

## Nommage

Les accolades ci-dessous sont des champs a remplir avec des valeurs reelles,
jamais des identifiants a envoyer tels quels a un outil.

| Objet | Convention |
| --- | --- |
| Run global | `Anheart - Backlog - {date}-{identifiant-court}` |
| Pilote | `Anheart - Pilote backlog` |
| Coordinateur / Run enfant | `{ticket-parent} - Coordination` |
| Task d'implementation | `{ticket-parent} / {repere-local} - Developpement - {titre-court}` |
| Task de tests | `{ticket-parent} - Tests - {sha-court}` |
| Task de revue | `{ticket-parent} - Revue R1 - {sha-court}` |
| Task apres merge | `{ticket-parent} - Verification integration - {sha-court}` |
| Ligne de checklist du parent | `{repere-local} - {resultat-concret} / {EX-concernees}` |
| PR | `{ticket-parent}: {resultat-livre}` |

Utilise `task-create --task-title` et, si utile, `--display-name` pour les Tasks.
Ne passe pas les flags de creation de worktree a un workspace existant.
Renomme un onglet seulement via le mecanisme supporte par sa surface ; un handle
de chat n'est pas necessairement un terminal. Les noms servent a lire le suivi,
les vrais IDs servent a router les messages et reconnaitre l'autorite.

Une branche et une PR par ticket parent. Respecte le `gitBranchName` Linear et
le skill `anheart-ticket-workflow` ; verifie le nom effectivement cree par Orca.
Ne laisse pas un nom d'affichage changer le nom de branche requis. Les nouveaux
commits de sous-taches utilisent la cle Linear reelle du parent, sous la forme
`{ticket-parent}: {changement} ({repere-local})`. Les reperes T01/T02 ne sont pas
des cles Linear. Conserve la tracabilite deja etablie des sous-tickets existants.

## Etat durable et partage entre agents

Au lancement, cree un repertoire local de campagne sous
`~/.local/state/anheart-orchestration/`, hors des checkouts applicatifs.
Resous son chemin absolu et transmets-le dans les briefs. N'enregistre aucun
secret. Ces fichiers sont des preuves et un index de reprise, pas un substitut
aux registres Orca ou aux checks GitHub.

- `campaign.json` : ecrit par le pilote seul. Inclut schema_version, identite
  du depot/hote, scope et snapshot Linear, source absolue des skills, modele
  effectif du pilote, correspondances de modeles, Run global, graphe, limites,
  verrou de merge (ticket, Dispatch, PR, SHA candidat, base, etape), et tickets.
- Pour chaque ticket : IDs et URLs Linear, dependances, statut logique, Task et
  Dispatch parent, Run enfant, worktree, branche, PR, SHA candidat et SHA merge,
  liens de preuves, blocage et prochaine action.
- `tickets/{cle}/state.json` : ecrit par le coordinateur seul. Detaille les
  checklist, exigences, Tasks, Dispatches, tentatives et resultats, ainsi que
  les sous-tickets Linear preexistants eventuels. Chaque
  worker ecrit ses preuves dans son propre sous-repertoire de Dispatch.
- `tickets/{cle}/reports/{sha-complet}/` : rapport Markdown, sorties de tests,
  JUnit/coverage quand disponibles, preuves manuelles et revue. Les revisions
  du rapport identifient leurs preuves ; ne remplace pas un echec par un succes
  sans garder sa trace. Les tests apres merge portent leur propre SHA.

Les rapports restent hors des sources pour ne pas creer un nouveau commit
apres chaque validation. Publie leur synthese et les preuves partageables dans
la PR et Linear au cours d'une campagne autorisee. Un chemin local seul ne
constitue pas une preuve accessible aux lecteurs de la PR : inclus les
resultats detailles et les liens CI/artefacts, et conserve les fichiers locaux.

Les worktrees issus de `develop` peuvent ne pas encore contenir ces skills.
Transmets alors la racine absolue des skills depuis le checkout de preparation,
avec leur revision ou empreinte, et fais-la lire explicitement aux agents
Claude comme Codex. Ne suppose pas que des fichiers non commites sont copies.

## Reprise et verrous

Avant de reprendre, reconcilie les Runs/Dispatches, les sous-tickets existants,
la tete de PR, le merge eventuel et les checks du SHA fusionne. Reutilise les
IDs connus ; une absence de reponse n'autorise pas un doublon.

Une seule campagne d'execution et un seul pilote possedent la file de ce depot
sur cet hote. Si un pilote existe deja, reprends son contexte ou fais un
transfert explicite suivant le guide Orca, jamais deux pilotes concurrents.
Une preparation peut coexister mais ne prend pas la file. Ne vole pas un verrou de merge sur timeout : verifie
positivement le proprietaire et l'etat GitHub avant une reprise autorisee.

Statuts logiques : `queued`, `active`, `waiting-review`, `ready-to-merge`,
`merging`, `post-merge-check`, `done`, `blocked`. Ce ne sont pas des statuts
Linear a creer. Resous les vrais etats de l'equipe avant de les utiliser.
`worker_done` sur une sous-tache signifie sa livraison, pas la cloture Linear
du parent. Le coordinateur ne rapporte son succes final qu'apres integration.

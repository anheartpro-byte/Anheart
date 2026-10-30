# Preuves d'acceptation et rapport de tests

Lire avant de deleguer les tests et avant de valider un candidat. Le rapport
est une preuve de comportement sur un commit exact, pas une liste de commandes
prevues. Conserver les criteres originaux et les essais infructueux.

## Contenu obligatoire du rapport

1. Ticket parent, URL, reperes de checklist et IDs Tasks Orca, branche, base, SHA complet du candidat,
   date, environnement et versions des outils pertinents. Identifier les
   changements d'environnement qui limitent la reproductibilite.
2. Matrice par exigence : reference et texte original, Tasks et reperes de checklist,
   mini-criteres, tests ou scenarios precis, resultat, preuve et SHA verifie.
   Chaque mini-critere a sa propre verification ; la ligne du parent etablit
   aussi que leur combinaison satisfait l'exigence complete.
3. Journal des executions : repertoire, commande exacte, debut/fin ou duree,
   code de sortie, nombres de tests reussis/echoues/ignores si disponibles,
   resultat de couverture exige et liens des sorties/JUnit/artefacts.
4. Verification sur la surface reelle quand requise : etapes, resultat attendu,
   resultat observe, capture ou fichier produit. Une exigence d'impression
   necessite le PDF reel ; un test de composant seul ne la valide pas.
5. Revue : agent et modele effectifs, SHA relu, constats, resolutions et verdict.
6. Decision : `passed`, `failed`, `blocked` ou `not-run` par critere et pour
   l'ensemble, avec justification. `blocked` et `not-run` ne sont pas verts.
7. Integration : PR, SHA effectivement fusionne, executions CI du push, leurs
   liens et conclusions, puis decision finale. Avant merge, cette partie est
   explicitement en attente et n'empeche pas l'acceptation du candidat ; elle
   empeche sa cloture comme ticket termine.

Ne deduis pas le succes d'une sortie partielle, d'un code de sortie du dernier
element d'un pipeline, d'un test ignore ou d'une moyenne de couverture. Conserve
le code du runner et les echecs de chaque etape. Les preuves portent sur un
arbre propre ; si le code change pendant les tests, invalide cette execution.
Les runners qui generent des fichiers ne doivent pas modifier le code candidat.

## Selection des controles Anheart

Relis les scripts et workflows du checkout cible : cette liste est un point
d'entree, pas une copie figee de la CI. Ajoute les tests de comportement utiles
pour les criteres qui ne sont pas encore couverts. Ne reduis pas les gates.

| Perimetre | Sources et controles a consulter |
| --- | --- |
| Site / bibliotheques | `package.json`, `npx tsc --noEmit`, `npm run lint`, `test:lib`, `test:site`, `coverage:site`, `npm run build` ; scenarios UI selon exigences |
| Convex | `node scripts/ci/convex-generated-api.mjs`, `npx tsc -p convex/tsconfig.json --noEmit`, `test:convex`, `coverage:convex` |
| Pi | `bash raspberry-pi/scripts/check.sh`, contrat `anheart-strict-python` ; tests du panneau web selon les changements |
| Simulation | `bash simulation/scripts/check.sh` et scenarios de `.github/workflows/ci.yml` ; inclure les tests de `deploy/simulation-vercel/tests` si concernes |
| CI / docs / release | jobs `docs` et `audit` de `.github/workflows/ci.yml`, tests de scripts correspondants, `npm run test:release` si pertinent |

Les noms `test:*` et `coverage:*` ci-dessus sont des scripts npm. Utilise les
versions et variables de test du depot. Ne lance pas `npm run dev` pour
initialiser les tests sans lire ses hooks : ce projet peut lancer Convex.
Un secret ou un service manquant produit un blocage explicite, pas un succes.

Collecte JUnit et coverage via les options deja utilisees par la CI. Les gates
Pi et simulation acceptent `QUALITY_REPORT_DIR` ; les executions shardees ont
leurs propres variables et doivent etre combinees selon leurs scripts. Une
partie reussie ne prouve pas la gate entiere. Le rapport existant
`scripts/ci/quality-report.mjs` peut servir de synthese, mais sa propre reussite
ne remplace pas celle des gates, ni la matrice des exigences.

Les six gates de reference sont `pi-gate`, `simulation-gate`, `convex-tests`,
`web`, `audit`, `docs` ; verifie les noms et protections actuels avant le merge.
Sur PR, distingue les exclusions de chemins legitimement appliquees par la CI
d'un controle d'acceptation obligatoire non execute. Apres merge, exige les
six gates sur le SHA du push vers `develop`, ainsi que tout autre controle
requis. Un run annule, absent, en attente ou seulement ignore ne suffit pas.

## Livraison des preuves

L'executeur rend au coordinateur son rapport et les fichiers existants, avec le
SHA et son verdict explicite. Le coordinateur verifie la matrice avant la revue.
Publie dans la PR une synthese lisible par exigence et les liens des executions
et artefacts disponibles, puis le bilan de cloture dans Linear. Ne copie ni
secrets ni donnees personnelles dans les logs ou commentaires.

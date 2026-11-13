# Programmes d'entraînement

## Aujourd'hui

Les programmes sont définis et conservés sur chaque Raspberry Pi. Quand leur
révision change, le Pi synchronise avec Convex les profils que sa configuration
de sécurité autorise. Convex les copie dans `machine_profiles`, liés à leur
machine ; `syncProfiles` remplace cette liste en bloc. Le site lit ces profils
pour montrer le nom, la zone, la durée, la FC limite et la vitesse maximale.
Ils sont en lecture seule dans le site. Le Pi reste la source de vérité des
programmes proposés.

Depuis ANH-137, l'équipe Anheart écrit aussi un **catalogue** de programmes
dans Convex, depuis le site, sans toucher au code ni au Pi (section suivante).
Ce catalogue ne part pas encore vers les machines : c'est ANH-138. D'ici là,
les lancements utilisent toujours les programmes synchronisés par le Pi.

## Catalogue de programmes (ANH-137)

Décision produit du 1er octobre 2026 : l'équipe Anheart crée, valide et
publie les programmes ; une organisation cliente n'en écrit jamais, elle
utilise ceux qui sont publiés. Tables `programmes` et `programme_revisions`,
fonctions de `convex/programmes.ts` : voir [convex.md](convex.md).

### Qui fait quoi

| Qui | Ce qu'il fait |
|---|---|
| Admin Anheart (`admin`) | Crée un brouillon, le duplique, le modifie, y joint le rapport de simulation, publie un programme validé, retire un programme publié. |
| Un second admin Anheart | Valide un brouillon qu'il n'a ni créé ni réécrit en dernier, en saisissant la signature médicale (double validation nommée : `createdBy` et `validatedBy`). |
| Admin d'organisation, gestionnaire, patient | Lisent les programmes publiés, sans pouvoir les écrire ; ils ne voient ni les brouillons, ni les programmes retirés, ni le rapport de simulation. |

Chaque écriture et chaque changement d'état est dans le journal d'audit
(`programme.created`, `programme.updated`, `programme.report_attached`,
`programme.validated`, `programme.published`, `programme.retired`), en
ligne globale lisible par l'admin Anheart.

Au lancement depuis le site, on ne change que la durée totale, jamais la
zone, les paliers ou la vitesse : c'est la seule surcharge que prend
`training.launchAutoSession`, et elle ne peut que **raccourcir** le
programme. Le maintien absorberait toute durée plus longue, que rien d'autre
ne borne : Convex refuse une durée plus longue que celle du programme
(`duration_above_programme`), et la console refuse elle aussi un lancement du
tableau de bord plus long que son programme, avant `training/start`, en le
rendant comme une séance échouée avec sa raison. Une durée saisie à la
console reste libre, comme avant. Le lancement depuis le catalogue arrive
avec ANH-138.

### Cycle de vie

1. **`draft`** : créé par un admin Anheart. Chaque enregistrement passe la
   validation du Pi (section suivante) et refuse un `profileId` déjà pris,
   même par un programme retiré, car il sert de clé aux séances enregistrées.
   Le serveur écrit `medicalParametersVersion`, la version du fichier médical
   dont il a appliqué les bornes. Réécrire un brouillon efface son rapport de
   simulation et sa signature médicale : ils portaient sur d'autres valeurs.
2. **`validated`** : seulement si, au moment de valider, la validation du Pi
   passe encore sur les valeurs enregistrées, la version médicale courante du
   serveur est `medicalParametersVersion`, un rapport de simulation est joint
   et la signature médicale (`{by, at, note}`) est renseignée, par un admin
   Anheart autre que le créateur et le dernier rédacteur. **En production,
   aucune version médicale n'est enregistrée** (décision de l'utilisateur, en
   attendant les valeurs signées d'ANH-98) : aucun programme ne peut y être
   validé, et le refus le dit (`programme_medical_version_unavailable`).
3. **`published`** : un admin Anheart publie un programme validé. `revision`
   augmente de 1 et la copie complète du programme est gardée dans
   `programme_revisions`.
4. **`retired`** : un programme publié est retiré du catalogue. Il reste
   lisible par l'admin Anheart, et chaque séance garde sa propre copie de ce
   qui a été lancé (`profileId`, nom, zone, durée, FC max) ; il ne sera ni
   lancé ni envoyé aux machines.

Aucune autre transition n'existe : un programme publié ne redevient pas
brouillon. Pour le changer, on le duplique en un nouveau brouillon.

### Validation identique au Pi

`convex/lib/programmes.ts` (`programmeRefusal`) reprend une à une les
vérifications de `TrainingProfile` et de `parse_profile` dans
`raspberry-pi/src/training/plan.py`, dans leur ordre, avec le même verdict et
le même message, par exemple `training profile refused:
zone_above_subject_ceiling`. Les refus sont ceux de l'énumération
`Violation` : identifiant de profil en snake_case de 64 caractères au plus,
nom non blanc (au sens de Python), durées positives et finies, cool-down d'au
moins la rampe du variateur (4 s) et du plancher médical, récupération d'au
moins le plancher médical, maintien (la durée totale moins les phases fixes)
d'au moins `holdMinS`, seuils cardiaques positifs, FC max du sujet entre 100
et 220, zone croissante, `zoneHighBpm <= 0,9 x subjectHrMax` (la fraction du
fichier médical), `hardMaxBpm <= subjectHrMax`, `criticalBpm > hardMaxBpm`,
vitesses positives, `minRunRpm <= maxRpm`, `maxRpm` au plus la plaque
(1380 tr/min moteur) sauf `allowAboveNameplate`, fraction d'échauffement dans
`]0, 1]`, plafond d'échauffement arrondi vers le bas d'au moins `minRunRpm`,
voie ECG présente et aucune voie en double. Un seuil ou une vitesse qui n'est
pas un nombre entier est refusé avec le message du Pi pour un document mal
formé.

Les bornes que `plan.py` prend dans le fichier médical (plancher de
récupération, plancher de cool-down, fraction de zone) sont lues dans le même
fichier, `raspberry-pi/config/medical_parameters.json` : les deux côtés ne
peuvent pas diverger.

Le fichier partagé `contracts/programme-validation-cases.json` porte les cas,
bornes comprises (zone haute égale à 0,9 x FC max, `minRunRpm` 55 au plafond
d'échauffement, plaque 1380, durées dont la somme vaut la durée totale,
`allowAboveNameplate`, arrondi des flottants). Le test
`raspberry-pi/tests/test_programme_validation_parity.py` les passe à
`parse_profile` et le test `convex/programmes.test.ts` à `programmes.create` :
les deux exigent le verdict et le message du fichier.

Ce que cette validation ne juge pas, parce que cela dépend d'une machine et
non du programme : l'égalité de `hardMaxBpm` et `criticalBpm` avec les paliers
du superviseur, et `maxRpm` sous le plafond avec une personne à bord. Le Pi
les vérifie au départ de chaque séance (`TrainingRuntime._limits_mismatch`,
`LocalConfig.ceiling_for`) et n'envoie que les profils dont les paliers sont
les siens ; la publication vers les machines (ANH-138) devra les contrôler.
`plan.py` n'impose pas non plus `minRunRpm >= 55` : 55 tr/min est la plus
petite consigne du limiteur de mouvement (`min_run_motor_rpm` du fichier
médical), pas une règle du profil.

### Rapport de simulation

Le rapport est un fichier PDF ou JSON d'au plus 10 Mio que l'admin joint au
brouillon, déposé dans Convex Storage depuis moins d'une heure
(`simulationReport` garde son `storageId`). Il vient de la simulation
d'ANH-99, `python -m simulation.quick --cohort` (`report.json`) ; l'option
`--profile <json>` qui simulera le programme lui-même appartient à ANH-99 et
n'existe pas encore dans `simulation/quick.py`.

Reste à faire : lancer cette simulation depuis le site par un
`workflow_dispatch` de GitHub Actions. Il faut pour cela un jeton GitHub dans
Convex, un secret externe qui n'existe pas encore. Le champ ne changera pas :
le workflow déposera son rapport dans Convex Storage et en écrira le
`storageId` dans `simulationReport`, sans migration.

## Vérification d'un lancement distant

Un lancement auto depuis le site passe deux contrôles successifs :

1. **Convex** vérifie les droits, le programme synchronisé, la physiologie et
   les paramètres médicaux de la version courante du registre
   `medical_parameters_versions`. La fraction de plafond de zone, l'âge
   minimum et la formule d'estimation de la FC max viennent du fichier signé
   enregistré. Sans version courante, Convex refuse fermé avec
   `medical_parameters_unavailable`. Si le heartbeat de la machine annonce
   explicitement `medical_parameters_version: null`, Convex renvoie
   `medical_parameters_unsigned` avant `programs_disabled` et le site indique
   que la machine est en mode banc.
2. **Le Pi** lit le lancement à distance, puis vérifie à nouveau le programme,
   l'âge et les conditions locales de sécurité avec son propre
   `config/medical_parameters.json` signé. La version annoncée par le Pi est
   conservée dans `machines.medicalParametersVersion`. La vérification locale
   du Pi fait foi : Convex prépare le lancement, mais ne peut pas autoriser le
   mouvement.

La fiche machine montre la version médicale annoncée et son état par rapport à
la version de référence du serveur : `unknown`, `unsigned`, `no_reference`,
`current`, `obsolete` ou `ahead`. `unknown` signifie qu'il n'y a pas d'annonce
et n'a pas de libellé dans la fiche. Les écrans de lancement affichent la
raison précise lorsque le serveur n'a pas de version médicale ou lorsque la
machine signale qu'elle n'a pas de paramètres signés. Voir
[convex.md](convex.md) et [tableau-de-bord.md](tableau-de-bord.md).

## Parité vérifiée

Le test `raspberry-pi/tests/test_medical_parity.py` et le test
`convex/medicalParity.test.ts` lisent le même jeu de cas,
`raspberry-pi/tests/fixtures/medical_zone_parity.json`. Ils vérifient que le Pi
et Convex calculent la même estimation Tanaka et rendent les mêmes décisions
de plafond de zone pour ces cas, notamment aux âges 25, 45, 65 et 85 ans. Cette
parité de calcul ne remplace pas la vérification du fichier signé par le Pi.

## Reste à faire

Le catalogue enregistre `medicalParametersVersion` et ne valide un programme
que pour la version médicale courante du serveur. Le Pi devra refuser un
programme validé pour une autre version médicale, revalider tout programme
reçu et vérifier les paliers et le plafond de sa machine : ces contrôles
relèvent d'ANH-138, avec la publication vers les machines.

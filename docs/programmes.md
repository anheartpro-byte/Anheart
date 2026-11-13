# Programmes d'entraînement

## Aujourd'hui

Depuis ANH-137, l'équipe Anheart écrit un **catalogue** de programmes dans
Convex, depuis le site, sans toucher au code ni au Pi (section suivante).
Depuis ANH-138, ce qui y est publié descend vers chaque machine, signé
([Publication vers les machines](#publication-vers-les-machines-anh-138)) :
le Pi le revalide avec ses propres règles, garde ce qu'il accepte et dit ce
qu'il refuse, et pourquoi.

Le Pi reste la source de vérité des programmes proposés. Quand ses
programmes changent, il synchronise avec Convex les profils que sa
configuration de sécurité autorise. Convex les copie dans `machine_profiles`,
liés à leur machine ; `syncProfiles` remplace cette liste en bloc. Le site lit
ces profils pour montrer le nom, la zone, la durée, la FC limite et la
vitesse maximale, en lecture seule, et les lancements n'utilisent qu'eux.

## Catalogue de programmes (ANH-137)

Décision produit du 1er octobre 2026 : l'équipe Anheart crée, valide et
publie les programmes ; une organisation cliente n'en écrit jamais, elle
utilise ceux qui sont publiés. Tables `programmes` et `programme_revisions`,
fonctions de `convex/programmes.ts` : voir [convex.md](convex.md). Page du
site : `/dashboard/programmes`, voir [tableau-de-bord.md](tableau-de-bord.md#programmes--frdashboardprogrammes).

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
console reste libre, comme avant. Un programme du catalogue se lance de la
même façon, une fois qu'une machine l'a accepté : il est alors dans ses
programmes synchronisés.

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
nom non blanc (au sens de Python) et sans surrogat isolé (`name_lone_surrogate` : il
n'a pas d'UTF-8, et aucune réponse de la console ne pourrait le porter), durées
positives et finies, retour au calme d'au
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

Deux bornes tiennent compte de la façon dont un nombre passe de Convex au Pi.
À partir de 1e21, `JSON.stringify` écrit un nombre entier `1e+21`, que le Pi
lit comme un flottant : Convex refuse donc un seuil ou une vitesse de 1e21 ou
plus avec le message du Pi pour un flottant. Un zéro négatif est écrit `0` et
refusé comme un zéro (`bpm_not_positive`). À l'inverse, `json.loads` lit un
entier sans limite de taille : le Pi refuse comme mal formé un nombre qu'un
flottant ne peut pas contenir (au-delà d'environ 1,8e308), au lieu de lever
`OverflowError`. Ce dernier cas n'est pas dans le fichier partagé : un tel
entier n'existe pas en JavaScript (`JSON.parse` le lit comme `Infinity`), et
Convex ne le reçoit jamais ; des tests du Pi le couvrent (lecture d'un profil,
magasin de profils écarté au démarrage, programme reçu du catalogue signé).

Les bornes que `plan.py` prend dans le fichier médical (plancher de
récupération, plancher de retour au calme, fraction de zone) sont lues dans le même
fichier, `raspberry-pi/config/medical_parameters.json` : les deux côtés ne
peuvent pas diverger.

Le fichier partagé `contracts/programme-validation-cases.json` porte les cas,
bornes comprises (zone haute égale à 0,9 x FC max, `minRunRpm` 55 au plafond
d'échauffement, plaque 1380, durées dont la somme vaut la durée totale,
`allowAboveNameplate`, arrondi des flottants, fraction absurde, 1e21, zéro
négatif). Le test
`raspberry-pi/tests/test_programme_validation_parity.py` les passe à
`parse_profile` et le test `convex/programmes.test.ts` à `programmes.create` :
les deux exigent le verdict et le message du fichier.

Ce que cette validation ne juge pas, parce que cela dépend d'une machine et
non du programme : l'égalité de `hardMaxBpm` et `criticalBpm` avec les paliers
du superviseur, et `maxRpm` sous le plafond avec une personne à bord. Le Pi
les vérifie au départ de chaque séance (`TrainingRuntime._limits_mismatch`,
`LocalConfig.ceiling_for`) et n'envoie que les profils dont les paliers sont
les siens ; depuis ANH-138, il les contrôle aussi à la réception de chaque
programme du catalogue ([ci-dessous](#ce-que-la-machine-en-fait)).
`plan.py` n'impose pas non plus `minRunRpm >= 55` : 55 tr/min est la plus
petite consigne du limiteur de mouvement (`min_run_motor_rpm` du fichier
médical), pas une règle du profil ; c'est une règle de la machine, que le Pi
applique à un programme du catalogue.

### Décisions à confirmer par l'utilisateur

Deux choix de cette version, relevés en revue et laissés tels quels :

- La double validation n'écarte que le créateur et le dernier à avoir écrit
  les valeurs du brouillon (`createdBy`, `updatedBy`). Un admin qui a réécrit
  le brouillon avant le dernier rédacteur, ou qui y a joint le rapport, peut le
  valider.
- La validation compare la version des paramètres médicaux par sa chaîne
  (`medicalParametersVersion` égale à la version courante enregistrée), pas le
  contenu du fichier médical.

### Rapport de simulation

Le rapport est un fichier PDF ou JSON d'au plus 10 Mio que l'admin joint au
brouillon depuis la page Programmes, déposé dans Convex Storage depuis moins d'une heure
(`simulationReport` garde son `storageId`). Il vient de la simulation
d'ANH-99, `python -m simulation.quick --cohort` (`report.json`) ; l'option
`--profile <json>` qui simulera le programme lui-même appartient à ANH-99 et
n'existe pas encore dans `simulation/quick.py`.

Reste à faire : lancer cette simulation depuis le site par un
`workflow_dispatch` de GitHub Actions. Il faut pour cela un jeton GitHub dans
Convex, un secret externe qui n'existe pas encore. Le champ ne changera pas :
le workflow déposera son rapport dans Convex Storage et en écrira le
`storageId` dans `simulationReport`, sans migration.

## Publication vers les machines (ANH-138)

Ce qui est publié part vers les machines sans intervention, mais chaque
machine reste l'autorité : tout ce qui descend du cloud est une proposition
que le Pi revalide ([README](README.md), « Qui décide quoi »).

### Ce que Convex envoie

`GET /api/machine/programmes?since=<révision>` (contrat 1.11, détail dans
[convex.md](convex.md#le-catalogue-signé-contrat-111-anh-138)) rend, à une
machine authentifiée par sa clé, un texte signé : la révision globale du
catalogue (`catalogRevision`, un de plus à chaque publication et à chaque
retrait) et tous les programmes `published` du catalogue Anheart, chacun avec
sa `medicalParametersVersion`, ou aucun quand la machine a déjà cette
révision. La signature est Ed25519, par la clé privée Anheart que Convex lit
dans `ANHEART_CATALOG_SIGNING_KEY` ; la machine la vérifie avec la clé
publique de son image, `raspberry-pi/config/anheart_catalog.pub`. Sans clé
posée dans Convex, rien n'est signé (`not_configured`) et les machines
gardent leurs programmes : c'est l'état de tous les déploiements aujourd'hui
([secrets.md](secrets.md#34-clés-de-signature)).

Un programme retiré n'est plus envoyé : la machine le perd à sa prochaine
mise à jour, au repos. Une séance déjà lancée garde sa propre copie
(`sessions`), et la console la sienne : rien ne l'interrompt.

### Ce que la machine en fait

Détail dans [raspberry-pi.md, 8.8](raspberry-pi.md#88-le-catalogue-de-programmes-signé-anh-138).
La console demande le catalogue **au repos seulement** (aucune séance, aucun
départ en attente, aucun lancement du site offert ou en confirmation), à son
démarrage puis au plus une fois par minute. Elle vérifie la signature avant
de lire quoi que ce soit ; une signature invalide est écrite comme
avertissement dans son journal de bord et la réponse est ignorée : ses
programmes restent ceux d'avant. Puis elle juge chaque programme avec ses
propres règles, dans cet ordre :

1. `medicalParametersVersion` égale à la version de son fichier médical
   signé (aucun programme accepté en mode banc) ;
2. la validation de `plan.py` (la section précédente), avec les bornes de
   son propre fichier médical ;
3. les paliers du programme égaux à ceux de son superviseur
   (`HR_HARD_MAX_BPM`, `HR_CRITICAL_BPM` du fichier signé) ;
4. `minRunRpm` au moins la plus petite consigne de ses limites de mouvement
   signées ;
5. `maxRpm` au plus la plaque moteur (1380 tr/min), quoi que dise
   `allowAboveNameplate` ;
6. `maxRpm` sous son plafond avec une personne à bord, qui doit exister
   (`OCCUPANCY_OCCUPIED_ENABLED=true` et paramètres signés).

Les programmes acceptés remplacent son magasin (`data/profiles.local.json`)
en une écriture atomique ; chaque refusé est dit au site avec sa raison. La
mise à jour n'est appliquée que si la console est encore au repos quand la
réponse arrive, puis encore quand l'écriture a abouti : un lancement du site
offert pendant l'écriture garde les programmes qu'il a vus. Les profils
livrés (`config/profiles.default.json`) ne sont plus que ses programmes de
secours, tant qu'aucun catalogue signé n'a été accepté, et la console n'a
plus de route pour écrire un profil. Un catalogue où rien n'a jamais été
publié (révision 0) n'en est pas un : la console garde ses programmes de
secours, et une console qui tenait le catalogue d'un autre déploiement
signé par la même clé y revient (une ligne au journal de bord), sans plus
annoncer la révision de l'autre.

À chaque démarrage, qu'elle ait un tableau de bord ou non, la console juge
de nouveau le catalogue qu'elle a gardé : accepté sous d'autres paramètres
médicaux, il est écarté entier ; sinon chaque programme que sa configuration
refuse maintenant (un plafond plus bas, la personne à bord désactivée) est
écarté, jusqu'au prochain catalogue accepté.

Deux conséquences à connaître :

- **Une console où `OCCUPANCY_OCCUPIED_ENABLED=false`** (le réglage par
  défaut), ou dont les paramètres médicaux ne sont pas signés, n'a pas de
  plafond avec une personne à bord : elle refuse chaque programme du
  catalogue avec cette raison, et son magasin n'en garde aucun. Un programme
  a toujours quelqu'un à bord : il n'y partirait pas. Une séance de banc
  lancée du site y part toujours, son départ ne lisant pas le magasin.
- **Convex ne vérifie pas les paliers cardiaques**, ni le plafond, contre le
  fichier médical et la configuration de chaque console : il ne les connaît
  pas. Un programme dont les paliers ne sont pas ceux d'une machine est
  refusé par elle, et sa fiche affiche « refusé par la machine : » suivi des
  paliers en cause.

### Ce que la fiche machine montre

La machine dit ce qu'elle a fait du catalogue avec ses programmes
(`POST /api/machine/profiles`) : la révision acceptée, ou `null` quand elle
tourne sur ses **programmes de secours** (ceux livrés avec la console, quand
aucun catalogue signé n'a jamais été reçu), et chaque programme refusé avec
sa raison. La carte Programmes de la fiche machine affiche « Programmes de
secours » dans le premier cas, et « refusé par la machine : » suivi de la
raison pour chaque refus. Un programme refusé n'est pas dans les programmes
synchronisés de la machine : il n'est pas lançable sur elle.

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

- La clé de signature du catalogue : la créer, poser sa partie privée dans
  Convex et livrer sa partie publique dans l'image du Pi
  ([secrets.md](secrets.md#34-clés-de-signature)), action de l'utilisateur.
  D'ici là, aucune machine ne reçoit de catalogue et toutes gardent leurs
  programmes.
- Mesurer, contre le déploiement de développement et une console en
  simulation, le délai entre une publication et un programme lançable sur le
  site (critère d'acceptation d'ANH-138 : moins de 2 minutes ; au plus une
  minute pour la demande, puis l'envoi des profils). Jamais mesuré.

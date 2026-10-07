# Enregistrement de séance, schéma 2

Le format d'ANH-127 est commun au Pi et à la simulation. La bibliothèque
`raspberry-pi/src/record/` définit les valeurs typées, le writer, le reader et
la validation. La simulation appelle cette bibliothèque, et la console du Pi
aussi depuis ANH-128 : elle écrit un dossier par séance sous `data/records/`,
par une file bornée et un fil d'écriture à part, sans jamais écrire dans la
boucle de contrôle. Ce branchement (file, cadence de `fsync`, comportement en
cas d'erreur, refus de départ, rétention, export) est décrit dans
[raspberry-pi.md](raspberry-pi.md#14-lenregistrement-de-séance-boîte-noire-locale).
Ce document-ci ne décrit que le format.

## Arborescence

Une séance fermée contient exactement ces sept entrées :

```text
2026-10-05T101112Z_<session_id-ou-local_ref>/
  manifest.json
  ticks.csv
  events.jsonl
  drive_frames.jsonl
  ecg_raw/
    000000.bin.gz
    000002.bin.gz
  sensors.csv
  checksums.sha256
```

La date du chemin est l'heure UTC du début, sans deux-points. Les identifiants
acceptent uniquement les lettres ASCII, chiffres, `_` et `-`. Ils doivent
être des identifiants opaques, jamais des noms. Un dossier existant est refusé,
jamais remplacé. La simulation utilise un identifiant aléatoire par séance,
même si son horloge déterministe répète la même date.

Les flux sont ajoutés en fin de fichier. Le manifeste est le document de
cycle de vie : ouvert avec `ended_at` et `end_reason` à `null`, puis finalisé
à la fermeture par remplacement atomique depuis un fichier temporaire voisin.
`checksums.sha256` n'est créé qu'après cette finalisation.
Une séance interrompue peut donc n'avoir que six entrées, sans checksums.

Rien d'autre n'entre dans le dossier. Sur le Pi, la confirmation d'un dépôt
hors de la machine est un fichier **à côté** du dossier
(`<nom du dossier>.deposit.json`), pas dedans : le dossier reste exactement
ces sept entrées et se relit sans avertissement, déposé ou non.

## Manifeste

Tous les champs suivants sont présents, y compris ceux dont la valeur est
`null`. La version de schéma inconnue est refusée par le reader.

| Champ | Type et signification |
| --- | --- |
| `schema_version` | entier, exactement `2` |
| `record_id` | identifiant opaque de l'enregistrement |
| `machine_id` | identifiant de la machine ; `simulation` pour les scénarios |
| `organization_id` | identifiant de l'organisation ; `synthetic` en simulation |
| `session_id` | identifiant de séance Convex, ou `null` si inconnu |
| `local_ref` | référence locale opaque, utilisable hors ligne |
| `kind` | `auto` ou `manual` |
| `occupancy` | `bench` ou `occupied` |
| `operator` | identifiant opaque d'opérateur, jamais son nom affiché |
| `subject_id` | pseudonyme du passager, ou `null` |
| `profile` | programme résolu figé, ou `null` pour une séance manuelle |
| `config_hash` | SHA-256 hexadécimal minuscule de la configuration appliquée |
| `software_version` | version fournie par l'appelant ; en simulation `ANHEART_SOFTWARE_VERSION`, sinon `unversioned` explicite |
| `contract_version` | version du contrat d'enregistrement, `2` actuellement |
| `medical_parameters_version` | version fournie par l'appelant ; SHA-256 du fichier de programmes livré en simulation |
| `clocks` | objet décrit ci-dessous |
| `started_at` | horodatage ISO 8601 UTC terminé par `Z` |
| `ended_at` | horodatage UTC `Z`, ou `null` avant fermeture |
| `end_reason` | motif de fin, ou `null` avant fermeture ; les valeurs écrites par la console du Pi, dont `interrupted` et `superseded`, sont dans [raspberry-pi.md](raspberry-pi.md#141-où-et-quand) |
| `preflight` | liste de `{check, passed}` pour les contrôles réellement faits, ou `null` |
| `geometry` | copie typée de la géométrie appliquée décrite ci-dessous, ou `null` si non observée |
| `end_observation` | observation finale typée décrite ci-dessous, ou `null` si absente |

`clocks.monotonic_start` est le point de départ sur l'horloge monotone injectée,
en secondes. `clocks.utc_start` est son ancrage UTC ISO 8601. La différence NTP
mesurée, en secondes, est `clocks.ntp_offset_s` ; `null` signifie inconnue,
jamais zéro présumé. L'heure UTC sert à la corrélation entre machines ; les
instants `t` des flux sont relatifs au début monotone. Des observations de
préparation antérieures au départ peuvent avoir un `t` négatif.

La copie `profile` conserve `total_duration_s`, `baseline_s`, `warmup_max_s`,
`hold_min_s`, `cooldown_s`, `recovery_s`, `zone_low_bpm`, `zone_high_bpm`,
`hard_max_bpm`, `critical_bpm`, `subject_hr_max`, `min_run_rpm`, `max_rpm`,
`warmup_rpm_ceiling_fraction`, `channels` dans l'ordre d'acquisition,
`allow_above_nameplate`, `source_rev`, `resolved_at` (millisecondes Unix) et
`total_overridden`. La durée HOLD se déduit des durées conservées, comme dans
`TrainingProfile`. Le nom affiché du profil et les descriptions libres sont
omis. Les seuils ne sont ni recalculés ni modifiés à l'export.

En simulation, le hash de configuration couvre le scénario résolu et les
valeurs de géométrie et limites utilisées par le harness. La simulation
déclare sa révision de profil synthétique ; elle n'invente pas une révision
de profil provenant d'une console réelle.

### Contexte commun aux deux producteurs

`geometry` et `end_observation` sont des champs du contrat partagé, pas des
détails d'événement spécifiques au simulateur. Ils sont optionnels à la
lecture (absence équivalente à `null`) et émis explicitement par le writer.
Le Pi comme la simulation peuvent les fournir sans changer d'API ou de viewer.

`GeometrySnapshot` contient `reference_radius_m`, `leg_tip_radius_m`,
`arm_tip_radius_m`, `capsule_near_radius_m`, `capsule_far_radius_m` et
`counterweight_radius_m` en mètres ; `leg_tip_measured` indique si ce rayon
est mesuré ; `gear_ratio` est le rapport de réduction, `nominal_motor_rpm`
la vitesse moteur nominale et `base_hz` la fréquence nominale. Le producteur
copie sa configuration réellement appliquée. La simulation copie le `RigGeometry`
résolu, y compris ses overrides. Le writer n'invente ni rayon, ni calibration.
La géométrie est écrite à l'ouverture : elle reste disponible après une
interruption sans fermeture, avec les warnings de complétude habituels.

`EndObservation.t` est l'instant monotone relatif de l'observation finale,
en secondes, et non une conversion de l'heure UTC de fermeture. Les autres
champs peuvent individuellement être `null` s'ils n'ont pas été observés :

| Champ | Provenance |
| --- | --- |
| `runtime_state` | état effectivement lu du runtime |
| `drive_state` | état effectivement observé du backend/variateur |
| `runtime_output_enabled` | autorisation de sortie lue dans le runtime |
| `runtime_applied_rpm` | demande moteur appliquée lue dans le runtime |
| `lfrd_motor_rpm` | référence tenue par le variateur, observée en tr/min moteur |
| `shaft_motor_rpm` | vitesse de l'arbre réellement lue, tr/min moteur |
| `energised` | présence de couple connue du producteur ; `null` si elle n'est pas connue |
| `silent` | état de silence du runtime observé |
| `stop_reason` | motif d'arrêt du runtime, éventuellement absent |
| `shutdown_detail` | diagnostic de fermeture nettoyé à la frontière de confidentialité |

Le producteur appelle `Writer.close(clock, end_reason, observation)` avec
cette valeur typée. Sans observation, la fermeture ne déduit rien du dernier
tic. La simulation copie son bilan réel après la fenêtre de teardown : état
du modèle, consigne tenue, lecture de vitesse par la sonde et état du runtime.
Un échec de cette sonde reste `shaft_motor_rpm: null`. La console du Pi
fournit les mêmes champs depuis ses propres observations : l'état du runtime,
l'état du variateur de son dernier instantané, la consigne appliquée, LFRD et
la vitesse de l'arbre du dernier statut lu **s'il est encore frais** (`null`
sinon), le motif d'arrêt. `energised` y reste `null` : la console n'observe
pas la présence de couple.

Sur la console, `geometry` vaut `null` : elle connaît son rayon de référence
et la pointe des pieds, pas les rayons de capsule et de contrepoids que
`GeometrySnapshot` exige, et elle n'en invente pas. Sa géométrie appliquée
entre dans `config_hash`. `operator` y est un alias opaque et stable dérivé du
nom saisi (`op-` suivi de 16 chiffres hexadécimaux), jamais ce nom.

Le viewer lit la même géométrie et le même état final neutre pour les deux
producteurs. Il avertit `missing_viewer_geometry` en l'absence de géométrie,
et `missing_final_drive_state` si l'état, la vitesse finale ou la présence de
couple ne sont pas connus. Il ne reconstitue pas ces données depuis le hash
de configuration ou le dernier tic ; les indicateurs inconnus restent inconnus.

## Tics, 5 Hz

`ticks.csv` a une ligne d'en-tête, puis une ligne par tic réellement observé.
Le writer n'invente pas les tics manquants. L'ordre est celui du type partagé
`src.record.rows.Row`, réexporté par `simulation.tracefile` :

```text
t,state,mode,phase,drive_state,sim_state,setpoint_motor_rpm,lfrd_motor_rpm,measured_motor_rpm,measured_fresh,output_rpm,hertz,g_reference,g_leg_tip,setpoint_output_rpm,setpoint_g_leg_tip,manual_target_motor_rpm,hr_true,hr_live,target_bpm,safety_action,safety_rule,output_enabled,silent,current_a,hr_raw,hr_confirmed,hr_quality,drive_status_word
```

| Colonnes | Sens et unité |
| --- | --- |
| `t` | secondes depuis le début ; précision exportée de 1 ms |
| `state`, `mode`, `phase` | état du runtime, mode et phase réellement observés |
| `drive_state`, `sim_state` | état du variateur ; état interne du modèle, seulement disponible en simulation |
| `setpoint_motor_rpm` | demande du runtime, tr/min moteur |
| `lfrd_motor_rpm` | référence tenue par le variateur, tr/min moteur |
| `measured_motor_rpm`, `measured_fresh` | vitesse mesurée et fraîcheur de cette observation |
| `output_rpm`, `hertz` | vitesse de sortie et fréquence électrique |
| `g_reference`, `g_leg_tip` | accélération centripète aux rayons de référence et des pieds |
| `setpoint_output_rpm`, `setpoint_g_leg_tip` | demande traduite en vitesse de sortie et accélération aux pieds |
| `manual_target_motor_rpm` | cible manuelle en tr/min moteur |
| `hr_true` | vérité du modèle physiologique ; `null` en données réelles |
| `hr_live`, `target_bpm` | fréquence affichable et cible du programme, bpm ou `null` |
| `safety_action`, `safety_rule` | action dominante et identifiant de règle, règle éventuellement `null` |
| `output_enabled`, `silent` | sortie autorisée et silence volontaire du runtime |
| `current_a` | courant moteur observé, ampères ou `null` |
| `hr_raw` | bpm de l'échantillon disponible, avant le filtre d'affichage/fraîcheur, ou `null` |
| `hr_confirmed` | bpm utilisable et frais effectivement observé, ou `null` |
| `hr_quality` | qualité de cet échantillon ; `no_signal` s'il manque |
| `drive_status_word` | ETA du dernier appel observé réussi, `null` après échec ou avant lecture |

Les champs numériques optionnels vides représentent `null`, jamais zéro.
Les champs textuels obligatoires gardent la chaîne vide ; en particulier
`sim_state` est vide pour un enregistrement sans modèle simulé.
Les booléens CSV sont `True` et `False`. Les valeurs physiques calculées sont
arrondies à quatre décimales, conformément au `Row` historique. Le statut
et les fréquences cardiaques ajoutés viennent des observations du runtime et
du wrapper variateur ; ils ne sont pas reconstruits depuis un état simulé.

## Événements

Chaque ligne complète de `events.jsonl` est un objet :

```json
{"t":12.4,"kind":"verdict_ack","detail":"acknowledged","actor":"operator-42"}
```

L'énumération fermée est `verdict`, `verdict_ack`, `refusal`, `phase`,
`operator_action`, `drive_fault`, `remote_command`, `preflight`, `warning`,
`end`. Un kind inconnu est une erreur de lecture. `actor` vaut `system`,
`remote` ou un identifiant opaque d'opérateur. `detail` est du texte nettoyé
à la frontière d'export ; son contenu libre n'est jamais traité comme HTML.

Le texte d'un événement n'est pas un canal de métadonnées. Le viewer ne
décode aucun préfixe réservé dans `detail` : géométrie et état final viennent
uniquement des champs typés communs du manifeste.

## Observations du variateur

Chaque ligne de `drive_frames.jsonl` contient
`{t, kind, register, value, ok, latency_ms}` et le champ optionnel `raw_hex`
(octets hexadécimaux, `null` pour les observations sans octets).

Sur le pilote natif, `RecordingDrive` active un `ExchangeLog` en mémoire.
Chaque appel Modbus effectif produit `modbus_read` ou `modbus_write`, avec
l'adresse réellement adressée (offset compris), la valeur brute 16 bits lue
ou demandée, le succès et la durée mesurée par l'horloge injectée. Une lecture
échouée garde `value: null`. Les succès avant un échec partiel restent présents,
ainsi que chaque nouvel essai et l'écriture d'urgence. Aucun registre absent
de la transaction n'est reconstruit depuis `DriveStatus`.

Le client SDK instrumenté conserve aussi chaque appel `send` et `recv` :
`modbus_send` porte dans `raw_hex` les octets que le SDK indique avoir acceptés ;
`modbus_receive_chunk` porte exactement les octets retournés par cet appel,
y compris `""` pour une lecture vide et chaque essai de lecture du corps.
Une exception avant le retour laisse `raw_hex: null`, jamais une transmission
présumée. Pour `send`, `ok` signifie que tous les octets demandés ont été
acceptés ; pour un fragment reçu, il signifie seulement qu'il est non vide,
pas qu'un CRC ou une réponse complète a été validé. `register` et `value`
restent `null` sur ces fragments : seul le décodeur SDK et le pilote donnent
les valeurs de l'opération `modbus_read`/`modbus_write`. On ne recopie pas de
parseur Modbus. Les lignes sont ajoutées à la fin de chaque appel ; `t` est
son instant de début et `latency_ms` sa durée, ce qui distingue les appels
SDK imbriqués de l'opération qui les contient.

Pour un port système tty/COM, construire le maître avec
`serial_master(settings, clock, exchange_log=ExchangeLog(clock))` active le
client instrumenté avant de l'envelopper. Le client système conserve toujours
le verrou exclusif du câble, avec ou sans journal. Sans journal, aucune capture
des échanges SDK n'est activée. Les clients système et FTDI activent aussi ce
point d'observation quand le pilote reçoit le journal. Le journal peut être
fourni directement au pilote par `observe_exchanges(log)` : aucune importation de simulation
n'est nécessaire. Le consommateur remet les champs des `Exchange` au writer
partagé. **La console du Pi ne le fait pas encore** : son `drive_frames.jsonl`
existe et reste vide. Le journal des échanges n'a ni borne ni vidage, et le
brancher tel quel ferait grossir la mémoire de la console pendant toute une
séance (ticket de suite proposé avec ANH-128). Ce journal en mémoire n'est pas
une garantie de capture physique ni une preuve du comportement d'un câble réel.

Le point existant de simulation, `RecordingDrive`, est un wrapper de
`DriveBackend`, pas une capture du fil Modbus RTU. Il conserve chaque appel
`open`, `close`, `command`, `speed`, `emergency_zero`, `read_status`,
`read_failed`, `read_limits`, avec l'instant de début et la durée mesurés par
l'horloge injectée. Les commandes/speeds portent CMD/LFRD et leur valeur
effective. Les ouvertures, fermetures et échecs sans réponse ont des valeurs
inconnues à `null`.

Un succès `read_status` ou `read_limits` est **une observation groupée** :
`register` et `value` sont des tableaux alignés. ETA, LFRD, RFRD, LCR et LFT
proviennent du résultat `DriveStatus` ; les paramètres tFr/HSP/LSP/ACC/dEC
proviennent de `DriveLimits`. Les valeurs décodées en ampères, hertz ou
secondes sont reconverties dans l'échelle logique des registres. `latency_ms`
est la durée de cet appel groupé, pas celle de transactions physiques
individuelles. Avec l'horloge déterministe, un appel qui ne fait pas avancer
l'horloge dure zéro ; aucun délai fictif n'est ajouté.

Cette observation groupée concerne uniquement les backends sans transactions
natives (dont le modèle simulé). Elle ne sert jamais à fabriquer les trames
individuelles du pilote natif.

## Échantillons bruts

Chaque bloc `ecg_raw/NNNNNN.bin.gz` se décompresse en un objet JSON UTF-8,
un octet LF (`0a`), puis le binaire :

```json
{"seq":2,"t_first":0.4,"n_samples":200,"channels":["ECG","EDA"],"sample_rate":1000}
```

Les champs sont : numéro de bloc `seq`, premier instant relatif `t_first`
en secondes, nombre d'échantillons **par canal** `n_samples`, noms de canaux
dans l'ordre, et fréquence `sample_rate`, exactement 1000 Hz. Le binaire est
en entiers signés 16 bits little-endian, **canal par canal** : tous les
échantillons du premier canal, puis tous ceux du deuxième, etc. Sa taille
est exactement `2 × n_samples × nombre_de_canaux` octets. Il n'y a aucun
padding ni octet final de séparation. Le writer ne remplace jamais un bloc.

Les noms de voies autorisés sont ECG, EDA, SpO2, RESP, EMG et LUX. Les limites
du contenant int16 sont vérifiées ; elles ne changent pas la résolution du
convertisseur réel. Un bloc 1 absent entre 0 et 2 reste absent. Le reader ne
comble ni ne rééchantillonne les trous. Un dernier gzip interrompu est ignoré
avec un avertissement. Une corruption complète est une erreur.

Sur la console du Pi, un bloc est un lot d'acquisition tel que reçu (200
échantillons par voie à la cadence normale, davantage après un retard) et
`seq` est un compteur de lots par séance, pris à la réception : un trou dans
la numérotation est un lot que l'enregistreur a dû refuser (file pleine). Ce
que le BITalino lui-même a perdu est dit par un événement `warning`
(`bitalino_loss:`, avec les compteurs de la liaison), et se voit dans les
`t_first`. Le premier bloc d'une séance peut commencer un peu avant `t = 0`.

Dans les scénarios DSP, le tap placé avant le traitement capture tous les
canaux acquis. La perturbation secteur synthétique est quantifiée au point
de production des comptes ADC ; le DSP et le fichier reçoivent donc les
mêmes valeurs entières. Le recorder refuse une valeur fractionnaire plutôt
que de fabriquer une autre version des données. Les scénarios `direct`
injectent des bpm, pas une acquisition ADC : leur `ecg_raw/` est vide.

## Indicateurs des capteurs, 1 Hz

`sensors.csv` contient `t,channel,quality,metric,value,unit`. Chaque seconde,
une ligne par métrique de chaque voie configurée est écrite depuis les
`SensorReading` publiés par `SensorHub`. Une voie sans métrique garde une
ligne avec `metric`, `value` et `unit` vides. Une métrique indisponible a une
valeur vide, distincte de zéro. La waveform décimée et les libellés destinés
à l'affichage ne sont pas exportés. L'horloge injectée détermine la cadence ;
la tolérance de comparaison est 1 ns pour les additions flottantes de tics.

## Fermeture, intégrité et lecture interrompue

`checksums.sha256` est créé à la fermeture : une ligne
`<64 chiffres hexadécimaux>  <chemin relatif>`, compatible avec `sha256sum`.
Chaque fichier présent, y compris chaque bloc gzip et le manifeste final,
est couvert ; le fichier des checksums ne se référence pas lui-même.

`read(path)` retourne `Ok(Recording)` ou `Err(RecordError)`. Les warnings
structurés sont `missing_checksums`, `truncated`, `checksum_mismatch` et
`missing_file`. Une absence de checksums ne prouve pas la complétude. Une
différence de hash n'est pas cachée, même si les lignes restantes se lisent.
Les flux UTF-8 sont lus jusqu'au dernier LF complet ; une dernière ligne
incomplète, y compris coupée au milieu d'un caractère UTF-8, est ignorée.
Une ligne complète invalide, un schéma inconnu ou un nombre non fini est
refusé. Il n'y a ni récupération silencieuse de données corrompues ni NaN,
Infinity ou -Infinity numérique dans les exports.

Le writer retourne les erreurs d'E/S par `Result`, nommées par leur classe et
leur code (`OSError:ENOSPC`), jamais par un chemin. Il n'a ni file ni fil : il
doit être appelé par un propriétaire unique, hors de toute boucle de contrôle.
Rien n'est poussé vers le disque tant que `Writer.sync()` n'est pas appelé
(`fsync` de chaque fichier écrit depuis le dernier appel, et de leurs
dossiers). Un ajout que le disque refuse en cours de ligne est retiré (retour
à la dernière ligne complète), pour qu'une demi-ligne ne se retrouve jamais au
milieu d'un fichier ; si même ce retrait est refusé, le flux est abandonné et
sa fin tronquée se lit comme une troncature ordinaire. Sur la console, le
propriétaire unique est le fil du journal de séance
([raspberry-pi.md](raspberry-pi.md#14-lenregistrement-de-séance-boîte-noire-locale)).

## Frontière de confidentialité

Le programme figé exclut son nom libre. Le manifeste ne reçoit ni nom de
scénario, ni `description`, `tags` ou `profile_id` libres ; l'export historique
de simulation omet également ces trois derniers champs. Les chemins ne proviennent jamais du
nom du scénario, du passager ou du profil. Les noms de voies brutes sont
fermés. Aucun objet « passager » avec nom/e-mail ne fait partie du schéma.

L'appelant doit fournir à `Privacy(names=(...))` tous les noms et alias
identifiants qu'il connaît, et n'utiliser que des identifiants opaques dans
les champs d'identité. Ces chaînes sont remplacées sans distinction de
casse avant sérialisation ; les adresses e-mail sont retirées également.
Un identifiant qui nécessite ce nettoyage est refusé, pas remplacé dans un
chemin. Les détails, versions et autres chaînes libres doivent déjà être
des données machine sans identité inconnue. Le recorder ne reconnaît pas
magiquement un nom humain arbitraire : le test sentinelle prouve le respect
de cette frontière explicite, pas une détection générale de personnes.

## Utilisation et compatibilité

Depuis la racine, avec l'environnement Python existant :

```bash
PYTHONPATH=.:raspberry-pi raspberry-pi/.venv/bin/python -m simulation.run manual_32_rpm_refused
PYTHONPATH=.:raspberry-pi raspberry-pi/.venv/bin/python -m simulation.live --port 8765
```

Ouvrir `/?trace=out/<dossier>` pour le schéma 2. Le serveur utilise le reader
partagé et le viewer affiche les avertissements de complétude dans sa zone
source existante. `/?trace=out/ancienne-trace.jsonl` garde la lecture du
schéma 1. `Trace.write_jsonl()` reste un export de compatibilité explicite ;
la CLI écrit par défaut les dossiers v2. `--csv` conserve l'export CSV
supplémentaire demandé par l'utilisateur, à côté des dossiers.

## Ce que le format ne contient pas

Pas de nom de passager, d'adresse e-mail, de nom libre de profil, de vidéo,
d'image de présence, de clé machine, de jeton ou de mot de passe. Pas de
consigne interpolée, de battement synthétisé pour masquer une perte, ni de
vérité physiologique réelle inventée. Pas de preuve de chiffrement au repos,
de consentement ou de conformité réglementaire. Pas de dépôt Storage,
synchronisation, rejeu ni capture RTU native : ces fonctionnalités relèvent
des tickets suivants. Le branchement de la console réelle et la rétention
locale existent (ANH-128) et sont décrits dans
[raspberry-pi.md](raspberry-pi.md#14-lenregistrement-de-séance-boîte-noire-locale).

La relecture indépendante de ce document et du SHA final reste une étape
d'acceptation du ticket ; ce document ne vaut pas signature de reviewer.

# Enregistrement de séance, schéma 2

Le format d'ANH-127 est commun au Pi et à la simulation. La bibliothèque
`raspberry-pi/src/record/` définit les valeurs typées, le writer, le reader et
la validation. La simulation appelle cette bibliothèque, et la console du Pi
aussi depuis ANH-128 : elle écrit un dossier par séance sous `data/records/`,
par une file bornée et un fil d'écriture à part, sans jamais écrire dans la
boucle de contrôle. Ce branchement (file, cadence de `fsync`, comportement en
cas d'erreur, refus de départ, rétention, export) est décrit dans
[raspberry-pi.md](raspberry-pi.md#15-lenregistrement-de-séance-boîte-noire-locale).
Ce document décrit le format et son dépôt vérifié dans Convex (ANH-130).

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
(`<nom du dossier>.deposit.json`), pas dedans, et le curseur de la
synchronisation avec le tableau de bord aussi (`<nom du dossier>.sync.json`,
avec, une fois pour tout le répertoire, `.sync-baseline.json` ;
[raspberry-pi.md](raspberry-pi.md#81-ce-qui-est-envoyé-vient-du-disque)) : le
dossier reste exactement ces sept entrées et se relit sans avertissement,
déposé ou non, synchronisé ou non. Un fichier
ajouté dans un dossier fermé serait signalé à la lecture
(`checksum_mismatch`) : ce qui arrive à la console après la fermeture va dans
le journal hors séance, un dossier `logbook/` à côté des enregistrements
([raspberry-pi.md](raspberry-pi.md#159-le-journal-hors-séance)), jamais dans
un enregistrement.

Le writer crée chaque dossier en mode 700 et chaque fichier en mode 600, pour
le Pi comme pour la simulation : le mode est donné à l'appel qui crée le
fichier (`create_private` et `write_file`, `src/record/writer.py`), y compris
pour le manifeste final et les sommes de contrôle. Qui pose un fichier à côté
d'un dossier (un marqueur de dépôt, un curseur de synchronisation, la liste
`.sync-baseline.json`) le crée de la même façon.

**Télémétrie synchronisée.** De tout enregistrement fait depuis le
premier démarrage de la console avec un tableau de bord configuré sur ce
répertoire : sa télémétrie à 1 Hz, ses événements et sa fin, et rien d'autre
(ni les trames du variateur, ni les blocs ECG, ni `sensors.csv`, ni rien du
journal hors séance `logbook/`). Cela vaut
aussi pour un enregistrement fait **pendant qu'aucun tableau de bord n'était
configuré**, s'il est postérieur à ce premier démarrage : il part au premier
démarrage où une clé est de nouveau en place, et aucun réglage ne l'en
empêche (décision du chef de projet du 14 octobre 2026). Les enregistrements
antérieurs à ce premier démarrage ne partent jamais. Le détail, et le seul
moyen, manuel, de garder locaux les seconds, sont dans
[raspberry-pi.md §8.4](raspberry-pi.md#84-après-un-redémarrage-de-la-console).

**Fichiers complets.** Après acquittement de la fin de séance, les nouveaux
enregistrements clos munis de l'inventaire `files` sont aussi déposés dans
Convex Storage : manifeste, traces variateur, capteurs et blocs bruts compris.
Le journal hors séance reste local. Voir [Dépôt vérifié](#dépôt-vérifié-dans-convex).

## Manifeste

Tous les champs suivants sont présents, y compris ceux dont la valeur est
`null`. La version de schéma inconnue est refusée par le reader.

| Champ | Type et signification |
| --- | --- |
| `schema_version` | entier, exactement `2` |
| `files` | inventaire des chemins relatifs attendus, fixé à la clôture ; facultatif pour relire les anciens schémas 2, obligatoire pour un nouveau dépôt complet |
| `record_id` | identifiant opaque de l'enregistrement |
| `machine_id` | identifiant de la machine ; `simulation` pour les scénarios |
| `organization_id` | identifiant de l'organisation ; `synthetic` en simulation |
| `session_id` | identifiant de séance Convex, ou `null` si inconnu |
| `local_ref` | référence locale opaque, utilisable hors ligne |
| `kind` | `auto` ou `manual` |
| `occupancy` | `bench` ou `occupied` |
| `operator` | identifiant opaque d'opérateur, jamais son nom affiché |
| `subject_id` | pseudonyme du passager : son identifiant Convex (`userId`), celui du lancement venu du tableau de bord ou du passager choisi dans la liste de la machine pour une séance lancée à la console ; `null` quand personne n'est nommé (séance de banc). Jamais son nom, qui n'est écrit nulle part dans l'enregistrement |
| `profile` | programme résolu figé, ou `null` pour une séance manuelle |
| `config_hash` | SHA-256 hexadécimal minuscule de la configuration appliquée |
| `software_version` | version du logiciel qui a écrit l'enregistrement : le contenu de `raspberry-pi/VERSION`, pour la console du Pi comme pour les scénarios de la simulation (`pi-unknown` si le fichier manque ou est mal formé). Aucune variable d'environnement ne la règle. Les enregistrements exportés dans la bibliothèque de la simulation portent `unversioned`, exprès, pour rester reproductibles d'une version à l'autre |
| `contract_version` | version du contrat d'enregistrement, `2` actuellement |
| `medical_parameters_version` | version fournie par l'appelant. La console du Pi y écrit la version de son fichier médical signé (`config/medical_parameters.json`, `0.1.0` aujourd'hui), ou `unsigned-sha256:` suivi de l'empreinte du fichier quand sa signature ne s'est pas vérifiée au démarrage, l'enregistrement s'ouvrant alors sur un événement `warning` (ANH-139). La simulation y écrit le SHA-256 du fichier de programmes livré |
| `clocks` | objet décrit ci-dessous |
| `started_at` | horodatage ISO 8601 UTC terminé par `Z` |
| `ended_at` | horodatage UTC `Z`, ou `null` avant fermeture |
| `end_reason` | motif de fin, ou `null` avant fermeture ; les valeurs écrites par la console du Pi, dont `interrupted` et `superseded`, sont dans [raspberry-pi.md](raspberry-pi.md#151-où-et-quand) |
| `preflight` | liste de `{check, passed}` pour les contrôles réellement faits, ou `null` |
| `geometry` | copie typée de la géométrie appliquée décrite ci-dessous, ou `null` si non observée |
| `end_observation` | observation finale typée décrite ci-dessous, ou `null` si absente |

`clocks.monotonic_start` est le point de départ sur l'horloge monotone injectée,
en secondes. `clocks.utc_start` est son ancrage UTC ISO 8601. La différence NTP
mesurée, en secondes, est `clocks.ntp_offset_s` ; `null` signifie inconnue,
jamais zéro présumé. L'heure UTC sert à la corrélation entre machines ; les
instants `t` des flux sont relatifs au début monotone. Des observations de
préparation antérieures au départ peuvent avoir un `t` négatif.

`clocks.wall_trusted` dit ce que valait l'horloge murale qui a donné
`utc_start`, à l'ouverture de l'enregistrement :

| Valeur | Sens |
| --- | --- |
| `true` | un service de temps tenait cette horloge : `utc_start` et `started_at` peuvent être crus |
| `false` | personne n'en répondait (aucune source de temps jointe depuis l'allumage de la machine, ou état illisible) : ces dates peuvent être fausses de toute la durée pendant laquelle la machine était éteinte. Les instants `t`, eux, restent justes : ils sont monotones |
| `null`, ou champ absent | le producteur n'a pas jugé son horloge : la simulation (son horloge est déterministe), et tout enregistrement écrit avant ce champ |

Le champ suit la règle des champs ajoutés au schéma 2 : optionnel à la lecture
(son absence vaut `null`, un enregistrement plus ancien se relit et se rejoue
comme avant), émis explicitement par le writer. La version du schéma ne change
pas. Dans l'autre sens, un lecteur d'une version antérieure refuse un
manifeste qui le porte, comme il refuse toute clé qu'il ne connaît pas : après
un retour à une version antérieure de la console, les enregistrements écrits
par la plus récente ne se relisent qu'avec elle. La console écrit `true` ou
`false` ([raspberry-pi.md](raspberry-pi.md#124-lhorloge-murale-et-le-journal-de-la-console)) ;
elle laisse `ntp_offset_s` à `null`, faute de connaître l'écart mesuré.

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
Le writer n'invente pas les tics manquants. Les tics de la console au repos
qui précèdent le départ ont un `t` négatif ou nul : `t = 0` est l'instant de
la commande de départ, et un tic horodaté 0 est le dernier tic d'avant le
départ. Les tics de la séance ont un `t` strictement positif. La simulation
écrit les tics de sa période de repos (15 s par défaut) ; le viewer n'affiche
que la séance. L'ordre des colonnes est celui du type partagé
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
{"t":12.4,"kind":"verdict_ack","detail":"acknowledge estop_released=true","actor":"operator-42"}
```

L'énumération fermée est `verdict`, `verdict_ack`, `refusal`, `phase`,
`operator_action`, `drive_fault`, `remote_command`, `preflight`, `warning`,
`end`. Un kind inconnu est une erreur de lecture. `actor` vaut `system`,
`remote` ou un identifiant opaque d'opérateur. `detail` est du texte nettoyé
à la frontière d'export ; son contenu libre n'est jamais traité comme HTML.

Le texte d'un événement n'est pas un canal de métadonnées. Le viewer ne
décode aucun préfixe réservé dans `detail` : géométrie et état final viennent
uniquement des champs typés communs du manifeste.

### Commandes : les trois kinds qui sont des entrées du runtime

`operator_action`, `remote_command` et `verdict_ack` ne décrivent pas ce que
le runtime a fait : ils disent ce qu'on lui a demandé. Pour qu'un rejeu puisse
redonner la même demande, leur `detail` est une commande d'un vocabulaire
fermé, écrite par `encode` et relue par `parse` dans
`raspberry-pi/src/record/commands.py`, à côté du writer, pour que la console
et la simulation écrivent la même chose :

| `detail` | Appel du runtime rejoué |
| --- | --- |
| `confirm_estop_wiring` | `confirm_estop_wiring(actor)` |
| `start_programme` | `start(programme du manifeste, sujet)` |
| `start_manual ceiling_motor_rpm=1380` | `start_manual(occupation du manifeste, actor, plafond)` |
| `manual_target output_rpm=27.0` | `set_manual_target(27.0)` |
| `stop` | `request_stop(...)` (bouton, ou fin demandée à distance : kind `remote_command`) |
| `estop` | `request_estop(...)` |
| `acknowledge estop_released=true` | `acknowledge(actor, estop_released=True)` (kind `verdict_ack`) |
| `fault_reset` | `fault_reset()` |
| `shutdown` | `shutdown(...)` : la console quitte (signal, tâche en échec, fin d'une simulation) |
| `attendant present=true` | tant que `true`, chaque tic note la présence de l'accompagnant |

Qui a demandé est dans `actor`. Ce que le runtime a répondu n'est pas dans la
commande : un refus est son propre événement `refusal`, et les décisions sont
dans `ticks.csv`. Le plafond d'une séance manuelle est écrit dans la commande
parce que le manifeste ne le porte pas. `t` est l'horloge monotone que le
runtime lit en traitant la commande, **non arrondie**.

Tout autre texte sous l'un de ces trois kinds rend l'enregistrement non
rejouable, et le rejeu le dit : une commande ignorée en silence donnerait le
rejeu d'une autre séance. Ce que la simulation fait au modèle (défaut
variateur injecté, perte de liaison, électrode décollée) n'est pas une commande
au runtime : c'est un événement `warning` d'acteur `system`, préfixé
`simulation:`. Il en va de même de ce que la console dit d'elle-même sous les
autres kinds (pertes du BITalino, nouvelles du lien avec le tableau de bord
préfixées `dashboard:`, refus, défauts) : le rejeu ne lit comme commandes que
les trois kinds d'entrée, et jamais un autre événement, quel que soit son texte.

Le format n'a qu'un kind `warning` ; ce dont il parle se lit au préfixe de
son `detail` :

| Préfixe | Qui l'écrit, et ce qu'il dit |
| --- | --- |
| `bitalino_loss:` | la console : la liaison d'acquisition a perdu ou comblé des échantillons (compteurs de la liaison) |
| `dashboard:` | la console : une nouvelle du lien avec le tableau de bord montrée à l'opérateur, par exemple `serveur incompatible (contrat 1.0 vs 2.0)` |
| `record_degraded:` | le fil du journal de séance : ce qui manque à cet enregistrement, `dropped=<n> failures=<n>`, `drive_frames_lost=<n>` s'il manque des trames du variateur (ce que ce nombre compte : [Observations du variateur](#observations-du-variateur)), puis `last=<opération>:<erreur>`. Une ligne par changement de ces comptes : la dernière de l'enregistrement fait foi, et elle est écrite avant sa clôture, y compris quand il est clos `superseded` |
| `simulation:` | la simulation : ce qu'elle a fait au modèle |

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
partagé. Ce journal en mémoire n'est pas une garantie de capture physique ni
une preuve du comportement d'un câble réel.

**La console du Pi le fait depuis ANH-191**, hors de la boucle de contrôle.
Le journal des échanges accepte une borne (`ExchangeLog(clock, capacity)`) :
au-delà, l'échange le plus récent est refusé et compté, et `drain()` rend ce
qui attend avec ce compte. Sans borne (l'usage de la simulation), rien ne
change. Sur le pilote natif, la console lui donne un journal borné et le fil
du journal de séance le vide à chaque cycle. Elle n'y garde par défaut que les
transactions de registre (`modbus_read`, `modbus_write`) : les appels du SDK
(`modbus_send`, `modbus_receive_chunk`) ne sont écrits que si
`RECORD_DRIVE_SDK_FRAMES=true`, un diagnostic de banc qui multiplie les lignes
par quatre (`ExchangeLog(clock, capacity, transport=False)` les écarte à
l'entrée, sans qu'elles prennent de place sous la borne). Sur un variateur qui ne rapporte
rien (le simulateur), elle l'enveloppe d'une prise qui note chaque appel, avec
le vocabulaire et les registres des observations d'appel décrites ci-dessous
(`src/record/drive_tap.py`,
[raspberry-pi.md](raspberry-pi.md#152-aucune-écriture-dans-la-boucle)). Un
appel qui lève une exception au lieu de répondre y est noté au moment où il
lève, comme un appel échoué (`ok: false`, valeurs inconnues à `null` ; une
lecture d'état est alors `read_failed`). Les
trames perdues faute de place (la prise pleine parce que personne ne venait,
ou trop de trames en attente d'un enregistrement) sont comptées dans
l'événement `warning` `record_degraded:` (`drive_frames_lost=<n>`) : combien
de trames du variateur manquent à cet enregistrement.

- Pour les trames que la prise a refusées, ce nombre est **exact si les deux
  lectures de frontière réussissent** : la
  console lit le compte des refus au début de l'armement et à la fin de la
  séance, et l'enregistrement compte la différence. Ce qu'une console au repos
  s'est vu refuser avant ou après la séance n'y est pas.
- Il reste un **majorant**, jamais inférieur à la perte réelle, dans trois
  cas : les trames que le fil du journal a lâchées lui-même faute de place ;
  et tout refus de la prise quand son compte n'a pas pu être lu au début de
  l'armement. Ces trames sont comptées pour chaque séance dont l'intervalle
  recouvre celui où elles ont pu commencer, donc par deux enregistrements
  quand deux séances le recouvrent.
- Si seul le compte de fin manque, le fil soustrait le compte d'armement du
  dernier compte qu'il a lu en vidant la prise pour cette fermeture. Cette
  lecture arrive après la fin : elle peut inclure des refus au repos après
  la séance. C'est donc aussi un majorant, mais calculé par différence de
  compteurs, sans attribution par recouvrement d'intervalles.

La ligne ne dit pas elle-même lequel des deux comptes a servi. Sur la console,
le premier est le seul attendu : le second n'a été obtenu qu'en test (une
borne réduite, une lecture du compte rendue fautive), et une lecture du compte
qui échoue est écrite dans le journal de la console. Le format ne change pas :
c'est le même texte dans le `detail` d'un événement `warning`, qu'un lecteur
plus ancien lit comme avant, et le nombre n'est jamais plus grand que celui
que la console écrivait jusque-là pour la même séance. Les lignes sont celles de la séance, du début de
son armement à sa fin : ce que la console demande au variateur hors séance
(scrutation au repos, consigne nulle après une séance, départ refusé,
réarmement de défaut) n'y est pas, ni avant ni après, même quand cela
attendait avec les trames de la séance que le fil du journal vienne les
prendre. Les échanges de l'armement précèdent l'ouverture de l'enregistrement
(leur `t` est négatif) : ils y sont tous, parce que le fil du journal garde ce
qu'il prend pendant un armement jusqu'à l'ouverture de l'enregistrement
([raspberry-pi.md](raspberry-pi.md#158-ce-qui-nest-pas-enregistré-aujourdhui)).

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
dans l'ordre, et fréquence `sample_rate`, exactement 1000 Hz. Un champ
optionnel `t_received` donne l'instant relatif, en secondes, auquel
l'acquisition a remis ce lot au traitement. Ce n'est pas `t_first` plus la
durée du bloc : `t_first` est l'heure de l'horloge d'échantillonnage, et un
lot peut être lu un tic plus tard que la fin de ses échantillons. Un
producteur qui ne connaît pas cet instant omet le champ, et l'en-tête reste
alors, octet pour octet, celui de l'exemple ci-dessus. Le binaire est
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
canaux acquis, avec `t_received`. Les blocs acquis pendant le repos qui précède
le départ sont conservés, avec des instants négatifs : la fenêtre du DSP en est
pleine quand la séance commence. La perturbation secteur synthétique est quantifiée au point
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
([raspberry-pi.md](raspberry-pi.md#15-lenregistrement-de-séance-boîte-noire-locale)).

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

## Ce qu'un enregistrement doit contenir pour être rejoué

`python -m simulation.run --replay <dossier>` redonne un enregistrement à un
vrai `TrainingRuntime` neuf et compare ce qu'il décide à ce qui a été
enregistré (voir [framework-de-test.md](framework-de-test.md#16-rejouer-une-séance-enregistrée)).
Le runtime décide à partir de quatre choses, et le rejeu ne peut redonner que
ce que l'enregistrement contient :

| Entrée du runtime | Où elle est | Ce qu'il faut |
| --- | --- | --- |
| l'horloge de chaque tic | `ticks.csv`, colonne `t` | tous les tics, y compris ceux du repos avant le départ (`t ≤ 0`) : la scrutation du variateur au repos et la fenêtre du DSP font partie de l'état au départ |
| les réponses du variateur | `drive_frames.jsonl` | les observations d'appel (`open`, `close`, `command`, `speed`, `emergency_zero`, `read_status`, `read_failed`, `read_limits`), depuis le premier tic. Un fichier vide est refusé : rien ne dit ce que le variateur a répondu |
| l'ECG | `ecg_raw/` | les blocs bruts depuis au moins 8 s avant le départ (la fenêtre du DSP), chacun avec `t_received` |
| les demandes | `events.jsonl` | les commandes du tableau ci-dessus, pour les trois kinds d'entrée |

Il faut aussi la géométrie du manifeste (`geometry`) : le runtime rejoué est
construit avec elle. Quand il manque plusieurs de ces choses, le refus les
donne ensemble, séparées par « ; ».

**L'enregistrement que la console du Pi écrit aujourd'hui est refusé**, pour
deux raisons que le rejeu énonce : ses événements d'entrée sont du texte
(`manual session started`, `end_requested: done`) et non des commandes du
tableau ci-dessus ; son manifeste n'a pas de géométrie. Son
`drive_frames.jsonl` n'est plus vide (ANH-191) : en simulation il porte les
observations d'appel, depuis l'ouverture de l'enregistrement. Il n'a pas les
tics du repos (son premier tic est à `t = 0`), donc pas non plus les réponses
du variateur à ces tics, ni `t_received` sur ses blocs ; et sur le pilote
natif ses trames sont des échanges Modbus, que le rejeu ne sait pas encore
redonner. Même structure ne veut donc pas encore dire rejouable :
`test_record_console_parity.py` vérifie les deux, et que la console et la
simulation écrivent les mêmes observations d'appel.

À instant égal (à la milliseconde), l'ordre rejoué est : avant et jusqu'au
départ (`t ≤ 0`), le tic puis les commandes ; pendant la séance (`t > 0`), les
commandes puis le tic, comme la console vide sa boîte de commandes avant de
tiquer ; `shutdown` vient toujours après le tic de son instant.

L'horloge rejouée part de l'instant de la première entrée, tel qu'il est
écrit, et avance par les intervalles enregistrés, arrondis à la milliseconde.
Un enregistrement produit sur une horloge déterministe (la simulation) est
ainsi rejoué sur les mêmes nombres flottants, et son rejeu est exact. Les
instants d'une séance réelle sont connus à la milliseconde : une décision
prise à moins d'une milliseconde d'un seuil peut tomber un tic plus tôt ou
plus tard au rejeu.

Ce qu'un rejeu ne peut pas redonner aujourd'hui, parce que le format ou le
producteur ne le porte pas :

- les échanges Modbus natifs (`modbus_read`, `modbus_write`, `modbus_send`,
  `modbus_receive_chunk`) : il faudrait rejouer un maître Modbus sous le vrai
  pilote ATV320. Un enregistrement qui en contient est refusé, pas rejoué à
  moitié ;
- la nature d'un échange en échec : chaque échec est rejoué comme une absence
  de réponse. Le runtime ne distingue que deux variantes (étage de sortie
  peut-être sous tension), que seul le pilote natif produit ;
- le courant moteur à mieux que 0,1 A, résolution du registre dans lequel
  l'observation est écrite ;
- une fréquence cardiaque qui n'est pas passée par l'acquisition : le mode
  `direct` de la batterie injecte des bpm sans ECG brut, et ses enregistrements
  sont refusés au rejeu ;
- une exception levée dans le tic d'origine, un saut de l'horloge murale
  pendant l'acquisition, une perte signalée par le lien d'acquisition sans
  trou dans les horodatages.

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
de consentement ou de conformité réglementaire. Le dépôt Storage est décrit
ci-dessous ; la synchronisation du tableau de bord et la capture RTU native
sont décrites dans les documents de la console. Le branchement de la console réelle et la rétention
locale existent (ANH-128) et sont décrits dans
[raspberry-pi.md](raspberry-pi.md#15-lenregistrement-de-séance-boîte-noire-locale).

La relecture indépendante de ce document et du SHA final reste une étape
d'acceptation du ticket ; ce document ne vaut pas signature de reviewer.

## Dépôt vérifié dans Convex

Le writer clôt le manifeste avec l'inventaire des fichiers avant de calculer
`checksums.sha256`. Les anciens enregistrements sans inventaire restent lisibles
et conservés localement ; ils ne sont pas déclarés déposés par extrapolation.

`RecordDeposit` travaille seulement hors séance, après acquittement de la fin
par le serveur. Le démarrage d'une séance annule son travail réseau. Ses lectures
et mises à jour de curseur passent par un worker disque dédié, jamais par le
cycle moteur. Il envoie le manifeste en premier et attend sa vérification.
Chaque fichier suivant reçoit une réservation liée à la machine, à la séance,
au chemin, à la taille et au SHA-256 ; le POST natif Storage utilise seulement
l'URL signée de ce déploiement et son type de contenu réservé, sans clé machine.

Quatre transferts au plus tournent ensemble, chacun cadencé à 256 KiB/s, par
lots de 64 fichiers. Une erreur déclenche un recul de 2 à 300 secondes. Le
curseur adjacent `.sync.json` garde les reçus non acquittés ; un redémarrage
réutilise ces reçus et le statut serveur. Le balayage serveur toutes les cinq
minutes retrouve aussi un fichier envoyé dont la réponse a été perdue, et
retire ses doublons. Il ne traite que les objets portant une réservation connue.

Un enregistrement local illisible ou en échec ne bloque pas les suivants :
la sélection continue après lui et réessaie ensuite. Une suppression cloud
est conservée dans le curseur comme révocation, y compris après redémarrage ;
les octets locaux restent présents et aucun marqueur de dépôt n'est créé.
La réservation signale un fichier rejeté même s'il n'est pas dans la page de
statut courante, afin de remplacer son ancien reçu Storage supprimé.

Limites : 40 000 fichiers, 16 MiB par fichier, 2 MiB pour le manifeste et
1 MiB pour son inventaire JSON. Cela couvre les quelque 36 000 blocs bruts
d'un programme de deux heures ; la séance manuelle reste limitée à une heure.
Les chemins sont relatifs, ASCII, sans `..`,
et longs de 160 caractères au plus. Le serveur conserve l'inventaire par pages
de 1 024 chemins au plus ; les lectures de fichiers retournent 256 lignes au
plus. Ces bornes permettent les nombreux petits blocs bruts sans dépasser la
limite native des tableaux Convex. Un fichier hors limites reste sur le Pi.

Les blocs bruts sont refusés avant allocation au-delà de 1 MiB décodé
(`MAX_RAW_BLOCK_BYTES` dans `src/record/ecg.py`). La borne fichier de 16 MiB
reste inférieure aux 100 MiB de réponse HTTP et ménage la mémoire de l'action
de vérification (64 MiB). Références consultées le 13 octobre 2026 :
[limites Convex](https://docs.convex.dev/production/state/limits) et
[dépôt natif des fichiers](https://docs.convex.dev/file-storage/upload-files).
Le POST natif doit finir dans les deux minutes ; à 256 KiB/s, un fichier de
16 MiB prend au minimum 64 secondes. Une liaison plus lente peut faire échouer
ce délai et déclencher une reprise, sans effacer la copie locale.

Convex vérifie la taille et le SHA-256 des octets déposés. Une séance passe de
`none` à `partial`, puis `complete` seulement lorsque chaque fichier de
l'inventaire est vérifié ; un rejet la place en `rejected` jusqu'à une reprise
valide. Le Pi relit toutes les pages du statut `complete`, compare toutes les
empreintes avec ses fichiers locaux et écrit alors le marqueur adjacent
`.deposit.json`, lié à `checksums.sha256`. La purge locale existante reste
subordonnée à ce marqueur et à son délai ; un simple succès HTTP ne l'autorise
jamais. Après suppression du dossier, la même purge retire son marqueur et
son curseur `.sync.json` ; un refus de suppression est retenté à la purge
suivante. Les curseurs des dossiers conservés et les écritures temporaires
en cours restent intacts. Des sommes de contrôle réécrites par un compte local compromis ne
constituent pas une preuve d'authenticité de la séance.

Le téléchargement passe par `/api/records/download?fileId=…` avec un JWT Clerk
et une autorisation vérifiée à chaque lecture. Aucun `storageId`, jeton ou URL
publique n'est transmis dans les métadonnées de la carte. L'archive `.tar` est
assemblée par le navigateur à partir des fichiers autorisés et revérifiés.
Le visualiseur reçoit les données du manifeste, des ticks et des événements
par un message entre fenêtres de même origine, sans JWT ; les blocs bruts
restent disponibles dans l'archive.

La rétention cloud est désactivée par défaut. `RECORD_RETENTION_DAYS`, entier
strictement positif, active une purge quotidienne des dépôts expirés selon
la fin de séance. Un administrateur autorisé peut aussi supprimer un dépôt :
le téléchargement est révoqué immédiatement, puis fichiers et pages sont
retirés par lots de 100. La séance et sa télémétrie restent présentes. Les
réservations sont gardées comme tombstones pour reconnaître et supprimer les
envois tardifs effectués avec d'anciennes URL signées. Les durées de conservation
et la décision relative aux données de santé restent du ressort d'ANH-172.

## Résumé de séance calculé dans Convex (ANH-89)

Dès que `manifest.json`, `ticks.csv` et `events.jsonl` d'un dépôt sont
vérifiés, Convex calcule le résumé final de la séance à partir de ces trois
fichiers seulement : les blocs bruts, `drive_frames.jsonl`, `sensors.csv` et
`checksums.sha256` ne sont pas lus. Il remplace le résumé provisoire calculé à
la fin de la séance sur la télémétrie à 1 Hz. Il lit les colonnes de
`ticks.csv` par leur nom dans l'en-tête, ne compte que les tics de la séance
(`t > 0`), ignore une ligne qui n'est pas un tic ou un événement en le
signalant, et note `record_degraded` quand l'enregistrement dit lui-même qu'il
lui manque des données. Les définitions des chiffres, et le texte de la
console ou de la simulation dont chacune vient, sont dans
[convex.md](convex.md#session_summaries_v2--résumé-dune-séance-dentraînement-anh-89).
Le format de l'enregistrement ne change pas.

# Racine protégée et données persistantes

ANH-224 termine le lot stockage logiciel d'ANH-151. Les essais utilisent un
fichier disque jetable sous Linux, pas une carte SD ni un Pi. Ils ne prouvent
pas le démarrage de l'image Raspberry Pi après une coupure secteur.

## Répartition

| Chemin | Support et propriétaire |
|---|---|
| `/` | racine inférieure en lecture seule, modifications dans un overlay RAM, perdues au redémarrage |
| `/var/lib/anheart-storage` | volume LUKS2 ext4, root, mode 700 |
| `/var/lib/anheart` | bind du sous-dossier `console`, au compte anheart, mode 700 ; profils, enregistrements, certificats TLS |
| `/var/lib/docker` | bind du sous-dossier `docker`, root, mode 700 ; les images persistent sans imbriquer le stockage Docker dans l'overlay de la racine |
| `/var/log/journal` | bind du sous-dossier `journal`, root ; journal persistant borné à 200 Mo |
| `/etc/anheart/data.key` | clé du volume, root:root, mode 600 |

Le volume et ses sous-dossiers root ne sont jamais donnés au compte de la
console. Le durcissement existant ne parcourt que `/var/lib/anheart`.

Les fichiers que la console garde ou embarque, lus au démarrage ou au repos,
et ceux de ses enregistrements, lus par les fils de travail qui les envoient,
les déposent ou les purgent, passent par `src.kept_file` (ANH-149) :
`read_kept` pour un fichier lu en entier, `open_kept` pour un fichier lu par
morceaux. Jamais à travers un lien symbolique, jamais en attendant sur une
FIFO, jamais lus en entier au-delà de leur borne. Un tel fichier est illisible,
et chaque lecteur garde son chemin habituel pour un fichier illisible. Ce sont
exactement ceux-ci :

| Fichier | Borne | Illisible |
|---|---|---|
| `operators.json` | 1 Mio | liste refusée |
| `operator_lockout.json` | 1 Mio | aucun échec gardé : la console verrouille de nouveau après cinq codes faux |
| `config.applied.json`, `config.level.json` | 1 Mio | configuration refusée, la console ne démarre pas ; niveau illisible : verrous tenus à faux |
| `drive_interventions_due.json` | 4 Kio | compte inconnu |
| compteurs de santé | 16 Kio | compteurs inconnus |
| état du service (redémarrages) | 64 Kio | état illisible : le démarrage compte comme non propre |
| curseur d'un enregistrement, registre des séances perdues | 1 Mio | curseur douteux ; une perte au registre |
| `.sync-baseline.json` | 16 Mio | refaite : les enregistrements sans curseur sont mis de côté, non envoyés |
| marqueur de dépôt `<enregistrement>.deposit.json` | 4 Kio | aucune confirmation : l'enregistrement n'est pas purgé |
| `profiles.local.json` | 4 Mio | mis de côté, les profils livrés prennent le relais |
| `profiles.default.json`, paramètres médicaux | 4 Mio, 64 Kio | la console ne démarre pas |
| signature et clé publique des paramètres médicaux | 4 Kio | non signés : banc seulement, personne à bord |
| `anheart_catalog.pub` | 4 Kio | aucune clé : rien de ce qu'elle vérifie (catalogue, configuration, liste des opérateurs) n'est pris pour signé |
| `VERSION` | 1 Kio | version inconnue, dite au tableau de bord |
| `manifest.json` d'un enregistrement | 2 Mio | `upload.read_head` : rien de l'enregistrement n'est envoyé au tableau de bord, il est laissé au démarrage suivant ; `deposit_files.eligible_record` : pas déposé ; `reader.read` (relecture, import) : refusé |
| `checksums.sha256` d'un enregistrement | 4 994 048 octets | `retention` : aucune confirmation de dépôt écrite, et celle d'avant ne tient plus, l'enregistrement n'est jamais purgé ; `deposit_files.inventory` : pas déposé ; `reader.read` : refusé |
| `ticks.csv`, `events.jsonl` lus pour le tableau de bord | un morceau à la fois (`open_kept`) | `upload.read_points`, `read_events`, `read_last_tick_ms` : erreur de lecture, rien du fichier n'est envoyé, l'enregistrement est laissé au démarrage suivant ; la fin d'une séance qui vient de finir part quand même, de mémoire, après `CLOSE_GRACE` (`_end_from_memory`) |
| un fichier d'enregistrement à déposer | 16 Mio (`MAX_FILE_BYTES`) | `deposit_files.payload` : non envoyé, aucune confirmation. Il lit le chemin que `resolve_under` rend, liens suivis et gardés dans l'enregistrement : ce qu'un lien désigne est ouvert comme un fichier gardé et jugé par son empreinte |
| les autres fichiers d'un enregistrement relus en entier (`ticks.csv`, `sensors.csv`, `events.jsonl`, `drive_frames.jsonl`, blocs ECG) | celles de `FILE_BOUNDS` et `LARGEST_BLOCK_FILE` (`src/record/reader.py`) | `reader.read` : refusé. Il les juge déjà par `lstat` avant d'en lire un octet ; la lecture gardée ferme la fenêtre entre ce contrôle et la lecture. Chaque fichier que `checksums.sha256` liste est haché par morceaux (`open_kept`), au plus à la borne de son nom (la sienne, celle d'un bloc sous `ecg_raw/`, la plus grande pour un autre) : un lien est refusé, une FIFO, qui n'est pas un fichier, est dite manquante sans être ouverte |
| un document de configuration gardé sous son empreinte (`config/<sha>.json`) | 64 Kio (`MAX_CONFIG_BYTES`) | `configuration.persist`, sur le fil du journal : un avertissement de la séance, le document n'est pas remplacé |
| chaque fichier d'un enregistrement, à sa fermeture | haché par morceaux (`open_kept`) | `Writer.close`, sur le fil du journal : un lien planté fait échouer la fermeture (aucun `checksums.sha256` : l'enregistrement n'est pas pris pour fermé ni déposé) ; une FIFO n'est ni listée ni ouverte |

`O_NOFOLLOW` ne garde que le dernier élément d'un chemin. Un enregistrement dont
le répertoire, ou son `ecg_raw`, est un lien est donc refusé à part, par
`lstat` (`kept_file.refuse_link`) : à la fermeture (`Writer.close`, qui sinon
laisserait les blocs ECG hors de son manifeste et de ses empreintes), à la liste
(`retention.records`), au dépôt (`deposit_files.eligible_record` et `inventory`,
répertoire et `ecg_raw`, et `payload` juste avant chaque ouverture, puisqu'un
dépôt dure des minutes), à la confirmation et à la purge (`retention`), à
l'en-tête lu pour le tableau de bord (`upload.read_head`) et à la relecture
(`reader.read`, refus `linked_directory`).

Ce contrôle et l'ouverture qui le suit restent deux appels : un répertoire
remplacé entre les deux est suivi. La fenêtre est courte, mais elle se gagne,
la revue l'a gagnée en échangeant le répertoire d'un enregistrement dans une
boucle serrée, et un dépôt relit chaque fichier à chaque essai. Il faut pour
cela écrire sous `data/records/`, c'est-à-dire les droits du compte de la
console. La correction complète ouvrirait le répertoire de l'enregistrement
avec `O_DIRECTORY | O_NOFOLLOW`, puis chaque fichier relativement à lui
(`openat`, `dir_fd`), sans plus jamais suivre son chemin : elle n'est pas faite.

Reste ouvert : l'export d'un enregistrement (`export.build_archive`, sur le fil
que l'opérateur lance depuis la console) refuse un enregistrement dont le
répertoire est un lien (`export.locate`), mais `tarfile` ouvre chaque fichier
sans `O_NONBLOCK` après son propre `lstat`. Une FIFO mise à la place d'un
fichier entre les deux bloque ce fil d'export, borné par `RecordIo`, pas la
console.

Sous Windows (un poste de développement), `O_NOFOLLOW` et `O_NONBLOCK`
n'existent pas : les liens y sont suivis. La menace est propre au Pi, où ces
drapeaux s'appliquent. Les fichiers y sont ouverts en binaire (`O_BINARY`).

## Mise en service, console et Docker arrêtés

Préparer un volume **LUKS2 déjà formaté en ext4**, vide, et conserver une copie
de récupération de sa clé. Le script ne contient aucune commande de formatage.
Le choix du périphérique, sa création et toute destruction de son ancien
contenu sont des opérations explicites du technicien, hors de ce script.

Installer les prérequis :

```sh
sudo apt-get install cryptsetup-bin rsync initramfs-tools overlayroot
sudo systemctl stop anheart docker
sudoedit /etc/anheart/storage.conf
```

`storage.conf`, root:root 600, contient `DATA_DEVICE=/dev/disk/by-uuid/<UUID-LUKS>`.
Installer la clé correspondante sous `/etc/anheart/data.key`, root:root 600.
Dans `/boot/firmware/config.txt`, `auto_initramfs=1` doit déjà être actif.
L'identité du système dans `/etc/machine-id` doit être définie.

```sh
sudo bash scripts/harden.sh storage --dry-run
sudo bash scripts/harden.sh storage
```

L'essai à blanc vérifie la clé et le format LUKS2, sans ouvrir de mapping ni
écrire de fichier. L'application vérifie aussi ext4 et l'identité inscrite
dans le volume. Un volume non vide et non revendiqué, un volume d'une autre
machine, une mauvaise clé ou un mapping occupé par un autre périphérique sont
refusés. Si la console ou Docker tourne, rien n'est préparé.

La première application copie les données existantes des services arrêtés
avec leurs attributs, puis prépare les montages systemd, `crypttab`, la
configuration overlayroot et régénère les initramfs. Elle ne démarre aucun
service. Chaque copie terminée est synchronisée avant l'écriture atomique
d'un reçu de migration : un nouvel appel reprend une copie inachevée, mais
ne recopie jamais les anciens fichiers par-dessus les données chiffrées
déjà utilisées. Elle ne supprime aucune copie d'origine : ces copies restent sur
la racine inférieure, même après qu'un bind les masque. **Le chiffrement des
nouveaux enregistrements ne chiffre pas rétroactivement ces anciennes
copies.** Leur retrait ou l'effacement de l'ancien support exige une
vérification et une décision explicites après sauvegarde et essais.

Redémarrer avant toute mise en service. Vérifier avec `findmnt` la racine
overlay, le volume ext4 et les trois binds ; contrôler la présence des
enregistrements et des images Docker, puis lancer une séance simulée. La
console et Docker exigent leurs montages : un volume absent ou une clé
inutilisable ne doit jamais faire démarrer la console sur un dossier vide
de la racine. Le journal est basculé sur son stockage par le service de
vidage de journald.

## Portée du chiffrement

La clé en fichier satisfait le mode de mise en service sans TPM prévu par le
ticket. Elle protège l'accès ordinaire sur le Pi en marche et le volume
séparé de sa clé. **Une personne possédant toute la carte SD peut lire une clé
qui se trouve sur cette même carte.** Ce mode ne ferme donc pas MEN-18 pour
le vol complet de la machine. Une clé externe ou scellée dans un TPM et sa
procédure de récupération relèvent de la décision matérielle. Aucune donnée
de santé réelle n'est utilisée par les essais logiciels.

## Verrou du câble partagé

Le service monte le fichier hôte `/tmp/anheart-drive.lock` sur le même chemin
dans le conteneur. Le noyau voit donc le même inode depuis les deux côtés.
`systemd-tmpfiles` le crée sans le tronquer et l'exclut du nettoyage ; le
pré-démarrage vérifie que c'est un fichier régulier à un seul lien, sans
suivre de lien symbolique. Il conserve son inode et son contenu. Le fichier
est au compte anheart et au groupe dialout, mode 660 : un outil hôte doit
appartenir à ce groupe, comme pour le port série.

Le service autorise en écriture ce seul fichier dans son espace de fichiers
protégé. Sa disparition fait échouer la préparation du service, au lieu de
créer silencieusement un autre verrou dans le conteneur. L'acquisition et la
libération restent celles de `DriveLease` ; un fichier PID n'est jamais une
autorisation de voler le câble. Ne jamais supprimer ce fichier pour forcer
un outil à démarrer. La libération du descripteur, ou la mort du processus,
rend le verrou disponible.

## Maintenance et preuves

Une racine déjà en overlay refuse cette préparation : entrer en maintenance
sur la racine persistante, console arrêtée, selon `overlayroot-chroot`, puis
regénérer l'initramfs et redémarrer. Les changements faits directement dans
la racine RAM ne constituent pas une mise à jour persistante.

`scripts/pi/test_storage.sh` vérifie sur un vrai mapping LUKS2 jetable : clé
correcte et incorrecte, dry-run sans modification, copie et remontage des
enregistrements, configuration identique au second passage, permissions des
données Docker, et montage overlay qui laisse sa couche inférieure intacte.
Le même test prouve la contention entre deux chemins bind du verrou, sa
libération par le noyau et le refus d'un lien dur. L'installation systemd
complète reste exercée par le job `pi-install`.

Au banc restent le boot de l'initramfs Raspberry Pi, la perte du volume ou
de sa clé au démarrage, le journal après coupure secteur, la contention avec
le vrai pilote et la mesure des arrêts. Consigner ces essais dans ANH-151 et
ses tickets de validation ; les preuves logicielles ne les remplacent pas.

Références : [overlayroot Debian](https://sources.debian.org/src/cloud-initramfs-tools/0.18.debian13%2Bdeb12u1/overlayroot/etc/overlayroot.conf/),
[cryptsetup open](https://manpages.debian.org/bookworm/cryptsetup-bin/cryptsetup-open.8.en.html).

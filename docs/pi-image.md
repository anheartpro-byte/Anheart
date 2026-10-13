# Image du Raspberry Pi et démarrage automatique de la console

Ce document dit **comment un Raspberry Pi neuf est installé**, **quelles
versions sont figées**, et **ce qui lance la console à l'allumage**. Il complète
[deploiement.md](deploiement.md#7-le-raspberry-pi) (les environnements et les
clés), [raspberry-pi.md](raspberry-pi.md) (le logiciel lui-même) et
[release.md](release.md) (les numéros de version).

[Retour au sommaire](README.md) · termes : [glossaire](glossaire.md)

> **État réel au 7 octobre 2026.**
>
> - **Vérifié en CI, sans Raspberry Pi** : le script d'installation est exécuté
>   pour de bon dans une machine de remplacement (Debian 12, systemd, arm64) ;
>   la console y démarre en simulation, répond sur `/healthz`, revient au repos
>   après un arrêt du service en pleine séance, après un arrêt brutal, et
>   après l'arrêt puis le rallumage de la machine de remplacement
>   ([section 5](#5-ce-que-la-ci-vérifie)). Premier passage : job `pi-install`
>   de la PR #37, run 37607234436.
> - **Ajouté le 10 octobre 2026, vérifié de la même façon** : systemd surveille
>   la console et la relance quand elle se fige ou meurt
>   ([Le chien de garde](#le-chien-de-garde)). Dans la machine de remplacement,
>   une console figée en pleine séance est arrêtée par le chien de garde du
>   service puis relancée au repos (13 s après le gel dans l'exécution
>   38072378030 du job `pi-install`), et une console qui meurt trois fois de
>   suite revient hors service (étapes 9 et 10 de la
>   [section 5](#5-ce-que-la-ci-vérifie)). Une console qui ne répond
>   réellement à rien, pas même à son arrêt, n'a été exercée nulle part sous
>   le vrai systemd.
> - **Pas vérifié** : aucune installation sur un vrai Raspberry Pi, ni avec le
>   vrai variateur, ni avec le vrai BITalino. Le chien de garde **matériel** du
>   Pi n'a jamais été armé ni déclenché : seul son réglage est écrit et relu.
>   La liste de ce qu'une personne doit faire et constater est en
>   [section 6](#6-installer-un-vrai-raspberry-pi).
> - **Heure et journal du système (10 octobre 2026)** : le script installe le
>   service de temps `chrony` et règle un journal gardé sur le disque et borné ;
>   la console dit si son horloge est tenue par ce service
>   ([section 4](#lheure)). Vérifié dans la machine de remplacement, pas sur
>   un Pi : **l'essai de coupure secteur n'est pas fait**
>   ([section 6](#essai-de-coupure-secteur--à-faire-au-banc)).
> - **Console sans privilèges inutiles (10 octobre 2026, premier lot du
>   durcissement)** : dans son conteneur la console est le compte `anheart`,
>   sans aucune capacité, sur une image en lecture seule, et n'ouvre que trois
>   classes de périphériques ; la configuration est à root seul, les données au
>   compte seul ; plus aucune mise à jour automatique des paquets
>   ([Sous quel compte](#sous-quel-compte),
>   [section 10](#10-durcissement--ce-que-fait-scriptshardensh-et-la-check-list-de-limage)).
>   Vérifié dans la machine de remplacement, en simulation (job `pi-install`
>   de la PR #73). **Pas vérifié** :
>   le vrai câble du variateur et le vrai BITalino sous ce compte, dont les
>   droits ne se constatent que sur un Pi
>   ([section 6](#essai-du-durcissement--à-faire-au-banc)). Le point d'accès,
>   le pare-feu et TLS sont désormais intégrés par
>   [le lot réseau](pi-reseau.md). Le système racine en lecture seule reste
>   le lot stockage. Aucun de ces essais n'est une validation du Pi réel.
> - **TLS de la console (10 octobre 2026)** : la console sait servir sa page en
>   TLS et un script fait son certificat sur le Pi. Écrit et testé dans la gate
>   du Pi ; activé lors de la mise en service par `harden.sh network`,
>   jamais essayé avec une tablette réelle
>   ([section 4](#le-certificat-de-la-console)).
> - **Pas fait** : l'image n'est ni publiée dans un registre ni signée
>   ([section 1](#ce-qui-reste-pour-une-image-construite-et-signée-par-la-ci)).

## Sommaire

1. [Le choix : une image Docker lancée par systemd](#1-le-choix--une-image-docker-lancée-par-systemd)
2. [Versions figées](#2-versions-figées)
3. [Ce que fait `scripts/install.sh`](#3-ce-que-fait-scriptsinstallsh)
4. [Le service `anheart`](#4-le-service-anheart)
5. [Ce que la CI vérifie](#5-ce-que-la-ci-vérifie)
6. [Installer un vrai Raspberry Pi](#6-installer-un-vrai-raspberry-pi)
7. [Mettre à jour, changer une version figée](#7-mettre-à-jour-changer-une-version-figée)
8. [Limites et reste à faire](#8-limites-et-reste-à-faire)
9. [Endurance : deux heures en simulation sur le Pi, puis une journée au banc](#9-endurance--deux-heures-en-simulation-sur-le-pi-puis-une-journée-au-banc)
10. [Durcissement : ce que fait `scripts/harden.sh`, et la check-list de l'image](#10-durcissement--ce-que-fait-scriptshardensh-et-la-check-list-de-limage)

---

## 1. Le choix : une image Docker lancée par systemd

Deux chemins existaient à moitié dans le dépôt : une image Docker avec un
fichier Compose, et une installation native (environnement virtuel Python lancé
par systemd). **Un seul est gardé et maintenu : l'image Docker de la console,
lancée par un service systemd.**

| | Image Docker lancée par systemd (retenu) | Installation native (abandonné) |
|---|---|---|
| Python 3.12, exigé par le code | celui de l'image officielle `python`, figé par son empreinte | absent du système : Raspberry Pi OS 12 fournit Python 3.11.2, il fallait compiler ou télécharger un Python à part |
| Ce qui tourne sur le Pi | la même image que celle que la CI démarre sur arm64 | un environnement construit sur chaque Pi, jamais vu par la CI |
| Mises à jour à distance prévues | une version est une image nommée : en changer, c'est changer un nom, l'ancienne reste sur la machine | à inventer |
| Démarrage à l'allumage, journal, arrêt | systemd, comme pour tout service | systemd |

Les raisons, toutes constatées dans le dépôt :

* **Python 3.12.** `raspberry-pi/pyproject.toml` vise Python 3.12 et la gate
  tourne sur cette version. L'image Raspberry Pi OS figée ci-dessous contient
  `python3` 3.11.2. L'ancien script acceptait Python 3.9 et installait 3.11 :
  il ne pouvait pas produire une console conforme.
* **Ce qui est testé est ce qui tourne.** La CI construit l'image sur arm64,
  l'architecture des Pi 4 et 5, et y démarre la console
  ([section 5](#5-ce-que-la-ci-vérifie)).
* **Les mises à jour à distance** (tickets ANH-116, ANH-168, ANH-169) : le
  service lance l'image dont le nom est écrit dans un seul petit fichier, et
  l'image de la version précédente reste sur la machine.

Ce qui a été retiré : `docker-compose.yml`, `docker-compose.dev.yml`, l'unité
systemd qui lançait un environnement virtuel, et toute installation de Python
par `scripts/install.sh`. Sur le Pi, Docker est le paquet `docker.io` de Debian,
sans dépôt tiers ni greffon Compose.

Pour essayer l'image sur un poste de développement, sans Pi : lancer le test de
bout en bout (`bash raspberry-pi/scripts/pi/test_install.sh`,
[section 5](#5-ce-que-la-ci-vérifie)). Pour le travail courant, la console se
lance sans Docker, depuis l'environnement virtuel
([demarrage-rapide.md](demarrage-rapide.md#3-lancer-la-console-locale-en-simulation-complète)).

### Ce qui reste pour une image construite et signée par la CI

La recommandation du ticket était une image **signée**, construite par la CI.
Ce qui est fait : la CI construit l'image et prouve qu'elle démarre. Ce qui ne
l'est pas : la publier et la signer. Tant que ce n'est pas fait, **le Pi
construit lui-même l'image**, à partir de sa copie des sources, avec le même
`Dockerfile` et les mêmes fichiers de verrouillage que la CI.

| Reste à faire | Ce qu'il faut | Qui |
|---|---|---|
| Publier l'image depuis la CI | un registre (GitHub Container Registry ou autre), et la permission d'écriture correspondante dans un workflow : aucun workflow du dépôt n'a de permission d'écriture en dehors de l'analyse CodeQL | le chef de projet décide du registre et de sa visibilité (le dépôt est public) ; ticket ANH-126 |
| Signer l'image, vérifier la signature sur le Pi avant de la lancer | une clé de signature ou une signature sans clé liée à l'identité du workflow, et la clé publique sur le Pi | le chef de projet fournit ou fait créer la clé ; ticket ANH-168 |
| Faire tirer l'image par le Pi au lieu de la construire | les deux lignes ci-dessus ; dans `scripts/install.sh`, seule l'étape qui obtient l'image change | ticket ANH-168 |

---

## 2. Versions figées

Chaque version ci-dessous est écrite dans un fichier du dépôt. Le test
`tests/test_pi_install.py` échoue si cette page et ces fichiers ne disent plus
la même chose.

### Le système du Pi

| Élément | Version figée | Où |
|---|---|---|
| Carte | Raspberry Pi 4 ou 5 (arm64) | |
| Système | Raspberry Pi OS (Legacy) Lite, 64 bits, Debian 12 « bookworm », référence `2026-10-06` | `OS_CODENAME` et `OS_REFERENCE` dans `scripts/install.sh` |
| Fichier à flasher | `2026-10-06-raspios-bookworm-arm64-lite.img.xz`, 442 245 740 octets | |
| SHA-256 du fichier | `9e938c5a6981c3f498b3b564c527f3e1fd58ef20bbf703be0eea748384a6c1af` | publié par Raspberry Pi à côté du fichier (`.sha256`) |
| Docker | paquet `docker.io` de Debian 12 : 20.10.24+dfsg1-1+deb12u1+b6 (installé par la CI le 7 octobre 2026) | installé par `scripts/install.sh` |
| BlueZ | paquet `bluez` : 5.66-1+rpt2+deb12u2 dans l'image figée (5.66-1+deb12u2 dans Debian, celui de la CI) | déjà dans l'image, vérifié par le script |
| Service de temps | paquet `chrony` de Debian 12 (série 4.3) ; il prend la place de `systemd-timesyncd`, le service de temps de Raspberry Pi OS (à constater sur un Pi, [section 6](#6-installer-un-vrai-raspberry-pi)) | installé par `scripts/install.sh` |
| libusb | paquet `libusb-1.0-0` : 2:1.0.26-1 dans l'image figée | déjà dans l'image, vérifié par le script |
| setpriv | paquet `util-linux` de Debian 12, déjà dans l'image | lance le relais du chien de garde sous le compte `anheart` ([section 4](#le-chien-de-garde)) |
| socat | paquet `socat` de Debian 12 : 1.7.4.4-2 (installé par la CI le 10 octobre 2026) | installé par `scripts/install.sh` : c'est le relais du chien de garde ([section 4](#le-chien-de-garde)) |
| systemd | 252.39-1~deb12u2 dans l'image figée | déjà dans l'image |

Le fichier se télécharge sur `downloads.raspberrypi.com`, dossier
`raspios_oldstable_lite_arm64/images/raspios_oldstable_lite_arm64-2026-10-06/`.

Le script refuse un système qui n'est pas Debian 12 « bookworm ». Il compare la
référence de l'image (`/etc/rpi-issue`) à celle de cette page et **avertit** si
elle diffère, sans refuser : une image plus récente de la même série reste
Debian 12.

Les paquets du système (`docker.io`, `bluez`, `libusb-1.0-0`, `chrony`, `socat`)
viennent de Debian 12 : ils sont figés par la série, qui ne reçoit que des
corrections, pas par un numéro exact. Les numéros ci-dessus sont ceux de la série au 7 octobre
2026 ; le script affiche ceux qu'il trouve.

### L'image de la console

| Élément | Version figée | Où |
|---|---|---|
| Image de base | `python:3.12.15-slim-bookworm` | ligne `FROM` de `raspberry-pi/Dockerfile` |
| Empreinte de l'image de base | `sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258` | même ligne |
| Python | 3.12.15 | celui de l'image de base |
| pip | celui de l'image de base, jamais mis à jour pendant la construction | |
| Paquets Python | 51 paquets, chacun à une version exacte, avec ses empreintes | `raspberry-pi/requirements-lock.txt` |
| Outils de construction | 3 paquets, idem | `raspberry-pi/requirements-build-lock.txt` |

`pip` installe avec `--require-hashes` : un paquet absent des fichiers de
verrouillage, ou dont l'empreinte ne correspond pas, fait échouer la
construction. Un seul paquet est compilé depuis ses sources,
`PyBluez-bitalino` (tiré par `bitalino`) ; ses outils de construction sont
figés eux aussi et `pip` ne télécharge rien d'autre pour le compiler.

Les paquets que le code demande directement (`requirements-base.txt`,
`requirements-prod.txt`, `requirements-build.txt`) :

| Paquet | Version | Fichier |
|---|---|---|
| `numpy` | `2.5.3` | base |
| `pyserial` | `3.5` | base |
| `scipy` | `1.18.1` | base |
| `biosppy` | `2.2.4` | base |
| `peakutils` | `1.3.5` | base |
| `pymodbus` | `3.7.4` | base |
| `pyftdi` | `0.57.2` | base |
| `fastapi` | `0.141.1` | base |
| `uvicorn` | `0.54.0` | base |
| `websockets` | `17.1` | base |
| `httpx` | `0.28.1` | base |
| `pydantic` | `2.13.5` | base |
| `python-dotenv` | `1.2.3` | base |
| `pyobjc-framework-IOBluetooth` | `12.2.2` | base, macOS seulement : absent de l'image |
| `bitalino` | `1.2.6` | prod |
| `pexpect` | `4.9.0` | prod |
| `setuptools` | `84.0.0` | build |
| `wheel` | `0.48.0` | build |

`requirements-base.txt` est aussi ce qu'installent le poste de développement et
la CI (`requirements-dev.txt` l'inclut) : les trois font tourner les mêmes
versions. Les outils de développement de `requirements-dev.txt` (pytest, ruff,
mypy, basedpyright) ne sont pas figés par ce ticket.

Les paquets Debian installés **dans** l'image (`bluez`, `libusb-1.0-0`,
compilateurs) sont ceux de Debian 12 le jour de la construction : figés par la
série, pas par un numéro exact.

### La machine de remplacement du test

Le test de bout en bout démarre `debian:bookworm-20261005`, figée par son
empreinte dans `scripts/pi/test_install.sh` (`MACHINE_BASE`).

---

## 3. Ce que fait `scripts/install.sh`

Sur le Pi, depuis le dossier `raspberry-pi/` :

```sh
sudo bash scripts/install.sh                 # machine réelle
sudo bash scripts/install.sh --simulation    # sans matériel : variateur et ECG simulés
```

Dans l'ordre :

| Étape | Ce qui est fait | Si on relance le script |
|---|---|---|
| Système | refuse tout autre système que Debian 12 ; avertit si l'architecture n'est pas arm64 ou si la référence Raspberry Pi OS diffère | idem |
| Version | lit `raspberry-pi/VERSION` ; refuse une version qui ne peut pas nommer une image | idem |
| Console en marche | si le service tourne déjà, ou si un conteneur nommé `anheart` tourne sans lui (lancé à la main, ou resté d'une autre façon de lancer l'image), demande à la console si elle est au repos ; **refuse de continuer sinon**, y compris quand elle ne répond pas | idem |
| Paquets | installe `docker.io`, `bluez`, `libusb-1.0-0`, `ca-certificates`, `curl`, `chrony`, `socat` ; active Docker et le Bluetooth | rien à installer : `apt` n'est pas appelé |
| Heure | écrit `/etc/chrony/conf.d/anheart.conf`, active et démarre `chrony` ; dit si la machine a une horloge temps réel ([section 4](#lheure)) | fichier identique : `chrony` n'est pas redémarré |
| Journal du système | écrit `/etc/systemd/journald.conf.d/anheart.conf` (`Storage=persistent`, `SystemMaxUse=200M`) et redémarre `systemd-journald` ([section 4](#le-journal-du-système)) | fichier identique : rien n'est redémarré |
| Compte | crée le compte système `anheart` : sans connexion, sans mot de passe, membre du groupe `dialout` (les périphériques série) | déjà là ; l'étape Protection le corrige s'il a été modifié |
| Dossiers | crée `/var/lib/anheart/data` (profils locaux), `/var/lib/anheart/records` (enregistrements de séance, journal hors séance, état des redémarrages) et `/var/lib/anheart/data/records` (le point où le service monte le second dans le premier) | déjà là |
| Configuration | écrit `/etc/anheart/anheart.env` à partir de `.env.pi.example`, lisible par root seul (mode 600) | **le fichier existant n'est jamais réécrit ni affiché** |
| Câble du variateur | écrit `/etc/udev/rules.d/70-anheart-drive.rules` : le câble USB-RS485 Schneider (16de:0003) appartient au groupe `dialout`, comme un port `ttyUSB` ([Sous quel compte](#sous-quel-compte)) ; fait relire ses règles à `udev` | fichier identique : rien n'est relu |
| Protection | lance les étapes `account`, `files` et `updates` de `scripts/harden.sh` ([section 10](#10-durcissement--ce-que-fait-scriptshardensh-et-la-check-list-de-limage)) : le compte tel qu'il doit être ; la configuration à root seul (600) ; tout ce qui est sous `/var/lib/anheart` au compte `anheart` seul (dossiers 700, fichiers sans aucun droit pour le groupe ni pour les autres) ; plus aucune mise à jour automatique des paquets. **Avant** la construction de l'image : une construction qui échoue laisse la machine protégée | chaque étape regarde d'abord : rien n'est changé, et le script le dit (`harden.sh: nothing to change`). Sur une machine installée avant ce changement, ou modifiée à la main, ce qui diffère est corrigé, **sans lire ni écrire aucun contenu** |
| Image | construit `anheart-console:<version>` depuis le dossier | couches en cache : même image, rien ne change |
| Chien de garde matériel | écrit `/etc/systemd/system.conf.d/10-anheart-watchdog.conf` (`RuntimeWatchdogSec=15s`) : systemd arme et nourrit le chien de garde du Pi, pris en compte au plus tard au démarrage suivant de la machine ; avertit si `/dev/watchdog` n'existe pas | fichier identique : inchangé. Ce fichier seul ne fait jamais redémarrer la console |
| Service | installe `anheart.service`, le fichier qui porte la version, celui qui porte **les numéros** du compte `anheart` et du groupe `dialout` sur cette machine (`20-account.conf`), et `/usr/local/lib/anheart/bind-bitalino`, que l'unité lance avant la console ; active le service au démarrage | fichiers identiques : inchangés |
| Démarrage | démarre le service ; s'il tournait, ne le relance **que si l'image ou l'unité a changé** : il l'arrête, repasse l'étape `files`, puis le démarre. Dans tous les cas, une console en marche est interrogée une seconde fois juste avant, et doit se dire au repos | console inchangée : laissée en marche |
| Test de démarrage | attend que `GET /healthz` réponde (180 s au plus) ; si le script vient de démarrer la console, exige qu'elle se dise au repos ; vérifie que le service est activé au démarrage | idem |

Le script ne parle jamais au variateur et ne lance aucune séance.

`pyftdi`, qui pilote le câble USB-RS485 Schneider, n'est pas un paquet du
système : c'est un paquet Python de l'image ([section 2](#limage-de-la-console)),
avec la bibliothèque `libusb-1.0-0` dont il a besoin. Le script installe aussi
`libusb-1.0-0` sur le Pi lui-même.

**Le format du fichier.** Une ligne est `CLÉ=valeur` et rien d'autre. Docker
passe à la console tout ce qui suit le `=` : des guillemets ou un commentaire
en fin de ligne feraient partie de la valeur. Le script avertit, en ne nommant
que les clés, s'il en trouve.

**Arrêt puis démarrage, et non redémarrage.** Une console installée avant ce
changement tournait sous root : en s'arrêtant, elle écrit encore un fichier
sous root (l'état de ses redémarrages). Le script arrête donc le service, rend
au compte `anheart` ce qui vient d'être écrit, puis démarre la nouvelle
console, qui n'est plus root et doit pouvoir le lire.

**Les secrets.** `/etc/anheart/anheart.env` porte la clé de la machine
(`MACHINE_API_KEY`) et, si la page est ouverte au réseau, son jeton
(`UI_TOKEN`). **Le fichier appartient à root, mode 600, et non au compte
`anheart`** : c'est le client Docker du service, sous root, qui le lit pour
la console. Le compte sous lequel tourne la console ne peut ni le lire sur la
machine, ni surtout le réécrire : ce fichier porte aussi les limites que la
console applique (`MOTOR_MAX_RPM`, `OCCUPANCY_OCCUPIED_ENABLED`,
`PROGRAMS_ENABLED`, les seuils cardiaques). Le script ne les affiche jamais. Quand il interroge une console
protégée par un jeton, le jeton passe par l'entrée standard de `curl`, jamais
par sa ligne de commande, que tout utilisateur de la machine peut lire dans la
liste des processus. `scripts/pi/preflight.sh` donne la clé de la machine à
`curl` de la même façon. Le fichier créé a une clé de machine **vide** : la
console d'un Pi neuf n'est reliée à aucun tableau de bord tant qu'une personne
n'a pas écrit la clé.

**Les clés du fichier.** Le fichier créé est une copie de `.env.pi.example`,
qui ne porte que des clés lues par la console
(`tests/test_legacy_recorder_retired.py` le vérifie). Tous les réglages sont
encore locaux à la machine : la configuration centralisée dans Convex (ticket
ANH-141) n'existe pas. Quand elle existera, les clés qu'elle reprend quitteront
ce modèle.

**`--simulation`** n'agit que sur un fichier **nouveau** : il y écrit
`MOTOR_BACKEND=sim` et `ECG_SOURCE=sim`, l'interrupteur de simulation de la
console ([raspberry-pi.md](raspberry-pi.md#122-minimum-pour-une-simulation-complète)).
Le moteur ne tourne pas, aucun matériel n'est ouvert. Pour passer ensuite au
matériel réel, modifier le fichier à la main
([section 6](#6-installer-un-vrai-raspberry-pi)).

**Où vont les données.** Le service monte `/var/lib/anheart/data` sur
`/app/data` et `/var/lib/anheart/records` sur `/app/data/records`, l'emplacement
par défaut des enregistrements de séance (`RECORD_ROOT`) : aucune clé à régler.
Ce montage demande un dossier vide `/var/lib/anheart/data/records` sur le Pi,
que le script crée ; les enregistrements sont dans `/var/lib/anheart/records`.
Le même dossier porte `service-state.json`, ce que la console garde de ses
propres redémarrages ([Le chien de garde](#le-chien-de-garde)), et `logbook/`,
le journal hors séance. Tout y appartient au compte `anheart` et à lui seul :
pour y lire, `sudo`.

**Désinstaller** : `sudo bash scripts/uninstall.sh` arrête la console par son
arrêt contrôlé, puis **reprend tout ce que l'installation et le durcissement
ont réglé dans le système**. Ce qui appartient à la machine reste.

| Repris | Laissé, exprès |
|---|---|
| l'unité `anheart.service`, ses deux fichiers d'accompagnement, et `/usr/local/lib/anheart` | la configuration `/etc/anheart/anheart.env` : elle porte la clé de la machine |
| `/etc/chrony/conf.d/anheart.conf` (`chrony` est relancé) | les données `/var/lib/anheart` et le compte `anheart` qui les possède, aussi privés qu'avant |
| `/etc/systemd/journald.conf.d/anheart.conf` (`journald` est relancé ; le journal déjà sur le disque est gardé) | les images Docker |
| `/etc/systemd/system.conf.d/10-anheart-watchdog.conf` : systemd n'arme plus le chien de garde matériel à partir du démarrage suivant | les paquets du système (`docker.io`, `bluez`, `chrony`, `socat`...) : `chrony` reste le service de temps |
| `/etc/udev/rules.d/70-anheart-drive.rules` | |
| ce que `scripts/harden.sh` a réglé (`harden.sh --undo`) : le système met de nouveau ses paquets à jour seul, comme la distribution le prévoit | |

Le relancer est sans effet, et `sudo bash scripts/install.sh` réinstalle
ensuite la console sur la même configuration et les mêmes données (test de
bout en bout, étape 12). Le réglage du chien de garde matériel était laissé en
place jusqu'ici : il est repris avec le reste, parce qu'une machine sans
console n'a plus de raison de redémarrer seule, et que l'installation le
réécrit.

---

## 4. Le service `anheart`

`scripts/anheart.service` lance l'image par `docker run`, au premier plan : ce
que la console écrit va dans le journal de systemd. Le fichier
`/etc/systemd/system/anheart.service.d/10-version.conf`, écrit par le script,
porte la version et le nom de l'image.

```sh
systemctl status anheart          # état, et la version dans la première ligne
journalctl -u anheart -f          # journal de la console
sudo systemctl stop anheart       # arrêt contrôlé
sudo systemctl restart anheart    # au repos seulement
```

### La version

La première ligne de `systemctl status anheart` porte la version. Sortie
relevée dans la CI, après la première installation :

```text
● anheart.service - Console opérateur Anheart, version pi-0.0.0-dev
     Loaded: loaded (/etc/systemd/system/anheart.service; enabled; preset: enabled)
    Drop-In: /etc/systemd/system/anheart.service.d
             └─10-version.conf
     Active: active (running) since Wed 2026-10-07 10:26:32 UTC; 2s ago
```

C'est le contenu de `raspberry-pi/VERSION` au moment où l'image a été
construite. Le même fichier est copié dans l'image (`/app/VERSION`) et l'image
porte l'étiquette `org.opencontainers.image.version`. Le test de bout en bout
compare les trois.

**La console, le tableau de bord et les enregistrements de séance portent cette
version.** La console lit elle-même `/app/VERSION`, une fois, au démarrage
(`src/contract.py`). Elle l'affiche sur sa page (pastille **Version**),
l'annonce au tableau de bord dans chaque heartbeat et l'écrit dans le manifeste
de chaque enregistrement (`software_version`). Un fichier `VERSION` absent,
illisible ou mal formé ne l'empêche pas de démarrer : les trois disent alors
`pi-unknown`.

Le script d'entrée de l'image (`docker/entrypoint.sh`) ne s'occupe plus de la
version. Il donnait auparavant à la console la variable
`ANHEART_SOFTWARE_VERSION`, pour le manifeste seul : cette variable n'est plus
lue. Si `/etc/anheart/anheart.env` la règle encore, la ligne est sans effet et
peut être retirée ; la console le signale une fois dans son journal au
démarrage.

**Une image absente ne se télécharge pas.** L'unité lance `docker run` avec
`--pull never` : si l'image de la version installée n'est pas sur la machine,
le démarrage échoue tout de suite, et rien n'est jamais demandé à un registre.

### Le contrôle de santé

L'image interroge elle-même `GET /healthz` toutes les 30 s (instruction
`HEALTHCHECK` du `Dockerfile`) ; l'unité ne remplace pas ce contrôle.

```sh
curl -fsS http://127.0.0.1:8090/healthz
sudo docker inspect --format '{{.State.Health.Status}}' anheart    # healthy
```

`/healthz` répond sans jeton et ne dit rien de la séance. Le port est celui de
`UI_PORT` dans `/etc/anheart/anheart.env` (8090 dans le modèle).

Ce contrôle **constate**, il ne répare pas : une console dont la page ne répond
plus est marquée `unhealthy` par Docker, et ce contrôle ne la redémarre pas. Ce
qui relance une console figée est le chien de garde de l'unité, qui écoute la
boucle de contrôle elle-même et non la page.

### Le chien de garde

Si la console se fige ou meurt, systemd la relance, **au repos**. Trois
mécanismes, du plus rapide au plus lent, et le premier n'est pas le nôtre :

| Qui | Ce qu'il surveille | Délai | Ce qu'il fait |
|---|---|---|---|
| Le variateur (ttO) | les trames de la console | réglé dans le variateur (3 s dans le simulateur) | arrête le moteur. **C'est le filet du moteur**, et le seul : rien de ce qui suit n'arrête quoi que ce soit plus vite |
| systemd, sur l'unité (`WatchdogSec=10`) | les tics de contrôle terminés de la console | 10 s sans nouvelle | arrête la console par son arrêt contrôlé, puis la relance |
| Le chien de garde matériel du Pi (`RuntimeWatchdogSec=15s`) | systemd lui-même | 15 s sans que systemd le nourrisse | redémarre le Pi : système entier figé, systemd compris |

**Ce que systemd entend.** La console dit `READY=1` à son premier tic de
contrôle terminé, puis `WATCHDOG=1` à chaque tic terminé, et de nulle part
ailleurs dans son code
([raspberry-pi.md](raspberry-pi.md#36-sous-systemd--signe-de-vie-redémarrage-hors-service)).
Le service est `Type=notify` : il n'est « démarré » qu'à ce premier tic, et le
compte des 10 s part de là, pas du lancement de l'image (une carte SD froide met
bien plus de 10 s à charger la console ; l'unité lui laisse 300 s).

**Le relais.** systemd n'écoute que les processus de l'unité, et la console est
un processus du conteneur. L'unité lance donc `socat`, qui est un des siens : il
écoute sur `/run/anheart/relay/notify`, que la console reçoit comme `NOTIFY_SOCKET`,
et transmet chaque datagramme à systemd tel quel (`NotifyAccess=all`). Il
n'invente rien : une console qui ne dit rien n'est pas entendue. S'il meurt,
systemd n'entend plus la console et l'arrête au bout de 10 s, par son arrêt
contrôlé : une panne du relais coûte un arrêt contrôlé, jamais un arrêt brutal.
Sans `socat` ou sans `setpriv`, l'unité refuse de démarrer
(`AssertFileIsExecutable`).

**Qui peut parler au relais.** `NotifyAccess` reste à `all` et ne peut pas
être plus étroit : le relais est un processus de l'unité sans en être le
processus principal (c'est `docker run`), ni la commande d'une de ses lignes.
Ce qui est étroit, c'est l'entrée du relais. Il tourne sous le compte
`anheart` (`setpriv`), et non plus sous root : c'est lui qui lit ce que la
console envoie. Il écoute dans un dossier à lui, `/run/anheart/relay`, créé à
chaque démarrage pour le compte seul (mode 700) ; son socket appartient au
compte. Seule la console, qui est ce même compte, peut donc nourrir le chien
de garde. Si ce dossier ne peut pas être créé, le démarrage échoue tout de
suite, sans lancer de console que personne n'entendrait.

Ce dossier n'est pas `/run/anheart` lui-même, et il y a une raison : systemd
reprend `/run/anheart`, sous root, chaque fois qu'il lance une ligne de
l'unité, **celle de l'arrêt comprise**. Donné au compte, il lui était retiré
au moment précis où la console, en s'arrêtant, dit qu'elle s'arrête : elle
ne joignait plus son relais. Vu dans la machine de remplacement, pas en
lisant l'unité. Ce que systemd trouve tel qu'il l'attend, il n'y regarde
pas : le dossier `relay`, à l'intérieur, reste au compte.

**Un relais présent mais qui ne démarre pas** (il n'arrive pas à écouter) est
le cas à connaître, parce que la console a alors l'air de marcher. Elle
tourne, sa page répond, elle peut armer une séance ; mais systemd ne l'entend
jamais se dire prête. Au bout de 300 s (`TimeoutStartSec`) il arrête l'unité
par son arrêt contrôlé, **séance en cours comprise**, la relance, et
recommence toutes les 300 s jusqu'à sa limite de démarrages. Ce que
l'opérateur voit : sous les pastilles de la page, `chien de garde : N
notification(s) non envoyee(s)`, avec N qui monte ; sur le Pi,
`systemctl status anheart` reste à `activating`, et `journalctl -u anheart`
porte `notification not sent` et l'erreur de `socat`. **Ne pas lancer de
séance sur une console qui affiche cette ligne.** Lu dans l'unité et dans le
code, jamais observé.

**Ce qui se passe quand la console se tait.**

Trois cas, qui ne se ressemblent pas, et un seul des trois a été observé sous
le vrai systemd.

| Instant | Tic bloqué, boucle vivante | Processus figé, puis relâché par l'arrêt (`SIGSTOP`, `docker pause`) | Processus qui ne répond à rien, pas même à l'arrêt |
|---|---|---|---|
| 0 | dernier tic terminé ; plus aucune trame vers le variateur | idem | idem |
| ttO du variateur | le variateur arrête le moteur | idem | idem |
| 10 s | systemd : `Watchdog timeout`, puis SIGTERM suivi de SIGCONT aux processus de l'unité (`WatchdogSignal=SIGTERM`) ; `docker run` relaie les deux à la console | idem : **le SIGCONT réveille la console** | idem ; sans effet |
| ensuite | la console sort par son arrêt contrôlé | la console réveillée fait un tic, en retard (`loop_stall`, `GO_SILENT`), puis sort par son arrêt contrôlé et clôt son enregistrement : 60 ms dans la CI | rien pendant 15 s (`TimeoutAbortSec=15`), puis l'arrêt ordinaire de l'unité : `docker stop -t 60`, qui tue au bout de 60 s |
| Code de sortie | 1 : aucun tic depuis plus de 3 s à l'arrêt | 1 : le tic revenu en retard a marqué le processus | tuée |
| + 2 s (`RestartSec=2`) | nouvelle console, au repos | nouvelle console, au repos, **13 s après le gel** | nouvelle console, au repos, environ 90 s après le gel (calculé : 10 + 15 + 60 + 2) |
| Vu où | banc logiciel, face à un modèle de systemd (`test_ex4_a_stalled_loop_is_ended_by_the_watchdog_…`) | **sous le vrai systemd** : test de bout en bout, étape 9 (durées de l'exécution 38072378030) ; et banc logiciel (`test_ex4_a_console_frozen_then_woken_by_its_own_stop_…`) | **jamais sous systemd.** Lu dans l'unité ; côté console, banc logiciel face au modèle (`test_ex4_a_frozen_console_is_killed_…`) |

Un gel appliqué du dehors à la console (`SIGSTOP`, `docker pause`) est
toujours levé par l'arrêt lui-même : c'est le cas du milieu. Celui de droite
demande un processus bloqué hors de sa boucle d'événements, qu'aucun geste
extérieur ne produit.

La nouvelle console est un nouveau processus : pas d'attestation, pas de
séance, et un variateur trouvé activé ou en rotation est mis à zéro et
verrouillé ([Allumage et redémarrage](#allumage-et-redémarrage--rien-ne-repart-seul)).
Dans les trois cas son démarrage suit un arrêt non propre : il est compté, et
elle écrit l'avertissement « démarrage après arrêt non propre ». Elle y nomme
l'enregistrement de la séance coupée quand il est resté ouvert, ce qui est le
cas d'une console tuée ; une console réveillée par son arrêt a clos le sien.

**La limite, et le mode hors service.**

| Niveau | Règle | Ce qu'on voit | Comment en sortir |
|---|---|---|---|
| La console | trois démarrages après un arrêt non propre en dix minutes | la console tourne, sa page affiche `CONSOLE HORS SERVICE`, elle n'arme rien ; `journalctl -u anheart` porte `out_of_service` | un opérateur nommé la remet en service depuis la page (Securite), après avoir fait vérifier la machine |
| systemd | un onzième démarrage en 30 minutes est refusé (`StartLimitBurst=10`, `StartLimitIntervalSec=1800`) | plus de console : la page ne répond plus, `systemctl status anheart` dit `failed (Result: start-limit-hit)` | lire `journalctl -u anheart`, corriger, puis `sudo systemctl reset-failed anheart && sudo systemctl start anheart` |

La limite de systemd est pour la console qui ne revient pas du tout ; celle qui
revient s'est mise hors service bien avant. Les démarrages faits à la main
comptent dans les dix : plus de dix `systemctl restart` en une demi-heure
donnent la même erreur, et la même commande la lève.

```sh
systemctl show -p Result,NRestarts anheart     # watchdog, exit-code, start-limit-hit...
journalctl -u anheart | grep -i watchdog       # « Watchdog timeout (limit 10s)! »
curl -s http://127.0.0.1:8090/api/panel        # "service":{"out_of_service":...}, "watchdog":{...}
```

**Le Pi lui-même.** Le chien de garde matériel (`bcm2835_wdt`, `/dev/watchdog0`)
est armé et nourri par systemd : il redémarre le Pi quand le système entier est
figé. Il ne redémarre **pas** le Pi quand systemd tourne mais n'arrive plus à
relancer la console : l'unité reste alors en échec et le Pi reste joignable
pour le diagnostic ([section 8](#8-limites-et-reste-à-faire)).

**Ce qui est vérifié, et ce qui ne l'est pas.** A tourné avec le vrai systemd
et le vrai Docker de Debian 12, dans la machine de remplacement, en
simulation ([section 5](#5-ce-que-la-ci-vérifie)) : le démarrage par le relais
et le signe de vie (étape 1), la console figée puis réveillée par son arrêt
(étape 9), les morts répétées, le mode hors service et la remise en service
(étape 10). N'a jamais tourné sous systemd : une console qui ne répond à rien
(la colonne de droite du tableau), un relais qui ne démarre pas, la sortie
d'une console sur une exception de son tic. Sur un vrai Pi : rien. En
particulier que `/dev/watchdog0` existe, que systemd l'arme, et qu'un Pi figé
redémarre, ne se constate que sur la machine
([section 6](#6-installer-un-vrai-raspberry-pi), étape 7).

### L'arrêt

`systemctl stop anheart` envoie SIGTERM à la console (`docker stop`), qui met
la consigne à zéro et rend la liaison au variateur, puis attend 60 s avant de
forcer. Arrêter le Pi proprement (`sudo poweroff`) passe par le même chemin.

La ligne d'arrêt attend ensuite, dix secondes au plus, que le processus
principal de l'unité, le client Docker, se termine de lui-même : il sort avec
le code de la console un instant après elle. Dès que cette ligne rend la
main, systemd termine ce qui reste de l'unité ; un client qu'il aurait dû
terminer lui-même rapporterait ce signal à la place du code de la console.
Sans cette attente le résultat dépendait de quelques millisecondes, et la
console, dont le conteneur en lecture seule se retire plus vite qu'avant,
perdait parfois : vu dans la machine de remplacement (l'arrêt restait propre,
le code lu ne l'était pas).

Le test de bout en bout arrête le service **pendant une séance simulée**
(étape 4) : la console écrit elle-même `shutdown complete` dans le journal,
sort avec le code 0 en quelques secondes, systemd constate un arrêt propre,
l'enregistrement de la séance est clos (raison `shutdown`, sommes de contrôle
écrites), et au démarrage suivant la console est au repos, sans attestation.
En simulation seulement : la descente du vrai moteur pendant cet arrêt reste à
constater sur la machine.

### Une seule console à la fois

Le conteneur de la console s'appelle toujours `anheart`, et Docker refuse deux
conteneurs du même nom : le service ne peut pas lancer une seconde console à
côté d'une première.

Si un conteneur de ce nom existe quand le service démarre, ou subsiste quand
il s'arrête (le client Docker du service est mort en laissant la console en
marche, ou quelqu'un a lancé l'image à la main), **l'unité l'arrête par l'arrêt
contrôlé (SIGTERM, 60 s) et le retire avant de lancer le sien**. Qui pose la
question « au repos ? » :

| Qui démarre le service | La console trouvée est-elle interrogée avant d'être arrêtée ? |
|---|---|
| `scripts/install.sh` | **oui**, deux fois : avant de toucher à quoi que ce soit, puis juste avant le démarrage. En séance, ou sans réponse : le script refuse et rien n'est arrêté (test de bout en bout, étape 7) |
| `sudo systemctl start anheart` tapé à la main, ou la relance automatique après la mort du client Docker | **non**. Une console trouvée en séance est arrêtée sur sa rampe contrôlée : la séance s'arrête. C'est un arrêt, jamais un départ, et jamais un arrêt brutal |

C'est une limite assumée : l'unité ne lit pas la configuration de la machine
et ne sait donc pas interroger la console. Avant un `systemctl start` à la
main, vérifier qu'aucune console ne tourne : `sudo docker ps --all`.

### Outils de banc et de diagnostic : arrêter le service d'abord

**Aucun outil de banc ou de diagnostic (`bench_console.py`,
`bench_comm_latency.py`, `probe_atv320.py`, `scan_modbus.py`, SoMove) sur une
machine dont le service tourne.** Le service partage désormais le verrou du
câble avec l'hôte. Un outil Anheart coopérant est refusé avant d'ouvrir le
transport ; un outil tiers qui ignore ce verrou reste hors de cette garantie
([README du Pi](../raspberry-pi/README.md#sur-un-raspberry-pi-installé--le-verrou-est-partagé)).

```sh
sudo systemctl stop anheart
systemctl is-active anheart     # doit répondre : inactive
sudo docker ps --all            # ne doit lister aucun conteneur anheart
```

Lancer l'outil seulement ensuite. Après l'intervention : fermer l'outil, puis
`sudo systemctl start anheart`.

### Allumage et redémarrage : rien ne repart seul

Le service démarre la console, et rien d'autre. À l'allumage, ou après un
redémarrage par systemd, la console est **au repos** :

* elle ne lance aucune séance : une séance exige un départ explicite, et un
  départ exige que quelqu'un ait attesté le câblage de l'arrêt d'urgence
  **depuis ce démarrage**. Cette attestation vit dans la mémoire du processus
  et disparaît avec lui ;
* si elle trouve le variateur activé, elle commande zéro, verrouille, et attend
  un acquittement nommé ([raspberry-pi.md](raspberry-pi.md#5-le-superviseur-de-sécurité)) ;
* l'unité ne contient aucun appel à la console : elle lance une image.

Comment c'est vérifié :

| Quoi | Preuve |
|---|---|
| L'unité ne fait que lancer l'image ; elle ne redémarre pas sur une erreur de configuration (code 2) | `tests/test_pi_install.py` |
| Le service arrêté en pleine séance simulée, puis redémarré : console **au repos**, sans attestation | test de bout en bout, étape 4 |
| Une console tuée en pleine séance simulée est relancée par systemd **au repos**, sans attestation | test de bout en bout, étape 5 |
| Après l'arrêt puis le rallumage de la machine de remplacement, la console revient seule, **au repos**, sans attestation | test de bout en bout, étape 8 |
| Le script d'installation ne remplace pas une console qui n'est pas au repos, que le service l'ait lancée ou non | `tests/test_pi_install.py` et test de bout en bout, étapes 3 et 7 |
| Un variateur trouvé activé au démarrage est mis à zéro et verrouillé | gate du Pi (`tests/test_runtime.py` : `test_a_drive_found_already_enabled_is_stopped_latched_and_refused`, `test_a_drive_found_enabled_while_idle_is_stopped_latched_and_disabled`) |
| Une console figée en pleine séance simulée est arrêtée par le chien de garde du service et relancée **au repos**, sans attestation | test de bout en bout, étape 9 |
| Après une exception dans le tic, une boucle bloquée ou une console tuée, la console relancée trouve le variateur simulé **laissé en marche**, le met à zéro et verrouille ; rien ne bouge avant une attestation, un acquittement nommé, un départ et une cible | gate du Pi (`tests/test_supervised_console.py`, les quatre tests `test_ex4_…`) |
| Une console qui redémarre en boucle revient hors service et n'arme rien | gate du Pi (`tests/test_out_of_service_console.py`) et test de bout en bout, étape 10 |

Règles de redémarrage de l'unité : relance 2 s après une sortie en erreur, un
arrêt par le chien de garde ou une mort (`Restart=on-failure`, `RestartSec=2`),
dans la limite de dix démarrages par demi-heure
([Le chien de garde](#le-chien-de-garde)) ; jamais après une sortie avec le
code 2 (configuration refusée : la console liste les problèmes et sort ; les
lire avec `journalctl -u anheart`).

Non couvert : ces vérifications se font en simulation. Le comportement avec le
vrai variateur à l'allumage reste à constater sur la machine. L'étape 8 est un
arrêt **ordonné** de la machine de remplacement : une coupure franche de
l'alimentation, en plein milieu d'une écriture, n'est pas simulée. C'est
l'étape 6 de la [marche à suivre sur un vrai Pi](#6-installer-un-vrai-raspberry-pi).

### L'heure

**L'horloge murale ne sert qu'à dater.** La console ne compte aucune durée
dessus : délais, rampes et règles de sécurité lisent l'horloge monotone, qu'un
changement d'heure ne touche pas
([raspberry-pi.md](raspberry-pi.md#124-lhorloge-murale-et-le-journal-de-la-console)).
Elle date ce que la console écrit : le début d'un enregistrement de séance, une
ligne du journal.

Un Raspberry Pi 4 n'a pas d'horloge sauvegardée par une pile. Après une coupure
de courant il redémarre à l'heure de son dernier arrêt, et se trompe de toute
la durée de la coupure tant qu'il n'a pas retrouvé une source de temps.

| Ce que fait l'installation | Pourquoi |
|---|---|
| installe `chrony` | un seul service de temps : le paquet `chrony` prend la place de `systemd-timesyncd`, celui de Raspberry Pi OS (les deux paquets s'excluent). `chrony` plutôt que lui, comme le demandait le ticket, pour deux raisons : il publie au noyau l'erreur qu'il estime et celle qu'il garantit, que la console lit sans parler à aucun service ; et il tient à l'heure une horloge temps réel quand la machine en a une. Ces deux comportements sont ceux que décrit sa documentation : ni l'un ni l'autre n'a été observé sur un Pi |
| écrit `/etc/chrony/conf.d/anheart.conf` | les deux directives dont dépend ce que lit la console : `makestep 1 3` (l'heure est corrigée d'un coup, et non lentement, quand elle est fausse de plus d'une seconde à l'une des trois premières corrections après le démarrage de `chrony`) et `rtcsync` (une fois synchronisé, `chrony` le dit au noyau, et le noyau recopie l'heure dans l'horloge temps réel toutes les 11 minutes). Les serveurs de temps sont ceux du fichier de Debian (`2.debian.pool.ntp.org`) |
| dit si une horloge temps réel est là | il lit `/sys/class/rtc/rtc0`. Présente : le noyau la lit à l'allumage et `chrony` la tient à l'heure, sans autre réglage. Absente : il le dit |

**Ce que la console en dit.** Sur le Pi, toutes les 10 secondes, hors de la
boucle de contrôle, elle demande au noyau ce que vaut l'horloge : la question que pose
`timedatectl` pour afficher `System clock synchronized`, jugée par la même
règle. Sa réponse est sur la page (ligne `horloge` sous les pastilles), dans le
manifeste de chaque enregistrement (`clocks.wall_trusted`) et dans son journal
(`wall clock: ...`, au démarrage et à chaque changement).

```sh
timedatectl                         # System clock synchronized: yes
chronyc tracking                    # la source, l'écart mesuré, l'erreur
chronyc sources                     # les serveurs joints
journalctl -u anheart | grep '"event": "wall clock'
```

**Avec une horloge temps réel et sans réseau, la console dit encore « NON
FIABLE ».** Elle ne fait confiance qu'à un service de temps synchronisé. Une
horloge temps réel remet le Pi à une heure plausible à l'allumage, mais rien
dans ce dépôt ne vérifie qu'elle est juste : une pile morte, ou l'horloge
intégrée d'un Pi 5 sans pile, donnent une heure lisible et fausse. La règle qui
ferait confiance à un module est à écrire quand un module sera choisi et
essayé ([section 8](#8-limites-et-reste-à-faire)).

### Le journal du système

Ce que la console écrit va dans le journal de systemd (`journalctl -u anheart`).
L'installation règle ce journal par `/etc/systemd/journald.conf.d/anheart.conf` :

| Réglage | Effet |
|---|---|
| `Storage=persistent` | le journal est écrit sous `/var/log/journal`, sur la carte : il survit à un redémarrage. Sans ce réglage, un système sans ce dossier le garde en mémoire et le perd |
| `SystemMaxUse=200M` | il n'occupe jamais plus de 200 Mo : les lignes les plus anciennes sont retirées. Sans ce réglage, sa limite est un dixième de la carte (jusqu'à 4 Go), sur la carte qui porte aussi les enregistrements de séance, dont un départ exige 500 Mo libres |

`journald` lit ce fichier à son démarrage : le script le redémarre quand il
vient de l'écrire, sans toucher à la console (systemd garde le fil par lequel
elle écrit).

**Chaque ligne de la console est un objet JSON**, avec quatre champs (`ts`,
`level`, `logger`, `event`) et un cinquième, `exception`, quand il y a une
trace d'erreur ([raspberry-pi.md](raspberry-pi.md#124-lhorloge-murale-et-le-journal-de-la-console)) :

```sh
journalctl -u anheart -o cat                                 # les lignes telles qu'écrites
journalctl -u anheart -o cat | grep '"level": "ERROR"'
journalctl --disk-usage                                      # la place occupée
```

Deux choses à savoir :

* **Une coupure de courant peut emporter les dernières minutes du journal.**
  `journald` ne force l'écriture sur le disque que toutes les 5 minutes, et tout
  de suite pour un message critique. Ce n'est pas le cas de l'enregistrement de
  séance, poussé sur le disque toutes les 2 secondes
  ([raspberry-pi.md](raspberry-pi.md#152-aucune-écriture-dans-la-boucle)) : c'est lui
  la trace d'une séance, pas le journal.
* **Ce que le script d'entrée de l'image écrit avant la console** (une ou deux
  lignes sur la liaison RFCOMM) et la trace d'une erreur qui empêcherait
  Python de démarrer ne sont pas en JSON.

### Sous quel compte

**La console ne tourne plus sous root.** Il y a deux moitiés : ce que systemd
lance sur le Pi (le relais du chien de garde et le client Docker), et ce qui
tourne dans le conteneur (la console, c'est-à-dire le processus auquel un
navigateur parle).

| Processus | Compte | Ce qu'il peut faire |
|---|---|---|
| La console, dans son conteneur | `anheart`, par ses numéros sur cette machine (`--user`), plus le groupe `dialout` | aucune capacité (`--cap-drop ALL`), aucun moyen d'en gagner (`no-new-privileges`) ; image en lecture seule (`--read-only`), `/tmp` en mémoire, 64 Mo, sans exécution ; écrit dans `/var/lib/anheart` et nulle part ailleurs |
| Le relais du chien de garde (`socat`) | `anheart` | lit ce que la console lui envoie, le passe à systemd ; système de fichiers en lecture seule sauf `/run/anheart` |
| Le client Docker de l'unité (`docker run`, `docker stop`) | **root** | voir [ce qui reste sous root](#ce-qui-reste-sous-root) |
| L'étape qui lie le nœud série du capteur (`bind-bitalino`) | **root** | une commande `rfcomm`, avant la console, puis se termine |

**Ce dont chaque besoin dépend.** Établi en lisant le code de la console ; la
colonne de droite dit comment c'est vérifié sans matériel.

| Besoin de la console | Ce qu'il lui faut | Ce qui le lui donne | Vérifié |
|---|---|---|---|
| Câble du variateur vu par le noyau (`MOTOR_PORT=/dev/ttyUSB0`) | ouvrir un périphérique de classe 188, qui appartient au groupe `dialout` | `--device-cgroup-rule "c 188:* rw"` et `--group-add` | test de bout en bout : l'ouverture d'un nœud de classe 188 du groupe `dialout` est permise (il n'y a pas de matériel derrière lui), la même à root seul est refusée |
| Câble Schneider sans pilote série (`MOTOR_PORT=ftdi://schneider:rs485/1`) | écrire sur le périphérique USB lui-même (classe 189), qui par défaut n'est inscriptible que par root | la règle `udev` écrite par l'installation (16de:0003 au groupe `dialout`) et `--device-cgroup-rule "c 189:* rw"` | la règle est écrite et relue ; **son effet ne se voit qu'avec le câble** |
| BITalino par nœud série (`BITALINO_ADDRESS=/dev/rfcomm0`) | que le nœud existe : le créer demande un droit sur le réseau de la machine (`CAP_NET_ADMIN`) ; puis l'ouvrir (classe 216, groupe `dialout`) | le nœud est lié **sur le Pi, par l'unité, sous root**, avant la console (`bind-bitalino`, depuis `BITALINO_MAC`) ; la console n'a que `--device-cgroup-rule "c 216:* rw"` | le script est exécuté avec un `rfcomm` scripté (gate du Pi) ; **la liaison réelle ne se voit qu'avec le capteur** |
| Appairage du BITalino (`bluetoothctl`, par le D-Bus du système) | rien dans le conteneur : il se fait une fois sur le Pi, `bash scripts/pair_device.sh` | **le D-Bus de la machine n'est plus monté dans le conteneur** | l'unité ne le monte pas (gate du Pi) |
| Dossier des données et des enregistrements | en être propriétaire | `/var/lib/anheart` appartient au compte ; deux montages | test de bout en bout : une séance est enregistrée ; tout ce qui est écrit appartient au compte, fermé aux autres |
| Signe de vie vers systemd | écrire sur le socket du relais | le socket et son dossier sont au compte | test de bout en bout : le chien de garde est nourri ; console figée relancée |
| État de l'horloge de la machine | une question au noyau qui ne change rien (`adjtimex`) | aucun droit particulier | test de bout en bout : la console et `timedatectl` disent la même chose |
| Page de la console | écouter sur un port au-dessus de 1024, réseau de la machine (`--network host`) | aucun droit particulier | test de bout en bout : `/healthz` répond |
| Caméra | aucun périphérique : seules des caméras simulées existent dans le code | rien | |

**Pourquoi tout `/dev` est encore monté.** Les nœuds du câble et du capteur
apparaissent et disparaissent avec eux : le câble se rebranche, le nœud du
capteur n'existe qu'une fois lié. Les nommer un par un (`--device`)
empêcherait la console de démarrer en leur absence, alors qu'elle doit
démarrer et **dire** que la liaison manque. Le conteneur voit donc les noms de
tous les périphériques de la machine, et n'en ouvre que trois classes : pour
tout le reste, le noyau refuse, quels que soient les droits du fichier (test
de bout en bout, étape 1).

**Une image lancée autrement que par le service** (à la main, par un essai)
ne tourne pas sous root non plus : l'image nomme son propre compte, `anheart`
(numéro 10001). Elle n'a alors ni les périphériques ni les données de la
machine, sauf à les lui donner.

#### Ce qui reste sous root

Dit sans détour, parce qu'aucune option de l'unité ne le change :

* **Le démon Docker est à root, et qui peut lui demander un conteneur peut
  tout lui demander.** Le client Docker de l'unité est donc root, de fait. Les
  options de l'unité (`NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome`) ferment ses propres processus : aucun ne peut écrire ailleurs
  que dans `/run/anheart`, ni lire un dossier personnel. Elles contraignent le
  relais. **Elles ne bornent pas ce que le client peut demander au démon.**
  Ce client ne lit rien qui vienne de la console, sinon ce qu'elle affiche,
  qu'il recopie au journal.
* `ReadWritePaths=/var/lib/anheart`, que le ticket nommait, n'est pas posé :
  les processus de l'unité n'y écrivent pas. C'est le démon qui monte ce
  dossier dans le conteneur, et la console qui y écrit.
* L'unité ne porte ni `User=` ni `Group=` : les écrire ferait croire que le
  client Docker n'est pas root.
* **L'installation, le durcissement et la désinstallation** se lancent avec
  `sudo`.

En sortir demanderait un Docker sans root, ou de lancer la console sans
Docker : ni l'un ni l'autre n'est prévu par un ticket.

### Le certificat de la console

Hors de la boucle locale, la console ne sert sa page qu'en TLS, ou ne démarre
pas ([raspberry-pi.md, 12.5](raspberry-pi.md#125-la-page-en-tls-hors-de-la-boucle-locale)).
Le certificat qu'elle présente est fait **sur le Pi**, à la mise en service,
par `scripts/pi/make_console_tls.sh`.

> **État réel au 10 octobre 2026.** La console et le script sont écrits et
> testés (gate du Pi, sur la boucle locale d'une machine de test). **Sur un Pi
> installé, rien de ceci n'est encore actif** : la page y écoute sur la boucle
> locale, en clair, comme avant ; l'unité ne monte pas le dossier du
> certificat dans le conteneur et `scripts/install.sh` n'appelle pas le
> script. Ouvrir la console au réseau que le Pi crée (point d'accès, pare-feu,
> intégration du certificat au service) est le reste d'ANH-151. Le compte non privilégié est déjà en place. Aucune tablette réelle n'a
> installé d'autorité ni ouvert la page en TLS : la liste de ce qui reste à
> essayer est [plus bas](#ce-qui-reste-à-décider-et-à-essayer).

#### Ce qui a été décidé, et pourquoi

| Décision | Raison |
|---|---|
| Une autorité de certification **par Pi**, créée sur ce Pi | Une autorité commune à toutes les machines, dont la clé privée serait sur chaque Pi ou dans ce dépôt public, permettrait à quiconque tient un Pi de fabriquer un certificat accepté par les tablettes de toutes les autres. Ici l'autorité d'une machine ne reconnaît que cette machine, et le dépôt ne contient ni certificat ni clé |
| La clé privée de l'autorité **n'est pas gardée** | Elle signe un seul certificat, celui de la console, puis disparaît : elle n'existe que dans la mémoire du script, passe à `openssl` par son entrée standard, et n'est écrite dans aucun fichier ni sur aucune ligne de commande. Ce qu'une tablette accepte en installant l'autorité se réduit donc à **un certificat qui existe déjà**. Gardée sur le Pi, même lisible par root seul, elle aurait été une clé au repos avec laquelle signer, aux yeux de la tablette, un certificat pour n'importe quel site ; la console ne tourne plus sous root ([plus haut](#sous-quel-compte)), mais une compromission de root donnerait encore accès à une clé conservée |
| Le prix : renouveler, c'est changer d'autorité | Un nouveau certificat vient avec une nouvelle autorité, que chaque tablette installe de nouveau. D'où la durée la plus longue que les tablettes acceptent, et un renouvellement qui se prépare ([plus bas](#renouveler)) |
| Durée : 825 jours | La limite d'iOS et de macOS pour tout certificat de serveur, quelle que soit l'autorité qui l'a signé. L'autorité vit un jour de plus que le certificat |
| Algorithmes : ECDSA P-256, SHA-256 | Acceptés par OpenSSL 3.0 (Debian 12) et par les navigateurs actuels. C'est aussi la signature la moins coûteuse pour un Pi, et la poignée de main TLS tourne sur la boucle de la console |
| Noms portés par le certificat | Ceux que la tablette tape : l'adresse fixe du point d'accès et un nom local, donnés au script par `--name`. Par défaut `10.42.0.1` (l'adresse que NetworkManager donne à un Pi qui partage sa connexion en point d'accès) et `anheart.internal` (une zone réservée aux usages privés). **Ces deux valeurs sont des hypothèses tant que le point d'accès n'est pas fait.** `127.0.0.1` et `localhost` sont toujours ajoutés : une console qui écoute sur toutes les adresses peut ainsi être vérifiée depuis le Pi lui-même |
| Fichiers, dans `/etc/anheart/tls/` (mode 750) | `authority.crt` : le certificat de l'autorité, **public**, le seul fichier qu'une tablette installe. `console.crt` : le certificat de la console, public. `console.key` : sa clé privée, mode 600, qui ne quitte jamais le Pi. Le dossier et les fichiers appartiennent au compte donné par `--owner` (celui sous lequel tourne la console) ; sans cette option, à celui qui lance le script |

Le script s'arrête sans rien écrire si l'horloge du Pi n'a jamais été réglée
(une date d'avant octobre 2026) : un certificat daté d'une telle horloge serait
refusé par les tablettes. Vérifier l'heure d'abord ([L'heure](#lheure)).

#### Faire le certificat

Sur le Pi, console au repos :

```bash
date                                   # l'heure du Pi doit être la bonne
sudo bash scripts/pi/make_console_tls.sh --dry-run --name 10.42.0.1 --name anheart.internal
sudo bash scripts/pi/make_console_tls.sh --name 10.42.0.1 --name anheart.internal
```

Le script écrit les trois fichiers, puis affiche les noms, les dates de
validité, **l'empreinte SHA-256 de l'autorité** (à noter sur la fiche de mise
en service) et les trois lignes à mettre dans la configuration de la console
(`UI_TLS_CERT`, `UI_TLS_KEY`, `UI_TLS_NAMES`). Il ne modifie aucune
configuration et ne redémarre rien : la console lit son certificat une fois,
à son démarrage.

Le relancer ne change rien à ce qui est en place et encore bon (code 0). Ce
qui est en place et ne l'est plus (expiré, autre nom, clé qui ne correspond
pas) n'est **jamais** remplacé sans `--force`, parce que le remplacer change
l'autorité et donne donc quelque chose à faire sur chaque tablette.
`--check` juge sans rien écrire et sort en code 1 si le certificat manque, est
inutilisable, ou expire dans moins de `--min-days` jours (30 par défaut).

#### Mettre l'autorité sur la tablette

`authority.crt` est un fichier public :
ce qui compte n'est pas qu'il reste secret, mais que la tablette installe bien
celui de ce Pi.

1. Sortir le fichier du Pi par un chemin tenu par la personne qui met en
   service : une clé USB branchée sur le Pi
   (`sudo cp /etc/anheart/tls/authority.crt /media/<clé>/`), ou une copie par
   SSH vers son poste.
2. L'apporter à la tablette : la clé USB, un câble, ou l'outil de gestion des
   appareils de l'établissement, qui est la voie normale pour des tablettes
   gérées.
3. **Avant de lui faire confiance, comparer l'empreinte SHA-256** que la
   tablette affiche pour ce certificat avec celle notée à l'étape précédente.
   C'est cette comparaison, et elle seule, qui dit que le fichier est celui
   du Pi, quel que soit le chemin qu'il a pris.

Ce qu'il ne faut pas faire : le télécharger depuis le réseau de la console
sans comparer l'empreinte, ni passer l'avertissement du navigateur « pour cette
fois » à la place de l'installation. Tant que l'autorité n'est pas installée,
rien ne distingue la console d'un autre appareil qui répondrait à sa place ;
c'est précisément ce que l'installation règle. La console elle-même ne
distribue pas son autorité.

#### Renouveler

`sudo bash scripts/pi/make_console_tls.sh --check --min-days 90` dit si le
certificat tient encore 90 jours, et affiche ses dates. Pour renouveler sans qu'aucune tablette ne
perde la console :

1. faire le nouveau certificat **à côté** de l'ancien :
   `sudo bash scripts/pi/make_console_tls.sh --dir /etc/anheart/tls-suivant --name ...` ;
2. installer la nouvelle autorité sur chaque tablette (elles ont alors les
   deux) ;
3. séance terminée, pointer `UI_TLS_CERT` et `UI_TLS_KEY` vers le nouveau
   dossier et redémarrer la console, au repos ;
4. retirer l'ancienne autorité de chaque tablette et supprimer l'ancien
   dossier du Pi.

Remplacer sur place (`--force`) est possible aussi : le script affiche alors
l'empreinte de l'autorité remplacée, à retirer des tablettes, et celles-ci
voient l'avertissement du navigateur jusqu'à ce que la nouvelle soit
installée.

#### Une tablette perdue ou volée

Elle ne détient, pour TLS, que le certificat
public de l'autorité : rien à révoquer de ce côté. Elle connaît en revanche ce
qu'on lui a saisi : changer le jeton de la console (`UI_TOKEN`) et, le jour où
il existera, le mot de passe du point d'accès. **Un Pi ou une carte SD perdus**
emportent la clé privée de la console : faire un certificat neuf sur la
machine de remplacement et **retirer l'ancienne autorité de chaque tablette**.
Il n'existe pas d'autre révocation que ce retrait.

#### Ce qui reste à décider et à essayer

| Quoi | Qui |
|---|---|
| Installer l'autorité sur une vraie tablette et ouvrir la page, **WebSocket comprise** (la ligne d'état doit vivre, pas seulement la page s'afficher). Attendu, non essayé : sur iOS, l'installation du profil puis, à part, l'activation de la confiance totale pour cette autorité dans les réglages de confiance des certificats ; sur Android, l'installation comme « certificat CA », avec l'avertissement du système qui reste affiché ensuite. Chrome et Safari suivent les autorités installées par l'utilisateur ; Firefox pour Android ne les suit pas sans réglage. Un certificat seulement « accepté pour cette fois » dans le navigateur ne vaut pas installation : selon le navigateur, la WebSocket reste refusée | une personne avec le matériel |
| Que l'empreinte SHA-256 soit bien lisible sur la tablette avant d'accorder la confiance, et où | une personne avec le matériel |
| L'adresse du point d'accès et le nom local, et que la tablette résolve ce nom sur un réseau sans accès à Internet. L'adresse est l'URL sûre ; le nom dépend du DNS du point d'accès | lot « point d'accès » d'ANH-151, puis une personne avec le matériel |
| La procédure de renouvellement : sa période (le certificat dure 825 jours), qui la fait, et où l'échéance est suivie. Rien ne prévient aujourd'hui de l'échéance en dehors de `--check` | chef de projet ; ANH-115 pour la fiche de mise en service |
| Le dossier `/var/lib/anheart/tls` est monté en lecture seule sous `/run/anheart-tls`, appartient au compte de la console et est créé à la mise en service | logiciel fait par ANH-223 ; essai installé au banc à faire |
| L'installateur et le contrôle de santé utilisent HTTPS avec l'autorité locale et l'adresse configurée ; le Pi doit réellement posséder `10.42.0.1` | logiciel fait par ANH-223 ; essai installé au banc à faire |
| La console écoute sur `10.42.0.1` ; le service demande l'activation du profil AP avant de lancer le conteneur, avec un délai de 60 s | logiciel fait ; disponibilité de la radio à vérifier au banc |
| Le pare-feu borne les nouvelles connexions TLS à 32 simultanées par adresse IPv4, sans couper celles déjà établies | ANH-223 ; résistance à une charge hostile sur le Pi à mesurer |

---

## 5. Ce que la CI vérifie

Le workflow `.github/workflows/pi-install.yml` lance
`raspberry-pi/scripts/pi/test_install.sh` sur un runner **arm64**. Ce script :

1. démarre une machine de remplacement : un conteneur Debian 12 avec systemd,
   sans rien d'Anheart, comme un Raspberry Pi OS Lite juste flashé ;
2. y copie le dossier `raspberry-pi/` et y exécute **le vrai**
   `scripts/install.sh --simulation`, en root ;
3. vérifie, dans cet ordre :

| Étape | Ce qui doit être vrai |
|---|---|
| 1. Première installation | service activé au démarrage et en marche ; `/healthz` répond ; console au repos, sans attestation ; `systemctl status anheart` montre la version ; l'image porte la même version et tourne sous Python 3.12 ; compte, dossiers et droits en place ; configuration en simulation, sans clé de machine ; le journal contient la ligne de démarrage de la console ; `chrony` est installé, activé au démarrage, avec ses deux directives ; la console, dans son conteneur, lit l'état de l'horloge de la machine et dit la même chose que `timedatectl` ; le journal est sous `/var/log/journal` et `journald` annonce lui-même sa borne de 200 Mo ; le contrôle de santé de l'image passe à `healthy` ; systemd lit l'unité comme prévu (`Type=notify`, 10 s de chien de garde, relance à 2 s, limite de dix démarrages) ; plus de 15 s sans redémarrage ni `Watchdog timeout` : les notifications de la console arrivent à systemd par le relais ; la console se dit surveillée, sans aucun envoi manqué, et en service ; son état de redémarrage est écrit, au compte `anheart` seul, à côté des enregistrements ; le réglage du chien de garde matériel est là où systemd le lit. **Ce avec quoi la console tourne**, demandé au système en marche : dans son conteneur elle est le compte `anheart` et a le groupe `dialout` ; le conteneur n'est pas privilégié, est en lecture seule, toutes capacités retirées ; le noyau ne donne au processus aucune capacité (`CapPrm`, `CapEff`, `CapBnd` à zéro) et `NoNewPrivs` vaut 1 ; elle ne peut pas écrire dans son image, peut écrire dans `/tmp`, où rien ne s'exécute ; l'ouverture d'un périphérique de classe 188 du groupe `dialout` lui est permise (sans matériel derrière, elle n'aboutit pas), le noyau lui refuse (`EPERM`) un périphérique d'une autre classe aux mêmes droits, et un périphérique de classe 188 à root seul lui est refusé (`EACCES`) ; l'image nomme un compte qui n'est pas root. Le relais du chien de garde tourne sous le compte `anheart`, dans un dossier où lui seul entre. Les processus de l'unité ne peuvent ni gagner un privilège, ni écrire dans `/etc`, ni lire le dossier de root ; le client Docker de l'unité est root, et le test le dit. Le compte `anheart` ne peut pas lire la configuration. Les trois services de mise à jour automatique sont masqués et arrêtés, et `apt` lui-même dit qu'il ne met plus rien à jour seul, alors que le fichier de la distribution dit le contraire. Tout ce qui est sous `/var/lib/anheart` appartient au compte, fermé aux autres |
| 2. Deuxième installation | un jeton est écrit dans la configuration ; le script ne l'affiche pas, ne réécrit pas le fichier, ne redémarre pas la console ; le `curl` de Debian 12 envoie bien un en-tête lu sur son entrée standard ; un réglage du journal modifié fait redémarrer `journald` et pas la console, dont les lignes suivantes arrivent encore au journal ; dans la même relance, un fichier de la console rendu à root (l'état de ses redémarrages) et une configuration rendue lisible par tous sont corrigés, **sans que leur contenu change** ; le script dit qu'il n'y avait rien d'autre à protéger |
| 3. Séance en cours | une séance manuelle simulée tourne ; son enregistrement s'écrit dans `/var/lib/anheart/records`, avec la version de l'image ; une installation qui remplacerait la console **refuse** et ne touche à rien |
| 4. Service arrêté en pleine séance | `systemctl stop anheart` rend la main en moins de 60 s ; systemd constate un arrêt propre (code 0) ; le journal porte le `shutdown complete` de la console ; l'enregistrement est clos, raison `shutdown` ; la console qui s'arrête joint encore son relais ; aucun conteneur ne reste ; redémarrée, la console est au repos, sans attestation |
| 5. Arrêt brutal | la console est tuée en pleine séance ; systemd la relance ; elle est au repos, sans attestation |
| 6. Au repos | la même installation remplace la console par la nouvelle version ; l'image précédente reste sur la machine |
| 7. Console que le service n'a pas lancée | un conteneur `anheart` lancé à la main tourne une séance : l'installation **refuse**, ne le touche pas, ne démarre pas le service ; ce conteneur lancé à la main ne tourne pas sous root non plus ; une fois la séance terminée, l'installation passe, l'unité arrête et retire ce conteneur puis lance le sien |
| 8. Arrêt puis rallumage | la machine de remplacement est arrêtée (arrêt ordonné) puis rallumée ; la console revient seule, au repos ; une ligne écrite dans le journal avant l'arrêt y est encore, et les lignes de la console d'avant l'arrêt aussi |
| 9. Console figée en pleine séance | la console est figée (`SIGSTOP` : elle ne termine plus de tic) ; pas avant 10 s, systemd écrit `Watchdog timeout (limit 10s)!` et arrête l'unité, ce qui réveille la console ; elle dit elle-même que son arrêt n'est pas propre et sort en erreur (`status=1/FAILURE`) ; systemd constate l'échec `watchdog` et lance une autre console ; l'enregistrement de la séance figée est clos ; la nouvelle console est au repos, sans attestation, se dit démarrée après un arrêt non propre, compte ce démarrage, et le journal hors séance porte l'avertissement |
| 10. Console qui meurt en boucle | la console est tuée jusqu'à son troisième démarrage après un arrêt non propre en dix minutes : elle tourne, répond, se dit hors service, et le journal le porte ; un départ demandé n'est pas armé ; `POST /api/service/restore`, avec un nom, la remet en service, sans rien lancer ; une séance démarre alors ; après un redémarrage propre elle est toujours en service ; tout ce que la console a écrit pendant ces dix étapes appartient au compte `anheart`, fermé aux autres, et aucune notification n'a manqué son relais |
| 11. Machine qui a dérivé | à la main : un fichier laissé par root dans les données, l'état des redémarrages rendu à root, le journal hors séance et le dossier des enregistrements ouverts, la configuration lisible par tous, un shell donné au compte, une mise à jour automatique rétablie. `harden.sh --dry-run` annonce ces neuf changements, une ligne chacun, et **n'en fait aucun** ; `harden.sh` les fait, sans toucher à un contenu ni à la console ; un second essai à blanc ne trouve plus rien |
| 12. Désinstallation, puis installation | `uninstall.sh` : plus de console, plus d'unité, plus aucun des réglages écrits pour elle ; les mises à jour automatiques sont de nouveau celles de la distribution ; la configuration, les enregistrements, le compte et l'image sont intacts, `chrony` est toujours là. La même installation ramène ensuite la console, au repos |

Rien n'y touche du matériel ni un tableau de bord : variateur simulé, ECG
simulé, clé de machine vide. Trois choses n'y sont pas. Une console qui ne
répond à rien, pas même à son arrêt : `SIGSTOP` est levé par l'arrêt de
systemd, et aucun geste extérieur ne bloque la console autrement. Le variateur simulé vit
dans le processus de la console et meurt avec lui : ce qu'une console relancée
fait d'un variateur **laissé en marche** est vérifié ailleurs, par
`tests/test_supervised_console.py`, où le variateur simulé survit à la console.
Et le chien de garde matériel n'est pas exercé : le systemd de la machine de
remplacement reçoit `/dev/null` comme périphérique de chien de garde, pour ne
jamais armer celui de la machine qui fait tourner le test.

**Ce que la machine de remplacement ne montre pas de l'heure.** Elle partage
l'horloge de la machine qui lance le test : `chrony` y est installé et activé,
mais qu'il y tourne et s'y synchronise n'est pas exigé, et une heure fausse au
démarrage n'y est pas provoquée. Le remplacement de `systemd-timesyncd` par
`chrony`, la correction de l'heure au retour du réseau, une horloge temps réel
et une coupure de courant ne se voient que sur un vrai Pi
([section 6](#essai-de-coupure-secteur--à-faire-au-banc)). Ce que la console
fait d'une horloge que personne ne tient (elle démarre, prévient au banc,
refuse une personne à bord, l'écrit dans le manifeste) est prouvé par la gate
du Pi, avec un état d'horloge décidé par le test
(`tests/test_wall_clock_console.py`).

**Les scripts sont relus avant d'être lancés.** La première étape du job
passe ShellCheck sur les scripts qui tournent sous root sur un Pi
(`install.sh`, `harden.sh`, `uninstall.sh`, `pi/bind-bitalino.sh`,
`pi/preflight.sh`, `pair_device.sh`), sur le script d'entrée de l'image et sur
le test lui-même : un avertissement ou une erreur fait échouer le job. Ses
remarques de style ne sont pas retenues.

**Quand il tourne.** Sur une PR qui modifie de quoi l'installation est faite
(`Dockerfile`, fichiers `requirements*.txt`, `scripts/install.sh`,
`scripts/harden.sh`, `scripts/uninstall.sh`, `scripts/anheart.service`,
`scripts/pi/bind-bitalino.sh`, le script de test, `.env.pi.example`, `VERSION`,
`docker/`, le workflow), et à chaque push sur `develop`, où une modification
des sources de la console qui casserait son démarrage dans l'image est vue.
La pipeline de release l'appelle aussi, sur le commit de `main` qu'elle
s'apprête à publier : un push sur `main` ne le lance pas, et aucun tag n'est
posé si l'installation y échoue
([release.md](release.md#la-pipeline-de-release)).

**Ce n'est pas une gate.** Le job `pi-install` n'est pas une vérification
obligatoire de la branche `develop` : un workflow filtré par chemins qui ne se
déclenche pas laisserait une vérification obligatoire en attente. Le rendre
obligatoire est un réglage du dépôt, décidé par le chef de projet, et
demanderait d'abord de retirer le filtre. Une conséquence à connaître :
`scripts/release.sh` lit **toutes** les vérifications du commit de tête
([release.md](release.md#4-ce-que-fait-le-script)) ; un job `pi-install` en
échec ou en cours sur la tête de `develop` fait donc refuser `prepare` et `pr`.

**Permissions.** Le workflow ne peut que lire le dépôt, ne lit aucun secret, ne
publie rien. Ses actions sont épinglées par commit complet, celle de checkout
sur le même commit que `ci.yml`.

**Durée.** Avec les douze étapes et la relecture des scripts (PR #73) :
environ 4 minutes pour le job. Avant le durcissement et le chien
de garde, sur la PR #37 (run 37607234436) : 2 min 40 s pour le job, dont
environ une minute pour construire l'image sur le runner arm64. Ce n'est pas la
durée sur un Raspberry Pi, qui reste à mesurer. À l'étape 9, la console figée
a été remplacée 13 s après le gel (exécution 38072378030) : les 10 s du chien
de garde, l'arrêt de la console réveillée, les 2 s d'attente et le démarrage.

**Le lancer soi-même.** `bash raspberry-pi/scripts/pi/test_install.sh`, sur une
machine avec Docker. La machine de remplacement y construit l'image complète de
la console : compter environ 2 Go de disque, rendus à la fin.
`ANHEART_TEST_KEEP=1` laisse la machine en place pour l'inspecter.

**Les tests de la gate du Pi.** `tests/test_pi_install.py` exécute le vrai
script contre un dossier racine d'essai (`ANHEART_ROOT`), avec des commandes
système scriptées : première installation, relance sans effet, configuration
existante gardée et secrets jamais affichés, refus sous une console qui n'est
pas au repos (lancée par le service ou non), échec du test de démarrage. Il
exécute aussi le script d'entrée de l'image (la version donnée aux
enregistrements) et vérifie l'unité, les versions figées (dont cette page) et
le workflow. Pour le chien de garde : les réglages de l'unité, le fichier que
l'installation écrit pour le chien de garde matériel (sans redémarrer la
console pour lui), et la ligne de l'unité qui lance le relais, exécutée pour de
bon avec un relais et un `docker` scriptés (le relais est lancé sur le socket
de systemd, puis le shell devient `docker run` avec tous ses arguments et son
code de sortie). Pour le durcissement : ce que l'unité donne et ne donne pas à
la console, le relais lancé sous le compte, le démarrage qui échoue si son
dossier ne peut pas lui être donné, le script qui lie le nœud du capteur
exécuté avec un `rfcomm` scripté, et `harden.sh` et `uninstall.sh` exécutés
pour de bon contre le dossier d'essai : essai à blanc qui annonce et ne
change rien, modes réellement corrigés sans toucher aux contenus, lien
symbolique jamais suivi, reprise complète à la désinstallation. Aucun test
n'y tourne sous root : le changement de propriétaire y est annoncé et demandé,
et n'est constaté pour de bon que par le test de bout en bout.

---

## 6. Installer un vrai Raspberry Pi

**Non fait à ce jour.** Voici ce qu'une personne doit faire et constater, avec
un Raspberry Pi 4 ou 5 et une carte SD. Les étapes 1 à 7 ne demandent ni
variateur ni BITalino.

1. **Flasher l'image figée.** Télécharger
   `2026-10-06-raspios-bookworm-arm64-lite.img.xz`
   ([section 2](#le-système-du-pi)), vérifier son SHA-256
   (`shasum -a 256 <fichier>`), l'écrire sur la carte avec Raspberry Pi Imager
   (« Use custom »), en y réglant le nom d'utilisateur, le réseau et SSH.
2. **Premier démarrage.** Sur le Pi : `cat /etc/rpi-issue` doit commencer par
   `Raspberry Pi reference 2026-10-06`, et `uname -m` répondre `aarch64`.
3. **Copier les sources.** Depuis le poste de développement, sur le commit ou le
   tag voulu, dans `raspberry-pi/` : `bash scripts/pi/deploy.sh <utilisateur>@<pi>`.
   Le dossier arrive dans `~/anheart/raspberry-pi`.
4. **Installer en simulation.** Sur le Pi :
   `cd ~/anheart/raspberry-pi && sudo bash scripts/install.sh --simulation`.
   Le Pi doit avoir accès à Internet. Noter la durée de la construction de
   l'image. Le script doit finir par `Installed: console <version>`.
5. **Constater.**
   - `systemctl status anheart` : `active (running)`, et la version dans la
     première ligne ;
   - `systemctl is-enabled anheart` : `enabled` ;
   - `curl -fsS http://127.0.0.1:8090/healthz` : `{"status":"ok",...}` ;
   - `curl -s http://127.0.0.1:8090/api/status` : commence par
     `{"run_state":"idle"` et contient `"attested":false` ;
   - après une minute, `sudo docker inspect --format '{{.State.Health.Status}}' anheart` :
     `healthy` ;
   - `timedatectl` : `System clock synchronized: yes` et `NTP service: active`,
     et `systemctl is-active chrony systemd-timesyncd` : `active` puis
     `inactive` (ou `unknown`), pour le remplacement d'un service par l'autre ;
   - sur la page de la console, sous les pastilles : `horloge synchronisee`,
     suivie de deux erreurs en millisecondes ;
   - `journalctl --disk-usage` répond, et `ls /var/log/journal` montre un
     dossier.
6. **Couper et remettre l'alimentation**, sans rien taper. Une fois le Pi
   redémarré, refaire les constats de l'étape 5 : la console doit être revenue
   seule, au repos. **C'est le critère d'acceptation du ticket** : consigner
   dans le ticket la référence du système, la durée de construction, la sortie
   de `systemctl status anheart` et le résultat de cet essai.
7. **Constater le chien de garde**, toujours en simulation, rien de branché.
   - `ls -l /dev/watchdog0` : le périphérique existe ;
   - `systemctl show -p RuntimeWatchdogUSec` : `15s` ; et
     `journalctl -b | grep -i watchdog` doit porter la ligne où systemd dit
     quel chien de garde matériel il utilise (attendu : celui du BCM2835) ;
   - `systemctl show -p WatchdogUSec,NRestarts anheart` : `10s` et `0`, et
     `"watchdog":{"enabled":true` avec `"failed":0` dans
     `curl -s http://127.0.0.1:8090/api/panel` ;
   - figer la console : `sudo kill -STOP $(sudo docker inspect --format '{{.State.Pid}}' anheart)`,
     puis `journalctl -u anheart -f`. Attendu, d'après la machine de
     remplacement : `Watchdog timeout (limit 10s)!` 10 s après le gel ; l'arrêt
     de systemd réveille la console, qui écrit `not a clean stop` et sort
     (`status=1/FAILURE`) ; une nouvelle console quelques secondes plus tard
     (13 s après le gel dans la CI ; plus sur un Pi, où la console met plus
     longtemps à se charger), au repos (constats de l'étape 5). Sa page écrit
     sous les pastilles `demarre apres un arret non propre`. Noter les deux
     durées.

   Le déclenchement du chien de garde **matériel** (Pi entier figé) et les
   essais avec le variateur sont dans la procédure de banc, pas ici
   ([securite.md](securite.md#96-essai-au-banc--procédure-non-faite)).
8. **Passer au matériel réel**, quand le banc est prêt (jalon M3) :
   - `sudo nano /etc/anheart/anheart.env` : `MOTOR_BACKEND=serial`,
     `ECG_SOURCE=serial`, puis `MOTOR_PORT`, `BITALINO_MAC`, les rayons mesurés,
     `CONVEX_URL` et `MACHINE_API_KEY`
     ([deploiement.md](deploiement.md#74-configurer)) ;
   - appairer le BITalino : `bash scripts/pair_device.sh <adresse MAC>` ;
   - `sudo bash scripts/pi/preflight.sh` ;
   - `sudo systemctl restart anheart`, puis les constats de l'étape 5.
9. **Avant tout outil de banc ou de diagnostic sur cette machine** (banc,
   mesure de latence, sonde ou balayage Modbus, SoMove) : arrêter le service et
   vérifier qu'il est arrêté. La console ne refuse pas un outil lancé hors de
   son conteneur
   ([section 4](#outils-de-banc-et-de-diagnostic--arrêter-le-service-dabord)).

Ce que ces étapes ne couvrent pas : l'écran en plein écran au démarrage
([deploiement.md](deploiement.md#75-vérifier-puis-démarrer)) et ce que le
durcissement change pour le câble et le capteur, qui a son propre essai
([plus bas](#essai-du-durcissement--à-faire-au-banc)).

### Essai de coupure secteur : à faire au banc

**Non fait à ce jour.** C'est le critère d'acceptation qui reste au ticket de
l'horloge et des journaux (ANH-163), et il complète l'étape 6 ci-dessus. Il
demande un Pi installé (étapes 1 à 5), son réseau, et de quoi couper ce réseau
(débrancher le câble, ou éteindre le point d'accès). Ni variateur ni BITalino :
console en simulation, **capsule vide**. Compter une demi-heure.

À noter pour chaque constat : l'heure d'une montre, ce qui est lu, et si c'est
ce qui est attendu. Le tout se consigne dans un commentaire du ticket ANH-163,
avec la référence du Pi et celle du système (`cat /etc/rpi-issue`).

| | Faire | Lire | Attendu |
|---|---|---|---|
| 1 | Pi en marche, réseau branché, attendre 2 minutes | `timedatectl` ; la ligne `horloge` de la page | `System clock synchronized: yes` ; `horloge synchronisee`, deux erreurs en millisecondes |
| 2 | démarrer une séance manuelle de banc (cible 0), puis **STOP** | page Configuration, « Exporter l'enregistrement », puis `manifest.json` de l'archive | `"wall_trusted":true` ; `started_at` à l'heure de la montre |
| 3 | noter l'heure de la montre. **Couper le réseau**, puis **couper l'alimentation** du Pi, sans rien taper. Attendre **10 minutes** au moins | | |
| 4 | remettre l'alimentation, **sans le réseau**. Attendre que la page réponde | la ligne `horloge` ; `date` dans un terminal du Pi ; `timedatectl` | `horloge NON FIABLE`, en orange ; `date` en retard d'environ la durée de la coupure ; `System clock synchronized: no`. La console a démarré seule, au repos |
| 5 | démarrer une séance manuelle de banc (cible 0), puis **STOP** | la liste des événements ; le manifeste exporté | un événement `horloge murale non fiable (...)` ; `"wall_trusted":false` ; `started_at` en retard comme `date` |
| 6 | rebrancher le réseau, noter l'heure de la montre | la ligne `horloge`, toutes les 10 secondes ; `chronyc tracking` | `horloge synchronisee` ; noter en combien de temps. `date` est de nouveau à l'heure de la montre |
| 7 | démarrer une troisième séance de banc, puis **STOP** | le manifeste exporté | `"wall_trusted":true` ; `started_at` à l'heure de la montre |
| 8 | relire le journal | `journalctl -u anheart -o cat \| grep '"event": "wall clock'` ; `journalctl --list-boots` | les lignes d'avant la coupure sont là (journal gardé) ; une ligne `unsynchronised` après le rallumage, une `synchronised` après le retour du réseau. Noter la dernière ligne écrite avant la coupure et son heure : c'est ce que la coupure a laissé du journal |
| 9 | relire les trois enregistrements | `sudo ls /var/lib/anheart/records` | trois dossiers ; celui de l'étape 5 porte dans son nom l'heure fausse |

Ce que cet essai ne dit pas : le comportement d'un module d'horloge temps réel
(aucun n'est monté), ni celui d'une séance avec une personne à bord, mode fermé
(`OCCUPANCY_OCCUPIED_ENABLED=false`). Le refus d'un tel départ sur une horloge
que personne ne tient est prouvé par les tests.

### Essai du durcissement : à faire au banc

**Non fait à ce jour.** C'est le critère d'acceptation « essai au banc
consigné » du ticket ANH-151, pour son premier lot. La machine de
remplacement prouve que la console démarre, enregistre et se relance sans
privilèges ; elle n'a ni câble ni capteur. **Ce que seul un Pi montre : que le
vrai câble du variateur et le vrai BITalino s'ouvrent sous le compte
`anheart`.** Compter une heure. Les constats 1 à 8 se font sur un Pi installé
en simulation ([étapes 1 à 5](#6-installer-un-vrai-raspberry-pi)), sans rien de
branché. Les constats 9 à 11 se font **variateur hors tension** : le câble
est alimenté par le Pi, la console l'ouvre, et rien ne lui répond. Ils ne
relient donc pas un variateur en marche à la console du service, ce qui
reste soumis à la condition écrite en
[section 8](#8-limites-et-reste-à-faire) (le verrou du câble).

| | Faire | Lire | Attendu |
|---|---|---|---|
| 1 | | `id anheart`, puis `sudo docker exec anheart id` | le même numéro de compte des deux côtés, différent de 0 ; le groupe `dialout` (20) des deux côtés |
| 2 | | `sudo docker inspect --format '{{.HostConfig.Privileged}} {{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}}' anheart` | `false true [ALL]` |
| 3 | | `ps -o user=,args= -C socat` ; `sudo ls -ld /run/anheart/relay` | `anheart` ; `drwx------ ... anheart anheart` |
| 4 | | `sudo stat -c '%U:%G %a %n' /etc/anheart/anheart.env /var/lib/anheart /var/lib/anheart/records` | `root:root 600`, puis `anheart:anheart 700` deux fois |
| 5 | une séance manuelle de banc, cible 0, puis **STOP** | `sudo find /var/lib/anheart \( ! -user anheart -o -perm /077 \)` | rien |
| 6 | | `systemctl is-enabled apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service` ; `apt-config dump \| grep Periodic` ; `systemctl list-timers --all` | `masked` trois fois ; toutes les valeurs à `"0"` ; **noter les autres minuteries de la liste**, et en particulier ce que fait `rpi-eeprom-update` sur cette image |
| 7 | | `sudo bash scripts/harden.sh --dry-run` | `harden.sh, dry run: nothing to change` |
| 8 | couper puis remettre l'alimentation | les constats 1 à 4 | les mêmes : la console est revenue seule, sous le même compte |
| 9 | brancher le câble du variateur, **variateur hors tension** | `lsusb` ; `ls -l /dev/ttyUSB* /dev/bus/usb/*/*` | une ligne `16de:0003` ; le nœud du câble en `crw-rw---- root dialout` (un `ttyUSB` si le noyau a un pilote pour lui, sinon son nœud sous `/dev/bus/usb`). **Noter lequel des deux** : il décide de `MOTOR_PORT` |
| 10 | **variateur toujours hors tension**, câble branché, aucun outil de banc : `MOTOR_BACKEND=serial` et `MOTOR_PORT` dans la configuration, puis `sudo systemctl restart anheart` | la page ; `journalctl -u anheart` | **aucune ligne `Permission denied` ni `Access denied`** : le câble s'ouvre sous le compte. Une liaison sans réponse est normale, personne ne répondant au bout du câble. Noter la ligne exacte du journal et ce que la page dit du variateur |
| 11 | débrancher le câble, attendre une minute, le rebrancher, **sans relancer le service** | `journalctl -u anheart -f` | après le rebranchement, la console rouvre le câble seule : les mêmes lignes qu'au constat 10 reviennent, sans refus d'accès. C'est la raison pour laquelle `/dev` est monté tout entier. Noter si un redémarrage du service a été nécessaire |
| 12 | BITalino appairé (`bash scripts/pair_device.sh <adresse>`), `ECG_SOURCE=serial`, `BITALINO_ADDRESS=/dev/rfcomm0`, `BITALINO_MAC` ; `sudo systemctl restart anheart` | `journalctl -u anheart \| grep bind-bitalino` ; `ls -l /dev/rfcomm0` ; la pastille ECG | `/dev/rfcomm0 is bound to the BITalino ...` ; `crw-rw---- root dialout` ; la liaison ECG est établie |
| 13 | redémarrer le Pi (`sudo reboot`), capteur allumé | les mêmes | le nœud est lié de nouveau sans personne, la liaison ECG revient |

Si le constat 9 ou 12 donne un autre groupe ou d'autres droits, ou si le 10 ou
le 12 échoue sur un refus d'accès : **ne rien élargir à la main**, noter la
sortie exacte de `ls -l` et la ligne du journal. C'est le résultat de l'essai.

La liaison avec le variateur **sous tension** n'est pas dans cet essai : elle
relève de la [procédure de banc](securite.md#96-essai-au-banc--procédure-non-faite)
et de ses conditions.

À consigner dans un commentaire du ticket ANH-151 : la référence du Pi et du
système (`cat /etc/rpi-issue`), la version de la console, ce tableau avec ce
qui a été lu, et la sortie de `sudo bash scripts/harden.sh --dry-run`. Puis
corriger ici et dans [Sous quel compte](#sous-quel-compte) ce qui est écrit
« ne se voit qu'avec le câble » ou « qu'avec le capteur ».

---

## 7. Mettre à jour, changer une version figée

**Mettre à jour la console d'un Pi.** Terminer la séance en cours. Copier les
nouvelles sources (`scripts/pi/deploy.sh`), puis relancer
`sudo bash scripts/install.sh` sur le Pi : il construit l'image de la nouvelle
version, refuse si la console n'est pas au repos, redémarre le service, refait
le test de démarrage. L'image de l'ancienne version reste sur la machine ; pour
y revenir, relancer le script depuis les anciennes sources (l'image est en
cache). Les images qui ne servent plus se retirent avec `sudo docker image rm`.

**Modifier `/etc/anheart/anheart.env`.** Le service lit le fichier à son
démarrage : `sudo systemctl restart anheart`, console au repos.

**Changer une version figée.**

| Quoi | Où | Puis |
|---|---|---|
| Un paquet Python | `requirements-base.txt` ou `requirements-prod.txt` | régénérer `requirements-lock.txt` avec la commande écrite en tête du fichier (en avançant sa date `--exclude-newer`), mettre à jour l'environnement de développement, corriger le tableau de la [section 2](#limage-de-la-console) |
| Les outils de construction | `requirements-build.txt` | régénérer `requirements-build-lock.txt` de la même façon |
| L'image de base (Python) | ligne `FROM` du `Dockerfile` : version **et** empreinte | corriger la [section 2](#limage-de-la-console) |
| Le système du Pi | `OS_REFERENCE` (et `OS_CODENAME` pour une autre série) dans `scripts/install.sh` | corriger la [section 2](#le-système-du-pi) ; pour une autre série Debian, changer aussi `MACHINE_BASE` dans `scripts/pi/test_install.sh` |

`tests/test_pi_install.py` échoue tant que les fichiers et cette page ne
concordent pas, et le workflow `pi-install` reconstruit l'image avec les
nouvelles versions.

---

## 8. Limites et reste à faire

| Quoi | Qui ou quel ticket |
|---|---|
| Installer un vrai Pi et constater le démarrage à l'allumage ([section 6](#6-installer-un-vrai-raspberry-pi)) | une personne avec le matériel |
| Endurance sur le Pi : deux heures en simulation, puis une journée au banc, capsule vide ([section 9](#9-endurance--deux-heures-en-simulation-sur-le-pi-puis-une-journée-au-banc)) | une personne avec le matériel, ANH-164 |
| Durée de construction de l'image sur un Pi 4 et sur un Pi 5 | idem, étape 4 |
| Publier et signer l'image, la faire tirer par le Pi | chef de projet (registre, clé), ANH-126, ANH-168 |
| Mettre en service et éprouver sur le matériel le point d'accès/pare-feu, TLS, la racine en lecture seule et le volume chiffré ; les outils logiciels sont livrés ([section 10](#10-durcissement--ce-que-fait-scriptshardensh-et-la-check-list-de-limage)) | ANH-151 pour la validation terrain ; logiciels ANH-223 et ANH-224 livrés |
| Le câble du variateur et le BITalino sous le compte `anheart` n'ont jamais été essayés : la règle `udev` du câble Schneider, le nœud série lié par l'unité, les droits des nœuds ([Sous quel compte](#sous-quel-compte)) | une personne avec le matériel : [essai du durcissement](#essai-du-durcissement--à-faire-au-banc) |
| Le client Docker de l'unité est root, parce que le démon Docker l'est ([ce qui reste sous root](#ce-qui-reste-sous-root)) | aucun ticket : il faudrait un Docker sans root, ou une console lancée sans Docker |
| Le conteneur voit le nom de tous les périphériques de la machine (`/dev` est monté), même s'il n'en ouvre que trois classes | limite assumée : les nœuds du câble et du capteur vont et viennent |
| `rpi-eeprom-update` n'est pas touché : il applique au démarrage une mise à jour du micrologiciel du Pi **déjà installée par un paquet**, donc seulement après un `apt` lancé par une personne | à constater sur un Pi ([essai du durcissement](#essai-du-durcissement--à-faire-au-banc)) ; à décider par le chef de projet |
| Servir la page en TLS sur un Pi installé et éprouver l'accès depuis une tablette réelle : le branchement des certificats et du healthcheck HTTPS au service est livré ([Le certificat de la console](#le-certificat-de-la-console)) | logiciels ANH-151/223 livrés ; essais par une personne avec le matériel |
| Une console qui ne répond réellement à rien, pas même à son arrêt, n'a été exercée nulle part sous systemd : tout gel appliqué du dehors est levé par l'arrêt. Ce que l'unité fait alors (15 s, puis `docker stop`, qui tue au bout de 60 s) est lu, pas observé | à observer le jour où le cas se présente ; aucun essai sûr ne le produit |
| Le chien de garde du service, la relance et le mode hors service n'ont jamais tourné sur un vrai Pi ; le chien de garde matériel n'a jamais été armé ni déclenché | une personne avec le matériel : [section 6](#6-installer-un-vrai-raspberry-pi), étape 7, puis la procédure de banc de [securite.md](securite.md#96-essai-au-banc--procédure-non-faite) |
| Quand systemd tourne mais n'arrive plus à relancer la console (limite de dix démarrages atteinte, Docker bloqué), l'unité reste en échec et **le Pi ne redémarre pas de lui-même** : seul un système entier figé est redémarré par le chien de garde matériel. Le faire redémarrer aussi dans ce cas est une ligne de l'unité (`StartLimitAction=reboot`), au prix d'un Pi qui redémarre en boucle, donc difficile à joindre, quand la console ne peut pas démarrer du tout (image absente) | décision du chef de projet ; d'ici là, `systemctl reset-failed anheart` ou un redémarrage du Pi à la main |
| Constater sur Pi la remontée de l'état « hors service » : il est désormais transmis dans la santé (`restart_loop`) et affiché sur la fiche machine, en plus de la console et de son journal | logiciel ANH-148 livré ; essai matériel restant |
| Essai de coupure secteur au banc ([section 6](#essai-de-coupure-secteur--à-faire-au-banc)) : il n'est pas fait, et lui seul montre le remplacement de `systemd-timesyncd`, la correction de l'heure au retour du réseau et ce que la coupure laisse du journal | une personne avec le matériel ; ANH-163 |
| Horloge temps réel : aucun module n'est monté ni essayé. Le script en signale une et `chrony` la tiendrait à l'heure, mais la console ne lui fait pas confiance sans réseau ([section 4](#lheure)). Choisir un module, l'activer dans `/boot/firmware/config.txt`, puis écrire la règle de confiance | chef de projet (matériel), puis un ticket |
| Les serveurs de temps sont ceux de Debian, sur Internet. Un site qui ferme la sortie NTP (UDP 123) laisse l'horloge « NON FIABLE » : y déclarer le serveur de temps du site (`/etc/chrony/sources.d/`) | à décider par site |
| Le contrôle pré-vol qui regroupe l'horloge avec les autres contrôles ; le décalage d'horloge est déjà transmis dans la santé du heartbeat lorsqu'il est disponible | ANH-142 ; transmission logicielle ANH-148 livrée |
| Mises à jour à distance | ANH-116, ANH-168, ANH-169 |
| Le service partage `/tmp/anheart-drive.lock` avec l'hôte par bind du même inode. Les outils Anheart membres de dialout sont exclus pendant toute la possession du câble, reconnexions comprises. Un conteneur lancé à la main doit recevoir le même montage | logiciel livré par ANH-224 ; contention et vrai pilote à éprouver au banc ([procédure](pi-stockage.md)) |
| Un `systemctl start anheart` à la main, ou la relance après la mort du client Docker, arrête une console trouvée en marche sans lui demander si elle est au repos ([section 4](#une-seule-console-à-la-fois)) | limite assumée ; `scripts/install.sh`, lui, pose la question |
| Une coupure franche de l'alimentation n'est pas simulée par la CI (l'étape 8 est un arrêt ordonné) | une personne avec le matériel, [section 6](#6-installer-un-vrai-raspberry-pi), étape 6 |
| Raspberry Pi OS 12 est la série « Legacy » ; la série courante est Debian 13. En changer se fait en [section 7](#7-mettre-à-jour-changer-une-version-figée) | décision du chef de projet |
| Figer les outils de développement (`requirements-dev.txt`) | ticket d'hygiène de la CI (ANH-183) |

---

## 9. Endurance : deux heures en simulation sur le Pi, puis une journée au banc

**Non fait à ce jour : aucun Raspberry Pi n'a servi.** Cette section prépare les
deux essais du ticket ANH-164 qui demandent le matériel. Leur partie sans
matériel, 24 heures simulées de la console entière, tourne chaque nuit en CI ;
le scénario, ses mesures et ses seuils sont décrits dans
[framework-de-test.md, section 17](framework-de-test.md#17-endurance-de-la-console).

Les deux essais relèvent les mêmes quatre choses : la mémoire résidente de la
console, la durée ou la régularité de son tic, ses descripteurs de fichiers
ouverts, la taille de son dossier de données.

### 9.1 Deux heures en temps réel, en simulation

Sans variateur ni BITalino : variateur et ECG simulés. La console tourne pour
de bon, sur l'horloge réelle, avec son serveur web, son fil d'écriture et son
traitement du signal, dans **l'image installée** (même Python, mêmes paquets,
même code que le service). C'est cet essai qui mesure la **durée du tic sur un
Pi**.

Prérequis : le Pi est installé ([section 6](#6-installer-un-vrai-raspberry-pi),
étapes 1 à 5). L'image ne contient que la console : le scénario et la doublure
du tableau de bord sont copiés à part.

1. **Copier le scénario.** Depuis le poste de développement, à la racine du
   dépôt, sur le commit installé sur le Pi :

   ```sh
   git archive --format=tar HEAD \
       simulation/__init__.py simulation/endurance.py simulation/endurance_measures.py \
       raspberry-pi/tests/__init__.py raspberry-pi/tests/fake_dashboard.py \
     | ssh <utilisateur>@<pi> 'mkdir -p anheart-endurance && tar -xf - -C anheart-endurance'
   ```

2. **Arrêter le service**, console au repos : l'essai et le service se
   disputeraient le processeur.

   ```sh
   sudo systemctl stop anheart
   systemctl is-active anheart     # doit répondre : inactive
   ```

3. **Lancer l'essai** dans l'image installée. Aucun port n'est publié, aucun
   périphérique n'est monté, aucun réseau n'est utilisé : le tableau de bord est
   une doublure en mémoire.

   ```sh
   IMAGE=$(sed -n 's/^Environment=ANHEART_IMAGE=//p' /etc/systemd/system/anheart.service.d/10-version.conf)
   echo "$IMAGE"                   # anheart-console:<version>
   sudo docker run --rm --name anheart-endurance --pull never --no-healthcheck --network none \
       --user "$(id -u):$(id -g)" \
       --volume "$HOME/anheart-endurance:/endurance" \
       --env PYTHONPATH=/endurance:/endurance/raspberry-pi \
       "$IMAGE" python -m simulation.endurance real-time --minutes 120 --out /endurance/out
   echo "code de sortie : $?"
   ```

   Deux heures et quelques minutes. Code de sortie 0 : tous les seuils sont
   tenus (`PASS`). La santé et le dépôt complet sont actifs avec un stockage
   simulé vérifiant les octets. La rétention locale de cet essai est de zéro
   jour ; le journal examine les purges toutes les six heures, donc les deux
   heures prouvent les confirmations, et le scénario de 24 h prouve la purge.

4. **Relever.** Le résumé est affiché à la fin et écrit dans
   `~/anheart-endurance/out/endurance.md` (le détail dans `endurance.json`, ce
   que la console a dit dans `console.log`). L'image ne tourne plus sous root :
   `--user` lui donne le compte de la personne, qui possède le dossier où le
   résultat est écrit.

   ```sh
   cat ~/anheart-endurance/out/endurance.md
   cat /proc/device-tree/model; echo; uname -r
   ```

5. **Relancer le service** : `sudo systemctl start anheart`, puis les constats
   de la [section 6](#6-installer-un-vrai-raspberry-pi), étape 5.

À noter dans le ticket : le modèle du Pi, la version de l'image, le tableau des
seuils, la ligne « Écart entre deux tics », le code de sortie.

Pour mesurer aussi ce que **coûte** un tic sur le Pi, sans attendre deux
heures : la même commande avec `simulated --hours 1` à la place de
`real-time --minutes 120` joue une heure simulée, chaque tic chronométré.

Cette commande n'a été lancée ni sur un Pi ni dans l'image : sur le poste de
développement, la même commande Python a tourné hors Docker, avec ces cinq
fichiers seulement à côté de la console
([framework-de-test.md, section 17.6](framework-de-test.md#176-mesures-relevées)).

### 9.2 Une journée au banc, capsule vide

Avec le vrai variateur, le service `anheart` en marche, une personne à la
console qui lance et arrête ses séances manuelles comme d'habitude. **Rien
n'est lancé à la place du service et rien ne pilote la machine** : le suivi se
fait de l'extérieur, en lecture seule, depuis une seconde session SSH.

1. **Avant la première séance**, noter la place et les inodes libres, et
   démarrer le relevé (une ligne toutes les cinq minutes ; parcourir le dossier
   des enregistrements plus souvent chargerait la carte SD) :

   ```sh
   df -h /var/lib/anheart/records; df -i /var/lib/anheart/records
   PID=$(sudo docker inspect --format '{{.State.Pid}}' anheart)
   echo "epoch_s,rss_kb,descriptors,data_kb,data_inodes" > ~/endurance-banc.csv
   while sudo test -d "/proc/$PID"; do
       printf '%s,%s,%s,%s,%s\n' "$(date +%s)" \
           "$(sudo awk '/^VmRSS:/ {print $2}' "/proc/$PID/status")" \
           "$(sudo ls "/proc/$PID/fd" | wc -l)" \
           "$(sudo du -sk /var/lib/anheart/records | cut -f1)" \
           "$(sudo find /var/lib/anheart/records | wc -l)" >> ~/endurance-banc.csv
       sleep 300
   done
   ```

   `$PID` est le processus de la console (le script d'entrée de l'image lui
   passe la main). La boucle s'arrête d'elle-même si ce processus disparaît :
   un redémarrage de la console pendant la journée est à noter, avec l'heure.

2. **Pendant la journée** : les séances du protocole de banc, sans rien changer
   à la façon de travailler. Noter l'heure de début, le nombre de séances, et
   tout message inattendu de la console.

3. **En fin de journée**, arrêter la boucle (Ctrl-C), puis relever la
   régularité du tic dans les enregistrements du jour. La console ne publie pas
   la durée de son tic ; ses enregistrements portent l'instant de chaque tic et
   le verdict en vigueur. Remplacer `2026-10-15` par la date du jour (UTC) :

   ```sh
   JOUR=2026-10-15
   # Écart entre deux tics, en secondes : médiane, 99e centile, maximum (0.2 attendu).
   sudo sh -c "for f in /var/lib/anheart/records/${JOUR}T*/ticks.csv; do awk -F, 'NR>2 {print \$1-p} {p=\$1}' \"\$f\"; done" \
     | sort -n | awk '{v[NR]=$1} END {print "tics", NR, "p50", v[int(NR*0.5)], "p99", v[int(NR*0.99)], "max", v[NR]}'
   # Tics passés sous un verdict loop_stall, par enregistrement : 0 partout.
   sudo sh -c "grep -c loop_stall /var/lib/anheart/records/${JOUR}T*/ticks.csv"
   df -h /var/lib/anheart/records; df -i /var/lib/anheart/records
   ```

4. **Juger le relevé**, sur le poste de développement, avec les seuils de
   l'essai simulé (mémoire stable à 10 % près après la première heure, aucun
   descripteur qui fuit) :

   ```sh
   scp <utilisateur>@<pi>:endurance-banc.csv .
   PYTHONPATH=.:raspberry-pi raspberry-pi/.venv/bin/python -m simulation.endurance judge endurance-banc.csv --out simulation/out/banc
   ```

Ce que cet essai ne mesure pas : la **durée** du tic avec le vrai variateur. De
l'extérieur on ne lit que l'écart entre deux tics, qui reste à 200 ms tant
qu'un tic tient dans sa période ; le seuil « 99e centile sous 150 ms » se lit
donc sur l'essai 9.1, en simulation. Mesurer la durée au banc demanderait que
la console la publie elle-même, ce qu'aucun ticket ne prévoit à ce jour.

À surveiller : **aucun enregistrement non confirmé n'est purgé**. Le dépôt
complet d'ANH-130 écrit désormais la confirmation vérifiée ; la libération
dépend aussi de la durée de rétention configurée. Une heure de séance
ajoute environ 18 000 fichiers ; la console refuse d'armer une séance sous
36 016 inodes libres ou 500 Mo libres. Les deux `df` de la journée donnent
l'autonomie réelle de la carte.

### 9.3 Où consigner les résultats

Dans le ticket ANH-164 (un commentaire par essai, avec `endurance.md` ou le
résumé de `judge` en pièce jointe), et dans le tableau ci-dessous.

| Essai | Date | Pi, version de l'image | Durée, séances | Mémoire résidente (min, max) | Descripteurs (max) | Tic | Données (Mo, fichiers, inodes libres avant et après) | Verdict |
|---|---|---|---|---|---|---|---|---|
| 9.1, deux heures en simulation | à faire | | | | | durée p50, p99 ; écart p50, p99, max | | |
| 9.2, journée au banc | à faire | | | | | écart p50, p99, max ; tics sous `loop_stall` | | |

---

## 10. Durcissement : ce que fait `scripts/harden.sh`, et la check-list de l'image

Le Pi doit devenir un appareil fermé (ticket ANH-151) : la console joignable
seulement depuis le réseau que le Pi crée, rien d'autre qui écoute, pas de
service sous root là où c'est évitable, un système qu'une coupure ne peut pas
abîmer, des secrets et des données protégés. Le ticket est coupé en quatre
lots. Les lots service, TLS, réseau et stockage sont implémentés et testés
sans Pi. Les essais matériels restent distingués dans la check-list ci-dessous.

`scripts/install.sh` installe la console ; `scripts/harden.sh` ferme la
machine autour d'elle. Le ticket le nommait `scripts/harden.sh` : il est avec
les autres scripts du Pi, dans `raspberry-pi/scripts/`.

```sh
sudo bash scripts/harden.sh --dry-run    # dit ce qui changerait, ne change rien
sudo bash scripts/harden.sh              # les trois étapes par défaut
sudo bash scripts/harden.sh files        # une étape, ou plusieurs, par leur nom
sudo bash scripts/harden.sh --undo       # reprend ce que les étapes ont réglé
bash scripts/harden.sh --list            # les étapes
```

Ce qu'il garantit pour les trois étapes par défaut (`account`, `files`, `updates`) :

* **elle regarde d'abord**, et n'agit que sur ce qui diffère : une seconde
  exécution ne change rien ;
* **l'essai à blanc** fait les mêmes constats et remplace chaque changement
  par une ligne `would ...` : il écrit exactement ce qu'une exécution ferait,
  et le compte (`N change(s) would be made, none was`). C'est aussi la façon
  de contrôler une machine : `sudo bash scripts/harden.sh --dry-run` doit
  répondre `nothing to change` ;
* **elle n'arrête, ne relance et ne reconfigure jamais la console**, et une
  exécution interrompue au milieu laisse une console qui démarre ;
* rien n'est téléchargé, rien n'est installé ;
* ce qui n'a pas pu être changé est dit, compté, et le script sort en erreur :
  il ne dit jamais qu'il a protégé ce qu'il n'a pas protégé.

### Les étapes du premier lot

`scripts/install.sh` les lance toutes les trois à chaque installation.

| Étape | Ce qui doit être vrai après elle | Reprise par `--undo` |
|---|---|---|
| `account` | le compte `anheart` a `/usr/sbin/nologin` pour shell, un mot de passe verrouillé, et appartient au groupe `dialout`. Un compte `anheart` qui porterait un numéro de root est refusé, et rien n'est construit ni démarré : la console tournerait sous root | rien : le compte reste, il possède les données |
| `files` | `/etc/anheart/anheart.env` appartient à root, mode 600 (`/etc/anheart` : root, 755) ; **tout** ce qui est sous `/var/lib/anheart` appartient au compte `anheart` ; `/var/lib/anheart`, `data` et `records` sont en mode 700 ; rien d'autre sous eux n'est lisible, inscriptible ni traversable par un groupe ou par les autres (enregistrements, journal hors séance, état des redémarrages, profils). Un lien symbolique n'est jamais suivi. Aucun contenu n'est lu ni écrit | rien : la configuration et les données restent aussi privées |
| `updates` | `/etc/apt/apt.conf.d/99-anheart-no-automatic-updates` éteint les tâches périodiques d'`apt`, et il est lu après les fichiers de la distribution ; `apt-daily.timer`, `apt-daily-upgrade.timer` et `unattended-upgrades.service` sont masqués et arrêtés. Masqués, pas seulement désactivés : un paquet installé plus tard ne peut pas rallumer sa propre tâche. Un paquet ne change plus que lorsqu'une personne lance `apt`, console au repos | le fichier est retiré, les trois services démasqués |

L'étape `files` et les enregistrements déjà sur le disque : ceux qu'une
console plus ancienne avait écrits en 755 / 644, ou sous root, sont refermés
et rendus au compte. Seuls des droits sont retirés aux autres ; ce que le
propriétaire pouvait faire ne change pas.

### La check-list de l'image

Une ligne par exigence du ticket. La colonne « Vérifié » dit par quoi, sans
Raspberry Pi ; un essai matériel marqué **non fait** reste à réaliser, même
quand son logiciel est écrit.

| Exigence | État | Par quoi | Vérifié sans Pi |
|---|---|---|---|
| Console sous un compte dédié, sans shell, sans privilèges ; périphériques explicites, plus de conteneur privilégié | **fait** (lot 1) | l'unité, l'image, `install.sh`, étape `account` ([Sous quel compte](#sous-quel-compte)) | test de bout en bout, étape 1 |
| Options de l'unité (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`) | **fait** (lot 1), avec sa limite : le client Docker reste root ([ce qui reste sous root](#ce-qui-reste-sous-root)) | l'unité | test de bout en bout, étape 1 |
| Configuration en 600, enregistrements en 700 | **fait** (lot 1) ; la configuration est à root et non au compte ([les secrets](#3-ce-que-fait-scriptsinstallsh)) | étape `files` | test de bout en bout, étapes 1, 2 et 11 |
| Aucune mise à jour automatique des paquets | **fait** (lot 1) | étape `updates` | test de bout en bout, étapes 1, 11 et 12 |
| `scripts/harden.sh` idempotent, essai à blanc, relu et exécuté en CI | **fait** (lot 1) | ce script, `uninstall.sh`, le job `pi-install` | ShellCheck, puis test de bout en bout, étapes 11 et 12 |
| Point d'accès Wi-Fi dédié à la console, liaison Internet séparée, aucun routage entre les deux | **logiciel fait**, radio à essayer | `harden.sh network`, [procédure](pi-reseau.md) | parseur NetworkManager et paquets dans deux espaces réseau Linux |
| Pare-feu ; SSH par clés sur le réseau de la console seulement ; compte de service privé de sudo | **logiciel fait**, scan au banc à faire | même commande | nftables, sshd, visudo et tests TCP/routage Linux |
| TLS sur la console, certificat propre au Pi ; HTTP clair refusé hors boucle locale | **logiciel fait**, tablette à essayer | certificat monté en lecture seule, contrôle de santé HTTPS | tests TLS sur vraies sockets et contrôle de santé avec autorité distincte refusée |
| Racine avec overlay RAM, données et journal persistants et bornés | **logiciel fait**, boot Pi et coupure à essayer | `harden.sh storage`, [procédure](pi-stockage.md) | vraie couche overlay et données persistantes sous Linux |
| Chiffrement du volume des données | **logiciel fait** pour un volume LUKS2 existant ; clé en fichier et anciennes copies : limites explicites | [ANH-224](pi-stockage.md), décision matérielle et avis ANH-172 encore nécessaires | bon/mauvais déverrouillage, copie, remount sur volume jetable |
| Essai final sur un Pi réel | **non fait** | [essai du durcissement](#essai-du-durcissement--à-faire-au-banc) | |

### Ajouter une étape

Pour les lots suivants. Une étape est un nom dans `STEPS`, à la place où elle
doit s'exécuter, et trois fonctions (un tiret dans le nom devient un
soulignement) : `describe_<nom>` (une ligne pour `--list`), `step_<nom>` et
`undo_<nom>`. Tout changement passe par `change "ce qui est fait" commande...`,
qui le compte, l'exécute, et à blanc se contente de l'écrire : c'est ce qui
rend l'essai à blanc exact sans une ligne de plus. `scripts/uninstall.sh`
lance `harden.sh --undo` : écrire `undo_<nom>` suffit pour que la
désinstallation reprenne l'étape.

Trois choses à savoir avant d'en écrire une :

* **`install.sh` ne lance que les étapes qu'il nomme** (`account files
  updates`). Une étape qui coupe un accès (réseau, pare-feu, lecture seule) ne
  doit pas partir à chaque mise à jour de la console : elle se lance à la
  main, à la mise en service, et `install.sh` n'a pas à la connaître ;
* **le test de bout en bout lance `harden.sh` sans nom d'étape** (étapes 11
  et 12), donc les trois étapes par défaut. `network` et `storage` sont
  explicites et possèdent leurs propres tests Linux : ils ne doivent pas
  couper le réseau ni préparer les montages lors d'une simple réinstallation ;
* **la console tourne sous un compte sans droits** : ce qu'une étape lui
  donne à lire (un certificat, une clé) doit être lisible par le compte
  `anheart` **et** monté dans son conteneur par l'unité, dont l'image est en
  lecture seule. Docker, lui, écrit sous `/var/lib/docker` à chaque démarrage
  de la console, et l'installation écrit sous `/etc`, `/usr/local/lib` et
  `/var/lib/docker` : un système en lecture seule doit en tenir compte.

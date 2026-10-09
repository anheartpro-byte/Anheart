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
> - **Pas vérifié** : aucune installation sur un vrai Raspberry Pi, ni avec le
>   vrai variateur, ni avec le vrai BITalino. La liste de ce qu'une personne
>   doit faire et constater est en [section 6](#6-installer-un-vrai-raspberry-pi).
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
| libusb | paquet `libusb-1.0-0` : 2:1.0.26-1 dans l'image figée | déjà dans l'image, vérifié par le script |
| systemd | 252.39-1~deb12u2 dans l'image figée | déjà dans l'image |

Le fichier se télécharge sur `downloads.raspberrypi.com`, dossier
`raspios_oldstable_lite_arm64/images/raspios_oldstable_lite_arm64-2026-10-06/`.

Le script refuse un système qui n'est pas Debian 12 « bookworm ». Il compare la
référence de l'image (`/etc/rpi-issue`) à celle de cette page et **avertit** si
elle diffère, sans refuser : une image plus récente de la même série reste
Debian 12.

Les paquets du système (`docker.io`, `bluez`, `libusb-1.0-0`) viennent de
Debian 12 : ils sont figés par la série, qui ne reçoit que des corrections, pas
par un numéro exact. Les numéros ci-dessus sont ceux de la série au 7 octobre
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
| Paquets | installe `docker.io`, `bluez`, `libusb-1.0-0`, `ca-certificates`, `curl` ; active Docker et le Bluetooth | rien à installer : `apt` n'est pas appelé |
| Compte | crée le compte système `anheart` (sans connexion) | déjà là |
| Dossiers | `/var/lib/anheart/data` (profils locaux) et `/var/lib/anheart/records` (enregistrements de séance, mode 700), propriété de `anheart` | propriétaire et mode remis |
| Configuration | écrit `/etc/anheart/anheart.env` à partir de `.env.pi.example`, lisible par root seul (mode 600) | **le fichier existant n'est jamais réécrit ni affiché** ; propriétaire et mode remis |
| Image | construit `anheart-console:<version>` depuis le dossier | couches en cache : même image, rien ne change |
| Service | installe `anheart.service` et le fichier qui porte la version ; active le service au démarrage | fichiers identiques : inchangés |
| Démarrage | démarre le service ; s'il tournait, ne le redémarre **que si l'image ou l'unité a changé** ; dans les deux cas, une console en marche est interrogée une seconde fois juste avant, et doit se dire au repos | console inchangée : laissée en marche |
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

**Les secrets.** `/etc/anheart/anheart.env` porte la clé de la machine
(`MACHINE_API_KEY`) et, si la page est ouverte au réseau, son jeton
(`UI_TOKEN`). Le script ne les affiche jamais. Quand il interroge une console
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
Docker crée pour ce montage un dossier vide `/var/lib/anheart/data/records` sur
le Pi ; les enregistrements sont dans `/var/lib/anheart/records`.

**Désinstaller** : `sudo bash scripts/uninstall.sh` arrête la console et retire
le service. La configuration, les données, le compte et les images restent.

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

Ce contrôle **constate**, il ne répare pas : une console qui ne répond plus
mais dont le processus vit encore est marquée `unhealthy` par Docker et n'est
pas redémarrée. Le chien de garde est le ticket ANH-162.

### L'arrêt

`systemctl stop anheart` envoie SIGTERM à la console (`docker stop`), qui met
la consigne à zéro et rend la liaison au variateur, puis attend 60 s avant de
forcer. Arrêter le Pi proprement (`sudo poweroff`) passe par le même chemin.

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
machine dont le service tourne.** Le verrou qui interdit deux programmes sur le
câble du variateur est un fichier du conteneur de la console : un outil lancé
sur le Pi lui-même, ou dans un autre conteneur, **n'est pas refusé**
([README du Pi](../raspberry-pi/README.md#sur-un-raspberry-pi-installé--le-verrou-est-celui-du-conteneur)).

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

Règles de redémarrage de l'unité : relance 10 s après une sortie en erreur
(`Restart=on-failure`), jamais après une sortie avec le code 2 (configuration
refusée : la console liste les problèmes et sort ; les lire avec
`journalctl -u anheart`).

Non couvert : ces vérifications se font en simulation. Le comportement avec le
vrai variateur à l'allumage reste à constater sur la machine. L'étape 8 est un
arrêt **ordonné** de la machine de remplacement : une coupure franche de
l'alimentation, en plein milieu d'une écriture, n'est pas simulée. C'est
l'étape 6 de la [marche à suivre sur un vrai Pi](#6-installer-un-vrai-raspberry-pi).

### Sous quel compte

Le compte `anheart` possède les dossiers de données. **La console tourne encore
sous root**, dans un conteneur privilégié qui voit `/dev` et le D-Bus de
l'hôte : c'est ce qu'exigent aujourd'hui l'adaptateur USB-RS485 et la liaison
RFCOMM du BITalino. La faire tourner sous `anheart`, sans privilèges, est le
ticket ANH-151.

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
| 1. Première installation | service activé au démarrage et en marche ; `/healthz` répond ; console au repos, sans attestation ; `systemctl status anheart` montre la version ; l'image porte la même version et tourne sous Python 3.12 ; compte, dossiers et droits en place ; configuration en simulation, sans clé de machine ; le journal contient la ligne de démarrage de la console ; le contrôle de santé de l'image passe à `healthy` |
| 2. Deuxième installation | un jeton est écrit dans la configuration ; le script ne l'affiche pas, ne réécrit pas le fichier, ne redémarre pas la console ; le `curl` de Debian 12 envoie bien un en-tête lu sur son entrée standard |
| 3. Séance en cours | une séance manuelle simulée tourne ; son enregistrement s'écrit dans `/var/lib/anheart/records`, avec la version de l'image ; une installation qui remplacerait la console **refuse** et ne touche à rien |
| 4. Service arrêté en pleine séance | `systemctl stop anheart` rend la main en moins de 60 s ; systemd constate un arrêt propre (code 0) ; le journal porte le `shutdown complete` de la console ; l'enregistrement est clos, raison `shutdown` ; aucun conteneur ne reste ; redémarrée, la console est au repos, sans attestation |
| 5. Arrêt brutal | la console est tuée en pleine séance ; systemd la relance ; elle est au repos, sans attestation |
| 6. Au repos | la même installation remplace la console par la nouvelle version ; l'image précédente reste sur la machine |
| 7. Console que le service n'a pas lancée | un conteneur `anheart` lancé à la main tourne une séance : l'installation **refuse**, ne le touche pas, ne démarre pas le service ; une fois la séance terminée, l'installation passe, l'unité arrête et retire ce conteneur puis lance le sien |
| 8. Arrêt puis rallumage | la machine de remplacement est arrêtée (arrêt ordonné) puis rallumée ; la console revient seule, au repos |

Rien n'y touche du matériel ni un tableau de bord : variateur simulé, ECG
simulé, clé de machine vide.

**Quand il tourne.** Sur une PR qui modifie de quoi l'installation est faite
(`Dockerfile`, fichiers `requirements*.txt`, `scripts/install.sh`,
`scripts/anheart.service`, le script de test, `.env.pi.example`, `VERSION`,
`docker/`, le workflow), et à chaque push sur `develop`, où une modification
des sources de la console qui casserait son démarrage dans l'image est vue.

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

**Durée.** Sur la PR #37 (run 37607234436) : 2 min 40 s pour le job, dont
environ une minute pour construire l'image sur le runner arm64. Ce n'est pas la
durée sur un Raspberry Pi, qui reste à mesurer.

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
le workflow.

---

## 6. Installer un vrai Raspberry Pi

**Non fait à ce jour.** Voici ce qu'une personne doit faire et constater, avec
un Raspberry Pi 4 ou 5 et une carte SD. Les étapes 1 à 6 ne demandent ni
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
     `healthy`.
6. **Couper et remettre l'alimentation**, sans rien taper. Une fois le Pi
   redémarré, refaire les constats de l'étape 5 : la console doit être revenue
   seule, au repos. **C'est le critère d'acceptation du ticket** : consigner
   dans le ticket la référence du système, la durée de construction, la sortie
   de `systemctl status anheart` et le résultat de cet essai.
7. **Passer au matériel réel**, quand le banc est prêt (jalon M3) :
   - `sudo nano /etc/anheart/anheart.env` : `MOTOR_BACKEND=serial`,
     `ECG_SOURCE=serial`, puis `MOTOR_PORT`, `BITALINO_MAC`, les rayons mesurés,
     `CONVEX_URL` et `MACHINE_API_KEY`
     ([deploiement.md](deploiement.md#74-configurer)) ;
   - appairer le BITalino : `bash scripts/pair_device.sh <adresse MAC>` ;
   - `sudo bash scripts/pi/preflight.sh` ;
   - `sudo systemctl restart anheart`, puis les constats de l'étape 5.
8. **Avant tout outil de banc ou de diagnostic sur cette machine** (banc,
   mesure de latence, sonde ou balayage Modbus, SoMove) : arrêter le service et
   vérifier qu'il est arrêté. La console ne refuse pas un outil lancé hors de
   son conteneur
   ([section 4](#outils-de-banc-et-de-diagnostic--arrêter-le-service-dabord)).

Ce que ces étapes ne couvrent pas : l'écran en plein écran au démarrage
([deploiement.md](deploiement.md#75-vérifier-puis-démarrer)), le durcissement
du système, l'horloge et les journaux persistants
([section 8](#8-limites-et-reste-à-faire)).

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
| Durée de construction de l'image sur un Pi 4 et sur un Pi 5 | idem, étape 4 |
| Publier et signer l'image, la faire tirer par le Pi | chef de projet (registre, clé), ANH-126, ANH-168 |
| Faire tourner la console sous le compte `anheart`, sans privilèges ; pare-feu ; système en lecture seule | ANH-151 |
| Relancer une console qui ne répond plus (chien de garde) | ANH-162 |
| Horloge fiable, journaux persistants et bornés | ANH-163 |
| Mises à jour à distance | ANH-116, ANH-168, ANH-169 |
| Le verrou du câble du variateur (`/tmp/anheart-drive.lock`) est celui du conteneur : un outil de banc lancé sur le Pi **hors** du conteneur n'est pas refusé par la console. D'ici là, la règle est pour la personne : **aucun outil de banc ou de diagnostic tant que le service tourne** ([section 4](#outils-de-banc-et-de-diagnostic--arrêter-le-service-dabord)). Les sources de ces outils arrivent sur le Pi avec le dossier copié, mais rien n'y est installé pour les lancer (Python 3.11 du système, aucun environnement), et ils ne sont pas dans l'image | à concevoir (verrou partagé entre le Pi et le conteneur, ou outils lancés dans le conteneur), dans la suite de ANH-74 ; **condition avant de relier au vrai variateur une console lancée par ce service** |
| Un `systemctl start anheart` à la main, ou la relance après la mort du client Docker, arrête une console trouvée en marche sans lui demander si elle est au repos ([section 4](#une-seule-console-à-la-fois)) | limite assumée ; `scripts/install.sh`, lui, pose la question |
| Une coupure franche de l'alimentation n'est pas simulée par la CI (l'étape 8 est un arrêt ordonné) | une personne avec le matériel, [section 6](#6-installer-un-vrai-raspberry-pi), étape 6 |
| Raspberry Pi OS 12 est la série « Legacy » ; la série courante est Debian 13. En changer se fait en [section 7](#7-mettre-à-jour-changer-une-version-figée) | décision du chef de projet |
| Figer les outils de développement (`requirements-dev.txt`) | ticket d'hygiène de la CI (ANH-183) |

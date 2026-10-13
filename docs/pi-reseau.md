# Réseau privé de la console

ANH-223 termine le lot réseau logiciel d'ANH-151. La console utilise son
certificat propre au Pi et son jeton. Le réseau de la tablette et la liaison
Internet du Pi sont deux interfaces distinctes. Les essais ci-dessous utilisent
Linux dans un conteneur et des interfaces virtuelles ; ils ne valident ni la
radio Wi-Fi du Pi ni une tablette réelle.

## Préparer sur le Pi, console arrêtée

Installer d'abord la console par `scripts/install.sh`, puis les outils système :

```sh
sudo apt-get install network-manager dnsmasq-base nftables openssh-server sudo openssl
sudo systemctl stop anheart
sudoedit /etc/anheart/network.conf
```

Le fichier contient exactement les valeurs de la machine, par exemple :

```ini
MACHINE_ID=machine-001
AP_INTERFACE=wlan0
UPLINK_INTERFACE=eth0
```

L'identifiant comporte 1 à 20 lettres, chiffres, tirets ou soulignements.
Il ne contient aucun nom de personne. L'interface Internet peut être un second
adaptateur Wi-Fi. L'adresse du réseau console est `10.42.0.1/24` : vérifier
qu'elle ne recouvre pas le réseau Internet avant la mise en service.

Dans `/etc/anheart/anheart.env`, choisir `UI_PORT` entre 1024 et 65535, sauf
8123, et un `UI_TOKEN` aléatoire d'au moins 16 caractères. Préparer depuis le
terminal local du Pi :

```sh
sudo bash scripts/harden.sh network --dry-run
sudo bash scripts/harden.sh network
```

Le premier appel n'écrit rien. Le second prépare les fichiers et les unités
pour le prochain démarrage, mais n'active pas le réseau courant. Il refuse
si le service ou un conteneur de console tourne. Un nouvel appel conserve
le mot de passe, les certificats encore valides et les fichiers identiques.

Le mot de passe WPA2-AES est généré sur le Pi et conservé dans
`/etc/anheart/wifi-password`, root, mode 600. La sortie du script ne le
contient jamais. Le technicien le reporte sur la fiche privée de mise en
service. Le SSID est `anheart-<MACHINE_ID>`. Le profil NetworkManager utilise
`ipv4.method=manual`, jamais `shared` ; aucun NAT n'est créé.

Le certificat et sa clé sont dans `/var/lib/anheart/tls`, au compte `anheart`.
Le service monte ce dossier en lecture seule sous `/run/anheart-tls` dans
le conteneur. La configuration reçoit `UI_HOST=10.42.0.1`, les deux chemins
TLS et `UI_TLS_NAMES=anheart.internal`. L'installateur et le contrôle de santé
de l'image vérifient HTTPS avec `authority.crt` ; aucun `--insecure`.

## Activer et vérifier

Depuis le terminal local, avec une clé SSH déjà installée pour le compte
de maintenance :

```sh
sudo bash scripts/harden.sh network --activate
sudo systemctl start anheart
```

Installer `/var/lib/anheart/tls/authority.crt` sur la tablette en comparant
son empreinte au Pi, puis joindre le Wi-Fi et ouvrir
`https://anheart.internal:8090` (adapter le port). Le DNS et DHCP ne servent
que ce réseau ; la tablette n'obtient aucune route Internet du Pi.

Le pare-feu `inet anheart` est chargé avant la console. Il autorise sur l'AP
le port TLS, SSH et DHCP/DNS ; les nouvelles connexions Internet sont
refusées, sauf les réponses nécessaires au client DHCP et au voisinage IPv6
du Pi. Les réponses aux connexions que le Pi a ouvertes restent permises.
Les transferts entrant ou sortant de l'AP sont bloqués, même si Docker a
activé le routage. Les autres tables nftables ne sont pas vidées.

SSH utilise les clés, refuse root et `anheart`, et désactive les tunnels et
transferts SSH. Le compte de service reçoit un refus explicite de sudo.
La limite de 32 connexions TLS simultanées par adresse IPv4 refuse les
nouvelles connexions excédentaires ; elle ne ferme pas les connexions
existantes, notamment celle qui porte STOP.

Pour retirer le profil et DHCP/DNS, console arrêtée :
`sudo bash scripts/harden.sh network --undo`. Les secrets et certificats
sont conservés, ainsi que le pare-feu fermé. Reconfigurer volontairement
l'accès et la console avant de retirer cette protection.

## Preuves logicielles et reste au banc

`scripts/pi/test_network.sh`, lancé en CI dans un conteneur Linux isolé,
exécute le générateur deux fois, vérifie l'absence d'écriture à blanc et de
secret dans la sortie, puis soumet les fichiers aux vrais parseurs nftables,
dnsmasq, OpenSSH, sudo et NetworkManager. Deux espaces réseau jouent la
tablette et Internet : connexions TCP autorisées sur l'AP, refusées depuis
Internet, et routage d'abord fonctionnel puis bloqué par les règles.

`test_console_tls.py` vérifie le nouveau contrôle de santé contre un vrai
serveur HTTPS, avec la bonne autorité puis une autre. Les tests de
l'installateur couvrent aussi son accès à la console et le montage TLS.

Au banc restent : vérifier les noms d'interfaces et l'absence de recouvrement
des sous-réseaux, associer une tablette, installer l'autorité, ouvrir HTTPS et
WSS avec le jeton, scanner le Pi depuis les deux réseaux, éprouver SSH par
clé et le refus des mots de passe, puis redémarrer. Consigner les mesures
dans ANH-151 ; aucune n'est acquise par les essais logiciels.

Références de configuration : [NetworkManager keyfile](https://networkmanager.pages.freedesktop.org/NetworkManager/NetworkManager/nm-settings-keyfile.html),
[nftables](https://netfilter.org/projects/nftables/manpage.html),
[interaction avec Docker](https://docs.docker.com/engine/network/firewall-nftables).

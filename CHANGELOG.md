# Changelog

Chaque version d'un composant a sa section : `pi-X.Y.Z` (Raspberry Pi),
`cloud-X.Y.Z` (Convex), `web-X.Y.Z` (site). Les trois se versionnent
séparément. Une section liste les PR de ticket fusionnées dans `develop` qui
touchent le composant depuis sa version précédente.

Ce fichier est écrit par `scripts/release.sh prepare` : ne pas y ajouter de
section à la main. Le processus, les règles de version et le sens des niveaux
de validation sont dans [docs/release.md](docs/release.md).

Seule une version du Pi porte un niveau de validation (`bench`,
`auto_validated`, `occupied_validated`). Quand une revue relève ou retire le
niveau d'une version déjà publiée, une ligne datée est ajoutée sous sa ligne
« Niveau de validation » par une PR qui cite la revue, et la même valeur est
enregistrée dans Convex.

<!-- release.sh : nouvelles sections sous cette ligne -->

## pi-1.0.0 (2026-10-10)

Composant : Raspberry Pi. Changements depuis : la première version.
Niveau de validation : `bench`.

- ANH-71 : versionner et relire le socle logiciel
- ANH-127 : format partagé v2 et observation des échanges natifs
- ANH-74 : exclusive drive-cable ownership across console and diagnostics (#12)
- ANH-125 : align guides and configuration with current software
- ANH-176 : un bras arrêté en cours de séance ne repart jamais seul (#13)
- ANH-72 : gate Pi sous 25 minutes en CI, tests répartis sur plusieurs processus (#7)
- ANH-124 : corriger les défauts d'affichage de la console locale (points 1 à 7) (#10)
- ANH-178 : aucune cible manuelle n'attend sur un bras à l'arrêt (verdict, fréquence cardiaque, variateur) (#18)
- ANH-181 : session_overrun ne juge plus une séance finie (#19)
- ANH-175 : faire descendre la consigne sur un arrêt demandé, même sous FREEZE (#31)
- ANH-135 : retirer l'ancien enregistreur ECG du Pi et du déploiement (couche Pi) (#21)
- ANH-134 : outiller le processus de release (versions par composant, changelog, check-list, registre des versions) (#26)
- ANH-182 : afficher sur la console ce que la machine fait réellement (cible refusée, retenue cardiaque, E-STOP sans réponse, verrou caméra, reprise automatique) (#33)
- ANH-133 : versionner le contrat HTTP entre le Pi et Convex et refuser un pair incompatible (#28)
- ANH-128 : écrire l'enregistrement de séance sur le disque du Pi, hors de la boucle de contrôle (#34)
- ANH-197 : traiter les constats du premier passage de l'analyse statique (#36)
- ANH-131 : rejouer une séance enregistrée contre le vrai runtime, en ligne de commande et en CI (#32)
- ANH-189 : faire suivre à la consigne la descente du programme sous FREEZE (#38)
- ANH-161 : installer un Pi par script, image figée lancée par systemd au démarrage (#37)
- ANH-199 : afficher un rapport de qualité à chaque exécution de la CI (#41)
- ANH-201 : mettre au propre les tests et les scripts du Pi signalés par l'analyse statique (#43)
- ANH-200 : activer la suite de qualité de l'analyse statique et mettre au propre le code de la console (#44)
- ANH-129 : rendre idempotente la réception d'une séance relue du journal local (Convex, lot 1 sur 2) (#51)
- ANH-207 : garantir qu'une fenêtre ECG non jugeable n'est jamais notée « good » (#52)
- ANH-185 : ne plus verrouiller d'alerte sur une fin de séance qui se déroule normalement (#55)
- ANH-183 : assainir les tests et la CI, et répartir pi-gate sur deux jobs (#56)
- ANH-213 : juger les paliers cardiaques sur la dernière fréquence utilisable (#58)
- ANH-191 : enregistrer les trames du variateur, refuser un départ sans inodes, journaliser hors séance et borner la sortie (#59)
- ANH-129 : envoyer les séances au tableau de bord en relisant leur enregistrement local, avec reprise après redémarrage (Pi, lot 2 sur 2) (#57)
- ANH-210 : afficher sur la console sa version et l'état du lien avec le tableau de bord (#60)

## cloud-1.0.0 (2026-10-10)

Composant : Convex. Changements depuis : la première version.

- ANH-71 : versionner et relire le socle logiciel
- ANH-121 : sécuriser les clés machine et refuser les accès révoqués
- ANH-132 : matrice d'autorisation Convex par rôle et tests de contrat (#16)
- ANH-177 : renforcer deux contrôles d'autorisation Convex (#17)
- ANH-154 : appliquer exactement les cases cochées dans « Assigner des machines » (#20)
- ANH-155 : ne plus appeler la mutation réservée à l'admin quand un gestionnaire modifie une machine (#23)
- ANH-160 : recalculer « En direct » et « Données périmées » à l'horloge (#24)
- ANH-134 : outiller le processus de release (versions par composant, changelog, check-list, registre des versions) (#26)
- ANH-135 : retirer l'ancien mode d'enregistrement ECG de Convex (couche Convex) (#29)
- ANH-133 : versionner le contrat HTTP entre le Pi et Convex et refuser un pair incompatible (#28)
- ANH-114 : séparer les organisations dans Convex (modèle, autorité, migration, matrice) (#30)
- ANH-193 : juger la fraîcheur sur l'horloge du serveur, statut et dernier signal compris (#39)
- ANH-203 : exiger 80 % de couverture sur Convex et sur le site, et écrire les tests qui manquent (#45)
- ANH-205 : exiger un entier fini pour la physiologie d'un passager (#48)
- ANH-208 : n'enregistrer dans « Assigner des patients » que ce que l'admin voit et a choisi (#50)
- ANH-129 : rendre idempotente la réception d'une séance relue du journal local (Convex, lot 1 sur 2) (#51)
- ANH-195 : fermer les suites Convex et docs d'ANH-133, ANH-134 et ANH-135 (#53)
- ANH-183 : assainir les tests et la CI, et répartir pi-gate sur deux jobs (#56)
- ANH-129 : envoyer les séances au tableau de bord en relisant leur enregistrement local, avec reprise après redémarrage (Pi, lot 2 sur 2) (#57)

## web-1.0.0 (2026-10-10)

Composant : Site. Changements depuis : la première version.

- ANH-71 : versionner et relire le socle logiciel
- ANH-73 : maintain onboarding docs and fix locale root layout (#15)
- ANH-123 : textes restés en anglais et promesse « jusqu'à 3 G » sur le site (#9)
- ANH-154 : appliquer exactement les cases cochées dans « Assigner des machines » (#20)
- ANH-155 : ne plus appeler la mutation réservée à l'admin quand un gestionnaire modifie une machine (#23)
- ANH-160 : recalculer « En direct » et « Données périmées » à l'horloge (#24)
- ANH-134 : outiller le processus de release (versions par composant, changelog, check-list, registre des versions) (#26)
- ANH-135 : retirer l'ancien mode d'enregistrement ECG du site (couche site) (#25)
- ANH-135 : retirer l'ancien mode d'enregistrement ECG de Convex (couche Convex) (#29)
- ANH-133 : versionner le contrat HTTP entre le Pi et Convex et refuser un pair incompatible (#28)
- ANH-156 : afficher un succès ou un échec pour chaque mutation du site (#22)
- ANH-193 : juger la fraîcheur sur l'horloge du serveur, statut et dernier signal compris (#39)
- ANH-161 : installer un Pi par script, image figée lancée par systemd au démarrage (#37)
- ANH-199 : afficher un rapport de qualité à chaque exécution de la CI (#41)
- ANH-203 : exiger 80 % de couverture sur Convex et sur le site, et écrire les tests qui manquent (#45)
- ANH-202 : mettre au propre la simulation et le site signalés par l'analyse statique (#46)
- ANH-204 : tester les pages du site et les faire entrer dans la couverture exigée (#42)
- ANH-205 : exiger un entier fini pour la physiologie d'un passager (#48)
- ANH-208 : n'enregistrer dans « Assigner des patients » que ce que l'admin voit et a choisi (#50)
- ANH-129 : rendre idempotente la réception d'une séance relue du journal local (Convex, lot 1 sur 2) (#51)
- ANH-183 : assainir les tests et la CI, et répartir pi-gate sur deux jobs (#56)

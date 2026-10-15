# Programmes d'entraînement

## Aujourd'hui

Les programmes sont définis et conservés sur chaque Raspberry Pi. Quand leur
révision change, le Pi synchronise avec Convex les profils que sa configuration
de sécurité autorise. Convex les copie dans `machine_profiles`, liés à leur
machine ; `syncProfiles` remplace cette liste en bloc. Le site lit ces profils
pour montrer le nom, la zone, la durée, la FC limite et la vitesse maximale.
Ils sont en lecture seule dans le site. Le Pi reste la source de vérité des
programmes proposés.

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

Le catalogue prévu par EX-5 et ANH-137 référencera
`medicalParametersVersion`. Le Pi devra refuser un programme validé pour une
autre version médicale ; ce contrôle relève d'ANH-138. Tant que ces changements
ne sont pas livrés, la version liée au programme n'est pas vérifiée dans le
catalogue.

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

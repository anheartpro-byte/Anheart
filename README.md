# Anheart

Anheart est le logiciel d'une centrifugeuse d'entraînement : une console locale
pilote le variateur ATV320, acquiert l'ECG du BITalino et applique les règles de
sécurité. Le tableau de bord distant gère les machines, les utilisateurs et les
séances. La simulation exerce le code du Pi sans matériel.

La [documentation du projet](docs/README.md) décrit l'architecture, les
parcours opérateur et les limites du système.

## Se repérer

| Dossier | Rôle |
|---|---|
| [raspberry-pi/](raspberry-pi/README.md) | Console locale Python, variateur, acquisition et supervision ; entrée `python -m src.local_panel` |
| `app/`, `components/`, `lib/`, `messages/` | Tableau de bord Next.js / React, authentification Clerk |
| `convex/` | Données, droits et API machine du tableau de bord |
| [simulation/](simulation/README.md) | Scénarios, cohorte, injections de pannes et visualiseur |

## Démarrer

Suivre le [démarrage rapide](docs/demarrage-rapide.md) pour préparer les
dépendances et la configuration. Le client Python utilise Python 3.12 ou plus
récent et l'environnement `raspberry-pi/.venv`.

Pour lancer la console sans matériel ni liaison distante, depuis `raspberry-pi/` :

```sh
MOTOR_BACKEND=sim ECG_SOURCE=sim MACHINE_API_KEY= ARM_RADIUS_M=1.5 UI_PORT=8080 \
  .venv/bin/python -m src.local_panel
```

Ouvrir `http://127.0.0.1:8080/`. Les paramètres détaillés sont dans
[.env.example](raspberry-pi/.env.example) ; l'installation sur Pi est décrite
dans [le guide de déploiement](docs/deploiement.md).

Pour le site, configurer les clés Clerk et Convex décrites dans le démarrage
rapide, puis exécuter `npm install` et `npm run dev` à la racine.
`npm run dev` lance Next.js et Convex ; son étape `predev` synchronise les
fonctions Convex et ouvre le tableau de bord Convex.

## Vérifier une modification

- Pi, depuis `raspberry-pi/` : `./scripts/check.sh`.
- Simulation, depuis la racine : `simulation/scripts/check.sh`.
- Site, depuis la racine : `npm run lint`, `npm run build`,
  `npm run test:convex`, `npm run test:ecg` et `npm run test:site`.
- Processus de release, depuis la racine : `npm run test:release`
  ([docs/release.md](docs/release.md)).

Lire le [contrat Python strict](.agents/skills/anheart-strict-python/SKILL.md)
avant de modifier le client. Le [framework de test](docs/framework-de-test.md)
explique les scénarios et les résultats ; les [limites de sécurité](docs/securite.md)
restent distinctes d'un résultat de simulation.

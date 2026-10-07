# Moteur de simulation hébergé (Vercel)

Le moteur de `python -m simulation.live`, servi par une fonction Vercel :
<https://anheart-simulation.vercel.app>.

Il se déploie par les deux boutons de GitHub Actions, en choisissant
`simulation` : « Déployer la préversion (develop) » et « Déployer en production
(main) ». Aucun push ne le déploie. Le bouton lance `build.sh` sur le commit de
la branche, puis envoie `dist/` à Vercel.

    ./build.sh            # assemble dist/
    ./deploy.sh           # secours, depuis un poste : préversion
    ./deploy.sh --prod    # secours, depuis un poste : production

Ce qu'il fait, ses limites, les boutons, la connexion Vercel du secours et la
procédure complète : [docs/deploiement.md](../../docs/deploiement.md#6-le-moteur-de-simulation-hébergé).

Avec `deploy.sh`, le code embarqué est celui **du disque** au moment de
`build.sh` (`simulation/`, `raspberry-pi/src`, `raspberry-pi/config`). Avec un
bouton, c'est celui du commit. Dans les deux cas, redéployer après tout
changement de ces dossiers.

Le dépôt garde une seconde disposition, que Vercel construisait à chaque push
avant que les déploiements Git soient coupés : la racine du dépôt, avec
`simulation_app:app` déclaré dans le `pyproject.toml` racine. Ce point d'entrée
charge la même application ; le `requirements.txt` racine inclut les
dépendances de ce dossier, et la table `[project]` du manifeste racine porte
les mêmes : conserver les deux listes alignées. Plus aucun déploiement ne
construit cette disposition ; elle sert en local, où le visualiseur est servi
dans les deux dispositions.

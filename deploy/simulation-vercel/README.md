# Moteur de simulation hébergé (Vercel)

Le moteur de `python -m simulation.live`, servi par une fonction Vercel :
<https://anheart-simulation.vercel.app>.

    ./build.sh            # assemble dist/
    ./deploy.sh           # préversion
    ./deploy.sh --prod    # production

Ce qu'il fait, ses limites, la connexion Vercel à utiliser et la procédure
complète : [docs/deploiement.md](../../docs/deploiement.md#6-le-moteur-de-simulation-hébergé).

Le code embarqué est celui **du disque** au moment de `build.sh`
(`simulation/`, `raspberry-pi/src`, `raspberry-pi/config`). Redéployer après
tout changement de ces dossiers.

Pour les préversions Git, Vercel construit depuis la racine du dépôt avec
`simulation_app:app`, déclaré dans le `pyproject.toml` racine. Ce point d'entrée
charge la même application ; le `requirements.txt` racine inclut les
dépendances de ce dossier. Aucun assemblage préalable de `dist/` n'est requis.
Le visualiseur est également servi en local, dans les deux dispositions.

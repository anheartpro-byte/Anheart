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

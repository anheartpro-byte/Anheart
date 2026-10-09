## Release

<!-- release.sh : versions -->

Cette PR est ouverte par `scripts/release.sh pr`. Le processus complet est dans `docs/release.md`.

## Check-list de release

Chaque case cochée porte sa preuve sur la même ligne (lien, SHA, sortie de commande). Une case sans preuve reste ouverte, et la PR n'est pas fusionnée tant qu'une case est ouverte.

- [ ] **Gates vertes.** Toutes les vérifications CI du candidat sont terminées et réussies : gate Pi, gate simulation, tests Convex, site, audit, docs.
- [ ] **Docs à jour.** Chaque changement de comportement du changelog ci-dessous a sa page de `docs/` à jour.
- [ ] **Menaces revues.** `docs/menaces.md` est relu contre le candidat, et la check-list `docs/release-threat-review.md` est remplie et jointe.
- [ ] **Matrice de compatibilité à jour.** La section « Versions et compatibilité » de `docs/convex.md` et de `docs/raspberry-pi.md` couvre les versions de cette release.
- [ ] **Rapport de simulation joint.** `python -m simulation.quick --all --dsp` a tourné sur le candidat, sans échec ; `report.html` et `report.json` sont joints.
- [ ] **Rejeu des séances réelles vert.** Chaque fichier de `simulation/scenarios/real/` est rejoué sur le candidat sans écart inexpliqué ; si le dossier est vide, c'est écrit ici.
- [ ] **Aucune valeur `[MED]` modifiée sans décision.** Le diff depuis la version précédente est relu ; toute valeur marquée `[MED]` qui change renvoie à sa décision médicale signée, jointe.
- [ ] **Aucun verrou modifié par défaut.** `PROGRAMS_ENABLED` et `OCCUPANCY_OCCUPIED_ENABLED` valent toujours `false` par défaut dans le code et dans les fichiers d'exemple.
- [ ] **Niveau de validation du Pi justifié.** Le niveau annoncé ci-dessus est celui que les revues enregistrées permettent ; au-dessus de `bench`, le compte rendu de la revue M5 ou M6 est joint.
- [ ] **Fenêtre de déploiement fixée.** Rien ne se déploie à la fusion. La date et l'heure de la release réelle par la pipeline « Release en production (main) », qui pose les tags puis déploie Convex, le site et la simulation, sont écrites ici : une même fenêtre, à un moment où aucune séance n'est en cours, avec le nom de la personne qui approuve.

## Changelog

<!-- release.sh : changelog -->

## Fusion et suite

- Fusion par **commit de fusion** (ni squash, ni rebase), une fois l'avis indépendant `agent-review/R1` réussi sur le candidat ci-dessus, sans contournement administrateur. GitHub n'exige aucune approbation sur `main` : la revue est cet avis (`docs/release.md`, section 3).
- Rien ne se déploie à la fusion. Après la fusion et la CI de `main` : la pipeline « Release en production (main) », à blanc d'abord (`docs/release.md`, étape 7).
- Puis la même pipeline pour de bon, dans la fenêtre fixée et hors de toute séance : elle pose les tags par `scripts/release.sh tag`, qui refuse si le candidat ne porte pas `agent-review/R1` réussi, déploie Convex, puis le site et la simulation, chacune de ces écritures après une approbation (`docs/release.md`, étape 8).
- Puis enregistrer chaque version dans Convex (`softwareReleases.recordRelease`), avec les lignes que l'étape 4 de la pipeline a écrites : après le déploiement de Convex, pas avant (`docs/release.md`, étape 9).

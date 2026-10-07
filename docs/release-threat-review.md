# Check-list de release : revue des menaces

Ce contrôle documentaire ANH-136 est obligatoire à chaque jalon, avant chaque
release et avant le pilote ANH-120. C'est une ligne de la
[check-list de release](release.md#5-la-check-list-de-release) ; il ne remplace
ni les gates de release, ni les validations machine, médicales ou humaines. Une
case non prouvée reste ouverte.

- [ ] Le [modèle de menaces](menaces.md) correspond au SHA candidat et aux
  composants effectivement déployés ; les protections locales non déployées
  sont distinguées.
- [ ] Chaque frontière modifiée et incident a été examiné avec les huit flux,
  les actifs et les sept acteurs ; les nouvelles menaces ont un identifiant.
- [ ] Les tickets de toutes les menaces OPEN ont été relus dans Linear ; date,
  UUID et URL du registre ont été actualisés depuis les réponses réelles.
- [ ] Chaque ticket de sécurité cite les MEN concernés et leurs preuves de
  fermeture. Les liens et décisions d'acceptation ont été lus, pas seulement
  validés par le script.
- [ ] `node --test scripts/ci/check-men.test.mjs` et
  `bash scripts/ci/check-men.sh` ont réussi sur le candidat ; sorties jointes.
- [ ] Un reviewer indépendant a cherché les menaces oubliées. Identité, date,
  SHA, verdict et lien de son avis réel figurent en tête du modèle. La PR liste
  explicitement les menaces ajoutées en revue, ou son constat motivé d'absence
  d'ajout. Une revue d'agent est identifiée comme technique.
- [ ] Chaque menace fermée possède une preuve du comportement et du déploiement
  applicable ; chaque risque accepté possède une justification réellement signée,
  avec périmètre et échéance. Un agent n'a pas signé à la place d'un humain.
- [ ] Les changements médicaux et les verrous de mouvement sont renvoyés à leurs
  décisions séparées ; cette check-list ne vaut pas autorisation de personne à bord.

[Sommaire](README.md) · [Sécurité](securite.md)

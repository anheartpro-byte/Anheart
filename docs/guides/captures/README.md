# Captures des pages principales du tableau de bord

Neuf pages du site, en français et en anglais, en thème clair et en thème
sombre, photographiées par la suite de bout en bout
(`e2e/tests/captures.spec.ts`, ANH-83) avec ses données de test : le client B
d'une exécution, sa gestionnaire « Fanny Blanc » et son passager « Gabriel
Bertin » (comptes fictifs), deux machines « Machine démo » et la vraie console
en simulation (fréquence cardiaque simulée, aucune personne réelle). Les
préfixes des clés de machine sont masqués.

Ce premier jeu a été versé le 29 octobre 2026. Chaque exécution de la suite en
refait un, sous les mêmes noms, dans `<E2E_OUTPUT_DIR>/captures/`, et la CI le
garde dans l'artefact `e2e-captures` ; il n'est pas recopié ici
automatiquement. Ce qui change d'une exécution à l'autre : l'identifiant de
l'exécution dans les noms d'organisation et les adresses, les dates, les
courbes. Pour les refaire : [Tests de bout en bout dans un navigateur](../../framework-de-test.md#tests-de-bout-en-bout-dans-un-navigateur-anh-83).

| Page | Français, clair | Français, sombre | Anglais, clair | Anglais, sombre |
|---|---|---|---|---|
| Tableau de bord (gestionnaire) | [01](01-tableau-de-bord-fr-clair.png) | [01](01-tableau-de-bord-fr-sombre.png) | [01](01-tableau-de-bord-en-clair.png) | [01](01-tableau-de-bord-en-sombre.png) |
| Mes machines (passager) | [02](02-mes-machines-fr-clair.png) | [02](02-mes-machines-fr-sombre.png) | [02](02-mes-machines-en-clair.png) | [02](02-mes-machines-en-sombre.png) |
| Fiche machine, avec l'état en direct et les clés (admin Anheart, page entière) | [03](03-fiche-machine-fr-clair.png) | [03](03-fiche-machine-fr-sombre.png) | [03](03-fiche-machine-en-clair.png) | [03](03-fiche-machine-en-sombre.png) |
| Patient, avec l'aptitude et le consentement (gestionnaire) | [04](04-patient-fr-clair.png) | [04](04-patient-fr-sombre.png) | [04](04-patient-en-clair.png) | [04](04-patient-en-sombre.png) |
| Fenêtre de lancement (passager) | [05](05-fenetre-de-lancement-fr-clair.png) | [05](05-fenetre-de-lancement-fr-sombre.png) | [05](05-fenetre-de-lancement-en-clair.png) | [05](05-fenetre-de-lancement-en-sombre.png) |
| Séance en direct, avec ses courbes (passager) | [06](06-seance-en-direct-fr-clair.png) | [06](06-seance-en-direct-fr-sombre.png) | [06](06-seance-en-direct-en-clair.png) | [06](06-seance-en-direct-en-sombre.png) |
| Audit (admin Anheart) | [07](07-audit-fr-clair.png) | [07](07-audit-fr-sombre.png) | [07](07-audit-en-clair.png) | [07](07-audit-en-sombre.png) |
| Rapport de séance (gestionnaire) | [08](08-rapport-de-seance-fr-clair.png) | [08](08-rapport-de-seance-fr-sombre.png) | [08](08-rapport-de-seance-en-clair.png) | [08](08-rapport-de-seance-en-sombre.png) |
| Alertes (gestionnaire) | [09](09-alertes-fr-clair.png) | [09](09-alertes-fr-sombre.png) | [09](09-alertes-en-clair.png) | [09](09-alertes-en-sombre.png) |

Deux remarques que montrent ces captures, sans changement du site dans ce
ticket : sur la séance en direct, le mot « Échauffement » déborde de sa case
sur celle du temps écoulé, à cette largeur ; et les champs de date natifs
gardent le format du navigateur (`mm/dd/yyyy`), quelle que soit la langue.

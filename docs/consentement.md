# Consentement du passager

Version du texte : 0.1.0

> **TEXTE PROVISOIRE, NON VALIDE juridiquement.** À remplacer après l’avis juridique (ANH-87b) et l’avis sur les données de santé (ANH-172). Ce texte ne vaut pas consentement éclairé au sens juridique et ne lève aucune interdiction. Une personne ne peut pas être à bord tant que les paramètres médicaux ne sont pas signés (ANH-98).

<!-- texte-du-consentement:debut -->
Une séance utilise une centrifugeuse en rotation. La fréquence cardiaque du passager est mesurée et pilote la vitesse de rotation.

L’opérateur peut arrêter la séance à tout moment. Le passager peut aussi demander son arrêt à tout moment.

La séance enregistre la fréquence cardiaque, l’ECG et la télémétrie de séance. Le passager, ses gestionnaires et les administrateurs peuvent consulter ces données. Les réponses au questionnaire d’aptitude sont visibles uniquement par ses gestionnaires et les administrateurs.

Le passager peut retirer son consentement à tout moment depuis le site. Après ce retrait, aucun nouveau lancement de séance n’est possible.

La durée de conservation de ces données sera précisée. Ce texte est provisoire et ne constitue pas un avis juridique.
<!-- texte-du-consentement:fin -->

## Maintenance

Ce fichier est la source du texte de consentement. Le site rend le texte compris entre les deux marqueurs. Tout changement du texte impose de changer la version ici et dans `lib/consentText.ts`. Un consentement signé pour une autre version ne compte plus ; cette règle sera appliquée par Convex dans une tâche suivante.

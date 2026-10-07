# Modèle de menaces logiciel Anheart

| Date | Auteur / signature technique | Périmètre | État |
|---|---|---|---|
| 2026-10-05 | Codex, agent `/root/anh136_threat_model` — attribution technique de cette rédaction | Base source courante `4e6720b10a2aefb73f6e1c42e51b4434d00042f7`, ANH-136 | Auteur du modèle et des ajouts MEN-14 à MEN-17 ; actualisation après intégration du source ANH-121 |
| 2026-10-05 | Codex, agent `/root/anh136_aa01af2_gate` — reviewer technique indépendant | Commit `aa01af2030d9a38d67d1f6ec070f678018f096a7`, arbre `fd7883a4cddbac2f7dc76baedd4fb6799ec6f360` | REJECT, confiance HIGH : seul blocage B1, absence de cette inscription de revue (EX-5). Recherche d'omissions effectuée : aucune menace ajoutée. [Compte rendu daté](reviews/anh-136-2026-10-05.md) |

Ce registre nomme le commit effectivement relu. Le verdict REJECT n'est pas une
approbation du candidat corrigé : celui-ci exige une nouvelle revue indépendante
liée à son SHA exact dans le rapport/check de PR. Inscrire ici le SHA du commit
qui contient sa propre inscription serait autoréférentiel. Le registre daté et
la validation du candidat courant restent tous deux obligatoires ; aucune
signature humaine ni acceptation de risque n'est revendiquée.

[Sommaire](README.md) · [Sécurité machine et limites](securite.md) · [Check-list de revue de release](release-threat-review.md)

Cette analyse STRIDE décrit le code de cette base, pas l'état vérifié d'une machine
ou d'un service en production. ANH-121 est intégré au source courant ; sa migration
des clés existantes et son déploiement réel restent non vérifiés et non réalisés
dans ce travail (ANH-82). ANH-74 n'est pas intégré à cette base. La revue historique
ci-dessus portait sur la base `68e4dcf1bda9217329b7d3976519f74c65eb24bd`, avant
ANH-121 ; elle ne vaut pas approbation de cette actualisation. Tous les risques
ci-dessous sont **OPEN** ; aucune décision clinique, réglementaire ou
d'acceptation de risque n'est prise ici. Les analyses ANH-103/104, décisions
médicales ANH-98 et avis ANH-105/172 restent distincts.

## Actifs et conséquences

| Actif | Ce qu'il faut préserver |
|---|---|
| Personne à bord | Identité, présence d'un opérateur et contrôle du mouvement ; un logiciel compromis peut nuire physiquement |
| Données de santé | Confidentialité, exactitude, accès et durée de conservation des signaux, profils et comptes rendus |
| Clés et identités | Secrets machine/console, sessions Clerk, clés de signature et de déploiement |
| Intégrité du logiciel de sécurité | Superviseur, paramètres médicaux, profils, configuration, binaire et provenance |
| Disponibilité de la machine | Contrôle local et arrêt, ressources du Pi, variateur et récupération ; cloud et historique séparément |

Gravité : **critique** si une attaque peut contourner une barrière de mouvement
ou compromettre toute la chaîne ; **haute** pour exposition de santé ou perte
importante de service/preuve. Chaque fiche indique explicitement le lien avec
la personne. Ce classement logiciel ne remplace pas l'analyse de risques machine.

## Acteurs et hypothèses de confiance

| Acteur | Capacité considérée |
|---|---|
| Passager | Compte ordinaire ou accès physique ; peut se tromper, prêter son compte ou être malveillant |
| Opérateur | Console locale et présence sur place ; le nom saisi aujourd'hui ne prouve pas son identité |
| Gestionnaire d'organisation | Droits sur patients/machines ; compte volé ou abus de droits légitimes inclus |
| Admin Anheart | Droits globaux, release et gestion de clés ; compromission et erreur incluses |
| Attaquant LAN | Accès au réseau console, rejeu, écoute, client non navigateur, saturation |
| Attaquant Internet | Appels directs au cloud, phishing, vol de sessions et saturation |
| Fournisseur compromis | Paquet npm/pip, artefact, service d'identité, CI ou hébergeur compromis |

Hypothèses, à revérifier à chaque jalon :

- Le Pi est l'autorité de mouvement dans le code étudié : le cloud propose AUTO
  et stop ; le superviseur et la validation locale doivent encore autoriser.
  Cette propriété suppose l'intégrité du Pi et n'est pas une défense contre root.
- Le jeton machine identifie une machine, pas un opérateur ni un passager.
  Le jeton console partagé n'assure pas l'attribution nominative.
- Le contrôle d'Origin vise un navigateur ; un programme natif peut l'omettre.
  Ni le LAN, ni une connexion Bluetooth, ni un câble Modbus ne sont supposés
  authentifiés simplement parce qu'ils sont proches de la machine.
- Les bibliothèques TLS/JWT et les fournisseurs sont nécessaires à l'architecture,
  mais leurs comptes, clés et configurations peuvent être compromis. Les
  paramètres hébergés n'ont pas été vérifiés dans cette tâche.
- Un profil signé prouve une provenance sous une clé donnée, pas sa validité
  médicale. L'autorisation du passager et les décisions médicales sont externes
  à ce modèle. Aucun seuil n'est proposé ou changé.
- L'E-STOP attesté est une déclaration opérateur par démarrage du processus ;
  ce n'est ni une preuve du câblage réel ni une protection matérielle indépendante
  attestée par ce travail. Les deux verrous de configuration restent faux par
  défaut dans le source ; leur valeur sur une installation n'est pas connue.
- Les erreurs ou pertes de cloud ne doivent pas devenir une autorité de mouvement.
  Elles peuvent néanmoins supprimer télémétrie, preuve et stop distant ;
  la sûreté de l'arrêt matériel exige les essais séparés prévus au banc.

## Frontières, huit flux et STRIDE par couche

S = usurpation, T = altération, R = répudiation, I = divulgation,
D = déni de service, E = élévation de privilège.
Les catégories décrivent les attaques, pas six cases obligatoirement distinctes
par endpoint. Les liens ci-dessous incluent les traversées entre couches.

| Flux | Frontière / données | STRIDE et menaces |
|---|---|---|
| F1 tablette ↔ Pi | Navigateur/LAN → API et WebSocket ; commandes, jeton, télémétrie | S/T/E MEN-02, MEN-05 ; R MEN-03 ; I/D MEN-05 |
| F2 Pi ↔ variateur | Processus/port série → Modbus ; consignes et retours | S/T/D MEN-14 ; E MEN-04 ; R MEN-11 ; I : paramètres lisibles après accès local (MEN-04) |
| F3 BITalino → Pi | Bluetooth/capteur → traitement ; ECG et qualité | S/T/D MEN-15 ; I MEN-13 ; R MEN-11 ; E MEN-04 |
| F4 Pi ↔ Convex | Appareil → Internet/cloud ; Bearer, roster, AUTO, télémétrie | S/E MEN-01, MEN-02 ; T MEN-08, MEN-11 ; R MEN-03, MEN-11 ; I MEN-09, MEN-13 ; D MEN-12 |
| F5 site ↔ Convex | Navigateur non fiable → fonctions/données ; JWT et IDs | S/E MEN-03, MEN-08, MEN-17 ; T/R MEN-03 ; I MEN-09, MEN-13 ; D MEN-12 (ressources cloud partagées) |
| F6 site ↔ Clerk | Navigateur → identité/session → JWT Convex | S/E/I MEN-07, MEN-17 ; T/R MEN-03, MEN-07 ; D : identité indisponible, MEN-07 (accès distant), sans dépendance de l'arrêt local |
| F7 CI → Vercel / Convex | Code tiers/runner → artefact et autorité de déploiement | S/T/E MEN-06, MEN-16 ; R MEN-16 ; I MEN-13, MEN-16 ; D MEN-16 |
| F8 OTA → Pi | Distribution/signature → installation du logiciel de sécurité | S/T/E MEN-10 ; R MEN-10, MEN-16 ; I MEN-04 ; D MEN-10 |

Couche Pi : MEN-02, MEN-04, MEN-05, MEN-10, MEN-11, MEN-14, MEN-15.
Couche Convex : MEN-01, MEN-03, MEN-08, MEN-09, MEN-12, MEN-13, MEN-17.
Couche site/Clerk : MEN-03, MEN-07, MEN-08, MEN-09, MEN-17.
Couche build/livraison : MEN-06, MEN-10, MEN-13, MEN-16.

## MEN-01 Clé machine volée ou rejouée

- Status: OPEN
- Issues: [ANH-121](https://linear.app/anheart/issue/ANH-121/cle-machine-reversible-et-machine-supprimee-encore-authentifiee), [ANH-165](https://linear.app/anheart/issue/ANH-165/convex-rotation-revocation-et-expiration-des-cles-machine-avec), [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en)

F4 · Convex/Pi · S, I, E · Critique, personne indirectement (identité machine), confidentialité et disponibilité.

**Scénario.** Le vol d'une clé encore valide en clair sur le Pi ou dans son environnement permet d'imiter une machine, lire son roster et injecter son état. Les anciennes valeurs réversibles éventuellement conservées dans une base non migrée restent exposées ; leur état réel n'a pas été inspecté.

**Existant vérifié dans le source.** [`generateApiKey`, `hashApiKey`, `verifyApiKey`](../convex/lib/crypto.ts) émettent un credential avec sélecteur aléatoire de 128 bits et secret de 256 bits ; le stockage est un HMAC-SHA256 versionné avec sel aléatoire de 128 bits, vérifié par `crypto.subtle.verify`. [`createMachine` et `regenerateApiKey`](../convex/machines.ts) stockent le sélecteur et le digest, et ne renvoient la clé en clair que dans le résultat d'émission. La nouvelle clé n'est pas reconstructible depuis ces champs. [`authenticateMachine`](../convex/lib/machineAuth.ts) refuse une machine supprimée ou `authenticationEnabled === false`, puis vérifie le credential complet ; hors ligne seul n'est pas une révocation. `getMachineByApiKey` et `validateMachineApiKey` partagent cette garde. [`validateMachineAuth`](../convex/lib/machineHttpAuth.ts), utilisé par les routes de [`http.ts`](../convex/http.ts), exige le Bearer et renvoie 401 en cas de refus. Les formats hérités sont refusés par ce nouveau chemin ; cela ne supprime ni ne migre les anciennes valeurs déjà stockées. Ces protections existent dans le source intégré, sans preuve de leur déploiement réel. Le digest ne protège pas contre le rejeu d'une clé valide volée.

**Manquant.** [ANH-121](https://linear.app/anheart/issue/ANH-121/cle-machine-reversible-et-machine-supprimee-encore-authentifiee) reste incomplet au sens de son acceptation finale tant que [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en) n'a pas assuré la migration coordonnée des anciennes clés et le déploiement vérifié. [ANH-165](https://linear.app/anheart/issue/ANH-165/convex-rotation-revocation-et-expiration-des-cles-machine-avec) porte rotation, révocation et expiration ; le vol/rejeu d'un Bearer valide reste possible. La limitation de débit reste MEN-12 / ANH-166. La fusion du correctif n'est ni une migration des enregistrements hérités, ni une preuve de protection en production, ni une fermeture de MEN-01.

**Preuve de fermeture attendue.** Clé ancienne/révoquée/supprimée refusée sur toutes les routes ; rotation synthétique avant migration coordonnée.

## MEN-02 Lancement distant forgé ou rejoué

- Status: OPEN
- Issues: [ANH-85](https://linear.app/anheart/issue/ANH-85/lancement-a-distance-confirmation-physique-obligatoire-a-la-machine), [ANH-144](https://linear.app/anheart/issue/ANH-144/identite-et-droits-reverifies-a-larmement-passager-affiche-et-confirme), [ANH-152](https://linear.app/anheart/issue/ANH-152/console-locale-authentification-de-loperateur-connexion-nommee-par)

F1, F4, F5 · Pi/Convex · S, T, E · Critique, personne directement.

**Scénario.** Un compte autorisé volé, une demande ancienne ou une identité de passager erronée provoque une rotation alors que personne n'a confirmé la préparation sur place.

**Existant vérifié dans le source.** [`launchAutoSession` et `markTrainingStarted`](../convex/training.ts) vérifient droits au clic, machine et statut `pending` à la confirmation. [`CloudSync._poll`](../raspberry-pi/src/cloud_sync.py) soumet immédiatement à `ControlSurface.submit_start`; [`_start_programme` / `_prepare_programme`](../raspberry-pi/src/local_panel.py) imposent les portes locales. L'attestation E-STOP au démarrage du processus ne vaut pas confirmation physique de chaque départ.

**Manquant.** [ANH-85](https://linear.app/anheart/issue/ANH-85/lancement-a-distance-confirmation-physique-obligatoire-a-la-machine) impose le geste local ; [ANH-144](https://linear.app/anheart/issue/ANH-144/identite-et-droits-reverifies-a-larmement-passager-affiche-et-confirme) l'identité confirmée, la revalidation des droits, l'expiration et l'unicité ; [ANH-152](https://linear.app/anheart/issue/ANH-152/console-locale-authentification-de-loperateur-connexion-nommee-par) l'opérateur authentifié.

**Preuve de fermeture attendue.** Aucun mouvement sans confirmation locale ; demande expirée, droit retiré ou replay refusé, y compris après reconnexion.

## MEN-03 Gestionnaire malveillant ou compte gestionnaire compromis

- Status: OPEN
- Issues: [ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site), [ANH-167](https://linear.app/anheart/issue/ANH-167/convex-journal-daudit-immuable-des-actions-sensibles-consultable-et), [ANH-144](https://linear.app/anheart/issue/ANH-144/identite-et-droits-reverifies-a-larmement-passager-affiche-et-confirme)

F5 · Convex/site · T, R, E, I · Critique, personne directement via la physiologie et les droits.

**Scénario.** Un gestionnaire abuse de droits légitimes, change la physiologie d'un passager ou accède à un autre centre ; l'authentification ne prouve pas la bienveillance.

**Existant vérifié dans le source.** [`canAccessUser`, `canAccessMachine`, `requireRole`](../convex/lib/auth.ts) et [`setUserPhysiology`](../convex/training.ts) imposent liens gestionnaire/patient et bornes de valeurs. Depuis le premier lot d'[ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site), ces règles répondent pour une seule organisation : l'organisation et le rôle de l'appel viennent des revendications du jeton vérifié, et un gestionnaire ou un admin d'organisation n'atteint ni machine, ni patient, ni séance d'une autre organisation ; [`authorization.matrix.ts`](../convex/authorization.matrix.ts) et [`organizationIsolation.test.ts`](../convex/organizationIsolation.test.ts) le vérifient pour chaque fonction publique, y compris par identifiant direct. À l'intérieur d'une organisation, une valeur plausible peut rester malveillante. L'admin de l'organisation Anheart garde des droits sur toutes les organisations.

**Manquant.** Le cloisonnement d'[ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site) existe dans le source intégré ; son effet sur le service rendu dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en), de la migration et de la configuration de Clerk Organizations, et ses lots suivants (miroir par webhook, site) restent à livrer ; [ANH-167](https://linear.app/anheart/issue/ANH-167/convex-journal-daudit-immuable-des-actions-sensibles-consultable-et) trace les actions sensibles ; [ANH-144](https://linear.app/anheart/issue/ANH-144/identite-et-droits-reverifies-a-larmement-passager-affiche-et-confirme) revérifie le passager et les droits à l'armement. Ces mesures réduisent l'abus sans établir une validation médicale.

**Preuve de fermeture attendue.** Tests croisés entre organisations, gestionnaire non lié refusé, modification attribuée et révocation avant armement effective.

## MEN-04 Pi compromis : logiciel de sécurité, paramètres et profils

- Status: OPEN
- Issues: [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu), [ANH-139](https://linear.app/anheart/issue/ANH-139/parametres-medicaux-signes-fichier-versionne-verification-au-demarrage), [ANH-138](https://linear.app/anheart/issue/ANH-138/publication-des-programmes-vers-les-machines-le-pi-recupere-revalide)

F1, F2, F3, F4, F8 · Pi · T, E · Critique, personne directement.

**Scénario.** Un accès privilégié ou un paquet compromis modifie le superviseur, les limites, les programmes ou leurs clés de vérification.

**Existant vérifié dans le source.** [`local_config._flag`](../raspberry-pi/src/local_config.py) met `PROGRAMS_ENABLED` et `OCCUPANCY_OCCUPIED_ENABLED` à faux par défaut ; [`SafetySupervisor.require_estop_confirmed`](../raspberry-pi/src/training/safety.py) bloque sans attestation et [`_prepare_programme`](../raspberry-pi/src/local_panel.py) revalide le profil. Ces barrières exécutées sur le même Pi ne résistent pas à un root compromis ; aucune preuve de démarrage mesuré n'est revendiquée.

**Manquant.** [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu) durcit le système et les secrets ; [ANH-139](https://linear.app/anheart/issue/ANH-139/parametres-medicaux-signes-fichier-versionne-verification-au-demarrage) vérifie les paramètres médicaux signés ; [ANH-138](https://linear.app/anheart/issue/ANH-138/publication-des-programmes-vers-les-machines-le-pi-recupere-revalide) revalide les programmes reçus. La racine de confiance et ses limites doivent rester explicites ; les analyses physiques ANH-103/104 restent séparées.

**Preuve de fermeture attendue.** Paramètre/profil falsifié refusé avant armement ; image durcie testée, sans activer les deux verrous ni inventer de seuil médical.

## MEN-05 Appareil hostile sur le réseau de la console

- Status: OPEN
- Issues: [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu), [ANH-152](https://linear.app/anheart/issue/ANH-152/console-locale-authentification-de-loperateur-connexion-nommee-par)

F1 · Pi/tablette · S, T, I, D, E · Critique, personne directement.

**Scénario.** Un appareil LAN intercepte le jeton, appelle des commandes ou inonde la console ; une page web tente d'ouvrir le WebSocket.

**Existant vérifié dans le source.** [`WebConfig.__post_init__`, `token_matches`, `origin_allowed`](../raspberry-pi/src/web/deps.py) refusent le bind hors loopback sans jeton d'au moins 16 caractères et comparent en temps constant ; [`serve_telemetry`](../raspberry-pi/src/web/ws.py) contrôle Origin et jeton avant acceptation. Origin absent est autorisé pour un client non navigateur, toujours soumis au jeton ; HTTP n'est pas TLS. Le jeton WebSocket en query peut être journalisé et le nom saisi n'authentifie personne. L'analyse statique du dépôt ([`codeql.yml`](../.github/workflows/codeql.yml), [Analyse statique externe](framework-de-test.md#analyse-statique-externe--codeql-anh-196)) suit à chaque PR et à chaque push sur `develop` ce qui vient d'une requête jusqu'aux chemins de fichiers et aux en-têtes de réponse ; ce n'est pas une gate, et elle ne voit que les trajets qu'elle sait suivre. [`resolve_under`](../raspberry-pi/src/record/containment.py) confine un nom reçu de l'extérieur au dossier permis, liens symboliques suivis ([`test_record_containment.py`](../raspberry-pi/tests/test_record_containment.py)) ; le visualiseur du simulateur est aujourd'hui son seul appelant.

**Manquant.** [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu) prévoit AP isolé, pare-feu et TLS ; [ANH-152](https://linear.app/anheart/issue/ANH-152/console-locale-authentification-de-loperateur-connexion-nommee-par) identité nominative, habilitation et expiration. La fuite du jeton ne constitue pas un risque accepté par la présente analyse.

**Preuve de fermeture attendue.** Console inaccessible depuis l'interface Internet ; mauvais jeton/Origin refusé ; accès TLS et sessions d'opérateurs isolées, charge hostile sans perte de boucle locale.

## MEN-06 Compromission d'un paquet pip ou npm

- Status: OPEN
- Issues: [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite), [ANH-126](https://linear.app/anheart/issue/ANH-126/cicd-deploiement-vercel-et-convex-automatise-rapports-du-framework-de)

F7, F8 · Livraison/Pi/site/Convex · T, E · Critique, personne potentiellement directement après livraison.

**Scénario.** Un fournisseur ou mainteneur compromis publie une dépendance malveillante exécutée au build ou au runtime ; une absence d'avis CVE ne prouve pas son innocuité.

**Existant vérifié dans le source.** [`ci.yml`, jobs audit/web/python](../.github/workflows/ci.yml) utilise `npm ci`, npm audit, pip-audit, actions épinglées par SHA et tests ; [`package-lock.json`](../package-lock.json) fige le graphe npm. Le job Python installe des requirements sans exiger leurs hashes ; les hashes générés pour l'audit ne sécurisent pas rétroactivement l'installation. Un audit de vulnérabilités ne détecte pas tout code malveillant.

**Manquant.** [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite) porte dépendances figées avec empreintes, inventaire/SBOM, secrets et audit ; [ANH-126](https://linear.app/anheart/issue/ANH-126/cicd-deploiement-vercel-et-convex-automatise-rapports-du-framework-de) la livraison liée aux gates et ses droits.

**Preuve de fermeture attendue.** Altération d'une dépendance détectée avant livraison ; vulnérabilité bloquante et secret synthétique refusés ; provenance et contenu du paquet revus.

## MEN-07 Prise de compte Clerk

- Status: OPEN
- Issues: [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite), [ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site)

F6, F5 · Site/Convex · S, E, I · Critique, personne indirectement via lancement et données.

**Scénario.** Phishing, cookie volé ou révocation non propagée donne les droits d'un utilisateur, gestionnaire ou admin à l'attaquant.

**Existant vérifié dans le source.** [`proxy.ts`, middleware Clerk](../proxy.ts) protège dashboard/settings ; [`ConvexClientProvider`](../components/ConvexClientProvider.tsx) transmet l'identité Clerk ; [`auth.config.ts`](../convex/auth.config.ts) configure issuer et audience `convex` ; [`requireAuth`](../convex/lib/auth.ts) exige l'identité serveur. Depuis le premier lot d'[ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site), `getCurrentUserOrThrow` lit l'organisation et le rôle de l'appel dans les revendications du jeton vérifié (`org_id`, `org_role`) : aucun argument ne les fournit, le rôle stocké n'autorise pas l'appelant quand le jeton porte une organisation, et une organisation ou un rôle inconnus sont refusés ([`organizations.test.ts`](../convex/organizations.test.ts)). Le proxy n'est pas la garde des fonctions Convex publiques. Aucune exigence MFA vérifiée dans `requireRole`.

**Manquant.** [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite) porte MFA, session bornée et révocation. La lecture de l'appartenance et du rôle d'organisation côté serveur ([ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site)) existe dans le source intégré ; son effet sur le service rendu dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en) et de la configuration de Clerk Organizations, et le miroir des appartenances par webhook reste à livrer.

**Preuve de fermeture attendue.** Session révoquée et mutations sensibles sans preuve MFA refusées ; rôle fourni par un client ignoré.

## MEN-08 Fonction Convex publique sans contrôle suffisant

- Status: OPEN
- Issues: [ANH-177](https://linear.app/anheart/issue/ANH-177/convex-renforcer-deux-controles-dautorisation-men-08-men-17), [ANH-132](https://linear.app/anheart/issue/ANH-132/tests-convex-matrice-dautorisation-par-role-et-organisation-contrat), [ANH-122](https://linear.app/anheart/issue/ANH-122/corriger-les-defauts-fonctionnels-du-site-releves-par-la-documentation), [ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site)

F4, F5 · Convex · S, T, I, E · Critique, personne indirectement.

**Scénario.** Une fonction exposée oublie la garde ou contrôle l'identité sans contrôler la ressource ; le client appelle directement l'API en contournant le site.

**Existant vérifié dans le source.** [`requireAuth` / `canAccessUser`](../convex/lib/auth.ts) existent mais doivent être appelés par chaque endpoint. [`getSessionTelemetry`](../convex/training.ts) vérifie passager ou machine. Depuis le premier lot d'[ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site), chaque fonction publique répond dans l'organisation de l'appelant ; la matrice appelle chacune depuis une autre organisation, et [`completeness.test.ts`](../convex/completeness.test.ts) échoue si une fonction n'a pas ce cas ou si une fonction qui prend un identifiant n'est pas appelée avec celui d'une autre organisation. Quatre routes de [`http.ts`](../convex/http.ts) lisent ou modifient une séance désignée par son identifiant : `/training/start`, `/training/end`, `/training/status` et `/training/telemetry`. Chacune transmet la machine authentifiée à sa fonction interne (`markTrainingStarted`, `endTrainingSession`, `getTrainingStatus`, `storeTelemetry` dans [`training.ts`](../convex/training.ts)), qui vérifie d'abord que la séance appartient à cette machine. Pour ces quatre routes, une séance d'une autre machine reçoit exactement la réponse d'une séance inconnue (statut, en-têtes et corps), quel que soit l'état de la séance, et ni la séance, ni sa machine, ni les mesures enregistrées ne changent ; [`httpRoutes.test.ts`](../convex/httpRoutes.test.ts) compare les deux réponses pour chaque route ([ANH-177](https://linear.app/anheart/issue/ANH-177/convex-renforcer-deux-controles-dautorisation-men-08-men-17)). Voir aussi MEN-17.

**Manquant.** [ANH-132](https://linear.app/anheart/issue/ANH-132/tests-convex-matrice-dautorisation-par-role-et-organisation-contrat) couvre la matrice de toutes les fonctions publiques et routes ; [ANH-122](https://linear.app/anheart/issue/ANH-122/corriger-les-defauts-fonctionnels-du-site-releves-par-la-documentation) les défauts fonctionnels documentés. L'isolation par organisation d'[ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site) existe dans le source intégré ; comme les autres, son effet sur le service rendu dépend du redéploiement. Le contrôle d'[ANH-177](https://linear.app/anheart/issue/ANH-177/convex-renforcer-deux-controles-dautorisation-men-08-men-17) existe dans le source intégré ; son effet sur le service rendu dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en). Les routes héritées de l'ancien mode d'enregistrement ECG sont retirées du source ([ANH-135](https://linear.app/anheart/issue/ANH-135/retirer-lancien-mode-enregistrement-ecg-srcmain-routes-sessiondata)) : [`legacyRecordingRetired.test.ts`](../convex/legacyRecordingRetired.test.ts) vérifie qu'elles répondent 404 ; leur disparition du service rendu dépend aussi du redéploiement ANH-82.

**Preuve de fermeture attendue.** Appel anonyme et identifiant de ressource d'une autre machine/organisation refusés pour chaque fonction ; export public ajouté sans cas de matrice fait échouer la gate.

## MEN-09 URL de fichier Convex exposée

- Status: OPEN
- Issues: [ANH-130](https://linear.app/anheart/issue/ANH-130/depot-des-enregistrements-de-seance-dans-convex-storage-organise-par), [ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site)

F4, F5 · Convex Storage/site · I · Haute, confidentialité santé ; pas de mouvement direct.

**Scénario.** Une URL de téléchargement devient une capacité transférable, fuit par historique, referrer ou logs et contourne la révocation des droits.

**Existant vérifié dans le source.** [`schema.ts`, `session_summaries.reportFileId`](../convex/schema.ts) déclare un identifiant de stockage ; [`getSummary`](../convex/sessionSummaries.ts) retourne les métadonnées. Aucun `ctx.storage.getUrl` ni chemin de dépôt/téléchargement de ces enregistrements n'a été trouvé dans `convex/` à cette base. Il n'existe donc pas encore de protection de téléchargement à créditer ; risque anticipé, pas preuve d'une URL actuellement exposée.

**Manquant.** [ANH-130](https://linear.app/anheart/issue/ANH-130/depot-des-enregistrements-de-seance-dans-convex-storage-organise-par) impose un téléchargement via route JWT autorisée sans URL publique et la vérification des fichiers ; [ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site) fournit le cloisonnement par organisation des séances auxquelles ces fichiers se rattacheront (dans le source intégré, aucun fichier n'étant encore déposé).

**Preuve de fermeture attendue.** Copie du lien ou identifiant de fichier sans identité autorisée refusée ; changement d'organisation/révocation effectif à chaque téléchargement.

## MEN-10 Mise à jour OTA falsifiée ou rétrogradée

- Status: OPEN
- Issues: [ANH-168](https://linear.app/anheart/issue/ANH-168/ota-paquets-de-version-signes-par-la-ci-verification-de-signature-sur), [ANH-170](https://linear.app/anheart/issue/ANH-170/ota-jamais-pendant-une-seance-installation-seulement-machine-au-repos), [ANH-171](https://linear.app/anheart/issue/ANH-171/ota-essai-de-retour-arriere-volontaire-et-procedure-documentee)

F8 · Livraison/Pi · S, T, E, D · Critique, personne directement.

**Scénario.** Un attaquant remplace un artefact, son manifeste ou sa clé, ou réinstalle une ancienne version pourtant signée qui retire une protection ; une mise à jour pendant une séance perturbe le contrôle.

**Existant vérifié dans le source.** [`ci.yml`](../.github/workflows/ci.yml) exécute des gates, mais aucun workflow de release Pi, vérificateur de signature OTA ou hook d'installation n'existe à cette base. L'attestation E-STOP au démarrage n'authentifie pas un binaire.

**Manquant.** [ANH-168](https://linear.app/anheart/issue/ANH-168/ota-paquets-de-version-signes-par-la-ci-verification-de-signature-sur) signature et niveau de validation ; [ANH-170](https://linear.app/anheart/issue/ANH-170/ota-jamais-pendant-une-seance-installation-seulement-machine-au-repos) installation uniquement au repos avec auto-test ; [ANH-171](https://linear.app/anheart/issue/ANH-171/ota-essai-de-retour-arriere-volontaire-et-procedure-documentee) retour arrière essayé et documenté. La règle anti-rétrogradation de sécurité et la rotation/révocation des clés de signature doivent être précisées lors de ces travaux : une signature valide ne suffit pas.

**Preuve de fermeture attendue.** Artefact altéré/autre clé refusé ; version de niveau inférieur refusée ; installation en séance refusée ; rollback conserve les contraintes de sécurité.

## MEN-11 Horloge du Pi manipulée

- Status: OPEN
- Issues: [ANH-163](https://linear.app/anheart/issue/ANH-163/pi-horloge-fiable-ntp-ou-rtc-journaux-persistants-et-bornes), [ANH-142](https://linear.app/anheart/issue/ANH-142/controles-pre-vol-automatiques-avant-chaque-seance-variateur-bitalino)

F3, F4 · Pi/Convex · T, R, D · Haute, personne via pré-vol ; intégrité des preuves.

**Scénario.** Un changement d'heure fausse l'âge, les dates de profils, les traces ou l'acceptation de télémétrie ; perte réseau au démarrage rend l'heure non fiable.

**Existant vérifié dans le source.** [`RealClock.monotonic` et `unix_millis`](../raspberry-pi/src/clock.py) séparent durées de sécurité et horodatage ; [`SafetySupervisor`](../raspberry-pi/src/training/safety.py) utilise le monotone ; La borne que le serveur appliquait à l'heure des lots ECG a disparu avec la route de l'ancien mode d'enregistrement ; l'heure de la télémétrie d'entraînement reste celle du Pi ([`http.ts`](../convex/http.ts)). [`_prepare_programme`](../raspberry-pi/src/local_panel.py) transmet l'heure murale à la résolution du profil : toute décision n'est donc pas indépendante de l'heure.

**Manquant.** [ANH-163](https://linear.app/anheart/issue/ANH-163/pi-horloge-fiable-ntp-ou-rtc-journaux-persistants-et-bornes) heure fiable et indication de confiance ; [ANH-142](https://linear.app/anheart/issue/ANH-142/controles-pre-vol-automatiques-avant-chaque-seance-variateur-bitalino) pré-vol bloque un état d'horloge non fiable avant personne à bord. Un système privilégié compromis reste couvert par MEN-04.

**Preuve de fermeture attendue.** Horloge murale reculée/avancée sans raccourcir les délais monotones ; démarrage hors réseau signale heure non fiable et bloque le pré-vol occupé.

## MEN-12 Déni de service des routes machine

- Status: OPEN
- Issues: [ANH-166](https://linear.app/anheart/issue/ANH-166/convex-limitation-de-debit-par-machine-et-par-utilisateur-taille)

F4 · Convex · D · Haute, disponibilité et preuve ; pas de mouvement direct.

**Scénario.** Appels anonymes ou clé valide compromise saturent l'authentification, le parsing JSON, le stockage ou les quotas cloud ; les demandes de stop distantes deviennent indisponibles.

**Existant vérifié dans le source.** [`machineRoute` et route `/training/telemetry`](../convex/http.ts) authentifient et limitent à 600 points après parsing ; [`CloudSync.step`](../raspberry-pi/src/cloud_sync.py) sépare la synchronisation de l'autorité locale. Il n'y a pas de limite de débit applicative ni de refus de taille avant parsing à créditer. La perte du cloud ne remplace jamais l'arrêt local.

**Manquant.** [ANH-166](https://linear.app/anheart/issue/ANH-166/convex-limitation-de-debit-par-machine-et-par-utilisateur-taille) limite par machine/utilisateur et taille des corps avant parcours, avec recul du Pi.

**Preuve de fermeture attendue.** Rafale de heartbeats et corps trop gros refusés avec codes attendus ; commande locale et boucle de contrôle restent disponibles.

## MEN-13 Fuite de données de santé dans les journaux d'erreur

- Status: OPEN
- Issues: [ANH-150](https://linear.app/anheart/issue/ANH-150/vues-de-flotte-et-alertes-equipe-liste-filtrable-detail-machine), [ANH-163](https://linear.app/anheart/issue/ANH-163/pi-horloge-fiable-ntp-ou-rtc-journaux-persistants-et-bornes)

F3, F4, F5, F7 · Pi/Convex/site/exploitation · I · Haute, confidentialité santé.

**Scénario.** Une exception, une trace, un rapport CI ou un outil de suivi exporte ECG, fréquence cardiaque, réponses ou identité passager hors des accès prévus.

**Existant vérifié dans le source.** [`CloudSync._note`](../raspberry-pi/src/cloud_sync.py) limite la répétition des erreurs, sans expurger tous les détails serveur ; [`machineRoute`](../convex/http.ts) renvoie `error.message`. Le calcul de résumé qui journalisait moyenne/min/max de FC et HRV a été retiré du source avec l'ancien mode d'enregistrement ECG ; il reste présent dans la version déployée jusqu'au redéploiement ANH-82. L'analyse statique du dépôt ([`codeql.yml`](../.github/workflows/codeql.yml), [Analyse statique externe](framework-de-test.md#analyse-statique-externe--codeql-anh-196)) cherche les valeurs écrites en clair dans un journal ou sur la sortie standard quand leur nom désigne une donnée privée, fréquence cardiaque comprise ; elle les reconnaît à leur nom, pas à leur contenu, et n'est pas une gate. Aucune garantie générale de logs sans données de santé n'est donc acquise.

**Manquant.** [ANH-150](https://linear.app/anheart/issue/ANH-150/vues-de-flotte-et-alertes-equipe-liste-filtrable-detail-machine) exige les erreurs sans variables/données de santé et un test d'exclusion ; [ANH-163](https://linear.app/anheart/issue/ANH-163/pi-horloge-fiable-ntp-ou-rtc-journaux-persistants-et-bornes) journaux persistants bornés sans données de santé. Les identifiants de séance et opérateur sont aussi à traiter selon le contexte.

**Preuve de fermeture attendue.** Erreur avec données synthétiques sentinelles : absence de champs de santé et identifiants passager dans chaque export de logs ; purge et accès examinés sans données réelles.

## MEN-14 Injection Modbus ou double maître sur le câble variateur

- Status: OPEN
- Issues: [ANH-74](https://linear.app/anheart/issue/ANH-74/verrou-unique-sur-le-cable-variateur-console-bench-console), [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu)

F2 · Pi/variateur · S, T, D · Critique, personne directement.

**Scénario.** Un outil de banc concurrent ou un accès physique/privilégié au port écrit une consigne contradictoire ou forge un retour.

**Existant vérifié dans le source.** [`ATV320Drive.open`](../raspberry-pi/src/motor/atv320.py) valide l'adressage par lecture ; le backend sérialise ses appels dans le processus. [`SafetySupervisor._rule_setpoint_unconfirmed`](../raspberry-pi/src/training/safety.py) surveille les retours. Un verrou de coroutine n'est pas un verrou interprocessus ni une authentification Modbus ; un retour forgé peut tromper le contrôle.

**Manquant.** [ANH-74](https://linear.app/anheart/issue/ANH-74/verrou-unique-sur-le-cable-variateur-console-bench-console) réserve le transport entre processus coopérants ; [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu) restreint l'accès au périphérique. Le travail parallèle ANH-74 n'est pas intégré à la base ici ; verrou logiciel et permissions ne couvrent pas une injection physique/root, à reprendre dans ANH-103/104.

**Preuve de fermeture attendue.** Second processus refusé avant ouverture/écriture ; accès non habilité au périphérique refusé ; danger d'injection physique explicitement repris à la revue machine.

## MEN-15 BITalino usurpé ou signal physiologique forgé

- Status: OPEN
- Issues: [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu), [ANH-80](https://linear.app/anheart/issue/ANH-80/valider-les-6-capteurs-bitalino-reels-fonctions-de-transfert-gains), [ANH-142](https://linear.app/anheart/issue/ANH-142/controles-pre-vol-automatiques-avant-chaque-seance-variateur-bitalino)

F3 · Pi/capteur · S, T, D · Critique, personne directement.

**Scénario.** Un appareil substitué, Bluetooth perturbé ou un signal artificiel produit une FC plausible, ou supprime la mesure pendant la rotation.

**Existant vérifié dans le source.** [`EcgBridge._follow`, `_vet`, `judge`](../raspberry-pi/src/ecg_pipeline.py) contrôlent continuité et accord des deux traitements ; [`SafetySupervisor._rule_hr_stale`](../raspberry-pi/src/training/safety.py) réagit aux mesures périmées. Deux traitements du même signal ne prouvent pas son origine ni sa vérité ; aucune authentification cryptographique du capteur n'est établie par cette lecture.

**Manquant.** [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu) limite l'accès au Pi ; [ANH-80](https://linear.app/anheart/issue/ANH-80/valider-les-6-capteurs-bitalino-reels-fonctions-de-transfert-gains) valide les capteurs réels ; [ANH-142](https://linear.app/anheart/issue/ANH-142/controles-pre-vol-automatiques-avant-chaque-seance-variateur-bitalino) pré-vol vérifie leur état. Ces tickets ne promettent pas à eux seuls une liaison anti-usurpation : la revue indépendante doit préciser ce résiduel et son traitement avant clôture.

**Preuve de fermeture attendue.** Déconnexion/perte de continuité traitée ; capteur substitué et signal plausible falsifié explicitement examinés au banc avant toute acceptation humaine.

## MEN-16 Secrets CI ou artefacts de livraison compromis

- Status: OPEN
- Issues: [ANH-126](https://linear.app/anheart/issue/ANH-126/cicd-deploiement-vercel-et-convex-automatise-rapports-du-framework-de), [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite), [ANH-134](https://linear.app/anheart/issue/ANH-134/processus-de-release-versions-semantiques-pi-convex-site-changelog)

F7 · Livraison/site/Convex · S, T, R, E · Critique, personne indirectement via logiciel livré.

**Scénario.** Un contributeur, compte GitHub/Vercel compromis ou runner altéré vole des credentials de déploiement, livre un commit non revu ou substitue l'artefact après les tests.

**Existant vérifié dans le source.** [`ci.yml`](../.github/workflows/ci.yml) déclare `contents: read`, actions par SHA et `persist-credentials: false`; les gates auditées ne constituent pas un déploiement Vercel/Convex authentifié. [`codeql.yml`](../.github/workflows/codeql.yml) fait aussi lire les workflows GitHub Actions du dépôt par l'analyse statique ([Analyse statique externe](framework-de-test.md#analyse-statique-externe--codeql-anh-196)), sans en faire une gate. Le workflow de déploiement automatique prévu n'est pas présent ; protections des branches, variables hébergées et paramètres SaaS n'ont pas été inspectés ici. [`scripts/release.sh`](../scripts/release.sh) refuse de préparer une release, d'ouvrir la PR `develop` vers `main` ou de poser un tag quand une vérification CI du commit visé n'est pas terminée et réussie, et chaque composant porte une version lisible ([release.md](release.md)) ; ce contrôle tourne sur le poste du responsable de release et ne relie pas encore l'artefact déployé au commit tagué.

**Manquant.** [ANH-126](https://linear.app/anheart/issue/ANH-126/cicd-deploiement-vercel-et-convex-automatise-rapports-du-framework-de) lie aperçu et production aux gates ; [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite) inventaire/rotation des secrets et audit ; [ANH-134](https://linear.app/anheart/issue/ANH-134/processus-de-release-versions-semantiques-pi-convex-site-changelog) a écrit le processus (version, changelog, check-list), la première release réelle reste à faire. L'identité de l'artefact testé doit être celle livrée.

**Preuve de fermeture attendue.** Échec de gate empêche livraison ; PR non fiable sans secret de production ; SHA et empreinte de l'artefact livré reliés au rapport et au reviewer.

## MEN-17 Liaison d'une fiche patient à un compte usurpateur

- Status: OPEN
- Issues: [ANH-177](https://linear.app/anheart/issue/ANH-177/convex-renforcer-deux-controles-dautorisation-men-08-men-17), [ANH-157](https://linear.app/anheart/issue/ANH-157/site-invitations-par-clerk-organizations-liaison-dun-patient-pre-cree), [ANH-132](https://linear.app/anheart/issue/ANH-132/tests-convex-matrice-dautorisation-par-role-et-organisation-contrat)

F5, F6 · Convex/site · S, I, E · Haute, identité et santé ; personne indirectement.

**Scénario.** Un utilisateur connecté fournit l'e-mail d'un autre patient précréé pour récupérer sa fiche et ses droits sans prouver qu'il contrôle cet e-mail.

**Existant vérifié dans le source.** [`users.linkPatientToClerk`](../convex/users.ts) exige `requireAuth`, puis ne lie un dossier que si l'e-mail vérifié de l'identité de l'appelant (claims `email` et `email_verified`) est celui du dossier ; la comparaison ignore la casse des lettres ASCII seulement. L'argument `email` ne fait pas autorité, une identité sans e-mail vérifié est refusée, un dossier déjà lié n'est jamais relié, et aucune réponse ne distingue « aucun dossier » de « e-mail différent » ([ANH-177](https://linear.app/anheart/issue/ANH-177/convex-renforcer-deux-controles-dautorisation-men-08-men-17)).

**Manquant.** [ANH-157](https://linear.app/anheart/issue/ANH-157/site-invitations-par-clerk-organizations-liaison-dun-patient-pre-cree) remplace la liaison par une invitation et un webhook Clerk signés, dans la bonne organisation ; [ANH-132](https://linear.app/anheart/issue/ANH-132/tests-convex-matrice-dautorisation-par-role-et-organisation-contrat) couvre l'appel direct de cette fonction. Le contrôle d'[ANH-177](https://linear.app/anheart/issue/ANH-177/convex-renforcer-deux-controles-dautorisation-men-08-men-17) existe dans le source intégré ; son effet sur le service rendu dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en).

**Preuve de fermeture attendue.** Compte synthétique A ne peut lier la fiche non revendiquée B en fournissant son e-mail ; invitation signée autorisée et rejeu webhook idempotent.

## Traçabilité vérifiable et entretien

Le format consommé par le test est un titre `## MEN-nn`, une ligne
`- Status: OPEN` et une ligne `- Issues:` de liens Markdown Linear.
Les identifiants ne sont jamais réutilisés. Les autres mentions sont des
références, y compris celles des tableaux. Les états `MITIGATED` et `ACCEPTED`
exigent une ligne `- Evidence:` vers un fichier local non vide dans docs ;
`ACCEPTED` exige aussi `- Signed-by:` et `- Reviewed-on: YYYY-MM-DD`.
Ce contrôle de structure ne vérifie pas l'authenticité d'une signature :
le reviewer doit lire la décision et refuser une attribution inventée.

```sh
node --test scripts/ci/check-men.test.mjs
bash scripts/ci/check-men.sh
```

Le validateur échoue sur catalogue absent, doublon, référence inconnue, statut
invalide, ticket absent du registre ou lien ne correspondant pas au ticket.
Il compare les liens au [registre Linear vérifié](../scripts/ci/men-linear-issues.tsv)
(clé, UUID, date de lecture, URL canonique), constitué par lectures réelles
`get_issue`. Chaque ligne porte sa date de lecture : 2026-10-05 pour le lot
initial, 2026-10-06 pour ANH-177. Aucun jeton Linear n'est requis en CI.
Ce snapshot atteste **l'existence au jour de lecture**, pas le statut actuel,
l'achèvement d'une mesure ou la présence d'un backlink dans le ticket.

À chaque modification des tickets cités, chaque jalon et avant le pilote :
relire chaque issue par son identifiant dans Linear, vérifier UUID/URL et contenu,
actualiser le registre avec la date réelle, conserver le résultat de lecture
dans les preuves de la PR et relancer les deux commandes. Un ticket supprimé,
fusionné ou une URL modifiée exige une décision de remappage ; ne jamais inventer
une ligne pour faire passer la gate. Le registre n'a pas d'expiration automatique
à l'horloge CI afin de garder les anciens commits reproductibles ; la revue
datée impose la fraîcheur avant livraison.

Chaque ticket de sécurité doit citer les MEN qu'il **contribue à fermer**,
avec mesures et preuves ; un statut Done du ticket ne ferme pas automatiquement
la menace. Les mappings exacts sont les champs Issues de chaque fiche.
Le 5 octobre 2026, le responsable de l'intégration a ajouté les backlinks
aux 27 tickets de traitement référencés ; leur texte et leur identité ont été
revérifiés par lecture de chaque ticket dans Linear. Cette traçabilité ne ferme
aucune menace. Une fermeture exige le résultat observé, le SHA, le déploiement
applicable et une revue.

## Procédure de revue aux jalons et avant pilote

1. À chaque jalon du [guide de roadmap](roadmap.md), avant toute release et avant
   [ANH-120](https://linear.app/anheart/issue/ANH-120/pilote-chez-un-premier-client-et-bilan-de-mise-en-production), le responsable relit le modèle contre le SHA candidat, les
   nouvelles frontières et les incidents. Il réévalue chaque OPEN et vérifie les
   tickets dans Linear ; les risques physiques/médicaux restent aux responsables
   concernés.
2. **Un reviewer indépendant** du rédacteur cherche les menaces oubliées
   (décision utilisateur du 5 octobre 2026 / EX-6 ANH-136), les hypothèses fragiles,
   les usages légitimes abusifs, l'offline, les retours arrière et les comptes
   privilégiés. Son identité, date, SHA, verdict et lien de rapport sont inscrits
   en tête après son avis réel. Une attribution d'agent est signalée comme telle.
3. La PR contient explicitement « Menaces ajoutées en revue » avec identifiants
   et scénario ; si aucune n'est ajoutée, le reviewer doit avoir confirmé ce
   résultat. La [revue du 5 octobre 2026](reviews/anh-136-2026-10-05.md) a conclu
   « Menaces ajoutées en revue indépendante : aucune ». MEN-14 à MEN-17 sont des
   ajouts de l'auteur. Le verdict de cette revue reste REJECT pour B1/EX-5 ;
   l'avis sur le nouveau candidat corrigé doit être obtenu sur son SHA exact.
4. Une acceptation nécessite une justification signée par la personne habilitée,
   un périmètre et une échéance/revue. Faute de cette preuve, garder OPEN et son
   ticket. Une signature technique d'agent ne vaut pas acceptation humaine.
5. La [check-list de release](release-threat-review.md) impose ces preuves.
   Le [processus de release](release.md) ([ANH-134](https://linear.app/anheart/issue/ANH-134/processus-de-release-versions-semantiques-pi-convex-site-changelog)) en fait une ligne de la check-list de la PR
   `develop` vers `main` ; cette page ne déclare aucune release faite ni
   autorisée.

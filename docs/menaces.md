# Modèle de menaces logiciel Anheart

| Date | Auteur / signature technique | Périmètre | État |
|---|---|---|---|
| 2026-10-05 | Codex, agent `/root/anh136_threat_model` — attribution technique de cette rédaction | Base source courante `4e6720b10a2aefb73f6e1c42e51b4434d00042f7`, ANH-136 | Auteur du modèle et des ajouts MEN-14 à MEN-17 ; actualisation après intégration du source ANH-121 |
| 2026-10-05 | Codex, agent `/root/anh136_aa01af2_gate` — reviewer technique indépendant | Commit `aa01af2030d9a38d67d1f6ec070f678018f096a7`, arbre `fd7883a4cddbac2f7dc76baedd4fb6799ec6f360` | REJECT, confiance HIGH : seul blocage B1, absence de cette inscription de revue (EX-5). Recherche d'omissions effectuée : aucune menace ajoutée. [Compte rendu daté](reviews/anh-136-2026-10-05.md) |
| 2026-10-08 | Agent développeur du ticket ANH-191 (attribution technique d'agent, aucune signature humaine) | Branche `develop` du 8 octobre 2026 et changements d'ANH-191 | Auteur de l'ajout MEN-18 (enregistrements de séance au repos sur le disque du Pi) ; aucune autre fiche modifiée. Relu le 8 octobre 2026 par un agent reviewer indépendant du rédacteur, sur le commit local `4b89bbd73568b25db1f3556810abf41cd859d412` qui a introduit cette fiche (avant rebase et publication) : APPROVE, aucun blocage. Ce verdict a été transmis par le coordinateur de la fusion ; le rapport n'est pas joint à ce dépôt et ne dit pas ici si des menaces oubliées ont été cherchées. Le SHA publié reste à relire selon la procédure ci-dessous |
| 2026-10-10 | Agent reviewer indépendant (revue technique d'agent, aucune signature humaine) | Commit `def21238818ecb05ef6dcc4b693651b4ec762b8f`, tête de la PR #63 de release pi-1.0.0, cloud-1.0.0, web-1.0.0 | REQUEST_CHANGES : énoncés à corriger (ANH-74 dans MEN-14, installation du Pi dans MEN-10, rappel ANH-82 dans MEN-11 et MEN-12). Menaces ajoutées en revue : MEN-19, MEN-20, MEN-21. Tickets Linear non relus dans cette revue. [Avis](https://github.com/anheartpro-byte/Anheart/pull/63#issuecomment-6098563933) |

Ce registre nomme le commit effectivement relu. Un verdict REJECT ou
REQUEST_CHANGES n'est pas une approbation du candidat corrigé : celui-ci exige
une nouvelle revue indépendante liée à son SHA exact dans le rapport/check de
PR. Inscrire ici le SHA du commit
qui contient sa propre inscription serait autoréférentiel. Le registre daté et
la validation du candidat courant restent tous deux obligatoires ; aucune
signature humaine ni acceptation de risque n'est revendiquée.

[Sommaire](README.md) · [Sécurité machine et limites](securite.md) · [Check-list de revue de release](release-threat-review.md)

Cette analyse STRIDE décrit le code de cette base, pas l'état vérifié d'une machine
ou d'un service en production. ANH-121 est intégré au source courant ; sa migration
des clés existantes et son déploiement réel restent non vérifiés et non réalisés
dans ce travail (ANH-82). ANH-74 est intégré au source courant (verrou entre
processus sur le câble du variateur) ; MEN-14 en donne la portée et la limite
sur un Pi installé par l'image. Trois revues sont inscrites ci-dessus. Celle du
5 octobre 2026 portait sur la base `68e4dcf1bda9217329b7d3976519f74c65eb24bd`,
avant ANH-121. Celle du 8 octobre 2026 portait sur le commit local qui a
introduit MEN-18. Celle du 10 octobre 2026 portait sur le commit
`def21238818ecb05ef6dcc4b693651b4ec762b8f` : elle a demandé les corrections
d'énoncés faites depuis et ajouté MEN-19, MEN-20 et MEN-21. Aucune ne vaut
approbation de cette actualisation, qui reste à relire sur son propre SHA.
Tous les risques
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
| Preuve d'une séance | Enregistrement complet et intact de ce qu'une séance a été ; une copie hors de la machine avant toute purge |
| Services hébergés | Disponibilité du site, du backend et de la démonstration de simulation ; quotas du compte d'hébergement et portée des jetons de déploiement |

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
| Contributeur du dépôt | Écrit dans un dépôt public (branche, PR) ; erreur incluse, compte compromis inclus |

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
- Le dépôt est public : ce qui y est versé, son historique et les journaux de
  sa CI sont lisibles par tous, et un commit publié ne se reprend pas.

## Frontières, dix flux et STRIDE par couche

S = usurpation, T = altération, R = répudiation, I = divulgation,
D = déni de service, E = élévation de privilège.
Les catégories décrivent les attaques, pas six cases obligatoirement distinctes
par endpoint. Les liens ci-dessous incluent les traversées entre couches.

| Flux | Frontière / données | STRIDE et menaces |
|---|---|---|
| F1 tablette ↔ Pi | Navigateur/LAN → API et WebSocket ; commandes, jeton, télémétrie, export d'un enregistrement | S/T/E MEN-02, MEN-05 ; R MEN-03 ; I/D MEN-05 ; I MEN-18 |
| F2 Pi ↔ variateur | Processus/port série → Modbus ; consignes et retours | S/T/D MEN-14 ; E MEN-04 ; R MEN-11 ; I : paramètres lisibles après accès local (MEN-04) |
| F3 BITalino → Pi | Bluetooth/capteur → traitement ; ECG et qualité | S/T/D MEN-15 ; I MEN-13 ; R MEN-11 ; E MEN-04 |
| F4 Pi ↔ Convex | Appareil → Internet/cloud ; Bearer, roster, AUTO, télémétrie | S/E MEN-01, MEN-02 ; T MEN-08, MEN-11, MEN-20 ; R MEN-03, MEN-11, MEN-20 ; I MEN-09, MEN-13 ; D MEN-12 |
| F5 site ↔ Convex | Navigateur non fiable → fonctions/données ; JWT et IDs | S/E MEN-03, MEN-08, MEN-17 ; T/R MEN-03 ; I MEN-09, MEN-13 ; D MEN-12 (ressources cloud partagées) |
| F6 site ↔ Clerk | Navigateur → identité/session → JWT Convex | S/E/I MEN-07, MEN-17 ; T/R MEN-03, MEN-07 ; D : identité indisponible, MEN-07 (accès distant), sans dépendance de l'arrêt local |
| F7 CI → Vercel / Convex | Code tiers/runner → artefact et autorité de déploiement | S/T/E MEN-06, MEN-16 ; R MEN-16 ; I MEN-13, MEN-16 ; D MEN-16 |
| F8 mise à jour → Pi | Aujourd'hui installation manuelle : sources copiées depuis un poste, image construite sur le Pi, service lancé au démarrage. Plus tard distribution/signature OTA → installation du logiciel de sécurité | S/T/E MEN-10 ; R MEN-10, MEN-16 ; I MEN-04 ; D MEN-10 |
| F9 console → poste → dépôt public | Export d'un enregistrement → poste de travail → bibliothèque de rejeu du dépôt → copie de travail, journaux et artefacts de la CI → build de la simulation hébergée ; séance enregistrée | I MEN-19 |
| F10 Internet → simulation hébergée | Client quelconque, sans identité → fonction hébergée ; nom de scénario et vitesse | D MEN-21 ; S/T/E MEN-16 (jeton de déploiement) |

Couche Pi : MEN-02, MEN-04, MEN-05, MEN-10, MEN-11, MEN-14, MEN-15, MEN-18,
MEN-20 (MEN-18 et, pour sa part locale, MEN-20 portent sur le disque du Pi :
pas un flux, d'où leur absence des autres lignes du tableau).
Couche Convex : MEN-01, MEN-03, MEN-08, MEN-09, MEN-12, MEN-13, MEN-17, MEN-20.
Couche site/Clerk : MEN-03, MEN-07, MEN-08, MEN-09, MEN-17.
Couche build/livraison : MEN-06, MEN-10, MEN-13, MEN-16, MEN-19, MEN-21 (la
simulation hébergée y est rangée : elle ne parle à aucune machine).

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

**Existant vérifié dans le source.** [`canAccessUser`, `canAccessMachine`, `requireRole`](../convex/lib/auth.ts) et [`setUserPhysiology`](../convex/training.ts) imposent liens gestionnaire/patient et bornes de valeurs. Depuis le premier lot d'[ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site), ces règles répondent pour une seule organisation : l'organisation et le rôle de l'appel viennent des revendications du jeton vérifié, et un gestionnaire ou un admin d'organisation n'atteint ni machine, ni patient, ni séance d'une autre organisation ; [`authorization.matrix.ts`](../convex/authorization.matrix.ts) et [`organizationIsolation.test.ts`](../convex/organizationIsolation.test.ts) le vérifient pour chaque fonction publique, y compris par identifiant direct. À l'intérieur d'une organisation, une valeur plausible peut rester malveillante. L'admin de l'organisation Anheart garde des droits sur toutes les organisations ; cette organisation est désignée par un réglage hébergé du déploiement (MEN-16).

**Manquant.** Le cloisonnement d'[ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site) existe dans le source intégré ; son effet sur le service rendu dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en), de la migration et de la configuration de Clerk Organizations, et ses lots suivants (miroir par webhook, site) restent à livrer ; [ANH-167](https://linear.app/anheart/issue/ANH-167/convex-journal-daudit-immuable-des-actions-sensibles-consultable-et) trace les actions sensibles ; [ANH-144](https://linear.app/anheart/issue/ANH-144/identite-et-droits-reverifies-a-larmement-passager-affiche-et-confirme) revérifie le passager et les droits à l'armement. Ces mesures réduisent l'abus sans établir une validation médicale.

**Preuve de fermeture attendue.** Tests croisés entre organisations, gestionnaire non lié refusé, modification attribuée et révocation avant armement effective.

## MEN-04 Pi compromis : logiciel de sécurité, paramètres et profils

- Status: OPEN
- Issues: [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu), [ANH-139](https://linear.app/anheart/issue/ANH-139/parametres-medicaux-signes-fichier-versionne-verification-au-demarrage), [ANH-138](https://linear.app/anheart/issue/ANH-138/publication-des-programmes-vers-les-machines-le-pi-recupere-revalide)

F1, F2, F3, F4, F8 · Pi · T, E · Critique, personne directement.

**Scénario.** Un accès privilégié ou un paquet compromis modifie le superviseur, les limites, les programmes ou leurs clés de vérification.

**Existant vérifié dans le source.** [`local_config._flag`](../raspberry-pi/src/local_config.py) met `PROGRAMS_ENABLED` et `OCCUPANCY_OCCUPIED_ENABLED` à faux par défaut ; [`SafetySupervisor.require_estop_confirmed`](../raspberry-pi/src/training/safety.py) bloque sans attestation et [`_prepare_programme`](../raspberry-pi/src/local_panel.py) revalide le profil. Ces barrières exécutées sur le même Pi ne résistent pas à un root compromis ; aucune preuve de démarrage mesuré n'est revendiquée. Sur un Pi installé par l'image, la console tourne encore sous un compte privilégié, dans un conteneur privilégié ([pi-image.md](pi-image.md#sous-quel-compte)) ; l'en sortir relève d'ANH-151.

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

**Existant vérifié dans le source.** [`ci.yml`, jobs audit/web/python](../.github/workflows/ci.yml) utilise `npm ci`, npm audit, pip-audit, actions épinglées par SHA et tests ; [`package-lock.json`](../package-lock.json) fige le graphe npm. Le job Python installe des requirements sans exiger leurs hashes ; les hashes générés pour l'audit ne sécurisent pas rétroactivement l'installation. L'image de la console d'un Pi installé part, elle, d'une image de base figée par son empreinte et n'installe que les paquets Python de ses fichiers de verrouillage, empreintes exigées ([`Dockerfile`](../raspberry-pi/Dockerfile), [pi-image.md](pi-image.md#2-versions-figées)) ; les paquets du système n'y sont figés que par leur série. Un audit de vulnérabilités ne détecte pas tout code malveillant.

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

**Existant vérifié dans le source.** [`requireAuth` / `canAccessUser`](../convex/lib/auth.ts) existent mais doivent être appelés par chaque endpoint. [`getSessionTelemetry`](../convex/training.ts) vérifie passager ou machine. Depuis le premier lot d'[ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site), chaque fonction publique répond dans l'organisation de l'appelant ; la matrice appelle chacune depuis une autre organisation, et [`completeness.test.ts`](../convex/completeness.test.ts) échoue si une fonction n'a pas ce cas ou si une fonction qui prend un identifiant n'est pas appelée avec celui d'une autre organisation. Cinq routes de [`http.ts`](../convex/http.ts) lisent ou modifient une séance désignée par son identifiant : `/training/start`, `/training/end`, `/training/status`, `/training/telemetry` et `/training/events`. Chacune transmet la machine authentifiée à sa fonction interne (`markTrainingStarted`, `endTrainingSession`, `getTrainingStatus`, `storeTelemetry`, `storeEvents` dans [`training.ts`](../convex/training.ts)), qui vérifie d'abord que la séance appartient à cette machine. Pour ces cinq routes, une séance d'une autre machine reçoit exactement la réponse d'une séance inconnue (statut, en-têtes et corps), quel que soit l'état de la séance, et ni la séance, ni sa machine, ni les mesures enregistrées ne changent ; [`httpRoutes.test.ts`](../convex/httpRoutes.test.ts) compare les deux réponses pour chaque route ([ANH-177](https://linear.app/anheart/issue/ANH-177/convex-renforcer-deux-controles-dautorisation-men-08-men-17)). Voir aussi MEN-17.

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

**Scénario.** Un attaquant remplace un artefact, son manifeste ou sa clé, ou réinstalle une ancienne version pourtant signée qui retire une protection ; une mise à jour pendant une séance perturbe le contrôle. Tant que l'installation est manuelle, la même classe de risque porte sur les sources copiées sur le Pi et sur l'image qui y est construite.

**Existant vérifié dans le source.** [`ci.yml`](../.github/workflows/ci.yml) exécute des gates, et la pipeline de release ([`release.yml`](../.github/workflows/release.yml)) pose le tag `pi-X.Y.Z` avec ceux des deux autres composants ; son étape « machines » ne touche à aucune machine : aucune mise à jour à distance n'existe. La seule voie de mise à jour d'un Pi est manuelle ([pi-image.md](pi-image.md#3-ce-que-fait-scriptsinstallsh), [deploiement.md](deploiement.md#76-arrêter-mettre-à-jour)) : les sources sont copiées sur le Pi depuis un poste, [`install.sh`](../raspberry-pi/scripts/install.sh) y construit l'image, et [`anheart.service`](../raspberry-pi/scripts/anheart.service) la lance au démarrage. Ce que cette voie garantit : l'image de base est figée par son empreinte et les paquets Python ne s'installent qu'avec les empreintes de leurs fichiers de verrouillage ([`Dockerfile`](../raspberry-pi/Dockerfile)) ; le service ne tire jamais une image d'un registre ; le script refuse d'agir tant qu'une console en marche ne se dit pas au repos, y compris quand elle ne répond pas ; une console qui démarre est au repos et ne reprend aucune séance. Ce qu'elle ne relie pas : la version affichée est le contenu de `raspberry-pi/VERSION` au moment de la construction, et rien ne vérifie que les sources copiées sont celles du commit tagué ; l'image n'est ni publiée ni signée. Le registre des versions ([`softwareReleases.ts`](../convex/softwareReleases.ts)), lu et écrit par le seul admin Anheart, garde pour chaque version du Pi son niveau de validation et chaque changement de ce niveau, avec son auteur, sa date et son motif ; un niveau n'y vaut que ce que vaut le compte qui l'écrit (MEN-07). La règle « une machine ne reçoit qu'une version validée pour son état » est codée et testée ([`releaseAllowedOnMachine`](../convex/lib/releaseValidation.ts), [release.md](release.md#la-règle--quelle-machine-reçoit-quelle-version)), mais rien ne l'applique encore, et ce registre existe dans le source intégré : son effet sur le service rendu dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en). Il n'existe ni vérificateur de signature, ni règle anti-rétrogradation. L'attestation E-STOP au démarrage n'authentifie pas un binaire.

**Manquant.** [ANH-168](https://linear.app/anheart/issue/ANH-168/ota-paquets-de-version-signes-par-la-ci-verification-de-signature-sur) signature et niveau de validation ; [ANH-170](https://linear.app/anheart/issue/ANH-170/ota-jamais-pendant-une-seance-installation-seulement-machine-au-repos) installation uniquement au repos avec auto-test ; [ANH-171](https://linear.app/anheart/issue/ANH-171/ota-essai-de-retour-arriere-volontaire-et-procedure-documentee) retour arrière essayé et documenté. La règle anti-rétrogradation de sécurité et la rotation/révocation des clés de signature doivent être précisées lors de ces travaux : une signature valide ne suffit pas. D'ici là, pour la voie manuelle, relier la version affichée au commit tagué et publier une image signée restent à faire ([pi-image.md](pi-image.md#8-limites-et-reste-à-faire)) ; l'application de la règle du registre des versions attend le registre machine et la mise à jour à distance.

**Preuve de fermeture attendue.** Artefact altéré/autre clé refusé ; version de niveau inférieur refusée ; installation en séance refusée ; rollback conserve les contraintes de sécurité.

## MEN-11 Horloge du Pi manipulée

- Status: OPEN
- Issues: [ANH-163](https://linear.app/anheart/issue/ANH-163/pi-horloge-fiable-ntp-ou-rtc-journaux-persistants-et-bornes), [ANH-142](https://linear.app/anheart/issue/ANH-142/controles-pre-vol-automatiques-avant-chaque-seance-variateur-bitalino), [ANH-129](https://linear.app/anheart/issue/ANH-129/synchronisation-cloud-par-relecture-du-journal-local-avec-reprise)

F3, F4 · Pi/Convex · T, R, D · Haute, personne via pré-vol ; intégrité des preuves.

**Scénario.** Un changement d'heure fausse l'âge, les dates de profils, les traces ou l'acceptation de télémétrie ; perte réseau au démarrage rend l'heure non fiable.

**Existant vérifié dans le source.** [`RealClock.monotonic` et `unix_millis`](../raspberry-pi/src/clock.py) séparent durées de sécurité et horodatage ; [`SafetySupervisor`](../raspberry-pi/src/training/safety.py) utilise le monotone. La borne que le serveur appliquait à l'heure des lots ECG a disparu avec la route de l'ancien mode d'enregistrement. La console dit au serveur l'âge de chaque séance, compté sur son horloge monotone, et date ses points comme le début plus le temps écoulé ([`record_uplink.py`](../raspberry-pi/src/record_uplink.py), [`upload.py`](../raspberry-pi/src/record/upload.py)) : Convex place alors début, fin et date de mesure sur sa propre horloge, n'écrit un point ou un événement que s'il est daté dans sa séance sur l'axe de la machine (`machineAxis`, `sessionWindow` dans [`training.ts`](../convex/training.ts), [`journalSync.test.ts`](../convex/journalSync.test.ts)), et une heure murale fausse ou corrigée en cours de séance ne change ni ce qui est stocké ni ce qui est montré comme actuel ([`test_cloud_journal_e2e.py`](../raspberry-pi/tests/test_cloud_journal_e2e.py)). Ce traitement par Convex existe dans le source intégré ; son effet sur le service rendu dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en). Reste, pour cette console, l'exactitude de l'âge : le délai d'acheminement de la déclaration, et un âge faux, dont ce qui le borne est décrit dans [convex.md](convex.md#deux-horloges). Pour une console d'une version antérieure, qui ne dit pas cet âge, l'heure de la télémétrie reste celle du Pi : stockée et servie telle quelle, sans borne ; une mesure envoyée en retard n'est pas montrée comme actuelle si l'horloge du Pi est juste ou en retard, mais une horloge en avance de X peut faire lire une mesure envoyée en retard comme actuelle jusqu'à 20 s + X après elle. Après un redémarrage du système, la console ne sait plus compter l'âge d'une séance d'avant : elle laisse alors la date qu'elle avait écrite, ou, si cette date est antérieure à 2024, annonce l'âge le plus petit possible. [`_prepare_programme`](../raspberry-pi/src/local_panel.py) transmet l'heure murale à la résolution du profil : toute décision n'est donc pas indépendante de l'heure.

**Manquant.** [ANH-163](https://linear.app/anheart/issue/ANH-163/pi-horloge-fiable-ntp-ou-rtc-journaux-persistants-et-bornes) heure fiable et indication de confiance : la date qu'une console écrit reste sans borne dans les deux cas où le serveur ne la remplace pas, une séance envoyée après un redémarrage du système (la console ne sait plus compter son âge), et toute séance d'une console d'une version antérieure, qui ne dit pas cet âge ; [ANH-142](https://linear.app/anheart/issue/ANH-142/controles-pre-vol-automatiques-avant-chaque-seance-variateur-bitalino) pré-vol bloque un état d'horloge non fiable avant personne à bord. Un système privilégié compromis reste couvert par MEN-04.

**Preuve de fermeture attendue.** Horloge murale reculée/avancée sans raccourcir les délais monotones ; démarrage hors réseau signale heure non fiable et bloque le pré-vol occupé.

## MEN-12 Déni de service des routes machine

- Status: OPEN
- Issues: [ANH-166](https://linear.app/anheart/issue/ANH-166/convex-limitation-de-debit-par-machine-et-par-utilisateur-taille)

F4 · Convex · D · Haute, disponibilité et preuve ; pas de mouvement direct.

**Scénario.** Appels anonymes ou clé valide compromise saturent l'authentification, le parsing JSON, le stockage ou les quotas cloud ; les demandes de stop distantes deviennent indisponibles.

**Existant vérifié dans le source.** [`machineRoute` et route `/training/telemetry`](../convex/http.ts) authentifient et limitent à 600 points après parsing ; la route `/training/events` limite à 200 événements et 2000 caractères de texte par événement, après parsing aussi ; un lot envoyé de nouveau n'écrit rien de plus (`storeTelemetry`, `storeEvents`) ; la console espace d'elle-même ce qu'elle renvoie (un lot de 300 points au plus toutes les 2 s, rien pendant 15 s après une réponse retenue, [`record_uplink.py`](../raspberry-pi/src/record_uplink.py)) ; [`CloudSync.step`](../raspberry-pi/src/cloud_sync.py) sépare la synchronisation de l'autorité locale. Les bornes de ces deux routes et l'écriture unique d'un lot renvoyé existent dans le source intégré ; leur effet sur le service rendu dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en). Il n'y a pas de limite de débit applicative ni de refus de taille avant parsing à créditer. La perte du cloud ne remplace jamais l'arrêt local.

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

**Existant vérifié dans le source.** [`ATV320Drive.open`](../raspberry-pi/src/motor/atv320.py) valide l'adressage par lecture ; le backend sérialise ses appels dans le processus. Le verrou entre processus d'[ANH-74](https://linear.app/anheart/issue/ANH-74/verrou-unique-sur-le-cable-variateur-console-bench-console) est intégré : [`DriveLease`](../raspberry-pi/src/motor/drive_process_lock.py) prend, sans attendre, un verrou du noyau sur un fichier de la machine, et les deux transports ([`atv320.py`](../raspberry-pi/src/motor/atv320.py), [`ftdi_link.py`](../raspberry-pi/src/motor/ftdi_link.py)) ne s'ouvrent qu'avec lui. Un second programme est refusé avant toute ouverture du transport, le texte laissé dans le fichier ne permet jamais de reprendre le verrou, et celui-ci n'est rendu qu'après une fermeture réussie du transport ou la fin du processus ([`test_drive_process_lock.py`](../raspberry-pi/tests/test_drive_process_lock.py)). Sa portée s'arrête aux programmes qui voient ce fichier : sur un Pi installé par l'image, c'est un fichier du conteneur de la console, et la règle « aucun outil de banc ou de diagnostic tant que le service tourne » est donnée à la personne ([pi-image.md](pi-image.md#8-limites-et-reste-à-faire)). [`SafetySupervisor._rule_setpoint_unconfirmed`](../raspberry-pi/src/training/safety.py) surveille les retours. Ce verrou n'engage que les programmes qui le demandent ; ce n'est pas une authentification Modbus, et un retour forgé peut tromper le contrôle.

**Manquant.** [ANH-74](https://linear.app/anheart/issue/ANH-74/verrou-unique-sur-le-cable-variateur-console-bench-console) a réservé le transport entre processus coopérants ; sa suite, un verrou partagé entre le Pi et le conteneur ou des outils lancés dans le conteneur, reste à concevoir, et c'est la condition écrite avant de relier au vrai variateur une console lancée par le service ([pi-image.md](pi-image.md#8-limites-et-reste-à-faire)). [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu) restreint l'accès au périphérique. Verrou logiciel et permissions ne couvrent pas une injection physique/root, à reprendre dans ANH-103/104.

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

**Existant vérifié dans le source.** [`ci.yml`](../.github/workflows/ci.yml) déclare `contents: read`, actions par SHA et `persist-credentials: false`; les gates auditées ne constituent pas un déploiement Vercel/Convex authentifié. [`codeql.yml`](../.github/workflows/codeql.yml) fait aussi lire les workflows GitHub Actions du dépôt par l'analyse statique ([Analyse statique externe](framework-de-test.md#analyse-statique-externe--codeql-anh-196)), sans en faire une gate. [`vercel.json`](../vercel.json) coupe les déploiements Vercel déclenchés par un push (constaté sur une branche de travail, pas encore sur `main`), et deux workflows lancés à la main ([`deploy-production.yml`](../.github/workflows/deploy-production.yml), [`deploy-preview.yml`](../.github/workflows/deploy-preview.yml)) déploient le site et la simulation, chacun depuis sa seule branche, par un environnement GitHub, sans permission d'écriture, avec un jeton Vercel lu comme secret et remis aux seules étapes qui appellent Vercel ([framework-de-test.md](framework-de-test.md#déploiement-vercel-par-bouton-anh-198)). Ces deux workflows ne vérifient pas que les gates du commit déployé sont vertes. Un troisième, la pipeline de release ([`release.yml`](../.github/workflows/release.yml)), elle aussi lancée à la main et sur `main` seulement, relie la livraison aux contrôles : elle refait tourner toutes les gates et CodeQL sur le commit exact, s'arrête à la première alerte ouverte, pose les tags par `scripts/release.sh`, puis déploie Convex et, par les jobs du bouton de production, le site et la simulation. Un seul de ses jobs peut écrire dans le dépôt (les tags), chacune de ses écritures passe par l'environnement GitHub `production`, la clé de déploiement de Convex est comparée au déploiement attendu avant usage, et une exécution à blanc, cochée par défaut, n'a ni secret ni droit d'écriture ([framework-de-test.md](framework-de-test.md#pipeline-de-release-anh-219)). Elle n'a jamais tourné. Environnements GitHub, approbation, secrets, protections des branches, variables hébergées et paramètres SaaS sont des réglages hors du dépôt : ils n'ont pas été inspectés ici. L'une de ces variables hébergées fait autorité sur les droits : `ANHEART_ORG_ID`, sur le déploiement Convex, désigne l'organisation dont les admins sont admins Anheart ([`anheartClerkOrganizationId`, `isAnheartOrganization`](../convex/lib/auth.ts), [convex.md](convex.md#activer-le-multi-organisation)). La protection de cette désignation est donc celle des droits d'écriture sur les variables de ce déploiement, hors du dépôt ; ce modèle ne la couvre que par cette fiche, de façon générale, et son effet sur le service rendu dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en). [`scripts/release.sh`](../scripts/release.sh) refuse de préparer une release, d'ouvrir la PR `develop` vers `main` ou de poser un tag quand une vérification CI du commit visé n'est pas terminée et réussie, et chaque composant porte une version lisible ([release.md](release.md)) ; ce contrôle tourne sur le poste du responsable de release, ou dans la pipeline de release, qui déploie le commit qu'elle vient de taguer. Rien ne relie encore l'empreinte de l'artefact servi au commit tagué.

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

## MEN-18 Enregistrements de séance non chiffrés sur le disque du Pi

- Status: OPEN
- Issues: [ANH-172](https://linear.app/anheart/issue/ANH-172/donnees-de-sante-decision-avis-ecrit-juriste-ou-dpo-sur-convex-clerk), [ANH-130](https://linear.app/anheart/issue/ANH-130/depot-des-enregistrements-de-seance-dans-convex-storage-organise-par), [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu), [ANH-191](https://linear.app/anheart/issue/ANH-191/boite-noire-suites-danh-128-inodes-trames-variateur-journal-hors)

F1 (export) et stockage local · Pi · I · Haute, confidentialité santé ; personne indirectement.

**Scénario.** Le support de stockage du Pi sort de la machine (perte, vol, retour en maintenance, réemploi), ou une personne obtient un accès privilégié au système : les enregistrements de séance qui s'y trouvent sont lus. Ils contiennent l'ECG brut et les autres voies du capteur, la fréquence cardiaque, le déroulé de chaque séance, l'identifiant du passager transmis par le tableau de bord et un pseudonyme stable de l'opérateur.

**Existant vérifié dans le source.** [`prepare_root`](../raspberry-pi/src/record/journal.py) réserve le dossier des enregistrements au compte de la console (mode 700). [`Writer.create`, `create_private` et `write_file`](../raspberry-pi/src/record/writer.py) créent chaque dossier de séance en 700 et chaque fichier en 600, dès l'appel qui le crée ; le journal hors séance ([`Logbook`](../raspberry-pi/src/record/logbook.py)) et le marqueur de dépôt ([`confirm_deposit`](../raspberry-pi/src/record/retention.py)) suivent la même règle ([ANH-191](https://linear.app/anheart/issue/ANH-191/boite-noire-suites-danh-128-inodes-trames-variateur-journal-hors)). Un enregistrement ne porte ni nom de passager, ni nom d'opérateur ([`operator_alias`, `redact`](../raspberry-pi/src/record/session.py)), ni adresse e-mail, ni clé de machine. L'export passe par la console, derrière son jeton, machine au repos ([`_register_records`](../raspberry-pi/src/web/routes.py)). Ces droits de fichiers ne valent que sur la machine en marche et contre un compte sans privilèges : **les enregistrements ne sont pas chiffrés au repos**, et une archive exportée ne l'est pas non plus. Les enregistrements créés avant ce changement gardent leurs modes d'origine sous le dossier en 700. Aucun enregistrement n'est purgé sans dépôt confirmé hors de la machine, et ce dépôt n'existe pas encore : ils s'accumulent sur le disque. L'alias de l'opérateur est un pseudonyme, pas un anonymat. Sur un Pi installé, la console écrit encore sous un compte privilégié ([pi-image.md](pi-image.md#sous-quel-compte)). Rien de ceci n'a été vérifié sur une machine réelle.

**Manquant.** [ANH-172](https://linear.app/anheart/issue/ANH-172/donnees-de-sante-decision-avis-ecrit-juriste-ou-dpo-sur-convex-clerk) doit dire ce qui est exigé du disque du Pi (chiffrement au repos ou purge après dépôt) et les durées de conservation. [ANH-130](https://linear.app/anheart/issue/ANH-130/depot-des-enregistrements-de-seance-dans-convex-storage-organise-par) apporte le dépôt hors de la machine, condition de la purge locale déjà écrite. [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu) durcit l'accès au système et fait tourner la console sans privilèges. La forme de l'alias de l'opérateur est une décision ouverte du chef de projet, inscrite dans [ANH-191](https://linear.app/anheart/issue/ANH-191/boite-noire-suites-danh-128-inodes-trames-variateur-journal-hors). Le contrôle retenu (chiffrement, purge, ou les deux) n'est pas encore choisi : aucun n'est annoncé ici comme fait.

**Preuve de fermeture attendue.** Avis ANH-172 joint et appliqué ; sur une machine d'essai, selon le contrôle retenu, des enregistrements synthétiques illisibles une fois le support sorti de la machine, ou absents du disque au terme de la durée de conservation après un dépôt confirmé ; droits des dossiers et fichiers constatés sur un Pi installé ; décision écrite sur l'alias de l'opérateur.

## MEN-19 Séance réelle versée dans la bibliothèque de rejeu d'un dépôt public

- Status: OPEN
- Issues: [ANH-172](https://linear.app/anheart/issue/ANH-172/donnees-de-sante-decision-avis-ecrit-juriste-ou-dpo-sur-convex-clerk)

F9 · Livraison (chaîne de développement) · I · Haute, confidentialité santé ; personne indirectement, sans mouvement.

**Scénario.** Un admin Anheart ou un contributeur verse par erreur l'archive d'une séance réelle dans la bibliothèque de rejeu `simulation/scenarios/real/`. Le dépôt est public : l'archive devient lisible par tous, dans le dépôt et son historique, dans la copie de travail de chaque exécution de la CI et dans le build de la simulation hébergée. Une donnée de santé quitte ainsi la machine par la chaîne de développement, hors de tout contrôle d'accès du cloud, et un commit publié ne se reprend pas.

**Existant vérifié dans le source.** [`judge`](../simulation/real_records.py) juge chaque archive présente dans la bibliothèque, à chaque exécution de la gate de simulation ([`test_real_records.py`](../simulation/tests/test_real_records.py)) : enregistrement fermé et intact, manifeste sans `subject_id` ni `session_id`, machine, organisation, opérateur et acteur de chaque événement sous l'une de deux formes fermées (`identifying`), identifiants de l'enregistrement remplacés par ceux de la bibliothèque ([framework-de-test.md, 16.5](framework-de-test.md#165-la-bibliothèque-simulationscenariosreal)). Une archive qui ne tient pas ces exigences fait échouer la gate et ne peut pas être fusionnée. Les trois archives versionnées à ce jour ont été produites par la simulation : leur manifeste porte `machine_id` `simulation` et aucun sujet, et la commande qui les exporte n'accepte que le nom d'un scénario de la batterie. Le rapport d'un rejeu, qui part dans les journaux et les artefacts de la CI, ne porte aucun texte libre de l'enregistrement : des nombres de décision, des valeurs d'énumération et des noms de règles vérifiés ([`replay_report.py`](../simulation/replay_report.py)). L'export d'un enregistrement passe par la console, derrière son jeton, machine au repos (MEN-18). La marche à suivre pour une séance réelle est écrite comme pas encore possible de bout en bout ([16.6](framework-de-test.md#166-ajouter-un-enregistrement)). [`build.sh`](../deploy/simulation-vercel/build.sh) copie tout `simulation/scenarios`, bibliothèque comprise, dans le build de la simulation hébergée. Ce jugement lit des champs nommés, pas le signal : sous la forme « séance réelle », **l'ECG brut et la date de la séance restent dans l'archive**. Et il s'exerce à la gate, donc après la publication d'une branche.

**Manquant.** [ANH-172](https://linear.app/anheart/issue/ANH-172/donnees-de-sante-decision-avis-ecrit-juriste-ou-dpo-sur-convex-clerk) doit dire si une séance réelle peut entrer dans un dépôt public, sous quelle forme et avec quel accord de la personne : il n'existe ni décision, ni règle de consentement. L'outil d'anonymisation n'existe pas, et ce qu'il doit faire de l'ECG brut et de la date n'est pas décidé. Restent aussi à décider : un contrôle avant publication, le lieu de la bibliothèque (ce dépôt, ou un stockage à accès restreint lu par la gate), ce que le build hébergé en copie, et la conduite à tenir si une archive réelle a été publiée. Aucun de ces contrôles n'est annoncé ici comme fait.

**Preuve de fermeture attendue.** Avis ANH-172 joint et appliqué (décision, forme admise, consentement) ; archive synthétique sous une forme non admise refusée avant publication et à la gate ; contenu du build hébergé constaté conforme à la décision ; conduite en cas de publication par erreur écrite et essayée sur une archive synthétique.

## MEN-20 Enregistrement de séance altéré, supprimé ou jamais déposé, et disque dont dépend un départ

- Status: OPEN
- Issues: [ANH-130](https://linear.app/anheart/issue/ANH-130/depot-des-enregistrements-de-seance-dans-convex-storage-organise-par), [ANH-163](https://linear.app/anheart/issue/ANH-163/pi-horloge-fiable-ntp-ou-rtc-journaux-persistants-et-bornes), [ANH-191](https://linear.app/anheart/issue/ANH-191/boite-noire-suites-danh-128-inodes-trames-variateur-journal-hors), [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu)

Stockage local et F4 · Pi/Convex · T, R, D · Haute, intégrité de la preuve et disponibilité de la machine ; personne indirectement, sans mouvement.

**Scénario.** Avant qu'une copie complète ait quitté la machine, l'enregistrement d'une séance est modifié, supprimé ou perdu avec son support, du fait d'un compte local privilégié, d'un Pi compromis ou d'une panne du stockage : ce que la séance a été ne peut plus être établi, ou l'est à tort. Sans aucun acteur, les enregistrements s'accumulent faute de dépôt, jusqu'au seuil sous lequel la console refuse tout départ.

**Existant vérifié dans le source.** [`Writer.close`](../raspberry-pi/src/record/writer.py) écrit à la clôture `checksums.sha256`, une empreinte SHA-256 par fichier ; un enregistrement clos n'accepte plus d'ajout, un bloc n'est jamais remplacé, et le lecteur signale tout fichier manquant, tronqué ou différent de son empreinte ([enregistrement.md](enregistrement.md#fermeture-intégrité-et-lecture-interrompue)) ; dossiers et fichiers sont privés (MEN-18). [`purge`](../raspberry-pi/src/record/retention.py) ne retire un enregistrement qu'après un dépôt confirmé hors de la machine, lié à l'empreinte de ses sommes de contrôle, puis le délai de conservation ; rien n'écrit encore cette confirmation. Un départ est refusé, variateur non touché, sous 500 Mo ou 36 016 inodes libres sous le dossier des enregistrements, et quand la mesure est illisible ou trop ancienne ([`storage_gate`](../raspberry-pi/src/record/session.py), seuils dans [`journal.py`](../raspberry-pi/src/record/journal.py), [raspberry-pi.md, 15.4](raspberry-pi.md#154-départ-refusé-sous-500-mo)). Le journal hors séance est borné sur disque ([`Logbook`](../raspberry-pi/src/record/logbook.py), [ANH-191](https://linear.app/anheart/issue/ANH-191/boite-noire-suites-danh-128-inodes-trames-variateur-journal-hors)). Une écriture refusée en cours de séance n'arrête ni la séance ni la sécurité : elle est comptée, dite à l'opérateur et annoncée dans le battement de cœur au tableau de bord, qui ne l'affiche pas encore. Côté Convex, un point ou un événement n'est écrit qu'une fois, et un lot renvoyé ne change rien à ce qui est stocké (`storeTelemetry`, `storeEvents` dans [`training.ts`](../convex/training.ts)) ; cet effet dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en). Ces sommes de contrôle ne sont pas authentifiées : elles montrent une altération accidentelle, pas une réécriture par un compte qui peut écrire le dossier. **Le Pi détient la seule copie complète** : Convex ne reçoit que la télémétrie à 1 Hz et les événements ([`upload.py`](../raspberry-pi/src/record/upload.py)), ni l'ECG brut, ni les trames du variateur. Rien de ceci n'a été constaté sur un Pi installé.

**Manquant.** [ANH-130](https://linear.app/anheart/issue/ANH-130/depot-des-enregistrements-de-seance-dans-convex-storage-organise-par) apporte le dépôt hors de la machine, condition de la purge déjà écrite, et la vérification des fichiers déposés : d'ici là le disque ne se libère que par une intervention, et l'accumulation finit en départs refusés. Comment authentifier les empreintes (clé hors de portée du compte de la console, ou empreinte déposée hors de la machine dès la clôture) reste à décider. [ANH-163](https://linear.app/anheart/issue/ANH-163/pi-horloge-fiable-ntp-ou-rtc-journaux-persistants-et-bornes) apporte l'heure fiable et les journaux persistants bornés, dont dépend la date que porte la preuve (MEN-11). [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu) durcit l'accès au système et fait tourner la console sans privilèges. Aucune alerte de flotte ne prévient qu'un disque se remplit ou qu'un enregistrement est dégradé ; le ticket qui la portera reste à désigner à la relecture des tickets dans Linear. Aucun de ces contrôles n'est annoncé ici comme fait.

**Preuve de fermeture attendue.** Sur une machine d'essai, avec des enregistrements synthétiques : un enregistrement modifié après sa clôture, sommes de contrôle recalculées comprises, reconnu comme tel au dépôt ; un enregistrement déposé puis purgé retrouvé complet hors de la machine ; aucune purge sans dépôt confirmé ; un disque rempli produit le refus de départ attendu, et l'équipe en est prévenue avant que l'opérateur ne le rencontre.

## MEN-21 Service de simulation hébergé ouvert à Internet

- Status: OPEN
- Issues: [ANH-126](https://linear.app/anheart/issue/ANH-126/cicd-deploiement-vercel-et-convex-automatise-rapports-du-framework-de), [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite)

F10 · Livraison (simulation hébergée) · D · Haute seulement si la perte de service est importante, c'est-à-dire si elle atteint aussi le site : cela suppose des quotas communs sur le compte d'hébergement, ce que le dépôt ne permet pas de vérifier ; sinon en deçà de l'échelle de ce modèle. Aucune machine, aucun mouvement, aucune donnée de santé lue ; personne non concernée.

**Scénario.** La simulation hébergée est un service public, sans identité, qui calcule à la demande. La classe de risque est celle de tout service ouvert qui partage ses moyens : sa consommation s'impute à un compte d'hébergement, et sa livraison passe par un jeton de déploiement. Si ce compte, ses quotas ou ce jeton sont communs au site, l'indisponibilité ou la compromission de l'un atteint l'autre.

**Existant vérifié dans le source.** [`app.py`](../deploy/simulation-vercel/app.py) n'accepte un scénario que par son nom, dans le catalogue fermé des scénarios livrés (`CATALOGUE`, `_refusal`) : aucun fichier n'est lu d'après un nom reçu. La vitesse demandée est ramenée dans un intervalle fermé (`_clamp_speed`), et les scénarios en mode `dsp` n'y tournent pas. Le processus n'ouvre ni port Modbus, ni capteur, ni lien vers Convex : tout ce qu'un scénario touche est un modèle, et la bibliothèque de rejeu que son build copie n'y est pas lue (MEN-19). Dans [`deploy-production.yml`](../.github/workflows/deploy-production.yml) et [`deploy-preview.yml`](../.github/workflows/deploy-preview.yml), le site et la simulation sont deux projets d'une même équipe d'hébergement, déployés avec le même jeton ([deploiement.md, 5.5](deploiement.md#55-réglages-à-faire-une-fois-à-la-main)). Les quotas, la facturation et les protections du compte d'hébergement sont des réglages hors du dépôt : ils n'ont pas été inspectés.

**Manquant.** À décider : la séparation entre le site et la simulation, pour les jetons de déploiement (un jeton par projet, ou un jeton commun assumé par écrit) et pour les quotas (comptes ou équipes distincts, ou plafonds par projet). [ANH-126](https://linear.app/anheart/issue/ANH-126/cicd-deploiement-vercel-et-convex-automatise-rapports-du-framework-de) porte la livraison et ses droits ; [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite) l'inventaire et la rotation des secrets. Le partage réel des quotas se constate dans les réglages du compte d'hébergement, pas dans le dépôt. Aucune séparation n'est annoncée ici comme faite.

**Preuve de fermeture attendue.** Décision écrite sur la séparation ; portée de chaque jeton et quotas ou plafonds de chaque projet constatés dans les réglages par une personne habilitée ; inventaire des secrets à jour ; simulation rendue indisponible sur un environnement d'essai sans effet constaté sur le site.

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
initial, 2026-10-06 pour ANH-177, 2026-10-08 pour ANH-129 (MEN-11), ANH-172 et
ANH-191 (MEN-18). La revue du 10 octobre 2026 n'a relu aucun ticket dans
Linear : MEN-19, MEN-20 et MEN-21 citent des tickets déjà inscrits, à leur date
de lecture d'origine. Aucun jeton Linear n'est requis en CI.
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
revérifiés par lecture de chaque ticket dans Linear. L'ajout de MEN-19,
MEN-20 et MEN-21 n'a écrit aucun backlink dans leurs tickets : ils restent à
ajouter, à la prochaine relecture dans Linear. Cette traçabilité ne ferme
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
   MEN-18 est un ajout de l'auteur d'ANH-191. La revue du 10 octobre 2026, sur
   le commit `def21238818ecb05ef6dcc4b693651b4ec762b8f`, a ajouté MEN-19, MEN-20
   et MEN-21 ; son verdict est REQUEST_CHANGES, et les corrections faites depuis
   restent à relire sur leur propre SHA.
4. Une acceptation nécessite une justification signée par la personne habilitée,
   un périmètre et une échéance/revue. Faute de cette preuve, garder OPEN et son
   ticket. Une signature technique d'agent ne vaut pas acceptation humaine.
5. La [check-list de release](release-threat-review.md) impose ces preuves.
   Le [processus de release](release.md) ([ANH-134](https://linear.app/anheart/issue/ANH-134/processus-de-release-versions-semantiques-pi-convex-site-changelog)) en fait une ligne de la check-list de la PR
   `develop` vers `main` ; cette page ne déclare aucune release faite ni
   autorisée.

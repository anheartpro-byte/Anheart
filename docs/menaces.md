# Modèle de menaces logiciel Anheart

| Date | Auteur / signature technique | Périmètre | État |
|---|---|---|---|
| 2026-10-05 | Codex, agent `/root/anh136_threat_model` — attribution technique de cette rédaction | Base source courante `4e6720b10a2aefb73f6e1c42e51b4434d00042f7`, ANH-136 | Auteur du modèle et des ajouts MEN-14 à MEN-17 ; actualisation après intégration du source ANH-121 |
| 2026-10-05 | Codex, agent `/root/anh136_aa01af2_gate` — reviewer technique indépendant | Commit `aa01af2030d9a38d67d1f6ec070f678018f096a7`, arbre `fd7883a4cddbac2f7dc76baedd4fb6799ec6f360` | REJECT, confiance HIGH : seul blocage B1, absence de cette inscription de revue (EX-5). Recherche d'omissions effectuée : aucune menace ajoutée. [Compte rendu daté](reviews/anh-136-2026-10-05.md) |
| 2026-10-08 | Agent développeur du ticket ANH-191 (attribution technique d'agent, aucune signature humaine) | Branche `develop` du 8 octobre 2026 et changements d'ANH-191 | Auteur de l'ajout MEN-18 (enregistrements de séance au repos sur le disque du Pi) ; aucune autre fiche modifiée. Relu le 8 octobre 2026 par un agent reviewer indépendant du rédacteur, sur le commit local qui a introduit cette fiche (avant rebase et publication) : APPROVE, aucun blocage. Ce verdict a été transmis par le coordinateur de la fusion ; le rapport n'est pas joint à ce dépôt, et ce commit local n'y est pas non plus. Ce qu'un lecteur peut vérifier est dans la [PR #59](https://github.com/anheartpro-byte/Anheart/pull/59), qui a publié ce changement : trois avis de revue indépendante d'agent du 9 octobre 2026, tous APPROVE. Le [premier](https://github.com/anheartpro-byte/Anheart/pull/59#issuecomment-6081245622), sur des commits encore locaux, rappelle la relecture du 8 octobre et dit exacte la ligne que ce registre portait alors. Le [deuxième](https://github.com/anheartpro-byte/Anheart/pull/59#issuecomment-6082346938) porte sur le commit publié `f61f77b3b689f7fa14e42a4dbc743f500d420bce` et constate que ses deux premiers commits sont ceux déjà relus, rebasés sans autre changement. Le [troisième](https://github.com/anheartpro-byte/Anheart/pull/59#issuecomment-6082496377) contrôle le dernier écart, sur la tête de la PR, `a06a2bc05ea0b8df74fde04267b576f9a9fa3c44`. Aucun de ces avis ne dit avoir cherché des menaces oubliées : cette recherche n'est pas revendiquée ici |
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

**Scénario.** Le vol d'une clé encore valide en clair sur le Pi ou dans son environnement permet d'imiter une machine, lire son roster (pour chaque passager autorisé : identifiant, nom, FC max et, depuis le 15 octobre 2026, année de naissance) et injecter son état. Les anciennes valeurs réversibles éventuellement conservées dans une base non migrée restent exposées ; leur état réel n'a pas été inspecté.

**Existant vérifié dans le source.** [`generateApiKey`, `hashApiKey`, `verifyApiKey`](../convex/lib/crypto.ts) émettent un credential avec sélecteur aléatoire de 128 bits et secret de 256 bits ; le stockage est un HMAC-SHA256 versionné avec sel aléatoire de 128 bits, vérifié par `crypto.subtle.verify`. [`createMachine` et `regenerateApiKey`](../convex/machines.ts) stockent le sélecteur et le digest, et ne renvoient la clé en clair que dans le résultat d'émission. La nouvelle clé n'est pas reconstructible depuis ces champs. [`authenticateMachine`](../convex/lib/machineAuth.ts) refuse une machine supprimée ou `authenticationEnabled === false`, puis vérifie le credential complet ; hors ligne seul n'est pas une révocation. `getMachineByApiKey` et `validateMachineApiKey` partagent cette garde. [`validateMachineAuth`](../convex/lib/machineHttpAuth.ts), utilisé par les routes de [`http.ts`](../convex/http.ts), exige le Bearer et renvoie 401 en cas de refus. Les formats hérités sont refusés par ce nouveau chemin ; cela ne supprime ni ne migre les anciennes valeurs déjà stockées. Ces protections existent dans le source intégré, sans preuve de leur déploiement réel. Le digest ne protège pas contre le rejeu d'une clé valide volée.

**Manquant.** [ANH-121](https://linear.app/anheart/issue/ANH-121/cle-machine-reversible-et-machine-supprimee-encore-authentifiee) reste incomplet au sens de son acceptation finale tant que [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en) n'a pas assuré la migration coordonnée des anciennes clés et le déploiement vérifié. [ANH-165](https://linear.app/anheart/issue/ANH-165/convex-rotation-revocation-et-expiration-des-cles-machine-avec) porte rotation, révocation et expiration ; le vol/rejeu d'un Bearer valide reste possible. La limitation de débit par machine (ANH-166) borne ce qu'une clé volée peut envoyer sans l'empêcher d'envoyer : MEN-12. La fusion du correctif n'est ni une migration des enregistrements hérités, ni une preuve de protection en production, ni une fermeture de MEN-01.

**Preuve de fermeture attendue.** Clé ancienne/révoquée/supprimée refusée sur toutes les routes ; rotation synthétique avant migration coordonnée.

## MEN-02 Lancement distant forgé ou rejoué

- Status: OPEN
- Issues: [ANH-85](https://linear.app/anheart/issue/ANH-85/lancement-a-distance-confirmation-physique-obligatoire-a-la-machine), [ANH-144](https://linear.app/anheart/issue/ANH-144/identite-et-droits-reverifies-a-larmement-passager-affiche-et-confirme), [ANH-152](https://linear.app/anheart/issue/ANH-152/console-locale-authentification-de-loperateur-connexion-nommee-par)

F1, F4, F5 · Pi/Convex · S, T, E · Critique, personne directement.

**Scénario.** Un compte autorisé volé, une demande ancienne ou une identité de passager erronée provoque une rotation alors que personne n'a confirmé la préparation sur place.

**Existant vérifié dans le source.** [`launchAutoSession`](../convex/training.ts) vérifie les droits au clic et refuse un second lancement pour un passager ou une machine déjà tenus (`rider_busy`, `machine_busy`). Depuis la couche Convex d'[ANH-144](https://linear.app/anheart/issue/ANH-144/identite-et-droits-reverifies-a-larmement-passager-affiche-et-confirme), `armingRefusal` revérifie un lancement en attente au poll, à la confirmation (`markTrainingStarted`, 409 avec un code stable, séance `failed`) et chaque minute : droit du lanceur relu dans ses adhésions, organisation, machine, âge, FC max inchangée, passager libre, attente d'au plus 300 s ([convex.md](convex.md#revérification-à-larmement-anh-144)) ; des tests en mémoire le vérifient, rien n'est déployé. [`CloudSync._poll`](../raspberry-pi/src/cloud_sync.py) soumet immédiatement à `ControlSurface.submit_start`; [`_start_programme` / `_prepare_programme`](../raspberry-pi/src/local_panel.py) imposent les portes locales. La console arme **avant** de confirmer le départ : un refus de la confirmation l'arrête aussitôt sur la rampe ordinaire, il ne l'empêche pas de démarrer, et une confirmation restée sans réponse (réseau coupé) laisse tourner la séance. L'attestation E-STOP au démarrage du processus ne vaut pas confirmation physique de chaque départ.

**Manquant.** [ANH-85](https://linear.app/anheart/issue/ANH-85/lancement-a-distance-confirmation-physique-obligatoire-a-la-machine) impose le geste local ; la couche Pi d'[ANH-144](https://linear.app/anheart/issue/ANH-144/identite-et-droits-reverifies-a-larmement-passager-affiche-et-confirme) le passager affiché et confirmé à la console, et la confirmation demandée au serveur avant d'armer ; l'aptitude et le consentement ne sont pas encore revérifiés à l'armement ; [ANH-152](https://linear.app/anheart/issue/ANH-152/console-locale-authentification-de-loperateur-connexion-nommee-par) l'opérateur authentifié.

**Preuve de fermeture attendue.** Aucun mouvement sans confirmation locale ; demande expirée, droit retiré ou replay refusé, y compris après reconnexion.

## MEN-03 Gestionnaire malveillant ou compte gestionnaire compromis

- Status: OPEN
- Issues: [ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site), [ANH-167](https://linear.app/anheart/issue/ANH-167/convex-journal-daudit-immuable-des-actions-sensibles-consultable-et), [ANH-144](https://linear.app/anheart/issue/ANH-144/identite-et-droits-reverifies-a-larmement-passager-affiche-et-confirme)

F5 · Convex/site · T, R, E, I · Critique, personne directement via la physiologie et les droits.

**Scénario.** Un gestionnaire abuse de droits légitimes, change la physiologie d'un passager ou accède à un autre centre ; l'authentification ne prouve pas la bienveillance.

**Existant vérifié dans le source.** [`canAccessUser`, `canAccessMachine`, `requireRole`](../convex/lib/auth.ts) et [`setUserPhysiology`](../convex/training.ts) imposent liens gestionnaire/patient et bornes de valeurs. Depuis le premier lot d'[ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site), ces règles répondent pour une seule organisation : l'organisation et le rôle de l'appel viennent des revendications du jeton vérifié, et un gestionnaire ou un admin d'organisation n'atteint ni machine, ni patient, ni séance d'une autre organisation ; [`authorization.matrix.ts`](../convex/authorization.matrix.ts) et [`organizationIsolation.test.ts`](../convex/organizationIsolation.test.ts) le vérifient pour chaque fonction publique, y compris par identifiant direct. À l'intérieur d'une organisation, une valeur plausible peut rester malveillante. L'admin de l'organisation Anheart garde des droits sur toutes les organisations ; cette organisation est désignée par un réglage hébergé du déploiement (MEN-16) et, tant que ce réglage n'est pas posé, c'est l'organisation par défaut créée par la migration, pas encore reliée à Clerk ([`isAnheartOrganization`](../convex/lib/auth.ts)).

**Manquant.** Le cloisonnement d'[ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site) existe dans le source intégré ; son effet sur le service rendu dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en), de la migration et de la configuration de Clerk Organizations ; ses lots suivants, le miroir par webhook ([ANH-186](https://linear.app/anheart/issue/ANH-186/multi-organisation-lot-2-webhook-clerk-vers-le-miroir-convex-signature)) et le site qui travaille dans une seule organisation à la fois, avec sélecteur d'organisation et page Membres ([ANH-187](https://linear.app/anheart/issue/ANH-187/multi-organisation-lot-3-site-selecteur-dorganisation-membres-delegues)), existent aussi dans le source intégré, sans essai de bout en bout dans un navigateur ; [ANH-167](https://linear.app/anheart/issue/ANH-167/convex-journal-daudit-immuable-des-actions-sensibles-consultable-et) trace les actions sensibles ; [ANH-144](https://linear.app/anheart/issue/ANH-144/identite-et-droits-reverifies-a-larmement-passager-affiche-et-confirme) revérifie le passager et les droits à l'armement : sa couche Convex existe dans le source (une FC max ou un droit changé pendant l'attente fait échouer le lancement), sa couche Pi reste à faire. Ces mesures réduisent l'abus sans établir une validation médicale.

**Risque résiduel, à accepter.** Pour tenir « une séance à la fois » sur toutes les machines, [`launchAutoSession`](../convex/training.ts) refuse `rider_busy` à qui lance pour un passager partagé entre deux organisations quand ce passager a une séance ouverte dans l'autre. L'appelant de la seconde organisation apprend donc que ce passager a une séance en attente, en cours ou interrompue quelque part, sans savoir où : le refus ne nomme ni machine ni organisation (`armingRecheck.test.ts`, centre B), et seul un appelant déjà autorisé à lancer pour ce passager sur sa propre machine le reçoit, le contrôle venant après les droits et l'adhésion. C'est inhérent à l'exigence d'unicité d'[ANH-144](https://linear.app/anheart/issue/ANH-144/identite-et-droits-reverifies-a-larmement-passager-affiche-et-confirme) ; cette page ne l'accepte pas elle-même : une acceptation exige la justification signée prévue par la procédure de revue (point 4), sans laquelle ce risque reste ouvert avec MEN-03.

**Preuve de fermeture attendue.** Tests croisés entre organisations, gestionnaire non lié refusé, modification attribuée et révocation avant armement effective.

## MEN-04 Pi compromis : logiciel de sécurité, paramètres et profils

- Status: OPEN
- Issues: [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu), [ANH-139](https://linear.app/anheart/issue/ANH-139/parametres-medicaux-signes-fichier-versionne-verification-au-demarrage), [ANH-138](https://linear.app/anheart/issue/ANH-138/publication-des-programmes-vers-les-machines-le-pi-recupere-revalide)

F1, F2, F3, F4, F8 · Pi · T, E · Critique, personne directement.

**Scénario.** Un accès privilégié ou un paquet compromis modifie le superviseur, les limites, les programmes ou leurs clés de vérification.

**Existant vérifié dans le source.** [`local_config._flag`](../raspberry-pi/src/local_config.py) met `PROGRAMS_ENABLED` et `OCCUPANCY_OCCUPIED_ENABLED` à faux par défaut ; [`SafetySupervisor.require_estop_confirmed`](../raspberry-pi/src/training/safety.py) bloque sans attestation et [`_prepare_programme`](../raspberry-pi/src/local_panel.py) revalide le profil. Ces barrières exécutées sur le même Pi ne résistent pas à un root compromis ; aucune preuve de démarrage mesuré n'est revendiquée. Sur un Pi installé par l'image, la console ne tourne plus sous un compte privilégié (premier lot d'ANH-151) : [`anheart.service`](../raspberry-pi/scripts/anheart.service) la lance dans son conteneur sous le compte `anheart`, sans aucune capacité, sur une image en lecture seule, avec les seules classes de périphériques du câble et du capteur ; [`harden.sh`](../raspberry-pi/scripts/harden.sh) garde à root seul la configuration, où sont les limites et les deux verrous, donne au compte seul ce que la console écrit, et coupe les mises à jour automatiques des paquets ([pi-image.md](pi-image.md#sous-quel-compte)). Vérifié dans la machine de remplacement de la CI ([`test_install.sh`](../raspberry-pi/scripts/pi/test_install.sh)), jamais sur un Pi. Le client Docker qui lance la console reste root, parce que le démon Docker l'est ; les sources, l'image et l'unité restent modifiables par root. La même unité porte la supervision du redémarrage ([pi-image.md](pi-image.md#le-chien-de-garde)) : systemd relance, au repos, une console qu'il n'entend plus pendant 10 s ou qui meurt, dans une limite de dix démarrages par demi-heure, et [`ServiceGuard`](../raspberry-pi/src/service_state.py) met hors service une console qui redémarre trois fois en dix minutes après un arrêt non propre, jusqu'à ce qu'un opérateur nommé la remette en service. Cette supervision n'arrête aucun moteur et ne juge pas ce que fait une console qui tourne : elle croit la console qui se dit vivante. Qui peut la modifier sur le Pi : root, pour l'unité, ses délais et sa limite ; et le compte de la console lui-même, pour le signe de vie (le relais n'écoute que lui depuis ce lot) et pour l'état « hors service », qui est un fichier de ce compte à côté des enregistrements. Elle protège donc d'une console en panne, pas d'une console compromise.

**Manquant.** [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu) : le TLS et la configuration du réseau sont écrits et testés en logiciel ([procédure réseau](pi-reseau.md)) ; restent le lot stockage et les essais au banc ([pi-image.md](pi-image.md#essai-du-durcissement--à-faire-au-banc)) ; [ANH-139](https://linear.app/anheart/issue/ANH-139/parametres-medicaux-signes-fichier-versionne-verification-au-demarrage) vérifie les paramètres médicaux signés ; [ANH-138](https://linear.app/anheart/issue/ANH-138/publication-des-programmes-vers-les-machines-le-pi-recupere-revalide) revalide les programmes reçus. La racine de confiance et ses limites doivent rester explicites ; les analyses physiques ANH-103/104 restent séparées.

**Preuve de fermeture attendue.** Paramètre/profil falsifié refusé avant armement ; image durcie testée, sans activer les deux verrous ni inventer de seuil médical.

## MEN-05 Appareil hostile sur le réseau de la console

- Status: OPEN
- Issues: [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu), [ANH-152](https://linear.app/anheart/issue/ANH-152/console-locale-authentification-de-loperateur-connexion-nommee-par)

F1 · Pi/tablette · S, T, I, D, E · Critique, personne directement.

**Scénario.** Un appareil LAN intercepte le jeton, appelle des commandes ou inonde la console ; une page web tente d'ouvrir le WebSocket.

**Existant vérifié dans le source.** [`WebConfig.__post_init__`, `token_matches`, `origin_allowed`](../raspberry-pi/src/web/deps.py) refusent le bind hors loopback sans jeton d'au moins 16 caractères et comparent en temps constant ; [`serve_telemetry`](../raspberry-pi/src/web/ws.py) contrôle Origin et jeton avant acceptation. Origin absent est autorisé pour un client non navigateur, toujours soumis au jeton. Depuis le lot TLS d'[ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu), le même module refuse aussi le bind hors loopback sans certificat ([`clear_http_refusal`](../raspberry-pi/src/web/deps.py)) : la console n'y sert qu'en TLS, sur un seul port, sans port en clair ni redirection ([`build_server`](../raspberry-pi/src/web/app.py), [`server_context`](../raspberry-pi/src/web/tls.py)), et son WebSocket n'y accepte plus que l'origine `https`. Le certificat est propre au Pi, signé par une autorité créée sur ce Pi dont la clé privée n'est écrite nulle part et ne signe que lui ([`make_console_tls.sh`](../raspberry-pi/scripts/pi/make_console_tls.sh), [pi-image.md](pi-image.md#le-certificat-de-la-console)) : ni ce dépôt ni un autre Pi ne peuvent produire un certificat qu'une tablette accepterait pour cette console. Une séance ne dépend pas de ce TLS : des clients qui n'achèvent jamais leur poignée de main ne déplacent pas le tic de contrôle, et un certificat expiré n'empêche ni de servir ni d'arrêter ([`test_console_tls_load.py`](../raspberry-pi/tests/test_console_tls_load.py), [securite.md](securite.md#11-la-console-en-tls-hors-de-la-boucle-locale-anh-151)). Ces contrôles existent dans le source et sont exercés sur la boucle locale d'une machine de test : ils ne sont pas en service sur un Pi installé, dont la page écoute sur la boucle locale, et n'ont été essayés avec aucune tablette. En boucle locale la page reste en HTTP clair. Hors loopback, le jeton WebSocket en query ne circule qu'à l'intérieur de TLS et la console ne tient pas de journal d'accès ; il reste lisible sur la tablette, et le nom saisi n'authentifie personne. La clé privée de la console est au repos sur le Pi (MEN-04). L'analyse statique du dépôt ([`codeql.yml`](../.github/workflows/codeql.yml), [Analyse statique externe](framework-de-test.md#analyse-statique-externe--codeql-anh-196)) suit à chaque PR et à chaque push sur `develop` ce qui vient d'une requête jusqu'aux chemins de fichiers et aux en-têtes de réponse ; ce n'est pas une gate, et elle ne voit que les trajets qu'elle sait suivre. [`resolve_under`](../raspberry-pi/src/record/containment.py) confine un nom reçu de l'extérieur au dossier permis, liens symboliques suivis ([`test_record_containment.py`](../raspberry-pi/tests/test_record_containment.py)) ; le visualiseur du simulateur est aujourd'hui son seul appelant.

**Manquant.** Le reste d'[ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu) : le logiciel d'AP, de pare-feu et de mise en service TLS est désormais fourni par [ANH-223](pi-reseau.md), vérifié dans un noyau Linux isolé ; restent l'essai sur une tablette réelle (installation de l'autorité, WebSocket), renouvellement du certificat, qui change d'autorité, et retrait d'une autorité des tablettes, seule révocation possible ; [ANH-152](https://linear.app/anheart/issue/ANH-152/console-locale-authentification-de-loperateur-connexion-nommee-par) identité nominative, habilitation et expiration. La fuite du jeton ne constitue pas un risque accepté par la présente analyse.

Le service installé exécute la console sous le compte `anheart`, sans capacité, sur une image en lecture seule ([pi-image.md](pi-image.md#sous-quel-compte)). Cela réduit les droits du processus compromis, sans modifier les commandes qu'il peut envoyer au variateur.

**Preuve de fermeture attendue.** Console inaccessible depuis l'interface Internet ; mauvais jeton/Origin refusé ; accès TLS et sessions d'opérateurs isolées, charge hostile sans perte de boucle locale.

## MEN-06 Compromission d'un paquet pip ou npm

- Status: OPEN
- Issues: [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite), [ANH-126](https://linear.app/anheart/issue/ANH-126/cicd-deploiement-vercel-et-convex-automatise-rapports-du-framework-de)

F7, F8 · Livraison/Pi/site/Convex · T, E · Critique, personne potentiellement directement après livraison.

**Scénario.** Un fournisseur ou mainteneur compromis publie une dépendance malveillante exécutée au build ou au runtime ; une absence d'avis CVE ne prouve pas son innocuité.

**Existant vérifié dans le source.** [`ci.yml`, jobs audit/web/python](../.github/workflows/ci.yml) utilise `npm ci`, npm audit, pip-audit, actions épinglées par SHA et tests ; [`package-lock.json`](../package-lock.json) fige le graphe npm, seul verrou JavaScript du dépôt depuis le premier lot d'ANH-153 (`bun.lock` supprimé). Le job Python installe des requirements sans exiger leurs hashes ; les hashes générés pour l'audit ne sécurisent pas rétroactivement l'installation. L'image de la console d'un Pi installé part, elle, d'une image de base figée par son empreinte et n'installe que les paquets Python de ses fichiers de verrouillage, empreintes exigées ([`Dockerfile`](../raspberry-pi/Dockerfile), [pi-image.md](pi-image.md#2-versions-figées)) ; les paquets du système n'y sont figés que par leur série. Depuis le premier lot d'ANH-153, le job `audit`, vérification obligatoire lancée sur chaque PR, chaque push et chaque exécution de nuit, audite chaque environnement Python installé à part : les deux verrous de l'image du Pi tels quels, puis l'environnement des tests, la simulation hébergée et l'extracteur CAO, chacun résolu avec ses dépendances transitives. Une vulnérabilité haute ou critique npm, ou toute vulnérabilité Python connue, le fait échouer ; seules les exceptions datées et bornées de [`.github/audit-exceptions.md`](../.github/audit-exceptions.md) sont sautées, et une exception expirée le fait échouer aussi. Que la vérification rougisse sur une dépendance vulnérable puis reverdisse après son retrait a été constaté sur un poste, avec les commandes du workflow, pas encore sur une PR ([framework-de-test.md](framework-de-test.md#dépendances--un-gestionnaire-des-versions-figées-un-audit-anh-153)). Un fichier Dependabot propose chaque mois les mises à jour npm, pip et des actions ; il n'agit qu'une fois sur `main`, et les alertes et mises à jour de sécurité de Dependabot sont des réglages du dépôt, désactivés au 14 octobre 2026. Chaque exécution écrit la nomenclature CycloneDX de ce qui s'installe depuis un verrou (artefact `sbom`), sans la signer. Un audit de vulnérabilités ne détecte pas tout code malveillant.

**Manquant.** L'environnement des tests et de la CI et la simulation hébergée n'ont pas de verrou et ne s'installent pas avec leurs empreintes : un ticket de suite d'ANH-153 les porte ([framework-de-test.md](framework-de-test.md#dépendances--un-gestionnaire-des-versions-figées-un-audit-anh-153)). [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite) porte encore la revue de provenance d'un paquet et l'audit externe ; la nomenclature n'est ni durable ni signée ([release.md](release.md#8-limites-et-reste-à-faire)) ; [ANH-126](https://linear.app/anheart/issue/ANH-126/cicd-deploiement-vercel-et-convex-automatise-rapports-du-framework-de) la livraison liée aux gates et ses droits.

**Preuve de fermeture attendue.** Altération d'une dépendance détectée avant livraison ; vulnérabilité bloquante et secret synthétique refusés ; provenance et contenu du paquet revus.

## MEN-07 Prise de compte Clerk

- Status: OPEN
- Issues: [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite), [ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site)

F6, F5 · Site/Convex · S, E, I · Critique, personne indirectement via lancement et données.

**Scénario.** Phishing, cookie volé ou révocation non propagée donne les droits d'un utilisateur, gestionnaire ou admin à l'attaquant.

**Existant vérifié dans le source.** [`proxy.ts`, middleware Clerk](../proxy.ts) protège dashboard/settings ; [`ConvexClientProvider`](../components/ConvexClientProvider.tsx) transmet l'identité Clerk ; [`auth.config.ts`](../convex/auth.config.ts) configure issuer et audience `convex` ; [`requireAuth`](../convex/lib/auth.ts) exige l'identité serveur. Depuis le premier lot d'[ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site), `getCurrentUserOrThrow` lit l'organisation et le rôle de l'appel dans les revendications du jeton vérifié (`org_id`, `org_role`) : aucun argument ne les fournit, le rôle stocké n'autorise pas l'appelant quand le jeton porte une organisation, et une organisation ou un rôle inconnus sont refusés ([`organizations.test.ts`](../convex/organizations.test.ts)). Le proxy n'est pas la garde des fonctions Convex publiques. Aucune exigence MFA vérifiée dans `requireRole`.

**Manquant.** [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite) porte MFA, session bornée et révocation : son premier lot ne les traite pas. Il inventorie seulement les clés de Clerk (clé secrète, secret du webhook) et écrit leur rotation, non essayée ([secrets.md](secrets.md)). La lecture de l'appartenance et du rôle d'organisation côté serveur ([ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site)) existe dans le source intégré ; son effet sur le service rendu dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en) et de la configuration de Clerk Organizations. Le miroir des appartenances par webhook ([ANH-186](https://linear.app/anheart/issue/ANH-186/multi-organisation-lot-2-webhook-clerk-vers-le-miroir-convex-signature)) existe aussi dans le source intégré ; un retrait fait dans Clerk ne prend effet qu'à l'expiration du jeton en cours.

**Preuve de fermeture attendue.** Session révoquée et mutations sensibles sans preuve MFA refusées ; rôle fourni par un client ignoré.

## MEN-08 Fonction Convex publique sans contrôle suffisant

- Status: OPEN
- Issues: [ANH-177](https://linear.app/anheart/issue/ANH-177/convex-renforcer-deux-controles-dautorisation-men-08-men-17), [ANH-132](https://linear.app/anheart/issue/ANH-132/tests-convex-matrice-dautorisation-par-role-et-organisation-contrat), [ANH-122](https://linear.app/anheart/issue/ANH-122/corriger-les-defauts-fonctionnels-du-site-releves-par-la-documentation), [ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site)

F4, F5 · Convex · S, T, I, E · Critique, personne indirectement.

**Scénario.** Une fonction exposée oublie la garde ou contrôle l'identité sans contrôler la ressource ; le client appelle directement l'API en contournant le site.

**Existant vérifié dans le source.** [`requireAuth` / `canAccessUser`](../convex/lib/auth.ts) existent mais doivent être appelés par chaque endpoint. [`getSessionTelemetry`](../convex/training.ts) vérifie passager ou machine. Depuis le premier lot d'[ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site), chaque fonction publique répond dans l'organisation de l'appelant ; la matrice appelle chacune depuis une autre organisation, et [`completeness.test.ts`](../convex/completeness.test.ts) échoue si une fonction n'a pas ce cas ou si une fonction qui prend un identifiant n'est pas appelée avec celui d'une autre organisation. Cinq routes de [`http.ts`](../convex/http.ts) lisent ou modifient une séance désignée par son identifiant : `/training/start`, `/training/end`, `/training/status`, `/training/telemetry` et `/training/events`. Chacune transmet la machine authentifiée à sa fonction interne (`markTrainingStarted`, `endTrainingSession`, `getTrainingStatus`, `storeTelemetry`, `storeEvents` dans [`training.ts`](../convex/training.ts)), qui vérifie d'abord que la séance appartient à cette machine. Pour ces cinq routes, une séance d'une autre machine reçoit exactement la réponse d'une séance inconnue (statut, en-têtes et corps), quel que soit l'état de la séance, et ni la séance, ni sa machine, ni les mesures enregistrées ne changent ; [`httpRoutes.test.ts`](../convex/httpRoutes.test.ts) compare les deux réponses pour chaque route ([ANH-177](https://linear.app/anheart/issue/ANH-177/convex-renforcer-deux-controles-dautorisation-men-08-men-17)). Voir aussi MEN-17.

**Manquant.** [ANH-132](https://linear.app/anheart/issue/ANH-132/tests-convex-matrice-dautorisation-par-role-et-organisation-contrat) couvre la matrice de toutes les fonctions publiques et routes ; [ANH-122](https://linear.app/anheart/issue/ANH-122/corriger-les-defauts-fonctionnels-du-site-releves-par-la-documentation) les défauts fonctionnels documentés. L'isolation par organisation d'[ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site) existe dans le source intégré ; comme les autres, son effet sur le service rendu dépend du redéploiement. Le contrôle d'[ANH-177](https://linear.app/anheart/issue/ANH-177/convex-renforcer-deux-controles-dautorisation-men-08-men-17) existe dans le source intégré ; son effet sur le service rendu dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en). Les routes héritées de l'ancien mode d'enregistrement ECG sont retirées du source ([ANH-135](https://linear.app/anheart/issue/ANH-135/retirer-lancien-mode-enregistrement-ecg-srcmain-routes-sessiondata)) : [`legacyRecordingRetired.test.ts`](../convex/legacyRecordingRetired.test.ts) vérifie qu'elles répondent 404 ; leur disparition du service rendu dépend aussi du redéploiement ANH-82.

**Complément ANH-180.** La conservation et l'attribution des séances locales
suivent la règle documentée dans [Convex](convex.md#sessions). Les messages de
création de dossier respectent les droits de lecture de l'appelant. Les tests de
matrice et de route accompagnent ces contrôles ; leur effet sur le service rendu
reste conditionné au redéploiement ANH-82. Cette mise à jour ne clôt pas MEN-08.

**Preuve de fermeture attendue.** Appel anonyme et identifiant de ressource d'une autre machine/organisation refusés pour chaque fonction ; export public ajouté sans cas de matrice fait échouer la gate.

## MEN-09 URL de fichier Convex exposée

- Status: OPEN
- Issues: [ANH-130](https://linear.app/anheart/issue/ANH-130/depot-des-enregistrements-de-seance-dans-convex-storage-organise-par), [ANH-114](https://linear.app/anheart/issue/ANH-114/multi-organisation-separer-les-clients-dans-convex-et-le-site)

F4, F5 · Convex Storage/site · I · Haute, confidentialité santé ; pas de mouvement direct.

**Scénario.** Une URL de téléchargement devient une capacité transférable, fuit par historique, referrer ou logs et contourne la révocation des droits.

**Existant vérifié dans le source.** [`schema.ts`, `session_summaries.reportFileId`](../convex/schema.ts) déclare un identifiant de stockage ; [`getSummary`](../convex/sessionSummaries.ts) retourne les métadonnées. ANH-130 ajoute le dépôt vérifié et la route JWT `/api/records/download` : chaque lecture contrôle le rôle, l'organisation et l'accès à la machine ; les métadonnées publiques n'exposent aucun `storageId` ni URL Storage. Les réponses sont privées, sans cache, et une suppression révoque immédiatement les lectures. Les tests couvrent appel anonyme, patient, autre organisation, altération et suppression ; un téléchargement authentifié a été exercé sur Convex de développement avec un enregistrement synthétique.

**Manquant.** Les contrôles d'ANH-130 et le cloisonnement d'ANH-114 existent dans le source et sur le déploiement de développement vérifié. Leur déploiement et leur contrôle en production, ainsi que la revue de menace au jalon, restent nécessaires ; les URL natives d'envoi restent des capacités temporaires et ne sont pas des URL de lecture.

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

**Existant vérifié dans le source.** [`RealClock.monotonic` et `unix_millis`](../raspberry-pi/src/clock.py) séparent durées de sécurité et horodatage ; [`SafetySupervisor`](../raspberry-pi/src/training/safety.py) utilise le monotone. La borne que le serveur appliquait à l'heure des lots ECG a disparu avec la route de l'ancien mode d'enregistrement. La console dit au serveur l'âge de chaque séance, compté sur son horloge monotone, et date ses points comme le début plus le temps écoulé ([`record_uplink.py`](../raspberry-pi/src/record_uplink.py), [`upload.py`](../raspberry-pi/src/record/upload.py)) : Convex place alors début, fin et date de mesure sur sa propre horloge, n'écrit un point ou un événement que s'il est daté dans sa séance sur l'axe de la machine (`machineAxis`, `sessionWindow` dans [`training.ts`](../convex/training.ts), [`journalSync.test.ts`](../convex/journalSync.test.ts)), et une heure murale fausse ou corrigée en cours de séance ne change ni ce qui est stocké ni ce qui est montré comme actuel ([`test_cloud_journal_e2e.py`](../raspberry-pi/tests/test_cloud_journal_e2e.py)). Ce traitement par Convex existe dans le source intégré ; son effet sur le service rendu dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en). Reste, pour cette console, l'exactitude de l'âge : le délai d'acheminement de la déclaration, et un âge faux, dont ce qui le borne est décrit dans [convex.md](convex.md#deux-horloges). Pour une console d'une version antérieure, qui ne dit pas cet âge, l'heure de la télémétrie reste celle du Pi : stockée et servie telle quelle, sans borne ; une mesure envoyée en retard n'est pas montrée comme actuelle si l'horloge du Pi est juste ou en retard, mais une horloge en avance de X peut faire lire une mesure envoyée en retard comme actuelle jusqu'à 20 s + X après elle. Après un redémarrage du système, la console ne sait plus compter l'âge d'une séance d'avant : elle laisse alors la date qu'elle avait écrite, ou, si cette date est antérieure à 2024, annonce l'âge le plus petit possible. [`_prepare_programme`](../raspberry-pi/src/local_panel.py) transmet l'heure murale à la résolution du profil : toute décision n'est donc pas indépendante de l'heure. La console lit si un service de temps tient son horloge murale ([`wall_clock.py`](../raspberry-pi/src/wall_clock.py), hors de la boucle de contrôle) : elle l'affiche, l'écrit dans le manifeste de chaque enregistrement (`clocks.wall_trusted`), prévient au départ d'une séance de banc et refuse tout départ avec une personne à bord tant que personne ne répond de cette horloge ([raspberry-pi.md, 12.4](raspberry-pi.md#124-lhorloge-murale-et-le-journal-de-la-console), [`test_wall_clock_console.py`](../raspberry-pi/tests/test_wall_clock_console.py)) ; l'installation du Pi met en place le service de temps ([pi-image.md](pi-image.md#lheure)). Vérifié par les tests, avec un état d'horloge que le test décide ; rien n'a été constaté sur un Pi.

**Manquant.** [ANH-163](https://linear.app/anheart/issue/ANH-163/pi-horloge-fiable-ntp-ou-rtc-journaux-persistants-et-bornes) essai de coupure secteur au banc, non fait ([pi-image.md](pi-image.md#essai-de-coupure-secteur--à-faire-au-banc)), et une indication de confiance qui ne quitte pas le Pi : pour le serveur, la date qu'une console écrit reste sans borne dans les deux cas où le serveur ne la remplace pas, une séance envoyée après un redémarrage du système (la console ne sait plus compter son âge), et toute séance d'une console d'une version antérieure, qui ne dit pas cet âge ; [ANH-142](https://linear.app/anheart/issue/ANH-142/controles-pre-vol-automatiques-avant-chaque-seance-variateur-bitalino) pré-vol bloque un état d'horloge non fiable avant personne à bord. Un système privilégié compromis reste couvert par MEN-04.

**Preuve de fermeture attendue.** Horloge murale reculée/avancée sans raccourcir les délais monotones ; démarrage hors réseau signale heure non fiable et bloque le pré-vol occupé.

## MEN-12 Déni de service des routes machine

- Status: OPEN
- Issues: [ANH-166](https://linear.app/anheart/issue/ANH-166/convex-limitation-de-debit-par-machine-et-par-utilisateur-taille)

F4 · Convex · D · Haute, disponibilité et preuve ; pas de mouvement direct.

**Scénario.** Appels anonymes ou clé valide compromise saturent l'authentification, le parsing JSON, le stockage ou les quotas cloud ; les demandes de stop distantes deviennent indisponibles.

**Existant vérifié dans le source.** [`machineRoute` et route `/training/telemetry`](../convex/http.ts) authentifient puis, sur cinq routes, prennent un jeton de la limite de la machine avant de lire le corps ([`convex/lib/rateLimits.ts`](../convex/lib/rateLimits.ts), composant `@convex-dev/rate-limiter` 0.3.2 monté par [`convex/convex.config.ts`](../convex/convex.config.ts)) : heartbeat 1 toutes les 5 s, interrogation 1 toutes les 2 s, télémétrie 2 lots toutes les 5 s, événements 10 par seconde, réservation de dépôt 10 par seconde avec une réserve de 64 (le ticket demandait 10 par minute : écart motivé dans [convex.md](convex.md#limitation-de-débit-et-taille-des-corps-anh-166), à confirmer). Au-delà : 429 `rate_limited` avec `retryAfterMs`, rien d'écrit. Les corps du heartbeat (16 Kio), de la télémétrie (300 Kio) et des événements (2,5 Mio) sont refusés en 413 `payload_too_large` sur l'en-tête `Content-Length` ou au premier octet de trop, avant toute lecture du JSON ; 601 points ou 201 événements le sont aussi, avant d'en lire un. Le rang d'un événement est borné à 999 999 (donc le nombre d'événements d'une séance) et sa date est entière. Par compte : `launchAutoSession` 5 par minute, toute mutation publique qui écrit et `createPatient` 60 par minute, en `ConvexError` `rate_limited`. La demande d'arrêt n'est jamais limitée, ni côté machine (`/training/status`) ni côté compte (`requestStop`). Un lot envoyé de nouveau n'écrit rien de plus (`storeTelemetry`, `storeEvents`). La console espace d'elle-même ce qu'elle envoie (un lot de 300 points au plus toutes les 3 s, rien pendant 15 s après une réponse retenue, [`record_uplink.py`](../raspberry-pi/src/record_uplink.py)) et ses cadences sont épinglées sous ces limites par ses tests ; [`PacedTransport`](../raspberry-pi/src/cloud_sync.py) ne redemande pas une route avant la fin de l'attente d'un 429 (5 min au plus) et le journalise ; [`CloudSync.step`](../raspberry-pi/src/cloud_sync.py) sépare la synchronisation de l'autorité locale. Tests : `convex/rateLimits.test.ts` (dont 1000 heartbeats en 10 s d'une machine : 2 acceptés, 998 refusés, aucune erreur) et `raspberry-pi/tests/test_cloud_rate_limit.py`. Tout cela existe dans le source et n'a été exécuté qu'en mémoire ; son effet sur le service rendu dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en). La perte du cloud ne remplace jamais l'arrêt local.

**Manquant.** Un appel sans clé valide n'est pas limitable dans le code : Convex ne donne pas l'adresse de l'appelant, et chaque appel anonyme coûte une lecture d'index avant son 401 ; seule une protection en amont (pare-feu, limite de la plateforme) le bornerait. Les routes appelées une fois par séance ou par fichier (`training/local`, `training/start`, `training/end`, `profiles`, `records/commit`, `records/status`) ne sont pas limitées, ni `roster`, que la console demande à son démarrage puis toutes les 60 s au repos ; une clé volée peut donc encore créer des séances locales sans plafond de nombre. Pour une console qui ne dit pas l'âge de sa séance (contrat 1.0), le nombre de points par séance n'est borné que par le débit. Aucune mesure contre un déploiement réel.

**Preuve de fermeture attendue.** Rafale de heartbeats et corps trop gros refusés avec codes attendus ; commande locale et boucle de contrôle restent disponibles.

## MEN-13 Fuite de données de santé dans les journaux d'erreur

- Status: OPEN
- Issues: [ANH-150](https://linear.app/anheart/issue/ANH-150/vues-de-flotte-et-alertes-equipe-liste-filtrable-detail-machine), [ANH-163](https://linear.app/anheart/issue/ANH-163/pi-horloge-fiable-ntp-ou-rtc-journaux-persistants-et-bornes), [ANH-90](https://linear.app/anheart/issue/ANH-90/messages-derreur-serveur-en-francais-et-alertes-machine-hors-ligne)

F3, F4, F5, F7 · Pi/Convex/site/exploitation · I · Haute, confidentialité santé.

**Scénario.** Une exception, une trace, un rapport CI ou un outil de suivi exporte ECG, fréquence cardiaque, réponses ou identité passager hors des accès prévus.

**Existant vérifié dans le source.** [`CloudSync._note`](../raspberry-pi/src/cloud_sync.py) limite la répétition des erreurs, sans expurger tous les détails serveur ; [`refusalOf`](../convex/lib/contract.ts) met le texte de toute erreur levée pendant une route machine (`error.message`) dans le refus que [`machineRoute`](../convex/http.ts) renvoie à la machine. Le calcul de résumé qui journalisait moyenne/min/max de FC et HRV a été retiré du source avec l'ancien mode d'enregistrement ECG ; il reste présent dans la version déployée jusqu'au redéploiement ANH-82. L'analyse statique du dépôt ([`codeql.yml`](../.github/workflows/codeql.yml), [Analyse statique externe](framework-de-test.md#analyse-statique-externe--codeql-anh-196)) cherche les valeurs écrites en clair dans un journal ou sur la sortie standard quand leur nom désigne une donnée privée, fréquence cardiaque comprise ; elle les reconnaît à leur nom, pas à leur contenu, et n'est pas une gate. Aucune garantie générale de logs sans données de santé n'est donc acquise. Pour la seule console du Pi : chaque ligne qu'elle journalise est un objet JSON ([`log_format.py`](../raspberry-pi/src/log_format.py)), et [`test_log_format.py`](../raspberry-pi/tests/test_log_format.py) relit chaque appel de journalisation de son source et mène une vraie console là où ces données existent (passager refusé pour son âge, fréquence cardiaque qui déclenche un verdict) : ni fréquence cardiaque, ni valeur d'échantillon, ni identité de passager n'y sont écrites ([raspberry-pi.md, 12.4](raspberry-pi.md#124-lhorloge-murale-et-le-journal-de-la-console)). Le journal du Pi est gardé sur le disque et borné par l'installation ([pi-image.md](pi-image.md#le-journal-du-système)). Le nom saisi par l'opérateur y figure encore, et le texte d'une exception imprévue n'est pas filtré. Les alertes aux gestionnaires d'[ANH-90](https://linear.app/anheart/issue/ANH-90/messages-derreur-serveur-en-francais-et-alertes-machine-hors-ligne) ([`convex/lib/notifications.ts`](../convex/lib/notifications.ts)) ne gardent qu'un type, une machine, une séance et un code de la machine (mnémonique du défaut, action du verdict), jamais la raison de fin ni le détail d'un verdict ; l'e-mail qui peut les accompagner ([`convex/lib/notificationEmail.ts`](../convex/lib/notificationEmail.ts)) ne dit que le type, le nom de la machine et un lien vers le site, et [`convex/notifications.test.ts`](../convex/notifications.test.ts) vérifie avec des valeurs sentinelles (fréquence cardiaque, nom du passager, identifiant de séance, règle du verdict) qu'aucune n'y figure. Aucun e-mail n'a été envoyé : le compte d'envoi et sa clé ne sont pas fournis. La raison de fin qu'affiche la page des alertes, lue sur la séance pour qui peut la lire, est le texte de la machine, non filtré.

**Manquant.** [ANH-150](https://linear.app/anheart/issue/ANH-150/vues-de-flotte-et-alertes-equipe-liste-filtrable-detail-machine) exige les erreurs sans variables/données de santé et un test d'exclusion ; [ANH-163](https://linear.app/anheart/issue/ANH-163/pi-horloge-fiable-ntp-ou-rtc-journaux-persistants-et-bornes) journal du Pi relu après une coupure secteur, essai au banc non fait ([pi-image.md](pi-image.md#essai-de-coupure-secteur--à-faire-au-banc)). Les identifiants de séance et opérateur sont aussi à traiter selon le contexte.

**Preuve de fermeture attendue.** Erreur avec données synthétiques sentinelles : absence de champs de santé et identifiants passager dans chaque export de logs ; purge et accès examinés sans données réelles.

## MEN-14 Injection Modbus ou double maître sur le câble variateur

- Status: OPEN
- Issues: [ANH-74](https://linear.app/anheart/issue/ANH-74/verrou-unique-sur-le-cable-variateur-console-bench-console), [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu)

F2 · Pi/variateur · S, T, D · Critique, personne directement.

**Scénario.** Un outil de banc concurrent ou un accès physique/privilégié au port écrit une consigne contradictoire ou forge un retour.

**Existant vérifié dans le source.** [`ATV320Drive.open`](../raspberry-pi/src/motor/atv320.py) valide l'adressage par lecture ; le backend sérialise ses appels dans le processus. Le verrou entre processus d'[ANH-74](https://linear.app/anheart/issue/ANH-74/verrou-unique-sur-le-cable-variateur-console-bench-console) est intégré : [`DriveLease`](../raspberry-pi/src/motor/drive_process_lock.py) prend, sans attendre, un verrou du noyau sur un fichier de la machine, et les deux transports ([`atv320.py`](../raspberry-pi/src/motor/atv320.py), [`ftdi_link.py`](../raspberry-pi/src/motor/ftdi_link.py)) ne s'ouvrent qu'avec lui. Un second programme est refusé avant toute ouverture du transport, le texte laissé dans le fichier ne permet jamais de reprendre le verrou, et celui-ci n'est rendu qu'après une fermeture réussie du transport ou la fin du processus ([`test_drive_process_lock.py`](../raspberry-pi/tests/test_drive_process_lock.py)). Sa portée s'arrête aux programmes qui voient ce fichier : sur un Pi installé par l'image, ANH-224 partage désormais ce fichier avec l'hôte par bind du même inode ([preuve Linux](pi-stockage.md)) ; les outils tiers qui ignorent le verrou restent à fermer ([pi-image.md](pi-image.md#8-limites-et-reste-à-faire)). [`SafetySupervisor._rule_setpoint_unconfirmed`](../raspberry-pi/src/training/safety.py) surveille les retours. Ce verrou n'engage que les programmes qui le demandent ; ce n'est pas une authentification Modbus, et un retour forgé peut tromper le contrôle. Depuis le premier lot d'ANH-151, sur un Pi installé, la console n'ouvre le câble que par le groupe des périphériques série et par une classe de périphériques nommée dans [`anheart.service`](../raspberry-pi/scripts/anheart.service), et une règle écrite par [`install.sh`](../raspberry-pi/scripts/install.sh) donne à ce seul groupe le câble Schneider, qui n'a pas de pilote série ([pi-image.md](pi-image.md#sous-quel-compte)). Ces droits n'ont été constatés qu'avec des nœuds sans matériel, dans la machine de remplacement de la CI. Ce n'est pas une exclusivité : root et tout compte de ce groupe ouvrent le même câble.

**Manquant.** [ANH-74](https://linear.app/anheart/issue/ANH-74/verrou-unique-sur-le-cable-variateur-console-bench-console) a réservé le transport entre processus coopérants ; le partage du verrou hôte/conteneur est maintenant écrit et vérifié sur Linux par ANH-224 ; sa validation avec le vrai pilote reste nécessaire avant de relier le variateur ([pi-image.md](pi-image.md#8-limites-et-reste-à-faire)). Le premier lot d'[ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu) a restreint l'accès de la console au périphérique ; l'essai avec le vrai câble reste à faire ([pi-image.md](pi-image.md#essai-du-durcissement--à-faire-au-banc)). Verrou logiciel et permissions ne couvrent pas une injection physique/root, à reprendre dans ANH-103/104.

**Preuve de fermeture attendue.** Second processus refusé avant ouverture/écriture ; accès non habilité au périphérique refusé ; danger d'injection physique explicitement repris à la revue machine.

## MEN-15 BITalino usurpé ou signal physiologique forgé

- Status: OPEN
- Issues: [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu), [ANH-80](https://linear.app/anheart/issue/ANH-80/valider-les-6-capteurs-bitalino-reels-fonctions-de-transfert-gains), [ANH-142](https://linear.app/anheart/issue/ANH-142/controles-pre-vol-automatiques-avant-chaque-seance-variateur-bitalino)

F3 · Pi/capteur · S, T, D · Critique, personne directement.

**Scénario.** Un appareil substitué, Bluetooth perturbé ou un signal artificiel produit une FC plausible, ou supprime la mesure pendant la rotation.

**Existant vérifié dans le source.** [`EcgBridge._follow`, `_vet`, `judge`](../raspberry-pi/src/ecg_pipeline.py) contrôlent continuité et accord des deux traitements ; [`SafetySupervisor._rule_hr_stale`](../raspberry-pi/src/training/safety.py) réagit aux mesures périmées. Deux traitements du même signal ne prouvent pas son origine ni sa vérité ; aucune authentification cryptographique du capteur n'est établie par cette lecture. Depuis le premier lot d'ANH-151, sur un Pi installé, la console n'a plus accès au D-Bus de la machine ni le droit de lier elle-même un nœud Bluetooth : le service lie le nœud du capteur configuré avant de la lancer ([`bind-bitalino.sh`](../raspberry-pi/scripts/pi/bind-bitalino.sh)), ce qui n'a jamais été essayé avec le capteur. L'appairage repose sur le code d'usine du BITalino, le même pour tous les capteurs : il n'est plus écrit dans le journal, et ce n'est pas une authentification.

**Manquant.** Les lots suivants d'[ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu) limitent l'accès au Pi par le réseau, et ne sont pas faits ; [ANH-80](https://linear.app/anheart/issue/ANH-80/valider-les-6-capteurs-bitalino-reels-fonctions-de-transfert-gains) valide les capteurs réels ; [ANH-142](https://linear.app/anheart/issue/ANH-142/controles-pre-vol-automatiques-avant-chaque-seance-variateur-bitalino) pré-vol vérifie leur état. Ces tickets ne promettent pas à eux seuls une liaison anti-usurpation : la revue indépendante doit préciser ce résiduel et son traitement avant clôture.

**Preuve de fermeture attendue.** Déconnexion/perte de continuité traitée ; capteur substitué et signal plausible falsifié explicitement examinés au banc avant toute acceptation humaine.

## MEN-16 Secrets CI ou artefacts de livraison compromis

- Status: OPEN
- Issues: [ANH-126](https://linear.app/anheart/issue/ANH-126/cicd-deploiement-vercel-et-convex-automatise-rapports-du-framework-de), [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite), [ANH-134](https://linear.app/anheart/issue/ANH-134/processus-de-release-versions-semantiques-pi-convex-site-changelog)

F7 · Livraison/site/Convex · S, T, R, E · Critique, personne indirectement via logiciel livré.

**Scénario.** Un contributeur, compte GitHub/Vercel compromis ou runner altéré vole des credentials de déploiement, livre un commit non revu ou substitue l'artefact après les tests.

**Existant vérifié dans le source.** [`ci.yml`](../.github/workflows/ci.yml) déclare `contents: read`, actions par SHA et `persist-credentials: false`; les gates auditées ne constituent pas un déploiement Vercel/Convex authentifié. [`codeql.yml`](../.github/workflows/codeql.yml) fait aussi lire les workflows GitHub Actions du dépôt par l'analyse statique ([Analyse statique externe](framework-de-test.md#analyse-statique-externe--codeql-anh-196)), sans en faire une gate. [`vercel.json`](../vercel.json) coupe les déploiements Vercel déclenchés par un push (constaté d'abord sur une branche de travail ; le fichier est sur `main` depuis la fusion de la release du 10 octobre 2026, et GitHub n'enregistre aucun déploiement depuis le 7 octobre 2026, ni à cette fusion ni à celle du 13 octobre 2026 ; relevé le 14 octobre 2026 depuis GitHub, sans consulter Vercel), et deux workflows lancés à la main ([`deploy-production.yml`](../.github/workflows/deploy-production.yml), [`deploy-preview.yml`](../.github/workflows/deploy-preview.yml)) déploient le site et la simulation, chacun depuis sa seule branche, par un environnement GitHub, sans permission d'écriture, avec un jeton Vercel lu comme secret et remis aux seules étapes qui appellent Vercel ([framework-de-test.md](framework-de-test.md#déploiement-vercel-par-bouton-anh-198)). Ces deux workflows ne vérifient pas que les gates du commit déployé sont vertes. Un troisième, la pipeline de release ([`release.yml`](../.github/workflows/release.yml)), elle aussi lancée à la main et sur `main` seulement, relie la livraison aux contrôles : elle refait tourner toutes les gates et CodeQL sur le commit exact, s'arrête à la première alerte ouverte, pose les tags par `scripts/release.sh`, puis déploie Convex et, par les jobs du bouton de production, le site et la simulation. Un seul de ses jobs peut écrire dans le dépôt (les tags), chacune de ses écritures passe par l'environnement GitHub `production`, la clé de déploiement de Convex est comparée au déploiement attendu avant usage, et une exécution à blanc, cochée par défaut, n'a ni secret ni droit d'écriture ([framework-de-test.md](framework-de-test.md#pipeline-de-release-anh-219)). Elle n'a jamais tourné. Environnements GitHub, approbation, secrets, protections des branches, variables hébergées et paramètres SaaS sont des réglages hors du dépôt : ils n'ont pas été inspectés ici. L'une de ces variables hébergées fait autorité sur les droits : `ANHEART_ORG_ID`, sur le déploiement Convex, désigne l'organisation dont les admins sont admins Anheart ([`anheartClerkOrganizationId`, `isAnheartOrganization`](../convex/lib/auth.ts), [convex.md](convex.md#activer-le-multi-organisation)). Tant que cette variable n'est pas posée, cette organisation est l'organisation par défaut créée par la migration, pas encore reliée à Clerk. La protection de cette désignation est donc celle des droits d'écriture sur les variables de ce déploiement, hors du dépôt ; ce modèle ne la couvre que par cette fiche, de façon générale, et son effet sur le service rendu dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en). [`scripts/release.sh`](../scripts/release.sh) refuse de préparer une release, d'ouvrir la PR `develop` vers `main` ou de poser un tag quand une vérification CI du commit visé n'est pas terminée et réussie, et chaque composant porte une version lisible ([release.md](release.md)) ; ce contrôle tourne sur le poste du responsable de release, ou dans la pipeline de release, qui déploie le commit qu'elle vient de taguer. Rien ne relie encore l'empreinte de l'artefact servi au commit tagué.

**Manquant.** [ANH-126](https://linear.app/anheart/issue/ANH-126/cicd-deploiement-vercel-et-convex-automatise-rapports-du-framework-de) lie aperçu et production aux gates ; [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite) a écrit l'inventaire des secrets et une procédure de rotation pour chacun ([secrets.md](secrets.md)), mais aucune n'a été essayée, les listes nominatives d'accès ne sont pas tenues, et l'analyse des secrets de GitHub avec sa protection au push est désactivée (réglage du dépôt) ; gitleaks, lui, analyse tout l'historique à chaque exécution de la CI. L'audit externe reste à faire ; [ANH-134](https://linear.app/anheart/issue/ANH-134/processus-de-release-versions-semantiques-pi-convex-site-changelog) a écrit le processus (version, changelog, check-list) ; la première release, `pi-1.0.0`, `cloud-1.0.0`, `web-1.0.0`, est préparée et fusionnée dans `main` le 10 octobre 2026, mais pas publiée : aucun tag n'est posé et rien n'est déployé ([release.md](release.md)). L'identité de l'artefact testé doit être celle livrée.

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

**Existant vérifié dans le source.** [`prepare_root`](../raspberry-pi/src/record/journal.py) réserve le dossier des enregistrements au compte de la console (mode 700). [`Writer.create`, `create_private` et `write_file`](../raspberry-pi/src/record/writer.py) créent chaque dossier de séance en 700 et chaque fichier en 600, dès l'appel qui le crée ; le journal hors séance ([`Logbook`](../raspberry-pi/src/record/logbook.py)) et le marqueur de dépôt ([`confirm_deposit`](../raspberry-pi/src/record/retention.py)) suivent la même règle ([ANH-191](https://linear.app/anheart/issue/ANH-191/boite-noire-suites-danh-128-inodes-trames-variateur-journal-hors)). Un enregistrement ne porte ni nom de passager, ni nom d'opérateur ([`operator_alias`, `redact`](../raspberry-pi/src/record/session.py)), ni adresse e-mail, ni clé de machine. L'export passe par la console, derrière son jeton, machine au repos ([`_register_records`](../raspberry-pi/src/web/routes.py)). Ces droits de fichiers ne valent que sur la machine en marche et contre un compte sans privilèges : **le volume des nouveaux enregistrements est chiffré après la mise en service LUKS2 d'ANH-224** ([procédure et limites](pi-stockage.md)). La clé en fichier sur la même carte ne protège pas contre le vol de toute la carte ; les anciennes copies et les archives exportées restent en clair. La console ne retouche pas les enregistrements créés avant ce changement ; sur un Pi, l'installation les referme et les rend au compte de la console, sans en lire le contenu ([`harden.sh`](../raspberry-pi/scripts/harden.sh)). Aucun enregistrement n'est purgé sans dépôt confirmé hors de la machine, le dépôt vérifié d'ANH-130 écrit maintenant cette confirmation après contrôle de l'inventaire complet et des empreintes. Les enregistrements non confirmés restent sur le disque. L'alias de l'opérateur est un pseudonyme, pas un anonymat. Sur un Pi installé, la console écrit sous le compte `anheart`, sans privilèges, depuis le premier lot d'ANH-151 ([pi-image.md](pi-image.md#sous-quel-compte)). Rien de ceci n'a été vérifié sur une machine réelle.

**Manquant.** [ANH-172](https://linear.app/anheart/issue/ANH-172/donnees-de-sante-decision-avis-ecrit-juriste-ou-dpo-sur-convex-clerk) doit dire ce qui est exigé du disque du Pi (chiffrement au repos ou purge après dépôt) et les durées de conservation. Le dépôt hors de la machine d'[ANH-130](https://linear.app/anheart/issue/ANH-130/depot-des-enregistrements-de-seance-dans-convex-storage-organise-par) fournit la confirmation nécessaire à la purge locale ; son effet sur un Pi installé reste à constater. Le premier lot d'[ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu) fait tourner la console sans privilèges ; ses lots suivants durcissent l'accès au système, et ANH-224 fournit maintenant la préparation du volume LUKS2 ; le choix matériel de conservation de la clé et le traitement des copies antérieures restent à valider. La forme de l'alias de l'opérateur est une décision ouverte du chef de projet, inscrite dans [ANH-191](https://linear.app/anheart/issue/ANH-191/boite-noire-suites-danh-128-inodes-trames-variateur-journal-hors). Le contrôle retenu (chiffrement, purge, ou les deux) n'est pas encore choisi : la capacité logicielle LUKS2 est fournie, mais aucune mise en service physique ni fermeture de cette menace n'est acquise.

**Preuve de fermeture attendue.** Avis ANH-172 joint et appliqué ; sur une machine d'essai, selon le contrôle retenu, des enregistrements synthétiques illisibles une fois le support sorti de la machine, ou absents du disque au terme de la durée de conservation après un dépôt confirmé ; droits des dossiers et fichiers constatés sur un Pi installé ; décision écrite sur l'alias de l'opérateur.

**Ce qu'un lecteur peut vérifier de la relecture de cette fiche.** La relecture du 8 octobre 2026 inscrite au registre en tête de page a porté sur un commit local, que le dépôt public ne contient pas. La fiche y est entrée par la [PR #59](https://github.com/anheartpro-byte/Anheart/pull/59) (ANH-191), dont la page donne pour commit de fusion `a96dd1cff07574e3ff552ab2b108d5c867a3d09a`. Les identifiants des commits de `develop` antérieurs au 10 octobre 2026 ont changé à cette date : ce changement y porte aujourd'hui l'identifiant `3f50b009c82efa999542545b9b365c416d36b8cc`, pour le même arbre de fichiers. Trois avis de revue indépendante d'agent y sont publiés, tous APPROVE. Le [premier](https://github.com/anheartpro-byte/Anheart/pull/59#issuecomment-6081245622) porte sur des commits locaux, avant publication, et dit exacte la ligne que ce registre consacrait alors à la fiche. Le [deuxième](https://github.com/anheartpro-byte/Anheart/pull/59#issuecomment-6082346938) porte sur le commit publié `f61f77b3b689f7fa14e42a4dbc743f500d420bce`, et constate que ses deux premiers commits sont ceux déjà relus, rebasés sans autre changement. Le [troisième](https://github.com/anheartpro-byte/Anheart/pull/59#issuecomment-6082496377) contrôle le dernier écart, sur la tête de la PR, `a06a2bc05ea0b8df74fde04267b576f9a9fa3c44`. Aucun de ces avis ne dit avoir cherché des menaces oubliées : ils ne remplacent pas la revue de ce modèle décrite dans la [procédure](#procédure-de-revue-aux-jalons-et-avant-pilote).

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
- Issues: [ANH-130](https://linear.app/anheart/issue/ANH-130/depot-des-enregistrements-de-seance-dans-convex-storage-organise-par), [ANH-163](https://linear.app/anheart/issue/ANH-163/pi-horloge-fiable-ntp-ou-rtc-journaux-persistants-et-bornes), [ANH-191](https://linear.app/anheart/issue/ANH-191/boite-noire-suites-danh-128-inodes-trames-variateur-journal-hors), [ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu), [ANH-150](https://linear.app/anheart/issue/ANH-150/vues-de-flotte-et-alertes-equipe-liste-filtrable-detail-machine)

Stockage local et F4 · Pi/Convex · T, R, D · Haute, intégrité de la preuve et disponibilité de la machine ; personne indirectement, sans mouvement.

**Scénario.** Avant qu'une copie complète ait quitté la machine, l'enregistrement d'une séance est modifié, supprimé ou perdu avec son support, du fait d'un compte local privilégié, d'un Pi compromis ou d'une panne du stockage : ce que la séance a été ne peut plus être établi, ou l'est à tort. Sans aucun acteur, les enregistrements s'accumulent faute de dépôt, jusqu'au seuil sous lequel la console refuse tout départ.

**Existant vérifié dans le source.** [`Writer.close`](../raspberry-pi/src/record/writer.py) écrit à la clôture `checksums.sha256`, une empreinte SHA-256 par fichier ; un enregistrement clos n'accepte plus d'ajout, un bloc n'est jamais remplacé, et le lecteur signale tout fichier manquant, tronqué ou différent de son empreinte ([enregistrement.md](enregistrement.md#fermeture-intégrité-et-lecture-interrompue)) ; dossiers et fichiers sont privés (MEN-18). [`purge`](../raspberry-pi/src/record/retention.py) ne retire un enregistrement qu'après un dépôt confirmé hors de la machine, lié à l'empreinte de ses sommes de contrôle, puis le délai de conservation ; `RecordDeposit` écrit cette confirmation seulement après vérification de toutes les empreintes cloud et locales. Un départ est refusé, variateur non touché, sous 500 Mo ou 36 016 inodes libres sous le dossier des enregistrements, et quand la mesure est illisible ou trop ancienne ([`storage_gate`](../raspberry-pi/src/record/session.py), seuils dans [`journal.py`](../raspberry-pi/src/record/journal.py), [raspberry-pi.md, 15.4](raspberry-pi.md#154-départ-refusé-sous-500-mo)). Le journal hors séance est borné sur disque ([`Logbook`](../raspberry-pi/src/record/logbook.py), [ANH-191](https://linear.app/anheart/issue/ANH-191/boite-noire-suites-danh-128-inodes-trames-variateur-journal-hors)). Une écriture refusée en cours de séance n'arrête ni la séance ni la sécurité : elle est comptée, dite à l'opérateur et annoncée dans le battement de cœur et affichée par la carte de santé machine (ANH-148). Côté Convex, un point ou un événement n'est écrit qu'une fois, et un lot renvoyé ne change rien à ce qui est stocké (`storeTelemetry`, `storeEvents` dans [`training.ts`](../convex/training.ts)) ; cet effet dépend du redéploiement [ANH-82](https://linear.app/anheart/issue/ANH-82/redeployer-convex-avec-le-nouveau-schema-sans-casser-le-site-en). Ces sommes de contrôle ne sont pas authentifiées : elles montrent une altération accidentelle, pas une réécriture par un compte qui peut écrire le dossier. **Le Pi garde sa copie jusqu'au dépôt confirmé** : après clôture et acquittement de fin, ANH-130 envoie aussi les fichiers complets, ECG brut et trames variateur compris, puis compare l'inventaire et les empreintes avant d'autoriser la purge locale. Rien de ceci n'a été constaté sur un Pi installé.

**Manquant.** Le dépôt vérifié d'[ANH-130](https://linear.app/anheart/issue/ANH-130/depot-des-enregistrements-de-seance-dans-convex-storage-organise-par) et sa confirmation sont implémentés ; la reprise après interruption et les refus de purge sans confirmation sont testés logiciellement. La mise en service sur Pi et le contrôle en production restent à faire. Comment authentifier les empreintes (clé hors de portée du compte de la console, ou empreinte déposée hors de la machine dès la clôture) reste à décider. [ANH-163](https://linear.app/anheart/issue/ANH-163/pi-horloge-fiable-ntp-ou-rtc-journaux-persistants-et-bornes) apporte l'heure fiable et les journaux persistants bornés, dont dépend la date que porte la preuve (MEN-11). Le premier lot d'[ANH-151](https://linear.app/anheart/issue/ANH-151/durcissement-du-raspberry-pi-point-dacces-dedie-a-la-console-pare-feu) fait tourner la console sans privilèges : le compte qui écrit les enregistrements est le seul, avec root, à pouvoir les modifier, et il reste celui de la console ; ses lots suivants durcissent l'accès au système. Aucune alerte de flotte ne prévient qu'un disque se remplit ou qu'un enregistrement est dégradé. Le ticket qui porte les alertes de flotte est [ANH-150](https://linear.app/anheart/issue/ANH-150/vues-de-flotte-et-alertes-equipe-liste-filtrable-detail-machine) : lu le 10 octobre 2026, il prévoyait une alerte sous 1 Go de disque libre et une autre sur un dépôt d'enregistrement en échec, aucune pour un enregistrement dégradé dans la liste de son exigence ; depuis ce jour-là, sa section de traçabilité porte cette règle comme alerte à ajouter à cette liste. Il dépend d'[ANH-148](https://linear.app/anheart/issue/ANH-148/sante-de-la-machine-dans-le-heartbeat-temperature-disque-horloge), qui fait garder par Convex et afficher sur la fiche de la machine la santé annoncée par le battement de cœur, disque libre et `recordDegraded` compris. Aucun de ces contrôles n'est annoncé ici comme fait.

**Preuve de fermeture attendue.** Sur une machine d'essai, avec des enregistrements synthétiques : un enregistrement modifié après sa clôture, sommes de contrôle recalculées comprises, reconnu comme tel au dépôt ; un enregistrement déposé puis purgé retrouvé complet hors de la machine ; aucune purge sans dépôt confirmé ; un disque rempli produit le refus de départ attendu, et l'équipe en est prévenue avant que l'opérateur ne le rencontre.

## MEN-21 Service de simulation hébergé ouvert à Internet

- Status: OPEN
- Issues: [ANH-126](https://linear.app/anheart/issue/ANH-126/cicd-deploiement-vercel-et-convex-automatise-rapports-du-framework-de), [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite), [ANH-198](https://linear.app/anheart/issue/ANH-198/vercel-plus-de-deploiement-automatique-un-bouton-de-deploiement-pour)

F10 · Livraison (simulation hébergée) · D · Haute seulement si la perte de service est importante, c'est-à-dire si elle atteint aussi le site : cela suppose des quotas communs sur le compte d'hébergement, ce que le dépôt ne permet pas de vérifier ; sinon en deçà de l'échelle de ce modèle. Aucune machine, aucun mouvement, aucune donnée de santé lue ; personne non concernée.

**Scénario.** La simulation hébergée est un service public, sans identité, qui calcule à la demande. La classe de risque est celle de tout service ouvert qui partage ses moyens : sa consommation s'impute à un compte d'hébergement, et sa livraison passe par un jeton de déploiement. Si ce compte, ses quotas ou ce jeton sont communs au site, l'indisponibilité ou la compromission de l'un atteint l'autre.

**Existant vérifié dans le source.** [`app.py`](../deploy/simulation-vercel/app.py) n'accepte un scénario que par son nom, dans le catalogue fermé des scénarios livrés (`CATALOGUE`, `_refusal`) : aucun fichier n'est lu d'après un nom reçu. La vitesse demandée est ramenée dans un intervalle fermé (`_clamp_speed`), et les scénarios en mode `dsp` n'y tournent pas. Le processus n'ouvre ni port Modbus, ni capteur, ni lien vers Convex : tout ce qu'un scénario touche est un modèle, et la bibliothèque de rejeu que son build copie n'y est pas lue (MEN-19). Dans [`deploy-production.yml`](../.github/workflows/deploy-production.yml) et [`deploy-preview.yml`](../.github/workflows/deploy-preview.yml), le site et la simulation sont deux projets d'une même équipe d'hébergement, déployés avec le même jeton ([deploiement.md, 5.5](deploiement.md#55-réglages-à-faire-une-fois-à-la-main)). Les quotas, la facturation et les protections du compte d'hébergement sont des réglages hors du dépôt : ils n'ont pas été inspectés.

**Manquant.** À décider : la séparation entre le site et la simulation, pour les jetons de déploiement (un jeton par projet, ou un jeton commun assumé par écrit) et pour les quotas (comptes ou équipes distincts, ou plafonds par projet). [ANH-198](https://linear.app/anheart/issue/ANH-198/vercel-plus-de-deploiement-automatique-un-bouton-de-deploiement-pour) a écrit la livraison du site et de la simulation, deux boutons lancés à la main et un seul jeton pour les deux projets, et n'a pas tranché cette séparation ; [ANH-126](https://linear.app/anheart/issue/ANH-126/cicd-deploiement-vercel-et-convex-automatise-rapports-du-framework-de) garde le déploiement de Convex, les environnements et le lien entre la livraison et les gates ; [ANH-153](https://linear.app/anheart/issue/ANH-153/dependances-secrets-authentification-forte-et-audit-de-securite) l'inventaire et la rotation des secrets : l'inventaire écrit ([secrets.md](secrets.md)) constate le jeton commun aux deux projets et aux deux environnements GitHub, et ne tranche pas. Le partage réel des quotas se constate dans les réglages du compte d'hébergement, pas dans le dépôt. Aucune séparation n'est annoncée ici comme faite.

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
invalide, ticket absent du registre ou lien ne correspondant pas au ticket. Un
ticket compte dès que cette page le nomme, dans un champ Issues ou ailleurs, et
chaque lien Linear de la page est comparé au registre, où qu'il soit.
Il compare les liens au [registre Linear vérifié](../scripts/ci/men-linear-issues.tsv)
(clé, UUID, date de lecture, URL canonique), constitué par lectures réelles
`get_issue`. Chaque ligne porte sa date de lecture, la plus récente quand le
ticket a été relu : 2026-10-10 pour 31 lignes, 2026-10-14 pour 14 lignes,
2026-10-15 pour 1 ligne.
La revue de release du 10 octobre 2026 n'avait relu
aucun ticket ; la relecture a suivi le même jour, et son résultat, ticket par
ticket, est dans les preuves de la PR qui a redaté le registre. Les 32 tickets
déjà inscrits, lus jusque-là les 5, 6 et 8 octobre 2026, ont été relus un par
un, description et commentaires : identifiant, UUID et adresse sont ceux du
registre, aucun n'est supprimé ni fusionné. Six tickets que cette page nommait
sans qu'ils soient au registre ont été lus le même jour et y sont inscrits :
ANH-98, ANH-103, ANH-104 et ANH-105 (décisions et analyses séparées, citées en
tête, dans MEN-04 et dans MEN-14), ANH-135 (MEN-08) et ANH-136 (le ticket de ce
modèle). ANH-148, que MEN-20 nomme depuis ce jour-là, a été lu et inscrit avec
eux, et ANH-198, lu ce jour-là lui aussi, y entre avec son inscription dans le
champ Issues de MEN-21. Rien ne refusait alors un ticket nommé hors d'un champ
Issues sans être au registre : entre le 10 et le 14 octobre 2026, cinq ont été
nommés ainsi dans cette page, ANH-180 (MEN-08), ANH-186 (MEN-03 et MEN-07), ANH-187 (MEN-03),
ANH-223 (MEN-05) et ANH-224 (MEN-14 et MEN-18). Ils ont été lus dans Linear le
14 octobre 2026 (identifiant, UUID, adresse et description) et sont inscrits
au registre ; les deux liens vers ANH-186 portaient une adresse tronquée,
remplacée par celle que Linear rend. Le même jour, neuf tickets déjà inscrits
ont été relus, description seule, pour constater leur section « Traçabilité
des menaces » (paragraphe suivant) : identifiant, UUID et adresse n'ont pas
changé, et leur ligne porte cette date. Le validateur refuse désormais tout ticket
nommé par cette page et absent du registre. Le fichier de tests vérifie que la
phrase qui donne ces dates nomme celles que porte le registre, avec le nombre
de lignes de chacune, et aucune autre. Aucun jeton Linear n'est requis en CI.
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
revérifiés par lecture de chaque ticket dans Linear. À la relecture du
10 octobre 2026, ces 27 tickets portaient toujours leur section « Traçabilité
des menaces » ; aucune ne citait une menace que cette page n'attribue pas au
ticket, et aucun ticket, description et commentaires lus, ne disait fermer ou
accepter une menace. Les attributions faites depuis le 5 octobre n'y étaient
pas reportées : dix tickets ne citaient pas une menace que cette page leur
attribue. Le responsable de l'intégration y a écrit le texte manquant le même
jour, et la description de chacun a été relue ensuite. Six sections nomment
désormais, dans leur phrase « Menaces concernées » : pour ANH-126, MEN-06,
MEN-16 et MEN-21 ; pour ANH-130, MEN-09, MEN-18 et MEN-20 ; pour ANH-150,
MEN-13 et MEN-20 ; pour ANH-151, MEN-04, MEN-05, MEN-14, MEN-15, MEN-18 et
MEN-20 ; pour ANH-153, MEN-06, MEN-07, MEN-16 et MEN-21 ; pour ANH-163,
MEN-11, MEN-13 et MEN-20. Chacune se termine par une ligne datée du 10 octobre
2026, qui nomme la menace ajoutée et renvoie à la preuve de fermeture de sa
fiche. Quatre tickets qui n'avaient pas la section la portent : ANH-129
(MEN-11), ANH-172 (MEN-18 et MEN-19), ANH-177 (MEN-08 et MEN-17) et ANH-191
(MEN-18 et MEN-20). Dans ANH-150, la liste des alertes de l'exigence n'a pas
changé : la règle d'un enregistrement dégradé est écrite dans la section, comme
alerte à ajouter à cette liste. D'après ces lectures du même jour, les dix
tickets après l'écriture et les autres avant, chaque ticket d'un champ Issues
cite dans sa section chaque menace que cette page lui attribue, sauf un :
ANH-198, inscrit ce jour-là dans le champ Issues de MEN-21, n'avait pas de
section à sa lecture, et le texte lui a été demandé. Le relecteur indépendant
de la PR qui a redaté le registre l'a lu ensuite, le même jour : la section y
était depuis 21 h 59 UTC et nommait MEN-21
([avis](https://github.com/anheartpro-byte/Anheart/pull/72#issuecomment-6102747237)).
Le 14 octobre 2026, les descriptions d'ANH-126, ANH-130, ANH-150, ANH-151,
ANH-153, ANH-163, ANH-172, ANH-191 et ANH-198 ont été relues : chacune porte
toujours sa section, qui nomme les menaces dites ci-dessus, et MEN-21 pour
ANH-198 ; les champs Issues de cette page n'ont pas changé depuis le
10 octobre. Les autres tickets des champs Issues n'ont pas été relus ce
jour-là. Cette traçabilité ne ferme
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

# Sécurité : ce qui est garanti, ce qui ne l'est pas

Cette page est une synthèse honnête. Elle ne remplace ni une analyse de risques
ni une validation médicale ; aucune des deux n'existe encore. Les détails
techniques sont dans [raspberry-pi.md](raspberry-pi.md) (superviseur, défauts
variateur, arrêt) et [framework-de-test.md](framework-de-test.md) (comment c'est
vérifié). Les termes sont définis dans le [glossaire](glossaire.md).

[Retour au sommaire](README.md)

Le [modèle de menaces logiciel](menaces.md) complète cette synthèse avec les
attaques STRIDE, leurs limites de protection et les tickets de traitement.
Il distingue explicitement le source inspecté d'une protection déployée.
Sa revue datée, attribuée et indépendante est exigée à chaque jalon et avant le
pilote par la [check-list de revue de release](release-threat-review.md).
Les tickets de sécurité citent les identifiants MEN qu'ils contribuent à fermer ;
une fermeture exige des preuves, et une acceptation une justification signée.

---

## 1. En une phrase

Le logiciel du Pi applique une chaîne de sécurité stricte et testée, mais **tout
a été validé en simulation seulement**. Personne n'a encore tourné dans la
machine avec ce logiciel, et la machine n'a **aucune sécurité matérielle
indépendante** du logiciel et du variateur.

## 2. Ce que le logiciel garantit

« Garanti » veut dire ici : écrit dans le code, couvert par des tests (100 % des
branches sur la chaîne de sécurité, tests de propriétés `hypothesis`) et vérifié
dans la simulation (64 scénarios, 199 pannes injectées, cohorte de 30
personnes). Pas sur la vraie machine avec une personne à bord.

| Garantie | Où |
|---|---|
| **La sécurité passe avant la régulation.** Le superviseur rend son verdict sans voir la demande du régulateur, et le verdict le plus grave l'emporte toujours. | `src/training/safety.py`, `runtime.py` |
| **Pas d'accélération sur un malaise.** La consigne ne peut pas monter tant que la pente de la fréquence cardiaque est sous −20 bpm/min, ou inconnue. | garde vasovagale, `runtime.py` |
| **Une consigne revenue à 0 en cours de séance ne remonte jamais seule.** Un verdict verrouillé ne se lève que par un acquittement nominatif ; aucun réarmement automatique de défaut. GO_SILENT ne s'acquitte jamais dans le même processus. Un avertissement non verrouillé (FREEZE, REDUCE) se lève seul, et la consigne suit alors de nouveau la régulation ou la cible sans clic, tant que le bras tourne. Mais dès que le bras a tourné dans une séance, tout retour de la consigne à 0 que personne n'a demandé, par un avertissement ou par la régulation cardiaque, termine la séance sur un verrou (`session_standstill`). En séance manuelle, aucune cible n'attend sur un bras à l'arrêt : tant que quelque chose y retient une montée (un verdict, verrouillé ou non ; avec une personne à bord, une fréquence cardiaque inutilisable, de tendance inconnue ou en baisse rapide ; un premier pas que le variateur n'a pas confirmé), une cible non nulle est refusée et celle déjà saisie est remise à 0, de sorte que ni un avertissement qui se lève, ni un acquittement, ni une fréquence cardiaque qui revient ou se stabilise ne mettent le bras en mouvement. Reste possible sans clic à cet instant : le premier mouvement d'un programme après sa BASELINE ([décisions des 5 et 6 octobre 2026](#7-décisions-des-5-et-6-octobre-2026-sur-les-reprises-automatiques), avec en 7.4 la liste exacte de ce que les règles ne couvrent pas et en 7.6 la règle des cibles manuelles). | `safety.py`, `runtime.py` |
| **Une séance qui dépasse sa durée est arrêtée ; une séance finie n'est plus jugée.** `session_overrun` termine, sur un verrou, une séance encore en cours 30 s après sa durée prévue. Une fois la séance finie, la règle ne juge plus, aussi longtemps que la console reste au repos : rien ne se verrouille seul après une séance, et un nouveau départ ne demande pas de redémarrer la console ([section 8](#8-une-séance-finie-nest-plus-jugée-sur-sa-durée-anh-181)). | `safety.py`, `runtime.py` |
| **Un arrêt demandé descend toujours.** STOP à la console, arrêt demandé depuis le site, cible manuelle remise à 0 : la consigne descend aux limites de mouvement dès le cycle suivant, qu'un avertissement FREEZE tienne ou non, verrouillé ou non, et un FREEZE qui apparaît pendant la descente ne la fige pas. Sans arrêt demandé, FREEZE tient la consigne comme avant ; les verdicts plus sévères décident toujours en premier ([7.7](#77-un-arrêt-demandé-descend-toujours-même-sous-freeze-anh-175)). | `runtime.py` |
| **Attestation du câblage E-STOP** à chaque démarrage du processus, par un opérateur nommé, avant tout mouvement. | `confirm_estop_wiring` |
| **L'état du variateur est lu, jamais supposé.** Un variateur trouvé en marche (laissé par un processus planté) est arrêté et verrouillé (`drive_precommanded`), même console au repos. | `runtime.py` |
| **Aucun chemin de sortie ne laisse le moteur commandé** : fin normale, arrêt distant, perte du BITalino, exception, SIGTERM, perte de liaison. Après chaque sortie : arbre à 0, pas de couple, LFRD à 0 là où une trame peut passer. | tests d'invariants, simulation |
| **Le keepalive part en premier à chaque cycle.** Si la boucle cale, le variateur ne reçoit plus rien et son propre ttO l'arrête. | `drive.service()` en tête de tic |
| **Les écritures sont vérifiées.** LFRD est relu ; une écriture qui n'atterrit pas donne `setpoint_unconfirmed`, puis GO_SILENT si l'arbre ne suit pas non plus. | `safety.py`, `tracking.py` |
| **Une FC n'est utilisée que si elle est fiable.** Deux traitements ECG indépendants doivent être d'accord à 5 bpm près sur la même fenêtre continue. Sinon la FC est « périmée » : FREEZE à 10 s, REDUCE à 30 s, RAMP_DOWN à 60 s. | `ecg_pipeline.py`, règle `hr_stale` |
| **Les 66 défauts du variateur** (plus un code inconnu) mènent tous à un arrêt contrôlé, avec le code affiché. Le réarmement est refusé pour les 40 défauts non réarmables et pour un code inconnu. | `motor/drive.py`, table LFT |
| **Jamais d'arrêt plus rapide que la rampe.** QUICK_STOP met la consigne à 0 mais garde l'ordre de marche : le variateur freine sur sa rampe réglée, ce qui évite ObF et la roue libre. | `motor/atv320.py` |
| **Limites anti-nausée** (0,25 tr/min/s au bras, 0,03 g/s) appliquées à toute variation non urgente, manuelle comme programmée, avec la variation de g jugée au bout des pieds (`LEG_TIP_RADIUS_M`). | `training/motion.py` |
| **Le tableau de bord ne peut ni arrêter la machine en tombant en panne, ni lancer une séance manuelle.** Il ne peut envoyer que deux choses : un lancement AUTO (qui repasse par toutes les portes de la console) et une demande d'arrêt (exécutée sur la rampe normale). | `cloud_sync.py` |
| **Un tableau de bord d'un autre contrat n'arme rien.** Un lancement venu d'une réponse dont la majeure de contrat n'est pas celle de la console, ou qui n'annonce aucune version, n'atteint jamais la boîte aux lettres : la console l'affiche et le renvoie comme séance échouée. Dans l'autre sens, Convex refuse (426) toute requête d'une machine dont il ne sert pas la majeure, sauf celle qui porte la demande d'arrêt : un arrêt demandé au tableau de bord, ou une séance que le serveur ne tient plus pour active, arrête la séance en cours quelle que soit la version des deux côtés. C'est tout ce que la console retient d'une réponse d'une autre majeure : le pire qu'une telle réponse puisse faire est un arrêt ordinaire. | `contract.py`, `cloud_sync.py`, `convex/lib/contract.ts` |
| **Âge minimum** : une séance programmée exige un âge connu et d'au moins `MIN_RIDER_AGE` (18 par défaut). Le site et la console refusent tous les deux. | `local_panel.py`, `convex/training.ts` |
| **Réglages prudents par défaut** : personne à bord refusée (`OCCUPANCY_OCCUPIED_ENABLED=false`), séances programmées désactivées (`PROGRAMS_ENABLED=false`), plafond 300 tr/min moteur (≈ 6 tr/min au bras). | `.env.example`, `local_config.py` |

## 3. Ce que le logiciel ne garantit PAS

| Limite | Conséquence |
|---|---|
| **STO ponté.** L'entrée Safe Torque Off du variateur est pontée. | Aucun moyen indépendant de couper le couple. L'arrêt le plus rapide est une rampe de 3 à 4 s. L'E-STOP de la page web est un arrêt logiciel, pas un arrêt d'urgence certifié. Attention : l'attestation exigée au démarrage (`ESTOP_ATTESTATION`) affirme qu'un coup-de-poing est câblé sur STO et que le pont STO a été retiré. Le logiciel ne peut pas le vérifier ; sur le banc actuel, cocher ces cases serait faux. |
| **Pas de frein mécanique commandé.** La CAO montre un disque de frein, mais aucun frein n'est commandé par le logiciel ni documenté comme installé. | Si le variateur passe en roue libre (par exemple sur ObF), le bras ralentit seul, en ~145 s, sans contrôle. |
| **Pas d'arrêt dans la capsule.** La personne à bord n'a aucun moyen d'arrêter la machine. | Tout arrêt dépend de l'opérateur, du superviseur ou du variateur. |
| **ttO et SLL jamais relus.** Le délai de perte de communication (ttO) et la réaction à cette perte (SLL) sont réglés dans le variateur ; le logiciel ne les vérifie pas. Le simulateur suppose ttO = 3 s. | Si SLL est réglé sur roue libre ou « ignorer », GO_SILENT ne garantit plus un arrêt sur rampe. À vérifier sur le variateur. |
| **HSP à 50 Hz.** Le code accepte jusqu'à 1380 tr/min moteur parce que le moteur est aujourd'hui désaccouplé. | HSP doit être abaissé avant d'accoupler le bras avec une personne. |
| **Caméra simulée seulement.** Les règles de présence existent et sont testées, mais seule une caméra simulée existe ; le lecteur d'un vrai détecteur (`src/presence/detector_link.py`) n'est pas écrit. | Aucune détection réelle d'intrusion dans la zone. |
| **Tout est validé en simulation.** Variateur simulé, physiologie simulée, cohorte fictive. | Le comportement réel du variateur, du bras et d'un corps humain peut différer. Aucune séance avec quelqu'un à bord n'a eu lieu. |
| **Décisions médicales en attente [MED].** Limites anti-nausée, plafond 1,2 g résultant avec personne à bord, `MIN_RIDER_AGE=18`, paliers cardiaques 148/158 bpm, zones. | Ce sont des valeurs provisoires, pas des valeurs validées. Une zone « jog » vers 150 bpm demande des paliers plus hauts que les défauts : c'est une décision médicale. |
| **Réglages d'ingénierie estimés [ING].** Gains du régulateur (Kp = 3, Ti = 40 s), rayon réel du passager. | À mesurer et régler au banc. |
| **`src/signal_processing.py` hors gate.** Il calcule la FC qui entre dans la chaîne, mais il n'est encore ni sous la couverture à 100 % ni sous la vérification de types stricte (dette déclarée dans `pyproject.toml`). | Atténué par la confirmation indépendante (`ecg_pipeline.py`), mais pas éliminé. |
| **Profils livrés = profils de banc.** `standard_30_min` et `standard_45_min` plafonnent à 276 tr/min moteur (5,5 tr/min au bras) et n'atteignent pas leur zone. | Ils se terminent proprement en saturant, mais n'entraînent personne. |
| **Site et Convex : vérifiés en développement seulement.** Le nouveau Convex tourne sur le déploiement de développement ; il y a été testé de bout en bout avec une console simulée (1er octobre 2026) et toutes les pages du site y ont été ouvertes à la main (2 octobre 2026). Il n'est pas en production, il n'y a aucun test automatisé, les formulaires n'ont pas été soumis depuis le navigateur, et plusieurs défauts restent (clé API non hachée, machine supprimée encore authentifiée, séance orpheline après un arrêt de la console, textes en anglais…). | Voir [deploiement.md](deploiement.md), [convex.md](convex.md) et le [guide du tableau de bord](guides/guide-tableau-de-bord.md#94-ce-que-les-vrais-écrans-ont-montré-2-octobre-2026). |
| **Déploiement du Pi jamais essayé sur un Pi.** L'image Docker et `scripts/anheart.service` lancent maintenant la console `src.local_panel`. L'image a été construite et essayée en simulation dans un conteneur ARM64, pas sur un vrai Pi, ni avec le variateur ou le BITalino réels. | Voir [deploiement.md](deploiement.md#7-le-raspberry-pi). Le premier démarrage sur la machine reste à faire. |

## 4. Défauts trouvés par la simulation, et corrigés

Les chiffres viennent de `simulation/README.md` (section « Findings ») et ont été
recoupés avec le code actuel. « Avant » décrit le défaut d'origine, « après » ce
que mesure la batterie aujourd'hui.

| # | Défaut | Avant | Après |
|---|---|---|---|
| 1 | Malaise vagal : le régulateur accélérait avant que `hr_drop` ne se déclenche | FC 145 → 121 bpm pendant que la consigne montait de 968 à 1039 tr/min moteur (g au bout des pieds 1,03 → 1,18) pendant ~16 s | consigne tenue à 968 tr/min pendant l'effondrement ; `hr_drop` à t = 920 s. Résiduel : voir section 5 |
| 2 | Les séances programmées ignoraient les limites anti-nausée | bras jusqu'à ~0,54 tr/min/s sur 1 s (limite 0,25) | 0 violation ; `auto_jog_150_nominal` : 0,28 tr/min/s (dans la tolérance de mesure), 0,017 g/s |
| 3 | La limite de variation de g n'était jugée qu'au rayon de référence | à 27 tr/min : 0,038 g/s au bout des pieds contre 0,023 g/s à 1,5 m | le runtime reçoit `limit_radius` ; `local_panel.py` lui passe `LEG_TIP_RADIUS_M` et la simulation le rayon du bout des pieds. Le test correspondant est un test ordinaire qui passe (le README de la simulation le dit encore « ouvert ») |
| 5 | `hr_unresponsive` se déclenchait dans chaque montée en régime | vers 350 s, REDUCE ramenait la consigne de 353 à 117 tr/min | jugé sur la charge commandée ; plus aucun déclenchement en scénario nominal |
| 6, 9 | `hr_drop` (présyncope) se déclenchait sur des personnes qui ne s'évanouissent pas | 10 sujets sur 25 finissaient en `safety_verdict` sur leur propre retour au calme ; 2 sujets ectopiques bloqués en BASELINE | 0 faux `hr_drop` dans la cohorte ; le sujet vasovagal le déclenche toujours |
| 8 | Rien ne refusait un passager mineur | S01 (10 ans) tourné à 20,9 tr/min, 1,19 g au bout des pieds | âge exigé, minimum 18 ans : les 8 sujets de 10 à 17 ans sont refusés avant tout mouvement |
| 10 | Une console au repos laissait tourner un variateur trouvé en marche | 900 tr/min moteur pendant tout le repos, sans verdict | détecté au premier relevé (t = 0,2 s), arbre 900 → 0, étage de sortie coupé |
| 11 | `tracking_error` était aveugle pendant les rampes | RFRD bloqué détecté 52 s après ; statut figé détecté 110 s après l'arrêt | 7 s ; 7 s ; 6 s dans le HOLD d'un programme |
| 12 | Écritures acquittées mais mal adressées jamais escaladées | arbre encore à 1344 tr/min 90 s après un RAMP_DOWN | `setpoint_unconfirmed` à t = 201,4 s, GO_SILENT à 207,2 s, le ttO arrête le moteur |
| 13 | L'écho LFRD était affiché mais jamais utilisé | aucun verdict, aucun message | verdict en 1,4 s, arrêt contrôlé |
| 14 | Un mot d'arrêt refusé était renvoyé indéfiniment, en silence | ~480 trames refusées, étage de sortie laissé actif à 0 tr/min | après 5 refus : SHUTDOWN (arbre confirmé à l'arrêt) et verdict `disable_refused` ; si SHUTDOWN est refusé aussi, GO_SILENT |
| 4, 15 | Le traitement ECG donnait des FC fausses notées « bonnes » (mouvement, bruit, lots perdus) | 116-146 bpm lus pour 70 réels ; 47 puis 135 pour 71 ; faux `hr_drop` | une FC n'est utilisable que confirmée par le traitement indépendant sur la même fenêtre ; les cas `ecg_dsp_corrupted` et `ecg_dsp_gaps` passent (le README de la simulation ne marque pas encore ces deux constats « FIXED ») |
| 16 | `hr_rate` prenait une seule mesure pour une tendance | 341 épisodes REDUCE parasites dans la cohorte | 0 dans la cohorte ; les vraies montées restent détectées |
| - | Table des défauts du variateur provisoire et fausse | SLF1 noté 19 au lieu de 5, OCF 15 au lieu de 9 | table complète Schneider : 66 défauts + nOF, chacun injecté en séance AUTO et MANUELLE |

## 5. Le résiduel connu

- **S07, séance jog (xfail strict).** Cohorte, femme de 48 ans sujette au malaise
  vagal. Une hausse de +12 tr/min décidée au tic exact où l'effondrement scripté
  commence, avant que le capteur ne montre la moindre baisse (123 bpm réels, 122
  lus), est encore exécutée jusqu'au bout. Rien ne monte après la première baisse
  mesurable. C'est le seul xfail strict restant dans les deux suites de tests.
  Voir [framework-de-test.md](framework-de-test.md#12-les--xfail-strict--et-le-cas-résiduel-s07).
- **Un statut variateur figé à vitesse constante** est indiscernable d'un statut
  vrai. Il n'est détecté que quand la consigne bouge (escalade vers GO_SILENT).
- **Une écriture mal adressée à vitesse constante** est indiscernable d'une
  écriture correcte ; la détection attend que la consigne bouge.
- **Personne à bord en manuel** : la FC monte assez pour déclencher `hr_rate`
  (FREEZE) avant 19 tr/min, à la rampe anti-nausée (`manual_occupied_ceiling`).
  C'est un comportement voulu, mais il limite l'usage manuel avec passager.
- **Le simulateur modélise une fermeture minimale du variateur** : après chaque
  sortie de console, le variateur simulé verrouille SLF via ttO, alors que le
  vrai `ATV320Drive.close` envoie la séquence d'arrêt. C'est une différence de
  modèle, pas un défaut.

## 6. Avant la première personne à bord

Le code ne lève « personne à bord » qu'au jalon M6, qui exige des accords écrits
d'ingénierie et médical (`local_config.py` : « jalon M6, accords ingenierie et
medical requis »). Les points ouverts relevés ci-dessus donnent la liste des
préalables :

1. Une sécurité matérielle indépendante : STO câblé sur un vrai arrêt d'urgence,
   un arrêt accessible depuis la capsule, un frein si l'analyse de risques le demande.
2. Vérifier ttO et SLL sur le variateur ; abaisser HSP.
3. Mesurer `ARM_RADIUS_M` et `LEG_TIP_RADIUS_M` sur la vraie machine et le vrai passager.
4. Faire signer toutes les valeurs [MED] par l'équipe médicale.
5. Brancher une vraie caméra (écrire `detector_link.py`) et la mettre en service.
6. Refaire les essais sur le banc réel, puis capsule vide, avant tout passager.

## 7. Décisions des 5 et 6 octobre 2026 sur les reprises automatiques

Ticket ANH-176. Les deux décisions ci-dessous sont celles du propriétaire du
produit, reprises telles qu'il les a formulées. Les mesures, la façon de les
appliquer et la liste de ce qui reste (7.2 à 7.5) sont de l'auteur du
changement et de la revue indépendante : elles n'engagent pas le propriétaire
du produit. La section 7.6 (ticket ANH-178, cibles manuelles) porte ses deux
propres décisions, du 6 octobre 2026 elles aussi, et dit qui les a prises. La
section 7.7 (ticket ANH-175, arrêt demandé sous FREEZE) corrige un défaut :
elle applique les décisions ci-dessous sans en ajouter, et dit ce qu'elle
laisse à décider.

### 7.1 Les décisions

**5 octobre 2026.** Quand un avertissement non verrouillé a ramené la consigne
à 0, la séance se termine. Deux options sont écartées : la reprise sur un geste
de l'opérateur, et la reprise automatique bornée.

**6 octobre 2026.**

1. La règle s'étend à tout arrêt en cours de séance : « un bras arrêté ne
   repart jamais seul ». Une fois que le bras a tourné dans une séance, tout
   retour de la consigne à 0 termine la séance, sur un verrou, que ce soit un
   avertissement ou la régulation cardiaque qui l'y ait amenée. Conséquence
   acceptée : la régulation ne peut plus se reposer à 0 puis reprendre.
2. Les séances manuelles capsule vide (`bench`) sont couvertes aussi.
3. Un avertissement pendant la BASELINE, avant que le bras ait bougé, reste
   hors de la règle : le premier mouvement après la BASELINE est le départ
   normal du programme.

### 7.2 Ce qui a été mesuré

Tout est mesuré sur le banc d'essai logiciel (variateur factice, horloge
manuelle, jamais sur le matériel), avec le profil livré `standard_30_min` et
les limites de la console. « Avant » est la branche `develop` avant ANH-176.

**L'électrode décollée.** La fréquence cardiaque (FC) est perdue pendant 50 s
puis revient. Personne ne clique.

| Temps depuis la perte de la FC | Avant, en tr/min moteur | Maintenant |
|---|---|---|
| 0 s | 164, séance en cours | 164, séance en cours |
| 12 s | 164, FREEZE `hr_stale` | 164, FREEZE `hr_stale` |
| 32 s | 122, REDUCE `hr_stale` | 122, REDUCE `hr_stale` |
| 42 s | 0, mode toujours « SEANCE » | 0 ; au cycle suivant (0,2 s), séance terminée, verrou `session_standstill`, mode « ARRET » |
| 50 s | la FC revient | la FC revient |
| 95 s | 69 | 0 |
| 170 s | 168 | 0 |

L'opérateur voit le bras arrêté, va à la capsule remettre l'électrode, la FC
revient, et le bras repartait à côté de lui.

**Le dernier pas pris par la régulation.** Séquence trouvée par la revue
indépendante de la première version de ce changement, qui ne la couvrait pas.
Bras à 204 tr/min moteur en HOLD, FC à 128 dans sa zone (118 à 138). L'électrode
se décolle, puis elle est remise au moment où la consigne est descendue à la
vitesse minimale ; la FC lue vaut alors 140, soit 2 au-dessus de la zone.

| Temps depuis la perte de la FC | Avant | Maintenant |
|---|---|---|
| 9,2 s | 204, FREEZE `hr_stale` | 204, FREEZE `hr_stale` |
| 29,2 s | REDUCE `hr_stale`, la consigne descend | REDUCE `hr_stale`, la consigne descend |
| 39,2 s | 55 (vitesse minimale) ; la FC revient à 140 | 55 ; la FC revient à 140 |
| 39,4 s | plus aucun verdict à l'écran | plus aucun verdict à l'écran |
| 44,2 s | 0, posé par la régulation ; mode « SEANCE », aucun verdict | 0, posé par la régulation ; au cycle suivant, séance terminée, verrou `session_standstill` |
| 269 s | la FC est repassée sous la zone : 55, puis 203 deux minutes après | 0 |

**La régulation seule, sans aucun avertissement.** La FC dérive de 100 à 145 à
15 bpm/min et y reste : au-dessus de la zone, sous le palier dur (148), trop
lentement pour `hr_rate`.

| Moment | Avant | Maintenant |
|---|---|---|
| la FC passe 138 | 158 tr/min moteur | 158 tr/min moteur |
| 117 s plus tard | 0 ; mode « SEANCE », aucun verdict, étage de sortie sous tension | 0 ; au cycle suivant, séance terminée, verrou `session_standstill`, étage de sortie retiré une fois l'arrêt lu |
| la FC redescend 150 s après l'arrêt, à 15 bpm/min | le bras repart 300 s après l'arrêt : 55, puis 133 une minute après | 0 |

**Capsule vide.** Les règles de FC sont désactivées en séance `bench` ; seul
`current_high` peut y ramener la consigne à 0, et il se lève forcément à
l'arrêt, puisque le courant retombe avec la vitesse. Mesuré avec un seuil
d'alerte abaissé pour le banc d'essai (le variateur factice ne dépasse jamais
2,4 A), cible 300 tr/min moteur : avant, REDUCE, arrêt, puis le bras remontait
seul vers sa cible, et le cycle recommençait toutes les 40 s environ ;
maintenant la séance se termine à l'arrêt, sur le verrou.

**Les séances normales.** La batterie de simulation complète (61 scénarios,
199 pannes injectées dont celles du traitement ECG réel, 90 séances de la
cohorte de 30 personnes : 350 courses) a été rejouée avant et après.

- Aucune séance nominale (sujet sain, aucune panne injectée) ne change : mêmes
  fins, mêmes règles, mêmes vitesses de pointe, même temps passé en zone. Sur
  ces séances la régulation ne ramène jamais la consigne à 0 en cours de
  séance ; le seul retour à 0 hors fin de séance est une cible 0 demandée par
  l'opérateur (`manual_ladder`), qui ne termine rien.
- Huit courses existantes changent, toutes avec une panne ECG injectée : la
  perte de FC y amène maintenant la séance sur le verrou `session_standstill`.
  Six d'entre elles (`ecg_dsp_flat`, `_saturated`, `_mains`, `_stopped`,
  `_corrupted`, `_gaps`) ne se terminaient pas avant : le bras repartait et la
  course allait au bout du scénario (`shutdown`) ; leur vitesse de pointe passe
  de 5,8 ou 6,5 tr/min au bras à 1,4 ou 1,7. Les deux autres
  (`ecg_disconnect_warmup`, `fault_bitalino_disconnect_dsp`) se terminaient
  déjà sur un verdict et gagnent seulement cette règle.
- Un scénario est ajouté : `fault_ecg_electrode_refitted_standstill`.

### 7.3 Comment c'est appliqué

- **Un seul endroit voit le retour à 0.** À la fin de l'étape de commande du
  cycle (`TrainingRuntime._command`), quel que soit le bras du verdict qui a
  écrit la consigne : si elle était non nulle et que le variateur vient
  d'acquitter un 0, le runtime note le fait et ce qui l'a produit
  (`_note_standstill`). Un 0 seulement envoyé, non acquitté, ne termine rien :
  le cycle suivant réessaie.
- **Le verdict est celui du superviseur.** Au cycle suivant, la règle
  `session_standstill` (RAMP_DOWN, verrouillée, sans délai) lit ce fait. Elle
  est donc dans le plancher verrouillé du superviseur, comme la fin verrouillée
  de `hr_stale` à 60 s : même affichage dans `/api/status` (`standing` et
  `floor`), même refus immédiat d'un départ (409) à la console, même refus d'un
  lancement venu du tableau de bord (renvoyé comme séance échouée), même
  acquittement nominatif. Entre les deux cycles rien ne peut bouger : la
  consigne est à 0, et le cycle qui pourrait la remonter est celui où le
  verdict décide. Le verrou propre au runtime reste libre : un mot d'arrêt
  refusé ensuite par le variateur est bien signalé (`disable_refused`).
- **L'acquittement tient une fois la séance finie.** Tant que la séance arrêtée
  n'est pas terminée (descente confirmée, puis récupération surveillée), la
  règle reste vraie : un acquittement donné trop tôt est repris au cycle
  suivant, comme pour `hr_stale` tant que la FC manque, et ici même si la cause
  de l'avertissement a disparu entre-temps. Dans tous les cas il ne relance
  rien, et il faut un nouveau départ.
- **Ce qui amène la consigne à 0 sans que personne l'ait demandé** : un
  REDUCE (`hr_stale` entre 30 et 60 s, `hr_rate`, `hr_unresponsive`,
  `current_high` au niveau d'alerte), ou la régulation cardiaque quand la FC
  est au-dessus de la zone. En séance programmée comme en séance manuelle, avec
  ou sans personne déclarée à bord. La phrase du verdict nomme la cause.
- **Ce qui n'est pas « seul »**, et ne lève donc pas ce verrou : un STOP de
  l'opérateur, donné sous un avertissement ou non ; une cible manuelle mise à
  0 par l'opérateur alors qu'aucun avertissement ne tient ; le retour au calme
  et la récupération du programme. Une séance déjà en train de se terminer
  garde sa propre raison de fin.
- **Lecture prudente retenue** : si l'opérateur tape une cible 0 pendant qu'un
  avertissement est en train de baisser la vitesse, l'arrêt est compté comme
  celui de l'avertissement et la séance se termine. Cela coûte un acquittement.
  Depuis ANH-175 une cible 0 est suivie aussi sous un FREEZE, qui ne baisse
  rien lui-même : la même lecture s'applique à l'arrivée à 0, et la phrase du
  verdict nomme alors la cible de l'opérateur, pas l'avertissement (7.7).
- **Ce qui continue de reprendre seul**, parce que le bras ne s'est pas
  arrêté : une vitesse seulement maintenue (FREEZE) ou baissée sans atteindre 0
  (REDUCE en cours de descente), et la régulation, qui baisse et remonte
  toujours la vitesse d'un bras qui tourne.
- **Aucun seuil, aucun délai d'une règle existante n'a changé.**
- **La phrase à l'écran.** Tant qu'un avertissement non verrouillé tient et que
  la séance peut encore prendre de la vitesse, la phrase du verdict affichée
  par la console se termine par : « NOT LATCHED: it lifts by itself when its
  cause ends, and the speed then follows the programme or the manual target
  again, upwards too, with nobody clicking ». Elle n'est pas ajoutée sur une
  séance qui se termine ou qui est finie, ni reprise dans un refus de départ.
  Elle est en anglais, comme les phrases de verdict du superviseur (celles des
  règles caméra sont en français), et n'apparaît que sur les pages Séance et
  Sécurité. Elle reste affichée, telle quelle, sur un
  bras tenu à l'arrêt en séance manuelle : la cible qu'elle dit « suivie de
  nouveau » y vaut alors 0 (voir 7.6), donc rien n'y remonte sans une nouvelle
  cible. Elle en dit là plus qu'il n'y a à craindre, jamais moins.

### 7.4 Ce que la règle ne couvre pas

Dans ce cas le bras peut encore quitter l'arrêt sans clic à cet instant.

1. **Avant le premier mouvement d'une séance programmée** (décision du 6
   octobre, point 3). Mesuré : aucune FC pendant les 45 premières secondes de
   BASELINE (électrodes pas encore posées) donne FREEZE à 10 s puis REDUCE à
   30 s, la consigne étant à 0 depuis le départ. La FC arrive, l'avertissement
   se lève, le programme continue, et le bras fait son premier mouvement
   pendant WARMUP (81 tr/min moteur à 285 s). La phrase ci-dessus reste
   affichée tant que l'avertissement dure. Ce premier mouvement est aussi
   retenu, sans verdict, par la garde vasovagale et par un état du variateur
   illisible (`_follow_controller`), et il a lieu quand la retenue disparaît :
   c'est lu dans le code, pas mesuré ici, et c'est le même cas.

**Ce qui n'est plus dans cette liste.** Une cible manuelle saisie sur un bras à
l'arrêt y figurait deux fois : derrière un verdict, puis derrière la fréquence
cardiaque sans aucun verdict (FC inutilisable, ou en baisse de plus de
20 bpm/min, garde vasovagale). Les deux sont fermés par ANH-178 (7.6) : une
telle cible est refusée, ou retirée si elle était déjà saisie. En séance
manuelle, il ne reste entre une cible acceptée et le premier pas que le temps
du profil de mouvement, qui ne dépend que du temps : avec les limites livrées
et le cycle de 0,2 s, le pas est écrit au premier cycle qui suit, ou au second
quand le profil n'a pas encore de base de temps (premier cycle de la séance,
cycle qui suit un FREEZE), soit 0,4 s au plus.

**Le bandeau à l'écran.** Depuis le 7 octobre 2026 (ANH-182), la page de la
console le dit en français et sur toutes les pages : un bandeau orange
`REPRISE AUTOMATIQUE POSSIBLE`, affiché tant qu'un `freeze` ou un `reduce` non
verrouillé tient ou baisse une vitesse qui peut encore remonter (mode `SEANCE`
ou `MANUEL`, phase `baseline`, `warmup` ou `hold`, et en manuel une cible
au-dessus de la consigne). Conditions et rejeu dans un navigateur :
[console-locale.md](console-locale.md#11-page--securite-). La phrase anglaise
ci-dessus reste dans le détail des verdicts ; elle y figure encore sur un bras
manuel tenu à l'arrêt, où la cible vaut 0 et où plus rien ne peut remonter.

### 7.5 Ce que cela coûte

- Près de la vitesse minimale, quelques secondes d'un REDUCE suffisent à
  terminer la séance : la descente n'a plus que le dernier pas à faire.
- Une FC qui reste au-dessus de la zone assez longtemps pour que la régulation
  ramène la consigne à 0 (environ deux minutes dans la mesure ci-dessus)
  termine la séance, alors qu'avant le bras attendait à l'arrêt que la FC
  redescende. C'est la conséquence acceptée le 6 octobre. Aucune des 90 séances
  de la cohorte simulée ni aucun scénario nominal n'est dans ce cas, mais ces
  séances sont simulées : la fréquence réelle de ce cas reste à observer.
- Chaque fin de ce type demande un acquittement nominatif avant le départ
  suivant.

### 7.6 Aucune cible manuelle n'attend sur un bras à l'arrêt (ANH-178)

**Les décisions.**

- **6 octobre 2026.** Le propriétaire du produit a retenu la recommandation de
  la revue indépendante d'ANH-176 : refuser une cible manuelle tant qu'un
  verdict tient un bras à l'arrêt.
- **6 octobre 2026**, décision prise par le coordinateur sur délégation du
  propriétaire du produit, qui suit la recommandation de la revue pour les
  verdicts et l'étend à l'attente de la fréquence cardiaque : un bras à l'arrêt
  ne part que sur une cible saisie alors que rien ne le retient.

Le reste de cette section (mesures, application, limites, coûts) est de
l'auteur du changement. Il a appliqué le même principe à une troisième retenue
trouvée à la lecture du code, le premier pas que le variateur ne confirme pas ;
c'est signalé comme tel plus bas. Le coordinateur a accepté cette troisième
retenue le 6 octobre 2026, capsule vide comprise, après la revue indépendante
de la PR #18.

**Ce qui se passait.** Une cible est une destination ; tant qu'une montée est
retenue, elle n'est pas suivie. Tapée sur un bras à l'arrêt, elle attendait, et
le bras partait seul quand la retenue disparaissait. Tout est mesuré sur le
banc d'essai logiciel, en séance manuelle.

*Derrière un verdict* (mesures de la revue indépendante, « avant » = `develop`
avant ANH-178) :

| Séquence | Avant | Maintenant |
|---|---|---|
| L'opérateur met la cible à 0, le bras s'arrête, la séance continue. La FC est perdue (FREEZE `hr_stale`). Il tape 200 tr/min moteur. | Cible acceptée. Trente secondes sans mouvement, la FC revient : première consigne non nulle 0,2 s plus tard, sans clic à cet instant. | Cible refusée, le refus nomme `hr_stale`. La FC revient : rien ne bouge. |
| Séance manuelle qui n'a pas encore bougé, même avertissement, même cible. | Acceptée ; premier mouvement 3,2 s après le retour de la FC pour une séance démarrée sur une seule lecture de FC (le cas mesuré alors), 0,2 s quand six lectures précèdent le départ. | Refusée ; rien ne bouge. |
| FREEZE verrouillé (`loop_stall`) sur un bras que l'opérateur a arrêté ; une cible est tapée, puis le verdict est acquitté. | Acceptée ; le bras part 0,4 s après l'acquittement : acquitter relançait. | Refusée ; l'acquittement ne met rien en mouvement. |

*Sans aucun verdict* (mesures de l'auteur ; « avant » = la première version de
ce changement, qui ne jugeait que les verdicts, et `develop`, identiques sur ce
point). Personne déclarée à bord pour les trois premières lignes ; aucun
verdict à aucun moment :

| Séquence | Avant | Maintenant |
|---|---|---|
| Six secondes sans lecture de FC, ce qui n'est pas encore un avertissement. 200 tr/min moteur sont tapés ; la lecture revient deux secondes après. | Cible acceptée ; première consigne non nulle 0,2 s après le retour de la lecture. | Refusée : « pas de frequence cardiaque utilisable ». La lecture revient : rien ne bouge. |
| L'opérateur arrête le bras ; la FC redescend de 130 à 80 à 30 bpm/min, comme après un effort. La cible est tapée dix secondes après le début de la baisse. | Acceptée ; elle attend 91 s, puis le bras part quand la baisse s'arrête (66 s pour une baisse de 140 à 90 à 40 bpm/min). | Refusée : « la frequence cardiaque baisse trop vite ». La baisse s'arrête : rien ne bouge. |
| Premières secondes d'un ECG : une seule lecture, ce qui suffit au départ, puis une cible. | Acceptée ; le bras part 3,2 s plus tard, à la cinquième lecture. | Refusée : « tendance de la frequence cardiaque pas encore connue ». Rien ne bouge. |
| Le variateur confirme le maintien de liaison mais pas le premier pas (capsule vide ou non). | Cible gardée ; le premier pas est redemandé à chaque cycle (100 fois en 20 s) sans qu'aucun verdict ne se lève, et le bras part 0,2 s après la première confirmation. | Le pas est demandé une fois ; la cible est remise à 0 et la console le dit. Le variateur confirme de nouveau : rien n'est demandé. |

Dans tous les cas, une cible tapée ensuite, quand plus rien ne retient, est
acceptée et suivie.

**La règle**, en deux moitiés :

- **Refus.** Tant que quelque chose retient une montée et que la consigne
  appliquée vaut 0, une cible non nulle est refusée. Le message nomme ce qui
  retient et dit quoi faire (tableau ci-dessous). Une cible de 0 est toujours
  acceptée.
- **Retrait.** Une cible non nulle déjà saisie est remise à 0 au cycle où
  quelque chose retient le bras à l'arrêt. Elle n'est donc pas suivie quand la
  retenue disparaît ; l'opérateur la retape. La console le dit une fois :
  « cible de 200 tr/min moteur remise a 0 : … », avec la même raison.

À la fin de chaque cycle : **si la consigne appliquée vaut 0 et que quelque
chose retiendrait une montée, la cible vaut 0.** Un bras à l'arrêt ne reçoit
donc de vitesse que d'une cible saisie alors que rien ne le retient.

**Ce qui peut retenir une montée sur un bras à l'arrêt**, en séance manuelle.
Liste établie en lisant toutes les conditions sous lesquelles
`_follow_manual`, et ce qu'il appelle, n'élève pas la consigne :

| Ce qui retient | Refus à la saisie | Retrait au cycle | Ce que dit la console, puis « puis redonner la cible » |
|---|---|---|---|
| Un verdict, verrouillé ou non (FREEZE, REDUCE, et tout verdict de fin) | oui | oui | « le verdict hr_stale tient le bras a l'arret. Attendre qu'il soit leve » (verrouillé : « L'acquitter une fois sa cause levee ») |
| Personne à bord, pas de FC utilisable (aucune lecture fiable depuis plus de 4 s) | oui | oui | « pas de frequence cardiaque utilisable, rien ne monte depuis l'arret. Attendre une frequence cardiaque fiable » |
| Personne à bord, tendance de la FC inconnue (moins de 5 lectures depuis le début de l'historique : premières secondes de l'ECG, ou après un saut confirmé) | oui | oui | « tendance de la frequence cardiaque pas encore connue, rien ne monte depuis l'arret. Attendre quelques secondes de lecture » |
| Personne à bord, FC en baisse de plus de 20 bpm/min sur les 5 dernières lectures (garde vasovagale) | oui | oui | « la frequence cardiaque baisse trop vite, rien ne monte depuis l'arret. Attendre qu'elle se stabilise » |
| Le variateur n'a pas confirmé l'écriture du premier pas (troisième retenue, trouvée à la lecture du code) | non : rien ne permet de le prévoir | oui, au cycle où le pas est demandé | « le variateur n'a pas confirme la consigne, elle n'est pas redemandee. Verifier la liaison » |

Le message du variateur ne dit pas que le bras est resté à l'arrêt : si la
trame est arrivée et que seule sa réponse s'est perdue, le variateur garde ce
pas pendant un cycle (0,2 s), jusqu'au zéro du maintien de liaison suivant. Le
runtime ne peut pas distinguer les deux cas.

Ne sont pas des retenues : une séance qui se termine (cible refusée, comme
avant) ; une cible hors domaine (refusée, comme avant) ; le profil de
mouvement, qui écrit le premier pas au premier cycle après la cible, ou au
second quand il n'a pas encore de base de temps (0,4 s au plus avec les limites
livrées, voir 7.4). Le plafond d'un REDUCE est un verdict : première ligne.

**Comment c'est appliqué.**

- Dans le runtime, parce que c'est le seul point par où une cible manuelle
  arrive : `TrainingRuntime.set_manual_target`. La page l'envoie, la boîte aux
  lettres de la console la prend (202), la boucle la remet au runtime au cycle
  suivant ; la simulation appelle la même méthode. Le tableau de bord ne sait
  pas envoyer de cible. Un refus revient à la page comme un événement
  `refused`, comme pour une cible hors domaine.
- Le refus juge un verdict sur ce que le dernier cycle a laissé en vigueur,
  seule chose connue entre deux cycles : un verdict sur le point de se lever
  refuse encore. La fréquence cardiaque, elle, est lue à l'instant de la
  saisie. Quand un verdict et la FC retiennent tous les deux, c'est le verdict
  qui est nommé.
- Le retrait est fait à la fin de l'étape de commande de chaque cycle
  (`_withdraw_waiting_target`), quel que soit le verdict et quel que soit le
  bras ; sans verdict, il lit ce que le suivi de la cible vient de constater à
  ce même cycle (`_follow_manual`) : la garde de la FC, ou une écriture non
  confirmée. Ce qui apparaît entre la saisie et le premier pas est donc
  l'affaire du retrait. Aucun chemin ne passe entre les deux : pour qu'un cycle
  suive une cible depuis l'arrêt, il faut qu'elle ait été saisie alors que rien
  ne retenait, et que rien ne retienne à ce cycle.
- La garde de la FC est énoncée une seule fois (`_heart_rate_hold`), lue par le
  refus et par le cycle. Sa décision est celle d'avant, inchangée : FC
  utilisable, et garde vasovagale ouverte (le test que passe aussi la montée
  d'un programme). Les trames envoyées au variateur sont les mêmes qu'avant ;
  seule la cible change.
- **Rien ne change pour un bras qui tourne** : une cible tapée pendant qu'un
  FREEZE, un REDUCE, la garde de la FC ou un variateur qui ne confirme pas
  retiennent la montée d'un bras en mouvement est acceptée, gardée, et suivie
  quand la retenue disparaît, comme avant.
- Au cycle où un avertissement vient lui-même d'amener la consigne à 0, la
  cible est remise à 0 sans message : la séance se termine au cycle suivant
  (`session_standstill`) et cette fin dit tout. Entre les deux, une cible est
  refusée au nom de l'avertissement.
- Un départ manuel ne porte jamais de cible : la séance qu'il arme a une cible
  de 0, et il est refusé tant qu'un verdict tient.
- Capsule vide (`bench`) : la FC n'y retient rien, comme avant. Restent les
  verdicts et le variateur.
- Aucun seuil, aucun délai, aucune règle du superviseur n'est modifié.

**Le départ normal avec une personne à bord.** L'ECG tourne avant le départ :
quand l'opérateur tape sa première cible, la FC est utilisable et sa tendance
connue. Si elle est stable, la cible est acceptée et le premier pas est écrit
au cycle suivant, ou au second : c'est vérifié sur le runtime avec les limites
de la console, sur la console réelle avec le sujet simulé, et par les deux
scénarios de simulation avec une personne à bord, qui ne changent pas. Le
départ lui-même reste refusé sans FC utilisable, comme avant. Si la première
cible est refusée, l'opérateur lit la raison dans la liste des événements,
attend ce qu'elle dit d'attendre (une lecture fiable, quelques secondes de
lecture, une FC stabilisée) et retape la cible : rien d'autre à faire, aucun
acquittement.

**Ce que l'opérateur voit.** La page existante affiche déjà les événements
`refused` avec leur texte (en orange, dans la liste des événements) et la
« cible appliquee » lue dans l'instantané : elle montre donc le refus, et la
cible revenue à 0 avec le brouillon resté en orange. Trois défauts restaient
dans la page ; ils sont corrigés depuis le 7 octobre 2026 (ANH-182). Après
« Appliquer », la note de la carte dit « cible envoyee … pas encore prise par
la machine », puis ce que la boucle a répondu : « cible prise par la
machine », ou « refus de la machine », suivi du message du refus ou du
retrait, en rouge. Et avec une personne à bord, un encadré « MONTEE RETENUE
PAR LA FREQUENCE CARDIAQUE » dit avant de taper que la FC retient la montée,
et pourquoi : la console donne elle-même cette garde à la page, qui ne la
recalcule pas. Détail : [console-locale.md](console-locale.md#6-page--tableau-de-bord-).

**Ce que cela coûte.**

- Après un avertissement passager sur un bras à l'arrêt, l'opérateur doit
  retaper sa cible une fois l'avertissement levé. Une cible tapée dans le cycle
  (0,2 s) où un verdict se lève peut être refusée : il la retape.
- **La garde vasovagale se ferme aussi sur des variations ordinaires de la
  FC.** Elle juge la pente des 5 dernières lectures, une par seconde, contre
  20 bpm/min : deux battements de moins en cinq secondes suffisent. Avant, une
  telle fermeture retardait le premier pas de quelques secondes ; maintenant
  elle refuse la cible, que l'opérateur retape. Estimation, pas mesure : FC
  stable plus un bruit gaussien indépendant à chaque lecture, arrondi au
  battement, la garde est fermée 3 % du temps pour un écart-type de 0,5 bpm,
  14 % pour 1 bpm, 29 % pour 2 bpm (la revue indépendante retrouve 2,7 %,
  14,2 % et 29,0 % avec le tracker du runtime). Sur l'ECG synthétique de la simulation
  passé par le vrai traitement (trace du scénario `auto_jog_150_dsp`, une
  lecture par seconde), elle n'est jamais fermée au repos, où ce signal n'a
  aucune variabilité, et elle l'est sur 6 lectures sur 116 (5 %) pendant le
  palier d'effort, où la FC lue bouge d'un ou deux battements. **La fréquence
  réelle de ces refus est à mesurer avec un vrai ECG avant la première séance
  manuelle avec une personne à bord.** Le seuil et la fenêtre de la garde ne
  sont pas modifiés.
- Une seule trame perdue au moment du premier pas retire la cible, alors
  qu'avant le pas était redemandé 0,2 s plus tard. L'opérateur la retape.
- Aucune séance simulée n'est concernée : les 350 courses de la batterie
  donnent le même résultat avant et après (mêmes fins, mêmes règles, mêmes
  vitesses de pointe), y compris les deux scénarios manuels avec une personne
  à bord.

### 7.7 Un arrêt demandé descend toujours, même sous FREEZE (ANH-175)

Ticket
[ANH-175](https://linear.app/anheart/issue/ANH-175/stop-operateur-sans-effet-tant-quun-verdict-freeze-est-en-cours-la),
7 octobre 2026. C'est la correction d'un défaut. Aucune décision du
propriétaire du produit n'est ajoutée ici : les choix faits en l'appliquant
sont de l'auteur du changement, et les deux points qui demandent une décision
sont listés à la fin.

**Ce qui se passait.** Un FREEZE maintient la consigne. Le runtime la
maintenait aussi quand l'opérateur demandait l'arrêt : la demande était
enregistrée, le mode passait à « ARRET », la phase à `cooldown`, la page
affichait une rampe et une heure d'arrivée, et le bras continuait à la même
vitesse tant que le FREEZE durait. Jusqu'à 20 s sous `hr_stale` (du FREEZE des
10 s au REDUCE des 30 s), sans limite sous un FREEZE verrouillé (`loop_stall`,
alerte venue d'un autre fil). Seul E-STOP agissait.

**La règle.** Un FREEZE tient une vitesse, jamais un arrêt. Dès qu'un arrêt a
été demandé sur un bras qui tourne, la consigne descend vers 0 aux limites de
mouvement, sous un FREEZE exactement comme sans verdict, à partir du cycle
suivant. Est un arrêt demandé :

- une fin de séance en cours, quelle qu'en soit l'origine : STOP à la console,
  arrêt demandé depuis le site (les deux arrivent par la même boîte aux
  lettres), limite d'une heure d'une séance manuelle, verdict de fin acquitté
  avant que le bras soit arrêté ;
- une cible manuelle remise à 0.

Sans arrêt demandé, rien ne change : le FREEZE tient la consigne, y compris
devant une cible manuelle plus basse mais non nulle, et la reprise se fait
comme avant. REDUCE, RAMP_DOWN, QUICK_STOP et GO_SILENT décident toujours en
premier, chacun dans sa branche.

**Ce qui a été mesuré.** Tout est mesuré sur le banc d'essai logiciel
(variateur factice, horloge manuelle), jamais sur le matériel. « Avant » est
la branche `develop` au commit `96061d0`. Vitesses en tr/min moteur, temps
comptés depuis la demande d'arrêt. Les trois premières lignes sont sur le banc
accéléré des tests (programme court, limites de mouvement élargies), les deux
dernières avec les limites de mouvement livrées.

| Séquence | Avant | Maintenant |
|---|---|---|
| Séance programmée, aucun verdict (témoin), STOP à 165 | 152 au cycle suivant (0,2 s), 55 à 2,0 s, 0 à 2,4 s | identique |
| Séance programmée, FC perdue depuis 12 s (FREEZE `hr_stale`), STOP | 165 tenu pendant 17,2 s ; la descente ne commence qu'avec le REDUCE de la règle ; 0 à 19,6 s | 152 à 0,2 s, 0 à 2,4 s, sous le FREEZE : les consignes du témoin |
| Séance programmée, un cycle en retard de 1 s (FREEZE `loop_stall` verrouillé), STOP | 165 tenu pendant 35 s, jusqu'à l'E-STOP | 152 à 0,2 s, 0 à 2,4 s, sous le FREEZE |
| Manuel capsule vide à 200, FREEZE `loop_stall` verrouillé, cible 0 puis STOP | cible 0 acceptée, sans effet pendant 20 s ; STOP sans effet pendant 45 s ; l'E-STOP arrête | cible 0 suivie : 198 à 0,2 s, 0 à 12,0 s. STOP seul, sans la cible 0 : mêmes consignes, fin `operator_stop` |
| Manuel avec personne déclarée à bord à 300, STOP 3 s après la perte de la FC | descente de 300 à 226 en 7 s, puis 226 tenu pendant 20 s quand le FREEZE arrive ; reprise avec le REDUCE ; 0 à 40,2 s | descente sans pause ; 0 à 20,0 s |

Sur le profil livré `standard_30_min`, en simulation (scénario
`stop_operator_auto_under_freeze` : FC perdue à 420 s pendant 27 s, STOP à
432 s) : avant, 165 tenu jusqu'au retour de la FC à 447 s, 0 à 460 s ;
maintenant, descente dès 432 s, 0 à 444,6 s. Avec les limites de la console,
la descente sous FREEZE a les mêmes consignes, cycle pour cycle, qu'un STOP
sans aucun verdict. Capsule vide à 27 tr/min au bras (1344 tr/min moteur),
FREEZE `loop_stall` verrouillé, arrêt demandé hors de la machine (scénario
`stop_remote_manual_under_latched_freeze`) : avant, 1344 tenu jusqu'à la fin
du scénario, 240 s plus tard, mode « ARRET » ; maintenant, 0 après 105,8 s de
descente aux limites de mouvement, console revenue à « REPOS ».

**Comment c'est appliqué.**

- Dans `TrainingRuntime._command`, la branche FREEZE a une garde devant elle
  (`_stop_asked`) : bras en mouvement, et fin de séance en cours ou cible
  manuelle à 0. La branche de maintien elle-même n'est pas modifiée.
- La descente (`_stop_under_freeze`) est un pas du profileur de mouvement vers
  0, celui d'un arrêt ordinaire. La destination est 0 et rien d'autre : rien
  ne peut y monter, la loi de commande et la FC ne sont pas consultées. Le
  dernier pas d'un programme, de la vitesse minimale à 0, attend comme
  d'habitude.
- **Dès le cycle suivant, sans à-coup.** Le maintien ne laisse pas de base de
  temps au profileur, et le REDUCE d'un programme en laisse une périmée.
  Suivies telles quelles, la première ferait commencer l'arrêt un cycle plus
  tard, la seconde ferait payer d'un coup toute la durée du REDUCE. La descente
  part donc du cycle précédent, sauf si le profileur y tournait déjà : un cycle
  de mouvement, jamais plus.
- Un FREEZE qui apparaît pendant une descente déjà commencée trouve le
  profileur en marche et ne change rien : mêmes consignes qu'une descente sans
  FREEZE (vérifié cycle pour cycle).
- **La fin de séance.** Un STOP sous FREEZE se termine comme tout STOP : fin
  `operator_stop`, aucun verrou `session_standstill`, puisque la séance se
  terminait déjà, sur demande. Un FREEZE qui était verrouillé avant le STOP le
  reste, et c'est la seule chose à acquitter ensuite. Une cible 0 suivie sous
  un FREEZE ne termine rien pendant la descente (mode « MANUEL ») ; à l'arrivée
  à 0, la séance se termine sur `session_standstill`, par la lecture prudente
  de 7.3. La phrase du verdict dit alors, pour un FREEZE `loop_stall` : « the
  operator's manual target of zero, followed under the warning loop_stall,
  brought the setpoint to zero ».
- **L'écran.** Tant qu'un FREEZE tient la consigne à distance d'une cible
  manuelle non nulle, le runtime n'annonce ni rampe ni heure d'arrivée
  (`ramping` faux, `ramp_eta_s` vide) : le bandeau « RAMPE EN COURS » ne
  s'affiche pas, et la ligne `rampe` dit « consigne maintenue, cible non
  atteinte » au lieu de « cible atteinte ». Une descente vers 0 sous FREEZE
  est une vraie rampe : elle garde son bandeau et son heure d'arrivée. Le mode
  « ARRET » n'est donc plus affiché sur une consigne qu'un FREEZE empêche de
  descendre.
- Aucun seuil, aucun délai, aucune règle du superviseur n'est modifié.

**Ce que cela ne couvre pas, et ce qui reste à décider.**

1. **Le retour au calme d'un programme sur sa propre chronologie.** Quand un
   programme arrive à sa phase COOLDOWN sous un FREEZE, sans qu'aucune fin
   n'ait été demandée, la consigne reste tenue, comme avant. Mesuré sur le banc
   accéléré, FREEZE `loop_stall` verrouillé : 165 tenu pendant tout le retour
   au calme et toute la récupération, jusqu'à ce que `session_overrun` termine
   la séance 30 s après sa durée prévue (c'est le premier cas de 8.4, mesuré
   là à 276 avec un FREEZE plus tardif). Ce n'est pas un arrêt demandé par
   quelqu'un, et le ticket laisse le maintien tel quel hors de ce cas ;
   l'opérateur a maintenant un STOP qui agit. Étendre la règle à cette phase
   est une décision à prendre.
2. **La cible 0 sous FREEZE termine la séance.** C'est la lecture prudente
   déjà retenue sous REDUCE, appliquée telle quelle. L'autre lecture possible
   (la séance continue à l'arrêt, puisque sous un FREEZE ce zéro ne peut venir
   que de l'opérateur) demanderait une décision ; rien ne repartirait seul
   dans les deux cas, puisque la cible vaut 0.

**Ce que cela coûte.** Un acquittement de plus quand l'opérateur amène le bras
à 0 par la cible plutôt que par STOP pendant un avertissement. Pour terminer
une séance sous un avertissement, STOP reste le geste simple.

## 8. Une séance finie n'est plus jugée sur sa durée (ANH-181)

Ticket
[ANH-181](https://linear.app/anheart/issue/ANH-181/la-regle-session-overrun-se-verrouille-apres-la-fin-dune-seance-et),
6 octobre 2026. C'est la correction d'un défaut, pas une décision de produit :
aucun seuil, aucune action, aucun verrou ne change. La définition de « séance
en cours » donnée en 8.3 est celle de l'auteur du changement ; la revue
indépendante la juge.

### 8.1 Ce qui se passait

La règle `session_overrun` arrête une séance qui dure plus que son programme :
durée prévue plus 30 s, ou 3600 s plus 30 s en séance manuelle. Elle mesure le
temps écoulé depuis le départ, et ce temps continue de courir après la fin de
la séance, jusqu'au départ suivant. La règle se verrouillait donc sur une
machine au repos, séance finie, sans que personne touche à rien. Sa condition
restait ensuite vraie pour de bon : chaque acquittement était accepté puis
repris au cycle suivant, tout départ était refusé (programme, séance manuelle,
lancement depuis le site), et il fallait redémarrer la console, ce qui efface
aussi l'attestation du câblage. Rien ne bougeait : la consigne valait 0 et
l'étage de sortie était retiré. Le risque était indirect : une console
verrouillée à tort après chaque séance habitue à acquitter par réflexe.

### 8.2 Ce qui a été mesuré

Tout est mesuré sur le banc d'essai logiciel (variateur factice, horloge
manuelle) et sur la console réelle en simulation, reliée à un tableau de bord
scripté ; jamais sur le matériel. « Avant » est la branche `develop` au commit
`d652990`. Les temps sont comptés depuis le départ de la séance, au cycle
près (0,2 s).

| Séquence | Avant | Maintenant |
|---|---|---|
| Profil livré de 1800 s mené à son terme, puis personne ne touche à rien. | `REPOS` à 1800,2 s. À 1830,2 s, `session_overrun` verrouillé : retour au mode `ARRET` et à la phase `recovery` pendant 300 s, puis `REPOS` à 2130,6 s avec le verdict. Acquittement accepté, verdict revenu au cycle suivant. Départ refusé. | `REPOS` à 1800,2 s et plus rien : aucun verdict à aucun cycle pendant une heure de repos, deux fois la durée prévue. Rien à acquitter. Départ accepté. |
| Même programme, STOP à 420 s. | `REPOS` à 732,8 s. Verdict à 1830,2 s. | `REPOS` à 732,8 s. Aucun verdict (suivi jusqu'à 2620 s). |
| Séance manuelle capsule vide, STOP à 60 s. | `REPOS` à 68,4 s. Verdict à 3630,2 s. Départ refusé ; acquittement accepté, départ refusé de nouveau. | Aucun verdict (suivi jusqu'à 3760 s). Départ accepté. |
| Console reliée, programme de 1400 s lancé depuis le site et mené à son terme. | `REPOS` à 1400,4 s. Verdict à 1430,4 s, mode `ARRET` pendant 305 s, puis `REPOS` avec le verdict. Deux acquittements acceptés, chacun repris au cycle suivant. Départ d'un programme, départ manuel et lancement du site refusés, ce dernier renvoyé au site comme séance échouée (« refusee par la machine : verdict de securite session_overrun a acquitter a la console »). Pareil dix minutes plus tard. | `REPOS` à 1400,4 s. Aucun changement de mode, de phase ni de verdict pendant 2800 s de repos. Un lancement du site est accepté et confirmé au site ; après un STOP, un programme démarré à la console est accepté ; après un autre STOP, une séance manuelle aussi. La console n'est jamais redémarrée. |
| Fin par `session_standstill` (électrode détachée, profil livré), verdict laissé sans acquittement. | À 1830,2 s, `session_overrun` s'ajoutait et ne s'effaçait plus : il fallait redémarrer la console. | `session_standstill` reste le seul verdict ; un acquittement nominatif le lève, il ne revient pas, et le départ est accepté. |

La batterie de simulation (62 scénarios, 199 pannes injectées, 90 séances de
la cohorte : 351 courses) a été rejouée avant et après. Les 350 courses
existantes donnent le même résultat : mêmes fins, mêmes règles, mêmes vitesses
de pointe, même temps en zone. Aucune ne laissait la console au repos au-delà
de l'échéance, donc aucune ne voyait le défaut. Le scénario ajouté,
`auto_rest_after_end_then_start_again` (programme de 1400 s, 2830 s de repos,
puis un départ), échoue avant (la règle se déclenche à 1430 s, le départ à
4230 s est refusé) et passe maintenant.

### 8.3 Ce que « séance en cours » veut dire

Une séance est **en cours** depuis son départ et jusqu'à ce que le runtime la
constate finie. Il la constate finie quand deux faits sont vrais ensemble :

1. depuis le départ, la machine à phases de cette séance a atteint `DONE` au
   moins une fois (le programme est allé au bout, ou la descente et la
   surveillance de récupération qui suivent un STOP ou un verdict sont
   terminées) ; ce constat est gardé jusqu'au départ suivant ;
2. la consigne en vigueur est 0.

Le runtime le dit au superviseur dans son observation (`session_over`), et la
règle ne juge que tant que ce n'est pas dit. Le temps écoulé, lui, continue de
courir comme avant.

Pourquoi ces deux faits, et pas un critère plus simple :

- **La phase seule ne garde pas le fait.** Un verdict qui arrive au repos
  après un programme allé à son terme (E-STOP pendant que le passager
  descend, variateur éteint qui passe en défaut) rouvre une fin de séance, et
  la phase repasse par `recovery` sur une séance finie depuis longtemps. Une
  règle qui ne lirait que la phase se verrouillerait là.
- **Le mode `REPOS` arrive trop tard dans un cas.** Avec un variateur en
  défaut, la fin de séance attend le réarmement par l'opérateur : le mode reste
  `ARRET`, bras arrêté, consigne à 0, phase `DONE`. La séance y est finie ; une
  règle qui attendrait `REPOS` se verrouillerait sur cette machine à l'arrêt si
  l'opérateur revient après l'échéance.
- **« Étage de sortie retiré » changerait le comportement pendant la
  séance** : il l'est pendant toute la phase `recovery`, où la règle juge
  aujourd'hui.
- **La consigne à 0 garde la règle armée tant qu'une vitesse est commandée**,
  quoi que dise la machine à phases. Un runtime devenu silencieux atteint
  `DONE` avec une consigne qu'il ne peut plus reprendre : la règle continue de
  juger, comme avant.
- **Le constat porte sur la consigne, pas sur la vitesse mesurée.** La seule
  action de la règle est de ramener la consigne à 0 ; quand elle y est déjà et
  que la séance est finie, la règle n'a plus rien à commander. Un arbre encore
  en roue libre à ce moment (variateur en défaut) n'est pas ralenti par cette
  règle, avant comme après : elle n'ajoutait qu'un verrou à celui qui a
  terminé la séance.
- **Le constat vaut « non » par défaut, et ne fait taire que cette règle.**
  Une observation qui ne le porte pas est jugée comme une séance en cours, et
  aucune autre règle ne le lit.

### 8.4 Ce qui ne change pas

Tout ce que la règle arrêtait pendant une séance, elle l'arrête encore, au
même instant, avec la même action et le même verrou. Mesuré avant et après,
résultats identiques jusqu'au verdict :

- **Un bras tenu en vitesse au-delà de la fin du programme.** Programme de
  130 s du banc d'essai, FREEZE verrouillé à 40 s et jamais acquitté : la
  consigne reste à 276 tr/min moteur après la fin du programme, la phase ne
  passe pas à `DONE` sur un bras qui tourne, et `session_overrun` se déclenche
  à 160,0 s. RAMP_DOWN l'emporte sur FREEZE : consigne à 0 à 164,2 s. C'est
  cette règle qui arrête une descente qui ne finit pas, et elle le fait
  toujours. Cela vaut quand personne n'a demandé l'arrêt : un STOP donné sous
  ce FREEZE fait descendre la consigne dès le cycle suivant, sans attendre la
  règle (7.7), et la séance se termine alors comme après tout STOP (8.5 pour
  un STOP donné tard).
- **Une fin de séance qui dépasse l'échéance.** Voir 8.5.
- **Un acquittement donné avant la fin de la séance** est accepté et le verdict
  revient au cycle suivant, comme pour toute règle dont la cause est encore
  là.

Ce qui change pour ces verdicts légitimes : une fois la séance finie (mode
`REPOS`), un acquittement nominatif les lève et **tient**, et le départ suivant
est accepté. Avant, ils ne s'effaçaient plus.

### 8.5 Ce que le correctif ne couvre pas

- **Une fin de séance ouverte tard dans un programme mène encore au
  verdict.** Toute fin de séance rouvre une surveillance de récupération
  complète (300 s avec le profil livré) : un STOP, à la console ou demandé
  depuis le site, un E-STOP, ou un verdict d'arrêt, même pendant la `recovery`
  du programme, bras déjà arrêté. Si cette récupération dépasse l'échéance, la
  règle se déclenche pendant qu'elle dure. Mesuré sur le profil livré de
  1800 s :
  - STOP à 1520 s : `REPOS` à 1820,4 s, aucun verdict. STOP à 1540 s ou à
    1600 s : `session_overrun` à 1830,2 s pendant la `recovery`, `REPOS` à
    1840,4 s ou 1900,4 s ;
  - E-STOP à 1600 s : `session_overrun` à 1830,2 s, verrouillé derrière
    l'arrêt d'urgence, `REPOS` à 1900,4 s ; un seul acquittement donné à
    `REPOS` lève les deux. Si l'E-STOP est acquitté avant `REPOS`,
    `session_overrun` reste (ou apparaît à 1830,2 s) comme verdict en vigueur,
    et se lève par un second acquittement à `REPOS` ;
  - fréquence cardiaque perdue à partir de 1700 s : `hr_stale` termine la
    séance à 1760,0 s, `session_overrun` se déclenche à 1830,2 s, `REPOS` à
    2060,4 s. Le plancher verrouillé garde `hr_stale`, le premier des deux
    verdicts de même niveau : `session_overrun` n'apparaît que parmi les
    règles actives, et un seul acquittement à `REPOS` suffit.

  Toute fin de séance ouverte dans les quatre dernières minutes et demie de
  ce programme se termine donc par ce verdict. Le mouvement n'est pas
  concerné : le bras est déjà arrêté. C'est le comportement d'avant, gardé
  parce que la séance est encore en cours ; seule la suite change
  (l'acquittement tient à `REPOS`). Faut-il qu'une fin ouverte pendant la
  `recovery` rouvre une récupération complète, ou que l'échéance suive une fin
  déjà engagée : la question n'est pas tranchée ici.
- **Une séance manuelle qui atteint ses 3600 s à grande vitesse.** Le runtime
  demande alors la fin de séance et la consigne descend aux limites de
  mouvement. Si la descente dure plus de 30 s, la règle se verrouille pendant
  la descente, sans changer la rampe. Mesuré sur le banc d'essai avec une
  limite raccourcie : depuis 1344 tr/min moteur (27 tr/min au bras), descente
  d'environ 104 s, verdict 30 s après la limite ; depuis 300 tr/min moteur (le
  plafond par défaut), descente de 20 s, aucun verdict. Comportement d'avant,
  gardé pour la même raison.
- **Un verdict qui arrive au repos après un programme allé à son terme**
  (E-STOP, défaut variateur) rouvre toujours une fin de séance : mode `ARRET`
  et phase `recovery` pendant la durée de récupération du profil, règles de
  fréquence cardiaque de nouveau actives. Mesuré sur le profil livré : E-STOP
  65 s après la fin, électrodes retirées 5 s plus tôt ; `hr_stale` monte
  jusqu'au RAMP_DOWN verrouillé 60 s après la dernière lecture, et `REPOS`
  revient 300 s après l'E-STOP. Après une séance terminée par un STOP, la fin
  est déjà enregistrée et rien n'est rouvert : le mode reste `REPOS` (vu sur
  la console en simulation, séance programmée et séance manuelle).
  `session_overrun` ne s'y ajoute plus ; le reste est le comportement d'avant,
  hors de ce ticket.
- **Rien n'a été mesuré sur la vraie machine**, ni avec le vrai variateur.

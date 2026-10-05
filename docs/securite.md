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
dans la simulation (60 scénarios, 199 pannes injectées, cohorte de 30
personnes). Pas sur la vraie machine avec une personne à bord.

| Garantie | Où |
|---|---|
| **La sécurité passe avant la régulation.** Le superviseur rend son verdict sans voir la demande du régulateur, et le verdict le plus grave l'emporte toujours. | `src/training/safety.py`, `runtime.py` |
| **Pas d'accélération sur un malaise.** La consigne ne peut pas monter tant que la pente de la fréquence cardiaque est sous −20 bpm/min, ou inconnue. | garde vasovagale, `runtime.py` |
| **Rien ne redémarre seul.** Un verdict verrouillé ne se lève que par un acquittement nominatif ; aucun réarmement automatique de défaut, aucune reprise automatique. GO_SILENT ne s'acquitte jamais dans le même processus. | `safety.py` |
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

# Glossaire

Les termes techniques utilisés dans cette documentation, dans l'ordre alphabétique.
Quand un terme correspond à un nom dans le code, le nom est donné entre
parenthèses, avec le fichier où il est défini.

[Retour au sommaire](README.md)

---

<a id="acquittement"></a>
**Acquittement** (`acknowledge`, `src/training/safety.py`)\
Action nommée d'un opérateur qui lève un [verdict verrouillé](#latch). Il faut
donner son nom. Après un E-STOP, l'opérateur doit aussi déclarer que le
bouton coup-de-poing est relâché (`estop_released`). Un acquittement ne
relance jamais la machine : il permet seulement un nouveau démarrage.

**ACC / dEC** (registres du variateur)\
Temps de rampe d'accélération et de décélération réglés dans le variateur
(mise en service : environ 3 à 4 s). Le logiciel les lit au démarrage et les
utilise pour juger si l'arbre suit la consigne (voir `tracking_error`).

**Admin / gestionnaire / user** (rôles, `convex/lib/auth.ts`)\
Les trois rôles du site. L'admin voit et gère tout, crée les machines et
change les rôles. Le gestionnaire gère ses machines et ses patients, et peut
donner à ses patients le droit de lancement sur ses machines. Le user
(patient) ne peut lancer une séance que pour lui-même, et seulement s'il a ce
droit. Un nouveau compte reçoit le rôle user. Détails dans
[tableau-de-bord.md](tableau-de-bord.md) et [convex.md](convex.md).

**Attendant / présence opérateur** (`attendant_absent`)\
La console web envoie régulièrement un signal « je suis là ». Si ce signal
s'arrête pendant une séance, la règle `attendant_absent` fige puis arrête la
séance. Voir [console-locale.md](console-locale.md).

**Attestation du câblage E-STOP** (`confirm_estop_wiring`)\
À chaque démarrage de la console, un opérateur nommé doit attester, en
cochant deux cases, la phrase exacte du code (`ESTOP_ATTESTATION`) : « a latching
mushroom emergency stop is wired normally-closed into P24 -> STO and the STO
jumper has been removed » (un coup-de-poing à accrochage est câblé en contact
fermé sur P24 -> STO, et le pont STO a été retiré). Sans attestation, aucun
mouvement n'est accepté (`EstopUnattested`). Le logiciel ne peut pas vérifier
que c'est vrai : voir [STO](#sto).

**AUTO (séance programmée)**\
Séance où la fréquence cardiaque pilote la vitesse pour tenir une
[zone](#zone). Elle suit un programme enregistré (profil). Elle peut être
lancée à la console ou depuis le site.

**BASELINE, WARMUP, HOLD, COOLDOWN, RECOVERY** (`Phase`, `src/training/types.py`)\
Les phases d'une séance AUTO. BASELINE : mesure de la fréquence de repos,
moteur arrêté. WARMUP : montée en vitesse. HOLD : la seule phase où la loi de
commande vise la zone. COOLDOWN : descente. RECOVERY : récupération à
l'arrêt. Une séance peut passer d'une phase à n'importe quelle autre (par
exemple WARMUP directement en COOLDOWN sur un verdict RAMP_DOWN).

**BENCH / OCCUPIED** (`Occupancy`)\
Déclaration de l'opérateur avant tout mouvement. BENCH : personne à bord
(moteur désaccouplé, ou capsule vide). OCCUPIED : une personne à bord. La
déclaration ne change jamais pendant la rotation. OCCUPIED reste refusé tant
que `OCCUPANCY_OCCUPIED_ENABLED=false`.

**BITalino**\
Boîtier d'acquisition physiologique (Bluetooth). Il fournit l'ECG, et
d'autres voies (EDA, RESP, EMG, LUX, SpO2) affichées pour surveillance
seulement.

**CiA402**\
Norme de profil de variateur (machine d'états : SWITCH_ON_DISABLED,
READY_TO_SWITCH_ON, SWITCHED_ON, OPERATION_ENABLED, FAULT…). Le logiciel
pilote l'ATV320 en envoyant les mots de commande de cette norme. Point
important : la « transition 8 » (SHUTDOWN depuis OPERATION_ENABLED) coupe le
couple et laisse la machine en [roue libre](#roue-libre) ; le logiciel ne
l'envoie jamais sur une machine qui tourne.

**Cohorte**\
Les 30 personnes fictives générées à partir d'une graine fixe dans
`simulation/cohort/`. Chacune a un âge, une FC max, une réponse cardiaque,
et parfois une condition particulière (vasovagal, ectopique…). Voir
[framework-de-test.md](framework-de-test.md).

**Convex**\
La base de données et les fonctions distantes du projet (dossier `convex/`).
Voir [convex.md](convex.md).

**E-STOP**\
Arrêt d'urgence. Sur la console web, il déclenche l'action QUICK_STOP
(règle `operator_estop`) et se verrouille. Attention : ce n'est pas une
coupure de puissance, voir [STO](#sto) et [securite.md](securite.md).

**ECG / FC / bpm**\
Électrocardiogramme ; fréquence cardiaque ; battements par minute.

**ETA** (registre du variateur)\
Le mot d'état CiA402 de l'ATV320. Le logiciel le lit à chaque cycle pour
connaître l'état du variateur (prêt, en marche, en défaut…).

**FC max (HRmax)**\
Fréquence cardiaque maximale de la personne. Sur le site on la saisit ou on
la déduit de l'année de naissance. Un programme est refusé si le haut de sa
zone dépasse 90 % de la FC max de la personne.

**g centripète (Gc) / g résultant (Gr)** (`src/units.py`)\
Gc = ω²·r / 9,81, l'accélération due à la rotation, au rayon r. Gr = √(Gc² + 1),
la charge totale ressentie (rotation + pesanteur). Le logiciel cite le g au
rayon de référence `ARM_RADIUS_M` et limite la variation de g (anti-nausée)
au rayon du bout des pieds `LEG_TIP_RADIUS_M`.

<a id="gate-porte"></a>
**Gate (« porte »)**\
Le script qui doit passer avant toute fusion : lint, deux vérificateurs de
types stricts, tests et couverture. `raspberry-pi/scripts/check.sh` pour le
Pi, `simulation/scripts/check.sh` pour la simulation. Voir
[framework-de-test.md](framework-de-test.md).

**GO_SILENT** : voir [Niveaux d'action](#niveaux-daction).\

**HSP** (registre du variateur, *High SPeed*)\
Vitesse maximale réglée dans le variateur, en Hz (ici 50 Hz, soit 1380 tr/min
moteur). Le logiciel refuse un plafond de vitesse au-dessus de HSP.

**Hypothesis**\
Bibliothèque Python de tests de propriétés : elle génère des milliers
d'entrées au hasard pour vérifier qu'une propriété reste vraie (par exemple
« la consigne reste toujours dans `{0} ∪ [min_run, plafond]` »).

<a id="latch"></a>
**Latch (verrouillage)** (le « plancher » du superviseur)\
Un verdict verrouillé reste en place même si sa cause disparaît. Un verdict
plus grave remplace un moins grave ; seul un [acquittement](#acquittement)
nommé le lève. Rien ne redémarre tout seul.

**LCR** (registre du variateur)\
Courant moteur mesuré.

**LFRD** (registre du variateur)\
La consigne de vitesse, en tr/min **moteur**, écrite par le logiciel. Elle est
relue après écriture (l'« écho ») ; un écho qui ne suit pas déclenche
`setpoint_unconfirmed`.

**LFT** (registre du variateur, *Last FaulT*)\
Le code du dernier défaut du variateur. La table complète (66 codes) est dans
[raspberry-pi.md](raspberry-pi.md). « nOF » signifie « pas de défaut ».

**MANUEL (séance manuelle)**\
Séance où l'opérateur fixe la vitesse cible en tr/min. Elle ne se lance
**que** depuis la console de la machine, jamais depuis le site.\

**[MED]**\
Marque, dans le code et la configuration, d'une valeur qui relève d'une
décision médicale non encore prise (par exemple `MIN_RIDER_AGE=18`, les
paliers cardiaques).

**min_run**\
La plus petite vitesse moteur non nulle acceptée (55 tr/min moteur). Entre 0
et min_run, une consigne est refusée, pas arrondie.

**Motoréducteur / rapport 49,79**\
Le moteur tourne 49,79 fois plus vite que le bras. Le logiciel distingue par
des types différents la vitesse moteur (`MotorRpm`) et la vitesse de sortie,
celle du bras (`OutputRpm`). Exemple : 1380 tr/min moteur ≈ 27,7 tr/min bras.

<a id="niveaux-daction"></a>
**Niveaux d'action** (`SafetyAction`, `src/training/types.py`)\
Ce que le superviseur de sécurité exige, du moins grave au plus grave. Quand
plusieurs règles se déclenchent, la plus grave gagne.

| Niveau | Effet |
|---|---|
| NONE | aucune exigence ; la demande du régulateur s'applique |
| FREEZE | figer la vitesse : la consigne ne bouge plus |
| REDUCE | baisser la consigne et continuer à réguler |
| RAMP_DOWN | finir la séance : descente contrôlée jusqu'à zéro |
| QUICK_STOP | consigne à zéro tout de suite ; l'ordre de marche reste en place, et le variateur freine sur sa propre rampe (3 à 4 s) |
| GO_SILENT | ne plus rien écrire au variateur ; son [ttO](#tto) expire et il arrête le moteur tout seul. Sans retour possible |

**ObF**\
Défaut « surtension du bus continu » du variateur (LFT 18). Il survient si on
freine plus vite que la rampe réglée : le bus n'absorbe qu'environ 11 J sur les
~420 J stockés dans le bras en rotation. Le variateur passe alors en
[roue libre](#roue-libre).

**Palier hard max / palier critique** (`HR_HARD_MAX_BPM`, `HR_CRITICAL_BPM`)\
Deux seuils de fréquence cardiaque du superviseur (défaut 148 et 158 bpm).
Au-dessus du hard max : REDUCE (`hr_hard_max`). Au-dessus du critique :
QUICK_STOP (`hr_critical`). Chaque profil doit porter exactement les mêmes
valeurs, sinon il est refusé.

**Présence (caméra)** (`src/presence/`)\
Règles qui transforment ce que voit une caméra en arrêts ou en refus de
démarrage. Aujourd'hui, seule une caméra **simulée** existe.

**RFRD** (registre du variateur)\
La vitesse moteur mesurée par le variateur, en tr/min.

<a id="roue-libre"></a>
**Roue libre (freewheel)**\
Le variateur ne produit plus de couple et le bras ralentit seul, par
frottement : environ 145 s, sans contrôle. C'est ce que le logiciel évite en
freinant toujours sur la rampe.

**Scénario**\
Un fichier JSON dans `simulation/scenarios/` qui décrit une séance simulée et
ce qui doit être vrai à la fin. Voir [framework-de-test.md](framework-de-test.md).

**SLF**\
Défaut « perte de communication » du variateur (SLF1 = LFT 5 pour Modbus).
C'est ce qu'on voit après que le [ttO](#tto) a expiré.

<a id="sto"></a>
**STO** (*Safe Torque Off*)\
Entrée de sécurité du variateur qui coupe le couple par le matériel. **Sur
cette machine, elle est aujourd'hui pontée** (commentaires du code, par exemple
`SafetyAction` dans `src/training/types.py`) : aucune action logicielle ne coupe
le couple instantanément. L'arrêt le plus rapide est une rampe. L'attestation
demandée au démarrage affirme au contraire que le pont a été retiré ; sur le
banc actuel, elle serait donc fausse.

**Superviseur de sécurité** (`SafetySupervisor`, `src/training/safety.py`)\
Le module qui évalue toutes les règles à chaque cycle et rend un
[verdict](#verdict). Il a toujours priorité sur le régulateur de fréquence
cardiaque.

<a id="tto"></a>
**ttO** (paramètre du variateur)\
Délai de perte de communication Modbus, réglé **dans le variateur**. Si le
logiciel cesse d'écrire plus longtemps que ttO, le variateur arrête le moteur
seul (défaut SLF). C'est le chien de garde externe au logiciel.

<a id="verdict"></a>
**Verdict** (`SafetyVerdict`)\
La décision d'une règle de sécurité : un [niveau d'action](#niveaux-daction),
l'identifiant de la règle (par exemple `hr_stale`) et une phrase pour
l'opérateur.

**xfail strict**\
Un test marqué « échec attendu » (`pytest.mark.xfail(strict=True)`). Il décrit
le comportement correct que le code ne fournit pas encore. Tant que le défaut
existe, le test échoue et c'est « normal » (XFAIL). Le jour où le défaut est
corrigé, le test passe et, parce qu'il est strict, la batterie devient
rouge : il faut alors retirer la marque. Voir
[framework-de-test.md](framework-de-test.md).

<a id="zone"></a>
**Zone**\
La plage de fréquence cardiaque visée pendant HOLD, par exemple « jog »
145-155 bpm. Le régulateur accélère sous la zone et ralentit au-dessus, dans
les limites du superviseur.

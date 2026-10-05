# Anheart Raspberry Pi: operator console

The local console controls the ATV320, acquires the BITalino ECG, applies the
safety supervisor and serves the operator's web page. Its entry point is
`python -m src.local_panel`. The separate `python -m src.main` entry point is
the legacy ECG recorder; it does not control the motor.

The [project documentation](../docs/README.md) describes the architecture.
For deployment, follow [the Pi procedure](../docs/deploiement.md#7-le-raspberry-pi):
`.env.pi.example`, `scripts/pi/preflight.sh` and `scripts/pi/deploy.sh`.
The Docker image, Compose service and native systemd unit start `src.local_panel`.

## Development setup

Use Python 3.12 (the target in `pyproject.toml` and the Docker base) and the
shared `.venv` used by the Pi and simulation tests. From this directory:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install pyserial
.venv/bin/python -m pip install --no-deps bitalino
```

`requirements-dev.txt` explains the separate BITalino installation. Hardware
Bluetooth setup depends on the platform and selected transport; use the
[deployment guide](../docs/deploiement.md) and the supplied Pi configuration.
Simulation needs neither a cable nor a BITalino. A machine API key is needed
only for the optional dashboard link.

## Console locale

Une seule commande, depuis `raspberry-pi/`, en natif (sur macOS, Docker ne voit
ni le cable FTDI ni le Bluetooth) :

```bash
.venv/bin/python -m src.local_panel
```

puis ouvrir `http://127.0.0.1:8080/` (ou le port choisi par `UI_PORT`).
La navigation est décrite dans le [guide opérateur](../docs/console-locale.md).
Ctrl-C demande l'arrêt de la console.

La console accepte les commandes manuelles sous les portes de sécurité ; les
programmes demandent `PROGRAMS_ENABLED=true` et l'autorisation d'occupation.
Au repos confirmé, elle lit le variateur à 2 Hz. Un variateur trouvé déjà
activé ou en rotation déclenche la mise à zéro et le verrouillage
`drive_precommanded`. À la fermeture, une liaison au repos confirmé ou non
acquise est libérée sans écriture ; un état acquis inconnu ou une séance
démarrée passe par l'arrêt du runtime. Une commande d'arrêt ne constitue pas
une mesure de l'arrêt de l'arbre.

La page affiche : ECG en direct, BPM (qualite, age, tendance), vitesse
**mesuree** (tr/min de sortie, tr/min moteur, Hz, Gc centripete, Gr resultant),
etat du variateur avec le code LFT **brut**, latence et echecs de la liaison
variateur, compteurs de la liaison BITalino (trames, pertes de synchro, octets
ignores, echantillons combles, reconnexions).

Configuration dans `.env` (voir `.env.example`, section « Local operator
console ») ; chaque probleme est signale d'un coup au demarrage :

| Cle | Banc reel (Mac) | Simulation complete |
|---|---|---|
| `MOTOR_BACKEND` | `serial` | `sim` |
| `MOTOR_PORT` | `ftdi://schneider:rs485/1` | (ignore) |
| `MOTOR_SLAVE_ID` | `248` | (ignore) |
| `ECG_SOURCE` | `rfcomm` | `sim` |
| `BITALINO_ADDRESS` | `rfcomm:98-d3-91-fe-4e-9f` | (ignore) |
| `ARM_RADIUS_M` | `1.5` (obligatoire, sans defaut) | `1.5` |
| `UI_PORT` | `8080` (défaut ; pas 8123 : `bench_console.py`) | `8080` |

Optionnels : `GEAR_RATIO` (defaut 49.79, confirme au banc), `MOTOR_MAX_RPM`
(defaut 300, 0..1380), `UI_HOST`/`UI_TOKEN` (hors loopback, jeton de 16
caracteres minimum), `OCCUPANCY_OCCUPIED_ENABLED` (reste `false` jusqu'au
jalon M6).

Pour une repetition a blanc sans aucun materiel, les variables du processus
priment sur `.env` :

```bash
MOTOR_BACKEND=sim ECG_SOURCE=sim MACHINE_API_KEY= ARM_RADIUS_M=1.5 \
  UI_HOST=127.0.0.1 UI_PORT=8080 .venv/bin/python -m src.local_panel
```

## Propriété exclusive du câble variateur

Un seul programme possede la liaison variateur par hote. La console, le banc,
la mesure de latence, `probe_atv320.py` et `scan_modbus.py` prennent le meme
verrou noyau avant toute ouverture du transport. Un concurrent refuse avec
`drive cable already owned` et le PID du proprietaire ; aucune commande ni
ouverture physique ne precede cette acquisition. Fermer le programme proprietaire
avant de reprendre la liaison.

La configuration contient un seul `drive_link` et cette protection reserve
volontairement **tous les cables variateur de l'hote**, meme si deux ports
semblent distincts : un nom FTDI et un alias serie peuvent designer le meme
cable. Commander plusieurs variateurs depuis un hote n'est pas pris en charge.
Le fichier partage est `/tmp/anheart-drive.lock` sous Linux/macOS et
`%PROGRAMDATA%/anheart-drive.lock` sous Windows (`C:/ProgramData` par defaut).
Il ne depend ni du repertoire de lancement, ni du checkout, ni de `TMPDIR`.
Les comptes service et operateur doivent pouvoir ouvrir ce meme fichier ;
une erreur de droits refuse la liaison, sans repli vers un autre verrou.

Ne jamais supprimer le fichier pour forcer une prise : son inode doit rester
stable. Le PID est seulement informatif et peut rester apres fermeture ; le
noyau libere le verrou a la fermeture du transport ou a la fin du processus,
y compris un crash. Un echec d'ouverture libere la reservation seulement apres
fermeture du transport partiellement ouvert. Si la
fermeture du transport echoue, la reservation reste jusqu'a sa fermeture
effective ou la fin du processus, meme si l'appelant abandonne l'erreur.
`close()` retente ce nettoyage ; une nouvelle ouverture FTDI ou une reconnexion
serie termine d'abord le nettoyage en attente, sans ouvrir par-dessus l'ancien
handle. Pour un appel direct a `open_ftdi_port`,
`src.motor.drive_process_lock.retry_failed_drive_closes()` permet aussi de
retenter la fermeture. Chaque reconnexion doit reprendre le verrou ;
cela n'autorise aucune reprise automatique du mouvement. Cette protection
concerne les outils Anheart : un logiciel tiers tel que SoMove doit rester ferme.

---

## Dashboard link (local console ↔ Convex)

The local console (`python -m src.local_panel`) is the machine's admin: it
runs every session, and the Convex dashboard mirrors it. Set
`MACHINE_API_KEY` (and `CONVEX_URL`, the `.convex.site` host) to link it;
leave the key blank and the console is purely local. The link is
`src/cloud_sync.py`, and it can never stop the machine by failing: a dead
network only makes the dashboard's picture stale.

| Direction | What | When |
|---|---|---|
| machine → dashboard | heartbeat with live state (mode, phase, bpm, rpm, g, safety) | every 10 s |
| machine → dashboard | training presets whose cardiac tiers match this machine | when the profile store changes |
| machine → dashboard | every session run here, AUTO or MANUAL, with telemetry at 1 Hz and its end | while it runs |
| dashboard → machine | an AUTO launch (preset, rider, rider's max heart rate) | polled every 3 s when idle |
| dashboard → machine | a stop request, run as an ordinary stop on the commissioned ramp | checked every 3 s |

**Two kinds of training session:**

* **AUTO**: a pre-saved preset. The heart rate drives the turns per minute:
  the controller holds the rider in the preset's zone. It can be launched at
  the console or from the dashboard: by an admin, by a manager (gestionnaire)
  of the machine, or by a user a manager has granted the launch right on
  that machine. A dashboard launch passes the same gates as a start typed at
  the console, plus: `PROGRAMS_ENABLED=true`, `OCCUPANCY_OCCUPIED_ENABLED=true`
  (a programme always has a person on board), the preset under the occupied
  ceiling, and the preset re-validated against **this rider's** maximum heart
  rate. Any refusal comes back to the dashboard as a failed session with the
  console's reason.
* **MANUAL**: the operator sets the speed. Only ever started at the console.
  Nothing on the dashboard can start one; it only shows it.

**Cardiac tiers.** The supervisor's `HR_HARD_MAX_BPM` / `HR_CRITICAL_BPM`
(default 148 / 158) must equal every preset's `hard_max_bpm` /
`critical_bpm`. Presets that disagree are refused at start and are not
offered on the dashboard. A zone around 150 bpm needs higher tiers than the
defaults, which is a decision for the medical side, made in `.env`.

## Legacy ECG recorder configuration

| Variable             | Required | Default   | Description                             |
| -------------------- | -------- | --------- | --------------------------------------- |
| CONVEX_URL           | Yes      | -         | Convex deployment URL (.convex.site)    |
| MACHINE_API_KEY      | Yes      | -         | 64-character API key from dashboard     |
| BITALINO_MAC         | Yes      | -         | BITalino Bluetooth MAC address          |
| SAMPLE_RATE          | No       | 1000      | Sample rate in Hz (1, 10, 100, or 1000) |
| BATCH_INTERVAL_MS    | No       | 1000      | How often to send data (ms)             |
| HEARTBEAT_INTERVAL_S | No       | 30        | Heartbeat interval (seconds)            |
| LOG_LEVEL            | No       | INFO      | Logging level (DEBUG, INFO, WARNING)    |
| BUFFER_DB_PATH       | No       | buffer.db | Path for offline data buffer            |

The recorder defaults to acquisition at 1000 Hz and output at 250 Hz
(`SAMPLE_RATE`, `OUTPUT_SAMPLE_RATE`). Acquisition accepts 1, 10, 100 or
1000 Hz; output must not exceed acquisition. These keys configure `src.main`.
The local console acquires at fixed `ECG_SAMPLE_RATE=1000` and uses
`BITALINO_ADDRESS` / `ECG_SOURCE` instead of the recorder's `BITALINO_MAC`.
See [the configuration reference](../docs/raspberry-pi.md#12-la-configuration-env)
for the console's keys.

```sh
.venv/bin/python -m src.main --help
```

---

## Running as a Service

Docker/Compose and the native unit both launch the operator console. Use the
[deployment guide](../docs/deploiement.md#7-le-raspberry-pi) for installation and
permissions. The native unit is `scripts/anheart.service`; its paths and service
account must match the installation.

### Control Service

```bash
# Start
sudo systemctl start anheart

# Stop
sudo systemctl stop anheart

# Restart
sudo systemctl restart anheart

# Check status
sudo systemctl status anheart

# Enable auto-start on boot
sudo systemctl enable anheart

# View logs
journalctl -u anheart -f
```

---

## Troubleshooting

### "pip install" fails with PyBluez errors

**Error:** `error: command 'gcc' failed` or `bluetooth/bluetooth.h: No such file`

**Solution:** Install Bluetooth development libraries:

```bash
# Debian/Ubuntu/Raspberry Pi
sudo apt install libbluetooth-dev python3-dev

# Then retry
.venv/bin/python -m pip install -r requirements-prod.txt
```

### Cannot find BITalino device

**Error:** Device not appearing in scan

**Solutions:**

1. Make sure BITalino is powered ON
2. Check Bluetooth is enabled:
   ```bash
   bluetoothctl power on
   rfkill unblock bluetooth
   ```
3. Try scanning manually:
   ```bash
   bluetoothctl
   scan on
   # Wait 10-15 seconds
   ```
4. Power cycle the BITalino device

### Connection refused / Device busy

**Error:** `[Errno 16] Device or resource busy` or `Connection refused`

**Solutions:**

1. Make sure no other application is using the BITalino
2. Check if already paired:
   ```bash
   bluetoothctl info XX:XX:XX:XX:XX:XX
   ```
3. Remove and re-pair:
   ```bash
   bluetoothctl remove XX:XX:XX:XX:XX:XX
   bluetoothctl pair XX:XX:XX:XX:XX:XX
   ```

### Server connection fails

**Error:** `HTTP 404` or `Invalid API key`

**Solutions:**

1. Use `.convex.site` URL (not `.convex.cloud`)
2. Check API key is exactly 64 characters
3. Verify machine exists in web dashboard
4. Test connectivity:
   ```bash
   curl -X POST "https://your-project.convex.site/api/machine/heartbeat" \
     -H "Authorization: Bearer YOUR_API_KEY"
   ```

### Data not appearing in dashboard

**Solutions:**

1. Check `MACHINE_API_KEY`, the `.convex.site` URL and the console logs.
2. Verify that the machine appears online in the dashboard.
3. The console reports local MANUAL and AUTO sessions; only AUTO can start
   remotely. The legacy recorder has a separate session-polling path.

### Signal quality is "Poor"

**Cause:** ECG electrodes not connected or poor contact

**Solutions:**

1. Connect electrodes to body:
   - Red (RA) → Right arm/wrist
   - Black (LA) → Left arm/wrist
   - White (REF) → Right leg or lower torso
2. Use electrode gel for better contact
3. Clean skin before applying electrodes

---

## Development checks

Read the [strict Python contract](../.agents/skills/anheart-strict-python/SKILL.md)
before editing Python. From `raspberry-pi/`:

```sh
./scripts/check.sh
.venv/bin/python -m pytest --collect-only -q
```

The executable gate runs Ruff, both strict type checkers and the tests with the
configured 100% branch-coverage requirement. The current scope and explicit
migration debt live in `pyproject.toml`; test counts come from collection, not
a fixed inventory in this README. Hardware-marked tests are excluded by default.

For module structure, entry points and the legacy recorder's data path, see
[the Pi reference](../docs/raspberry-pi.md). For trace files shared with the
simulation, see [the recording format](../docs/enregistrement.md).

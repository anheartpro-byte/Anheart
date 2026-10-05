# AnHeart Raspberry Pi Client

> **Deployment of the operator console (October 2026).** The Docker image and
> `docker-compose.yml` now start the console (`python -m src.local_panel`), not
> the ECG recorder described below. The up-to-date procedure, in French, is
> [docs/deploiement.md](../docs/deploiement.md#7-le-raspberry-pi): `.env.pi.example`,
> `scripts/pi/preflight.sh`, `scripts/pi/deploy.sh`.


Python application for collecting BITalino ECG data and streaming to the AnHeart cloud platform.

## Features

- Bluetooth Classic connection to BITalino devices (BITalino, psychoBIT, etc.)
- Real-time ECG data streaming to Convex backend
- Offline data buffering when network unavailable
- Automatic reconnection and recovery
- Systemd service for auto-start

## Requirements

### Hardware

- Raspberry Pi 4 or 5 (or any Linux computer with Bluetooth)
- BITalino device (BITalino (r)evolution, psychoBIT, etc.)
- WiFi or Ethernet connection

### Software

- Python 3.9+ (tested with 3.11, 3.12, 3.14)
- Bluetooth enabled and working
- System packages for PyBluez

---

## Installation

### Step 1: Install System Dependencies

The `bitalino` library requires PyBluez, which needs system-level Bluetooth libraries.

**On Raspberry Pi / Debian / Ubuntu:**

```bash
sudo apt update
sudo apt install -y \
    python3-dev \
    python3-venv \
    bluetooth \
    libbluetooth-dev \
    bluez \
    bluez-tools
```

**On Arch Linux / CachyOS:**

```bash
sudo pacman -S python bluez bluez-utils
```

**On Fedora:**

```bash
sudo dnf install python3-devel bluez bluez-libs-devel
```

### Step 2: Enable Bluetooth

```bash
# Start bluetooth service
sudo systemctl enable bluetooth
sudo systemctl start bluetooth

# Make sure Bluetooth is not blocked
sudo rfkill unblock bluetooth

# Power on the adapter
bluetoothctl power on
```

### Step 3: Create Virtual Environment

```bash
cd raspberry-pi

# Create virtual environment
python3 -m venv venv

# Activate it
source venv/bin/activate
```

### Step 4: Install Python Dependencies

```bash
# Upgrade pip first
pip install --upgrade pip

# Install dependencies
pip install -r requirements.txt
```

**If `pip install` fails with PyBluez errors:**

```bash
# Try installing PyBluez separately first
pip install PyBluez

# If that fails, try the bitalino-specific fork
pip install PyBluez-bitalino

# Then install the rest
pip install -r requirements.txt
```

**Alternative: Install from system packages (Raspberry Pi):**

```bash
# On Raspberry Pi OS, you can use apt
sudo apt install python3-bluez

# Then create venv with system packages access
python3 -m venv venv --system-site-packages
source venv/bin/activate
pip install -r requirements.txt
```

### Step 5: Pair Your BITalino Device

Before the application can connect, you need to pair the BITalino with your system.

```bash
# Start bluetoothctl
bluetoothctl

# Inside bluetoothctl:
agent on
default-agent
scan on

# Wait for your BITalino to appear (e.g., "BITalino-XX-XX")
# Note the MAC address (format: XX:XX:XX:XX:XX:XX)

# Pair with the device (PIN is usually 1234)
pair XX:XX:XX:XX:XX:XX
# Enter PIN: 1234

# Trust the device
trust XX:XX:XX:XX:XX:XX

# Exit
quit
```

### Step 6: Configure

```bash
# Copy example config
cp .env.example .env

# Edit with your values
nano .env
```

Required settings in `.env`:

```bash
# Convex URL (use .convex.site for HTTP endpoints)
CONVEX_URL=https://your-project.convex.site

# Machine API key (get from web dashboard when creating a machine)
MACHINE_API_KEY=your_64_character_api_key_here

# BITalino MAC address (from pairing step)
BITALINO_MAC=XX:XX:XX:XX:XX:XX

# Sample rate (100 Hz recommended for monitoring)
SAMPLE_RATE=100
```

### Step 7: Test the Connection

```bash
# Activate venv if not already
source venv/bin/activate

# Test BITalino discovery
python scripts/discover_devices.py

# Test BITalino connection and data
python scripts/test_bitalino.py --mac XX:XX:XX:XX:XX:XX

# Run the full application
python -m src.main --debug
```

#### BITalino sur macOS (RFCOMM IOBluetooth)

Sur le Mac, `/dev/cu.BITalino-*` est intermittent. Le transport verifie est le
canal RFCOMM 1 via IOBluetooth (`src/bitalino_rfcomm_macos.py`), choisi par une
adresse `rfcomm:` (`BITALINO_ADDRESS=rfcomm:98-d3-91-fe-4e-9f`,
`ECG_SOURCE=rfcomm`). Appairer une fois dans les reglages Bluetooth de macOS
(PIN 1234), puis verifier la liaison, en lecture seule (aucun moteur) :

```bash
.venv/bin/python scripts/test_bitalino_rfcomm.py            # 20 s, A1 a 1000 Hz
.venv/bin/python scripts/test_bitalino_rfcomm.py --seconds 600
```

Le script affiche chaque seconde trames/s (attendu ~1000), echecs CRC, octets
sautes, trous, reconnexions, qualite et BPM, puis un verdict.

---

## Console locale

Une seule commande, depuis `raspberry-pi/`, en natif (sur macOS, Docker ne voit
ni le cable FTDI ni le Bluetooth) :

```bash
.venv/bin/python -m src.local_panel
```

puis ouvrir `http://127.0.0.1:8090/` (onglet **Console**, page « Console du
banc »). Ctrl-C arrete proprement.

**Jalon M1 : LECTURE SEULE.** Le variateur est lu (ETA, LFRD, RFRD, LCR a
2 Hz) et **jamais ecrit** : pas de keepalive, pas de mot de commande, pas de
consigne. Toute route de mouvement repond 403. STOP, E-STOP, acquittement et
lectures restent disponibles ; un E-STOP web est la seule ecriture possible
(consigne mise a zero). A la sortie, la liaison est simplement refermee, sans
sequence d'arret, puisque rien n'a ete commande.

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
| `UI_PORT` | `8090` (pas 8123 : `bench_console.py`) | `8090` |

Optionnels : `GEAR_RATIO` (defaut 49.79, confirme au banc), `MOTOR_MAX_RPM`
(defaut 300, 0..1380), `UI_HOST`/`UI_TOKEN` (hors loopback, jeton de 16
caracteres minimum), `OCCUPANCY_OCCUPIED_ENABLED` (reste `false` jusqu'au
jalon M6).

Pour une repetition a blanc sans aucun materiel, les variables du processus
priment sur `.env` :

```bash
MOTOR_BACKEND=sim ECG_SOURCE=sim .venv/bin/python -m src.local_panel
```

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

## Configuration Reference

| Variable             | Required | Default   | Description                             |
| -------------------- | -------- | --------- | --------------------------------------- |
| CONVEX_URL           | Yes      | -         | Convex deployment URL (.convex.site)    |
| MACHINE_API_KEY      | Yes      | -         | 64-character API key from dashboard     |
| BITALINO_MAC         | Yes      | -         | BITalino Bluetooth MAC address          |
| SAMPLE_RATE          | No       | 100       | Sample rate in Hz (1, 10, 100, or 1000) |
| BATCH_INTERVAL_MS    | No       | 1000      | How often to send data (ms)             |
| HEARTBEAT_INTERVAL_S | No       | 30        | Heartbeat interval (seconds)            |
| LOG_LEVEL            | No       | INFO      | Logging level (DEBUG, INFO, WARNING)    |
| BUFFER_DB_PATH       | No       | buffer.db | Path for offline data buffer            |

### Sample Rate Recommendations

| Rate    | Use Case                        | Data per minute |
| ------- | ------------------------------- | --------------- |
| 100 Hz  | Monitoring, basic visualization | 6,000 samples   |
| 250 Hz  | Detailed analysis               | 15,000 samples  |
| 500 Hz  | Clinical quality                | 30,000 samples  |
| 1000 Hz | Research, maximum detail        | 60,000 samples  |

**Recommendation:** Use **100 Hz** for most monitoring applications.

---

## Running as a Service

### Install Service

```bash
# Make install script executable
chmod +x scripts/install.sh

# Run install (requires sudo)
sudo ./scripts/install.sh
```

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
pip install -r requirements.txt
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

1. Create a session from the web UI first
2. Check the RPi client logs for errors
3. Verify machine status is "online" in dashboard
4. Check ECG electrodes are connected properly

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

## Development

### Run Tests

```bash
source venv/bin/activate
pytest
```

### Run with Debug Logging

```bash
python -m src.main --debug
```

### Project Structure

```
raspberry-pi/
├── src/
│   ├── __init__.py
│   ├── main.py              # Entry point
│   ├── config.py            # Configuration loading
│   ├── bitalino_client.py   # BITalino Bluetooth Classic client
│   ├── convex_client.py     # HTTP client for Convex
│   ├── data_buffer.py       # SQLite offline buffer
│   └── session_manager.py   # State machine for sessions
├── scripts/
│   ├── discover_devices.py  # Find BITalino devices
│   ├── test_bitalino.py     # Test BITalino connection
│   ├── pair_device.sh       # Pairing helper
│   ├── install.sh           # Service installer
│   └── anheart.service      # Systemd service file
├── tests/
│   ├── test_config.py
│   ├── test_bitalino.py
│   ├── test_convex_client.py
│   └── test_buffer.py
├── requirements.txt
├── .env.example
├── .env                     # Your configuration (not in git)
└── README.md
```

---

## ECG Data Format

The BITalino sends data as 10-bit ADC values (0-1023):

- **Baseline:** ~512 (when no signal)
- **Peaks:** Values above/below baseline represent ECG waveform
- **Sample rate:** Configurable (100 Hz recommended)

Each data batch contains:

```json
{
  "sessionId": "session_id",
  "timestamp": 1234567890123,
  "samples": [
    {
      "channel": "ECG",
      "values": [512, 515, 520, 890, 520, 510, ...]
    }
  ]
}
```

---

## Support

For issues and feature requests, please open an issue on GitHub.

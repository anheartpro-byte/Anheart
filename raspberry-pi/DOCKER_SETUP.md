# AnHeart Docker Setup - Complete Guide

This is the **production-ready** way to deploy AnHeart on Raspberry Pi.

---

## Step 1: Copy Files to Raspberry Pi

### From your computer, run:

```bash
# SSH into Pi and clean up old installation
ssh pi@anheart-pi.local "sudo rm -rf ~/anheart && mkdir ~/anheart"

# Copy project files (from your computer's project directory)
cd /home/mohamede/Documents/anheart
scp -r raspberry-pi pi@anheart-pi.local:~/anheart/

# SSH into Pi for remaining steps
ssh pi@anheart-pi.local
```

> **Note:** Replace `anheart-pi.local` with your Pi's IP address if `.local` doesn't work.
> Find IP on Pi with: `hostname -I`

---

## Step 2: Install Docker on Raspberry Pi

Run these commands **on the Raspberry Pi**:

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker (official script)
curl -fsSL https://get.docker.com | sh

# Add your user to docker group (so you don't need sudo)
sudo usermod -aG docker $USER

# IMPORTANT: Logout and login again for group change to take effect
exit
```

**SSH back in:**

```bash
ssh pi@anheart-pi.local
```

**Verify Docker works:**

```bash
docker --version
docker run hello-world
```

You should see "Hello from Docker!" message.

---

## Step 3: Install Docker Compose

```bash
# Install Docker Compose plugin
sudo apt install -y docker-compose-plugin

# Verify it works
docker compose version
```

---

## Step 4: Pair BITalino via Bluetooth (One-Time Setup)

Before Docker can connect to BITalino, you must pair it at the system level.

**Make sure BITalino is powered ON (LED blinking).**

```bash
# Start Bluetooth control
sudo bluetoothctl
```

Inside bluetoothctl, run these commands:

```
agent on
default-agent
scan on
```

**Wait 10-15 seconds** until you see your BITalino:

```
[NEW] Device XX:XX:XX:XX:XX:XX BITalino-XX-XX
```

**Write down the MAC address!** Then pair it:

```
pair XX:XX:XX:XX:XX:XX
```

When prompted for PIN, enter: **1234**

```
trust XX:XX:XX:XX:XX:XX
quit
```

**Verify pairing:**

```bash
bluetoothctl info XX:XX:XX:XX:XX:XX | grep -E "Paired|Trusted"
```

Should show:

```
Paired: yes
Trusted: yes
```

---

## Step 5: Configure the Application

```bash
cd ~/anheart/raspberry-pi

# Create config file from template
cp .env.example .env

# Edit with your values
nano .env
```

**Set these required values:**

```bash
# Your Convex deployment URL
CONVEX_URL=https://your-project-id.convex.cloud

# Your machine API key (64 characters, from web dashboard)
MACHINE_API_KEY=your-64-character-api-key-here

# BITalino MAC address (from Step 4)
BITALINO_MAC=XX:XX:XX:XX:XX:XX
```

**Save and exit:** `Ctrl+O`, `Enter`, `Ctrl+X`

---

## Step 6: Build and Run

```bash
cd ~/anheart/raspberry-pi

# Build the Docker image (takes 2-5 minutes first time)
docker compose build

# Start the service
docker compose up -d

# View logs (Ctrl+C to stop viewing)
docker compose logs -f
```

**You should see:**

```
anheart  | 2024-XX-XX 12:00:00 [INFO] Starting AnHeart RPi Client v1.0.0
anheart  | 2024-XX-XX 12:00:00 [INFO] Convex URL: https://...
anheart  | 2024-XX-XX 12:00:00 [INFO] BITalino MAC: XX:XX:XX:XX:XX:XX
anheart  | 2024-XX-XX 12:00:01 [INFO] Connected to BITalino
```

---

## Step 7: Verify Auto-Start on Boot

The container is configured to auto-start. Test it:

```bash
# Reboot the Pi
sudo reboot
```

After reboot, SSH back in and check:

```bash
ssh pi@anheart-pi.local
docker compose -f ~/anheart/raspberry-pi/docker-compose.yml logs --tail=20
```

---

## Common Commands Reference

| Action                | Command                                                   |
| --------------------- | --------------------------------------------------------- |
| Start service         | `docker compose up -d`                                    |
| Stop service          | `docker compose down`                                     |
| View logs             | `docker compose logs -f`                                  |
| View last 50 lines    | `docker compose logs --tail=50`                           |
| Restart               | `docker compose restart`                                  |
| Rebuild after changes | `docker compose build --no-cache && docker compose up -d` |
| Check status          | `docker compose ps`                                       |

> **Note:** Run these commands from `~/anheart/raspberry-pi/` directory

---

## Troubleshooting

### "Cannot connect to BITalino"

1. Check BITalino is powered on (LED blinking)
2. Verify it's paired:
   ```bash
   bluetoothctl info XX:XX:XX:XX:XX:XX | grep Paired
   ```
3. Re-pair if needed (repeat Step 4)
4. Check MAC address in `.env` is correct

### "Permission denied" for Docker

```bash
# Make sure you're in docker group
groups

# If 'docker' not listed, add yourself and re-login
sudo usermod -aG docker $USER
exit
# SSH back in
```

### Container keeps restarting

```bash
# Check logs for errors
docker compose logs --tail=100

# Common issues:
# - Wrong CONVEX_URL
# - Invalid MACHINE_API_KEY
# - BITalino not paired
```

### Bluetooth service not running

```bash
sudo systemctl status bluetooth

# If not running:
sudo systemctl enable bluetooth
sudo systemctl start bluetooth
```

### Check if Docker is running

```bash
sudo systemctl status docker

# If not running:
sudo systemctl start docker
```

### View detailed container info

```bash
docker inspect anheart
```

### Completely reset and rebuild

```bash
cd ~/anheart/raspberry-pi
docker compose down
docker system prune -f
docker compose build --no-cache
docker compose up -d
```

---

## Updating the Application

When you have new code:

**From your computer:**

```bash
cd /home/mohamede/Documents/anheart
scp -r raspberry-pi/src pi@anheart-pi.local:~/anheart/raspberry-pi/
```

**On the Raspberry Pi:**

```bash
cd ~/anheart/raspberry-pi
docker compose build
docker compose up -d
```

---

## Quick Setup Summary

```bash
# === ON YOUR COMPUTER ===
ssh pi@anheart-pi.local "sudo rm -rf ~/anheart && mkdir ~/anheart"
scp -r raspberry-pi pi@anheart-pi.local:~/anheart/
ssh pi@anheart-pi.local

# === ON RASPBERRY PI ===
# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
exit

# SSH back in
ssh pi@anheart-pi.local

# Pair BITalino
sudo bluetoothctl
# agent on -> default-agent -> scan on -> pair XX:XX:XX:XX:XX:XX -> 1234 -> trust XX:XX:XX:XX:XX:XX -> quit

# Configure
cd ~/anheart/raspberry-pi
cp .env.example .env
nano .env  # Set CONVEX_URL, MACHINE_API_KEY, BITALINO_MAC

# Run
docker compose build
docker compose up -d
docker compose logs -f
```

---

## Hardware Checklist

- [ ] Raspberry Pi powered on
- [ ] Pi connected to WiFi/Ethernet
- [ ] BITalino powered on (LED blinking)
- [ ] BITalino paired (Step 4 completed)
- [ ] ECG electrodes connected to BITalino
- [ ] `.env` configured with correct values

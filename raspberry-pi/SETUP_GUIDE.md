# Complete Setup Guide: Raspberry Pi + BITalino

This guide assumes you're starting from scratch with a new Raspberry Pi and BITalino device.

---

## Part 1: What You Need

### Hardware Checklist

- [ ] Raspberry Pi 4 or 5 (recommended: 4GB RAM or more)
- [ ] MicroSD card (32GB or larger, Class 10 recommended)
- [ ] USB-C power supply (5V 3A for Pi 4, 5V 5A for Pi 5)
- [ ] Micro HDMI to HDMI cable (Pi 4/5 uses micro HDMI)
- [ ] USB keyboard and mouse
- [ ] Monitor/TV with HDMI
- [ ] Ethernet cable OR WiFi network credentials
- [ ] BITalino Core BT (Bluetooth Low Energy version)
- [ ] ECG electrodes and cables for BITalino
- [ ] Computer (Windows/Mac/Linux) to prepare the SD card

### Software to Download (on your computer)

- Raspberry Pi Imager: https://www.raspberrypi.com/software/

---

## Part 2: Prepare the Raspberry Pi SD Card

### Step 1: Install Raspberry Pi Imager

1. Go to https://www.raspberrypi.com/software/
2. Download the Imager for your operating system
3. Install it on your computer

### Step 2: Flash the SD Card

1. Insert your microSD card into your computer
2. Open **Raspberry Pi Imager**
3. Click **"Choose Device"** → Select your Pi model (Pi 4 or Pi 5)
4. Click **"Choose OS"** → Select **"Raspberry Pi OS (64-bit)"**
5. Click **"Choose Storage"** → Select your SD card

6. Click the **gear icon (⚙️)** or **"Edit Settings"** to configure:

   **General tab:**
   - ✅ Set hostname: `anheart-pi`
   - ✅ Set username and password:
     - Username: `pi`
     - Password: (choose a secure password, write it down!)
   - ✅ Configure wireless LAN (if using WiFi):
     - SSID: Your WiFi name
     - Password: Your WiFi password
     - Country: Your country code (e.g., FR, US, MA)
   - ✅ Set locale settings:
     - Time zone: Your timezone
     - Keyboard layout: Your layout

   **Services tab:**
   - ✅ Enable SSH (Use password authentication)

7. Click **"Save"**
8. Click **"Write"** and confirm
9. Wait for it to complete (5-10 minutes)
10. Remove the SD card

---

## Part 3: First Boot of Raspberry Pi

### Step 1: Connect Everything

1. Insert the microSD card into the Raspberry Pi
2. Connect the HDMI cable to the Pi and your monitor
3. Connect keyboard and mouse via USB
4. Connect Ethernet cable (if not using WiFi)
5. **Last:** Connect the power supply

### Step 2: Wait for Boot

1. You'll see a rainbow screen, then the Raspberry Pi logo
2. The first boot takes 2-5 minutes (it expands the filesystem)
3. You'll eventually see the desktop or login prompt

### Step 3: Login

If you see a desktop, you're logged in automatically.

If you see a terminal:

```
anheart-pi login: pi
Password: (the password you set)
```

### Step 4: Open Terminal

- Click the **Terminal icon** in the top menu bar
- Or press `Ctrl + Alt + T`

---

## Part 4: Update the System

Run these commands in the terminal:

```bash
# Update package list
sudo apt update

# Upgrade all packages (this takes 5-15 minutes)
sudo apt upgrade -y

# Install required system packages
sudo apt install -y python3-pip python3-venv git bluetooth bluez

# Reboot to apply updates
sudo reboot
```

After reboot, log in again and open Terminal.

---

## Part 5: Enable Bluetooth

### Step 1: Check Bluetooth Status

```bash
# Check if Bluetooth service is running
sudo systemctl status bluetooth
```

You should see "active (running)" in green.

### Step 2: Start Bluetooth (if not running)

```bash
sudo systemctl enable bluetooth
sudo systemctl start bluetooth
```

---

## Part 6: Set Up the BITalino

### Step 1: Unbox BITalino

Your BITalino Core BT kit should contain:

- BITalino Core BT board
- Sensor cables (ECG, EMG, etc.)
- Electrodes
- Battery or USB power cable

### Step 2: Connect ECG Sensor

1. Connect the **ECG sensor cable** to the BITalino board
   - The cable has colored connectors that match the ports
   - ECG usually goes to **Channel A1** (analog input 1)

2. Attach the **3 electrodes** to the cable ends:
   - **Red (IN+)**: Right arm/chest
   - **Black (IN-)**: Left arm/chest
   - **White (REF)**: Reference (right leg or lower position)

### Step 3: Power On BITalino

1. Connect the battery or USB power to BITalino
2. You should see an **LED blinking** (indicates it's advertising via Bluetooth)
3. If using battery, check it's charged

### Step 4: Find BITalino MAC Address

On your Raspberry Pi terminal:

```bash
# Scan for Bluetooth devices (wait 10-15 seconds)
sudo bluetoothctl

# Inside bluetoothctl, type:
scan on
```

Wait until you see something like:

```
[NEW] Device XX:XX:XX:XX:XX:XX BITalino
```

**Write down the MAC address!** (the XX:XX:XX:XX:XX:XX part)

To exit bluetoothctl:

```
scan off
exit
```

---

## Part 7: Install AnHeart Application

### Step 1: Create Project Folder

```bash
# Create the anheart folder
mkdir -p ~/anheart
cd ~/anheart
```

### Step 2: Copy the Application

**Option A: If you have the files on a USB drive:**

```bash
# Mount USB (usually auto-mounted to /media/pi/USBNAME)
cp -r /media/pi/USBNAME/raspberry-pi ~/anheart/
```

**Option B: If you have the code in a Git repository:**

```bash
git clone https://github.com/YOUR_USERNAME/anheart.git ~/anheart
```

**Option C: Copy files via SCP from your computer:**

On your computer (not the Pi), open terminal:

```bash
# Replace with your Pi's IP address
scp -r /path/to/raspberry-pi pi@192.168.1.XXX:~/anheart/
```

### Step 3: Create Virtual Environment

```bash
cd ~/anheart/raspberry-pi

# Create virtual environment
python3 -m venv venv

# Activate it
source venv/bin/activate

# You should see (venv) at the start of your prompt
```

### Step 4: Install Python Dependencies

```bash
# Upgrade pip first
pip install --upgrade pip

# Install requirements
pip install -r requirements.txt
```

This might take 2-5 minutes.

---

## Part 8: Configure the Application

### Step 1: Create Configuration File

```bash
# Copy the example config
cp .env.example .env

# Edit the config file
nano .env
```

### Step 2: Fill in Your Values

Use the nano editor to change these values:

```bash
# Your Convex URL (from the web dashboard)
CONVEX_URL=https://your-project-id.convex.cloud

# Your machine API key (64 characters, from web dashboard)
MACHINE_API_KEY=paste-your-64-character-key-here

# The BITalino MAC address you found earlier
BITALINO_MAC=XX:XX:XX:XX:XX:XX
```

**Nano editor shortcuts:**

- Arrow keys to move
- Type to edit
- `Ctrl + O` then `Enter` to save
- `Ctrl + X` to exit

---

## Part 9: Test the Application

### Step 1: Test Configuration

```bash
# Make sure you're in the right folder with venv activated
cd ~/anheart/raspberry-pi
source venv/bin/activate

# Test if configuration loads
python -c "from src.config import get_config; c = get_config(); print(f'Config OK: {c.convex_url}')"
```

You should see: `Config OK: https://your-project.convex.cloud`

### Step 2: Test BITalino Discovery

```bash
# Make sure BITalino is powered on and blinking
python scripts/discover_devices.py
```

You should see your BITalino device listed.

### Step 3: Run the Application (Debug Mode)

```bash
python -m src.main --debug
```

You should see logs like:

```
2024-XX-XX 12:00:00 [INFO] Starting AnHeart RPi Client v1.0.0
2024-XX-XX 12:00:00 [INFO] Convex URL: https://...
2024-XX-XX 12:00:00 [INFO] BITalino MAC: XX:XX:XX:XX:XX:XX
2024-XX-XX 12:00:01 [INFO] Connected to server
2024-XX-XX 12:00:01 [INFO] Session manager started
```

**Press `Ctrl + C` to stop the application.**

---

## Part 10: Install as System Service (Auto-Start)

This makes the application start automatically when the Pi boots.

### Step 1: Run the Installer

```bash
cd ~/anheart/raspberry-pi
sudo ./scripts/install.sh
```

### Step 2: Enable Auto-Start

```bash
# Enable the service to start on boot
sudo systemctl enable anheart

# Start it now
sudo systemctl start anheart
```

### Step 3: Check Status

```bash
sudo systemctl status anheart
```

You should see "active (running)" in green.

### Step 4: View Logs

```bash
# Follow logs in real-time
journalctl -u anheart -f

# Press Ctrl+C to stop following
```

---

## Part 11: Using the System

### Normal Workflow

1. **Power on** Raspberry Pi (application starts automatically)
2. **Power on** BITalino device
3. **Attach electrodes** to patient
4. **Create session** in web dashboard
5. **Monitor** data in real-time on web dashboard
6. **End session** in web dashboard

### Useful Commands

```bash
# Check if service is running
sudo systemctl status anheart

# Stop the service
sudo systemctl stop anheart

# Start the service
sudo systemctl start anheart

# Restart the service
sudo systemctl restart anheart

# View recent logs
journalctl -u anheart -n 50

# View logs in real-time
journalctl -u anheart -f
```

---

## Part 12: Troubleshooting

### Problem: Cannot find BITalino in Bluetooth scan

1. Make sure BITalino is powered on (LED blinking)
2. Check battery is charged
3. Move Pi closer to BITalino (within 5 meters)
4. Try restarting Bluetooth:
   ```bash
   sudo systemctl restart bluetooth
   ```

### Problem: "CONVEX_URL environment variable is required"

1. Make sure `.env` file exists:
   ```bash
   ls -la ~/anheart/raspberry-pi/.env
   ```
2. Check the content:
   ```bash
   cat ~/anheart/raspberry-pi/.env
   ```

### Problem: "Invalid API key"

1. Check your API key in the web dashboard
2. Make sure you copied all 64 characters
3. No extra spaces before or after the key

### Problem: Connection timeout to BITalino

1. Check BITalino is powered on
2. Check MAC address is correct in `.env`
3. Check BITalino is not connected to another device
4. Try power-cycling the BITalino

### Problem: "Cannot reach server"

1. Check internet connection:
   ```bash
   ping google.com
   ```
2. Check WiFi is connected:
   ```bash
   iwconfig
   ```
3. Check Convex URL is correct in `.env`

### Problem: Service fails to start

1. Check logs for errors:
   ```bash
   journalctl -u anheart -n 100
   ```
2. Try running manually to see errors:
   ```bash
   cd ~/anheart/raspberry-pi
   source venv/bin/activate
   python -m src.main --debug
   ```

---

## Part 13: Finding Your Pi's IP Address

You need the IP address to:

- Access Pi remotely via SSH
- Copy files to/from Pi

### Method 1: On the Pi

```bash
hostname -I
```

### Method 2: From your router

Log into your router's admin page and look for connected devices.

### Connecting via SSH (from another computer)

```bash
ssh pi@192.168.1.XXX
```

---

## Quick Reference Card

| Action         | Command                            |
| -------------- | ---------------------------------- |
| Start service  | `sudo systemctl start anheart`     |
| Stop service   | `sudo systemctl stop anheart`      |
| Check status   | `sudo systemctl status anheart`    |
| View logs      | `journalctl -u anheart -f`         |
| Edit config    | `nano ~/anheart/raspberry-pi/.env` |
| Scan Bluetooth | `sudo bluetoothctl` then `scan on` |
| Check WiFi     | `iwconfig`                         |
| Check IP       | `hostname -I`                      |
| Reboot Pi      | `sudo reboot`                      |
| Shutdown Pi    | `sudo shutdown now`                |

---

## Electrode Placement for ECG

```
        ┌─────────┐
        │  HEAD   │
        └─────────┘
             │
    ┌────────┼────────┐
    │        │        │
   [R]───────┼───────[L]    R = Red (IN+) - Right side
    │        │        │     L = Black (IN-) - Left side
    │        │        │
    │        │        │
    └────────┼────────┘
             │
            [W]             W = White (REF) - Reference
             │
        ┌─────────┐
        │  LEGS   │
        └─────────┘
```

**Standard Placement:**

- **Red (IN+)**: Right arm or right side of chest
- **Black (IN-)**: Left arm or left side of chest
- **White (REF)**: Right leg or lower right abdomen

**Tips:**

- Clean skin with alcohol before applying electrodes
- Shave hair if necessary for better contact
- Use fresh electrodes (gel dries out over time)
- Ensure good contact (press firmly when applying)

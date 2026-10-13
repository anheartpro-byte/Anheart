#!/usr/bin/env bash
# Install the AnHeart operator console on a Raspberry Pi and start it at boot.
#
#     sudo bash scripts/install.sh                (from raspberry-pi/, on the Pi)
#     sudo bash scripts/install.sh --simulation   (no hardware: simulated drive and ECG)
#
# One path, described in docs/pi-image.md: the console runs in a Docker image
# built here from this directory, and systemd (scripts/anheart.service) starts
# that image at boot. Nothing else is maintained: no venv on the host, no
# Compose file.
#
# Safe to run again. A second run changes nothing that is already in place and
# restarts nothing that did not change. In particular:
#
#   * an existing /etc/anheart/anheart.env is never rewritten and never shown:
#     it holds the machine's API key. Only its owner and mode are set again.
#   * the console runs as the account `anheart`, never as root, and what it
#     wrote stays its own: on a machine installed before that was so, the
#     owner and the modes of what is under /var/lib/anheart are corrected, and
#     no content is touched (scripts/harden.sh, which this script runs).
#   * a console that is running is restarted only when its image or its unit
#     changed, and only when it says it is at rest. Replacing the software of a
#     machine that turns is not something a script decides. That holds for a
#     console the service did not start too (a container named `anheart` run
#     by hand, or left by an older way of running the image).
#   * the time service and the system journal are set by two small files of
#     their own; changing one restarts that system service, never the console.
#
# This script never talks to the drive and starts no session. The console it
# starts comes up at rest, as it does after every start (docs/pi-image.md).
#
# ANHEART_ROOT, when set, is prepended to every path this script writes under
# /etc and /var/lib: tests/test_pi_install.py runs it against a scratch
# directory. It is the only test hook; leave it unset on a machine.
set -euo pipefail

readonly SERVICE="anheart"
readonly ACCOUNT="anheart"
# The group of the serial devices on Debian: the USB-RS485 adapter of the drive
# (ttyUSB*, or the USB device itself, see install_device_rule) and the
# BITalino's Bluetooth serial node (rfcomm*).
readonly DEVICE_GROUP="dialout"
readonly IMAGE_NAME="anheart-console"
readonly CONTAINER="anheart"

# The system this script is written for (docs/pi-image.md, "Versions figées").
readonly OS_CODENAME="bookworm"
readonly OS_REFERENCE="2026-10-06"
# chrony: the one time service of the machine (docs/pi-image.md, "L'heure").
# socat: the relay of the unit's watchdog (docs/pi-image.md, "Le chien de garde").
readonly PACKAGES=(docker.io bluez libusb-1.0-0 ca-certificates curl chrony socat)

readonly ROOT="${ANHEART_ROOT:-}"
readonly CONFIG_DIR="$ROOT/etc/anheart"
readonly ENV_FILE="$CONFIG_DIR/anheart.env"
readonly STATE_DIR="$ROOT/var/lib/anheart"
readonly UNIT_DIR="$ROOT/etc/systemd/system"
readonly UNIT_FILE="$UNIT_DIR/$SERVICE.service"
readonly DROP_IN_DIR="$UNIT_DIR/$SERVICE.service.d"
readonly DROP_IN="$DROP_IN_DIR/10-version.conf"
readonly ACCOUNT_DROP_IN="$DROP_IN_DIR/20-account.conf"
# What the unit runs on the machine itself before the console (ExecStartPre).
readonly BIND_HELPER="$ROOT/usr/local/lib/anheart/bind-bitalino"
readonly DEVICE_RULE="$ROOT/etc/udev/rules.d/70-anheart-drive.rules"
readonly CHRONY_CONF="$ROOT/etc/chrony/conf.d/anheart.conf"
readonly JOURNALD_CONF="$ROOT/etc/systemd/journald.conf.d/anheart.conf"
# Where the kernel lists the real-time clocks it found, if any.
readonly RTC_DIR="$ROOT/sys/class/rtc"
# systemd's own settings: the hardware watchdog of the Pi (install_watchdog).
readonly MANAGER_DROP_IN="$ROOT/etc/systemd/system.conf.d/10-anheart-watchdog.conf"

# How long the start test waits for GET /healthz, in seconds. The first start
# on a Pi imports the whole signal-processing stack from a cold SD card.
readonly HEALTH_TIMEOUT_S="${ANHEART_HEALTH_TIMEOUT_S:-180}"
readonly HEALTH_PERIOD_S=2

TREE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TREE
# The steps that close the machine, and the only place they are written.
readonly HARDEN="$TREE/scripts/harden.sh"

simulation=false
# Whether the image or the unit differs from what was installed before.
changed=false
# Whether this run started or restarted the console.
started=false

say()  { printf '== %s\n' "$1"; }
note() { printf '   %s\n' "$1"; }
warn() { printf '   WARNING: %s\n' "$1" >&2; }
die()  { printf 'install.sh: %s\n' "$1" >&2; exit 1; }

usage() {
    cat <<'EOF'
usage: sudo bash scripts/install.sh [--simulation]

  --simulation   a NEW /etc/anheart/anheart.env is written for a simulated
                 drive and a simulated ECG (MOTOR_BACKEND=sim, ECG_SOURCE=sim):
                 the console starts with no hardware at all. An existing file
                 is left as it is.
EOF
}

# --- What this machine is ---------------------------------------------------

require_root() {
    [ "$(id -u)" -eq 0 ] || die "run it as root: sudo bash scripts/install.sh"
}

check_platform() {
    say "System"
    local release="$ROOT/etc/os-release" codename=""
    [ -r "$release" ] || die "$release is missing: this is not a Debian system"
    codename="$(sed -n 's/^VERSION_CODENAME=//p' "$release" | tr -d '"')"
    [ "$codename" = "$OS_CODENAME" ] \
        || die "written for Raspberry Pi OS (64-bit) on Debian $OS_CODENAME, found '${codename:-unknown}' (docs/pi-image.md)"
    note "Debian $codename"

    local arch
    arch="$(dpkg --print-architecture)"
    if [ "$arch" = "arm64" ]; then
        note "architecture arm64"
    else
        warn "architecture $arch, not arm64: fine for a trial in simulation, not a Raspberry Pi"
    fi

    local issue="$ROOT/etc/rpi-issue" reference=""
    if [ -r "$issue" ]; then
        reference="$(sed -n 's/^Raspberry Pi reference //p' "$issue" | head -n 1)"
        if [ "$reference" = "$OS_REFERENCE" ]; then
            note "Raspberry Pi OS reference $reference"
        else
            warn "Raspberry Pi OS reference '${reference:-unknown}', this version was checked on $OS_REFERENCE"
        fi
    else
        warn "no /etc/rpi-issue: this is not a Raspberry Pi OS image"
    fi
}

read_version() {
    local file="$TREE/VERSION"
    [ -r "$file" ] || die "$file is missing: the image would have no version"
    version="$(cat "$file")"
    version="${version%$'\r'}"
    # Also what Docker accepts as a tag: no '+', no '/'.
    [[ "$version" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] \
        || die "$file does not hold a usable version"
    readonly version
    readonly image="$IMAGE_NAME:$version"
    say "Console version $version"
}

# --- The .env of the machine --------------------------------------------------

# The value of KEY in the machine's configuration, without quotes or a trailing
# comment. Never printed by this script: it is only ever captured.
env_value() {
    [ -r "$ENV_FILE" ] || return 0
    grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- \
        | sed -E 's/[[:space:]]+#.*$//; s/^["'\'']//; s/["'\'']$//' || true
}

console_url() {
    local port host scheme=http
    port="$(env_value UI_PORT)"
    host="$(env_value UI_HOST)"
    case "$host" in
        '' | 0.0.0.0) host=127.0.0.1 ;;
        ::) host='[::1]' ;;
        *:*) host="[$host]" ;;
    esac
    [ -z "$(env_value UI_TLS_CERT)" ] || scheme=https
    printf '%s://%s:%s' "$scheme" "$host" "${port:-8080}"
}

# GET a path of the console. The token, when the machine has one, goes through
# a curl configuration read on standard input: never on a command line.
console_get() {
    local token
    local trust=()
    token="$(env_value UI_TOKEN)"
    if [ -n "$(env_value UI_TLS_CERT)" ]; then
        trust=(--cacert "$STATE_DIR/tls/authority.crt")
    fi
    if [ -n "$token" ]; then
        printf 'header = "x-anheart-token: %s"\n' "$token" \
            | curl --silent --fail --max-time 3 ${trust[@]+"${trust[@]}"} --config - "$(console_url)$1"
    else
        curl --silent --fail --max-time 3 ${trust[@]+"${trust[@]}"} "$(console_url)$1"
    fi
}

# --- Never under a machine that turns -----------------------------------------

service_active() { systemctl is-active --quiet "$SERVICE"; }

# A console running in a container the service does not account for: started by
# hand, or left by an older way of running the image. The unit stops any
# container of that name before it starts its own, so this one is asked first
# too. Docker may not be installed yet: then there is none.
container_running() {
    [ "$(docker container inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)" = "true" ]
}

console_running() { service_active || container_running; }

require_idle_console() {
    local status
    status="$(console_get /api/status 2>/dev/null || true)"
    case "$status" in
        *'"run_state":"idle"'*) note "the running console is at rest" ;;
        *) die "the console is running and does not say it is at rest: nothing is restarted. End the session at the machine, or stop the console on purpose (sudo systemctl stop $SERVICE; for a console the service did not start: sudo docker stop -t 60 $CONTAINER), then run this script again." ;;
    esac
}

# --- Steps --------------------------------------------------------------------

install_packages() {
    say "System packages"
    local package missing=()
    for package in "${PACKAGES[@]}"; do
        if [ "$(dpkg-query --show --showformat='${db:Status-Status}' "$package" 2>/dev/null || true)" != "installed" ]; then
            missing+=("$package")
        fi
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        note "installing: ${missing[*]}"
        DEBIAN_FRONTEND=noninteractive apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends "${missing[@]}"
    fi
    for package in "${PACKAGES[@]}"; do
        note "$package $(dpkg-query --show --showformat='${Version}' "$package")"
    done
    systemctl enable --now docker.service
    # No adapter, no service: the console then shows the ECG link as down.
    systemctl enable --now bluetooth.service \
        || warn "the bluetooth service did not start (no adapter?)"
}

# system_file TARGET [MODE]: its content is read on standard input. The file
# is replaced only when the content differs, and the exit status says which: 0
# written, 1 unchanged. MODE is 0644 unless given. It never touches `changed`:
# that one is about the console's image and unit, and decides whether the
# console is restarted.
# Called as the condition of an `if`, where a failing command does not stop
# the script by itself: each step that can fail says so and stops it.
system_file() {
    local target="$1" mode="${2:-0644}" directory draft
    directory="$(dirname "$target")"
    mkdir -p "$directory" || die "cannot create $directory"
    draft="$(mktemp "$directory/.anheart.XXXXXX")" || die "cannot write under $directory"
    cat > "$draft" || die "cannot write under $directory"
    if [ -e "$target" ] && cmp -s "$draft" "$target"; then
        rm -f "$draft"
        note "$target unchanged"
        return 1
    fi
    chmod "$mode" "$draft" && mv "$draft" "$target" || die "cannot write $target"
    note "$target written"
}

# The wall clock only dates what the console writes (records, journal); the
# console never times anything on it. A Raspberry Pi has no battery-backed
# clock: after a power cut it restarts on the time of its last shutdown, and
# chrony corrects it once the network is back. Until then the console says its
# clock is not trusted (it asks the kernel, where chrony publishes its state).
configure_clock() {
    say "Clock"
    if system_file "$CHRONY_CONF" <<'EOF'
# Written by scripts/install.sh (docs/pi-image.md, "L'heure"). The two
# directives the console's reading of the clock relies on, stated here so that
# an edit of the distribution's own file does not change them unnoticed.

# Step the clock when it is more than 1 s off at one of the first three
# updates after chronyd started: a Pi without a real-time clock restarts on the
# time of its last shutdown, however long it was off.
makestep 1 3

# Once synchronised, tell the kernel so (the console reads it there) and keep
# a real-time clock, when the machine has one, on the system time: the kernel
# copies it there every 11 minutes.
rtcsync
EOF
    then
        # Read at its start only. Nothing to restart when it is not running.
        systemctl try-restart chrony.service || true
    fi
    systemctl enable --now chrony.service \
        || warn "the time service (chrony) did not start: the console will say its clock is not trusted"
    local rtc="$RTC_DIR/rtc0"
    if [ -e "$rtc" ]; then
        note "real-time clock found: rtc0 ($(cat "$rtc/name" 2>/dev/null || echo unnamed)). The system reads it at power-on and chrony keeps it on time"
    else
        note "no real-time clock on this machine: after a power cut the time is wrong until the network is back, and the console says so"
    fi
}

# Kept on the disk, so the journal survives a restart; bounded, so it cannot
# fill the card the session records are written on.
configure_journal() {
    say "System journal"
    if system_file "$JOURNALD_CONF" <<'EOF'
# Written by scripts/install.sh (docs/pi-image.md, "Le journal du système").
[Journal]
Storage=persistent
SystemMaxUse=200M
EOF
    then
        # journald reads its configuration at its start. The console's output
        # stays connected across this restart: systemd holds the stream.
        systemctl restart systemd-journald.service
        # What was logged since power-on moves to the disk with it.
        journalctl --flush || warn "the journal of this boot was not moved to the disk"
    fi
}

create_account() {
    say "Account and directories"
    if getent passwd "$ACCOUNT" > /dev/null; then
        note "account $ACCOUNT exists"
    else
        useradd --system --user-group --home-dir /var/lib/anheart --no-create-home \
            --shell /usr/sbin/nologin --groups "$DEVICE_GROUP" "$ACCOUNT"
        note "account $ACCOUNT created (system account, no login, group $DEVICE_GROUP for the serial devices)"
    fi
}

# Made here; given to the account and closed by scripts/harden.sh (protect).
#   data          local profiles (profiles.local.json)
#   records       session records, the logbook, the console's restart state
#   data/records  where the unit mounts `records` inside `data`: made here so
#                 that it is the account's, and not root's as Docker makes it
create_directories() {
    mkdir -p "$STATE_DIR/data/records" "$STATE_DIR/records" "$STATE_DIR/tls" "$CONFIG_DIR"
    note "$STATE_DIR/data, $STATE_DIR/records, $CONFIG_DIR"
}

write_env() {
    say "Configuration"
    if [ -e "$ENV_FILE" ]; then
        note "$ENV_FILE exists: left as it is"
        if $simulation; then
            warn "--simulation only applies to a new file; this one was not changed"
        fi
    else
        local template="$TREE/.env.pi.example" draft
        [ -r "$template" ] || die "$template is missing"
        draft="$(umask 077 && mktemp "$CONFIG_DIR/.anheart.env.XXXXXX")"
        if $simulation; then
            sed -E 's/^MOTOR_BACKEND=.*/MOTOR_BACKEND=sim/; s/^ECG_SOURCE=.*/ECG_SOURCE=sim/' \
                "$template" > "$draft"
        else
            cat "$template" > "$draft"
        fi
        mv "$draft" "$ENV_FILE"
        note "$ENV_FILE written from .env.pi.example"
        if $simulation; then
            note "SIMULATION: simulated drive and simulated ECG, the motor will not turn"
        else
            note "fill it in (MACHINE_API_KEY, MOTOR_PORT, BITALINO_MAC, measured radii), then: sudo systemctl restart $SERVICE"
        fi
    fi
    # The file holds the machine's API key and the limits the console applies.
    # It is root's, mode 0600, and scripts/harden.sh puts that back at every
    # run (protect): root's Docker client reads it for the console, which can
    # neither read it on the machine nor rewrite its own limits there.
    # `docker run --env-file` hands the console everything after the `=`: a
    # quote or a trailing comment would be part of the value. Keys only are
    # named here, never a value.
    local odd
    odd="$(grep -E "^[A-Za-z_][A-Za-z0-9_]*=(.*[[:space:]]#.*|[\"'].*|.*[\"'])\$" "$ENV_FILE" \
        | cut -d= -f1 | tr '\n' ' ' || true)"
    if [ -n "$odd" ]; then
        warn "quotes or a trailing comment are read as part of the value of: ${odd}(write KEY=value and nothing else on the line)"
    fi
}

# The Schneider USB-RS485 cable (VID 16de, PID 0003) has no serial driver in the
# kernel: the console then opens the USB device itself, through pyftdi
# (MOTOR_PORT=ftdi://schneider:rs485/1). A USB device belongs to root and
# nobody else may write to it; this rule gives that one cable to the group of
# the serial devices, like a ttyUSB node. udev reads it when a cable is plugged
# in; one already plugged in is asked again here.
install_device_rule() {
    say "Drive cable"
    if system_file "$DEVICE_RULE" <<EOF
# Written by scripts/install.sh (docs/pi-image.md, "Sous quel compte").
# The Schneider USB-RS485 cable of the drive, for the console's account.
SUBSYSTEM=="usb", ATTR{idVendor}=="16de", ATTR{idProduct}=="0003", GROUP="$DEVICE_GROUP", MODE="0660"
EOF
    then
        if command -v udevadm > /dev/null 2>&1; then
            udevadm control --reload \
                || warn "udev did not read its rules again: the cable is the group's at the next start of the machine"
            udevadm trigger --subsystem-match=usb --attr-match=idVendor=16de \
                || warn "the cable already plugged in was not asked again: unplug it and plug it back"
        else
            note "no udevadm on this machine: the rule is read at its next start"
        fi
    fi
}

# The account as it must be, what belongs to whom and who may read it, and no
# package update that nobody asked for: the steps of scripts/harden.sh that
# every install applies. Each looks first and changes only what differs.
protect() {
    say "Protection (scripts/harden.sh)"
    [ -r "$HARDEN" ] || die "$HARDEN is missing: this copy of the sources is incomplete"
    bash "$HARDEN" account files updates
}

image_id() { docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true; }

build_image() {
    say "Image $image"
    local before after
    before="$(image_id)"
    [ -n "$before" ] || note "first build: several minutes on a Raspberry Pi, and it needs the network"
    docker build \
        --tag "$image" \
        --label "org.opencontainers.image.version=$version" \
        "$TREE"
    after="$(image_id)"
    [ -n "$after" ] || die "the image was not built"
    if [ "$before" != "$after" ]; then
        changed=true
        note "image built: $after"
    else
        note "image unchanged: $after"
    fi
}

# install_file SOURCE TARGET: copied only when the content differs.
install_file() {
    if [ -e "$2" ] && cmp -s "$1" "$2"; then
        note "$2 unchanged"
    else
        cp "$1" "$2"
        chmod 0644 "$2"
        changed=true
        note "$2 written"
    fi
}

# The hardware watchdog of the Pi (bcm2835_wdt, /dev/watchdog0): armed and fed
# by systemd itself, so that a system that freezes whole, systemd included, is
# reset by the hardware. 15 s is the longest that watchdog can count. This is
# not what restarts a frozen console: that is the unit's own WatchdogSec.
# The file is read by systemd when it starts or reloads its configuration: at
# the latest at the next boot of this machine. Writing it changes nothing for
# the console, which is neither restarted nor marked as changed.
install_watchdog() {
    say "Hardware watchdog"
    local draft
    mkdir -p "$(dirname "$MANAGER_DROP_IN")"
    draft="$(mktemp "$(dirname "$MANAGER_DROP_IN")/.anheart-watchdog.XXXXXX")"
    cat > "$draft" <<'EOF'
# Written by scripts/install.sh (docs/pi-image.md, "Le chien de garde").
[Manager]
RuntimeWatchdogSec=15s
EOF
    if [ -e "$MANAGER_DROP_IN" ] && cmp -s "$draft" "$MANAGER_DROP_IN"; then
        rm -f "$draft"
        note "$MANAGER_DROP_IN unchanged"
    else
        chmod 0644 "$draft"
        mv "$draft" "$MANAGER_DROP_IN"
        note "$MANAGER_DROP_IN written: armed at the next boot at the latest"
    fi
    if [ -e "$ROOT/dev/watchdog" ]; then
        note "/dev/watchdog is present"
    else
        warn "no /dev/watchdog on this machine: nothing resets it if the whole system freezes"
    fi
}

# A number of this machine, or nothing: never a name, never a sign.
number() {
    case "$1" in
        '' | *[!0-9]*) return 1 ;;
        *) printf '%s' "$1" ;;
    esac
}

install_unit() {
    say "Service $SERVICE"
    # What the unit runs on the machine before the console. Read at the next
    # start of the console: writing it restarts nothing.
    local helper="$TREE/scripts/pi/bind-bitalino.sh"
    [ -r "$helper" ] || die "$helper is missing: this copy of the sources is incomplete"
    system_file "$BIND_HELPER" 0755 < "$helper" || true
    mkdir -p "$DROP_IN_DIR"
    install_file "$TREE/scripts/$SERVICE.service" "$UNIT_FILE"
    local draft
    draft="$(mktemp "$DROP_IN_DIR/.version.XXXXXX")"
    cat > "$draft" <<EOF
# Written by scripts/install.sh. The version is the content of VERSION when
# the image below was built; \`systemctl status $SERVICE\` shows it.
[Unit]
Description=Console opérateur Anheart, version $version

[Service]
Environment=ANHEART_IMAGE=$image
EOF
    install_file "$draft" "$DROP_IN"
    rm -f "$draft"
    # The console's account, as numbers: Docker looks a name up in the image,
    # which does not know the accounts of this machine.
    local user group devices
    user="$(number "$(id -u "$ACCOUNT")")" \
        || die "the account $ACCOUNT has no number on this machine"
    group="$(number "$(id -g "$ACCOUNT")")" \
        || die "the account $ACCOUNT has no group on this machine"
    devices="$(number "$(getent group "$DEVICE_GROUP" | cut -d: -f3)")" \
        || die "the group $DEVICE_GROUP is missing: the console could open no serial device"
    draft="$(mktemp "$DROP_IN_DIR/.account.XXXXXX")"
    cat > "$draft" <<EOF
# Written by scripts/install.sh. The account the console runs under in its
# container ($ACCOUNT), and the group of the serial devices ($DEVICE_GROUP),
# as the numbers they have on this machine.
[Service]
Environment=ANHEART_ACCOUNT=$user:$group
Environment="ANHEART_GROUPS=--group-add $devices"
EOF
    install_file "$draft" "$ACCOUNT_DROP_IN"
    rm -f "$draft"
    systemctl daemon-reload
    systemctl enable "$SERVICE.service"
}

start_service() {
    if service_active && ! $changed; then
        note "$SERVICE is running and nothing changed: left alone"
        return
    fi
    # Asked again, right before acting: the build above took minutes.
    if console_running; then
        require_idle_console
    fi
    if service_active; then
        # Stopped, then started, and not restarted in one go: a console
        # installed before it stopped running as root writes its last file as
        # root while it stops. What it left is given to the account before the
        # new one, which is no longer root, starts and reads it.
        note "restarting $SERVICE on the new image or unit"
        systemctl stop "$SERVICE.service"
        bash "$HARDEN" files \
            || warn "the console's files could not be checked again: sudo bash scripts/harden.sh files"
        systemctl start "$SERVICE.service"
    else
        # The unit first stops and removes a container named $CONTAINER, if one
        # is there: the console that was just found at rest.
        note "starting $SERVICE"
        systemctl start "$SERVICE.service"
    fi
    started=true
}

start_test() {
    say "Start test"
    local attempts=$((HEALTH_TIMEOUT_S / HEALTH_PERIOD_S)) attempt health=""
    for ((attempt = 0; attempt < attempts; attempt++)); do
        health="$(console_get /healthz 2>/dev/null || true)"
        case "$health" in *'"status":"ok"'*) break ;; esac
        health=""
        service_active || break
        sleep "$HEALTH_PERIOD_S"
    done
    if [ -z "$health" ]; then
        journalctl --unit "$SERVICE" --lines 30 --no-pager >&2 || true
        die "the console did not answer $(console_url)/healthz: see the journal above (journalctl -u $SERVICE)"
    fi
    note "GET /healthz answers"
    if $started; then
        # A console this script just started must be at rest: it never starts
        # a session by itself, at boot or after a restart.
        local status
        status="$(console_get /api/status 2>/dev/null || true)"
        case "$status" in
            *'"run_state":"idle"'*) note "the console is at rest" ;;
            *) die "the console answers but does not say it is at rest after its start" ;;
        esac
    fi
    systemctl is-enabled --quiet "$SERVICE.service" || die "$SERVICE is not enabled at boot"
    note "$SERVICE is enabled at boot"
    systemctl status "$SERVICE.service" --no-pager --lines 0 | head -n 6 || true
}

# --- Main -----------------------------------------------------------------------

for argument in "$@"; do
    case "$argument" in
        --simulation) simulation=true ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

require_root
check_platform
read_version
if console_running; then
    say "A console is running"
    require_idle_console
fi
install_packages
configure_clock
configure_journal
create_account
create_directories
write_env
install_device_rule
protect
build_image
install_watchdog
install_unit
start_service
start_test

say "Installed: console $version"
note "status : systemctl status $SERVICE"
note "journal: journalctl -u $SERVICE -f"
note "page   : $(console_url)/ (on this machine)"

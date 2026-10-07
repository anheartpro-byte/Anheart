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
#   * a console that is running is restarted only when its image or its unit
#     changed, and only when it says it is at rest. Replacing the software of a
#     machine that turns is not something a script decides.
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
readonly IMAGE_NAME="anheart-console"

# The system this script is written for (docs/pi-image.md, "Versions figées").
readonly OS_CODENAME="bookworm"
readonly OS_REFERENCE="2026-10-06"
readonly PACKAGES=(docker.io bluez libusb-1.0-0 ca-certificates curl)

readonly ROOT="${ANHEART_ROOT:-}"
readonly CONFIG_DIR="$ROOT/etc/anheart"
readonly ENV_FILE="$CONFIG_DIR/anheart.env"
readonly STATE_DIR="$ROOT/var/lib/anheart"
readonly UNIT_DIR="$ROOT/etc/systemd/system"
readonly UNIT_FILE="$UNIT_DIR/$SERVICE.service"
readonly DROP_IN_DIR="$UNIT_DIR/$SERVICE.service.d"
readonly DROP_IN="$DROP_IN_DIR/10-version.conf"

# How long the start test waits for GET /healthz, in seconds. The first start
# on a Pi imports the whole signal-processing stack from a cold SD card.
readonly HEALTH_TIMEOUT_S="${ANHEART_HEALTH_TIMEOUT_S:-180}"
readonly HEALTH_PERIOD_S=2

TREE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TREE

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
    local port
    port="$(env_value UI_PORT)"
    # The console is asked on the machine's own loopback, whatever UI_HOST is:
    # a wildcard bind answers there too.
    printf 'http://127.0.0.1:%s' "${port:-8080}"
}

# GET a path of the console. The token, when the machine has one, goes through
# a curl configuration read on standard input: never on a command line.
console_get() {
    local token
    token="$(env_value UI_TOKEN)"
    if [ -n "$token" ]; then
        printf 'header = "x-anheart-token: %s"\n' "$token" \
            | curl --silent --fail --max-time 3 --config - "$(console_url)$1"
    else
        curl --silent --fail --max-time 3 "$(console_url)$1"
    fi
}

# --- Never under a machine that turns -----------------------------------------

service_active() { systemctl is-active --quiet "$SERVICE"; }

require_idle_console() {
    local status
    status="$(console_get /api/status 2>/dev/null || true)"
    case "$status" in
        *'"run_state":"idle"'*) note "the running console is at rest" ;;
        *) die "the console is running and does not say it is at rest: nothing is restarted. End the session at the machine (or stop the service on purpose: sudo systemctl stop $SERVICE), then run this script again." ;;
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

create_account() {
    say "Account and directories"
    if getent passwd "$ACCOUNT" > /dev/null; then
        note "account $ACCOUNT exists"
    else
        useradd --system --user-group --home-dir /var/lib/anheart --no-create-home \
            --shell /usr/sbin/nologin "$ACCOUNT"
        note "account $ACCOUNT created (system account, no login)"
    fi
}

# directory MODE PATH OWNER: created if missing, owner and mode set every time.
directory() {
    mkdir -p "$2"
    chown "$3" "$2"
    chmod "$1" "$2"
    note "$2 ($3, mode $1)"
}

create_directories() {
    directory 0750 "$STATE_DIR" "$ACCOUNT:$ACCOUNT"
    # Local profiles (profiles.local.json).
    directory 0750 "$STATE_DIR/data" "$ACCOUNT:$ACCOUNT"
    # Session records: private, like the console creates them.
    directory 0700 "$STATE_DIR/records" "$ACCOUNT:$ACCOUNT"
    directory 0755 "$CONFIG_DIR" "root:root"
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
    # The file holds the machine's API key: readable by root only, every run.
    chown root:root "$ENV_FILE"
    chmod 0600 "$ENV_FILE"
    note "$ENV_FILE (root:root, mode 0600)"
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

install_unit() {
    say "Service $SERVICE"
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
    systemctl daemon-reload
    systemctl enable "$SERVICE.service"
}

start_service() {
    if ! service_active; then
        note "starting $SERVICE"
        systemctl start "$SERVICE.service"
        started=true
    elif $changed; then
        # Asked again, right before acting: the build above took minutes.
        require_idle_console
        note "restarting $SERVICE on the new image or unit"
        systemctl restart "$SERVICE.service"
        started=true
    else
        note "$SERVICE is running and nothing changed: left alone"
    fi
}

start_test() {
    say "Start test"
    local attempts=$((HEALTH_TIMEOUT_S / HEALTH_PERIOD_S)) attempt health=""
    for ((attempt = 0; attempt < attempts; attempt++)); do
        health="$(curl --silent --fail --max-time 3 "$(console_url)/healthz" 2>/dev/null || true)"
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
if service_active; then
    say "A console is running"
    require_idle_console
fi
install_packages
create_account
create_directories
write_env
build_image
install_unit
start_service
start_test

say "Installed: console $version"
note "status : systemctl status $SERVICE"
note "journal: journalctl -u $SERVICE -f"
note "page   : $(console_url)/ (on this machine)"

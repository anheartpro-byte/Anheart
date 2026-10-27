#!/usr/bin/env bash
# Preflight for the AnHeart console on the Raspberry Pi. Read-only: it checks,
# it changes nothing, and it never talks to the drive.
#
#     sudo bash scripts/pi/preflight.sh     (from raspberry-pi/, on the Pi)
#
# Exit code 0 when nothing blocks the console, 1 otherwise.
#
# The configuration read is the machine's, /etc/anheart/anheart.env, written by
# scripts/install.sh and readable by root only (hence sudo). In a development
# tree that has its own .env, that file is read instead; ANHEART_ENV_FILE names
# another one.
set -uo pipefail

cd "$(dirname "$0")/../.." || exit 1

failures=0
warnings=0
ok()   { printf '  ok    %s\n' "$1"; }
warn() { printf '  WARN  %s\n' "$1"; warnings=$((warnings + 1)); }
fail() { printf '  FAIL  %s\n' "$1"; failures=$((failures + 1)); }

env_file="${ANHEART_ENV_FILE:-}"
if [ -z "$env_file" ]; then
    if [ -f .env ]; then env_file=".env"; else env_file="/etc/anheart/anheart.env"; fi
fi

value() {
    # The value of KEY in the configuration, without quotes or a trailing comment.
    grep -E "^$1=" "$env_file" 2>/dev/null | tail -1 | cut -d= -f2- | sed -E 's/[[:space:]]+#.*$//; s/^["'\'']//; s/["'\'']$//'
}

echo "1. Docker"
if command -v docker >/dev/null 2>&1; then
    ok "docker: $(docker --version)"
    if docker info >/dev/null 2>&1; then
        ok "the daemon answers for this user"
    else
        fail "the daemon does not answer (run this script with sudo)"
    fi
else
    fail "docker missing (sudo bash scripts/install.sh)"
fi

echo "2. Configuration ($env_file)"
if [ ! -r "$env_file" ]; then
    fail "$env_file missing or unreadable (sudo bash scripts/install.sh writes it; it is readable by root only)"
else
    ok "$env_file present"
    for key in MOTOR_BACKEND ECG_SOURCE ARM_RADIUS_M; do
        if [ -n "$(value "$key")" ]; then ok "$key=$(value "$key")"; else fail "$key is required"; fi
    done
    host="$(value UI_HOST)"
    if [ -n "$host" ] && [ "$host" != "127.0.0.1" ] && [ "$host" != "localhost" ]; then
        token="$(value UI_TOKEN)"
        if [ "${#token}" -lt 16 ]; then fail "UI_HOST=$host needs a UI_TOKEN of at least 16 characters"; fi
        # Off loopback the console serves TLS or does not start. Only that the
        # two keys are set is judged here: their paths are the console's own.
        if [ -z "$(value UI_TLS_CERT)" ] || [ -z "$(value UI_TLS_KEY)" ]; then
            fail "UI_HOST=$host needs UI_TLS_CERT and UI_TLS_KEY (sudo bash scripts/pi/make_console_tls.sh makes the certificate)"
        fi
    fi
    # The medical values are in config/medical_parameters.json, signed (ANH-139):
    # the console refuses to start while a .env still sets one of its own.
    for key in HR_HARD_MAX_BPM HR_CRITICAL_BPM MIN_RIDER_AGE MOTION_LIMITS_PATH; do
        if [ -n "$(value "$key")" ]; then
            fail "$key is no longer read: the medical values are in config/medical_parameters.json, signed; remove this line"
        fi
    done
    if [ "$(value OCCUPANCY_OCCUPIED_ENABLED)" = "true" ] || [ "$(value PROGRAMS_ENABLED)" = "true" ]; then
        warn "a person on board or programmes are enabled: check the milestone sign-off (M5/M6)"
    fi
    if [ "$(value UI_TLS_CERT)" = /run/anheart-tls/console.crt ]; then
        if bash "$(dirname "$0")/make_console_tls.sh" --dir /var/lib/anheart/tls --check; then
            ok 'installed TLS material is usable'
        else
            fail 'installed TLS material is missing, unusable or nearing expiry'
        fi
    fi
fi

echo "3. Drive link"
backend="$(value MOTOR_BACKEND)"
port="$(value MOTOR_PORT)"
if [ "$backend" = "sim" ]; then
    warn "MOTOR_BACKEND=sim: the drive is SIMULATED, the motor will not turn"
elif [ "$backend" = "serial" ]; then
    case "$port" in
        ftdi://*) ok "MOTOR_PORT=$port (pyftdi, through libusb)" ;;
        /dev/*)
            if [ -e "$port" ]; then ok "$port present"; else fail "$port absent (is the USB-RS485 cable plugged in?)"; fi ;;
        *) fail "MOTOR_PORT=$port is neither a /dev node nor an ftdi:// URL" ;;
    esac
fi
# The STO chain's feedback: the console lowercases the value, and so does this.
sto="$(value STO_SOURCE | tr '[:upper:]' '[:lower:]')"
case "$sto" in
    "")
        warn "STO_SOURCE not set: with MOTOR_BACKEND=serial none applies, and every start and acknowledgement is refused until the safety chain is wired, configured and ready (STO_SOURCE=drive_input with STO_DRIVE_INPUT)" ;;
    none)
        warn "STO_SOURCE=none: every start and acknowledgement is refused until the safety chain is wired, configured and ready (STO_SOURCE=drive_input with STO_DRIVE_INPUT)" ;;
    sim_*)
        if [ "$backend" = "serial" ]; then
            fail "STO_SOURCE=$sto is a simulated chain, refused with MOTOR_BACKEND=serial: the console will not start"
        else
            ok "STO_SOURCE=$sto"
        fi ;;
    *) ok "STO_SOURCE=$sto" ;;
esac

echo "4. BITalino"
source_kind="$(value ECG_SOURCE)"
if [ "$source_kind" = "sim" ]; then
    warn "ECG_SOURCE=sim: the heart rate is SIMULATED"
elif [ "$source_kind" = "serial" ]; then
    mac="$(value BITALINO_MAC)"
    if command -v bluetoothctl >/dev/null 2>&1; then
        if systemctl is-active --quiet bluetooth 2>/dev/null; then ok "bluetooth service active"; else fail "bluetooth service not active (sudo systemctl enable --now bluetooth)"; fi
        if [ -n "$mac" ]; then
            if bluetoothctl info "$mac" 2>/dev/null | grep -q "Paired: yes"; then
                ok "BITalino $mac paired"
            else
                fail "BITalino $mac not paired (bash scripts/pair_device.sh $mac)"
            fi
        else
            warn "BITALINO_MAC empty: bind $(value BITALINO_ADDRESS) yourself before starting"
        fi
    else
        fail "bluetoothctl missing (sudo apt install bluez)"
    fi
fi

echo "5. Dashboard link"
url="$(value CONVEX_URL)"
key="$(value MACHINE_API_KEY)"
if [ -z "$key" ]; then
    warn "MACHINE_API_KEY empty: the console will run with no link to the dashboard"
else
    case "$url" in
        *.convex.cloud*) fail "CONVEX_URL must be the .convex.site host, not .convex.cloud" ;;
        https://*)
            # One read-only request, the one an idle console sends itself: it
            # records nothing and changes no session. A wrong key answers 401. A
            # dashboard that does not serve this console's contract answers 426.
            # A 200 proves the key and nothing more: the contract is served only
            # if the answer itself announces a version of the same major, which
            # a dashboard older than the contract does not. The answer may name
            # a rider, so it is read and never printed.
            contract="$(sed -nE 's/^CONTRACT_VERSION.*ContractVersion\("([0-9.]+)"\).*/\1/p' src/contract.py)"
            if [ -z "$contract" ]; then
                fail "src/contract.py: this console's contract version could not be read"
            else
                # The key reaches curl on its standard input (`-H @-`), never
                # on its command line, which every user of the machine can read
                # in the process list. printf is the shell's own: no process.
                answer="$(printf 'Authorization: Bearer %s\n' "$key" \
                    | curl -s --max-time 10 -w '\n%{http_code}' "$url/api/machine/training/poll" \
                        -H @- -H "X-Anheart-Contract: $contract")"
                code="${answer##*$'\n'}"
                server="$(printf '%s' "${answer%$'\n'*}" \
                    | sed -nE 's/.*"server_contract_version"[[:space:]]*:[[:space:]]*"([0-9]+\.[0-9]+)".*/\1/p' | head -n 1)"
                case "$code" in
                    200)
                        ok "$url reachable, key accepted"
                        if [ -z "$server" ]; then
                            warn "the dashboard does not announce a contract version, as one older than the contract does: this console will refuse every remote launch until the dashboard is updated"
                        elif [ "${server%%.*}" = "${contract%%.*}" ]; then
                            ok "contract $contract served (the dashboard announces $server)"
                        else
                            warn "the dashboard announces contract $server, this console speaks $contract: it will refuse every remote launch"
                        fi ;;
                    401) fail "$url reachable, but the key is refused (create a new key on the machine's page of the dashboard)" ;;
                    426) fail "$url reachable, key accepted, but it does not serve contract $contract (update the console or the dashboard)" ;;
                    000) fail "$url unreachable (network?)" ;;
                    *)   warn "$url answered HTTP $code" ;;
                esac
            fi ;;
        *) fail "CONVEX_URL must start with https://" ;;
    esac
fi

echo
if [ "$failures" -gt 0 ]; then
    echo "PREFLIGHT FAILED: $failures blocking, $warnings warning(s)"
    exit 1
fi
echo "PREFLIGHT OK ($warnings warning(s))"

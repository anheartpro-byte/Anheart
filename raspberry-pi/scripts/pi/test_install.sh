#!/usr/bin/env bash
# End-to-end test of scripts/install.sh, without a Raspberry Pi.
#
#     bash scripts/pi/test_install.sh        (from raspberry-pi/; needs Docker)
#
# A stand-in for the Pi is started as a container: Debian 12 with systemd as
# PID 1, its own Docker daemon to come, and nothing of ours in it, like a
# freshly flashed Raspberry Pi OS Lite. This directory is copied into it and
# the REAL scripts/install.sh is run there as root, in simulation (simulated
# drive, simulated ECG, no dashboard). The stand-in has the architecture of
# the machine that runs this script: arm64 in CI (.github/workflows/pi-install.yml).
#
# What is checked, in this order (docs/pi-image.md, "Ce que la CI vérifie"):
#
#   1. a first install ends with the console enabled at boot, running,
#      answering GET /healthz, at rest, and `systemctl status anheart` showing
#      the version of the VERSION file;
#   2. a second install changes nothing: the configuration is not rewritten,
#      its secret is not printed, the console is not restarted;
#   3. while a session runs, its record is written under /var/lib/anheart/records,
#      and an install that would replace the console refuses and restarts
#      nothing;
#   4. a console killed in the middle of a session is brought back by systemd
#      at rest, with nothing started and the emergency-stop attestation gone;
#   5. at rest, the same install replaces the console by the new version;
#   6. after a power cycle of the stand-in, the console is back by itself, at
#      rest.
#
# Nothing here touches hardware or a dashboard: the drive is the simulator and
# MACHINE_API_KEY stays empty. The session of steps 3 and 4 is a manual bench
# session at target 0 on the simulated drive.
#
# ANHEART_TEST_KEEP=1 leaves the stand-in running for inspection
# (docker exec -it anheart-install-test bash).
set -euo pipefail

readonly MACHINE="anheart-install-test"
readonly MACHINE_IMAGE="anheart-install-test-machine"
# Debian 12, by date and digest: the system Raspberry Pi OS (bookworm) is built on.
readonly MACHINE_BASE="debian:bookworm-20261005@sha256:2c037a04925515fdd6ea85ea14a682d0e79931f5e9f5d07b6dbfc6ba12f9e858"
readonly COPY="/home/pi/anheart/raspberry-pi"
readonly ENV_FILE="/etc/anheart/anheart.env"
# Synthetic, for this test only: the token of the operator page.
readonly TOKEN="install-test-token-0123456789abcdef"
readonly NEXT_VERSION="pi-0.0.0-install-test"

tree="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly tree
port=""

step() { printf '\n== %s\n' "$1"; }
ok()   { printf '  ok    %s\n' "$1"; }

in_machine() { docker exec "$MACHINE" "$@"; }

diagnose() {
    printf '\n---- diagnosis ----\n' >&2
    in_machine systemctl status anheart --no-pager --lines 0 >&2 || true
    in_machine journalctl --unit anheart --no-pager --lines 80 >&2 || true
    in_machine journalctl --unit docker --no-pager --lines 30 >&2 || true
    in_machine docker ps --all >&2 || true
}

fail() {
    printf '  FAIL  %s\n' "$1" >&2
    diagnose
    exit 1
}

cleanup() {
    if [ "${ANHEART_TEST_KEEP:-}" = "1" ]; then
        printf '\nThe stand-in is left running: docker exec -it %s bash\n' "$MACHINE"
    else
        docker rm --force --volumes "$MACHINE" > /dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

# expect_in TEXT WORDS LABEL: TEXT must contain WORDS.
expect_in() {
    case "$1" in
        *"$2"*) ok "$3" ;;
        *) fail "$3 (expected '$2')" ;;
    esac
}

wait_for_system() {
    local state="" attempt
    for ((attempt = 0; attempt < 90; attempt++)); do
        state="$(in_machine systemctl is-system-running 2> /dev/null || true)"
        case "$state" in running | degraded) return 0 ;; esac
        sleep 1
    done
    fail "systemd did not come up in the stand-in (state: ${state:-none})"
}

healthz() { in_machine curl --silent --fail --max-time 3 "http://127.0.0.1:$port/healthz" 2> /dev/null || true; }
status()  { in_machine curl --silent --fail --max-time 3 --header "x-anheart-token: $TOKEN" "http://127.0.0.1:$port/api/status" 2> /dev/null || true; }

post() {
    in_machine curl --silent --fail --max-time 5 --request POST \
        --header "x-anheart-token: $TOKEN" --header 'content-type: application/json' \
        --data "$2" "http://127.0.0.1:$port$1"
}

wait_for_console() {
    local attempt
    for ((attempt = 0; attempt < 90; attempt++)); do
        case "$(healthz)" in *'"status":"ok"'*) return 0 ;; esac
        sleep 2
    done
    fail "the console does not answer GET /healthz ($1)"
}

wait_for_run_state() {
    local attempt
    for ((attempt = 0; attempt < 30; attempt++)); do
        case "$(status)" in *"\"run_state\":\"$1\""*) return 0 ;; esac
        sleep 1
    done
    fail "the console never reported run_state $1 ($2)"
}

container_id() { in_machine docker inspect --format '{{.Id}}' anheart 2> /dev/null || true; }

expect_at_rest() {
    local answer
    answer="$(status)"
    expect_in "$answer" '"run_state":"idle"' "$1: the console is at rest"
    expect_in "$answer" '"attested":false' "$1: nobody has attested the emergency stop, so nothing can start"
}

install() { in_machine bash "$COPY/scripts/install.sh" --simulation; }

# --- The stand-in ---------------------------------------------------------------

step "A stand-in Raspberry Pi: Debian 12, systemd, nothing installed"
docker rm --force --volumes "$MACHINE" > /dev/null 2>&1 || true
# What Raspberry Pi OS Lite has before this script: an init system, D-Bus,
# sudo, curl and certificates. Docker, BlueZ and libusb are install.sh's job.
docker build --tag "$MACHINE_IMAGE" - <<EOF
FROM $MACHINE_BASE
ENV container=docker
RUN apt-get update \\
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \\
        systemd systemd-sysv dbus sudo curl ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
EOF
# Privileged with its own cgroup namespace and /var/lib/docker on a volume:
# what a Docker daemon needs to run inside a container.
docker run --detach --name "$MACHINE" --hostname anheart-pi \
    --privileged --cgroupns=private \
    --tmpfs /run --tmpfs /run/lock \
    --volume /var/lib/docker \
    "$MACHINE_IMAGE" > /dev/null
wait_for_system
ok "systemd is up ($(in_machine dpkg --print-architecture), $(in_machine sed -n 's/^PRETTY_NAME=//p' /etc/os-release))"

in_machine mkdir -p "$COPY"
tar -C "$tree" --exclude=.env --exclude=.venv --exclude=__pycache__ \
    --exclude=.pytest_cache --exclude=.mypy_cache --exclude=.ruff_cache --exclude=.hypothesis \
    -cf - . | docker exec --interactive "$MACHINE" tar -C "$COPY" -xf -
version="$(tr -d '[:space:]' < "$tree/VERSION")"
ok "this directory is copied to $COPY (version $version)"

# --- 1. First install ----------------------------------------------------------------

step "1. First install"
install || fail "install.sh failed on a fresh system"
port="$(in_machine sed -n 's/^UI_PORT=//p' "$ENV_FILE")"
[ -n "$port" ] || fail "no UI_PORT in $ENV_FILE"

[ "$(in_machine systemctl is-enabled anheart)" = "enabled" ] || fail "the service is not enabled at boot"
ok "the service is enabled at boot"
[ "$(in_machine systemctl is-active anheart)" = "active" ] || fail "the service is not running"
ok "the service is running"
expect_in "$(healthz)" '"status":"ok"' "GET /healthz answers on port $port"
expect_at_rest "after the first start"
expect_in "$(in_machine systemctl status anheart --no-pager --lines 0)" "version $version" \
    "systemctl status anheart shows version $version"
case "$(in_machine systemctl status anheart --no-pager --lines 0)" in
    *FAILURE*) fail "systemctl status anheart reports a failed step on a healthy service" ;;
esac
ok "systemctl status anheart reports no failed step"
expect_in "$(in_machine docker inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' anheart)" \
    "$version" "the running image is labelled $version"
expect_in "$(in_machine docker exec anheart cat /app/VERSION)" "$version" "the image carries VERSION $version"
expect_in "$(in_machine docker exec anheart python --version)" "Python 3.12." "the console runs on Python 3.12"

expect_in "$(in_machine id anheart)" "(anheart)" "the account anheart exists"
expect_in "$(in_machine stat --format '%U:%G %a' /var/lib/anheart/data)" "anheart:anheart 750" "/var/lib/anheart/data belongs to anheart"
expect_in "$(in_machine stat --format '%U:%G %a' /var/lib/anheart/records)" "anheart:anheart 700" "/var/lib/anheart/records is private to anheart"
expect_in "$(in_machine stat --format '%U:%G %a' "$ENV_FILE")" "root:root 600" "$ENV_FILE is readable by root only"
expect_in "$(in_machine grep -c -E '^(MOTOR_BACKEND|ECG_SOURCE)=sim$' "$ENV_FILE")" "2" "the configuration is the simulation"
[ -z "$(in_machine sed -n 's/^MACHINE_API_KEY=//p' "$ENV_FILE")" ] || fail "the generated configuration carries a machine key"
ok "the generated configuration carries no machine key: no dashboard is contacted"
expect_in "$(in_machine journalctl --unit anheart --no-pager)" "variateur: simulateur" \
    "the journal holds the console's own start line, on the simulated drive"

health=""
for ((attempt = 0; attempt < 60; attempt++)); do
    health="$(in_machine docker inspect --format '{{.State.Health.Status}}' anheart 2> /dev/null || true)"
    [ "$health" = "healthy" ] && break
    sleep 2
done
[ "$health" = "healthy" ] || fail "the image's own health check never passed (state: ${health:-none})"
ok "the image's health check on /healthz reports healthy"

# --- 2. Second install: nothing changes ------------------------------------------------

step "2. Second install: nothing changes"
# A secret in the configuration, and a console restarted to read it.
in_machine sed -i "s/^# UI_TOKEN=.*/UI_TOKEN=$TOKEN/" "$ENV_FILE"
expect_in "$(in_machine grep -c "^UI_TOKEN=$TOKEN$" "$ENV_FILE")" "1" "a secret is written in the configuration"
in_machine systemctl restart anheart
wait_for_console "after the restart with a token"
expect_at_rest "after a restart by hand"
before_sum="$(in_machine sha256sum "$ENV_FILE")"
before_id="$(container_id)"
output="$(install 2>&1)" || { printf '%s\n' "$output"; fail "the second install failed"; }
printf '%s\n' "$output"
case "$output" in *"$TOKEN"*) fail "the second install printed the secret of the configuration" ;; esac
ok "the secret of the configuration is not printed"
[ "$(in_machine sha256sum "$ENV_FILE")" = "$before_sum" ] || fail "the configuration was rewritten"
ok "the configuration is byte for byte the same"
expect_in "$(in_machine stat --format '%U:%G %a' "$ENV_FILE")" "root:root 600" "$ENV_FILE is still readable by root only"
[ "$(container_id)" = "$before_id" ] || fail "the console was restarted although nothing changed"
ok "the console was not restarted"
expect_in "$output" "nothing changed: left alone" "install.sh says it left the console alone"

# --- 3. Never under a session ---------------------------------------------------------

step "3. A session runs: an install that would replace the console refuses"
post /api/safety/attest '{"operator":"install-test","sto_jumper_removed":true,"mushroom_wired_nc":true}' > /dev/null \
    || fail "the simulated console refused the attestation"
post /api/manual/start '{"occupancy":"bench","operator":"install-test"}' > /dev/null \
    || fail "the simulated console refused the manual bench start"
wait_for_run_state running "manual bench session on the simulated drive"
ok "a manual bench session runs on the simulated drive"
# The session record (the local black box) lands in the directory install.sh
# made for it, through the unit's mount of the console's default RECORD_ROOT.
record=""
for ((attempt = 0; attempt < 20; attempt++)); do
    record="$(in_machine find /var/lib/anheart/records -mindepth 2 -maxdepth 2 -name manifest.json | head -n 1)"
    [ -n "$record" ] && break
    sleep 1
done
[ -n "$record" ] || fail "the session wrote no record under /var/lib/anheart/records"
ok "the session writes its record on the machine: $(dirname "$record")"
in_machine bash -c "printf '%s\n' '$NEXT_VERSION' > '$COPY/VERSION'"
before_id="$(container_id)"
if output="$(install 2>&1)"; then
    printf '%s\n' "$output"
    fail "install.sh went on while a session was running"
fi
printf '%s\n' "$output"
expect_in "$output" "does not say it is at rest: nothing is restarted" "install.sh refuses and says why"
[ "$(container_id)" = "$before_id" ] || fail "the console was replaced under a running session"
ok "the console was not touched"
expect_in "$(status)" '"run_state":"running"' "the session still runs"
expect_in "$(in_machine systemctl status anheart --no-pager --lines 0)" "version $version" \
    "the installed version is still $version"

# --- 4. Killed in the middle of a session ------------------------------------------------

step "4. The console is killed in the middle of the session"
in_machine docker kill --signal KILL anheart > /dev/null
for ((attempt = 0; attempt < 60; attempt++)); do
    current="$(container_id)"
    [ -n "$current" ] && [ "$current" != "$before_id" ] && break
    sleep 1
done
[ -n "$current" ] && [ "$current" != "$before_id" ] || fail "systemd did not start the console again"
wait_for_console "after the kill"
expect_in "$(in_machine systemctl show --property NRestarts anheart)" "NRestarts=1" "systemd restarted the service once"
expect_at_rest "after the restart by systemd"

# --- 5. At rest, the install replaces the console ------------------------------------

step "5. At rest: the same install now replaces the console"
install || fail "install.sh failed at rest"
expect_in "$(in_machine systemctl status anheart --no-pager --lines 0)" "version $NEXT_VERSION" \
    "systemctl status anheart shows version $NEXT_VERSION"
expect_in "$(in_machine docker exec anheart cat /app/VERSION)" "$NEXT_VERSION" "the running image carries VERSION $NEXT_VERSION"
expect_at_rest "after the replacement"
expect_in "$(in_machine docker image ls --format '{{.Repository}}:{{.Tag}}')" "anheart-console:$version" \
    "the image of the previous version is still on the machine"

# --- 6. Power cycle ---------------------------------------------------------------------

step "6. Power cycle of the stand-in"
docker stop -t 120 "$MACHINE" > /dev/null
docker start "$MACHINE" > /dev/null
wait_for_system
wait_for_console "after the power cycle"
ok "the console is back by itself"
expect_at_rest "after the power cycle"
expect_in "$(in_machine systemctl status anheart --no-pager --lines 0)" "version $NEXT_VERSION" \
    "systemctl status anheart shows version $NEXT_VERSION"

step "Versions seen in this run"
in_machine dpkg-query --show --showformat='  ${Package} ${Version}\n' docker.io bluez libusb-1.0-0 systemd
in_machine docker exec anheart python --version | sed 's/^/  /'
in_machine docker image ls --format '  {{.Repository}}:{{.Tag}} {{.Size}}' anheart-console
printf '\nINSTALL TEST PASSED\n'

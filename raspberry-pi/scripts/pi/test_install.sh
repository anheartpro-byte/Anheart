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
#   3. while a session runs, its record is written under /var/lib/anheart/records
#      and stamped with the version, and an install that would replace the
#      console refuses and restarts nothing;
#   4. the service stopped during that session ends through the console's own
#      shutdown path, well inside its stop timeout, with the record closed;
#      started again, the console is at rest and unattested;
#   5. a console killed in the middle of a session is brought back by systemd
#      at rest, with nothing started and the emergency-stop attestation gone;
#   6. at rest, the same install replaces the console by the new version;
#   7. a console the service did not start (a container named anheart run by
#      hand) is not replaced while it runs a session; at rest it is, and the
#      unit stops and removes it before it starts its own;
#   8. after a shutdown and a power-on of the stand-in, the console is back by
#      itself, at rest. The shutdown is an orderly one: a power cut in the
#      middle of a write is not simulated here.
#
# Nothing here touches hardware or a dashboard: the drive is the simulator and
# MACHINE_API_KEY stays empty. Every session is a manual bench session at
# target 0 on the simulated drive.
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
readonly THIRD_VERSION="pi-0.0.0-install-test-2"

tree="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly tree
port=""

step() { printf '\n== %s\n' "$1"; }
ok()   { printf '  ok    %s\n' "$1"; }

in_machine() { docker exec "$MACHINE" "$@"; }

diagnose() {
    printf '\n---- diagnosis ----\n' >&2
    in_machine systemctl status anheart --no-pager --lines 0 >&2 || true
    in_machine systemctl show --property ActiveState,Result,ExecMainStatus,NRestarts anheart >&2 || true
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

# A manual bench session at target 0 on the simulated drive.
start_session() {
    post /api/safety/attest '{"operator":"install-test","sto_jumper_removed":true,"mushroom_wired_nc":true}' > /dev/null \
        || fail "the simulated console refused the attestation ($1)"
    post /api/manual/start '{"occupancy":"bench","operator":"install-test"}' > /dev/null \
        || fail "the simulated console refused the manual bench start ($1)"
    wait_for_run_state running "$1"
}

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
# How install.sh and preflight.sh hand a secret to curl: on its standard input,
# never on its command line. Checked with the curl of this system.
answer="$(printf 'x-anheart-token: %s\n' "$TOKEN" \
    | docker exec --interactive "$MACHINE" curl --silent --fail --max-time 3 -H @- "http://127.0.0.1:$port/api/status" || true)"
expect_in "$answer" '"run_state":"idle"' "this system's curl sends a header it reads on its standard input"
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
start_session "manual bench session on the simulated drive"
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
expect_in "$(in_machine cat "$record")" "\"software_version\":\"$version\"" \
    "the record is stamped with the version of the image, $version"
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

# --- 4. The service is stopped during the session ---------------------------------------

step "4. The service is stopped during the session"
begin="$(date +%s)"
in_machine systemctl stop anheart || fail "systemctl stop anheart failed"
elapsed=$(($(date +%s) - begin))
# Docker gives the console 60 s before it kills it, systemd 90 s per stop command.
[ "$elapsed" -lt 60 ] || fail "systemctl stop anheart took $elapsed s: the console did not stop by itself"
ok "systemctl stop anheart returned after $elapsed s"
stopped="$(in_machine systemctl show --property ActiveState,Result,ExecMainStatus anheart)"
expect_in "$stopped" "ActiveState=inactive" "the service is stopped"
expect_in "$stopped" "Result=success" "systemd saw a clean stop, inside its stop timeout"
expect_in "$stopped" "ExecMainStatus=0" "the console ended with exit code 0"
left="?"
for ((attempt = 0; attempt < 10; attempt++)); do
    left="$(in_machine docker ps --all --quiet)"
    [ -z "$left" ] && break
    sleep 1
done
[ -z "$left" ] || fail "a console container outlived the service"
ok "no console container is left"
logged=""
for ((attempt = 0; attempt < 10; attempt++)); do
    logged="$(in_machine journalctl --unit anheart --no-pager --since "@$begin")"
    case "$logged" in *"shutdown complete: emergency zero"*) break ;; esac
    sleep 1
done
expect_in "$logged" "shutdown complete: emergency zero" "the journal holds the console's own shutdown of the session"
expect_in "$(in_machine cat "$record")" '"end_reason":"shutdown"' "the record of the session is closed, reason: shutdown"
in_machine test -f "$(dirname "$record")/checksums.sha256" || fail "the closed record has no checksums"
ok "the closed record carries its checksums"
in_machine systemctl start anheart || fail "systemctl start anheart failed"
wait_for_console "after the stop during a session"
expect_at_rest "started again after a stop during a session"

# --- 5. Killed in the middle of a session ------------------------------------------------

step "5. The console is killed in the middle of a session"
start_session "manual bench session before the kill"
before_id="$(container_id)"
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

# --- 6. At rest, the install replaces the console ------------------------------------

step "6. At rest: the same install now replaces the console"
install || fail "install.sh failed at rest"
expect_in "$(in_machine systemctl status anheart --no-pager --lines 0)" "version $NEXT_VERSION" \
    "systemctl status anheart shows version $NEXT_VERSION"
expect_in "$(in_machine docker exec anheart cat /app/VERSION)" "$NEXT_VERSION" "the running image carries VERSION $NEXT_VERSION"
expect_at_rest "after the replacement"
expect_in "$(in_machine docker image ls --format '{{.Repository}}:{{.Tag}}')" "anheart-console:$version" \
    "the image of the previous version is still on the machine"

# --- 7. A console the service did not start -------------------------------------------

step "7. A console the service did not start: a container named anheart run by hand"
in_machine systemctl stop anheart || fail "systemctl stop anheart failed"
# The way an image is run without the unit: no --rm, Docker's own logging.
in_machine docker run --detach --name anheart --network host --env-file "$ENV_FILE" \
    "anheart-console:$NEXT_VERSION" > /dev/null || fail "the console could not be started by hand"
wait_for_console "started by hand"
foreign_id="$(container_id)"
start_session "manual bench session on the console started by hand"
ok "it runs a session; the service is $(in_machine systemctl is-active anheart || true)"
in_machine bash -c "printf '%s\n' '$THIRD_VERSION' > '$COPY/VERSION'"
if output="$(install 2>&1)"; then
    printf '%s\n' "$output"
    fail "install.sh went on while a console the service did not start was running a session"
fi
printf '%s\n' "$output"
expect_in "$output" "does not say it is at rest: nothing is restarted" "install.sh refuses and says why"
[ "$(container_id)" = "$foreign_id" ] || fail "the console started by hand was replaced under a running session"
expect_in "$(status)" '"run_state":"running"' "its session still runs"
[ "$(in_machine systemctl is-active anheart || true)" = "inactive" ] || fail "the service was started over a running session"
ok "the service was not started"
post /api/session/stop '{"operator":"install-test","reason":"install test"}' > /dev/null \
    || fail "the simulated console refused to end the session"
wait_for_run_state idle "after the session was ended at the console"
install || fail "install.sh failed over a console at rest that the service did not start"
[ "$(in_machine systemctl is-active anheart)" = "active" ] || fail "the service is not running"
[ "$(container_id)" != "$foreign_id" ] || fail "the console started by hand is still the one running"
expect_in "$(in_machine docker inspect --format '{{.HostConfig.AutoRemove}}' anheart)" "true" \
    "the unit stopped and removed it, and runs its own console"
[ "$(in_machine docker ps --all --quiet | wc -l)" -eq 1 ] || fail "more than one container is left on the machine"
ok "one console container, and only one"
expect_in "$(in_machine systemctl status anheart --no-pager --lines 0)" "version $THIRD_VERSION" \
    "systemctl status anheart shows version $THIRD_VERSION"
expect_at_rest "after the replacement of the console started by hand"

# --- 8. Shutdown and power-on -------------------------------------------------------------

step "8. Shutdown, then power-on, of the stand-in"
docker stop -t 120 "$MACHINE" > /dev/null
docker start "$MACHINE" > /dev/null
wait_for_system
wait_for_console "after the power-on"
ok "the console is back by itself"
expect_at_rest "after the power-on"
expect_in "$(in_machine systemctl status anheart --no-pager --lines 0)" "version $THIRD_VERSION" \
    "systemctl status anheart shows version $THIRD_VERSION"

step "Versions seen in this run"
in_machine dpkg-query --show --showformat='  ${Package} ${Version}\n' docker.io bluez libusb-1.0-0 systemd
in_machine docker exec anheart python --version | sed 's/^/  /'
in_machine docker image ls --format '  {{.Repository}}:{{.Tag}} {{.Size}}' anheart-console
printf '\nINSTALL TEST PASSED\n'

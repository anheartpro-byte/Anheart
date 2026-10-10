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
#      the version of the VERSION file; the time service is installed and
#      enabled, the journal is on the disk and bounded, and the console reads
#      the state of the machine's clock from the kernel, as timedatectl does;
#   2. a second install changes nothing: the configuration is not rewritten,
#      its secret is not printed, the console is not restarted; a change of
#      the journal's settings restarts journald and not the console, which
#      goes on writing to the journal;
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
#      itself, at rest, and the journal written before the shutdown is still
#      there. The shutdown is an orderly one: a power cut in the middle of a
#      write is not simulated here;
#   9. a console frozen in the middle of a session (SIGSTOP: it completes no
#      tick) is stopped by the unit's watchdog after 10 s. That stop wakes it
#      (systemd sends SIGTERM then SIGCONT, `docker run` relays both): it
#      leaves by its controlled stop, closes its record, and exits in
#      failure, because its loop had gone without a tick for too long. systemd
#      starts another console, at rest, which says it started after an
#      unclean stop;
#  10. a console that dies again and again comes up out of service: it runs,
#      says so, and arms nothing, until a named operator puts it back in
#      service;
#  11. on a machine that drifted (a file of the console given back to root, a
#      mode widened, the account given a shell, an automatic update turned back
#      on), `harden.sh --dry-run` names each change and makes none, then
#      `harden.sh` makes them without touching a content or the console, and a
#      second dry run finds nothing left;
#  12. `uninstall.sh` takes back what the install and the hardening set in the
#      system, keeps the configuration, the data and the account, and the same
#      install then brings the console back.
#
# What the console runs with is checked from step 1 on (docs/pi-image.md, "Sous
# quel compte"): in its container it is the account anheart, with no
# capability, on a read-only image, and may open the device classes of the
# drive cable and of the sensor and no other; the relay of the watchdog is the
# account too; the configuration is root's alone and everything under
# /var/lib/anheart the account's alone; nothing updates the packages by itself.
# Steps 1 to 10 then run under exactly that.
#
# Nothing here touches hardware or a dashboard: the drive is the simulator and
# MACHINE_API_KEY stays empty. Every session is a manual bench session at
# target 0 on the simulated drive. The simulated drive lives in the console's
# own process: what a restart finds of a drive left RUNNING is not here, it
# is tests/test_supervised_console.py, where the drive outlives the console.
#
# A console that answers NOTHING, not even the stop (a process blocked outside
# its event loop), is not exercised here either: every freeze that can be
# applied from outside (SIGSTOP, `docker pause`) is lifted by the stop itself.
# What the unit does then (15 s, then `docker stop`, which kills after 60 s)
# is read from the unit, not observed.
#
# The hardware watchdog is not exercised either: install.sh writes systemd's
# setting for it, which this test reads back, and the stand-in's systemd is
# told to take /dev/null for a watchdog device, so that it never arms the one
# of the machine this test runs on.
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
# In the console's own container, as the account the console runs under.
in_console() { docker exec "$MACHINE" docker exec anheart "$@"; }

diagnose() {
    printf '\n---- diagnosis ----\n' >&2
    in_machine systemctl status anheart --no-pager --lines 0 >&2 || true
    in_machine systemctl show --property ActiveState,Result,ExecMainStatus,NRestarts anheart >&2 || true
    in_machine journalctl --unit anheart --no-pager --lines 80 >&2 || true
    in_machine journalctl --unit docker --no-pager --lines 30 >&2 || true
    in_machine docker ps --all >&2 || true
    in_machine sh -c 'cat /etc/systemd/system/anheart.service.d/*.conf' >&2 || true
    in_machine sh -c 'ls -ln /run/anheart /var/lib/anheart /var/lib/anheart/records' >&2 || true
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
# What the console says of itself: in service or not, its last stop, its watchdog, its clock.
panel()   { in_machine curl --silent --fail --max-time 3 --header "x-anheart-token: $TOKEN" "http://127.0.0.1:$port/api/panel" 2> /dev/null || true; }

# What the console says of the machine's wall clock: yes (a time service keeps
# it), no, unreadable (it could not ask the kernel), or absent.
console_clock() {
    case "$(panel)" in
        *'"wall_clock":{"state":"synchronised"'*) echo yes ;;
        *'"wall_clock":{"state":"unsynchronised"'*) echo no ;;
        *'"wall_clock":{"state":"unreadable"'*) echo unreadable ;;
        *) echo absent ;;
    esac
}

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

# wait_for_replacement ID SECONDS LABEL: systemd must start another console than ID.
wait_for_replacement() {
    local attempt current=""
    for ((attempt = 0; attempt < $2; attempt++)); do
        current="$(container_id)"
        [ -n "$current" ] && [ "$current" != "$1" ] && return 0
        sleep 1
    done
    fail "systemd did not start the console again within $2 s ($3)"
}

# The session records on the machine, oldest first, by name.
records() {
    in_machine find /var/lib/anheart/records -mindepth 1 -maxdepth 1 -type d -name '2*Z_*' -printf '%f\n' | sort
}

restarts() { in_machine systemctl show --property NRestarts --value anheart; }

# What the console keeps of its restarts, as it is on the disk.
restart_state() { in_machine cat /var/lib/anheart/records/service-state.json 2> /dev/null || true; }

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
harden()  { in_machine bash "$COPY/scripts/harden.sh" "$@"; }

# owned PATH: its owner, its group and its mode, as `user:group mode`.
owned() { in_machine stat --format '%U:%G %a' "$1"; }

# What in the console's directories is not the account's, or is open to a
# group or to anybody else. Nothing, on a machine as it must be.
not_private() {
    in_machine find /var/lib/anheart \( ! -user anheart -o ! -group anheart -o -perm /077 \) -print \
        2> /dev/null || true
}

expect_private() {
    local found
    found="$(not_private)"
    [ -z "$found" ] || fail "$1: not the account's alone under /var/lib/anheart: $found"
    ok "$1: everything under /var/lib/anheart is the account's alone"
}

# The three services that update the packages of a Debian system by themselves.
readonly UPDATE_UNITS="apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service"

expect_no_automatic_update() {
    local unit settings
    for unit in $UPDATE_UNITS; do
        [ "$(in_machine systemctl is-enabled "$unit" 2> /dev/null || true)" = "masked" ] \
            || fail "$1: $unit is not masked"
        [ "$(in_machine systemctl is-active "$unit" 2> /dev/null || true)" != "active" ] \
            || fail "$1: $unit is running"
    done
    ok "$1: $UPDATE_UNITS are masked and none runs"
    # apt itself, asked what it applies: the distribution's file says 1.
    settings="$(in_machine apt-config dump)"
    expect_in "$settings" 'APT::Periodic::Update-Package-Lists "0";' "$1: apt no longer refreshes its lists by itself"
    expect_in "$settings" 'APT::Periodic::Unattended-Upgrade "0";' "$1: apt no longer upgrades by itself"
}

# what_opening PATH: what the console gets when it opens a device, in one word:
# `opened`, or the name of the error (EACCES: the file's own permissions;
# EPERM: a device class the container may not open; ENXIO or ENODEV: allowed,
# and no hardware behind the node).
what_opening() {
    in_console python -c '
import errno, os, sys
try:
    os.close(os.open(sys.argv[1], os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY))
except OSError as error:
    print(errno.errorcode.get(error.errno, str(error.errno)))
else:
    print("opened")
' "$1"
}

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
# install.sh sets systemd's hardware watchdog. In a container systemd reads its
# own command line as the kernel's: it is told to take /dev/null for the
# device, and so never arms a watchdog of the machine that runs this test.
CMD ["/lib/systemd/systemd", "systemd.watchdog_device=/dev/null"]
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

# A system that updates its packages by itself, as the package
# unattended-upgrades sets it: the install must turn that off.
in_machine sh -c 'printf "%s\n" "APT::Periodic::Update-Package-Lists \"1\";" "APT::Periodic::Unattended-Upgrade \"1\";" > /etc/apt/apt.conf.d/20auto-upgrades'
expect_in "$(in_machine apt-config dump)" 'APT::Periodic::Unattended-Upgrade "1";' \
    "before the install, this system upgrades its packages by itself"

# Three device nodes with no hardware behind them, to ask the console later
# what it may open. They are in the stand-in's own /dev, which is not the one
# of the machine this test runs on.
#   ttyUSB7                a USB serial adapter (class 188), as udev makes one: group dialout
#   anheart-test-refused   a serial port of another class (4), same owner and mode
#   anheart-test-other     class 188, but root's alone
in_machine sh -c '
    mknod -m 0660 /dev/ttyUSB7 c 188 7 && chgrp dialout /dev/ttyUSB7 &&
    mknod -m 0660 /dev/anheart-test-refused c 4 71 && chgrp dialout /dev/anheart-test-refused &&
    mknod -m 0600 /dev/anheart-test-other c 188 8' \
    || fail "the stand-in could not make the test's device nodes"

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
expect_in "$(in_machine getent passwd anheart)" ":/usr/sbin/nologin" "the account anheart cannot log in"
expect_in "$(in_machine passwd --status anheart)" "anheart L " "the account anheart has no usable password"
expect_in " $(in_machine id -nG anheart) " " dialout " "the account anheart is in the group of the serial devices"
for directory in /var/lib/anheart /var/lib/anheart/data /var/lib/anheart/records; do
    expect_in "$(owned "$directory")" "anheart:anheart 700" "$directory is the account's alone"
done
expect_in "$(owned "$ENV_FILE")" "root:root 600" "$ENV_FILE is readable by root only"
in_machine setpriv --reuid=anheart --regid=anheart --clear-groups cat "$ENV_FILE" > /dev/null 2>&1 \
    && fail "the account anheart can read the configuration, where the machine's key and the console's limits are"
ok "the account anheart cannot read the configuration, and so cannot rewrite its own limits"
expect_in "$(in_machine grep -c -E '^(MOTOR_BACKEND|ECG_SOURCE)=sim$' "$ENV_FILE")" "2" "the configuration is the simulation"
[ -z "$(in_machine sed -n 's/^MACHINE_API_KEY=//p' "$ENV_FILE")" ] || fail "the generated configuration carries a machine key"
ok "the generated configuration carries no machine key: no dashboard is contacted"
expect_in "$(in_machine journalctl --unit anheart --no-pager)" "variateur: simulateur" \
    "the journal holds the console's own start line, on the simulated drive"

# The time service: installed, enabled at boot, and given the two directives
# the console's reading of the clock relies on. Whether it runs here is said,
# not required: it needs the right to set the clock of the machine under it.
expect_in "$(in_machine dpkg-query --show --showformat='${db:Status-Status}' chrony)" "installed" \
    "the time service chrony is installed"
[ "$(in_machine systemctl is-enabled chrony)" = "enabled" ] || fail "chrony is not enabled at boot"
ok "chrony is enabled at boot"
chrony_conf="$(in_machine cat /etc/chrony/conf.d/anheart.conf)"
expect_in "$chrony_conf" "rtcsync" "chrony publishes its state to the kernel and keeps a real-time clock on time"
expect_in "$chrony_conf" "makestep 1 3" "chrony steps a clock that restarted on an old time"
ok "chrony is $(in_machine systemctl is-active chrony || true) in this stand-in (said, not required)"

# The console reads the state of the machine's clock from the kernel, inside
# its container, and systemd reads the same kernel: they must agree. The
# console asks every 10 s, and the state may change while chrony starts.
clock="$(console_clock)"
case "$clock" in
    yes | no) ok "the console reads the state of the machine's clock in its container (kept by a time service: $clock)" ;;
    *) fail "the console does not say what the machine's clock is worth (got: $clock)" ;;
esac
system_clock=""
agreed=""
for ((attempt = 0; attempt < 8; attempt++)); do
    system_clock="$(in_machine timedatectl show --property=NTPSynchronized --value 2> /dev/null || true)"
    clock="$(console_clock)"
    case "$system_clock" in yes | no) ;; *) break ;; esac
    if [ "$clock" = "$system_clock" ]; then agreed=yes; break; fi
    sleep 5
done
case "$system_clock" in
    yes | no)
        [ -n "$agreed" ] || fail "the console says the clock is kept: $clock, timedatectl says: $system_clock"
        ok "the console and timedatectl agree on the clock (synchronized: $system_clock)" ;;
    *) ok "timedatectl gives no answer in this stand-in: the console's reading is not compared" ;;
esac

# The journal: on the disk, and bounded. journald says itself what it applies.
journald_conf="$(in_machine cat /etc/systemd/journald.conf.d/anheart.conf)"
expect_in "$journald_conf" "Storage=persistent" "the journal is kept on the disk"
expect_in "$journald_conf" "SystemMaxUse=200M" "the journal is bounded to 200 MB"
in_machine sh -c 'ls /var/log/journal/*/system.journal' > /dev/null 2>&1 \
    || fail "the journal is not written under /var/log/journal"
ok "the journal is written under /var/log/journal"
usage="$(in_machine journalctl --no-pager --output cat | grep -F 'System Journal (/var/log/journal/' | tail -n 1 || true)"
if [ -n "$usage" ]; then
    expect_in "$usage" "max 200.0M" "journald reports the bound it applies"
else
    ok "journald wrote no usage line in this stand-in: its bound is read from its configuration only"
fi

health=""
for ((attempt = 0; attempt < 60; attempt++)); do
    health="$(in_machine docker inspect --format '{{.State.Health.Status}}' anheart 2> /dev/null || true)"
    [ "$health" = "healthy" ] && break
    sleep 2
done
[ "$health" = "healthy" ] || fail "the image's own health check never passed (state: ${health:-none})"
ok "the image's health check on /healthz reports healthy"

# The watchdog and the restart policy, as systemd itself reads the unit.
unit="$(in_machine systemctl show --property Type,NotifyAccess,WatchdogUSec,Restart,RestartUSec,StartLimitBurst anheart)"
expect_in "$unit" "Type=notify" "the service is ready when the console says so"
expect_in "$unit" "WatchdogUSec=10s" "the service must be heard every 10 s"
expect_in "$unit" "Restart=on-failure" "a failure is followed by a restart"
expect_in "$unit" "RestartUSec=2s" "two seconds later"
expect_in "$unit" "StartLimitBurst=10" "within a limit of restarts"
# The console's notifications reach systemd through the unit's relay: the
# service has been up for longer than the watchdog's 10 s, and was not ended.
first_id="$(container_id)"
sleep 15
[ "$(container_id)" = "$first_id" ] || fail "the console was replaced within 15 s: its notifications do not reach systemd"
[ "$(restarts)" = "0" ] || fail "the service was restarted $(restarts) time(s) on a healthy console"
case "$(in_machine journalctl --unit anheart --no-pager)" in
    *"Watchdog timeout"*) fail "the watchdog ended a healthy console" ;;
esac
ok "more than 15 s without a restart: the watchdog is fed by the console"
case "$(in_machine journalctl --unit anheart --no-pager)" in
    *"notification not sent"*) fail "a notification of the console did not reach its relay" ;;
esac
ok "every notification of the console reached its relay"
answer="$(panel)"
expect_in "$answer" '"watchdog":{"enabled":true' "the console says a service manager watches it"
expect_in "$answer" '"failed":0,' "and that every notification left"
expect_in "$answer" '"service":{"out_of_service":false,"unclean_starts":0,' "a first start is in service"
expect_in "$(owned /var/lib/anheart/records/service-state.json)" "anheart:anheart 600" \
    "the restart state is kept next to the records, the account's alone"
# systemd's own setting for the hardware watchdog, where systemd reads it.
expect_in "$(in_machine systemd-analyze cat-config systemd/system.conf)" "RuntimeWatchdogSec=15s" \
    "systemd is told to arm the hardware watchdog (15 s)"

# What the console runs with: asked of the running system, not read from the unit.
account="$(in_machine id -u anheart):$(in_machine id -g anheart)"
case "$account" in 0:* | *:0 | :) fail "the account anheart has the numbers of root ($account)" ;; esac
[ "$(in_console id -u):$(in_console id -g)" = "$account" ] \
    || fail "the console runs as $(in_console id -u):$(in_console id -g), not as the account anheart ($account)"
ok "in its container the console is the account anheart ($account), not root"
expect_in " $(in_console id -G) " " $(in_machine getent group dialout | cut -d: -f3) " \
    "and it has the group of the serial devices"
expect_in "$(in_machine docker inspect --format '{{.HostConfig.Privileged}} {{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}}' anheart)" \
    "false true [ALL] [no-new-privileges]" "its container is not privileged, read-only, with every capability dropped"
console_pid="$(in_machine docker inspect --format '{{.State.Pid}}' anheart)"
rights="$(in_machine grep -E '^(CapPrm|CapEff|CapBnd|NoNewPrivs):' "/proc/$console_pid/status" | tr -s '[:space:]' ' ')"
expect_in "$rights" "CapPrm: 0000000000000000 CapEff: 0000000000000000 CapBnd: 0000000000000000" \
    "the kernel gives the console's process no capability, and none it could regain"
expect_in "$rights" "NoNewPrivs: 1" "and no way to gain a privilege"
in_console sh -c ': > /app/written-by-the-install-test' 2> /dev/null \
    && fail "the console can write in its own image"
ok "the console cannot write in its image"
in_console sh -c ': > /tmp/written-by-the-install-test' || fail "the console cannot write in /tmp"
expect_in "$(in_console grep ' /tmp ' /proc/mounts)" "noexec" "/tmp is memory, where nothing can be executed"
# The devices: the class of the drive cable with the group udev gives it, and nothing else.
case "$(what_opening /dev/ttyUSB7)" in
    opened | ENXIO | ENODEV) ok "the console may open a USB serial adapter of the group dialout (no hardware behind this one)" ;;
    *) fail "the console may not open a USB serial adapter: $(what_opening /dev/ttyUSB7)" ;;
esac
expect_in "$(what_opening /dev/anheart-test-refused)" "EPERM" \
    "a device of a class it was not given is refused by the kernel, whatever the file's permissions"
expect_in "$(what_opening /dev/anheart-test-other)" "EACCES" \
    "a USB serial adapter that is root's alone is refused: the group is needed too"
expect_in "$(in_machine docker image inspect --format '{{.Config.User}}' "anheart-console:$version")" "anheart" \
    "the image itself names an account that is not root"

# The relay of the watchdog: the account, in a directory only the account enters.
# Its real, effective, saved and file-system user, as the kernel has them.
relay_users="$(in_machine sh -c "grep -l '^socat\$' /proc/[0-9]*/comm 2> /dev/null | sed 's|/comm\$|/status|' | xargs -r grep -h '^Uid:'" \
    | tr -s '[:space:]' ' ' || true)"
relay_user="${account%%:*}"
[ "$relay_users" = "Uid: $relay_user $relay_user $relay_user $relay_user " ] \
    || fail "the relay of the watchdog does not run as the account anheart ($relay_user) and nothing else: '${relay_users:-no relay found}'"
ok "the relay of the watchdog runs as the account anheart"
expect_in "$(owned /run/anheart/relay)" "anheart:anheart 700" "only the account enters the relay's directory"
expect_in "$(in_machine stat --format '%U' /run/anheart/relay/notify)" "anheart" "and the relay's socket is the account's"

# The unit's own processes (the Docker client is its main process), as systemd
# reads the unit and as the kernel applies it.
unit="$(in_machine systemctl show --property NoNewPrivileges,ProtectSystem,ProtectHome anheart)"
expect_in "$unit" "NoNewPrivileges=yes" "the unit's own processes can gain no privilege"
expect_in "$unit" "ProtectSystem=strict" "and see the file system read-only"
expect_in "$unit" "ProtectHome=yes" "and no home directory"
main_pid="$(in_machine systemctl show --property MainPID --value anheart)"
in_machine nsenter --target "$main_pid" --mount -- sh -c ': > /etc/written-by-the-install-test' 2> /dev/null \
    && fail "where the unit's processes run, /etc can be written"
ok "where the unit's processes run, /etc cannot be written, even by root"
in_machine sh -c 'ls -A /root/ | grep -q .' || fail "root's home directory is empty in the stand-in: nothing to hide"
in_machine nsenter --target "$main_pid" --mount -- sh -c 'ls -A /root/ 2> /dev/null | grep -q .' \
    && fail "where the unit's processes run, root's home directory can be read"
ok "and root's home directory shows nothing there"
# Said plainly: the Docker client is root. What it may ask of the daemon is not bounded.
expect_in "$(in_machine stat --format '%U' "/proc/$main_pid")" "root" \
    "the Docker client of the unit is root (the daemon is root's): this is not reduced"

# The step before the console: the sensor's serial node is bound on the machine.
expect_in "$(owned /usr/local/lib/anheart/bind-bitalino)" "root:root 755" \
    "what binds the sensor's serial node is installed, root's"
expect_in "$(in_machine systemctl show --property ExecStartPre anheart)" "/usr/local/lib/anheart/bind-bitalino" \
    "and the unit runs it before the console"
expect_in "$(in_machine cat /etc/udev/rules.d/70-anheart-drive.rules)" 'ATTR{idVendor}=="16de", ATTR{idProduct}=="0003", GROUP="dialout", MODE="0660"' \
    "the drive's USB cable is given to the group of the serial devices"

expect_no_automatic_update "after the install"
expect_private "after the first start"

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
expect_in "$output" "harden.sh: nothing to change" "and that nothing was left to protect"

# The journal's settings changed under a running console: journald is restarted
# to read them, the console is not, and it goes on writing to the journal.
# In the same run, what an older install left of the console's files (its
# restart state written by root, a configuration anybody can read) is
# corrected, and no content is touched.
in_machine sh -c 'printf "# changed by the install test\n" >> /etc/systemd/journald.conf.d/anheart.conf'
in_machine chown root:root /var/lib/anheart/records/service-state.json
in_machine chmod 0644 "$ENV_FILE"
before_sums="$(in_machine sha256sum "$ENV_FILE" /var/lib/anheart/records/service-state.json)"
before_id="$(container_id)"
begin="$(date +%s)"
output="$(install 2>&1)" || { printf '%s\n' "$output"; fail "the install failed after a change of the journal's settings"; }
expect_in "$output" "/etc/systemd/journald.conf.d/anheart.conf written" "install.sh puts the journal's settings back"
expect_in "$(owned /var/lib/anheart/records/service-state.json)" "anheart:anheart 600" \
    "a file of the console that root owned is given back to the account"
expect_in "$(owned "$ENV_FILE")" "root:root 600" "a configuration anybody could read is closed again"
[ "$(in_machine sha256sum "$ENV_FILE" /var/lib/anheart/records/service-state.json)" = "$before_sums" ] \
    || fail "the install changed the content of a file it only had to protect"
ok "neither content was touched"
[ "$(container_id)" = "$before_id" ] || fail "the console was restarted by a change of the journal's settings"
expect_in "$(in_machine systemctl show --property NRestarts anheart)" "NRestarts=0" \
    "the console was not restarted by the restart of journald"
# A request the console refuses and logs, with no effect on its state: it
# carries no token, on a console that now requires one.
in_machine curl --silent --max-time 3 "http://127.0.0.1:$port/api/status" > /dev/null || true
logged=""
for ((attempt = 0; attempt < 10; attempt++)); do
    logged="$(in_machine journalctl --unit anheart --no-pager --since "@$begin")"
    case "$logged" in *"rejected GET /api/status"*) break ;; esac
    sleep 1
done
expect_in "$logged" "rejected GET /api/status" "what the console writes after that restart still reaches the journal"

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
# systemd takes /run/anheart back, as root's, when it runs the line of the
# stop: the relay's own directory inside it is still the account's then, and
# the console that stops can still say so.
case "$logged" in
    *"notification not sent"*) fail "the console that stops could not reach its relay any more" ;;
esac
ok "the console that stops still reaches its relay"
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
expect_in "$(in_console id -un)" "anheart" "started by hand, with no account named, the image does not run as root either"
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
# A line of this run, to look for afterwards. /run is a tmpfs that the restart
# empties: a journal that was not on the disk would be gone with it.
marker="written-before-the-shutdown-$$"
in_machine logger --tag anheart-install-test "$marker"
docker stop -t 120 "$MACHINE" > /dev/null
docker start "$MACHINE" > /dev/null
wait_for_system
wait_for_console "after the power-on"
ok "the console is back by itself"
expect_at_rest "after the power-on"
expect_in "$(in_machine journalctl --no-pager --output cat --identifier anheart-install-test || true)" "$marker" \
    "a line written before the shutdown is still in the journal after the power-on"
expect_in "$(in_machine journalctl --unit anheart --no-pager)" "shutdown complete: emergency zero" \
    "the console's own lines from before the shutdown are still there too"
expect_in "$(in_machine systemctl status anheart --no-pager --lines 0)" "version $THIRD_VERSION" \
    "systemctl status anheart shows version $THIRD_VERSION"

# --- 9. A console frozen in the middle of a session ---------------------------------------

step "9. The console freezes in the middle of a session: the watchdog has it restarted"
known="$(records | wc -l)"
start_session "manual bench session before the freeze"
for ((attempt = 0; attempt < 20; attempt++)); do
    [ "$(records | wc -l)" -gt "$known" ] && break
    sleep 1
done
[ "$(records | wc -l)" -gt "$known" ] || fail "the session before the freeze wrote no record"
frozen_record="$(records | tail -n 1)"
before_id="$(container_id)"
before_restarts="$(restarts)"
# SIGSTOP on the console itself: every thread of it stops. It completes no
# tick and tells systemd nothing.
console_pid="$(in_machine docker inspect --format '{{.State.Pid}}' anheart)"
begin="$(date +%s)"
in_machine bash -c "kill -STOP $console_pid" || fail "the console could not be frozen"
ok "the console is frozen (process $console_pid), its session record is $frozen_record"
# 10 s for the watchdog. Its stop is a SIGTERM followed by a SIGCONT, both
# relayed to the console by `docker run`: the stop itself wakes the console,
# which then leaves by its controlled stop. Then two seconds, and a new start.
wait_for_replacement "$before_id" 200 "after the freeze"
elapsed=$(($(date +%s) - begin))
[ "$elapsed" -ge 10 ] || fail "the frozen console was replaced after $elapsed s: before the watchdog's 10 s"
ok "systemd started another console $elapsed s after the freeze"
wait_for_console "after the freeze"
logged="$(in_machine journalctl --unit anheart --no-pager --since "@$begin")"
expect_in "$logged" "Watchdog timeout (limit 10s)" "the journal says the watchdog stopped the frozen console"
expect_in "$logged" "Failed with result 'watchdog'" "and that the service failed on its watchdog"
# The woken console knows its loop had stopped completing ticks, whether or
# not one late tick ran before it handled the stop: it does not leave as if
# nothing had happened.
expect_in "$logged" "not a clean stop" "the woken console says its own stop is not a clean one"
expect_in "$logged" "status=1/FAILURE" "and exits in failure"
[ "$(restarts)" -gt "$before_restarts" ] || fail "systemd does not count a restart of the service"
ok "systemd restarted the service by itself"
expect_at_rest "after the restart by the watchdog"
# It left by its own exit path: the record of the frozen session is closed.
in_machine test -f "/var/lib/anheart/records/$frozen_record/checksums.sha256" \
    || fail "the woken console did not close the record of its session"
ok "the record of the frozen session is closed ($(in_machine grep -o '"end_reason":"[a-z_]*"' "/var/lib/anheart/records/$frozen_record/manifest.json"))"
answer="$(panel)"
expect_in "$answer" '"unclean_stop":true' "the new console says it started after an unclean stop"
expect_in "$answer" '"open_record":null' "with no session record left open"
case "$answer" in
    *'"unclean_starts":0,'*) fail "the start after the freeze is not counted" ;;
esac
ok "and the start after the freeze is counted"
written=""
for ((attempt = 0; attempt < 15; attempt++)); do
    written="$(in_machine cat /var/lib/anheart/records/logbook/events.jsonl 2> /dev/null || true)"
    case "$written" in *'"detail":"unclean_stop: demarrage apres arret non propre"'*) break ;; esac
    sleep 1
done
expect_in "$written" '"detail":"unclean_stop: demarrage apres arret non propre"' \
    "the logbook holds the warning: start after an unclean stop"

# --- 10. A console that keeps dying ------------------------------------------------------

step "10. The console dies again and again: out of service, until a named operator puts it back"
begin="$(date +%s)"
deaths=0
# Three starts after an unclean stop within ten minutes. The start after the
# freeze above was one; the one after the kill of step 5 is another when this
# run got here in under ten minutes.
while :; do
    case "$(panel)" in *'"out_of_service":true'*) break ;; esac
    [ "$deaths" -lt 3 ] || fail "three more deaths did not take the console out of service"
    before_id="$(container_id)"
    before_state="$(restart_state)"
    in_machine docker kill --signal KILL anheart > /dev/null
    deaths=$((deaths + 1))
    wait_for_replacement "$before_id" 60 "after death $deaths"
    wait_for_console "after death $deaths"
    # A console writes its start on the disk after its first control tick,
    # once the drive is read: one killed before that is not counted. Each of
    # these deaths is of a console whose start was written.
    for ((attempt = 0; attempt < 30; attempt++)); do
        [ "$(restart_state)" != "$before_state" ] && break
        sleep 1
    done
    [ "$(restart_state)" != "$before_state" ] || fail "the console did not write its start after death $deaths"
done
[ "$deaths" -ge 1 ] || fail "the console was out of service before it died once more"
ok "after $deaths more death(s) the console is out of service"
expect_in "$(panel)" '"unclean_starts":3,' "at its third start after an unclean stop in ten minutes"
[ "$(in_machine systemctl is-active anheart)" = "active" ] || fail "the service is not running"
expect_in "$(healthz)" '"status":"ok"' "out of service, the console still runs and answers"
expect_in "$(in_machine journalctl --unit anheart --no-pager --since "@$begin")" "out_of_service: console hors service" \
    "the journal says the console is out of service"
post /api/safety/attest '{"operator":"install-test","sto_jumper_removed":true,"mushroom_wired_nc":true}' > /dev/null \
    || fail "the console out of service refused the attestation"
post /api/manual/start '{"occupancy":"bench","operator":"install-test"}' > /dev/null \
    || fail "the console out of service did not take the request to start"
sleep 3
expect_in "$(status)" '"run_state":"idle"' "out of service, a start that is asked for is not armed"
answer="$(post /api/service/restore '{"operator":"install-test"}')" \
    || fail "the console refused to be put back in service"
expect_in "$answer" '"out_of_service":false' "a named operator puts the console back in service"
expect_in "$answer" '"persisted":true' "and that is written on the machine"
expect_in "$(status)" '"run_state":"idle"' "putting it back in service starts nothing"
start_session "manual bench session on the console back in service"
ok "back in service, a session starts when an operator starts one"
post /api/session/stop '{"operator":"install-test","reason":"install test"}' > /dev/null \
    || fail "the simulated console refused to end the session"
wait_for_run_state idle "after the session was ended at the console"
in_machine systemctl restart anheart || fail "systemctl restart anheart failed"
wait_for_console "after a restart by hand"
expect_in "$(panel)" '"out_of_service":false' "still in service after a clean restart"
expect_at_rest "after the restart of the console back in service"
# Sessions, stops, kills, a freeze, a power-on: all of it was written by the
# console as the account, and none of it is open to anybody else.
expect_private "after every step above"

# --- 11. A machine that drifted --------------------------------------------------------

step "11. A machine that drifted: harden.sh says what it would change, changes nothing, then does it"
# What an older install, or a hand, can leave behind. The console is at rest:
# it reads its restart state at a start only, and a wider mode takes nothing
# from it.
left_over="/var/lib/anheart/data/left-by-an-older-install.json"
restart_state="/var/lib/anheart/records/service-state.json"
logbook="/var/lib/anheart/records/logbook/events.jsonl"
in_machine sh -c "
    printf '%s\n' '{}' > '$left_over' && chmod 0644 '$left_over' &&
    chown root:root '$restart_state' &&
    chmod 0644 '$logbook' &&
    chmod 0755 /var/lib/anheart/records &&
    chmod 0644 '$ENV_FILE' &&
    usermod --shell /bin/bash anheart &&
    systemctl unmask apt-daily.timer &&
    rm /etc/apt/apt.conf.d/99-anheart-no-automatic-updates" > /dev/null 2>&1 \
    || fail "the test could not make the machine drift"
# Owners, modes and contents of what was changed by hand, and the rest of the drift.
drifted() {
    in_machine sh -c "stat --format '%n %U:%G %a' '$left_over' '$restart_state' '$logbook' /var/lib/anheart/records '$ENV_FILE'; sha256sum '$left_over' '$restart_state' '$logbook' '$ENV_FILE'; getent passwd anheart; systemctl is-enabled apt-daily.timer; ls /etc/apt/apt.conf.d" 2>&1 || true
}
contents() { in_machine sha256sum "$left_over" "$restart_state" "$logbook" "$ENV_FILE"; }
before_state="$(drifted)"
before_id="$(container_id)"
output="$(harden --dry-run 2>&1)" || { printf '%s\n' "$output"; fail "harden.sh --dry-run failed"; }
printf '%s\n' "$output"
for expected in \
    "would give to anheart:anheart: $left_over" \
    "would give to anheart:anheart: $restart_state" \
    "would close to its group and to everybody else: $left_over" \
    "would close to its group and to everybody else: $logbook" \
    "would set the mode of /var/lib/anheart/records to 0700" \
    "would set the mode of $ENV_FILE to 0600" \
    "would give the account anheart the shell /usr/sbin/nologin in place of /bin/bash" \
    "would mask apt-daily.timer" \
    "would write /etc/apt/apt.conf.d/99-anheart-no-automatic-updates"; do
    expect_in "$output" "$expected" "the dry run says: $expected"
done
# Nine lines above, nine changes counted: no more than what was done by hand.
announced="$(printf '%s\n' "$output" | sed -n 's/^harden.sh, dry run: \([0-9]*\) change(s) would be made, none was$/\1/p')"
[ "$announced" = "9" ] || fail "the dry run announces '${announced:-no}' change(s), 9 are expected"
[ "$(printf '%s\n' "$output" | grep -c '^   would ')" = "9" ] \
    || fail "the dry run does not print one line for each change it counts"
ok "the dry run announces these 9 changes and no other, one line each, and says none was made"
[ "$(drifted)" = "$before_state" ] || fail "the dry run changed something: $(drifted)"
ok "the dry run changed nothing"
before_sums="$(contents)"
output="$(harden 2>&1)" || { printf '%s\n' "$output"; fail "harden.sh failed"; }
printf '%s\n' "$output"
expect_in "$output" "harden.sh: 9 change(s) made" "the run makes the changes the dry run announced, and no other"
expect_in "$(owned "$left_over")" "anheart:anheart 600" "a file root left in the data is the account's alone"
expect_in "$(owned "$restart_state")" "anheart:anheart 600" "the restart state is the account's again"
expect_in "$(owned "$logbook")" "anheart:anheart 600" "the logbook is closed again"
expect_in "$(owned /var/lib/anheart/records)" "anheart:anheart 700" "the records are closed again"
expect_in "$(owned "$ENV_FILE")" "root:root 600" "the configuration is root's alone again"
expect_in "$(in_machine getent passwd anheart)" ":/usr/sbin/nologin" "the account cannot log in again"
expect_no_automatic_update "after harden.sh"
expect_private "after harden.sh"
[ "$(contents)" = "$before_sums" ] || fail "harden.sh changed the content of a file it only had to protect"
ok "no content was touched"
[ "$(container_id)" = "$before_id" ] || fail "harden.sh restarted the console"
expect_at_rest "harden.sh left the console running"
output="$(harden --dry-run 2>&1)" || { printf '%s\n' "$output"; fail "the second dry run failed"; }
expect_in "$output" "harden.sh, dry run: nothing to change" "a second dry run finds nothing left to change"
expect_in "$(in_machine bash "$COPY/scripts/harden.sh" --list)" "updates" "harden.sh lists its steps"

# --- 12. Uninstall, then install again -------------------------------------------------

step "12. Uninstall takes back what was set in the system and keeps the machine's own; the install can be run again"
kept() { in_machine sh -c "sha256sum '$ENV_FILE' '$left_over'; ls /var/lib/anheart/records; id anheart" 2>&1 || true; }
kept_before="$(kept)"
in_machine bash "$COPY/scripts/uninstall.sh" || fail "uninstall.sh failed"
[ "$(in_machine systemctl is-active anheart 2> /dev/null || true)" != "active" ] || fail "the service still runs"
[ -z "$(in_machine docker ps --all --quiet)" ] || fail "a console container outlived the uninstall"
ok "no console runs"
for taken in \
    /etc/systemd/system/anheart.service \
    /etc/systemd/system/anheart.service.d \
    /usr/local/lib/anheart \
    /etc/chrony/conf.d/anheart.conf \
    /etc/systemd/journald.conf.d/anheart.conf \
    /etc/systemd/system.conf.d/10-anheart-watchdog.conf \
    /etc/udev/rules.d/70-anheart-drive.rules \
    /etc/apt/apt.conf.d/99-anheart-no-automatic-updates; do
    in_machine test ! -e "$taken" || fail "uninstall.sh left $taken"
done
ok "the unit, what it ran, and every setting written for the console are gone"
for unit in $UPDATE_UNITS; do
    [ "$(in_machine systemctl is-enabled "$unit" 2> /dev/null || true)" != "masked" ] || fail "$unit is still masked"
done
expect_in "$(in_machine apt-config dump)" 'APT::Periodic::Unattended-Upgrade "1";' \
    "the system updates its packages by itself again, as before the install"
case "$(in_machine systemd-analyze cat-config systemd/system.conf)" in
    *RuntimeWatchdogSec=15s*) fail "systemd is still told to arm the hardware watchdog" ;;
esac
ok "systemd is no longer told to arm the hardware watchdog"
[ "$(kept)" = "$kept_before" ] \
    || fail "uninstall.sh touched the configuration, the records or the account"
ok "the configuration, the records and the account are as they were"
expect_in "$(owned "$ENV_FILE")" "root:root 600" "the configuration is still root's alone"
expect_private "after the uninstall"
expect_in "$(in_machine docker image ls --format '{{.Repository}}:{{.Tag}}')" "anheart-console:$THIRD_VERSION" \
    "the image is still on the machine"
[ "$(in_machine systemctl is-enabled chrony)" = "enabled" ] || fail "the machine was left without its time service"
ok "the time service is still installed and enabled"
install || fail "install.sh failed after an uninstall"
wait_for_console "after the install that follows an uninstall"
expect_at_rest "installed again"
expect_no_automatic_update "installed again"
expect_private "installed again"

step "Versions seen in this run"
in_machine dpkg-query --show --showformat='  ${Package} ${Version}\n' docker.io bluez libusb-1.0-0 socat systemd chrony
in_machine docker exec anheart python --version | sed 's/^/  /'
in_machine docker image ls --format '  {{.Repository}}:{{.Tag}} {{.Size}}' anheart-console
printf '\nINSTALL TEST PASSED\n'

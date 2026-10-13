#!/usr/bin/env bash
# Remove the AnHeart console from a Raspberry Pi: the service, and everything
# scripts/install.sh and scripts/harden.sh set in the system for it.
#
#     sudo bash scripts/uninstall.sh       (from raspberry-pi/, on the Pi)
#
# Stops the console the way it is always stopped (SIGTERM: the speed reference
# is zeroed and the drive link released), then takes back, in this order:
#
#   * the systemd unit, its drop-ins, and what it ran before the console
#     (/usr/local/lib/anheart);
#   * the settings written for the console: the time service's
#     (/etc/chrony/conf.d/anheart.conf), the journal's
#     (/etc/systemd/journald.conf.d/anheart.conf), the hardware watchdog's
#     (/etc/systemd/system.conf.d/10-anheart-watchdog.conf) and the rule that
#     gives the drive cable to the console (/etc/udev/rules.d/70-anheart-drive.rules);
#   * what scripts/harden.sh set (`harden.sh --undo`): the system updates its
#     packages by itself again, as the distribution has it.
#
# After it the machine behaves as before the install, with three things left
# on purpose, because they are the machine's and not this script's to delete:
#
#   * the configuration, /etc/anheart/anheart.env: it holds the machine's API key;
#   * the data, /var/lib/anheart (profiles, session records, logbook), and the
#     account `anheart` that owns them. Both stay as private as they were;
#   * the Docker images and the system packages (docker.io, bluez, chrony,
#     socat...). chrony stays the time service; the journal already on the disk
#     is kept, and goes on being written there.
#
# Safe to run again: what is already gone is not an error.
#
# ANHEART_ROOT, when set, is prepended to every path this script removes:
# tests/test_pi_install.py runs it against a scratch directory. It is the only
# test hook; leave it unset on a machine.
set -euo pipefail

readonly SERVICE="anheart"
readonly ROOT="${ANHEART_ROOT:-}"
readonly UNIT_DIR="$ROOT/etc/systemd/system"
readonly HELPER_DIR="$ROOT/usr/local/lib/anheart"
readonly CHRONY_CONF="$ROOT/etc/chrony/conf.d/anheart.conf"
readonly JOURNALD_CONF="$ROOT/etc/systemd/journald.conf.d/anheart.conf"
readonly MANAGER_DROP_IN="$ROOT/etc/systemd/system.conf.d/10-anheart-watchdog.conf"
readonly DEVICE_RULE="$ROOT/etc/udev/rules.d/70-anheart-drive.rules"

TREE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TREE
readonly HARDEN="$TREE/scripts/harden.sh"

say()  { printf '== %s\n' "$1"; }
note() { printf '   %s\n' "$1"; }
warn() { printf '   WARNING: %s\n' "$1" >&2; }
die()  { printf 'uninstall.sh: %s\n' "$1" >&2; exit 1; }

# gone PATH: removed if it is there. The exit status says which: 0 removed, 1
# it was not there. Called as the condition of an `if`.
gone() {
    [ -e "$1" ] || [ -L "$1" ] || return 1
    rm -rf "$1" || die "cannot remove $1"
    note "$1 removed"
}

[ "$(id -u)" -eq 0 ] || die "run it as root: sudo bash scripts/uninstall.sh"

say "Console"
# The controlled stop, then no start at boot. A service that is not installed
# is not an error.
systemctl disable --now "$SERVICE.service" 2> /dev/null || true
gone "$UNIT_DIR/$SERVICE.service" || note "the unit was not installed"
gone "$UNIT_DIR/$SERVICE.service.d" || true
gone "$HELPER_DIR" || true
systemctl daemon-reload

say "Settings written for the console"
if gone "$CHRONY_CONF"; then
    # Read at its start only. chrony itself stays: the machine keeps a time service.
    systemctl try-restart chrony.service || true
fi
if gone "$JOURNALD_CONF"; then
    # The journal already on the disk is kept, and journald goes on writing
    # there; only the bound of 200 MB goes back to the distribution's.
    systemctl restart systemd-journald.service \
        || warn "the system journal was not restarted: its settings change at the next start of the machine"
fi
if gone "$MANAGER_DROP_IN"; then
    note "systemd no longer arms the hardware watchdog from the next start of the machine"
fi
if gone "$DEVICE_RULE"; then
    if command -v udevadm > /dev/null 2>&1; then
        udevadm control --reload || true
    fi
fi

say "Hardening (scripts/harden.sh --undo)"
if [ -r "$HARDEN" ]; then
    bash "$HARDEN" --undo
else
    warn "$HARDEN is missing: what it set in the system is left as it is"
fi

say "Removed: the console and what was set in the system for it"
note "left in place: $ROOT/etc/anheart/anheart.env, $ROOT/var/lib/anheart, the account anheart, the Docker images, the system packages"

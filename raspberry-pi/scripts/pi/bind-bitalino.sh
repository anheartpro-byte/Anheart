#!/bin/sh
# Bind the BITalino's serial node on the Raspberry Pi, before the console starts.
#
# Installed by scripts/install.sh as /usr/local/lib/anheart/bind-bitalino and
# run by the service (scripts/anheart.service, ExecStartPre), as root, at every
# start of the console. Not meant to be run by hand, though it can be.
#
# Why here and not in the image. The console reads the BITalino through a
# serial node, /dev/rfcommN (ECG_SOURCE=serial, BITALINO_ADDRESS=/dev/rfcommN).
# The kernel only makes that node when asked to bind it to a device, and asking
# takes a right over the machine's network (CAP_NET_ADMIN). The console runs in
# its container as an account with no capability at all, so the service does it
# on the machine, where nothing else has to be given that right.
#
# A binding does not survive a restart of the machine, and the sensor's address
# may have been changed in the configuration since the last start: the node is
# released, then bound again, every time. The console is not running then.
#
# Best effort, like the pairing itself: whatever happens this script says so
# and ends with 0. The console starts all the same, shows the ECG link as down
# and refuses any programme, which is the right behaviour for a missing sensor.
#
# The three keys are read from the machine's configuration the way
# `docker run --env-file` reads them: everything after the `=`.
# ANHEART_ENV_FILE names another file, for tests/test_pi_install.py.
set -u

env_file="${ANHEART_ENV_FILE:-/etc/anheart/anheart.env}"

value() {
    sed -n "s/^$1=//p" "$env_file" 2>/dev/null | tail -n 1
}

[ -r "$env_file" ] || exit 0
[ "$(value ECG_SOURCE)" = "serial" ] || exit 0

address="$(value BITALINO_ADDRESS)"
case "$address" in
    /dev/rfcomm[0-9] | /dev/rfcomm[0-9][0-9]) ;;
    # A serial port of another kind, or a Bluetooth address: nothing to bind.
    *) exit 0 ;;
esac
index="${address#/dev/rfcomm}"

mac="$(value BITALINO_MAC)"
if ! printf '%s\n' "$mac" | grep -Eq '^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$'; then
    echo "bind-bitalino: BITALINO_MAC is not the address of a sensor: $address is not bound" >&2
    exit 0
fi

if ! command -v rfcomm >/dev/null 2>&1; then
    echo "bind-bitalino: no rfcomm tool on this machine (package bluez): $address is not bound" >&2
    exit 0
fi

# Nothing to release on the first start after power-on: not an error.
rfcomm release "$index" >/dev/null 2>&1
if rfcomm bind "$index" "$mac" 1; then
    echo "bind-bitalino: $address is bound to the BITalino $mac"
else
    echo "bind-bitalino: $address could not be bound to $mac (is the BITalino paired? bash scripts/pair_device.sh)" >&2
fi
exit 0

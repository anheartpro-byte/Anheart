#!/bin/sh
# Container entrypoint for the AnHeart console.
#
# One job before handing over: when the BITalino is read through a serial
# RFCOMM node (ECG_SOURCE=serial, BITALINO_ADDRESS=/dev/rfcommN) and
# BITALINO_MAC is set, bind that node to the paired device. The vendor library
# opens a MAC address through PyBluez, which does not install on Python 3.12,
# so on the Pi the supported path is the serial node.
#
# The bind is best effort: a failure is reported and the console still starts.
# It then shows the ECG link as down and refuses any programme, which is the
# right behaviour for a missing sensor.
set -u

case "${ECG_SOURCE:-}:${BITALINO_ADDRESS:-}" in
    serial:/dev/rfcomm*)
        if [ -n "${BITALINO_MAC:-}" ] && [ ! -e "${BITALINO_ADDRESS}" ]; then
            index="${BITALINO_ADDRESS#/dev/rfcomm}"
            if command -v rfcomm >/dev/null 2>&1; then
                echo "entrypoint: rfcomm bind ${index} ${BITALINO_MAC} 1"
                rfcomm bind "${index}" "${BITALINO_MAC}" 1 \
                    || echo "entrypoint: rfcomm bind failed (is the BITalino paired on the host?)" >&2
            else
                echo "entrypoint: no rfcomm tool in this image; bind ${BITALINO_ADDRESS} on the host" >&2
            fi
        fi
        ;;
esac

# Nothing about the version here: the console reads the VERSION file of the
# image itself (src/contract.py), for its page, for the dashboard and for the
# manifest of every session record alike.

# exec: the console must be PID 1's direct child target of SIGTERM, so that
# `docker stop` reaches its controlled ramp-down.
exec "$@"

#!/bin/sh
# Container entrypoint for the AnHeart console.
#
# Two jobs before handing over; the second is further down, next to its code.
#
# The first: when the BITalino is read through a serial
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

# The record of each session is stamped with the software version, which the
# console reads from ANHEART_SOFTWARE_VERSION (src/local_config.py). Unless the
# configuration sets it, it is the version this image was built from: the
# VERSION file next to src/ (the working directory, /app in the image). A
# missing or malformed file changes nothing: the console starts all the same
# and stamps "unversioned", as it does without this.
if [ -z "${ANHEART_SOFTWARE_VERSION:-}" ] && [ -r VERSION ]; then
    built="$(cat VERSION)"
    case "${built}" in
        "" | *[!A-Za-z0-9_.+-]*) ;;
        *)
            if [ "${#built}" -le 128 ]; then
                export ANHEART_SOFTWARE_VERSION="${built}"
            fi
            ;;
    esac
fi

# exec: the console must be PID 1's direct child target of SIGTERM, so that
# `docker stop` reaches its controlled ramp-down.
exec "$@"

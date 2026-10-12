#!/bin/sh
# Container entrypoint for the AnHeart console.
#
# Two things before handing over, and neither needs a privilege.
#
# Every file the console creates from here on is its account's alone: the
# session records set their own modes, this covers the rest (the local
# profiles, an archive being exported, the lock on the drive cable).
#
# And one line of diagnosis. When the BITalino is read through a serial RFCOMM
# node (ECG_SOURCE=serial, BITALINO_ADDRESS=/dev/rfcommN), that node is bound
# to the paired sensor on the machine itself, by the service, before this image
# starts (scripts/pi/bind-bitalino.sh): binding takes a right over the network
# that this container is not given. A node that is not there is said here; the
# console still starts. It then shows the ECG link as down and refuses any
# programme, which is the right behaviour for a missing sensor.
set -u

umask 077

case "${ECG_SOURCE:-}:${BITALINO_ADDRESS:-}" in
    serial:/dev/rfcomm*)
        if [ ! -e "${BITALINO_ADDRESS}" ]; then
            echo "entrypoint: ${BITALINO_ADDRESS} is not there. The service binds it before it starts the console (BITALINO_MAC, a BITalino paired on the machine); an image run by hand needs: sudo rfcomm bind <N> <address> 1" >&2
        fi
        ;;
esac

# Nothing about the version here: the console reads the VERSION file of the
# image itself (src/contract.py), for its page, for the dashboard and for the
# manifest of every session record alike.

# exec: the console must be PID 1's direct child target of SIGTERM, so that
# `docker stop` reaches its controlled ramp-down.
exec "$@"

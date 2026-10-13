#!/usr/bin/env bash
set -euo pipefail
[ -f /.dockerenv ] || { echo 'Run this test inside its isolated Docker container.' >&2; exit 2; }
tree="$(cd "$(dirname "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
export ANHEART_ROOT="$work/root"
mkdir -p "$ANHEART_ROOT/etc/anheart" "$ANHEART_ROOT/etc/ssh" "$work/bin" /run/sshd
id anheart >/dev/null 2>&1 || useradd --system --user-group --shell /usr/sbin/nologin anheart
cat > "$work/bin/systemctl" <<'EOF'
#!/bin/sh
[ "$1" != is-active ]
EOF
cat > "$work/bin/docker" <<'EOF'
#!/bin/sh
exit 1
EOF
chmod +x "$work/bin/"*
export PATH="$work/bin:$PATH"
cat > "$ANHEART_ROOT/etc/anheart/network.conf" <<'EOF'
MACHINE_ID=software-test
AP_INTERFACE=ap0
UPLINK_INTERFACE=wan0
EOF
cat > "$ANHEART_ROOT/etc/anheart/anheart.env" <<'EOF'
UI_PORT=8090
UI_HOST=127.0.0.1
UI_TOKEN=synthetic-network-test-token
EOF
printf 'Include %s/etc/ssh/sshd_config.d/*.conf\n' "$ANHEART_ROOT" > "$ANHEART_ROOT/etc/ssh/sshd_config"
fingerprint() {
    find "$ANHEART_ROOT" -type f -exec sha256sum '{}' + | sort
    find "$ANHEART_ROOT" -type f -printf '%p %m %u %g\n' | sort
}
before="$(fingerprint)"
bash "$tree/scripts/harden.sh" network --dry-run > "$work/dry.log"
[ "$(fingerprint)" = "$before" ]
bash "$tree/scripts/harden.sh" network > "$work/first.log"
nmcli --offline connection modify connection.id anheart-ap < "$ANHEART_ROOT/etc/NetworkManager/system-connections/anheart-ap.nmconnection" > /dev/null
sshd -T -f "$ANHEART_ROOT/etc/ssh/sshd_config" | grep '^passwordauthentication no$' > /dev/null
password="$(cat "$ANHEART_ROOT/etc/anheart/wifi-password")"
! grep -F "$password" "$work/first.log"
! grep -F 'synthetic-network-test-token' "$work/first.log"
[ "$(stat -c '%a' "$ANHEART_ROOT/etc/anheart/wifi-password")" = 600 ]
[ "$(stat -c '%a' "$ANHEART_ROOT/etc/NetworkManager/system-connections/anheart-ap.nmconnection")" = 600 ]
before="$(fingerprint)"
bash "$tree/scripts/harden.sh" network > "$work/second.log"
[ "$(fingerprint)" = "$before" ]
bash "$tree/scripts/harden.sh" network --dry-run > "$work/dry-again.log"
[ "$(fingerprint)" = "$before" ]
printf 'MACHINE_ID=bad;id\nAP_INTERFACE=ap0\nUPLINK_INTERFACE=wan0\n' > "$ANHEART_ROOT/etc/anheart/network.conf"
before="$(fingerprint)"
if bash "$tree/scripts/harden.sh" network > "$work/invalid.log" 2>&1; then exit 1; fi
[ "$(fingerprint)" = "$before" ]
printf 'MACHINE_ID=software-test\nAP_INTERFACE=wan0\nUPLINK_INTERFACE=wan0\n' > "$ANHEART_ROOT/etc/anheart/network.conf"
if bash "$tree/scripts/harden.sh" network > "$work/same.log" 2>&1; then exit 1; fi
echo 'PASS: real configuration parsing, dry run, private secrets, idempotence, invalid input refusal'
python3 "$tree/scripts/pi/test_network_packets.py" "$ANHEART_ROOT/etc/anheart/firewall.nft"

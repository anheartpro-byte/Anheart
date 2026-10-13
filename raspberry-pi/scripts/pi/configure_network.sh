#!/usr/bin/env bash
set -euo pipefail
umask 077

root="${ANHEART_ROOT:-}"
config="$root/etc/anheart/network.conf"
env_file="$root/etc/anheart/anheart.env"
dry=false
activate=false
undo=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) dry=true ;;
        --activate) activate=true ;;
        --undo) undo=true ;;
        --help)
            echo 'Usage: configure_network.sh [--dry-run] [--activate] [--undo]'
            echo 'Reads /etc/anheart/network.conf: MACHINE_ID, AP_INTERFACE, UPLINK_INTERFACE.'
            exit 0 ;;
        *) echo 'Unknown argument' >&2; exit 2 ;;
    esac
done
die() { echo "network: $1" >&2; exit 1; }
value() { sed -n "s/^$2=//p" "$1" | tail -n 1; }
[ "$(id -u)" = 0 ] || die 'run as root'
[ -f "$config" ] && [ ! -L "$config" ] || die 'network.conf must be a regular file'
[ -f "$env_file" ] && [ ! -L "$env_file" ] || die 'install the console first'
machine="$(value "$config" MACHINE_ID)"
ap="$(value "$config" AP_INTERFACE)"
uplink="$(value "$config" UPLINK_INTERFACE)"
[[ "$machine" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,19}$ ]] || die 'invalid MACHINE_ID (1..20 letters, digits, underscore or dash)'
for interface in "$ap" "$uplink"; do
    [[ "$interface" =~ ^[a-zA-Z][a-zA-Z0-9_-]{0,14}$ ]] || die 'invalid interface'
    [ "$interface" != lo ] || die 'loopback is not a network interface for this setup'
done
[ "$ap" != "$uplink" ] || die 'AP and uplink must be distinct interfaces'
port="$(value "$env_file" UI_PORT)"
[[ "$port" =~ ^[0-9]{4,5}$ ]] && [ "$port" -ge 1024 ] && [ "$port" -le 65535 ] || die 'UI_PORT must be 1024..65535'
[ "$port" != 8123 ] || die 'UI_PORT 8123 is reserved for the bench tool'
token="$(value "$env_file" UI_TOKEN)"
[ "${#token}" -ge 16 ] || die 'configure a UI_TOKEN of at least 16 characters first'
if systemctl is-active --quiet anheart.service; then
    die 'stop the console deliberately before changing its network'
fi
if [ "$(docker container inspect --format '{{.State.Running}}' anheart 2>/dev/null || true)" = true ]; then
    die 'stop the manually started console before changing its network'
fi

profile="$root/etc/NetworkManager/system-connections/anheart-ap.nmconnection"
firewall="$root/etc/anheart/firewall.nft"
dns="$root/etc/anheart/dnsmasq.conf"
credentials="$root/etc/anheart/wifi-password"
unit_dir="$root/etc/systemd/system"
ssh="$root/etc/ssh/sshd_config.d/00-anheart.conf"
sudoers="$root/etc/sudoers.d/zz-anheart-deny"
tls="$root/var/lib/anheart/tls"

write_file() {
    local path="$1" mode="$2" content draft
    content="$(cat)"
    [ ! -L "$path" ] || die "refusing symbolic link: $path"
    if [ -f "$path" ] && [ "$(cat "$path")" = "$content" ] &&
        [ "$(stat -c '%a:%u:%g' "$path")" = "${mode#0}:0:0" ]; then
        return
    fi
    echo "network: write $path (mode $mode)"
    $dry && return
    mkdir -p "$(dirname "$path")"
    draft="$(mktemp "$(dirname "$path")/.anheart.XXXXXX")"
    printf '%s\n' "$content" > "$draft"
    chmod "$mode" "$draft"
    chown root:root "$draft"
    mv "$draft" "$path"
}

if $undo; then
    $dry && { echo 'network: would remove managed network configuration; credentials and TLS retained'; exit 0; }
    systemctl disable --now anheart-dns.service
    nmcli connection down anheart-ap 2>/dev/null || true
    rm -f "$profile" "$dns" "$ssh" "$sudoers" "$unit_dir/anheart-dns.service"
    echo 'network: AP removed; firewall retained closed. Reconfigure SSH and the console before removing it.'
    systemctl daemon-reload
    exit 0
fi

for tool in nmcli nft dnsmasq sshd visudo openssl; do
    command -v "$tool" >/dev/null || die "install prerequisite: $tool"
done

if [ -f "$credentials" ] && [ ! -L "$credentials" ]; then
    password="$(cat "$credentials")"
    [[ "$password" =~ ^[0-9a-f]{32}$ ]] || die 'invalid saved Wi-Fi password'
elif [ -e "$credentials" ] || [ -L "$credentials" ]; then
    die 'Wi-Fi password is not a regular file'
elif $dry; then
    password=00000000000000000000000000000000
else
    password="$(openssl rand -hex 16)"
fi
write_file "$credentials" 0600 <<< "$password"
write_file "$profile" 0600 <<EOF
[connection]
id=anheart-ap
type=wifi
interface-name=$ap
autoconnect=true
autoconnect-priority=999
[wifi]
mode=ap
ssid=anheart-$machine
band=bg
[wifi-security]
key-mgmt=wpa-psk
proto=rsn;
pairwise=ccmp;
group=ccmp;
psk=$password
[ipv4]
method=manual
address1=10.42.0.1/24
never-default=true
[ipv6]
method=disabled
EOF
write_file "$dns" 0644 <<EOF
interface=$ap
listen-address=10.42.0.1
bind-dynamic
no-resolv
no-hosts
domain-needed
local=/#/
address=/anheart.internal/10.42.0.1
dhcp-range=10.42.0.20,10.42.0.200,255.255.255.0,12h
dhcp-option=3
dhcp-option=6,10.42.0.1
dhcp-leasefile=/run/anheart-dns/leases
EOF
write_file "$firewall" 0600 <<EOF
table inet anheart
delete table inet anheart
table inet anheart {
  chain input {
    type filter hook input priority -10; policy drop;
    iifname "lo" accept
    ct state invalid drop
    ct state established,related accept
    iifname "$ap" udp dport { 53, 67 } accept
    iifname "$ap" tcp dport { 22, 53 } accept
    iifname "$ap" tcp dport $port ct state new meter console_clients { ip saddr ct count over 32 } reject
    iifname "$ap" tcp dport $port accept
    iifname "$uplink" udp sport 67 udp dport 68 accept
    iifname "$uplink" meta l4proto 58 icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert } ip6 hoplimit 255 accept
  }
  chain forward {
    type filter hook forward priority -10; policy accept;
    iifname "$ap" drop
    oifname "$ap" drop
  }
}
EOF
write_file "$ssh" 0600 <<EOF
ListenAddress 10.42.0.1
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
DenyUsers anheart
AllowTcpForwarding no
AllowAgentForwarding no
X11Forwarding no
PermitTunnel no
GatewayPorts no
EOF
write_file "$sudoers" 0440 <<'EOF'
anheart ALL=(ALL:ALL) !ALL
EOF
write_file "$unit_dir/anheart-firewall.service" 0644 <<'EOF'
[Unit]
Description=Anheart console network isolation
Before=NetworkManager.service anheart.service anheart-dns.service ssh.service
[Service]
Type=oneshot
ExecStart=/usr/sbin/nft -f /etc/anheart/firewall.nft
ExecReload=/usr/sbin/nft -f /etc/anheart/firewall.nft
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
write_file "$unit_dir/anheart-dns.service" 0644 <<'EOF'
[Unit]
Description=Anheart access point DHCP and local DNS
Requires=anheart-firewall.service
After=anheart-firewall.service NetworkManager.service
[Service]
Type=simple
RuntimeDirectory=anheart-dns
ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --conf-file=/etc/anheart/dnsmasq.conf
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF
write_file "$unit_dir/anheart.service.d/30-network.conf" 0644 <<'EOF'
[Unit]
Requires=anheart-firewall.service
After=anheart-firewall.service NetworkManager.service
[Service]
ExecStartPre=/usr/bin/nmcli --wait 60 connection up anheart-ap
RestartSec=5
EOF

if $dry; then
    echo 'network: would create/check the per-Pi TLS certificate and configure HTTPS; no secret printed'
    exit 0
fi
bash "$(dirname "$0")/make_console_tls.sh" --dir "$tls" --name 10.42.0.1 --name anheart.internal --owner anheart
draft="$(mktemp "$(dirname "$env_file")/.network-env.XXXXXX")"
awk '!/^(UI_HOST|UI_TLS_CERT|UI_TLS_KEY|UI_TLS_NAMES)=/' "$env_file" > "$draft"
printf '%s\n' 'UI_HOST=10.42.0.1' 'UI_TLS_CERT=/run/anheart-tls/console.crt' 'UI_TLS_KEY=/run/anheart-tls/console.key' 'UI_TLS_NAMES=anheart.internal' >> "$draft"
chmod 0600 "$draft"
chown root:root "$draft"
if cmp -s "$draft" "$env_file"; then rm "$draft"; else mv "$draft" "$env_file"; fi
nft --check --file "$firewall"
dnsmasq --test --conf-file="$dns"
sshd -t -f "$root/etc/ssh/sshd_config"
visudo -cf "$sudoers"
systemctl daemon-reload
systemctl enable anheart-firewall.service anheart-dns.service
echo "network: prepared anheart-$machine; password in $credentials (root only); tablet authority in $tls/authority.crt"
if $activate; then
    systemctl start anheart-firewall.service
    systemctl reload anheart-firewall.service
    nmcli connection load "$profile"
    nmcli connection up anheart-ap
    systemctl restart anheart-dns.service ssh.service
    echo 'network: active; start the console deliberately after checking access from the AP'
else
    echo 'network: not activated; run --activate from the local Pi terminal'
fi

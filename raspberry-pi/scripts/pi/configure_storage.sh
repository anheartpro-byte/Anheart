#!/usr/bin/env bash
set -euo pipefail
umask 077

root="${ANHEART_ROOT:-}"
dry=false
case "${1:-}" in
    --dry-run) dry=true ;;
    --help) echo 'Usage: configure_storage.sh [--dry-run] (existing LUKS2 volume; never formats)'; exit 0 ;;
    '') ;;
    *) echo 'Unknown argument' >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || exit 2
die() { echo "storage: $1" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die 'run as root'
[ -z "$root" ] || [ -f /.dockerenv ] || die 'ANHEART_ROOT is only for isolated container tests'
config="$root/etc/anheart/storage.conf"
key="$root/etc/anheart/data.key"
store="$root/var/lib/anheart-storage"
mapper=/dev/mapper/anheart-data
for file in "$config" "$key" "$root/etc/machine-id"; do
    [ -f "$file" ] && [ ! -L "$file" ] || die "not a regular file: $file"
done
[ "$(stat -c '%a:%u:%g' "$key")" = 600:0:0 ] || die 'data.key must be root:root 600'
[ "$(stat -c '%a:%u:%g' "$config")" = 600:0:0 ] || die 'storage.conf must be root:root 600'
device="$(sed -n 's/^DATA_DEVICE=//p' "$config" | tail -n 1)"
[[ "$device" =~ ^/dev/[a-zA-Z0-9_./:-]+$ ]] && [ -b "$device" ] || die 'DATA_DEVICE must name an existing block device'
identity="$(cat "$root/etc/machine-id")"
[[ "$identity" =~ ^[0-9a-f]{32}$ ]] || die 'invalid machine-id'
for unit in anheart.service docker.service; do
    if systemctl is-active --quiet "$unit"; then die "stop $unit deliberately before preparing storage"; fi
done
for tool in cryptsetup rsync mount findmnt update-initramfs systemd-escape; do
    command -v "$tool" >/dev/null || die "install prerequisite: $tool"
done
[ -f /usr/share/initramfs-tools/scripts/init-bottom/overlayroot ] || die 'install overlayroot first'
grep -Eq '^[[:space:]]*auto_initramfs=1[[:space:]]*$' "$root/boot/firmware/config.txt" || die 'enable auto_initramfs=1 in the Pi firmware configuration first'
if [ -z "$root" ] && [ "$(findmnt -n -o FSTYPE /)" = overlay ]; then
    die 'enter persistent-root maintenance before changing this configuration'
fi
cryptsetup isLuks --type luks2 "$device" || die 'the data device is not LUKS2'
cryptsetup open --test-passphrase --key-file "$key" "$device" || die 'the data key cannot unlock this volume'
uuid="$(cryptsetup luksUUID "$device")"
[[ "$uuid" =~ ^[0-9a-f-]{36}$ ]] || die 'invalid LUKS UUID'
if $dry; then
    echo "storage: would unlock UUID=$uuid, verify ext4 and machine identity, copy stopped-service data and prepare persistent mounts and overlayroot"
    exit 0
fi

opened=false
mounted=false
cleanup() {
    local status=$?
    if [ "$status" -ne 0 ]; then
        if $mounted; then umount "$store" || true; fi
        if $opened; then cryptsetup close anheart-data || true; fi
    fi
}
trap cleanup EXIT
if [ -e "$mapper" ]; then
    backing="$(cryptsetup status anheart-data | awk '$1 == "device:" {print $2}')"
    [ "$(readlink -f "$backing")" = "$(readlink -f "$device")" ] || die 'anheart-data already names another device'
else
    cryptsetup open --key-file "$key" "$device" anheart-data
    opened=true
fi
[ "$(blkid -s TYPE -o value "$mapper")" = ext4 ] || die 'the unlocked volume must already contain ext4'
[ ! -L "$store" ] || die 'storage mount point is a symbolic link'
install -d -m 0700 "$store"
if mountpoint -q "$store"; then
    source="$(findmnt -n -o SOURCE --mountpoint "$store")"
    [ "$(readlink -f "$source")" = "$(readlink -f "$mapper")" ] || die 'storage mount point belongs to another device'
else
    [ -z "$(find "$store" -mindepth 1 -maxdepth 1 -print -quit)" ] || die 'storage mount point is not empty'
    mount -o nosuid,nodev "$mapper" "$store"
    mounted=true
fi
marker="$store/.anheart-machine"
if [ -e "$marker" ]; then
    [ ! -L "$marker" ] && [ "$(cat "$marker")" = "$identity" ] || die 'the volume belongs to another machine'
else
    [ -z "$(find "$store" -mindepth 1 -maxdepth 1 ! -name lost+found -print -quit)" ] || die 'unclaimed volume is not empty'
    printf '%s\n' "$identity" > "$marker"
fi
chmod 0700 "$store"
if [ -z "$root" ]; then journalctl --sync; fi
for item in console docker journal; do
    case "$item" in
        console) source="$root/var/lib/anheart" ;;
        docker) source="$root/var/lib/docker" ;;
        journal) source="$root/var/log/journal" ;;
    esac
    [ ! -L "$store/$item" ] && [ ! -L "$source" ] || die 'refusing a symbolic link in storage paths'
    install -d -m 0700 "$store/$item"
    migrated="$store/.migrated-$item"
    if [ -e "$migrated" ] || [ -L "$migrated" ]; then
        [ -f "$migrated" ] && [ ! -L "$migrated" ] && [ "$(cat "$migrated")" = "$identity" ] || die 'invalid migration receipt'
    else
        if [ -d "$source" ] && [ "$(stat -Lc '%d:%i' "$source")" != "$(stat -Lc '%d:%i' "$store/$item")" ]; then
            rsync -aHAX --numeric-ids "$source/" "$store/$item/"
        fi
        sync -f "$store/$item"
        receipt="$(mktemp "$store/.migration.XXXXXX")"
        printf '%s\n' "$identity" > "$receipt"
        sync -f "$receipt"
        mv "$receipt" "$migrated"
        sync -f "$store"
    fi
done
chown -Rh anheart:anheart "$store/console"
chown root:root "$store" "$store/docker" "$store/journal"
chmod 0700 "$store" "$store/console" "$store/docker" "$store/journal"

write_file() {
    local path="$1" content draft
    content="$(cat)"
    [ ! -L "$path" ] || die "refusing symbolic link: $path"
    if [ -f "$path" ] && [ "$(cat "$path")" = "$content" ]; then return; fi
    mkdir -p "$(dirname "$path")"
    draft="$(mktemp "$(dirname "$path")/.storage.XXXXXX")"
    printf '%s\n' "$content" > "$draft"
    chmod 0644 "$draft"
    mv "$draft" "$path"
    echo "storage: wrote $path"
}
crypttab="$root/etc/crypttab"
[ ! -L "$crypttab" ] || die 'crypttab is a symbolic link'
existing=""
if [ -f "$crypttab" ]; then existing="$(awk '$1 != "anheart-data"' "$crypttab")"; fi
{
    [ -z "$existing" ] || printf '%s\n' "$existing"
    printf 'anheart-data UUID=%s /etc/anheart/data.key luks,nofail\n' "$uuid"
} | write_file "$crypttab"
unit_dir="$root/etc/systemd/system"
storage_unit="$(systemd-escape --path --suffix=mount /var/lib/anheart-storage)"
crypt_unit="$(systemd-escape --template=systemd-cryptsetup@.service anheart-data)"
write_file "$unit_dir/$storage_unit" <<EOF
[Unit]
Requires=$crypt_unit
After=$crypt_unit
[Mount]
What=/dev/mapper/anheart-data
Where=/var/lib/anheart-storage
Type=ext4
Options=nosuid,nodev
[Install]
WantedBy=local-fs.target
EOF
for item in console docker journal; do
    case "$item" in
        console) destination=/var/lib/anheart ;;
        docker) destination=/var/lib/docker ;;
        journal) destination=/var/log/journal ;;
    esac
    name="$(systemd-escape --path --suffix=mount "$destination")"
    write_file "$unit_dir/$name" <<EOF
[Unit]
Requires=$storage_unit
After=$storage_unit
[Mount]
What=/var/lib/anheart-storage/$item
Where=$destination
Type=none
Options=bind
[Install]
WantedBy=local-fs.target
EOF
    systemctl enable "$name"
done
write_file "$unit_dir/anheart.service.d/40-storage.conf" <<'EOF'
[Unit]
RequiresMountsFor=/var/lib/anheart /var/lib/docker /var/log/journal
EOF
write_file "$unit_dir/docker.service.d/40-storage.conf" <<'EOF'
[Unit]
RequiresMountsFor=/var/lib/docker
EOF
write_file "$unit_dir/systemd-journal-flush.service.d/40-storage.conf" <<'EOF'
[Unit]
RequiresMountsFor=/var/log/journal
EOF
write_file "$root/etc/overlayroot.local.conf" <<'EOF'
overlayroot="tmpfs:recurse=0"
overlayroot_cfgdisk="disabled"
EOF
write_file "$root/etc/systemd/journald.conf.d/anheart.conf" <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=200M
EOF
systemctl daemon-reload
systemctl enable "$storage_unit"
update-initramfs -u -k all
echo 'storage: prepared; keep services stopped, reboot, then verify overlay and persistent mounts before commissioning'

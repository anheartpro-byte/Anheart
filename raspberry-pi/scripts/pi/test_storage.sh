#!/usr/bin/env bash
set -euo pipefail
[ -f /.dockerenv ] || { echo 'Run inside the isolated storage-test container.' >&2; exit 2; }
if [ -e /dev/mapper/anheart-data ]; then echo 'Test mapping already exists; refusing.' >&2; exit 2; fi
tree="$(cd "$(dirname "$0")/../.." && pwd)"
work="$(mktemp -d)"
loop=""
cleanup() {
    umount "$work/merged" "$work/ram" "$work/root/var/lib/anheart" "$work/root/var/lib/anheart-storage" 2>/dev/null || true
    cryptsetup close anheart-data 2>/dev/null || true
    [ -z "$loop" ] || losetup -d "$loop"
    rm -rf "$work"
}
trap cleanup EXIT
export ANHEART_ROOT="$work/root"
id anheart >/dev/null 2>&1 || useradd --system --user-group --shell /usr/sbin/nologin anheart
mkdir -p "$ANHEART_ROOT/etc/anheart" "$ANHEART_ROOT/boot/firmware" "$ANHEART_ROOT/var/lib/anheart" "$work/bin"
mkdir -p "$ANHEART_ROOT/tmp" "$work/container"
printf '%s\n' 0123456789abcdef0123456789abcdef > "$ANHEART_ROOT/etc/machine-id"
printf '%s\n' auto_initramfs=1 > "$ANHEART_ROOT/boot/firmware/config.txt"
printf 'synthetic record\n' > "$ANHEART_ROOT/var/lib/anheart/record.txt"
cat > "$work/bin/systemctl" <<'EOF'
#!/bin/sh
[ "$1" != is-active ]
EOF
cat > "$work/bin/update-initramfs" <<'EOF'
#!/bin/sh
test "$*" = '-u -k all'
EOF
chmod +x "$work/bin/"*
export PATH="$work/bin:$PATH"
umask 077
head -c 32 /dev/urandom > "$ANHEART_ROOT/etc/anheart/data.key"
truncate -s 128M "$work/disk.img"
loop="$(losetup --find --show "$work/disk.img")"
printf 'DATA_DEVICE=%s\n' "$loop" > "$ANHEART_ROOT/etc/anheart/storage.conf"
cryptsetup luksFormat --batch-mode --type luks2 --pbkdf pbkdf2 --iter-time 1 --key-file "$ANHEART_ROOT/etc/anheart/data.key" "$loop"
cryptsetup open --key-file "$ANHEART_ROOT/etc/anheart/data.key" "$loop" anheart-data
mkfs.ext4 -q /dev/mapper/anheart-data
cryptsetup close anheart-data
before="$(find "$ANHEART_ROOT" -type f -exec sha256sum '{}' + | sort)"
bash "$tree/scripts/pi/configure_storage.sh" --dry-run
[ "$(find "$ANHEART_ROOT" -type f -exec sha256sum '{}' + | sort)" = "$before" ]
[ ! -e /dev/mapper/anheart-data ]
bash "$tree/scripts/pi/configure_storage.sh"
store="$ANHEART_ROOT/var/lib/anheart-storage"
cmp "$store/console/record.txt" "$ANHEART_ROOT/var/lib/anheart/record.txt"
[ "$(stat -c '%a:%u:%g' "$store/docker")" = 700:0:0 ]
before="$(find "$ANHEART_ROOT/etc" -type f -exec sha256sum '{}' + | sort)"
bash "$tree/scripts/pi/configure_storage.sh"
[ "$(find "$ANHEART_ROOT/etc" -type f -exec sha256sum '{}' + | sort)" = "$before" ]
mount --bind "$store/console" "$ANHEART_ROOT/var/lib/anheart"
printf 'second record\n' > "$ANHEART_ROOT/var/lib/anheart/second.txt"
umount "$ANHEART_ROOT/var/lib/anheart"
umount "$store"
cryptsetup close anheart-data
cryptsetup open --key-file "$ANHEART_ROOT/etc/anheart/data.key" "$loop" anheart-data
mount /dev/mapper/anheart-data "$store"
grep '^second record$' "$store/console/second.txt"
printf 'wrong key' > "$ANHEART_ROOT/etc/anheart/data.key"
if bash "$tree/scripts/pi/configure_storage.sh" --dry-run > "$work/bad-key.log" 2>&1; then exit 1; fi
mkdir -p "$work/lower" "$work/ram" "$work/merged"
printf 'original\n' > "$work/lower/system.conf"
mount -t tmpfs tmpfs "$work/ram"
mkdir "$work/ram/upper" "$work/ram/work"
mount -t overlay overlay -o "lowerdir=$work/lower,upperdir=$work/ram/upper,workdir=$work/ram/work" "$work/merged"
printf 'temporary\n' > "$work/merged/system.conf"
grep '^original$' "$work/lower/system.conf"
umount "$work/merged" "$work/ram"
lock="$ANHEART_ROOT/tmp/anheart-drive.lock"
printf 'f %s 0660 anheart dialout -\nx %s - - - -\n' "$lock" "$lock" > "$work/tmpfiles.conf"
systemd-tmpfiles --create "$work/tmpfiles.conf"
python3 "$tree/scripts/pi/prepare_drive_lock.py"
inode="$(stat -c '%d:%i' "$lock")"
printf 'held contents' > "$lock"
python3 "$tree/scripts/pi/prepare_drive_lock.py"
systemd-tmpfiles --create --clean "$work/tmpfiles.conf"
[ "$(stat -c '%d:%i' "$lock")" = "$inode" ]
grep '^held contents$' "$lock"
touch "$work/container/lock"
mount --bind "$lock" "$work/container/lock"
if flock -n "$lock" flock -n "$work/container/lock" true; then exit 1; fi
flock -n "$work/container/lock" true
umount "$work/container/lock"
ln "$lock" "$work/second-link"
if python3 "$tree/scripts/pi/prepare_drive_lock.py" > "$work/hardlink.log" 2>&1; then exit 1; fi
rm "$work/second-link"
echo 'PASS: drive lock inode/content retained, host/container contention, kernel release and hardlink refusal'
echo 'PASS: LUKS2 key verification, no-write dry run, persistent records after remount, idempotent config, bad key refused, overlay leaves lower root unchanged'

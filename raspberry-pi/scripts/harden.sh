#!/usr/bin/env bash
# Close what the console of an installed Raspberry Pi does not need.
#
#     sudo bash scripts/harden.sh              every step (from raspberry-pi/, on the Pi)
#     sudo bash scripts/harden.sh --dry-run    say what would change, and change nothing
#     sudo bash scripts/harden.sh files        one step, or several, by name
#     sudo bash scripts/harden.sh --undo       take back what the steps set in the system
#     bash scripts/harden.sh --list            the steps
#
# scripts/install.sh installs the console; this script closes the machine
# around it (docs/pi-image.md, "Durcissement"). install.sh runs the steps
# `account`, `files` and `updates` itself at every install; the others, when
# there are some, are run on purpose, here.
#
# Safe to run again, and safe to stop half-way:
#
#   * a step LOOKS first and acts only on what differs. A second run changes
#     nothing, and `--dry-run` prints, line by line, exactly what a run would
#     do: the same looks, with every change replaced by its description;
#   * no step stops, restarts or reconfigures the console. After any step, and
#     between any two changes of a step, the console can still be started;
#   * nothing is downloaded and nothing is installed.
#
# Writing a step. One name in STEPS, in the order the steps must run, and
# three functions (a dash in the name is an underscore in theirs):
#
#     describe_<name>   one line for --list
#     step_<name>       look, then go through `change` for every change
#     undo_<name>       take back what the step set in the system, through
#                       `change` too; scripts/uninstall.sh runs it
#
# `change WORDS COMMAND...` is the only way a step may change the machine: it
# counts the change, runs COMMAND, and in a dry run only prints WORDS. A step
# that reads a file with a secret prints its name, never its content.
#
# ANHEART_ROOT, when set, is prepended to every path this script reads or
# writes: tests/test_pi_install.py runs it against a scratch directory. It is
# the only test hook; leave it unset on a machine.
set -euo pipefail

readonly ACCOUNT="anheart"
# The group of the serial devices on Debian (see scripts/install.sh).
readonly DEVICE_GROUP="dialout"
readonly NO_LOGIN="/usr/sbin/nologin"

readonly ROOT="${ANHEART_ROOT:-}"
readonly CONFIG_DIR="$ROOT/etc/anheart"
readonly ENV_FILE="$CONFIG_DIR/anheart.env"
readonly STATE_DIR="$ROOT/var/lib/anheart"
# Read by apt after the distribution's own files (10periodic, 20auto-upgrades).
readonly APT_CONF="$ROOT/etc/apt/apt.conf.d/99-anheart-no-automatic-updates"
# What updates the packages of a Debian system by itself.
readonly UPDATE_UNITS=(apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service)

# The steps, in the order they run. `--undo` takes them in the reverse order.
readonly STEPS=(account files updates)

dry_run=false
undo=false
# How many changes were made, or would be.
changes=0
# How many could not be: the script then ends with 1, after every step has run.
left=0

say()  { printf '== %s\n' "$1"; }
note() { printf '   %s\n' "$1"; }
warn() { printf '   WARNING: %s\n' "$1" >&2; }
die()  { printf 'harden.sh: %s\n' "$1" >&2; exit 1; }

usage() {
    cat <<'EOF'
usage: sudo bash scripts/harden.sh [--dry-run] [--undo] [STEP ...]
       bash scripts/harden.sh --list

  --dry-run   print what would change, change nothing
  --undo      take back what the steps set in the system (scripts/uninstall.sh)
  --list      the steps, in the order they run
  STEP ...    only these steps (default: every step)
EOF
}

# change WORDS COMMAND...: see the top of this file.
change() {
    local what="$1"
    shift
    changes=$((changes + 1))
    if $dry_run; then
        printf '   would %s\n' "$what"
        return 0
    fi
    "$@" || die "could not $what"
    printf '   done: %s\n' "$what"
}

# --- Owners and modes -------------------------------------------------------------
#
# Every look is a `find -P`: a symbolic link is looked at and never followed,
# and `chown -h` changes a link, never what it points to. Once the step `files`
# has run, nothing under the console's directories is root's any more: a link
# the console's account plants there later finds nothing of root's to give away.

# ids NAME: the uid and gid of an account of this machine, as `uid:gid`.
ids() {
    local user group
    user="$(id -u "$1" 2> /dev/null)" || return 1
    group="$(id -g "$1" 2> /dev/null)" || return 1
    case "$user$group" in
        '' | *[!0-9]*) return 1 ;;
    esac
    printf '%s:%s' "$user" "$group"
}

# The numbers of the console's account, as `uid:gid`. Never root's: a console
# given them would be root, whatever else this script and the unit do.
account_ids() {
    local numbers
    numbers="$(ids "$ACCOUNT")" \
        || die "the account $ACCOUNT does not exist: sudo bash scripts/install.sh creates it"
    case "$numbers" in
        0:* | *:0) die "the account $ACCOUNT has a number of root's: the console would run as root" ;;
    esac
    printf '%s' "$numbers"
}

# how_many COMMAND...: the number of lines COMMAND prints. An entry that goes
# away while a tree is read is not an error: the console may be running.
how_many() {
    { "$@" || true; } | wc -l | tr -d '[:space:]'
}

entries() {
    if [ "$1" -eq 1 ]; then echo "1 entry"; else echo "$1 entries"; fi
}

# across WOULD DONE FIND... -- ACTION...: one pass over a tree. FIND is the
# arguments of a `find` that selects the entries to put right, and ACTION what
# find does to them, all at once. In a dry run each entry is printed after
# WOULD and nothing is done.
#
# The entries are looked at again after the pass, and what is still selected
# is counted in `left`. It is not taken from the exit status of the pass: an
# entry that went away in between is not a failure.
across() {
    local would="$1" done_words="$2" found still path
    shift 2
    local look=() act=() seen=false word
    for word in "$@"; do
        if [ "$word" = "--" ] && ! $seen; then
            seen=true
        elif $seen; then
            act+=("$word")
        else
            look+=("$word")
        fi
    done
    found="$(how_many find "${look[@]}" -print)"
    [ "$found" -gt 0 ] || return 0
    changes=$((found + changes))
    if $dry_run; then
        { find "${look[@]}" -print || true; } | while IFS= read -r path; do
            printf '   would %s %s\n' "$would" "$path"
        done
        return 0
    fi
    find "${look[@]}" "${act[@]}" || true
    still="$(how_many find "${look[@]}" -print)"
    if [ "$still" -gt 0 ]; then
        left=$((still + left))
        warn "could not $done_words: $(entries "$still") out of $found left as before"
    else
        printf '   done: %s (%s)\n' "$done_words" "$(entries "$found")"
    fi
}

# give NAMES UID:GID TREE: the whole tree, TREE included, belongs to NAMES
# (`user:group`).
give() {
    local names="$1" numbers="$2" tree="$3"
    across "give to $names:" "give what is under $tree to $names" \
        -P "$tree" '(' ! -uid "${numbers%%:*}" -o ! -gid "${numbers##*:}" ')' \
        -- -exec chown -h "$names" '{}' +
}

# close_tree TREE [EXCEPT ...]: nothing in the tree, TREE included, may be
# read, written or entered by its group or by anybody else; the paths EXCEPT
# have a mode of their own (mode_of). Only permissions are removed: what the
# owner may do is left as it is. A symbolic link has no mode of its own.
close_tree() {
    local tree="$1" path
    shift
    local skip=()
    for path in "$@"; do
        skip+=(! -path "$path")
    done
    across "close to its group and to everybody else:" \
        "close what is under $tree to its group and to everybody else" \
        -P "$tree" ${skip[@]+"${skip[@]}"} ! -type l \
        '(' -perm -040 -o -perm -020 -o -perm -010 -o -perm -004 -o -perm -002 -o -perm -001 ')' \
        -- -exec chmod go-rwx '{}' +
}

# owner_of NAMES UID:GID PATH: this one path belongs to NAMES. A path that does
# not exist is not created.
owner_of() {
    local names="$1" numbers="$2" path="$3"
    [ -e "$path" ] || [ -L "$path" ] || return 0
    if [ -n "$(find -P "$path" -maxdepth 0 \( ! -uid "${numbers%%:*}" -o ! -gid "${numbers##*:}" \) -print)" ]; then
        change "give $path to $names" chown -h "$names" "$path"
    fi
}

# mode_of MODE PATH: this one path has exactly this mode. Never a symbolic link.
mode_of() {
    [ -e "$2" ] || return 0
    if [ -n "$(find -P "$2" -maxdepth 0 ! -type l ! -perm "$1" -print)" ]; then
        change "set the mode of $2 to $1" chmod "$1" "$2"
    fi
}

# --- Step: account ------------------------------------------------------------------

describe_account() {
    echo "the account $ACCOUNT cannot log in, has no password, and is in the group of the serial devices"
}

step_account() {
    local entry shell status
    account_ids > /dev/null
    entry="$(getent passwd "$ACCOUNT" || true)"
    [ -n "$entry" ] || die "the account $ACCOUNT does not exist: sudo bash scripts/install.sh creates it"
    shell="${entry##*:}"
    case "$shell" in
        /usr/sbin/nologin | /sbin/nologin | /bin/false | /usr/bin/false) ;;
        *)
            change "give the account $ACCOUNT the shell $NO_LOGIN in place of ${shell:-the default one}" \
                usermod --shell "$NO_LOGIN" "$ACCOUNT"
            ;;
    esac
    # L: locked. Anything else (NP, no password at all; P, a usable one) is a way in.
    status="$(passwd --status "$ACCOUNT" 2> /dev/null | cut -d' ' -f2 || true)"
    if [ "$status" != "L" ]; then
        change "lock the password of the account $ACCOUNT" passwd --lock "$ACCOUNT"
    fi
    case " $(id -nG "$ACCOUNT") " in
        *" $DEVICE_GROUP "*) ;;
        *)
            change "add the account $ACCOUNT to the group $DEVICE_GROUP" \
                usermod --append --groups "$DEVICE_GROUP" "$ACCOUNT"
            ;;
    esac
}

undo_account() {
    note "nothing to take back: the account $ACCOUNT stays, it owns the machine's data"
}

# --- Step: files -----------------------------------------------------------------------

describe_files() {
    echo "the configuration is root's alone; the data, the session records, the logbook and the restart state are the account's alone"
}

step_files() {
    local account root_ids
    # It has said why when it fails.
    account="$(account_ids)" || exit 1
    root_ids="$(ids root)" || die "this machine has no account root"
    # The configuration: the machine's API key, the token of the page, and the
    # limits the console applies. Root's Docker client reads it for the
    # console; the console's own account can neither read nor rewrite it.
    owner_of root:root "$root_ids" "$CONFIG_DIR"
    mode_of 0755 "$CONFIG_DIR"
    owner_of root:root "$root_ids" "$ENV_FILE"
    mode_of 0600 "$ENV_FILE"
    if [ ! -d "$STATE_DIR" ]; then
        note "$STATE_DIR is not there yet: sudo bash scripts/install.sh makes it"
        return 0
    fi
    # Everything the console writes: the local profiles, the session records,
    # the logbook, its restart state. What an older console wrote as root, or
    # with wider modes, is corrected here; no content is read or written.
    # The three directories the installer makes have their mode set whole; in
    # everything under them, only what is open to others is closed.
    local top tops=("$STATE_DIR" "$STATE_DIR/data" "$STATE_DIR/records")
    give "$ACCOUNT:$ACCOUNT" "$account" "$STATE_DIR"
    for top in "${tops[@]}"; do
        mode_of 0700 "$top"
    done
    close_tree "$STATE_DIR" "${tops[@]}"
}

undo_files() {
    note "nothing to take back: the configuration and the data stay as private as they are"
}

# --- Step: updates -----------------------------------------------------------------------

describe_updates() {
    echo "no package is updated unless a person runs apt: the periodic jobs of apt and unattended-upgrades are off"
}

# What the file of this step holds. apt reads it last: it wins over the files
# the distribution or the package unattended-upgrades wrote before it.
apt_settings() {
    cat <<'EOF'
// Written by scripts/harden.sh (docs/pi-image.md, "Durcissement").
// Nothing updates the packages of this machine by itself: one changes when a
// person runs apt, console at rest. Removed by scripts/uninstall.sh.
APT::Periodic::Enable "0";
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::Unattended-Upgrade "0";
APT::Periodic::AutocleanInterval "0";
EOF
}

write_apt_settings() {
    local directory draft
    directory="$(dirname "$APT_CONF")"
    mkdir -p "$directory" || return 1
    draft="$(mktemp "$directory/.anheart.XXXXXX")" || return 1
    apt_settings > "$draft" && chmod 0644 "$draft" && mv "$draft" "$APT_CONF"
}

step_updates() {
    local unit
    if [ ! -f "$APT_CONF" ] || [ "$(cat "$APT_CONF")" != "$(apt_settings)" ]; then
        change "write $APT_CONF (the periodic jobs of apt are off)" write_apt_settings
    fi
    # Masked, not only disabled: a package that is installed or upgraded later
    # cannot turn its own job back on. A job that is running is left to end.
    for unit in "${UPDATE_UNITS[@]}"; do
        if [ "$(systemctl is-enabled "$unit" 2> /dev/null || true)" != "masked" ]; then
            change "mask $unit" systemctl mask "$unit"
        fi
        if systemctl is-active --quiet "$unit"; then
            change "stop $unit" systemctl stop "$unit"
        fi
    done
}

undo_updates() {
    local unit
    if [ -e "$APT_CONF" ]; then
        change "remove $APT_CONF" rm -f "$APT_CONF"
    fi
    for unit in "${UPDATE_UNITS[@]}"; do
        if [ "$(systemctl is-enabled "$unit" 2> /dev/null || true)" = "masked" ]; then
            change "unmask $unit" systemctl unmask "$unit"
        fi
    done
    note "the distribution's own jobs run again from the next start of the machine"
}

# --- Main -----------------------------------------------------------------------------

known() {
    local step
    for step in "${STEPS[@]}"; do
        if [ "$step" = "$1" ]; then return 0; fi
    done
    return 1
}

list() {
    local step
    for step in "${STEPS[@]}"; do
        printf '%-10s %s\n' "$step" "$("describe_${step//-/_}")"
    done
}

# The steps named on the command line, each between two spaces. Empty: all.
named=""
for argument in "$@"; do
    case "$argument" in
        --dry-run) dry_run=true ;;
        --undo) undo=true ;;
        --list) list; exit 0 ;;
        -h | --help) usage; exit 0 ;;
        -*) usage >&2; exit 2 ;;
        *)
            known "$argument" || { usage >&2; exit 2; }
            named="$named $argument "
            ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "run it as root: sudo bash scripts/harden.sh"

run_step() {
    case "$named" in
        "" | *" $1 "*) ;;
        *) return 0 ;;
    esac
    if $undo; then
        say "Taking back: $1"
        "undo_${1//-/_}"
    else
        say "Step: $1"
        "step_${1//-/_}"
    fi
}

# In the order of STEPS, whatever the order the steps were named in; the
# reverse order to take them back.
if $undo; then
    for ((index = ${#STEPS[@]} - 1; index >= 0; index--)); do
        run_step "${STEPS[index]}"
    done
else
    for step in "${STEPS[@]}"; do
        run_step "$step"
    done
fi

if $dry_run; then
    if [ "$changes" -eq 0 ]; then
        echo "harden.sh, dry run: nothing to change"
    else
        echo "harden.sh, dry run: $changes change(s) would be made, none was"
    fi
elif [ "$left" -gt 0 ]; then
    die "$left change(s) out of $changes could not be made: see the warnings above"
elif [ "$changes" -eq 0 ]; then
    echo "harden.sh: nothing to change"
else
    echo "harden.sh: $changes change(s) made"
fi

#!/usr/bin/env bash
# The TLS certificate of the operator console, made ON the Raspberry Pi when
# the machine is commissioned (docs/pi-image.md, "Le certificat de la console").
#
#     sudo bash scripts/pi/make_console_tls.sh --name 10.42.0.1 --name anheart.internal
#     sudo bash scripts/pi/make_console_tls.sh --check
#
# What it leaves in DIR (default /etc/anheart/tls):
#
#   authority.crt   the certificate of this Pi's own authority. Public. The one
#                   file a tablet installs, once, to recognise this console.
#   console.crt     the console's certificate, signed by that authority. Public.
#   console.key     the console's private key. Mode 600. It never leaves the Pi.
#
# The authority belongs to this Pi alone. Its private key is made here, signs
# exactly one certificate (the console's) and is then gone: it only ever exists
# in the memory of this script, is handed to openssl on its standard input, and
# is written to no file and to no command line. Nothing can sign a second
# certificate under an authority a tablet trusts: not this Pi once the script
# has ended, not another Pi, not this repository. The price is the renewal: a
# new certificate means a new authority, which every tablet installs again.
#
# Safe to run again. What is already in DIR and still good for the names asked
# is left as it is (exit 0). What is there and is NOT good (expired, another
# name, a key that does not match) is never replaced without --force: replacing
# it changes the authority, so every tablet has something to do.
#
# This script changes no configuration and restarts nothing. It prints the
# three lines the console's configuration needs; the console reads its
# certificate once, when it starts.
#
# Exit codes: 0 done, or nothing to do; 1 refused or failed; 2 wrong usage.
set -euo pipefail
umask 077

readonly PROGRAM="make_console_tls.sh"

readonly DEFAULT_DIR="/etc/anheart/tls"
# The address NetworkManager gives a Pi that shares a connection as an access
# point, and a name of the zone reserved for private use. Both are assumptions
# until the access point is set up: --name says otherwise.
readonly DEFAULT_NAMES=(10.42.0.1 anheart.internal)
# Always carried too: what the Pi itself calls the console when it listens on
# every address, so that a check run on the Pi can verify the certificate.
readonly LOCAL_NAMES=(127.0.0.1 localhost)

# Longest validity the tablets accept for a server certificate (825 days is
# the limit of iOS and macOS, whatever authority signed it).
readonly MAX_DAYS=825
readonly DEFAULT_MIN_DAYS=30

# 2026-10-01T00:00:00Z. A clock that reads earlier was never set: a Pi has no
# battery, and a certificate dated from such a clock is refused by a tablet.
readonly CLOCK_FLOOR=1790812800

readonly AUTHORITY_FILE="authority.crt"
readonly CERTIFICATE_FILE="console.crt"
readonly KEY_FILE="console.key"

dir="$DEFAULT_DIR"
names=()
label=""
owner=""
days="$MAX_DAYS"
min_days="$DEFAULT_MIN_DAYS"
force=false
dry_run=false
check=false

staging=""

say()  { printf '== %s\n' "$1"; }
note() { printf '   %s\n' "$1"; }
die()  { printf '%s: %s\n' "$PROGRAM" "$1" >&2; exit 1; }
misuse() { printf '%s: %s\n' "$PROGRAM" "$1" >&2; usage >&2; exit 2; }

usage() {
    cat <<EOF
usage: sudo bash scripts/pi/$PROGRAM [options]

  --dir DIR            where the certificate is kept (default $DEFAULT_DIR)
  --name NAME          an IPv4 address or a name under which a tablet opens the
                       console; repeat it for each (default: ${DEFAULT_NAMES[*]}).
                       ${LOCAL_NAMES[*]} are always added.
  --label TEXT         what tells this machine's authority from another's in the
                       list of a tablet (default: the host name)
  --owner USER[:GROUP] the account the console runs as: owner of DIR and of its
                       files (default: whoever runs this script)
  --days N             validity in days, 1 to $MAX_DAYS (default $MAX_DAYS)
  --force              replace what is there. The authority changes: every
                       tablet installs the new one, and removes the old one
  --dry-run            say what would be done; write nothing
  --check              judge what is there; write nothing. Exit 1 when it is
                       missing, unusable, or expires within --min-days
  --min-days N         for --check (default $DEFAULT_MIN_DAYS)
EOF
}

cleanup() {
    if [ -n "$staging" ] && [ -d "$staging" ]; then
        rm -rf "$staging"
    fi
}
trap cleanup EXIT
# Interrupted half way: leave through the cleanup above, like any other exit.
trap 'exit 1' HUP INT TERM

# --- Arguments ----------------------------------------------------------------

is_count() { [[ "$1" =~ ^[0-9]{1,5}$ ]]; }

is_ipv4() {
    local part
    [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
    for part in ${1//./ }; do
        [ "$((10#$part))" -le 255 ] || return 1
    done
}

is_dns_name() {
    [ "${#1}" -le 253 ] || return 1
    [[ "$1" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$ ]] || return 1
    # A last label made of digits is an address somebody mistyped, not a name.
    [[ ! "${1##*.}" =~ ^[0-9]+$ ]]
}

add_name() {
    local name known
    name="$(printf '%s' "$1" | tr 'A-Z' 'a-z')"
    if ! is_ipv4 "$name" && ! is_dns_name "$name"; then
        misuse "'$1' is neither an IPv4 address nor a host name"
    fi
    for known in ${names[@]+"${names[@]}"}; do
        if [ "$known" = "$name" ]; then return 0; fi
    done
    names+=("$name")
}

parse_arguments() {
    local explicit=false name
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --dir | --name | --label | --owner | --days | --min-days)
                [ "$#" -ge 2 ] || misuse "$1 needs a value"
                ;;
        esac
        case "$1" in
            --dir) dir="$2"; shift ;;
            --name) add_name "$2"; explicit=true; shift ;;
            --label) label="$2"; shift ;;
            --owner) owner="$2"; shift ;;
            --days) days="$2"; shift ;;
            --min-days) min_days="$2"; shift ;;
            --force) force=true ;;
            --dry-run) dry_run=true ;;
            --check) check=true ;;
            -h | --help) usage; exit 0 ;;
            *) misuse "unknown argument '$1'" ;;
        esac
        shift
    done

    [ -n "$dir" ] || misuse "--dir needs a directory"
    case "$dir" in
        /*) ;;
        *) misuse "--dir must be an absolute path, got '$dir'" ;;
    esac
    is_count "$days" && [ "$days" -ge 1 ] && [ "$days" -le "$MAX_DAYS" ] \
        || misuse "--days must be 1 to $MAX_DAYS (a tablet refuses a longer certificate), got '$days'"
    is_count "$min_days" || misuse "--min-days must be a number of days, got '$min_days'"
    if [ -n "$owner" ] && [[ ! "$owner" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*(:[A-Za-z0-9_][A-Za-z0-9_.-]*)?$ ]]; then
        misuse "--owner must be USER or USER:GROUP, got '$owner'"
    fi
    if "$check" && { "$force" || "$dry_run"; }; then
        misuse "--check judges and writes nothing: it goes with neither --force nor --dry-run"
    fi

    # --check without a name judges the certificate as it is; with names, it
    # also requires them. Making one always needs names: the defaults, then.
    if ! "$explicit" && ! "$check"; then
        for name in "${DEFAULT_NAMES[@]}"; do add_name "$name"; done
    fi
    if ! "$check"; then
        for name in "${LOCAL_NAMES[@]}"; do add_name "$name"; done
    fi

    if [ -z "$label" ]; then label="$(hostname 2>/dev/null || true)"; fi
    label="$(printf '%s' "$label" | tr -c 'A-Za-z0-9._-' '-' | cut -c1-40)"
    [ -n "$label" ] || label="console"
}

# --- What is already there ------------------------------------------------------

# The names a certificate carries, one per line, as this script writes them.
certificate_names() {
    openssl x509 -in "$1" -noout -text 2>/dev/null \
        | grep -A1 'Subject Alternative Name' | tail -n 1 \
        | tr ',' '\n' | sed -e 's/^ *//' -e 's/^IP Address://' -e 's/^DNS://'
}

fingerprint() {
    openssl x509 -in "$1" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2
}

validity() {
    openssl x509 -in "$1" -noout -dates 2>/dev/null | tr '\n' ' ' | sed 's/ *$//'
}

# Sets `existing` to absent, good or unusable, and `existing_reason`.
existing=""
existing_reason=""
judge_existing() {
    local authority="$dir/$AUTHORITY_FILE" certificate="$dir/$CERTIFICATE_FILE" key="$dir/$KEY_FILE"
    local file missing="" verdict carried name of_key of_certificate
    existing_reason=""

    if [ ! -e "$authority" ] && [ ! -e "$certificate" ] && [ ! -e "$key" ]; then
        existing="absent"
        return 0
    fi
    existing="unusable"
    for file in "$authority" "$certificate" "$key"; do
        if [ ! -r "$file" ]; then missing="$missing ${file##*/}"; fi
    done
    if [ -n "$missing" ]; then
        existing_reason="missing or unreadable:$missing"
        return 0
    fi

    # The chain, and the dates of both certificates, as a tablet judges them.
    if ! verdict="$(openssl verify -CAfile "$authority" "$certificate" 2>&1)"; then
        existing_reason="the certificate is not accepted under its authority ($(printf '%s' "$verdict" | grep -m1 -i 'error' || printf 'openssl verify failed'))"
        return 0
    fi
    of_key="$(openssl pkey -in "$key" -pubout 2>/dev/null || true)"
    of_certificate="$(openssl x509 -in "$certificate" -noout -pubkey 2>/dev/null || true)"
    if [ -z "$of_key" ] || [ "$of_key" != "$of_certificate" ]; then
        existing_reason="the private key is not the one of the certificate"
        return 0
    fi
    carried="$(certificate_names "$certificate" || true)"
    for name in ${names[@]+"${names[@]}"}; do
        if ! printf '%s\n' "$carried" | grep -qxF "$name"; then
            existing_reason="the certificate does not carry the name '$name'"
            return 0
        fi
    done
    existing="good"
}

describe_existing() {
    note "names: $(certificate_names "$dir/$CERTIFICATE_FILE" | tr '\n' ' ' | sed 's/ *$//')"
    note "validity: $(validity "$dir/$CERTIFICATE_FILE")"
    note "authority, SHA-256: $(fingerprint "$dir/$AUTHORITY_FILE")"
}

# --- --check --------------------------------------------------------------------

run_check() {
    judge_existing
    case "$existing" in
        absent) die "no certificate in $dir: run this script without --check to make one" ;;
        unusable) die "$dir is not usable: $existing_reason" ;;
    esac
    say "Console certificate in $dir"
    describe_existing
    if ! openssl x509 -in "$dir/$CERTIFICATE_FILE" -noout -checkend "$((min_days * 86400))" > /dev/null 2>&1; then
        die "the certificate expires within $min_days days: plan the renewal (--force, then the new authority on every tablet)"
    fi
    note "good for more than $min_days days"
}

# --- Making it ------------------------------------------------------------------

subject_alternative_names() {
    local name list=""
    for name in "${names[@]}"; do
        if is_ipv4 "$name"; then list="$list, IP:$name"; else list="$list, DNS:$name"; fi
    done
    printf '%s' "${list#, }"
}

# One configuration file per certificate, each with its own subject: written
# there rather than passed with -subj, which not every openssl honours next to
# a configuration file. The label and the names were checked character by
# character before they get here.
write_authority_configuration() {
    cat > "$1" <<EOF
[req]
distinguished_name = subject
x509_extensions = extensions
prompt = no

[subject]
O = Anheart
CN = Anheart console authority $label $2

[extensions]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
EOF
}

write_console_configuration() {
    cat > "$1" <<EOF
[req]
distinguished_name = subject
prompt = no

[subject]
O = Anheart
CN = ${names[0]}

[extensions]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
subjectAltName = $(subject_alternative_names)
EOF
}

new_key() {
    openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -pkeyopt ec_param_enc:named_curve "$@"
}

new_serial() { printf '0x%s' "$(openssl rand -hex 16)"; }

# Everything is made in a directory of its own inside DIR, checked, and only
# then moved into place: a failure leaves DIR as it was.
make_certificate() {
    local of_authority of_console authority_key log

    mkdir -p "$dir" 2> /dev/null || die "cannot create $dir: run this script as root"
    staging="$(mktemp -d "$dir/.incoming.XXXXXX" 2> /dev/null)" \
        || die "cannot write in $dir: run this script as root"
    # Before anything is made: an owner that cannot be given is said now.
    if [ -n "$owner" ]; then
        chown "$owner" "$staging" 2>/dev/null \
            || die "cannot give $dir to '$owner': run this script as root, with an account that exists"
    fi
    of_authority="$staging/authority.cnf"
    of_console="$staging/console.cnf"
    log="$staging/openssl.log"
    write_authority_configuration "$of_authority" "$(date -u +%Y-%m-%d)"
    write_console_configuration "$of_console"

    # The authority's private key: in this variable and nowhere else. `printf`
    # is the shell's own, so the key is on no command line, and openssl reads
    # it from a pipe.
    authority_key="$(new_key 2> "$log")" || die "openssl could not make a key: $(tail -n 1 "$log")"
    printf '%s\n' "$authority_key" | openssl req -x509 -new -key /dev/stdin -sha256 \
        -days "$((days + 1))" -set_serial "$(new_serial)" -config "$of_authority" \
        -out "$staging/$AUTHORITY_FILE" 2> "$log" \
        || die "openssl could not make the authority: $(tail -n 1 "$log")"

    new_key -out "$staging/$KEY_FILE" 2> "$log" \
        || die "openssl could not make the console's key: $(tail -n 1 "$log")"
    openssl req -new -key "$staging/$KEY_FILE" -config "$of_console" \
        -out "$staging/console.csr" 2> "$log" \
        || die "openssl could not make the certificate request: $(tail -n 1 "$log")"
    printf '%s\n' "$authority_key" | openssl x509 -req -in "$staging/console.csr" \
        -CA "$staging/$AUTHORITY_FILE" -CAkey /dev/stdin -set_serial "$(new_serial)" \
        -days "$days" -sha256 -extfile "$of_console" -extensions extensions \
        -out "$staging/$CERTIFICATE_FILE" 2> "$log" \
        || die "openssl could not sign the console's certificate: $(tail -n 1 "$log")"
    authority_key=""
    unset authority_key

    openssl verify -CAfile "$staging/$AUTHORITY_FILE" "$staging/$CERTIFICATE_FILE" > /dev/null 2> "$log" \
        || die "the certificate just made is not accepted under its authority: $(tail -n 1 "$log")"

    chmod 600 "$staging/$KEY_FILE"
    chmod 644 "$staging/$AUTHORITY_FILE" "$staging/$CERTIFICATE_FILE"
    chmod 750 "$dir"
    if [ -n "$owner" ]; then
        chown "$owner" "$dir" "$staging/$KEY_FILE" "$staging/$AUTHORITY_FILE" "$staging/$CERTIFICATE_FILE"
    fi
    mv -f "$staging/$KEY_FILE" "$dir/$KEY_FILE"
    mv -f "$staging/$CERTIFICATE_FILE" "$dir/$CERTIFICATE_FILE"
    mv -f "$staging/$AUTHORITY_FILE" "$dir/$AUTHORITY_FILE"
}

network_names() {
    local name list=""
    for name in "${names[@]}"; do
        case " ${LOCAL_NAMES[*]} " in
            *" $name "*) ;;
            *) list="$list,$name" ;;
        esac
    done
    printf '%s' "${list#,}"
}

say_what_is_next() {
    say "What is left to do"
    note "1. In the console's configuration (anheart.env), with the paths as the console sees them:"
    note "     UI_TLS_CERT=$dir/$CERTIFICATE_FILE"
    note "     UI_TLS_KEY=$dir/$KEY_FILE"
    note "     UI_TLS_NAMES=$(network_names)"
    note "2. Restart the console, at rest: it reads its certificate when it starts."
    note "3. Install $dir/$AUTHORITY_FILE on each tablet, and compare the SHA-256"
    note "   above with the one the tablet shows before trusting it (docs/pi-image.md)."
}

run_make() {
    local now previous=""

    judge_existing
    if [ "$existing" = "good" ] && ! "$force"; then
        say "Console certificate in $dir: already there and good, nothing to do"
        describe_existing
        return 0
    fi
    if [ "$existing" = "unusable" ] && ! "$force"; then
        die "$dir holds a certificate that is not usable as asked ($existing_reason). Nothing is replaced without --force: a new certificate comes with a new authority, which every tablet must install."
    fi

    now="$(date -u +%s)"
    [ "$now" -ge "$CLOCK_FLOOR" ] \
        || die "the clock of this machine reads $(date -u +%Y-%m-%d): it was never set. A certificate dated from it would be refused by the tablets. Set the clock, then run this again."

    if "$dry_run"; then
        say "Dry run: nothing is written"
        if [ "$existing" = "absent" ]; then
            note "would make an authority and a certificate in $dir"
        else
            note "would REPLACE the authority and the certificate in $dir (--force)"
        fi
        note "names: ${names[*]}"
        note "validity: $days days"
        note "owner: ${owner:-unchanged ($(id -un))}"
        return 0
    fi

    if [ "$existing" != "absent" ] && [ -r "$dir/$AUTHORITY_FILE" ]; then
        previous="$(fingerprint "$dir/$AUTHORITY_FILE")"
    fi
    make_certificate
    say "Console certificate written in $dir"
    describe_existing
    if [ -n "$previous" ]; then
        note "REPLACED the authority of SHA-256 $previous:"
        note "remove that one from every tablet, it recognises nothing any more."
    fi
    say_what_is_next
}

main() {
    command -v openssl > /dev/null 2>&1 \
        || die "openssl is not installed (Debian package openssl)"
    parse_arguments "$@"
    if "$check"; then
        run_check
    else
        run_make
    fi
}

main "$@"

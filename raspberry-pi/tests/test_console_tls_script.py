"""``scripts/pi/make_console_tls.sh``: the console's certificate, made on the Pi (ANH-151 EX-3).

The script is run for real, under ``bash``, with the machine's ``openssl``,
in a directory of the test. What is judged:

* **what it makes**: an authority and a certificate that a standard verifier
  accepts under that authority and refuses under another; the names, the
  algorithms, the lifetime and the modes that the tablets and the console need;
* **what it never leaves**: the authority's private key is in no file and on
  no command line (every ``openssl`` call is recorded by a stand-in first on
  ``PATH``), and nothing the script prints is a key;
* **what it never does twice**: run again it changes nothing, and what is
  there is not replaced without ``--force``, whatever is wrong with it;
* **what it refuses**, each with its message: a wrong argument, a clock that
  was never set, an owner it cannot give the files to, a machine without
  ``openssl``; and a failure half way leaves the directory as it was.

Not judged here, because it needs root: giving the files to ANOTHER account
(``--owner`` is exercised with the account that runs the tests).
"""

from __future__ import annotations

import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from tests.test_web_api import TOKEN
from tests.tls_support import (
    AP_ADDRESS,
    AP_NAME,
    AUTHORITY,
    BASH,
    CERTIFICATE,
    KEY,
    OPENSSL,
    SCRIPT,
    Material,
    issue_dated,
    make_material,
    new_key,
    openssl,
    openssl_status,
    requires_openssl,
    run_script,
    tool,
)

pytestmark = requires_openssl

DAY: Final[int] = 86_400
LIFETIME_DAYS: Final[int] = 825
"""What iOS and macOS accept at most for a server certificate, whoever signed it."""

KEY_MARK: Final[str] = "PRIVATE KEY"
"""What every PEM private key says of itself, encrypted or not."""

END_OF_CALL: Final[str] = "#end-of-call"

RECORDING_OPENSSL: Final[str] = f"""#!/bin/sh
# Stand-in for openssl: writes down each argument it is given, one per line,
# then becomes the real one. What it is handed on its standard input is not
# touched: that is where a key may travel.
for argument in "$@"; do printf '%s\\n' "$argument" >> "$ANHEART_TEST_CALLS"; done
printf '%s\\n' '{END_OF_CALL}' >> "$ANHEART_TEST_CALLS"
exec "$ANHEART_TEST_OPENSSL" "$@"
"""

FAILING_TO_SIGN: Final[str] = """#!/bin/sh
# Stand-in for openssl: everything works, except signing the console's certificate.
if [ "$1" = "x509" ] && [ "$2" = "-req" ]; then
    echo "injected: the signature failed" >&2
    exit 1
fi
exec "$ANHEART_TEST_OPENSSL" "$@"
"""

CLOCK_NEVER_SET: Final[str] = """#!/bin/sh
# Stand-in for date: a Pi that booted without a network and never had the time.
case "$*" in
    *%s*) echo 1700000000 ;;
    *) echo 2023-11-14 ;;
esac
"""


def stand_in(directory: Path, name: str, body: str) -> dict[str, str]:
    """Put a scripted ``name`` first on ``PATH``; the environment to run the script with."""
    bin_directory = directory / "bin"
    bin_directory.mkdir(exist_ok=True)
    command = bin_directory / name
    command.write_text(body, encoding="utf-8")
    command.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
        "ANHEART_TEST_OPENSSL": tool(OPENSSL),
        "ANHEART_TEST_CALLS": str(directory / "calls"),
    }


def text_of(certificate: Path) -> str:
    return openssl("x509", "-in", str(certificate), "-noout", "-text")


def fingerprint(certificate: Path) -> str:
    printed = openssl("x509", "-in", str(certificate), "-noout", "-fingerprint", "-sha256")
    return printed.strip().split("=", 1)[1]


def expires_within(certificate: Path, seconds: int) -> bool:
    status = openssl_status("x509", "-in", str(certificate), "-noout", "-checkend", str(seconds))
    return status != 0


def accepted_under(authority: Path, certificate: Path) -> bool:
    return openssl_status("verify", "-CAfile", str(authority), str(certificate)) == 0


def lasts(certificate: Path, days: int) -> tuple[bool, bool]:
    """Whether the certificate still holds an hour short of ``days`` days, and an hour past."""
    return (
        not expires_within(certificate, (days * DAY) - 3600),
        not expires_within(certificate, (days * DAY) + 3600),
    )


EXACTLY: Final[tuple[bool, bool]] = (True, False)
"""What :func:`lasts` answers for a certificate that lasts that many days, to the hour."""


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def snapshot_of(directory: Path) -> dict[str, tuple[bytes, int]]:
    """Every file of the directory with what it holds and when it was written."""
    return {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(directory.iterdir())
    }


# =========================================================================
# What it makes
# =========================================================================


def test_ex3_the_script_makes_an_authority_a_certificate_and_a_key(tmp_path: Path) -> None:
    directory = tmp_path / "tls"
    done = run_script("--dir", str(directory), "--name", AP_ADDRESS, "--name", AP_NAME)
    assert done.returncode == 0, done.stderr
    material = Material(directory)
    assert sorted(path.name for path in directory.iterdir()) == [AUTHORITY, CERTIFICATE, KEY], (
        "three files, and nothing left of the making"
    )
    assert mode_of(material.key) == 0o600
    assert mode_of(material.certificate) == 0o644
    assert mode_of(material.authority) == 0o644
    assert mode_of(directory) == 0o750

    console = text_of(material.certificate)
    assert f"IP Address:{AP_ADDRESS}, DNS:{AP_NAME}, IP Address:127.0.0.1, DNS:localhost" in console
    assert "CA:FALSE" in console
    assert "TLS Web Server Authentication" in console
    assert "ASN1 OID: prime256v1" in console
    assert "Signature Algorithm: ecdsa-with-SHA256" in console
    authority = text_of(material.authority)
    assert "CA:TRUE, pathlen:0" in authority
    assert "Certificate Sign" in authority
    assert "ASN1 OID: prime256v1" in authority

    # 825 days, the longest a tablet accepts; the authority outlives it by a day.
    of_console = lasts(material.certificate, LIFETIME_DAYS)
    assert of_console == EXACTLY
    of_authority = lasts(material.authority, LIFETIME_DAYS + 1)
    assert of_authority == EXACTLY

    # What the person who commissions the machine reads.
    said = done.stdout
    printed = fingerprint(material.authority)
    assert f"== Console certificate written in {directory}" in said
    assert f"authority, SHA-256: {printed}" in said
    assert f"UI_TLS_CERT={material.certificate}" in said
    assert f"UI_TLS_KEY={material.key}" in said
    assert f"UI_TLS_NAMES={AP_ADDRESS},{AP_NAME}" in said
    assert f"Install {material.authority} on each tablet" in said


def test_ex3_a_standard_verifier_accepts_the_certificate_under_its_authority_and_no_other(
    tmp_path: Path,
) -> None:
    this_pi = make_material(tmp_path / "this-pi", AP_ADDRESS, AP_NAME)
    another_pi = make_material(tmp_path / "another-pi", AP_ADDRESS, AP_NAME)
    verdicts = {
        "this certificate, this authority": accepted_under(this_pi.authority, this_pi.certificate),
        "that certificate, that authority": accepted_under(
            another_pi.authority, another_pi.certificate
        ),
        "this certificate, that authority": accepted_under(
            another_pi.authority, this_pi.certificate
        ),
        "that certificate, this authority": accepted_under(
            this_pi.authority, another_pi.certificate
        ),
    }
    assert verdicts == {
        "this certificate, this authority": True,
        "that certificate, that authority": True,
        "this certificate, that authority": False,
        "that certificate, this authority": False,
    }
    prints = {fingerprint(this_pi.authority), fingerprint(another_pi.authority)}
    assert len(prints) == 2, "two machines made the same way share nothing"


def test_ex3_without_a_name_the_certificate_carries_the_documented_defaults(
    tmp_path: Path,
) -> None:
    material = make_material(tmp_path / "tls")
    assert (
        "IP Address:10.42.0.1, DNS:anheart.internal, IP Address:127.0.0.1, DNS:localhost"
        in text_of(material.certificate)
    )


def test_ex3_the_authority_is_named_after_the_machine_and_the_day(tmp_path: Path) -> None:
    """Two machines on one tablet, or an authority and the one that replaced it, are told apart."""
    directory = tmp_path / "tls"
    done = run_script("--dir", str(directory), "--label", "Salle 2 / Lyon")
    assert done.returncode == 0, done.stderr
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    subject = openssl("x509", "-in", str(directory / AUTHORITY), "-noout", "-subject")
    assert "Anheart console authority Salle-2---Lyon " in subject
    assert today in subject or yesterday in subject, "the day it was made (a run across midnight)"


def test_ex3_the_owner_asked_for_is_given_the_directory_and_its_files(tmp_path: Path) -> None:
    """With the account that runs the tests: giving them to another one needs root."""
    directory = tmp_path / "tls"
    me = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()  # noqa: S607
    done = run_script("--dir", str(directory), "--owner", me)
    assert done.returncode == 0, done.stderr
    for path in (directory, directory / KEY, directory / CERTIFICATE, directory / AUTHORITY):
        assert path.stat().st_uid == os.geteuid()
    assert mode_of(directory / KEY) == 0o600


# =========================================================================
# What it never leaves behind
# =========================================================================


def calls_of(environment: dict[str, str]) -> list[list[str]]:
    """Every ``openssl`` call the script made, as its list of arguments."""
    recorded = Path(environment["ANHEART_TEST_CALLS"]).read_text(encoding="utf-8")
    return [call.splitlines() for call in recorded.split(f"{END_OF_CALL}\n") if call]


def test_ex3_the_authoritys_private_key_is_in_no_file_and_on_no_command_line(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "tls"
    environment = stand_in(tmp_path, "openssl", RECORDING_OPENSSL)
    done = run_script("--dir", str(directory), "--name", AP_ADDRESS, env=environment)
    assert done.returncode == 0, done.stderr
    calls = calls_of(environment)

    # Two keys are made: one printed to the script (the authority's), one
    # written straight to its file (the console's).
    made = [call for call in calls if call[0] == "genpkey"]
    assert len(made) == 2
    assert sum("-out" in call for call in made) == 1
    written = next(call for call in made if "-out" in call)
    assert written[written.index("-out") + 1].endswith(f"/{KEY}")

    # Wherever a key is named, it is the console's file or the standard input.
    for call in calls:
        for option in ("-key", "-CAkey", "-signkey", "-keyfile"):
            if option not in call:
                continue
            where = call[call.index(option) + 1]
            assert where == "/dev/stdin" or where.endswith(f"/{KEY}"), call
        for argument in call:
            assert KEY_MARK not in argument, "a key was passed on a command line"
            assert not argument.startswith("pass:"), "a passphrase was passed on a command line"
    signing = next(call for call in calls if call[:2] == ["x509", "-req"])
    assert signing[signing.index("-CAkey") + 1] == "/dev/stdin"
    self_signing = next(call for call in calls if call[:2] == ["req", "-x509"])
    assert self_signing[self_signing.index("-key") + 1] == "/dev/stdin"

    # On the disk: one private key, the console's, and nothing left of the making.
    holders = [
        path.name
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file() and KEY_MARK in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert holders == [KEY]
    assert KEY_MARK not in done.stdout + done.stderr
    assert sorted(path.name for path in directory.iterdir()) == [AUTHORITY, CERTIFICATE, KEY]


# =========================================================================
# What it never does twice
# =========================================================================


def test_ex3_run_again_it_changes_nothing(tmp_path: Path) -> None:
    directory = tmp_path / "tls"
    make_material(directory, AP_ADDRESS, AP_NAME)
    before = snapshot_of(directory)
    again = run_script("--dir", str(directory), "--name", AP_ADDRESS, "--name", AP_NAME)
    assert again.returncode == 0, again.stderr
    assert "already there and good, nothing to do" in again.stdout
    assert "What is left to do" not in again.stdout
    after = snapshot_of(directory)
    assert after == before


def refused(*arguments: str, env: dict[str, str] | None = None) -> str:
    """Run the script expecting a refusal (exit 1); what it said on its error output."""
    done = run_script(*arguments, env=env)
    assert done.returncode == 1, f"exit {done.returncode}:\n{done.stdout}\n{done.stderr}"
    return done.stderr


def test_ex3_a_certificate_for_other_names_is_not_replaced_without_force(tmp_path: Path) -> None:
    directory = tmp_path / "tls"
    make_material(directory, AP_ADDRESS)
    before = snapshot_of(directory)
    said = refused("--dir", str(directory), "--name", "10.42.0.9")
    assert "the certificate does not carry the name '10.42.0.9'" in said
    assert "Nothing is replaced without --force" in said
    assert "every tablet must install" in said
    after = snapshot_of(directory)
    assert after == before


def test_ex3_a_key_that_is_not_the_certificates_is_seen_and_not_replaced(tmp_path: Path) -> None:
    directory = tmp_path / "tls"
    make_material(directory, AP_ADDRESS)
    new_key(directory / KEY)
    before = snapshot_of(directory)
    said = refused("--dir", str(directory), "--name", AP_ADDRESS)
    assert "the private key is not the one of the certificate" in said
    after = snapshot_of(directory)
    assert after == before


def test_ex3_a_file_that_went_missing_is_seen_and_nothing_is_made_around_it(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "tls"
    make_material(directory, AP_ADDRESS)
    (directory / KEY).unlink()
    before = snapshot_of(directory)
    said = refused("--dir", str(directory), "--name", AP_ADDRESS)
    assert f"missing or unreadable: {KEY}" in said
    after = snapshot_of(directory)
    assert after == before


def test_ex3_an_expired_certificate_is_seen_and_not_replaced_without_force(
    tmp_path: Path,
) -> None:
    """Replacing it is a decision: every tablet then has an authority to install."""
    directory = tmp_path / "tls"
    now = datetime.now(UTC)
    issue_dated(directory, not_before=now - timedelta(days=30), not_after=now - timedelta(days=1))
    before = snapshot_of(directory)
    said = refused("--dir", str(directory), "--name", AP_ADDRESS)
    assert "the certificate is not accepted under its authority" in said
    assert "expired" in said
    after = snapshot_of(directory)
    assert after == before
    checked = refused("--dir", str(directory), "--check")
    assert "is not usable: the certificate is not accepted under its authority" in checked


def test_ex3_force_replaces_the_authority_and_says_which_one_to_remove(tmp_path: Path) -> None:
    directory = tmp_path / "tls"
    old = make_material(directory, AP_ADDRESS)
    kept = tmp_path / "old-authority.crt"
    kept.write_bytes(old.authority.read_bytes())
    old_print = fingerprint(old.authority)
    done = run_script("--dir", str(directory), "--name", "10.42.0.9", "--force", "--days", "30")
    assert done.returncode == 0, done.stderr
    new = Material(directory)
    new_print = fingerprint(new.authority)
    assert new_print != old_print
    carried = text_of(new.certificate)
    assert "IP Address:10.42.0.9" in carried
    under_new = accepted_under(new.authority, new.certificate)
    under_old = accepted_under(kept, new.certificate)
    assert under_new
    assert not under_old, (
        "a tablet that only has the old authority no longer recognises the console"
    )
    assert f"REPLACED the authority of SHA-256 {old_print}" in done.stdout
    assert "remove that one from every tablet" in done.stdout
    life = lasts(new.certificate, 30)
    assert life == EXACTLY
    assert sorted(path.name for path in directory.iterdir()) == [AUTHORITY, CERTIFICATE, KEY]


def test_ex3_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    directory = tmp_path / "tls"
    done = run_script("--dir", str(directory), "--name", AP_ADDRESS, "--dry-run")
    assert done.returncode == 0, done.stderr
    assert "== Dry run: nothing is written" in done.stdout
    assert f"would make an authority and a certificate in {directory}" in done.stdout
    assert f"names: {AP_ADDRESS} 127.0.0.1 localhost" in done.stdout
    assert "validity: 825 days" in done.stdout
    assert not directory.exists()

    make_material(directory, AP_ADDRESS)
    before = snapshot_of(directory)
    forced = run_script("--dir", str(directory), "--name", "10.42.0.9", "--force", "--dry-run")
    assert forced.returncode == 0, forced.stderr
    assert f"would REPLACE the authority and the certificate in {directory}" in forced.stdout
    after = snapshot_of(directory)
    assert after == before
    # Without --force, a dry run is refused as the real run would be.
    unforced = refused("--dir", str(directory), "--name", "10.42.0.9", "--dry-run")
    assert "Nothing is replaced without --force" in unforced
    after = snapshot_of(directory)
    assert after == before


def test_ex3_check_judges_what_is_there_and_writes_nothing(tmp_path: Path) -> None:
    directory = tmp_path / "tls"
    absent = refused("--dir", str(directory), "--check")
    assert f"no certificate in {directory}" in absent
    assert not directory.exists()

    make_material(directory, AP_ADDRESS, AP_NAME)
    before = snapshot_of(directory)
    good = run_script("--dir", str(directory), "--check")
    assert good.returncode == 0, good.stderr
    assert "good for more than 30 days" in good.stdout
    assert f"names: {AP_ADDRESS} {AP_NAME} 127.0.0.1 localhost" in good.stdout
    named = run_script("--dir", str(directory), "--check", "--name", AP_NAME)
    assert named.returncode == 0, named.stderr

    late = refused("--dir", str(directory), "--check", "--min-days", "900")
    assert "the certificate expires within 900 days: plan the renewal" in late
    other = refused("--dir", str(directory), "--check", "--name", "other.internal")
    assert "is not usable: the certificate does not carry the name 'other.internal'" in other
    after = snapshot_of(directory)
    assert after == before


# =========================================================================
# What it refuses
# =========================================================================


@pytest.mark.parametrize(
    ("arguments", "said"),
    [
        (("--nope",), "unknown argument '--nope'"),
        (("--dir",), "--dir needs a value"),
        (("--dir", "relative/tls"), "--dir must be an absolute path, got 'relative/tls'"),
        (("--name", "bad name"), "'bad name' is neither an IPv4 address nor a host name"),
        (("--name", "fd00::1"), "'fd00::1' is neither an IPv4 address nor a host name"),
        (("--name", "10.42.0.256"), "'10.42.0.256' is neither an IPv4 address nor a host name"),
        (("--name", "*.internal"), "'*.internal' is neither an IPv4 address nor a host name"),
        (("--days", "826"), "--days must be 1 to 825"),
        (("--days", "0"), "--days must be 1 to 825"),
        (("--min-days", "soon"), "--min-days must be a number of days, got 'soon'"),
        (("--owner", "some body"), "--owner must be USER or USER:GROUP, got 'some body'"),
        (("--check", "--force"), "--check judges and writes nothing: it goes with neither"),
        (("--check", "--dry-run"), "--check judges and writes nothing: it goes with neither"),
    ],
)
def test_ex3_a_wrong_argument_is_said_and_nothing_is_made(
    tmp_path: Path, arguments: tuple[str, ...], said: str
) -> None:
    directory = tmp_path / "tls"
    prefix = () if arguments[0] == "--dir" else ("--dir", str(directory))
    done = run_script(*prefix, *arguments)
    assert done.returncode == 2, done.stdout
    assert f"make_console_tls.sh: {said}" in done.stderr
    assert "usage: sudo bash scripts/pi/make_console_tls.sh" in done.stderr
    assert not directory.exists()


def test_ex3_help_is_given_and_nothing_is_made(tmp_path: Path) -> None:
    done = run_script("--dir", str(tmp_path / "tls"), "--help")
    assert done.returncode == 0
    assert "--force" in done.stdout
    assert not (tmp_path / "tls").exists()


def test_ex3_a_clock_that_was_never_set_is_refused(tmp_path: Path) -> None:
    """A certificate dated from it would be refused by every tablet."""
    directory = tmp_path / "tls"
    said = refused("--dir", str(directory), env=stand_in(tmp_path, "date", CLOCK_NEVER_SET))
    assert "the clock of this machine reads 2023-11-14: it was never set" in said
    assert "Set the clock, then run this again" in said
    assert not directory.exists()


def test_ex3_an_owner_that_cannot_be_given_the_files_is_refused_before_anything_is_made(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "tls"
    said = refused("--dir", str(directory), "--owner", "nobody-of-this-machine")
    assert f"cannot give {directory} to 'nobody-of-this-machine'" in said
    assert list(directory.iterdir()) == []


def test_ex3_a_machine_without_openssl_is_told_so(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    said = refused("--dir", str(tmp_path / "tls"), env={"PATH": str(empty)})
    assert "make_console_tls.sh: openssl is not installed" in said


def test_ex3_a_directory_it_cannot_write_in_is_refused(tmp_path: Path) -> None:
    blocked = tmp_path / "a-file"
    blocked.write_text("", encoding="utf-8")
    said = refused("--dir", str(blocked / "tls"))
    assert f"cannot create {blocked}/tls: run this script as root" in said


def test_ex3_a_failure_half_way_leaves_the_directory_as_it_was(tmp_path: Path) -> None:
    directory = tmp_path / "tls"
    make_material(directory, AP_ADDRESS)
    before = snapshot_of(directory)
    environment = stand_in(tmp_path, "openssl", FAILING_TO_SIGN)
    said = refused("--dir", str(directory), "--name", "10.42.0.9", "--force", env=environment)
    assert (
        "openssl could not sign the console's certificate: injected: the signature failed" in said
    )
    after = snapshot_of(directory)
    assert after == before, "the old certificate still serves, nothing is half made"


def test_ex3_the_script_is_what_the_repository_ships() -> None:
    """Executable, bash, strict, and silent about itself where it must be."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in source
    assert "umask 077" in source
    assert os.access(SCRIPT, os.X_OK)
    assert "set -x" not in source, "tracing would print the authority's key"
    assert "pass:" not in source


# =========================================================================
# The preflight says what the console will say
# =========================================================================

PREFLIGHT: Final[Path] = SCRIPT.with_name("preflight.sh")

SCRIPTED_DOCKER: Final[str] = """#!/bin/sh
echo "0.0.0-scripted"
"""

OFF_LOOPBACK: Final[str] = f"""MOTOR_BACKEND=sim
ECG_SOURCE=sim
ARM_RADIUS_M=1.5
UI_HOST={AP_ADDRESS}
UI_TOKEN={TOKEN}
"""


def preflight(tmp_path: Path, configuration: str) -> subprocess.CompletedProcess[str]:
    """Run the real preflight on a configuration of the test, with a scripted docker."""
    env_file = tmp_path / "anheart.env"
    env_file.write_text(configuration, encoding="utf-8")
    environment = stand_in(tmp_path, "docker", SCRIPTED_DOCKER)
    return subprocess.run(  # noqa: S603  # the repository's own script, fixed arguments
        [tool(BASH), str(PREFLIGHT)],
        env={**environment, "ANHEART_ENV_FILE": str(env_file)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_ex3_the_preflight_refuses_a_network_address_without_a_certificate(
    tmp_path: Path,
) -> None:
    """What the console would refuse at its start is said before it is started."""
    done = preflight(tmp_path, OFF_LOOPBACK)
    assert done.returncode == 1
    assert (
        f"FAIL  UI_HOST={AP_ADDRESS} needs UI_TLS_CERT and UI_TLS_KEY "
        "(sudo bash scripts/pi/make_console_tls.sh makes the certificate)"
    ) in done.stdout
    assert "PREFLIGHT FAILED: 1 blocking" in done.stdout
    half = preflight(tmp_path, OFF_LOOPBACK + "UI_TLS_CERT=/etc/anheart/tls/console.crt\n")
    assert "needs UI_TLS_CERT and UI_TLS_KEY" in half.stdout


def test_ex3_the_preflight_passes_a_network_address_with_its_certificate(tmp_path: Path) -> None:
    done = preflight(
        tmp_path,
        OFF_LOOPBACK
        + "UI_TLS_CERT=/etc/anheart/tls/console.crt\nUI_TLS_KEY=/etc/anheart/tls/console.key\n",
    )
    assert done.returncode == 0, done.stdout
    assert "UI_TLS" not in done.stdout
    assert "PREFLIGHT OK" in done.stdout
    # On loopback nothing is asked: clear HTTP stays possible there.
    loopback = preflight(tmp_path, "MOTOR_BACKEND=sim\nECG_SOURCE=sim\nARM_RADIUS_M=1.5\n")
    assert loopback.returncode == 0, loopback.stdout


def test_anh139_the_preflight_refuses_a_medical_value_left_in_the_env(tmp_path: Path) -> None:
    """A Pi installed before ANH-139 has MIN_RIDER_AGE=18 in its .env: said before the start."""
    base = "MOTOR_BACKEND=sim\nECG_SOURCE=sim\nARM_RADIUS_M=1.5\n"
    done = preflight(tmp_path, base + "MIN_RIDER_AGE=18\nHR_HARD_MAX_BPM=148\nHR_CRITICAL_BPM=\n")
    assert done.returncode == 1
    for key in ("MIN_RIDER_AGE", "HR_HARD_MAX_BPM"):
        assert (
            f"FAIL  {key} is no longer read: the medical values are in "
            "config/medical_parameters.json, signed; remove this line"
        ) in done.stdout
    # Blank, it sets nothing, as the console reads it.
    assert "HR_CRITICAL_BPM" not in done.stdout
    assert "PREFLIGHT FAILED: 2 blocking" in done.stdout

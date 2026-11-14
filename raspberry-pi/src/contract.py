"""The versioned contract between this machine and the dashboard (Convex).

One number, ``major.minor``, names the shape of everything the console sends
and everything it accepts back. The two sides must share the **major**:

* every request carries :data:`CONTRACT_HEADER`, and a server that does not
  serve this major answers 426 with :data:`CONTRACT_UNSUPPORTED` instead of
  reading a body it would misunderstand;
* every poll answer names the server's own version, and
  :func:`server_refusal` is what keeps a launch written for another major from
  ever being armed here. An answer that names no version is not an answer of
  this contract, and is refused the same way.

A different **minor** is fine in both directions: within one major each side
relies only on what every minor of it provides.

Since 1.4 the dashboard may answer a machine route "wait": 429
:data:`RATE_LIMITED`, with how long in ``retryAfterMs``
(:mod:`src.cloud_sync` keeps to it), or 413 :data:`PAYLOAD_TOO_LARGE` for a
body over the route's size. A console of an earlier minor reads the first as
a refusal about the link, as it always read a 429: nothing is dropped, and the
same request is made again later.

Since 1.5 the dashboard checks a launch when the console confirms its start,
and may refuse it: 409 with one of :data:`ARMING_REFUSALS` (rights withdrawn,
the launch expired, the rider's age or maximum heart rate changed, or the rider
is in another session). The console asks for this confirmation before arming.
It sends the wall-clock time of the request as ``startedAt`` and
``sessionAgeMs: 0``; the dashboard dates the start at confirmation while later
measurements use that start plus monotonic age, shifted by Convex onto its time
axis with one common offset. A 2xx is carried into the record cursor, so the
armed session is not confirmed a second time
(:mod:`src.record_uplink`). An older armed session without that pre-confirmation
still uses the ordinary confirmation and refusal stop path.

Since 1.6 the dashboard also checks the rider's latest validated fitness and
active consent at arming: ``rider_not_fit``, ``fitness_expired`` and
``consent_missing`` are additional 409 refusals. The roster carries the fitness
status, its expiry and questionnaire version, and consent, never answers or
review notes.

Since 1.7 the events route takes the rider's feedback after a session, entered
by the operator: one event of kind ``feedback`` alone in its request, which the
dashboard stores apart and never among the session's events, refused with
``feedback_not_eligible``, ``feedback_session_not_ended`` or
``feedback_window_closed`` when the session takes none. A console sends it
only to a dashboard that announces 1.7 or later: an earlier one would keep it
as an ordinary event.

Since 1.8 a confirmation sent again for a start the dashboard already confirmed
is answered 2xx, and changes nothing, while the machine has sent nothing of the
session (no point, no event, no heartbeat naming it, no end) and no stop was
asked for, within 90 s of the start the dashboard dated at the first
confirmation: a console whose 2xx was lost may ask again and arm. The console
keeps for it the offer left unanswered that the poll no longer offers, for that
session only and for 60 s at most after the click of the first confirmation
left unanswered; the offer is no longer confirmable as soon as the dashboard
refuses, and goes at the next poll that does not offer it
(:mod:`src.cloud_sync`). Every other start that is no longer pending is refused
as before.

Since 1.9 the dashboard may launch a ballasted bench session (ANH-250,
``bench_launch`` of the shared file): the poll answer's launch carries
``mode: "bench"``, ``occupancy: "ballast"``, the declared ``ballastKg`` and the
launcher's ``notes``, and no programme, rider or heart rate. An automatic
launch carries ``mode: "auto"``, and one with no ``mode`` (a 1.8 dashboard) is
read as automatic. The dashboard offers a bench launch only to a console that
announces 1.9 or later, and a console of 1.8 would drop it as malformed: it
never arms one. This console arms it as a manual bench session only after the
operator's physical confirmation and the bench attestation
(:mod:`src.cloud_sync`); its start may be refused ``machine_busy`` (another
open session holds the machine).

Since 1.10 the poll answer carries ``incidents_open_high`` (ANH-145,
``incidents`` of the shared file): how many high-severity incidents of the
machine are open or analysed, which the pre-flight reads
(:mod:`src.training.preflight`). While it is above 0 the dashboard refuses to
launch an automatic session there, and refuses its start 409
``incident_open``; a bench launch is never refused for it.

Since 1.11 the published programme catalogue comes down signed
(``GET /api/machine/programmes``, ``programme_catalog`` of the shared file),
asked for at rest only and judged again by this console's rules
(:mod:`src.programme_catalog`), and ``POST /api/machine/profiles`` says what
was accepted and refused. A dashboard before 1.11 answers 404 to the route:
nothing is received, the programmes stay.

Since 1.12 an automatic launch is tied to the programme list it was made from
(``launch_programme_list`` of the shared file): the console sends the digest
of its list with its programmes and with the start it confirms, the poll gives
the launch's, and a launch made from another list, or from none, is refused
``programme_changed``; this console never arms one, whatever the dashboard
answers (:mod:`src.cloud_sync`).

Since 1.13 the machine's operators and tablets come down signed on the same
channel (``GET /api/machine/operators``, ``operator_list`` of the shared
file), at rest only (:mod:`src.operator_sync`); an event may name a tablet
(``device:<id>``) and ``training/local`` carries ``operatorId``. A dashboard
before 1.13 answers 404: the console keeps its list, or opens no action
without one (:mod:`src.console_access`).

Since 1.15 the machine's configuration, held in Convex, comes down signed on
the same channel (``GET /api/machine/config``, ``machine_config`` of the shared
file), and the heartbeat's ``config_hash`` is the hash of the configuration
applied (``null`` on a ``.env``). The console asks for it at rest, revalidates
it, applies it with a controlled restart, holds a safety key until an operator
at the console confirms it, and says what it applied or refused
(``POST /api/machine/config/applied``, :mod:`src.config_sync`). A dashboard
before 1.15 answers 404: the console keeps its configuration.

One exchange is outside this rule on purpose: **a stop**. It means the same
under every contract, so the dashboard answers the status request whatever
major the console announces, and the console ends its session on that answer
whatever major it is of: a stop asked for, or a session no longer active
(:mod:`src.cloud_sync`). Both can only cause an ordinary stop. Nothing else is
taken from an answer of another major.

Why the version is a constant and not read from a file at run time
------------------------------------------------------------------
The single source is ``contracts/machine-api.json`` at the repository root,
which the dashboard's code reads directly. This console is deployed as the
``raspberry-pi/`` directory alone (``scripts/pi/deploy.sh``, the Docker build
context), so that file is not on the machine. The constants below are therefore
pinned to it by ``tests/test_contract.py``: a change on either side that is not
made on the other fails the gate before it can reach a machine.

The software version is different: it is a fact about a build, not about the
code, so it IS read from a file (``raspberry-pi/VERSION``, written by the
release process), once, at startup.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Final, NewType, cast

_logger: logging.Logger = logging.getLogger(__name__)

ContractVersion = NewType("ContractVersion", str)
"""A well-formed ``major.minor`` contract version."""

ErrorCode = NewType("ErrorCode", str)
"""A stable error code of the machine API (``error_codes`` of the shared file)."""

SoftwareVersion = NewType("SoftwareVersion", str)
"""What this build calls itself: a git tag such as ``pi-0.4.2``."""

CONTRACT_VERSION: Final[ContractVersion] = ContractVersion("1.15")
"""The contract this console speaks. Pinned to ``contracts/machine-api.json``."""

CONTRACT_HEADER: Final[str] = "X-Anheart-Contract"
"""The request header that carries :data:`CONTRACT_VERSION`."""

SERVER_VERSION_FIELD: Final[str] = "server_contract_version"
"""Where a poll answer names the server's contract version."""

CONTRACT_UNSUPPORTED: Final[ErrorCode] = ErrorCode("contract_unsupported")
"""The code of the 426 a server answers to a major it does not serve."""

RATE_LIMITED: Final[ErrorCode] = ErrorCode("rate_limited")
"""The code of the 429 a server answers a machine over a route's rate limit."""

PAYLOAD_TOO_LARGE: Final[ErrorCode] = ErrorCode("payload_too_large")
"""The code of the 413 a server answers a body over a route's size."""

PROGRAMME_CHANGED: Final[ErrorCode] = ErrorCode("programme_changed")
"""The arming refusal of an automatic launch made from another programme list than
the one the machine holds (contract 1.12, ``launch_programme_list``)."""

ARMING_REFUSALS: Final[frozenset[ErrorCode]] = frozenset(
    {
        ErrorCode("expired_before_start"),
        ErrorCode("machine_deleted_before_start"),
        ErrorCode("organization_changed_before_start"),
        ErrorCode("right_revoked_before_start"),
        ErrorCode("rider_not_fit"),
        ErrorCode("fitness_expired"),
        ErrorCode("consent_missing"),
        ErrorCode("age_refused_before_start"),
        ErrorCode("hr_max_changed_before_start"),
        ErrorCode("rider_busy"),
        ErrorCode("machine_busy"),
        ErrorCode("incident_open"),
        PROGRAMME_CHANGED,
    }
)
"""The codes of the 409 a server answers a start it refuses (contract 1.6,
``machine_busy`` since 1.9 for a bench launch, ``incident_open`` since 1.10 and
``programme_changed`` since 1.12 for an automatic one; ``arming_refusals`` of
the shared file). Pinned to the shared file."""

STABLE_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {
        CONTRACT_UNSUPPORTED,
        ErrorCode("unauthorized"),
        ErrorCode("invalid_request"),
        ErrorCode("session_not_found"),
        ErrorCode("session_not_pending"),
        ErrorCode("machine_not_found"),
        ErrorCode("request_failed"),
        RATE_LIMITED,
        PAYLOAD_TOO_LARGE,
        *ARMING_REFUSALS,
        ErrorCode("feedback_not_eligible"),
        ErrorCode("feedback_session_not_ended"),
        ErrorCode("feedback_window_closed"),
    }
)
"""Every stable code the dashboard refuses a request under. Pinned to the shared file.

A refusal that carries one of them is in the dashboard's own shape: it is the
dashboard that answered. A refusal that carries none, or a word that is not
one of these, could come from anything that listens at the address.
"""

FEEDBACK_SINCE: Final[ContractVersion] = ContractVersion("1.7")
"""The first minor of this major whose dashboard takes the rider's feedback
(``feedback_events`` of the shared file): an earlier one would keep it as an
ordinary event of the session, with the session's other events."""

HEALTH_STATE_SINCE: Final[ContractVersion] = ContractVersion("1.14")
"""The first minor of this major whose dashboard takes the safety chain's and the
wall clock's state in the heartbeat's ``health`` (``health_state`` of the shared file):
an earlier one refuses a health object with a field it does not know."""

HEALTH_STATE_FIELDS: Final[tuple[str, ...]] = (
    "sto_state",
    "sto_source",
    "clock_trusted",
    "clock_max_error_s",
)
"""Those fields, left out of the heartbeat to a dashboard of an earlier minor."""

UNKNOWN_SOFTWARE_VERSION: Final[SoftwareVersion] = SoftwareVersion("pi-unknown")
"""Reported when ``VERSION`` is missing or unreadable: never a guess."""

VERSION_PATH: Final[Path] = Path(__file__).resolve().parent.parent / "VERSION"
"""``raspberry-pi/VERSION`` in a checkout, ``/app/VERSION`` in the image."""

UNKNOWN_THEIRS: Final[str] = "inconnu"
"""What the console shows for a server version it could not read."""

MAJORS_SHOWN: Final[int] = 4
"""How many of the majors a 426 lists the console's sentence names; the others are counted.

A dashboard serves one major, two while a migration lasts. The list is the
dashboard's to send all the same, and the sentence goes to the operator's
event list, to the session record and to the log: it names the first few and
says how many more there were, whatever was sent.
"""

MAX_REFUSAL_SENTENCE: Final[int] = 100
"""The most characters :func:`server_refusal` and :func:`unsupported_refusal` ever give."""

# ``[0-9]`` and never ``\d``: in a ``str`` pattern ``\d`` also matches the digits
# of every other script, and a version is ASCII.
_VERSION: Final[re.Pattern[str]] = re.compile(r"(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})")
_MAJOR: Final[re.Pattern[str]] = re.compile(r"0|[1-9][0-9]{0,3}")
_CODE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SOFTWARE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")


def version_of(value: object) -> ContractVersion | None:
    """The contract version in an untrusted value; ``None`` unless it is exactly one."""
    if isinstance(value, str) and _VERSION.fullmatch(value) is not None:
        return ContractVersion(value)
    return None


def major_of(version: ContractVersion) -> str:
    """The major of a contract version: what both sides must share."""
    return version.partition(".")[0]


def error_code_of(value: object) -> ErrorCode | None:
    """The stable code in an untrusted value; ``None`` for a sentence or anything else."""
    if isinstance(value, str) and _CODE.fullmatch(value) is not None:
        return ErrorCode(value)
    return None


def majors_of(value: object) -> tuple[str, ...]:
    """The contract majors a 426 says the server serves; anything unreadable is left out.

    Each major once, in the order first listed. A major is at most four
    digits, so what is kept of the list is bounded whatever its length was:
    ten thousand entries at the very most, and one or two in practice.
    """
    if not isinstance(value, list):
        return ()
    # A JSON array: its items are untrusted, so each is narrowed before use.
    items = cast("Sequence[object]", value)
    return tuple(
        dict.fromkeys(
            item for item in items if isinstance(item, str) and _MAJOR.fullmatch(item) is not None
        )
    )


def incompatible_server(theirs: str) -> str:
    """The one sentence the console shows and logs for a server it cannot work with."""
    return f"serveur incompatible (contrat {CONTRACT_VERSION} vs {theirs})"


def server_refusal(announced: object) -> str | None:
    """Why a poll answer must not arm anything here; ``None`` when it is of this major.

    ``announced`` is the answer's :data:`SERVER_VERSION_FIELD`, untrusted. A
    missing or unreadable version is refused: nothing says the rest of the
    answer means what this console would take it to mean.
    """
    theirs = version_of(announced)
    if theirs is None:
        return incompatible_server(UNKNOWN_THEIRS)
    if major_of(theirs) == major_of(CONTRACT_VERSION):
        return None
    return incompatible_server(theirs)


def minor_of(version: ContractVersion) -> int:
    """The minor of a contract version: what one side may add to a major."""
    return int(version.partition(".")[2])


def takes_feedback(announced: ContractVersion | None) -> bool:
    """Whether a dashboard that announced ``announced`` may be sent the rider's feedback.

    Only one of this console's major and of :data:`FEEDBACK_SINCE` or a later
    minor. A version not known (no heartbeat answered yet, or an unreadable
    one) is not one: the feedback waits rather than land among the events.
    """
    return serves(announced, FEEDBACK_SINCE)


def takes_health_state(announced: ContractVersion | None) -> bool:
    """Whether a dashboard that announced ``announced`` takes :data:`HEALTH_STATE_FIELDS`.

    As :func:`takes_feedback`, from :data:`HEALTH_STATE_SINCE`: before the first
    answer, and to an earlier minor, the heartbeat goes without them.
    """
    return serves(announced, HEALTH_STATE_SINCE)


def serves(announced: ContractVersion | None, since: ContractVersion) -> bool:
    """Whether ``announced`` is of this console's major and of ``since`` or a later minor."""
    return (
        announced is not None
        and major_of(announced) == major_of(CONTRACT_VERSION)
        and minor_of(announced) >= minor_of(since)
    )


def unsupported_refusal(supported: Sequence[str]) -> str:
    """The sentence for a 426: this console's version against the majors the server serves.

    The first :data:`MAJORS_SHOWN` are named and the others counted
    (``2, 3, 4, 5 et 596 autres``): the sentence stays a sentence, under
    :data:`MAX_REFUSAL_SENTENCE` characters, however many majors were listed.
    """
    if not supported:
        return incompatible_server(UNKNOWN_THEIRS)
    named = ", ".join(supported[:MAJORS_SHOWN])
    more = len(supported) - MAJORS_SHOWN
    if more <= 0:
        return incompatible_server(named)
    return incompatible_server(f"{named} et {more} {'autre' if more == 1 else 'autres'}")


def read_software_version(path: Path = VERSION_PATH) -> SoftwareVersion:
    """This build's version, from ``VERSION``; :data:`UNKNOWN_SOFTWARE_VERSION` if it has none.

    Read once at startup, never from the control loop. A missing, unreadable
    or malformed file must not keep the console from starting: the dashboard
    is told the version is unknown, which is true.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        _logger.warning("software version unreadable (%s): reporting it as unknown", error)
        return UNKNOWN_SOFTWARE_VERSION
    if _SOFTWARE.fullmatch(text) is None:
        _logger.warning("software version malformed in %s: reporting it as unknown", path)
        return UNKNOWN_SOFTWARE_VERSION
    return SoftwareVersion(text)

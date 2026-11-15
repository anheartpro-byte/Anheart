"""The commissioning of a console (ANH-115, contract 1.18): the pre-flight in commissioning mode.

At rest, the capsule empty, an operator signed in starts it from the console's page
(:class:`CommissioningDesk`). It runs the pre-flight again (the BITalino's battery read
afresh, every control waited for), reads the drive's parameters back, then judges
:data:`COMMISSIONING_CONTROLS`: the pre-flight's list as it is at the bench, but the clock,
the BITalino and the medical parameters red when they do not hold, and three controls of
its own (:func:`sto_source_result`, :func:`sto_ready_result`, :func:`drive_limits_result`).
The pre-flight itself is not changed: its bench rules and its nobody-on-board lock stay
what :mod:`src.training.preflight` says.

What it judged and read goes to the dashboard (``POST /api/machine/commissioning``,
``machine_commissioning`` of ``contracts/machine-api.json``), with the emergency-stop
attestation of this run of the console and who gave it. The dashboard compares it with the
machine's registry: the drive's values with those typed with its drive sheet, the
BITalino's address, the software version. It records it signed, and answers the verdict
and each finding, which the card shows. A red commissioning is sent too: it is evidence. A
declared STO bridge is red here, and any STO source but the drive's input is a finding
there: such a machine is never recorded conforming. A commissioning changes nothing on the
console, and nothing of the machine's validation level.

Refused before anything is read: one under way, a console not linked to a dashboard of
1.18 or later, no emergency-stop attestation since this console started, a console not at
rest. One that leaves rest while it runs is dropped, and nothing is sent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, Protocol, assert_never, cast

from src.clock import Clock
from src.cloud_sync import CloudError, Document, Refused, Unreachable
from src.commissioning_view import (
    CommissioningView,
    Finding,
    LastRun,
    NotRecorded,
    Outcome,
    Recorded,
    Verdict,
)
from src.contract import ContractVersion, ErrorCode, serves
from src.motor.drive import DriveError, DriveLimits, check_limits, describe_violation
from src.motor.sto import StoReading, StoSourceKind, sto_bridged
from src.result import Err, Ok, Result
from src.training.plan import JsonValue
from src.training.preflight import (
    CONTROLS,
    Control,
    ControlResult,
    ControlState,
    PreflightInputs,
    Severity,
    check_sto_ready,
    describe_sto_refusal,
    judge,
)
from src.training.runtime import describe_drive_error
from src.training.safety import EstopAttestation, MotionRefusal
from src.training.types import Occupancy
from src.units import Hertz, Seconds, UnixMillis, elapsed

_logger = logging.getLogger(__name__)

COMMISSIONING_SINCE: Final[ContractVersion] = ContractVersion("1.18")
"""The first contract whose dashboard records a commissioning (``since``)."""

COMMISSIONING_PATH: Final[str] = "/api/machine/commissioning"

COMMISSIONING_REFUSED: Final[ErrorCode] = ErrorCode("commissioning_refused")

RAISED: Final[frozenset[str]] = frozenset({"clock", "bitalino", "medical"})
"""The pre-flight's controls a commissioning holds red where the bench holds them orange, or
does not judge them: its records are dated by the clock, and name the BITalino and the
medical parameters the machine runs with."""

COMMISSIONING_CONTROLS: Final[tuple[Control, ...]] = tuple(
    replace(control, bench=Severity.BLOCK) if control.id in RAISED else control
    for control in CONTROLS
)
"""The pre-flight, in its order, judged at the bench, with :data:`RAISED` red."""

_DRIVE_STATE: Final[Control] = next(control for control in CONTROLS if control.id == "drive_state")
"""What "at rest" is here: the drive neither in fault nor running, the console in REPOS."""

RUN_WAIT: Final[Seconds] = Seconds(60.0)
"""How long a commissioning waits for its pre-flight run: the battery wait (15 s) and the
run's longest wait after the read (44 s), and no more."""

POLL: Final[Seconds] = Seconds(0.5)
"""How often a commissioning looks at its pre-flight run, and at the console's rest."""

MAX_DETAIL_CHARS: Final[int] = 300
MAX_STATEMENT_CHARS: Final[int] = 400
MAX_TEXT_CHARS: Final[int] = 64
"""The contract's bounds (``max_detail_chars``, ``max_statement_chars``, ``max_text_chars``)."""

MAX_BODY_BYTES: Final[int] = 16_384
"""The route's largest body (``max_body_bytes``), as the transport encodes it: ``json.dumps``,
which writes every character beyond ASCII as six bytes."""

MAX_FINDINGS: Final[int] = 64
"""Findings an answer may carry, at most: one per control and one per fixed id, and room."""

UNDER_WAY: Final[str] = "une mise en service est deja en cours"
NOT_LINKED: Final[str] = (
    "mise en service impossible : tableau de bord pas encore joint, ou anterieur au contrat "
    "1.18 (il ne l'enregistrerait pas)"
)
UNATTESTED: Final[str] = (
    "arret d'urgence non atteste depuis le demarrage de la console : donner l'attestation, "
    "page Securite"
)
UNREADABLE_ANSWER: Final[str] = (
    "reponse du tableau de bord illisible : la mise en service n'est pas enregistree"
)

STO_SOURCE_LABEL: Final[str] = "Source de la chaine STO"
STO_READY_LABEL: Final[str] = "Chaine STO prete"
DRIVE_LIMITS_LABEL: Final[str] = "Parametres du variateur relus (tFr, HSP, LSP, ACC, dEC)"


def never_coasting() -> str | None:
    """No coast hold to wait for.

    The seam of the coast-hold guard (``CoastDownHold``, built apart on the runtime):
    until it lands, "at rest" is :data:`_DRIVE_STATE`'s rule alone. Once it does, the
    console says here why its shaft may still turn, and no commissioning starts or goes on
    while it does: a zero reference with the stage off does not prove the shaft still.
    """
    return None


def dropped(why: str) -> str:
    """The words of a commissioning dropped because the console left rest: nothing is sent."""
    return f"mise en service interrompue, rien n'est envoye : {why}"


# =========================================================================
# The controls of its own
# =========================================================================


def sto_source_result(source: StoSourceKind) -> ControlResult:
    """The STO's source: the drive's input green; a declared bridge red; any other orange.

    A machine whose emergency stop does not open the drive's STO input is never recorded
    conforming: the dashboard holds every source but ``drive_input`` as a finding.
    """
    match source:
        case StoSourceKind.DRIVE_INPUT:
            return ControlResult(
                "sto_source",
                STO_SOURCE_LABEL,
                ControlState.OK,
                "chaine STO lue sur l'entree du variateur",
            )
        case StoSourceKind.BRIDGED_DECLARED:
            return ControlResult(
                "sto_source",
                STO_SOURCE_LABEL,
                ControlState.BLOCK,
                "STO ponte declare, aucun arret d'urgence materiel : la mise en service est "
                "enregistree non conforme",
            )
        case StoSourceKind.NONE | StoSourceKind.SIM_READY | StoSourceKind.SIM_OPEN:
            return ControlResult(
                "sto_source",
                STO_SOURCE_LABEL,
                ControlState.WARN,
                f"chaine STO {source.value}, pas l'entree du variateur : la mise en service "
                "est enregistree non conforme",
            )
    raise assert_never(source)


def sto_ready_result(reading: StoReading) -> ControlResult:
    """The STO as a start at the bench takes it: READY, or a declared bridge."""
    if sto_bridged(reading):
        return ControlResult(
            "sto_ready",
            STO_READY_LABEL,
            ControlState.OK,
            "STO ponte declare : depart au banc seulement",
        )
    refusal = check_sto_ready(reading)
    if refusal is None:
        return ControlResult("sto_ready", STO_READY_LABEL, ControlState.OK, "chaine STO prete")
    return ControlResult(
        "sto_ready", STO_READY_LABEL, ControlState.BLOCK, describe_sto_refusal(refusal)
    )


def drive_limits_result(read: Result[DriveLimits, DriveError], ceiling: Hertz) -> ControlResult:
    """The drive's parameters, read back now, and judged as every arming judges them."""
    match read:
        case Ok(limits):
            checked = check_limits(limits, ceiling)
            if isinstance(checked, Err):
                return ControlResult(
                    "drive_limits",
                    DRIVE_LIMITS_LABEL,
                    ControlState.BLOCK,
                    f"{describe_violation(checked.error)} (lus : {limits.describe()})",
                )
            return ControlResult(
                "drive_limits", DRIVE_LIMITS_LABEL, ControlState.OK, f"relus : {limits.describe()}"
            )
        case Err(error):
            return ControlResult(
                "drive_limits",
                DRIVE_LIMITS_LABEL,
                ControlState.BLOCK,
                f"parametres du variateur illisibles : {describe_drive_error(error).detail}",
            )
    raise assert_never(read)


# =========================================================================
# What is sent, and what comes back
# =========================================================================


@dataclass(frozen=True, slots=True, kw_only=True)
class Facts:
    """What the record names of this console, besides what it judged."""

    sto_source: StoSourceKind
    bitalino_address: str | None
    software_version: str
    medical_version: str
    config_hash: str | None


FORBIDDEN_CHARS: Final[re.Pattern[str]] = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u200b-\u200d\u2028\u2029\u202a-\u202e\u2060\u2066-\u2069\ufeff]"
)
"""What the dashboard refuses in any text of a report: C0, DEL and C1 controls, the
bidirectional overrides and isolates, the line and paragraph separators, the zero-width
characters. Each becomes a space here."""


def _text(value: str, limit: int) -> str:
    """``value`` as the contract takes text: none of :data:`FORBIDDEN_CHARS`, no space at
    either end, at most ``limit`` characters, never empty."""
    return FORBIDDEN_CHARS.sub(" ", value).strip()[:limit].strip() or "-"


def _short(value: str | None) -> str | None:
    """A short text of the contract, or ``None`` for one that is not set."""
    if value is None or value.strip() == "":
        return None
    return _text(value, MAX_TEXT_CHARS)


def drive_fields(limits: DriveLimits) -> Mapping[str, JsonValue]:
    """The five values read, in the contract's names and units."""
    return {
        "tfrHz": limits.max_frequency,
        "hspHz": limits.high_speed,
        "lspHz": limits.low_speed,
        "accS": limits.acceleration,
        "decS": limits.deceleration,
    }


def report_body(
    *,
    ran_at: UnixMillis,
    operator: str,
    attestation: EstopAttestation,
    controls: Sequence[ControlResult],
    limits: DriveLimits | None,
    facts: Facts,
) -> Mapping[str, JsonValue]:
    """The body of ``POST /api/machine/commissioning``, within :data:`MAX_BODY_BYTES`.

    Every control's detail is shortened alike, halved each time, until the encoded body
    fits: a detail of 300 characters beyond ASCII takes six bytes each. At one character
    each, the largest report the contract allows fits with room to spare.
    """
    limit = MAX_DETAIL_CHARS
    body = _body(ran_at, operator, attestation, controls, limits, facts, limit)
    while len(json.dumps(body)) > MAX_BODY_BYTES and limit > 1:
        limit //= 2
        body = _body(ran_at, operator, attestation, controls, limits, facts, limit)
    return body


def _body(  # noqa: PLR0917  # the report's parts, and the length of a detail
    ran_at: UnixMillis,
    operator: str,
    attestation: EstopAttestation,
    controls: Sequence[ControlResult],
    limits: DriveLimits | None,
    facts: Facts,
    detail_limit: int,
) -> Mapping[str, JsonValue]:
    return {
        "ranAt": ran_at,
        "operator": _text(operator, MAX_TEXT_CHARS),
        "attestation": {
            "operator": _text(attestation.operator, MAX_TEXT_CHARS),
            "statement": _text(attestation.statement, MAX_STATEMENT_CHARS),
            "at": attestation.wall_clock,
        },
        "stoSource": facts.sto_source.value,
        "controls": [
            {
                "id": result.id,
                "state": result.state.value,
                "detail": _text(result.detail, detail_limit),
            }
            for result in controls
        ],
        "drive": None if limits is None else drive_fields(limits),
        "bitalinoAddress": _short(facts.bitalino_address),
        "softwareVersion": _text(facts.software_version, MAX_TEXT_CHARS),
        "medicalParametersVersion": _short(facts.medical_version),
        "configHash": facts.config_hash,
    }


def _finding(entry: object) -> Finding | None:
    """One finding of an answer, or ``None`` for one that is not three texts."""
    if not isinstance(entry, dict):
        return None
    fields = cast("Document", entry)
    finding_id, expected, read = fields.get("id"), fields.get("expected"), fields.get("read")
    if isinstance(finding_id, str) and isinstance(expected, str) and isinstance(read, str):
        return Finding(finding_id, expected, read)
    return None


def recorded_of(document: Document) -> Recorded | None:
    """The answer of a commissioning recorded, or ``None`` for one this console cannot read."""
    record_id = document.get("recordId")
    verdict = document.get("verdict")
    findings = document.get("findings")
    signed = document.get("signed")
    if (
        not isinstance(record_id, str)
        or record_id == ""
        or verdict not in {member.value for member in Verdict}
        or not isinstance(findings, list)
        or not isinstance(signed, bool)
    ):
        return None
    entries = cast("list[object]", findings)
    if len(entries) > MAX_FINDINGS:
        return None
    parsed = tuple(_finding(entry) for entry in entries)
    kept = tuple(finding for finding in parsed if finding is not None)
    if len(kept) != len(parsed):
        return None
    return Recorded(record_id, Verdict(verdict), kept, signed)


def outcome_of(sent: Result[Document, CloudError]) -> Outcome:
    """What the dashboard made of a commissioning sent, in the card's words."""
    match sent:
        case Ok(document):
            recorded = recorded_of(document)
            return NotRecorded(UNREADABLE_ANSWER) if recorded is None else recorded
        case Err(error):
            match error:
                case Refused():
                    if error.code == COMMISSIONING_REFUSED:
                        return NotRecorded(f"refusee par le tableau de bord : {error.detail}")
                    return NotRecorded(
                        f"refusee par le tableau de bord ({error.status}) : {error.detail}"
                    )
                case Unreachable():
                    return NotRecorded(f"tableau de bord injoignable : {error.detail}")
            raise assert_never(error)
    raise assert_never(sent)


# =========================================================================
# The desk
# =========================================================================


class PreflightRuns(Protocol):
    """The pre-flight as a commissioning drives it: a
    :class:`~src.training.preflight.PreflightMonitor`."""

    @property
    @abstractmethod
    def running(self) -> bool:
        """Whether a run is under way."""

    @abstractmethod
    def rerun(self) -> None:
        """Start a run now: the battery read again, every control waited for again."""

    @abstractmethod
    def inputs(self) -> PreflightInputs:
        """What the controls read now."""


async def _pause(delay: Seconds) -> None:
    await asyncio.sleep(delay)


@dataclass(frozen=True, slots=True, kw_only=True)
class CommissioningSetup:
    """What a commissioning reads of this console."""

    clock: Clock
    monitor: PreflightRuns
    attestation: Callable[[], EstopAttestation | None]
    """The emergency-stop attestation of this run of the console; ``None``: none given."""

    sto: Callable[[], StoReading]
    read_limits: Callable[[], Awaitable[Result[DriveLimits, DriveError]]]
    """The drive's five parameters, read back: a READ, nothing written."""

    ceiling: Hertz
    """The HSP this console arms under (``max_motor_hz``)."""

    facts: Facts
    coasting: Callable[[], str | None] = never_coasting
    """Why the shaft may still turn; ``None``: nothing says so (:func:`never_coasting`)."""

    pause: Callable[[Seconds], Awaitable[None]] = _pause


@dataclass(frozen=True, slots=True)
class DashboardLink:
    """How a commissioning reaches the dashboard: the link's transport, and what it heard."""

    post: Callable[[Mapping[str, JsonValue]], Awaitable[Result[Document, CloudError]]]
    """``POST`` a body to :data:`COMMISSIONING_PATH`."""

    announced: Callable[[], ContractVersion | None]
    """The contract the dashboard announced in its last heartbeat answered."""


def attested(confirmed: Result[EstopAttestation, MotionRefusal]) -> EstopAttestation | None:
    """The attestation the runtime holds, or ``None`` before one was given."""
    return confirmed.value if isinstance(confirmed, Ok) else None


class CommissioningDesk:
    """The commissioning of this console: one at a time, the last one kept for the card.

    Mutable on purpose: whether one is under way, the last one, and the dashboard link once
    it is built (:meth:`bind`). Read and written on the event loop only.
    """

    __slots__ = ("_busy", "_last", "_link", "_setup")

    def __init__(self, setup: CommissioningSetup) -> None:
        self._setup = setup
        self._busy = False
        self._last: LastRun | None = None
        self._link: DashboardLink | None = None

    def bind(self, link: DashboardLink) -> None:
        """The dashboard link, built after the page's services: until then, none starts."""
        self._link = link

    def view(self) -> CommissioningView:
        ready = self._ready()
        return CommissioningView(
            running=self._busy,
            refusal=ready.error if isinstance(ready, Err) else None,
            last=self._last,
        )

    def _away(self) -> str | None:
        """Why the console is not at rest now; ``None`` when it is."""
        found = _DRIVE_STATE.check(self._setup.monitor.inputs())
        if isinstance(found, Err):
            return found.error.reason
        return self._setup.coasting()

    def _ready(self) -> Result[tuple[EstopAttestation, DashboardLink], str]:
        """The attestation a commissioning would carry now and where it goes, or why none
        can start."""
        link = self._link
        if self._busy:
            return Err(UNDER_WAY)
        if link is None or not serves(link.announced(), COMMISSIONING_SINCE):
            return Err(NOT_LINKED)
        attestation = self._setup.attestation()
        if attestation is None:
            return Err(UNATTESTED)
        away = self._away()
        if away is not None:
            return Err(f"console pas au repos : {away}")
        return Ok((attestation, link))

    async def run(self, operator: str) -> Result[LastRun, str]:
        """Run a commissioning for ``operator`` and send it; or why not, nothing sent."""
        ready = self._ready()
        if isinstance(ready, Err):
            return ready
        attestation, link = ready.value
        self._busy = True
        try:
            return await self._run(operator, attestation, link)
        finally:
            self._busy = False

    async def _run(
        self, operator: str, attestation: EstopAttestation, link: DashboardLink
    ) -> Result[LastRun, str]:
        setup = self._setup
        began = setup.clock.monotonic()
        setup.monitor.rerun()
        while setup.monitor.running and elapsed(began, setup.clock.monotonic()) < RUN_WAIT:
            away = self._away()
            if away is not None:
                return Err(dropped(away))
            await setup.pause(POLL)
        read = await setup.read_limits()
        # The run and the read awaited: the console may have left rest meanwhile.
        away = self._away()
        if away is not None:
            return Err(dropped(away))
        controls = (
            *judge(COMMISSIONING_CONTROLS, setup.monitor.inputs(), Occupancy.BENCH).results,
            sto_source_result(setup.facts.sto_source),
            sto_ready_result(setup.sto()),
            drive_limits_result(read, setup.ceiling),
        )
        ran_at = setup.clock.unix_millis()
        body = report_body(
            ran_at=ran_at,
            operator=operator,
            attestation=attestation,
            controls=controls,
            limits=read.value if isinstance(read, Ok) else None,
            facts=setup.facts,
        )
        outcome = outcome_of(await link.post(body))
        last = LastRun(at=ran_at, operator=operator, controls=controls, outcome=outcome)
        self._last = last
        _logger.info("commissioning by %s: %s", operator, describe_outcome(outcome))
        return Ok(last)


def describe_outcome(outcome: Outcome) -> str:
    """One line for the log: the verdict and the findings' ids, or why it was not recorded."""
    match outcome:
        case Recorded():
            ids = ", ".join(finding.id for finding in outcome.findings) or "aucun ecart"
            return f"recorded {outcome.verdict.value} ({ids})"
        case NotRecorded():
            return f"not recorded ({outcome.reason})"
    raise assert_never(outcome)

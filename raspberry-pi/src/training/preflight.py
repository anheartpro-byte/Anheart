"""The pre-flight: what every start must pass, as one list (ANH-142), and the STO control.

The list (:data:`CONTROLS`) is the single source of the pre-flight. The
console's page shows it (``/api/preflight``), every arming judges it
(:meth:`PreflightMonitor.refusal`, asked by the runtime's arming gate and by
the preflight of a launch from the dashboard), every completed run is written
to the logbook (``kind: preflight``) and the dashboard is told the verdict of
the last one (``preflight_last``) with the battery it read.

Each control is a pure function of what the console holds in memory
(:class:`PreflightInputs`): it returns ``Ok(detail)`` or ``Err`` with the
reason. A control has a severity with a person on board and one at the bench
(``None``: not judged there), and a longest wait: while a run is under way a
control that is not green yet is *pending* until its wait is over, then red
(``block``) or orange (``warn``). An arming never waits: it is refused on the
first blocking control that is not green now.

A future control (a door closed, a harness buckled) is one more entry of the
list: the page, the record and the heartbeat follow without a change.

The STO-ready control, shared by every arming path, is at the end of this
module; it predates the list and keeps its own refusals.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum, unique
from types import MappingProxyType
from typing import Final, assert_never, final

from src.clock import Clock
from src.link_state import LinkState, LinkStatus
from src.link_state import label_of as link_label
from src.motor.drive import DriveState
from src.motor.sto import StoReading, StoState, sto_bridged, sto_ready
from src.result import Err, Ok, Result
from src.training.types import Occupancy, RunMode, TelemetrySnapshot
from src.units import Monotonic, Percent, Seconds, elapsed
from src.wall_clock import Synchronised, WallClockState, detail_of
from src.wall_clock import label_of as clock_label

# =========================================================================
# What a control says
# =========================================================================


@unique
class Severity(Enum):
    """What a control that is not green does to a start."""

    BLOCK = "block"
    """Refuses it."""

    WARN = "warn"
    """Lets it start; the operator is shown why, and it is written down."""


@unique
class ControlState(Enum):
    """A control as the page shows it. Wire strings; ``ok``, ``warn`` and ``block``
    are also the dashboard's ``preflight_last``."""

    OK = "ok"
    PENDING = "pending"
    WARN = "warn"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class Failed:
    """The control is not satisfied: red or orange, as its severity says."""

    reason: str


@dataclass(frozen=True, slots=True)
class Unjudged:
    """Nothing to judge against (no reference from the server): orange, never red."""

    reason: str


type Finding = Failed | Unjudged


@dataclass(frozen=True, slots=True)
class BatteryReading:
    """The BITalino's battery as the last run read it, at rest."""

    percent: Percent | None
    """``None``: it could not be read (BITalino absent, transport that cannot say it)."""

    at: Monotonic


DEFAULT_MIN_BATTERY: Final[Percent] = Percent(30.0)
"""``BITALINO_MIN_BATTERY_PCT`` when it is not set."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PreflightInputs:
    """Everything the controls read, in memory, at one instant. Nothing here waits."""

    at: Monotonic
    snapshot: TelemetrySnapshot
    """The runtime's last picture: the drive, the verdict, the heart rate, the mode."""

    attested: bool
    """Whether the emergency-stop attestation of this run of the console was given."""

    bitalino_connected: bool
    wall_clock: WallClockState | None
    """``None``: the system cannot be asked (anything but the Pi)."""

    record_storage: str | None
    """Why the session record has nowhere to go, in its words; ``None``: it has room."""

    link: LinkStatus
    medical_refusal: str | None
    """Why the medical parameters do not hold (unsigned, altered); ``None``: signed."""

    medical_version: str
    software_version: str
    incidents_open_high: int | None = None
    """As the dashboard last announced it; ``None``: not announced."""

    cpu_temp_c: float | None = None
    records_pending: int | None = None
    min_battery: Percent = DEFAULT_MIN_BATTERY
    battery: BatteryReading | None = None
    """``None``: not read yet. Filled in by :class:`PreflightMonitor`."""

    ecg_good_since: Monotonic | None = None
    """Since when every heart-rate sample seen was fresh and ``good``. Filled in by
    :class:`PreflightMonitor`."""


type Check = Callable[[PreflightInputs], Result[str, Finding]]


@dataclass(frozen=True, slots=True, kw_only=True)
class Control:
    """One entry of the list."""

    id: str
    """Stable, for the page, the record and the logs."""

    label: str
    on_board: Severity
    bench: Severity | None
    """``None``: not judged for a start with nobody on board."""

    max_duration: Seconds
    """How long a run waits for it to turn green before it is red or orange."""

    check: Check


# =========================================================================
# The controls
# =========================================================================

CLOCK_MAX_ERROR: Final[Seconds] = Seconds(5.0)
"""The wall clock's error a start with a person on board accepts, at most: the
bound the time service guarantees, never its estimate."""

ECG_WINDOW: Final[Seconds] = Seconds(30.0)
"""How long the ECG must have been ``good`` without a break, with a person on board."""

BATTERY_MAX_AGE: Final[Seconds] = Seconds(30 * 60.0)
"""A battery read longer ago than this says nothing of now: the run is to be relaunched."""

BATTERY_WAIT: Final[Seconds] = Seconds(15.0)
"""How long a run waits for the BITalino to be connected before it reads its battery."""

PI_HOT_C: Final[float] = 70.0


def _drive_link(inputs: PreflightInputs) -> Result[str, Finding]:
    snapshot = inputs.snapshot
    age = snapshot.drive_status_age
    if age is None or snapshot.drive_status_is_stale:
        return Err(Failed("variateur injoignable : aucune lecture recente de son etat"))
    return Ok(
        f"variateur lu il y a {age:.1f} s ; ses parametres (tFr, HSP, LSP, ACC, dEC) "
        "sont relus et juges a chaque armement"
    )


def _drive_state(inputs: PreflightInputs) -> Result[str, Finding]:
    snapshot = inputs.snapshot
    if snapshot.drive_status_is_stale:
        return Err(Failed("etat du variateur inconnu : aucune lecture recente"))
    report = snapshot.fault
    if report is not None or snapshot.drive_state is DriveState.FAULT:
        code = "?" if report is None else f"{report.fault.mnemonic}, LFT {report.raw_code}"
        return Err(Failed(f"variateur en defaut ({code}) : le reinitialiser a la console"))
    if (
        snapshot.mode is not RunMode.REPOS
        or snapshot.drive_state is DriveState.OPERATION_ENABLED
        or snapshot.measured.motor_rpm != 0
    ):
        return Err(Failed("variateur en marche ou seance en cours : attendre l'arret complet"))
    return Ok(f"variateur a l'arret, sans defaut ({snapshot.drive_state.name})")


def _estop(inputs: PreflightInputs) -> Result[str, Finding]:
    if inputs.attested:
        return Ok("attestation de l'arret d'urgence donnee")
    return Err(Failed("arret d'urgence non atteste : donner l'attestation, page Securite"))


def _verdict(inputs: PreflightInputs) -> Result[str, Finding]:
    verdict = inputs.snapshot.safety
    if verdict is None:
        return Ok("aucun verdict de securite en cours")
    return Err(Failed(f"verdict {verdict.rule} en cours : l'acquitter, page Securite"))


def _disk(inputs: PreflightInputs) -> Result[str, Finding]:
    refusal = inputs.record_storage
    if refusal is None:
        return Ok("place pour l'enregistrement de seance (500 Mo libres au moins)")
    return Err(Failed(refusal))


def _clock(inputs: PreflightInputs) -> Result[str, Finding]:
    state = inputs.wall_clock
    if state is None:
        return Err(Unjudged("horloge non jugee : ce systeme ne peut pas etre interroge"))
    if not isinstance(state, Synchronised):
        return Err(Failed(f"horloge {clock_label(state)} : {detail_of(state)}"))
    if state.max_error >= CLOCK_MAX_ERROR:
        return Err(
            Failed(
                f"horloge : erreur jusqu'a {state.max_error:.1f} s, "
                f"{CLOCK_MAX_ERROR:.0f} s au plus avec une personne a bord"
            )
        )
    return Ok(f"horloge synchronisee : {detail_of(state)}")


def _contract(inputs: PreflightInputs) -> Result[str, Finding]:
    link = inputs.link
    state = link.state
    match state:
        case LinkState.NOT_CONFIGURED:
            return Ok("console non reliee au tableau de bord : sans objet")
        case LinkState.REACHABLE:
            return Ok("contrat servi par le tableau de bord")
        case LinkState.INCOMPATIBLE:
            return Err(Failed(f"contrat refuse par le tableau de bord : {link.detail}"))
        case (
            LinkState.WAITING
            | LinkState.UNREACHABLE
            | LinkState.KEY_REFUSED
            | LinkState.SERVER_ERROR
        ):
            return Err(Unjudged(f"contrat non verifie : tableau de bord {link_label(state)}"))
    raise assert_never(state)


def _medical(inputs: PreflightInputs) -> Result[str, Finding]:
    refusal = inputs.medical_refusal
    if refusal is not None:
        return Err(Failed(refusal))
    return Ok(
        f"parametres medicaux {inputs.medical_version} signes (version attendue non jugee : "
        "aucune reference du serveur)"
    )


def _incidents(inputs: PreflightInputs) -> Result[str, Finding]:
    count = inputs.incidents_open_high
    if count is None:
        return Err(Unjudged("incidents graves ouverts : non annonces par le tableau de bord"))
    if count > 0:
        return Err(Failed(f"{count} incident(s) grave(s) ouvert(s) sur cette machine"))
    return Ok("aucun incident grave ouvert")


def _bitalino(inputs: PreflightInputs) -> Result[str, Finding]:
    if not inputs.bitalino_connected:
        return Err(Failed("BITalino injoignable : l'allumer, le rapprocher, verifier l'appairage"))
    reading = inputs.battery
    if reading is None:
        return Err(Failed("batterie BITalino pas encore lue"))
    percent = reading.percent
    if percent is None:
        return Err(Failed("batterie BITalino illisible : relancer le pre-vol"))
    age = elapsed(reading.at, inputs.at)
    if age > BATTERY_MAX_AGE:
        return Err(Failed(f"batterie BITalino lue il y a {age / 60:.0f} min : relancer le pre-vol"))
    floor = inputs.min_battery
    if percent < floor:
        return Err(Failed(f"batterie BITalino {math.floor(percent)} % < {floor:g} %"))
    return Ok(f"BITalino joignable, batterie {math.floor(percent)} %")


def _ecg(inputs: PreflightInputs) -> Result[str, Finding]:
    since = inputs.ecg_good_since
    if since is None:
        snapshot = inputs.snapshot
        sample = snapshot.heart_rate
        quality = (
            "sans mesure recente"
            if sample is None or snapshot.heart_rate_is_stale
            else sample.quality.value
        )
        return Err(Failed(f"qualite ECG {quality} : {ECG_WINDOW:.0f} s continues de good requises"))
    good_for = elapsed(since, inputs.at)
    if good_for < ECG_WINDOW:
        window = f"{ECG_WINDOW:.0f} s continues requises"
        return Err(Failed(f"qualite ECG good depuis {good_for:.0f} s, {window}"))
    return Ok(f"qualite ECG good depuis {good_for:.0f} s")


def _temperature(inputs: PreflightInputs) -> Result[str, Finding]:
    celsius = inputs.cpu_temp_c
    if celsius is None:
        return Err(Unjudged("temperature du Pi non mesuree"))
    if celsius > PI_HOT_C:
        return Err(Failed(f"Pi a {celsius:.0f} °C, au-dessus de {PI_HOT_C:.0f} °C : l'aerer"))
    return Ok(f"Pi a {celsius:.0f} °C")


def _cloud(inputs: PreflightInputs) -> Result[str, Finding]:
    link = inputs.link
    if link.state is LinkState.REACHABLE:
        return Ok("tableau de bord joignable")
    if link.state is LinkState.NOT_CONFIGURED:
        return Ok("console non reliee au tableau de bord : sans objet")
    return Err(
        Failed(
            f"tableau de bord {link_label(link.state)} ({link.detail}) : la seance reste "
            "possible, ce qu'elle doit au tableau de bord partira plus tard"
        )
    )


def _records(inputs: PreflightInputs) -> Result[str, Finding]:
    if inputs.link.state is LinkState.NOT_CONFIGURED:
        return Ok("console non reliee au tableau de bord : rien a envoyer")
    pending = inputs.records_pending
    if pending is None:
        return Err(Unjudged("enregistrements a envoyer : pas encore comptes"))
    if pending > 0:
        return Err(Failed(f"{pending} enregistrement(s) de seance pas encore envoye(s)"))
    return Ok("tous les enregistrements de seance sont envoyes")


def _software(inputs: PreflightInputs) -> Result[str, Finding]:
    return Err(
        Unjudged(f"version {inputs.software_version} non jugee : aucune reference du serveur")
    )


BLOCK: Final[Severity] = Severity.BLOCK
WARN: Final[Severity] = Severity.WARN

CONTROLS: Final[tuple[Control, ...]] = (
    Control(
        id="drive_link",
        label="Liaison variateur et parametres relus",
        on_board=BLOCK,
        bench=BLOCK,
        max_duration=Seconds(10.0),
        check=_drive_link,
    ),
    Control(
        id="drive_state",
        label="Variateur ni en defaut ni en marche",
        on_board=BLOCK,
        bench=BLOCK,
        max_duration=Seconds(10.0),
        check=_drive_state,
    ),
    Control(
        id="estop",
        label="Attestation de l'arret d'urgence",
        on_board=BLOCK,
        bench=BLOCK,
        max_duration=Seconds(0.0),
        check=_estop,
    ),
    Control(
        id="verdict",
        label="Aucun verdict de securite en cours",
        on_board=BLOCK,
        bench=BLOCK,
        max_duration=Seconds(0.0),
        check=_verdict,
    ),
    Control(
        id="disk",
        label="Espace disque (500 Mo)",
        on_board=BLOCK,
        bench=BLOCK,
        max_duration=Seconds(10.0),
        check=_disk,
    ),
    Control(
        id="clock",
        label="Horloge (erreur < 5 s)",
        on_board=BLOCK,
        bench=WARN,
        max_duration=Seconds(10.0),
        check=_clock,
    ),
    Control(
        id="contract",
        label="Contrat compatible avec le tableau de bord",
        on_board=BLOCK,
        bench=BLOCK,
        max_duration=Seconds(30.0),
        check=_contract,
    ),
    Control(
        id="medical",
        label="Parametres medicaux signes",
        on_board=BLOCK,
        bench=None,
        max_duration=Seconds(0.0),
        check=_medical,
    ),
    Control(
        id="incidents",
        label="Aucun incident grave ouvert",
        on_board=BLOCK,
        bench=BLOCK,
        max_duration=Seconds(30.0),
        check=_incidents,
    ),
    Control(
        id="bitalino",
        label="BITalino joignable et batterie",
        on_board=BLOCK,
        bench=WARN,
        max_duration=Seconds(10.0),
        check=_bitalino,
    ),
    Control(
        id="ecg",
        label="Qualite ECG good depuis 30 s",
        on_board=BLOCK,
        bench=None,
        max_duration=Seconds(44.0),
        check=_ecg,
    ),
    Control(
        id="temperature",
        label="Temperature du Pi (70 °C)",
        on_board=WARN,
        bench=WARN,
        max_duration=Seconds(15.0),
        check=_temperature,
    ),
    Control(
        id="cloud",
        label="Tableau de bord joignable",
        on_board=WARN,
        bench=WARN,
        max_duration=Seconds(30.0),
        check=_cloud,
    ),
    Control(
        id="records",
        label="Enregistrements envoyes",
        on_board=WARN,
        bench=WARN,
        max_duration=Seconds(15.0),
        check=_records,
    ),
    Control(
        id="software",
        label="Version logicielle",
        on_board=WARN,
        bench=WARN,
        max_duration=Seconds(0.0),
        check=_software,
    ),
)
"""The pre-flight, in order: the first red blocking control is the reason a start is refused.

The ECG comes last of the blocking ones because it is the long one (30 s of
good signal, after the battery is read). A run waits at most its longest
wait, 44 s from the battery read: under the 45 s the pre-flight is allowed."""


# =========================================================================
# Judging the list
# =========================================================================

NOT_AT_THE_BENCH: Final[str] = "sans objet au banc : aucune personne a bord"


@dataclass(frozen=True, slots=True)
class ControlResult:
    """One control, judged."""

    id: str
    label: str
    state: ControlState
    detail: str


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """The whole list, judged for one occupancy, in its order."""

    occupancy: Occupancy
    results: tuple[ControlResult, ...]

    @property
    def verdict(self) -> ControlState:
        """``block`` if any is red, else ``pending`` if any is waited for, else ``warn``
        if any is orange, else ``ok``."""
        states = {result.state for result in self.results}
        for state in (ControlState.BLOCK, ControlState.PENDING, ControlState.WARN):
            if state in states:
                return state
        return ControlState.OK

    @property
    def settled(self) -> bool:
        """Whether no control is waited for any more."""
        return all(result.state is not ControlState.PENDING for result in self.results)

    @property
    def first_block(self) -> ControlResult | None:
        """The first red control, in the list's order; ``None`` when none is."""
        return next((r for r in self.results if r.state is ControlState.BLOCK), None)


@dataclass(frozen=True, slots=True)
class PreflightRefused:
    """A blocking control of the pre-flight is not green: the start is refused, with its reason."""

    control: str
    reason: str

    @property
    def detail(self) -> str:
        """The words of the refusal, the console's and the dashboard's."""
        return f"pre-vol : {self.reason}"


def judge(
    controls: tuple[Control, ...],
    inputs: PreflightInputs,
    occupancy: Occupancy,
    *,
    began: Monotonic | None = None,
) -> PreflightReport:
    """Every control for a start that puts ``occupancy`` on board.

    ``began``: when the run under way started, whose controls are pending
    until their wait is over; ``None``, which is what an arming asks: nothing
    is waited for, a control that is not green now is red or orange.
    """
    return PreflightReport(
        occupancy, tuple(_result(control, inputs, occupancy, began) for control in controls)
    )


def _result(
    control: Control, inputs: PreflightInputs, occupancy: Occupancy, began: Monotonic | None
) -> ControlResult:
    severity = control.on_board if occupancy is Occupancy.OCCUPIED else control.bench
    if severity is None:
        return ControlResult(control.id, control.label, ControlState.OK, NOT_AT_THE_BENCH)
    found = control.check(inputs)
    match found:
        case Ok(detail):
            return ControlResult(control.id, control.label, ControlState.OK, detail)
        case Err(finding):
            waited = began is not None and elapsed(began, inputs.at) < control.max_duration
            return ControlResult(
                control.id,
                control.label,
                ControlState.PENDING if waited else _state_of(finding, severity),
                finding.reason,
            )
    raise assert_never(found)


def _state_of(finding: Finding, severity: Severity) -> ControlState:
    match finding:
        case Unjudged():
            return ControlState.WARN
        case Failed():
            return ControlState.BLOCK if severity is Severity.BLOCK else ControlState.WARN
    raise assert_never(finding)


def refusal_of(report: PreflightReport) -> PreflightRefused | None:
    """Why ``report`` refuses a start: its first red control; ``None`` when none is red."""
    first = report.first_block
    return None if first is None else PreflightRefused(first.id, first.detail)


def ecg_window(since: Monotonic | None, snapshot: TelemetrySnapshot) -> Monotonic | None:
    """Since when the heart rate has been fresh and ``good``, after ``snapshot``; ``None``: not now.

    Judged on every tick's snapshot: a sample that is not usable, or none
    fresh, breaks the window, and the next good one starts it again.
    """
    sample = snapshot.heart_rate
    if sample is None or snapshot.heart_rate_is_stale or sample.usable_bpm is None:
        return None
    return snapshot.at if since is None else since


OCCUPANCY_WORDS: Final[Mapping[Occupancy, str]] = MappingProxyType(
    {Occupancy.BENCH: "banc", Occupancy.OCCUPIED: "personne a bord"}
)


def describe_run(report: PreflightReport) -> str:
    """One line for the logbook and the log: the verdict, then each control that is not green."""
    head = f"pre-vol {report.verdict.value} ({OCCUPANCY_WORDS[report.occupancy]})"
    shown = [
        f"{result.id} {result.state.value} ({result.detail})"
        for result in report.results
        if result.state is not ControlState.OK
    ]
    if not shown:
        return f"{head} : {len(report.results)} controles verts"
    return f"{head} : " + " ; ".join(shown)


# =========================================================================
# Running it
# =========================================================================

type BatteryRead = Callable[[], Awaitable[Percent | None]]
"""Reads the BITalino's battery at rest. Pauses the acquisition for as long as it takes."""


@final
class PreflightMonitor:
    """The pre-flight of one console: its runs, the facts only it keeps, and the verdicts.

    A run starts with the console, whenever the console comes back to rest
    after a session (so before each session) and when the operator asks for
    one (:meth:`rerun`). It first reads the BITalino's battery, at rest, once
    the BITalino is connected (or after :data:`BATTERY_WAIT` without it); its
    controls are waited for from that read on, and the run ends when none is
    pending any more: it is then written down and kept as the last
    (:attr:`last`).

    Mutable, owned by the event loop: :meth:`observe` is called by every
    control tick, :meth:`step` by a task of its own, the reads by the page and
    the arming gate. Only :meth:`step` waits, for the battery.
    """

    __slots__ = (
        "_battery",
        "_began",
        "_clock",
        "_controls",
        "_due",
        "_ecg_since",
        "_facts",
        "_last",
        "_read_battery",
        "_record",
        "_resting",
        "_summary",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        facts: Callable[[], PreflightInputs],
        read_battery: BatteryRead,
        summary: Occupancy,
        record: Callable[[PreflightReport], None],
        controls: tuple[Control, ...] = CONTROLS,
    ) -> None:
        """``facts``: what the console holds now, battery and ECG window aside (memory
        only). ``summary``: the occupancy a run is written down and announced for, the
        most demanding start this console can make. ``record``: writes a completed run."""
        self._clock: Clock = clock
        self._facts: Callable[[], PreflightInputs] = facts
        self._read_battery: BatteryRead = read_battery
        self._summary: Occupancy = summary
        self._record: Callable[[PreflightReport], None] = record
        self._controls: tuple[Control, ...] = controls
        # The run under way, from its start; None between two runs.
        self._began: Monotonic | None = clock.monotonic()
        # Whether that run has still to read the battery.
        self._due: bool = True
        self._battery: BatteryReading | None = None
        self._ecg_since: Monotonic | None = None
        self._resting: bool = True
        self._last: PreflightReport | None = None

    @property
    def running(self) -> bool:
        """Whether a run is under way."""
        return self._began is not None

    @property
    def last(self) -> PreflightReport | None:
        """The last completed run; ``None`` before the first."""
        return self._last

    @property
    def battery(self) -> Percent | None:
        """The battery the last read found; ``None`` before one, or when it could not say."""
        reading = self._battery
        return None if reading is None else reading.percent

    def rerun(self) -> None:
        """Start a run now: the battery is read again, every control is waited for again."""
        self._began = self._clock.monotonic()
        self._due = True

    def observe(self, snapshot: TelemetrySnapshot) -> None:
        """One tick: the ECG window, and a new run when the console has come back to rest."""
        self._ecg_since = ecg_window(self._ecg_since, snapshot)
        resting = snapshot.mode is RunMode.REPOS
        if resting and not self._resting:
            self.rerun()
        self._resting = resting

    async def step(self) -> None:
        """Read the battery when the run under way asks for it, then close the run once settled."""
        began = self._began
        if began is None:
            return
        if self._due:
            if not self._resting:
                return
            connected = self._facts().bitalino_connected
            if not connected and elapsed(began, self._clock.monotonic()) < BATTERY_WAIT:
                return
            percent = await self._read_battery() if connected else None
            now = self._clock.monotonic()
            self._battery = BatteryReading(percent, now)
            self._due = False
            # The controls are waited for from here: the ECG starts again after the read.
            self._began = now
            return
        report = self.report(self._summary)
        if not report.settled:
            return
        self._began = None
        self._last = report
        self._record(report)

    def inputs(self) -> PreflightInputs:
        """What the controls read now."""
        return replace(self._facts(), battery=self._battery, ecg_good_since=self._ecg_since)

    def report(self, occupancy: Occupancy) -> PreflightReport:
        """The list now, for the page: a run under way shows what it still waits for."""
        return judge(self._controls, self.inputs(), occupancy, began=self._began)

    def refusal(self, occupancy: Occupancy) -> PreflightRefused | None:
        """The arming gate: the first blocking control that is not green now, or ``None``."""
        return refusal_of(judge(self._controls, self.inputs(), occupancy))


# =========================================================================
# The STO control
# =========================================================================


@dataclass(frozen=True, slots=True, kw_only=True)
class StoNotReady:
    """A positive ready observation is required before arming or acknowledging."""

    state: StoState
    detail: str


@dataclass(frozen=True, slots=True)
class StoBridgedOnBoard:
    """Under a declared STO bridge only a bench session, with nobody on board, arms.

    A programme (a person on board by definition), a manual session with a
    person on board, and a check that does not say who is on board are
    refused, whatever the medical parameters and the enabled modes.
    """


BRIDGED_BENCH_ONLY: Final[str] = (
    "chaine STO pontee (declaree), aucun arret d'urgence materiel : seule une seance "
    "manuelle de banc, capsule vide ou lestee, aucune personne a bord, peut demarrer"
)
"""The words of :class:`StoBridgedOnBoard` and of a BRIDGED reading, for the console
and the dashboard."""


@dataclass(frozen=True, slots=True)
class StoBridgedRemoteLaunch:
    """Under a declared STO bridge no programme launched from the dashboard arms.

    Its own refusal, apart from :class:`StoBridgedOnBoard`: a programme puts a
    person on board by definition. A ballasted bench session launched from the
    dashboard (contract 1.9, ANH-250) is not one: nobody is on board, and it
    passes the gates of a bench start at the console.
    """


BRIDGED_REMOTE_LAUNCH: Final[str] = (
    "STO ponte : programme lance depuis le site refuse, seule une seance de banc lestee "
    "peut etre lancee du site"
)
"""The words of :class:`StoBridgedRemoteLaunch`, for the console and the dashboard."""


def check_sto_ready(reading: StoReading) -> StoNotReady | None:
    """Pure STO-ready control, judged apart from the list above."""
    if sto_ready(reading):
        return None
    return StoNotReady(state=reading.state, detail=reading.detail)


def check_sto_for_start(
    reading: StoReading, occupancy: Occupancy | None
) -> StoNotReady | StoBridgedOnBoard | None:
    """The STO control of an arming: READY, or a declared bridge with nobody on board.

    ``occupancy`` is who the start puts on board: ``OCCUPIED`` for a
    programme, ``None`` for a check that does not know it. Any other reading
    gets :func:`check_sto_ready`'s answer, whatever the occupancy.
    """
    if not sto_bridged(reading):
        return check_sto_ready(reading)
    return None if occupancy is Occupancy.BENCH else StoBridgedOnBoard()


def describe_sto_refusal(refusal: StoNotReady) -> str:
    """The same actionable words for the console and dashboard, without accents."""
    match refusal.state:
        case StoState.OPEN:
            return "chaine STO ouverte : retablir la chaine de securite"
        case StoState.UNKNOWN:
            return "etat STO inconnu : retablir une lecture fiable de la chaine de securite"
        case StoState.NOT_CONFIGURED:
            return "lecture STO non configuree : configurer la source de la chaine de securite"
        case StoState.BRIDGED:
            return BRIDGED_BENCH_ONLY
        case StoState.READY:
            return "chaine STO prete"
    raise assert_never(refusal.state)

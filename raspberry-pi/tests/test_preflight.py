"""ANH-142: the pre-flight list, its runs, and the arming gate it feeds.

Every control is judged green and not green, in its order; a run waits for a
control until its longest wait and no longer; the bench has its own rules
(warnings where a person on board is refused); the runtime refuses an arming
on the first red blocking control and lets a warning through; a completed run
is recorded once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Final, cast

import pytest

from src.clock import ManualClock
from src.health_system import Celsius
from src.link_state import NOT_CONFIGURED, LinkState, LinkStatus
from src.motor.drive import DriveState, describe_fault
from src.result import Err, Ok, Result, is_ok
from src.training.preflight import (
    BATTERY_MAX_AGE,
    BATTERY_WAIT,
    CONTROLS,
    NOT_AT_THE_BENCH,
    BatteryReading,
    Control,
    ControlResult,
    ControlState,
    Failed,
    Finding,
    PreflightInputs,
    PreflightMonitor,
    PreflightRefused,
    PreflightReport,
    Severity,
    describe_run,
    ecg_window,
    judge,
    refusal_of,
)
from src.training.runtime import RecordStorageLow, Subject, TrainingRuntime
from src.training.types import (
    HeartRateSample,
    Occupancy,
    RunMode,
    SafetyAction,
    SafetyVerdict,
    SignalQuality,
    SpeedView,
    TelemetrySnapshot,
)
from src.units import Bpm, Monotonic, MotorRpm, Percent, RawRegister, Seconds, UnixMillis
from src.wall_clock import Synchronised, Unreadable, Unsynchronised
from tests.sto_support import ready_sto
from tests.test_runtime import (
    GEOMETRY,
    LIMITS,
    OPERATOR,
    FakeDrive,
    _profile,  # pyright: ignore[reportPrivateUsage]  # the shared rig builders
    _program,  # pyright: ignore[reportPrivateUsage]
    _safety_for,  # pyright: ignore[reportPrivateUsage]
)

NOW: Final[Monotonic] = Monotonic(1000.0)
CHARGED: Final[Percent] = Percent(80.0)


def _base() -> TelemetrySnapshot:
    """A real snapshot of a runtime at rest, its drive read a moment ago, a good ECG."""
    clock = ManualClock(NOW, UnixMillis(1_700_000_000_000))
    runtime = TrainingRuntime(
        clock=clock,
        sto=ready_sto(),
        drive=FakeDrive(clock),
        geometry=GEOMETRY,
        limits=LIMITS,
        safety=_safety_for(_profile()),
    )
    return replace(
        runtime.snapshot(),
        at=NOW,
        drive_state=DriveState.SWITCH_ON_DISABLED,
        drive_status_age=Seconds(0.3),
        heart_rate=HeartRateSample(bpm=Bpm(72), quality=SignalQuality.GOOD, seq=4, at=NOW),
        heart_rate_age=Seconds(0.5),
    )


BASE: Final[TelemetrySnapshot] = _base()
REACHABLE: Final[LinkStatus] = LinkStatus(LinkState.REACHABLE, "", Seconds(1.0))


def _green(**changes: object) -> PreflightInputs:
    """Every control green for a person on board, but the software version (never judged)."""
    inputs = PreflightInputs(
        at=NOW,
        snapshot=BASE,
        attested=True,
        bitalino_connected=True,
        bitalino_streaming=True,
        wall_clock=Synchronised(estimated_error=Seconds(0.01), max_error=Seconds(0.2)),
        record_storage=None,
        link=REACHABLE,
        medical_refusal=None,
        medical_version="med-1",
        software_version="pi-1.0.0",
        incidents_open_high=0,
        cpu_temp_c=Celsius(48.0),
        records_pending=0,
        battery=BatteryReading(Percent(80.0), Monotonic(NOW - 60.0)),
        ecg_good_since=Monotonic(NOW - 31.0),
    )
    return replace(inputs, **changes)  # type: ignore[arg-type]  # keyword pass-through


def _result(
    inputs: PreflightInputs, control: str, occupancy: Occupancy = Occupancy.OCCUPIED
) -> ControlResult:
    """One control of the list, judged."""
    report = judge(CONTROLS, inputs, occupancy)
    return next(result for result in report.results if result.id == control)


def _snapshot(**changes: object) -> TelemetrySnapshot:
    return replace(BASE, **changes)  # type: ignore[arg-type]  # keyword pass-through


# =========================================================================
# The list
# =========================================================================


def test_the_list_is_in_its_order_and_says_every_control_once() -> None:
    ids = [control.id for control in CONTROLS]
    assert ids == [
        "drive_link",
        "drive_state",
        "estop",
        "verdict",
        "disk",
        "clock",
        "contract",
        "medical",
        "incidents",
        "bitalino",
        "ecg",
        "temperature",
        "cloud",
        "records",
        "software",
    ]
    # EX-6: no run waits longer than 45 s, the long ECG window included.
    assert max(control.max_duration for control in CONTROLS) < 45.0
    # The warnings never block, the bench downgrades only what the pilot decided.
    severities = {c.id: (c.on_board, c.bench) for c in CONTROLS}
    assert severities["clock"] == (Severity.BLOCK, Severity.WARN)
    assert severities["bitalino"] == (Severity.BLOCK, Severity.WARN)
    assert severities["ecg"] == (Severity.BLOCK, None)
    assert severities["medical"] == (Severity.BLOCK, None)
    # ANH-145 EX-4: a serious incident refuses a person on board, warns at the bench.
    assert severities["incidents"] == (Severity.BLOCK, Severity.WARN)
    for blocking in ("drive_link", "drive_state", "estop", "verdict", "disk", "contract"):
        assert severities[blocking] == (Severity.BLOCK, Severity.BLOCK)
    for warning in ("temperature", "cloud", "records", "software"):
        assert severities[warning] == (Severity.WARN, Severity.WARN)


def test_all_green_but_the_unjudged_version_lets_every_start_through() -> None:
    for occupancy in Occupancy:
        report = judge(CONTROLS, _green(), occupancy)
        assert report.first_block is None
        assert report.verdict is ControlState.WARN
        assert refusal_of(report) is None
        warned = [r.id for r in report.results if r.state is not ControlState.OK]
        assert warned == ["software"]


@dataclass(frozen=True, slots=True)
class Case:
    control: str
    inputs: PreflightInputs
    reason: str


CASES: Final[tuple[Case, ...]] = (
    Case("drive_link", _green(snapshot=_snapshot(drive_status_age=None)), "variateur injoignable"),
    Case(
        "drive_link",
        _green(snapshot=_snapshot(drive_status_age=Seconds(9.0))),
        "variateur injoignable",
    ),
    Case(
        "drive_state",
        _green(snapshot=_snapshot(drive_state=DriveState.COMM_LOST)),
        "etat du variateur inconnu",
    ),
    Case(
        "drive_state",
        _green(snapshot=_snapshot(fault=describe_fault(RawRegister(9)))),
        "variateur en defaut (",
    ),
    Case(
        "drive_state",
        _green(snapshot=_snapshot(drive_state=DriveState.FAULT)),
        "variateur en defaut (?)",
    ),
    Case(
        "drive_state",
        _green(snapshot=_snapshot(drive_state=DriveState.OPERATION_ENABLED)),
        "variateur en marche",
    ),
    Case("drive_state", _green(snapshot=_snapshot(mode=RunMode.ARRET)), "seance en cours"),
    Case(
        "drive_state",
        _green(
            snapshot=_snapshot(
                measured=SpeedView.from_motor_rpm(
                    MotorRpm(40),
                    ratio=GEOMETRY.ratio,
                    radius=GEOMETRY.radius,
                    nominal_rpm=GEOMETRY.nominal_rpm,
                    base_hz=GEOMETRY.base_hz,
                )
            )
        ),
        "variateur en marche",
    ),
    Case("estop", _green(attested=False), "arret d'urgence non atteste"),
    Case(
        "verdict",
        _green(
            snapshot=_snapshot(
                safety=SafetyVerdict(
                    action=SafetyAction.QUICK_STOP,
                    rule="estop",
                    detail="x",
                    latched=True,
                    since=NOW,
                )
            )
        ),
        "verdict estop en cours",
    ),
    Case("disk", _green(record_storage="espace disque insuffisant"), "espace disque insuffisant"),
    Case("clock", _green(wall_clock=Unsynchronised()), "horloge NON FIABLE"),
    Case("clock", _green(wall_clock=Unreadable("x")), "horloge NON VERIFIEE"),
    Case(
        "clock",
        _green(wall_clock=Synchronised(estimated_error=Seconds(60.0), max_error=Seconds(60.0))),
        "horloge : erreur jusqu'a 60.0 s, 5 s au plus",
    ),
    Case(
        "contract",
        _green(link=LinkStatus(LinkState.INCOMPATIBLE, "contrat 2.0", None)),
        "contrat refuse par le tableau de bord : contrat 2.0",
    ),
    Case("medical", _green(medical_refusal="parametres non signes"), "parametres non signes"),
    Case("incidents", _green(incidents_open_high=2), "2 incident(s) grave(s)"),
    Case("bitalino", _green(bitalino_connected=False), "BITalino injoignable"),
    Case("bitalino", _green(battery=None), "batterie BITalino pas encore lue"),
    Case("bitalino", _green(battery=BatteryReading(None, NOW)), "batterie BITalino illisible"),
    Case(
        "bitalino",
        _green(battery=BatteryReading(Percent(80.0), Monotonic(NOW - BATTERY_MAX_AGE - 60.0))),
        "batterie BITalino lue il y a 31 min",
    ),
    Case(
        "bitalino",
        _green(battery=BatteryReading(Percent(20.0), NOW)),
        "batterie BITalino 20 % < 30 %",
    ),
    Case("ecg", _green(ecg_good_since=None), "qualite ECG good : 30 s"),
    Case(
        "ecg",
        _green(ecg_good_since=None, snapshot=_snapshot(heart_rate=None)),
        "qualite ECG sans mesure recente",
    ),
    Case(
        "ecg",
        _green(ecg_good_since=Monotonic(NOW - 12.0)),
        "qualite ECG good depuis 12 s, 30 s continues requises",
    ),
    Case("temperature", _green(cpu_temp_c=72.4), "Pi a 72 °C, au-dessus de 70 °C"),
    Case(
        "cloud",
        _green(link=LinkStatus(LinkState.UNREACHABLE, "delai", None)),
        "tableau de bord injoignable (delai) : la seance reste possible",
    ),
    Case("records", _green(records_pending=3), "3 enregistrement(s)"),
)


@pytest.mark.parametrize("case", CASES, ids=[f"{c.control}-{i}" for i, c in enumerate(CASES)])
def test_every_control_says_why_it_is_not_green(case: Case) -> None:
    assert _result(_green(), case.control).state is ControlState.OK
    result = _result(case.inputs, case.control)
    assert result.state is not ControlState.OK
    assert case.reason in result.detail, result.detail


def test_the_green_details_say_what_was_found() -> None:
    green = {r.id: r.detail for r in judge(CONTROLS, _green(), Occupancy.OCCUPIED).results}
    assert green["drive_link"].startswith("variateur lu il y a 0.3 s")
    assert green["drive_state"] == "variateur a l'arret, sans defaut (SWITCH_ON_DISABLED)"
    assert green["bitalino"] == "BITalino joignable, batterie 80 %"
    assert green["ecg"] == "qualite ECG good depuis 31 s"
    assert green["clock"].startswith("horloge synchronisee")
    assert green["medical"].startswith("parametres medicaux med-1 signes")
    assert green["temperature"] == "Pi a 48 °C"


def test_what_the_server_cannot_judge_is_orange_never_red() -> None:
    unannounced = _green(
        incidents_open_high=None,
        cpu_temp_c=None,
        records_pending=None,
        link=LinkStatus(LinkState.WAITING, "", None),
    )
    for occupancy in Occupancy:
        report = judge(CONTROLS, unannounced, occupancy)
        states = {r.id: r.state for r in report.results}
        for control in ("incidents", "temperature", "records", "contract", "software"):
            assert states[control] is ControlState.WARN, control
        assert report.first_block is None


def test_a_clock_that_cannot_be_asked_refuses_a_person_on_board_and_warns_at_the_bench() -> None:
    """EX-2: an error known and under 5 s with a person on board; unknown is not known."""
    unknown = _green(wall_clock=None)
    on_board = _result(unknown, "clock")
    assert on_board.state is ControlState.BLOCK
    assert on_board.detail == "horloge non verifiable sur ce systeme : son erreur est inconnue"
    assert _result(unknown, "clock", Occupancy.BENCH).state is ControlState.WARN


def test_with_the_simulated_drive_a_clock_that_cannot_be_asked_only_warns() -> None:
    """No real motor, nobody in a simulated capsule: the demo and the browser tests on a Mac."""
    simulated = _green(wall_clock=None, simulated_drive=True)
    for occupancy in Occupancy:
        result = _result(simulated, "clock", occupancy)
        assert result.state is ControlState.WARN
        assert result.detail.endswith("admise avec le variateur simule")
    # Only a clock that cannot be asked: one that a time service no longer keeps still blocks.
    unkept = _result(_green(wall_clock=Unsynchronised(), simulated_drive=True), "clock")
    assert unkept.state is ControlState.BLOCK


def test_a_console_without_a_dashboard_has_nothing_to_send_and_no_contract_to_keep() -> None:
    report = judge(CONTROLS, _green(link=NOT_CONFIGURED, records_pending=None), Occupancy.BENCH)
    states = {r.id: r.state for r in report.results}
    assert states["contract"] is states["cloud"] is states["records"] is ControlState.OK


def test_a_contract_unknown_while_the_dashboard_is_silent_is_orange() -> None:
    for state in (LinkState.UNREACHABLE, LinkState.KEY_REFUSED, LinkState.SERVER_ERROR):
        result = _result(_green(link=LinkStatus(state, "", None)), "contract")
        assert result.state is ControlState.WARN
        assert result.detail.startswith("contrat non verifie")


# =========================================================================
# The bench and a person on board
# =========================================================================


def test_at_the_bench_a_clock_a_missing_bitalino_and_a_low_battery_only_warn() -> None:
    """The pilot's decision of 29 October 2026: no danger for a person, so no block."""
    low = _green(
        wall_clock=Unsynchronised(),
        bitalino_connected=False,
        ecg_good_since=None,
        medical_refusal="parametres non signes",
    )
    bench = judge(CONTROLS, low, Occupancy.BENCH)
    states = {r.id: r for r in bench.results}
    assert states["clock"].state is ControlState.WARN
    assert states["bitalino"].state is ControlState.WARN
    assert states["ecg"].state is ControlState.OK
    assert states["ecg"].detail == NOT_AT_THE_BENCH
    assert states["medical"].detail == NOT_AT_THE_BENCH
    assert refusal_of(bench) is None
    on_board = refusal_of(judge(CONTROLS, low, Occupancy.OCCUPIED))
    assert on_board is not None
    assert on_board.control == "clock"


def test_every_other_blocking_control_still_blocks_the_bench() -> None:
    full = _green(record_storage="espace disque insuffisant : 100 Mo libres")
    refused = refusal_of(judge(CONTROLS, full, Occupancy.BENCH))
    assert refused == PreflightRefused("disk", "espace disque insuffisant : 100 Mo libres")
    assert refused is not None
    assert refused.detail == "pre-vol : espace disque insuffisant : 100 Mo libres"


def test_the_first_red_control_in_the_list_is_the_reason() -> None:
    both = _green(battery=BatteryReading(Percent(20.0), NOW), ecg_good_since=None)
    refused = refusal_of(judge(CONTROLS, both, Occupancy.OCCUPIED))
    assert refused is not None
    assert refused.detail == "pre-vol : batterie BITalino 20 % < 30 %"


def test_the_battery_floor_is_the_configured_one() -> None:
    battery = BatteryReading(Percent(35.5), NOW)
    assert _result(_green(battery=battery), "bitalino").state is ControlState.OK
    refused = _result(_green(battery=battery, min_battery=Percent(40.0)), "bitalino")
    assert refused.detail == "batterie BITalino 35 % < 40 %"


# =========================================================================
# A run waits, then judges
# =========================================================================


def test_a_control_is_pending_until_its_wait_is_over_then_red() -> None:
    late = _green(ecg_good_since=Monotonic(NOW - 12.0))
    waiting = judge(CONTROLS, late, Occupancy.OCCUPIED, began=Monotonic(NOW - 20.0))
    ecg = next(r for r in waiting.results if r.id == "ecg")
    assert ecg.state is ControlState.PENDING
    assert waiting.verdict is ControlState.PENDING
    assert not waiting.settled
    timed_out = judge(CONTROLS, late, Occupancy.OCCUPIED, began=Monotonic(NOW - 44.0))
    assert next(r for r in timed_out.results if r.id == "ecg").state is ControlState.BLOCK
    assert timed_out.settled
    assert timed_out.verdict is ControlState.BLOCK


def test_a_control_with_no_wait_is_judged_at_once_even_in_a_run() -> None:
    unattested = judge(CONTROLS, _green(attested=False), Occupancy.BENCH, began=NOW)
    assert next(r for r in unattested.results if r.id == "estop").state is ControlState.BLOCK


def test_a_green_list_has_the_ok_verdict() -> None:
    passing: Callable[[PreflightInputs], Result[str, Finding]] = lambda _: Ok("vert")  # noqa: E731
    only = (
        Control(
            id="door",
            label="Porte fermee",
            on_board=Severity.BLOCK,
            bench=Severity.BLOCK,
            max_duration=Seconds(5.0),
            check=passing,
        ),
    )
    report = judge(only, _green(), Occupancy.OCCUPIED)
    assert report.verdict is ControlState.OK
    assert describe_run(report) == "pre-vol ok (personne a bord) : 1 controles verts"


def test_a_future_hardware_control_is_one_more_entry() -> None:
    """EX-4: the list grows, the judging, the refusal and the record follow unchanged."""

    def harness(inputs: PreflightInputs) -> Result[str, Finding]:
        del inputs
        return Err(Failed("harnais non boucle"))

    extended = (
        *CONTROLS,
        Control(
            id="harness",
            label="Harnais boucle",
            on_board=Severity.BLOCK,
            bench=None,
            max_duration=Seconds(0.0),
            check=harness,
        ),
    )
    report = judge(extended, _green(), Occupancy.OCCUPIED)
    assert refusal_of(report) == PreflightRefused("harness", "harnais non boucle")
    assert refusal_of(judge(extended, _green(), Occupancy.BENCH)) is None
    assert "harness block (harnais non boucle)" in describe_run(report)


def test_the_record_of_a_run_names_what_was_not_green() -> None:
    report = judge(CONTROLS, _green(cpu_temp_c=75.0), Occupancy.BENCH)
    assert describe_run(report) == (
        "pre-vol warn (banc) : temperature warn (Pi a 75 °C, au-dessus de 70 °C : l'aerer) ; "
        "software warn (version pi-1.0.0 non jugee : aucune reference du serveur)"
    )


# =========================================================================
# The ECG window
# =========================================================================


def test_the_ecg_window_starts_at_the_first_good_sample_and_breaks_on_any_other() -> None:
    assert ecg_window(None, BASE) == NOW
    later = replace(BASE, at=Monotonic(NOW + 5.0))
    assert ecg_window(NOW, later) == NOW
    noisy = replace(
        BASE,
        heart_rate=HeartRateSample(bpm=None, quality=SignalQuality.NOISY, seq=5, at=NOW),
    )
    assert ecg_window(NOW, noisy) is None
    assert ecg_window(NOW, replace(BASE, heart_rate=None)) is None
    assert ecg_window(NOW, replace(BASE, heart_rate_age=Seconds(30.0))) is None


# =========================================================================
# The runs
# =========================================================================


@dataclass
class Console:
    """What the monitor reads and writes, as a test drives it."""

    clock: ManualClock = field(default_factory=lambda: ManualClock(NOW))
    facts: PreflightInputs = field(default_factory=_green)
    battery: Percent | None = CHARGED
    reads: int = 0
    recorded: list[PreflightReport] = field(default_factory=list[PreflightReport])
    hold: asyncio.Event | None = None
    """When set, a battery read waits for it: the read under way, as a test holds it."""

    async def read_battery(self) -> Percent | None:
        self.reads += 1
        if self.hold is not None:
            await self.hold.wait()
        return self.battery

    def inputs(self) -> PreflightInputs:
        return replace(self.facts, at=self.clock.monotonic())

    def monitor(
        self, summary: Occupancy = Occupancy.OCCUPIED, temperature: Celsius | None = None
    ) -> PreflightMonitor:
        async def measure() -> Celsius | None:
            return temperature

        return PreflightMonitor(
            clock=self.clock,
            facts=self.inputs,
            read_battery=self.read_battery,
            summary=summary,
            record=self.recorded.append,
            read_temperature=None if temperature is None else measure,
        )


def _running(monitor: PreflightMonitor) -> bool:
    """Read through a call, so mypy does not narrow one assertion into the next."""
    return monitor.running


def _battery(monitor: PreflightMonitor) -> Percent | None:
    """Read through a call, for the reason :func:`_running` gives."""
    return monitor.battery


def _last(monitor: PreflightMonitor) -> PreflightReport | None:
    """Read through a call, for the reason :func:`_running` gives."""
    return monitor.last


def _good(at: Monotonic) -> TelemetrySnapshot:
    """A tick at rest at ``at``, with a good heart-rate sample taken then."""
    sample = HeartRateSample(bpm=Bpm(72), quality=SignalQuality.GOOD, seq=4, at=at)
    return replace(BASE, at=at, heart_rate=sample)


def _tick(clock: ManualClock, monitor: PreflightMonitor, seconds: float) -> None:
    """``seconds`` of control ticks at rest, a good ECG in each."""
    for _ in range(round(seconds / 0.2)):
        clock.advance(Seconds(0.2))
        monitor.observe(_good(clock.monotonic()))


async def test_a_run_reads_the_battery_waits_for_the_ecg_and_is_recorded_once() -> None:
    console = Console()
    monitor = console.monitor()
    assert _running(monitor)
    assert _battery(monitor) is None
    await monitor.step()
    assert console.reads == 1
    assert _battery(monitor) == Percent(80.0)
    _tick(console.clock, monitor, 10.0)
    await monitor.step()
    report = monitor.report(Occupancy.OCCUPIED)
    assert next(r for r in report.results if r.id == "ecg").state is ControlState.PENDING
    assert console.recorded == []
    assert _last(monitor) is None
    _tick(console.clock, monitor, 21.0)
    await monitor.step()
    assert not _running(monitor)
    assert len(console.recorded) == 1
    last = _last(monitor)
    assert last is not None
    assert last.verdict is ControlState.WARN  # the software version, never judged
    assert last.occupancy is Occupancy.OCCUPIED
    await monitor.step()
    assert len(console.recorded) == 1
    # EX-6: battery read, then the 30 s ECG window: well under 45 s.
    assert console.clock.monotonic() - NOW < 45.0


async def test_a_run_waits_for_the_bitalino_then_reads_without_it() -> None:
    console = Console(facts=_green(bitalino_connected=False, bitalino_streaming=False))
    monitor = console.monitor(Occupancy.BENCH)
    await monitor.step()
    assert console.reads == 0
    console.clock.advance(BATTERY_WAIT)
    await monitor.step()
    assert console.reads == 0
    assert _battery(monitor) is None
    assert _running(monitor)
    console.clock.advance(Seconds(11.0))
    await monitor.step()
    last = _last(monitor)
    assert last is not None
    bitalino = next(r for r in last.results if r.id == "bitalino")
    assert bitalino.state is ControlState.WARN  # the bench


@pytest.mark.parametrize("streaming", [True, False], ids=["nominal", "bitalino-absent"])
async def test_no_run_lasts_45_s(streaming: bool) -> None:
    """EX-6: the battery's wait and the ECG's run side by side, from the start of the run.

    Before, an absent BITalino was waited for 15 s, and the ECG 44 s after that.
    """
    facts = _green(bitalino_connected=streaming, bitalino_streaming=streaming)
    console = Console(facts=facts)
    monitor = console.monitor()
    for _ in range(44):
        console.clock.advance(Seconds(1.0))
        await monitor.step()
        if streaming:
            monitor.observe(_good(console.clock.monotonic()))
    assert not _running(monitor)
    assert console.clock.monotonic() - NOW < 45.0


async def test_a_battery_read_due_or_under_way_refuses_a_person_on_board() -> None:
    """The read stops the ECG: the gate must not arm on a reading and a window of before it."""
    console = Console()
    monitor = console.monitor()
    await monitor.step()
    _tick(console.clock, monitor, 31.0)
    await monitor.step()
    assert monitor.refusal(Occupancy.OCCUPIED) is None
    console.hold = asyncio.Event()
    monitor.rerun()
    # Due, not under way yet: no read runs, and the words do not say one does.
    due = PreflightRefused(
        "bitalino", "batterie BITalino a relire : des que le BITalino acquiert normalement"
    )
    assert monitor.refusal(Occupancy.OCCUPIED) == due
    assert monitor.refusal(Occupancy.BENCH) is None  # the bench is not held for it
    reading = asyncio.create_task(monitor.step())
    await asyncio.sleep(0)
    under_way = PreflightRefused(
        "bitalino", "batterie BITalino en cours de lecture : l'ECG est suspendu pendant ce temps"
    )
    assert monitor.refusal(Occupancy.OCCUPIED) == under_way
    assert monitor.refusal(Occupancy.BENCH) is None
    console.hold.set()
    await asyncio.wait_for(reading, 3.0)


async def test_a_battery_read_breaks_the_ecg_window_until_samples_of_after_it() -> None:
    console = Console(hold=asyncio.Event())
    monitor = console.monitor()
    _tick(console.clock, monitor, 1.0)
    assert monitor.inputs().ecg_good_since is not None
    reading = asyncio.create_task(monitor.step())
    await asyncio.sleep(0)
    assert monitor.inputs().ecg_good_since is None
    _tick(console.clock, monitor, 1.0)  # samples go on for a moment, the read holds the window
    assert monitor.inputs().ecg_good_since is None
    before = console.clock.monotonic()
    console.clock.advance(Seconds(2.0))
    assert console.hold is not None
    console.hold.set()
    await asyncio.wait_for(reading, 3.0)
    monitor.observe(_good(before))  # taken before the read ended: nothing
    assert monitor.inputs().ecg_good_since is None
    _tick(console.clock, monitor, 0.2)
    assert monitor.inputs().ecg_good_since == console.clock.monotonic()


async def test_the_pi_is_measured_by_the_preflight_itself() -> None:
    console = Console(facts=_green(cpu_temp_c=None))
    monitor = console.monitor(temperature=Celsius(72.4))
    assert _result(monitor.inputs(), "temperature").state is ControlState.WARN
    await monitor.step()
    measured = _result(monitor.inputs(), "temperature")
    assert measured.detail == "Pi a 72 °C, au-dessus de 70 °C : l'aerer"


async def test_the_battery_is_not_read_while_a_session_runs() -> None:
    console = Console()
    monitor = console.monitor()
    monitor.observe(replace(BASE, mode=RunMode.MANUEL))
    await monitor.step()
    assert console.reads == 0
    monitor.observe(BASE)
    # Back at rest: a new run, which reads the battery.
    assert _running(monitor)
    await monitor.step()
    assert console.reads == 1


async def test_a_rerun_reads_the_battery_again() -> None:
    console = Console()
    monitor = console.monitor()
    await monitor.step()
    _tick(console.clock, monitor, 45.0)
    await monitor.step()
    assert not _running(monitor)
    console.battery = Percent(20.0)
    monitor.rerun()
    assert _running(monitor)
    await monitor.step()
    assert console.reads == 2
    assert _battery(monitor) == Percent(20.0)
    refused = monitor.refusal(Occupancy.OCCUPIED)
    assert refused == PreflightRefused("bitalino", "batterie BITalino 20 % < 30 %")
    assert monitor.refusal(Occupancy.BENCH) is None


async def test_the_arming_gate_never_waits() -> None:
    console = Console()
    monitor = console.monitor()
    await monitor.step()
    # The run waits for the ECG; a start with a person on board is refused now.
    assert monitor.report(Occupancy.OCCUPIED).verdict is ControlState.PENDING
    refused = monitor.refusal(Occupancy.OCCUPIED)
    assert refused is not None
    assert refused.control == "ecg"
    assert monitor.refusal(Occupancy.BENCH) is None


# =========================================================================
# The runtime's arming gate
# =========================================================================


@dataclass
class Gate:
    """A pre-flight that answers what the test says, and remembers who asked."""

    answer: PreflightRefused | None = None
    asked: list[Occupancy] = field(default_factory=list[Occupancy])

    def __call__(self, occupancy: Occupancy) -> PreflightRefused | None:
        self.asked.append(occupancy)
        return self.answer


def _runtime(gate: Gate, storage: RecordStorageLow | None = None) -> TrainingRuntime:
    clock = ManualClock(NOW, UnixMillis(1_700_000_000_000))
    profile = _profile()
    runtime = TrainingRuntime(
        clock=clock,
        sto=ready_sto(),
        drive=FakeDrive(clock),
        geometry=GEOMETRY,
        limits=LIMITS,
        safety=_safety_for(profile),
        arming_gate=lambda: storage,
        preflight=gate,
    )
    assert is_ok(runtime.confirm_estop_wiring(OPERATOR))
    return runtime


async def test_arming_is_refused_on_a_red_blocking_control() -> None:
    refused = PreflightRefused("bitalino", "batterie BITalino 20 % < 30 %")
    gate = Gate(refused)
    runtime = _runtime(gate)
    assert await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(300)) == Err(refused)
    assert await runtime.start(
        _program(_profile()), Subject(subject_id="rider-1", operator=OPERATOR)
    ) == Err(refused)
    # A programme puts a person on board: it is judged so.
    assert gate.asked == [Occupancy.BENCH, Occupancy.OCCUPIED]


async def test_a_warning_does_not_prevent_the_start() -> None:
    gate = Gate(None)
    runtime = _runtime(gate)
    assert is_ok(await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(300)))
    assert gate.asked == [Occupancy.BENCH]


async def test_the_record_storage_is_said_before_the_preflight() -> None:
    gate = Gate(PreflightRefused("disk", "x"))
    storage = RecordStorageLow(free_bytes=1, required_bytes=500_000_000, where="data/records")
    runtime = _runtime(gate, storage)
    assert await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(300)) == Err(storage)
    assert gate.asked == []


def test_what_is_neither_said_nor_known_fails_loudly() -> None:
    """A link state, a result or a finding added later with no words for it is an error."""
    later = LinkStatus(cast("LinkState", "later"), "", None)
    with pytest.raises(AssertionError):
        judge(CONTROLS, _green(link=later), Occupancy.BENCH)

    def odd(inputs: PreflightInputs) -> Result[str, Finding]:
        del inputs
        return cast("Result[str, Finding]", "neither")

    def unknown(inputs: PreflightInputs) -> Result[str, Finding]:
        del inputs
        return Err(cast("Finding", "not-a-finding"))

    for check in (odd, unknown):
        entry = Control(
            id="odd",
            label="odd",
            on_board=Severity.BLOCK,
            bench=Severity.BLOCK,
            max_duration=Seconds(0.0),
            check=check,
        )
        with pytest.raises(AssertionError):
            judge((entry,), _green(), Occupancy.BENCH)

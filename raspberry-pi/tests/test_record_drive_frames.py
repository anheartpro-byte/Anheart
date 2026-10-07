"""ANH-191 EX-2: the drive's frames reach ``drive_frames.jsonl``, bounded and off the tick.

Three layers, each with its own cases:

* the bounded exchange log of the drive seam (:class:`~src.motor.observation.ExchangeLog`);
* the tap (:mod:`src.record.drive_tap`): a delegating wrapper for a backend that
  reports nothing itself, the exchange log for one that does, never both;
* the journal thread, which takes what waits at every cycle and writes it to the
  record that was open.

And the acceptance case on the REAL console in simulation: a drive fault
injected in a session is read back from ``drive_frames.jsonl``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from src import local_panel
from src.clock import Clock, ManualClock, RealClock
from src.local_config import load_local_config
from src.motor.acquisition import AcquisitionEvidence
from src.motor.atv320 import ATV320Drive
from src.motor.drive import (
    ACC_LOGICAL,
    CMD_LOGICAL,
    DEC_LOGICAL,
    ETA_LOGICAL,
    FAULT_BIT,
    HSP_LOGICAL,
    LCR_LOGICAL,
    LFRD_LOGICAL,
    LFT_FAULT_CODES,
    LFT_LOGICAL,
    LSP_LOGICAL,
    RFRD_LOGICAL,
    TFR_LOGICAL,
    CommTimeout,
    ControlWord,
    DriveBackend,
    DriveError,
    DriveFault,
    DriveLimits,
    DriveState,
    DriveStatus,
    EmergencyStopOutcome,
    RegisterMap,
)
from src.motor.observation import Drained, Exchange, ExchangeKind, ExchangeLog
from src.record.codec import Privacy, document, encode
from src.record.drive_tap import (
    FRAME_BACKLOG,
    CallKind,
    CallTap,
    Observation,
    Taken,
    Tapped,
    tap_drive,
)
from src.record.journal import Cause, Journal
from src.record.reader import read
from src.record.schema import DriveFrame, EventKind
from src.record.writer import FRAME
from src.result import Err, Ok, Result
from src.training.runtime import RuntimeState
from src.units import (
    Amperes,
    Hertz,
    Monotonic,
    MotorRpm,
    RawRegister,
    RegisterAddress,
    Seconds,
    StatusWord,
)
from tests.record_console_support import recorded_rig, set_target, start_bench, stop
from tests.record_journal_support import clock_at_start, closing, opened, record_of, session
from tests.test_failure_rig import BENCH_ENV
from tests.test_ftdi_link import FakeAltivar, ftdi_master

# =========================================================================
# The exchange log of the drive seam: bounded, and drained
# =========================================================================


def exchange(at: float, value: int = 0) -> Exchange:
    return Exchange(
        at=Monotonic(at),
        kind=ExchangeKind.READ,
        register=RegisterAddress(3201),
        value=RawRegister(value),
        ok=True,
        latency_ms=1.5,
        detail="read",
    )


def test_ex2_the_exchange_log_keeps_everything_when_no_bound_is_given() -> None:
    """The simulation's use, unchanged: no capacity, ``entries`` read at the end."""
    log = ExchangeLog(ManualClock())
    for index in range(5000):
        log.append(exchange(float(index)))
    assert len(log.entries) == 5000
    drained = log.drain()
    assert drained.refused == 0


def test_ex2_a_bounded_exchange_log_refuses_the_newest_and_counts_it() -> None:
    log = ExchangeLog(ManualClock(), capacity=3)
    for index in range(5):
        log.append(exchange(float(index), index))
    assert [entry.value for entry in log.entries] == [0, 1, 2], "the oldest are kept"
    drained = log.drain()
    assert drained == Drained(tuple(exchange(float(i), i) for i in range(3)), refused=2)
    assert log.entries == (), "draining empties the log"
    # Room again: the log goes on, and the count of the refused is never reset.
    log.append(exchange(9.0, 9))
    after = log.drain()
    assert after == Drained((exchange(9.0, 9),), refused=2)


def test_ex2_an_exchange_log_keeps_the_sdk_calls_unless_asked_for_registers_only() -> None:
    """Both positions of the switch, at the one place every exchange goes through."""
    whole = ExchangeLog(ManualClock())
    registers = ExchangeLog(ManualClock(), capacity=2, transport=False)
    for log in (whole, registers):
        sent = log.send(b"\x01\x03\x0c", lambda: 3)
        received = log.receive(lambda: b"\x01\x03")
        log.append(exchange(1.0, 7))
        assert (sent, received) == (3, b"\x01\x03"), "the SDK's call is made either way"
    assert [entry.kind for entry in whole.entries] == [
        ExchangeKind.SEND,
        ExchangeKind.RECEIVE_CHUNK,
        ExchangeKind.READ,
    ]
    assert [entry.kind for entry in registers.entries] == [ExchangeKind.READ]
    # What is not kept takes no room under the bound, and is not counted as lost.
    registers.append(exchange(2.0, 8))
    drained = registers.drain()
    assert (len(drained.entries), drained.refused) == (2, 0)


# =========================================================================
# The tap around a backend that reports nothing itself
# =========================================================================

STATUS: Final[DriveStatus] = DriveStatus(
    state=DriveState.OPERATION_ENABLED,
    status_word=StatusWord(0x0637),
    setpoint_echo_rpm=MotorRpm(150),
    output_rpm=MotorRpm(149),
    current=Amperes(1.26),
    fault=None,
    fault_code=RawRegister(0),
)

LIMITS: Final[DriveLimits] = DriveLimits(
    max_frequency=Hertz(60.0),
    high_speed=Hertz(50.0),
    low_speed=Hertz(0.0),
    acceleration=Seconds(3.0),
    deceleration=Seconds(2.5),
)

TIMEOUT: Final[DriveError] = CommTimeout(after=Seconds(0.5))


class Scripted:
    """A ``DriveBackend`` that answers what the test set, 25 ms after it was asked."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock: ManualClock = clock
        self.status: Result[DriveStatus, DriveError] = Ok(STATUS)
        self.limits: Result[DriveLimits, DriveError] = Ok(LIMITS)
        self.written: Result[None, DriveError] = Ok(None)
        self.emergency: EmergencyStopOutcome = EmergencyStopOutcome.ACKNOWLEDGED
        self.asked: list[str] = []

    def _answer(self, what: str) -> None:
        self.asked.append(what)
        self.clock.advance(Seconds(0.025))

    @property
    def acquisition_evidence(self) -> AcquisitionEvidence:
        return AcquisitionEvidence(possible_frames=7, address_proven=True)

    async def open(self) -> Result[None, DriveError]:
        self._answer("open")
        return self.written

    async def close(self) -> Result[None, DriveError]:
        self._answer("close")
        return self.written

    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        self._answer(f"command {word.name}")
        return self.written

    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        self._answer(f"speed {rpm}")
        return self.written

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        self._answer("read_status")
        return self.status

    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        self._answer("read_limits")
        return self.limits

    @property
    def emergency_budget(self) -> Seconds:
        return Seconds(1.25)

    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        self._answer(f"emergency {timeout}")
        return self.emergency


class Native(Scripted):
    """A backend that reports its own exchanges, like the ATV320 driver."""

    def __init__(self, clock: ManualClock) -> None:
        super().__init__(clock)
        self.log: ExchangeLog | None = None

    def observe_exchanges(self, log: ExchangeLog) -> None:
        self.log = log


def tapped(capacity: int = FRAME_BACKLOG) -> tuple[CallTap, Scripted, ManualClock]:
    clock = ManualClock(Monotonic(100.0))
    inner = Scripted(clock)
    return CallTap(inner, clock, capacity), inner, clock


def conforms(backend: DriveBackend) -> DriveBackend:
    """Typed identity: the tap IS a drive backend, for the runtime that is given it."""
    return backend


async def test_ex2_the_tap_hands_back_every_answer_unchanged_and_notes_the_call() -> None:
    tap, inner, _clock = tapped()
    drive = conforms(tap)
    assert isinstance(drive, DriveBackend)
    opened_link = await drive.open()
    limits = await drive.read_limits()
    commanded = await drive.write_command(ControlWord.ENABLE_OPERATION)
    written = await drive.write_speed(MotorRpm(150))
    status = await drive.read_status()
    zeroed = drive.emergency_disable_blocking(Seconds(1.25))
    closed = await drive.close()
    # Each answer is the drive's own object, handed back as it came.
    assert opened_link is inner.written
    assert limits is inner.limits
    assert commanded is inner.written
    assert written is inner.written
    assert status is inner.status
    assert zeroed is EmergencyStopOutcome.ACKNOWLEDGED
    assert closed is inner.written
    assert drive.emergency_budget == Seconds(1.25)
    assert drive.acquisition_evidence == AcquisitionEvidence(possible_frames=7, address_proven=True)
    assert inner.asked == [
        "open",
        "read_limits",
        "command ENABLE_OPERATION",
        "speed 150",
        "read_status",
        "emergency 1.25",
        "close",
    ]

    taken = tap.take()
    assert taken.lost == 0
    seen = [(o.kind, o.register, o.value, o.ok) for o in taken.observations]
    assert seen == [
        ("open", None, None, True),
        (
            "read_limits",
            (TFR_LOGICAL, HSP_LOGICAL, LSP_LOGICAL, ACC_LOGICAL, DEC_LOGICAL),
            (600, 500, 0, 30, 25),
            True,
        ),
        ("command", CMD_LOGICAL, int(ControlWord.ENABLE_OPERATION), True),
        ("speed", LFRD_LOGICAL, 150, True),
        (
            "read_status",
            (ETA_LOGICAL, LFRD_LOGICAL, RFRD_LOGICAL, LCR_LOGICAL, LFT_LOGICAL),
            (0x0637, 150, 149, 13, 0),
            True,
        ),
        ("emergency_zero", LFRD_LOGICAL, 0, True),
        ("close", None, None, True),
    ]
    # When each call began on the console's clock, and how long the drive took.
    assert [o.at for o in taken.observations] == pytest.approx(
        [100.0 + 0.025 * index for index in range(7)]
    )
    assert [o.latency_ms for o in taken.observations] == pytest.approx([25.0] * 7)
    again = tap.take()
    assert again == Taken((), 0), "taken once"


async def test_ex2_a_call_the_drive_refuses_is_noted_as_refused_with_nothing_invented() -> None:
    tap, inner, _clock = tapped()
    inner.status = Err(TIMEOUT)
    inner.limits = Err(TIMEOUT)
    inner.written = Err(TIMEOUT)
    inner.emergency = EmergencyStopOutcome.NOTHING_SENT
    status = await tap.read_status()
    limits = await tap.read_limits()
    written = await tap.write_speed(MotorRpm(60))
    commanded = await tap.write_command(ControlWord.SHUTDOWN)
    opened_link = await tap.open()
    closed = await tap.close()
    zeroed = tap.emergency_disable_blocking(Seconds(0.5))
    assert status is inner.status
    assert limits is inner.limits
    assert (written, commanded, opened_link, closed) == (inner.written,) * 4
    assert zeroed is EmergencyStopOutcome.NOTHING_SENT
    taken = tap.take()
    seen = [(o.kind, o.register, o.value, o.ok) for o in taken.observations]
    assert seen == [
        ("read_failed", None, None, False),
        ("read_limits", None, None, False),
        ("speed", LFRD_LOGICAL, 60, False),
        ("command", CMD_LOGICAL, int(ControlWord.SHUTDOWN), False),
        ("open", None, None, False),
        ("close", None, None, False),
        ("emergency_zero", LFRD_LOGICAL, 0, False),
    ]
    assert {kind.value for kind in CallKind} >= {kind for kind, *_ in seen}


async def test_ex2_the_tap_is_bounded_what_it_refuses_is_counted_and_the_drive_never_waits() -> (
    None
):
    tap, inner, _clock = tapped(capacity=3)
    for _ in range(10):
        answered = await tap.read_status()
        assert answered is inner.status, "the answer comes back all the same"
    taken = tap.take()
    assert len(taken.observations) == 3, "the bound: the newest are refused"
    assert taken.lost == 7
    written = await tap.write_speed(MotorRpm(0))
    assert written is inner.written
    after = tap.take()
    assert [o.kind for o in after.observations] == ["speed"]
    assert after.lost == 7, "counted since startup, never reset"


async def test_ex2_an_observation_that_cannot_be_built_is_lost_and_never_raised() -> None:
    """A status the tap cannot put into registers costs its own line, not the tick."""
    tap, inner, _clock = tapped()
    unsayable = DriveStatus(
        state=DriveState.OPERATION_ENABLED,
        status_word=StatusWord(0x0637),
        setpoint_echo_rpm=MotorRpm(150),
        output_rpm=MotorRpm(149),
        current=Amperes(float("nan")),
        fault=None,
    )
    inner.status = Ok(unsayable)
    answered = await tap.read_status()
    assert answered is inner.status, (
        "the runtime gets the drive's answer whatever the tap makes of it"
    )
    taken = tap.take()
    assert taken == Taken((), lost=1)


def test_ex2_a_backend_that_reports_its_own_exchanges_is_not_wrapped() -> None:
    """The native driver: nothing is put between it and the runtime, and no call is made up."""
    clock = ManualClock(Monotonic(100.0))
    native = Native(clock)
    listened = tap_drive(native, clock, capacity=2, sdk_frames=True)
    assert listened.backend is native
    log = native.log
    assert log is not None
    nothing = listened.source()
    assert nothing == Taken((), 0)
    log.append(exchange(100.5, 0x0637))
    log.append(
        Exchange(
            at=Monotonic(100.6),
            kind=ExchangeKind.SEND,
            register=None,
            value=None,
            ok=True,
            latency_ms=0.2,
            detail="SDK transport call",
            raw_hex="0103",
        )
    )
    log.append(exchange(100.7, 1))
    taken = listened.source()
    assert taken.lost == 1, "the exchange log is bounded too"
    assert taken.observations == (
        Observation(
            at=Monotonic(100.5),
            kind="modbus_read",
            register=3201,
            value=0x0637,
            ok=True,
            latency_ms=1.5,
        ),
        Observation(
            at=Monotonic(100.6),
            kind="modbus_send",
            register=None,
            value=None,
            ok=True,
            latency_ms=0.2,
            raw_hex="0103",
        ),
    )


def test_ex2_by_default_a_native_backend_gives_its_register_transactions_only() -> None:
    """The switch off: the SDK's transport calls are not kept, and take no room."""
    clock = ManualClock(Monotonic(100.0))
    native = Native(clock)
    listened = tap_drive(native, clock, capacity=2)
    log = native.log
    assert log is not None
    log.append(exchange(100.5, 0x0637))
    log.append(
        Exchange(
            at=Monotonic(100.6),
            kind=ExchangeKind.SEND,
            register=None,
            value=None,
            ok=True,
            latency_ms=0.2,
            detail="SDK transport call",
            raw_hex="0103",
        )
    )
    log.append(exchange(100.7, 1))
    taken = listened.source()
    assert taken.lost == 0, "the two register transactions fit: the SDK call took no room"
    assert [(o.kind, o.value) for o in taken.observations] == [
        ("modbus_read", 0x0637),
        ("modbus_read", 1),
    ]


TICKS: Final[int] = 50


async def native_frames(*, sdk_frames: bool) -> tuple[dict[str, int], int]:
    """Fifty ticks of the REAL ATV320 driver over a simulated FTDI link, as the record
    would get them: the lines by kind, and the bytes they make in ``drive_frames.jsonl``."""
    clock = RealClock()
    chip = FakeAltivar({3201: 0x0637, 8602: 0, 8604: 0, 3204: 12, 7121: 0, 8501: 15})
    master, settings = ftdi_master(chip, timeout=Seconds(0.05))
    drive = ATV320Drive(clock, master, settings, RegisterMap())
    listened = tap_drive(drive, clock, sdk_frames=sdk_frames)
    assert listened.backend is drive, "the driver itself: nothing between it and the runtime"
    try:
        opened_link = await drive.open()
        assert isinstance(opened_link, Ok), opened_link
        before = listened.source()  # the opening's own exchanges are not what a tick costs
        assert before.lost == 0
        for _ in range(TICKS):
            written = await drive.write_speed(MotorRpm(0))
            status = await drive.read_status()
            assert isinstance(written, Ok), written
            assert isinstance(status, Ok), status
        taken = listened.source()
    finally:
        await drive.close()
    assert taken.lost == 0
    origin = clock.monotonic()
    kinds: dict[str, int] = {}
    size = 0
    for observation in taken.observations:
        kinds[observation.kind] = kinds.get(observation.kind, 0) + 1
        size += len(encode(document(FRAME, observation.frame(origin)), Privacy())) + 1
    return kinds, size


async def test_ex2_the_sdk_frames_switch_on_the_real_driver_both_positions_measured() -> None:
    """Off (the default): register transactions only. On: the SDK's calls too."""
    registers, small = await native_frames(sdk_frames=False)
    everything, large = await native_frames(sdk_frames=True)

    assert set(registers) == {"modbus_read", "modbus_write"}
    assert registers["modbus_write"] == TICKS, "the speed written at each tick"
    assert registers["modbus_read"] >= 4 * TICKS, "the status: ETA, LFRD, RFRD, LCR at least"
    assert set(everything) == {
        "modbus_read",
        "modbus_write",
        "modbus_send",
        "modbus_receive_chunk",
    }
    # The switch adds lines; it takes none away and changes none.
    assert everything["modbus_read"] == registers["modbus_read"]
    assert everything["modbus_write"] == registers["modbus_write"]
    transactions = registers["modbus_read"] + registers["modbus_write"]
    assert everything["modbus_send"] == transactions, "one request sent per transaction"
    assert everything["modbus_receive_chunk"] >= transactions
    assert small < large / 3

    per_minute = 5 * 60 / TICKS / 1000
    summary = (
        f"\nEX-2, ATV320 driver over a simulated FTDI link, one speed write and one status "
        f"read per tick: RECORD_DRIVE_SDK_FRAMES=false {sum(registers.values()) / TICKS:.0f} "
        f"lines per tick, {small * per_minute:.0f} kB per minute; =true "
        f"{sum(everything.values()) / TICKS:.0f} lines per tick, {large * per_minute:.0f} kB "
        f"per minute"
    )
    print(summary)  # noqa: T201 - the measurement the ticket asks for, shown with -s


def test_ex2_any_other_backend_is_given_a_tap_of_the_same_bound() -> None:
    clock = ManualClock(Monotonic(100.0))
    inner = Scripted(clock)
    listened = tap_drive(inner, clock, sdk_frames=True)
    assert isinstance(listened, Tapped)
    assert isinstance(listened.backend, CallTap), "the switch is about a native driver's SDK"
    nothing = listened.source()
    assert nothing == Taken((), 0)


def test_ex2_an_observation_becomes_a_line_timed_from_the_start_of_its_session() -> None:
    observation = Observation(
        at=Monotonic(112.3456),
        kind="read_status",
        register=(3201, 8602),
        value=(1591, None),
        ok=True,
        latency_ms=25.0,
    )
    assert observation.frame(Monotonic(100.0)) == DriveFrame(
        t=12.346,
        kind="read_status",
        register=(3201, 8602),
        value=(1591, None),
        ok=True,
        latency_ms=25.0,
        raw_hex=None,
    )
    assert observation.frame(Monotonic(112.5)).t == -0.154, "what preceded the start is negative"


# =========================================================================
# The journal thread takes what waits, and writes it to the record that was open
# =========================================================================


class Waiting:
    """A frame source the test fills: what the drive's tap would be holding."""

    def __init__(self) -> None:
        self.observations: list[Observation] = []
        self.lost: int = 0
        self.taken: int = 0

    def add(self, at: float, kind: str = "read_status", latency_ms: float = 1.0) -> None:
        self.observations.append(
            Observation(
                at=Monotonic(at), kind=kind, register=3201, value=1, ok=True, latency_ms=latency_ms
            )
        )

    def __call__(self) -> Taken:
        self.taken += 1
        waiting = tuple(self.observations)
        self.observations = []
        return Taken(waiting, self.lost)


def frames_of(journal: Journal) -> list[tuple[float, str]]:
    loaded = read(record_of(journal))
    assert isinstance(loaded, Ok), loaded
    return [(frame.t, frame.kind) for frame in loaded.value.frames]


def test_ex2_the_journal_takes_the_frames_at_every_cycle_and_writes_them(tmp_path: Path) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    waiting = Waiting()
    journal.listen(waiting)
    journal.drain()
    waiting.add(10.2)
    waiting.add(10.4, "speed")
    journal.drain()
    assert waiting.taken == 2, "once per cycle, frames or not"
    assert frames_of(journal) == [(0.2, "read_status"), (0.4, "speed")]
    assert journal.status(clock.monotonic()).degraded is False


def test_ex2_the_last_frames_of_a_session_are_in_its_record_not_lost_behind_its_closing(
    tmp_path: Path,
) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    waiting = Waiting()
    journal.listen(waiting)
    journal.drain()
    waiting.add(11.0, "emergency_zero")
    waiting.add(11.1, "close")
    journal.close(closing())
    journal.drain()
    loaded = read(record_of(journal))
    assert isinstance(loaded, Ok)
    assert loaded.value.warnings == (), "written before the checksums: the record is whole"
    assert [(f.t, f.kind) for f in loaded.value.frames] == [(1.0, "emergency_zero"), (1.1, "close")]


def test_ex2_the_frames_of_an_arming_go_to_the_record_that_arming_opens(tmp_path: Path) -> None:
    """Taken with no record open, but the same cycle opens one: they are its first lines."""
    clock = clock_at_start()
    journal = Journal(tmp_path / "records", clock)
    waiting = Waiting()
    journal.listen(waiting)
    waiting.add(9.9, "read_limits")
    waiting.add(9.95, "command")
    journal.open(session(), Privacy())
    journal.drain()
    assert frames_of(journal) == [(-0.1, "read_limits"), (-0.05, "command")]


def test_ex2_with_no_record_open_the_frames_are_let_go_and_nothing_is_counted(
    tmp_path: Path,
) -> None:
    clock = clock_at_start()
    journal = Journal(tmp_path / "records", clock)
    waiting = Waiting()
    journal.listen(waiting)
    waiting.add(5.0)
    journal.drain()
    assert waiting.observations == [], "taken, so that nothing piles up while idle"
    status = journal.status(clock.monotonic())
    assert (status.degraded, status.dropped, status.failures) == (False, 0, 0)
    journal.open(session(), Privacy())
    journal.drain()
    assert frames_of(journal) == [], "an idle console's polling is in no session's record"


def test_ex2_frames_the_tap_refused_are_counted_for_the_session_and_said_in_the_record(
    tmp_path: Path,
) -> None:
    clock = clock_at_start()
    journal = Journal(tmp_path / "records", clock)
    waiting = Waiting()
    journal.listen(waiting)
    waiting.lost = 4  # before any session: not this session's loss
    journal.drain()
    journal.open(session(), Privacy())
    journal.drain()
    assert journal.status(clock.monotonic()).dropped == 0

    waiting.lost = 9
    waiting.add(10.5)
    clock.advance(Seconds(2.0))
    journal.drain()
    status = journal.status(clock.monotonic())
    assert status.dropped == 5
    assert (status.degraded, status.cause) == (True, Cause.QUEUE_FULL)
    journal.close(closing())
    journal.drain()
    loaded = read(record_of(journal))
    assert isinstance(loaded, Ok)
    warnings = [e.detail for e in loaded.value.events if e.kind is EventKind.WARNING]
    assert warnings == ["record_degraded: dropped=0 failures=0 drive_frames_lost=5"]

    # The next session starts its own count.
    journal.open(session(2), Privacy())
    journal.drain()
    assert journal.status(clock.monotonic()).dropped == 0


def test_ex2_a_frame_the_format_refuses_costs_that_frame_and_is_counted(tmp_path: Path) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    waiting = Waiting()
    journal.listen(waiting)
    waiting.add(10.2, latency_ms=float("inf"))
    waiting.add(10.4)
    journal.drain()
    assert frames_of(journal) == [(0.4, "read_status")]
    status = journal.status(clock.monotonic())
    assert status.failures == 1
    assert status.error is not None
    assert status.error.operation == "append"


# =========================================================================
# The REAL console in simulation
# =========================================================================


@pytest.mark.parametrize("sdk_frames", ["false", "true", None])
async def test_ex2_acceptance_a_drive_fault_injected_in_a_simulated_session_is_in_drive_frames(
    tmp_path: Path, sdk_frames: str | None
) -> None:
    """With the switch off (and unset, which is off), and with it on: the fault is read."""
    env = dict(BENCH_ENV)
    if sdk_frames is not None:
        env["RECORD_DRIVE_SDK_FRAMES"] = sdk_frames
    recorded = recorded_rig(tmp_path, env=env)
    async with recorded.rig.http() as http:
        await start_bench(recorded, http)
        await set_target(http, 5.0)
        await recorded.tick(15.0)
        recorded.rig.simulator.inject_fault(DriveFault.OVERCURRENT)
        await recorded.tick(180.0)
        assert recorded.state() is RuntimeState.ENDING, "the fault is latched, nobody reset it"
    recording = recorded.recording()
    assert recording.warnings == (), "closed and whole, frames included in its checksums"
    frames = recording.frames
    assert frames, "the file is no longer empty"

    code = next(raw for raw, fault in LFT_FAULT_CODES.items() if fault is DriveFault.OVERCURRENT)
    statuses = [f for f in frames if f.kind == "read_status"]
    assert all(
        f.register == (ETA_LOGICAL, LFRD_LOGICAL, RFRD_LOGICAL, LCR_LOGICAL, LFT_LOGICAL)
        for f in statuses
    )
    faulted = [f for f in statuses if isinstance(f.value, tuple) and f.value[4] == code]
    assert faulted, "the fault code the drive latched is read in the frames"
    first = faulted[0]
    assert isinstance(first.value, tuple)
    word = first.value[0]
    assert word is not None
    assert word & FAULT_BIT, "and the status word that carried it says FAULT"
    # The record tells the same story twice: the frame, then the event made of it.
    said = next(e for e in recording.events if e.kind is EventKind.DRIVE_FAULT)
    assert said.detail.startswith(f"OCF (LFT {code})")
    assert first.t <= said.t <= first.t + 0.2

    # What the runtime asked is there too: the speed it wrote at each tick, and
    # the frames stop with the record (an idle console's polling is not in it).
    speeds = [f for f in frames if f.kind == "speed"]
    assert max(f.value for f in speeds if isinstance(f.value, int)) > 55
    assert frames[-1].t <= recording.rows[-1].t + 0.4


async def test_ex2_measured_what_the_frames_add_to_a_minute_of_record(tmp_path: Path) -> None:
    """One minute of a bench session at speed on the simulated console, then the sizes."""
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as http:
        await start_bench(recorded, http)
        await set_target(http, 5.0)
        await recorded.tick(60.0)
        path = recorded.only_record()
        size = (path / "drive_frames.jsonl").stat().st_size
        lines = len((path / "drive_frames.jsonl").read_text(encoding="utf-8").splitlines())
        rows = (path / "ticks.csv").stat().st_size
        await stop(recorded, http)
    summary = (
        f"\nEX-2, one minute of session on the simulated console: drive_frames.jsonl "
        f"{size} bytes in {lines} lines ({lines / 60:.1f} per second); ticks.csv {rows} bytes"
    )
    print(summary)  # noqa: T201 - the measurement the ticket asks for, shown with -s
    assert 5 * 60 <= lines <= 20 * 60, "a few calls per tick: a status read and a speed write"
    assert size < 150_000, "about a tenth of a megabyte a minute, appended to ONE file"


async def test_ex2_a_journal_that_stops_coming_costs_frames_and_never_a_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disk is hung: nobody takes the frames. The tap fills to its bound, and that is all."""
    taps: list[Tapped] = []

    def small(backend: DriveBackend, clock: Clock, *, sdk_frames: bool) -> Tapped:
        taps.append(tap_drive(backend, clock, capacity=40, sdk_frames=sdk_frames))
        return taps[0]

    monkeypatch.setattr(local_panel, "tap_drive", small)
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as http:
        await start_bench(recorded, http)
        await set_target(http, 5.0)
        await recorded.tick(30.0, drain=False)
        runtime = recorded.rig.panel.runtime
        assert recorded.state() is RuntimeState.RUNNING
        assert runtime.snapshot().measured.motor_rpm > 55, "the session went on behind a full tap"
        # The disk answers again: what waited is written, what was refused is said.
        await recorded.tick(3.0)
        await stop(recorded, http)
    recording = recorded.recording()
    # Nothing was taken between the arming tick and t = 30 s: what the record
    # holds of that stretch is what the tap could keep, and not one frame more.
    held = [f for f in recording.frames if 0.3 < f.t <= 30.2]
    assert 30 <= len(held) <= 40, "the bound held"
    assert max(f.t for f in held) < 5.0, "the oldest are kept, the newest refused"
    lost = [
        e.detail
        for e in recording.events
        if e.kind is EventKind.WARNING and "drive_frames_lost=" in e.detail
    ]
    assert lost, "the record says what it did not get"
    assert int(lost[-1].split("drive_frames_lost=")[1].split()[0]) > 100
    assert any(f.t > 31.0 for f in recording.frames), "and the frames resumed"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, False),
        ({"RECORD_DRIVE_SDK_FRAMES": "false"}, False),
        ({"RECORD_DRIVE_SDK_FRAMES": "true"}, True),
    ],
)
def test_ex2_the_console_hands_the_configured_switch_to_the_tap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: bool
) -> None:
    asked: list[bool] = []

    def remembered(backend: DriveBackend, clock: Clock, *, sdk_frames: bool) -> Tapped:
        asked.append(sdk_frames)
        return tap_drive(backend, clock, sdk_frames=sdk_frames)

    monkeypatch.setattr(local_panel, "tap_drive", remembered)
    loaded = load_local_config({**BENCH_ENV, **env})
    assert isinstance(loaded, Ok), loaded
    assert loaded.value.record.drive_sdk_frames is expected
    clock = ManualClock()
    local_panel.build_panel(
        loaded.value,
        clock=clock,
        profiles_path=tmp_path / "profiles.json",
        journal=Journal(tmp_path / "records", clock),
    )
    assert asked == [expected]

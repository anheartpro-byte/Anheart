"""A record played back: the drive it recorded, and the ECG it recorded.

:class:`TapeDrive` is a :class:`~src.motor.drive.DriveBackend` that answers each
call with what ``drive_frames.jsonl`` holds next: same order, same answer, same
latency (moved on the injected clock). It checks that the call IS the recorded
one first. A different call, a different command word, a speed more than the
tolerance away, a call made at another moment, or a call past the end of the
tape is a *mismatch*: from there on the recorded answers belong to questions
nobody is asking, so the tape stops answering and says where (EX-3). It never
raises: a drive that raised inside the runtime's tick would be handled there as
a crash of the tick, and bury the finding under the runtime's own exit path.
Every call after the mismatch simply fails, and the replay loop, which reads
:attr:`TapeDrive.mismatch` after each step, stops.

:class:`TapeSource` is the acquisition client the real
:class:`~src.ecg_pipeline.EcgBridge` reads: it hands each ``ecg_raw`` block
back at the instant the record says it was received.

Only the drive-call observations of :class:`~simulation.recording.RecordingDrive`
are a tape (``open``, ``command``, ``speed``, ``read_status``...). The native
driver's ``modbus_*`` exchanges are not: replaying those means replaying a
Modbus master under the real ATV320 driver, which this module does not do, and
a record holding them is refused rather than half-replayed.

What a tape cannot give back, because the record does not hold it:

* WHICH error a failed exchange was. Every one is replayed as a
  :class:`~src.motor.drive.CommTimeout`. The runtime branches on the variant
  in one way only (``EnableUnconfirmed`` and ``StopUnconfirmed`` mean the
  output stage may be energised), and neither is produced by the simulated
  drive whose calls are recorded here.
* the motor current below 0.1 A, the resolution of the LCR register the
  observation is written in.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final, final

from simulation.recording import FrameKind
from simulation.replay_report import SETPOINT_TOLERANCE_RPM
from src.bitalino_client import ChannelData, SampleBatch
from src.clock import ManualClock
from src.motor.acquisition import AcquisitionEvidence
from src.motor.drive import (
    ACC_LOGICAL,
    CMD_LOGICAL,
    DEC_LOGICAL,
    ETA_LOGICAL,
    HSP_LOGICAL,
    LCR_LOGICAL,
    LFRD_LOGICAL,
    LFT_LOGICAL,
    LSP_LOGICAL,
    RFRD_LOGICAL,
    TFR_LOGICAL,
    CommTimeout,
    ControlWord,
    DriveError,
    DriveLimits,
    DriveStatus,
    EmergencyStopOutcome,
    decode_current,
    decode_limits,
    decode_status_word,
    describe_fault,
)
from src.record.ecg import RawBlock
from src.record.schema import DriveFrame
from src.result import Err, Ok, Result
from src.units import Monotonic, MotorRpm, RawRegister, Seconds, StatusWord, UnixMillis, elapsed

TICK: Final[Seconds] = Seconds(0.2)
"""The control period: how far from its recorded instant an exchange may be asked for."""

INSTANT_EPSILON: Final[Seconds] = Seconds(1e-6)
"""Record instants are written to the millisecond; this absorbs the float additions."""

EMERGENCY_BUDGET: Final[Seconds] = Seconds(0.5)
"""What the tape reports as its emergency bound. Nothing is waited for on a tape."""

MILLIS_PER_SECOND: Final[int] = 1000
REGISTER_MAX: Final[int] = 0xFFFF

STATUS_REGISTERS: Final = (ETA_LOGICAL, LFRD_LOGICAL, RFRD_LOGICAL, LCR_LOGICAL, LFT_LOGICAL)
LIMIT_REGISTERS: Final = (TFR_LOGICAL, HSP_LOGICAL, LSP_LOGICAL, ACC_LOGICAL, DEC_LOGICAL)
NO_FURTHER: Final[str] = "no further exchange"


@unique
class Call(Enum):
    """One method of the drive seam, as the tape names it in a report."""

    OPEN = "open"
    CLOSE = "close"
    COMMAND = "command"
    SPEED = "speed"
    EMERGENCY_ZERO = "emergency_zero"
    READ_STATUS = "read_status"
    READ_LIMITS = "read_limits"


_CALLS: Final = {
    FrameKind.OPEN.value: Call.OPEN,
    FrameKind.CLOSE.value: Call.CLOSE,
    FrameKind.COMMAND.value: Call.COMMAND,
    FrameKind.SPEED.value: Call.SPEED,
    FrameKind.EMERGENCY_ZERO.value: Call.EMERGENCY_ZERO,
    FrameKind.READ.value: Call.READ_STATUS,
    FrameKind.READ_FAILED.value: Call.READ_STATUS,
    FrameKind.READ_LIMITS.value: Call.READ_LIMITS,
}


_READS: Final = frozenset({Call.READ_STATUS, Call.READ_LIMITS})


@dataclass(frozen=True, slots=True, kw_only=True)
class Exchange:
    """One recorded call of the drive seam, parsed: what was asked, what came back."""

    t: Seconds
    call: Call
    ok: bool
    latency: Seconds
    written: int | None = None
    """The command word or the speed that was written."""

    status: DriveStatus | None = None
    limits: DriveLimits | None = None


def describe(call: Call, written: int | None) -> str:
    """A call in words made of this module's vocabulary and numbers only."""
    if written is None:
        return call.value
    if call is Call.COMMAND:
        return f"command {ControlWord(written).name}"
    return f"{call.value} {written} motor rpm"


def _status(frame: DriveFrame) -> DriveStatus | None:
    """The status a successful ``read_status`` observation recorded, or ``None`` if malformed."""
    values = frame.value
    if frame.register != STATUS_REGISTERS or not isinstance(values, tuple):
        return None
    if len(values) != len(STATUS_REGISTERS):
        return None
    word, echo, speed, current, fault = values
    if word is None or echo is None or speed is None or current is None:
        return None
    if not (0 <= word <= REGISTER_MAX and 0 <= current <= REGISTER_MAX):
        return None
    if fault is not None and not 0 <= fault <= REGISTER_MAX:
        return None
    status_word = StatusWord(word)
    return DriveStatus(
        state=decode_status_word(status_word),
        status_word=status_word,
        setpoint_echo_rpm=MotorRpm(echo),
        output_rpm=MotorRpm(speed),
        current=decode_current(RawRegister(current)),
        fault=None if fault is None else describe_fault(RawRegister(fault)).fault,
        fault_code=None if fault is None else RawRegister(fault),
    )


def _limits(frame: DriveFrame) -> DriveLimits | None:
    """The limits a successful ``read_limits`` observation recorded, or ``None`` if malformed."""
    values = frame.value
    if frame.register != LIMIT_REGISTERS or not isinstance(values, tuple):
        return None
    registers = [RawRegister(value) for value in values if value is not None]
    if len(registers) != len(LIMIT_REGISTERS):
        return None
    tfr, hsp, lsp, acc, dec = registers
    return decode_limits(tfr=tfr, hsp=hsp, lsp=lsp, acc=acc, dec=dec)


def _written(call: Call, frame: DriveFrame) -> Result[int | None, str]:
    """The value a write carried. ``Err`` when the frame does not carry a usable one."""
    value = frame.value
    if call is Call.COMMAND:
        if frame.register != CMD_LOGICAL or not isinstance(value, int):
            return Err("a command without its word")
        if value not in tuple(ControlWord):
            return Err("a command word this build does not know")
        return Ok(value)
    if call is Call.SPEED:
        if not isinstance(value, int):
            return Err("a speed write without its value")
        return Ok(value)
    return Ok(None)


def _exchange(frame: DriveFrame) -> Result[Exchange, str]:
    call = _CALLS.get(frame.kind)
    if call is None:
        return Err(
            "not a drive-call observation (the native driver's Modbus exchanges are not "
            "replayable by this tape)"
        )
    if frame.latency_ms < 0.0:
        return Err("a negative latency")
    written = _written(call, frame)
    if isinstance(written, Err):
        return written
    status = _status(frame) if call is Call.READ_STATUS and frame.ok else None
    limits = _limits(frame) if call is Call.READ_LIMITS and frame.ok else None
    if call is Call.READ_STATUS and frame.ok != (frame.kind == FrameKind.READ.value):
        return Err("a status read whose kind and outcome disagree")
    if frame.ok and status is None and limits is None and call in _READS:
        return Err("a read without its five registers")
    return Ok(
        Exchange(
            t=Seconds(frame.t),
            call=call,
            ok=frame.ok,
            latency=Seconds(frame.latency_ms / MILLIS_PER_SECOND),
            written=written.value,
            status=status,
            limits=limits,
        )
    )


def parse_frames(frames: Sequence[DriveFrame]) -> Result[tuple[Exchange, ...], str]:
    """Every frame as an :class:`Exchange`, or which one is not a tape and why."""
    exchanges: list[Exchange] = []
    for index, frame in enumerate(frames):
        parsed = _exchange(frame)
        if isinstance(parsed, Err):
            return Err(f"drive frame {index} (t={frame.t:.3f} s) is {parsed.error}")
        exchanges.append(parsed.value)
    return Ok(tuple(exchanges))


@final
class Pace:
    """How the replay clock moves: from one recorded instant to the next, by the interval.

    By the INTERVAL, and in whole control periods when it is a multiple of one:
    that is the arithmetic of the clock that produced a simulated record
    (``start + 0.2 + 0.2 + ...``), and only the same additions give the same
    floats. An instant rebuilt as ``start + t`` is one ulp away often enough,
    and one ulp is what separates 59.999... s from 60 s when a phase ends at
    60 s and the ticks fall every 0.2 s: the phase changes a tick early, the
    ramp starts a tick early, and the tape rightly says the runtime diverged.
    A real session's instants are not multiples of anything; there this is
    simply the recorded interval, to the millisecond the record is written in.

    Never backwards: a replayed latency may have carried the clock past a
    step's instant, exactly as the original exchange carried the original clock.
    """

    __slots__ = ("_at", "_clock", "_nominal")

    def __init__(self, clock: ManualClock, t: Seconds) -> None:
        """``clock`` stands at the first step, which the record stamps ``t``."""
        self._clock: ManualClock = clock
        self._nominal: Monotonic = clock.monotonic()
        self._at: Seconds = t

    def to(self, t: Seconds) -> None:
        """Move on to the step recorded at ``t`` (never before the previous one)."""
        interval = round(t - self._at, 3)
        self._at = t
        if interval > 0.0:
            periods = round(interval / TICK)
            if abs(interval - periods * TICK) < INSTANT_EPSILON:
                for _ in range(periods):
                    self._nominal = Monotonic(self._nominal + TICK)
            else:
                self._nominal = Monotonic(self._nominal + interval)
        ahead = elapsed(self._clock.monotonic(), self._nominal)
        if ahead > 0.0:
            # Exact: the two instants are within a factor of two of each other,
            # so their difference is representable and the sum lands on _nominal.
            self._clock.advance(ahead)


@dataclass(frozen=True, slots=True)
class Mismatch:
    """Where the runtime stopped asking what the record answers."""

    t: Seconds
    requested: str
    recorded: str


@final
class TapeDrive:
    """``drive_frames.jsonl`` as a drive. Mutable, single-use, owned by the replay loop."""

    __slots__ = ("_clock", "_cursor", "_exchanges", "_mismatch", "_origin", "_proven", "_served")

    def __init__(
        self, exchanges: Sequence[Exchange], clock: ManualClock, origin: Monotonic
    ) -> None:
        self._exchanges: tuple[Exchange, ...] = tuple(exchanges)
        self._clock: ManualClock = clock
        self._origin: Monotonic = origin
        self._cursor: int = 0
        self._mismatch: Mismatch | None = None
        self._served: int = 0
        self._proven: bool = False

    @property
    def mismatch(self) -> Mismatch | None:
        """The first call that was not the recorded one, or ``None`` while the tape holds."""
        return self._mismatch

    @property
    def pending(self) -> Exchange | None:
        """The next recorded exchange nobody has asked for yet."""
        if self._cursor < len(self._exchanges):
            return self._exchanges[self._cursor]
        return None

    def _take(self, call: Call, written: int | None = None) -> Exchange | None:
        """The recorded exchange that answers this call, or ``None``: the tape no longer holds."""
        if self._mismatch is not None:
            return None
        now = elapsed(self._origin, self._clock.monotonic())
        recorded = self.pending
        if recorded is not None and _answers(recorded, call, written, now):
            self._cursor += 1
            if recorded.latency > 0.0:
                self._clock.advance(recorded.latency)
            if recorded.ok:
                self._served += 1
            return recorded
        self._mismatch = Mismatch(
            t=now,
            requested=describe(call, written),
            recorded=NO_FURTHER if recorded is None else located(recorded),
        )
        return None

    # -- DriveBackend -----------------------------------------------------

    @property
    def acquisition_evidence(self) -> AcquisitionEvidence:
        """Answered exchanges, and whether one ``open`` succeeded: what a tape can vouch for."""
        return AcquisitionEvidence(self._served, self._proven)

    async def open(self) -> Result[None, DriveError]:
        exchange = self._take(Call.OPEN)
        answer = _acknowledged(exchange)
        if isinstance(answer, Ok):
            self._proven = True
        return answer

    async def close(self) -> Result[None, DriveError]:
        return _acknowledged(self._take(Call.CLOSE))

    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        return _acknowledged(self._take(Call.COMMAND, int(word)))

    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        return _acknowledged(self._take(Call.SPEED, int(rpm)))

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        exchange = self._take(Call.READ_STATUS)
        if exchange is None or exchange.status is None:
            return Err(_failure(exchange))
        return Ok(exchange.status)

    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        exchange = self._take(Call.READ_LIMITS)
        if exchange is None or exchange.limits is None:
            return Err(_failure(exchange))
        return Ok(exchange.limits)

    @property
    def emergency_budget(self) -> Seconds:
        return EMERGENCY_BUDGET

    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:  # noqa: ARG002  # the seam's signature; a tape waits for nothing
        """The recorded emergency zero, or ``NOTHING_SENT`` once the tape no longer holds."""
        exchange = self._take(Call.EMERGENCY_ZERO)
        if exchange is None:
            return EmergencyStopOutcome.NOTHING_SENT
        if exchange.ok:
            return EmergencyStopOutcome.ACKNOWLEDGED
        return EmergencyStopOutcome.SENT_UNCONFIRMED


def _answers(recorded: Exchange, call: Call, written: int | None, now: Seconds) -> bool:
    """Whether ``recorded`` is this call: same method, same value, same moment."""
    if recorded.call is not call:
        return False
    if abs(recorded.t - now) > TICK + INSTANT_EPSILON:
        return False
    if written is None or recorded.written is None:
        return True
    if call is Call.SPEED:
        return abs(recorded.written - written) <= SETPOINT_TOLERANCE_RPM
    return recorded.written == written


def located(exchange: Exchange) -> str:
    """A recorded exchange and when it was recorded, for a report."""
    return f"{describe(exchange.call, exchange.written)} at t={exchange.t:.3f} s"


def _failure(exchange: Exchange | None) -> DriveError:
    """The one error a failed exchange is replayed as (see the module docstring).

    ``None`` is the tape that no longer holds: nothing answers, at once.
    """
    return CommTimeout(after=Seconds(0.0) if exchange is None else exchange.latency)


def _acknowledged(exchange: Exchange | None) -> Result[None, DriveError]:
    if exchange is not None and exchange.ok:
        return Ok(None)
    return Err(_failure(exchange))


# =========================================================================
# The ECG
# =========================================================================


@dataclass(frozen=True, slots=True)
class Delivery:
    """One recorded acquisition batch and the instant the acquisition handed it over."""

    due: Seconds
    batch: SampleBatch


def deliveries(blocks: Sequence[RawBlock], origin: Monotonic) -> tuple[Delivery, ...]:
    """The blocks as batches, in the order they were recorded.

    A block is due when the record says it was received. A record that does not
    say (``t_received`` absent) is given the earliest instant the batch can have
    existed: when its last sample was taken.
    """
    out: list[Delivery] = []
    for block in blocks:
        header = block.header
        received = header.t_received
        due = (
            header.t_first + header.n_samples / header.sample_rate if received is None else received
        )
        first_ms = round((float(origin) + header.t_first) * MILLIS_PER_SECOND)
        channels = tuple(
            ChannelData(channel=name, values=tuple(float(value) for value in samples))
            for name, samples in zip(header.channels, block.samples, strict=True)
        )
        batch = SampleBatch(timestamp=UnixMillis(first_ms), channels=channels)
        out.append(Delivery(due=Seconds(due), batch=batch))
    return tuple(out)


@final
class TapeSource:
    """``ecg_raw/`` as an acquisition client. Mutable, single-use, owned by the replay loop."""

    __slots__ = ("_clock", "_cursor", "_deliveries", "_origin")

    def __init__(self, recorded: Sequence[Delivery], clock: ManualClock, origin: Monotonic) -> None:
        self._deliveries: tuple[Delivery, ...] = tuple(recorded)
        self._clock: ManualClock = clock
        self._origin: Monotonic = origin
        self._cursor: int = 0

    async def read_samples(self, count: int = 1000) -> SampleBatch | None:  # noqa: ARG002  # the client's signature; a recorded batch has the size it had
        """The next recorded batch once its instant has come, else ``None``."""
        if self._cursor >= len(self._deliveries):
            return None
        delivery = self._deliveries[self._cursor]
        now = elapsed(self._origin, self._clock.monotonic())
        if delivery.due > now + INSTANT_EPSILON:
            return None
        self._cursor += 1
        return delivery.batch

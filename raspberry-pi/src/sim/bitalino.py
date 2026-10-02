"""A BITalino that is not there: a drop-in duck type for ``BITalinoClient``.

This is the seam that closes the loop with no hardware. It wires
``src/sim/physiology.py`` (the subject) to ``src/sim/ecg.py`` (the sensor) and
presents exactly the surface ``src/session_manager.py`` already calls::

    BITalinoClient(mac_address=..., channels=[...], sample_rate=...)
    set_disconnect_callback(cb)
    await connect()            await start_acquisition()
    await read_samples(n)      -> SampleBatch | None
    await stop_acquisition()   await disconnect()
    is_connected               is_acquiring

and it returns **real** ``src.bitalino_client.SampleBatch`` objects carrying
real ``ChannelData``, not look-alikes of its own. That matters: the session
manager builds ``[{"channel": ch.channel, "values": ch.values} for ch in
batch.channels]`` and hands it straight to ``SignalTreatment.treat_batch``, so
a simulator with its own record type would be testing its own record type.

**The honest departure from the contract.** Every method below returns a
``bool`` or ``None`` where the contract asks for ``Result[T, E]``, and
``is_connected`` / ``is_acquiring`` are public mutable attributes rather than
properties. That is not an oversight and it is not laziness: those signatures
are dictated by the class this one substitutes for. A simulator that were
better typed than the thing it replaces would not be a drop-in, and the
substitution is the entire value - the real client is what should move to
``Result``, and then this file follows it. Reported rather than diverged from.

**Aligned with the real client, not merely shaped like it.** The real module
is imported directly (it is inside both type checkers now), the channels are
sorted and de-duplicated into wire order exactly as the real client does, and
a batch is stamped with the wall-clock time of its FIRST sample, which is what
``SampleBatch.timestamp`` means. A simulator that labelled columns in request
order, or stamped batches with "now", would pass tests the hardware fails.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Final, final

from src.bitalino_client import CHANNEL_NAMES, ChannelData, SampleBatch
from src.clock import Clock
from src.sim.ecg import DEFAULT_ECG_CONFIG, EcgSynthesizer
from src.sim.physiology import Physiology
from src.sim.signals.base import SignalContext, SignalGenerator
from src.units import AdcCount, Monotonic, MotorRpm, UnixMillis, elapsed

ECG_CHANNEL: Final[str] = "ECG"
"""The channel name this simulator actually models. Compared against
``src.bitalino_client.CHANNEL_NAMES``, which is the single source of truth for
index-to-name, so a remap there follows through to here."""

LEGAL_SAMPLE_RATES: Final[frozenset[int]] = frozenset({1, 10, 100, 1000})
"""What the BITalino hardware accepts. Mirrored from the real client's
validation, because a session manager that is allowed to start a simulated
session at 500 Hz and not a real one is a session manager whose configuration
was never tested."""

MIN_ANALOG_CHANNEL: Final[int] = 0
MAX_ANALOG_CHANNEL: Final[int] = 5
"""A1..A6 as the BITalino numbers them."""

MILLIS_PER_SECOND: Final[int] = 1000


# =========================================================================
# The simulator
# =========================================================================


@final
class SimulatedBitalinoClient:
    """A BITalino-shaped source of synthetic ECG.

    **Mutable by design**, like the device it stands in for. Time is injected:
    the number of samples available is derived from the clock, so
    :meth:`read_samples` returns ``None`` until enough simulated time has
    passed - exactly as the real client returns ``None`` until its queue has
    enough rows. A test therefore has to advance the clock to get data, which
    is the discipline that keeps a 45-minute session testable in under a second.

    The motor speed is pushed in rather than pulled, with
    :meth:`set_motor_rpm`, so this object holds no reference to the drive.
    Closing the loop is then visible in the runner::

        status = await drive.read_status()
        client.set_motor_rpm(measured_rpm)
        batch = await client.read_samples(1000)

    and nothing here can quietly regulate on a speed that was never commanded.
    """

    __slots__ = (
        "_clock",
        "_ecg",
        "_generated",
        "_generators",
        "_motor_rpm",
        "_on_disconnect",
        "_physiology",
        "_refuse_connect",
        "_started_at",
        "channels",
        "is_acquiring",
        "is_connected",
        "mac_address",
        "sample_rate",
    )

    def __init__(
        self,
        clock: Clock,
        *,
        mac_address: str = "/dev/sim-bitalino",
        physiology: Physiology,
        channels: Sequence[int] | None = None,
        sample_rate: int = 1000,
        ecg: EcgSynthesizer | None = None,
        generators: Mapping[str, SignalGenerator] | None = None,
    ) -> None:
        """Build a disconnected client, validating exactly what the real one does.

        ``physiology`` is required: the subject carries the machine geometry,
        and a default subject would carry a default radius nobody measured.

        ``channels`` defaults to ``[0]`` (A1, the ECG column) and
        ``sample_rate`` to 1000 Hz, matching the real client's defaults. Both
        are validated here and both raise ``ValueError``: this runs at startup
        with nothing spinning, and a channel index the hardware does not have
        is a configuration bug that must not become a silently mislabelled
        column in a stored session. The channels are then sorted and
        de-duplicated, because that is the order the device streams them in.
        """
        if sample_rate not in LEGAL_SAMPLE_RATES:
            raise ValueError(
                f"sample rate must be one of {sorted(LEGAL_SAMPLE_RATES)}, got {sample_rate}"
            )
        requested = [0] if channels is None else list(channels)
        if not requested:
            raise ValueError("at least one analog channel is required")
        for channel in requested:
            if channel < MIN_ANALOG_CHANNEL or channel > MAX_ANALOG_CHANNEL:
                raise ValueError(
                    f"channel must be {MIN_ANALOG_CHANNEL}-{MAX_ANALOG_CHANNEL}, got {channel}"
                )

        self.mac_address: str = mac_address
        self.channels: tuple[int, ...] = tuple(sorted(set(requested)))
        """The acquired channels in wire order (ascending, unique), exactly as
        the real client exposes them."""
        self.sample_rate: int = sample_rate
        self.is_connected: bool = False
        self.is_acquiring: bool = False

        self._clock: Clock = clock
        self._physiology: Physiology = physiology
        self._ecg: EcgSynthesizer = EcgSynthesizer(DEFAULT_ECG_CONFIG) if ecg is None else ecg
        # Channel name -> generator for the non-ECG channels that are modelled.
        # Absent: the channel reads the synthesizer's flat "unmodelled" line.
        self._generators: Mapping[str, SignalGenerator] = (
            {} if generators is None else dict(generators)
        )
        self._motor_rpm: MotorRpm = MotorRpm(0)
        self._on_disconnect: Callable[[], Awaitable[None]] | None = None
        self._started_at: Monotonic = clock.monotonic()
        self._generated: int = 0
        self._refuse_connect: bool = False

    # -- observation -------------------------------------------------------

    @property
    def subject(self) -> Physiology:
        """The plant behind the electrodes. For tests and simulation logs.

        A caller on the control path must not read it: ground truth is exactly
        what the real system does not have, and code that consults it stops
        being evidence about the real system.
        """
        return self._physiology

    @property
    def samples_generated(self) -> int:
        """Samples produced since acquisition started. For tests and logs."""
        return self._generated

    # -- simulation control ------------------------------------------------

    def set_motor_rpm(self, rpm: MotorRpm) -> None:
        """Tell the subject how fast the machine it is strapped into is turning.

        Motor-shaft rpm, signed, as the drive reports it - the gearbox ratio is
        applied inside the plant, so handing an output-shaft speed in here
        would understate the load by a factor of 49.79.
        """
        self._motor_rpm = rpm

    def inject_connect_failure(self) -> None:
        """Make every subsequent :meth:`connect` fail, as an unpaired device does.

        One-way on purpose. "The BITalino would not connect" is a state an
        operator resolves with their hands, and a simulator that healed itself
        would let a retry loop look correct when it is not.
        """
        self._refuse_connect = True

    async def inject_disconnect(self) -> None:
        """Drop the link mid-acquisition and fire the disconnect callback.

        This mirrors the real client's ``_acquisition_loop`` error path
        precisely, including the order: both flags go false **before** the
        callback runs, so a callback that inspects the client sees a device
        that is already gone rather than one that is about to be. The session
        manager's disconnect handling is only as good as that ordering.
        """
        self.is_acquiring = False
        self.is_connected = False
        if self._on_disconnect is not None:
            await self._on_disconnect()

    # -- the BITalinoClient surface ---------------------------------------

    def set_disconnect_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register the coroutine to run when the link drops."""
        self._on_disconnect = callback

    # ASYNC109 wants `asyncio.timeout` instead of a timeout parameter, and it is
    # right in general. Here there is nothing to bound: the parameter exists
    # only so that a caller written against BITalinoClient.connect(timeout=...)
    # type-checks against this class too, and the body discards it before doing
    # anything. Verified by hand: no await, no I/O, no blocking call follows.
    async def connect(self, timeout: float = 30.0) -> bool:  # noqa: ASYNC109
        """Open the link. ``False`` if a failure was injected.

        ``timeout`` is accepted and ignored: nothing here blocks, so there is
        nothing to time out. It is in the signature because the real client has
        it, and a caller that passes it must not fail to compile against this.
        """
        del timeout
        if self._refuse_connect:
            return False
        self.is_connected = True
        return True

    async def disconnect(self) -> None:
        """Close the link, stopping acquisition first, as the real client does."""
        if self.is_acquiring:
            await self.stop_acquisition()
        self.is_connected = False

    async def start_acquisition(self) -> bool:
        """Begin acquiring. ``False`` if not connected; ``True`` if already running.

        Both of those are the real client's answers, including the odd one:
        starting an already-running acquisition returns ``True`` rather than
        failing, and does **not** restart the sample clock. A simulator that
        reset it here would hide a double-start bug in the caller.
        """
        if not self.is_connected:
            return False
        if self.is_acquiring:
            return True
        self._started_at = self._clock.monotonic()
        self._generated = 0
        self.is_acquiring = True
        return True

    async def stop_acquisition(self) -> None:
        """Stop acquiring. A no-op if not acquiring, as the real client is."""
        if not self.is_acquiring:
            return
        self.is_acquiring = False

    async def read_samples(self, count: int = 1000) -> SampleBatch | None:
        """The next ``count`` samples per channel, or ``None`` if not ready yet.

        ``None`` has the same two meanings it has on real hardware - not
        acquiring, or not enough data yet - and a caller must handle both
        without inventing a batch. The available count is
        ``floor(elapsed * sample_rate)``: truncated, because a fraction of a
        sample does not exist, so a caller that advances the clock by exactly
        one block's worth gets exactly one block.

        The subject is integrated to the **end** of the block and the block is
        then rendered from that state. First order, like the plant's own step;
        at 0.2 s blocks against a 2 s time constant the difference is not
        observable, and the alternative - rendering from the state at the start -
        would report a heart rate from before the speed change that caused it.
        """
        if not self.is_acquiring:
            return None
        if count <= 0:
            raise ValueError(f"count must be positive, got {count}")

        now = self._clock.monotonic()
        available = int(elapsed(self._started_at, now) * self.sample_rate)
        if available - self._generated < count:
            return None
        first_at = Monotonic(self._started_at + self._generated / self.sample_rate)
        self._generated += count

        block_end = Monotonic(self._started_at + self._generated / self.sample_rate)
        subject = self._physiology.advance(block_end, self._motor_rpm)
        ecg = self._ecg.render(count, subject)

        context = SignalContext(start=first_at, fs=self.sample_rate, count=count, subject=subject)
        channels = [
            ChannelData(channel=name, values=_as_floats(samples))
            for name, samples in self._columns(context, ecg)
        ]
        # The FIRST sample's wall-clock time, as SampleBatch.timestamp is
        # defined: "now" on the wall clock, minus how long ago that sample was.
        behind = round(elapsed(first_at, now) * MILLIS_PER_SECOND)
        return SampleBatch(
            timestamp=UnixMillis(self._clock.unix_millis() - behind), channels=channels
        )

    # -- internals ---------------------------------------------------------

    def _columns(
        self, context: SignalContext, ecg: tuple[AdcCount, ...]
    ) -> list[tuple[str, tuple[AdcCount, ...]]]:
        """One (name, samples) pair per acquired channel, in wire order.

        Order matters and is the real client's: the device streams channels
        ascending, and the client labels the Nth column with the Nth of its
        sorted channels, so a permuted request comes back ascending.
        ``CHANNEL_NAMES`` covers every index the constructor accepts, so the
        lookup is total (asserted in ``tests/test_sim.py``).
        """
        columns: list[tuple[str, tuple[AdcCount, ...]]] = []
        for index in self.channels:
            name = CHANNEL_NAMES[index]
            generator = self._generators.get(name)
            if name == ECG_CHANNEL:
                samples = ecg
            elif generator is not None:
                samples = generator.render(context)
            else:
                samples = self._ecg.unmodelled(context.count)
            columns.append((name, samples))
        return columns


def _as_floats(samples: tuple[AdcCount, ...]) -> list[float]:
    """Counts as the floats the real client hands over.

    The real client reaches this shape through ``numpy.ndarray.tolist()`` on a
    float matrix, so the pipeline downstream has always been given floats. The
    conversion is explicit here so that nothing downstream ever sees a shape
    the hardware path would not have produced.
    """
    return [float(sample) for sample in samples]

from collections.abc import Callable
from dataclasses import replace

from src.bitalino_client import SampleBatch
from src.clock import Clock
from src.record.ecg import Header, RawBlock, decode_block, encode_block
from src.sensors.base import SensorKind, SensorReading
from src.sensors.hub import SensorHub


async def _inline(work: Callable[[], tuple[SensorReading, ...]]) -> tuple[SensorReading, ...]:
    return work()


def _moved(raw: bytes, shift: float) -> bytes:
    """One encoded block, with its instants ``shift`` seconds earlier on the record's axis."""
    block = decode_block(raw)
    header = block.header
    received = header.t_received
    return encode_block(
        RawBlock(
            replace(
                header,
                t_first=round(header.t_first - shift, 3),
                t_received=None if received is None else round(received - shift, 3),
            ),
            block.samples,
        )
    )


class Capture:
    """Single-loop accumulator for the actual acquisition tap and 1 Hz sensor publication."""

    def __init__(self, clock: Clock, kinds: tuple[SensorKind, ...]) -> None:
        self.clock: Clock = clock
        self.hub: SensorHub = SensorHub(clock=clock, kinds=kinds, fs=1000, offload=_inline)
        self.blocks: list[bytes] = []
        self.sensors: list[tuple[float, tuple[SensorReading, ...]]] = []
        self.origin: float = clock.monotonic()
        self.acquisition_origin: float = self.origin
        self.last_sensor: float = -1.0

    def reset(self) -> None:
        """The session starts now: this instant becomes ``t`` = 0.

        The blocks acquired before it are KEPT, moved to negative instants: the
        DSP's window is full of them when the session starts, so a replay that
        began with the first session block would compute another heart rate for
        the first seconds. The 1 Hz sensor indicators restart.
        """
        now = self.clock.monotonic()
        shift = now - self.origin
        self.blocks[:] = [_moved(raw, shift) for raw in self.blocks]
        self.sensors.clear()
        self.origin = now
        self.last_sensor = -1.0

    def accept(self, batch: SampleBatch) -> None:
        self.hub.accept(batch)
        if not batch.channels:
            return
        n_samples = len(batch.channels[0].values)
        if not n_samples:
            return
        now = self.clock.monotonic()
        first = now - (self.clock.unix_millis() - batch.timestamp) / 1000
        offset = (first - self.acquisition_origin) * 1000
        header = Header(
            seq=round(offset / n_samples),
            t_first=round(first - self.origin, 3),
            n_samples=n_samples,
            channels=tuple(channel.channel for channel in batch.channels),
            # When the acquisition handed the batch over, which is not when its
            # samples were taken: a batch can be read a tick late.
            t_received=round(now - self.origin, 3),
        )
        if any(not value.is_integer() for channel in batch.channels for value in channel.values):
            raise ValueError("raw acquisition requires integer ADC counts")
        samples = tuple(tuple(int(value) for value in channel.values) for channel in batch.channels)
        self.blocks.append(encode_block(RawBlock(header, samples)))

    async def refresh(self) -> None:
        at = self.clock.monotonic() - self.origin
        if at - self.last_sensor >= 1.0 - 1e-9:
            self.sensors.append((at, await self.hub.refresh()))
            self.last_sensor = at

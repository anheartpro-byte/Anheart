import gzip
from typing import Final, Literal

from pydantic import TypeAdapter
from pydantic.dataclasses import dataclass

from src.record.codec import Privacy, document, encode, mapping
from src.record.schema import CONFIG
from src.sensors.base import SensorKind

SAMPLE_RATE: Final = 1000
INT16_MIN: Final = -32768
INT16_MAX: Final = 32767


@dataclass(frozen=True, slots=True, config=CONFIG)
class Header:
    seq: int
    t_first: float
    n_samples: int
    channels: tuple[str, ...]
    sample_rate: Literal[1000] = 1000
    t_received: float | None = None
    """When the acquisition handed this batch over, seconds on the record's axis.

    Optional: a producer that does not know it leaves it out. A replay needs it
    to hand the block back at the same instant, because ``t_first`` is the
    sample clock's time, not the moment the batch reached the DSP.
    """


@dataclass(frozen=True, slots=True)
class RawBlock:
    """Samples are channel-major, with no synthesized values at sequence gaps."""

    header: Header
    samples: tuple[tuple[int, ...], ...]


HEADER: Final[TypeAdapter[Header]] = TypeAdapter(Header)


def encode_block(block: RawBlock) -> bytes:
    header = block.header
    if header.seq < 0 or header.n_samples <= 0 or not header.channels:
        raise ValueError("invalid ECG block dimensions")
    if len(set(header.channels)) != len(header.channels):
        raise ValueError("duplicate ECG channels")
    if any(channel not in {kind.value for kind in SensorKind} for channel in header.channels):
        raise ValueError("unknown ECG channel")
    if len(block.samples) != len(header.channels):
        raise ValueError("ECG channel count mismatch")
    payload = bytearray()
    for channel in block.samples:
        if len(channel) != header.n_samples:
            raise ValueError("ECG sample count mismatch")
        for value in channel:
            if not INT16_MIN <= value <= INT16_MAX:
                raise ValueError("ECG value outside int16")
            payload.extend(value.to_bytes(2, "little", signed=True))
    fields = dict(mapping(document(HEADER, header)))
    if header.t_received is None:
        # Left out, never written as null: a block from a producer that does not
        # know the instant stays byte-identical to what it always was.
        del fields["t_received"]
    prefix = encode(fields, Privacy()).encode("utf-8") + b"\n"
    return gzip.compress(prefix + payload, mtime=0)


def decode_block(raw: bytes) -> RawBlock:
    prefix, data = gzip.decompress(raw).split(b"\n", 1)
    header = HEADER.validate_json(prefix)
    expected = header.n_samples * len(header.channels) * 2
    if len(data) != expected:
        raise ValueError("ECG payload length mismatch")
    samples = tuple(
        tuple(
            int.from_bytes(data[i : i + 2], "little", signed=True)
            for i in range(channel * header.n_samples * 2, (channel + 1) * header.n_samples * 2, 2)
        )
        for channel in range(len(header.channels))
    )
    block = RawBlock(header, samples)
    encode_block(block)
    return block

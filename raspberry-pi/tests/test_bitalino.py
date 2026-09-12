"""Test BITalino client: initialization, frame parsing, ordering, integrity."""

import logging

import numpy as np
import pytest

from src.bitalino_client import (
    CHANNEL_MAP,
    CHANNEL_NAMES,
    BITalinoClient,
    ChannelData,
    SampleBatch,
)


@pytest.fixture
def client():
    return BITalinoClient(
        mac_address="AA:BB:CC:DD:EE:FF",
        channels=[0],
        sample_rate=1000,
    )


def _frame(seq_start: int, analog_values: list[int]) -> np.ndarray:
    """Build a synthetic BITalino read() matrix: [seq, I1, I2, O1, O2, A1].

    analog_values populates the single analog column (A1 at column 5).
    """
    n = len(analog_values)
    m = np.zeros((n, 6), dtype=float)
    m[:, 0] = [(seq_start + i) % 16 for i in range(n)]  # seq ramps 0..15
    m[:, 1] = [i % 2 for i in range(n)]  # I1 toggles 0/1
    m[:, 5] = analog_values  # A1 = the "ECG"
    return m


# ---------------------------------------------------------------------------
# Initialization / config
# ---------------------------------------------------------------------------


def test_client_initialization(client):
    assert client.mac_address == "AA:BB:CC:DD:EE:FF"
    assert client.channels == [0]
    assert client.sample_rate == 1000
    assert not client.is_connected
    assert not client.is_acquiring


def test_default_channels():
    c = BITalinoClient(mac_address="AA:BB:CC:DD:EE:FF")
    assert c.channels == [0]


def test_multiple_channels():
    c = BITalinoClient(mac_address="AA:BB:CC:DD:EE:FF", channels=[0, 1, 2])
    assert c.channels == [0, 1, 2]


def test_channel_map_is_bijective_and_consistent():
    # The reverse map must be the exact inverse (no drift between the two).
    assert {index: name for name, index in CHANNEL_MAP.items()} == CHANNEL_NAMES
    assert CHANNEL_MAP["ECG"] == 0
    assert CHANNEL_NAMES[0] == "ECG"


def test_sample_batch_format():
    batch = SampleBatch(
        timestamp=1234567890,
        channels=[ChannelData(channel="ECG", values=[100, 200, 300])],
    )
    assert batch.timestamp == 1234567890
    assert batch.channels[0].channel == "ECG"
    assert batch.channels[0].values == [100, 200, 300]


# ---------------------------------------------------------------------------
# Frame parsing: correct analog column is extracted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_samples_extracts_analog_column(client):
    """The ECG values must come from analog column 5, not seq/digital columns."""
    client.is_acquiring = True
    ramp = list(range(100))  # known analog signal 0..99
    client._buffer.put((1000, _frame(0, ramp)))

    batch = await client.read_samples(count=100)

    assert batch is not None
    assert len(batch.channels) == 1
    assert batch.channels[0].channel == "ECG"
    assert batch.channels[0].values == [float(v) for v in ramp]


@pytest.mark.asyncio
async def test_read_samples_preserves_order_across_chunks(client):
    """Chunks drained from the queue must reassemble strictly oldest-first."""
    client.is_acquiring = True
    client._buffer.put((1000, _frame(0, list(range(0, 50)))))
    client._buffer.put((1001, _frame(50, list(range(50, 100)))))

    batch = await client.read_samples(count=100)

    assert batch is not None
    assert batch.channels[0].values == [float(v) for v in range(100)]


@pytest.mark.asyncio
async def test_read_samples_leftover_stays_oldest_first(client):
    """Surplus samples carried to the next call must not jump ahead of new data.

    This is the reordering-race regression test: read 60 of 100, then push a new
    newer chunk; the next read must return the 40 leftover BEFORE the new samples.
    """
    client.is_acquiring = True
    client._buffer.put((1000, _frame(0, list(range(0, 100)))))

    first = await client.read_samples(count=60)
    assert first is not None
    assert first.channels[0].values == [float(v) for v in range(60)]

    # Newer data arrives after the leftover (60..99) was set aside.
    client._buffer.put((1001, _frame(100, list(range(100, 120)))))
    second = await client.read_samples(count=60)

    assert second is not None
    # Expect leftover 60..99 first, then new 100..119 — strict time order.
    assert second.channels[0].values == [float(v) for v in range(60, 120)]


@pytest.mark.asyncio
async def test_read_samples_returns_none_until_enough(client):
    client.is_acquiring = True
    client._buffer.put((1000, _frame(0, list(range(30)))))  # only 30 < count
    assert await client.read_samples(count=100) is None


# ---------------------------------------------------------------------------
# Integrity guard: flat channel is flagged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flat_channel_logs_warning(client, caplog):
    """A near-constant analog channel (sensor unplugged) must WARN, not pass silently."""
    client.is_acquiring = True
    # The {0,1,3}-style unusable signal we observed with no sensor connected.
    flat = [0, 0, 0, 1, 0, 0, 3, 0] * 13  # 104 samples, max=3, ~std<1
    client._buffer.put((1000, _frame(0, flat)))

    with caplog.at_level(logging.WARNING):
        batch = await client.read_samples(count=100)

    assert batch is not None
    assert any("no analog signal" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_real_signal_does_not_warn(client, caplog):
    """A varied 10-bit signal must NOT trigger the no-signal warning."""
    client.is_acquiring = True
    # Sine-like swing across the ADC range (a real ECG-ish signal).
    real = [int(512 + 300 * np.sin(i / 5.0)) for i in range(100)]
    client._buffer.put((1000, _frame(0, real)))

    with caplog.at_level(logging.WARNING):
        await client.read_samples(count=100)

    assert not any("no analog signal" in r.message for r in caplog.records)

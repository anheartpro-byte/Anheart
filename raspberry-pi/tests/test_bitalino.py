"""Test BITalino client."""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from src.bitalino_client import BITalinoClient, ChannelData, SampleBatch


@pytest.fixture
def client():
    return BITalinoClient(
        mac_address="AA:BB:CC:DD:EE:FF",
        channels=[0],
        sample_rate=1000,
    )


@pytest.mark.asyncio
async def test_client_initialization(client):
    """Test client initializes correctly."""
    assert client.mac_address == "AA:BB:CC:DD:EE:FF"
    assert client.channels == [0]
    assert client.sample_rate == 1000
    assert not client.is_connected
    assert not client.is_acquiring


@pytest.mark.asyncio
@patch("src.bitalino_client.BleakClient")
async def test_connect_success(mock_bleak, client):
    """Test successful connection."""
    mock_instance = AsyncMock()
    mock_bleak.return_value = mock_instance

    result = await client.connect(timeout=5.0)

    assert result is True
    assert client.is_connected is True


@pytest.mark.asyncio
@patch("src.bitalino_client.BleakClient")
async def test_connect_timeout(mock_bleak, client):
    """Test connection timeout."""
    mock_instance = AsyncMock()
    mock_instance.connect.side_effect = asyncio.TimeoutError()
    mock_bleak.return_value = mock_instance

    result = await client.connect(timeout=1.0)

    assert result is False
    assert client.is_connected is False


def test_sample_batch_format():
    """Test sample batch data structure."""
    batch = SampleBatch(
        timestamp=1234567890,
        channels=[
            ChannelData(channel="ECG", values=[100, 200, 300]),
        ],
    )

    assert batch.timestamp == 1234567890
    assert len(batch.channels) == 1
    assert batch.channels[0].channel == "ECG"
    assert batch.channels[0].values == [100, 200, 300]


@pytest.mark.asyncio
async def test_default_channels():
    """Test default channel configuration."""
    client = BITalinoClient(mac_address="AA:BB:CC:DD:EE:FF")
    assert client.channels == [0]


@pytest.mark.asyncio
async def test_multiple_channels():
    """Test multiple channel configuration."""
    client = BITalinoClient(
        mac_address="AA:BB:CC:DD:EE:FF",
        channels=[0, 1, 2],
    )
    assert client.channels == [0, 1, 2]

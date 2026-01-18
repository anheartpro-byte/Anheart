"""Test Convex HTTP client."""

import pytest
from unittest.mock import patch

from src.convex_client import (
    ConvexClient,
    ApiResponse,
    ResponseStatus,
)


@pytest.fixture
def client():
    return ConvexClient(
        base_url="https://test.convex.cloud",
        api_key="test-api-key",
    )


@pytest.mark.asyncio
async def test_client_initialization(client):
    """Test client initializes correctly."""
    assert client.base_url == "https://test.convex.cloud"
    assert client.api_key == "test-api-key"


@pytest.mark.asyncio
async def test_heartbeat_success(client):
    """Test successful heartbeat."""
    with patch.object(client, "_request") as mock_request:
        mock_request.return_value = ApiResponse(
            status=ResponseStatus.SUCCESS,
            data={"success": True},
        )

        await client.start()
        response = await client.send_heartbeat(battery_level=85)
        await client.close()

        mock_request.assert_called_once()
        assert response.status == ResponseStatus.SUCCESS


@pytest.mark.asyncio
async def test_poll_session_with_pending(client):
    """Test polling returns pending session."""
    with patch.object(client, "_request") as mock_request:
        mock_request.return_value = ApiResponse(
            status=ResponseStatus.SUCCESS,
            data={
                "session": {
                    "id": "session-123",
                    "channels": ["ECG", "EMG"],
                    "config": {"sampleRate": 1000},
                }
            },
        )

        response, pending = await client.poll_session()

        assert response.status == ResponseStatus.SUCCESS
        assert pending is not None
        assert pending.session_id == "session-123"
        assert pending.channels == ["ECG", "EMG"]


@pytest.mark.asyncio
async def test_poll_session_empty(client):
    """Test polling returns no session."""
    with patch.object(client, "_request") as mock_request:
        mock_request.return_value = ApiResponse(
            status=ResponseStatus.SUCCESS,
            data={"session": None},
        )

        response, pending = await client.poll_session()

        assert response.status == ResponseStatus.SUCCESS
        assert pending is None


@pytest.mark.asyncio
async def test_send_data_format(client):
    """Test data sending format."""
    with patch.object(client, "_request") as mock_request:
        mock_request.return_value = ApiResponse(
            status=ResponseStatus.SUCCESS,
            data={"success": True},
        )

        await client.send_data(
            session_id="session-123",
            timestamp=1234567890000,
            samples=[{"channel": "ECG", "values": [100, 200, 300]}],
            batch_id="batch-001",
        )

        mock_request.assert_called_with(
            "POST",
            "/api/machine/data",
            json={
                "sessionId": "session-123",
                "timestamp": 1234567890000,
                "samples": [{"channel": "ECG", "values": [100, 200, 300]}],
                "batchId": "batch-001",
            },
            retry=False,
        )


@pytest.mark.asyncio
async def test_start_session(client):
    """Test starting a session."""
    with patch.object(client, "_request") as mock_request:
        mock_request.return_value = ApiResponse(
            status=ResponseStatus.SUCCESS,
            data={"success": True},
        )

        response = await client.start_session("session-123")

        assert response.status == ResponseStatus.SUCCESS
        mock_request.assert_called_with(
            "POST",
            "/api/machine/session/start",
            json={"sessionId": "session-123"},
        )


@pytest.mark.asyncio
async def test_end_session(client):
    """Test ending a session."""
    with patch.object(client, "_request") as mock_request:
        mock_request.return_value = ApiResponse(
            status=ResponseStatus.SUCCESS,
            data={"success": True},
        )

        response = await client.end_session("session-123", failed=True, reason="Test error")

        assert response.status == ResponseStatus.SUCCESS
        mock_request.assert_called_with(
            "POST",
            "/api/machine/session/end",
            json={
                "sessionId": "session-123",
                "failed": True,
                "reason": "Test error",
            },
        )

# Task 6.3: Convex HTTP Client

## Objective

Implement async HTTP client for communicating with Convex backend endpoints.

## Dependencies

- Task 6.1 (Project Setup) completed

---

## Acceptance Criteria

### ConvexClient Class

- [ ] Async HTTP client using httpx
- [ ] Authorization header with API key
- [ ] send_heartbeat method
- [ ] send_data method for ECG batches
- [ ] poll_session method for pending sessions
- [ ] start_session method
- [ ] end_session method

### Error Handling

- [ ] Retry logic with exponential backoff
- [ ] Timeout handling
- [ ] Network error handling
- [ ] Returns structured response (success/error)

### Response Types

- [ ] HeartbeatResponse
- [ ] DataResponse
- [ ] SessionResponse

---

## Implementation

```python
# raspberry-pi/src/convex_client.py
"""HTTP client for Convex backend communication."""

import asyncio
import logging
from typing import Optional
from dataclasses import dataclass
from enum import Enum

import httpx

logger = logging.getLogger(__name__)


class ResponseStatus(Enum):
    SUCCESS = "success"
    ERROR = "error"
    NETWORK_ERROR = "network_error"
    AUTH_ERROR = "auth_error"


@dataclass
class ApiResponse:
    """Generic API response."""
    status: ResponseStatus
    data: Optional[dict] = None
    error: Optional[str] = None


@dataclass
class PendingSession:
    """Pending session info from poll."""
    session_id: str
    channels: list[str]
    config: dict


class ConvexClient:
    """HTTP client for Convex API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def start(self) -> None:
        """Initialize HTTP client."""
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        json: Optional[dict] = None,
        retry: bool = True,
    ) -> ApiResponse:
        """Make HTTP request with retry logic."""
        if not self._client:
            return ApiResponse(
                status=ResponseStatus.ERROR,
                error="Client not initialized",
            )

        url = f"{self.base_url}{path}"
        attempts = self.max_retries if retry else 1
        last_error = None

        for attempt in range(attempts):
            try:
                if method == "GET":
                    response = await self._client.get(url)
                elif method == "POST":
                    response = await self._client.post(url, json=json)
                else:
                    raise ValueError(f"Unsupported method: {method}")

                # Check status
                if response.status_code == 401:
                    return ApiResponse(
                        status=ResponseStatus.AUTH_ERROR,
                        error="Invalid API key",
                    )

                if response.status_code >= 400:
                    error_data = response.json() if response.text else {}
                    return ApiResponse(
                        status=ResponseStatus.ERROR,
                        error=error_data.get("error", f"HTTP {response.status_code}"),
                    )

                return ApiResponse(
                    status=ResponseStatus.SUCCESS,
                    data=response.json() if response.text else {},
                )

            except httpx.TimeoutException as e:
                last_error = f"Timeout: {e}"
                logger.warning(f"Request timeout (attempt {attempt + 1}): {path}")
            except httpx.NetworkError as e:
                last_error = f"Network error: {e}"
                logger.warning(f"Network error (attempt {attempt + 1}): {path}")
            except Exception as e:
                last_error = f"Unexpected error: {e}"
                logger.error(f"Request error: {e}")
                break

            # Exponential backoff
            if attempt < attempts - 1:
                delay = 2 ** attempt
                await asyncio.sleep(delay)

        return ApiResponse(
            status=ResponseStatus.NETWORK_ERROR,
            error=last_error,
        )

    async def send_heartbeat(
        self,
        battery_level: Optional[int] = None,
        wifi_strength: Optional[int] = None,
        active_session_id: Optional[str] = None,
    ) -> ApiResponse:
        """Send heartbeat to server."""
        payload = {}
        if battery_level is not None:
            payload["batteryLevel"] = battery_level
        if wifi_strength is not None:
            payload["wifiStrength"] = wifi_strength
        if active_session_id is not None:
            payload["activeSessionId"] = active_session_id

        return await self._request(
            "POST",
            "/api/machine/heartbeat",
            json=payload if payload else None,
        )

    async def send_data(
        self,
        session_id: str,
        timestamp: int,
        samples: list[dict],
        batch_id: Optional[str] = None,
    ) -> ApiResponse:
        """
        Send ECG data batch.

        Args:
            session_id: Active session ID
            timestamp: Unix timestamp in milliseconds
            samples: List of {channel: str, values: list[int]}
            batch_id: Optional batch identifier for deduplication
        """
        payload = {
            "sessionId": session_id,
            "timestamp": timestamp,
            "samples": samples,
        }
        if batch_id:
            payload["batchId"] = batch_id

        return await self._request(
            "POST",
            "/api/machine/data",
            json=payload,
            retry=False,  # Don't retry data - let buffer handle it
        )

    async def poll_session(self) -> tuple[ApiResponse, Optional[PendingSession]]:
        """
        Poll for pending session.

        Returns:
            Tuple of (response, pending_session or None)
        """
        response = await self._request("GET", "/api/machine/session/poll")

        if response.status != ResponseStatus.SUCCESS:
            return response, None

        session_data = response.data.get("session") if response.data else None
        if not session_data:
            return response, None

        pending = PendingSession(
            session_id=session_data["id"],
            channels=session_data.get("channels", ["ECG"]),
            config=session_data.get("config", {}),
        )
        return response, pending

    async def start_session(self, session_id: str) -> ApiResponse:
        """Notify server that session has started."""
        return await self._request(
            "POST",
            "/api/machine/session/start",
            json={"sessionId": session_id},
        )

    async def end_session(
        self,
        session_id: str,
        failed: bool = False,
        reason: Optional[str] = None,
    ) -> ApiResponse:
        """Notify server that session has ended."""
        payload = {"sessionId": session_id}
        if failed:
            payload["failed"] = True
            payload["reason"] = reason or "Unknown error"

        return await self._request(
            "POST",
            "/api/machine/session/end",
            json=payload,
        )
```

```python
# raspberry-pi/tests/test_convex_client.py
"""Test Convex HTTP client."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.convex_client import (
    ConvexClient,
    ApiResponse,
    ResponseStatus,
    PendingSession,
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
```

---

## Testing Steps

### Unit Tests

1. Run `pytest tests/test_convex_client.py`
2. Verify all tests pass

### Integration Test

1. Configure `.env` with real credentials
2. Test heartbeat:

```python
import asyncio
from src.config import get_config
from src.convex_client import ConvexClient

async def test_heartbeat():
    config = get_config()
    async with ConvexClient(config.convex_url, config.machine_api_key) as client:
        response = await client.send_heartbeat(battery_level=100)
        print(f"Response: {response}")

asyncio.run(test_heartbeat())
```

3. Verify heartbeat appears in Convex dashboard

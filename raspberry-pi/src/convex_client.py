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
            retry=False,
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
        payload: dict = {"sessionId": session_id}
        if failed:
            payload["failed"] = True
            payload["reason"] = reason or "Unknown error"

        return await self._request(
            "POST",
            "/api/machine/session/end",
            json=payload,
        )

    async def check_session_status(self, session_id: str) -> tuple[ApiResponse, Optional[dict]]:
        """
        Check if session is still active (to detect remote session end).
        
        Returns:
            Tuple of (response, status_info or None)
            status_info contains: {"status": str, "active": bool, "endedAt": int|None}
        """
        response = await self._request(
            "GET",
            f"/api/machine/session/status?sessionId={session_id}",
            retry=False,
        )
        
        if response.status != ResponseStatus.SUCCESS:
            return response, None
        
        return response, response.data

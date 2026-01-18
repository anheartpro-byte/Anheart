"""Session manager state machine."""

import asyncio
import logging
import time
from enum import Enum
from typing import Optional

from .config import Config
from .bitalino_client import BITalinoClient, SampleBatch
from .convex_client import ConvexClient, ResponseStatus, PendingSession
from .data_buffer import DataBuffer

logger = logging.getLogger(__name__)


class SessionState(Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    ACQUIRING = "acquiring"
    ENDING = "ending"
    ERROR = "error"


class SessionManager:
    """Manages ECG recording sessions."""

    def __init__(self, config: Config):
        self.config = config
        self.state = SessionState.IDLE
        self.current_session: Optional[PendingSession] = None

        self.bitalino: Optional[BITalinoClient] = None
        self.convex: Optional[ConvexClient] = None
        self.buffer: Optional[DataBuffer] = None

        self._last_heartbeat = 0
        self._last_batch = 0
        self._batch_counter = 0

        self._running = False
        self._online = False

    async def start(self) -> None:
        """Initialize and start the session manager."""
        logger.info("Starting session manager...")

        self.buffer = DataBuffer(self.config.buffer_db_path)
        self.convex = ConvexClient(
            self.config.convex_url,
            self.config.machine_api_key,
        )
        await self.convex.start()

        response = await self.convex.send_heartbeat()
        if response.status == ResponseStatus.AUTH_ERROR:
            logger.error("Invalid API key - check configuration")
            raise ValueError("Invalid API key")

        self._online = response.status == ResponseStatus.SUCCESS
        if self._online:
            logger.info("Connected to server")
        else:
            logger.warning("Cannot reach server - will retry")

        self._running = True
        logger.info("Session manager started")

    async def stop(self) -> None:
        """Stop the session manager."""
        logger.info("Stopping session manager...")
        self._running = False

        if self.current_session and self.state == SessionState.ACQUIRING:
            await self._end_session(failed=True, reason="Shutdown requested")

        if self.bitalino and self.bitalino.is_connected:
            await self.bitalino.stop_acquisition()
            await self.bitalino.disconnect()

        if self.convex:
            await self.convex.close()

        if self.buffer:
            self.buffer.close()

        logger.info("Session manager stopped")

    async def run(self) -> None:
        """Main loop."""
        while self._running:
            try:
                await self._tick()
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(1)

    async def _tick(self) -> None:
        """Single tick of the main loop."""
        now = time.time()

        if now - self._last_heartbeat >= self.config.heartbeat_interval_s:
            await self._send_heartbeat()
            self._last_heartbeat = now

        if self.state == SessionState.IDLE:
            await self._handle_idle()
        elif self.state == SessionState.CONNECTING:
            await self._handle_connecting()
        elif self.state == SessionState.ACQUIRING:
            await self._handle_acquiring()
        elif self.state == SessionState.ENDING:
            await self._handle_ending()
        elif self.state == SessionState.ERROR:
            await self._handle_error()

        if self._online and self.state == SessionState.IDLE:
            await self._sync_buffered_data()

    async def _send_heartbeat(self) -> None:
        """Send heartbeat to server."""
        session_id = self.current_session.session_id if self.current_session else None

        response = await self.convex.send_heartbeat(
            active_session_id=session_id,
        )

        was_online = self._online
        self._online = response.status == ResponseStatus.SUCCESS

        if self._online and not was_online:
            logger.info("Connection restored")
        elif not self._online and was_online:
            logger.warning("Connection lost")

    async def _handle_idle(self) -> None:
        """Handle IDLE state - poll for sessions."""
        if not self._online:
            return

        response, pending = await self.convex.poll_session()

        if pending:
            logger.info(f"Found pending session: {pending.session_id}")
            self.current_session = pending
            self.state = SessionState.CONNECTING

    async def _handle_connecting(self) -> None:
        """Handle CONNECTING state - connect to BITalino."""
        if not self.current_session:
            self.state = SessionState.IDLE
            return

        channel_map = {"ECG": 0, "EMG": 1, "EDA": 2, "EEG": 3, "ACC": 4, "LUX": 5}
        channels = [
            channel_map.get(ch.upper(), 0)
            for ch in self.current_session.channels
        ]

        self.bitalino = BITalinoClient(
            mac_address=self.config.bitalino_mac,
            channels=channels,
            sample_rate=self.config.sample_rate,
        )
        self.bitalino.set_disconnect_callback(self._on_bitalino_disconnect)

        connected = await self.bitalino.connect()
        if not connected:
            logger.error("Failed to connect to BITalino")
            self.state = SessionState.ERROR
            return

        started = await self.bitalino.start_acquisition()
        if not started:
            logger.error("Failed to start acquisition")
            await self.bitalino.disconnect()
            self.state = SessionState.ERROR
            return

        response = await self.convex.start_session(self.current_session.session_id)
        if response.status != ResponseStatus.SUCCESS:
            logger.error(f"Failed to start session on server: {response.error}")
            await self.bitalino.stop_acquisition()
            await self.bitalino.disconnect()
            self.state = SessionState.ERROR
            return

        logger.info("Session started - acquiring data")
        self._batch_counter = 0
        self._last_batch = time.time()
        self.state = SessionState.ACQUIRING

    async def _handle_acquiring(self) -> None:
        """Handle ACQUIRING state - read and send data."""
        if not self.bitalino or not self.current_session:
            self.state = SessionState.ERROR
            return

        now = time.time()
        if (now - self._last_batch) < (self.config.batch_interval_ms / 1000):
            return

        batch = await self.bitalino.read_samples(self.config.sample_rate)
        if not batch:
            return

        self._last_batch = now
        self._batch_counter += 1

        samples = [
            {"channel": ch.channel, "values": ch.values}
            for ch in batch.channels
        ]

        if self._online:
            response = await self.convex.send_data(
                session_id=self.current_session.session_id,
                timestamp=batch.timestamp,
                samples=samples,
                batch_id=f"batch-{self._batch_counter}",
            )

            if response.status == ResponseStatus.SUCCESS:
                logger.debug(f"Sent batch {self._batch_counter}")
                return
            else:
                logger.warning(f"Failed to send batch: {response.error}")
                self._online = False

        self.buffer.store(
            session_id=self.current_session.session_id,
            timestamp=batch.timestamp,
            samples=samples,
        )
        logger.debug(f"Buffered batch {self._batch_counter}")

    async def _handle_ending(self) -> None:
        """Handle ENDING state - cleanup."""
        await self._end_session(failed=False)
        self.state = SessionState.IDLE

    async def _handle_error(self) -> None:
        """Handle ERROR state - attempt recovery."""
        if self.current_session:
            await self._end_session(failed=True, reason="Error during session")

        await asyncio.sleep(5)
        self.state = SessionState.IDLE

    async def _end_session(
        self,
        failed: bool = False,
        reason: Optional[str] = None,
    ) -> None:
        """End the current session."""
        if not self.current_session:
            return

        logger.info(f"Ending session: {self.current_session.session_id}")

        if self.bitalino:
            await self.bitalino.stop_acquisition()
            await self.bitalino.disconnect()
            self.bitalino = None

        if self._online:
            await self.convex.end_session(
                session_id=self.current_session.session_id,
                failed=failed,
                reason=reason,
            )

        self.current_session = None

    async def _on_bitalino_disconnect(self) -> None:
        """Handle unexpected BITalino disconnect."""
        logger.error("BITalino disconnected unexpectedly")
        if self.state == SessionState.ACQUIRING:
            self.state = SessionState.ERROR

    async def _sync_buffered_data(self) -> None:
        """Sync buffered data to server."""
        pending = self.buffer.get_pending_count()
        if pending == 0:
            return

        logger.info(f"Syncing {pending} buffered batches...")

        batches = self.buffer.get_unsynced(limit=50)
        synced_ids = []

        for batch in batches:
            response = await self.convex.send_data(
                session_id=batch.session_id,
                timestamp=batch.timestamp,
                samples=batch.samples,
            )

            if response.status == ResponseStatus.SUCCESS:
                synced_ids.append(batch.id)
            else:
                logger.warning(f"Failed to sync batch {batch.id}: {response.error}")
                break

        if synced_ids:
            self.buffer.mark_synced(synced_ids)
            logger.info(f"Synced {len(synced_ids)} batches")

        self.buffer.cleanup_old(max_age_hours=24)

    def request_end_session(self) -> None:
        """Request to end the current session (called externally)."""
        if self.state == SessionState.ACQUIRING:
            self.state = SessionState.ENDING

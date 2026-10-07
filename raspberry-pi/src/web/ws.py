"""The telemetry socket, and the two checks a WebSocket does not get for free.

Why this endpoint has its own authorisation
-------------------------------------------
**WebSockets are not subject to CORS.** A browser will happily open a socket
from any page to any origin and hand the response to that page's script: the
same-origin policy that stops ``fetch`` does not apply, and a ``SameSite``
cookie does not protect a socket either, because the handshake is a plain GET
that carries whatever the browser has. Without an ``Origin`` check, any page
that happened to be open on the clinic wifi could connect to this socket - and,
through the same trust, drive the rest of the API.

So the handshake is refused, **before** ``accept``, unless:

* the ``Origin`` header is absent or in the configured allowlist (see
  :meth:`~src.web.deps.WebConfig.origin_allowed` for why absent is allowed),
  and
* the shared token matches, when one is configured.

Refusing before ``accept`` matters: a socket that is accepted and then closed
has already been upgraded, and the page's script has already been told it
connected. The rejection has to happen at the handshake, where it surfaces as
an HTTP failure the browser reports honestly.

What this endpoint sends
------------------------
Whatever the hub hands it, in order: coalesced snapshots, every event, and -
pulled rather than pushed - a slice of the ECG ring after each snapshot. The
ECG is pulled on purpose: a 250 Hz stream pushed at a sleeping tablet is
exactly the backpressure ``src/telemetry.py`` exists to prevent, and a client
that missed a second wants the newest second, not a queue of the second it
missed.

Detecting a disconnect
----------------------
A reader task consumes incoming frames and does nothing with them. Its real job
is to notice the disconnect: a writer that only ever writes learns a socket is
gone when a write fails, which on a half-open connection can take minutes. When
the reader sees the socket go, it *evicts* the client, which sets the same
wake-up the hub uses - so the writer comes out of its await through the
mechanism that already exists rather than through a second one that could
disagree with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Final

from fastapi import FastAPI, WebSocket, status
from starlette.websockets import WebSocketDisconnect

from src.telemetry import PayloadKind, TelemetryClient, TelemetryHub
from src.web.deps import TOKEN_QUERY_PARAM, Services, WebConfig
from src.web.schemas import WsEnvelope, encode_envelope, payload_is_terminal

_logger: logging.Logger = logging.getLogger(__name__)

TELEMETRY_PATH: Final[str] = "/ws/telemetry"

ECG_FRAME_LIMIT: Final[int] = 1500
"""Most ECG samples in one frame: six seconds at 250 Hz, the whole ring.

A newly connected page gets the ring in one frame so its trace is populated
immediately; afterwards each frame carries only what is new, which at 5 Hz is
about fifty samples.
"""

_BAD_ORIGIN: Final[str] = (
    "origin not allowed: this socket can start and stop a motor, and WebSockets "
    "are not subject to the same-origin policy that would otherwise stop you"
)

_BAD_TOKEN: Final[str] = "a valid token is required"  # noqa: S105  # a message, not a secret

_FELL_BEHIND: Final[str] = "this screen fell behind and was disconnected; reload to resync"

LOGGED_HEADER_LIMIT: Final[int] = 120
"""Most characters of a refused header value that go into the log.

An ``Origin`` is a scheme, a host and a port. The value logged is whatever the
peer sent, read before any check has passed, so its length is bounded here
rather than left to the server's own limit on a header.
"""


def logged_header(value: str | None) -> str | None:
    """A header value as it is written to the log: on one line, and bounded.

    The value comes from the peer and has passed no check yet. Line breaks are
    replaced first, so that it stays on the line of the message that quotes it
    whatever the format directive is; what is left is cut to
    :data:`LOGGED_HEADER_LIMIT` characters, and the cut is stated with the
    length received. ``None`` (the header absent) is returned as it is.
    """
    if value is None:
        return None
    single = value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    if len(single) <= LOGGED_HEADER_LIMIT:
        return single
    return f"{single[:LOGGED_HEADER_LIMIT]}... ({len(single)} characters received)"


def register_socket(app: FastAPI, *, services: Services, config: WebConfig) -> None:
    """Register the telemetry socket on ``app``."""

    @app.websocket(TELEMETRY_PATH)
    async def telemetry(websocket: WebSocket) -> None:
        """Stream snapshots, events and the ECG trace to one browser."""
        await serve_telemetry(websocket, services=services, config=config)


async def serve_telemetry(websocket: WebSocket, *, services: Services, config: WebConfig) -> None:
    """Authorise the handshake, then stream until either side goes away.

    Split out of the route so a test can drive it directly, and so the
    handshake checks are testable without a running server.
    """
    origin = websocket.headers.get("origin")
    if not config.origin_allowed(origin):
        _logger.warning("refused telemetry socket from origin %r", logged_header(origin))
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=_BAD_ORIGIN)
        return
    if not config.token_matches(websocket.query_params.get(TOKEN_QUERY_PARAM)):
        _logger.warning("refused telemetry socket: bad or missing token")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=_BAD_TOKEN)
        return

    await websocket.accept()
    hub = services.hub
    client = hub.subscribe()
    reader = asyncio.create_task(_watch_for_disconnect(websocket, client))
    try:
        await _stream(websocket, hub, client)
    finally:
        # Release first: the hub must stop publishing to a client whose socket
        # is going away before anything else is attempted, because that is the
        # part the control loop can be made to wait on.
        hub.release(client)
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader


async def _stream(websocket: WebSocket, hub: TelemetryHub, client: TelemetryClient) -> None:
    """Write payloads until the client is evicted or the socket fails."""
    cursor = await _send_ecg(websocket, hub, after=None)
    try:
        while True:
            payload = await client.next_payload()
            await websocket.send_text(encode_envelope(WsEnvelope.of(payload)))
            if payload_is_terminal(payload.kind):
                await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER, reason=_FELL_BEHIND)
                return
            if payload.kind is PayloadKind.SNAPSHOT:
                cursor = await _send_ecg(websocket, hub, after=cursor)
    except WebSocketDisconnect:
        _logger.info("telemetry socket closed by the client")
    except RuntimeError as exc:
        # Starlette raises this when writing to a socket that has already been
        # closed from the other end. Not an error worth propagating: a browser
        # closing a tab is the normal case, and this handler has nothing left
        # to do about it.
        _logger.info("telemetry socket write failed: %s", exc)


async def _send_ecg(websocket: WebSocket, hub: TelemetryHub, *, after: int | None) -> int:
    """Send the ECG samples after ``after``; return the new cursor.

    Nothing is sent when there is nothing new, so an idle machine costs one
    snapshot frame per tick rather than two. A ``gap`` is always sent, even
    with no samples, because the page has to break its line rather than join
    two ends of a discontinuity - that join draws a vertical stroke which reads
    as a QRS complex.
    """
    window = hub.ecg_window(after=after, limit=ECG_FRAME_LIMIT)
    if not window.samples and not window.gap:
        return window.seq
    await websocket.send_text(encode_envelope(WsEnvelope.of_ecg(window)))
    return window.seq


async def _watch_for_disconnect(websocket: WebSocket, client: TelemetryClient) -> None:
    """Consume incoming frames, and evict the client when the socket goes.

    The page sends nothing, so this normally blocks until the disconnect. It
    exists for exactly that: without a pending read, a closed socket is only
    discovered by a failing write, which on a half-open connection can take
    minutes - minutes in which an operator is looking at a dashboard that has
    stopped updating and does not know it.

    Anything the page does send is read and discarded. Commands arrive over
    HTTP, where they are authorised, validated and answered with a status code;
    accepting them here would be a second, weaker control path into the same
    machine.

    Cancellation is deliberately NOT caught. ``asyncio.CancelledError`` is a
    ``BaseException`` and so passes straight through the clause below - there is
    no ``except CancelledError: raise`` here because it would be a branch that
    cannot change the outcome, and an unreachable branch is something to delete
    rather than to explain.
    """
    try:
        while True:
            _ = await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        client.evict("socket closed by the client")

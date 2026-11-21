"""``python -m simulation.live``: serve the 2D viewer and stream a scenario live, at N x speed.

    python -m simulation.live                       # http://127.0.0.1:8765/
    python -m simulation.live --port 9000

Then open ``http://127.0.0.1:8765/?live=manual_27_rpm&speed=20`` for a live run
(Server-Sent Events: stdlib only, no WebSocket dependency), or
``http://127.0.0.1:8765/?trace=out/manual_27_rpm.jsonl`` to play back a trace
written by ``python -m simulation.run``, or ``http://127.0.0.1:8765/?console`` to follow
a running console (``--console``, default ``http://127.0.0.1:8090``; read only,
:mod:`simulation.console_view`).

The live run is the same :class:`~simulation.harness.Session` as the battery, in
deterministic simulated time shown at ``speed`` x (:class:`~simulation.harness.SleepingTicker`),
or with ``&clock=sim`` on the codebase's own :class:`~src.clock.SimClock` at ``speed`` x
real time (:class:`~simulation.harness.PacedTicker`; keep the speed modest there, a host
pause becomes a loop stall the runtime reacts to). Each
browser connection gets its own run in its own thread and event loop; nothing
is shared between them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import queue
import threading
import time
from collections.abc import Mapping, Sequence
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, override
from urllib.parse import parse_qs, urlsplit

from simulation.console_view import DEFAULT_CONSOLE, console_address, http_fetch, stream_console
from simulation.harness import PacedTicker, Session, SleepingTicker
from simulation.record_view import view_record
from simulation.scenario import load_scenario, resolve, scenario_paths
from simulation.tracefile import JsonValue, Row
from src.clock import SimClock
from src.record.containment import resolve_under
from src.result import Err

ROOT: Final[Path] = Path(__file__).resolve().parent
"""``simulation/``: the viewer is ``viewer/index.html``, traces are ``out/*.jsonl``."""

VIEWER: Final[str] = "/viewer/index.html"

DEFAULT_PORT: Final[int] = 8765
MAX_SPEED: Final[float] = 200.0

type Message = tuple[str, Mapping[str, JsonValue]] | None
"""``(event, payload)`` for the stream, ``None`` for the end of it."""


def viewer_location(query: str) -> str:
    """Where ``/`` redirects: the viewer, with the query it was asked with.

    The query goes into a response header: it is kept on one line, whatever it holds.
    """
    kept = query.replace("\r", "").replace("\n", "")
    return VIEWER + (f"?{kept}" if kept else "")


def run_into(
    scenario_name: str, speed: float, out: queue.Queue[Message], *, sim_clock: bool = False
) -> None:
    """Run one scenario at ``speed`` x and push ``meta``, every ``row``, then ``final``.

    Deterministic simulated time by default (:class:`SleepingTicker`); with
    ``sim_clock`` the codebase's own :class:`SimClock` (real time, scaled), where
    a real pause on the host becomes a pause in the loop the runtime can see.
    """
    loaded = load_scenario(resolve(scenario_name))
    if isinstance(loaded, Err):
        out.put(("error", {"detail": loaded.error.detail}))
        out.put(None)
        return

    async def sink(row: Row) -> None:
        out.put(("row", row.to_json()))

    async def main() -> None:
        ticker = PacedTicker(SimClock(speed=speed)) if sim_clock else SleepingTicker(speed)
        session = Session(loaded.value, ticker=ticker, sink=sink)
        out.put(("meta", session.meta()))
        result = await session.run()
        for event in result.trace.events:
            out.put(("event", event.to_json()))
        out.put(("final", result.trace.final.to_json()))

    try:
        asyncio.run(main())
    except ValueError as error:
        out.put(("error", {"detail": str(error)}))
    finally:
        out.put(None)


class Handler(SimpleHTTPRequestHandler):
    """Static files from ``simulation/``, plus ``/api/scenarios`` and ``/stream``.

    Built with ``directory=ROOT`` through :func:`functools.partial` in :func:`serve`.
    """

    @override
    def log_message(self, format: str, *args: object) -> None:  # stdlib name
        """Quiet: one line per stream, not per static file."""

    @override
    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        match parts.path:
            case "" | "/":
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", viewer_location(parts.query))
                self.end_headers()
            case "/api/scenarios":
                body = json.dumps([path.stem for path in scenario_paths()]).encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            case "/stream":
                self._stream(parse_qs(parts.query))
            case "/console":
                self._console()
            case "/api/record":
                self._record(parse_qs(parts.query))
            case _:
                super().do_GET()

    def _record(self, query: Mapping[str, Sequence[str]]) -> None:
        requested = (query.get("path") or [""])[0]
        # Relative to ``simulation/``, as the viewer's ``trace`` parameter is.
        located = resolve_under(ROOT, requested.lstrip("/"))
        if located is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "outside_root")
            return
        # The record is read at the path just resolved, given as the
        # directory that holds it and its name: the text of the request
        # goes no further than this handler.
        result = view_record(located.parent, located.name)
        if isinstance(result, Err):
            self.send_error(HTTPStatus.BAD_REQUEST, result.error.detail)
            return
        body = result.value.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, query: Mapping[str, Sequence[str]]) -> None:
        name = (query.get("scenario") or [""])[0]
        try:
            speed = min(MAX_SPEED, max(0.1, float((query.get("speed") or ["20"])[0])))
        except ValueError:
            speed = 20.0
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        messages: queue.Queue[Message] = queue.Queue()
        sim_clock = (query.get("clock") or ["manual"])[0] == "sim"
        worker = threading.Thread(
            target=run_into,
            args=(name, speed, messages),
            kwargs={"sim_clock": sim_clock},
            daemon=True,
        )
        worker.start()
        try:
            while True:
                message = messages.get()
                if message is None:
                    self.wfile.write(b"event: end\ndata: {}\n\n")
                    self.wfile.flush()
                    return
                event, payload = message
                self.wfile.write(f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _console(self) -> None:
        """The console of the server's ``--console``, streamed until the browser goes."""
        server = self.server
        host, port = server.console if isinstance(server, ViewerServer) else CONSOLE
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        stream_console(http_fetch(host, port), time.sleep, self._emit)

    def _emit(self, event: str, payload: Mapping[str, JsonValue]) -> bool:
        try:
            self.wfile.write(f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True


CONSOLE: Final[tuple[str, int]] = ("127.0.0.1", 8090)
"""The console :data:`~simulation.console_view.DEFAULT_CONSOLE` names."""


class ViewerServer(ThreadingHTTPServer):
    """The viewer's server, with the console its ``/console`` follows."""

    def __init__(self, address: tuple[str, int], console: tuple[str, int]) -> None:
        super().__init__(address, partial(Handler, directory=str(ROOT)))
        self.console: tuple[str, int] = console


def serve(
    port: int = DEFAULT_PORT, host: str = "127.0.0.1", console: tuple[str, int] = CONSOLE
) -> ViewerServer:
    """A server bound and ready; the caller runs ``serve_forever``."""
    return ViewerServer((host, port), console)


class _Args(argparse.Namespace):
    port: int = DEFAULT_PORT
    console: str = DEFAULT_CONSOLE


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m simulation.live", description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--console", default=DEFAULT_CONSOLE, help="console that ?console follows")
    args = parser.parse_args(argv, namespace=_Args())
    console = console_address(args.console)
    if isinstance(console, Err):
        parser.error(console.error.detail)
    port = args.port
    server = serve(port, console=console.value)
    print(
        f"viewer on http://127.0.0.1:{port}/  (live: ?live=<scenario>&speed=20,"
        f" console {args.console}: ?console)"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # Ctrl-C is how this server is stopped: its normal end, not an error.
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

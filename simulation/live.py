"""``python -m simulation.live``: serve the 2D viewer and stream a scenario live, at N x speed.

    python -m simulation.live                       # http://127.0.0.1:8765/
    python -m simulation.live --port 9000

Then open ``http://127.0.0.1:8765/?live=manual_27_rpm&speed=20`` for a live run
(Server-Sent Events: stdlib only, no WebSocket dependency), or
``http://127.0.0.1:8765/?trace=out/manual_27_rpm.jsonl`` to play back a trace
written by ``python -m simulation.run``.

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
from collections.abc import Mapping, Sequence
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, override
from urllib.parse import parse_qs, urlsplit

from simulation.harness import PacedTicker, Session, SleepingTicker
from simulation.scenario import load_scenario, resolve, scenario_paths
from simulation.tracefile import JsonValue, Row
from src.clock import SimClock
from src.result import Err

ROOT: Final[Path] = Path(__file__).resolve().parent
"""``simulation/``: the viewer is ``viewer/index.html``, traces are ``out/*.jsonl``."""

DEFAULT_PORT: Final[int] = 8765
MAX_SPEED: Final[float] = 200.0

type Message = tuple[str, Mapping[str, JsonValue]] | None
"""``(event, payload)`` for the stream, ``None`` for the end of it."""


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
        if parts.path in {"", "/"}:
            self.send_response(HTTPStatus.FOUND)
            self.send_header(
                "Location", "/viewer/index.html" + (f"?{parts.query}" if parts.query else "")
            )
            self.end_headers()
            return
        if parts.path == "/api/scenarios":
            body = json.dumps([path.stem for path in scenario_paths()]).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parts.path == "/stream":
            self._stream(parse_qs(parts.query))
            return
        super().do_GET()

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


def serve(port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """A server bound and ready; the caller runs ``serve_forever``."""
    return ThreadingHTTPServer((host, port), partial(Handler, directory=str(ROOT)))


class _Args(argparse.Namespace):
    port: int = DEFAULT_PORT


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m simulation.live", description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    port = parser.parse_args(argv, namespace=_Args()).port
    server = serve(port)
    print(f"viewer on http://127.0.0.1:{port}/  (live: ?live=<scenario>&speed=20)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

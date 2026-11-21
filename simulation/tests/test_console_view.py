"""A running console in the 2D viewer (:mod:`simulation.console_view`, ``/console``)."""

from __future__ import annotations

import json
import threading
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from http.client import HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final, override

import pytest

from simulation import console_view as view
from simulation import live
from simulation.console_view import (
    PERIOD_S,
    RETRY_S,
    ConsoleUnreadable,
    Fetch,
    Panel,
    console_address,
    console_meta,
    console_rig,
    console_row,
    http_fetch,
    read_panel,
    read_snapshot,
    stream_console,
)
from simulation.rig import RigGeometry
from src.record.rows import JsonValue
from src.result import Err, Ok, Result
from src.units import OutputRpm

PANEL: Final[str] = json.dumps({"radius_m": 1.5, "gear_ratio": 49.79, "motor_backend": "sim"})


def snapshot(
    *,
    at: float = 100.0,
    output_rpm: float = 3.0,
    drive_state: str = "operation_enabled",
    safety: Mapping[str, JsonValue] | None = None,
    manual: bool = True,
) -> str:
    """A snapshot of the shape the console serves, with fields the viewer ignores."""
    speeds: Mapping[str, JsonValue] = {
        "motor_rpm": output_rpm * 49.79,
        "output_rpm": output_rpm,
        "hertz": output_rpm * 49.79 / 27.6,
        "g_load": 0.015,
        "resultant_g": 1.0001,
    }
    body: dict[str, JsonValue] = {
        "at": at,
        "wall_clock": 1795284004955,
        "phase": "hold",
        "mode": "manuel",
        "heart_rate": {"bpm": 77, "quality": "good", "age_s": 0.06, "stale": False, "seq": 1},
        "target_bpm": None,
        "live_bpm": 77,
        "setpoint": speeds,
        "measured": speeds,
        "drive_state": drive_state,
        "drive_status_stale": False,
        "current_a": 1.25,
        "safety": safety,
        "safety_action": "none" if safety is None else "quick_stop",
        "manual": {"target": speeds, "occupancy": "bench"} if manual else None,
    }
    return json.dumps(body)


@pytest.fixture
def rig() -> RigGeometry:
    loaded = console_rig(Panel(radius_m=1.5))
    assert isinstance(loaded, Ok)
    return loaded.value


# --------------------------------------------------------------- the address


@pytest.mark.parametrize(
    ("url", "address"),
    [
        ("http://127.0.0.1:8090", ("127.0.0.1", 8090)),
        ("http://127.0.0.1:8090/", ("127.0.0.1", 8090)),
        ("http://localhost", ("localhost", 80)),
    ],
)
def test_a_console_address_is_a_host_and_a_port(url: str, address: tuple[str, int]) -> None:
    assert console_address(url) == Ok(address)


@pytest.mark.parametrize(
    "url", ["https://127.0.0.1:8090", "http://127.0.0.1:8090/api", "http://:80", "ftp://h", "h:1"]
)
def test_anything_but_http_host_port_is_refused(url: str) -> None:
    assert isinstance(console_address(url), Err)


def test_a_port_out_of_range_is_refused() -> None:
    assert console_address("http://h:99999") == Err(
        ConsoleUnreadable("'http://h:99999' : port illisible")
    )


# ------------------------------------------------------------ what is parsed


def test_a_snapshot_keeps_what_the_viewer_needs_and_ignores_the_rest() -> None:
    read = read_snapshot(snapshot())
    assert isinstance(read, Ok)
    assert read.value.measured.output_rpm == 3.0
    assert read.value.heart_rate.bpm == 77
    assert read.value.safety is None


@pytest.mark.parametrize("text", ["not json", "{}", json.dumps({"at": "soon"})])
def test_an_unreadable_snapshot_is_said_unreadable(text: str) -> None:
    read = read_snapshot(text)
    assert isinstance(read, Err)
    assert read.error.detail.startswith("snapshot illisible")


def test_a_panel_gives_the_reference_radius() -> None:
    assert read_panel(PANEL) == Ok(Panel(radius_m=1.5))


def test_an_unreadable_panel_is_said_unreadable() -> None:
    read = read_panel(json.dumps({"gear_ratio": 49.79}))
    assert isinstance(read, Err)
    assert read.error.detail.startswith("panel illisible")


def test_a_radius_the_geometry_refuses_is_said_so() -> None:
    read = console_rig(Panel(radius_m=0.0))
    assert isinstance(read, Err)
    assert read.error.detail.startswith("geometrie inutilisable")


# ------------------------------------------------------- what the viewer sees


def test_the_header_draws_the_cad_machine_at_the_console_radius(rig: RigGeometry) -> None:
    meta = console_meta(rig)
    assert meta["reference_radius_m"] == 1.5
    assert meta["gear_ratio"] == float(rig.machine.ratio)
    assert meta["leg_tip_radius_m"] == float(rig.leg_tip_radius)
    assert meta["leg_tip_measured"] is False


def test_a_row_carries_the_console_speeds_g_and_heart_rate(rig: RigGeometry) -> None:
    read = read_snapshot(snapshot(safety={"rule": "operator_estop", "rank": 4}))
    assert isinstance(read, Ok)
    row = console_row(read.value, rig, 2.5)
    assert row.t == 2.5
    assert row.output_rpm == 3.0
    assert row.measured_motor_rpm == round(3.0 * 49.79)
    assert row.manual_target_motor_rpm == round(3.0 * 49.79)
    assert row.g_reference == 0.015
    assert row.g_leg_tip == rig.g_leg_tip(OutputRpm(3.0))
    assert row.drive_state == "OPERATION_ENABLED"
    assert row.output_enabled
    assert row.measured_fresh
    assert (row.hr_true, row.hr_live, row.hr_raw, row.hr_quality) == (None, 77, 77, "good")
    assert (row.safety_rule, row.safety_action) == ("operator_estop", "quick_stop")
    assert row.sim_state == "console"


def test_a_row_without_a_manual_target_or_a_verdict(rig: RigGeometry) -> None:
    read = read_snapshot(snapshot(drive_state="ready", manual=False))
    assert isinstance(read, Ok)
    row = console_row(read.value, rig, 0.0)
    assert row.manual_target_motor_rpm == 0
    assert row.safety_rule is None
    assert not row.output_enabled


# ------------------------------------------------------------------ the loop


class Script:
    """A console answering a list of answers in turn, and a browser leaving after N events."""

    def __init__(self, answers: Sequence[Result[str, ConsoleUnreadable]], *, events: int) -> None:
        self._answers: list[Result[str, ConsoleUnreadable]] = list(answers)  # consumed in order
        self.asked: list[str] = []
        self.sent: list[tuple[str, Mapping[str, JsonValue]]] = []
        self.paused: list[float] = []
        self._events: int = events

    def fetch(self, path: str) -> Result[str, ConsoleUnreadable]:
        self.asked.append(path)
        return self._answers.pop(0)

    def pause(self, seconds: float) -> None:
        self.paused.append(seconds)

    def emit(self, event: str, payload: Mapping[str, JsonValue]) -> bool:
        self.sent.append((event, payload))
        return len(self.sent) < self._events

    def run(self) -> None:
        fetch: Fetch = self.fetch
        stream_console(fetch, self.pause, self.emit)

    @property
    def events(self) -> list[str]:
        return [event for event, _ in self.sent]


DOWN: Final[Result[str, ConsoleUnreadable]] = Err(ConsoleUnreadable("console injoignable"))


def test_the_header_then_a_row_per_snapshot_until_the_browser_leaves() -> None:
    script = Script([Ok(PANEL), Ok(snapshot(at=10.0)), Ok(snapshot(at=10.25))], events=3)
    script.run()
    assert script.events == ["meta", "row", "row"]
    assert script.asked == [view.PANEL_PATH, view.SNAPSHOT_PATH, view.SNAPSHOT_PATH]
    assert [payload["t"] for event, payload in script.sent if event == "row"] == [0.0, 0.25]
    assert script.paused == [PERIOD_S]


def test_a_console_that_does_not_answer_is_said_and_tried_again() -> None:
    script = Script([DOWN, Ok("{}"), Ok(PANEL), DOWN, Ok("[]"), Ok(snapshot())], events=6)
    script.run()
    assert script.events == ["error", "error", "meta", "error", "error", "row"]
    assert script.sent[0][1] == {"detail": "console injoignable"}
    assert script.paused == [RETRY_S, RETRY_S, RETRY_S, RETRY_S]


def test_a_console_reporting_an_unusable_radius_is_said_and_tried_again() -> None:
    script = Script([Ok(json.dumps({"radius_m": 0.0}))], events=1)
    script.run()
    assert script.events == ["error"]


@pytest.mark.parametrize(
    ("answers", "events"),
    [([DOWN], ["error"]), ([Ok(PANEL)], ["meta"]), ([Ok(PANEL), DOWN], ["meta", "error"])],
)
def test_a_browser_gone_at_any_event_ends_the_stream(
    answers: Sequence[Result[str, ConsoleUnreadable]], events: Sequence[str]
) -> None:
    script = Script(answers, events=len(events))
    script.run()
    assert script.events == list(events)
    assert script.paused == []


def test_the_stream_time_goes_on_across_a_restart_of_the_console() -> None:
    answers = [Ok(PANEL), Ok(snapshot(at=50.0)), Ok(snapshot(at=51.0)), Ok(snapshot(at=3.0))]
    script = Script(answers, events=4)
    script.run()
    times = [payload["t"] for event, payload in script.sent if event == "row"]
    assert times == [0.0, 1.0, 1.0 + PERIOD_S]


# ---------------------------------------------------------------- over HTTP


class FakeConsole(BaseHTTPRequestHandler):
    """``/api/panel`` and ``/api/snapshot`` as a console serves them; 500 elsewhere."""

    @override
    def log_message(self, format: str, *args: object) -> None:  # stdlib name
        """Quiet."""

    def do_GET(self) -> None:  # stdlib name
        bodies = {
            view.PANEL_PATH: PANEL.encode(),
            view.SNAPSHOT_PATH: snapshot().encode(),
            "/latin": b"\xff\xfe",
        }
        body = bodies.get(self.path)
        self.send_response(500 if body is None else 200)
        self.end_headers()
        self.wfile.write(body or b"no")


@pytest.fixture
def console() -> Iterator[tuple[str, int]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeConsole)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_a_fetch_reads_a_route_of_the_console(console: tuple[str, int]) -> None:
    assert http_fetch(*console)(view.PANEL_PATH) == Ok(PANEL)


def test_a_fetch_refuses_an_answer_that_is_not_200(console: tuple[str, int]) -> None:
    assert http_fetch(*console)("/missing") == Err(ConsoleUnreadable("console /missing : HTTP 500"))


def test_a_fetch_refuses_an_answer_that_is_not_text(console: tuple[str, int]) -> None:
    read = http_fetch(*console)("/latin")
    assert isinstance(read, Err)
    assert "injoignable" in read.error.detail


def test_a_fetch_of_a_console_that_is_not_there_says_so() -> None:
    closed = ThreadingHTTPServer(("127.0.0.1", 0), FakeConsole)
    port = closed.server_address[1]
    closed.server_close()
    read = http_fetch("127.0.0.1", port)(view.PANEL_PATH)
    assert isinstance(read, Err)
    assert read.error.detail.startswith(f"console 127.0.0.1:{port} injoignable")


def test_the_viewer_server_streams_its_console(console: tuple[str, int]) -> None:
    httpd = live.serve(port=0, console=console)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/console"
        response: HTTPResponse = urllib.request.urlopen(url, timeout=30)  # pyright: ignore[reportAny]  # loopback test server; urlopen is untyped
        with response:
            assert response.headers["Content-Type"] == "text/event-stream"
            lines = [response.readline().decode().strip() for _ in range(5)]
        assert lines[0] == "event: meta"
        assert lines[3] == "event: row"
        assert json.loads(lines[4].removeprefix("data: "))["hr_live"] == 77
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_handler_outside_the_viewer_server_follows_the_default_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    followed: list[tuple[str, int]] = []

    def fetch_of(host: str, port: int) -> Fetch:
        followed.append((host, port))
        return lambda _path: DOWN

    def no_stream(fetch: Fetch, *_args: object) -> None:
        assert fetch(view.PANEL_PATH) == DOWN

    monkeypatch.setattr(live, "http_fetch", fetch_of)
    monkeypatch.setattr(live, "stream_console", no_stream)
    handler = live.Handler.__new__(live.Handler)
    handler.server = object()  # type: ignore[assignment]  # not a ViewerServer

    def ignore(*_args: object) -> None:
        return None

    handler.send_response = ignore  # type: ignore[method-assign,assignment]  # no socket
    handler.send_header = ignore  # type: ignore[method-assign,assignment]  # no socket
    handler.end_headers = ignore  # type: ignore[method-assign]  # no socket
    handler._console()  # pyright: ignore[reportPrivateUsage]
    assert followed == [live.CONSOLE]


def test_an_event_to_a_browser_that_has_gone_says_so() -> None:
    class Gone:
        def write(self, _data: bytes) -> int:
            raise ConnectionResetError

        def flush(self) -> None:
            return None

    handler = live.Handler.__new__(live.Handler)
    handler.wfile = Gone()  # type: ignore[assignment]  # a socket stand-in
    assert handler._emit("row", {"t": 0.0}) is False  # pyright: ignore[reportPrivateUsage]


def test_main_refuses_a_console_it_cannot_follow(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        live.main(["--console", "https://127.0.0.1:8090"])
    assert stopped.value.code == 2
    assert "attendu http://<hote>:<port>" in capsys.readouterr().err

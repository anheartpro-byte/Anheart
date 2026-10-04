"""The two entry points: ``python -m simulation.run`` and ``python -m simulation.live``."""

from __future__ import annotations

import json
import queue
import threading
import urllib.request
from collections.abc import Iterator
from http.client import HTTPResponse
from pathlib import Path

import pytest

import simulation.run as cli
from simulation import live
from simulation.scenario import SCENARIO_DIR
from simulation.tests.conftest import document

FAST = SCENARIO_DIR / "manual_32_rpm_refused.json"
FAIL = SCENARIO_DIR / "manual_below_min_run_refused.json"


def test_list_names_the_battery(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--list"]) == 0
    listed = capsys.readouterr().out.split()
    assert "manual_27_rpm" in listed
    assert "_profiles" not in listed


def test_one_scenario_writes_its_trace_and_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["manual_32_rpm_refused", "--csv", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "all invariants and expectations hold" in out
    assert (tmp_path / "manual_32_rpm_refused.jsonl").exists()
    assert (tmp_path / "manual_32_rpm_refused.csv").exists()


def test_a_failing_scenario_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = json.loads(FAIL.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
    broken["expect"] = {"reaches_output_rpm": 5.0}
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(broken), encoding="utf-8")
    assert cli.main([str(path), "--out", str(tmp_path)]) == 1
    assert "VIOLATIONS" in capsys.readouterr().out


def test_an_invalid_scenario_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="missing 'kind'"):
        cli.main([str(path), "--out", str(tmp_path)])


def test_no_argument_is_a_usage_error() -> None:
    with pytest.raises(SystemExit):
        cli.main([])


def test_all_writes_the_summary_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "scenario_paths", lambda: (FAST, SCENARIO_DIR / "fault_drive_already_faulted.json")
    )
    assert cli.main(["--all", "--out", str(tmp_path)]) == 0
    table = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "| manual_32_rpm_refused | manual |" in table
    assert "refused (DriveInFault)" in table
    assert "PASS" in capsys.readouterr().out
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
    assert len(summary) == 2  # pyright: ignore[reportAny]


def test_all_reports_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = json.loads(FAST.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
    broken["expect"] = {"reaches_output_rpm": 5.0}
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(broken), encoding="utf-8")
    monkeypatch.setattr(cli, "scenario_paths", lambda: (path,))
    assert cli.main(["--all", "--out", str(tmp_path)]) == 1
    assert "FAIL (1)" in (tmp_path / "summary.md").read_text(encoding="utf-8")


# -- live -------------------------------------------------------------------------


def _drain(messages: queue.Queue[live.Message]) -> Iterator[tuple[str, dict[str, object]]]:
    while True:
        message = messages.get(timeout=120)
        if message is None:
            return
        event, payload = message
        yield event, dict(payload)


@pytest.mark.parametrize(("speed", "sim_clock"), [(200.0, False), (5.0, True)])
def test_a_live_run_streams_meta_rows_events_and_final(speed: float, sim_clock: bool) -> None:
    messages: queue.Queue[live.Message] = queue.Queue()
    live.run_into("manual_32_rpm_refused", speed, messages, sim_clock=sim_clock)
    seen = list(_drain(messages))
    kinds = [event for event, _ in seen]
    assert kinds[0] == "meta"
    assert kinds[-1] == "final"
    assert kinds.count("row") > 100 if not sim_clock else kinds.count("row") > 10
    assert seen[0][1]["scenario"] == "manual_32_rpm_refused"


def test_a_live_run_of_an_unknown_scenario_says_so() -> None:
    messages: queue.Queue[live.Message] = queue.Queue()
    live.run_into("no_such_scenario", 20.0, messages)
    ((event, payload),) = list(_drain(messages))
    assert event == "error"
    assert "no_such_scenario" in str(payload["detail"])


def test_a_live_run_the_harness_refuses_says_so(tmp_path: Path) -> None:
    doc = {
        "name": "x",
        "kind": "manual",
        "duration_s": 5,
        "ecg": {"mode": "dsp"},
        "actions": [{"at_s": 1, "do": "ecg_dropout", "duration_s": 1}],
    }
    path = tmp_path / "x.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    messages: queue.Queue[live.Message] = queue.Queue()
    live.run_into(str(path), 20.0, messages)
    events = [event for event, _ in _drain(messages)]
    assert events == ["error"]


@pytest.fixture
def server() -> Iterator[str]:
    httpd = live.serve(port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(url: str) -> HTTPResponse:
    response: HTTPResponse = urllib.request.urlopen(url, timeout=120)  # noqa: S310  # pyright: ignore[reportAny]  # loopback test server; urlopen is untyped
    return response


def test_the_server_redirects_to_the_viewer_and_serves_it(server: str) -> None:
    with _get(server + "/?trace=out/x.jsonl") as response:
        assert response.url.endswith("/viewer/index.html?trace=out/x.jsonl")
        assert b"Anheart 2D Simulator" in response.read()
    with _get(server + "/") as response:
        assert response.url.endswith("/viewer/index.html")


def test_the_server_lists_the_scenarios(server: str) -> None:
    with _get(server + "/api/scenarios") as response:
        names = json.loads(response.read())  # pyright: ignore[reportAny]
    assert "manual_27_rpm" in names


@pytest.mark.parametrize("query", ["speed=200", "speed=fast", "speed=5&clock=sim"])
def test_the_stream_is_server_sent_events_until_end(server: str, query: str) -> None:
    with _get(f"{server}/stream?scenario=manual_32_rpm_refused&{query}") as response:
        assert response.headers["Content-Type"] == "text/event-stream"
        body = response.read().decode()
    events = [
        line.removeprefix("event: ") for line in body.splitlines() if line.startswith("event: ")
    ]
    assert events[0] == "meta"
    assert events[-1] == "end"
    assert "final" in events
    first = body.split("data: ", 1)[1].split("\n", 1)[0]
    assert document(first)["scenario"] == "manual_32_rpm_refused"


def test_main_serves_until_interrupted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Stub:
        closed = False

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            Stub.closed = True

    def fake_serve(port: int) -> Stub:
        assert port == 0
        return Stub()

    monkeypatch.setattr(live, "serve", fake_serve)
    assert live.main(["--port", "0"]) == 0
    assert Stub.closed
    assert "viewer on" in capsys.readouterr().out


def test_a_viewer_that_goes_away_mid_stream_is_let_go() -> None:
    """A closed browser tab is a broken pipe: the handler returns, the run is abandoned."""

    class Gone:
        def write(self, _data: bytes) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            return None

    handler = live.Handler.__new__(live.Handler)
    handler.wfile = Gone()  # type: ignore[assignment]  # a socket stand-in

    def ignore(*_args: object) -> None:
        return None

    handler.send_response = ignore  # type: ignore[method-assign,assignment]  # no socket
    handler.send_header = ignore  # type: ignore[method-assign,assignment]  # no socket
    handler.end_headers = ignore  # type: ignore[method-assign]  # no socket
    handler._stream({"scenario": ["manual_32_rpm_refused"], "speed": ["200"]})  # pyright: ignore[reportPrivateUsage]


def test_a_known_defect_is_reported_as_such(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known = json.loads(FAST.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
    known["known_defect"] = "documented"
    known["expect"] = {"reaches_output_rpm": 5.0}
    failing = tmp_path / "known.json"
    failing.write_text(json.dumps(known), encoding="utf-8")
    known["expect"] = {}
    passing = tmp_path / "fixed.json"
    passing.write_text(json.dumps(known), encoding="utf-8")
    assert cli.main([str(failing), "--out", str(tmp_path)]) == 0
    monkeypatch.setattr(cli, "scenario_paths", lambda: (failing,))
    assert cli.main(["--all", "--out", str(tmp_path)]) == 0
    assert "KNOWN DEFECT (1)" in (tmp_path / "summary.md").read_text(encoding="utf-8")
    monkeypatch.setattr(cli, "scenario_paths", lambda: (passing,))
    assert cli.main(["--all", "--out", str(tmp_path)]) == 1
    assert "FIXED?" in (tmp_path / "summary.md").read_text(encoding="utf-8")

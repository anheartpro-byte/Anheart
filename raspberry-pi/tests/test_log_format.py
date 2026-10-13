"""The console's log lines: their shape, and what they must never carry (ANH-163 EX-2).

On the Pi the console's output is the journal of its service, kept on disk
across restarts. Two things are held here:

* **the shape**: one JSON object per line, with an ``event`` field
  (``src/log_format.py``), installed by the console's entry point;
* **the content**: no heart rate, no value taken from the ECG samples and
  nothing that identifies a rider reaches a logger. Proved two ways, because
  neither is enough alone: every logging call of ``src/`` is read (what it is
  handed, by name), and a real console is run through the cases where such
  data exists (a rider refused for their age, a heart rate that trips a
  cardiac verdict) with everything it logs captured.

The operator's name is not a rider's identity and is outside this file: three
lines of the safety supervisor still name who attested and who acknowledged.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

import httpx
import pytest

from src import local_panel
from src.local_panel import EXIT_CONFIG, main, rider_words
from src.log_format import JsonLines, json_handler
from src.training.runtime import RuntimeState
from src.training.types import SignalQuality
from src.units import Bpm
from tests.test_failure_rig import (
    OPERATOR,
    PROGRAMME_ENV,
    SHORT_PROFILE,
    Rig,
    attest,
    make_rig,
)

CONSOLE: Final[Path] = Path(__file__).resolve().parents[1]
SOURCES: Final[Path] = CONSOLE / "src"

# =========================================================================
# The shape: one JSON object per line, with an `event` field
# =========================================================================


def _record(message: str, *args: object, level: int = logging.WARNING) -> logging.LogRecord:
    return logging.LogRecord("src.somewhere", level, __file__, 1, message, args, None)


def _line(record: logging.LogRecord) -> dict[str, str]:
    """One formatted record, parsed back. A second line would fail the parse."""
    text = JsonLines().format(record)
    assert "\n" not in text
    return cast("dict[str, str]", json.loads(text))


def test_ex2_a_record_is_one_json_object_whose_event_is_the_message() -> None:
    record = _record("manual session started: occupancy=%s ceiling=%d motor rpm", "bench", 300)
    record.created = 1_791_800_000.25
    line = _line(record)
    assert line == {
        "ts": "2026-10-12T10:13:20.250Z",
        "level": "WARNING",
        "logger": "src.somewhere",
        "event": "manual session started: occupancy=bench ceiling=300 motor rpm",
    }


def test_ex2_a_line_break_or_a_quote_in_a_message_stays_inside_its_line() -> None:
    line = _line(_record('drive said "%s"\nand then nothing', "ttO"))
    assert line["event"] == 'drive said "ttO"\nand then nothing'


def test_ex2_words_are_kept_as_written_not_escaped_to_ascii() -> None:
    text = JsonLines().format(_record("variateur: défaut réarmé"))
    assert "variateur: défaut réarmé" in text


def _fail() -> None:
    raise RuntimeError("injected")


def _record_of_a_failure() -> logging.LogRecord:
    try:
        _fail()
    except RuntimeError:
        return logging.LogRecord(
            "src.somewhere", logging.ERROR, __file__, 1, "the tick raised", (), sys.exc_info()
        )
    raise AssertionError("the failure was not raised")


def test_ex2_a_traceback_is_a_field_of_the_same_line() -> None:
    line = _line(_record_of_a_failure())
    assert line["event"] == "the tick raised"
    assert line["level"] == "ERROR"
    assert "RuntimeError: injected" in line["exception"]
    assert "Traceback" in line["exception"]


def test_ex2_the_entry_point_installs_that_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[int, list[logging.Handler]]] = []

    def configure(*, level: int, handlers: list[logging.Handler]) -> None:
        asked.append((level, handlers))

    monkeypatch.setattr(logging, "basicConfig", configure)
    code = main({"MOTOR_BACKEND": "warp"})
    assert code == EXIT_CONFIG
    ((level, handlers),) = asked
    assert level == logging.INFO
    assert [type(handler.formatter) for handler in handlers] == [JsonLines]


def test_ex2_the_handler_writes_to_standard_error() -> None:
    handler = json_handler()
    assert isinstance(handler, logging.StreamHandler)
    assert isinstance(handler.formatter, JsonLines)


@pytest.mark.slow
def test_ex2_everything_the_real_console_prints_is_json_with_an_event(tmp_path: Path) -> None:
    """The console itself, in a process of its own: a refused configuration, read as the
    journal receives it. Every line is one JSON object with the four fields."""
    done = subprocess.run(  # fixed argv, this interpreter, no shell
        [sys.executable, "-m", "src.local_panel"],
        cwd=CONSOLE,
        env={
            "PATH": os.environ.get("PATH", ""),
            "MOTOR_BACKEND": "warp",
            "ECG_SOURCE": "sim",
            "ARM_RADIUS_M": "1.5",
            "RECORD_ROOT": str(tmp_path / "records"),
            "MPLBACKEND": "Agg",
            "ANHEART_SOFTWARE_VERSION": "pi-0.0.0",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == EXIT_CONFIG, done.stderr
    lines = [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]
    events: list[str] = []
    for line in lines:
        parsed = cast("dict[str, str]", json.loads(line))
        assert set(parsed) == {"ts", "level", "logger", "event"}, line
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", parsed["ts"]), line
        events.append(parsed["event"])
    assert any(event.startswith("configuration: MOTOR_BACKEND: ") for event in events), events
    assert any("ANHEART_SOFTWARE_VERSION is set and no longer read" in e for e in events), events


# =========================================================================
# The content, read: what every logging call of src/ is handed
# =========================================================================

LEVELS: Final[frozenset[str]] = frozenset(
    {"debug", "info", "warning", "error", "critical", "exception", "log"}
)

HEALTH_WORDS: Final[frozenset[str]] = frozenset(
    {
        # a heart rate, however it is named
        "bpm",
        "hr",
        "heart",
        "pulse",
        "rr",
        "hrv",
        # a value taken from the samples
        "ecg",
        "millivolts",
        "mv",
        "adc",
        "samples",
        "values",
        "waveform",
        "peak",
        "spread",
        "metrics",
        "reading",
        "readings",
        # a rider
        "subject",
        "rider",
        "passenger",
        "patient",
        "age",
        "birth",
    }
)
"""Words that name health data or a rider. An identifier is cut at its underscores:
``heart_rate`` and ``subject_id`` are caught, ``sample_rate`` (a setting) is not."""


@dataclass(frozen=True, slots=True)
class LogCall:
    """One call to a logger of ``src/``: where, its format, and every name it is handed."""

    where: str
    template: str | None
    names: frozenset[str]


def _loggers(tree: ast.Module) -> frozenset[str]:
    """The names a module binds to ``logging.getLogger(...)``."""
    found: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr]
        value: ast.expr | None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        made = (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "getLogger"
        )
        if made:
            found.update(target.id for target in targets if isinstance(target, ast.Name))
    return frozenset(found)


def _names(argument: ast.expr) -> Iterator[str]:
    for inner in ast.walk(argument):
        if isinstance(inner, ast.Name):
            yield inner.id
        elif isinstance(inner, ast.Attribute):
            yield inner.attr


def _log_calls() -> list[LogCall]:
    calls: list[LogCall] = []
    for path in sorted(SOURCES.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        loggers = _loggers(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in LEVELS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in loggers
            ):
                continue
            first = node.args[0] if node.args else None
            literal = first.value if isinstance(first, ast.Constant) else None
            handed = [*node.args[1:], *(keyword.value for keyword in node.keywords)]
            calls.append(
                LogCall(
                    where=f"{path.relative_to(CONSOLE)}:{node.lineno}",
                    template=literal if isinstance(literal, str) else None,
                    names=frozenset(name for argument in handed for name in _names(argument)),
                )
            )
    return calls


def _words(identifier: str) -> frozenset[str]:
    return frozenset(word for word in identifier.lower().split("_") if word)


def test_ex2_the_walk_finds_the_console_s_logging_calls() -> None:
    """A walk that silently finds nothing would prove nothing."""
    calls = _log_calls()
    files = {call.where.split(":")[0] for call in calls}
    assert len(calls) > 100
    for expected in ("src/training/runtime.py", "src/bitalino_client.py", "src/cloud_sync.py"):
        assert expected in files


def test_ex2_every_log_call_has_a_literal_format() -> None:
    """A format built at run time could carry anything: there is none."""
    built = [call.where for call in _log_calls() if call.template is None]
    assert built == []


def test_ex2_no_log_call_is_handed_a_heart_rate_an_ecg_value_or_a_rider() -> None:
    """By name: nothing called a heart rate, a sample value or a rider is given to a logger."""
    offenders = {
        call.where: sorted(name for name in call.names if _words(name) & HEALTH_WORDS)
        for call in _log_calls()
    }
    found = {where: names for where, names in offenders.items() if names}
    assert found == {}


def test_ex2_no_log_format_speaks_of_a_heart_rate() -> None:
    spoken = [
        call.where
        for call in _log_calls()
        if call.template is not None
        and re.search(r"\b(bpm|heart rate)\b", call.template, re.IGNORECASE)
    ]
    assert spoken == []


def test_ex2_the_reading_catches_what_it_is_there_to_catch() -> None:
    """The rule itself, on names it must refuse and names it must leave alone."""
    refused = ["bpm", "heart_rate", "hr_live", "subject_id", "rider_age", "ecg_mv", "samples"]
    allowed = ["sample_rate", "output_rpm", "operator", "session_id", "channel", "rate", "value"]
    assert all(_words(name) & HEALTH_WORDS for name in refused)
    assert not any(_words(name) & HEALTH_WORDS for name in allowed)


# =========================================================================
# The content, run: a real console where the data exists
# =========================================================================

RESTING: Final[int] = 97
TOO_FAST: Final[int] = 171
"""Two heart rates no other number of this test looks like: one to start on, one above
the critical tier (158 bpm)."""

RIDER_AGE: Final[int] = 9


@dataclass
class Pulse:
    """A heart rate handed to the runtime once a second, as the ECG bridge hands it over.

    The bridge itself is left out (the rig's default treatment yields nothing):
    it only passes on a rate that an independent reading of the simulated ECG
    confirms, and this test needs rates it chooses.
    """

    rig: Rig
    seq: int = 0

    async def beat(self, seconds: int, bpm: int) -> None:
        for _ in range(seconds):
            self.seq += 1
            self.rig.panel.runtime.observe_ecg(
                self.rig.clock.monotonic(), self.seq, SignalQuality.GOOD, Bpm(bpm)
            )
            await self.rig.tick(1.0)


async def test_ex2_a_session_with_a_rider_leaves_no_health_data_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A rider refused for their age, then a person on board whose heart rate trips a
    cardiac verdict. The operator is shown the age and the rate; the log gets neither."""
    rig, _ = make_rig(tmp_path, env=PROGRAMME_ENV)
    pulse = Pulse(rig)
    runtime = rig.panel.runtime
    with caplog.at_level(logging.DEBUG):
        async with rig.http() as session:
            await pulse.beat(10, RESTING)
            await attest(session)
            refused = await session.post(
                "/api/session/start",
                json={
                    "profile_id": SHORT_PROFILE,
                    "operator": OPERATOR,
                    "total_duration_s": None,
                    "subject_age": RIDER_AGE,
                },
            )
            assert refused.status_code == httpx.codes.ACCEPTED, refused.text
            await rig.tick(0.4)
            started = await session.post(
                "/api/manual/start", json={"occupancy": "occupied", "operator": OPERATOR}
            )
            assert started.status_code == httpx.codes.ACCEPTED, started.text
            await pulse.beat(2, RESTING)
            assert runtime.state is RuntimeState.RUNNING, rig.refusals()
            await pulse.beat(30, TOO_FAST)
    # The data existed, and went where it is allowed to go: the operator's screen.
    assert any(rider_words(RIDER_AGE) in said for said in rig.refusals()), rig.refusals()
    verdict = runtime.standing
    assert verdict is not None, "the heart rate tripped no verdict: nothing was put to the test"
    assert verdict.rule.startswith("hr_"), verdict
    assert str(TOO_FAST) in verdict.detail, verdict
    # None of it is in a single line the console logged, at any level.
    logged = [record.getMessage() for record in caplog.records if record.name.startswith("src.")]
    assert len(logged) > 5, logged
    assert any(verdict.rule in line for line in logged), "the verdict itself is logged, by name"
    for line in logged:
        assert not re.search(rf"\b({RESTING}|{TOO_FAST})\b", line), line
        assert "bpm" not in line.lower(), line
        assert rider_words(RIDER_AGE) not in line, line
        assert SHORT_PROFILE not in line, line
        assert verdict.detail not in line, line
    await rig.panel.close()


def test_ex2_the_console_s_banner_names_its_cardiac_tiers_which_are_settings(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one place the journal says ``bpm``: the two thresholds of the configuration, in
    the start banner. A setting of the machine, not a measurement of anybody."""

    handed: list[object] = []

    async def never_run(_config: object, *, stopped: object, notify_socket: str | None) -> int:
        assert notify_socket is None, "no service manager started this test"
        handed.append(stopped)  # kept, never called: it would end this very process
        return local_panel.EXIT_OK

    monkeypatch.setattr(local_panel, "run_console", never_run)
    with caplog.at_level(logging.INFO, logger="src.local_panel"):
        code = main({"MOTOR_BACKEND": "sim", "ECG_SOURCE": "sim", "ARM_RADIUS_M": "1.5"})
    assert code == local_panel.EXIT_OK
    banner = [r.getMessage() for r in caplog.records if "paliers" in r.getMessage()]
    assert len(banner) == 1
    assert "paliers 148/158 bpm" in banner[0]

"""A running console in the 2D viewer: ``/console`` of ``python -m simulation.live``.

The viewer draws a geometry header and rows. A console publishes neither: it serves
``/api/panel`` (its radius and gear ratio) and ``/api/snapshot`` (speeds, g, heart rate,
drive state) at its own address, and a browser page from another origin may not read
them. So the viewer's server reads both, server side, and streams them in the form of a
scenario: one ``meta``, then a ``row`` per snapshot.

Read only: the only requests sent to the console are ``GET`` of those two routes. The
leg tip is the CAD bound, as for a scenario without an override: the console does not
publish it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass as plain_dataclass
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException
from typing import Final
from urllib.parse import urlsplit

from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass

from simulation.rig import RigGeometry, load_geometry
from src.record.rows import JsonValue, Row
from src.result import Err, Ok, Result
from src.units import Metres, OutputRpm

DEFAULT_CONSOLE: Final[str] = "http://127.0.0.1:8090"
PANEL_PATH: Final[str] = "/api/panel"
SNAPSHOT_PATH: Final[str] = "/api/snapshot"
PERIOD_S: Final[float] = 0.2
"""Between two snapshots: five a second, the viewer's own rate is the browser's frame."""
RETRY_S: Final[float] = 1.0
"""After a console that did not answer, or answered something unreadable."""
FETCH_TIMEOUT_S: Final[float] = 2.0
MAX_ANSWER_BYTES: Final[int] = 1 << 20

CONFIG = ConfigDict(allow_inf_nan=False, extra="ignore")


@plain_dataclass(frozen=True, slots=True)
class ConsoleUnreadable:
    """The console did not answer, or not with what this module reads."""

    detail: str


@dataclass(frozen=True, slots=True, kw_only=True, config=CONFIG)
class Speeds:
    motor_rpm: float
    output_rpm: float
    hertz: float
    g_load: float


@dataclass(frozen=True, slots=True, kw_only=True, config=CONFIG)
class HeartRate:
    bpm: int | None
    quality: str


@dataclass(frozen=True, slots=True, kw_only=True, config=CONFIG)
class Verdict:
    rule: str


@dataclass(frozen=True, slots=True, kw_only=True, config=CONFIG)
class Manual:
    target: Speeds


@dataclass(frozen=True, slots=True, kw_only=True, config=CONFIG)
class Snapshot:
    """What the viewer needs of ``GET /api/snapshot``; the rest is ignored."""

    at: float
    phase: str
    mode: str
    heart_rate: HeartRate
    live_bpm: int | None
    target_bpm: int | None
    setpoint: Speeds
    measured: Speeds
    drive_state: str
    drive_status_stale: bool
    current_a: float | None
    safety: Verdict | None
    safety_action: str
    manual: Manual | None = None


@dataclass(frozen=True, slots=True, kw_only=True, config=CONFIG)
class Panel:
    """What the viewer needs of ``GET /api/panel``: the reference radius."""

    radius_m: float


SNAPSHOT: Final[TypeAdapter[Snapshot]] = TypeAdapter(Snapshot)
PANEL: Final[TypeAdapter[Panel]] = TypeAdapter(Panel)

type Fetch = Callable[[str], Result[str, ConsoleUnreadable]]
"""``GET`` of a path of the console: its body, or why there is none."""
type Emit = Callable[[str, Mapping[str, JsonValue]], bool]
"""Sends one event to the browser; ``False`` once the browser has gone."""


def console_address(url: str) -> Result[tuple[str, int], ConsoleUnreadable]:
    """``http://host[:port]`` and nothing else: the host and the port it names."""
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname or parts.path not in {"", "/"}:
        return Err(ConsoleUnreadable(f"{url!r} : attendu http://<hote>:<port>"))
    try:
        port = parts.port
    except ValueError:
        return Err(ConsoleUnreadable(f"{url!r} : port illisible"))
    return Ok((parts.hostname, port or 80))


def http_fetch(host: str, port: int) -> Fetch:
    """A :data:`Fetch` on the console at ``host:port``."""

    def fetch(path: str) -> Result[str, ConsoleUnreadable]:
        connection = HTTPConnection(host, port, timeout=FETCH_TIMEOUT_S)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read(MAX_ANSWER_BYTES)
            if response.status != HTTPStatus.OK:
                return Err(ConsoleUnreadable(f"console {path} : HTTP {response.status}"))
            return Ok(body.decode("utf-8"))
        except (OSError, HTTPException, UnicodeDecodeError) as error:
            return Err(ConsoleUnreadable(f"console {host}:{port} injoignable ({error})"))
        finally:
            connection.close()

    return fetch


def read_snapshot(text: str) -> Result[Snapshot, ConsoleUnreadable]:
    try:
        return Ok(SNAPSHOT.validate_json(text))
    except ValidationError as error:
        return Err(ConsoleUnreadable(f"snapshot illisible ({error.error_count()} erreurs)"))


def read_panel(text: str) -> Result[Panel, ConsoleUnreadable]:
    try:
        return Ok(PANEL.validate_json(text))
    except ValidationError as error:
        return Err(ConsoleUnreadable(f"panel illisible ({error.error_count()} erreurs)"))


def console_rig(panel: Panel) -> Result[RigGeometry, ConsoleUnreadable]:
    """The CAD geometry at the console's reference radius."""
    loaded = load_geometry(reference_radius=Metres(panel.radius_m))
    if isinstance(loaded, Err):
        return Err(ConsoleUnreadable(f"geometrie inutilisable : {loaded.error.detail}"))
    return Ok(loaded.value)


def console_meta(rig: RigGeometry) -> Mapping[str, JsonValue]:
    """The header the viewer draws the machine from, as a scenario's carries it."""
    return {
        "scenario": "console",
        "description": "console en direct",
        "reference_radius_m": float(rig.reference_radius),
        "leg_tip_radius_m": float(rig.leg_tip_radius),
        "leg_tip_measured": rig.leg_tip_measured,
        "arm_tip_radius_m": float(rig.arm_tip_radius),
        "capsule_near_radius_m": float(rig.capsule_near_radius),
        "capsule_far_radius_m": float(rig.capsule_far_radius),
        "counterweight_radius_m": float(rig.counterweight_radius),
        "gear_ratio": float(rig.machine.ratio),
    }


def console_row(snapshot: Snapshot, rig: RigGeometry, t: float) -> Row:
    """One snapshot as a viewer row at ``t`` s of the stream.

    No true heart rate exists outside a simulation: ``hr_true`` is ``None``, and the
    simulated drive's own state is not published: ``sim_state`` says ``console``.
    """
    measured, setpoint = snapshot.measured, snapshot.setpoint
    motor = round(measured.motor_rpm)
    target = 0 if snapshot.manual is None else round(snapshot.manual.target.motor_rpm)
    return Row(
        t=t,
        state=snapshot.mode,
        mode=snapshot.mode,
        phase=snapshot.phase,
        drive_state=snapshot.drive_state.upper(),
        sim_state="console",
        setpoint_motor_rpm=round(setpoint.motor_rpm),
        lfrd_motor_rpm=motor,
        measured_motor_rpm=motor,
        measured_fresh=not snapshot.drive_status_stale,
        output_rpm=measured.output_rpm,
        hertz=measured.hertz,
        g_reference=measured.g_load,
        g_leg_tip=rig.g_leg_tip(OutputRpm(measured.output_rpm)),
        setpoint_output_rpm=setpoint.output_rpm,
        setpoint_g_leg_tip=rig.g_leg_tip(OutputRpm(setpoint.output_rpm)),
        manual_target_motor_rpm=target,
        hr_true=None,
        hr_live=snapshot.live_bpm,
        target_bpm=snapshot.target_bpm,
        safety_action=snapshot.safety_action,
        safety_rule=None if snapshot.safety is None else snapshot.safety.rule,
        output_enabled=snapshot.drive_state == "operation_enabled",
        silent=False,
        current_a=snapshot.current_a,
        hr_raw=snapshot.heart_rate.bpm,
        hr_quality=snapshot.heart_rate.quality,
    )


def stream_console(
    fetch: Fetch, pause: Callable[[float], None], emit: Emit, *, period_s: float = PERIOD_S
) -> None:
    """``meta`` once the console's radius is read, then a ``row`` per snapshot.

    Runs until :data:`Emit` says the browser has gone. A console that does not answer
    is an ``error`` event and another try after :data:`RETRY_S`. The stream's time
    follows the console's own clock, and keeps going forward across a restart of the
    console, whose clock then starts again.
    """
    rig: RigGeometry | None = None
    last: tuple[float, float] | None = None  # (console clock, stream time) of the last row
    while True:
        if rig is None:
            read = _rig(fetch)
            if isinstance(read, Err):
                if not emit("error", {"detail": read.error.detail}):
                    return
                pause(RETRY_S)
                continue
            rig = read.value
            if not emit("meta", console_meta(rig)):
                return
        snapshot = _snapshot(fetch)
        if isinstance(snapshot, Err):
            if not emit("error", {"detail": snapshot.error.detail}):
                return
            pause(RETRY_S)
            continue
        at = snapshot.value.at
        t = 0.0 if last is None else last[1] + (at - last[0] if at >= last[0] else period_s)
        last = (at, t)
        if not emit("row", console_row(snapshot.value, rig, t).to_json()):
            return
        pause(period_s)


def _rig(fetch: Fetch) -> Result[RigGeometry, ConsoleUnreadable]:
    answer = fetch(PANEL_PATH)
    if isinstance(answer, Err):
        return answer
    panel = read_panel(answer.value)
    if isinstance(panel, Err):
        return panel
    return console_rig(panel.value)


def _snapshot(fetch: Fetch) -> Result[Snapshot, ConsoleUnreadable]:
    answer = fetch(SNAPSHOT_PATH)
    if isinstance(answer, Err):
        return answer
    return read_snapshot(answer.value)

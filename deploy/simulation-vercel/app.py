"""The simulation engine as a hosted web app (Vercel, FastAPI).

Same engine as ``python -m simulation.live``: the viewer page, the scenario
list, and a Server-Sent Events stream that runs one scenario of the REAL
runtime (``raspberry-pi/src``) against the simulated drive and physiology.

It never talks to a machine. There is no Modbus port, no BITalino and no Convex
link in this process: everything a scenario touches is a model.

Differences with the local server, all forced by a hosted function:

- scenarios are accepted by NAME only, from the shipped list (the local server
  also accepts a file path, which a public endpoint must not);
- ``dsp`` scenarios (the BioSPPy path, about 50 ms of CPU per simulated second)
  are listed as unavailable: BioSPPy and its dependencies are not installed;
- the speed is kept at or above ``MIN_SPEED`` so a 45-minute scenario ends
  before the function's time limit;
- ``clock=sim`` is ignored: that clock turns a host pause into a loop stall,
  which is meaningless on a shared host.

Run it locally from the assembled ``dist/`` directory (see ``build.sh``):

    uvicorn app:app --port 8765
"""

from __future__ import annotations

import json
import math
import queue
import threading
from collections.abc import Iterator, Mapping, Sequence
from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from src.result import Err

from simulation.live import MAX_SPEED, Message, run_into
from simulation.scenario import EcgMode, load_scenario, scenario_paths
from simulation.tracefile import JsonValue

MIN_SPEED: Final[float] = 10.0
"""2700 s of scenario at 10x is 270 s of wall clock, under the 300 s limit."""

DEFAULT_SPEED: Final[float] = 20.0

app = FastAPI(title="Anheart - moteur de simulation", docs_url=None, redoc_url=None)


def _catalogue() -> Mapping[str, bool]:
    """Every shipped scenario by name, and whether it can run here (not ``dsp``)."""
    runnable: dict[str, bool] = {}
    for path in scenario_paths():
        loaded = load_scenario(path)
        if isinstance(loaded, Err):
            continue
        runnable[path.stem] = loaded.value.ecg.mode is not EcgMode.DSP
    return runnable


CATALOGUE: Final[Mapping[str, bool]] = _catalogue()


def _frame(event: str, payload: Mapping[str, JsonValue]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


def _clamp_speed(raw: str) -> float:
    try:
        speed = float(raw)
    except ValueError:
        return DEFAULT_SPEED
    if math.isnan(speed):
        return DEFAULT_SPEED
    return min(MAX_SPEED, max(MIN_SPEED, speed))


def _refusal(name: str) -> str | None:
    """Why ``name`` cannot run here, or ``None`` when it can."""
    if name not in CATALOGUE:
        return f"scenario inconnu: {name!r}"
    if not CATALOGUE[name]:
        return (
            f"{name}: scenario en mode dsp, indisponible sur la version hebergee "
            "(lancez-le en local avec python -m simulation.live)"
        )
    return None


def _events(name: str, speed: float) -> Iterator[bytes]:
    refusal = _refusal(name)
    if refusal is not None:
        yield _frame("error", {"detail": refusal})
        yield _frame("end", {})
        return
    messages: queue.Queue[Message] = queue.Queue()
    threading.Thread(target=run_into, args=(name, speed, messages), daemon=True).start()
    while True:
        message = messages.get()
        if message is None:
            yield _frame("end", {})
            return
        event, payload = message
        yield _frame(event, payload)


@app.get("/")
def root(request: Request) -> RedirectResponse:
    """To the viewer, keeping the query (``/?live=manual_27_rpm&speed=20``)."""
    query = request.url.query
    return RedirectResponse("/viewer/index.html" + (f"?{query}" if query else ""), status_code=302)


@app.get("/api/scenarios")
def scenarios() -> Sequence[str]:
    """The scenarios that can run here, by name (the viewer fills its list with it)."""
    return [name for name, runnable in CATALOGUE.items() if runnable]


@app.get("/api/catalogue")
def catalogue() -> Mapping[str, bool]:
    """Every shipped scenario, with ``false`` for the ones that only run locally."""
    return CATALOGUE


@app.get("/stream")
def stream(scenario: str = "", speed: str = "20") -> StreamingResponse:
    return StreamingResponse(
        _events(scenario, _clamp_speed(speed)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

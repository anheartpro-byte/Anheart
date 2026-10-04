"""Shared fixtures: every scenario file is run ONCE per test session and cached.

The battery asserts several independent things about each run (invariants,
expectations, trace round-trip); running a 30-minute programme once per
assertion would multiply the wall time for no extra evidence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from simulation.harness import RunResult, run_scenario
from simulation.jsondoc import as_mapping, as_sequence, load_object
from simulation.scenario import Scenario, load_scenario
from src.result import Err

_CACHE: Final[dict[Path, RunResult]] = {}


def load(path: Path) -> Scenario:
    """A scenario file that must parse (a test failure names every problem)."""
    loaded = load_scenario(path)
    if isinstance(loaded, Err):
        raise AssertionError(f"{path.name}: {loaded.error.detail}")
    return loaded.value


def run_file(path: Path) -> RunResult:
    """The cached run of one scenario file."""
    cached = _CACHE.get(path)
    if cached is None:
        cached = asyncio.run(run_scenario(load(path)))
        _CACHE[path] = cached
    return cached


def run(scenario: Scenario) -> RunResult:
    """An uncached run of an in-memory scenario (property tests)."""
    return asyncio.run(run_scenario(scenario))


def document(raw: str) -> Mapping[str, object]:
    """A JSON object, typed (``json.loads`` is ``Any``)."""
    loaded = load_object(raw)
    if isinstance(loaded, str):
        raise AssertionError(loaded)
    return loaded


def obj(value: object) -> Mapping[str, object]:
    """``value`` as a JSON object, or a test failure."""
    mapping = as_mapping(value)
    if mapping is None:
        raise AssertionError(f"not an object: {value!r}")
    return mapping


def seq(value: object) -> Sequence[object]:
    """``value`` as a JSON array, or a test failure."""
    items = as_sequence(value)
    if items is None:
        raise AssertionError(f"not an array: {value!r}")
    return items


def num(value: object) -> float:
    """``value`` as a JSON number, or a test failure."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AssertionError(f"not a number: {value!r}")
    return float(value)

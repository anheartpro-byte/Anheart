"""The coast-down hold kept across a restart of the console (ANH-92).

A console that restarts while a shaft may still coast (it was killed, it froze, it
restarts for a configuration) would otherwise come back with no hold: the shaft it
last read turning is unknown to the new process, and RFRD reads 0 with the stage off.
So each hold is written as it starts, the instant it ends on the wall clock and the
whole of it in seconds, and the file is removed once it is over.

At the next start: with a wall clock a time service keeps (``clock_trusted``), the hold
runs to its stored end; without one, its whole stored length again, from the start.
A file that is there and cannot be read is the longest hold there is. Nothing here
ever shortens a hold, and none is ever longer than :func:`~src.motor.coast.hold_cap`.
"""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass as validated

from src.clock import Clock
from src.console_access import replace_file
from src.kept_file import read_kept
from src.record.writer import describe_os_error
from src.result import Err
from src.training.runtime import TrainingRuntime
from src.units import Monotonic, Seconds, UnixMillis

_logger: logging.Logger = logging.getLogger(__name__)

COAST_HOLD_FILE: Final[str] = "coast_hold.json"

HOLD_FILE_MAX_BYTES: Final[int] = 4096
"""A few dozen bytes of JSON: a larger file is not one this wrote."""

MILLIS_PER_SECOND: Final[int] = 1000


@validated(frozen=True, config=ConfigDict(extra="forbid", strict=True))
class StoredHold:
    """One hold as written: its end on the wall clock, and its whole length."""

    until_unix_ms: Annotated[int, Field(ge=0)]
    remaining_s: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    schema_version: Literal[1] = 1


_STORED: Final[TypeAdapter[StoredHold]] = TypeAdapter(StoredHold)


def kept_hold(path: Path, *, now: UnixMillis, trusted: bool, cap: Seconds) -> Seconds | None:
    """The hold this start keeps from the previous process; ``None``: none. Startup only.

    ``trusted``: whether a time service keeps the wall clock now. Never more than ``cap``.
    """
    try:
        stored = _STORED.validate_json(read_kept(path, HOLD_FILE_MAX_BYTES))
    except FileNotFoundError:
        return None
    except (OSError, ValidationError) as error:
        _logger.warning("coast-down hold kept unreadable (%s): the longest hold", _why(error))
        return cap
    if not trusted:
        return Seconds(min(stored.remaining_s, cap)) if stored.remaining_s > 0.0 else None
    left = (stored.until_unix_ms - now) / MILLIS_PER_SECOND
    return Seconds(min(left, cap)) if left > 0.0 else None


def _why(error: OSError | ValidationError) -> str:
    return describe_os_error(error) if isinstance(error, OSError) else "not a hold this wrote"


class HoldKeeper:
    """Writes the runtime's hold as it starts and removes it once over, on a thread of its own.

    Never waited on by the control loop: each change goes to one worker thread, in
    order, so a removal never overtakes the write before it; :meth:`close` waits for
    the last one.
    """

    __slots__ = ("_closed", "_kept", "_path", "_synced", "_worker")

    def __init__(self, path: Path) -> None:
        self._path: Path = path
        self._kept: Monotonic | None = None
        # Not yet: the first call writes or removes whatever the previous process left.
        self._synced: bool = False
        self._worker: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="coast-hold"
        )
        self._closed: bool = False

    def keep(self, runtime: TrainingRuntime, clock: Clock, now: Monotonic) -> None:
        """After a tick: the file follows the hold, written once per hold, never per tick."""
        left = runtime.coast_hold_remaining(now)
        until = None if left is None else runtime.coast_hold_until
        if self._closed or (self._synced and until == self._kept):
            return
        self._synced = True
        self._kept = until
        if left is None:
            _ = self._worker.submit(_remove, self._path)
            return
        content = _STORED.dump_json(
            StoredHold(
                until_unix_ms=clock.unix_millis() + math.ceil(left * MILLIS_PER_SECOND),
                remaining_s=float(left),
            )
        )
        _ = self._worker.submit(_write, self._path, content)

    def close(self) -> None:
        """Wait for the last change to reach the disk. Blocking: at the console's exit.

        A tick after it (a stop that comes back late) keeps nothing more: what is on the
        disk is the last hold the console ran with."""
        self._closed = True
        self._worker.shutdown(wait=True)


def _write(path: Path, content: bytes) -> None:
    written = replace_file(path, content)
    if isinstance(written, Err):
        _logger.warning("coast-down hold not kept across a restart: %s", written.error)


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        _logger.warning("coast-down hold file not removed: %s", describe_os_error(error))

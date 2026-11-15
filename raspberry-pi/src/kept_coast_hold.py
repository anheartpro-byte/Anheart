"""The coast-down hold kept across a restart of the console (ANH-92).

A console that restarts while a shaft may still coast (it was killed, it froze, it
restarts for a configuration) would otherwise come back with no hold: the shaft it
last read turning is unknown to the new process, and RFRD reads 0 with the stage off.
So each hold is written as it starts: the instant it ends on the wall clock, the whole
of it in seconds, and whether a time service kept the wall clock then. The file is
removed once the hold is over.

At the next start, the hold runs again from the first read that shows the stage off
(:meth:`~src.training.runtime.TrainingRuntime.expect_coast`): to its stored end when a
time service kept the wall clock both when it was written and now; otherwise its whole
stored length again. A hold no read had started yet (one the process before was itself
told to expect) is written with its whole length, and the clock not believed. A file
that is there and cannot be read is the longest hold there is. Nothing here ever
shortens a hold, and none is ever longer than :func:`~src.motor.coast.longest_hold`,
whatever ``COAST_TAU_S`` the restarted console runs on.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
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
    """One hold as written: its end on the wall clock, its whole length, and whether a
    time service kept the wall clock that end was read on."""

    until_unix_ms: Annotated[int, Field(ge=0)]
    remaining_s: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    clock_trusted: bool
    schema_version: Literal[1] = 1


_STORED: Final[TypeAdapter[StoredHold]] = TypeAdapter(StoredHold)


def kept_hold(path: Path, *, now: UnixMillis, trusted: bool, cap: Seconds) -> Seconds | None:
    """The hold this start keeps from the previous process; ``None``: none. Startup only.

    ``trusted``: whether a time service keeps the wall clock now. The stored end is
    believed only when one kept it at the write too: a clock set right in between would
    otherwise move the end by however wrong it was. Never more than ``cap``.
    """
    try:
        stored = _STORED.validate_json(read_kept(path, HOLD_FILE_MAX_BYTES))
    except FileNotFoundError:
        return None
    except (OSError, ValidationError) as error:
        _logger.warning("coast-down hold kept unreadable (%s): the longest hold", _why(error))
        return cap
    if not (trusted and stored.clock_trusted):
        return Seconds(min(stored.remaining_s, cap)) if stored.remaining_s > 0.0 else None
    left = (stored.until_unix_ms - now) / MILLIS_PER_SECOND
    return Seconds(min(left, cap)) if left > 0.0 else None


def stored_hold(hold: Seconds, *, now: UnixMillis, trusted: bool) -> bytes:
    """The file for a hold of ``hold`` from ``now``, the wall clock ``trusted`` or not."""
    return _STORED.dump_json(
        StoredHold(
            until_unix_ms=now + math.ceil(hold * MILLIS_PER_SECOND),
            remaining_s=float(hold),
            clock_trusted=trusted,
        )
    )


def _why(error: OSError | ValidationError) -> str:
    return describe_os_error(error) if isinstance(error, OSError) else "not a hold this wrote"


class HoldKeeper:
    """Writes the runtime's hold as it starts and removes it once over, on a thread of its own.

    Never waited on by the control loop: each change goes to one worker thread, in
    order, so a removal never overtakes the write before it; :meth:`close` waits for
    the last one, and says whether it reached the disk.
    """

    __slots__ = ("_closed", "_kept", "_landed", "_path", "_synced", "_trusted", "_worker")

    def __init__(self, path: Path, trusted: Callable[[], bool]) -> None:
        """``trusted``: whether a time service keeps the wall clock, asked at each write."""
        self._path: Path = path
        self._trusted: Callable[[], bool] = trusted
        self._kept: tuple[Monotonic | None, Seconds | None] = (None, None)
        # Not yet: the first call writes or removes whatever the previous process left.
        self._synced: bool = False
        # Written by the worker thread, read once it is shut down (close).
        self._landed: bool = True
        self._worker: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="coast-hold"
        )
        self._closed: bool = False

    def keep(self, runtime: TrainingRuntime, clock: Clock, now: Monotonic) -> None:
        """After a tick: the file follows the hold, written once per hold, never per tick.

        A hold no read has started yet (:attr:`~TrainingRuntime.coast_expected`) is written
        too, whole and with the clock not believed: none of it has run."""
        left = runtime.coast_hold_remaining(now)
        expected = runtime.coast_expected
        kept = (None if left is None else runtime.coast_hold_until, expected)
        if self._closed or (self._synced and kept == self._kept):
            return
        self._synced = True
        self._kept = kept
        hold = max((part for part in (left, expected) if part is not None), default=None)
        if hold is None:
            _ = self._worker.submit(self._remove)
            return
        content = stored_hold(
            hold, now=clock.unix_millis(), trusted=expected is None and self._trusted()
        )
        _ = self._worker.submit(self._write, content)

    def close(self) -> bool:
        """Wait for the last change to reach the disk. Blocking: at the console's exit.

        Whether it did: ``False`` when the hold the next start needs is not there. A tick
        after it (a stop that comes back late) keeps nothing more: what is on the disk is
        the last hold the console ran with."""
        self._closed = True
        self._worker.shutdown(wait=True)
        return self._landed

    def _write(self, content: bytes) -> None:
        written = replace_file(self._path, content)
        self._landed = not isinstance(written, Err)
        if isinstance(written, Err):
            _logger.warning("coast-down hold not kept across a restart: %s", written.error)

    def _remove(self) -> None:
        # Landed either way: a file left behind is a hold the next start runs for nothing.
        self._landed = True
        try:
            self._path.unlink(missing_ok=True)
        except OSError as error:
            _logger.warning("coast-down hold file not removed: %s", describe_os_error(error))

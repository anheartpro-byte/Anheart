"""One-session nonblocking producer boundary with a single filesystem owner."""

from collections import deque
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from threading import Event as Signal
from threading import Lock, Thread
from typing import Final, assert_never

from src.clock import Clock
from src.record.budget import (
    FIXED_OVERHEAD,
    MAX_MESSAGE,
    MAX_SEQUENCE,
    bounded_ending,
    bounded_manifest,
    retained_charge,
)
from src.record.messages import (
    Ending,
    Loss,
    Message,
    Prepared,
    SequenceNumber,
    Status,
    Stream,
)
from src.record.schema import Manifest, RecordError
from src.record.storage import ByteCount, Generation
from src.record.worker import Worker
from src.result import Err, Ok, Result
from src.units import Seconds

DEFAULT_BYTES: Final = ByteCount(16 * 1024 * 1024)
CHECKPOINT_TARGET: Final = 1.0
CHECKPOINT_DEADLINE: Final = 2.0


@dataclass(frozen=True, slots=True)
class Limits:
    items: int = 4096
    resident_bytes: ByteCount = DEFAULT_BYTES


DEFAULT_LIMITS: Final = Limits()


class Service:
    """Mutable queue/lifecycle state; locks protect memory only, never disk work."""

    def __init__(self, root: Path, clock: Clock, limits: Limits = DEFAULT_LIMITS) -> None:
        self._root = root
        self._clock = clock
        self._limits = limits
        self._lock = Lock()
        self._health_lock = Lock()
        self._wake = Signal()
        self.preparation_done = Signal()
        self.finished = Signal()
        self._queue: deque[tuple[Message, ByteCount]] = deque()
        self._resident = 0
        self._request: Manifest | None = None
        self._ending: Ending | None = None
        self._prepared: Prepared | None = None
        self._generation = Generation(0)
        self._checkpoint_at = clock.monotonic()
        self._losses = {stream: Loss() for stream in Stream}
        self._degraded = False
        self._error: RecordError | None = None
        self._closing = False
        self._worker = Thread(target=self._run, name="record-writer", daemon=True)
        self._worker.start()

    def prepare(self, manifest: Manifest, generation: Generation) -> bool:
        if not bounded_manifest(manifest) or not 1 <= generation <= MAX_SEQUENCE:
            self._degrade()
            return False
        with self._lock:
            if self._generation or self._closing:
                return False
            self._generation = generation
            self._request = manifest
            self._wake.set()
        return True

    @property
    def prepared(self) -> Prepared | None:
        with self._lock:
            return self._prepared

    def try_submit(self, message: Message) -> bool:
        if self._lock.acquire(blocking=False):
            try:
                charge = retained_charge(message)
                if (
                    self._prepared is not None
                    and self._prepared.path is not None
                    and not self._closing
                    and charge <= MAX_MESSAGE
                    and len(self._queue) < self._limits.items
                    and self._resident + charge <= self._limits.resident_bytes
                ):
                    self._queue.append((message, charge))
                    self._resident += charge
                    self._wake.set()
                    return True
            finally:
                self._lock.release()
        self._lose(message)
        return False

    def close(self, ending: Ending) -> bool:
        if not bounded_ending(ending):
            self._degrade()
            return False
        with self._lock:
            if self._closing:
                return False
            self._ending = ending
            self._closing = True
            self._wake.set()
        return True

    def join(self, timeout: Seconds) -> bool:
        self._worker.join(max(0, min(timeout, CHECKPOINT_TARGET)))
        completed = self.finished.is_set()
        if not completed:
            self._degrade()
        return completed

    def status(self) -> Status:
        with self._lock, self._health_lock:
            if (
                self._generation > 0
                and not self.finished.is_set()
                and self._clock.monotonic() - self._checkpoint_at >= CHECKPOINT_DEADLINE
            ):
                self._degraded = True
            return Status(
                self._generation,
                self._degraded,
                len(self._queue),
                ByteCount(self._resident + FIXED_OVERHEAD),
                self._checkpoint_at,
                tuple(self._losses.values()),
                self.finished.is_set(),
                self._error,
            )

    def _degrade(self, error: RecordError | None = None) -> None:
        with self._health_lock:
            self._degraded = True
            if self._error is None:
                self._error = error

    def _lose(self, message: Message) -> None:
        sequence = SequenceNumber(max(0, min(message.sequence, MAX_SEQUENCE)))
        at = message.at if isfinite(message.at) else self._clock.monotonic()
        with self._health_lock:
            previous = self._losses[message.stream]
            self._losses[message.stream] = Loss(
                min(MAX_SEQUENCE, previous.count + 1),
                previous.first_sequence if previous.count else sequence,
                sequence,
                previous.first_at if previous.count else at,
                at,
            )
            self._degraded = True

    def _result(self, result: Result[None, RecordError]) -> bool:
        match result:
            case Err(error):
                self._degrade(error)
                return False
            case Ok():
                return True
            case _ as unreachable:
                assert_never(unreachable)

    def _checkpoint(self, worker: Worker) -> None:
        with self._health_lock:
            losses = tuple(self._losses.items())
        if self._result(worker.checkpoint(losses)):
            with self._lock:
                now = self._clock.monotonic()
                if now - self._checkpoint_at >= CHECKPOINT_DEADLINE:
                    self._degrade()
                self._checkpoint_at = now

    def _run(self) -> None:
        worker = Worker(self._root, self._clock)
        due = self._clock.monotonic() + CHECKPOINT_TARGET
        while True:
            wait = (
                CHECKPOINT_TARGET
                if worker.writer is None
                else max(0, due - self._clock.monotonic())
            )
            self._wake.wait(wait)
            with self._lock:
                self._wake.clear()
                request, self._request = self._request, None
                item = self._queue.popleft() if self._queue else None
                ending = self._ending if item is None else None
            if request is not None:
                prepared = worker.prepare(request, self._generation)
                with self._lock:
                    self._prepared = prepared
                    self._checkpoint_at = prepared.capacity.observed_at
                if prepared.degraded:
                    self._degrade(prepared.error)
                self.preparation_done.set()
            if item is not None:
                message, charge = item
                if not self._result(worker.write(message)):
                    self._lose(message)
                with self._lock:
                    self._resident -= charge
                    if self._queue or self._closing:
                        self._wake.set()
            if worker.writer is not None and self._clock.monotonic() >= due:
                self._checkpoint(worker)
                due = self._clock.monotonic() + CHECKPOINT_TARGET
            if ending is not None:
                if worker.writer is not None:
                    self._checkpoint(worker)
                self._result(worker.close(ending))
                self.finished.set()
                return

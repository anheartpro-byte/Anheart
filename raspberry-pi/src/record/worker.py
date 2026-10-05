from pathlib import Path
from typing import assert_never

from src.clock import Clock
from src.record.ecg import RawBlock
from src.record.messages import Ending, Loss, Message, Payload, Prepared, Sensors, Stream
from src.record.rows import Row
from src.record.schema import DriveFrame, Event, EventKind, Manifest, RecordError
from src.record.storage import (
    Capacity,
    Generation,
    capacity,
    failure_detail,
    private_root,
    private_supported,
)
from src.record.writer import Writer
from src.result import Err, Ok, Result


class Worker:
    """Mutable disk state accessed exclusively by the service's dedicated thread."""

    def __init__(self, root: Path, clock: Clock) -> None:
        self.root = root
        self.clock = clock
        self.writer: Writer | None = None
        self.warned = 0

    def prepare(self, manifest: Manifest, generation: Generation) -> Prepared:
        if not private_supported():
            return Prepared(
                Capacity(generation, self.clock.monotonic(), None),
                None,
                True,
                RecordError("create", "private_storage_unavailable"),
            )
        observed = capacity(
            self.root if self.root.exists() else self.root.parent, self.clock, generation
        )
        failure: RecordError | None = None
        if observed.refusal is None:
            try:
                private_root(self.root)
            except OSError as storage_error:
                failure = RecordError("create", failure_detail(storage_error))
            else:
                match Writer.create(self.root, manifest, require_private=True):
                    case Ok(value):
                        self.writer = value
                    case Err(error):
                        failure = error
                    case _ as unreachable:
                        assert_never(unreachable)
        return Prepared(
            observed, str(self.writer.path) if self.writer else None, failure is not None, failure
        )

    def write(self, message: Message) -> Result[None, RecordError]:
        try:
            return self._append(message.payload)
        except ValueError as error:
            return Err(RecordError("append", type(error).__name__))

    def _append(self, payload: Payload) -> Result[None, RecordError]:
        if self.writer is None:
            return Err(RecordError("append", "unavailable"))
        match payload:
            case Row() as row:
                return self.writer.tick(row)
            case Event() as event:
                return self.writer.event(event)
            case DriveFrame() as frame:
                return self.writer.frame(frame)
            case RawBlock() as raw:
                return self.writer.raw(raw)
            case Sensors() as sensors:
                return self.writer.sensors(sensors.at, sensors.readings)
            case _ as unreachable:
                assert_never(unreachable)

    def checkpoint(self, losses: tuple[tuple[Stream, Loss], ...]) -> Result[None, RecordError]:
        if self.writer is None:
            return Err(RecordError("append", "unavailable"))
        total = sum(loss.count for _, loss in losses)
        if total != self.warned:
            warning = Event(
                t=self.clock.monotonic() - self.writer.manifest.clocks.monotonic_start,
                kind=EventKind.WARNING,
                detail="recording_loss:"
                + ";".join(
                    f"{stream.value}:{loss.count}:{loss.first_sequence}..{loss.last_sequence}:"
                    f"{loss.first_at}..{loss.last_at}"
                    for stream, loss in losses
                    if loss.count
                ),
            )
            match self.writer.event(warning):
                case Err(error):
                    return Err(error)
                case Ok():
                    self.warned = total
                case _ as unreachable:
                    assert_never(unreachable)
        return self.writer.checkpoint()

    def close(self, ending: Ending) -> Result[None, RecordError]:
        if self.writer is None:
            return Ok(None)
        return self.writer.close(ending, ending.reason, ending.observation)

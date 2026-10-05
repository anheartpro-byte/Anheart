"""Immutable producer messages and observer status snapshots."""

from dataclasses import dataclass
from enum import StrEnum
from typing import NewType, assert_never

from src.record.ecg import RawBlock
from src.record.rows import Row
from src.record.schema import DriveFrame, EndObservation, Event, RecordError
from src.record.storage import ByteCount, Capacity, Generation
from src.sensors.base import SensorReading
from src.units import Monotonic, UnixMillis

SequenceNumber = NewType("SequenceNumber", int)


class Stream(StrEnum):
    TICK = "tick"
    EVENT = "event"
    FRAME = "frame"
    RAW = "raw"
    SENSOR = "sensor"


@dataclass(frozen=True, slots=True)
class Sensors:
    at: float
    readings: tuple[SensorReading, ...]


type Payload = Row | Event | DriveFrame | RawBlock | Sensors


@dataclass(frozen=True, slots=True)
class Message:
    sequence: SequenceNumber
    at: Monotonic
    payload: Payload

    @property
    def stream(self) -> Stream:
        match self.payload:
            case Row():
                return Stream.TICK
            case Event():
                return Stream.EVENT
            case DriveFrame():
                return Stream.FRAME
            case RawBlock():
                return Stream.RAW
            case Sensors():
                return Stream.SENSOR
            case _ as unreachable:
                assert_never(unreachable)


@dataclass(frozen=True, slots=True)
class Ending:
    at: Monotonic
    utc: UnixMillis
    reason: str
    observation: EndObservation | None = None

    def monotonic(self) -> Monotonic:
        return self.at

    def unix_millis(self) -> UnixMillis:
        return self.utc


@dataclass(frozen=True, slots=True)
class Prepared:
    capacity: Capacity
    path: str | None
    degraded: bool
    error: RecordError | None = None


@dataclass(frozen=True, slots=True)
class Loss:
    count: int = 0
    first_sequence: SequenceNumber | None = None
    last_sequence: SequenceNumber | None = None
    first_at: Monotonic | None = None
    last_at: Monotonic | None = None


@dataclass(frozen=True, slots=True)
class Status:
    generation: Generation
    degraded: bool
    queued_items: int
    resident_bytes: ByteCount
    checkpoint_at: Monotonic
    losses: tuple[Loss, ...]
    closed: bool
    error: RecordError | None = None

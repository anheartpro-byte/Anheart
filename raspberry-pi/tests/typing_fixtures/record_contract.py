from typing import assert_never

from src.record.reader import Recording
from src.record.schema import Clocks, Event, RecordError
from src.result import Ok, Result
from src.units import UnixMillis


def incomplete_record_result(result: Result[Recording, RecordError]) -> Recording:
    match result:
        case Ok(recording):
            return recording
        case _ as unreachable:
            assert_never(unreachable)


invalid_event = Event(t=0.0, kind="invented", detail="synthetic")
invalid_clocks = Clocks(
    monotonic_start=UnixMillis(10), utc_start="2026-10-05T00:00:00Z", ntp_offset_s=None
)

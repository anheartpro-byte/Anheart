"""ANH-162 EX-2: how a console knows it keeps restarting, when each restart is a new process.

One small file next to the session records carries it from a process to the
next. What is pinned here:

* an unclean start is one whose previous process never wrote a clean stop;
  three of them within ten minutes, during one start of the system, take the
  console out of service, and the third is the one that does;
* out of service is kept across restarts and reboots until it is lifted;
* the mechanism can never hold a machine for good: lifting it works whatever
  the disk does, a damaged or absent file is never a reason to be out of
  service, and neither is a disk that takes nothing;
* a session record left open is reported by one start, not by every start
  after it.

Each "process" below is a new :class:`ServiceGuard` on the same directory,
which is all a restarted console is to this module.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.clock import ManualClock
from src.result import Err, Ok
from src.service_state import (
    IN_SERVICE,
    STATE_NAME,
    TEMPORARY_NAME,
    THRESHOLD,
    WINDOW,
    Absent,
    NotOutOfService,
    Previous,
    ServiceGuard,
    ServiceStatus,
    Stored,
    Unreadable,
    load,
    next_start,
    store,
)
from src.units import Monotonic, Seconds, UnixMillis

BOOT: Final[str] = "2f1c7a0e-5b0d-4c58-9d39-000000000001"
OTHER_BOOT: Final[str] = "2f1c7a0e-5b0d-4c58-9d39-000000000002"
RECORD: Final[str] = "2026-10-10T101112Z_0123456789abcdef"


def clock_at(seconds: float) -> ManualClock:
    return ManualClock(Monotonic(seconds), UnixMillis(1_700_000_000_000))


class Machine:
    """A records directory and the console's clock: what outlives a process."""

    def __init__(self, root: Path, boot_id: str | None = BOOT) -> None:
        self.root: Path = root
        self.boot_id: str | None = boot_id
        self.clock: ManualClock = clock_at(100.0)
        root.mkdir(parents=True, exist_ok=True)

    def start(self, *, after: float = 5.0, open_record: str | None = None) -> ServiceGuard:
        """A new process, ``after`` seconds later."""
        self.clock.advance(Seconds(after))
        guard = ServiceGuard(self.root, self.clock)
        guard.begin(boot_id=self.boot_id, open_record=open_record)
        return guard

    def crash(self, *, after: float = 5.0, open_record: str | None = None) -> ServiceGuard:
        """A start whose process then dies without a word: nothing else is written."""
        return self.start(after=after, open_record=open_record)

    def reboot(self, boot_id: str | None = OTHER_BOOT) -> None:
        """The system restarts: another name, and a monotonic clock that starts again."""
        self.boot_id = boot_id
        self.clock = clock_at(20.0)


# =========================================================================
# One start after another
# =========================================================================


def test_ex2_a_first_start_is_in_service_and_follows_no_unclean_stop(tmp_path: Path) -> None:
    machine = Machine(tmp_path / "records")
    guard = machine.start()
    assert guard.status == ServiceStatus()
    assert guard.status == IN_SERVICE
    assert guard.path == tmp_path / "records" / STATE_NAME
    written = load(guard.path)
    assert isinstance(written, Stored)
    assert written.running, "a process that dies from here on is found running by the next"
    assert not (tmp_path / "records" / TEMPORARY_NAME).exists()


def test_ex2_a_start_after_a_clean_stop_is_not_counted(tmp_path: Path) -> None:
    machine = Machine(tmp_path / "records")
    for _ in range(10):
        guard = machine.start()
        assert guard.status == IN_SERVICE
        guard.stopped()
    final = load(tmp_path / "records" / STATE_NAME)
    assert isinstance(final, Stored)
    assert not final.running
    assert final.unclean_starts == ()


def test_ex2_the_third_unclean_start_within_the_window_takes_the_console_out_of_service(
    tmp_path: Path,
) -> None:
    machine = Machine(tmp_path / "records")
    machine.crash()
    first = machine.crash()
    assert (first.status.unclean_starts, first.status.out_of_service) == (1, False)
    assert first.status.unclean_stop
    second = machine.crash()
    assert (second.status.unclean_starts, second.status.out_of_service) == (2, False)
    third = machine.start()
    assert third.status.unclean_starts == THRESHOLD == 3
    assert third.status.out_of_service
    assert third.status.persisted


def test_ex2_unclean_starts_spread_over_more_than_the_window_never_add_up(
    tmp_path: Path,
) -> None:
    machine = Machine(tmp_path / "records")
    machine.crash()
    for _ in range(20):
        guard = machine.crash(after=float(WINDOW) / 2 + 1.0)
        # At most the one before it is still inside the ten minutes.
        assert guard.status.unclean_starts <= 2
        assert not guard.status.out_of_service


def test_ex2_a_clean_stop_between_two_deaths_does_not_forget_the_first(tmp_path: Path) -> None:
    """Counted by the clock, not by "in a row": a console restarted by hand between
    two deaths has still died twice in ten minutes."""
    machine = Machine(tmp_path / "records")
    machine.crash()
    machine.crash()  # unclean start 1
    survivor = machine.crash()  # unclean start 2
    survivor.stopped()
    clean = machine.crash()
    assert clean.status.unclean_starts == 2
    assert not clean.status.unclean_stop
    last = machine.start()
    assert last.status.unclean_starts == 3
    assert last.status.out_of_service


def test_ex2_a_machine_switched_off_at_the_wall_every_evening_is_not_a_restart_loop(
    tmp_path: Path,
) -> None:
    """Each unclean stop is of another start of the system: its instants compare to nothing."""
    machine = Machine(tmp_path / "records")
    machine.crash()
    for day in range(10):
        machine.reboot(f"2f1c7a0e-5b0d-4c58-9d39-0000000001{day:02d}")
        guard = machine.crash()
        assert guard.status.unclean_stop, "the power went: that stop was not a clean one"
        assert guard.status.unclean_starts == 1
        assert not guard.status.out_of_service


def test_ex2_a_system_that_cannot_name_its_start_still_drops_instants_from_the_future(
    tmp_path: Path,
) -> None:
    """No boot identifier (not Linux): the clock starting again is what tells the starts apart."""
    machine = Machine(tmp_path / "records", boot_id=None)
    machine.clock.advance(Seconds(5_000.0))
    machine.crash()
    machine.crash()
    before = machine.crash()
    assert before.status.unclean_starts == 2
    machine.reboot(None)
    after = machine.start()
    assert after.status.unclean_starts == 1
    assert not after.status.out_of_service


# =========================================================================
# Out of service: kept, and lifted
# =========================================================================


def out_of_service(machine: Machine) -> ServiceGuard:
    for _ in range(THRESHOLD):
        machine.crash()
    guard = machine.start()
    assert guard.status.out_of_service
    return guard


def test_ex2_out_of_service_survives_clean_restarts_reboots_and_any_time(tmp_path: Path) -> None:
    machine = Machine(tmp_path / "records")
    out_of_service(machine).stopped()
    later = machine.start(after=10 * float(WINDOW))
    assert later.status.out_of_service, "time alone never lifts it"
    assert later.status.unclean_starts == 0
    later.stopped()
    machine.reboot()
    rebooted = machine.start()
    assert rebooted.status.out_of_service, "a reboot never lifts it"


def test_ex2_restoring_puts_the_console_back_in_service_at_once_and_for_the_next_start(
    tmp_path: Path,
) -> None:
    machine = Machine(tmp_path / "records")
    guard = out_of_service(machine)
    restored = guard.restore()
    assert isinstance(restored, Ok)
    assert not guard.status.out_of_service
    assert guard.status.unclean_starts == 0, "whoever lifts it has answered for those restarts"
    guard.saved(store(guard.path, restored.value))
    assert guard.status.persisted
    guard.stopped()
    again = machine.start()
    assert again.status == IN_SERVICE


def test_ex2_there_is_nothing_to_lift_on_a_console_in_service(tmp_path: Path) -> None:
    guard = Machine(tmp_path / "records").start()
    refused = guard.restore()
    assert refused == Err(NotOutOfService())
    assert guard.status == IN_SERVICE


def test_ex2_a_disk_that_takes_nothing_never_holds_the_machine(tmp_path: Path) -> None:
    """The records directory cannot be written: the state is not kept, and nothing is refused."""
    blocked = tmp_path / "records"
    blocked.write_text("a file where the directory should be", encoding="utf-8")
    clock = clock_at(100.0)
    for _ in range(2 * THRESHOLD):
        clock.advance(Seconds(5.0))
        guard = ServiceGuard(blocked, clock)
        status = guard.begin(boot_id=BOOT, open_record=None)
        assert not status.out_of_service
        assert not status.persisted, "the page says the count is not kept"
        guard.stopped()
        assert not guard.status.persisted


def test_ex2_lifting_holds_in_memory_when_the_disk_refuses_the_new_state(tmp_path: Path) -> None:
    machine = Machine(tmp_path / "records")
    guard = out_of_service(machine)
    restored = guard.restore()
    assert isinstance(restored, Ok)
    guard.saved(Err("OSError:EROFS"))
    assert not guard.status.out_of_service, "in service now, whatever the disk said"
    assert not guard.status.persisted
    # The file still says out of service: the next start is out of service again,
    # and is lifted the same way. Never a machine held for good.
    again = machine.start()
    assert again.status.out_of_service
    assert isinstance(again.restore(), Ok)
    assert not again.status.out_of_service


# =========================================================================
# What a start finds on the disk
# =========================================================================


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{",
        b"[]",
        b'{"schema_version": 2}',
        b'{"schema_version": 1, "unclean_starts": [-1.0]}',
        b'{"schema_version": 1, "surprise": true}',
        b"\xff\xfe\x00",
    ],
)
def test_ex2_a_state_that_cannot_be_read_counts_as_one_unclean_stop_and_never_more(
    tmp_path: Path, content: bytes
) -> None:
    """A damaged file is not a restart loop, and not a way to stay out of service either."""
    machine = Machine(tmp_path / "records")
    (machine.root / STATE_NAME).write_bytes(content)
    assert load(machine.root / STATE_NAME) == Unreadable("invalid")
    guard = machine.start()
    assert guard.status.unclean_stop
    assert guard.status.unclean_starts == 1
    assert not guard.status.out_of_service
    rewritten = load(guard.path)
    assert isinstance(rewritten, Stored), "the start replaced it by a state that reads"


def test_ex2_a_state_that_cannot_be_opened_is_unreadable_not_absent(tmp_path: Path) -> None:
    root = tmp_path / "records"
    (root / STATE_NAME).mkdir(parents=True)
    found = load(root / STATE_NAME)
    assert isinstance(found, Unreadable)
    assert found.detail.startswith(("IsADirectoryError", "PermissionError"))
    assert load(root / "absent.json") == Absent()


def test_ex2_the_state_is_replaced_whole_and_private(tmp_path: Path) -> None:
    root = tmp_path / "records"
    root.mkdir()
    path = root / STATE_NAME
    leftover = root / TEMPORARY_NAME
    leftover.write_text("half a state, left by a process killed while writing", encoding="utf-8")
    first = Stored(boot_id=BOOT, unclean_starts=(1.5,), running=True)
    assert store(path, first) == Ok(None)
    assert load(path) == first
    assert not leftover.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    second = Stored(boot_id=BOOT, running=False, out_of_service=True)
    assert store(path, second) == Ok(None)
    assert load(path) == second
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_ex2_a_previous_state_that_is_none_of_the_three_fails_loudly() -> None:
    with pytest.raises(AssertionError):
        next_start(cast("Previous", "not-a-state"), boot_id=BOOT, now=1.0, open_record=None)


# =========================================================================
# EX-3: the session record a stop left open
# =========================================================================


def test_ex3_a_record_left_open_is_reported_by_one_start_and_not_by_the_next(
    tmp_path: Path,
) -> None:
    machine = Machine(tmp_path / "records")
    machine.crash()
    after_crash = machine.start(open_record=RECORD)
    assert after_crash.status.unclean_stop
    assert after_crash.status.open_record == RECORD
    after_crash.stopped()
    # Nothing closes that record afterwards: it is still the newest, still open.
    after_clean_stop = machine.start(open_record=RECORD)
    assert not after_clean_stop.status.unclean_stop
    assert after_clean_stop.status.open_record is None
    assert after_clean_stop.status.unclean_starts == 1, "counted once, by the start that found it"


def test_ex3_an_open_record_is_reported_even_when_the_console_itself_stopped_cleanly(
    tmp_path: Path,
) -> None:
    """The disk did not finalise the record in time, or the record is older than this file."""
    machine = Machine(tmp_path / "records")
    guard = machine.start(open_record=RECORD)
    assert guard.status.unclean_stop
    assert guard.status.open_record == RECORD
    assert guard.status.unclean_starts == 0, "an open record is said, not counted as a death"
    assert not guard.status.out_of_service


def test_ex3_another_record_left_open_later_is_reported_in_its_turn(tmp_path: Path) -> None:
    machine = Machine(tmp_path / "records")
    machine.start(open_record=RECORD).stopped()
    newer = "2026-10-11T080000Z_fedcba9876543210"
    guard = machine.start(open_record=newer)
    assert guard.status.open_record == newer
    guard.stopped()
    closed = machine.start(open_record=None)
    assert closed.status == IN_SERVICE


# =========================================================================
# Whatever the history
# =========================================================================

instants = st.floats(min_value=0.0, max_value=1.0e7, allow_nan=False, allow_infinity=False)
boots = st.sampled_from([None, BOOT, OTHER_BOOT])
stored_states = st.builds(
    Stored,
    boot_id=boots,
    unclean_starts=st.lists(instants, max_size=8).map(tuple),
    running=st.booleans(),
    out_of_service=st.booleans(),
    reported_record=st.sampled_from([None, RECORD]),
)
previous_states = st.one_of(
    st.just(Absent()), st.builds(Unreadable, st.just("invalid")), stored_states
)


@given(previous=previous_states, boot=boots, now=instants, record=st.sampled_from([None, RECORD]))
def test_ex2_property_out_of_service_is_only_ever_inherited_or_three_recent_deaths(
    previous: Previous, boot: str | None, now: float, record: str | None
) -> None:
    stored, status = next_start(previous, boot_id=boot, now=now, open_record=record)
    held = isinstance(previous, Stored) and previous.out_of_service
    assert status.out_of_service == (held or status.unclean_starts >= THRESHOLD)
    assert stored.out_of_service == status.out_of_service
    assert stored.running, "every start is written as running"
    assert stored.boot_id == boot
    assert status.unclean_starts == len(stored.unclean_starts)
    # Every instant kept is of this start of the system and inside the window.
    assert all(0 <= now - instant < WINDOW for instant in stored.unclean_starts)
    if not isinstance(previous, Stored):
        assert not status.out_of_service, "no file, or a damaged one, is never out of service"
    if isinstance(previous, Stored) and previous.boot_id != boot:
        assert status.unclean_starts <= 1, "nothing is carried from another start of the system"


@given(previous=previous_states, boot=boots, now=instants)
def test_ex2_property_a_console_out_of_service_can_always_be_put_back(
    previous: Previous, boot: str | None, now: float
) -> None:
    """Whatever was found and whatever the disk does next: restoring ends in service."""
    guard = ServiceGuard(Path("/nonexistent/anheart/records"), clock_at(now))
    status = guard.begin(boot_id=boot, open_record=None)
    assert not status.out_of_service, "nothing could be read there: never a reason"
    stored, status = next_start(previous, boot_id=boot, now=now, open_record=None)
    object.__setattr__(guard, "_stored", stored)
    object.__setattr__(guard, "_status", status)
    restored = guard.restore()
    if status.out_of_service:
        assert isinstance(restored, Ok)
        assert not restored.value.out_of_service
        assert restored.value.unclean_starts == ()
    else:
        assert restored == Err(NotOutOfService())
    assert not guard.status.out_of_service

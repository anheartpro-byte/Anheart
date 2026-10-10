"""Whether the wall clock can be believed (``src/wall_clock.py``, ANH-163 EX-1).

The rule, the reading of the kernel's answer, the last reading kept in memory
and the words the operator is shown. What the console does with it (the
record's manifest, the start with a person on board, the page) is in
``test_wall_clock_console``.
"""

from __future__ import annotations

import ctypes
import struct
import sys
from typing import cast

import pytest

from src.units import Seconds
from src.wall_clock import (
    TIMEX_BYTES,
    UNSYNCHRONISED_FROM,
    KernelClock,
    Synchronised,
    Unreadable,
    Unsynchronised,
    WallClockProbe,
    WallClockState,
    WallClockWatch,
    clock_offset_s,
    detail_of,
    is_trusted,
    judge,
    label_of,
    state_name,
    system_probe,
)
from tests.wall_clock_support import IN_SYNC, NO_NETWORK, ScriptedClock

UNREADABLE = Unreadable("adjtimex refuse par le systeme")
NOBODY_ANSWERS = (
    "aucune source de temps jointe : l'heure peut etre fausse de la duree pendant "
    "laquelle le Pi etait eteint"
)
"""What the operator reads of a clock no time service keeps."""
EVERY_STATE: tuple[WallClockState, ...] = (IN_SYNC, NO_NETWORK, UNREADABLE)

# =========================================================================
# The rule: the kernel's two figures
# =========================================================================


def test_ex1_a_clock_a_time_service_keeps_is_trusted_with_its_two_errors() -> None:
    """Estimated 412 microseconds off, 45 ms at most: what chrony publishes once synchronised."""
    state = judge(max_error_us=45_000, estimated_error_us=412)
    assert state == Synchronised(estimated_error=Seconds(0.000412), max_error=Seconds(0.045))
    assert is_trusted(state)
    offset = clock_offset_s(state)
    assert offset == Seconds(0.000412)


@pytest.mark.parametrize(
    "max_error_us",
    [UNSYNCHRONISED_FROM, UNSYNCHRONISED_FROM + 1, 2**40, -1],
    ids=["the kernel's ceiling", "above it", "far above", "a negative figure"],
)
def test_ex1_a_clock_nobody_keeps_is_not_trusted(max_error_us: int) -> None:
    """16 s is what the kernel holds at boot and after a time service lost its sources."""
    state = judge(max_error_us=max_error_us, estimated_error_us=0)
    assert state == Unsynchronised()
    assert not is_trusted(state)
    offset = clock_offset_s(state)
    assert offset is None


def test_ex1_the_rule_turns_exactly_at_sixteen_seconds() -> None:
    below = judge(max_error_us=UNSYNCHRONISED_FROM - 1, estimated_error_us=1)
    at = judge(max_error_us=UNSYNCHRONISED_FROM, estimated_error_us=1)
    assert isinstance(below, Synchronised)
    assert isinstance(at, Unsynchronised)
    assert Seconds(16.0) == UNSYNCHRONISED_FROM / 1_000_000


@pytest.mark.parametrize("estimated_error_us", [0, -5, UNSYNCHRONISED_FROM])
def test_ex1_an_estimate_nobody_published_is_unknown_never_zero(estimated_error_us: int) -> None:
    """A time service that writes 0 there has measured nothing: no figure, not a perfect clock."""
    state = judge(max_error_us=30_000, estimated_error_us=estimated_error_us)
    assert state == Synchronised(estimated_error=None, max_error=Seconds(0.03))
    offset = clock_offset_s(state)
    assert offset is None


def test_ex1_an_unreadable_clock_is_not_a_trusted_one() -> None:
    trusted = is_trusted(UNREADABLE)
    offset = clock_offset_s(UNREADABLE)
    assert not trusted
    assert offset is None


# =========================================================================
# Asking the kernel
# =========================================================================


def _kernel_answer(max_error_us: int, estimated_error_us: int, code: int = 0) -> KernelClock:
    """A kernel that fills ``struct timex`` the way the C compiler lays it out here."""
    answer = struct.pack("@Illlli", 0, 0, 0, max_error_us, estimated_error_us, 0)

    def adjtimex(buffer: ctypes.Array[ctypes.c_char]) -> int:
        ctypes.memmove(buffer, answer, len(answer))
        return code

    return KernelClock(adjtimex)


def test_ex1_the_two_figures_are_read_where_the_kernel_writes_them() -> None:
    """The offsets in the module are those of the C structure on a 64-bit system."""
    state = _kernel_answer(max_error_us=52_000, estimated_error_us=950).read()
    assert state == Synchronised(estimated_error=Seconds(0.00095), max_error=Seconds(0.052))


def test_ex1_a_kernel_that_says_nobody_keeps_the_clock_is_read_as_such() -> None:
    """Code 5 (``TIME_ERROR``) is an answer, not a failure: the figures are judged as ever."""
    state = _kernel_answer(max_error_us=16_000_000, estimated_error_us=16_000_000, code=5).read()
    assert state == Unsynchronised()


def test_ex1_the_kernel_is_asked_to_change_nothing() -> None:
    """The structure is handed over zeroed: a zero ``modes`` sets nothing."""
    seen: list[bytes] = []

    def adjtimex(buffer: ctypes.Array[ctypes.c_char]) -> int:
        seen.append(buffer.raw)
        return 0

    KernelClock(adjtimex).read()
    assert seen == [bytes(TIMEX_BYTES)]
    assert struct.calcsize("@Illlli") <= TIMEX_BYTES


@pytest.mark.parametrize("fault", [AttributeError("adjtimex"), OSError(1, "refused")])
def test_ex1_a_system_with_no_such_call_gives_an_unreadable_clock(fault: Exception) -> None:
    def adjtimex(_buffer: ctypes.Array[ctypes.c_char]) -> int:
        raise fault

    state = KernelClock(adjtimex).read()
    assert state == Unreadable(f"adjtimex indisponible sur ce systeme : {type(fault).__name__}")


def test_ex1_a_call_the_system_refuses_gives_an_unreadable_clock() -> None:
    state = _kernel_answer(max_error_us=10, estimated_error_us=10, code=-1).read()
    assert state == Unreadable("adjtimex refuse par le systeme")


def test_ex1_the_real_kernel_answers_on_linux_and_is_unreadable_elsewhere() -> None:
    """The production probe, for real: on Linux (CI, the Pi) the kernel answers one of the
    two standings; where there is no ``adjtimex`` (a macOS workstation) nothing is trusted."""
    state = KernelClock().read()
    if sys.platform == "linux":
        assert isinstance(state, Synchronised | Unsynchronised), state
    else:
        assert isinstance(state, Unreadable), state
        assert not is_trusted(state)


def test_ex1_the_console_judges_its_clock_on_linux_and_nowhere_else() -> None:
    """On the Pi the kernel is asked. A system with no such question (the macOS workstation
    of the bench) gets no probe at all: the console then judges nothing, and refuses nothing."""
    on_the_pi = system_probe("linux")
    on_a_mac = system_probe("darwin")
    here = system_probe()
    assert isinstance(on_the_pi, KernelClock)
    assert on_a_mac is None
    assert (here is not None) == (sys.platform == "linux")


# =========================================================================
# The last reading
# =========================================================================


def _watch(probe: WallClockProbe) -> WallClockWatch:
    """Typed identity too: the fake satisfies the protocol."""
    return WallClockWatch(probe)


def test_the_watch_reads_once_when_built_then_only_when_asked() -> None:
    probe = ScriptedClock(NO_NETWORK)
    watch = _watch(probe)
    assert probe.reads == 1
    first = watch.current()
    again = watch.current()
    assert first == again == NO_NETWORK
    assert probe.reads == 1, "current() reads memory: the gates and the page never reach the probe"

    probe.state = IN_SYNC
    stale = watch.current()
    assert stale == NO_NETWORK, "nothing changes until the console's own task refreshes"
    refreshed = watch.refresh()
    assert refreshed == IN_SYNC
    assert watch.current() == IN_SYNC
    assert probe.reads == 2


def test_a_probe_that_raises_is_an_unreadable_clock_never_an_exception() -> None:
    probe = ScriptedClock(IN_SYNC, fault=RuntimeError("injected: the probe broke"))
    watch = _watch(probe)
    built = watch.current()
    assert built == Unreadable("lecture en echec : RuntimeError")

    probe.fault = None
    back = watch.refresh()
    assert back == IN_SYNC
    probe.fault = ValueError("injected again")
    broken = watch.refresh()
    assert broken == Unreadable("lecture en echec : ValueError")
    assert not is_trusted(broken)


def test_the_log_says_the_clocks_standing_at_start_and_when_it_changes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One line at startup, one per change of standing, none while it stays the same.
    A warning, unless the clock is one to believe."""
    probe = ScriptedClock(NO_NETWORK)
    with caplog.at_level("INFO", logger="src.wall_clock"):
        watch = _watch(probe)
        watch.refresh()
        probe.state = IN_SYNC
        watch.refresh()
        probe.state = Synchronised(estimated_error=Seconds(0.002), max_error=Seconds(0.3))
        watch.refresh()
        probe.state = NO_NETWORK
        watch.refresh()
    said = [(record.levelname, record.getMessage()) for record in caplog.records]
    cut = ("WARNING", f"wall clock: unsynchronised ({NOBODY_ANSWERS})")
    assert said == [
        cut,
        ("INFO", "wall clock: synchronised (erreur estimee 0.4 ms, erreur au plus 45.0 ms)"),
        cut,
    ]


# =========================================================================
# In words
# =========================================================================


def test_each_standing_has_a_name_a_word_and_a_reason_of_its_own() -> None:
    names = [state_name(state) for state in EVERY_STATE]
    labels = [label_of(state) for state in EVERY_STATE]
    details = [detail_of(state) for state in EVERY_STATE]
    assert names == ["synchronised", "unsynchronised", "unreadable"]
    assert labels == ["synchronisee", "NON FIABLE", "NON VERIFIEE"]
    assert details == [
        "erreur estimee 0.4 ms, erreur au plus 45.0 ms",
        NOBODY_ANSWERS,
        "etat de l'horloge illisible (adjtimex refuse par le systeme)",
    ]


def test_a_synchronised_clock_with_no_estimate_says_only_its_bound() -> None:
    said = detail_of(Synchronised(estimated_error=None, max_error=Seconds(0.25)))
    assert said == "erreur au plus 250.0 ms"


def test_a_standing_that_is_not_one_fails_loudly() -> None:
    """A fourth standing added later, with no words written for it, is an error and not a blank."""
    unknown = cast("WallClockState", "not-a-standing")
    for words in (state_name, label_of, detail_of):
        with pytest.raises(AssertionError):
            words(unknown)

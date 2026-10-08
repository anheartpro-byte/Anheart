"""The level rules judge the last usable heart rate, through the real runtime.

``hr_hard_max`` and ``hr_critical`` are judged by the supervisor
(``tests/test_safety.py`` holds the rule itself and its property). These tests
are about what the RUNTIME owes that rule and what it does with its verdict:

* **every reading counts, not only the one a tick happens to see.** The
  supervisor is shown one sample a tick; readings arrive on another task, and a
  backlog arrives several at a time. The runtime keeps the last fresh sample
  that carried a usable rate beside the last sample, so a usable reading
  overtaken by one with no rate before the tick is still judged;
* **the dwell runs through a signal that is lost**, and the session ends when
  it elapses, on the real control tick;
* **what follows the verdict is what follows any verdict**: the setpoint never
  rises under it, the machine ends at rest, nothing restarts by itself, and
  ``hr_stale`` goes on counting from the last usable reading as if the level
  rule were not there.

The fake drive and the manual clock of ``tests/test_runtime.py``; the rig's
profile puts the hard maximum at 148 bpm and the critical level at 158.
"""

from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.result import is_err, is_ok
from src.training.hr_control import StaleSequence
from src.training.runtime import EndReason, RuntimeState
from src.training.safety import (
    RULE_HR_CRITICAL,
    RULE_HR_HARD_MAX,
    RULE_HR_STALE,
    RULE_SESSION_OVERRUN,
    SafetyLimits,
    SafetyObservation,
    SafetySupervisor,
)
from src.training.types import SafetyAction, SafetyVerdict, SignalQuality
from src.units import Bpm
from tests.test_runtime import (
    OPERATOR,
    TICK,
    Rig,
    _rig,  # pyright: ignore[reportPrivateUsage]  # the shared rig builders
    _running_rig,  # pyright: ignore[reportPrivateUsage]
)

ABOVE: Final[Bpm] = Bpm(150)
"""Above the rig's hard maximum (148), under its critical level (158)."""

AT_CRITICAL: Final[Bpm] = Bpm(158)

LIMITS: Final[SafetyLimits] = SafetyLimits(hard_max_bpm=Bpm(148), critical_bpm=Bpm(158))

TICKS_PER_READING: Final[int] = 5
"""The pipeline refreshes at 1 Hz and the loop runs at 5 Hz."""

EPSILON: Final[float] = 1e-6
"""Tick instants are sums of 0.2 s: a boundary may land one rounding either side."""


@contextmanager
def _observations() -> Generator[list[SafetyObservation]]:
    """Record every observation the runtime hands its supervisor, and judge it as usual."""
    seen: list[SafetyObservation] = []
    real = SafetySupervisor.evaluate

    def _record(self: SafetySupervisor, observation: SafetyObservation) -> SafetyVerdict | None:
        seen.append(observation)
        return real(self, observation)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SafetySupervisor, "evaluate", _record)
        yield seen


def _no_rate(rig: Rig, quality: SignalQuality = SignalQuality.NOISY) -> None:
    """One fresh reading that carries no rate: by default, what a noisy window hands on."""
    rig.feed(None, quality=quality)


def _live_rules(rig: Rig) -> frozenset[str]:
    return frozenset(verdict.rule for verdict in rig.runtime.supervisor.live)


def _standing_rule(rig: Rig) -> str | None:
    verdict = rig.runtime.standing
    return None if verdict is None else verdict.rule


async def _one_second(rig: Rig) -> None:
    for _ in range(TICKS_PER_READING):
        await rig.step(feed=False)


async def _first_tick_with(
    rig: Rig, rule: str, *, within: float, quality: SignalQuality = SignalQuality.NOISY
) -> float | None:
    """Feed a reading with no rate every second; the instant ``rule`` first fires, if it does."""
    for tick in range(round(within / TICK)):
        if tick % TICKS_PER_READING == 0:
            _no_rate(rig, quality)
        await rig.step(feed=False)
        if rule in _live_rules(rig):
            return float(rig.now)
    return None


# =========================================================================
# What the runtime hands the supervisor
# =========================================================================


async def test_the_runtime_keeps_the_last_fresh_sample_that_carried_a_rate() -> None:
    """Replaced by a fresh sample with a usable rate, and by nothing else.

    A fresh sample with no rate replaces the LAST sample (the screen and
    ``hr_stale`` must know nothing is being measured) and leaves the last
    usable one where it is. A repeated sequence number replaces neither: it is
    the pipeline saying it measured nothing.
    """
    rig = _rig()
    started = await rig.start()
    with _observations() as seen:
        await rig.step(feed=False)
        before_any_reading = seen[-1]

        rig.feed(Bpm(100))
        await rig.step(feed=False)
        usable = seen[-1]

        _no_rate(rig)
        await rig.step(feed=False)
        overtaken = seen[-1]

        repeated = rig.runtime.observe_ecg(rig.now, rig.seq, SignalQuality.GOOD, Bpm(120))
        await rig.step(feed=False)
        after_the_repeat = seen[-1]

        rig.feed(Bpm(110))
        await rig.step(feed=False)
        refreshed = seen[-1]

    assert is_ok(started)
    assert before_any_reading.heart_rate is None
    assert before_any_reading.last_usable_heart_rate is None
    assert usable.last_usable_heart_rate is not None
    assert usable.last_usable_heart_rate is usable.heart_rate
    assert usable.last_usable_heart_rate.bpm == Bpm(100)
    assert overtaken.heart_rate is not None
    assert overtaken.heart_rate.quality is SignalQuality.NOISY
    assert overtaken.last_usable_heart_rate is usable.heart_rate
    assert is_err(repeated)
    assert isinstance(repeated.error, StaleSequence)
    assert after_the_repeat.last_usable_heart_rate is usable.heart_rate
    assert refreshed.last_usable_heart_rate is refreshed.heart_rate
    assert refreshed.last_usable_heart_rate is not None
    assert refreshed.last_usable_heart_rate.bpm == Bpm(110)


async def test_a_critical_reading_overtaken_before_the_tick_still_stops_the_machine() -> None:
    """Two readings between two ticks: one at the critical level, then one with no rate.

    The bridge drains a backlog in one pump and runs on its own task, so this
    is an ordinary interleaving on the console. The supervisor is shown the
    later sample only; the earlier one reaches the rule as the last usable
    sample, and the machine is stopped on the first tick after it.
    """
    rig = await _running_rig()
    turning = rig.runtime.applied_rpm

    rig.feed(AT_CRITICAL)
    _no_rate(rig)
    await rig.step(feed=False)
    standing = rig.runtime.standing

    assert turning > 0
    assert standing is not None
    assert standing.rule == RULE_HR_CRITICAL
    assert standing.action is SafetyAction.QUICK_STOP
    assert standing.latched is True
    assert rig.runtime.applied_rpm == 0
    assert rig.drive.commanded_rpm == 0


# =========================================================================
# The dwell runs through a lost signal
# =========================================================================


@pytest.mark.parametrize(
    "grade",
    [
        pytest.param(SignalQuality.NO_SIGNAL, id="no_signal"),
        pytest.param(SignalQuality.NOISY, id="noisy"),
        pytest.param(SignalQuality.MAINS_DOMINATED, id="mains_dominated"),
    ],
)
async def test_a_signal_lost_during_the_hard_maximum_dwell_ends_the_session_when_it_elapses(
    grade: SignalQuality,
) -> None:
    """Three readings above the hard maximum, then no reading carries a rate.

    Whatever grade those readings carry, and so whatever stopped the rate
    being measured: ``no_signal`` (a flat lead, an electrode off, a window the
    grader could not grade at all), ``noisy`` (clipping, or a rate the
    independent confirmation withheld), ``mains_dominated`` (hum).

    The session ends five seconds after the first tick that saw a rate above
    the limit - two and a bit after the last usable reading - on the level
    rule, and before ``hr_stale`` has begun: that rule's first answer, ten
    seconds after the loss, would only have held the speed.
    """
    rig = await _running_rig()
    for _ in range(3):
        rig.feed(ABOVE)
        await _one_second(rig)
    first_seen = float(rig.now) - 3.0 + TICK
    setpoint_at_the_loss = rig.runtime.applied_rpm

    fired_at = await _first_tick_with(rig, RULE_HR_HARD_MAX, within=12.0, quality=grade)
    standing = rig.runtime.standing

    assert setpoint_at_the_loss > 0
    assert fired_at is not None
    assert 5.0 - EPSILON <= fired_at - first_seen <= 5.0 + TICK + EPSILON
    assert standing is not None
    assert standing.rule == RULE_HR_HARD_MAX
    assert standing.action is SafetyAction.RAMP_DOWN
    assert standing.latched is True
    assert "the last usable reading" in standing.detail
    assert RULE_HR_STALE not in _live_rules(rig)


async def test_a_noisy_signal_above_the_hard_maximum_ends_the_session() -> None:
    """One reading in three carries a rate, above the limit; the others carry none.

    No five continuous seconds of readings with a rate exist anywhere in this
    signal. The dwell is five continuous seconds of the heart not having been
    measured under the limit, and that is what elapses.
    """
    rig = await _running_rig()
    fired_at: float | None = None
    began = float(rig.now)
    for second in range(9):
        if second % 3 == 0:
            rig.feed(ABOVE)
        else:
            _no_rate(rig)
        await _one_second(rig)
        if fired_at is None and RULE_HR_HARD_MAX in _live_rules(rig):
            fired_at = float(rig.now)

    assert fired_at is not None
    assert fired_at - began <= 6.0 + EPSILON
    assert _standing_rule(rig) == RULE_HR_HARD_MAX
    assert rig.runtime.end_reason is EndReason.SAFETY_VERDICT


# =========================================================================
# What follows the verdict
# =========================================================================


async def test_after_the_level_verdict_nothing_rises_and_nothing_restarts() -> None:
    """The order of verdicts on a signal lost above the hard maximum, and what the arm does.

    * the level verdict comes first, when its dwell elapses, and from that
      tick on the setpoint never rises;
    * ``hr_stale`` begins ten seconds after the LAST USABLE reading, on its
      own clock: neither the readings with no rate nor the level verdict
      restart it;
    * the session ends at rest, and a good heart rate coming back afterwards
      moves nothing: the verdict is latched, and only a named acknowledgement
      and a new start put the machine back in service.
    """
    rig = await _running_rig()
    for _ in range(3):
        rig.feed(ABOVE)
        await _one_second(rig)
    last_usable_at = float(rig.now) - 1.0
    fired_at = await _first_tick_with(rig, RULE_HR_HARD_MAX, within=12.0)
    assert fired_at is not None

    setpoints = [rig.runtime.applied_rpm]
    stale_at: float | None = None
    for tick in range(round(30.0 / TICK)):
        if tick % TICKS_PER_READING == 0:
            _no_rate(rig)
        await rig.step(feed=False)
        setpoints.append(rig.runtime.applied_rpm)
        if stale_at is None and RULE_HR_STALE in _live_rules(rig):
            stale_at = float(rig.now)
    at_rest = (rig.runtime.applied_rpm, rig.drive.commanded_rpm, round(rig.drive.shaft_rpm))

    rig.fed_bpm = Bpm(70)
    await rig.run(30.0)
    after_the_rate_came_back = (rig.runtime.applied_rpm, round(rig.drive.shaft_rpm))

    assert all(later <= earlier for earlier, later in pairwise(setpoints))
    assert stale_at is not None
    assert 10.0 - EPSILON <= stale_at - last_usable_at <= 10.0 + TICK + EPSILON
    assert at_rest == (0, 0, 0)
    assert after_the_rate_came_back == (0, 0)
    assert _standing_rule(rig) == RULE_HR_HARD_MAX
    assert rig.state() is not RuntimeState.RUNNING
    assert not rig.drive.is_enabled()


async def test_a_level_verdict_inside_an_ending_leaves_that_ending_as_it_was() -> None:
    """A STOP, then the rate above the hard maximum and the signal lost during the descent.

    The two sets of rules together. The operator's stop has opened the ending
    and the setpoint is walking down. The level verdict rises when its dwell
    elapses, there as anywhere else, and latches. It does not replace the
    ending's cause, the setpoint goes on down without ever rising, the session
    finishes at rest, and ``session_overrun`` has nothing to say about an
    ending that went as it should. One acknowledgement, once the session is
    over, clears it for good.
    """
    rig = await _running_rig()
    rig.runtime.request_stop("the operator pressed stop")
    await _one_second(rig)
    setpoints = [rig.runtime.applied_rpm]
    stood: set[str] = set()
    fired_at: float | None = None
    first_seen = float(rig.now) + TICK
    for tick in range(round(100.0 / TICK)):
        if tick % TICKS_PER_READING == 0 and tick < 3 * TICKS_PER_READING:
            rig.feed(ABOVE)
        elif tick % TICKS_PER_READING == 0:
            _no_rate(rig, SignalQuality.NO_SIGNAL)
        await rig.step(feed=False)
        setpoints.append(rig.runtime.applied_rpm)
        stood |= _live_rules(rig)
        if fired_at is None and RULE_HR_HARD_MAX in _live_rules(rig):
            fired_at = float(rig.now)
    at_rest = (rig.runtime.applied_rpm, rig.drive.commanded_rpm, round(rig.drive.shaft_rpm))
    finished = rig.state()
    standing_before = _standing_rule(rig)
    acknowledged = rig.runtime.acknowledge(OPERATOR)
    await rig.step(feed=False)

    assert setpoints[0] > 0
    assert fired_at is not None
    assert 5.0 - EPSILON <= fired_at - first_seen <= 5.0 + TICK + EPSILON
    assert all(later <= earlier for earlier, later in pairwise(setpoints))
    assert at_rest == (0, 0, 0)
    assert finished is RuntimeState.FINISHED
    assert rig.runtime.end_reason is EndReason.OPERATOR_STOP
    assert RULE_SESSION_OVERRUN not in stood
    assert standing_before == RULE_HR_HARD_MAX
    assert is_ok(acknowledged)
    assert rig.runtime.standing is None


type _Fed = int | SignalQuality
"""One reading a second: a usable rate, or the grade of a reading that carries none."""

_NO_RATE: Final[st.SearchStrategy[SignalQuality]] = st.sampled_from(
    [SignalQuality.NO_SIGNAL, SignalQuality.NOISY, SignalQuality.MAINS_DOMINATED]
)
_ANY_READING: Final[st.SearchStrategy[_Fed]] = st.one_of(
    _NO_RATE, st.integers(min_value=120, max_value=165)
)
_NEAR_THE_LIMIT: Final[st.SearchStrategy[_Fed]] = st.one_of(
    _NO_RATE, _NO_RATE, _NO_RATE, st.integers(min_value=140, max_value=160)
)
"""Three readings in four carry no rate, and the rates sit around the hard maximum
(148) and reach the critical level (158): a rate above the limit followed by a
silence is common in a run of these, without being built in."""
_QUIET_READING: Final[st.SearchStrategy[_Fed]] = st.one_of(
    _NO_RATE, st.integers(min_value=130, max_value=147)
)
"""A reading under the hard maximum (148), or one with no rate."""

_EXCESS: Final[st.SearchStrategy[list[int]]] = st.lists(
    st.integers(min_value=149, max_value=157), min_size=1, max_size=4
)
"""Above the hard maximum and under the critical level, for less than the dwell:
one to four readings a second apart never make five seconds of readings."""


@dataclass(slots=True)
class _Watch:
    """What the properties below remember from one reading to the next."""

    above_since: float | None = None
    usable_at: float | None = None
    ended: bool = False
    rules: set[str] = field(default_factory=set[str])
    """Every rule that fired on some tick, whatever stood over it."""


async def _feed_and_judge(rig: Rig, readings: Sequence[_Fed], watch: _Watch) -> None:
    """Feed one reading a second and assert, after every tick, what must always hold.

    * the setpoint never rises on a tick where a verdict stands;
    * once the last usable rate has stood above the hard maximum for the dwell
      and a tick, with no usable rate at or under it in between and without
      going stale, a verdict that ends the session stands;
    * a verdict that ends the session is never withdrawn.
    """
    for reading in readings:
        if isinstance(reading, SignalQuality):
            _no_rate(rig, reading)
        else:
            rig.feed(Bpm(reading))
            watch.usable_at = float(rig.now)
            above = reading > LIMITS.hard_max_bpm
            watch.above_since = (watch.above_since or watch.usable_at) if above else None
        for _ in range(TICKS_PER_READING):
            before = rig.runtime.applied_rpm
            await rig.step(feed=False)
            now = float(rig.now)
            action = rig.standing_action()
            watch.rules |= _live_rules(rig)
            assert action is SafetyAction.NONE or rig.runtime.applied_rpm <= before
            assert not watch.ended or action >= SafetyAction.RAMP_DOWN
            watch.ended = watch.ended or action >= SafetyAction.RAMP_DOWN
            usable_at = watch.usable_at
            known = usable_at is not None and now - usable_at <= LIMITS.hr_stale_freeze_after
            if watch.above_since is not None and known:
                held = now - watch.above_since
                assert held < LIMITS.hr_hard_max_dwell + 2 * TICK or watch.ended, (held, reading)


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(readings=st.lists(_NEAR_THE_LIMIT, min_size=20, max_size=40))
async def test_safety_wins_over_regulation_for_any_mix_of_readings_with_and_without_a_rate(
    readings: list[_Fed],
) -> None:
    """Over arbitrary readings, one a second, most of them carrying no rate.

    Through the real runtime and control law, which is asking to accelerate at
    the start of each run: see :func:`_feed_and_judge` for the three things
    asserted after every tick. Nothing is built into these sequences; the
    rates are drawn around the hard maximum so that an excess followed by a
    silence turns up in most runs of forty of them.
    """
    rig = await _running_rig()
    await _feed_and_judge(rig, readings, _Watch())


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    before=st.lists(_QUIET_READING, max_size=6),
    excess=_EXCESS,
    lost=st.lists(_NO_RATE, min_size=6, max_size=9),
    after=st.lists(_ANY_READING, max_size=8),
)
async def test_an_excess_then_readings_with_no_rate_ends_the_session_whatever_surrounds_it(
    before: list[_Fed], excess: list[int], lost: list[SignalQuality], after: list[_Fed]
) -> None:
    """Every example holds the sequence the rule exists for, wherever it falls.

    Some readings under the limit or with no rate; then one to four readings
    above the hard maximum, which is never five seconds of readings; then six
    to nine readings with no rate, of any grade; then anything. In every one
    of them ``hr_hard_max`` fires before the last usable rate is stale, the
    session is ended, and the three things :func:`_feed_and_judge` asserts
    hold on every tick, before, during and after.

    The arbitrary sequences of the test above only sometimes contain an excess
    followed by a long enough silence; here each one does, so a level rule that
    let a reading with no rate restart its dwell fails this on every example.
    """
    rig = await _running_rig()
    watch = _Watch()
    await _feed_and_judge(rig, before, watch)
    fired_before = RULE_HR_HARD_MAX in watch.rules
    await _feed_and_judge(rig, [*excess, *lost], watch)
    fired = RULE_HR_HARD_MAX in watch.rules
    ended = watch.ended
    await _feed_and_judge(rig, after, watch)

    assert not fired_before
    assert fired
    assert ended

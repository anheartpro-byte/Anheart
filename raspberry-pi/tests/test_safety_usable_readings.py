from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.result import is_ok
from src.training.runtime import RuntimeState
from src.training.safety import (
    RULE_HR_DROP,
    RULE_HR_RATE,
    RULE_HR_STALE,
    RULE_HR_UNRESPONSIVE,
    SafetyObservation,
    SafetySupervisor,
)
from src.training.types import HeartRateSample, SafetyAction, SafetyVerdict, SignalQuality
from src.units import Bpm, GLoad, Monotonic, Seconds
from tests.test_runtime import TICK, build_rig
from tests.test_safety import START, Rig

JUDGED: Final[frozenset[str]] = frozenset(
    {RULE_HR_STALE, RULE_HR_DROP, RULE_HR_RATE, RULE_HR_UNRESPONSIVE}
)


def sample(at: float, seq: int, bpm: int | None) -> HeartRateSample:
    return HeartRateSample(
        at=Monotonic(START + at),
        seq=seq,
        bpm=None if bpm is None else Bpm(bpm),
        quality=SignalQuality.NO_SIGNAL if bpm is None else SignalQuality.GOOD,
    )


def observation(
    at: float,
    latest: HeartRateSample | None,
    usable: HeartRateSample | None,
    *,
    load: float = 0.3,
) -> SafetyObservation:
    return replace(
        Rig().observation(),
        now=Monotonic(START + at),
        elapsed=Seconds(at),
        heart_rate=latest,
        last_usable_heart_rate=usable,
        attendant_last_seen=Monotonic(START + at),
        commanded_g=GLoad(load),
    )


def judged(supervisor: SafetySupervisor) -> tuple[SafetyVerdict, ...]:
    return tuple(verdict for verdict in supervisor.live if verdict.rule in JUDGED)


def action(supervisor: SafetySupervisor, rule: str) -> SafetyAction:
    return next(
        (verdict.action for verdict in supervisor.live if verdict.rule == rule),
        SafetyAction.NONE,
    )


def test_a_usable_reading_each_second_overtaken_before_every_tick_never_goes_stale() -> None:
    supervisor = Rig().supervisor
    for second in range(71):
        usable = sample(second, 2 * second + 1, 100)
        missing = sample(second + 0.1, 2 * second + 2, None)
        for tick in range(1, 6):
            supervisor.evaluate(observation(second + tick / 5, missing, usable))
            assert action(supervisor, RULE_HR_STALE) is SafetyAction.NONE
    assert supervisor.retained_samples == 71


def test_a_truly_lost_signal_escalates_from_the_overtaken_readings_measurement_instant() -> None:
    hidden, direct = Rig().supervisor, Rig().supervisor
    usable = sample(3, 1, 100)
    missing = sample(3.125, 2, None)
    hidden.evaluate(observation(3.25, missing, usable))
    direct.evaluate(observation(3.25, usable, usable))
    expected = (
        (10.0, SafetyAction.NONE),
        (10.25, SafetyAction.FREEZE),
        (30.0, SafetyAction.FREEZE),
        (30.25, SafetyAction.REDUCE),
        (60.0, SafetyAction.REDUCE),
        (60.25, SafetyAction.RAMP_DOWN),
    )
    for age, wanted in expected:
        for supervisor in (hidden, direct):
            supervisor.evaluate(observation(3 + age, missing, usable))
            assert action(supervisor, RULE_HR_STALE) is wanted
        assert judged(hidden) == judged(direct)


@pytest.mark.parametrize(
    ("rule", "rates"),
    [
        (RULE_HR_DROP, (120,) * 10 + (80,) * 8),
        (RULE_HR_RATE, tuple(range(70, 102))),
        (RULE_HR_UNRESPONSIVE, (100,) * 250),
    ],
)
def test_an_overtaken_usable_trend_fires_on_the_same_tick_as_a_visible_trend(
    rule: str,
    rates: tuple[int, ...],
) -> None:
    hidden, direct = Rig().supervisor, Rig().supervisor
    firings: list[int] = []
    for second, bpm in enumerate(rates):
        usable = sample(second, 2 * second + 1, bpm)
        missing = sample(second + 0.125, 2 * second + 2, None)
        load = 0.1 if second < 120 else 0.4
        hidden.evaluate(observation(second + 0.25, missing, usable, load=load))
        direct.evaluate(observation(second + 0.25, usable, usable, load=load))
        assert judged(hidden) == judged(direct)
        if action(hidden, rule) is not SafetyAction.NONE:
            firings.append(second)
    assert firings
    assert hidden.retained_samples == direct.retained_samples == len(rates)


def test_a_reading_crossing_fields_and_repeated_ticks_counts_once_for_drop_persistence() -> None:
    supervisor = Rig().supervisor
    for second in range(10):
        usable = sample(second, second + 1, 120)
        supervisor.evaluate(observation(second, usable, usable))
    for second in range(10, 15):
        usable = sample(second, second + 1, 80)
        supervisor.evaluate(observation(second, usable, usable))
        for repeat in range(1, 4):
            supervisor.evaluate(observation(second + repeat / 4, None, usable))
            assert action(supervisor, RULE_HR_DROP) is SafetyAction.NONE
    usable = sample(15, 16, 80)
    supervisor.evaluate(observation(15.25, sample(15.125, 17, None), usable))
    assert supervisor.retained_samples == 16
    assert action(supervisor, RULE_HR_DROP) is SafetyAction.RAMP_DOWN


@pytest.mark.parametrize("retained_only", [True, False])
def test_repeated_and_decreasing_sequences_neither_extend_freshness_nor_add_trend_evidence(
    retained_only: bool,
) -> None:
    supervisor = Rig().supervisor
    original = sample(0, 10, 100)
    supervisor.evaluate(observation(0.25, sample(0.125, 11, None), original))
    for tick, seq in enumerate((10, 9, 0, 10), start=1):
        replay = sample(tick * 3, seq, 140)
        supervisor.evaluate(
            observation(
                tick * 3,
                None if retained_only else replay,
                replay if retained_only else None,
            )
        )
        assert supervisor.retained_samples == 1
    assert action(supervisor, RULE_HR_STALE) is SafetyAction.FREEZE


def test_a_pre_session_reading_overtaken_before_its_first_tick_leaves_no_staleness_gap() -> None:
    supervisor = Rig().supervisor
    usable = sample(-6, 1, 152)
    missing = sample(0, 2, None)
    supervisor.evaluate(observation(0, missing, usable))
    supervisor.evaluate(observation(4, missing, usable))
    assert action(supervisor, RULE_HR_STALE) is SafetyAction.NONE
    supervisor.evaluate(observation(4.25, missing, usable))
    assert action(supervisor, RULE_HR_STALE) is SafetyAction.FREEZE
    assert {verdict.rule for verdict in supervisor.live} == {RULE_HR_STALE}


async def test_two_usable_readings_between_ticks_contribute_only_the_last_one_once() -> None:
    supervisor = Rig().supervisor
    first = sample(0, 1, 90)
    last = sample(0.5, 2, 100)
    latest = sample(0.625, 3, None)
    assert first.seq < last.seq < latest.seq
    supervisor.evaluate(observation(0.75, latest, last))
    supervisor.evaluate(observation(1, latest, last))
    assert supervisor.retained_samples == 1
    supervisor.evaluate(observation(10.5, latest, last))
    assert action(supervisor, RULE_HR_STALE) is SafetyAction.NONE
    supervisor.evaluate(observation(10.75, latest, last))
    assert action(supervisor, RULE_HR_STALE) is SafetyAction.FREEZE
    rig = build_rig()
    assert is_ok(await rig.start())
    rig.feed(Bpm(158))
    rig.clock.advance(Seconds(0.125))
    rig.feed(Bpm(100))
    rig.feed(None, quality=SignalQuality.NO_SIGNAL)
    await rig.step(feed=False)
    assert rig.runtime.supervisor.retained_samples == 1
    assert rig.runtime.standing is None
    await rig.run(1, feed=False)
    assert rig.runtime.supervisor.retained_samples == 1


_BATCH: Final[st.SearchStrategy[tuple[int | None, int, int]]] = st.tuples(
    st.one_of(st.none(), st.integers(min_value=60, max_value=140)),
    st.integers(min_value=0, max_value=5),
    st.integers(min_value=0, max_value=5),
)


@settings(max_examples=100, deadline=None)
@given(st.lists(_BATCH, min_size=1, max_size=75))
def test_staleness_and_trends_match_visible_usable_readings_for_arbitrary_batches(
    batches: list[tuple[int | None, int, int]],
) -> None:
    hidden, direct = Rig().supervisor, Rig().supervisor
    retained: HeartRateSample | None = None
    visible: HeartRateSample | None = None
    latest: HeartRateSample | None = None
    seq = 0
    for second, (bpm, before, after) in enumerate(batches):
        for index in range(before):
            seq += 1
            latest = sample(second + index / 32, seq, None)
        if bpm is not None:
            seq += 1
            retained = sample(second + 0.25, seq, bpm)
            latest = retained
            visible = retained
        for index in range(after):
            seq += 1
            latest = sample(second + 0.5 + index / 32, seq, None)
        at = second + 0.75
        load = 0.1 + 0.005 * second
        hidden.evaluate(observation(at, latest, retained, load=load))
        direct.evaluate(observation(at, visible, None, load=load))
        assert judged(hidden) == judged(direct)
        assert hidden.retained_samples == direct.retained_samples
    for age in (10.25, 30.25, 60.25):
        at = len(batches) + age
        hidden.evaluate(observation(at, latest, retained))
        direct.evaluate(observation(at, visible, None))
        assert judged(hidden) == judged(direct)


@settings(max_examples=60, deadline=None)
@given(st.lists(st.integers(min_value=1, max_value=8), min_size=1, max_size=40))
def test_staleness_uses_the_last_of_arbitrarily_many_usable_readings_between_ticks(
    counts: list[int],
) -> None:
    hidden, direct = Rig().supervisor, Rig().supervisor
    seq = 0
    retained = sample(-1, seq, 100)
    for second, count in enumerate(counts):
        for index in range(count):
            seq += 1
            retained = sample(second + index / 16, seq, 100)
        seq += 1
        missing = sample(second + 0.5, seq, None)
        hidden.evaluate(observation(second + 0.75, missing, retained))
        direct.evaluate(observation(second + 0.75, retained, None))
        assert action(hidden, RULE_HR_STALE) == action(direct, RULE_HR_STALE)
    for age in (10.0, 10.25, 30.0, 30.25, 60.0, 60.25):
        at = float(retained.at - START) + age
        hidden.evaluate(observation(at, None, retained))
        direct.evaluate(observation(at, retained, None))
        assert judged(hidden) == judged(direct)


@dataclass(frozen=True, slots=True)
class RuntimeReplay:
    stale: tuple[tuple[float, SafetyAction], ...]
    ended_at: float | None
    final_state: RuntimeState


async def replay_overtaken_readings(duration: int = 71) -> RuntimeReplay:
    rig = build_rig()
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    await rig.run(11)
    rig.fed_bpm = Bpm(65)
    await rig.run(9)
    assert rig.runtime.applied_rpm > 0
    began = float(rig.now)
    events: list[tuple[float, SafetyAction]] = []
    previous = SafetyAction.NONE
    ended_at: float | None = None
    for tick in range(round(duration / TICK)):
        if tick % 5 == 0:
            rig.feed(Bpm(65))
            rig.feed(None, quality=SignalQuality.NO_SIGNAL)
        before = rig.runtime.applied_rpm
        await rig.step(feed=False)
        current = action(rig.runtime.supervisor, RULE_HR_STALE)
        if current is not previous:
            events.append((round(float(rig.now) - began, 6), current))
            previous = current
        if ended_at is None and rig.runtime.end_reason is not None:
            ended_at = round(float(rig.now) - began, 6)
        if rig.runtime.standing_action is not SafetyAction.NONE:
            assert rig.runtime.applied_rpm <= before
    return RuntimeReplay(tuple(events), ended_at, rig.state())


async def test_the_runtime_keeps_running_with_an_overtaken_usable_reading_each_second() -> None:
    replay = await replay_overtaken_readings()
    assert replay.stale == ()
    assert replay.ended_at is None
    assert replay.final_state is RuntimeState.RUNNING


async def test_the_runtime_escalates_a_real_loss_from_the_last_overtaken_usable_reading() -> None:
    rig = build_rig()
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    await rig.run(11)
    rig.fed_bpm = Bpm(65)
    await rig.run(9)
    rig.feed(Bpm(65))
    rig.feed(None, quality=SignalQuality.NO_SIGNAL)
    began = float(rig.now)
    events: list[tuple[float, SafetyAction]] = []
    previous = SafetyAction.NONE
    for _ in range(round(62 / TICK)):
        rig.feed(None, quality=SignalQuality.NO_SIGNAL)
        before = rig.runtime.applied_rpm
        await rig.step(feed=False)
        current = action(rig.runtime.supervisor, RULE_HR_STALE)
        if current is not previous:
            events.append((float(rig.now) - began, current))
            previous = current
        if rig.runtime.standing_action is not SafetyAction.NONE:
            assert rig.runtime.applied_rpm <= before
    assert [entry[1] for entry in events] == [
        SafetyAction.FREEZE,
        SafetyAction.REDUCE,
        SafetyAction.RAMP_DOWN,
    ]
    for (at, _), threshold in zip(events, (10, 30, 60), strict=True):
        assert threshold - 1e-6 <= at <= threshold + TICK + 1e-6


@pytest.mark.parametrize("rates", [(120,) * 10 + (80,) * 8, tuple(range(70, 102))])
def test_duplicates_do_not_flatten_a_trend_delivered_as_retained_evidence(
    rates: Sequence[int],
) -> None:
    hidden, direct = Rig().supervisor, Rig().supervisor
    for second, bpm in enumerate(rates):
        usable = sample(second, second * 2 + 1, bpm)
        missing = sample(second + 0.125, second * 2 + 2, None)
        for tick in range(1, 4):
            at = second + tick / 4
            hidden.evaluate(observation(at, missing, usable))
            direct.evaluate(observation(at, usable, None))
            assert judged(hidden) == judged(direct)
        assert hidden.retained_samples == direct.retained_samples == second + 1
    assert any(verdict.rule in {RULE_HR_DROP, RULE_HR_RATE} for verdict in judged(hidden))

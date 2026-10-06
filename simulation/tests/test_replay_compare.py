"""Independent decision fixtures; no runtime outputs are used as their own oracle."""

from dataclasses import replace
from typing import Final

import pytest

from simulation.replay_compare import Decision, RuleKey, SafetyDecision, compare_decisions
from simulation.replay_report import UNPRINTABLE_RULE, DifferenceCode, printable_rule
from src.training.types import Phase, SafetyAction
from src.units import MotorRpm, Seconds

CLEAR: Final = SafetyDecision(SafetyAction.NONE, None)
FREEZE: Final = SafetyDecision(SafetyAction.FREEZE, RuleKey("ecg_stale"))
REDUCE: Final = SafetyDecision(SafetyAction.REDUCE, RuleKey("hr_high"))
GRID: Final = (0.0, 0.2, 0.7, 8.0, 8.1, 19.0)


def decisions(states: tuple[SafetyDecision, ...]) -> tuple[Decision, ...]:
    return tuple(
        Decision(Seconds(GRID[i]), MotorRpm(150), Phase.HOLD, state)
        for i, state in enumerate(states)
    )


@pytest.mark.parametrize("delta", [-2, -1, 0, 1, 2])
def test_setpoint_boundary(delta: int) -> None:
    # Given independent actual setpoints.
    expected = decisions((CLEAR, CLEAR))
    actual = tuple(replace(row, setpoint=MotorRpm(150 + delta)) for row in expected)
    # When
    report = compare_decisions(expected, actual)
    # Then
    assert report.matches is (abs(delta) <= 1)
    if abs(delta) > 1:
        assert report.first is not None
        assert (report.first.code, report.first.index, report.first.t) == (
            DifferenceCode.SETPOINT,
            0,
            Seconds(0.0),
        )
        assert report.counts == ((DifferenceCode.SETPOINT, 2),)
    else:
        assert report.tolerated_rpm == (2 * (delta == -1), 2 * (delta == 1))


@pytest.mark.parametrize("offset", [-2, -1, 0, 1, 2])
def test_transition_boundary_nonuniform(offset: int) -> None:
    # Given a verdict at index 3, including an eight-second stall.
    expected = decisions((CLEAR,) * 3 + (FREEZE,) * 3)
    actual = decisions((CLEAR,) * (3 + offset) + (FREEZE,) * (3 - offset))
    # When
    report = compare_decisions(expected, actual)
    # Then
    assert report.matches is (abs(offset) <= 1)
    if abs(offset) > 1:
        assert report.first is not None
        assert (report.first.code, report.first.index, report.first.t) == (
            DifferenceCode.TRANSITION_TIMING,
            min(3, 3 + offset),
            Seconds(0.2 if offset == -2 else 8.0),
        )
    else:
        assert report.shifted_transitions == (int(offset == -1), int(offset == 1))


@pytest.mark.parametrize(
    ("expected_states", "actual_states", "matches"),
    [
        ((CLEAR, FREEZE, CLEAR, CLEAR), (CLEAR, CLEAR, FREEZE, CLEAR), True),
        ((CLEAR, FREEZE, REDUCE, CLEAR), (CLEAR, REDUCE, FREEZE, CLEAR), False),
        ((CLEAR, FREEZE, FREEZE, CLEAR), (CLEAR, FREEZE, CLEAR, CLEAR), True),
        ((CLEAR, FREEZE, FREEZE, CLEAR), (CLEAR, FREEZE, FREEZE, FREEZE), False),
        ((CLEAR, FREEZE, FREEZE, FREEZE), (CLEAR, FREEZE, CLEAR, FREEZE), False),
        ((CLEAR, CLEAR, CLEAR, CLEAR), (CLEAR, FREEZE, CLEAR, CLEAR), False),
        ((FREEZE, FREEZE), (CLEAR, FREEZE), False),
        ((CLEAR, FREEZE), (CLEAR, REDUCE), False),
    ],
)
def test_altered_duplicate_missing_clearing_transitions(
    expected_states: tuple[SafetyDecision, ...],
    actual_states: tuple[SafetyDecision, ...],
    matches: bool,
) -> None:
    # Given
    expected, actual = decisions(expected_states), decisions(actual_states)
    # When
    report = compare_decisions(expected, actual)
    # Then
    assert report.matches is matches


def test_phase_exact_even_at_tolerated_transition_boundary() -> None:
    # Given
    expected = decisions((CLEAR, FREEZE, FREEZE))
    actual = list(decisions((CLEAR, CLEAR, FREEZE)))
    actual[1] = replace(actual[1], phase=Phase.COOLDOWN)
    # When
    report = compare_decisions(expected, tuple(actual))
    # Then
    assert report.first is not None
    assert (report.first.code, report.first.index, report.first.t) == (
        DifferenceCode.PHASE,
        1,
        Seconds(0.2),
    )


@pytest.mark.parametrize("lengths", [(0, 0), (0, 1), (1, 0), (2, 1), (1, 2)])
def test_empty_and_missing_ticks(lengths: tuple[int, int]) -> None:
    # Given
    expected = decisions((CLEAR,) * lengths[0])
    actual = decisions((CLEAR,) * lengths[1])
    # When
    report = compare_decisions(expected, actual)
    # Then
    assert report.matches is (lengths[0] == lengths[1])
    if report.first is not None:
        assert report.first.code is DifferenceCode.TICK_COUNT
        assert report.first.index == min(lengths)
        assert report.first.t == Seconds(0.0 if min(lengths) == 0 else 0.2)


def test_grid_exact_and_earliest_cause_over_later_transition() -> None:
    # Given
    expected = decisions((CLEAR, CLEAR, FREEZE, FREEZE))
    actual = list(decisions((CLEAR, CLEAR, CLEAR, CLEAR)))
    actual[1] = replace(actual[1], t=Seconds(0.201))
    # When
    report = compare_decisions(expected, tuple(actual))
    # Then
    assert report.first is not None
    assert (report.first.code, report.first.index, report.first.t) == (
        DifferenceCode.TICK_GRID,
        1,
        Seconds(0.2),
    )


@pytest.mark.parametrize(
    "sentinel",
    [
        "ecg_stale_SUBJECT_847_ORG_62_/home/operator/token=SYNTHETIC",
        "hr_high_12345678-abcd-abcd-abcd-123456789abc",
        "drive_fault_OPERATOR_987_RAW_HR_163_ECG_512_exception_text",
    ],
)
def test_privacy_same_action_changed_rule_and_bounded_report(sentinel: str) -> None:
    # Given plausible rule labels containing identifiers and free text.
    expected = tuple(
        Decision(Seconds(i / 5), MotorRpm(150), Phase.HOLD, CLEAR if i == 0 else FREEZE)
        for i in range(2000)
    )
    actual = tuple(
        replace(row, safety=SafetyDecision(SafetyAction.FREEZE, RuleKey(sentinel))) if i else row
        for i, row in enumerate(expected)
    )
    # When
    report = compare_decisions(expected, actual)
    # Then
    assert not report.matches
    assert report.first is not None
    assert report.first.rule_changed
    assert report.first.index == 1
    assert sentinel not in report.to_json() + report.to_text() + repr(report)
    assert report.first.expected is not None
    assert report.first.actual is not None
    assert report.first.expected.rule == "ecg_stale"
    assert report.first.actual.rule == UNPRINTABLE_RULE
    assert "SUBJECT" not in report.to_json() + report.to_text()
    assert len(report.to_json()) < 2000
    assert (DifferenceCode.SAFETY_STATE, 1999) in report.counts


def test_nonuniform_index_ten_to_twelve() -> None:
    # Given independent expected index/time, never a fixed 200 ms reconstruction.
    times = (0.0, 0.1, 0.4, 1.2, 1.3, 2.0, 8.0, 9.0, 9.1, 20.0, 40.5, 41.0, 55.0)
    expected = tuple(
        Decision(Seconds(t), MotorRpm(150), Phase.HOLD, CLEAR if i < 10 else FREEZE)
        for i, t in enumerate(times)
    )
    actual = tuple(
        Decision(Seconds(t), MotorRpm(150), Phase.HOLD, CLEAR if i < 12 else FREEZE)
        for i, t in enumerate(times)
    )
    # When
    report = compare_decisions(expected, actual)
    # Then
    assert report.first is not None
    assert (report.first.code, report.first.index, report.first.t) == (
        DifferenceCode.TRANSITION_TIMING,
        10,
        Seconds(40.5),
    )
    assert (report.first.expected_transition, report.first.actual_transition) == (10, 12)


def test_determinism_canonical_empty_report() -> None:
    # Given
    expected: tuple[Decision, ...] = ()
    # When
    first, second = compare_decisions(expected, ()), compare_decisions((), expected)
    # Then: pinned public serialization contract, independent of actual output.
    assert (
        first.to_json()
        == second.to_json()
        == (
            '{"actual_ticks":0,"counts":{},"expected_ticks":0,"first":null,'
            '"matches":true,"shifted_transitions":[0,0],"tolerated_rpm":[0,0]}\n'
        )
    )
    assert first.to_text() == second.to_text()


def test_determinism_uses_independent_expected_and_actual_summaries() -> None:
    # Given
    expected = (Decision(Seconds(3.75), MotorRpm(111), Phase.HOLD, FREEZE),)
    actual = (Decision(Seconds(3.75), MotorRpm(222), Phase.RECOVERY, REDUCE),)
    # When
    report, again = compare_decisions(expected, actual), compare_decisions(expected, actual)
    # Then
    assert report.document() == {
        "expected_ticks": 1,
        "actual_ticks": 1,
        "matches": False,
        "counts": {"setpoint": 1, "phase": 1, "safety_state": 1},
        "tolerated_rpm": (0, 0),
        "shifted_transitions": (0, 0),
        "first": {
            "code": "setpoint",
            "index": 0,
            "t": 3.75,
            "rule_changed": True,
            "expected_transition": None,
            "actual_transition": None,
            "expected": {
                "t": 3.75,
                "setpoint": 111,
                "phase": "hold",
                "action": "FREEZE",
                "rule": "ecg_stale",
            },
            "actual": {
                "t": 3.75,
                "setpoint": 222,
                "phase": "recovery",
                "action": "REDUCE",
                "rule": "hr_high",
            },
        },
    }
    assert report.to_json().encode() == again.to_json().encode()
    assert report.to_text().encode() == again.to_text().encode()


def test_boundary_cannot_match_adjacent_whole_rows() -> None:
    # Given
    expected = (
        Decision(Seconds(0), MotorRpm(100), Phase.HOLD, CLEAR),
        Decision(Seconds(1), MotorRpm(200), Phase.HOLD, FREEZE),
        Decision(Seconds(2), MotorRpm(300), Phase.HOLD, FREEZE),
    )
    actual = (
        Decision(Seconds(0), MotorRpm(100), Phase.HOLD, CLEAR),
        Decision(Seconds(1), MotorRpm(100), Phase.HOLD, CLEAR),
        Decision(Seconds(2), MotorRpm(200), Phase.HOLD, FREEZE),
    )
    # When
    report = compare_decisions(expected, actual)
    # Then
    assert report.first is not None
    assert (report.first.code, report.first.index, report.first.t) == (
        DifferenceCode.SETPOINT,
        1,
        Seconds(1),
    )
    assert report.counts == ((DifferenceCode.SETPOINT, 2),)


@pytest.mark.parametrize(
    ("rule", "shown"),
    [
        (None, None),
        ("hr_stale", "hr_stale"),
        ("operator_estop", "operator_estop"),
        ("rule2", "rule2"),
        ("", UNPRINTABLE_RULE),
        ("Hr_stale", UNPRINTABLE_RULE),
        ("hr stale", UNPRINTABLE_RULE),
        ("2fast", UNPRINTABLE_RULE),
        ("a" * 49, UNPRINTABLE_RULE),
    ],
)
def test_a_recorded_rule_is_shown_only_when_it_is_a_plain_identifier(
    rule: str | None, shown: str | None
) -> None:
    assert printable_rule(rule) == shown

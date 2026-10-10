"""Tests for the training vocabulary.

Most of ``src/training/types.py`` is declarations, and a test that an enum has
the members it has proves nothing. These tests are evidence for the claims that
three other modules are about to be written against, and each of those claims
is something that can actually be wrong at runtime:

1. **The severity ordering is real.** Precedence in the safety layer is
   implemented as a ``max`` over :class:`SafetyAction`, so the order of the
   members *is* the policy. It is pinned here member by member, checked
   pairwise, and checked over every one of the 64 subsets, because a reordering
   would otherwise be a silent policy change that no other test could see.
2. **``SignalQuality.from_metric`` is total and never optimistic.** Property
   tests over arbitrary strings, plus a cross-check against the strings
   ``src/signal_processing.py`` actually returns - so a rename in the pipeline
   fails here instead of silently downgrading every reading to NO_SIGNAL.
3. **The sequence gate rejects a re-emitted metrics dict.** The pipeline
   re-emits its previous metrics when extraction fails, so this is the one
   input trap in the system that looks like perfectly healthy data.
4. **The display rule holds, and is never optimistic.** A missing age reads as
   stale; a stale or untrustworthy reading yields no ``live_bpm``.
5. **Every record is immutable and slotted**, so an observation cannot change
   under a decision taken from it.
6. **The field names and annotations are the contract.** Pinned with
   ``dataclasses.fields`` so that renaming or retyping a field is a deliberate
   act with a test diff, not a surprise for whichever agent built against it.

The numbers in the ``SpeedView`` tests are derived by hand from the
commissioning figures rather than from the functions under test - see that
section.
"""

from __future__ import annotations

import ast
import itertools
import re
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.motor.drive import DriveFault, DriveState, FaultReport, describe_fault
from src.training.types import (
    DRIVE_STATUS_STALE_AFTER,
    HEART_RATE_STALE_AFTER,
    RULE_ID_PATTERN,
    ControlDecision,
    HeartRateSample,
    Phase,
    SafetyAction,
    SafetyVerdict,
    SignalQuality,
    SpeedView,
    TelemetrySnapshot,
    ZoneCounters,
    is_rule_id,
    most_severe,
)
from src.units import (
    Amperes,
    Bpm,
    GearRatio,
    Hertz,
    Metres,
    Monotonic,
    MotorRpm,
    RawRegister,
    Seconds,
    UnixMillis,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TYPES_SOURCE = PROJECT_ROOT / "src" / "training" / "types.py"
SIGNAL_PROCESSING_SOURCE = PROJECT_ROOT / "src" / "signal_processing.py"

#: The quality grader in the signal pipeline. Its returned string literals are
#: the wire format :class:`SignalQuality` claims to parse.
GRADER_FUNCTION: Final[str] = "_ecg_quality"

#: Severity, lowest first. This literal is the safety policy: the supervisor
#: resolves competing verdicts by taking the maximum, so this order decides
#: which rule wins. Written out rather than derived from the enum, because
#: deriving it from the thing under test would prove nothing.
SEVERITY_ORDER: Final[tuple[SafetyAction, ...]] = (
    SafetyAction.NONE,
    SafetyAction.FREEZE,
    SafetyAction.REDUCE,
    SafetyAction.RAMP_DOWN,
    SafetyAction.QUICK_STOP,
    SafetyAction.GO_SILENT,
)

#: The exact strings src/signal_processing.py emits. Duplicated deliberately:
#: the test below compares them against the pipeline's own source, so this
#: literal is evidence rather than an import of the value under test.
WIRE_QUALITIES: Final[frozenset[str]] = frozenset({"no_signal", "mains_dominated", "noisy", "good"})


# =========================================================================
# Helpers
# =========================================================================


def _assign(target: object, name: str, value: object) -> None:
    """Attempt an attribute assignment the type checker knows is illegal.

    Exists so the immutability tests can try the mutation at runtime without a
    suppression comment: ``sample.bpm = ...`` is a type error, which is the
    *static* half of the guarantee, while these tests check the runtime half
    still holds for code that got past the checkers some other way (an untyped
    caller, a dict of objects, a future refactor).
    """
    setattr(target, name, value)


def _sample(
    *,
    bpm: Bpm | None = Bpm(72),
    quality: SignalQuality = SignalQuality.GOOD,
    seq: int = 1,
    at: Monotonic = Monotonic(100.0),
) -> HeartRateSample:
    """A heart-rate sample with the fields a given test does not care about filled in."""
    return HeartRateSample(bpm=bpm, quality=quality, seq=seq, at=at)


def _verdict(
    *,
    action: SafetyAction = SafetyAction.REDUCE,
    rule: str = "hr_above_zone",
    detail: str = "heart rate 156 bpm, zone ceiling 150 bpm.",
    latched: bool = False,
    since: Monotonic = Monotonic(50.0),
) -> SafetyVerdict:
    return SafetyVerdict(action=action, rule=rule, detail=detail, latched=latched, since=since)


#: The machine's geometry, from the commissioning notes: SEW KA37 i = 49.79,
#: DRS71S4 at 1380 rpm / 50 Hz, occupant at 1.5 m from the axis. Passed
#: explicitly on every call because ``SpeedView`` deliberately has no defaults.
RATIO: Final[GearRatio] = GearRatio(49.79)
RADIUS: Final[Metres] = Metres(1.5)
NOMINAL_RPM: Final[MotorRpm] = MotorRpm(1380)
BASE_HZ: Final[Hertz] = Hertz(50.0)


def _view(rpm: int) -> SpeedView:
    """One speed, in the four units, for this machine's geometry."""
    return SpeedView.from_motor_rpm(
        MotorRpm(rpm), ratio=RATIO, radius=RADIUS, nominal_rpm=NOMINAL_RPM, base_hz=BASE_HZ
    )


def _decision(
    *,
    target_bpm: Bpm | None = Bpm(130),
    error_bpm: float = 8.0,
    in_deadband: bool = False,
) -> ControlDecision:
    return ControlDecision(
        desired_rpm=MotorRpm(640),
        phase=Phase.HOLD,
        error_bpm=error_bpm,
        in_deadband=in_deadband,
        target_bpm=target_bpm,
        reason="heart rate 122 bpm, 8 below target; stepping up.",
    )


def _snapshot(
    *,
    heart_rate: HeartRateSample | None = None,
    heart_rate_age: Seconds | None = None,
    drive_status_age: Seconds | None = Seconds(0.2),
    safety: SafetyVerdict | None = None,
    fault: FaultReport | None = None,
    drive_state: DriveState = DriveState.OPERATION_ENABLED,
) -> TelemetrySnapshot:
    """A snapshot with everything a given test does not care about filled in."""
    return TelemetrySnapshot(
        at=Monotonic(100.0),
        wall_clock=UnixMillis(1_700_000_000_000),
        phase=Phase.HOLD,
        elapsed=Seconds(600.0),
        remaining=Seconds(1500.0),
        heart_rate=heart_rate,
        heart_rate_age=heart_rate_age,
        target_bpm=Bpm(130),
        setpoint=_view(700),
        measured=_view(695),
        setpoint_confirmed=True,
        drive_state=drive_state,
        drive_status_age=drive_status_age,
        current=Amperes(1.8),
        fault=fault,
        safety=safety,
        counters=ZoneCounters(
            in_zone=Seconds(300.0), above_zone=Seconds(60.0), below_zone=Seconds(90.0)
        ),
    )


# =========================================================================
# Phase
# =========================================================================


def test_phase_values_are_the_stable_identifiers_the_log_stores() -> None:
    """Pinned because stored sessions and the web UI key on these strings.

    A renamed value silently reinterprets every historical record, so renaming
    one has to break this test first.
    """
    assert Phase.BASELINE.value == "baseline"
    assert Phase.WARMUP.value == "warmup"
    assert Phase.HOLD.value == "hold"
    assert Phase.COOLDOWN.value == "cooldown"
    assert Phase.RECOVERY.value == "recovery"
    assert Phase.DONE.value == "done"


def test_phase_has_exactly_the_six_documented_members() -> None:
    """A seventh phase changes what every consumer must handle, so it argues here."""
    assert set(Phase) == {
        Phase.BASELINE,
        Phase.WARMUP,
        Phase.HOLD,
        Phase.COOLDOWN,
        Phase.RECOVERY,
        Phase.DONE,
    }


def test_no_phase_carries_an_auto_integer() -> None:
    """Every value is a lowercase identifier, never an ``auto()`` ordinal.

    ``auto()`` renumbers on a reorder, which would rewrite the meaning of
    stored sessions. This is the check that keeps that decision from being
    undone by a well-meaning tidy-up.
    """
    for phase in Phase:
        assert isinstance(phase.value, str), phase
        assert phase.value == phase.name.lower(), phase


def test_phases_are_deliberately_not_orderable() -> None:
    """A phase is a label, not a position: a safety stop jumps WARMUP to COOLDOWN.

    ``Phase`` is a plain ``Enum`` rather than an ``IntEnum`` precisely so that
    code inferring "later than" from a phase fails to run at all.
    """
    with pytest.raises(TypeError):
        _ = Phase.BASELINE < Phase.HOLD  # type: ignore[operator]  # asserting the absence of an order


# =========================================================================
# SafetyAction - the ordering IS the safety policy
# =========================================================================


def test_safety_actions_are_ordered_by_increasing_severity() -> None:
    """The load-bearing claim: NONE < FREEZE < REDUCE < RAMP_DOWN < QUICK_STOP < GO_SILENT.

    Written as explicit comparisons rather than a loop so that the failure
    message names the pair that is out of order.
    """
    assert SafetyAction.NONE < SafetyAction.FREEZE
    assert SafetyAction.FREEZE < SafetyAction.REDUCE
    assert SafetyAction.REDUCE < SafetyAction.RAMP_DOWN
    assert SafetyAction.RAMP_DOWN < SafetyAction.QUICK_STOP
    assert SafetyAction.QUICK_STOP < SafetyAction.GO_SILENT


def test_the_severity_order_is_total_and_matches_the_documented_sequence() -> None:
    """Sorting the members must reproduce the policy order, and nothing may tie.

    Totality matters as much as the order: ``max`` over a partial order would
    pick whichever verdict happened to come first.
    """
    assert tuple(sorted(SafetyAction)) == SEVERITY_ORDER
    assert len(set(SEVERITY_ORDER)) == len(SEVERITY_ORDER)
    assert set(SafetyAction) == set(SEVERITY_ORDER)


@pytest.mark.parametrize(
    ("lower_index", "higher_index"),
    [(i, j) for i in range(len(SEVERITY_ORDER)) for j in range(len(SEVERITY_ORDER)) if i < j],
)
def test_every_pair_of_actions_compares_the_way_the_policy_says(
    lower_index: int, higher_index: int
) -> None:
    """All 15 ordered pairs, so no single comparison can be wrong in isolation."""
    lower = SEVERITY_ORDER[lower_index]
    higher = SEVERITY_ORDER[higher_index]
    assert lower < higher
    assert higher > lower
    assert max(lower, higher) is higher
    assert min(lower, higher) is lower


def test_go_silent_wins_over_every_other_demand() -> None:
    """ "Stop writing to the drive" must never lose a race with a lesser action.

    It is the only action that does not depend on this process continuing to
    behave, so any combination that includes it must resolve to it.
    """
    assert max(SafetyAction) is SafetyAction.GO_SILENT
    for action in SafetyAction:
        assert most_severe((action, SafetyAction.GO_SILENT)) is SafetyAction.GO_SILENT
        assert most_severe((SafetyAction.GO_SILENT, action)) is SafetyAction.GO_SILENT


def test_the_severity_rank_is_pinned_so_a_reorder_is_visible() -> None:
    """The integers are the rank and nothing else - not a register, not a wire format.

    Pinned so that inserting a member in the middle, which renumbers the rest,
    has to be a deliberate edit here as well.
    """
    assert tuple(int(action) for action in SEVERITY_ORDER) == (0, 1, 2, 3, 4, 5)


# =========================================================================
# most_severe
# =========================================================================


def test_most_severe_of_nothing_is_none() -> None:
    """An empty verdict set must yield NONE, not a ValueError.

    ``max(())`` raises, and a ValueError out of the supervisor would unwind the
    tick with the motor still commanded. This is the whole reason the helper
    exists.
    """
    assert most_severe(()) is SafetyAction.NONE
    assert most_severe([]) is SafetyAction.NONE


@pytest.mark.parametrize(
    "subset",
    [
        subset
        for size in range(1, len(SEVERITY_ORDER) + 1)
        for subset in itertools.combinations(SEVERITY_ORDER, size)
    ],
)
def test_most_severe_picks_the_worst_of_every_possible_verdict_set(
    subset: tuple[SafetyAction, ...],
) -> None:
    """All 63 non-empty subsets: the answer is always the last one in policy order.

    Exhaustive rather than sampled because the combination rule is the safety
    layer's precedence rule, and 63 cases is cheap.
    """
    expected = max(subset, key=SEVERITY_ORDER.index)
    assert most_severe(subset) is expected
    assert most_severe(tuple(reversed(subset))) is expected, "order of arrival must not matter"


@pytest.mark.parametrize(
    "subset",
    [
        subset
        for size in range(1, len(SEVERITY_ORDER) + 1)
        for subset in itertools.combinations(SEVERITY_ORDER, size)
        if any(action is not SafetyAction.NONE for action in subset)
    ],
)
def test_a_quiet_rule_can_never_dilute_a_demand(subset: tuple[SafetyAction, ...]) -> None:
    """If any rule asks for something, the combination must not resolve to NONE.

    The failure this rules out is the dangerous direction of a combination bug:
    a supervisor that averaged, counted votes or took the *first* verdict would
    let a dozen silent rules bury one that mattered.
    """
    assert most_severe(subset) is not SafetyAction.NONE


# =========================================================================
# Rule ids
# =========================================================================


@pytest.mark.parametrize(
    "rule",
    ["hr_above_zone", "hr_stale", "a", "drive_comm_lost", "g_load_ceiling", "rule_42"],
)
def test_well_formed_rule_ids_are_accepted(rule: str) -> None:
    assert is_rule_id(rule) is True


@pytest.mark.parametrize(
    ("rule", "why"),
    [
        ("", "empty is not an id"),
        ("HR_ABOVE_ZONE", "uppercase would make two spellings of one rule"),
        ("1_rule", "must start with a letter"),
        ("_leading", "must start with a letter"),
        ("hr above zone", "a space means it is prose, not an id"),
        ("hr-above-zone", "kebab-case is a different convention"),
        ("hr.above.zone", "dots read as a namespace this system does not have"),
        ("hr_above_zone\n", "a trailing newline is invisible in every log line"),
        ("a" * 65, "an id this long is a sentence"),
    ],
)
def test_malformed_rule_ids_are_rejected(rule: str, why: str) -> None:
    """The newline case is the reason the pattern anchors with ``\\Z`` and not ``$``.

    ``$`` matches *before* a trailing newline, so ``"hr_above_zone\\n"`` would be
    accepted and would then key a dashboard, an alert and a log under an id
    nobody can see the end of.
    """
    assert is_rule_id(rule) is False, why


def test_the_pattern_is_exported_so_rules_do_not_each_invent_one() -> None:
    """Rule modules assert their own ids against this, so it has to be shareable."""
    assert isinstance(RULE_ID_PATTERN, re.Pattern)
    assert RULE_ID_PATTERN.fullmatch("hr_above_zone") is not None


@given(st.text())
def test_is_rule_id_is_total_and_only_accepts_ids_with_the_documented_shape(value: str) -> None:
    """Never raises, and anything it accepts really does have the stated shape.

    The properties are checked against the *description* of a rule id, not
    against a second copy of the regex, so this cannot pass by restating the
    implementation.
    """
    accepted = is_rule_id(value)
    if accepted:
        assert 1 <= len(value) <= 64
        assert value[0].isalpha()
        assert value[0].islower()
        assert all(char.islower() or char.isdigit() or char == "_" for char in value)
        assert value == value.strip()


# =========================================================================
# SafetyVerdict
# =========================================================================


def test_a_verdict_ages_from_when_it_first_fired() -> None:
    """Dwell time separates a spike from a trend, so ``since`` is the first instant."""
    verdict = _verdict(since=Monotonic(50.0))
    assert verdict.age(Monotonic(62.5)) == pytest.approx(12.5)
    assert verdict.age(Monotonic(50.0)) == pytest.approx(0.0)


def test_verdicts_are_deliberately_not_orderable() -> None:
    """``max`` must be taken over actions, never over verdicts.

    A field-by-field dataclass comparison would break ties on ``rule``, i.e.
    alphabetically: a coin toss dressed as a safety decision. The documented
    idiom keeps the object and sorts on the action instead, so that is what is
    checked here.
    """
    quiet = _verdict(action=SafetyAction.FREEZE, rule="zzz_last_alphabetically")
    loud = _verdict(action=SafetyAction.GO_SILENT, rule="aaa_first_alphabetically")

    with pytest.raises(TypeError):
        _ = quiet < loud  # type: ignore[operator]  # asserting the absence of an order

    assert max((quiet, loud), key=lambda verdict: verdict.action) is loud


# =========================================================================
# SignalQuality
# =========================================================================


def _grader_return_strings() -> frozenset[str]:
    """Every string literal ``_ecg_quality`` returns, read out of its own source.

    Located with ``ast`` (so a moved function is found rather than guessed at)
    and then scanned with a regex over those lines, which keeps the untyped
    ``ast.Constant.value`` out of this file entirely.
    """
    source = SIGNAL_PROCESSING_SOURCE.read_text(encoding="utf-8")
    lines = source.splitlines()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == GRADER_FUNCTION:
            assert node.end_lineno is not None, "ast gave the grader no end line"
            body = "\n".join(lines[node.lineno - 1 : node.end_lineno])
            return frozenset(re.findall(r'return "([a-z_]+)"', body))
    raise AssertionError(f"{GRADER_FUNCTION} no longer exists in {SIGNAL_PROCESSING_SOURCE.name}")


def test_the_quality_members_are_the_strings_the_pipeline_actually_emits() -> None:
    """Cross-checked against ``src/signal_processing.py`` itself, not against a memory.

    This is the test that matters for this enum. If the pipeline renames a
    grade, every reading silently becomes NO_SIGNAL - the machine simply stops
    trusting a working ECG, and nothing else in the system can tell why. So the
    parser is pinned to the producer's own source.
    """
    emitted = _grader_return_strings()
    assert emitted == WIRE_QUALITIES, (
        "the grader's vocabulary changed; SignalQuality must be updated to match"
    )
    assert {member.value for member in SignalQuality} == emitted


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ("no_signal", SignalQuality.NO_SIGNAL),
        ("mains_dominated", SignalQuality.MAINS_DOMINATED),
        ("noisy", SignalQuality.NOISY),
        ("good", SignalQuality.GOOD),
    ],
)
def test_each_wire_string_parses_to_its_member(wire: str, expected: SignalQuality) -> None:
    assert SignalQuality.from_metric(wire) is expected


def test_a_missing_quality_field_is_no_signal() -> None:
    """The metrics dict has no ``quality`` key until the pipeline is warm."""
    assert SignalQuality.from_metric(None) is SignalQuality.NO_SIGNAL


@pytest.mark.parametrize(
    "value",
    ["GOOD", "Good", " good", "good ", "goo", "goodish", "excellent", "", "ok", "no-signal"],
)
def test_anything_the_parser_does_not_recognise_is_no_signal(value: str) -> None:
    """Including near-misses of ``good``, which is the dangerous direction.

    An unknown grade is indistinguishable from a renamed grade or a version
    skew, and the only reading of those that cannot hurt somebody is "no usable
    signal".
    """
    assert SignalQuality.from_metric(value) is SignalQuality.NO_SIGNAL


@given(st.text())
def test_from_metric_is_total_over_arbitrary_strings(value: str) -> None:
    """Never raises for any string, and only the four known words are recognised."""
    parsed = SignalQuality.from_metric(value)
    assert parsed in set(SignalQuality)
    if value not in WIRE_QUALITIES:
        assert parsed is SignalQuality.NO_SIGNAL


@given(st.one_of(st.none(), st.text()))
def test_from_metric_is_never_optimistic(value: str | None) -> None:
    """Nothing but the exact word ``good`` may ever yield a trustworthy grade.

    This is the property that stops the machine regulating on hum: the grade is
    what decides whether a heart rate exists at all.
    """
    parsed = SignalQuality.from_metric(value)
    if parsed.is_trustworthy:
        assert value == "good"


def test_only_good_is_trustworthy() -> None:
    """One place answers "may this reading be acted on?"."""
    assert SignalQuality.GOOD.is_trustworthy is True
    for member in SignalQuality:
        if member is not SignalQuality.GOOD:
            assert member.is_trustworthy is False, member


# =========================================================================
# HeartRateSample - the sequence trap
# =========================================================================


def test_a_re_emitted_metrics_dict_is_not_new_evidence() -> None:
    """The trap itself, spelled out.

    ``src/signal_processing.py`` re-emits its previous metrics dict when
    extraction fails, so a second sample can carry the same heart rate with a
    perfectly current timestamp and a *repeated* sequence number. That is not a
    measurement; it is the memory of one. The gate must reject it.
    """
    measured = _sample(bpm=Bpm(88), seq=7, at=Monotonic(100.0))
    re_emitted = _sample(bpm=Bpm(88), seq=7, at=Monotonic(101.0))

    assert measured.is_new_evidence_after(6) is True
    assert re_emitted.is_new_evidence_after(measured.seq) is False, (
        "an unchanged seq means no new evidence, however fresh the timestamp looks"
    )


def test_the_first_sample_of_a_session_is_new_evidence() -> None:
    """Nothing has been seen yet, so there is nothing for it to repeat."""
    assert _sample(seq=0).is_new_evidence_after(None) is True


def test_a_sequence_that_went_backwards_is_not_new_evidence() -> None:
    """A restarted pipeline counts from zero again; those are old numbers.

    This is why the gate is ``>`` and not ``!=``: an inequality test would read
    a counter reset as a fresh measurement and hand the controller a heart rate
    from a previous session.
    """
    assert _sample(seq=3).is_new_evidence_after(9) is False


@given(st.integers(), st.integers())
def test_the_gate_accepts_exactly_the_sequences_that_advanced(seq: int, previous: int) -> None:
    """Over arbitrary counters: accepted if and only if it moved forward.

    Stated as two implications over "advanced" / "did not advance" rather than
    as a copy of the comparison, so the test names a property instead of the
    implementation.
    """
    accepted = _sample(seq=seq).is_new_evidence_after(previous)
    if seq <= previous:
        assert accepted is False
    else:
        assert accepted is True


def test_a_sample_ages_from_when_it_was_taken() -> None:
    sample = _sample(at=Monotonic(100.0))
    assert sample.age(Monotonic(103.5)) == pytest.approx(3.5)


@pytest.mark.parametrize(
    ("bpm", "quality", "expected"),
    [
        (Bpm(72), SignalQuality.GOOD, Bpm(72)),
        (Bpm(72), SignalQuality.NOISY, None),
        (Bpm(72), SignalQuality.MAINS_DOMINATED, None),
        (Bpm(72), SignalQuality.NO_SIGNAL, None),
        (None, SignalQuality.GOOD, None),
        (None, SignalQuality.NO_SIGNAL, None),
    ],
)
def test_a_rate_is_usable_only_when_it_exists_and_its_grade_is_trustworthy(
    bpm: Bpm | None, quality: SignalQuality, expected: Bpm | None
) -> None:
    """Both conditions, joined in one place so no consumer applies only one.

    The ``(72, MAINS_DOMINATED)`` row is the one that matters: mains hum has a
    perfectly steady "rate", and a consumer that checked only for a number
    would regulate a motor on 50 Hz noise.
    """
    assert _sample(bpm=bpm, quality=quality).usable_bpm == expected


# =========================================================================
# ControlDecision
# =========================================================================


def test_a_decision_can_record_a_negative_error_and_no_target() -> None:
    """Both shapes the control law must be able to express.

    A negative error (heart rate above target) has to round-trip as a signed
    float, and a phase with no target must be representable without inventing
    one - ``None`` is not a target of zero.
    """
    above_target = _decision(error_bpm=-14.0)
    assert above_target.error_bpm == pytest.approx(-14.0)

    untargeted = _decision(target_bpm=None, error_bpm=0.0, in_deadband=True)
    assert untargeted.target_bpm is None
    assert untargeted.error_bpm == pytest.approx(0.0)
    assert untargeted.in_deadband is True


# =========================================================================
# SpeedView
# =========================================================================
#
# The expected numbers below are derived by hand from the commissioning
# figures, NOT from the functions under test:
#
#   output rpm = 900 / 49.79                        = 18.075919
#   hertz      = 900 / 1380 * 50                    = 32.608696
#   omega      = 2*pi * 18.075919 / 60              =  1.892905 rad/s
#   g          = omega^2 * 1.5 / 9.80665            =  0.548061
#
# A test that recomputed them with motor_to_output_rpm would agree with any
# mistake that function might contain.


def test_one_speed_is_derived_into_every_unit_at_once() -> None:
    """Hand-computed from the nameplate, so the conversions cannot agree with a bug."""
    view = _view(900)
    assert view.motor_rpm == 900
    assert view.output_rpm == pytest.approx(18.075919, abs=1e-6)
    assert view.hertz == pytest.approx(32.608696, abs=1e-6)
    assert view.g_load == pytest.approx(0.548061, abs=1e-6)
    # sqrt(0.548061^2 + 1): gravity and the centripetal load in quadrature.
    assert view.resultant_g == pytest.approx(1.140338, abs=1e-6)


def test_the_nameplate_point_maps_to_the_nameplate_frequency() -> None:
    """1380 rpm is 50 Hz on this motor; ~27.7 output rpm through i = 49.79."""
    view = _view(1380)
    assert view.hertz == pytest.approx(50.0, abs=1e-9)
    assert view.output_rpm == pytest.approx(27.716409, abs=1e-6)


def test_a_standstill_is_zero_in_every_unit() -> None:
    view = _view(0)
    assert view.motor_rpm == 0
    assert view.output_rpm == pytest.approx(0.0)
    assert view.hertz == pytest.approx(0.0)
    assert view.g_load == pytest.approx(0.0)
    # Gravity does not stop when the machine does.
    assert view.resultant_g == pytest.approx(1.0)


def test_reverse_rotation_keeps_its_sign_but_still_loads_the_occupant() -> None:
    """Load goes as the square of speed, so g is unsigned while the rpm is not.

    A view that reported a negative g would tell a screen that running backwards
    relieves the person of the load. It does not.
    """
    reverse = _view(-900)
    forward = _view(900)
    assert reverse.motor_rpm == -900
    assert reverse.output_rpm == pytest.approx(-18.075919, abs=1e-6)
    assert reverse.hertz == pytest.approx(-32.608696, abs=1e-6)
    assert reverse.g_load == pytest.approx(forward.g_load)
    assert reverse.g_load > 0.0


def test_the_output_shaft_is_fifty_times_slower_than_the_motor() -> None:
    """The confusion this type exists to prevent, stated as a ratio.

    Mixing the two up is a 49.79-fold error and invisible in a plain float.
    """
    view = _view(900)
    assert view.motor_rpm / view.output_rpm == pytest.approx(49.79, abs=1e-9)


# =========================================================================
# ZoneCounters
# =========================================================================


def test_the_counters_total_only_the_time_that_was_actually_measured() -> None:
    """Distinct values, so a transposed field could not produce the same sum.

    The total is deliberately not the elapsed session time: ticks with no
    usable heart rate belong to none of the three buckets, and the gap between
    this total and ``elapsed`` is how much of the session was spent blind.
    """
    counters = ZoneCounters(
        in_zone=Seconds(300.0), above_zone=Seconds(60.0), below_zone=Seconds(15.0)
    )
    assert counters.in_zone == pytest.approx(300.0)
    assert counters.above_zone == pytest.approx(60.0)
    assert counters.below_zone == pytest.approx(15.0)
    assert counters.total == pytest.approx(375.0)


def test_counters_at_the_start_of_a_session_total_nothing() -> None:
    zero = ZoneCounters(in_zone=Seconds(0.0), above_zone=Seconds(0.0), below_zone=Seconds(0.0))
    assert zero.total == pytest.approx(0.0)


# =========================================================================
# TelemetrySnapshot - the display rule
# =========================================================================


def test_a_heart_rate_that_has_never_arrived_counts_as_stale() -> None:
    """Unknown freshness is never treated as fresh. That assumption is the danger."""
    snapshot = _snapshot(heart_rate=None, heart_rate_age=None)
    assert snapshot.heart_rate_is_stale is True
    assert snapshot.live_bpm is None


def test_a_fresh_trustworthy_reading_is_the_one_the_ui_may_show() -> None:
    snapshot = _snapshot(heart_rate=_sample(bpm=Bpm(118)), heart_rate_age=Seconds(1.0))
    assert snapshot.heart_rate_is_stale is False
    assert snapshot.live_bpm == Bpm(118)


def test_a_reading_older_than_the_display_limit_must_be_greyed_out() -> None:
    """The exact boundary, because "a few seconds" has to mean one number.

    At the limit the reading still counts; past it, the UI is required to stop
    presenting it as live. A stale number that looks live is what makes an
    operator decide everything is fine about a machine nobody is watching.
    """
    at_limit = _snapshot(heart_rate=_sample(), heart_rate_age=HEART_RATE_STALE_AFTER)
    just_past = _snapshot(
        heart_rate=_sample(), heart_rate_age=Seconds(HEART_RATE_STALE_AFTER + 0.001)
    )

    assert at_limit.heart_rate_is_stale is False
    assert at_limit.live_bpm is not None
    assert just_past.heart_rate_is_stale is True
    assert just_past.live_bpm is None


def test_a_stale_reading_is_withheld_even_though_its_grade_was_good() -> None:
    """Freshness and quality are independent gates, and both are applied.

    The pipeline re-emits its last metrics dict on failure, so an eight-minute-
    old number can carry ``quality=good``. Age is what catches it.
    """
    snapshot = _snapshot(
        heart_rate=_sample(bpm=Bpm(96), quality=SignalQuality.GOOD),
        heart_rate_age=Seconds(480.0),
    )
    assert snapshot.live_bpm is None


def test_a_fresh_reading_with_an_untrustworthy_grade_is_still_withheld() -> None:
    """Hum arrives on time. Being recent does not make it a heartbeat."""
    snapshot = _snapshot(
        heart_rate=_sample(bpm=Bpm(50), quality=SignalQuality.MAINS_DOMINATED),
        heart_rate_age=Seconds(0.5),
    )
    assert snapshot.heart_rate_is_stale is False
    assert snapshot.live_bpm is None


def test_a_drive_observation_that_never_happened_counts_as_stale() -> None:
    """And the state it is paired with must be COMM_LOST, not NOT_READY.

    COMM_LOST says "the drive's state is unknown", which is not "the motor is
    stopped"; NOT_READY reads as stopped to anything that does not know better.
    """
    snapshot = _snapshot(drive_status_age=None, drive_state=DriveState.COMM_LOST)
    assert snapshot.drive_status_is_stale is True
    assert snapshot.drive_state is DriveState.COMM_LOST


def test_a_drive_observation_goes_stale_at_its_own_limit() -> None:
    """Ten ticks of the 5 Hz loop. Past that, the displayed state is a memory."""
    at_limit = _snapshot(drive_status_age=DRIVE_STATUS_STALE_AFTER)
    just_past = _snapshot(drive_status_age=Seconds(DRIVE_STATUS_STALE_AFTER + 0.001))
    assert at_limit.drive_status_is_stale is False
    assert just_past.drive_status_is_stale is True


def test_the_two_staleness_limits_are_a_few_seconds_not_a_few_minutes() -> None:
    """Pinned: these are the numbers the display rule and the control law share.

    The heart-rate limit allows for an 8 s median refreshed at 1 Hz; the drive
    limit allows for ten ticks of the control loop. Either one measured in
    minutes would make the ages decorative.
    """
    assert HEART_RATE_STALE_AFTER == 4.0
    assert DRIVE_STATUS_STALE_AFTER == 2.0
    assert 0.0 < HEART_RATE_STALE_AFTER <= 10.0
    assert 0.0 < DRIVE_STATUS_STALE_AFTER <= 10.0


def test_no_verdict_reads_as_no_demand() -> None:
    """One place decides what an absent verdict means, so no consumer guesses."""
    assert _snapshot(safety=None).safety_action is SafetyAction.NONE


def test_a_standing_verdict_is_reported_as_its_own_action() -> None:
    verdict = _verdict(action=SafetyAction.GO_SILENT, rule="drive_comm_lost", latched=True)
    snapshot = _snapshot(safety=verdict)
    assert snapshot.safety_action is SafetyAction.GO_SILENT
    assert snapshot.safety is not None
    assert snapshot.safety.latched is True


def test_a_snapshot_carries_the_raw_fault_code_an_operator_can_read_off_the_drive() -> None:
    """The mnemonic on the drive's own display is worth more than our prose."""
    snapshot = _snapshot(fault=describe_fault(RawRegister(22)), drive_state=DriveState.FAULT)
    assert snapshot.fault is not None
    assert snapshot.fault.fault is DriveFault.UNDERVOLTAGE
    assert snapshot.fault.raw_code == RawRegister(22)
    assert "USF" in snapshot.fault.message


def test_a_snapshot_says_whether_the_commanded_speed_was_confirmed() -> None:
    """A Modbus write to the wrong address is acked while the reference never moves."""
    assert _snapshot().setpoint_confirmed is True


# =========================================================================
# The contract: field names, annotations, immutability, slots
# =========================================================================

#: Every record in the vocabulary, with its fields in order and the annotation
#: each one carries. Three other modules construct these by keyword, so a
#: rename or a retype is a breaking change to their code; pinning it here means
#: making one is a deliberate edit with a visible diff, rather than a surprise
#: for whoever built against the old shape.
EXPECTED_FIELDS: Final[tuple[tuple[type, tuple[tuple[str, str], ...]], ...]] = (
    (
        SafetyVerdict,
        (
            ("action", "SafetyAction"),
            ("rule", "str"),
            ("detail", "str"),
            ("latched", "bool"),
            ("since", "Monotonic"),
        ),
    ),
    (
        HeartRateSample,
        (
            ("bpm", "Bpm | None"),
            ("quality", "SignalQuality"),
            ("seq", "int"),
            ("at", "Monotonic"),
        ),
    ),
    (
        ControlDecision,
        (
            ("desired_rpm", "MotorRpm"),
            ("phase", "Phase"),
            ("error_bpm", "float"),
            ("in_deadband", "bool"),
            ("target_bpm", "Bpm | None"),
            ("reason", "str"),
        ),
    ),
    (
        SpeedView,
        (
            ("motor_rpm", "MotorRpm"),
            ("output_rpm", "OutputRpm"),
            ("hertz", "Hertz"),
            ("g_load", "GLoad"),
            ("resultant_g", "ResultantG"),
        ),
    ),
    (
        ZoneCounters,
        (
            ("in_zone", "Seconds"),
            ("above_zone", "Seconds"),
            ("below_zone", "Seconds"),
        ),
    ),
    (
        TelemetrySnapshot,
        (
            ("at", "Monotonic"),
            ("wall_clock", "UnixMillis"),
            ("phase", "Phase"),
            ("elapsed", "Seconds"),
            ("remaining", "Seconds"),
            ("heart_rate", "HeartRateSample | None"),
            ("heart_rate_age", "Seconds | None"),
            ("target_bpm", "Bpm | None"),
            ("setpoint", "SpeedView"),
            ("measured", "SpeedView"),
            ("setpoint_confirmed", "bool"),
            ("drive_state", "DriveState"),
            ("drive_status_age", "Seconds | None"),
            ("current", "Amperes | None"),
            ("fault", "FaultReport | None"),
            ("safety", "SafetyVerdict | None"),
            ("counters", "ZoneCounters"),
            ("mode", "RunMode"),
            ("manual", "ManualView | None"),
        ),
    ),
)


@pytest.mark.parametrize(("record", "expected"), EXPECTED_FIELDS, ids=str)
def test_the_published_shape_of_each_record_is_pinned(
    record: type, expected: tuple[tuple[str, str], ...]
) -> None:
    """Names, order and annotations: this is what the other three modules import.

    Annotations are compared as the source text they were written as, which is
    exactly what a consumer reads. It catches the changes that matter most and
    are otherwise silent at this layer - an optional that stopped being
    optional, or a ``MotorRpm`` that became an ``OutputRpm``.
    """
    actual = tuple((field.name, str(field.type)) for field in fields(record))
    assert actual == expected


#: One instance of every record, paired with a field name to try to overwrite.
IMMUTABLE_RECORDS: Final[tuple[tuple[object, str], ...]] = (
    (_verdict(), "action"),
    (_sample(), "bpm"),
    (_decision(), "desired_rpm"),
    (_view(900), "motor_rpm"),
    (
        ZoneCounters(in_zone=Seconds(1.0), above_zone=Seconds(2.0), below_zone=Seconds(4.0)),
        "in_zone",
    ),
    (_snapshot(), "phase"),
)


@pytest.mark.parametrize(("record", "field_name"), IMMUTABLE_RECORDS, ids=str)
def test_every_record_is_immutable(record: object, field_name: str) -> None:
    """A decision taken from an observation must not be able to change it.

    A declared field is what is probed here, so the exception is CPython's
    ``FrozenInstanceError`` rather than the ``TypeError`` a slotted frozen
    dataclass raises for a name it does not know (see the slots test below,
    which covers that path deliberately).
    """
    with pytest.raises(FrozenInstanceError):
        _assign(record, field_name, None)


@pytest.mark.parametrize(("record", "field_name"), IMMUTABLE_RECORDS, ids=str)
def test_every_record_is_slotted(record: object, field_name: str) -> None:
    """No instance dict: these are built per tick, and a typo cannot add a field.

    Two exception types are accepted because CPython's generated
    ``__setattr__`` for a frozen **slotted** dataclass picks between them: a
    name that is a field raises ``FrozenInstanceError``, while a name that is
    not falls through to a ``super()`` call that raises ``TypeError``. The
    contract is that neither assignment succeeds.
    """
    assert field_name  # the parametrisation is shared with the test above
    assert not hasattr(record, "__dict__")
    with pytest.raises((AttributeError, TypeError, FrozenInstanceError)):
        _assign(record, "invented_field", 1)
    assert not hasattr(record, "invented_field")


# =========================================================================
# The module's own shape: vocabulary only
# =========================================================================


def _imported_modules(source: Path) -> frozenset[str]:
    """Every module name imported by ``source``, as written."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return frozenset(names)


def test_the_vocabulary_depends_on_nothing_in_the_training_package() -> None:
    """The dependency direction that keeps policy out of the vocabulary.

    The plan, the control law and the safety supervisor all import this module;
    it imports none of them. ``src.motor.drive`` is allowed because it is the
    pure drive seam (no ``pymodbus``, no ``serial``) and telemetry has to name
    the drive's own state and fault vocabulary rather than inventing a second
    set of names that could disagree.
    """
    imported = _imported_modules(TYPES_SOURCE)
    project_imports = {name for name in imported if name.startswith("src.")}
    assert project_imports == {"src.units", "src.motor.drive"}


def test_the_vocabulary_never_reads_a_clock() -> None:
    """Every instant and duration arrives as a parameter (contract rule 4).

    A module that reads the clock cannot be tested at 60x, and the closed-loop
    simulation is the only evidence the control law is safe before a person
    sits in the machine. There is a repo-wide grep test for direct
    ``time.monotonic()`` calls; this one additionally forbids the import, so
    the temptation is not even in scope.
    """
    assert "time" not in _imported_modules(TYPES_SOURCE)

"""Tests for the training programme, its timeline, and the profile store.

What these are evidence for, in the order the risk sits:

1. **The subject ceiling actually refuses an unsafe zone.** A 145-160 bpm zone
   is fine at 30 and above maximum at 65, and the difference is one number in
   one field. There is a test named after exactly that, and a table that pins
   every other refusal - plus a completeness check asserting no member of
   :class:`Violation` is left untested.
2. **The phase timeline is half-open and exact at its boundaries.** Tested at
   ``0``, at each boundary minus an epsilon, at each boundary, at the total, and
   past it; plus a property test that the five spans tile ``[0, total)`` with no
   instant in two phases and none in none.
3. **The override puts the delta in HOLD.** Asserted by comparing the WARMUP
   and COOLDOWN spans before and after a 30 -> 45 minute change: a clinically
   fixed 5-minute warmup must not become 7.5 minutes.
4. **An interrupted write leaves the old file whole.** The "power cut" test
   captures the temporary file's contents at the instant of the rename and
   checks it was already complete, then checks the original bytes, the
   in-memory revision, and that no ``.tmp`` was left behind. A second test
   proves a leftover ``.tmp`` is never read.
5. **A corrupt store does not brick startup**, is renamed out of the way under
   a content-addressed name, falls back to the shipped defaults, and says so at
   ``ERROR`` level. Also: what happens when the rename itself fails, and when
   the defaults are the broken thing.
6. **Two editors cannot silently overwrite each other** - the revision
   conflict - and a failed write does not advance the revision.

The channel names are cross-checked against ``src/bitalino_client.py`` with
``ast``, because that name selects which analog column a sample is read from: a
silent rename there would not produce missing data, it would feed muscle
activity to the control law.
"""

from __future__ import annotations

import ast
import itertools
import json
import logging
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from typing import Final, assert_never, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.result import Err, Ok, Result
from src.training.plan import (
    COMMISSIONED_DECEL_S,
    INITIAL_REV,
    MAX_SUBJECT_AGE_YEARS,
    MIN_RECOVERY_S,
    MIN_SUBJECT_AGE_YEARS,
    NAMEPLATE_MOTOR_RPM,
    PROFILE_ID_PATTERN,
    SCHEMA_VERSION,
    SHIPPED_DEFAULTS_PATH,
    SUBJECT_HR_MAX_MAX,
    SUBJECT_HR_MAX_MIN,
    ZONE_CEILING_FRACTION,
    Channel,
    DefaultsUnusable,
    DeleteError,
    LoadReport,
    LoadSource,
    Malformed,
    PhaseSpan,
    ProfileParseError,
    ProfileRefusedError,
    ProfileStore,
    Program,
    Rejected,
    ResolveError,
    RevMismatch,
    StoreContent,
    StoreRev,
    StoreUnwritable,
    TrainingProfile,
    UnknownProfile,
    UpsertError,
    Violation,
    hr_max_from_age,
    is_profile_id,
    parse_profile,
    parse_store,
    profile_to_document,
)
from src.training.types import Phase
from src.units import Bpm, MotorRpm, Seconds, UnixMillis

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
PLAN_SOURCE: Final[Path] = PROJECT_ROOT / "src" / "training" / "plan.py"
BITALINO_SOURCE: Final[Path] = PROJECT_ROOT / "src" / "bitalino_client.py"

#: The name of the sensor-name -> analog-column table in the acquisition layer.
CHANNEL_MAP_NAME: Final[str] = "CHANNEL_MAP"

#: Small enough to be inside float resolution at 1800 s (about 2e-13) by nine
#: orders of magnitude, so "just below the boundary" is a real instant and not
#: a rounding artefact.
EPS: Final[float] = 1e-6

LOGGER_NAME: Final[str] = "src.training.plan"


# =========================================================================
# Helpers
# =========================================================================


def _ok[T, E](result: Result[T, E]) -> T:
    """The success value, failing the test with the error if there is not one."""
    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    return result.value


def _err[T, E](result: Result[T, E]) -> E:
    """The error value, failing the test with the success if there is not one."""
    assert isinstance(result, Err), f"expected Err, got {result!r}"
    return result.error


def _refusal(build: Callable[[], TrainingProfile]) -> tuple[Violation, ...]:
    """The violations a refused construction reports.

    Goes through the constructor rather than calling the checks directly, so it
    is evidence that ``__post_init__`` is wired up and that no invalid profile
    escapes - which is the guarantee every other module leans on.
    """
    with pytest.raises(ProfileRefusedError) as raised:
        build()
    return raised.value.violations


def _assign(target: object, name: str, value: object) -> None:
    """An attribute assignment the type checker knows is illegal.

    Lets the immutability tests attempt the mutation at runtime without a
    suppression comment. The static half of the guarantee is that
    ``profile.max_rpm = ...`` does not type-check; this is the runtime half,
    for code that reached the object some other way.
    """
    setattr(target, name, value)


#: The shipped 30-minute profile, written out rather than loaded from
#: config/profiles.default.json. Duplicated deliberately: a test that read its
#: fixture from the file under test could not notice the file changing.
BASE: Final[TrainingProfile] = TrainingProfile(
    profile_id="standard_30_min",
    name="30 min",
    total_duration_s=Seconds(1800.0),
    baseline_s=Seconds(180.0),
    warmup_max_s=Seconds(300.0),
    hold_min_s=Seconds(300.0),
    cooldown_s=Seconds(240.0),
    recovery_s=Seconds(300.0),
    zone_low_bpm=Bpm(118),
    zone_high_bpm=Bpm(138),
    hard_max_bpm=Bpm(148),
    critical_bpm=Bpm(158),
    subject_hr_max=Bpm(162),
    min_run_rpm=MotorRpm(55),
    max_rpm=MotorRpm(276),
    warmup_rpm_ceiling_fraction=0.6,
    channels=(Channel.ECG,),
)

#: Hand-derived from BASE, not recomputed with the code under test:
#: 180 | +300 = 480 | +780 = 1260 | +240 = 1500 | +300 = 1800.
BASE_FIXED_S: Final[float] = 1020.0
BASE_HOLD_S: Final[float] = 780.0
BASE_BOUNDARIES: Final[tuple[float, ...]] = (180.0, 480.0, 1260.0, 1500.0, 1800.0)


def _store(tmp_path: Path, *, defaults: Path = SHIPPED_DEFAULTS_PATH) -> ProfileStore:
    return ProfileStore(tmp_path / "profiles.json", defaults)


def _loaded_store(tmp_path: Path) -> ProfileStore:
    """A store holding the shipped defaults, with no file on disk yet."""
    store = _store(tmp_path)
    report = _ok(store.load())
    assert report.source is LoadSource.DEFAULTS_NO_STORE
    return store


def _written_store(tmp_path: Path) -> ProfileStore:
    """A store whose contents have actually been written to disk once."""
    store = _loaded_store(tmp_path)
    assert _ok(store.upsert(BASE, expected_rev=store.rev)) == StoreRev(1)
    return store


def _store_file_document(path: Path) -> Mapping[str, object]:
    parsed: object = json.loads(path.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
    assert isinstance(parsed, dict)
    # JSON object keys are strings by construction, so this states the format
    # rather than assuming anything about the data.
    return cast("Mapping[str, object]", parsed)


def _valid_store_text(*profiles: TrainingProfile, rev: int = 3) -> str:
    return json.dumps(
        {
            "version": SCHEMA_VERSION,
            "rev": rev,
            "profiles": [profile_to_document(profile) for profile in profiles],
        }
    )


# =========================================================================
# The refusal every other check is decoration without
# =========================================================================


def test_the_same_zone_is_accepted_at_thirty_and_refused_at_sixty_five() -> None:
    """A 145-160 bpm zone: unremarkable at 30, above maximum at 65.

    The single scenario this module exists to stop. Both profiles are
    identical apart from ``subject_hr_max``, and the two ceilings are the
    Tanaka estimates for the two ages, computed by :func:`hr_max_from_age`
    rather than typed in - so this test also pins that the estimate and the
    ceiling check agree with each other.
    """
    at_thirty = _ok(hr_max_from_age(30))
    at_sixty_five = _ok(hr_max_from_age(65))
    assert (at_thirty, at_sixty_five) == (Bpm(187), Bpm(162))

    def build(subject_hr_max: Bpm) -> TrainingProfile:
        return replace(
            BASE,
            zone_low_bpm=Bpm(145),
            zone_high_bpm=Bpm(160),
            hard_max_bpm=Bpm(170),
            critical_bpm=Bpm(180),
            subject_hr_max=subject_hr_max,
        )

    young = build(at_thirty)
    assert young.zone_high_bpm == Bpm(160)
    assert young.zone_high_bpm <= ZONE_CEILING_FRACTION * at_thirty

    violations = _refusal(lambda: build(at_sixty_five))
    assert Violation.ZONE_ABOVE_SUBJECT_CEILING in violations
    assert Violation.HARD_MAX_ABOVE_SUBJECT_MAX in violations


def test_the_zone_ceiling_bites_exactly_at_the_fraction() -> None:
    """Ninety percent of the subject maximum is allowed; one bpm more is not.

    Pins the comparison as ``<=`` against ``0.9 * subject_hr_max`` at the
    boundary, because an off-by-one here is the difference between a documented
    limit and a limit that is one bpm looser than documented.
    """
    subject = Bpm(200)
    permitted = Bpm(int(ZONE_CEILING_FRACTION * subject))
    assert permitted == Bpm(180)

    at_the_limit = replace(BASE, zone_high_bpm=permitted, subject_hr_max=subject)
    assert at_the_limit.zone_high_bpm == permitted

    violations = _refusal(
        lambda: replace(BASE, zone_high_bpm=Bpm(permitted + 1), subject_hr_max=subject)
    )
    assert violations == (Violation.ZONE_ABOVE_SUBJECT_CEILING,)


# =========================================================================
# Every refusal, and nothing left untested
# =========================================================================

#: One entry per :class:`Violation`, each a minimal mutation of BASE. Explicit
#: lambdas rather than a name->value table, so every mutation is type-checked
#: against the field it changes: a ``Bpm`` handed to an rpm field is caught here
#: by the checkers rather than by a test that passes for the wrong reason.
REFUSALS: Final[tuple[tuple[Violation, Callable[[], TrainingProfile]], ...]] = (
    (
        Violation.BAD_PROFILE_ID,
        lambda: replace(BASE, profile_id="Standard_30_Min"),
    ),
    (
        Violation.EMPTY_NAME,
        lambda: replace(BASE, name="   "),
    ),
    (
        Violation.NON_POSITIVE_DURATION,
        lambda: replace(BASE, baseline_s=Seconds(0.0)),
    ),
    (
        Violation.COOLDOWN_FASTER_THAN_DRIVE_RAMP,
        lambda: replace(BASE, cooldown_s=Seconds(2.0)),
    ),
    (
        Violation.RECOVERY_TOO_SHORT,
        lambda: replace(BASE, recovery_s=Seconds(30.0)),
    ),
    (
        Violation.HOLD_TOO_SHORT,
        lambda: replace(BASE, total_duration_s=Seconds(BASE_FIXED_S + 299.0)),
    ),
    (
        Violation.BPM_NOT_POSITIVE,
        lambda: replace(BASE, zone_low_bpm=Bpm(0)),
    ),
    (
        Violation.SUBJECT_HR_MAX_IMPLAUSIBLE,
        lambda: replace(BASE, subject_hr_max=Bpm(1620)),
    ),
    (
        Violation.ZONE_NOT_ASCENDING,
        lambda: replace(BASE, zone_low_bpm=Bpm(138)),
    ),
    (
        Violation.ZONE_ABOVE_SUBJECT_CEILING,
        lambda: replace(BASE, zone_high_bpm=Bpm(150)),
    ),
    (
        Violation.HARD_MAX_ABOVE_SUBJECT_MAX,
        lambda: replace(BASE, hard_max_bpm=Bpm(163), critical_bpm=Bpm(170)),
    ),
    (
        Violation.CRITICAL_NOT_ABOVE_HARD_MAX,
        lambda: replace(BASE, critical_bpm=Bpm(148)),
    ),
    (
        Violation.RPM_NOT_POSITIVE,
        lambda: replace(BASE, min_run_rpm=MotorRpm(0)),
    ),
    (
        Violation.MIN_RUN_ABOVE_MAX,
        lambda: replace(BASE, min_run_rpm=MotorRpm(300)),
    ),
    (
        Violation.RPM_ABOVE_NAMEPLATE,
        lambda: replace(BASE, max_rpm=MotorRpm(NAMEPLATE_MOTOR_RPM + 1)),
    ),
    (
        Violation.WARMUP_FRACTION_OUT_OF_RANGE,
        lambda: replace(BASE, warmup_rpm_ceiling_fraction=1.5),
    ),
    (
        Violation.WARMUP_CEILING_BELOW_MIN_RUN,
        lambda: replace(BASE, min_run_rpm=MotorRpm(200)),
    ),
    (
        Violation.ECG_CHANNEL_MISSING,
        lambda: replace(BASE, channels=(Channel.EDA,)),
    ),
    (
        Violation.DUPLICATE_CHANNEL,
        lambda: replace(BASE, channels=(Channel.ECG, Channel.ECG)),
    ),
)


# The id is the violation alone: ``str`` of a lambda carries its memory address,
# which differs in every process, and the gate, when it spreads the suite over
# several processes, refuses to pass unless they all collect identical ids.
@pytest.mark.parametrize(
    ("violation", "build"), REFUSALS, ids=[str(violation) for violation, _ in REFUSALS]
)
def test_each_violation_is_refused_at_construction(
    violation: Violation, build: Callable[[], TrainingProfile]
) -> None:
    """Each refusal reason is reachable and is actually reported.

    ``in`` rather than equality because some mutations legitimately trip more
    than one check - a cooldown of zero is both non-positive and faster than
    the drive's ramp - and pinning the exact set here would make the table a
    test of the checks' interactions rather than of each check.
    """
    assert violation in _refusal(build)


def test_every_violation_is_covered_by_the_refusal_table() -> None:
    """No refusal reason may exist without a test that triggers it.

    Without this, adding a member to :class:`Violation` silently adds an
    untested limit - and the limits in this enum are the ones standing between
    a hand-edited JSON file and a person in a centrifuge.
    """
    tested = {violation for violation, _ in REFUSALS}
    assert tested == set(Violation)


def test_the_valid_base_profile_trips_no_check() -> None:
    """The fixture every refusal test mutates is itself clean.

    Load-bearing: if BASE tripped a check, every ``in`` assertion above could
    pass on the wrong violation. ``replace`` with no changes re-runs the whole
    constructor, so this is the checks passing and not just an object existing.
    """
    assert replace(BASE) == BASE


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: replace(BASE, total_duration_s=Seconds(0.0)), id="total"),
        pytest.param(lambda: replace(BASE, baseline_s=Seconds(0.0)), id="baseline"),
        pytest.param(lambda: replace(BASE, warmup_max_s=Seconds(0.0)), id="warmup_max"),
        pytest.param(lambda: replace(BASE, hold_min_s=Seconds(0.0)), id="hold_min"),
        pytest.param(lambda: replace(BASE, cooldown_s=Seconds(-1.0)), id="cooldown_negative"),
        pytest.param(lambda: replace(BASE, recovery_s=Seconds(-1.0)), id="recovery_negative"),
        pytest.param(lambda: replace(BASE, baseline_s=Seconds(math.nan)), id="baseline_nan"),
        pytest.param(lambda: replace(BASE, baseline_s=Seconds(math.inf)), id="baseline_inf"),
        pytest.param(lambda: replace(BASE, total_duration_s=Seconds(math.nan)), id="total_nan"),
    ],
)
def test_every_duration_field_must_be_positive_and_finite(
    build: Callable[[], TrainingProfile],
) -> None:
    """Each duration is checked, and NaN is refused rather than accepted.

    NaN is the case worth naming: every comparison against it is ``False``, so
    a plain ``value <= 0.0`` guard accepts it. A NaN duration reaches the phase
    timeline, makes every boundary comparison false, and lands on the operator's
    screen as the remaining time.
    """
    assert Violation.NON_POSITIVE_DURATION in _refusal(build)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: replace(BASE, zone_low_bpm=Bpm(0)), id="zone_low"),
        pytest.param(
            lambda: replace(BASE, zone_low_bpm=Bpm(-5), zone_high_bpm=Bpm(0)), id="zone_high"
        ),
        pytest.param(
            lambda: replace(BASE, hard_max_bpm=Bpm(0), zone_low_bpm=Bpm(-2), zone_high_bpm=Bpm(-1)),
            id="hard_max",
        ),
        pytest.param(lambda: replace(BASE, critical_bpm=Bpm(-1)), id="critical"),
    ],
)
def test_every_heart_rate_threshold_must_be_positive(
    build: Callable[[], TrainingProfile],
) -> None:
    """A zero or negative threshold is a missing key read as a default."""
    assert Violation.BPM_NOT_POSITIVE in _refusal(build)


@pytest.mark.parametrize("rpm", [0, -1])
def test_neither_speed_bound_may_be_zero_or_reverse(rpm: int) -> None:
    """No reverse rotation, and no zero ceiling.

    A negative ceiling would defeat every clamp written against it, and nothing
    about the rig asks to turn backwards.
    """
    assert Violation.RPM_NOT_POSITIVE in _refusal(
        lambda: replace(BASE, max_rpm=MotorRpm(rpm), min_run_rpm=MotorRpm(rpm))
    )


@pytest.mark.parametrize(
    "fraction", [0.0, -0.1, 1.0001, 1.5, math.nan, math.inf, -math.inf], ids=str
)
def test_the_warmup_fraction_must_be_a_ratio(fraction: float) -> None:
    """``(0, 1]``, and a non-finite fraction is refused rather than crashed.

    The non-finite cases are why :func:`_check_warmup_ceiling_turns` guards its
    inputs: ``math.floor(276 * inf)`` raises ``OverflowError``, and a refusal
    must never come back as a traceback from inside a validator.
    """
    violations = _refusal(lambda: replace(BASE, warmup_rpm_ceiling_fraction=fraction))
    assert Violation.WARMUP_FRACTION_OUT_OF_RANGE in violations


def test_a_fraction_of_exactly_one_is_allowed() -> None:
    """The closed end of the interval: the warmup may reach the full ceiling."""
    profile = replace(BASE, warmup_rpm_ceiling_fraction=1.0)
    assert profile.warmup_rpm_ceiling == profile.max_rpm


def test_a_refused_speed_bound_does_not_also_report_the_derived_ceiling() -> None:
    """A check declines when the fields it derives from are themselves refused.

    ``floor(-1 * 0.6)`` is ``-1``, which is below ``min_run_rpm`` - so without
    the guard a single bad ``max_rpm`` would report a second, confusing
    violation about the warmup ceiling and send the operator to the wrong field.
    """
    violations = _refusal(lambda: replace(BASE, max_rpm=MotorRpm(-1)))
    assert Violation.RPM_NOT_POSITIVE in violations
    assert Violation.WARMUP_CEILING_BELOW_MIN_RUN not in violations


def test_going_above_the_nameplate_needs_the_explicit_flag() -> None:
    """1380 rpm is the nameplate; above it is a recorded, deliberate act."""
    assert replace(BASE, max_rpm=NAMEPLATE_MOTOR_RPM).max_rpm == NAMEPLATE_MOTOR_RPM
    above = MotorRpm(NAMEPLATE_MOTOR_RPM + 1)
    assert Violation.RPM_ABOVE_NAMEPLATE in _refusal(lambda: replace(BASE, max_rpm=above))
    permitted = replace(BASE, max_rpm=above, allow_above_nameplate=True)
    assert permitted.max_rpm == above


def test_a_cooldown_faster_than_the_measured_ramp_is_refused() -> None:
    """Software must not promise a stop faster than the one that was measured.

    The DC bus absorbs about 2.5% of the rotating energy, so a faster commanded
    stop trips overvoltage into FREEWHEEL: a longer, uncontrolled coast-down
    with somebody inside. The boundary is inclusive - the commissioned ramp
    itself is allowed.
    """
    assert replace(BASE, cooldown_s=COMMISSIONED_DECEL_S).cooldown_s == COMMISSIONED_DECEL_S
    just_under = Seconds(COMMISSIONED_DECEL_S - 0.5)
    violations = _refusal(lambda: replace(BASE, cooldown_s=just_under))
    assert violations == (Violation.COOLDOWN_FASTER_THAN_DRIVE_RAMP,)


def test_the_recovery_phase_may_not_be_cut_short() -> None:
    """Recovery is the highest vasovagal-risk window, and monitoring continues.

    Blood pressure falls when the centripetal load goes away, so a profile that
    stops watching the moment the shaft stops is refused. The floor itself is
    allowed.
    """
    at_floor = replace(BASE, recovery_s=MIN_RECOVERY_S)
    assert at_floor.recovery_s == MIN_RECOVERY_S
    just_under = Seconds(MIN_RECOVERY_S - 1.0)
    assert Violation.RECOVERY_TOO_SHORT in _refusal(lambda: replace(BASE, recovery_s=just_under))


@pytest.mark.parametrize("subject", [SUBJECT_HR_MAX_MIN - 1, SUBJECT_HR_MAX_MAX + 1, 16, 1620])
def test_an_implausible_subject_ceiling_is_refused(subject: int) -> None:
    """The number every cardiac check is a fraction of is bounded itself.

    ``16`` (a dropped digit) is safe by accident - it refuses everything -
    but ``1620`` would switch the zone-ceiling check off entirely, which is the
    direction that hurts. A bound on the input is what keeps the derived checks
    meaningful.
    """
    violations = _refusal(lambda: replace(BASE, subject_hr_max=Bpm(subject)))
    assert Violation.SUBJECT_HR_MAX_IMPLAUSIBLE in violations


@pytest.mark.parametrize("subject", [SUBJECT_HR_MAX_MIN, SUBJECT_HR_MAX_MAX])
def test_the_subject_ceiling_bounds_are_inclusive(subject: Bpm) -> None:
    """Both ends of the plausible range are accepted."""
    profile = replace(
        BASE,
        subject_hr_max=subject,
        zone_low_bpm=Bpm(80),
        zone_high_bpm=Bpm(85),
        hard_max_bpm=Bpm(90),
        critical_bpm=Bpm(95),
    )
    assert profile.subject_hr_max == subject


@pytest.mark.parametrize(
    ("zone_low", "zone_high"), [(138, 138), (140, 138)], ids=["equal", "inverted"]
)
def test_an_empty_or_inverted_zone_is_refused(zone_low: int, zone_high: int) -> None:
    """A zone the control law cannot aim inside of is not a zone."""
    violations = _refusal(
        lambda: replace(BASE, zone_low_bpm=Bpm(zone_low), zone_high_bpm=Bpm(zone_high))
    )
    assert Violation.ZONE_NOT_ASCENDING in violations


def test_the_two_safety_tiers_may_not_invert() -> None:
    """``critical_bpm`` must be strictly above ``hard_max_bpm``.

    Equal is refused too: with the tiers coincident the emergency response
    fires at the same instant as the ordinary one, so the ordinary one may as
    well not exist.
    """
    assert Violation.CRITICAL_NOT_ABOVE_HARD_MAX in _refusal(
        lambda: replace(BASE, critical_bpm=BASE.hard_max_bpm)
    )
    assert replace(BASE, critical_bpm=Bpm(BASE.hard_max_bpm + 1)).critical_bpm == Bpm(149)


@pytest.mark.parametrize(
    "channels",
    [pytest.param((), id="empty"), pytest.param((Channel.EDA, Channel.LUX), id="other_sensors")],
)
def test_a_profile_with_no_ecg_channel_is_refused(channels: tuple[Channel, ...]) -> None:
    """No ECG is no heart rate, and no heart rate is nothing to regulate on.

    The empty list included: "records nothing" and "records the wrong things"
    are the same failure from the control law's point of view.
    """
    assert Violation.ECG_CHANNEL_MISSING in _refusal(lambda: replace(BASE, channels=channels))


def test_all_violations_are_reported_at_once_in_a_stable_order() -> None:
    """Every problem in one pass, and the same order every time.

    Accumulating rather than stopping at the first refusal is what stops a
    hand-edited file turning into twenty save-and-retry cycles; the fixed order
    is so the message reads the same way twice while somebody works through it.
    """
    violations = _refusal(
        lambda: replace(
            BASE,
            profile_id="BAD ID",
            name="",
            critical_bpm=Bpm(100),
            min_run_rpm=MotorRpm(400),
        )
    )
    assert violations == (
        Violation.BAD_PROFILE_ID,
        Violation.EMPTY_NAME,
        Violation.CRITICAL_NOT_ABOVE_HARD_MAX,
        Violation.MIN_RUN_ABOVE_MAX,
        Violation.WARMUP_CEILING_BELOW_MIN_RUN,
    )


def test_the_refusal_message_names_every_violation() -> None:
    """The exception's text is what ends up in a log line, so it must be usable."""
    with pytest.raises(ProfileRefusedError) as raised:
        replace(BASE, name="")
    assert Violation.EMPTY_NAME.value in str(raised.value)


# =========================================================================
# Profile ids are stored-data keys
# =========================================================================


@pytest.mark.parametrize(
    "value",
    ["a", "standard_30_min", "p0", "a" + "b" * 63],
    ids=["single", "typical", "digits", "sixty_four"],
)
def test_well_formed_profile_ids_are_accepted(value: str) -> None:
    assert is_profile_id(value)
    assert replace(BASE, profile_id=value).profile_id == value


@pytest.mark.parametrize(
    "value",
    ["", "Standard", "30_min", "_leading", "has space", "has-dash", "a" + "b" * 64, "ok\n"],
    ids=[
        "empty",
        "upper",
        "leading_digit",
        "leading_underscore",
        "space",
        "dash",
        "too_long",
        "trailing_newline",
    ],
)
def test_malformed_profile_ids_are_rejected(value: str) -> None:
    """Including a trailing newline, which is the trap the anchors exist for.

    ``$`` matches *before* a trailing newline, so a pasted ``"standard_30_min\\n"``
    would pass a ``^...$`` pattern and then key a stored session under an id
    whose end nobody can see. ``\\A``/``\\Z`` plus ``fullmatch`` both close it;
    the pattern is public, so both are kept.
    """
    assert not is_profile_id(value)
    assert Violation.BAD_PROFILE_ID in _refusal(lambda: replace(BASE, profile_id=value))


def test_the_profile_id_pattern_is_anchored_against_a_trailing_newline() -> None:
    """Directly on the public pattern, for consumers that call ``.match``."""
    assert PROFILE_ID_PATTERN.match("standard_30_min\n") is None


# =========================================================================
# The maximum-heart-rate estimate
# =========================================================================


@pytest.mark.parametrize(
    ("age", "expected"),
    [(10, 201), (20, 194), (30, 187), (40, 180), (50, 173), (65, 162), (100, 138)],
)
def test_the_age_estimate_matches_the_published_formula(age: int, expected: int) -> None:
    """``208 - 0.7 x age``, evaluated by hand.

    Written out rather than recomputed with the constants under test, so a
    changed coefficient fails here instead of agreeing with itself. Note 65:
    ``208 - 45.5 = 162.5`` rounds to 162, which is the figure the module
    docstring and the shipped defaults are built on.
    """
    assert _ok(hr_max_from_age(age)) == Bpm(expected)


@pytest.mark.parametrize("age", [MIN_SUBJECT_AGE_YEARS - 1, MAX_SUBJECT_AGE_YEARS + 1, 0, -5, 300])
def test_an_implausible_age_is_refused_rather_than_clamped(age: int) -> None:
    """Clamping an age of 300 to 100 would answer a question nobody asked."""
    error = _err(hr_max_from_age(age))
    assert error.quantity == "subject_age_years"
    assert (error.low, error.high) == (float(MIN_SUBJECT_AGE_YEARS), float(MAX_SUBJECT_AGE_YEARS))


@given(
    younger=st.integers(min_value=MIN_SUBJECT_AGE_YEARS, max_value=MAX_SUBJECT_AGE_YEARS),
    older=st.integers(min_value=MIN_SUBJECT_AGE_YEARS, max_value=MAX_SUBJECT_AGE_YEARS),
)
def test_the_estimate_never_rises_with_age(younger: int, older: int) -> None:
    """Monotonic, and inside the range this module calls plausible.

    The monotonicity is what makes the estimate safe to use as a ceiling: if it
    could rise with age, an older subject could be handed a higher ceiling than
    a younger one from the same formula.
    """
    if younger > older:
        younger, older = older, younger
    estimate_young = _ok(hr_max_from_age(younger))
    estimate_old = _ok(hr_max_from_age(older))
    assert estimate_young >= estimate_old
    assert SUBJECT_HR_MAX_MIN <= estimate_old <= SUBJECT_HR_MAX_MAX


# =========================================================================
# Derived quantities
# =========================================================================


def test_hold_is_the_total_less_every_fixed_phase() -> None:
    """Derived, never stored. Hand-computed: 1800 - (180+300+240+300) = 780."""
    assert BASE.fixed_phases_s == Seconds(BASE_FIXED_S)
    assert BASE.hold_s == Seconds(BASE_HOLD_S)


@pytest.mark.parametrize(("max_rpm", "expected_hz"), [(276, 10.0), (690, 25.0), (1380, 50.0)])
def test_the_hsp_value_an_operator_types_is_derived_from_the_nameplate(
    max_rpm: int, expected_hz: float
) -> None:
    """1380 rpm at 50 Hz, so 276 rpm is 10.0 Hz.

    Hand-derived from the nameplate rather than recomputed with ``src.units``,
    so this cannot agree with a bug in the conversion. It is the number the
    commissioning procedure turns into the drive's own ceiling - the one that
    survives a software bug.
    """
    profile = replace(BASE, max_rpm=MotorRpm(max_rpm))
    assert profile.hsp_hertz == pytest.approx(expected_hz)


def test_the_warmup_ceiling_rounds_down() -> None:
    """``floor``, not ``round``: 276 x 0.6 = 165.6 becomes 165, not 166.

    Rounding a ceiling upwards would permit a speed above the fraction that was
    actually authorised. One rpm, and the wrong direction.
    """
    assert BASE.warmup_rpm_ceiling == MotorRpm(165)


def test_a_warmup_ceiling_below_the_starting_speed_is_refused() -> None:
    """Otherwise the warmup could only ever command zero.

    ``floor(276 x 0.6) = 165``, so a ``min_run_rpm`` of 200 leaves the warmup
    with no speed it is allowed to use, and the session would arrive at HOLD
    having warmed nobody up.
    """
    violations = _refusal(lambda: replace(BASE, min_run_rpm=MotorRpm(200)))
    assert violations == (Violation.WARMUP_CEILING_BELOW_MIN_RUN,)


def test_phase_span_reports_its_own_duration() -> None:
    span = PhaseSpan(Phase.HOLD, Seconds(480.0), Seconds(1260.0))
    assert span.duration == Seconds(780.0)


# =========================================================================
# The nominal timeline
# =========================================================================


def test_the_timeline_names_the_five_timed_phases_in_order() -> None:
    """DONE is deliberately absent: it is not a span but everything after the end."""
    assert tuple(span.phase for span in BASE.timeline) == (
        Phase.BASELINE,
        Phase.WARMUP,
        Phase.HOLD,
        Phase.COOLDOWN,
        Phase.RECOVERY,
    )


def test_the_timeline_boundaries_are_the_hand_computed_ones() -> None:
    """180 | 480 | 1260 | 1500 | 1800, accumulated by hand from BASE."""
    assert tuple(span.end for span in BASE.timeline) == BASE_BOUNDARIES
    assert BASE.timeline[0].start == Seconds(0.0)


def test_the_last_span_ends_exactly_at_the_total() -> None:
    """Exactly, not within an epsilon, and that is deliberate.

    The final boundary is the one a UI counts down to, so it is set from
    ``total_duration_s`` rather than from the accumulated sum of four floats.
    """
    assert BASE.timeline[-1].end == BASE.total_duration_s


#: Durations whose accumulated sum is NOT bit-identical to the total: adding
#: baseline, warmup, the derived hold, cooldown and recovery back up lands
#: 2.3e-13 s above ``total_duration_s``. Found by search, because BASE's own
#: round numbers add up exactly and so cannot show this defect at all.
DRIFTING: Final[TrainingProfile] = replace(
    BASE,
    total_duration_s=Seconds(1312.95),
    baseline_s=Seconds(35.74),
    warmup_max_s=Seconds(457.18),
    hold_min_s=Seconds(42.84),
    cooldown_s=Seconds(26.35),
    recovery_s=Seconds(424.26),
)


def test_the_fixture_for_the_drift_test_really_does_drift() -> None:
    """Otherwise the test below would pass for the wrong reason.

    This is the arithmetic the implementation deliberately does NOT do, spelled
    out here so the next reader can see that the hazard is real rather than
    take the docstring's word for it.
    """
    accumulated = (
        DRIFTING.baseline_s
        + DRIFTING.warmup_max_s
        + DRIFTING.hold_s
        + DRIFTING.cooldown_s
        + DRIFTING.recovery_s
    )
    assert accumulated != DRIFTING.total_duration_s
    assert accumulated > DRIFTING.total_duration_s
    assert accumulated - DRIFTING.total_duration_s < 1e-9


def test_the_final_boundary_is_exact_even_when_the_phases_do_not_add_up() -> None:
    """The boundary a UI counts down to is set from the total, not accumulated.

    With durations that drift, an accumulated final boundary sits a fraction of
    a picosecond *above* ``total_duration_s`` - so ``phase_at(total)`` answers
    RECOVERY with a positive remaining, and the session never reaches DONE at
    the instant its own clock says it is over. A countdown that stops at
    "0.0 remaining" while still claiming to be in RECOVERY is precisely the
    kind of thing nobody notices until an operator is waiting for it.
    """
    assert DRIFTING.timeline[-1].end == DRIFTING.total_duration_s
    phase, phase_elapsed, phase_remaining = DRIFTING.phase_at(DRIFTING.total_duration_s)
    assert phase is Phase.DONE
    assert (phase_elapsed, phase_remaining) == (Seconds(0.0), Seconds(0.0))


@pytest.mark.parametrize(
    ("elapsed", "phase", "phase_elapsed", "phase_remaining"),
    [
        pytest.param(0.0, Phase.BASELINE, 0.0, 180.0, id="start"),
        pytest.param(180.0 - EPS, Phase.BASELINE, 180.0 - EPS, EPS, id="warmup_minus_eps"),
        pytest.param(180.0, Phase.WARMUP, 0.0, 300.0, id="warmup"),
        pytest.param(480.0 - EPS, Phase.WARMUP, 300.0 - EPS, EPS, id="hold_minus_eps"),
        pytest.param(480.0, Phase.HOLD, 0.0, 780.0, id="hold"),
        pytest.param(1260.0 - EPS, Phase.HOLD, 780.0 - EPS, EPS, id="cooldown_minus_eps"),
        pytest.param(1260.0, Phase.COOLDOWN, 0.0, 240.0, id="cooldown"),
        pytest.param(1500.0 - EPS, Phase.COOLDOWN, 240.0 - EPS, EPS, id="recovery_minus_eps"),
        pytest.param(1500.0, Phase.RECOVERY, 0.0, 300.0, id="recovery"),
        pytest.param(1800.0 - EPS, Phase.RECOVERY, 300.0 - EPS, EPS, id="total_minus_eps"),
        pytest.param(1800.0, Phase.DONE, 0.0, 0.0, id="total"),
        pytest.param(1801.0, Phase.DONE, 1.0, 0.0, id="total_plus_one"),
    ],
)
def test_phase_at_every_boundary(
    elapsed: float, phase: Phase, phase_elapsed: float, phase_remaining: float
) -> None:
    """Half-open intervals: a boundary belongs to the phase it opens.

    At ``elapsed == baseline_s`` the answer is WARMUP with zero elapsed, not
    BASELINE with zero remaining - so no instant is in two phases and none is
    in neither.
    """
    found_phase, found_elapsed, found_remaining = BASE.phase_at(Seconds(elapsed))
    assert found_phase is phase
    assert found_elapsed == pytest.approx(phase_elapsed)
    assert found_remaining == pytest.approx(phase_remaining)


@pytest.mark.parametrize("elapsed", [-EPS, -1.0, -10_000.0])
def test_a_negative_elapsed_reads_as_the_start_of_the_session(elapsed: float) -> None:
    """It cannot arise from a monotonic clock, and only one answer is safe.

    Of the available answers, "the session has not started" is the one that
    cannot command motion. DONE would be wrong in the other direction - a
    session that has not begun is not one that is over.
    """
    assert BASE.phase_at(Seconds(elapsed)) == (Phase.BASELINE, Seconds(0.0), Seconds(180.0))


@pytest.mark.parametrize("elapsed", [math.nan, math.inf, -math.inf], ids=str)
def test_a_non_finite_elapsed_reads_as_done_with_no_nan_escaping(elapsed: float) -> None:
    """NaN compares false against every boundary, so it must be caught first.

    A bare fall-through would hand back DONE with a NaN ``phase_elapsed``,
    which is a NaN on the operator's screen. DONE is also the only phase in
    which no setpoint is ever issued again, which is the right response to
    arithmetic that has stopped making sense.
    """
    phase, phase_elapsed, phase_remaining = BASE.phase_at(Seconds(elapsed))
    assert phase is Phase.DONE
    assert (phase_elapsed, phase_remaining) == (Seconds(0.0), Seconds(0.0))
    assert math.isfinite(phase_elapsed)
    assert math.isfinite(phase_remaining)


@given(elapsed=st.floats(min_value=0.0, max_value=1800.0, exclude_max=True))
def test_every_instant_in_the_session_is_in_exactly_one_span(elapsed: float) -> None:
    """The five spans tile ``[0, total)``: no gaps and no overlaps.

    The property the half-open intervals exist for. Checked against the spans
    themselves, so ``phase_at`` and ``timeline`` are proven to agree rather
    than being two independent stories about the same session.
    """
    containing = [span for span in BASE.timeline if span.start <= elapsed < span.end]
    assert len(containing) == 1
    span = containing[0]
    phase, phase_elapsed, phase_remaining = BASE.phase_at(Seconds(elapsed))
    assert phase is span.phase
    assert phase_elapsed == pytest.approx(elapsed - span.start)
    assert phase_remaining == pytest.approx(span.end - elapsed)
    assert phase_elapsed + phase_remaining == pytest.approx(span.duration)


@given(elapsed=st.floats(allow_nan=True, allow_infinity=True))
def test_phase_at_never_returns_a_negative_or_non_finite_duration(elapsed: float) -> None:
    """For any float at all, including the ones that should never arrive.

    This is the guarantee the render path and the runner both rely on: whatever
    arrives, two finite non-negative durations come back, so nothing downstream
    has to defend itself against a NaN countdown.
    """
    phase, phase_elapsed, phase_remaining = BASE.phase_at(Seconds(elapsed))
    assert isinstance(phase, Phase)
    assert math.isfinite(phase_elapsed)
    assert phase_elapsed >= 0.0
    assert math.isfinite(phase_remaining)
    assert phase_remaining >= 0.0


# =========================================================================
# The override rule: HOLD absorbs the delta
# =========================================================================


def test_a_longer_session_lengthens_hold_and_nothing_else() -> None:
    """30 -> 45 minutes: the warmup stays 5 minutes, not 7.5.

    The requirement, asserted on the spans rather than on the fields: BASELINE,
    WARMUP, COOLDOWN and RECOVERY come back byte-identical, and the whole extra
    900 seconds is in HOLD. A proportional stretch would be a clinical decision
    taken by arithmetic.
    """
    longer = _ok(BASE.with_total_duration(Seconds(2700.0)))

    assert longer.warmup_max_s == BASE.warmup_max_s == Seconds(300.0)
    assert longer.baseline_s == BASE.baseline_s
    assert longer.cooldown_s == BASE.cooldown_s
    assert longer.recovery_s == BASE.recovery_s
    assert longer.hold_s == Seconds(BASE_HOLD_S + 900.0)

    before = {span.phase: span for span in BASE.timeline}
    after = {span.phase: span for span in longer.timeline}
    for phase in (Phase.BASELINE, Phase.WARMUP):
        assert after[phase] == before[phase], f"{phase.value} moved"
    assert after[Phase.HOLD].duration == pytest.approx(before[Phase.HOLD].duration + 900.0)
    assert after[Phase.COOLDOWN].duration == pytest.approx(before[Phase.COOLDOWN].duration)
    assert after[Phase.RECOVERY].duration == pytest.approx(before[Phase.RECOVERY].duration)


def test_a_shorter_session_shortens_hold_down_to_its_floor() -> None:
    """The floor is inclusive, and a total one second below it is refused."""
    exactly_at_floor = _ok(BASE.with_total_duration(Seconds(BASE_FIXED_S + 300.0)))
    assert exactly_at_floor.hold_s == exactly_at_floor.hold_min_s

    error = _err(BASE.with_total_duration(Seconds(BASE_FIXED_S + 299.0)))
    assert error.violations == (Violation.HOLD_TOO_SHORT,)
    assert Violation.HOLD_TOO_SHORT.value in error.detail


def test_an_override_that_breaks_a_different_limit_is_still_refused() -> None:
    """The override goes through the same construction, so every check re-runs.

    A total of zero is not only too short for a hold, it is a non-positive
    duration - and both come back, which is evidence the override is validated
    by the constructor rather than by a shortcut that only knows about hold.
    """
    error = _err(BASE.with_total_duration(Seconds(0.0)))
    assert set(error.violations) == {
        Violation.NON_POSITIVE_DURATION,
        Violation.HOLD_TOO_SHORT,
    }


def test_the_original_profile_is_untouched_by_an_override() -> None:
    """Frozen records: the override returns a new profile, it does not edit one."""
    _ok(BASE.with_total_duration(Seconds(2700.0)))
    assert BASE.total_duration_s == Seconds(1800.0)
    assert BASE.hold_s == Seconds(BASE_HOLD_S)


# =========================================================================
# The resolved programme
# =========================================================================


def test_resolving_freezes_the_profile_against_a_later_edit(tmp_path: Path) -> None:
    """An operator editing the profile mid-session must not rewrite history.

    The reason :class:`Program` carries a copy rather than an id. Here the
    stored profile has its zone and its total changed *after* the programme was
    resolved, and the programme still describes what was actually commanded.
    """
    store = _written_store(tmp_path)
    program = _ok(store.resolve(BASE.profile_id, at=UnixMillis(1_700_000_000_000)))

    edited = replace(
        BASE,
        zone_low_bpm=Bpm(100),
        zone_high_bpm=Bpm(120),
        total_duration_s=Seconds(2700.0),
    )
    assert _ok(store.upsert(edited, expected_rev=store.rev)) == StoreRev(2)
    assert _ok(store.get(BASE.profile_id)).zone_high_bpm == Bpm(120)

    assert program.profile == BASE
    assert program.profile.zone_high_bpm == Bpm(138)
    assert program.total_duration_s == Seconds(1800.0)
    assert program.timeline == BASE.timeline
    assert program.source_rev == StoreRev(1)


def test_a_programme_reports_where_it_came_from(tmp_path: Path) -> None:
    """Provenance: which profile, and which revision of the store.

    ``source_profile_id`` is a property over the copy rather than a second
    stored field, so the two can never disagree; the revision is the part that
    is not recoverable from the copy, so that one is stored.
    """
    store = _written_store(tmp_path)
    at = UnixMillis(1_700_000_000_123)
    program = _ok(store.resolve(BASE.profile_id, at=at))
    assert program.source_profile_id == BASE.profile_id == program.profile.profile_id
    assert program.source_rev == store.rev
    assert program.resolved_at == at
    assert program.total_overridden is False


def test_a_programme_delegates_the_timeline_to_the_profile_it_froze(tmp_path: Path) -> None:
    """So the runner asks the programme and cannot consult a different profile."""
    store = _written_store(tmp_path)
    program = _ok(store.resolve(BASE.profile_id, at=UnixMillis(1)))
    assert program.phase_at(Seconds(480.0)) == BASE.phase_at(Seconds(480.0))
    assert program.phase_at(Seconds(480.0))[0] is Phase.HOLD


def test_resolving_with_an_override_records_that_it_was_overridden(tmp_path: Path) -> None:
    """ "The 45-minute profile" and "the 30-minute one stretched" are different facts.

    They read identically from the resolved fields alone, which is why the flag
    is stored rather than derived.
    """
    store = _written_store(tmp_path)
    program = _ok(
        store.resolve(BASE.profile_id, at=UnixMillis(1), total_duration_s=Seconds(2700.0))
    )
    assert program.total_overridden is True
    assert program.total_duration_s == Seconds(2700.0)
    assert program.profile.hold_s == Seconds(BASE_HOLD_S + 900.0)
    assert program.profile.warmup_max_s == BASE.warmup_max_s


def test_a_rider_s_own_maximum_refits_the_programme(tmp_path: Path) -> None:
    """A fitter rider's measured maximum replaces the preset's conservative one."""
    store = _written_store(tmp_path)
    program = _ok(store.resolve(BASE.profile_id, at=UnixMillis(1), subject_hr_max=Bpm(185)))
    assert program.profile.subject_hr_max == Bpm(185)
    assert program.profile.zone_high_bpm == BASE.zone_high_bpm
    # The stored preset is untouched: the refit is this session's, not the store's.
    assert _ok(store.get(BASE.profile_id)).subject_hr_max == BASE.subject_hr_max


def test_a_zone_too_high_for_this_rider_is_refused_before_anything_turns(
    tmp_path: Path,
) -> None:
    """A 138 bpm zone top and a 148 bpm hard max are both too high for a 140 bpm maximum."""
    store = _written_store(tmp_path)
    error = _err(store.resolve(BASE.profile_id, at=UnixMillis(1), subject_hr_max=Bpm(140)))
    assert isinstance(error, Rejected)
    assert Violation.ZONE_ABOVE_SUBJECT_CEILING in error.violations
    assert Violation.HARD_MAX_ABOVE_SUBJECT_MAX in error.violations


def test_the_preset_s_own_maximum_is_not_a_refit(tmp_path: Path) -> None:
    store = _written_store(tmp_path)
    program = _ok(
        store.resolve(BASE.profile_id, at=UnixMillis(1), subject_hr_max=BASE.subject_hr_max)
    )
    assert program.profile == BASE


def test_an_override_equal_to_the_stored_total_still_counts_as_one(tmp_path: Path) -> None:
    """The flag records what was asked for, because that is the auditable fact."""
    store = _written_store(tmp_path)
    program = _ok(
        store.resolve(BASE.profile_id, at=UnixMillis(1), total_duration_s=BASE.total_duration_s)
    )
    assert program.total_overridden is True
    assert program.total_duration_s == BASE.total_duration_s


def test_resolving_an_unusable_override_reports_the_violation(tmp_path: Path) -> None:
    store = _written_store(tmp_path)
    error = _err(store.resolve(BASE.profile_id, at=UnixMillis(1), total_duration_s=Seconds(60.0)))
    assert isinstance(error, Rejected)
    assert Violation.HOLD_TOO_SHORT in error.violations


def test_resolving_an_unknown_profile_names_the_ones_that_exist(tmp_path: Path) -> None:
    """ "Unknown profile" alone sends somebody to find a file at 2am."""
    store = _written_store(tmp_path)
    error = _err(store.resolve("standard_30min", at=UnixMillis(1)))
    assert isinstance(error, UnknownProfile)
    assert error.profile_id == "standard_30min"
    assert BASE.profile_id in error.known


# =========================================================================
# The document format
# =========================================================================


def test_the_document_has_exactly_the_fields_of_the_profile() -> None:
    """Adding a field without adding it here is a silently defaulted profile.

    Pinned against ``dataclasses.fields`` rather than a written-out list, so
    this fails the moment the record grows a field the writer does not know
    about - which would otherwise show up as somebody's stored ceiling quietly
    reverting to a default.
    """
    document = profile_to_document(BASE)
    assert set(document) == {field.name for field in fields(TrainingProfile)}


def test_a_profile_survives_a_round_trip_through_json() -> None:
    """Through real ``json.dumps``/``json.loads``, not just the two functions."""
    raw = json.dumps(profile_to_document(BASE))
    parsed: object = json.loads(raw)  # pyright: ignore[reportAny]
    assert isinstance(parsed, dict)
    assert _ok(parse_profile(cast("Mapping[str, object]", parsed))) == BASE


def test_the_age_input_is_never_written_back() -> None:
    """The derived number is what gets stored, not the age it came from.

    If a later version adopted a different formula, a profile that had stored
    only an age would silently re-zone itself - widening somebody's target
    without anyone editing anything.
    """
    document = dict(profile_to_document(BASE))
    del document["subject_hr_max"]
    document["subject_age_years"] = 65
    derived = _ok(parse_profile(document))
    assert derived.subject_hr_max == Bpm(162)
    assert "subject_age_years" not in profile_to_document(derived)
    assert profile_to_document(derived)["subject_hr_max"] == 162


def test_a_stored_ceiling_wins_over_an_age(tmp_path: Path) -> None:
    """Both present: the measured number is the one used.

    A measured maximum from a supervised test beats a population estimate, and
    silently preferring the estimate would discard the better evidence.
    """
    assert tmp_path.exists()
    document = dict(profile_to_document(BASE))
    document["subject_age_years"] = 30
    profile = _ok(parse_profile(document))
    assert profile.subject_hr_max == BASE.subject_hr_max == Bpm(162)


def test_a_profile_with_neither_ceiling_nor_age_is_malformed() -> None:
    document = dict(profile_to_document(BASE))
    del document["subject_hr_max"]
    error = _err(parse_profile(document))
    assert isinstance(error, Malformed)
    assert "subject_hr_max" in error.detail
    assert "subject_age_years" in error.detail


def test_an_implausible_age_in_a_document_is_malformed() -> None:
    document = dict(profile_to_document(BASE))
    del document["subject_hr_max"]
    document["subject_age_years"] = 4
    error = _err(parse_profile(document))
    assert isinstance(error, Malformed)
    assert "subject_age_years" in error.detail


def test_a_well_formed_document_with_an_unsafe_limit_is_rejected_not_malformed() -> None:
    """The two failures are not interchangeable, and the UI must tell them apart.

    "You typed the wrong thing" and "that zone is above this subject's maximum"
    need different responses from the person reading the screen.
    """
    document = dict(profile_to_document(BASE))
    document["zone_high_bpm"] = 160
    error = _err(parse_profile(document))
    assert isinstance(error, Rejected)
    assert error.violations == (Violation.ZONE_ABOVE_SUBJECT_CEILING,)


def test_a_malformed_document_reports_every_problem_at_once() -> None:
    """One save, every problem: a hand-edited file rarely has just one."""
    document = dict(profile_to_document(BASE))
    del document["name"]
    document["baseline_s"] = "180"
    document["max_rpm"] = 276.5
    error = _err(parse_profile(document))
    assert isinstance(error, Malformed)
    assert len(error.problems) == 3
    assert error.detail == "; ".join(error.problems)
    joined = error.detail
    for key in ("name", "baseline_s", "max_rpm"):
        assert key in joined


def test_the_problem_label_says_which_profile_failed() -> None:
    """So a store with four profiles can name the one that is wrong."""
    error = _err(parse_profile({}, where="profiles[2]"))
    assert isinstance(error, Malformed)
    assert all(problem.startswith("profiles[2]: ") for problem in error.problems)


# =========================================================================
# Reading untrusted values
# =========================================================================
#
# Driven through parse_profile rather than against the reader directly, so
# these are tests of the seam an operator's hand-edited file actually arrives
# through - and so the exact message they will read is what gets asserted.


def _profile_document(*, drop: tuple[str, ...] = (), **changes: object) -> dict[str, object]:
    """A valid profile document with keys removed and/or replaced."""
    document: dict[str, object] = {}
    document.update(profile_to_document(BASE))
    for key in drop:
        del document[key]
    document.update(changes)
    return document


def _problems(document: Mapping[str, object]) -> tuple[str, ...]:
    """The problems a malformed document reports, failing if it is not malformed."""
    error = _err(parse_profile(document))
    assert isinstance(error, Malformed), f"expected Malformed, got {error!r}"
    return error.problems


def test_the_document_fixture_is_itself_valid() -> None:
    """Every test below mutates this, so an unmutated one must parse cleanly."""
    assert _ok(parse_profile(_profile_document())) == BASE


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param(
            _profile_document(drop=("name",)),
            "profile: 'name' must be a string, got nothing",
            id="missing",
        ),
        pytest.param(
            _profile_document(name=None),
            "profile: 'name' must be a string, got null",
            id="null",
        ),
        pytest.param(
            _profile_document(name=5),
            "profile: 'name' must be a string, got int",
            id="wrong_type",
        ),
    ],
)
def test_a_missing_key_a_null_and_a_wrong_type_read_differently(
    document: Mapping[str, object], expected: str
) -> None:
    """Three different operator mistakes, so three different messages.

    "Nothing" rather than "null" for an absent key matters: the fix for one is
    to add a line and the fix for the other is to change one, and a message
    that conflates them sends somebody looking for a key that is already there.
    """
    assert _problems(document) == (expected,)


def test_a_whole_number_is_accepted_where_a_real_number_belongs() -> None:
    """A duration of ``180`` and one of ``180.0`` are the same duration."""
    profile = _ok(parse_profile(_profile_document(baseline_s=180)))
    assert profile.baseline_s == Seconds(180.0)


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param(
            _profile_document(baseline_s=True),
            "profile: 'baseline_s' must be a number, got bool",
            id="duration",
        ),
        pytest.param(
            _profile_document(max_rpm=True),
            "profile: 'max_rpm' must be a whole number, got bool",
            id="speed",
        ),
    ],
)
def test_a_boolean_is_refused_where_a_number_belongs(
    document: Mapping[str, object], expected: str
) -> None:
    """``bool`` is a subclass of ``int`` and ``float(True)`` is ``1.0``.

    Without the explicit rejection a stray ``true`` where a duration belongs
    would be read as one second, and one where a speed belongs as 1 rpm - both
    well inside the range every other check would wave through.
    """
    assert _problems(document) == (expected,)


def test_a_fractional_heart_rate_is_refused_rather_than_truncated() -> None:
    """``138.7`` is somebody's arithmetic; reading it as 138 lowers a limit."""
    assert _problems(_profile_document(zone_high_bpm=138.7)) == (
        "profile: 'zone_high_bpm' must be a whole number, got float",
    )


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param(
            _profile_document(drop=("baseline_s",)),
            "profile: 'baseline_s' must be a number, got nothing",
            id="duration",
        ),
        pytest.param(
            _profile_document(drop=("min_run_rpm",)),
            "profile: 'min_run_rpm' must be a whole number, got nothing",
            id="speed",
        ),
        pytest.param(
            _profile_document(baseline_s="180"),
            "profile: 'baseline_s' must be a number, got str",
            id="quoted_number",
        ),
    ],
)
def test_a_missing_or_quoted_number_is_reported(
    document: Mapping[str, object], expected: str
) -> None:
    """A quoted number is the commonest hand-edit mistake, and is not coerced.

    Coercing ``"180"`` would work until the day somebody writes ``"18O"``, and
    the failure would then be a silent default rather than a message.
    """
    assert _problems(document) == (expected,)


def test_an_absent_flag_takes_its_safe_default() -> None:
    """The nameplate override is the only optional key, and its default is safe."""
    profile = _ok(parse_profile(_profile_document(drop=("allow_above_nameplate",))))
    assert profile.allow_above_nameplate is False


def test_a_flag_that_is_set_is_honoured() -> None:
    """And it is what lets an above-nameplate ceiling through, deliberately."""
    above = int(NAMEPLATE_MOTOR_RPM) + 20
    profile = _ok(parse_profile(_profile_document(allow_above_nameplate=True, max_rpm=above)))
    assert profile.allow_above_nameplate is True
    assert profile.max_rpm == MotorRpm(above)


def test_a_flag_that_is_not_a_boolean_is_reported() -> None:
    """``"yes"`` is not consent to exceed the nameplate."""
    assert _problems(_profile_document(allow_above_nameplate="yes")) == (
        "profile: 'allow_above_nameplate' must be true or false, got str",
    )


def test_channel_names_are_parsed_in_the_order_they_are_written() -> None:
    """The order is the order the acquisition layer requests the analog columns."""
    profile = _ok(parse_profile(_profile_document(channels=["ECG", "SpO2"])))
    assert profile.channels == (Channel.ECG, Channel.SPO2)


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param(
            _profile_document(channels=["ecg"]),
            "profile: channels[0] is not a known channel: 'ecg'",
            id="near_miss",
        ),
        pytest.param(
            _profile_document(channels=["ECG", 3]),
            "profile: channels[1] must be a channel name, got int",
            id="not_a_name",
        ),
        pytest.param(
            _profile_document(channels="ECG"),
            "profile: 'channels' must be a list of channel names, got str",
            id="not_a_list",
        ),
        pytest.param(
            _profile_document(drop=("channels",)),
            "profile: 'channels' must be a list of channel names, got nothing",
            id="missing",
        ),
    ],
)
def test_an_unknown_channel_is_never_guessed_at(
    document: Mapping[str, object], expected: str
) -> None:
    """The name selects which analog column a sample is read from.

    So ``"ecg"`` is not quietly read as ``"ECG"``: accepting a near-miss is how
    the control law ends up regulating on somebody's muscle activity. The index
    is in the message because a six-channel list needs it.
    """
    assert _problems(document) == (expected,)


# =========================================================================
# The store: reading
# =========================================================================


def test_the_shipped_defaults_are_two_named_profiles(tmp_path: Path) -> None:
    """ "30 min" and "45 min", loaded through the real parser.

    Also the evidence that ``config/profiles.default.json`` is itself valid: it
    is parsed and every limit in it is checked, so a bad shipped file fails
    here rather than on somebody's Pi.
    """
    store = _loaded_store(tmp_path)
    assert [profile.profile_id for profile in store.list_profiles()] == [
        "standard_30_min",
        "standard_45_min",
    ]
    assert [profile.name for profile in store.list_profiles()] == ["30 min", "45 min"]
    assert store.rev == INITIAL_REV


def test_the_shipped_ceiling_is_deliberately_low_and_matches_a_round_hsp_value(
    tmp_path: Path,
) -> None:
    """The first commissioning step, not a guess at a working speed.

    20% of the nameplate, and an HSP value an operator can actually type into
    the drive - because the procedure is to raise the software ceiling one step
    at a time and re-set HSP to match, HSP being the ceiling that survives a
    software bug.
    """
    for profile in _loaded_store(tmp_path).list_profiles():
        assert profile.allow_above_nameplate is False
        assert profile.max_rpm <= NAMEPLATE_MOTOR_RPM * 0.2
        assert profile.hsp_hertz == pytest.approx(10.0)


def test_the_shipped_zone_is_safe_for_the_oldest_plausible_subject(tmp_path: Path) -> None:
    """The shipped ``subject_hr_max`` is the conservative age-65 figure.

    A shipped default cannot know the subject, so it must be safe for the most
    restricted one an operator might sit in the machine - and an operator must
    then set the real number. The check is the same one the module enforces,
    applied to the file.
    """
    for profile in _loaded_store(tmp_path).list_profiles():
        assert profile.subject_hr_max == _ok(hr_max_from_age(65))
        assert profile.zone_high_bpm <= ZONE_CEILING_FRACTION * profile.subject_hr_max


def test_first_boot_loads_the_defaults_without_creating_a_file(tmp_path: Path) -> None:
    """One less write to an SD card, and nothing to clean up if the unit never runs."""
    store = _store(tmp_path)
    report = _ok(store.load())
    assert report.source is LoadSource.DEFAULTS_NO_STORE
    assert report.quarantined is None
    assert report.rev == INITIAL_REV
    assert report.profile_ids == ("standard_30_min", "standard_45_min")
    assert not (tmp_path / "profiles.json").exists()


def test_profiles_are_listed_in_a_stable_order(tmp_path: Path) -> None:
    """Sorted by id, so the operator's list does not reshuffle on an unrelated save."""
    store = _loaded_store(tmp_path)
    zebra = replace(BASE, profile_id="zzz_last", name="Last")
    alpha = replace(BASE, profile_id="aaa_first", name="First")
    rev = _ok(store.upsert(zebra, expected_rev=store.rev))
    _ok(store.upsert(alpha, expected_rev=rev))
    assert [profile.profile_id for profile in store.list_profiles()] == [
        "aaa_first",
        "standard_30_min",
        "standard_45_min",
        "zzz_last",
    ]


def test_an_unknown_profile_is_reported_with_the_known_ids(tmp_path: Path) -> None:
    error = _err(_loaded_store(tmp_path).get("standard_30min"))
    assert error.profile_id == "standard_30min"
    assert error.known == ("standard_30_min", "standard_45_min")


def test_a_written_store_round_trips_through_a_fresh_instance(tmp_path: Path) -> None:
    """The whole point of the file: what was saved is what comes back.

    A second :class:`ProfileStore` over the same path, so this exercises the
    real serialise-write-read-parse path rather than an in-memory cache.
    """
    written = _written_store(tmp_path)
    reopened = _store(tmp_path)
    report = _ok(reopened.load())
    assert report.source is LoadSource.STORE_FILE
    assert report.rev == written.rev == StoreRev(1)
    assert _ok(reopened.get(BASE.profile_id)) == BASE
    assert reopened.list_profiles() == written.list_profiles()


def test_the_written_bytes_are_deterministic(tmp_path: Path) -> None:
    """Sorted keys and an explicit newline, so an unchanged store is an unchanged file.

    No spurious diffs on a machine somebody is trying to debug, and no write at
    all when a deploy re-serialises the same content.
    """
    first = _written_store(tmp_path)
    bytes_after_first = (tmp_path / "profiles.json").read_bytes()

    other = tmp_path / "second"
    other.mkdir()
    second = ProfileStore(other / "profiles.json", SHIPPED_DEFAULTS_PATH)
    _ok(second.load())
    _ok(second.upsert(BASE, expected_rev=second.rev))
    assert (other / "profiles.json").read_bytes() == bytes_after_first
    assert b"\r\n" not in bytes_after_first
    assert first.rev == second.rev


# =========================================================================
# The store: writing, revisions, and the power cut
# =========================================================================


def test_a_save_bumps_the_revision_by_one(tmp_path: Path) -> None:
    store = _loaded_store(tmp_path)
    assert store.rev == INITIAL_REV
    assert _ok(store.upsert(BASE, expected_rev=INITIAL_REV)) == StoreRev(1)
    assert store.rev == StoreRev(1)
    document = _store_file_document(tmp_path / "profiles.json")
    assert document["rev"] == 1
    assert document["version"] == SCHEMA_VERSION


def test_a_stale_revision_is_refused_rather_than_overwriting(tmp_path: Path) -> None:
    """Two tabs editing heart-rate limits: last-write-wins is not acceptable.

    The stale save must change nothing at all - not the file, not the cache -
    so the caller can re-read and re-apply its edit to the current state.
    """
    store = _written_store(tmp_path)
    before = (tmp_path / "profiles.json").read_bytes()

    stale = replace(BASE, zone_high_bpm=Bpm(130))
    error = _err(store.upsert(stale, expected_rev=INITIAL_REV))
    assert isinstance(error, RevMismatch)
    assert (error.expected, error.actual) == (INITIAL_REV, StoreRev(1))

    assert (tmp_path / "profiles.json").read_bytes() == before
    assert store.rev == StoreRev(1)
    assert _ok(store.get(BASE.profile_id)).zone_high_bpm == Bpm(138)


def test_deleting_removes_a_profile_and_bumps_the_revision(tmp_path: Path) -> None:
    store = _written_store(tmp_path)
    assert _ok(store.delete("standard_45_min", expected_rev=store.rev)) == StoreRev(2)
    assert "standard_45_min" not in [profile.profile_id for profile in store.list_profiles()]
    reopened = _store(tmp_path)
    _ok(reopened.load())
    assert isinstance(reopened.get("standard_45_min"), Err)


def test_deleting_an_unknown_profile_changes_nothing(tmp_path: Path) -> None:
    store = _written_store(tmp_path)
    before = (tmp_path / "profiles.json").read_bytes()
    error = _err(store.delete("never_existed", expected_rev=store.rev))
    assert isinstance(error, UnknownProfile)
    assert store.rev == StoreRev(1)
    assert (tmp_path / "profiles.json").read_bytes() == before


def test_deleting_with_a_stale_revision_is_refused(tmp_path: Path) -> None:
    store = _written_store(tmp_path)
    error = _err(store.delete(BASE.profile_id, expected_rev=INITIAL_REV))
    assert isinstance(error, RevMismatch)
    assert store.rev == StoreRev(1)


def test_an_empty_store_is_a_deliberate_state_and_not_a_corrupt_one(tmp_path: Path) -> None:
    """Deleting every profile leaves an empty store, and it stays empty.

    Falling back to the defaults here would make the last profile undeletable
    and would look, to whoever tried, like the delete had silently failed.
    """
    store = _written_store(tmp_path)
    rev = store.rev
    for profile_id in ("standard_30_min", "standard_45_min"):
        rev = _ok(store.delete(profile_id, expected_rev=rev))
    assert store.list_profiles() == ()

    reopened = _store(tmp_path)
    report = _ok(reopened.load())
    assert report.source is LoadSource.STORE_FILE
    assert reopened.list_profiles() == ()
    assert report.profile_ids == ()


def test_the_temporary_file_is_already_complete_when_the_rename_happens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual atomicity guarantee, checked at the only instant that matters.

    A power cut between the write and the rename must leave the OLD file whole.
    That is only true if the new content is fully in the temporary file before
    the rename is attempted - so this captures the temporary file's contents at
    exactly that moment and parses them.
    """
    store = _written_store(tmp_path)
    captured: list[str] = []

    def capture_then_die(staged: Path, _target: str | os.PathLike[str]) -> Path:
        captured.append(staged.read_text(encoding="utf-8"))
        raise OSError("power cut")

    monkeypatch.setattr(Path, "replace", capture_then_die)
    addition = replace(BASE, profile_id="new_profile", name="New")
    error = _err(store.upsert(addition, expected_rev=store.rev))
    assert isinstance(error, StoreUnwritable)

    assert len(captured) == 1
    parsed: object = json.loads(captured[0])  # pyright: ignore[reportAny]
    assert isinstance(parsed, dict)
    staged = cast("Mapping[str, object]", parsed)
    assert staged["rev"] == 2
    content = _ok(parse_store(captured[0]))
    assert {profile.profile_id for profile in content.profiles} == {
        "standard_30_min",
        "standard_45_min",
        "new_profile",
    }


def test_a_failed_write_leaves_the_original_intact_and_the_revision_unmoved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing half-done: not the file, not the cache, not the revision.

    Bumping the revision before the write landed would leave the store claiming
    the disk holds an edit it does not, and the next caller's concurrency check
    would then pass against a revision that never existed.
    """
    store = _written_store(tmp_path)
    path = tmp_path / "profiles.json"
    before = path.read_bytes()

    def boom(_self: Path, _target: str | os.PathLike[str]) -> Path:
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "replace", boom)
    addition = replace(BASE, profile_id="new_profile", name="New")
    error = _err(store.upsert(addition, expected_rev=store.rev))
    assert isinstance(error, StoreUnwritable)
    assert error.path == str(path)
    assert "no space left on device" in error.detail

    assert path.read_bytes() == before
    assert store.rev == StoreRev(1)
    assert isinstance(store.get("new_profile"), Err)
    assert not (tmp_path / "profiles.json.tmp").exists()

    monkeypatch.undo()
    assert _ok(store.upsert(addition, expected_rev=store.rev)) == StoreRev(2)
    assert _ok(store.get("new_profile")) == addition


def test_a_leftover_temporary_file_is_never_read(tmp_path: Path) -> None:
    """The other half of the power-cut story: the ``.tmp`` name is not the store.

    A cut after the temporary file was written but before the rename leaves it
    on disk. The loader only ever opens the real name, so the previous complete
    revision is what comes back.
    """
    _written_store(tmp_path)
    other = replace(BASE, zone_high_bpm=Bpm(120), profile_id="never_landed", name="Ghost")
    (tmp_path / "profiles.json.tmp").write_text(_valid_store_text(other, rev=99), encoding="utf-8")

    reopened = _store(tmp_path)
    report = _ok(reopened.load())
    assert report.source is LoadSource.STORE_FILE
    assert reopened.rev == StoreRev(1)
    assert isinstance(reopened.get("never_landed"), Err)


def test_the_bytes_are_flushed_to_the_medium_before_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering the atomicity claim rests on, asserted rather than assumed.

    If the rename happened first, a power cut could publish an empty or partial
    file under the real name - the failure mode the temporary file exists to
    prevent.
    """
    store = _loaded_store(tmp_path)
    calls: list[str] = []
    real_fsync = os.fsync
    real_replace = Path.replace

    def spy_fsync(fd: int) -> None:
        calls.append("fsync")
        real_fsync(fd)

    def spy_replace(self: Path, target: str | os.PathLike[str]) -> Path:
        calls.append("replace")
        return real_replace(self, target)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(Path, "replace", spy_replace)
    _ok(store.upsert(BASE, expected_rev=store.rev))
    assert calls == ["fsync", "replace"]


def test_the_store_directory_is_created_on_demand(tmp_path: Path) -> None:
    """A first save on a fresh install must not fail because a folder is missing."""
    nested = tmp_path / "a" / "b" / "profiles.json"
    store = ProfileStore(nested, SHIPPED_DEFAULTS_PATH)
    _ok(store.load())
    assert _ok(store.upsert(BASE, expected_rev=store.rev)) == StoreRev(1)
    assert nested.exists()


# =========================================================================
# The store: recovering from a corrupt file
# =========================================================================


def test_a_corrupt_store_falls_back_to_the_defaults_loudly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Starting on conservative shipped limits beats not starting at all.

    Refusing to start would leave an operator with a machine that will not run
    and no profiles to fix it with. What makes that acceptable is that nobody
    can miss it: the report says the source, and the log line is at ERROR.
    """
    path = tmp_path / "profiles.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = _store(tmp_path)

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        report = _ok(store.load())

    assert report.source is LoadSource.DEFAULTS_AFTER_CORRUPTION
    assert report.profile_ids == ("standard_30_min", "standard_45_min")
    assert "NOT loaded" in report.detail
    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    assert report.detail in caplog.text


def test_a_corrupt_store_is_moved_aside_with_its_contents_preserved(tmp_path: Path) -> None:
    """Renamed, not deleted: it is the operator's data and the evidence.

    The name is a hash of the contents, so a unit that reboots into the same
    corrupt file ten times leaves one quarantine file rather than ten - which
    matters on an SD card with finite room and finite write cycles.
    """
    path = tmp_path / "profiles.json"
    garbage = '{"version": 1, "rev": "not a number"}'
    path.write_text(garbage, encoding="utf-8")

    report = _ok(_store(tmp_path).load())
    quarantined = report.quarantined
    assert quarantined is not None
    assert quarantined.name.startswith("profiles.json.corrupt-")
    assert quarantined.read_text(encoding="utf-8") == garbage
    assert not path.exists()

    path.write_text(garbage, encoding="utf-8")
    second = _ok(_store(tmp_path).load())
    assert second.quarantined == quarantined
    siblings = sorted(item.name for item in tmp_path.glob("profiles.json.corrupt-*"))
    assert siblings == [quarantined.name]


def test_different_corruption_is_quarantined_under_a_different_name(tmp_path: Path) -> None:
    """Content-addressed, so two distinct failures do not overwrite each other."""
    path = tmp_path / "profiles.json"
    path.write_text("first kind of broken", encoding="utf-8")
    first = _ok(_store(tmp_path).load()).quarantined
    path.write_text("second kind of broken", encoding="utf-8")
    second = _ok(_store(tmp_path).load()).quarantined
    assert first is not None
    assert second is not None
    assert first != second


def test_recovery_still_happens_when_the_rename_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A tidying step must not decide whether the machine works.

    The defaults are loaded either way; ``quarantined`` is ``None`` and the
    failure is logged, so the difference is visible without being fatal.
    """
    path = tmp_path / "profiles.json"
    path.write_text("not json", encoding="utf-8")

    def boom(_self: Path, _target: str | os.PathLike[str]) -> Path:
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "replace", boom)
    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        report = _ok(_store(tmp_path).load())

    assert report.source is LoadSource.DEFAULTS_AFTER_CORRUPTION
    assert report.quarantined is None
    assert report.profile_ids == ("standard_30_min", "standard_45_min")
    assert "could not move the unusable profile store" in caplog.text
    assert path.exists()


def test_a_store_that_is_not_valid_utf8_is_treated_as_corrupt(tmp_path: Path) -> None:
    """Bit rot on an SD card does not produce a tidy JSON error.

    A byte sequence that is not UTF-8 fails before the parser sees it, which is
    a different exception on a different line - and must land on the same
    recovery path.
    """
    path = tmp_path / "profiles.json"
    path.write_bytes(b'{"version": 1, "rev": 0, "profiles": [\xff\xfe]}')
    report = _ok(_store(tmp_path).load())
    assert report.source is LoadSource.DEFAULTS_AFTER_CORRUPTION
    assert report.quarantined is not None
    assert report.quarantined.name.endswith(".corrupt-unreadable")


def test_a_store_path_that_cannot_be_read_at_all_is_treated_as_corrupt(tmp_path: Path) -> None:
    """ "Present and unreadable" is not "absent", and must not read as a first boot.

    A directory where the store belongs is the honest way to produce it: it
    raises an ``OSError`` that is not ``FileNotFoundError``, on every platform.
    """
    (tmp_path / "profiles.json").mkdir()
    report = _ok(_store(tmp_path).load())
    assert report.source is LoadSource.DEFAULTS_AFTER_CORRUPTION
    assert report.profile_ids == ("standard_30_min", "standard_45_min")


def test_a_recovered_store_can_be_written_again(tmp_path: Path) -> None:
    """Recovery leaves a usable store, not a read-only museum piece."""
    (tmp_path / "profiles.json").write_text("broken", encoding="utf-8")
    store = _store(tmp_path)
    _ok(store.load())
    assert _ok(store.upsert(BASE, expected_rev=store.rev)) == StoreRev(1)
    assert (tmp_path / "profiles.json").exists()


def test_missing_defaults_are_the_one_unrecoverable_failure(tmp_path: Path) -> None:
    """Nothing left to fall back to, so refusing to start is the honest answer.

    A broken installation, not broken data - and running a session with no
    limits at all is not an available option.
    """
    store = ProfileStore(tmp_path / "profiles.json", tmp_path / "no-such-defaults.json")
    error = _err(store.load())
    assert isinstance(error, DefaultsUnusable)
    assert error.path.endswith("no-such-defaults.json")
    assert error.detail == "the file is not there"


def test_unreadable_defaults_are_also_unrecoverable(tmp_path: Path) -> None:
    """The other read failure on the defaults path, reported rather than swallowed."""
    defaults = tmp_path / "defaults-as-a-directory"
    defaults.mkdir()
    error = _err(ProfileStore(tmp_path / "profiles.json", defaults).load())
    assert isinstance(error, DefaultsUnusable)
    assert error.detail != "the file is not there"
    assert error.detail


def test_invalid_defaults_are_unrecoverable(tmp_path: Path) -> None:
    """A shipped file that parses as JSON but is not a store is still fatal."""
    defaults = tmp_path / "defaults.json"
    defaults.write_text('{"version": 7, "rev": 0, "profiles": []}', encoding="utf-8")
    error = _err(ProfileStore(tmp_path / "profiles.json", defaults).load())
    assert isinstance(error, DefaultsUnusable)
    assert "unsupported version 7" in error.detail


# =========================================================================
# The store file's own shape
# =========================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("{ not json", "not valid JSON", id="not_json"),
        pytest.param("[]", "top level must be an object, got list", id="top_level_list"),
        pytest.param("null", "top level must be an object, got null", id="top_level_null"),
        pytest.param('{"rev": 0, "profiles": []}', "unsupported version 0", id="version_missing"),
        pytest.param(
            '{"version": 2, "rev": 0, "profiles": []}', "unsupported version 2", id="version_wrong"
        ),
        pytest.param('{"version": 1, "profiles": []}', "must be a whole number", id="rev_missing"),
        pytest.param(
            '{"version": 1, "rev": -1, "profiles": []}', "must not be negative", id="rev_negative"
        ),
        pytest.param(
            '{"version": 1, "rev": 0, "profiles": {}}', "'profiles' must be a list", id="not_a_list"
        ),
        pytest.param(
            '{"version": 1, "rev": 0, "profiles": [3]}',
            "profiles[0] must be an object",
            id="entry_not_an_object",
        ),
        pytest.param(
            '{"version": 1, "rev": 0, "profiles": [{}]}',
            "profiles[0] is malformed",
            id="entry_malformed",
        ),
    ],
)
def test_a_store_file_of_the_wrong_shape_is_malformed(raw: str, expected: str) -> None:
    """Every shape failure named, because each is a different operator mistake.

    Tested on the parser directly rather than through ``load``, which would
    report success after falling back to the defaults and hide which problem
    was found.
    """
    error = _err(parse_store(raw))
    assert expected in error.detail
    assert error.detail == "; ".join(error.problems)


def test_an_empty_profile_list_parses_cleanly() -> None:
    """The state a full delete leaves behind. Valid, not corrupt."""
    content = _ok(parse_store('{"version": 1, "rev": 4, "profiles": []}'))
    assert content.profiles == ()
    assert content.rev == StoreRev(4)


def test_a_stored_profile_with_an_unsafe_limit_makes_the_store_malformed() -> None:
    """A refused profile in the file is a file that cannot be loaded as it stands.

    Loading the rest and dropping the bad one would silently change which
    profiles exist; refusing sends the file to the recovery path, where the
    operator is told and the conservative defaults take over.
    """
    document = dict(profile_to_document(BASE))
    document["zone_high_bpm"] = 160
    raw = json.dumps({"version": 1, "rev": 0, "profiles": [document]})
    error = _err(parse_store(raw))
    assert "profiles[0] was refused" in error.detail
    assert Violation.ZONE_ABOVE_SUBJECT_CEILING.value in error.detail


def test_a_duplicate_profile_id_is_refused_rather_than_shadowed() -> None:
    """Which of the two won would depend on iteration order.

    One would silently shadow the other, so a file with two profiles under the
    same id is refused rather than half-loaded.
    """
    raw = _valid_store_text(BASE, BASE)
    error = _err(parse_store(raw))
    assert f"repeats the id {BASE.profile_id!r}" in error.detail


def test_every_problem_in_a_store_file_is_reported_at_once() -> None:
    """A hand-edited store, like a hand-edited profile, rarely has one problem."""
    raw = '{"version": 9, "rev": -2, "profiles": [1, {}]}'
    error = _err(parse_store(raw))
    assert len(error.problems) == 4


def test_a_non_finite_number_in_the_file_is_refused_by_the_limits(tmp_path: Path) -> None:
    """``json.loads`` accepts a bare ``NaN``; one place decides it is unusable.

    The reader passes non-finite values through deliberately, so that "what
    counts as a usable duration" is decided by :class:`Violation` and not in
    two places that could disagree.
    """
    assert tmp_path.exists()
    document = dict(profile_to_document(BASE))
    document["baseline_s"] = float("nan")
    raw = json.dumps({"version": 1, "rev": 0, "profiles": [document]})
    assert "NaN" in raw
    error = _err(parse_store(raw))
    assert Violation.NON_POSITIVE_DURATION.value in error.detail


# =========================================================================
# Immutability, slots, and the pinned shapes
# =========================================================================

_PROGRAM: Final[Program] = Program(
    profile=BASE,
    source_rev=StoreRev(3),
    resolved_at=UnixMillis(1_700_000_000_000),
    total_overridden=False,
)

IMMUTABLE_RECORDS: Final[tuple[tuple[object, str], ...]] = (
    (BASE, "max_rpm"),
    (PhaseSpan(Phase.HOLD, Seconds(0.0), Seconds(1.0)), "phase"),
    (_PROGRAM, "source_rev"),
    (LoadReport(LoadSource.STORE_FILE, StoreRev(1), (), None, "detail"), "rev"),
    (StoreContent(StoreRev(1), (BASE,)), "rev"),
    (Malformed("detail", ("problem",)), "detail"),
    (Rejected((Violation.EMPTY_NAME,), "detail"), "violations"),
    (UnknownProfile("id", ()), "profile_id"),
    (RevMismatch(StoreRev(1), StoreRev(2)), "expected"),
    (StoreUnwritable("path", "detail"), "path"),
    (DefaultsUnusable("path", "detail"), "path"),
)


@pytest.mark.parametrize(("record", "field_name"), IMMUTABLE_RECORDS, ids=str)
def test_every_record_is_frozen(record: object, field_name: str) -> None:
    """A decision taken from a profile must not change under the decision.

    Programmes especially: a :class:`Program` that could be edited would make
    the session record stop being evidence of what ran.
    """
    with pytest.raises(FrozenInstanceError):
        _assign(record, field_name, None)


@pytest.mark.parametrize(("record", "field_name"), IMMUTABLE_RECORDS, ids=str)
def test_every_record_is_slotted(record: object, field_name: str) -> None:
    """No instance dict, so a typo cannot invent a field that silently does nothing.

    Two exception types are accepted because CPython's generated
    ``__setattr__`` for a frozen slotted dataclass picks between them: a name
    that is a field raises ``FrozenInstanceError``, one that is not falls
    through to a ``super()`` call that raises ``AttributeError``.
    """
    assert field_name
    assert not hasattr(record, "__dict__")
    with pytest.raises((AttributeError, TypeError, FrozenInstanceError)):
        _assign(record, "invented_field", 1)
    assert not hasattr(record, "invented_field")


PROFILE_SHAPE: Final[tuple[tuple[str, str], ...]] = (
    ("profile_id", "str"),
    ("name", "str"),
    ("total_duration_s", "Seconds"),
    ("baseline_s", "Seconds"),
    ("warmup_max_s", "Seconds"),
    ("hold_min_s", "Seconds"),
    ("cooldown_s", "Seconds"),
    ("recovery_s", "Seconds"),
    ("zone_low_bpm", "Bpm"),
    ("zone_high_bpm", "Bpm"),
    ("hard_max_bpm", "Bpm"),
    ("critical_bpm", "Bpm"),
    ("subject_hr_max", "Bpm"),
    ("min_run_rpm", "MotorRpm"),
    ("max_rpm", "MotorRpm"),
    ("warmup_rpm_ceiling_fraction", "float"),
    ("channels", "tuple[Channel, ...]"),
    ("allow_above_nameplate", "bool"),
)

PROGRAM_SHAPE: Final[tuple[tuple[str, str], ...]] = (
    ("profile", "TrainingProfile"),
    ("source_rev", "StoreRev"),
    ("resolved_at", "UnixMillis"),
    ("total_overridden", "bool"),
)

PHASE_SPAN_SHAPE: Final[tuple[tuple[str, str], ...]] = (
    ("phase", "Phase"),
    ("start", "Seconds"),
    ("end", "Seconds"),
)


@pytest.mark.parametrize(
    ("record", "shape"),
    [
        pytest.param(TrainingProfile, PROFILE_SHAPE, id="TrainingProfile"),
        pytest.param(Program, PROGRAM_SHAPE, id="Program"),
        pytest.param(PhaseSpan, PHASE_SPAN_SHAPE, id="PhaseSpan"),
    ],
)
def test_the_record_shapes_are_the_contract(
    record: type, shape: tuple[tuple[str, str], ...]
) -> None:
    """Name, order and annotation text of every field.

    The annotation text is the half that matters most: it is what catches "a
    ``MotorRpm`` became an ``OutputRpm``" - a fifty-fold error the field name
    would not mention - and "an optional stopped being optional", for the three
    modules being written against these shapes in parallel.
    """
    assert tuple((field.name, field.type) for field in fields(record)) == shape


def test_only_the_nameplate_override_has_a_default() -> None:
    """Every limit must be stated. Nothing important may be got by omission.

    A defaulted duration or threshold is a limit somebody did not choose, which
    is exactly the class of mistake this module exists to catch.
    """
    defaulted = {field.name for field in fields(TrainingProfile) if field.default is not MISSING_}
    assert defaulted == {"allow_above_nameplate"}
    assert BASE.allow_above_nameplate is False


MISSING_: Final[object] = fields(PhaseSpan)[0].default
"""The sentinel ``dataclasses`` uses for "no default", read off a field that
has none - so this test does not import a private name."""


# =========================================================================
# The closed error unions
# =========================================================================


def _describe_parse(error: ProfileParseError) -> str:
    """Exhaustive match over :data:`ProfileParseError`, ending in ``assert_never``.

    The nested form of contract rule 3 lives in the tests for the unions this
    module owns: in ``src/`` the ``case _`` arm is unreachable by construction
    and the 100%-branch gate cannot close it (see the note in
    ``src/motor/atv320.py``), while here it costs nothing and still fails the
    type check the moment a variant is added without being handled.
    """
    match error:
        case Malformed(detail, _):
            return f"malformed: {detail}"
        case Rejected(violations, _):
            return f"rejected: {len(violations)}"
        case _ as unreachable:
            assert_never(unreachable)


def _describe_upsert(error: UpsertError) -> str:
    match error:
        case RevMismatch(expected, actual):
            return f"conflict: {expected} != {actual}"
        case StoreUnwritable(path, _):
            return f"unwritable: {path}"
        case _ as unreachable:
            assert_never(unreachable)


def _describe_delete(error: DeleteError) -> str:
    match error:
        case UnknownProfile(profile_id, _):
            return f"unknown: {profile_id}"
        case RevMismatch(expected, actual):
            return f"conflict: {expected} != {actual}"
        case StoreUnwritable(path, _):
            return f"unwritable: {path}"
        case _ as unreachable:
            assert_never(unreachable)


def _describe_resolve(error: ResolveError) -> str:
    match error:
        case UnknownProfile(profile_id, _):
            return f"unknown: {profile_id}"
        case Rejected(violations, _):
            return f"rejected: {len(violations)}"
        case _ as unreachable:
            assert_never(unreachable)


def test_every_parse_failure_is_handled_exhaustively() -> None:
    assert _describe_parse(Malformed("d", ("p",))).startswith("malformed")
    assert _describe_parse(Rejected((Violation.EMPTY_NAME,), "d")) == "rejected: 1"


def test_every_store_failure_is_handled_exhaustively() -> None:
    """Adding a variant to any of these unions fails the type check here.

    Which is the point of the closed unions: an unhandled failure of a store
    that holds heart-rate limits must not reach a bench, let alone a session.
    """
    conflict = RevMismatch(StoreRev(1), StoreRev(2))
    unwritable = StoreUnwritable("p", "d")
    unknown = UnknownProfile("id", ())
    rejected = Rejected((Violation.HOLD_TOO_SHORT,), "d")

    assert _describe_upsert(conflict).startswith("conflict")
    assert _describe_upsert(unwritable).startswith("unwritable")
    assert _describe_delete(unknown).startswith("unknown")
    assert _describe_delete(conflict).startswith("conflict")
    assert _describe_delete(unwritable).startswith("unwritable")
    assert _describe_resolve(unknown).startswith("unknown")
    assert _describe_resolve(rejected).startswith("rejected")


# =========================================================================
# Property tests over arbitrary valid profiles
# =========================================================================


@st.composite
def valid_profiles(draw: st.DrawFn) -> TrainingProfile:
    """Arbitrary profiles that satisfy every limit, built so they cannot be refused.

    Constructed from the constraints rather than filtered against them, so the
    strategy is fast and so a change that makes a limit stricter shows up as a
    refusal from the constructor here rather than as a silent drop in the
    number of examples.
    """
    subject = draw(st.integers(min_value=120, max_value=200))
    zone_high = draw(
        st.integers(min_value=40, max_value=math.floor(ZONE_CEILING_FRACTION * subject))
    )
    zone_low = draw(st.integers(min_value=1, max_value=zone_high - 1))
    hard_max = draw(st.integers(min_value=zone_high, max_value=subject))
    critical = draw(st.integers(min_value=hard_max + 1, max_value=hard_max + 40))

    # Fractional, not whole: round durations add back up to the total exactly,
    # so a strategy that only drew integers could not see an accumulated final
    # boundary drift off the total. `spare` is at least a second so the derived
    # hold cannot land below its floor through float error alone.
    baseline = draw(st.floats(1.0, 600.0))
    warmup = draw(st.floats(1.0, 900.0))
    cooldown = draw(st.floats(float(COMMISSIONED_DECEL_S), 600.0))
    recovery = draw(st.floats(float(MIN_RECOVERY_S), 900.0))
    hold_min = draw(st.floats(1.0, 600.0))
    spare = draw(st.floats(1.0, 3600.0))

    max_rpm = draw(st.integers(min_value=20, max_value=int(NAMEPLATE_MOTOR_RPM)))
    fraction = draw(st.floats(min_value=0.2, max_value=1.0, allow_nan=False, allow_infinity=False))
    min_run = draw(st.integers(min_value=1, max_value=math.floor(max_rpm * fraction)))

    extra = draw(
        st.lists(
            st.sampled_from([member for member in Channel if member is not Channel.ECG]),
            unique=True,
            max_size=3,
        )
    )
    return TrainingProfile(
        profile_id=draw(st.from_regex(r"[a-z][a-z0-9_]{0,20}", fullmatch=True)),
        name=draw(st.from_regex(r"[A-Za-z0-9][A-Za-z0-9 ]{0,20}", fullmatch=True)),
        total_duration_s=Seconds(baseline + warmup + cooldown + recovery + hold_min + spare),
        baseline_s=Seconds(baseline),
        warmup_max_s=Seconds(warmup),
        hold_min_s=Seconds(hold_min),
        cooldown_s=Seconds(cooldown),
        recovery_s=Seconds(recovery),
        zone_low_bpm=Bpm(zone_low),
        zone_high_bpm=Bpm(zone_high),
        hard_max_bpm=Bpm(hard_max),
        critical_bpm=Bpm(critical),
        subject_hr_max=Bpm(subject),
        min_run_rpm=MotorRpm(min_run),
        max_rpm=MotorRpm(max_rpm),
        warmup_rpm_ceiling_fraction=fraction,
        channels=(Channel.ECG, *extra),
        allow_above_nameplate=draw(st.booleans()),
    )


@given(profile=valid_profiles())
def test_any_valid_profile_survives_a_document_round_trip(profile: TrainingProfile) -> None:
    """Write it, read it back, get the same object. For any valid profile.

    Exact equality, not approximate: the fraction is an arbitrary float and
    JSON round-trips finite floats exactly, so an approximate assertion here
    would be hiding a real loss of precision in a ceiling.
    """
    assert _ok(parse_profile(profile_to_document(profile))) == profile


@given(profile=valid_profiles())
def test_any_valid_profile_survives_a_whole_store_round_trip(profile: TrainingProfile) -> None:
    """And through the store file, which is what actually reaches the disk."""
    content = _ok(parse_store(_valid_store_text(profile, rev=7)))
    assert content.rev == StoreRev(7)
    assert content.profiles == (profile,)


@given(profile=valid_profiles())
def test_any_valid_profile_has_a_timeline_that_tiles_its_session(
    profile: TrainingProfile,
) -> None:
    """Contiguous spans, positive durations, ending exactly at the total.

    The invariant every consumer of ``phase_at`` depends on, checked over
    arbitrary durations rather than only the shipped ones.
    """
    timeline = profile.timeline
    assert timeline[0].start == Seconds(0.0)
    assert timeline[-1].end == profile.total_duration_s
    for earlier, later in itertools.pairwise(timeline):
        assert earlier.end == later.start
        assert earlier.duration > 0.0
    assert timeline[-1].duration > 0.0
    assert sum(span.duration for span in timeline) == pytest.approx(profile.total_duration_s)


@given(profile=valid_profiles(), spare=st.integers(min_value=0, max_value=7200))
def test_lengthening_any_session_moves_only_hold(profile: TrainingProfile, spare: int) -> None:
    """For any valid profile: the fixed phases keep their spans, HOLD takes the rest.

    The override rule as a property rather than as one worked example, so it
    cannot be satisfied by a special case around the shipped durations.
    """
    longer = _ok(profile.with_total_duration(Seconds(profile.total_duration_s + spare)))
    assert longer.baseline_s == profile.baseline_s
    assert longer.warmup_max_s == profile.warmup_max_s
    assert longer.cooldown_s == profile.cooldown_s
    assert longer.recovery_s == profile.recovery_s
    assert longer.hold_s == pytest.approx(profile.hold_s + spare)

    before = {span.phase: span for span in profile.timeline}
    after = {span.phase: span for span in longer.timeline}
    for phase in (Phase.BASELINE, Phase.WARMUP):
        assert after[phase] == before[phase]
    assert after[Phase.COOLDOWN].duration == pytest.approx(before[Phase.COOLDOWN].duration)
    assert after[Phase.RECOVERY].duration == pytest.approx(before[Phase.RECOVERY].duration)


@given(profile=valid_profiles())
def test_the_warmup_ceiling_is_always_reachable_and_never_above_the_maximum(
    profile: TrainingProfile,
) -> None:
    """``min_run_rpm <= warmup_rpm_ceiling <= max_rpm``, for any valid profile.

    The range the warmup's control law will clamp into. If the ceiling could
    fall below the starting speed the warmup could only command zero; if it
    could exceed ``max_rpm`` the fraction would not be a ceiling at all.
    """
    assert profile.min_run_rpm <= profile.warmup_rpm_ceiling <= profile.max_rpm


# =========================================================================
# Cross-module agreement and the module's own shape
# =========================================================================


def _channel_map_keys() -> frozenset[str]:
    """The sensor names in ``src/bitalino_client.py``, read with ``ast``.

    Parsed rather than imported: that module imports the ``bitalino`` vendor
    package, which is deliberately absent from the dev environment, and
    importing it here would also break the one-module-per-untyped-library rule.
    """
    tree = ast.parse(BITALINO_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign):
            continue
        target = node.target
        if not isinstance(target, ast.Name) or target.id != CHANNEL_MAP_NAME:
            continue
        mapping = node.value
        assert isinstance(mapping, ast.Dict), f"{CHANNEL_MAP_NAME} is no longer a dict literal"
        names: set[str] = set()
        for key in mapping.keys:
            assert isinstance(key, ast.Constant)
            literal: object = key.value  # pyright: ignore[reportAny]
            assert isinstance(literal, str)
            names.add(literal)
        return frozenset(names)
    pytest.fail(f"{CHANNEL_MAP_NAME} was not found in {BITALINO_SOURCE}")


def test_the_channel_names_match_the_acquisition_layer() -> None:
    """The channel name chooses which analog column a sample is read from.

    So this is not a tidiness test. A rename in ``CHANNEL_MAP`` that did not
    reach :class:`Channel` would not produce missing data - it would produce an
    EMG or a light reading arriving where the ECG belongs, and the machine
    would regulate its speed on it. Compared against that module's source, so
    the failure lands here.
    """
    assert {member.value for member in Channel} == _channel_map_keys()


def _imported_modules(source: Path) -> frozenset[str]:
    """Every module name imported by ``source``, as written."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return frozenset(names)


def test_the_plan_depends_only_on_the_vocabulary_and_the_units() -> None:
    """One direction: the plan imports the vocabulary, never the reverse.

    In particular it does not import the drive, the control law or the safety
    supervisor. A profile is a document about a session, not a participant in
    one, and keeping the dependency thin is what lets the store be loaded at
    startup before any hardware exists.
    """
    project_imports = {name for name in _imported_modules(PLAN_SOURCE) if name.startswith("src.")}
    assert project_imports == {"src.result", "src.training.types", "src.units"}


def test_the_plan_never_reads_a_clock() -> None:
    """``resolved_at`` arrives as a parameter, and ``phase_at`` is pure.

    There is a repo-wide grep test for direct ``time.monotonic()`` calls; this
    one forbids the import outright, so the temptation is not in scope. It is
    also what lets a 45-minute programme be walked end to end in a test in
    under a millisecond.
    """
    assert "time" not in _imported_modules(PLAN_SOURCE)


def test_the_shipped_defaults_file_is_where_the_module_says_it_is() -> None:
    """Resolved from the module's own location, not the working directory.

    The recovery path has to work from a systemd unit or an ``atexit`` handler
    with a different cwd, which is why the path is not relative.
    """
    assert SHIPPED_DEFAULTS_PATH.is_file()
    assert SHIPPED_DEFAULTS_PATH == PROJECT_ROOT / "config" / "profiles.default.json"
    document = _store_file_document(SHIPPED_DEFAULTS_PATH)
    assert document["version"] == SCHEMA_VERSION
    assert document["rev"] == 0

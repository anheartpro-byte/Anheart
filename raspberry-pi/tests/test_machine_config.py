"""What :mod:`src.machine_config` makes of a proposal beyond the shared cases (ANH-141).

The shared verdicts are ``test_machine_config_parity.py``. Here: what only the
console can judge (its local keys, its medical parameters, a value JSON alone
can carry), and what the proposal becomes as a ``.env``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Final, cast

import pytest

from src.local_config import ConfigProblem
from src.machine_config import (
    KEYS,
    ConfigValue,
    ValidationLevel,
    ValueType,
    env_text,
    environment,
    lock_refusal,
    read_value,
    read_values,
    review,
)
from src.result import Err, Ok
from tests.medical_support import signed, unsigned
from tests.test_machine_config_parity import CASES

BASE: Final[dict[str, object]] = dict(CASES["base"])


def _problems(document: dict[str, object], **local: str) -> tuple[ConfigProblem, ...]:
    reviewed = review(document, local, ValidationLevel.OCCUPIED, signed())
    assert isinstance(reviewed, Err)
    return reviewed.error


def test_an_accepted_proposal_is_the_console_s_configuration() -> None:
    reviewed = review({**BASE, "LEG_TIP_RADIUS_M": 2.43}, {}, ValidationLevel.NONE, signed())
    assert isinstance(reviewed, Ok)
    assert float(reviewed.value.config.geometry.radius) == 1.5
    assert reviewed.value.config.leg_tip_radius == 2.43
    assert reviewed.value.values["name"] == "Banc d'essai"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, None),
        (True, "true"),
        (False, "false"),
        ("sim", "sim"),
        (300, "300"),
        (0.1, "0.1"),
        (("ECG", "EDA"), "ECG,EDA"),
    ],
)
def test_a_value_is_written_as_a_env_would_hold_it(
    value: str | int | float | bool | tuple[str, ...] | None, text: str | None
) -> None:
    assert env_text(value) == text


def test_the_local_env_keeps_its_own_keys_and_loses_the_dashboard_s() -> None:
    merged = environment(
        {"ARM_RADIUS_M": 1.5, "LEG_TIP_RADIUS_M": None, "name": "Banc"},
        {"ARM_RADIUS_M": "1.2", "LEG_TIP_RADIUS_M": "2.0", "UI_HOST": "127.0.0.1"},
    )
    assert merged == {"ARM_RADIUS_M": "1.5", "UI_HOST": "127.0.0.1"}


def test_a_local_key_the_proposal_contradicts_is_refused_by_the_console() -> None:
    """The safety relay's wiring stays local: a backend it does not fit is not taken."""
    problems = _problems(BASE, STO_SOURCE="bridged_declared")
    assert problems == (
        ConfigProblem("STO_SOURCE", "bridged_declared interdit avec MOTOR_BACKEND=sim"),
    )


def test_a_lock_the_console_would_run_off_is_refused_with_unsigned_medical_parameters() -> None:
    document = {**BASE, "PROGRAMS_ENABLED": True, "OCCUPANCY_OCCUPIED_ENABLED": True}
    reviewed = review(document, {}, ValidationLevel.OCCUPIED, unsigned())
    assert isinstance(reviewed, Err)
    assert [problem.key for problem in reviewed.error] == [
        "PROGRAMS_ENABLED",
        "OCCUPANCY_OCCUPIED_ENABLED",
    ]
    assert all(
        problem.detail.startswith("refusee a true par la console : ") for problem in reviewed.error
    )
    # Off, the same proposal runs as approved.
    assert isinstance(review(BASE, {}, ValidationLevel.OCCUPIED, unsigned()), Ok)


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_a_number_json_alone_carries_is_refused(raw: str) -> None:
    value: object = json.loads(raw)  # pyright: ignore[reportAny]  # a float, NaN or infinite
    read = read_values({**BASE, "ARM_RADIUS_M": value, "MOTOR_MAX_RPM": value})
    assert [problem.key for problem in read.problems] == ["MOTOR_MAX_RPM", "ARM_RADIUS_M"]


@pytest.mark.parametrize("raw", [{"a": 1}, ["ECG", 1], "\ud800", "a\x7fb", 1.5])
def test_a_value_of_another_shape_is_refused(raw: object) -> None:
    read = read_values({**BASE, "name": raw, "SENSORS": raw})
    assert [problem.key for problem in read.problems] == ["name", "SENSORS"]


def test_a_paired_surrogate_is_one_character_of_a_text() -> None:
    read = read_values({**BASE, "name": "Banc \U0001f600"})
    assert read.problems == ()


def test_turning_a_lock_off_is_always_allowed() -> None:
    for level in ValidationLevel:
        assert lock_refusal(level, False, ValidationLevel.OCCUPIED) is None
    assert lock_refusal(ValidationLevel.OCCUPIED, True, ValidationLevel.OCCUPIED) is None
    assert lock_refusal(ValidationLevel.AUTO, True, ValidationLevel.OCCUPIED) == (
        "machine validée jusqu'au niveau auto"
    )


def test_every_key_has_a_family_and_the_safety_ones_are_the_ticket_s() -> None:
    safety = {key for key, spec in KEYS.items() if spec.family.value == "safety"}
    assert safety == {
        "MOTOR_BACKEND",
        "MOTOR_MAX_RPM",
        "ARM_RADIUS_M",
        "LEG_TIP_RADIUS_M",
        "PROGRAMS_ENABLED",
        "OCCUPANCY_OCCUPIED_ENABLED",
    }


def test_a_value_or_a_type_that_is_not_one_fails_loudly() -> None:
    not_a_value = cast(ConfigValue, object())
    with pytest.raises(AssertionError):
        _ = env_text(not_a_value)
    not_a_type = replace(KEYS["name"], kind=cast(ValueType, "bogus"))
    with pytest.raises(AssertionError):
        _ = read_value(not_a_type, "Banc")

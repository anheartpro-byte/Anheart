from __future__ import annotations

from dataclasses import replace

import pytest

from simulation.harness import RUNTIME_LIMITS
from simulation.signal_loss import Category, LossCase, load, replay
from src.units import Seconds


@pytest.mark.parametrize(
    ("scenario", "subject", "category", "at"),
    [
        ("auto_jog_subject_fit", "fast_responder", Category.REST, 233.4),
        ("auto_jog_subject_fit", "fit", Category.ROTATING, 393.2),
        ("manual_occupied_ceiling", "fit", Category.MANUAL, 11.0),
    ],
)
def test_t01_loss_instants_never_raise_the_applied_setpoint_after_the_recent_reading_budget(
    scenario: str, subject: str, category: Category, at: float
) -> None:
    base = load(scenario)
    if category is not Category.MANUAL:
        base = replace(base, subject=load(f"auto_jog_subject_{subject}").subject)
    case = LossCase(f"t01-{scenario}-{category.value}", category, base, Seconds(at))
    measured = replay(case)
    assert measured.violations(RUNTIME_LIMITS.rise_reading_within) == ()
    if category is Category.REST:
        assert not any(rise.from_rest for rise in measured.rises)

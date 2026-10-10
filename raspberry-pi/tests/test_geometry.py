"""Tests for the one machine geometry every speed is rendered through.

The reference table is the one in the local-console plan (r = 1.5 m, i = 49.79,
1380 rpm at 50 Hz), derived by hand there from Gc = 0.0016768 x n_out^2 and
Gr = sqrt(Gc^2 + 1). It is asserted here against numbers typed in, not against
the functions under test, so a shared bug in ``src/units.py`` cannot make both
sides agree.
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.geometry import CONFIRMED_GEAR_RATIO, MachineGeometry
from src.units import GearRatio, Hertz, Metres, MotorRpm, ResultantG

ARM: Final[MachineGeometry] = MachineGeometry(radius=Metres(1.5))


def test_the_confirmed_ratio_is_the_bench_measured_one() -> None:
    """49.79, confirmed on the bench: 1380 / 49.79 = 27.7 output rpm, 13.9 turns in 30 s."""
    assert CONFIRMED_GEAR_RATIO == 49.79
    assert ARM.ratio == 49.79
    assert ARM.nominal_rpm == 1380
    assert ARM.base_hz == 50.0


def test_there_is_no_default_radius() -> None:
    """A measurement of this rig. A default is how a wrong one gets used."""
    with pytest.raises(TypeError):
        MachineGeometry()  # type: ignore[call-arg]  # the point of the test


def test_the_geometry_is_keyword_only() -> None:
    """Radius and ratio are both float-looking; positional use could swap them silently."""
    with pytest.raises(TypeError):
        MachineGeometry(Metres(1.5))  # type: ignore[call-arg]  # the point of the test


def test_the_geometry_is_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        ARM.radius = Metres(2.0)  # type: ignore[misc]  # the point of the test


@pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf])
@pytest.mark.parametrize("name", ["radius", "ratio", "nominal_rpm", "base_hz"])
def test_the_geometry_refuses_a_quantity_that_cannot_describe_a_machine(
    name: str, bad: float
) -> None:
    """NaN in particular: every comparison against it is false, so ``<= 0`` accepts it."""
    fields: dict[str, float] = {"radius": 1.5, name: bad}
    with pytest.raises(ValueError, match=f"{name} must be positive and finite"):
        MachineGeometry(
            radius=Metres(fields["radius"]),
            ratio=GearRatio(fields.get("ratio", 49.79)),
            nominal_rpm=MotorRpm(1380) if name != "nominal_rpm" else _bad_rpm(bad),
            base_hz=Hertz(fields.get("base_hz", 50.0)),
        )


def _bad_rpm(bad: float) -> MotorRpm:
    """``MotorRpm`` is an ``int``; NaN and inf cannot be one, so they map to zero."""
    return MotorRpm(int(bad)) if math.isfinite(bad) else MotorRpm(0)


@pytest.mark.parametrize(
    ("load", "output_rpm", "motor_rpm", "hertz"),
    [
        (1.1, 16.5, 823, 29.8),
        (1.2, 19.9, 990, 35.9),
        (1.3, 22.3, 1108, 40.2),
        (1.4, 24.2, 1204, 43.6),
        (1.5, 25.8, 1286, 46.6),
        (2.0, 32.1, 1600, 58.0),
    ],
)
def test_the_plan_conversion_table(
    load: float, output_rpm: float, motor_rpm: int, hertz: float
) -> None:
    """The plan's table at r = 1.5 m. It rounds to 0.1, so +/-0.06 on rpm and Hz, +/-1 motor rpm."""
    assert ARM.output_rpm_for(ResultantG(load)) == pytest.approx(output_rpm, abs=0.06)
    motor = ARM.motor_rpm_for(ResultantG(load))
    assert abs(motor - motor_rpm) <= 1
    view = ARM.view(motor)
    assert view.hertz == pytest.approx(hertz, abs=0.06)
    assert view.resultant_g == pytest.approx(load, abs=0.01)


def test_the_nameplate_is_1_63_resultant_g_at_this_radius() -> None:
    """1380 rpm -> 27.716 output rpm -> Gc 1.28855 -> Gr sqrt(1.28855^2 + 1) = 1.63106."""
    view = ARM.view(MotorRpm(1380))
    assert view.output_rpm == pytest.approx(27.716409, rel=1e-6)
    assert view.hertz == pytest.approx(50.0)
    assert view.g_load == pytest.approx(1.288553, rel=1e-5)
    assert view.resultant_g == pytest.approx(1.631062, rel=1e-5)


@pytest.mark.parametrize("load", [1.0, 0.5, math.nan])
def test_gravity_alone_is_standstill(load: float) -> None:
    assert ARM.output_rpm_for(ResultantG(load)) == 0.0
    assert ARM.motor_rpm_for(ResultantG(load)) == 0


@given(load=st.floats(min_value=1.01, max_value=2.5, allow_nan=False))
def test_the_speed_for_a_load_renders_back_to_that_load(load: float) -> None:
    """Rounding to whole motor rpm costs well under 0.005 g anywhere in the range."""
    view = ARM.view(ARM.motor_rpm_for(ResultantG(load)))
    assert view.resultant_g == pytest.approx(load, abs=0.005)


@given(
    low=st.floats(min_value=1.0, max_value=2.5, allow_nan=False),
    high=st.floats(min_value=1.0, max_value=2.5, allow_nan=False),
)
def test_a_higher_load_never_needs_a_lower_speed(low: float, high: float) -> None:
    """Monotone: a ceiling written in g cannot map to a lower rpm than a smaller one."""
    small, large = sorted((low, high))
    assert ARM.motor_rpm_for(ResultantG(small)) <= ARM.motor_rpm_for(ResultantG(large))

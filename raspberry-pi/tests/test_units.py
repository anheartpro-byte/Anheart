"""Tests for the domain units.

Two jobs. First, the boundary constructors must reject out-of-domain hardware
values rather than passing them downstream. Second, the conversions must be
exact and must round-trip, because the gearbox ratio makes a motor/output
confusion a factor-of-50 error and nothing downstream would notice.

The reference numbers come from the hardware nameplates: SEW KA37 DRS71S4 at
1380 rpm / 50 Hz, gearbox i = 49.79, BITalino 10-bit ADC with the ECG sensor
gain of 1100 on a 3.3 V rail.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.result import Err, Ok
from src.units import (
    ADC_MAX,
    ADC_MIN,
    REGISTER_MAX,
    SIGNED16_MAX,
    SIGNED16_MIN,
    AdcCount,
    GearRatio,
    GLoad,
    Hertz,
    Metres,
    Millivolts,
    Monotonic,
    MotorRpm,
    OutputRpm,
    RawRegister,
    ResultantG,
    adc_count,
    adc_to_millivolts,
    elapsed,
    g_to_output_rpm,
    hertz_to_motor_rpm,
    millivolts_to_adc,
    motor_rpm_to_hertz,
    motor_to_output_rpm,
    output_rpm_to_g,
    output_to_motor_rpm,
    raw_register,
    register_to_signed,
    resultant_g,
    resultant_g_to_output_rpm,
    signed_to_register,
)

GEAR_RATIO = GearRatio(49.79)
NOMINAL_RPM = MotorRpm(1380)
BASE_FREQ = Hertz(50.0)
RADIUS = Metres(1.0)

#: (3.3 V / 1024 levels / gain 1100) x 1000, i.e. mV per ADC count for ECG.
ECG_MV_PER_COUNT = (3.3 / 1024 / 1100) * 1000.0


def _ok_register(value: int) -> RawRegister:
    """Parse a register, asserting success, so the union is narrowed for mypy."""
    parsed = raw_register(value)
    assert isinstance(parsed, Ok), parsed
    return parsed.value


# --- ADC boundary --------------------------------------------------------


def test_adc_accepts_the_full_10_bit_domain() -> None:
    assert adc_count(ADC_MIN) == Ok(AdcCount(0))
    assert adc_count(ADC_MAX) == Ok(AdcCount(1023))
    assert adc_count(512) == Ok(AdcCount(512))


def test_adc_rejects_values_outside_the_domain() -> None:
    """Out of range means a misaligned frame or a wrong column, never a reading."""
    below = adc_count(-1)
    above = adc_count(1024)
    assert isinstance(below, Err)
    assert isinstance(above, Err)
    assert below.error.quantity == "adc"
    assert above.error.value == 1024.0
    assert above.error.high == 1023.0


# --- Modbus registers ----------------------------------------------------


def test_raw_register_accepts_the_16_bit_domain() -> None:
    assert raw_register(0) == Ok(0)
    assert raw_register(REGISTER_MAX) == Ok(0xFFFF)


def test_raw_register_rejects_wider_values() -> None:
    result = raw_register(70000)
    assert isinstance(result, Err)
    assert result.error.quantity == "register"


def test_register_to_signed_reads_negative_speeds() -> None:
    """Read unsigned, a reverse speed of -1 rpm would look like 65535 rpm."""
    assert register_to_signed(_ok_register(65535)) == -1
    assert register_to_signed(_ok_register(65236)) == -300
    assert register_to_signed(_ok_register(SIGNED16_MAX)) == SIGNED16_MAX
    assert register_to_signed(_ok_register(0)) == 0


def test_signed_to_register_encodes_negatives() -> None:
    assert signed_to_register(-1) == Ok(65535)
    assert signed_to_register(0) == Ok(0)
    assert signed_to_register(SIGNED16_MAX) == Ok(32767)
    assert signed_to_register(SIGNED16_MIN) == Ok(32768)


def test_signed_to_register_rejects_out_of_range() -> None:
    for value in (SIGNED16_MAX + 1, SIGNED16_MIN - 1, 40000):
        result = signed_to_register(value)
        assert isinstance(result, Err), value
        assert result.error.quantity == "signed16"


@given(st.integers(min_value=SIGNED16_MIN, max_value=SIGNED16_MAX))
def test_signed_register_round_trip(value: int) -> None:
    """Any signed rpm the drive can hold survives encode then decode."""
    encoded = signed_to_register(value)
    assert isinstance(encoded, Ok)
    assert register_to_signed(encoded.value) == value


# --- Gearbox: the factor-of-50 error ------------------------------------


def test_nameplate_speed_maps_to_documented_output_speed() -> None:
    assert motor_to_output_rpm(NOMINAL_RPM, GEAR_RATIO) == OutputRpm(1380 / 49.79)
    assert round(motor_to_output_rpm(NOMINAL_RPM, GEAR_RATIO), 3) == 27.716


def test_gearbox_conversion_round_trips() -> None:
    output = motor_to_output_rpm(NOMINAL_RPM, GEAR_RATIO)
    assert output_to_motor_rpm(output, GEAR_RATIO) == NOMINAL_RPM


@given(st.integers(min_value=0, max_value=1500))
def test_gearbox_round_trip_for_any_reachable_speed(rpm: int) -> None:
    output = motor_to_output_rpm(MotorRpm(rpm), GEAR_RATIO)
    assert output_to_motor_rpm(output, GEAR_RATIO) == MotorRpm(rpm)


# --- Frequency: the HSP ceiling -----------------------------------------


def test_nameplate_point_is_exact() -> None:
    assert motor_rpm_to_hertz(NOMINAL_RPM, NOMINAL_RPM, BASE_FREQ) == Hertz(50.0)


def test_software_ceiling_maps_to_a_drive_ceiling() -> None:
    """900 rpm needs HSP just above 32.61 Hz; the plan sets 34 Hz."""
    assert round(motor_rpm_to_hertz(MotorRpm(900), NOMINAL_RPM, BASE_FREQ), 2) == 32.61
    assert hertz_to_motor_rpm(Hertz(34.0), NOMINAL_RPM, BASE_FREQ) == MotorRpm(938)


def test_zero_speed_is_zero_frequency() -> None:
    assert motor_rpm_to_hertz(MotorRpm(0), NOMINAL_RPM, BASE_FREQ) == Hertz(0.0)
    assert hertz_to_motor_rpm(Hertz(0.0), NOMINAL_RPM, BASE_FREQ) == MotorRpm(0)


# --- Centripetal load ----------------------------------------------------


def test_full_speed_g_matches_the_design_figure() -> None:
    output = motor_to_output_rpm(NOMINAL_RPM, GEAR_RATIO)
    assert round(output_rpm_to_g(output, RADIUS), 3) == 0.859


def test_g_is_quadratic_in_speed() -> None:
    """Halving the speed quarters the load; this is why control gain falls off."""
    fast = output_rpm_to_g(OutputRpm(28.0), RADIUS)
    half = output_rpm_to_g(OutputRpm(14.0), RADIUS)
    assert math.isclose(fast / half, 4.0, rel_tol=1e-6)


def test_zero_speed_is_zero_g() -> None:
    assert output_rpm_to_g(OutputRpm(0.0), RADIUS) == GLoad(0.0)


def test_g_conversion_round_trips() -> None:
    load = output_rpm_to_g(OutputRpm(20.0), RADIUS)
    assert math.isclose(g_to_output_rpm(load, RADIUS), 20.0, rel_tol=1e-6)


def test_non_physical_g_inputs_give_zero_rather_than_a_domain_error() -> None:
    """A radius of zero is the default until the rig is measured; g is then hidden."""
    assert g_to_output_rpm(GLoad(-1.0), RADIUS) == OutputRpm(0.0)
    assert g_to_output_rpm(GLoad(0.0), RADIUS) == OutputRpm(0.0)
    assert g_to_output_rpm(GLoad(1.0), Metres(0.0)) == OutputRpm(0.0)


# --- ECG scaling ---------------------------------------------------------


def test_one_millivolt_lands_where_the_design_says() -> None:
    """A 1 mV R peak must sit at ADC 853: comfortable, not clipping."""
    assert millivolts_to_adc(Millivolts(1.0), ECG_MV_PER_COUNT) == AdcCount(853)


def test_adc_scaling_round_trips_through_millivolts() -> None:
    counts = adc_count(341)
    assert isinstance(counts, Ok)
    assert round(adc_to_millivolts(counts.value, ECG_MV_PER_COUNT), 3) == 0.999


def test_zero_millivolts_is_mid_scale() -> None:
    assert millivolts_to_adc(Millivolts(0.0), ECG_MV_PER_COUNT) == AdcCount(512)


def test_large_signals_clip_like_real_hardware() -> None:
    """Clamping is correct here: a real BITalino clips and the quality gate notices."""
    assert millivolts_to_adc(Millivolts(99.0), ECG_MV_PER_COUNT) == AdcCount(ADC_MAX)
    assert millivolts_to_adc(Millivolts(-99.0), ECG_MV_PER_COUNT) == AdcCount(ADC_MIN)


@given(st.floats(min_value=-1.4, max_value=1.4, allow_nan=False, allow_infinity=False))
def test_millivolt_to_adc_stays_in_domain(value: float) -> None:
    count = millivolts_to_adc(Millivolts(value), ECG_MV_PER_COUNT)
    assert ADC_MIN <= count <= ADC_MAX


# --- Time ----------------------------------------------------------------


def test_elapsed_is_a_duration() -> None:
    assert elapsed(Monotonic(10.0), Monotonic(14.5)) == 4.5


def test_elapsed_of_the_same_instant_is_zero() -> None:
    assert elapsed(Monotonic(7.0), Monotonic(7.0)) == 0.0


# =========================================================================
# Resultant g: what the occupant feels
# =========================================================================


def test_the_resultant_adds_gravity_in_quadrature() -> None:
    """Hand-derived: sqrt(0^2 + 1) = 1, sqrt(1^2 + 1) = 1.41421, sqrt(1.72^2 + 1) = 1.98959."""
    assert resultant_g(GLoad(0.0)) == 1.0
    assert resultant_g(GLoad(1.0)) == math.sqrt(2.0)
    assert resultant_g(GLoad(1.72)) == pytest.approx(1.989573, rel=1e-6)


def test_the_engineers_two_g_is_a_resultant_at_32_output_rpm() -> None:
    """32 output rpm at 1.5 m: omega = 3.35103 rad/s, Gc = 1.71762, Gr = 1.98752."""
    gc = output_rpm_to_g(OutputRpm(32.0), Metres(1.5))
    assert gc == pytest.approx(1.717623, rel=1e-5)
    assert resultant_g(gc) == pytest.approx(1.987518, rel=1e-5)


@pytest.mark.parametrize("load", [1.0, 0.99, 0.0, -2.0, math.nan, math.inf, -math.inf])
def test_a_resultant_at_or_below_gravity_alone_is_standstill(load: float) -> None:
    """Gravity alone gives 1 g, so nothing at or below it is a speed; non-finite is never one."""
    assert resultant_g_to_output_rpm(ResultantG(load), Metres(1.5)) == 0.0


@given(
    rpm=st.floats(min_value=1.0, max_value=60.0, allow_nan=False),
    radius=st.floats(min_value=0.2, max_value=5.0, allow_nan=False),
)
def test_resultant_round_trips_through_output_rpm(rpm: float, radius: float) -> None:
    """rpm -> Gc -> Gr -> rpm is the identity above standstill.

    To 1e-6, not to the last bit: near Gr = 1 the inverse subtracts two nearly
    equal numbers (Gr^2 - 1), so precision is lost there by construction. At
    1 output rpm and 0.2 m the load is 1.0000001 g, far below anything this
    machine is commanded to, and the error is still under a millionth.
    """
    load = resultant_g(output_rpm_to_g(OutputRpm(rpm), Metres(radius)))
    assert load >= 1.0
    assert math.isclose(resultant_g_to_output_rpm(load, Metres(radius)), rpm, rel_tol=1e-6)


@given(gc=st.floats(min_value=0.0, max_value=100.0, allow_nan=False))
def test_the_resultant_is_never_below_gravity_or_the_centripetal_part(gc: float) -> None:
    """Gr >= max(1, Gc) for any centripetal load: the occupant never feels less than either."""
    load = resultant_g(GLoad(gc))
    assert load >= 1.0
    assert load >= gc

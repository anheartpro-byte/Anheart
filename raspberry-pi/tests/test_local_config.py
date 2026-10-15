"""Tests for the local console's configuration.

Every key is exercised through :func:`load_local_config` with a plain dict, so
nothing here reads the process environment, opens a port or needs a ``.env``.
The medical parameters are given as a status (:mod:`tests.medical_support`)
where a test is about them; elsewhere they are the shipped file's, verified.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Final, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.local_config import (
    BENCH_CONSOLE_PORT,
    DEFAULT_MOTOR_MAX_RPM,
    MEDICAL_KEYS,
    CardiacTiers,
    CloudConfig,
    ConfigProblem,
    DriveLink,
    EcgSource,
    LocalConfig,
    MotorBackend,
    bitalino_address,
    build_drive_link,
    load_local_config,
)
from src.medical import SHIPPED_PARAMETERS, MedicalStatus
from src.motor.ftdi_link import SCHNEIDER_CABLE_URL, Parity
from src.result import Err, Ok
from src.training.motion import DEFAULT_MOTION_LIMITS
from src.training.types import Occupancy, OccupancyRefused
from src.units import Bpm, GLoadPerSecond, MotorRpm, ResultantG
from tests.medical_support import signed, unsigned

#: The owner's documented bench .env for the real hardware.
BENCH_ENV: Final[Mapping[str, str]] = {
    "MOTOR_BACKEND": "serial",
    "MOTOR_PORT": "ftdi://schneider:rs485/1",
    "MOTOR_SLAVE_ID": "248",
    "ECG_SOURCE": "rfcomm",
    "BITALINO_ADDRESS": "rfcomm:98-d3-91-fe-4e-9f",
    "ARM_RADIUS_M": "1.5",
    "UI_PORT": "8090",
}

#: The full dry run: nothing opened.
SIM_ENV: Final[Mapping[str, str]] = {
    "MOTOR_BACKEND": "sim",
    "ECG_SOURCE": "sim",
    "ARM_RADIUS_M": "1.5",
}


def _load(env: Mapping[str, str], medical: MedicalStatus | None = None) -> LocalConfig:
    loaded = load_local_config(env, medical)
    assert isinstance(loaded, Ok), f"unexpected problems: {loaded!r}"
    return loaded.value


def _problems(
    env: Mapping[str, str], medical: MedicalStatus | None = None
) -> tuple[ConfigProblem, ...]:
    loaded = load_local_config(env, medical)
    assert isinstance(loaded, Err), f"accepted a bad configuration: {loaded!r}"
    return loaded.error


def _keys(env: Mapping[str, str], medical: MedicalStatus | None = None) -> set[str]:
    return {problem.key for problem in _problems(env, medical)}


def _with(base: Mapping[str, str], **changes: str) -> dict[str, str]:
    merged = dict(base)
    merged.update(changes)
    return merged


def _without(base: Mapping[str, str], key: str) -> dict[str, str]:
    return {name: value for name, value in base.items() if name != key}


# =========================================================================
# The two documented setups
# =========================================================================


def test_the_bench_env_loads_as_documented() -> None:
    config = _load(BENCH_ENV)
    assert config.motor_backend is MotorBackend.SERIAL
    link = config.drive_link
    assert link is not None
    assert link.settings.port == SCHNEIDER_CABLE_URL
    assert link.settings.slave_address == 248
    assert link.settings.parity is Parity.EVEN
    assert link.registers.offset == 0
    assert "esclave 248" in link.describe()
    assert config.ecg_source is EcgSource.RFCOMM
    assert config.bitalino_address == "rfcomm:98-d3-91-fe-4e-9f"
    assert config.geometry.radius == 1.5
    assert config.geometry.ratio == 49.79
    assert config.motor_max_rpm == DEFAULT_MOTOR_MAX_RPM == 300
    assert config.occupied_enabled is False
    assert config.web.port == 8090
    assert config.web.host == "127.0.0.1"
    assert config.web.token is None
    assert config.medical.signed
    assert config.motion == DEFAULT_MOTION_LIMITS


def test_the_simulation_env_needs_no_hardware_key() -> None:
    config = _load(SIM_ENV)
    assert config.motor_backend is MotorBackend.SIM
    assert config.drive_link is None
    assert config.ecg_source is EcgSource.SIM
    assert config.bitalino_address is None


def test_blank_values_fall_back_to_defaults_as_dotenv_leaves_them() -> None:
    config = _load(_with(SIM_ENV, MOTOR_MAX_RPM="  ", GEAR_RATIO="", UI_TOKEN=""))
    assert config.motor_max_rpm == 300
    assert config.geometry.ratio == 49.79
    assert config.web.token is None


def test_paths_and_a_token_can_be_set() -> None:
    config = _load(
        _with(
            SIM_ENV,
            UI_HOST="0.0.0.0",  # noqa: S104  # the refusal path is the next test's
            UI_TOKEN="a-sixteen-char-token",  # noqa: S106  # a test value
            # Off loopback the console serves TLS or does not start (ANH-151):
            # two paths, which this module does not open.
            UI_TLS_CERT="/etc/anheart/tls/console.crt",
            UI_TLS_KEY="/etc/anheart/tls/console.key",
        )
    )
    assert config.web.token == "a-sixteen-char-token"  # noqa: S105  # a test value


# =========================================================================
# ARM_RADIUS_M: required, no default
# =========================================================================


@pytest.mark.parametrize("radius", [None, "", "abc", "nan", "inf", "0", "-1.5", "15"])
def test_the_radius_is_required_and_plausible(radius: str | None) -> None:
    env = (
        _without(SIM_ENV, "ARM_RADIUS_M") if radius is None else _with(SIM_ENV, ARM_RADIUS_M=radius)
    )
    assert _keys(env) == {"ARM_RADIUS_M"}


# =========================================================================
# MOTOR_MAX_RPM: 0..1380, default 300
# =========================================================================


@given(rpm=st.integers(min_value=0, max_value=1380))
def test_any_ceiling_up_to_the_nameplate_is_accepted(rpm: int) -> None:
    assert _load(_with(SIM_ENV, MOTOR_MAX_RPM=str(rpm))).motor_max_rpm == rpm


@pytest.mark.parametrize("rpm", ["1381", "1600", "-1", "3OO"])
def test_a_ceiling_above_the_nameplate_or_not_a_number_is_refused(rpm: str) -> None:
    assert _keys(_with(SIM_ENV, MOTOR_MAX_RPM=rpm)) == {"MOTOR_MAX_RPM"}


@pytest.mark.parametrize("ratio", ["0", "-49.79", "x", "nan"])
def test_a_gear_ratio_must_be_positive_and_finite(ratio: str) -> None:
    assert _keys(_with(SIM_ENV, GEAR_RATIO=ratio)) == {"GEAR_RATIO"}


# =========================================================================
# Occupancy
# =========================================================================


def test_bench_uses_the_configured_ceiling() -> None:
    config = _load(_with(SIM_ENV, MOTOR_MAX_RPM="450"))
    assert config.ceiling_for(Occupancy.BENCH) == Ok(MotorRpm(450))
    assert Occupancy.BENCH.label == "BANC - personne a bord : NON"


def test_a_person_on_board_is_refused_by_default() -> None:
    config = _load(SIM_ENV)
    match config.ceiling_for(Occupancy.OCCUPIED):
        case Ok(rpm):
            pytest.fail(f"OCCUPIED allowed at {rpm} rpm with the flag off")
        case Err(refused):
            assert refused == OccupancyRefused(Occupancy.OCCUPIED, refused.detail)
            assert "OCCUPANCY_OCCUPIED_ENABLED=false" in refused.detail
    assert Occupancy.OCCUPIED.label == "PERSONNE A BORD"


@pytest.mark.parametrize(
    ("max_rpm", "expected"),
    [("1380", 990), ("300", 300)],
)
def test_once_enabled_a_person_on_board_gets_the_lower_ceiling(max_rpm: str, expected: int) -> None:
    """Gr 1.2 at 1.5 m is 990 motor rpm, and MOTOR_MAX_RPM still binds below it."""
    config = _load(_with(SIM_ENV, OCCUPANCY_OCCUPIED_ENABLED="true", MOTOR_MAX_RPM=max_rpm))
    assert config.occupied_enabled is True
    assert config.medical.parameters.occupied_ceiling_g == 1.2
    assert config.ceiling_for(Occupancy.OCCUPIED) == Ok(MotorRpm(expected))


def test_the_ceiling_with_a_person_on_board_is_the_medical_file_s() -> None:
    """A lower signed ceiling lowers it: 1.1 g is about 788 motor rpm at 1.5 m, not 990."""
    lower = signed(replace(SHIPPED_PARAMETERS, occupied_ceiling_g=ResultantG(1.1)))
    config = _load(_with(SIM_ENV, OCCUPANCY_OCCUPIED_ENABLED="true", MOTOR_MAX_RPM="1380"), lower)
    ceiling = config.ceiling_for(Occupancy.OCCUPIED)
    assert ceiling == Ok(config.geometry.motor_rpm_for(ResultantG(1.1)))
    assert isinstance(ceiling, Ok)
    assert ceiling.value < 990


@pytest.mark.parametrize(("text", "expected"), [("1", True), ("YES", True), ("off", False)])
def test_the_occupancy_flag_spellings(text: str, expected: bool) -> None:
    assert _load(_with(SIM_ENV, OCCUPANCY_OCCUPIED_ENABLED=text)).occupied_enabled is expected


def test_an_ambiguous_occupancy_flag_is_refused() -> None:
    assert _keys(_with(SIM_ENV, OCCUPANCY_OCCUPIED_ENABLED="maybe")) == {
        "OCCUPANCY_OCCUPIED_ENABLED"
    }


# =========================================================================
# Sources
# =========================================================================


@pytest.mark.parametrize("key", ["MOTOR_BACKEND", "ECG_SOURCE"])
def test_the_backend_and_the_ecg_source_are_explicit_choices(key: str) -> None:
    assert _keys(_without(SIM_ENV, key)) == {key}
    assert _keys(_with(SIM_ENV, **{key: "usb"})) == {key}


@pytest.mark.parametrize(
    "address", ["", "98-d3-91-fe-4e-9f", "rfcomm:98:d3:91:fe:4e:9f", "rfcomm:98-d3-91"]
)
def test_rfcomm_needs_an_rfcomm_address(address: str) -> None:
    assert _keys(_with(SIM_ENV, ECG_SOURCE="rfcomm", BITALINO_ADDRESS=address)) == {
        "BITALINO_ADDRESS"
    }


def test_an_rfcomm_address_is_normalised_to_lower_case() -> None:
    config = _load(_with(SIM_ENV, ECG_SOURCE="RFCOMM", BITALINO_ADDRESS="rfcomm:98-D3-91-FE-4E-9F"))
    assert config.bitalino_address == "rfcomm:98-d3-91-fe-4e-9f"


@pytest.mark.parametrize("address", ["", "rfcomm:98-d3-91-fe-4e-9f"])
def test_serial_needs_a_serial_address(address: str) -> None:
    assert _keys(_with(SIM_ENV, ECG_SOURCE="serial", BITALINO_ADDRESS=address)) == {
        "BITALINO_ADDRESS"
    }


def test_serial_accepts_a_device_node() -> None:
    config = _load(_with(SIM_ENV, ECG_SOURCE="serial", BITALINO_ADDRESS="/dev/rfcomm0"))
    assert config.ecg_source is EcgSource.SERIAL
    assert config.bitalino_address == "/dev/rfcomm0"


# =========================================================================
# The drive link
# =========================================================================


@pytest.mark.parametrize(
    ("changes", "key"),
    [
        ({"MOTOR_SLAVE_ID": "0"}, "MOTOR_*"),
        ({"MOTOR_SLAVE_ID": "abc"}, "MOTOR_SLAVE_ID"),
        ({"MODBUS_BAUDRATE": "fast"}, "MODBUS_BAUDRATE"),
        ({"MODBUS_PARITY": "X"}, "MODBUS_PARITY"),
        ({"MODBUS_TIMEOUT_S": "soon"}, "MODBUS_TIMEOUT_S"),
        ({"MODBUS_TIMEOUT_S": "0"}, "MOTOR_*"),
        ({"MOTOR_REG_OFFSET": "one"}, "MOTOR_REG_OFFSET"),
        ({"MOTOR_REG_OFFSET": "40"}, "MOTOR_*"),
    ],
)
def test_a_bad_link_setting_is_named(changes: dict[str, str], key: str) -> None:
    assert _keys(_with(BENCH_ENV, **changes)) == {key}


def test_the_link_is_not_parsed_for_the_simulated_drive() -> None:
    """A dry run must not fail on a bench-only key it never uses."""
    assert _load(_with(SIM_ENV, MOTOR_SLAVE_ID="abc")).drive_link is None


def test_lower_case_parity_is_accepted() -> None:
    link = _load(_with(BENCH_ENV, MODBUS_PARITY="n")).drive_link
    assert link is not None
    assert link.settings.parity is Parity.NONE


def test_the_scripts_share_the_same_parser() -> None:
    match build_drive_link(
        port="COM3", slave="0xF8", baud="19200", parity="E", timeout="0.2", offset="0"
    ):
        case Ok(link):
            assert isinstance(link, DriveLink)
            assert link.settings.slave_address == 248
            assert link.settings.timeout == 0.2
        case Err(problem):
            pytest.fail(str(problem))


# =========================================================================
# The web bind
# =========================================================================


def test_the_bench_console_port_is_refused() -> None:
    assert _keys(_with(SIM_ENV, UI_PORT=str(BENCH_CONSOLE_PORT))) == {"UI_PORT"}


def test_a_non_numeric_port_is_refused() -> None:
    assert _keys(_with(SIM_ENV, UI_PORT="http")) == {"UI_PORT"}


def test_a_network_bind_without_a_token_is_refused() -> None:
    keys = _keys(_with(SIM_ENV, UI_HOST="0.0.0.0"))  # noqa: S104  # the refusal is the point
    # And, since ANH-151, without a certificate: both are said at once
    # (tests/test_console_tls.py has the certificate's own tests).
    assert keys == {"UI_HOST/UI_PORT/UI_TOKEN", "UI_HOST/UI_TLS_CERT/UI_TLS_KEY"}


def test_every_problem_is_reported_at_once() -> None:
    """An operator fixes a .env in one go, not one line per restart."""
    assert _keys({"MOTOR_BACKEND": "x", "ECG_SOURCE": "y", "MOTOR_MAX_RPM": "9999"}) == {
        "MOTOR_BACKEND",
        "ECG_SOURCE",
        "ARM_RADIUS_M",
        "MOTOR_MAX_RPM",
    }


def test_a_serial_backend_with_a_bad_link_is_refused_even_if_the_rest_is_fine() -> None:
    assert _keys(_with(BENCH_ENV, MOTOR_SLAVE_ID="abc")) == {"MOTOR_SLAVE_ID"}


# =========================================================================
# Programmes, cardiac tiers and the dashboard link
# =========================================================================


def test_programmes_tiers_and_the_link_default_to_off() -> None:
    config = _load(SIM_ENV)
    assert not config.programs_enabled
    assert config.tiers == CardiacTiers(hard_max_bpm=Bpm(148), critical_bpm=Bpm(158))
    assert config.cloud is None


def test_programmes_are_a_flag() -> None:
    assert _load(_with(SIM_ENV, PROGRAMS_ENABLED="true")).programs_enabled
    assert _keys(_with(SIM_ENV, PROGRAMS_ENABLED="maybe")) == {"PROGRAMS_ENABLED"}


# =========================================================================
# The medical parameters (ANH-139): one signed file, no key
# =========================================================================

MEDICAL: Final[str] = "config/medical_parameters.json"


def test_the_tiers_and_the_minimum_age_are_the_medical_file_s() -> None:
    """Today's values, read from the shipped file: 148 / 158 bpm, 18 years."""
    config = _load(SIM_ENV)
    assert config.tiers == CardiacTiers(
        hard_max_bpm=SHIPPED_PARAMETERS.hr_hard_max_bpm,
        critical_bpm=SHIPPED_PARAMETERS.hr_critical_bpm,
    )
    assert config.min_rider_age == SHIPPED_PARAMETERS.min_rider_age == 18
    screened = signed(
        replace(SHIPPED_PARAMETERS, hr_hard_max_bpm=Bpm(165), hr_critical_bpm=Bpm(175))
    )
    assert _load(SIM_ENV, screened).tiers == CardiacTiers(Bpm(165), Bpm(175))
    assert _load(SIM_ENV, signed(replace(SHIPPED_PARAMETERS, min_rider_age=16))).min_rider_age == 16


@pytest.mark.parametrize("key", sorted(MEDICAL_KEYS))
def test_a_key_that_used_to_set_a_medical_value_stops_the_console(key: str) -> None:
    """Two sources for one medical value is how the unsigned one gets applied."""
    problems = _problems(_with(SIM_ENV, **{key: "148"}))
    assert [problem.key for problem in problems] == [key]
    assert MEDICAL in problems[0].detail
    assert MEDICAL_KEYS[key] in problems[0].detail


def test_a_blank_medical_key_sets_nothing_and_is_not_refused() -> None:
    """As dotenv leaves ``HR_HARD_MAX_BPM=``: no value, no second source."""
    assert _load(_with(SIM_ENV, **dict.fromkeys(MEDICAL_KEYS, "  "))).medical.signed


@pytest.mark.parametrize(
    ("hard", "critical", "members"),
    [
        (99, 158, {"hr_hard_max_bpm"}),
        (148, 221, {"hr_critical_bpm"}),
        (90, 230, {"hr_hard_max_bpm", "hr_critical_bpm"}),
    ],
)
def test_signed_tiers_outside_the_believable_heart_rates_are_refused(
    hard: int, critical: int, members: set[str]
) -> None:
    """The bounds the ``.env`` keys had: a signature does not make 99 bpm a typo-free number."""
    medical = signed(
        replace(SHIPPED_PARAMETERS, hr_hard_max_bpm=Bpm(hard), hr_critical_bpm=Bpm(critical))
    )
    assert _keys(SIM_ENV, medical) == {f"{MEDICAL}:{member}" for member in members}


@pytest.mark.parametrize("age", [5, 101])
def test_a_signed_minimum_age_the_formula_does_not_hold_for_is_refused(age: int) -> None:
    medical = signed(replace(SHIPPED_PARAMETERS, min_rider_age=age))
    assert _keys(SIM_ENV, medical) == {f"{MEDICAL}:min_rider_age"}


def test_signed_motion_limits_outside_the_limiter_s_bounds_are_refused_with_the_rest() -> None:
    """Every problem at once: the motion limits and a tier, and the .env's own."""
    motion = replace(SHIPPED_PARAMETERS.motion_limits, g_per_s=GLoadPerSecond(0.9))
    medical = signed(replace(SHIPPED_PARAMETERS, motion_limits=motion, hr_hard_max_bpm=Bpm(99)))
    problems = _problems(_with(SIM_ENV, MIN_RIDER_AGE="16"), medical)
    assert {problem.key for problem in problems} == {
        "MIN_RIDER_AGE",
        f"{MEDICAL}:hr_hard_max_bpm",
        f"{MEDICAL}:motion_limits",
    }
    assert any("g_rate" in problem.detail for problem in problems)


def test_unsigned_parameters_start_the_console_in_bench_mode_only() -> None:
    """Whatever the .env says: no programme, nobody on board, and the reason said."""
    config = _load(
        _with(SIM_ENV, PROGRAMS_ENABLED="true", OCCUPANCY_OCCUPIED_ENABLED="true"), unsigned()
    )
    assert not config.medical.signed
    assert config.programs_enabled is False
    assert config.occupied_enabled is False
    assert config.ceiling_for(Occupancy.BENCH) == Ok(DEFAULT_MOTOR_MAX_RPM)
    refused = config.ceiling_for(Occupancy.OCCUPIED)
    assert isinstance(refused, Err)
    assert refused.error.detail.startswith(
        "personne a bord refusee : parametres medicaux non signes"
    )
    # The values still govern the bench: the motion limits are the file's.
    assert config.motion == DEFAULT_MOTION_LIMITS


def test_signed_parameters_leave_both_flags_to_the_env() -> None:
    config = _load(_with(SIM_ENV, PROGRAMS_ENABLED="true", OCCUPANCY_OCCUPIED_ENABLED="true"))
    assert config.programs_enabled is True
    assert config.occupied_enabled is True


@pytest.mark.parametrize("flag", ["programs_enabled", "occupied_enabled"])
def test_no_configuration_lets_unsigned_parameters_near_a_person(flag: str) -> None:
    """The bench mode is a property of every LocalConfig, not of one function."""
    bench = _load(SIM_ENV, unsigned())

    def enabled() -> LocalConfig:
        if flag == "programs_enabled":
            return replace(bench, programs_enabled=True)
        return replace(bench, occupied_enabled=True)

    with pytest.raises(ValueError, match="parametres medicaux non signes"):
        enabled()


def test_no_key_no_link_even_with_the_example_url() -> None:
    """The shipped .env.example has a placeholder URL and a blank key: still unlinked."""
    config = _load(_with(SIM_ENV, CONVEX_URL="https://your-deployment.convex.site"))
    assert config.cloud is None


@pytest.mark.parametrize(
    "url", ["https://abc.convex.site/", "http://localhost:3211", "http://127.0.0.1:3211"]
)
def test_a_key_links_to_an_https_or_local_url(url: str) -> None:
    config = _load(_with(SIM_ENV, MACHINE_API_KEY="secret-key", CONVEX_URL=url))
    assert config.cloud == CloudConfig(url=url.rstrip("/"), api_key="secret-key")
    assert "secret-key" not in repr(config.cloud)


@pytest.mark.parametrize("url", ["", "http://abc.convex.site", "abc.convex.site"])
def test_a_key_with_no_url_or_plain_http_is_refused(url: str) -> None:
    assert _keys(_with(SIM_ENV, MACHINE_API_KEY="k", CONVEX_URL=url)) == {"CONVEX_URL"}


def test_the_leg_tip_radius_is_optional_and_never_inside_the_arm() -> None:
    assert _load(SIM_ENV).leg_tip_radius is None
    assert _load(_with(SIM_ENV, LEG_TIP_RADIUS_M="2.43")).leg_tip_radius == pytest.approx(2.43)
    assert _keys(_with(SIM_ENV, LEG_TIP_RADIUS_M="1.2")) == {"LEG_TIP_RADIUS_M"}
    assert _keys(_with(SIM_ENV, LEG_TIP_RADIUS_M="9")) == {"LEG_TIP_RADIUS_M"}
    assert _keys(_with(SIM_ENV, LEG_TIP_RADIUS_M="loin")) == {"LEG_TIP_RADIUS_M"}
    # With the arm radius itself refused, only the plausibility bound applies.
    assert _keys(_with(SIM_ENV, ARM_RADIUS_M="-1", LEG_TIP_RADIUS_M="2.0")) == {"ARM_RADIUS_M"}
    assert _keys(_with(SIM_ENV, ARM_RADIUS_M="-1", LEG_TIP_RADIUS_M="0")) == {
        "ARM_RADIUS_M",
        "LEG_TIP_RADIUS_M",
    }


# =========================================================================
# Exhaustiveness guards
# =========================================================================


def test_an_occupancy_outside_the_enum_fails_loudly() -> None:
    """The ``assert_never`` guard: a new member must be handled, not fall through."""
    config = _load(SIM_ENV)
    with pytest.raises(AssertionError):
        config.ceiling_for(cast("Occupancy", "cockpit"))


def test_an_ecg_source_outside_the_enum_fails_loudly() -> None:
    with pytest.raises(AssertionError):
        bitalino_address({}, cast("EcgSource", "usb"))

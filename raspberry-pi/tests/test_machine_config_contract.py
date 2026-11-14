"""The keys this console takes from the dashboard are those of the shared contract (ANH-141).

``contracts/machine-config.json`` is the one list of the keys the site
configures: their family, type, bounds and default. The console's image does
not carry it, so :data:`src.machine_config.KEYS` is its copy, and these tests
hold the two equal, with the bounds :mod:`src.local_config` enforces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, cast

from src import local_config
from src.local_config import (
    BENCH_CONSOLE_PORT,
    MAX_ARM_RADIUS_M,
    MAX_RECORD_RETENTION_DAYS,
    MEDICAL_KEYS,
    RETIRED_KEYS,
)
from src.machine_config import CLOUD_ENV_KEYS, KEYS, TYPE_WORDS, ValueType
from src.motor.atv320 import MODBUS_SLAVE_MIN, SCHNEIDER_POINT_TO_POINT_ADDRESS
from src.motor.drive import REGISTER_OFFSET_MAX, REGISTER_OFFSET_MIN
from src.training.plan import NAMEPLATE_MOTOR_RPM
from src.web.deps import MAX_TCP_PORT, MIN_TCP_PORT

CONTRACT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "contracts" / "machine-config.json"
)


def _contract() -> dict[str, object]:
    decoded: object = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]  # narrowed below
    assert isinstance(decoded, dict)
    return cast("dict[str, object]", decoded)


CONTRACT: Final[dict[str, object]] = _contract()
SHARED_KEYS: Final[dict[str, dict[str, object]]] = cast(
    "dict[str, dict[str, object]]", CONTRACT["keys"]
)


def _shared(key: str, field: str) -> object:
    return SHARED_KEYS[key].get(field)


def test_the_keys_are_the_contract_s_in_its_order() -> None:
    assert list(KEYS) == list(SHARED_KEYS)


def test_every_key_has_the_contract_s_family_type_env_and_nullability() -> None:
    for key, spec in KEYS.items():
        assert _shared(key, "family") == spec.family.value, key
        assert _shared(key, "type") == spec.kind.value, key
        assert _shared(key, "env") == spec.env, key
        assert _shared(key, "nullable") == spec.nullable, key
        assert _shared(key, "values") == (list(spec.values) if spec.values else None), key
        assert _shared(key, "max_chars") == (spec.max_chars or None), key
        required = None if spec.requires is None else spec.requires.value
        assert _shared(key, "requires_level") == required, key


def test_every_default_is_the_console_s_own() -> None:
    for key, spec in KEYS.items():
        default = spec.default
        assert _shared(key, "default") == (list(default) if isinstance(default, tuple) else default)


def test_every_type_of_the_contract_is_one_this_console_reads_and_names() -> None:
    types = cast("dict[str, str]", CONTRACT["types"])
    assert set(types) == {kind.value for kind in ValueType}
    assert set(TYPE_WORDS) == set(ValueType)


def test_the_bounds_are_those_the_console_enforces() -> None:
    assert _shared("MOTOR_MAX_RPM", "min") == 0
    assert _shared("MOTOR_MAX_RPM", "max") == NAMEPLATE_MOTOR_RPM
    assert _shared("ARM_RADIUS_M", "min_exclusive") == 0
    assert _shared("ARM_RADIUS_M", "max") == MAX_ARM_RADIUS_M
    assert _shared("LEG_TIP_RADIUS_M", "max") == MAX_ARM_RADIUS_M
    assert _shared("LEG_TIP_RADIUS_M", "min_key") == "ARM_RADIUS_M"
    assert _shared("RECORD_LOCAL_RETENTION_DAYS", "min") == 0
    assert _shared("RECORD_LOCAL_RETENTION_DAYS", "max") == MAX_RECORD_RETENTION_DAYS
    assert _shared("UI_PORT", "min") == MIN_TCP_PORT
    assert _shared("UI_PORT", "max") == MAX_TCP_PORT
    assert _shared("UI_PORT", "not") == [BENCH_CONSOLE_PORT]
    assert _shared("MOTOR_REG_OFFSET", "min") == REGISTER_OFFSET_MIN
    assert _shared("MOTOR_REG_OFFSET", "max") == REGISTER_OFFSET_MAX
    # 1..247 and Schneider's point-to-point 248: one range, 0 (broadcast) left out.
    assert _shared("MOTOR_SLAVE_ID", "min") == MODBUS_SLAVE_MIN
    assert _shared("MOTOR_SLAVE_ID", "max") == SCHNEIDER_POINT_TO_POINT_ADDRESS
    assert _shared("MODBUS_TIMEOUT_S", "min_exclusive") == 0
    assert _shared("GEAR_RATIO", "min_exclusive") == 0
    assert _shared("SENSORS", "required_value") == "ECG"


def test_every_key_the_console_reads_is_either_the_dashboard_s_or_local() -> None:
    """Nothing the console reads is left out of the contract, and nothing is in both lists."""
    read: set[str] = set()
    for name in dir(local_config):
        value: object = getattr(local_config, name)  # pyright: ignore[reportAny]  # narrowed next
        if name.startswith("KEY_") and isinstance(value, str):
            read.add(value)
    local = set(cast("dict[str, str]", CONTRACT["local_keys"]))
    assert not local & CLOUD_ENV_KEYS
    assert read - set(MEDICAL_KEYS) - set(RETIRED_KEYS) == local | CLOUD_ENV_KEYS
    assert {"MACHINE_API_KEY", "CONVEX_URL", "UI_HOST", "DEVICE_TOKEN", "UI_TOKEN"} <= local

"""Seeded random proposals, judged the same here and on the dashboard (ANH-141).

``contracts/machine-config-random-cases.json`` holds :data:`COUNT` proposals
drawn with :data:`SEED` by :func:`draw`, and the verdict :func:`review` gives
each: the problems, in their order and words. This test draws them again and
requires the file to be exactly that, so the file cannot drift from the draw,
and ``convex/machineConfigs.test.ts`` requires the same verdicts of
``convex/lib/machineConfig.ts``. A key refused for its type and then judged
again by one side only is the kind of difference this catches.

To draw them again after a change of the rules:
``python -m tests.test_machine_config_random`` from ``raspberry-pi/``.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Final

from src.machine_config import KEYS, ValidationLevel, review
from src.result import Ok
from tests.medical_support import signed
from tests.test_machine_config_parity import CASES

SEED: Final[int] = 141
COUNT: Final[int] = 300
PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "contracts" / "machine-config-random-cases.json"
)

POOL: Final[tuple[object, ...]] = (
    None, True, False, 0, -1, 1, 1.5, -0.0, 248, 249, 0.1, 300.0, 1380, 1381, 5, 5.5, 2.43,
    8123, 70000, 3650, 3651, 50, 49.79, -0.00001, 1e21, 10**400, "", " x", "sim", "serial",
    "rfcomm", "SIM", "E", "N", "e", "none", "sim_empty", "ftdi://schneider:rs485/1", "COM3",
    "98 D3", "rfcomm:98-D3-91-FE-4E-9F", "98:D3:91:FE:4E:9F", "/dev/rfcomm0", ["ECG"], ["EDA"],
    [], ["ECG", "ECG"], ["ECG", "SpO2"], "Banc", {"a": 1},
)  # fmt: skip
"""Values of every kind: valid, at a bound, out of bounds, of another type."""

PAIRS: Final[tuple[tuple[str, tuple[object, ...], tuple[str, ...]], ...]] = (
    ("ECG_SOURCE", ("serial", "rfcomm"), ("BITALINO_ADDRESS",)),
    ("MOTOR_BACKEND", ("serial",), ("MOTOR_SLAVE_ID", "MODBUS_TIMEOUT_S", "MOTOR_REG_OFFSET")),
    ("ARM_RADIUS_M", (1.5, 15, "1.5", None), ("LEG_TIP_RADIUS_M",)),
)
"""Keys whose value decides how others are judged: the console judges those others with
it, and leaves out what it would say of one it already refused for its type."""


def draw() -> list[dict[str, object]]:
    """The proposals, each with the verdict of this console."""
    chance = random.Random(SEED)  # noqa: S311  # test data, not a secret
    names = list(KEYS)
    levels = [level.value for level in ValidationLevel]
    drawn: list[dict[str, object]] = []
    for index in range(COUNT):
        changes = {key: chance.choice(POOL) for key in chance.sample(names, chance.randint(1, 4))}
        # A key that decides how another is judged, with that other key drawn too.
        pair = chance.choice(PAIRS)
        if chance.random() < 0.4:
            changes[pair[0]] = chance.choice(pair[1])
            changes.update({key: chance.choice(POOL) for key in pair[2]})
        if chance.random() < 0.05:
            changes["EXTRA"] = 1
        remove = [chance.choice(names)] if chance.random() < 0.05 else []
        level = chance.choice(levels)
        document = {**CASES["base"], **changes}
        for key in remove:
            document.pop(key, None)
        reviewed = review(document, {}, ValidationLevel(level), signed())
        problems = (
            []
            if isinstance(reviewed, Ok)
            else [{"key": problem.key, "detail": problem.detail} for problem in reviewed.error]
        )
        drawn.append(
            {"name": f"tirage {index}", "changes": changes, "remove": remove, "level": level,
             "problems": problems}
        )  # fmt: skip
    return drawn


def text() -> str:
    """The file as it is committed: one proposal per line."""
    lines = ",\n".join(f"  {json.dumps(case, ensure_ascii=False)}" for case in draw())
    return f"[\n{lines}\n]\n"


def test_the_shared_random_proposals_are_the_draw_and_its_verdicts() -> None:
    assert PATH.read_text(encoding="utf-8") == text()


def test_the_draw_refuses_and_accepts() -> None:
    verdicts = {bool(case["problems"]) for case in draw()}
    assert verdicts == {True, False}


if __name__ == "__main__":
    _ = PATH.write_text(text(), encoding="utf-8")

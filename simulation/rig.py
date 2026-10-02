"""The rig as the simulation sees it: the CAD-derived geometry plus the rider parameters.

``src.geometry.MachineGeometry`` is the one record the runtime renders every
speed through, and it carries ONE radius: the "axis to the occupant" radius at
which the runtime quotes g and applies the anti-nausea g-dot limit. This module
does not replace it. It wraps it with the one number the runtime does not know
and the operator cares about most: the **leg-tip radius**, the farthest point of
the rider from the axis, where the centripetal load is highest.

Both radii come from ``cad/machine_geometry.json`` (written by
``cad/extract_geometry.py``), and both are flagged there as parameters that must
be measured: the rider is not modelled in the CAD. A scenario may override
either one; nothing here invents a value the file does not state.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from simulation.jsondoc import as_mapping, load_object
from src.geometry import MachineGeometry
from src.result import Err, Ok, Result
from src.units import GLoad, Metres, OutputRpm, ResultantG, output_rpm_to_g, resultant_g

GEOMETRY_PATH: Final[Path] = Path(__file__).resolve().parent / "cad" / "machine_geometry.json"
"""The extracted CAD geometry, committed next to the script that produces it."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RigGeometry:
    """The machine as rendered by the runtime, plus where the rider's leg tip is.

    ``machine.radius`` is the reference radius (the runtime's ``ARM_RADIUS_M``);
    ``leg_tip_radius`` is the farthest point of the rider. They are distinct on
    purpose: g goes linearly with radius, so a g quoted at the reference radius
    understates the load at the feet by ``leg_tip_radius / radius``.
    """

    machine: MachineGeometry
    leg_tip_radius: Metres
    arm_tip_radius: Metres
    """Outboard end of the arm beams (CAD), for the 2D view."""

    capsule_near_radius: Metres
    """Inner surface of the capsule's inboard wall, on the far side of the axis (CAD)."""

    capsule_far_radius: Metres
    """Inner surface of the capsule's outboard wall (CAD): the leg-tip upper bound."""

    counterweight_radius: Metres
    """Counterweight centroid, on the inboard side (CAD), for the 2D view."""

    leg_tip_measured: bool
    """``False`` while the leg-tip radius is the CAD upper bound or an estimate."""

    def __post_init__(self) -> None:
        for name, value in (
            ("leg_tip_radius", float(self.leg_tip_radius)),
            ("arm_tip_radius", float(self.arm_tip_radius)),
            ("capsule_near_radius", float(self.capsule_near_radius)),
            ("capsule_far_radius", float(self.capsule_far_radius)),
            ("counterweight_radius", float(self.counterweight_radius)),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be positive and finite, got {value}")

    @property
    def reference_radius(self) -> Metres:
        """Where the runtime quotes g: ``machine.radius``."""
        return self.machine.radius

    def g_reference(self, rpm: OutputRpm) -> GLoad:
        """Centripetal g at the reference radius."""
        return output_rpm_to_g(rpm, self.machine.radius)

    def g_leg_tip(self, rpm: OutputRpm) -> GLoad:
        """Centripetal g at the leg tip: the highest load on the rider."""
        return output_rpm_to_g(rpm, self.leg_tip_radius)

    def resultant_leg_tip(self, rpm: OutputRpm) -> ResultantG:
        """What the rider feels at the leg tip, gravity included."""
        return resultant_g(self.g_leg_tip(rpm))


@dataclass(frozen=True, slots=True)
class GeometryUnusable:
    """The geometry file is missing, malformed, or states a non-physical number."""

    detail: str


def _number(document: object, *path: str) -> Result[float, GeometryUnusable]:
    """The finite number at ``path`` inside a JSON document, or why not."""
    node = document
    for key in path:
        mapping = as_mapping(node)
        if mapping is None or key not in mapping:
            return Err(GeometryUnusable(f"missing {'.'.join(path)}"))
        node = mapping[key]
    if isinstance(node, bool) or not isinstance(node, int | float):
        return Err(GeometryUnusable(f"{'.'.join(path)} is not a number"))
    value = float(node)
    if not math.isfinite(value) or value <= 0.0:
        return Err(GeometryUnusable(f"{'.'.join(path)} = {value} is not positive and finite"))
    return Ok(value)


def parse_geometry(
    raw: str,
    *,
    reference_radius: Metres | None = None,
    leg_tip_radius: Metres | None = None,
) -> Result[RigGeometry, GeometryUnusable]:
    """Build the rig from the JSON text, with optional scenario overrides."""
    document = load_object(raw)
    if isinstance(document, str):
        return Err(GeometryUnusable(document))
    fields = {
        "reference": ("derived", "reference_radius_m", "value"),
        "leg_tip": ("derived", "leg_tip_radius_m", "value"),
        "arm_tip": ("arm", "outboard_tip_radius_m"),
        "near": ("capsule", "near_wall_inner_radius_m"),
        "far": ("capsule", "far_wall_inner_radius_max_m"),
        "counterweight": ("arm", "counterweight_centroid_radius_m"),
    }
    values: dict[str, float] = {}
    for name, path in fields.items():
        number = _number(document, *path)
        if isinstance(number, Err):
            return number
        values[name] = number.value
    reference = values["reference"] if reference_radius is None else float(reference_radius)
    leg_tip = values["leg_tip"] if leg_tip_radius is None else float(leg_tip_radius)
    try:
        return Ok(
            RigGeometry(
                machine=MachineGeometry(radius=Metres(reference)),
                leg_tip_radius=Metres(leg_tip),
                arm_tip_radius=Metres(values["arm_tip"]),
                capsule_near_radius=Metres(values["near"]),
                capsule_far_radius=Metres(values["far"]),
                counterweight_radius=Metres(values["counterweight"]),
                leg_tip_measured=False,
            )
        )
    except ValueError as error:
        return Err(GeometryUnusable(str(error)))


def load_geometry(
    path: Path = GEOMETRY_PATH,
    *,
    reference_radius: Metres | None = None,
    leg_tip_radius: Metres | None = None,
) -> Result[RigGeometry, GeometryUnusable]:
    """Read and parse the geometry file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        return Err(GeometryUnusable(f"cannot read {path}: {error}"))
    return parse_geometry(raw, reference_radius=reference_radius, leg_tip_radius=leg_tip_radius)

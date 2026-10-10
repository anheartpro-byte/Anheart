"""Extract the geometry that matters for g-load from the Gaura assembly STEP file.

Run it in the CAD venv (cadquery-ocp is ~300 MB, so it is kept out of the
project venv)::

    cd simulation/cad
    uv venv --python 3.12 .venv-cad && uv pip install --python .venv-cad/bin/python -r requirements.txt
    .venv-cad/bin/python extract_geometry.py ../../CAO/Gaura_Assy_2907.STEP machine_geometry.json

What it does, and why each step is there
----------------------------------------

1. Reads the STEP through OCCT's XCAF reader, so every part carries its
   assembly placement (NEXT_ASSEMBLY_USAGE_OCCURRENCE + its transform) and its
   PRODUCT name. Nothing is inferred from raw CARTESIAN_POINT soup.
2. Finds the **rotation axis** from the main shaft (the part named ``AXE KZBF45``)
   and checks it against the two radial bearings and the thrust bearing: all
   four must be coaxial, or the axis is reported as unconfirmed.
3. Finds the **arm** (the two ``Profile Polyester`` beams) and measures its
   reach on both sides of the axis along its own length direction.
4. Finds the **capsule** the occupant lies in (``Human_Capsule_v2``) and
   ray-casts along the arm direction to find the inner surface of its far
   (outboard) wall at several heights above its floor: the farthest point any
   part of a person inside it can reach.
5. Looks for a modelled person. There is none in this file (checked by product
   name, reported), so the leg-tip radius is emitted as a **parameter** with a
   conservative default and a flag saying it must be measured.

Every number in the output names the part(s) it came from. Length units are
read from the file (millimetres in this STEP) and converted to metres.

This is a script, not part of the typed package: it is excluded from the
strict type checks for the same reason ``raspberry-pi/scripts/`` is (OCP ships
no stubs usable under zero-Any), and it touches nothing on the motor path.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepGProp import BRepGProp
from OCP.collections import Sequence_TDF_Label
from OCP.gp import gp_Dir, gp_Lin, gp_Pnt
from OCP.GProp import GProp_GProps
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label
from OCP.TDocStd import TDocStd_Document
from OCP.TopoDS import TopoDS_Shape
from OCP.XCAFDoc import XCAFDoc_DocumentTool

MM_PER_M = 1000.0
COAXIAL_TOLERANCE_MM = 0.5
HUMAN_NAME_HINTS = ("mannequin", "human_body", "body", "person", "occupant", "pilot", "dummy_human")

# Default stature for the stature-based leg-tip estimate. NOT from the CAD.
ASSUMED_STATURE_M = 1.75


@dataclass(frozen=True, slots=True)
class Part:
    """One placed part: its occurrence id, its product name, its world shape."""

    occurrence: str
    product: str
    shape: TopoDS_Shape


@dataclass(frozen=True, slots=True)
class Box:
    xmin: float
    ymin: float
    zmin: float
    xmax: float
    ymax: float
    zmax: float

    def centre(self) -> tuple[float, float, float]:
        return (
            (self.xmin + self.xmax) / 2,
            (self.ymin + self.ymax) / 2,
            (self.zmin + self.zmax) / 2,
        )

    def extents(self) -> tuple[float, float, float]:
        return (self.xmax - self.xmin, self.ymax - self.ymin, self.zmax - self.zmin)

    def as_list(self) -> list[float]:
        return [
            round(v, 2) for v in (self.xmin, self.ymin, self.zmin, self.xmax, self.ymax, self.zmax)
        ]


def _name(label: TDF_Label) -> str:
    attribute = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attribute):
        return str(attribute.Get().ToExtString())
    return "?"


def _box(shape: TopoDS_Shape) -> Box:
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, True)
    low, high = box.CornerMin(), box.CornerMax()
    return Box(low.X(), low.Y(), low.Z(), high.X(), high.Y(), high.Z())


def _volume_centre(shape: TopoDS_Shape) -> tuple[float, float, float]:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    centre = props.CentreOfMass()
    return (centre.X(), centre.Y(), centre.Z())


def _length_unit_to_mm(raw: str) -> float:
    """The file's length unit, as a factor to millimetres. Refuses anything unrecognised."""
    if "SI_UNIT ( .MILLI., .METRE. )" in raw or "SI_UNIT(.MILLI.,.METRE.)" in raw:
        return 1.0
    if "SI_UNIT ( $, .METRE. )" in raw or "SI_UNIT($,.METRE.)" in raw:
        return 1000.0
    raise SystemExit("unrecognised STEP length unit: refusing to guess")


def load_parts(path: Path) -> tuple[list[Part], str]:
    """Every top-level occurrence of the root assembly, placed in world coordinates."""
    document = TDocStd_Document(TCollection_ExtendedString("gaura"))
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    status = reader.ReadFile(str(path))
    if "RetDone" not in str(status):
        raise SystemExit(f"STEP read failed: {status}")
    reader.Transfer(document)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    roots = Sequence_TDF_Label()
    tool.GetFreeShapes(roots)
    if roots.Length() != 1:
        raise SystemExit(f"expected one root assembly, found {roots.Length()}")
    root = roots.Value(1)
    parts: list[Part] = []
    stack: list[tuple[TDF_Label, object]] = []
    components = Sequence_TDF_Label()
    tool.GetComponents_s(root, components)
    for index in range(1, components.Length() + 1):
        stack.append((components.Value(index), None))
    while stack:
        label, parent_location = stack.pop()
        referred = TDF_Label()
        tool.GetReferredShape_s(label, referred)
        location = tool.GetLocation_s(label)
        if parent_location is not None:
            location = parent_location.Multiplied(location)  # type: ignore[attr-defined]
        if tool.IsAssembly_s(referred):
            children = Sequence_TDF_Label()
            tool.GetComponents_s(referred, children)
            for index in range(1, children.Length() + 1):
                stack.append((children.Value(index), location))
            continue
        shape = tool.GetShape_s(referred).Moved(location)
        parts.append(Part(occurrence=_name(label), product=_name(referred), shape=shape))
    return parts, _name(root)


def _only(parts: list[Part], needle: str) -> list[Part]:
    found = [part for part in parts if needle.lower() in part.product.lower()]
    if not found:
        raise SystemExit(f"no part named like {needle!r}")
    return found


def _ray_hits(
    shape: TopoDS_Shape, origin: tuple[float, float, float], direction: tuple[float, float, float]
) -> list[float]:
    """Signed parameters along the ray (in mm) where it crosses the shape's faces."""
    intersector = IntCurvesFace_ShapeIntersector()
    intersector.Load(shape, 1e-3)
    line = gp_Lin(gp_Pnt(*origin), gp_Dir(*direction))
    intersector.Perform(line, -1e9, 1e9)
    return sorted(intersector.WParameter(i) for i in range(1, intersector.NbPnt() + 1))


def main(step: Path, out: Path) -> None:
    raw_head = step.read_bytes()
    digest = hashlib.sha256(raw_head).hexdigest()
    unit = _length_unit_to_mm(raw_head[:400_000].decode("latin-1"))
    parts, root_name = load_parts(step)

    def mm(value: float) -> float:
        return value * unit

    # --- vertical: the floor plate is the thin, widest part ------------------
    floor = _only(parts, "Surface anti")[0]
    floor_box = _box(floor.shape)
    extents = floor_box.extents()
    vertical_index = extents.index(min(extents))
    if vertical_index != 1:
        raise SystemExit("expected Y to be vertical (floor plate thinnest along Y)")

    # --- rotation axis: the shaft, checked against the bearings --------------
    shaft = _only(parts, "AXE KZBF45")[0]
    shaft_box = _box(shaft.shape)
    shaft_x, _, shaft_z = shaft_box.centre()
    coaxial: list[dict[str, object]] = []
    for needle in ("6009-2rs1", "81209", "Disque de Frein", "Entretoise Roulement"):
        for part in _only(parts, needle):
            cx, _, cz = _box(part.shape).centre()
            offset = math.hypot(cx - shaft_x, cz - shaft_z)
            coaxial.append(
                {
                    "part": part.product,
                    "occurrence": part.occurrence,
                    "offset_mm": round(mm(offset), 3),
                }
            )
    worst = max(float(entry["offset_mm"]) for entry in coaxial)  # type: ignore[arg-type]
    axis_confirmed = worst <= COAXIAL_TOLERANCE_MM

    # --- motoreducer ---------------------------------------------------------
    gearmotor = _only(parts, "KA37")[0]
    gearmotor_box = _box(gearmotor.shape)

    # --- arm -----------------------------------------------------------------
    beams = _only(parts, "Polyester")
    beam_boxes = [_box(beam.shape) for beam in beams]
    beam_extents = beam_boxes[0].extents()
    arm_axis_index = beam_extents.index(max(beam_extents))
    if arm_axis_index != 2:
        raise SystemExit("expected the arm beams to run along Z")
    arm_far = max(box.zmax for box in beam_boxes) - shaft_z
    arm_near = shaft_z - min(box.zmin for box in beam_boxes)
    arm_top = max(box.ymax for box in beam_boxes)

    counterweight = _only(parts, "Support Poids")[0]
    cw_centre = _volume_centre(counterweight.shape)
    wheels = _only(parts, "CGZJ50")
    wheel_radii = sorted(abs(_box(w.shape).centre()[2] - shaft_z) for w in wheels)

    # --- capsule ---------------------------------------------------------------
    capsule = _only(parts, "Human_Capsule_v2")[0]
    capsule_box = _box(capsule.shape)
    canopy = _only(parts, "Human_Capsule_Top")[0]
    canopy_box = _box(canopy.shape)
    # The floor: first two hits of a vertical ray through the arm centreline.
    floor_hits = _ray_hits(capsule.shape, (shaft_x, -5000.0, shaft_z + 1000.0), (0.0, 1.0, 0.0))
    floor_hits_y = [-5000.0 + value for value in floor_hits]
    inner_floor_y = floor_hits_y[1]
    far_wall: list[dict[str, float]] = []
    near_wall: list[dict[str, float]] = []
    heights = [inner_floor_y + d for d in (5.0, 25.0, 50.0, 100.0, 150.0, 195.0)]
    for y in heights:
        if y >= capsule_box.ymax:
            continue
        hits = _ray_hits(capsule.shape, (shaft_x, y, shaft_z - 5000.0), (0.0, 0.0, 1.0))
        zs = [shaft_z - 5000.0 + value for value in hits]
        outboard = [z for z in zs if z > shaft_z]
        inboard = [z for z in zs if z < shaft_z]
        if len(outboard) >= 2 and len(inboard) >= 2:
            far_wall.append(
                {
                    "height_above_floor_mm": round(mm(y - inner_floor_y), 1),
                    "inner_mm": round(mm(outboard[0] - shaft_z), 1),
                    "outer_mm": round(mm(outboard[-1] - shaft_z), 1),
                }
            )
            near_wall.append(
                {
                    "height_above_floor_mm": round(mm(y - inner_floor_y), 1),
                    "inner_mm": round(mm(shaft_z - inboard[-1]), 1),
                    "outer_mm": round(mm(shaft_z - inboard[0]), 1),
                }
            )
    if not far_wall:
        raise SystemExit("could not ray-cast the capsule end walls")
    far_inner_max = max(entry["inner_mm"] for entry in far_wall)
    far_inner_floor = far_wall[0]["inner_mm"]
    near_inner_min = min(entry["inner_mm"] for entry in near_wall)
    # Inner half-width at mid-length, one ray across at 100 mm above the floor.
    lateral = _ray_hits(
        capsule.shape, (shaft_x - 5000.0, inner_floor_y + 100.0, shaft_z + 1000.0), (1.0, 0.0, 0.0)
    )
    xs = [shaft_x - 5000.0 + value for value in lateral]
    inner_half_width = (xs[-2] - xs[1]) / 2 if len(xs) >= 4 else float("nan")

    # --- a modelled person? --------------------------------------------------
    products = sorted({part.product for part in parts})
    human_like = [name for name in products if any(h in name.lower() for h in HUMAN_NAME_HINTS)]
    rider_modelled = bool(human_like)

    # --- derived defaults ------------------------------------------------------
    leg_tip_upper_bound_m = far_inner_max / MM_PER_M
    stature_estimate_m = ASSUMED_STATURE_M - near_inner_min / MM_PER_M

    def m(value_mm: float) -> float:
        return round(mm(value_mm) / MM_PER_M, 4) + 0.0  # + 0.0 folds -0.0 to 0.0

    document = {
        "schema": 1,
        "units": {"length": "m", "source_length_unit": "mm" if unit == 1.0 else "m"},
        "source": {
            "file": step.name,
            "sha256": digest,
            "bytes": len(raw_head),
            "root_assembly": root_name,
            "parts_placed": len(parts),
            "distinct_products": len(products),
            "extractor": "simulation/cad/extract_geometry.py (OCCT/OCP XCAF reader, world-placed bounding boxes and ray casts)",
        },
        "frame": {
            "vertical_axis": "+Y (the floor plate 'Surface anti derapante' is thinnest along Y)",
            "arm_direction": "+Z (the 'Profile Polyester' beams are longest along Z); +Z is the capsule/outboard side",
        },
        "rotation_axis": {
            "point_m": [m(shaft_x), 0.0, m(shaft_z)],
            "direction": [0.0, 1.0, 0.0],
            "from_part": shaft.product,
            "coaxial_checks": coaxial,
            "confirmed": axis_confirmed,
            "note": f"shaft bbox {shaft_box.as_list()} mm; every bearing/brake-disc centre within {worst:.3f} mm of it",
        },
        "gearmotor": {
            "part": gearmotor.product,
            "bbox_mm": gearmotor_box.as_list(),
            "note": "SEW KA37 DRS71S4, sits under the hub on the axis",
        },
        "arm": {
            "outboard_tip_radius_m": m(arm_far),
            "inboard_tip_radius_m": m(arm_near),
            "top_of_beams_height_mm": round(mm(arm_top), 1),
            "from_parts": sorted({b.product for b in beams}),
            "note": (
                "two polyester beams, X = +/-150 mm, span Z "
                f"{min(b.zmin for b in beam_boxes):.0f}..{max(b.zmax for b in beam_boxes):.0f} mm. "
                "The product is NAMED '4000mm' but the modelled length is 3000 mm: check which is built."
            ),
            "counterweight_centroid_radius_m": m(abs(cw_centre[2] - shaft_z)),
            "counterweight_part": counterweight.product,
            "support_wheel_radii_m": [m(r) for r in wheel_radii],
            "support_wheel_part": wheels[0].product,
        },
        "capsule": {
            "part": capsule.product,
            "bbox_mm": capsule_box.as_list(),
            "inner_floor_height_mm": round(mm(inner_floor_y), 1),
            "inner_half_width_m": m(inner_half_width),
            "far_wall_profile_mm": far_wall,
            "near_wall_profile_mm": near_wall,
            "far_wall_inner_radius_at_floor_m": round(far_inner_floor / MM_PER_M, 4),
            "far_wall_inner_radius_max_m": round(far_inner_max / MM_PER_M, 4),
            "far_wall_outer_radius_m": m(capsule_box.zmax - shaft_z),
            "near_wall_inner_radius_m": round(near_inner_min / MM_PER_M, 4),
            "near_wall_is_across_the_axis": True,
            "canopy_part": canopy.product,
            "canopy_span_along_arm_mm": [
                round(mm(canopy_box.zmin - shaft_z), 1),
                round(mm(canopy_box.zmax - shaft_z), 1),
            ],
            "orientation_inference": (
                "The occupant LIES in the capsule along the arm (it is 2.8 m long, 0.9 m wide, "
                "0.2 m deep). The raised canopy covers the inboard end (over the axis), so the head "
                "is inferred to be inboard and the feet outboard. Inference from shape only: LOW-MEDIUM confidence."
            ),
        },
        "rider": {
            "modelled_in_cad": rider_modelled,
            "human_like_products": human_like,
            "note": (
                "No person is modelled in this STEP (no product named like a body/mannequin; "
                "the only 'Dummy' parts are a brake and a bearing block). Every rider position "
                "below is a PARAMETER, not a measurement."
            ),
        },
        "derived": {
            "leg_tip_radius_m": {
                "value": round(leg_tip_upper_bound_m, 4),
                "kind": "upper_bound",
                "from": "capsule far (outboard) wall, inner surface, max over heights above the floor",
                "must_be_measured": True,
                "note": (
                    "The farthest any part of a person inside the capsule can be from the axis: "
                    "the soles/toes pressed against the far wall. Conservative for g: the real "
                    "toe radius is this or less."
                ),
            },
            "leg_tip_radius_stature_estimate_m": {
                "value": round(stature_estimate_m, 4),
                "kind": "estimate",
                "from": (
                    f"head crown against the inboard (near) wall at {near_inner_min / MM_PER_M:.3f} m on the "
                    f"far side of the axis, plus an ASSUMED stature of {ASSUMED_STATURE_M} m"
                ),
                "must_be_measured": True,
            },
            "reference_radius_m": {
                "value": 1.5,
                "kind": "project_parameter",
                "from": (
                    "NOT from the CAD. ARM_RADIUS_M=1.5 is the radius raspberry-pi/ tests and the console "
                    "use as 'axis to occupant' (where the runtime quotes g and applies the g-dot limit). "
                    "There is no seat in this CAD: the occupant lies down, so 'seat radius' has no "
                    "single CAD value."
                ),
                "must_be_measured": True,
            },
        },
        "confidence": {
            "rotation_axis": "HIGH" if axis_confirmed else "LOW",
            "arm_reach": "HIGH (direct part geometry)",
            "capsule_walls": "HIGH (ray casts on the capsule solid)",
            "occupant_orientation": "LOW-MEDIUM (inferred from the canopy position)",
            "leg_tip_radius": "UPPER BOUND ONLY - the rider is not in the CAD",
            "reference_radius": "NONE from CAD - project parameter",
        },
    }
    out.write_text(json.dumps(document, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(
        f"wrote {out}: axis confirmed={axis_confirmed}, arm tip {m(arm_far)} m, "
        f"capsule far wall inner <= {far_inner_max / MM_PER_M:.3f} m, rider modelled={rider_modelled}"
    )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: extract_geometry.py <assembly.STEP> <out.json>")
    main(Path(sys.argv[1]), Path(sys.argv[2]))

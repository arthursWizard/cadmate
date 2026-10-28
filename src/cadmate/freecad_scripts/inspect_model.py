# Runs inside FreeCADCmd. Reads the STEP file in $CADMATE_MODEL and writes its
# bounding box (mm) and solid count as JSON to $CADMATE_OUT.
#
# With $CADMATE_PLACEMENT (JSON: "matrix", 16 numbers row by row, the footprint's
# model placement; "section_z", the height of the board's mid-plane), it also
# places the model as KiCad does and lists every solid: its placed bounding box,
# its volume, and the closed loops where it crosses section_z (a lead in its
# hole, or a body that goes into the board), unless "parts" is false. Either way it
# lists the round pin 1 marks on top of the solids ("marks", placed): a full circle
# cylinder along Z, shallower than half its solid, ending at the solid's top and
# lying inside it, as a pin 1 dimple does (a corner fillet or a lead does not).
import json
import math
import os

import FreeCAD as App
import Part

shape = Part.Shape()
shape.read(os.environ["CADMATE_MODEL"])
bb = shape.BoundBox
result = {
    "bbox": {"xmin": bb.XMin, "ymin": bb.YMin, "zmin": bb.ZMin, "xmax": bb.XMax, "ymax": bb.YMax, "zmax": bb.ZMax},
    "solids": len(shape.Solids),
    "valid": shape.isValid(),
}


def tight(shape):
    """The shape's box. transformGeometry turns cylinders into B-splines whose plain BoundBox follows their control
    points, about 0.2 mm too big on a 0.55 mm peg; the optimal box follows the surface."""
    try:
        return shape.optimalBoundingBox()
    except Exception:  # older FreeCAD, or a shape OCC cannot measure that way
        return shape.BoundBox


def box(b):
    return {"xmin": b.XMin, "ymin": b.YMin, "zmin": b.ZMin, "xmax": b.XMax, "ymax": b.YMax, "zmax": b.ZMax}


def placed_box(b, m):
    """The box of b's corners moved by m (exact for the quarter turns KiCad models use)."""
    pts = [m.multVec(App.Vector(x, y, z)) for x in (b.XMin, b.XMax) for y in (b.YMin, b.YMax) for z in (b.ZMin, b.ZMax)]
    lo = [min(getattr(p, a) for p in pts) for a in "xyz"]
    hi = [max(getattr(p, a) for p in pts) for a in "xyz"]
    return App.BoundBox(*lo, *hi)


def pin1_marks(shape, m):
    marks = []
    o = m.multVec(App.Vector(0, 0, 0))
    for solid in shape.Solids:
        sb = placed_box(solid.BoundBox, m)
        for face in solid.Faces:
            cyl = face.Surface
            if not isinstance(cyl, Part.Cylinder):
                continue
            u0, u1 = face.ParameterRange[:2]
            if u1 - u0 < 2 * math.pi - 1e-3:
                continue
            axis = m.multVec(cyl.Axis) - o
            if abs(axis.z) < 0.99 * axis.Length:
                continue
            r = cyl.Radius * axis.Length
            c = m.multVec(cyl.Center)
            fb = placed_box(face.BoundBox, m)
            inside = min(c.x - sb.XMin, sb.XMax - c.x, c.y - sb.YMin, sb.YMax - c.y)
            if fb.ZMax < sb.ZMax - 0.05 or fb.ZLength > sb.ZLength / 2 or inside < r + 0.05:
                continue
            marks.append({"x": c.x, "y": c.y, "r": r})
    return marks


spec = os.environ.get("CADMATE_PLACEMENT")
if spec:
    spec = json.loads(spec)
    m = App.Matrix(*spec["matrix"])
    result["marks"] = pin1_marks(shape, m)
if spec and spec.get("parts", True):
    placed = shape.transformGeometry(m)
    z = spec["section_z"]
    parts = []
    for i, solid in enumerate(placed.Solids):
        sb = tight(solid)
        loops = []
        if sb.ZMin < z < sb.ZMax:
            for wire in solid.slice(App.Vector(0, 0, 1), z):
                if not wire.isClosed():
                    continue
                try:
                    area = Part.Face(wire).Area
                except Exception:  # a wire OCC cannot fill; its box still says where it is
                    area = None
                loops.append({"bbox": box(tight(wire)), "area": area})
        parts.append({"name": f"solid {i + 1}", "bbox": box(sb), "volume": solid.Volume, "section": loops})
    result["parts"] = parts

with open(os.environ["CADMATE_OUT"], "w") as f:
    json.dump(result, f)

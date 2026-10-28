# Runs inside FreeCADCmd. Reads the STEP file in $CADMATE_MODEL and writes its
# bounding box (mm) and solid count as JSON to $CADMATE_OUT.
#
# With $CADMATE_PLACEMENT (JSON: "matrix", 16 numbers row by row, the footprint's
# model placement; "section_z", the height of the board's mid-plane), it also
# places the model as KiCad does and lists every solid: its placed bounding box,
# its volume, and the closed loops where it crosses section_z (a lead in its
# hole, or a body that goes into the board).
import json
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


spec = os.environ.get("CADMATE_PLACEMENT")
if spec:
    spec = json.loads(spec)
    placed = shape.transformGeometry(App.Matrix(*spec["matrix"]))
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

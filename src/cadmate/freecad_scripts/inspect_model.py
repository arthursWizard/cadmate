# Runs inside FreeCADCmd. Reads the STEP file in $CADMATE_MODEL and writes its
# bounding box (mm) and solid count as JSON to $CADMATE_OUT.
import json
import os

import Part

shape = Part.Shape()
shape.read(os.environ["CADMATE_MODEL"])
bb = shape.BoundBox
result = {
    "bbox": {"xmin": bb.XMin, "ymin": bb.YMin, "zmin": bb.ZMin, "xmax": bb.XMax, "ymax": bb.YMax, "zmax": bb.ZMax},
    "solids": len(shape.Solids),
    "valid": shape.isValid(),
}
with open(os.environ["CADMATE_OUT"], "w") as f:
    json.dump(result, f)

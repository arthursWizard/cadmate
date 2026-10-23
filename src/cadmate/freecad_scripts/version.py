# Runs inside FreeCADCmd. Writes FreeCAD's version as JSON to $CADMATE_OUT.
import json
import os

import FreeCAD

v = FreeCAD.Version()
with open(os.environ["CADMATE_OUT"], "w") as f:
    json.dump({"version": ".".join(str(p) for p in v[:3] if p != ""), "raw": [str(p) for p in v]}, f)

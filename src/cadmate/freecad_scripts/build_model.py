# Runs inside FreeCADCmd. Builds the solids listed in the JSON file $CADMATE_SPEC
# (planned by cadmate/model.py) and exports them as one STEP file, one solid per
# object so body and leads keep separate colours. Writes the bounding box and
# solid count as JSON to $CADMATE_OUT. The STEP keeps the colours on FreeCAD 1.0 and later, headless too.
#
# Solids: {"name", "colour", one of "box": [x, y, z, dx, dy, dz],
#          "cyl": [x, y, z, r, h], "prism": {"pts": [[x, z], ...], "y0", "dy"},
#          and optionally "cut": [box or cyl, ...]}
import json
import os

import FreeCAD as App
import Part

V = App.Vector


def make(s):
    if "box" in s:
        x, y, z, dx, dy, dz = s["box"]
        return Part.makeBox(dx, dy, dz, V(x, y, z))
    if "cyl" in s:
        x, y, z, r, h = s["cyl"]
        return Part.makeCylinder(r, h, V(x, y, z))
    pr = s["prism"]
    pts = [V(x, pr["y0"], z) for x, z in pr["pts"]]
    face = Part.Face(Part.makePolygon(pts + [pts[0]]))
    return face.extrude(V(0, pr["dy"], 0))


spec = json.load(open(os.environ["CADMATE_SPEC"]))
doc = App.newDocument("cadmate_model")
objs = []
for s in spec["solids"]:
    shape = make(s)
    for c in s.get("cut", []):
        shape = shape.cut(make(c))
    obj = doc.addObject("Part::Feature", s["name"])
    obj.Shape = shape
    if App.GuiUp:
        obj.ViewObject.ShapeColor = tuple(s["colour"])
    objs.append(obj)
doc.recompute()

# Colours: with the GUI, from the view objects. Headless, FreeCAD 1.0 and later take (object, face colours)
# pairs in Import.export; 0.20 and 0.21 have no headless way, so their STEP stays grey.
headless_colours = int(App.Version()[0]) >= 1
if App.GuiUp:
    import ImportGui

    ImportGui.export(objs, spec["out"])
else:
    import Import

    if headless_colours:
        items = [(o, [tuple(float(c) for c in s["colour"][:3])] * len(o.Shape.Faces)) for o, s in zip(objs, spec["solids"])]
        Import.export(items, spec["out"])
    else:
        Import.export(objs, spec["out"])

compound = Part.makeCompound([o.Shape for o in objs])
bb = compound.BoundBox
result = {
    "bbox": {"xmin": bb.XMin, "ymin": bb.YMin, "zmin": bb.ZMin, "xmax": bb.XMax, "ymax": bb.YMax, "zmax": bb.ZMax},
    "solids": len(compound.Solids),
    "valid": all(o.Shape.isValid() for o in objs),
}
with open(os.environ["CADMATE_OUT"], "w") as f:
    json.dump(result, f)

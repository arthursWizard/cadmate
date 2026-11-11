# Reads how a FreeCAD document is built, not what it looks like: every object's type, label, parent, what it is
# attached to, its driving values and which of them are bound to an expression, its sketch constraints, and each
# Body's feature order and tip. cadmate/structure.py turns these facts into findings (sketches on generated faces,
# under-constrained sketches, default names, bare numbers, dead solids...).
#
# Headless: FreeCADCmd opens $CADMATE_DOC (an .FCStd), recomputes it and writes the facts as JSON to $CADMATE_OUT.
# Live: the bridge runs this file's text in the user's window and calls collect(doc) without recomputing, so the
# check reads the document as it is and changes nothing.
import json
import os

ORIGIN_TYPES = ("App::Origin", "App::Plane", "App::Line", "App::Point")
# Values that size or place a feature; structure.py decides which of them are in use from the mode properties.
VALUE_PROPS = ("Length", "Length2", "Angle", "Angle2", "Radius", "Radius2", "Size", "Size2", "Value", "Diameter", "Depth",
               "Occurrences", "Width", "Height", "Offset", "LengthFwd", "LengthRev", "Thickness")
MODE_PROPS = ("Type", "DepthType", "ChamferType", "Mode")
DIMENSIONS = ("Distance", "DistanceX", "DistanceY", "Radius", "Diameter", "Angle")


def _num(v):
    v = getattr(v, "Value", v)
    if isinstance(v, bool):
        return None
    return float(v) if isinstance(v, (int, float)) else None


def _support(o):
    sup = None
    for p in ("AttachmentSupport", "Support"):  # renamed in FreeCAD 1.0
        if p in o.PropertiesList:
            sup = getattr(o, p)
            break
    out = []
    for item in sup or []:
        target, subs = item[0], item[1] if len(item) > 1 else ()
        if isinstance(subs, str):
            subs = (subs,)
        out.append([target.Name, target.TypeId, [s for s in subs if s]])
    return out


def _sketch(o):
    dims = []
    for i, c in enumerate(o.Constraints):
        if c.Type not in DIMENSIONS:
            continue
        dims.append({"index": i, "name": c.Name or "", "type": c.Type, "value": _num(c.Value),
                     "driving": bool(getattr(c, "Driving", True)), "active": bool(getattr(c, "IsActive", True))})
    fully = getattr(o, "FullyConstrained", None)
    return {"fully_constrained": None if fully is None else bool(fully), "dimensions": dims,
            "geometry": len(o.Geometry), "external": len(getattr(o, "ExternalGeometry", []) or [])}


def _object(o):
    parent = None
    try:
        g = o.getParentGeoFeatureGroup()
        parent = g.Name if g is not None else None
    except Exception:
        pass
    out = {
        "name": o.Name, "label": o.Label, "type": o.TypeId, "parent": parent,
        "state": [str(s) for s in getattr(o, "State", [])],
        "in_list": [[p.Name, p.TypeId] for p in o.InList],
        "expressions": [str(path) for path, _ in getattr(o, "ExpressionEngine", [])],
    }
    if "MapMode" in o.PropertiesList:
        out["map_mode"] = str(o.MapMode)
        out["support"] = _support(o)
    props = {}
    for p in VALUE_PROPS:
        if p in o.PropertiesList:
            n = _num(getattr(o, p))
            if n is not None:
                props[p] = n
    for p in MODE_PROPS:
        if p in o.PropertiesList:
            props[p] = str(getattr(o, p))
    if props:
        out["props"] = props
    if "Suppressed" in o.PropertiesList and getattr(o, "Suppressed"):
        out["suppressed"] = True
    if o.TypeId == "Sketcher::SketchObject":
        out["sketch"] = _sketch(o)
    if o.TypeId == "PartDesign::Body":
        tip = getattr(o, "Tip", None)
        base = getattr(o, "BaseFeature", None)
        out["body"] = {"group": [x.Name for x in o.Group], "tip": tip.Name if tip else None,
                       "base": base.Name if base else None}
    shape = getattr(o, "Shape", None)
    if shape is not None and hasattr(shape, "isNull"):
        try:
            out["solids"] = 0 if shape.isNull() else len(shape.Solids)
        except Exception:
            pass
    return out


def collect(doc):
    modified = None
    try:
        import FreeCAD as App

        if App.GuiUp:
            import FreeCADGui as Gui

            modified = bool(Gui.getDocument(doc.Name).Modified)
    except Exception:
        pass
    return {"doc": doc.Name, "label": doc.Label, "file": doc.FileName or None, "modified": modified,
            "objects": [_object(o) for o in doc.Objects if o.TypeId not in ORIGIN_TYPES]}


if os.environ.get("CADMATE_DOC"):
    import FreeCAD as App

    d = App.openDocument(os.environ["CADMATE_DOC"])
    d.recompute()
    facts = collect(d)
    facts["version"] = ".".join(str(x) for x in App.Version()[:3])
    with open(os.environ["CADMATE_OUT"], "w") as f:
        json.dump(facts, f)

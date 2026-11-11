"""Model structure check: is a FreeCAD part built so it can be changed, not just correct?

A model can pass every geometric check and still be hard to change: sketches stuck to faces that renumber when the
geometry changes, sizes typed as bare numbers in twenty places, FreeCAD's default names, fixes stacked on top of
features instead of the feature being edited. This check reads the document's tree (freecad_scripts/model_structure.py
collects the facts, headless or in the user's window) and reports those, so the modelling rules in the
freecad-modelling skill are checked rather than hoped for.

The rules, for a part meant to be edited (a printed case, a bracket):
- every sketch and datum is attached to an origin plane or a datum, never to a face, edge or vertex of a feature;
- every sketch is fully constrained;
- driving sizes come from one parameter sheet (a Spreadsheet or a VarSet): feature values (pad lengths, fillet radii,
  wall thickness...) are bound to it; a sketch dimension is bound to it or, when it is local to that sketch, named;
- bodies, sketches, features and datums carry names that say what they are, not Sketch003 or Pad001;
- no feature is broken, suppressed or left past the body's tip, and no sketch is left unused;
- the printed pieces are PartDesign bodies, not dead solids (a Part::Feature has a shape and no history).

The report is saved beside the .FCStd as <name>-structure.json; project_check flags a part whose report is missing,
older than the .FCStd, or has errors.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import apps, project

SCRIPT = Path(__file__).parent / "freecad_scripts" / "model_structure.py"
REPORT_SUFFIX = "-structure.json"
LONG_TREE = 40  # solid features in one body before the tree is worth a second look

ORIGIN = {"App::Origin", "App::Plane", "App::Line", "App::Point"}
DATUMS = {"PartDesign::Plane", "PartDesign::Line", "PartDesign::Point", "PartDesign::CoordinateSystem",
          "Part::DatumPlane", "Part::DatumLine", "Part::DatumPoint", "App::LocalCoordinateSystem"}
BINDERS = {"PartDesign::ShapeBinder", "PartDesign::SubShapeBinder", "Part::SubShapeBinder"}
PARAM_SOURCES = {"Spreadsheet::Sheet", "App::VarSet"}
CONTAINERS = {"PartDesign::Body", "App::Part", "App::DocumentObjectGroup"}
NOT_FEATURES = DATUMS | BINDERS | {"Sketcher::SketchObject", "PartDesign::Body"}
# FreeCAD's default label for a type, where it isn't the type's own short name.
DEFAULT_NAMES = {"PartDesign::Plane": "DatumPlane", "PartDesign::Line": "DatumLine", "PartDesign::Point": "DatumPoint",
                 "PartDesign::CoordinateSystem": "Local_CS", "Sketcher::SketchObject": "Sketch",
                 "Spreadsheet::Sheet": "Spreadsheet", "PartDesign::ShapeBinder": "ShapeBinder",
                 "PartDesign::SubShapeBinder": "Binder", "Part::SubShapeBinder": "Binder",
                 "App::DocumentObjectGroup": "Group", "Part::Extrusion": "Extrude", "Part::Fuse": "Fusion",
                 "Part::MultiFuse": "Fusion", "Part::MultiCommon": "Common"}
NAMED_TYPES = ("PartDesign::", "Sketcher::", "Part::", "App::Part", "App::DocumentObjectGroup")


class StructureError(RuntimeError):
    pass


def _short(t: str) -> str:
    return t.split("::")[-1]


def _default_label(o: dict) -> bool:
    if not o["type"].startswith(NAMED_TYPES) or o["type"] == "Part::Feature":
        return False  # a plain solid takes its name from the file it was imported from
    base = DEFAULT_NAMES.get(o["type"], _short(o["type"]))
    names = {base, _short(o["type"]), "Unnamed"}
    return re.fullmatch(rf"(?:{'|'.join(map(re.escape, names))})\d*", o["label"]) is not None


def _bound(o: dict) -> set[str]:
    """The property names (and sketch constraints, as 'Constraints.<name>' or 'Constraints[<i>]') driven by an expression."""
    out = set()
    for p in o.get("expressions", []):
        p = p.lstrip(".")
        m = re.match(r"Constraints(?:\.(\w+)|\[(\d+)\])", p)
        if m:
            out.add(f"Constraints.{m.group(1)}" if m.group(1) else f"Constraints[{m.group(2)}]")
        else:
            out.add(re.split(r"[.\[]", p, maxsplit=1)[0])
    return out


def driving_values(o: dict) -> list[str]:
    """The values that size or place this feature, given its modes (a through-all pocket has no length in use)."""
    props, t = o.get("props", {}), _short(o["type"])
    mode = props.get("Type")
    use: list[str] = []
    if t in ("Pad", "Pocket"):
        if mode in (None, "Length", "Dimension", "TwoLengths"):
            use.append("Length")
        if mode == "TwoLengths":
            use.append("Length2")
    elif t in ("Revolution", "Groove"):
        if mode in (None, "Angle", "TwoAngles"):
            use.append("Angle")
        if mode == "TwoAngles":
            use.append("Angle2")
    elif t == "Fillet":
        use.append("Radius")
    elif t == "Chamfer":
        use.append("Size")
        ct = props.get("ChamferType", "")
        if ct.startswith("Two"):
            use.append("Size2")
        elif "Angle" in ct:
            use.append("Angle")
    elif t == "Thickness":
        use.append("Value")
    elif t == "Draft":
        use.append("Angle")
    elif t == "Hole":
        use.append("Diameter")
        if props.get("DepthType") in (None, "Dimension"):
            use.append("Depth")
    elif t == "LinearPattern":
        use += ["Offset" if props.get("Mode") == "Offset" else "Length", "Occurrences"]
    elif t == "PolarPattern":
        use += ["Offset" if props.get("Mode") == "Offset" else "Angle", "Occurrences"]
    elif t.startswith(("Additive", "Subtractive")) and o["type"].startswith("PartDesign::") or o["type"] in (
            "Part::Box", "Part::Cylinder", "Part::Cone", "Part::Sphere", "Part::Torus"):
        use += ["Length", "Width", "Height", "Radius", "Radius2"]
        if props.get("Angle") not in (None, 360.0):
            use.append("Angle")
    elif o["type"] == "Part::Extrusion":
        use += ["LengthFwd", "LengthRev"]
    return [p for p in use if props.get(p) not in (None, 0.0)]


def _finding(sev: str, check: str, obj: str | None, problem: str, fix: str) -> dict:
    return {"severity": sev, "check": check, "object": obj, "problem": problem, "fix": fix}


def _few(labels: list[str], n: int = 6) -> str:
    return ", ".join(labels[:n]) + (f" and {len(labels) - n} more" if len(labels) > n else "")


def evaluate(facts: dict) -> dict:
    """Findings and a summary from model_structure.py's facts. Pure: no FreeCAD needed."""
    objs = facts.get("objects", [])
    by_name = {o["name"]: o for o in objs}
    found: list[dict] = []

    def label(name: str) -> str:
        o = by_name.get(name)
        return o["label"] if o else name

    # Broken, suppressed
    for o in objs:
        bad = [s for s in o.get("state", []) if s in ("Invalid", "Error")]
        if bad:
            found.append(_finding("error", "broken", o["label"], f"{o['label']} ({_short(o['type'])}) is in error",
                                  "fix it before anything else: open it, read the error in the Report view, and repair "
                                  "the reference or value that broke"))
        if o.get("suppressed"):
            found.append(_finding("warning", "tree", o["label"], f"{o['label']} is suppressed",
                                  "delete it, or unsuppress it if it's meant to be there; a suppressed feature is a "
                                  "change nobody finished"))

    # Attachment
    for o in objs:
        if "map_mode" not in o or not (o["type"] == "Sketcher::SketchObject" or o["type"] in DATUMS):
            continue
        kind = "sketch" if o["type"] == "Sketcher::SketchObject" else "datum"
        support = o.get("support", [])
        if not support or o["map_mode"] == "Deactivated":
            if kind == "sketch" and o.get("parent") and "Placement" not in _bound(o):
                found.append(_finding("warning", "attachment", o["label"],
                                      f"{o['label']} isn't attached to anything: its position is a fixed placement",
                                      "attach it to an origin plane or a datum plane (with an offset from Params "
                                      "if it isn't on the origin)"))
            continue
        for target, ttype, subs in support:
            geo = [s for s in subs if re.match(r"(Face|Edge|Vertex)\d+", s)]
            if ttype in ORIGIN or ttype in DATUMS or ttype == "Sketcher::SketchObject" or not geo:
                continue
            if ttype in BINDERS:
                found.append(_finding("warning", "attachment", o["label"],
                                      f"{o['label']} is attached to {', '.join(geo)} of the binder {label(target)}",
                                      "a binder's faces renumber when the part it copies changes; attach to a datum "
                                      "plane placed from Params instead"))
                continue
            found.append(_finding("error", "attachment", o["label"],
                                  f"{kind} {o['label']} is attached to {', '.join(geo)} of {label(target)}, a face "
                                  "the model generates, so it moves or breaks when that feature changes",
                                  "re-attach it to an origin plane (XY, XZ, YZ) with an attachment offset bound to "
                                  "Params, or to a datum plane placed that way; then check the sketch didn't move"))

    # Sketches: constrained, used
    for o in objs:
        sk = o.get("sketch")
        if not sk:
            continue
        if sk.get("fully_constrained") is False:
            found.append(_finding("error", "constraints", o["label"], f"sketch {o['label']} isn't fully constrained",
                                  "constrain every point (to the origin, the axes, or Params-driven dimensions) "
                                  "until FreeCAD shows it fully constrained; a free point moves on the next recompute"))
        users = [n for n, t in (tuple(x) for x in o.get("in_list", [])) if t not in CONTAINERS]
        if not users:
            found.append(_finding("info", "tree", o["label"], f"sketch {o['label']} isn't used by any feature",
                                  "delete it if it's left over from an earlier attempt; a reference sketch "
                                  "(a master layout) is fine when features or expressions use it"))

    # Names
    defaults = [o["label"] for o in objs if _default_label(o)]
    if defaults:
        found.append(_finding("warning", "names", None,
                              f"{len(defaults)} object(s) keep FreeCAD's default name: {_few(defaults)}",
                              "rename each for what it is (base-outline, usb-cutout, wall-fillet, lid body); "
                              "the tree is how a person finds the feature to change"))

    # Parameters
    sources = [o for o in objs if o["type"] in PARAM_SOURCES]
    driving = bound_n = named_local = 0
    unbound_features: list[tuple[str, list[str]]] = []
    for o in objs:
        b = _bound(o)
        values = driving_values(o)
        if values:
            loose = [p for p in values if p not in b]
            driving += len(values)
            bound_n += len(values) - len(loose)
            if loose:
                unbound_features.append((o["label"], loose))
        sk = o.get("sketch")
        if sk:
            bare = 0
            for d in sk.get("dimensions", []):
                if not d.get("driving", True) or not d.get("active", True):
                    continue
                driving += 1
                if f"Constraints.{d['name']}" in b or f"Constraints[{d['index']}]" in b:
                    bound_n += 1
                elif d["name"]:
                    named_local += 1
                else:
                    bare += 1
            if bare:
                found.append(_finding("warning", "parameters", o["label"],
                                      f"sketch {o['label']} has {bare} bare dimension(s): not from Params, not named",
                                      "bind each size that matters outside this sketch to Params; give the ones "
                                      "that are local to it a name (Constraints > name) so they read as deliberate"))
    for lab, loose in unbound_features:
        found.append(_finding("warning", "parameters", lab, f"{lab}: {', '.join(loose)} typed as a number",
                              "bind it to a Params entry (an expression like Params.wall), adding the entry "
                              "if it's new"))
    if driving and not sources:
        found.append(_finding("error", "parameters", None,
                              "no parameter sheet: every size is typed into the feature that uses it",
                              "add one Spreadsheet (or VarSet) named Params with the sizes that drive the part "
                              "(outline, wall, heights, clearances) and bind features and sketches to it"))
    elif len(sources) > 1:
        found.append(_finding("warning", "parameters", None,
                              f"parameters in {len(sources)} places: {_few([s['label'] for s in sources])}",
                              "keep one Params sheet so every size is found in one place; merge the others into it"))

    # Dead solids
    containers = {o["name"]: o for o in objs if o["type"] in CONTAINERS}
    dead, imported = [], []
    for o in objs:
        if o["type"] != "Part::Feature":
            continue
        parent = containers.get(o.get("parent") or "")
        (imported if parent and parent["type"] == "App::Part" else dead).append(o["label"])
    if dead:
        found.append(_finding("warning", "dead solids", None,
                              f"{len(dead)} solid(s) with no history (Part::Feature): {_few(dead)}",
                              "a printed piece should be a PartDesign Body so it can be changed by editing a "
                              "feature; a component model built by a script, or an imported board, is fine as is "
                              "(its script or file is the source)"))

    # Bodies
    bodies = []
    for o in objs:
        body = o.get("body")
        if not body:
            continue
        feats = [n for n in body["group"] if n in by_name and by_name[n]["type"] not in NOT_FEATURES]
        sketches = [n for n in body["group"] if n in by_name and by_name[n]["type"] == "Sketcher::SketchObject"]
        tip = body.get("tip")
        if tip in feats and feats.index(tip) < len(feats) - 1:
            after = [label(n) for n in feats[feats.index(tip) + 1:]]
            found.append(_finding("warning", "tree", o["label"],
                                  f"body {o['label']} has feature(s) past its tip: {_few(after)}",
                                  "set the tip to the last feature, or delete what's past it"))
        if len(feats) > LONG_TREE:
            found.append(_finding("warning", "tree", o["label"],
                                  f"body {o['label']} has {len(feats)} features",
                                  "look for fixes stacked on earlier features (a pad then a pocket that trims it "
                                  "back) and fold them into the feature they correct; pattern repeated features"))
        bodies.append({"name": o["name"], "label": o["label"], "features": len(feats), "sketches": len(sketches),
                       "tip": label(tip) if tip else None})

    order = {"error": 0, "warning": 1, "info": 2}
    found.sort(key=lambda f: order[f["severity"]])
    counts = {s: sum(1 for f in found if f["severity"] == s) for s in order}
    return {
        "ok": counts["error"] == 0,
        "counts": counts,
        "findings": found,
        "summary": {
            "objects": len(objs), "bodies": bodies,
            "parameters": {"sources": [s["label"] for s in sources], "driving_values": driving, "bound": bound_n,
                           "named_local": named_local},
            "dead_solids": len(dead), "imported_solids": len(imported),
        },
    }


def report_path(fcstd: Path) -> Path:
    return fcstd.with_name(fcstd.stem + REPORT_SUFFIX)


def locate(path: str | Path) -> Path:
    """The .FCStd to check: the file itself, or the only one in a folder."""
    p = Path(path).expanduser().resolve()
    if p.is_file():
        if p.suffix.lower() != ".fcstd":
            raise StructureError(f"{p.name} isn't a FreeCAD document (.FCStd)")
        return p
    if p.is_dir():
        docs = sorted(f for f in p.iterdir() if f.suffix.lower() == ".fcstd")
        if len(docs) == 1:
            return docs[0]
        if docs:
            raise StructureError(f"several .FCStd in {p} ({', '.join(d.name for d in docs)}); pass the one to check")
    raise StructureError(f"no .FCStd at {p}")


def save(result: dict, fcstd: Path) -> Path:
    rep = report_path(fcstd)
    rep.write_text(json.dumps(result | {"checked": datetime.now().isoformat(timespec="seconds")}, indent=1),
                   encoding="utf-8")
    return rep


def check(path: str | Path) -> dict:
    """Check a saved .FCStd headless and save the report beside it."""
    fcstd = locate(path)
    facts = apps.run_freecad_script(SCRIPT, env={"CADMATE_DOC": str(fcstd)})
    result = {"file": str(fcstd)} | evaluate(facts)
    result["report"] = str(save(result, fcstd))
    return result


def live_code() -> str:
    """What the bridge runs in the user's window: the collector, then collect() on the document it was given."""
    # A read changes nothing: drop the bridge's transaction so the user's undo list doesn't get an empty step.
    return SCRIPT.read_text(encoding="utf-8") + "\nresult = collect(doc)\ndoc.abortTransaction()\n"


def from_live(facts: dict) -> dict:
    """Evaluate facts read in the user's window; save the report when the document is saved and unchanged since."""
    result = {"doc": facts.get("doc"), "file": facts.get("file")} | evaluate(facts)
    f = facts.get("file")
    if f and facts.get("modified") is False and Path(f).exists():
        result["report"] = str(save(result, Path(f)))
    elif f:
        result["report"] = None
        result["note"] = "unsaved changes: the report is saved once the document is saved and checked again"
    return result


def project_findings(root: Path) -> list[dict]:
    """A FreeCAD part whose structure report is missing, older than the .FCStd, or has errors."""
    sh = project.shape(root)
    if "freecad" not in sh.tools:
        return []
    area = root / sh.dir("freecad")
    if not area.is_dir():
        return []
    _, folders = project.units(root, sh, "freecad")
    out = []
    for d in [area, *folders]:
        for f in sorted(d.glob("*.FCStd")):
            out += _file_findings(root, f)
    return out


def _file_findings(root: Path, fcstd: Path) -> list[dict]:
    rep = report_path(fcstd)
    rel = fcstd.relative_to(root).as_posix()
    if not rep.exists():
        return [project._finding("info", rel, "its structure has never been checked",
                                 "run check_model_structure on it (sketch attachment, constraints, parameters, names)")]
    if fcstd.stat().st_mtime > rep.stat().st_mtime:
        return [project._finding("warning", rep.relative_to(root).as_posix(), f"older than {fcstd.name}",
                                 "run check_model_structure again")]
    try:
        r = json.loads(rep.read_text(encoding="utf-8"))
    except ValueError:
        return [project._finding("warning", rep.relative_to(root).as_posix(), "not valid JSON",
                                 "run check_model_structure again")]
    n = r.get("counts", {}).get("error", 0)
    if n:
        first = next((f["problem"] for f in r.get("findings", []) if f["severity"] == "error"), "")
        return [project._finding("warning", rel, f"the last structure check found {n} error(s), e.g. {first[:160]}",
                                 "fix them (the freecad-modelling skill says how) and run check_model_structure again")]
    return []

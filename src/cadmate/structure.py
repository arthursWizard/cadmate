"""Model structure check: is a FreeCAD part built so it can be changed, not just correct?

A model can pass every geometric check and still be hard to change: sketches stuck to faces that renumber when the
geometry changes, sizes typed as bare numbers in twenty places, FreeCAD's default names, a cut that misses the part and
changes nothing. This check reads the document's tree (freecad_scripts/model_structure.py collects the facts, headless
or in the user's window) and reports those, so the convention in the freecad-modelling skill is checked rather than
hoped for.

The convention, for a printed or machined part (from FreeCAD's "Feature editing" guidance and the Resilient Modeling
Strategy's feature groups; no formal standard covers feature-tree structure):
- one Params sheet (a Spreadsheet, or a VarSet) holds every driving size; a value counts as driven only when its
  expression refers to it, so `7` typed as an expression is still a bare number;
- sketches and datums are attached to origin planes or datums, with any attachment offset from Params; sketches,
  external geometry and binders reference origin, datum or sketch geometry, never a face, edge or vertex a feature
  generated;
- every sketch is fully constrained; a dimension local to its sketch may be named instead of bound (reported as info);
- every feature changes the shape (a cut that misses the part is an error), none is broken, suppressed or past the tip;
- fillets and chamfers come last in each body (RMS's last group: nothing should build on them);
- bodies, sketches, features and datums carry names that say what they are;
- printed pieces are PartDesign bodies: solids with no history (Part::Feature) and Part-workbench CSG are reported.

What it can't see: a fix stacked on a feature that still changes the shape (a pad that refills part of a pocket). The
skill's rule is to edit the feature instead; LONG_TREE is only a weak hint.

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
NO_EFFECT = 1e-6  # relative volume change below which a feature changed nothing

ORIGIN = {"App::Origin", "App::Plane", "App::Line", "App::Point"}
DATUMS = {"PartDesign::Plane", "PartDesign::Line", "PartDesign::Point", "PartDesign::CoordinateSystem",
          "Part::DatumPlane", "Part::DatumLine", "Part::DatumPoint", "App::LocalCoordinateSystem"}
BINDERS = {"PartDesign::ShapeBinder", "PartDesign::SubShapeBinder", "Part::SubShapeBinder"}
SKETCH = "Sketcher::SketchObject"
PARAM_SOURCES = {"Spreadsheet::Sheet", "App::VarSet"}
CONTAINERS = {"PartDesign::Body", "App::Part", "App::DocumentObjectGroup"}
NOT_FEATURES = DATUMS | BINDERS | {SKETCH, "PartDesign::Body"}
FINISH = {"PartDesign::Fillet", "PartDesign::Chamfer"}
NOT_CSG = {"Part::Feature", "Part::FeaturePython", "Part::Part2DObject", "Part::Part2DObjectPython"} | DATUMS | BINDERS
# FreeCAD's default label for a type, where it isn't the type's own short name.
DEFAULT_NAMES = {"PartDesign::Plane": "DatumPlane", "PartDesign::Line": "DatumLine", "PartDesign::Point": "DatumPoint",
                 "PartDesign::CoordinateSystem": "Local_CS", SKETCH: "Sketch",
                 "Spreadsheet::Sheet": "Spreadsheet", "PartDesign::ShapeBinder": "ShapeBinder",
                 "PartDesign::SubShapeBinder": "Binder", "Part::SubShapeBinder": "Binder",
                 "App::DocumentObjectGroup": "Group", "Part::Extrusion": "Extrude", "Part::Fuse": "Fusion",
                 "Part::MultiFuse": "Fusion", "Part::MultiCommon": "Common"}
NAMED_TYPES = ("PartDesign::", "Sketcher::", "Part::", "App::Part", "App::DocumentObjectGroup")
GEOMETRY = re.compile(r"(?:.*\.)?(Face|Edge|Vertex)\d+$")


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


def _path(p: str) -> str:
    """An expression path as a key: 'Length', 'Constraints.width', 'Constraints[3]', 'AttachmentOffset.Base.z'."""
    p = p.lstrip(".")
    m = re.match(r"Constraints(?:\.(\w+)|\[(\d+)\])", p)
    if m:
        return f"Constraints.{m.group(1)}" if m.group(1) else f"Constraints[{m.group(2)}]"
    return p


def _refers(expr: str, sources: list[dict]) -> bool:
    """Whether an expression reads the parameter sheet (by name or label, bare or <<label>>)."""
    for s in sources:
        for n in {s["name"], s["label"]}:
            if re.search(rf"(?<![\w.]){re.escape(n)}\s*\.|<<{re.escape(n)}>>\s*\.", expr):
                return True
    return False


def _bound(o: dict, sources: list[dict] | None = None) -> set[str]:
    """The paths driven by an expression that reads the Params sheet. Without sources, any expression counts."""
    out = set()
    for item in o.get("expressions", []):
        path, expr = (item, "") if isinstance(item, str) else (item[0], item[1])
        if sources is None or _refers(expr, sources):
            out.add(_path(path))
    return out


def _is_bound(prop: str, bound: set[str]) -> bool:
    """prop is bound itself, or through a bound parent path (AttachmentOffset binds AttachmentOffset.Base.z)."""
    parts = prop.split(".")
    return any(".".join(parts[:i]) in bound for i in range(1, len(parts) + 1)) or any(
        b.split(".")[0] == prop for b in bound)


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
        if not props.get("Threaded") and props.get("ThreadType") in (None, "None"):
            use.append("Diameter")  # a threaded hole's diameter comes from its thread size
        if props.get("DepthType") in (None, "Dimension"):
            use.append("Depth")
    elif t == "LinearPattern":
        use += ["Offset" if props.get("Mode") in ("Spacing", "Offset") else "Length", "Occurrences"]
    elif t == "PolarPattern":
        use += ["Offset" if props.get("Mode") in ("Spacing", "Offset") else "Angle", "Occurrences"]
    elif t.startswith(("Additive", "Subtractive")) and o["type"].startswith("PartDesign::") or o["type"] in (
            "Part::Box", "Part::Cylinder", "Part::Cone", "Part::Sphere", "Part::Torus"):
        use += ["Length", "Width", "Height", "Radius", "Radius2"]
        if props.get("Angle") not in (None, 360.0):
            use.append("Angle")
    elif o["type"] == "Part::Extrusion":
        use += ["LengthFwd", "LengthRev"]
    return [p for p in use if props.get(p) not in (None, 0.0)]


def offset_values(o: dict) -> list[str]:
    """The non-zero parts of an attachment offset: each places the sketch or datum, so each is a driving value."""
    off = o.get("offset")
    if not off or o.get("map_mode") in (None, "Deactivated"):
        return []
    names = ["AttachmentOffset.Base.x", "AttachmentOffset.Base.y", "AttachmentOffset.Base.z", "AttachmentOffset.Rotation"]
    return [n for n, v in zip(names, off) if abs(v) > 1e-9]


def _reference(ttype: str, subs: list[str]) -> str:
    """'ok' (origin, datum, sketch, a whole object), 'binder' (a binder's faces), or 'generated' (a feature's faces)."""
    geo = [s for s in subs if GEOMETRY.match(s)]
    if ttype in ORIGIN or ttype in DATUMS or ttype == SKETCH or not geo:
        return "ok"
    return "binder" if ttype in BINDERS else "generated"


def _finding(sev: str, check: str, obj: str | None, problem: str, fix: str) -> dict:
    return {"severity": sev, "check": check, "object": obj, "problem": problem, "fix": fix}


def _few(labels: list[str], n: int = 6) -> str:
    return ", ".join(labels[:n]) + (f" and {len(labels) - n} more" if len(labels) > n else "")


RE_ATTACH = ("re-attach it to an origin plane (XY, XZ, YZ) with an attachment offset bound to Params, or to a datum "
             "plane placed that way; then measure that it sits where it did")


def evaluate(facts: dict) -> dict:
    """Findings and a summary from model_structure.py's facts. Pure: no FreeCAD needed."""
    objs = facts.get("objects", [])
    by_name = {o["name"]: o for o in objs}
    sources = [o for o in objs if o["type"] in PARAM_SOURCES]
    found: list[dict] = []

    def label(name: str) -> str:
        o = by_name.get(name)
        return o["label"] if o else name

    # Broken, suppressed, no effect
    for o in objs:
        solver = (o.get("sketch") or {}).get("solver") or {}
        if any(solver.values()):
            continue  # the sketch's own finding below says which constraints are wrong
        if any(s in ("Invalid", "Error") for s in o.get("state", [])):
            found.append(_finding("error", "broken", o["label"], f"{o['label']} ({_short(o['type'])}) is in error",
                                  "fix it before anything else: open it, read the error in the Report view, and repair "
                                  "the reference or value that broke"))
            continue
        if o.get("suppressed"):
            found.append(_finding("warning", "tree", o["label"], f"{o['label']} is suppressed",
                                  "delete it, or unsuppress it if it's meant to be there; a suppressed feature is a "
                                  "change nobody finished"))
            continue
        if (o["type"].startswith("PartDesign::") and o["type"] not in NOT_FEATURES and o.get("solids") == 0):
            found.append(_finding("error", "broken", o["label"], f"{o['label']} produced no solid",
                                  "its profile or input is broken (often an over- or under-constrained sketch, or a "
                                  "profile that isn't closed); fix the sketch it uses, then recompute"))
            continue
        base = by_name.get(o.get("base_feature") or "")
        if base and o.get("volume") is not None and base.get("volume") is not None:
            v, b = o["volume"], base["volume"]
            if abs(v - b) <= NO_EFFECT * max(1.0, abs(b)):
                found.append(_finding("error", "no effect", o["label"],
                                      f"{o['label']} doesn't change the shape: the volume is the same as before it",
                                      "it's in the wrong place or the wrong direction (a cut outside the part, a pad "
                                      "inside it): check its sketch's plane, offset sign and direction, and measure "
                                      "what it adds or removes"))

    # References: attachment, external geometry, binders
    for o in objs:
        kind = "sketch" if o["type"] == SKETCH else "datum" if o["type"] in DATUMS else None
        if kind and "map_mode" in o:
            support = o.get("support", [])
            if not support or o["map_mode"] == "Deactivated":
                if kind == "sketch" and o.get("parent") and not _is_bound("Placement", _bound(o, sources)):
                    found.append(_finding("warning", "attachment", o["label"],
                                          f"{o['label']} isn't attached to anything: its position is a fixed placement",
                                          "attach it to an origin plane or a datum plane (with an offset from Params "
                                          "if it isn't on the origin)"))
            else:
                for target, ttype, subs in support:
                    r = _reference(ttype, subs)
                    geo = ", ".join(s for s in subs if GEOMETRY.match(s))
                    if r == "binder":
                        found.append(_finding("warning", "attachment", o["label"],
                                              f"{o['label']} is attached to {geo} of the binder {label(target)}",
                                              "a binder's faces renumber when the part it copies changes; attach to a "
                                              "datum plane placed from Params instead"))
                    elif r == "generated":
                        found.append(_finding("error", "attachment", o["label"],
                                              f"{kind} {o['label']} is attached to {geo} of {label(target)}, geometry "
                                              "the model generates, so it moves or breaks when that feature changes",
                                              RE_ATTACH))
        for target, ttype, subs in (o.get("sketch") or {}).get("external", []) or []:
            if _reference(ttype, subs) != "ok":
                geo = ", ".join(s for s in subs if GEOMETRY.match(s))
                found.append(_finding("error", "references", o["label"],
                                      f"sketch {o['label']} uses {geo} of {label(target)} as external geometry",
                                      "reference the layout sketch, an origin axis or a datum instead, or bind the "
                                      "value to Params; a feature's edges renumber when it changes"))
        if o["type"] in BINDERS:
            for target, ttype, subs in o.get("binder_source", []):
                if ttype not in BINDERS and _reference(ttype, subs) != "ok":
                    geo = ", ".join(s for s in subs if GEOMETRY.match(s))
                    found.append(_finding("error", "references", o["label"],
                                          f"binder {o['label']} copies {geo} of {label(target)}, geometry the model "
                                          "generates", "bind the layout sketch (or a whole body) instead of its faces"))

    # Sketches: constrained, used, named-but-local dimensions
    named_local = 0
    for o in objs:
        sk = o.get("sketch")
        if not sk:
            continue
        solver, types = sk.get("solver") or {}, sk.get("constraint_types") or []

        def named(nums: list[int]) -> str:
            return ", ".join(f"{n} ({types[n - 1]})" if 0 < n <= len(types) else str(n) for n in nums)

        if solver.get("conflicting"):
            found.append(_finding("error", "constraints", o["label"],
                                  f"sketch {o['label']} has conflicting constraints: {named(solver['conflicting'])}",
                                  "remove or correct one of them (constraint numbers as FreeCAD's constraint list "
                                  "shows them); features using the sketch can't be trusted until it solves"))
        elif solver.get("redundant") or solver.get("malformed"):
            nums = (solver.get("redundant") or []) + (solver.get("malformed") or [])
            found.append(_finding("error", "constraints", o["label"],
                                  f"sketch {o['label']} is over-constrained: {named(nums)} "
                                  f"{'is' if len(nums) == 1 else 'are'} redundant or malformed",
                                  "delete the redundant constraint (another already fixes the same thing); a "
                                  "feature on an over-constrained sketch can come out empty without an error"))
        elif sk.get("fully_constrained") is False:
            found.append(_finding("error", "constraints", o["label"], f"sketch {o['label']} isn't fully constrained",
                                  "constrain every point (to the origin, the axes, the layout sketch, or "
                                  "Params-driven dimensions) until FreeCAD shows it fully constrained"))
        users = [n for n, t in (tuple(x) for x in o.get("in_list", [])) if t not in CONTAINERS]
        if not users:
            found.append(_finding("info", "tree", o["label"], f"sketch {o['label']} isn't used by any feature",
                                  "delete it if it's left over from an earlier attempt; a layout sketch is fine when "
                                  "other sketches or expressions use it"))

    # Names
    defaults = [o["label"] for o in objs if _default_label(o)]
    if defaults:
        found.append(_finding("warning", "names", None,
                              f"{len(defaults)} object(s) keep FreeCAD's default name: {_few(defaults)}",
                              "rename each for what it is (layout, outline, usb-cutout, wall-fillet, lid); "
                              "the tree is how a person finds the feature to change"))

    # Parameters
    driving = bound_n = 0
    for o in objs:
        b = _bound(o, sources)
        values = driving_values(o) + offset_values(o)
        loose = [p for p in values if not _is_bound(p, b)]
        driving += len(values)
        bound_n += len(values) - len(loose)
        if loose:
            found.append(_finding("warning", "parameters", o["label"],
                                  f"{o['label']}: {', '.join(loose)} not from Params",
                                  "bind it to a Params entry (an expression like Params.wall), adding the entry "
                                  "if it's new; a constant typed as an expression is still a bare number"))
        sk = o.get("sketch")
        if sk:
            bare, local = 0, []
            for d in sk.get("dimensions", []):
                if not d.get("driving", True) or not d.get("active", True):
                    continue
                driving += 1
                if f"Constraints.{d['name']}" in b or f"Constraints[{d['index']}]" in b:
                    bound_n += 1
                elif d["name"]:
                    local.append(d["name"])
                else:
                    bare += 1
            named_local += len(local)
            if bare:
                found.append(_finding("warning", "parameters", o["label"],
                                      f"sketch {o['label']} has {bare} bare dimension(s): not from Params, not named",
                                      "bind each size that matters outside this sketch to Params; name the ones "
                                      "that are local to it so they read as deliberate"))
            if local:
                found.append(_finding("info", "parameters", o["label"],
                                      f"sketch {o['label']} keeps {len(local)} named local dimension(s): "
                                      f"{_few(local)}",
                                      "fine if each only matters inside this sketch; move it to Params if anything "
                                      "else depends on it"))
    if driving and not sources:
        found.append(_finding("error", "parameters", None,
                              "no parameter sheet: every size is typed into the feature that uses it",
                              "add one Spreadsheet (or VarSet) named Params with the sizes that drive the part "
                              "(outline, wall, heights, clearances) and bind features and sketches to it"))
    elif len(sources) > 1:
        found.append(_finding("warning", "parameters", None,
                              f"parameters in {len(sources)} places: {_few([s['label'] for s in sources])}",
                              "keep one Params sheet so every size is found in one place; merge the others into it"))

    # Dead solids and Part-workbench CSG
    containers = {o["name"]: o for o in objs if o["type"] in CONTAINERS}
    dead, imported, csg = [], [], []
    for o in objs:
        parent = containers.get(o.get("parent") or "")
        if o["type"] == "Part::Feature":
            (imported if parent and parent["type"] == "App::Part" else dead).append(o["label"])
        elif o["type"].startswith("Part::") and o["type"] not in NOT_CSG and not (
                parent and parent["type"] == "PartDesign::Body"):
            csg.append(o["label"])
    if dead:
        found.append(_finding("warning", "dead solids", None,
                              f"{len(dead)} solid(s) with no history (Part::Feature): {_few(dead)}",
                              "a printed piece should be a PartDesign Body so it can be changed by editing a "
                              "feature; an imported board or a component model built by a script is fine as is "
                              "(its file or script is the source)"))
    if csg:
        found.append(_finding("warning", "part workbench", None,
                              f"{len(csg)} Part-workbench object(s) (primitives, booleans, Part fillets): {_few(csg)}",
                              "a printed piece is a PartDesign Body with sketches on planes and sizes from Params; "
                              "rebuild it that way (keep CSG only for a quick placeholder the user agreed to)"))

    # Bodies: tip, finish last, length
    bodies = []
    for o in objs:
        body = o.get("body")
        if not body:
            continue
        feats = [n for n in body["group"] if n in by_name and by_name[n]["type"] not in NOT_FEATURES]
        sketches = [n for n in body["group"] if n in by_name and by_name[n]["type"] == SKETCH]
        tip = body.get("tip")
        if tip in feats and feats.index(tip) < len(feats) - 1:
            after = [label(n) for n in feats[feats.index(tip) + 1:]]
            found.append(_finding("warning", "tree", o["label"],
                                  f"body {o['label']} has feature(s) past its tip: {_few(after)}",
                                  "set the tip to the last feature, or delete what's past it"))
        first_finish = next((i for i, n in enumerate(feats) if by_name[n]["type"] in FINISH), None)
        if first_finish is not None:
            late = [label(n) for n in feats[first_finish + 1:] if by_name[n]["type"] not in FINISH]
            if late:
                found.append(_finding("warning", "order", o["label"],
                                      f"body {o['label']}: {_few(late)} come(s) after the fillet or chamfer "
                                      f"{label(feats[first_finish])}",
                                      "move fillets and chamfers to the end of the body (right-click > Move object "
                                      "after other object), so no feature builds on rounded edges"))
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
            "dead_solids": len(dead), "imported_solids": len(imported), "part_workbench": len(csg),
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
    # Run with the bridge's read op: no transaction and no recompute, so nothing lands in any document's undo list.
    return SCRIPT.read_text(encoding="utf-8") + "\nresult = collect(doc)\n"


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
        for f in sorted(f for f in d.iterdir() if f.is_file() and f.suffix.lower() == ".fcstd"):
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

import json
import os
import time

import pytest

from cadmate import apps, project, structure

HAS_FREECAD = apps.find_freecadcmd() is not None


def obj(name, type_, label=None, **kw):
    return {"name": name, "label": label or name, "type": type_, "parent": kw.pop("parent", "Body"), "state": [],
            "in_list": kw.pop("in_list", []), "expressions": kw.pop("expressions", []), **kw}


def sketch(name, label=None, support=(("XY_Plane", "App::Plane", []),), fully=True, dims=(), users=("Pad",), **kw):
    return obj(name, "Sketcher::SketchObject", label, map_mode="FlatFace", support=[list(s) for s in support],
               sketch={"fully_constrained": fully, "dimensions": list(dims), "geometry": 4, "external": 0},
               in_list=[["Body", "PartDesign::Body"]] + [[u, "PartDesign::Pad"] for u in users], **kw)


def dim(i, name="", driving=True):
    return {"index": i, "name": name, "type": "DistanceX", "value": 10.0, "driving": driving, "active": True}


def clean():
    """A small part built the way the rules ask: Params, sketch on XY, named, bound."""
    return {"objects": [
        obj("Params", "Spreadsheet::Sheet", parent=None),
        obj("Body", "PartDesign::Body", "base", parent=None, body={"group": ["Sketch", "Pad"], "tip": "Pad"}),
        sketch("Sketch", "base-outline", dims=[dim(8, "width"), dim(9, "corner")],
               expressions=[".Constraints.width"]),
        obj("Pad", "PartDesign::Pad", "base-plate", props={"Length": 3.0, "Type": "Length"}, expressions=["Length"],
            in_list=[["Body", "PartDesign::Body"]]),
    ]}


def run(facts):
    return structure.evaluate(facts)


def checks(r, sev=None):
    return [f["check"] for f in r["findings"] if sev is None or f["severity"] == sev]


def test_a_clean_part_passes():
    r = run(clean())
    assert r["ok"] and r["findings"] == []
    assert r["summary"]["parameters"] == {"sources": ["Params"], "driving_values": 3, "bound": 2, "named_local": 1}
    assert r["summary"]["bodies"] == [{"name": "Body", "label": "base", "features": 1, "sketches": 1, "tip": "base-plate"}]


def test_sketch_on_a_generated_face_is_an_error_but_on_a_datum_or_sketch_is_not():
    f = clean()
    f["objects"].append(sketch("Sketch001", "usb-cutout", support=[["Pad", "PartDesign::Pad", ["Face6"]]]))
    f["objects"].append(sketch("Sketch002", "rim", support=[["DatumPlane", "PartDesign::Plane", [""]]]))
    f["objects"].append(obj("DatumLine", "PartDesign::Line", "hinge-axis", map_mode="ObjectX",
                            support=[["Sketch", "Sketcher::SketchObject", ["Edge2"]]]))
    r = run(f)
    errs = [x for x in r["findings"] if x["severity"] == "error"]
    assert [x["object"] for x in errs] == ["usb-cutout"]
    assert "Face6 of base-plate" in errs[0]["problem"]


def test_datum_on_an_edge_is_an_error_and_on_a_binder_a_warning():
    f = clean()
    f["objects"].append(obj("DatumPlane", "PartDesign::Plane", "lid-seam", map_mode="FlatFace",
                            support=[["Pad", "PartDesign::Pad", ["Edge3"]]]))
    f["objects"].append(sketch("Sketch001", "lid-lip", support=[["Binder", "PartDesign::SubShapeBinder", ["Face1"]]]))
    r = run(f)
    assert [(x["severity"], x["object"]) for x in r["findings"] if x["check"] == "attachment"] == [
        ("error", "lid-seam"), ("warning", "lid-lip")]


def test_under_constrained_sketch_and_unattached_sketch():
    f = clean()
    f["objects"][2]["sketch"]["fully_constrained"] = False
    f["objects"].append(sketch("Sketch001", "loose", support=[], users=("Pocket",)))
    r = run(f)
    assert ("error", "constraints") in [(x["severity"], x["check"]) for x in r["findings"]]
    assert any(x["check"] == "attachment" and x["object"] == "loose" for x in r["findings"])


def test_default_names_are_reported_together():
    f = clean()
    f["objects"][1]["label"] = "Body"
    f["objects"][2]["label"] = "Sketch"
    f["objects"][3]["label"] = "Pad001"
    names = [x for x in run(f)["findings"] if x["check"] == "names"]
    assert len(names) == 1 and "3 object(s)" in names[0]["problem"] and "Pad001" in names[0]["problem"]


def test_imported_solid_labels_are_not_default_names():
    f = clean()
    f["objects"].append(obj("Part__Feature", "Part::Feature", "Part__Feature", parent="Import"))
    f["objects"].append(obj("Import", "App::Part", "main-board", parent=None))
    r = run(f)
    assert "names" not in checks(r) and "dead solids" not in checks(r)
    assert r["summary"]["imported_solids"] == 1


def test_bare_numbers():
    f = clean()
    f["objects"][3]["expressions"] = []
    f["objects"][2]["sketch"]["dimensions"].append(dim(10))
    f["objects"].append(obj("Fillet", "PartDesign::Fillet", "edge-round", props={"Radius": 1.0}))
    f["objects"].append(obj("Pocket", "PartDesign::Pocket", "port", props={"Length": 5.0, "Type": "ThroughAll"}))
    r = run(f)
    probs = [x["problem"] for x in r["findings"] if x["check"] == "parameters"]
    assert any("1 bare dimension" in p for p in probs)
    assert "base-plate: Length typed as a number" in probs and "edge-round: Radius typed as a number" in probs
    assert not any(p.startswith("port") for p in probs)  # a through-all pocket has no length in use


def test_no_parameter_sheet_is_an_error_and_two_a_warning():
    f = clean()
    f["objects"] = f["objects"][1:]
    assert "parameters" in checks(run(f), "error")
    f = clean()
    f["objects"].append(obj("Vars", "App::VarSet", "case-vars", parent=None))
    assert "parameters" in checks(run(f), "warning")


def test_dead_solids_tree_and_broken():
    f = clean()
    f["objects"].append(obj("Shape", "Part::Feature", "top-shell", parent=None))
    f["objects"].append(obj("Pocket", "PartDesign::Pocket", "vent", props={"Type": "ThroughAll"}, state=["Invalid"]))
    f["objects"].append(obj("Chamfer", "PartDesign::Chamfer", "edge", props={"Size": 0.5}, expressions=["Size"],
                            suppressed=True))
    f["objects"][1]["body"]["group"] += ["Pocket", "Chamfer"]
    r = run(f)
    assert checks(r, "error") == ["broken"]
    assert "dead solids" in checks(r, "warning")
    tree = [x["problem"] for x in r["findings"] if x["check"] == "tree"]
    assert any("suppressed" in p for p in tree) and any("past its tip: vent, edge" in p for p in tree)


def test_unused_sketch_is_info():
    f = clean()
    f["objects"].append(sketch("Sketch001", "old-try", users=()))
    assert [(x["severity"], x["object"]) for x in run(f)["findings"]] == [("info", "old-try")]


def test_long_tree():
    f = clean()
    for i in range(structure.LONG_TREE + 1):
        n = f"Fillet{i}"
        f["objects"].append(obj(n, "PartDesign::Fillet", f"round-{i}", props={"Radius": 1.0}, expressions=["Radius"]))
        f["objects"][1]["body"]["group"].append(n)
    f["objects"][1]["body"]["tip"] = n
    assert any("42 features" in x["problem"] for x in run(f)["findings"])


def test_expression_paths_are_normalised():
    o = {"expressions": [".Constraints.width", "Constraints[3]", "Length", ".Placement.Base.x"]}
    assert structure._bound(o) == {"Constraints.width", "Constraints[3]", "Length", "Placement"}


def test_live_report_only_for_a_saved_unchanged_document(tmp_path):
    doc = tmp_path / "bracket.FCStd"
    doc.write_bytes(b"")
    r = structure.from_live(clean() | {"doc": "bracket", "file": str(doc), "modified": True})
    assert r["report"] is None and not (tmp_path / "bracket-structure.json").exists()
    r = structure.from_live(clean() | {"doc": "bracket", "file": str(doc), "modified": False})
    assert r["report"] == str(tmp_path / "bracket-structure.json") and r["ok"]


def test_live_code_reads_and_leaves_no_undo_step():
    code = structure.live_code()
    assert "result = collect(doc)" in code and code.rstrip().endswith("doc.abortTransaction()")
    compile(code, "<live>", "exec")


def test_project_check_flags_missing_stale_and_failing_reports(tmp_path):
    root = tmp_path / "demo"
    project.init(root, tools=["freecad"])
    fc = root / "bracket.FCStd"
    fc.write_bytes(b"")
    msgs = lambda: [f["problem"] for f in project.check(root)["findings"] if "structure" in f["problem"] or "structure" in f["path"]]  # noqa: E731
    assert msgs() == ["its structure has never been checked"]
    rep = root / "bracket-structure.json"
    rep.write_text(json.dumps({"counts": {"error": 1}, "findings": [{"severity": "error", "problem": "sketch x isn't fully constrained"}]}))
    assert any("found 1 error(s)" in m for m in msgs())
    later = time.time() + 5
    os.utime(fc, (later, later))
    assert any(m.startswith("older than bracket.FCStd") for m in msgs())


def test_locate(tmp_path):
    with pytest.raises(structure.StructureError):
        structure.locate(tmp_path)
    (tmp_path / "a.FCStd").write_bytes(b"")
    assert structure.locate(tmp_path).name == "a.FCStd"
    (tmp_path / "b.FCStd").write_bytes(b"")
    with pytest.raises(structure.StructureError, match="several"):
        structure.locate(tmp_path)


# A real document, built and checked by FreeCAD: one body made the way the rules ask, and the same part patched the
# way the rules forbid (a sketch on the pad's top face, bare numbers, default names, a dead solid).
BUILD = r'''
import json, os
import FreeCAD as App
import Part
import Sketcher

V = App.Vector


def rect(sk, w, h):
    pts = [V(-w / 2, -h / 2, 0), V(w / 2, -h / 2, 0), V(w / 2, h / 2, 0), V(-w / 2, h / 2, 0)]
    for i in range(4):
        sk.addGeometry(Part.LineSegment(pts[i], pts[(i + 1) % 4]), False)
    for i in range(4):
        sk.addConstraint(Sketcher.Constraint("Coincident", i, 2, (i + 1) % 4, 1))
    sk.addConstraint(Sketcher.Constraint("Horizontal", 0))
    sk.addConstraint(Sketcher.Constraint("Horizontal", 2))
    sk.addConstraint(Sketcher.Constraint("Vertical", 1))
    sk.addConstraint(Sketcher.Constraint("Vertical", 3))
    sk.addConstraint(Sketcher.Constraint("Symmetric", 0, 1, 2, 1, -1, 1))
    w_i = sk.addConstraint(Sketcher.Constraint("DistanceX", 0, 1, 0, 2, w))
    h_i = sk.addConstraint(Sketcher.Constraint("DistanceY", 1, 1, 1, 2, h))
    return w_i, h_i


def plane(body, role):
    return [f for f in body.Origin.OriginFeatures if getattr(f, "Role", f.Name).startswith(role)][0]


out = os.environ["CADMATE_BUILD_DIR"]

doc = App.newDocument("clean")
params = doc.addObject("Spreadsheet::Sheet", "Params")
for cell, alias, v in (("A1", "width", 40), ("A2", "depth", 30), ("A3", "plate", 3)):
    params.set(cell, str(v))
    params.setAlias(cell, alias)
doc.recompute()
body = doc.addObject("PartDesign::Body", "Body")
body.Label = "base"
sk = body.newObject("Sketcher::SketchObject", "Sketch")
sk.Label = "base-outline"
sk.AttachmentSupport = [(plane(body, "XY_Plane"), "")]
sk.MapMode = "FlatFace"
w_i, h_i = rect(sk, 40, 30)
sk.renameConstraint(w_i, "width")
sk.renameConstraint(h_i, "depth")
sk.setExpression(".Constraints.width", "Params.width")
sk.setExpression(".Constraints.depth", "Params.depth")
pad = body.newObject("PartDesign::Pad", "Pad")
pad.Label = "base-plate"
pad.Profile = sk
pad.setExpression("Length", "Params.plate")
doc.recompute()
doc.saveAs(os.path.join(out, "clean.FCStd"))

doc = App.newDocument("messy")
body = doc.addObject("PartDesign::Body", "Body")
sk = body.newObject("Sketcher::SketchObject", "Sketch")
sk.AttachmentSupport = [(plane(body, "XY_Plane"), "")]
sk.MapMode = "FlatFace"
rect(sk, 40, 30)
pad = body.newObject("PartDesign::Pad", "Pad")
pad.Profile = sk
pad.Length = 3
doc.recompute()
top = [f"Face{i + 1}" for i, f in enumerate(pad.Shape.Faces) if abs(f.CenterOfMass.z - 3) < 1e-6][0]
sk2 = body.newObject("Sketcher::SketchObject", "Sketch001")
sk2.AttachmentSupport = [(pad, top)]
sk2.MapMode = "FlatFace"
sk2.addGeometry(Part.Circle(V(0, 0, 0), V(0, 0, 1), 4), False)
pocket = body.newObject("PartDesign::Pocket", "Pocket")
pocket.Profile = sk2
pocket.Length = 1
doc.addObject("Part::Feature", "Shape").Shape = Part.makeBox(5, 5, 5)
doc.recompute()
doc.saveAs(os.path.join(out, "messy.FCStd"))

with open(os.environ["CADMATE_OUT"], "w") as f:
    json.dump({"top": top}, f)
'''


@pytest.mark.skipif(not HAS_FREECAD, reason="needs FreeCAD")
def test_real_documents(tmp_path):
    script = tmp_path / "build.py"
    script.write_text(BUILD)
    apps.run_freecad_script(script, env={"CADMATE_BUILD_DIR": str(tmp_path)})

    good = structure.check(tmp_path / "clean.FCStd")
    assert good["ok"], good["findings"]
    assert [f for f in good["findings"] if f["severity"] != "info"] == []
    assert good["summary"]["parameters"]["bound"] == 3
    assert (tmp_path / "clean-structure.json").exists()

    bad = structure.check(tmp_path / "messy.FCStd")
    assert not bad["ok"]
    errs = {(f["check"], f["object"]) for f in bad["findings"] if f["severity"] == "error"}
    assert ("attachment", "Sketch001") in errs
    assert ("constraints", "Sketch001") in errs  # the circle has no constraints
    assert ("parameters", None) in errs  # no Params at all
    warns = {f["check"] for f in bad["findings"] if f["severity"] == "warning"}
    assert {"names", "dead solids", "parameters"} <= warns

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
        sketch("Sketch", "base-outline", dims=[dim(8, "width"), dim(9, "height")],
               expressions=[[".Constraints.width", "Params.width"], [".Constraints.height", "<<Params>>.depth"]]),
        obj("Pad", "PartDesign::Pad", "base-plate", props={"Length": 3.0, "Type": "Length"},
            expressions=[["Length", "Params.plate"]], in_list=[["Body", "PartDesign::Body"]], volume=3600.0),
    ]}


def run(facts):
    return structure.evaluate(facts)


def checks(r, sev=None):
    return [f["check"] for f in r["findings"] if sev is None or f["severity"] == sev]


def test_a_clean_part_passes():
    r = run(clean())
    assert r["ok"] and r["findings"] == []
    assert r["summary"]["parameters"] == {"sources": ["Params"], "driving_values": 3, "bound": 3, "named_local": 0}
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
    f["objects"][3]["expressions"] = [["Length", "7"]]  # a constant typed as an expression is still a bare number
    f["objects"][2]["sketch"]["dimensions"] += [dim(10), dim(11, "slot_r")]
    f["objects"].append(obj("Fillet", "PartDesign::Fillet", "edge-round", props={"Radius": 1.0},
                            expressions=[["Radius", "base_outline.Constraints.width / 40"]]))
    f["objects"].append(obj("Pocket", "PartDesign::Pocket", "port", props={"Length": 5.0, "Type": "ThroughAll"}))
    r = run(f)
    probs = [x["problem"] for x in r["findings"] if x["check"] == "parameters"]
    assert any("1 bare dimension" in p for p in probs) and any("named local dimension(s): slot_r" in p for p in probs)
    assert "base-plate: Length not from Params" in probs and "edge-round: Radius not from Params" in probs
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
    f["objects"].append(obj("Chamfer", "PartDesign::Chamfer", "edge", props={"Size": 0.5},
                            expressions=[["Size", "Params.chamfer"]], suppressed=True))
    f["objects"][1]["body"]["group"] += ["Pocket", "Chamfer"]
    r = run(f)
    assert checks(r, "error") == ["broken"]
    assert "dead solids" in checks(r, "warning")
    tree = [x["problem"] for x in r["findings"] if x["check"] == "tree"]
    assert any("suppressed" in p for p in tree) and any("past its tip: vent, edge" in p for p in tree)


SK = "Sketcher::SketchObject"


def test_unused_sketch_is_info():
    f = clean()
    f["objects"].append(sketch("Sketch001", "old-try", users=()))
    assert [(x["severity"], x["object"]) for x in run(f)["findings"]] == [("info", "old-try")]


def test_long_tree():
    f = clean()
    for i in range(structure.LONG_TREE + 1):
        n = f"Fillet{i}"
        f["objects"].append(obj(n, "PartDesign::Fillet", f"round-{i}", props={"Radius": 1.0},
                                expressions=[["Radius", "Params.r"]]))
        f["objects"][1]["body"]["group"].append(n)
    f["objects"][1]["body"]["tip"] = n
    assert any("42 features" in x["problem"] for x in run(f)["findings"])


def test_expression_paths_are_normalised_and_must_read_params():
    o = {"expressions": [[".Constraints.width", "Params.w"], ["Constraints[3]", "<<Params>>.h * 2"], ["Length", "7"],
                         [".AttachmentOffset.Base.z", "-Params.d / 2"], ["Offset", "Paramsx.d"]]}
    src = [{"name": "Spreadsheet", "label": "Params"}]
    assert structure._bound(o, src) == {"Constraints.width", "Constraints[3]", "AttachmentOffset.Base.z"}
    assert structure._is_bound("AttachmentOffset.Base.z", {"AttachmentOffset"})
    assert not structure._is_bound("AttachmentOffset.Base.x", {"AttachmentOffset.Base.z"})


def test_typed_attachment_offset_is_a_bare_number():
    f = clean()
    f["objects"][2]["offset"] = [0, 0, 5.0, 0]
    f["objects"].append(sketch("Sketch001", "lid-lip", offset=[0, 0, 4.0, 0],
                               expressions=[[".AttachmentOffset.Base.z", "Params.lid_z"]]))
    probs = [x["problem"] for x in run(f)["findings"] if x["check"] == "parameters"]
    assert probs == ["base-outline: AttachmentOffset.Base.z not from Params"]


def test_external_geometry_and_binders_from_generated_geometry():
    f = clean()
    f["objects"].append(sketch("Sketch001", "boss", support=[["XY_Plane", "App::Plane", []]]))
    f["objects"][-1]["sketch"]["external"] = [["Pad", "PartDesign::Pad", ["Edge4"]], ["Sketch", SK, ["Edge1"]]]
    f["objects"].append(obj("Binder", "PartDesign::SubShapeBinder", "lid-outline",
                            binder_source=[["Pad", "PartDesign::Pad", ["Face6"]]]))
    f["objects"].append(obj("Binder001", "PartDesign::SubShapeBinder", "layout-copy",
                            binder_source=[["Sketch", SK, []]]))
    errs = [(x["check"], x["object"]) for x in run(f)["findings"] if x["severity"] == "error"]
    assert errs == [("references", "boss"), ("references", "lid-outline")]


def test_a_feature_that_changes_nothing_is_an_error():
    f = clean()
    f["objects"].append(obj("Pocket", "PartDesign::Pocket", "usb-port", props={"Type": "ThroughAll"},
                            base_feature="Pad", volume=3600.0))
    f["objects"].append(obj("Pocket001", "PartDesign::Pocket", "vent", props={"Type": "ThroughAll"},
                            base_feature="Pocket", volume=3500.0))
    f["objects"][1]["body"]["group"] += ["Pocket", "Pocket001"]
    f["objects"][1]["body"]["tip"] = "Pocket001"
    errs = [(x["check"], x["object"]) for x in run(f)["findings"] if x["severity"] == "error"]
    assert errs == [("no effect", "usb-port")]


def test_fillets_and_chamfers_come_last():
    f = clean()
    f["objects"].append(obj("Fillet", "PartDesign::Fillet", "edge-round", props={"Radius": 1.0},
                            expressions=[["Radius", "Params.r"]]))
    f["objects"].append(obj("Pocket", "PartDesign::Pocket", "port", props={"Type": "ThroughAll"}))
    f["objects"][1]["body"]["group"] += ["Fillet", "Pocket"]
    f["objects"][1]["body"]["tip"] = "Pocket"
    order = [x["problem"] for x in run(f)["findings"] if x["check"] == "order"]
    assert order == ["body base: port come(s) after the fillet or chamfer edge-round"]


def test_threaded_holes_and_spacing_patterns_are_not_false_positives():
    f = clean()
    f["objects"].append(obj("Hole", "PartDesign::Hole", "m2-insert",
                            props={"Diameter": 1.6, "Depth": 4.0, "DepthType": "Dimension", "Threaded": True,
                                   "ThreadType": "ISOMetricProfile"}, expressions=[["Depth", "Params.insert_d"]]))
    f["objects"].append(obj("LinearPattern", "PartDesign::LinearPattern", "vents",
                            props={"Mode": "Spacing", "Length": 100.0, "Offset": 4.0, "Occurrences": 5.0},
                            expressions=[["Offset", "Params.vent_pitch"], ["Occurrences", "Params.vents"]]))
    assert [x for x in run(f)["findings"] if x["check"] == "parameters"] == []


def test_part_workbench_csg_is_reported():
    f = clean()
    f["objects"].append(obj("Box", "Part::Box", "shell-block", parent=None, props={"Length": 10.0}))
    f["objects"].append(obj("Cut", "Part::Cut", "shell", parent=None))
    assert any(x["check"] == "part workbench" and "2 Part-workbench" in x["problem"] for x in run(f)["findings"])


def test_live_report_only_for_a_saved_unchanged_document(tmp_path):
    doc = tmp_path / "bracket.FCStd"
    doc.write_bytes(b"")
    r = structure.from_live(clean() | {"doc": "bracket", "file": str(doc), "modified": True})
    assert r["report"] is None and not (tmp_path / "bracket-structure.json").exists()
    r = structure.from_live(clean() | {"doc": "bracket", "file": str(doc), "modified": False})
    assert r["report"] == str(tmp_path / "bracket-structure.json") and r["ok"]


def test_live_code_compiles_and_only_collects():
    code = structure.live_code()
    assert code.rstrip().endswith("result = collect(doc)") and "Transaction" not in code
    compile(code, "<live>", "exec")


def test_bridge_read_op_runs_code_without_transaction_or_recompute(monkeypatch):
    """The live check must not add an undo step or recompute anything, even with a broken document open."""
    import sys
    import types

    from test_live import FakeApp, load_bridge

    calls = []

    class Doc:
        Name = "case"

        def __getattr__(self, n):  # openTransaction, recompute, commitTransaction... any call is recorded
            return lambda *a, **k: calls.append(n)

    d = Doc()
    fc = types.SimpleNamespace(ActiveDocument=d, Vector=lambda *a: a, getDocument=lambda n: d,
                               listDocuments=lambda: {"case": d})
    monkeypatch.setitem(sys.modules, "FreeCAD", fc)
    monkeypatch.setitem(sys.modules, "FreeCADGui", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "Part", types.SimpleNamespace())
    bridge = load_bridge().Bridge(FakeApp, "unix")
    monkeypatch.setattr(bridge, "_doc", lambda name: d)
    r = bridge._handle({"op": "read", "code": "result = {'doc': doc.Name}", "label": "read model structure"})
    assert r["ok"] and r["result"] == {"doc": "case"} and calls == []
    bad = bridge._handle({"op": "read", "code": "raise ValueError('nope')"})
    assert bad["ok"] is False and "nope" in bad["error"] and calls == []


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
sk3 = body.newObject("Sketcher::SketchObject", "Sketch002")
sk3.AttachmentSupport = [(plane(body, "XY_Plane"), "")]
sk3.MapMode = "FlatFace"
rect(sk3, 4, 4)
inside = body.newObject("PartDesign::Pad", "Pad001")  # inside the plate: adds nothing
inside.Profile = sk3
inside.Length = 1
sk4 = body.newObject("Sketcher::SketchObject", "Sketch003")  # over-constrained: the symmetry already makes it level
sk4.AttachmentSupport = [(plane(body, "XY_Plane"), "")]
sk4.MapMode = "FlatFace"
rect(sk4, 6, 6)
sk4.addConstraint(Sketcher.Constraint("Symmetric", 0, 1, 0, 2, -2))
empty = body.newObject("PartDesign::Pad", "Pad002")
empty.Profile = sk4
empty.Length = 2
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
    assert ("no effect", "Pad001") in errs
    over = [f["problem"] for f in bad["findings"] if f["object"] == "Sketch003" and f["check"] == "constraints"]
    assert over and ("over-constrained" in over[0] or "conflicting" in over[0]), over
    assert ("broken", "Pad002") in errs  # produced no solid, though FreeCAD only marks it Touched
    warns = {f["check"] for f in bad["findings"] if f["severity"] == "warning"}
    assert {"names", "dead solids", "parameters"} <= warns


def test_a_feature_with_no_solid_is_an_error():
    f = clean()
    f["objects"][3]["solids"] = 0
    f["objects"][3]["state"] = ["Touched"]
    assert [(x["check"], x["object"]) for x in run(f)["findings"] if x["severity"] == "error"] == [("broken", "base-plate")]


def test_over_constrained_and_conflicting_sketches_say_which_constraints():
    f = clean()
    sk = f["objects"][2]
    sk["state"] = ["Touched", "Invalid"]
    sk["sketch"] |= {"fully_constrained": False, "solver": {"redundant": [5], "conflicting": [], "malformed": []},
                     "constraint_types": ["Coincident"] * 4 + ["Horizontal", "Vertical"]}
    errs = [x["problem"] for x in run(f)["findings"] if x["severity"] == "error"]
    assert errs == ["sketch base-outline is over-constrained: 5 (Horizontal) is redundant or malformed"]
    sk["sketch"]["solver"] = {"redundant": [], "conflicting": [5, 6]}
    errs = [x["problem"] for x in run(f)["findings"] if x["severity"] == "error"]
    assert errs == ["sketch base-outline has conflicting constraints: 5 (Horizontal), 6 (Vertical)"]

import json
import os
import time

import pytest

from cadmate import apps, enclosure, project

HAS_FREECAD = apps.find_freecadcmd() is not None


def opening(centre, face="side", near=(), size=(2.0, 1.6, 4.0)):
    box = [c - s / 2 for c, s in zip(centre, size)] + [c + s / 2 for c, s in zip(centre, size)]
    return {"box": box, "outer_box": box, "centre": list(centre), "face": face, "normal": [1, 0, 0],
            "outer_area": 8.0, "near": list(near)}


def raw(openings=(), sealed=True, joints=(), inter=(), tops=None):
    tops = tops or {"box-lid": 12.0, "box-bottom": 6.0}
    return {
        "parts": {n: {"bbox": [0, 0, 0, 50, 30, z], "volume": 1.0, "solids": 1} for n, z in tops.items()} | {"board": {"bbox": [2, 2, 2, 48, 28, 3.6]}},
        "missing_case_parts": [],
        "interference": list(inter),
        "enclosures": {"box": {"parts": ["box-bottom", "box-lid"], "interior_volume": 9000.0,
                               "openings": list(openings), "exact": {"sealed": sealed}}},
        "joints": list(joints),
    }


def run(r, spec=None):
    spec = spec or {}
    resolved = enclosure.DEFAULTS | {"case_parts": ["box-bottom", "box-lid"]} | {k: v for k, v in spec.items() if k in enclosure.DEFAULTS}
    return enclosure.evaluate(r, spec, resolved)["findings"]


def test_parts_group_into_shells_by_name():
    assert enclosure.group_enclosures(["main-top", "main-base", "probe-top", "probe-base", "knob"]) == {
        "main": ["main-top", "main-base"], "probe": ["probe-top", "probe-base"], "knob": ["knob"]}
    assert enclosure.group_enclosures(["top", "base", "front-panel"], "box") == {"box": ["top", "base", "front-panel"]}


def test_undeclared_opening_is_an_error_and_a_declared_one_is_not():
    usb = opening([50, 15, 4], near=["board"])
    slot = opening([25, 30, 8], size=(6, 1.6, 1))
    found = run(raw([usb, slot]), {"openings": [{"name": "USB-C", "near": "board", "face": "side"}]})
    assert [f["check"] for f in found if f["severity"] == "error"] == ["closed shell"]
    assert "[25, 30, 8]" in found[0]["problem"] and usb["intended"] == "USB-C" and slot["intended"] is None


def test_a_name_alone_matches_nothing_and_counts_are_checked():
    keys = [opening([10 + 15 * i, 15, 12], face="top") for i in range(3)]
    found = run(raw(keys), {"openings": [{"name": "keys", "face": "top", "count": 4}, {"name": "ghost"}]})
    probs = [f["problem"] for f in found]
    assert any("3 openings found, 4 expected" in p for p in probs)
    assert any("'ghost' wasn't found" in p for p in probs)
    assert not [f for f in found if f["severity"] == "error"]


def test_leak_the_voxels_missed_is_reported():
    found = run(raw(sealed=False))
    assert found[0]["severity"] == "error" and "narrower than" in found[0]["problem"]


def test_collisions_respect_allow_overlap_and_tolerance():
    inter = [{"a": "box-lid", "b": "board", "volume": 3.2, "at": [1, 1, 3, 2, 2, 3.5]},
             {"a": "box-bottom", "b": "board", "volume": 0.002},
             {"a": "box-lid", "b": "switch", "volume": 5.0}]
    found = run(raw(inter=inter), {"allow_overlap": [["switch", "box-lid"]]})
    errs = [f for f in found if f["check"] == "collision"]
    assert len(errs) == 1 and errs[0]["parts"] == ["box-lid", "board"] and "3.2 mm³" in errs[0]["problem"]


def test_joint_gap_and_step_use_limits_per_joint():
    samples = [{"at": [0, 0, 12], "normal": [0, 0, 1], "gap": 0.05, "step": 0.0},
               {"at": [5, 0, 12], "normal": [0, 0, 1], "gap": 0.8, "step": -0.6}]
    j = {"a": "box-lid", "b": "box-bottom", "seam_voxels": 40, "samples": samples}
    found = run(raw(joints=[j]))
    probs = sorted(f["problem"] for f in found if f["check"] == "flush joint")
    assert len(probs) == 2 and probs[0].startswith("box-bottom stands 0.60 mm proud") and "gap up to 0.80" in probs[1]
    assert not run(raw(joints=[j]), {"joints": {"box-bottom/box-lid": {"skip": True}}})
    found = run(raw(joints=[j]), {"joints": {"box-lid/box-bottom": {"max_step": 1.0, "max_gap": 1.0}}})
    assert not found


def test_heights_from_the_brief():
    found = run(raw(), {"heights": {"box-lid": 11.1, "box-bottom": 6.0, "nope": 1}})
    assert [f["severity"] for f in found] == ["error", "warning"] and "12.00" in found[0]["problem"]


def test_project_check_knows_about_the_enclosure_report(tmp_path):
    root = tmp_path / "kb"
    project.init(root, tools=["kicad", "freecad"], parts=["enclosure", "knob"])
    (root / "freecad/knob/knob.step").write_text("x")  # a part with no assembly isn't an enclosure to check
    (root / "freecad/enclosure/box-lid.step").write_text("x")
    (root / "freecad/enclosure/assembly.step").write_text("x")
    paths = lambda: {f["path"]: f for f in project.check(root)["findings"]}  # noqa: E731
    rel = "freecad/enclosure/enclosure-check.json"
    assert paths()[rel]["severity"] == "info" and not any("knob" in p for p in paths())
    rep = root / rel
    rep.write_text(json.dumps({"counts": {"error": 1}, "findings": [{"severity": "error", "problem": "slot in the back"}]}))
    assert "slot in the back" in paths()[rel]["problem"]
    rep.write_text(json.dumps({"counts": {"error": 0}, "findings": []}))
    assert rel not in paths()
    later = time.time() + 5
    os.utime(root / "freecad/enclosure/box-lid.step", (later, later))
    assert "older than box-lid.step" in paths()[rel]["problem"]
    # the project finds the assembly when only one part has one, and asks when several do
    assert enclosure.locate(root)[1] == (root / "freecad/enclosure").resolve()
    (root / "freecad/knob/assembly.step").write_text("x")
    with pytest.raises(enclosure.EnclosureError, match="several parts"):
        enclosure.locate(root)


def test_a_freecad_only_project_with_one_part_keeps_it_at_the_root(tmp_path):
    root = tmp_path / "bracket"
    project.init(root, tools="freecad")
    (root / "assembly.step").write_text("x")
    (root / "bracket.step").write_text("x")
    assert enclosure.locate(root) == ((root / "assembly.step").resolve(), root.resolve())
    assert enclosure._part_name(root) == "bracket"
    assert "enclosure-check.json" in {f["path"] for f in project.check(root)["findings"]}


BUILD = r'''
import FreeCAD as App, Part, Import, os
V = App.Vector
doc = App.newDocument("t")
outer = Part.makeBox(60, 40, 6)
bottom = outer.cut(Part.makeBox(56, 36, 6, V(2, 2, 2)))
lid = Part.makeBox(60, 40, 2, V(0, 0, 6)).cut(Part.makeBox(56, 36, 1, V(2, 2, 6)))
lid = lid.fuse(Part.makeBox(10, 10, 0.6, V(25, 15, 8)))  # a boss standing 0.6 proud: not a joint, just a feature
bottom = bottom.cut(Part.makeBox(4, 2, 2, V(-1, 15, 3)))  # a slot through the x = 0 wall
slab = Part.makeBox(30, 20, 1.6, V(15, 10, 2.5))
objs = {}
for n, s in (("box-bottom", bottom), ("box-lid", lid), ("board", slab), ("plug", Part.makeBox(3, 3, 3, V(56, 30, 2.5)))):
    o = objs[n] = doc.addObject("Part::Feature", n.replace("-", "_"))
    o.Label, o.Shape = n, s
part = doc.addObject("App::Part", "assembly")
for o in list(doc.Objects):
    if o is not part:
        part.addObject(o)
d = os.environ["OUT_DIR"]
Import.export([part], os.path.join(d, "assembly.step"))
for n in ("box-bottom", "box-lid"):
    Part.export([objs[n]], os.path.join(d, n + ".step"))
'''


@pytest.mark.skipif(not HAS_FREECAD, reason="needs FreeCAD")
def test_a_box_with_a_slot_and_a_collision(tmp_path):
    import subprocess

    case = tmp_path / "case"
    case.mkdir()
    script = tmp_path / "build.py"
    script.write_text(BUILD)
    subprocess.run([apps.find_freecadcmd(), str(script)], env=os.environ | {"OUT_DIR": str(case)}, check=True, capture_output=True)
    r = enclosure.check(case, {"resolution": 0.5})
    errs = [f for f in r["findings"] if f["severity"] == "error"]
    shell = [f for f in errs if f["check"] == "closed shell"]
    assert len(shell) == 1 and "side" in shell[0]["problem"], r["findings"]
    assert -1.5 < shell[0]["at"][0] < 2.5 and 14 < shell[0]["at"][1] < 18
    assert [f["parts"] for f in errs if f["check"] == "collision"] == [["box-bottom", "plug"]]
    assert not [f for f in errs if f["check"] == "flush joint"], r["summary"]["joints"]
    assert r["summary"]["enclosures"]["box"]["sealed"] is False
    assert (case / "enclosure-check.json").exists()
    # with the slot declared and the overlap allowed, nothing is left
    r = enclosure.check(case, {"openings": [{"name": "slot", "face": "side", "box": [-2, 14, 2, 3, 18, 6]}],
                               "allow_overlap": [["box-bottom", "plug"]]})
    assert r["ok"], r["findings"]
    assert r["summary"]["enclosures"]["box"]["sealed"] is True

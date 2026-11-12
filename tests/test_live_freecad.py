"""The bridge's edits in a real FreeCAD: an edit lands in the documents it is about and nowhere else."""
import json
from pathlib import Path

import pytest

from cadmate import apps

HAS_FREECAD = apps.find_freecadcmd() is not None
BRIDGE = Path(__file__).parent.parent / "src" / "cadmate" / "freecad_scripts" / "live_bridge.py"

# Runs under FreeCADCmd. "broken" stands for a document the user has open with a feature that won't recompute: a
# fillet too big for its edge stays Touched and Invalid, so every recompute of that document runs it again.
SCRIPT = r'''
import importlib.util, json, os
import FreeCAD as App

spec = importlib.util.spec_from_file_location("live_bridge", os.environ["CADMATE_BRIDGE"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

broken = App.newDocument("broken")
body = broken.addObject("PartDesign::Body", "Body")
box = broken.addObject("PartDesign::AdditiveBox", "Box")
body.addObject(box)
broken.recompute()
fillet = broken.addObject("PartDesign::Fillet", "Fillet")
body.addObject(fillet)
fillet.Base = (box, ["Edge1"])
fillet.Radius = 50
broken.recompute()
other = App.newDocument("other")
work = App.newDocument("work")
for d in (broken, other, work):
    d.UndoMode = 1  # as in the GUI
    d.clearUndos()

# FreeCAD 1.1 records an open transaction in every document that changes before it is committed; newer builds keep
# it to the document it was opened on. Find out which this one does, so the checks below hold each to its own rule.
probe_a, probe_b = App.newDocument("probe_a"), App.newDocument("probe_b")
for d in (probe_a, probe_b):
    d.UndoMode = 1
probe_a.openTransaction("probe")
probe_b.addObject("Part::Box", "Box")
probe_a.commitTransaction()
app_wide = probe_b.UndoCount > 0
App.closeDocument("probe_a")
App.closeDocument("probe_b")

bridge = mod.Bridge(App, "unix")
App.addDocumentObserver(bridge.observer)
out = {"fillet_before": list(fillet.State), "app_wide": app_wide}

def call(label, doc, code):
    before = bridge.seq
    r = bridge._handle({"op": "run", "label": label, "doc": doc, "code": code})
    docs = sorted({e["doc"] for e in bridge.events if e["seq"] > before})
    return {"ok": r.get("ok"), "error": r.get("error"), "touched": r.get("touched"), "event_docs": docs}

App.setActiveDocument("work")
out["edits"] = [call(f"edit {i}", "work", f"b = doc.addObject('Part::Box', 'B{i}'); b.Length = {i + 1}")
                for i in range(3)]
out["after_edits"] = {d.Name: list(d.UndoNames) for d in (broken, other, work)}
out["fillet_after"] = list(fillet.State)
out["both"] = call("both", "work", "doc.addObject('Part::Box', 'W'); App.getDocument('other').addObject('Part::Box', 'O')")
out["other_box_state"] = list(other.getObject("O").State)
out["fix"] = call("fix", "broken", "doc.getObject('Fillet').Radius = 1")
out["fillet_fixed"] = list(fillet.State)
out["after_all"] = {d.Name: list(d.UndoNames) for d in (broken, other, work)}
App.removeDocumentObserver(bridge.observer)
json.dump(out, open(os.environ["CADMATE_OUT"], "w"))
'''


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    script = tmp_path_factory.mktemp("bridge") / "edits.py"
    script.write_text(SCRIPT)
    return apps.run_freecad_script(script, env={"CADMATE_BRIDGE": str(BRIDGE)})


@pytest.mark.skipif(not HAS_FREECAD, reason="needs FreeCAD")
def test_edit_leaves_other_open_documents_alone(run):
    """An edit to one document must not recompute another open one: FreeCAD's transaction is application-wide, so a
    document with a failed feature would pick up an undo step for every edit cadmate makes anywhere."""
    assert "Invalid" in run["fillet_before"], run  # the setup really has a broken feature
    assert all(e["ok"] for e in run["edits"]), run["edits"]
    # On FreeCAD 1.1 a recompute of the broken document here adds an undo step to it; on newer builds it doesn't, but
    # the failed fillet still runs again and its changes are logged as cadmate's, which event_docs catches.
    assert run["after_edits"]["broken"] == []
    assert run["after_edits"]["other"] == []
    assert run["after_edits"]["work"] == ["cadmate: edit 2", "cadmate: edit 1", "cadmate: edit 0"]
    assert all(e["event_docs"] == ["work"] for e in run["edits"]), run["edits"]
    assert all(e["touched"] == [f"B{i}"] for i, e in enumerate(run["edits"])), run["edits"]
    assert run["fillet_after"] == run["fillet_before"]  # not run again


@pytest.mark.skipif(not HAS_FREECAD, reason="needs FreeCAD")
def test_edit_recomputes_every_document_its_code_changed(run):
    assert run["both"]["ok"], run["both"]
    assert "Touched" not in run["other_box_state"], run  # the other document it wrote to was recomputed
    # The change there can be undone where FreeCAD records it (1.1); newer builds keep the step to the target document.
    assert run["after_all"]["other"] == (["cadmate: both"] if run["app_wide"] else []), run
    assert run["after_all"]["broken"] == ["cadmate: fix"]  # an edit aimed at the broken document still lands there
    assert "Invalid" not in run["fillet_fixed"], run

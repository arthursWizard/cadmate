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
    try:
        r = bridge._handle({"op": "run", "label": label, "doc": doc, "code": code})
    except Exception as e:  # the bridge's queue turns this into an error reply, so it counts as one
        r = {"ok": False, "error": f"bridge raised {type(e).__name__}: {e}"}
    docs = sorted({e["doc"] for e in bridge.events if e["seq"] > before})
    return {"ok": r.get("ok"), "error": r.get("error"), "touched": r.get("touched"), "event_docs": docs,
            "doc": r.get("doc"), "closed": r.get("closed")}

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
out["active_after_fix"] = App.ActiveDocument.Name
# The user undoes cadmate's last edit in their document, then cadmate edits another one in the background.
work.undo()
out["background"] = call("background", "other", "doc.addObject('Part::Box', 'P')")
out["active_after_background"] = App.ActiveDocument.Name
out["background_undo"] = {d.Name: {"undo": list(d.UndoNames), "redo": list(d.RedoNames)} for d in (other, work)}
# Code that makes another document active on purpose keeps it that way.
out["switch"] = call("switch", "other", "App.setActiveDocument('broken')")
out["active_after_switch"] = App.ActiveDocument.Name
App.setActiveDocument("work")
out["failed"] = call("failed", "other", "doc.addObject('Part::Box', 'Q'); raise ValueError('nope')")
out["active_after_failed"] = App.ActiveDocument.Name
out["failed_undo"] = {d.Name: list(d.UndoNames) for d in (other, work)}
# Code that closes the document it was given: the close is what was asked, so it's a success.
for name in ("scratch", "scratch_active", "scratch_failed"):
    App.newDocument(name).UndoMode = 1
App.setActiveDocument("work")
work_undo = list(work.UndoNames)
out["close"] = call("close", "scratch", "App.closeDocument(doc.Name)")
out["close_after"] = {"active": App.ActiveDocument.Name, "documents": sorted(App.listDocuments()),
                      "work_undo_same": list(work.UndoNames) == work_undo}
App.setActiveDocument("scratch_active")
out["close_active"] = call("close active", "scratch_active",
                           "App.getDocument('other').addObject('Part::Box', 'R'); App.closeDocument(doc.Name)")
out["close_active_after"] = {"documents": sorted(App.listDocuments()),
                             "other_box_state": list(other.getObject("R").State) if other.getObject("R") else None}
App.setActiveDocument("work")
out["close_failed"] = call("close failed", "scratch_failed", "App.closeDocument(doc.Name); raise ValueError('after close')")
out["close_failed_after"] = {"active": App.ActiveDocument.Name, "documents": sorted(App.listDocuments())}
out["close_then_edit"] = call("after close", "work", "doc.addObject('Part::Box', 'Z')")
out["close_then_edit_undo"] = list(work.UndoNames)[:1]
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


@pytest.mark.skipif(not HAS_FREECAD, reason="needs FreeCAD")
def test_background_edit_leaves_no_entry_in_the_active_document(run):
    """FreeCAD 1.1 marks the active document's undo list ("-> cadmate: ...") when another document opens a
    transaction. An edit to a document in the background must leave the user's own undo and redo lists as they were."""
    assert run["active_after_fix"] == "work"
    assert run["background"]["ok"], run["background"]
    assert run["active_after_background"] == "work"
    work = run["background_undo"]["work"]
    assert not [n for n in work["undo"] + run["after_all"]["work"] if n.startswith("->")], run
    assert work["redo"] == ["cadmate: both"]  # the user's undo is still there to redo
    assert run["background_undo"]["other"]["undo"][0] == "cadmate: background"


@pytest.mark.skipif(not HAS_FREECAD, reason="needs FreeCAD")
def test_active_document_after_a_switch_or_a_failed_edit(run):
    assert run["switch"]["ok"] and run["active_after_switch"] == "broken"  # the code's own choice stands
    assert run["failed"]["ok"] is False and "nope" in run["failed"]["error"]
    assert run["active_after_failed"] == "work"
    assert not [n for n in run["failed_undo"]["work"] if n.startswith("->")], run
    assert "cadmate: failed" not in run["failed_undo"]["other"]


@pytest.mark.skipif(not HAS_FREECAD, reason="needs FreeCAD")
def test_edit_whose_code_closes_its_own_document(run):
    """Closing the target document is something an edit may ask for: it succeeds, says so, and leaves the rest as an
    edit would, with no transaction left open behind it."""
    close, after = run["close"], run["close_after"]
    assert close["ok"], close
    assert close["closed"] == "scratch" and close["doc"] == "work", close
    assert "scratch" not in after["documents"] and after["active"] == "work" and after["work_undo_same"], after

    # the active document closed, after the code changed another one: that one is still recomputed
    assert run["close_active"]["ok"], run["close_active"]
    assert run["close_active"]["closed"] == "scratch_active"
    assert "scratch_active" not in run["close_active_after"]["documents"]
    assert "Touched" not in run["close_active_after"]["other_box_state"], run["close_active_after"]

    # code that closes its document and then fails reports its own error, not one about the closed document
    failed = run["close_failed"]
    assert failed["ok"] is False and "after close" in failed["error"], failed
    assert run["close_failed_after"]["active"] == "work"

    # and the next edit is its own undo step as usual
    assert run["close_then_edit"]["ok"], run["close_then_edit"]
    assert run["close_then_edit_undo"] == ["cadmate: after close"]

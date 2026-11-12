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
            "doc": r.get("doc"), "closed": r.get("closed"), "reopened": r.get("reopened"), "note": r.get("note"),
            "undo_step_left": r.get("undo_step_left"), "check": r.get("check")}

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
other_undo = list(other.UndoNames)
out["close_failed"] = call("close failed", "scratch_failed", "App.getDocument('other').addObject('Part::Box', 'S'); "
                           "App.closeDocument(doc.Name); raise ValueError('after close')")
out["close_failed_after"] = {"active": App.ActiveDocument.Name, "documents": sorted(App.listDocuments()),
                             "other_box": other.getObject("S") is not None,
                             "other_undo_new": list(other.UndoNames)[:len(other.UndoNames) - len(other_undo)],
                             "other_redo": list(other.RedoNames)}
# A reload from disk: the code closes its document and opens the saved file again, under the same name.
saved = App.newDocument("saved")
saved.UndoMode = 1
saved.addObject("Part::Box", "Kept")
saved.recompute()
saved.saveAs(os.path.join(os.environ["CADMATE_TMP"], "saved.FCStd"))
saved.clearUndos()
App.setActiveDocument("work")
work_undo = list(work.UndoNames)
out["reload"] = call("reload", "saved", "path = doc.FileName; App.closeDocument(doc.Name); App.openDocument(path)")
out["reload_after"] = {"active": App.ActiveDocument.Name, "work_undo_same": list(work.UndoNames) == work_undo,
                       "objects": [o.Name for o in App.getDocument("saved").Objects],
                       "undo": list(App.getDocument("saved").UndoNames)}
out["close_then_edit"] = call("after close", "work", "doc.addObject('Part::Box', 'Z')")
out["close_then_edit_undo"] = list(work.UndoNames)[:1]
App.removeDocumentObserver(bridge.observer)
json.dump(out, open(os.environ["CADMATE_OUT"], "w"))
'''


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    script = tmp_path_factory.mktemp("bridge") / "edits.py"
    script.write_text(SCRIPT)
    return apps.run_freecad_script(script, env={"CADMATE_BRIDGE": str(BRIDGE), "CADMATE_TMP": str(script.parent)})


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

    # code that changes another document, closes its own and then fails. The close can't be rolled back, and on 1.1 it
    # already committed the change in the other document, so the reply says what's left there instead of undoing it
    # (an undo would leave the failed edit in the user's redo list)
    failed, after = run["close_failed"], run["close_failed_after"]
    assert failed["ok"] is False and "after close" in failed["error"], failed
    assert failed["closed"] == "scratch_failed" and "can't be undone" in failed["note"], failed
    assert after["active"] == "work" and after["other_redo"] == [], after
    if after["other_box"]:  # left in place: named, as an undo step where FreeCAD recorded one
        if after["other_undo_new"] == ["cadmate: close failed"]:
            assert failed["undo_step_left"] == ["other"] and not failed["check"], (failed, after)
        else:
            assert after["other_undo_new"] == [] and failed["check"] == ["other"], (failed, after)

    # a reload from disk: same name, a new document; the edit succeeds and says so
    reload, after = run["reload"], run["reload_after"]
    assert reload["ok"], reload
    assert reload["closed"] == "saved" and reload["reopened"] == "saved", reload
    assert after["objects"] == ["Kept"] and after["undo"] == [], after
    assert after["active"] == "work" and after["work_undo_same"], after

    # and the next edit is its own undo step as usual
    assert run["close_then_edit"]["ok"], run["close_then_edit"]
    assert run["close_then_edit_undo"] == ["cadmate: after close"]



# Code that closes its target and then fails, in the orders a review tried on FreeCAD 1.1.3. FreeCAD commits the open
# transaction when its document closes and cancels some changes made after that itself, so the bridge doesn't try to
# roll anything back: it must not add undo or redo entries anywhere, must not say anything was undone, and must name
# every document that still holds a change.
CLOSE_THEN_FAIL = r"""
import importlib.util, json, os
import FreeCAD as App

spec = importlib.util.spec_from_file_location("live_bridge", os.environ["CADMATE_BRIDGE"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
bridge = mod.Bridge(App, "unix")
App.addDocumentObserver(bridge.observer)
G = "App.getDocument"
CASES = {
    "before": f"{G}('o1').addObject('Part::Box', 'K'); App.closeDocument(doc.Name); raise ValueError('boom')",
    "after": f"App.closeDocument(doc.Name); {G}('o1').addObject('Part::Box', 'K'); raise ValueError('boom')",
    "mixed": f"{G}('o1').addObject('Part::Box', 'K'); App.closeDocument(doc.Name); {G}('o2').addObject('Part::Box', 'K'); "
             f"{G}('o1').addObject('Part::Box', 'K2'); raise ValueError('boom')",
}
out = {}
for case, code in CASES.items():
    for n in list(App.listDocuments()):
        App.closeDocument(n)
    for n in ("user", "o1", "o2", "t"):
        App.newDocument(n).UndoMode = 1
    App.getDocument("o1").openTransaction("user box")  # the user's own history, which must stay as it was
    App.getDocument("o1").addObject("Part::Box", "U")
    App.getDocument("o1").commitTransaction()
    App.setActiveDocument("user")
    r = bridge._handle({"op": "run", "label": case, "doc": "t", "code": code})
    docs = App.listDocuments()
    out[case] = {"reply": {k: r.get(k) for k in ("ok", "error", "closed", "note", "undo_step_left", "check")},
                 "active": App.ActiveDocument.Name,
                 "undo": {n: list(d.UndoNames) for n, d in docs.items()},
                 "redo": {n: list(d.RedoNames) for n, d in docs.items()},
                 "objects": {n: [o.Name for o in d.Objects] for n, d in docs.items()}}
App.removeDocumentObserver(bridge.observer)
json.dump(out, open(os.environ["CADMATE_OUT"], "w"))
"""


@pytest.fixture(scope="module")
def close_then_fail(tmp_path_factory):
    script = tmp_path_factory.mktemp("bridge") / "close_then_fail.py"
    script.write_text(CLOSE_THEN_FAIL)
    return apps.run_freecad_script(script, env={"CADMATE_BRIDGE": str(BRIDGE)})


@pytest.mark.skipif(not HAS_FREECAD, reason="needs FreeCAD")
@pytest.mark.parametrize("case", ["before", "after", "mixed"])
def test_code_that_closes_its_target_then_fails_is_reported_not_rolled_back(close_then_fail, case):
    run = close_then_fail[case]
    r = run["reply"]
    assert r["ok"] is False and "boom" in r["error"] and r["closed"] == "t", r
    assert "can't be undone" in r["note"] and "was undone" not in r["note"], r
    assert run["active"] == "user", run
    assert all(redo == [] for redo in run["redo"].values()), run  # nothing for the user to redo by mistake
    named = set(r["undo_step_left"] or []) | set(r["check"] or [])
    for n in ("o1", "o2"):
        left = [o for o in run["objects"][n] if o != "U"]
        if left:  # every document still holding a change is named
            assert n in named, (n, run)
        undo = run["undo"][n]
        if n == "o1":
            assert undo[-1] == "user box", run  # the user's own history is still there
        if n in (r["undo_step_left"] or []):  # and an undo step the reply points to is really there, on top
            assert undo[0] == f"cadmate: {case}" and left, run

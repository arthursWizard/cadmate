import json
from datetime import datetime

import pytest

from cadmate import pcb_flow, project

T0 = datetime(2026, 10, 31, 12, 13)
FP_TABLE = '(fp_lib_table\n\t(version 7)\n\t(lib (name "kb")(type "KiCad")(uri "${KIPRJMOD}/../lib/kb.pretty")(options "")(descr ""))\n)\n'


def make(tmp_path, boards=("core",)):
    root = tmp_path / "magpi"
    project.init(root, boards=list(boards))
    lib = root / "kicad/lib/kb.pretty"
    lib.mkdir(parents=True)
    (lib / "Pogo.kicad_mod").write_text("(footprint \"Pogo\")\n")
    (root / "kicad/lib/kb.3dshapes").mkdir()
    (root / "kicad/lib/kb.3dshapes/Pogo.step").write_text("ISO-10303-21;\n")
    for b in boards:
        d = root / "kicad" / b
        (d / f"{b}.kicad_pro").write_text("{}")
        (d / f"{b}.kicad_pcb").write_text('(kicad_pcb (footprint "kb:Pogo" (model "${KIPRJMOD}/../lib/kb.3dshapes/Pogo.step")))\n')
        (d / f"{b}.kicad_sch").write_text(f'(kicad_sch (symbol (instances (project "{b}" (path "/x" (reference "R1"))))))\n')
        (d / "fp-lib-table").write_text(FP_TABLE)
    return root


def test_init_makes_the_layout_and_is_safe_to_rerun(tmp_path):
    root = tmp_path / "kb"
    out = project.init(root, boards=["core", "left"])
    assert {"cadmate-project.json", "README.md", "CLAUDE.md", "kicad/core", "kicad/left", "history/HISTORY.md", ".cadmate/work"} <= set(out["made"])
    (root / "README.md").write_text("mine")
    assert project.init(root)["made"] == [] and (root / "README.md").read_text() == "mine"
    assert project.find_root(root / "kicad/core") == root.resolve()
    assert project.find_root(tmp_path) is None
    with pytest.raises(project.ProjectError, match="lowercase"):
        project.init(root, boards=["Core V2"])


def test_snapshot_names_sort_by_time_and_carry_the_libraries(tmp_path):
    root = make(tmp_path)
    out = project.snapshot(root / "kicad/core", "before-mid-joint", ["kicad/core"], note="user approved the flat case", now=T0)
    snap = root / "history" / "20261031-1213_before-mid-joint"
    assert out["name"] == snap.name and (snap / "kicad/core/core.kicad_pcb").exists()
    # ${KIPRJMOD}/../lib resolves inside the snapshot too: the library and 3D model came along
    assert (snap / "kicad/lib/kb.pretty/Pogo.kicad_mod").exists() and (snap / "kicad/lib/kb.3dshapes/Pogo.step").exists()
    assert set(out["paths"]) == {"kicad/core", "kicad/lib/kb.pretty", "kicad/lib/kb.3dshapes"}
    assert json.loads((snap / "SNAPSHOT.json").read_text())["note"] == "user approved the flat case"
    index = (root / "history/HISTORY.md").read_text()
    assert "20261031-1213_before-mid-joint" in index and "user approved the flat case" in index
    # same minute: seconds keep it unique and in order
    again = project.snapshot(root, "before-mid-joint", ["kicad/core"], now=T0.replace(second=30))
    assert again["name"] == "20261031-121330_before-mid-joint"
    assert [s["name"] for s in project.history(root)["snapshots"]] == sorted([out["name"], again["name"]])


def test_whole_project_snapshot_skips_history_and_scratch(tmp_path):
    root = make(tmp_path)
    (root / ".cadmate/work/core-routed.kicad_pcb").write_text("x")
    project.snapshot(root, "first", now=T0)
    out = project.snapshot(root, "second", now=T0.replace(minute=14))
    snap = root / "history" / out["name"]
    assert (snap / "kicad/core/core.kicad_pcb").exists() and not (snap / "history").exists() and not (snap / ".cadmate").exists()


def test_snapshot_rejects_bad_labels_and_paths_outside(tmp_path):
    root = make(tmp_path)
    for bad in ("Before Mid", "v2_final", "", "x" * 41):
        with pytest.raises(project.ProjectError):
            project.snapshot(root, bad)
    with pytest.raises(project.ProjectError, match="outside"):
        project.snapshot(root, "x", [str(tmp_path)])
    with pytest.raises(project.ProjectError, match="history"):
        project.snapshot(root, "x", ["history"])
    with pytest.raises(project.ProjectError, match="isn't in a cadmate project"):
        project.snapshot(tmp_path, "x")


def test_restore_brings_files_back_and_keeps_the_current_state(tmp_path):
    root = make(tmp_path)
    pcb = root / "kicad/core/core.kicad_pcb"
    good = pcb.read_text()
    snap = project.snapshot(root, "approved", ["kicad/core"], now=T0)["name"]
    pcb.write_text("(kicad_pcb broken)")
    (root / "kicad/core/stray.txt").write_text("made after")
    out = project.restore(root, snap, now=T0.replace(minute=20))
    assert pcb.read_text() == good and not (root / "kicad/core/stray.txt").exists()
    before = root / "history" / out["previous_state"]
    assert out["previous_state"] == "20261031-1220_before-restore-approved"
    assert (before / "kicad/core/core.kicad_pcb").read_text() == "(kicad_pcb broken)"
    with pytest.raises(project.ProjectError, match="no snapshot"):
        project.restore(root, "nope")


def test_check_passes_a_clean_project(tmp_path):
    root = make(tmp_path, ("core", "left"))
    out = project.check(root)
    assert out["ok"], out["findings"]


def test_check_finds_the_sprawl_from_the_keyboard_run(tmp_path):
    root = make(tmp_path, ("core", "left"))
    core = root / "kicad/core"
    (core / "core-hand3.kicad_pcb").write_text("x")
    (core / "core.kicad_pcb.bak").write_text("x")
    (root / "kicad/left-v2").mkdir()
    (root / "kicad/left-v2/left-v2.kicad_pro").write_text("{}")
    (root / "case/variant-flat").mkdir(parents=True)
    (root / "case/variant-flat/half.step").write_text("x")
    (root / "case/a/b/c").mkdir(parents=True)
    (root / "case/a/b/c/deep.step").write_text("x")
    (root / "semi-split-piantor").mkdir()
    (root / "history/mid joint try").mkdir()
    out = project.check(root)
    by_path = {f["path"]: f for f in out["findings"]}
    assert not out["ok"]
    assert "a second .kicad_pcb" in by_path["kicad/core/core-hand3.kicad_pcb"]["problem"]
    assert "backup" in by_path["kicad/core/core.kicad_pcb.bak"]["problem"]
    assert "version" in by_path["kicad/left-v2"]["problem"]
    assert "version" in by_path["case/variant-flat"]["problem"]
    assert "deeper" in by_path["case/a/b/c/deep.step"]["problem"]
    assert "top level" in by_path["semi-split-piantor"]["problem"]
    assert "named" in by_path["history/mid joint try"]["problem"]
    # library part names with dashes and numbers aren't versions
    (root / "kicad/lib/kb.pretty/SOIC-8.kicad_mod").write_text("x")
    assert not any("SOIC" in f["path"] for f in project.check(root)["findings"])


def test_check_catches_what_breaks_kicad(tmp_path):
    root = make(tmp_path, ("core", "left"))
    left = root / "kicad/left"
    (left / "fp-lib-table").write_text(FP_TABLE.replace("${KIPRJMOD}/../lib/kb.pretty", "/Users/me/old/kb.pretty"))
    (left / "left.kicad_sch").write_text('(kicad_sch (symbol (instances (project "left-v2" (path "/x" (reference "R1"))))))\n')
    (left / "left.kicad_pcb").write_text('(kicad_pcb (model "${KIPRJMOD}/../../../elsewhere/x.step"))\n')
    (root / "kicad/core/fp-lib-table").write_text(FP_TABLE.replace("kb.pretty", "gone.pretty"))
    out = project.check(root)
    errors = {(f["path"], f["problem"].split()[0]) for f in out["findings"] if f["severity"] == "error"}
    assert ("kicad/left/fp-lib-table", "absolute") in errors
    assert ("kicad/left/left.kicad_sch", "symbol") in errors
    assert ("kicad/core/fp-lib-table", "${KIPRJMOD}/../lib/gone.pretty") in errors
    assert any(f["path"] == "kicad/left/left.kicad_pcb" and "is outside the project" in f["problem"] for f in out["findings"])


def test_check_flags_stale_and_orphaned_fab_files(tmp_path):
    import os

    root = make(tmp_path, ("core",))
    (root / "fab/core").mkdir()
    (root / "fab/core/core-gerbers.zip").write_text("x")
    os.utime(root / "fab/core/core-gerbers.zip", (1, 1))
    (root / "fab/port-centre-v3").mkdir()
    (root / "fab/port-centre-v3/x.zip").write_text("x")
    probs = {f["path"]: f["problem"] for f in project.check(root)["findings"]}
    assert "older than the board" in probs["fab/core"]
    assert "isn't in kicad" in probs["fab/port-centre-v3"]


def test_routing_target_in_a_project_is_the_board_itself_after_a_snapshot(tmp_path):
    root = make(tmp_path)
    pcb = root / "kicad/core/core.kicad_pcb"
    dst, snap = pcb_flow.target(str(pcb), None, "routed", None)
    assert dst == pcb and snap["name"].endswith("_before-routed-core")
    assert (root / "history" / snap["name"] / "kicad/core/core.kicad_pcb").exists()
    # in_place=False: scratch, never beside the board
    dst, snap = pcb_flow.target(str(pcb), None, "routed", False)
    assert dst == root / ".cadmate/work/core-routed.kicad_pcb" and snap is None
    # an explicit out_path is honoured
    dst, snap = pcb_flow.target(str(pcb), str(tmp_path / "x.kicad_pcb"), "routed", None)
    assert dst == tmp_path / "x.kicad_pcb" and snap is None


def test_routing_target_outside_a_project_keeps_the_old_behaviour(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    assert pcb_flow.target(str(pcb), None, "finished", None) == (tmp_path / "b-finished.kicad_pcb", None)
    with pytest.raises(pcb_flow.FlowError, match="cadmate project"):
        pcb_flow.target(str(pcb), None, "finished", True)
    with pytest.raises(pcb_flow.FlowError, match="in_place"):
        pcb_flow.target(str(pcb), str(pcb), "finished", None)


def test_finish_routing_changes_the_project_board_in_place(tmp_path, monkeypatch):
    from test_pcb_routing import HEAD, ic

    from cadmate import freerouting as fr

    root = make(tmp_path)
    pcb = root / "kicad/core/core.kicad_pcb"
    pcb.write_text(HEAD + ic() + ")\n")
    monkeypatch.setattr(fr, "refill", lambda f: None)
    out = pcb_flow.finish_routing(str(pcb))
    assert out["written"] == str(pcb) and out["snapshot"]["name"].endswith("before-finished-core")
    assert sorted(p.name for p in pcb.parent.iterdir()) == ["core.kicad_pcb", "core.kicad_pro", "core.kicad_sch", "fp-lib-table"]


def test_replaced_library_files_go_to_history_not_bak(tmp_path):
    root = make(tmp_path)
    fp = root / "kicad/lib/kb.pretty/Pogo.kicad_mod"
    kept = project.keep([fp, None, root / "kicad/lib/kb.pretty/missing.kicad_mod"], "footprint")
    assert "history" in kept and not fp.with_suffix(".kicad_mod.bak").exists()
    assert (root / "history" / kept.split("/")[-1] / "kicad/lib/kb.pretty/Pogo.kicad_mod").exists()
    loose = tmp_path / "x.kicad_mod"
    loose.write_text("x")
    assert project.keep([loose], "footprint").endswith("x.kicad_mod.bak") and (tmp_path / "x.kicad_mod.bak").exists()
    assert project.keep([tmp_path / "none"], "footprint") is None


def test_brief_open_items_show_in_the_check(tmp_path):
    root = make(tmp_path)
    assert project.check(root)["brief"] == {"open": [], "met": [], "say": project.check(root)["brief"]["say"]}
    (root / "docs/brief.md").write_text("# b\n\n- [ ] Centre case: upside-down pentagon (user, 29 Oct)\n- [x] Choc V1 hotswap\n")
    b = project.check(root)["brief"]
    assert b["open"] == ["Centre case: upside-down pentagon (user, 29 Oct)"] and b["met"] == ["Choc V1 hotswap"]
    (root / "docs/brief.md").unlink()
    assert any(f["path"] == "docs/brief.md" for f in project.check(root)["findings"])

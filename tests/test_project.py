import json
from datetime import datetime

import pytest

from cadmate import pcb_flow, project

T0 = datetime(2026, 10, 31, 12, 13)
FP_TABLE = '(fp_lib_table\n\t(version 7)\n\t(lib (name "proj")(type "KiCad")(uri "${KIPRJMOD}/../lib/proj.pretty")(options "")(descr ""))\n)\n'


def make(tmp_path, boards=("main",)):
    root = tmp_path / "demo"
    project.init(root, boards=list(boards))
    lib = root / "kicad/lib/proj.pretty"
    lib.mkdir(parents=True)
    (lib / "Conn.kicad_mod").write_text("(footprint \"Conn\")\n")
    (root / "kicad/lib/proj.3dshapes").mkdir()
    (root / "kicad/lib/proj.3dshapes/Conn.step").write_text("ISO-10303-21;\n")
    for b in boards:
        d = root / "kicad" / b
        (d / f"{b}.kicad_pro").write_text("{}")
        (d / f"{b}.kicad_pcb").write_text('(kicad_pcb (footprint "proj:Conn" (model "${KIPRJMOD}/../lib/proj.3dshapes/Conn.step")))\n')
        (d / f"{b}.kicad_sch").write_text(f'(kicad_sch (symbol (instances (project "{b}" (path "/x" (reference "R1"))))))\n')
        (d / "fp-lib-table").write_text(FP_TABLE)
    return root


def test_init_makes_the_layout_and_is_safe_to_rerun(tmp_path):
    root = tmp_path / "proj"
    out = project.init(root, boards=["main", "sensor"])
    assert {"cadmate-project.json", "README.md", "CLAUDE.md", "kicad/main", "kicad/sensor", "history/HISTORY.md", ".cadmate/work"} <= set(out["made"])
    (root / "README.md").write_text("mine")
    assert project.init(root)["made"] == [] and (root / "README.md").read_text() == "mine"
    assert project.find_root(root / "kicad/main") == root.resolve()
    assert project.find_root(tmp_path) is None
    with pytest.raises(project.ProjectError, match="lowercase"):
        project.init(root, boards=["Core V2"])


def test_snapshot_names_sort_by_time_and_carry_the_libraries(tmp_path):
    root = make(tmp_path)
    out = project.snapshot(root / "kicad/main", "before-rework", ["kicad/main"], note="user approved the placement", now=T0)
    snap = root / "history" / "20261031-1213_before-rework"
    assert out["name"] == snap.name and (snap / "kicad/main/main.kicad_pcb").exists()
    # ${KIPRJMOD}/../lib resolves inside the snapshot too: the library and 3D model came along
    assert (snap / "kicad/lib/proj.pretty/Conn.kicad_mod").exists() and (snap / "kicad/lib/proj.3dshapes/Conn.step").exists()
    assert set(out["paths"]) == {"kicad/main", "kicad/lib/proj.pretty", "kicad/lib/proj.3dshapes"}
    assert json.loads((snap / "SNAPSHOT.json").read_text())["note"] == "user approved the placement"
    index = (root / "history/HISTORY.md").read_text()
    assert "20261031-1213_before-rework" in index and "user approved the placement" in index
    # same minute: seconds keep it unique and in order
    again = project.snapshot(root, "before-rework", ["kicad/main"], now=T0.replace(second=30))
    assert again["name"] == "20261031-121330_before-rework"
    assert [s["name"] for s in project.history(root)["snapshots"]] == sorted([out["name"], again["name"]])


def test_whole_project_snapshot_skips_history_and_scratch(tmp_path):
    root = make(tmp_path)
    (root / ".cadmate/work/main-routed.kicad_pcb").write_text("x")
    project.snapshot(root, "first", now=T0)
    out = project.snapshot(root, "second", now=T0.replace(minute=14))
    snap = root / "history" / out["name"]
    assert (snap / "kicad/main/main.kicad_pcb").exists() and not (snap / "history").exists() and not (snap / ".cadmate").exists()


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
    pcb = root / "kicad/main/main.kicad_pcb"
    good = pcb.read_text()
    snap = project.snapshot(root, "approved", ["kicad/main"], now=T0)["name"]
    pcb.write_text("(kicad_pcb broken)")
    (root / "kicad/main/stray.txt").write_text("made after")
    out = project.restore(root, snap, now=T0.replace(minute=20))
    assert pcb.read_text() == good and not (root / "kicad/main/stray.txt").exists()
    before = root / "history" / out["previous_state"]
    assert out["previous_state"] == "20261031-1220_before-restore-approved"
    assert (before / "kicad/main/main.kicad_pcb").read_text() == "(kicad_pcb broken)"
    with pytest.raises(project.ProjectError, match="no snapshot"):
        project.restore(root, "nope")


def test_check_passes_a_clean_project(tmp_path):
    root = make(tmp_path, ("main", "sensor"))
    out = project.check(root)
    assert out["ok"], out["findings"]


def test_check_finds_side_by_side_versions_and_backups(tmp_path):
    root = make(tmp_path, ("main", "sensor"))
    main = root / "kicad/main"
    (main / "main-hand3.kicad_pcb").write_text("x")
    (main / "main.kicad_pcb.bak").write_text("x")
    (root / "kicad/sensor-v2").mkdir()
    (root / "kicad/sensor-v2/sensor-v2.kicad_pro").write_text("{}")
    (root / "case/variant-flat").mkdir(parents=True)
    (root / "case/variant-flat/half.step").write_text("x")
    (root / "case/a/b/c").mkdir(parents=True)
    (root / "case/a/b/c/deep.step").write_text("x")
    (root / "semi-split-piantor").mkdir()
    (root / "history/mid joint try").mkdir()
    out = project.check(root)
    by_path = {f["path"]: f for f in out["findings"]}
    assert not out["ok"]
    assert "a second .kicad_pcb" in by_path["kicad/main/main-hand3.kicad_pcb"]["problem"]
    assert "backup" in by_path["kicad/main/main.kicad_pcb.bak"]["problem"]
    assert "version" in by_path["kicad/sensor-v2"]["problem"]
    assert "version" in by_path["case/variant-flat"]["problem"]
    assert "deeper" in by_path["case/a/b/c/deep.step"]["problem"]
    assert "top level" in by_path["semi-split-piantor"]["problem"]
    assert "named" in by_path["history/mid joint try"]["problem"]
    # library part names with dashes and numbers aren't versions
    (root / "kicad/lib/proj.pretty/SOIC-8.kicad_mod").write_text("x")
    assert not any("SOIC" in f["path"] for f in project.check(root)["findings"])


def test_check_catches_what_breaks_kicad(tmp_path):
    root = make(tmp_path, ("main", "sensor"))
    sensor = root / "kicad/sensor"
    (sensor / "fp-lib-table").write_text(FP_TABLE.replace("${KIPRJMOD}/../lib/proj.pretty", "/Users/me/old/proj.pretty"))
    (sensor / "sensor.kicad_sch").write_text('(kicad_sch (symbol (instances (project "sensor-v2" (path "/x" (reference "R1"))))))\n')
    (sensor / "sensor.kicad_pcb").write_text('(kicad_pcb (model "${KIPRJMOD}/../../../elsewhere/x.step"))\n')
    (root / "kicad/main/fp-lib-table").write_text(FP_TABLE.replace("proj.pretty", "gone.pretty"))
    out = project.check(root)
    errors = {(f["path"], f["problem"].split()[0]) for f in out["findings"] if f["severity"] == "error"}
    assert ("kicad/sensor/fp-lib-table", "absolute") in errors
    assert ("kicad/sensor/sensor.kicad_sch", "symbol") in errors
    assert ("kicad/main/fp-lib-table", "${KIPRJMOD}/../lib/gone.pretty") in errors
    assert any(f["path"] == "kicad/sensor/sensor.kicad_pcb" and "is outside the project" in f["problem"] for f in out["findings"])


def test_check_flags_stale_and_orphaned_fab_files(tmp_path):
    import os

    root = make(tmp_path, ("main",))
    (root / "fab/main").mkdir()
    (root / "fab/main/main-gerbers.zip").write_text("x")
    os.utime(root / "fab/main/main-gerbers.zip", (1, 1))
    (root / "fab/port-centre-v3").mkdir()
    (root / "fab/port-centre-v3/x.zip").write_text("x")
    probs = {f["path"]: f["problem"] for f in project.check(root)["findings"]}
    assert "older than the board" in probs["fab/main"]
    assert "isn't in kicad" in probs["fab/port-centre-v3"]


def test_routing_target_in_a_project_is_the_board_itself_after_a_snapshot(tmp_path):
    root = make(tmp_path)
    pcb = root / "kicad/main/main.kicad_pcb"
    dst, snap = pcb_flow.target(str(pcb), None, "routed", None)
    assert dst == pcb and snap["name"].endswith("_before-routed-main")
    assert (root / "history" / snap["name"] / "kicad/main/main.kicad_pcb").exists()
    # in_place=False: scratch, never beside the board
    dst, snap = pcb_flow.target(str(pcb), None, "routed", False)
    assert dst == root / ".cadmate/work/main-routed.kicad_pcb" and snap is None
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
    pcb = root / "kicad/main/main.kicad_pcb"
    pcb.write_text(HEAD + ic() + ")\n")
    monkeypatch.setattr(fr, "refill", lambda f: None)
    out = pcb_flow.finish_routing(str(pcb))
    assert out["written"] == str(pcb) and out["snapshot"]["name"].endswith("before-finished-main")
    assert sorted(p.name for p in pcb.parent.iterdir()) == ["fp-lib-table", "main.kicad_pcb", "main.kicad_pro", "main.kicad_sch"]


def test_replaced_library_files_go_to_history_not_bak(tmp_path):
    root = make(tmp_path)
    fp = root / "kicad/lib/proj.pretty/Conn.kicad_mod"
    kept = project.keep([fp, None, root / "kicad/lib/proj.pretty/missing.kicad_mod"], "footprint")
    assert "history" in kept and not fp.with_suffix(".kicad_mod.bak").exists()
    assert (root / "history" / kept.split("/")[-1] / "kicad/lib/proj.pretty/Conn.kicad_mod").exists()
    loose = tmp_path / "x.kicad_mod"
    loose.write_text("x")
    assert project.keep([loose], "footprint").endswith("x.kicad_mod.bak") and (tmp_path / "x.kicad_mod.bak").exists()
    assert project.keep([tmp_path / "none"], "footprint") is None


def test_brief_open_items_show_in_the_check(tmp_path):
    root = make(tmp_path)
    assert project.check(root)["brief"] == {"open": [], "met": [], "say": project.check(root)["brief"]["say"]}
    (root / "docs/brief.md").write_text("# b\n\n- [ ] Case: 40 x 60 mm, rounded corners (user, 29 Oct)\n- [x] USB-C for power\n")
    b = project.check(root)["brief"]
    assert b["open"] == ["Case: 40 x 60 mm, rounded corners (user, 29 Oct)"] and b["met"] == ["USB-C for power"]
    (root / "docs/brief.md").unlink()
    assert any(f["path"] == "docs/brief.md" for f in project.check(root)["findings"])


def make_flat(tmp_path, boards=("main",)):
    """A KiCad-only project: board folders and lib/ at the root."""
    root = tmp_path / "flat"
    out = project.init(root, boards=list(boards), tools=["kicad"])
    assert out["flat"] and out["tools"] == ["kicad"]
    (root / "lib/proj.pretty").mkdir(parents=True)
    (root / "lib/proj.pretty/Conn.kicad_mod").write_text("(footprint \"Conn\")\n")
    for b in boards:
        d = root / b
        (d / f"{b}.kicad_pro").write_text("{}")
        (d / f"{b}.kicad_pcb").write_text('(kicad_pcb (footprint "proj:Conn"))\n')
        (d / f"{b}.kicad_sch").write_text(f'(kicad_sch (symbol (instances (project "{b}" (path "/x" (reference "R1"))))))\n')
        (d / "fp-lib-table").write_text(FP_TABLE)
    return root


def test_one_tool_projects_are_flat(tmp_path):
    root = make_flat(tmp_path, ("main", "sensor"))
    assert not (root / "kicad").exists() and not (root / "case").exists() and (root / "fab").is_dir()
    assert json.loads((root / "cadmate-project.json").read_text())["tools"] == ["kicad"]
    assert "main/<board>" not in (root / "CLAUDE.md").read_text() and "<board>/<board>.*" in (root / "CLAUDE.md").read_text()
    out = project.check(root)
    assert out["ok"] and out["flat"], out["findings"]
    # the same rules apply one level up: second boards, version names, libraries outside lib/, orphaned fab files
    (root / "main/main-routed.kicad_pcb").write_text("x")
    (root / "sensor-v2").mkdir()
    (root / "sensor-v2/sensor-v2.kicad_pro").write_text("{}")
    (root / "other.pretty").mkdir()
    (root / "fab/gone").mkdir()
    (root / "fab/gone/x.zip").write_text("x")
    (root / "notes").mkdir()
    (root / "notes/x.txt").write_text("x")
    probs = {f["path"]: f["problem"] for f in project.check(root)["findings"]}
    assert "a second .kicad_pcb" in probs["main/main-routed.kicad_pcb"]
    assert "version" in probs["sensor-v2"]
    assert "outside lib/" in probs["other.pretty"]
    assert "isn't in gone/" in probs["fab/gone"]
    assert "isn't a board" in probs["notes"]


def test_a_freecad_only_project_keeps_the_case_at_the_root(tmp_path):
    root = tmp_path / "box"
    out = project.init(root, tools="freecad")
    assert out["flat"] and not (root / "case").exists() and not (root / "fab").exists() and not (root / "lib").exists()
    (root / "top.step").write_text("x")
    (root / "box.FCStd").write_text("x")
    (root / "renders").mkdir()
    (root / "renders/top.png").write_text("x")
    probs = {f["path"]: f for f in project.check(root)["findings"]}
    assert probs == {"enclosure-check.json": probs["enclosure-check.json"]}  # only: the case hasn't been checked
    (root / "renders/a/b").mkdir(parents=True)
    (root / "renders/a/b/deep.png").write_text("x")
    (root / "top-v2.step").write_text("x")
    (root / "case").mkdir()
    probs = {f["path"]: f["problem"] for f in project.check(root)["findings"]}
    assert "deeper" in probs["renders/a/b/deep.png"] and "version" in probs["top-v2.step"]
    assert "flat" in probs["case"]
    with pytest.raises(project.ProjectError, match="boards need KiCad"):
        project.init(tmp_path / "x", boards=["main"], tools="freecad")


def test_init_infers_the_tools_and_keeps_them(tmp_path):
    root = tmp_path / "existing"
    (root / "main").mkdir(parents=True)
    (root / "main/main.kicad_pro").write_text("{}")
    (root / "lib/x.3dshapes").mkdir(parents=True)
    (root / "lib/x.3dshapes/a.step").write_text("x")  # a library model isn't a case
    assert project.init(root)["tools"] == ["kicad"]
    assert project.init(root)["made"] == []
    with pytest.raises(project.ProjectError, match="project_add_tool freecad"):
        project.init(root, tools=["kicad", "freecad"])
    assert project.init(tmp_path / "empty")["tools"] == ["kicad", "freecad"]  # nothing to go by: both, nested


def test_check_flags_the_other_tool_at_the_root(tmp_path):
    root = make_flat(tmp_path)
    (root / "case.FCStd").write_text("x")
    f = next(f for f in project.check(root)["findings"] if f["path"] == "case.FCStd")
    assert "freecad files in a kicad-only project" in f["problem"] and "project_add_tool freecad" in f["fix"]


def test_add_tool_moves_a_flat_project_into_subfolders_after_a_snapshot(tmp_path):
    root = make_flat(tmp_path, ("main", "sensor"))
    (root / "fab/main").mkdir()
    (root / "fab/main/main.zip").write_text("x")
    (root / "tools/gen.py").write_text("x")
    out = project.add_tool(root, "freecad", now=T0)
    assert out["tools"] == ["kicad", "freecad"] and out["snapshot"] == "20261031-1213_before-add-freecad"
    snap = root / "history" / out["snapshot"]
    assert (snap / "main/main.kicad_pcb").exists() and (snap / "lib/proj.pretty/Conn.kicad_mod").exists()
    assert sorted(p.name for p in (root / "kicad").iterdir()) == ["lib", "main", "sensor"]
    assert (root / "case").is_dir() and (root / "fab/main/main.zip").exists() and (root / "tools/gen.py").exists()
    assert not (root / "main").exists() and project.shape(root).nested
    # ${KIPRJMOD}/../lib still resolves, so the moved project is clean
    assert project.check(root)["ok"], project.check(root)["findings"]
    assert project.add_tool(root, "kicad")["moved"] == []


def test_add_tool_to_a_freecad_project_moves_the_case(tmp_path):
    root = tmp_path / "box"
    project.init(root, tools=["freecad"])
    (root / "top.step").write_text("x")
    (root / "renders").mkdir()
    (root / "renders/a.png").write_text("x")
    out = project.add_tool(root, "kicad", now=T0)
    assert sorted(out["moved"]) == ["renders -> case/renders", "top.step -> case/top.step"]
    assert (root / "case/top.step").exists() and (root / "kicad/lib").is_dir() and (root / "fab").is_dir()
    assert (root / "README.md").exists() and (root / "docs/brief.md").exists()

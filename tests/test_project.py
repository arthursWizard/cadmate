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
        d.mkdir(exist_ok=True)
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
    (root / "freecad/variant-flat").mkdir(parents=True)
    (root / "freecad/variant-flat/half.step").write_text("x")
    (root / "freecad/a/b/c").mkdir(parents=True)
    (root / "freecad/a/b/c/deep.step").write_text("x")
    (root / "old-case-try").mkdir()
    (root / "history/mid joint try").mkdir()
    out = project.check(root)
    by_path = {f["path"]: f for f in out["findings"]}
    assert not out["ok"]
    assert "a second .kicad_pcb" in by_path["kicad/main/main-hand3.kicad_pcb"]["problem"]
    assert "backup" in by_path["kicad/main/main.kicad_pcb.bak"]["problem"]
    assert "version" in by_path["kicad/sensor-v2"]["problem"]
    assert "version" in by_path["freecad/variant-flat"]["problem"]
    assert "deeper" in by_path["freecad/a/b/c/deep.step"]["problem"]
    assert "top level" in by_path["old-case-try"]["problem"]
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
    (root / "fab/old-v3").mkdir()
    (root / "fab/old-v3/x.zip").write_text("x")
    probs = {f["path"]: f["problem"] for f in project.check(root)["findings"]}
    assert "older than the board" in probs["fab/main"]
    assert "isn't in the project" in probs["fab/old-v3"]


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


ROOT_TABLE = FP_TABLE.replace("${KIPRJMOD}/../lib/", "${KIPRJMOD}/lib/")


def board_files(d, b, lib="${KIPRJMOD}/lib"):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{b}.kicad_pro").write_text("{}")
    (d / f"{b}.kicad_pcb").write_text(f'(kicad_pcb (footprint "proj:Conn" (model "{lib}/proj.3dshapes/Conn.step")))\n')
    (d / f"{b}.kicad_sch").write_text(f'(kicad_sch (symbol (instances (project "{b}" (path "/x" (reference "R1"))))))\n')
    (d / "fp-lib-table").write_text(FP_TABLE.replace("${KIPRJMOD}/../lib", lib))


def make_flat(tmp_path, boards=("main",)):
    """A KiCad-only project. One board: its files at the root, beside lib/. Two or more: a folder each."""
    root = tmp_path / "flat"
    out = project.init(root, boards=list(boards), tools=["kicad"])
    assert out["flat"] and out["tools"] == ["kicad"]
    (root / "lib/proj.pretty").mkdir(parents=True)
    (root / "lib/proj.pretty/Conn.kicad_mod").write_text("(footprint \"Conn\")\n")
    (root / "lib/proj.3dshapes").mkdir()
    (root / "lib/proj.3dshapes/Conn.step").write_text("ISO-10303-21;\n")
    for b in boards:
        if len(boards) == 1:
            board_files(root, b)
        else:
            board_files(root / b, b, "${KIPRJMOD}/../lib")
    return root


def test_one_board_sits_at_the_root(tmp_path):
    root = make_flat(tmp_path)
    assert not (root / "main").exists() and not (root / "kicad").exists() and not (root / "freecad").exists()
    assert json.loads((root / "cadmate-project.json").read_text())["tools"] == ["kicad"]
    out = project.check(root)
    assert out["ok"] and out["flat"], out["findings"]
    snap = project.snapshot(root, "approved", [str(f) for f in project.board_files(root / "main.kicad_pcb")], now=T0)
    assert {"main.kicad_pcb", "lib/proj.3dshapes"} <= set(snap["paths"])  # ${KIPRJMOD}/lib came along
    dst, s = pcb_flow.target(str(root / "main.kicad_pcb"), None, "routed", None)
    assert dst == root / "main.kicad_pcb" and s is not None
    (root / "main-routed.kicad_pcb").write_text("x")
    (root / "stray.kicad_pro").write_text("{}")
    probs = {f["path"]: f["problem"] for f in project.check(root)["findings"]}
    assert "a second .kicad_pcb beside the main board" in probs["main-routed.kicad_pcb"]
    assert "a second board directly in the project root" in probs["stray.kicad_pro"]


def test_two_boards_get_a_folder_each_with_the_same_rules(tmp_path):
    root = make_flat(tmp_path, ("main", "sensor"))
    assert not (root / "kicad").exists() and (root / "fab").is_dir()
    out = project.check(root)
    assert out["ok"], out["findings"]
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
    assert "isn't in the project (gone)" in probs["fab/gone"]
    assert "isn't a board" in probs["notes"]


def test_add_board_moves_the_first_board_into_its_folder_and_fixes_its_paths(tmp_path):
    root = make_flat(tmp_path)
    (root / "fab/main").mkdir()
    (root / "fab/main/main.zip").write_text("x")
    board_files(root / "sensor", "sensor", "${KIPRJMOD}/../lib")  # a second board, added by hand
    f = next(f for f in project.check(root)["findings"] if f["path"] == "main.kicad_pro")
    assert "beside board folders (sensor)" in f["problem"] and "project_add_board" in f["fix"]
    out = project.add_board(root, now=T0)
    assert out["board"] == "main" and out["snapshot"] == "20261031-1213_before-board-folders-main"
    assert (root / "history" / out["snapshot"] / "main.kicad_pcb").exists()
    assert sorted(p.name for p in (root / "main").iterdir()) == ["fp-lib-table", "main.kicad_pcb", "main.kicad_pro", "main.kicad_sch"]
    assert "${KIPRJMOD}/../lib/proj.pretty" in (root / "main/fp-lib-table").read_text()
    assert "${KIPRJMOD}/../lib/proj.3dshapes/Conn.step" in (root / "main/main.kicad_pcb").read_text()
    assert set(out["rewritten"]) == {"main/fp-lib-table", "main/main.kicad_pcb"}
    assert project.check(root)["ok"], project.check(root)["findings"]
    # once boards have folders, a new one just gets its folder
    assert project.add_board(root, "power")["made"] == ["power"] and (root / "power").is_dir()


def test_add_board_with_one_board_says_where_the_new_one_goes(tmp_path):
    root = tmp_path / "p"
    project.init(root, tools="kicad")
    assert "no folder" in project.add_board(root, "main")["next"] and not (root / "main").exists()
    with pytest.raises(project.ProjectError, match="layout folder"):
        project.add_board(root, "docs")


def test_a_freecad_only_project_keeps_its_part_at_the_root(tmp_path):
    root = tmp_path / "bracket"
    out = project.init(root, tools="freecad")
    assert out["flat"] and not any((root / d).exists() for d in ("case", "freecad", "fab", "lib"))
    assert "## Parts" in (root / "README.md").read_text() and "case" not in (root / "README.md").read_text().lower()
    (root / "bracket.step").write_text("x")
    (root / "bracket.FCStd").write_text("x")
    (root / "renders").mkdir()
    (root / "renders/top.png").write_text("x")
    assert project.check(root)["ok"], project.check(root)["findings"]  # no assembly: nothing for check_enclosure
    (root / "renders/a/b").mkdir(parents=True)
    (root / "renders/a/b/deep.png").write_text("x")
    (root / "bracket-v2.step").write_text("x")
    (root / "case").mkdir()
    (root / "junk").mkdir()
    (root / "junk/notes.txt").write_text("x")
    probs = {f["path"]: f["problem"] for f in project.check(root)["findings"]}
    assert "deeper" in probs["renders/a/b/deep.png"] and "version" in probs["bracket-v2.step"]
    assert "whose files sit at the root" in probs["case"] and "isn't a part" in probs["junk"]
    with pytest.raises(project.ProjectError, match="boards need kicad"):
        project.init(tmp_path / "x", boards=["main"], tools="freecad")


def test_add_part_moves_the_first_part_into_a_folder_named_for_it(tmp_path):
    root = tmp_path / "desk"
    project.init(root, tools="freecad")
    (root / "Bracket.FCStd").write_text("x")
    (root / "bracket.step").write_text("x")
    (root / "renders").mkdir()
    (root / "renders/a.png").write_text("x")
    (root / ".gitignore").write_text("x")
    (root / "knob").mkdir()
    (root / "knob/knob.FCStd").write_text("x")
    f = next(f for f in project.check(root)["findings"] if "beside part folders" in f["problem"])
    assert "project_add_part" in f["fix"]
    out = project.add_part(root, now=T0)  # the part's name comes from its only .FCStd
    assert out["part"] == "bracket" and sorted(out["moved"]) == [
        "Bracket.FCStd -> bracket/Bracket.FCStd", "bracket.step -> bracket/bracket.step", "renders -> bracket/renders"]
    assert (root / ".gitignore").exists() and (root / "README.md").exists() and (root / "docs/brief.md").exists()
    assert project.check(root)["ok"], project.check(root)["findings"]
    # with no .FCStd to go by, the caller names it
    other = tmp_path / "other"
    project.init(other, tools="freecad")
    (other / "a.step").write_text("x")
    (other / "b.step").write_text("x")
    with pytest.raises(project.ProjectError, match="pass current"):
        project.add_part(other, "lid")
    assert project.add_part(other, "lid", current="enclosure", now=T0)["part"] == "enclosure"
    assert (other / "enclosure/a.step").exists() and (other / "lid").is_dir()


def test_init_infers_the_tools_and_keeps_them(tmp_path):
    root = tmp_path / "existing"
    (root / "main").mkdir(parents=True)
    (root / "main/main.kicad_pro").write_text("{}")
    (root / "lib/x.3dshapes").mkdir(parents=True)
    (root / "lib/x.3dshapes/a.step").write_text("x")  # a library model isn't a FreeCAD part
    assert project.init(root)["tools"] == ["kicad"]
    assert project.init(root)["made"] == []
    with pytest.raises(project.ProjectError, match="project_add_tool freecad"):
        project.init(root, tools=["kicad", "freecad"])
    both = project.init(tmp_path / "empty")  # nothing to go by: both, each in its folder
    assert both["tools"] == ["kicad", "freecad"] and (tmp_path / "empty/freecad").is_dir()
    assert not (tmp_path / "empty/case").exists()


def test_a_layout_1_project_keeps_its_case_folder(tmp_path):
    root = make(tmp_path)
    m = json.loads((root / "cadmate-project.json").read_text())
    (root / "freecad").rename(root / "case")
    (root / "cadmate-project.json").write_text(json.dumps({"name": m["name"], "layout": 1}))
    assert project.shape(root).dir("freecad") == "case"
    assert project.check(root)["ok"], project.check(root)["findings"]


def test_check_flags_the_other_tool_at_the_root(tmp_path):
    root = make_flat(tmp_path)
    (root / "enclosure.FCStd").write_text("x")
    f = next(f for f in project.check(root)["findings"] if f["path"] == "enclosure.FCStd")
    assert "freecad files in a kicad-only project" in f["problem"] and "project_add_tool freecad" in f["fix"]


def test_add_tool_moves_a_one_board_project_into_kicad(tmp_path):
    root = make_flat(tmp_path)
    (root / "fab/main").mkdir()
    (root / "fab/main/main.zip").write_text("x")
    (root / "tools/gen.py").write_text("x")
    out = project.add_tool(root, "freecad", now=T0)
    assert out["tools"] == ["kicad", "freecad"] and out["snapshot"] == "20261031-1213_before-add-freecad"
    assert (root / "history" / out["snapshot"] / "main.kicad_pcb").exists()
    assert sorted(p.name for p in (root / "kicad").iterdir()) == [
        "fp-lib-table", "lib", "main.kicad_pcb", "main.kicad_pro", "main.kicad_sch"]
    assert (root / "freecad").is_dir() and not (root / "case").exists()
    assert (root / "fab/main/main.zip").exists() and (root / "tools/gen.py").exists()
    # ${KIPRJMOD}/lib still resolves: the board and lib/ moved together
    assert project.check(root)["ok"], project.check(root)["findings"]
    assert project.add_tool(root, "kicad")["moved"] == []


def test_add_tool_moves_board_folders_too(tmp_path):
    root = make_flat(tmp_path, ("main", "sensor"))
    project.add_tool(root, "freecad", now=T0)
    assert sorted(p.name for p in (root / "kicad").iterdir()) == ["lib", "main", "sensor"]
    assert project.check(root)["ok"], project.check(root)["findings"]


def test_add_tool_to_a_freecad_project_moves_the_part(tmp_path):
    root = tmp_path / "box"
    project.init(root, tools=["freecad"])
    (root / "enclosure.FCStd").write_text("x")
    (root / "top.step").write_text("x")
    (root / "renders").mkdir()
    (root / "renders/a.png").write_text("x")
    out = project.add_tool(root, "kicad", now=T0)
    assert sorted(out["moved"]) == ["enclosure.FCStd -> freecad/enclosure.FCStd", "renders -> freecad/renders",
                                    "top.step -> freecad/top.step"]
    assert (root / "kicad/lib").is_dir() and (root / "fab").is_dir()
    assert (root / "README.md").exists() and (root / "docs/brief.md").exists()
    assert project.check(root)["ok"], project.check(root)["findings"]


def protoboard(f, cols=24):
    f.write_text(json.dumps({"board": {"type": "perfboard", "cols": cols, "rows": 16}, "parts": [], "nets": {}}))


def test_a_protoboard_project_keeps_its_board_at_the_root(tmp_path):
    root = tmp_path / "blinker"
    root.mkdir()
    protoboard(root / "blinker.json")
    out = project.init(root)
    assert out["tools"] == ["protoboard"] and out["flat"] and not (root / "docs/blinker.json").exists()
    for f in ("blinker.png", "blinker.svg", "blinker-build.md"):
        (root / f).write_text("x")
    assert project.check(root)["ok"], project.check(root)["findings"]
    (root / "timer").mkdir()
    protoboard(root / "timer/timer.json")
    f = next(f for f in project.check(root)["findings"] if "beside protoboard folders" in f["problem"])
    assert "project_add_protoboard" in f["fix"]
    out = project.add_protoboard(root, now=T0)  # its name comes from the only design JSON
    assert out["protoboard"] == "blinker" and (root / "blinker/blinker-build.md").exists()
    assert project.check(root)["ok"], project.check(root)["findings"]


def test_add_tool_brings_a_protoboard_left_at_the_root_into_its_folder(tmp_path):
    root = make_flat(tmp_path)
    protoboard(root / "tester.json")
    (root / "tester.png").write_text("x")
    f = next(f for f in project.check(root)["findings"] if f["path"] == "tester.json")
    assert "protoboard files in a kicad-only project" in f["problem"] and "project_add_tool protoboard" in f["fix"]
    out = project.add_tool(root, "protoboard", now=T0)
    assert out["tools"] == ["kicad", "protoboard"]
    assert (root / "protoboard/tester.json").exists() and (root / "protoboard/tester.png").exists()
    assert (root / "kicad/main.kicad_pcb").exists()
    assert project.check(root)["ok"], project.check(root)["findings"]
    # with folders already, adding a tool just makes its folder
    assert project.add_tool(root, "freecad")["made"] == ["freecad"]


def test_check_flags_a_protoboard_left_in_docs(tmp_path):
    root = make_flat(tmp_path)
    protoboard(root / "docs/tester.json")
    f = next(f for f in project.check(root)["findings"] if f["path"] == "docs/tester.json")
    assert "docs/" in f["problem"] and "project_add_tool protoboard" in f["fix"]


def test_fab_is_asked_once_and_recorded_in_the_brief(tmp_path):
    from cadmate import fab, project

    r = project.init(tmp_path, tools=["kicad"])
    assert "board house" in r["ask_user"]
    assert any("where the board gets made" in f["problem"] for f in project.check(tmp_path)["findings"])
    assert fab.resolve(None, tmp_path)["from"] == "not asked"
    assert "Not asked yet" in (tmp_path / "docs/brief.md").read_text(encoding="utf-8")
    # reading only: nothing recorded yet
    assert project.set_fab(tmp_path)["asked"] is False

    # the user leaves the house open but knows the rest
    r = project.set_fab(tmp_path, house="not decided yet", assembly="no", layers=4)
    assert r["fab"] == {"house": None, "assembly": False, "distributor": None, "layers": 4}
    brief = (tmp_path / "docs/brief.md").read_text(encoding="utf-8")
    assert "- Board house: not decided yet" in brief and "- Assembly by the board house: no" in brief
    assert "- Copper layers: 4" in brief and brief.count("## Fab") == 1
    assert not any("where the board gets made" in f["problem"] for f in project.check(tmp_path)["findings"])
    assert fab.resolve(None, tmp_path) == {"fab": None, "house": None, "assembly": False, "distributor": None, "layers": 4,
                                           "from": "project"}
    # naming a house for one call keeps the project's other answers: they're about the order, not the house
    assert fab.resolve("jlcpcb", tmp_path) == {"fab": "jlcpcb", "house": "jlcpcb", "assembly": False, "distributor": None,
                                                    "layers": 4, "from": "argument"}

    # later they pick one; fields not given keep their value, and the brief's section is replaced, not added
    project.set_fab(tmp_path, house="PCBWay")
    brief = (tmp_path / "docs/brief.md").read_text(encoding="utf-8")
    assert "- Board house: PCBWay" in brief and "- Copper layers: 4" in brief and brief.count("## Fab") == 1
    assert fab.resolve(None, tmp_path)["fab"] == "pcbway"
    assert fab.resolve("none", tmp_path)["fab"] is None  # an explicit choice wins over the project's
    # a house cadmate has no profile for is kept as named
    r = project.set_fab(tmp_path, house="Acme Boards")
    assert "no rule profile for Acme Boards" in r["note"] and fab.resolve(None, tmp_path)["house"] == "Acme Boards"


def test_fab_section_added_to_an_older_brief(tmp_path):
    from cadmate import project

    project.init(tmp_path, tools=["kicad"])
    brief = tmp_path / "docs/brief.md"
    brief.write_text("# old: brief\n\n## Requirements\n\n- [ ] small\n", encoding="utf-8")
    project.set_fab(tmp_path, house="JLCPCB")
    text = brief.read_text(encoding="utf-8")
    assert text.startswith("# old: brief") and "- [ ] small" in text and "- Board house: JLCPCB" in text
    assert project.read_brief(tmp_path)["open"] == ["small"]

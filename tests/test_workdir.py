"""cadmate's own files stay in .cadmate/ of the working folder, and go away when they're done."""

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from cadmate import project, undo, workdir

SRC = Path(__file__).parent.parent / "src" / "cadmate"


def test_only_workdir_and_the_bridge_use_the_system_temp_folder():
    # every other module asks workdir for scratch, so nothing lands in /tmp or $TMPDIR behind the user's back
    users = sorted(p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py")
                   if re.search(r"^\s*(import tempfile|from tempfile import)", p.read_text(encoding="utf-8"), re.M))
    # part_numbers writes each file through a temp file beside it (mkstemp(dir=f.parent)), renamed over it at once
    assert users == ["freecad_scripts/live_bridge.py", "part_numbers.py", "workdir.py"]
    assert "mkstemp(dir=f.parent" in (SRC / "part_numbers.py").read_text(encoding="utf-8")


def test_scratch_is_deleted_and_leaves_no_trace_beside_a_loose_file(tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    with workdir.tmp(board, "drc") as d:
        assert d.parent == tmp_path / ".cadmate/tmp" and d.is_dir()
        assert (tmp_path / ".cadmate/.gitignore").read_text().strip().endswith("*")
        (d / "drc.json").write_text("{}")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b.kicad_pcb"]


def test_scratch_is_deleted_on_an_error_too(tmp_path):
    with pytest.raises(RuntimeError), workdir.tmp(tmp_path, "x") as d:
        (d / "half.json").write_text("{")
        raise RuntimeError("kicad-cli fell over")
    assert not (tmp_path / ".cadmate").exists()


def test_scratch_in_a_project_goes_to_the_project_root(tmp_path):
    root = tmp_path / "p"
    project.init(root, tools=["kicad"])
    (root / "main.kicad_pcb").write_text("(kicad_pcb)")
    with workdir.tmp(root / "main.kicad_pcb") as d:
        assert d.parent == root.resolve() / ".cadmate/tmp"
    # and a library folder's working folder is the folder holding it, never the .pretty itself
    lib = tmp_path / "loose/mine.pretty"
    lib.mkdir(parents=True)
    assert workdir.base(lib / "x.kicad_mod") == lib.parent.resolve()


def test_leftovers_of_a_dead_process_are_swept_and_reported(tmp_path):
    root = tmp_path / "p"
    project.init(root, tools=["kicad"])
    dead = root / ".cadmate/tmp/999999999-drc-0000"
    dead.mkdir(parents=True)
    (dead / "drc.json").write_text("{}")
    f = next(f for f in project.check(root)["findings"] if f["path"] == ".cadmate/tmp")
    assert "didn't finish" in f["problem"]
    with workdir.tmp(root):
        assert not dead.exists()
    assert not any(f["path"] == ".cadmate/tmp" for f in project.check(root)["findings"])


def test_no_folder_to_go_by_uses_the_system_temp_folder_and_still_deletes_it():
    with workdir.tmp(None, "version") as d:
        assert d.is_dir()
    assert not d.exists()


def _write(f: Path, text: str, why: str, t: int):
    undo.save([f], why, now=datetime(2026, 11, 1, 12, 0, t))
    f.write_text(text)


def test_undo_keeps_the_newest_three_and_goes_back_one_at_a_time(tmp_path):
    f = tmp_path / "main.kicad_pcb"
    f.write_text("v0")
    for i in range(1, 6):
        _write(f, f"v{i}", f"route pass {i}", i)
    listed = undo.entries(f)["entries"]
    assert [e["why"] for e in listed] == ["route pass 5", "route pass 4", "route pass 3"]
    assert undo.undo(f)["ok"] and f.read_text() == "v4"
    assert undo.undo(f)["ok"] and f.read_text() == "v3"
    assert undo.undo(f)["ok"] and f.read_text() == "v2"
    assert not undo.undo(f)["ok"] and f.read_text() == "v2"
    assert not (tmp_path / ".cadmate").exists()  # nothing left to keep: the folder goes too


def test_undo_removes_what_the_write_created_and_refuses_out_of_order(tmp_path):
    a, b = tmp_path / "a.kicad_mod", tmp_path / "b.kicad_mod"
    a.write_text("a0")
    undo.save([a, b], "write a footprint pair", now=datetime(2026, 11, 1, 12, 0, 1))
    a.write_text("a1")
    b.write_text("b1")
    first = undo.entries(a)["entries"][0]["name"]
    undo.save([a], "fix pad 1", now=datetime(2026, 11, 1, 12, 0, 2))
    a.write_text("a2")
    r = undo.undo(a, first)
    assert not r["ok"] and "later" in r["error"] and a.read_text() == "a2"
    assert undo.undo(a)["ok"] and a.read_text() == "a1"
    r = undo.undo(a)
    assert r["ok"] and a.read_text() == "a0" and not b.exists() and r["removed"] == ["b.kicad_mod"]
    meta = undo.entries(a)
    assert meta["entries"] == [] and json.dumps(meta)

import json
import os
import time
from datetime import datetime

import pytest

from cadmate import project, release

T0 = datetime(2026, 11, 2, 9, 30)

BOARD = """(kicad_pcb (version 20260206)
  (title_block (title "demo") (rev "{rev}"))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal))
  (net 0 "") (net 1 "GND") (net 2 "VBUS"){extra_net}
  (gr_rect (start 0 0) (end {w} 40) (layer "Edge.Cuts"))
  (footprint "proj:R_0603" (layer "F.Cu")
    (property "Reference" "R1") (property "Value" "{r1}") (property "MPN" "RC0603-{r1}"))
  (footprint "proj:C_0603" (layer "F.Cu")
    (property "Reference" "C1") (property "Value" "100n")){extra_part}
  (segment (start 1 1) (end 2 2) (net 1))
  (via (at 2 2) (net 1))
)
"""


def board_text(rev="", r1="10k", w=50, extra=False):
    return BOARD.format(rev=rev, r1=r1, w=w, extra_net='\n  (net 3 "SDA")' if extra else "",
                        extra_part='\n  (footprint "proj:R_0603" (layer "B.Cu") (property "Reference" "R2") '
                                   '(property "Value" "4k7"))' if extra else "")


def make(tmp_path, text=None):
    root = tmp_path / "demo"
    project.init(root, tools=["kicad"], boards=["main"])
    (root / "main.kicad_pro").write_text("{}")
    (root / "main.kicad_pcb").write_text(text or board_text())
    export(root)
    return root


def export(root):
    """What export_fab leaves in fab/main/, newer than the board."""
    fab = root / "fab/main"
    (fab / "gerbers").mkdir(parents=True, exist_ok=True)
    (fab / "gerbers/main-F_Cu.gbr").write_text("G04*")
    for name, text in (("main-gerbers.zip", "PK"), ("main-bom.csv", "ref\n"), ("main-pos.csv", "ref\n")):
        (fab / name).write_text(text)
    later = max(time.time(), (root / "main.kicad_pcb").stat().st_mtime) + 5
    for f in fab.rglob("*"):
        os.utime(f, (later, later))


def test_first_release_freezes_the_fab_files_and_snapshots_the_project(tmp_path):
    root = make(tmp_path)
    out = release.release(root, notes="first order", render=False, now=T0)
    assert out["ok"] and out["revision"] == "A" and out["folder"] == "releases/main/A"
    rel = root / "releases/main/A"
    assert {"main-gerbers.zip", "main-bom.csv", "main-pos.csv", "RELEASE.md", "release.json"} <= {f.name for f in rel.iterdir()}
    assert not (rel / "gerbers").exists()  # the zip holds them
    snap = root / "history" / out["snapshot"]
    assert out["snapshot"] == "20261102-0930_release-main-a"
    assert (snap / "main.kicad_pcb").exists() and (snap / "fab/main/main-gerbers.zip").exists()
    assert not (snap / "releases").exists()
    assert json.loads((snap / "SNAPSHOT.json").read_text())["release"] == {"board": "main", "revision": "A"}
    notes = (rel / "RELEASE.md").read_text()
    assert "# main revision A" in notes and "First release" in notes and "first order" in notes
    assert "| main | [A](main/A/RELEASE.md)" in (root / "releases/RELEASES.md").read_text()
    assert project.check(root)["counts"]["warning"] == 0


def test_next_revision_lists_what_changed(tmp_path):
    root = make(tmp_path)
    release.release(root, render=False, now=T0)
    (root / "main.kicad_pcb").write_text(board_text(r1="22k", w=55, extra=True))
    later = time.time() + 10
    os.utime(root / "main.kicad_pcb", (later, later))
    with pytest.raises(project.ProjectError, match="older than the board"):
        release.release(root, render=False, now=T0.replace(hour=10))
    export(root)
    out = release.release(root / "main.kicad_pcb", notes="fix pull-up", render=False, now=T0.replace(hour=10))
    assert out["revision"] == "B" and out["previous"] == "A"
    ch = out["changes"]
    assert ch["parts_added"] == ["R2 4k7"] and ch["nets_added"] == ["SDA"]
    assert ch["parts_changed"] == [{"ref": "R1", "value": ["10k", "22k"], "mpn": ["RC0603-10k", "RC0603-22k"]}]
    assert ch["outline_mm"] == [(50.0, 40.0), (55.0, 40.0)]
    notes = (root / "releases/main/B/RELEASE.md").read_text()
    assert "Since A:" in notes and "R1: value 10k -> 22k" in notes and "Board outline: 50.0 x 40.0 mm -> 55.0 x 40.0 mm" in notes


def test_a_release_is_never_overwritten_and_revisions_are_checked(tmp_path):
    root = make(tmp_path)
    release.release(root, revision="b", render=False, now=T0)
    assert (root / "releases/main/B").is_dir()
    export(root)
    with pytest.raises(project.ProjectError, match="already released"):
        release.release(root, revision="B", render=False, now=T0.replace(hour=11))
    with pytest.raises(project.ProjectError, match="revision"):
        release.release(root, revision="v2-final", render=False, now=T0.replace(hour=11))
    assert release.release(root, render=False, now=T0.replace(hour=11))["revision"] == "C"
    assert release._next_rev(["Z"]) == "AA" and release._next_rev(["AZ"]) == "BA"


def test_release_needs_fab_files(tmp_path):
    root = tmp_path / "demo"
    project.init(root, tools=["kicad"], boards=["main"])
    (root / "main.kicad_pro").write_text("{}")
    (root / "main.kicad_pcb").write_text(board_text())
    with pytest.raises(project.ProjectError, match="export_fab"):
        release.release(root, render=False)
    assert not (root / "releases/main").exists()


def test_title_block_revision_mismatch_is_a_warning(tmp_path):
    root = make(tmp_path, board_text(rev="C"))
    out = release.release(root, render=False, now=T0)
    assert "title block says rev C" in out["warnings"][0]


def test_renders_are_copied_and_a_missing_kicad_cli_is_a_note(tmp_path, monkeypatch):
    from cadmate import fab_export

    monkeypatch.setattr(fab_export, "find_kicad_cli", lambda: None)
    root = make(tmp_path)
    (root / "docs/photo.png").write_bytes(b"\x89PNG")
    out = release.release(root, renders=["docs/photo.png"], now=T0)
    assert out["renders"] == ["photo.png"] and (root / "releases/main/A/photo.png").exists()
    assert "kicad-cli not found" in out["note"]


def test_check_flags_a_changed_release(tmp_path):
    root = make(tmp_path)
    release.release(root, render=False, now=T0)
    (root / "releases/main/A/main-bom.csv").write_text("edited\n")
    found = [f for f in project.check(root)["findings"] if f["path"] == "releases/main/A"]
    assert found and "changed after release" in found[0]["problem"] and "main-bom.csv" in found[0]["problem"]


def test_choosing_the_board_in_a_two_board_project(tmp_path):
    root = tmp_path / "demo"
    project.init(root, boards=["main", "sensor"])
    for b in ("main", "sensor"):
        (root / f"kicad/{b}/{b}.kicad_pro").write_text("{}")
        (root / f"kicad/{b}/{b}.kicad_pcb").write_text(board_text())
    with pytest.raises(project.ProjectError, match="which board"):
        release.release(root, render=False)
    fab = root / "fab/sensor"
    fab.mkdir(parents=True)
    (fab / "sensor-gerbers.zip").write_text("PK")
    later = time.time() + 5
    os.utime(fab / "sensor-gerbers.zip", (later, later))
    assert release.release(root, board="sensor", render=False, now=T0)["folder"] == "releases/sensor/A"


def test_prune_keeps_labelled_and_released_snapshots(tmp_path):
    root = make(tmp_path)
    project.snapshot(root, "approved-layout", note="user approved the placement", now=T0)
    for i in range(4):
        project.snapshot(root, f"before-route-{i}", ["main.kicad_pcb"], note="automatic, before routing",
                         now=T0.replace(hour=10, minute=i))
    rel = release.release(root, render=False, now=T0.replace(hour=12))
    dry = project.prune(root, keep=1)
    assert [s["name"] for s in dry["would_delete"]] == [f"20261102-100{i}_before-route-{i}" for i in range(3)]
    assert dry["deleted"] == [] and len(project.history(root)["snapshots"]) == 6
    done = project.prune(root, keep=1, confirm=True)
    left = {s["name"]: s["kind"] for s in project.history(root)["snapshots"]}
    assert left == {"20261102-0930_approved-layout": "labelled", "20261102-1003_before-route-3": "automatic",
                    rel["snapshot"]: "release"}
    assert len(done["deleted"]) == 3
    index = (root / "history/HISTORY.md").read_text()
    assert "~~20261102-1000_before-route-0~~ (pruned" in index and "[20261102-1003_before-route-3]" in index


def test_check_suggests_pruning_when_automatic_snapshots_pile_up(tmp_path):
    root = make(tmp_path)
    for i in range(project.KEEP_AUTOMATIC + 2):
        project.snapshot(root, f"before-route-{i}", ["main.kicad_pcb"], note="automatic, before routing",
                         now=T0.replace(minute=i))
    found = [f for f in project.check(root)["findings"] if f["path"] == "history"]
    assert found and "project_prune" in found[0]["fix"]

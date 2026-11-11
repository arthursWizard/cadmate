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
    # ASME Y14.35: no I, O, Q, S, X or Z
    assert release._next_rev(["H"]) == "J" and release._next_rev(["Y"]) == "AA" and release._next_rev(["AY"]) == "BA"
    assert release._next_rev(["YY"]) == "AAA" and release.REV.match("AAA")


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
        project.snapshot(root, f"before-route-{i}", ["main.kicad_pcb"], note="automatic, before routing", automatic=True,
                         now=T0.replace(hour=10, minute=i))
    rel = release.release(root, render=False, now=T0.replace(hour=12))
    dry = project.prune(root, keep=1)
    assert [s["name"] for s in dry["would_delete"]] == [f"20261102-100{i}_before-route-{i}" for i in range(3)]
    assert dry["deleted"] == [] and len(project.history(root)["snapshots"]) == 6
    done = project.prune(root, keep=1, delete=[x["name"] for x in dry["would_delete"]])
    left = {s["name"]: s["kind"] for s in project.history(root)["snapshots"]}
    assert left == {"20261102-0930_approved-layout": "labelled", "20261102-1003_before-route-3": "automatic",
                    rel["snapshot"]: "release"}
    assert len(done["deleted"]) == 3
    index = (root / "history/HISTORY.md").read_text()
    assert "~~20261102-1000_before-route-0~~ (pruned" in index and "[20261102-1003_before-route-3]" in index


def test_check_suggests_pruning_when_automatic_snapshots_pile_up(tmp_path):
    root = make(tmp_path)
    for i in range(project.KEEP_AUTOMATIC + 2):
        project.snapshot(root, f"before-route-{i}", ["main.kicad_pcb"], note="automatic, before routing", automatic=True,
                         now=T0.replace(minute=i))
    found = [f for f in project.check(root)["findings"] if f["path"] == "history"]
    assert found and "project_prune" in found[0]["fix"]


def test_a_deliberate_snapshot_is_never_automatic_whatever_its_note(tmp_path):
    root = make(tmp_path)
    project.snapshot(root, "routed", note="Automatically routed, user approved", now=T0)
    project.snapshot(root, "approved", note="automatic, before routing (said the user)", now=T0.replace(minute=31))
    assert {s["kind"] for s in project.history(root)["snapshots"]} == {"labelled"}
    assert project.prune(root, keep=0)["would_delete"] == []


def test_legacy_snapshots_go_by_the_tools_note(tmp_path):
    root = make(tmp_path)
    for i, note in enumerate(("automatic, before autoroute on main.kicad_pcb", "automatically fine")):
        d = root / f"history/20261001-120{i}_legacy-{i}"
        d.mkdir(parents=True)
        (d / "SNAPSHOT.json").write_text(json.dumps({"label": f"legacy-{i}", "note": note, "paths": []}))
    kinds = {s["name"]: s["kind"] for s in project.history(root)["snapshots"]}
    assert kinds == {"20261001-1200_legacy-0": "automatic", "20261001-1201_legacy-1": "labelled"}
    assert [x["name"] for x in project.prune(root, keep=0)["would_delete"]] == ["20261001-1200_legacy-0"]


def test_prune_deletes_only_the_listed_snapshots(tmp_path):
    root = make(tmp_path)
    for i in range(2):
        project.snapshot(root, f"before-route-{i}", ["main.kicad_pcb"], note="automatic, before routing", automatic=True,
                         now=T0.replace(minute=i))
    listed = [x["name"] for x in project.prune(root, keep=1)["would_delete"]]
    assert listed == ["20261102-0900_before-route-0"]
    # a routing pass after the list was shown: it must not shift what goes
    project.snapshot(root, "before-route-2", ["main.kicad_pcb"], note="automatic, before routing", automatic=True,
                     now=T0.replace(minute=2))
    out = project.prune(root, keep=1, delete=listed + ["20261102-0930_nope"])
    assert [x["name"] for x in out["deleted"]] == listed and out["not_deleted"] == ["20261102-0930_nope"]
    assert len(project.history(root)["snapshots"]) == 2
    with pytest.raises(project.ProjectError, match="names"):
        project.prune(root, keep=0, delete=[])


def test_a_failed_release_leaves_no_snapshot_and_never_deletes_an_existing_release(tmp_path, monkeypatch):
    root = make(tmp_path)
    (root / "docs/a").mkdir()
    (root / "docs/b").mkdir()
    (root / "docs/a/top.png").write_bytes(b"1")
    (root / "docs/b/top.png").write_bytes(b"2")
    with pytest.raises(project.ProjectError, match="two files named top.png"):
        release.release(root, renders=["docs/a", "docs/b"], render=False, now=T0)
    assert not (root / "releases/main/A").exists()
    assert project.history(root)["snapshots"] == []
    assert "release-main-a" not in (root / "history/HISTORY.md").read_text()
    # revision A appears between the check and the copy (another session): it is left alone
    real = project.snapshot

    def racing(*a, **kw):
        out = real(*a, **kw)
        (root / "releases/main/A").mkdir(parents=True, exist_ok=True)
        (root / "releases/main/A/keep.txt").write_text("theirs")
        return out
    monkeypatch.setattr(project, "snapshot", racing)
    with pytest.raises(project.ProjectError, match="meanwhile"):
        release.release(root, render=False, now=T0)
    assert (root / "releases/main/A/keep.txt").read_text() == "theirs"
    assert project.history(root)["snapshots"] == []


def test_a_broken_snapshot_record_is_flagged_not_a_crash(tmp_path):
    root = make(tmp_path)
    project.snapshot(root, "before-route", ["main.kicad_pcb"], note="automatic, before routing", automatic=True, now=T0)
    (root / "history/20261102-0930_before-route/SNAPSHOT.json").write_text('{"label": "befo')
    snaps = project.history(root)["snapshots"]
    assert snaps[0]["kind"] == "labelled" and snaps[0]["unreadable"]
    found = [f for f in project.check(root)["findings"] if f["path"].endswith("SNAPSHOT.json")]
    assert found and "can't be read" in found[0]["problem"]
    assert project.prune(root, keep=0)["would_delete"] == []


def test_fab_files_from_another_state_of_the_board_are_refused(tmp_path):
    root = make(tmp_path)
    old = time.time() - 1000
    os.utime(root / "main.kicad_pcb", (old, old))
    snap = project.snapshot(root, "approved", ["main.kicad_pcb"], now=T0)["name"]
    (root / "main.kicad_pcb").write_text(board_text(r1="22k"))
    (root / "fab/main/export.json").write_text(json.dumps({"board": "main", "board_sha256": release._sha(root / "main.kicad_pcb")}))
    # back to the approved board: its file time is restored, but the fab files are the 22k board's
    project.restore(root, snap, ["main.kicad_pcb"], now=T0.replace(hour=11))
    with pytest.raises(project.ProjectError, match="another state of the board"):
        release.release(root, render=False, now=T0.replace(hour=12))
    # without an export record, file times decide: the restore made the board newer than the export, though the
    # snapshot kept the board's old file time
    (root / "fab/main/export.json").unlink()
    earlier = time.time() - 100
    for f in (root / "fab/main").rglob("*"):
        os.utime(f, (earlier, earlier))
    with pytest.raises(project.ProjectError, match="older than the board"):
        release.release(root, render=False, now=T0.replace(hour=12))

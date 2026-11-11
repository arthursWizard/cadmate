import os
import time

from cadmate import apps, pcb_review, project, ready, schematic_review
from test_pcb_review import HEAD, footprint, pad

CLEAN_DRC = {"violations": [], "unconnected_items": [], "schematic_parity": []}


def resistor(ref, x, fields=""):
    fp = footprint("Resistor_SMD:R_0603_1608Metric", ref, "10k", x, 20, [pad(1, -0.8, 0, "A"), pad(2, 0.8, 0, "A")],
                   court=((-1.5, -0.75), (1.5, 0.75)))
    return fp.replace('(attr smd)', f'{fields} (attr smd)')


def write(folder, *parts, name="main"):
    pcb = folder / f"{name}.kicad_pcb"
    pcb.write_text(HEAD + "".join(parts) + ")\n", encoding="utf-8", newline="\n")
    pcb.with_suffix(".kicad_pro").write_text("{}\n", encoding="utf-8")
    return pcb


def fake_kicad(monkeypatch, drc=CLEAN_DRC, erc=None):
    monkeypatch.setattr(pcb_review, "run_drc", lambda f: drc)
    monkeypatch.setattr(schematic_review, "run_erc", lambda f: erc if erc is not None else {"sheets": []})
    monkeypatch.setattr(apps, "find_kicad_cli", lambda: "kicad-cli")


def export_files(pcb):
    d = pcb.parent / "fab" / pcb.stem
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pcb.stem}-bom.csv").write_text("x\n", encoding="utf-8")
    later = time.time() + 5
    os.utime(d / f"{pcb.stem}-bom.csv", (later, later))


def test_a_clean_board_with_everything_done_is_ready(tmp_path, monkeypatch):
    fake_kicad(monkeypatch)
    project.init(tmp_path, tools=["kicad"])
    project.set_fab(tmp_path, house="undecided")
    brief = tmp_path / "docs/brief.md"
    brief.write_text(brief.read_text(encoding="utf-8").replace("## Requirements\n", "## Requirements\n\n- [x] two holes\n"),
                     encoding="utf-8")
    pcb = write(tmp_path, resistor("R1", 20, '(property "MPN" "RC0603FR-0710KL")'))
    (tmp_path / "main.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    export_files(pcb)
    r = ready.check(str(pcb))
    assert r["ready"], r["blocking"]
    assert "DRC: 0 errors, 0 warnings, 0 unconnected, 0 parity" in r["passed"]
    assert "ERC: 0 errors, 0 warnings" in r["passed"] and "brief: all 1 requirement(s) met" in r["passed"]
    # no house chosen: not blocking, but asked about, and nothing vendor-specific ran
    assert any(i["check"] == "fab" for i in r["to_look_at"]) and "board house" in r["ask_user"]
    assert r["fab"]["fab"] is None


def test_what_blocks_an_order(tmp_path, monkeypatch):
    drc = {"violations": [{"severity": "error", "type": "clearance"}, {"severity": "warning", "type": "silk_overlap"}],
           "unconnected_items": [{"items": [{"description": "pad", "pos": {"x": 1, "y": 1}}]}],
           "schematic_parity": [{"severity": "error", "type": "missing_footprint", "description": "R9"}]}
    erc = {"sheets": [{"violations": [{"severity": "error", "type": "pin_not_connected"}]}]}
    fake_kicad(monkeypatch, drc, erc)
    project.init(tmp_path, tools=["kicad"])
    project.set_fab(tmp_path, house="JLCPCB", assembly="yes")
    brief = tmp_path / "docs/brief.md"
    brief.write_text(brief.read_text(encoding="utf-8").replace("## Requirements\n", "## Requirements\n\n- [ ] fits the case\n"),
                     encoding="utf-8")
    pcb = write(tmp_path, resistor("R1", 20, '(property "MPN" "RC0603FR-0710KL")'), resistor("R2", 30))
    (tmp_path / "main.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    export_files(pcb)
    os.utime(pcb, (time.time() + 60, time.time() + 60))  # the board changed after the export
    r = ready.check(str(pcb))
    assert not r["ready"]
    whats = " | ".join(i["what"] for i in r["blocking"])
    for expected in ("DRC: 1 error(s): 1 × clearance", "1 unconnected item", "schematic and board disagree",
                     "ERC: 1 error(s)", "1 part(s) with no part number: R2", "without an LCSC number for JLCPCB assembly: R1, R2",
                     "older than the board", "not ticked"):
        assert expected in whats, expected
    assert any("silk_overlap" in i["what"] for i in r["to_look_at"])


def test_not_ready_without_drc(tmp_path, monkeypatch):
    monkeypatch.setattr(pcb_review, "run_drc", lambda f: None)
    monkeypatch.setattr(apps, "find_kicad_cli", lambda: None)
    pcb = write(tmp_path, resistor("R1", 20))
    r = ready.check(str(pcb))
    assert not r["ready"] and "DRC didn't run" in r["why_not_ready"]
    # outside a project, a part without a number is to look at (needed to buy it), not a blocker: assembly isn't known
    assert any(i["check"] == "bom" for i in r["to_look_at"]) and not any(i["check"] == "bom" for i in r["blocking"])

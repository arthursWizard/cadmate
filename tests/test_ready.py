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


def export_files(pcb, jlc=False, d=None):
    d = d or pcb.parent / "fab" / pcb.stem
    d.mkdir(parents=True, exist_ok=True)
    names = ["gerbers.zip", "bom.csv", "pos.csv"] + (["bom-jlc.csv", "cpl-jlc.csv"] if jlc else [])
    later = time.time() + 5
    for n in names:
        (d / f"{pcb.stem}-{n}").write_text("x\n", encoding="utf-8")
        os.utime(d / f"{pcb.stem}-{n}", (later, later))


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
    export_files(pcb, jlc=True)
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


def test_naming_the_house_keeps_the_projects_assembly_choice(tmp_path, monkeypatch):
    """fab="jlcpcb" in a project that says JLCPCB assembles it must still block on missing part numbers."""
    fake_kicad(monkeypatch)
    project.init(tmp_path, tools=["kicad"])
    project.set_fab(tmp_path, house="JLCPCB", assembly="yes")
    pcb = write(tmp_path, resistor("R1", 20))
    for fab in (None, "jlcpcb", "JLCPCB"):
        r = ready.check(str(pcb), fab=fab)
        assert not r["ready"] and r["fab"]["assembly"] is True, fab
        whats = " | ".join(i["what"] for i in r["blocking"])
        assert "no part number: R1" in whats and "without an LCSC number" in whats, fab


def test_fab_files_must_match_the_house(tmp_path, monkeypatch):
    fake_kicad(monkeypatch)
    project.init(tmp_path, tools=["kicad"])
    project.set_fab(tmp_path, house="undecided")
    pcb = write(tmp_path, resistor("R1", 20, '(property "MPN" "X1")'))
    export_files(pcb)  # exported while undecided: generic files only
    assert "fab files complete and newer than the board" in ready.check(str(pcb))["passed"]
    project.set_fab(tmp_path, house="JLCPCB", assembly="yes")  # then JLCPCB was chosen: its own files are missing
    r = ready.check(str(pcb))
    assert any("fab files missing: main-bom-jlc.csv, main-cpl-jlc.csv" in i["what"] for i in r["blocking"])
    # exported somewhere else: found with fab_dir
    other = tmp_path / "out"
    export_files(pcb, d=other)
    assert not any(i["check"] == "fab_files" for i in ready.check(str(pcb), fab="none", fab_dir=str(other))["blocking"])


def test_answers_are_not_asked_again(tmp_path, tmp_path_factory, monkeypatch):
    fake_kicad(monkeypatch)
    project.init(tmp_path, tools=["kicad"])
    project.set_fab(tmp_path, house="undecided")
    pcb = write(tmp_path, resistor("R1", 20))
    assert "ask_user" in ready.check(str(pcb))  # undecided in the project: ask now, at order time
    r = ready.check(str(pcb), fab="none")  # the user just said no house: that's an answer
    assert "ask_user" not in r and not any(i["check"] == "fab" for i in r["to_look_at"])
    # outside a project there's nowhere to keep an answer, so no question either
    loose = tmp_path_factory.mktemp("loose")  # not under the project
    r = ready.check(str(write(loose, resistor("R1", 20))))
    assert "ask_user" not in r and any("no board house given" in i["what"] for i in r["to_look_at"])


def test_empty_brief_distributor_and_layers(tmp_path, monkeypatch):
    fake_kicad(monkeypatch)
    project.init(tmp_path, tools=["kicad"])
    project.set_fab(tmp_path, house="undecided", distributor="DigiKey", layers=4)
    pcb = write(tmp_path, resistor("R1", 20, '(property "MPN" "X1")'))
    r = ready.check(str(pcb))
    assert not any(p.startswith("brief:") for p in r["passed"])
    looks = " | ".join(i["what"] for i in r["to_look_at"])
    assert "the brief lists no requirements" in looks and "1 part(s) without a DigiKey number: R1" in looks
    assert any("the board has 2 copper layers; the project says 4" in i["what"] for i in r["blocking"])


def test_autoroute_uses_the_projects_board_house(tmp_path, monkeypatch):
    from cadmate import freerouting, pcb_flow

    seen = {}

    def fake(src, dst, widths, passes, timeout, lim):
        seen["lim"] = lim
        return {"ok": True}
    monkeypatch.setattr(freerouting, "autoroute_file", fake)
    project.init(tmp_path, tools=["kicad"])
    pcb = write(tmp_path, resistor("R1", 20))
    out = pcb_flow.autoroute(str(pcb), out_path=str(tmp_path / "routed.kicad_pcb"), neck_rules=False)
    assert seen["lim"] is None and out["fab_choice"]["from"] == "not asked"  # nothing assumed
    project.set_fab(tmp_path, house="PCBWay")
    pcb_flow.autoroute(str(pcb), out_path=str(tmp_path / "routed.kicad_pcb"), neck_rules=False)
    assert seen["lim"]["fab"] == "pcbway"
    pcb_flow.autoroute(str(pcb), out_path=str(tmp_path / "routed.kicad_pcb"), fab_name="none", neck_rules=False)  # an explicit "none" wins over the project
    assert seen["lim"] is None


def test_package_bought_against_footprint(tmp_path, monkeypatch):
    fake_kicad(monkeypatch)
    pcb = write(tmp_path, resistor("R1", 20, '(property "MPN" "X1") (property "Package" "0805")'),
                resistor("R2", 30, '(property "MPN" "X2")'))
    r = ready.check(str(pcb))
    assert any("R1 (0805) on footprint R_0603_1608Metric: the part is 0805" in i["what"] for i in r["blocking"])
    # packages from a part search, without editing the schematic; the argument wins over the field
    r = ready.check(str(pcb), packages={"R1": "0603", "R2": "0603 (1608 Metric)", "R9": "0402"})
    assert not any(i["check"] == "package" for i in r["blocking"])
    assert "packages match their footprints (2 part(s) with a package to compare)" in r["passed"]
    assert any("not on the board: R9" in i["what"] for i in r["to_look_at"])
    # an empty package skips a part whose Package field is wrong, rather than falling back to it
    r = ready.check(str(pcb), packages={"R1": ""})
    assert not any(i["check"] == "package" for i in r["blocking"])

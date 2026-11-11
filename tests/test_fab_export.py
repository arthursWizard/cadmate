import hashlib
import json

from cadmate import fab_export as fx

BOARD = '''(kicad_pcb (version 20250926)
  (layers (0 "F.Cu" signal) (4 "In1.Cu" power) (6 "In2.Cu" power) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu") (at 10 10)
    (property "Reference" "R2") (property "Value" "10k") (property "LCSC" "C25744") (attr smd))
  (footprint "Resistor_SMD:R_0402_1005Metric" (layer "B.Cu") (at 12 10)
    (property "Reference" "R10") (property "Value" "10k") (property "LCSC" "C25744") (attr smd))
  (footprint "Capacitor_SMD:C_0402_1005Metric" (layer "F.Cu") (at 14 10)
    (property "Reference" "C1") (property "Value" "100n") (attr smd))
  (footprint "cadmate:MountingHole_2.2mm_M2_SMDStandoff" (layer "B.Cu") (at 0 0)
    (property "Reference" "H1") (property "Value" "M2") (attr smd exclude_from_bom exclude_from_pos_files))
  (footprint "TestPoint:TestPoint_Pad_D1.0mm" (layer "B.Cu") (at 1 1)
    (property "Reference" "TP1") (property "Value" "SWCLK") (attr smd dnp))
)
'''

POS = """Ref,Val,Package,PosX,PosY,Rot,Side
"C1","100n","C_0402",114.0,-110.0,90.0,top
"R10","10k","R_0402",112.0,-110.0,180.0,bottom
"R2","10k","R_0402",110.0,-110.0,-90.0,top
"H1","M2","Hole",100.0,-100.0,0.0,bottom
"""


def test_bom_groups_by_value_footprint_and_part_and_lists_missing_numbers():
    parts = fx.footprints(BOARD)
    assert {p["ref"]: p["side"] for p in parts}["R10"] == "bottom"
    bom = fx.jlc_bom(parts).splitlines()
    assert bom[0] == "Comment,Designator,Footprint,LCSC Part #"
    assert bom[1:] == ['100n,C1,C_0402_1005Metric,', '10k,"R2,R10",R_0402_1005Metric,C25744']
    assert not any("H1" in l or "TP1" in l for l in bom)


def test_cpl_in_jlc_columns_without_holes_or_dnp():
    cpl = fx.jlc_cpl(POS, fx.footprints(BOARD)).splitlines()
    assert cpl[0] == "Designator,Mid X,Mid Y,Layer,Rotation"
    assert cpl[1:] == ["C1,114.0000mm,-110.0000mm,Top,90", "R2,110.0000mm,-110.0000mm,Top,270", "R10,112.0000mm,-110.0000mm,Bottom,180"]


def test_copper_layers_in_stack_order():
    assert fx.copper_layers(BOARD) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def test_placeholder_part_numbers_are_not_part_numbers():
    assert [fx.placeholder(x) for x in ("C25744", "", "TBD", "C25900?", " C1525 ")] == [False, True, True, True, False]


MPN_BOARD = BOARD.replace('(property "Value" "100n")', '(property "Value" "100n") (property "Manufacturer" "Samsung") '
                                                        '(property "MPN" "CL05B104KO5NNNC") (property "DigiKey" "1276-1001-1-ND")')


def test_generic_bom_puts_the_manufacturer_part_first_and_only_the_distributors_in_use():
    bom = fx.generic_bom(fx.footprints(MPN_BOARD)).splitlines()
    assert bom[0] == "Designator,Quantity,Value,Footprint,Manufacturer,MPN,LCSC,DigiKey"
    assert bom[1:] == ["C1,1,100n,C_0402_1005Metric,Samsung,CL05B104KO5NNNC,,1276-1001-1-ND",
                       '"R2,R10",2,10k,R_0402_1005Metric,,,C25744,']
    # field names as schematics spell them
    other = BOARD.replace('(property "LCSC" "C25744")', '(property "Manufacturer_Part_Number" "RC0402FR-0710KL")', 1)
    assert fx.footprints(other)[0]["mpn"] == "RC0402FR-0710KL"


def test_generic_pos_keeps_placed_parts_in_mm():
    pos = fx.generic_pos(POS, fx.footprints(BOARD)).splitlines()
    assert pos[0] == "Designator,Value,Footprint,X (mm),Y (mm),Rotation,Side"
    assert pos[1:] == ["C1,100n,C_0402,114.0000,-110.0000,90,top", "R2,10k,R_0402,110.0000,-110.0000,270,top",
                       "R10,10k,R_0402,112.0000,-110.0000,180,bottom"]


def _fake_cli(monkeypatch):
    def cli(*args, timeout=300):
        out = args[args.index("--output") + 1]
        if "gerbers" in args or "drill" in args:
            from pathlib import Path
            Path(out, "board-F_Cu.gbr" if "gerbers" in args else "board.drl").write_text("G04*", encoding="utf-8")
        else:
            with open(out, "w", encoding="utf-8") as f:
                f.write(POS)
        return ""
    monkeypatch.setattr(fx, "_cli", cli)


def test_export_assumes_no_board_house(tmp_path, monkeypatch):
    _fake_cli(monkeypatch)
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(MPN_BOARD, encoding="utf-8")
    r = fx.export(pcb, refill=False)
    names = sorted(p.name for p in (tmp_path / "fab" / "board").iterdir())
    assert names == ["board-bom.csv", "board-gerbers.zip", "board-pos.csv", "export.json", "gerbers"]
    # which state of the board the files are, so a release can tell they belong to it
    rec = json.loads((tmp_path / "fab/board/export.json").read_text())
    assert rec["board_sha256"] == hashlib.sha256(pcb.read_bytes()).hexdigest()
    assert r["fab"]["fab"] is None and "jlc_bom" not in r
    # C1 has an MPN, R2 and R10 an LCSC number: every part has some number
    assert r["no_part_number"] == []
    # with JLCPCB chosen, its own BOM and CPL come too, and a later neutral export removes them again
    r = fx.export(pcb, refill=False, fab="jlcpcb")
    assert r["no_lcsc_number"] == ["C1"] and (tmp_path / "fab" / "board" / "board-cpl-jlc.csv").exists()
    fx.export(pcb, refill=False, fab="none")
    assert not (tmp_path / "fab" / "board" / "board-bom-jlc.csv").exists()


def test_export_follows_the_project_choice(tmp_path, monkeypatch):
    from cadmate import project

    _fake_cli(monkeypatch)
    project.init(tmp_path, tools=["kicad"])
    project.set_fab(tmp_path, house="JLCPCB", assembly="yes")
    pcb = tmp_path / "main.kicad_pcb"
    pcb.write_text(BOARD, encoding="utf-8")
    r = fx.export(pcb, refill=False)
    assert r["fab"]["from"] == "project" and r["fab"]["fab"] == "jlcpcb" and "jlc_bom" in r
    assert r["no_part_number"] == ["C1"]


def test_jlcpcb_part_number_field_is_an_lcsc_number():
    other = BOARD.replace('(property "LCSC" "C25744")', '(property "JLCPCB Part #" "C25744")', 1)
    assert fx.footprints(other)[0]["lcsc"] == "C25744"


def test_board_step_goes_to_the_parts_pcb_models(tmp_path, monkeypatch):
    from pathlib import Path

    import pytest

    from cadmate import project

    def cli(*args, timeout=300):
        Path(args[args.index("--output") + 1]).write_text("ISO-10303-21;\n", encoding="utf-8")
        return ""
    monkeypatch.setattr(fx, "_cli", cli)
    # KiCad only, or outside a project: beside the board
    pcb = tmp_path / "loose" / "main.kicad_pcb"
    pcb.parent.mkdir()
    pcb.write_text(BOARD, encoding="utf-8")
    assert fx.step_target(pcb) == pcb.with_suffix(".step")
    # KiCad and FreeCAD, one part: its pcb-models/
    root = tmp_path / "demo"
    project.init(root, tools=["kicad", "freecad"], parts=["enclosure"])
    pcb = root / "kicad" / "main.kicad_pcb"
    pcb.write_text(BOARD, encoding="utf-8")
    r = fx.export_step(pcb)
    assert Path(r["step"]) == root / "freecad" / "pcb-models" / "main.step"
    with pytest.raises(fx.ExportError, match="no FreeCAD part 'knobz'"):
        fx.step_target(pcb, "knobz")
    # every footprint in BOARD lacks a model link; the reply says so (TP1 is DNP, left out of the STEP on purpose)
    assert r["no_3d_model"] == ["C1", "H1", "R2", "R10"]
    # two parts: say which
    (root / "freecad" / "enclosure").mkdir()
    (root / "freecad" / "enclosure" / "enclosure.FCStd").write_bytes(b"")
    (root / "freecad" / "knob").mkdir()
    (root / "freecad" / "knob" / "knob.FCStd").write_bytes(b"")
    with pytest.raises(fx.ExportError, match="which FreeCAD part"):
        fx.step_target(pcb)
    assert fx.step_target(pcb, "enclosure") == root / "freecad" / "enclosure" / "pcb-models" / "main.step"


def test_board_step_names_parts_whose_model_didnt_make_it(tmp_path, monkeypatch):
    from pathlib import Path

    import pytest
    from test_pcb_review import HEAD, footprint, pad

    said = {"text": ""}

    def cli(*args, timeout=300):
        Path(args[args.index("--output") + 1]).write_text("ISO-10303-21;\n", encoding="utf-8")
        return said["text"]
    monkeypatch.setattr(fx, "_cli", cli)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "ok.step").write_text("x", encoding="utf-8")
    (tmp_path / "models" / "swapped.wrl").write_text("x", encoding="utf-8")
    (tmp_path / "models" / "swapped.step").write_text("x", encoding="utf-8")
    (tmp_path / "models" / "vrml.wrl").write_text("x", encoding="utf-8")

    def part(ref, x, model="", attr="smd"):
        return footprint("Lib:P", ref, "v", x, 20, [pad(1, 0, 0, "A")]).replace("(attr smd)", f"(attr {attr}) {model}")
    parts = [part("U1", 10, '(model "${KIPRJMOD}/models/ok.step")'),
             part("U2", 20, '(model "${KIPRJMOD}/models/swapped.wrl")'),  # --subst-models uses the STEP beside it
             part("U3", 30, '(model "${KIPRJMOD}/models/vrml.wrl")'),
             part("U4", 40, '(model "${KIPRJMOD}/models/gone.step")'),
             part("U5", 50, '(model "${KIPRJMOD}/models/ok.step" (hide yes))'),
             part("U6", 60, '(model "${CADMATE_TEST_UNSET_DIR}/lib.3dshapes/far.step")'),  # only kicad-cli can tell
             part("U7", 70, attr="smd dnp")]
    pcb = tmp_path / "main.kicad_pcb"
    pcb.write_text(HEAD + "".join(parts) + ")\n", encoding="utf-8")
    monkeypatch.delenv("CADMATE_TEST_UNSET_DIR", raising=False)
    r = fx.export_step(pcb)
    assert r["no_3d_model"] == ["U5"]  # hidden models aren't exported; U7 is DNP and left out
    assert sorted(r["model_not_loaded"]) == ["U3", "U4"]
    assert "VRML only" in r["model_not_loaded"]["U3"] and "file not found" in r["model_not_loaded"]["U4"]
    # what kicad-cli says about a model it couldn't load counts too
    said["text"] = "Build STEP data\nCould not add 3D model to U6.\nFile not found: /opt/lib.3dshapes/far.step\n"
    r = fx.export_step(pcb)
    assert sorted(r["model_not_loaded"]) == ["U3", "U4", "U6"] and "far.step" in r["kicad_cli_said"][-1]
    # a part name is checked even when there's no part to choose between
    with pytest.raises(fx.ExportError, match="only applies in a cadmate project"):
        fx.step_target(pcb, "knobz")


def test_kicad_cli_timeout_is_an_export_error(monkeypatch):
    import subprocess

    import pytest

    monkeypatch.setattr(fx, "find_kicad_cli", lambda: "kicad-cli")

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("kicad-cli", 600)
    monkeypatch.setattr(fx.subprocess, "run", slow)
    with pytest.raises(fx.ExportError, match="didn't finish in 10 min"):
        fx._cli("pcb", "export", "step", timeout=600)

from pathlib import Path

import pytest

from cadmate import project, sexpr
from cadmate import schematic_write as sw

FIX = Path(__file__).parent / "fixtures"
LIBS = {"Device": str(FIX / "sch" / "Device.kicad_sym"), "power": str(FIX / "sch" / "power.kicad_sym"),
        "Demo": str(FIX / "sch" / "Demo.kicad_sym"), "Reg": str(FIX / "Symbols.kicad_symdir")}

PARTS = [{"ref": "U1", "symbol": "Demo:MCU8", "value": "MCU8", "block": "MCU"},
         {"ref": "C1", "symbol": "Device:C", "value": "100n", "block": "MCU"},
         {"ref": "R1", "symbol": "Device:R", "value": "10k", "block": "MCU"},
         {"ref": "R2", "symbol": "Device:R", "value": "1k", "block": "LED"},
         {"ref": "R3", "symbol": "Device:R", "value": "1k", "block": "LED"}]
NETS = {"+3V3": ["U1.VDD", "C1.1", "R1.1"], "GND": ["U1.GND", "C1.2", "R3.2"], "RST": ["U1.7", "R1.2"],
        "LED": ["U1.PB0", "R2.1"], "MID": ["R2.2", "R3.1"]}


def items(path, head):
    root = sexpr.parse(Path(path).read_text(encoding="utf-8"))
    return root.children(head)


def lib_ids(path):
    return [(s.child("lib_id").atoms() or [""])[0] for s in items(path, "symbol")]


def test_a_sheet_from_parts_and_nets(tmp_path):
    out = tmp_path / "demo.kicad_sch"
    r = sw.write(str(out), PARTS, NETS, no_connect=["U1.8"], libraries=LIBS, erc=False)
    assert r["ok"] and out.exists()
    assert r["review"]["counts"]["problem"] == 0 and r["review"]["counts"]["warning"] == 0, r["review"]["findings"]
    assert "MID" in r["wired"]  # R2 and R3 sit in the same block: a wire, not two labels
    assert "LED" in r["labelled_between_blocks"]  # U1 and R2 are in different blocks
    assert r["unconnected_pins"] == ["U1.3", "U1.4", "U1.6"]
    ids = lib_ids(out)
    assert ids.count("power:GND") == 3 and ids.count("power:+3V3") == 3
    assert ids.count("power:PWR_FLAG") == 2  # nothing drives +3V3 or GND on this sheet
    assert len(items(out, "no_connect")) == 1
    labels = [n.atoms()[0] for n in items(out, "label")]
    assert labels.count("LED") == 2 and "MID" not in labels
    # every symbol the sheet uses is embedded once, under its library name
    embedded = [s.atoms()[0] for s in sexpr.parse(out.read_text()).child("lib_symbols").children("symbol")]
    assert sorted(embedded) == ["Demo:MCU8", "Device:C", "Device:R", "power:+3V3", "power:GND", "power:PWR_FLAG"]


def test_parts_in_line_get_a_straight_wire(tmp_path):
    out = tmp_path / "div.kicad_sch"
    parts = [{"ref": "R1", "symbol": "Device:R", "value": "10k", "at": [50.8, 50.8]},
             {"ref": "R2", "symbol": "Device:R", "value": "10k", "at": [50.8, 63.5]}]
    r = sw.write(str(out), parts, {"MID": ["R1.2", "R2.1"], "VIN": ["R1.1"], "GND": ["R2.2"]}, libraries=LIBS,
                 erc=False)
    wires = [sorted(tuple(xy.floats()) for xy in w.child("pts").children("xy")) for w in items(out, "wire")]
    assert [(50.8, 54.61), (50.8, 59.69)] in wires
    assert r["review"]["stats"]["two_terminal_by_label_only"] == 0


def test_a_derived_library_symbol_is_flattened(tmp_path):
    out = tmp_path / "reg.kicad_sch"
    parts = [{"ref": "U1", "symbol": "Reg:AMS1117-3.3", "at": [76.2, 50.8]},
             {"ref": "C1", "symbol": "Device:C", "value": "10u", "at": [96.52, 55.88]}]
    r = sw.write(str(out), parts, {"+3V3": ["U1.VO", "C1.1"], "GND": ["U1.GND", "C1.2"], "VIN": ["U1.VI"]},
                 libraries=LIBS, erc=False)
    lib = next(s for s in sexpr.parse(out.read_text()).child("lib_symbols").children("symbol")
               if s.atoms()[0] == "Reg:AMS1117-3.3")
    assert lib.child("extends") is None
    assert {s.atoms()[0] for s in lib.children("symbol")} >= {"AMS1117-3.3_1_1"}
    # the regulator's output drives +3V3, so only GND needs a PWR_FLAG
    assert lib_ids(out).count("power:PWR_FLAG") == 1
    assert lib_ids(out).count("power:+3V3") == 2  # power nets get a symbol at each pin, not a wire
    assert r["review"]["counts"]["problem"] == 0


def test_plan_only_gives_pin_positions_without_writing(tmp_path):
    out = tmp_path / "plan.kicad_sch"
    r = sw.write(str(out), [{"ref": "R1", "symbol": "Device:R", "at": [50.8, 50.8]}], {}, libraries=LIBS, plan_only=True)
    assert not out.exists()
    assert r["parts"]["R1"]["pins"]["1"] == {"at": [50.8, 46.99], "faces": "up", "name": "~", "net": None}


def test_mistakes_are_named(tmp_path):
    out = tmp_path / "x.kicad_sch"
    r1 = [{"ref": "R1", "symbol": "Device:R"}]
    with pytest.raises(sw.WriteError, match="no part R9"):
        sw.write(str(out), r1, {"A": ["R9.1"]}, libraries=LIBS)
    with pytest.raises(sw.WriteError, match="on two nets"):
        sw.write(str(out), r1, {"A": ["R1.1"], "B": ["R1.1"]}, libraries=LIBS)
    with pytest.raises(sw.WriteError, match="has no pin '7'"):
        sw.write(str(out), r1, {"A": ["R1.7"]}, libraries=LIBS)
    with pytest.raises(sw.WriteError, match="library 'Nope' not found"):
        sw.write(str(out), [{"ref": "X1", "symbol": "Nope:X"}], {}, libraries=LIBS)
    out.write_text("x")
    with pytest.raises(sw.WriteError, match="exists"):
        sw.write(str(out), r1, {}, libraries=LIBS)


def test_writing_over_a_sheet_in_a_project_snapshots_it_first(tmp_path):
    project.init(tmp_path, tools=["kicad"])
    out = tmp_path / "kicad" / "main.kicad_sch"
    out.parent.mkdir(exist_ok=True)
    out.write_text("(kicad_sch)\n")
    r = sw.write(str(out), [{"ref": "R1", "symbol": "Device:R"}], {"A": ["R1.1", "R1.2"]}, libraries=LIBS,
                 replace=True, erc=False)
    assert r["snapshot"]["paths"] == ["kicad/main.kicad_sch"]

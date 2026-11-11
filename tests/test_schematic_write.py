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
    with pytest.raises(sw.WriteError, match="bus syntax"):
        sw.write(str(out), r1, {"D[0..3]": ["R1.1"]}, libraries=LIBS)
    with pytest.raises(sw.WriteError, match="bus syntax"):
        sw.write(str(out), r1, {"my net": ["R1.1"]}, libraries=LIBS)
    with pytest.raises(sw.WriteError, match="library 'Nope' not found"):
        sw.write(str(out), [{"ref": "X1", "symbol": "Nope:X"}], {}, libraries=LIBS)
    two = [{"ref": "R1", "symbol": "Device:R", "at": [50.8, 50.8]}, {"ref": "R2", "symbol": "Device:R", "at": [50.8, 58.42]}]
    with pytest.raises(sw.WriteError, match="same point"):  # R1.2 lands on R2.1
        sw.write(str(out), two, {"A": ["R1.2"], "B": ["R2.1"]}, libraries=LIBS)
    out.write_text("x")
    with pytest.raises(sw.WriteError, match="exists"):
        sw.write(str(out), r1, {}, libraries=LIBS)


def test_writing_over_a_sheet_in_a_project_snapshots_it_first(tmp_path):
    project.init(tmp_path, tools=["kicad"])
    out = tmp_path / "kicad" / "main.kicad_sch"
    out.parent.mkdir(exist_ok=True)
    out.write_text('(kicad_sch (version 20250114) (generator "cadmate"))\n')
    r = sw.write(str(out), [{"ref": "R1", "symbol": "Device:R"}], {"A": ["R1.1", "R1.2"]}, libraries=LIBS,
                 replace=True, erc=False)
    assert r["snapshot"]["paths"] == ["kicad/main.kicad_sch"]


def test_a_sheet_someone_drew_is_never_written_over(tmp_path):
    out = tmp_path / "theirs.kicad_sch"
    out.write_text('(kicad_sch (version 20250114) (generator "eeschema"))\n')
    with pytest.raises(sw.WriteError, match="wasn't written by cadmate"):
        sw.write(str(out), [{"ref": "R1", "symbol": "Device:R"}], {}, libraries=LIBS, replace=True)


def test_outside_a_project_the_old_sheet_is_kept_as_bak(tmp_path):
    out = tmp_path / "s.kicad_sch"
    sw.write(str(out), [{"ref": "R1", "symbol": "Device:R"}], {}, libraries=LIBS, erc=False)
    first = out.read_text()
    r = sw.write(str(out), [{"ref": "R1", "symbol": "Device:R"}], {}, libraries=LIBS, replace=True, erc=False)
    assert Path(r["backup"]).read_text() == first


def test_writing_again_gives_the_same_file(tmp_path):
    """Symbol UUIDs stay put across rewrites, so KiCad's Update PCB keeps footprints linked."""
    a, b = tmp_path / "a" / "s.kicad_sch", tmp_path / "b" / "s.kicad_sch"
    for out in (a, b):
        sw.write(str(out), PARTS, NETS, no_connect=["U1.8"], libraries=LIBS, erc=False)
    assert a.read_text() == b.read_text()
    before = a.read_text()
    sw.write(str(a), PARTS, NETS, no_connect=["U1.8"], libraries=LIBS, replace=True, erc=False)
    assert a.read_text() == before


def test_a_hidden_power_pin_can_only_go_on_its_own_net(tmp_path):
    lib = tmp_path / "H.kicad_sym"
    lib.write_text('(kicad_symbol_lib (version 20241209) (generator "test")\n'
                   ' (symbol "H" (in_bom yes) (on_board yes)\n'
                   '  (property "Reference" "U" (at 0 5.08 0) (effects (font (size 1.27 1.27))))\n'
                   '  (property "Value" "H" (at 0 -5.08 0) (effects (font (size 1.27 1.27))))\n'
                   '  (symbol "H_1_1" (rectangle (start -2.54 2.54) (end 2.54 -2.54) (stroke (width 0.254) (type default))'
                   ' (fill (type background)))\n'
                   '   (pin input line (at -5.08 0 0) (length 2.54) (name "A" (effects (font (size 1.27 1.27))))'
                   ' (number "1" (effects (font (size 1.27 1.27)))))\n'
                   '   (pin power_in line (at 0 5.08 270) (length 2.54) hide (name "VCC" (effects (font (size 1.27 1.27))))'
                   ' (number "2" (effects (font (size 1.27 1.27))))))))\n')
    parts = [{"ref": "U1", "symbol": "H:H"}]
    with pytest.raises(sw.WriteError, match="hidden power pin named VCC"):
        sw.write(str(tmp_path / "h.kicad_sch"), parts, {"+3V3": ["U1.2"]}, libraries={**LIBS, "H": str(lib)})
    r = sw.write(str(tmp_path / "h.kicad_sch"), parts, {}, libraries={**LIBS, "H": str(lib)}, erc=False)
    assert r["hidden_power_pins"] == ["U1.2 (VCC)"]


def big_symbol_lib(tmp_path) -> str:
    """A 40-pin chip: 20 pins down each side, 2.54 mm apart, the first at y 24.13."""
    rows = []
    for i in range(20):
        y = 24.13 - i * 2.54
        for side, x, ang, num in (("L", -15.24, 0, i + 1), ("R", 15.24, 180, i + 21)):
            rows.append(f'(pin bidirectional line (at {x} {y:.2f} {ang}) (length 2.54) (name "{side}{i + 1}" '
                        f'(effects (font (size 1.27 1.27)))) (number "{num}" (effects (font (size 1.27 1.27)))))')
    lib = tmp_path / "Big.kicad_sym"
    lib.write_text('(kicad_symbol_lib (version 20241209) (generator "test")\n (symbol "BIG40" (in_bom yes) (on_board yes)\n'
                   '  (property "Reference" "U" (at 0 27.94 0) (effects (font (size 1.27 1.27))))\n'
                   '  (property "Value" "BIG40" (at 0 -27.94 0) (effects (font (size 1.27 1.27))))\n'
                   '  (symbol "BIG40_1_1" (rectangle (start -12.7 26.67) (end 12.7 -26.67) (stroke (width 0.254) '
                   '(type default)) (fill (type background)))\n   ' + "\n   ".join(rows) + ")))\n")
    return str(lib)


def random_circuit(seed: int):
    """A chip with resistors and caps hung off random pins, their other ends on rails or shared nets."""
    import random
    rnd = random.Random(seed)
    chip = rnd.choice(["Big:BIG40", "Demo:MCU8"])
    big = chip == "Big:BIG40"
    parts = [{"ref": "U1", "symbol": chip, "block": "core"}]
    pool = [str(i) for i in range(1, 41 if big else 8)]
    rnd.shuffle(pool)
    nets: dict[str, list[str]] = {}
    for k in range(rnd.randint(4, 25 if big else 6)):
        if not pool:
            break
        ref = f"R{k + 1}"
        parts.append({"ref": ref, "symbol": rnd.choice(["Device:R", "Device:C"]),
                      "block": rnd.choice(["core", "core", "ext", "io"])})
        nets[f"S{k}"] = [f"U1.{pool.pop()}", f"{ref}.1"]
        nets.setdefault(rnd.choice(["GND", "+3V3", "SHARED", "SIG2"]), []).append(f"{ref}.2")
    return parts, nets, [f"U1.{p}" for p in pool] + ([] if big else ["U1.8"])


@pytest.mark.parametrize("seed", [0, 2, 6, 49, 53, 58, 82, 104])  # all but 0 and 104 once put two nets on one point
def test_random_circuits_come_out_with_the_nets_asked_for(tmp_path, seed):
    """write() rebuilds the netlist from what it drew and refuses to write a sheet that differs; these random
    circuits (the kind that once shorted two rails) must all pass that check."""
    parts, nets, nc = random_circuit(seed)
    libs = {**LIBS, "Big": big_symbol_lib(tmp_path)}
    r = sw.write(str(tmp_path / "r.kicad_sch"), parts, nets, no_connect=nc, libraries=libs, paper="A3", erc=False)
    assert r["ok"]


def test_the_self_check_sees_a_short_and_a_split():
    pin_net = {("R1", 1, "1"): "A", ("R2", 1, "1"): "A", ("R3", 1, "1"): "B"}
    assert sw._self_check([{"R1.1", "R2.1"}, {"R3.1"}], pin_net, {}) == []
    assert "also joined to ['R3.1']" in sw._self_check([{"R1.1", "R2.1", "R3.1"}], pin_net, {})[0]
    assert "2 pieces" in sw._self_check([{"R1.1"}, {"R2.1"}, {"R3.1"}], pin_net, {})[0]
    assert "net(s) ['VCC']" in sw._self_check([{"R1.1", "R2.1", "=A", "=VCC"}, {"R3.1"}], pin_net, {})[0]


def test_auto_placed_blocks_keep_clear_of_parts_placed_by_hand(tmp_path):
    parts = [{"ref": "R9", "symbol": "Device:R", "at": [38.1, 38.1]}, {"ref": "R8", "symbol": "Device:R", "at": [45.72, 38.1]},
             {"ref": "U1", "symbol": "Demo:MCU8", "block": "A"}, {"ref": "R1", "symbol": "Device:R", "block": "A"}]
    r = sw.write(str(tmp_path / "m.kicad_sch"), parts, {"P": ["R9.1", "R8.1"], "S": ["U1.3", "R1.1"]},
                 no_connect=["U1.8"], libraries=LIBS, erc=False)
    assert not [f for f in r["review"]["findings"] if f["rule"] in ("symbol_overlap", "text_overlap")], r["review"]

from cadmate import schematic_review as sr

LIB_R = '''(symbol "Device:R" (pin_numbers (hide yes)) (pin_names (offset 0))
  (property "Reference" "R" (at 2.032 0 90) (effects (font (size 1.27 1.27))))
  (property "Value" "R" (at 0 0 90) (effects (font (size 1.27 1.27))))
  (symbol "R_0_1" (rectangle (start -1.016 -2.54) (end 1.016 2.54) (stroke (width 0.254) (type default)) (fill (type none))))
  (symbol "R_1_1"
    (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))))'''

LIB_GND = '''(symbol "power:GND" (power) (pin_names (offset 0) (hide yes))
  (property "Reference" "#PWR" (at 0 -6.35 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (property "Value" "GND" (at 0 -3.81 0) (effects (font (size 1.27 1.27))))
  (symbol "GND_0_1" (polyline (pts (xy 0 0) (xy 0 -1.27) (xy 1.27 -1.27) (xy 0 -2.54) (xy -1.27 -1.27) (xy 0 -1.27))
    (stroke (width 0) (type default)) (fill (type none))))
  (symbol "GND_1_1" (pin power_in line (at 0 0 270) (length 0) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))))'''


def r(ref, x, y, rot=0, value="10k", fx=None, fy=None):
    fx = x + 2.54 if fx is None else fx
    fy = y - 1.27 if fy is None else fy
    return (f'(symbol (lib_id "Device:R") (at {x} {y} {rot}) (unit 1)'
            f' (property "Reference" "{ref}" (at {fx} {fy} 0) (effects (font (size 1.27 1.27)) (justify left)))'
            f' (property "Value" "{value}" (at {fx} {fy + 2.54} 0) (effects (font (size 1.27 1.27)) (justify left)))'
            f' (pin "1") (pin "2"))')


def gnd(x, y, rot=0):
    return (f'(symbol (lib_id "power:GND") (at {x} {y} {rot}) (unit 1)'
            f' (property "Reference" "#PWR01" (at {x} {y + 6.35} 0) (effects (font (size 1.27 1.27)) (hide yes)))'
            f' (property "Value" "GND" (at {x} {y + 3.81} 0) (effects (font (size 1.27 1.27))))'
            f' (pin "1"))')


def wire(a, b):
    return f'(wire (pts (xy {a[0]} {a[1]}) (xy {b[0]} {b[1]})) (stroke (width 0) (type default)))'


def label(name, x, y, angle=0):
    return f'(label "{name}" (at {x} {y} {angle}) (effects (font (size 1.27 1.27)) (justify left bottom)))'


def sheet(tmp_path, *items, paper="A4"):
    text = (f'(kicad_sch (version 20250114) (generator "test") (paper "{paper}")\n'
            f'(lib_symbols {LIB_R} {LIB_GND})\n' + "\n".join(items) + "\n)")
    f = tmp_path / "t.kicad_sch"
    f.write_text(text)
    return sr.review_file(str(f), erc=False)


def rules(rep, sev=None):
    return [f["rule"] for f in rep["findings"] if sev is None or f["severity"] == sev]


def test_clean_divider_has_no_findings(tmp_path):
    rep = sheet(tmp_path,
                r("R1", 50.8, 50.8), r("R2", 50.8, 63.5),
                wire((50.8, 54.61), (50.8, 59.69)),
                wire((50.8, 46.99), (50.8, 41.91)), label("VIN", 50.8, 41.91, 90),
                wire((50.8, 67.31), (50.8, 69.85)), gnd(50.8, 69.85))
    assert rep["findings"] == []
    assert rep["stats"]["two_terminal_by_label_only"] == 0
    assert rep["stats"]["pins_by_wire"] == 2


def test_overlapping_text_and_parts(tmp_path):
    rep = sheet(tmp_path, r("R1", 50.8, 50.8), r("R2", 52.07, 50.8), r("R3", 80, 50.8, fx=60, fy=50),
                r("R4", 100, 50.8, fx=60.5, fy=50))
    assert "symbol_overlap" in rules(rep)
    assert "text_overlap" in rules(rep)
    assert any("R3" in f["what"] and "R4" in f["what"] for f in rep["findings"] if f["rule"] == "text_overlap")


def test_off_sheet_and_title_block(tmp_path):
    rep = sheet(tmp_path, r("R1", 5, 50.8), r("R2", 280, 195))
    assert "off_sheet" in rules(rep, "problem")
    assert "title_block" in rules(rep)


def test_four_way_junction(tmp_path):
    c = (60.96, 60.96)
    rep = sheet(tmp_path, *(wire(c, (c[0] + dx, c[1] + dy)) for dx, dy in ((10, 0), (-10, 0), (0, 10), (0, -10))),
                f'(junction (at {c[0]} {c[1]}) (diameter 0) (color 0 0 0 0))')
    assert "four_way_junction" in rules(rep)
    # a T is fine
    rep = sheet(tmp_path, wire((50, 60.96), (70, 60.96)), wire(c, (c[0], 70)))
    assert "four_way_junction" not in rules(rep)


def test_ground_pointing_up(tmp_path):
    rep = sheet(tmp_path, gnd(50.8, 50.8, 180))
    assert "power_direction" in rules(rep)
    assert "power_direction" not in rules(sheet(tmp_path, gnd(50.8, 50.8, 0)))


def test_label_only_passives_flagged(tmp_path):
    items = []
    for i in range(4):
        x = 40 + i * 20
        items += [r(f"R{i + 1}", x, 60), wire((x, 56.19), (x, 53.65)), label(f"A{i}", x, 53.65, 90),
                  wire((x, 63.81), (x, 66.35)), label(f"B{i}", x, 66.35, 270)]
    rep = sheet(tmp_path, *items)
    f = [f for f in rep["findings"] if f["rule"] == "label_use"][0]
    assert f["severity"] == "warning"
    assert rep["stats"]["two_terminal_by_label_only"] == 4

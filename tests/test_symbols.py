from pathlib import Path

import pytest

from cadmate import server, symbol, symbol_check, symbol_generate

FIX = Path(__file__).parent / "fixtures"
LIB = FIX / "Symbols.kicad_symdir"  # symbols from KiCad's own library (kicad-symbols, KiCad 10 format)
SOIC = FIX / "SOIC-8.kicad_mod"

# Pin tables as the datasheets give them.
NE555 = [
    {"number": 1, "name": "GND", "type": "G"}, {"number": 2, "name": "TRIG", "type": "I"},
    {"number": 3, "name": "OUT", "type": "O"}, {"number": 4, "name": "RST#", "type": "I"},
    {"number": 5, "name": "CONT", "type": "passive", "side": "top"}, {"number": 6, "name": "THRES", "type": "I"},
    {"number": 7, "name": "DISCH", "type": "I"}, {"number": 8, "name": "VCC", "type": "P"},
]
NE555_FIELDS = {"footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "datasheet": "http://www.ti.com/lit/ds/symlink/ne555.pdf",
                "description": "Precision Timers, 555 compatible, SOIC-8", "keywords": "single timer 555"}
AMS1117 = [{"number": 1, "name": "GND", "type": "P"}, {"number": 2, "name": "VO", "type": "power_out"}, {"number": 3, "name": "VI", "type": "P"}]
LM358 = [
    {"number": 1, "name": "", "type": "O", "unit": 1}, {"number": 2, "name": "-", "type": "I", "unit": 1},
    {"number": 3, "name": "+", "type": "I", "unit": 1}, {"number": 5, "name": "+", "type": "I", "unit": 2},
    {"number": 6, "name": "-", "type": "I", "unit": 2}, {"number": 7, "name": "", "type": "O", "unit": 2},
    {"number": 4, "name": "V-", "type": "P", "unit": 3}, {"number": 8, "name": "V+", "type": "P", "unit": 3},
]
CH340G = [
    (1, "GND", "P", None, None), (2, "TXD", "O", "right", "uart"), (3, "RXD", "I", "right", "uart"),
    (4, "V3", "power_out", "top", None), (5, "UD+", "I/O", "left", "usb"), (6, "UD-", "I/O", "left", "usb"),
    (7, "XI", "I", "left", "xtal"), (8, "XO", "O", "left", "xtal"), (9, "CTS#", "I", "right", "modem"),
    (10, "DSR#", "I", "right", "modem"), (11, "RI#", "I", "right", "modem"), (12, "DCD#", "I", "right", "modem"),
    (13, "DTR#", "O", "right", "modem"), (14, "RTS#", "O", "right", "modem"), (15, "R232", "I", "left", None),
    (16, "VCC", "P", "top", None),
]


def build(name, pins, **kw):
    r = symbol_generate.generate(name, pins, **kw)
    assert r["ok"], r
    return r, symbol.parse_symbol(r["text"])


def rp2040_table():
    """The RP2040 datasheet's pin table, with groups as a reader would give them."""
    ref = symbol.read(LIB, "RP2040")
    rows = []
    for p in ref.pins:
        plain = p.name.replace("~{", "").replace("}", "")
        group = next((g for g, k in (("adc", "ADC"), ("gpio", "GPIO"), ("qspi", "QSPI"), ("usb", "USB_D"), ("xtal", "X"), ("swd", "SW")) if k in plain and p.etype != "power_in"), None)
        rows.append({"number": p.number, "name": plain + ("#" if p.name.startswith("~{") else ""),
                     "type": "power_in" if p.hidden else p.etype, "group": group,
                     "side": p.side if p.side in ("left", "right") and p.etype not in ("power_in", "power_out") else None})
    return ref, rows


def test_reads_kicad10_symbols_and_resolves_derived_ones():
    s = symbol.read(LIB / "NE555P.kicad_sym")
    assert s.extends == "NE555D"
    assert s.prop("Footprint") == "Package_DIP:DIP-8_W7.62mm"  # its own field
    assert len(s.pins) == 8 and s.body(1).xmax == pytest.approx(7.62)  # the parent's drawing
    rp = symbol.read(LIB / "RP2040.kicad_sym")
    assert len(rp.pins) == 57
    assert [p.number for p in rp.pins if p.name == "IOVDD" and not p.hidden] == ["1"]
    assert rp.pins[1].side == "right" and rp.pins[1].name == "GPIO0"
    assert symbol.read(LIB, "LM358").unit_count == 3


def test_ne555_matches_kicad_library():
    r, s = build("NE555D", NE555, **NE555_FIELDS)
    ref = symbol.read(LIB, "NE555D")
    cmp = symbol_check.compare_symbols(s, ref)
    assert cmp["same_connections"]
    assert cmp["differences"] == []  # same names, types and sides, pin by pin
    assert cmp["bodies"] == [[15.24, 15.24], [15.24, 15.24]]
    assert [p.name for p in s.pins if p.number == "4"] == ["~{RST}"]
    assert r["warnings"] == ["pin 4: name 'RST#' written as '~{RST}' (KiCad overbar)"]
    assert s.prop("ki_fp_filters") == "SOIC*3.9x4.9mm*P1.27mm*"  # KiCad's own filter for it
    assert all(s.properties[k].hidden for k in ("Footprint", "Datasheet", "Description"))


def test_regulator_is_laid_out_as_a_power_converter():
    r, s = build("AMS1117-3.3", AMS1117, footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
                 datasheet="http://www.advanced-monolithic.com/pdf/ds1117.pdf",
                 description="1A Low Dropout regulator, positive, 3.3V fixed output, SOT-223", keywords="linear regulator ldo fixed positive")
    assert r["warnings"] == []
    assert symbol_check.compare_symbols(s, symbol.read(LIB, "AMS1117-3.3"))["same_pins"]
    assert {p.name: p.side for p in s.pins} == {"GND": "bottom", "VO": "right", "VI": "left"}


def test_multi_unit_opamp_matches_kicad_pins():
    r, s = build("LM358", LM358, datasheet="http://www.ti.com/lit/ds/symlink/lm2904-n.pdf",
                 description="Low-Power, Dual Operational Amplifiers, DIP-8/SOIC-8/TO-99-8", keywords="dual opamp")
    cmp = symbol_check.compare_symbols(s, symbol.read(LIB, "LM358"))
    assert cmp["same_pins"] and cmp["units"] == [3, 3]
    assert s.units_locked  # the power unit differs from the amplifiers


def test_groups_and_sides_follow_the_table():
    pins = [{"number": n, "name": m, "type": t, "side": side, "group": g} for n, m, t, side, g in CH340G]
    r, s = build("CH340G", pins, footprint="Package_SO:SOIC-16_3.9x9.9mm_P1.27mm", datasheet="http://www.datasheet5.com/pdf-local-2195953",
                 description="USB serial converter, UART, SOIC-16", keywords="USB UART Serial Converter Interface")
    cmp = symbol_check.compare_symbols(s, symbol.read(LIB, "CH340G"))
    assert cmp["same_pins"], cmp
    left = sorted((p for p in s.pins if p.side == "left"), key=lambda p: -p.y)
    assert [p.name for p in left] == ["UD+", "UD-", "XI", "XO", "R232"]
    assert left[1].y - left[2].y == pytest.approx(5.08)  # an empty row between groups
    # KiCad's own CH340G has its regulator output on top, which KLC flags; so do we.
    assert any("V3" in w and "KLC S4.2" in w for w in r["warnings"])


def test_rp2040_stacks_power_pins_like_kicad():
    ref, rows = rp2040_table()
    r, s = build("RP2040", rows, footprint="Package_DFN_QFN:QFN-56-1EP_7x7mm_P0.4mm_EP3.2x3.2mm",
                 datasheet="https://datasheets.raspberrypi.com/rp2040/rp2040-datasheet.pdf",
                 description="A microcontroller by Raspberry Pi, Dual ARM Cortex-M0+, 133MHz, no flash, 264KB SRAM, 8 PIO state machines, VQFN-56",
                 keywords="RPi Pico USB QSPI I2C UART SC0914")
    cmp = symbol_check.compare_symbols(s, ref)
    assert cmp["same_connections"], cmp
    # Only the regulator output differs: KiCad keeps it on top under a KLC exception, cadmate puts power outputs right.
    assert [d["number"] for d in cmp["differences"]] == ["45"]
    assert r["report"]["units"][1]["stacked"] == {"IOVDD": ["1", "10", "22", "33", "42", "49"], "DVDD": ["23", "50"]}
    iovdd = [p for p in s.pins if p.name == "IOVDD"]
    assert [(p.number, p.etype, p.hidden) for p in iovdd][:2] == [("1", "power_in", False), ("10", "passive", True)]
    assert len({(p.x, p.y) for p in iovdd}) == 1
    assert {p.side for p in s.pins if p.etype == "power_in" and not p.hidden} == {"top", "bottom"}
    assert r["warnings"] == ["pin 56: name 'QSPI_SS#' written as '~{QSPI_SS}' (KiCad overbar)"]


@pytest.mark.parametrize("name", ["NE555D", "CH340G", "AMS1117-3.3", "RP2040", "LM358"])
def test_generated_pins_sit_on_the_grid_and_the_body_is_centred(name):
    tables = {"NE555D": NE555, "AMS1117-3.3": AMS1117, "LM358": LM358, "RP2040": rp2040_table()[1],
              "CH340G": [{"number": n, "name": m, "type": t, "side": sd, "group": g} for n, m, t, sd, g in CH340G]}
    r, s = build(name, tables[name], datasheet="https://example.com/ds.pdf", description="Test part, SOIC-8", keywords="test")
    for p in s.pins:
        assert round(p.x / 1.27) * 1.27 == pytest.approx(p.x) and round(p.y / 1.27) * 1.27 == pytest.approx(p.y)
        if p.etype != "no_connect":
            assert round(p.x / 2.54) * 2.54 == pytest.approx(p.x) and round(p.y / 2.54) * 2.54 == pytest.approx(p.y)
    for r_ in s.rects:
        assert r_.center == pytest.approx((0, 0)) and r_.width == 0.254 and r_.fill == "background"
    assert not [e for e in symbol_check.check_symbol(s)["errors"]]


def test_nc_pins_are_hidden_inside_the_body():
    pins = NE555[:7] + [{"number": 8, "name": "VCC"}, {"number": 9, "name": "NC"}, {"number": 10, "name": "NC"}]
    r, s = build("TEST", pins, **NE555_FIELDS)
    nc = [p for p in s.pins if p.etype == "no_connect"]
    assert [p.number for p in nc] == ["9", "10"] and all(p.hidden for p in nc)
    body = s.body(1)
    assert all(body.xmin < p.x < body.xmax and body.ymin < p.y < body.ymax for p in nc)
    assert len({(p.x, p.y) for p in nc}) == 2


def test_table_errors():
    assert "needs a type" in symbol_generate.generate("X", [{"number": 1, "name": "EN"}])["errors"][0]
    assert "given twice" in symbol_generate.generate("X", [{"number": 1, "name": "A", "type": "I"}, {"number": 1, "name": "B", "type": "I"}])["errors"][0]
    assert "KLC 1.7" in symbol_generate.generate("X Y", [{"number": 1, "name": "A", "type": "I"}])["errors"][0]
    assert "unknown type" in symbol_generate.generate("X", [{"number": 1, "name": "A", "type": "weird"}])["errors"][0]


def test_name_and_type_shorthand():
    assert symbol_generate.normalise_name("/RESET") == "~{RESET}"
    assert symbol_generate.normalise_name("CS#") == "~{CS}"
    assert symbol_generate.normalise_name("~{CS}") == "~{CS}"
    assert symbol_generate.normalise_name("IN+") == "IN+"
    assert symbol_generate.footprint_filters("Package_DFN_QFN:QFN-56-1EP_7x7mm_P0.4mm_EP3.2x3.2mm") == ["QFN*1EP*7x7mm*P0.4mm*EP3.2x3.2mm*"]
    assert symbol_generate.footprint_filters("Package_TO_SOT_SMD:SOT-223-3_TabPin2") == ["SOT?223*TabPin2*"]


def _mutate(text, old, new):
    assert old in text
    return symbol.parse_symbol(text.replace(old, new, 1))


def test_checker_catches_broken_symbols():
    r, _ = build("NE555D", NE555, **NE555_FIELDS)
    text = r["text"]
    off_grid = _mutate(text, "(at -10.16 5.08 0)", "(at -10.16 5.5 0)")
    assert any("grid" in e for e in symbol_check.check_symbol(off_grid)["errors"])
    wrong_way = _mutate(text, "(at -10.16 5.08 0)", "(at -5.08 5.08 0)")
    assert any("points the wrong way" in e for e in symbol_check.check_symbol(wrong_way)["errors"])
    gnd_input = _mutate(text, "(pin power_in line\n\t\t\t\t(at 0 -10.16 90)", "(pin input line\n\t\t\t\t(at 0 -10.16 90)")
    assert any("use power_in" in e for e in symbol_check.check_symbol(gnd_input)["errors"])
    shown_fp = _mutate(text, "(justify left)\n\t\t\t\thide", "(justify left)")
    assert any("Footprint field must be hidden" in e for e in symbol_check.check_symbol(shown_fp)["errors"])


def test_checker_on_kicads_own_symbols():
    # KiCad's library passes, apart from its documented KLC S4.2 exceptions...
    for name in ("NE555D", "NE555P", "LM358", "RP2040"):
        assert symbol_check.check_symbol(symbol.read(LIB, name))["errors"] == [], name
    # ...and its AP1117 drawing, which sits 62.5 mil off centre (kicad-library-utils flags it too).
    assert symbol_check.check_symbol(symbol.read(LIB, "AMS1117-3.3"))["errors"] == [
        "symbol is not centred on the origin: centre at (0.000, -1.587) mm (KLC S3.1)"]
    assert symbol_check.check_symbol(symbol.read(LIB, "NE555D"))["warnings"] == []


def test_pins_against_table_and_footprint():
    s = symbol.read(LIB, "NE555D")
    assert symbol_check.compare_with_table(s, NE555)["ok"]
    bad = [dict(p) for p in NE555]
    bad[2]["name"] = "Q"
    assert "datasheet" in symbol_check.compare_with_table(s, bad)["errors"][0]
    from cadmate import footprint
    assert symbol_check.compare_with_footprint(s, footprint.read(SOIC))["ok"]
    ch = symbol_check.compare_with_footprint(symbol.read(LIB, "CH340G"), footprint.read(SOIC))
    assert not ch["ok"] and "no pad" in ch["errors"][0]


def test_put_in_library_keeps_other_symbols_byte_for_byte():
    r1, _ = build("NE555D", NE555, **NE555_FIELDS)
    r2, _ = build("AMS1117-3.3", AMS1117, description="LDO, SOT-223", datasheet="https://example.com/a.pdf", keywords="ldo")
    lib = symbol.put_in_library(None, r1["text"], "NE555D")
    lib = symbol.put_in_library(lib, r2["text"], "AMS1117-3.3")
    assert [s.name for s in symbol.parse_library(lib)] == ["NE555D", "AMS1117-3.3"]
    with pytest.raises(FileExistsError):
        symbol.put_in_library(lib, r1["text"], "NE555D")
    replaced = symbol.put_in_library(lib, r1["text"].replace("single timer 555", "timer 555"), "NE555D", replace=True)
    assert r2["text"].rstrip() in replaced and "timer 555" in replaced
    kicad = (LIB / "CH340G.kicad_sym").read_text()
    merged = symbol.put_in_library(kicad, r1["text"], "NE555D")
    assert merged.startswith(kicad[: kicad.rindex(")")].rstrip())


def test_generate_symbol_tool_writes_and_refuses_to_overwrite(tmp_path):
    lib = tmp_path / "parts.kicad_sym"
    r = server.generate_symbol("NE555D", NE555, str(lib), footprint_path=str(SOIC), **NE555_FIELDS)
    assert r["ok"] and r["written"] == str(lib) and r["vs_footprint"]["ok"]
    assert symbol.read(lib, "NE555D").prop("Value") == "NE555D"
    again = server.generate_symbol("NE555D", NE555, str(lib), **NE555_FIELDS)
    assert not again["ok"] and "overwrite" in again["errors"][0]
    assert server.generate_symbol("NE555D", NE555, str(lib), overwrite=True, **NE555_FIELDS)["ok"]
    assert (tmp_path / "parts.kicad_sym.bak").exists()
    ch = server.generate_symbol("CH340G", [{"number": n, "name": m, "type": t} for n, m, t, *_ in CH340G], str(lib), footprint_path=str(SOIC))
    assert not ch["ok"] and "no pad" in ch["errors"][0]
    review = server.check_symbol(str(lib), "NE555D", pins=NE555, footprint_path=str(SOIC))
    assert review["ok"], review


def test_fields_clear_the_pins_of_every_unit():
    r, s = build("LM358", LM358, datasheet="https://example.com/ds.pdf", description="Dual opamp, SOIC-8", keywords="dual")
    ref, value = s.properties["Reference"], s.properties["Value"]
    top = max(p.y for p in s.pins)
    bottom = min(p.y for p in s.pins)
    assert ref.y > top and (value.y > top or value.y < bottom)


def test_opamp_units_are_drawn_like_kicads():
    pins = [
        {"number": 1, "name": "OUT1", "type": "O", "unit": 1}, {"number": 2, "name": "IN1-", "type": "I", "unit": 1},
        {"number": 3, "name": "IN1+", "type": "I", "unit": 1}, {"number": 5, "name": "IN2+", "type": "I", "unit": 2},
        {"number": 6, "name": "IN2-", "type": "I", "unit": 2}, {"number": 7, "name": "OUT2", "type": "O", "unit": 2},
        {"number": 4, "name": "V-", "type": "P", "unit": 3}, {"number": 8, "name": "V+", "type": "P", "unit": 3},
    ]
    r, s = build("LM358", pins, unit_shapes={1: "opamp", 2: "opamp"}, datasheet="http://www.ti.com/lit/ds/symlink/lm2904-n.pdf",
                 description="Low-Power, Dual Operational Amplifiers, DIP-8/SOIC-8/TO-99-8", keywords="dual opamp")
    ref = symbol.read(LIB, "LM358")
    assert [(p.number, p.name, p.x, p.y, p.rotation) for p in s.pins_of(1)] == [
        (p.number, p.name, p.x, p.y, p.rotation) for p in ref.pins_of(1)]
    assert s.polylines[0].pts == ref.polylines[0].pts and s.polylines[0].fill == "background"
    assert symbol_check.compare_symbols(s, ref)["same_pins"]
    assert symbol_check.compare_with_table(s, pins)["ok"]  # + / - / unnamed output still match the datasheet names
    assert r["summary"]["bodies"][0]["shape"] == "opamp"
    single = [{"number": 1, "name": "OUT", "type": "O"}, {"number": 2, "name": "V-", "type": "P"}, {"number": 3, "name": "+IN", "type": "I"},
              {"number": 4, "name": "-IN", "type": "I"}, {"number": 5, "name": "V+", "type": "P"}]
    r, s = build("LMV321", single, unit_shapes={1: "opamp"}, datasheet="https://example.com/a.pdf", description="Opamp, SOT-23-5", keywords="opamp")
    assert {p.name: (p.x, p.y, p.side) for p in s.pins}["V+"] == (-2.54, 7.62, "top")
    bad = symbol_generate.generate("X", single + [{"number": 6, "name": "SHDN", "type": "I"}], unit_shapes={1: "opamp"})
    assert not bad["ok"] and "rectangle" in bad["errors"][0]

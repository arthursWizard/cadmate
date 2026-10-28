from pathlib import Path

import pytest

from cadmate import apps, check, footprint, generate, model, packages, part, server, symbol
from test_generate import QFN16, RGF40, SOIC8
from test_symbols import NE555

NE555_SYMBOL = {"name": "NE555D", "pins": NE555, "description": "Precision Timers, 555 compatible, SOIC-8", "keywords": "single timer 555"}
SOIC8_FP = {"family": "soic", "params": SOIC8}
DATASHEET = "http://www.ti.com/lit/ds/symlink/ne555.pdf"
HAS_FREECAD = apps.find_freecadcmd() is not None


@pytest.fixture
def no_freecad(monkeypatch):
    monkeypatch.setattr(apps, "find_freecadcmd", lambda: None)


def footprint_of(family, params):
    r = generate.generate(family, params)
    assert r["ok"], r
    return footprint.parse(r["text"]), footprint.BBox(**r["summary"]["body"])


@pytest.mark.parametrize("family,params", [("soic", SOIC8), ("qfn", QFN16 | {"height": 0.9})])
def test_planned_dimple_is_on_pin1_and_caught_turned_round(family, params):
    fp, body = footprint_of(family, params)
    plan = model.plan(family, params, fp, body)
    marks = model.planned_marks(plan["solids"])
    assert len(marks) == 1
    assert not any("pin 1" in e for e in check.check_model(fp, plan["bbox"], marks=marks)["errors"])
    turned = [m | {"x": -m["x"], "y": -m["y"]} for m in marks]  # the same model turned 180 degrees about the origin
    r = check.check_model(fp, plan["bbox"], marks=turned)
    assert any("turned 180 degrees" in e for e in r["errors"]), r


def test_colours_are_read_from_the_step(tmp_path):
    grey, coloured = tmp_path / "grey.step", tmp_path / "coloured.step"
    grey.write_text("ISO-10303-21;\nDATA;\n#1=MANIFOLD_SOLID_BREP('',#2);\nENDSEC;\n")
    coloured.write_text("ISO-10303-21;\nDATA;\n#1=COLOUR_RGB('',0.15,0.15,0.15);\nENDSEC;\n")
    assert not model.has_colours(grey) and model.has_colours(coloured)
    assert not model.has_colours(tmp_path / "missing.step")


def test_soic_model_puts_a_lead_on_every_pad():
    fp, body = footprint_of("soic", SOIC8)
    plan = model.plan("soic", SOIC8, fp, body)
    leads = {s["name"]: s for s in plan["solids"] if s["name"].startswith("Lead_")}
    assert len(leads) == 8
    for p in fp.pads:
        prism = leads[f"Lead_{p.number}"]["prism"]
        foot_x = [x for x, z in prism["pts"] if z == 0]
        # The flat foot lies on the pad, which is 3D y = -footprint y.
        assert min(foot_x) < p.x < max(foot_x) or p.box.xmin <= min(foot_x) <= p.box.xmax
        assert prism["y0"] == pytest.approx(-p.y - 0.41 / 2)
    assert plan["bbox"] == {"xmin": -3.0, "xmax": 3.0, "ymin": -2.45, "ymax": 2.45, "zmin": 0, "zmax": 1.75}
    assert plan["expected"] == {"width": 6.0, "length": 4.9, "height": 1.75}


def test_dimple_marks_pin_1():
    fp, body = footprint_of("soic", SOIC8)
    dimple = model.plan("soic", SOIC8, fp, body)["solids"][0]["cut"][0]["cyl"]
    # Pad 1 is top left in the footprint (x < 0, y < 0), so top left in 3D is x < 0, y > 0.
    assert dimple[0] < 0 and dimple[1] > 0
    assert dimple[2] == pytest.approx(1.75 - model.DIMPLE_DEPTH)


def test_qfn_terminals_are_cut_from_the_body():
    params = QFN16 | {"height": 0.9}
    fp, body = footprint_of("qfn", params)
    plan = model.plan("qfn", params, fp, body)
    names = [s["name"] for s in plan["solids"]]
    assert names.count("Body") == 1 and "EP_17" in names and len(names) == 18
    assert len(plan["solids"][0]["cut"]) == 17 + 1  # terminals, exposed pad and the dimple
    assert plan["bbox"]["zmin"] == 0 and plan["bbox"]["zmax"] == 0.9
    assert (plan["bbox"]["xmax"] - plan["bbox"]["xmin"]) == pytest.approx(3.0)


def test_model_needs_the_height():
    fp, body = footprint_of("qfn", QFN16)
    with pytest.raises(ValueError, match="height"):
        model.plan("qfn", QFN16, fp, body)


def test_package_params_take_the_footprint_values_as_they_are():
    # One parameter set for footprint and model: toleranced values used to be refused here.
    r = packages.check_params("soic", SOIC8)
    assert r["ok"], r
    assert r["params"]["lead_length"] == pytest.approx(0.835)
    q = packages.check_params("qfn", {k: v for k, v in QFN16.items() if k != "pins"} | {"pins_x": 4, "pins_y": 4, "height": 0.9})
    assert q["ok"], q
    assert q["params"]["pins"] == 16


def test_part_is_linked_and_registered(tmp_path, no_freecad):
    r = part.generate_part(tmp_path, "my_parts", NE555_SYMBOL, SOIC8_FP, datasheet=DATASHEET)
    assert r["ok"], r
    sym = symbol.read(tmp_path / "my_parts.kicad_sym", "NE555D")
    assert sym.prop("Footprint") == "my_parts:SOIC-8_3.9x4.9mm_P1.27mm"
    assert sym.prop("Datasheet") == DATASHEET
    assert (tmp_path / "my_parts.pretty" / "SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod").exists()
    assert '"${KIPRJMOD}/my_parts.pretty"' in (tmp_path / "fp-lib-table").read_text()
    assert '"${KIPRJMOD}/my_parts.kicad_sym"' in (tmp_path / "sym-lib-table").read_text()
    # Without FreeCAD there is no model, and nothing links to a file that is not there.
    assert r["model"]["source"] == "not built"
    assert not footprint.read(tmp_path / "my_parts.pretty" / "SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod").models
    check = part.check_part(tmp_path / "my_parts.kicad_sym", "NE555D")
    assert check["ok"], check
    assert any("links no 3D model" in w for w in check["warnings"])


def test_pin_without_pad_writes_nothing(tmp_path, no_freecad):
    pins = NE555 + [{"number": 9, "name": "EXTRA", "type": "I"}]
    r = part.generate_part(tmp_path, "my_parts", NE555_SYMBOL | {"pins": pins}, SOIC8_FP, datasheet=DATASHEET)
    assert not r["ok"]
    assert any("no pad" in e for e in r["errors"])
    assert not any(tmp_path.iterdir())


def test_existing_files_are_kept_unless_asked(tmp_path, no_freecad):
    assert part.generate_part(tmp_path, "my_parts", NE555_SYMBOL, SOIC8_FP, datasheet=DATASHEET)["ok"]
    again = part.generate_part(tmp_path, "my_parts", NE555_SYMBOL, SOIC8_FP, datasheet=DATASHEET)
    assert not again["ok"] and any("overwrite" in e for e in again["errors"])
    assert part.generate_part(tmp_path, "my_parts", NE555_SYMBOL, SOIC8_FP, datasheet=DATASHEET, overwrite=True)["ok"]
    assert (tmp_path / "my_parts.kicad_sym.bak").exists()
    # The library tables keep one entry each.
    assert (tmp_path / "fp-lib-table").read_text().count("(lib ") == 1


def test_lib_tables_keep_other_entries(tmp_path):
    table = '(fp_lib_table\n  (version 7)\n  (lib (name "Other")(type "KiCad")(uri "/x/Other.pretty")(options "")(descr ""))\n)\n'
    (tmp_path / "fp-lib-table").write_text(table)
    part.add_to_lib_tables(tmp_path, "my_parts")
    text = (tmp_path / "fp-lib-table").read_text()
    assert text.startswith(table[:-3]) and '(name "my_parts")' in text


def test_check_part_finds_broken_links(tmp_path, no_freecad):
    assert part.generate_part(tmp_path, "my_parts", NE555_SYMBOL, SOIC8_FP, datasheet=DATASHEET)["ok"]
    fp_file = tmp_path / "my_parts.pretty" / "SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod"
    server.link_model_to_footprint(str(fp_file), "${KIPRJMOD}/my_parts.3dshapes/missing.step")
    r = part.check_part(tmp_path / "my_parts.kicad_sym", "NE555D")
    assert any("missing.step not found" in e for e in r["errors"])
    # A symbol whose Footprint field names a footprint that is not there.
    lib = tmp_path / "my_parts.kicad_sym"
    lib.write_text(lib.read_text().replace("my_parts:SOIC-8_3.9x4.9mm_P1.27mm", "my_parts:SOIC-8_wrong"))
    r = part.check_part(lib, "NE555D")
    assert not r["ok"] and any("not found" in e for e in r["errors"])


def test_check_part_flags_filters_that_hide_the_footprint(tmp_path, no_freecad):
    spec = NE555_SYMBOL | {"fp_filters": ["DIP*W7.62mm*"]}
    assert part.generate_part(tmp_path, "my_parts", spec, SOIC8_FP, datasheet=DATASHEET)["ok"]
    r = part.check_part(tmp_path / "my_parts.kicad_sym", "NE555D")
    assert any("filters" in w and "hides" in w for w in r["warnings"])


def test_given_model_follows_the_footprint_origin(tmp_path, no_freecad):
    # A through-hole part drawn with pin 1 at (5, 5): the footprint moves pin 1 to the origin,
    # and a model built in the datasheet's coordinates moves with it.
    params = {"body_width": 10, "body_length": 4, "pad_rows": [{"first": 1, "count": 3, "x": 5, "y": 5, "dx": 2.54, "lead_diameter": [0.5, 0.6]}]}
    pins = [{"number": n, "name": f"P{n}", "type": "passive"} for n in (1, 2, 3)]
    r = part.generate_part(tmp_path, "my_parts", {"name": "Thing", "pins": pins, "reference": "J", "description": "test"},
                           {"family": "custom", "params": params, "name": "Thing_1x03"},
                           {"path": "${KIPRJMOD}/Thing.step", "frame": "datasheet"}, datasheet=DATASHEET)
    assert r["ok"], r
    fp = footprint.read(tmp_path / "my_parts.pretty" / "Thing_1x03.kicad_mod")
    assert fp.models[0].offset == (-5.0, 5.0, 0.0)


@pytest.mark.skipif(not HAS_FREECAD, reason="needs FreeCAD")
def test_whole_part_with_model(tmp_path):
    r = part.generate_part(tmp_path, "my_parts", NE555_SYMBOL, SOIC8_FP, datasheet=DATASHEET)
    assert r["ok"], r
    step = tmp_path / "my_parts.3dshapes" / "SOIC-8_3.9x4.9mm_P1.27mm.step"
    assert step.exists()
    assert r["model"]["check"]["solids"] == 9
    check = part.check_part(tmp_path / "my_parts.kicad_sym", "NE555D", expected={"width": 6.0, "length": 4.9, "height": 1.75})
    assert check["ok"] and not check["warnings"], check
    assert check["model_check"]["center_offset"] == [0.0, 0.0] or check["model_check"]["center_offset"] == (0.0, 0.0)


DIP_LEAD = {"lead_width": [0.36, 0.56], "lead_thickness": [0.20, 0.36]}
DIP8 = {"body_width": 6.35, "body_length": 9.81,
        "pad_rows": [{"first": 1, "count": 4, "x": 0, "y": 0, "dy": 2.54} | DIP_LEAD,
                     {"first": 5, "count": 4, "x": 7.62, "y": 7.62, "dy": -2.54} | DIP_LEAD]}
DIP_MODEL = {"height": 5.33, "standoff": 0.38, "lead_below": 3.3, "lead_width": 0.46, "lead_thickness": 0.28}


def test_dip_part_matches_kicads_ne555p(tmp_path, no_freecad):
    sym = NE555_SYMBOL | {"name": "NE555P", "description": "Precision Timers, 555 compatible, PDIP-8"}
    r = part.generate_part(tmp_path, "my_parts", sym, {"family": "custom", "name": "DIP-8_W7.62mm", "params": DIP8}, DIP_MODEL, datasheet=DATASHEET)
    assert r["ok"], r
    fp = footprint.read(tmp_path / "my_parts.pretty" / "DIP-8_W7.62mm.kicad_mod")
    # KiCad's DIP-8_W7.62mm: pin 1 at the origin, rows 7.62 mm apart, counted down then up, pin 1 square.
    assert [(p.number, p.x, p.y) for p in fp.pads] == [("1", 0, 0), ("2", 0, 2.54), ("3", 0, 5.08), ("4", 0, 7.62),
                                                       ("5", 7.62, 7.62), ("6", 7.62, 5.08), ("7", 7.62, 2.54), ("8", 7.62, 0)]
    assert fp.pads[0].shape == "rect" and {p.shape for p in fp.pads[1:]} == {"circle"}
    assert fp.pads[0].drill == 0.9  # the lead's diagonal + 0.2 (IPC-7251 nominal); KiCad's library uses 0.8
    from cadmate import symbol_check
    kicad = symbol.read(Path(__file__).parent / "fixtures" / "Symbols.kicad_symdir", "NE555P")
    assert symbol_check.compare_symbols(symbol.read(tmp_path / "my_parts.kicad_sym", "NE555P"), kicad)["same_connections"]


def test_dip_model_has_shoulders_into_the_body():
    r = generate.generate("custom", DIP8)
    fp, body = footprint.parse(r["text"]), footprint.BBox(**r["summary"]["body"])
    plan = model.plan("custom", DIP8 | DIP_MODEL, fp, body)
    leads = [s for s in plan["solids"] if s["name"].startswith("Lead_")]
    shoulders = [s for s in plan["solids"] if s["name"].startswith("Shoulder_")]
    assert len(leads) == len(shoulders) == 8
    for s in shoulders:  # each runs from its lead to the body's side
        x, _, _, dx, _, _ = s["box"]
        assert x == pytest.approx(body.xmax) or x + dx == pytest.approx(body.xmin)
    assert plan["bbox"]["zmin"] == -3.3 and plan["bbox"]["zmax"] == 5.33
    assert check_model_ok(fp, plan)


def test_header_posts_stand_above_the_body():
    params = {"body_width": 2.54, "body_length": 10.16,
              "pad_rows": [{"first": 1, "count": 4, "x": 0, "y": 0, "dy": 2.54, "lead_width": 0.64, "lead_thickness": 0.64}]}
    r = generate.generate("custom", params)
    fp, body = footprint.parse(r["text"]), footprint.BBox(**r["summary"]["body"])
    assert fp.pads[0].drill == 1.15
    plan = model.plan("custom", params | {"height": 2.5, "lead_above": 6, "lead_width": 0.64, "lead_thickness": 0.64}, fp, body)
    assert plan["bbox"]["zmax"] == pytest.approx(8.5) and plan["expected"]["height"] == pytest.approx(8.5)
    assert not [s for s in plan["solids"] if s["name"].startswith("Shoulder_")]
    assert check_model_ok(fp, plan)


def check_model_ok(fp, plan):
    from cadmate import check
    r = check.check_model(fp, plan["bbox"], footprint.Model(path=""), plan["expected"])
    assert r["ok"] and not r["warnings"], r
    return True


def test_smd_module_with_pads_on_three_sides():
    # Castellated module soldered flat: pads on the left, right and bottom only, parts on its underside.
    pads = [{"number": i + 1, "x": 9.5, "y": -6 + 2.54 * i, "width": 2.6, "height": 1.6} for i in range(5)]
    pads += [{"number": 6 + i, "x": 2.54 - 2.54 * i, "y": 6.5, "width": 1.6, "height": 2.6} for i in range(3)]
    pads += [{"number": 9 + i, "x": -9.5, "y": 6 - 2.54 * i, "width": 2.6, "height": 1.6} for i in range(5)]
    r = generate.generate("custom", {"body_width": 18, "body_length": 16, "body_center": [0, 0], "pads": pads, "origin": "as_given"})
    fp = footprint.parse(r["text"])
    module = {"xmin": -9, "xmax": 9, "ymin": -8, "ymax": 8, "zmin": -1.1, "zmax": 4}
    from cadmate import check
    flat = check.check_model(fp, module)
    assert not any("centre" in e for e in flat["errors"])  # the pads' centre is not the module's
    assert any("below the board" in e for e in flat["errors"])
    assert check.check_model(fp, module, cutout=True)["ok"]
    off = check.check_model(fp, module | {"xmin": -4, "xmax": 14}, cutout=True)
    assert any("pads lie" in w for w in off["warnings"])


def test_custom_no_lead_terminals_stop_at_the_body():
    # TI's land pattern reaches 0.2 mm past a QFN's body; the terminals in the model must not.
    r = generate.generate("custom", RGF40)
    fp = footprint.parse(r["text"])
    plan = model.plan("custom", RGF40 | {"height": 1.0}, fp, footprint.BBox(-2.5, -3.5, 2.5, 3.5))
    assert plan["expected"] == {"width": 5.0, "length": 7.0, "height": 1.0}

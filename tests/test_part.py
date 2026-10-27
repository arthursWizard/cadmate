from pathlib import Path

import pytest

from cadmate import apps, footprint, generate, model, packages, part, server, symbol
from test_generate import QFN16, SOIC8
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
    assert plan["expected"] == {"length": 6.0, "width": 4.9, "height": 1.75}


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
    check = part.check_part(tmp_path / "my_parts.kicad_sym", "NE555D", expected={"length": 6.0, "width": 4.9, "height": 1.75})
    assert check["ok"] and not check["warnings"], check
    assert check["model_check"]["center_offset"] == [0.0, 0.0] or check["model_check"]["center_offset"] == (0.0, 0.0)

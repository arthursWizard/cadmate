from pathlib import Path

import pytest

from cadmate import footprint, sexpr

FIX = Path(__file__).parent / "fixtures"
SOIC = FIX / "SOIC-8.kicad_mod"
CAP = FIX / "CP_Radial_D5.0mm_P2.00mm.kicad_mod"


def test_reads_kicad9_soic():
    fp = footprint.read(SOIC)
    assert fp.name == "SOIC-8_3.9x4.9mm_P1.27mm"
    assert fp.format_version == "20241229"
    assert fp.smd
    assert len(fp.signal_pads()) == 8
    assert fp.pitch() == pytest.approx(1.27)
    assert fp.pin1.x == pytest.approx(-2.475)
    assert fp.courtyard.width == pytest.approx(7.4)
    assert fp.models == []


def test_reads_kicad5_module_with_inch_model_offset():
    fp = footprint.read(CAP)
    assert not fp.smd
    assert fp.pitch() == pytest.approx(2.0)
    assert fp.courtyard.xmin == pytest.approx(-1.75)
    assert fp.models[0].path.endswith("CP_Radial_D5.0mm_P2.00mm.wrl")


def test_link_model_appends_and_keeps_rest_of_file():
    text = SOIC.read_text()
    model = footprint.Model(path="${KIPRJMOD}/3d/SOIC-8.step", offset=(0, 0, 0.1))
    out = footprint.link_model(text, model)
    assert out.startswith(text[: text.rindex("(embedded_fonts no)")])
    fp = footprint.parse(out)
    assert len(fp.models) == 1
    assert fp.models[0].offset == (0.0, 0.0, 0.1)


def test_link_model_replaces_existing_models():
    text = CAP.read_text()
    out = footprint.link_model(text, footprint.Model(path="new.step"))
    out = footprint.link_model(out, footprint.Model(path="newer.step", rotate=(0, 0, 90)), replace=False)
    out = footprint.link_model(out, footprint.Model(path="final.step"))
    fp = footprint.parse(out)
    assert [m.path for m in fp.models] == ["final.step"]
    assert "(pad 2 thru_hole circle" in out


def test_parser_handles_escaped_strings():
    node = sexpr.parse(r'(a "x \"y\" z" (b 1))')
    assert node.atoms() == ['x "y" z']
    assert node.child("b").floats() == [1.0]


def test_rejects_non_footprint():
    with pytest.raises(ValueError):
        footprint.parse('(kicad_sch (version 1))')

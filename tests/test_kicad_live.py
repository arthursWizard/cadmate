import shutil
from pathlib import Path

import pytest

from cadmate import kicad_live, live, symbol
from conftest import short_tmp

FIX = Path(__file__).parent / "fixtures"
SYMS = FIX / "Symbols.kicad_symdir"


@pytest.fixture(autouse=True)
def data(monkeypatch):
    d = Path(short_tmp())
    monkeypatch.setenv(live.SOCKET_ENV, str(d / "b.sock"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fp(tmp_path):
    lib = tmp_path / "Test.pretty"
    lib.mkdir()
    path = lib / "SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod"
    shutil.copy(FIX / "SOIC-8.kicad_mod", path)
    return path


@pytest.fixture
def lib(tmp_path):
    text = None
    for name in ("NE555D", "CH340G"):
        block = (SYMS / f"{name}.kicad_sym").read_text()
        node = kicad_live.sexpr.parse(block).children("symbol")[0]
        text = symbol.put_in_library(text, "\t" + block[node.start:node.end], name)
    path = tmp_path / "parts.kicad_sym"
    path.write_text(text)
    return path


def hand_edit(path, old, new):
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


def test_first_look_then_user_changes_pad_by_pad(fp):
    s = kicad_live.state(str(fp))
    assert s["ok"] and s["kind"] == "footprint" and s["user_changes"] == {"first_look": True}
    assert s["summary"]["signal_pad_count"] == 8 and "(pad \"1\"" in s["text"]
    hand_edit(fp, '(pad "3" smd roundrect (at -2.475 0.635) (size 1.95 0.6)', '(pad "3" smd roundrect (at -2.5 0.635) (size 2.1 0.6)')
    s = kicad_live.state(str(fp))
    ch = s["user_changes"]
    assert ch["changed"] and ch["items"] == [{"pad": "3", "changed": {"width": [1.95, 2.1], "x": [-2.475, -2.5]}}]
    assert "-2.5 0.635" in ch["raw_diff"]
    assert kicad_live.state(str(fp))["user_changes"] == {"changed": False, "items": []}  # seen now


def test_edit_builds_on_the_users_save_and_reports_it(fp):
    kicad_live.state(str(fp))
    hand_edit(fp, '(pad "8" smd roundrect (at 2.475 -1.905)', '(pad "8" smd roundrect (at 2.5 -1.905)')
    r = kicad_live.edit(str(fp), [{"old": "(size 1.95 0.6)", "new": "(size 2 0.6)", "all": True}], "pads 0.05 mm longer")
    assert r["ok"], r
    assert r["user_changes"]["items"] == [{"pad": "8", "changed": {"x": [2.475, 2.5]}}]
    assert len(r["changed"]) == 8 and r["step"] == 1
    text = fp.read_text()
    assert "(at 2.5 -1.905)" in text and text.count("(size 2 0.6)") == 8  # the user's edit stays
    assert kicad_live.state(str(fp))["user_changes"]["changed"] is False  # cadmate's own write isn't a user change


def test_edit_refusals_write_nothing(fp):
    before = fp.read_text()
    assert "occurs 8 times" in kicad_live.edit(str(fp), [{"old": "(size 1.95 0.6)", "new": "(size 2 0.6)"}], "x")["error"]
    assert "not found" in kicad_live.edit(str(fp), [{"old": "(size 9 9)", "new": "x"}], "x")["error"]
    assert "rename" in kicad_live.edit(str(fp), [{"old": '(footprint "SOIC-8_3.9x4.9mm_P1.27mm"', "new": '(footprint "Other"'}], "x")["error"]
    assert not kicad_live.edit(str(fp), [{"old": '(pad "1" smd', "new": '(pad "1" (smd'}], "x")["ok"]  # unbalanced
    with pytest.raises(kicad_live.LiveError):
        kicad_live.edit(str(fp), [{"old": "a", "new": "b"}], " ")
    assert fp.read_text() == before


def test_edit_reports_new_check_errors(fp):
    r = kicad_live.edit(str(fp), [{"old": '(pad "2" smd roundrect (at -2.475 -0.635)', "new": '(pad "2" smd roundrect (at -2.475 -1.6)'}],
                        "move pad 2 into pad 1")
    assert r["ok"] and not r["check"]["ok"] and r["check"]["new_errors"]


def test_undo_keeps_later_user_edits(fp):
    kicad_live.state(str(fp))
    original = fp.read_text()
    kicad_live.edit(str(fp), [{"old": '(pad "1" smd roundrect (at -2.475 -1.905) (size 1.95 0.6)',
                               "new": '(pad "1" smd roundrect (at -2.475 -1.905) (size 2.2 0.6)'}], "pad 1 longer")
    r = kicad_live.undo(str(fp))
    assert r["ok"] and r["undid"] == "pad 1 longer" and fp.read_text() == original
    assert not kicad_live.undo(str(fp))["ok"]

    kicad_live.edit(str(fp), [{"old": '(pad "1" smd roundrect (at -2.475 -1.905) (size 1.95 0.6)',
                               "new": '(pad "1" smd roundrect (at -2.475 -1.905) (size 2.2 0.6)'}], "pad 1 longer")
    hand_edit(fp, '(pad "5" smd roundrect (at 2.475 1.905)', '(pad "5" smd roundrect (at 2.6 1.905)')
    r = kicad_live.undo(str(fp))
    assert r["ok"] and r["user_changes"]["items"] == [{"pad": "5", "changed": {"x": [2.475, 2.6]}}]
    text = fp.read_text()
    assert "(size 2.2 0.6)" not in text and "(at 2.6 1.905)" in text


def test_undo_refused_when_the_user_changed_the_same_text(fp):
    kicad_live.edit(str(fp), [{"old": '(at -2.475 -1.905) (size 1.95 0.6)', "new": '(at -2.475 -1.905) (size 2.2 0.6)'}], "pad 1 longer")
    hand_edit(fp, '(at -2.475 -1.905) (size 2.2 0.6)', '(at -2.475 -1.905) (size 2.3 0.6)')
    r = kicad_live.undo(str(fp))
    assert not r["ok"] and "reverse change" in r["error"]


def test_save_over_cadmate_step_is_reported(fp):
    kicad_live.state(str(fp))
    original = fp.read_text()
    kicad_live.edit(str(fp), [{"old": '(at -2.475 -1.905) (size 1.95 0.6)', "new": '(at -2.475 -1.905) (size 2.2 0.6)'}], "pad 1 longer")
    fp.write_text(original.replace('(pad "4" smd roundrect (at -2.475 1.905)', '(pad "4" smd roundrect (at -2.4 1.905)'))
    s = kicad_live.state(str(fp))
    assert s["user_changes"]["overwrote_cadmate_step"] == "pad 1 longer"


def test_stale_save_after_undo_is_reported(fp):
    # What happened in KiCad 10 on the Mac: cadmate's undo, then a save from the editor's stale copy that still had the
    # step, with a pad dragged and the whole file reformatted.
    kicad_live.state(str(fp))
    kicad_live.edit(str(fp), [{"old": '(at -2.475 -1.905) (size 1.95 0.6)', "new": '(at -2.475 -1.905) (size 2.2 0.6)'}], "pad 1 longer")
    stale = fp.read_text()
    kicad_live.undo(str(fp))
    fp.write_text(stale.replace('(pad "4" smd roundrect (at -2.475 1.905)', '(pad "4" smd roundrect (at -2.4 1.905)').replace(" (", "\n  ("))
    u = kicad_live.state(str(fp))["user_changes"]
    assert u["overwrote_cadmate_step"] == "undo 'pad 1 longer'"
    assert u["reverted"] == [{"pad": "1", "changed": {"width": [2.2, 1.95]}}]


def test_ordinary_user_edit_is_not_an_overwrite(fp):
    kicad_live.state(str(fp))
    kicad_live.edit(str(fp), [{"old": '(at -2.475 -1.905) (size 1.95 0.6)', "new": '(at -2.475 -1.905) (size 2.2 0.6)'}], "pad 1 longer")
    hand_edit(fp, '(at -2.475 -1.905) (size 2.2 0.6)', '(at -2.475 -1.905) (size 2.3 0.6)')
    u = kicad_live.state(str(fp))["user_changes"]
    assert u["changed"] and "overwrote_cadmate_step" not in u


def test_history_survives_a_new_session(fp):
    kicad_live.edit(str(fp), [{"old": '(at -2.475 -1.905) (size 1.95 0.6)', "new": '(at -2.475 -1.905) (size 2.2 0.6)'}], "pad 1 longer")
    s = kicad_live.state(str(fp))
    assert [h["label"] for h in s["history"]] == ["pad 1 longer"]


def test_symbol_in_a_library_other_symbols_untouched(lib):
    with pytest.raises(kicad_live.LiveError, match="give the symbol name"):
        kicad_live.state(str(lib))
    s = kicad_live.state(str(lib), "NE555D")
    assert s["kind"] == "symbol" and s["text"].startswith('(symbol "NE555D"') and "CH340G" not in s["text"]
    ch340 = kicad_live.Target(lib, "CH340G").item(lib.read_text())

    hand_edit(lib, '(name "GND"', '(name "VSS"')
    r = kicad_live.edit(str(lib), [{"old": "(at 0 -10.16 90)\n\t\t\t\t(length 2.54)", "new": "(at 0 -12.7 90)\n\t\t\t\t(length 5.08)"}],
                        "GND pin longer", name="NE555D")
    assert r["ok"], r
    assert r["user_changes"]["items"] == [{"pin": "1", "unit": 0, "changed": {"name": ["GND", "VSS"]}}]
    assert r["changed"] == [{"pin": "1", "unit": 0, "changed": {"length": [2.54, 5.08], "y": [-10.16, -12.7]}}]
    assert kicad_live.Target(lib, "CH340G").item(lib.read_text()) == ch340
    pin = [p for p in symbol.read(lib, "NE555D").pins if p.number == "1"][0]
    assert (pin.name, pin.length) == ("VSS", 5.08)


def test_symbol_field_change_is_reported(lib):
    kicad_live.state(str(lib), "NE555D")
    hand_edit(lib, '(property "Value" "NE555D"', '(property "Value" "NE555DR"')
    items = kicad_live.state(str(lib), "NE555D")["user_changes"]["items"]
    assert [i["field"] for i in items] == ["Value"] and items[0]["now"]["value"] == "NE555DR"


def test_symbol_rename_refused(lib):
    r = kicad_live.edit(str(lib), [{"old": '(symbol "NE555D"', "new": '(symbol "NE555X"'}], "rename", name="NE555D")
    assert not r["ok"] and "rename" in r["error"]


def test_svg_gets_a_pixel_size(tmp_path):
    svg = tmp_path / "a.svg"
    original = ('<?xml version="1.0"?>\n<svg\n  xmlns="http://www.w3.org/2000/svg"\n  version="1.1"\n'
                '  width="20mm" height="10mm" viewBox="0 0 20 10">\n<title>x</title></svg>')
    svg.write_text(original)
    kicad_live._pixel_size(svg, 900, margin=0.05)
    text = svg.read_text()
    assert '<svg width="900" height="491" viewBox="-1.0000 -1.0000 22.0000 12.0000"' in text and "mm" not in text
    svg.write_text(original)
    kicad_live._pixel_size(svg, 900, square=True, margin=0.05)
    assert '<svg width="900" height="900" viewBox="-1.0000 -6.0000 22.0000 22.0000"' in svg.read_text()


def test_empty_fields_kicad_adds_on_save_are_not_user_changes(fp):
    kicad_live.state(str(fp))
    hand_edit(fp, '\t(attr smd)', '\t(property "Datasheet" "" (at 0 0 0) (layer "F.Fab"))\n\t(property "Description" "" (at 0 0 0) (layer "F.Fab"))\n\t(attr smd)')
    ch = kicad_live.state(str(fp))["user_changes"]
    assert ch["changed"] and ch["items"] == [] and "note" in ch


def test_edit_reply_lists_errors_that_were_already_there(fp):
    kicad_live.state(str(fp))
    hand_edit(fp, '(pad "1" smd roundrect (at -2.475 -1.905)', '(pad "1" smd roundrect (at -2.9 -1.905)')  # past the courtyard
    r = kicad_live.edit(str(fp), [{"old": '(at -2.9 -1.905) (size 1.95 0.6)', "new": '(at -2.9 -1.905) (size 2.15 0.6)'}], "pad 1 longer")
    assert r["ok"] and not r["check"]["new_errors"]
    assert any("outside the courtyard" in e for e in r["check"]["errors"])


def test_edit_says_how_to_see_it_in_kicad(fp):
    r = kicad_live.edit(str(fp), [{"old": '(at -2.475 -1.905) (size 1.95 0.6)', "new": '(at -2.475 -1.905) (size 2.2 0.6)'}], "pad 1 longer")
    assert r["ok"] and "Refresh" in r["kicad"] and "double-click" in r["kicad"]


def test_macos_symbol_write_moves_the_file_out_and_back(lib, monkeypatch):
    monkeypatch.setattr(kicad_live.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(kicad_live, "MAC_SETTLE", 0)
    seen = []
    real = kicad_live.os.replace
    monkeypatch.setattr(kicad_live.os, "replace", lambda a, b: (seen.append((Path(a).name, Path(b).name)), real(a, b)))
    r = kicad_live.edit(str(lib), [{"old": "(at 0 -10.16 90)\n\t\t\t\t(length 2.54)", "new": "(at 0 -12.7 90)\n\t\t\t\t(length 5.08)"}],
                        "GND pin longer", name="NE555D")
    assert r["ok"] and "reloads" in r["kicad"]
    assert seen == [(lib.name, f".{lib.name}.cadmate-old"), (f".{lib.name}.cadmate-new", lib.name)]
    assert sorted(p.name for p in lib.parent.iterdir()) == [lib.name]


def test_macos_footprint_write_stays_in_place(fp, monkeypatch):
    monkeypatch.setattr(kicad_live.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(kicad_live.os, "replace", lambda a, b: pytest.fail("footprints are written in place"))
    assert kicad_live.edit(str(fp), [{"old": '(at -2.475 -1.905) (size 1.95 0.6)', "new": '(at -2.475 -1.905) (size 2.2 0.6)'}], "pad 1")["ok"]

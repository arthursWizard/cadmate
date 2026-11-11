from pathlib import Path

import pytest

from cadmate import pcb_board, pcb_write, sexpr
from cadmate.pcb_write import Comp

FIX = Path(__file__).parent / "fixtures"
FP = {"Test": str(FIX / "pcb" / "Test.pretty"), "Fix": str(FIX)}

COMPS = [Comp("U1", "MCU8", "Fix:SOIC-8", "/u1", sch_at=(50.8, 50.8)),
         Comp("C1", "100n", "Test:C_0603", "/c1", sch_at=(73.66, 45.72)),
         Comp("R1", "10k", "Test:R_0603", "/r1", sch_at=(30.48, 38.1)),
         Comp("R2", "1k", "Test:R_0603", "/r2", sch_at=(106.68, 35.56)),
         Comp("R3", "1k", "Test:R_0603", "/r3", sch_at=(106.68, 50.8))]
NETS = {"+3V3": ["U1.1", "C1.1", "R1.1"], "GND": ["U1.2", "C1.2", "R3.2"], "/RST": ["U1.7", "R1.2"],
        "/LED": ["U1.5", "R2.1"], "/MID": ["R2.2", "R3.1"]}


def board(path) -> pcb_board.Board:
    return pcb_board.parse(Path(path).read_text(encoding="utf-8"))


def test_every_pad_is_on_its_net(tmp_path):
    out = tmp_path / "demo.kicad_pcb"
    r = pcb_write.write(str(out), COMPS, NETS, libraries=FP, pin_info={"U1.1": ("VDD", "power_in")})
    assert r["ok"] and r["footprints"] == 5
    b = board(out)
    got = {f"{p.ref}.{pad.number}": pad.net for p in b.parts for pad in p.pads}
    for net, pins in NETS.items():
        for pin in pins:
            assert got[pin] == net, pin
    assert got["U1.3"] is None  # a pin on no net stays off every net
    vdd = next(pad for p in b.parts if p.ref == "U1" for pad in p.pads if pad.number == "1")
    assert (vdd.pinfunction, vdd.pintype) == ("VDD", "power_in")  # so the critical-net checks see a power pin


def test_footprints_keep_the_link_to_their_symbols_and_have_their_own_uuids(tmp_path):
    out = tmp_path / "demo.kicad_pcb"
    pcb_write.write(str(out), COMPS, NETS, libraries=FP)
    text = out.read_text()
    root = sexpr.parse(text)
    fps = root.children("footprint")
    assert [f.child("path").atoms()[0] for f in fps if f.child("path") is not None] == ["/u1", "/c1", "/r1", "/r2", "/r3"]
    import re
    uuids = re.findall(r'\(uuid "([0-9a-f-]+)"\)', text)
    assert len(uuids) == len(set(uuids))  # the three 0603s don't share their library's UUIDs
    refs = [next(p.atoms()[1] for p in f.children("property") if p.atoms()[0] == "Reference") for f in fps]
    assert refs == ["U1", "C1", "R1", "R2", "R3"]


def test_first_placement_follows_the_schematic_with_nothing_overlapping(tmp_path):
    out = tmp_path / "demo.kicad_pcb"
    r = pcb_write.write(str(out), COMPS, NETS, libraries=FP)
    b = board(out)
    at = {p.ref: (p.x, p.y) for p in b.parts}
    assert at["R1"][0] < at["U1"][0] < at["C1"][0] < at["R2"][0]  # left to right, as on the sheet
    assert at["R2"][1] < at["R3"][1]
    boxes = [(min(x for x, _ in p.courtyard), min(y for _, y in p.courtyard), max(x for x, _ in p.courtyard),
              max(y for _, y in p.courtyard)) for p in b.parts]
    for i, a in enumerate(boxes):
        for b2 in boxes[i + 1:]:
            assert not pcb_write._overlaps(a, b2)
    w, h = r["board_mm"]
    assert all(100 <= x <= 100 + w and 100 <= y <= 100 + h for x, y in at.values())


def test_writing_again_gives_the_same_board(tmp_path):
    a, b = tmp_path / "a" / "x.kicad_pcb", tmp_path / "b" / "x.kicad_pcb"
    for out in (a, b):
        pcb_write.write(str(out), COMPS, NETS, libraries=FP)
    assert a.read_text() == b.read_text()


def test_missing_footprints_are_listed_not_guessed(tmp_path):
    comps = COMPS + [Comp("J1", "conn", ""), Comp("D1", "led", "Test:LED_0603")]
    r = pcb_write.write(str(tmp_path / "x.kicad_pcb"), comps, NETS, libraries=FP)
    assert r["no_footprint"] == ["J1"]
    assert "no footprint 'LED_0603'" in r["footprint_not_found"][0]


def test_a_board_someone_placed_is_never_written_over(tmp_path):
    out = tmp_path / "x.kicad_pcb"
    out.write_text('(kicad_pcb (version 20250926) (generator "pcbnew"))\n')
    with pytest.raises(pcb_write.BoardWriteError, match="exists"):
        pcb_write.write(str(out), COMPS, NETS, libraries=FP)
    with pytest.raises(pcb_write.BoardWriteError, match="wasn't started by cadmate"):
        pcb_write.write(str(out), COMPS, NETS, libraries=FP, replace=True)


def test_kicad_netlist_is_read():
    text = '''(export (version "E")
  (components
    (comp (ref "R1") (value "10k") (footprint "Test:R_0603")
      (property (name "Sheetname") (value "Root")) (property (name "Sheetfile") (value "main.kicad_sch"))
      (sheetpath (names "/") (tstamps "/")) (tstamps "0a1b2c3d-0000-4000-8000-000000000001"))
    (comp (ref "MH1") (value "hole") (footprint "Test:R_0603") (property (name "exclude_from_board"))
      (sheetpath (names "/") (tstamps "/")) (tstamps "0a1b2c3d-0000-4000-8000-000000000002")))
  (nets
    (net (code "1") (name "/A") (class "Default") (node (ref "R1") (pin "1") (pintype "passive")))
    (net (code "2") (name "GND") (class "Default") (node (ref "R1") (pin "2") (pintype "passive")))))'''
    info = {}
    comps, nets = pcb_write.parse_netlist(text, info)
    assert info["R1.1"] == ("", "passive")
    assert [c.ref for c in comps] == ["R1"]
    assert comps[0].symbol_uuid == "/0a1b2c3d-0000-4000-8000-000000000001"
    assert comps[0].sheet_file == "main.kicad_sch"
    assert nets == {"/A": ["R1.1"], "GND": ["R1.2"]}

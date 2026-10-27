"""Fixes from a cold trial on a connector: GCT's USB4056-03-A, a hybrid USB-C receptacle (12 SMD pads, a
staggered row of 12 plated holes, 4 slotted shell legs, 2 bare pegs). The fixtures are the trial's own files."""

import json
import math
from dataclasses import replace
from pathlib import Path

from cadmate import check, footprint, generate, review, symbol, symbol_check, symbol_generate

FIXTURES = Path(__file__).parent / "fixtures"
FP = footprint.read(FIXTURES / "USB_C_Receptacle_GCT_USB4056.kicad_mod")
PART = json.loads((FIXTURES / "usb4056_part.json").read_text())
PADS = PART["footprint"]["params"]["pads"]
# The datasheet's PCB layout: Y up, origin on the front shell legs' centre line.
TABLE = [{"number": p["number"], "x": p["x"], "y": -p["y"]} for p in PADS if p["number"]]


def test_pad_gap_follows_round_pads_not_their_boxes():
    # B12 and B11 sit 0.4 mm and 0.65 mm apart: their boxes are 0.05 mm apart, the copper 0.163 mm (KiCad's DRC: 0.1632).
    r = check.check_footprint(FP)
    assert 0.162 < r["measured"]["min_pad_gap"] <= 0.1632
    assert not any("closer than" in w for w in r["warnings"])


def test_pad_gap_shapes():
    def pad(shape, x, y, w, h, rot=0.0):
        return footprint.Pad("1", "smd", shape, x, y, rot, w, h, None, ["F.Cu"])

    a, b = pad("circle", 0, 0, 1, 1), pad("circle", 1.1, 1.1, 1, 1)
    assert math.isclose(check.pad_gap(a, b), math.hypot(1.1, 1.1) - 1, abs_tol=1e-3)
    assert check.pad_gap(a, b) <= math.hypot(1.1, 1.1) - 1 + 1e-9  # never more than the real gap
    # An oval turned 90 degrees reaches 0.8 mm along X.
    assert check.pad_gap(pad("oval", 0, 0, 0.8, 1.6, 90), pad("rect", 1.0, 0, 0.6, 1.0)) < 0
    assert math.isclose(check.pad_gap(pad("oval", 0, 0, 0.8, 1.6, 90), pad("rect", 1.2, 0, 0.6, 1.0)), 0.1, abs_tol=1e-6)
    assert math.isclose(check.pad_gap(pad("rect", 0, 0, 1, 1), pad("rect", 1.5, 0, 1, 1)), 0.5, abs_tol=1e-6)


def test_every_close_pair_is_reported():
    pads = [footprint.Pad(str(i + 1), "smd", "rect", i * 0.4, 0, 0, 0.3, 1.0, None, ["F.Cu", "F.Paste", "F.Mask"]) for i in range(6)]
    fp = replace(FP, pads=pads)
    warning = next(w for w in check.check_footprint(fp)["warnings"] if "closer than" in w)
    assert warning.startswith("5 pad pairs")
    assert "5 and 6" in warning


def test_thin_rings_are_one_warning_and_bare_holes_are_not_copper():
    rings = [w for w in check.check_footprint(FP)["warnings"] if "copper ring" in w]
    assert len(rings) == 1 and "B12" in rings[0] and "B2" in rings[0]
    peg = next(p for p in FP.pads if p.kind == "np_thru_hole")
    assert not check._copper(peg)


def test_datasheet_pads_with_shell_legs_sharing_a_number():
    r = review.compare_pad_positions(FP, TABLE)
    assert r["ok"], r["errors"]
    assert r["matched"] == 28
    assert r["frame"]["datasheet_y"] == "up"
    moved = [dict(row) for row in TABLE]
    next(row for row in moved if row["number"] == "SH")["x"] += 1.0
    errors = review.compare_pad_positions(FP, moved)["errors"]
    assert errors == ["the datasheet puts a pin SH where the footprint has no pad SH (nearest is 1.00 mm away)"]
    three = [row for row in TABLE if row["number"] != "SH"] + [row for row in TABLE if row["number"] == "SH"][:3]
    r = review.compare_pad_positions(FP, three)
    assert r["ok"] and any("4 pads numbered SH" in w for w in r["warnings"])


def test_half_given_leads_are_reported_not_dropped():
    r = review.review_footprint(FP, leads={"SH": {"lead_width": 0.7}, "B1": {"lead_width": 0.24}})
    assert any("B1, SH" in s and "lead_thickness" in s for s in r["skipped"])


def test_compare_footprints_aligns_origins():
    shifted = replace(FP, pads=[replace(p, x=p.x - 2.75, y=p.y - 5.0) for p in FP.pads], courtyard=None)
    r = check.compare_footprints(FP, shifted, align=True)
    assert r["same_pads"] and r["origin_offset"] == [2.75, 5.0]
    assert check.compare_footprints(FP, shifted)["max_pad_difference"] >= 5  # unaligned, as check_footprint uses it
    assert "origin_offset" not in check.compare_footprints(FP, FP, align=True)


def test_connector_symbol_with_passive_supplies():
    spec = PART["symbol"]
    r = symbol_generate.generate(spec["name"], spec["pins"], reference="J", description=spec["description"],
                                 keywords=spec["keywords"], datasheet=PART["datasheet"], footprint="Lib:USB_C_Receptacle_GCT_USB4056")
    assert r["ok"], r["errors"]
    stacked = r["report"]["units"][1]["stacked"]
    assert stacked == {"GND": "[A1,A12,B1,B12]", "VBUS": "[A4,A9,B4,B9]"}
    sym = symbol.parse_symbol(r["text"])
    assert len(sym.pins) == 19
    assert not symbol_check.check_symbol(sym)["errors"]
    # On an IC a passive GND is still wrong.
    ic = symbol_generate.generate("X", spec["pins"], reference="U", datasheet=PART["datasheet"], footprint="Lib:X")
    assert not ic["ok"] and any("power pin by its name" in e for e in ic["errors"])


def test_symbol_body_is_the_largest_rectangle():
    sym = symbol.Symbol("J", rects=[symbol.Rect(-0.635, -0.635, 0.635, 0.635), symbol.Rect(-10.16, -20.32, 10.16, 20.32)])
    body = sym.body(1)
    assert (body.xmax - body.xmin, body.ymax - body.ymin) == (20.32, 40.64)


def test_mixed_footprint_origin_edge_line_description():
    params = dict(PART["footprint"]["params"], board_edge=-7.0)
    r = generate.generate("custom", params, name="USB", description="USB 3.2 Type-C receptacle, hybrid", tags="USB C Type-C")
    assert r["ok"], r["errors"]
    assert r["report"]["origin"] == "center"
    fp = footprint.parse(r["text"])
    ext = fp.pad_extents
    assert abs(ext.center[0]) < 1e-6 and abs(ext.center[1]) < 1e-6
    edge = [g for g in fp.on_layer("Dwgs.User") if g.kind == "line"]
    assert len(edge) == 1 and math.isclose(edge[0].pts[0][1], -7.0 + r["report"]["shifted_by"][1], abs_tol=1e-6)
    assert '(descr "USB 3.2 Type-C receptacle, hybrid")' in r["text"] and '(tags "USB C Type-C")' in r["text"]
    assert "PCB Edge" in r["text"]
    # All through-hole still goes on pin 1.
    tht = generate.generate("custom", {"body_width": 6, "body_length": 3, "pad_rows": [{"first": 1, "count": 2, "x": 0, "y": 0, "dx": 2.54, "lead_diameter": 0.64}]})
    assert tht["report"]["origin"] == "pin1"


def test_tails_that_stop_inside_the_board():
    hole = next(p for p in FP.pads if p.number == "B1")
    lead = lambda z: review.Solid("Lead_B1", footprint.BBox(hole.x - 0.1, hole.y - 0.1, hole.x + 0.1, hole.y + 0.1), z, 1.0)
    r = review.Report()
    review.check_solids(FP, [lead(-1.05)], r)
    assert any("stop 1.05 mm below the board's top" in w for w in r.warnings)
    r = review.Report()
    review.check_solids(FP, [lead(-1.05)], r, board_thickness=0.8)
    assert not any("stop" in w for w in r.warnings)
    r = review.Report()
    review.check_solids(FP, [lead(-3.0)], r)
    assert not any("stop" in w for w in r.warnings)

from pathlib import Path

import pytest

from cadmate import (
    check,
    footprint,
    generate,
    model,
    part,
    review,
    symbol,
    symbol_generate,
)

ROOT = Path(__file__).parent.parent
ZERO_FILE = ROOT / "examples" / "rp2040-zero" / "RP2040-Zero.kicad_mod"
ZERO_TEXT = ZERO_FILE.read_text()
ZERO = footprint.parse(ZERO_TEXT)
OUTLINE = {"xmin": 1.16, "ymin": -24.45, "xmax": 19.16, "ymax": -0.95}  # drawing: 18 x 23.5 mm, on the courtyard

# Waveshare's pinout: GP0..GP8 down the right edge, GP9..GP13 along the bottom, then up the left edge to 5V.
NAMES = [f"GP{i}" for i in range(16)] + ["GP26", "GP27", "GP28", "GP29", "3V3", "GND", "5V"]
POSITIONS = ([(17.78, -22.86 + i * 2.54) for i in range(9)] + [(15.24 - i * 2.54, -2.33) for i in range(5)]
             + [(2.54, -2.54 - i * 2.54) for i in range(9)])
PINOUT = [{"number": i + 1, "name": n, "x": x, "y": y} for i, (n, (x, y)) in enumerate(zip(NAMES, POSITIONS))]
# Parts on the module's underside (build_rp2040_zero.py), in the footprint frame.
UNDERSIDE = [
    {"name": "RP2040", "x": 10.15, "y": -12.13, "size_x": 7.0, "size_y": 7.0, "height": 0.9},
    {"name": "crystal", "x": 10.15, "y": -5.48, "size_x": 3.2, "size_y": 2.5, "height": 0.8},
    {"name": "LDO", "x": 10.03, "y": -21.69, "size_x": 1.6, "size_y": 2.9, "height": 1.1},
]


TYPES = {"3V3": "power_out", "GND": "power_in", "5V": "power_in"}
PINS = [{"number": i + 1, "name": n, "type": TYPES.get(n, "bidirectional")} for i, n in enumerate(NAMES)]


def zero_symbol():
    pins = PINS
    return symbol.parse_symbol(symbol_generate.generate("RP2040-Zero", pins)["text"])


def test_swapped_pads_found_from_the_footprint_alone():
    r = review.review_footprint(ZERO)
    assert any("pads 22 and 23 look swapped" in w for w in r["warnings"]), r["warnings"]
    assert "pad numbering order" in r["checked"]


def test_swapped_pads_named_against_the_datasheet_and_symbol():
    r = review.review_footprint(ZERO, datasheet_pads=PINOUT, sym=zero_symbol())
    swap = [e for e in r["errors"] if "swapped" in e]
    assert len(swap) == 1 and "pads 22 and 23" in swap[0]
    assert "GND" in swap[0] and "5V" in swap[0]
    assert r["measured"]["vs_datasheet_pins"]["matched"] == 21


def test_datasheet_frame_is_found_whatever_its_origin_and_y_direction():
    # The same pinout drawn Y up with pin 1 at the origin.
    x1, y1 = POSITIONS[0]
    table = [row | {"x": row["x"] - x1, "y": -(row["y"] - y1)} for row in PINOUT]
    cmp = review.compare_pad_positions(ZERO, table)
    assert cmp["frame"]["datasheet_y"] == "up"
    assert [e for e in cmp["errors"] if "swapped" in e]
    fixed = [row if row["number"] not in (22, 23) else row | {"number": 45 - row["number"]} for row in PINOUT]
    assert review.compare_pad_positions(ZERO, fixed)["ok"]


def test_well_numbered_footprints_raise_no_order_warning():
    for family, params in [
        ("soic", {"pins": 8, "pitch": 1.27, "body_width": 3.9, "body_length": 4.9, "overall_width": 6.0, "lead_width": [0.31, 0.51], "lead_length": [0.4, 1.27]}),
        ("qfn", {"pins": 20, "pitch": 0.5, "body_width": 4, "body_length": 4, "lead_width": [0.18, 0.3], "lead_length": [0.3, 0.5], "ep_width": 2.5, "ep_length": 2.5}),
        ("custom", {"body_width": 5.08, "body_length": 10.16, "pad_rows": [
            {"first": 1, "count": 4, "x": 0, "y": 0, "dy": 2.54, "width": 1.7, "height": 1.7, "drill": 1.0},
            {"first": 5, "count": 4, "x": 2.54, "y": 7.62, "dy": -2.54, "width": 1.7, "height": 1.7, "drill": 1.0}]}),
    ]:
        fp = footprint.parse(generate.generate(family, params)["text"])
        r = review.review_footprint(fp)
        assert not any("swapped" in w or "not in order" in w for w in r["warnings"]), (family, r["warnings"])


def test_parts_under_a_flat_module_need_a_cutout_or_a_standoff():
    r = review.review_footprint(ZERO, solids=review.solids_from_table(UNDERSIDE))
    hit = [e for e in r["errors"] if "hit the host board" in e]
    assert len(hit) == 1
    assert "RP2040" in hit[0] and "LDO" in hit[0] and "raise the part at least 1.10 mm" in hit[0]
    assert "pin headers" in hit[0]
    x0, y0, x1, y1 = r["measured"]["suggested_cutout"]
    assert (x0, x1) == pytest.approx((6.15, 14.15)) and (y0, y1) == pytest.approx((-23.64, -3.73))
    # On a 2.54 mm header's spacer the parts clear the board.
    raised = review.review_footprint(ZERO, solids=review.solids_from_table(UNDERSIDE, standoff=2.5))
    assert not any("hit the host board" in e for e in raised["errors"])
    # A board cut out by hand: a reminder, not an error.
    told = review.review_footprint(ZERO, solids=review.solids_from_table(UNDERSIDE), cutout=True)
    assert not any("hit the host board" in e for e in told["errors"])
    assert any("cut out there" in w for w in told["warnings"])


def _with_cutout(xmin, ymin, xmax, ymax):
    rect = f'\t(fp_rect (start {xmin} {ymin}) (end {xmax} {ymax}) (stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))\n'
    i = ZERO_TEXT.rindex("(embedded_fonts")
    return footprint.parse(ZERO_TEXT[:i] + rect + "\t" + ZERO_TEXT[i:])


def test_a_cutout_in_the_footprint_must_cover_the_underside_parts():
    covered = review.review_footprint(_with_cutout(6.15, -23.64, 14.15, -3.73), solids=review.solids_from_table(UNDERSIDE))
    assert not any("hit the host board" in e for e in covered["errors"])
    assert any("inside the footprint's Edge.Cuts cutout" in w for w in covered["warnings"])
    short = review.review_footprint(_with_cutout(6.15, -16.0, 14.15, -3.73), solids=review.solids_from_table(UNDERSIDE))
    hit = [e for e in short["errors"] if "hit the host board" in e]
    assert hit and "LDO" in hit[0] and "RP2040" not in hit[0] and "make the footprint's Edge.Cuts cutout cover it" in hit[0]


def test_castellated_pads_must_reach_past_the_module_edge():
    r = review.review_footprint(ZERO, body=OUTLINE)
    assert r["measured"]["castellated_copper_outside"] == pytest.approx(0.52)
    assert "castellated pads reaching past the module's edge" in r["checked"]
    assert not any("castellated" in m for m in r["errors"] + r["warnings"])
    # Copper back on the hole: the right-hand pads stop inside the module.
    centred = footprint.parse(ZERO_TEXT.replace("(offset 0.6 0)", "(offset 0 0)"))
    r = review.review_footprint(centred, body=OUTLINE)
    stop = [e for e in r["errors"] if "stop short of the module's edge" in e]
    assert stop and "'1 (0.08 mm inside)'" in stop[0]


def test_castellated_check_says_what_it_needs():
    r = review.review_footprint(ZERO)
    assert any("give body" in s for s in r["skipped"])
    assert not any("castellated" in c for c in review.review_footprint(ZERO, body=OUTLINE, castellated=False)["checked"])


def test_module_own_board_castellations_are_cut_by_the_edge():
    text = ZERO_TEXT.replace("(remove_unused_layers no)", "(property pad_prop_castellated)\n\t\t(remove_unused_layers no)")
    fp = footprint.parse(text)
    assert all(p.castellated for p in fp.pads)
    r = review.review_footprint(fp, body=OUTLINE)
    assert any("does not cut in half" in w for w in r["warnings"])  # these holes sit 1.38 mm in from the edge


def test_holes_against_datasheet_leads():
    r = review.review_footprint(ZERO, leads={"lead_width": 0.64, "lead_thickness": 0.64})
    assert any("0.64 x 0.64 mm lead (datasheet) does not fit the 0.8 mm hole" in e for e in r["errors"])
    header = footprint.parse(generate.generate("custom", {"body_width": 2.54, "body_length": 5.08, "pad_rows": [
        {"first": 1, "count": 2, "x": 0, "y": 0, "dy": 2.54, "width": 1.7, "height": 1.7, "drill": 1.0}]})["text"])
    r = review.review_footprint(header, leads={"*": {"lead_width": 0.64, "lead_thickness": 0.64}})
    assert r["measured"]["min_hole_clearance"] == pytest.approx(1.0 - 0.64 * 2 ** 0.5, abs=1e-3)
    assert not any("lead" in m for m in r["errors"] + r["warnings"])  # KiCad's own pin headers: 1.0 mm for 0.64 mm posts
    r = review.review_footprint(header, leads={"2": {"lead_diameter": [0.9, 0.98]}})
    assert any("pads 2: the 0.98 mm round lead" in w and "too tight" in w for w in r["warnings"])


def test_slots_take_flat_leads_along_their_length():
    params = {"body_width": 9, "body_length": 14, "pads": [
        {"number": 1, "x": 0, "y": 0, "lead_width": 2.0, "lead_thickness": 0.5, "slot": True},
        {"number": 2, "x": 4.7, "y": 3, "lead_width": 2.0, "lead_thickness": 0.5, "slot": True, "rotation": 90}]}
    fp = footprint.parse(generate.generate("custom", params)["text"])
    r = review.review_footprint(fp, leads={"lead_width": 2.0, "lead_thickness": 0.5})
    assert not any("lead" in e for e in r["errors"]), r["errors"]
    assert r["measured"]["min_hole_clearance"] > 0.1
    assert not review.review_footprint(fp, leads={"lead_width": 2.4, "lead_thickness": 0.5})["ok"]


def _planned_parts(family, params):
    """A generated footprint and its planned model as FreeCAD would list it: one placed box per solid, no sections."""
    r = generate.generate(family, params)
    fp = footprint.parse(r["text"])
    plan = model.plan(family, params, fp, footprint.BBox(**r["summary"]["body"]))
    parts = [{"name": s["name"], "bbox": model.bbox([s])} for s in plan["solids"]]
    return fp, [review.Solid(p["name"], review._fp_box(p["bbox"]), p["bbox"]["zmin"], p["bbox"]["zmax"]) for p in parts]


def test_generated_parts_and_models_pass_the_review():
    fp, solids = _planned_parts("custom", {"body_width": 10, "body_length": 8, "height": 10, "lead_diameter": [1.0, 1.1],
                                           "pad_rows": [{"first": 1, "count": 2, "x": 0, "y": 0, "dx": 5.08, "lead_diameter": [1.0, 1.1]}]})
    r = review.review_footprint(fp, solids=solids)
    assert r["ok"] and not r["warnings"], r
    assert "model leads in their holes" in r["checked"]
    fp, solids = _planned_parts("soic", {"pins": 8, "pitch": 1.27, "body_width": 3.9, "body_length": 4.9, "overall_width": 6.0,
                                         "lead_width": [0.31, 0.51], "lead_length": [0.4, 1.27], "height": 1.75, "standoff": 0.1})
    r = review.review_footprint(fp, solids=solids)
    assert r["ok"] and not r["warnings"], r


def test_a_lead_off_its_hole_and_a_body_too_low_are_caught():
    fp, solids = _planned_parts("custom", {"body_width": 10, "body_length": 8, "height": 10, "lead_diameter": 0.8,
                                           "pad_rows": [{"first": 1, "count": 2, "x": 0, "y": 0, "dx": 5.08, "lead_diameter": 0.8}]})
    lead = next(s for s in solids if s.name == "Lead_1")
    lead.box = footprint.BBox(lead.box.xmin + 0.4, lead.box.ymin, lead.box.xmax + 0.4, lead.box.ymax)
    body = next(s for s in solids if s.name == "Body")
    body.zmin = -0.3
    r = review.review_footprint(fp, solids=solids)
    assert any("pads 1: the 0.80 mm lead (model) does not fit" in e for e in r["errors"]), r["errors"]
    assert any("Body" in e and "0.30 mm into the board" in e for e in r["errors"])


def test_freecad_sections_tell_leads_from_bodies():
    # As inspect_model.py reports a module with header posts fused into one solid, and one part under it.
    holes = [p for p in ZERO.pads][:3]
    loops = [{"bbox": {"xmin": p.x - 0.32, "xmax": p.x + 0.32, "ymin": -p.y - 0.32, "ymax": -p.y + 0.32, "zmin": -0.8, "zmax": -0.8},
              "area": 0.4096} for p in holes]
    module = {"name": "solid 1", "bbox": {"xmin": 1.16, "xmax": 19.16, "ymin": 0.95, "ymax": 24.45, "zmin": -3.0, "zmax": 8.0}, "section": loops}
    chip = {"name": "solid 2", "bbox": {"xmin": 6.65, "xmax": 13.65, "ymin": 8.63, "ymax": 15.63, "zmin": -0.9, "zmax": 0.0},
            "section": [{"bbox": {"xmin": 6.65, "xmax": 13.65, "ymin": 8.63, "ymax": 15.63, "zmin": -0.8, "zmax": -0.8}, "area": 49.0}]}
    r = review.review_footprint(ZERO, solids=review.solids_from_model([module, chip]))
    hit = [e for e in r["errors"] if "hit the host board" in e]
    assert hit and "solid 2" in hit[0] and "solid 1" not in hit[0]
    assert any("0.64 x 0.64 mm lead (model) does not fit the 0.8 mm hole" in e for e in r["errors"])


def test_bodies_outside_the_courtyard_are_named_with_the_side():
    usb = review.Solid("USB-C", footprint.BBox(5.73, -25.65, 14.67, -18.3), 0.0, 3.25)
    r = review.review_footprint(ZERO, solids=[usb])
    assert any("USB-C" in w and "1.20 mm past the courtyard on the top" in w for w in r["warnings"]), r["warnings"]


def test_module_outline_is_read_from_the_model_board():
    pcb = review.Solid("solid 1", footprint.BBox(1.16, -24.45, 19.16, -0.95), 0.0, 1.0)
    usb = review.Solid("solid 2", footprint.BBox(5.73, -25.65, 14.67, -18.3), 1.0, 4.25)
    r = review.review_footprint(ZERO, solids=[pcb, usb])
    assert r["measured"]["module_outline"]["source"] == "model"
    assert r["measured"]["castellated_copper_outside"] == pytest.approx(0.52)


def test_placement_matrix_matches_the_box_placement():
    m = footprint.Model(path="", offset=(10.25, 12.75, 0.5), rotate=(-90, 0, 90), scale=(1, 2, 1))
    mat = check.placement_matrix(m)
    box = {"xmin": -9.0, "xmax": 9.0, "ymin": -1.0, "ymax": 4.35, "zmin": -12.95, "zmax": 11.75}
    corners = [(x, y, z) for x in (box["xmin"], box["xmax"]) for y in (box["ymin"], box["ymax"]) for z in (box["zmin"], box["zmax"])]
    moved = [tuple(sum(mat[4 * i + k] * (c[k] if k < 3 else 1.0) for k in range(4)) for i in range(3)) for c in corners]
    placed = check._apply_placement(box, m)
    for i, axis in enumerate("xyz"):
        assert min(c[i] for c in moved) == pytest.approx(placed[f"{axis}min"])
        assert max(c[i] for c in moved) == pytest.approx(placed[f"{axis}max"])


def test_review_file_without_freecad_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr("cadmate.apps.find_freecadcmd", lambda: None)
    (tmp_path / "RP2040-Zero.step").write_text("ISO-10303-21;")
    fp = tmp_path / "RP2040-Zero.kicad_mod"
    fp.write_text(ZERO_TEXT)
    r = review.review_file(fp, model_path=str(tmp_path / "RP2040-Zero.step"), underside_parts=UNDERSIDE)
    assert any("FreeCAD not found" in w for w in r["warnings"])
    assert any("hit the host board" in e for e in r["errors"])  # the parts given by hand are still checked
    unset = review.review_file(fp)  # ${KICAD_USER_BASE} is not set here
    assert any("KICAD_USER_BASE" in w for w in unset["warnings"]) and not any("KICAD_USER_BASE" in e for e in unset["errors"])


def test_check_part_passes_the_review_sources_on(tmp_path, monkeypatch):
    monkeypatch.setattr("cadmate.apps.find_freecadcmd", lambda: None)
    lib = tmp_path / "parts.pretty"
    lib.mkdir()
    (lib / "RP2040-Zero.kicad_mod").write_text(ZERO_TEXT)
    text = symbol_generate.generate("RP2040-Zero", PINS, footprint="parts:RP2040-Zero")["text"]
    (tmp_path / "parts.kicad_sym").write_text(symbol.put_in_library(None, text, "RP2040-Zero"))
    part.add_to_lib_tables(tmp_path, "parts")
    r = part.check_part(tmp_path / "parts.kicad_sym", "RP2040-Zero", review_options={"datasheet_pads": PINOUT, "body": OUTLINE})
    assert any(e.startswith("footprint: pads 22 and 23 are swapped") and "GND" in e for e in r["errors"]), r["errors"]
    assert "castellated pads reaching past the module's edge" in r["review"]["checked"]


def test_leads_of_a_part_under_the_module_fold_into_it():
    chip = review.Solid("solid 99", footprint.BBox(6.65, -15.85, 13.65, -8.85), -1.0, 0.0)
    leads = [review.Solid(f"solid {i}", footprint.BBox(x, -16.1, x + 0.2, -15.9), -0.3, 0.0) for i, x in enumerate((7.0, 7.5, 8.0))]
    stray = review.Solid("solid 9", footprint.BBox(3.0, -20.0, 3.2, -19.8), -0.3, 0.0)
    r = review.review_footprint(ZERO, solids=[chip, *leads, stray])
    hit = next(e for e in r["errors"] if "hit the host board" in e)
    assert "solid 99 (7.00 x 7.00 mm" in hit and "with 3 small solids beside it (its leads)" in hit
    assert "solid 9 (" in hit and "solid 0 (" not in hit
    assert len(r["measured"]["into_board"]) == 5

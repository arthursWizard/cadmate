import json
import shutil
from pathlib import Path

import pytest

from cadmate import protoboard as pb
from cadmate import protoboard_render as pr

EXAMPLE = Path(__file__).parent.parent / "examples" / "protoboard" / "555-blinker.json"


def board(parts, nets, wires=(), cols=10, rows=6):
    return {"board": {"type": "perfboard", "cols": cols, "rows": rows}, "parts": parts, "nets": nets, "wires": list(wires)}


def codes(result):
    return {i["code"] for i in result["issues"]}


def test_hole_names_round_trip():
    for h in [(0, 0), (4, 2), (25, 25), (2, 26), (0, 701)]:
        assert pb.parse_hole(pb.hole_name(h)) == h
    assert pb.hole_name((4, 2)) == "C5"
    assert pb.row_name(26) == "AA"
    with pytest.raises(pb.DesignError):
        pb.parse_hole("5C")


def test_library_parts_build():
    parts = pb.list_parts()
    for name in pb.LIBRARY:
        d = pb.part_def(name)
        assert len(set(d.pins.values())) == len(d.pins), name
        assert not (pb.covered_offsets(d) & set(d.pins.values()))
        assert parts[name]["pins"]


def test_dip_numbering_counter_clockwise():
    d = pb.load(board([{"ref": "U1", "part": "dip", "params": {"pins": 8}, "at": "E2"}], {}, cols=8, rows=6))
    pins = {k: pb.hole_name(h) for k, h in d.parts["U1"].pin_holes().items()}
    assert pins == {"1": "E2", "2": "E3", "3": "E4", "4": "E5", "5": "B5", "6": "B4", "7": "B3", "8": "B2"}
    assert {pb.hole_name(h) for h in d.parts["U1"].covered()} == {f"{r}{c}" for r in "CD" for c in range(2, 6)}


def test_resistor_covers_holes_between_leads_and_rotates():
    d = pb.load(board([{"ref": "R1", "part": "resistor_axial", "at": "A2"},
                       {"ref": "R2", "part": "resistor_axial", "at": "B1", "rotation": 90}], {}))
    assert {pb.hole_name(h) for h in d.parts["R1"].covered()} == {"A3", "A4", "A5"}
    assert pb.hole_name(d.parts["R2"].pin_holes()["2"]) == "F1"
    with pytest.raises(pb.DesignError):
        pb.part_def("resistor_axial", {"span": 2})


def test_check_finds_placement_errors():
    r = pb.check(board([{"ref": "R1", "part": "resistor_axial", "at": "A1"},
                        {"ref": "R2", "part": "resistor_axial", "at": "A3", "rotation": 90},  # pin 1 under R1's body
                        {"ref": "R3", "part": "resistor_axial", "at": "C8"},  # off the board
                        {"ref": "C1", "part": "cap_ceramic", "at": "F1"}],
                       {"X": ["R1.1", "C1.9"]}))
    assert {"pin_under_body", "pin_off_board", "unknown_pin"} <= codes(r)
    assert not r["ok"]


def test_check_finds_shared_hole_and_overlap():
    r = pb.check(board([{"ref": "R1", "part": "resistor_axial", "at": "B1"},
                        {"ref": "R2", "part": "resistor_axial", "at": "B5"},
                        {"ref": "U1", "part": "dip", "params": {"pins": 4}, "at": "E7"},
                        {"ref": "U2", "part": "dip", "params": {"pins": 4}, "at": "E8"}], {}))
    assert {"hole_shared", "bodies_overlap"} <= codes(r)


def test_wires_join_nets_and_shorts_are_found():
    parts = [{"ref": "R1", "part": "resistor_axial", "at": "A1"}, {"ref": "R2", "part": "resistor_axial", "at": "C1"}]
    nets = {"N1": ["R1.2", "R2.2"], "N2": ["R1.1", "R2.1"]}
    ok = pb.check(board(parts, nets, [{"net": "N1", "side": "bottom", "path": ["A5", "C5"]},
                                      {"net": "N2", "side": "bottom", "path": ["A1", "C1"]}]))
    assert ok["ok"] and set(ok["nets_done"]) == {"N1", "N2"}
    short = pb.check(board(parts, nets, [{"net": "N1", "side": "bottom", "path": ["A5", "A6", "C6", "C1"]}]))
    assert "short" in codes(short)
    jumper = pb.check(board(parts, nets, [{"net": "N2", "side": "top", "path": ["A1", "C1"]}]))
    assert "jumper_in_pin_hole" in codes(jumper)


def test_jumper_over_body_is_an_error():
    parts = [{"ref": "R1", "part": "resistor_axial", "at": "B1"}]
    r = pb.check(board(parts, {}, [{"net": "X", "side": "top", "path": ["A3", "C3"]}]))
    assert "jumper_over_body" in codes(r)


def test_route_simple_board_on_the_bottom():
    parts = [{"ref": "R1", "part": "resistor_axial", "at": "A1"}, {"ref": "R2", "part": "resistor_axial", "at": "D1"},
             {"ref": "J1", "part": "header", "params": {"pins": 2}, "at": "F8"}]
    r = pb.route(board(parts, {"A": ["R1.1", "R2.1", "J1.1"], "B": ["R1.2", "R2.2", "J1.2"]}))
    assert r["ok"], r
    assert r["jumpers"] == 0
    assert pb.check(r["design"])["ok"]


def test_route_jumps_over_a_wall():
    # Net W's locked bottom wire runs down the whole of column 5: X can only get across on top.
    parts = [{"ref": "H1", "part": "header", "params": {"pins": 1}, "at": "C2"},
             {"ref": "H2", "part": "header", "params": {"pins": 1}, "at": "C9"},
             {"ref": "H3", "part": "header", "params": {"pins": 2}, "at": "A4", "rotation": 90}]
    d = board(parts, {"X": ["H1.1", "H2.1"], "W": ["H3.1", "H3.2"]},
              [{"net": "W", "side": "bottom", "path": ["A5", "F5"]}, {"net": "W", "side": "bottom", "path": ["A4", "A5"]}])
    r = pb.route(d)
    assert r["ok"], r
    assert r["jumpers"] == 1
    jumper = next(w for w in r["design"]["wires"] if w["side"] == "top")
    assert jumper["net"] == "X"
    # the locked wires are still there, unchanged
    assert {"net": "W", "side": "bottom", "path": ["A5", "F5"]} in r["design"]["wires"]


def test_route_reports_what_it_cannot_reach():
    parts = [{"ref": "H1", "part": "header", "params": {"pins": 1}, "at": "C1"},
             {"ref": "H2", "part": "header", "params": {"pins": 1}, "at": "C6"},
             {"ref": "W1", "part": "header", "params": {"pins": 6}, "at": "A3", "rotation": 90}]  # a wall of leads
    r = pb.route(board(parts, {"X": ["H1.1", "H2.1"]}))
    assert not r["ok"]
    assert r["open"][0]["net"] == "X"


def test_route_refuses_bad_placement():
    r = pb.route(board([{"ref": "R1", "part": "resistor_axial", "at": "A8"}], {}))
    assert not r["ok"] and "error" in r


def test_example_routes_and_renders(tmp_path):
    r = pb.route(json.loads(EXAMPLE.read_text()))
    assert r["ok"], r["open"]
    out = pr.render(r["design"], tmp_path / "blinker")
    svg = Path(out["svg"]).read_text()
    assert "TOP · component side" in svg and "BOTTOM · solder side" in svg
    md = Path(out["build_list"]).read_text()
    assert "**U1** NE555" in md and "## 3. Bottom wiring" in md
    if "png" in out:
        assert Path(out["png"]).stat().st_size > 10_000


def test_bottom_view_is_mirrored():
    d = pb.load(board([{"ref": "R1", "part": "resistor_axial", "at": "A1"}], {}))
    top, bottom = pr._View(d, 0, 0, False), pr._View(d, 0, 0, True)
    assert top.x(0) < top.x(9)
    assert bottom.x(0) > bottom.x(9)
    assert bottom.x(0) == top.x(9)


def test_open_nets_listed_in_build_list():
    d = board([{"ref": "R1", "part": "resistor_axial", "at": "A1"}, {"ref": "R2", "part": "resistor_axial", "at": "D1"}],
              {"A": ["R1.1", "R2.1"]})
    md = pr.build_list(d)
    assert "## Still open" in md and "R1.1" in md


def test_stripboard_not_yet():
    with pytest.raises(pb.DesignError, match="perfboard"):
        pb.load({"board": {"type": "stripboard", "cols": 10, "rows": 10}})

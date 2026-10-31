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
    jumper = pb.check(board(parts, nets, [{"net": "N2", "side": "jumper", "path": ["A1", "C1"]}]))
    assert "jumper_in_pin_hole" in codes(jumper)


def test_jumper_over_body_is_an_error():
    parts = [{"ref": "R1", "part": "resistor_axial", "at": "B1"}]
    r = pb.check(board(parts, {}, [{"net": "X", "side": "jumper", "path": ["A3", "C3"]}]))
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
    jumper = next(w for w in r["design"]["wires"] if w["side"] == "jumper")
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


# --- double-sided boards ------------------------------------------------------------------------------


def wall_design(pads):
    """X has to get from column 2 to column 9 across W's bottom wire down column 5."""
    parts = [{"ref": "H1", "part": "header", "params": {"pins": 1}, "at": "C2"},
             {"ref": "H2", "part": "header", "params": {"pins": 1}, "at": "C9"},
             {"ref": "H3", "part": "header", "params": {"pins": 2}, "at": "A4", "rotation": 90}]
    d = board(parts, {"X": ["H1.1", "H2.1"], "W": ["H3.1", "H3.2"]},
              [{"net": "W", "side": "bottom", "path": ["A5", "F5"]}, {"net": "W", "side": "bottom", "path": ["A4", "A5"]}])
    d["board"]["pads"] = pads
    return d


def test_unplated_board_crosses_on_top_instead_of_jumping():
    r = pb.route(wall_design("double-unplated"))
    assert r["ok"], r
    assert r["jumpers"] == 0 and r["top_runs"] >= 1
    # X goes over column 5 on the top pads, so no hole in column 5 is a via
    assert all(pb.parse_hole(v["hole"])[0] != 4 for v in r["via_wires"])


def test_plated_board_is_one_node_per_hole():
    # plated: the top pad of C5 is W's too, so a top track can't help; a jumper is still needed
    r = pb.route(wall_design("double"))
    assert r["ok"] and r["jumpers"] == 1 and r["top_runs"] == 0
    d = wall_design("double")
    d["wires"].append({"net": "X", "side": "top", "path": ["C2", "C9"]})
    assert "short" in codes(pb.check(d))


def test_unplated_top_track_over_other_net_bottom_pad_is_fine():
    d = wall_design("double-unplated")
    d["wires"].append({"net": "X", "side": "top", "path": ["C2", "C9"]})
    r = pb.check(d)
    assert r["ok"], r["issues"]
    assert "X" in r["nets_done"]


def test_via_wires_listed_where_a_net_changes_side():
    d = wall_design("double-unplated")
    d["wires"] += [{"net": "X", "side": "bottom", "path": ["C2", "D2", "D3"]},
                   {"net": "X", "side": "top", "path": ["D3", "D4", "C4", "C9"]}]
    d["wires"][0] = {"net": "W", "side": "bottom", "path": ["A5", "B5"]}  # leave C5 free on the bottom too
    D = pb.load(d)
    assert [pb.hole_name(h) for _, h in pb.vias(D)] == ["D3"]
    assert "X" in pb.check(D)["nets_done"]
    md = pr.build_list(D)
    assert "Via wires" in md and "**V1** X: D3" in md


def test_top_track_rules():
    single = wall_design("single")
    single["wires"].append({"net": "X", "side": "top", "path": ["C2", "C3"]})
    assert "top_track_single_sided" in codes(pb.check(single))
    under = board([{"ref": "R1", "part": "resistor_axial", "at": "B1"}], {},
                  [{"net": "X", "side": "top", "path": ["A3", "C3"]}])
    under["board"]["pads"] = "double"
    assert "top_track_under_body" in codes(pb.check(under))
    with pytest.raises(pb.DesignError):
        pb.load({"board": {"cols": 5, "rows": 5, "pads": "triple"}})


# --- automatic placement -------------------------------------------------------------------------------


def unplaced_example():
    d = json.loads(EXAMPLE.read_text())
    for p in d["parts"]:
        p.pop("at")
        p.pop("rotation", None)
    return d


def test_place_and_route_example_from_scratch():
    from cadmate import protoboard_place as pp

    r = pp.place_and_route(unplaced_example(), tries=2)
    assert r["ok"], (r.get("open"), r["placement"])
    assert pb.check(r["design"])["ok"]


def test_place_keeps_fixed_parts_and_follows_hints():
    from cadmate import protoboard_place as pp

    d = unplaced_example()
    by = {p["ref"]: p for p in d["parts"]}
    by["U1"].update(at="H8", rotation=0, fixed=True)
    by["J1"]["region"] = "edge"
    by["D1"]["near"] = "B18"
    r = pp.place(d, seed=3)
    assert r["ok"], r["problems"]
    placed = pb.load(r["design"])
    assert placed.parts["U1"].at == pb.parse_hole("H8") and placed.parts["U1"].rotation == 0
    j1 = placed.parts["J1"].pin_holes().values()
    assert min(min(h[0], 19 - h[0], h[1], 13 - h[1]) for h in j1) == 0
    d1 = list(placed.parts["D1"].pin_holes().values())
    target = pb.parse_hole("B18")
    assert min(abs(h[0] - target[0]) + abs(h[1] - target[1]) for h in d1) <= 3
    assert pb.check(r["design"])["ok"]


def test_place_reports_bad_hints_and_no_room():
    from cadmate import protoboard_place as pp

    d = unplaced_example()
    d["parts"][0]["region"] = "upstairs"
    assert any("upstairs" in m for m in pp.place(d)["problems"])
    tiny = unplaced_example()
    tiny["board"].update(cols=6, rows=4)
    r = pp.place(tiny, iters=200)
    assert not r["ok"] and r["unplaced"]

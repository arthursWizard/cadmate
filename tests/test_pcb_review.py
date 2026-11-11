import json

import pytest

from cadmate import pcb_board as pb, pcb_review as pr

HEAD = '''(kicad_pcb
	(version 20250926)
	(generator "pcbnew")
	(general (thickness 1.6))
	(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user) (31 "F.CrtYd" user) (29 "B.CrtYd" user))
	(setup (stackup (layer "F.Cu" (type "copper") (thickness 0.035)) (layer "B.Cu" (type "copper") (thickness 0.035))))
	(gr_rect (start 0 0) (end 60 40) (stroke (width 0.05) (type default)) (fill no) (layer "Edge.Cuts") (uuid "edge"))
'''


def pad(num, x, y, net, w=0.6, h=0.6, pintype="passive", fn=None, layer="F.Cu", kind="smd", angle=0):
    f = f' (pinfunction "{fn}")' if fn else ""
    layers = '"*.Cu" "*.Mask"' if kind == "thru_hole" else f'"{layer}" "{layer[0]}.Mask" "{layer[0]}.Paste"'
    return f'(pad "{num}" {kind} rect (at {x} {y} {angle}) (size {w} {h}) (layers {layers}) (net "{net}"){f} (pintype "{pintype}"))'


def footprint(lib, ref, value, x, y, pads, rot=0, layer="F.Cu", court=None):
    c = ""
    if court:
        (x0, y0), (x1, y1) = court
        c = f'(fp_rect (start {x0} {y0}) (end {x1} {y1}) (stroke (width 0.05) (type solid)) (fill no) (layer "{layer[0]}.CrtYd"))'
    return (f'\t(footprint "{lib}" (layer "{layer}") (at {x} {y} {rot})\n'
            f'\t\t(property "Reference" "{ref}") (property "Value" "{value}") (attr smd) {c}\n\t\t' + "\n\t\t".join(pads) + "\n\t)\n")


def seg(a, b, net, w=0.25, layer="F.Cu"):
    return f'\t(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) (layer "{layer}") (net "{net}"))\n'


def via(x, y, net, size=0.6, drill=0.3):
    return f'\t(via (at {x} {y}) (size {size}) (drill {drill}) (layers "F.Cu" "B.Cu") (net "{net}"))\n'


def zone(net, layer, polys):
    fills = "".join(f'(filled_polygon (layer "{layer}") (pts {" ".join(f"(xy {x} {y})" for x, y in poly)}))' for poly in polys)
    return f'\t(zone (net "{net}") (net_name "{net}") (layer "{layer}") (polygon (pts (xy 0 0) (xy 60 0) (xy 60 40) (xy 0 40))) {fills})\n'


def ic():
    """U1 at (30, 20): VDD on pad 1 (left), GND on pad 2, XI/XO on 3/4, CLK out on 5."""
    return footprint("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "U1", "MCU", 30, 20, [
        pad(1, -2.7, -1.9, "+3V3", 1.5, 0.6, "power_in", "VDD"), pad(2, -2.7, -0.635, "GND", 1.5, 0.6, "power_in", "GND"),
        pad(3, -2.7, 0.635, "XI", 1.5, 0.6, "input", "XI"), pad(4, -2.7, 1.9, "XO", 1.5, 0.6, "output", "XO"),
        pad(5, 2.7, 1.9, "CLK", 1.5, 0.6, "output", "CLK")], court=((-3.7, -2.7), (3.7, 2.7)))


def cap(ref, x, y, pwr="+3V3", value="100n"):
    return footprint("Capacitor_SMD:C_0603_1608Metric", ref, value, x, y, [pad(1, -0.8, 0, pwr, 0.9, 0.95), pad(2, 0.8, 0, "GND", 0.9, 0.95)],
                     court=((-1.5, -0.75), (1.5, 0.75)))


def board(*items) -> pb.Board:
    return pb.parse(HEAD + "".join(items) + ")\n", "test.kicad_pcb")


def rules(result, rule):
    return [f for f in result["findings"] if f["rule"] == rule]


# --- reading the board ---------------------------------------------------------------------------------


def test_pads_land_in_board_coordinates_with_rotation():
    b = board(footprint("Capacitor_SMD:C_0603_1608Metric", "C1", "100n", 10, 10, [pad(1, -0.8, 0, "A"), pad(2, 0.8, 0, "B")], rot=90))
    p1, p2 = b.part("C1").copper_pads
    assert (round(p1.x, 3), round(p1.y, 3)) == (10, 10.8)  # 90° turns counter-clockwise on screen: -x goes down (Y down)
    assert (round(p2.x, 3), round(p2.y, 3)) == (10, 9.2)


def test_net_graph_follows_tracks_vias_and_t_joints():
    b = board(cap("C1", 10, 10), cap("C2", 20, 10),
              seg((10.8, 10), (15, 10), "GND"), seg((15, 10), (15, 15), "GND"), via(15, 15, "GND"),
              seg((15, 15), (20, 15), "GND", layer="B.Cu"), via(20, 15, "GND"), seg((20, 15), (20.8, 10), "GND"),
              seg((15, 12), (12, 12), "GND"))  # a stub joining the middle of a track
    c1, c2 = b.part("C1").copper_pads[1], b.part("C2").copper_pads[1]
    g = pb.NetGraph(b, "GND")
    r = g.path(c1, c2)
    assert r["vias"] == 2 and r["length"] == pytest.approx(4.2 + 5 + 5 + 5.06, abs=0.1)
    assert len(g.islands()) == 1


def test_net_graph_reports_islands_when_unrouted():
    b = board(cap("C1", 10, 10), cap("C2", 20, 10), seg((10.8, 10), (15, 10), "GND"))
    assert len(pb.NetGraph(b, "GND").islands()) == 2


def test_pour_connects_and_contains():
    f = pb.Fill("GND", "B.Cu", [(0, 0), (10, 0), (10, 10), (0, 10)], (0, 0, 10, 10))
    assert f.contains((5, 5)) and not f.contains((11, 5)) and not f.contains((5, 10.2))


# --- roles ------------------------------------------------------------------------------------------


def test_roles_from_names_pin_types_and_parts():
    b = board(ic(), cap("C1", 26, 17), footprint("Crystal:Crystal_SMD_3215-2Pin_3.2x1.5mm", "Y1", "32.768kHz", 22, 20,
                                                  [pad(1, -1.25, 0, "XI", 1, 1.8), pad(2, 1.25, 0, "XO", 1, 1.8)]),
              footprint("Device:R_0603", "R1", "10k", 40, 20, [pad(1, -0.8, 0, "Net-(U1-FB)"), pad(2, 0.8, 0, "/SW")]))
    roles = pr.infer_roles(b)
    assert roles["GND"]["role"] == "ground"
    assert roles["+3V3"]["role"] == "power"
    assert roles["XI"]["role"] == "crystal" and roles["XO"]["role"] == "crystal"
    assert roles["CLK"]["role"] == "fast"
    assert roles["/SW"]["role"] == "switching"
    assert roles["Net-(U1-FB)"]["role"] == "sensitive"


def test_roles_can_be_overridden():
    b = board(ic())
    assert pr.infer_roles(b, {"CLK": "signal"})["CLK"] == {"role": "signal", "why": "given by the caller"}


# --- decoupling ----------------------------------------------------------------------------------------


def _decap_board(cap_x, route):
    vdd = (30 - 2.7, 20 - 1.9)
    gnd = (30 - 2.7, 20 - 0.635)
    return board(ic(), cap("C1", cap_x, 18.1), *route(vdd, gnd, cap_x))


def test_close_cap_with_short_tracks_is_fine():
    b = _decap_board(25.2, lambda vdd, gnd, x: [seg((x - 0.8, 18.1), (x - 0.8, 17.4), "+3V3"), seg((x - 0.8, 17.4), vdd, "+3V3"),
                                                seg((x + 0.8, 18.1), (x + 0.8, 19.365), "GND"), seg((x + 0.8, 19.365), gnd, "GND")])
    r = pr.review_board(b, fab=None)
    assert rules(r, "decoupling") == []
    assert "decoupling" in r["checked"]


def test_far_cap_and_autorouted_detour_are_reported():
    def route(vdd, gnd, x):  # the router took the cap's power around the board
        return [seg((x - 0.8, 18.1), (x - 0.8, 5), "+3V3"), seg((x - 0.8, 5), (27.3, 5), "+3V3"), seg((27.3, 5), vdd, "+3V3"),
                seg((x + 0.8, 18.1), (x + 0.8, 30), "GND"), seg((x + 0.8, 30), (27.3, 30), "GND"), seg((27.3, 30), gnd, "GND")]
    b = _decap_board(24.0, route)  # 4.1 mm from VDD
    found = rules(pr.review_board(b, fab=None), "decoupling")
    whats = " | ".join(f["what"] for f in found)
    assert "placed" in whats
    assert "track runs" in whats  # routed length far over the straight line
    assert "loop" in whats
    assert all(f["source"] in pr.SOURCES for f in found)


def test_power_pin_without_any_cap():
    found = rules(pr.review_board(board(ic()), fab=None), "decoupling")
    assert not found  # no caps at all: the rule says so in not_checked instead
    r = pr.review_board(board(ic(), cap("C9", 50, 35)), fab=None)
    assert any("no bypass cap within" in f["what"] for f in rules(r, "decoupling"))


def test_cap_values():
    assert pr._farads("100n") == pytest.approx(1e-7)
    assert pr._farads("0.1uF 100V") == pytest.approx(1e-7)
    assert pr._farads("4u7") == pytest.approx(4.7e-6)
    assert pr._farads("22pF") == pytest.approx(22e-12)
    assert pr._farads("DNP") is None


# --- crystal -----------------------------------------------------------------------------------------


def _xtal(*extra):
    return board(ic(), footprint("Crystal:Crystal_SMD_3215-2Pin_3.2x1.5mm", "Y1", "32.768kHz", 20, 20,
                                 [pad(1, -1.25, 0, "XI", 1, 1.8), pad(2, 1.25, 0, "XO", 1, 1.8)], court=((-2.2, -1.2), (2.2, 1.2))),
                 seg((18.75, 20), (18.75, 20.635), "XI"), seg((18.75, 20.635), (27.3, 20.635), "XI"),
                 seg((21.25, 20), (21.25, 21.9), "XO"), seg((21.25, 21.9), (27.3, 21.9), "XO"), *extra)


def test_quiet_crystal():
    r = pr.review_board(_xtal(), fab=None)
    assert rules(r, "crystal") == []


def test_signal_next_to_and_under_the_crystal():
    r = pr.review_board(_xtal(seg((21, 22.5), (27, 22.5), "CLK"), seg((20, 15), (20, 25), "CLK", layer="F.Cu"),
                              via(14, 20, "XI"), seg((14, 20), (18.75, 20), "XI")), fab=None)
    whats = [f["what"] for f in rules(r, "crystal")]
    assert any("CLK runs" in w and "alongside crystal net XO" in w for w in whats)
    assert any("CLK passes through crystal Y1" in w for w in whats)
    assert any("via" in w for w in whats)


def test_long_crystal_trace():
    r = pr.review_board(board(ic(), footprint("Crystal:Crystal_SMD_3215-2Pin_3.2x1.5mm", "Y1", "8MHz", 5, 20,
                                              [pad(1, -1.25, 0, "XI", 1, 1.8), pad(2, 1.25, 0, "XO", 1, 1.8)]),
                              seg((3.75, 20), (3.75, 20.635), "XI"), seg((3.75, 20.635), (27.3, 20.635), "XI")), fab=None)
    assert any("of track" in f["what"] for f in rules(r, "crystal"))


# --- widths, current, vias, fab -----------------------------------------------------------------------


def test_ipc2221_round_trip():
    # 1 A, 10 °C, 1 oz outer: about 0.3 mm (12 mil) by IPC-2221's formula
    w = pr.ipc2221_width(1.0, 10)
    assert w == pytest.approx(0.30, abs=0.02)
    assert pr.ipc2221_current(w, 10) == pytest.approx(1.0, rel=1e-6)
    assert pr.ipc2221_width(1.0, 10, outer=False) > 2 * w


def test_current_and_net_class_width(tmp_path):
    b = board(ic(), cap("C1", 25.2, 18.1), seg((24.4, 18.1), (24.4, 10), "+3V3", w=0.2), seg((24.4, 10), (27.3, 18.1), "+3V3", w=0.2))
    pro = tmp_path / "t.kicad_pro"
    pro.write_text(json.dumps({"net_settings": {"classes": [{"name": "Default", "track_width": 0.2, "clearance": 0.2},
                                                             {"name": "Power", "track_width": 0.5}],
                                                 "netclass_patterns": [{"netclass": "Power", "pattern": "+3V*"}]}}))
    pb.load_project(b, pro)
    r = pr.review_board(b, currents={"+3V3": 2.0}, fab=None)
    assert any("narrower than its net class 'Power'" in f["what"] for f in rules(r, "track_width"))
    cur = rules(r, "current")
    assert cur and cur[0]["severity"] == "problem" and cur[0]["values"]["required_mm"] > 0.5


def test_power_net_without_current_gets_its_capacity():
    b = board(ic(), cap("C1", 25.2, 18.1), seg((24.4, 18.1), (27.3, 18.1), "+3V3", w=0.3))
    cur = rules(pr.review_board(b, fab=None), "current")
    assert cur[0]["severity"] == "note" and cur[0]["values"]["capacity_a"] == pytest.approx(1.0, abs=0.1)


def test_jlcpcb_via_and_track_limits():
    b = board(ic(), via(10, 10, "CLK", 0.25, 0.12), seg((5, 5), (8, 5), "CLK", w=0.08))
    fab = rules(pr.review_board(b, fab="jlcpcb"), "fab")
    assert any("below JLCPCB's minimum" in f["what"] for f in fab)
    assert any("track(s) below JLCPCB" in f["what"] for f in fab)
    assert rules(pr.review_board(b, fab=None), "fab") == []


# --- return path ----------------------------------------------------------------------------------


def test_fast_trace_over_a_gap_in_the_ground_pour():
    left = [(0, 0), (29, 0), (29, 40), (0, 40)]
    right = [(31, 0), (60, 0), (60, 40), (31, 40)]
    b = board(ic(), zone("GND", "B.Cu", [left, right]), seg((10, 10), (50, 10), "CLK"), seg((10, 12), (50, 12), "ROW1"))
    rp = rules(pr.review_board(b, fab=None), "return_path")
    fast = [f for f in rp if f["severity"] == "warning"]
    assert len(fast) == 1 and fast[0]["where"]["net"] == "CLK" and fast[0]["values"]["widest_mm"] == pytest.approx(2.0, abs=0.3)
    assert any("ROW1" in f["values"].get("nets", []) for f in rp if f["severity"] == "note")


def test_no_pour_no_return_path_check():
    r = pr.review_board(board(ic(), seg((10, 10), (50, 10), "CLK")), fab=None)
    assert any(s["rule"] == "return_path" for s in r["not_checked"])


# --- switching nodes ---------------------------------------------------------------------------------


def test_feedback_next_to_switch_node():
    reg = footprint("Package_TO_SOT_SMD:SOT-23-6", "U2", "TPS562201", 10, 30, [
        pad(1, -1.1, -0.95, "GND", 1, 0.6, "power_in", "GND"), pad(2, -1.1, 0, "/SW", 1, 0.6, "output", "SW"),
        pad(3, -1.1, 0.95, "VIN", 1, 0.6, "power_in", "VIN"), pad(4, 1.1, 0.95, "/FB", 1, 0.6, "input", "FB"),
        pad(5, 1.1, 0, "EN", 1, 0.6, "input", "EN"), pad(6, 1.1, -0.95, "BST", 1, 0.6, "input", "VBST")])
    ind = footprint("Inductor_SMD:L_1210_3225Metric", "L1", "4.7u", 20, 30, [pad(1, -1.5, 0, "/SW", 1.2, 2.5), pad(2, 1.5, 0, "+3V3", 1.2, 2.5)])
    b = board(reg, ind, seg((8.9, 30), (18.5, 30), "/SW", w=0.8), seg((11.1, 30.95), (11.1, 30.9), "/FB"), seg((11.1, 30.9), (18, 30.9), "/FB", w=0.2))
    r = pr.review_board(b, fab=None)
    sw = rules(r, "switching_node")
    assert sw and sw[0]["where"]["net"] == "/FB" and sw[0]["severity"] == "warning"


# --- assembly ------------------------------------------------------------------------------------


def test_jlcpcb_smt_spacing():
    b = board(cap("C1", 10, 10), cap("C2", 12.7, 10))  # 0603 pads 0.2 mm apart, JLCPCB asks 0.18: fine
    assert not [f for f in rules(pr.review_board(b, fab="jlcpcb"), "assembly") if "apart" in f["what"]]
    b = board(cap("C1", 10, 10), cap("C2", 12.55, 10))  # 0.05 mm: well under
    a = [f for f in rules(pr.review_board(b, fab="jlcpcb"), "assembly") if "apart" in f["what"]]
    assert a and a[0]["severity"] == "warning" and a[0]["source"] == "jlcpcb_smt"


def test_no_board_house_is_assumed():
    """Without a fab, no vendor's limits are applied: the review says what it skipped instead."""
    b = board(cap("C1", 10, 10), cap("C2", 12.55, 10), via(30, 30, "SIG", size=0.3, drill=0.1))
    r = pr.review_board(b)
    assert not [f for f in r["findings"] if "JLCPCB" in f["what"] or f["rule"] == "fab"]
    skipped = {s["rule"]: s["why"] for s in r["not_checked"]}
    assert "no board house chosen" in skipped["fab"] and "no board house chosen" in skipped["assembly_spacing"]
    # the project says the board house won't assemble it: no spacing check even with JLCPCB
    r = pr.review_board(b, fab="jlcpcb", assembly=False)
    assert not [f for f in rules(r, "assembly") if "apart" in f["what"]]
    assert any(s["rule"] == "assembly_spacing" and "won't assemble" in s["why"] for s in r["not_checked"])


def test_pcbway_limits():
    b = board(seg((10, 10), (20, 10), "SIG", w=0.08), via(30, 30, "SIG", size=0.4, drill=0.15))
    found = rules(pr.review_board(b, fab="PCBWay"), "fab")
    whats = " | ".join(f["what"] for f in found)
    assert "below PCBWay's 0.1 mm minimum" in whats
    assert "annular ring under PCBWay's 0.15 mm" in whats and "charges extra below 0.2 mm" in whats
    assert all(f["source"] == "pcbway_pcb" for f in found)
    # a house cadmate has no profile for: named in what was skipped, nothing invented
    r = pr.review_board(b, fab=None, house="Acme Boards")
    assert not rules(r, "fab") and any("no rule profile for Acme Boards" in s["why"] for s in r["not_checked"])


@pytest.mark.parametrize("house,name,track,drill,ring", [("OSH Park", "OSH Park", 0.1524, 0.254, 0.127),
                                                          ("aisler", "AISLER", 0.2, 0.3, 0.2),
                                                          ("Eurocircuits", "Eurocircuits", 0.1, 0.1, 0.15)])
def test_other_houses_track_via_and_ring_limits(house, name, track, drill, ring):
    b = board(seg((10, 10), (20, 10), "SIG", w=0.08), via(30, 30, "SIG", size=0.3, drill=0.08))
    found = rules(pr.review_board(b, fab=house), "fab")
    whats = " | ".join(f["what"] for f in found)
    assert f"below {name}'s {track:g} mm minimum" in whats
    assert f"below {name}'s {drill:g} mm minimum" in whats and f"annular ring under {name}'s {ring:.3g} mm" in whats
    key = pr.fabmod.profile(house)
    assert all(f["source"] == f"{key}_pcb" for f in found) and f"{key}_pcb" in pr.SOURCES
    # a via the house makes comfortably: nothing to say
    ok = board(via(30, 30, "SIG", size=0.8, drill=0.4))
    assert rules(pr.review_board(ok, fab=house), "fab") == []


def test_eurocircuits_multilayer_via_needs_the_inner_pad():
    """A through via has its pad on the inner layers too, where Eurocircuits wants the hole + 0.35 mm, not + 0.30."""
    v = via(30, 30, "SIG", size=0.4, drill=0.1)
    two = board(v)
    four = pb.parse(HEAD.replace('(2 "B.Cu" signal)', '(4 "In1.Cu" signal) (6 "In2.Cu" signal) (2 "B.Cu" signal)') + v + ")\n",
                    "test.kicad_pcb")
    assert len(four.copper_layers) == 4
    assert rules(pr.review_board(two, fab="eurocircuits"), "fab") == []
    assert any("annular ring under Eurocircuits's 0.175 mm" in f["what"] for f in rules(pr.review_board(four, fab="eurocircuits"), "fab"))


def test_package_classes():
    def part(lib, ref="U1", pads=8):
        return pb.Part(ref, "", lib, 0, 0, 0, "front", [pb.Pad(ref, str(i), None, 0, 0, 1, 1, 0, "rect", ["F.Cu"], "smd") for i in range(pads)],
                       [], True, False)
    assert pr.package_class(part("Capacitor_SMD:C_0402_1005Metric", "C1", 2)) == ("chip", "0402")
    assert pr.package_class(part("footprints:2012_C", "C1", 2)) == ("chip", "0805")
    assert pr.package_class(part("Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm"))[0] == "qfn"
    assert pr.package_class(part("Package_QFP:LQFP-48_7x7mm_P0.5mm"))[0] == "qfp"
    assert pr.package_class(part("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"))[0] == "sop"
    assert pr.package_class(part("Package_TO_SOT_SMD:SOT-23"))[0] == "sot"
    assert pr.package_class(part("Connector:USB_C")) is None
    assert pr._smt_spacing(("chip", "0402"), ("chip", "0402")) == 0.15
    assert pr._smt_spacing(("qfn", ""), ("chip", "0603")) == 1.0


# --- output --------------------------------------------------------------------------------------


def test_every_finding_cites_a_known_source_and_lists_what_was_skipped():
    r = pr.review_board(_xtal(seg((21, 22.5), (27, 22.5), "CLK")), fab="jlcpcb")
    assert r["ok"] and r["findings"]
    for f in r["findings"]:
        assert f["source"] in r["sources"] and f["severity"] in ("problem", "warning", "note")
    assert {s["rule"] for s in r["not_checked"]} >= {"switching_node", "kicad_drc"}
    assert r["limits"]["decap_distance"] == pr.LIMITS["decap_distance"]


def test_drc_report_becomes_findings():
    rep = {"unconnected_items": [{"items": [{"description": "Pad 1 [GND] of C1", "pos": {"x": 1, "y": 2}}]}],
           "violations": [{"type": "clearance", "severity": "error", "description": "Clearance violation",
                           "items": [{"description": "Track [CLK]", "pos": {"x": 3, "y": 4}}]}]}
    r = pr.review_board(board(ic()), fab=None, drc=rep)
    assert r["findings"][0]["rule"] in ("unconnected", "drc") and r["counts"]["problem"] == 2
    assert "kicad_drc" in r["checked"]
    assert r["drc"]["errors"] == 1 and r["drc"]["unconnected"] == 1 and r["drc"]["warnings"] == 0


def test_drc_summary_counts_warnings_by_type():
    rep = {"violations": [{"type": "silk_overlap", "severity": "warning"}] * 71 + [{"type": "track_dangling", "severity": "warning"}] * 7,
           "unconnected_items": [], "schematic_parity": []}
    s = pr.drc_summary(rep)
    assert (s["errors"], s["warnings"], s["unconnected"]) == (0, 78, 0)
    assert s["warnings_by_type"] == {"silk_overlap": 71, "track_dangling": 7}


# --- differential pairs and what was measured -----------------------------------------------------------


def test_pair_names():
    nets = ["/U1D+", "/U1D-", "USB_DP", "USB_DM", "LVDS_P", "LVDS_N", "PWM_P", "GND", "VBUSP", "VBUSN"]
    pairs = pr.diff_pairs(nets)
    assert ("/U1D+", "/U1D-") in pairs and ("USB_DP", "USB_DM") in pairs and ("LVDS_P", "LVDS_N") in pairs
    assert not any("VBUS" in a for a, _ in pairs)


def test_pair_routed_apart_and_unequal():
    together = board(ic(), seg((5, 10), (25, 10), "USB_D+", w=0.2), seg((5, 10.45), (25, 10.45), "USB_D-", w=0.2))
    r = pr.review_board(together, fab=None)
    assert rules(r, "diff_pair") == [] and r["measured"]["diff_pair"][0]["coupled"] == 1.0
    apart = board(ic(), seg((5, 10), (25, 10), "USB_D+", w=0.2), seg((5, 10.45), (8, 10.45), "USB_D-", w=0.2),
                  seg((8, 10.45), (8, 20), "USB_D-", w=0.2), seg((8, 20), (25, 20), "USB_D-", w=0.2))
    whats = [f["what"] for f in rules(pr.review_board(apart, fab=None), "diff_pair")]
    assert any("lengths differ" in w for w in whats) and any("run together" in w for w in whats)


def test_measurements_are_reported_without_findings():
    r = pr.review_board(_xtal(), fab=None)
    xi = next(m for m in r["measured"]["crystal"] if m["net"] == "XI")
    assert xi["route_mm"] == pytest.approx(0.635 + 8.55, abs=0.2) and xi["vias"] == 0


def test_unrouted_pair_is_not_called_a_mismatch():
    b = board(footprint("Connector:USB", "J1", "USB", 5, 10, [pad(1, 0, 0, "USB_D+"), pad(2, 0, 1, "USB_D-")]),
              footprint("Connector:USB", "J2", "USB", 25, 10, [pad(1, 0, 0, "USB_D+"), pad(2, 0, 1, "USB_D-")]),
              seg((5, 10), (7, 10), "USB_D+", w=0.2), seg((5, 11), (25, 11), "USB_D-", w=0.2))
    found = rules(pr.review_board(b, fab=None), "diff_pair")
    assert len(found) == 1 and "not fully routed" in found[0]["what"]


def test_cap_with_its_ground_side_unrouted():
    b = _decap_board(25.2, lambda vdd, gnd, x: [seg((x - 0.8, 18.1), (x - 0.8, 17.4), "+3V3"), seg((x - 0.8, 17.4), vdd, "+3V3")])
    assert any("ground pad" in f["what"] for f in rules(pr.review_board(b, fab=None), "decoupling"))


def test_front_part_with_back_smd_pads_counts_as_a_back_part():
    sw = footprint("cadmate:BackPads", "SW1", "sw", 20, 20, [pad("1", 3.2, 6, "A", w=2.6, h=2.6, layer="B.Cu"),
                                                          pad("2", -8.2, 3.8, "B", w=2.6, h=2.6, layer="B.Cu")])
    r = footprint("R:R", "R1", "1k", 40, 20, [pad("1", -0.5, 0, "A", layer="B.Cu"), pad("2", 0.5, 0, "B", layer="B.Cu")],
                  layer="B.Cu")
    res = pr.review_board(board(sw, r))
    assert not [f for f in res["findings"] if f["what"].startswith("SMD parts on both sides")]


def test_only_the_boards_own_schematic_is_read(tmp_path):
    (tmp_path / "main.kicad_sch").write_text('(kicad_sch (sheet (property "Sheetfile" "sub.kicad_sch")))')
    (tmp_path / "sub.kicad_sch").write_text("(kicad_sch)")
    (tmp_path / "main-routed.kicad_sch").write_text("(kicad_sch)")
    assert {f.name for f in pb.schematic_files(tmp_path, "main")} == {"main.kicad_sch", "sub.kicad_sch"}
    assert len(pb.schematic_files(tmp_path, "nothing")) == 3


def test_schematic_parity_from_drc_is_reported():
    rep = {"violations": [], "unconnected_items": [],
           "schematic_parity": [{"type": "net_conflict", "severity": "warning", "description": "Net mismatch",
                                 "items": [{"description": "Pad 2 of D1", "pos": {"x": 1, "y": 2}}]}] * 3}
    [f] = pr._drc_findings(rep)
    assert f["rule"] == "parity" and f["severity"] == "warning" and "3 × net_conflict" in f["what"]

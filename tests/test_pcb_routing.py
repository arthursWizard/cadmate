import pytest

from cadmate import freerouting as fr, pcb_board as pb, pcb_critical as pc, pcb_route as rt
from test_pcb_review import HEAD, board, cap, footprint, ic, pad, zone


def items(plan, cls):
    return [it for it in plan["items"] if it["class"] == cls]


def vcap(ref="C1", x=25.5, y=16.5, net="+3V3"):
    """A 0603 standing up left of U1's VDD pin: pad 1 (power) at the bottom, nearest the pin."""
    return footprint("Capacitor_SMD:C_0603_1608Metric", ref, "100n", x, y, [pad(1, -0.8, 0, net, 0.9, 0.95), pad(2, 0.8, 0, "GND", 0.9, 0.95)],
                     rot=90, court=((-1.5, -0.75), (1.5, 0.75)))


def gnd_pour():
    return zone("GND", "B.Cu", [[(0, 0), (60, 0), (60, 40), (0, 40)]])


# --- classification --------------------------------------------------------------------------------------


def test_decap_item_routes_cap_to_pin_and_ground_into_the_pour():
    b = board(ic(), cap("C1", 25.2, 18.1), gnd_pour())
    plan = pc.classify(b)
    [d] = items(plan, "decap")
    assert d["action"] == "route" and d["refs"] == ["C1", "U1"] and d["sources"] == ["mt101"]
    power, ground = d["connections"]
    assert (power["a"]["pad"], power["b"]["pad"], power["max_vias"]) == ("C1.1", "U1.1", 0)
    assert ground["b"]["pad"] == "U1.2"  # the IC's ground pin is right there: a track, not a via


def test_far_decap_is_a_placement_problem_not_a_route():
    b = board(ic(), cap("C1", 20, 18.1), gnd_pour())
    [d] = items(pc.classify(b), "decap")
    assert d["action"] == "place" and "move it to the pin" in d["placement"][0]


def test_power_pin_without_cap_is_listed():
    [d] = items(pc.classify(board(ic())), "decap")
    assert d["action"] == "place" and "no bypass cap" in d["title"]


def test_crystal_load_caps_and_ground_vias():
    y = footprint("Crystal:Crystal_SMD_3215-2Pin_3.2x1.5mm", "Y1", "12MHz", 22, 20, [pad(1, -1.25, 0, "XI", 1, 1.8), pad(2, 1.25, 0, "XO", 1, 1.8)])
    c = footprint("Capacitor_SMD:C_0402_1005Metric", "C5", "18p", 20.75, 23, [pad(1, 0, 0, "XI", 0.5, 0.5), pad(2, 0, 1, "GND", 0.5, 0.5)])
    [x] = items(pc.classify(board(ic(), y, c, gnd_pour())), "crystal")
    pads = {(cn["a"]["pad"], cn["b"].get("pad") or "via") for cn in x["connections"]}
    assert ("Y1.1", "U1.3") in pads and ("Y1.2", "U1.4") in pads and ("C5.1", "Y1.1") in pads and ("C5.2", "via") in pads
    assert x["rule"]["max_vias"] == 0 and x["sources"] == ["an2867"]


def usb_board():
    j = footprint("Connector_USB:USB_Micro", "J1", "USB", 10, 20, [pad(2, 0, -0.65, "/D-", 1.3, 0.4), pad(3, 0, 0, "/D+", 1.3, 0.4),
                                                                     pad(5, 0, 1.3, "GND", 1.3, 0.4)])
    u = footprint("Package_DFN_QFN:QFN-16", "U2", "PHY", 30, 20, [pad(1, -1.5, -0.65, "/D-", 0.6, 0.3), pad(2, -1.5, 0, "/D+", 0.6, 0.3),
                                                                 pad(3, 1.5, 0, "GND", 0.6, 0.3), pad(4, 1.5, 0.65, "x1"), pad(5, 1.5, -0.65, "x2")])
    tvs = footprint("Package_TO_SOT_SMD:SOT-23", "D1", "USBLC6", 15, 23, [pad(1, -1, 0, "/D+", 0.6, 0.6), pad(2, 1, 0, "GND", 0.6, 0.6)])
    return board(j, u, tvs, gnd_pour())


def test_usb_pair_with_tvs_in_its_path():
    plan = pc.classify(usb_board())
    [p] = items(plan, "diff_pair")
    assert p["nets"] == ["/D+", "/D-"] and p["rule"]["impedance_ohm"] == 90 and p["sources"][0] == "spraar7"
    assert p["rule"]["max_mismatch_mm"] == pytest.approx(1.27)
    chain = [(c["a"]["pad"], c["b"]["pad"]) for c in p["connections"] if c["net"] == "/D+"]
    assert chain == [("J1.3", "D1.1"), ("D1.1", "U2.2")]  # through the TVS pad, not past it
    [e] = items(plan, "esd")
    assert e["refs"] == ["D1", "J1"] and "routed with their pair" in e["notes"][0]


def test_hot_loop_finds_input_cap_switch_node_inductor_and_output_cap():
    buck = footprint("Package_TO_SOT_SMD:SOT-23-6", "U3", "TPS562201", 30, 20, [
        pad(1, -1, -0.95, "GND", 0.6, 0.6, "power_in", "GND"), pad(2, -1, 0, "/SW", 0.6, 0.6, "output", "SW"),
        pad(3, -1, 0.95, "+12V", 0.6, 0.6, "power_in", "VIN"), pad(4, 1, 0.95, "Net-(U3-FB)", 0.6, 0.6, "input", "FB"),
        pad(5, 1, 0, "Net-(U3-EN)", 0.6, 0.6, "input", "EN"), pad(6, 1, -0.95, "Net-(U3-VBST)", 0.6, 0.6, "input", "VBST")])
    ind = footprint("Inductor_SMD:L_1210", "L1", "4.7u", 25, 20, [pad(1, 0, -1.4, "/SW", 1, 1), pad(2, 0, 1.4, "+5V", 1, 1)])
    cin = cap("C1", 28, 23, "+12V", "10u")
    cout = cap("C2", 22, 23, "+5V", "22u")
    rfb = footprint("Resistor_SMD:R_0402", "R1", "10k", 33, 21, [pad(1, 0, 0, "Net-(U3-FB)", 0.5, 0.5), pad(2, 0, 1, "GND", 0.5, 0.5)])
    plan = pc.classify(board(buck, ind, cin, cout, rfb, gnd_pour()))
    [h] = items(plan, "hot_loop")
    assert {"U3", "L1", "C1", "C2"} <= set(h["refs"]) and h["rule"]["max_vias"] == 0 and h["sources"] == ["an139", "an1149"]
    nets = {(c["a"]["pad"], c["b"].get("pad")) for c in h["connections"]}
    assert ("U3.2", "L1.1") in nets and ("C1.1", "U3.3") in nets and ("C1.2", "U3.1") in nets
    [s] = items(plan, "sensitive")
    assert s["nets"] == ["Net-(U3-FB)"] and "FB" in s["why"]


def test_strap_and_rf_and_high_current():
    pull = footprint("Resistor_SMD:R_0402", "R2", "10k", 34, 18, [pad(1, 0, 0, "Net-(U1-CLK)", 0.5, 0.5), pad(2, 0, 1, "+3V3", 0.5, 0.5)])
    ufl = footprint("Connector_Coaxial:U.FL_Hirose_U.FL-R-SMT-1_Vertical", "J2", "U.FL", 50, 20,
                    [pad(1, 0, 0, "/ANT", 0.8, 0.8), pad(2, 1.5, 0, "GND", 1, 2)])
    mod = footprint("RF_Module:ESP32", "U4", "ESP32", 45, 20, [pad(1, 0, 0, "/ANT", 0.8, 0.8, "bidirectional", "ANT"),
                                                               pad(2, 0, 1, "GND", 0.8, 0.8), pad(3, 0, 2, "x"), pad(4, 0, 3, "y"), pad(5, 0, 4, "z")])
    ic_ = footprint("Package_SO:SOIC-8", "U1", "MCU", 30, 20, [pad(5, 2.7, -1.9, "Net-(U1-CLK)", 1.5, 0.6, "input", "RESET"),
                                                               pad(1, -2.7, -1.9, "+3V3", 1.5, 0.6, "power_in", "VDD")])
    plan = pc.classify(board(ic_, pull, ufl, mod, gnd_pour()), currents={"+3V3": 2.0})
    [s] = items(plan, "strap")
    assert s["connections"][0]["a"]["pad"] == "R2.1" and "named like a strap" in s["why"]
    [rf] = items(plan, "rf")
    assert rf["action"] == "hand" and rf["rule"]["impedance_ohm"] == 50
    [hc] = items(plan, "high_current")
    assert hc["action"] == "netclass" and hc["rule"]["width_outer_mm"] == pytest.approx(pc.ipc2221_width(2.0, 10, 0.035, True), abs=1e-3)


def test_overrides_and_table():
    b = board(ic(), cap("C1", 25.2, 18.1), gnd_pour())
    plan = pc.classify(b, overrides={"decap:U1.1": "hand"})
    [d] = items(plan, "decap")
    assert d["action"] == "hand" and d["overridden"]
    assert "| 1 | Decoupling cap to pin |" in plan["table"] and set(plan["sources"]) >= {"mt101"}


def test_every_class_has_a_rule_and_known_sources():
    for c in pc.CLASSES.values():
        assert c.rule and c.sources and all(s in pc.SOURCES for s in c.sources), c.name


# --- routing -------------------------------------------------------------------------------------------


def _clear(b: pb.Board, tracks, vias):
    """No added track or via comes closer than the minimum clearance to other-net copper."""
    obs = rt.Obstacles(b)
    own = [p for p in b.pads]
    for t in tracks:
        n = max(2, int(t.length / 0.02))
        for k in range(n + 1):
            p = (t.start[0] + (t.end[0] - t.start[0]) * k / n, t.start[1] + (t.end[1] - t.start[1]) * k / n)
            if any(q.net == t.net and q.contains(p) for q in own):
                continue  # inside its own pad: that copper is the footprint's
            ok, what = obs.clear(t.layer, p, t.net, t.width / 2 - 1e-3)
            assert ok, (t, what)


def test_router_connects_decap_on_one_layer_and_locks_it():
    b = board(ic(), vcap(), gnd_pour())
    plan = pc.classify(b)
    res = rt.route_plan(b, plan)
    assert res["failed"] == 0 and res["routed"] == 2
    assert all(t.locked for t in res["tracks"]) and {t.layer for t in res["tracks"]} == {"F.Cu"}
    c1, vdd = b.part("C1").copper_pads[0], b.part("U1").copper_pads[0]
    path = pb.NetGraph(b, "+3V3").path(c1, vdd)
    assert path and path["vias"] == 0 and path["length"] < 4
    _clear(b, res["tracks"], res["vias"])


def test_router_goes_around_an_obstacle_and_keeps_clearance():
    wall = footprint("Resistor_SMD:R_0603", "R9", "0", 26.2, 17.0, [pad(1, -0.6, 0, "OTHER", 0.8, 0.8), pad(2, 0.6, 0, "OTHER2", 0.8, 0.8)])
    b = board(ic(), vcap(x=25.0, y=17.5), wall, gnd_pour())
    plan = pc.classify(b, overrides={"decap:U1.1": "route"})
    res = rt.route_plan(b, plan, ids=["decap:U1.1"])
    assert res["connections"][0]["ok"], res["connections"]
    _clear(b, res["tracks"], res["vias"])


def test_ground_via_beside_the_pad_when_the_ic_ground_is_far():
    far_gnd = footprint("Package_SO:SOIC-8", "U1", "MCU", 30, 20, [pad(1, -2.7, -1.9, "+3V3", 1.5, 0.6, "power_in", "VDD"),
                                                                    pad(8, 2.7, 1.9, "GND", 1.5, 0.6, "power_in", "GND")])
    b = board(far_gnd, vcap(), gnd_pour())
    plan = pc.classify(b)
    [d] = items(plan, "decap")
    assert "via" in d["connections"][1]["b"]
    res = rt.route_plan(b, plan)
    assert res["failed"] == 0 and len(res["vias"]) == 1 and res["vias"][0].net == "GND"
    v = res["vias"][0]
    gp = b.part("C1").copper_pads[1]
    assert pb.dist((v.x, v.y), (gp.x, gp.y)) < 1.5


def test_pair_routes_both_nets_and_reports_mismatch():
    b = usb_board()
    res = rt.route_plan(b, pc.classify(b), ids=["diff_pair:/D+"])
    [p] = res["pairs"]
    assert p["complete"], res["connections"]
    assert "mismatch_mm" in p
    _clear(b, res["tracks"], res["vias"])


def test_written_file_has_locked_segments_on_the_right_nets(tmp_path):
    src = tmp_path / "b.kicad_pcb"
    src.write_text(HEAD + ic() + vcap() + gnd_pour() + ")\n")
    b = pb.parse(src.read_text(), src.name)
    res = rt.route_plan(b, pc.classify(b))
    dst = tmp_path / "out" / "b-critical.kicad_pcb"
    rt.write_file(src, dst, res["tracks"], res["vias"])
    again = pb.parse(dst.read_text(), dst.name)
    assert len(again.tracks) == len(res["tracks"]) and all(t.locked for t in again.tracks)
    assert {t.net for t in again.tracks} == {"+3V3", "GND"}


# --- Freerouting round trip ----------------------------------------------------------------------------


DSN = """(pcb /tmp/b.dsn
  (parser (string_quote ") (space_in_quoted_tokens on) (host_cad "KiCad's Pcbnew") (host_version "10.0.4"))
  (resolution um 10)
  (unit um)
  (structure (layer F.Cu (type signal)) (layer B.Cu (type signal)))
  (placement (component "C_0603" (place C1 25200 -17000 front 0)))
  (network
    (net GND (pins C1-2 U1-2))
    (net "+3V3" (pins C1-1 U1-1))
    (net "/D+" (pins J1-3 U2-2))
    (class kicad_default "" "+3V3" "/D+" GND
      (circuit
        (use_via "Via[0-1]_600:300_um")
      )
      (rule
        (width 200)
        (clearance 200)
      )
    )
  )
  (wiring
  )
)
"""


def test_dsn_widths_moves_a_net_into_its_own_class():
    out = fr.dsn_widths(DSN, {"+3V3": 1.0})
    default = out[out.index("(class kicad_default"):out.index("(circuit")]
    assert '"+3V3"' not in default and "GND" in default and '"/D+"' in default
    assert '(class cadmate_wide_0 "+3V3"' in out and "(width 1000.0)" in out and "(clearance 200)" in out
    assert out.index("cadmate_wide_0") < out.index("(wiring")



def test_dsn_gets_mask_openings_as_keepouts():
    b = board(cap("C1", 25.2, 17.0), '(gr_rect (start 10 10) (end 12 12) (fill solid) (layer "F.Mask"))\n')
    out = fr.dsn_keepouts(DSN, b)
    k = out[out.index("(keepout"):]
    assert k.startswith('(keepout "" (polygon F.Cu 0 ') and "10000.0 -10000.0" in k and "12000.0 -12000.0" in k
    assert out.index("(keepout") < out.index("(placement")

SES = """(session b.ses
  (base_design b.dsn)
  (placement
    (resolution um 10)
    (component "C_0603" (place C1 252000 -170000 front 0))
    (component "SOIC" (place U1 300000 -200000 front 0))
  )
  (was_is)
  (routes
    (resolution um 10)
    (parser (host_cad "KiCad's Pcbnew") (host_version "10.0.4"))
    (library_out
      (padstack "Via[0-1]_600:300_um" (shape (circle F.Cu 6000 0 0)) (shape (circle B.Cu 6000 0 0)) (attach off))
    )
    (network_out
      (net GND
        (wire (path F.Cu 1124 260000 -170000 260000 -193650 273000 -193650))
        (via "Via[0-1]_600:300_um" 260000 -170000)
      )
    )
  )
)
"""


def test_ses_is_read_in_board_coordinates_and_narrow_tracks_widened():
    b = board(ic(), cap("C1", 25.2, 17.0))
    pb.load_project(b, None)
    b.net_classes["Default"] = pb.NetClass("Default", 0.2, 0.2, 0.6, 0.3)
    tracks, vias = fr.parse_ses(SES, b)
    assert [(t.start, t.end) for t in tracks] == [((26.0, 17.0), (26.0, 19.365)), ((26.0, 19.365), (27.3, 19.365))]
    assert tracks[0].width == pytest.approx(0.1124) and tracks[0].net == "GND"
    assert (vias[0].x, vias[0].y, vias[0].size, vias[0].drill) == (26.0, 17.0, 0.6, 0.3)
    widened = fr.widen(b, tracks, {})
    assert widened == [{"net": "GND", "from_mm": 0.1124, "to_mm": 0.2, "segments": 2}] and tracks[0].width == 0.2


def test_widen_stops_where_clearance_would_break():
    b = board(ic())
    b.net_classes["Default"] = pb.NetClass("Default", 0.2, 0.2, 0.6, 0.3)
    t = pb.Track((26.0, 17.0), (26.0, 19.365), 0.1124, "F.Cu", "GND")  # 0.55 mm from U1.1's edge
    [w] = fr.widen(b, [t], {"GND": 1.0})
    assert w["to_mm"] == pytest.approx(0.7, abs=0.006) and w["wanted_mm"] == 1.0 and w["held_back_by"].startswith("U1 pad 1")
    assert t.width == w["to_mm"] and "below_board_minimum" not in w


def test_vias_keep_hole_to_hole_on_the_same_net():
    b = board(gnd_pour())
    r = rt.Router(b)
    r.commit(rt.Result({}, "x", True, vias=[pb.Via(10, 10, 0.6, 0.3, ["F.Cu", "B.Cu"], "GND")]))
    assert not r._via_fits((10.4, 10), "GND", 0.6)
    assert r._via_fits((10.9, 10), "GND", 0.6)


def test_nothing_is_routed_in_a_solder_mask_opening():
    logo = footprint("Logo:Small", "LOGO1", "LOGO", 40, 10, ['(fp_poly (pts (xy -1 -1) (xy 1 -1) (xy 1 1) (xy -1 1)) (layer "B.Mask"))'])
    opening = '(gr_rect (start 10 10) (end 12 12) (fill solid) (layer "F.Mask"))\n'
    b = board(logo, opening)
    assert sorted(l for l, _ in b.mask_openings) == ["B.Cu", "F.Cu"]
    obs = rt.Obstacles(b)
    assert obs.clear("F.Cu", (11, 11), "X", 0.1) == (False, "a solder mask opening")
    assert obs.clear("B.Cu", (11, 11), "X", 0.1)[0]
    assert not obs.clear("B.Cu", (40, 10), "X", 0.1)[0] and obs.clear("F.Cu", (40, 10), "X", 0.1)[0]


def test_ses_items_already_on_the_board_are_not_added_twice():
    b = board(ic(), cap("C1", 25.2, 17.0))
    tracks, vias = fr.parse_ses(SES, b)
    b.tracks.append(pb.Track((26.0, 19.365), (26.0, 17.0), 0.2, "F.Cu", "GND", locked=True))
    new_t, new_v = fr.new_items(b, tracks, vias)
    assert len(new_t) == 1 and len(new_v) == 1


def test_pair_on_one_layer_is_routed_coupled_and_matched():
    j = footprint("Connector:Header", "J1", "USB", 10, 20, [pad(2, 0, -0.5, "/D-", 0.3, 0.3), pad(3, 0, 0, "/D+", 0.3, 0.3)])
    u = footprint("Package_SO:SOIC-8", "U2", "PHY", 25, 26, [pad(1, 0, -0.5, "/D-", 0.3, 0.3), pad(2, 0, 0, "/D+", 0.3, 0.3),
                                                            pad(3, 0, 3, "GND", 0.3, 0.3), pad(4, 0, 4, "a"), pad(5, 0, 5, "b")])
    b = board(j, u, gnd_pour())
    res = rt.route_plan(b, pc.classify(b), ids=["diff_pair:/D+"])
    [p] = res["pairs"]
    assert p["complete"] and all(c.get("note", "").startswith("routed as a coupled pair") for c in res["connections"])
    assert p["mismatch_mm"] < 0.5 and p["within"]
    _clear(b, res["tracks"], res["vias"])


def test_flow_on_a_file_writes_a_new_board_and_keeps_the_original(tmp_path):
    from cadmate import pcb_flow

    src = tmp_path / "b.kicad_pcb"
    src.write_text(HEAD + ic() + vcap() + gnd_pour() + ")\n")
    plan = pcb_flow.classify(str(src))
    assert plan["ok"] and "next" in plan
    out = pcb_flow.route_critical(str(src))
    assert out["routed"] == 2 and out["written"].endswith("b-critical.kicad_pcb")
    assert "(segment" not in src.read_text()
    with pytest.raises(pcb_flow.FlowError):
        pcb_flow.route_critical(str(src), out_path=str(src))


def test_autoroute_says_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CADMATE_FREEROUTING_JAR", str(tmp_path / "nope.jar"))
    monkeypatch.setenv("CADMATE_KICAD_PYTHON", "/bin/true")
    dsn, ses = tmp_path / "x.dsn", tmp_path / "x.ses"
    dsn.write_text(DSN)
    with pytest.raises(fr.RouteError, match="jar not found"):
        fr.run_freerouting(dsn, ses)


def test_custom_pad_polygons_count_as_its_copper():
    jp = ('(pad "2" smd custom (at 0 0) (size 1 1) (layers "F.Cu") (net "VIN") '
          '(primitives (gr_poly (pts (xy 0 -0.5) (xy 2 -0.5) (xy 2 0.5) (xy 0 0.5)) (width 0) (fill yes))))')
    b = board(footprint("Jumper:SolderJumper", "JP1", "JP", 20, 10, [jp], rot=90))
    [p] = b.part("JP1").copper_pads
    assert p.contains((21.8, 10)) and not p.contains((22.3, 10))  # the pad's own orientation is 0: not turned with the part
    assert p.distance((22.5, 10)) == pytest.approx(0.5)


def test_fab_limits_have_a_buffer_over_the_fab_minimum():
    from cadmate import fab

    jlc = fab.limits("JLCPCB")["rules"]["track"]
    assert jlc == {"hard": 0.10, "preferred": 0.125}
    assert fab.limits("jlcpcb", layers=4)["rules"]["track"]["hard"] == 0.09
    with pytest.raises(ValueError):
        fab.limits("acme")


def test_narrow_tracks_sorted_against_the_fab():
    from cadmate import fab

    b = board(ic())
    lim = fab.limits("jlcpcb")
    neck = pb.Track((27.3, 18.1), (26.5, 18.1), 0.1124, "F.Cu", "+3V3")  # into U1.1
    thin = pb.Track((10, 10), (15, 10), 0.075, "F.Cu", "+3V3")
    fine = pb.Track((10, 12), (15, 12), 0.15, "F.Cu", "+3V3")
    out = fr.fab_check(b, [neck, thin, fine], lim)
    [w], [e] = out["warnings"], out["errors"]
    assert w["width_mm"] == 0.1124 and w["what"].startswith("neck into U1 pad 1")
    assert e["width_mm"] == 0.075 and e["what"] == "narrow between other copper"


def test_track_passing_over_a_pad_connects_it():
    b = board(ic(), cap("C1", 20, 17.6))
    b.tracks.append(pb.Track((19.2, 17.6), (28.5, 17.6), 0.5, "F.Cu", "+3V3"))  # grazes U1.1's top edge, ends outside it
    assert len(pb.NetGraph(b, "+3V3").islands()) == 1
    b.tracks[-1] = pb.Track((19.2, 17.6), (28.5, 17.6), 0.3, "F.Cu", "+3V3")  # 0.05 mm short of it
    assert len(pb.NetGraph(b, "+3V3").islands()) == 2


def test_neck_rules_only_for_necks_into_pads_and_replaced_on_rerun():
    from cadmate import fab

    warnings = [{"net": "/LC", "ref": "D17", "what": "neck into D17 pad GK"}, {"net": "+3.3V", "what": "narrow between other copper"}]
    block = fab.neck_rules(warnings, 0.1)
    assert block.count("(rule ") == 1 and "A.NetName == '/LC' && A.intersectsCourtyard('D17')" in block
    assert "(constraint track_width (min 0.1mm))" in block
    mine = '(version 1)\n(rule "keep" (constraint clearance (min 0.2mm)))\n'
    once = fab.merge_rules(mine, block)
    assert once.startswith(mine.rstrip()) and once.count("cadmate neck") == 1
    assert fab.merge_rules(once, block) == once
    assert "cadmate" not in fab.merge_rules(once, "") and '(rule "keep"' in fab.merge_rules(once, "")


def hole(ref, x, y, d=2.2):
    return footprint("MountingHole:M2", ref, "M2", x, y,
                     [f'(pad "" np_thru_hole circle (at 0 0) (size {d} {d}) (drill {d}) (layers "*.Cu" "*.Mask"))'])


def test_dsn_gets_edge_and_hole_clearance_keepouts():
    b = board(cap("C1", 25.2, 17.0), hole("H1", 40, 20))
    polys = fr.clearance_keepouts(b, 0.3, 0.25)
    assert len(polys) == 5  # four sides of the outline and the hole
    hx = [p for p in polys if len(p) == 8][0]
    assert all(abs(((x - 40) ** 2 + (y - 20) ** 2) ** 0.5) >= 1.35 - 1e-6 for x, y in hx)
    out = fr.dsn_keepouts(DSN, b, 0.3, 0.25)
    assert out.count("(keepout") == 10  # each polygon on both copper layers
    assert fr.dsn_keepouts(DSN, b) == DSN  # nothing asked, nothing added


def test_clearance_check_flags_copper_at_the_edge_and_holes():
    from cadmate import fab

    b = board(hole("H1", 40, 20))
    b.rules = {"min_copper_edge_clearance": 0.25, "min_hole_clearance": 0.25}
    lim = fab.limits("jlcpcb")
    at_edge = pb.Track((5, 0.15), (15, 0.15), 0.2, "F.Cu", "A")      # 0.05 mm from the edge
    near_edge = pb.Track((5, 0.42), (15, 0.42), 0.2, "F.Cu", "A")    # 0.32 mm: clear of both
    by_hole = pb.Track((30, 21.3), (50, 21.3), 0.2, "F.Cu", "B")     # 1.3 - 0.1 - 1.1 = 0.1 mm from the hole
    via = pb.Via(59.8, 20, 0.6, 0.3, ["F.Cu", "B.Cu"], "C")         # past the edge
    out = fr.clearance_check(b, [at_edge, near_edge, by_hole], [via], lim)
    nets = sorted(e["net"] for e in out["errors"])
    assert nets == ["A", "B", "C"] and not out["warnings"]
    assert out["errors"][0]["gap_mm"] < 0


def test_clearance_check_measures_a_slot_from_its_axis():
    b = board(footprint("LED:LED", "LED1", "LED", 40, 20,
                        ['(pad "" np_thru_hole oval (at 0 0) (size 1.8 2.4) (drill oval 1.8 2.4) (layers "*.Cu" "*.Mask"))']))
    b.rules = {"min_hole_clearance": 0.25}
    beside = pb.Track((41.4, 15), (41.4, 25), 0.2, "F.Cu", "A")   # along the long side: 1.4 - 0.1 - 0.9 = 0.4 mm
    past_end = pb.Track((35, 21.6), (45, 21.6), 0.2, "F.Cu", "B")  # past the end: 1.6 - 0.1 - 1.2 = 0.3 mm
    into_end = pb.Track((35, 21.4), (45, 21.4), 0.2, "F.Cu", "C")  # 1.4 - 0.1 - 1.2 = 0.1 mm
    out = fr.clearance_check(b, [beside, past_end, into_end], [], None)
    assert [e["net"] for e in out["errors"]] == ["C"] and abs(out["errors"][0]["gap_mm"] - 0.1) < 1e-3


def test_stub_vias_and_their_dead_end_tracks_are_pruned():
    b = board(footprint("R:R", "R1", "1k", 10, 10, [pad("1", 0, 0, "A"), pad("2", 1, 0, "B")]),
              footprint("R:R", "R2", "1k", 30, 10, [pad("1", 0, 0, "A", layer="B.Cu"), pad("2", 1, 0, "C", layer="B.Cu")],
                        layer="B.Cu"))
    a = pb.Track((10, 10), (20, 10), 0.2, "F.Cu", "A")
    b2 = pb.Track((20, 10), (30, 10), 0.2, "B.Cu", "A")
    keep = pb.Via(20, 10, 0.6, 0.3, ["F.Cu", "B.Cu"], "A")
    stub = pb.Track((10, 10), (10, 20), 0.2, "F.Cu", "A")            # goes to a via with nothing on B.Cu
    lone = pb.Via(10, 20, 0.6, 0.3, ["F.Cu", "B.Cu"], "A")
    assert fr.dangling_vias(b, [a, b2, stub], [keep, lone]) == [lone]
    tracks, vias, removed = fr.prune_stubs(b, [a, b2, stub], [keep, lone])
    assert vias == [keep] and tracks == [a, b2] and removed == {"vias": 1, "tracks": 1}


def test_no_ground_net_means_no_pour_item():
    b = board(footprint("R:R", "R1", "1k", 20, 20, [pad("1", -0.5, 0, "A"), pad("2", 0.5, 0, "B")]))
    assert "return_path" not in pc.classify(b)["counts"]


def four_layer(*items) -> pb.Board:
    head = HEAD.replace('(2 "B.Cu" signal)', '(4 "In1.Cu" power) (6 "In2.Cu" power) (2 "B.Cu" signal)')
    return pb.parse(head + "".join(items) + ")\n", "test.kicad_pcb")


def test_plane_fanout_puts_a_locked_via_at_each_plane_pad():
    b = four_layer(ic(), cap("C1", 22, 14), zone("GND", "In1.Cu", [[(0, 0), (60, 0), (60, 40), (0, 40)]]),
                   zone("+3V3", "In2.Cu", [[(0, 0), (60, 0), (60, 40), (0, 40)]]))
    assert rt.plane_nets(b) == {"GND": ["In1.Cu"], "+3V3": ["In2.Cu"]}
    fan = rt.plane_fanout(b)
    assert not fan["no_room"]
    pads = {f"{p.ref}.{p.number}" for p in b.pads if p.net in ("GND", "+3V3")}
    assert len(fan["vias"]) == len(pads) == 4
    assert all(t.locked for t in fan["tracks"]) and all(v.layers == ["F.Cu", "B.Cu"] for v in fan["vias"])
    for p in b.pads:
        if p.net in ("GND", "+3V3"):  # a track from the pad to its own via, close by
            [t] = [t for t in fan["tracks"] if p.contains(t.start)]
            assert t.net == p.net and any((v.x, v.y) == t.end and v.net == p.net for v in fan["vias"]) and t.length < 2
    _clear(b, fan["tracks"], fan["vias"])
    assert rt.plane_fanout(b)["vias"] == []  # pads already joined are left alone


def test_no_fanout_without_inner_planes():
    b = board(ic(), cap("C1", 22, 14), gnd_pour())
    assert rt.plane_nets(b) == {} and rt.plane_fanout(b)["vias"] == []


def test_zero_length_ses_tracks_are_dropped():
    b = board(ic())
    t = pb.Track((27.3, 18.1), (27.3005, 18.1), 0.25, "F.Cu", "+3V3")
    keep = pb.Track((27.3, 18.1), (25.0, 18.1), 0.25, "F.Cu", "+3V3")
    assert fr.new_items(b, [t, keep], [])[0] == [keep]


def test_rules_file_keeps_its_version_line_when_cadmate_has_nothing_to_add():
    from cadmate import fab

    assert fab.merge_rules("", "") == "(version 1)\n"


def test_router_keeps_out_of_keepout_areas_and_holds_hole_clearance():
    ko = ('\t(zone (net 0) (net_name "") (layers "F.Cu" "B.Cu") (name "hole") (keepout (tracks not_allowed) (vias not_allowed) '
          '(pads allowed) (copperpour not_allowed) (footprints allowed)) (polygon (pts (xy 10 10) (xy 12 10) (xy 12 12) (xy 10 12))))\n')
    npth = footprint("MountingHole:MountingHole_2.2mm", "H1", "", 40, 20, ['(pad "" np_thru_hole circle (at 0 0) (size 2.2 2.2) (drill 2.2) (layers "*.Cu" "*.Mask"))'])
    b = board(ko, npth)
    assert b.keepouts and {l for l, _ in b.keepouts} == {"F.Cu", "B.Cu"}
    obs = rt.Obstacles(b)
    assert obs.clear("F.Cu", (11, 11), "A", 0.1) == (False, "a keepout area")
    assert obs.clear("F.Cu", (12.2, 11), "A", 0.1)[0]  # beside it is fine: a keepout has no clearance of its own
    # 0.25 mm hole clearance (KiCad's default), not the 0.2 mm copper clearance
    assert not obs.clear("F.Cu", (40 + 1.1 + 0.1 + 0.22, 20), "A", 0.1)[0]
    assert obs.clear("F.Cu", (40 + 1.1 + 0.1 + 0.27, 20), "A", 0.1)[0]

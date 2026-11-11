import pytest
from test_pcb_review import HEAD, board, cap, footprint, ic, pad, zone

from cadmate import pcb_board as pb
from cadmate import pcb_critical as pc
from cadmate import pcb_flow
from cadmate import pcb_place as pl


def gnd_pour():
    return zone("GND", "B.Cu", [[(0, 0), (60, 0), (60, 40), (0, 40)]])


def crystal(x=8, y=30, rot=0):
    return footprint("Crystal:Crystal_SMD_3215-2Pin_3.2x1.5mm", "Y1", "12MHz", x, y,
                     [pad(1, -1.25, 0, "XI", 1, 1.8), pad(2, 1.25, 0, "XO", 1, 1.8)], rot=rot, court=((-2.1, -1.2), (2.1, 1.2)))


def load_cap(ref, x, y, net):
    return footprint("Capacitor_SMD:C_0402_1005Metric", ref, "18p", x, y, [pad(1, -0.5, 0, net, 0.5, 0.5), pad(2, 0.5, 0, "GND", 0.5, 0.5)],
                     court=((-0.95, -0.5), (0.95, 0.5)))


def pad_at(b, key):
    ref, num = key.split(".")
    return next(p for p in b.part(ref).copper_pads if p.number == num)


def applied(text, res):
    return pb.parse(pl.move_text(text, {m["ref"]: (m["to"]["x"], m["to"]["y"], m["to"]["rotation"]) for m in res["moves"]}))


def test_far_decap_goes_to_its_pin_power_pad_first():
    text = HEAD + ic() + cap("C1", 50, 35) + gnd_pour() + ")\n"
    b = pb.parse(text)
    res = pl.propose(b)
    [m] = res["moves"]
    assert m["ref"] == "C1" and m["for"] == "decap:U1.1" and m["distance_mm"]["before"] > 20
    after = applied(text, res)
    pp, gp, pin = pad_at(after, "C1.1"), pad_at(after, "C1.2"), pad_at(after, "U1.1")
    assert pb.dist((pp.x, pp.y), (pin.x, pin.y)) < 3.0
    assert gp.x < pp.x < pin.x  # VDD is on U1's left edge: the cap sits left of it, ground pad furthest out
    assert abs(pp.y - pin.y) < 0.01
    assert pl.overlaps(after, {"C1"}) == []
    [d] = [it for it in pc.classify(after)["items"] if it["class"] == "decap"]
    assert d["action"] == "route"


def test_close_decap_is_kept_and_everything_moves_it_anyway():
    b = board(ic(), cap("C1", 25.2, 18.1), gnd_pour())
    res = pl.propose(b)
    assert res["moves"] == [] and res["kept"][0]["for"] == "decap:U1.1"
    assert [m["ref"] for m in pl.propose(b, everything=True)["moves"]] == ["C1"]


def test_decap_slides_clear_of_a_part_already_there():
    blocker = footprint("Resistor_SMD:R_0603_1608Metric", "R9", "10k", 24.5, 18.1, [pad(1, -0.8, 0, "A"), pad(2, 0.8, 0, "B")],
                        court=((-1.5, -0.75), (1.5, 0.75)))
    text = HEAD + ic() + cap("C1", 50, 35) + blocker + gnd_pour() + ")\n"
    res = pl.propose(pb.parse(text))
    [m] = res["moves"]
    after = applied(text, res)
    assert pl.overlaps(after, {"C1"}) == []
    assert m["distance_mm"]["after"] < 5


def test_two_caps_on_one_pin_net_do_not_land_on_each_other():
    two = footprint("Package_SO:SOIC-8", "U1", "MCU", 30, 20, [
        pad(1, -2.7, -1.9, "+3V3", 1.5, 0.6, "power_in", "VDD"), pad(2, -2.7, -0.635, "GND", 1.5, 0.6, "power_in", "GND"),
        pad(8, 2.7, -1.9, "+3V3", 1.5, 0.6, "power_in", "VDDA")], court=((-3.7, -2.7), (3.7, 2.7)))
    text = HEAD + two + cap("C1", 50, 35) + cap("C2", 10, 35) + gnd_pour() + ")\n"
    res = pl.propose(pb.parse(text))
    assert sorted(m["ref"] for m in res["moves"]) == ["C1", "C2"]
    after = applied(text, res)
    assert pl.overlaps(after, {"C1", "C2"}) == []
    assert all(m["distance_mm"]["after"] < 3 for m in res["moves"])


def test_crystal_and_load_caps_go_to_the_mcu():
    text = HEAD + ic() + crystal(50, 35) + load_cap("C5", 5, 5, "XI") + load_cap("C6", 5, 10, "XO") + gnd_pour() + ")\n"
    res = pl.propose(pb.parse(text))
    assert [m["ref"] for m in res["moves"]] == ["Y1", "C5", "C6"]
    after = applied(text, res)
    for xp, pin in (("Y1.1", "U1.3"), ("Y1.2", "U1.4")):
        a, p = pad_at(after, xp), pad_at(after, pin)
        assert pb.dist((a.x, a.y), (p.x, p.y)) < 6
    assert pad_at(after, "Y1.1").y < pad_at(after, "Y1.2").y  # turned so XI faces XI
    for cp, xp in (("C5.1", "Y1.1"), ("C6.1", "Y1.2")):
        a, p = pad_at(after, cp), pad_at(after, xp)
        assert pb.dist((a.x, a.y), (p.x, p.y)) < 3
    assert pl.overlaps(after, {"Y1", "C5", "C6"}) == []


def test_power_pin_without_a_cap_cannot_be_placed():
    res = pl.propose(board(ic(), gnd_pour()))
    assert res["moves"] == [] and "add a cap" in res["cannot"][0]["why"]


def test_move_text_turns_pads_and_keeps_them_on_their_spots():
    text = HEAD + cap("C1", 10, 10) + ")\n"
    moved = pb.parse(pl.move_text(text, {"C1": (20, 15, 90)}))
    p1, p2 = moved.part("C1").copper_pads
    assert (round(p1.x, 3), round(p1.y, 3)) == (20, 15.8) and (round(p2.x, 3), round(p2.y, 3)) == (20, 14.2)
    assert p1.angle == 90 and moved.part("C1").rotation == 90
    back = pb.parse(pl.move_text(pl.move_text(text, {"C1": (20, 15, 90)}), {"C1": (10, 10, 0)}))
    assert back.part("C1").copper_pads[0].angle == 0


def test_apply_writes_a_new_board_and_refuses_stale_moves(tmp_path):
    src = tmp_path / "main.kicad_pcb"
    src.write_text(HEAD + ic() + cap("C1", 50, 35) + gnd_pour() + ")\n", encoding="utf-8")
    b, _ = pcb_flow.load(str(src))
    moves = pl.propose(b)["moves"]
    out = tmp_path / "placed.kicad_pcb"
    res = pl.apply(str(src), moves, out_path=str(out))
    assert res["moved"] == ["C1"] and res["courtyard_overlaps"] == []
    c = pb.parse(out.read_text(encoding="utf-8")).part("C1")
    assert (c.x, c.y, c.rotation) == (moves[0]["to"]["x"], moves[0]["to"]["y"], moves[0]["to"]["rotation"])
    with pytest.raises(pcb_flow.FlowError, match="moved since"):
        pl.apply(str(out), moves, out_path=str(tmp_path / "again.kicad_pcb"))
    with pytest.raises(pcb_flow.FlowError, match="no part"):
        pl.apply(str(src), [{"ref": "C99", "to": {"x": 1, "y": 1}}], out_path=str(out))

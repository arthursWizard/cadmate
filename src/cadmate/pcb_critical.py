"""Which connections on a board are critical, and the rule each one follows, before anything is routed.

The flow this serves: classify the critical connections, show them to the user to confirm or override, route those
first (cadmate's router or the user), lock just those segments, let Freerouting route the rest, then review.

Each class is a detector (a function that finds its items on the board) registered in CLASSES with the rule it applies
and the sources the rule comes from. Adding a class is adding one detector. An item names the parts and nets involved,
the connections to route (pad to pad, or pad to a ground via), the numbers the router and the review use, what to do
with it (route, set a width for the autorouter, fix the placement first, route by hand, or check after routing), and
why cadmate thinks it belongs to the class. Numbers that no source sets are marked as cadmate heuristics.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .pcb_board import Board, Pad, Part, dist
from .pcb_review import (_CAP_REF, _IND_REF, SOURCES as REVIEW_SOURCES, _farads, _fmt_f, _is_crystal, _is_ic, diff_pairs, infer_roles,
                         ipc2221_width)

SOURCES = {
    **{k: REVIEW_SOURCES[k] for k in ("mt101", "an2867", "an1149", "ipc2221", "return_path", "heuristic")},
    "spraar7": "TI SPRAAR7J 'High-Speed Interface Layout Guidelines' (ti.com/lit/an/spraar7j/spraar7j.pdf). Table A-1: USB 2.0 DP/DM "
               "differential impedance 81/90/99 Ω, 'Skew within any USB2.0 differential pair 50 Mils', at most 4 vias per USB 2.0 "
               "trace, 0 stubs, 50 mil to clock or periodic signals. Table A-2: PCIe and HDMI TMDS 90/100/110 Ω, PCIe 0 vias. §2.4: "
               "route over a solid GND reference, not across a split or void, same reference from end to end. §3.1: 30 mil keep-out "
               "to other signals.",
    "slva680": "TI SLVA680A 'ESD Protection Layout Guide' (ti.com/lit/an/slva680a/slva680a.pdf). §2.1: 'Place the TVS as near to the "
               "connector as design rules allow'; the protected line runs from the connector straight to the TVS pin, 'ideally with "
               "no VIAs', and a branch off to the TVS is a stub to avoid. §2.4: ground pin to the ground plane 'with one VIA "
               "immediately adjacent to the ground pin'.",
    "an139": "Analog Devices AN-139 'Power Supply Layout and EMI' (analog.com/en/resources/app-notes/an-139.html): the hot loop, input "
             "capacitor and switches, carries the fully switched current; keep its area and inductance as small as possible, use "
             "small low-ESL input caps right at the switches, and a solid plane close under the loop.",
    "mt031": "Analog Devices MT-031 Rev. A 'Grounding Data Converters' (analog.com/media/en/training-seminars/tutorials/MT-031.pdf): "
             "'both should be kept away from digital signals'; references and amplifiers 'referenced and decoupled to the analog "
             "ground plane'.",
    "espressif_rf": "Espressif ESP Hardware Design Guidelines, PCB Layout Design (docs.espressif.com/projects/esp-hardware-design-"
                    "guidelines/en/latest/esp32/pcb-layout-design.html): RF trace 50 Ω, 'as short as possible with dense ground vias "
                    "around', on the outer layer without vias, no high-frequency traces close by, complete ground on the adjacent "
                    "layer, matching parts close to the pin.",
    "datasheet": "The part's datasheet: reference resistors, strap and timing components, and soft-start/slew caps have placement "
                 "notes there. cadmate only knows to keep them at the pin.",
}

# Numbers no source sets (SOURCES["heuristic"]), on top of pcb_review.LIMITS.
LIMITS = {
    "decap_distance": 3.0,  # mm: a bypass cap further than this from its pin is a placement problem, not a routing one
    "decap_ground_direct": 2.5,  # mm: cap ground pad this close to the IC's ground pin gets a track; further gets a via to the pour
    "crystal_distance": 10.0,  # mm crystal pad to IC pin
    "local_distance": 5.0,  # mm: strap, reference and feedback parts further than this from their pin
    "tvs_distance": 10.0,  # mm from the connector pad to the TVS pad
    "pair_mismatch_default": 2.0,  # mm, pairs without a published number
    "pair_vias": 2,  # vias per trace where no source sets it
    "delta_t": 10.0,  # °C rise for IPC-2221
}

MIL = 0.0254


@dataclass
class Cls:
    name: str
    label: str
    priority: int  # routing order: lower goes first
    rule: str
    sources: list[str]
    detect: Callable | None = None


CLASSES: dict[str, Cls] = {}


def detector(name: str, label: str, priority: int, rule: str, sources: list[str]):
    """Register a class. The function takes a Ctx and returns items (dicts from Ctx.item)."""

    def wrap(fn):
        CLASSES[name] = Cls(name, label, priority, rule, sources, fn)
        return fn

    return wrap


# --- context ------------------------------------------------------------------------------------------


def pad_key(p: Pad) -> str:
    return f"{p.ref}.{p.number}"


def _end(p: Pad) -> dict:
    return {"pad": pad_key(p), "x": round(p.x, 4), "y": round(p.y, 4)}


@dataclass
class Ctx:
    b: Board
    roles: dict[str, dict]
    currents: dict[str, float]
    lim: dict
    used: set[str] = field(default_factory=set)  # pads already in an item's route
    items: list[dict] = field(default_factory=list)

    def role(self, net: str | None) -> str:
        return self.roles.get(net, {}).get("role", "signal") if net else "none"

    @property
    def ground_nets(self) -> set[str]:
        return {n for n, v in self.roles.items() if v["role"] == "ground"}

    def ground_pour_layers(self, net: str) -> list[str]:
        return self.b.zone_nets.get(net, [])

    def width(self, net: str) -> float:
        nc = self.b.net_class(net)
        return (nc.track_width if nc and nc.track_width else None) or 0.2

    def ics(self) -> list[Part]:
        return [p for p in self.b.parts if _is_ic(p) and not _is_crystal(p)]

    def caps(self) -> list[tuple[Part, Pad, Pad, float | None]]:
        """(cap, pad on the other net, ground pad, farads) for two-pad caps with one side on ground."""
        out = []
        for part in self.b.parts:
            pads = [p for p in part.copper_pads if p.net]
            if not _CAP_REF.match(part.ref) or len(pads) != 2:
                continue
            g = [p for p in pads if self.role(p.net) == "ground"]
            if len(g) == 1:
                o = pads[0] if pads[1] is g[0] else pads[1]
                out.append((part, o, g[0], _farads(part.value)))
        return out

    def ground_end(self, gp: Pad, near: Pad | None = None) -> dict:
        """Where a critical ground pad connects: straight to a nearby ground pin, otherwise a via into the pour."""
        if near is not None and dist((gp.x, gp.y), (near.x, near.y)) <= self.lim["decap_ground_direct"]:
            return _end(near)
        return {"via": gp.net, "near": pad_key(gp), "pour_layers": self.ground_pour_layers(gp.net)}

    def item(self, cls: str, key: str, title: str, *, refs: list[str], nets: list[str], connections: list[dict] | None = None,
             action: str = "route", why: str, rule: dict | None = None, placement: list[str] | None = None, confidence: str = "high",
             sources: list[str] | None = None, notes: list[str] | None = None) -> dict:
        c = CLASSES[cls]
        it = {"id": f"{cls}:{key}", "class": cls, "title": title, "refs": refs, "nets": nets, "action": action,
              "connections": connections or [], "rule": rule or {}, "rule_text": c.rule, "sources": sources or c.sources,
              "why": why, "confidence": confidence, "placement": placement or [], "notes": notes or []}
        if placement and action == "route":
            it["action"] = "place"
        self.items.append(it)
        return it


def _conn(net: str, a: Pad, b: Pad | dict, width: float, max_vias: int | None = None, **kw) -> dict:
    out = {"net": net, "a": _end(a), "b": b if isinstance(b, dict) else _end(b), "width": round(width, 3)}
    if max_vias is not None:
        out["max_vias"] = max_vias
    return out | kw


def _nearest(p: Pad, pads: list[Pad]) -> Pad | None:
    return min(pads, key=lambda q: dist((p.x, p.y), (q.x, q.y)), default=None)


def _d(a: Pad, b: Pad) -> float:
    return dist((a.x, a.y), (b.x, b.y))


def _gap(a: Pad, b: Pad) -> float:
    """Edge to edge, roughly: centre distance less the pads' half sizes along the line."""
    return max(0.0, _d(a, b) - min(a.w, a.h) / 2 - min(b.w, b.h) / 2)


_POWER_FN = re.compile(r"^(P?VIN|VCC\w*|VDD\w*|AVDD\w*|DVDD\w*|AVCC|VBAT|VIO|VDDIO\w*|VCORE|VREG\w*|V\d+V\d*|IN)$", re.I)


def _power_pins(ctx: Ctx, ic: Part) -> list[Pad]:
    return [p for p in ic.copper_pads if p.net and ctx.role(p.net) == "power"
            and ((p.pintype or "").startswith("power_in") or (p.pintype is None and _POWER_FN.match(p.pinfunction or "")))]


# --- the classes ----------------------------------------------------------------------------------------


@detector("hot_loop", "Switching regulator hot loop", 1,
          "The loop input cap → high-side switch → low-side switch → ground → input cap carries the full switched current: input "
          "cap at the VIN and PGND pins on the same layer, no vias in the loop, SW node copper compact, inductor "
          "right at SW, output cap at the inductor, solid ground plane under it. Feedback runs away from the inductor and SW.",
          ["an139", "an1149"])
def _hot_loops(ctx: Ctx) -> None:
    sw_nets = [n for n, v in ctx.roles.items() if v["role"] == "switching"]
    caps = ctx.caps()
    for sw in sw_nets:
        ics = [p for p in ctx.ics() if any(q.net == sw for q in p.copper_pads)]
        inds = [p for p in ctx.b.parts if _IND_REF.match(p.ref) and any(q.net == sw for q in p.copper_pads)]
        if not ics:
            continue
        ic = ics[0]
        sw_pin = next(q for q in ic.copper_pads if q.net == sw)
        conns, refs, place, notes = [], [ic.ref], [], []
        w_sw = max(ctx.width(sw), 0.4)
        if inds:
            ind = min(inds, key=lambda l: _d(next(q for q in l.copper_pads if q.net == sw), sw_pin))
            lp = next(q for q in ind.copper_pads if q.net == sw)
            refs.append(ind.ref)
            conns.append(_conn(sw, sw_pin, lp, w_sw, 0))
            if _d(lp, sw_pin) > 6:
                place.append(f"{ind.ref} is {_d(lp, sw_pin):.1f} mm from {ic.ref}'s SW pin; put the inductor right at it")
            out_pad = next((q for q in ind.copper_pads if q.net and q.net != sw), None)
            if out_pad:
                cout = [c for c in caps if c[1].net == out_pad.net]
                best = min(cout, key=lambda c: _d(c[1], out_pad), default=None)
                if best:
                    refs.append(best[0].ref)
                    conns.append(_conn(out_pad.net, out_pad, best[1], max(ctx.width(out_pad.net), 0.4), 0))
                    conns.append(_conn(best[2].net, best[2], ctx.ground_end(best[2]), max(ctx.width(best[2].net), 0.4)))
        vin_pins = [q for q in ic.copper_pads if q.net and ctx.role(q.net) == "power" and re.match(r"^(P?VIN\d*|VCC|IN|PVDD)$", q.pinfunction or "", re.I)]
        gnd_pins = [q for q in ic.copper_pads if q.net and ctx.role(q.net) == "ground"]
        pgnd = [q for q in gnd_pins if re.match(r"^(PGND|GND|PAD|EP)", q.pinfunction or "", re.I)] or gnd_pins
        for vp in vin_pins[:1]:
            cin = [c for c in caps if c[1].net == vp.net]
            best = min(cin, key=lambda c: _d(c[1], vp), default=None)
            if best is None:
                place.append(f"no input cap on {vp.net} near {ic.ref}")
                continue
            refs.append(best[0].ref)
            conns.append(_conn(vp.net, best[1], vp, max(ctx.width(vp.net), 0.4), 0))
            gpin = _nearest(best[2], pgnd)
            conns.append(_conn(best[2].net, best[2], _end(gpin) if gpin else ctx.ground_end(best[2]), max(ctx.width(best[2].net), 0.4), 0))
            if _d(best[1], vp) > 3:
                place.append(f"input cap {best[0].ref} is {_d(best[1], vp):.1f} mm from {vp.name}; AN-1149: 'as close to the VIN pin of the IC as possible'")
        bst = [q for q in ic.copper_pads if re.match(r"^(BST|BOOT|CB|BS)$", q.pinfunction or "", re.I) and q.net]
        for bp in bst:
            bc = next((c for c in ctx.b.parts if _CAP_REF.match(c.ref) and {q.net for q in c.copper_pads} == {bp.net, sw}), None)
            if bc:
                refs.append(bc.ref)
                conns.append(_conn(bp.net, next(q for q in bc.copper_pads if q.net == bp.net), bp, ctx.width(bp.net), 0))
                notes.append(f"bootstrap cap {bc.ref} between {bp.pinfunction} and SW")
        for c in conns:
            ctx.used.add(c["a"]["pad"])
        ctx.item("hot_loop", sw, f"{ic.ref} switching loop on {sw}", refs=refs, nets=sorted({c["net"] for c in conns} | {sw}),
                 connections=conns, why=ctx.roles[sw]["why"], rule={"max_vias": 0, "min_width_mm": 0.4, "same_layer": True},
                 placement=place, notes=notes)


@detector("decap", "Decoupling cap to pin", 2,
          "Each IC power pin gets its nearest small bypass cap (≤ 1 µF). Cap power pad straight to the pin on the same layer, as "
          "short and wide as the pin allows, no via between them; the cap's ground pad straight to the IC's ground pin when it is "
          "right there, otherwise one via into the ground pour at the pad. Bulk caps (> 1 µF) are left to the pours.",
          ["mt101"])
def _decaps(ctx: Ctx) -> None:
    caps = [c for c in ctx.caps() if ctx.role(c[1].net) == "power" and (c[3] is None or c[3] <= 1.01e-6)]
    taken: set[str] = set()
    pins = [(ic, p) for ic in ctx.ics() for p in _power_pins(ctx, ic)]
    cand = sorted(((_gap(c[1], p), ic, p, c) for ic, p in pins for c in caps if c[1].net == p.net), key=lambda t: t[0])
    done: set[int] = set()
    for gap, ic, pin, (cap, pp, gp, farads) in cand:
        if cap.ref in taken or id(pin) in done or pad_key(pp) in ctx.used:
            continue
        if _d(pp, pin) > ctx.lim["decap_distance"] * 3:
            continue
        taken.add(cap.ref)
        done.add(id(pin))
        # the other pins on the same net this cap also sits next to share it
        w = max(ctx.width(pin.net), min(0.5, min(pin.w, pin.h), min(pp.w, pp.h)))
        gnd_pins = [q for q in ic.copper_pads if q.net == gp.net]
        conns = [_conn(pin.net, pp, pin, w, 0), _conn(gp.net, gp, ctx.ground_end(gp, _nearest(gp, gnd_pins)), max(ctx.width(gp.net), min(0.5, gp.w, gp.h)))]
        place = []
        if _d(pp, pin) > ctx.lim["decap_distance"]:
            place.append(f"{cap.ref} is {_d(pp, pin):.1f} mm from {pin.name} (limit {ctx.lim['decap_distance']:g} mm); move it to the pin first")
        ctx.used.add(pad_key(pp))
        ctx.item("decap", f"{ic.ref}.{pin.number}", f"{cap.ref} ({_fmt_f(farads)}) to {ic.ref} pin {pin.number} {pin.pinfunction or ''} ({pin.net})".replace("  ", " "),
                 refs=[cap.ref, ic.ref], nets=[pin.net, gp.net], connections=conns,
                 why=f"{cap.ref} is the nearest small cap on {pin.net} to this power pin, {_d(pp, pin):.1f} mm away",
                 rule={"max_vias": 0, "max_length_mm": round(max(2 * _d(pp, pin), _d(pp, pin) + 1.0), 2), "width_mm": round(w, 3)},
                 placement=place)
    for ic, pin in pins:
        if id(pin) not in done:
            near = min((_d(c[1], pin) for c in caps if c[1].net == pin.net), default=None)
            if near is None or near > ctx.lim["decap_distance"] * 3:
                ctx.item("decap", f"{ic.ref}.{pin.number}", f"{ic.ref} pin {pin.number} {pin.pinfunction or ''} ({pin.net}) has no bypass cap near it",
                         refs=[ic.ref], nets=[pin.net], action="place", confidence="medium",
                         why="power input pin with no cap ≤ 1 µF on its net within "
                             f"{ctx.lim['decap_distance'] * 3:g} mm" + (f" (nearest {near:.1f} mm)" if near else ""),
                         placement=["add or move a cap to this pin, unless the datasheet lets it share one"], sources=["mt101", "heuristic"])


@detector("crystal", "Crystal or oscillator", 3,
          "Crystal right at the MCU, XI/XO short and on one layer with no vias, load caps next to the crystal pads, nothing else "
          "routed alongside or underneath, solid ground under the crystal area, crystal and load-cap grounds straight to the pour.",
          ["an2867"])
def _crystals(ctx: Ctx) -> None:
    caps = ctx.caps()
    for x in ctx.b.parts:
        if not _is_crystal(x):
            continue
        sig = [p for p in x.copper_pads if p.net and ctx.role(p.net) not in ("ground", "power")]
        if not sig:
            continue
        conns, refs, place = [], [x.ref], []
        for p in sig:
            ic_pins = [q for ic in ctx.ics() for q in ic.copper_pads if q.net == p.net]
            pin = _nearest(p, ic_pins)
            if pin:
                refs.append(pin.ref)
                conns.append(_conn(p.net, p, pin, ctx.width(p.net), 0))
                if _d(p, pin) > ctx.lim["crystal_distance"]:
                    place.append(f"{x.ref} pad {p.number} is {_d(p, pin):.1f} mm from {pin.name}; AN2867: 'as close as possible'")
            for c, cp, gp, f in caps:
                if cp.net == p.net:
                    refs.append(c.ref)
                    conns.append(_conn(p.net, cp, p, ctx.width(p.net), 0))
                    conns.append(_conn(gp.net, gp, ctx.ground_end(gp), ctx.width(gp.net)))
                    ctx.used.add(pad_key(cp))
        for g in x.copper_pads:
            if g.net and ctx.role(g.net) == "ground":
                conns.append(_conn(g.net, g, ctx.ground_end(g), ctx.width(g.net)))
        osc = any(ctx.role(p.net) == "power" for p in x.copper_pads if p.net)
        ctx.item("crystal", x.ref, f"{x.ref} {x.value} ({'oscillator' if osc else 'crystal'}) to {', '.join(sorted(set(refs) - {x.ref}))}",
                 refs=sorted(set(refs)), nets=sorted({c["net"] for c in conns}), connections=conns,
                 why=f"{x.ref} looks like a {'oscillator' if osc else 'crystal'} (reference, footprint or value)",
                 rule={"max_vias": 0, "max_length_mm": ctx.lim["crystal_distance"], "keepout_mm": 0.5}, placement=place)


_PAIR_KINDS = [  # (pattern on the pair's base name, kind, impedance Ω, tolerance, max mismatch mm, max vias per trace, source)
    (r"USB|^D$|^U\d*D$", "USB 2.0", 90, "81-99 Ω", 50 * MIL, 4, "spraar7"),
    (r"HDMI|TMDS", "HDMI TMDS", 100, "90-110 Ω", None, None, "spraar7"),
    (r"PCIE|PCI|SATA", "PCIe/SATA", 100, "90-110 Ω", 5 * MIL, 0, "spraar7"),
    (r"ETH|MDI|TX|RX|TD|RD", "Ethernet/serial pair", 100, None, None, None, "heuristic"),
    (r"LVDS|CLK", "LVDS/clock", 100, None, None, None, "heuristic"),
    (r"CAN", "CAN", 120, None, None, None, "heuristic"),
]


def _pair_kind(base: str) -> tuple:
    for pat, *rest in _PAIR_KINDS:
        if re.search(pat, base, re.I):
            return tuple(rest)
    return ("differential pair", 100, None, None, None, "heuristic")


def _chain(ctx: Ctx, net: str) -> list[Pad]:
    """The net's pads in route order: from the connector (or the pad furthest from the rest) by nearest neighbour."""
    pads = [p for p in ctx.b.pads if p.net == net]
    if len(pads) < 2:
        return pads
    conn = [p for p in pads if re.match(r"^(J|P|CN|USB|X)\d", p.ref) and not _is_crystal(ctx.b.part(p.ref))]
    start = conn[0] if conn else max(pads, key=lambda p: sum(_d(p, q) for q in pads))
    out, left = [start], [p for p in pads if p is not start]
    while left:
        nxt = _nearest(out[-1], left)
        out.append(nxt)
        left.remove(nxt)
    return out


@detector("diff_pair", "Differential pair", 4,
          "Both traces routed together at constant width and gap for the pair's impedance, matched in length, same layer and same "
          "ground reference end to end (no plane splits under it), as few vias as possible and placed symmetrically, no stubs "
          "(route through ESD and connector pads, not past them), kept clear of other signals.",
          ["spraar7", "return_path"])
def _pairs(ctx: Ctx) -> None:
    for pos, neg in diff_pairs(ctx.b.nets):
        base = re.sub(r"(\+|-|_P|_N|P|N|DP|DM)$", "", pos.split("/")[-1])
        kind, z, tol, mismatch, vias, src = _pair_kind(base or pos.split("/")[-1])
        cp, cn = _chain(ctx, pos), _chain(ctx, neg)
        if len(cp) < 2 or len(cn) < 2:
            continue
        nc = ctx.b.net_class(pos)
        w = (nc.diff_pair_width if nc and nc.diff_pair_width else None) or ctx.width(pos)
        gap = nc.diff_pair_gap if nc and nc.diff_pair_gap else None
        conns = []
        for net, chain in ((pos, cp), (neg, cn)):
            for a, b in zip(chain, chain[1:]):
                conns.append(_conn(net, a, b, w, vias if vias is not None else ctx.lim["pair_vias"], pair=pos))
                ctx.used.add(pad_key(a))
        pr = {"impedance_ohm": z, "impedance_range": tol, "max_mismatch_mm": round(mismatch if mismatch is not None else ctx.lim["pair_mismatch_default"], 3),
              "max_vias_per_trace": vias if vias is not None else ctx.lim["pair_vias"], "width_mm": w, "gap_mm": gap,
              "keepout_mm": round(30 * MIL, 2) if src == "spraar7" else None}
        notes = [f"{kind}: impedance target {z} Ω" + (f" ({tol})" if tol else "") + ". cadmate doesn't compute width and gap from "
                 "the stackup; take them from the fab's impedance calculator or KiCad's calculator and set them in the net class."]
        if src != "spraar7":
            notes.append("mismatch and via limits here are cadmate heuristics; the interface's own spec or the chip's layout guide wins")
        refs = sorted({p.ref for p in cp + cn})
        ctx.item("diff_pair", pos, f"{kind} pair {pos} / {neg}: {' → '.join(dict.fromkeys(p.ref for p in cp))}", refs=refs, nets=[pos, neg],
                 connections=conns, why=f"nets named as a pair ({pos.split('/')[-1]} / {neg.split('/')[-1]})", rule=pr,
                 sources=[src] + ([] if src == "spraar7" else ["spraar7"]) + ["return_path"], notes=notes)


_CONNECTOR = re.compile(r"^(J|P|CN|USB|CON)\d")
_TVS = re.compile(r"TVS|ESD|PESD|USBLC|TPD\d|SP0\d|PRTR|IP4|RCLAMP|SMAJ|SMBJ|SMF", re.I)


@detector("esd", "ESD/TVS protection", 5,
          "TVS as near to the connector as the rules allow; the protected line runs from the connector into the TVS pad and on to "
          "the IC (no branch off to the TVS, no via before it); TVS ground pin to the ground plane with one via right at the pin.",
          ["slva680"])
def _esd(ctx: Ctx) -> None:
    groups: dict[str, list[tuple[Part, list[Pad], list[Pad]]]] = {}
    for part in ctx.b.parts:
        if not (_TVS.search(part.value) or _TVS.search(part.lib) or _TVS.search(part.description)):
            continue
        gnd = [p for p in part.copper_pads if p.net and ctx.role(p.net) == "ground"]
        sig = [p for p in part.copper_pads if p.net and ctx.role(p.net) != "ground"]
        if not gnd or not sig:
            continue
        jp = [q for s in sig for q in ctx.b.pads if q.net == s.net and _CONNECTOR.match(q.ref)]
        j = _nearest(sig[0], jp)
        groups.setdefault(j.ref if j else part.ref, []).append((part, gnd, sig))
    for key, members in sorted(groups.items()):
        conns, place, notes = [], [], []
        for part, gnd, sig in members:
            g = gnd[0]
            conns.append(_conn(g.net, g, {"via": g.net, "near": pad_key(g), "pour_layers": ctx.ground_pour_layers(g.net)}, max(ctx.width(g.net), 0.3)))
            for s in sig:
                jp = [q for q in ctx.b.pads if q.net == s.net and _CONNECTOR.match(q.ref)]
                j = _nearest(s, jp)
                if j and _d(s, j) > ctx.lim["tvs_distance"]:
                    place.append(f"{part.ref} is {_d(s, j):.1f} mm from connector {j.ref}; SLVA680: as near to the connector as the rules allow")
                if any(it["class"] == "diff_pair" and s.net in it["nets"] for it in ctx.items):
                    continue
                chain = _chain(ctx, s.net)
                for a, b in zip(chain, chain[1:]):
                    conns.append(_conn(s.net, a, b, ctx.width(s.net), 0 if part.ref in (a.ref, b.ref) else None))
        sig_nets = sorted({s.net for _, _, sig in members for s in sig})
        if any(it["class"] == "diff_pair" and set(sig_nets) & set(it["nets"]) for it in ctx.items):
            notes.append("the protected lines are routed with their pair, through the TVS pads")
        refs = [m[0].ref for m in members]
        ctx.item("esd", key, f"{', '.join(refs)} protecting {', '.join(sig_nets)}" + (f" at {key}" if key not in refs else ""),
                 refs=refs + ([key] if key not in refs else []), nets=sorted(set(sig_nets) | {m[1][0].net for m in members}), connections=conns,
                 why="TVS/ESD parts by value, footprint or description, with a pad on ground", rule={"ground_via": "at the pin", "max_vias_before_tvs": 0},
                 placement=place, notes=notes)


_SENSE_FN = re.compile(r"(?:^|[_-])(V?FB|FEEDBACK|ADC\d*|AIN\d*|A\d+|VREF\w*|REF\w*|REXT|RSET|ISET|IREF|SENSE\w*|ISNS\w*|CS[PN]?|COMP|TEMP|NTC)(?:$|[_-])", re.I)


@detector("sensitive", "Sensitive analog", 6,
          "Reference, feedback and ADC parts at the pin they serve, short tracks, kept away from switching nodes, inductors, clocks "
          "and other fast signals, referenced to solid ground. A feedback divider's top takes the output where it is regulated "
          "(at the output cap), and the divider sits at the FB pin.",
          ["mt031", "an1149", "datasheet"])
def _sensitive(ctx: Ctx) -> None:
    for net in ctx.b.nets:
        if ctx.role(net) in ("ground", "power", "switching", "crystal"):
            continue
        pads = [p for p in ctx.b.pads if p.net == net]
        ic_pins = [p for p in pads if _is_ic(ctx.b.part(p.ref))]
        fn = next((p.pinfunction for p in ic_pins if p.pinfunction and _SENSE_FN.search(p.pinfunction)), None)
        if not (ctx.role(net) == "sensitive" or fn):
            continue
        if not ic_pins:
            continue
        pin = ic_pins[0]
        conns, place = [], []
        for p in pads:
            if p is pin or pad_key(p) in ctx.used or _is_ic(ctx.b.part(p.ref)):
                continue
            conns.append(_conn(net, p, pin, ctx.width(net), 1))
            if _d(p, pin) > ctx.lim["local_distance"]:
                place.append(f"{p.ref} is {_d(p, pin):.1f} mm from {pin.name}; keep it at the pin")
        noisy = [n for n, v in ctx.roles.items() if v["role"] in ("switching",)]
        ctx.item("sensitive", net, f"{net} ({fn or ctx.roles[net]['why']}) at {pin.name}", refs=sorted({p.ref for p in pads}), nets=[net],
                 connections=conns, why=f"pin function {fn}" if fn else ctx.roles[net]["why"],
                 rule={"max_vias": 1, "keep_away_from": noisy or ["switching nodes", "clocks", "USB"], "keepout_mm": 0.5},
                 placement=place, confidence="high" if fn else "medium")
        for c in conns:
            ctx.used.add(c["a"]["pad"])


_STRAP_FN = re.compile(r"(RST|RESET|NRST|EN|ENABLE|BOOT\w*|TEST\w*|MODE\w*|STRAP\w*|CFG\w*|CONF\w*|SEL\w*|ADDR\w*|SS|TR|CAP|RT|ILIM|PG|PGOOD|WP|HOLD|ON)#?$", re.I)


@detector("strap", "Reset, boot and strap nets", 7,
          "A pin with only local parts on it (pull-up/down, filter or timing cap, slew cap): the parts sit at the pin and the track "
          "is short and direct. Reset and enable lines pick up noise when long; a reset filter cap belongs at the pin.",
          ["datasheet", "heuristic"])
def _straps(ctx: Ctx) -> None:
    for net in ctx.b.nets:
        if ctx.role(net) in ("ground", "power", "switching", "crystal"):
            continue
        pads = [p for p in ctx.b.pads if p.net == net]
        if len(pads) < 2 or len(pads) > 4 or any(pad_key(p) in ctx.used for p in pads):
            continue
        ic_pins = [p for p in pads if _is_ic(ctx.b.part(p.ref))]
        others = [p for p in pads if p not in ic_pins]
        if len(ic_pins) != 1 or not others:
            continue
        # every other part on the net is a two-pad passive whose far side goes to a supply or ground
        local = True
        for p in others:
            part = ctx.b.part(p.ref)
            far = [q for q in part.copper_pads if q is not p and q.net]
            if not (re.match(r"^(R|C|D|JP|SW)\d", part.ref) and len(part.copper_pads) == 2 and far and ctx.role(far[0].net) in ("power", "ground")):
                local = False
        pin = ic_pins[0]
        named = bool(_STRAP_FN.search((pin.pinfunction or "").replace("#", "")) or _STRAP_FN.search(net.split("/")[-1].strip(")").split("-")[-1]))
        if not local and not named:
            continue
        if not local:
            continue
        conns, place = [], []
        for p in others:
            conns.append(_conn(net, p, pin, ctx.width(net), 1))
            if _d(p, pin) > ctx.lim["local_distance"]:
                place.append(f"{p.ref} is {_d(p, pin):.1f} mm from {pin.name}")
            ctx.used.add(pad_key(p))
        ctx.item("strap", net, f"{pin.name} with {', '.join(p.ref for p in others)}", refs=sorted({p.ref for p in pads}), nets=[net],
                 connections=conns, why="one IC pin, and only pull, filter or timing parts to a supply or ground" + (" (named like a strap)" if named else ""),
                 rule={"max_length_mm": ctx.lim["local_distance"]}, placement=place, confidence="high" if named else "medium")


_RF_NET = re.compile(r"(?:^|[/_-])(ANT\w*|RF\w*|LNA\w*|PA_?OUT)(?:$|[_-])", re.I)
_RF_LIB = re.compile(r"(?:^|[:_ -])(U\.?FL|SMA|SMB|MMCX|IPEX|MHF\d?)(?:[_ -]|$)|Antenna|Coax", re.I)


@detector("rf", "RF and antenna feed", 0,
          "50 Ω line (coplanar waveguide with ground on an outer layer), as short as possible, no vias or layer changes, dense "
          "ground vias along both sides, nothing else routed nearby, complete ground on the next layer, matching parts at the pin "
          "or at the antenna as the datasheet says.",
          ["espressif_rf"])
def _rf(ctx: Ctx) -> None:
    for net in ctx.b.nets:
        pads = [p for p in ctx.b.pads if p.net == net]
        lib_hit = [p for p in pads if _RF_LIB.search(ctx.b.part(p.ref).lib)]
        fn_hit = [p for p in pads if p.pinfunction and _RF_NET.search(p.pinfunction)]
        if ctx.role(net) in ("ground", "power") or not (_RF_NET.search(net.split("/")[-1]) or lib_hit or fn_hit):
            continue
        chain = _chain(ctx, net)
        conns = [_conn(net, a, b, ctx.width(net), 0) for a, b in zip(chain, chain[1:])]
        ctx.item("rf", net, f"RF line {net}: {' → '.join(dict.fromkeys(p.ref for p in chain))}", refs=sorted({p.ref for p in pads}), nets=[net],
                 connections=conns, action="hand", why="net name, pin function or an RF connector/antenna footprint",
                 rule={"impedance_ohm": 50, "max_vias": 0, "ground_stitching": True},
                 notes=["width and gap for 50 Ω come from the stackup; cadmate doesn't compute them, so this one is routed by hand "
                        "(or with KiCad's calculator values put in a net class first)"])


@detector("high_current", "High-current path", 8,
          "Width from the current: IPC-2221 for the temperature rise allowed (outer layers carry more than inner). Short and "
          "direct, several vias where it changes layer. The autorouter gets the width through the net class; nothing is locked, "
          "and pours can carry it instead.",
          ["ipc2221"])
def _high_current(ctx: Ctx) -> None:
    t_out = ctx.b.copper_thickness.get(ctx.b.copper_layers[0], 0.035) if ctx.b.copper_layers else 0.035
    unknown = []
    for net, v in sorted(ctx.roles.items()):
        if v["role"] != "power" and net not in ctx.currents:
            continue
        amps = ctx.currents.get(net)
        cls = ctx.width(net)
        if amps is None:
            unknown.append((net, cls))
            continue
        w_out = ipc2221_width(amps, ctx.lim["delta_t"], t_out, True)
        w_in = ipc2221_width(amps, ctx.lim["delta_t"], t_out, False)
        ctx.item("high_current", net, f"{net} at {amps:g} A: {w_out:.2f} mm outer / {w_in:.2f} mm inner", refs=[], nets=[net],
                 action="netclass" if w_out > cls + 1e-3 else "check", why=f"current {amps:g} A given",
                 rule={"amps": amps, "width_outer_mm": round(w_out, 3), "width_inner_mm": round(w_in, 3), "delta_t": ctx.lim["delta_t"],
                       "class_width_mm": cls},
                 notes=[] if w_out > cls + 1e-3 else [f"the net class width {cls} mm already covers it"])
    if unknown:
        caps = "; ".join(f"{n} {w} mm carries {_ipc_amps(w, ctx.lim['delta_t'], t_out):.2f} A" for n, w in unknown)
        ctx.item("high_current", "unknown", f"Power nets with no current given: {', '.join(n for n, _ in unknown)}", refs=[],
                 nets=[n for n, _ in unknown], action="ask", confidence="low", why="power nets (names, power symbols or pin types)",
                 rule={"delta_t": ctx.lim["delta_t"]},
                 notes=[f"Give the currents (currents={{net: amps}}) to size them. At the net class width, outer layer, "
                        f"{ctx.lim['delta_t']:g} °C rise: {caps}."])


def _ipc_amps(w: float, dt: float, t: float) -> float:
    from .pcb_review import ipc2221_current

    return ipc2221_current(w, dt, t, True)


@detector("return_path", "Ground return and pours", 9,
          "Fast and critical signals run over unbroken ground on the next layer, from end to end. Tracks on the ground layer must not "
          "cut the pour under them; critical grounds go into the pour through a via at the pad rather than a long track. Checked "
          "after routing, since the autorouter decides what crosses the pour.",
          ["return_path", "spraar7"])
def _return_path(ctx: Ctx) -> None:
    grounds = sorted(ctx.ground_nets)
    pours = {g: ctx.ground_pour_layers(g) for g in grounds}
    fast = sorted({n for it in ctx.items if it["class"] in ("diff_pair", "crystal", "rf") for n in it["nets"] if ctx.role(n) not in ("ground", "power")}
                  | {n for n, v in ctx.roles.items() if v["role"] == "fast"})
    layers = ctx.b.copper_layers
    if not grounds:  # a board with no ground net at all (a key matrix carrier, an LED strip): nothing to pour
        return
    if not any(pours.values()):
        ctx.item("return_path", "no_pour", "No ground pour on the board", refs=[], nets=grounds, action="place", confidence="high",
                 why="no zone on a ground net", placement=["add a ground pour (on 2 layers: the bottom, kept as unbroken as you can) before routing"])
        return
    for g, lays in pours.items():
        if not lays:
            continue
        missing = [l for l in layers if l not in lays]
        ctx.item("return_path", g, f"{g} pour on {', '.join(lays)}; {len(fast)} fast net(s) need it unbroken under them", refs=[], nets=[g],
                 action="check", why=f"zones on {g}", rule={"pour_layers": lays, "fast_nets": fast[:40]},
                 notes=([f"no {g} pour on {', '.join(missing)}"] if missing else [])
                 + (["2-layer board: every bottom track under a fast net cuts its return path; keep bottom tracks short and away "
                     "from the pairs, crystal and RF"] if len(layers) == 2 else []))


# --- classify ------------------------------------------------------------------------------------------


def classify(b: Board, roles: dict[str, str] | None = None, currents: dict[str, float] | None = None, limits: dict | None = None,
             overrides: dict[str, str] | None = None, only: list[str] | None = None) -> dict:
    """Find the critical items on a board. overrides maps an item id to an action ("route", "hand", "skip", "check", ...)."""
    ctx = Ctx(b, infer_roles(b, roles), currents or {}, {**LIMITS, **(limits or {})})
    for c in sorted(CLASSES.values(), key=lambda c: c.priority):
        if only and c.name not in only:
            continue
        c.detect(ctx)
    for it in ctx.items:
        if overrides and it["id"] in overrides:
            it["action"], it["overridden"] = overrides[it["id"]], True
    ctx.items.sort(key=lambda it: (CLASSES[it["class"]].priority, it["id"]))
    for i, it in enumerate(ctx.items, 1):
        it["n"] = i
    used = sorted({s for it in ctx.items for s in it["sources"]})
    counts: dict[str, int] = {}
    for it in ctx.items:
        counts[it["class"]] = counts.get(it["class"], 0) + 1
    actions: dict[str, int] = {}
    for it in ctx.items:
        actions[it["action"]] = actions.get(it["action"], 0) + 1
    return {
        "ok": True, "board": b.name, "items": ctx.items, "counts": counts, "actions": actions,
        "classes": {c.name: {"label": c.label, "rule": c.rule, "sources": c.sources, "order": c.priority} for c in CLASSES.values()},
        "not_found": [c.label for c in CLASSES.values() if c.name not in counts and (not only or c.name in only)],
        "actions_meaning": ACTIONS, "sources": {k: SOURCES[k] for k in used if k in SOURCES}, "limits": ctx.lim,
        "table": table(ctx.items),
    }


ACTIONS = {
    "route": "cadmate routes it first and locks only these segments",
    "place": "a placement problem: routing can't fix it, move the part first (or override to route anyway)",
    "hand": "route by hand (cadmate can't meet the rule, e.g. a controlled-impedance RF line)",
    "netclass": "the autorouter gets a wider track through a net class; nothing locked",
    "ask": "needs a number from the user (the current) before it can be sized",
    "check": "checked by the layout review after routing",
    "skip": "the user said to leave it to the autorouter",
}


def table(items: list[dict]) -> str:
    """The classification as a Markdown table, for the user to confirm or override before routing."""
    rows = ["| # | Class | What | Action | Rule | Why | Source |", "|---|---|---|---|---|---|---|"]
    for it in items:
        rule = ", ".join(f"{k} {v}" for k, v in it["rule"].items() if v not in (None, [], "") and k not in ("fast_nets", "keep_away_from"))
        extra = "; ".join(it["placement"])
        rows.append(f"| {it['n']} | {CLASSES[it['class']].label} | {it['title']}{' — ' + extra if extra else ''} | {it['action']} | "
                    f"{rule} | {it['why']} | {', '.join(it['sources'])} |")
    return "\n".join(rows)


def save_plan(plan: dict, path: Path) -> Path:
    path.write_text(json.dumps(plan, indent=1))
    return path


def load_plan(path: Path) -> dict:
    return json.loads(Path(path).read_text())

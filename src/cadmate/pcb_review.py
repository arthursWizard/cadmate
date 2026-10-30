"""Review a routed board's layout: what an experienced reviewer would point out after routing, by hand or by an autorouter.

Each finding says where it is, what is wrong, why it matters, and which source the reasoning or the
number comes from. Thresholds that no standard sets are cadmate's own heuristics, and the output says so:
they are a starting point for a conversation, not a pass/fail line.

Net roles (ground, power, switching node, crystal, fast signal) are inferred from net names, the pads'
pin types from the schematic, the schematic's power symbols and the parts on each net. Where a role can't
be inferred the review says so instead of guessing, and the caller can pass roles and currents in.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import tempfile
from pathlib import Path

from . import apps
from .pcb_board import (Board, NetGraph, Pad, Part, Track, convex_hull, dist, load_project, load_schematic, parse, point_in_polygon, point_seg,
                        polygon_area, polygon_gap, seg_seg)

SOURCES = {
    "ipc2221": "IPC-2221 §6.2 conductor sizing: I = k·ΔT^0.44·A^0.725, A in mil², k = 0.048 outer / 0.024 inner layers "
               "(as reproduced in KiCad's PCB Calculator docs, pcb_calculator/tracks_width_versus_current_formula.md). "
               "IPC-2152 replaces these charts and finds IPC-2221 conservative, inner layers especially "
               "(protoexpress.com/blog/how-to-optimize-your-pcb-trace-using-ipc-2152-standard).",
    "mt101": "Analog Devices MT-101 'Decoupling Techniques': capacitors 'as physically close to the power pins of the chip as is possible'; "
             "they 'must connect directly to a low impedance ground plane'; 'short traces or vias are required ... to minimize "
             "additional series inductance' (analog.com/media/en/training-seminars/tutorials/MT-101.pdf).",
    "an2867": "ST AN2867 'Oscillator design guide for STM32', PCB design guidelines (§7.1 in Rev 24): crystal 'as close as possible "
              "to the microcontroller', high-frequency signals 'routed away from the oscillator', a ground guard ring and ground "
              "plane under the oscillator (st.com/resource/en/application_note/an2867-oscillator-design-guide-for-stm32-microcontrollers-stmicroelectronics.pdf).",
    "an1149": "TI AN-1149 (SNVA021C) 'Layout Guidelines for Switching Power Supplies': 'run the feedback trace as far from the "
              "inductor and noisy power traces as possible'; power traces 'short, direct, and thick' (ti.com/lit/an/snva021c/snva021c.pdf).",
    "return_path": "Altium, 'Never Cross a Ground Plane Gap in High Speed PCB Design': a gap under a trace raises loop inductance and "
                   "is an impedance discontinuity (resources.altium.com/p/never-cross-a-ground-plane-gap-in-high-speed-pcb-design).",
    "jlcpcb_pcb": "JLCPCB PCB capabilities (jlcpcb.com/capabilities/pcb-capabilities): min track/space 0.10/0.10 mm for 1 oz 2-layer, "
                  "0.09/0.09 mm multilayer, 0.16/0.16 mm (0.15 multilayer) for 2 oz; min via hole/diameter 0.15/0.25 mm, with 0.1-0.15 mm "
                  "holes at extra cost and 0.2 mm the preferred minimum; via diameter 0.1 mm (0.15 preferred) over the hole; copper to "
                  "routed edge ≥ 0.2 mm; via hole-to-hole 0.2 mm. Read October 2026; check the page before ordering.",
    "jlcpcb_smt": "JLCPCB 'Minimum Spacing Requirements for SMD Components' (jlcpcb.com/help/article/minimum-spacing-for-smd-components), "
                  "recommended package-to-package spacing in mm. The page shows how spacing is measured only in a drawing; cadmate "
                  "measures between package extents (fab outline and pads).",
    "jlcpcb_edge": "JLCPCB PCB assembly FAQ part 2, item 14 (jlcpcb.com/help/article/pcb-assembly-faqs-part-2): for economic assembly "
                   "'traces and components must be more than 0.3mm away from the edge of the board'.",
    "kicad_drc": "KiCad's own design rule checker (kicad-cli pcb drc), with the board's rules.",
    "heuristic": "cadmate heuristic: no standard sets this number. Debatable; adjust it to the design.",
}

# JLCPCB's published minimums, mm (SOURCES["jlcpcb_pcb"]).
JLCPCB = {"track": {1: 0.10, 2: 0.16}, "track_multilayer": {1: 0.09, 2: 0.15}, "via_drill": 0.15, "via_drill_preferred": 0.2,
          "via_diameter": 0.25, "via_ring_over_hole": 0.1, "edge": 0.2, "assembly_edge": 0.3}

# JLCPCB's SMD spacing table, mm, by package class (SOURCES["jlcpcb_smt"]).
_SMT = {("chip", "chip"): 0.18, ("chip", "qfn"): 1.0, ("chip", "qfp"): 0.5, ("chip", "sop"): 0.4, ("chip", "sot"): 0.2,
        ("chip", "bga"): 1.0, ("qfn", "qfn"): 1.0, ("qfn", "qfp"): 1.0, ("qfn", "sop"): 1.0, ("qfn", "sot"): 1.0, ("qfn", "bga"): 1.5,
        ("qfp", "qfp"): 1.25, ("qfp", "sop"): 1.25, ("qfp", "sot"): 1.0, ("qfp", "bga"): 1.5, ("sop", "sop"): 0.5, ("sop", "sot"): 0.4,
        ("sop", "bga"): 1.0, ("sot", "sot"): 0.4, ("sot", "bga"): 1.0, ("bga", "bga"): 2.0}
_SMT_CHIP = {"0201": 0.15, "0402": 0.15, "1206": 0.35}  # chip-to-chip pairs that differ from 0.18 (both parts that size)

# Thresholds no standard sets (SOURCES["heuristic"]); the output repeats them.
LIMITS = {
    "decap_distance": 3.0,  # mm from a bypass cap's power pad to the nearest power pin it can serve
    "decap_loop": 10.0,  # mm, power path + ground path from cap to IC
    "bulk_distance": 15.0,  # mm, for caps over 1 µF
    "pin_without_cap": 5.0,  # mm: an IC power pin with no bypass cap this close
    "crystal_route": 10.0,  # mm from the crystal pad to the IC pin
    "crystal_gap": 0.5,  # mm: other signals closer than this to crystal traces
    "noisy_gap": 1.0,  # mm: signals closer than this to a switching node
    "neck_length": 1.0,  # mm: a narrower track this short at a small pad is a fan-out neck, not a thin trace
    "vias_per_net": 6,
    "pair_mismatch": 2.0,  # mm between the two nets of a pair
    "pair_coupled": 0.6,  # share of a pair's length run side by side (edge gap within 3 widths, at least 0.5 mm)
    "delta_t": 10.0,  # °C temperature rise for IPC-2221
}

LISTED = 60  # findings beyond this are counted, not listed

_AUTO = re.compile(r"^(Net-\(|unconnected-\()")
_GROUND = re.compile(r"^/?(?:[\w.]*[_-])?(GND|VSS|AGND|DGND|PGND|SGND|GNDA|GNDD|GNDPWR|EARTH|CHASSIS|0V)(?:[_-][\w.]*)?\d*$", re.I)
_POWER = re.compile(r"^/?(?:[+-]?\d+(?:[.,]\d+)?V\d*|\d+V\d+|[+-]?V(?:CC|DD|IN|BUS|BAT|SYS|CORE|IO|DDA|DDIO|LED|MOT|M|PP|USB|AA|S)\w*"
                    r"|AVDD\w*|DVDD\w*|AVCC\w*|PVIN\w*|VCCIO\w*|\w*_(?:VCC|VDD|VIN|3V3|5V|12V))$", re.I)
_SWITCH = re.compile(r"(?:^|[/_-])(SW|LX|PH|SWN|SW_?NODE|SWITCH)\d*(?:$|[_-])", re.I)
_FAST = re.compile(r"(CLK|SCK|SCLK|XTAL|XIN|XOUT|OSC|USB|D[+-]$|DP$|DM$|MOSI|MISO|SDIO|ETH|HDMI|LVDS|MCLK|BCLK|LRCK|QSPI|RMII|MII)", re.I)
_SENSITIVE = re.compile(r"(?:^|[/_-])(V?FB|ADC\d*|AIN\d*|SENSE\w*|VREF\w*|COMP|ISNS\w*|REXT|RSET)(?:$|[_-]|\))", re.I)
_SW_PIN = re.compile(r"^(SW\d*|LX\d*|PH|SWN|SW_?NODE)$", re.I)
_IC_REF = re.compile(r"^(U|IC)\d+", re.I)
_CAP_REF = re.compile(r"^C\d+")
_IND_REF = re.compile(r"^(L|FB)\d+")
_XTAL_REF = re.compile(r"^(Y|X|XTAL|XT|OSC)\d+", re.I)
_XTAL_LIB = re.compile(r"crystal|xtal|resonator|oscillator", re.I)


class ReviewError(RuntimeError):
    pass


# --- net roles ---------------------------------------------------------------------------------------


def _is_ic(part: Part) -> bool:
    return bool(_IC_REF.match(part.ref)) or (len(part.copper_pads) >= 5 and not re.match(r"^(J|P|CN|SW|K|H|MH|TP|BT)\d", part.ref))


def _is_crystal(part: Part) -> bool:
    return bool(_XTAL_REF.match(part.ref)) or bool(_XTAL_LIB.search(part.lib)) or bool(re.search(r"\d\s*[kM]Hz", part.value) and part.ref[:1] in "YX")


def infer_roles(b: Board, overrides: dict[str, str] | None = None) -> dict[str, dict]:
    """{net: {"role", "why"}}: ground, power, switching, crystal, fast or signal."""
    roles: dict[str, dict] = {}
    pintypes: dict[str, set[str]] = {}
    for p in b.pads:
        if p.net and p.pintype:
            pintypes.setdefault(p.net, set()).add(p.pintype.split("+")[0])
    sw_pin_nets = {p.net for part in b.parts if _is_ic(part) for p in part.copper_pads if p.net and p.pinfunction and _SW_PIN.match(p.pinfunction)}
    ind_nets = {p.net for part in b.parts if _IND_REF.match(part.ref) for p in part.copper_pads if p.net}
    xtal_nets: dict[str, str] = {}
    for part in b.parts:
        if _is_crystal(part):
            for p in part.copper_pads:
                if p.net:
                    xtal_nets.setdefault(p.net, part.ref)
    for net in b.nets:
        bare = net.split("/")[-1]
        auto = bool(_AUTO.match(bare))
        types = pintypes.get(net, set())
        if not auto and _GROUND.match(bare):
            r = ("ground", "named like ground")
        elif net in b.power_symbol_nets:
            r = ("ground", "ground power symbol in the schematic") if _GROUND.match(bare) or "GND" in bare.upper() else ("power", "power symbol in the schematic")
        elif not auto and _POWER.match(bare):
            r = ("power", "named like a supply")
        elif net in sw_pin_nets and net in ind_nets:
            r = ("switching", "joins a regulator's SW/LX pin to an inductor")
        elif not auto and _SWITCH.search(bare):
            r = ("switching", "named like a switch node")
        elif types & {"power_in", "power_out"}:
            r = ("power", f"pin types {', '.join(sorted(types & {'power_in', 'power_out'}))} from the schematic")
        elif re.match(r"^(PWR|POWER|SUPPLY|GND|GROUND)", b.net_class_of.get(net, ""), re.I):
            cls = b.net_class_of[net]
            r = ("ground" if re.match(r"^(GND|GROUND)", cls, re.I) else "power", f"in net class {cls}")
        elif net in xtal_nets and not (types & {"power_in"}):
            r = ("crystal", f"on crystal {xtal_nets[net]}")
        elif not auto and _FAST.search(bare):
            r = ("fast", "named like a clock, USB or bus signal")
        else:
            r = ("signal", "nothing says what it carries")
        roles[net] = {"role": r[0], "why": r[1]}
        if _SENSITIVE.search(bare) and r[0] == "signal":
            roles[net] = {"role": "sensitive", "why": "named like feedback, sense or reference"}
    for net, role in (overrides or {}).items():
        roles[net] = {"role": role, "why": "given by the caller"}
    return roles


# --- findings ----------------------------------------------------------------------------------------


def _f(rule: str, severity: str, what: str, why: str, source: str, x: float | None = None, y: float | None = None,
       layer: str | None = None, refs: list[str] | None = None, net: str | None = None, **values) -> dict:
    out = {"rule": rule, "severity": severity, "what": what, "why": why, "source": source}
    where = {k: v for k, v in (("x", None if x is None else round(x, 2)), ("y", None if y is None else round(y, 2)),
                                ("layer", layer), ("refs", refs), ("net", net)) if v}
    if where:
        out["where"] = where
    if values:
        out["values"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in values.items()}
    return out


def _farads(value: str) -> float | None:
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*([pnuµμm]?)(\d*)\s*F?", value)
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    if m.group(3):  # 4u7
        num = float(f"{m.group(1)}.{m.group(3)}")
    scale = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1e-6 if num < 1000 else 1e-12}[m.group(2)]
    return num * scale


def _fmt_f(f: float | None) -> str:
    if f is None:
        return "unknown value"
    for s, u in ((1e-6, "µF"), (1e-9, "nF"), (1e-12, "pF")):
        if f >= s:
            return f"{f / s:g} {u}"
    return f"{f:g} F"


class Review:
    def __init__(self, board: Board, roles: dict[str, dict], currents: dict[str, float] | None, limits: dict):
        self.b, self.roles, self.currents, self.lim = board, roles, currents or {}, limits
        self.findings: list[dict] = []
        self.checked: list[str] = []
        self.skipped: list[dict] = []
        self._graphs: dict[str, NetGraph] = {}
        self.measured: dict[str, list] = {}  # what each rule measured, findings or not

    def role(self, net: str | None) -> str:
        return self.roles.get(net, {}).get("role", "signal") if net else "none"

    def graph(self, net: str) -> NetGraph:
        if net not in self._graphs:
            self._graphs[net] = NetGraph(self.b, net)
        return self._graphs[net]

    def add(self, *a, **kw) -> None:
        self.findings.append(_f(*a, **kw))

    # decoupling --------------------------------------------------------------------------------------

    def decoupling(self) -> None:
        """Per IC power pin: the nearest bypass cap on its net is its decoupling cap; judge that pair's placement and routing.

        Placement (distance) is the designer's; route length, vias and the ground return are what a router decides.
        """
        caps = []
        for part in self.b.parts:
            pads = [p for p in part.copper_pads if p.net]
            if not _CAP_REF.match(part.ref) or len(pads) != 2:
                continue
            gnd = [p for p in pads if self.role(p.net) == "ground"]
            pwr = [p for p in pads if self.role(p.net) == "power"]
            if len(gnd) == 1 and len(pwr) == 1:
                caps.append((part, pwr[0], gnd[0], _farads(part.value)))
        ics = [p for p in self.b.parts if _is_ic(p)]
        if not caps or not ics:
            self.skipped.append({"rule": "decoupling", "why": "no capacitor between a power and a ground net" if not caps
                                 else "no IC on the board"})
            return
        self.checked.append("decoupling")
        bypass = [c for c in caps if c[3] is None or c[3] <= 1.01e-6]
        pairs: dict[tuple[str, str], list[Pad]] = {}  # (cap, ic) -> pins it is nearest to
        lonely: dict[str, list[tuple[Pad, float | None]]] = {}
        for ic in ics:
            for pin in ic.copper_pads:
                if not pin.net or self.role(pin.net) != "power" or not (pin.pintype or "").startswith("power_in"):
                    continue
                cands = [c for c in bypass if c[1].net == pin.net]
                best = min(cands, key=lambda c: dist((c[1].x, c[1].y), (pin.x, pin.y)), default=None)
                d = dist((best[1].x, best[1].y), (pin.x, pin.y)) if best else None
                if best is None or d > self.lim["pin_without_cap"]:
                    lonely.setdefault(ic.ref, []).append((pin, d))
                if best is not None:
                    pairs.setdefault((best[0].ref, ic.ref), []).append(pin)
        capinfo = {c[0].ref: c for c in caps}
        for (cref, iref), pins in pairs.items():
            part, pp, gp, farads = capinfo[cref]
            ic = self.b.part(iref)
            pin = min(pins, key=lambda p: dist((p.x, p.y), (pp.x, pp.y)))
            straight = dist((pp.x, pp.y), (pin.x, pin.y))
            if straight > self.lim["pin_without_cap"]:
                continue  # reported with the IC's lonely pins
            route = self.graph(pp.net).path(pp, pin)
            gnd_pins = [p for p in ic.copper_pads if p.net == gp.net]
            gpin = min(gnd_pins, key=lambda p: dist((p.x, p.y), (gp.x, gp.y)), default=None)
            groute = self.graph(gp.net).path(gp, gpin) if gpin else None
            names = ", ".join(sorted({p.number for p in pins}, key=lambda s: (len(s), s)))
            fn = pin.pinfunction or pin.net
            label = f"{cref} ({_fmt_f(farads)}) for {iref} {fn} (pin{'s' if len(pins) > 1 else ''} {names})"
            vals = {"straight_mm": straight, "power_route_mm": route["length"] if route else None,
                    "power_route_vias": route["vias"] if route else None,
                    "ground_route_mm": groute["length"] if groute else None, "ground_route_vias": groute["vias"] if groute else None,
                    "ground_through_pour": groute["through_pour"] if groute else None}
            where = dict(refs=[cref, iref], net=pp.net)
            self.measured.setdefault("decoupling", []).append(
                {"cap": cref, "ic": iref, "pins": sorted({p.number for p in pins}), "net": pp.net,
                 **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in vals.items()},
                 "loop_mm": round(route["length"] + groute["length"], 2) if route and groute else None})
            if route is None:
                self.add("decoupling", "problem", f"{label}: no copper path from the cap to the pin",
                         "The cap isn't connected to the pin it sits next to; check for an unrouted or broken track.", "mt101", pp.x, pp.y, **where, **vals)
                continue
            if straight > self.lim["decap_distance"]:
                self.add("decoupling", "warning", f"{label}: placed {straight:.1f} mm from the pin (limit {self.lim['decap_distance']:g} mm)",
                         "MT-101: as close to the power pin as possible. Distance is placement, so the fix is moving the cap; "
                         "a router can't make up for it.", "mt101", pp.x, pp.y, **where, **vals)
            if route["length"] > 2 * straight + 2.0:
                self.add("decoupling", "warning", f"{label}: the track runs {route['length']:.1f} mm to reach a pin {straight:.1f} mm away",
                         "The cap is close but its track takes the long way; the inductance that matters is along the copper, "
                         "not the straight line. Typical autorouter result.", "mt101", pp.x, pp.y, **where, **vals)
            if route["vias"]:
                self.add("decoupling", "note" if route["vias"] == 1 else "warning", f"{label}: {route['vias']} via(s) between cap and pin",
                         "Each via adds series inductance. Fine when the cap sits on the other side right under the pin; "
                         "otherwise put cap and pin on one layer.", "mt101", pp.x, pp.y, **where, **vals)
            if gpin is not None and groute is None:
                self.add("decoupling", "warning", f"{label}: no copper path from the cap's ground pad to {gpin.name}",
                         "Without its ground side the cap decouples nothing. Often a ground pad left out of the pour "
                         "or an unrouted ground connection.", "mt101", gp.x, gp.y, refs=[cref, iref], net=gp.net, **vals)
            if groute is not None:
                loop = route["length"] + groute["length"]
                if loop > self.lim["decap_loop"]:
                    self.add("decoupling", "warning", f"{label}: loop cap → pin → {gpin.name} → cap is about {loop:.1f} mm",
                             f"Loop length sets loop inductance. Power side {route['length']:.1f} mm, ground side {groute['length']:.1f} mm"
                             + (" (through the ground pour, counted as a straight line)" if groute["through_pour"] else "")
                             + ". MT-101: the cap's ground goes straight to the ground plane with a short trace or via.",
                             "mt101", gp.x, gp.y, refs=[cref, iref], net=gp.net, loop_mm=loop, limit_mm=self.lim["decap_loop"], **vals)
        for iref, pins in lonely.items():
            desc = ", ".join(f"{p.number} ({p.pinfunction or p.net})" + (f" nearest {d:.1f} mm" if d is not None else " none on its net") for p, d in pins)
            self.add("decoupling", "warning", f"{iref}: power pin(s) with no bypass cap within {self.lim['pin_without_cap']:g} mm: {desc}",
                     "Pins sharing one cap further away see the trace inductance to it. Some pins don't need their own cap; "
                     "check the datasheet's recommendation.", "heuristic", pins[0][0].x, pins[0][0].y, refs=[iref], net=pins[0][0].net)

    # crystals --------------------------------------------------------------------------------------

    def crystals(self) -> None:
        xtals = [p for p in self.b.parts if _is_crystal(p)]
        if not xtals:
            self.skipped.append({"rule": "crystal", "why": "no crystal or oscillator found (by reference Y/X or a crystal footprint)"})
            return
        self.checked.append("crystal")
        for x in xtals:
            nets = sorted({p.net for p in x.copper_pads if p.net and self.role(p.net) not in ("ground", "power")})
            own = set(nets)
            area = x.courtyard or [(p.x, p.y) for p in x.copper_pads]
            for net in nets:
                pad = next(p for p in x.copper_pads if p.net == net)
                ic_pins = [p for part in self.b.parts if _is_ic(part) for p in part.copper_pads if p.net == net]
                tracks = [t for t in self.b.tracks if t.net == net]
                net_vias = [v for v in self.b.vias if v.net == net]
                r = None
                if ic_pins:
                    pin = min(ic_pins, key=lambda p: dist((p.x, p.y), (pad.x, pad.y)))
                    r = self.graph(net).path(pad, pin)
                    self.measured.setdefault("crystal", []).append(
                        {"crystal": x.ref, "net": net, "to": pin.name, "route_mm": r["length"] if r else None,
                         "vias": len(net_vias), "track_mm": round(sum(t.length for t in tracks), 2),
                         "layers": sorted({t.layer for t in tracks})})
                    if r and r["length"] > self.lim["crystal_route"]:
                        self.add("crystal", "warning", f"{x.ref} to {pin.name}: {r['length']:.1f} mm of track",
                                 "Crystal traces are high-impedance and pick up noise; AN2867 puts the crystal as close as possible to the MCU.",
                                 "an2867", pad.x, pad.y, refs=[x.ref, pin.ref], net=net, route_mm=r["length"], limit_mm=self.lim["crystal_route"])
                if net_vias:
                    self.add("crystal", "warning", f"{net} (crystal {x.ref}): {len(net_vias)} via(s)",
                             "Vias on crystal nets add length and couple to the other layer; keep the oscillator on one layer.",
                             "an2867", net_vias[0].x, net_vias[0].y, refs=[x.ref], net=net, vias=len(net_vias))
                near = self._neighbours(tracks, exclude=own, gap=self.lim["crystal_gap"], skip_roles=("ground", "power"))
                for other, (gap, length, at, layer) in sorted(near.items(), key=lambda kv: -kv[1][1])[:3]:
                    self.add("crystal", "warning" if self.role(other) in ("fast", "switching", "signal", "sensitive") else "note",
                             f"{other} runs {length:.1f} mm alongside crystal net {net}, {gap:.2f} mm apart",
                             "AN2867: route high-frequency and other signals away from the oscillator; coupling into the crystal "
                             "nets shifts or stops the oscillation.", "an2867", at[0], at[1], layer=layer, refs=[x.ref], net=other,
                             gap_mm=gap, coupled_mm=length, threshold_mm=self.lim["crystal_gap"])
                for c in self.b.parts:
                    if _CAP_REF.match(c.ref) and any(p.net == net for p in c.copper_pads):
                        cp = next(p for p in c.copper_pads if p.net == net)
                        d = dist((cp.x, cp.y), (pad.x, pad.y))
                        if d > 5.0:
                            self.add("crystal", "note", f"load cap {c.ref} is {d:.1f} mm from {x.ref}",
                                     "Load caps belong next to the crystal, so the loop crystal–caps–ground stays small.",
                                     "an2867", cp.x, cp.y, refs=[c.ref, x.ref], net=net, distance_mm=d)
            # foreign tracks through the crystal's area, any layer
            grown = _grow(area, 0.5)
            under: dict[str, tuple[float, str, tuple]] = {}
            for t in self.b.tracks:
                if t.net in own or self.role(t.net) == "ground" or t.net is None:
                    continue
                if any(x.ref == p.ref for p in self.b.pads if p.net == t.net):
                    continue
                for s, e in t.pieces():
                    if _seg_hits_poly(s, e, grown):
                        under.setdefault(t.net, (t.length, t.layer, s))
            xl = x.copper_pads[0].layers[0] if x.copper_pads and x.copper_pads[0].layers else None
            for net, (_, layer, at) in sorted(under.items(), key=lambda kv: (kv[1][1] != xl, self.role(kv[0]) == "power"))[:6]:
                same = layer == xl
                near_layer = same or (len(self.b.copper_layers) > 2 and layer in self.b.adjacent(xl or ""))
                if not near_layer and len(self.b.copper_layers) > 2:
                    continue
                self.add("crystal", "warning" if near_layer and self.role(net) != "power" else "note",
                         f"{net} passes {'through' if same else 'under'} crystal {x.ref}'s area on {layer}"
                         + ("" if near_layer else ", the other side of the board"),
                         "AN2867 (Signals around the oscillator): no other signals under or next to the crystal and its load caps."
                         + ("" if near_layer else " Through the full board thickness the coupling is weaker, but nothing shields it "
                            "unless there's ground pour between."), "an2867", at[0], at[1], layer=layer, refs=[x.ref], net=net)
            ref_layers = self.b.adjacent(x.copper_pads[0].layers[0]) if x.copper_pads and x.copper_pads[0].layers else []
            cover = self._ground_cover(area, ref_layers) if any(self.role(f.net) == "ground" for f in self.b.fills) else None
            if cover is not None and cover < 0.5:
                self.add("crystal", "note", f"{x.ref}: only {cover:.0%} of its area has ground pour on {', '.join(ref_layers)}",
                         "AN2867 recommends a ground plane under the oscillator, tied to the MCU's ground.", "an2867",
                         x.x, x.y, refs=[x.ref], ground_cover=round(cover, 2))

    def _ground_cover(self, poly, layers) -> float | None:
        fills = [f for f in self.b.fills if f.layer in layers and self.role(f.net) == "ground"]
        if not poly or not layers:
            return None
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        n = hit = 0
        for i in range(10):
            for j in range(10):
                p = (min(xs) + (i + 0.5) * (max(xs) - min(xs)) / 10, min(ys) + (j + 0.5) * (max(ys) - min(ys)) / 10)
                if len(poly) >= 3 and not point_in_polygon(p, poly):
                    continue
                n += 1
                hit += any(f.contains(p) for f in fills)
        return hit / n if n else None

    def _neighbours(self, tracks: list[Track], exclude: set, gap: float, skip_roles=()) -> dict:
        """Other nets' copper within gap (edge to edge) of these tracks: {net: (min gap, coupled length, where, layer)}."""
        out: dict[str, list] = {}
        others = [t for t in self.b.tracks if t.net not in exclude and t.net and self.role(t.net) not in skip_roles]
        pads = [p for p in self.b.pads if p.net not in exclude and p.net and self.role(p.net) not in skip_roles]
        for t in tracks:
            for s, e in t.pieces():
                L = dist(s, e)
                n = max(1, int(L / 0.2))
                for k in range(n):
                    p = (s[0] + (e[0] - s[0]) * (k + 0.5) / n, s[1] + (e[1] - s[1]) * (k + 0.5) / n)
                    best: dict[str, float] = {}
                    for o in others:
                        if o.layer != t.layer:
                            continue
                        for a, b in o.pieces():
                            d = point_seg(p, a, b)[0] - (o.width + t.width) / 2
                            if d < gap and d < best.get(o.net, math.inf):
                                best[o.net] = d
                    for pd in pads:
                        if pd.on(t.layer):
                            d = pd.distance(p) - t.width / 2
                            if d < gap and d < best.get(pd.net, math.inf):
                                best[pd.net] = d
                    for net, d in best.items():
                        cur = out.setdefault(net, [math.inf, 0.0, p, t.layer])
                        if d < cur[0]:
                            cur[0], cur[2], cur[3] = d, p, t.layer
                        cur[1] += L / n
        return {k: (round(max(0.0, v[0]), 3), round(v[1], 2), v[2], v[3]) for k, v in out.items()}

    # switching nodes -------------------------------------------------------------------------------------

    def noisy(self) -> None:
        sw = [n for n in self.b.nets if self.role(n) == "switching"]
        if not sw:
            self.skipped.append({"rule": "switching_node", "why": "no switching node found (a regulator's SW/LX pin to an inductor, or a net named SW/LX)"})
            return
        self.checked.append("switching_node")
        for net in sw:
            tracks = [t for t in self.b.tracks if t.net == net]
            near = self._neighbours(tracks, exclude={net}, gap=self.lim["noisy_gap"], skip_roles=("ground", "power", "switching"))
            for other, (gap, length, at, layer) in sorted(near.items(), key=lambda kv: -kv[1][1]):
                sens = self.role(other) in ("sensitive", "crystal", "fast")
                if not sens and length < 2.0:
                    continue
                self.add("switching_node", "warning" if sens else "note", f"{other} runs {length:.1f} mm alongside switch node {net}, {gap:.2f} mm apart",
                         "The switch node swings the full input voltage at every edge and couples into what runs next to it; "
                         "AN-1149: keep feedback and other sensitive traces away from the inductor and noisy power traces.",
                         "an1149", at[0], at[1], layer=layer, net=other, gap_mm=gap, coupled_mm=length)
            copper = sum(t.length * t.width for t in tracks)
            if copper > 40:
                self.add("switching_node", "note", f"switch node {net} has {copper:.0f} mm² of track",
                         "A large switch node area radiates; keep it no bigger than the current needs.", "an1149", tracks[0].start[0], tracks[0].start[1], net=net)

    # track width and current ---------------------------------------------------------------------------

    def widths(self) -> None:
        self.checked.append("track_width")
        dT = self.lim["delta_t"]
        by_net: dict[str, list[Track]] = {}
        for t in self.b.tracks:
            if t.net:
                by_net.setdefault(t.net, []).append(t)
        for net, tracks in by_net.items():
            cls = self.b.net_class(net)
            role = self.role(net)
            want = cls.track_width if cls else None
            if want:
                thin = [t for t in tracks if t.width < want - 1e-6 and not self._neck(t)]
                if thin:
                    t = min(thin, key=lambda t: t.width)
                    self.add("track_width", "warning" if role in ("power", "ground") else "note",
                             f"{net}: {len(thin)} track(s) ({sum(x.length for x in thin):.1f} mm in all) narrower than its net class "
                             f"'{cls.name}' ({want:g} mm), down to {t.width:g} mm",
                             "KiCad's DRC checks the minimum width, not the class width, so a router or a hand edit can go narrower "
                             "without an error. Short necks into small pads are left out.", "heuristic", t.start[0], t.start[1], t.layer,
                             net=net, class_width=want, narrowest=t.width)
            if net in self.currents:
                amps = self.currents[net]
                bad = []
                for t in tracks:
                    need = ipc2221_width(amps, dT, self.b.copper_thickness.get(t.layer, 0.035), self.b.is_outer(t.layer))
                    if t.width < need - 1e-6:
                        bad.append((t, need))
                if bad:
                    t, need = min(bad, key=lambda x: x[0].width - x[1])
                    necks = [x for x, _ in bad if self._neck(x)]
                    self.add("current", "problem" if len(necks) < len(bad) else "note",
                             f"{net} at {amps:g} A: {len(bad)} track(s) narrower than the {need:.2f} mm IPC-2221 asks for a {dT:g} °C rise, "
                             f"down to {t.width:g} mm on {t.layer}" + (f" ({len(necks)} of them short necks at pads)" if necks else ""),
                             "Too narrow a trace heats up and drops voltage. IPC-2221 is conservative; IPC-2152 allows narrower, "
                             "especially on inner layers.", "ipc2221", t.start[0], t.start[1], t.layer, net=net, amps=amps,
                             required_mm=need, narrowest=t.width, delta_t=dT)
            elif role in ("power", "ground") and net not in self.b.zone_nets:
                t = min(tracks, key=lambda t: t.width)
                cap = ipc2221_current(t.width, dT, self.b.copper_thickness.get(t.layer, 0.035), self.b.is_outer(t.layer))
                self.add("current", "note", f"{net}: narrowest track {t.width:g} mm on {t.layer} carries about {cap:.2f} A at a {dT:g} °C rise",
                         "No current given for this net, so this is the limit, not a verdict. Pass the expected current to check it.",
                         "ipc2221", t.start[0], t.start[1], t.layer, net=net, capacity_a=cap, width=t.width)

    def _neck(self, t: Track) -> bool:
        if t.length > self.lim["neck_length"]:
            return False
        for p in self.b.pads:
            if p.net == t.net and p.on(t.layer) and min(p.w, p.h) <= (self.b.net_class(t.net).track_width or 0) + 1e-6 \
                    and (p.contains(t.start, t.width / 2) or p.contains(t.end, t.width / 2)):
                return True
        return False

    # differential pairs ------------------------------------------------------------------------------

    def pairs(self) -> None:
        found = diff_pairs(self.b.nets)
        routed = [(a, b) for a, b in found if any(t.net == a for t in self.b.tracks) and any(t.net == b for t in self.b.tracks)]
        if not routed:
            self.skipped.append({"rule": "diff_pair", "why": "no routed net pairs named like X+/X-, X_P/X_N or DP/DM"})
            return
        self.checked.append("diff_pair")
        for a, b in routed:
            ta = [t for t in self.b.tracks if t.net == a]
            tb = [t for t in self.b.tracks if t.net == b]
            la, lb = sum(t.length for t in ta), sum(t.length for t in tb)
            broken = [n for n in (a, b) if len(self.graph(n).islands()) > 1]
            if broken:
                self.measured.setdefault("diff_pair", []).append({"pair": [a, b], "length_mm": [round(la, 2), round(lb, 2)], "unrouted": broken})
                self.add("diff_pair", "note", f"{a} / {b}: {' and '.join(broken)} not fully routed, so length and coupling aren't compared",
                         "Route the pair first; the unrouted connection is reported on its own.", "heuristic", ta[0].start[0], ta[0].start[1],
                         ta[0].layer, net=a)
                continue
            coupled = self._coupled(ta, tb)
            self.measured.setdefault("diff_pair", []).append({"pair": [a, b], "length_mm": [round(la, 2), round(lb, 2)],
                                                              "mismatch_mm": round(abs(la - lb), 2), "coupled": round(coupled, 2)})
            if abs(la - lb) > self.lim["pair_mismatch"]:
                self.add("diff_pair", "warning", f"{a} / {b}: lengths differ by {abs(la - lb):.1f} mm ({la:.1f} vs {lb:.1f})",
                         "Length mismatch in a pair turns into skew between the two signals, which turns into common-mode noise. "
                         "Tracks count only; pad and package lengths aren't included.", "heuristic", ta[0].start[0], ta[0].start[1],
                         ta[0].layer, net=a, mismatch_mm=abs(la - lb))
            if coupled < self.lim["pair_coupled"]:
                self.add("diff_pair", "warning" if coupled < 0.4 else "note", f"{a} / {b}: run together for only {coupled:.0%} of their length",
                         "A pair keeps its impedance and rejects noise only while the two tracks run side by side; an autorouter "
                         "routes them as two separate nets.", "heuristic", ta[0].start[0], ta[0].start[1], ta[0].layer, net=a, coupled=coupled)

    def _coupled(self, ta: list[Track], tb: list[Track]) -> float:
        total = near = 0.0
        for t in ta:
            for s, e in t.pieces():
                L = dist(s, e)
                n = max(1, int(L / 0.2))
                for k in range(n):
                    p = (s[0] + (e[0] - s[0]) * (k + 0.5) / n, s[1] + (e[1] - s[1]) * (k + 0.5) / n)
                    total += L / n
                    gap = min((point_seg(p, a, b)[0] - (o.width + t.width) / 2 for o in tb if o.layer == t.layer for a, b in o.pieces()),
                              default=math.inf)
                    if gap <= max(0.5, 3 * t.width):
                        near += L / n
        return near / total if total else 1.0

    # vias ----------------------------------------------------------------------------------------------

    def vias(self, fab: str | None) -> None:
        self.checked.append("vias")
        count: dict[str, int] = {}
        for v in self.b.vias:
            if v.net:
                count[v.net] = count.get(v.net, 0) + 1
        for net, n in sorted(count.items(), key=lambda kv: -kv[1]):
            role = self.role(net)
            if role in ("ground", "power", "crystal"):
                continue
            if role == "fast" and n >= 2:
                self.add("vias", "note", f"{net}: {n} vias on a fast signal", "Each via is an impedance step and moves the return current "
                         "to another plane; on USB and clocks keep layer changes few and put a ground via next to each.", "heuristic", net=net, vias=n)
            elif n >= self.lim["vias_per_net"]:
                self.add("vias", "note", f"{net}: {n} vias", "Many layer changes on one net usually means the router wandered; "
                         "harmless on slow signals, but worth a look for a shorter path.", "heuristic", net=net, vias=n)
        if fab == "jlcpcb":
            seen = set()
            for v in self.b.vias:
                key = (v.size, v.drill)
                if key in seen:
                    continue
                seen.add(key)
                n = sum(1 for w in self.b.vias if (w.size, w.drill) == key)
                if v.drill < JLCPCB["via_drill"] - 1e-6 or v.size < JLCPCB["via_diameter"] - 1e-6:
                    self.add("fab", "problem", f"{n} via(s) {v.size:g}/{v.drill:g} mm below JLCPCB's minimum {JLCPCB['via_diameter']:g}/{JLCPCB['via_drill']:g} mm",
                             "The fab can't drill or plate them.", "jlcpcb_pcb", v.x, v.y, net=v.net)
                elif v.drill < JLCPCB["via_drill_preferred"] - 1e-6:
                    self.add("fab", "note", f"{n} via(s) with a {v.drill:g} mm hole: JLCPCB charges extra below 0.2 mm",
                             "Larger holes are cheaper and more reliable when space allows.", "jlcpcb_pcb", v.x, v.y, net=v.net)
                if v.size - v.drill < JLCPCB["via_ring_over_hole"] - 1e-6:
                    self.add("fab", "problem", f"{n} via(s) {v.size:g}/{v.drill:g} mm: diameter less than 0.1 mm over the hole",
                             "JLCPCB wants the via diameter 0.1 mm (0.15 preferred) larger than the hole.", "jlcpcb_pcb", v.x, v.y, net=v.net)

    # return path ---------------------------------------------------------------------------------------

    def return_path(self) -> None:
        refs = [f for f in self.b.fills if self.role(f.net) == "ground"]
        inferred = False
        if not refs:
            big = self._plane_nets()
            refs = [f for f in self.b.fills if f.net in big]
            inferred = bool(refs)
        if not refs:
            self.skipped.append({"rule": "return_path", "why": "no ground pour (or any large pour) to act as a reference plane"})
            return
        self.checked.append("return_path")
        ref_nets = {f.net for f in refs}
        by_net: dict[str, list] = {}
        for t in self.b.tracks:
            if t.net is None or t.net in ref_nets or self.role(t.net) in ("ground", "power"):
                continue
            layers = [l for l in self.b.adjacent(t.layer) if any(f.layer == l for f in refs)]
            if not layers:
                continue
            fills = [f for f in refs if f.layer in layers]
            for s, e in t.pieces():
                L = dist(s, e)
                n = max(2, int(L / 0.2))
                cov = []
                for k in range(n + 1):
                    p = (s[0] + (e[0] - s[0]) * k / n, s[1] + (e[1] - s[1]) * k / n)
                    cov.append(any(f.contains(p) for f in fills))
                k = 0
                while k <= n:  # runs of no plane, with plane on both sides: the trace crosses a gap
                    if not cov[k]:
                        j = k
                        while j <= n and not cov[j]:
                            j += 1
                        if k > 0 and j <= n:
                            mid = (k + j - 1) / 2 / n
                            p = (s[0] + (e[0] - s[0]) * mid, s[1] + (e[1] - s[1]) * mid)
                            by_net.setdefault(t.net, []).append((p, t.layer, layers, (j - k) * L / n))
                        k = j
                    else:
                        k += 1
        plane = "a pour on " + ", ".join(sorted(ref_nets)) + (" (not named ground; taken as the reference plane because it covers most of the board)" if inferred else "")
        slow = 0
        for net, gaps in sorted(by_net.items(), key=lambda kv: -len(kv[1])):
            role = self.role(net)
            p, layer, layers, w = max(gaps, key=lambda g: g[3])
            if role in ("fast", "crystal"):
                self.add("return_path", "warning", f"{net} crosses {len(gaps)} gap(s) in {plane} on {', '.join(layers)}, the widest {w:.1f} mm",
                         "The return current under a fast signal has to detour around the gap: more loop inductance, more emission, "
                         "and an impedance step.", "return_path", p[0], p[1], layer, net=net, crossings=len(gaps), widest_mm=w)
            else:
                slow += len(gaps)
        if slow:
            nets = [n for n in by_net if self.role(n) not in ("fast", "crystal")]
            self.add("return_path", "note", f"{len(nets)} other net(s) cross {slow} gap(s) in {plane}",
                     "Only fast edges care (clocks, USB, SPI, PWM). Slow nets such as key matrix rows or LED enables don't. "
                     "cadmate can't tell a net's speed from its name here, so check these: " + ", ".join(nets[:12]) + ("…" if len(nets) > 12 else ""),
                     "return_path", nets=nets[:30])

    def _plane_nets(self) -> set[str]:
        box = self.b.bbox()
        if not box:
            return set()
        area = (box[2] - box[0]) * (box[3] - box[1])
        out = set()
        per: dict[tuple[str, str], float] = {}
        for f in self.b.fills:
            if f.net:
                per[(f.net, f.layer)] = per.get((f.net, f.layer), 0) + polygon_area(f.pts)
        for (net, _), a in per.items():
            if area and a > 0.4 * area:
                out.add(net)
        return out

    # assembly ------------------------------------------------------------------------------------------

    def assembly(self, fab: str | None) -> None:
        if fab != "jlcpcb":
            self.skipped.append({"rule": "assembly", "why": "no fab given; JLCPCB's spacing table is the one cadmate knows"})
            return
        self.checked.append("assembly")
        parts = [p for p in self.b.parts if p.smd and not p.dnp and p.copper_pads]
        unknown = set()
        extents = {p.ref: _extent(p) for p in parts}
        for i, a in enumerate(parts):
            ca = package_class(a)
            for b in parts[i + 1:]:
                if a.side != b.side:
                    continue
                cb = package_class(b)
                if not ca or not cb:
                    unknown |= {p.ref for p, c in ((a, ca), (b, cb)) if not c}
                    continue
                need = _smt_spacing(ca, cb)
                if dist((a.x, a.y), (b.x, b.y)) > 30:
                    continue
                gap = polygon_gap(extents[a.ref], extents[b.ref])
                if gap < need - 1e-6:
                    self.add("assembly", "warning" if gap < need / 2 else "note",
                             f"{a.ref} and {b.ref} are {gap:.2f} mm apart (pad to pad); JLCPCB recommends {need:g} mm for {ca[0]}–{cb[0]}",
                             "Close parts are harder to place, inspect and rework, and can bridge. JLCPCB calls these recommended minimums, "
                             "not hard limits; dense boards go below them routinely, so weigh it against the board's size.",
                             "jlcpcb_smt", (a.x + b.x) / 2, (a.y + b.y) / 2, refs=[a.ref, b.ref], gap_mm=gap, recommended_mm=need)
        if unknown:
            self.skipped.append({"rule": "assembly", "why": f"package type not known for {', '.join(sorted(unknown)[:15])}; no JLCPCB spacing applied"})
        # the side an SMD part is soldered on is where its SMD pads are: a key switch footprint sits on the front but
        # carries its hotswap socket's pads on the back
        def smd_side(p):
            lays = {l for q in p.pads if q.kind == "smd" for l in q.layers if l.endswith(".Cu")}
            return "back" if lays == {self.b.copper_layers[-1]} else "front" if lays == {self.b.copper_layers[0]} else p.side

        smd = [p for p in self.b.parts if p.smd and not p.dnp]
        sides = {smd_side(p) for p in smd}
        if len(sides) == 2:
            back = sorted(p.ref for p in smd if smd_side(p) == "back")
            front = sorted(p.ref for p in smd if smd_side(p) == "front")
            self.add("assembly", "note", f"SMD parts on both sides ({len(front)} front, {len(back)} back)",
                     "Assembling both sides costs a second pass. Fine if intended.", "heuristic", refs=(back if len(back) <= len(front) else front)[:20])
        if self.b.edges:
            for p in parts:
                if re.match(r"^(J|P|CN)\d", p.ref):
                    continue  # connectors sit at the edge on purpose
                d = min((point_seg(q, s, e)[0] for q in _extent(p) for s, e in self.b.edges), default=math.inf)
                if d < JLCPCB["assembly_edge"]:
                    self.add("assembly", "note", f"{p.ref} is {d:.2f} mm from the board edge", "JLCPCB wants components over 0.3 mm from the edge "
                             "for economic assembly, and parts near the edge can collide with the conveyor rails.", "jlcpcb_edge", p.x, p.y, refs=[p.ref], edge_mm=d)

    # fab capability ------------------------------------------------------------------------------------

    def fab_limits(self, fab: str | None) -> None:
        if fab != "jlcpcb":
            return
        self.checked.append("fab")
        multi = len(self.b.copper_layers) > 2
        oz = max(1, round(max(self.b.copper_thickness.values() or [0.035]) / 0.035))
        table = JLCPCB["track_multilayer" if multi else "track"]
        min_track = table.get(oz, table[2] if oz > 2 else table[1])
        thin = [t for t in self.b.tracks if t.width < min_track - 1e-6]
        if thin:
            t = min(thin, key=lambda t: t.width)
            self.add("fab", "problem", f"{len(thin)} track(s) below JLCPCB's {min_track:g} mm minimum for {oz} oz, down to {t.width:g} mm",
                     "The fab can't etch them reliably.", "jlcpcb_pcb", t.start[0], t.start[1], t.layer, net=t.net)
        r = self.b.rules
        if r:
            for key, fab_min, what in (("min_clearance", min_track, "clearance"), ("min_track_width", min_track, "track width"),
                                       ("min_copper_edge_clearance", JLCPCB["edge"], "copper to edge"),
                                       ("min_via_diameter", JLCPCB["via_diameter"], "via diameter"),
                                       ("min_through_hole_diameter", JLCPCB["via_drill"], "hole size")):
                v = r.get(key)
                if v is not None and v < fab_min - 1e-6:
                    cls_note = ""
                    if key == "min_clearance" and self.b.net_classes:
                        lo = min((c.clearance for c in self.b.net_classes.values() if c.clearance is not None), default=None)
                        if lo is not None and lo >= fab_min:
                            cls_note = f" The net classes still ask for at least {lo:g} mm, so DRC catches tracks, but not every item."
                    self.add("fab", "note", f"the board's minimum {what} rule is {v:g} mm, below JLCPCB's {fab_min:g} mm",
                             "KiCad's DRC only checks the rules you set, so it won't catch what the fab can't make." + cls_note,
                             "jlcpcb_pcb", setting=key, board_value=v, fab_min=fab_min)


# --- helpers ----------------------------------------------------------------------------------------


def diff_pairs(nets: list[str]) -> list[tuple[str, str]]:
    """Net pairs named like a differential pair: X+/X-, X_P/X_N, XP/XN, X_DP/X_DM."""
    names = set(nets)
    out = []
    for n in nets:
        for pos, neg in (("+", "-"), ("_P", "_N"), ("P", "N"), ("DP", "DM")):
            if n.endswith(pos) and len(n) > len(pos):
                other = n[: -len(pos)] + neg
                base = n[: -len(pos)].split("/")[-1]
                if other in names and (pos not in ("P", "DP") or re.search(r"(USB|D|LVDS|ETH|TX|RX|CLK|HDMI)_?$", base, re.I)):
                    out.append((n, other))
                    break
    return out


def ipc2221_current(width_mm: float, delta_t: float, thickness_mm: float = 0.035, outer: bool = True) -> float:
    """Amps a track carries at this temperature rise, per IPC-2221 (SOURCES["ipc2221"])."""
    k = 0.048 if outer else 0.024
    area = (width_mm / 0.0254) * (thickness_mm / 0.0254)
    return k * delta_t ** 0.44 * area ** 0.725


def ipc2221_width(amps: float, delta_t: float, thickness_mm: float = 0.035, outer: bool = True) -> float:
    """Track width (mm) IPC-2221 asks for to carry this current at this temperature rise."""
    k = 0.048 if outer else 0.024
    area = (amps / (k * delta_t ** 0.44)) ** (1 / 0.725)
    return area / (thickness_mm / 0.0254) * 0.0254


def package_class(p: Part) -> tuple[str, str] | None:
    """(JLCPCB's package class, chip size) from the footprint name: chip, sot, sop, qfp, qfn, bga."""
    name = p.lib.split(":")[-1].upper()
    m = re.search(r"(?:^|_)(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)(?:_|METRIC|$)", name)
    if m and re.match(r"^(R|C|L|D|LED|FUSE|FB|F)_", name):
        return ("chip", m.group(1))
    m = re.search(r"(?:^|_)(0603|1005|1608|2012|3216|3225|4532|5025|6332)(?:_|METRIC|$)", name)
    if m and (re.match(r"^(R|C|L|D|LED|FB|F)\d", p.ref) or re.match(r"^(R|C|L|D|LED|FUSE|FB|F)_", name)) and len(p.copper_pads) == 2:
        return ("chip", _METRIC[m.group(1)])
    for pat, cls in ((r"BGA", "bga"), (r"^(QFN|DFN|VQFN|WQFN|UQFN|TQFN|SON|VSON|WSON|LGA|MLF|MLP)", "qfn"), (r"QFN|DFN", "qfn"),
                     (r"^(LQFP|TQFP|QFP|PQFP)", "qfp"), (r"^(SOIC|SOP|SSOP|TSSOP|MSOP|VSSOP|HTSSOP|SO-|SO_|TSOP)", "sop"),
                     (r"^(SOT|SC-70|SC-88|TSOT|SOD|D_SOD|D_SMA|D_SMB|D_SMC|TO-252|TO-263|DPAK)", "sot")):
        if re.search(pat, name):
            return (cls, "")
    return None


_METRIC = {"0603": "0201", "1005": "0402", "1608": "0603", "2012": "0805", "3216": "1206", "3225": "1210", "4532": "1812",
           "5025": "2010", "6332": "2512"}


def _smt_spacing(a: tuple[str, str], b: tuple[str, str]) -> float:
    if a[0] == b[0] == "chip":
        if a[1] == b[1] and a[1] in _SMT_CHIP:
            return _SMT_CHIP[a[1]]
        if {a[1], b[1]} <= {"0201", "0402", "01005"}:
            return 0.15
        if a[1] in ("1206", "1210", "1812", "2010", "2512") and b[1] in ("1206", "1210", "1812", "2010", "2512"):
            return 0.35
        return 0.18
    key = tuple(sorted((a[0], b[0]), key=["chip", "qfn", "qfp", "sop", "sot", "bga"].index))
    return _SMT.get(key, _SMT.get(key[::-1], 0.5))


def _extent(p: Part) -> list[tuple[float, float]]:
    """The package's outline: its pads' corners, as a convex hull (the fab outline isn't kept, pads bound most parts)."""
    pts = []
    for pd in p.copper_pads:
        a = math.radians(pd.angle)
        for sx, sy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            lx, ly = sx * pd.w / 2, sy * pd.h / 2
            pts.append((pd.x + lx * math.cos(a) + ly * math.sin(a), pd.y - lx * math.sin(a) + ly * math.cos(a)))
    return convex_hull(pts)


def _grow(poly: list[tuple[float, float]], d: float) -> list[tuple[float, float]]:
    if not poly:
        return poly
    cx, cy = sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly)
    out = []
    for x, y in poly:
        L = math.hypot(x - cx, y - cy) or 1
        out.append((x + (x - cx) / L * d, y + (y - cy) / L * d))
    return out


def _seg_hits_poly(s, e, poly) -> bool:
    if len(poly) < 3:
        return False
    if point_in_polygon(s, poly) or point_in_polygon(e, poly):
        return True
    return any(seg_seg(s, e, a, b) == 0 for a, b in zip(poly, poly[1:] + poly[:1]))


# --- KiCad's DRC ------------------------------------------------------------------------------------


def run_drc(board_file: Path) -> dict | None:
    """kicad-cli's DRC on a board file: unconnected items and violations, or None without kicad-cli."""
    cli = apps.find_kicad_cli()
    if not cli:
        return None
    out = Path(tempfile.mkdtemp(prefix="cadmate-drc-")) / "drc.json"
    try:
        parity = ["--schematic-parity"] if board_file.with_suffix(".kicad_sch").exists() else []
        # zones as they'd be after a refill: a saved board may hold unfilled pours, which DRC would count as unrouted
        proc = subprocess.run([cli, "pcb", "drc", "--format", "json", "--severity-all", "--units", "mm", "--refill-zones", *parity,
                               "--output", str(out), str(board_file)], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"kicad-cli drc failed: {e}"}
    if not out.exists():
        return {"error": f"kicad-cli drc wrote no report (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-800:]}"}
    return json.loads(out.read_text())


def _drc_findings(rep: dict) -> list[dict]:
    out = []
    un = rep.get("unconnected_items", [])
    if un:
        items = []
        for u in un[:10]:
            its = u.get("items", [])
            items.append({"what": "; ".join(i.get("description", "") for i in its)[:200],
                          "at": [its[0].get("pos", {}).get("x"), its[0].get("pos", {}).get("y")] if its else None})
        first = items[0]["at"] if items and items[0]["at"] else [None, None]
        # both ends of an unconnected item are on one net; KiCad sometimes labels the second end differently, so the
        # first end names it
        nets = sorted({m for u in un for i in u.get("items", [])[:1] for m in re.findall(r"\[([^\]]+)\]", i.get("description", ""))[:1]})
        out.append(_f("unconnected", "problem", f"{len(un)} connection(s) not routed, on {len(nets)} net(s)",
                      "The ratsnest still shows these: the board doesn't work until they are routed. KiCad counts each missing "
                      "link, so one net split in three counts twice; autorouters usually count nets.", "kicad_drc", first[0], first[1],
                      nets=nets[:30], items=items))
    kinds: dict[str, list] = {}
    for v in rep.get("violations", []):
        kinds.setdefault((v.get("type", "?"), v.get("severity", "?")), []).append(v)
    for (kind, sev), vs in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
        its = vs[0].get("items", [])
        pos = its[0].get("pos", {}) if its else {}
        out.append(_f("drc", "problem" if sev == "error" else "note", f"DRC {sev}: {len(vs)} × {kind}: {vs[0].get('description', '')}",
                      "From KiCad's own checker, with the board's rules.", "kicad_drc", pos.get("x"), pos.get("y"),
                      example="; ".join(i.get("description", "") for i in its)[:200]))
    par: dict[tuple, list] = {}
    for v in rep.get("schematic_parity", []):
        par.setdefault((v.get("type", "?"), v.get("severity", "?")), []).append(v)
    for (kind, sev), vs in sorted(par.items(), key=lambda kv: -len(kv[1])):
        its = vs[0].get("items", [])
        pos = its[0].get("pos", {}) if its else {}
        out.append(_f("parity", "problem" if sev == "error" else "warning",
                      f"Schematic parity {sev}: {len(vs)} × {kind}: {vs[0].get('description', '')}",
                      "The board and its schematic disagree: Update PCB from Schematic (F8) brings them back together, or the "
                      "schematic needs the change.", "kicad_drc", pos.get("x"), pos.get("y"),
                      example="; ".join(i.get("description", "") for i in its)[:200]))
    return out


# --- entry points -----------------------------------------------------------------------------------

_ORDER = {"problem": 0, "warning": 1, "note": 2}


def review_board(board: Board, currents: dict[str, float] | None = None, roles: dict[str, str] | None = None,
                 fab: str | None = "jlcpcb", limits: dict | None = None, drc: dict | None = None) -> dict:
    lim = {**LIMITS, **(limits or {})}
    r = Review(board, infer_roles(board, roles), currents, lim)
    r.decoupling()
    r.crystals()
    r.pairs()
    r.noisy()
    r.widths()
    r.vias(fab)
    r.return_path()
    r.assembly(fab)
    r.fab_limits(fab)
    findings = r.findings
    if drc is not None and "error" not in drc:
        found = _drc_findings(drc)
        for f in found:  # name the unrouted nets from the board's own copper; KiCad's item labels can be off
            if f["rule"] == "unconnected":
                from .freerouting import unrouted

                left = unrouted(board)
                if left:
                    f["values"]["nets"] = [u["net"] for u in left][:30]
                    f["values"]["items"] = [{"net": u["net"], "islands": u["islands"], "pads_apart": u["pads_apart"]} for u in left][:10]
                    f["what"] = f"{f['what'].split(', on ')[0]}, on {len(left)} net(s)"
                    pad = next((p for p in board.pads if left[0]["pads_apart"] and p.name == left[0]["pads_apart"][0]), None)
                    if pad is not None:
                        f["where"] = {**f.get("where", {}), "x": round(pad.x, 2), "y": round(pad.y, 2)}
        findings = found + findings
        r.checked.append("kicad_drc")
    elif drc is not None:
        r.skipped.append({"rule": "kicad_drc", "why": drc["error"]})
    else:
        r.skipped.append({"rule": "kicad_drc", "why": "kicad-cli not found, so unrouted nets and clearances weren't checked by KiCad; "
                                                       f"set {apps.KICAD_CLI_ENV}"})
    findings.sort(key=lambda f: _ORDER[f["severity"]])
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in _ORDER}
    by_role: dict[str, list[str]] = {}
    for net, v in r.roles.items():
        by_role.setdefault(v["role"], []).append(net)
    used = sorted({f["source"] for f in findings})
    return {
        "ok": True, "board": board.name, "counts": counts, "findings": findings[:LISTED],
        "more": len(findings) - LISTED if len(findings) > LISTED else 0,
        "checked": r.checked, "not_checked": r.skipped, "measured": r.measured,
        "net_roles": {k: (v if k not in ("signal",) else {"count": len(v), "examples": v[:10]}) for k, v in sorted(by_role.items())},
        "net_roles_note": "Inferred from net names, pin types, power symbols and the parts on each net. 'signal' means cadmate "
                          "couldn't tell what the net carries; pass roles={net: 'fast'|'sensitive'|'power'|...} to correct it.",
        "limits": {k: lim[k] for k in LIMITS}, "sources": {k: SOURCES[k] for k in used},
        "read": [board.name] + board.sources,
        "units": "mm, KiCad's board frame (Y down)",
    }


def review_file(path: str, **kw) -> dict:
    f = Path(path).expanduser()
    if not f.exists():
        raise ReviewError(f"no board at {f}")
    board = parse(f.read_text(errors="replace"), f.name)
    load_project(board, f.with_suffix(".kicad_pro"))
    load_schematic(board, f.parent, f.stem)
    drc = run_drc(f) if kw.pop("drc", True) else None
    return review_board(board, drc=drc, **kw)


def review_live(**kw) -> dict:
    """The board open in KiCad's PCB editor, unsaved edits included."""
    from . import kicad_pcb_live

    kicad, live_board = kicad_pcb_live.connect()
    board = parse(live_board.get_as_string(), live_board.name)
    project = getattr(getattr(live_board.document, "project", None), "path", "") or ""
    folder = Path(project) if project else None
    if folder and folder.is_file():
        folder = folder.parent
    if folder:
        pro = folder / Path(live_board.name).with_suffix(".kicad_pro").name
        load_project(board, pro if pro.exists() else next(iter(folder.glob("*.kicad_pro")), None))
        load_schematic(board, folder, Path(live_board.name).stem)
    drc = None
    if kw.pop("drc", True) and apps.find_kicad_cli():
        tmp = Path(tempfile.mkdtemp(prefix="cadmate-review-"))
        copy = tmp / Path(live_board.name).name
        live_board.save_as(str(copy), overwrite=True, include_project=True)
        drc = run_drc(copy)
    out = review_board(board, drc=drc, **kw)
    out["source"] = "the open PCB editor, unsaved edits included"
    return out

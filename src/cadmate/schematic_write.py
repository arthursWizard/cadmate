"""Write a new KiCad schematic from parts and nets, drawn the way the kicad-schematics skill asks.

The caller says what the circuit is: the parts (library symbol, reference, value, footprint, and where each sits, or
leave that to the first layout: the biggest part of each block first, each part wired to it placed beside the pin it
connects to, the rest in a row) and the nets (which pins connect). This module draws it:

- wires between pins of the same block when a clean path exists (straight, one bend, or out from the pin then one
  bend), never through a part, a field, another net's pin or wire, and without crossing other wires;
- a net label on a short stub where a net leaves its block, or where no clean wire path exists;
- a power symbol at each pin of a supply or ground net (ground pointing down, supplies up), and a PWR_FLAG on supply
  nets nothing drives, so ERC doesn't complain about undriven power;
- a no-connect flag on pins said to be unused, and junction dots where three or more connections meet;
- fields beside each part, and a heading over each named block.

It writes new sheets. It doesn't edit a schematic someone drew: that waits for KiCad 11's schematic API. The result is
then checked with review_schematic (readability) and KiCad's ERC, and their findings come back with the drawing.

Coordinates are KiCad's schematic frame: mm, Y down, on the 1.27 mm grid.
"""

from __future__ import annotations

import re
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path

from . import kicad_libs, project, schematic_review, sexpr, symbol, versions

GRID = 1.27
STEP = 2.54
EPS = 1e-6
FORMAT = versions.SCHEMATIC_FORMAT
GROUND = re.compile(r"^(GND\w*|\w*GND|VSS\w*|V-|-\d.*)$", re.IGNORECASE)  # ground or a negative rail: the symbol points down
SUPPLY = re.compile(r"^(\+.*|-.*|GND\w*|\w*GND|V(CC|DD|SS|BUS|BAT|IN)\w*)$", re.IGNORECASE)  # names drawn as power symbols
POWER_LIB = "power"
CLEARANCE = 1.0  # mm a searched wire keeps from parts and text


class WriteError(ValueError):
    pass


def snap(v: float, g: float = GRID) -> float:
    return round(round(v / g) * g, 4)


def _uuid() -> str:
    return str(uuidlib.uuid4())


Box = tuple[float, float, float, float]
Pt = tuple[float, float]


@dataclass(eq=False)  # one object per placed unit: compared and hashed by identity
class Part:
    ref: str
    lib_id: str
    value: str
    footprint: str
    at: Pt
    rot: int
    mirror: str
    unit: int
    fields: dict
    block: str
    sym: symbol.Symbol
    text: str
    power: bool = False
    pins: dict[str, tuple[Pt, Pt, symbol.Pin]] = field(default_factory=dict)  # number -> (tip, outward, pin)
    keepout: Box = (0, 0, 0, 0)
    body: Box = (0, 0, 0, 0)
    field_at: dict[str, tuple[Pt, str]] = field(default_factory=dict)  # name -> (point, justify)
    field_boxes: list[Box] = field(default_factory=list)


def _xf(px: float, py: float, at: Pt, rot: int, mirror: str) -> Pt:
    x, y = schematic_review._xf(px, py, at, rot, mirror)
    return (round(x, 4), round(y, 4))


def _box(pts: list[Pt]) -> Box:
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def _place(p: Part) -> None:
    """Pin tips, outward directions, body and keepout boxes, and field positions for a placed part."""
    pts = []
    for r in [r for r in p.sym.rects if r.unit in (p.unit, 0)]:
        pts += [_xf(r.xmin, r.ymin, p.at, p.rot, p.mirror), _xf(r.xmax, r.ymax, p.at, p.rot, p.mirror)]
    for pl in [g for g in p.sym.polylines if g.unit in (p.unit, 0)]:
        pts += [_xf(x, y, p.at, p.rot, p.mirror) for x, y in pl.pts]
    p.pins = {}
    tips = []
    for pin in p.sym.pins_of(p.unit):
        if pin.style not in (0, 1):
            continue
        tip = _xf(pin.x, pin.y, p.at, p.rot, p.mirror)
        inner = _xf(*pin.inner, p.at, p.rot, p.mirror)
        d = (tip[0] - inner[0], tip[1] - inner[1])
        n = max(abs(d[0]), abs(d[1])) or 1.0
        out = (round(d[0] / n), round(d[1] / n)) if pin.length else (0, 0)
        for num in pin.numbers:
            p.pins[num] = (tip, out, pin)
        tips += [tip, inner]
    if not pts:
        pts = tips or [p.at]
    p.body = _box(pts)
    p.keepout = _box(pts + tips)
    if p.power:  # its value where the library puts it, upright
        lp = p.sym.properties.get("Value")
        if lp and not lp.hidden:
            x, y = _xf(lp.x, lp.y, p.at, p.rot, "")
            p.field_boxes = [schematic_review._text_box(p.value, x, y, 0, 1.27, "center", "center")]
        return
    k = p.keepout
    narrow = (k[2] - k[0]) <= 7.62 and (k[3] - k[1]) >= (k[2] - k[0])
    if narrow:  # a vertical R, C, diode: both fields to its right
        cy = snap((k[1] + k[3]) / 2)
        p.field_at = {"Reference": ((snap(k[2] + GRID), snap(cy - GRID)), "left"),
                      "Value": ((snap(k[2] + GRID), snap(cy + GRID)), "left")}
    else:  # anything else: reference above, value below, at its left edge
        p.field_at = {"Reference": ((snap(k[0]), snap(k[1] - GRID)), "left bottom"),
                      "Value": ((snap(k[0]), snap(k[3] + GRID + 1.27)), "left bottom")}
    for name, own in (("Reference", "ref_at"), ("Value", "value_at")):
        if p.fields.get(own):
            x, y = p.fields[own]
            p.field_at[name] = ((snap(x), snap(y)), "left")
    p.field_boxes = []
    for name, ((x, y), just) in p.field_at.items():
        text = p.ref if name == "Reference" else p.value
        hj = "left" if "left" in just else "center"
        vj = "bottom" if "bottom" in just else "center"
        p.field_boxes.append(schematic_review._text_box(text, x, y, 0, 1.27, hj, vj))


# --- geometry helpers ---------------------------------------------------------------------------------------------


def _inside(pt: Pt, b: Box, m: float = EPS) -> bool:
    return b[0] + m < pt[0] < b[2] - m and b[1] + m < pt[1] < b[3] - m


def _seg_hits_box(a: Pt, b: Pt, box: Box) -> bool:
    """Does an axis-aligned segment pass through the box's interior?"""
    x0, y0, x1, y1 = box
    if abs(a[1] - b[1]) < EPS:  # horizontal
        lo, hi = sorted((a[0], b[0]))
        return y0 + EPS < a[1] < y1 - EPS and lo < x1 - EPS and hi > x0 + EPS
    lo, hi = sorted((a[1], b[1]))
    return x0 + EPS < a[0] < x1 - EPS and lo < y1 - EPS and hi > y0 + EPS


def _on_seg(p: Pt, a: Pt, b: Pt, ends: bool = True) -> bool:
    if abs(a[0] - b[0]) < EPS and abs(p[0] - a[0]) < EPS:
        lo, hi = sorted((a[1], b[1]))
        return (lo - EPS <= p[1] <= hi + EPS) if ends else (lo + EPS < p[1] < hi - EPS)
    if abs(a[1] - b[1]) < EPS and abs(p[1] - a[1]) < EPS:
        lo, hi = sorted((a[0], b[0]))
        return (lo - EPS <= p[0] <= hi + EPS) if ends else (lo + EPS < p[0] < hi - EPS)
    return False


def _touch(a: Pt, b: Pt, c: Pt, d: Pt) -> bool:
    """Do two axis-aligned segments share any point (cross, overlap or touch)?"""
    ah = abs(a[1] - b[1]) < EPS
    ch = abs(c[1] - d[1]) < EPS
    if ah == ch:  # parallel: share a point only if collinear and overlapping
        if ah and abs(a[1] - c[1]) < EPS:
            return min(a[0], b[0]) <= max(c[0], d[0]) + EPS and min(c[0], d[0]) <= max(a[0], b[0]) + EPS
        if not ah and abs(a[0] - c[0]) < EPS:
            return min(a[1], b[1]) <= max(c[1], d[1]) + EPS and min(c[1], d[1]) <= max(a[1], b[1]) + EPS
        return False
    h1, h2, v1, v2 = (a, b, c, d) if ah else (c, d, a, b)
    return (min(h1[0], h2[0]) - EPS <= v1[0] <= max(h1[0], h2[0]) + EPS
            and min(v1[1], v2[1]) - EPS <= h1[1] <= max(v1[1], v2[1]) + EPS)


def _overlap_collinear(a: Pt, b: Pt, c: Pt, d: Pt) -> bool:
    """Collinear with more than a point in common."""
    ah, ch = abs(a[1] - b[1]) < EPS, abs(c[1] - d[1]) < EPS
    if ah and ch and abs(a[1] - c[1]) < EPS:
        return min(max(a[0], b[0]), max(c[0], d[0])) - max(min(a[0], b[0]), min(c[0], d[0])) > EPS
    if not ah and not ch and abs(a[0] - c[0]) < EPS:
        return min(max(a[1], b[1]), max(c[1], d[1])) - max(min(a[1], b[1]), min(c[1], d[1])) > EPS
    return False


def _cross_point(a: Pt, b: Pt, c: Pt, d: Pt) -> Pt | None:
    """Where a horizontal and a vertical segment meet (None for parallel ones)."""
    ah, ch = abs(a[1] - b[1]) < EPS, abs(c[1] - d[1]) < EPS
    if ah == ch:
        return None
    return (c[0], a[1]) if ah else (a[0], c[1])


def _grid_points(a: Pt, b: Pt) -> list[Pt]:
    """Every 1.27 mm grid point along an axis-aligned segment, ends included."""
    n = round((abs(a[0] - b[0]) + abs(a[1] - b[1])) / GRID)
    if n == 0:
        return [a]
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]


def _length(path: list[Pt]) -> float:
    return sum(abs(path[i][0] - path[i + 1][0]) + abs(path[i][1] - path[i + 1][1]) for i in range(len(path) - 1))


# --- the drawing --------------------------------------------------------------------------------------------------


class Drawing:
    def __init__(self, parts: list[Part]):
        self.parts = parts
        self.wires: list[tuple[Pt, Pt, str]] = []
        self.labels: list[tuple[str, Pt, int]] = []
        self.power: list[tuple[Part, str]] = []  # power symbols, with their net
        self.nc: list[Pt] = []
        self.texts: list[tuple[str, Pt]] = []
        self.owner: dict[tuple[float, float], str] = {}  # point -> net, for pin tips and label/power points
        self.extra_boxes: list[Box] = []  # power symbols and labels drawn so far

    def key(self, p: Pt) -> tuple[float, float]:
        return (round(p[0], 3), round(p[1], 3))

    def ok(self, path: list[Pt], net: str, own_end: Pt | None = None) -> bool:
        """Can these wires be drawn for net without touching anything of another net, crossing a wire, or running
        through a part or text?"""
        for i in range(len(path) - 1):
            a, b = path[i], path[i + 1]
            if abs(a[0] - b[0]) > EPS and abs(a[1] - b[1]) > EPS:
                return False
            if abs(a[0] - b[0]) < EPS and abs(a[1] - b[1]) < EPS:
                continue
            for p in self.parts:
                if _seg_hits_box(a, b, p.body) or any(_seg_hits_box(a, b, fb) for fb in p.field_boxes):
                    return False
                if not p.power and _seg_hits_box(a, b, p.keepout):
                    return False
            if any(_seg_hits_box(a, b, bx) for bx in self.extra_boxes):
                return False
            for pt, owner in self.owner.items():
                if owner != net and _on_seg(pt, a, b):
                    return False
            for c, d, other in self.wires:
                if other != net:
                    if _touch(a, b, c, d):
                        return False
                elif _overlap_collinear(a, b, c, d):
                    return False
                elif _touch(a, b, c, d):
                    x = _cross_point(a, b, c, d)
                    if x is not None and _on_seg(x, a, b, ends=False) and _on_seg(x, c, d, ends=False):
                        return False  # a same-net crossing reads as a four-way junction
        return True

    def add(self, path: list[Pt], net: str) -> None:
        for i in range(len(path) - 1):
            if self.key(path[i]) != self.key(path[i + 1]):
                self.wires.append((path[i], path[i + 1], net))

    def routes(self, a: Pt, a_out: Pt, b: Pt, b_out: Pt | None) -> list[list[Pt]]:
        """Candidate paths from a to b, shortest and simplest first."""
        cands = [[a, b], [a, (b[0], a[1]), b], [a, (a[0], b[1]), b]]
        for k in (1, 2, 3, 4):
            e = (snap(a[0] + a_out[0] * STEP * k), snap(a[1] + a_out[1] * STEP * k))
            cands += [[a, e, (b[0], e[1]), b], [a, e, (e[0], b[1]), b]]
            if b_out is not None:
                f = (snap(b[0] + b_out[0] * STEP * k), snap(b[1] + b_out[1] * STEP * k))
                cands += [[a, (f[0], a[1]), f, b], [a, (a[0], f[1]), f, b], [a, e, (f[0], e[1]), f, b], [a, e, (e[0], f[1]), f, b]]
        out = []
        for c in cands:
            c = [c[0]] + [q for i, q in enumerate(c[1:], 1) if self.key(q) != self.key(c[i - 1])]
            if all(abs(c[i][0] - c[i + 1][0]) < EPS or abs(c[i][1] - c[i + 1][1]) < EPS for i in range(len(c) - 1)):
                out.append(c)
        return sorted(out, key=lambda c: _length(c) + 2.0 * (len(c) - 2))

    def connect(self, net: str, pins: list[tuple[Part, str]]) -> list[tuple[Part, str]]:
        """Wire the pins together; returns the pins that couldn't be reached by wire (left for labels)."""
        if len(pins) < 2:
            return []
        tree_pts: list[tuple[Pt, Pt | None]] = []  # points on the net's wires so far, with an outward if a pin tip
        segs: list[tuple[Pt, Pt]] = []
        first = pins[0]
        tree_pts.append((first[0].pins[first[1]][0], first[0].pins[first[1]][1]))
        left, failed = list(pins[1:]), []
        while left:
            best = None
            for part, num in left:
                tip, out, _ = part.pins[num]
                targets = list(tree_pts)
                for c, d in segs:  # straight onto a wire: project the tip (or its first step out)
                    for src in (tip, (snap(tip[0] + out[0] * STEP), snap(tip[1] + out[1] * STEP))):
                        for q in ((src[0], c[1]), (c[0], src[1])):
                            if _on_seg(q, c, d, ends=False):
                                targets.append((q, None))
                for tgt, tgt_out in targets:
                    for path in self.routes(tip, out, tgt, tgt_out):
                        cost = _length(path) + 2.0 * (len(path) - 2)
                        if best is not None and cost >= best[0]:
                            break
                        if self.ok(path, net):
                            best = (cost, part, num, path)
                            break
            if best is None:  # no simple path: search the grid around the parts
                goals = {self.key(q) for q, _ in tree_pts}
                for c, d in segs:
                    goals |= {self.key(q) for q in _grid_points(c, d)}
                for part, num in left:
                    tip, out, _ = part.pins[num]
                    path = self.maze(tip, out, net, goals)
                    if path and (best is None or _length(path) < best[0]) and self.ok(path, net):
                        best = (_length(path), part, num, path)
            if best is None:
                failed += left
                break
            _, part, num, path = best
            self.add(path, net)
            segs += [(path[i], path[i + 1]) for i in range(len(path) - 1)]
            tree_pts += [(q, None) for q in path[1:-1]]
            tree_pts.append((path[0], part.pins[num][1]))
            left.remove((part, num))
        return failed

    def maze(self, start: Pt, out: Pt, net: str, goals: set) -> list[Pt] | None:
        """Shortest path with few bends on the 1.27 mm grid from a pin to any goal point, around everything else."""
        import heapq
        pts = [start] + [(g[0], g[1]) for g in goals]
        x0, y0 = min(p[0] for p in pts) - 25.4, min(p[1] for p in pts) - 25.4
        x1, y1 = max(p[0] for p in pts) + 25.4, max(p[1] for p in pts) + 25.4
        boxes = [p.body for p in self.parts] + [p.keepout for p in self.parts if not p.power] + \
            [b for p in self.parts for b in p.field_boxes] + self.extra_boxes
        c = CLEARANCE  # keep searched wires a little off parts and text, not hugging them
        raw = [b for b in boxes if b[2] > x0 and b[0] < x1 and b[3] > y0 and b[1] < y1]
        boxes = [(b[0] - c, b[1] - c, b[2] + c, b[3] + c) for b in raw]
        others = {k for k, n in self.owner.items() if n != net}
        for a, b, n in self.wires:
            if n != net:
                others |= {self.key(q) for q in _grid_points(a, b)}
        same = set()
        for a, b, n in self.wires:
            if n == net:
                same |= {self.key(q) for q in _grid_points(a, b)}
        same -= goals

        def free(k) -> bool:
            if k in others or k in same:
                return False
            return not any(_inside(k, b) for b in boxes)
        g = GRID
        sk = self.key(start)
        dirs = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        heap = [(0.0, sk, out if out != (0, 0) else None)]
        seen: dict = {}
        prev: dict = {}
        best: dict = {}
        while heap:
            cost, k, d = heapq.heappop(heap)
            if (k, d) in seen:
                continue
            seen[(k, d)] = cost
            if k in goals and k != sk:
                path = [k]
                state = (k, d)
                while state in prev:
                    state = prev[state]
                    path.append(state[0])
                path.reverse()
                return [path[0]] + [q for i, q in enumerate(path[1:-1], 1)
                                    if not ((path[i - 1][0] == q[0] == path[i + 1][0]) or (path[i - 1][1] == q[1] == path[i + 1][1]))] + [path[-1]]
            for nd in ([d] if k == sk and d else dirs):
                if d and nd == (-d[0], -d[1]):
                    continue
                nk = self.key((k[0] + nd[0] * g, k[1] + nd[1] * g))
                if not (x0 <= nk[0] <= x1 and y0 <= nk[1] <= y1):
                    continue
                if nk not in goals and not free(nk):
                    continue
                if nk in goals and any(_inside(((k[0] + nk[0]) / 2, (k[1] + nk[1]) / 2), b) for b in raw):
                    continue
                nc = cost + 1 + (4 if d and nd != d else 0)
                if (nk, nd) not in seen and nc < best.get((nk, nd), 1e18):
                    best[(nk, nd)] = nc
                    prev[(nk, nd)] = (k, d)
                    heapq.heappush(heap, (nc, nk, nd))
        return None

    def stub(self, tip: Pt, out: Pt, net: str, lengths=(STEP, 2 * STEP)) -> Pt | None:
        if out == (0, 0):
            return None
        for n in lengths:
            end = (snap(tip[0] + out[0] * n), snap(tip[1] + out[1] * n))
            if self.ok([tip, end], net) and not any(_inside(end, p.keepout, -0.5) for p in self.parts if not p.power):
                return end
        return None


def _resolve_pin(part_units: list[Part], spec: str) -> tuple[Part, str]:
    ref, _, pin = spec.partition(".")
    if not pin:
        raise WriteError(f"{spec!r}: write pins as REF.PIN, e.g. U1.3 or U1.VDD")
    for p in part_units:
        if pin in p.pins:
            return p, pin
    all_pins = {num: p for p in part_units for num in p.pins}
    named = [(p, num) for p in part_units for num, (_, _, pn) in p.pins.items() if pn.name == pin]
    if not named:
        named = [(p, num) for p in part_units for num, (_, _, pn) in p.pins.items() if pn.name.lower() == pin.lower()]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        raise WriteError(f"{spec}: pin name {pin!r} is on pins {sorted(n for _, n in named)}; use the number")
    units = {p.unit for p in part_units}
    other = part_units[0].sym.pins
    if any(pin in pn.numbers for pn in other):
        u = next(pn.unit for pn in other if pin in pn.numbers)
        raise WriteError(f"{spec}: pin {pin} is on unit {u} of {ref}, which isn't placed (placed: {sorted(units)})")
    raise WriteError(f"{spec}: {ref} has no pin {pin!r}; pins: {sorted(all_pins)[:40]}")


def _pin_wants(net: str | None) -> Pt | None:
    """The way a pin on a power net should face: supplies up, grounds down."""
    if not net or not SUPPLY.match(net):
        return None
    return (0, 1) if GROUND.match(net) else (0, -1)


def _clear(p: Part, others: list[Part], gap: float = 2.54) -> bool:
    mine = [p.keepout] + p.field_boxes
    theirs = [b for q in others for b in [q.keepout] + q.field_boxes]
    return not any(a[0] < b[2] + gap and b[0] < a[2] + gap and a[1] < b[3] + gap and b[1] < a[3] + gap
                   for a in mine for b in theirs)


def _beside(p: Part, num: str, anchor: Part, anum: str, pin_net: dict, done: list[Part]) -> bool:
    """Put p so its pin num sits a short wire from anchor's pin anum: facing it in line, or an L away, power pins
    pointing the way their symbols go. False when nothing near is clear."""
    a_tip, a_out, _ = anchor.pins[anum]
    if a_out == (0, 0):
        return False
    best = None
    for rot in (0, 90, 180, 270):
        p.rot, p.at = rot, (0.0, 0.0)
        _place(p)
        tip, out, _ = p.pins[num]
        if out == a_out or out == (0, 0):
            continue
        bad = sum(1 for n, (_, o, _) in p.pins.items()
                  if (w := _pin_wants(pin_net.get((p.ref, p.unit, n)))) and o != w)
        score = bad * 10 + (0 if out == (-a_out[0], -a_out[1]) else 1)
        if best is None or score < best[0]:
            best = (score, rot)
    if best is None:
        return False
    p.rot = best[1]
    p.at = (0.0, 0.0)
    _place(p)
    tip, out, _ = p.pins[num]
    inline = out == (-a_out[0], -a_out[1])
    side = (a_out[1], a_out[0])  # across the anchor pin's direction
    shift = 0 if inline else STEP
    tries = sorted(((dist, k) for dist in (7.62, 10.16, 12.7, 15.24, 17.78) for k in range(-4, 5)),
                   key=lambda t: t[0] + abs(t[1]) * 2 * STEP)  # shortest wire first
    for dist, k in tries:
        tx = a_tip[0] + a_out[0] * dist - out[0] * shift + side[0] * k * STEP
        ty = a_tip[1] + a_out[1] * dist - out[1] * shift + side[1] * k * STEP
        p.at = (snap(tx - tip[0], GRID), snap(ty - tip[1], GRID))
        _place(p)
        if _clear(p, done):
            return True
    return False


def _layout(parts: list[Part], auto: set[int], paper: tuple[float, float], pin_net: dict) -> None:
    """A first layout for parts given no position. In each block the part with the most pins goes first; each part
    wired to one already placed goes next to that pin (a pull-up beside the pin it pulls, a resistor in line with
    the next), the rest in a row beside them. Blocks go left to right in rows across the sheet. A starting point to
    refine, not a finished drawing."""
    blocks: dict[str, list[Part]] = {}
    for p in parts:
        if id(p) in auto:
            blocks.setdefault(p.block, []).append(p)
    signal: dict[str, list[tuple[Part, str]]] = {}
    for p in parts:
        for num in p.pins:
            net = pin_net.get((p.ref, p.unit, num))
            if net and not SUPPLY.match(net):
                signal.setdefault(net, []).append((p, num))
    margin, gap = 30.48, 20.32
    x, y, row_h = margin, margin, 0.0
    for members in blocks.values():
        members.sort(key=lambda p: -len(p.pins))
        done: list[Part] = []
        todo = list(members)
        waited: set[int] = set()
        while todo:
            p = todo.pop(0)
            if not done:
                p.at = (0.0, 0.0)
                _place(p)
                done.append(p)
            else:
                ok = False
                for num in sorted(p.pins):
                    net = pin_net.get((p.ref, p.unit, num))
                    for q, qn in signal.get(net, []) if net else []:
                        if q in done and _beside(p, num, q, qn, pin_net, done):
                            ok = True
                            break
                    if ok:
                        break
                if not ok:
                    linked = any(q in todo for n in p.pins for q, _ in signal.get(pin_net.get((p.ref, p.unit, n)) or "", []))
                    if linked and id(p) not in waited:  # place what it hangs off first
                        waited.add(id(p))
                        todo.append(p)
                        continue
                    p.at = (0.0, 0.0)
                    _place(p)
                    right = max(b[2] for q in done for b in [q.keepout] + q.field_boxes)
                    top = min(q.keepout[1] for q in done)
                    p.at = (snap(right + 7.62 - p.keepout[0], STEP), snap(top - p.keepout[1], STEP))
                    _place(p)
                    while not _clear(p, done):
                        p.at = (p.at[0] + STEP, p.at[1])
                        _place(p)
                done.append(p)
        # move the block to its place on the sheet
        bx0 = min(b[0] for q in done for b in [q.keepout] + q.field_boxes)
        by0 = min(b[1] for q in done for b in [q.keepout] + q.field_boxes)
        bw = max(b[2] for q in done for b in [q.keepout] + q.field_boxes) - bx0
        bh = max(b[3] for q in done for b in [q.keepout] + q.field_boxes) - by0
        if x > margin and x + bw > paper[0] - margin:
            x, y, row_h = margin, y + row_h + gap, 0.0
        for p in done:
            p.at = (snap(p.at[0] - bx0 + x, STEP), snap(p.at[1] - by0 + y, STEP))
            _place(p)
        x += bw + gap
        row_h = max(row_h, bh)


def write(path: str, parts: list[dict], nets: dict[str, list[str]], no_connect: list[str] | None = None,
          notes: list[dict] | None = None, paper: str = "A4", title: str = "", libraries: dict[str, str] | None = None,
          show_names: list[str] | None = None, replace: bool = False, plan_only: bool = False, erc: bool = True) -> dict:
    out_path = Path(path).expanduser()
    if out_path.suffix != ".kicad_sch":
        raise WriteError("path must end in .kicad_sch")
    if out_path.exists() and not replace and not plan_only:
        raise WriteError(f"{out_path} exists; this writes new sheets (replace=True to write over it)")
    if paper not in schematic_review.PAPER:
        raise WriteError(f"paper must be one of {sorted(schematic_review.PAPER)}")
    size = schematic_review.PAPER[paper]
    libs = kicad_libs.Libraries(out_path.parent, libraries)

    placed: list[Part] = []
    by_ref: dict[str, list[Part]] = {}
    for spec in parts:
        ref = str(spec.get("ref", "")).strip()
        lib_id = str(spec.get("symbol", "")).strip()
        if not ref or not lib_id:
            raise WriteError(f"each part needs ref and symbol: {spec}")
        try:
            text, sym = libs.get(lib_id)
        except (kicad_libs.LibraryError, OSError, sexpr.SexprError, ValueError) as e:
            raise WriteError(f"{ref}: {e}") from None
        unit = int(spec.get("unit", 1))
        if unit < 1 or unit > sym.unit_count:
            raise WriteError(f"{ref}: {lib_id} has {sym.unit_count} unit(s)")
        if any(p.unit == unit for p in by_ref.get(ref, [])):
            raise WriteError(f"{ref} unit {unit} is listed twice")
        rot = int(spec.get("rot", 0)) % 360
        if rot % 90:
            raise WriteError(f"{ref}: rot must be 0, 90, 180 or 270")
        at = spec.get("at")
        p = Part(ref=ref, lib_id=lib_id, value=str(spec.get("value") or sym.prop("Value") or ""),
                 footprint=str(spec.get("footprint") or sym.prop("Footprint") or ""),
                 at=(snap(float(at[0])), snap(float(at[1]))) if at else None, rot=rot, mirror=str(spec.get("mirror", "")),
                 unit=unit, fields=dict(spec.get("fields") or {}), block=str(spec.get("block") or ""), sym=sym, text=text)
        for k in ("ref_at", "value_at"):
            if spec.get(k):
                p.fields[k] = spec[k]
        placed.append(p)
        by_ref.setdefault(ref, []).append(p)
    auto = {id(p) for p in placed if p.at is None}
    for p in placed:
        if p.at is None:
            p.at = (0.0, 0.0)
        _place(p)

    # which pin is on which net
    pin_net: dict[tuple[str, int, str], str] = {}
    net_pins: dict[str, list[tuple[Part, str]]] = {}
    for net, specs in nets.items():
        if not net or not isinstance(specs, list):
            raise WriteError(f"nets: {net!r} must name a list of pins")
        for s in specs:
            ref = s.partition(".")[0]
            if ref not in by_ref:
                raise WriteError(f"net {net}: no part {ref}")
            part, num = _resolve_pin(by_ref[ref], s)
            k = (part.ref, part.unit, num)
            if k in pin_net and pin_net[k] != net:
                raise WriteError(f"{s} is on two nets: {pin_net[k]} and {net}")
            if k not in pin_net:
                pin_net[k] = net
                net_pins.setdefault(net, []).append((part, num))
    nc_pins = []
    for s in no_connect or []:
        ref = s.partition(".")[0]
        if ref not in by_ref:
            raise WriteError(f"no_connect: no part {ref}")
        part, num = _resolve_pin(by_ref[ref], s)
        if (part.ref, part.unit, num) in pin_net:
            raise WriteError(f"{s} is in no_connect and on net {pin_net[(part.ref, part.unit, num)]}")
        nc_pins.append((part, num))
    _layout(placed, auto, size, pin_net)

    geometry = {f"{p.ref}" + (f" unit {p.unit}" if p.sym.unit_count > 1 else ""): {
        "at": list(p.at), "rot": p.rot, "outline": [round(v, 2) for v in p.keepout],
        "pins": {n: {"at": list(t), "faces": {(-1, 0): "left", (1, 0): "right", (0, -1): "up", (0, 1): "down"}.get(o, "-"),
                     "name": pn.name, "net": pin_net.get((p.ref, p.unit, n))} for n, (t, o, pn) in p.pins.items()}}
        for p in placed}
    if plan_only:
        return {"ok": True, "plan_only": True, "parts": geometry,
                "note": "outline is each part's extent with its pins; pins at their connection points. Place parts on "
                        "the 1.27 mm grid with room between blocks, then write without plan_only."}

    d = Drawing(placed)
    for p in placed:
        for num, (tip, _, _) in p.pins.items():
            net = pin_net.get((p.ref, p.unit, num))
            d.owner[d.key(tip)] = net or f"~{p.ref}.{num}"

    power_parts: list[Part] = []
    flipped, label_only, wired, flags = [], [], [], []
    power_ids: dict[str, str] = {}
    for net in net_pins:
        if SUPPLY.match(net):
            lib_id = f"{POWER_LIB}:{net}"
            try:
                libs.get(lib_id)
                power_ids[net] = lib_id
            except (kicad_libs.LibraryError, OSError, ValueError):
                pass
    pwr_n = [0]

    def power_symbol(lib_id: str, value: str, at: Pt, rot: int) -> Part:
        text, sym = libs.get(lib_id)
        pwr_n[0] += 1
        p = Part(ref=f"#PWR{pwr_n[0]:02d}" if "FLAG" not in lib_id else f"#FLG{pwr_n[0]:02d}", lib_id=lib_id, value=value,
                 footprint="", at=at, rot=rot, mirror="", unit=1, fields={}, block="", sym=sym, text=text, power=True)
        _place(p)
        return p

    show = set(show_names or [])
    for net, pins in net_pins.items():
        if net in power_ids:  # a power symbol at each pin
            ground = bool(GROUND.match(net))
            for part, num in pins:
                tip, out, _ = part.pins[num]
                end = d.stub(tip, out, net) or tip
                if end != tip:
                    d.add([tip, end], net)
                down = (0, 1)
                pointing = down if ground else (0, -1)
                rot = 0
                if out == (-pointing[0], -pointing[1]):  # the pin faces the wrong way for this symbol
                    rot = 180
                    flipped.append(f"{part.ref}.{num}")
                ps = power_symbol(power_ids[net], net, end, rot)
                d.extra_boxes += [ps.body] + ps.field_boxes
                d.owner[d.key(end)] = net
                d.power.append((ps, net))
                power_parts.append(ps)
            if not any(part.pins[num][2].etype == "power_out" for part, num in pins):
                flags.append(net)  # nothing on the board drives it: ERC wants a PWR_FLAG
            continue
        groups: dict[str, list[tuple[Part, str]]] = {}
        for part, num in pins:
            groups.setdefault(part.block, []).append((part, num))
        needs_label = len(groups) > 1 or net in show
        for members in groups.values():
            failed = d.connect(net, members)
            if len(members) > 1 and len(failed) < len(members) - 1:
                wired.append(net)
            reached = [m for m in members if m not in failed]  # always holds the first pin
            todo = list(failed) + ([reached[0]] if needs_label or failed else [])
            for part, num in dict.fromkeys(todo):
                tip, out, _ = part.pins[num]
                end = d.stub(tip, out, net)
                if end is None:
                    end = tip
                else:
                    d.add([tip, end], net)
                angle = {(1, 0): 0, (-1, 0): 180, (0, -1): 90, (0, 1): 270}.get(tuple(out), 0)
                d.labels.append((net, end, angle))
                d.owner[d.key(end)] = net
                w = len(net) * schematic_review.CHAR_W * 1.27
                lx, ly = end
                d.extra_boxes.append({0: (lx, ly - 1.6, lx + w, ly), 180: (lx - w, ly - 1.6, lx, ly),
                                      90: (lx - 1.6, ly - w, lx, ly), 270: (lx - 1.6, ly, lx, ly + w)}[angle])
                if failed and (part, num) in failed:
                    label_only.append(f"{part.ref}.{num}")

    # PWR_FLAG on power nets nothing drives
    unflagged = []
    for net in dict.fromkeys(flags):
        ps_sites = [(ps.at, ps) for ps, n in d.power if n == net]
        done = False
        for at, ps in ps_sites:
            for dx in (7.62, -7.62, 10.16, -10.16, 12.7, -12.7):
                f = (snap(at[0] + dx), at[1])
                if not d.ok([at, f], net) or any(_inside(f, p.keepout, -0.5) for p in placed):
                    continue
                try:
                    flag = power_symbol(f"{POWER_LIB}:PWR_FLAG", "PWR_FLAG", f, 0)
                except (kicad_libs.LibraryError, OSError, ValueError):
                    break
                taken = [b for q in placed + power_parts for b in q.field_boxes] + d.extra_boxes
                if any(schematic_review._overlap(fb, b) for fb in flag.field_boxes + [flag.body] for b in taken):
                    pwr_n[0] -= 1  # its text would land on other text: try further along
                    continue
                d.add([at, f], net)
                d.owner[d.key(f)] = net
                d.extra_boxes += [flag.body] + flag.field_boxes
                d.power.append((flag, net))
                power_parts.append(flag)
                done = True
                break
            if done:
                break
        if not done:
            unflagged.append(net)

    for part, num in nc_pins:
        d.nc.append(part.pins[num][0])

    # junctions: three or more connections at a point
    ends: dict[tuple[float, float], int] = {}
    for a, b, _ in d.wires:
        for q in (a, b):
            ends[d.key(q)] = ends.get(d.key(q), 0) + 1
    for p in placed:
        for tip, _, _ in p.pins.values():
            if d.key(tip) in ends:
                ends[d.key(tip)] += 1
    for ps, _ in d.power:
        k = d.key(ps.at)
        if k in ends:
            ends[k] += 1
    for k in list(ends):
        for a, b, _ in d.wires:
            if _on_seg(k, a, b, ends=False):
                ends[k] += 2
    junctions = [k for k, n in ends.items() if n >= 3]

    # block headings
    for name in dict.fromkeys(p.block for p in placed if p.block):
        members = [p for p in placed if p.block == name]
        x0 = min(p.keepout[0] for p in members)
        y0 = min(min(p.keepout[1], *(b[1] for b in p.field_boxes)) if p.field_boxes else p.keepout[1] for p in members)
        taken = [b for q in placed + power_parts for b in q.field_boxes + [q.keepout]] + d.extra_boxes
        hy = snap(y0 - 3.81)
        for _ in range(8):  # move it up until it's clear of labels and fields
            box = schematic_review._text_box(name, x0, hy, 0, 1.524, "left", "bottom")
            if not any(schematic_review._overlap(box, b) for b in taken):
                break
            hy = snap(hy - 2.54)
        d.texts.append((name, (snap(x0), hy)))
    for n in notes or []:
        d.texts.append((str(n["text"]), (snap(float(n["at"][0])), snap(float(n["at"][1])))))

    unconnected = sorted(f"{p.ref}.{num}" for p in placed for num, (_, _, pn) in p.pins.items()
                         if (p.ref, p.unit, num) not in pin_net and (p, num) not in nc_pins and not pn.hidden)

    text = _render(out_path, placed, power_parts, d, junctions, paper, title)
    sexpr.parse(text)
    snapshot = None
    root = project.find_root(out_path)
    if out_path.exists() and root:
        snapshot = project.snapshot(root, "before-schematic-write", paths=[str(out_path)])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8", newline="\n")

    review = schematic_review.review_file(str(out_path), erc=erc)
    result = {"ok": True, "path": str(out_path), "parts": len(placed), "nets": len(net_pins),
              "wired": sorted(set(wired)), "labelled_between_blocks": sorted({n for n, _, _ in d.labels} - set(label_only)),
              "review": {k: review[k] for k in ("counts", "findings", "more", "stats", "not_checked")},
              "next": "fix every review problem and warning (move parts with at, fields with ref_at/value_at), then "
                      "write again with replace=True; ask the user to look at the sheet in KiCad"}
    if snapshot:
        result["snapshot"] = snapshot
    if label_only:
        result["label_instead_of_wire"] = label_only
        result["label_note"] = "no clean wire path for these pins inside their block; a label joins them. Moving the " \
                               "parts closer or in line usually lets them be wired"
    if flipped:
        result["power_symbol_flipped"] = flipped
    if unflagged:
        result["no_pwr_flag"] = unflagged
    if unconnected:
        result["unconnected_pins"] = unconnected[:60]
        result["unconnected_note"] = "put each on a net or in no_connect; ERC flags them"
    return result


def _fmt(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _render(path: Path, parts: list[Part], power: list[Part], d: Drawing, junctions, paper: str, title: str) -> str:
    root_uuid = _uuid()
    proj = path.stem
    q = sexpr.quote
    lib_texts = list(dict.fromkeys(p.text for p in parts + power))
    out = [(f'(kicad_sch\n\t(version {FORMAT})\n\t(generator "cadmate")\n\t(generator_version "1.0")\n\t(uuid {q(root_uuid)})\n'
            f'\t(paper {q(paper)})')]
    if title:
        out.append(f"\t(title_block\n\t\t(title {q(title)})\n\t)")
    out.append("\t(lib_symbols\n" + "\n".join(lib_texts) + "\n\t)")
    for k in junctions:
        out.append(f'\t(junction (at {_fmt(k[0])} {_fmt(k[1])}) (diameter 0) (color 0 0 0 0) (uuid {q(_uuid())}))')
    for pt in d.nc:
        out.append(f"\t(no_connect (at {_fmt(pt[0])} {_fmt(pt[1])}) (uuid {q(_uuid())}))")
    for a, b, _ in d.wires:
        out.append(f"\t(wire (pts (xy {_fmt(a[0])} {_fmt(a[1])}) (xy {_fmt(b[0])} {_fmt(b[1])})) "
                   f"(stroke (width 0) (type default)) (uuid {q(_uuid())}))")
    for name, (x, y), angle in d.labels:
        just = "left bottom" if angle in (0, 90) else "right bottom"
        out.append(f"\t(label {q(name)} (at {_fmt(x)} {_fmt(y)} {angle}) (fields_autoplaced yes) "
                   f"(effects (font (size 1.27 1.27)) (justify {just})) (uuid {q(_uuid())}))")
    for text, (x, y) in d.texts:
        out.append(f"\t(text {q(text)} (exclude_from_sim no) (at {_fmt(x)} {_fmt(y)} 0) "
                   f"(effects (font (size 1.524 1.524) (bold yes)) (justify left bottom)) (uuid {q(_uuid())}))")
    for p in parts + power:
        out.append(_instance(p, root_uuid, proj))
    out.append('\t(sheet_instances (path "/" (page "1")))\n\t(embedded_fonts no)\n)\n')
    return "\n".join(out)


def _instance(p: Part, root_uuid: str, proj: str) -> str:
    q = sexpr.quote
    in_bom = "no" if p.power else "yes"
    lines = [f"\t(symbol (lib_id {q(p.lib_id)}) (at {_fmt(p.at[0])} {_fmt(p.at[1])} {p.rot})"
             + (f" (mirror {p.mirror})" if p.mirror else "")
             + f" (unit {p.unit}) (exclude_from_sim no) (in_bom {in_bom}) (on_board yes) (dnp no) (uuid {q(_uuid())})"]

    def prop(name: str, value: str, at: Pt, just: str = "", hide: bool = False) -> str:
        j = f" (justify {just})" if just else ""
        h = " (hide yes)" if hide else ""
        return (f"\t\t(property {q(name)} {q(value)} (at {_fmt(at[0])} {_fmt(at[1])} 0) "
                f"(effects (font (size 1.27 1.27)){j}){h})")
    if p.power:  # fields where the library puts them, upright
        for name, val, hide in (("Reference", p.ref, True), ("Value", p.value, False)):
            lp = p.sym.properties.get(name)
            at = _xf(lp.x, lp.y, p.at, p.rot, "") if lp else p.at
            lines.append(prop(name, val, at, hide=hide))
        lines += [prop("Footprint", "", p.at, hide=True), prop("Datasheet", "", p.at, hide=True)]
    else:
        (rx, ry), rj = p.field_at["Reference"]
        (vx, vy), vj = p.field_at["Value"]
        lines += [prop("Reference", p.ref, (rx, ry), rj), prop("Value", p.value, (vx, vy), vj),
                  prop("Footprint", p.footprint, p.at, hide=True),
                  prop("Datasheet", str(p.fields.get("Datasheet") or p.sym.prop("Datasheet") or ""), p.at, hide=True),
                  prop("Description", str(p.sym.prop("Description") or ""), p.at, hide=True)]
        for k, v in p.fields.items():
            if k in ("ref_at", "value_at", "Datasheet") or k.lower() in ("reference", "value", "footprint", "description"):
                continue
            lines.append(prop(str(k), str(v), p.at, hide=True))
    for num in sorted({n for pin in p.sym.pins_of(p.unit) for n in pin.numbers}):
        lines.append(f"\t\t(pin {q(num)} (uuid {q(_uuid())}))")
    lines.append(f"\t\t(instances (project {q(proj)} (path {q('/' + root_uuid)} (reference {q(p.ref)}) (unit {p.unit}))))")
    lines.append("\t)")
    return "\n".join(lines)
